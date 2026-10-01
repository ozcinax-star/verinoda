"""Mutation testing scoped to the diff: do the tests that reach a change actually check it?

The changed lines are the working tree's lines that differ from ``base`` (default HEAD) in Python files that are
not tests. On those lines a small set of operators makes one mutant each, written by splicing the source text (the
rest of the file stays byte for byte):

* a comparison swapped (``<`` / ``<=``, ``>`` / ``>=``, ``==`` / ``!=``, ``in`` / ``not in``, ``is`` / ``is not``);
* an arithmetic operator swapped (``+`` / ``-``, ``*`` / ``/``, ``//`` -> ``*``);
* ``and`` / ``or`` swapped;
* ``True`` / ``False`` swapped, an integer constant ``n`` -> ``n + 1``;
* ``not x`` -> ``x``, an ``if`` / ``while`` condition negated;
* ``return x`` -> ``return None``.

A mutant that does not compile is dropped; two mutants with the same text are one. The tests run are the ones the
change review selects (:mod:`verinoda.review`'s ``affected`` block: the test map's observed tests, tests the change
edits, static reach) unless ``tests`` names them. Each run is an experiment (:mod:`verinoda.experiments`): a copy
of ``HEAD`` with the working tree's changed files written on top, and the mutant on top of that, under the same
policy and isolation as any test run (a project that is not trusted runs only in a container).

The tests first run on the unmutated change (the baseline): when they do not pass there, no mutant can be judged
and none is run. A mutant is **killed** when the tests fail with it, **survived** when they pass, **timeout**
when they ran past the limit (counted apart: a mutant that loops is often killed in effect, but nothing failed),
and **not_run** when the run was refused or inconclusive. A mutant whose run stops while pytest collects the
tests (exit 2: the module no longer imports) after the baseline passed is killed too, with that reason.

Each mutant's outcome is observed in its experiment (run-scoped: those tests, that tree). "The tests do not check
this line" is a ``strong_inference`` from a surviving mutant, never verified: the mutant may be equivalent (no
input tells it from the original), and the selection may have left out the test that would kill it.
"""

from __future__ import annotations

import ast
import difflib
import io
import re
import tokenize
from pathlib import Path

from verinoda import experiments, testcode, treestate
from verinoda.store import Store

MAX_MUTANTS = 25          # default number of mutants run
MAX_MUTANTS_CAP = 500
MAX_TESTS = 200           # test ids handed to one pytest command
MAX_FILE_BYTES = 1_000_000
LIMITS = [
    "a surviving mutant may be equivalent to the original (no input tells them apart): it is a lead, not proof",
    "only the selected tests run; a test the selection left out may kill a mutant that survived",
    "Python files only; the operators are a small fixed set on the changed lines (no deleted lines; augmented "
    "assignments, `**`, `%`, unary minus and conditional expressions are not mutated)",
    "each mutant is one full run of the selected tests in a copy of the repository",
]

_CMP = {ast.Lt: ("<", ast.LtE, "<="), ast.LtE: ("<=", ast.Lt, "<"), ast.Gt: (">", ast.GtE, ">="),
        ast.GtE: (">=", ast.Gt, ">"), ast.Eq: ("==", ast.NotEq, "!="), ast.NotEq: ("!=", ast.Eq, "=="),
        ast.In: ("in", ast.NotIn, "not in"), ast.NotIn: ("not in", ast.In, "in"),
        ast.Is: ("is", ast.IsNot, "is not"), ast.IsNot: ("is not", ast.Is, "is")}
_BIN = {ast.Add: ("+", "-"), ast.Sub: ("-", "+"), ast.Mult: ("*", "/"), ast.Div: ("/", "*"),
        ast.FloorDiv: ("//", "*")}
_OP_RX = {"<": r"<(?!=)", "<=": r"<=", ">": r">(?!=)", ">=": r">=", "==": r"==", "!=": r"!=",
          "in": r"(?<![\w.])in\b", "not in": r"\bnot\s+in\b", "is": r"\bis\b(?!\s+not\b)",
          "is not": r"\bis\s+not\b", "+": r"\+", "-": r"-", "*": r"\*(?!\*)", "/": r"/(?!/)", "//": r"//"}


