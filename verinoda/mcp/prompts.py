"""Ready workflows served as MCP prompts (``prompts/list``, ``prompts/get``) and by ``verinoda mcp prompts``.

A prompt is a fixed sequence of Verinoda tool calls for one kind of task - review a change, get to know a
project, debug a failure, check a branch before merging - with the reporting rules that go with it. It
states nothing about the code itself: the tools it names return the claims and evidence. The text is built
for the profile being served: a tool the menu lists is named directly, a tool behind ``run_tool`` is named
as a ``run_tool`` call, and a step whose tool the profile does not serve names the CLI command instead
(or is left out when there is none).
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass

PROMPT_NAMES: tuple[str, ...] = ("review", "onboarding", "debug", "pre_merge")

DESCRIPTIONS: dict[str, str] = {
    "review": "Review the working-tree change (or the change since base): what it touches, names that do not "
              "exist, decision records, dependents.",
    "onboarding": "Get to know the project (or one topic in it): structure, dependencies, tests, then how it "
                  "works as claims with evidence.",
    "debug": "Debug a failure: locate the code the symptom names, ask why, then record every fix attempt "
             "against one repro.",
    "pre_merge": "Check a branch before merging into base: commits and files it adds, what they touch, names, "
                 "dependencies, decision records, cycles.",
}

# (name, description, required) of each prompt's arguments; every value is a string (MCP prompt arguments)
ARGUMENTS: dict[str, tuple[tuple[str, str, bool], ...]] = {
    "review": (("base", "Revision to compare with (default: HEAD, the uncommitted change).", False),),
    "onboarding": (("topic", "A feature or area to start with (default: the whole project).", False),),
    "debug": (("symptom", "What is wrong, in a sentence (an error message, a wrong result).", True),
              ("repro", "Command that shows the failure, e.g. 'python -m pytest -q tests/test_x.py'.", False)),
    "pre_merge": (("base", "The branch to merge into (default: main).", False),),
}


@dataclass(frozen=True)
class Step:
    tool: str
    arguments: dict
    why: str
    cli: str | None = None   # the command to use when the profile does not serve the tool


def _steps(name: str, args: dict[str, str]) -> tuple[str, list[Step], str]:
    """(opening, steps, closing) of one prompt with its arguments filled in."""
    if name == "review":
        base = args.get("base") or "HEAD"
        diff = {} if base == "HEAD" else {"base": base}
        what = "the uncommitted change" if base == "HEAD" else f"the change since {base}"
        return (f"Review {what} in this repository with Verinoda.", [
            Step("index_update", {}, "bring the index up to the working tree"),
            Step("change_review", diff, "what the change touches, by concern: dependents, findings, tests that "
                                        "reach it, unknowns; read its read_first"),
            Step("code_check", {"diff": base}, "do the modules, names and arguments the changed lines use exist"),
            Step("decision_check", {"changed_only": True} if base == "HEAD" else {"base": base},
                 "the change against accepted decision records"),
        ], "Report every concern with its file:line, and each unknown with its next step. 'No finding' is not "
           "'safe': never call the change safe.")
    if name == "onboarding":
        topic = (args.get("topic") or "").strip()
        question = (f"how does {topic} work and where does it start?" if topic
                    else "what does this project do and where are its entry points?")
        return ("Get to know this repository with Verinoda before changing it.", [
            Step("index_update", {}, "scan or refresh the index"),
            Step("map_view", {"view": "hierarchy"}, "the modules and what they contain"),
            Step("map_view", {"view": "dependencies"}, "how the modules depend on each other"),
            Step("map_view", {"view": "tests"}, "which tests cover which code"),
            Step("analyze", {"question": question}, "the answer as claims with file:line evidence"),
        ], "Write a short orientation: the main modules, how they depend on each other, where to start "
           "reading. Cite file:line for every fact; the map views are extractions and heuristics, so state what "
           "they suggest as inference, and never state a weak_inference or unknown claim as fact.")
    if name == "debug":
        symptom = (args.get("symptom") or "").strip()
        repro = (args.get("repro") or "").strip()
        command = split_repro(repro) if repro else ["<the command that shows the failure>"]
        cli_start = (f"verinoda debug start {_quote(symptom, always=True)} -- "
                     + (" ".join(_quote(a) for a in command) if repro else "<repro command>"))
        return (f"Debug this failure with Verinoda: {symptom}", [
            Step("index_update", {}, "bring the index up to the working tree"),
            Step("project_query", {"question": symptom}, "the code locations the symptom names (leads, not "
                                                         "answers)"),
            Step("analyze", {"question": f"why {symptom}?"}, "the likely cause as claims with evidence"),
            Step("debug_start", {"symptom": symptom, "command": command},
                 "open the debug ledger before the first edit; the repro runs once as attempt 0", cli=cli_start),
            Step("debug_attempt", {"hypothesis": "<what this edit should fix>"},
                 "after every edit; when it returns stop=true, stop editing and follow strategies[0]",
                 cli="verinoda debug try --hypothesis \"<what this edit should fix>\""),
            Step("code_check", {}, "on the code you edited"),
        ], "Never say 'fixed': say which tree the repro passed on. Report the hypotheses that failed as well.")
    if name == "pre_merge":
        base = args.get("base") or "main"
        return (f"Check this branch before merging it into {base}, with Verinoda.", [
            Step("index_update", {}, "bring the index up to the working tree"),
            Step("history_search", {"base": base}, "the commits and files this branch adds"),
            Step("change_review", {"base": base}, "what those changes touch, by concern"),
            Step("code_check", {"diff": base}, "do the names the changed lines use exist"),
            Step("code_check", {"deps": True}, "the manifests' dependencies against the imports"),
            Step("decision_check", {"base": base}, "new findings against accepted decision records"),
            Step("map_view", {"view": "cycles"}, "file dependency cycles"),
        ], "Report blocking findings first (absent names, VIOLATED decisions), then concerns and unknowns, each "
           "with file:line, and the tests to run. Say what was not checked; never say the branch is safe to "
           "merge.")
    raise KeyError(name)


def _call(step: Step, listed: set[str], served: set[str], gateway: str) -> str | None:
    args = json.dumps(step.arguments, ensure_ascii=False)
    if step.tool in listed:
        return f"{step.tool} {args}"
    if step.tool in served:
        return f"{gateway} {json.dumps({'name': step.tool, 'arguments': step.arguments}, ensure_ascii=False)}"
    if step.cli:
        return f"`{step.cli}` in a terminal ({step.tool} is served by `verinoda mcp serve --profile full`)"
    return None


def split_repro(repro: str) -> list[str]:
    r"""The argument list of a repro command: split at whitespace, ' and " group, a backslash is kept as it is.

    Shell escapes are not applied, so a Windows path (``C:\Users\me\x.py``) keeps its separators.
    Raises ValueError on an unbalanced quote.
    """
    lex = shlex.shlex(repro, posix=True)
    lex.whitespace_split, lex.escape, lex.commenters = True, "", ""
    return list(lex)


_PLAIN = re.compile(r"[\w@%+=:,./\\-]+")


def _quote(arg: str, *, always: bool = False) -> str:
    """``arg`` in double quotes when it is empty or has a space or a shell character (for a command to copy)."""
    if not always and _PLAIN.fullmatch(arg):
        return arg
    return '"' + arg.replace('"', '\\"') + '"'


def missing_required(name: str, args: dict[str, str]) -> list[str]:
    return [a for a, _, req in ARGUMENTS[name] if req and not (args.get(a) or "").strip()]


def argument_problems(name: str, args: dict[str, str]) -> list[str]:
    """Why prompt ``name`` cannot be filled in with ``args`` (empty when it can); the server and the CLI both check."""
    problems = [f"{name} needs {a} (a non-blank value)" for a in missing_required(name, args)]
    if name == "debug" and (args.get("repro") or "").strip():
        try:
            split_repro(args["repro"].strip())
        except ValueError as exc:
            problems.append(f"repro: {exc}; balance its quotes")
    return problems


def render(name: str, args: dict[str, str] | None = None, *, listed, served, gateway: str = "run_tool") -> str:
    """The text of prompt ``name`` for a server listing ``listed`` and serving ``served`` tools."""
    args = {k: str(v) for k, v in (args or {}).items() if v is not None}
    opening, steps, closing = _steps(name, args)
    lines = [opening, "", "Steps (Verinoda tools, in order):"]
    n = 0
    for step in steps:
        call = _call(step, set(listed), set(served), gateway)
        if call is None:
            continue
        n += 1
        lines.append(f"{n}. {call} - {step.why}.")
    lines += ["", closing,
              "Every statement about the code comes from a tool result: a claim with its status and file:line "
              "evidence. Missing evidence is 'unknown' plus a next step, never a guess."]
    return "\n".join(lines)


def catalog(*, listed, served, gateway: str = "run_tool") -> list[dict]:
    """Every prompt: name, description, arguments and the steps it would run with its defaults."""
    out = []
    for name in PROMPT_NAMES:
        _, steps, _ = _steps(name, {a: f"<{a}>" for a, _, req in ARGUMENTS[name] if req})
        out.append({"name": name, "description": DESCRIPTIONS[name],
                    "arguments": [{"name": a, "description": d, "required": r} for a, d, r in ARGUMENTS[name]],
                    "tools": list(dict.fromkeys(s.tool for s in steps
                                                if _call(s, set(listed), set(served), gateway) is not None))})
    return out
