"""``verinoda pin``: pin one Python function's current behaviour as a generated pytest file (opt-in).

For one function (``path.py::name``, ``path.py::Class.method`` or a unique
name):

1. **Eligibility** (syntax tree): Python only; top-level functions, static and
   class methods, and instance methods of a dataclass (``self`` must be
   rebuilt by the class's constructor). Generators, coroutines and test code
   are ``unsupported``, with the reason.
2. **Tests**: the ones given (``tests``), else those the test-to-code map
   (:mod:`verinoda.testmap`) saw run the function and those that reach it
   statically (:func:`verinoda.runtime.trace.select_tests`), at most 50.
3. **Recording run**: those tests through :func:`verinoda.experiments.run` -
   the trust gate, throw-away copy, scrubbed environment and tree-killing
   timeout of every experiment - with the pin plugin
   (:mod:`verinoda.runtime.pin_plugin`), which keeps each call's arguments and
   its return value or exception (type and message) when every value
   round-trips through Python source (literals, Decimal, Fraction, dates,
   enum members, project dataclasses). Other calls are skipped and counted by
   reason; an input seen with two different results is dropped.
4. **Generated file** (default ``tests/pinned/test_pin_<name>.py``; never over
   an existing file without ``force``): one test per input, asserting the
   recorded result with types compared all the way down, or the exception's
   exact type and message. The header names the commit, the copy's tree hash
   and the tests the inputs came from.
5. **Replay**: the generated file runs once in a fresh copy of the working
   tree (the file added on top). Only the tests that pass are kept: one that
   fails on replay is nondeterministic (time, randomness, state the tests set
   up) and is dropped and reported. When some were dropped, the kept file is
   run once more; the file is written only after a run of exactly its content
   passed.
6. **Claim**: "FUNCTION's behaviour on these N inputs is pinned at commit X",
   ``experiment_verified`` through the claim rules, supported by that passing
   run's evidence (a ``test_run`` claim naming the run).

What a pin is (and is not): it records what the function did on the inputs
the tests gave it, at one commit. It is not a specification - a pinned bug
stays pinned - and it says nothing about other inputs.
"""

from __future__ import annotations

import ast
import json
import keyword
from collections import Counter
from pathlib import Path

from verinoda import experiments, testcode, treestate
from verinoda.snapshot import git_info, list_files
from verinoda.store import Store

PLUGIN_MODULE = "verinoda_pin"
RECORD_FILE = "pin.jsonl"
REPLAY_FILE = "pin_replay.jsonl"
DEFAULT_MAX_CASES = 50
MAX_CASES = 500
MAX_TESTS = 50
DEFAULT_DIR = "tests/pinned"
DROP_REPLAY = "failed on replay (nondeterministic: time, randomness, or state the tests set up)"
DROP_INCONSISTENT = "different results for the same input in the recording run"
DROP_OVER_MAX = "over --max-cases"
LIMITS = [
    "a pin records what the function did on the inputs the selected tests gave it at this commit: not a "
    "specification (a pinned bug stays pinned), and nothing about other inputs",
    "only return values and raised exceptions (type and message) are pinned: side effects, the arguments' state "
    "after the call and output are not",
    "values that do not rebuild from Python source (objects other than literals, Decimal, Fraction, dates, enum "
    "members and project dataclasses) are skipped, with the reason counted",
    "every run pins PYTHONHASHSEED=0: an order that depends on string hashing looks stable on replay",
    "one replay run decides what is kept: a result that changes rarely can still pass it",
]


def plugin_source() -> bytes:
    return (Path(__file__).parent / "runtime" / "pin_plugin.py").read_bytes()


def _one_line(text: str) -> str:
    return "".join(c if c.isprintable() else " " for c in str(text))


# -- target -----------------------------------------------------------------------------------------

