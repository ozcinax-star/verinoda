"""A local trigram index for exact and regular-expression search (``verinoda search``).

The index lives in ``.verinoda/index/trigram.db`` (derived, disposable data, like the passage index). For every
text file of the project - git's tracked and untracked-not-ignored files, or a directory walk outside git; at most
:data:`MAX_FILE_BYTES` bytes, no NUL byte in its first 8 KiB - it keeps the set of byte trigrams of the file's
content with ASCII letters lowered, and for every trigram the sorted list of files holding it (the posting list).

A search turns the pattern into what any match must contain (:func:`required`): the literal runs of three or more
characters that every match holds, combined with AND (a sequence, a repeat of at least one) and OR (alternatives).
The files whose postings hold every trigram of a required literal are the candidates; the regular expression then
runs over each candidate's text, so the index only narrows which files are read, it never decides a match. A
pattern with no such literal (``\\w+``, ``.``) reads every file: still correct, and slower than a walk by the
freshness check.

Soundness: a literal is required only where every match must contain its exact bytes. Case-insensitive patterns
(``-i``, ``(?i)``) require only their ASCII runs without ``i``, ``k`` and ``s`` (Python's case folding matches
those three to non-ASCII letters: ``İ``, ``ı``, the Kelvin sign, the long s); a non-ASCII character, U+FFFD or a
surrogate breaks a run. Files are read as UTF-8 with replacement.

Freshness: every search compares each file's size and modification time with the index and re-indexes the files
that differ (and drops the ones gone), patching their postings, before it reads the index; when more than a fifth
of the files changed it rebuilds instead. A file whose modification time lies within :data:`RACY_NS` of the last
refresh is read again on the next one (git's "racily clean" rule: a rewrite in the same clock tick keeps the time).
A file that could not be read (locked, no permission) is stored without a time, so the next search tries it again.
The first search builds the whole index. Writers take the database's write lock before they compare, so two
searches at once do not repeat each other's work.

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
    import re._constants as _src  # type: ignore[import-not-found]
    import re._parser as _sre  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - older Pythons
    import sre_constants as _src  # type: ignore[no-redef]
    import sre_parse as _sre  # type: ignore[no-redef]

SCHEMA = 2
MAX_FILE_BYTES = 2_000_000
MAX_RESULTS = 200
MAX_LINE_CHARS = 300
MAX_PATTERN_CHARS = 2_000
DEFAULT_TIMEOUT = 60.0          # seconds a search may spend reading files
RACY_NS = 3_000_000_000         # a file this close to the last refresh is read again
REBUILD_SHARE = 0.2             # more changed files than this share: rebuild instead of patching
_FOLDS = set("iksIKS")          # ASCII letters that re.IGNORECASE also matches to non-ASCII letters
LIMITS = [
    "text files only: binary files (a NUL byte in the first 8 KiB) and files over 2 MB are not indexed or searched",
    "a pattern with no literal run of three characters every match must hold reads every file (no faster than a "
    "walk); case-insensitive patterns narrow less",
    "a file rewritten with the same size and modification time long after the last search is not re-read until one "
    "of them changes",
    "the time limit is checked between files: one slow expression on one file is not interrupted",
]


class SearchError(ValueError):
    """A search that cannot run (bad pattern or path, an index that cannot be opened)."""


def db_path(repo: Path) -> Path:
    from verinoda.paths import index_dir

    return index_dir(repo) / "trigram.db"


# -- the pattern side -----------------------------------------------------------------------------------------

def _breaks(ch: str, fold: bool) -> bool:
    o = ord(ch)
    if ch == "�" or 0xD800 <= o <= 0xDFFF:
        return True
    return fold and (o > 127 or ch in _FOLDS)


def _split(run: str, fold: bool) -> list[str]:
    """The pieces of a literal run that every match holds byte for byte (see the module docstring)."""
    out, cur = [], []
    for ch in run:
        if _breaks(ch, fold):
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

_TABLES = ("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);"
           "CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE NOT NULL,"
           " mtime_ns INTEGER, size INTEGER, tris BLOB);"
           "CREATE TABLE IF NOT EXISTS post (tri INTEGER PRIMARY KEY, ids BLOB NOT NULL) WITHOUT ROWID;")


def _open(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db), timeout=120, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        row = None
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
        if row is None or row[0] != str(SCHEMA):
            conn.execute("BEGIN IMMEDIATE")   # checked again under the write lock: another search may have done it
            try:
                for stmt in _TABLES.split(";"):
                    if stmt.strip():
                        conn.execute(stmt)
                row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
                if row is None or row[0] != str(SCHEMA):
                    for stmt in ("DROP TABLE IF EXISTS files", "DROP TABLE IF EXISTS post", "DELETE FROM meta"):
                        conn.execute(stmt)
                    for stmt in _TABLES.split(";"):
                        if stmt.strip():
                            conn.execute(stmt)
                    conn.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA),))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return conn
    except BaseException:
        conn.close()
        raise


def _connect(db: Path) -> sqlite3.Connection:
    """The index database; a file that is not one (corrupt, truncated) is derived data: it is deleted and made
    again. Raises :class:`SearchError` when it cannot be opened at all."""
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
        try:
            return _open(db)
        except sqlite3.DatabaseError as exc:
            if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc):
                raise
            for suffix in ("", "-wal", "-shm"):
                p = db.with_name(db.name + suffix)
                if p.exists():
                    p.unlink()
            return _open(db)
    except (sqlite3.Error, OSError) as exc:
        raise SearchError(f"the search index {db} cannot be opened: {exc}") from None


def _ids(blob: bytes | None) -> array:
    a = array("I")
    if blob:
        a.frombytes(blob)
    return a


def _storable(rel: str) -> bool:
    try:
        rel.encode("utf-8")
    except UnicodeEncodeError:   # a file name that is not UTF-8 (POSIX): SQLite cannot hold it
        return False
    return True


def _list_files(repo: Path) -> list[str]:
    """The project's files: git's tracked and untracked-not-ignored ones (a walk outside git), without Verinoda's
    own state and index."""
    from verinoda.paths import atlas_dir, index_dir

    own_abs = {atlas_dir(repo), index_dir(repo).resolve()}
    own_rel = [os.path.relpath(p, repo).replace("\\", "/") + "/" for p in own_abs
               if str(p).lower().startswith(str(repo).lower())]
    try:
        r = subprocess.run(["git", "ls-files", "-z", "-co", "--exclude-standard"], cwd=repo, capture_output=True,
                           timeout=120)
        if r.returncode == 0:
            out = []
            for p in r.stdout.decode("utf-8", "surrogateescape").split("\0"):
                if p and not p.startswith(".git/") and not any(p.startswith(o) for o in own_rel):
                    out.append(p)
            return sorted(set(out))
    except (OSError, subprocess.SubprocessError):
        pass
    out = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if not d.startswith(".") and (Path(root) / d).resolve() not in own_abs
                   and d not in ("node_modules", "__pycache__")]
        for f in files:
            out.append((Path(root) / f).relative_to(repo).as_posix())
    return sorted(out)


def _read(repo: Path, rel: str) -> tuple[bytes | None, str]:
    """``(data, why)``: the file's bytes, or None with ``"skip"`` (binary, too large, not a regular file: the
    rules) or ``"error"`` (it could not be read now: try again next time)."""
    p = repo / rel
    try:
        if p.is_symlink() or not p.is_file():
            return None, "skip"
        if p.stat().st_size > MAX_FILE_BYTES:
            return None, "skip"
        data = p.read_bytes()
    except OSError:
        return None, "error"
    return (None, "skip") if b"\0" in data[:8192] else (data, "")


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


def _grouped(pairs: list):
    """``(keys, starts, ends, fids)`` numpy arrays: for each trigram key the slice of ``fids`` holding it (file ids
    in ascending order within a key when ``pairs`` is in ascending file id order)."""
    import numpy as np

    pairs = [(a, f) for a, f in pairs if len(a)]
    if not pairs:
        z = np.zeros(0, dtype=np.uint32)
        return z, z, z, z
    tri = np.concatenate([a for a, _ in pairs])
    fids = np.concatenate([np.full(len(a), f, dtype=np.uint32) for a, f in pairs])
    order = np.argsort(tri, kind="stable")
    tri, fids = tri[order], fids[order]
    keys, starts = np.unique(tri, return_index=True)
    ends = np.append(starts[1:], len(tri))
    return keys, starts, ends, fids


def refresh(repo: Path, *, rebuild: bool = False) -> dict:
    """Bring the index in line with the files (re-index the changed ones, drop the gone ones). Returns counts.
    Raises :class:`SearchError` when the index cannot be opened."""
    import numpy as np

    repo = Path(repo).resolve()
    t0 = time.perf_counter()
    started = time.time_ns()
    listed = [rel for rel in _list_files(repo) if _storable(rel)]
    stats = _stats(repo, listed)            # before the lock: reading the disk does not hold up other searches
    conn = _connect(db_path(repo))
    try:
        conn.execute("BEGIN IMMEDIATE")      # compare and write under the write lock
        try:
            last = conn.execute("SELECT value FROM meta WHERE key = 'refreshed_ns'").fetchone()
            racy_from = int(last[0]) - RACY_NS if last else None
            known = {r[0]: (r[1], r[2], r[3]) for r in conn.execute("SELECT path, id, mtime_ns, size FROM files")}
            full = rebuild or not known
            changed = [(rel, mt, size) for rel, (mt, size) in sorted(stats.items())
                       if full or (k := known.get(rel)) is None or k[1] is None or k[1] != mt or k[2] != size
                       or (racy_from is not None and mt >= racy_from)]
            gone = [rel for rel in known if rel not in stats]
            if not full and known and len(changed) + len(gone) > REBUILD_SHARE * max(len(known), 1):
                full = True
                changed = [(rel, mt, size) for rel, (mt, size) in sorted(stats.items())]
            if full and known:
                conn.execute("DELETE FROM files")
                conn.execute("DELETE FROM post")
                known = {}
                gone = []
            res = _apply(conn, repo, known, changed, gone, full, np)
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('refreshed_ns', ?)", (str(started),))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        res.update(indexed=conn.execute("SELECT COUNT(*) FROM files").fetchone()[0],
                   seconds=round(time.perf_counter() - t0, 3))
        return res
    except sqlite3.Error as exc:
        raise SearchError(f"the search index could not be updated: {exc}") from None
    finally:
        conn.close()


def _apply(conn, repo: Path, known: dict, changed: list, gone: list, full: bool, np) -> dict:
    if not changed and not gone:
        return {"changed": 0, "removed": 0}
    empty = np.zeros(0, dtype=np.uint32)
    adds: list = []
    drops: list = []
    for rel in gone:
        fid = known[rel][0]
        old = np.frombuffer(conn.execute("SELECT tris FROM files WHERE id = ?", (fid,)).fetchone()[0] or b"",
                            dtype=np.uint32)
        drops.append((old, fid))
        conn.execute("DELETE FROM files WHERE id = ?", (fid,))
    skipped = unreadable = 0
    for rel, mt, size in changed:
        data, why = _read(repo, rel)
        skipped += why == "skip"
        unreadable += why == "error"
        new = trigram_array(data) if data is not None else empty
        k = known.get(rel)
        if k is None:
            fid, old = conn.execute("INSERT INTO files (path, mtime_ns, size, tris) VALUES (?,?,?,?)",
                                    (rel, mt, size, b"")).lastrowid, empty
        else:
            fid = k[0]
            old = np.frombuffer(conn.execute("SELECT tris FROM files WHERE id = ?", (fid,)).fetchone()[0] or b"",
                                dtype=np.uint32)
        if full:
            adds.append((new, fid))
        else:
            adds.append((np.setdiff1d(new, old, assume_unique=True), fid))
            drops.append((np.setdiff1d(old, new, assume_unique=True), fid))
        # an unreadable file keeps no time: the next refresh reads it again
        conn.execute("UPDATE files SET mtime_ns = ?, size = ?, tris = ? WHERE id = ?",
                     (None if why == "error" else mt, size, new.astype(np.uint32).tobytes(), fid))
    if full:
        keys, starts, ends, fids = _grouped(adds)
        conn.executemany("INSERT INTO post (tri, ids) VALUES (?, ?)",
                         ((int(k), fids[s:e].tobytes()) for k, s, e in zip(keys, starts, ends)))
    else:
        ka, sa, ea, fa = _grouped(adds)
        kd, sd, ed, fd = _grouped(drops)
        add = {int(k): fa[s:e].tolist() for k, s, e in zip(ka, sa, ea)}
        drop = {int(k): fd[s:e].tolist() for k, s, e in zip(kd, sd, ed)}
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
    return {"changed": len(changed), "removed": len(gone), "not_text": skipped, "unreadable": unreadable,
            "built": full}


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


def scope_paths(repo: Path, paths: list[str] | None, cwd: Path | None = None) -> list[str]:
    """Repository-relative prefixes of ``paths`` (each relative to ``cwd`` or absolute; ``.`` and the repository
    itself mean everything). Raises :class:`SearchError` for a path outside the repository or one that does not
    exist."""
    repo = Path(repo).resolve()
    base = Path(cwd).resolve() if cwd else Path.cwd().resolve()
    out = []
    for raw in paths or []:
        p = Path(raw)
        if not p.is_absolute():
            p = base / p if (base / p).exists() or not (repo / p).exists() else repo / p
        p = p.resolve()
        try:
            rel = p.relative_to(repo).as_posix()
        except ValueError:
            raise SearchError(f"{raw} is outside the repository {repo}") from None
        if not p.exists():
            raise SearchError(f"{raw} does not exist")
        if rel in ("", "."):
            return []
        out.append(rel)
    return out


def search(repo: Path, pattern: str, *, ignore_case: bool = False, fixed: bool = False,
           paths: list[str] | None = None, max_results: int = MAX_RESULTS, refresh_index: bool = True,
           timeout: float = DEFAULT_TIMEOUT, rebuild: bool = False, cwd: Path | None = None) -> dict:
    """Search the project's text files for ``pattern`` (a Python regular expression, or a fixed string). Raises
    :class:`SearchError` (a ValueError) on a bad pattern, path or argument, or an index that cannot be opened."""
    repo = Path(repo).resolve()
    if not pattern:
        raise SearchError("the pattern is empty")
    if len(pattern) > MAX_PATTERN_CHARS:
        raise SearchError(f"the pattern is over {MAX_PATTERN_CHARS} characters")
    if max_results < 1:
        raise SearchError("--max-results must be at least 1")
    if not timeout or timeout <= 0:
        raise SearchError("--timeout must be positive")
    src = re.escape(pattern) if fixed else pattern
    try:
        rx = re.compile(src, (re.IGNORECASE if ignore_case else 0) | re.MULTILINE)
        req = required(src, ignore_case)
    except re.error as exc:
        raise SearchError(f"not a valid regular expression: {exc}") from None
    scope = scope_paths(repo, paths, cwd)
    t0 = time.perf_counter()
    upd = refresh(repo, rebuild=rebuild) if (refresh_index or rebuild) else None
    t1 = time.perf_counter()
    conn = _connect(db_path(repo))
    try:
        conn.execute("BEGIN")    # one snapshot for the file list and the postings
        files = {r[0]: r[1] for r in conn.execute("SELECT id, path FROM files WHERE mtime_ns IS NOT NULL")}
        unreadable = [r[0] for r in conn.execute("SELECT path FROM files WHERE mtime_ns IS NULL")]
        cand_ids = set(files) if req is None else _eval(conn, req, {})
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        raise SearchError(f"the search index could not be read: {exc}") from None
    finally:
        conn.close()
    fold_case = os.name == "nt"   # Windows paths match whatever their case

    def in_scope(f: str) -> bool:
        if not scope:
            return True
        g = f.lower() if fold_case else f
        return any(g == s or g.startswith(s + "/") for s in ([s.lower() for s in scope] if fold_case else scope))

    cands = sorted(files[i] for i in cand_ids if i in files and in_scope(files[i]))
    # a file the index could not read is read now (it may hold a match)
    cands += sorted(f for f in unreadable if in_scope(f))
    t2 = time.perf_counter()
    matches: list[dict] = []
    files_matched = total = 0
    not_read: list[str] = []
    deadline = t2 + timeout
    for n, rel in enumerate(cands):
        if time.perf_counter() > deadline:
            not_read = cands[n:]
            break
        data, why = _read(repo, rel)
        if data is None:
            if why == "error":
                not_read.append(rel)
            continue
        text = data.decode("utf-8", "replace")
        hit = False
        for m in rx.finditer(text):
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
    res = {"pattern": pattern, "fixed": fixed, "ignore_case": ignore_case,
           "status": "incomplete" if not_read else "observed",
           "matches": matches, "total": total, "files_matched": files_matched, "truncated": total > len(matches),
           "files_indexed": len(files), "candidates": len(cands), "narrowed": req is not None,
           "timing_s": {"index": round(t1 - t0, 3), "lookup": round(t2 - t1, 3), "scan": round(t3 - t2, 3),
                        "total": round(t3 - t0, 3)},
           "limits": list(LIMITS)}
    if not_read:
        res["not_read"] = {"total": len(not_read), "files": not_read[:20],
                           "why": "the time limit ran out" if time.perf_counter() > deadline else
                                  "the file could not be read"}
    if upd and (upd.get("changed") or upd.get("removed")):
        res["index_update"] = upd
    return res


def skipped_files(repo: Path, scope: list[str] | None = None) -> list[str]:
    """The indexed files under ``scope`` (repository-relative prefixes; none: all) that the index holds no text
    for because the rules skip them (over :data:`MAX_FILE_BYTES`, a NUL byte in the first 8 KiB, not a regular
    file): a search never reads them, so a match in one is never found. Read from the index as last refreshed
    (an empty list when there is no index yet). Raises :class:`SearchError` when the index cannot be read."""
    repo = Path(repo).resolve()
    db = db_path(repo)
    if not db.is_file():
        return []
    fold = os.name == "nt"
    pre = [s.lower() if fold else s for s in scope or []]
    conn = _connect(db)
    try:
        # a text file of 3 bytes or more always has a trigram: an empty set at that size is a skipped file
        rows = conn.execute("SELECT path FROM files WHERE mtime_ns IS NOT NULL AND size >= 3 AND length(tris) = 0"
                            " ORDER BY path").fetchall()
    except sqlite3.Error as exc:
        raise SearchError(f"the search index could not be read: {exc}") from None
    finally:
        conn.close()
    out = []
    for (rel,) in rows:
        g = rel.lower() if fold else rel
        if not pre or any(g == s or g.startswith(s + "/") for s in pre):
            out.append(rel)
    return out


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
    nr = res.get("not_read")
    if nr:
        out.append(f"not read: {nr['total']} file(s) ({nr['why']}), e.g. {', '.join(nr['files'][:3])}: an incomplete "
                   "search, no proof of absence")
    return "\n".join(out)
