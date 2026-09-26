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
import os
import re
import textwrap
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

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
PROSE_SUFFIXES = (".md", ".rst", ".txt", ".adoc", ".markdown", ".mdx",
                  ".pdf", ".docx", ".xlsx", ".pptx")  # the last four: their text view (doctext.py)
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
TEXT_MAX_EXPANSIONS = 3     # from->to pairs on the text's `expanded:` line (JSON lists all, with why)

# The default text budget (`verinoda query`, MCP project_query, analyze's passages). With the
# question-shape budget on (config query.shape_budget, env VERINODA_SHAPE_BUDGET; off by default,
# docs/BENCHMARKS.md "Update 2026-09-26: token wins" says why) a single-clause question gets
# SHAPE_CHARS_NARROW; compound, flow and test questions keep QUERY_CHARS.
QUERY_CHARS = 6000
SHAPE_CHARS_NARROW = 4800
_WIDE_QUESTION_RX = re.compile(r",|\b(?:and|ve|how does|nasil|what happens|ne oluyor|ends? up|down to|kadar|"
                               r"which tests?|hangi test\w*)\b|\bfrom\b.+\bto\b")

FLOW_RX = re.compile(r"\b(call|calls|called|calling|path|flow|pipeline|turn\w*|how does|how is|steps?|"
                     r"cagir\w*|akis\w*|nasil calis\w*|hangi fonksiyon\w*)\b", re.I)
IMPACT_RX = re.compile(r"\b(affect\w*|impact\w*|retest\w*|breaks?|if .* chang\w+|who uses|used by|callers?|"
                       r"depend\w*|etkile\w*|kim kullan\w*|kullanan\w*|bagiml\w*)\b", re.I)
_KIND_OF = {"calls": "call", "method": "containment", "contains": "containment", "uses": "reference",
            "references": "reference", "imports": "import", "imports_from": "import", "inherits": "inheritance",
            "implements": "inheritance", "extends": "inheritance", "registers": "callback"}


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


def backlog_lines(repo, file: str, a: int, b: int, limit: int = 2) -> list[str]:
    """``backlog: 69.3 Title (docs/BACKLOG.md:2235)`` for the backlog items the unit's comments cite (docs/DESIGN.md
    D50): the shortest path from a line to why it is so."""
    from verinoda import backlog

    try:
        found = backlog.items_for(repo, file, a, b, context=2)
    except Exception:  # noqa: BLE001 - a backlog that cannot be read never breaks retrieval
        return []
    return [f"  backlog: {x['item'].id} {_clip(x['item'].title, 90)} ({x['item'].at})" for x in found[:limit]]


def runs_lines(g: Graph, nid: str, limit: int = 2) -> list[str]:
    """What starts a symbol, when the graph knows it (docs/DESIGN.md D47, D48): ``mixin: @Inject into
    Mob.checkSpawnRules at HEAD - ...`` for a Mixin handler, ``runs: at the end of every server tick (registered at
    file:line)`` for a registered handler."""
    from verinoda import index as ix

    out: list[str] = []
    mix = list(dict.fromkeys(str(d.get("context")) for _v, d in g.out_edges(nid, {"injects", "accesses"})
                             if d.get("context")))
    if mix:
        out.append("  mixin: " + "; ".join(mix[:limit]) + (f" (+{len(mix) - limit})" if len(mix) > limit else ""))
    regs = [(d, u) for u, d in g.in_edges(nid, {ix.CALLBACK_RELATION})]
    if regs:
        seen = list(dict.fromkeys(f"{ix.event_label(d.get('registrar') or '', d.get('delay'))} (registered at "
                                  f"{d.get('source_file') or g.file(u)}:{str(d.get('source_location') or 'L?')[1:]})"
                                  for d, u in regs))
        out.append("  runs: " + "; ".join(seen[:limit]) + (f" (+{len(seen) - limit})" if len(seen) > limit else ""))
    return out


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