def _target(repo: Path, symbol: str, files: list[str]) -> tuple[str, str, str | None, str | None, tuple[int, ...]]:
    """(file, qualified name, call kind, why unsupported, the definition's first lines)."""
    from verinoda import probe

    rel, qual = probe.resolve_target(repo, symbol, files)
    if not rel.endswith(".py"):
        return rel, qual, None, "pin runs Python functions only", ()
    if testcode.is_test_or_support_file(rel):
        return rel, qual, None, f"{rel} is test code: pin the production function the tests call", ()
    text = probe._read(repo, rel)
    if text is None:
        raise ValueError(f"{rel} is not a file in the working tree")
    tree = probe._parse(text)
    if tree is None:
        return rel, qual, None, f"{rel} does not parse as Python", ()
    defs = probe._defs(tree)
    node = defs.get(qual)
    if node is None:
        return rel, qual, None, (f"no top-level function or method {qual} in {rel} (nested functions cannot be "
                                 "called by name)"), ()
    if isinstance(node, ast.ClassDef):
        return rel, qual, None, f"{qual} is a class: name one of its functions", ()
    lines = (node.lineno, *(d.lineno for d in node.decorator_list))
    if isinstance(node, ast.AsyncFunctionDef) or probe._is_generator(node):
        return rel, qual, None, f"{qual} is a generator or coroutine: its result is not one value", lines
    kind = probe._call_kind(node, qual)
    if kind == "method":
        cls = defs.get(qual.split(".")[0])
        decos = {ast.unparse(d).split("(")[0].rpartition(".")[2] for d in getattr(cls, "decorator_list", [])}
        if "dataclass" not in decos:
            return rel, qual, kind, (f"{qual} is an instance method of a class that is not a dataclass: self "
                                     "cannot be rebuilt by its constructor"), lines
    return rel, qual, kind, None, lines


def _out_path(qual: str, out: str | None) -> str:
    rel = (out or f"{DEFAULT_DIR}/test_pin_{qual.replace('.', '_')}.py").replace("\\", "/").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    if not rel or rel.startswith("/") or experiments.path_escape(rel) or not treestate.safe_path(rel) \
            or any(ord(c) < 32 for c in rel):
        raise ValueError(f"--out {rel!r} must be a file path inside the repository")
    if not rel.endswith(".py"):
        raise ValueError(f"--out {rel!r} must be a .py file")
    return rel


# -- test selection ---------------------------------------------------------------------------------

def select(store: Store, repo: Path, rel: str, qual: str, def_lines: tuple[int, ...] = (), *,
           exclude: str | None = None) -> tuple[list[str], dict]:
    """Tests that ran the function (the test map) and tests that reach it statically, at most 50.

    ``def_lines``: the lines the definition starts on (``def`` and decorators), to find its graph node."""
    from verinoda import testmap
    from verinoda.runtime import trace

    mapped = [r["test"] for r in store.all("SELECT DISTINCT test FROM test_map WHERE path = ? AND qual = ? "
                                            "ORDER BY test", (rel, qual))]
    ids = list(dict.fromkeys(testmap.runner_id(t) for t in mapped))
    static: list[str] = []
    g = trace._load_graph(repo)
    if g is not None:
        name = qual.rpartition(".")[2]
        named = [n for n in g.symbols_in(rel) if trace._clean_label(g.label(n)).rpartition(".")[2] == name]
        nodes = named if len(named) == 1 else [n for n in named if g.line(n) in def_lines]
        if len(nodes) == 1:
            static = trace.select_tests(g, nodes)
    for t in static:
        if t not in ids:
            ids.append(t)
    ids = [t for t in ids if not (exclude and t.split("::")[0] == exclude)][:MAX_TESTS]
    return ids, {"test_map": len(mapped), "static": len(static), "graph": g is not None}


# -- output of the plugin ---------------------------------------------------------------------------

def parse_output(data: bytes) -> dict:
    out: dict = {"header": None, "tests": {}, "cases": []}
    for raw in data.decode("utf-8", "replace").splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        k = rec.get("k")
        if k == "header":
            out["header"] = rec
        elif k == "test":
            out["tests"][str(rec.get("id"))] = rec.get("phases") or {}
        elif k == "case" and isinstance(rec.get("call"), dict) and isinstance(rec.get("outs"), list):
            out["cases"].append(rec)
    return out


