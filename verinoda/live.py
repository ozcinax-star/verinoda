"""Verinoda that stays with the agent while it works: the commands behind ``verinoda live`` (docs/DESIGN.md D137).

The Claude Code mod *verinoda-live* (the Symbiosis repository) keeps the index fresh while the agent edits, checks
each edit for names that do not exist, reviews commits, and puts Verinoda in front of a code question. A mod is a
Claude Code plugin, so Codex users had none of it. Here the same behaviour is commands any agent can run, and one
hook command (``live hook``) that Claude Code's and Codex's hook systems both call (``live install``):

====================  ===========================================  =========================================
feature               command                                      hook event that runs it
====================  ===========================================  =========================================
fresh index           ``live refresh``                             ``Stop`` (and ``SessionStart``)
name check on edits   ``live check [FILE...]``                     ``PostToolUse`` of an edit tool
auto-context          ``live context "PROMPT"``                    ``UserPromptSubmit``
review of commits     ``live review``, ``live guard on|off``       ``PostToolUse`` of a shell command
state                 ``live status``, ``live config``             -
====================  ===========================================  =========================================

Settings are the project's, in ``.verinoda/live.json`` (``auto``: off|nudge|search, ``check``, ``guard``,
``refresh``, ``offer``), changed by ``live auto``, ``live guard`` and ``live config`` and read by every command and hook (``offer``: a vague request for
"better" gets the one-line offer of ``verinoda improve``).
The defaults are the mod's: the index refresh and the name check on, the review of commits and the auto-context off,
because measuring them (``benchmarks/results/agent-compare-*`` in the Symbiosis repository) showed no win on real
code for the auto-context and a check that never fired because the agent read the code first.

A hook never fails the agent's step: every error is swallowed and the exit status is 0; it answers with
``hookSpecificOutput.additionalContext`` (the text the model reads with the tool's result or the prompt) or with
nothing at all. What the model reads is English.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

LIVE_DIR = "live"
SETTINGS_NAME = "live.json"
DEFAULTS: dict = {"refresh": True, "check": True, "guard": False, "auto": "off", "offer": False}
AUTO_MODES = ("off", "nudge", "search")
CHECKABLE = re.compile(r"\.(py|pyi|java|kt|kts)$", re.I)
CHECK_TIMEOUT_S = 60
QUERY_TIMEOUT_S = 25
REVIEW_TIMEOUT_S = 600
CHECK_NOTE_MAX = 8
NUDGE_MAX_CHARS = 20_000
SEARCH_MAX_CHARS = 2_000
EDIT_TOOLS = re.compile(r"^(Edit|Write|MultiEdit|NotebookEdit|apply_patch|ApplyPatch|edit|write)$")
SHELL_TOOLS = re.compile(r"^(Bash|PowerShell|shell|local_shell|exec_command|run_shell_command|Shell)$")
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update) File: (.+?)\s*$", re.M)

_END = r"(?=[\s?.,!:;]|$)"
_QUESTION_START = re.compile(
    r"^(how|why|where|what|which|who|when|does|do|is|are|can|could|should|explain|show|find|list|trace|"
    r"nasıl|neden|niye|niçin|nerede|nereden|nereye|ne|neler|hangi|kim|kimler|açıkla|göster|bul|listele)" + _END,
    re.I)
_QUESTION_PARTICLE = re.compile(r"\s(mi|mı|mu|mü|misin|mısın|mudur|müdür)" + _END, re.I)
_QUESTION_ANYWHERE = re.compile(
    r"(?:^|[\s\"'(])(nasıl|neden|niye|niçin|nerede|nereden|nereye|ne|neler|hangi|hangisi|kim|kimler|how|why|where|"
    r"which)" + _END, re.I)


# -- settings and state files -------------------------------------------------------------------------------

def live_dir(repo: Path) -> Path:
    return Path(repo) / ".verinoda" / LIVE_DIR


def _read_json(p: Path) -> dict:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes((json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    os.replace(tmp, p)


def settings(repo: Path) -> dict:
    """The project's settings: the defaults with what ``.verinoda/live.json`` holds (a value of the wrong type is
    ignored)."""
    saved = _read_json(live_dir(repo) / SETTINGS_NAME)
    out = dict(DEFAULTS)
    for k, default in DEFAULTS.items():
        v = saved.get(k)
        if k == "auto" and v in AUTO_MODES or k != "auto" and isinstance(v, bool):
            out[k] = v
        else:
            out[k] = default
    return out


def parse_value(key: str, text: str):
    t = text.strip().lower()
    if key == "auto":
        if t in ("on", "aç", "açık"):
            t = "nudge"
        if t not in AUTO_MODES:
            raise ValueError(f"auto is one of {', '.join(AUTO_MODES)}")
        return t
    if key not in DEFAULTS:
        raise ValueError(f"unknown setting {key!r} (one of {', '.join(DEFAULTS)})")
    if t in ("on", "true", "yes", "1"):
        return True
    if t in ("off", "false", "no", "0"):
        return False
    raise ValueError(f"{key} is on or off")


def set_setting(repo: Path, key: str, value: str) -> dict:
    if key not in DEFAULTS:
        raise ValueError(f"unknown setting {key!r} (one of {', '.join(DEFAULTS)})")
    saved = _read_json(live_dir(repo) / SETTINGS_NAME)
    saved[key] = parse_value(key, value)
    _write_json(live_dir(repo) / SETTINGS_NAME, saved)
    return settings(repo)


def _state(repo: Path, name: str) -> dict:
    return _read_json(live_dir(repo) / name)


def _put(repo: Path, name: str, data: dict) -> None:
    try:
        _write_json(live_dir(repo) / name, data)
    except OSError:
        pass


# -- finding the project and the program ---------------------------------------------------------------------

def find_project(start: str | Path) -> Path | None:
    """The nearest folder at or above ``start`` that has a ``.verinoda`` index; never the home folder or a drive root
    (a stray ``~/.verinoda`` must not turn every session into a Verinoda one)."""
    try:
        p = Path(start).resolve()
    except OSError:
        return None
    home = Path.home().resolve()
    for d in (p, *p.parents):
        if d == home or d == d.parent:
            return None
        if (d / ".verinoda").is_dir():
            return d
    return None


def module_argv() -> list[str]:
    """``<this interpreter> -P -m verinoda`` (``-I`` before 3.11): a project's own ``verinoda/`` folder can never
    stand in for the installed package."""
    return [sys.executable, "-P" if sys.version_info >= (3, 11) else "-I", "-m", "verinoda"]


def cli_word() -> str:
    """How the model is told to run Verinoda: ``verinoda`` when it is on PATH, else this interpreter's module form."""
    return "verinoda" if shutil.which("verinoda") else "python -m verinoda"