def shape_budget_enabled(repo: Path | str | None = None) -> bool:
    """Is the question-shape budget on? ``VERINODA_SHAPE_BUDGET`` (1/0) wins, else the project's
    ``query.shape_budget``; off by default."""
    env = os.environ.get("VERINODA_SHAPE_BUDGET")
    if env is not None and env.strip():
        return env.strip().lower() in ("1", "true", "on", "yes")
    if repo is None:
        return False
    from verinoda.paths import load_config

    try:
        return bool((load_config(Path(repo)).get("query") or {}).get("shape_budget"))
    except Exception:  # noqa: BLE001 - an unreadable config keeps the default
        return False


def question_chars(question: str, repo: Path | str | None = None) -> int:
    """The default character budget of the text for ``question`` (see :data:`QUERY_CHARS`)."""
    if not shape_budget_enabled(repo):
        return QUERY_CHARS
    return QUERY_CHARS if _WIDE_QUESTION_RX.search(fold_tr(question or "")) else SHAPE_CHARS_NARROW


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


def attach_freshness(result: dict, g: Graph, fresh: dict) -> dict:
    """Project-wide staleness on a :func:`retrieve` result (:func:`verinoda.freshness.check`):
    ``stale_count`` / ``stale_files``, and ``not_in_index``: code names of the question the index
    does not have but a changed file spells (the answer's items are not about them)."""
    from verinoda import freshness, naming

    result.update(freshness.summary(fresh))
    if fresh.get("count"):
        missing = naming.unindexed_names(g, result.get("question") or "", fresh.get("files") or [])
        if missing:
            result["not_in_index"] = missing
    return result


def stale_lines(result: dict) -> list[str]:
    """The staleness lines of a query answer (text): names the index does not have yet, then the files
    changed since the index (project-wide when :func:`attach_freshness` ran, else the answer's own)."""
    out = []
    missing = result.get("not_in_index") or []
    new = [m for m in missing if m.get("new")]
    maybe = [m for m in missing if not m.get("new")]
    if new:  # the index's version of the file was read and does not spell it
        out.append(_clip("not in the index yet (spelled in a file changed since it was built, not in the version "
                         "it describes; run `verinoda update`; the passages below are not about it): "
                         + "; ".join(f"`{m['name']}` at {m['at']}" for m in new[:3]), 400))
    if maybe:
        out.append(_clip("may not be in the index yet (spelled in a file changed since it was built; whether the "
                         "version it describes spells it could not be read; run `verinoda update`): "
                         + "; ".join(f"`{m['name']}` at {m['at']}" for m in maybe[:3]), 400))
    rd: _RenderData | None = getattr(result, "render", None)
    if result.get("stale_count"):
        files = result.get("stale_files") or []
        more = result["stale_count"] - len(files[:10])
        out.append(_clip(f"{result['stale_count']} file(s) changed since the index (run `verinoda update`; what it "
                         "says about them is the previous version): " + ", ".join(files[:10])
                         + (f", ... (+{more})" if more > 0 else ""), 600))
    else:
        stale = (result.get("budget") or {}).get("stale_files") or (rd.stale if rd else [])
        if stale:
            out.append(_clip("changed since indexing (run `verinoda update`): " + ", ".join(stale), 300))
    return out


