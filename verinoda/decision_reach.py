"""Decision records a change reaches: the records to read before a diff is merged (docs/DESIGN.md D33, D35).

``verinoda review`` calls :func:`reached` with the changed definitions and the changed files. A record is
listed when the change reaches what its front matter names, each reach with the changed line (``at``), the
front matter line it matched (``evidence_at``) and a status:

* ``governs``: a changed, added or removed definition is the governed symbol or lies inside it
  (``statically_verified``: the symbol's file and qualified name are compared as text); a changed line inside
  the governed symbol's span that no definition change counts, such as a docstring or comment edit
  (``statically_verified``: the diff's lines against the parsed span); a changed file of the governed symbol
  whose definitions could not be read (``strong_inference``);
* ``only_in``: a changed file is one the guard allows the call in (``statically_verified``, a glob match); a
  changed line's code (comments and strings blanked), in a file ``decide check`` would search for the guard,
  holds the guard's dotted call or matches its pattern (``strong_inference``: text, names are not bound), or
  holds only the call's last name (``weak_inference``);
* ``no_edge``: a changed file matches the guard's ``from`` or ``to`` glob (``statically_verified``); the same
  for the globs of ``layers`` (``order``), ``allow_edges`` (``from``, ``allowed``) and ``public`` (``module``,
  ``api``), a ``tag:NAME`` read as its committed globs (D93);
* ``dependency`` and ``revisit-when dependency_added``: a changed line of a manifest ``decide check`` reads
  names the package, compared as ``decide check`` compares names (runs of ``-``, ``_`` and ``.`` alike)
  (``strong_inference``);
* ``revisit-when file_appears``: an added file matches the glob (``statically_verified``);
* the record's own file, or the hand-written document it was imported from, is in the diff, including a
  record the change deletes or takes out of force (``statically_verified``).

A guard ``decide check`` does not run (a proposed guard, a record that is not enforced) still reaches, and
each of its reaches says so; its own ``pattern`` is not run. Entries of a hand-edited record are read only
when they have the types a record needs.

Nothing here decides whether the change keeps a decision: ``verinoda decide check`` does that; this only says
which records to read. Superseded, rejected and deprecated records are counted, not listed, unless the change
edits their file.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path, PurePosixPath

LIVE_STATUSES = ("accepted", "proposed")
MAX_REACH_PER_RECORD = 8
LIMITS = [
    "a reach is where the change meets a record's front matter (governed symbols, guard files, globs, calls and "
    "patterns); whether the change keeps the decision is `verinoda decide check`'s answer",
    "calls are matched as text on changed lines (the base side is not searched); a call through an alias, "
    "`sink=` guards and edges added in unchanged files are not seen",
    "a guard's own `pattern` is run only for the guards `decide check` runs (accepted guards of enforced records)",
    "hand-written decision documents without a record (`verinoda decide import`) have no front matter to match",
]
_RANK = {"statically_verified": 0, "strong_inference": 1}


def _strs(value) -> list[str]:
    """The non-empty strings of a front matter list; anything else (a hand-edited string, numbers) gives none."""
    return [x for x in value if isinstance(x, str) and x.strip()] if isinstance(value, list) else []


def _front_lines(path: Path | None) -> dict[str, int]:
    """1-based line of each front matter key of a record file (``governs``, ``guards``, ``revisit-when``)."""
    out: dict[str, int] = {}
    if path is None:
        return out
    try:
        text = path.read_bytes().decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    except OSError:
        return out
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return out
    for i, ln in enumerate(lines[1:], start=2):
        if ln.strip() == "---":
            break
        key = ln.partition(":")[0].strip()
        if key and key not in out:
            out[key] = i
    return out


def _glob(path: str, pattern: str) -> bool:
    from verinoda.guards import glob_match

    try:
        return glob_match(path, pattern)
    except Exception:  # noqa: BLE001 - a hand-edited pattern must not stop the review
        return False


def _call_patterns(g: dict, run_pattern: bool) -> list[tuple[re.Pattern, str, str]]:
    """(regex, status, what) for the calls or pattern an only_in guard watches. ``run_pattern``: the guard's
    own regex is run only for a guard ``decide check`` runs."""
    out = []
    pattern = g.get("pattern")
    if run_pattern and isinstance(pattern, str) and pattern:
        try:
            out.append((re.compile(pattern), "strong_inference", f"matches pattern `{pattern}`"))
        except re.error:
            pass
    for c in _strs(g.get("calls")):
        parts = c.strip().split(".")
        out.append((re.compile(r"(?<![\w$.])" + r"\s*\.\s*".join(map(re.escape, parts)) + r"\b"),
                    "strong_inference", f"calls `{c}`"))
        out.append((re.compile(r"(?<![\w$])" + re.escape(parts[-1]) + r"\s*\("), "weak_inference",
                    f"calls a `{parts[-1]}` (the last name of `{c}`; its owner is not bound)"))
    return out


class _Files:
    """The changed files: their changed lines on the new side, code text (comments and strings blanked),
    whether each was added, and which ones only a planned change names."""

    def __init__(self, diffs, changes):
        from verinoda.review import _changed_lines

        self.new_lines: dict[str, set[int]] = {}
        self.old_lines: dict[str, set[int]] = {}
        self.text: dict[str, str | None] = {}
        self.old_text: dict[str, str | None] = {}
        self.added: set[str] = set()
        self.planned: set[str] = set()
        self._code: dict[str, list[str]] = {}
        for fd in diffs:
            n, o = _changed_lines(fd.old, fd.new)
            self.new_lines[fd.rel], self.old_lines[fd.rel] = n, o
            self.text[fd.rel], self.old_text[fd.rel] = fd.new, fd.old
            if fd.old is None and fd.new is not None:
                self.added.add(fd.rel)
        for c in changes:   # a planned change has no diff: its definitions' lines stand for the changed lines
            if c.file not in self.text:
                self.new_lines.setdefault(c.file, set()).update(c.new_changed or (
                    set(range(c.lines[0], c.lines[1] + 1)) if c.lines else set()))
                self.text.setdefault(c.file, None)
                self.planned.add(c.file)

    @property
    def names(self) -> list[str]:
        return sorted(self.text)

    def first_line(self, rel: str) -> str:
        ls = self.new_lines.get(rel) or self.old_lines.get(rel)
        return f"{rel}:{min(ls)}" if ls else rel

    def code_lines(self, rel: str, repo: Path) -> list[str]:
        if rel not in self._code:
            self._code[rel] = self._code_lines(rel, repo)
        return self._code[rel]

    def _code_lines(self, rel: str, repo: Path) -> list[str]:
        from verinoda.guards import code_text

        text = self.text.get(rel)
        if text is None:
            try:
                text = (repo / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return []
        try:
            return code_text(text, PurePosixPath(rel).suffix).split("\n")
        except Exception:  # noqa: BLE001 - an unreadable file is only not searched
            return text.split("\n")

    def raw_changed(self, rel: str) -> list[tuple[str, int, str]]:
        """(side, line, text) of every changed line of a file, both sides."""
        out = []
        for side, text, lines in (("new", self.text.get(rel), self.new_lines.get(rel)),
                                  ("base", self.old_text.get(rel), self.old_lines.get(rel))):
            rows = (text or "").split("\n")
            out += [(side, ln, rows[ln - 1]) for ln in sorted(lines or ()) if 0 < ln <= len(rows)]
        return out


def _reach(kind: str, entry: str | None, at: str, why: str, status: str, evidence: str | None) -> dict:
    d = {"kind": kind, "entry": entry, "at": at, "why": why, "status": status}
    if evidence:
        d["evidence_at"] = [evidence]
    return d


def _span(text: str | None, rel: str, qual: str) -> tuple[int, int] | None:
    """(start, end) of the definition ``qual`` in one version of ``rel``; None when it is not there once."""
    from verinoda import anchors

    if text is None or not qual:
        return None
    try:
        facts = anchors.compute_facts(rel, text.encode("utf-8"))
    except Exception:  # noqa: BLE001 - an unparsable version has no span
        return None
    if not anchors.usable(facts):
        return None
    hits = [s for q, s in facts.get("symbols", {}).items() if q.split("#")[0] == qual]
    return (hits[0]["start"], hits[0]["end"]) if len(hits) == 1 else None


def _governs_lines(d, v: dict, files: _Files, ev: str | None) -> list[dict]:
    """A changed line inside the governed symbol's span, either side, that no definition change counts (a
    docstring, comment or whitespace edit): ``decide check`` compares the symbol's whole text."""
    vf, vq = str(v.get("file") or ""), str(v.get("qual") or "")
    if vf not in files.text or vf in files.planned:
        return []
    for side, text, lines in (("new", files.text.get(vf), files.new_lines.get(vf)),
                              ("base", files.old_text.get(vf), files.old_lines.get(vf))):
        span = _span(text, vf, vq) if lines else None
        inside = sorted(ln for ln in lines or () if span and span[0] <= ln <= span[1])
        if inside:
            base = " (base)" if side == "base" else ""
            return [_reach("governs", v.get("id"), f"{vf}:{inside[0]}{base}",
                           f"a changed line inside {vf}::{vq} (lines {span[0]}-{span[1]}{base}) that no definition "
                           f"change counts (a docstring, comment or whitespace edit); {d.id} governs "
                           f"{v.get('symbol')}", "statically_verified", ev)]
    return []


