"""What a model reads of an analysis: the answer and its evidence, not the run (docs/DESIGN.md D20).

:func:`analysis.analyze` returns the full record (plan check, per-sub-question plumbing, steps, usage,
every claim with every field); ``verinoda analyze --json`` prints it. A model answering the user
needs less, and every token of plumbing is a token not spent on evidence. Two views of the same
result, built by one set of rules:

* :func:`render_text` - the default output of ``verinoda analyze``: the snapshot, how the question
  was understood, one block per sub-question (verdict, answer claims, unknowns), the other claims,
  and the passages ``verinoda query`` gives for the question, as plain text;
* :func:`lean` - the MCP ``analyze`` response: the same content as compact JSON.

Rules (nothing an answer rests on is dropped; the full record stays one ``--json`` away):

* claims keep id, status and text; confidence only where it differs from the status's cap
  (``claims.CONFIDENCE_CAP``), evidence only where it adds a locator the text does not carry
  (a ``supports`` locator the text quotes, or a graph edge restating a relation the text states
  with its call site, is left out), uncertainties and "not challenged" reasons always;
* a verified context claim (not an answer claim) whose cited lines all lie in a window the
  passages print is left out and counted: the passage shows those lines;
* the plan's links stay (where the question's words resolved in the code: a locator), weak ones go;
* dropped: steps, usage (unless a budget ran out), the question echo, plan id/source, per
  sub-question done_when / links_used / retrieval / handler / claim_ids, weak plan links.
"""

from __future__ import annotations

import re
from typing import Callable

from verinoda.claims import CONFIDENCE_CAP

# verified statuses whose cited lines are the whole support: a printed passage shows the same
LINE_VERIFIED = ("statically_verified", "primary_source_verified")
_LOC_RX = re.compile(r"([A-Za-z0-9_.][\w.\-/]*\.[A-Za-z0-9_]+:\d+(?:-\d+)?)")
_HEAD_RX = re.compile(r"^## (\S+):(\d+)-(\d+)(?: |$)")
_SUB_RX = re.compile(r"^  (\S+):(\d+)-(\d+)$")
_ITEM_LINE_RX = re.compile(r"^\S+:\d+-\d+ ")
_META = ("  doc: ", "  names: ", "  named by: ", "  same content: ", "  calls: ", "  called by: ", "  const ")
_NOT_BODY = ("## ", "… ", "(calls / called by", "not indexed, and", "next: ", "expanded: ",
             "changed since indexing", "no candidate locations")


def _evidence(c: dict) -> list[str]:
    """Evidence strings that add something to the claim's text (see the module rules)."""
    text = c.get("text") or ""
    out = []
    for e in c.get("evidence") or []:
        role, _, rest = e.partition(":")
        kind, _, where = rest.partition(":")
        if role == "supports":
            if kind == "graph_edge" and _LOC_RX.search(text):
                continue  # the relation and its call site are the text itself
            m = _LOC_RX.search(where)
            if m and m.group(1) in _LOC_RX.findall(text):  # the same locator, not one it is a prefix of
                continue
            out.append(f"{kind}:{where}")
        else:
            out.append(e)
    return out


def _confidence(c: dict) -> float | None:
    conf = c.get("confidence")
    if conf is None or CONFIDENCE_CAP.get(c.get("status")) == conf:
        return None
    return conf


def lean_claim(c: dict) -> dict:
    """A claim as the lean views carry it."""
    out = {"id": c["id"], "status": c["status"], "text": c["text"]}
    conf = _confidence(c)
    if conf is not None:
        out["confidence"] = conf
    ev = _evidence(c)
    if ev:
        out["evidence"] = ev
    if c.get("uncertainties"):
        out["uncertainties"] = c["uncertainties"]
    if c.get("challenged") is False:
        out["not_challenged"] = c.get("not_challenged_reason") or "yes"
    return out


