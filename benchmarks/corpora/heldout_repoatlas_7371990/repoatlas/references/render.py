"""Small outputs of a reference resolution for agents and people.

:func:`compact` keeps what an agent needs to answer (one line per reference:
pin, basis, status, mismatches, unresolved parts with next steps, questions)
and drops the per-mention detail; :func:`render_text` prints the same as plain
text ("References: pinned / floating / unresolved").
"""

from __future__ import annotations


def _ref_line(r: dict) -> dict:
    pin = r.get("pin") or {}
    out = {"id": r["id"], "class": r["class"], "status": r["status"],
           "what": (r.get("identity") or {}).get("canonical_url") or (r.get("requested") or {}).get("url")
           or ((r.get("identity") or {}).get("package") or {}).get("purl")
           or ((r.get("identity") or {}).get("paper") or {}).get("arxiv_id")
           or ((r.get("identity") or {}).get("app") or {}).get("name") or (r.get("requested") or {}).get("path"),
           "pin": pin.get("display"), "basis": pin.get("basis"), "value": pin.get("value")}
    req = r.get("requested") or {}
    if req.get("path"):
        out["path"] = req["path"]
    if req.get("lines"):
        out["lines"] = req["lines"]
    if r.get("mismatches"):
        out["mismatches"] = [f"{m['code']} {m['severity']}: {m['detail']}" for m in r["mismatches"][:4]]
    if r.get("unresolved"):
        out["unresolved"] = [{"what": u["what"], "why": u["why"], "next_step": u["next_step"]}
                             for u in r["unresolved"][:3]]
    if r.get("alternates"):
        out["alternates"] = [a["pin"].get("display") for a in r["alternates"][:3]]
    warn = [w for w in r.get("warnings") or [] if w.startswith("W_")]
    if warn:
        out["warnings"] = warn[:2]
    return {k: v for k, v in out.items() if v not in (None, [], {})}


def compact(result: dict) -> dict:
    return {"id": result.get("id"), "status": result.get("status"),
            "references": [_ref_line(r) for r in result.get("references", [])],
            "unbound": [f"{u.get('text') or u['mention']}: {u['why']}" for u in result.get("unbound_mentions", [])],
            "questions_for_user": result.get("questions_for_user", []),
            "summary": {k: v for k, v in result.get("summary", {}).items() if v not in (None, 0) or k in (
                "mentions_accounted", "mentions_total", "silent_floating_pins")}}


def render_text(result: dict) -> str:
    lines = []
    s = result.get("summary") or {}
    lines.append(f"References ({result.get('status')}): {s.get('pinned', 0)} pinned, "
                 f"{s.get('pinned_floating', 0)} floating, {s.get('unresolved', 0) + s.get('refused', 0)} unresolved"
                 + (f", {s.get('ambiguous')} ambiguous" if s.get("ambiguous") else "")
                 + (f"  [{result['id']}]" if result.get("id") else ""))
    for r in result.get("references", []):
        c = _ref_line(r)
        head = f"  {c['id']} {c['class']} {c.get('what') or ''}"
        lines.append(head + (f" @ {c['pin']} (basis: {c['basis']})" if c.get("pin") else f" [{c['status']}]"))
        for m in c.get("mismatches", []):
            lines.append(f"     ! {m}")
        for u in c.get("unresolved", []):
            lines.append(f"     ? {u['what']}: {u['why']} -> {u['next_step']}")
        for w in c.get("warnings", []):
            lines.append(f"     ~ {w}")
    for u in result.get("unbound_mentions", []):
        lines.append(f"  unbound '{u.get('text') or u['mention']}': {u['why']}")
    for q in result.get("questions_for_user", []):
        opts = f" [{' | '.join(q['options'])}]" if q.get("options") else ""
        lines.append(f"  ask ({q['reference']}): {q['question']}{opts}")
    return "\n".join(lines)
