"""Notes of your own on the code (verinoda.usernotes), in the served view and on the command line."""

from __future__ import annotations

import http.client
import json
import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from verinoda import usernotes, workflow
from verinoda.store import open_store
from verinoda.ui import data as uidata
from verinoda.ui import server as uiserver

BILLING = '''"""Billing."""


def charge(amount, rate):
    """Charge with tax."""
    total = amount * (1 + rate)
    return round(total, 2)


def refund(amount):
    return -amount
'''


def _scan(root: Path) -> None:
    st = open_store(root)
    try:
        workflow.scan(st, root)
    finally:
        st.close()


@pytest.fixture
def shop(tmp_path):
    root = tmp_path / "shop"
    (root / "src").mkdir(parents=True)
    (root / "src" / "billing.py").write_text(BILLING, encoding="utf-8")
    (root / "settings.cfg").write_text("rate = 0.18\nregion = eu\n", encoding="utf-8")
    workflow.init(root)
    _scan(root)
    return root


def _charge(root: Path) -> tuple[str, int, int]:
    lines = (root / "src" / "billing.py").read_text(encoding="utf-8").split("\n")
    start = next(i for i, x in enumerate(lines, 1) if x.startswith("def charge"))
    end = next(i for i, x in enumerate(lines, 1) if x.strip().startswith("return round"))
    return "src/billing.py::charge()", start, end


def test_a_note_follows_its_code_moved_changed_kept_and_gone(shop):
    subject, start, end = _charge(shop)
    n = usernotes.save(shop, subject, "src/billing.py", start, end, "Rounds **after** tax.\n\n- see [[refund()]]")
    assert n.path.exists() and n.path.parent == usernotes.notes_dir(shop)
    assert usernotes.check(shop, usernotes.find(shop, subject))["status"] == "fresh"

    src = shop / "src" / "billing.py"
    src.write_text(BILLING.replace('"""Billing."""', '"""Billing."""\nimport math\n\n# a comment'), encoding="utf-8")
    st = usernotes.check(shop, usernotes.find(shop, subject))  # moved, same code
    assert st["status"] == "fresh" and st["start"] == start + 3

    src.write_text(BILLING.replace("round(total, 2)", "round(total, 3)"), encoding="utf-8")
    st = usernotes.check(shop, usernotes.find(shop, subject))
    assert st["status"] == "changed"
    usernotes.keep(shop, usernotes.find(shop, subject))  # read again: still right
    assert usernotes.check(shop, usernotes.find(shop, subject))["status"] == "fresh"
    assert usernotes.find(shop, subject).text.startswith("Rounds **after** tax.")

    src.write_text(BILLING.replace("def charge(amount, rate):", "def bill(amount, rate):"), encoding="utf-8")
    assert usernotes.check(shop, usernotes.find(shop, subject))["status"] == "gone"
    with pytest.raises(ValueError):
        usernotes.keep(shop, usernotes.find(shop, subject))

    assert usernotes.save(shop, subject, "src/billing.py", start, end, "") is None  # empty text deletes
    assert usernotes.find(shop, subject) is None and not list(usernotes.notes_dir(shop).glob("*.md"))


def test_a_part_of_a_file_without_code_facts_is_pinned_to_its_lines(shop):
    usernotes.save(shop, "settings.cfg::rate", "settings.cfg", 1, 1, "EU rate.")
    n = usernotes.find(shop, "settings.cfg::rate")
    assert "text" in n.anchor and usernotes.check(shop, n)["status"] == "fresh"
    (shop / "settings.cfg").write_text("# rates\nrate = 0.18\nregion = eu\n", encoding="utf-8")
    st = usernotes.check(shop, n)
    assert st["status"] == "fresh" and st["start"] == 2  # the same line, moved
    (shop / "settings.cfg").write_text("rate = 0.20\nregion = eu\n", encoding="utf-8")
    assert usernotes.check(shop, n)["status"] == "changed"
    assert usernotes.check(shop, n, resolves=False)["status"] == "gone"  # the index no longer has it
    assert usernotes.check(shop, usernotes.UserNote("x", "../outside.cfg", 1, 1, "t"))["status"] == "gone"


