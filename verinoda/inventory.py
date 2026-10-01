"""Inventories computed from search hits, not estimated: counts with their sites.

``verinoda inventory`` runs one or more named searches (the trigram search, :mod:`verinoda.trigram`), puts each hit
in a unit - its file, its line, or the innermost symbol that holds it (from the index) - keeps the units a
condition selects, and counts them by group (file, folder, extension, symbol). The condition is a small
expression over the searches' names, evaluated per unit on its hit counts::

    open and not close          # units with a hit of `open` and none of `close`
    calls >= 3 or (a and b)     # comparisons between a count and a whole number

It is read with :mod:`ast` and only names, ``and`` / ``or`` / ``not``, comparisons and whole numbers are allowed:
no call, attribute, subscript or anything else is ever evaluated (the "script" is data, not code).

Every count comes with the lines behind it. A count is exact only when every search read every file in scope and
kept every hit; otherwise the result is ``incomplete`` and the counts are lower bounds ("at least").
"""

from __future__ import annotations

import ast
import keyword
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

MAX_HITS = 20_000          # hits kept per search; beyond it the counts are lower bounds
SITES_PER_GROUP = 5
MAX_GROUPS = 50
UNITS = ("file", "line", "symbol")
MAX_WHERE_CHARS = 2000
MAX_WHERE_DEPTH = 40       # nesting of the condition's tree
MODULE = "(module level)"


class InventoryError(ValueError):
    pass


# -- the condition ---------------------------------------------------------------------------------

_CMP = {ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b, ast.Lt: lambda a, b: a < b,
        ast.LtE: lambda a, b: a <= b, ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b}


def parse_where(text: str, names: set[str]) -> ast.Expression:
    """The condition as a checked syntax tree; :class:`InventoryError` for anything but names of the searches,
    whole numbers, ``and`` / ``or`` / ``not``, comparisons and parentheses."""
    if len(text) > MAX_WHERE_CHARS:
        raise InventoryError(f"--where is over {MAX_WHERE_CHARS} characters")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise InventoryError(f"--where is not an expression: {exc.msg}") from None
    except (RecursionError, MemoryError, ValueError):
        raise InventoryError("--where is nested too deeply") from None
    stack = [(tree, 0)]
    while stack:   # depth first, iteratively: the tree's depth is checked before anything recurses over it
        node, d = stack.pop()
        if d > MAX_WHERE_DEPTH:
            raise InventoryError(f"--where is nested more than {MAX_WHERE_DEPTH} levels")
        stack.extend((c, d + 1) for c in ast.iter_child_nodes(node))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.Not, ast.Load)):
            continue
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            continue
        if isinstance(node, ast.Compare) and all(type(op) in _CMP for op in node.ops):
            if not all(isinstance(x, (ast.Name, ast.Constant)) for x in (node.left, *node.comparators)):
                raise InventoryError("--where compares counts and whole numbers only: a name or a number on each "
                                     "side of a comparison (and/or/not give true or false, not a count)")
            continue
        if isinstance(node, tuple(_CMP)):
            continue
        if isinstance(node, ast.Name):
            if node.id not in names:
                raise InventoryError(f"--where names {node.id!r}, which is no search (searches: "
                                     f"{', '.join(sorted(names))})")
            continue
        if isinstance(node, ast.Constant) and type(node.value) is int:
            continue
        raise InventoryError(f"--where may use only search names, whole numbers, and/or/not and comparisons "
                             f"(not {type(node).__name__})")
    return tree


def _eval(node, counts: Counter):
    if isinstance(node, ast.Expression):
        return _eval(node.body, counts)
    if isinstance(node, ast.BoolOp):
        vals = (_eval(v, counts) for v in node.values)
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.UnaryOp):
        return not _eval(node.operand, counts)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, counts)
        for op, right in zip(node.ops, node.comparators):
            r = _eval(right, counts)
            if not _CMP[type(op)](left, r):
                return False
            left = r
        return True
    if isinstance(node, ast.Name):
        return counts[node.id]
    return node.value   # a whole number (parse_where allowed nothing else)


def evaluate(tree: ast.Expression, counts: Counter) -> bool:
    return bool(_eval(tree, counts))