def printed_windows(passages: list[str]) -> list[tuple[str, int, int]]:
    """Line ranges whose source lines the passages print (``retrieval.render_text`` layout).

    A ``  path:x-y`` sub-window counts (its lines follow it in one block); an item's ``## path:a-b``
    header counts only when its body follows directly (after its doc/calls/... lines). A window
    covers only the lines that follow it: passages cut short (the MCP response cap keeps a prefix)
    print a window's first lines, and only those count."""
    out = []
    for i, ln in enumerate(passages):
        m = _SUB_RX.match(ln)
        j = i + 1
        if not m:
            m = _HEAD_RX.match(ln)
            if not m:
                continue
            while j < len(passages) and passages[j].startswith(_META):
                j += 1
            if j < len(passages) and (_SUB_RX.match(passages[j]) or _ITEM_LINE_RX.match(passages[j])):
                continue  # a sub-window or the next item follows: the header prints no body
        path, lo, hi = m.group(1), int(m.group(2)), int(m.group(3))
        n = 0
        while j + n < len(passages) and n < hi - lo + 1 and not _block_start(passages[j + n]):
            n += 1
        if n:
            out.append((path, lo, lo + n - 1))
    return out


def _block_start(ln: str) -> bool:
    """Does ``ln`` start a new block of the text (not a source line of the window above it)?"""
    return bool(ln.startswith(_NOT_BODY) or _SUB_RX.match(ln) or _ITEM_LINE_RX.match(ln))


def _covered(loc: str, wins: list[tuple[str, int, int]]) -> bool:
    path, _, rng = loc.rpartition(":")
    a, _, b = rng.partition("-")
    lo, hi = int(a), int(b or a)
    return any(p == path and x <= lo and hi <= y for p, x, y in wins)


def answer_ids(res: dict) -> list[str]:
    return list(dict.fromkeys(cid for s in res.get("subquestions") or [] for cid in s.get("answer_claim_ids") or []))


def shown_by_passages(res: dict) -> set[str]:
    """Ids of verified context claims whose cited lines all lie in windows the passages print."""
    wins = printed_windows(res.get("passages") or [])
    if not wins:
        return set()
    answering = set(answer_ids(res))
    out = set()
    for c in res.get("claims") or []:
        if c["id"] in answering or c.get("status") not in LINE_VERIFIED:
            continue
        locs = _LOC_RX.findall(c.get("text") or "")
        if not locs:
            locs = [x for e in c.get("evidence") or [] for x in _LOC_RX.findall(e.split(":", 2)[-1])]
        if locs and all(_covered(loc, wins) for loc in locs):
            out.add(c["id"])
    return out


# -- MCP: lean JSON ------------------------------------------------------------------------------

def _link(lk: dict, *, untagged: str | None = None) -> str | None:
    """One plan link as a line (None for a weak one); a link whose status is ``untagged`` goes without it."""
    st = lk.get("status")
    if st == "weak":
        return None
    if st == "not_found":
        dym = lk.get("did_you_mean") or []
        return f"{lk.get('text')}: not_found" + (f" (did you mean {', '.join(map(str, dym[:3]))})" if dym else "")
    tag = "" if st == untagged else f" ({st})"
    if lk.get("at"):
        return f"{lk.get('text')} -> {lk['at']}{tag}"
    return f"{lk.get('text')}{tag}"


def lean_plan_check(pc: dict) -> dict:
    out: dict = {"status": pc.get("status")}
    for k in ("errors", "warnings", "clarifications", "references", "intent_divergence"):
        if pc.get(k):
            out[k] = pc[k]
    links = [x for x in (_link(lk) for lk in pc.get("links") or []) if x]
    if links:
        out["links"] = links
    return out


SUB_KEYS = ("id", "intent", "text", "status", "answer_claim_ids", "flags", "proposition", "references",
            "decision_brief")


