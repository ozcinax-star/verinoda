"""Watching the project's files: operating-system change events wake the update instead of polling.

:class:`Watcher` runs ``verinoda update`` when the project's files change (``ui --watch``, ``mcp serve
--watch``). An event only wakes it: the tree's signature (each listed file's size and modification
time, ``snapshot.list_files``) still decides whether anything changed, so an event for an ignored file,
a save that leaves the bytes as they were, or a lost event never produces a wrong update, only a look.

Event sources, no dependency needed (``ctypes`` over the system library):

- **Windows**: ``ReadDirectoryChangesW`` on the project root, recursive (one handle for the tree).
- **Linux**: ``inotify``, one watch per folder (new folders are added as they appear); the per-user
  watch limit (``fs.inotify.max_user_watches``) can stop it part way, which is said and polled around.
- elsewhere ``watchdog`` when it is installed (macOS FSEvents), else the old polling.

A full buffer (``ReadDirectoryChangesW`` returning nothing, ``IN_Q_OVERFLOW``) means "something
changed": the signature is compared. With events the tree is still looked at every ``rescan``
seconds, for changes no event reports (a network share, a folder added past the watch limit).
"""

from __future__ import annotations

import os
import queue
import struct
import sys
import threading
import time
from pathlib import Path

OVERFLOW = "*"          # an event source lost events: compare the signature
_IGNORED_PARTS = {
    ".git", ".verinoda", "graphify-out", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea", ".vscode",
}
BUF_SIZE = 64 * 1024   # ReadDirectoryChangesW over a network share fails above 64 KB


def relevant(rel: str) -> bool:
    """An event for ``rel`` (path relative to the root, either separator) can matter to the index:
    not under Verinoda's own folder, git's, a virtual environment, caches or editor settings."""
    if rel == OVERFLOW:
        return True
    parts = rel.replace("\\", "/").split("/")
    return not any(p in _IGNORED_PARTS or p.endswith(".egg-info") for p in parts)


# -- event parsing (pure, tested on every platform) ------------------------------------------------

def parse_win(buf: bytes, size: int) -> list[str]:
    """Relative paths in a ``FILE_NOTIFY_INFORMATION`` chain of ``size`` bytes; ``[OVERFLOW]`` for 0
    (the system's buffer overflowed and the changes are unknown)."""
    if size <= 0:
        return [OVERFLOW]
    out, off = [], 0
    while off + 12 <= size:
        nxt, _action, nlen = struct.unpack_from("<III", buf, off)
        if off + 12 + nlen > size:
            out.append(OVERFLOW)  # a cut record: say something changed rather than guess
            break
        out.append(bytes(buf[off + 12: off + 12 + nlen]).decode("utf-16-le", errors="replace"))
        if nxt == 0:
            break
        off += nxt
    return out


IN_MODIFY, IN_ATTRIB, IN_CLOSE_WRITE = 0x2, 0x4, 0x8
IN_MOVED_FROM, IN_MOVED_TO, IN_CREATE, IN_DELETE = 0x40, 0x80, 0x100, 0x200
IN_DELETE_SELF, IN_MOVE_SELF = 0x400, 0x800
IN_Q_OVERFLOW, IN_IGNORED, IN_ONLYDIR, IN_ISDIR = 0x4000, 0x8000, 0x01000000, 0x40000000
IN_MASK = (IN_MODIFY | IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE
           | IN_DELETE_SELF | IN_MOVE_SELF)


def parse_inotify(buf: bytes) -> list[tuple[int, int, str]]:
    """``(wd, mask, name)`` of each ``struct inotify_event`` in ``buf``; a cut record ends the list
    with an overflow entry."""
    out, off = [], 0
    while off + 16 <= len(buf):
        wd, mask, _cookie, nlen = struct.unpack_from("iIII", buf, off)
        if off + 16 + nlen > len(buf):
            out.append((-1, IN_Q_OVERFLOW, ""))
            break
        name = bytes(buf[off + 16: off + 16 + nlen]).split(b"\0", 1)[0]
        out.append((wd, mask, os.fsdecode(name)))
        off += 16 + nlen
    return out


