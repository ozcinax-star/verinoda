"""Adversarial check: try to break a claim before anyone relies on it.

Checks (each yields pass / warn / fail with a concrete detail):

* support        - is there supporting evidence at all, and is any of it verifying?
* entailment     - does the support state the claim at the level its status needs
                   (:mod:`verinoda.entail` grades; irrelevant support fails)?
* source_recheck - do cited source lines still match (relocated through their
                   anchor when the code only moved)? Blank cited lines fail.
* existence      - do cited commits and experiment runs exist?
* graph_only     - is the support only graph edges (EXTRACTED/INFERRED)?
* call_site      - for relation claims, is there an AST call to the target at the
                   cited line, inside the claimed caller (import aliases count)?
                   When the line shows none, the caller's whole body is read: no
                   direct call there refutes within that stated scope, a call at
                   another line is only a warning (the citation is off).
* ambiguity      - could an INFERRED call (a relation, or any INFERRED hop of a
                   flow) resolve to another symbol with the same name?
* exclusivity    - for "only X does Y" claims, does Y appear anywhere else?
* staleness      - did anything the claim depends on change since its snapshot
                   (symbol facets; files for claims without recorded dependencies)?
* probes         - counter-hypotheses from :data:`PROBES` (CoVe/CRITIC-style, no
                   LLM): each answers one verification question from source/AST
                   using only the claim's spec and subjects, never its evidence
                   (among them a written config text's binding, a written
                   definition's existence and the order "A before B in F" over
                   F's whole body).

:func:`check_at_creation` runs the checks that can refute definitively within a
stated scope (call site and caller body, :data:`SCOPE_PROBES`) when ``claim add``
creates a claim, so such a miss is ``contradicted`` at once (docs/DESIGN.md D31).

Every finding that refutes carries a *strength*. A ``definitive`` refutation (an
exhaustive check within a stated scope: the cited line has no call to the
target, the line is outside the caller, the "only" pattern occurs elsewhere, a
Python definition ends elsewhere) attaches refuting evidence and makes the
claim ``contradicted``. A ``heuristic`` one never contradicts: it is a warning
that lowers the claim one step and adds an uncertainty (and qualifying
evidence when there is a location to show).

Otherwise warnings and failures each lower the claim one step below its
*assessed* level (``spec.assessed``, fixed at creation), never below the result
of an earlier critique, so re-running critique on an unchanged claim with the
same findings gives the same status and confidence:

    ceiling    = assessed level (as far as the recorded evidence allows it)
                 + 1 step if any warning + 1 step if any failure
    status     = the weaker of that ceiling and the current status, then what
                 the evidence allows (claims rules)
    confidence = min(current confidence, cap(status) - 0.1/warn - 0.2/fail)

The penalty is stored in ``spec.penalty`` so a later re-verification cannot
raise the confidence back. The status goes through
:class:`verinoda.claims.Claims`, so it still obeys the evidence rules. Status
and confidence only ever go down here.
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from verinoda import anchors, callsite, entail
from verinoda import evidence as evmod
from verinoda.claims import (
    CONFIDENCE_CAP,
    ORDER,
    VERIFIED,
    Claims,
    assess_change,
    best_status,
    claim_files,
    grade_evidence,
    watches_tests,
)
from verinoda.snapshot import sha256_file
from verinoda.store import Store

WARN_PENALTY = 0.1
FAIL_PENALTY = 0.2
EMPTY_HASH = evmod.content_hash("")

# call-site codes (verinoda.entail) -> (result, strength)
_CALL_VERDICT = {
    "call": ("pass", None), "alias_call": ("pass", None), "self_call": ("pass", None),
    "module_call": ("pass", None), "method_unresolved": ("pass", None), "reference": ("pass", None),
    "rebound": ("warn", "heuristic"), "other_module": ("warn", "heuristic"), "module_assign": ("warn", "heuristic"),
    "unbound": ("warn", "heuristic"), "not_called": ("warn", "heuristic"), "no_grammar": ("warn", "heuristic"),
    "outside_caller": ("fail", "definitive"), "string_only": ("fail", "definitive"), "absent": ("fail", "definitive"),
    "wrong_module": ("fail", "definitive"), "unreadable": ("fail", None), "blank": ("fail", None),
}


def _name_token(label: str) -> str:
    return callsite.target_token(label)


def _weaker(a: str, b: str) -> str:
    """The weaker of two ORDER statuses."""
    return a if ORDER.index(a) >= ORDER.index(b) else b


def _claim_file_hashes(repo: Path, files: set[str]) -> dict[str, str]:
    """sha256 of just these files now (missing files are absent), as snapshots record them."""
    out: dict[str, str] = {}
    for f in files:
        try:
            out[f] = sha256_file(repo / f)
        except OSError:
            continue
    return out


def _same_refutation(existing: list[dict], ev: dict) -> bool:
    return any(e["relation"] == "refutes" and e.get("path") == ev.get("path")
               and e.get("line_start") == ev.get("line_start") and e.get("line_end") == ev.get("line_end")
               and e.get("content_hash") == ev.get("content_hash") for e in existing)


# =============================================================================
# counter-hypothesis probes
# =============================================================================

@dataclass
class ProbeContext:
    """What a probe may look at: the claim's statement, never its evidence (CoVe factored checks)."""
    repo: Path
    kind: str
    spec: dict
    subjects: list[str]
    text: str
    graph: object | None = None