def _passed(phases: dict) -> bool:
    return phases.get("call") == "passed" and phases.get("setup") in (None, "passed") \
        and phases.get("teardown") in (None, "passed")


# -- the generated file -----------------------------------------------------------------------------

SAME_SOURCE = '''

def _pin_same(a, b):
    """Equal values of the same types all the way down (1 == 1.0 == True is not enough); NaN equals NaN."""
    if type(a) is not type(b):
        return False
    if type(a) is float:
        return a == b or (a != a and b != b)
    if type(a) in (list, tuple):
        return len(a) == len(b) and all(_pin_same(x, y) for x, y in zip(a, b))
    if type(a) is dict:
        return a.keys() == b.keys() and all(_pin_same(a[k], b[k]) for k in a)
    return bool(a == b)
'''


def _call_source(module: str, qual: str, kind: str, call: dict) -> str:
    if kind == "method":
        target = f"{call['self']}.{qual.rpartition('.')[2]}"
    else:
        target = f"{module}.{qual}"
    parts = list(call.get("args") or [])
    odd = []
    for name, src in call.get("kwargs") or []:
        if name.isidentifier() and not keyword.iskeyword(name):
            parts.append(f"{name}={src}")
        else:
            odd.append(f"{name!r}: {src}")
    if odd:
        parts.append("**{" + ", ".join(odd) + "}")
    return f"{target}({', '.join(parts)})"


def _at(commit: str | None, dirty: bool) -> str:
    if not commit:
        return "a tree without a commit"
    return f"commit {commit[:12]}" + (" plus uncommitted changes" if dirty else "")


def _tests_of(cases: list[dict]) -> list[str]:
    """The test ids the cases were recorded in (calls made outside a test, such as at import, are not named)."""
    return list(dict.fromkeys(t for c in cases for t in c.get("tests") or [] if not t.startswith("<")))


def render(sym: str, module: str, qual: str, kind: str, cases: list[dict], *, commit: str | None,
           tree: str | None, dirty: bool = False) -> str:
    """The pytest file for ``cases`` (each with one recorded result)."""
    imports = {module}
    for c in cases:
        imports |= set(c.get("imports") or [])
    tests = _tests_of(cases)
    stem = qual.replace(".", "_")
    body: list[str] = []
    raises = False
    for n, c in enumerate(cases, 1):
        o = c["outs"][0]
        call = _call_source(module, qual, kind, c["call"])
        body += ["", "", f"def test_{stem}_pin_{n}():"]
        named = _tests_of([c])
        if named:
            body.append(f"    # recorded while {_one_line(', '.join(named[:3]))} ran")
        if "e" in o:
            raises = True
            body += [f"    with pytest.raises({o['e']}) as _pin_info:", f"        {call}",
                     f"    assert type(_pin_info.value) is {o['e']}",
                     f"    assert str(_pin_info.value) == {o['m']!r}"]
        else:
            body += [f"    _pin_expected = {o['r']}", f"    _pin_result = {call}",
                     "    assert _pin_same(_pin_result, _pin_expected)"]
    head = [f"# Generated by verinoda pin at {_at(commit, dirty)} (working-tree copy {(tree or '?')[:12]}) from "
            "these tests:"]
    head += [f"#   {_one_line(t)}" for t in tests[:10]]
    if len(tests) > 10:
        head.append(f"#   ... and {len(tests) - 10} more")
    head += [f"# Each test calls {_one_line(sym)} with inputs recorded while those tests ran and checks the "
             "result it",
             "# returned (or the exception it raised) then. A failure after a change means the behaviour on that "
             "input",
             "# changed: decide whether that was intended. This pins current behaviour; it is not a specification.",
             f"# Regenerate: verinoda pin {_one_line(sym)} --force", ""]
    head += (["import pytest", ""] if raises else [])
    head += [f"import {m}" for m in sorted(imports)]
    return "\n".join(head + SAME_SOURCE.rstrip("\n").split("\n") + body) + "\n"