def test_a_note_on_a_whole_file_watches_the_whole_file(shop):
    usernotes.save(shop, "settings.cfg", "settings.cfg", 1, 1, "EU settings.")  # the lines given do not matter
    n = usernotes.find(shop, "settings.cfg")
    assert "file" in n.anchor and (n.start, n.end) == (1, 3)
    (shop / "settings.cfg").write_text("rate = 0.18\nregion = eu\nextra = 1\n", encoding="utf-8")
    assert usernotes.check(shop, n)["status"] == "changed"  # anything added counts
    kept = usernotes.keep(shop, n)
    assert usernotes.check(shop, kept)["status"] == "fresh" and (kept.start, kept.end) == (1, 4)


def test_note_files_are_plain_markdown_that_survive_a_round_trip(shop):
    subject, start, end = _charge(shop)
    body = "---\nnot a header\n---\nline with: colons\n\n```\ncode\n```"
    usernotes.save(shop, subject, "src/billing.py", start, end, body)
    n = usernotes.find(shop, subject)
    assert n.text == body and n.path.read_text(encoding="utf-8").startswith("---\nsubject: src/billing.py::charge()\n")
    with pytest.raises(ValueError):
        usernotes.save(shop, "a\nb", "src/billing.py", 1, 1, "x")
    with pytest.raises(ValueError):
        usernotes.save(shop, "x", "../elsewhere.py", 1, 1, "x")


def test_notes_folder_can_be_configured(shop):
    cfg = shop / ".verinoda" / "config.json"
    data = json.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
    data["notes"] = {"dir": "docs/notes"}
    cfg.write_text(json.dumps(data), encoding="utf-8")
    assert usernotes.notes_dir(shop) == shop / "docs" / "notes"
    usernotes.save(shop, "settings.cfg", "settings.cfg", 1, 2, "Kept with the code.")
    assert (shop / "docs" / "notes").is_dir() and usernotes.find(shop, "settings.cfg") is not None


def test_the_view_shows_and_writes_notes_and_refuses_a_stale_index(shop):
    a = uidata.Atlas(shop)
    hit = next(r for r in a.search("charge")["results"] if r["title"].endswith("charge()"))
    n = a.note(hit["id"])
    assert n["can_note"] and n["user_note"] is None
    out = a.write_user_note(hit["id"], "Rounds after tax.")
    assert out["user_note"]["status"] == "fresh" and a.note(hit["id"])["user_note"]["text"] == "Rounds after tax."
    listed = a.user_notes()["notes"]
    assert [x["id"] for x in listed] == [hit["id"]]
    (shop / "src" / "billing.py").write_text(BILLING + "\n\ndef late():\n    return 1\n", encoding="utf-8")
    with pytest.raises(LookupError):  # the index does not know the file as it is: the lines could be wrong
        a.write_user_note(hit["id"], "Changed text.")
    assert a.write_user_note(hit["id"], "")["user_note"] is None  # deleting needs no lines
    with pytest.raises(KeyError):
        a.write_user_note("no such note", "x")


def _post(port: int, body: dict | bytes, *, token: str | None, origin: str | None = None,
          ctype: str = "application/json") -> tuple[int, dict]:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    headers = {"Host": f"127.0.0.1:{port}", "Content-Type": ctype, "Content-Length": str(len(raw))}
    if token is not None:
        headers["X-Verinoda-Token"] = token
    if origin is not None:
        headers["Origin"] = origin
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("POST", "/api/usernote", body=raw, headers=headers)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    try:
        return r.status, json.loads(data)
    except ValueError:
        return r.status, {}


def _page_token(port: int) -> str:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("GET", "/", headers={"Host": f"127.0.0.1:{port}"})
    html = conn.getresponse().read().decode()
    conn.close()
    return re.search(r'<meta name="verinoda-token" content="([^"]*)">', html).group(1)


def test_the_server_writes_a_note_only_for_its_own_page(shop):
    server, _url = uiserver.start(shop)
    port = server.server_address[1]
    try:
        token = _page_token(port)
        assert len(token) >= 24
        nid = next(r["id"] for r in uidata.Atlas(shop).search("charge")["results"] if r["title"].endswith("charge()"))
        ok = {"id": nid, "text": "From the page."}
        assert _post(port, ok, token=None)[0] == 403
        assert _post(port, ok, token="guess")[0] == 403
        assert _post(port, ok, token=token, origin="http://evil.example")[0] == 403
        assert _post(port, ok, token=token, ctype="text/plain")[0] == 415  # a plain form cannot write
        assert _post(port, b"x" * (uiserver.MAX_BODY + 1), token=token)[0] == 413
        assert _post(port, {"id": 5, "text": "x"}, token=token)[0] == 400
        status, body = _post(port, ok, token=token, origin=f"http://127.0.0.1:{port}")
        assert status == 200 and body["user_note"]["text"] == "From the page."
        assert usernotes.find(shop, "src/billing.py::charge()").text == "From the page."
    finally:
        server.shutdown()
        server.server_close()
    ro, _u = uiserver.start(shop, read_only=True)
    port = ro.server_address[1]
    try:
        assert _page_token(port) == ""
        assert _post(port, {"id": nid, "text": "x"}, token="")[0] == 405
    finally:
        ro.shutdown()
        ro.server_close()


