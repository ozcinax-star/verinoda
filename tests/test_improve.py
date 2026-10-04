"""The improvement list (verinoda/improve.py, D137): a proposed list, the person's decisions, the questions that
carry them, the selection that goes to the agent and the outcomes that come back. Pure state functions first, then
the state file."""

from __future__ import annotations

import json

import pytest

from verinoda import improve as im


def item(i, group="improvement", **kw):
    base = {"id": f"i{i}", "group": group, "title": f"title {i}", "observation": f"obs {i}", "why": f"why {i}",
            "change": f"change {i}"}
    return {**base, **kw}


def listing(n=3, **kw):
    return {"subject": "checkout.py", "items": [item(i, **kw) for i in range(1, n + 1)]}


def stored(data=None):
    state, _ = im.propose(im.empty(), data or listing())
    return state


def test_a_list_is_stored_in_order_trimmed_cut_and_undecided():
    data = {"subject": "  " + "s" * 200, "items": [
        item(1, "problem", title="  " + "t" * 300, cost=7, evidence="not a list", seen="yes"),
        item(2, "taste", evidence=["a.py:1", "", 5, "b.py:2"], seen=True),
    ]}
    state, msg = im.propose(im.empty(), data)
    a, b = state["items"]
    assert [i["id"] for i in state["items"]] == ["i1", "i2"]
    assert len(a["title"]) == im.TITLE_MAX and a["title"].endswith("…")
    assert a["cost"] == "" and a["evidence"] == [] and a["seen"] is False      # wrong types: absent, no refusal
    assert b["evidence"] == ["a.py:1", "b.py:2"] and b["seen"] is True
    assert len(state["subject"]) == im.SUBJECT_MAX
    assert all(i["decision"] == "none" and i["outcome"] is None for i in state["items"])
    assert state["phase"] == "choosing" and "Stored 2 items (1 possible problems, 0 improvements, 1 matters" in msg


def test_subject_falls_back_to_what_was_asked_then_to_the_conversation():
    state, _ = im.start(im.empty(), "the parser")
    got, _ = im.propose(state, {"items": listing()["items"]})
    assert got["subject"] == "the parser"
    got, _ = im.propose(im.empty(), {"items": listing()["items"]})
    assert got["subject"] == im.FALLBACK_SUBJECT


@pytest.mark.parametrize("data, needle", [
    ({"items": []}, "1 to 30"),
    ({"items": [item(i) for i in range(31)]}, "1 to 30"),
    ({"items": [item(1), item(1)]}, "id i1 is repeated"),
    ({"items": [item(1, id="bad id!")]}, "id must be"),
    ({"items": [item(1, id="u1")]}, "id must be"),
    ({"items": [item(1, group="nice")]}, "group must be"),
    ({"items": [item(1, title="  ")]}, "title must be a non-empty string"),
    ({"items": [item(1, change=None)]}, "change must be a non-empty string"),
    ("a string", "JSON object"),
])
def test_an_invalid_list_is_refused_and_the_state_is_as_it_was(data, needle):
    state = stored()
    before = json.dumps(state, sort_keys=True)
    with pytest.raises(im.ImproveError, match=needle):
        im.propose(state, data)
    assert json.dumps(state, sort_keys=True) == before


def test_first_sight_shows_seven_and_counts_what_is_folded():
    data = {"subject": "x", "items": [item(i, "problem" if i % 2 else "improvement") for i in range(1, 13)]}
    state = stored(data)
    shown, hidden = im.first_sight(state)
    assert [i["id"] for i in shown] == [f"i{k}" for k in range(1, 8)]
    assert hidden == {"problem": 2, "improvement": 3}
    # a low-ranked item with a decision, a verified one, and an unfolded group show too
    im.decide(state, "i12", "apply")
    state["items"][10].update(verified=True, checked="seen")
    shown, hidden = im.first_sight(state)
    assert {"i12", "i11"} <= {i["id"] for i in shown}
    assert hidden == {"problem": 1, "improvement": 2}
    im.unfold(state, "problem")
    assert "problem" not in im.first_sight(state)[1]
    assert len(im.first_sight(state, everything=True)[0]) == 12


def test_decisions_and_their_refusals():
    state = stored({"subject": "x", "items": [item(1, "problem"), item(2, "improvement")]})
    im.decide(state, "i1", "check")
    assert im.find(state, "i1")["decision"] == "check"
    im.decide(state, "i1", "none")
    assert im.find(state, "i1")["decision"] == "none"
    with pytest.raises(im.ImproveError, match="cannot be checked first"):
        im.decide(state, "i2", "check")
    with pytest.raises(im.ImproveError, match="no item i9"):
        im.decide(state, "i9", "apply")
    with pytest.raises(im.ImproveError, match="not a decision"):
        im.decide(state, "i1", "maybe")
    state["items"][0].update(verified=True)
    with pytest.raises(im.ImproveError, match="cannot be checked first"):
        im.decide(state, "i1", "check")


