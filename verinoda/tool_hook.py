"""Graph context after the agent's own searches and reads: one hook call per tool call.

A PostToolUse hook of Claude Code, Codex or Cursor hands the tool call over (JSON on stdin); :func:`answer`
returns what the project knows about it, in the agent's own hook output shape:

- a search - the Grep tool, or ``grep`` / ``rg`` / ``git grep`` / ``ag`` / ``ack`` / ``findstr`` /
  ``Select-String`` in a shell command - gets the definition, callers and callees of the symbols its pattern
  names (:func:`grep_text`, the text MCP ``grep_context`` returns);
- a read or an edit of a file gets what the project says about that file (:mod:`verinoda.scoped`, the text MCP
  ``read_context`` returns).

Anything else, any error, a project without an index: ``{}``. A hook never holds up or fails the tool call.

Codex and Cursor hooks are commands, one process per call, so the graph is not kept in memory between them:
each word's line is kept in ``.verinoda/index/hook_memo.sqlite``, valid while the graph file is the same, and the
graph is loaded only for a word not seen since the last index build.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import sqlite3
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

# identifier-like words of a search pattern, those that are only regex or language keywords left out
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
SKIP = {"def", "class", "function", "return", "import", "from", "self", "this", "public", "private", "static",
        "void", "const", "async", "await", "final", "override", "interface", "record", "true", "false", "none",
        "null", "string", "self."}
CONTEXT_CHARS = 450   # every search that names a known symbol pays it: about 110 tokens at most
CALLERS, CALLS = 3, 4
# regex escapes (\bname\b, \w+) and shell variables ($NAME, ${NAME}) are no symbol names
_NOT_NAMES = re.compile(r"\\[bBwWsSdDAzZ<>]|\$\{?[A-Za-z_]\w*\}?")
WORDS_TRIED = 4       # the longest words of a pattern looked up, at most
WORDS_SAID = 2        # symbols described, at most
MEMO = "hook_memo.sqlite"
AGENTS = ("claude", "codex", "cursor")

SEARCH_TOOLS = {"grep"}                                  # tool names, lower case
SHELL_TOOLS = {"bash", "shell", "run_shell_command", "powershell"}
FILE_TOOLS = {"read", "edit", "multiedit", "write"}
# the options of each search program that take a value (the value is not the pattern), and those that give the
# pattern itself; a pattern read from a file (-f) is not seen, so such a command gives none
_COMMON = {"-A", "-B", "-C", "-m", "--context", "--after-context", "--before-context", "--max-count", "--color",
           "--colors"}
VALUE_OPTS = {
    "grep": _COMMON | {"-d", "-D", "--include", "--exclude", "--exclude-dir", "--exclude-from", "--label",
                       "--binary-files", "--devices", "--directories"},
    "rg": _COMMON | {"-g", "-t", "-T", "-r", "-E", "-M", "-j", "--glob", "--iglob", "--type", "--type-not",
                     "--type-add", "--type-clear", "--replace", "--encoding", "--max-columns", "--threads", "--sort",
                     "--sortr", "--pre", "--pre-glob", "--max-depth", "--max-filesize", "--ignore-file",
                     "--path-separator", "--context-separator", "--field-match-separator", "--engine"},
    "ag": _COMMON | {"-G", "-g", "--file-search-regex", "--ignore", "--ignore-dir", "--depth", "--pager"},
    "ack": _COMMON | {"--type", "--ignore-dir", "--ignore-file", "--match", "--output", "--pager"},
}
PATTERN_OPTS = {"-e", "--regexp"}
FILE_PATTERN_OPTS = {"-f", "--file"}
PS_VALUE_OPTS = {"-path", "-literalpath", "-encoding", "-context", "-include", "-exclude"}   # Select-String
FAMILY = {"grep": "grep", "egrep": "grep", "fgrep": "grep", "rg": "rg", "ag": "ag", "ack": "ack",
          "findstr": "findstr", "select-string": "ps", "sls": "ps"}
WRAPPERS = {"sudo", "command", "env", "xargs", "nice", "nohup", "time", "exec"}
WRAPPER_VALUE_OPTS = {"-u", "-g", "-C", "-p", "-U", "-h", "-r", "-t", "-n", "-I", "-L", "-P", "-S"}
SEPARATORS = "|&;\n()"


# -- what the tool call searched for, or read ------------------------------------------------------

def _segments(command: str) -> list[list[str]]:
    """The simple commands of a shell line (split at pipes, ``&&``, ``||``, ``;``, newlines and parentheses
    outside quotes), each as its words."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=SEPARATORS)
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        lex.commenters = ""
        toks = list(lex)
    except ValueError:   # an unclosed quote: words as written
        toks = command.split()
    out: list[list[str]] = [[]]
    for tok in toks:
        if tok and set(tok) <= set(SEPARATORS):
            out.append([])
        else:
            out[-1].append(tok)
    return [s for s in out if s]


