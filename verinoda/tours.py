"""Code tours built from evidence (``verinoda tour``): a ``trace`` path written as a CodeTour file.

CodeTour (the VS Code extension) plays a ``.tour`` JSON file step by step: each step opens a file at a line and
shows a Markdown description. ``verinoda tour SOURCE TARGET`` writes one from the first path ``trace`` finds:

- a step at the source's definition, one at each hop's site (the call, the import, the reference: the line the
  graph edge was extracted from), and one at the target's definition;
- each step quotes the line it opens, and says what the hop is: the graph's relation and its confidence
  (``EXTRACTED`` edges are read from the code, ``INFERRED`` ones are guesses), so a step is a lead to read, not a
  verified fact;
- the tour is pinned to the commit it was made at (CodeTour's ``ref``), and each step keeps the text of its line
  (under ``verinoda``, which CodeTour ignores).

Code moves: ``verinoda tour --check FILE`` finds each step's line again by its text (the nearest occurrence to
where it was), says which steps moved and which lost their line, and ``--fix`` writes the moved lines back. A tour
file Verinoda did not write is never overwritten.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

SCHEMA = "https://aka.ms/codetour-schema"
MARK = "verinoda"


class TourError(ValueError):
    pass


def _line(repo: Path, rel: str, n: int) -> str | None:
    try:
        lines = (repo / rel).read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").split("\n")
    except OSError:
        return None
    return lines[n - 1] if 0 < n <= len(lines) else None


def _split(at: str | None) -> tuple[str, int] | None:
    rel, _, line = str(at or "").rpartition(":")
    return (rel, int(line)) if rel and line.isdigit() else None


def _slug(text: str) -> str:
    return (re.sub(r"-{2,}", "-", re.sub(r"[^\w.-]+", "-", text)).strip("-.") or "tour")[:80]


def build(repo: Path, g, source: str, target: str, *, mode: str = "flow", title: str | None = None,
          stale=()) -> dict:
    """The tour of ``trace(source, target)``'s first path; TourError when trace finds none."""
    from verinoda import retrieval
    from verinoda.snapshot import git

    repo = Path(repo).resolve()
    res = retrieval.trace(g, source, target, mode=mode, stale=stale)
    if res.get("status") != "found" or not res.get("paths"):
        raise TourError(f"no {mode} path from {source} to {target}: {res.get('status')}"
                        + (f" ({res['note']})" if res.get("note") else ""))
    path = res["paths"][0]
    stops: list[tuple[str, int, str, str]] = []   # (file, line, title, description)
    src = (res.get("resolved") or {}).get("source") or {}
    tgt = (res.get("resolved") or {}).get("target") or {}
    if _split(src.get("at")):
        rel, n = _split(src["at"])
        stops.append((rel, n, f"Start: {src.get('label')}", f"`{src.get('label')}` is defined here."))
    for k, h in enumerate(path, 1):
        site = _split(h.get("at"))
        if site is None:
            continue
        kind = h.get("kind") or "call"
        how = {"call": "calls", "construction": "constructs", "import": "imports", "reference": "refers to",
               "inheritance": "inherits from", "containment": "contains", "callback": "registers"}.get(kind, kind)
        stops.append((site[0], site[1], f"{k}. {h['from']} {how} {h['to']}",
                      f"`{h['from']}` {how} `{h['to']}` here (graph edge `{h.get('relation')}`, "
                      f"{h.get('confidence') or 'unknown'} confidence: read the line to confirm)."))
    if _split(tgt.get("at")):
        rel, n = _split(tgt["at"])
        stops.append((rel, n, f"End: {tgt.get('label')}", f"`{tgt.get('label')}` is defined here."))
    steps = []
    for rel, n, step_title, desc in stops:
        text = _line(repo, rel, n)
        quote = f"\n\n`{rel}:{n}`: `{text.strip()}`" if text and text.strip() and "`" not in text else \
            f"\n\n`{rel}:{n}`"
        steps.append({"file": rel, "line": n, "title": step_title, "description": desc + quote,
                      MARK: {"text": (text or "").strip()}})
    head = (git(repo, "rev-parse", "HEAD") or "").strip()
    tour = {"$schema": SCHEMA, "title": title or f"{source} -> {target}",
            "description": (f"Built by `verinoda tour` from `trace {source} {target} --mode {mode}` (the first of "
                            f"{len(res['paths'])} path(s)). Steps are graph edges to read, not verified facts."),
            "steps": steps, MARK: {"source": source, "target": target, "mode": mode}}
    if head:
        tour["ref"] = head
    return tour


