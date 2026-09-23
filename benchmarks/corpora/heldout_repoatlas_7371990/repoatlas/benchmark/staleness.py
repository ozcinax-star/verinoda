"""Staleness harness (docs/DESIGN.md D30): history replay and a mutation suite against a from-scratch oracle.

Question-independent. It measures whether invalidation marks exactly the claims
whose truth changed:

* **History replay** (:func:`replay`) walks the last N non-merge commits of a
  corpus that touch the selected files. Git objects are only read (``git
  cat-file --batch``); each commit is replayed in a temporary directory with
  its own store. At the parent commit a claim population is generated
  mechanically for every modified file - every definition's location, every
  call through a plain name that resolves to a definition in the file or to an
  in-repo import, every environment read - and recorded through the
  production :class:`repoatlas.claims.Claims` API. The child versions are then
  written and :func:`repoatlas.claims.invalidate_stale` runs (symbol mode). The
  file-level rule of RepoAtlas <= 0.1 (any cited file changed) is the baseline.
* **Oracle**: the same population is re-derived *from scratch* at the child
  with an independent normaliser (``ast.unparse`` texts, its own name
  resolver). A claim's truth is unchanged only when an identical claim exists
  at the child - same definition and signature text, same call statement text
  resolving to the same target (which still exists), same environment read -
  and its cited text is unchanged.
* Metrics: stale recall (must be 1.0), stale precision, false-stale rate
  (stale among unchanged claims), silent-wrong (truth changed, not stale,
  status verified: must be 0), relocation accuracy of claims that only moved,
  invalidation time per commit.
* **Mutation suite** (:func:`run_mutation_suite`): twelve edit categories on a
  git copy of ``examples/orders_app`` run end to end through
  ``workflow.update``; each category has an expected verdict per claim.

``python -m repoatlas.benchmark.staleness replay --repo PATH [--commits N] [--out FILE]``
``python -m repoatlas.benchmark.staleness mutations [--out FILE]``
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from repoatlas import anchors
from repoatlas import evidence as evmod
from repoatlas.claims import VERIFIED, Claims, invalidate_stale
from repoatlas.store import Store, now

ENV_FUNCS = {("os", "getenv"), ("environ", "get"), ("os.environ", "get")}


# =============================================================================
# read-only git access
# =============================================================================

class _Git:
    """Read-only access to a repository's objects (``cat-file --batch``)."""

    def __init__(self, repo: Path):
        self.repo = Path(repo)
        self._cat: subprocess.Popen | None = None

    def run(self, *args: str) -> str:
        r = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
        return r.stdout

    def blob(self, rev: str, path: str) -> bytes | None:
        if self._cat is None:
            self._cat = subprocess.Popen(["git", "-C", str(self.repo), "cat-file", "--batch"],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        assert self._cat.stdin and self._cat.stdout
        self._cat.stdin.write(f"{rev}:{path}\n".encode("utf-8"))
        self._cat.stdin.flush()
        header = self._cat.stdout.readline().decode("utf-8", "replace")
        if header.rstrip().endswith("missing") or len(header.split()) < 3:
            return None
        size = int(header.split()[2])
        data = self._cat.stdout.read(size)
        self._cat.stdout.read(1)
        return data

    def close(self) -> None:
        if self._cat is not None:
            try:
                self._cat.stdin.close()  # type: ignore[union-attr]
                self._cat.wait(timeout=10)
            except Exception:
                self._cat.kill()


# =============================================================================
# claim population and the from-scratch oracle
# =============================================================================

@dataclass
class HClaim:
    kind: str
    key: tuple
    text: str
    path: str
    start: int
    end: int
    spec: dict = field(default_factory=dict)
    subjects: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    target: tuple | None = None  # (path, qualname) for relations


def _norm_lines(lines: list[str], a: int, b: int) -> str:
    return "\n".join(ln.rstrip() for ln in lines[a - 1: b])


def _sig_text(node: ast.AST) -> str:
    if isinstance(node, ast.ClassDef):
        parts = [node.name, *(ast.unparse(b) for b in node.bases), *(ast.unparse(k) for k in node.keywords)]
    else:
        parts = [type(node).__name__, node.name, ast.unparse(node.args), ast.unparse(node.returns)
                 if node.returns else ""]
    parts += ["@" + ast.unparse(d) for d in node.decorator_list]
    return "|".join(parts)


def _module_path(mod: str, importer: str, level: int, exists) -> str | None:
    if level:
        pkg = list(PurePosixPath(importer).parent.parts)
        pkg = pkg[: len(pkg) - (level - 1)] if level > 1 else pkg
        mod = ".".join([*pkg, *([mod] if mod else [])])
    rel = mod.replace(".", "/")
    for cand in (f"{rel}.py", f"{rel}/__init__.py"):
        if exists(cand):
            return cand
    return None


def derive(path: str, text: str, exists=lambda p: False) -> tuple[list[HClaim], dict[str, dict]]:
    """Claims derivable from one Python file, and its definitions ``qual -> {sig, start, end}``."""
    try:
        tree = anchors.parse_python(text)
    except (SyntaxError, ValueError, RecursionError):
        return [], {}
    lines = text.splitlines()
    defs: dict[str, dict] = {}
    claims: list[HClaim] = []
    parent_of: dict[int, str] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = f"{prefix}{ch.name}"
                while q in defs:
                    q += "#"
                start = min([ch.lineno] + [d.lineno for d in ch.decorator_list])
                defs[q] = {"sig": _sig_text(ch), "start": start, "end": ch.end_lineno or ch.lineno, "node": ch}
                for sub in ast.walk(ch):
                    parent_of.setdefault(id(sub), q)
                walk(ch, q + ".")
            else:
                walk(ch, prefix)

    walk(tree, "")
    # innermost def for every node: re-walk defs from the innermost
    inner: dict[int, str] = {}
    for q, d in sorted(defs.items(), key=lambda kv: (kv[1]["end"] - kv[1]["start"]), reverse=True):
        for sub in ast.walk(d["node"]):
            inner[id(sub)] = q
    # module-level bindings of names: def in file / from-import / import
    binds: dict[str, tuple[str, str] | None] = {}
    for st in tree.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            binds[st.name] = (path, st.name)
        elif isinstance(st, ast.ImportFrom):
            target = _module_path(st.module or "", path, st.level, exists)
            for a in st.names:
                if a.name != "*":
                    binds[a.asname or a.name] = (target, a.name) if target else None
        elif isinstance(st, ast.Import):
            for a in st.names:
                binds[a.asname or a.name.split(".")[0]] = None
        elif isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for t in (st.targets if isinstance(st, ast.Assign) else [st.target]):
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        binds[n.id] = None
    stars = any(isinstance(st, ast.ImportFrom) and any(a.name == "*" for a in st.names) for st in tree.body)

    for q, d in defs.items():
        a, b = d["start"], d["end"]
        claims.append(HClaim("location", ("location", path, q, d["sig"], _norm_lines(lines, a, b)),
                             f"`{q.split('.')[-1].rstrip('#')}()` is defined at {path}:{a}-{b}", path, a, b,
                             meta={"symbol": q.split(".")[-1].rstrip("#")},
                             subjects=[f"{path}::{q.split('.')[-1].rstrip('#')}()"]))

    occ: Counter = Counter()

    def local_names(fn: ast.AST) -> set[str]:
        out: set[str] = set()
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = fn.args
            out |= {x.arg for x in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg] if x}
            for n in ast.walk(fn):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    out.add(n.id)
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not fn:
                    out.add(n.name)
                elif isinstance(n, (ast.Import, ast.ImportFrom)):
                    out |= {a.asname or a.name.split(".")[0] for a in n.names}
        return out

    locals_cache: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        stmt_line = node.lineno
        encl = inner.get(id(node))
        # environment reads
        var = None
        if isinstance(f, ast.Attribute) and f.attr in ("get", "getenv") and node.args and \
                isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            recv = ast.unparse(f.value)
            if (recv, f.attr) in ENV_FUNCS:
                var = node.args[0].value
        if var:
            key = ("config", path, encl, var, lines[stmt_line - 1].strip())
            occ[key] += 1
            claims.append(HClaim("config", key + (occ[key],),
                                 f"`{(encl or 'module').split('.')[-1]}` reads environment variable {var} "
                                 f"({path}:{stmt_line})", path, stmt_line, stmt_line, spec={"env": var},
                                 subjects=[path], meta={"env": var}))
            continue
        if not isinstance(f, ast.Name) or encl is None or stars:
            continue
        n = f.id
        if encl not in locals_cache:
            locals_cache[encl] = local_names(defs[encl]["node"])
        if n in locals_cache[encl]:
            continue
        tgt = binds.get(n)
        if not tgt or not tgt[0]:
            continue
        caller = encl
        key = ("relation", path, caller, n, tgt, lines[stmt_line - 1].strip())
        occ[key] += 1
        label = f"{tgt[1]}()"
        claims.append(HClaim("relation", key + (occ[key],),
                             f"`{caller.split('.')[-1]}()` calls `{label}` ({path}:{stmt_line})", path, stmt_line,
                             stmt_line, spec={"target_label": label, "relation": "calls", "at": f"{path}:{stmt_line}",
                                              "confidence": "EXTRACTED"},
                             subjects=[f"{path}::{caller.split('.')[-1]}()", f"{tgt[0]}::{label}"], target=tgt))
    for d in defs.values():
        d.pop("node", None)
    return claims, defs


