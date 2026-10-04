"""``verinoda improve`` through the CLI (D137): the whole conversation between an agent and a person, in commands."""

from __future__ import annotations

import io
import json

import pytest

from verinoda import cli
from verinoda import improve as im


@pytest.fixture()
def proj(tmp_path):
    p = tmp_path / "proj"
    (p / ".verinoda").mkdir(parents=True)
    return p


def run(proj, capsys, *argv, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(stdin.encode("utf-8"))))
    code = cli.main(["improve", *argv, "--repo", str(proj)])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


LIST = {"subject": "checkout page", "items": [
    {"id": "i1", "group": "problem", "title": "Total ignores the discount", "observation": "pricing.py:12 adds tax first",
     "why": "customers are overcharged", "change": "apply the discount before tax", "evidence": ["pricing.py:12"]},
    {"id": "i2", "group": "improvement", "title": "Split place_order", "observation": "80 lines", "why": "hard to test",
     "change": "extract validate()"},
    {"id": "i3", "group": "taste", "title": "Make Save the only primary button", "observation": "two blue buttons",
     "why": "weak hierarchy", "change": "grey out Cancel"},
]}


def test_the_whole_conversation(proj, capsys, tmp_path, monkeypatch):
    code, out, _ = run(proj, capsys, "start", "the", "checkout", "page")
    assert code == 0 and "Do not change anything yet" in out and "What to look at: the checkout page" in out
    f = tmp_path / "list.json"
    f.write_text(json.dumps(LIST), encoding="utf-8")
    code, out, _ = run(proj, capsys, "propose", "--file", str(f))
    assert code == 0 and "Stored 3 items (1 possible problems, 1 improvements, 1 matters of taste)" in out

    code, out, _ = run(proj, capsys, "questions")
    assert code == 0 and "AskUserQuestion" in out and "request_user_input" in out and "[i1] (problem)" in out
    assert "Check first -> decision=check" in out and "undecided: that is not keep" not in out.split("Batch 1")[1]
    code, out, _ = run(proj, capsys, "questions", "--json")
    q = json.loads(out)
    assert q["questions"] == 3 and [len(b) for b in q["batches"]] == [3]

    code, out, err = run(proj, capsys, "send")
    assert code == 1 and "nothing to send" in err and "keeping alone sends nothing" in err   # nothing chosen: refused

    code, out, _ = run(proj, capsys, "decide", "i1=check", "i2=apply", "i3=keep")
    assert code == 0 and "1 to apply, 1 to check first, 1 to keep" in out
    code, out, _ = run(proj, capsys, "add", "rename", "the", "total")
    assert "Added u1" in out
    code, out, _ = run(proj, capsys, "show")
    assert "[?] i1" in out and "[x] i2" in out and "[-] i3" in out and "[x] u1" in out and "`verinoda improve send` sends 3" in out

    code, out, _ = run(proj, capsys, "send")
    assert code == 0 and "Apply these:" in out and "[i2] Split place_order: extract validate()" in out
    assert "Check these first" in out and "Keep these as they are" in out and "(my own item)" in out
    code, out, err = run(proj, capsys, "decide", "i2=keep")
    assert code == 1 and "being applied" in err

    report = {"outcomes": [{"id": "i2", "status": "applied", "note": "extracted", "evidence": ["service.py:40"]},
                           {"id": "u1", "status": "applied", "note": "renamed"},
                           {"id": "i1", "status": "confirmed", "note": "tax is applied first",
                            "change": "swap the two lines", "evidence": ["pricing.py:12"]}],
              "extra_changes": ["updated one import"]}
    code, out, _ = run(proj, capsys, "report", stdin=json.dumps(report), monkeypatch=monkeypatch)
    assert code == 0 and out.strip() == "Recorded: 3 outcomes."
    code, out, _ = run(proj, capsys, "show", "--detail")
    assert "i2  Split place_order: applied" in out and "Changes that were not on the list" in out
    assert "(verified)" in out and "Checked: tax is applied first" in out and "Change: swap the two lines" in out
    state = im.load(proj)
    assert state["phase"] == "choosing" and im.find(state, "i1")["decision"] == "none"        # back, undecided
    code, out, _ = run(proj, capsys, "reset")
    assert im.load(proj) == im.empty()