def _program(word: str) -> str:
    name = PureWindowsPath(word).name if "\\" in word else PurePosixPath(word).name
    name = name.lower()
    return name[:-4] if name.endswith(".exe") else name


def _unwrap(words: list[str]) -> list[str]:
    """The search command inside ``VAR=x``, ``sudo``/``env``/``timeout N``/``nice`` wrappers and ``git [-C dir]
    grep``."""
    while words:
        w = words[0]
        if "=" in w and not w.startswith("-"):   # VAR=value prefixes
            words = words[1:]
        elif _program(w) in WRAPPERS:
            words = words[1:]
            while words and words[0].startswith("-"):
                words = words[2:] if words[0] in WRAPPER_VALUE_OPTS else words[1:]
        elif _program(w) == "timeout":
            words = words[1:]
            while words and words[0].startswith("-"):
                words = words[1:]
            words = words[1:]   # the duration
        else:
            break
    if words and _program(words[0]) == "git":
        i = 1
        while i < len(words) and words[i].startswith("-"):
            i += 2 if words[i] in ("-C", "-c", "--git-dir", "--work-tree") else 1
        if i < len(words) and words[i] == "grep":
            return ["grep", *words[i + 1:]]
    return words


def _one(family: str, args: list[str]) -> list[str]:
    """The patterns of one search command's arguments (none when they come from a file)."""
    given: list[str] = []
    positional: list[str] = []
    values = VALUE_OPTS.get(family, set())
    from_file = False
    i = 0
    while i < len(args):
        w = args[i]
        low = w.lower()
        nxt = args[i + 1] if i + 1 < len(args) else None
        if w == "--":
            positional += args[i + 1:]
            break
        if family == "findstr":
            if low.startswith("/c:"):
                given.append(w[3:])
            elif low.startswith("/g:"):
                from_file = True
            elif not w.startswith("/"):
                positional.append(w)
        elif family == "ps":
            if low == "-pattern" and nxt is not None:
                given.append(nxt)
                i += 1
            elif low in PS_VALUE_OPTS:
                i += 1
            elif not w.startswith("-"):
                positional.append(w)
        elif w in PATTERN_OPTS and nxt is not None:
            given.append(nxt)
            i += 1
        elif w.startswith("--regexp="):
            given.append(w.split("=", 1)[1])
        elif w in FILE_PATTERN_OPTS or w.startswith("--file="):
            from_file = True
            i += 1 if w in FILE_PATTERN_OPTS else 0
        elif w in values:
            i += 1
        elif w.startswith("--") and "=" in w:
            pass   # --glob=*.py, --max-count=5
        elif w.startswith("-") and not w.startswith("--") and len(w) > 2:
            # bundled short options: -rn, -ie PATTERN, -A3, -e<pattern>
            for k, ch in enumerate(w[1:], 1):
                opt = "-" + ch
                if opt in PATTERN_OPTS:
                    rest = w[k + 1:]
                    if rest:
                        given.append(rest)
                    elif nxt is not None:
                        given.append(nxt)
                        i += 1
                    break
                if opt in FILE_PATTERN_OPTS:
                    from_file = True
                    if not w[k + 1:]:
                        i += 1
                    break
                if opt in values:
                    if not w[k + 1:]:
                        i += 1   # the value is the next word; otherwise it was attached (-A3)
                    break
        elif not w.startswith("-"):
            positional.append(w)
        i += 1
    if given:
        return given
    return [] if from_file else positional[:1]