def render_text(result: dict, budget_chars: int = 6000) -> str:
    """Plain text for a model, skeleton first, packed to ``budget_chars``.

    Works on the result of :func:`retrieve`. A result that went through JSON
    (no ranking attached) is rendered from its items alone.

    Nothing is printed twice: the question is not echoed, an item's ``## path:a-b`` header carries
    only its name when the passage below starts at its first line (the signature is that line),
    and each passage window is dedented on its own. ``## path:a-b`` / ``  path:x-y`` locators are
    kept exactly: they are what an agent (and the benchmark's locator parser) cites.
    """
    rd: _RenderData | None = getattr(result, "render", None)
    out: list[str] = []
    used = 0

    def add(block: str) -> bool:
        nonlocal used
        if used + len(block) + 1 > budget_chars:
            return False
        out.append(block)
        used += len(block) + 1
        return True

    # no echo of the question (the model asked it); expansions stay visible, compactly
    expansions = (result.get("budget") or {}).get("expansions")
    if expansions:
        add(_clip(_expanded_line(expansions), 300))
    for line in stale_lines(result):
        add(line)
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
    sig_back: dict[int, tuple[int, str]] = {}  # passage block -> (its short header block, header with signature)
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
            parts += runs_lines(g, h.nid) if full or flow else []
        if full:
            parts += backlog_lines(g.root, h.file, a, b)
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
        # A passage that starts at the item's first line prints the signature itself: the header only
        # names the item (it gets the signature back below if that passage does not fit).
        short = bool(wins) and min(wins)[0] == a and bool(h.sig)
        if short:
            parts[0] = f"## {h.file}:{a}-{b} {_clip(name, 80)}{ref_tag}"
        if not add("\n".join(parts)):
            if not add(f"{h.file}:{a}-{b} {_clip(h.sig or name, 120)}"):
                rest = [f"{x.file}:{x.a}-{x.b} {x.qual or x.name}" for x in rd.ranking.hits[i:]]
                break
            continue
        at = len(out) - 1
        for x, y in sorted(wins):
            # each window dedented on its own: the path:lines header keeps the place, indentation is noise
            body = textwrap.dedent("\n".join(_clip(lines[j - 1], TEXT_LINE_CHARS)
                                             for j in range(x, min(y, len(lines)) + 1)))
            added = add((f"  {h.file}:{x}-{y}\n" if (x, y) != (a, b) else "") + body)
            if short and x == a:
                with_sig = "\n".join([f"## {h.file}:{a}-{b} {sig}{ref_tag}", *parts[1:]])
                if added:  # should the note at the end push this passage out, the header takes it back
                    sig_back[len(out) - 1] = (at, with_sig)
                else:  # the section with its signature, else the one line an item gets when that does not fit
                    for block in (with_sig, f"{h.file}:{a}-{b} {_clip(h.sig, 120)}"):
                        now = _swap_block(out, at, block, used, budget_chars)
                        if out[at] == block:
                            used = now
                            break
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
        # the CLI command, named as such: MCP clients read this text too, and project_query takes no budget
        nxt = f"next: verinoda query \"…\" --max-chars {budget_chars * 2}"

        def drop_last() -> None:
            """Make room for the note: the last block goes (an item's section or line is listed instead)."""
            nonlocal used, more
            dropped = out.pop()
            used -= len(dropped) + 1
            if len(out) in sig_back:  # the passage that printed a signature went: its header shows it again
                at, with_sig = sig_back.pop(len(out))
                used = _swap_block(out, at, with_sig, used, budget_chars, force=True)
            m = _ITEM_HEAD_RX.match(dropped)
            if m:
                rest.insert(0, f"{m.group(1)} {m.group(2) or ''}".strip()[:120])
                more += 1

        while True:  # the whole note, keeping the top block
            tail = _tail_forms(more, rest, nxt, budget_chars, _any_item(out))[0]
            if used + len(tail) + 1 <= budget_chars or len(out) <= 1:
                break
            drop_last()
        # the top block (or a header that took its signature back) can leave too little room: a shorter
        # note, else that block goes as well; the note itself is never left out
        while True:
            room = budget_chars - used - 1
            tail = next((t for t in _tail_forms(more, rest, nxt, room, _any_item(out)) if len(t) <= room), None)
            if tail is not None or not out:
                break
            drop_last()
        add(tail if tail is not None else _clip(_tail_forms(more, [], nxt, 0, False)[-1], max(1, budget_chars - 1)))
    if not out:
        add("no candidate locations to show for this question")
    return "\n".join(out)


def _any_item(out: list[str]) -> bool:
    """Does the text show at least one item (a section or an item line)?"""
    return any(_ITEM_HEAD_RX.match(b) for b in out)


def _tail_forms(more: int, rest: list[str], nxt: str, room: int, shown: bool) -> list[str]:
    """The note on the candidates left out, longest form first: the count with the first of them and the
    next step, the same list cut to ``room``, the count and the next step, the count alone."""
    head = (f"… {more} more candidates not shown" if shown
            else f"… {more} candidates not shown (budget too small)")
    forms = []
    if rest:
        listed = head + ": " + "; ".join(rest[:4])
        forms.append(_clip(listed, 400) + "\n" + nxt)
        if room - len(nxt) - 1 >= len(head) + 24:
            forms.append(_clip(listed, room - len(nxt) - 1) + "\n" + nxt)
    return forms + [head + "\n" + nxt, head]


