"""One MCP server over several projects (verinoda.mcp.projects) and the HTTP transport (verinoda.mcp.transport).

Two fixture projects (copies of examples/orders_app and examples/forge_mod, scanned) are answered from one server
built in-process; a call that does not say which project is refused; each project keeps the profile a server
over it alone would give it. The HTTP tests bind a real port on 127.0.0.1 and shut the server down after; the
daemon test starts a real background process and always stops it.
"""

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import contextlib  # noqa: E402
import http.client  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda.mcp import projects as P  # noqa: E402
from verinoda.mcp import server as S  # noqa: E402
from verinoda.mcp import transport as T  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
QUESTION = "where is the order saved to the database?"

anyio = pytest.importorskip("anyio")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


def _project(dst: Path, example: str) -> Path:
    from verinoda import workflow
    from verinoda.store import open_store

    shutil.copytree(ROOT / "examples" / example, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__",
                                                                                    "*.pyc", "*.db"))
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    workflow.init(dst)
    st = open_store(dst)
    try:
        workflow.scan(st, dst)
    finally:
        st.close()
    return dst


@pytest.fixture(scope="module")
def two(tmp_path_factory) -> list[tuple[str, Path]]:
    base = tmp_path_factory.mktemp("projects")
    return [("orders", _project(base / "orders_app", "orders_app")),
            ("forge", _project(base / "forge_mod", "forge_mod"))]


@pytest.fixture
def config_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "userconfig"
    monkeypatch.setenv("VERINODA_CONFIG_DIR", str(d))
    return d


def _text(res) -> str:
    return res.content[0].text


def _json(res) -> dict:
    return json.loads(_text(res))


def _err(res) -> bool:
    v = getattr(res, "is_error", None)
    return bool(getattr(res, "isError", False) if v is None else v)


# -- one server, two projects ----------------------------------------------------------------

def test_two_projects_are_answered_from_one_server(two, config_dir):
    hub = P.ProjectHub(two)
    srv = S.build_server(None, hub=hub)
    listed = {t.name: t for t in anyio.run(srv.list_tools)}
    assert set(listed) == {*S.CORE_DIRECT, S.GATEWAY}  # the core menu, each tool with a project argument
    for t in listed.values():
        schema = getattr(t, "input_schema", None) or t.inputSchema
        assert "project" in schema["properties"] and "project" not in schema.get("required", [])
    gate = getattr(listed[S.GATEWAY], "input_schema", None) or listed[S.GATEWAY].inputSchema
    assert {"list_projects", "index_status"} <= set(gate["properties"]["name"]["enum"])
    assert "2 projects: orders (" in srv.verinoda_instructions and "forge (" in srv.verinoda_instructions

    async def calls():
        a = await srv.call_tool("project_query", {"question": QUESTION, "project": "orders"})
        b = await srv.call_tool("project_query", {"question": "the forge screen init", "project": "forge"})
        by_path = await srv.call_tool("run_tool", {"name": "node_inspect", "arguments": {"name": "place_order"},
                                                   "project": str(two[0][1] / "orders")})
        inside = await srv.call_tool("run_tool", {"name": "node_inspect", "arguments": {"name": "place_order",
                                                                                       "project": "orders"}})
        lp = await srv.call_tool("run_tool", {"name": "list_projects"})
        st = await srv.call_tool("run_tool", {"name": "index_status"})
        one = await srv.call_tool("run_tool", {"name": "index_status", "project": "forge"})
        return a, b, by_path, inside, lp, st, one

    a, b, by_path, inside, lp, st, one = anyio.run(calls)
    assert not _err(a) and not _err(b)
    # the same answers a server over each project alone gives
    assert _text(a) == S.AtlasTools(two[0][1]).project_query(QUESTION)["text"]
    assert _text(b) == S.AtlasTools(two[1][1]).project_query("the forge screen init")["text"]
    assert "orders/repository.py" in _text(a) and ".kt" in _text(b) and "orders/" not in _text(b)
    assert _json(by_path)["node"]["file"] == "orders/service.py" == _json(inside)["node"]["file"]
    rows = {r["name"]: r for r in _json(lp)["projects"]}
    assert set(rows) == {"orders", "forge"} and all(r["graph_loaded"] and r["state"] == "indexed"
                                                       for r in rows.values())
    assert {r["name"] for r in _json(st)["projects"]} == {"orders", "forge"}
    assert _json(one)["state"] == "indexed" and _json(one)["stale_count"] == 0
    assert _json(one)["path"] == str(two[1][1])


