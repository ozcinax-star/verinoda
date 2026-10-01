"""Named maps: a ``trace`` or ``map`` result saved under a name, read back with whether it is still current.

``verinoda map save NAME --trace SOURCE TARGET`` (or ``--view VIEW``, or neither for the default views)
writes ``.verinoda/maps/NAME.json``: the result as the command printed it with ``--json``, the
arguments that made it, the index snapshot and commit it was computed from, and the sha256 *the
snapshot recorded* for every file the result cites. A file is cited when a string of the result
names it - a path, ``path:LINE``, ``path:A-B`` or ``path::Symbol`` - and the snapshot lists it.

Read back (``verinoda map show NAME``, MCP ``map_view`` with ``view='saved'``), the cited files are
hashed again: ``current`` when every one still has the content the snapshot recorded, ``stale`` when
one changed or is gone (named, with the command that makes the map again). A stale map is returned
as what it was - ``as_of`` the snapshot and commit, never as the code now. A file changed in the
working tree when the map was saved is already stale: the result describes the indexed version.

``current`` says only that what the map cites is unchanged: a file it does not cite can change what
a new run would return (a new path through a new file, a new dependent); the read says so in ``limits``.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

FORMAT = "verinoda.named_map/1"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_LOC = re.compile(r"(?::\d+(?:-\d+)?)+$")      # path:LINE, path:A-B
SHOWN = 20                                     # changed files named in a read (the count is exact)
# what the saved result said about the index when it was made: kept beside it, not inside it
_INDEX_KEYS = ("stale_count", "stale_files", "index_freshness")
LIMITS = ["current means the files the map cites have the content its snapshot recorded; a file it does not "
          "cite can change what a new run returns (a new path, a new dependent): run it again to be sure",
          "the saved result is what the command returned then; its edges are extractions as before, not "
          "verification"]


def maps_dir(repo: Path) -> Path:
    from verinoda.paths import atlas_dir

    return atlas_dir(repo) / "maps"


def check_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise ValueError(f"map name {name!r}: use 1-64 letters, digits, '.', '_' or '-' (not starting with "
                         "'.', '_' or '-')")
    return name


def _latest(repo: Path) -> dict | None:
    """The index's latest snapshot: ``{"id", "commit", "dirty", "created_at", "files": {path: sha256}}``."""
    from verinoda.paths import db_path

    db = db_path(repo)
    if not db.is_file():
        return None
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=0.5)
    try:
        row = conn.execute("SELECT id, commit_sha, dirty, created_at FROM snapshots "
                           "ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
        if row is None:
            return None
        files = dict(conn.execute("SELECT path, sha256 FROM snapshot_files WHERE snapshot_id = ?", (row[0],)))
    finally:
        conn.close()
    return {"id": row[0], "commit": row[1], "dirty": bool(row[2]), "created_at": row[3], "files": files}


def cited_files(result, known) -> list[str]:
    """The files of ``known`` (the snapshot's paths) that a string or key anywhere in ``result`` names."""
    found: set[str] = set()

    def look(s: str) -> None:
        if len(s) > 600:
            return
        for c in (s, s.split("::", 1)[0], _LOC.sub("", s)):
            if c in known:
                found.add(c)
                return

    stack = [result]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            for k, x in v.items():
                look(str(k))
                stack.append(x)
        elif isinstance(v, (list, tuple)):
            stack.extend(v)
        elif isinstance(v, str):
            look(v)
    return sorted(found)


def _strip_index_notes(result: dict) -> dict:
    """The result without the index-freshness keys (at the top, and in each view of a map)."""
    out = {k: v for k, v in result.items() if k not in _INDEX_KEYS}
    for k, v in out.items():
        if isinstance(v, dict) and any(x in v for x in _INDEX_KEYS):
            out[k] = {x: y for x, y in v.items() if x not in _INDEX_KEYS}
    return out


def save(repo: Path, name: str, kind: str, args: dict, result: dict, *, stale=()) -> dict:
    """Write ``result`` (a trace or map result) as map ``name``; ``stale`` = the files changed since the
    index (:func:`verinoda.freshness.check`). Overwrites a map of the same name."""
    repo = Path(repo).resolve()
    name = check_name(name)
    snap = _latest(repo)
    if snap is None:
        raise ValueError("the project has no index snapshot: run `verinoda scan` first")
    cited = cited_files(result, snap["files"])
    stale_cited = sorted(set(cited) & set(stale))
    doc = {"format": FORMAT, "name": name, "kind": kind, "args": args,
           "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "snapshot": snap["id"], "commit": snap["commit"], "dirty": snap["dirty"],
           "files": {f: snap["files"][f] for f in cited},
           **({"stale_at_save": stale_cited} if stale_cited else {}),
           "result": _strip_index_notes(result)}
    d = maps_dir(repo)
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, d / f"{name}.json")
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return {"status": "saved", "name": name, "path": (d / f"{name}.json").relative_to(repo).as_posix(),
            "kind": kind, "snapshot": snap["id"], "commit": snap["commit"], "cited_files": len(cited),
            **({"stale_at_save": stale_cited,
                "note": f"{len(stale_cited)} cited file(s) changed since the index: the map describes the indexed "
                        "version and reads back stale; run `verinoda update` and save it again"}
               if stale_cited else {})}