def monotone(tree: ast.Expression | None) -> bool:
    """Whether a lost hit can only make the condition false, never true: no ``not``, and every comparison a
    count at least a number (``a >= 2``, ``2 <= a``). Only then are incomplete counts lower bounds."""
    if tree is None:
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.UnaryOp):
            return False
        if isinstance(node, ast.Compare):
            if len(node.ops) != 1:
                return False
            op, left, right = node.ops[0], node.left, node.comparators[0]
            if not ((isinstance(op, (ast.Gt, ast.GtE)) and isinstance(left, ast.Name)
                     and isinstance(right, ast.Constant))
                    or (isinstance(op, (ast.Lt, ast.LtE)) and isinstance(left, ast.Constant)
                        and isinstance(right, ast.Name))):
                return False
    return True


# -- the inventory ---------------------------------------------------------------------------------

def _group_of(how: str, rel: str, symbol: str | None) -> str:
    if how == "file":
        return rel
    if how.startswith("folder"):
        depth = int(how.split(":", 1)[1]) if ":" in how else None
        parts = PurePosixPath(rel).parts[:-1]
        if depth is not None:
            parts = parts[:depth]
        return "/".join(parts) or "."
    if how == "ext":
        return PurePosixPath(rel).suffix.lower() or "(none)"
    if how == "symbol":
        return symbol or f"{rel} {MODULE}"
    return "all"


def check_group_by(how: str) -> str:
    if how in ("file", "folder", "ext", "symbol", "none"):
        return how
    if how.startswith("folder:") and how[7:].isdigit() and int(how[7:]) >= 1:
        return how
    raise InventoryError(f"--group-by must be file, folder, folder:N, ext, symbol or none (not {how!r})")


