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


def test_own_code_is_listed_before_tests_and_the_reference_copy(atlas):
    snap = atlas.snapshot()

    def place(item):
        f = item.get("file") or ""
        return 2 if snap.in_reference(f) else 1 if uidata.is_test_file(f) else 0

    mixed = 0
    for hit in [_find(atlas, "Wisp.spawn()")] + atlas.stats()["hubs"]:
        for s in atlas.note(hit["id"])["sections"]:
            if s["key"] == "claims":
                continue
            places = [place(it) for it in s["items"]]
            assert places == sorted(places), (hit["title"], s["key"])
            mixed += len(set(places)) > 1
    assert mixed  # the order was checked where it matters


def test_a_file_note_does_not_repeat_its_outline_as_members(atlas):
    cls = _find(atlas, "Wisp", "class")
    fnote = next(r for r in atlas.search(cls["file"].rsplit("/", 1)[1])["results"] if r["kind"] == "file")
    n = atlas.note(fnote["id"])
    assert n["outline"] and any(o["id"] == cls["id"] for o in n["outline"])
    members = next((s["items"] for s in n["sections"] if s["key"] == "members"), [])
    assert not {o["id"] for o in n["outline"]} & {m["id"] for m in members}


def test_graph_groups_are_named_by_their_folder(atlas):
    g = atlas.global_graph()
    names = [x["name"] for x in g["groups"]]
    assert names and len(names) == len(set(names))  # two communities never share a name
    folders: dict = {}
    for n in g["nodes"]:
        if isinstance(n["group"], int):
            folders.setdefault(n["group"], set()).add("/" + n["folder"].strip("./"))
    for x in g["groups"]:
        short = x["name"].split(" · ")[0]
        assert short.count("/") <= 1 and any(f.endswith("/" + short.strip("/")) for f in folders[x["id"]])


def test_graph_areas_are_the_first_two_folders(atlas):
    g = atlas.global_graph()
    for n in g["nodes"]:
        parts = [p for p in n["folder"].split("/") if p not in ("", ".")]
        assert n["area"] == ("/".join(parts[:2]) or "/")
    assert len({n["area"] for n in g["nodes"]}) > 1
    assert uidata._area("") == uidata._area(".") == "/" and uidata._area("a/b/c") == "a/b"


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
        text = text.replace("http://www.w3.org/2000/svg", "")  # the icon's SVG namespace names, it loads nothing
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


def test_a_claim_recorded_after_a_note_was_read_is_shown(glow, monkeypatch):
    from verinoda.claims import Claims

    monkeypatch.setattr(uidata, "RACY_NS", 0)  # keep what was read, however recent: the cache must follow atlas.db
    a = uidata.Atlas(glow)
    fid = _find(a, "GlowMod.java", "file")["id"]

    def claims():
        return [x["title"] for s in a.note(fid)["sections"] if s["key"] == "claims" for x in s["items"]]

    assert "GlowMod.java registers the mod" not in claims()
    st = open_store(glow)
    try:
        Claims(st, glow).create("GlowMod.java registers the mod", project=str(glow), snapshot=st.latest_snapshot(),
                                subjects=["src/main/java/com/example/glowmod/GlowMod.java"])
    finally:
        st.close()
    assert "GlowMod.java registers the mod" in claims()


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


# -- the exported file (`verinoda ui --export`) ----------------------------------------------------

def _exported(glow, tmp_path) -> tuple[str, dict]:
    from verinoda.ui import export

    out = export.write(glow, tmp_path / "graph.html")
    html = Path(out["path"]).read_text(encoding="utf-8")
    raw = re.search(r'<script type="application/json" id="verinoda-data">(.*?)</script>', html, re.DOTALL).group(1)
    return html, json.loads(raw)


def test_the_export_is_one_file_that_fetches_nothing(glow, tmp_path):
    import base64
    import hashlib

    html, data = _exported(glow, tmp_path)
    assert data["format"] == "verinoda-export" and data["global"]["nodes"] and data["notes"]
    assert not re.search(r'<(script|link|img)[^>]+(src|href)="(?!data:)', html)  # nothing loaded from anywhere
    csp = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', html).group(1)
    assert "default-src 'none'" in csp and "connect-src" not in csp and "unsafe" not in csp
    inline = {"script": re.findall(r"<script>(.*?)</script>", html, re.DOTALL), "style": re.findall(r"<style>(.*?)</style>", html, re.DOTALL)}
    assert len(inline["script"]) == len(inline["style"]) == 1
    for kind, (body,) in inline.items():  # the policy allows exactly the page's own script and style
        digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()
        assert f"{kind}-src 'sha256-{digest}'" in csp
    assert str(glow.resolve()) not in html and glow.resolve().as_posix() not in html
    assert "root" not in data["stats"]


