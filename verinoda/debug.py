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
resolved needs a passing attempt whose tree hash equals the current tree, and
an earlier run of the repro in the session that failed (the baseline itself
never resolves anything). The repro is compared without pytest's output-only
options (``-vv``, ``--tb=short``, ...).
Verinoda never edits or reverts the user's code; the only files it writes are
under ``.verinoda/``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
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
SYMPTOM_OUTCOMES = ("fail", "timeout")   # a run of the repro that showed the symptom (a hang is one too)
INSERT_RETRIES = 8                        # another ledger command took the attempt number: evaluate again


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
    _check_observed(observed_output, exit_code)
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
    try:
        res = attempt(store, repo, sid, hypothesis=hypothesis or f"baseline: {symptom}", expect="fail",
                      kind="baseline", observed_output=observed_output, exit_code=exit_code)
    except BaseException as exc:
        # no session without its baseline: it would become the default target of later calls
        store.update("debug_sessions", sid, {"status": "abandoned", "closed_at": now(),
                                             "close_note": f"the baseline could not be recorded: "
                                                           f"{type(exc).__name__}: {exc}"[:500]})
        raise
    out = {"session": sid, "symptom": symptom, "command": argv, "base": {"ref": ref, "commit": base_sha},
           "settings": settings, **res}
    if res["outcome"] not in SYMPTOM_OUTCOMES:
        # the symptom was not seen: a session whose repro never failed has nothing a later pass could resolve
        out["reproduced"] = False
        out["warning"] = (f"the repro command did not fail at the baseline ({res['outcome']}): the symptom did not "
                          "reproduce, and this session cannot be closed as resolved until a run of it fails")
        out["next_step"] = ("start a session with a command that fails with the symptom (the failing test, or the "
                            f"test file it is in); close this one with `verinoda debug close {sid} --abandoned`")
    else:
        out["reproduced"] = True
    return out


def _check_observed(observed_output, exit_code) -> None:
    if observed_output is not None and exit_code is None:
        raise DebugError("an agent-reported run needs --exit-code N with --observed-output")
    if observed_output is None and exit_code is not None:
        raise DebugError("--exit-code goes with --observed-output (a run you made yourself)")


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
    anywhere: set[str] = set()   # every in-repo function called in any context (tests, collection, between tests)
    spawns: set[str] = set()     # calls that start child processes (their code runs untraced)
    for e in tr["edges"]:
        path, _, qual = e["callee"]
        if path == rt.EXT:
            if e.get("boundary") and SPAWN_RE.match(str(qual or "")):
                spawns.add(str(qual))
            continue
        key = f"{path}::{failsig.clean_qual(qual)}"
        anywhere.add(key)
        for ctx in e.get("contexts", []):
            nid = ctx.rpartition("|")[0]
            if nid in reached:
                reached[nid].add(key)
    return {"run_id": rid, "complete": complete, "failing": failing, "called": called,
            "reached": {t: sorted(v)[:2000] for t, v in reached.items()}, "tracer": h.get("tracer"),
            "spawns": sorted(spawns)[:20], "_anywhere": anywhere}


# Calls that start another process: the tracer runs in the pytest process only.
SPAWN_RE = re.compile(r"^(subprocess\.|multiprocessing\.|concurrent\.futures\.process\.|asyncio\.(create_subprocess|"
                      r"subprocess)|(os|nt|posix)\.(system|popen|spawn|exec|posix_spawn|fork|startfile)|pty\.spawn|"
                      r"_posixsubprocess\.|_winapi\.CreateProcess|pexpect\.)")


def _reached_anywhere(trace_info: dict, paths: set[str], anywhere: set[str]) -> None:
    """Keep, of every function the traced run called (``anywhere``), those in the files this session touched."""
    keep = sorted(k for k in anywhere if k.split("::", 1)[0] in paths)
    trace_info["reached_any"] = keep[:5000]
    trace_info.pop("reached_any_truncated", None)
    if len(keep) > 5000:
        trace_info["reached_any_truncated"] = True


def _renumber(store: Store, exp: dict | None, evidence_id: str | None, sid: str, was: int, now_n: int) -> None:
    """The records made before the attempt number was known say the number it became."""
    try:
        if exp:
            row = store.get("experiments", exp["id"]) or {}
            hyp = str(row.get("hypothesis") or "")
            store.update("experiments", exp["id"], {"hypothesis": hyp.replace(f"[{sid} attempt {was}]",
                                                                              f"[{sid} attempt {now_n}]", 1)})
        if evidence_id:
            ev = store.evidence(evidence_id) or {}
            meta = dict(ev.get("meta") or {})
            meta["attempt"] = now_n
            store.update_evidence_meta(evidence_id, meta)
    except Exception:  # noqa: BLE001 - labels only; the attempt itself is recorded
        pass


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
                spec = treestate.blob_spec(repo, src, rel)
                raw = treestate.read_blobs(repo, [spec]).get(spec)
                data = treestate.normalise(raw) if raw is not None else None
        cache[rel] = data
        return data
    return read


_VS_BASE_KEYS = ("path", "status", "test", "symbols", "kinds", "no_code_change", "binary", "too_large")


def _tree_record(repo: Path, base: str, ids: dict[str, str], source: dict, prior: list[dict] | None = None) -> dict:
    """Changes vs the base for the tree that ran; blobs stored; the per-file diff vs the base.

    A file with the same content as in an earlier attempt of the session reuses that attempt's diff
    record (the base is the same), so an unchanged big file is diffed once per session, not per attempt."""
    if source.get("kind") in ("commit", "agent_base"):
        ch = treestate.changes_between_commits(repo, base, source["commit"])
        for rel in source.get("overlay") or []:
            data = (repo / rel).read_bytes()
            norm = treestate.normalise(data)
            spec = treestate.blob_spec(repo, base, rel)
            braw = treestate.read_blobs(repo, [spec]).get(spec)
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
    known: dict[tuple[str, str], dict] = {}
    for a in prior or []:
        tf = a.get("tree_files") or {}
        for rec in (a.get("touched") or {}).get("vs_base") or []:
            cid = tf.get(rec.get("path"))
            if cid and not rec.get("content_unknown"):
                known[(rec["path"], cid)] = rec
    vs_base = []
    for rel in sorted(ch["tree_files"]):
        new = ch["contents"].get(rel)
        cid = ch["tree_files"][rel]
        if cid is not None and (rel, cid) in known:
            vs_base.append(known[(rel, cid)])
            continue
        if cid is not None and new is None:
            vs_base.append({"path": rel, "status": "modified", "symbols": [], "content_unknown": True})
            continue
        d = treestate.diff_file(rel, ch["base"].get(rel), new)
        vs_base.append({k: d[k] for k in _VS_BASE_KEYS if k in d}
                       | {"hunks": [{"old": h["old"], "new": h["new"], "symbols": h["symbols"]} for h in d["hunks"]]})
    return {**ch, "vs_base": vs_base, "code_hash": treestate.code_tree_id(ch, repo)}


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


_PT_VALUE_OPTS = ("-p", "-k", "-m", "-o", "-c", "--rootdir", "-W", "--deselect", "--ignore", "--ignore-glob",
                  "--maxfail", "--confcutdir", "--basetemp", "-n", "--dist")


def _not_run(argv: list[str], run_by: str, signature: dict | None = None) -> list[str]:
    """What a passing run did not cover: tests outside the selection, deselected or skipped ones, other
    environments, and whether Verinoda ran it at all."""
    out = []
    if experiments._is_pytest(argv):
        at = 1 if experiments._exe_name(argv[0]) == "pytest" else 3
        rest = argv[at:]
        sel = [a for i, a in enumerate(rest) if not a.startswith("-") and not (i > 0 and rest[i - 1] in _PT_VALUE_OPTS)]
        if sel:
            out.append(f"only {' '.join(sel[:5])} ran; the rest of the test suite was not run")
        for i, a in enumerate(rest):
            opt, _, val = a.partition("=")
            if not val and opt in ("-k", "-m", "--deselect", "--ignore", "--ignore-glob") and i + 1 < len(rest):
                val = rest[i + 1]
            if opt == "-k" and val:
                out.append(f"tests not matching -k {val!r} were deselected")
            elif opt == "-m" and val:
                out.append(f"tests not matching the marker expression -m {val!r} were deselected")
            elif opt == "--deselect" and val:
                out.append(f"{val} was deselected")
            elif opt in ("--ignore", "--ignore-glob") and val:
                out.append(f"{val} was ignored (not collected)")
            elif opt in ("-x", "--exitfirst", "--maxfail", "--sw", "--stepwise", "--lf", "--last-failed", "--ff",
                         "--failed-first", "--nf", "--new-first"):
                out.append(f"{opt} changes which tests run or where the run stops")
        tests = (signature or {}).get("tests")
        counts = (signature or {}).get("reported_counts")
        if isinstance(tests, dict) and tests:
            off = sorted(t for t, o in tests.items() if o in ("skipped", "xfailed"))
            if off:
                out.append(f"{len(off)} test(s) were skipped or xfailed in this run: {', '.join(off[:5])}"
                           + (" ..." if len(off) > 5 else ""))
        elif isinstance(counts, dict):
            off = {k: v for k, v in counts.items() if k in ("skipped", "xfailed", "deselected") and v}
            if not counts:
                out.append("the reported output says no tests ran")
            elif off:
                out.append("the reported summary counts " + ", ".join(f"{v} {k}" for k, v in off.items())
                           + ": which tests is not known from it (per-test outcomes were not recorded)")
            else:
                out.append("per-test outcomes were not recorded (an agent-reported run); its summary counts no "
                           "skipped, xfailed or deselected test")
        elif signature is not None:
            out.append("per-test outcomes were not recorded: which tests were skipped or deselected (also by "
                       "the project's pytest configuration) is unknown")
    else:
        out.append("only the repro command ran; other tests were not run; which tests it skipped is unknown")
    out.append("one environment only (this machine, this interpreter); nothing else was tried")
    if run_by == "agent":
        out.append("Verinoda did not run this command: the outcome and output are as the agent reported them")
    return out