def run(repo: Path, searches: list[tuple[str, str]], *, where: str | None = None, unit: str = "file",
        group_by: str = "file", fixed: bool = False, ignore_case: bool = False, paths: list[str] | None = None,
        timeout: float = 60.0, max_groups: int = MAX_GROUPS) -> dict:
    """Run the searches, select the units the condition keeps, and count them by group (see the module
    docstring). ``searches``: ``(name, pattern)`` pairs."""
    from verinoda import trigram

    repo = Path(repo).resolve()
    if not searches:
        raise InventoryError("give at least one search")
    names = [n for n, _p in searches]
    bad = [n for n in names if not n.isidentifier() or keyword.iskeyword(n) or n in ("True", "False", "None")
           or unicodedata.normalize("NFKC", n) != n]
    if bad:
        raise InventoryError(f"a search name must be a plain word (letters, digits, _; no Python keyword): "
                             f"{', '.join(bad)}")
    if len(set(names)) != len(names):
        raise InventoryError("two searches have the same name")
    if unit not in UNITS:
        raise InventoryError(f"--unit must be one of {', '.join(UNITS)}")
    group_by = check_group_by(group_by)
    if group_by == "symbol" and unit == "file":
        raise InventoryError("--group-by symbol needs --unit symbol or line: a file unit holds hits of several "
                             "symbols")
    if max_groups < 1:
        raise InventoryError("--max-groups must be at least 1")
    tree = parse_where(where, set(names)) if where else None

    t0 = time.perf_counter()
    g = None
    if unit == "symbol" or group_by == "symbol":
        from verinoda import index
        from verinoda.paths import graph_path

        if not graph_path(repo).exists():
            raise InventoryError("--unit symbol and --group-by symbol read the index: run `verinoda scan` first")
        g = index.load(repo)

    per_search = {}
    hits: list[tuple[str, str, int, str]] = []   # (search, file, line, text)
    complete = True
    not_read: set[str] = set()
    for name, pattern in searches:
        res = trigram.search(repo, pattern, ignore_case=ignore_case, fixed=fixed, paths=paths,
                             max_results=MAX_HITS, timeout=timeout)
        info = {"pattern": pattern, "total": res["total"], "files_matched": res["files_matched"],
                "kept": len(res["matches"]), "status": res["status"], "truncated": res["truncated"]}
        if res["truncated"] or res["status"] != "observed":
            complete = False
        not_read.update((res.get("not_read") or {}).get("files") or [])
        per_search[name] = info
        for m in res["matches"]:
            rel, _, line = m["at"].rpartition(":")
            hits.append((name, rel, int(line), m["text"]))

    unit_counts: dict[tuple, Counter] = defaultdict(Counter)
    unit_sites: dict[tuple, list[tuple[str, int, str, str]]] = defaultdict(list)
    unit_symbol: dict[tuple, str | None] = {}
    for name, rel, line, text in hits:
        sym, nid = None, None
        if g is not None:
            nid = g.symbol_at(rel, line)
            if nid:   # a label repeats (every __exit__ is `.__exit__()`): the definition line tells them apart
                sym = f"{rel}::{g.label(nid).strip('.')} (line {g.line(nid)})"
        key = (rel,) if unit == "file" else (rel, line) if unit == "line" else (rel, nid or MODULE)
        unit_counts[key][name] += 1
        unit_symbol.setdefault(key, sym)
        unit_sites[key].append((rel, line, name, text.strip()[:120]))
    kept = [k for k in unit_counts if tree is None or evaluate(tree, unit_counts[k])]
    zero_true = bool(tree is not None and evaluate(tree, Counter()))

    groups: dict[str, dict] = {}
    for k in kept:
        grp = _group_of(group_by, k[0], unit_symbol.get(k))
        row = groups.setdefault(grp, {"group": grp, "units": 0, "hits": Counter(), "sites": []})
        row["units"] += 1
        row["hits"].update(unit_counts[k])
        row["sites"].extend(unit_sites[k])
    rows = sorted(groups.values(), key=lambda r: (-r["units"], -sum(r["hits"].values()), r["group"]))
    out_rows = [{"group": r["group"], "units": r["units"], "hits": dict(sorted(r["hits"].items())),
                 "sites": [f"{f}:{ln} [{n}] {tx}" for f, ln, n, tx in sorted(r["sites"])[:SITES_PER_GROUP]],
                 "sites_total": len(r["sites"])}
                for r in rows[:max_groups]]
    limits = list(trigram.LIMITS)
    if zero_true:
        limits.append("the condition also holds for a unit with no hit of any search; only units with a hit are "
                      "counted, so a count of what is absent everywhere is not given")
    if unit == "symbol" or group_by == "symbol":
        limits.append("symbols are the index's: a file changed since the index may place a hit in the wrong symbol "
                      "(run `verinoda update`)")
    status = "observed" if complete else "incomplete"
    bounded = complete or monotone(tree)
    res = {
        "status": status,
        "exact": complete,
        "lower_bound": not complete and bounded,
        "searches": per_search,
        "where": where, "unit": unit, "group_by": group_by,
        "units": len(kept), "units_with_hits": len(unit_counts),
        "groups": out_rows, "groups_total": len(rows), "truncated": len(rows) > max_groups,
        "timing_s": round(time.perf_counter() - t0, 3),
        "limits": limits,
    }
    if not complete:
        res["note"] = (("the counts are lower bounds: " if bounded else
                        "the counts are not bounded either way (the condition has `not` or a comparison that a lost "
                        "hit can turn true): ") + "; ".join(
            f"{n}: {i['total']} hits, {i['kept']} kept" + (" (time limit or unreadable files)"
                                                           if i["status"] != "observed" else "")
            for n, i in per_search.items() if i["truncated"] or i["status"] != "observed"))
        if not_read:
            res["not_read"] = sorted(not_read)[:20]
    return res


def render(res: dict) -> str:
    at_least = "" if res["exact"] else "at least " if res.get("lower_bound") else "incomplete: "
    head = (f"{at_least}{res['units']} {res['unit']}(s)" + (f" where {res['where']}" if res["where"] else "")
            + f" ({res['status']}); searches: "
            + ", ".join(f"{n} = {i['pattern']!r} {i['total']} hit(s)" for n, i in res["searches"].items()))
    out = [head]
    for r in res["groups"]:
        hits = ", ".join(f"{n} {c}" for n, c in r["hits"].items())
        out.append(f"  {r['units']:>6}  {r['group']}  ({hits})")
        for s in r["sites"]:
            out.append(f"          {s}")
        if r["sites_total"] > len(r["sites"]):
            out.append(f"          ... {r['sites_total'] - len(r['sites'])} more hit(s)")
    if res["truncated"]:
        out.append(f"  ... {res['groups_total'] - len(res['groups'])} more group(s) (--max-groups)")
    if res.get("note"):
        out.append(f"note: {res['note']}")
    return "\n".join(out) + "\n"