def lean(res: dict, *, shown_by: list[str] | None = None) -> dict:
    """The MCP ``analyze`` response (see the module rules); ``unknowns`` carry their ``sub_question``.

    ``shown_by``: the passage lines that leave claims out as printed (default: all of them;
    :func:`lean_capped` passes the ones a response cap keeps)."""
    out: dict = {k: res[k] for k in ("analysis_id", "status", "understood_as") if res.get(k) is not None}
    snap = res.get("snapshot")
    if snap:
        out["snapshot"] = {"id": snap.get("id"), "commit": snap.get("commit"), **({"dirty": True} if snap.get("dirty")
                                                                                   else {})}
    for k in ("errors", "clarifications", "index_refresh_error"):
        if res.get(k):
            out[k] = res[k]
    if res.get("plan_check"):
        out["plan_check"] = lean_plan_check(res["plan_check"])
    if res.get("plan_source") == "host":
        out["plan_id"] = res.get("plan_id")
    out["subquestions"] = [{k: s[k] for k in SUB_KEYS if s.get(k)} for s in res.get("subquestions") or []]
    hidden = shown_by_passages(res if shown_by is None else {**res, "passages": shown_by})
    out["claims"] = [lean_claim(c) for c in res.get("claims") or [] if c["id"] not in hidden]
    if hidden:
        out["claims_in_passages"] = len(hidden)
    for k in ("unknowns", "critique"):
        if res.get(k):
            out[k] = res[k]
    exhausted = (res.get("usage") or {}).get("exhausted")
    if exhausted:
        out["budget_exhausted"] = exhausted
    if res.get("passages"):
        out["passages"] = res["passages"]
    return out


def lean_capped(res: dict, cap: Callable[[dict], dict]) -> dict:
    """``cap(lean(res))``, where ``cap`` cuts a response to its size limit (MCP ``cap_response``: the
    passages from the end, before any claim), with the claims left out as printed by the passages
    counted only on the passage lines the cut keeps: a claim whose lines the cut removed comes back.

    The cut keeps a prefix of the passages; each round counts on the prefix the last one kept, until a
    round keeps every line it counted on (at most one round per passage line, usually one or two)."""
    lines = list(res.get("passages") or [])
    while True:
        out = cap(lean(res, shown_by=lines))
        kept = len(out.get("passages") or [])
        if kept >= len(lines):
            return out
        lines = lines[:kept]


# -- CLI: plain text -----------------------------------------------------------------------------

def claim_line(c: dict) -> str:
    conf = _confidence(c)
    line = f"[{c['status']}{f' {conf:.2f}' if conf is not None else ''}] {c['text']}"
    ev = _evidence(c)
    if ev:
        line += " {" + "; ".join(ev) + "}"
    if c.get("uncertainties"):
        line += " (uncertain: " + "; ".join(c["uncertainties"]) + ")"
    line += f"  ({c['id']})"
    if c.get("challenged") is False:
        reason = c.get("not_challenged_reason")
        line += f"  [not challenged: {reason}]" if reason else "  [not challenged]"
    return line


def problem_lines(problems: list[dict], indent: str = "  ") -> list[str]:
    return [f"{indent}{p.get('at', '/')}: {p.get('msg')}" + (f"  fix: {p['fix']}" if p.get("fix") else "")
            for p in problems]


def clarification_lines(clar: list[dict], indent: str = "  ") -> list[str]:
    out = []
    for c in clar:
        out.append(f"{indent}{c['id']}: {c.get('question_user_lang') or c.get('question_en')}")
        for o in c.get("options") or []:
            ev = f"  [{o['evidence']}]" if o.get("evidence") else ""
            out.append(f"{indent}   - {o.get('value')}: {o.get('label')}{ev}")
    return out


def _brief(b: dict) -> list[str]:
    out = []
    for f in b.get("forces") or []:
        out.append(f"  [{f['status']}] {f['fact'][:200]}  ({', '.join(f['at'][:3])})")
    for a in b.get("absences") or []:
        out.append(f"  absent: {a['what']}")
    for q in b.get("questions_for_human") or []:
        out.append(f"  ask the user {q['id']}: {q['text']}")
    return out