# -- runs -------------------------------------------------------------------------------------------

def _argv(repo: Path, ids: list[str]) -> list[str]:
    return [experiments.python_for(repo), "-m", "pytest", "-q", "-p", PLUGIN_MODULE, "-p", "no:cacheprovider", *ids]


def _run(store: Store, repo: Path, ids: list[str], env: dict, out_name: str, *, hypothesis: str, commit,
         timeout: float, add_files: dict | None = None) -> tuple[dict, dict]:
    exp = experiments.run(store, repo, _argv(repo, ids), hypothesis=hypothesis, commit=commit, timeout=timeout,
                          plugins={f"{PLUGIN_MODULE}.py": plugin_source()},
                          env_extra={**env, "VERINODA_PIN_OUT": out_name}, add_files=add_files)
    path = (exp.get("artifacts") or {}).get(out_name)
    data = Path(path).read_bytes() if path and Path(path).is_file() else b""
    return exp, parse_output(data)


def _replay(store, repo, out_rel, text, *, sym, commit, timeout, what) -> tuple[dict, dict]:
    return _run(store, repo, [out_rel], {"VERINODA_PIN_MODE": "replay"}, REPLAY_FILE,
                hypothesis=f"pin {sym}: {what}", commit=commit, timeout=timeout,
                add_files={out_rel: text.encode("utf-8")})


def _test_name(nodeid: str) -> str:
    return nodeid.rpartition("::")[2].split("[")[0]


# -- the command ------------------------------------------------------------------------------------

