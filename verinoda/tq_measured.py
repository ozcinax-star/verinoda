"""Measured frequencies for typed answers: the ``measured: k/n held-out`` of a ``verinoda tq`` answer.

``verinoda benchmark tq-audit`` (:mod:`verinoda.benchmark.tq_audit`) runs ``tq`` over the frozen held-out gold
sets and writes ``verinoda/data/tq_calibration.json``: for each cell of (question type, answer, status) how
many held-out answers fell in it and how many were right. An answer shows its cell's ``k/n`` only when all of
these hold, checked here at run time:

- the table's gold-set hash is :data:`GOLD_SHA` (the hash of :data:`GOLD_FILES`) and, in a source checkout,
  the gold files on disk still have those hashes;
- the table's engine hash equals :func:`engine_sha` of the installed sources (a table measured on other code
  is never shown);
- the cell has at least :data:`MIN_N` answers, its answer is decided (not ``?``), and the dev split's precision
  for the same cell lies inside the held-out Wilson interval (the audit records this as ``shown``).

It is a frequency on a named set, never a probability, and it never changes an answer or its status.

**The engine hash** is the sha256 of the lines ``<path>\\0<sha256 of the file's bytes, CRLF read as LF>\\n``,
one per file of :func:`engine_files` sorted by path (paths relative to the ``verinoda`` package, ``/``-separated):
``tq.py`` and ``index.py``, every package module they import at any depth (found by a static walk of the import
statements, those inside functions included), and every ``.py`` file under ``project_index/``; never this module
or ``benchmark/``. A missing root gives no hash, so nothing is shown.

**Options.** A cell also records the question options its held-out answers were asked with (``depth=3``,
``scope=lib``; ``""`` for none); an answer shows the cell only when its own options are among them, so an option
no held-out question used (``exists NAME scope=lib`` reads the installed library, another engine) shows nothing.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

PKG = Path(__file__).resolve().parent
TABLE = PKG / "data" / "tq_calibration.json"
SCHEMA = "verinoda.tq_calibration/1"
MIN_N = 30
# where the engine starts: every package module these import, at any depth, is part of it
ENGINE_ROOTS = ("tq.py", "index.py")
GRAPH_BUILDER = "project_index"
# never part of the engine: the display of the table itself and the harnesses that write it
ENGINE_EXCLUDED = ("tq_measured.py", "benchmark/")
# the frozen held-out files (paths under benchmarks/) and their sha256, as their MANIFEST.json files hold them
GOLD_FILES = {
    "tq_gold/held_out.json": "4860547c7c6ce26f0956c71882aa934eec3bc667b4accf1c73eb0202d60e589f",
    "tq_gold2/held_out.json": "c19578ede6befd35354ce8d43dfa6e1589d74e28e21c1e0afb1ee3bfd2f72031",
}
BENCHMARKS = PKG.parent / "benchmarks"   # present in a source checkout only


def gold_sha(files: dict[str, str] | None = None) -> str:
    """One hash for a set of gold files: sha256 of ``<path> <sha256>\\n`` per file, sorted by path."""
    files = GOLD_FILES if files is None else files
    text = "".join(f"{p} {files[p]}\n" for p in sorted(files))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


GOLD_SHA = gold_sha()


def _excluded(rel: str) -> bool:
    return any(rel == e or (e.endswith("/") and rel.startswith(e)) for e in ENGINE_EXCLUDED)


def _module_file(pkg: Path, parts: list[str], probes: set[str]) -> list[str]:
    """The package files that importing ``verinoda.<parts>`` runs: the module and its packages' ``__init__.py``."""
    out = []
    for i in range(len(parts) + 1):
        head = parts[:i]
        init = "/".join([*head, "__init__.py"])
        probes.add(init)
        if (pkg / init).is_file():
            out.append(init)
    if parts:
        mod = "/".join(parts) + ".py"
        probes.add(mod)
        if (pkg / mod).is_file():
            out.append(mod)
    return out


