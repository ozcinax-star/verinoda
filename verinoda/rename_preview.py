"""Rename preview: every place a rename of one symbol would touch, each with its evidence, without editing.

``verinoda rename-preview compute_total price_total`` resolves the old name exactly
(:func:`verinoda.naming.resolve`) and reads every line that spells it (the name as a whole word), sorted by what
ties the line to the symbol:

- **sites**, the lines a rename changes:

  * the definition (``statically_verified``: the node's own line spells the name);
  * a line an edge of the index ties to it (a call, an import, a reference, a subclass): a call line is graded as
    ``analyze`` grades it (:func:`verinoda.entail.call_site`: ``statically_verified`` only for an EXTRACTED edge
    whose Python line calls it, ``strong_inference`` otherwise); an import line naming the definition's module is
    ``statically_verified`` in Python, other edge lines ``strong_inference`` (EXTRACTED); a line whose only tie
    is a guessed (INFERRED) edge that grades no better than ``weak_inference`` is a mention, not a site (a
    local variable of the same name draws such edges);
  * a line bound to it by where it is (``strong_inference``): inside a caller the index ties to it (one edge stands
    for every call of a caller to a callee: the second call of ``pay`` has no edge of its own), in a file that
    imports it by this name, or in its own module (its class, for a method);
  * an override or overload: a method of the same name in a class above or below the owner in the class hierarchy
    (a rename that leaves it breaks the override) or an overload in the same class, with its own call lines
    (``strong_inference`` at most: the tie is by name);

- **mentions**: the name elsewhere, in code, a comment, a string or a document, that nothing but at most a
  guessed edge ties to the symbol (``weak_inference``: it may be the symbol, reflection or a word). A rename tool that follows bindings leaves
  them; a text replacement changes them all;
- **other symbols** of the same name (defined elsewhere, not in its family) and the lines the index ties to
  them: left alone;
- **conflicts**: the new name already defined in the same scope (the owner class, the module, the class family),
  or already spelled in a file the rename edits (a local or an import that would shadow or be shadowed:
  ``weak_inference``, read it); a new name that is not an identifier or is a keyword is refused.

A line in a file changed since the index keeps ``strong_inference`` at most (its edges may point at lines that
moved). An edge line that does not spell the name (a call through an import alias: ``ct([2])``) is not a site; it
is listed under ``not_spelled`` with the reason. Nothing is written: not the code, not a claim.
"""
from __future__ import annotations

import keyword
import re
import time
from collections import deque
from pathlib import Path

MAX_SITES = 300          # per list (sites, mentions, other symbols); "truncated" says when one was cut
_SCAN_SECONDS = 10.0     # the mention scan over the repository's files; past it the rest is not read (said)
_STRUCTURAL = {"contains", "method", "rationale_for", "defines"}  # the symbol's own place, not a use of it
_SUPERTYPES = {"inherits", "implements", "extends", "mixes_in"}
_IDENT = re.compile(r"^[A-Za-z_$][\w$]*$")
# reserved words of the other languages the index reads (Python's come from `keyword`)
_RESERVED = {"abstract", "boolean", "break", "byte", "case", "catch", "char", "class", "const", "continue",
             "default", "do", "double", "else", "enum", "export", "extends", "final", "finally", "float", "for",
             "fun", "function", "goto", "if", "implements", "import", "instanceof", "int", "interface", "let",
             "long", "native", "new", "null", "package", "private", "protected", "public", "return", "short",
             "static", "super", "switch", "synchronized", "this", "throw", "throws", "transient", "try",
             "typeof", "val", "var", "void", "volatile", "when", "while", "yield", "true", "false"}
_RANK = ["statically_verified", "strong_inference", "weak_inference"]


def _word(name: str) -> re.Pattern:
    return re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")


def bare_name(label: str) -> str:
    """The identifier a label spells: ``compute_total()`` -> ``compute_total``, ``.size()`` -> ``size``."""
    return (label or "").strip().lstrip(".").split("(")[0].strip().rpartition(".")[2]


