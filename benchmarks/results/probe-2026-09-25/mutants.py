"""Automated single-point mutants of the pure functions in the fixture project (docs/DESIGN.md D36, kill rate).

    python mutants.py generate WORKDIR survivors.json   # every mutant; keep those the project's tests do not catch
    python mutants.py probe WORKDIR survivors.json labels.json OUT.json

Operators, applied inside one function at a time: comparison flips (< <=, > >=, == !=), and/or swap, removing a
``not``, + <-> -, * <-> /, and integer constants +-1 (float constants +1%). The project is examples/orders_app plus
fixtures.EXTRA, committed as the base; the mutant is the working tree. ``labels.json`` ({id: "equivalent" |
"change", reason}) is written by hand after ``generate`` and before ``probe`` runs; the probe's kill rate counts the
non-equivalent survivors, and any difference on an equivalent one is a false alarm.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import fixtures  # noqa: E402
import run_fixtures  # noqa: E402

TARGETS = [("orders/pricing.py", "apply_discount"), ("orders/pricing.py", "compute_total"),
           ("orders/service.py", "validate_items"), ("orders/textutil.py", "customer_key"),
           ("orders/textutil.py", "mask_email"), ("orders/textutil.py", "title_ok"),
           ("orders/mathutil.py", "safe_div"), ("orders/mathutil.py", "percent"), ("orders/mathutil.py", "average"),
           ("orders/mathutil.py", "bucket"), ("orders/rules.py", "is_eligible"), ("orders/rules.py", "chunk")]
CMP_FLIP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq}
BIN_FLIP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult}


def _source(rel: str) -> str:
    base = ROOT / "examples" / "orders_app" / rel
    return fixtures.EXTRA[rel] if rel in fixtures.EXTRA else base.read_text(encoding="utf-8")


def _def(tree: ast.AST, name: str) -> ast.FunctionDef:
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)


def _points(fn: ast.FunctionDef) -> list[tuple[str, int, str]]:
    """(operator, node index in ast.walk order, description) for every mutation point of a function body."""
    doc = ast.get_docstring(fn) is not None
    out = []
    for i, n in enumerate(ast.walk(fn)):
        if isinstance(n, ast.Compare):
            for k, op in enumerate(n.ops):
                if type(op) in CMP_FLIP:
                    out.append(("cmp", i, f"{k}"))
        elif isinstance(n, ast.BoolOp):
            out.append(("bool", i, ""))
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            out.append(("not", i, ""))
        elif isinstance(n, ast.BinOp) and type(n.op) in BIN_FLIP:
            out.append(("bin", i, ""))
        elif isinstance(n, ast.Constant) and type(n.value) in (int, float) and not (doc and i == 0):
            if isinstance(n.value, int):
                out += [("int+1", i, ""), ("int-1", i, "")]
            else:
                out.append(("float+1%", i, ""))
    return out


def _apply(fn: ast.FunctionDef, op: str, idx: int, extra: str) -> ast.FunctionDef:
    m = copy.deepcopy(fn)
    node = list(ast.walk(m))[idx]
    if op == "cmp":
        k = int(extra)
        node.ops[k] = CMP_FLIP[type(node.ops[k])]()
    elif op == "bool":
        node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
    elif op == "not":
        parent = next(p for p in ast.walk(m) for f, v in ast.iter_fields(p)
                      if v is node or (isinstance(v, list) and any(x is node for x in v)))
        for f, v in ast.iter_fields(parent):
            if v is node:
                setattr(parent, f, node.operand)
            elif isinstance(v, list):
                for j, x in enumerate(v):
                    if x is node:
                        v[j] = node.operand
    elif op == "bin":
        node.op = BIN_FLIP[type(node.op)]()
    elif op == "int+1":
        node.value += 1
    elif op == "int-1":
        node.value -= 1
    elif op == "float+1%":
        node.value = node.value * 1.01
    return m


def mutants() -> list[dict]:
    out = []
    for rel, name in TARGETS:
        src = _source(rel)
        tree = ast.parse(src)
        fn = _def(tree, name)
        lines = src.splitlines(keepends=True)
        a, b = fn.lineno - 1 - len(fn.decorator_list), fn.end_lineno
        for op, idx, extra in _points(fn):
            m = _apply(fn, op, idx, extra)
            new_fn = ast.unparse(m)
            if new_fn == ast.unparse(fn):
                continue
            new_src = "".join(lines[:a]) + new_fn + "\n" + "".join(lines[b:])
            try:
                ast.parse(new_src)
            except SyntaxError:
                continue
            mid = f"{name}:{op}:{idx}:{extra}"
            out.append({"id": mid, "file": rel, "function": name, "operator": op,
                        "before": ast.unparse(fn), "after": new_fn, "source": new_src})
    return out


def build(work: Path, mid: str, rel: str, source: str) -> Path:
    fx = {"id": hashlib.sha1(mid.encode()).hexdigest()[:10], "edits": []}
    d = run_fixtures.build(work, fx)
    (d / rel).write_bytes(source.encode("utf-8"))
    return d


def generate(work: Path, out: Path) -> None:
    rows = []
    for m in mutants():
        d = build(work, m["id"], m["file"], m["source"])
        survived = run_fixtures.tests_pass(d)
        rows.append({k: v for k, v in m.items() if k != "source"} | {"survives_tests": survived})
        print(m["id"], "survives" if survived else "killed by tests", flush=True)
    out.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(f"{len(rows)} mutants, {sum(r['survives_tests'] for r in rows)} survive the tests")


def probe_all(work: Path, survivors: Path, labels: Path, out: Path) -> None:
    from verinoda import probe
    from verinoda.store import open_store

    rows = json.loads(survivors.read_text(encoding="utf-8"))
    lab = json.loads(labels.read_text(encoding="utf-8"))
    src = {m["id"]: m for m in mutants()}
    res_rows = []
    for r in rows:
        if not r["survives_tests"]:
            continue
        m = src[r["id"]]
        d = build(work, m["id"], m["file"], m["source"])
        st = open_store(d)
        t0 = time.monotonic()
        try:
            res = probe.probe(st, d, f"{m['file']}::{m['function']}", seed=0)
        finally:
            st.close()
        classes = [x["class"] for x in res.get("differences") or []]
        label = lab[r["id"]]["label"]
        row = {"id": r["id"], "label": label, "status": res["status"], "classes": classes,
               "example": ((res.get("differences") or [{}])[0].get("examples") or [{}])[0].get("call"),
               "duration_s": res.get("duration_s"), "wall_s": round(time.monotonic() - t0, 2)}
        res_rows.append(row)
        print(json.dumps(row), flush=True)
    change = [r for r in res_rows if r["label"] == "change"]
    eq = [r for r in res_rows if r["label"] == "equivalent"]
    killed = [r for r in change if r["status"] in ("differences_found", "property_violated")]
    alarms = [r for r in eq if r["classes"] or r["status"] in ("differences_found", "property_violated")]
    times = sorted(r["duration_s"] for r in res_rows if r["duration_s"])
    msg_only = [r for r in res_rows if r["label"] == "change_message_only"]
    summary = {"labels_sha256": hashlib.sha256(labels.read_bytes()).hexdigest(),
               "survivors": len(res_rows), "non_equivalent": len(change), "equivalent": len(eq),
               "message_only": {r["id"]: r["status"] for r in msg_only},
               "kill_rate": f"{len(killed)}/{len(change)}", "not_killed": [r["id"] for r in change if r not in killed],
               "false_alarms_on_equivalent": f"{len(alarms)}/{len(eq)}", "false_alarm_ids": [r["id"] for r in alarms],
               "time_per_probe_s": {"median": round(statistics.median(times), 2) if times else None,
                                    "max": times[-1] if times else None}}
    out.write_text(json.dumps({"summary": summary, "mutants": res_rows}, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "generate":
        generate(Path(sys.argv[2]), Path(sys.argv[3]))
    elif cmd == "probe":
        probe_all(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), Path(sys.argv[5]))