def _governs(d, v: dict, changes, files: _Files, ev: str | None) -> list[dict]:
    out = []
    vf, vq = str(v.get("file") or ""), str(v.get("qual") or "")
    if not vf:
        return out
    for c in changes:
        if c.file != vf:
            continue
        if c.kind == "file_only":
            out.append(_reach("governs", v.get("id"), files.first_line(vf), f"{vf} changed and its definitions "
                              f"could not be compared; {d.id} governs {v.get('symbol')}", "strong_inference", ev))
            continue
        q = c.qual or ""
        if not vq or not (q == vq or q.startswith(vq + ".")):
            continue
        span = c.old_lines if c.kind == "removed" else c.lines
        at = f"{vf}:{span[0]}" if span else vf
        if c.kind == "removed":
            at += " (base)"
        out.append(_reach("governs", v.get("id"), at, f"{c.kind} change to {c.symbol}, which {d.id} governs "
                          f"({v.get('symbol')})", "statically_verified", ev))
    return out or _governs_lines(d, v, files, ev)


def _not_run(d, g: dict) -> str:
    """'' for a guard ``decide check`` runs, else why it does not (said in each of its reaches)."""
    if g.get("status") != "accepted":
        return f"; the guard is {g.get('status') or 'without a status'}, not enforced"
    return "" if d.enforced else "; the record is not enforced"


