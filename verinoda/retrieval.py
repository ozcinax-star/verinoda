"""Question -> small, justified set of code locations (docs/DESIGN.md D17-D22).

Ranking comes from the persistent passage index (:mod:`verinoda.search_index`):
BM25F over 12-line passages of every symbol, module block and prose section,
unit score = best passage, an exact-identifier override, then a personalised
PageRank prior from the lexical hits. Nothing is scanned per question.

Two outputs share one ranking:

* :func:`retrieve` returns JSON for programs (``id, symbol, file, lines, span,
  score, why, excerpt`` per item; the top items also carry a compact
  ``calls`` / ``called_by`` outline), cut to a character budget that counts
  everything returned. ``lines`` is an excerpt window on the matched lines of
  the item's best passage, ``span`` its whole definition. The ``budget``
  block also says what was cut or widened: ``more`` (further candidates as
  ``path:a-b name``), ``expansions``, ``prose_items_skipped`` and
  ``stale_files`` (changed since indexing). Items whose excerpt
  text is identical to one already chosen are folded into it
  (``same_text_at``), and at most :data:`MAX_PROSE_ITEMS` items come from prose
  files: prose restates the question's words far more often than code does,
  and this is a code locator.
* :func:`render_text` renders the same result as plain text for a model,
  skeleton first: the top items with signature, doc line, call outline with
  call-site lines (``?`` marks an INFERRED edge), referenced module constants
  and their best passages; the next items with one passage; the rest as one
  line each; and an explicit note with the follow-up command when candidates
  did not fit.

Every item says why it was chosen (``why``); graph proximity only moves the
ranking and is labelled ``graph``. Query-side expansions (abbreviations,
Turkish stems, caller-supplied glosses) are reported, never silent.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import networkx as nx

from verinoda import search_index, testcode
from verinoda.index import Graph, file_lines
from verinoda.search_index import named_identifiers, stem  # noqa: F401 - re-exported API
from verinoda.textnorm import fold_tr

EXPAND_RELATIONS = {"calls", "uses", "inherits", "method", "imports_from", "references"}
MAX_EDGES = 40
ENVELOPE_CHARS = 150  # the "budget" block and the JSON punctuation around the lists
MAX_LINE_CHARS = 240        # longer excerpt lines are cut and marked (JSON)
CONTEXT_LINES = 1           # lines shown around the matched ones
HEAD_LINES = 8              # excerpt of a long item without text hits: its first lines
DOC_SUFFIXES = (".md", ".rst")
PROSE_SUFFIXES = (".md", ".rst", ".txt", ".adoc", ".markdown", ".mdx")
MAX_PROSE_ITEMS = 3
RANK_LIMIT = 60             # ranked candidates kept for rendering
OUTLINE_ITEMS = 3           # JSON items that carry calls / called_by
# JSON excerpt tiers (D20 skeleton-first): items 1-3 up to Budget.excerpt_lines,
# items 4-7 half of it, later items only their matched lines; then "more".
JSON_FULL_ITEMS, JSON_EXCERPT_ITEMS, JSON_TAIL_LINES = 3, 7, 3
MAX_MORE = 15               # further candidates listed as "path:a-b name" after the items
MAX_CALLS, MAX_CALLERS, MAX_CALLERS2 = 12, 8, 4

# text rendering (D20)
TEXT_FULL_ITEMS = 3
TEXT_EXCERPT_ITEMS = 7
TEXT_SHORT_BODY = 24
TEXT_PASSAGE_LINES = 14
TEXT_LINE_CHARS = 160
TEXT_MAX_PROSE = 2

FLOW_RX = re.compile(r"\b(call|calls|called|calling|path|flow|pipeline|turn\w*|how does|how is|steps?|"
                     r"cagir\w*|akis\w*|nasil calis\w*|hangi fonksiyon\w*)\b", re.I)
IMPACT_RX = re.compile(r"\b(affect\w*|impact\w*|retest\w*|breaks?|if .* chang\w+|who uses|used by|callers?|"
                       r"depend\w*|etkile\w*|kim kullan\w*|kullanan\w*|bagiml\w*)\b", re.I)
_KIND_OF = {"calls": "call", "method": "containment", "contains": "containment", "uses": "reference",
            "references": "reference", "imports": "import", "imports_from": "import", "inherits": "inheritance",
            "implements": "inheritance", "extends": "inheritance"}


def _cost(obj) -> int:
    """Serialised size, plus the ", " that separates list elements."""
    return len(json.dumps(obj, ensure_ascii=False, default=str)) + 2


@dataclass
class Budget:
    max_items: int = 12
    max_chars: int = 8000
    excerpt_lines: int = 14
    used_chars: int = 0
    truncated: bool = False
    extra: dict = field(default_factory=dict)

    def take(self, n: int) -> bool:
        if self.used_chars + n > self.max_chars:
            self.truncated = True
            return False
        self.used_chars += n
        return True


class RetrievalResult(dict):
    """The JSON result of :func:`retrieve`; ``render`` keeps the ranking for :func:`render_text`.

    It serialises (``json.dumps``) and compares exactly like the plain dict.
    """

    render: "_RenderData | None" = None


@dataclass
class _RenderData:
    g: Graph
    question: str
    ranking: "search_index.Ranking"
    terms: set[str]
    stale: list[str]
    flow: bool = False
    impact: bool = False


def terms_for(question: str) -> list[str]:
    """Graphify's query terms (the raw-grep benchmark baseline uses them)."""
    from verinoda.project_index.serve import _query_terms

    return list(dict.fromkeys(_query_terms(question)))


