"""doctor: install-layout warning for sandboxed agents, no secret values in output, and the
round-3 state (search index, receiver sidecar, lexicon, precise, tracer, SCIP, reference network)."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import hashlib  # noqa: E402
import json  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

from verinoda import doctor  # noqa: E402


def test_install_layout_reports_booleans():
    lay = doctor.install_layout()
    assert set(lay) == {"path", "hardlinked", "editable"}
    assert isinstance(lay["hardlinked"], bool) and isinstance(lay["editable"], bool)


def test_hardlinked_install_warns_about_sandboxes(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "install_layout",
                        lambda: {"path": "x", "hardlinked": True, "editable": False})
    res = doctor.run(tmp_path)
    warn = [c for c in res["checks"] if c["check"] == "sandbox_readable"]
    assert warn and warn[0]["level"] == "warn" and "--link-mode copy" in warn[0]["detail"]
    assert res["ok"]  # a warning, not a failure


def test_copy_install_has_no_sandbox_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "install_layout",
                        lambda: {"path": "x", "hardlinked": False, "editable": False})
    res = doctor.run(tmp_path)
    assert not [c for c in res["checks"] if c["check"] == "sandbox_readable"]


def test_secret_values_never_appear(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-SECRET-VALUE-123")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_SECRET-TOKEN-456")
    res = doctor.run(tmp_path)
    assert res["env"]["ANTHROPIC_API_KEY"] == "set" and res["env"]["GITHUB_TOKEN"] == "set"
    assert "SECRET-VALUE" not in json.dumps(res) and "SECRET-TOKEN" not in json.dumps(res)
    assert os.environ["ANTHROPIC_API_KEY"]  # untouched


# -- round 3 --------------------------------------------------------------------------------------

def _checks(res: dict) -> dict:
    return {c["check"]: c for c in res["checks"]}


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _ld(num: int, payload) -> bytes:
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    return _varint(num << 3 | 2) + _varint(len(data)) + data


def _scip(paths: list[str]) -> bytes:
    """scip.Index with tool info and one definition occurrence per document."""
    out = _ld(1, _ld(2, _ld(1, "scip-test") + _ld(2, "0.1")))
    for p in paths:
        occ = _ld(1, b"".join(_varint(x) for x in (0, 4, 7))) + _ld(2, f"scip-test x 1 `{p}`/f().") + \
            _varint(3 << 3) + _varint(1)
        out += _ld(2, _ld(1, p) + _ld(2, occ))
    return out


def test_uninitialised_directory_is_reported_without_creating_state(tmp_path):
    (tmp_path / "a.py").write_bytes(b"def f():\n    return 1\n")
    time.sleep(0.05)
    (tmp_path / "index.scip").write_bytes(_scip(["a.py", "missing.py"]))
    res = doctor.run(tmp_path)
    assert not (tmp_path / ".verinoda").exists()  # doctor never creates project state
    checks = _checks(res)
    assert {"lexicon", "precise", "tracer", "scip", "reference_network", "packaging"} <= set(checks)
    assert "search_index" not in checks  # no graph, nothing to index
    scip = res["project"]["scip"]
    assert (scip["documents"], scip["fresh"], scip["fresh_share"]) == (2, 1, 0.5) and scip["tool"] == "scip-test 0.1"
    assert checks["scip"]["level"] == "info" and "1/2 documents fresh" in checks["scip"]["detail"]
    lex = res["project"]["lexicon"]
    assert lex["exists"] is False and lex["seed_entries"] > 100
    assert f"seed dictionary only ({lex['seed_entries']} entries)" in checks["lexicon"]["detail"]
    assert res["ok"] is True


def test_unreadable_scip_index_is_a_warning(tmp_path):
    (tmp_path / "index.scip").write_bytes(b"garbage-not-scip")
    res = doctor.run(tmp_path)
    c = _checks(res)["scip"]
    assert c["level"] == "warn" and "unreadable" in c["detail"] and res["ok"] is True


def test_reference_network_cache_and_blocked_hosts(tmp_path):
    atlas = tmp_path / ".verinoda"
    cache = atlas / "research" / "http-cache"
    cache.mkdir(parents=True)
    (atlas / "config.json").write_bytes(b'{"research": {"network": "off"}}')
    (cache / "k1.json").write_bytes(b'{"url": "https://pypi.org/x"}')
    (cache / "k2.json").write_bytes(b'{"url": "https://api.github.com/y"}')
    now = time.time()
    (cache / "hosts.json").write_bytes(json.dumps({"blocked_until": {"api.github.com": now + 3600,
                                                                     "old.example": now - 10}}).encode("utf-8"))
    res = doctor.run(tmp_path)
    ref = res["project"]["references"]
    assert ref["network"] == "off" and ref["http_cache"]["entries"] == 2 and ref["http_cache"]["size_bytes"] > 0
    assert list(ref["blocked_hosts"]) == ["api.github.com"]  # an expired block is not reported
    checks = _checks(res)
    assert "research.network = off" in checks["reference_network"]["detail"]
    assert "HTTP cache 2 entries" in checks["reference_network"]["detail"]
    hosts = checks["reference_hosts"]
    assert hosts["level"] == "warn" and "api.github.com" in hosts["detail"] and "old.example" not in hosts["detail"]
    assert res["ok"] is True  # a warning, not a failure


def test_missing_optional_resolvers_are_info_not_errors(tmp_path, monkeypatch):
    import importlib.util

    from verinoda import experiments, precise

    real_find_spec, real_run = importlib.util.find_spec, subprocess.run
    fake_py = str(tmp_path / "venv" / "python.exe")

    def find_spec(name, *a, **k):
        return None if name == "packaging" else real_find_spec(name, *a, **k)

    def run(cmd, *a, **k):
        if cmd and cmd[0] == fake_py:
            return subprocess.CompletedProcess(cmd, 0, "False 3.11.9\n", "")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(experiments, "python_for", lambda repo: fake_py)
    monkeypatch.setattr(precise, "available",
                        lambda: (False, "jedi is not installed (pip install 'verinoda[precise]')"))
    res = doctor.run(tmp_path)
    checks = _checks(res)
    assert checks["precise"]["level"] == "info" and "pip install 'verinoda[precise]'" in checks["precise"]["detail"]
    assert checks["packaging"]["level"] == "info" and "numeric parser" in checks["packaging"]["detail"]
    tr = checks["tracer"]
    assert tr["level"] == "info" and "no sys.monitoring" in tr["detail"] and "setprofile" in tr["detail"]
    assert "3.11.9" in tr["detail"] and res["project"]["tracer"]["sys_monitoring"] is False
    assert res["project"]["precise"] == {"available": False,
                                         "detail": "jedi is not installed (pip install 'verinoda[precise]')"}
    assert res["project"]["references"]["packaging"] is False
    assert res["ok"] is True


def _graph(repo: Path) -> Path:
    from verinoda.paths import graph_path

    gp = graph_path(repo)
    gp.parent.mkdir(parents=True, exist_ok=True)
    gp.write_bytes(b'{"nodes": [], "links": []}')
    return gp


def _search_db(repo: Path, meta: dict, files: list[tuple]) -> Path:
    from verinoda.paths import search_db_path

    db = search_db_path(repo)
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE files (file TEXT PRIMARY KEY, sha256 TEXT, gsig TEXT, size INTEGER, mtime_ns INTEGER)")
    conn.executemany("INSERT INTO meta VALUES (?, ?)", [(k, json.dumps(v)) for k, v in meta.items()])
    conn.executemany("INSERT INTO files (file, sha256, size, mtime_ns) VALUES (?, ?, ?, ?)", files)
    conn.commit()
    conn.close()
    return db


def test_search_index_versions_graph_and_stale_files(tmp_path):
    from verinoda import search_index
    from verinoda.index import graph_identity

    gp = _graph(tmp_path)
    (tmp_path / "a.py").write_bytes(b"x = 1\n")
    (tmp_path / "b.py").write_bytes(b"y = 2  # edited after indexing\n")
    st_a = (tmp_path / "a.py").stat()
    rows = [("a.py", hashlib.sha256(b"x = 1\n").hexdigest(), st_a.st_size, st_a.st_mtime_ns),
            ("b.py", hashlib.sha256(b"y = 2\n").hexdigest(), 6, 1),
            ("c.py", "0" * 64, 3, 1)]  # deleted since
    _search_db(tmp_path, {"schema_version": search_index.SCHEMA_VERSION + 1,
                          "tokenizer_version": search_index.TOKENIZER_VERSION,
                          "graph": {"size": 1, "mtime_ns": 1}, "generation": 3, "n_units": 7}, rows)
    checks: list = []
    info = doctor._search_index(tmp_path, gp, checks)
    assert (info["generation"], info["units"], info["files"]) == (3, 7, 3)
    assert info["versions_match"] is False and info["matches_graph"] is False
    assert info["stale_files"] == 2 and info["stale_sample"] == ["b.py", "c.py"]
    (c,) = checks
    assert c["level"] == "warn" and "built by another version" in c["detail"]
    assert "describes another graph.json" in c["detail"] and "2 file(s) changed since indexing" in c["detail"]

    (tmp_path / ".verinoda" / "index" / "search.db").unlink()
    _search_db(tmp_path, {"schema_version": search_index.SCHEMA_VERSION,
                          "tokenizer_version": search_index.TOKENIZER_VERSION,
                          "graph": graph_identity(gp), "generation": 4, "n_units": 7}, rows[:1])
    checks = []
    info = doctor._search_index(tmp_path, gp, checks)
    assert info["versions_match"] and info["matches_graph"] and info["stale_files"] == 0
    assert checks[0]["ok"] and checks[0]["detail"].startswith("generation 4, 7 units in 1 files")


def test_missing_search_index_and_receiver_sidecar_are_warnings(tmp_path):
    from verinoda.index import RECEIVER_SIDECAR_VERSION, graph_identity
    from verinoda.paths import receiver_calls_path

    gp = _graph(tmp_path)
    res = doctor.run(tmp_path)
    checks = _checks(res)
    assert checks["search_index"]["level"] == "warn" and "no search.db yet" in checks["search_index"]["detail"]
    assert checks["receiver_calls"]["level"] == "warn" and res["ok"] is True
    side = receiver_calls_path(tmp_path)
    side.write_bytes(json.dumps({"version": RECEIVER_SIDECAR_VERSION, "graph": graph_identity(gp),
                                 "edges": [["a", "b", {}]]}).encode("utf-8"))
    checks: list = []
    assert doctor._receiver_sidecar(tmp_path, gp, checks)["matches_graph"] is True and checks[0]["ok"]
    gp.write_bytes(b'{"nodes": [{"id": "n"}], "links": []}')  # the graph was rewritten
    checks = []
    assert doctor._receiver_sidecar(tmp_path, gp, checks)["matches_graph"] is False
    assert checks[0]["level"] == "warn" and "does not match graph.json" in checks[0]["detail"]


def test_lexicon_state(tmp_path):
    from verinoda import lexicon
    from verinoda.paths import lexicon_path

    p = lexicon_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(json.dumps({"version": lexicon.LEXICON_VERSION, "built_at": "2026-09-23T00:00:00+00:00",
                              "tree_hash": "abc", "pairs": {"siparis": [{"part": "order", "score": 12.0}]},
                              "vocab": {"order": 3, "total": 2}, "n_files": 2}).encode("utf-8"))
    checks: list = []
    info = doctor._lexicon(tmp_path, checks)
    assert info["exists"] and info["built_at"] == "2026-09-23T00:00:00+00:00" and info["tree_hash"] == "abc"
    assert (info["pairs"], info["vocab"]) == (1, 2) and info["size_bytes"] == p.stat().st_size
    assert checks[0]["ok"] and "1 learned pair(s)" in checks[0]["detail"]
    p.write_bytes(b'{"version": 999}')
    checks = []
    doctor._lexicon(tmp_path, checks)
    assert checks[0]["level"] == "warn" and "another version" in checks[0]["detail"]
