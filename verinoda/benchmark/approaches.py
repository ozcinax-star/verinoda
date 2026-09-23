"""The compared approaches. Each returns the exact text a model would be given.

raw                 deterministic simulation of an agent that greps the question
                    terms (same term extraction as Verinoda) and reads the
                    top-ranked files in full, with line numbers, up to a char cap.
graphify_vendored   Graphify's query renderer at the pinned upstream commit
                    (``verinoda.index.graphify_query_text``: MCP ``query_graph``
                    defaults, BFS depth 3, 2000-token budget) over Verinoda's
                    Graphify-derived index.
graphify_cli        the real upstream ``graphify query`` (BFS depth 2, 2000-token
                    budget) in a separate copy indexed with ``graphify update .``.
verinoda_analyze   ``verinoda.analysis.analyze``: compact JSON of claims + unknowns.
verinoda_retrieve  ``verinoda.retrieval.retrieve``: bounded, justified excerpts (JSON).
verinoda_retrieve_text
                    the same retrieval rendered for a model with
                    ``retrieval.render_text`` (skeleton first, call outlines),
                    the default output of ``verinoda query``.

Budget sweep: ``<approach>@<tokens>`` runs a sweepable approach
(:data:`SWEEP_BASES`) with a context cap of ``4 * tokens`` characters, i.e.
``tokens`` in the benchmark's chars/4 estimate, so every approach gets the same
number of characters. Graphify's own budget counts 3 chars per token, so it is
given ``ceil(4 * tokens / 3)`` Graphify tokens; raw gets ``char_cap = 4 * tokens``;
retrieve_text gets ``Budget(10, 4 * tokens)`` and ``render_text(..., 4 * tokens)``.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from fnmatch import fnmatch
from pathlib import Path

RAW_TOP_FILES = 5
RAW_CHAR_CAP = 24000
RAW_GREP_LINES = 40
RAW_MIN_TERM = 3

APPROACHES = ("raw", "graphify_vendored", "graphify_cli", "verinoda_analyze", "verinoda_retrieve",
              "verinoda_retrieve_text")
# Approaches that take a context budget, in the order the sweep reports them.
SWEEP_BASES = ("verinoda_retrieve_text", "graphify_vendored", "graphify_cli", "raw")
DEFAULT_SWEEP = (750, 1500, 3000)
CHARS_PER_TOKEN = 4          # the benchmark's own estimate (metrics.count_tokens without tiktoken)
GRAPHIFY_CHARS_PER_TOKEN = 3  # Graphify's renderer: char_budget = token_budget * 3


def split_approach(name: str) -> tuple[str, int | None]:
    """``"raw@750"`` -> ``("raw", 750)``; ``"raw"`` -> ``("raw", None)``."""
    base, sep, tok = name.partition("@")
    if not sep:
        return name, None
    if base not in SWEEP_BASES or not tok.isdigit() or int(tok) <= 0:
        raise ValueError(f"not a sweepable approach: {name!r}")
    return base, int(tok)


def sweep_chars(tokens: int) -> int:
    """Character cap for a sweep point of ``tokens`` (chars/4 tokens)."""
    return CHARS_PER_TOKEN * int(tokens)


def graphify_budget(tokens: int) -> int:
    """Graphify ``token_budget`` whose character cap equals :func:`sweep_chars` (Graphify counts 3 chars/token)."""
    return math.ceil(sweep_chars(tokens) / GRAPHIFY_CHARS_PER_TOKEN)


# -- corpus preparation --------------------------------------------------------

def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _selected(rel: str, include: list[str] | None, exclude: list[str] | None) -> bool:
    if include and not any(rel == i.rstrip("/") or rel.startswith(i.rstrip("/") + "/") for i in include):
        return False
    return not any(fnmatch(rel, pat) for pat in exclude or [])


def _safe_rel(name: str) -> str | None:
    """A repo-relative POSIX path from an archive member, or None if it could escape ``dest``."""
    rel = name.replace("\\", "/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel) or ".." in parts:
        return None
    return "/".join(parts)


def export_commit(source: Path, commit: str, dest: Path, *, include: list[str] | None = None,
                  exclude: list[str] | None = None) -> tuple[list[str], str]:
    """Write the files of ``commit`` (``git archive``, bytes as committed) into ``dest``.

    Returns ``(files, full_sha)``. The source repository is only read; its
    working tree is not used, so the corpus cannot move when the checkout does.
    """
    import io
    import tarfile

    r = _git(Path(source), "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
    sha = (r.stdout or "").strip() if r is not None and r.returncode == 0 else ""
    if not sha:
        raise ValueError(f"commit {commit!r} not found in {source}")
    try:
        # -c core.autocrlf=false: the archive carries the committed bytes on every platform.
        data = subprocess.run(["git", "-c", "core.autocrlf=false", "archive", "--format=tar", sha],
                              cwd=str(source), capture_output=True, timeout=600, check=True).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"git archive {sha[:12]} failed in {source}: {exc}") from exc
    files: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        for m in tar.getmembers():
            rel = _safe_rel(m.name)
            if rel is None or not m.isfile() or not _selected(rel, include, exclude):
                continue
            fh = tar.extractfile(m)
            if fh is None:
                continue
            dst = Path(dest) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(fh.read())
            files.append(rel)
    return sorted(files), sha


def prepare_workdir(source: Path, dest: Path, *, include: list[str] | None = None,
                    exclude: list[str] | None = None, commit: str | None = None) -> dict:
    """Copy the corpus (tracked + untracked-not-ignored files) and commit it in a fresh repo.

    The source is only read. ``include`` limits the copy to path prefixes,
    ``exclude`` drops fnmatch globs. With ``commit`` the files come from that
    commit of ``source`` (``git archive``) instead of its working tree, so a
    question set can pin a corpus that does not move.
    """
    from verinoda.snapshot import git_info, list_files

    source, dest = Path(source).resolve(), Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError(f"benchmark workdir {dest} is not empty")
    if commit:
        dest.mkdir(parents=True, exist_ok=True)
        files, pinned = export_commit(source, commit, dest, include=include, exclude=exclude)
    else:
        files, pinned = [r for r in list_files(source) if _selected(r, include, exclude)], None
    if not files:
        raise FileNotFoundError(f"no files selected from {source} (include={include})")
    total = 0
    for rel in files:
        dst = dest / rel
        if not pinned:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / rel, dst)
        total += dst.stat().st_size
    src_git = git_info(source)
    wd_commit = None
    if _git(dest, "init", "-q") is not None:
        _git(dest, "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "add", "-A")
        r = _git(dest, "-c", "user.name=verinoda-bench", "-c", "user.email=bench@localhost",
                 "-c", "core.autocrlf=false", "commit", "-q", "-m", "benchmark corpus")
        if r is not None and r.returncode == 0:
            wd_commit = (_git(dest, "rev-parse", "HEAD").stdout or "").strip() or None
    return {"source": str(source), "source_commit": pinned or src_git.get("commit"),
            "source_dirty": False if pinned else src_git.get("dirty"),
            "snapshot": f"git archive {pinned}" if pinned else "working tree (tracked + untracked, not ignored)",
            "include": include, "exclude": exclude, "files": files, "file_count": len(files),
            "bytes": total, "workdir_commit": wd_commit}


def remove_tree(p: Path) -> None:
    def _fix(func, path, _exc):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass

    if not Path(p).exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(p, onexc=_fix)
    else:  # pragma: no cover - older Pythons
        shutil.rmtree(p, onerror=_fix)


def count_files(p: Path) -> int:
    return sum(1 for x in Path(p).rglob("*") if x.is_file()) if Path(p).exists() else 0


@contextmanager
def _cwd(p: Path):
    old = os.getcwd()
    os.chdir(p)
    try:
        yield
    finally:
        os.chdir(old)


# -- 1. raw search + read ---------------------------------------------------------

def _read_text(root: Path, rel: str) -> str | None:
    try:
        data = (root / rel).read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def raw_context(root: Path, question: str, *, top_files: int = RAW_TOP_FILES, char_cap: int = RAW_CHAR_CAP,
                grep_lines: int = RAW_GREP_LINES) -> tuple[str, dict]:
    """grep the question terms, then read the best files in full (numbered) up to ``char_cap``.

    Ranking: files matching more distinct terms first, then more matching
    lines, then path. Deterministic for a given corpus and question.
    """
    from verinoda.retrieval import terms_for
    from verinoda.snapshot import list_files

    root = Path(root)
    terms = terms_for(question)
    search = [t for t in terms if len(t) >= RAW_MIN_TERM] or terms
    rxs = [re.compile(re.escape(t), re.I) for t in search]
    ranked = []
    texts: dict[str, list[str]] = {}
    for rel in list_files(root):
        text = _read_text(root, rel)
        if text is None:
            continue
        lines = text.splitlines()
        hit_terms: set[str] = set()
        hits = []
        for i, line in enumerate(lines, 1):
            got = {t for t, rx in zip(search, rxs) if rx.search(line)}
            if got:
                hit_terms |= got
                hits.append((i, line))
        if hits:
            texts[rel] = lines
            ranked.append((-len(hit_terms), -len(hits), rel, hits))
    ranked.sort()
    total_hits = sum(len(r[3]) for r in ranked)
    out = [f"$ grep -rniE '{'|'.join(search)}' .   # {total_hits} matching lines in {len(ranked)} files"]
    used = len(out[0]) + 1
    shown, full = 0, False
    for _, _, rel, hits in ranked:
        for i, line in hits:
            row = f"{rel}:{i}: {line.strip()[:160]}"
            # The grep listing is part of the delivered context, so it obeys the cap too.
            if shown >= grep_lines or used + len(row) + 1 + 60 > char_cap:
                full = True
                break
            out.append(row)
            used += len(row) + 1
            shown += 1
        if full:
            break
    if shown < total_hits:
        note = f"... ({total_hits - shown} more matching lines not shown)"
        out.append(note)
        used += len(note) + 1
    read, truncated = [], False
    for _, _, rel, _ in ranked[:top_files]:
        lines = texts[rel]
        reserve = len(f"==> {rel}:1-{len(lines)} <==  (truncated at char cap; file has {len(lines)} lines)") + 2
        body, last = [], 0
        for i, line in enumerate(lines, 1):
            row = f"{i:>6}\t{line}"
            if used + reserve + len(row) + 1 > char_cap:
                truncated = True
                break
            body.append(row)
            used += len(row) + 1
            last = i
        if last == 0:
            truncated = True
            break
        header = f"==> {rel}:1-{last} <==" + ("" if last == len(lines) else f"  (truncated at char cap; file has {len(lines)} lines)")
        out.append("")
        out.append(header)
        out.extend(body)
        used += len(header) + 2
        read.append({"file": rel, "lines": [1, last], "of": len(lines)})
        if truncated:
            break
    meta = {"terms": search, "matching_files": len(ranked), "matching_lines": total_hits,
            "files_read": read, "truncated": truncated, "agent_tool_calls": 1 + len(read),
            "params": {"top_files": top_files, "char_cap": char_cap, "grep_lines": grep_lines}}
    return "\n".join(out), meta


# -- 2. Graphify (vendored renderer and real upstream CLI) ----------------------------

def graphify_vendored_context(root: Path, question: str, budget: int = 2000) -> tuple[str, dict]:
    from verinoda import index

    # Run from the corpus root so the header names the graph relative to it,
    # exactly as the upstream CLI does when run inside a project.
    with _cwd(Path(root)):
        text = index.graphify_query_text(Path(root), question, budget=budget)
    return text, {"agent_tool_calls": 1, "budget_tokens": budget, "depth": 3, **_graphify_stats(text)}


def _graphify_stats(text: str) -> dict:
    conf: dict[str, int] = {}
    for m in re.finditer(r"^EDGE .*?\[(\w+)", text or "", re.M):
        conf[m.group(1)] = conf.get(m.group(1), 0) + 1
    nodes = len(re.findall(r"^NODE ", text or "", re.M))
    trunc = re.search(r"TRUNCATED: showing (\d+) of (\d+) nodes", text or "")
    found = re.search(r"\| (\d+) nodes found", text or "")
    return {"nodes_shown": nodes, "edges_shown": sum(conf.values()), "edge_confidence": conf,
            "truncated": bool(trunc), "nodes_found": int(found.group(1)) if found else nodes}


def resolve_cmd(cmd: str) -> str | None:
    p = Path(cmd)
    if p.exists():
        return str(p.resolve())
    return shutil.which(cmd)


def cli_env() -> dict:
    """Environment for the upstream CLI: its default output dir, no model keys, no query log."""
    env = dict(os.environ)
    env.pop("GRAPHIFY_OUT", None)  # Verinoda points this at .verinoda/index; upstream must use graphify-out
    for k in list(env):
        if k.endswith(("_API_KEY", "_AUTH_TOKEN")):
            env.pop(k)
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", GRAPHIFY_QUERY_LOG_DISABLE="1")
    return env


def _run(argv: list[str], cwd: Path, timeout: float = 900) -> tuple[subprocess.CompletedProcess | None, float, str | None]:
    t0 = time.perf_counter()
    try:
        r = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=cli_env(), timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, time.perf_counter() - t0, str(exc)
    return r, time.perf_counter() - t0, None


def _stderr_summary(err: str) -> dict:
    lines = [l for l in (err or "").splitlines() if l.strip()]
    skill_warn = [l for l in lines if "skill at" in l and "is from graphify" in l]
    other = [l for l in lines if l not in skill_warn]
    return {"lines": len(lines), "skill_version_warnings": len(skill_warn), "other": [l[:200] for l in other[:5]]}


def graphify_cli_version(cmd: str, cwd: Path) -> str | None:
    r, _, _ = _run([cmd, "--version"], cwd, timeout=120)
    return (r.stdout or "").strip() if r is not None and r.returncode == 0 else None


def graphify_cli_update(cmd: str, root: Path) -> dict:
    """``graphify update .`` (AST-only, no LLM). Returns timing and what it built."""
    r, dt, err = _run([cmd, "update", "."], Path(root))
    res = {"seconds": round(dt, 3), "ok": r is not None and r.returncode == 0, "error": err}
    if r is not None:
        m = re.search(r"Rebuilt: (\d+) nodes, (\d+) edges", r.stdout or "")
        res.update(returncode=r.returncode, nodes=int(m.group(1)) if m else None, edges=int(m.group(2)) if m else None,
                   log=[l for l in (r.stdout or "").splitlines() if l.strip()][:6],
                   stderr=_stderr_summary(r.stderr))
    res["cache_files"] = count_files(Path(root) / "graphify-out" / "cache")
    gp = Path(root) / "graphify-out" / "graph.json"
    if gp.exists() and res.get("nodes") is None:
        try:
            data = json.loads(gp.read_text(encoding="utf-8"))
            res["nodes"] = len(data.get("nodes", []))
            res["edges"] = len(data.get("links", data.get("edges", [])))
        except (OSError, ValueError):
            pass
    return res


def graphify_cli_context(cmd: str, root: Path, question: str, budget: int | None = None) -> tuple[str, dict]:
    """``graphify query "<question>"`` (CLI defaults), or with ``--budget N`` when ``budget`` is given."""
    argv = [cmd, "query", question] + (["--budget", str(int(budget))] if budget is not None else [])
    r, dt, err = _run(argv, Path(root), timeout=600)
    if r is None or r.returncode != 0:
        detail = err if r is None else (r.stderr or "")[-500:]
        raise RuntimeError(f"graphify query failed: {detail}")
    return r.stdout or "", {"agent_tool_calls": 1, "budget_tokens": 2000 if budget is None else int(budget),
                            "depth": 2, "stderr": _stderr_summary(r.stderr), **_graphify_stats(r.stdout)}


# -- 3. Verinoda ---------------------------------------------------------------------

def analyze_context(res: dict) -> str:
    """What an agent receives from ``verinoda analyze --json``, minus run bookkeeping."""
    claims = []
    for c in res["claims"]:
        claims.append({k: c[k] for k in ("id", "text", "status", "confidence", "evidence", "uncertainties", "challenged")
                       if k in c})
    body = {"question": res["question"], "intents": res["intents"], "claims": claims, "unknowns": res["unknowns"]}
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def verinoda_analyze(store, root: Path, question: str) -> tuple[str, dict]:
    from verinoda import analysis

    before = {r["id"] for r in store.all("SELECT id FROM claims")}
    res = analysis.analyze(store, Path(root), question)
    ids = [c["id"] for c in res["claims"]]
    meta = {"agent_tool_calls": 1, "claims": res["claims"], "unknowns": res["unknowns"], "intents": res["intents"],
            "usage": res["usage"], "steps": len(res["steps"]),
            "claims_reused": sum(1 for i in ids if i in before), "claims_new": sum(1 for i in ids if i not in before)}
    return analyze_context(res), meta


def verinoda_retrieve(root: Path, question: str, *, max_items: int = 10, max_chars: int = 6000) -> tuple[str, dict]:
    from verinoda import index, retrieval

    g = index.load(Path(root))
    res = retrieval.retrieve(g, question, retrieval.Budget(max_items=max_items, max_chars=max_chars))
    conf: dict[str, int] = {}
    for e in res["edges"]:
        conf[str(e.get("confidence"))] = conf.get(str(e.get("confidence")), 0) + 1
    return (json.dumps(res, ensure_ascii=False, separators=(",", ":")),
            {"agent_tool_calls": 1, "items": len(res["items"]), "edges": res["edges"], "edge_confidence": conf,
             "budget": res["budget"], "params": {"max_items": max_items, "max_chars": max_chars}})


def verinoda_retrieve_text(root: Path, question: str, *, max_items: int = 10,
                            max_chars: int = 6000) -> tuple[str, dict]:
    """The model-facing text of ``verinoda query``: ``render_text(retrieve(g, q, Budget(10, 6000)), 6000)``."""
    from verinoda import index, retrieval
    from verinoda.benchmark import metrics as mx

    g = index.load(Path(root))
    res = retrieval.retrieve(g, question, retrieval.Budget(max_items=max_items, max_chars=max_chars))
    text = retrieval.render_text(res, max_chars)
    edges = mx.text_outline_assertions(text)
    conf: dict[str, int] = {}
    for e in edges:
        conf[str(e.get("confidence"))] = conf.get(str(e.get("confidence")), 0) + 1
    return text, {"agent_tool_calls": 1, "items": len(res.get("items") or []), "outline_edges": len(edges),
                  "edge_confidence": conf, "truncated_note": "more candidates not shown" in text,
                  "params": {"max_items": max_items, "max_chars": max_chars, "render_chars": max_chars}}
