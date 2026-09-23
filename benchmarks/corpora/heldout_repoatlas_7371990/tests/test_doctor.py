"""doctor: install-layout warning for sandboxed agents, and no secret values in output."""

from __future__ import annotations

import json
import os

from repoatlas import doctor


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
    res = doctor.run(tmp_path)
    assert res["env"]["ANTHROPIC_API_KEY"] == "set"
    assert "SECRET-VALUE" not in json.dumps(res)
    assert os.environ["ANTHROPIC_API_KEY"]  # untouched