def _imported(pkg: Path, rel: str, probes: set[str]) -> set[str]:
    """The package files one file imports anywhere in its body (inside functions too); a static over-approximation."""
    try:
        tree = ast.parse((pkg / rel).read_bytes(), filename=rel)
    except (OSError, SyntaxError, ValueError):
        return set()
    here = rel.split("/")[:-1]   # the module's package, relative to the package root
    out: set[str] = set()
    for node in ast.walk(tree):
        targets: list[list[str]] = []
        if isinstance(node, ast.Import):
            for a in node.names:
                ps = a.name.split(".")
                if ps[0] == "verinoda":
                    targets.append(ps[1:])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level - 1 > len(here):
                    continue
                base = here[:len(here) - (node.level - 1)] + (node.module.split(".") if node.module else [])
            elif node.module and node.module.split(".")[0] == "verinoda":
                base = node.module.split(".")[1:]
            else:
                continue
            targets.append(base)
            if pkg.joinpath(*base).is_dir():   # from a package, a name may be a submodule
                targets += [base + [a.name] for a in node.names if a.name != "*"]
        for t in targets:
            out.update(_module_file(pkg, t, probes))
    return out


_FILES_CACHE: dict[str, tuple[list[str], dict[str, tuple[int, int]], frozenset[str], list[str]]] = {}


def _stamp(pkg: Path, rels) -> dict[str, tuple[int, int]] | None:
    try:
        return {r: ((st := (pkg / r).stat()).st_mtime_ns, st.st_size) for r in rels}
    except OSError:
        return None


def engine_files(pkg: Path | None = None) -> list[str]:
    """The files the engine hash covers, relative to the package, sorted: the roots (:data:`ENGINE_ROOTS`), every
    package module they import at any depth (a static walk of ``import`` statements anywhere in each file, so
    imports inside functions count), and every ``.py`` file of the vendored graph builder; never
    :data:`ENGINE_EXCLUDED`. A dynamic import (``importlib``) is not followed."""
    pkg = pkg or PKG
    builder = pkg / GRAPH_BUILDER
    built = sorted(p.relative_to(pkg).as_posix() for p in builder.rglob("*.py")
                   if "__pycache__" not in p.parts) if builder.is_dir() else []
    hit = _FILES_CACHE.get(str(pkg))
    if hit is not None:
        files, stamps, missing, was_built = hit
        if (was_built == built and _stamp(pkg, stamps) == stamps
                and not any((pkg / m).exists() for m in missing)):
            return list(files)
    probes: set[str] = set(ENGINE_ROOTS)
    seen: set[str] = set()
    todo = [*ENGINE_ROOTS, *built]
    while todo:
        r = todo.pop()
        if r in seen or _excluded(r):
            continue
        seen.add(r)
        todo += sorted(_imported(pkg, r, probes) - seen)
    files = sorted(seen)
    stamps = _stamp(pkg, [r for r in files if (pkg / r).is_file()]) or {}
    _FILES_CACHE[str(pkg)] = (files, stamps, frozenset(p for p in probes if not (pkg / p).exists()), built)
    return list(files)


def file_sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


_ENGINE_CACHE: dict[tuple, str | None] = {}


def engine_sha(pkg: Path | None = None) -> str | None:
    """The engine hash of the sources under ``pkg`` (the installed package by default); None if a file is missing."""
    pkg = pkg or PKG
    rels = engine_files(pkg)
    stamps = _FILES_CACHE[str(pkg)][1]   # checked against the disk by engine_files just now
    if any(r not in stamps for r in rels):
        return None
    stamp = tuple((r, *stamps[r]) for r in rels)
    key = (str(pkg), stamp)
    if key not in _ENGINE_CACHE:
        try:
            lines = "".join(f"{r}\0{file_sha(pkg / r)}\n" for r in rels)
        except OSError:
            _ENGINE_CACHE[key] = None
        else:
            _ENGINE_CACHE[key] = hashlib.sha256(lines.encode("utf-8")).hexdigest()
    return _ENGINE_CACHE[key]


