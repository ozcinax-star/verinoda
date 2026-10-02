"""``verinoda lsp`` and ``resolve-call --lsp``: an installed language server, driven through the real client.

The server here is a fake (tests/fixtures/lsp/fake_lsp.py): a Python script that speaks LSP over stdio and
answers from a table the test writes. Nothing is installed; every process a test starts is stopped, and the
tests that make a server crash, hang or write garbage check that no process is left behind.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from verinoda import cli, lsp, paths

FAKE = Path(__file__).parent / "fixtures" / "lsp" / "fake_lsp.py"

LIB_TS = ("export function helper(n: number): number {\n  return n + 1;\n}\n\n"
          "export function other(): number {\n  return 2;\n}\n")
APP_TS = ("import { helper, other } from \"./lib\";\n\nexport function run(): number {\n  const a = helper(1);\n"
          "  return a + other();\n}\n")
ALT_TS = "export function other(): number {\n  return 3;\n}\n"
UTIL_JAVA = ("package demo;\n\npublic class Util {\n    @Deprecated\n    public int helper(int n) {\n"
             "        return n + 1;\n    }\n}\n")
MAIN_JAVA = ("package demo;\n\npublic class Main {\n    public static int run() {\n        Util u = new Util();\n"
             "        return u.helper(1);\n    }\n}\n")
API_TS = "export interface Api {\n  other(): number;\n}\n"
FILES = {"src/lib.ts": LIB_TS, "src/app.ts": APP_TS, "src/alt.ts": ALT_TS, "src/api.ts": API_TS,
         "src/main/java/demo/Util.java": UTIL_JAVA, "src/main/java/demo/Main.java": MAIN_JAVA}

HELPER = {"path": "src/lib.ts", "line": 1, "col": 17}
OTHER = {"path": "src/lib.ts", "line": 5, "col": 17}
JAVA_HELPER = {"path": "src/main/java/demo/Util.java", "line": 5, "col": 16}
TABLE = {
    "name": "fake-ls", "version": "1.2.3",
    "definition": {"src/app.ts:4:13": [HELPER], "src/app.ts:5:14": [OTHER],
                   "src/main/java/demo/Main.java:6:18": [JAVA_HELPER]},
    "references": {"src/lib.ts:1:17": [{"path": "src/app.ts", "line": 1, "col": 10},
                                       {"path": "src/app.ts", "line": 4, "col": 13}]},
    "hover": {"src/app.ts:4:13": "function helper(n: number): number"},
    "implementation": {},
    "calls": {"src/app.ts:3:17": {"item": {"name": "run", "path": "src/app.ts", "line": 3, "col": 17},
                                  "outgoing": [{"name": "helper", **HELPER, "from": [[4, 13]]},
                                               {"name": "other", **OTHER, "from": [[5, 14]]}],
                                  "incoming": []}},
    "types": {},
    "diagnostics": {"src/app.ts": [{"line": 5, "col": 3, "message": "unused value", "severity": 2}]},
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


def make_repo(root: Path, git: bool = False) -> Path:
    for rel, text in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    if git:
        _git(root, "init", "-q")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "init")
    return root


def use_fake(repo: Path, table: dict, *, languages=("typescript", "java"), name: str = "table.json") -> Path:
    """Point lsp.servers in the project's own config at the fake server answering from ``table``."""
    tp = repo / ".verinoda" / name
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(json.dumps(table), encoding="utf-8")
    cfg_path = repo / ".verinoda" / "config.json"
    cfg = json.loads(cfg_path.read_text("utf-8")) if cfg_path.is_file() else {}
    cfg["lsp"] = {"servers": {lang: [sys.executable, str(FAKE), str(tp)] for lang in languages}}
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    return tp


def alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True,
                             text=True).stdout
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def gone(pid: int, wait: float = 5.0) -> bool:
    deadline = time.monotonic() + wait
    while alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "proj")


# -- the client --------------------------------------------------------------------------------------------

def test_frames_are_read_and_anything_else_is_garbage():
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": None}).encode()
    s = io.BytesIO(b"Content-Length: %d\r\nContent-Type: application/vscode-jsonrpc\r\n\r\n" % len(body) + body)
    assert lsp._read_message(s) == {"jsonrpc": "2.0", "id": 1, "result": None}
    assert lsp._read_message(s) is None   # end of stream
    for bad in (b"hello world\r\n\r\n", b"Content-Length: x\r\n\r\n{}", b"\r\n{}",
                b"Content-Length: 2\r\n\r\n[]", b"Content-Length: 3\r\n\r\n{]}", b"A" * 9000):
        with pytest.raises(lsp._Garbage):
            lsp._read_message(io.BytesIO(bad))