def _unknown(u: dict) -> str:
    return f"unknown: {u.get('question')}: {u.get('why')}" + (f"; next: {u['next_step']}" if u.get("next_step") else "")


def render_text(res: dict) -> str:
    """The default ``verinoda analyze`` output (see the module rules)."""
    out: list[str] = []
    snap = res.get("snapshot") or {}
    head = f"analysis {res.get('analysis_id')}"
    if snap:
        head += (f", snapshot {snap.get('id')} (commit {(snap.get('commit') or 'no-git')[:10]}"
                 f"{', dirty' if snap.get('dirty') else ''})")
    out.append(head)
    if res.get("understood_as"):
        out.append(f"understood as: {res['understood_as']}")
    status = res.get("status")
    pc = res.get("plan_check") or {}
    if res.get("plan_source") == "host":
        out.append(f"plan {res.get('plan_id')}: {pc.get('status') or status}")
    linked = []
    for lk in pc.get("links") or []:
        if lk.get("status") == "not_found":
            out.append("plan check: " + _link(lk))
        elif lk.get("status") != "weak":
            linked.append(_link(lk, untagged="linked"))
    if linked:  # where the question's words resolved in the code (the MCP view lists the same links)
        out.append("plan links: " + "; ".join(linked))
    if res.get("index_refresh_error"):
        out.append(f"warning: {res['index_refresh_error']}")
    if status == "invalid_plan":
        out.append("the plan is invalid; nothing was analysed:")
        out += problem_lines(res.get("errors") or pc.get("errors") or [])
        out.append("  next: fix the plan file (`verinoda plan schema`, `verinoda plan check FILE`)")
    elif status == "needs_clarification":
        out.append("clarification needed before analysing (ask the user, record the choice in the plan's answers[] "
                   "with its clarification_id, then re-run):")
        out += clarification_lines(res.get("clarifications") or pc.get("clarifications") or [])
    elif status == "no_index":
        out.append("no index to analyse: run `verinoda scan` first")
    claims = {c["id"]: c for c in res.get("claims") or []}
    unknowns = list(res.get("unknowns") or [])
    printed: set[str] = set()
    for s in res.get("subquestions") or []:
        out.append(f"{s['id']} [{s.get('status') or '?'}] {s.get('intent')}: {s.get('text') or ''}")
        b = s.get("decision_brief") or {}
        if b.get("forces") is not None:  # a choice: what the human decides with (no recommendation)
            out.append(f"  decision brief {b.get('brief_id')} [{b.get('verdict')}]: {b.get('understood_as')}")
            out += _brief(b)
        for cid in s.get("answer_claim_ids") or []:
            if cid in printed:
                out.append(f"  (also answers this: {cid})")
            elif cid in claims:
                out.append("  " + claim_line(claims[cid]))
                printed.add(cid)
        for u in [u for u in unknowns if u.get("sub_question") == s["id"]]:
            out.append("  " + _unknown(u))
    hidden = shown_by_passages(res)
    rest = [c for c in claims.values() if c["id"] not in printed and c["id"] not in hidden]
    if rest or hidden:
        out.append("context (found on the way; not what answers):" if printed else "claims:")
        out += ["  " + claim_line(c) for c in rest]
        if hidden:
            out.append(f"  +{len(hidden)} verified claim(s) about lines the passages below print (--json lists them)")
    loose = [u for u in unknowns if not u.get("sub_question")]
    if loose:
        out += [_unknown(u) for u in loose]
    for c in res.get("critique") or []:
        out.append(f"critique {c['claim']}: {c['before']} -> {c['after']}  "
                   + "; ".join((c.get("fails") or []) + (c.get("warns") or []))[:300])
    exhausted = (res.get("usage") or {}).get("exhausted")
    if exhausted:
        out.append(f"budget exhausted: {exhausted} (what was not reached is listed as unknown)")
    if res.get("passages"):
        out.append("passages (what `verinoda query` gives for the question; search results, not claims):")
        out += res["passages"]
    return "\n".join(out)