@dataclass
class ProbeResult:
    probe: str
    hypothesis: str
    result: str  # refutes | qualifies | supports | n/a
    strength: str  # definitive | heuristic
    detail: str
    at: str | None = None  # path:line to cite
    extra: dict = field(default_factory=dict)


def _py_defs(repo: Path, files: set[str]) -> dict[str, list[tuple[str, ast.AST]]]:
    """name -> [(file, def node)] over these Python files."""
    out: dict[str, list[tuple[str, ast.AST]]] = {}
    for f in sorted(files):
        if not f.endswith(".py"):
            continue
        try:
            tree = anchors.parse_python((repo / f).read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, ValueError):
            continue
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.setdefault(n.name, []).append((f, n))
    return out


def _called(node: ast.AST) -> list[tuple[int, str]]:
    calls = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if name:
                calls.append((n.lineno, name))
    return sorted(calls)


def _reaches(defs: dict, start: str, target: str, seen: set[str] | None = None, depth: int = 6) -> bool:
    seen = seen if seen is not None else set()
    if start == target:
        return True
    if start in seen or start not in defs or depth <= 0:
        return False
    seen.add(start)
    return any(_reaches(defs, c, target, seen, depth - 1) for _, c in _called(defs[start][0][1]))


def _raises(defs: dict, fname: str, exc: str, depth: int = 2) -> tuple[str, int] | None:
    """(file, line) of a ``raise exc(...)`` in ``fname`` or a function it calls (``depth`` levels)."""
    if fname not in defs or depth < 0:
        return None
    f, node = defs[fname][0]
    for n in ast.walk(node):
        if isinstance(n, ast.Raise) and n.exc is not None:
            e = n.exc.func if isinstance(n.exc, ast.Call) else n.exc
            name = e.id if isinstance(e, ast.Name) else e.attr if isinstance(e, ast.Attribute) else None
            if name == exc:
                return f, n.lineno
    if depth:
        for _, c in _called(node):
            hit = _raises(defs, c, exc, depth - 1)
            if hit:
                return hit
    return None


def probe_tests_early_exit(ctx: ProbeContext) -> list[ProbeResult]:
    """T1: a test that calls the chain inside ``pytest.raises(E)``, where a callee before the step
    toward the target raises ``E``, probably never reaches the target (heuristic)."""
    tests = [str(t) for t in ctx.spec.get("tests") or [] if "::" in str(t)]
    if not tests or not ctx.subjects:
        return []
    target = entail._token(str(ctx.subjects[0]).partition("::")[2] or "")
    if not target:
        return []
    test_files = sorted({t.split("::")[0] for t in tests})
    files = entail._import_closure(ctx.repo, test_files)
    defs = _py_defs(ctx.repo, files)
    out: list[ProbeResult] = []
    for tid in tests:
        tfile, *_, tname = tid.split("::")
        cands = [(f, n) for f, n in defs.get(tname, []) if f == tfile]
        if not cands:
            continue
        _, tnode = cands[0]
        for w in ast.walk(tnode):
            if not isinstance(w, (ast.With, ast.AsyncWith)):
                continue
            for item in w.items:
                ce = item.context_expr
                if not (isinstance(ce, ast.Call) and isinstance(ce.func, ast.Attribute) and ce.func.attr == "raises"
                        and ce.args):
                    continue
                exc_node = ce.args[0]
                exc = exc_node.id if isinstance(exc_node, ast.Name) else \
                    exc_node.attr if isinstance(exc_node, ast.Attribute) else None
                if not exc:
                    continue
                for _, entry in _called(ast.Module(body=w.body, type_ignores=[])):
                    if entry not in defs or not _reaches(defs, entry, target):
                        continue
                    ef, enode = defs[entry][0]
                    calls = _called(enode)
                    toward = next((ln for ln, c in calls if c != entry and _reaches(defs, c, target)), None)
                    if toward is None:
                        continue
                    for ln, c in calls:
                        if ln >= toward:
                            break
                        hit = _raises(defs, c, exc)
                        if hit:
                            out.append(ProbeResult(
                                "tests_early_exit",
                                f"{tname} stops at {exc} before reaching {target}",
                                "qualifies", "heuristic",
                                f"{tname} expects {exc} (pytest.raises at {tfile}:{w.lineno}); {entry} calls {c} at "
                                f"{ef}:{ln}, which raises {exc} at {hit[0]}:{hit[1]}, before the call toward "
                                f"{target} at {ef}:{toward} - '{tname} reaches {target}' is doubtful",
                                at=f"{hit[0]}:{hit[1]}", extra={"test": tid}))
                            break
    return out


def probe_location_span(ctx: ProbeContext) -> list[ProbeResult]:
    """L1: the cited span must be the definition's span (definitive for Python ASTs)."""
    m = re.search(r"`([^`]+)` is defined at ((?:[\w.-]+/)*[\w.-]+):(\d+)-(\d+)", ctx.text or "")
    if not m:
        return []
    name, path, a, b = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
    facts = anchors.facts_for(None, ctx.repo, path)
    if not anchors.usable(facts):
        return []
    if facts.get("lang") == "markdown":
        secs = [s for s in facts.get("sections", {}).values()
                if s["start"] == a and entail.norm_title(s["title"]) == entail.norm_title(name)]
        if secs and secs[0]["end"] != b:
            s = secs[0]
            return [ProbeResult("location_span", f"`{name}` ends at {s['end']}", "refutes", "heuristic",
                                f"section `{s['title']}` spans {s['start']}-{s['end']} (to the next heading), "
                                f"not {a}-{b}", at=f"{path}:{s['start']}-{s['end']}")]
        return []
    strength = "definitive" if facts.get("lang") == "python" else "heuristic"
    named = anchors.symbols_named(facts, name)
    cands = [s for _, s in named if a in (s["start"], s["def"])]
    if cands and cands[0]["end"] != b:
        s = cands[0]
        return [ProbeResult("location_span", f"`{name}` ends at {s['end']}", "refutes", strength,
                            f"`{name}` spans {s['start']}-{s['end']} in the syntax tree, not {a}-{b}",
                            at=f"{path}:{s['start']}-{s['end']}")]
    if not cands and len(named) == 1:
        s = named[0][1]
        return [ProbeResult("location_span", f"`{name}` starts at {s['start']}", "refutes", strength,
                            f"`{name}` is defined at {s['start']}-{s['end']} in the syntax tree, not {a}-{b}",
                            at=f"{path}:{s['start']}-{s['end']}")]
    return []


