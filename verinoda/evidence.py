"""Evidence records: where a fact came from, at which version, with which hash.

Source priority (lower rank = stronger), from the research protocol:

    1 pinned source code of the analysed project
    2 the project's tests / reproducible runs / a static resolver's answer
    3 the project's git history and design documents
    4 dependency source at the version actually used
    5 official documentation / standards
    6 reference repositories / original papers
    7 secondary sources

``graph_edge``, ``user_feedback``, ``agent_report`` (a run the agent reported
to the debug ledger) and ``search_result``/``model_summary``
are recorded for traceability but never count as verification on their own
(see :data:`NON_VERIFYING`); a search result, model summary or user feedback
is not even support for an inference by itself (:data:`NOT_SUPPORT`).

Source lines are *anchored* (docs/DESIGN.md D25): ``meta.anchor`` pins them to
the enclosing symbol, module statement or Markdown section
(:mod:`verinoda.anchors`), so :func:`check_source` can tell code that only
moved (``moved``: still valid, exact new lines) from code that changed
(``changed``: a candidate location only). Evidence rows are immutable; the
claims layer appends relocations to ``evidence_locations``. Evidence without
an anchor keeps the exact hash comparison, and its search for a moved block
never relocates a block that occurs more than once (``ambiguous``).

Markdown/reStructuredText lines recorded as ``source_code`` are documents, not
code: they are recorded - and, for older rows, read - as ``design_doc``
(:func:`effective_type`).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from verinoda.store import Store

SOURCE_RANK: dict[str, int] = {
    "source_code": 1,
    "test_result": 2,
    "experiment": 2,
    "static_resolution": 2,
    "git_history": 3,
    "design_doc": 3,
    "dependency_source": 4,
    "official_doc": 5,
    "standard": 5,
    "reference_repo": 6,
    "paper": 6,
    "secondary": 7,
    "graph_edge": 8,
    "user_feedback": 9,
    # a command the agent ran itself and reported to the debug ledger (output hash stored);
    # Verinoda did not run it, so it never verifies anything (docs/DESIGN.md D34)
    "agent_report": 9,
    "search_result": 10,
    "model_summary": 10,
}

NON_VERIFYING = {"graph_edge", "user_feedback", "search_result", "model_summary", "secondary", "agent_report"}
# Pointers to evidence, not evidence: they cannot support even an inference on
# their own (see claims.check_status for strong_inference).
NOT_SUPPORT = {"user_feedback", "search_result", "model_summary"}
RELATIONS = ("supports", "refutes", "qualifies")
EXCERPT_MAX = 400
# Documents, not code: cited lines in these files are design_doc evidence.
DOC_SUFFIXES = (".md", ".markdown", ".rst", ".adoc", ".pdf", ".docx", ".xlsx", ".pptx")  # the last four: doctext.py
# Evidence whose cited lines can be re-read from a file.
FILE_TYPES = ("source_code", "design_doc", "dependency_source", "reference_repo")


def content_hash(text: str) -> str:
    norm = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))
    return "sha256:" + hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _file_lines(path: Path) -> list[str] | None:
    from verinoda import doctext

    if doctext.kind(Path(path).name) is not None:  # a PDF, Office document or image: its text view
        return doctext.text_lines(Path(path))
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def read_lines(path: Path, start: int, end: int) -> str | None:
    lines = _file_lines(path)
    if lines is None or start < 1 or start > len(lines) or end < start:
        return None
    return "\n".join(lines[start - 1 : min(end, len(lines))])


def _excerpt(text: str) -> str:
    return text if len(text) <= EXCERPT_MAX else text[: EXCERPT_MAX - 3] + "..."


def effective_type(ev: dict) -> str:
    """The evidence type rules should use: Markdown/ADR lines are documents even if recorded as code."""
    st = ev.get("source_type") or ""
    if st == "source_code" and str(ev.get("path") or "").lower().endswith(DOC_SUFFIXES):
        return "design_doc"
    return st


def rank(ev: dict) -> int:
    return SOURCE_RANK.get(effective_type(ev), 99)


@dataclass
class SourceCheck:
    ok: bool
    reason: str
    current_hash: str | None = None
    moved_to: tuple[int, int] | None = None
    status: str = "changed"  # same | moved | changed | gone | ambiguous
    candidate: tuple[int, int] | None = None  # where changed anchored lines probably are now (never OK)

    def as_dict(self) -> dict:
        """The stable four-key view (unchanged since v0.1); :meth:`details` adds status and candidate."""
        return {"ok": self.ok, "reason": self.reason, "current_hash": self.current_hash,
                "moved_to": list(self.moved_to) if self.moved_to else None}

    def details(self) -> dict:
        return {**self.as_dict(), "status": self.status,
                "candidate": list(self.candidate) if self.candidate else None}


def source_evidence(
    repo: Path, path: str, start: int, end: int | None = None, *,
    commit: str | None, source_type: str = "source_code", meta: dict | None = None,
    anchor: bool = True,
) -> dict | None:
    """Build a source-line evidence record, or None if the lines cannot be evidence.

    Refused (None): a reversed or empty range, a range past the end of the
    file, lines that are blank (an empty string hashes to a constant that
    would "re-check" forever), and paths that resolve outside ``repo`` -
    unless the caller names the checkout explicitly with ``meta={"root": ...}``
    (reference repositories), in which case the path must stay inside that
    root. With ``anchor`` (the default) the lines are anchored to their symbol,
    module statement or Markdown section when the language is supported.
    """
    end = end or start
    if start < 1 or end < start:
        return None
    root = Path((meta or {}).get("root") or repo).resolve()
    target = (root / path).resolve()
    try:
        rel = target.relative_to(root)
    except ValueError:
        return None
    if not target.is_file():
        return None
    if rel.parts and rel.parts[0] in (".verinoda", ".git"):
        return None  # Verinoda's own state and git internals are never evidence about the project
    lines = _file_lines(target)
    if lines is None or end > len(lines):
        return None
    text = "\n".join(lines[start - 1 : end])
    if not text.strip():
        return None
    path = rel.as_posix()
    meta = dict(meta or {})
    if source_type == "source_code" and path.lower().endswith(DOC_SUFFIXES):
        source_type = "design_doc"
        meta["reclassified_from"] = "source_code"
    if anchor and "anchor" not in meta:
        from verinoda import anchors

        a = anchors.anchor_for_file(target, path, start, end)
        if a is not None:
            meta["anchor"] = a
    return {
        "source_type": source_type,
        "locator": f"{path}:{start}" + (f"-{end}" if end != start else ""),
        "path": path,
        "line_start": start,
        "line_end": end,
        "commit_sha": commit,
        "content_hash": content_hash(text),
        "excerpt": _excerpt(text),
        "meta": meta,
    }


def _legacy_search(lines: list[str], ev: dict, n: int) -> list[int]:
    """Start lines of blocks with the recorded hash (first-line index, no full slide)."""
    want = ev.get("content_hash")
    excerpt = ev.get("excerpt") or ""
    excerpt_first = excerpt.split("\n", 1)[0].rstrip()
    if "\n" not in excerpt and excerpt.endswith("...") and len(excerpt) >= EXCERPT_MAX:
        excerpt_first = ""  # the first line itself was truncated: scan every start
    starts = [i + 1 for i, ln in enumerate(lines) if ln.rstrip() == excerpt_first] if excerpt_first else \
        list(range(1, len(lines) + 1))
    return [s for s in starts if s + n - 1 <= len(lines)
            and content_hash("\n".join(lines[s - 1 : s - 1 + n])) == want]


def check_source(repo: Path, ev: dict) -> SourceCheck:
    """Re-read the cited lines: are they still there (``same``), elsewhere (``moved``), or not?

    Anchored evidence is relocated through its symbol; a ``moved`` block counts
    as OK with its exact new lines. Unanchored evidence only passes an exact
    hash match at the cited lines; a unique identical block elsewhere is
    reported (not OK), an ambiguous one is never relocated.
    """
    if not ev.get("path") or not ev.get("line_start"):
        return SourceCheck(False, "not a file/line evidence", status="gone")
    meta = ev.get("meta") or {}
    root = Path(meta.get("root") or repo)
    p = root / ev["path"]
    if not p.is_file():
        return SourceCheck(False, f"file no longer exists: {ev['path']}", status="gone")
    start, end = int(ev["line_start"]), int(ev["line_end"] or ev["line_start"])
    n = end - start + 1
    lines = _file_lines(p)
    if lines is None:
        return SourceCheck(False, "file unreadable", status="gone")
    cur_text = "\n".join(lines[start - 1 : end]) if 0 < start <= len(lines) and end <= len(lines) else None
    cur = content_hash(cur_text) if cur_text is not None else None
    anchor = meta.get("anchor")
    if isinstance(anchor, dict):
        from verinoda import anchors

        rel = anchors.relocate_file(p, ev["path"], anchor, ev.get("content_hash"), start)
        if rel.status == "same":
            return SourceCheck(True, "cited lines unchanged", cur, status="same")
        if rel.status == "moved":
            return SourceCheck(True, f"cited lines moved to {rel.start}-{rel.end} ({_what(anchor)} unchanged)",
                               cur, (rel.start, rel.end), status="moved")
        if rel.status == "gone":
            return SourceCheck(False, f"cited code is gone: {rel.detail.get('why', '')}".rstrip(": "), cur,
                               status="gone")
        if rel.status == "changed":
            return SourceCheck(False, "cited lines changed", cur, None, status="changed",
                               candidate=(rel.start, rel.end) if rel.start else None)
        # "unanchored": no comparable facts (grammar missing, parse error, scheme change) -> exact compare
    if cur is not None and cur == ev.get("content_hash") and cur_text is not None and cur_text.strip():
        return SourceCheck(True, "cited lines unchanged", cur, status="same")
    hits = _legacy_search(lines, ev, n)
    if len(hits) == 1:
        return SourceCheck(False, f"cited block moved to lines {hits[0]}-{hits[0] + n - 1}", cur,
                           (hits[0], hits[0] + n - 1), status="moved")
    if len(hits) > 1:
        return SourceCheck(False, f"cited block occurs {len(hits)} times (lines "
                           + ", ".join(str(h) for h in hits[:5]) + "); not relocated: ambiguous", cur,
                           status="ambiguous")
    return SourceCheck(False, "cited lines changed", cur, status="changed")


def _what(anchor: dict) -> str:
    if "sym" in anchor:
        return f"symbol {anchor['sym']}"
    if "mod" in anchor:
        return "module statement"
    return "section"


def record_location(store: Store, ev: dict, chk: SourceCheck, snapshot_id: str | None = None) -> bool:
    """Append where anchored evidence is now (``evidence_locations``); the evidence row never changes.

    Nothing is written while a citation stays where it was recorded; a later
    return to the recorded lines is written once, after a relocation.
    """
    if not ev.get("id") or not ev.get("path"):
        return False
    start, end = (chk.moved_to if chk.moved_to else (ev.get("line_start"), ev.get("line_end")))
    if chk.status == "same" and store.latest_evidence_location(ev["id"]) is None:
        return False
    return store.add_evidence_location(ev["id"], snapshot_id=snapshot_id, line_start=start, line_end=end,
                                       status=chk.status if chk.status in ("same", "moved", "changed", "gone",
                                                                           "ambiguous") else "changed",
                                       detail={"reason": chk.reason[:200]})


def graph_edge_evidence(edge: dict, *, graph_path: str, commit: str | None) -> dict:
    loc = edge.get("source_location") or ""
    line = int(loc[1:]) if loc.startswith("L") and loc[1:].isdigit() else None
    return {
        "source_type": "graph_edge",
        "locator": f"{edge['source']} -[{edge.get('relation')}]-> {edge['target']}",
        "path": edge.get("source_file"),
        "line_start": line,
        "line_end": line,
        "commit_sha": commit,
        "content_hash": None,
        "excerpt": None,
        "meta": {
            "confidence": edge.get("confidence"),
            "confidence_score": edge.get("confidence_score"),
            "relation": edge.get("relation"),
            "graph_path": graph_path,
            "source": edge.get("source"),
            "target": edge.get("target"),
        },
    }


def url_evidence(url: str, text: str, *, source_type: str, version: str | None,
                 meta: dict | None = None) -> dict:
    return {
        "source_type": source_type,
        "locator": url,
        "url": url,
        "version": version,
        "content_hash": content_hash(text),
        "excerpt": _excerpt(text.strip()),
        "meta": meta or {},
    }


def is_verifying(ev: dict) -> bool:
    return effective_type(ev) not in NON_VERIFYING


def add(store: Store, ev: dict) -> str:
    if ev["source_type"] not in SOURCE_RANK:
        raise ValueError(f"unknown evidence source_type {ev['source_type']!r}")
    return store.add_evidence(ev)


def summarize(ev: dict, *, location: dict | None = None, grade: str | None = None) -> dict:
    """Small, model-friendly view of an evidence row.

    ``location`` (the latest ``evidence_locations`` row) adds ``now`` when the
    cited lines moved or changed; ``grade`` is the entailment grade for the
    claim it is shown with (:mod:`verinoda.entail`).
    """
    out = {
        "id": ev["id"], "type": ev["source_type"], "at": ev["locator"],
        "commit": (ev.get("commit_sha") or "")[:12] or None,
        "version": ev.get("version"),
        "hash": (ev.get("content_hash") or "")[:19] or None,
    }
    if effective_type(ev) != ev["source_type"]:
        out["type"] = effective_type(ev)
    if ev.get("relation"):
        out["relation"] = ev["relation"]
    if ev.get("grp"):
        out["group"] = ev["grp"]
    if grade:
        out["grade"] = grade
    if location and location.get("status") != "same" and ev.get("path"):
        a, b = location.get("line_start"), location.get("line_end")
        where = f"{ev['path']}:{a}" + (f"-{b}" if b and b != a else "") if a else ev["path"]
        out["now"] = f"{where} ({location['status']})"
    if ev.get("excerpt"):
        out["excerpt"] = ev["excerpt"][:200]
    return out