def test_utf16_columns_and_call_tokens():
    assert lsp.utf16_col("a\U0001F600b", 2) == 3 and lsp.char_col("a\U0001F600b", 3) == 2
    calls, other = lsp.token_cols("Util u = new Util(); u.helper (1); helper; foo.helper?.()", "Util")
    assert calls == [13] and other == [0]
    calls, other = lsp.token_cols("u.helper (1); x = helper; foo.helper?.(); helperX()", "helper")
    assert calls == [2, 30] and other == [18]


def test_definition_references_hover_and_diagnostics(repo):
    table = {**TABLE, "log": str(repo / "log.txt")}
    use_fake(repo, table)
    with lsp.Session(repo) as s:
        d = lsp.navigate(s, "definition", "src/app.ts", 4, name="helper")
        r = lsp.navigate(s, "references", "src/lib.ts", 1, 17)
        h = lsp.navigate(s, "hover", "src/app.ts", 4, name="helper")
        client = s.clients["typescript"]
    assert d["status"] == "answered" and d["server"] == "fake-ls 1.2.3" and d["site"] == "src/app.ts:4:13"
    assert d["definitions"] == [{"path": "src/lib.ts", "line": 1, "col": 17, "in_repo": True}]
    assert [(x["path"], x["line"]) for x in r["references"]] == [("src/app.ts", 1), ("src/app.ts", 4)]
    assert h["hover"] == "function helper(n: number): number"
    # the server's publishDiagnostics for the opened file come with the answers
    assert h["diagnostics"] == [{"line": 5, "severity": "warning", "message": "unused value"}]
    log = (repo / "log.txt").read_text("utf-8").split()
    # initialize, initialized; the server's workspace/configuration request was answered; each file opened once
    assert log[:2] == ["initialize", "initialized"] and "response" in log
    assert log.count("textDocument/didOpen") == 2 and log[-2:] == ["shutdown", "exit"]
    assert client.proc.poll() == 0   # it exited on `exit`


def test_call_hierarchy_and_type_hierarchy_when_not_advertised(repo):
    caps = {"definitionProvider": True, "callHierarchyProvider": True}
    use_fake(repo, {**TABLE, "capabilities": caps})
    with lsp.Session(repo) as s:
        c = lsp.navigate(s, "calls", "src/app.ts", 3, name="run")
        t = lsp.navigate(s, "types", "src/app.ts", 3, name="run")
        i = lsp.navigate(s, "implementations", "src/app.ts", 3, name="run")
    assert c["status"] == "answered" and c["item"]["name"] == "run"
    assert [(x["name"], x["path"], x["line"], x["at_lines"]) for x in c["outgoing"]] == [
        ("helper", "src/lib.ts", 1, [4]), ("other", "src/lib.ts", 5, [5])]
    assert c["incoming"] == []
    assert t["status"] == "unknown" and "does not advertise typeHierarchyProvider" in t["reason"]
    assert i["status"] == "unknown" and "implementationProvider" in i["reason"]


def test_type_hierarchy_and_implementations_when_advertised(repo):
    item = {"name": "Api", "path": "src/api.ts", "line": 1, "col": 18}
    table = {**TABLE, "types": {"src/api.ts:1:18": {"item": item, "supertypes": [],
                                                    "subtypes": [{"name": "Impl", **OTHER}]}},
             "implementation": {"src/api.ts:2:3": [OTHER]}}
    use_fake(repo, table)
    with lsp.Session(repo) as s:
        t = lsp.navigate(s, "types", "src/api.ts", 1, name="Api")
        i = lsp.navigate(s, "implementations", "src/api.ts", 2, name="other")
    assert t["status"] == "answered" and [x["name"] for x in t["subtypes"]] == ["Impl"]
    assert [(x["path"], x["line"]) for x in i["implementations"]] == [("src/lib.ts", 5)]


