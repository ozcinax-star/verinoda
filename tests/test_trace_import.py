"""Sentry events and OpenTelemetry spans read from an exported file, their frames mapped onto the code."""

from __future__ import annotations

import json
import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, failsig, index, search_index, trace_import, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "trace_import"

FILES = {
    "shop/orders.py": """class Order:
    def __init__(self, qty):
        self.qty = qty

    def place(self):
        if self.qty <= 0:
            raise ValueError("bad qty")
        return self.qty


def checkout(order):
    return order.place()
""",
    "shop/util.py": "def helper():\n    return 1\n",
    "a/config.py": "def load():\n    return {}\n",
    "b/config.py": "def other():\n    return {}\n",
    "src/main/java/com/example/Foo.java": """package com.example;

public class Foo {
    void bar() {
        throw new IllegalStateException("closed");
    }
}
""",
}


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("traceimport") / "shop"
    for rel, text in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    workflow.init(root)
    st = open_store(root)
    try:
        workflow.scan(st, root)
    finally:
        st.close()
    search_index._HANDLES.clear()
    return root


def _analyze(repo: Path, name: str) -> dict:
    docs, skipped = trace_import.load((FIX / name).read_bytes(), name=name)
    return trace_import.analyze(repo, index.load(repo), docs, source=name, skipped=skipped)


def _by_ref(ev: dict) -> dict:
    return {r["ref"]: r for r in ev["frames"]}


def test_load_tells_an_export_from_a_log():
    assert trace_import.load(b"[14:02:08] [Server thread/INFO] guardtests.x passed!\n", name="latest.log") is None
    assert trace_import.load(b"java.lang.IllegalStateException: x\n", name="crash.txt") is None
    assert trace_import.load(b'{"a": 1}\n{"b": 2}\n', name="events.jsonl") == ([{"a": 1}, {"b": 2}], 0)
    # an export still being written: its cut last line is skipped and counted, the others kept
    assert trace_import.load(b'{"a": 1}\n{"b": 2}\n{"c": ', name="events.jsonl") == ([{"a": 1}, {"b": 2}], 1)
    assert trace_import.load('{"a": 1}'.encode("utf-16"), name="export.json") == ([{"a": 1}], 0)  # PowerShell's >
    with pytest.raises(ValueError, match=r"not valid JSON \(line 1, column"):
        trace_import.load(b'{"exception": {"values": [', name="event.json")
    with pytest.raises(ValueError, match="not valid JSON"):
        trace_import.load(b'{"exception": oops}', name="paste.txt")      # a { starts JSON whatever the name
    with pytest.raises(ValueError, match="does not start"):
        trace_import.load(b"Traceback (most recent call last):", name="event.json")
    with pytest.raises(ValueError, match="nested too deeply"):
        trace_import.load(b"[" * 100000 + b"]" * 100000, name="deep.json")
    # bytes that are not UTF-8 inside a string do not stop the reading
    assert trace_import.load(b'{"exception": "caf\xe9"}', name="e.json")[0][0]["exception"] == "caf\ufffd"


def test_path_resolver_names_every_file_of_a_shared_suffix():
    r = failsig.PathResolver(["a/config.py", "b/config.py", "shop/orders.py"])
    assert r.matches("config.py") == ["a/config.py", "b/config.py"]
    assert r.resolve("config.py") is None                              # resolve() is unchanged: no pick
    assert r.matches("/srv/app/config.py") == []                       # neither is a whole suffix of the other
    assert r.matches("C:\\Users\\dev\\shop\\orders.py") == ["shop/orders.py"]
    assert r.matches("/usr/lib/python3.12/site-packages/shop/orders.py") == []


def test_sentry_frames_are_mapped_or_said_why_not(repo):
    res = _analyze(repo, "sentry_events.json")
    assert res["format"] == "sentry"
    py, java = res["events"]
    assert py["id"] == "9f3c2a7d51e04b0c8a1d2e3f4a5b6c7d" and py["title"] == "ValueError: bad qty"
    f = _by_ref(py)
    win = f["x0.f1"]                                                  # a Windows absolute path
    assert (win["result"], win["path"], win["line"], win["in"], win["status"]) == \
        ("mapped", "shop/orders.py", 12, "checkout", "observed")
    assert (f["x0.f2"]["result"], f["x0.f2"]["in"]) == ("mapped", "Order.place")
    assert f["x0.f3"]["result"] == "stale" and "beyond the end of the file (2 lines)" in f["x0.f3"]["why"]
    assert f["x0.f4"]["result"] == "stale" and "inside `Order.__init__`, not in `place`" in f["x0.f4"]["why"]
    amb = f["x0.f5"]
    assert amb["result"] == "ambiguous" and amb["candidates"] == ["a/config.py", "b/config.py"]
    assert amb["fits"] == ["a/config.py"] and "status" not in amb
    assert f["x0.f6"]["result"] == "not_in_repo" and "file missing" in f["x0.f6"]["why"]
    assert f["x0.f7"]["result"] == "stale" and "recorded source line differs" in f["x0.f7"]["why"]
    assert "x0.f0" not in f and py["library"] == {"frames": 1, "first": "request", "last": "request"}
    jf = _by_ref(java)                                                # the API's `entries` form, a JVM frame
    assert java["id"] == "0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e"
    assert (jf["x0.f1"]["result"], jf["x0.f1"]["path"], jf["x0.f1"]["in"]) == \
        ("mapped", "src/main/java/com/example/Foo.java", "Foo.bar")
    assert res["counts"] == {"mapped": 3, "stale": 3, "ambiguous": 1, "not_in_repo": 1, "library": 2}
    text = trace_import.render(res)
    assert "observed  shop/orders.py:12 in checkout (x0.f1)" in text
    assert "ambiguous /srv/app/config.py:2 - 2 files end with that path; none is chosen" in text
    assert "what that event recorded, not what always happens" in text


