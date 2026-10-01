"""Rationale comments (WHY/NOTE/HACK, decision record citations) attached to the code they explain."""

from __future__ import annotations

import json
import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, index, rationale, search_index, workflow  # noqa: E402
from verinoda.mcp.server import AtlasTools  # noqa: E402
from verinoda.store import open_store  # noqa: E402

CACHE = '''"""Cache of prices."""
import time

# NOTE: module-level, shared by every request


class PriceCache:
    # WHY: a dict, not lru_cache - entries expire by time, not by count
    #   (see docs/adr/0003-price-cache.md)
    def __init__(self):
        self.data = {}

    # HACK: callers still read .size
    @property
    def size(self):
        return len(self.data)

    def get(self, key):
        hit = self.data.get(key)
        # important: a stale entry counts as a miss
        if hit and hit[1] < time.time():
            return None
        url = "http://example.com/#NOTE: not a comment"
        return hit  # see ADR-7 for the expiry rule


def plain():
    # an ordinary comment
    return 1
'''

GUARD = """package com.example.guard;

public final class Guard {
    /**
     * Why: the body may not be loaded yet in its first ticks.
     * It is looked up again on the next tick.
     */
    @Override
    public Object body() {
        return null;
    }

    public void follow() {
        // NB: walks with the player (docs/DESIGN.md D50)
        int ticks = 12;
    }
}
"""


def _rows(rel, text, syms=None):
    return [(r["at"], r["tag"], r["attach"], r["symbol"]) for r in rationale.scan_text(rel, text, syms)]


def test_markers_blocks_and_attachment_in_python(tmp_path):
    (tmp_path / "cache.py").write_text(CACHE, encoding="utf-8")
    rows = rationale.file_rationale(tmp_path, "cache.py")
    got = [(r["at"], r["tag"], r["attach"], r["symbol"]) for r in rows]
    assert got == [
        ("cache.py:4", "NOTE", "module", None),
        ("cache.py:8-9", "WHY", "above", "PriceCache.__init__"),
        ("cache.py:13", "HACK", "above", "PriceCache.size"),          # decorator lines may come between
        ("cache.py:20", "IMPORTANT", "inside", "PriceCache.get"),
        ("cache.py:24", "ADR", "inside", "PriceCache.get"),             # a citation after code on the line
    ]
    why = rows[1]
    assert why["text"] == ("# WHY: a dict, not lru_cache - entries expire by time, not by count\n"
                           "#   (see docs/adr/0003-price-cache.md)")
    assert why["cites"] == ["docs/adr/0003-price-cache.md"] and why["status"] == "statically_verified"
    assert rows[4]["cites"] == ["ADR-7"]
    assert all("not a comment" not in r["text"] for r in rows)        # '#' inside a string is not a comment


def test_javadoc_and_line_comments_with_given_spans():
    syms = {"Guard": {"start": 3, "end": 17}, "Guard.body": {"start": 9, "end": 11},
            "Guard.follow": {"start": 13, "end": 16}}
    rows = rationale.scan_text("src/Guard.java", GUARD, syms)
    assert [(r["at"], r["tag"], r["attach"], r["symbol"]) for r in rows] == [
        ("src/Guard.java:5-6", "WHY", "above", "Guard.body"),          # past the @Override line
        ("src/Guard.java:14", "NB", "inside", "Guard.follow"),
    ]
    assert rows[0]["text"].splitlines()[1] == "* It is looked up again on the next tick."
    assert rows[1]["cites"] == ["docs/DESIGN.md D50"]


def test_a_comment_between_decorator_and_def_is_above_it(tmp_path):
    (tmp_path / "d.py").write_text("@staticmethod\n# why: no self\ndef f():\n    pass\n", encoding="utf-8")
    assert [(r["attach"], r["symbol"]) for r in rationale.file_rationale(tmp_path, "d.py")] == [("above", "f")]


