"""File events wake the watcher (``verinoda.fswatch``): parsing, filtering, the native sources, the
fallback to polling, and ``mcp serve --watch``."""

from __future__ import annotations

import struct
import sys
import threading
import time
from pathlib import Path

import pytest

from verinoda import fswatch
from verinoda.fswatch import OVERFLOW, Source, Watcher, parse_inotify, parse_win, relevant


def _win_record(name: str, nxt: int = 0, action: int = 3) -> bytes:
    raw = name.encode("utf-16-le")
    rec = struct.pack("<III", nxt, action, len(raw)) + raw
    return rec


def _wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


# -- parsing -------------------------------------------------------------------------------------

def test_parse_win_reads_a_chain_of_records():
    first = _win_record("src\\a.py")
    first = struct.pack("<I", len(first) + (-len(first) % 4)) + first[4:] + b"\0" * (-len(first) % 4)
    buf = first + _win_record("ş/b.py")
    assert parse_win(buf, len(buf)) == ["src\\a.py", "ş/b.py"]


def test_parse_win_an_empty_or_cut_buffer_is_an_overflow():
    assert parse_win(b"", 0) == [OVERFLOW]  # the system's buffer overflowed
    rec = _win_record("a.py")
    assert parse_win(rec, len(rec) - 2) == [OVERFLOW]


def test_parse_inotify_reads_names_and_cut_records():
    name = b"a.py\0\0\0\0"
    buf = struct.pack("iIII", 1, fswatch.IN_CLOSE_WRITE, 0, len(name)) + name
    buf += struct.pack("iIII", -1, fswatch.IN_Q_OVERFLOW, 0, 0)
    assert parse_inotify(buf) == [(1, fswatch.IN_CLOSE_WRITE, "a.py"), (-1, fswatch.IN_Q_OVERFLOW, "")]
    cut = struct.pack("iIII", 2, fswatch.IN_MODIFY, 0, 16) + b"short"
    assert parse_inotify(cut) == [(-1, fswatch.IN_Q_OVERFLOW, "")]


@pytest.mark.parametrize("rel,want", [
    ("src/a.py", True), ("src\\a.py", True), ("README.md", True), (OVERFLOW, True),
    (".verinoda/index/graph.json", False), (".git\\index", False), ("pkg/__pycache__/a.pyc", False),
    ("web/node_modules/x/i.js", False), ("verinoda.egg-info/PKG-INFO", False), (".venv/Lib/x.py", False),
])
def test_relevant_leaves_out_tool_and_cache_folders(rel, want):
    assert relevant(rel) is want


# -- native sources ------------------------------------------------------------------------------

def _native(tmp_path):
    src, why = fswatch.open_source(tmp_path)
    if src is None or src.backend == "watchdog":
        pytest.skip(f"no native file events here: {why}")
    return src


@pytest.mark.skipif(not (sys.platform == "win32" or sys.platform.startswith("linux")),
                    reason="native sources: Windows and Linux")
def test_a_native_source_reports_a_write_and_not_its_own_folders(tmp_path):
    (tmp_path / "sub").mkdir()
    src = _native(tmp_path)
    try:
        (tmp_path / ".verinoda").mkdir()
        (tmp_path / ".verinoda" / "x.json").write_text("{}", encoding="utf-8")
        assert src.wait(0.4) == []  # Verinoda's own writes do not wake anything
        (tmp_path / "sub" / "a.py").write_text("x = 1\n", encoding="utf-8")
        got = src.wait(3)
        assert any(g.replace("\\", "/") == "sub/a.py" for g in got), got
    finally:
        t0 = time.monotonic()
        src.close()
        assert time.monotonic() - t0 < 3 and not src.alive


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="inotify")
def test_inotify_watches_a_folder_created_later(tmp_path):
    src = _native(tmp_path)
    try:
        (tmp_path / "new").mkdir()
        src.wait(1)
        time.sleep(0.2)
        (tmp_path / "new" / "b.py").write_text("y = 2\n", encoding="utf-8")
        assert _wait_for(lambda: "new/b.py" in src.wait(0.3), 3)
    finally:
        src.close()