def test_every_link_in_the_export_opens_a_note_it_holds(glow, tmp_path):
    _html, data = _exported(glow, tmp_path)
    notes = data["notes"]
    assert all(n["kind"] in ("file", "doc", "data") and n["code"] is None for n in notes.values())
    links = [it for n in notes.values() for s in n["sections"] for it in s["items"]]
    links += [c for n in notes.values() for c in n["breadcrumb"]] + data["stats"]["hubs"]
    assert links and all(it["id"] in notes for it in links if "id" in it)
    assert not any("snippet" in it for it in links)  # a line of code is code: not in the exported file
    assert all("id" not in o for n in notes.values() for o in n["outline"])  # a file's own symbols: text
    tree_ids = []
    stack = [data["tree"]]
    while stack:
        node = stack.pop()
        stack.extend(node.get("children", []))
        if "id" in node:
            tree_ids.append(node["id"])
    assert tree_ids and set(tree_ids) <= set(notes)
    assert {n["id"] for n in data["global"]["nodes"]} <= set(notes)
    # a symbol that calls into another file leads to that file's note
    spawn_file = next(n for n in notes.values() if n["title"] == "Wisp.java")
    assert any(it.get("id") and it["id"] != spawn_file["id"] for s in spawn_file["sections"] for it in s["items"])


def _machine(monkeypatch, name: str):
    """A root and a home folder of a user called ``name``, in this platform's form."""
    from verinoda.ui import export

    base = Path(f"C:/Users/{name}") if os.name == "nt" else Path(f"/home/{name}")
    monkeypatch.setattr(export.Path, "home", classmethod(lambda cls: base))
    return base / "src" / "proj", base


@pytest.mark.parametrize("user", ["someone", "Çınar", "Jürgen"])
def test_the_export_carries_no_path_of_this_machine(monkeypatch, user):
    from verinoda.project_index.ids import make_id
    from verinoda.ui import export

    root, home = _machine(monkeypatch, user)
    scrub = export._scrubber(root)
    assert scrub(str(root / "pkg" / "a.py")) == str(Path("pkg") / "a.py")
    assert scrub(root.as_posix() + "/pkg/a.py") == "pkg/a.py"
    # an unresolved import is named after its absolute path, folded the way the index folds it
    assert scrub(make_id(str(root / "tests" / "foundation"))) == "tests_foundation"
    assert scrub("see " + make_id(str(root / "lib" / "helpers")) + ".") == "see lib_helpers."
    elsewhere = scrub("cache in " + str(home / "other" / "b.py"))
    assert elsewhere.startswith("cache in ~/") and user.lower() not in elsewhere.lower()


@pytest.mark.parametrize("user", ["user", "me", "dev"])
def test_the_scrub_leaves_ordinary_names_alone(monkeypatch, user):
    from verinoda.ui import export

    root, _home = _machine(monkeypatch, user)
    scrub = export._scrubber(root)
    for text in ("src/components/home/UserCard.tsx", "pages/home/menu.tsx", "src/home/devices.ts",
                 "get_home_user_dir()", "HOME_USER_ID", "tasks/sync_users_members.py", "sync_users_members",
                 "users_me_panel", "src/app/page.tsx", "ordinary text about a user"):
        assert scrub(text) == text
    for short in (Path("/app"), Path("/workspace"), Path("D:/code")):  # a short root is a word as well
        s = export._scrubber(short)
        assert s("src/app/page.tsx") == "src/app/page.tsx" and s("workspace_settings") == "workspace_settings"
        assert s("unused_code") == "unused_code"


def test_note_ids_are_never_rewritten(glow, tmp_path, monkeypatch):
    from verinoda.ui import export

    # a home folder whose name is also a folder of the project: ids and paths must stay distinct
    monkeypatch.setattr(export.Path, "home", classmethod(lambda cls: Path("/src")))
    _html, data = _exported(glow, tmp_path)
    ids = [n["id"] for n in data["global"]["nodes"]]
    assert len(ids) == len(set(ids)) and set(ids) <= set(data["notes"])
    assert all(k == n["id"] for k, n in data["notes"].items())