# the first line of a rendered item: "## path:a-b name" (a section) or "path:a-b signature" (a line)
_ITEM_HEAD_RX = re.compile(r"(?:## )?([^\s:]+:\d+-\d+)(?: ([^\n]*))?")


def _swap_block(out: list[str], at: int, block: str, used: int, budget_chars: int, *, force: bool = False) -> int:
    """Replace ``out[at]`` with ``block`` when the budget allows (always with ``force``: the caller is
    making room and cuts again); returns the new character count."""
    grow = len(block) - len(out[at])
    if force or used + grow <= budget_chars:
        out[at] = block
        return used + grow
    return used


def _expanded_line(expansions: list[str]) -> str:
    """The query-side expansions as ``from->to`` pairs, at most :data:`TEXT_MAX_EXPANSIONS`.

    Why each was made (abbreviation, Turkish stem, gloss) and the full list are in the JSON
    result's ``budget.expansions``; the text says how many were left out."""
    pairs = [e.split(" (", 1)[0] for e in expansions]
    left = len(pairs) - TEXT_MAX_EXPANSIONS
    return "expanded: " + ", ".join(pairs[:TEXT_MAX_EXPANSIONS]) + (f" (+{left} more)" if left > 0 else "")


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
        add(f"… more candidates exist: verinoda query \"…\" --max-chars {budget_chars * 2}")
    return "\n".join(out)


# -- trace ----------------------------------------------------------------------------------------

FLOW_ONLY = {"calls"}
ANY_RELATIONS = {"calls", "uses", "imports", "imports_from", "inherits", "method", "references"}
# a method handed over as a callback (JVM method references, verinoda.index.java_registers_edges): followed
# only when no path exists without it, so every path found before stays the same
CALLBACK_RELATIONS = {"registers"}
CALLBACK_NOTE = ("a path includes a callback hop (registers): the method is handed over there (a method "
                 "reference passed on); what receives it calls it later (an event, a ticker, a command) or at once "
                 "(forEach, map, filter) - it is not a call written at that line")


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


def _code_written(text: str) -> bool:
    from verinoda import question_plan as qp

    return bool(qp.code_shape(text)) or any(c in (text or "") for c in ("::", "#", "/", "\\"))