def test_notes_command_lists_and_fails_on_changed_code(shop, capsys):
    from verinoda import cli

    subject, start, end = _charge(shop)
    usernotes.save(shop, subject, "src/billing.py", start, end, "Rounds after tax.")
    assert cli.main(["notes", str(shop), "--changed"]) == 0
    (shop / "src" / "billing.py").write_text(BILLING.replace("round(total, 2)", "round(total, 4)"), encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["notes", str(shop), "--changed"]) == 1
    assert "CHANGED" in capsys.readouterr().out
    assert cli.main(["notes", str(shop), "--keep", subject]) == 0
    assert cli.main(["notes", str(shop), "--changed"]) == 0
    assert cli.main(["notes", str(shop), "--keep", "nope::x()"]) == 2


def test_the_export_carries_the_notes_read_only(shop, tmp_path):
    from verinoda.ui import export

    subject, start, end = _charge(shop)
    usernotes.save(shop, subject, "src/billing.py", start, end, "Rounds after tax.")
    usernotes.save(shop, "src/billing.py", "src/billing.py", 1, 12, "The billing module.")
    data = export.build(shop)
    note = next(n for n in data["notes"].values() if n["file"] == "src/billing.py")
    assert note["user_note"]["text"] == "The billing module."
    assert [u["label"] for u in note["user_notes"]] == ["charge()"]
    assert {u["subject"] for u in data["user_notes"]} == {subject, "src/billing.py"}
    html = export.render(data)
    assert 'content=""' in re.search(r'<meta name="verinoda-token"[^>]*>', html).group(0)  # no way to write


# -- review findings ------------------------------------------------------------------------------

PETS = '''class Cat:
    def speak(self):
        return "meow"


class Dog:
    def speak(self):
        return "woof"
'''


def test_same_named_symbols_and_headings_get_their_own_notes(tmp_path):
    root = tmp_path / "pets"
    root.mkdir()
    (root / "pets.py").write_text(PETS, encoding="utf-8")
    (root / "guide.md").write_text("# Guide\n\n## Usage\n\nA.\n\n## Other\n\n## Usage\n\nB.\n", encoding="utf-8")
    workflow.init(root)
    _scan(root)
    a = uidata.Atlas(root)
    snap = a.snapshot()
    subjects = snap._subjects_in("pets.py")
    cat = next(n for n, s in subjects.items() if s == "pets.py::Cat.speak()")
    dog = next(n for n, s in subjects.items() if s == "pets.py::Dog.speak()")
    a.write_user_note(cat, "Cat says meow.")
    a.write_user_note(dog, "Dog says woof.")
    assert a.note(cat)["user_note"]["text"] == "Cat says meow."
    assert a.note(dog)["user_note"]["text"] == "Dog says woof."
    a.write_user_note(cat, "")
    assert a.note(dog)["user_note"]["text"] == "Dog says woof."  # deleting one leaves the other
    usages = sorted(s for s in snap._subjects_in("guide.md").values() if "Usage" in s)
    assert usages == ["guide.md::Usage", "guide.md::Usage#2"]
    assert all(snap.subject_of(snap.note_for_subject(s))[0] == s for s in usages)


