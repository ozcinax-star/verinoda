"""The debug ledger: one symptom, one repro command, every attempt recorded (docs/DESIGN.md D34).

``start`` opens a session (``dbg_*``) against a base commit and runs the repro
once as attempt 0. ``attempt`` (``verinoda debug try``) runs it again after an
edit - through :func:`verinoda.experiments.run`, so in a throw-away copy with
the same policy and isolation - or records a run the agent made itself
(``observed_output`` + ``exit_code``: ``run_by=agent``, the output's sha256
kept, evidence type ``agent_report``, which never verifies anything).

Each attempt stores the tree it ran on (:mod:`verinoda.treestate`: tree
hash, the files that differ from the base with their content ids, a patch
under ``runs/<attempt id>/change.patch``, the symbols each change touches),
the failure signature (:mod:`verinoda.failsig`; for pytest the
``verinoda_failsig`` plugin is loaded, and with ``trace`` the call tracer too)
and what the loop rules (:mod:`verinoda.looprules`) found at that moment.
Attempts are append-only.

When a definitive rule fires, or after ``debug.max_no_progress`` fix attempts
without progress, the answer says ``stop: true`` and lists strategies, picked
by fixed conditions: differential run vs the base, bisect over commits, an
instrumented run, a narrowed command, a question for the human. Strategies
run only on request (:func:`differential`, :func:`bisect`, :func:`rerun`,
:func:`observe`) and are recorded as attempts too.

Honesty: Verinoda never says "fixed". A pass is reported as "the repro command
passed at tree T in run R", with what was not run. Closing a session as
resolved needs a passing attempt whose tree hash equals the current tree.
Verinoda never edits or reverts the user's code; the only files it writes are
under ``.verinoda/``.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path

from verinoda import evidence as evmod
from verinoda import experiments, failsig, looprules, treestate
from verinoda.paths import load_config, runs_dir
from verinoda.store import Store, new_id, now

MAX_OUTPUT_BYTES = 5 * 1024 * 1024
LIST_CAP = 20
LOOP_KINDS = looprules.LOOP_KINDS
TRY_KINDS = ("fix", "probe", "rerun", "differential")
CALLTRACE_MODULE = "verinoda_calltrace"


class DebugError(ValueError):
    """A debug request that cannot be carried out (no session, bad attempt number, ...)."""


# -- sessions ------------------------------------------------------------------------------------

def _cfg(repo: Path) -> dict:
    return load_config(repo).get("debug") or {}


def _session(store: Store, session_id: str | None) -> dict:
    if session_id:
        s = store.get("debug_sessions", session_id)
        if s is None:
            raise KeyError(f"no debug session {session_id}")
        return s
    s = store.one("SELECT * FROM debug_sessions WHERE status = 'open' ORDER BY created_at DESC, rowid DESC LIMIT 1")
    if s is None:
        raise KeyError("no open debug session; start one with `verinoda debug start \"<symptom>\" -- <repro command>`")
    return s


def _attempts(store: Store, sid: str) -> list[dict]:
    return store.all("SELECT * FROM debug_attempts WHERE session_id = ? ORDER BY n", (sid,))


def _argv(command) -> list[str]:
    if isinstance(command, str):
        import shlex

        command = shlex.split(command)
    argv = [str(a) for a in (command or [])]
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise DebugError("give the repro command (after --)")
    return argv


def _runnable(repo: Path, argv: list[str]) -> tuple[bool, str | None]:
    kind, why = experiments.policy(argv, load_config(repo)["experiments"]["process_isolation_allowlist"])
    if kind == "allowlisted":
        return True, None
    return (experiments.container_runtime() is not None), why


def start(store: Store, repo: Path, symptom: str, command, *, base: str | None = None, trace: bool = False,
          timeout: float | None = None, hypothesis: str | None = None, observed_output: bytes | str | None = None,
          exit_code: int | None = None) -> dict:
    """Open a session and record attempt 0 (the baseline), run by Verinoda or reported by the agent."""
    repo = Path(repo).resolve()
    symptom = (symptom or "").strip()
    if not symptom:
        raise DebugError("describe the symptom")
    argv = _argv(command)
    ref = treestate.check_ref(base) if base else "HEAD"
    if treestate.head_commit(repo) is None:
        raise treestate.NotAGitTree(f"{repo} is not a git work tree with a commit; the debug ledger compares "
                                    "every attempt with a base commit")
    base_sha = treestate.resolve_commit(repo, ref)
    agent = observed_output is not None
    if not agent:
        ok, why = _runnable(repo, argv)
        if not ok:
            raise experiments.ExperimentRefused(
                f"the repro command cannot run under Verinoda's policy ({why}); run it yourself and report the "
                "output with --observed-output FILE --exit-code N (an agent-reported run)")
    sid = new_id("dbg")
    cfg = _cfg(repo)
    settings = {"trace": bool(trace), "timeout": timeout, "max_no_progress": int(cfg.get("max_no_progress", 3))}
    store.insert("debug_sessions", {"id": sid, "symptom": symptom, "command": argv, "base_ref": ref,
                                    "base_commit": base_sha, "settings": settings, "status": "open",
                                    "created_at": now()})
    res = attempt(store, repo, sid, hypothesis=hypothesis or f"baseline: {symptom}", expect="fail", kind="baseline",
                  observed_output=observed_output, exit_code=exit_code)
    return {"session": sid, "symptom": symptom, "command": argv, "base": {"ref": ref, "commit": base_sha},
            "settings": settings, **res}


# -- running one attempt -------------------------------------------------------------------------------

def _inject(argv: list[str], modules: list[str]) -> list[str]:
    """``-p <module>`` right after the pytest runner words (once each)."""
    if not experiments._is_pytest(argv):
        return argv
    at = 1 if experiments._exe_name(argv[0]) == "pytest" else 3
    extra: list[str] = []
    for m in modules:
        if m not in argv:
            extra += ["-p", m]
    return argv[:at] + extra + argv[at:]


def _trace_env(timeout: float) -> dict[str, str]:
    from verinoda.runtime import trace as rt

    grace = max(2.0, min(30.0, 0.15 * timeout))
    lim = rt.DEFAULT_LIMITS
    return {"VERINODA_TRACE_MODE": "auto", "VERINODA_TRACE_FILE": rt.TRACE_FILE,
            "VERINODA_TRACE_DEADLINE_S": f"{max(1.0, timeout - grace):.1f}",
            "VERINODA_TRACE_MAX_PY_START": str(int(lim["max_py_start"])),
            "VERINODA_TRACE_MAX_EDGES": str(int(lim["max_edges"])),
            "VERINODA_TRACE_MAX_BYTES": str(int(lim["max_bytes"]))}


def _trace_summary(store: Store, repo: Path, exp: dict, data: bytes, commit: str | None) -> dict:
    """Ingest the call trace of a repro run; per failing test, the in-repo symbols it reached."""
    from verinoda.runtime import trace as rt

    sha = hashlib.sha256(data).hexdigest()
    tr = rt.parse_trace(data)
    h = tr["header"]
    complete = bool(h.get("complete")) and bool(h.get("final")) and h.get("exitstatus") in (0, 1) \
        and not h.get("stopped_early") and exp["outcome"] != "timeout"
    tr["header"] = {**h, "complete": complete}
    rid = rt.ingest(store, tr, experiment_id=exp["id"], snapshot_id=None, commit=commit, trace_sha256=sha,
                    extra_header={"source": "debug", "experiment_outcome": exp["outcome"]})
    outcomes = {t: rt.phase_outcome(v.get("phases", {})) for t, v in tr["tests"].items()}
    called = sorted(t for t, v in tr["tests"].items() if (v.get("phases") or {}).get("call"))
    failing = sorted(t for t, o in outcomes.items() if o in ("failed", "error"))
    reached: dict[str, set[str]] = {t: set() for t in failing}
    for e in tr["edges"]:
        path, _, qual = e["callee"]
        if path == rt.EXT:
            continue
        key = f"{path}::{failsig.clean_qual(qual)}"
        for ctx in e.get("contexts", []):
            nid = ctx.rpartition("|")[0]
            if nid in reached:
                reached[nid].add(key)
    return {"run_id": rid, "complete": complete, "failing": failing, "called": called,
            "reached": {t: sorted(v)[:2000] for t, v in reached.items()}, "tracer": h.get("tracer")}


def _reader(repo: Path, base: str | None, ids: dict[str, str], tree_files: dict, commit: str | None,
            base_map: dict[str, str] | None = None):
    """Content of a file in the tree that ran (for mapping crash locations to symbols)."""
    cache: dict[str, bytes | None] = {}

    def read(rel: str) -> bytes | None:
        if rel in cache:
            return cache[rel]
        data = None
        want = ids.get(rel)
        p = repo / rel
        if want is not None and p.is_file():
            try:
                raw = p.read_bytes()
                if treestate.content_id(raw) == want:
                    data = treestate.normalise(raw)
            except OSError:
                data = None
        if data is None and rel in tree_files and tree_files[rel]:
            data = treestate.get_blob(repo, tree_files[rel])
        if data is None and base_map is not None and rel not in tree_files and base_map.get(rel):
            data = treestate.get_blob(repo, base_map[rel])
        if data is None:
            src = commit or base
            if src:
                raw = treestate.read_blobs(repo, [f"{src}:{rel}"]).get(f"{src}:{rel}")
                data = treestate.normalise(raw) if raw is not None else None
        cache[rel] = data
        return data
    return read


def _tree_record(repo: Path, base: str, ids: dict[str, str], source: dict) -> dict:
    """Changes vs the base for the tree that ran; blobs stored; the per-file diff vs the base."""
    if source.get("kind") in ("commit", "agent_base"):
        ch = treestate.changes_between_commits(repo, base, source["commit"])
        for rel in source.get("overlay") or []:
            data = (repo / rel).read_bytes()
            norm = treestate.normalise(data)
            braw = treestate.read_blobs(repo, [f"{base}:{rel}"]).get(f"{base}:{rel}")
            bnorm = treestate.normalise(braw) if braw is not None else None
            if norm != bnorm:
                ch["tree_files"][rel] = hashlib.sha256(norm).hexdigest()
                ch["contents"][rel] = norm
                if bnorm is not None:
                    ch["base"][rel] = bnorm
    else:
        ch = treestate.changes_from_ids(repo, base, ids, treestate.base_ids(repo, base))
    for data in ch["contents"].values():
        treestate.put_blob(repo, data)
    vs_base = []
    for rel in sorted(ch["tree_files"]):
        new = ch["contents"].get(rel)
        if ch["tree_files"][rel] is not None and new is None:
            vs_base.append({"path": rel, "status": "modified", "symbols": [], "content_unknown": True})
            continue
        d = treestate.diff_file(rel, ch["base"].get(rel), new)
        vs_base.append({k: d[k] for k in ("path", "status", "test", "symbols", "kinds") if k in d}
                       | {"hunks": [{"old": h["old"], "new": h["new"], "symbols": h["symbols"]} for h in d["hunks"]]})
    return {**ch, "vs_base": vs_base}


def _write_patch(repo: Path, aid: str, ch: dict) -> str | None:
    pairs = {p: (ch["base"].get(p), ch["contents"].get(p)) for p in ch["tree_files"]
             if ch["tree_files"][p] is None or p in ch["contents"]}
    if not pairs:
        return None
    d = runs_dir(repo) / aid
    d.mkdir(parents=True, exist_ok=True)
    out = d / "change.patch"
    out.write_bytes(treestate.unified_patch(pairs).encode("utf-8"))
    return str(out)


def _not_run(argv: list[str], run_by: str) -> list[str]:
    out = []
    if experiments._is_pytest(argv):
        at = 1 if experiments._exe_name(argv[0]) == "pytest" else 3
        sel = [a for i, a in enumerate(argv[at:]) if not a.startswith("-")
               and not (i > 0 and argv[at:][i - 1] in ("-p", "-k", "-m", "-o", "-c", "--rootdir"))]
        if sel:
            out.append(f"only {' '.join(sel[:5])} ran; the rest of the test suite was not run")
    else:
        out.append("only the repro command ran; other tests were not run")
    out.append("one environment only (this machine, this interpreter); nothing else was tried")
    if run_by == "agent":
        out.append("Verinoda did not run this command: the outcome and output are as the agent reported them")
    return out


def _passed_text(a: dict) -> str:
    run = a.get("experiment_id") or "an agent-reported run"
    return f"the repro command passed at tree {(a.get('tree_hash') or '?')[:12]} in {run} (attempt {a['n']})"


def attempt(store: Store, repo: Path, session_id: str | None = None, *, hypothesis: str, expect: str = "pass",
            command=None, kind: str = "fix", observed_output: bytes | str | None = None,
            exit_code: int | None = None, trace: bool | None = None, timeout: float | None = None,
            ref: str | None = None, overlay: list[str] | None = None) -> dict:
    """Record one attempt (``verinoda debug try``). See the module docstring."""
    t_start = time.perf_counter()
    steps: dict[str, float] = {}
    t_run = t_start
    repo = Path(repo).resolve()
    sess = _session(store, session_id)
    if sess["status"] != "open":
        raise DebugError(f"session {sess['id']} is {sess['status']}; start a new one")
    if kind not in (*TRY_KINDS, "baseline", "bisect"):
        raise DebugError(f"kind must be one of {', '.join(TRY_KINDS)}")
    if expect not in ("pass", "fail"):
        raise DebugError("expect must be pass or fail")
    hypothesis = (hypothesis or "").strip()
    if not hypothesis:
        raise DebugError("state the hypothesis this attempt tests (what you believe and why)")
    settings = sess.get("settings") or {}
    argv = _argv(command) if command else list(sess["command"])
    base = sess["base_commit"]
    prior = _attempts(store, sess["id"])
    n = len(prior)
    aid = new_id("dba")
    agent = observed_output is not None
    if agent and exit_code is None:
        raise DebugError("an agent-reported run needs --exit-code N with --observed-output")
    if agent and ref:
        raise DebugError("an agent-reported run is recorded against the working tree (or, with kind "
                         "differential, the session base); do not give ref")
    trace = bool(settings.get("trace")) if trace is None else bool(trace)
    source: dict = {"kind": "worktree"}
    ids: dict[str, str] = {}
    exp = None
    stdout = stderr = ""
    plugin_data = None
    trace_info: dict = {}
    output_sha = None
    duration = None
    if agent:
        data = observed_output.encode("utf-8") if isinstance(observed_output, str) else bytes(observed_output)
        data = data[:MAX_OUTPUT_BYTES]
        output_sha = hashlib.sha256(data).hexdigest()
        d = runs_dir(repo) / aid
        d.mkdir(parents=True, exist_ok=True)
        (d / "observed_output.txt").write_bytes(data)
        stdout = data.decode("utf-8", "replace")
        if kind == "differential":  # the agent ran the repro on the prepared copy of the base
            source = {"kind": "agent_base", "commit": base}
            ids = treestate.commit_files(repo, base)
            tree_hash = treestate.tree_id(ids)
        else:
            state = treestate.current(repo, store=store)
            ids = state["files"]
            tree_hash = state["hash"]
        outcome, why = experiments._classify_outcome(argv, int(exit_code), False, stdout, "")
        ev = {"source_type": "agent_report", "locator": f"agent-reported run, {sess['id']} attempt {n}: "
                                                       f"{' '.join(argv)}",
              "path": None, "commit_sha": None, "content_hash": "sha256:" + output_sha,
              "excerpt": " | ".join(experiments._summarize(stdout, "", int(exit_code), False)["summary_lines"]
                                    or [stdout.strip().splitlines()[-1][:200]] if stdout.strip() else [])[:400],
              "meta": {"run_by": "agent", "outcome": outcome, "exit_code": int(exit_code), "session": sess["id"],
                       "attempt": n, "tree_hash": tree_hash, "output_sha256": output_sha,
                       "observed_output": str(d / "observed_output.txt"),
                       "scope": "reported by the agent; Verinoda did not run it and it never verifies a claim"}}
        evidence_id = evmod.add(store, ev)
        code = int(exit_code)
    else:
        mods = [failsig.PLUGIN_MODULE] + ([CALLTRACE_MODULE] if trace else [])
        plugins = {f"{failsig.PLUGIN_MODULE}.py": failsig.plugin_source()}
        env_extra: dict[str, str] = {}
        t = float(timeout or settings.get("timeout") or load_config(repo)["experiments"]["default_timeout"])
        if trace and experiments._is_pytest(argv):
            from verinoda.runtime import trace as rt

            plugins[f"{CALLTRACE_MODULE}.py"] = rt.plugin_source()
            env_extra.update(_trace_env(t))
        run_argv = _inject(argv, mods) if experiments._is_pytest(argv) else argv
        t_run = time.perf_counter()
        exp = experiments.run(store, repo, run_argv, hypothesis=f"[{sess['id']} attempt {n}] {hypothesis}",
                              expect=expect, timeout=t, plugins=plugins if experiments._is_pytest(argv) else None,
                              env_extra=env_extra or None, file_ids=ids, ref=ref, overlay=overlay)
        source = exp["source"]
        tree_hash = exp["tree"]["hash"]
        outcome = exp["outcome"]
        code = exp.get("exit_code")
        duration = exp["duration_s"]
        evidence_id = exp["evidence_id"]
        stdout = Path(exp["logs"]["stdout"]).read_text(encoding="utf-8", errors="replace")
        stderr = Path(exp["logs"]["stderr"]).read_text(encoding="utf-8", errors="replace")
        art = exp.get("artifacts") or {}
        if art.get(failsig.PLUGIN_FILE):
            plugin_data = Path(art[failsig.PLUGIN_FILE]).read_bytes()
        if trace:
            from verinoda.runtime import trace as rt

            if art.get(rt.TRACE_FILE):
                trace_info = _trace_summary(store, repo, exp, Path(art[rt.TRACE_FILE]).read_bytes(),
                                            source.get("commit"))
            else:
                trace_info = {"complete": False, "error": "the tracer wrote no trace (not a pytest run, or it "
                                                          "failed to load)"}
    steps["run_s"] = round(time.perf_counter() - t_run, 3) if exp else 0.0
    t_step = time.perf_counter()
    ch = _tree_record(repo, base, ids, source)
    patch_path = _write_patch(repo, aid, ch)
    reader = _reader(repo, base, ids, ch["tree_files"], source.get("commit"),
                     base_map=treestate.base_ids(repo, base) if source.get("kind") == "worktree" else None)
    steps["tree_s"] = round(time.perf_counter() - t_step, 3)
    t_step = time.perf_counter()
    sig = failsig.extract(stdout, stderr, outcome=outcome, files=ids.keys(), reader=reader, plugin_data=plugin_data,
                          roots=[str(repo)])
    sig_exact, sig_coarse = failsig.keys(sig)
    loop_prior = [a for a in prior if a["kind"] in LOOP_KINDS and (a.get("copy_source") or {}).get("kind",
                                                                                                    "worktree")
                  == "worktree" and a["command"] == argv]
    is_loop = kind in LOOP_KINDS and source.get("kind") == "worktree"
    prev = loop_prior[-1] if loop_prior else None
    vs_prev = treestate.diff_trees(repo, base, prev["tree_files"] or {}, ch["tree_files"],
                                   base_map=treestate.base_ids(repo, base)) if (prev and is_loop) else []
    cur = {"n": n, "kind": kind, "outcome": outcome, "run_by": "agent" if agent else "verinoda",
           "experiment_id": exp["id"] if exp else None, "tree_hash": tree_hash, "tree_files": ch["tree_files"],
           "signature": sig, "sig_exact": sig_exact, "sig_coarse": sig_coarse, "hypothesis": hypothesis,
           "hypothesis_terms": looprules.terms(hypothesis), "expect": expect, "vs_prev": vs_prev,
           "trace": trace_info, "command": argv, "vs_base": ch["vs_base"]}
    steps["signature_s"] = round(time.perf_counter() - t_step, 3)
    t_step = time.perf_counter()
    hist = [_as_rule_input(a) for a in loop_prior]
    if is_loop:
        ev = looprules.evaluate(hist, cur, max_no_progress=int(settings.get("max_no_progress", 3)))
    else:
        ev = {"progress": None, "findings": [], "stop": False, "stop_reason": None, "flaky": None,
              "no_progress_streak": None, "assertion_lines": []}
    ev["findings"] = ev["findings"] + _order_dependent(prior, sess, argv, tree_hash, sig, n)
    questions = _questions(cur, prev, ev)
    strategies = propose(store, repo, sess, prior, cur, ev) if (ev["stop"] or ev["flaky"]) else []
    row = {"id": aid, "session_id": sess["id"], "n": n, "kind": kind, "hypothesis": hypothesis,
           "hypothesis_terms": cur["hypothesis_terms"], "command": argv, "expect": expect,
           "run_by": cur["run_by"], "copy_source": source, "tree_hash": tree_hash, "tree_files": ch["tree_files"],
           "patch_path": patch_path, "touched": {"vs_base": ch["vs_base"], "vs_prev": vs_prev},
           "experiment_id": cur["experiment_id"], "evidence_id": evidence_id,
           "runtime_run_id": trace_info.get("run_id"), "outcome": outcome, "exit_code": code, "duration_s": duration,
           "output_sha256": output_sha, "signature": sig, "sig_exact": sig_exact, "sig_coarse": sig_coarse,
           "trace": trace_info, "progress": ev["progress"],
           "findings": ev["findings"] + ([{"rule": "flaky", "strength": "flaky", **ev["flaky"]}] if ev["flaky"] else []),
           "stop": int(bool(ev["stop"])), "stop_reason": ev["stop_reason"], "strategies": strategies,
           "created_at": now()}
    steps["rules_s"] = round(time.perf_counter() - t_step, 3)
    t_step = time.perf_counter()
    store.insert("debug_attempts", row)
    steps["store_s"] = round(time.perf_counter() - t_step, 3)
    out = _attempt_view(row, ch.get("drift") or [], questions)
    out["session"] = sess["id"]
    if ev.get("no_progress_streak") is not None:
        out["no_progress_streak"] = ev["no_progress_streak"]
    if outcome == "pass":
        out["result"] = _passed_text(row)
        out["not_run"] = _not_run(argv, row["run_by"])
    out["overhead_s"] = round(time.perf_counter() - t_start - (exp["duration_s"] if exp else 0.0), 3)
    out["cost"] = {"command_s": exp["duration_s"] if exp else None, "overhead_s": out["overhead_s"],
                   "steps": steps}
    return out


def _as_rule_input(a: dict) -> dict:
    return {"n": a["n"], "kind": a["kind"], "outcome": a["outcome"], "run_by": a["run_by"],
            "experiment_id": a.get("experiment_id"), "tree_hash": a.get("tree_hash"),
            "tree_files": a.get("tree_files") or {}, "signature": a.get("signature") or {},
            "sig_exact": a.get("sig_exact"), "sig_coarse": a.get("sig_coarse"), "hypothesis": a.get("hypothesis"),
            "hypothesis_terms": a.get("hypothesis_terms") or [], "expect": a.get("expect"),
            "vs_prev": (a.get("touched") or {}).get("vs_prev") or [], "trace": a.get("trace") or {},
            "progress": a.get("progress"), "command": a.get("command")}


def _questions(cur: dict, prev: dict | None, ev: dict) -> list[dict]:
    """Questions only the user can answer (whether a test or the code states the intended behaviour)."""
    if not any(f["rule"] == "test_edited" for f in ev["findings"]):
        return []
    lines = ev.get("assertion_lines") or []
    test_side = [f"{x['path']}:{x['line']}: {x['text']}" for x in lines[:3]] or \
        [c["path"] for c in cur.get("vs_prev") or [] if c.get("test")][:3]
    from verinoda.runtime.trace import is_test_path

    code_side = []
    for f in ((prev or {}).get("signature") or {}).get("failures") or []:
        # the code under test: the innermost frame outside test files, else where the test itself failed
        code = [fr for fr in f.get("frames") or [] if fr.get("path") and not is_test_path(fr["path"])]
        if code:
            fr = code[-1]
            code_side.append(f"{fr['path']}:{fr.get('line')} ({fr.get('symbol')}; {f.get('exc')} raised at "
                             f"{f.get('path')}:{f.get('line')})")
        elif f.get("path") and f.get("line"):
            code_side.append(f"{f['path']}:{f['line']} (the test fails here: {f.get('exc')} in {f.get('symbol')})")
    return [{"question": "Is the test's expectation or the code's behaviour the intended one?",
             "asked_because": "an attempt changed an existing test instead of the code (test_edited)",
             "discriminates": "keep the test and fix the code, or accept the new behaviour and change the test",
             "test_side": test_side, "code_side": list(dict.fromkeys(code_side))[:3]}]


def _attempt_view(row: dict, drift: list[str], questions: list[dict]) -> dict:
    sig = row.get("signature") or {}
    touched = row.get("touched") or {}
    view = {
        "attempt": row["n"], "kind": row["kind"], "outcome": row["outcome"], "run_by": row["run_by"],
        "experiment_id": row.get("experiment_id"), "evidence_id": row.get("evidence_id"),
        "hypothesis": row["hypothesis"],
        "tree": {"hash": row.get("tree_hash"), "source": row.get("copy_source"),
                 "changed_files": [{"path": c["path"], "status": c.get("status"), "symbols": c.get("symbols", [])[:8]}
                                   for c in (touched.get("vs_base") or [])[:LIST_CAP]],
                 "changed_files_total": len(touched.get("vs_base") or []),
                 "vs_prev": [{"path": c["path"], "status": c.get("status"), "symbols": c.get("symbols", [])[:8],
                              **({"test": True} if c.get("test") else {})} for c in (touched.get("vs_prev") or [])],
                 "patch": row.get("patch_path")},
        "signature": {"status": sig.get("status"), "parser": sig.get("parser"),
                      "failures": [{"test": f.get("test"), "phase": f.get("phase"), "exc": f.get("exc"),
                                    "at_symbol": f.get("at"), "at_line": (f"{f['path']}:{f['line']}"
                                                                          if f.get("path") and f.get("line") else None),
                                    "msg_norm": f.get("msg_norm")} for f in (sig.get("failures") or [])[:LIST_CAP]],
                      "failures_total": len(sig.get("failures") or []), "sig_exact": row.get("sig_exact"),
                      "sig_coarse": row.get("sig_coarse")},
        "progress": row.get("progress"),
        "loop": [f for f in row.get("findings") or [] if f.get("rule") != "flaky"],
        "stop": bool(row.get("stop")), "stop_reason": row.get("stop_reason"),
        "strategies": row.get("strategies") or [], "questions_for_human": questions,
    }
    fl = [f for f in row.get("findings") or [] if f.get("rule") == "flaky"]
    if fl:
        view["flaky"] = {k: v for k, v in fl[0].items() if k not in ("rule", "strength")}
        view["note"] = "flaky: loop rules and stop are suspended until a rerun series of one tree comes out identical"
    if row.get("trace"):
        tr = row["trace"]
        view["trace"] = {"run_id": tr.get("run_id"), "complete": tr.get("complete"), "failing": tr.get("failing"),
                         **({"error": tr["error"]} if tr.get("error") else {})}
    if drift:
        view["tree"]["changed_during_run"] = drift[:LIST_CAP]
    if sig.get("status") in ("unknown", "partial") and row["outcome"] != "pass":
        view["signature"]["note"] = ("the output did not yield a complete failure record; this attempt's signature "
                                     "is unknown and never counts as the same as another")
    return view


# -- strategies ----------------------------------------------------------------------------------------

def _base_outcome(sess: dict, attempts: list[dict]) -> tuple[str | None, int | None]:
    """How the repro did on the base tree: a differential run, or a baseline on a clean tree (the base itself)."""
    for a in reversed(attempts):
        src = a.get("copy_source") or {}
        if a["kind"] == "differential" and (src.get("commit") == sess["base_commit"] or src.get("kind") == "agent_base"):
            return a["outcome"], a["n"]
    for a in attempts:
        if a["kind"] == "baseline" and not (a.get("tree_files") or {}) and \
                (a.get("copy_source") or {}).get("kind", "worktree") == "worktree":
            return a["outcome"], a["n"]
    return None, None


def _known_good(attempts: list[dict]) -> str | None:
    for a in reversed(attempts):
        src = a.get("copy_source") or {}
        if a["outcome"] == "pass" and src.get("kind") == "commit" and src.get("commit"):
            return src["commit"]
    return None


def _duration(attempts: list[dict], cur: dict | None = None) -> float:
    ds = [a.get("duration_s") for a in attempts if a.get("duration_s")]
    return float(ds[-1]) if ds else 5.0


def propose(store: Store, repo: Path, sess: dict, prior: list[dict], cur: dict, ev: dict) -> list[dict]:
    """Strategies for a stopped (or flaky) session, in the design's fixed order, each with why and cost."""
    sid = sess["id"]
    argv = list(sess["command"])
    attempts = prior + [{**cur, "copy_source": {"kind": "worktree"}, "duration_s": None}]
    run_s = _duration(prior)
    runnable, _ = _runnable(repo, argv)
    out: list[dict] = []
    if ev.get("flaky"):
        times = int(_cfg(repo).get("rerun_times", 5))
        out.append({"id": "rerun", "why": "the same tree gave different results: measure the pass rate before "
                                          "anything else", "command": f"verinoda debug rerun --session {sid} --times "
                                                                      f"{times}",
                    "cost_estimate_s": round(times * run_s, 1), "runnable_by_verinoda": runnable})
    base_out, base_n = _base_outcome(sess, prior)
    differs = bool(cur.get("tree_files"))
    if differs and base_out != "fail":
        out.append({"id": "differential", "why": f"the working tree differs from the base {sess['base_commit'][:12]}"
                                                 ": run the repro on a copy of the base; if it passes there, the cause"
                                                 " is in the diff (hunks ranked by the failure's traceback)",
                    "command": f"verinoda debug differential --session {sid}"
                               + ("" if runnable else " --prepare"),
                    "cost_estimate_s": round(run_s, 1), "runnable_by_verinoda": runnable})
    if base_out == "fail":
        good = _known_good(prior)
        out.append({"id": "bisect", "why": f"the repro also fails at the base (attempt {base_n}): search the history "
                                           "for the first commit where it fails, in throw-away copies"
                                           + ("" if good else "; no passing commit is known yet - give --good, or "
                                                              "Verinoda steps back through history to find one"),
                    "command": f"verinoda debug bisect --session {sid}" + (f" --good {good[:12]}" if good else ""),
                    "cost_estimate_s": round(run_s * (math.ceil(math.log2(16)) + (0 if good else 4)), 1),
                    "runnable_by_verinoda": runnable})
    if experiments._is_pytest(argv) and not (cur.get("trace") or {}).get("complete"):
        out.append({"id": "observe", "why": "run the repro under the call tracer: are the edited symbols reached by "
                                            "the failing tests at all, and which calls lead to the crash",
                    "command": f"verinoda debug observe --session {sid}",
                    "cost_estimate_s": round(1.5 * run_s, 1), "runnable_by_verinoda": runnable})
    suspects = _suspects(repo, sess, prior, cur)
    if suspects:
        out.append({"id": "narrowing", "why": "suspects, each with its evidence: the failure's traceback, what "
                                              "changed since the last passing state, and whether the failing tests "
                                              "reached it; only a complete trace that did not reach a suspect rules "
                                              "it out", "command": None, "cost_estimate_s": 0,
                    "suspects": suspects[:LIST_CAP]})
    failing = sorted((cur.get("signature") or {}).get("failed_tests") or [])
    if experiments._is_pytest(argv) and failing and not any("::" in a for a in argv):
        narrow = _without_selectors(argv) + failing[:5]
        out.append({"id": "minimal_repro", "why": "narrow the repro to the failing tests (faster runs; a test that "
                                                  "passes alone but fails in the suite is order-dependent)",
                    "command": f"verinoda debug try --session {sid} --kind probe --hypothesis \"the failing tests "
                               f"alone\" -- " + " ".join(narrow),
                    "cost_estimate_s": round(run_s, 1), "runnable_by_verinoda": runnable})
    if any(f["rule"] == "test_edited" for f in ev.get("findings") or []) or not out:
        out.append({"id": "ask_human", "why": "a test was edited, or nothing else narrows the cause: only the user "
                                              "knows the intended behaviour", "command": None, "cost_estimate_s": 0})
    return out