def cmd_text(argv) -> str:
    """A command as the user's shell takes it back (arguments quoted: ``-k "a or b"`` stays one argument)."""
    import os
    import shlex
    import subprocess

    argv = [str(a) for a in argv or []]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


# pytest options that change what is printed, not which tests run or how: a repro run with -vv or --tb=short
# is the same repro
_OUTPUT_FLAGS = {"-v", "-vv", "-vvv", "-vvvv", "-q", "-qq", "-qqq", "--verbose", "--quiet", "-s", "-l",
                 "--showlocals", "--no-header", "--no-summary", "--disable-warnings", "--disable-pytest-warnings",
                 "--full-trace"}
_OUTPUT_VALUE_OPTS = {"--tb", "--color", "--capture", "--durations", "--durations-min", "-r", "--code-highlight"}


def canonical_command(argv) -> list[str]:
    """The command with pytest's output-only options (-v/-q, --tb, -r, --color, --capture/-s, --durations,
    ``-p no:cacheprovider``) left out, for comparing a run with the session's repro."""
    argv = [str(a) for a in argv or []]
    if not experiments._is_pytest(argv):
        return argv
    at = 1 if experiments._exe_name(argv[0]) == "pytest" else 3
    out = argv[:at]
    rest = argv[at:]
    i = 0
    while i < len(rest):
        a = rest[i]
        opt = a.split("=", 1)[0]
        if a in _OUTPUT_FLAGS or re.fullmatch(r"-r[a-zA-Z]+", a) or ("=" in a and opt in _OUTPUT_VALUE_OPTS):
            i += 1
            continue
        if a in _OUTPUT_VALUE_OPTS and i + 1 < len(rest):
            i += 2
            continue
        if a == "-p" and i + 1 < len(rest) and rest[i + 1] == "no:cacheprovider":
            i += 2
            continue
        if a == "-pno:cacheprovider":
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def _same_command(a, b) -> bool:
    return canonical_command(a) == canonical_command(b)


def _is_repro(a: dict, sess: dict) -> bool:
    return _same_command(a.get("command") or [], sess["command"])


def _passed_text(a: dict, sess: dict) -> str:
    run = a.get("experiment_id") or "an agent-reported run"
    tree = (a.get("tree_hash") or "?")[:12]
    skipped = next((f for f in a.get("findings") or [] if f.get("rule") == "failing_tests_skipped"), None)
    if not _is_repro(a, sess):
        return (f"`{cmd_text(a.get('command') or [])}` passed at tree {tree} in {run} (attempt {a['n']}); that is not "
                "the session's repro command, so it says nothing about the repro")
    if list(a.get("command") or []) != list(sess["command"]):
        run += f", run as `{cmd_text(a.get('command') or [])}` (output options differ)"
    if skipped:
        return (f"the repro command exited 0 at tree {tree} in {run} (attempt {a['n']}), but "
                f"{len(skipped.get('tests') or {})} test(s) that failed before did not pass (skipped, xfailed or not "
                "run): that is not a passing repro")
    return f"the repro command passed at tree {tree} in {run} (attempt {a['n']})"


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
    agent = observed_output is not None
    _check_observed(observed_output, exit_code)
    if agent and not command and kind != "baseline":
        raise DebugError("an agent-reported run needs the command you ran, after -- (MCP: command); it is compared "
                         "with the session's repro command and never assumed to be it")
    argv = _argv(command) if command else list(sess["command"])
    base = sess["base_commit"]
    prior = _attempts(store, sess["id"])
    n = len(prior)
    aid = new_id("dba")
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
    ch = _tree_record(repo, base, ids, source, prior)
    patch_path = _write_patch(repo, aid, ch)
    reader = _reader(repo, base, ids, ch["tree_files"], source.get("commit"),
                     base_map=treestate.base_ids(repo, base) if source.get("kind") == "worktree" else None)
    steps["tree_s"] = round(time.perf_counter() - t_step, 3)
    t_step = time.perf_counter()
    sig = failsig.extract(stdout, stderr, outcome=outcome, files=ids.keys(), reader=reader, plugin_data=plugin_data,
                          roots=[str(repo)])
    notes: list[str] = []
    if outcome == "inconclusive" and code == 2 and experiments._is_pytest(argv) and \
            any(f.get("phase") == "collect" for f in sig.get("failures") or []):
        # pytest stopped at a collection error (an import or syntax error): for the repro that is a failing run
        # (the signature above was read as a failure already: only a pass is read differently)
        outcome = "fail"
        notes.append("pytest stopped at a collection error (exit 2): recorded as a failing run of the repro")
    if agent and experiments._is_pytest(argv):
        counts = failsig.pytest_counts(stdout)
        if counts is not None:
            sig["reported_counts"] = counts
    sig_exact, sig_coarse = failsig.keys(sig)
    is_loop = kind in LOOP_KINDS and source.get("kind") == "worktree"
    anywhere = trace_info.pop("_anywhere", None) if trace_info else None
    steps["signature_s"] = round(time.perf_counter() - t_step, 3)
    run_n = n
    for attempt_try in range(INSERT_RETRIES):
        # Another ledger command (a parallel tool call) may have recorded an attempt since this one started:
        # the attempt number and the loop rules are taken from the attempts as they are at the insert.
        t_step = time.perf_counter()
        if attempt_try:
            prior = _attempts(store, sess["id"])
            n = max((a["n"] for a in prior), default=-1) + 1
        loop_prior = [a for a in prior if a["kind"] in LOOP_KINDS and (a.get("copy_source") or {}).get(
            "kind", "worktree") == "worktree" and _same_command(a["command"], argv)]
        prev = loop_prior[-1] if loop_prior else None
        vs_prev = treestate.diff_trees(repo, base, prev["tree_files"] or {}, ch["tree_files"],
                                       base_map=treestate.base_ids(repo, base)) if (prev and is_loop) else []
        if anywhere is not None:
            touched = set(ch["tree_files"]) | {c["path"] for c in vs_prev}
            for a in prior:
                touched |= set(a.get("tree_files") or {})
            _reached_anywhere(trace_info, touched, anywhere)
        cur = {"n": n, "kind": kind, "outcome": outcome, "run_by": "agent" if agent else "verinoda",
               "experiment_id": exp["id"] if exp else None, "tree_hash": tree_hash, "code_hash": ch["code_hash"],
               "tree_files": ch["tree_files"],
               "signature": sig, "sig_exact": sig_exact, "sig_coarse": sig_coarse, "hypothesis": hypothesis,
               "hypothesis_terms": looprules.terms(hypothesis), "expect": expect, "vs_prev": vs_prev,
               "trace": trace_info, "command": argv, "vs_base": ch["vs_base"]}
        hist = [_as_rule_input(a) for a in loop_prior]
        if is_loop:
            ev = looprules.evaluate(hist, cur, max_no_progress=int(settings.get("max_no_progress", 3)))
        else:
            ev = {"progress": None, "findings": [], "stop": False, "stop_reason": None, "flaky": None,
                  "no_progress_streak": None, "assertion_lines": []}
        ev["findings"] = ev["findings"] + _order_dependent(prior, sess, argv, tree_hash, sig, n)
        questions = _questions(cur, prev, ev, loop_prior)
        maybe_flaky = any(f["rule"] == "possibly_flaky" for f in ev["findings"])
        strategies = propose(store, repo, sess, prior, cur, ev) if (ev["stop"] or ev["flaky"] or maybe_flaky) \
            else []
        steps["rules_s"] = round(time.perf_counter() - t_step, 3)
        overhead = round(time.perf_counter() - t_start - (exp["duration_s"] if exp else 0.0), 3)
        row = {"id": aid, "session_id": sess["id"], "n": n, "kind": kind, "hypothesis": hypothesis,
               "hypothesis_terms": cur["hypothesis_terms"], "command": argv, "expect": expect,
               "run_by": cur["run_by"], "copy_source": source, "tree_hash": tree_hash, "tree_files": ch["tree_files"],
               "patch_path": patch_path, "touched": {"vs_base": ch["vs_base"], "vs_prev": vs_prev,
                                                     "code_hash": ch["code_hash"],
                                                     "cost": {"overhead_s": overhead}},
               "experiment_id": cur["experiment_id"], "evidence_id": evidence_id,
               "runtime_run_id": trace_info.get("run_id"), "outcome": outcome, "exit_code": code,
               "duration_s": duration, "output_sha256": output_sha, "signature": sig, "sig_exact": sig_exact,
               "sig_coarse": sig_coarse, "trace": trace_info, "progress": ev["progress"],
               "findings": ev["findings"] + ([{"rule": "flaky", "strength": "flaky", **ev["flaky"]}] if ev["flaky"]
                                             else []),
               "stop": int(bool(ev["stop"])), "stop_reason": ev["stop_reason"], "strategies": strategies,
               "created_at": now()}
        t_step = time.perf_counter()
        try:
            store.insert("debug_attempts", row)
        except sqlite3.IntegrityError:
            if attempt_try + 1 >= INSERT_RETRIES:
                raise
            continue
        steps["store_s"] = round(time.perf_counter() - t_step, 3)
        break
    if n != run_n:
        _renumber(store, exp, evidence_id if agent else None, sess["id"], run_n, n)
    out = _attempt_view(row, ch.get("drift") or [], questions)
    out["session"] = sess["id"]
    if notes:
        out["notes"] = notes
    if exp and exp.get("limits"):
        left = [x for x in exp["limits"] if "left running" in x or "could not be deleted" in x]
        if left:
            out.setdefault("notes", []).extend(left)
    if ev.get("no_progress_streak") is not None:
        out["no_progress_streak"] = ev["no_progress_streak"]
    if outcome == "pass":
        out["result"] = _passed_text(row, sess)
        out["not_run"] = _not_run(argv, row["run_by"], sig)
        if source.get("kind") == "worktree" and _is_repro(row, sess):
            runs = _tree_runs(prior + [row], sess, tree_hash)
            if runs["disagree"]:
                out["result"] += f"; {runs['text']}"
    out["overhead_s"] = round(time.perf_counter() - t_start - (exp["duration_s"] if exp else 0.0), 3)
    out["cost"] = {"command_s": exp["duration_s"] if exp else None, "overhead_s": out["overhead_s"],
                   "steps": steps}
    return out


