"""`verinoda decide undocumented` / `decide dismiss`: structural choices no decision record covers
(verinoda/undocumented.py).

Each test works on its own copy of a small project: storage in one file, one library behind one file, the
environment read in one file, and a library imported in two files (no candidate)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from verinoda import cli
from verinoda import decision_brief as dbr
from verinoda import decisions as dm
from verinoda import undocumented as ud
from verinoda import workflow
from verinoda.store import open_store

FILES = {
    "requirements.txt": "requests\npyyaml\n",
    "store/__init__.py": "",
    "store/db.py": ("import sqlite3\n\n\ndef connect(path):\n    return sqlite3.connect(path)\n\n\n"
                    "def save(conn, x):\n    conn.execute(\"INSERT INTO t VALUES (?)\", (x,))\n    conn.commit()\n"),
    "net/__init__.py": "",
    "net/client.py": "import requests\n\n\ndef fetch(url):\n    return requests.get(url).text\n",
    "app/__init__.py": "",
    "app/config.py": ("import os\n\nDB = os.environ.get(\"APP_DB\", \"app.db\")\n"
                      "PORT = os.getenv(\"APP_PORT\", \"8000\")\n"),
    "app/main.py": ("import yaml\n\nfrom app.config import DB\nfrom net.client import fetch\nfrom store.db import "
                    "connect\n\n\ndef run():\n    # import requests would be a comment\n"
                    "    return connect(DB), fetch(\"x\"), yaml.safe_load(\"a: 1\")\n"),
    "app/settings.py": "import yaml\n\n\ndef load(text):\n    return yaml.safe_load(text)\n",
    "tests/test_client.py": "import requests\n\n\ndef test_fetch():\n    assert requests\n",
}


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    repo = tmp_path_factory.mktemp("undoc") / "shop"
    for rel, text in FILES.items():
        _write(repo, rel, text)
    workflow.init(repo)
    return repo


@pytest.fixture
def repo(base, tmp_path):
    dst = tmp_path / "shop"
    shutil.copytree(base, dst)
    return dst


def _ids(res: dict) -> list[str]:
    return [c["id"] for c in res["candidates"]]


def test_lists_one_storage_path_an_exclusive_library_and_one_config_reader(repo):
    res = ud.find(repo)
    assert _ids(res) == ["storage:store/db.py", "library:requests", "config:app/config.py"]
    by = {c["id"]: c for c in res["candidates"]}
    for c in res["candidates"]:
        assert c["status"] == "weak_inference" and c["fact"]["status"] == "statically_verified"
        assert c["fact"]["evidence"] and all(dbr.recheck(repo, e) for e in c["fact"]["evidence"])
        assert c["dismiss_with"] == f"verinoda decide dismiss {c['id']} --reason \"<the user's reason>\""
        # the proposed guard is one `decide record --guard` accepts
        dm.parse_guard(c["guard"], repo, "g1")
    st = by["storage:store/db.py"]
    assert "db-connection" in st["fact"]["text"] and "sql-write" in st["fact"]["text"]
    assert st["guard"] == "only_in sink=db-connection allowed=store/db.py"
    lib = by["library:requests"]
    # a test file importing it and a comment naming it do not count against "one product file"
    assert "net/client.py:1" in lib["fact"]["text"]
    assert [e["locator"] for e in lib["fact"]["evidence"]] == ["net/client.py:1", "requirements.txt:1"]
    assert "--guard" in lib["record_with"] and "<the user's choice>" in lib["record_with"]
    assert by["config:app/config.py"]["fact"]["text"].startswith("all 2 environment read(s)")
    # pyyaml is imported by two product files: no candidate
    assert not any("yaml" in i for i in _ids(res))
    assert res["searched"]["records"] == 0 and res["covered"] == [] and res["dismissed"] == []


def test_a_record_or_an_adr_that_names_the_choice_covers_it(repo):
    st = open_store(repo)
    try:
        rec = dm.record(st, repo, chosen="SQLite", rationale="one local file", title="Keep SQLite in the store",
                        guards=["only_in calls=sqlite3.connect allowed=store/**"])
    finally:
        st.close()
    _write(repo, "docs/adr/0001-http.md", "# HTTP\n\nStatus: accepted\n\nWe call services with Requests only.\n")
    res = ud.find(repo)
    assert _ids(res) == ["config:app/config.py"]
    cov = {c["id"]: c["by"][0] for c in res["covered"]}
    assert cov["storage:store/db.py"]["record"] == rec["id"]
    # the record's guard glob store/** matches the file; the line it cites holds that glob
    at, line = cov["storage:store/db.py"]["at"].rsplit(":", 1)
    assert cov["storage:store/db.py"]["via"] == "a guard's glob store/** matches store/db.py"
    assert "store/**" in (repo / at).read_text(encoding="utf-8").split("\n")[int(line) - 1]
    assert cov["library:requests"] == {"document": "docs/adr/0001-http.md", "at": "docs/adr/0001-http.md:5",
                                       "via": "names requests"}
    assert res["searched"] == {**res["searched"], "records": 1, "documents": 1}


def test_a_rejected_record_covers_nothing(repo):
    st = open_store(repo)
    try:
        rec = dm.record(st, repo, chosen="requests", rationale="x", title="Use requests in net/client.py")
    finally:
        st.close()
    path = next((repo / ".verinoda" / "decisions").glob("*.md"))
    path.write_text(path.read_text(encoding="utf-8").replace("status: accepted", "status: rejected"),
                    encoding="utf-8", newline="\n")
    assert rec["id"] and "library:requests" in _ids(ud.find(repo))


def test_a_dismissal_is_kept_locally_and_can_be_undone(repo):
    with pytest.raises(ud.UndocumentedError, match="--reason"):
        ud.dismiss(repo, "config:app/config.py", "")
    with pytest.raises(ud.UndocumentedError, match="not a current candidate"):
        ud.dismiss(repo, "library:pyyaml", "two files")
    out = ud.dismiss(repo, "config:app/config.py", "a script, not a service")
    assert out["reason"] == "a script, not a service" and out["file"].endswith(".verinoda/dismissed_decisions.json")
    with pytest.raises(ud.UndocumentedError, match="already"):
        ud.dismiss(repo, "config:app/config.py", "again")
    res = ud.find(repo)
    assert "config:app/config.py" not in _ids(res)
    assert res["dismissed"][0]["id"] == "config:app/config.py" and res["dismissed"][0]["current"] is True
    # the candidate goes away (the environment is read in a second file): the dismissal says so
    _write(repo, "app/main.py", FILES["app/main.py"] + "\nimport os\nMODE = os.getenv(\"APP_MODE\")\n")
    assert ud.find(repo)["dismissed"][0]["current"] is False
    ud.dismiss(repo, "config:app/config.py", None, undo=True)
    assert ud.load_dismissed(repo) == []
    with pytest.raises(ud.UndocumentedError, match="not dismissed"):
        ud.dismiss(repo, "config:app/config.py", None, undo=True)


def test_a_library_loaded_by_name_elsewhere_is_not_kept_behind_one_file(repo):
    _write(repo, "app/plugins.py", "# \"requests\" in a comment is no load\n")
    assert "library:requests" in _ids(ud.find(repo))
    _write(repo, "app/plugins.py", "import importlib\n\n\ndef http():\n    return importlib.import_module(\"requests\")\n")
    assert "library:requests" not in _ids(ud.find(repo))


def test_an_unreadable_dismissal_list_is_an_error(repo):
    ud.dismissed_path(repo).write_text("{}", encoding="utf-8")
    with pytest.raises(ud.UndocumentedError, match="not a Verinoda dismissal list"):
        ud.find(repo)


def test_a_small_project_gives_no_candidate(tmp_path):
    for rel in ("requirements.txt", "store/db.py", "net/client.py"):
        _write(tmp_path, rel, FILES[rel])
    res = ud.find(tmp_path)
    assert res["candidates"] == [] and res["searched"]["product_files"] == 2


def test_cli_lists_and_dismisses(repo, capsys):
    assert cli.main(["decide", "undocumented", "--repo", str(repo), "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert [c["id"] for c in res["candidates"]][:2] == ["storage:store/db.py", "library:requests"]
    assert not any(k.startswith("_") for c in res["candidates"] for k in c)
    assert cli.main(["decide", "dismiss", "library:requests", "--reason", "only a demo client",
                     "--repo", str(repo)]) == 0
    assert "dismissed library:requests" in capsys.readouterr().out
    assert cli.main(["decide", "undocumented", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "2 candidate(s), weak_inference" in out and "dismissed library:requests" in out
    assert "record: verinoda decide record" in out
    assert cli.main(["decide", "dismiss", "library:nothing", "--reason", "x", "--repo", str(repo)]) == 2
    assert "not a current candidate" in capsys.readouterr().err