def test_no_marker_no_row_and_prose_files_are_skipped():
    assert _rows("a.py", "x = 1  # just a note\n# Note that this is prose\n") == []
    assert _rows("README.md", "# WHY: a heading\n") == []
    assert _rows("a.py", "#!/usr/bin/env python\n# why: shebang above\nx = 1\n") == [("a.py:2", "WHY", "module", None)]


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("rationale") / "proj"
    for rel, text in {"app/cache.py": CACHE, "src/main/java/com/example/guard/Guard.java": GUARD}.items():
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


def test_node_inspect_quotes_the_rationale_of_the_symbol(repo):
    t = AtlasTools(repo)
    res = t.node_inspect("PriceCache.get")
    assert [(r["at"], r["tag"]) for r in res["rationale"]] == [("app/cache.py:20", "IMPORTANT"),
                                                              ("app/cache.py:24", "ADR")]
    assert res["rationale"][0]["text"] == "# important: a stale entry counts as a miss"
    assert res["rationale"][0]["status"] == "statically_verified" and "not verified" in res["rationale_note"]
    assert [r["tag"] for r in t.node_inspect("PriceCache.__init__")["rationale"]] == ["WHY"]
    assert "rationale" not in t.node_inspect("plain")


def test_node_inspect_on_java_uses_the_graph_spans(repo):
    g = index.load(repo)
    nid = next(n for n in g.symbols_in("src/main/java/com/example/guard/Guard.java") if g.label(n).strip(".()") == "body")
    assert [r["tag"] for r in rationale.for_node(g, nid)] == ["WHY"]