def term_regex(term: str) -> re.Pattern:
    """``term`` (case-insensitive) starting at an identifier-part boundary."""
    return re.compile(r"(?:(?<![^\W_])|(?<=[a-z0-9])(?=[A-Z]))(?i:" + re.escape(term) + ")")


def label_signal(raw: float) -> float:
    return min(1.0, math.log10(1.0 + max(0.0, raw)) / 4.0)


def _excerpt(lines: list[str], a: int, b: int) -> str:
    out = []
    for x in lines[a - 1:b]:
        out.append(x if len(x) <= MAX_LINE_CHARS else f"{x[:MAX_LINE_CHARS]} …[+{len(x) - MAX_LINE_CHARS} chars]")
    return "\n".join(out)


def _window(span: tuple[int, int], focus: tuple[int, int] | None, n: int, total: int) -> tuple[int, int]:
    """Excerpt range inside ``span`` (and the file): the whole span when it fits in
    ``n`` lines; else the matched lines ``focus`` plus :data:`CONTEXT_LINES` around
    them, at most ``n`` lines; without a focus the first :data:`HEAD_LINES` lines
    (signature and docstring)."""
    s = span[0]
    e = max(s, min(span[1], total) if total else span[1])
    if e - s + 1 <= n:
        return s, e
    if focus is None:
        return s, min(e, s + min(n, HEAD_LINES) - 1)
    a = max(s, min(e, focus[0]) - CONTEXT_LINES)
    b = min(e, max(a, focus[1]) + CONTEXT_LINES)
    return a, min(b, a + n - 1)


def _hit_lines(lines: list[str], a: int, b: int, terms: set[str]) -> list[int]:
    """Lines in ``a..b`` that contain a query term (same tokenizer as the index)."""
    out = []
    for i in range(max(1, a), min(b, len(lines)) + 1):
        if terms.intersection(search_index.tokens(lines[i - 1])):
            out.append(i)
    return out


def _edge_line(d: dict) -> int | None:
    loc = d.get("source_location") or ""
    return int(loc[1:]) if loc.startswith("L") and loc[1:].isdigit() else None


def _at(d: dict) -> str | None:
    loc = d.get("source_location") or ""
    return f"{d.get('source_file')}:{loc[1:]}" if d.get("source_file") and loc.startswith("L") else d.get("source_file")


def _short(g: Graph, nid: str) -> str:
    return search_index.bare_label(g.label(nid))


def _mark(d: dict) -> str:
    return "" if d.get("confidence") == "EXTRACTED" else "?"


def call_outline(g: Graph, nid: str, *, impact: bool = False) -> tuple[list[str], list[str], int, int]:
    """``calls`` (``name@line``, by call-site line) and ``called_by`` (``name (path:line)``).

    ``?`` marks an INFERRED edge. With ``impact`` each caller also lists its own
    callers (one more reverse hop, at most :data:`MAX_CALLERS2`). Returns the two
    lists (capped) and the uncapped counts.
    """
    calls, seen = [], set()
    for v, d in sorted(g.out_edges(nid, {"calls"}), key=lambda x: (_edge_line(x[1]) or 0, _short(g, x[0]))):
        lab = _short(g, v)
        if lab in seen:
            continue
        seen.add(lab)
        ln = _edge_line(d)
        calls.append(f"{lab}@{ln}{_mark(d)}" if ln else f"{lab}{_mark(d)}")
    callers = []
    seen_callers: set[str] = set()
    # product code before tests (a mod's gametest callers sort before src/main), one line per caller
    for u, d in sorted(g.in_edges(nid, {"calls"}),
                       key=lambda x: (testcode.is_test_file(g.file(x[0]) or ""), _at(x[1]) or "", x[0])):
        if u in seen_callers:
            continue
        seen_callers.add(u)
        entry = f"{_short(g, u)} ({_at(d) or g.file(u)}){_mark(d)}"
        if impact:
            sub = [f"{_short(g, w)} ({_at(d2) or g.file(w)}){_mark(d2)}"
                   for w, d2 in sorted(g.in_edges(u, {"calls"}), key=lambda x: (_at(x[1]) or "", x[0]))]
            if sub:
                entry += " <- " + ", ".join(sub[:MAX_CALLERS2]) + (f" (+{len(sub) - MAX_CALLERS2})"
                                                                  if len(sub) > MAX_CALLERS2 else "")
        callers.append(entry)
    return calls[:MAX_CALLS], callers[:MAX_CALLERS], len(calls), len(callers)


