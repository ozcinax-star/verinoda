"""`verinoda extract`: the whole definition around a location."""

from __future__ import annotations

import json
import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, extract  # noqa: E402

ORDERS = '''"""Orders."""
import os

LIMIT = {
    "a": 1,
}


class Service:
    """Places orders."""

    @staticmethod
    def place(order):
        total = 0
        for line in order:
            total += line

        def helper():
            return total

        return helper()

    def cancel(self, order_id):
        return order_id


def place(order):
    return Service.place(order)
'''

JAVA = """package com.example.orders;

public final class Store {
    public static int save(int id) {
        int x = id + 1;
        return x;
    }

    static final class Cache {
        void clear() {
            int y = 0;
        }
    }
}
"""

BROKEN_TS = """export function ok(a: number): number {
    return a + 1;
}

export function broken(a: number {
    return a +;
}
"""

DOC = """# Guide

Intro.

## Install

Run it.
Then use it.
"""


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("extract") / "proj"
    files = {"src/orders.py": ORDERS, "mod/src/main/java/com/example/orders/Store.java": JAVA,
             "web/broken.ts": BROKEN_TS, "docs/GUIDE.md": DOC, "shaders/a.glsl": "void main() {\n}\n"}
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    return root


def _one(repo: Path, target: str, **kw) -> dict:
    res = extract.run(repo, [target], cwd=repo, **kw)
    assert len(res["results"]) == 1, res
    return res["results"][0]


def test_a_line_gives_the_innermost_enclosing_function_whole(repo):
    r = _one(repo, "src/orders.py:15")
    d = r["definition"]
    assert r["status"] == "found" and d["name"] == "Service.place" and d["kind"] == "def"
    # the decorator belongs to the definition; the nested helper is part of it
    assert (d["start"], d["end"]) == (12, 21) and d["inside"] == ["Service"]
    assert r["text"].splitlines()[0] == "    @staticmethod" and "return helper()" in r["text"]
    c = r["claim"]
    assert c["status"] == "statically_verified" and c["text"] == \
        "src/orders.py:15 is inside function Service.place (src/orders.py:12-21)"
    ev = c["evidence"][0]
    assert ev["locator"] == "src/orders.py:12-21" and ev["content_hash"].startswith("sha256:")


def test_nested_definition_class_and_range(repo):
    assert _one(repo, "src/orders.py:19")["definition"]["name"] == "Service.place.helper"
    cls = _one(repo, "src/orders.py:10")["definition"]
    assert (cls["name"], cls["kind"], cls["start"], cls["end"]) == ("Service", "class", 9, 24)
    # a range spanning two methods: the class that holds both
    assert _one(repo, "src/orders.py:14-23")["definition"]["name"] == "Service"
    # a column is ignored, as a compiler prints it
    assert _one(repo, "src/orders.py:24:9")["definition"]["name"] == "Service.cancel"


def test_top_level_statement_blank_line_and_past_the_end(repo):
    st = _one(repo, "src/orders.py:5")
    assert st["status"] == "found" and st["definition"]["kind"] == "statement"
    assert st["definition"]["name"] == "LIMIT" and (st["definition"]["start"], st["definition"]["end"]) == (4, 6)
    assert _one(repo, "src/orders.py:7")["status"] == "no_definition"
    past = _one(repo, "src/orders.py:999")
    assert past["status"] == "not_found" and "lines" in past["note"]


def test_a_symbol_by_name(repo):
    r = _one(repo, "src/orders.py#cancel")
    assert r["status"] == "found" and r["definition"]["name"] == "Service.cancel"
    assert r["claim"]["text"] == "Service.cancel is a function at src/orders.py:23-24"
    assert _one(repo, "src/orders.py::Service.place")["definition"]["start"] == 12
    amb = _one(repo, "src/orders.py#place")  # the method and the module function
    assert amb["status"] == "ambiguous" and len(amb["candidates"]) == 2
    assert _one(repo, "src/orders.py#nothing")["status"] == "not_found"


def test_java_method_and_inner_class(repo):
    r = _one(repo, "mod/src/main/java/com/example/orders/Store.java:5")
    assert r["definition"]["name"] == "Store.save" and r["claim"]["status"] == "statically_verified"
    assert _one(repo, "mod/src/main/java/com/example/orders/Store.java:11")["definition"]["name"] == \
        "Store.Cache.clear"


