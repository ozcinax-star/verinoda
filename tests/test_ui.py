"""`verinoda ui`: the notes-and-graph view model (verinoda.ui.data) and its local server."""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
from importlib import resources
from pathlib import Path

import pytest

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from verinoda import workflow
from verinoda.paths import graph_path
from verinoda.store import open_store
from verinoda.ui import data as uidata
from verinoda.ui import server as uiserver

ROOT = Path(__file__).resolve().parents[1]
GLOW = ROOT / "examples" / "glow_mod"


@pytest.fixture(scope="module")
def glow(tmp_path_factory):
    repo = tmp_path_factory.mktemp("ui") / "glow"
    shutil.copytree(GLOW, repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__"))
    workflow.init(repo)
    from verinoda.setup import add_reference

    add_reference(repo, "reference=original,plugin")  # as `verinoda setup --reference` records it
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
    finally:
        st.close()
    return repo


@pytest.fixture(scope="module")
def atlas(glow):
    a = uidata.Atlas(glow)
    a.ensure()
    return a


def _find(atlas, title: str, kind: str | None = None) -> dict:
    return next(r for r in atlas.search(title)["results"] if r["title"] == title and (kind is None or r["kind"] == kind))


def test_stats_and_the_most_connected_notes(atlas):
    s = atlas.stats()
    assert s["notes"] > 100 and s["files"] >= 30 and s["data_notes"] > 10 and s["links"] > 100
    assert s["hubs"] and all(h["kind"] in ("class", "function", "method", "file") for h in s["hubs"])
    assert not any(h["file"].startswith("reference/") for h in s["hubs"])  # the reference tree is not the project


def test_a_method_note_has_its_code_owner_and_callers(atlas):
    hit = atlas.search("spawn")["results"][0]
    assert hit["title"] == "Wisp.spawn()" and not hit["file"].startswith("reference/")  # own code before the copy
    n = atlas.note(hit["id"])
    assert n["kind"] == "method" and n["signature"] and "spawn" in n["signature"]
    assert n["code"]["lines"] and n["code"]["start"] == n["span"][0]
    assert [c["title"] for c in n["breadcrumb"]][-1] == "Wisp"
    secs = {s["key"]: s for s in n["sections"]}
    assert secs["defined_in"]["items"][0]["title"] == "Wisp"
    callers = secs["called_by"]["items"]
    assert callers and all(re.match(r".+:\d+$", c["at"] or "") for c in callers)
    assert all(c["confidence"] in ("EXTRACTED", "INFERRED") for c in callers)


def test_members_are_listed_by_line(atlas):
    cls = _find(atlas, "Wisp", "class")
    members = next(s for s in atlas.note(cls["id"])["sections"] if s["key"] == "members")["items"]
    lines = [int(m["at"].rsplit(":", 1)[1]) for m in members if m.get("at")]
    assert lines == sorted(lines)


def test_a_data_file_is_a_note_linked_to_the_code_that_names_it(atlas):
    death = next(r for r in atlas.search("wisp_death")["results"] if r["kind"] == "data")
    n = atlas.note(death["id"])
    assert n["kind"] == "data" and n["code"]["lang"] == "mcfunction" and n["code"]["lines"]
    named_by = next(s for s in n["sections"] if s["key"] == "named_by")["items"]
    assert any(i["kind"] in ("method", "function", "class") for i in named_by)  # the Java that runs it
    for i in named_by:  # every link opens a note
        assert atlas.note(i["id"])["id"] == i["id"]


def test_local_graph_is_the_note_and_its_neighbours(atlas):
    hit = atlas.search("spawn")["results"][0]
    g1 = atlas.local_graph(hit["id"], 1)
    ids = {n["id"] for n in g1["nodes"]}
    assert hit["id"] in ids and len(ids) > 1 and {n["depth"] for n in g1["nodes"]} <= {0, 1}
    assert all(e["source"] in ids and e["target"] in ids for e in g1["edges"])
    assert not any(n["kind"] == "external" for n in g1["nodes"])  # hidden unless asked for
    g2 = atlas.local_graph(hit["id"], 2)
    assert len(g2["nodes"]) >= len(g1["nodes"])
    only_calls = atlas.local_graph(hit["id"], 1, {"calls"}, data=False)
    assert all(e["relation"] == "calls" for e in only_calls["edges"])
    with pytest.raises(KeyError):
        atlas.local_graph("no such note")


def test_global_graph_is_files_and_their_links(atlas):
    g = atlas.global_graph()
    ids = {n["id"] for n in g["nodes"]}
    assert len(ids) == len(g["nodes"]) >= 20
    assert all(e["source"] in ids and e["target"] in ids and e["weight"] >= 1 for e in g["edges"])
    assert any(n["kind"] == "data" for n in g["nodes"]) and any(e["relation"] == "names" for e in g["edges"])
    no_data = atlas.global_graph(data=False)
    assert not any(n["kind"] == "data" for n in no_data["nodes"])
    for n in g["nodes"][:5]:
        assert atlas.note(n["id"])["id"] == n["id"]


def test_tree_lists_every_file_with_a_note(atlas):
    tree = atlas.tree()
    files = []

    def walk(node):
        for c in node.get("children", []):
            walk(c) if "children" in c else files.append(c)

    walk(tree)
    paths = {f["path"] for f in files}
    assert "src/main/java/com/example/glowmod/entity/Wisp.java" in paths
    assert any(p.endswith("wisp_death.mcfunction") for p in paths)
    for f in files[:10]:
        assert atlas.note(f["id"])["file"] == f["path"]


def test_a_new_index_is_picked_up(glow):
    a = uidata.Atlas(glow)
    a.ensure()
    first = a.g
    gp = graph_path(glow)
    gp.write_bytes(gp.read_bytes() + b"\n")  # what `verinoda update` does: a new graph.json
    assert a.ensure() is not first


# -- the server ------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def served(glow):
    server, _url = uiserver.start(glow)
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def _get(port: int, path: str, *, host: str | None = None, method: str = "GET"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request(method, path, headers={"Host": host or f"127.0.0.1:{port}"})
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r.status, dict(r.getheaders()), body


def test_the_page_and_the_api_are_served_locally(served):
    status, headers, body = _get(served, "/")
    assert status == 200 and b"app.js" in body and "default-src 'none'" in headers["Content-Security-Policy"]
    assert _get(served, "/app.js")[0] == 200 and _get(served, "/app.css")[0] == 200
    status, _h, body = _get(served, "/api/stats")
    assert status == 200 and json.loads(body)["notes"] > 0
    hit = json.loads(_get(served, "/api/search?q=spawn")[2])["results"][0]
    from urllib.parse import quote

    status, _h, body = _get(served, "/api/note?id=" + quote(hit["id"]))
    assert status == 200 and json.loads(body)["title"] == hit["title"]
    status, _h, body = _get(served, "/api/local?depth=2&id=" + quote(hit["id"]))
    assert status == 200 and json.loads(body)["center"] == hit["id"]
    assert _get(served, "/api/global?data=0")[0] == 200


def test_the_server_refuses_other_hosts_writes_and_unknown_paths(served):
    assert _get(served, "/api/stats", host="evil.example")[0] == 403        # DNS rebinding
    assert _get(served, "/", host=f"attacker.test:{served}")[0] == 403
    assert _get(served, "/api/stats", host=f"localhost:{served}")[0] == 200
    assert _get(served, "/api/stats", method="POST")[0] == 405
    assert _get(served, "/../pyproject.toml")[0] == 404
    status, _h, body = _get(served, "/api/note?id=nope")
    assert status == 404 and json.loads(body)["error"] == "not_found"


def test_the_page_loads_nothing_from_outside():
    for name in ("index.html", "app.js", "app.css"):
        text = resources.files("verinoda.ui").joinpath("static", name).read_text(encoding="utf-8")
        assert not re.search(r"https?://", text), name   # no CDN, no fonts, no telemetry
    html = resources.files("verinoda.ui").joinpath("static", "index.html").read_text(encoding="utf-8")
    assert "<script>" not in html and " style=" not in html   # nothing inline (the CSP allows only 'self')


def test_ui_without_an_index_fails_before_serving(tmp_path):
    with pytest.raises(FileNotFoundError):
        uiserver.start(tmp_path)


# -- review fixes ----------------------------------------------------------------------------------

def test_a_file_note_spans_the_whole_file(atlas):
    f = _find(atlas, "Wisp.java", "file")
    n = atlas.note(f["id"])
    assert n["span"] == [1, n["code"]["total"]] and n["code"]["total"] > 1


def test_name_search_uses_the_name_written_in_the_code(atlas):
    # every .java file matches "java" only by its path, never as a name
    assert all(r["why"] == "path" for r in atlas.search("java")["results"] if r["kind"] == "file")
    # the class or module in a title is not part of the member's name: members do not crowd out names
    assert not [r for r in atlas.search("glowmod")["results"] if r["kind"] == "method" and r["why"] != "path"]
    assert atlas.search("Wisp.spa")["results"][0]["title"] == "Wisp.spawn()"   # a qualified prefix still works


def test_claims_are_matched_exactly_including_file_claims(glow):
    from verinoda.claims import Claims

    st = open_store(glow)
    try:
        c = Claims(st, glow)
        snap = st.latest_snapshot()
        c.create("Wisp.java is the wisp entity", project=str(glow), snapshot=snap,
                 subjects=["src/main/java/com/example/glowmod/entity/Wisp.java"])
        c.create("spawn lives elsewhere", project=str(glow), snapshot=snap,
                 subjects=["other/src/main/java/com/example/glowmod/entity/Wisp.java::.spawn()"])
    finally:
        st.close()
    a = uidata.Atlas(glow)
    file_note = a.note(_find(a, "Wisp.java", "file")["id"])
    claims = next(s for s in file_note["sections"] if s["key"] == "claims")["items"]
    assert [x["title"] for x in claims] == ["Wisp.java is the wisp entity"]
    spawn = a.note(next(r for r in a.search("spawn")["results"] if r["title"] == "Wisp.spawn()")["id"])
    assert not any(s["key"] == "claims" for s in spawn["sections"])  # a path that only ends like its own


def test_only_files_inside_the_project_are_read(atlas):
    snap = atlas.snapshot()
    assert snap.inside("src/main/java/com/example/glowmod/entity/Wisp.java")
    for bad in ("../outside.txt", "/etc/passwd", "C:/Windows/win.ini", "src/../../x", ""):
        assert not snap.inside(bad), bad
    assert snap.read_lines("../pyproject.toml") == []


def test_lines_are_split_only_where_the_graph_counts_them(tmp_path, atlas):
    snap = atlas.snapshot()
    p = snap.repo / "ff_sample.py"
    p.write_bytes(b"a = 1\n\x0c\nb = 2\r\nc = '\xe2\x80\xa8'\n")
    try:
        assert snap.read_lines("ff_sample.py") == ["a = 1", "\x0c", "b = 2", "c = '\u2028'"]
    finally:
        p.unlink()


def test_a_changed_search_index_is_picked_up(glow):
    from verinoda.paths import search_db_path

    a = uidata.Atlas(glow)
    first = a.snapshot()
    db = search_db_path(glow)
    st = db.stat()
    os.utime(db, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert a.snapshot() is not first


def test_a_capped_local_graph_shows_users_before_members(atlas, monkeypatch):
    cls = _find(atlas, "Wisp", "class")
    full = atlas.local_graph(cls["id"], 1)
    members = {n["id"] for n in full["nodes"]} & {e["target"] for e in full["edges"]   # its own members
                                                 if e["source"] == cls["id"] and e["relation"] in ("method", "contains")}
    others = {n["id"] for n in full["nodes"]} - members - {cls["id"]}
    assert members and others
    monkeypatch.setattr(uidata, "MAX_LOCAL_NODES", 1 + len(others))
    capped = atlas.snapshot().local_graph(cls["id"], 3)
    assert capped["truncated"] and {n["id"] for n in capped["nodes"]} - {cls["id"]} == others