def test_the_export_keeps_the_claims_on_a_files_symbols(glow, tmp_path):
    from verinoda.claims import Claims

    st = open_store(glow)
    try:
        Claims(st, glow).create("spawn is only called on the server", project=str(glow), snapshot=st.latest_snapshot(),
                                subjects=["src/main/java/com/example/glowmod/entity/Wisp.java::.spawn()"])
    finally:
        st.close()
    _html, data = _exported(glow, tmp_path)
    wisp = next(n for n in data["notes"].values() if n["file"] == "src/main/java/com/example/glowmod/entity/Wisp.java")
    claims = next(s for s in wisp["sections"] if s["key"] == "claims")["items"]
    assert {"title": "spawn is only called on the server", "at": ".spawn()"}.items() <= next(
        c for c in claims if c["title"] == "spawn is only called on the server").items()
    assert all("id" not in c for c in claims)  # a claim is not a note to open


def test_the_export_outline_is_every_symbol(glow, tmp_path, monkeypatch):
    from verinoda.ui import export

    real = export.Atlas.snapshot

    def small_view(self):  # the note view keeps a few; the export still lists them all
        snap = real(self)
        orig = snap.note

        def note(nid, **kw):
            n = orig(nid, **kw)
            return {**n, "outline": (n.get("outline") or [])[:1]}

        snap.note = note
        return snap

    monkeypatch.setattr(export.Atlas, "snapshot", small_view)
    _html, data = _exported(glow, tmp_path)
    wisp = next(n for n in data["notes"].values() if n["title"] == "Wisp.java")
    titles = [o["title"] for o in wisp["outline"]]
    assert len(titles) > 1 and "Wisp.spawn()" in titles


def test_the_exported_graph_is_the_whole_graph_with_the_cap_to_apply(glow, tmp_path):
    _html, data = _exported(glow, tmp_path)
    g = data["global"]
    assert g["cap"] == uidata.MAX_GLOBAL_NODES and g["hidden_files"] == 0
    served = uidata.Atlas(glow).global_graph()
    assert {n["id"] for n in served["nodes"]} <= {n["id"] for n in g["nodes"]}


def test_a_failed_export_leaves_nothing_behind(glow, tmp_path, monkeypatch):
    from verinoda.ui import export

    def fail(src, dst):
        raise PermissionError("held by another program")

    monkeypatch.setattr(export.os, "replace", fail)
    with pytest.raises(PermissionError):
        export.write(glow, tmp_path / "g.html")
    assert list(tmp_path.iterdir()) == []


def test_an_export_into_a_new_folder(glow, tmp_path):
    from verinoda.ui import export

    out = export.write(glow, str(tmp_path / "new") + "/")
    assert Path(out["path"]) == (tmp_path / "new" / export.DEFAULT_NAME).resolve()
    assert export.target_path(glow, tmp_path) == tmp_path / export.DEFAULT_NAME  # an existing folder
    assert export.target_path(glow, None) == export.default_path(glow)


def test_ui_export_and_graph_flags(glow, tmp_path, monkeypatch, capsys):
    import webbrowser

    from verinoda import cli

    out = tmp_path / "g.html"
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    assert cli.main(["ui", str(glow), "--export", str(out)]) == 0
    printed = capsys.readouterr().out
    assert out.stat().st_size > 1000 and "no server needed" in printed and not opened
    assert out.resolve().as_uri() in printed  # an address to paste into a browser
    assert cli.main(["ui", str(glow), "--export", str(out), "--open"]) == 0
    assert opened == [out.resolve().as_uri()]
    seen = {}
    monkeypatch.setattr(uiserver, "serve", lambda repo, **kw: seen.update(kw))
    assert cli.main(["ui", str(glow), "--graph", "--no-browser"]) == 0
    assert seen["open_at"] == "#/graph" and seen["open_browser"] is False
    assert cli.main(["ui", str(glow), "--watch", "--no-browser"]) == 0 and seen["watch"] is True


def test_a_link_shows_the_line_it_is_written_on(atlas, glow):
    hit = atlas.search("spawn")["results"][0]
    n = atlas.note(hit["id"])
    secs = {s["key"]: s["items"] for s in n["sections"]}
    callers = [c for c in secs["called_by"] if c.get("at")]
    assert callers and all(c["snippet"] for c in callers)
    for c in callers:
        f, _, ln = c["at"].rpartition(":")
        line = (glow / f).read_text(encoding="utf-8").split("\n")[int(ln) - 1].strip()
        assert c["snippet"] == line[:uidata.MAX_SNIPPET] or c["snippet"].endswith("…")
    assert all(i.get("snippet") is None for k in uidata.STRUCTURAL_SECTIONS for i in secs.get(k, []))
    assert atlas.snapshot().line_at("../outside.py:1") is None and atlas.snapshot().line_at(None) is None