def test_a_call_that_does_not_say_which_project_is_refused(two, config_dir):
    srv = S.build_server(None, hub=P.ProjectHub(two))
    elsewhere = two[0][1].parent / "not_served"

    async def calls():
        return (await srv.call_tool("project_query", {"question": QUESTION}),
                await srv.call_tool("project_query", {"question": QUESTION, "project": "nope"}),
                await srv.call_tool("project_query", {"question": QUESTION, "project": str(elsewhere)}),
                await srv.call_tool("project_query", {"question": QUESTION, "project": "orders_app"}),
                await srv.call_tool("run_tool", {"name": "node_inspect", "arguments": {"name": "place_order"}}),
                # a relative path ties nothing; an absolute one inside a project does
                await srv.call_tool("run_tool", {"name": "read_context",
                                                 "arguments": {"file_path": "orders/service.py"}}),
                await srv.call_tool("run_tool", {"name": "read_context",
                                                 "arguments": {"file_path": str(two[0][1] / "orders" / "service.py")}}),
                await srv.call_tool("index_update", {}))

    missing, unknown, outside, folder_name, gw, rel, absolute, upd = anyio.run(calls)
    for res, code in ((missing, "project_required"), (unknown, "unknown_project"), (outside, "unknown_project"),
                      (folder_name, "unknown_project"), (gw, "project_required"), (rel, "project_required"),
                      (upd, "project_required")):
        assert _err(res), code
        body = _json(res)
        assert body["error"] == code and "orders, forge" in body["hint"], body
    assert not _err(absolute) and "error" not in _json(absolute)
    assert not (elsewhere / ".verinoda").exists()  # a path outside every project never opens one


def test_one_project_needs_no_project_argument(two, config_dir):
    srv = S.build_server(None, hub=P.ProjectHub(two[:1]))
    res = anyio.run(lambda: srv.call_tool("project_query", {"question": QUESTION}))
    assert not _err(res) and "orders/repository.py" in _text(res)


def test_only_the_most_recently_used_projects_keep_their_graph(two, config_dir):
    hub = P.ProjectHub(two, max_loaded=1)
    srv = S.build_server(None, hub=hub)

    async def ask(project):
        return await srv.call_tool("run_tool", {"name": "node_inspect", "arguments": {"name": "place_order"},
                                                "project": project})

    anyio.run(ask, "orders")
    orders = hub.tools_for("orders")
    assert orders.graph_loaded and orders.cache_stats["graph_loads"] == 1
    anyio.run(ask, "forge")
    assert not orders.graph_loaded and hub.tools_for("forge").graph_loaded and hub.evictions == 1
    res = anyio.run(ask, "orders")  # loaded again on its next call, same answer
    assert not _err(res) and _json(res)["node"]["file"] == "orders/service.py"
    assert orders.cache_stats["graph_loads"] == 2 and not hub.tools_for("forge").graph_loaded
    assert hub.status()["graphs_in_memory"] == ["orders"]