def _run(argv: list[str], repo: Path, timeout: float, stdin: str | None = None) -> tuple[int | None, str, str]:
    try:
        p = subprocess.run(argv, cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL)
        return p.returncode, p.stdout or "", p.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, "", f"{type(exc).__name__}: {exc}"


def spawn_detached(argv: list[str], repo: Path) -> bool:
    """Start ``argv`` so that it outlives this process and never holds the agent's step up."""
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                "cwd": str(repo)}
    if sys.platform == "win32":
        kw["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0x8)
                               | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
                               | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen(argv, **kw)  # noqa: S603 - argv is ours, never a shell string
    except OSError:
        return False
    return True


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# -- the index: fresh or not -----------------------------------------------------------------------------------

def index_state(repo: Path) -> dict:
    """``{"state": fresh|stale|building|none, "changed": N, "files": [...], "why": ...}``. ``building``: a build holds
    the index lock right now (the graph of an ``update --fast`` is built in the background)."""
    from verinoda import buildlock, freshness

    repo = Path(repo)
    building = buildlock.is_locked(repo)
    holder = buildlock.holder(repo) if building else None
    fresh = freshness.check(repo)
    if not fresh.get("checked"):
        return {"state": "none", "changed": 0, "files": [], "why": fresh.get("why", ""), "building": building}
    state = "building" if building else ("stale" if fresh["count"] else "fresh")
    return {"state": state, "changed": fresh["count"], "files": fresh["files"][:10], "building": building,
            "holder": holder, "snapshot": fresh.get("snapshot")}


def refresh(repo: Path, *, wait: bool = False) -> dict:
    """Take the changed files into the index now (``update --fast``: the search index, the lexicon and the stale
    claims in seconds; the graph in a background build). In the background unless ``wait``; nothing starts while
    another build runs, that one or the next update takes the changes in. ``{"started"|"state"|...}``."""
    repo = Path(repo)
    before = index_state(repo)
    if before["state"] == "none":
        return {"ran": False, "why": f"no index yet ({before['why']}): `verinoda scan` first", **before}
    if before["state"] == "building":
        return {"ran": False, "why": "an index build is running; it takes the changes in or the next refresh does",
                **before}
    if before["state"] == "fresh":
        return {"ran": False, "why": "the index is fresh", **before}
    argv = [*module_argv(), "update", "--fast", "--repo", str(repo)]
    if not wait:
        ok = spawn_detached(argv, repo)
        return {"ran": ok, "background": True, "changed": before["changed"], "state": before["state"],
                "why": "update --fast started in the background" if ok else "could not start `verinoda update --fast`"}
    code, out, err = _run(argv, repo, 900)
    return {"ran": code == 0, "background": False, "changed": before["changed"], "exit": code,
            "why": (out or err).strip().splitlines()[-1][:200] if (out or err).strip() else ""}


# -- the name check after an edit -------------------------------------------------------------------------------

def _rel(repo: Path, file: str) -> str | None:
    try:
        return Path(os.path.abspath(Path(repo) / file if not os.path.isabs(file) else file)).relative_to(
            Path(os.path.abspath(repo))).as_posix()
    except ValueError:
        return None


def _bad_sites(report: dict, rel: str | None) -> list[dict]:
    sites = [s for s in report.get("sites") or [] if isinstance(s, dict) and s.get("verdict") in ("absent", "mismatch")]
    if rel is None:
        return sites
    return [s for s in sites if str(s.get("path") or "").replace("\\", "/").lower() == rel.lower()]


def check_note(repo: Path, rel: str, report: dict) -> str | None:
    """What the model is told after an edit: the sites of ``rel`` that ``check`` found absent or mismatched (an
    ``unknown`` is not checked, so never reported), with the nearest real names; ``None`` when there are none."""
    bad = _bad_sites(report, rel)
    if not bad:
        return None
    cli = cli_word()
    lines = []
    for s in bad[:CHECK_NOTE_MAX]:
        near = [n.get("name") for n in s.get("nearest") or [] if isinstance(n, dict) and n.get("name")][:3]
        lines.append(f"- {s.get('at') or rel} {s.get('expr') or s.get('name') or ''}: "
                     f"{s.get('message') or s.get('verdict')}" + (f"; nearest: {', '.join(near)}" if near else ""))
    more = [f"({len(bad) - CHECK_NOTE_MAX} more: {cli} check --diff)"] if len(bad) > CHECK_NOTE_MAX else []
    return "\n".join([
        f"[Verinoda check] The lines you just changed in {rel} use "
        f"{'a name that does' if len(bad) == 1 else f'{len(bad)} names that do'} not exist in this project or its "
        "environment (a static check; runtime-made names are never reported):",
        *lines, *more,
        f"Fix them before relying on this code; the nearest real names are listed, and {cli} api <module or class> "
        "lists what one defines."])


def run_check(repo: Path, rel: str) -> dict | None:
    """``verinoda check --diff`` (the lines changed against HEAD; without git the file itself). Exit 3 (something
    absent) and 4 (something not checked) are answers too; anything else, or a timeout, is no answer."""
    for argv in ([*module_argv(), "check", "--diff", "--json", "--repo", str(repo)],
                 [*module_argv(), "check", "--json", "--repo", str(repo), "--", rel]):
        code, out, _err = _run(argv, repo, CHECK_TIMEOUT_S)
        if code is None:
            return None  # a timeout: the second command would wait as long
        if code in (0, 3, 4):
            try:
                data = json.loads(out)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    return None


def check_edit(repo: Path, file: str) -> dict:
    """Check one edited file. ``{"file", "checked", "bad", "note"}``; ``checked`` False for a file the checker
    does not read (Python, Java, Kotlin) or outside the project."""
    rel = _rel(repo, file)
    if rel is None or not CHECKABLE.search(rel):
        return {"file": rel or file, "checked": False, "bad": 0, "note": None,
                "why": "not a Python, Java or Kotlin file inside the project"}
    report = run_check(repo, rel)
    if report is None:
        res = {"file": rel, "checked": False, "bad": 0, "note": None, "why": "the check gave no answer"}
    else:
        note = check_note(repo, rel, report)
        summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
        # an unknown site was not checked (no resolver, an open receiver type): never reported as fine
        res = {"file": rel, "checked": True, "bad": len(_bad_sites(report, rel)), "note": note,
               "unknown": int(summary.get("unknown") or 0)}
    _put(repo, "last-check.json", {k: res.get(k) for k in ("file", "checked", "bad", "unknown")} | {"at": _now()})
    return res


def check_all(repo: Path) -> dict:
    """The whole change against HEAD (``check --diff``) rather than one file: ``{"results": [...], "bad": N}`` with one
    result per file that has a missing name; a clean answer is one result for the diff, with its unknown count."""
    report = run_check_diff(repo)
    if report is None:
        return {"results": [{"file": "(the change against HEAD)", "checked": False, "bad": 0, "note": None,
                             "why": "the check gave no answer"}], "bad": 0}
    bad = _bad_sites(report, None)
    paths = sorted({str(s.get("path") or "").replace("\\", "/") for s in bad})
    unknown = int((report.get("summary") or {}).get("unknown") or 0) if isinstance(report.get("summary"), dict) else 0
    results = [{"file": p, "checked": True, "bad": len(_bad_sites(report, p)), "note": check_note(repo, p, report),
                "unknown": unknown} for p in paths]
    return {"results": results or [{"file": "(the change against HEAD)", "checked": True, "bad": 0, "note": None,
                                    "unknown": unknown}], "bad": len(bad)}


def run_check_diff(repo: Path) -> dict | None:
    code, out, _err = _run([*module_argv(), "check", "--diff", "--json", "--repo", str(repo)], repo, CHECK_TIMEOUT_S)
    if code not in (0, 3, 4):
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# -- the auto-context -------------------------------------------------------------------------------------------

def looks_like_code_question(text: str, limit: int = SEARCH_MAX_CHARS) -> bool:
    """A prompt worth a code search: a question by its mark, its first word or a Turkish question particle. A slash
    command, a shell line or a note is not one; nor is anything shorter than 12 or longer than ``limit`` characters."""
    t = text.strip()
    if len(t) < 12 or len(t) > limit or t[0] in "/!#":
        return False
    return "?" in t or bool(_QUESTION_START.search(t) or _QUESTION_PARTICLE.search(t) or _QUESTION_ANYWHERE.search(t))


# "make it better" without saying what: the request `verinoda improve` is for (the mod's `improveOffer`)
_VAGUE = re.compile(
    r"\b(make|improve|polish|clean(?:\s*up)?)\b.{0,40}\b(better|nicer|cleaner|prettier|higher quality|more professional)\b"
    r"|\b(better|nicer|cleaner|prettier|higher quality)\b.{0,20}\b(please|pls)\b|\bdaha\s+(iyi|güzel|kaliteli|temiz|profesyonel)\b"
    r"|\b(iyileştir|güzelleştir|kalitesini artır)\w*", re.I)
OFFER = ("[Verinoda improve] The user asked for something to be better, nicer, cleaner or of higher quality without saying "
         "what should change. Do not guess and do not start editing. Offer in one line to list what could be changed so "
         "that they choose: `/verinoda-improve` in Claude Code, `$verinoda-improve` in Codex (they may add what to look "
         "at). If they ask for the list in words, run `verinoda improve start` first. A request that says what to change "
         "is not vague: do it.")


def is_vague_request(text: str) -> bool:
    t = text.strip()
    return 8 <= len(t) <= 400 and not t.startswith(("/", "$", "!", "#")) and bool(_VAGUE.search(t))


def nudge_block(repo: Path) -> str:
    cli = cli_word()
    return (f"[Verinoda auto-context] This project has a Verinoda code index ({repo}). For this question, start by "
            f"running Verinoda's analyze on it before any other search: {cli} analyze \"<the question>\" --repo {repo} "
            f"(and {cli} query \"<names it surfaces>\" --repo {repo}); then verify and complete with your own reading "
            "as needed. Use this CLI with --repo: a Verinoda MCP server, if one is connected, may index another "
            "folder. Its claims carry file:line evidence and a status; never state an inference or unknown as fact.")


def search_block(repo: Path, prompt: str) -> str | None:
    query = " ".join(prompt.split())[:400]
    code, out, _err = _run([*module_argv(), "query", query, "--repo", str(repo), "--max-chars", "6000"], repo,
                           QUERY_TIMEOUT_S)
    if code != 0 or not out.strip():
        return None
    return ("[Verinoda auto-context] Passages of this project's index that match the question (`verinoda query`; "
            "each carries its file:line; they are leads to verify by reading, not answers):\n" + out.strip())


def context_for(repo: Path, prompt: str, mode: str | None = None) -> str | None:
    """What the auto-context attaches to a prompt in ``mode`` (default: the project's setting), or ``None``. A nudge
    runs nothing before the prompt; a search runs ``verinoda query`` on it."""
    mode = mode or settings(repo)["auto"]
    if mode == "off":
        return None
    if not looks_like_code_question(prompt, NUDGE_MAX_CHARS if mode == "nudge" else SEARCH_MAX_CHARS):
        return None
    t0 = time.monotonic()
    block = nudge_block(repo) if mode == "nudge" else search_block(repo, prompt)
    _put(repo, "last-context.json", {"mode": mode, "prompt": " ".join(prompt.split())[:80], "ok": block is not None,
                                     "chars": len(block or ""), "seconds": round(time.monotonic() - t0, 2),
                                     "at": _now()})
    return block


# -- the review of commits ------------------------------------------------------------------------------------------

def head_of(repo: Path) -> str | None:
    code, out, _ = _run(["git", "-C", str(repo), "rev-parse", "HEAD"], repo, 10)
    return out.strip() if code == 0 and out.strip() else None


def review_commit(repo: Path, base: str = "HEAD~1") -> dict:
    """``verinoda review --base BASE`` on the commit at HEAD, kept in ``last-review.json`` for ``live status`` and for
    the next hook to hand to the model once. Exit 3 (findings or unknowns) is an answer."""
    sha = (head_of(repo) or "")[:7]
    t0 = time.monotonic()
    code, out, err = _run([*module_argv(), "review", "--json", "--repo", str(repo), "--base", base, "--max-chars",
                           "4000"], repo, REVIEW_TIMEOUT_S)
    info: dict = {"sha": sha, "at": _now(), "seconds": round(time.monotonic() - t0, 1), "announced": False}
    if code not in (0, 3):
        info |= {"ok": False, "summary": f"review exit {code}: {err.strip()[:160]}", "findings": 0}
    else:
        try:
            d = json.loads(out)
        except ValueError:
            d = {}
        risk = d.get("risk") if isinstance(d.get("risk"), dict) else {}
        score = risk.get("score")
        info |= {"ok": True, "score": score, "of": risk.get("of", 100), "band": risk.get("band", ""),
                 "findings": (d.get("counts") or {}).get("findings", 0),
                 "summary": str(d.get("summary") or "")[:600]}
    _put(repo, "last-review.json", info)
    return info


def review_line(info: dict) -> str:
    if not info.get("ok"):
        return f"[Verinoda review] the review of commit {info.get('sha')} did not finish: {info.get('summary')}"
    risk = f"{info['score']}/{info.get('of', 100)} ({info.get('band') or '?'})" if info.get("score") is not None else "n/a"
    return (f"[Verinoda review] Commit {info.get('sha')}: risk {risk}, {info.get('findings', 0)} finding(s). "
            f"{info.get('summary', '')} (`{cli_word()} review --base HEAD~1` for the rest)")


def guard_tick(repo: Path, *, spawn: bool = True) -> bool:
    """Called after a shell command: when the project's HEAD moved since the last call, review that commit in the
    background (``live review``). The first call only records HEAD. Returns whether a review was started."""
    if not settings(repo)["guard"]:
        return False      # no git call per prompt or command for a feature that is off
    head = head_of(repo)
    if head is None:
        return False
    st = _state(repo, "head.json")
    _put(repo, "head.json", {"head": head})
    if not st.get("head") or st["head"] == head:
        return False
    if not spawn:
        return True
    return spawn_detached([*module_argv(), "live", "review", "--repo", str(repo)], repo)


def pending_review_note(repo: Path) -> str | None:
    """The finished review the model has not been told about yet, once."""
    info = _state(repo, "last-review.json")
    if not info or info.get("announced") or "sha" not in info:
        return None
    _put(repo, "last-review.json", {**info, "announced": True})
    return review_line(info)


# -- status ---------------------------------------------------------------------------------------------------------------

def status(repo: Path) -> dict:
    return {"repo": str(repo), "index": index_state(repo), "settings": settings(repo),
            "last_check": _state(repo, "last-check.json") or None,
            "last_context": _state(repo, "last-context.json") or None,
            "last_review": _state(repo, "last-review.json") or None}


def render_status(st: dict) -> str:
    ix, cfg = st["index"], st["settings"]
    words = {"fresh": "fresh: the index describes the working tree",
             "stale": f"stale: {ix['changed']} file(s) changed since the index"
                      + (f" ({', '.join(ix['files'][:5])}{', ...' if ix['changed'] > 5 else ''})" if ix["files"] else "")
                      + " - `verinoda live refresh`",
             "building": "text index up to date; the graph is being built in the background"
                         + (f" ({ix['changed']} file(s) behind)" if ix["changed"] else ""),
             "none": f"no index yet ({ix.get('why', '')}) - `verinoda scan`"}[ix["state"]]
    lines = [f"index    {words}"]
    on = lambda v: "on" if v else "off"  # noqa: E731
    lines.append(f"settings refresh {on(cfg['refresh'])} · check {on(cfg['check'])} · guard {on(cfg['guard'])}"
                 f" · auto {cfg['auto']}")
    ck, cx, rv = st["last_check"], st["last_context"], st["last_review"]
    if ck:
        lines.append(f"check    last {ck.get('file')}: " + (f"{ck['bad']} name(s) that do not exist"
                                                          + (f", {ck['unknown']} site(s) not checked" if ck.get("unknown") else "")
                                                          if ck.get("checked") else "not checked") + f" ({ck.get('at')})")
    if cx:
        lines.append(f"context  last {cx.get('mode')}: " + ("attached" if cx.get("ok") else "nothing attached")
                     + f" ({cx.get('at')})")
    if rv:
        lines.append("review   " + review_line(rv).replace("[Verinoda review] ", ""))
    return "\n".join(lines)


# -- the hook command ------------------------------------------------------------------------------------------------------

def _edited_files(tool_input) -> list[str]:
    """The files an edit tool touched: ``file_path`` / ``notebook_path`` (Claude Code), the ``*** Update File:`` lines of
    a patch (Codex's ``apply_patch``), whichever string field holds them."""
    if not isinstance(tool_input, dict):
        return []
    out: list[str] = []
    for key in ("file_path", "notebook_path", "path"):
        if isinstance(tool_input.get(key), str):
            out.append(tool_input[key])
    for v in tool_input.values():
        if isinstance(v, str) and "*** Begin Patch" in v:
            out += _PATCH_FILE.findall(v)
    return list(dict.fromkeys(out))


def _event(payload: dict) -> str:
    return str(payload.get("hook_event_name") or payload.get("hookEventName") or "")


def hook(payload: dict) -> dict | None:
    """One hook call of Claude Code or Codex (the JSON each sends on stdin) -> the JSON answer or ``None``.

    ``UserPromptSubmit``: the review of a commit the model has not been told about, then the auto-context.
    ``PostToolUse``: after an edit tool the name check, after a shell command the review of a moved HEAD.
    ``Stop`` and ``SessionStart``: the index refresh. Anything else, or a project with no index: nothing."""
    event = _event(payload)
    repo = find_project(payload.get("cwd") or os.getcwd())
    if repo is None:
        return None
    cfg = settings(repo)
    notes: list[str] = []
    if event == "UserPromptSubmit":
        review = pending_review_note(repo)
        if review:
            notes.append(review)
        guard_tick(repo)
        prompt = payload.get("prompt")
        if isinstance(prompt, str) and not prompt.startswith(("<", "[Verinoda")):
            block = context_for(repo, prompt)
            if block:
                notes.append(block)
            if cfg["offer"] and is_vague_request(prompt):
                notes.append(OFFER)
    elif event == "PostToolUse":
        tool = str(payload.get("tool_name") or "")
        if EDIT_TOOLS.match(tool) and cfg["check"]:
            for f in _edited_files(payload.get("tool_input"))[:3]:
                note = check_edit(repo, f)["note"]
                if note:
                    notes.append(note)
        elif SHELL_TOOLS.match(tool):
            guard_tick(repo)
        review = pending_review_note(repo)
        if review:
            notes.append(review)
    elif event in ("Stop", "SessionStart"):
        if event == "SessionStart":
            guard_tick(repo, spawn=False)
        if cfg["refresh"]:
            refresh(repo)
        return None
    else:
        return None
    if not notes:
        return None
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": "\n\n".join(notes)}}