# -- sources -------------------------------------------------------------------------------------

class Source:
    """Changed paths (relative to ``root``) pushed by a reader thread; :meth:`wait` drains them."""

    backend = "none"

    def __init__(self, root: Path):
        self.root = Path(root)
        self.q: queue.Queue = queue.Queue()
        self.error: str | None = None
        self.note: str | None = None
        self.closed = threading.Event()

    def _push(self, rel: str) -> None:
        if relevant(rel):
            self.q.put(rel)

    def wait(self, timeout: float) -> list[str]:
        """The relevant paths changed since the last call, waiting up to ``timeout`` seconds for the
        first; ``[]`` when none came."""
        try:
            first = self.q.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return []
        got = [first]
        while True:
            try:
                got.append(self.q.get_nowait())
            except queue.Empty:
                return got

    @property
    def alive(self) -> bool:
        return not self.closed.is_set()

    def close(self) -> None:
        self.closed.set()


class WinSource(Source):
    """``ReadDirectoryChangesW`` on the root, recursive, read by a daemon thread (synchronous calls;
    :meth:`close` cancels the pending one with ``CancelIoEx``)."""

    backend = "ReadDirectoryChangesW"
    FILTER = 0x1 | 0x2 | 0x8 | 0x10  # file name, dir name, size, last write

    def __init__(self, root: Path):
        super().__init__(root)
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.restype = wintypes.HANDLE
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k.ReadDirectoryChangesW.restype = wintypes.BOOL
        k.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.BOOL,
                                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
                                            wintypes.LPVOID]
        k.CancelIoEx.restype = wintypes.BOOL
        k.CancelIoEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        self._k, self._ctypes, self._wt = k, ctypes, wintypes
        h = k.CreateFileW(str(self.root), 0x1, 0x1 | 0x2 | 0x4, None, 3, 0x02000000, None)
        if h is None or h == wintypes.HANDLE(-1).value:
            raise OSError(ctypes.get_last_error(), f"CreateFileW failed on {self.root}")
        self._h = h
        self._t = threading.Thread(target=self._read, name="verinoda-fswatch", daemon=True)
        self._t.start()

    def _read(self) -> None:
        ct, wt = self._ctypes, self._wt
        buf = ct.create_string_buffer(BUF_SIZE)
        got = wt.DWORD(0)
        while not self.closed.is_set():
            ok = self._k.ReadDirectoryChangesW(self._h, buf, BUF_SIZE, True, self.FILTER, ct.byref(got), None, None)
            if self.closed.is_set():
                break
            if not ok:
                self.error = f"ReadDirectoryChangesW failed (error {ct.get_last_error()})"
                self.closed.set()
                self.q.put(OVERFLOW)  # wake the watcher: it falls back to polling
                break
            for rel in parse_win(buf.raw, got.value):
                self._push(rel)

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        self._k.CancelIoEx(self._h, None)
        self._t.join(2)
        self._k.CloseHandle(self._h)


