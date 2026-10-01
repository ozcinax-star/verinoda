"""Property test templates for ``verinoda probe --emit-test --template KIND``.

Three kinds, each run by the probe on its own generated inputs before the file is written:

* ``roundtrip``: ``g(f(x)) == x`` for an inverse ``g`` named with ``--inverse`` (``x`` is the first argument);
* ``idempotent``: ``f(f(x), *rest) == f(x, *rest)`` (applying ``f`` to its own result changes nothing);
* ``equivalence``: ``f`` in the working tree returns what ``f`` at the base returned (the probe's differential).

The written pytest file holds the inputs the probe generated (annotations, call sites, mined boundaries, edges,
then hypothesis when it is installed, else a fixed pseudo-random list) as source, so it needs neither Verinoda
nor hypothesis to run. Its header and the probe's result say on how many of those inputs the property was
*observed* to hold in the probe's runs: an observation over a finite list, never a verification. An existing file
is never overwritten.
"""

from __future__ import annotations

from pathlib import Path

from verinoda import probe_inputs as pin

KINDS = ("roundtrip", "idempotent", "equivalence")
EMIT_MAX = 50  # inputs written into one file
SOURCE_MAX = 2000  # characters of one input's source; a longer input is left out of the file


def plan(kind: str, qual: str, call_kind: str, params: list[dict], module: str,
         inverse: tuple[str, str, str] | None, differential: bool) -> dict:
    """The property the probe checks for ``kind`` (None for equivalence: the differential is the check) and the
    names the plugin binds for it. ``inverse`` is ``(module, qual, rel)`` of g. Raises ValueError when the kind
    does not fit the function."""
    if kind not in KINDS:
        raise ValueError(f"unknown template {kind!r} (one of {', '.join(KINDS)})")
    if call_kind == "method":
        raise ValueError(f"{qual} is an instance method: property templates take a function, a static method or a "
                         "class method")
    if kind == "equivalence":
        if inverse:
            raise ValueError("--inverse is for the roundtrip template")
        if not differential:
            raise ValueError(f"the equivalence template compares {qual} with its base version, and there is none "
                             "(no base commit, --no-base, or the function is new)")
        return {"kind": kind, "property": None, "refs": {}}
    pos = [p for p in params if p["kind"] != "kwonly"]
    if not pos or pos[0]["default"] is not None:
        raise ValueError(f"the {kind} template needs {qual} to take a required positional first parameter")
    first = pos[0]["name"]
    head = qual.split(".")[0]
    refs = {head: module}
    if kind == "roundtrip":
        if not inverse:
            raise ValueError("the roundtrip template needs the inverse function: --inverse path.py::name")
        gmod, gqual, grel = inverse
        ghead = gqual.split(".")[0]
        if ghead in refs and refs[ghead] != gmod:
            raise ValueError(f"{gqual} and {qual} share the name {ghead!r} in different modules")
        refs = {ghead: gmod}
        prop = f"{gqual}(result) == {first}"
        extra = {"inverse": f"{grel}::{gqual}", "check": f"{gqual}(result) == args[0]"}
    else:
        if inverse:
            raise ValueError("--inverse is for the roundtrip template")
        many = len(params) > 1
        prop = f"{qual}(result{', *__args__[1:], **__kwargs__' if many else ''}) == result"
        extra = {"check": f"{qual}(result{', *args[1:], **kwargs' if many else ''}) == result"}
    clash = set(refs) & ({p["name"] for p in params} | {"result"})
    if clash:
        raise ValueError(f"a parameter of {qual} is named {sorted(clash)[0]!r}, like the function the property "
                         "calls")
    return {"kind": kind, "property": prop, "refs": refs, "first": first, **extra}


def default_path(repo: Path, qual: str, kind: str) -> str:
    base = "tests/" if (repo / "tests").is_dir() else ""
    return f"{base}test_{qual.replace('.', '_').lower()}_{kind}.py"


def check_path(repo: Path, path: str) -> str:
    """The repository-relative path of the file to write; ValueError when it exists, is outside the repository
    or is not a ``.py`` file."""
    repo = Path(repo).resolve()
    p = Path(path)
    full = (p if p.is_absolute() else repo / p).resolve()
    try:
        rel = full.relative_to(repo).as_posix()
    except ValueError:
        raise ValueError(f"{path} is outside the repository") from None
    if full.suffix != ".py":
        raise ValueError(f"{path} is not a .py file")
    if full.exists():
        raise ValueError(f"{rel} exists: the property test is never written over a file (name another with "
                         "--test-file)")
    return rel


def _pinnable(o: dict) -> bool:
    if "e" in o:
        return True
    notes = set(o.get("np") or [])
    return "r" in o and not o.get("h") and not notes & {"masked", "raised", "digits"}


def observe(tmpl: dict, prop_index: int | None, cases: list[dict], rows_h: dict, rows_b: dict,
            skip: set[int], differing: set[int]) -> tuple[dict, list[int]]:
    """What was observed, and the inputs for the file (counterexamples first). ``skip``: inputs not compared
    (not run on a side, blocked, nondeterministic)."""
    chosen: list[int] = []
    if tmpl["kind"] == "equivalence":
        evaluated = [i for i in range(len(cases)) if i not in skip and all(
            "r" in r["x"][0] or "e" in r["x"][0] for r in (rows_h[i], rows_b[i]))]
        bad = [i for i in evaluated if i in differing]
        pool = [i for i in bad + [i for i in evaluated if i not in differing] if _pinnable(rows_b[i]["x"][0])]
        what = "the working tree returned or raised what the base did"
    else:
        evaluated, bad = [], []
        for i in range(len(cases)):
            o = (rows_h.get(i) or {}).get("x", [{}])[0]
            if i in skip or "r" not in o or any(e[0] == prop_index for e in o.get("pe") or []):
                continue
            evaluated.append(i)
            if prop_index in (o.get("pv") or []):
                bad.append(i)
        pool = bad + [i for i in evaluated if i not in set(bad)]
        what = f"`{tmpl['property']}`"
    for i in pool:
        if len(chosen) >= EMIT_MAX:
            break
        if len(pin.call_source(cases[i], full=True)) <= SOURCE_MAX:
            chosen.append(i)
    n, k = len(evaluated), len(bad)
    if not n:
        status, text = "not_evaluated", f"{what}: not evaluated on any input (every call raised or was not compared)"
    elif k:
        status, text = "did_not_hold", f"{what} did not hold on {k} of {n} generated inputs"
    else:
        status, text = "observed_to_hold", (f"{what} held on all {n} generated inputs it was evaluated on (an "
                                            "observation, not a verification)")
    return {"status": status, "observed": text, "evaluated": n, "counterexamples": k}, chosen