def test_a_link_line_is_not_shown_from_a_file_edited_since_the_index(glow):
    a = uidata.Atlas(glow)
    hit = next(r for r in a.search("spawn")["results"] if r["title"] == "Wisp.spawn()")
    callers = next(s["items"] for s in a.note(hit["id"])["sections"] if s["key"] == "called_by")
    edited = next(c["at"].rpartition(":")[0] for c in callers if c.get("snippet"))
    p = glow / edited
    before = p.read_bytes()
    try:
        p.write_bytes(b"// a line added on top\n" + before)
        again = next(s["items"] for s in a.note(hit["id"])["sections"] if s["key"] == "called_by")
        assert all(c.get("snippet") is None for c in again if c["at"].startswith(edited + ":"))
    finally:
        p.write_bytes(before)
    assert any(c.get("snippet") for c in next(s["items"] for s in a.note(hit["id"])["sections"]
                                               if s["key"] == "called_by"))


def test_impact_lists_what_depends_on_a_note_by_distance(atlas):
    spawn = _find(atlas, "Wisp.spawn()")
    n = atlas.note(spawn["id"])
    callers = {c["id"] for s in n["sections"] if s["key"] == "called_by" for c in s["items"]}
    r = atlas.impact(spawn["id"], 3)
    first = {x["id"] for x in r["items"] if x["depth"] == 1}
    assert callers and callers <= first  # every direct caller, one link back
    assert all(x["via"] == spawn["id"] for x in r["items"] if x["depth"] == 1)
    assert [x["depth"] for x in r["items"]] == sorted(x["depth"] for x in r["items"])
    no_tests = atlas.impact(spawn["id"], 3, tests=False)
    assert not any(uidata.is_test_file(x["file"] or "") for x in no_tests["items"])
    wisp_file = atlas.note(_find(atlas, "Wisp", "class")["id"])["breadcrumb"][0]["id"]
    assert first <= {x["id"] for x in atlas.impact(wisp_file, 1)["items"]}  # a file: what uses anything it defines
    with pytest.raises(KeyError):
        atlas.impact("no such note")


def test_path_finds_the_chain_of_uses_either_way(atlas):
    spawn = _find(atlas, "Wisp.spawn()")
    caller = atlas.impact(spawn["id"], 1)["items"][0]
    fwd = atlas.path(caller["id"], spawn["id"])
    assert fwd["found"] and fwd["direction"] == "forward"
    assert fwd["steps"][0]["id"] == caller["id"] and fwd["steps"][-1]["id"] == spawn["id"]
    assert fwd["steps"][-1]["relation"] and fwd["steps"][0]["relation"] is None
    back = atlas.path(spawn["id"], caller["id"])
    assert back["found"] and back["direction"] == "backward"
    with pytest.raises(KeyError):
        atlas.path("nope", spawn["id"])


def test_impact_and_path_are_served(served, atlas):
    spawn = _find(atlas, "Wisp.spawn()")
    status, _h, body = _get(served, "/api/impact?id=" + spawn["id"] + "&depth=2&tests=0")
    assert status == 200 and json.loads(body)["depth"] == 2
    caller = atlas.impact(spawn["id"], 1)["items"][0]["id"]
    status, _h, body = _get(served, f"/api/path?from={caller}&to={spawn['id']}")
    assert status == 200 and json.loads(body)["found"]
    assert _get(served, "/api/path?from=x&to=y")[0] == 404


def test_a_question_is_answered_with_the_passages_of_verinoda_query(atlas, glow):
    from verinoda.paths import search_db_path

    db = search_db_path(glow)
    before = db.stat().st_mtime_ns
    r = atlas.answer("what happens when a wisp dies?")
    assert r["items"] and r["question"] == "what happens when a wisp dies?"
    top = r["items"][0]
    assert top["file"] and top["lines"] and top["excerpt"] and top["why"]
    assert any(it["id"] for it in r["items"])
    for it in r["items"]:
        if it["id"]:
            assert atlas.note(it["id"])["id"] == it["id"]  # a passage opens its note
    assert db.stat().st_mtime_ns == before  # answering never writes the search index
    with pytest.raises(ValueError):
        atlas.answer("   ")