def shell_patterns(command: str) -> list[str]:
    """The patterns a shell command searches for with a known search program (``git grep`` included)."""
    pats: list[str] = []
    for seg in _segments(command or ""):
        words = _unwrap(list(seg))
        if not words or _program(words[0]) not in FAMILY:
            continue
        pats += _one(FAMILY[_program(words[0])], words[1:])
    return [p for p in pats if p]


def event_kind(event: dict) -> tuple[str, list[str]] | None:
    """``("search", patterns)`` or ``("file", [path])`` for a tool call, None for one the hook does not answer."""
    tool = str(event.get("tool_name") or "").strip()
    inp = event.get("tool_input")
    if isinstance(inp, str):   # a tool input sent as JSON text
        try:
            inp = json.loads(inp)
        except ValueError:
            inp = {}
    if not isinstance(inp, dict):
        return None
    low = tool.lower()
    if low in SEARCH_TOOLS:
        pat = next((inp.get(k) for k in ("pattern", "query", "regex") if isinstance(inp.get(k), str)), "")
        return ("search", [pat]) if pat else None
    if low in SHELL_TOOLS:
        cmd = inp.get("command")
        pats = shell_patterns(cmd) if isinstance(cmd, str) else []
        return ("search", pats) if pats else None
    if low in FILE_TOOLS:
        p = next((inp.get(k) for k in ("file_path", "path", "target_file") if isinstance(inp.get(k), str)), "")
        return ("file", [p]) if p else None
    return None


# -- the text -------------------------------------------------------------------------------------

def words_of(patterns: list[str]) -> list[str]:
    """The words of the patterns that may name a symbol, longest first (at most :data:`WORDS_TRIED`)."""
    ws = {w for p in patterns for w in WORD.findall(_NOT_NAMES.sub(" ", p or ""))
          if len(w) >= 4 and w.lower() not in SKIP}
    return sorted(ws, key=lambda w: (-len(w), w))[:WORDS_TRIED]


def word_line(g, word: str) -> str:
    """One line for a word the graph resolves by its exact name, else ``""``."""
    from verinoda import naming, retrieval

    if not naming.exact_nodes(g, word, fallback=False)[0]:   # no symbol of that name: no fuzzy search (seconds)
        return ""
    r = naming.resolve(g, word)
    if r.node is None or not r.exact:
        return ""
    calls, callers, nc, nb = retrieval.call_outline(g, r.node)
    line = f"`{word}` is defined at {g.file(r.node)}:{g.line(r.node)}"
    if callers:
        line += "; called by " + "; ".join(callers[:CALLERS]) + (f" (+{nb - CALLERS})" if nb > CALLERS else "")
    if calls:
        line += "; calls " + ", ".join(calls[:CALLS]) + (f" (+{nc - CALLS})" if nc > CALLS else "")
    return line


def join(lines: list[str]) -> str:
    said = [x for x in lines if x][:WORDS_SAID]
    if not said:
        return ""
    text = "Verinoda (static call graph, extracted, not verified): " + " | ".join(said)
    if len(text) > CONTEXT_CHARS:   # cut between entries, never inside a path
        cut = text.rfind("; ", 0, CONTEXT_CHARS - 4)
        text = text[:cut if cut > 0 else CONTEXT_CHARS - 4] + "; ..."
    return text


def grep_text(g, patterns: list[str]) -> str:
    """The hook's text for a search, from a loaded graph (MCP ``grep_context``)."""
    out = []
    for w in words_of(patterns):
        line = word_line(g, w)
        if line:
            out.append(line)
            if len(out) == WORDS_SAID:
                break
    return join(out)


# -- the memo (command hooks) ---------------------------------------------------------------------

def _stamp(repo: Path) -> str:
    """The graph file and the receiver-call edges :func:`verinoda.index.load` adds (an update can rewrite those
    and keep graph.json): any change to either clears the memo."""
    from verinoda.paths import graph_path, receiver_calls_path

    out = []
    for p in (graph_path(repo), receiver_calls_path(repo)):
        try:
            st = p.stat()
            out.append(f"{st.st_size}:{st.st_mtime_ns}")
        except OSError:
            out.append("-")
    return "|".join(out)