REPR_HELPERS = '''

def _probe_repr(v, depth=0):
    """repr with the elements of sets in sorted order, as verinoda probe rendered the result."""
    t = type(v)
    if depth > 20:
        return repr(v)
    if t in (set, frozenset) and len(v) <= 10_000:
        items = sorted(_probe_repr(x, depth + 1) for x in v)
        if t is set:
            return "{" + ", ".join(items) + "}" if items else "set()"
        return "frozenset({" + ", ".join(items) + "})" if items else "frozenset()"
    if t in (list, tuple) and len(v) <= 10_000 and any(type(x) in (set, frozenset, list, tuple, dict) for x in v):
        inner = ", ".join(_probe_repr(x, depth + 1) for x in v)
        return f"[{inner}]" if t is list else ("(" + inner + ("," if len(v) == 1 else "") + ")")
    if t is dict and len(v) <= 10_000 and any(type(x) in (set, frozenset, list, tuple, dict) for x in v.values()):
        pairs = (f"{_probe_repr(k, depth + 1)}: {_probe_repr(x, depth + 1)}" for k, x in v.items())
        return "{" + ", ".join(pairs) + "}"
    return repr(v)


def _type_name(v):
    t = type(v)
    return f"{t.__module__}.{t.__qualname__}"
'''


def render(tmpl: dict, obs: dict, chosen: list[int], cases: list[dict], rows_b: dict, qual: str, module: str,
           pid: str, runs: dict, materialize_max: int) -> str:
    """The pytest file's text."""
    kind = tmpl["kind"]
    imports: dict[str, set[str]] = {module: {qual.split(".")[0]}}
    for name, mod in tmpl["refs"].items():
        imports.setdefault(mod, set()).add(name)
    rows = []
    for i in chosen:
        case = cases[i]
        for enc in case.get("a", []) + [v for _, v in case.get("k", [])]:
            pin.imports_needed(enc, imports)
        make = f"lambda: _args({pin.call_source(case, full=True)})"
        if kind == "equivalence":
            o = rows_b[i]["x"][0]
            exp = (("raises", o["e"]) if "e" in o else
                   ("value", o["r"], str(o.get("t") or "").endswith(" (materialized)")))
            rows.append(f"    ({make}, {exp!r}),  # input #{i}")
        else:
            rows.append(f"    {make},  # input #{i}")
    name = f"test_{qual.replace('.', '_').lower()}_{kind}"
    run_text = "; ".join(f"{'base' if k == 'base' else 'working tree'} {', '.join(v)}"
                         for k, v in runs.items() if k in ("base", "head") and v)
    head = [f"# Property test ({kind}) written by verinoda probe {pid} for {qual}.",
            f"# Observed in the probe's runs ({run_text}): {obs['observed']}.",
            "# Observed on these generated inputs only, never verified: other inputs may break it.",
            f"# {len(chosen)} of the probe's inputs are below" + (" (counterexamples first)."
                                                                  if obs["counterexamples"] else ".")]
    if kind == "equivalence":
        head.append("# Each expected outcome is the BASE version's; whether a change was intended is the user's "
                    "decision.")
    body = ["", "", "def _args(*args, **kwargs):", "    return args, kwargs", ""]
    if kind == "equivalence":
        body += ["", "CASES = ["] + rows + ["]", "", "",
                 '@pytest.mark.parametrize("make, expected", CASES)', f"def {name}(make, expected):",
                 "    args, kwargs = make()",
                 '    if expected[0] == "raises":',
                 "        with pytest.raises(BaseException) as info:",
                 f"            {qual}(*args, **kwargs)",
                 "        assert _type_name(info.value) == expected[1]",
                 "        return",
                 f"    result = {qual}(*args, **kwargs)",
                 "    if expected[2]:  # a generator or iterator: the probe compared what it yields",
                 f"        result = list(itertools.islice(result, {materialize_max}))",
                 "    assert _probe_repr(result) == expected[1]"]
    else:
        body += ["", "CASES = ["] + rows + ["]", "", "",
                 '@pytest.mark.parametrize("make", CASES)', f"def {name}(make):",
                 "    args, kwargs = make()",
                 f"    result = {qual}(*args, **kwargs)",
                 f"    assert {tmpl['check']}"]
    std = ["import itertools", ""] if kind == "equivalence" else []
    lines = head + std + ["import pytest", ""]
    lines += [f"from {m} import {', '.join(sorted(names))}" for m, names in sorted(imports.items())]
    if kind == "equivalence":
        lines += REPR_HELPERS.rstrip("\n").split("\n")
    return "\n".join(lines + body) + "\n"


def write(repo: Path, rel: str, text: str) -> str:
    """Write the file (never over an existing one); its absolute path."""
    full = Path(repo).resolve() / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    with open(full, "x", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return str(full)
