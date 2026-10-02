"""Oracle fuzz of the update proportional to the change (``verinoda/incremental.py``).

    python -P tools/incremental_fuzz.py --corpus DIR [--corpus DIR ...] --seed N --edits K [--work DIR] [--json]

For each corpus: a copy is initialised and scanned with the switch on (``VERINODA_INCREMENTAL=1``), then K random
edits are applied one after the other - a function added, renamed or removed, a call pointed at another function,
an import added or removed, a comment or docstring edited, a class base changed, a Markdown section edited - and
after each one ``verinoda update`` runs. Its graph is compared (``tools/graph_equal.py``) with the graph a scan of
a FRESH COPY of the edited tree gives (the switch off, no index, no cache). Every difference is a bug or a missing
fallback. The edits are drawn from ``random.Random(seed)``: a seed replays the same edits.

Runs in one process (the corpora are small; the fresh copies are scanned in the same process, each with its
own index). Prints one line per edit, or with ``--json`` the list of results.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "tools"))
import graph_equal  # noqa: E402

PY, JS, GO, JAVA, MD = ".py", ".js/.ts", ".go", ".java", ".md"
_KIND = {".py": PY, ".js": JS, ".ts": JS, ".tsx": JS, ".jsx": JS, ".mjs": JS, ".go": GO, ".java": JAVA,
         ".md": MD}
_SKIP_PARTS = {".verinoda", ".git", "__pycache__", "node_modules"}


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                    "-c", "core.longpaths=true", *args], cwd=cwd, check=True, capture_output=True,
                   stdin=subprocess.DEVNULL)


def copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".verinoda", ".git", "__pycache__", "*.pyc"))
    git(dst, "init", "-q")
    git(dst, "add", "-A")
    git(dst, "commit", "-q", "-m", "corpus")


def _files(root: Path) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and not (set(p.relative_to(root).parts) & _SKIP_PARTS) and p.suffix in _KIND:
            out.append(p)
    return out


# --------------------------------------------------------------------------------------------- edits

_DEF = {PY: re.compile(r"^(?:async )?def (\w+)\(", re.M), JS: re.compile(r"function (\w+)\s*\(", re.M),
        GO: re.compile(r"^func (?:\([^)]*\) )?(\w+)\(", re.M),
        JAVA: re.compile(r"^\s+(?:(?:public|private|protected|static|final|synchronized)\s+)+[\w<>\[\],. ]+?\s+(\w+)\s*\([^;]*$", re.M)}
_CLASS = {PY: re.compile(r"^class (\w+)", re.M), JS: re.compile(r"class (\w+)", re.M),
          JAVA: re.compile(r"\b(?:class|interface|enum|record) (\w+)", re.M), GO: re.compile(r"^type (\w+) struct", re.M)}
_CALL = re.compile(r"\b([A-Za-z_]\w*)\(")
_KEYWORDS = {"if", "for", "while", "return", "switch", "catch", "function", "new", "super", "this", "print", "len",
             "make", "append", "range", "int", "str", "list", "dict", "set", "sizeof", "func", "def", "class",
             "elif", "and", "or", "not", "in", "is", "lambda", "with", "assert", "yield", "await", "typeof",
             "require", "import", "from", "export", "default", "const", "let", "var", "case", "throw"}


def _symbols(root: Path) -> dict[str, dict]:
    """Per language: function names, class names, and per file what it defines."""
    out: dict[str, dict] = {}
    for p in _files(root):
        kind = _KIND[p.suffix]
        text = p.read_text(encoding="utf-8", errors="replace")
        d = out.setdefault(kind, {"funcs": set(), "classes": set(), "by_file": {}})
        rel = p.relative_to(root).as_posix()
        fs = set(_DEF[kind].findall(text)) if kind in _DEF else set()
        cs = set(_CLASS[kind].findall(text)) if kind in _CLASS else set()
        fs -= _KEYWORDS
        d["funcs"] |= fs
        d["classes"] |= cs
        d["by_file"][rel] = (fs, cs)
    return out


def _module_of(rel: str) -> str:
    return rel[:-3].replace("/", ".") if rel.endswith(".py") else rel


def _block_end(lines: list[str], i: int, kind: str) -> int:
    """The index after the block that starts at line i (a top-level def / func / method)."""
    if kind == PY:
        j = i + 1
        while j < len(lines) and (not lines[j].strip() or lines[j][:1] in (" ", "\t")):
            j += 1
        return j
    depth, opened = 0, False
    for j in range(i, len(lines)):
        depth += lines[j].count("{") - lines[j].count("}")
        if "{" in lines[j]:
            opened = True
        if opened and depth <= 0:
            return j + 1
    return i + 1


def edit_once(root: Path, rng: random.Random, n: int) -> dict:
    """Apply one random edit to a file of ``root``; returns what was done (or {"op": None})."""
    syms = _symbols(root)
    files = _files(root)
    rng.shuffle(files)
    ops = ["add_function", "rename_function", "remove_function", "change_call", "change_import", "comment",
           "class_base", "markdown"]
    for _ in range(40):
        p = rng.choice(files)
        kind = _KIND[p.suffix]
        op = "markdown" if kind == MD else rng.choice([o for o in ops if o != "markdown"])
        text = p.read_text(encoding="utf-8", errors="replace")
        rel = p.relative_to(root).as_posix()
        funcs = sorted(syms.get(kind, {}).get("funcs", set())) if kind != MD else []
        new = _apply(op, kind, text, rel, rng, n, syms, funcs)
        if new is not None and new != text:
            p.write_text(new, encoding="utf-8", newline="\n")
            return {"op": op, "file": rel}
    return {"op": None}


def _apply(op, kind, text, rel, rng, n, syms, funcs):
    lines = text.split("\n")
    callee = rng.choice(funcs) if funcs else None
    if op == "add_function":
        if kind == PY:
            body = f"{callee}(x)" if callee else "x"
            return text.rstrip("\n") + f"\n\n\ndef fz{n}(x):\n    return {body}\n"
        if kind == JS:
            body = f"{callee}(x)" if callee else "x"
            pre = "export " if rel.endswith((".ts", ".tsx")) else ""
            return text.rstrip("\n") + f"\n\n{pre}function fz{n}(x) {{\n  return {body};\n}}\n"
        if kind == GO:
            body = f"{callee}(x, 1)" if callee and callee[:1].isupper() else "x"
            return text.rstrip("\n") + f"\n\nfunc Fz{n}(x int64) int64 {{\n\treturn {body}\n}}\n"
        if kind == JAVA:
            k = text.rstrip().rfind("}")
            if k < 0:
                return None
            call = f"        {callee}();\n" if callee else ""
            return text[:k] + f"\n    public void fz{n}() {{\n{call}    }}\n" + text[k:]
    if op in ("rename_function", "remove_function"):
        pat = _DEF.get(kind)
        if pat is None:
            return None
        ms = [m for m in pat.finditer(text) if m.group(1) not in _KEYWORDS and m.group(1) != "main"]
        if not ms:
            return None
        m = rng.choice(ms)
        if op == "rename_function":
            a, b = m.span(1)
            return text[:a] + m.group(1) + f"R{n}" + text[b:]
        i = text.count("\n", 0, m.start())
        if kind == JAVA and lines[i].strip().endswith(";"):
            return None
        j = _block_end(lines, i, kind)
        # keep a leading decorator/annotation/comment line with its function
        return "\n".join(lines[:i] + lines[j:])
    if op == "change_call":
        if not funcs:
            return None
        cands = [m for m in _CALL.finditer(text) if m.group(1) in set(funcs)
                 and not text[max(0, m.start() - 9):m.start()].rstrip().endswith(("def", "function", "func"))]
        if not cands:
            return None
        m = rng.choice(cands)
        others = [f for f in funcs if f != m.group(1)]
        if not others:
            return None
        a, b = m.span(1)
        return text[:a] + rng.choice(others) + text[b:]
    if op == "change_import":
        imp = [i for i, ln in enumerate(lines) if re.match(r"\s*(from \S+ import|import |export \{.*\} from)", ln)]
        if imp and rng.random() < 0.5:
            i = rng.choice(imp)
            if kind == GO and lines[i].strip() == "import (":
                return None
            return "\n".join(lines[:i] + lines[i + 1:])
        others = [(f, fs) for f, (fs, cs) in syms.get(kind, {}).get("by_file", {}).items() if f != rel and fs]
        if not others:
            return None
        f, fs = rng.choice(others)
        name = rng.choice(sorted(fs))
        if kind == PY:
            line = f"from {_module_of(f)} import {name}"
        elif kind == JS:
            here = Path(rel).parent
            target = os.path.relpath(Path(f).with_suffix(""), here).replace("\\", "/")
            if not target.startswith("."):
                target = "./" + target
            line = f'import {{ {name} }} from "{target}";' if rel.endswith((".ts", ".tsx")) else \
                f'const {{ {name} }} = require("{target}");'
        elif kind == GO:
            pkg = "example.com/goapp/" + str(Path(f).parent.as_posix())
            for i, ln in enumerate(lines):
                if ln.startswith("package "):
                    lines.insert(i + 1, f'\nimport _ "{pkg}"')
                    return "\n".join(lines)
            return None
        else:
            cls = sorted(syms.get(kind, {}).get("by_file", {}).get(f, (set(), set()))[1])
            if not cls:
                return None
            pkg = re.sub(r"^.*?java/", "", str(Path(f).parent.as_posix())).replace("/", ".")
            line = f"import {pkg}.{cls[0]};"
        at = (imp[-1] + 1) if imp else next((i + 1 for i, ln in enumerate(lines) if ln.startswith("package ")), 0)
        return "\n".join(lines[:at] + [line] + lines[at:])
    if op == "comment":
        i = rng.randrange(0, len(lines) + 1)
        mark = {PY: "#", JS: "//", GO: "//", JAVA: "//"}.get(kind, "#")
        indent = re.match(r"\s*", lines[i] if i < len(lines) else "").group(0)
        if kind == PY and '"""' in text and rng.random() < 0.5:
            k = text.find('"""')
            return text[:k + 3] + f"Fuzz note {n}. " + text[k + 3:]
        return "\n".join(lines[:i] + [f"{indent}{mark} fuzz note {n}"] + lines[i:])
    if op == "class_base":
        classes = sorted(syms.get(kind, {}).get("classes", set()))
        if kind == PY:
            ms = list(re.finditer(r"^class (\w+)(\([^)]*\))?:", text, re.M))
            if not ms or not classes:
                return None
            m = rng.choice(ms)
            base = rng.choice([c for c in classes if c != m.group(1)] or ["object"])
            return text[:m.start()] + f"class {m.group(1)}({base}):" + text[m.end():]
        if kind in (JS, JAVA):
            ms = list(re.finditer(r"\bclass (\w+)( extends \w+)?", text))
            if not ms or not classes:
                return None
            m = rng.choice(ms)
            others = [c for c in classes if c != m.group(1)]
            if not others:
                return None
            if m.group(2) and rng.random() < 0.3:
                return text[:m.start()] + f"class {m.group(1)}" + text[m.end():]
            return text[:m.start()] + f"class {m.group(1)} extends {rng.choice(others)}" + text[m.end():]
        return None
    if op == "markdown":
        allf = sorted(set().union(*(d["funcs"] | d["classes"] for d in syms.values()))) or ["main"]
        r = rng.random()
        if r < 0.4:
            return text.rstrip("\n") + f"\n\n## Fuzz {n}\n\nSee `{rng.choice(allf)}` and `{rng.choice(allf)}`.\n"
        heads = [i for i, ln in enumerate(lines) if ln.startswith("#")]
        if r < 0.7 and heads:
            i = rng.choice(heads)
            return "\n".join(lines[:i] + [lines[i] + f" {n}"] + lines[i + 1:])
        i = rng.randrange(0, len(lines) + 1)
        return "\n".join(lines[:i] + [f"A note that names `{rng.choice(allf)}`."] + lines[i:])
    return None


