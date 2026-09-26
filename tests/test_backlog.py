"""Backlog items <-> code comments (docs/DESIGN.md D50)."""

from __future__ import annotations

import json
import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import backlog, cli, index, retrieval, search_index, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

BACKLOG = """# Backlog

## 12 - The guard follows the player

| No | What | Status and why |
|---|---|---|
| 12.3 | **The guard forgot its body** | **DONE.** Root cause (being loaded is not existing): the lookup by id missed a fresh body in its first ticks, and one miss cleared the reference for good. A weak reference to the body we spawned answers first. |
| 12.4 | Guard walks with the player | DONE. |
| 1.21 | A version, not an item | - |
"""

GUARD = """package com.example.guard;

import java.lang.ref.WeakReference;

public final class Guard {
    private static java.util.UUID body;
    // why: 12.3 - the lookup by id misses a fresh body in its first ticks
    private static WeakReference<Object> bodyRef = new WeakReference<>(null);

    /** 12: the body the guard stands in now, or null. */
    public static Object bodyEntity() {
        if (body == null) {
            return null;
        }
        Object ref = bodyRef.get();
        if (ref != null) {
            return ref;
        }
        return null;
    }

    public static void follow() {
        // 12.4: walks with the player; needs Minecraft 1.21 or later
        int ticks = 12;
    }
}
"""


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("backlog") / "mod"
    for rel, text in {"docs/BACKLOG.md": BACKLOG, "src/main/java/com/example/guard/Guard.java": GUARD}.items():
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


F = "src/main/java/com/example/guard/Guard.java"


def test_items_rows_and_headings(repo):
    t = backlog.items(repo)
    assert set(t) == {"12", "12.3", "12.4", "1.21"}
    assert t["12.3"].title == "The guard forgot its body" and t["12.3"].at == "docs/BACKLOG.md:7"
    assert "being loaded is not existing" in t["12.3"].text
    assert t["12"].at == "docs/BACKLOG.md:3"


def test_comment_ids_need_a_known_item_and_a_label_for_whole_numbers():
    known = {"12", "12.3", "12.4", "1.21"}
    assert backlog.comment_ids("    // why: 12.3 - the lookup", known) == ["12.3"]
    assert backlog.comment_ids("    /** 12: the body */", known) == ["12"]
    assert backlog.comment_ids("    int ticks = 12; // 12 ticks", known) == []   # a number, not a label
    assert backlog.comment_ids("    String v = \"12.3\";", known) == []            # not a comment
    assert backlog.comment_ids("    // see 9.9 and 12.4", known) == ["12.4"]


def test_a_line_finds_the_item_of_the_field_it_uses(repo):
    ln = GUARD.splitlines().index("        Object ref = bodyRef.get();") + 1
    got = backlog.items_for(repo, F, ln)
    assert [(x["item"].id, x["via"]) for x in got] == [("12.3", "bodyRef (line 8)")]
    res = backlog.lookup(repo, f"{F}:{ln}")
    assert res["status"] == "found" and res["items"][0]["id"] == "12.3"
    assert "being loaded is not existing" in res["items"][0]["text"]


def test_a_symbol_and_an_item_both_ways(repo):
    res = backlog.lookup(repo, "Guard.bodyEntity", graph=lambda: index.load(repo))
    assert [x["id"] for x in res["items"]] == ["12", "12.3"]
    item = backlog.lookup(repo, "12.4")
    assert item["kind"] == "item" and [s["at"] for s in item["sites"]] == [f"{F}:23"]
    assert "Minecraft 1.21" in item["sites"][0]["text"]   # 1.21 is an item here, but the comment cites 12.4 first
    assert backlog.lookup(repo, "nothing.here")["status"] == "not_found"


def test_cli_and_query(repo, capsys):
    assert cli.main(["backlog", "12.3", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("12.3 The guard forgot its body (docs/BACKLOG.md:7)") and f"{F}:7" in out
    assert cli.main(["backlog", "Guard.bodyEntity", "--repo", str(repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["items"][1]["via"] == "bodyRef (line 8)"
    text = retrieval.render_text(retrieval.retrieve(index.load(repo), "guard body entity lookup"))
    assert "backlog: 12 The guard follows the player (docs/BACKLOG.md:3)" in text


def test_no_backlog(tmp_path):
    assert backlog.lookup(tmp_path, "1.2")["status"] == "no_backlog"
