"""The improvement list: a vague "make this better" turned into choices the person can say yes or no to.

People ask for "better", "cleaner", "nicer" without saying what should change. A model that guesses changes things
nobody asked for; one that asks open questions tires the person. ``verinoda improve`` keeps a list between the
agent and the person, in ``.verinoda/improve.json``, in four steps (docs/DESIGN.md D137):

1. ``improve start [WHAT]``: the agent looks at the code and changes **nothing**.
2. ``improve propose``: it hands over a ranked list (group ``problem``, ``improvement`` or ``taste``); nothing is chosen.
3. ``improve questions``: the list as batches of multiple-choice questions for the agent's own question tool
   (``AskUserQuestion`` in Claude Code, ``request_user_input`` in Codex) or as plain text; the person's answers
   are recorded with ``improve decide`` (``apply``, ``keep`` or ``check`` first; ``add`` for an item of their own).
4. ``improve send``: only now does the agent work, on what was chosen; ``improve report`` records what happened.

The rules every function here keeps (the Claude Code mod's pane of the same name, claude-mods/verinoda-live/
IMPROVE-PANE.md in the Symbiosis repository, decided them; the CLI has no pane, so the questions replace it):

* nothing changes without the person's decision; an item with no decision is "not decided", never "keep";
* a suspicion is not a fact: a ``problem`` shows its evidence or says it has none, a ``taste`` the model did not see
  rendered says it is a guess, and "check first" asks for an investigation, not a change;
* the first sight is short (the seven best-ranked items; the rest sit behind a count);
* the work's boundary is what the person chose; changes that were not on the list are reported.

An item's *outcome* is ``None``, ``waiting`` (sent), final (``applied``, ``partial``, ``failed``, ``not_confirmed``)
or ``unreported`` (the agent never said). A check that *confirmed* a problem is not an outcome: the item goes back
into the list, verified, for the person to decide on the fix.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

STATE_NAME = "improve.json"
VERSION = 1
GROUPS = ("problem", "improvement", "taste")
DECISIONS = ("none", "apply", "keep", "check")
FINAL = ("applied", "partial", "failed", "not_confirmed")
REPORT_STATUS = {"apply": ("applied", "partial", "failed"), "check": ("confirmed", "not_confirmed")}
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,16}")
OWN_ID_RE = re.compile(r"u\d+")
MAX_ITEMS = 30
MAX_OWN = 10
FIRST_SIGHT = 7
TITLE_MAX, TEXT_MAX, OWN_MAX, SUBJECT_MAX = 120, 600, 300, 80
EVIDENCE_N, EVIDENCE_MAX, EXTRA_N, EXTRA_MAX = 6, 160, 20, 300
FALLBACK_SUBJECT = "this conversation"
GLYPH = {"none": "[ ]", "apply": "[x]", "keep": "[-]", "check": "[?]"}
GROUP_TITLE = {"problem": "Possible problems", "improvement": "Improvements", "taste": "Matters of taste",
               "own": "Your own items"}
RESULT_WORDS = {"applied": "applied", "partial": "partly applied (see the note)", "failed": "could not be applied",
                "not_confirmed": "checked: the problem was not confirmed",
                "unreported": "no outcome was reported"}


class ImproveError(ValueError):
    """A refusal with the sentence to show; nothing was stored."""


def empty() -> dict:
    return {"version": VERSION, "phase": "idle", "subject": "", "items": [], "extra": [], "own_seq": 0,
            "unfolded": [], "note": ""}


def state_path(repo: Path) -> Path:
    return Path(repo) / ".verinoda" / STATE_NAME


def load(repo: Path) -> dict:
    """The saved list; a missing, unreadable or foreign file is an empty one (it is rewritten by the next save)."""
    try:
        data = json.loads(state_path(repo).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty()
    if not isinstance(data, dict) or data.get("version") != VERSION or not isinstance(data.get("items"), list):
        return empty()
    return {**empty(), **data}


def save(repo: Path, state: dict) -> None:
    p = state_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes((json.dumps(state, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    os.replace(tmp, p)


# -- text helpers -----------------------------------------------------------------------------------------

def _cut(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _text(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _strings(v, n: int, width: int) -> list[str]:
    """At most ``n`` non-empty strings of ``v`` (anything else is dropped, never a refusal)."""
    if not isinstance(v, list):
        return []
    return [_cut(x, width) for x in v if isinstance(x, str) and x.strip()][:n]


# -- lists -------------------------------------------------------------------------------------------------

def model_items(state: dict) -> list[dict]:
    return [i for i in state["items"] if i["group"] != "own"]


def listed(state: dict) -> list[dict]:
    """The list: items whose outcome is ``None`` or ``waiting``."""
    return [i for i in state["items"] if i["outcome"] is None or i["outcome"]["status"] == "waiting"]


def results(state: dict) -> list[dict]:
    return [i for i in state["items"] if i["outcome"] is not None and i["outcome"]["status"] != "waiting"]


def sendable(state: dict) -> list[dict]:
    return [i for i in state["items"] if i["decision"] in ("apply", "check") and i["outcome"] is None]


def waiting(state: dict) -> list[dict]:
    return [i for i in state["items"] if i["outcome"] is not None and i["outcome"]["status"] == "waiting"]


def unreported(state: dict) -> list[dict]:
    return [i for i in state["items"] if i["outcome"] is not None and i["outcome"]["status"] == "unreported"]


def find(state: dict, item_id: str) -> dict | None:
    return next((i for i in state["items"] if i["id"] == item_id), None)


def mark_suffix(item: dict) -> str:
    """What the row says about how far to trust the item: verified, no evidence (a problem), a guess (taste)."""
    if item.get("verified"):
        return " (verified)"
    if item["group"] == "problem" and not item["evidence"]:
        return " (no evidence)"
    if item["group"] == "taste" and not item.get("seen"):
        return " (a guess: not judged from a rendered view)"
    return ""


def first_sight(state: dict, *, everything: bool = False) -> tuple[list[dict], dict[str, int]]:
    """``(the items shown, per group how many are folded)``: of the list's model items the seven best-ranked, plus
    any item with a decision, any verified one, and (``everything``) all of them. The person's own items always show."""
    items = listed(state)
    mine = [i for i in items if i["group"] == "own"]
    ranked = [i for i in items if i["group"] != "own"]
    unfolded = set(state.get("unfolded") or ())
    shown_ids = {i["id"] for i in ranked[:FIRST_SIGHT]}
    shown, hidden = [], {g: 0 for g in GROUPS}
    for i in ranked:
        if everything or i["id"] in shown_ids or i["group"] in unfolded or i["decision"] != "none" or i.get("verified"):
            shown.append(i)
        else:
            hidden[i["group"]] += 1
    return shown + mine, {g: n for g, n in hidden.items() if n}