def test_a_question_is_answered_by_the_server(served):
    status, _h, body = _get(served, "/api/answer?q=" + "which%20class%20spawns%20the%20wisp%3F")
    assert status == 200 and json.loads(body)["items"]
    assert _get(served, "/api/answer?q=")[0] == 400


def test_changes_since_the_index_and_a_stale_note(tmp_path):
    repo = tmp_path / "orders"
    shutil.copytree(ROOT / "examples" / "orders_app", repo, ignore=shutil.ignore_patterns(".verinoda", "__pycache__"))
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
    finally:
        st.close()
    a = uidata.Atlas(repo)
    assert a.changes()["edited"] == [] and a.changes()["added"] == [] and a.changes()["deleted"] == []
    files = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*.py") if ".verinoda" not in p.parts)
    edited, gone = files[0], files[1]
    (repo / edited).write_text((repo / edited).read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    (repo / gone).unlink()
    (repo / "new_module.py").write_text("def fresh():\n    return 1\n", encoding="utf-8")
    ch = a.changes()
    assert [c["file"] for c in ch["edited"]] == [edited] and ch["edited"][0]["id"]
    assert [c["file"] for c in ch["deleted"]] == [gone]
    assert [c["file"] for c in ch["added"]] == ["new_module.py"]
    assert a.note(ch["edited"][0]["id"])["stale"] is True
    fresh = next(f for f in files[2:] if a.snapshot()._file_note(f))
    assert a.note(a.snapshot()._file_note(fresh))["stale"] is False



def test_the_page_can_tell_when_the_index_changed(glow):
    a = uidata.Atlas(glow)
    k1 = a.version()
    gp = graph_path(glow)
    st = gp.stat()
    os.utime(gp, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    assert a.version() != k1


def test_watch_runs_one_update_after_a_burst_of_edits(tmp_path):
    import time as _t

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    calls = []
    w = uiserver.Watcher(tmp_path, interval=0.05, run_update=lambda: calls.append(_t.perf_counter()))
    w.start()
    try:
        _t.sleep(0.3)
        assert calls == []  # nothing changed
        for i in range(3):  # a save that writes several files
            (tmp_path / f"b{i}.py").write_text(f"y = {i}\n", encoding="utf-8")
        deadline = _t.perf_counter() + 5
        while not calls and _t.perf_counter() < deadline:
            _t.sleep(0.05)
        _t.sleep(0.4)
        assert len(calls) == 1 and w.updates == 1 and w.error is None
    finally:
        w.stop.set()
        w.join(2)


# -- a large repository (step measured on the Python standard library: 2,305 files, 79,526 nodes) ---

def test_stats_are_made_once_per_load(atlas):
    assert atlas.stats() is atlas.stats()  # the start page asks on every visit


def test_a_lean_note_has_the_same_links_without_their_lines(atlas):
    snap = atlas.snapshot()
    nid = _find(atlas, "Wisp.spawn()")["id"]
    full, lean = snap.note(nid), snap.note(nid, lean=True)
    assert any(it.get("snippet") for s in full["sections"] for it in s["items"])
    assert not any(it.get("snippet") for s in lean["sections"] for it in s["items"])
    assert "user_note" in full and "user_note" not in lean
    assert [(s["key"], [it["id"] for it in s["items"]]) for s in full["sections"]] == \
        [(s["key"], [it["id"] for it in s["items"]]) for s in lean["sections"]]


def test_a_pinned_search_db_connection_is_shared_until_the_block_ends(tmp_path):
    import threading

    from verinoda.search_index import Handle

    h = Handle(db=str(tmp_path / "search.db"), meta={}, units={}, nid_uid={})
    with h.pinned():
        a = h.connect()
        h.release(a)
        b = h.connect()
        assert a is b and b.execute("SELECT 1").fetchone() == (1,)  # released, still open
        h.release(b)
        other = []

        def elsewhere():  # another thread gets its own connection, closed on release
            conn = h.connect()
            other.append(conn is a)
            h.release(conn)

        t = threading.Thread(target=elsewhere)
        t.start()
        t.join()
        assert other == [False]
    c = h.connect()
    try:
        assert c is not a
    finally:
        h.release(c)
    with pytest.raises(Exception):
        a.execute("SELECT 1")  # closed with the block