def _load(repo: Path, name: str) -> dict | None:
    try:
        doc = json.loads((maps_dir(repo) / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("format") == FORMAT else None


def rerun_command(doc: dict) -> str:
    a = doc.get("args") or {}
    cmd = f"verinoda map save {doc.get('name')}"
    if doc.get("kind") == "trace":
        cmd += f" --trace {_q(a.get('source'))} {_q(a.get('target'))}"
        if a.get("mode") and a["mode"] != "flow":
            cmd += f" --mode {a['mode']}"
        return cmd
    if a.get("view"):
        cmd += f" --view {a['view']}"
    cmd += "".join(f" --target {_q(t)}" for t in a.get("target") or ())
    if a.get("base"):
        cmd += f" --base {_q(a['base'])}"
    if a.get("max_tokens") is not None:
        cmd += f" --max-tokens {a['max_tokens']}"
    return cmd


def _q(s) -> str:
    s = str(s or "")
    return s if s and re.fullmatch(r"[\w./:@-]+", s) else json.dumps(s)


def _check(repo: Path, doc: dict) -> tuple[list[str], list[str]]:
    """``(changed, gone)``: the cited files whose content differs from the saved hash, and those not there."""
    from verinoda.snapshot import hash_files

    saved = doc.get("files") or {}
    now = hash_files(repo, sorted(saved))
    gone = sorted(f for f in saved if f not in now)
    changed = sorted(f for f, sha in saved.items() if f in now and now[f] != sha)
    return changed, gone


def read(repo: Path, name: str) -> dict:
    """Map ``name`` with ``status`` current | stale (or not_found), a claim on the cited files, ``as_of``."""
    repo = Path(repo).resolve()
    try:
        name = check_name(name)
    except ValueError as exc:
        return {"status": "invalid_name", "name": name, "message": str(exc)}
    doc = _load(repo, name)
    if doc is None:
        have = [m["name"] for m in listing(repo, check=False)["maps"]]
        return {"status": "not_found", "name": name, "saved": have[:30],
                "next_step": f"`verinoda map save {name} --trace SOURCE TARGET` (or --view VIEW) saves one"}
    changed, gone = _check(repo, doc)
    moved = changed + gone
    n = len(doc.get("files") or {})
    snap = _latest(repo)
    as_of = {"snapshot": doc.get("snapshot"), "commit": doc.get("commit"), "saved_at": doc.get("saved_at"),
             **({"dirty": True} if doc.get("dirty") else {})}
    where = f"snapshot {doc.get('snapshot')}" + (f", commit {str(doc.get('commit'))[:12]}" if doc.get("commit") else "")
    if moved:
        text = (f"{len(moved)} of the {n} file(s) map {name!r} cites changed or are gone since it was saved at "
                f"{where}: it shows the code as it was then, not now")
    else:
        text = f"the {n} file(s) map {name!r} cites have the content they had at {where}"
    claim = {"kind": "map_freshness", "status": "primary_source_verified", "text": text,
             "evidence": [{"path": f, "sha256_saved": doc["files"][f][:12], "change": "removed" if f in gone
                           else "modified"} for f in moved[:SHOWN]]
             or [{"method": "sha256 of each cited file now vs the hash the snapshot recorded", "files": n}],
             "subjects": moved[:SHOWN]}
    out = {"status": "stale" if moved else "current", "name": name, "kind": doc.get("kind"), "args": doc.get("args"),
           "as_of": as_of, "cited_files": n,
           **({"changed_files": moved[:SHOWN], "changed_count": len(moved)} if moved else {}),
           "claims": [claim],
           **({"next_step": f"run `verinoda update`, then `{rerun_command(doc)}` to make it again"} if moved else {}),
           **({"index_snapshot_now": snap["id"]} if snap and snap["id"] != doc.get("snapshot") else {}),
           "limits": LIMITS, "result": doc.get("result")}
    return out


def listing(repo: Path, *, check: bool = True) -> dict:
    """Every saved map: name, kind, when, and (``check``) current or stale."""
    repo = Path(repo).resolve()
    d = maps_dir(repo)
    maps = []
    for p in sorted(d.glob("*.json")) if d.is_dir() else ():
        doc = _load(repo, p.stem)
        if doc is None or not NAME_RE.match(p.stem):
            continue
        row = {"name": p.stem, "kind": doc.get("kind"), "saved_at": doc.get("saved_at"),
               "commit": (doc.get("commit") or "")[:12] or None, "cited_files": len(doc.get("files") or {})}
        if check:
            changed, gone = _check(repo, doc)
            row["status"] = "stale" if changed or gone else "current"
            if changed or gone:
                row["changed_count"] = len(changed) + len(gone)
        maps.append(row)
    return {"status": "found" if maps else "none", "dir": d.relative_to(repo).as_posix(), "maps": maps}


def render(res: dict) -> str:
    """The header of a read for a person (the saved result itself is rendered by the command)."""
    st = res.get("status")
    if st == "not_found":
        lines = [f"no saved map {res['name']!r}"]
        if res.get("saved"):
            lines.append(" saved maps: " + ", ".join(res["saved"]))
        return "\n".join(lines + [f" next: {res['next_step']}"])
    if st == "invalid_name":
        return f"error: {res['message']}"
    a = res.get("as_of") or {}
    commit = f", commit {str(a['commit'])[:12]}" if a.get("commit") else ""
    head = f"map {res['name']} ({res['kind']}) saved {a.get('saved_at')} at snapshot {a.get('snapshot')}{commit}"
    lines = [head]
    if st == "stale":
        more = res["changed_count"] - len(res["changed_files"])
        lines.append(f" STALE: {res['changed_count']} cited file(s) changed since: "
                     + ", ".join(res["changed_files"]) + (f", ... (+{more})" if more > 0 else ""))
        lines.append(" what follows is the map as it was then, not the code now")
        lines.append(f" next: {res['next_step']}")
    else:
        lines.append(f" current: the {res['cited_files']} file(s) it cites are unchanged (a file it does not cite "
                     "may still change a new run)")
    return "\n".join(lines)


def render_list(res: dict) -> str:
    if not res["maps"]:
        return f"no saved maps in {res['dir']}; `verinoda map save NAME --trace SOURCE TARGET` saves one"
    lines = [f"{len(res['maps'])} saved map(s) in {res['dir']}:"]
    for m in res["maps"]:
        st = m.get("status", "")
        if m.get("changed_count"):
            st += f" ({m['changed_count']} cited file(s) changed)"
        lines.append(f"   {m['name']:<24} {m['kind']:<6} {m['saved_at']}  {m['commit'] or '-':<12}  {st}")
    return "\n".join(lines)