def counts(state: dict) -> dict[str, int]:
    items = listed(state)
    send = sendable(state)
    return {"items": len(items), "apply": sum(1 for i in send if i["decision"] == "apply"),
            "check": sum(1 for i in send if i["decision"] == "check"),
            "keep": sum(1 for i in items if i["decision"] == "keep")}


# -- the review and the list -------------------------------------------------------------------------------

def review_prompt(what: str) -> str:
    """What the agent is told when the person asks for the list (printed by ``improve start``)."""
    target = (f"What to look at: {what}" if what.strip() else
              "What to look at: what we have been working on in this conversation. If that is not clear, "
              "ask me one question first.")
    return (
        "Make a list of what could be changed here so that I can choose. Do not change anything yet.\n"
        f"{target}\n\n"
        "Look at it first: read the code, and if it has a rendered view you can look at, look at it. Then hand over "
        "the items, ranked, the most worth doing first, with `verinoda improve propose` (JSON on stdin or --file; "
        "`verinoda improve schema` prints the shape). Do not pad the list. Then ask me which ones to apply with "
        "`verinoda improve questions` and stop: change nothing until I have chosen."
    )


def resumes(state: dict, what: str) -> bool:
    """A bare start while a list is open (items to decide on, or an outcome that never came) only shows it."""
    return state["phase"] == "choosing" and not what.strip() and bool(listed(state) or unreported(state))


def start(state: dict, what: str) -> tuple[dict, int]:
    """Begin a review: no model items, no results; the person's own unsent items stay. Returns ``(new state, how many
    items the earlier list had when the person had already decided on some of them)``."""
    if state["phase"] == "sent":
        raise ImproveError("a selection is being applied; its outcomes come first (`verinoda improve report`, or "
                           "`improve stop-waiting` if the agent never reports)")
    old = len(model_items(state))
    held = any(i["decision"] != "none" for i in model_items(state))
    state = {**state, "phase": "reviewing", "subject": _cut(what, SUBJECT_MAX), "extra": [], "unfolded": [],
             "note": "", "items": [i for i in state["items"] if i["group"] == "own" and i["outcome"] is None]}
    return state, (old if held else 0)


