"""When does a method run? (docs/DESIGN.md D47)

"When does ``Guard.watch`` run" is answered by walking back from the method through what calls it and what
registers it, to the event that starts it:

- a ``calls`` hop names the call line and the conditions around it: the ``if``/``while``/``for`` blocks the call
  sits in, a braceless ``if (...)`` right before it, and early exits above it in the same method (``if (x)
  return;`` means the call runs only when ``x`` is false);
- a ``registers`` hop (a method reference or a lambda handed to a registration, :func:`index.java_registers_edges`)
  names the event ("at the end of every server tick", "80 ticks later") and where it is registered;
- a path ends at a registration, at a method nothing in the project calls (an entry point: the framework calls
  it, or nothing does), or after :data:`MAX_DEPTH` hops.

Conditions are read from the source (Java and Kotlin; the structure from the text with strings and comments
blanked, the condition itself as written): they are what the code says, not an evaluation. Every hop carries
``file:line``.
"""
from __future__ import annotations

import re
from collections import deque

MAX_DEPTH = 6
MAX_PATHS = 8
_OPEN = re.compile(r"\b(else\s+if|if|while|for|else|when|switch)\b")
_EXIT_AFTER = re.compile(r"\s*\{?\s*(?:return|continue|break)\b")  # `if (x) return;`, `{ return; }`
_WORD = {"if": "if", "while": "while", "for": "for each", "when": "when", "switch": "switch on"}


def _cond_after(text: str, i: int) -> tuple[str, int]:
    """The parenthesised condition starting at or after ``i`` (and the index after it)."""
    j = text.find("(", i)
    if j < 0 or text[i:j].strip():
        return "", i
    depth = 0
    for k in range(j, len(text)):
        if text[k] == "(":
            depth += 1
        elif text[k] == ")":
            depth -= 1
            if depth == 0:
                return re.sub(r"\s+", " ", text[j + 1:k]).strip(), k + 1
    return re.sub(r"\s+", " ", text[j + 1:]).strip(), len(text)


def _original_cond(orig: list[str] | None, line_no: int, kw: str, nth: int) -> str | None:
    """The condition as written (string literals kept) of the ``nth`` ``kw`` on ``line_no`` (1-based); the
    blanked text loses a literal's contents (``Settings.on("", true)``)."""
    if not orig or not 0 < line_no <= len(orig):
        return None
    ms = list(re.finditer(rf"\b{kw}\b", orig[line_no - 1]))
    if nth >= len(ms):
        return None
    offset = sum(len(x) + 1 for x in orig[:line_no - 1])
    cond, _ = _cond_after("\n".join(orig), offset + ms[nth].end())
    return cond or None


def guards(code: list[str], start: int, call_line: int, orig: list[str] | None = None, *,
           lines: list[int] | None = None) -> list[str]:
    """The conditions a call at ``call_line`` runs under, from the method that starts at ``start`` (1-based lines
    of code with strings and comments blanked; ``orig``, the same lines as written, gives the conditions their
    literals back): enclosing blocks, a braceless ``if`` right before, and early exits (``if (x) return;``) in a
    block still open at the call. ``lines``, when given, receives each condition's line."""
    text = "\n".join(code[start - 1:call_line])
    stack: list[tuple[str, int] | None] = []  # per open brace: its condition and line (None: a plain block)
    pending: tuple[str, int] | None = None    # a condition whose block has not opened yet
    exits: list[tuple[str, int, int]] = []    # (condition, line, block depth) of `if (x) return;`
    last_if: str | None = None
    seen_kw: dict[tuple[int, str], int] = {}
    line_no = start
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\n":
            line_no += 1
            i += 1
            continue
        m = _OPEN.match(text, i) if (i == 0 or not (text[i - 1].isalnum() or text[i - 1] in "_$.")) else None
        if m:
            kw = m.group(1).split()[-1]
            nth = seen_kw.get((line_no, kw), 0)
            seen_kw[(line_no, kw)] = nth + 1
            if kw in _WORD:
                cond, j = _cond_after(text, m.end())
                if cond:
                    at = line_no
                    cond = _original_cond(orig, line_no, kw, nth) or cond
                    line_no += text.count("\n", m.end(), j)
                    i = j
                    else_if = m.group(1).startswith("else")
                    pending = (f"else, if {cond}" if else_if else f"{_WORD[kw]} {cond}", at)
                    if kw == "if":
                        last_if = cond
                        if _EXIT_AFTER.match(text, j):
                            exits.append((f"not ({cond})", at, len(stack)))
                    continue
            elif kw == "else":
                pending = (f"not ({last_if})" if last_if else "else", line_no)
                i = m.end()
                continue
        if c == "{":
            stack.append(pending)
            pending = None
        elif c == "}":
            if stack:
                stack.pop()
            exits = [e for e in exits if e[2] <= len(stack)]
        elif c == ";":
            if line_no == call_line:  # the call's own statement ends here: a braceless `if (x)` guards it
                break
            pending = None
        i += 1
    found = [s for s in stack if s] + ([pending] if pending else [])  # pending: a braceless `if (x) call();`
    found += [(t, ln) for t, ln, _d in exits]
    if lines is not None:
        lines.extend(ln for _t, ln in found)
    return [t for t, _ln in found]