def test_each_project_keeps_its_own_profile_and_trust(two, tmp_path, config_dir):
    from verinoda import paths

    trusted, untrusted = (tmp_path / "trusted", tmp_path / "untrusted")
    for d in (trusted, untrusted):
        shutil.copytree(two[0][1], d)
        (d / ".verinoda" / "config.json").write_text(json.dumps({"mcp": {"profile": "full"}}), encoding="utf-8")
    paths.set_trust(trusted)
    hub = P.ProjectHub([("t", trusted), ("u", untrusted)])
    # the untrusted project's own config cannot ask for the full menu, as for a server over it alone
    assert hub.profiles == {"t": S.resolve_profile(trusted), "u": S.resolve_profile(untrusted)}
    assert hub.profiles == {"t": "full", "u": "core"} and hub.menu_profile == "full"
    srv = S.build_server(None, hub=hub)

    async def calls():
        return (await srv.call_tool("lexicon_show", {"word": "order", "project": "t"}),
                await srv.call_tool("lexicon_show", {"word": "order", "project": "u"}),
                await srv.call_tool("analyze", {"question": QUESTION, "plan_json": "{}", "project": "u"}),
                await srv.call_tool("list_projects", {}))

    ok, refused, extra, lp = anyio.run(calls)
    assert not _err(ok)
    assert _err(refused) and _json(refused)["error"] == "not_served" and "profile core" in _json(refused)["message"]
    assert _err(extra) and _json(extra)["error"] == "not_served" and "plan_json" in _json(extra)["message"]
    assert {r["name"]: (r["trusted"], r["profile"]) for r in _json(lp)["projects"]} == {"t": (True, "full"),
                                                                                         "u": (False, "core")}
    # --profile on the command line applies to every project, as it does for one
    assert set(P.ProjectHub([("t", trusted), ("u", untrusted)], profile="core").profiles.values()) == {"core"}


def test_the_registry_and_project_specs(two, tmp_path, config_dir):
    orders, forge = two[0][1], two[1][1]
    assert P.registered() == []
    row = P.add(orders)
    assert row["name"] == "orders_app" and Path(row["path"]) == Path(os.path.realpath(orders))
    P.add(forge, "forge")
    with pytest.raises(P.ProjectError, match="taken"):
        P.add(tmp_path, "forge")
    with pytest.raises(P.ProjectError, match="letters"):
        P.add(tmp_path, "-bad name")
    assert [r["name"] for r in P.registered()] == ["orders_app", "forge"]
    assert P.resolve_specs(["forge", f"other={orders}"]) == [("forge", Path(os.path.realpath(forge))),
                                                             ("other", Path(os.path.realpath(orders)))]
    assert [n for n, _ in P.resolve_specs([], all_registered=True)] == ["orders_app", "forge"]
    assert P.resolve_specs([str(forge)])[0][0] == "forge"  # a registered folder keeps its registry name
    with pytest.raises(P.ProjectError, match="named twice"):
        P.resolve_specs(["forge", str(forge)])
    twin = tmp_path / "x" / "orders_app"
    twin.mkdir(parents=True)
    with pytest.raises(P.ProjectError, match="two projects are named"):
        P.resolve_specs([str(twin), "orders_app"])
    with pytest.raises(P.ProjectError, match="not a folder"):
        P.resolve_specs(["nothing_registered_by_this_name"])
    P.remove("forge")
    P.remove(str(orders))
    assert P.registered() == []
    with pytest.raises(P.ProjectError):
        P.remove("forge")


# -- HTTP --------------------------------------------------------------------------------------

def _http(port: int, method: str, path: str, *, token: str | None = None, body: dict | None = None,
          session: str | None = None, version: str | None = None) -> tuple[int, dict, dict]:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if session:
        headers["Mcp-Session-Id"] = session
    if version:
        headers["Mcp-Protocol-Version"] = version
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        r = conn.getresponse()
        raw = r.read().decode("utf-8", "replace")
        hdrs = {k.lower(): v for k, v in r.getheaders()}
    finally:
        conn.close()
    data: dict = {}
    if "text/event-stream" in hdrs.get("content-type", ""):
        for line in raw.splitlines():
            if line.startswith("data:") and line[5:].strip():
                data = json.loads(line[5:])
    elif raw.strip():
        data = json.loads(raw)
    return r.status, hdrs, data


@contextlib.contextmanager
def _running(srv, token: str, status=None):
    server, sock, url = T.make_http_server(srv, host="127.0.0.1", port=0, token=token, status=status)
    th = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    th.start()
    try:
        deadline = time.monotonic() + 30
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        yield server, sock.getsockname()[1], url, th
    finally:
        server.should_exit = True
        th.join(30)
        sock.close()
        assert not th.is_alive()


