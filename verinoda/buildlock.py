"""One index build at a time per project.

``scan`` and ``update`` (and everything that refreshes the index through them: analyze, verify,
claim add, feedback, decide check, ``ui --watch``, the MCP server) take this lock. Two builds of
one project used to collide: the second one's indexer failed on files the first was replacing
(``[WinError 2]``) and the user was told to run ``scan --force``, a full rebuild, for a conflict
that needed nothing but waiting.

A second caller either waits for the first (bounded: ``wait`` seconds) or, asked not to wait,
does nothing; either way it can say who holds the lock (:attr:`IndexBusy.holder`: process id,
what it runs, since when). The lock is an operating-system file lock on ``.verinoda/build.lock``
(``msvcrt`` on Windows, ``flock`` elsewhere), so a build that crashes releases it with its
process; a stale owner file never blocks anyone. The same thread may take it again (``update``
runs ``scan`` for a project without a snapshot).

``build_stats.json`` next to the index records how long the last graph build took, so a caller
can tell beforehand that a refresh would be slow (:func:`last_build_seconds`).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
from pathlib import Path

LOCK_NAME = "build.lock"
OWNER_NAME = "build.owner.json"
STATS_NAME = "build_stats.json"
POLL_SECONDS = 0.2
CLI_WAIT_SECONDS = 600.0     # scan / update from a terminal: wait up to 10 minutes for another build
DEFAULT_WAIT_SECONDS = 600.0

_local = threading.local()   # per thread: repo key -> [file object, depth]


class IndexBusy(Exception):
    """Another process (or thread) is building this project's index."""

    def __init__(self, repo: Path, holder: dict | None, waited: float):
        self.repo = Path(repo)
        self.holder = holder or {}
        self.waited = waited
        super().__init__(self.message())

    def message(self) -> str:
        h = self.holder
        who = []
        if h.get("purpose"):
            who.append(str(h["purpose"]))
        if h.get("pid"):
            who.append(f"pid {h['pid']}")
        if isinstance(h.get("started"), (int, float)):
            who.append(f"started {max(0, round(time.time() - h['started']))} s ago")
        what = f" ({', '.join(who)})" if who else ""
        waited = f"; waited {self.waited:.0f} s" if self.waited >= 1 else ""
        return f"another index build of this project is running{what}{waited}; nothing was done"

    def as_dict(self) -> dict:
        return {"busy": True, "holder": self.holder, "waited_s": round(self.waited, 1), "message": self.message()}


def _atlas(repo: Path) -> Path:
    from verinoda.paths import atlas_dir

    return atlas_dir(Path(repo))


def _key(repo: Path) -> str:
    try:
        return os.path.normcase(str(Path(repo).resolve()))
    except OSError:
        return os.path.normcase(str(repo))


def _held() -> dict:
    d = getattr(_local, "held", None)
    if d is None:
        d = _local.held = {}
    return d


def _try_lock(fh) -> bool:
    fh.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fh) -> None:
    try:
        fh.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        fh.close()


def holder(repo: Path) -> dict | None:
    """What the owner file says about the current build (None: no owner recorded)."""
    try:
        data = json.loads((_atlas(repo) / OWNER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def is_locked(repo: Path) -> bool:
    """Is an index build of ``repo`` running right now (in another process or thread)?"""
    if _key(repo) in _held():
        return False  # this thread's own build
    p = _atlas(repo) / LOCK_NAME
    if not p.exists():
        return False
    try:
        fh = open(p, "a+b")  # noqa: SIM115 - closed by _unlock
    except OSError:
        return False
    if _try_lock(fh):
        _unlock(fh)
        return False
    fh.close()
    return True


@contextlib.contextmanager
def build_lock(repo: Path, *, wait: float = DEFAULT_WAIT_SECONDS, purpose: str = "index build",
               on_wait=None):
    """Hold the project's build lock for the ``with`` block.

    ``wait``: seconds to wait for another build (0: do not wait). Raises :class:`IndexBusy` when the
    lock is still held after that. ``on_wait(holder)`` is called once when waiting starts (the CLI
    prints a line). Re-entrant in the thread that holds it.
    """
    key = _key(repo)
    held = _held()
    if key in held:
        held[key][1] += 1
        try:
            yield
        finally:
            held[key][1] -= 1
        return
    d = _atlas(repo)
    d.mkdir(parents=True, exist_ok=True)
    fh = open(d / LOCK_NAME, "a+b")  # noqa: SIM115 - closed by _unlock
    t0 = time.monotonic()
    told = False
    while not _try_lock(fh):
        waited = time.monotonic() - t0
        if waited >= wait:
            fh.close()
            raise IndexBusy(repo, holder(repo), waited)
        if not told and on_wait is not None:
            told = True
            try:
                on_wait(holder(repo))
            except Exception:  # noqa: BLE001 - a message callback never breaks the wait
                pass
        time.sleep(min(POLL_SECONDS, max(0.01, wait - waited)))
    owner = d / OWNER_NAME
    try:
        owner.write_text(json.dumps({"pid": os.getpid(), "purpose": purpose, "started": time.time(),
                                     "argv": [os.path.basename(a) if i == 0 else a
                                              for i, a in enumerate(sys.argv[:4])]}) + "\n", encoding="utf-8")
    except OSError:
        pass
    held[key] = [fh, 1]
    try:
        yield
    finally:
        del held[key]
        try:
            owner.unlink()
        except OSError:
            pass
        _unlock(fh)


def record_build(repo: Path, *, graph_seconds: float | None, files: int | None) -> None:
    """Remember how long the last graph build took (``build_stats.json`` beside the index)."""
    if graph_seconds is None:
        return
    from verinoda.paths import index_dir

    p = index_dir(Path(repo)) / STATS_NAME
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({"graph_seconds": round(float(graph_seconds), 3), "files": files,
                                   "at": time.time()}) + "\n", encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass


def last_build_seconds(repo: Path) -> float | None:
    """Seconds the last graph build of ``repo`` took (None: never recorded)."""
    from verinoda.paths import index_dir

    try:
        data = json.loads((index_dir(Path(repo)) / STATS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    v = data.get("graph_seconds") if isinstance(data, dict) else None
    return float(v) if isinstance(v, (int, float)) else None
