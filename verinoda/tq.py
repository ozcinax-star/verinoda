"""Typed questions, batched: closed questions about the code answered with one typed value each.

``verinoda tq`` and the MCP tool ``tq`` take up to :data:`MAX_QUESTIONS` questions, each in a line form or an
object form::

    calls create_order_handler place_order            {"type": "calls", "a": "...", "b": "...", "depth": 1}
    route "POST /orders" save_order depth=6           {"type": "route", "route": "POST /orders", "f": "..."}

and answer each with one value (yes / no / ? / ``>=N`` / the files that hold), its status, the ``file:line``
it rests on and, when it is not settled, why and the next step. Nothing new is analysed: every answer comes
from one engine - :mod:`verinoda.naming` and :mod:`verinoda.entail` (exists, which), :mod:`verinoda.graphquery`
(calls, reaches, route, writes, reads, callers, q), :mod:`verinoda.taint` and the test map
(:mod:`verinoda.testmap`) - and its status is the weaker of that engine's own and the type's ceiling; tq never
raises one. A "no" is an absence: never verified, always with what was not seen and how to see it. Counts are
lower bounds. Names resolve exactly or not at all: an ambiguous name is unknown with its candidates.

The batch shares one freshness check, one route table and one lines cache; questions run in order, with no
thread, no model and no network, so the same batch on the same index gives the same bytes (``seconds`` aside).
Nothing is written: answers are not stored as claims.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
import time
from pathlib import Path

SCHEMA = "verinoda.tq/1"
MAX_QUESTIONS = 20
MAX_LINE_CHARS = 4000
MAX_NAME_CHARS = 300
EXPANSIONS_PER_QUESTION = 200_000
EXPANSIONS_PER_BATCH = 1_000_000
TIMEOUT_S = 30.0
ROWS_SHOWN = 5
CANDIDATES = 3
TYPES = ("exists", "which", "calls", "reaches", "route", "writes", "reads", "callers", "taint", "tested", "q")
# (positional fields, keys allowed, default depth or None, depth range)
_FORMS = {
    "exists": (("name",), {"scope"}, None),
    "which": (("name", "in"), set(), None),
    "calls": (("a", "b"), {"depth"}, (1, 1, 10)),
    "reaches": (("a", "b"), {"depth"}, (6, 1, 10)),
    "route": (("route", "f"), {"depth"}, (6, 0, 10)),
    "writes": (("h", "t"), {"depth"}, (4, 0, 10)),
    "reads": (("h", "t"), {"depth"}, (4, 0, 10)),
    "callers": (("f",), set(), None),
    "taint": (("s", "k"), {"in"}, None),
    "tested": (("t", "f"), set(), None),
    "q": (("query",), {"as"}, None),
}
GRAMMAR = ('exists NAME [scope=lib]; which NAME in A.py|B.py; calls A B [depth=1]; reaches A B [depth=6]; '
           'route "POST /path" F [depth=6]; writes|reads H TABLE [depth=4]; callers F; taint SOURCE SINK '
           '[in=PATH,...]; tested TEST_ID F; q "QUERY" [as=bool|count|rows]; any: need=inference|verified. '
           'Object form: {"type", fields: name, in, a, b, route, f, h, t, s, k, query, as, depth, id, need}.')
HEAD_NOTE = "no = absent from the static graph; >=N = lower bound"


class BatchError(ValueError):
    """The batch itself cannot be read (no question, too many, not JSON)."""


class QuestionError(ValueError):
    """One question cannot be read; the others are still answered."""


# -- statuses -------------------------------------------------------------------------------------------

def _order() -> list[str]:
    from verinoda import claims

    order = list(claims.ORDER)
    order.insert(order.index("unknown"), "stale")   # a past observation of code that changed since
    return order


ORDER = _order()
VERIFIED = {"experiment_verified", "statically_verified", "primary_source_verified", "observed"}


def weaker(a: str, b: str) -> str:
    """The weaker of two statuses (``claims.ORDER``, with ``stale`` just above ``unknown``)."""
    return a if ORDER.index(a) >= ORDER.index(b) else b


def _enough(status: str, need: str) -> bool:
    if need == "verified":
        return status in VERIFIED
    return ORDER.index(status) <= ORDER.index("strong_inference")


# -- the question forms ---------------------------------------------------------------------------------

def _tokens(line: str) -> list[tuple[str, bool]]:
    """Words of a line form: ``(text, quoted)``; ``"..."`` or ``'...'`` keep spaces (``\\"`` escapes)."""
    out: list[tuple[str, bool]] = []
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c.isspace():
            i += 1
            continue
        if c in "'\"":
            j, buf = i + 1, []
            while True:
                if j >= n:
                    raise QuestionError(f"a quoted operand is not closed (column {i + 1})")
                ch = line[j]
                if ch == "\\" and j + 1 < n and line[j + 1] in (c, "\\"):
                    buf.append(line[j + 1])
                    j += 2
                    continue
                if ch == c:
                    break
                buf.append(ch)
                j += 1
            out.append(("".join(buf), True))
            i = j + 1
            continue
        j = i
        while j < n and not line[j].isspace():
            j += 1
        out.append((line[i:j], False))
        i = j
    return out


_KEY = re.compile(r"^(depth|in|as|need|scope|id)=(.*)$", re.S)
_ID = re.compile(r"^[\w.-]{1,40}$")


def parse_line(line: str) -> dict:
    """The object form of one line-form question (:class:`QuestionError` when it cannot be read)."""
    if not isinstance(line, str) or not line.strip():
        raise QuestionError("an empty question")
    if len(line) > MAX_LINE_CHARS:
        raise QuestionError(f"a question of more than {MAX_LINE_CHARS} characters")
    toks = _tokens(line)
    typ = toks[0][0].lower()
    if typ not in _FORMS or toks[0][1]:
        raise QuestionError(f"unknown question type {toks[0][0]!r}; types: {', '.join(TYPES)}")
    obj: dict = {"type": typ}
    pos: list[str] = []
    for text, quoted in toks[1:]:
        m = None if quoted else _KEY.match(text)
        if m:
            if m.group(1) in obj:
                raise QuestionError(f"{m.group(1)}= is given twice")
            obj[m.group(1)] = m.group(2)
        else:
            pos.append(text)
    if typ == "which":   # which NAME in A|B|C
        if len(pos) != 3 or pos[1].lower() != "in":
            raise QuestionError("write: which NAME in A.py|B.py")
        pos = [pos[0], pos[2]]
    names = _FORMS[typ][0]
    if len(pos) != len(names):
        raise QuestionError(f"{typ} takes {len(names)} operand(s) ({' '.join(n.upper() for n in names)}), "
                            f"{len(pos)} given")
    obj.update(zip(names, pos))
    return normalize(obj)


def normalize(obj: dict) -> dict:
    """The checked object form: known type, its fields, depth and options in range."""
    if not isinstance(obj, dict):
        raise QuestionError("a question is a line of text or an object with a type")
    typ = str(obj.get("type") or "").lower()
    if typ not in _FORMS:
        raise QuestionError(f"unknown question type {obj.get('type')!r}; types: {', '.join(TYPES)}")
    names, keys, depth = _FORMS[typ]
    allowed = {"type", "id", "need", *names, *keys}
    extra = sorted(set(obj) - allowed)
    if extra:
        raise QuestionError(f"{typ} has no field {', '.join(extra)}")
    out: dict = {"type": typ}
    for f in names:
        v = obj.get(f)
        if f == "in" and typ == "which":
            opts = v.split("|") if isinstance(v, str) else v
            if not isinstance(opts, list) or not opts or not all(isinstance(o, str) and o.strip() for o in opts):
                raise QuestionError("which needs its files: in A.py|B.py")
            out[f] = [o.strip().replace("\\", "/").removeprefix("./") for o in opts][:10]
            continue
        if not isinstance(v, str) or not v.strip():
            raise QuestionError(f"{typ} needs {f.upper()}")
        if len(v) > (MAX_LINE_CHARS if f == "query" else MAX_NAME_CHARS):
            raise QuestionError(f"{f.upper()} is too long")
        out[f] = v.strip()
    if depth is not None:
        d = obj.get("depth", depth[0])
        try:
            d = int(d)
        except (TypeError, ValueError):
            raise QuestionError(f"depth is a whole number, {d!r} given") from None
        if not depth[1] <= d <= depth[2]:
            raise QuestionError(f"{typ} depth is {depth[1]}..{depth[2]}")
        out["depth"] = d
    if "scope" in keys:
        s = obj.get("scope", "project")
        if s not in ("project", "lib"):
            raise QuestionError("scope is project or lib")
        out["scope"] = s
    if "as" in keys:
        a = obj.get("as", "bool")
        if a not in ("bool", "count", "rows"):
            raise QuestionError("as is bool, count or rows")
        out["as"] = a
    if "in" in keys:
        v = obj.get("in") or []
        paths = v.split(",") if isinstance(v, str) else v
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            raise QuestionError("in= is a comma-separated list of paths")
        out["in"] = sorted({p.strip().replace("\\", "/").strip("/") for p in paths if p.strip()})
    need = obj.get("need")
    if need is not None:
        if need not in ("inference", "verified"):
            raise QuestionError("need is inference or verified")
        out["need"] = need
    if obj.get("id") is not None:
        if not isinstance(obj["id"], str) or not _ID.match(obj["id"]):
            raise QuestionError("an id is 1-40 letters, digits, '.', '_' or '-'")
        out["id"] = obj["id"]
    return out


def echo(spec: dict) -> str:
    """A question in its line form (for the CLI's echo)."""
    def w(s: str) -> str:
        return s if s and not re.search(r"[\s\"'=]", s) else '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    t = spec["type"]
    names = _FORMS[t][0]
    if t == "which":
        parts = [t, w(spec["name"]), "in", "|".join(spec["in"])]
    else:
        parts = [t, *(w(spec[f]) for f in names)]
    for k in ("depth", "scope", "as"):
        if k in spec and spec[k] != (_FORMS[t][2] or (None,))[0] and not (k == "scope" and spec[k] == "project") \
                and not (k == "as" and spec[k] == "bool"):
            parts.append(f"{k}={spec[k]}")
    if t == "taint" and spec.get("in"):
        parts.append("in=" + ",".join(spec["in"]))
    if spec.get("need"):
        parts.append(f"need={spec['need']}")
    return " ".join(parts)


def read_batch(questions) -> list[tuple[str, dict | None, str | None]]:
    """``[(id, spec or None, error or None)]``; :class:`BatchError` when the batch itself is unusable."""
    if not isinstance(questions, list):
        raise BatchError("questions is a list of lines or objects")
    if not questions:
        raise BatchError("no question")
    if len(questions) > MAX_QUESTIONS:
        raise BatchError(f"{len(questions)} questions: at most {MAX_QUESTIONS} per batch")
    out, used = [], set()
    for k, q in enumerate(questions, 1):
        qid = f"q{k}"
        try:
            spec = parse_line(q) if isinstance(q, str) else normalize(q)
            qid = spec.pop("id", None) or qid
            if qid in used:
                raise QuestionError(f"the id {qid} is used twice")
            used.add(qid)
            out.append((qid, spec, None))
        except QuestionError as exc:
            used.add(qid)
            out.append((qid, None, str(exc)))
    return out


# -- answering ------------------------------------------------------------------------------------------

def _bare(label: str) -> str:
    return str(label or "").strip().lstrip(".").split("(")[0].rpartition(".")[2]


def _ans(answer=None, status: str = "unknown", at=None, **extra) -> dict:
    out = {"answer": answer, "status": status}
    if at:
        out["at"] = list(dict.fromkeys(a for a in at if a))
    out.update({k: v for k, v in extra.items() if v not in (None, [], "")})
    return out


class _Batch:
    def __init__(self, repo: Path, g, *, verify: bool, mcp: bool, deadline: float, expansions: int,
                 fresh: dict | None):
        from verinoda import freshness, graphquery

        self.repo, self.g, self.verify, self.mcp = repo, g, verify, mcp
        self.deadline, self.expansions = deadline, expansions
        self.ctx = graphquery.Shared()
        if fresh is None:
            self.ctx.computed["fresh"] += 1
            try:
                fresh = freshness.check(repo)
            except Exception as exc:  # noqa: BLE001 - a failed check is said, never taken for "fresh"
                fresh = {"checked": False, "why": f"{type(exc).__name__}", "count": 0, "files": []}
        self.ctx.fresh = fresh
        self.stale = set(fresh.get("files") or [])
        self.memo: dict[str, tuple] = {}
        self.taints: dict[tuple, dict] = {}
        self.cut = False
        self.update = "index_update" if mcp else "verinoda update"

    # names
    def loc(self, n: str) -> str | None:
        f, ln = self.g.file(n), self.g.line(n)
        return f"{f}:{ln}" if f and ln else f

    def cand(self, nodes) -> list[str]:
        return [f"{self.g.label(n)} {self.loc(n) or ''}".strip() for n in list(nodes)[:CANDIDATES]
                if n in self.g.G]

    def resolve(self, text: str) -> tuple:
        """``("ok", nodes)``, ``("absent", why, candidates)`` or ``("unknown", why, next, candidates)``."""
        if text in self.memo:
            return self.memo[text]
        from verinoda import naming

        r = naming.resolve(self.g, text, stale=self.stale)
        if r.status == naming.EXACT:
            out: tuple = ("ok", list(r.overloads or [r.node]))
        elif r.status == naming.AMBIGUOUS:
            out = ("unknown", f"`{text}` names {len(r.candidates)} symbols", "pass path/file.py::symbol",
                   self.cand(r.candidates))
        elif r.status == naming.NOT_INDEXED:
            out = ("unknown", f"`{text}` is in a file changed since the index", self.update, [])
        elif r.status == naming.NOT_A_SYMBOL:
            out = ("unknown", f"`{text}` is spelled at {r.site} but is not a symbol of the index",
                   "pass path/file.py::symbol", self.cand(r.candidates))
        else:   # not_found, unresolved, similar: no exact name (a similar one is never used)
            out = ("absent", f"no symbol named `{text}` in the index", self.cand(r.candidates))
        self.memo[text] = out
        return out

    def need_nodes(self, text: str):
        """The nodes of an exact name, or the unknown answer that stands for them."""
        r = self.resolve(text)
        if r[0] == "ok":
            stale = sorted({self.g.file(n) for n in r[1]} & self.stale)
            if stale:
                return None, _ans(why=f"{stale[0]} changed since the index", next=self.update)
            return r[1], None
        if r[0] == "absent":
            return None, _ans(why=r[1], next="pass path/file.py::symbol", candidates=r[2])
        return None, _ans(why=r[1], next=r[2], candidates=r[3])

    def lines(self, f: str) -> list[str] | None:
        cache = self.ctx.lines_cache
        if f not in cache:
            from verinoda.index import file_lines

            cache[f] = file_lines(self.repo / f)
        return cache[f]

    def def_ok(self, n: str) -> bool | None:
        """Does the definition line still name the symbol (Python, re-read now)? None: not Python or unreadable."""
        from verinoda import entail

        f, sp = self.g.file(n), self.g.span(n)
        if not self.verify or not f or not sp or self.g.is_file_node(n):
            return None
        # from the span's first line (a decorator, maybe) to the node's own line (the def)
        return entail.def_around(self.repo, f, sp[0], max(sp[0], self.g.line(n) or sp[0]), _bare(self.g.label(n)))

    # graph queries
    def gq(self, query, *, verify: bool, route: str | None = None) -> dict | None:
        from verinoda import graphquery

        left = self.deadline - time.monotonic()
        if left <= 0:
            return None
        res = graphquery.run(self.repo, query=query, ctx=self.ctx, graph=self.g, max_rows=50,
                             max_expansions=self.expansions, timeout=left, verify=verify, route=route)
        return None if _cut(res) else res

    def budget(self) -> dict:
        self.cut = True
        return _ans(why="the batch budget ran out before this question was settled", cut=True)


def _cut(res: dict) -> bool:
    """Did a budget stop the walk? Rows past the row cap are not a cut: a row found is found, a count goes on."""
    return not res["complete"] and res.get("truncated") != "max_rows"


def _hops(row: dict) -> list[dict]:
    out: list[dict] = []
    for p in row.get("evidence", {}).get("paths", []):
        out += p
    return out


def _best(rows: list[dict]) -> dict:
    return min(rows, key=lambda r: ORDER.index(r["status"]) if r["status"] in ORDER else len(ORDER))


def _path_answer(b: _Batch, rows: list[dict], *, ceiling: str, target: str, pre: list[str] | None = None) -> dict:
    row = _best(rows)
    if row["status"] == "unknown":
        return _ans(why=f"cites {(row.get('stale_files') or ['a file'])[0]}, changed since the index",
                    next=b.update)
    hops = _hops(row)
    status = weaker(row["status"], ceiling)
    at = (pre or []) + [h["at"] for h in hops]
    weak = next((h for h in hops if h["confidence"] != "EXTRACTED"), None)
    why = nxt = None
    if weak is not None:
        why = f"INFERRED {weak['relation']} edge {weak['at']}"
        if weak["relation"] == "calls" and weak.get("at"):
            nxt = f"verinoda resolve-call {weak['at']} {_bare(weak['to'])}"
    elif status == "strong_inference" and b.verify and ceiling == "statically_verified":
        probs = (row.get("verified") or {}).get("problems") or []
        if probs:
            why = probs[0][:160]
            site = next((h["at"] for h in hops if h.get("at") and not h["at"].rsplit(":", 1)[0].endswith(".py")),
                        None)
            if site:
                nxt = f"verinoda resolve-call {site} {_bare(target)}"
    return _ans(True, status, at, why=why, next=nxt)


def _ids_query(b: _Batch, a_nodes, b_nodes, rel, lo, hi):
    from verinoda import graphquery as gq

    return gq.build([gq.path(gq.node("a"), gq.edge(*rel, lo=lo, hi=hi), gq.node("b"))],
                    gq.BoolOp("and", [gq.id_in("a", a_nodes), gq.id_in("b", b_nodes)]), ["a", "b"])


def _ast_calls(b: _Batch, a_nodes: list[str], token: str) -> tuple[list[str], bool, bool]:
    """(call sites named ``token`` in A's bodies, ``token`` spelled in A, every A read as Python)."""
    from verinoda import entail

    sites, spelled, python = [], False, True
    rx = re.compile(rf"(?<![\w]){re.escape(token)}(?![\w])")
    for n in a_nodes:
        f, sp = b.g.file(n), b.g.span(n)
        lines = b.lines(f) if f and f.endswith((".py", ".pyi")) and sp else None
        tree = entail._py_tree("\n".join(lines)) if lines is not None else None
        at = {sp[0], b.g.line(n)}   # the span may start at a decorator; the def line is the node's line
        fns = [d for d in ast.walk(tree) if isinstance(d, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
               and d.lineno in at] if tree is not None else []
        if not fns:
            python = False
            continue
        sites += [f"{f}:{ln}" for ln in entail.calls_in(tree, fns[0], token)]
        body = lines[fns[0].lineno:(fns[0].end_lineno or fns[0].lineno)]   # past the def line
        spelled = spelled or any(rx.search(ln) for ln in body)
    return sorted(set(sites)), spelled, python


def _calls(b: _Batch, s: dict, reaches: bool) -> dict:
    a_nodes, bad = b.need_nodes(s["a"])
    if bad:
        return bad
    b_nodes, bad = b.need_nodes(s["b"])
    if bad:
        return bad
    d = s["depth"]
    res = b.gq(_ids_query(b, a_nodes, b_nodes, ("calls", "indirect_call"), 1, d), verify=b.verify)
    if res is None:
        return b.budget()
    if res["rows"]:
        return _path_answer(b, res["rows"], ceiling="statically_verified", target=s["b"])
    observe = f"verinoda observe --for {s['a']} {s['b']}"
    token = _bare(b.g.label(b_nodes[0]))
    if d == 1 and not reaches:
        sites, spelled, python = _ast_calls(b, a_nodes, token)
        if sites:   # a call of that name the graph did not bind to B: not a "no"
            return _ans(why=f"a call named {token} at {sites[0]} has no edge to {s['b']} in the graph",
                        next=f"verinoda resolve-call {sites[0]} {token}")
        if python and not spelled:
            return _ans(False, "strong_inference", why=f"no call named {token} in {s['a']} (AST) and the name "
                        "is not spelled there; a call through another name is not seen", next=observe)
        return _ans(False, "weak_inference", why=f"no calls edge; {token} is spelled in {s['a']} (callbacks, "
                    "getattr, DI not resolved)" if spelled else "no calls edge (not read as Python)", next=observe)
    return _ans(False, "weak_inference", why=f"no calls path of 1..{d} edges in the static graph (callbacks, "
                "getattr, DI not resolved)", next=observe)


def _route(b: _Batch, s: dict) -> dict:
    from verinoda import graphquery as gq

    want = s["route"].split()
    method, rpath = (want[0].upper(), want[1]) if len(want) == 2 else ("ANY", want[0])
    table = gq.route_table(b.g, b.ctx)
    hits: dict[str, dict] = {}
    for h, rs in sorted(table.items()):
        for r in rs:
            ms, _, p = r["route"].partition(" ")
            if p == rpath and (method == "ANY" or ms == "ANY" or method in ms.split("|")):
                hits.setdefault(h, r)
    if not hits:
        known = sorted({r["route"] for rs in table.values() for r in rs})
        why = (f"no route {method} {rpath} in the route table ({len(known)} routes)" if not b.ctx.route_error
               else b.ctx.route_error)
        return _ans(why=why, next="verinoda routes", candidates=known[:5])
    f_nodes, bad = b.need_nodes(s["f"])
    if bad:
        return bad
    q = gq.build([gq.path(gq.node("h", "handler"), gq.edge("calls", lo=0, hi=s["depth"]), gq.node("f"))],
                 gq.BoolOp("and", [gq.id_in("h", list(hits)), gq.id_in("f", f_nodes)]), ["h", "f"])
    asked = next(iter(hits.values()))
    res = b.gq(q, verify=b.verify, route=asked["route"])
    if res is None:
        return b.budget()
    if res["rows"]:
        row = _best(res["rows"])
        h_id = row["values"]["h"]["id"]
        return _path_answer(b, [row], ceiling="statically_verified", target=s["f"], pre=[hits[h_id]["at"]])
    return _ans(False, "weak_inference", why=f"no calls path of 0..{s['depth']} edges from the handler in the "
                "static graph", next=f"verinoda observe --for {s['f']}")


def _tables(b: _Batch, name: str) -> list[str]:
    from verinoda import dataschema

    ts = [n for n in b.g.G.nodes if str(n).startswith(dataschema.NODE_PREFIX)]
    exact = sorted(n for n in ts if b.g.label(n) == name)
    return exact or sorted(n for n in ts if str(b.g.label(n)).lower() == name.lower())


def _rw(b: _Batch, s: dict, rel: str) -> dict:
    from verinoda import dataschema
    from verinoda import graphquery as gq

    tables = _tables(b, s["t"])
    if not tables:
        known = sorted({str(b.g.label(n)) for n in b.g.G.nodes if str(n).startswith(dataschema.NODE_PREFIX)})
        return _ans(why=f"no table named {s['t']} in the index", next="verinoda schema", candidates=known[:5])
    h_nodes, bad = b.need_nodes(s["h"])
    if bad:
        return bad
    q = gq.build([gq.path(gq.node("h"), gq.edge("calls", lo=0, hi=s["depth"]), gq.node("f"), gq.edge(rel),
                          gq.node("t"))],
                 gq.BoolOp("and", [gq.id_in("h", h_nodes), gq.id_in("t", tables)]), ["h", "f", "t"])
    res = b.gq(q, verify=False)   # a writes_table / reads_table edge is not a call site re-reading can confirm
    if res is None:
        return b.budget()
    if res["rows"]:
        return _path_answer(b, res["rows"], ceiling="strong_inference", target=s["t"])
    return _ans(False, "weak_inference", why=f"no {rel} edge within {s['depth']} calls in the static graph",
                next=f"verinoda observe --for {s['h']}")


def _callers(b: _Batch, s: dict) -> dict:
    from verinoda import graphquery as gq

    f_nodes, bad = b.need_nodes(s["f"])
    if bad:
        return bad
    q = gq.build([gq.path(gq.node("c"), gq.edge("calls", "indirect_call"), gq.node("x"))],
                 gq.id_in("x", f_nodes), ["count(c)"])
    res = b.gq(q, verify=False)
    if res is None:
        return b.budget()
    row = res["rows"][0]
    if row["status"] == "unknown":
        return _ans(why=f"cites {(row.get('stale_files') or ['a file'])[0]}, changed since the index",
                    next=b.update)
    n = row["values"]["count(c)"]
    at = [p[0]["at"] for e in row.get("examples", []) for p in e.get("paths", []) if p]
    why = "a call through another name (callback, getattr, DI) is not counted" if n == 0 else None
    if row["status"] == "weak_inference":
        why = "an INFERRED calls edge is counted"
    return _ans(n, weaker(row["status"], "strong_inference"), sorted(at)[:CANDIDATES], bound="at_least", why=why)


def _taint(b: _Batch, s: dict) -> dict:
    from verinoda import taint

    key = tuple(s.get("in") or ())
    if key not in b.taints:
        try:
            b.taints[key] = taint.run(b.repo, list(key), graph=b.g, local=True)
        except Exception as exc:  # noqa: BLE001 - taint's own errors (a bad scope) settle this question only
            b.taints[key] = {"error": str(exc)[:200]}
    res = b.taints[key]
    if "error" in res:
        return _ans(why=res["error"], next="check in=PATH")

    def hit(want: str, side: dict) -> bool:
        m, k = str(side.get("match") or ""), str(side.get("kind") or "")
        return want in (m, k) or m.startswith(want + ".") or m.endswith("." + want)

    found = [f for f in res["findings"] if hit(s["s"], f["source"]) and hit(s["k"], f["sink"])]
    scope = ",".join(key) or "the project"
    if found:
        f = found[0]
        return _ans(True, "strong_inference", [f["source"]["at"], f["sink"]["at"]],
                    via=f"{f['rule']}, {len(f['path'])} steps")
    if res.get("truncated"):
        return _ans(why="taint stopped at its finding limit", next="add in=PATH")
    if not res.get("files"):
        return _ans(why=f"no Python file in {scope}", next="check in=PATH")
    deeper = f" --depth {taint.slicing.MAX_DEPTH}" if res.get("depth", 0) < taint.slicing.MAX_DEPTH else ""
    return _ans(False, "weak_inference", why=f"no {s['s']} -> {s['k']} path in {scope} at depth {res['depth']}; "
                "data flow only, not a proof of safety", next=" ".join(["verinoda taint", *key, *deeper.split()]))


def _qual(g, n: str) -> str:
    name = _bare(g.label(n))
    owners = [u for u, _d in g.in_edges(n, {"method"})]
    return f"{_bare(g.label(owners[0]))}.{name}" if owners else name


def _tested(b: _Batch, s: dict) -> dict:
    from verinoda import testmap, treestate
    from verinoda.paths import db_path

    t = s["t"]
    rid = testmap.runner_id(t)
    nxt = f"verinoda observe {rid}"
    db = db_path(b.repo)
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return _ans(why="test never traced", next=nxt)
    try:
        tests = [dict(r) for r in conn.execute("SELECT * FROM test_map_tests ORDER BY test")
                 if testmap.runner_id(r["test"]) == rid]
    except sqlite3.Error:
        tests = []
    if not tests:
        conn.close()
        return _ans(why="test never traced", next=nxt)
    f_nodes, bad = b.need_nodes(s["f"])
    if bad:
        conn.close()
        return bad

    class _Ro:   # the two reads testmap.mapping_current makes, on the read-only connection
        @staticmethod
        def all(sql, args=()):
            return [dict(r) for r in conn.execute(sql, args)]

    def versions(p: str) -> set[str]:
        try:
            return {treestate.content_id((b.repo / p).read_bytes())}
        except OSError:
            return set()

    names = {t_["test"] for t_ in tests}
    ran, fixture_anywhere = False, False
    for n in f_nodes:
        f, qual = b.g.file(n), _qual(b.g, n)
        for r in conn.execute("SELECT test, qual, fixture FROM test_map WHERE path = ?", (f,)):
            if testmap._covers(r["qual"], qual):
                ran = ran or r["test"] in names
                fixture_anywhere = fixture_anywhere or bool(r["fixture"])
    current = all(testmap.mapping_current(_Ro, t_["test"], versions) for t_ in tests)
    conn.close()
    last = tests[-1]
    via = f"in run {last['run_id']} @ commit {(last['commit_sha'] or '?')[:12]}"
    at = [b.loc(n) for n in f_nodes]
    if ran:
        if current:
            return _ans(True, "observed", at, via=via)
        return _ans(True, "stale", at, via=via, why="a file the test ran changed since that run", next=nxt)
    passed = all(t_["outcome"] == "passed" for t_ in tests)
    complete = all(t_["complete"] for t_ in tests)
    if passed and complete and current and not fixture_anywhere:
        return _ans(False, "strong_inference", at, via=via, why=testmap.LIMITS[0], next=nxt)
    why = ("the test did not pass (it may have stopped early)" if not passed else
           "the trace was incomplete" if not complete else
           "a file the test ran changed since that run" if not current else
           f"{s['f']} ran in a fixture phase, recorded for the first test that used it only")
    return _ans(why=why, next=nxt)


def _exists(b: _Batch, s: dict) -> dict:
    if s.get("scope") == "lib":
        from verinoda import codecheck

        try:
            r = codecheck.api(b.repo, s["name"], env="auto")
        except Exception as exc:  # noqa: BLE001 - the environment could not be read: not decided
            return _ans(why=f"{type(exc).__name__}", next="pass env")
        if r.get("found") is True:
            return _ans(True, "strong_inference", [str(r["at"])] if r.get("at") else None)
        if r.get("found") is False:
            return _ans(False, "strong_inference", why=str(r.get("why") or "not in the installed library")[:160],
                        next="pass env")
        return _ans(why=f"not decided ({r.get('decided') or 'unknown'})", next="pass env")
    r = b.resolve(s["name"])
    if r[0] == "absent":
        return _ans(False, "strong_inference", why=r[1] + " (an index absence)",
                    next=f"{b.update} if it was just added", candidates=r[2])
    if r[0] == "unknown":
        return _ans(why=r[1], next=r[2], candidates=r[3])
    status, at = "statically_verified", []
    for n in r[1]:
        if b.g.file(n) in b.stale:
            return _ans(why=f"{b.g.file(n)} changed since the index", next=b.update)
        ok = b.def_ok(n)
        if ok is False:
            return _ans(why=f"{b.loc(n)} no longer names it", next=b.update)
        status = weaker(status, "statically_verified" if ok else "strong_inference")
        at.append(b.loc(n))
    return _ans(True, status, sorted(at))


def _which(b: _Batch, s: dict) -> dict:
    from verinoda import naming

    opts = s["in"]
    stale = [o for o in opts if o in b.stale]
    if stale:
        return _ans(why=f"{stale[0]} changed since the index", next=b.update)
    best, aside = naming.exact_nodes(b.g, s["name"], fallback=False)
    nodes = sorted(set(best) | set(aside))
    holds, at, status = [], [], "statically_verified"
    for o in opts:
        mine = [n for n in nodes if b.g.file(n) == o]
        if not mine:
            continue
        holds.append(o)
        for n in mine:
            ok = b.def_ok(n)
            if ok is False:
                return _ans(why=f"{b.loc(n)} no longer names it", next=b.update)
            status = weaker(status, "statically_verified" if ok else "strong_inference")
            at.append(b.loc(n))
    other = [b.loc(n) for n in nodes if b.g.file(n) not in opts][:CANDIDATES]
    if not holds:
        return _ans([], "strong_inference", why=f"no definition of `{s['name']}` in these files in the index",
                    next=f"{b.update} if it was just added", other=other)
    return _ans(holds, status, at, other=other)


def _q(b: _Batch, s: dict) -> dict:
    from verinoda import graphquery

    mode = s.get("as", "bool")
    left = b.deadline - time.monotonic()
    if left <= 0:
        return b.budget()
    try:
        res = graphquery.run(b.repo, s["query"], ctx=b.ctx, graph=b.g, max_rows=50,
                             max_expansions=b.expansions, timeout=left, verify=b.verify and mode != "count")
    except graphquery.QueryError as exc:
        raise QuestionError(exc.render()[:400]) from None
    if _cut(res):
        return b.budget()
    rows = res["rows"]
    counted = [c for c in res["columns"] if c.startswith("count(")]
    if any(r["status"] == "unknown" for r in rows):
        return _ans(why="a row cites a file changed since the index", next=b.update)
    if mode == "count":
        n = (rows[0]["values"][counted[0]] if rows else 0) if counted else len(rows)
        st = "strong_inference" if not rows else weaker(max((r["status"] for r in rows), key=ORDER.index),
                                                          "strong_inference")
        return _ans(n, st, bound="at_least")

    def where(r: dict) -> list[str]:
        ev = r.get("evidence") or (r.get("examples") or [{}])[0]
        return [x["at"] for x in ev.get("nodes", []) if x.get("at")] + [h["at"] for p in ev.get("paths", [])
                                                                         for h in p if h.get("at")]

    held = [r for r in rows if r.get("bindings", 1) and not (counted and not any(
        r["values"].get(c) for c in counted))]
    if mode == "bool":
        if not held:
            return _ans(False, "weak_inference", why="no row: an absence in the static graph",
                        next="verinoda observe, or a wider pattern")
        r = _best(held)
        return _ans(True, r["status"], where(r)[:ROWS_SHOWN])
    shown = held[:ROWS_SHOWN]
    vals = []
    for r in shown:
        vals.append(", ".join(f"{k}={v['label'] if isinstance(v, dict) else v}" for k, v in r["values"].items()))
    st = max((r["status"] for r in shown), key=ORDER.index) if shown else "weak_inference"
    return _ans(vals, st, [a for r in shown for a in where(r)[:1]], truncated=len(held) > ROWS_SHOWN or None)


_ADAPTERS = {"exists": _exists, "which": _which, "calls": lambda b, s: _calls(b, s, False),
             "reaches": lambda b, s: _calls(b, s, True), "route": _route,
             "writes": lambda b, s: _rw(b, s, "writes_table"), "reads": lambda b, s: _rw(b, s, "reads_table"),
             "callers": _callers, "taint": _taint, "tested": _tested, "q": _q}


def ask(repo: Path, questions, *, graph=None, verify: bool = True, need: str = "inference",
        timeout: float = TIMEOUT_S, max_expansions: int | None = None, fresh: dict | None = None,
        mcp: bool = False, max_chars: int | None = None) -> dict:
    """Answer a batch (:data:`SCHEMA`). :class:`BatchError` when the batch itself cannot be read.

    ``fresh``: a freshness check already made (:func:`verinoda.freshness.check`), else one is made here;
    ``mcp``: next steps name MCP tools; ``max_chars``: answers past it are dropped and listed in ``truncated``."""
    from verinoda import graphquery, index

    t0 = time.perf_counter()
    if need not in ("inference", "verified"):
        raise BatchError("need is inference or verified")
    if timeout <= 0 or (max_expansions is not None and max_expansions < 1):
        raise BatchError("timeout and max_expansions must be positive")
    batch = read_batch(questions)
    repo = Path(repo).resolve()
    g = graph if graph is not None else index.load(repo)
    per = max_expansions or min(EXPANSIONS_PER_QUESTION, EXPANSIONS_PER_BATCH // len(batch))
    b = _Batch(repo, g, verify=verify, mcp=mcp, deadline=time.monotonic() + timeout, expansions=per, fresh=fresh)
    answers: list[dict] = []
    done: dict[str, dict] = {}
    invalid = False
    for qid, spec, err in batch:
        row: dict = {"id": qid}
        if spec is not None:
            row["question"] = echo(spec)
        if err is None:
            key = json.dumps({k: v for k, v in spec.items() if k != "need"}, sort_keys=True)
            try:
                got = done.get(key)
                if got is None:
                    try:
                        got = _ADAPTERS[spec["type"]](b, spec)
                    except (QuestionError, graphquery.QueryError):
                        raise
                    except Exception as exc:  # noqa: BLE001 - one question never fails the batch
                        got = _ans(why=f"this question failed: {type(exc).__name__}: {exc}"[:200],
                                   next="ask it alone, or report it")
                    done[key] = got
                row.update(json.loads(json.dumps(got)))
            except graphquery.QueryError as exc:
                err = exc.render()[:400]
            except QuestionError as exc:
                err = str(exc)
        if err is not None:
            invalid = True
            row.update({"answer": None, "status": "invalid", "why": err[:400]})
        elif row["answer"] is not None and not _enough(row["status"], (spec or {}).get("need") or need):
            row["enough"] = False
        answers.append(_order_keys(row))
    cut_ids = [a["id"] for a in answers if a.get("cut")]
    for a in answers:
        if a.get("cut"):
            a["next"] = "ask again: " + ",".join(cut_ids)
    fresh_d = b.ctx.fresh or {}
    res: dict = {"schema": SCHEMA, "answers": answers, "complete": not b.cut,
                 "stale_files": len(b.stale), "freshness": "checked" if fresh_d.get("checked", True) is not False
                 else "not checked"}
    if invalid:
        res["grammar"] = GRAMMAR[:600]
    res["shared"] = dict(b.ctx.computed)
    if max_chars:
        _fit(res, max_chars)
    res["seconds"] = round(time.perf_counter() - t0, 3)
    return res


_KEY_ORDER = ("id", "question", "answer", "bound", "status", "at", "via", "why", "enough", "next", "candidates",
              "other", "measured", "cut", "truncated")


def _order_keys(row: dict) -> dict:
    return {k: row[k] for k in _KEY_ORDER if k in row}


def _fit(res: dict, max_chars: int) -> None:
    """Drop answers from the end until the JSON fits ``max_chars``; their ids go to ``truncated``."""
    dropped: list[str] = []
    while len(json.dumps(res, ensure_ascii=False)) > max_chars - 200 and len(res["answers"]) > 1:
        dropped.insert(0, res["answers"].pop()["id"])
        res["truncated"] = {"ids": dropped, "next": "ask again: " + ",".join(dropped)}


# -- rendering ------------------------------------------------------------------------------------------

def _value(a: dict) -> str:
    v = a["answer"]
    if a["status"] == "invalid":
        return "invalid"
    if v is None:
        return "?"
    if v is True:
        return "yes"
    if v is False:
        return "no"
    if isinstance(v, int):
        return f">={v}" if a.get("bound") == "at_least" else str(v)
    if isinstance(v, list):
        return "; ".join(map(str, v)) or "none"
    return str(v)


def render(res: dict, *, echo_questions: bool = True) -> str:
    """Plain text: one header line, one line per answer (``echo_questions``: the question after its id)."""
    n = len(res["answers"])
    fresh = ("index fresh" if not res["stale_files"] else f"{res['stale_files']} file(s) changed since the index") \
        if res.get("freshness") != "not checked" else "freshness not checked"
    out = [f"tq {n} question{'s' if n != 1 else ''}, {fresh}, {res['seconds']} s. {HEAD_NOTE}"]
    for a in res["answers"]:
        head = f"{a['id']} {a['question']}" if echo_questions and a.get("question") else a["id"]
        parts = [f"{head} = {_value(a)}", a["status"]]
        if a.get("at"):
            parts.append(", ".join(a["at"]))
        if a.get("via"):
            parts.append(a["via"])
        if a.get("other"):
            parts.append("other: " + ", ".join(a["other"]))
        if a.get("why"):
            parts.append(f"why: {a['why']}")
        if a.get("enough") is False:
            parts.append("enough: no")
        if a.get("next"):
            parts.append(f"next: {a['next']}")
        if a.get("candidates"):
            parts.append("candidates: " + ", ".join(a["candidates"]))
        out.append(" | ".join(parts))
    if res.get("truncated"):
        out.append(f"cut for size: {res['truncated']['next']}")
    if res.get("grammar"):
        out.append(f"grammar: {res['grammar']}")
    return "\n".join(out) + "\n"


def compact(res: dict) -> dict:
    """The JSON form without the echoed questions and the counters (what programs read)."""
    out = {k: v for k, v in res.items() if k != "shared"}
    out["answers"] = [{k: v for k, v in a.items() if k != "question"} for a in res["answers"]]
    return out