def when(g, nid: str, *, max_depth: int = MAX_DEPTH, max_paths: int = MAX_PATHS) -> dict:
    """The paths by which ``nid`` comes to run (see the module docstring)."""
    from verinoda import index as ix

    code_cache: dict[str, tuple[list[str] | None, list[str] | None]] = {}

    def code_of(f: str) -> tuple[list[str] | None, list[str] | None]:
        """(the lines with strings and comments blanked, the lines as written)"""
        if f not in code_cache:
            lines = ix.file_lines(g.root / f)
            if lines is None or not f.endswith((".java", ".kt", ".kts", ".groovy", ".scala")):
                code_cache[f] = (lines, lines)
            else:
                code_cache[f] = (ix._java_code_lines("\n".join(lines), kotlin=True), lines)
        return code_cache[f]

    def loc(n: str) -> str:
        return f"{g.file(n)}:{g.line(n)}"

    def name(n: str) -> str:
        """``Class.method`` for a method (its owner by the ``method`` edge), else the label"""
        own = next((u for u, _d in g.in_edges(n, {"method"})), None)
        base = g.label(n).lstrip(".").removesuffix("()")
        return f"{g.label(own)}.{base}" if own else g.label(n)

    def hop(u: str, v: str, d: dict) -> dict:
        rel = d.get("relation")
        m = re.match(r"L(\d+)", str(d.get("source_location") or ""))
        line = int(m.group(1)) if m else None
        f = d.get("source_file") or g.file(u)
        h = {"from": name(u), "from_id": u, "to": name(v), "to_id": v, "relation": rel,
             "at": f"{f}:{line}" if line else loc(u)}
        if rel == ix.CALLBACK_RELATION:
            h["event"] = ix.event_label(d.get("registrar") or "", d.get("delay"))
            h["context"] = d.get("context")
        elif line:
            sp = g.span(u)
            code, orig = code_of(f) if f else (None, None)
            if sp and code and sp[0] <= line <= len(code):
                at: list[int] = []
                h["conditions"] = guards(code, sp[0], line, orig, lines=at)
                h["condition_lines"] = at
        h["_edge"] = d  # for analyze (dropped by run())
        return h

    paths: list[list[dict]] = []
    queue: deque = deque([(nid, [], {nid})])
    while queue and len(paths) < max_paths * 4:
        node, trail, seen = queue.popleft()
        callers = []
        for u, d in g.in_edges(node, {"calls", ix.CALLBACK_RELATION}):
            if u in seen or g.G.nodes[u].get("_callable_class"):
                continue
            callers.append((0 if d.get("relation") == ix.CALLBACK_RELATION else 1, u, d))
        # a call inside a lambda is also a `calls` edge from the method around it: the lambda's registration
        # says when it runs, the plain call would say "now"
        lambda_from = {u for _r, u, d in callers if d.get("lambda")}
        callers = [c for c in callers if c[0] == 0 or c[1] not in lambda_from]
        if not callers or len(trail) >= max_depth:
            paths.append(trail + [{"entry": name(node), "at": loc(node),
                                   "why": ("nothing in the project calls or registers it: the framework calls it, "
                                           "or nothing does") if not callers else "depth limit"}])
            continue
        for _rank, u, d in sorted(callers, key=lambda x: (x[0], name(x[1]))):
            h = hop(u, node, d)
            if h["relation"] == ix.CALLBACK_RELATION:  # the event is where the story starts
                paths.append(trail + [h, {"entry": name(u), "at": loc(u), "why": "registers it"}])
            else:
                queue.append((u, trail + [h], seen | {u}))
    # registrations first, then the shortest
    paths.sort(key=lambda p: (not any(h.get("event") for h in p), len(p)))
    return {"symbol": name(nid), "id": nid, "at": loc(nid), "paths": paths[:max_paths],
            "truncated": len(paths) > max_paths,
            "note": "conditions are the code's own text around each call; a registered handler runs when its event "
                    "fires"}


def run(g, symbol: str, *, stale=None, max_depth: int = MAX_DEPTH, max_paths: int = MAX_PATHS) -> dict:
    """:func:`when` for a name (resolved by :func:`verinoda.naming.resolve`); ``status`` found, ambiguous (the
    candidates) or not_found."""
    from verinoda import naming

    r = naming.resolve(g, symbol, stale=stale)
    if r.status == naming.EXACT and r.node:
        res = when(g, r.node, max_depth=max_depth, max_paths=max_paths)
        res["paths"] = [[{k: v for k, v in h.items() if not k.startswith("_")} for h in p] for p in res["paths"]]
        return {"status": "found", **res, **({"resolution_note": r.note} if r.note else {})}
    cands = [{"id": c, "label": g.label(c), "at": f"{g.file(c)}:{g.line(c)}"} for c in list(r.candidates)[:10]]
    return {"status": "ambiguous" if r.status == naming.AMBIGUOUS else "not_found", "query": symbol,
            "candidates": cands, "note": r.note}


def render(res: dict) -> str:
    if res.get("status") not in (None, "found"):
        out = [f"{res['query']}: {res['status']}" + (f" ({res['note']})" if res.get("note") else "")]
        out += [f"  - {c['label']} ({c['at']})" for c in res.get("candidates") or []]
        return "\n".join(out)
    out = [f"{res['symbol']} ({res['at']}) runs:"]
    if not res["paths"]:
        out.append("  no caller or registration found in the index")
    for k, path in enumerate(res["paths"], 1):
        ev = next((h for h in path if h.get("event")), None)
        head = ev["event"] if ev else "when called from " + path[-1].get("entry", "?")
        out.append(f"  {k}. {head}")
        for h in reversed(path):
            if "entry" in h:
                out.append(f"     - {h['entry']} ({h['at']}): {h['why']}")
            elif h.get("event"):
                out.append(f"     - {h['from']} registers {h['to']} at {h['at']}: {h.get('context') or ''}")
            else:
                cond = "; ".join(h.get("conditions") or [])
                out.append(f"     - {h['from']} calls {h['to']} at {h['at']}" + (f", when {cond}" if cond else ""))
    if res.get("truncated"):
        out.append("  (more paths: --json lists them)")
    return "\n".join(out)