def propose(state: dict, data) -> tuple[dict, str]:
    """Store a proposed list. Returns ``(new state, summary)``; raises :class:`ImproveError` and stores nothing."""
    if state["phase"] == "sent":
        raise ImproveError("a selection is being applied; report its outcomes with `verinoda improve report` "
                           "before proposing a new list")
    if not isinstance(data, dict):
        raise ImproveError("the list must be a JSON object {subject, items}; nothing was stored")
    raw = data.get("items")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_ITEMS or not all(isinstance(x, dict) for x in raw):
        raise ImproveError(f"items must be a list of 1 to {MAX_ITEMS} objects; nothing was stored")
    items, seen_ids = [], set()
    for n, x in enumerate(raw, 1):
        iid = x.get("id")
        label = f"item {n}" + (f" ({iid})" if isinstance(iid, str) and iid else "")
        if not isinstance(iid, str) or not ID_RE.fullmatch(iid) or OWN_ID_RE.fullmatch(iid):
            raise ImproveError(f"{label}: id must be 1-16 of A-Z a-z 0-9 _ - and not u<number> (kept for the "
                               "person's own items); nothing was stored")
        if iid in seen_ids:
            raise ImproveError(f"{label}: id {iid} is repeated; nothing was stored")
        seen_ids.add(iid)
        if x.get("group") not in GROUPS:
            raise ImproveError(f"{label}: group must be one of {', '.join(GROUPS)}; nothing was stored")
        for key in ("title", "observation", "why", "change"):
            if not _text(x.get(key)):
                raise ImproveError(f"{label}: {key} must be a non-empty string; nothing was stored")
        items.append({
            "id": iid, "group": x["group"], "title": _cut(x["title"], TITLE_MAX),
            "observation": _cut(x["observation"], TEXT_MAX), "why": _cut(x["why"], TEXT_MAX),
            "change": _cut(x["change"], TEXT_MAX), "cost": _cut(_text(x.get("cost")), TEXT_MAX),
            "evidence": _strings(x.get("evidence"), EVIDENCE_N, EVIDENCE_MAX), "seen": x.get("seen") is True,
            "verified": False, "checked": "", "decision": "none", "sent_as": None, "outcome": None})
    old = model_items(state)
    held = sum(1 for i in old if i["decision"] != "none")
    own_kept = [i for i in state["items"] if i["group"] == "own" and i["outcome"] is None]
    subject = _cut(_text(data.get("subject")), SUBJECT_MAX) or state.get("subject") or FALLBACK_SUBJECT
    new = {**state, "phase": "choosing", "subject": subject, "items": items + own_kept, "extra": [],
           "unfolded": [], "note": ""}
    by = {g: sum(1 for i in items if i["group"] == g) for g in GROUPS}
    msg = (f"Stored {len(items)} items ({by['problem']} possible problems, {by['improvement']} improvements, "
           f"{by['taste']} matters of taste). Nothing is chosen. Ask the person with `verinoda improve questions` "
           "and change nothing until they have answered.")
    if held:
        msg += f" The earlier list ({len(old)} items, {held} decisions) was replaced."
    return new, msg


# -- decisions ---------------------------------------------------------------------------------------------

def decide(state: dict, item_id: str, decision: str) -> None:
    """Set one decision in place. ``none`` clears it. Refused: an unknown id or decision, an item that already has
    an outcome, ``check`` outside a ``problem`` or on a verified item, any change while a selection is out."""
    if decision not in DECISIONS:
        raise ImproveError(f"{decision!r} is not a decision (one of {', '.join(DECISIONS)})")
    if state["phase"] == "sent":
        raise ImproveError("a selection is being applied; nothing can be marked until its outcomes are reported")
    item = find(state, item_id)
    if item is None or item not in listed(state):
        raise ImproveError(f"no item {item_id} is in the list")
    if decision == "check" and (item["group"] != "problem" or item.get("verified")):
        raise ImproveError(f"{item_id} cannot be checked first: only a possible problem that no check has "
                           "confirmed can")
    if item["group"] == "own" and decision != "apply":
        raise ImproveError(f"{item_id} is the person's own item: it is applied or removed (`improve remove`)")
    item["decision"] = decision