def _without_selectors(argv: list[str]) -> list[str]:
    """A pytest command without its positional test paths (option values kept)."""
    at = 1 if experiments._exe_name(argv[0]) == "pytest" else 3
    out = list(argv[:at])
    rest = argv[at:]
    for i, a in enumerate(rest):
        if a.startswith("-") or (i > 0 and rest[i - 1] in ("-p", "-k", "-m", "-o", "-c", "--rootdir", "-W")):
            out.append(a)
    return out


def _suspects(repo: Path, sess: dict, prior: list[dict], cur: dict) -> list[dict]:
    """Traceback symbols and symbols changed since the last passing state (else since the base), with evidence;
    a Python function the failing tests did not reach in a complete trace is marked ruled out."""
    from verinoda.runtime.trace import is_test_path

    found: dict[tuple[str, str], dict] = {}
    functions: set[tuple[str, str]] = set()  # changed Python functions: the only suspects a trace can rule out

    def add(path: str, sym: str, why: str) -> None:
        if not path or not sym or sym.startswith("#"):
            return
        s = found.setdefault((path, sym), {"at": f"{path}::{sym}", "evidence": []})
        if why not in s["evidence"]:
            s["evidence"].append(why)

    for f in (cur.get("signature") or {}).get("failures") or []:
        for fr in f.get("frames") or []:
            add(fr.get("path"), fr.get("symbol"), f"on the traceback of attempt {cur['n']} "
                                                  f"({fr.get('path')}:{fr.get('line')})")
        add(f.get("path"), f.get("symbol"), f"crash site of attempt {cur['n']} ({f.get('path')}:{f.get('line')})")
    passing = [a for a in prior if a["outcome"] == "pass" and a["kind"] in LOOP_KINDS and
               (a.get("copy_source") or {}).get("kind", "worktree") == "worktree"]
    if passing:
        ref = passing[-1]
        changes = treestate.diff_trees(repo, sess["base_commit"], ref.get("tree_files") or {},
                                       cur.get("tree_files") or {})
        label = f"changed since attempt {ref['n']} passed"
    else:
        changes = cur.get("vs_base") or []
        label = f"changed vs the base {sess['base_commit'][:12]}"
    for c in changes:
        for s in c.get("symbols") or []:
            if s != "<module>":
                add(c["path"], s, label)
                if (c.get("kinds") or {}).get(s) == "def" and c["path"].endswith(".py") and not c.get("test"):
                    functions.add((c["path"], s))
    tr = cur.get("trace") or {}
    reached: set[str] = set()
    for t_ in tr.get("failing") or []:
        reached |= set((tr.get("reached") or {}).get(t_) or [])
    for (path, sym), s in found.items():
        if tr.get("complete") and path.endswith(".py"):
            if s["at"] in reached:
                s["evidence"].append(f"reached by the failing tests in run {tr.get('run_id')}")
            elif (path, sym) in functions and not is_test_path(path) and not any(
                    e.startswith(("on the traceback", "crash site")) for e in s["evidence"]):
                s["ruled_out"] = f"not reached by the failing tests in the complete trace of run {tr.get('run_id')}"
    ranked = sorted(found.values(), key=lambda s: (bool(s.get("ruled_out")), is_test_path(s["at"].split("::")[0]),
                                                   -len(s["evidence"]), s["at"]))
    return ranked


