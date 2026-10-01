"""What the project says about one file (verinoda/scoped.py): decision records whose guards name it, the user's
notes on it or on a glob matching it, and Cursor/Kiro rules for it - for ``verinoda context`` and the Read/Edit
hook's ``read_context``."""

from __future__ import annotations

import json
from pathlib import Path

from verinoda import cli, scoped
from verinoda import decisions as dm
from verinoda.store import open_store


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _project(tmp_path: Path) -> Path:
    from verinoda import workflow

    r = tmp_path / "scoped ğ proj"
    _write(r, "app/ui/views.py", "def show():\n    return 1\n")
    _write(r, "app/db/store.py", "import sqlite3\n\n\ndef save(x):\n    return sqlite3.connect(x)\n")
    _write(r, "other/x.py", "y = 1\n")
    workflow.init(r)
    return r


def test_decisions_notes_and_rules_that_name_a_file(tmp_path):
    r = _project(tmp_path)
    st = open_store(r)
    try:
        dm.record(st, r, chosen="sqlite only in the store", rationale="one place writes",
                  guards=["only_in calls=sqlite3.connect allowed=app/db/store.py", "layers order=app/ui/**,app/db/**"],
                  user_statement="yes")
    finally:
        st.close()
    _write(r, ".verinoda/notes/db-rules.md", "---\nscope: app/db/**\nwritten: 2026-10-01\n---\n"
                                             "Every write goes through save(); keep it the only one.\n")
    _write(r, ".cursor/rules/db.mdc", "---\ndescription: db\nglobs: app/db/**, migrations/**\nalwaysApply: false\n---\n"
                                      "# Database\nUse parameterised SQL only.\n")
    _write(r, ".cursor/rules/always.mdc", "---\nalwaysApply: true\n---\nBe nice.\n")
    _write(r, ".kiro/steering/ui.md", "---\ninclusion: fileMatch\nfileMatchPattern: \"app/ui/**\"\n---\nUI rule.\n")
    res = scoped.for_file(r, "app/db/store.py")
    kinds = sorted(i["kind"] for i in res["items"])
    assert kinds == ["cursor rule", "decision", "decision", "scoped note"], res
    dec = [i for i in res["items"] if i["kind"] == "decision"]
    assert {(d["rule"], d["field"]) for d in dec} == {("only_in", "allowed"), ("layers", "order")}
    note = next(i for i in res["items"] if i["kind"] == "scoped note")
    assert note["text"].startswith("Every write goes through save()") and note["at"] == ".verinoda/notes/db-rules.md:5"
    rule = next(i for i in res["items"] if i["kind"] == "cursor rule")
    assert rule["text"] == "Use parameterised SQL only." and rule["glob"] == "app/db/**"
    text = scoped.text(res)
    assert text.startswith("verinoda: what this project says about app/db/store.py:") and "only_in" in text
    # a file nothing names: nothing (the hook adds nothing)
    assert scoped.for_file(r, "other/x.py")["items"] == [] and scoped.text(scoped.for_file(r, "other/x.py")) == ""
    ui = scoped.for_file(r, str(r / "app" / "ui" / "views.py"))      # an absolute path, as the hook passes it
    assert {i["kind"] for i in ui["items"]} == {"decision", "kiro steering"}
    assert scoped.for_file(r, str(tmp_path / "elsewhere.py"))["outside"]


def test_text_is_capped_between_items(tmp_path):
    res = {"file": "a.py", "items": [{"kind": "cursor rule", "glob": "**", "text": "x" * 150, "at": f"r{i}.mdc:1"}
                                     for i in range(20)]}
    text = scoped.text(res, limit=700)
    assert len(text) <= 760 and "more (`verinoda context a.py`)" in text


def test_mcp_hook_output_and_cli(tmp_path, capsys):
    from verinoda.mcp.server import AtlasTools

    r = _project(tmp_path)
    _write(r, ".verinoda/notes/ui.md", "---\nscope: app/ui/*.py\n---\nViews never touch the database.\n")
    out = AtlasTools(r).read_context(str(r / "app/ui/views.py"))
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "Views never touch the database." in out["hookSpecificOutput"]["additionalContext"]
    assert AtlasTools(r).read_context("other/x.py") == {} and AtlasTools(r).read_context("") == {}
    assert cli.main(["context", "app/ui/views.py", "--repo", str(r), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["items"][0]["kind"] == "scoped note"
    assert cli.main(["context", "other/x.py", "--repo", str(r)]) == 0
    assert "nothing in this project names other/x.py" in capsys.readouterr().out


def test_globs_as_editors_read_them():
    assert scoped.rule_match("src/a.ts", "src/**/*.ts") and scoped.rule_match("a.ts", "**/*.ts")
    assert scoped.rule_match("src/x/y/b.tsx", "src/**/*.{ts,tsx}") and not scoped.rule_match("src/a.js", "src/**/*.{ts,tsx}")
    assert scoped.rule_match("app/db/x.py", "app/db/**") and not scoped.rule_match("app/dbx/x.py", "app/db/*")
    assert scoped._globs('["src/**/*.{ts,tsx}", lib/**]') == ["src/**/*.{ts,tsx}", "lib/**"]
    meta, body, line = scoped._front("---\nglobs:\n  - a/**\n  - \"b/**\"\n--- \nText\n")
    assert scoped._globs(meta["globs"]) == ["a/**", "b/**"] and body.strip() == "Text" and line == 6
    assert scoped._front("---\nglobs: a\n---")[0] == {"globs": "a"}


def test_rule_variants_and_workspace_rules(tmp_path):
    import subprocess

    top = tmp_path / "ws"
    r = top / "sub proj"
    _write(r, "app/db/store.py", "x = 1\n")
    subprocess.run(["git", "init", "-q", str(top)], check=True, capture_output=True)
    _write(r, ".kiro/steering/q.md", '---\ninclusion: "fileMatch"\nfileMatchPattern: "app/**/*.py"\n---\nQuoted.\n')
    _write(r, ".cursor/rules/both.mdc", "---\nalwaysApply: true\nglobs: app/**\n---\nAlways.\n")
    _write(r, ".verinoda/notes/both.md", "---\nsubject: x\nfile: other.py\nscope: app/db/*\n---\nBoth headers.\n")
    _write(top, ".cursor/rules/root.mdc", "---\nglobs: sub proj/app/**\n---\n# Root\nFrom the workspace.\n")
    res = scoped.for_file(r, "app/db/store.py")
    got = {i["kind"]: i["text"] for i in res["items"]}
    assert got == {"kiro steering": "Quoted.", "scoped note": "Both headers.", "cursor rule": "From the workspace."}


def test_sources_are_kept_until_a_file_changes(tmp_path, monkeypatch):
    r = tmp_path / "cache"
    _write(r, ".verinoda/notes/n.md", "---\nscope: **\n---\nFirst.\n")
    assert scoped.for_file(r, "a.py")["items"][0]["text"] == "First."
    calls = []
    real = scoped._front
    monkeypatch.setattr(scoped, "_front", lambda t: calls.append(1) or real(t))
    scoped.for_file(r, "a.py")
    assert calls == []                      # nothing changed: nothing re-read
    import os
    import time

    _write(r, ".verinoda/notes/n.md", "---\nscope: **\n---\nSecond, longer.\n")
    st = (r / ".verinoda/notes/n.md").stat()
    os.utime(r / ".verinoda/notes/n.md", ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    time.sleep(0.01)
    assert scoped.for_file(r, "a.py")["items"][0]["text"] == "Second, longer."