def _truth_unchanged(c: HClaim, child: dict[str, set], child_defs: dict[str, dict[str, dict]]) -> bool:
    if c.key not in child.get(c.path, set()):
        return False
    if c.kind == "relation" and c.target:
        tpath, tname = c.target
        tdefs = child_defs.get(tpath)
        if tdefs is None:  # target file absent at the child
            return False
        if not any(q.split(".")[-1].rstrip("#") == tname for q in tdefs):
            return False
    return True


# =============================================================================
# history replay
# =============================================================================

def _wilson(k: int, n: int, z: float = 1.96) -> list[float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
    return [round((c - r) / d, 4), round((c + r) / d, 4)]


def _rates(stale: set, changed: set, universe: set) -> dict:
    unchanged = universe - changed
    tp = len(stale & changed)
    return {"stale": len(stale), "recall": round(tp / len(changed), 4) if changed else 1.0,
            "precision": round(tp / len(stale), 4) if stale else 1.0,
            "false_stale_rate": round(len(stale & unchanged) / len(unchanged), 4) if unchanged else 0.0,
            "missed": len(changed - stale)}


def _sample(claims: list[HClaim], cap: int | None) -> list[HClaim]:
    if not cap:
        return claims
    by_kind: dict[str, list[HClaim]] = defaultdict(list)
    for c in claims:
        by_kind[c.kind].append(c)
    out: list[HClaim] = []
    for kind, cs in sorted(by_kind.items()):
        step = max(1, len(cs) // cap)
        out += cs[::step][:cap]
    return out


def replay(corpus: Path, *, commits: int = 300, pathspec: str = "*.py", cap_per_kind: int | None = 40,
           workdir: Path | None = None, progress=None) -> dict:
    """Replay the last ``commits`` non-merge commits touching ``pathspec`` (see the module docstring)."""
    corpus = Path(corpus).resolve()
    git = _Git(corpus)
    shas = git.run("log", "--no-merges", f"-n{commits}", "--format=%H", "--", pathspec).split()
    base = Path(workdir or tempfile.mkdtemp(prefix="ra_staleness_"))
    base.mkdir(parents=True, exist_ok=True)
    per_kind: dict[str, dict[str, set]] = defaultdict(lambda: {"all": set(), "changed": set(), "file": set(),
                                                               "sym": set(), "verified": set()})
    reloc = Counter()
    t_inval: list[float] = []
    t_create: list[float] = []
    files_seen = 0
    events = 0
    rebound_total = 0
    silent: list[dict] = []
    false_stale: list[dict] = []
    tree_cache: dict[str, set[str]] = {}

    def exists_at(rev: str):
        if rev not in tree_cache:
            tree_cache[rev] = set(git.run("ls-tree", "-r", "--name-only", rev).split("\n"))
        return lambda p: p in tree_cache[rev]

    try:
        for i, sha in enumerate(shas):
            try:
                diff = git.run("diff-tree", "--no-commit-id", "-r", "--name-status", "-M0", f"{sha}^", sha,
                               "--", pathspec)
            except RuntimeError:
                continue  # root commit
            changes = [(ln.split("\t")[0][0], ln.split("\t")[-1]) for ln in diff.splitlines() if "\t" in ln]
            changes = [(s, p) for s, p in changes if s in "MD" and p.endswith(".py")]
            if not changes:
                continue
            events += 1
            parent_exists, child_exists = exists_at(f"{sha}^"), exists_at(sha)
            work = base / f"c{i:04d}"
            if work.exists():
                shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True)
            parent_text: dict[str, bytes] = {}
            child_text: dict[str, bytes | None] = {}
            population: list[HClaim] = []
            for status, p in changes:
                a = git.blob(f"{sha}^", p)
                if a is None:
                    continue
                parent_text[p] = a
                child_text[p] = git.blob(sha, p) if status == "M" else None
                cl_, _ = derive(p, a.decode("utf-8", "replace"), parent_exists)
                population += _sample(cl_, cap_per_kind)
                files_seen += 1
            targets = {c.target[0] for c in population if c.kind == "relation" and c.target} - set(parent_text)
            for t in sorted(targets):
                a = git.blob(f"{sha}^", t)
                if a is not None:
                    parent_text[t] = a
                    child_text[t] = git.blob(sha, t)
            for p, data in parent_text.items():
                (work / p).parent.mkdir(parents=True, exist_ok=True)
                (work / p).write_bytes(data)
            st = Store(":memory:")
            try:
                snap_a = {"id": f"snpA{i}", "repo_root": str(work), "project": corpus.name, "commit_sha": f"{sha}^",
                          "branch": None, "dirty": 0, "tree_hash": "a", "file_count": len(parent_text),
                          "created_at": now()}
                st.add_snapshot(snap_a, {p: hashlib.sha256(d).hexdigest() for p, d in parent_text.items()})
                cl = Claims(st, work)
                ids: dict[str, HClaim] = {}
                t0 = time.perf_counter()
                for c in population:
                    ev = evmod.source_evidence(work, c.path, c.start, c.end, commit=f"{sha}^", meta=dict(c.meta))
                    if ev is None:
                        continue
                    rec = cl.create(c.text, project=corpus.name, snapshot=snap_a, status="statically_verified",
                                    evidence=[(ev, "supports")], kind=c.kind, spec=c.spec, subjects=c.subjects)
                    ids[rec["id"]] = c
                t_create.append(time.perf_counter() - t0)
                verified = {cid for cid in ids if cl.get(cid)["status"] in VERIFIED}
                # child versions
                for p, data in child_text.items():
                    if data is None:
                        (work / p).unlink(missing_ok=True)
                    else:
                        (work / p).write_bytes(data)
                snap_b = {**snap_a, "id": f"snpB{i}", "commit_sha": sha, "tree_hash": "b",
                          "file_count": sum(1 for d in child_text.values() if d is not None)}
                st.add_snapshot(snap_b, {p: hashlib.sha256(d).hexdigest() for p, d in child_text.items()
                                         if d is not None})
                report: dict = {}
                t0 = time.perf_counter()
                stale_ids = {s["id"] for s in invalidate_stale(st, snap_b, repo=work, report=report)}
                t_inval.append(time.perf_counter() - t0)
                rebound_total += report.get("rebound", 0)
                # oracle: re-derive the changed files from scratch at the child
                child_keys: dict[str, set] = {}
                child_defs: dict[str, dict[str, dict]] = {}
                child_claims: dict[str, dict[tuple, HClaim]] = {}
                for p, data in child_text.items():
                    if data is None:
                        continue
                    cs, defs = derive(p, data.decode("utf-8", "replace"), child_exists)
                    child_keys[p] = {c.key for c in cs}
                    child_defs[p] = defs
                    child_claims[p] = {c.key: c for c in cs}
                changed_files = {p for p in child_text if child_text[p] != parent_text[p]}
                for cid, c in ids.items():
                    pk = per_kind[c.kind]
                    uid = f"{i}:{cid}"
                    pk["all"].add(uid)
                    if cid in verified:
                        pk["verified"].add(uid)
                    unchanged = c.path not in changed_files or _truth_unchanged(c, child_keys, {
                        **{p: child_defs.get(p) for p in child_text if child_text[p] is not None},
                        **{p: None for p in child_text if child_text[p] is None}})
                    if c.kind == "relation" and c.target and c.target[0] in changed_files and c.path not in \
                            changed_files:
                        unchanged = _truth_unchanged(c, {c.path: {c.key}}, {
                            c.target[0]: child_defs.get(c.target[0])})
                    if not unchanged:
                        pk["changed"].add(uid)
                    files_of = {c.path} | ({c.target[0]} if c.kind == "relation" and c.target else set())
                    if files_of & changed_files:
                        pk["file"].add(uid)
                    if cid in stale_ids:
                        pk["sym"].add(uid)
                        if unchanged and len(false_stale) < 40:
                            h = st.history(cid)[-1]
                            false_stale.append({"commit": sha[:12], "kind": c.kind, "claim": c.text[:120],
                                                "why": [f.get("dep") or f.get("why") for f in
                                                        (h["payload"] or {}).get("facets", [])][:3]})
                    elif not unchanged and cid in verified:
                        silent.append({"commit": sha[:12], "claim": c.text, "kind": c.kind})
                    # relocation of claims that only moved
                    if unchanged and cid not in stale_ids and c.path in changed_files:
                        new = child_claims.get(c.path, {}).get(c.key)
                        if new is not None and new.start != c.start:
                            e = cl.evidence(cid)[0]
                            chk = evmod.check_source(work, e)
                            reloc["moved"] += 1
                            reloc["exact"] += int(bool(chk.ok and chk.moved_to and chk.moved_to[0] == new.start))
            finally:
                st.close()
                shutil.rmtree(work, ignore_errors=True)
            if progress:
                progress(i + 1, len(shas), sha)
    finally:
        git.close()
        if workdir is None:
            shutil.rmtree(base, ignore_errors=True)

    def summarize_kind(pk: dict) -> dict:
        return {"claims": len(pk["all"]), "truth_changed": len(pk["changed"]),
                "verified_at_parent": len(pk["verified"]),
                "file_mode": _rates(pk["file"], pk["changed"], pk["all"]),
                "symbol_mode": _rates(pk["sym"], pk["changed"], pk["all"])}

    total = {"all": set(), "changed": set(), "file": set(), "sym": set(), "verified": set()}
    for pk in per_kind.values():
        for k in total:
            total[k] |= pk[k]
    t_sorted = sorted(t_inval)
    out = {
        "schema": "repoatlas.staleness_replay/1", "corpus": corpus.name, "pathspec": pathspec,
        "commits_requested": commits, "commits_replayed": events, "file_versions": files_seen,
        "cap_per_kind_per_file": cap_per_kind,
        "overall": summarize_kind(total),
        "by_kind": {k: summarize_kind(v) for k, v in sorted(per_kind.items())},
        "silent_wrong": len(silent), "silent_wrong_examples": silent[:10],
        "false_stale_examples": false_stale[:15],
        "rebound": rebound_total,
        "relocation": {"moved": reloc["moved"], "exact": reloc["exact"],
                       "accuracy": round(reloc["exact"] / reloc["moved"], 4) if reloc["moved"] else None,
                       "wilson95": _wilson(reloc["exact"], reloc["moved"])},
        "invalidation_ms": {"p50": round(1000 * t_sorted[len(t_sorted) // 2], 1) if t_sorted else None,
                            "p95": round(1000 * t_sorted[int(len(t_sorted) * 0.95)], 1) if t_sorted else None,
                            "max": round(1000 * t_sorted[-1], 1) if t_sorted else None},
        "claim_recording_s_per_commit": round(sum(t_create) / len(t_create), 3) if t_create else None,
    }
    out["recall_ok"] = out["overall"]["symbol_mode"]["recall"] == 1.0
    out["silent_wrong_ok"] = out["silent_wrong"] == 0
    return out


# =============================================================================
# mutation suite
# =============================================================================

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "orders_app"

# (category, edit, expected: claim name -> "stale" | "kept")
MUTATIONS: list[tuple[str, list[tuple[str, str, str]], dict[str, str]]] = [
    ("whitespace_comment", [("orders/pricing.py", "    subtotal = sum(", "    # add them up\n    subtotal = sum(")],
     {"loc_compute": "stale", "rel_compute_apply": "kept", "loc_apply": "kept", "rel_place_compute": "kept"}),
    ("shift_above", [("orders/pricing.py", '"""Pricing rules."""\n', '"""Pricing rules."""\n\n# pricing\n')],
     {"loc_compute": "kept", "rel_compute_apply": "kept", "loc_apply": "kept", "rel_place_compute": "kept"}),
    ("unrelated_def_edit", [("orders/pricing.py", "round(subtotal * 0.9, 2)", "round(subtotal * 0.85, 2)")],
     {"loc_compute": "kept", "rel_compute_apply": "kept", "loc_apply": "stale", "rel_place_compute": "kept"}),
    ("docstring_edit", [("orders/pricing.py", "def compute_total(items: list[dict]) -> float:\n",
                         'def compute_total(items: list[dict]) -> float:\n    """Total of an order."""\n')],
     {"loc_compute": "stale", "rel_compute_apply": "kept", "rel_place_compute": "kept"}),
    ("local_rename_in_cited_def", [("orders/pricing.py", "    subtotal = sum(", "    sub = sum("),
                                   ("orders/pricing.py", "return apply_discount(subtotal)",
                                    "return apply_discount(sub)")],
     {"loc_compute": "stale", "rel_compute_apply": "stale", "loc_apply": "kept", "rel_place_compute": "kept"}),
    ("delete_the_call", [("orders/pricing.py", "return apply_discount(subtotal)", "return subtotal")],
     {"rel_compute_apply": "stale", "loc_apply": "kept"}),
    ("rename_the_target", [("orders/pricing.py", "apply_discount", "apply_rebate")],
     {"rel_compute_apply": "stale", "loc_apply": "stale", "rel_place_compute": "kept"}),
    ("change_the_import_alias", [("orders/service.py", "from orders.pricing import compute_total\n",
                                  "from orders.pricing import apply_discount as compute_total\n")],
     {"rel_place_compute": "stale", "loc_compute": "kept"}),
    ("move_def_to_another_file", [("orders/pricing.py", "def compute_total(items: list[dict]) -> float:\n"
                                   "    subtotal = sum(i[\"price\"] * i[\"qty\"] for i in items)\n"
                                   "    return apply_discount(subtotal)\n",
                                   "from orders.totals import compute_total  # noqa\n"),
                                  ("orders/totals.py", None, "from orders.pricing import apply_discount\n\n\n"
                                   "def compute_total(items: list[dict]) -> float:\n"
                                   "    subtotal = sum(i[\"price\"] * i[\"qty\"] for i in items)\n"
                                   "    return apply_discount(subtotal)\n")],
     {"loc_compute": "stale", "rel_compute_apply": "stale", "loc_apply": "kept", "rel_place_compute": "stale"}),
    ("delete_the_file", [("orders/repository.py", None, None)], {"loc_save": "stale", "loc_compute": "kept"}),
    ("add_same_name_symbol", [("orders/audit_log.py", None, "def compute_total(entries):\n    return len(entries)\n")],
     {"rel_place_compute": "kept", "loc_compute": "kept", "no_test_watch": "kept"}),
    ("add_a_test", [("tests/test_new_probe.py", None, "def test_probe():\n    assert True\n")],
     {"no_test_watch": "stale", "loc_compute": "kept", "rel_compute_apply": "kept"}),
]


def _git_init(repo: Path) -> None:
    for args in (("init", "-q"), ("add", "-A"), ("commit", "-q", "-m", "init")):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                       cwd=repo, check=True, capture_output=True, stdin=subprocess.DEVNULL)


def _mutation_claims(repo: Path, st: Store) -> dict[str, str]:
    snap = st.latest_snapshot()
    cl = Claims(st, repo)

    def line(rel: str, needle: str) -> int:
        return next(i for i, t in enumerate((repo / rel).read_text(encoding="utf-8").splitlines(), 1) if needle in t)

    def loc(sym: str, rel: str, needle: str, n: int) -> str:
        a = line(rel, needle)
        ev = evmod.source_evidence(repo, rel, a, a + n - 1, commit=snap["commit_sha"], meta={"symbol": sym})
        return cl.create(f"`{sym}` is defined at {rel}:{a}-{a + n - 1}", project=snap["project"], snapshot=snap,
                         status="statically_verified", evidence=[(ev, "supports")], kind="location",
                         subjects=[f"{rel}::{sym}"])["id"]

    def rel_(caller: str, target: str, rel: str, needle: str, tfile: str) -> str:
        ln = line(rel, needle)
        ev = evmod.source_evidence(repo, rel, ln, commit=snap["commit_sha"])
        return cl.create(f"`{caller}` calls `{target}` ({rel}:{ln})", project=snap["project"], snapshot=snap,
                         status="statically_verified", evidence=[(ev, "supports")], kind="relation",
                         subjects=[f"{rel}::{caller}", f"{tfile}::{target}"],
                         spec={"target_label": target, "relation": "calls", "at": f"{rel}:{ln}",
                               "confidence": "EXTRACTED"})["id"]

    return {
        "loc_compute": loc("compute_total()", "orders/pricing.py", "def compute_total", 3),
        "loc_apply": loc("apply_discount()", "orders/pricing.py", "def apply_discount", 5),
        "loc_save": loc(".save()", "orders/repository.py", "def save(", 6),
        "rel_compute_apply": rel_("compute_total()", "apply_discount()", "orders/pricing.py",
                                  "return apply_discount(subtotal)", "orders/pricing.py"),
        "rel_place_compute": rel_("place_order()", "compute_total()", "orders/service.py",
                                  "total = compute_total(items)", "orders/pricing.py"),
        "no_test_watch": cl.create("No test statically reaches `get_order_handler`", project=snap["project"],
                                   snapshot=snap, status="weak_inference", kind="tests",
                                   subjects=["orders/api.py::get_order_handler()"], spec={"watch": "tests"})["id"],
    }


def run_mutation_suite(example: Path | None = None, workdir: Path | None = None) -> dict:
    """Apply each mutation to a fresh git copy of the example and compare verdicts with the table."""
    from repoatlas import workflow
    from repoatlas.store import open_store

    example = Path(example or EXAMPLE)
    base = Path(workdir or tempfile.mkdtemp(prefix="ra_mutations_"))
    rows = []
    try:
        for n, (category, edits, expected) in enumerate(MUTATIONS):
            repo = base / f"m{n:02d}" / "orders_app"
            shutil.copytree(example, repo, ignore=shutil.ignore_patterns(".repoatlas", "__pycache__", "*.pyc",
                                                                         ".pytest_cache", "*.db"))
            _git_init(repo)
            st = open_store(repo)
            try:
                workflow.scan(st, repo)
                ids = _mutation_claims(repo, st)
                statuses_before = {k: Claims(st, repo).get(v)["status"] for k, v in ids.items()}
                for rel, old, new in edits:
                    p = repo / rel
                    if old is None and new is None:
                        p.unlink()
                    elif old is None:
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(new.encode("utf-8"))
                    else:
                        text = p.read_bytes().decode("utf-8")
                        assert old in text, (category, old)
                        p.write_bytes(text.replace(old, new).encode("utf-8"))
                t0 = time.perf_counter()
                res = workflow.update(st, repo)
                ms = round(1000 * (time.perf_counter() - t0), 1)
                stale = {s["id"] for s in res["stale"]}
                got = {k: ("stale" if ids[k] in stale else "kept") for k in expected}
                rows.append({"category": category, "expected": expected, "got": got, "ok": got == expected,
                             "update_ms": ms, "statuses_before": {k: statuses_before[k] for k in expected}})
            finally:
                st.close()
    finally:
        if workdir is None:
            shutil.rmtree(base, ignore_errors=True)
    total = sum(len(r["expected"]) for r in rows)
    agree = sum(sum(1 for k in r["expected"] if r["got"][k] == r["expected"][k]) for r in rows)
    must_stale = [(r["category"], k) for r in rows for k, v in r["expected"].items() if v == "stale"]
    missed = [(r["category"], k) for r in rows for k, v in r["expected"].items() if v == "stale" and r["got"][k] != v]
    return {"schema": "repoatlas.staleness_mutations/1", "categories": len(rows), "verdicts": total,
            "agree": agree, "all_ok": all(r["ok"] for r in rows),
            "stale_recall": round(1 - len(missed) / len(must_stale), 4) if must_stale else 1.0,
            "missed": missed, "rows": rows}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="python -m repoatlas.benchmark.staleness")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replay", help="history replay against the from-scratch oracle")
    r.add_argument("--repo", required=True)
    r.add_argument("--commits", type=int, default=300)
    r.add_argument("--pathspec", default="*.py")
    r.add_argument("--cap", type=int, default=40, help="claims per kind per file (0 = all)")
    r.add_argument("--out")
    m = sub.add_parser("mutations", help="the mutation suite on examples/orders_app")
    m.add_argument("--out")
    args = p.parse_args(argv)
    if args.cmd == "replay":
        def progress(i: int, n: int, sha: str) -> None:
            if i % 10 == 0 or i == n:
                print(f"[staleness] {i}/{n} {sha[:10]}", file=sys.stderr, flush=True)

        res = replay(Path(args.repo), commits=args.commits, pathspec=args.pathspec, cap_per_kind=args.cap or None,
                     progress=progress)
    else:
        res = run_mutation_suite()
    text = json.dumps(res, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_bytes((text + "\n").encode("utf-8"))
    print(text)
    return 0 if res.get("recall_ok", res.get("all_ok", True)) and res.get("silent_wrong_ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