class _Text:
    """Source text with (line, UTF-8 column) -> character offset."""

    def __init__(self, text: str):
        self.text = text
        self.starts = [0]
        for line in text.split("\n"):
            self.starts.append(self.starts[-1] + len(line) + 1)
        self.lines = text.split("\n")

    def off(self, line: int, col: int) -> int:
        raw = self.lines[line - 1].encode("utf-8")[:col]
        return self.starts[line - 1] + len(raw.decode("utf-8", "replace"))

    def start(self, n: ast.AST) -> int:
        return self.off(n.lineno, n.col_offset)

    def end(self, n: ast.AST) -> int:
        return self.off(n.end_lineno, n.end_col_offset)


def _strip_comments(gap: str) -> str:
    """The gap between two operands with ``#`` comments blanked (same length, so offsets hold)."""
    return re.sub(r"#[^\n]*", lambda m: " " * len(m.group(0)), gap)


def _op_span(t: _Text, left: ast.AST, right: ast.AST, op: str) -> tuple[int, int] | None:
    a, b = t.end(left), t.start(right)
    if b <= a:
        return None
    gap = _strip_comments(t.text[a:b])
    hits = list(re.finditer(_OP_RX[op], gap))
    if len(hits) != 1:
        return None
    return a + hits[0].start(), a + hits[0].end()


def changed_lines(old: bytes | None, new: bytes) -> set[int]:
    """1-based lines of ``new`` that are added or replaced relative to ``old`` (every line when ``old`` is None)."""
    b = new.decode("utf-8", "replace").split("\n")
    if old is None:
        return set(range(1, len(b) + 1))
    a = old.decode("utf-8", "replace").split("\n")
    out: set[int] = set()
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            out.update(range(j1 + 1, j2 + 1))
    return out


def _docstring_nodes(tree: ast.AST) -> set[int]:
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body:
            first = n.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                out.add(id(first.value))
    return out


def mutants_for(rel: str, text: str, lines: set[int]) -> list[dict]:
    """The mutants of ``text`` on ``lines``: ``{"path", "line", "op", "was", "now", "start", "end", "text"}``,
    in source order, compiling, with distinct texts. ``start``/``end`` are character offsets of the replaced
    span."""
    bom = text.startswith("\ufeff")
    body = text[1:] if bom else text
    try:
        tree = ast.parse(body)
    except (SyntaxError, ValueError):
        return []
    t = _Text(body)
    skip = _docstring_nodes(tree) | _annotation_nodes(tree)
    spans: list[tuple[int, int, str, str]] = []   # start, end, op, replacement

    def on(n: ast.AST) -> bool:
        return getattr(n, "lineno", None) is not None and \
            any(ln in lines for ln in range(n.lineno, (n.end_lineno or n.lineno) + 1))

    for n in ast.walk(tree):
        if isinstance(n, ast.Compare) and len(n.ops) == 1 and on(n):
            info = _CMP.get(type(n.ops[0]))
            if info:
                sp = _op_span(t, n.left, n.comparators[0], info[0])
                if sp:
                    spans.append((*sp, f"{info[0]} -> {info[2]}", info[2]))
        elif isinstance(n, ast.BinOp) and on(n):
            info = _BIN.get(type(n.op))
            if info:
                sp = _op_span(t, n.left, n.right, info[0])
                if sp:
                    spans.append((*sp, f"{info[0]} -> {info[1]}", info[1]))
        elif isinstance(n, ast.BoolOp) and len(n.values) == 2 and on(n):
            was, now = ("and", "or") if isinstance(n.op, ast.And) else ("or", "and")
            a, b = t.end(n.values[0]), t.start(n.values[1])
            gap = _strip_comments(t.text[a:b])
            hits = list(re.finditer(rf"\b{was}\b", gap))
            if len(hits) == 1:
                spans.append((a + hits[0].start(), a + hits[0].end(), f"{was} -> {now}", now))
        elif isinstance(n, ast.Constant) and on(n) and id(n) not in skip:
            if n.value is True or n.value is False:
                now = "False" if n.value else "True"
                spans.append((t.start(n), t.end(n), f"{n.value} -> {now}", now))
            elif type(n.value) is int and t.text[t.start(n):t.end(n)].isdigit():
                spans.append((t.start(n), t.end(n), f"{n.value} -> {n.value + 1}", str(n.value + 1)))
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not) and on(n):
            inner = t.text[t.start(n.operand):t.end(n.operand)]
            spans.append((t.start(n), t.end(n), "not x -> x", f"({inner})"))
        elif isinstance(n, (ast.If, ast.While)) and on(n.test) and not isinstance(n.test, ast.UnaryOp):
            cond = t.text[t.start(n.test):t.end(n.test)]
            if "\n" not in cond:
                spans.append((t.start(n.test), t.end(n.test), "condition negated", f"not ({cond})"))
        elif isinstance(n, ast.Return) and n.value is not None and on(n) and \
                not (isinstance(n.value, ast.Constant) and n.value.value is None):
            spans.append((t.start(n.value), t.end(n.value), "return x -> return None", "None"))
    out, seen = [], set()
    for start, end, op, rep in sorted(spans):
        line = t.text.count("\n", 0, start) + 1
        if line not in lines:   # the node spans a changed line, but this operator sits on an unchanged one
            continue
        new = t.text[:start] + rep + t.text[end:]
        if new in seen or new == body:
            continue
        try:
            compile(new, rel, "exec", dont_inherit=True)
        except (SyntaxError, ValueError):
            continue
        seen.add(new)
        col = start - t.starts[line - 1] + 1
        out.append({"path": rel, "line": line, "col": col, "op": op, "was": t.text[start:end][:80],
                    "now": rep[:80], "start": start, "end": end, "text": ("\ufeff" if bom else "") + new})
    return out