def trace(g: Graph, source: str, target: str, *, max_paths: int = 3, cutoff: int = 8,
          mode: str = "flow", stale=(), callbacks: bool = True) -> dict:
    """Directed paths between two symbols/files, each hop with its edge location.

    ``mode="flow"`` follows only ``calls`` edges (plus class construction ->
    ``__init__``), i.e. control flow. ``mode="any"`` also follows uses/imports/
    inherits/containment, which answers "how are these related" rather than
    "how does execution get from A to B"; every hop says its ``kind``
    (``call``, ``construction``, ``containment``, ``reference``, ``import``,
    ``inheritance``) and a path with a non-call hop is reported as
    ``structural``, not as reachability.

    Endpoints resolve through :func:`verinoda.naming.resolve`: the node a name
    names exactly (a detected copy or reference tree giving way to the
    project's own code; ``resolution_notes`` says when only a copy defines it).
    A name that names several symbols is ``ambiguous`` (``ambiguous`` and
    ``hints`` list them; none is picked). A name written as code that names no
    node is never replaced by a similar one: spelled in a file changed since the
    index (``stale``) it is ``not_indexed``, spelled nowhere ``not_found`` ("no
    symbol named `x` in this repository; nearest: ..."), spelled somewhere else
    (a constant, an attribute) ``not_a_symbol``. Plain words may resolve by
    similarity, reported in ``fuzzy``. An endpoint that does not resolve comes
    back with ``hints`` and a ``next_step``.
    With ``callbacks`` (the default), when there is no path, ``registers`` edges (a method handed over
    as a callback) are followed too; such a hop is ``relation="registers"``, ``kind="callback"`` and the
    result carries a ``note``.
    """
    from verinoda import naming

    res = {"source": naming.resolve(g, source, stale=stale), "target": naming.resolve(g, target, stale=stale)}
    notes: dict[str, dict[str, str]] = defaultdict(dict)
    key = {naming.NOT_FOUND: "not_found", naming.NOT_INDEXED: "not_indexed", naming.SIMILAR: "fuzzy",
           naming.AMBIGUOUS: "ambiguous", naming.NOT_A_SYMBOL: "not_a_symbol", naming.EXACT: "resolution_notes"}
    for side, r in res.items():
        if r.note and r.status in key:
            notes[key[r.status]][side] = r.note
    s, t = res["source"].node, res["target"].node
    out = {"source": source, "target": target,
           "resolved": {"source": s and {"id": s, "label": g.label(s), "at": f"{g.file(s)}:{g.line(s)}"},
                        "target": t and {"id": t, "label": g.label(t), "at": f"{g.file(t)}:{g.line(t)}"}},
           "candidates": {side: list(r.candidates) for side, r in res.items()},
           "paths": [], "direction": "directed", "mode": mode, **dict(notes)}
    if not s or not t:
        tied = [side for side, r in res.items() if r.status == naming.AMBIGUOUS]
        unres = [side for side, r in res.items() if not r.node and r.status != naming.AMBIGUOUS]
        out["status"] = "unresolved" if unres else "ambiguous"
        # a name not in the index yet gets no similar names: it exists, in a changed file
        out["hints"] = {side: (r.status in (naming.AMBIGUOUS, naming.NOT_FOUND, naming.NOT_A_SYMBOL) and r.rows(g, 5)
                               or _hints(g, r.text)) for side, r in res.items()
                        if not r.node and r.status != naming.NOT_INDEXED}
        out["next_step"] = ("pass one of the hints (a node id, 'path/file.py::symbol' or 'Class.method'), "
                            "or run `verinoda query` with the name to find it")
        if tied and not unres:
            out["next_step"] = ("the name names several symbols: pass the one you mean as 'path/file.py::symbol' "
                                "or its node id (see hints)")
        if any(r.status == naming.NOT_INDEXED for r in res.values()):
            out["next_step"] = "run `verinoda update` so the index describes the changed file(s), then trace again"
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
    paths = _simple_paths(D, s, t, cutoff, max_paths)
    if not paths and callbacks:
        D2 = _with_callbacks(g, D)
        if D2 is not None:
            paths = _simple_paths(D2, s, t, cutoff, max_paths)
            if paths:
                D = D2
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
                         **({"derived_by": d["_origin"]} if str(d.get("_origin", "")).startswith("verinoda") else {}),
                         **({"context": d.get("context")} if kind == "callback" and d.get("context") else {})})
        out["paths"].append(hops)
    out["status"] = "found"
    if mode == "any":
        execution = kinds_seen <= {"call"}
        out["reachability"] = "execution" if execution else \
            "callback" if kinds_seen <= {"call", "callback"} else "structural"
        if "containment" in kinds_seen:
            out["note"] = ("a path includes containment hops (a class/module defines the next symbol): it shows "
                           "how the symbols are related, not that execution reaches the target; "
                           "use mode='flow' for call paths")
        elif not execution and "callback" not in kinds_seen:
            out["note"] = "a path includes non-call hops: a structural relation, not execution reachability"
    if "callback" in kinds_seen:
        out["note"] = CALLBACK_NOTE + (f"; {out['note']}" if out.get("note") else "")
    return out


def _simple_paths(D: nx.DiGraph, s: str, t: str, cutoff: int, max_paths: int) -> list[list[str]]:
    paths: list[list[str]] = []
    try:
        for p in nx.shortest_simple_paths(D, s, t):
            if len(p) - 1 > cutoff:
                break
            paths.append(p)
            if len(paths) >= max_paths:
                break
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        pass
    return paths


def _with_callbacks(g: Graph, D: nx.DiGraph) -> nx.DiGraph | None:
    """``D`` plus the ``registers`` edges between nodes it has no edge between; None when there are none."""
    extra = [(u, v, d) for u, v, d in g.edges(CALLBACK_RELATIONS) if not D.has_edge(u, v)]
    if not extra:
        return None
    D2 = D.copy()
    for u, v, d in extra:
        D2.add_edge(u, v, **d)
    return D2
