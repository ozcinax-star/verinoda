"""A local trigram index for exact and regular-expression search (``verinoda search``).

The index lives in ``.verinoda/index/trigram.db`` (derived, disposable data, like the passage index). For every
text file of the project - git's tracked and untracked-not-ignored files, or a directory walk outside git; at most
:data:`MAX_FILE_BYTES` bytes, no NUL byte in its first 8 KiB - it keeps the set of byte trigrams of the file's
content with ASCII letters lowered, and for every trigram the sorted list of files holding it (the posting list).

A search turns the pattern into what any match must contain (:func:`required`): the literal runs of three or more
characters that every match holds, combined with AND (a sequence, a repeat of at least one) and OR (alternatives).
The files whose postings hold every trigram of a required literal are the candidates; the regular expression then
runs over each candidate's text, so the index only narrows which files are read, it never decides a match. A
pattern with no such literal (``\\w+``, ``.``) reads every file: still correct, no faster than a walk.

Soundness: a literal is required only where every match must contain its exact bytes. Case-insensitive patterns
(``-i``, ``(?i)``) require only their ASCII runs without ``i``, ``k`` and ``s`` (Python's case folding matches
those three to non-ASCII letters: ``İ``, ``ı``, the Kelvin sign, the long s); a non-ASCII character or U+FFFD
breaks a run. Files are read as UTF-8 with replacement.

Freshness: every search compares each file's size and modification time with the index and re-indexes the files
that differ (and drops the ones gone), patching their postings, before it reads the index. The first search
builds the whole index.

Each match is observed in the file as it was read for this search (``path:line`` and the line's text), the same
as a grep: what the file says now, not a claim about behaviour.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import time
from array import array
from pathlib import Path

try:  # Python 3.11+
    import re._parser as _sre  # type: ignore[import-not-found]
    import re._constants as _src  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - older Pythons
    import sre_constants as _src  # type: ignore[no-redef]
    import sre_parse as _sre  # type: ignore[no-redef]

SCHEMA = 1
MAX_FILE_BYTES = 2_000_000
MAX_RESULTS = 200
MAX_LINE_CHARS = 300
MAX_PATTERN_CHARS = 2_000
_FOLDS = set("iksIKS")          # ASCII letters that re.IGNORECASE also matches to non-ASCII letters
LIMITS = [
    "text files only: binary files (a NUL byte in the first 8 KiB) and files over 2 MB are not indexed or searched",
    "a pattern with no literal run of three characters every match must hold reads every file (no faster than a "
    "walk)",
    "matches are found per file as read now; a match spanning files or a file being written is not seen",
]


def db_path(repo: Path) -> Path:
    from verinoda.paths import index_dir

    return index_dir(repo) / "trigram.db"


# -- the pattern side -----------------------------------------------------------------------------------------

def _split(run: str, fold: bool) -> list[str]:
    """The pieces of a literal run that every match holds byte for byte (see the module docstring)."""
    out, cur = [], []
    for ch in run:
        bad = ord(ch) > 127 or ch == "�" if not fold else (ord(ch) > 127 or ch in _FOLDS)
        if bad and fold:
            out.append("".join(cur))
            cur = []
        elif ch == "�":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [p for p in out if len(p.encode("utf-8")) >= 3]


def _and(xs: list) -> tuple | None:
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    return xs[0] if len(xs) == 1 else ("and", xs)


def _req(items, fold: bool, depth: int = 0) -> tuple | None:
    """The requirement tree of a parsed sequence: ``("lit", text)``, ``("and", [...])``, ``("or", [...])`` or None
    (no requirement)."""
    if depth > 50:
        return None
    parts: list = []
    run: list[str] = []

    def flush() -> None:
        if run:
            parts.extend(("lit", p) for p in _split("".join(run), fold))
            run.clear()

    for op, av in items:
        if op is _src.LITERAL:
            run.append(chr(av))
            continue
        flush()
        if op is _src.SUBPATTERN:
            _group, add, _del, p = av
            sub_fold = fold or bool(add & _src.SRE_FLAG_IGNORECASE)
            if _del & _src.SRE_FLAG_IGNORECASE:
                sub_fold = fold   # a scoped (?-i:...) inside an ignore-case pattern: keep the safe reading
            parts.append(_req(p, sub_fold, depth + 1))
        elif op in (_src.MAX_REPEAT, _src.MIN_REPEAT, getattr(_src, "POSSESSIVE_REPEAT", None)):
            lo, _hi, p = av
            if lo >= 1:
                parts.append(_req(p, fold, depth + 1))
        elif op is _src.BRANCH:
            alts = [_req(b, fold, depth + 1) for b in av[1]]
            if alts and all(a is not None for a in alts):
                parts.append(("or", alts))
        elif op is getattr(_src, "ATOMIC_GROUP", None):
            parts.append(_req(av, fold, depth + 1))
        # anything else (classes, any, anchors, backreferences, lookarounds) requires nothing here
    flush()
    return _and(parts)


def required(pattern: str, ignore_case: bool = False) -> tuple | None:
    """What every match of ``pattern`` must contain (None: nothing known). Raises ``re.error`` on a bad pattern."""
    flags = re.IGNORECASE if ignore_case else 0
    compiled = re.compile(pattern, flags)
    parsed = _sre.parse(pattern, flags)
    fold = bool(compiled.flags & re.IGNORECASE)
    return _req(list(parsed), fold)


def trigram_array(data: bytes):
    """The distinct byte trigrams of ``data`` with ASCII letters lowered, as a sorted numpy array of 24-bit
    integers."""
    import numpy as np

    if len(data) < 3:
        return np.zeros(0, dtype=np.uint32)
    d = np.frombuffer(data.lower(), dtype=np.uint8).astype(np.uint32)
    return np.unique((d[:-2] << 16) | (d[1:-1] << 8) | d[2:])


def trigrams(data: bytes) -> set[int]:
    """The byte trigrams of ``data`` with ASCII letters lowered, as 24-bit integers."""
    return set(trigram_array(data).tolist())


# -- the store ------------------------------------------------------------------------------------------------

def _connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db), timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);"
        "CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, mtime_ns INTEGER,"
        " size INTEGER, tris BLOB);"
        "CREATE TABLE IF NOT EXISTS post (tri INTEGER PRIMARY KEY, ids BLOB NOT NULL) WITHOUT ROWID;")
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    if row is None or row[0] != str(SCHEMA):
        conn.executescript("BEGIN; DELETE FROM files; DELETE FROM post;"
                           f"INSERT OR REPLACE INTO meta VALUES ('schema', '{SCHEMA}'); COMMIT;")
    return conn


def _ids(blob: bytes | None) -> array:
    a = array("I")
    if blob:
        a.frombytes(blob)
    return a


def _list_files(repo: Path) -> list[str]:
    """The project's files: git's tracked and untracked-not-ignored ones (a walk outside git), without Verinoda's
    own state."""
    from verinoda.paths import atlas_dir, index_dir

    own = {atlas_dir(repo), index_dir(repo).resolve()}
    try:
        r = subprocess.run(["git", "ls-files", "-z", "-co", "--exclude-standard"], cwd=repo, capture_output=True,
                           timeout=120)
        if r.returncode == 0:
            out = []
            for p in r.stdout.decode("utf-8", "surrogateescape").split("\0"):
                if p and not p.startswith(".verinoda/") and not p.startswith(".git/"):
                    out.append(p)
            return sorted(set(out))
    except (OSError, subprocess.SubprocessError):
        pass
    out = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if not d.startswith(".") and (Path(root) / d).resolve() not in own
                   and d not in ("node_modules", "__pycache__")]
        for f in files:
            out.append((Path(root) / f).relative_to(repo).as_posix())
    return sorted(out)


def _read(repo: Path, rel: str) -> bytes | None:
    p = repo / rel
    try:
        if p.is_symlink() or not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
            return None
        data = p.read_bytes()
    except OSError:
        return None
    return None if b"\0" in data[:8192] else data


def _stats(repo: Path, listed: list[str]) -> dict[str, tuple[int, int]]:
    """``{path: (mtime_ns, size)}`` of the listed regular files, read a directory at a time (on Windows the
    directory listing carries both, so no file is opened)."""
    from concurrent.futures import ThreadPoolExecutor

    wanted = set(listed)
    dirs = sorted({rel.rpartition("/")[0] for rel in listed})
    root = str(repo)

    def scan(d: str) -> dict:
        out = {}
        try:
            with os.scandir(os.path.join(root, d) if d else root) as it:
                for e in it:
                    rel = f"{d}/{e.name}" if d else e.name
                    if rel in wanted and e.is_file(follow_symlinks=False):
                        s = e.stat(follow_symlinks=False)
                        out[rel] = (s.st_mtime_ns, s.st_size)
        except OSError:
            pass
        return out

    res: dict[str, tuple[int, int]] = {}
    with ThreadPoolExecutor(8) as ex:
        for part in ex.map(scan, dirs):
            res.update(part)
    return res


def refresh(repo: Path, *, rebuild: bool = False) -> dict:
    """Bring the index in line with the files (re-index the changed ones, drop the gone ones). Returns counts."""
    import numpy as np

    repo = Path(repo).resolve()
    t0 = time.perf_counter()
    conn = _connect(db_path(repo))
    try:
        if rebuild:
            conn.executescript("BEGIN; DELETE FROM files; DELETE FROM post; COMMIT;")
        known = {r[0]: (r[1], r[2], r[3]) for r in conn.execute("SELECT path, id, mtime_ns, size FROM files")}
        stats = _stats(repo, _list_files(repo))
        changed = [(rel, mt, size) for rel, (mt, size) in sorted(stats.items())
                   if (k := known.get(rel)) is None or k[1] != mt or k[2] != size]
        gone = [rel for rel in known if rel not in stats]
        if not changed and not gone:
            return {"indexed": len(known), "changed": 0, "removed": 0, "seconds": round(time.perf_counter() - t0, 3)}
        full = not known
        empty = np.zeros(0, dtype=np.uint32)
        conn.execute("BEGIN IMMEDIATE")
        try:
            adds: list = []     # (trigrams, file id) arrays to add to the postings
            drops: list = []
            for rel in gone:
                fid = known[rel][0]
                old = np.frombuffer(conn.execute("SELECT tris FROM files WHERE id = ?", (fid,)).fetchone()[0] or b"",
                                    dtype=np.uint32)
                drops.append((old, fid))
                conn.execute("DELETE FROM files WHERE id = ?", (fid,))
            skipped = 0
            for rel, mt, size in changed:
                data = _read(repo, rel)
                if data is None:
                    skipped += 1
                new = trigram_array(data) if data is not None else empty
                k = known.get(rel)
                if k is None:
                    fid, old = conn.execute("INSERT INTO files (path, mtime_ns, size, tris) VALUES (?,?,?,?)",
                                            (rel, mt, size, b"")).lastrowid, empty
                else:
                    fid = k[0]
                    old = np.frombuffer(conn.execute("SELECT tris FROM files WHERE id = ?", (fid,)).fetchone()[0]
                                        or b"", dtype=np.uint32)
                adds.append((np.setdiff1d(new, old, assume_unique=True), fid))
                drops.append((np.setdiff1d(old, new, assume_unique=True), fid))
                conn.execute("UPDATE files SET mtime_ns = ?, size = ?, tris = ? WHERE id = ?",
                             (mt, size, new.astype(np.uint32).tobytes(), fid))

            def grouped(pairs: list) -> dict[int, list[int]]:
                pairs = [(a, f) for a, f in pairs if len(a)]
                if not pairs:
                    return {}
                tri = np.concatenate([a for a, _ in pairs])
                fids = np.concatenate([np.full(len(a), f, dtype=np.uint32) for a, f in pairs])
                order = np.argsort(tri, kind="stable")
                tri, fids = tri[order], fids[order]
                keys, starts = np.unique(tri, return_index=True)
                bounds = list(starts[1:]) + [len(tri)]
                return {int(k): fids[s:e].tolist() for k, s, e in zip(keys.tolist(), starts.tolist(), bounds)}

            add, drop = grouped(adds), grouped(drops)
            if full:
                conn.executemany("INSERT INTO post (tri, ids) VALUES (?, ?)",
                                 ((t, array("I", sorted(v)).tobytes()) for t, v in add.items()))
            else:
                for t in set(add) | set(drop):
                    row = conn.execute("SELECT ids FROM post WHERE tri = ?", (t,)).fetchone()
                    s = set(_ids(row[0] if row else None))
                    s.difference_update(drop.get(t, ()))
                    s.update(add.get(t, ()))
                    if s:
                        conn.execute("INSERT OR REPLACE INTO post (tri, ids) VALUES (?, ?)",
                                     (t, array("I", sorted(s)).tobytes()))
                    elif row:
                        conn.execute("DELETE FROM post WHERE tri = ?", (t,))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        return {"indexed": n, "changed": len(changed), "removed": len(gone), "not_text": skipped,
                "built": full, "seconds": round(time.perf_counter() - t0, 3)}
    finally:
        conn.close()


def _eval(conn: sqlite3.Connection, node: tuple, cache: dict) -> set[int]:
    kind = node[0]
    if kind == "lit":
        out: set[int] | None = None
        d = node[1].encode("utf-8").lower()
        for t in sorted({int.from_bytes(d[i:i + 3], "big") for i in range(len(d) - 2)}):
            if t not in cache:
                row = conn.execute("SELECT ids FROM post WHERE tri = ?", (t,)).fetchone()
                cache[t] = set(_ids(row[0] if row else None))
            out = set(cache[t]) if out is None else out & cache[t]
            if not out:
                return set()
        return out or set()
    if kind == "and":
        out = None
        for x in node[1]:
            s = _eval(conn, x, cache)
            out = s if out is None else out & s
            if not out:
                return set()
        return out or set()
    out = set()
    for x in node[1]:
        out |= _eval(conn, x, cache)
    return out


def search(repo: Path, pattern: str, *, ignore_case: bool = False, fixed: bool = False,
           paths: list[str] | None = None, max_results: int = MAX_RESULTS, refresh_index: bool = True) -> dict:
    """Search the project's text files for ``pattern`` (a Python regular expression, or a fixed string). Raises
    ValueError on a bad pattern or argument."""
    repo = Path(repo).resolve()
    if not pattern:
        raise ValueError("the pattern is empty")
    if len(pattern) > MAX_PATTERN_CHARS:
        raise ValueError(f"the pattern is over {MAX_PATTERN_CHARS} characters")
    if max_results < 1:
        raise ValueError("--max-results must be at least 1")
    src = re.escape(pattern) if fixed else pattern
    try:
        rx = re.compile(src, (re.IGNORECASE if ignore_case else 0) | re.MULTILINE)
        req = required(src, ignore_case)
    except re.error as exc:
        raise ValueError(f"not a valid regular expression: {exc}") from None
    t0 = time.perf_counter()
    upd = refresh(repo) if refresh_index else None
    t1 = time.perf_counter()
    conn = _connect(db_path(repo))
    try:
        files = {r[0]: r[1] for r in conn.execute("SELECT id, path FROM files")}
        cand_ids = set(files) if req is None else _eval(conn, req, {})
    finally:
        conn.close()
    scope = [p.replace("\\", "/").removeprefix("./").rstrip("/") for p in paths or [] if p not in (".", "./")]
    cands = sorted(files[i] for i in cand_ids
                   if not scope or any(files[i] == s or files[i].startswith(s + "/") for s in scope))
    t2 = time.perf_counter()
    matches: list[dict] = []
    files_matched = 0
    total = 0
    for rel in cands:
        data = _read(repo, rel)
        if data is None:
            continue
        text = data.decode("utf-8", "replace")
        hit = False
        for m in rx.finditer(text):
            if m.start() == m.end() and hit:
                continue
            hit = True
            total += 1
            if len(matches) < max_results:
                line = text.count("\n", 0, m.start()) + 1
                ls = text.rfind("\n", 0, m.start()) + 1
                le = text.find("\n", m.start())
                body = text[ls:le if le >= 0 else len(text)].rstrip("\r")
                matches.append({"at": f"{rel}:{line}", "text": body[:MAX_LINE_CHARS]})
        files_matched += hit
    t3 = time.perf_counter()
    res = {"pattern": pattern, "fixed": fixed, "ignore_case": ignore_case, "status": "observed",
           "matches": matches, "total": total, "files_matched": files_matched, "truncated": total > len(matches),
           "files_indexed": len(files), "candidates": len(cands), "narrowed": req is not None,
           "timing_s": {"index": round(t1 - t0, 3), "lookup": round(t2 - t1, 3), "scan": round(t3 - t2, 3),
                        "total": round(t3 - t0, 3)},
           "limits": list(LIMITS)}
    if upd and (upd.get("changed") or upd.get("removed")):
        res["index_update"] = upd
    return res


def render(res: dict) -> str:
    out = [f"{m['at']}: {m['text']}" for m in res["matches"]]
    tail = (f"{res['total']} match(es) in {res['files_matched']} file(s); read {res['candidates']} of "
            f"{res['files_indexed']} indexed file(s)" + ("" if res["narrowed"] else " (no literal to narrow by)")
            + f" in {res['timing_s']['total']} s")
    if res["truncated"]:
        tail += f"; first {len(res['matches'])} shown (--max-results)"
    upd = res.get("index_update")
    if upd:
        tail += f"; index: {upd['changed']} file(s) re-indexed, {upd['removed']} removed"
    out.append(tail)
    return "\n".join(out)