def probe_config_read(ctx: ProbeContext) -> list[ProbeResult]:
    """C0: the cited line must read the claimed variable (definitive for Python ASTs)."""
    var = ctx.spec.get("env")
    m = re.search(r"\(((?:[\w.-]+/)*[\w.-]+\.py):(\d+)\)", ctx.text or "")
    if not var or not m:
        return []
    path, line = m.group(1), int(m.group(2))
    try:
        tree = anchors.parse_python((ctx.repo / path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return []
    reads = []
    for node in ast.walk(tree):
        if getattr(node, "lineno", None) != line:
            continue
        for sub in ast.walk(node):
            for other in {n.value for n in ast.walk(sub) if isinstance(n, ast.Constant) and isinstance(n.value, str)}:
                if entail._is_env_read(sub, other):
                    reads.append(other)
    reads = sorted(set(reads))
    if var in reads:
        return []
    detail = (f"{path}:{line} reads {', '.join(reads)}, not {var}" if reads
              else f"{path}:{line} has no environment read")
    return [ProbeResult("config_read", f"{var} is not read at {path}:{line}", "refutes", "definitive", detail,
                        at=f"{path}:{line}")]


def _subject_py_files(ctx: ProbeContext) -> list[tuple[str, str]]:
    out = []
    for s in ctx.subjects:
        p = str(s).partition("::")[0]
        if not p.endswith((".py", ".pyi")) or any(p == q for q, _ in out):
            continue
        try:
            out.append((p, (ctx.repo / p).read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return out


def probe_location_exists(ctx: ProbeContext) -> list[ProbeResult]:
    """L0: written text that places a name in a file ("`place_orders` is defined in service.py").

    A name that nothing in the Python file binds (def, class, assignment, import, parameter, ...) and
    that the file does not spell at all refutes it (definitive within the file's text). A name the
    file spells without a binding the tree shows is only a heuristic doubt; a bound name passes. A
    ``path::`` prefix on the symbol names the file."""
    sym = str(ctx.spec.get("symbol") or "").strip().strip("`")
    if not sym or not ctx.spec.get("free_text"):
        return []
    want, _, sym = sym.rpartition("::")
    want = want.replace("\\", "/").strip("/")
    bare = sym.split("(")[0].rpartition(".")[2].strip()
    out = []
    for p, text in _subject_py_files(ctx):
        if want and not (p == want or p.endswith("/" + want)):
            continue
        facts = anchors.facts_for(None, ctx.repo, p)
        if not bare or not anchors.usable(facts) or facts.get("lang") != "python" or \
                anchors.symbols_named(facts, sym):
            continue
        tree = entail._py_tree(text)
        if tree is None or entail.binds_name(tree, bare):
            continue
        whole = f"{p}:1-{max(1, len(text.splitlines()))}"
        if re.search(rf"(?<![\w]){re.escape(bare)}(?![\w])", text):
            out.append(ProbeResult("location_exists", f"`{sym}` is not bound in {p}", "refutes", "heuristic",
                                   f"{p} spells `{bare}` but nothing in its syntax tree binds it (a def, class, "
                                   "assignment or import)", at=whole))
            continue
        names = sorted({q.split("#")[0].rpartition(".")[2] for q in facts.get("symbols", {})})
        near = difflib.get_close_matches(bare, names, n=2, cutoff=0.8)
        out.append(ProbeResult("location_exists", f"`{sym}` is not defined in {p}", "refutes", "definitive",
                               f"no definition, assignment or import named `{sym}` in {p}, and the file does not "
                               f"spell `{bare}` (scope: the file's text)"
                               + (f"; nearest: {', '.join(near)}" if near else ""), at=whole))
    return out


def probe_config_binding(ctx: ProbeContext) -> list[ProbeResult]:
    """C1: written text about a setting ("the discount threshold is read from X") is about the name the
    read of X is bound to. Another read in the same file whose binding spells the text's whole subject,
    while no read of X is bound to any of it, refutes the text (definitive within that file's
    environment reads)."""
    var = ctx.spec.get("env")
    if not var or not ctx.spec.get("free_text"):
        return []
    out = []
    for p, text in _subject_py_files(ctx):
        res = entail.binding_check(p, text, var, ctx.text)
        if res is None or res["ok"] or res["alt"] is None:
            continue
        alt = res["alt"]
        out.append(ProbeResult("config_binding", f"the text is about {alt['names'][0]}, which reads {alt['var']}",
                               "refutes", "definitive", res["why"], at=f"{p}:{alt['line']}"))
    return out


def probe_order(ctx: ProbeContext) -> list[ProbeResult]:
    """O1: "A before B in F" against the first calls of A and B in F's whole body. A missing call is
    definitive (within F; calls through other names are not followed); a reversed order is definitive
    when F has no branches or loops and no nested function or lambda calls A or B, else heuristic; a
    call made only inside a nested function or lambda proves no order."""
    if "holds" not in ctx.spec:
        return []
    m = entail._ORDER_PROP.match(str(ctx.spec.get("proposition") or ""))
    if not m:
        return []
    a, b, where = m.groups()
    first, second = (a, b) if ctx.spec.get("holds") else (b, a)
    for p, text in _subject_py_files(ctx):
        info = entail.call_order(text, p, where, first, second)
        if info is None:
            continue
        scope = f"{info['name']} ({p}:{info['start']}-{info['end']})"
        lines, nested = info["lines"], info.get("nested") or {}
        missing = [x for x in (first, second) if x not in lines and x not in nested]
        if missing:
            return [ProbeResult("order", f"{scope} does not call {', '.join(missing)}", "refutes", "definitive",
                                f"no direct call to {', '.join(missing)} in {scope}; calls through other names are "
                                "not followed", at=f"{p}:{info['start']}-{info['end']}")]
        if first in lines and second in lines and lines[first] > lines[second]:
            lo, hi = lines[second], lines[first]
            doubt = "; branches or loops may change the order at runtime" if info["branchy"] else ""
            if nested:
                doubt += "; a nested function or lambda also calls one of them, and runs when it is called"
            return [ProbeResult("order", f"{second} comes before {first} in {info['name']}", "refutes",
                                "heuristic" if doubt else "definitive",
                                f"in {scope}, `{second}` is first called at line {lo}, before `{first}` at line {hi}"
                                + doubt, at=f"{p}:{lo}-{hi}")]
        return []
    return []


def probe_decision_status(ctx: ProbeContext) -> list[ProbeResult]:
    """D1: a decision record whose status says superseded/deprecated/rejected (heuristic)."""
    out = []
    for s in ctx.subjects:
        p = str(s).partition("::")[0]
        try:
            head = (ctx.repo / p).read_text(encoding="utf-8", errors="replace").splitlines()[:40]
        except OSError:
            continue
        for i, ln in enumerate(head, 1):
            mm = re.match(r"\s*(?:\*\*)?status(?:\*\*)?\s*[:=-]\s*(\w+)", ln, re.I)
            if mm and mm.group(1).lower() in ("superseded", "deprecated", "rejected", "obsolete", "withdrawn"):
                out.append(ProbeResult("decision_status", f"the record is {mm.group(1).lower()}", "qualifies",
                                       "heuristic", f"{p}:{i} says status {mm.group(1)}: the decision may no "
                                                    "longer apply", at=f"{p}:{i}"))
    return out


# kind -> probes, run in cost order (AST first). Each takes a ProbeContext.
PROBES: dict[str, list[Callable[[ProbeContext], list[ProbeResult]]]] = {
    "tests": [probe_tests_early_exit],
    "location": [probe_location_span, probe_location_exists],
    "config": [probe_config_read, probe_config_binding],
    "decision": [probe_decision_status],
    "behaviour": [probe_order],
}
# Probes that can refute definitively within a stated scope: run when a claim is created too
# (:func:`check_at_creation`), not only when it is challenged.
SCOPE_PROBES: dict[str, list[Callable[[ProbeContext], list[ProbeResult]]]] = {
    "location": [probe_location_exists],
    "config": [probe_config_read, probe_config_binding],
    "behaviour": [probe_order],
}


# =============================================================================
# the call site of a relation, and the caller's whole body
# =============================================================================

# call-site codes after which the caller's whole body is read: the line shows no call, but the
# caller may still make it at another line (then the citation is off, the relation is not refuted)
_SCOPE_CODES = {"absent", "outside_caller", "string_only"}


def _caller_files(graph, caller: str) -> list[str]:
    """Files that define a symbol named like ``caller`` (from the graph; none without one)."""
    if graph is None:
        return []
    tok = _name_token(caller)
    return sorted({graph.file(n) for n in graph.G.nodes if graph.file(n) and _name_token(graph.label(n)) == tok})


def call_site_check(repo: Path, c: dict, *, graph=None) -> dict:
    """Does the cited line of relation claim ``c`` call its target inside the claimed caller?

    ``{"result": pass|warn|fail, "strength": definitive|heuristic|None, "detail", "evidence", "note"}``.
    A line that shows no call (absent, outside the caller, only in a string) is checked against the
    caller's whole body (:func:`verinoda.entail.caller_scope`): no direct call there refutes the claim
    within that stated scope; a call at another line makes the finding a heuristic warning (the
    citation is off, the relation holds). ``evidence`` is the refuting evidence to attach.
    """
    spec = c.get("spec") or {}
    at = spec["at"]
    path, _, line = at.rpartition(":")
    label = spec.get("target_label", "")
    target = _name_token(label)
    subjects = [str(s) for s in c.get("subjects") or []]
    caller = entail.claimed_caller(spec, subjects, c.get("text"))
    b_path = subjects[1].partition("::")[0] if len(subjects) > 1 and "::" in subjects[1] else None
    b_sym = subjects[1].partition("::")[2] if len(subjects) > 1 and "::" in subjects[1] else None
    g = entail.call_site(repo, path, int(line), label, caller=caller, target_path=b_path,
                         target_qual=b_sym or label, relation=spec.get("relation"))
    result, strength = _CALL_VERDICT.get(g.code, ("warn", "heuristic"))
    _, matched, text = callsite.check(repo, at, label)
    out = {"result": result, "strength": strength, "evidence": None, "note": None, "code": g.code}
    if g.code == "unreadable":
        return {**out, "result": "fail", "strength": None, "detail": f"cited line {at} does not exist"}
    if result == "pass":
        return {**out, "detail": f"{at} names '{target}'"
                + (f" through the import alias '{matched}'" if matched and matched != target else "")
                + f" ({g.reason})"}
    if strength != "definitive":
        return {**out, "result": "warn", "strength": "heuristic", "detail": f"{at}: {g.reason}"}
    detail = (f"{at} does not mention '{target}': {(text or '').strip()[:100]}"
              if g.code == "absent" else f"{at}: {g.reason}")
    # a caller the text names by a plain word ("checkout calls submit"): its body is read too. Only when
    # the word names the definition around the cited line is it that caller (its body can refute the
    # claim); another plain word never drives a contradiction (docs/DESIGN.md D31)
    word, word_is_def = None, False
    if caller is None and spec.get("free_text"):
        parsed = entail.relation_parse(c.get("text") or "", spec.get("target_label"))
        word = parsed["caller_word"] if parsed and not parsed["reversed"] else None
        word_is_def = bool(word) and entail.plain_caller_encloses(repo, path, int(line), word)
    who = caller or word
    scope = None
    if g.code in _SCOPE_CODES and who and (spec.get("relation") or "calls").lower() in ("calls", "call"):
        try:
            scope = entail.caller_scope(repo, who, target, path=path,
                                        files=[] if word_is_def else _caller_files(graph, who))
        except (OSError, ValueError, RecursionError):
            scope = None
    if scope is not None and scope["calls"]:
        f, ln = scope["calls"][0]
        return {**out, "result": "warn", "strength": "heuristic",
                "detail": f"{detail}; but {who.strip().rstrip('()')} calls {target} at {f}:{ln} (the cited line "
                          "is not the call site)"}
    if word and g.code in _SCOPE_CODES and not (word_is_def and scope is not None):
        return {**out, "result": "warn", "strength": "heuristic",
                "detail": f"{detail}; the text's caller `{word}` is a plain word that is not a definition around "
                          "the cited line" + (f" ({scope['miss']})" if scope else "")}
    if scope is None and g.code in _SCOPE_CODES and spec.get("free_text") and \
            (caller is None or entail.caller_from_text(spec, subjects)):
        # written text whose caller is unknown, or read from the text but its definition was not
        # found: the caller's whole body was not read, so the line alone refutes nothing
        return {**out, "result": "warn", "strength": "heuristic",
                "detail": f"{detail}; " + (f"no definition of {caller.strip().rstrip('()')} was found to read its "
                                           "whole body" if caller else "the text states no caller whose whole "
                                                                        "body could be read")}
    ev = None
    if scope is not None:  # the caller's whole body is the counterexample
        f, _name, a, b = scope["defs"][0]
        detail = f"{detail}; {scope['miss']}"
        ev = evmod.source_evidence(repo, f, a, b, commit=c.get("commit_sha"),
                                   meta={"check": "call_scope", "expected": target, "strength": "definitive",
                                         "code": g.code, "scope": scope["scope"]})
        note = scope["miss"][:200]
    if ev is None:
        ev = evmod.source_evidence(repo, path, int(line), commit=c.get("commit_sha"),
                                   meta={"check": "call_site", "expected": target, "strength": "definitive",
                                         "code": g.code})
        note = f"cited line does not show a call to '{target}' ({g.code})"
    return {**out, "detail": detail, "evidence": ev, "note": note}


# =============================================================================
# challenge
# =============================================================================

def _existence(store: Store, repo: Path, e: dict) -> str | None:
    """Why a cited commit or run does not exist (None when it does or cannot be checked)."""
    typ = evmod.effective_type(e)
    meta = e.get("meta") or {}
    if typ == "git_history" and e.get("commit_sha"):
        from verinoda.snapshot import git

        if git(repo, "rev-parse", "--git-dir") is None:
            return None
        if git(repo, "cat-file", "-e", f"{e['commit_sha']}^{{commit}}") is None:
            return f"commit {e['commit_sha'][:12]} does not exist in this repository"
    if typ in ("experiment", "test_result") and meta.get("experiment_id"):
        if store.get("experiments", meta["experiment_id"]) is None:
            return f"experiment {meta['experiment_id']} is not recorded"
    return None


def challenge(store: Store, repo: Path, cid: str, *, graph=None, actor: str = "critique",
              current_files: dict[str, str] | None = None) -> dict:
    """Try to break claim ``cid``; see the module docstring.

    ``current_files`` (path -> sha256 of the working tree, e.g. computed once
    per analysis) replaces hashing; without it only the files behind this
    claim are hashed.
    """
    repo = Path(repo).resolve()
    cl = Claims(store, repo)
    c = cl.get(cid)
    before = {"status": c["status"], "confidence": c["confidence"]}
    evs = cl.evidence(cid)
    sup = [e for e in evs if e["relation"] == "supports"]
    spec = c.get("spec") or {}
    findings: list[dict] = []
    alternatives: list[str] = []
    refute: list[tuple[dict, str, str]] = []  # (evidence, note, check)
    heuristic: list[tuple[str, dict | None, str]] = []  # (uncertainty, qualifying evidence, check)

    def add(check: str, result: str, detail: str, **kw):
        findings.append({"check": check, "result": result, "detail": detail, **kw})

    # support
    verifying = [e for e in sup if evmod.is_verifying(e)]
    if not sup:
        add("support", "fail", "no supporting evidence recorded")
    elif not verifying:
        add("support", "warn", "support is only non-verifying evidence: "
            + ", ".join(sorted({evmod.effective_type(e) for e in sup})))
    else:
        add("support", "pass", f"{len(verifying)} verifying supporting evidence")

    # source recheck (anchored relocation) + blank citations
    checks: dict[str, bool] = {}
    for e in evs:
        if e["source_type"] in (*evmod.FILE_TYPES, "static_resolution") and e.get("path") and e.get("line_start"):
            chk = evmod.check_source(repo, e)
            checks[e["id"]] = chk.ok
            if chk.status == "moved":
                try:
                    evmod.record_location(store, e, chk)
                except Exception:
                    pass
            add("source_recheck", "pass" if chk.ok else "fail", f"{e['locator']}: {chk.reason}", evidence=e["id"])
            if e.get("content_hash") == EMPTY_HASH:
                add("source_recheck", "fail", f"{e['locator']}: the cited text is empty", evidence=e["id"])

    # entailment: is the support about the claim, at the level the status needs?
    live = [e for e in sup if checks.get(e["id"], True)]
    if live:  # support that no longer matches is the source_recheck finding, not a second one
        grades = grade_evidence(c, evs, repo)
        best = max((grades.get(e["id"], "none") for e in live), key=entail.GRADES.index, default="none")
        need = "full" if c["status"] in VERIFIED else ("partial" if c["status"] == "strong_inference" else None)
        if c["status"] == "primary_source_verified" and c["kind"] in entail.DOCUMENTARY:
            need = "partial"
        irrelevant = [e for e in sup if grades.get(e["id"]) == "none" and evmod.effective_type(e)
                      not in evmod.NOT_SUPPORT]
        if need is None:
            add("entailment", "pass", f"best support grade: {best}")
        elif not entail.at_least(best, need):
            add("entailment", "fail", f"status {c['status']} needs support graded {need}; best is {best}")
        else:
            add("entailment", "pass", f"support graded {best}"
                + (f"; {len(irrelevant)} irrelevant evidence ignored" if irrelevant else ""))
        for e in irrelevant[:3]:
            g = entail.assess(c["kind"], repo, spec, e, text=c["text"], subjects=c.get("subjects") or [])
            findings.append({"check": "entailment", "result": "info", "detail": f"{e['locator']}: {g.reason}",
                             "evidence": e["id"]})
        if c["kind"] not in entail.DOCUMENTARY and c["kind"] not in ("relation", "location", "config", "flow",
                                                                      "exclusive", "tests", "test_run"):
            for e in live:
                for why in entail.text_conflicts(repo, c["text"], e):
                    heuristic.append((f"{e['locator']}: {why}", None, "entailment"))
                    add("entailment", "warn", f"{e['locator']}: {why}", strength="heuristic")

    # existence of cited commits and runs
    for e in evs:
        why = _existence(store, repo, e)
        if why:
            add("existence", "fail", why, evidence=e["id"])

    # graph only
    if sup and all(e["source_type"] == "graph_edge" for e in sup):
        confs = {(e.get("meta") or {}).get("confidence") for e in sup}
        add("graph_only", "warn", f"support is only graph edges ({', '.join(sorted(map(str, confs)))}); "
            "a graph edge is an extraction, not a verification")

    # call site: an AST call to the target at the cited line, inside the claimed caller (and, when the
    # line shows none, in the caller's whole body)
    if c["kind"] == "relation" and spec.get("at"):
        cs = call_site_check(repo, c, graph=graph)
        if cs["result"] == "pass":
            add("call_site", "pass", cs["detail"])
        elif cs["strength"] == "definitive":
            if cs["evidence"]:
                refute.append((cs["evidence"], cs["note"], "call_site"))
            add("call_site", "fail", cs["detail"], strength="definitive")
        elif cs["strength"] == "heuristic":
            heuristic.append((f"call site {cs['detail']}", None, "call_site"))
            add("call_site", "warn", cs["detail"], strength="heuristic")
        else:
            add("call_site", "fail", cs["detail"])

    # ambiguity for inferred relations and every INFERRED hop of a flow
    if graph is not None:
        pairs: list[tuple[str, str | None]] = []
        # a call whose class the file's imports bind to the target is not ambiguous (spec "resolved")
        if c["kind"] == "relation" and spec.get("confidence") != "EXTRACTED" and not spec.get("resolved"):
            node = spec.get("target")
            subjects = [str(s) for s in c.get("subjects") or []]
            if node is None and len(subjects) > 1 and "::" in subjects[1]:
                # no graph id recorded: the claimed target is the one in the subject's file
                tfile = subjects[1].partition("::")[0]
                tok = _name_token(spec.get("target_label", ""))
                mine = [n for n in graph.G.nodes if graph.file(n) == tfile and _name_token(graph.label(n)) == tok]
                node = mine[0] if len(mine) == 1 else None
            pairs.append((spec.get("target_label", ""), node))
        if c["kind"] == "flow":
            pairs += [(h.get("to", ""), None) for h in spec.get("hops") or [] if h.get("confidence") != "EXTRACTED"]
        for label, node in pairs:
            target = _name_token(label)
            if not target:
                continue
            same = [n for n in graph.G.nodes if _name_token(graph.label(n)) == target and n != node
                    and graph.file(n)]
            if c["kind"] == "flow":
                same = same[1:] if len(same) > 1 else []  # one of them is the hop's own target
            if same:
                alts = [f"{graph.label(n)} at {graph.file(n)}:{graph.line(n)}" for n in same[:5]]
                alternatives += alts
                add("ambiguity", "warn", f"'{target}' also names {len(same)} other symbol(s): " + "; ".join(alts))
            else:
                add("ambiguity", "pass", f"'{target}' is unique in the graph")

    # exclusivity: the guard engine (docs/DESIGN.md D33) - the pattern must match code, not a comment or
    # a string, and a pattern naming a dotted call also catches the calls made through import aliases
    if c["kind"] == "exclusive" and (spec.get("pattern") or spec.get("calls")):
        from verinoda import guards

        allowed = set(spec.get("allowed_files", []))
        found, method = guards.exclusive_hits(repo, spec)
        hits = [(rel, i) for rel, i, _ in found]
        if hits:
            for rel, i in hits[:5]:
                ev = evmod.source_evidence(repo, rel, i, commit=c["commit_sha"],
                                           meta={"check": "exclusivity", "strength": "definitive"})
                if ev:
                    refute.append((ev, "pattern found outside the allowed files", "exclusivity"))
            add("exclusivity", "fail", f"pattern also matches {len(hits)} line(s) outside {sorted(allowed)}: "
                + ", ".join(f"{r}:{i}" for r, i in hits[:5]) + f" ({method})", strength="definitive")
        else:
            add("exclusivity", "pass", f"pattern only occurs in {sorted(allowed)} ({method}; getattr with a "
                                       "computed name, importlib and exec are not followed)")

    # counter-hypothesis probes (spec and subjects only)
    pctx = ProbeContext(repo=repo, kind=c["kind"], spec=spec, subjects=[str(s) for s in c.get("subjects") or []],
                        text=c["text"], graph=graph)
    for probe in PROBES.get(c["kind"], []):
        try:
            results = probe(pctx)
        except (OSError, SyntaxError, ValueError, RecursionError) as exc:
            add("probe", "info", f"{probe.__name__} could not run: {type(exc).__name__}")
            continue
        for r in results:
            ev = None
            if r.at:
                p, _, rng = r.at.rpartition(":")
                a, _, b = rng.partition("-")
                if a.isdigit():
                    ev = evmod.source_evidence(repo, p, int(a), int(b) if b.isdigit() else None,
                                               commit=c["commit_sha"],
                                               meta={"check": r.probe, "strength": r.strength})
            if r.result == "refutes" and r.strength == "definitive" and ev:
                refute.append((ev, r.detail[:200], r.probe))
                add(r.probe, "fail", r.detail, strength="definitive")
            elif r.result in ("refutes", "qualifies"):
                heuristic.append((f"{r.probe}: {r.detail}", ev, r.probe))
                add(r.probe, "warn", r.detail, strength="heuristic")

    # staleness: anything this claim depends on (hash only its own files unless given the tree)
    if c["snapshot_id"]:
        old = store.snapshot_files(c["snapshot_id"])
        deps = store.claim_deps(cid)
        dep_paths = {d["dep_key"].partition(":")[2].partition("::")[0] for d in deps
                     if d["dep_key"].split(":", 1)[0] in ("sym", "bind", "mod", "sec", "file")}
        tests_watched = watches_tests(c) or any(d["dep_key"] == "testset" for d in deps)
        if current_files is not None:
            rels = set(current_files) | set(old)
            cur = dict(current_files)
        else:
            rels = {f for f in claim_files(store, c) | dep_paths if f in old or (repo / f).is_file()}
            if tests_watched:  # the set of test files is part of this claim: list them, hash only them
                from verinoda.snapshot import list_files
                from verinoda.testcode import is_test_file

                rels |= {f for f in list_files(repo) if is_test_file(f)} | {f for f in old if is_test_file(f)}
            cur = _claim_file_hashes(repo, rels)
        old_sub = {p: h for p, h in old.items() if p in rels}
        res = assess_change(store, repo, c, old_sub, cur, partial=current_files is None)
        if res["stale"]:
            files = res["changed"] or [{"file": f.get("file"), "change": f.get("change")} for f in res["facets"]]
            add("staleness", "fail", "files changed since the claim's snapshot: "
                + ", ".join(t["file"] if t["change"] == "modified" else f"{t['file']} ({t['change']})"
                            for t in files[:10] if t.get("file"))
                + (f" [{', '.join(sorted({f['dep'] for f in res['facets'] if f.get('dep')})[:4])}]"
                   if any(f.get("dep") for f in res["facets"]) else ""))
        else:
            add("staleness", "pass", "files behind the claim are unchanged"
                if not res["touched"] else "files changed, but nothing this claim depends on")

    decisive = set()
    added = 0
    for ev, note, check in refute:
        decisive.add(check)
        if _same_refutation(evs, ev):
            continue  # recorded by an earlier critique: link the same counterexample once
        cl.attach(cid, ev, "refutes", note=note)
        evs = cl.evidence(cid)
        added += 1
    unc_added = []
    if heuristic and not decisive:
        known = {e["id"] for e in evs}
        for unc, ev, check in heuristic:
            if ev is not None and not any(e.get("path") == ev.get("path") and e.get("line_start") ==
                                          ev.get("line_start") and e["relation"] == "qualifies" for e in evs):
                eid = cl.attach(cid, ev, "qualifies", note=unc[:200])
                known.add(eid)
            unc_added.append(unc[:300])
        cur_unc = list(cl.get(cid).get("uncertainties") or [])
        new_unc = [u for u in unc_added if u not in cur_unc]
        if new_unc:
            store.update_claim(cid, {"uncertainties": cur_unc + new_unc})
        evs = cl.evidence(cid)

    fails = [f for f in findings if f["result"] == "fail"]
    warns = [f for f in findings if f["result"] == "warn"]
    conf_before = c["confidence"] or 0.0
    penalty = WARN_PENALTY * len(warns) + FAIL_PENALTY * len(fails)
    if any(f["check"] == "staleness" for f in fails):
        after = cl.set_status(cid, "stale", reason="critique: source changed", actor=actor,
                              payload={"findings": findings}, confidence=min(conf_before, CONFIDENCE_CAP["stale"]))
    elif decisive:
        # A definitive counterexample (the cited call site has no call to the
        # target; the "only X" pattern occurs elsewhere; the definition spans
        # other lines) refutes the claim regardless of how strong its support looked.
        after = cl.set_status(cid, "contradicted", actor=actor, downgrade=True,
                              reason="critique: counterexample found (" + ", ".join(sorted(decisive)) + ")",
                              payload={"findings": findings},
                              confidence=min(conf_before, CONFIDENCE_CAP["contradicted"]))
    elif c["status"] not in ORDER:
        # stale / contradicted: critique never restores a claim - `verify` does.
        after = c
    elif fails or warns:
        # Step down from the fixed assessed level (as far as the recorded
        # evidence allows it; a failed source re-check is its own finding),
        # never from the result of an earlier critique - so critique run again
        # with the same findings changes nothing.
        cur = cl.get(cid)
        base, _ = best_status(evs, cl.assessed(cur), claim=cur, repo=repo)
        if base not in ORDER:
            base = "unknown"
        steps = (1 if warns else 0) + (1 if fails else 0)
        ceiling = ORDER[min(len(ORDER) - 1, ORDER.index(base) + steps)]
        cl.lower_ceiling(cid, ceiling)
        cl.set_penalty(cid, penalty)
        # One transition: the weaker of that ceiling and the current status
        # (never raised), then what the evidence still allows.
        cur = cl.get(cid)
        status, _ = best_status(evs, _weaker(ceiling, c["status"]), cl._source_checks(evs), claim=cur, repo=repo)
        if status not in ORDER:
            status = _weaker(ceiling, c["status"])
        conf_now = max(0.0, min(conf_before, CONFIDENCE_CAP[status] - penalty))
        after = cl.set_status(cid, status, reason=f"critique: {len(fails)} fail, {len(warns)} warn",
                              actor=actor, confidence=conf_now, downgrade=True, payload={"findings": findings})
    else:
        cl.set_penalty(cid, 0.0)
        after = c  # every check passed: nothing to change
    return {
        "claim": cid, "text": c["text"], "before": before,
        "after": {"status": after["status"], "confidence": round(after["confidence"] or 0.0, 2)},
        "findings": findings, "alternatives": alternatives,
        "refuting_evidence_added": added,
        **({"uncertainties_added": unc_added} if unc_added else {}),
    }


def check_at_creation(store: Store, repo: Path, cid: str, *, graph=None, actor: str = "scope_check") -> dict:
    """The definitive, scope-stated checks of a claim just created (``claim add``).

    A relation's call site and its caller's whole body (:func:`call_site_check`) and the kind's
    :data:`SCOPE_PROBES` (a config text's binding, an order "A before B in F"). A definitive miss makes
    the claim ``contradicted`` now, with the scope in the reason, instead of waiting for a challenge;
    heuristic findings are left to ``challenge``. Returns ``{"claim", "status", "findings"}``.
    """
    repo = Path(repo).resolve()
    cl = Claims(store, repo)
    c = cl.get(cid)
    spec = c.get("spec") or {}
    refute: list[tuple[dict, str, str]] = []
    if c["kind"] == "relation" and spec.get("at"):
        cs = call_site_check(repo, c, graph=graph)
        if cs["strength"] == "definitive" and cs["evidence"]:
            refute.append((cs["evidence"], cs["note"], cs["detail"]))
    pctx = ProbeContext(repo=repo, kind=c["kind"], spec=spec, subjects=[str(s) for s in c.get("subjects") or []],
                        text=c["text"], graph=graph)
    for probe in SCOPE_PROBES.get(c["kind"], []):
        try:
            results = probe(pctx)
        except (OSError, SyntaxError, ValueError, RecursionError):
            continue
        for r in results:
            if r.result != "refutes" or r.strength != "definitive" or not r.at:
                continue
            p, _, rng = r.at.rpartition(":")
            a, _, b = rng.partition("-")
            ev = evmod.source_evidence(repo, p, int(a), int(b) if b.isdigit() else None, commit=c["commit_sha"],
                                       meta={"check": r.probe, "strength": "definitive"}) if a.isdigit() else None
            if ev:
                refute.append((ev, r.detail[:200], r.detail))
    if not refute:
        return {"claim": cid, "status": c["status"], "findings": []}
    evs = cl.evidence(cid)
    for ev, note, _detail in refute:
        if not _same_refutation(evs, ev):
            cl.attach(cid, ev, "refutes", note=note)
    details = list(dict.fromkeys(d for _, _, d in refute))
    after = cl.set_status(cid, "contradicted", actor=actor, downgrade=True,
                          reason="checked at creation: " + "; ".join(details)[:600],
                          payload={"findings": details},
                          confidence=min(c["confidence"] or 0.0, CONFIDENCE_CAP["contradicted"]))
    return {"claim": cid, "status": after["status"], "findings": details}
