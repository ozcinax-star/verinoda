"""Code tours built from evidence (``verinoda tour``): a ``trace`` path written as a CodeTour file.

CodeTour (the VS Code extension) plays a ``.tour`` JSON file step by step: each step opens a file at a line and
shows a Markdown description. ``verinoda tour SOURCE TARGET`` writes one from the first path ``trace`` finds:

- a step at the source's definition, one at each hop's site (the call, the import, the reference: the line the
  graph edge was extracted from), and one at the target's definition; a hop without a location is left out and
  counted, and the trace's own note (a structural path, a callback) is kept in the tour's description;
- each step quotes the line it opens, and says what the hop is: the graph's relation and its confidence
  (``EXTRACTED`` edges are read from the code, ``INFERRED`` ones are guesses), so a step is a lead to read, not a
  verified fact;
- it refuses a file changed since the index (the graph's line numbers would be the old file's: run ``verinoda
  update`` first);
- it is pinned (CodeTour's ``ref``) to HEAD only when every file it opens is as HEAD has it; otherwise it has no
  ``ref`` and says why, since CodeTour would open the files at that commit. Each step keeps the text of its line
  (under ``verinoda``, which CodeTour ignores).

Code moves: ``verinoda tour --check FILE`` finds each step's line again by its text (the nearest occurrence to
where it was), says which steps moved and which lost their line, and ``--fix`` writes the moved lines back (and
re-pins ``ref`` to HEAD when the files are as HEAD has them, else drops it). A tour file Verinoda did not write, or
one changed since Verinoda wrote it, is not overwritten without ``--force``.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

SCHEMA = "https://aka.ms/codetour-schema"
MARK = "verinoda"
TOUR_DIRS = (".tours", ".vscode/tours", ".github/tours")


class TourError(ValueError):
    pass


def _lines(repo: Path, rel: str) -> list[str] | None:
    try:
        return (repo / rel).read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").split("\n")
    except OSError:
        return None


def _split(at: str | None) -> tuple[str, int] | None:
    rel, _, line = str(at or "").rpartition(":")
    return (rel, int(line)) if rel and line.isdigit() else None


def _slug(text: str) -> str:
    return (re.sub(r"-{2,}", "-", re.sub(r"[^\w.-]+", "-", text)).strip("-.") or "tour")[:80]


def _clean_vs_head(repo: Path, files: list[str]) -> bool:
    from verinoda.snapshot import git

    if not files:
        return True
    out = git(repo, "status", "--porcelain", "--", *sorted(set(files)))
    return out is not None and not out.strip()


def _body_hash(tour: dict) -> str:
    """A hash of the tour without its own record of it: tells a tour Verinoda wrote from one edited since."""
    t = {k: v for k, v in tour.items() if k != MARK}
    return hashlib.sha256(json.dumps(t, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _pin(repo: Path, tour: dict) -> None:
    """``ref`` = HEAD when every file the tour opens is as HEAD has it; else no ``ref`` and the reason."""
    from verinoda.snapshot import git

    files = [s["file"] for s in tour["steps"]]
    head = (git(repo, "rev-parse", "HEAD") or "").strip()
    tour.pop("ref", None)
    tour[MARK].pop("unpinned", None)
    if head and _clean_vs_head(repo, files):
        tour["ref"] = head
    else:
        tour[MARK]["unpinned"] = ("not pinned to a commit: a file it opens differs from HEAD (CodeTour would open "
                                  "the files at that commit)" if head else "not pinned: no commit")


def build(repo: Path, g, source: str, target: str, *, mode: str = "flow", title: str | None = None,
          stale=()) -> dict:
    """The tour of ``trace(source, target)``'s first path; TourError when trace finds none or a file it would open
    changed since the index."""
    from verinoda import retrieval

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
    no_site = 0
    for k, h in enumerate(path, 1):
        site = _split(h.get("at"))
        if site is None:
            no_site += 1
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
    stale_hit = sorted({rel for rel, *_ in stops} & set(stale or ()))
    if stale_hit:
        raise TourError(f"{', '.join(stale_hit)} changed since the index: its line numbers would be the old "
                        "file's; run `verinoda update`, then make the tour")
    steps = []
    for rel, n, step_title, desc in stops:
        if steps and steps[-1]["file"] == rel and steps[-1]["line"] == n:   # one line, one step
            steps[-1]["title"] += f" / {step_title}"
            continue
        lines = _lines(repo, rel) or []
        text = lines[n - 1] if 0 < n <= len(lines) else ""
        code = text.strip()
        quote = f"\n\n`{rel}:{n}`: `{code}`" if code and "`" not in code else f"\n\n`{rel}:{n}`"
        steps.append({"file": rel, "line": n, "title": step_title, "description": desc + quote,
                      MARK: {"text": code}})
    extra = []
    if res.get("note"):
        extra.append(str(res["note"]))
    if res.get("reachability") and res["reachability"] != "reachable":
        extra.append(f"Reachability: {res['reachability']}.")
    if no_site:
        extra.append(f"{no_site} hop(s) of the path have no location in the graph and have no step.")
    tour = {"$schema": SCHEMA, "title": title or f"{source} -> {target}",
            "description": (f"Built by `verinoda tour` from `trace {source} {target} --mode {mode}` (the first of "
                            f"{len(res['paths'])} path(s)). Steps are graph edges to read, not verified facts."
                            + (" " + " ".join(extra) if extra else "")),
            "steps": steps, MARK: {"source": source, "target": target, "mode": mode}}
    _pin(repo, tour)
    return tour


def default_path(repo: Path, tour: dict) -> Path:
    return Path(repo) / ".tours" / f"{_slug(tour['title'])}.tour"


def write(repo: Path, tour: dict, out: str | None = None, *, force: bool = False) -> tuple[Path, list[str]]:
    """Write ``tour`` (default ``.tours/<title>.tour``) and return where, with notes. TourError for a path outside
    the project or in ``.git``, and for an existing file Verinoda did not write or that was edited since (unless
    ``force``)."""
    repo = Path(repo).resolve()
    p = Path(out) if out else default_path(repo, tour)
    p = (p if p.is_absolute() else repo / p).resolve()
    try:
        rel = p.relative_to(repo).as_posix()
    except ValueError:
        raise TourError(f"{p} is outside the project: a tour's paths are the project's") from None
    if rel.startswith(".git/") or p.suffix != ".tour":
        raise TourError(f"{rel}: a tour is a .tour file outside .git")
    if p.exists() and not force:
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = None
        mine = isinstance(old, dict) and isinstance(old.get(MARK), dict) and \
            old[MARK].get("hash") == _body_hash(old)
        if not mine:
            raise TourError(f"{rel} exists and is not a tour verinoda wrote unchanged: not overwritten (--force, "
                            "or --out another file)")
    notes = []
    if not any(rel.startswith(d + "/") for d in TOUR_DIRS):
        notes.append(f"CodeTour finds tours in {', '.join(TOUR_DIRS)}: open {rel} with 'CodeTour: Open Tour File'")
    from verinoda.treestate import project_prefix

    if project_prefix(repo):
        notes.append(f"the project is the folder {project_prefix(repo)!r} of its repository: open that folder as "
                     "the VS Code workspace (the steps' paths are relative to it)")
    tour[MARK]["hash"] = _body_hash(tour)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(tour, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return p, notes


def check(repo: Path, path: str, *, fix: bool = False) -> dict:
    """Find each step's line again by its recorded text; ``fix`` writes the moved lines back and re-pins."""
    repo = Path(repo).resolve()
    p = Path(path)
    p = p if p.is_absolute() else repo / p
    try:
        tour = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TourError(f"{path} cannot be read as a tour: {exc}") from None
    steps = tour.get("steps") if isinstance(tour, dict) else None
    if not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
        raise TourError(f"{path} is not a tour (no list of steps)")
    rows = []
    changed = False
    for i, st in enumerate(steps, 1):
        rec = st.get(MARK) if isinstance(st.get(MARK), dict) else None
        want = str((rec or {}).get("text") or "").strip()
        rel, n = st.get("file"), st.get("line")
        if not isinstance(rel, str) or not isinstance(n, int) or isinstance(n, bool):
            rows.append({"step": i, "state": "not_checked", "why": "no file and line"})
            continue
        if rec is None:
            rows.append({"step": i, "file": rel, "line": n, "state": "not_checked",
                         "why": "the step has no recorded line text (not written by verinoda)"})
            continue
        lines = _lines(repo, rel)
        if lines is None:
            rows.append({"step": i, "file": rel, "line": n, "state": "gone", "why": f"{rel} cannot be read"})
            continue
        if not want:
            ok = 0 < n <= len(lines)
            rows.append({"step": i, "file": rel, "line": n, "state": "ok" if ok else "gone",
                         **({} if ok else {"why": f"{rel} has {len(lines)} lines"}),
                         "note": "a blank line: only its place is checked"})
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
        if isinstance(tour.get(MARK), dict):
            _pin(repo, tour)
            tour[MARK]["hash"] = _body_hash(tour)
        p.write_text(json.dumps(tour, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    bad = [r for r in rows if r["state"] in ("moved", "gone")]
    return {"tour": str(path), "steps": rows, "moved": sum(r["state"] == "moved" for r in rows),
            "gone": sum(r["state"] == "gone" for r in rows), "fixed": fix and changed,
            "ref": tour.get("ref"),
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