# --------------------------------------------------------------------------------------------- runs

class _Env:
    def __init__(self, **kv):
        self.kv = kv

    def __enter__(self):
        self.old = {k: os.environ.get(k) for k in self.kv}
        for k, v in self.kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _scan(repo: Path, switch: str | None) -> dict:
    from verinoda import workflow
    from verinoda.store import open_store

    with _Env(VERINODA_INCREMENTAL=switch):
        workflow.init(repo)
        st = open_store(repo)
        try:
            return workflow.scan(st, repo)
        finally:
            st.close()


def _update(repo: Path, switch: str) -> dict:
    from verinoda import workflow
    from verinoda.store import open_store

    with _Env(VERINODA_INCREMENTAL=switch):
        st = open_store(repo)
        try:
            return workflow.update(st, repo)
        finally:
            st.close()


def _graph(repo: Path) -> dict:
    return json.loads((repo / ".verinoda" / "index" / "graph.json").read_text(encoding="utf-8"))


def fresh_graph(src: Path, work: Path) -> dict:
    b = work / f"fresh{time.monotonic_ns()}"
    copy_tree(src, b)
    try:
        _scan(b, "0")
        return _graph(b)
    finally:
        shutil.rmtree(b, ignore_errors=True)


def run(corpus: Path, seed: int, edits: int, work: Path, *, switch: str = "1", keep_failed: bool = True) -> list[dict]:
    os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")
    rng = random.Random(seed)
    a = work / f"{corpus.name}-{seed}"
    if a.exists():
        shutil.rmtree(a)
    copy_tree(corpus, a)
    _scan(a, switch)
    results = []
    for k in range(edits):
        what = edit_once(a, rng, k)
        t = time.perf_counter()
        upd = _update(a, switch)
        dt = time.perf_counter() - t
        res = graph_equal.compare(_graph(a), fresh_graph(a, work))
        inc = upd.get("incremental") or {}
        row = {"corpus": corpus.name, "seed": seed, "step": k, **what, "update_s": round(dt, 2),
               "index_mode": upd.get("index_mode"), "used": bool(inc.get("used")), "reason": inc.get("reason"),
               "equal": res["equal"], "counts": res["counts"], "affected": inc.get("affected"),
               "batch_files": inc.get("batch_files")}
        if not res["equal"]:
            row["sample"] = {x: res[x][:3] for x in ("nodes_only_in_a", "nodes_only_in_b", "edges_only_in_a",
                                                      "edges_only_in_b")}
            row["changed_sample"] = res["nodes_changed"][:2]
            if keep_failed:
                keep = work / f"failed-{corpus.name}-{seed}-{k}"
                shutil.copytree(a, keep, ignore=shutil.ignore_patterns(".git"), dirs_exist_ok=True)
                row["kept"] = str(keep)
        results.append(row)
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--corpus", action="append", required=True)
    ap.add_argument("--seed", type=int, action="append")
    ap.add_argument("--edits", type=int, default=10)
    ap.add_argument("--work")
    ap.add_argument("--switch", default="1", help="the VERINODA_INCREMENTAL value of the edited copy (1 or verify)")
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv)
    work = Path(ns.work or tempfile.mkdtemp(prefix="incfuzz"))
    work.mkdir(parents=True, exist_ok=True)
    allres = []
    for c in ns.corpus:
        for seed in ns.seed or [1]:
            for row in run(Path(c), seed, ns.edits, work, switch=ns.switch):
                allres.append(row)
                if not ns.json:
                    print(json.dumps(row, ensure_ascii=False)[:700], flush=True)
    bad = [r for r in allres if not r["equal"]]
    summary = {"edits": len(allres), "used": sum(r["used"] for r in allres), "unequal": len(bad),
               "fallback_reasons": sorted({r["reason"] for r in allres if not r["used"] and r["reason"]})}
    if ns.json:
        print(json.dumps({"summary": summary, "results": allres}, indent=1, ensure_ascii=False))
    else:
        print(json.dumps(summary, ensure_ascii=False))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