def test_http_needs_the_token_and_answers_two_projects(two, config_dir):
    hub = P.ProjectHub(two)
    srv = S.build_server(None, hub=hub)
    token, path = T.load_token()
    with _running(srv, token, hub.status) as (server, port, url, th):
        assert url == f"http://127.0.0.1:{port}/mcp"
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "test", "version": "0"}}}
        for given in (None, "wrong", token + "x", token[:-1]):
            code, hdrs, body = _http(port, "POST", "/mcp", token=given, body=init)
            assert code == 401 and body["error"] == "unauthorized" and hdrs["www-authenticate"].startswith("Bearer")
        code, _, body = _http(port, "GET", "/verinoda/status")
        assert code == 401
        code, hdrs, body = _http(port, "POST", "/mcp", token=token, body=init)
        assert code == 200 and body["result"]["serverInfo"]["name"] == "verinoda", body
        sid, version = hdrs.get("mcp-session-id"), body["result"]["protocolVersion"]
        code, _, _ = _http(port, "POST", "/mcp", token=token, session=sid, version=version,
                           body={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert code in (200, 202)
        answers = {}
        for i, (project, q) in enumerate((("orders", QUESTION), ("forge", "the forge screen init")), 2):
            code, _, body = _http(port, "POST", "/mcp", token=token, session=sid, version=version,
                                  body={"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                        "params": {"name": "project_query",
                                                   "arguments": {"question": q, "project": project}}})
            assert code == 200 and not body["result"].get("isError"), body
            answers[project] = body["result"]["content"][0]["text"]
        assert "orders/repository.py" in answers["orders"] and ".kt" in answers["forge"]
        code, _, st = _http(port, "GET", "/verinoda/status", token=token)
        assert code == 200 and st["pid"] == os.getpid() and set(st["projects"]) == {"orders", "forge"}
        code, _, body = _http(port, "POST", "/verinoda/shutdown", token=token)
        assert code == 200 and body["stopping"] is True
        th.join(30)
        assert not th.is_alive()  # the shutdown route stopped the server


def test_the_token_file_is_the_users_alone(config_dir):
    token, path = T.load_token()
    assert len(token) >= 40 and path == config_dir / T.TOKEN_NAME
    assert T.load_token() == (token, path)  # kept, not regenerated
    if os.name == "nt":
        acl = subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout
        entries = [line for line in acl.splitlines()[:-2] if ":(" in line]
        assert len(entries) == 1 and os.environ["USERNAME"].lower() in entries[0].lower(), acl
    else:
        assert path.stat().st_mode & 0o077 == 0
    rotated, _ = T.load_token(rotate=True)
    assert rotated != token and T.load_token()[0] == rotated


def test_http_binds_loopback_unless_told_otherwise():
    import inspect

    assert inspect.signature(T.make_http_server).parameters["host"].default == "127.0.0.1"
    assert T.DEFAULT_HOST == "127.0.0.1" and T.is_loopback("127.0.0.1") and not T.is_loopback("0.0.0.0")


def test_daemon_start_status_stop(two, config_dir, monkeypatch):
    # the background server must import this checkout
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in (str(ROOT), os.environ.get("PYTHONPATH")) if p))
    pid = None
    try:
        res = T.daemon_start(["--projects", ",".join(f"{n}={p}" for n, p in two)], host="127.0.0.1", port=0)
        assert res.get("started"), res
        pid = res["pid"]
        st = T.daemon_status()
        assert st["running"] and st["pid"] == pid and set(st["projects"]) == {"orders", "forge"}
        again = T.daemon_start(["--projects", f"orders={two[0][1]}"], host="127.0.0.1", port=0)
        assert again["already_running"] and not again["started"]
        stop = T.daemon_stop()
        assert stop["stopped"] and stop["pid"] == pid
        assert not T.state_path().exists() and T.daemon_status() == {"running": False,
                                                                      "state_file": str(T.state_path())}
        pid = None
    finally:
        if pid is not None:  # never leave the background server running
            with contextlib.suppress(OSError):
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
                else:
                    os.kill(pid, 9)