def _only_in(d, g: dict, files: _Files, repo: Path, ddir: Path, ev: str | None) -> list[dict]:
    from verinoda.guards import scope_files

    out = []
    allowed, exclude = _strs(g.get("allowed")), _strs(g.get("exclude"))
    note = _not_run(d, g)
    pats = _call_patterns(g, run_pattern=not note)
    rest = []
    for rel in files.names:
        hit = next((a for a in allowed if _glob(rel, a)), None)
        if hit:
            out.append(_reach("guard", g.get("id"), files.first_line(rel), f"{rel} is where {d.id}'s guard "
                              f"`{g.get('spec') or g.get('kind')}` allows the call (allowed={hit}){note}",
                              "statically_verified", ev))
        else:
            rest.append(rel)
    if not pats or not rest:
        return out
    try:   # the files decide check searches for this guard: code files, without tests, samples, reference trees
        rest, _ = scope_files(repo, {"scope": g.get("scope"), "allowed": allowed, "exclude": exclude}, rest, ddir)
    except Exception:  # noqa: BLE001 - a scope that cannot be worked out: nothing is searched
        return out
    for rel in rest:
        code = files.code_lines(rel, repo)
        for ln in sorted(files.new_lines.get(rel) or ()):
            if not 0 < ln <= len(code):
                continue
            m = next(((st, what) for rx, st, what in pats if rx.search(code[ln - 1])), None)
            if m:
                where = "a line of the targeted definition" if rel in files.planned else "a changed line"
                out.append(_reach("guard", g.get("id"), f"{rel}:{ln}", f"{where} {m[1]}, which {d.id}'s guard "
                                  f"allows only in {', '.join(allowed) or '?'}{note}", m[0], ev))
                break
    return out