def _order_dependent(prior: list[dict], sess: dict, argv: list[str], tree_hash: str | None, sig: dict,
                     n: int) -> list[dict]:
    """A narrowed pytest command passed tests the session's full repro failed on the same tree."""
    if argv == list(sess["command"]) or not experiments._is_pytest(argv):
        return []
    passed = {t for t, o in (sig.get("tests") or {}).items() if o == "passed"}
    if not passed:
        return []
    for a in reversed(prior):
        if a["command"] == list(sess["command"]) and a.get("tree_hash") == tree_hash and a["outcome"] == "fail":
            both = sorted(passed & set((a.get("signature") or {}).get("failed_tests") or []))
            if both:
                return [{"rule": "order_dependent", "strength": "heuristic",
                         "text": f"{', '.join(both[:3])} passed alone (attempt {n}) but failed in the full repro on "
                                 f"the same tree (attempt {a['n']}): order-dependent, or flaky",
                         "evidence": [{"attempt": a["n"], "run": a.get("experiment_id")},
                                      {"attempt": n, "run": None}]}]
            break
    return []


def _traceback_symbols(a: dict) -> set[tuple[str, str]]:
    out = set()
    for f in (a.get("signature") or {}).get("failures") or []:
        if f.get("path") and f.get("symbol"):
            out.add((f["path"], f["symbol"]))
        for fr in f.get("frames") or []:
            if fr.get("path") and fr.get("symbol"):
                out.add((fr["path"], fr["symbol"]))
    return out


