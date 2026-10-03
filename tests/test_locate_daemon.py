"""The per-repository `verinoda locate` daemon (verinoda/locate_daemon.py): the graph stays loaded between requests.

The server is exercised in-process (a thread on a port the system picks) for its routes, token and idle end; `start`,
`status` and `stop` once for real, with a detached process, on a copy of examples/orders_app that is indexed once."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from test_locate import EXAMPLE, TEXT, _git

from verinoda import cli, locate_daemon, workflow
from verinoda.store import open_store


@pytest.fixture(scope="module")
def orders(tmp_path_factory) -> Path:
    repo = tmp_path_factory.mktemp("daemon") / "orders_app"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", ".pytest_cache", "*.db"))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    for i in range(3):
        for rel in ("orders/pricing.py", "tests/test_pricing.py"):
            with open(repo / rel, "ab") as fh:
                fh.write(f"\n# change {i}\n".encode())
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"change {i}")
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
    finally:
        st.close()
    return repo


def call(url: str, path: str, token: str | None, body=None, method: str | None = None, raw: bytes | None = None):
    """(status, parsed JSON) of one request; an HTTP error is an answer, not an exception."""
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode("utf-8"))
    req = urllib.request.Request(url + path, data=data, method=method or ("POST" if data is not None else "GET"))
    if token is not None:
        req.add_header("X-Verinoda-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8")
        return e.code, (json.loads(text) if text.startswith("{") else text)


@pytest.fixture()
def running(orders):
    """A daemon serving in this process; yields (url, token, daemon) and stops it."""
    d = locate_daemon.Daemon(orders, token="tok-" + "x" * 40, idle_timeout=600)
    t = threading.Thread(target=d.serve_forever, daemon=True)
    t.start()
    assert d.ready.wait(60)
    try:
        yield d.url, d.token, d
    finally:
        d.shutdown()
        t.join(10)


# -- the routes -------------------------------------------------------------------------------------------------

def test_every_request_needs_the_token(running):
    url, token, _ = running
    assert call(url, "/status", None)[0] == 401
    assert call(url, "/status", "wrong")[0] == 401
    assert call(url, "/locate", None, {"text": TEXT})[0] == 401
    assert call(url, "/shutdown", "wrong", {})[0] == 401
    assert call(url, "/status", token)[0] == 200


def test_locate_answers_the_cli_json_with_the_text_a_model_reads(running):
    url, token, _ = running
    code, res = call(url, "/locate", token, {"text": TEXT, "anchors": [], "max_chars": 1800})
    assert code == 200 and res["files"][0]["path"] == "orders/pricing.py"
    assert res["text"].startswith("verinoda locate: ") and "orders/pricing.py" in res["text"] and len(res["text"]) <= 1800
    code, short = call(url, "/locate", token, {"text": TEXT, "max_chars": 300})
    assert code == 200 and len(short["text"]) <= 300


def test_coupled_answers_with_its_text_and_the_status_counts_requests(running):
    url, token, d = running
    code, res = call(url, "/coupled", token, {"files": ["orders/pricing.py"]})
    assert code == 200 and res["files"][0]["path"] == "tests/test_pricing.py"
    assert res["text"].startswith("verinoda coupled: ") and "tests/test_pricing.py" in res["text"]
    code, st = call(url, "/status", token)
    assert code == 200 and st["requests"] >= 1 and st["graph_loaded"] is True and st["pid"] > 0
    assert Path(st["repo"]) == d.repo and st["idle_timeout"] == 600


def test_a_bad_request_is_an_error_answer_and_the_daemon_serves_the_next(running):
    url, token, _ = running
    assert call(url, "/locate", token, {"text": "   "})[0] == 400
    assert call(url, "/locate", token, raw=b"not json")[0] == 400
    assert call(url, "/coupled", token, {"files": []})[0] == 400
    assert call(url, "/nope", token, {})[0] == 404
    assert call(url, "/locate", token, method="GET")[0] == 405
    assert call(url, "/locate", token, {"text": TEXT})[0] == 200


def test_concurrent_requests_are_answered_one_after_another(running):
    url, token, _ = running
    codes = []

    def ask():
        codes.append(call(url, "/locate", token, {"text": TEXT})[0])
    threads = [threading.Thread(target=ask) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert codes == [200] * 4


def test_shutdown_ends_the_server_and_an_idle_one_ends_by_itself(orders):
    d = locate_daemon.Daemon(orders, token="t" * 40, idle_timeout=600)
    t = threading.Thread(target=d.serve_forever, daemon=True)
    t.start()
    assert d.ready.wait(60)
    assert call(d.url, "/shutdown", d.token, {})[0] == 200
    t.join(10)
    assert not t.is_alive()
    idle = locate_daemon.Daemon(orders, token="t" * 40, idle_timeout=0.4)
    t = threading.Thread(target=idle.serve_forever, daemon=True)
    t.start()
    assert idle.ready.wait(60)
    t.join(10)
    assert not t.is_alive()  # nothing asked: it ended after the idle timeout


# -- start, status and stop with a detached process -------------------------------------------------------------

def test_a_daemon_is_started_found_and_stopped_by_its_state_file(orders):
    assert locate_daemon.status(orders) == {"running": False}
    try:
        first = locate_daemon.start(orders, idle_timeout=120)
        assert first["running"] is True and first["started"] is True and first["url"].startswith("http://127.0.0.1:")
        assert len(first["token"]) >= 32 and first["pid"] > 0
        state = orders / ".verinoda" / "locate-daemon.json"
        assert state.is_file() and json.loads(state.read_text(encoding="utf-8"))["token"] == first["token"]
        code, res = call(first["url"], "/locate", first["token"], {"text": TEXT})
        assert code == 200 and res["files"][0]["path"] == "orders/pricing.py"
        again = locate_daemon.start(orders)
        assert again["started"] is False and again["pid"] == first["pid"] and again["token"] == first["token"]
        assert locate_daemon.status(orders)["pid"] == first["pid"]
    finally:
        assert locate_daemon.stop(orders) == {"running": False}
    assert locate_daemon.status(orders) == {"running": False}
    assert not (orders / ".verinoda" / "locate-daemon.json").exists()
    assert locate_daemon.stop(orders) == {"running": False}  # nothing to stop is not an error


def test_a_state_file_nobody_answers_for_is_stale_and_removed(orders):
    state = orders / ".verinoda" / "locate-daemon.json"
    state.write_text(json.dumps({"pid": 1, "url": "http://127.0.0.1:9", "host": "127.0.0.1", "port": 9, "token": "x" * 40,
                                 "repo": str(orders)}), encoding="utf-8")
    assert locate_daemon.status(orders) == {"running": False}
    assert not state.exists()
    state.write_text("{ not json", encoding="utf-8")  # a file that is no state at all goes the same way
    assert locate_daemon.status(orders) == {"running": False}
    assert not state.exists()


def test_the_command_line_starts_reports_and_stops_it(orders, capsys):
    try:
        assert cli.main(["locate", "--daemon", "status", "--repo", str(orders), "--json"]) == 0
        assert json.loads(capsys.readouterr().out) == {"running": False}
        assert cli.main(["locate", "--daemon", "start", "--repo", str(orders), "--json"]) == 0
        started = json.loads(capsys.readouterr().out)
        assert started["running"] is True and started["url"].startswith("http://127.0.0.1:")
        assert cli.main(["locate", "--daemon", "status", "--repo", str(orders)]) == 0
        assert "running" in capsys.readouterr().out
    finally:
        assert cli.main(["locate", "--daemon", "stop", "--repo", str(orders), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"running": False}


def test_start_without_an_index_is_an_error_not_a_hang(tmp_path, capsys):
    repo = tmp_path / "bare"
    repo.mkdir()
    started = time.monotonic()
    with pytest.raises(SystemExit):
        cli.main(["locate", "--daemon", "start", "--repo", str(repo), "--json"])
    assert time.monotonic() - started < 20


# -- a graph that takes a long time to load ---------------------------------------------------------------------

def test_the_daemon_listens_before_the_graph_is_loaded_and_coupled_answers_meanwhile(orders, monkeypatch):
    """Home Assistant's graph takes 30 s or more to load: `start` must not wait for it, and what needs no graph
    (`coupled`) is answered meanwhile; `locate` waits for it."""
    gate = threading.Event()
    real = locate_daemon.locate.GraphKeeper.__call__

    def slow(self):
        gate.wait(60)
        return real(self)
    monkeypatch.setattr(locate_daemon.locate.GraphKeeper, "__call__", slow)
    d = locate_daemon.Daemon(orders, token="t" * 40, idle_timeout=600)
    t = threading.Thread(target=d.serve_forever, daemon=True)
    t.start()
    try:
        assert d.ready.wait(10) and not d.loaded.is_set()
        code, st = call(d.url, "/status", d.token)
        assert code == 200 and st["graph_loaded"] is False and st["loading"] is True
        code, res = call(d.url, "/coupled", d.token, {"files": ["orders/pricing.py"]})
        assert code == 200 and res["files"][0]["path"] == "tests/test_pricing.py"
        answers = []
        asker = threading.Thread(target=lambda: answers.append(call(d.url, "/locate", d.token, {"text": TEXT})))
        asker.start()
        asker.join(1.0)
        assert asker.is_alive() and not answers  # locate waits for the graph
        gate.set()
        asker.join(60)
        assert answers and answers[0][0] == 200 and answers[0][1]["files"][0]["path"] == "orders/pricing.py"
        code, st = call(d.url, "/status", d.token)
        assert st["graph_loaded"] is True and st["loading"] is False
    finally:
        gate.set()
        d.shutdown()
        t.join(10)


# -- what an independent review found ----------------------------------------------------------------------------

def _state(orders, **kw) -> Path:
    state = orders / ".verinoda" / "locate-daemon.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"pid": 1, "host": "127.0.0.1", "token": "x" * 40, "repo": str(orders), **kw}), encoding="utf-8")
    return state


def test_status_is_this_repositorys_daemon_not_any_server_on_the_port(orders):
    import http.server

    class Other(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # a program that answers 200 to anything
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            return
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Other)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        state = _state(orders, port=srv.server_address[1], url=f"http://127.0.0.1:{srv.server_address[1]}")
        assert locate_daemon.status(orders) == {"running": False}
        assert not state.exists()
    finally:
        srv.shutdown()


def test_a_daemon_of_another_repository_on_the_port_is_not_this_ones(orders, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    d = locate_daemon.Daemon(orders, token="t" * 40, idle_timeout=600)
    t = threading.Thread(target=d.serve_forever, daemon=True)
    t.start()
    assert d.ready.wait(60)
    try:
        state = _state(other, port=d.port, token=d.token, pid=os.getpid(), repo=str(other))  # the other repository's file names our port
        assert locate_daemon.status(other) == {"running": False}
        assert not state.exists()
    finally:
        d.shutdown()
        t.join(10)


def test_a_host_in_the_state_file_is_never_connected_to(orders, monkeypatch):
    """A repository (cloned from anywhere) must not be able to make the commands, or the mod that asks them, talk to another host."""
    seen = []

    class Recorder:
        def __init__(self, host, port, timeout=None):
            seen.append(host)

        def request(self, *a, **k):
            raise ConnectionRefusedError

        def close(self):
            return None
    monkeypatch.setattr(locate_daemon.http.client, "HTTPConnection", Recorder)
    _state(orders, host="example.invalid", port=80, url="http://example.invalid:80")
    assert locate_daemon.status(orders) == {"running": False}
    assert seen == ["127.0.0.1"]
    state = _state(orders, host="example.invalid", port=70000, url="http://example.invalid:80")  # not a port either
    assert locate_daemon.status(orders) == {"running": False} and not state.exists()


def test_a_busy_daemon_that_misses_one_status_request_keeps_its_state_file(orders, monkeypatch):
    state = _state(orders, port=9, url="http://127.0.0.1:9", pid=4242)

    def slow(*a, **k):
        raise TimeoutError("timed out")
    monkeypatch.setattr(locate_daemon, "_request", slow)
    res = locate_daemon.status(orders)
    assert res["running"] is True and res["unresponsive"] is True and res["pid"] == 4242 and res["url"] == "http://127.0.0.1:9"
    assert state.exists()  # a daemon that is busy is not a daemon that is gone: a second one is not started over it


def test_stop_with_a_token_the_daemon_refuses_says_it_still_runs_and_keeps_the_state(orders):
    d = locate_daemon.Daemon(orders, token="t" * 40, idle_timeout=600)
    t = threading.Thread(target=d.serve_forever, daemon=True)
    t.start()
    assert d.ready.wait(60)
    try:
        state = _state(orders, port=d.port, token="wrong" * 8, pid=os.getpid(), repo=str(d.repo))
        res = locate_daemon.stop(orders)
        assert res["running"] is True and "refuses" in res["error"]
        assert state.exists() and call(d.url, "/status", d.token)[0] == 200  # still there, still answering to its own token
    finally:
        d.shutdown()
        t.join(10)


def test_two_starts_at_once_leave_one_daemon(orders):
    results = []
    threads = [threading.Thread(target=lambda: results.append(locate_daemon.start(orders, idle_timeout=120))) for _ in range(2)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(180)
        assert len(results) == 2 and results[0]["running"] and results[1]["running"]
        assert results[0]["pid"] == results[1]["pid"] and results[0]["token"] == results[1]["token"]
        time.sleep(3)  # the one that lost its race has ended
        log = (orders / ".verinoda" / "locate-daemon.log").read_text(encoding="utf-8", errors="replace")
        assert log.count("another daemon serves this repository") <= 1
        assert locate_daemon.status(orders)["pid"] == results[0]["pid"]
    finally:
        locate_daemon.stop(orders)


def test_a_request_in_flight_is_not_cut_by_the_idle_timeout(orders, monkeypatch):
    real = locate_daemon.locate.handle

    def slow(request, repo, keeper, cache=None):
        time.sleep(1.5)
        return real(request, repo, keeper, cache)
    monkeypatch.setattr(locate_daemon.locate, "handle", slow)
    d = locate_daemon.Daemon(orders, token="t" * 40, idle_timeout=0.5)
    t = threading.Thread(target=d.serve_forever, daemon=True)
    t.start()
    assert d.ready.wait(60)
    code, res = call(d.url, "/coupled", d.token, {"files": ["orders/pricing.py"]})
    assert code == 200 and res["files"]
    t.join(10)
    assert not t.is_alive()  # then it ended, an idle timeout after the request ended


def test_a_body_over_the_limit_is_a_413(running):
    url, token, _ = running
    big = b'{"text": "' + b"x" * (locate_daemon.MAX_BODY + 10) + b'"}'
    assert call(url, "/locate", token, raw=big)[0] == 413
    assert call(url, "/locate", token, {"text": TEXT})[0] == 200  # and the next request is served


# -- the commands use a running daemon by themselves ---------------------------------------------------------------

def test_locate_and_coupled_ask_a_running_daemon_and_compute_for_themselves_when_none_runs(orders, capsys):
    """The mod runs the commands (a host call has a time limit a big repository's answer can pass, and a command's can be
    ten minutes); the daemon's graph is what makes them quick, so they find it by themselves."""
    try:
        locate_daemon.start(orders, idle_timeout=120)
        before = locate_daemon.status(orders)["requests"]
        assert cli.main(["locate", TEXT, "--repo", str(orders), "--json"]) == 0
        res = json.loads(capsys.readouterr().out)
        assert res["files"][0]["path"] == "orders/pricing.py" and res["text"].startswith("verinoda locate: ")
        assert locate_daemon.status(orders)["requests"] == before + 1  # the daemon answered it
        assert cli.main(["coupled", "orders/pricing.py", "--repo", str(orders), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["files"][0]["path"] == "tests/test_pricing.py"
        assert locate_daemon.status(orders)["requests"] == before + 2
        assert cli.main(["locate", TEXT, "--repo", str(orders), "--json", "--no-daemon"]) == 0
        capsys.readouterr()
        assert cli.main(["coupled", "orders/pricing.py", "--repo", str(orders), "--no-daemon"]) == 0
        assert capsys.readouterr().out.startswith("verinoda coupled: ")
        assert locate_daemon.status(orders)["requests"] == before + 2  # --no-daemon asks nobody
        assert cli.main(["locate", TEXT, "--repo", str(orders), "--max-chars", "400"]) == 0  # text mode, through the daemon too
        assert len(capsys.readouterr().out.strip()) <= 400 and locate_daemon.status(orders)["requests"] == before + 3
    finally:
        locate_daemon.stop(orders)
    assert cli.main(["locate", TEXT, "--repo", str(orders), "--json"]) == 0  # none runs: computed here, the same answer
    assert json.loads(capsys.readouterr().out)["files"][0]["path"] == "orders/pricing.py"


def test_a_daemon_that_cannot_answer_does_not_stop_the_command(orders, capsys, monkeypatch):
    def broken(*a, **k):
        raise OSError("boom")
    monkeypatch.setattr(locate_daemon, "_post", broken)
    _state(orders, port=9, pid=1)
    assert cli.main(["locate", TEXT, "--repo", str(orders), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["files"][0]["path"] == "orders/pricing.py"