# the globs of each edge guard kind: (key, holds a list)
_EDGE_SIDES = {"no_edge": (("from", False), ("to", False)), "layers": (("order", True),),
               "allow_edges": (("from", False), ("allowed", True)), "public": (("module", False), ("api", True))}


def _edge_guard(d, g: dict, files: _Files, ev: str | None, tags: dict[str, list[str]]) -> list[dict]:
    from verinoda.decisions import TAG_PREFIX

    kind = g.get("kind")
    out = []
    rels = "/".join(_strs(g.get("relations"))[:3]) or "edge"
    note = _not_run(d, g)
    sides = []
    for key, many in _EDGE_SIDES[kind]:
        for pat in (_strs(g.get(key)) if many else [g.get(key)] if isinstance(g.get(key), str) else []):
            globs = tags.get(pat[len(TAG_PREFIX):], []) if pat.startswith(TAG_PREFIX) else [pat]
            sides.append((key, pat, globs))
    what = f"no {rels} from {g.get('from')} to {g.get('to')}" if kind == "no_edge" else f"{kind}, {rels}"
    for rel in files.names:
        for key, pat, globs in sides:
            if pat and any(_glob(rel, x) for x in globs):
                out.append(_reach("guard", g.get("id"), files.first_line(rel),
                                  f"{rel} matches {key}={pat} of {d.id}'s guard `{g.get('spec') or kind}` "
                                  f"({what}){note}", "statically_verified", ev))
                break
    return out


def _name_rx(name: str) -> str:
    """A package name as ``decide check`` compares names: a run of ``-``, ``_`` and ``.`` is one separator."""
    return r"[-_.]+".join(re.escape(p) for p in re.split(r"[-_.]+", name) if p)


class _Manifests:
    """Whether ``decide check`` reads a changed manifest: not under test, sample, fixture, vendor or output
    folders, nor in reference trees, nor outside the root and its workspace packages."""

    def __init__(self, repo: Path):
        self.repo = repo
        self._not_read: set[str] | None = None

    def read(self, rel: str) -> bool:
        from verinoda.guards import _MANIFEST_NAMES, _NOT_PROJECT_BUILD, _WS_NEVER, declared_dependencies
        from verinoda.testcode import is_test_file

        if PurePosixPath(rel).name not in _MANIFEST_NAMES:
            return False
        if set(PurePosixPath(rel).parts[:-1]) & (_NOT_PROJECT_BUILD | _WS_NEVER) or is_test_file(rel):
            return False
        if self._not_read is None:
            try:
                deps = declared_dependencies(self.repo)
                self._not_read = set(deps.get("skipped") or []) | set(deps.get("unread") or [])
            except Exception:  # noqa: BLE001 - the folder rule above still applies
                self._not_read = set()
        return rel not in self._not_read


def _manifest_names(d, name: str, files: _Files, manifests: _Manifests, what: str, kind: str, entry,
                    ev: str | None) -> list[dict]:
    out = []
    short = name.rsplit(":", 1)[1] if ":" in name else ""   # group:artifact: pom.xml names the artifact alone
    body = "|".join(f"(?:{rx})" for rx in (_name_rx(name), _name_rx(short)) if rx)
    if not body:
        return out
    rx = re.compile(r"(?<![\w.-])(?:" + body + r")(?![\w-])", re.I)
    for rel in files.names:
        if not manifests.read(rel):
            continue
        hit = next(((side, ln) for side, ln, t in files.raw_changed(rel) if rx.search(t)), None)
        if hit:
            out.append(_reach(kind, entry, f"{rel}:{hit[1]}" + (" (base)" if hit[0] == "base" else ""),
                              f"a changed line of {rel} names {name}; {d.id} {what}", "strong_inference", ev))
    return out