def _memo(repo: Path, stamp: str) -> sqlite3.Connection | None:
    from verinoda.paths import index_dir

    try:
        db = sqlite3.connect(str(index_dir(repo) / MEMO), timeout=2)
        db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS memo (word TEXT PRIMARY KEY, line TEXT)")
        row = db.execute("SELECT v FROM meta WHERE k = 'stamp'").fetchone()
        if not row or row[0] != stamp:   # a new index: every line may have changed
            with db:
                db.execute("DELETE FROM memo")
                db.execute("INSERT OR REPLACE INTO meta VALUES ('stamp', ?)", (stamp,))
        return db
    except sqlite3.Error:
        return None


def search_text(repo: Path, patterns: list[str]) -> str:
    """The text for a search from a separate process: remembered lines first, the graph only for new words."""
    from verinoda.paths import graph_path

    words = words_of(patterns)
    gp = graph_path(repo)
    if not words or not gp.exists():
        return ""
    stamp = _stamp(repo)
    db = _memo(repo, stamp)
    known: dict[str, str] = {}
    if db is not None:
        try:
            q = "SELECT word, line FROM memo WHERE word IN (%s)" % ",".join("?" * len(words))
            known = dict(db.execute(q, words).fetchall())
        except sqlite3.Error:
            known = {}
    lines = []
    g = None
    for w in words:
        if w not in known:
            if g is None:
                from verinoda import index   # networkx and the graph: only for a word not remembered

                g = index.load(repo)
                if _stamp(repo) != stamp:   # rebuilt while loading: the lines are not remembered under the old stamp
                    db = None
            known[w] = word_line(g, w)
            if db is not None:
                try:
                    with db:
                        db.execute("INSERT OR REPLACE INTO memo VALUES (?, ?)", (w, known[w]))
                except sqlite3.Error:
                    db = None
        if known[w]:
            lines.append(known[w])
            if len(lines) == WORDS_SAID:
                break
    if db is not None:
        db.close()
    return join(lines)


def file_text(repo: Path, path: str) -> str:
    from verinoda import scoped

    return scoped.text(scoped.for_file(repo, path)) if path else ""


# -- the hook -------------------------------------------------------------------------------------

def shape(agent: str, text: str) -> dict:
    """The hook output each agent reads: ``additionalContext`` (Claude Code, Codex), ``additional_context``
    (Cursor); ``{}`` when there is nothing to say."""
    if not text:
        return {}
    if agent == "cursor":
        return {"additional_context": text}
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}


def project_of(event: dict) -> Path | None:
    """The indexed project the tool call ran in: the event's ``cwd`` (Cursor: its first workspace root), else the
    working directory when it names none, up to the nearest folder whose ``.verinoda`` holds a project (its database or config),
    below the home folder."""
    roots = [r for r in [event.get("cwd"), *(event.get("workspace_roots") or [])] if isinstance(r, str) and r]
    roots = roots or [os.getcwd()]   # the working directory only when the event names no folder
    for r in roots:
        if not isinstance(r, str) or not r:
            continue
        p = Path(r)
        home = Path.home()
        for d in (p, *p.parents):
            if d == home:
                break
            v = d / ".verinoda"
            if (v / "atlas.db").is_file() or (v / "config.json").is_file():
                return d
    return None


def answer(event: dict, agent: str) -> dict:
    """The hook's output for one tool call (see the module docstring)."""
    try:
        kind = event_kind(event)
        if kind is None:
            return {}
        repo = project_of(event)
        if repo is None:
            return {}
        with contextlib.redirect_stdout(sys.stderr):   # stdout carries the hook's JSON only
            if kind[0] == "search":
                text = search_text(repo, kind[1])
            else:
                text = file_text(repo, kind[1][0])
        return shape(agent, text)
    except Exception:  # noqa: BLE001 - a hook never breaks the agent's tool call
        return {}


def run(agent: str, stdin=None) -> str:
    """Read the event from ``stdin`` (bytes; JSON is UTF-8 whatever the console's code page) and return the output
    line, ASCII-escaped so that no console encoding can garble it (``{}`` for anything unreadable)."""
    try:
        src = stdin if stdin is not None else sys.stdin.buffer
        raw = src.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8-sig")
        event = json.loads(raw or "{}")
    except (ValueError, OSError, UnicodeDecodeError, AttributeError):
        event = {}
    out = answer(event, agent) if isinstance(event, dict) else {}
    return json.dumps(out)