def default_path(repo: Path, tour: dict) -> Path:
    return Path(repo) / ".tours" / f"{_slug(tour['title'])}.tour"


def write(repo: Path, tour: dict, out: str | None = None) -> Path:
    """Write ``tour`` (default ``.tours/<title>.tour``); TourError when the file exists and Verinoda did not write
    it."""
    repo = Path(repo).resolve()
    p = Path(out) if out else default_path(repo, tour)
    p = p if p.is_absolute() else repo / p
    if p.exists():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = None
        if not isinstance(old, dict) or MARK not in old:
            raise TourError(f"{p} exists and was not written by verinoda: not overwritten (pass --out)")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(tour, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return p


def check(repo: Path, path: str, *, fix: bool = False) -> dict:
    """Find each step's line again by its recorded text; ``fix`` writes the moved lines back."""
    repo = Path(repo).resolve()
    p = Path(path)
    p = p if p.is_absolute() else repo / p
    try:
        tour = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TourError(f"{path} cannot be read as a tour: {exc}") from None
    rows = []
    changed = False
    for i, st in enumerate(tour.get("steps") or [], 1):
        want = ((st.get(MARK) or {}).get("text") or "").strip()
        rel, n = st.get("file"), st.get("line")
        if not rel or not isinstance(n, int):
            rows.append({"step": i, "state": "not_checked", "why": "no file and line"})
            continue
        if not want:
            rows.append({"step": i, "file": rel, "line": n, "state": "not_checked",
                         "why": "the step has no recorded line text (not written by verinoda)"})
            continue
        try:
            lines = (repo / rel).read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").split("\n")
        except OSError:
            rows.append({"step": i, "file": rel, "line": n, "state": "gone", "why": f"{rel} cannot be read"})
            continue
        if 0 < n <= len(lines) and lines[n - 1].strip() == want:
            rows.append({"step": i, "file": rel, "line": n, "state": "ok"})
            continue
        hits = [k + 1 for k, ln in enumerate(lines) if ln.strip() == want]
        if not hits:
            rows.append({"step": i, "file": rel, "line": n, "state": "gone",
                         "why": f"no line of {rel} reads `{want[:80]}` any more"})
            continue
        new = min(hits, key=lambda k: (abs(k - n), k))
        rows.append({"step": i, "file": rel, "line": n, "state": "moved", "to": new,
                     **({"note": f"{len(hits)} lines read the same; the nearest was taken"} if len(hits) > 1 else {})})
        if fix:
            st["line"] = new
            changed = True
    if fix and changed:
        p.write_text(json.dumps(tour, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    bad = [r for r in rows if r["state"] in ("moved", "gone")]
    return {"tour": str(path), "steps": rows, "moved": sum(r["state"] == "moved" for r in rows),
            "gone": sum(r["state"] == "gone" for r in rows), "fixed": fix and changed,
            "exit": 1 if any(r["state"] == "gone" for r in rows) or (bad and not fix) else 0}


def render_check(res: dict) -> str:
    out = [f"tour {res['tour']}: {len(res['steps'])} step(s), {res['moved']} moved, {res['gone']} gone"
           + (" (moved lines written back)" if res.get("fixed") else "")]
    for r in res["steps"]:
        if r["state"] != "ok":
            where = f"{r.get('file')}:{r.get('line')}" if r.get("file") else ""
            out.append(f"  step {r['step']} {r['state']} {where}" + (f" -> {r['to']}" if r.get("to") else "")
                       + (f": {r.get('why') or r.get('note')}" if r.get("why") or r.get("note") else ""))
    return "\n".join(out)