def _base_front(files: _Files, rel: str) -> dict | None:
    """The front matter of a record file's base version, when the diff has one."""
    from verinoda.decisions import split_front

    old = files.old_text.get(rel)
    if old is None:
        return None
    front, _ = split_front(old.lstrip("﻿").replace("\r\n", "\n"))
    return front if isinstance(front, dict) and "verinoda-decision" in front else None


def _own_file(d, repo: Path) -> str | None:
    if d.path is None:
        return None
    try:
        return d.path.resolve().relative_to(repo).as_posix()
    except ValueError:
        return None


def _record_files(d, repo: Path, files: _Files) -> list[dict]:
    out = []
    own = _own_file(d, repo)
    source = d.source if isinstance(d.source, str) else None
    for rel, what in ((own, "the record itself"), (source, "the document the record was imported from")):
        if rel and rel in files.text:
            base = _base_front(files, rel) if rel == own else None
            was = str(base.get("status") or "") if base else ""
            why = f"{what} changed" + (f" (status {was} in the base, {d.status} now)" if was and was != d.status
                                       else "")
            out.append(_reach("record", None, files.first_line(rel), why, "statically_verified", None))
    return out


def _deleted_records(repo: Path, ddir: Path, files: _Files) -> list[dict]:
    """The records the change deletes, read from their base version."""
    try:
        drel = ddir.relative_to(repo).as_posix()
    except ValueError:
        return []
    out = []
    for rel in files.names:
        if files.text.get(rel) is not None or rel in files.planned or not rel.endswith(".md") or \
                PurePosixPath(rel).parent.as_posix() != drel:
            continue
        front = _base_front(files, rel)
        if front is None:
            continue
        out.append({"decision": str(front.get("id") or PurePosixPath(rel).stem),
                    "title": str(front.get("title") or ""), "status": "deleted", "enforced": False, "file": rel,
                    "reached_by": [_reach("record", None, f"{rel} (base)", f"the change deletes the record (status "
                                          f"{front.get('status') or '?'} in the base)", "statically_verified",
                                          None)]})
    return out


def _hits(d, repo: Path, ddir: Path, changes, files: _Files, manifests: _Manifests, at) -> list[dict]:
    hits = []
    for v in d.governs:
        if isinstance(v, dict):
            hits += _governs(d, v, changes, files, at("governs"))
    for g in d.guards:
        if not isinstance(g, dict):
            continue
        kind = g.get("kind")
        if kind == "only_in":
            hits += _only_in(d, g, files, repo, ddir, at("guards"))
        elif kind in _EDGE_SIDES:
            from verinoda.decisions import architecture_tags

            hits += _edge_guard(d, g, files, at("guards"), architecture_tags(repo)[0])
        elif kind == "dependency":
            for k in ("absent", "present"):
                if isinstance(g.get(k), str) and g[k].strip():
                    hits += _manifest_names(d, g[k].strip(), files, manifests, f"requires it {k} (guard "
                                            f"{g.get('id')}){_not_run(d, g)}", "guard", g.get("id"), at("guards"))
    for r in d.revisit_when:
        if not isinstance(r, dict) or not isinstance(r.get("value"), str) or not r["value"].strip():
            continue
        if r.get("kind") == "file_appears":
            for f in sorted(files.added):
                if _glob(f, r["value"]) or fnmatch.fnmatchcase(PurePosixPath(f).name, r["value"]):
                    hits.append(_reach("revisit_when", r.get("id"), f, f"{f} is added and {d.id} asks for a "
                                       f"review when file_appears={r['value']}", "statically_verified",
                                       at("revisit-when")))
        elif r.get("kind") == "dependency_added":
            hits += _manifest_names(d, r["value"].strip(), files, manifests, f"asks for a review when it is added "
                                    f"(revisit {r.get('id')})", "revisit_when", r.get("id"), at("revisit-when"))
    return hits