def pin(store: Store, repo: Path, symbol: str, *, tests: list[str] | None = None,
        max_cases: int = DEFAULT_MAX_CASES, out: str | None = None, force: bool = False,
        timeout: float | None = None, record: bool = True, files: list[str] | None = None) -> dict:
    """Record, generate, replay and write; see the module docstring. Returns the result dict."""
    from verinoda.paths import load_config

    repo = Path(repo).resolve()
    max_cases = max(1, min(int(max_cases or DEFAULT_MAX_CASES), MAX_CASES))
    files = files if files is not None else list_files(repo)
    rel, qual, kind, why, def_lines = _target(repo, symbol, files)
    sym = f"{rel}::{qual}"
    res: dict = {"symbol": sym, "kind": kind, "status": None, "generated": None, "written": False,
                 "limits": list(LIMITS)}
    if why:
        return {**res, "status": "unsupported", "headline": f"{sym}: {why}",
                "next_step": "pin a top-level function, a static or class method, or a method of a dataclass"}
    out_rel = _out_path(qual, out)
    res["generated"] = out_rel
    if (repo / out_rel).exists() and not force:
        return {**res, "status": "refused", "headline": f"{out_rel} exists; nothing was run",
                "next_step": "pass --force to replace it, or --out another path"}
    if tests:
        ids = [str(t).replace("\\", "/") for t in tests]
        selection = {"given": len(ids)}
    else:
        ids, selection = select(store, repo, rel, qual, def_lines, exclude=out_rel)
    res["selection"] = selection
    if not ids:
        hint = "" if selection.get("graph") else " (no index: run `verinoda scan` first)"
        return {**res, "status": "unknown", "headline": f"no test is known to run {sym}{hint}; nothing was run",
                "next_step": f"name the tests: `verinoda pin {sym} --tests tests/`, or observe them first "
                             f"(`verinoda observe --for {qual.rpartition('.')[2]}`)"}
    if len(ids) > MAX_TESTS:
        ids = ids[:MAX_TESTS]
    res["tests"] = ids
    info = git_info(repo)
    commit = info.get("commit") or None
    # the copies are made from the working tree: uncommitted changes are part of what is pinned
    dirty = bool(info.get("dirty"))
    res.update(commit=commit, uncommitted_changes=dirty)
    cfg = load_config(repo)["experiments"]
    t_out = float(timeout or 2 * float(cfg["default_timeout"]))
    env = {"VERINODA_PIN_MODE": "record", "VERINODA_PIN_FILE": rel, "VERINODA_PIN_QUAL": qual,
           "VERINODA_PIN_KIND": kind, "VERINODA_PIN_MAX_INPUTS": str(min(2000, max(200, 4 * max_cases)))}
    try:
        exp, rec = _run(store, repo, ids, env, RECORD_FILE, commit=commit, timeout=t_out,
                        hypothesis=f"pin {sym}: record its calls while {len(ids)} test(s) run")
    except experiments.ExperimentRefused as exc:
        return {**res, **experiments.refusal(repo, exc), "symbol": sym, "generated": out_rel, "written": False,
                "headline": f"refused: {exc}"}
    res["record_run"] = exp["id"]
    res["tree"] = (exp.get("tree") or {}).get("hash")
    h = rec["header"]
    if h is None:
        return {**res, "status": "inconclusive", "headline": f"the recording run ended {exp['outcome']} without the "
                                                             "pin plugin's output",
                "logs": exp["logs"], "next_step": "read the run's stderr log; fix the test environment and pin again"}
    if h.get("error"):
        return {**res, "status": "unsupported", "headline": f"the recorder could not run: {h['error']}",
                "next_step": "run the tests with Python 3.12 or later" if "3.12" in str(h["error"]) else
                "another tool holds every free sys.monitoring tool id in the test run (2, 3 and 4)"}
    failed = sorted(t for t, ph in rec["tests"].items() if not _passed(ph) and ph.get("call") != "skipped")
    res["tests_failed"] = failed[:20]
    res["calls"] = int(h.get("calls") or 0)
    res["skipped_calls"] = h.get("skipped") or {}
    module = h.get("module")
    dropped: Counter = Counter()
    cand = []
    for c in rec["cases"]:
        if len(c["outs"]) != 1:
            dropped[DROP_INCONSISTENT] += 1
        else:
            cand.append(c)
    if len(cand) > max_cases:
        dropped[DROP_OVER_MAX] += len(cand) - max_cases
        cand = cand[:max_cases]
    res["cases"] = {"recorded": len(rec["cases"]), "kept": 0, "dropped": dict(dropped)}
    if not cand or not module:
        what = (f"the selected tests did not call {qual}" if not res["calls"] else
                f"none of the {res['calls']} call(s) of {qual} could be pinned")
        return {**res, "status": "nothing_pinned", "headline": f"{what}; nothing was written",
                "next_step": "see skipped_calls for the reasons, or name other tests with --tests"}
    if not all(p.isidentifier() for p in module.split(".")) or module == "__main__":
        return {**res, "status": "unsupported", "headline": f"{sym} ran as module {module!r}, which a test cannot "
                                                            "import by name"}
    text = render(sym, module, qual, kind, cand, commit=commit, tree=res["tree"], dirty=dirty)
    exp1, rp = _replay(store, repo, out_rel, text, sym=sym, commit=commit, timeout=t_out,
                       what=f"the {len(cand)} generated test(s) pass on the working tree")
    res["replay_run"] = exp1["id"]
    stem = qual.replace(".", "_")
    by_name = {_test_name(nid): ph for nid, ph in rp["tests"].items() if nid.split("::")[0] == out_rel}
    if not by_name:
        return {**res, "status": "inconclusive",
                "headline": f"the generated file did not run on replay ({exp1['outcome']}"
                            + (f": {exp1['inconclusive_reason']}" if exp1.get("inconclusive_reason") else "") + ")",
                "logs": exp1["logs"], "next_step": "read the replay run's stderr log (an import of the module may "
                                                   "need the project's pytest settings)"}
    kept = [c for n, c in enumerate(cand, 1) if _passed(by_name.get(f"test_{stem}_pin_{n}") or {})]
    if len(kept) < len(cand):
        dropped[DROP_REPLAY] += len(cand) - len(kept)
    res["cases"] = {"recorded": len(rec["cases"]), "kept": len(kept), "dropped": dict(dropped)}
    if not kept:
        return {**res, "status": "nothing_pinned",
                "headline": f"none of the {len(cand)} recorded input(s) gave the same result on replay; nothing "
                            "was written", "next_step": "the function's result depends on time, randomness or "
                                                        "state: pin a deterministic part of it"}
    final_exp = exp1
    if len(kept) < len(cand):
        text = render(sym, module, qual, kind, kept, commit=commit, tree=res["tree"], dirty=dirty)
        final_exp, _ = _replay(store, repo, out_rel, text, sym=sym, commit=commit, timeout=t_out,
                               what=f"the {len(kept)} kept test(s) pass on the working tree")
        res["confirm_run"] = final_exp["id"]
        if final_exp["outcome"] != "pass":
            return {**res, "status": "unstable",
                    "headline": f"the {len(kept)} test(s) that passed on replay did not all pass again "
                                f"({final_exp['outcome']}); nothing was written",
                    "logs": final_exp["logs"], "next_step": "the function is not deterministic on these inputs"}
    elif exp1["outcome"] != "pass":
        return {**res, "status": "inconclusive", "headline": f"the replay run ended {exp1['outcome']} although its "
                                                             "tests passed", "logs": exp1["logs"]}
    dest = repo / out_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(text.encode("utf-8"))
    res.update(written=True, status="pinned", run_id=final_exp["id"], tests_from=_tests_of(kept)[:20],
               headline=f"pinned {sym} on {len(kept)} input(s) at {_at(commit, dirty)}: {out_rel}"
                        + (f" ({sum(dropped.values())} dropped)" if dropped else ""))
    if record:
        try:
            res["claim"] = _record_claim(store, repo, sym, out_rel, len(kept), _at(commit, dirty), final_exp)
        except Exception as exc:  # noqa: BLE001 - the written file stands without its claim
            res["claim_error"] = f"{type(exc).__name__}: {exc}"
    return res