def _as_rule_input(a: dict) -> dict:
    return {"n": a["n"], "kind": a["kind"], "outcome": a["outcome"], "run_by": a["run_by"],
            "experiment_id": a.get("experiment_id"), "tree_hash": a.get("tree_hash"),
            "code_hash": (a.get("touched") or {}).get("code_hash"),
            "tree_files": a.get("tree_files") or {}, "signature": a.get("signature") or {},
            "sig_exact": a.get("sig_exact"), "sig_coarse": a.get("sig_coarse"), "hypothesis": a.get("hypothesis"),
            "hypothesis_terms": a.get("hypothesis_terms") or [], "expect": a.get("expect"),
            "vs_prev": (a.get("touched") or {}).get("vs_prev") or [], "trace": a.get("trace") or {},
            "progress": a.get("progress"), "command": a.get("command"),
            "rules": [f.get("rule") for f in a.get("findings") or []]}


def _questions(cur: dict, prev: dict | None, ev: dict, hist: list[dict] | None = None) -> list[dict]:
    """Questions only the user can answer (whether a test or the code states the intended behaviour)."""
    if not any(f["rule"] in ("test_edited", "failing_tests_skipped") for f in ev["findings"]):
        return []
    lines = ev.get("assertion_lines") or []
    test_paths = looprules.test_files_of(hist or [])
    test_side = [f"{x['path']}:{x['line']}: {x['text']}" for x in lines[:3]] or \
        [c["path"] for c in cur.get("vs_prev") or [] if c.get("test") or c["path"] in test_paths][:3]
    skipped = next((f for f in ev["findings"] if f["rule"] == "failing_tests_skipped"), None)
    if skipped:
        test_side += [f"{t}: {o}" for t, o in list((skipped.get("tests") or {}).items())[:3]]
    code_side = []
    for f in ((prev or {}).get("signature") or {}).get("failures") or []:
        # the code under test: the innermost frame outside test files, else where the test itself failed
        code = [fr for fr in f.get("frames") or [] if fr.get("path") and not treestate.is_test_file(fr["path"])
                and fr["path"] not in test_paths]
        if code:
            fr = code[-1]
            code_side.append(f"{fr['path']}:{fr.get('line')} ({fr.get('symbol')}; {f.get('exc')} raised at "
                             f"{f.get('path')}:{f.get('line')})")
        elif f.get("path") and f.get("line"):
            code_side.append(f"{f['path']}:{f['line']} (the test fails here: {f.get('exc')} in {f.get('symbol')})")
    why = [w for w, rule in (("an attempt changed an existing test instead of the code (test_edited)", "test_edited"),
                             ("tests that failed before were skipped or not run (failing_tests_skipped)",
                              "failing_tests_skipped"))
           if any(f["rule"] == rule for f in ev["findings"])]
    return [{"question": "Is the test's expectation or the code's behaviour the intended one?",
             "asked_because": "; ".join(why),
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
        view["note"] = ("flaky: loop rules and their stop are suspended (except test_edited and failing_tests_skipped) "
                        "until a rerun series of one tree comes out identical; the no-progress budget still applies")
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

def _symptom(sess: dict, attempts: list[dict], before: int | None = None, latest: bool = False) -> dict:
    """What the symptom looks like: the failing tests and coarse signature of attempt 0 when it failed on the
    working tree with the repro, else (or with ``latest``) of the latest failing loop attempt before ``before``."""
    cands = [a for a in attempts if (before is None or a["n"] < before) and a["kind"] in LOOP_KINDS and
             a["outcome"] == "fail" and _is_repro(a, sess) and
             (a.get("copy_source") or {}).get("kind", "worktree") == "worktree"]
    first = None if latest else next((a for a in cands if a["kind"] == "baseline"), None)
    a = first or (cands[-1] if cands else None)
    if a is None:
        return {"tests": set(), "coarse": None, "attempt": None}
    return {"tests": set((a.get("signature") or {}).get("failed_tests") or []), "coarse": a.get("sig_coarse"),
            "attempt": a["n"]}


def judge_at(row: dict, symptom: dict) -> tuple[str, str]:
    """A run of another tree (the base, a commit) judged against the symptom, not by its exit status alone:

    ``fail`` - the symptom's tests failed there (or, without per-test outcomes, the same coarse signature);
    ``part`` - some of the symptom's tests failed there and the others passed;
    ``pass`` - the run passed, or only other tests failed while every test of the symptom passed;
    ``other`` - it failed, but the symptom's tests did not run there or it failed differently: it cannot say;
    ``fail?`` - it failed and there is nothing to compare with (no per-test outcomes, no signature);
    anything else is the run's own outcome (inconclusive, timeout). Returns (verdict, why)."""
    out = row.get("outcome")
    if out == "pass":
        return "pass", "passed"
    if out != "fail":
        return str(out), f"the command could not tell ({out})"
    sig = row.get("signature") or {}
    tests = sig.get("tests") if isinstance(sig.get("tests"), dict) else {}
    want = sorted(symptom.get("tests") or [])
    failed = set(sig.get("failed_tests") or [])
    if want and tests:
        outcomes = {t: looprules._outcome_of(t, tests) for t in want}
        hit = [t for t, o in outcomes.items() if o in ("failed", "error")]
        passed = [t for t, o in outcomes.items() if o == "passed"]
        if hit and passed and len(hit) + len(passed) == len(want):
            return "part", (f"{len(passed)} of the symptom's {len(want)} failing test(s) passed there and "
                            f"{', '.join(hit[:3])}" + (" ..." if len(hit) > 3 else "") + " failed there too")
        if hit:
            return "fail", f"the symptom's test(s) {', '.join(hit[:3])} failed there"
        others = sorted(failed - set(want))
        if all(o == "passed" for o in outcomes.values()):
            return "pass", ("every test of the symptom passed there; it failed only other tests: "
                            + ", ".join(others[:3]) + (" ..." if len(others) > 3 else ""))
        off = [f"{t} ({o})" for t, o in outcomes.items() if o != "passed"]
        return "other", f"it failed, but the symptom's test(s) did not run there: {', '.join(off[:3])}"
    if want and failed & set(want):
        return "fail", f"the symptom's test(s) {', '.join(sorted(failed & set(want))[:3])} failed there"
    coarse = row.get("sig_coarse")
    if coarse and symptom.get("coarse"):
        if coarse == symptom["coarse"]:
            return "fail", "the same failure as the symptom's (exception and crash symbol)"
        return "other", f"it failed differently ({looprules._summ(row)})"
    return "fail?", "it failed; whether with the symptom's failure is unknown (no per-test outcomes or signature)"


def _base_outcome(sess: dict, attempts: list[dict]) -> tuple[str | None, int | None]:
    """How the repro did on the base tree for the symptom: a differential run (judged against the symptom:
    'fail' only when the symptom's failure is there), or a baseline on a clean tree (the base itself)."""
    for a in reversed(attempts):
        src = a.get("copy_source") or {}
        if a["kind"] == "differential" and (src.get("commit") == sess["base_commit"] or src.get("kind") == "agent_base"):
            # judged as the differential judged it: against the latest failure on the working tree before it
            verdict, _ = judge_at(a, _symptom(sess, attempts, before=a["n"], latest=True))
            if verdict in ("fail", "fail?", "part"):  # (part: some of the symptom predates the diff)
                return "fail", a["n"]
            return (verdict if verdict == "pass" else None), a["n"]
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
    """What one more run costs: the last run's command time plus Verinoda's own overhead around it (the copy of
    the tree, mostly - on a big tree that is most of the cost)."""
    for a in reversed(attempts):
        if a.get("duration_s"):
            over = ((a.get("touched") or {}).get("cost") or {}).get("overhead_s") or 0.0
            return float(a["duration_s"]) + max(0.0, float(over))
    return 5.0


def propose(store: Store, repo: Path, sess: dict, prior: list[dict], cur: dict, ev: dict) -> list[dict]:
    """Strategies for a stopped (or flaky) session, in the design's fixed order, each with why and cost."""
    sid = sess["id"]
    argv = list(sess["command"])
    attempts = prior + [{**cur, "copy_source": {"kind": "worktree"}, "duration_s": None}]
    run_s = _duration(prior)
    runnable, _ = _runnable(repo, argv)
    out: list[dict] = []
    maybe = any(f["rule"] == "possibly_flaky" for f in ev.get("findings") or [])
    if ev.get("flaky") or maybe:
        times = int(_cfg(repo).get("rerun_times", 5))
        why = ("the same tree gave different results" if ev.get("flaky") else
               "the same code (comments and docstrings aside) gave different results")
        out.append({"id": "rerun", "why": f"{why}: measure the pass rate before anything else",
                    "command": f"verinoda debug rerun --session {sid} --times {times}",
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
    # a test parametrised over a path has a new id in every copy: select its function instead
    failing = list(dict.fromkeys(
        t.split("[", 1)[0] if ("[" in t and re.search(r"[\\/]", t.split("[", 1)[1])) else t
        for t in sorted((cur.get("signature") or {}).get("failed_tests") or [])))
    if experiments._is_pytest(argv) and failing and not any("::" in a for a in argv):
        narrow = _without_selectors(argv) + failing[:5]
        out.append({"id": "minimal_repro", "why": "narrow the repro to the failing tests (faster runs; a test that "
                                                  "passes alone but fails in the suite is order-dependent)",
                    "command": f"verinoda debug try --session {sid} --kind probe --hypothesis \"the failing tests "
                               f"alone\" -- " + cmd_text(narrow),
                    "cost_estimate_s": round(run_s, 1), "runnable_by_verinoda": runnable})
    if any(f["rule"] in looprules.TEST_RULES for f in ev.get("findings") or []) or not out:
        out.append({"id": "ask_human", "why": "a test was edited or skipped, or nothing else narrows the cause: only "
                                              "the user knows the intended behaviour", "command": None,
                    "cost_estimate_s": 0})
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
    a Python function that a complete trace saw called nowhere in the run (no test, not at import time, no
    child process started) is marked ruled out."""
    is_test_path = treestate.is_test_file
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
    anywhere = set(tr.get("reached_any") or [])
    can_rule_out = looprules.trace_can_rule_out(tr)
    for (path, sym), s in found.items():
        if tr.get("complete") and path.endswith(".py"):
            if s["at"] in reached:
                s["evidence"].append(f"reached by the failing tests in run {tr.get('run_id')}")
            elif s["at"] in anywhere:
                s["evidence"].append(f"called in run {tr.get('run_id')} outside the failing tests' calls (at import "
                                     "time, or by another test): it can still set what they see")
            elif can_rule_out and (path, sym) in functions and not is_test_path(path) and not any(
                    e.startswith(("on the traceback", "crash site")) for e in s["evidence"]):
                s["ruled_out"] = (f"not called at all in the complete trace of run {tr.get('run_id')} (no test, not at "
                                  "import time; no child process started)")
    ranked = sorted(found.values(), key=lambda s: (bool(s.get("ruled_out")), is_test_path(s["at"].split("::")[0]),
                                                   -len(s["evidence"]), s["at"]))
    return ranked


def _order_dependent(prior: list[dict], sess: dict, argv: list[str], tree_hash: str | None, sig: dict,
                     n: int) -> list[dict]:
    """A narrowed pytest command passed tests the session's full repro failed on the same tree."""
    if _same_command(argv, sess["command"]) or not experiments._is_pytest(argv):
        return []
    passed = {t for t, o in (sig.get("tests") or {}).items() if o == "passed"}
    if not passed:
        return []
    for a in reversed(prior):
        if _is_repro(a, sess) and a.get("tree_hash") == tree_hash and a["outcome"] == "fail":
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

    Code before tests: hunks in files whose code is unchanged (only comments,
    docstrings or formatting of a Python file) come last, then hunks in test
    files. Tiers: on the failure's traceback, then reached by the failing tests
    in a complete trace, then the others. When the failing attempt still shows
    the symptom the session started with (the same coarse signature, or the same
    failing tests, as attempt 0), the hunks that were already there at attempt
    0 (``session_edit: false``) come before those the session's own attempts
    added; when the failure has changed, the tiers decide first and "already
    there" only breaks ties.
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
            no_code = bool(f.get("no_code_change"))
            out.append({"path": f["path"], "lines": lines, "at": f"{f['path']}:{lines[0]}", "symbols": syms,
                        "tier": tier, "why": why + ("; added by an attempt of this session" if session_edit
                                                    else ("; already there at attempt 0" if baseline else ""))
                        + ("; comments/docstrings only" if no_code else ""),
                        "session_edit": session_edit, "test_file": bool(f.get("test")), "no_code": no_code,
                        "removed": (h.get("removed") or [])[:5], "added": (h.get("added") or [])[:5]})
    same_symptom = False
    if baseline:
        coarse = baseline.get("sig_coarse")
        base_failed = set((baseline.get("signature") or {}).get("failed_tests") or [])
        same_symptom = (bool(coarse) and coarse == loop_attempt.get("sig_coarse")) or \
            (bool(base_failed) and base_failed == set((loop_attempt.get("signature") or {}).get("failed_tests") or []))
    if same_symptom:
        out.sort(key=lambda x: (x["no_code"], x["test_file"], x["session_edit"], x["tier"], x["path"], x["lines"][0]))
    else:
        out.sort(key=lambda x: (x["no_code"], x["test_file"], x["tier"], x["session_edit"], x["path"], x["lines"][0]))
    for i, x in enumerate(out, 1):
        x["rank"] = i
    return out


def _latest_loop_failure(attempts: list[dict]) -> dict | None:
    for a in reversed(attempts):
        if a["kind"] in LOOP_KINDS and a["outcome"] == "fail" and \
                (a.get("copy_source") or {}).get("kind", "worktree") == "worktree":
            return a
    return None


def _symptom_tests(repo: Path, sess: dict, attempts: list[dict], against: str) -> tuple[list[str], str | None]:
    """The files of the tests the symptom was recorded with (attempt 0's failing tests) that differ between
    ``against`` and attempt 0's tree - a new or changed test that the base does not have. Returns (the files to
    lay over the base, or why they cannot be: the working tree no longer has attempt 0's version)."""
    first = next((a for a in attempts if a["kind"] == "baseline"), None)
    if first is None or first["outcome"] != "fail" or not _is_repro(first, sess) or \
            (first.get("copy_source") or {}).get("kind", "worktree") != "worktree":
        return [], None
    files = sorted(p for p in looprules.test_files_of([first]) if (repo / p).is_file() or p in (first.get("tree_files")
                                                                                              or {}))
    if not files:
        return [], None
    at_base = treestate.read_blobs(repo, [treestate.blob_spec(repo, against, p) for p in files])
    tf = first.get("tree_files") or {}
    base_map = treestate.base_ids(repo, sess["base_commit"])
    out = []
    for p in files:
        then = tf[p] if p in tf else base_map.get(p)   # attempt 0's content id of the test file
        raw = at_base.get(treestate.blob_spec(repo, against, p))
        if then is None or (raw is not None and treestate.content_id(raw) == then):
            continue
        try:
            now_id = treestate.content_id((repo / p).read_bytes())
        except OSError:
            now_id = None
        if now_id != then:
            return [], (f"{p} (a failing test's file) differs at the base and has changed since attempt 0; the base "
                        "cannot be run with the test the symptom was recorded with")
        out.append(p)
    return out, None


def differential(store: Store, repo: Path, session_id: str | None = None, *, base: str | None = None,
                 prepare: bool = False, trace: bool = False, overlay: list[str] | None = None) -> dict:
    """Run the repro on a copy of the base commit; if it passes there, rank the diff's hunks.

    The test the symptom was recorded with is held fixed: when the files of attempt 0's failing tests differ
    at the base (a new or changed test), the working tree's versions are laid over the base copy (as long
    as they are still attempt 0's); ``overlay`` names such files explicitly. A base run in which the failing
    tests did not run (per-test outcomes) cannot judge them: the result is inconclusive, not "the cause is
    in the diff".

    With ``trace`` the base run is traced too and, when the failing tree has a complete trace (from
    the session's ``--trace`` or an ``observe`` probe, run first if missing), the failing tests' observed
    calls are compared: calls made only in the failing run, and calls made only at the base.
    """
    repo = Path(repo).resolve()
    sess = _session(store, session_id)
    against = treestate.resolve_commit(repo, base) if base else sess["base_commit"]
    if prepare:
        return _prepare_copy(repo, sess, against)
    out = {"session": sess["id"], "strategy": "differential", "base": against}
    auto: list[str] = []
    if overlay is None:
        auto, blocked = _symptom_tests(repo, sess, _attempts(store, sess["id"]), against)
        if blocked:
            return {**out, "status": "inconclusive", "conclusion": f"not run: {blocked}",
                    "next_step": "record the current tree with `verinoda debug try`, or give --overlay FILE explicitly"}
    lay = list(overlay) if overlay is not None else auto
    if trace and experiments._is_pytest(list(sess["command"])):
        lf = _latest_loop_failure(_attempts(store, sess["id"]))
        if lf is not None and not (lf.get("trace") or {}).get("complete"):
            observe(store, repo, sess["id"], hypothesis="instrumented run of the failing tree for the differential")
    res = attempt(store, repo, sess["id"], hypothesis=f"differential: the repro at the base {against[:12]}"
                  + (f" with {', '.join(lay)} laid over it" if lay else ""),
                  expect="pass", kind="differential", ref=against, trace=trace or None, overlay=lay or None)
    attempts = _attempts(store, sess["id"])
    loop_fail = _latest_loop_failure(attempts)
    base_row = next((a for a in attempts if a["n"] == res["attempt"]), {})
    out.update({"attempt": res["attempt"], "outcome_at_base": res["outcome"], "experiment_id": res.get("experiment_id")})
    with_tests = f" with {', '.join(lay)} laid over it" if lay else ""
    if lay:
        out["overlay"] = lay
        out["overlay_why"] = ("given with --overlay" if overlay is not None else
                              "the failing tests' files differ at the base: the base ran with the test the symptom "
                              "was recorded with (the working tree's version)")
    verdict, verdict_why = res["outcome"], ""
    if res["outcome"] == "fail" and loop_fail is not None:
        # the base fails - with the symptom, or only other tests (a long-red test is not the symptom's cause)?
        verdict, verdict_why = judge_at(base_row, {"tests": set((loop_fail.get("signature") or {}).get("failed_tests")
                                                                or []), "coarse": loop_fail.get("sig_coarse")})
        out["at_base"] = {"verdict": verdict, "why": verdict_why}
    if verdict in ("pass", "part") and loop_fail is not None:
        base_tests = (base_row.get("signature") or {}).get("tests")
        wanted = sorted((loop_fail.get("signature") or {}).get("failed_tests") or [])
        absent = [t for t in wanted if isinstance(base_tests, dict) and base_tests and
                  looprules._outcome_of(t, base_tests) not in ("passed", "failed", "error")]
        if absent:
            files = sorted({t.split("::", 1)[0] for t in absent if "::" in t})
            out["status"] = "inconclusive"
            out["conclusion"] = (f"the repro {'exits 0' if res['outcome'] == 'pass' else 'ran'} at the base "
                                 f"{against[:12]}{with_tests}, but the failing test(s) "
                                 f"{', '.join(absent[:3])} did not pass there ("
                                 + ", ".join(f"{base_tests.get(t, 'not run')}" for t in absent[:3])
                                 + "): they are new or changed, so the base run cannot judge them")
            out["next_step"] = (f"hold the test fixed: `verinoda debug differential --overlay {files[0]}`" if files and
                                not lay else "bisect with the test laid over old commits: `verinoda debug bisect "
                                             f"--overlay {files[0] if files else 'TEST_FILE'}`")
            return out
        first = next((a for a in attempts if a["kind"] == "baseline"), None)
        ranked = rank_hunks(repo, sess, _as_rule_input(loop_fail) | {"trace": loop_fail.get("trace") or {}},
                            against=against if against != sess["base_commit"] else None,
                            baseline=_as_rule_input(first) if first and first["outcome"] == "fail" and
                            (first.get("copy_source") or {}).get("kind", "worktree") == "worktree" else None)
        ranked = [h for h in ranked if h["path"] not in lay]  # the overlaid files ran on both sides
        for i, h in enumerate(ranked, 1):
            h["rank"] = i
        out["status"] = "cause_in_diff" if verdict == "pass" else "partly_in_diff"
        if res["outcome"] == "pass":
            out["conclusion"] = (f"the repro passes at the base {against[:12]}{with_tests} and fails at attempt "
                                 f"{loop_fail['n']}'s tree: the cause is in the diff between them (ranked below; "
                                 "run-scoped, one environment)")
        elif verdict == "part":
            out["conclusion"] = (f"at the base {against[:12]}{with_tests}, {verdict_why}: the cause of the failures "
                                 "that pass at the base is in the diff (ranked below; run-scoped, one environment); "
                                 "the others predate the working-tree changes (`verinoda debug bisect` searches the "
                                 "history for them)")
            out["fails_at_base_too"] = sorted(t for t in wanted if isinstance(base_tests, dict) and
                                              looprules._outcome_of(t, base_tests) in ("failed", "error"))
        else:
            out["conclusion"] = (f"the failing tests of attempt {loop_fail['n']} pass at the base {against[:12]}"
                                 f"{with_tests} and fail in its tree: the cause is in the diff between them (ranked "
                                 "below; run-scoped, one environment); the repro still exits non-zero at the base "
                                 f"because {verdict_why.split('; ', 1)[-1]}")
        if not isinstance(base_tests, dict) or not base_tests:
            out["limits"] = ["per-test outcomes were not recorded at the base: whether the failing tests exist and "
                             "ran there is not checked"]
        out["hunks"] = ranked[:LIST_CAP]
        out["hunks_total"] = len(ranked)
        if trace:
            out["trace_diff"] = _trace_diff(store, attempts, loop_fail, res["attempt"])
    elif res["outcome"] == "pass":
        out["status"] = "inconclusive"
        out["conclusion"] = (f"the repro passes at the base {against[:12]}{with_tests}, and no failing attempt on the "
                             "working tree is recorded to compare it with")
    elif verdict == "other":
        out["status"] = "inconclusive"
        out["conclusion"] = (f"the repro fails at the base {against[:12]}{with_tests}, but not with the symptom: "
                             f"{verdict_why}; whether the symptom's cause predates the working-tree changes is "
                             "unknown from this run")
        out["next_step"] = "narrow the repro to the symptom's failing tests (a new session), then run the differential"
    elif res["outcome"] == "fail":
        out["status"] = "fails_at_base"
        out["conclusion"] = (f"the repro also fails at the base {against[:12]}{with_tests}"
                             + (f" ({verdict_why})" if verdict_why else "") + ": the cause predates the "
                             "working-tree changes; bisect the history (`verinoda debug bisect"
                             + "".join(f" --overlay {p}" for p in lay) + "`)")
    else:
        out["status"] = "inconclusive"
        out["conclusion"] = f"inconclusive at the base ({res['outcome']}); the diff cannot be judged from this run"
    return out


def _edges_of(store: Store, run_id: str, test: str) -> set[tuple[str, str]]:
    """In-repo (caller, callee) pairs observed in ``test``'s call phase of a traced run."""
    out = set()
    ctx = f"{test}|call"
    for r in store.all("SELECT caller_path, caller_qual, callee_path, callee_qual, tests FROM runtime_calls "
                       "WHERE run_id = ? AND callee_path != '<ext>' AND caller_path != '<ext>'", (run_id,)):
        if ctx in (r.get("tests") or []):
            out.add((f"{r['caller_path']}::{failsig.clean_qual(r['caller_qual'])}",
                     f"{r['callee_path']}::{failsig.clean_qual(r['callee_qual'])}"))
    return {e for e in out if e[0] != e[1]}


def _trace_diff(store: Store, attempts: list[dict], failing: dict, base_n: int) -> dict:
    """The failing tests' observed calls: only in the failing run vs only in the passing run at the base."""
    traced = [a for a in attempts if a["kind"] in LOOP_KINDS and (a.get("trace") or {}).get("complete")
              and a["outcome"] == "fail" and a.get("tree_hash") == failing.get("tree_hash")]
    base_row = next((a for a in attempts if a["n"] == base_n), None) or {}
    bt = base_row.get("trace") or {}
    if not traced or not bt.get("complete"):
        return {"status": "unknown", "why": "a complete trace of both the failing tree and the base is needed"}
    ft = traced[-1]["trace"]
    out = {"failing_run": ft.get("run_id"), "base_run": bt.get("run_id"), "tests": {},
           "limits": ["observed calls of these two runs only; a call missing from the failing run can also be "
                      "the effect of the failure (execution stopped), not its cause"]}
    for test in (ft.get("failing") or [])[:3]:
        f_edges = _edges_of(store, ft["run_id"], test)
        b_edges = _edges_of(store, bt["run_id"], test)
        out["tests"][test] = {
            "only_when_failing": [f"{a} -> {b}" for a, b in sorted(f_edges - b_edges)][:LIST_CAP],
            "only_when_passing": [f"{a} -> {b}" for a, b in sorted(b_edges - f_edges)][:LIST_CAP]}
    return out


def _prepare_copy(repo: Path, sess: dict, commit: str) -> dict:
    """A copy of ``commit`` under ``.verinoda/runs/`` for a command Verinoda may not run itself."""
    d = runs_dir(repo) / f"base-{commit[:12]}"
    skipped: list[dict] = []
    if not d.exists():
        d.mkdir(parents=True)
        experiments._copy_commit(repo, commit, d, skipped=skipped)
    cmd = cmd_text(sess["command"])
    limits = ["Verinoda does not run this command (not allowlisted); the copy is a plain file copy of the commit "
              "under .verinoda/, without git's directory (no path spelled like .git is written)"]
    if skipped:
        limits.append(f"{len(skipped)} file(s) of the commit could not be written here: "
                      + ", ".join(s["path"] for s in skipped[:5]))
    return {"session": sess["id"], "strategy": "differential", "prepared_copy": str(d), "base": commit,
            "next_step": (f"run `{cmd}` in {d} yourself, then report it: `verinoda debug try --kind differential "
                          f"--hypothesis \"repro at the base\" --observed-output FILE --exit-code N -- {cmd}` "
                          "(recorded as agent-reported)"),
            "limits": limits}


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


def _recorded_at(attempts: list[dict], sess: dict, commit: str, overlay: list[str] | None) -> dict | None:
    """The latest pass/fail run Verinoda made of the session's repro on a copy of ``commit`` (same overlay), or
    - for the session base without overlay - on a working tree identical to it (no file differs)."""
    want = sorted(overlay or [])
    for a in reversed(attempts):
        src = a.get("copy_source") or {}
        if a["run_by"] != "verinoda" or not _is_repro(a, sess) or a["outcome"] not in ("pass", "fail"):
            continue
        if src.get("kind") == "commit" and src.get("commit") == commit and sorted(src.get("overlay") or []) == want:
            return a
        if src.get("kind", "worktree") == "worktree" and commit == sess["base_commit"] and not want and \
                not (a.get("tree_files") or {}) and a["kind"] in LOOP_KINDS:
            return a
    return None


def bisect(store: Store, repo: Path, session_id: str | None = None, *, good: str | None = None,
           bad: str | None = None, overlay: list[str] | None = None, max_runs: int | None = None) -> dict:
    """Binary search over first-parent commits for the first one where the repro fails (throw-away copies).

    Both ends are established by a run before the search: the bad end (default: the session base) must
    fail and the good end must pass - a run recorded earlier in the session on that commit (same overlay)
    counts, anything else is run now. If an end does not behave, bisect says so and stops ("unknown").
    Commits where the command cannot run (inconclusive, timeout) are skipped and said so. Without ``good``
    Verinoda steps back (1, 2, 4, ... commits) to find a passing one. Returns the first bad commit, the runs
    that show each side, and its hunks ranked by the failure's traceback.
    """
    repo = Path(repo).resolve()
    sess = _session(store, session_id)
    budget = max(1, int(max_runs if max_runs is not None else _cfg(repo).get("bisect_max_runs", 12)))
    bad_sha = treestate.resolve_commit(repo, bad) if bad else sess["base_commit"]
    runs: list[dict] = []
    skipped: list[str] = []
    notes: list[str] = []
    attempts = _attempts(store, sess["id"])
    head = {"session": sess["id"], "strategy": "bisect"}
    symptom = _symptom(sess, attempts)
    unconfirmed: list[str] = []
    partly: dict[str, str] = {}   # commits where only some of the symptom's failing tests fail

    def outcome_at(commit: str, why: str) -> tuple[str | None, int | None]:
        """pass / fail for the symptom at ``commit`` (a run that fails only other tests while the symptom's
        tests pass is a pass for the symptom; one that fails differently is 'other' and skipped)."""
        rec = _recorded_at(_attempts(store, sess["id"]), sess, commit, overlay)
        if rec is None:
            if sum(1 for r in runs if not r.get("recorded")) >= budget:
                return None, None
            got = _run_at(store, repo, sess, commit, why, overlay)
            rec = _attempts_by_n(store, sess["id"]).get(got["attempt"]) or {"outcome": got["outcome"],
                                                                            "n": got["attempt"]}
            entry = {"commit": commit, "outcome": rec["outcome"], "attempt": rec["n"]}
        else:
            entry = {"commit": commit, "outcome": rec["outcome"], "attempt": rec["n"], "recorded": True}
        verdict, vwhy = judge_at(rec, symptom)
        if rec["outcome"] == "fail" and verdict != "fail":
            entry["for_the_symptom"] = f"{verdict}: {vwhy}"
        runs.append(entry)
        if verdict == "fail?":
            unconfirmed.append(commit)
            verdict = "fail"
        elif verdict == "part":  # some of the symptom's tests fail there: the search is for the first such commit
            partly[commit] = vwhy
            verdict = "fail"
        return verdict, rec["n"]

    def ran() -> int:
        return sum(1 for r in runs if not r.get("recorded"))

    def stop(why: str, next_step: str) -> dict:
        return {**head, "status": "unknown", "runs": runs, "runs_total": ran(), "conclusion": why,
                "next_step": next_step}

    o_bad, n_bad = outcome_at(bad_sha, "the bad end: does the repro fail here?")
    if o_bad is None:
        return stop("the run budget ended before the bad end was run", "raise --max-runs")
    if o_bad == "pass":
        return stop(f"the repro passes at the bad end {bad_sha[:12]} (attempt {n_bad}): there is no failing commit to "
                    "search for" + ("; it passes at the session base, so the failure comes from the working tree's "
                                    "changes, not from history" if not bad else ""),
                    "compare the working tree with that commit: `verinoda debug differential`" if not bad else
                    "give --bad <a commit where the repro fails>")
    if o_bad == "other":
        return stop(f"the repro fails at the bad end {bad_sha[:12]} (attempt {n_bad}), but not with the symptom: "
                    f"{runs[-1].get('for_the_symptom', '')}; there is nothing of the symptom's to search for there",
                    "give --bad <a commit where the symptom's tests fail>")
    if o_bad != "fail":
        return stop(f"the repro could not run at the bad end {bad_sha[:12]} ({o_bad}, attempt {n_bad})",
                    "give --bad <a commit where the repro runs and fails>")
    n_good = None
    if good:
        good_sha = treestate.resolve_commit(repo, good)
    else:
        rec = next((a for a in reversed(attempts) if a["outcome"] == "pass" and
                    (a.get("copy_source") or {}).get("kind") == "commit" and a["run_by"] == "verinoda" and
                    _is_repro(a, sess) and sorted((a.get("copy_source") or {}).get("overlay") or []) ==
                    sorted(overlay or [])), None)
        good_sha = (rec.get("copy_source") or {}).get("commit") if rec else None
    if good_sha is None:  # step back through first-parent history
        chain = [ln for ln in (treestate._git(repo, "rev-list", "--first-parent", "--max-count", "65", bad_sha, "--")
                               or "").split() if ln]
        step = 1
        while step < len(chain) and ran() < budget:
            c = chain[step]
            o, nn = outcome_at(c, "looking for a passing commit")
            if o == "pass":
                good_sha, n_good = c, nn
                break
            if o == "fail":
                bad_sha, n_bad = c, nn
            elif o is not None:
                skipped.append(c)
            step *= 2
        if good_sha is None:
            return {**head, "status": "unknown", "runs": runs, "runs_total": ran(),
                    "conclusion": "no passing commit was found within the run budget",
                    "why": "no passing commit was found within the run budget",
                    "next_step": "give --good <commit where the repro passed>"}
    else:
        o_good, n_good = outcome_at(good_sha, "the good end: does the repro pass here?")
        if o_good is None:
            return stop("the run budget ended before the good end was run", "raise --max-runs")
        if o_good != "pass":
            return stop(f"the repro does not pass at the good end {good_sha[:12]} ({o_good}, attempt {n_good}): "
                        "the range has no passing side", "give --good <an older commit where the repro passes>")
    if not _is_ancestor(repo, good_sha, bad_sha):
        raise DebugError(f"{good_sha[:12]} is not an ancestor of {bad_sha[:12]}; bisect needs good before bad")
    commits = _rev_list(repo, good_sha, bad_sha)
    if not commits:
        raise DebugError("no commits between good and bad")
    estimate = math.ceil(math.log2(len(commits) + 1))
    lo, hi = -1, len(commits) - 1   # commits[hi] fails (bad, run above), lo = good (run above)
    tested: dict[int, str] = {hi: "fail"}
    seen_at: dict[int, int | None] = {hi: n_bad, -1: n_good}
    while hi - lo > 1 and ran() < budget:
        mid = (lo + hi) // 2
        order = [mid] + [x for k in range(1, hi - lo) for x in (mid + k, mid - k) if lo < x < hi]
        verdict = None
        for i in order:
            if i in tested or ran() >= budget:
                continue
            o, nn = outcome_at(commits[i], "does the repro fail here?")
            if o in ("pass", "fail"):
                tested[i] = o
                seen_at[i] = nn
                verdict = (i, o)
                break
            if o is not None:
                skipped.append(commits[i])
        if verdict is None:
            if ran() < budget:
                notes.append("every commit left in the range was skipped (the command could not run there)")
            break
        i, o = verdict
        if o == "pass":
            lo = i
        else:
            hi = i
    first_bad = commits[hi]
    status = "found" if hi - lo == 1 else "range"
    out = {**head, "status": status, "good": good_sha, "bad": bad_sha,
           "runs": runs, "runs_total": ran(), "skipped": skipped,
           "cost": {"estimate_runs": estimate + 2, "estimate_s": round((estimate + 2) * _duration(attempts), 1),
                    "measured_s": round(sum((_attempts_by_n(store, sess["id"]).get(r["attempt"]) or {})
                                            .get("duration_s") or 0 for r in runs if not r.get("recorded")), 1)},
           "limits": ["each commit ran in a copy of that commit only"
                      + (f" with the working-tree files {', '.join(overlay)} laid over it" if overlay else "")
                      + "; dependency or environment changes between commits are not modelled",
                      "a commit where the command could not run is skipped, not judged"]}
    if status == "found":
        parent = commits[hi - 1] if hi > 0 else good_sha
        n_parent = seen_at.get(hi - 1)
        n_first = seen_at.get(hi)
        subject = (treestate._git(repo, "log", "-1", "--format=%s", first_bad, "--") or "").strip()
        out["first_bad_commit"] = {"commit": first_bad, "subject": subject, "parent": parent,
                                   "fail_attempt": n_first, "parent_pass_attempt": n_parent}
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
        out["conclusion"] = (f"the repro passes at {parent[:12]} (attempt {n_parent}) and fails at {first_bad[:12]} "
                             f"({subject}; attempt {n_first}): that commit's change is where to look")
        if first_bad in partly:
            out["conclusion"] += (f" - for part of the symptom only: at {first_bad[:12]} {partly[first_bad]}; the "
                                  "other failing tests may start at a later commit (bisect again with the repro "
                                  "narrowed to them)")
    else:
        out["conclusion"] = (f"the first failing commit is between {commits[lo][:12] if lo >= 0 else good_sha[:12]} "
                             f"(passes, attempt {seen_at.get(lo)}) and {first_bad[:12]} (fails, attempt "
                             f"{seen_at.get(hi)}); the run budget or skipped commits left it open")
    judged = [r for r in runs if r.get("for_the_symptom")]
    if judged:
        out["limits"].append("pass and fail are judged on the symptom's tests (attempt "
                             f"{symptom.get('attempt')}): " + "; ".join(f"{r['commit'][:12]} {r['for_the_symptom']}"
                                                                         for r in judged[:3]))
        other = [r["commit"][:12] for r in judged if r["for_the_symptom"].startswith("other")]
        if other:
            notes.append(f"skipped, failing differently from the symptom: {', '.join(other[:5])}")
    if unconfirmed:
        out["limits"].append("whether the failure at " + ", ".join(c[:12] for c in unconfirmed[:3]) + " is the "
                             "symptom's is unknown (no per-test outcomes or signature to compare): counted as failing")
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
    rows = _attempts_by_n(store, sess["id"])
    mine = [_as_rule_input(rows[r["attempt"]]) for r in res]
    passed = sum(1 for r in res if r["outcome"] == "pass")
    sigs = {a.get("sig_coarse") for a in mine if a["outcome"] == "fail"}
    trees = {r["tree"]["hash"] for r in res}
    tree = res[-1]["tree"]["hash"]
    same_all = [a for a in rows.values() if a.get("tree_hash") == tree and a["kind"] in LOOP_KINDS
                and _is_repro(a, sess) and a["outcome"] in ("pass", "fail")]
    same = [a for a in same_all if a["run_by"] != "agent"]   # Verinoda's own runs of this tree
    reports = [a for a in same_all if a["run_by"] == "agent"]
    all_pass = sum(1 for a in same if a["outcome"] == "pass")
    results = [_as_rule_input(a) for a in same]
    out = {"session": sess["id"], "strategy": "rerun", "runs": k, "passed": passed, "pass_rate": round(passed / k, 2),
           "distinct_failures": len(sigs), "attempts": [r["attempt"] for r in res],
           "tree": tree, "tree_runs": len(same), "tree_passed": all_pass,
           "flaky": res[-1].get("flaky") is not None,
           "limits": ["a rerun series shows flakiness on this machine only; it cannot prove a test is stable",
                      "runs of this tree Verinoda made are counted; agent-reported runs are listed, not counted"]}
    if len(trees) > 1:
        out["note"] = "the tree changed during the reruns; the series mixes trees"
    series_same = all(looprules.same_result(mine[0], a) for a in mine[1:])
    history_same = all(looprules.same_result(results[0], a) for a in results[1:]) if results else True
    if series_same and history_same:
        out["conclusion"] = f"stable: all {len(same)} run(s) Verinoda made of this tree gave the same result"
    else:
        out["conclusion"] = (f"flaky: this tree passed {all_pass} of {len(same)} runs Verinoda made "
                             f"({passed} of {k} in this series)")
    if reports:
        agree = [a["n"] for a in reports if same and a["outcome"] == same[-1]["outcome"]]
        disagree = [a["n"] for a in reports if a["n"] not in agree]
        if disagree:
            out["agent_reports"] = (f"agent-reported run(s) of this tree (attempt {', '.join(map(str, disagree))}) "
                                    "disagree with Verinoda's runs; they are not counted")
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
    elsewhere: list[str] = []
    anywhere = set(tr.get("reached_any") or [])
    for c in (row.get("touched") or {}).get("vs_base") or []:
        for s in c.get("symbols") or []:
            if c["path"].endswith(".py") and (c.get("kinds") or {}).get(s) == "def":
                key = f"{c['path']}::{s}"
                reached_by[key] = [t_ for t_ in tr.get("failing") or [] if key in ((tr.get("reached") or {}).get(t_)
                                                                                   or [])]
                if not reached_by[key] and key in anywhere:
                    elsewhere.append(key)
    res["edits_reached"] = {"complete_trace": bool(tr.get("complete")), "by_failing_tests": reached_by,
                            "called_elsewhere": elsewhere, "child_processes": tr.get("spawns") or [],
                            "note": "run-scoped: an empty list in a complete trace means the failing tests' own calls "
                                    "did not reach that function in this run; called_elsewhere lists those called at "
                                    "import time or by other tests (they can still set what the failing tests see); "
                                    "child processes started by tests run untraced"}
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
    passing = [a for a in attempts if a["outcome"] == "pass" and a["kind"] in LOOP_KINDS and _is_repro(a, sess)
               and (a.get("copy_source") or {}).get("kind", "worktree") == "worktree"]
    if passing:
        p = passing[-1]
        out["result"] = _passed_text(p, sess) + ("; that is the current tree" if p.get("tree_hash") == cur_state["hash"]
                                                 else "; the working tree has changed since")
        out["not_run"] = _not_run(list(p["command"]), p["run_by"], p.get("signature"))
        runs = _tree_runs(attempts, sess, p.get("tree_hash"))
        out["tree_runs"] = runs
        if runs["disagree"]:
            out["result"] += f"; {runs['text']}"
    others = [a for a in attempts if a["outcome"] == "pass" and a["kind"] in LOOP_KINDS and not _is_repro(a, sess)]
    if others:
        out["other_commands_passed"] = [_passed_text(a, sess) for a in others[-3:]]
    if last_loop is not None:
        fl = next((f for f in last_loop.get("findings") or [] if f.get("rule") == "flaky"), None)
        if fl:
            out["flaky"] = fl.get("text")
    if sess["status"] != "open":
        out["closed"] = {"at": sess.get("closed_at"), "resolved_by": sess.get("resolved_by"),
                         "note": sess.get("close_note")}
    return out


def _tree_runs(attempts: list[dict], sess: dict, tree: str | None) -> dict:
    """Every recorded run of the session's repro on one working tree: Verinoda's and agent-reported."""
    same = [a for a in attempts if tree and a.get("tree_hash") == tree and a["kind"] in LOOP_KINDS
            and _is_repro(a, sess) and (a.get("copy_source") or {}).get("kind", "worktree") == "worktree"]
    own = [a for a in same if a["run_by"] != "agent"]
    reports = [a for a in same if a["run_by"] == "agent"]

    def count(xs: list[dict]) -> dict:
        return {"passed": [a["n"] for a in xs if a["outcome"] == "pass"],
                "not_passed": [f"{a['n']}: {a['outcome']}" for a in xs if a["outcome"] != "pass"]}
    v, r = count(own), count(reports)
    disagree = bool(v["not_passed"]) or (bool(r["not_passed"]) and not own)
    parts = []
    if own:
        parts.append(f"Verinoda ran this tree {len(own)} time(s): {len(v['passed'])} passed"
                     + (f", not passed in attempt(s) {', '.join(v['not_passed'])}" if v["not_passed"] else ""))
    if reports:
        parts.append(f"{len(reports)} agent-reported run(s)"
                     + (f", not passed in attempt(s) {', '.join(r['not_passed'])}" if r["not_passed"] else "")
                     + (" (not counted where Verinoda ran the tree)" if own else ""))
    return {"verinoda": v, "agent": r, "disagree": disagree, "text": "; ".join(parts)}


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


def _test_changes_since_baseline(repo: Path, sess: dict, attempts: list[dict], a: dict) -> tuple[list[dict], set[str]]:
    """The test_edited finding (if any) between attempt 0's tree and ``a``'s: tests changed on the way; and the
    test files that changed (or went) between them."""
    first = next((x for x in attempts if x["kind"] == "baseline"), None)
    if first is None or first["n"] == a["n"]:
        return [], set()
    changes = treestate.diff_trees(repo, sess["base_commit"], first.get("tree_files") or {}, a.get("tree_files") or {},
                                   base_map=treestate.base_ids(repo, sess["base_commit"]))
    hist = [_as_rule_input(first)]
    found, _ = looprules._test_edited(hist, {"n": a["n"], "vs_prev": changes, "run_by": a["run_by"],
                                             "experiment_id": a.get("experiment_id"), "command": a.get("command")})
    tests = looprules.test_files_of(hist)
    return found, {c["path"] for c in changes if c.get("test") or c["path"] in tests}


def close(store: Store, repo: Path, session_id: str | None = None, *, resolved_by: int | None = None,
          abandoned: bool = False, note: str | None = None, accept_test_edit: bool = False) -> dict:
    """Close a session. Resolved needs a pass of the session's repro command on the current tree in which the
    tests that failed before passed (not skipped), no other run of that tree by Verinoda that did not pass,
    and - when tests were changed since attempt 0 - ``accept_test_edit`` (the user's decision, recorded)."""
    repo = Path(repo).resolve()
    sess = _session(store, session_id)
    if sess["status"] != "open":
        raise DebugError(f"session {sess['id']} is already {sess['status']}")
    if bool(abandoned) == (resolved_by is not None):
        raise DebugError("give exactly one of --resolved-by N or --abandoned")
    if abandoned:
        store.update("debug_sessions", sess["id"], {"status": "abandoned", "closed_at": now(), "close_note": note})
        return {"session": sess["id"], "status": "abandoned", "note": note}
    attempts = _attempts(store, sess["id"])
    a = next((x for x in attempts if x["n"] == int(resolved_by)), None)
    if a is None:
        raise DebugError(f"session {sess['id']} has no attempt {resolved_by}")
    if a["outcome"] != "pass" or a["kind"] not in LOOP_KINDS or \
            (a.get("copy_source") or {}).get("kind", "worktree") != "worktree":
        raise DebugError(f"attempt {resolved_by} is not a passing run of the repro on the working tree "
                         f"({a['kind']}, {a['outcome']}); it cannot resolve the session")
    if a["kind"] == "baseline":
        raise DebugError(f"attempt {resolved_by} is the baseline: the repro passed before anything was changed, so "
                         "the symptom never reproduced and there is nothing it resolved; close with --abandoned and "
                         "start a session with a repro that fails")
    if not _is_repro(a, sess):
        raise DebugError(f"attempt {resolved_by} ran `{cmd_text(a['command'])}`, not the session's repro command "
                         f"`{cmd_text(sess['command'])}`; only a pass of the repro resolves the session (`verinoda "
                         "debug try` without a command runs it)")
    shown = [x for x in attempts if x["n"] < a["n"] and x["kind"] in LOOP_KINDS and x["outcome"] in SYMPTOM_OUTCOMES
             and _is_repro(x, sess) and (x.get("copy_source") or {}).get("kind", "worktree") == "worktree"]
    if not shown:
        raise DebugError(f"no run of the repro failed in this session before attempt {resolved_by}: the symptom was "
                         "never observed, so a pass resolves nothing; close with --abandoned")
    cur = treestate.current(repo, store=store)
    if cur["hash"] != a.get("tree_hash"):
        raise DebugError(f"the working tree changed since attempt {resolved_by} passed (tree "
                         f"{(a.get('tree_hash') or '')[:12]} then, {cur['hash'][:12]} now); run `verinoda debug try` "
                         "on the current tree first")
    runs = _tree_runs(attempts, sess, a.get("tree_hash"))
    if runs["disagree"]:
        by = "Verinoda made" if runs["verinoda"]["not_passed"] else "recorded (agent-reported)"
        raise DebugError(f"the same tree did not pass in every run {by} ({runs['text']}): the result is flaky; "
                         "`verinoda debug rerun` measures the pass rate (or report more runs), and `--abandoned "
                         "--note ...` closes the session without claiming a resolution")
    if a["run_by"] == "agent" and _runnable(repo, list(sess["command"]))[0]:
        own = [x["n"] for x in attempts if x["run_by"] == "verinoda" and x["outcome"] == "pass" and
               x.get("tree_hash") == a.get("tree_hash") and x["kind"] in LOOP_KINDS and _is_repro(x, sess)]
        raise DebugError(f"attempt {resolved_by} is a run the agent reported, and Verinoda can run this repro itself: "
                         + (f"close with attempt {own[-1]}, Verinoda's passing run of the same tree" if own else
                            "confirm the pass with a run Verinoda makes (`verinoda debug try`), then close with that "
                            "attempt"))
    edits, edited_tests = _test_changes_since_baseline(repo, sess, attempts, a)
    skipped = next((f for f in a.get("findings") or [] if f.get("rule") == "failing_tests_skipped"), None)
    gone: list[str] = []
    if skipped:
        missing = skipped.get("tests") or {}
        # failing tests the accepted test change removed or renamed: the user's decision covers them
        gone = [t for t, o in missing.items() if o == "not run" and t.split("::", 1)[0] in edited_tests]
        if not (accept_test_edit and edits and len(gone) == len(missing)):
            raise DebugError(f"attempt {resolved_by} exited 0, but tests that failed before did not pass in it: "
                             + ", ".join(f"{t} ({o})" for t, o in list(missing.items())[:5])
                             + ("" if accept_test_edit else
                                "; if the user decided that a test change removed or renamed them, close with "
                                "--accept-test-edit (recorded)")
                             + ("; --accept-test-edit covers failing tests an accepted test change removed or "
                                "renamed, not skipped or xfailed ones" if accept_test_edit else ""))
    if edits and not accept_test_edit:
        raise DebugError(f"tests changed since attempt 0 in the tree that passed: {edits[0]['text']}. Whether the "
                         "test or the code is right is the user's decision: after the user decided for the test "
                         "change, close with --accept-test-edit (recorded); otherwise restore the test")
    close_note = note
    if edits:
        at = ", ".join(f"{x['path']}:{x['line']}" if x.get("line") else x["path"]
                       for x in (edits[0].get("lines") or [])[:3]) or edits[0]["text"][:120]
        close_note = ((note + " ") if note else "") + f"[closed with a test change the user accepted: {at}]"
        if gone:
            close_note += f" [earlier failing tests the accepted change removed or renamed: {', '.join(gone[:5])}]"
    store.update("debug_sessions", sess["id"], {"status": "resolved", "resolved_by": int(resolved_by),
                                                "closed_at": now(), "close_note": close_note})
    result = _passed_text({k: v for k, v in a.items() if k != "findings"} if gone else a, sess)
    if gone:
        result += (f"; the earlier failing test(s) {', '.join(gone[:3])} did not run in it: the test change the "
                   "user accepted removed or renamed them")
    out = {"session": sess["id"], "status": "resolved", "result": result + "; that is the current tree",
           "not_run": _not_run(list(a["command"]), a["run_by"], a.get("signature")), "note": close_note,
           "tree_runs": runs["text"]}
    if edits:
        out["accepted_test_edit"] = edits[0]["text"]
    if a["run_by"] == "agent":
        out["basis"] = "agent-reported run: Verinoda did not run the command"
    return out


def compact(res: dict) -> dict:
    """The MCP view: the same answer, JSON-safe."""
    return json.loads(json.dumps(res, default=str))