def add_own(state: dict, text: str) -> dict:
    if state["phase"] == "sent":
        raise ImproveError("a selection is being applied; add the item after its outcomes are reported")
    text = _cut(text, OWN_MAX)
    if not text:
        raise ImproveError("the item's text is empty")
    if sum(1 for i in state["items"] if i["group"] == "own" and i["outcome"] is None) >= MAX_OWN:
        raise ImproveError(f"at most {MAX_OWN} items of the person's own in the list")
    state["own_seq"] = int(state.get("own_seq") or 0) + 1
    item = {"id": f"u{state['own_seq']}", "group": "own", "title": text, "observation": "", "why": "", "change": text,
            "cost": "", "evidence": [], "seen": True, "verified": False, "checked": "", "decision": "apply",
            "sent_as": None, "outcome": None}
    state["items"].append(item)
    if state["phase"] == "idle":
        state["phase"] = "choosing"
    return item


def remove_own(state: dict, item_id: str) -> None:
    item = find(state, item_id)
    if state["phase"] == "sent":
        raise ImproveError("a selection is being applied; remove the item after its outcomes are reported")
    if item is None or item["group"] != "own" or item["outcome"] is not None:
        raise ImproveError(f"{item_id} is not an item of the person's own that is in the list")
    state["items"].remove(item)


def unfold(state: dict, group: str) -> None:
    if group not in GROUPS:
        raise ImproveError(f"{group!r} is not a group (one of {', '.join(GROUPS)})")
    if group not in state["unfolded"]:
        state["unfolded"].append(group)


# -- the questions (the CLI's replacement for the pane) ----------------------------------------------------

def _option(label: str, description: str, decision: str) -> dict:
    return {"label": label, "description": description, "decision": decision}


def questions(state: dict, *, per_call: int = 3, everything: bool = False, group: str | None = None) -> dict:
    """The undecided items of the first sight as multiple-choice questions, ``per_call`` to a batch.

    One question per item with a single answer: apply it, keep it as it is, or (a possible problem no check has
    confirmed) check it first. Three to a batch because Codex's ``request_user_input`` takes at most three questions
    and Claude Code's ``AskUserQuestion`` four; the person's own "other" answer, or none, leaves an item undecided.
    The agent maps the label picked to the item's ``decision`` and runs ``improve decide``."""
    shown, hidden = first_sight(state, everything=everything)
    todo = [i for i in shown if i["group"] != "own" and i["decision"] == "none" and (group is None or i["group"] == group)]
    qs = []
    for i in todo:
        opts = [_option("Apply", i["change"], "apply")]
        if i["group"] == "problem" and not i.get("verified"):
            opts.append(_option("Check first", "Investigate only; change nothing until the problem is confirmed.",
                                "check"))
        opts.append(_option("Keep as is", "Leave this exactly as it is, also as a side effect.", "keep"))
        detail = [f"Now: {i['observation']}", f"Why: {i['why']}"]
        if i["cost"]:
            detail.append(f"Cost: {i['cost']}")
        if i["evidence"]:
            detail.append("Evidence: " + ", ".join(i["evidence"]))
        elif i["group"] == "problem":
            detail.append("Evidence: none given (a suspicion, not a fact)")
        elif i["group"] == "taste" and not i.get("seen"):
            detail.append("A guess: not judged from a rendered view")
        if i.get("verified"):
            detail.append(f"Checked: {i['checked']}")
        qs.append({"id": i["id"], "header": i["id"], "group": i["group"], "question": f"{i['title']}",
                   "detail": " | ".join(detail), "multiSelect": False, "options": opts})
    n = max(1, int(per_call))
    batches = [qs[k:k + n] for k in range(0, len(qs), n)]
    return {"subject": state.get("subject") or FALLBACK_SUBJECT, "batches": batches, "questions": len(qs),
            "folded": hidden, "ask_own": f"Anything of your own to add? (`verinoda improve add \"...\"`, at most {MAX_OWN})"}


# -- sending and reporting ---------------------------------------------------------------------------------