def test_resolve_call_confirms_refutes_and_defers_to_implementations(repo):
    table = {**TABLE, "definition": {**TABLE["definition"], "src/app.ts:5:14": [{"path": "src/api.ts", "line": 2,
                                                                                 "col": 3}]},
             "implementation": {"src/api.ts:2:3": [OTHER]}}
    use_fake(repo, table)
    with lsp.Session(repo) as s:
        ok = lsp.resolve_call(s, "src/app.ts", 4, "helper()", target_path="src/lib.ts", target_line=1)
        bad = lsp.resolve_call(s, "src/app.ts", 4, "helper()", target_path="src/alt.ts", target_line=1)
        dyn = lsp.resolve_call(s, "src/app.ts", 5, "other()", target_path="src/lib.ts", target_line=5)
        java = lsp.resolve_call(s, "src/main/java/demo/Main.java", 6, ".helper()",
                                target_path="src/main/java/demo/Util.java", target_line=4)   # the annotation line
        none = lsp.resolve_call(s, "src/app.ts", 4, "nothere()", target_path="src/lib.ts", target_line=1)
        again = lsp.resolve_call(s, "src/app.ts", 4, "helper()", target_path="src/lib.ts", target_line=1)
        missing = lsp.navigate(s, "hover", "src/app.ts", 4, name="nothere")
        assert missing["status"] == "unknown" and s.status["typescript"]["status"] == "ready"   # not the server's fault
    assert (ok["kind"], ok["verdict"], ok["tool"]) == ("definitive", "confirms", "fake-ls 1.2.3")
    assert (bad["kind"], bad["verdict"]) == ("definitive", "refutes")
    # the definition is an interface method and the graph's target one of its implementations: never a refutation
    assert (dyn["kind"], dyn["verdict"]) == ("dynamic", "undetermined") and "implementations" in dyn["reason"]
    assert java["verdict"] == "confirms"   # the node's line is the annotation above the name
    assert none["kind"] == "unresolved" and "no call to 'nothere'" in none["reason"]
    assert again["cached"] is True


# -- failures: unknown with the reason, nothing left running ---------------------------------------------

@pytest.mark.parametrize("mode, why", [("crash", "the server exited (exit code 7)"),
                                       ("garbage", "output that is not LSP"),
                                       ("hang", "no answer to textDocument/definition within 1 s")])
def test_crash_hang_and_garbage_are_unknown_and_leave_no_process(repo, mode, why):
    pid = repo / "pid.txt"
    use_fake(repo, {**TABLE, "mode": mode, "pidfile": str(pid), "child": mode == "hang"})
    with lsp.Session(repo, timeout=1) as s:
        d = lsp.navigate(s, "definition", "src/app.ts", 4, name="helper")
        again = lsp.navigate(s, "hover", "src/app.ts", 4, name="helper")
        res = lsp.resolve_call(s, "src/app.ts", 4, "helper()", target_path="src/lib.ts", target_line=1)
        status = dict(s.status["typescript"])
    assert d["status"] == "unknown" and why in d["reason"], d
    assert again["status"] == "unknown"   # a broken server is not asked again within the command
    assert res["failed"] and "verdict" not in res
    assert status["status"] == "failed" and status["next_step"]
    assert gone(int(pid.read_text()))
    if mode == "hang":   # the server ignored shutdown and exit: its process tree was stopped
        assert gone(int(Path(str(pid) + ".child").read_text()))


@pytest.mark.parametrize("mode", ["crash_on_init", "hang_on_init"])
def test_a_server_that_does_not_start_is_unknown(repo, mode):
    pid = repo / "pid.txt"
    use_fake(repo, {**TABLE, "mode": mode, "pidfile": str(pid)})
    with lsp.Session(repo, start_timeout=1) as s:
        d = lsp.navigate(s, "definition", "src/app.ts", 4, name="helper")
        d2 = lsp.navigate(s, "definition", "src/app.ts", 4, name="helper")
    assert d["status"] == d2["status"] == "unknown"
    assert ("exited" if mode == "crash_on_init" else "no answer to initialize") in d["reason"]
    assert d2["reason"] == d["reason"]   # not started a second time
    assert gone(int(pid.read_text()))


def test_untrusted_project_starts_no_server(repo, monkeypatch):
    pid = repo / "pid.txt"
    use_fake(repo, {**TABLE, "pidfile": str(pid)})
    monkeypatch.setattr(paths, "is_trusted", lambda r: False)
    with lsp.Session(repo) as s:
        d = lsp.navigate(s, "definition", "src/app.ts", 4, name="helper")
        status = s.status["typescript"]
    assert d["status"] == "unknown" and d["reason"] == lsp.UNTRUSTED
    assert "verinoda trust" in d["next_step"] and "never run it for them" in d["next_step"]
    assert status["status"] == "not_run"
    time.sleep(0.3)
    assert not pid.exists()
    # and the project's own config cannot name a server while it is untrusted
    assert "lsp.servers" in paths.ignored_repo_settings(repo)
    assert "lsp" not in paths.load_config(repo)