def test_no_source_means_polling_with_the_reason(tmp_path, monkeypatch):
    class Broken(Source):
        backend = "broken"

        def __init__(self, root):
            raise OSError("not here")

    monkeypatch.setattr(fswatch, "WinSource", Broken)
    monkeypatch.setattr(fswatch, "InotifySource", Broken)
    monkeypatch.setattr(fswatch, "WatchdogSource", Broken)
    src, why = fswatch.open_source(tmp_path)
    assert src is None and "broken: OSError" in why and why.endswith("polling")


# -- the watcher ---------------------------------------------------------------------------------

class FakeSource(Source):
    backend = "fake"


def _watcher(tmp_path, monkeypatch, src=None, **kw):
    calls = []
    if src is not None:
        monkeypatch.setattr(fswatch, "open_source", lambda root: (src, ""))
    w = Watcher(tmp_path, run_update=lambda: calls.append(time.monotonic()), **kw)
    return w, calls


def test_an_event_updates_once_after_the_writes_settle(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    src = FakeSource(tmp_path)
    w, calls = _watcher(tmp_path, monkeypatch, src, interval=30, settle=0.15, rescan=30)
    w.start()
    try:
        assert w.ready.wait(3) and w.backend == "fake"
        for i in range(3):  # a save that writes several files: three events, one update
            (tmp_path / f"b{i}.py").write_text(f"y = {i}\n", encoding="utf-8")
            src.q.put(f"b{i}.py")
            time.sleep(0.05)
        assert _wait_for(lambda: calls)
        time.sleep(0.4)
        assert len(calls) == 1 and w.updates == 1 and w.error is None
    finally:
        w.stop.set()
        w.join(3)
    assert not w.is_alive() and not src.alive  # stopping closes the source


def test_an_event_without_a_change_does_not_update(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    src = FakeSource(tmp_path)
    w, calls = _watcher(tmp_path, monkeypatch, src, interval=30, settle=0.05, rescan=30)
    w.start()
    try:
        assert w.ready.wait(3) and w.backend == "fake"
        src.q.put("a.py")  # touched, same bytes and time: the signature decides
        src.q.put(OVERFLOW)
        assert _wait_for(lambda: w.wakeups == 1)
        time.sleep(0.3)
        assert calls == []
    finally:
        w.stop.set()
        w.join(3)


def test_the_periodic_look_catches_a_change_without_an_event(tmp_path, monkeypatch):
    src = FakeSource(tmp_path)
    w, calls = _watcher(tmp_path, monkeypatch, src, interval=30, settle=0.05, rescan=0.3)
    w.start()
    try:
        assert w.ready.wait(3) and w.backend == "fake"
        (tmp_path / "c.py").write_text("z = 3\n", encoding="utf-8")  # no event: a network share
        assert _wait_for(lambda: calls, 3)
    finally:
        w.stop.set()
        w.join(3)


def test_a_source_that_dies_falls_back_to_polling(tmp_path, monkeypatch):
    src = FakeSource(tmp_path)
    w, calls = _watcher(tmp_path, monkeypatch, src, interval=0.05, settle=0.05, rescan=30)
    w.start()
    try:
        assert w.ready.wait(3) and w.backend == "fake"
        src.error = "ReadDirectoryChangesW failed (error 5)"
        src.closed.set()
        src.q.put(OVERFLOW)
        assert _wait_for(lambda: w.backend == "poll")
        assert "error 5" in w.note
        (tmp_path / "d.py").write_text("w = 4\n", encoding="utf-8")
        assert _wait_for(lambda: calls, 3)
    finally:
        w.stop.set()
        w.join(3)


def test_a_busy_build_is_tried_again_soon(tmp_path, monkeypatch):
    src = FakeSource(tmp_path)
    monkeypatch.setattr(fswatch, "open_source", lambda root: (src, ""))
    answers = [{"mode": "busy", "error": "another build"}, {"mode": "incremental"}]
    calls = []

    def run():
        calls.append(1)
        return answers[min(len(calls), 2) - 1]

    w = Watcher(tmp_path, run_update=run, interval=0.1, settle=0.05, rescan=30)
    w.start()
    try:
        assert w.ready.wait(3) and w.backend == "fake"
        (tmp_path / "e.py").write_text("v = 5\n", encoding="utf-8")
        src.q.put("e.py")
        assert _wait_for(lambda: w.updates == 1, 3)  # no second event needed
        assert len(calls) == 2 and w.error is None
    finally:
        w.stop.set()
        w.join(3)


def test_without_events_the_watcher_polls(tmp_path):
    calls = []
    w = Watcher(tmp_path, run_update=lambda: calls.append(1), interval=0.05, events=False)
    w.start()
    try:
        assert w.ready.wait(3)
        (tmp_path / "f.py").write_text("u = 6\n", encoding="utf-8")
        assert _wait_for(lambda: calls, 3)
        assert w.backend == "poll" and w.source is None
    finally:
        w.stop.set()
        w.join(3)


@pytest.mark.skipif(not (sys.platform == "win32" or sys.platform.startswith("linux")),
                    reason="native sources: Windows and Linux")
def test_a_real_edit_is_picked_up_without_polling(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    done = threading.Event()
    w = Watcher(tmp_path, run_update=done.set, interval=60, rescan=60, settle=0.1)
    w.start()
    try:
        assert w.ready.wait(3)
        if w.source is None or w.backend.startswith("watchdog"):
            pytest.skip(f"no native file events: {w.note}")
        time.sleep(0.2)
        t0 = time.monotonic()
        (tmp_path / "pkg" / "a.py").write_text("x = 2  # edited\n", encoding="utf-8")
        assert done.wait(5)
        assert time.monotonic() - t0 < 3  # an event, not the 60 s poll
        assert w.status()["backend"] == w.backend and w.updates == 1
    finally:
        w.stop.set()
        w.join(3)


# -- mcp serve --watch ---------------------------------------------------------------------------

def test_mcp_serve_takes_watch(monkeypatch, tmp_path):
    from verinoda import cli

    seen = {}
    monkeypatch.setattr("verinoda.mcp.server.serve", lambda repo, profile=None, watch=False:
                        seen.update(repo=repo, watch=watch))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".verinoda").mkdir()
    assert cli.main(["mcp", "serve", "--watch", "--repo", str(tmp_path)]) == 0
    assert seen["watch"] is True


# -- end to end: an edit reaches the index with no `update` call ----------------------------------

@pytest.mark.skipif(not (sys.platform == "win32" or sys.platform.startswith("linux")),
                    reason="native sources: Windows and Linux")
def test_an_edit_reaches_the_graph_through_the_watcher(tmp_path, monkeypatch):
    import json
    import os
    import shutil
    import subprocess
    from pathlib import Path

    from verinoda import workflow
    from verinoda.paths import graph_path
    from verinoda.store import open_store

    if shutil.which("git") is None:
        pytest.skip("git not available")
    monkeypatch.setenv("GRAPHIFY_OUT", os.environ.get("GRAPHIFY_OUT", ".verinoda/index"))
    repo = tmp_path / "orders_app"
    shutil.copytree(Path(__file__).resolve().parents[1] / "examples" / "orders_app", repo,
                    ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", ".pytest_cache", "*.db"))
    for args in (("init", "-q"), ("add", "-A"), ("commit", "-q", "-m", "init")):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                       cwd=repo, check=True, capture_output=True)
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
    finally:
        st.close()
    w = Watcher(repo, purpose="mcp serve --watch", fast=True, interval=60, rescan=60, settle=0.2)
    w.start()
    try:
        assert w.ready.wait(10)
        if w.source is None or w.backend.startswith("watchdog"):
            pytest.skip(f"no native file events: {w.note}")
        svc = repo / "orders" / "service.py"
        svc.write_bytes(svc.read_bytes() + b"\n\ndef watched_marker():\n    return 'seen'\n")
        assert _wait_for(lambda: w.updates >= 1, 60), w.status()
        assert w.error is None

        def in_graph():  # a fast update builds the graph in a background process, as index_update does
            try:
                nodes = json.loads(graph_path(repo).read_text(encoding="utf-8"))["nodes"]
            except (OSError, ValueError):
                return False
            return any(str(n.get("label")).startswith("watched_marker") for n in nodes)

        assert _wait_for(in_graph, 120)
    finally:
        w.stop.set()
        w.join(5)
        from verinoda import buildlock

        _wait_for(lambda: not buildlock.is_locked(repo), 120)  # let the background build end before cleanup


# -- review round ---------------------------------------------------------------------------------

def test_a_save_during_an_update_starts_another_update(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    src = FakeSource(tmp_path)
    monkeypatch.setattr(fswatch, "open_source", lambda root: (src, ""))
    calls = []

    def run():
        calls.append(1)
        if len(calls) == 1:  # the user saves again while the first update runs
            (tmp_path / "a.py").write_text("x = 2  # saved during the update\n", encoding="utf-8")
            src.q.put("a.py")

    w = Watcher(tmp_path, run_update=run, interval=30, settle=0.05, rescan=30)
    w.start()
    try:
        assert w.ready.wait(3)
        (tmp_path / "a.py").write_text("x = 3\n", encoding="utf-8")
        src.q.put("a.py")
        assert _wait_for(lambda: len(calls) == 2, 5), calls
    finally:
        w.stop.set()
        w.join(3)


def test_a_deleted_project_folder_stops_the_watcher_and_is_not_created_again(tmp_path, monkeypatch):
    import shutil

    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    src = FakeSource(repo)
    monkeypatch.setattr(fswatch, "open_source", lambda root: (src, ""))
    calls = []
    w = Watcher(repo, run_update=lambda: calls.append(1) or repo.mkdir(exist_ok=True), interval=30, settle=0.05,
                rescan=30)
    w.start()
    try:
        assert w.ready.wait(3)
        shutil.rmtree(repo)
        src.q.put(OVERFLOW)
        assert _wait_for(lambda: not w.is_alive(), 5)
        assert calls == [] and not repo.exists() and "is gone" in w.error
    finally:
        w.stop.set()
        w.join(3)


def test_a_refused_index_build_is_an_error_not_an_update(tmp_path, monkeypatch):
    src = FakeSource(tmp_path)
    monkeypatch.setattr(fswatch, "open_source", lambda root: (src, ""))
    w = Watcher(tmp_path, run_update=lambda: {"mode": "index_refused", "error": "the graph would shrink"},
                interval=30, settle=0.05, rescan=30)
    w.start()
    try:
        assert w.ready.wait(3)
        (tmp_path / "b.py").write_text("y = 1\n", encoding="utf-8")
        src.q.put("b.py")
        assert _wait_for(lambda: w.error, 3)
        assert w.updates == 0 and "shrink" in w.error
    finally:
        w.stop.set()
        w.join(3)


def test_inotify_stops_when_the_root_goes():
    src = object.__new__(fswatch.InotifySource)
    Source.__init__(src, Path("."))
    src._dirs = {1: "", 2: "sub"}
    sub = struct.pack("iIII", 2, fswatch.IN_DELETE_SELF, 0, 0) + struct.pack("iIII", 2, fswatch.IN_IGNORED, 0, 0)
    assert src._handle(sub) is False and 2 not in src._dirs and src.alive  # a sub-folder going is an event
    root = struct.pack("iIII", 1, fswatch.IN_DELETE_SELF, 0, 0)
    assert src._handle(root) is True and not src.alive and "deleted or moved" in src.error
    assert OVERFLOW in src.wait(0)


@pytest.mark.skipif(sys.platform != "win32", reason="ReadDirectoryChangesW")
def test_windows_source_closes_its_handle_and_never_hangs(tmp_path):
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetHandleInformation.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

    def is_open(h):
        return bool(k.GetHandleInformation(h, ctypes.byref(wintypes.DWORD())))

    stop = threading.Event()

    def churn():  # events all the time: close() races a read being started
        i = 0
        while not stop.is_set():
            (tmp_path / f"c{i % 5}.txt").write_text(str(i), encoding="utf-8")
            i += 1

    t = threading.Thread(target=churn, daemon=True)
    t.start()
    try:
        for _ in range(15):
            src = fswatch.WinSource(tmp_path)
            time.sleep(0.02)
            t0 = time.monotonic()
            src.close()
            assert time.monotonic() - t0 < 3 and not src._t.is_alive() and not is_open(src._h)
    finally:
        stop.set()
        t.join(3)
    gone = tmp_path / "gone"
    gone.mkdir()
    src = fswatch.WinSource(gone)
    gone.rmdir()  # the reader fails: it closes its handle itself
    assert _wait_for(lambda: not src.alive, 5) and src.error
    assert not is_open(src._h)
    src.close()