def test_a_path_from_another_directory_is_found_by_its_end(repo):
    # javac run in mod/ prints paths relative to it
    r = _one(repo, "src/main/java/com/example/orders/Store.java:5")
    assert r["status"] == "found" and r["file"] == "mod/src/main/java/com/example/orders/Store.java"
    assert _one(repo, "orders.py:24")["file"] == "src/orders.py"


def test_a_parse_the_tree_recovered_from_is_strong_inference(repo):
    ok = _one(repo, "web/broken.ts:2")
    assert ok["definition"]["name"] == "ok"
    assert ok["claim"]["status"] == "strong_inference" and ok["claim"]["uncertainties"]


def test_markdown_section_and_unsupported_language(repo):
    sec = _one(repo, "docs/GUIDE.md:7")
    assert sec["definition"] == {"name": "Install", "kind": "section", "start": 5, "end": 8, "inside": ["Guide"]}
    un = _one(repo, "shaders/a.glsl:1")
    assert un["status"] == "unsupported" and ".glsl" in un["note"]


def test_locations_in_compiler_and_test_output():
    text = "\n".join([
        'Traceback (most recent call last):',
        '  File "src/orders.py", line 15, in place',
        'src/orders.py:24:9: E501 line too long',
        'web/broken.ts(5,33): error TS1005: \',\' expected.',
        '[ERROR] /w/mod/src/main/java/com/example/orders/Store.java:[5,20] cannot find symbol',
        '\tat com.example.orders.Store$Cache.clear(Store.java:11)',
        '  File "src/orders.py", line 15, in place',   # the same location again: once
        'at 12:30:45 nothing happened; see http://example.com',
    ])
    got = [(x["path"], x["line"]) for x in extract.locations(text)]
    assert got == [("src/orders.py", 15), ("src/orders.py", 24), ("web/broken.ts", 5),
                   ("/w/mod/src/main/java/com/example/orders/Store.java", 5),
                   ("com/example/orders/Store.java", 11)]


def test_output_locations_outside_the_project_are_counted_not_listed(repo):
    text = ('  File "/usr/lib/python3.12/json/decoder.py", line 3, in decode\n'
            '  File "src/orders.py", line 15, in place\n'
            '\tat java.lang.Thread.run(Thread.java:833)\n'
            '\tat com.example.orders.Store$Cache.clear(Store.java:11)\n')
    res = extract.run(repo, [], output=text, cwd=repo)
    names = [r["definition"]["name"] for r in res["results"]]
    assert res["status"] == "found" and names == ["Service.place", "Store.Cache.clear"]
    assert res["not_in_project"] == 2
    none = extract.run(repo, [], output="all good\n", cwd=repo)
    assert none["status"] == "not_found" and none["note"]
    cut = extract.run(repo, ["src/orders.py:13", "src/orders.py:24", "src/orders.py:28"], cwd=repo, limit=2)
    assert len(cut["results"]) == 2 and cut["truncated"] and cut["locations_left"] == 1
    assert "(+1 more locations, not read: --limit)" in extract.render(cut)


def test_parse_target_forms():
    assert extract.parse_target("a/b.py:40") == {"path": "a/b.py", "line": 40, "end": 40}
    assert extract.parse_target("a/b.py:40-45") == {"path": "a/b.py", "line": 40, "end": 45}
    assert extract.parse_target("C:\\w\\b.py:7:3") == {"path": "C:\\w\\b.py", "line": 7, "end": 7}
    assert extract.parse_target("a/b.py#C.m") == {"path": "a/b.py", "symbol": "C.m"}
    assert extract.parse_target("a/b.py::m") == {"path": "a/b.py", "symbol": "m"}
    assert extract.parse_target("no location") is None


