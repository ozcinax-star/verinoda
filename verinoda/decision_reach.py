"""Decision records a change reaches: the records to read before a diff is merged (docs/DESIGN.md D33, D35).

``verinoda review`` calls :func:`reached` with the changed definitions and the changed files. A record is
listed when the change reaches what its front matter names, each reach with the changed line (``at``), the
front matter line it matched (``evidence_at``) and a status:

* ``governs``: a changed, added or removed definition is the governed symbol or lies inside it
  (``statically_verified``: the symbol's file and qualified name are compared as text); a changed file of the
  governed symbol whose definitions could not be read (``strong_inference``);
* ``only_in``: a changed file is one the guard allows the call in (``statically_verified``, a glob match); a
  changed line's code (comments and strings blanked) holds the guard's dotted call or matches its pattern
  (``strong_inference``: text, names are not bound), or holds only the call's last name
  (``weak_inference``);
* ``no_edge``: a changed file matches the guard's ``from`` or ``to`` glob (``statically_verified``);
* ``dependency`` and ``revisit-when dependency_added``: a changed line of a manifest names the package
  (``strong_inference``);
* ``revisit-when file_appears``: an added file matches the glob (``statically_verified``);
* the record's own file, or the hand-written document it was imported from, is in the diff
  (``statically_verified``).

Nothing here decides whether the change keeps a decision: ``verinoda decide check`` does that; this only says
which records to read. Superseded, rejected and deprecated records are counted, not listed.
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
    "hand-written decision documents without a record (`verinoda decide import`) have no front matter to match",
]


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


def _call_patterns(g: dict) -> list[tuple[re.Pattern, str, str]]:
    """(regex, status, what) for the calls or pattern an only_in guard watches."""
    out = []
    if g.get("pattern"):
        try:
            out.append((re.compile(g["pattern"]), "strong_inference", f"matches pattern `{g['pattern']}`"))
        except re.error:
            pass
    for c in g.get("calls") or []:
        if not isinstance(c, str) or not c.strip():
            continue
        parts = c.strip().split(".")
        out.append((re.compile(r"(?<![\w$.])" + r"\s*\.\s*".join(map(re.escape, parts)) + r"\b"),
                    "strong_inference", f"calls `{c}`"))
        out.append((re.compile(r"(?<![\w$])" + re.escape(parts[-1]) + r"\s*\("), "weak_inference",
                    f"calls a `{parts[-1]}` (the last name of `{c}`; its owner is not bound)"))
    return out


class _Files:
    """The changed files: their changed lines on the new side, code text (comments and strings blanked),
    whether each was added."""

    def __init__(self, diffs, changes):
        from verinoda.review import _changed_lines

        self.new_lines: dict[str, set[int]] = {}
        self.old_lines: dict[str, set[int]] = {}
        self.text: dict[str, str | None] = {}
        self.old_text: dict[str, str | None] = {}
        self.added: set[str] = set()
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
    return out


def _only_in(d, g: dict, files: _Files, repo: Path, ev: str | None) -> list[dict]:
    from verinoda.testcode import is_test_file

    out = []
    allowed = [a for a in g.get("allowed") or [] if isinstance(a, str)]
    exclude = [x for x in g.get("exclude") or [] if isinstance(x, str)]
    pats = _call_patterns(g)
    for rel in files.names:
        hit = next((a for a in allowed if _glob(rel, a)), None)
        if hit:
            out.append(_reach("guard", g.get("id"), files.first_line(rel), f"{rel} is where {d.id}'s guard "
                              f"`{g.get('spec') or g.get('kind')}` allows the call (allowed={hit})",
                              "statically_verified", ev))
            continue
        if not pats or any(_glob(rel, x) for x in exclude):
            continue
        if g.get("scope", "product") != "all" and is_test_file(rel):
            continue
        code = files.code_lines(rel, repo)
        for ln in sorted(files.new_lines.get(rel) or ()):
            if not 0 < ln <= len(code):
                continue
            m = next(((st, what) for rx, st, what in pats if rx.search(code[ln - 1])), None)
            if m:
                out.append(_reach("guard", g.get("id"), f"{rel}:{ln}", f"a changed line {m[1]}, which {d.id}'s "
                                  f"guard allows only in {', '.join(allowed) or '?'}", m[0], ev))
                break
    return out


def _no_edge(d, g: dict, files: _Files, ev: str | None) -> list[dict]:
    out = []
    for rel in files.names:
        for side in ("from", "to"):
            pat = g.get(side)
            if isinstance(pat, str) and pat and _glob(rel, pat):
                out.append(_reach("guard", g.get("id"), files.first_line(rel),
                                  f"{rel} matches {side}={pat} of {d.id}'s guard `{g.get('spec') or 'no_edge'}` "
                                  f"(no {g.get('relations') and '/'.join(g['relations'][:3]) or 'edge'} from "
                                  f"{g.get('from')} to {g.get('to')})", "statically_verified", ev))
                break
    return out


def _manifest_names(d, name: str, files: _Files, what: str, kind: str, entry, ev: str | None) -> list[dict]:
    from verinoda.guards import _MANIFEST_NAMES

    out = []
    rx = re.compile(r"(?<![\w.-])" + re.escape(name) + r"(?![\w-])", re.I)
    for rel in files.names:
        if PurePosixPath(rel).name not in _MANIFEST_NAMES:
            continue
        hit = next(((side, ln) for side, ln, t in files.raw_changed(rel) if rx.search(t)), None)
        if hit:
            out.append(_reach(kind, entry, f"{rel}:{hit[1]}" + (" (base)" if hit[0] == "base" else ""),
                              f"a changed line of {rel} names {name}; {d.id} {what}", "strong_inference", ev))
    return out


def _record_files(d, repo: Path, files: _Files) -> list[dict]:
    out = []
    own = None
    if d.path is not None:
        try:
            own = d.path.resolve().relative_to(repo).as_posix()
        except ValueError:
            own = None
    for rel, what in ((own, "the record itself"), (d.source, "the document the record was imported from")):
        if rel and rel in files.text:
            out.append(_reach("record", None, files.first_line(rel), f"{what} changed", "statically_verified",
                              None))
    return out


def reached(repo: Path, changes, diffs) -> dict:
    """The decision records a change reaches. ``changes``: the review's changed definitions; ``diffs``: its
    changed files (``rel``, ``old``, ``new``; empty for a planned change)."""
    from verinoda import decisions as dm

    repo = Path(repo).resolve()
    try:
        ddir, where = dm.decisions_dir_source(repo)
        recs = dm.load_all(repo)
    except Exception as exc:  # noqa: BLE001 - a broken setting is reported, the review goes on
        return {"records": [], "read": 0, "error": f"{type(exc).__name__}: {exc}"[:300], "limits": list(LIMITS)}
    files = _Files(diffs, changes)
    out, dormant = [], 0
    for d in recs:
        if d.status not in LIVE_STATUSES and not d.problems:
            dormant += 1
            continue
        ev = _front_lines(d.path)
        rel = None
        if d.path is not None:
            try:
                rel = d.path.resolve().relative_to(repo).as_posix()
            except ValueError:
                rel = str(d.path)

        def at(key: str) -> str | None:
            return f"{rel}:{ev[key]}" if rel and key in ev else rel

        hits = _record_files(d, repo, files)
        for v in d.governs:
            if isinstance(v, dict):
                hits += _governs(d, v, changes, files, at("governs"))
        for g in d.guards:
            if not isinstance(g, dict):
                continue
            kind = g.get("kind")
            if kind == "only_in":
                hits += _only_in(d, g, files, repo, at("guards"))
            elif kind == "no_edge":
                hits += _no_edge(d, g, files, at("guards"))
            elif kind == "dependency":
                for k in ("absent", "present"):
                    if isinstance(g.get(k), str) and g[k].strip():
                        hits += _manifest_names(d, g[k].strip(), files, f"requires it {k} (guard {g.get('id')})",
                                                "guard", g.get("id"), at("guards"))
        for r in d.revisit_when:
            if not isinstance(r, dict) or not isinstance(r.get("value"), str):
                continue
            if r.get("kind") == "file_appears":
                for f in sorted(files.added):
                    if _glob(f, r["value"]) or fnmatch.fnmatchcase(PurePosixPath(f).name, r["value"]):
                        hits.append(_reach("revisit_when", r.get("id"), f, f"{f} is added and {d.id} asks for a "
                                           f"review when file_appears={r['value']}", "statically_verified",
                                           at("revisit-when")))
            elif r.get("kind") == "dependency_added":
                hits += _manifest_names(d, r["value"], files, f"asks for a review when it is added (revisit "
                                        f"{r.get('id')})", "revisit_when", r.get("id"), at("revisit-when"))
        if not hits:
            continue
        seen, uniq = set(), []
        for h in hits:
            key = (h["kind"], h["entry"], h["at"])
            if key not in seen:
                seen.add(key)
                uniq.append(h)
        uniq.sort(key=lambda h: {"statically_verified": 0, "strong_inference": 1}.get(h["status"], 2))
        rec = {"decision": d.id, "title": d.title, "status": d.status, "enforced": d.enforced, "file": rel,
               "reached_by": uniq[:MAX_REACH_PER_RECORD]}
        if len(uniq) > MAX_REACH_PER_RECORD:
            rec["reached_by_total"] = len(uniq)
        if d.source:
            rec["source"] = d.source
        if d.problems:
            rec["problems"] = d.problems[:3]
        out.append(rec)
    out.sort(key=lambda r: (not r["enforced"], min(({"statically_verified": 0, "strong_inference": 1}.get(
        h["status"], 2) for h in r["reached_by"]), default=3), r["decision"]))
    res = {"records": out, "read": len(recs), "dir": {"path": ddir.relative_to(repo).as_posix()
                                                      if ddir.is_relative_to(repo) else str(ddir), "from": where},
           "limits": list(LIMITS)}
    if dormant:
        res["not_listed"] = f"{dormant} superseded, rejected or deprecated record(s) are not listed"
    return res


def render_lines(res: dict) -> list[str]:
    recs = (res or {}).get("records") or []
    if not recs:
        return []
    out = ["", f"Decisions to read ({len(recs)} record(s) the change reaches; `verinoda decide check` checks them):"]
    for r in recs[:10]:
        flag = "" if r["enforced"] else f" [{r['status']}{', not enforced' if r['status'] == 'accepted' else ''}]"
        out.append(f"  {r['decision']} {r['title']}{flag}  ({r['file']})")
        for h in r["reached_by"][:3]:
            out.append(f"      [{h['status']}] {h['why']}  at {h['at']}")
        if len(r["reached_by"]) > 3:
            out.append(f"      ... {r.get('reached_by_total', len(r['reached_by'])) - 3} more (--json)")
    if len(recs) > 10:
        out.append(f"  ... {len(recs) - 10} more (--json)")
    return out