def _annotation_nodes(tree: ast.AST) -> set[int]:
    """The nodes inside annotations: a mutant there changes nothing that runs (an equivalent mutant)."""
    out: set[int] = set()
    roots = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.returns is not None:
            roots.append(n.returns)
        elif isinstance(n, ast.arg) and n.annotation is not None:
            roots.append(n.annotation)
        elif isinstance(n, ast.AnnAssign):
            roots.append(n.annotation)
    for r in roots:
        out.update(id(x) for x in ast.walk(r))
    return out


def _tokens_ok(text: str) -> bool:
    try:
        for _ in tokenize.generate_tokens(io.StringIO(text).readline):
            pass
    except (tokenize.TokenError, SyntaxError):
        return False
    return True


def _select_tests(store: Store, repo: Path, base: str) -> tuple[list[str], dict]:
    """The tests the change review selects for the change against ``base``, and how they were chosen."""
    from verinoda import review as rv

    res = rv.review(repo, store=store, base=base, record=False, concerns=["health"])
    aff = ((res.get("tests") or {}).get("affected")) or {}
    ids = [r["test"] for r in aff.get("tests") or [] if r["test"].split("::", 1)[0].endswith(".py")]
    return ids, {"by": aff.get("by") or {}, "total": aff.get("total", 0), "basis": aff.get("basis")}