def test_cli_prints_the_definition_with_line_numbers(repo, capsys):
    rc = cli.main(["extract", "src/orders.py:24", "--repo", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.splitlines()[0] == "src/orders.py:23-24 def Service.cancel (for src/orders.py:24)"
    assert "23     def cancel(self, order_id):" in out and "24         return order_id" in out
    rc = cli.main(["extract", "src/orders.py:12", "--repo", str(repo), "--no-numbers", "--max-lines", "2"])
    out = capsys.readouterr().out.splitlines()
    assert rc == 0 and out[1:3] == ["    @staticmethod", "    def place(order):"] and "10 lines" in out[3]


def test_cli_json_from_a_file_and_exit_codes(repo, tmp_path, capsys):
    log = tmp_path / "build.log"
    log.write_text("src/orders.py:24:9: E501\nsrc/generated.py:3: error\nsrc/orders.py:7: W391\n", encoding="utf-8")
    rc = cli.main(["extract", "--from", str(log), "--repo", str(repo), "--json"])
    res = json.loads(capsys.readouterr().out)
    # a file the project does not have is counted with the library frames; a line between definitions is reported
    assert rc == 2 and res["status"] == "partial" and res["not_in_project"] == 1
    assert [r["status"] for r in res["results"]] == ["found", "no_definition"]
    assert cli.main(["extract", "src/nothing.py:7", "--repo", str(repo)]) == 2
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli.main(["extract", "--repo", str(repo)])


def test_a_call_in_the_message_does_not_hide_the_location():
    text = "src/m.py:7: in b\nsrc/m.py:2: assert v.get(3) == 4\nweb/a.ts(3,1): error TS1: x.y(2)\n"
    got = [(x["path"], x["line"]) for x in extract.locations(text)]
    assert got == [("src/m.py", 7), ("src/m.py", 2), ("web/a.ts", 3), ("x.y", 2)]


def test_a_long_line_without_spaces_is_scanned_in_linear_time():
    import time

    t = time.perf_counter()
    assert extract.locations("a=b.c;d.e=f.g;" * 4000) == []
    assert extract.locations("a." * 8000) == []
    assert time.perf_counter() - t < 2.0


def test_an_absolute_path_with_a_space(tmp_path):
    root = tmp_path / "my proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "orders.py").write_text(ORDERS, encoding="utf-8", newline="\n")
    p = str(root / "src" / "orders.py")
    text = f"{p}:24:5: error: x\n  1>{p}(15,5): error CS1: y\n"
    res = extract.run(root, [], output=text, cwd=root)
    assert [r["definition"]["name"] for r in res["results"]] == ["Service.cancel", "Service.place"]
    assert res["results"][0]["input"] == f"{p}:24" and "not_in_project" not in res


def test_frames_outside_the_project_list_the_files_once(repo, monkeypatch):
    from verinoda import snapshot

    calls = []
    real = snapshot.listed_files
    monkeypatch.setattr(snapshot, "listed_files", lambda r: calls.append(r) or real(r))
    text = "\n".join(f"\tat java.lang.Thread{i}.run(Thread{i}.java:{i + 1})" for i in range(30))
    res = extract.run(repo, [], output=text, cwd=repo, limit=5)
    assert res["not_in_project"] == 30 and len(calls) == 1


def test_negative_max_lines_is_refused_and_a_reversed_range_is_kept(repo, capsys):
    with pytest.raises(SystemExit):
        cli.main(["extract", "src/orders.py:13", "--repo", str(repo), "--max-lines", "-1"])
    whole = _one(repo, "src/orders.py:13", max_lines=-1)
    assert not whole["truncated"] and whole["text"].count("\n") == 9
    assert extract.parse_target("a/b.py:7-2") == {"path": "a/b.py", "line": 2, "end": 7}
    assert _one(repo, "src/orders.py:23-14")["definition"]["name"] == "Service"


def test_a_form_feed_does_not_shift_the_printed_lines(tmp_path):
    (tmp_path / "ff.py").write_text("x = 1\n\n# page\x0cbreak\n\n\n\ndef g():\n    return 2\n", encoding="utf-8",
                                    newline="\n")
    r = _one(tmp_path, "ff.py:7")
    assert (r["definition"]["start"], r["definition"]["end"]) == (7, 8)
    assert r["text"] == "def g():\n    return 2"


def test_a_pytest_node_id(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "class TestA:\n    def test_b(self):\n        pass\n\n\ndef test_p(a):\n    pass\n", encoding="utf-8")
    assert _one(tmp_path, "tests/test_x.py::TestA::test_b")["definition"]["name"] == "TestA.test_b"
    assert _one(tmp_path, "tests/test_x.py::test_p[1-2]")["definition"]["name"] == "test_p"