def reached(repo: Path, changes, diffs) -> dict:
    """The decision records a change reaches. ``changes``: the review's changed definitions; ``diffs``: its
    changed files (``rel``, ``old``, ``new``; empty for a planned change)."""
    from verinoda import decisions as dm

    repo = Path(repo).resolve()
    try:
        ddir, where = dm.decisions_dir_source(repo)
        recs = dm.load_all(repo)
    except Exception as exc:  # noqa: BLE001 - a broken setting is reported, the review goes on
        msg = f"{type(exc).__name__}: {exc}".replace(str(repo), "<repo>")
        return {"records": [], "read": 0, "error": msg[:300], "limits": list(LIMITS)}
    files = _Files(diffs, changes)
    manifests = _Manifests(repo)
    out, dormant, failed = [], 0, []
    for d in recs:
        rel = _own_file(d, repo) or (str(d.path) if d.path is not None else None)
        live = d.status in LIVE_STATUSES and not d.inactive
        try:
            hits = _record_files(d, repo, files)
            if live or d.problems:
                ev = _front_lines(d.path)

                def at(key: str, ev=ev, rel=rel) -> str | None:
                    return f"{rel}:{ev[key]}" if rel and key in ev else rel

                hits += _hits(d, repo, ddir, changes, files, manifests, at)
        except Exception as exc:  # noqa: BLE001 - one unreadable record must not stop the review
            failed.append(f"{d.id}: {type(exc).__name__}: {exc}"[:200])
            continue
        if not hits:
            if not live and not d.problems:
                dormant += 1
            continue
        seen, uniq = set(), []
        for h in hits:
            key = (h["kind"], h["entry"], h["at"])
            if key not in seen:
                seen.add(key)
                uniq.append(h)
        uniq.sort(key=lambda h: _RANK.get(h["status"], 2))
        rec = {"decision": d.id, "title": d.title, "status": d.status, "enforced": d.enforced, "file": rel,
               "reached_by": uniq[:MAX_REACH_PER_RECORD]}
        if len(uniq) > MAX_REACH_PER_RECORD:
            rec["reached_by_total"] = len(uniq)
        if isinstance(d.source, str) and d.source:
            rec["source"] = d.source
        if d.problems:
            rec["problems"] = d.problems[:3]
        if d.inactive:
            rec["not_enforced_because"] = d.inactive[:3]
        out.append(rec)
    out += _deleted_records(repo, ddir, files)
    out.sort(key=lambda r: (not r["enforced"], min((_RANK.get(h["status"], 2) for h in r["reached_by"]),
                                                   default=3), r["decision"]))
    res = {"records": out, "read": len(recs), "dir": {"path": ddir.relative_to(repo).as_posix()
                                                      if ddir.is_relative_to(repo) else str(ddir), "from": where},
           "limits": list(LIMITS)}
    if dormant:
        res["not_listed"] = f"{dormant} superseded, rejected or deprecated record(s) are not listed"
    if failed:
        res["error"] = "the front matter of some records could not be matched: " + "; ".join(failed[:3])
    return res


def render_lines(res: dict) -> list[str]:
    res = res or {}
    recs = res.get("records") or []
    out = []
    if res.get("error"):
        out += ["", f"Decisions: the records could not all be read ({res['error']})"]
    if not recs:
        return out
    out += ["", f"Decisions to read ({len(recs)} record(s) the change reaches; `verinoda decide check` checks them):"]
    for r in recs[:10]:
        why = ""
        if r.get("not_enforced_because"):
            why = f": {r['not_enforced_because'][0]}"
        elif r.get("problems"):
            why = f": {r['problems'][0]}"
        flag = "" if r["enforced"] else \
            f" [{r['status']}{', not enforced' if r['status'] == 'accepted' else ''}{why}]"
        out.append(f"  {r['decision']} {r['title']}{flag}  ({r['file']})")
        for h in r["reached_by"][:3]:
            out.append(f"      [{h['status']}] {h['why']}  at {h['at']}")
        if len(r["reached_by"]) > 3:
            out.append(f"      ... {r.get('reached_by_total', len(r['reached_by'])) - 3} more (--json)")
    if len(recs) > 10:
        out.append(f"  ... {len(recs) - 10} more (--json)")
    return out