def test_questions_carry_only_undecided_items_three_to_a_batch():
    state = stored({"subject": "x", "items": [item(1, "problem"), item(2, "problem", evidence=["a.py:3"]),
                                              item(3, "improvement"), item(4, "taste"), item(5, "improvement")]})
    im.decide(state, "i5", "keep")
    q = im.questions(state)
    assert q["questions"] == 4 and [len(b) for b in q["batches"]] == [3, 1]
    first = q["batches"][0][0]
    assert [o["decision"] for o in first["options"]] == ["apply", "check", "keep"]
    assert "none given (a suspicion, not a fact)" in first["detail"]
    assert "Evidence: a.py:3" in q["batches"][0][1]["detail"]
    imp = q["batches"][0][2]
    assert [o["decision"] for o in imp["options"]] == ["apply", "keep"]       # never "check" outside a problem
    assert "A guess" in q["batches"][1][0]["detail"]                            # an unseen taste says so
    assert im.questions(state, per_call=2)["batches"][0].__len__() == 2
    assert im.questions(state, group="problem")["questions"] == 2


def test_the_selection_names_only_what_was_chosen_and_never_an_undecided_item():
    state = stored({"subject": "cart", "items": [item(1, "problem"), item(2), item(3), item(4)]})
    im.decide(state, "i1", "check")
    im.decide(state, "i2", "apply")
    im.decide(state, "i3", "keep")
    im.add_own(state, "rename the total")
    prompt = im.send(state)
    assert "Apply these:\n- [i2] title 2: change 2\n- [u1] rename the total (my own item)" in prompt
    assert "Check these first (investigate only; change nothing for them):\n- [i1] title 1" in prompt
    assert "Keep these as they are" in prompt and "[i3] title 3" in prompt
    assert "i4" not in prompt                                                    # undecided: not named
    assert state["phase"] == "sent"
    assert [i["outcome"]["status"] for i in state["items"] if i["id"] in ("i1", "i2", "u1")] == ["waiting"] * 3
    with pytest.raises(im.ImproveError, match="already out"):
        im.send(state)


def test_an_empty_section_is_left_out_and_keeping_alone_sends_nothing():
    state = stored({"subject": "x", "items": [item(1), item(2)]})
    im.decide(state, "i1", "keep")
    with pytest.raises(im.ImproveError, match="keeping alone sends nothing"):
        im.send(state)
    im.decide(state, "i2", "apply")
    prompt = im.send(state)
    assert "Check these first" not in prompt


def test_own_items_are_applied_removed_and_never_reuse_an_id():
    state = stored()
    a = im.add_own(state, "  my wish  ")
    assert a["id"] == "u1" and a["decision"] == "apply" and a["title"] == "my wish"
    im.remove_own(state, "u1")
    assert im.add_own(state, "another")["id"] == "u2"
    with pytest.raises(im.ImproveError, match="own item"):
        im.decide(state, "u2", "keep")
    with pytest.raises(im.ImproveError, match="empty"):
        im.add_own(state, "   ")
    for k in range(im.MAX_OWN - 1):
        im.add_own(state, f"x{k}")
    with pytest.raises(im.ImproveError, match="at most 10"):
        im.add_own(state, "one too many")
    with pytest.raises(im.ImproveError, match="not an item of the person's own"):
        im.remove_own(state, "i1")


def sent_state():
    state = stored({"subject": "x", "items": [item(1), item(2, "problem"), item(3)]})
    im.decide(state, "i1", "apply")
    im.decide(state, "i2", "check")
    im.send(state)
    return state


def test_a_report_lands_on_each_item_and_the_list_returns_to_choosing():
    state = sent_state()
    answer, left = im.report(state, {"outcomes": [{"id": "i1", "status": "applied", "note": "done",
                                                  "evidence": ["a.py:1"]}], "extra_changes": ["fixed a typo", 5]})
    assert answer == "Recorded: 1 outcomes. Still waiting for: i2." and left == ["i2"]
    assert state["phase"] == "sent" and state["extra"] == ["fixed a typo"]
    answer, _ = im.report(state, {"outcomes": [{"id": "i2", "status": "not_confirmed", "note": "fine as is"}]})
    assert answer == "Recorded: 1 outcomes." and state["phase"] == "choosing"
    assert im.find(state, "i2")["outcome"]["status"] == "not_confirmed"
    with pytest.raises(im.ImproveError, match="no item i1 is waiting"):
        im.report(state, {"outcomes": [{"id": "i1", "status": "applied", "note": "again"}]})