def test_a_hand_edited_note_with_a_bad_header_does_not_break_anything(shop):
    subject, start, end = _charge(shop)
    usernotes.save(shop, subject, "src/billing.py", start, end, "ok")
    d = usernotes.notes_dir(shop)
    (d / "bad.md").write_text('---\nsubject: settings.cfg::rate\nfile: settings.cfg\nlines: 1-1\n'
                              'anchor: {"text":"sha256:x","n_lines":-1000000000}\nwritten: x\n---\nodd\n',
                              encoding="utf-8")
    (d / "worse.md").write_text('---\nsubject: settings.cfg::region\nfile: settings.cfg\nlines: 1-1\n'
                                'anchor: {"text":"sha256:x","n_lines":"x"}\nwritten: x\n---\nodd\n', encoding="utf-8")
    (d / "bom.md").write_bytes("\ufeff---\nsubject: src/billing.py\nfile: src/billing.py\nlines: 1-3\nanchor: {}\n"
                               "written: x\n---\nwith a BOM\n".encode())
    (d / "junk.md").write_text("no header at all", encoding="utf-8")
    import time as _t
    t0 = _t.perf_counter()
    listed = {x["subject"]: x for x in uidata.Atlas(shop).user_notes()["notes"]}
    assert _t.perf_counter() - t0 < 5
    assert listed["settings.cfg::rate"]["status"] in ("changed", "gone")
    assert listed["settings.cfg::region"]["status"] in ("changed", "gone")
    assert listed["src/billing.py"]["text"] == "with a BOM"  # a byte-order mark is read
    assert usernotes.save(shop, subject, "src/billing.py", start, end, "   \n  ") is None  # blank deletes
    assert usernotes.find(shop, subject) is None


def test_a_note_whose_code_is_gone_can_be_deleted(shop):
    from verinoda import cli

    subject, _start, _end = _charge(shop)
    a = uidata.Atlas(shop)
    nid = next(r["id"] for r in a.search("charge")["results"] if r["title"].endswith("charge()"))
    a.write_user_note(nid, "Rounds after tax.")
    (shop / "src" / "billing.py").write_text(BILLING.replace("def charge(", "def bill("), encoding="utf-8")
    st = open_store(shop)
    try:
        workflow.update(st, shop)
    finally:
        st.close()
    listed = uidata.Atlas(shop).user_notes()["notes"]
    assert [(x["subject"], x["status"], x["id"]) for x in listed] == [(subject, "gone", None)]
    assert cli.main(["notes", str(shop), "--changed"]) == 1
    assert cli.main(["notes", str(shop), "--keep", subject]) == 2  # gone: nothing to keep it on
    assert cli.main(["notes", str(shop), "--delete", subject]) == 0
    assert usernotes.find(shop, subject) is None
    usernotes.save(shop, "settings.cfg", "settings.cfg", 1, 1, "x")
    assert uidata.Atlas(shop).delete_user_note("settings.cfg")["deleted"] == "settings.cfg"
    with pytest.raises(KeyError):
        uidata.Atlas(shop).delete_user_note("settings.cfg")


def test_keep_anchors_a_symbol_to_its_current_extent_and_refuses_unparsable_code(shop):
    subject, start, end = _charge(shop)
    usernotes.save(shop, subject, "src/billing.py", start, end, "Rounds after tax.")
    src = shop / "src" / "billing.py"
    src.write_text(BILLING.replace('    """Charge with tax."""\n    total = amount * (1 + rate)\n    return round(total, 2)',
                                   '    return round(amount * (1 + rate), 2)'), encoding="utf-8")
    n = usernotes.find(shop, subject)
    assert usernotes.check(shop, n)["status"] == "changed"
    kept = usernotes.keep(shop, n)
    assert "sym" in kept.anchor and kept.end - kept.start == 1  # the two lines charge() has now
    assert usernotes.check(shop, kept)["status"] == "fresh"
    src.write_text("def charge(:\n", encoding="utf-8")  # does not parse
    assert usernotes.check(shop, usernotes.find(shop, subject))["status"] == "changed"
    with pytest.raises(ValueError):
        usernotes.keep(shop, usernotes.find(shop, subject))
    assert "sym" in usernotes.find(shop, subject).anchor  # not degraded to a line hash


def test_a_reformat_that_shortens_the_last_symbol_keeps_the_note_fresh(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    long_sig = "def first():\n    return 1\n\n\ndef last(\n    a,\n    b,\n):\n    return a + b\n"
    (root / "m.py").write_text(long_sig, encoding="utf-8")
    usernotes.save(root, "m.py::last()", "m.py", 5, 9, "adds")
    (root / "m.py").write_text("def first():\n    return 1\n\n\ndef last(a, b):\n    return a + b\n", encoding="utf-8")
    st = usernotes.check(root, usernotes.find(root, "m.py::last()"))
    assert st["status"] == "fresh" and (st["start"], st["end"]) == (5, 6)