def answer_kind(row: dict) -> str:
    """The answer part of a cell: yes, no, ?, count, files, none, rows, invalid."""
    a = row.get("answer")
    if row.get("status") == "invalid":
        return "invalid"
    if a is None:
        return "?"
    if a is True:
        return "yes"
    if a is False:
        return "no"
    if isinstance(a, int):
        return "count"
    if isinstance(a, list):
        if row.get("type") == "which":
            return "files" if a else "none"
        return "rows"
    return "?"


def cell_key(qtype: str, answer: str, status: str) -> str:
    return f"{qtype}|{answer}|{status}"


def shown_key(qtype: str, answer: str, status: str, options: str = "") -> str:
    """The key of :func:`shown_cells`: a cell and one set of question options it was measured with."""
    return f"{cell_key(qtype, answer, status)}|{options}"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score interval of k successes in n (95 % by default); (0, 1) for n = 0."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / den
    return max(0.0, mid - half), min(1.0, mid + half)


def gold_on_disk_ok(bench: Path | None = None) -> bool:
    """In a source checkout, do the gold files still hash to :data:`GOLD_FILES`? True when they are not there."""
    bench = BENCHMARKS if bench is None else bench
    for rel, sha in GOLD_FILES.items():
        p = bench / rel
        if not p.is_file():
            if bench.is_dir() and (bench / rel.split("/")[0]).is_dir():
                return False   # the set is there without this file
            continue
        if hashlib.sha256(p.read_bytes()).hexdigest() != sha:
            return False
    return True


SCORER = "tq_gold/score.py"   # decides right and wrong; the table records its hash


def scorer_sha(bench: Path | None = None) -> str | None:
    """The sha256 (CRLF read as LF) of the scorer in a source checkout; None when it is not there."""
    p = (BENCHMARKS if bench is None else bench) / SCORER
    try:
        return file_sha(p)
    except OSError:
        return None


_TABLE_CACHE: dict[tuple, dict] = {}


def shown_cells(table: Path | None = None, *, pkg: Path | None = None, bench: Path | None = None) -> dict[str, str]:
    """``{shown key: "k/n held-out @gold8"}`` for the cells an answer may show, one key per set of options the
    cell was measured with; empty when any check fails (in a source checkout the scorer must also still have the
    hash the table records)."""
    path = Path(table) if table is not None else TABLE
    try:
        raw = path.read_bytes()
    except OSError:
        return {}
    eng, gold_ok, scorer = engine_sha(pkg), gold_on_disk_ok(bench), scorer_sha(bench)
    key = (hashlib.sha256(raw).hexdigest(), eng, gold_ok, scorer)
    if key in _TABLE_CACHE:
        return _TABLE_CACHE[key]
    out: dict[str, str] = {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        data = None
    if (isinstance(data, dict) and data.get("schema") == SCHEMA and data.get("gold_files") == GOLD_FILES
            and data.get("gold_sha") == GOLD_SHA and gold_sha(data["gold_files"]) == GOLD_SHA
            and eng is not None and (data.get("engine") or {}).get("sha") == eng and gold_ok
            and (scorer is None or data.get("score_sha") == scorer)):
        for c in data.get("cells") or []:
            if not isinstance(c, dict):
                continue
            n, k, opts = c.get("n"), c.get("right"), c.get("options")
            if (c.get("shown") is True and isinstance(n, int) and isinstance(k, int) and n >= MIN_N
                    and 0 <= k <= n and c.get("answer") not in ("?", "invalid") and isinstance(opts, list)):
                for o in opts:
                    if isinstance(o, str):
                        out[shown_key(c["type"], c["answer"], c["status"], o)] = f"{k}/{n} held-out @{GOLD_SHA[:8]}"
    _TABLE_CACHE[key] = out
    return out


def measured(qtype: str, row: dict, cells: dict[str, str] | None = None, options: str = "") -> str | None:
    """The ``measured`` value of one answer asked with ``options`` (``tq.options_of``), or None."""
    cells = shown_cells() if cells is None else cells
    if not cells or row.get("answer") is None:
        return None
    return cells.get(shown_key(qtype, answer_kind({**row, "type": qtype}), row.get("status") or "", options))