def test_a_confirmed_check_puts_the_item_back_verified_and_undecided():
    state = sent_state()
    im.report(state, {"outcomes": [
        {"id": "i2", "status": "confirmed", "note": "the loop reads past the end", "change": "stop at len - 1",
         "evidence": ["b.py:9"]}, {"id": "i1", "status": "applied", "note": "ok"}]})
    i2 = im.find(state, "i2")
    assert i2["verified"] and i2["checked"] == "the loop reads past the end" and i2["outcome"] is None
    assert i2["decision"] == "none" and i2["sent_as"] is None and i2["change"] == "stop at len - 1"
    assert i2["evidence"] == ["b.py:9"] and im.mark_suffix(i2) == " (verified)"
    assert i2 in im.listed(state) and state["phase"] == "choosing"
    with pytest.raises(im.ImproveError):                       # it accepts no report until it is sent again
        im.report(state, {"outcomes": [{"id": "i2", "status": "applied", "note": "x"}]})


@pytest.mark.parametrize("outcomes, needle", [
    ([], "at least one"),
    ([{"id": "i1", "status": "applied", "note": "a"}, {"id": "i1", "status": "failed", "note": "b"}], "reported twice"),
    ([{"id": "i3", "status": "applied", "note": "a"}], "no item i3 is waiting"),
    ([{"id": "nope", "status": "applied", "note": "a"}], "no item nope is waiting"),
    ([{"id": "i1", "status": "confirmed", "note": "a"}], "was sent to apply; its outcome is one of applied"),
    ([{"id": "i2", "status": "applied", "note": "a"}], "was sent to check; its outcome is one of confirmed"),
    ([{"id": "i1", "status": "applied", "note": "  "}], "needs a note"),
])
def test_a_report_with_any_invalid_outcome_records_nothing(outcomes, needle):
    state = sent_state()
    before = json.dumps(state, sort_keys=True)
    with pytest.raises(im.ImproveError, match=needle):
        im.report(state, {"outcomes": outcomes})
    assert json.dumps(state, sort_keys=True) == before


def test_the_way_out_when_the_agent_never_reports():
    state = sent_state()
    assert im.stop_waiting(state) == 2 and state["phase"] == "choosing"
    assert [i["outcome"]["status"] for i in im.unreported(state)] == ["unreported"] * 2
    text = im.render(state)
    assert "no outcome was reported" in text and "ask-report" in text
    prompt = im.ask_report(state)
    assert prompt.startswith("Report the outcome of the items I sent that you have not reported yet (i1, i2)")
    assert state["phase"] == "sent"
    im.report(state, {"outcomes": [{"id": "i1", "status": "failed", "note": "could not"}]})   # still accepted
    with pytest.raises(im.ImproveError, match="no item is missing"):
        im.ask_report(stored())


def test_a_new_list_during_a_selection_is_refused_and_during_choosing_it_replaces():
    state = sent_state()
    with pytest.raises(im.ImproveError, match="being applied"):
        im.propose(state, listing())
    state = stored({"subject": "x", "items": [item(1), item(2)]})
    im.decide(state, "i1", "apply")
    im.add_own(state, "mine")
    new, msg = im.propose(state, listing(2))
    assert msg.endswith("The earlier list (2 items, 1 decisions) was replaced.")
    assert [i["id"] for i in new["items"]] == ["i1", "i2", "u1"]               # the person's own item is kept
    assert all(i["decision"] == "none" for i in new["items"] if i["group"] != "own")


def test_start_keeps_own_items_and_says_what_it_replaced():
    state = stored({"subject": "x", "items": [item(1), item(2)]})
    im.decide(state, "i1", "keep")
    im.add_own(state, "mine")
    new, replaced = im.start(state, "the cart")
    assert new["phase"] == "reviewing" and new["subject"] == "the cart" and replaced == 2
    assert [i["id"] for i in new["items"]] == ["u1"]
    assert "What to look at: the cart" in im.review_prompt("the cart")
    assert "ask me one question first" in im.review_prompt("")


def test_render_marks_trust_and_folds(capsys):
    state = stored({"subject": "ui", "items": [item(1, "problem"), item(2, "taste"),
                                               item(3, "taste", seen=True), item(4, "problem", evidence=["a:1"])]})
    text = im.render(state, detail=True)
    assert "Possible problems" in text and "Matters of taste" in text
    assert "(no evidence)" in text and "(a guess: not judged from a rendered view)" in text
    assert "Evidence: none given" in text and "[ ] i4" in text
    assert "Nothing chosen to send yet" in text
    im.decide(state, "i1", "apply")
    assert "`verinoda improve send` sends 1" in im.render(state)
    assert im.render(im.empty()).startswith("No improvement list")


def test_the_state_file_round_trips_and_a_foreign_file_reads_as_empty(tmp_path):
    state = stored()
    im.save(tmp_path, state)
    assert im.load(tmp_path) == state
    im.state_path(tmp_path).write_text("[1, 2]", encoding="utf-8")
    assert im.load(tmp_path) == im.empty()
    im.state_path(tmp_path).write_text("{broken", encoding="utf-8")
    assert im.load(tmp_path) == im.empty()
