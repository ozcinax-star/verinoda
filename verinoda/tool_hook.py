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
WORDS_TRIED = 4       # the longest words of a pattern looked up, at most
WORDS_SAID = 2        # symbols described, at most
MEMO = "hook_memo.sqlite"
AGENTS = ("claude", "codex", "cursor")

SEARCH_TOOLS = {"grep"}                                  # tool names, lower case
SHELL_TOOLS = {"bash", "shell", "run_shell_command", "powershell"}
FILE_TOOLS = {"read", "edit", "multiedit", "write"}
SEARCH_PROGRAMS = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "findstr", "select-string", "sls"}
# options of the search programs that take a value (the value is not the pattern)
VALUE_OPTS = {"-A", "-B", "-C", "-m", "-g", "-t", "-f", "-d", "-D", "-j", "-M", "--glob", "--type",
              "--type-not", "--max-count", "--context", "--after-context", "--before-context", "--file",
              "--include", "--exclude", "--exclude-dir", "--max-depth", "--threads", "--color", "--colors",
              "--sort", "--sortr", "--encoding", "--replace", "--max-columns", "--iglob", "--pre"}
PS_VALUE_OPTS = {"-path", "-literalpath", "-encoding", "-context", "-include", "-exclude"}   # Select-String
PATTERN_OPTS = {"-e", "--regexp"}


# -- what the tool call searched for, or read ------------------------------------------------------

def _segments(command: str) -> list[list[str]]:
    """The simple commands of a shell line (split at pipes, ``&&``, ``||`` and ``;``), each as its words."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars="|&;")
        lex.whitespace_split = True
        lex.commenters = ""
        toks = list(lex)
    except ValueError:   # an unclosed quote: words as written
        toks = command.split()
    out: list[list[str]] = [[]]
    for tok in toks:
        if tok and set(tok) <= set("|&;"):
            out.append([])
        else:
            out[-1].append(tok)
    return [s for s in out if s]


def _program(word: str) -> str:
    name = PureWindowsPath(word).name if "\\" in word else PurePosixPath(word).name
    name = name.lower()
    return name[:-4] if name.endswith(".exe") else name


def shell_patterns(command: str) -> list[str]:
    """The patterns a shell command searches for with a known search program (``git grep`` included)."""
    pats: list[str] = []
    for seg in _segments(command or ""):
        words = list(seg)
        while words and "=" in words[0] and not words[0].startswith("-"):   # VAR=value prefixes
            words.pop(0)
        if words and _program(words[0]) in ("sudo", "command", "env", "xargs"):
            words.pop(0)
        if len(words) >= 2 and _program(words[0]) == "git" and words[1] == "grep":
            words = words[1:]
        if not words or _program(words[0]) not in SEARCH_PROGRAMS:
            continue
        given: list[str] = []
        positional: list[str] = []
        i = 1
        while i < len(words):
            w = words[i]
            if w == "--":
                positional += words[i + 1:]
                break
            if w in PATTERN_OPTS or w.lower() == "-pattern":
                if i + 1 < len(words):
                    given.append(words[i + 1])
                i += 2
                continue
            if w.startswith("--regexp="):
                given.append(w.split("=", 1)[1])
            elif w.startswith("-e") and len(w) > 2 and not w.startswith("--"):
                given.append(w[2:])
            elif w in VALUE_OPTS or w.lower() in PS_VALUE_OPTS:
                i += 2
                continue
            elif w.startswith("/") and _program(words[0]) == "findstr" and len(w) > 1:
                if w[1:3].lower() == "c:":
                    given.append(w[3:])
            elif not w.startswith("-"):
                positional.append(w)
            i += 1
        pats += given or positional[:1]
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
    ws = {w for p in patterns for w in WORD.findall(p or "") if len(w) >= 4 and w.lower() not in SKIP}
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

def _stamp(gp: Path) -> str:
    st = gp.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


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
    stamp = _stamp(gp)
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
                if _stamp(gp) != stamp:   # rebuilt while loading: the lines are not remembered under the old stamp
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
    working directory, up to the nearest folder with ``.verinoda``."""
    roots = [event.get("cwd")] + list(event.get("workspace_roots") or []) + [os.getcwd()]
    for r in roots:
        if not isinstance(r, str) or not r:
            continue
        p = Path(r)
        for d in (p, *p.parents):
            if (d / ".verinoda").is_dir():
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