def selection_prompt(state: dict) -> str:
    """What the agent is told to do after the person's decisions (the sendable items; under Keep, the kept ones)."""
    apply = [i for i in sendable(state) if i["decision"] == "apply"]
    check = [i for i in sendable(state) if i["decision"] == "check"]
    keep = [i for i in listed(state) if i["decision"] == "keep"]
    parts = [f"My decisions on the improvement list ({state.get('subject') or FALLBACK_SUBJECT}):"]
    if apply:
        parts.append("Apply these:\n" + "\n".join(
            f"- [{i['id']}] {i['title']} (my own item)" if i["group"] == "own" else f"- [{i['id']}] {i['title']}: {i['change']}"
            for i in apply))
    if check:
        parts.append("Check these first (investigate only; change nothing for them):\n" + "\n".join(
            f"- [{i['id']}] {i['title']}" for i in check))
    if keep:
        parts.append("Keep these as they are (do not change them, also not as a side effect):\n" + "\n".join(
            f"- [{i['id']}] {i['title']}" for i in keep))
    parts.append(
        "Everything else stays as it is. You may make the small auxiliary changes an item needs. If one would add "
        "behaviour, change behaviour I did not choose, or cost something, tell me before you do it. When you are done "
        "or cannot go on, run `verinoda improve report` with one outcome for each item to apply or to check "
        "(`verinoda improve schema --report` prints the shape), and list under extra_changes any change that was "
        "not on the list.")
    return "\n\n".join(parts)


def report_prompt(ids: list[str]) -> str:
    return (f"Report the outcome of the items I sent that you have not reported yet ({', '.join(ids)}) with "
            "`verinoda improve report`. Change nothing else.")


def send(state: dict) -> str:
    """Mark the sendable items sent (their decision stays) and return the selection prompt."""
    if state["phase"] == "sent":
        raise ImproveError("a selection is already out; report its outcomes first")
    todo = sendable(state)
    if not todo:
        raise ImproveError("nothing to send: choose at least one item to apply or to check first "
                           "(keeping alone sends nothing)")
    prompt = selection_prompt(state)
    for i in todo:
        i["sent_as"] = i["decision"]
        i["outcome"] = {"status": "waiting", "note": "", "evidence": []}
    state["phase"], state["note"] = "sent", ""
    return prompt


def _settle(state: dict) -> None:
    if state["phase"] == "sent" and not waiting(state):
        state["phase"] = "choosing"


def stop_waiting(state: dict) -> int:
    """The way out when the agent never reports: every waiting item becomes ``unreported``."""
    items = waiting(state)
    for i in items:
        i["outcome"] = {"status": "unreported", "note": "", "evidence": []}
    state["note"] = ""
    _settle(state)
    return len(items)


def ask_report(state: dict) -> str:
    items = unreported(state)
    if not items:
        raise ImproveError("no item is missing an outcome")
    for i in items:
        i["outcome"] = {"status": "waiting", "note": "", "evidence": []}
    state["phase"], state["note"] = "sent", ""
    return report_prompt([i["id"] for i in items])


def report(state: dict, data) -> tuple[str, list[str]]:
    """Record outcomes. All or nothing: any invalid outcome records nothing. Returns ``(answer, ids still waiting)``."""
    if not isinstance(data, dict) or not isinstance(data.get("outcomes"), list) or not data["outcomes"] \
            or not all(isinstance(o, dict) for o in data["outcomes"]):
        raise ImproveError("outcomes must list at least one item; nothing was recorded")
    seen, plan = set(), []
    for o in data["outcomes"]:
        iid = o.get("id")
        if iid in seen:
            raise ImproveError(f"item {iid} is reported twice; nothing was recorded")
        seen.add(iid)
        item = find(state, iid) if isinstance(iid, str) else None
        cur = item["outcome"]["status"] if item and item["outcome"] else None
        if item is None or cur not in ("waiting", "unreported"):
            raise ImproveError(f"no item {iid} is waiting for an outcome; nothing was recorded")
        fits = REPORT_STATUS[item["sent_as"] or "apply"]
        if o.get("status") not in fits:
            raise ImproveError(f"item {iid} was sent to {item['sent_as']}; its outcome is one of "
                               f"{', '.join(fits)}; nothing was recorded")
        note = _cut(_text(o.get("note")), TEXT_MAX)
        if not note:
            raise ImproveError(f"item {iid} needs a note the person can read; nothing was recorded")
        plan.append((item, o["status"], note, _cut(_text(o.get("change")), TEXT_MAX),
                     _strings(o.get("evidence"), EVIDENCE_N, EVIDENCE_MAX)))
    for item, status, note, change, evidence in plan:
        if status == "confirmed":
            # a confirmed problem is not an outcome: back into the list, verified, for the person to decide
            item.update(verified=True, checked=note, outcome=None, decision="none", sent_as=None,
                        evidence=(item["evidence"] + evidence)[:EVIDENCE_N])
            if change:
                item["change"] = change
        else:
            item["outcome"] = {"status": status, "note": note, "evidence": evidence}
    state["extra"] = (state["extra"] + _strings(data.get("extra_changes"), EXTRA_N, EXTRA_MAX))[:EXTRA_N]
    _settle(state)
    left = [i["id"] for i in waiting(state)]
    return f"Recorded: {len(plan)} outcomes." + (f" Still waiting for: {', '.join(left)}." if left else ""), left