def test_no_server_on_path_is_unknown_with_the_next_step(repo, monkeypatch, tmp_path):
    empty = tmp_path / "pathbin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with lsp.Session(repo) as s:
        ts = lsp.navigate(s, "definition", "src/app.ts", 4, name="helper")
        java = lsp.resolve_call(s, "src/main/java/demo/Main.java", 6, ".helper()")
    assert ts["status"] == "unknown"
    assert "typescript-language-server (the default) was not found on PATH" in ts["reason"]
    assert "npm install -g typescript-language-server" in ts["next_step"] and "never installs" in ts["next_step"]
    assert java["failed"] and "jdtls" in java["reason"] and "jdtls" in java["next_step"]
    unknown = lsp.navigate(lsp.Session(repo), "definition", "README.md", 1)
    assert unknown["status"] == "unknown" and "no language server is known" in unknown["reason"]


def test_the_default_server_is_found_on_path(repo, monkeypatch, tmp_path):
    binp = tmp_path / "pathbin"
    binp.mkdir()
    tp = binp / "t.json"
    tp.write_text(json.dumps(TABLE), encoding="utf-8")
    (binp / "fake_lsp.py").write_bytes(FAKE.read_bytes())
    if os.name == "nt":   # a launcher like npm's: a .cmd file next to the program
        monkeypatch.setenv("VERINODA_FAKE_PY", sys.executable)
        (binp / "typescript-language-server.cmd").write_text(
            '@"%VERINODA_FAKE_PY%" "%~dp0fake_lsp.py" "%~dp0t.json" %*\r\n', encoding="ascii")
    else:
        exe = binp / "typescript-language-server"
        exe.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{FAKE}' '{tp}' \"$@\"\n", encoding="utf-8")
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(binp))
    argv, why, _ = lsp.find_server(repo, "typescript")
    assert why == "" and Path(argv[0]).parent == binp and argv[1:] == ["--stdio"]
    with lsp.Session(repo) as s:
        d = lsp.navigate(s, "definition", "src/app.ts", 4, name="helper")
        client = s.clients["typescript"]
    assert d["status"] == "answered" and d["definitions"][0]["line"] == 1
    assert client.proc.poll() is not None


# -- graph edges through the claim machinery --------------------------------------------------------------

@pytest.fixture(scope="module")
def scanned(tmp_path_factory):
    from verinoda import workflow
    from verinoda.store import open_store

    repo = make_repo(tmp_path_factory.mktemp("lspedges") / "proj", git=True)
    workflow.init(repo)
    st = open_store(repo)
    workflow.scan(st, repo)
    st.close()
    return repo


def _evidence(repo, cid):
    from verinoda.claims import Claims
    from verinoda.store import open_store

    st = open_store(repo)
    try:
        return Claims(st, repo).evidence(cid)
    finally:
        st.close()


def test_confirmed_typescript_and_java_edges_are_statically_verified(scanned):
    from verinoda.store import open_store

    use_fake(scanned, TABLE)
    st = open_store(scanned)
    try:
        res = lsp.verify_edges(st, scanned, paths=["src"])
    finally:
        st.close()
    rows = {(r["at"], r["token"]): r for r in res["results"]}
    for key in (("src/app.ts:4", "helper"), ("src/app.ts:5", "other"), ("src/main/java/demo/Main.java:6", "helper")):
        r = rows[key]
        assert (r["verdict"], r["status"], r["server"]) == ("confirms", "statically_verified", "fake-ls 1.2.3"), r
        ev = [e for e in _evidence(scanned, r["claim"]) if e["source_type"] == "static_resolution"]
        assert ev and ev[0]["relation"] == "supports" and ev[0]["meta"]["tool"] == "fake-ls 1.2.3"
        assert ev[0]["meta"]["kind"] == "definitive" and ev[0]["meta"]["verdict"] == "confirms"
    # `new Util()`: the fake has no answer -> no definition, the claim stays an inference
    assert rows[("src/main/java/demo/Main.java:5", "Util")]["status"] != "statically_verified"
    assert res["servers"][0]["server"] == "fake-ls" and res["unknown"] == 0