def _why(g: Graph, h: "search_index.Hit", hit_line: int | None, higher: dict[str, float],
         expanded: dict[str, str]) -> list[str]:
    out = list(h.reasons)
    if h.name_terms:
        out.append("name: " + ", ".join(f"'{t}'" + (f" (~{expanded[t]})" if t in expanded else "")
                                        for t in h.name_terms[:4]))
    if h.body_terms:
        out.append("text: " + ", ".join(f"'{t}'" + (f" (~{expanded[t]})" if t in expanded else "")
                                        for t in h.body_terms[:4])
                   + (f" at {h.file}:{hit_line}" if hit_line else ""))
    if h.ppr > 0 and h.nid and h.nid in g.G:
        via = None
        for other, d in list(g.out_edges(h.nid, search_index.REL_WEIGHTS)) + list(g.in_edges(h.nid, search_index.REL_WEIGHTS)):
            if other in higher and (via is None or higher[other] > via[0]):
                via = (higher[other], other, d.get("relation"))
        out.append(f"graph +{h.ppr:.2f}" + (f": {via[2]} {g.label(via[1])}" if via else " (2+ hops)"))
    return out[:3] or ["ranked by the search index"]


def retrieve(g: Graph, question: str, budget: Budget | None = None, *, include_tests: bool = True,
             seeds: dict[str, str] | None = None, expansions: dict[str, list[str]] | None = None,
             handle: search_index.Handle | None = None) -> dict:
    """Rank, then pack a JSON result into ``budget`` (see the module docstring).

    ``seeds`` maps graph node ids to the reason they were linked (for example by
    a question plan); they enter with lexical score 1.0. ``expansions`` maps a
    question word to extra words to search for (weighted below the question's
    own words). Both are reported in the items' ``why`` / the ``expansions`` list.
    ``handle``: an index already open (the notes view's, which never writes search.db).
    """
    budget = budget or Budget()
    rk = search_index.rank(g, question, include_tests=include_tests, seeds=seeds, expansions=expansions,
                           limit=RANK_LIMIT, handle=handle)
    q = rk.query
    ranked_files = {x.file for x in rk.hits[:40]}
    # the question's content words (folded), as the index saw them before tokenizing
    terms = list(dict.fromkeys(fold_tr(w) for w in q.words))
    # The envelope is always returned, so it is always charged (a list of terms
    # that alone overflows a tiny budget is shortened).
    while terms and _cost({"question": question, "terms": terms}) + ENVELOPE_CHARS > budget.max_chars:
        terms.pop()
    budget.used_chars += _cost({"question": question, "terms": terms}) + ENVELOPE_CHARS
    budget.truncated = budget.used_chars > budget.max_chars
    exps = [f"{e['from']}->{e['to']} ({e['via']})" for e in q.expansions]
    if exps and not budget.take(_cost(exps) + len(', "expansions": ')):
        exps = []
    expanded = {e["to"]: e["from"] for e in q.expansions}
    all_terms = set(q.weights)
    win = max(1, budget.excerpt_lines)
    flow = bool(FLOW_RX.search(fold_tr(question)))
    impact = bool(IMPACT_RX.search(fold_tr(question)))
    items: list[dict] = []
    seen_text: dict[str, dict] = {}
    prose, prose_skipped = 0, 0
    higher: dict[str, float] = {}
    rest_from = len(rk.hits)
    handle = handle or search_index.open_for(g)  # the index rank() just used: links and copies read from it
    for k, h in enumerate(rk.hits):
        if len(items) >= budget.max_items:
            budget.truncated = True
            rest_from = k
            break
        in_graph = bool(h.nid and h.nid in g.G)
        if h.file.lower().endswith(PROSE_SUFFIXES):
            if prose >= MAX_PROSE_ITEMS:
                prose_skipped += 1
                continue
            prose += 1
        lines = file_lines(g.root / h.file) or []
        span = (g.span(h.nid) if in_graph else None) or (h.a, h.b)
        best = h.passages[0] if h.passages else None
        hits = _hit_lines(lines, best[2], best[3], all_terms) if best else []
        focus = (hits[0], hits[-1]) if hits else ((best[2], best[3]) if best and best[0] > 0 else None)
        n = len(items)
        tier = win if n < JSON_FULL_ITEMS else (max(1, win // 2) if n < JSON_EXCERPT_ITEMS else min(win, JSON_TAIL_LINES))
        if h.kind == "module" and hits:
            a, b = hits[0], min(hits[-1], hits[0] + tier - 1)
        elif h.kind == "module":
            a, b = h.a, min(h.b, h.a + tier - 1)
        else:
            a, b = _window(span, focus, tier, len(lines))
        excerpt = _excerpt(lines, a, b) if lines else ""
        symbol = g.label(h.nid) if in_graph else ("(module level)" if h.kind == "module" else h.name)
        sig = hashlib.sha1("\n".join(x.strip() for x in excerpt.splitlines()).encode("utf-8")).hexdigest() \
            if excerpt.strip() else None
        if sig and sig in seen_text:  # identical text already shown: point at it, do not repeat it
            first = seen_text[sig]
            loc = f"{h.file}:{a}-{b}"
            key_cost = 0 if "same_text_at" in first else len(', "same_text_at": []')
            if len(first.get("same_text_at", [])) < 3 and budget.take(_cost(loc) + key_cost):
                first.setdefault("same_text_at", []).append(loc)
            continue
        item = {"id": h.key, "symbol": symbol, "file": h.file, "lines": [a, b], "span": [span[0], span[1]],
                "score": round(h.score, 3), "why": _why(g, h, hits[0] if hits else None, higher, expanded),
                "excerpt": excerpt}
        if h.kind == "data":
            item["kind"] = "data"
        if in_graph and len(items) < (JSON_EXCERPT_ITEMS if flow or impact else OUTLINE_ITEMS):
            calls, callers, _nc, _nb = call_outline(g, h.nid, impact=impact)
            if calls:
                item["calls"] = calls
            if callers:
                item["called_by"] = callers
        if len(items) < OUTLINE_ITEMS:
            try:
                lk = search_index.describe_links(handle, h.file, h.a, h.b)
            except Exception:  # noqa: BLE001 - links are an optional extra
                lk = None
            if lk:
                names = [{"rid": e["rid"], "line": e["line"], "form": e["form"], "sure": e["sure"],
                          "targets": [x for x in e["targets"] if x in ranked_files][:1]} for e in lk["names"]]
                names = [e for e in names if e["targets"]][:2]
                by = [{k: e[k] for k in ("file", "line", "rid", "form", "sure", "unit")} for e in lk["named_by"][:2]]
                if names or by:
                    item["links"] = {k: v for k, v in (("names", names), ("named_by", by)) if v}
        try:
            copies = search_index.copies_of(handle, h.file)
        except Exception:  # noqa: BLE001
            copies = []
        if copies:  # byte-identical files are ranked once; their locations are listed here
            item["same_text_at"] = [f"{c}:{a}-{b}" for c in copies[:3]]
        if not budget.take(_cost(item)):  # serialised size: escaped newlines/quotes count
            item.pop("calls", None)
            item.pop("called_by", None)
            item.pop("links", None)
            item["excerpt"] = ""
            if not budget.take(_cost(item)):
                rest_from = k
                break
        if sig:
            seen_text[sig] = item
        items.append(item)
        if in_graph:
            higher[h.nid] = h.score
    chosen = {i["id"]: i["score"] for i in items}
    edges = []
    # edges between returned items, those joining the most relevant items first
    between = sorted(((u, v, d) for u in chosen if u in g.G for v, d in g.out_edges(u, EXPAND_RELATIONS)
                      if v in chosen),
                     key=lambda x: (-(chosen[x[0]] + chosen[x[1]]), x[0], x[1], _at(x[2]) or ""))
    for u, v, d in between:
        e = {"from": g.label(u), "to": g.label(v), "relation": d.get("relation"),
             "confidence": d.get("confidence"), "at": _at(d),
             **({"derived_by": d["_origin"]} if str(d.get("_origin", "")).startswith("verinoda") else {})}
        if len(edges) >= MAX_EDGES:
            budget.truncated = True
            break
        if not budget.take(_cost(e)):
            break
        edges.append(e)
    shown = {i["id"] for i in items}
    more: list[str] = []
    more_cost = len(', "more": []')  # listed inside "budget": the candidates the budget cut
    for h in rk.hits[rest_from:]:
        if len(more) >= MAX_MORE or h.key in shown:
            continue
        if not include_tests and testcode.is_test_file(h.file):
            continue
        entry = f"{h.file}:{h.a}-{h.b} {_clip(h.qual or h.name or '(module level)', 60)}"
        if not budget.take(_cost(entry) + more_cost):
            break
        more_cost = 0
        more.append(entry)
    stale = search_index.stale_files(handle, g.root, [i["file"] for i in items])
    out_budget = {"max_items": budget.max_items, "max_chars": budget.max_chars,
                  "used_chars": budget.used_chars, "truncated": budget.truncated}
    if exps:
        out_budget["expansions"] = exps
    if more:
        out_budget["more"] = more
    if prose_skipped and budget.take(40):
        out_budget["prose_items_skipped"] = prose_skipped
    if stale and budget.take(_cost(stale) + 40):
        out_budget["stale_files"] = stale
    out_budget["used_chars"] = budget.used_chars
    res = RetrievalResult({"question": question, "terms": terms, "items": items, "edges": edges,
                           "budget": out_budget})
    res.render = _RenderData(g, question, rk, all_terms, stale, flow, impact)
    return res


# -- plain-text rendering (D20) -----------------------------------------------------------------

def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _link_how(e: dict) -> str:
    """How a link was read: stated, or inferred (bare name: namespace assumed; kind not stated)."""
    if e["form"] == "bare":
        return "bare name, inferred"
    if not e.get("sure", True):
        return f"{e['form']}, kind not stated: inferred"
    return e["form"]


def _link_lines(handle, h, ranked: set[str]) -> list[str]:
    """``names`` / ``named by`` / ``same content`` lines for a hit (resource ids between code and data).

    ``names`` lists only ids whose file is itself among the ranked candidates (the link is why
    both are here); ``named by`` lists the lines naming this file, code first.
    """
    try:
        lk = search_index.describe_links(handle, h.file, h.a, h.b, limit=12)
    except Exception:  # noqa: BLE001
        return []
    out = []
    bits = []
    for e in lk["names"]:
        tg = [x for x in e["targets"] if x in ranked]
        if not tg:
            continue
        bits.append(f"`{e['rid']}` (line {e['line']}, {_link_how(e)}) -> {tg[0]}")
    if bits:
        out.append("  names: " + "; ".join(bits[:2]) + (f" (+{len(bits) - 2})" if len(bits) > 2 else ""))
    if lk["named_by"]:
        shown = lk["named_by"][:3]
        more = len(lk["named_by"]) - len(shown) + lk["more_named_by"]
        out.append("  named by: " + "; ".join(f"{e['unit'] or '?'} ({e['file']}:{e['line']}) as `{e['rid']}`"
                                              + ("" if e.get("sure", True) else f" ({_link_how(e)})")
                                              for e in shown) + (f" (+{more} more)" if more else ""))
    if lk["copies"]:
        out.append("  same content: " + ", ".join(lk["copies"][:2]) + (" ..." if len(lk["copies"]) > 2 else ""))
    return [_clip(x, 500) for x in out]


def render_text(result: dict, budget_chars: int = 6000) -> str:
    """Plain text for a model, skeleton first, packed to ``budget_chars``.

    Works on the result of :func:`retrieve`. A result that went through JSON
    (no ranking attached) is rendered from its items alone.
    """
    rd: _RenderData | None = getattr(result, "render", None)
    q = result.get("question", "")
    out: list[str] = []
    used = 0

    def add(block: str) -> bool:
        nonlocal used
        if used + len(block) + 1 > budget_chars:
            return False
        out.append(block)
        used += len(block) + 1
        return True

    head = f"# {q}" if q else "# query"
    expansions = (result.get("budget") or {}).get("expansions")
    if expansions:
        head += "\nexpanded: " + "; ".join(expansions[:6])
    add(_clip(head, 600))
    stale = (result.get("budget") or {}).get("stale_files") or (rd.stale if rd else [])
    if stale:
        add(_clip("changed since indexing (run `verinoda update`): " + ", ".join(stale), 300))
    if rd is None:
        return _render_items(result, out, add, budget_chars)
    g = rd.g
    flow, impact = rd.flow, rd.impact
    try:
        handle = search_index.open_for(g, sync=False)
    except Exception:  # noqa: BLE001 - links and coverage notes are optional extras
        handle = None
    ranked = {x.file for x in rd.ranking.hits[:40]}
    shown: dict[str, list[tuple[int, int]]] = defaultdict(list)
    rank = prose = 0
    rest: list[str] = []
    outlined = False
    for i, h in enumerate(rd.ranking.hits):
        if h.file.lower().endswith(PROSE_SUFFIXES):
            if prose >= TEXT_MAX_PROSE:
                continue
            prose += 1
        a, b = h.a, h.b
        if any(x <= a and b <= y for x, y in shown[h.file]):
            continue
        lines = file_lines(g.root / h.file) or []
        if not lines:
            continue
        rank += 1
        name = h.qual or h.name or "(module level)"
        sig = _clip(h.sig or name, 200)
        if rank > TEXT_EXCERPT_ITEMS:
            line = f"{h.file}:{a}-{b} {_clip(h.sig or name, 120)}"
            if not add(line):
                rest = [f"{x.file}:{x.a}-{x.b} {x.qual or x.name}" for x in rd.ranking.hits[i:]]
                break
            continue
        full = rank <= TEXT_FULL_ITEMS
        ref_tag = " [reference]" if any("reference tree" in r for r in h.reasons) else ""
        parts = [f"## {h.file}:{a}-{b} {sig}{ref_tag}"]
        if h.doc and full:
            parts.append("  doc: " + _clip(h.doc, 160))
        parts += _link_lines(handle, h, ranked) if handle is not None else []
        if h.nid and h.nid in g.G and (full or flow or impact):
            calls, callers, nc, nb = call_outline(g, h.nid, impact=impact)
            if calls and (full or flow):
                parts.append("  calls: " + ", ".join(calls) + (f" (+{nc - len(calls)})" if nc > len(calls) else ""))
                outlined = True
            if callers and (full or flow or impact):
                parts.append("  called by: " + "; ".join(callers) + (f" (+{nb - len(callers)})" if nb > len(callers) else ""))
                outlined = True
        if full and h.consts:
            for c, ln in h.consts[:2]:
                if 0 < ln <= len(lines):
                    parts.append(f"  const {h.file}:{ln}: " + _clip(lines[ln - 1].strip(), 120))
        wins: list[tuple[int, int]] = []
        if h.own and (b - a + 1) <= TEXT_SHORT_BODY:
            wins = [(a, b)]
        else:
            k = 2 if full else 1
            for s, _pid, pa, pb in h.passages:
                pa, pb = max(a, pa - 1), min(b, pb + 1)
                if any(not (pb < x or pa > y) for x, y in wins):
                    continue
                wins.append((pa, min(pb, pa + TEXT_PASSAGE_LINES - 1)))
                if len(wins) >= k:
                    break
        if not add("\n".join(parts)):
            if not add(f"{h.file}:{a}-{b} {_clip(h.sig or name, 120)}"):
                rest = [f"{x.file}:{x.a}-{x.b} {x.qual or x.name}" for x in rd.ranking.hits[i:]]
                break
            continue
        for x, y in sorted(wins):
            body = "\n".join(_clip(lines[j - 1], TEXT_LINE_CHARS) for j in range(x, min(y, len(lines)) + 1))
            add((f"  {h.file}:{x}-{y}\n" if (x, y) != (a, b) else "") + body)
        # the lines printed, not the whole span: a method of a long class whose section showed two
        # other passages still gets its own section (with its callers)
        shown[h.file].extend(wins)
    if handle is not None:
        missing, n_missing = search_index.unindexed_matching(handle, rd.ranking.query)
        if missing:
            add(_clip(f"not indexed, and the path matches your words ({n_missing}): "
                      + "; ".join(f"{f} ({why})" for f, why in missing)
                      + ("; ..." if n_missing > len(missing) else ""), 400))
    more = len(rest) + max(0, rd.ranking.candidates - len(rd.ranking.hits))
    if outlined:
        add("(calls / called by: static call graph, '?' = inferred edge; may be incomplete)")
    if more:
        nxt = f'next: verinoda query "{_clip(q, 120)}" --max-chars {budget_chars * 2}'
        while True:
            tail = f"… {more} more candidates not shown" + (": " + "; ".join(rest[:4]) if rest else "")
            tail = _clip(tail, 400) + "\n" + nxt
            if used + len(tail) + 1 <= budget_chars or len(out) <= 1:
                break
            dropped = out.pop()  # make room for the note: the last block goes to the list instead
            used -= len(dropped) + 1
            first = dropped.splitlines()[0].lstrip("# ").split(" ", 1)
            if first and ":" in first[0] and not dropped.startswith("  "):
                rest.insert(0, " ".join(first)[:120])
                more += 1
        add(tail)
    return "\n".join(out)


def _render_items(result: dict, out: list[str], add, budget_chars: int) -> str:
    """Text from JSON items only (no ranking attached)."""
    items = result.get("items") or []
    for n, it in enumerate(items):
        a, b = it.get("lines") or [0, 0]
        head = f"## {it['file']}:{a}-{b} {it.get('symbol', '')}"
        extra = []
        if it.get("calls"):
            extra.append("  calls: " + ", ".join(it["calls"]))
        if it.get("called_by"):
            extra.append("  called by: " + "; ".join(it["called_by"]))
        block = "\n".join([head, *extra, it.get("excerpt") or ""]).rstrip()
        if not add(block):
            left = len(items) - n
            add(f"… {left} more items not shown")
            break
    if (result.get("budget") or {}).get("truncated"):
        add(f'… more candidates exist: verinoda query "{_clip(result.get("question", ""), 120)}" '
            f'--max-chars {budget_chars * 2}')
    return "\n".join(out)


# -- trace ----------------------------------------------------------------------------------------

FLOW_ONLY = {"calls"}
ANY_RELATIONS = {"calls", "uses", "imports", "imports_from", "inherits", "method", "references"}


def _hints(g: Graph, text: str) -> list[dict]:
    """Candidate symbols for an unresolved endpoint, from the search index."""
    try:
        rk = search_index.rank(g, text, limit=8, ppr=False)
    except Exception:  # noqa: BLE001 - hints are a convenience; the trace result stands without them
        return []
    out = []
    for h in rk.hits:
        if h.nid and h.nid in g.G and h.kind == "symbol":
            out.append({"id": h.nid, "label": g.label(h.nid), "at": f"{h.file}:{h.a}"})
        if len(out) >= 5:
            break
    return out


def _stem_path(f: str) -> str:
    return f.rpartition(".")[0] if "." in PurePosixPath(f).name else f


def _names_symbol(g: Graph, name: str, node: str) -> bool:
    label = fold_tr(g.label(node).strip().lstrip(".").split("(")[0])
    if label == fold_tr(name):
        return True
    owner, _, last = name.rpartition(".")
    if not owner or label != fold_tr(last):
        return False
    # `Owner.name`: the owner is the node's class (under an optional module or package path), or its
    # module / package (`service.place_order`, `com.example.Wisp`), not just any `name`
    classes = {fold_tr(g.label(c).strip().strip(".()")) for c, _ in g.in_edges(node, {"method"})}
    pkg, _, cls = owner.rpartition(".")
    if fold_tr(cls) in classes:
        if not pkg:
            return True
        owner = pkg
    path = fold_tr(owner.replace(".", "/"))
    stem = fold_tr(_stem_path(g.file(node) or ""))
    parent = stem.rpartition("/")[0]
    return stem == path or stem.endswith("/" + path) or parent == path or parent.endswith("/" + path)


def _names_exactly(g: Graph, query: str, node: str) -> bool:
    """Does ``query`` name ``node`` itself, rather than something the fuzzy scorer found similar? Its id,
    its file (a path with or without the extension, backslashes allowed, or a dotted module name), its
    label, ``Owner.name`` (the owner being its class, module or package), ``file::Class.method``,
    ``Class#method``, ``name()``."""
    from verinoda import question_plan as qp

    if query in g.G:
        return query == node
    path, name = qp.split_code_name(query)
    f = g.file(node) or ""
    if path:
        if not qp._names_file(f, {path}):
            return False
        return g.is_file_node(node) if not name else _names_symbol(g, name, node)
    if not name:
        return False
    if g.is_file_node(node):  # `orders.api` names orders/api.py
        mod = name.replace(".", "/")
        return _stem_path(f) == mod or _stem_path(f).endswith("/" + mod)
    return _names_symbol(g, name, node)


def _exact_nodes(g: Graph, text: str) -> list[str]:
    """The nodes that ``text`` names exactly (:func:`_names_exactly`), found through the linking index."""
    from verinoda import question_plan as qp

    ix = qp._index(g)
    path, name = qp.split_code_name(text)
    cands: set[str] = set()
    if path:
        files = [f for f in ix.files if qp._names_file(f, {path})]
        cands = {ix.files[f] for f in files} if not name else {n for f in files for n in g.symbols_in(f)}
    elif name:
        for form in {name, name.rpartition(".")[2]}:
            cands |= {n for n, _s, tier in qp._match_string(g, ix, form) if tier in qp.NAME_TIERS}
        if "." in name:
            cands |= {ix.files[f] for f in ix.files if qp._names_file(f, {name.replace(".", "/")})}
    return sorted(n for n in cands if _names_exactly(g, text, n))


def _code_written(text: str) -> bool:
    from verinoda import question_plan as qp

    return bool(qp.code_shape(text)) or any(c in (text or "") for c in ("::", "#", "/", "\\"))


def _endpoint_notes(g: Graph, text: str, nid: str | None) -> tuple[str | None, str | None, str | None]:
    """``(not_found, fuzzy, node)`` for one endpoint and the node ``g.resolve`` gave it.

    A node the text names exactly stands (or replaces a merely similar one). An endpoint written as
    code that names nothing exactly is not replaced by a similar name when the repository spells it
    nowhere ("no symbol named `x` in this repository; nearest: ..."); when the repository spells it
    somewhere (an external name, a key), or the endpoint is plain words, the similar node is kept and
    ``fuzzy`` says so."""
    from verinoda import question_plan as qp

    if nid and _names_exactly(g, text, nid):
        return None, None, nid
    if not _code_written(text):
        if not nid:
            return None, None, None
        return None, (f"'{text}' has no exact match; resolved by similarity to {g.label(nid)} "
                      f"({g.file(nid)}:{g.line(nid)})"), nid
    exact = _exact_nodes(g, text)
    if exact:
        # a class before its constructor or its file; a top-level symbol before a member of that name
        def rank(n: str) -> tuple:
            return (g.is_file_node(n), any(True for _ in g.in_edges(n, {"method"})), g.label(n).endswith(")"))

        best = [n for n in exact if rank(n) == min(map(rank, exact))]
        if len(best) == 1:
            return None, None, best[0]
        others = ", ".join(f"{g.label(n)} ({g.file(n)}:{g.line(n)})" for n in best[1:4])
        return None, (f"'{text}' names {len(best)} symbols; using {g.label(best[0])} "
                      f"({g.file(best[0])}:{g.line(best[0])}), not {others}"), best[0]
    if not nid:
        return None, None, None
    # the same existence check as analyze (a member of a known class or module is looked for in it);
    # an owner the graph does not define needs the whole name spelled (`Foo.save` is not `save`)
    path, name = qp.split_code_name(text)
    site = qp.name_site(g, text)
    if site not in (None, qp.UNCHECKED) and "." in name and not path and not qp.owner_known(g, text):
        site = qp.name_site(g, text, strict=True)
    if site is None:
        ix = qp._index(g)
        near = [{"label": g.label(n), "site": qp._site(g, n)} for n, _ in qp._member_near(g, ix, text)]
        near += [{"label": g.label(n), "site": qp._site(g, n)} for n, _ in qp._near_misses(ix, text)
                 if qp._site(g, n) not in {x["site"] for x in near}]
        return qp.not_found_line(g, text, near), None, None
    if site == qp.UNCHECKED:
        where = "its existence was not checked"
    elif qp.spells_whole(g, site, text):
        where = f"the name occurs at {site}"
    else:
        where = f"`{name.rpartition('.')[2]}` occurs at {site}, not the whole name"
    at = f" ({g.file(nid)}:{g.line(nid)})" if g.file(nid) else ""  # a package or directory node has no line
    return None, f"no symbol is named `{text}` ({where}); resolved by similarity to {g.label(nid)}{at}", nid


def trace(g: Graph, source: str, target: str, *, max_paths: int = 3, cutoff: int = 8,
          mode: str = "flow") -> dict:
    """Directed paths between two symbols/files, each hop with its edge location.

    ``mode="flow"`` follows only ``calls`` edges (plus class construction ->
    ``__init__``), i.e. control flow. ``mode="any"`` also follows uses/imports/
    inherits/containment, which answers "how are these related" rather than
    "how does execution get from A to B"; every hop says its ``kind``
    (``call``, ``construction``, ``containment``, ``reference``, ``import``,
    ``inheritance``) and a path with a non-call hop is reported as
    ``structural``, not as reachability. An endpoint that does not resolve
    comes back with ``hints`` (likely symbols) and a ``next_step``. An
    endpoint written as code (``orders\\api.py``, ``path::Class.method``,
    ``Class#method``, ``name()``, a dotted module or class name) resolves to
    the node it names exactly; one that names nothing and that the repository
    spells nowhere is not replaced by a similar one (``not_found``: "no symbol
    named `x` in this repository; nearest: ..."); an endpoint resolved by
    similarity is kept and reported in ``fuzzy``.
    """
    s, s_cands = g.resolve(source)
    t, t_cands = g.resolve(target)
    not_found: dict[str, str] = {}
    fuzzy: dict[str, str] = {}
    ends = {}
    for side, text, nid in (("source", source, s), ("target", target, t)):
        nf, fz, ends[side] = _endpoint_notes(g, text, nid)
        if nf:
            not_found[side] = nf
        if fz:
            fuzzy[side] = fz
    s, t = ends["source"], ends["target"]
    out = {"source": source, "target": target,
           "resolved": {"source": s and {"id": s, "at": f"{g.file(s)}:{g.line(s)}"},
                        "target": t and {"id": t, "at": f"{g.file(t)}:{g.line(t)}"}},
           "candidates": {"source": [c for _, c in s_cands], "target": [c for _, c in t_cands]},
           "paths": [], "direction": "directed", "mode": mode,
           **({"not_found": not_found} if not_found else {}), **({"fuzzy": fuzzy} if fuzzy else {})}
    if not s or not t:
        out["status"] = "unresolved"
        out["hints"] = {k: _hints(g, text) for k, text, nid in (("source", source, s), ("target", target, t))
                        if not nid}
        out["next_step"] = ("pass one of the hints (a node id, 'path/file.py::symbol' or 'Class.method'), "
                            "or run `verinoda query` with the name to find it")
        return out
    if s == t:
        out["status"] = "ambiguous: both endpoints resolved to the same node"
        return out
    D = nx.DiGraph()
    rels = FLOW_ONLY if mode == "flow" else ANY_RELATIONS
    for u, v, d in g.edges(rels | ({"method"} if mode == "flow" else set())):
        if d.get("relation") == "method" and mode == "flow" and g.label(v).strip(".()") != "__init__":
            continue
        if not D.has_edge(u, v) or d.get("confidence") == "EXTRACTED":
            D.add_edge(u, v, **d)
    paths = []
    try:
        for p in nx.shortest_simple_paths(D, s, t):
            if len(p) - 1 > cutoff:
                break
            paths.append(p)
            if len(paths) >= max_paths:
                break
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        pass
    if not paths:
        try:
            p = nx.shortest_path(D.to_undirected(as_view=True), s, t)
            out["undirected_hint"] = [g.label(n) for n in p]
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            pass
        out["status"] = "no directed path"
        return out
    kinds_seen: set[str] = set()
    for p in paths:
        hops = []
        for a, b in zip(p, p[1:]):
            d = D.edges[a, b]
            rel = d.get("relation")
            kind = "construction" if mode == "flow" and rel == "method" else _KIND_OF.get(rel, "reference")
            kinds_seen.add(kind)
            hops.append({"from": g.label(a), "from_id": a, "to": g.label(b), "to_id": b,
                         "relation": rel, "kind": kind, "confidence": d.get("confidence"), "at": _at(d),
                         **({"derived_by": d["_origin"]} if str(d.get("_origin", "")).startswith("verinoda") else {})})
        out["paths"].append(hops)
    out["status"] = "found"
    if mode == "any":
        execution = kinds_seen <= {"call"}
        out["reachability"] = "execution" if execution else "structural"
        if "containment" in kinds_seen:
            out["note"] = ("a path includes containment hops (a class/module defines the next symbol): it shows "
                           "how the symbols are related, not that execution reaches the target; "
                           "use mode='flow' for call paths")
        elif not execution:
            out["note"] = "a path includes non-call hops: a structural relation, not execution reachability"
    return out