def rank_hunks(repo: Path, sess: dict, loop_attempt: dict, against: str | None = None,
               baseline: dict | None = None) -> list[dict]:
    """The hunks of the failing tree vs the base (or ``against``), ranked.

    First the hunks that were already there when the symptom was first recorded
    (attempt 0's tree; ``session_edit: false``), then those the session's own
    attempts added. Within each: on the failure's traceback, then reached by the
    failing tests in a complete trace, then the others; test files last.
    """
    base = against or sess["base_commit"]
    tb = _traceback_symbols(loop_attempt) | (_traceback_symbols(baseline) if baseline else set())
    reached: set[str] = set()
    for a in (loop_attempt, baseline or {}):
        tr = a.get("trace") or {}
        if tr.get("complete"):
            for t_ in tr.get("failing") or []:
                reached |= set((tr.get("reached") or {}).get(t_) or [])
    tf = loop_attempt.get("tree_files") or {}
    if against:
        ch = treestate.changes_vs_base(repo, base)
        files = [treestate.diff_file(p, ch["base"].get(p), ch["contents"].get(p)) for p in sorted(ch["tree_files"])]
    else:
        files = treestate.diff_trees(repo, base, {}, tf)
    at_start: set[tuple] = set()  # (path, "+"/"-", line text) of the changes attempt 0 already had
    if baseline is not None and not against:
        for f in treestate.diff_trees(repo, base, {}, baseline.get("tree_files") or {}):
            for h in f.get("hunks") or []:
                at_start |= {(f["path"], "-", x.strip()) for x in h.get("removed") or [] if x.strip()}
                at_start |= {(f["path"], "+", x.strip()) for x in h.get("added") or [] if x.strip()}
    out = []
    for f in files:
        for h in f.get("hunks") or []:
            syms = h.get("symbols") or []
            if any((f["path"], s) in tb for s in syms):
                tier, why = 0, "on the failure's traceback"
            elif any(f"{f['path']}::{s}" in reached for s in syms):
                tier, why = 1, "reached by the failing test (complete trace)"
            else:
                tier, why = 2, "other"
            mine = {(f["path"], "-", x.strip()) for x in h.get("removed") or [] if x.strip()} |                 {(f["path"], "+", x.strip()) for x in h.get("added") or [] if x.strip()}
            session_edit = baseline is not None and not against and not (mine & at_start)
            lines = h["new"] if h["new"][1] >= h["new"][0] else h["old"]
            out.append({"path": f["path"], "lines": lines, "at": f"{f['path']}:{lines[0]}", "symbols": syms,
                        "tier": tier, "why": why + ("; added by an attempt of this session" if session_edit
                                                    else ("; already there at attempt 0" if baseline else "")),
                        "session_edit": session_edit, "test_file": bool(f.get("test")),
                        "removed": (h.get("removed") or [])[:5], "added": (h.get("added") or [])[:5]})
    out.sort(key=lambda x: (x["session_edit"], x["tier"], x["test_file"], x["path"], x["lines"][0]))
    for i, x in enumerate(out, 1):
        x["rank"] = i
    return out