def test_cli_symbol_path_and_project(repo, capsys):
    assert cli.main(["rationale", "PriceCache.size", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "app/cache.py:13 HACK (above PriceCache.size): # HACK: callers still read .size" in out
    assert cli.main(["rationale", "app", "--repo", str(repo), "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["total"] == 5 and res["counts"]["ADR"] == 1 and all("_key" not in r for r in res["rows"])
    assert cli.main(["rationale", "--repo", str(repo), "--json", "--limit", "2"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["total"] == 7 and len(res["rows"]) == 2 and res["truncated"]
    assert cli.main(["rationale", "plain", "--repo", str(repo)]) == 2
    assert "no WHY/NOTE" in capsys.readouterr().out
    assert cli.main(["rationale", "no_such_symbol_here", "--repo", str(repo)]) == 2


# -- review round -------------------------------------------------------------

LOAN_TS = """export function approve() {
  return true;
}

// WHY: arrow fn constant
export const total = () => {
  return 2;
};
"""


@pytest.fixture()
def ts_repo(tmp_path) -> Path:
    root = tmp_path / "proj"
    (root / "web").mkdir(parents=True)
    (root / "web" / "loan.ts").write_bytes(LOAN_TS.encode("utf-8"))
    (root / "m.py").write_bytes(b"def a():\n    # WHY: reason for a\n    return 1\n\n\ndef b():\n    return 2\n")
    workflow.init(root)
    st = open_store(root)
    try:
        workflow.scan(st, root)
    finally:
        st.close()
    search_index._HANDLES.clear()
    return root


def test_an_arrow_function_constant_gets_the_comment_above_it(ts_repo):
    t = AtlasTools(ts_repo)
    res = t.node_inspect("total")
    assert [(r["at"], r["attach"]) for r in res["rationale"]] == [("web/loan.ts:5", "above")]
    g = index.load(ts_repo)
    rows = rationale.file_rationale(ts_repo, "web/loan.ts", g)
    assert [(r["attach"], r["symbol"].strip("()")) for r in rows] == [("above", "total")]


def test_a_stale_file_is_not_attached_by_the_indexed_line(ts_repo):
    p = ts_repo / "m.py"
    p.write_bytes(b"def a():\n    # WHY: reason for a\n" + b"    x = 1\n" * 5 + b"    return 1\n\n\ndef b():\n    return 2\n")
    res = AtlasTools(ts_repo).node_inspect("b")
    assert "rationale" not in res and res["rationale_note"] == rationale.STALE_NOTE
    got = rationale.lookup(ts_repo, "b", graph=lambda: index.load(ts_repo), stale=["m.py"])
    assert got["status"] == "stale" and got["rows"] == []


def test_each_row_has_a_separate_status_for_its_attachment(tmp_path):
    (tmp_path / "d.py").write_text("# why: x\ndef f():\n    pass\n", encoding="utf-8")
    (row,) = rationale.file_rationale(tmp_path, "d.py")
    assert row["status"] == "statically_verified" and row["attach_status"] == "strong_inference"


def test_a_comment_after_the_last_statement_of_a_body_is_inside_it(tmp_path):
    (tmp_path / "a.py").write_text("def a():\n    x = 1\n    # NOTE: quadratic on purpose\n\ndef b():\n"
                                   "    return 2\n", encoding="utf-8")
    assert [(r["attach"], r["symbol"]) for r in rationale.file_rationale(tmp_path, "a.py")] == [("inside", "a")]


def test_a_source_folder_named_decisions_is_not_a_decision_record():
    syms = {"approve": {"start": 2, "end": 2}}
    assert _rows("pkg/loan.ts", "// moved from lib/decisions/rules.ts in the refactor\nexport function approve() {}\n",
                 syms) == []
    (row,) = rationale.scan_text("x.ts", "// see docs/decisions/0003-cache.md\nconst a = 1;\n", {})
    assert row["cites"] == ["docs/decisions/0003-cache.md"]


def test_a_long_comment_line_is_scanned_in_linear_time():
    import time

    t0 = time.perf_counter()
    rows = rationale.scan_text("x.py", "# NOTE: " + "a." * 32000 + "\n", {})
    assert len(rows) == 1 and time.perf_counter() - t0 < 2.0
    assert rows[0]["text"].endswith("...") and len(rows[0]["text"]) == rationale.MAX_TEXT
    assert rationale.clip("x" * 250, 200).endswith("...") and len(rationale.clip("x" * 250, 200)) == 200


def test_only_newline_ends_a_line():
    syms = {"f": {"start": 3, "end": 3}}
    assert _rows("x.js", "var a = '\u2028';\n// WHY: x\nfunction f() {}\n", syms) == [("x.js:2", "WHY", "above", "f")]


def test_paths_are_resolved_against_the_repository_and_git_names_are_not_quoted(tmp_path):
    import shutil
    import subprocess

    if not shutil.which("git"):
        pytest.skip("git not installed")
    repo = tmp_path / "r"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "\u00e7al\u0131\u015f.py").write_text("# NOTE: non-ascii file\nx = 1\n", encoding="utf-8")
    (repo / "pkg" / "a.py").write_text("# WHY: plain\nx = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"], cwd=repo, check=True)
    (repo / "pkg" / "new.py").write_text("# WHY: untracked new file\nx = 1\n", encoding="utf-8")
    from verinoda import snapshot

    snapshot._LISTED.clear()
    want = {"pkg/a.py:1", "pkg/new.py:1", "pkg/\u00e7al\u0131\u015f.py:1"}
    for target in ("pkg", ".", "./pkg", str(repo / "pkg"), None):
        res = rationale.lookup(repo, target)
        assert {r["at"] for r in res["rows"]} == want, target
    assert [r["at"] for r in rationale.lookup(repo, "./pkg/a.py")["rows"]] == ["pkg/a.py:1"]
    outside = tmp_path / "o.py"
    outside.write_text("# WHY: outside\n", encoding="utf-8")
    assert rationale.lookup(repo, str(outside))["status"] == "not_found"


def test_a_broken_index_is_reported_as_such(tmp_path):
    def broken():
        raise ValueError("garbage")

    res = rationale.lookup(tmp_path, "foo", graph=broken)
    assert res["status"] == "not_found" and "could not be loaded (ValueError: garbage" in res["note"]