def _record_claim(store: Store, repo: Path, sym: str, out_rel: str, n: int, at: str, exp: dict) -> dict:
    from verinoda.claims import Claims

    snap = store.latest_snapshot()
    project = (snap or {}).get("project") or Path(repo).name
    text = (f"{sym}'s behaviour on these {n} inputs is pinned at {at}: the generated tests in {out_rel} pass in "
            f"run {exp['id']} (working-tree copy {((exp.get('tree') or {}).get('hash') or '?')[:12]}).")
    c = Claims(store, repo).create(text, project=project, snapshot=snap, subjects=[sym], status="experiment_verified",
                                   kind="test_run", spec={"experiment": exp["id"], "pin": {"file": out_rel, "cases": n,
                                                                                          "at": at}},
                                   evidence=[(exp["evidence_id"], "supports")])
    return {"id": c["id"], "status": c["status"], "text": text}


def render_text(res: dict) -> str:
    lines = [f"[{res.get('status')}] {res.get('headline') or ''}"]
    if res.get("tests"):
        lines.append(f"  tests run: {len(res['tests'])}" + (f" ({len(res['tests_failed'])} failed)"
                                                            if res.get("tests_failed") else ""))
    c = res.get("cases")
    if c:
        lines.append(f"  inputs: {c['recorded']} distinct recorded, {c['kept']} kept")
        for why, n in sorted((c.get("dropped") or {}).items()):
            lines.append(f"  dropped {n}: {why}")
    for why, n in sorted((res.get("skipped_calls") or {}).items(), key=lambda kv: -kv[1])[:10]:
        lines.append(f"  skipped {n} call(s): {why}")
    for k in ("record_run", "replay_run", "confirm_run"):
        if res.get(k):
            lines.append(f"  {k.replace('_', ' ')}: {res[k]}")
    if res.get("claim"):
        lines.append(f"  claim {res['claim']['id']} [{res['claim']['status']}]")
    if res.get("reason"):
        lines.append(f"  reason: {res['reason']}")
    if res.get("next_step"):
        lines.append(f"  next: {res['next_step']}")
    return "\n".join(lines) + "\n"