def check_new_name(new: str, old: str) -> str | None:
    """Why ``new`` cannot replace ``old`` (None when it can)."""
    if not _IDENT.match(new or ""):
        return f"`{new}` is not an identifier (letters, digits, _ or $, not starting with a digit)"
    if new == old:
        return f"`{new}` is the current name"
    if keyword.iskeyword(new) or new in _RESERVED:
        return f"`{new}` is a reserved word"
    return None


def _weaker(a: str, b: str) -> str:
    return a if _RANK.index(a) >= _RANK.index(b) else b


class _Preview:
    def __init__(self, g, nid: str, new: str, *, stale=()):
        from verinoda import index as ix

        self.g, self.nid, self.new = g, nid, new
        self.old = bare_name(g.label(nid))
        self.rx = _word(self.old)
        self.stale = set(stale or ())
        self._lines: dict[str, list[str]] = {}
        self._ix = ix
        self.sites: dict[tuple[str, int], dict] = {}     # (file, line) -> site
        self.mentions: dict[tuple[str, int], dict] = {}  # (file, line) -> a spelling nothing sure ties to it
        self.not_spelled: list[dict] = []
        self.others: dict[tuple[str, int], dict] = {}    # lines tied to another symbol of the same name
        self.bound_spans: list[tuple[str, int, int, str]] = []  # (file, start, end, why): calls bound by place
        self.bound_files: dict[str, str] = {}            # file -> why (it imports the name / defines it)

    # -- reading ------------------------------------------------------------------
    def lines(self, f: str) -> list[str]:
        if f not in self._lines:
            self._lines[f] = self._ix.file_lines(Path(self.g.root) / f) or []
        return self._lines[f]

    def text(self, f: str, ln: int) -> str:
        ls = self.lines(f)
        return ls[ln - 1] if 0 < ln <= len(ls) else ""

    def label(self, n: str) -> str:
        """``Class.method`` for a method (its owner by the ``method`` edge), else the label."""
        own = self.owner(n)
        base = self.g.label(n).lstrip(".").removesuffix("()")
        return f"{self.g.label(own)}.{base}" if own else self.g.label(n)

    def owner(self, n: str) -> str | None:
        return next((u for u, _d in self.g.in_edges(n, {"method"})), None)

    # -- the family: the symbol, its overloads and overrides ------------------------
    def family(self, overloads: list[str]) -> dict[str, str]:
        """node -> why it is renamed with the symbol (``""`` for the symbol itself)."""
        g = self.g
        fam = {self.nid: ""}
        for o in overloads:
            if o != self.nid:
                fam[o] = "an overload of the same name in the same class"
        own = self.owner(self.nid)
        if own is None:
            return fam

        def walk(start: str, up: bool) -> list[str]:
            seen, out, q = {start}, [], deque([start])
            while q and len(seen) < 200:
                c = q.popleft()
                nxt = ([v for v, _d in g.out_edges(c, _SUPERTYPES)] if up
                       else [u for u, _d in g.in_edges(c, _SUPERTYPES)])
                for x in nxt:
                    if x not in seen:
                        seen.add(x)
                        out.append(x)
                        q.append(x)
            return out

        def methods(cls: str) -> list[str]:
            return [m for m, _d in g.out_edges(cls, {"method"}) if bare_name(g.label(m)) == self.old]

        ups = walk(own, True)
        classes = {c: "above" for c in ups}
        classes.update({c: "below" for c in walk(own, False)})
        for c in ups:  # a base that declares the method: its other subclasses override it too
            if methods(c):
                for d in walk(c, False):
                    classes.setdefault(d, "beside")
        for c, where in classes.items():
            if c == own:
                continue
            for m in methods(c):
                fam.setdefault(m, {"above": f"the method it overrides in {g.label(c)}",
                                   "below": f"an override in the subclass {g.label(c)}",
                                   "beside": f"an override of the same base method in {g.label(c)}"}[where])
        return fam

    # -- sites --------------------------------------------------------------------
    def add(self, bucket: dict, f: str, ln: int, kind: str, status: str, why: str, **extra) -> None:
        """One entry per spelling of the name on ``f:ln`` (the first reason for a line wins)."""
        line = self.text(f, ln)
        if f in self.stale:
            status = _weaker(status, "strong_inference")
            why += f"; {f} changed since the index (run `verinoda update`)"
        for m in self.rx.finditer(line):
            key = (f, ln, m.start())
            if key in bucket or (bucket is not self.sites and key in self.sites):
                continue
            if bucket is self.sites:  # a surer tie moves a guessed one out of the mentions
                self.mentions.pop(key, None)
            bucket[key] = {"at": f"{f}:{ln}", "column": m.start() + 1, "kind": kind, "status": status,
                           "why": why, "line": _window(line, m.start(), m.end()), **extra}

    def edge_site(self, u: str, v: str, d: dict, cap: str | None, why_family: str) -> None:
        g = self.g
        rel = d.get("relation")
        f = d.get("source_file") or g.file(u)
        m = re.match(r"L(\d+)", str(d.get("source_location") or ""))
        if not f or not m:
            return
        ln = int(m.group(1))
        src = self.label(u)
        extracted = d.get("confidence") == "EXTRACTED"
        if not self.rx.search(self.text(f, ln)):
            why = (f"{f}:{ln} does not spell `{self.old}`"
                   + (" (a call through an import alias or another name: the line keeps it)" if rel == "calls"
                      else "") + (f"; {f} changed since the index" if f in self.stale else ""))
            self.not_spelled.append({"at": f"{f}:{ln}", "relation": rel, "from": src, "to": self.label(v),
                                     "why": why})
            return
        if rel == "calls":
            status, why = self.call_status(u, v, f, ln, extracted)
            kind = "call"
        elif rel in ("imports", "imports_from", "re_exports"):
            status, why = self.import_status(v, f, ln, extracted)
            kind = "import"
        elif rel in _SUPERTYPES:
            status, why = ("strong_inference" if extracted else "weak_inference"), f"{src} {rel} it"
            kind = "supertype"
        else:
            status = "strong_inference" if extracted else "weak_inference"
            why = f"{src} {rel} it ({'EXTRACTED' if extracted else 'INFERRED'} edge)"
            kind = "reference"
        if cap:
            status = _weaker(status, cap)
            why += f"; {why_family}"
        if status == "weak_inference":
            self.add(self.mentions, f, ln, "inferred", status, why + ": a guess, not a site", via=rel,
                     **{"from": src})
        else:
            self.add(self.sites, f, ln, kind, status, why, via=rel, **{"from": src})
        if not extracted:  # a guessed edge binds nothing else by place
            return
        if rel in ("imports", "imports_from") and not g.span(u):
            self.bound_files.setdefault(f, f"{f} imports `{self.old}` at line {ln}")
        elif g.is_symbol(u) and (sp := g.span(u)):
            self.bound_spans.append((f, sp[0], sp[1], f"inside {src}, which the index ties to it at {f}:{ln}"))

    def call_status(self, u: str, v: str, f: str, ln: int, extracted: bool) -> tuple[str, str]:
        from verinoda import entail

        g = self.g
        src = self.label(u)
        caller = g.label(u) if g.is_symbol(u) else None
        try:
            grade = entail.call_site(g.root, f, ln, g.label(v), caller=caller, target_path=g.file(v),
                                     target_qual=g.label(v), relation="calls")
        except (OSError, ValueError, RecursionError):
            grade = None
        conf = "EXTRACTED" if extracted else "INFERRED"
        if grade is not None and grade.grade == "full":
            if extracted and f.endswith((".py", ".pyi")):
                return "statically_verified", f"{src} calls it here (the line calls `{self.old}`; {conf} edge)"
            return "strong_inference", (f"{src} calls it here ({conf} edge"
                                        + ("; which definition the line binds is checked for Python only)"
                                           if not f.endswith((".py", ".pyi")) else ")"))
        if grade is not None and grade.grade == "partial":
            return "strong_inference", f"{src} calls it here ({conf} edge; {grade.reason})"
        reason = f"; {grade.reason}" if grade is not None and grade.reason else ""
        return ("strong_inference" if extracted else "weak_inference"), f"{src} calls it here ({conf} edge{reason})"

    def import_status(self, v: str, f: str, ln: int, extracted: bool) -> tuple[str, str]:
        line = self.text(f, ln)
        mod = Path(self.g.file(v) or "").stem
        if (extracted and f.endswith((".py", ".pyi")) and mod
                and re.match(rf"\s*from\s+[\w.]*\b{re.escape(mod)}\s+import\b", line)):
            return "statically_verified", f"the line imports `{self.old}` from its module {mod}"
        return ("strong_inference" if extracted else "weak_inference"), \
            f"an import the index ties to it ({'EXTRACTED' if extracted else 'INFERRED'} edge)"

    def collect(self, fam: dict[str, str]) -> None:
        g = self.g
        for n, why_family in fam.items():
            f, ln = g.file(n), g.line(n)
            cap = "strong_inference" if why_family else None
            if f and ln:
                if why_family:
                    self.add(self.sites, f, ln, "override" if "overrid" in why_family else "overload",
                             "strong_inference", why_family)
                elif self.rx.search(self.text(f, ln)):
                    self.add(self.sites, f, ln, "definition", "statically_verified",
                             f"the definition of {self.label(n)}")
                else:
                    self.not_spelled.append({"at": f"{f}:{ln}", "relation": "definition", "from": self.label(n),
                                             "to": self.label(n), "why": f"{f}:{ln} does not spell `{self.old}`"
                                             + (f"; {f} changed since the index" if f in self.stale else "")})
            for u, d in g.in_edges(n):
                if d.get("relation") in _STRUCTURAL:
                    continue
                self.edge_site(u, n, d, cap, why_family)
        # the symbol's own module (its class, for a method) binds the name by place
        own = self.owner(self.nid)
        f = g.file(self.nid)
        if f:
            sp = g.span(own) if own else None
            if sp:
                self.bound_spans.append((f, sp[0], sp[1], f"inside its class {g.label(own)}"))
            elif not own and f.endswith((".py", ".pyi")):
                self.bound_files.setdefault(f, f"in its own module {f}")
            elif not own:
                sp = g.span(self.nid)
                if sp:
                    self.bound_spans.append((f, sp[0], sp[1], "inside its own definition"))

    def other_symbols(self, fam: dict[str, str]) -> list[dict]:
        """Symbols of the same name outside the family, and the lines the index ties to them."""
        g = self.g
        out = []
        for n in g.G.nodes:
            if n in fam or bare_name(g.label(n)) != self.old or not g.is_symbol(n):
                continue
            f, ln = g.file(n), g.line(n)
            out.append({"id": n, "label": self.label(n), "at": f"{f}:{ln}" if ln else f})
            if f and ln:
                self.add(self.others, f, ln, "other_symbol", "statically_verified",
                         f"the definition of {self.label(n)}, another symbol of the same name")
            for u, d in g.in_edges(n):
                m = re.match(r"L(\d+)", str(d.get("source_location") or ""))
                ef = d.get("source_file") or g.file(u)
                if d.get("relation") in _STRUCTURAL or not m or not ef:
                    continue
                self.add(self.others, ef, int(m.group(1)), "other_symbol", "strong_inference",
                         f"{self.label(u)} {d.get('relation')} {self.label(n)} ({f}:{ln}), another symbol "
                         "of the same name", via=d.get("relation"))
        out.sort(key=lambda r: r["at"] or "")
        return out

    def prefilter(self, files: list[str]) -> tuple[list[str], str | None]:
        """The files to read: those whose indexed version the search index says may spell the name (its token in
        a passage or a unit name), and every file it does not index, skipped or that changed since it indexed
        them, except generated output (``results/``, cassettes: a rename does not edit them). All of ``files``
        when the search index is missing or describes another graph."""
        import bisect
        import sqlite3

        from verinoda import search_index as si
        from verinoda.index import same_graph

        toks = si.tokens(self.old)
        db = si.db_path_for(self.g)
        if not toks or not db.is_file():
            return files, None
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2.0)
        except sqlite3.Error:
            return files, None
        try:
            m = si._meta(conn)
            if not (m.get("schema_version") == si.SCHEMA_VERSION and m.get("tokenizer_version") == si.TOKENIZER_VERSION
                    and same_graph(self.g.path, m.get("graph"))):
                return files, None
            rows = {r[0]: r[1:] for r in conn.execute("SELECT file, size, mtime_ns, pid_lo, pid_hi, skipped "
                                                      "FROM files")}
            pids = sorted(p for (p,) in conn.execute("SELECT pid FROM body WHERE term = ?", (toks[0],)))
            named = {f for (f,) in conn.execute("SELECT DISTINCT u.file FROM names n JOIN units u ON u.uid = n.uid "
                                                "WHERE n.term = ?", (toks[0],))}
            generated = {f for f, why in conn.execute("SELECT file, reason FROM unindexed")
                         if str(why).startswith("generated output")}
        except sqlite3.Error:
            return files, None
        finally:
            conn.close()
        out = []
        for f in files:
            r = rows.get(f)
            if f in generated:
                continue
            if r is None or r[4]:
                out.append(f)
                continue
            try:
                st = (Path(self.g.root) / f).stat()
            except OSError:
                continue
            if (st.st_size, st.st_mtime_ns) != (r[0], r[1]) or f in named:
                out.append(f)
            elif r[2] is not None and r[3] is not None:
                i = bisect.bisect_left(pids, r[2])
                if i < len(pids) and pids[i] <= r[3]:
                    out.append(f)
        gen = sum(1 for f in files if f in generated)
        return out, (f"{len(files) - len(out) - gen} file(s) the search index shows do not spell `{self.old}` "
                     "were not read (an import line is not indexed: a file that spells it only there and has no "
                     "import edge to it is missed)" + (f", nor {gen} file(s) of generated output" if gen else ""))

    def scan(self) -> tuple[dict, dict]:
        """Every other spelling in the repository: bound by place (sites) or mentions; and the scan's coverage."""
        from verinoda import question_plan as qp
        from verinoda.snapshot import list_files

        try:
            every = list_files(Path(self.g.root))
        except (OSError, ValueError):
            every = sorted({f for n in self.g.G.nodes if (f := self.g.file(n))})
        files, skipped = self.prefilter(every)
        cov: dict = {"files": len(every), **({"skipped": skipped} if skipped else {})}
        mentions = self.mentions
        t0 = time.perf_counter()
        read = 0
        for i, f in enumerate(files):
            if time.perf_counter() - t0 > _SCAN_SECONDS:
                return mentions, {**cov, "read": read, "not_read": files[i:i + 20],
                                  "why": f"the scan stopped after {_SCAN_SECONDS:.0f}s"}
            if f.lower().endswith(qp._TEXT_SUFFIXES_SKIP):
                continue
            data = qp._read_small(Path(self.g.root) / f)
            read += 1
            if not data or self.old not in data:
                continue
            self._lines.setdefault(f, data.splitlines())
            for ln, line in enumerate(self._lines[f], 1):
                if not self.rx.search(line):
                    continue
                bound = next((why for bf, a, b, why in self.bound_spans if bf == f and a <= ln <= b), None)
                bound = bound or self.bound_files.get(f)
                if bound and not any((f, ln, m.start()) in self.others for m in self.rx.finditer(line)):
                    self.add(self.sites, f, ln, "bound", "strong_inference", bound)
                elif not any((f, ln, m.start()) in self.others for m in self.rx.finditer(line)):
                    self.add(self.mentions, f, ln, "mention", "weak_inference",
                             "spells the name; nothing in the index ties this line to the symbol "
                             "(another symbol, reflection, a comment, a string or a document)")
        return mentions, {**cov, "read": read}

    def conflicts(self, fam: dict[str, str], site_files: set[str]) -> list[dict]:
        g, new = self.g, self.new
        out: list[dict] = []
        own = self.owner(self.nid)
        seen: set[str] = set()
        for o in dict.fromkeys(o for n in fam if (o := self.owner(n)) is not None):
            for m, _d in g.out_edges(o, {"method"}):
                if bare_name(g.label(m)) == new and m not in seen:
                    seen.add(m)
                    out.append(self._conflict(m, f"{g.label(o)} already has a member `{new}`"))
        if own is None:
            f = g.file(self.nid)
            for n in g.symbols_in(f or ""):
                if n not in seen and self.owner(n) is None and bare_name(g.label(n)) == new:
                    seen.add(n)
                    out.append(self._conflict(n, f"{f} already defines `{new}`"))
        rx = _word(new)
        for f in sorted(site_files):
            hit = next((i for i, ln in enumerate(self.lines(f), 1) if rx.search(ln)), None)
            if hit and not any(c["at"] == f"{f}:{hit}" for c in out):
                out.append({"at": f"{f}:{hit}", "status": "weak_inference", "line": self.text(f, hit).strip()[:200],
                            "why": f"{f} already spells `{new}`, and the rename edits this file: a name that "
                                   "could shadow or be shadowed (read it)"})
        return out

    def _conflict(self, n: str, why: str) -> dict:
        f, ln = self.g.file(n), self.g.line(n)
        line = self.text(f, ln) if f and ln else ""
        return {"at": f"{f}:{ln}", "status": "statically_verified" if _word(self.new).search(line)
                else "strong_inference", "line": line.strip()[:200], "why": why, "id": n}