def _argv(repo: Path, tests: list[str]) -> list[str]:
    from verinoda.testmap import runner_id

    ids = list(dict.fromkeys(runner_id(t) for t in tests))
    return [experiments.python_for(repo), "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *ids]


def run(store: Store, repo: Path, *, base: str | None = "HEAD", tests: list[str] | None = None,
        max_mutants: int = MAX_MUTANTS, timeout: float | None = None, files: list[str] | None = None) -> dict:
    """Make the mutants of the change against ``base``, run the selected tests on each and report what survived.
    ``files`` limits the files mutated (repository-relative). Raises ValueError on bad arguments and
    :class:`treestate.NotAGitTree` outside a git work tree."""
    repo = Path(repo).resolve()
    if max_mutants < 0 or max_mutants > MAX_MUTANTS_CAP:
        raise ValueError(f"--max-mutants must be 0..{MAX_MUTANTS_CAP}")
    sha = treestate.resolve_commit(repo, base or "HEAD")
    head = treestate.head_commit(repo)
    if head is None:
        raise ValueError("the repository has no commit to copy")
    ch = treestate.changes_vs_base(repo, sha)
    only = {f.replace("\\", "/").removeprefix("./") for f in files or []}
    res: dict = {"base": sha, "limits": list(LIMITS), "mutants": [], "skipped_files": []}
    eligible = [rel for rel in sorted(ch["tree_files"]) if rel.endswith(".py")
                and not testcode.is_test_or_support_file(rel) and ch["contents"].get(rel) is not None]
    missing = sorted(only - set(eligible))
    if missing:
        raise ValueError(f"not a changed Python file that is not a test: {', '.join(missing)}")
    all_mutants: list[dict] = []
    crlf: set[str] = set()
    for rel in eligible:
        if only and rel not in only:
            continue
        new = ch["contents"][rel]
        if len(new) > MAX_FILE_BYTES:
            res["skipped_files"].append({"path": rel, "why": f"over {MAX_FILE_BYTES} bytes"})
            continue
        try:
            text = new.decode("utf-8")
        except UnicodeDecodeError:
            res["skipped_files"].append({"path": rel, "why": "not UTF-8: a mutant could not keep the rest of the "
                                                             "file byte for byte"})
            continue
        if not _tokens_ok(text.removeprefix("\ufeff")):
            res["skipped_files"].append({"path": rel, "why": "does not tokenize as Python"})
            continue
        try:
            ast.parse(text.removeprefix("\ufeff"))
        except (SyntaxError, ValueError) as exc:
            res["skipped_files"].append({"path": rel, "why": f"does not parse as Python ({exc.__class__.__name__})"})
            continue
        try:
            raw = (repo / rel).read_bytes()
        except OSError:
            raw = new
        if b"\r\n" in raw and raw.count(b"\r\n") == raw.count(b"\n"):
            crlf.add(rel)
        all_mutants += mutants_for(rel, text, changed_lines(ch["base"].get(rel), new))
    res["mutants_total"] = len(all_mutants)
    chosen = all_mutants[:max_mutants]
    if len(all_mutants) > len(chosen):
        res["not_run_over_limit"] = len(all_mutants) - len(chosen)
    if not all_mutants:
        if res["skipped_files"]:
            res.update(status="incomplete", headline=f"no mutant could be made on the Python lines changed against "
                                                     f"{sha[:12]}; {len(res['skipped_files'])} changed file(s) could "
                                                     "not be read (see skipped_files): no pass")
        else:
            res.update(status="nothing_to_mutate",
                       headline=f"no mutant could be made on the Python lines changed against {sha[:12]}")
        return res
    if not chosen:
        res.update(status="incomplete", headline=f"{len(all_mutants)} mutant(s) made, none run (--max-mutants 0)")
        return res
    if tests is None:
        tests, sel = _select_tests(store, repo, sha)
        res["selection"] = sel
    else:
        res["selection"] = {"given": len(tests)}
    tests = [t for t in tests if not t.startswith("-")]
    if not tests:
        res.update(status="no_tests", headline=f"{len(all_mutants)} mutant(s), but no test reaches the change: "
                                               "nothing can kill them (name tests with --tests)",
                   next_step="name the tests that should check the change with --tests, or write one")
        return res
    if len(tests) > MAX_TESTS:
        res["tests_cut"] = len(tests) - MAX_TESTS
        tests = tests[:MAX_TESTS]
    res["tests"] = tests
    argv = _argv(repo, tests)
    # the copy: HEAD's files, the working tree's changed files on top (deleted ones left out)
    now = treestate.changes_vs_base(repo, head)
    replay: dict[str, bytes | None] = {}
    for rel, cid in now["tree_files"].items():
        try:
            replay[rel] = (repo / rel).read_bytes() if cid is not None else None
        except OSError:
            replay[rel] = None

    def one(hyp: str, extra: dict[str, bytes]) -> dict:
        try:
            r = experiments.run(store, repo, argv, hypothesis=hyp, expect="pass", timeout=timeout, ref=head,
                                replay={**replay, **extra})
        except experiments.ExperimentRefused as exc:
            return {"outcome": "refused", "why": str(exc), "next_step": getattr(exc, "next_step", None)}
        return {"outcome": r["outcome"], "experiment": r["id"], "why": r.get("inconclusive_reason"),
                "isolation": r.get("isolation"), "exit_code": r.get("exit_code")}

    baseline = one(f"the selected tests pass on the change against {sha[:12]} (mutation baseline)", {})
    res["baseline"] = baseline
    if baseline["outcome"] != "pass":
        why = baseline.get("why") or baseline["outcome"]
        res.update(status="baseline_failed" if baseline["outcome"] == "fail" else "not_run",
                   headline=f"the selected tests do not pass on the unmutated change ({why}): no mutant can be "
                            "judged, none was run")
        if baseline.get("next_step"):
            res["next_step"] = baseline["next_step"]
        return res
    counts = {"killed": 0, "survived": 0, "timeout": 0, "not_run": 0}
    for m in chosen:
        text = m["text"].replace("\n", "\r\n") if m["path"] in crlf else m["text"]
        r = one(f"the selected tests fail with mutant {m['path']}:{m['line']} ({m['op']})",
                {m["path"]: text.encode("utf-8")})
        verdict = {"fail": "killed", "pass": "survived", "timeout": "timeout"}.get(r["outcome"], "not_run")
        if verdict == "not_run" and r["outcome"] == "inconclusive" and r.get("exit_code") == 2:
            # the baseline collected and passed: a mutant that stops collection broke the module's import
            verdict, r["why"] = "killed", "pytest could not collect the tests with this mutant (exit 2)"
        counts[verdict] += 1
        row = {"at": f"{m['path']}:{m['line']}", "col": m["col"], "op": m["op"], "was": m["was"], "now": m["now"],
               "result": verdict, "experiment": r.get("experiment"),
               "status": "observed" if r.get("experiment") and verdict != "not_run" else "unknown"}
        if verdict == "survived":
            row["claim"] = {"statement": f"the selected tests do not check {m['path']}:{m['line']} ({m['op']} "
                                         "passes them)", "status": "strong_inference",
                            "why": "a surviving mutant; it may be equivalent to the original"}
        if verdict == "not_run" or r.get("why"):
            row["why"] = r.get("why") or r["outcome"]
        res["mutants"].append(row)
    res["counts"] = counts
    judged = counts["killed"] + counts["survived"]
    score = f"{counts['killed']}/{judged} killed" if judged else "none judged"
    res["status"] = "survivors" if counts["survived"] else \
        ("incomplete" if counts["not_run"] or counts["timeout"] or res.get("not_run_over_limit") else "all_killed")
    res["headline"] = (f"{len(chosen)} mutant(s) on lines changed against {sha[:12]}, {len(tests)} test(s): "
                       f"{score}, {counts['survived']} survived"
                       + (f", {counts['timeout']} timed out" if counts["timeout"] else "")
                       + (f", {counts['not_run']} not run" if counts["not_run"] else "")
                       + (f"; {res['not_run_over_limit']} more not run (--max-mutants)"
                          if res.get("not_run_over_limit") else ""))
    return res


def render(res: dict) -> str:
    lines = [res.get("headline", "")]
    for m in res.get("mutants") or []:
        if m["result"] != "killed":
            lines.append(f"  {m['result']:<9} {m['at']}:{m['col']}  {m['op']}  ({m['was']} -> {m['now']})"
                         + (f"  [{m['experiment']}]" if m.get("experiment") else "")
                         + (f"  {m['why']}" if m.get("why") else ""))
    for s in res.get("skipped_files") or []:
        lines.append(f"  skipped {s['path']}: {s['why']}")
    if res.get("next_step"):
        lines.append(f"next: {res['next_step']}")
    if res.get("mutants"):
        lines.append("limits: " + "; ".join(res.get("limits") or []))
    return "\n".join(lines)