def test_a_disagreeing_server_contradicts_the_edge_naming_both_locations(scanned, capsys):
    table = {**TABLE, "definition": {**TABLE["definition"], "src/app.ts:5:14": [{"path": "src/alt.ts", "line": 1,
                                                                                 "col": 17}]}}
    use_fake(scanned, table, name="t2.json")
    rc = cli.main(["lsp", "verify", "src/app.ts", "--repo", str(scanned), "--json"])
    res = json.loads(capsys.readouterr().out)
    assert rc == 1
    r = next(x for x in res["results"] if x["token"] == "other")
    # the same claim text was recorded by the confirming run: it is reused, and the server's refutation settles it
    assert (r["verdict"], r["status"]) == ("refutes", "contradicted")
    assert r["definition"] == ["src/alt.ts:1"] and r["target_at"] == "src/lib.ts:5"
    ev = [e for e in _evidence(scanned, r["claim"]) if e["source_type"] == "static_resolution"]
    assert any(e["relation"] == "refutes" and e["meta"]["strength"] == "definitive" for e in ev)


def test_a_fresh_contradiction_names_both_locations(tmp_path):
    from verinoda import workflow
    from verinoda.store import open_store

    repo = make_repo(tmp_path / "proj", git=True)
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
        # without a server the TypeScript call line is an inference: the binding is checked for Python only
        from verinoda import analysis, index

        g, snap = index.load(repo), st.latest_snapshot()
        rec = analysis._Recorder(st, repo, snap, "ana_plain", analysis.Budget())
        u, v, d = next((u, v, d) for u, v, d in g.edges({"calls"}) if d.get("source_location") == "L4"
                       and d.get("source_file") == "src/app.ts")
        plain = analysis._edge_claim(rec, g, u, v, d, snap["commit_sha"])
        assert plain["status"] == "strong_inference"
        assert any("checked for Python only" in x for x in plain["uncertainties"])
        use_fake(repo, {**TABLE, "definition": {"src/app.ts:5:14": [{"path": "src/alt.ts", "line": 1, "col": 17}]}},
                 languages=("typescript",))
        res = lsp.verify_edges(st, repo, paths=["src/app.ts"])
    finally:
        st.close()
    r = next(x for x in res["results"] if x["token"] == "other")
    assert r["status"] == "contradicted" and "reused" not in r
    assert "src/alt.ts:1" in r["uncertainty"] and "src/lib.ts:5" in r["uncertainty"]
    assert r["uncertainty"].startswith("fake-ls 1.2.3: the call at src/app.ts:5 binds to")


def test_verify_with_a_crashing_server_records_no_claims(scanned):
    from verinoda.store import open_store

    pid = scanned / "pid-crash.txt"
    use_fake(scanned, {**TABLE, "mode": "crash", "pidfile": str(pid)}, name="t3.json")
    st = open_store(scanned)
    try:
        res = lsp.verify_edges(st, scanned, paths=["src/app.ts"])
    finally:
        st.close()
    assert res["status"] == "unknown" and res["results"] == [] and res["unknown"] == 2
    assert all("exited" in u["reason"] for u in res["unanswered"])
    assert gone(int(pid.read_text()))


# -- the command line ---------------------------------------------------------------------------------------

def test_cli_lsp_and_resolve_call(repo, capsys):
    use_fake(repo, TABLE)
    assert cli.main(["lsp", "definition", "src/app.ts:4", "helper", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "lsp definition src/app.ts:4:13 helper (fake-ls 1.2.3" in out and "src/lib.ts:1:17" in out
    assert cli.main(["lsp", "calls", "src/app.ts:3:17", "--repo", str(repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["outgoing"][0]["name"] == "helper"
    assert cli.main(["resolve-call", "src/app.ts:4", "helper", "--target", "src/lib.ts:1", "--lsp",
                     "--repo", str(repo)]) == 0
    assert "definitive (fake-ls 1.2.3)  verdict: confirms" in capsys.readouterr().out
    assert cli.main(["lsp", "hover", "src/app.ts:4", "nothere", "--repo", str(repo)]) == 3
    assert "no 'nothere' on src/app.ts:4" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["lsp", "hover", "src/app.ts:4", "--timeout", "0", "--repo", str(repo)])