def _window(line: str, a: int, b: int, width: int = 200) -> str:
    """The line (stripped), or the part of a long one around the match at ``a:b``."""
    if len(line.strip()) <= width:
        return line.strip()
    lo = max(0, a - (width - (b - a)) // 2)
    return ("..." if lo else "") + line[lo:lo + width].strip() + ("..." if lo + width < len(line) else "")


def _cut(rows: list[dict], n: int) -> tuple[list[dict], bool]:
    return rows[:n], len(rows) > n


def _order(bucket: dict) -> list[dict]:
    """The definition first, then by file, line and column."""
    return [row for _k, row in sorted(bucket.items(), key=lambda x: (x[1]["kind"] != "definition", *x[0]))]


def preview(g, nid: str, new: str, *, stale=(), overloads: list[str] | None = None,
            max_sites: int = MAX_SITES) -> dict:
    """The rename preview of node ``nid`` to ``new`` (see the module docstring)."""
    p = _Preview(g, nid, new, stale=stale)
    fam = p.family(overloads or [])
    p.collect(fam)
    others = p.other_symbols(fam)
    mentions, coverage = p.scan()
    sites, cut_s = _cut(_order(p.sites), max_sites)
    ments, cut_m = _cut(_order(mentions), max_sites)
    left, cut_o = _cut(_order(p.others), max_sites)
    site_files = {k[0] for k in p.sites}
    by_status: dict[str, int] = {}
    for r in p.sites.values():
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    f, ln = g.file(nid), g.line(nid)
    return {
        "status": "found", "symbol": p.label(nid), "id": nid, "at": f"{f}:{ln}", "old": p.old, "new": new,
        "sites": sites, "not_spelled": p.not_spelled, "mentions": ments,
        "other_symbols": others[:max_sites], "other_symbol_lines": left,
        "conflicts": p.conflicts(fam, site_files),
        "family": [{"id": n, "label": p.label(n), "at": f"{g.file(n)}:{g.line(n)}", "why": why}
                   for n, why in fam.items() if why],
        "counts": {"sites": len(p.sites), "files": len(site_files), "mentions": len(mentions),
                   "other_symbol_lines": len(p.others), "by_status": by_status},
        "coverage": coverage,
        "truncated": cut_s or cut_m or cut_o or len(others) > max_sites or "not_read" in coverage,
        "written": "nothing: a preview reads the code and the index, and edits and records nothing",
        "note": "sites are what a rename changes (their status says how sure the tie is); mentions spell the name "
                "with nothing tying them to the symbol: read each; the index does not see reflection, "
                "string-built names or code outside the repository",
    }


def run(g, symbol: str, new: str, *, stale=(), max_sites: int = MAX_SITES) -> dict:
    """:func:`preview` for a name (resolved by :func:`verinoda.naming.resolve`); ``status`` found, ambiguous (the
    candidates), not_found, not_a_symbol (a file or module) or invalid (the new name)."""
    from verinoda import naming

    r = naming.resolve(g, symbol, stale=stale)
    if r.status == naming.EXACT and r.node:
        if not g.is_symbol(r.node):
            return {"status": "not_a_symbol", "query": symbol, "candidates": [],
                    "note": f"`{symbol}` is a file or module ({g.file(r.node)}): only a symbol's rename is "
                            "previewed (a file rename also changes the imports' module paths)"}
        why = check_new_name(new, bare_name(g.label(r.node)))
        if why:
            return {"status": "invalid", "query": symbol, "new": new, "candidates": [], "note": why}
        res = preview(g, r.node, new, stale=stale, overloads=r.overloads, max_sites=max_sites)
        return {**res, **({"resolution_note": r.note} if r.note else {})}
    cands = [{"id": c, "label": g.label(c), "at": f"{g.file(c)}:{g.line(c)}"} for c in list(r.candidates)[:10]
             if c in g.G]
    return {"status": "ambiguous" if r.status == naming.AMBIGUOUS else "not_found", "query": symbol,
            "candidates": cands, "note": r.note}


def render(res: dict) -> str:
    if res.get("status") != "found":
        out = [f"{res['query']}: {res['status']}" + (f" ({res['note']})" if res.get("note") else "")]
        out += [f"  - {c['label']} ({c['at']})" for c in res.get("candidates") or []]
        return "\n".join(out)
    c = res["counts"]
    out = [f"rename {res['symbol']} ({res['at']}): `{res['old']}` -> `{res['new']}`"]
    if res.get("resolution_note"):
        out.append(f"note: {res['resolution_note']}")
    by = ", ".join(f"{n} {s}" for s, n in sorted(c["by_status"].items(), key=lambda x: _RANK.index(x[0])))
    out.append(f"  {c['sites']} site(s) in {c['files']} file(s) ({by})")
    for s in res["sites"]:
        out.append(f"    {s['at']}:{s['column']}  {s['kind']} [{s['status']}]  {s['line']}")
        out.append(f"        {s['why']}")
    for fm in res.get("family") or []:
        out.append(f"  renamed with it: {fm['label']} ({fm['at']}): {fm['why']}")
    for x in res.get("not_spelled") or []:
        out.append(f"  not a site: {x['at']} ({x['relation']} from {x['from']}): {x['why']}")
    if res["mentions"]:
        out.append(f"  {c['mentions']} mention(s) nothing ties to it (weak_inference; a text replace would change "
                   "them, a rename that follows bindings would not: read each)")
        for m in res["mentions"]:
            out.append(f"    {m['at']}:{m['column']}  {m['line']}")
            if m["kind"] == "inferred":
                out.append(f"        {m['why']}")
    if res["other_symbols"]:
        out.append(f"  other symbols named `{res['old']}` (left alone): "
                   + ", ".join(f"{o['label']} ({o['at']})" for o in res["other_symbols"][:10]))
        out += [f"    {o['at']}:{o['column']}  {o['why']}" for o in res["other_symbol_lines"][:20]]
    for k in res.get("conflicts") or []:
        out.append(f"  conflict [{k['status']}] {k['at']}: {k['why']}  {k['line']}")
    cov = res.get("coverage") or {}
    if cov.get("why"):
        out.append(f"  mentions: {cov['why']}; {cov['read']} of {cov['files']} files read")
    elif cov.get("skipped"):
        out.append(f"  mentions: {cov['read']} of {cov['files']} files read; {cov['skipped']}")
    if res.get("truncated"):
        out.append("  (lists were cut: --max-sites N, or --json)")
    out.append(f"  written: {res['written']}")
    return "\n".join(out)