def _latest_loop_failure(attempts: list[dict]) -> dict | None:
    for a in reversed(attempts):
        if a["kind"] in LOOP_KINDS and a["outcome"] == "fail" and \
                (a.get("copy_source") or {}).get("kind", "worktree") == "worktree":
            return a
    return None


def differential(store: Store, repo: Path, session_id: str | None = None, *, base: str | None = None,
                 prepare: bool = False) -> dict:
    """Run the repro on a copy of the base commit; if it passes there, rank the diff's hunks."""
    repo = Path(repo).resolve()
    sess = _session(store, session_id)
    against = treestate.resolve_commit(repo, base) if base else sess["base_commit"]
    if prepare:
        return _prepare_copy(repo, sess, against)
    res = attempt(store, repo, sess["id"], hypothesis=f"differential: the repro at the base {against[:12]}",
                  expect="pass", kind="differential", ref=against)
    attempts = _attempts(store, sess["id"])
    loop_fail = _latest_loop_failure(attempts)
    out = {"session": sess["id"], "strategy": "differential", "base": against, "attempt": res["attempt"],
           "outcome_at_base": res["outcome"], "experiment_id": res.get("experiment_id")}
    if res["outcome"] == "pass" and loop_fail is not None:
        first = next((a for a in attempts if a["kind"] == "baseline"), None)
        ranked = rank_hunks(repo, sess, _as_rule_input(loop_fail) | {"trace": loop_fail.get("trace") or {}},
                            against=against if against != sess["base_commit"] else None,
                            baseline=_as_rule_input(first) if first and first["outcome"] == "fail" and
                            (first.get("copy_source") or {}).get("kind", "worktree") == "worktree" else None)
        out["conclusion"] = (f"the repro passes at the base {against[:12]} and fails at attempt {loop_fail['n']}'s "
                             "tree: the cause is in the diff between them (ranked below; run-scoped, one environment)")
        out["hunks"] = ranked[:LIST_CAP]
        out["hunks_total"] = len(ranked)
    elif res["outcome"] == "fail":
        out["conclusion"] = (f"the repro also fails at the base {against[:12]}: the cause predates the working-tree "
                             "changes; bisect the history (`verinoda debug bisect`)")
    else:
        out["conclusion"] = f"inconclusive at the base ({res['outcome']}); the diff cannot be judged from this run"
    return out