# -- reading it back ---------------------------------------------------------------------------------------

def _row(item: dict, detail: bool) -> list[str]:
    out = [f"  {GLYPH[item['decision']]} {item['id']}  {item['title']}{mark_suffix(item)}"
           + (" ... waiting" if item["outcome"] and item["outcome"]["status"] == "waiting" else "")]
    if detail and item["group"] != "own":
        out.append(f"        Now: {item['observation']}")
        out.append(f"        Why: {item['why']}")
        out.append(f"        Change: {item['change']}")
        if item["cost"]:
            out.append(f"        Cost: {item['cost']}")
        if item["evidence"]:
            out.append(f"        Evidence: {', '.join(item['evidence'])}")
        elif item["group"] == "problem":
            out.append("        Evidence: none given")
        if item.get("verified"):
            out.append(f"        Checked: {item['checked']}")
    return out


def render(state: dict, *, everything: bool = False, detail: bool = False) -> str:
    phase = state["phase"]
    if phase == "idle" and not state["items"]:
        return "No improvement list. `verinoda improve start [what to look at]` begins one; nothing changes until you choose."
    if phase == "reviewing" and not state["items"]:
        return ("Reviewing: the agent is making the list. Nothing is changed. "
                "(`verinoda improve propose` hands it over.)")
    lines = [f"Improvement list: {state.get('subject') or FALLBACK_SUBJECT}"]
    c = counts(state)
    bits = [f"{c['items']} items"] + [f"{c[k]} {w}" for k, w in (("apply", "to apply"), ("check", "to check first"),
                                                                ("keep", "to keep")) if c[k]]
    lines.append(" · ".join(bits))
    res = results(state)
    if res:
        lines += ["", "Results"]
        for i in res:
            o = i["outcome"]
            lines.append(f"  {i['id']}  {i['title']}: {RESULT_WORDS[o['status']]}")
            if o["note"]:
                lines.append(f"        {o['note']}")
            if o["evidence"]:
                lines.append(f"        {', '.join(o['evidence'])}")
    if state["extra"]:
        lines += ["", "Changes that were not on the list"] + [f"  - {x}" for x in state["extra"]]
    shown, hidden = first_sight(state, everything=everything)
    for g in (*GROUPS, "own"):
        rows = [i for i in shown if i["group"] == g]
        if not rows and not hidden.get(g):
            continue
        lines += ["", GROUP_TITLE[g]]
        for i in rows:
            lines += _row(i, detail)
        if hidden.get(g):
            lines.append(f"  {hidden[g]} more (`verinoda improve show --all`)")
    if phase == "sent":
        lines += ["", "The selection is out; its outcomes show here once reported "
                      "(`improve stop-waiting` if the agent never reports)."]
    elif sendable(state):
        lines += ["", f"`verinoda improve send` sends {len(sendable(state))} chosen item(s)."]
    elif state["items"]:
        lines += ["", "Nothing chosen to send yet: choose at least one item to apply or to check first."]
    if unreported(state):
        lines.append(f"{len(unreported(state))} item(s) have no reported outcome: `verinoda improve ask-report`.")
    return "\n".join(lines)


SCHEMA = {
    "propose": {"subject": "what was looked at, in a few words",
                "items": [{"id": "i1", "group": "problem | improvement | taste", "title": "one concrete line",
                           "observation": "what is there now", "why": "why it may matter",
                           "change": "exactly what you would do and what stays", "cost": "(optional)",
                           "evidence": ["file:line you read (optional; a problem without it says so)"],
                           "seen": "(optional, taste) true only if judged from a rendered view"}]},
    "report": {"outcomes": [{"id": "i1", "status": "applied | partial | failed (sent to apply); "
                                                   "confirmed | not_confirmed (sent to check)",
                             "note": "one or two sentences the person reads", "change": "(confirmed: the fix you propose)",
                             "evidence": ["the check you ran and its result, a file:line"]}],
               "extra_changes": ["every change you made that was not on the list"]},
}