def test_bad_input_is_an_error_with_the_reason_and_changes_nothing(proj, capsys, monkeypatch):
    with pytest.raises(SystemExit, match="the list is not readable JSON"):
        run(proj, capsys, "propose", stdin="{broken", monkeypatch=monkeypatch)
    code, out, err = run(proj, capsys, "propose", stdin=json.dumps({"items": []}), monkeypatch=monkeypatch)
    assert code == 1 and "1 to 30" in err
    assert not im.state_path(proj).exists()
    code, out, err = run(proj, capsys, "decide", "nope")
    assert code == 1 and "not ID=DECISION" in err
    code, out, err = run(proj, capsys, "decide", "i9=apply")
    assert code == 1 and "no item i9" in err
    code, out, err = run(proj, capsys, "add", "  ")
    assert code == 1 and "empty" in err
    code, out, err = run(proj, capsys, "ask-report")
    assert code == 1 and "no item is missing" in err


def test_nothing_is_stored_by_questions_show_or_schema(proj, capsys):
    run(proj, capsys, "show")
    run(proj, capsys, "questions")
    code, out, _ = cli.main(["improve", "schema"]), capsys.readouterr().out, None
    assert code == 0 and '"items"' in out
    assert not im.state_path(proj).exists()


def test_the_way_out_when_the_agent_never_reports(proj, capsys, tmp_path, monkeypatch):
    f = tmp_path / "l.json"
    f.write_text(json.dumps(LIST), encoding="utf-8")
    run(proj, capsys, "propose", "--file", str(f))
    run(proj, capsys, "decide", "i2=apply")
    run(proj, capsys, "send")
    code, out, _ = run(proj, capsys, "stop-waiting")
    assert "1 item(s) marked" in out
    code, out, _ = run(proj, capsys, "show")
    assert "no outcome was reported" in out and "ask-report" in out
    code, out, _ = run(proj, capsys, "ask-report")
    assert out.startswith("Report the outcome of the items I sent that you have not reported yet (i2)")
    code, out, _ = run(proj, capsys, "report", stdin=json.dumps({"outcomes": [{"id": "i2", "status": "failed",
                                                                                "note": "tests broke"}]}),
                       monkeypatch=monkeypatch)
    assert code == 0 and im.load(proj)["phase"] == "choosing"


def test_a_new_start_replaces_the_list_and_says_so_but_a_bare_one_only_shows_an_open_list(proj, capsys, tmp_path):
    f = tmp_path / "l.json"
    f.write_text(json.dumps(LIST), encoding="utf-8")
    run(proj, capsys, "propose", "--file", str(f))
    run(proj, capsys, "decide", "i1=apply")
    code, out, _ = run(proj, capsys, "start")                         # a list is open: shown, not thrown away
    assert "Improvement list: checkout page" in out and "still open" in out
    assert im.find(im.load(proj), "i1")["decision"] == "apply"
    code, out, _ = run(proj, capsys, "start", "the", "cart")
    assert "What to look at: the cart" in out and "The earlier list of 3 items was replaced" in out
    assert im.load(proj)["phase"] == "reviewing"
    code, out, _ = run(proj, capsys, "start")                         # nothing open (reviewing): starts anew
    assert "ask me one question first" in out


def test_start_is_refused_while_a_selection_is_out(proj, capsys, tmp_path):
    f = tmp_path / "l.json"
    f.write_text(json.dumps(LIST), encoding="utf-8")
    run(proj, capsys, "propose", "--file", str(f))
    run(proj, capsys, "decide", "i2=apply")
    run(proj, capsys, "send")
    code, out, err = run(proj, capsys, "start", "something", "else")
    assert code == 1 and "a selection is being applied" in err
    assert im.load(proj)["phase"] == "sent"


def test_more_unfolds_a_group(proj, capsys, tmp_path):
    many = {"subject": "x", "items": [{"id": f"i{n}", "group": "problem" if n % 2 else "taste", "title": f"t{n}",
                                       "observation": "o", "why": "w", "change": "c"} for n in range(1, 11)]}
    f = tmp_path / "l.json"
    f.write_text(json.dumps(many), encoding="utf-8")
    run(proj, capsys, "propose", "--file", str(f))
    _, out, _ = run(proj, capsys, "show")
    assert "more (`verinoda improve show --all`)" in out and "i10" not in out
    _, out, _ = run(proj, capsys, "more", "taste")
    assert "i10" in out