def test_otel_span_attributes_and_exception_stacktrace(repo):
    res = _analyze(repo, "otel_trace.json")
    assert res["format"] == "otel"
    py, java = res["events"]
    assert py["id"] == "eee19b7ec3c1b174" and py["trace"] == "5b8efff798038103d269b633813fc60c"
    assert py["title"] == "checkout - ValueError: bad qty"
    f = _by_ref(py)
    assert (f["code"]["result"], f["code"]["line"], f["code"]["in"]) == ("mapped", 11, "checkout")
    assert [(f[k]["line"], f[k]["in"]) for k in ("ev0.f0", "ev0.f1")] == [(12, "checkout"), (7, "Order.place")]
    jf = _by_ref(java)
    assert jf["code"]["path"] == "src/main/java/com/example/Foo.java" and jf["code"]["result"] == "mapped"
    assert jf["code"]["path_from"] == "code.namespace as a JVM class"
    assert jf["ev0.f1"]["result"] == "mapped"                         # innermost last: Foo.bar after Thread.run
    assert java["library"]["frames"] == 1                             # java/lang/Thread.java is not in the repo


def test_cli_stores_one_observed_claim_per_mapped_frame(repo, capsys):
    path = FIX / "sentry_events.json"
    assert cli.main(["trace-log", str(path), "--repo", str(repo), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["claims"]) == out["counts"]["mapped"] == 3
    assert all("not what always happens" in c["text"] for c in out["claims"])
    assert all(c["status"] in ("observed", "strong_inference", "weak_inference", "unknown") for c in out["claims"])
    kept = list((repo / ".verinoda" / "logs").glob("sentry_events-*.frames.json"))
    assert len(kept) == 1
    body = kept[0].read_text(encoding="utf-8")
    assert "do-not-keep" not in body and "x0.f2" in body               # the frames, not the event's variables
    assert cli.main(["trace-log", str(path), "--repo", str(repo), "--no-store"]) == 0
    assert "stored" not in capsys.readouterr().out


def test_cli_refuses_a_broken_or_empty_export(repo, tmp_path, capsys):
    bad = tmp_path / "event.json"
    bad.write_bytes(b'{"exception": {"values": [}')
    with pytest.raises(SystemExit, match="event.json: not valid JSON"):
        cli.main(["trace-log", str(bad), "--repo", str(repo)])
    empty = tmp_path / "other.json"
    empty.write_text('{"spans": []}', encoding="utf-8")
    with pytest.raises(SystemExit, match="no Sentry event"):
        cli.main(["trace-log", str(empty), "--repo", str(repo)])
    log = tmp_path / "structured.log"                                 # JSON lines that are no export: a log
    log.write_text('{"level": "info", "msg": "up"}\n', encoding="utf-8")
    assert cli.main(["trace-log", str(log), "--repo", str(repo), "--no-store"]) == 2


def test_hostile_frames_are_not_mapped_by_accident(repo):
    """Frames from site-packages, a library class whose file name the project shares, a Windows path in another
    case, line numbers that are not lines, and an event with more frames than are read."""
    def frame(**kw):
        return {"in_app": None, **kw}

    frames = [
        frame(abs_path="C:\\Python312\\Lib\\site-packages\\shop\\orders.py", lineno=12, function="checkout"),
        frame(filename="Foo.java", module="org.lib.Foo", function="bar", lineno=5),   # not com.example.Foo
        frame(abs_path="C:\\Users\\Dev\\Shop\\Orders.py", lineno=12, function="checkout", in_app=True),
        frame(abs_path="/srv/app/shop/orders.py", lineno=0, function="checkout", in_app=True),
        frame(abs_path="/srv/app/shop/orders.py", lineno="12", function="Object.<anonymous>", in_app=True),
        frame(abs_path="/srv/app/shop/orders.py", lineno=True, function="checkout", in_app=True),
        "not a frame",
    ]
    ev = {"event_id": "e1", "exception": {"values": [{"type": "E", "stacktrace": {"frames": frames}}]}}
    res = trace_import.analyze(repo, index.load(repo), [ev], source="x.json")
    f = _by_ref(res["events"][0])
    assert "x0.f0" not in f and "x0.f1" not in f                      # folded with the library frames
    assert res["events"][0]["library"]["frames"] == 2
    assert (f["x0.f2"]["result"], f["x0.f2"]["path"]) == ("mapped", "shop/orders.py")
    assert f["x0.f3"]["result"] == "stale" and "not a line number" in f["x0.f3"]["why"]
    assert f["x0.f4"]["result"] == "mapped"                           # a string line; an anonymous name unchecked
    assert f["x0.f5"]["result"] == "stale" and f["x0.f5"]["why"] == "no line recorded"
    many = {"exception": {"values": [{"type": "E", "stacktrace": {"frames": [
        {"abs_path": "/srv/app/shop/orders.py", "lineno": 12, "function": "checkout"}] * 5000}}]}}
    big = trace_import.analyze(repo, index.load(repo), [many] * 3, source="big.json")
    assert [len(e["frames"]) for e in big["events"]] == [trace_import.MAX_FRAMES] * 3
    assert big["events"][0]["truncated"] and "innermost 200 frames read" in trace_import.render(big)