class InotifySource(Source):
    """``inotify`` with a watch on each folder of the tree (ignored folders left out); a folder created
    later gets its watches when its event arrives."""

    backend = "inotify"

    def __init__(self, root: Path):
        super().__init__(root)
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
        libc.inotify_init1.argtypes = [ctypes.c_int]
        libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._libc, self._ctypes = libc, ctypes
        fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self._fd = fd
        self._dirs: dict[int, str] = {}   # watch descriptor -> folder relative to the root ("" = root)
        self._add_tree("")
        self._t = threading.Thread(target=self._read, name="verinoda-fswatch", daemon=True)
        self._t.start()

    def _add(self, rel: str) -> bool:
        path = self.root / rel if rel else self.root
        wd = self._libc.inotify_add_watch(self._fd, os.fsencode(str(path)), IN_MASK | IN_ONLYDIR)
        if wd < 0:
            err = self._ctypes.get_errno()
            if err == 28 and self.note is None:  # ENOSPC: the per-user watch limit
                self.note = ("the inotify watch limit (fs.inotify.max_user_watches) was reached: folders "
                             "past it are only seen by the periodic look")
            return False
        self._dirs[wd] = rel
        return True

    def _add_tree(self, rel: str) -> None:
        if not self._add(rel):
            return
        top = self.root / rel if rel else self.root
        for dirpath, dirnames, _files in os.walk(top):
            dirnames[:] = [d for d in dirnames if relevant(d)]
            base = Path(dirpath).relative_to(self.root).as_posix()
            dirnames[:] = [d for d in dirnames if self._add(f"{base}/{d}" if base != "." else d)]

    def _read(self) -> None:
        import select

        while not self.closed.is_set():
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.5)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                buf = os.read(self._fd, BUF_SIZE)
            except BlockingIOError:
                continue
            except OSError as exc:
                self.error = f"inotify read failed: {exc}"
                self.closed.set()
                self.q.put(OVERFLOW)
                break
            for wd, mask, name in parse_inotify(buf):
                if mask & IN_Q_OVERFLOW:
                    self.q.put(OVERFLOW)
                    continue
                if mask & IN_IGNORED:
                    self._dirs.pop(wd, None)
                    continue
                base = self._dirs.get(wd)
                if base is None:
                    continue
                rel = f"{base}/{name}" if base and name else (name or base)
                if mask & IN_ISDIR and mask & (IN_CREATE | IN_MOVED_TO) and relevant(rel):
                    self._add_tree(rel)
                self._push(rel or ".")

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        self._t.join(2)
        try:
            os.close(self._fd)
        except OSError:
            pass


class WatchdogSource(Source):
    """The ``watchdog`` package when installed (FSEvents on macOS, kqueue on BSD)."""

    backend = "watchdog"

    def __init__(self, root: Path):
        super().__init__(root)
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        src = self

        class _H(FileSystemEventHandler):
            def on_any_event(self, event):  # noqa: D401 - watchdog's callback
                for p in (getattr(event, "src_path", ""), getattr(event, "dest_path", "")):
                    if p:
                        try:
                            src._push(Path(os.fsdecode(p)).relative_to(src.root).as_posix())
                        except ValueError:
                            src._push(OVERFLOW)

        self._obs = Observer()
        self._obs.schedule(_H(), str(self.root), recursive=True)
        self._obs.start()
        self.backend = f"watchdog ({type(self._obs).__name__})"

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        self._obs.stop()
        self._obs.join(2)


def open_source(root: Path, *, platform: str | None = None) -> tuple[Source | None, str]:
    """An event source for ``root`` and a note; ``(None, why)`` when none can run (then poll)."""
    platform = platform or sys.platform
    root = Path(root).resolve()
    tried = []
    kinds = ([WinSource] if platform == "win32" else [InotifySource] if platform.startswith("linux") else [])
    kinds.append(WatchdogSource)
    for kind in kinds:
        try:
            return kind(root), ""
        except ImportError:
            tried.append(f"{kind.backend}: not installed")
        except Exception as exc:  # noqa: BLE001 - any failure means polling, said in the note
            tried.append(f"{kind.backend}: {type(exc).__name__}: {exc}"[:200])
    return None, "no file events (" + "; ".join(tried) + "): polling"


# -- the watcher ---------------------------------------------------------------------------------