def _prepare_copy(repo: Path, sess: dict, commit: str) -> dict:
    """A copy of ``commit`` under ``.verinoda/runs/`` for a command Verinoda may not run itself."""
    d = runs_dir(repo) / f"base-{commit[:12]}"
    if not d.exists():
        d.mkdir(parents=True)
        experiments._copy_commit(repo, commit, d)
    cmd = " ".join(sess["command"])
    return {"session": sess["id"], "strategy": "differential", "prepared_copy": str(d), "base": commit,
            "next_step": (f"run `{cmd}` in {d} yourself, then report it: `verinoda debug try --kind differential "
                          f"--hypothesis \"repro at the base\" --observed-output FILE --exit-code N` (recorded as "
                          "agent-reported)"),
            "limits": ["Verinoda does not run this command (not allowlisted); the copy is a plain file copy of the "
                       "commit (no .git), under .verinoda/"]}


def _rev_list(repo: Path, good: str, bad: str) -> list[str]:
    out = treestate._git(repo, "rev-list", "--first-parent", "--reverse", bad, f"^{good}", "--")
    return [ln.strip() for ln in (out or "").splitlines() if ln.strip()]


def _is_ancestor(repo: Path, a: str, b: str) -> bool:
    import subprocess

    try:
        r = subprocess.run(["git", "-C", str(repo), *treestate._GIT_SAFE, "merge-base", "--is-ancestor", a, b],
                           capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _run_at(store: Store, repo: Path, sess: dict, commit: str, why: str, overlay: list[str] | None) -> dict:
    return attempt(store, repo, sess["id"], hypothesis=f"bisect: {why} at {commit[:12]}", expect="pass",
                   kind="bisect", ref=commit, overlay=overlay)


def bisect(store: Store, repo: Path, session_id: str | None = None, *, good: str | None = None,
           bad: str | None = None, overlay: list[str] | None = None, max_runs: int | None = None) -> dict:
    """Binary search over first-parent commits for the first one where the repro fails (throw-away copies).

    Commits where the command cannot run (inconclusive, timeout) are skipped and
    said so. Without ``good`` Verinoda steps back (1, 2, 4, ... commits) to find a
    passing one. Returns the first bad commit and its hunks ranked by the failure's
    traceback.
    """
    repo = Path(repo).resolve()
    sess = _session(store, session_id)
    budget = int(max_runs or _cfg(repo).get("bisect_max_runs", 12))
    bad_sha = treestate.resolve_commit(repo, bad) if bad else sess["base_commit"]
    runs: list[dict] = []
    skipped: list[str] = []
    notes: list[str] = []
    attempts = _attempts(store, sess["id"])
    good_sha = treestate.resolve_commit(repo, good) if good else _known_good(attempts)
    if good_sha is None:  # step back through first-parent history
        chain = [ln for ln in (treestate._git(repo, "rev-list", "--first-parent", "--max-count", "65", bad_sha, "--")
                               or "").split() if ln]
        step = 1
        while step < len(chain) and len(runs) < budget:
            c = chain[step]
            r = _run_at(store, repo, sess, c, "looking for a passing commit", overlay)
            runs.append({"commit": c, "outcome": r["outcome"], "attempt": r["attempt"]})
            if r["outcome"] == "pass":
                good_sha = c
                break
            if r["outcome"] == "fail":
                bad_sha = c
            else:
                skipped.append(c)
            step *= 2
        if good_sha is None:
            return {"session": sess["id"], "strategy": "bisect", "status": "unknown", "runs": runs,
                    "why": "no passing commit was found within the run budget",
                    "next_step": "give --good <commit where the repro passed>"}
    if not _is_ancestor(repo, good_sha, bad_sha):
        raise DebugError(f"{good_sha[:12]} is not an ancestor of {bad_sha[:12]}; bisect needs good before bad")
    commits = _rev_list(repo, good_sha, bad_sha)
    if not commits:
        raise DebugError("no commits between good and bad")
    estimate = math.ceil(math.log2(len(commits) + 1))
    lo, hi = -1, len(commits) - 1   # commits[hi] fails (bad), lo = good
    tested: dict[int, str] = {hi: "fail"}
    while hi - lo > 1 and len(runs) < budget:
        mid = (lo + hi) // 2
        order = [mid] + [x for k in range(1, hi - lo) for x in (mid + k, mid - k) if lo < x < hi]
        verdict = None
        for i in order:
            if i in tested or len(runs) >= budget:
                continue
            r = _run_at(store, repo, sess, commits[i], "does the repro fail here?", overlay)
            runs.append({"commit": commits[i], "outcome": r["outcome"], "attempt": r["attempt"]})
            if r["outcome"] in ("pass", "fail"):
                tested[i] = r["outcome"]
                verdict = (i, r["outcome"])
                break
            skipped.append(commits[i])
        if verdict is None:
            notes.append("every commit left in the range was skipped (the command could not run there)")
            break
        i, o = verdict
        if o == "pass":
            lo = i
        else:
            hi = i
    first_bad = commits[hi]
    status = "found" if hi - lo == 1 else "range"
    out = {"session": sess["id"], "strategy": "bisect", "status": status, "good": good_sha, "bad": bad_sha,
           "runs": runs, "runs_total": len(runs), "skipped": skipped,
           "cost": {"estimate_runs": estimate, "estimate_s": round(estimate * _duration(attempts), 1),
                    "measured_s": round(sum((_attempts_by_n(store, sess["id"]).get(r["attempt"]) or {})
                                            .get("duration_s") or 0 for r in runs), 1)},
           "limits": ["each commit ran in a copy of that commit only"
                      + (f" with the working-tree files {', '.join(overlay)} laid over it" if overlay else "")
                      + "; dependency or environment changes between commits are not modelled",
                      "a commit where the command could not run is skipped, not judged"]}
    if status == "found":
        parent = commits[hi - 1] if hi > 0 else good_sha
        subject = (treestate._git(repo, "log", "-1", "--format=%s", first_bad, "--") or "").strip()
        out["first_bad_commit"] = {"commit": first_bad, "subject": subject, "parent": parent}
        loop_fail = _latest_loop_failure(_attempts(store, sess["id"]))
        tb = _traceback_symbols(_as_rule_input(loop_fail)) if loop_fail else set()
        ch = treestate.changes_between_commits(repo, parent, first_bad)
        hunks = []
        for p in sorted(ch["tree_files"]):
            d = treestate.diff_file(p, ch["base"].get(p), ch["contents"].get(p))
            for h in d["hunks"]:
                on = any((p, s) in tb for s in h["symbols"])
                lines = h["new"] if h["new"][1] >= h["new"][0] else h["old"]
                hunks.append({"path": p, "lines": lines, "at": f"{p}:{lines[0]}", "symbols": h["symbols"],
                              "on_failing_path": on, "removed": h["removed"][:5], "added": h["added"][:5]})
        hunks.sort(key=lambda x: (not x["on_failing_path"], x["path"], x["lines"][0]))
        out["hunks"] = hunks[:LIST_CAP]
        out["conclusion"] = (f"the repro passes at {parent[:12]} and fails at {first_bad[:12]} ({subject}): that "
                             "commit's change is where to look")
    else:
        out["conclusion"] = (f"the first failing commit is between {commits[lo][:12] if lo >= 0 else good_sha[:12]} "
                             f"and {first_bad[:12]}; the run budget or skipped commits left it open")
    if notes:
        out["notes"] = notes
    return out


def _attempts_by_n(store: Store, sid: str) -> dict[int, dict]:
    return {a["n"]: a for a in _attempts(store, sid)}


def rerun(store: Store, repo: Path, session_id: str | None = None, *, times: int | None = None) -> dict:
    """Run the repro ``times`` times on the current tree and report the pass rate."""
    repo = Path(repo).resolve()
    sess = _session(store, session_id)
    k = max(1, min(20, int(times or _cfg(repo).get("rerun_times", 5))))
    res = [attempt(store, repo, sess["id"], hypothesis=f"rerun {i + 1}/{k}: is the result stable?", expect="pass",
                   kind="rerun") for i in range(k)]
    passed = sum(1 for r in res if r["outcome"] == "pass")
    sigs = {r["signature"]["sig_exact"] for r in res if r["outcome"] == "fail"}
    trees = {r["tree"]["hash"] for r in res}
    tree = res[-1]["tree"]["hash"]
    same = [a for a in _attempts(store, sess["id"]) if a.get("tree_hash") == tree and a["kind"] in LOOP_KINDS
            and a["command"] == list(sess["command"]) and a["outcome"] in ("pass", "fail")]
    all_pass = sum(1 for a in same if a["outcome"] == "pass")
    all_sigs = {a.get("sig_exact") for a in same if a["outcome"] == "fail"}
    out = {"session": sess["id"], "strategy": "rerun", "runs": k, "passed": passed, "pass_rate": round(passed / k, 2),
           "distinct_failures": len(sigs), "attempts": [r["attempt"] for r in res],
           "tree": tree, "tree_runs": len(same), "tree_passed": all_pass,
           "flaky": res[-1].get("flaky") is not None,
           "limits": ["a rerun series shows flakiness on this machine only; it cannot prove a test is stable"]}
    if len(trees) > 1:
        out["note"] = "the tree changed during the reruns; the series mixes trees"
    series_same = passed in (0, k) and len(sigs) <= 1
    history_same = all_pass in (0, len(same)) and len(all_sigs) <= 1
    if series_same and history_same:
        out["conclusion"] = f"stable: all {len(same)} recorded run(s) of this tree gave the same result"
    else:
        out["conclusion"] = (f"flaky: this tree passed {all_pass} of {len(same)} recorded runs "
                             f"({passed} of {k} in this series)")
    return out


def observe(store: Store, repo: Path, session_id: str | None = None, *, hypothesis: str | None = None) -> dict:
    """The repro once more on the current tree under the call tracer (a probe attempt).

    Besides the attempt record: ``edits_reached`` - for each Python function changed vs the base,
    the failing tests that reached it (an empty list in a complete trace means not reached in this
    run) - and ``chain``: the calls observed from the first failing test to its crash symbol.
    """
    sess = _session(store, session_id)
    if not experiments._is_pytest(list(sess["command"])):
        raise DebugError("the call tracer needs a pytest repro command")
    res = attempt(store, repo, sess["id"], hypothesis=hypothesis or "instrumented run: are the edits reached?",
                  expect="fail", kind="probe", trace=True)
    row = _attempts_by_n(store, sess["id"]).get(res["attempt"]) or {}
    tr = row.get("trace") or {}
    if not tr.get("run_id"):
        return res
    reached_by: dict[str, list[str]] = {}
    for c in (row.get("touched") or {}).get("vs_base") or []:
        for s in c.get("symbols") or []:
            if c["path"].endswith(".py") and (c.get("kinds") or {}).get(s) == "def":
                key = f"{c['path']}::{s}"
                reached_by[key] = [t_ for t_ in tr.get("failing") or [] if key in ((tr.get("reached") or {}).get(t_)
                                                                                   or [])]
    res["edits_reached"] = {"complete_trace": bool(tr.get("complete")), "by_failing_tests": reached_by,
                            "note": "run-scoped: an empty list in a complete trace means the failing tests did not "
                                    "reach that function in this run"}
    fails = (row.get("signature") or {}).get("failures") or []
    if fails and fails[0].get("test") and fails[0].get("path") and fails[0].get("symbol"):
        res["chain"] = _call_chain(store, tr["run_id"], fails[0]["test"], fails[0]["path"], fails[0]["symbol"])
    return res


def _call_chain(store: Store, run_id: str, test: str, path: str, symbol: str) -> list[str]:
    """Shortest chain of observed calls (in ``test``'s call phase) from the test function to ``path::symbol``."""
    rows = store.all("SELECT caller_path, caller_qual, callee_path, callee_qual, tests FROM runtime_calls "
                     "WHERE run_id = ? AND callee_path != '<ext>'", (run_id,))
    ctx = f"{test}|call"
    adj: dict[str, set[str]] = {}
    for r in rows:
        if ctx not in (r.get("tests") or []):
            continue
        a = f"{r['caller_path']}::{failsig.clean_qual(r['caller_qual'])}"
        b = f"{r['callee_path']}::{failsig.clean_qual(r['callee_qual'])}"
        adj.setdefault(a, set()).add(b)
    parts = test.split("::")
    start = f"{parts[0]}::{'.'.join(p.split('[')[0] for p in parts[1:])}"
    goal = f"{path}::{symbol}"
    prev: dict[str, str | None] = {start: None}
    queue = [start]
    while queue:
        cur = queue.pop(0)
        if cur == goal:
            chain = [cur]
            while prev[chain[-1]] is not None:
                chain.append(prev[chain[-1]])  # type: ignore[arg-type]
            return list(reversed(chain))
        for nxt in sorted(adj.get(cur, ())):
            if nxt not in prev:
                prev[nxt] = cur
                queue.append(nxt)
    return []


# -- status, diff, close --------------------------------------------------------------------------------

def status(store: Store, repo: Path, session_id: str | None = None) -> dict:
    sess = _session(store, session_id)
    attempts = _attempts(store, sess["id"])
    repo = Path(repo).resolve()
    rows = []
    for a in attempts:
        rules = [f["rule"] for f in a.get("findings") or [] if f.get("strength") in ("definitive", "heuristic")]
        rows.append({"n": a["n"], "kind": a["kind"], "outcome": a["outcome"], "run_by": a["run_by"],
                     "hypothesis": a["hypothesis"][:160], "tree": (a.get("tree_hash") or "")[:12],
                     "source": (a.get("copy_source") or {}).get("kind"),
                     "commit": ((a.get("copy_source") or {}).get("commit") or "")[:12] or None,
                     "failure": looprules._summ(a) if a["outcome"] != "pass" else None,
                     "progress": a.get("progress"), "loop": rules, "stop": bool(a.get("stop")),
                     "experiment_id": a.get("experiment_id")})
    last_loop = next((a for a in reversed(attempts) if a["kind"] in LOOP_KINDS), None)
    cur_state = treestate.current(repo, store=store)
    out = {"session": sess["id"], "symptom": sess["symptom"], "status": sess["status"],
           "command": sess["command"], "base": {"ref": sess.get("base_ref"), "commit": sess["base_commit"]},
           "attempts": rows, "current_tree": cur_state["hash"]}
    if last_loop is not None:
        strategies = [dict(s) for s in last_loop.get("strategies") or []]
        ran_as = {"differential": ("differential",), "bisect": ("bisect",), "rerun": ("rerun",), "observe": ("probe",)}
        for s in strategies:
            later = [a for a in attempts if a["n"] > last_loop["n"] and a["kind"] in ran_as.get(s["id"], ())
                     and (s["id"] != "observe" or (a.get("trace") or {}).get("run_id"))]
            if later:
                s["done"] = f"attempt {later[-1]['n']} ({later[-1]['kind']}): {later[-1]['outcome']}"
        out["latest"] = {"attempt": last_loop["n"], "stop": bool(last_loop.get("stop")),
                         "stop_reason": last_loop.get("stop_reason"), "strategies": strategies,
                         "tree_is_current": last_loop.get("tree_hash") == cur_state["hash"]}
    passing = [a for a in attempts if a["outcome"] == "pass" and a["kind"] in LOOP_KINDS]
    if passing:
        p = passing[-1]
        out["result"] = _passed_text(p) + ("; that is the current tree" if p.get("tree_hash") == cur_state["hash"]
                                           else "; the working tree has changed since")
        out["not_run"] = _not_run(list(p["command"]), p["run_by"])
    if sess["status"] != "open":
        out["closed"] = {"at": sess.get("closed_at"), "resolved_by": sess.get("resolved_by"),
                         "note": sess.get("close_note")}
    return out


def diff(store: Store, repo: Path, session_id: str | None = None, *, attempt_n: int | None = None,
         base: str | None = None) -> dict:
    """What changed: the working tree now against the session base, another commit or an attempt's tree."""
    repo = Path(repo).resolve()
    sess = _session(store, session_id)
    against_commit = treestate.resolve_commit(repo, base) if base else sess["base_commit"]
    now_ch = treestate.changes_vs_base(repo, against_commit)
    if attempt_n is not None:
        a = _attempts_by_n(store, sess["id"]).get(int(attempt_n))
        if a is None:
            raise DebugError(f"session {sess['id']} has no attempt {attempt_n}")
        if (a.get("copy_source") or {}).get("kind", "worktree") != "worktree":
            raise DebugError(f"attempt {attempt_n} ran on a commit copy; diff against --base instead")
        cur_tf = treestate.changes_vs_base(repo, sess["base_commit"])
        for data in cur_tf["contents"].values():
            treestate.put_blob(repo, data)
        files = treestate.diff_trees(repo, sess["base_commit"], a.get("tree_files") or {}, cur_tf["tree_files"])
        label = f"attempt {attempt_n} (tree {(a.get('tree_hash') or '')[:12]})"
        pairs = {}
        rd_old = treestate.content_reader(repo, sess["base_commit"], a.get("tree_files") or {})
        rd_new = treestate.content_reader(repo, sess["base_commit"], cur_tf["tree_files"])
        for f in files:
            pairs[f["path"]] = (rd_old(f["path"]), rd_new(f["path"]))
    else:
        files = [treestate.diff_file(p, now_ch["base"].get(p), now_ch["contents"].get(p))
                 for p in sorted(now_ch["tree_files"])]
        label = f"commit {against_commit[:12]}"
        pairs = {p: (now_ch["base"].get(p), now_ch["contents"].get(p)) for p in now_ch["tree_files"]}
    patch = treestate.unified_patch(pairs)
    return {"session": sess["id"], "against": label, "files": [
        {"path": f["path"], "status": f["status"], "symbols": f.get("symbols", []),
         "hunks": [{"old": h["old"], "new": h["new"], "symbols": h["symbols"]} for h in f.get("hunks", [])]}
        for f in files[:LIST_CAP]], "files_total": len(files),
        "patch": patch if len(patch) <= 20000 else patch[:20000] + "\n... (cut; full patch: `git diff` or the "
                                                                    "attempt's change.patch)"}


def close(store: Store, repo: Path, session_id: str | None = None, *, resolved_by: int | None = None,
          abandoned: bool = False, note: str | None = None) -> dict:
    repo = Path(repo).resolve()
    sess = _session(store, session_id)
    if sess["status"] != "open":
        raise DebugError(f"session {sess['id']} is already {sess['status']}")
    if bool(abandoned) == (resolved_by is not None):
        raise DebugError("give exactly one of --resolved-by N or --abandoned")
    if abandoned:
        store.update("debug_sessions", sess["id"], {"status": "abandoned", "closed_at": now(), "close_note": note})
        return {"session": sess["id"], "status": "abandoned", "note": note}
    a = _attempts_by_n(store, sess["id"]).get(int(resolved_by))
    if a is None:
        raise DebugError(f"session {sess['id']} has no attempt {resolved_by}")
    if a["outcome"] != "pass" or a["kind"] not in LOOP_KINDS or \
            (a.get("copy_source") or {}).get("kind", "worktree") != "worktree":
        raise DebugError(f"attempt {resolved_by} is not a passing run of the repro on the working tree "
                         f"({a['kind']}, {a['outcome']}); it cannot resolve the session")
    cur = treestate.current(repo, store=store)
    if cur["hash"] != a.get("tree_hash"):
        raise DebugError(f"the working tree changed since attempt {resolved_by} passed (tree "
                         f"{(a.get('tree_hash') or '')[:12]} then, {cur['hash'][:12]} now); run `verinoda debug try` "
                         "on the current tree first")
    store.update("debug_sessions", sess["id"], {"status": "resolved", "resolved_by": int(resolved_by),
                                                "closed_at": now(), "close_note": note})
    out = {"session": sess["id"], "status": "resolved", "result": _passed_text(a) + "; that is the current tree",
           "not_run": _not_run(list(a["command"]), a["run_by"]), "note": note}
    if a["run_by"] == "agent":
        out["basis"] = "agent-reported run: Verinoda did not run the command"
    return out


def compact(res: dict) -> dict:
    """The MCP view: the same answer, JSON-safe."""
    return json.loads(json.dumps(res, default=str))