class Watcher(threading.Thread):
    """Run ``verinoda update`` when the project's files change.

    With an event source (``events=True``, the default), an event wakes the watcher; it waits until
    no event came for ``settle`` seconds (a save that writes several files is one update; at most
    ``max_settle``), then compares the tree's signature with the last one and updates when it differs.
    Without events (none available, or the source failed), every ``interval`` seconds the signature
    is compared; once it has stopped changing for one more look, ``update`` runs. One update at a time;
    a slow listing slows the looks down.
    """

    def __init__(self, repo: Path, *, interval: float = 2.0, run_update=None, purpose: str = "ui --watch",
                 fast: bool = False, events: bool = True, settle: float = 0.3, max_settle: float = 5.0,
                 rescan: float = 60.0):
        super().__init__(name=f"verinoda-watch ({purpose})", daemon=True)
        self.repo, self.interval = Path(repo).resolve(), interval
        self.purpose, self.fast, self.events = purpose, fast, events
        self.settle, self.max_settle, self.rescan = settle, max_settle, rescan
        self.run_update = run_update or self._update
        self.stop = threading.Event()
        self.ready = threading.Event()  # the first look is taken and the source opened
        self.running, self.updates, self.error = False, 0, None
        self.backend, self.note, self.wakeups = "poll", None, 0
        self.source: Source | None = None

    def status(self) -> dict:
        return {"running": self.running, "updates": self.updates, "error": self.error,
                "backend": self.backend, "note": self.note}

    def signature(self) -> tuple:
        from verinoda.snapshot import list_files

        sig = []
        for f in list_files(self.repo):
            try:
                st = (self.repo / f).stat()
            except OSError:
                continue
            sig.append((f, st.st_size, st.st_mtime_ns))
        return tuple(sig)

    def _update(self):
        from verinoda import workflow
        from verinoda.store import open_store

        st = open_store(self.repo)
        try:
            return workflow.update(st, self.repo, wait=0, purpose=self.purpose, fast=self.fast)
        finally:
            st.close()

    def _do_update(self, now: tuple) -> tuple | None:
        """Run one update; the signature to compare with next (None: look again next time)."""
        self.running = True
        try:
            res = self.run_update()
            if isinstance(res, dict) and res.get("mode") == "busy":
                # another build is running: nothing was done; the change is picked up next time
                self.error = res.get("error")
                return None
            self.updates += 1
            self.error = None
        except Exception as exc:  # noqa: BLE001 - reported; the next change tries again
            self.error = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            self.running = False
        try:
            return self.signature()
        except Exception:  # noqa: BLE001
            return now

    def _wait(self, src: Source, timeout: float) -> list[str]:
        """Events within ``timeout``, in short slices so :attr:`stop` is seen."""
        end = time.monotonic() + timeout
        while not self.stop.is_set():
            got = src.wait(min(0.5, max(0.0, end - time.monotonic())))
            if got or time.monotonic() >= end:
                return got
        return []

    def run(self) -> None:
        try:
            last = self.signature()
        except Exception as exc:  # noqa: BLE001 - no listing, no watching; the caller still works
            self.error = f"{type(exc).__name__}: {exc}"[:300]
            return
        if self.events:
            self.source, why = open_source(self.repo)
            if self.source is not None:
                self.backend, self.note = self.source.backend, self.source.note
            else:
                self.note = why
        self.ready.set()
        try:
            if self.source is not None:
                last = self._run_events(self.source, last)
            if not self.stop.is_set():
                self._run_poll(last)
        finally:
            if self.source is not None:
                self.source.close()

    def _run_events(self, src: Source, last):
        while not self.stop.is_set() and src.alive:
            # after a busy build lock, look again soon rather than on the next event
            got = self._wait(src, self.rescan if last is not None else self.interval)
            if self.stop.is_set():
                break
            if got:
                self.wakeups += 1
                t_end = time.monotonic() + self.max_settle
                while time.monotonic() < t_end and not self.stop.is_set():  # until quiet
                    if not self._wait(src, self.settle):
                        break
            self.note = src.note or self.note
            try:
                now = self.signature()
            except Exception as exc:  # noqa: BLE001 - try again on the next event
                self.error = f"{type(exc).__name__}: {exc}"[:300]
                continue
            if last is not None and now == last:
                continue
            last = self._do_update(now)
        if not src.alive and not self.stop.is_set():
            self.backend = "poll"
            self.note = f"file events stopped ({src.error or 'source closed'}): polling"
        return last

    def _run_poll(self, last) -> None:
        wait = self.interval
        pending = None
        while not self.stop.wait(wait):
            t0 = time.perf_counter()
            try:
                now = self.signature()
            except Exception as exc:  # noqa: BLE001 - try again next time
                self.error = f"{type(exc).__name__}: {exc}"[:300]
                continue
            wait = max(self.interval, 10 * (time.perf_counter() - t0))  # a large tree is looked at less often
            if now == last:
                pending = None
                continue
            if now != pending:  # still being written: look once more before updating
                pending = now
                continue
            last = self._do_update(now)
            pending = None
