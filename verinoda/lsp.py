"""An installed language server as a resolver and a navigator (opt-in; CLI only).

``verinoda lsp definition|references|hover|calls|implementations|types FILE:LINE[:COL] [NAME]`` asks the
language server of the file's language one question; ``verinoda resolve-call FILE:LINE NAME --lsp`` asks it which
definition a call binds to; ``verinoda lsp verify [PATH ...]`` checks the graph's call edges against it and
records each as a relation claim through the analysis loop's own edge claim (``analysis._edge_claim``): a call
site whose definition per the server is the edge's target node (its file, and its line or the declaration
header above the name) is ``statically_verified`` with the server's answer as ``static_resolution`` evidence
naming the server and its version; one the server binds elsewhere is ``contradicted``, both locations named.

The client is a small JSON-RPC client over the server's stdin and stdout (``Content-Length`` framing; standard
library only): ``initialize``/``initialized``, ``textDocument/didOpen`` once per file, the queries, the
server's ``publishDiagnostics`` kept per file, an answer to each request the server sends (``null``, or one
``null`` per ``workspace/configuration`` item), and ``shutdown``/``exit`` with a timeout and then the process
tree stopped. One server per language per command (:class:`Session`), its workspace root the project.

Which server: ``lsp.servers`` in the config (``{"typescript": ["typescript-language-server", "--stdio"]}``),
else the known default for the language looked up on PATH (its absolute folders only). None found is
``unknown`` with the next step naming what to install; Verinoda never installs one.

Trust is the boundary (:func:`verinoda.paths.is_trusted`): a language server runs third-party code over the
project - tsserver loads the plugins ``tsconfig.json`` names, jdtls imports Gradle and Maven builds,
rust-analyzer runs build scripts and procedural macros, gopls runs ``go list`` - so none is started for a project
the user has not trusted, wherever the server itself is installed. ``lsp`` is a protected setting: a
repository's own ``.verinoda/config.json`` sets it only when the project is trusted. The MCP server never starts
one.

Never a silent pass: a server that is not found, not started, exits, does not answer within the timeout, or
writes output that is not LSP leaves the question ``unknown`` with the reason, and its process is stopped.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

START_TIMEOUT_S = 60.0       # starting a server and its initialize answer
REQUEST_TIMEOUT_S = 30.0     # one query; --timeout changes it
SHUTDOWN_TIMEOUT_S = 3.0     # shutdown's answer, then the exit notification's effect; then the tree is stopped
MAX_HEADER = 8 * 1024        # bytes of one message's header block
MAX_BODY = 64 * 1024 * 1024  # bytes of one message's body
STDERR_TAIL = 4096           # bytes of the server's stderr kept (shown when it fails)
DEFAULT_SITES = 200          # fresh call-site questions per command (config budget.lsp_sites)
DEFAULT_SECONDS = 120.0      # seconds spent on them (config budget.lsp_seconds)
MAX_RESULTS = 200            # locations listed per answer; the rest counted
HEADER_GAP = 12              # lines between a graph node's line and the server's name line read as one header
OPS = ("definition", "references", "hover", "calls", "implementations", "types")

# file suffix -> (server key, LSP languageId)
LANGUAGES = {
    ".ts": ("typescript", "typescript"), ".mts": ("typescript", "typescript"), ".cts": ("typescript", "typescript"),
    ".tsx": ("typescript", "typescriptreact"), ".js": ("typescript", "javascript"),
    ".mjs": ("typescript", "javascript"), ".cjs": ("typescript", "javascript"),
    ".jsx": ("typescript", "javascriptreact"), ".java": ("java", "java"), ".go": ("go", "go"),
    ".py": ("python", "python"), ".pyi": ("python", "python"), ".rs": ("rust", "rust"),
    ".c": ("c", "c"), ".h": ("c", "c"), ".cc": ("cpp", "cpp"), ".cpp": ("cpp", "cpp"), ".cxx": ("cpp", "cpp"),
    ".hpp": ("cpp", "cpp"), ".hh": ("cpp", "cpp"), ".hxx": ("cpp", "cpp"),
}
DEFAULT_SERVERS = {
    "typescript": ["typescript-language-server", "--stdio"], "java": ["jdtls"], "go": ["gopls"],
    "python": ["pyright-langserver", "--stdio"], "rust": ["rust-analyzer"], "c": ["clangd"], "cpp": ["clangd"],
}
INSTALL = {
    "typescript": "install typescript-language-server and typescript (npm install -g typescript-language-server "
                  "typescript) so that `typescript-language-server` is on PATH",
    "java": "install Eclipse JDT Language Server and put its `jdtls` launcher on PATH",
    "go": "install gopls (go install golang.org/x/tools/gopls@latest) and put it on PATH",
    "python": "install pyright's Node package (npm install -g pyright) so that `pyright-langserver` is on PATH",
    "rust": "install rust-analyzer (rustup component add rust-analyzer) and put it on PATH",
    "c": "install clangd (LLVM) and put it on PATH", "cpp": "install clangd (LLVM) and put it on PATH",
}
_PROVIDER = {"definition": "definitionProvider", "references": "referencesProvider", "hover": "hoverProvider",
             "calls": "callHierarchyProvider", "implementations": "implementationProvider",
             "types": "typeHierarchyProvider"}
_OUTSIDE_PARTS = {".venv", "venv", "site-packages", "node_modules", ".tox", ".nox", ".gradle", "target"}
_HEADER_LINE = re.compile(r"^\s*(?:$|@|//|/\*|\*|#\[|(?:export|public|private|protected|internal|static|async|"
                          r"abstract|final|override|default|declare|synchronized|native|open|inline)\b)")
_WORD = r"[\w$]"


UNTRUSTED = ("a language server runs third-party code over the project (its build files, compiler plugins, "
             "procedural macros): none is started in a project that is not trusted")


class LspError(Exception):
    """A server that could not answer: ``reason`` says why."""


# -- the client ----------------------------------------------------------------------------------------

def _kill_tree(proc: subprocess.Popen) -> None:
    from verinoda.codecheck_external import _kill_tree as kill

    kill(proc)


class Client:
    """One language server process: start, initialize, ask, stop."""

    def __init__(self, argv: list[str], root: Path, *, timeout: float = REQUEST_TIMEOUT_S,
                 start_timeout: float = START_TIMEOUT_S):
        self.argv, self.root, self.timeout, self.start_timeout = list(argv), Path(root), timeout, start_timeout
        self.proc: subprocess.Popen | None = None
        self.broken = ""                       # why the server can no longer answer
        self.name, self.version = Path(argv[0]).stem, ""
        self.capabilities: dict = {}
        self.diagnostics: dict[str, list] = {}   # uri -> the last published diagnostics
        self.opened: set[str] = set()
        self.started_s = 0.0
        self._next = 0
        self._pending: dict[int, threading.Event] = {}
        self._answers: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._wlock = threading.Lock()
        self._stderr: deque = deque(maxlen=STDERR_TAIL)
        self._threads: list[threading.Thread] = []

    # process -------------------------------------------------------------------------------------------
    def start(self) -> "Client":
        t0 = time.perf_counter()
        env = {**os.environ, "NoDefaultCurrentDirectoryInExePath": "1", "NO_COLOR": "1"}
        kw: dict = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else \
            {"start_new_session": True}
        try:
            self.proc = subprocess.Popen(self.argv, cwd=str(self.root), stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, **kw)
        except OSError as exc:
            raise LspError(f"{self.argv[0]} could not be started: {exc}") from exc
        for fn in (self._read_loop, self._drain_stderr):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self._threads.append(t)
        try:
            res = self.request("initialize", {
                "processId": os.getpid(), "clientInfo": {"name": "verinoda"},
                "rootUri": self.root.as_uri(), "rootPath": str(self.root),
                "workspaceFolders": [{"uri": self.root.as_uri(), "name": self.root.name}],
                "capabilities": {
                    "general": {"positionEncodings": ["utf-16"]},
                    "workspace": {"configuration": True, "workspaceFolders": True},
                    "textDocument": {
                        "synchronization": {"didSave": False},
                        "definition": {"linkSupport": True}, "implementation": {"linkSupport": True},
                        "references": {}, "hover": {"contentFormat": ["plaintext", "markdown"]},
                        "callHierarchy": {}, "typeHierarchy": {},
                        "publishDiagnostics": {"relatedInformation": False}}},
            }, timeout=self.start_timeout)
            res = res if isinstance(res, dict) else {}
            self.capabilities = res.get("capabilities") if isinstance(res.get("capabilities"), dict) else {}
            info = res.get("serverInfo") if isinstance(res.get("serverInfo"), dict) else {}
            self.name = str(info.get("name") or self.name)[:80]
            self.version = str(info.get("version") or "")[:40]
            self.notify("initialized", {})
        except LspError:
            self.close()
            raise
        self.started_s = time.perf_counter() - t0
        return self

    @property
    def tool(self) -> str:
        return f"{self.name} {self.version}".strip()

    def stderr_tail(self) -> str:
        return bytes(self._stderr).decode("utf-8", "replace").strip()[-600:]

    def _fail(self, why: str) -> None:
        with self._lock:
            if not self.broken:
                self.broken = why
            for ev in self._pending.values():
                ev.set()

    def _drain_stderr(self) -> None:
        stream = self.proc.stderr
        try:
            while True:
                chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
                if not chunk:
                    return
                self._stderr.extend(chunk)
        except (OSError, ValueError):
            return

    def _read_loop(self) -> None:
        stream = self.proc.stdout
        try:
            while True:
                msg = _read_message(stream)
                if msg is None:
                    rc = self.proc.poll()
                    if rc is None:
                        try:
                            rc = self.proc.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            rc = None
                    tail = self.stderr_tail()
                    self._fail("the server exited" + (f" (exit code {rc})" if rc is not None else "")
                               + (f"; it wrote: {tail[-300:]}" if tail else ""))
                    return
                self._dispatch(msg)
        except _Garbage as exc:
            self._fail(f"the server wrote output that is not LSP ({exc})")
        except (OSError, ValueError) as exc:
            self._fail(f"reading the server failed: {exc}")

    def _dispatch(self, msg: dict) -> None:
        method = msg.get("method")
        if method is None and "id" in msg:            # an answer
            with self._lock:
                ev = self._pending.get(msg["id"]) if isinstance(msg["id"], int) else None
                if ev is not None:
                    self._answers[msg["id"]] = msg
                    ev.set()
            return
        if not isinstance(method, str):
            return
        if "id" in msg:                                # a request from the server: always answered
            params = msg.get("params") or {}
            result = None
            if method == "workspace/configuration":
                result = [None] * len(params.get("items") or [])
            elif method == "workspace/workspaceFolders":
                result = [{"uri": self.root.as_uri(), "name": self.root.name}]
            try:
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            except LspError:
                pass   # the client is broken now; the waiting request sees why
            return
        if method == "textDocument/publishDiagnostics":
            p = msg.get("params") or {}
            if isinstance(p.get("uri"), str) and isinstance(p.get("diagnostics"), list):
                self.diagnostics[p["uri"]] = p["diagnostics"][:MAX_RESULTS]

    def _send(self, msg: dict) -> None:
        body = json.dumps(msg).encode("utf-8")
        try:
            with self._wlock:
                self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
                self.proc.stdin.flush()
        except (OSError, ValueError, AttributeError) as exc:
            self._fail(f"writing to the server failed ({type(exc).__name__}): it has stopped")
            raise LspError(self.broken) from exc

    def notify(self, method: str, params) -> None:
        if self.broken:
            raise LspError(self.broken)
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params, timeout: float | None = None):
        """The ``result`` of one request; :class:`LspError` when the server answers with an error, stops,
        writes something that is not LSP or does not answer within ``timeout`` (then it is not asked again)."""
        if self.broken:
            raise LspError(self.broken)
        with self._lock:
            self._next += 1
            rid = self._next
            ev = self._pending[rid] = threading.Event()
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            limit = self.timeout if timeout is None else timeout
            if not ev.wait(limit):
                self._fail(f"no answer to {method} within {limit:g} s")
                try:   # tell the server to stop working on it; it is not asked again
                    self._send({"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": rid}})
                except LspError:
                    pass
            with self._lock:
                msg = self._answers.pop(rid, None)
            if msg is None:
                raise LspError(self.broken or f"no answer to {method}")
        finally:
            with self._lock:
                self._pending.pop(rid, None)
        if "error" in msg:
            err = msg.get("error") or {}
            raise LspError(f"{method}: the server answered with error {err.get('code')}: "
                           f"{str(err.get('message'))[:200]}")
        return msg.get("result")

    def open(self, path: Path, language_id: str) -> str:
        """``didOpen`` once per file; the file's URI."""
        uri = path.as_uri()
        if uri not in self.opened:
            text = path.read_text(encoding="utf-8", errors="replace")
            self.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": language_id,
                                                                  "version": 1, "text": text}})
            self.opened.add(uri)
        return uri

    def advertises(self, op: str) -> bool:
        v = self.capabilities.get(_PROVIDER.get(op, op))
        return bool(v) and v is not False

    def close(self) -> None:
        """shutdown and exit, then the process tree is stopped whatever happened."""
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None and not self.broken:
            try:
                self.request("shutdown", None, timeout=SHUTDOWN_TIMEOUT_S)
                self.notify("exit", None)
                proc.wait(timeout=SHUTDOWN_TIMEOUT_S)
            except (LspError, subprocess.TimeoutExpired):
                pass
        if proc.poll() is None:
            _kill_tree(proc)
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        # the readers end at the end of their pipes; a pipe is closed here only once its reader has ended (closing
        # a buffered pipe another thread is blocked reading would wait for that read)
        for t in self._threads:
            t.join(timeout=2)
        for stream, t in zip((proc.stdout, proc.stderr), self._threads):
            if not t.is_alive():
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        self._fail("the server was stopped")

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class _Garbage(ValueError):
    pass


def _read_message(stream) -> dict | None:
    """One message, None at the end of the stream; :class:`_Garbage` for anything that is not one."""
    size, seen = None, 0
    while True:
        line = stream.readline(MAX_HEADER + 1)
        if not line:
            return None
        seen += len(line)
        if seen > MAX_HEADER:
            raise _Garbage("a header longer than 8 KB")
        if line in (b"\r\n", b"\n"):
            if size is None:
                raise _Garbage("a message without Content-Length")
            break
        name, sep, value = line.decode("ascii", "replace").partition(":")
        if not sep or not re.fullmatch(r"[A-Za-z-]+", name.strip()):
            raise _Garbage(f"the line {line[:60]!r}")
        if name.strip().lower() == "content-length":
            if not value.strip().isdigit():
                raise _Garbage(f"Content-Length {value.strip()[:20]!r}")
            size = int(value.strip())
    if size > MAX_BODY:
        raise _Garbage(f"a message of {size} bytes")
    body = stream.read(size)
    if len(body) < size:
        return None
    try:
        msg = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _Garbage(f"a body that is not JSON: {exc}") from exc
    if not isinstance(msg, dict):
        raise _Garbage("a body that is not a JSON object")
    return msg


# -- which server --------------------------------------------------------------------------------------

def language_of(path: str) -> tuple[str, str] | None:
    """(server key, languageId) of a file, None for a language no known server serves."""
    return LANGUAGES.get(Path(path).suffix.lower())


def configured(repo: Path) -> dict[str, list[str]]:
    """``lsp.servers`` of the config: {language: argv} with every argv a non-empty list of strings."""
    from verinoda.paths import load_config

    servers = (load_config(repo).get("lsp") or {}).get("servers") or {}
    if not isinstance(servers, dict):
        return {}
    return {str(k): [str(a) for a in v] for k, v in servers.items()
            if isinstance(v, list) and v and all(isinstance(a, str) and a for a in v)}


def find_server(repo: Path, language: str) -> tuple[list[str] | None, str, str]:
    """(argv, why not, next step) for ``language`` in ``repo``: the configured command, else the known
    default on PATH. Never starts anything."""
    from verinoda.codecheck_external import _cmd_safe, on_path, trust_step
    from verinoda.paths import is_trusted

    repo = Path(repo).resolve()
    if not is_trusted(repo):
        return None, UNTRUSTED, trust_step(repo)
    cfg = configured(repo)
    argv = cfg.get(language) or DEFAULT_SERVERS.get(language)
    if not argv:
        return None, f"no language server is known for {language}", \
            f"name one in the config: \"lsp\": {{\"servers\": {{\"{language}\": [\"server\", \"--stdio\"]}}}}"
    where = "lsp.servers in the config" if language in cfg else "the default"
    exe = argv[0]
    p = Path(exe)
    if p.is_absolute():
        found = p if p.is_file() else None
    elif "/" in exe or "\\" in exe:
        found = (repo / p) if (repo / p).is_file() else None
    else:
        found = on_path(exe)
    if found is None:
        nxt = INSTALL.get(language, f"install a {language} language server")
        return None, f"{exe} ({where}) was not found" + ("" if p.is_absolute() or "/" in exe or "\\" in exe
                                                          else " on PATH"), \
            (f"{nxt}, or set \"lsp\": {{\"servers\": {{\"{language}\": [...]}}}} in the user config; Verinoda "
             "never installs one")
    if not _cmd_safe(found, argv[1:]):
        return None, (f"{found} is a batch file, and its path or an argument holds a character cmd.exe would "
                      "interpret (one of \" & | < > ^ % ! ( )): not started"), \
            "start it from a folder whose path has none of those characters, or name the server's program itself"
    return [str(found), *argv[1:]], "", ""


# -- one command's servers -----------------------------------------------------------------------------

class Session:
    """The language servers of one command: one per language, started on first use, all stopped by
    :meth:`close` (or the ``with`` block)."""

    def __init__(self, repo: Path, *, timeout: float = REQUEST_TIMEOUT_S, start_timeout: float = START_TIMEOUT_S,
                 budget=None):
        self.repo = Path(repo).resolve()
        self.timeout, self.start_timeout = timeout, start_timeout
        self.clients: dict[str, Client] = {}
        self.status: dict[str, dict] = {}
        self.budget = budget
        self.memo: dict[tuple, dict] = {}
        self._lines: dict[str, list[str] | None] = {}
        self._rels: dict[str, tuple[str, bool]] = {}
        self._real_repo: Path | None = None

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        for lang, c in self.clients.items():
            c.close()
            self.status[lang]["stopped"] = True
        self.clients.clear()

    def serves(self, path: str) -> bool:
        return language_of(path) is not None

    def client(self, language: str) -> Client:
        """The running server for ``language``; :class:`LspError` (the status says why) when there is none."""
        c = self.clients.get(language)
        if c is not None and not c.broken:
            return c
        st = self.status.get(language)
        if st is not None and st["status"] != "ready":
            raise LspError(st["reason"])
        if c is not None:      # it broke: the reason stays, it is not restarted within one command
            st = self.status[language]
            st.update(status="failed", reason=c.broken)
            raise LspError(c.broken)
        argv, why, nxt = find_server(self.repo, language)
        if argv is None:
            self.status[language] = {"language": language, "status": "not_run" if why == UNTRUSTED else
                                     "not_found", "reason": why, "next_step": nxt}
            raise LspError(why)
        c = Client(argv, self.repo, timeout=self.timeout, start_timeout=self.start_timeout)
        try:
            c.start()
        except LspError as exc:
            self.status[language] = {"language": language, "status": "failed", "argv": argv,
                                     "reason": str(exc), "next_step": _failed_step(argv)}
            raise
        self.clients[language] = c
        self.status[language] = {"language": language, "status": "ready", "server": c.name,
                                 "version": c.version, "argv": argv, "start_s": round(c.started_s, 3)}
        return c

    def fail_status(self, language: str) -> None:
        """A server that broke (exited, hung, wrote garbage) is ``failed``; an error answer or a site that is
        not on its line leaves it ``ready``."""
        c, st = self.clients.get(language), self.status.get(language)
        if c is not None and c.broken and st is not None and st.get("status") == "ready":
            st.update(status="failed", reason=c.broken, next_step=_failed_step(st.get("argv") or []))

    def lines(self, path: str) -> list[str] | None:
        if path not in self._lines:
            p = Path(path) if Path(path).is_absolute() else self.repo / path
            try:
                self._lines[path] = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                self._lines[path] = None
        return self._lines[path]

    def rel(self, abs_path: Path) -> tuple[str, bool]:
        """(repository-relative posix path or the absolute path, inside the project's own code)."""
        key = str(abs_path)
        hit = self._rels.get(key)
        if hit is None:
            if self._real_repo is None:
                self._real_repo = Path(os.path.realpath(self.repo))
            try:
                rel = Path(os.path.realpath(abs_path)).relative_to(self._real_repo)
                hit = (rel.as_posix(), not (set(rel.parts) & _OUTSIDE_PARTS))
            except ValueError:
                hit = (key, False)
            self._rels[key] = hit
        return hit

    # positions -------------------------------------------------------------------------------------------
    def location(self, obj) -> dict | None:
        """A Location or LocationLink as {path, line, col, in_repo[, span]} (1-based, character columns)."""
        if not isinstance(obj, dict):
            return None
        uri = obj.get("targetUri") or obj.get("uri")
        rng = obj.get("targetSelectionRange") or obj.get("range")
        if not isinstance(uri, str) or not isinstance(rng, dict):
            return None
        path = path_of_uri(uri)
        if path is None:
            return {"uri": uri[:200], "in_repo": False}
        rel, inside = self.rel(path)
        start = rng.get("start") or {}
        line0, ch = int(start.get("line", 0)), int(start.get("character", 0))
        lines = self.lines(rel if inside else str(path))
        col = char_col(lines[line0], ch) if lines and 0 <= line0 < len(lines) else ch
        out = {"path": rel, "line": line0 + 1, "col": col + 1, "in_repo": inside}
        full = obj.get("targetRange")
        if isinstance(full, dict):
            out["span"] = [int((full.get("start") or {}).get("line", line0)) + 1,
                           int((full.get("end") or {}).get("line", line0)) + 1]
        return out


def _failed_step(argv: list[str]) -> str:
    return (f"run `{' '.join(argv[:3])}` yourself to see why it stops, or point lsp.servers at a working server "
            "in the user config")


def path_of_uri(uri: str) -> Path | None:
    u = urlparse(uri)
    if u.scheme != "file":
        return None
    p = url2pathname(unquote(u.path))
    if os.name == "nt" and len(p) > 2 and p[0] in "\\/" and p[2] == ":":
        p = p[1:]
    if u.netloc and os.name == "nt":
        p = f"\\\\{u.netloc}{p}"
    return Path(p)


def utf16_col(text: str, char_col: int) -> int:
    """A 0-based character column as LSP's UTF-16 code units."""
    return len(text[:char_col].encode("utf-16-le")) // 2


def char_col(text: str, utf16: int) -> int:
    """LSP's 0-based UTF-16 column as a character column."""
    units = 0
    for i, ch in enumerate(text):
        if units >= utf16:
            return i
        units += 2 if ord(ch) > 0xFFFF else 1
    return len(text)


def token_cols(text: str, token: str) -> tuple[list[int], list[int]]:
    """(0-based columns where ``token`` is called or constructed, every other column it stands at)."""
    calls, other = [], []
    for m in re.finditer(rf"(?<!{_WORD}){re.escape(token)}(?!{_WORD})", text):
        after = text[m.end():]
        before = text[:m.start()]
        if re.match(r"\s*(?:<[^()]*>\s*)?(?:\?\.)?\(", after) or re.search(r"\bnew\s+(?:[\w$]+\.)*$", before):
            calls.append(m.start())
        else:
            other.append(m.start())
    return calls, other


def _position(s: Session, rel: str, line: int, col: int | None, name: str | None) -> tuple[int, str]:
    """(0-based character column, token) of the site; :class:`LspError` when it is not on the line."""
    lines = s.lines(rel)
    if lines is None:
        raise LspError(f"{rel} cannot be read")
    if not 1 <= line <= len(lines):
        raise LspError(f"{rel} has {len(lines)} lines, not {line}")
    text = lines[line - 1]
    if col is not None:
        if not 1 <= col <= len(text) + 1:
            raise LspError(f"line {line} of {rel} has {len(text)} characters, not column {col}")
        m = re.match(rf"{_WORD}+", text[col - 1:])
        return col - 1, (m.group(0) if m else name or "")
    if name:
        calls, other = token_cols(text, name)
        if not calls and not other:
            raise LspError(f"no {name!r} on {rel}:{line}")
        return (calls or other)[0], name
    m = re.search(rf"{_WORD}+", text)
    if not m:
        raise LspError(f"{rel}:{line} holds no name")
    return m.start(), m.group(0)


# -- navigation ----------------------------------------------------------------------------------------

def navigate(s: Session, op: str, rel: str, line: int, col: int | None = None, name: str | None = None) -> dict:
    """One question to the server of ``rel``'s language. ``status``: ``answered`` or ``unknown`` (``reason``,
    and ``next_step`` when the user can do something about it)."""
    t0 = time.perf_counter()
    out: dict = {"op": op, "site": f"{rel}:{line}", "status": "unknown"}
    lang = language_of(rel)
    if lang is None:
        out.update(reason=f"no language server is known for {Path(rel).suffix or 'files without a suffix'}")
        return out
    try:
        c0, token = _position(s, rel, line, col, name)
        out.update(site=f"{rel}:{line}:{c0 + 1}", token=token)
        c = s.client(lang[0])
        out["server"] = c.tool
        if not c.advertises(op):
            out["reason"] = f"{c.tool} does not advertise {_PROVIDER[op]}"
            return out
        uri = c.open(s.repo / rel, lang[1])
        text = s.lines(rel)[line - 1]
        pos = {"textDocument": {"uri": uri}, "position": {"line": line - 1, "character": utf16_col(text, c0)}}
        out.update(_ask(s, c, op, pos))
        out["status"] = "answered"
        diags = c.diagnostics.get(uri)
        if diags:
            out["diagnostics"] = [_diag(d) for d in diags[:50]]
    except LspError as exc:
        out["reason"] = str(exc)
        s.fail_status(lang[0])
        st = s.status.get(lang[0]) or {}
        if st.get("next_step"):
            out["next_step"] = st["next_step"]
        if st.get("server") and "server" not in out:
            out["server"] = f"{st['server']} {st.get('version') or ''}".strip()
    out["seconds"] = round(time.perf_counter() - t0, 3)
    return out


def _locs(s: Session, res) -> list[dict]:
    items = res if isinstance(res, list) else [res] if isinstance(res, dict) else []
    out, seen = [], set()
    for it in items:
        loc = s.location(it)
        if loc is None:
            continue
        key = (loc.get("path"), loc.get("line"), loc.get("col"))
        if key not in seen:
            seen.add(key)
            out.append(loc)
    return out


def _item(s: Session, it: dict) -> dict:
    loc = s.location({"uri": it.get("uri"), "range": it.get("selectionRange") or it.get("range")}) or {}
    return {"name": str(it.get("name"))[:200], "kind": it.get("kind"), **loc}


def _ask(s: Session, c: Client, op: str, pos: dict) -> dict:
    if op == "definition":
        return _listed("definitions", _locs(s, c.request("textDocument/definition", pos)))
    if op == "implementations":
        return _listed("implementations", _locs(s, c.request("textDocument/implementation", pos)))
    if op == "references":
        res = c.request("textDocument/references", {**pos, "context": {"includeDeclaration": False}})
        return _listed("references", _locs(s, res))
    if op == "hover":
        return {"hover": _hover_text(c.request("textDocument/hover", pos))}
    if op == "calls":
        items = c.request("textDocument/prepareCallHierarchy", pos) or []
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            return {"item": None, "outgoing": [], "incoming": []}
        it = items[0]
        outgoing = c.request("callHierarchy/outgoingCalls", {"item": it}) or []
        incoming = c.request("callHierarchy/incomingCalls", {"item": it}) or []
        return {"item": _item(s, it),
                "outgoing": [_call(s, x, "to") for x in outgoing[:MAX_RESULTS] if isinstance(x, dict)],
                "incoming": [_call(s, x, "from") for x in incoming[:MAX_RESULTS] if isinstance(x, dict)]}
    if op == "types":
        items = c.request("textDocument/prepareTypeHierarchy", pos) or []
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            return {"item": None, "supertypes": [], "subtypes": []}
        it = items[0]
        sup = c.request("typeHierarchy/supertypes", {"item": it}) or []
        sub = c.request("typeHierarchy/subtypes", {"item": it}) or []
        return {"item": _item(s, it), "supertypes": [_item(s, x) for x in sup[:MAX_RESULTS] if isinstance(x, dict)],
                "subtypes": [_item(s, x) for x in sub[:MAX_RESULTS] if isinstance(x, dict)]}
    raise LspError(f"unknown question {op}")


def _listed(key: str, locs: list[dict]) -> dict:
    out = {key: locs[:MAX_RESULTS]}
    if len(locs) > MAX_RESULTS:
        out["more"] = len(locs) - MAX_RESULTS
    return out


def _call(s: Session, x: dict, side: str) -> dict:
    it = x.get(side) if isinstance(x.get(side), dict) else {}
    lines = [int((r.get("start") or {}).get("line", 0)) + 1 for r in x.get("fromRanges") or [] if isinstance(r, dict)]
    return {**_item(s, it), "at_lines": lines[:20]}


def _hover_text(res) -> str | None:
    if not isinstance(res, dict):
        return None
    c = res.get("contents")
    parts = c if isinstance(c, list) else [c]
    out = []
    for p in parts:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, dict) and isinstance(p.get("value"), str):
            out.append(p["value"])
    text = "\n".join(out).strip()
    return text[:4000] or None


def _diag(d: dict) -> dict:
    start = ((d.get("range") or {}).get("start") or {})
    return {"line": int(start.get("line", 0)) + 1, "severity": {1: "error", 2: "warning", 3: "information",
                                                                 4: "hint"}.get(d.get("severity"), "error"),
            "message": str(d.get("message"))[:300], **({"code": d["code"]} if d.get("code") is not None else {})}


# -- call-site resolution (the precise.resolve_call shape) ---------------------------------------------

def resolve_call(s: Session, path: str, line: int, target_label: str, *, target_path: str | None = None,
                 target_line: int | None = None) -> dict | None:
    """Which definition does the call to ``target_label`` on ``path:line`` bind to, per the language server?

    The answer has ``precise.resolve_call``'s shape (``tool`` is the server's name and version; ``kind``
    definitive / dynamic / ambiguous / external / unresolved; ``targets``; with a target ``verdict`` confirms /
    refutes / undetermined), so it goes through ``precise.resolution_evidence`` and the claim rules like jedi's.
    A server that could not answer gives ``failed: True`` with the reason (never a verdict). None: the
    session's budget is spent."""
    from verinoda.precise import target_token

    rel = path.replace("\\", "/")
    token = target_token(target_label)
    key = (rel, int(line), token, target_path, target_line)
    if key in s.memo:
        return dict(s.memo[key], cached=True)
    lang = language_of(rel)
    base = {"path": rel, "line": int(line), "token": token, "tool": "lsp"}
    if lang is None:
        return {**base, "kind": "unresolved", "targets": [], "failed": True,
                "reason": f"no language server is known for {Path(rel).suffix}"}
    if s.budget is not None and not s.budget.allow():
        return None
    t0 = time.perf_counter()
    try:
        out = _resolve(s, lang, rel, int(line), token, target_path, target_line, base)
    except LspError as exc:
        s.fail_status(lang[0])
        st = s.status.get(lang[0]) or {}
        out = {**base, "kind": "unresolved", "targets": [], "failed": True, "reason": str(exc),
               **({"next_step": st["next_step"]} if st.get("next_step") else {}),
               **({"tool": f"{st['server']} {st.get('version') or ''}".strip()} if st.get("server") else {})}
    elapsed = time.perf_counter() - t0
    if s.budget is not None:
        s.budget.charge(elapsed)
    out["elapsed_ms"] = round(elapsed * 1000, 1)
    s.memo[key] = out
    return dict(out, cached=False)


def _resolve(s: Session, lang: tuple[str, str], rel: str, line: int, token: str, target_path: str | None,
             target_line: int | None, base: dict) -> dict:
    lines = s.lines(rel)
    if lines is None or not 1 <= line <= len(lines):
        return {**base, "kind": "unresolved", "targets": [], "failed": True,
                "reason": f"{rel}:{line} cannot be read"}
    text = lines[line - 1]
    calls, other = token_cols(text, token)
    c = s.client(lang[0])
    base = {**base, "tool": c.tool}
    if not calls and not other:
        return {**base, "kind": "unresolved", "targets": [], "reason": f"no call to {token!r} on line {line}"}
    if not c.advertises("definition"):
        raise LspError(f"{c.tool} does not advertise definitionProvider")
    uri = c.open(s.repo / rel, lang[1])
    answers = []
    for col in (calls or other)[:3]:
        pos = {"textDocument": {"uri": uri}, "position": {"line": line - 1, "character": utf16_col(text, col)}}
        answers.append((col, _locs(s, c.request("textDocument/definition", pos))))
    col = answers[0][0]
    targets = []
    for _, locs in answers:
        for t in locs:
            if (t.get("path"), t.get("line")) not in {(x.get("path"), x.get("line")) for x in targets}:
                targets.append({**t, "name": token, "type": None, "qualname": None})
    base = {**base, "col": col}
    if not targets:
        out = {**base, "kind": "unresolved", "targets": [], "reason": f"{c.tool} found no definition"}
    elif len(targets) > 1:
        out = {**base, "kind": "ambiguous", "targets": targets,
               "reason": f"{len(targets)} definitions" + (" for the calls on this line" if len(answers) > 1 else "")}
    elif not targets[0].get("in_repo"):
        out = {**base, "kind": "external", "targets": targets, "reason": "the definition is outside the project"}
    elif not calls:
        out = {**base, "kind": "dynamic", "targets": targets, "site": "reference",
               "reason": f"{token!r} is referenced, not called, on this line: whether and when it runs is decided "
                         "at runtime"}
    else:
        out = {**base, "kind": "definitive", "targets": targets, "reason": None}
    if target_path:
        _judge(s, c, lang, out, target_path.replace("\\", "/"), target_line)
    return out


def matches(s: Session, t: dict, target_path: str, target_line: int | None) -> bool:
    """Is the server's location ``t`` the graph node at ``target_path:target_line``? The same file and the same
    line, or the node's line opens the declaration whose name the server points at (annotations, decorators,
    modifiers and comments between them), or the server's full range starts on the node's line."""
    if t.get("path") != target_path:
        return False
    if not target_line or t.get("line") == target_line:
        return True
    if (t.get("span") or [None])[0] == target_line:
        return True
    ln = t.get("line") or 0
    if target_line < ln <= target_line + HEADER_GAP:
        lines = s.lines(target_path) or []
        gap = lines[target_line - 1:ln - 1]
        return len(gap) == ln - target_line and all(_HEADER_LINE.match(x) for x in gap)
    return False


def _judge(s: Session, c: Client, lang: tuple[str, str], out: dict, tp: str, tl: int | None) -> None:
    """``verdict`` against the graph's target; a definitive miss is first checked against the
    implementations of the definition (an interface or overridable method: the runtime class decides)."""
    if out["kind"] != "definitive":
        out["verdict"] = "undetermined"
        return
    if any(matches(s, t, tp, tl) for t in out["targets"]):
        out["verdict"] = "confirms"
        return
    d = out["targets"][0]
    if c.advertises("implementations") and language_of(d["path"]) is not None:
        lines = s.lines(d["path"]) or []
        if 0 < d["line"] <= len(lines):
            uri = c.open(s.repo / d["path"], (language_of(d["path"]) or lang)[1])
            pos = {"textDocument": {"uri": uri}, "position": {
                "line": d["line"] - 1, "character": utf16_col(lines[d["line"] - 1], d["col"] - 1)}}
            try:
                impls = _locs(s, c.request("textDocument/implementation", pos))
            except LspError as exc:
                if c.broken:
                    raise
                # an error answer: whether the target implements the definition is not known
                out.update(verdict="undetermined", reason=f"the call binds to {d['path']}:{d['line']}; the "
                                                          f"server could not list its implementations ({exc})")
                return
            if any(matches(s, t, tp, tl) for t in impls):
                out.update(kind="dynamic", verdict="undetermined", implementations=impls[:5],
                           reason=f"the call binds to {d['path']}:{d['line']}, and the graph's target is one of its "
                                  "implementations: the runtime class decides which one runs")
                return
    out["verdict"] = "refutes"


# -- configuration and budget --------------------------------------------------------------------------

def budget_from_config(repo: Path):
    from verinoda.paths import load_config
    from verinoda.precise import Budget

    b = load_config(repo).get("budget") or {}
    try:
        return Budget(int(b.get("lsp_sites", DEFAULT_SITES)), float(b.get("lsp_seconds", DEFAULT_SECONDS)))
    except (TypeError, ValueError):
        return Budget(DEFAULT_SITES, DEFAULT_SECONDS)


# -- graph edges -----------------------------------------------------------------------------------------

def _settle(rec, c: dict, res: dict, commit: str | None) -> dict:
    """A claim of the same text recorded earlier in this snapshot (by a question, or a verify before) is
    reused, not created again: the server's answer is attached to it and the status it allows requested,
    rule-checked like any other (a refused request leaves the claim as it was, recorded in its history)."""
    from verinoda import claims as cm
    from verinoda import precise

    verdict = res.get("verdict")
    if verdict not in ("confirms", "refutes"):
        return c
    want = "statically_verified" if verdict == "confirms" else "contradicted"
    if c["status"] == want:
        return c
    ev = precise.resolution_evidence(rec.repo, res, commit=commit)
    if ev is None:
        return c
    ev["locator"] = f"{res.get('path')}:{res.get('line')} {res.get('kind')}"
    if verdict == "refutes":
        ev["meta"] = {**(ev.get("meta") or {}), "strength": "definitive"}
    rec.cl.attach(c["id"], ev, "supports" if verdict == "confirms" else "refutes")
    try:
        return rec.cl.set_status(c["id"], want, reason=f"{res.get('tool')}: the call at {res.get('path')}:"
                                 f"{res.get('line')} {verdict} the edge", actor="verinoda lsp")
    except cm.ClaimRuleError:
        return rec.cl.get(c["id"])


def verify_edges(store, repo: Path, *, paths: list[str] | None = None, max_edges: int = 100,
                 timeout: float = REQUEST_TIMEOUT_S, session: Session | None = None) -> dict:
    """Check the graph's call edges whose call site is in ``paths`` (files or folders; default every file a
    known server serves) against the language server and record each one the server answered as a relation
    claim (``analysis._edge_claim``). Edges the server could not answer are listed as ``unknown`` with the
    reason and get no claim."""
    from verinoda import analysis, index, retrieval, workflow
    from verinoda.precise import target_token
    from verinoda.store import new_id

    repo = Path(repo).resolve()
    snap, refresh = workflow._current_snapshot(store, repo)
    if refresh and refresh.get("error"):
        return {"status": "error", "error": f"the index could not be refreshed ({refresh['error']})"}
    if snap is None:
        return {"status": "error", "error": f"{repo} has no snapshot", "next_step": f"run `verinoda scan {repo}`"}
    g = index.load(repo)
    scope = [p.replace("\\", "/").rstrip("/") for p in paths or []]

    def in_scope(f: str) -> bool:
        return not scope or any(f == p or f.startswith(p + "/") or p in ("", ".") for p in scope)

    edges = []
    for u, v, d in g.edges({"calls"}):
        at = retrieval._at(d)
        if not at or ":" not in at:
            continue
        f, _, ln = at.rpartition(":")
        if ln.isdigit() and in_scope(f) and language_of(f) is not None and g.file(v):
            edges.append((f, int(ln), u, v, d))
    edges.sort(key=lambda e: (e[0], e[1], str(e[3])))
    total = len(edges)
    edges = edges[:max_edges]
    own = session is None
    s = session or Session(repo, timeout=timeout, budget=budget_from_config(repo))
    rec = analysis._Recorder(store, repo, snap, new_id("lsp"),
                             analysis.Budget(seconds=1e9, tool_calls=10 ** 9, context_tokens=10 ** 9))
    rec.lsp = s
    rows, unknown, skipped = [], [], 0
    t0 = time.perf_counter()
    try:
        for f, ln, u, v, d in edges:
            res = resolve_call(s, f, ln, g.label(v), target_path=g.file(v), target_line=g.line(v))
            edge = {"at": f"{f}:{ln}", "caller": g.label(u), "target": g.label(v),
                    "target_at": f"{g.file(v)}:{g.line(v)}" if g.line(v) else g.file(v),
                    "token": target_token(g.label(v))}
            if res is None:
                skipped += 1
                continue
            if res.get("failed"):
                unknown.append({**edge, "status": "unknown", "reason": res.get("reason"),
                                **({"next_step": res["next_step"]} if res.get("next_step") else {})})
                continue
            c = analysis._edge_claim(rec, g, u, v, d, snap["commit_sha"])
            if c is not None and c["id"] in rec.reused:
                c = _settle(rec, c, res, snap["commit_sha"])
            row = {**edge, "server": res.get("tool"), "kind": res.get("kind"), "verdict": res.get("verdict"),
                   "definition": [f"{t.get('path')}:{t.get('line')}" for t in res.get("targets") or []][:3]}
            if res.get("reason"):
                row["reason"] = res["reason"]
            if c is not None:
                row.update(claim=c["id"], status=c["status"], text=c["text"],
                           **({"reused": True} if c["id"] in rec.reused else {}))
                unc = [x for x in c.get("uncertainties") or [] if x.startswith(str(res.get("tool")))]
                if unc:
                    row["uncertainty"] = unc[0]
            rows.append(row)
    finally:
        if own:
            s.close()
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.get("status") or "no_claim"] = counts.get(r.get("status") or "no_claim", 0) + 1
    out = {"status": "done" if rows else "unknown", "edges": total, "checked": len(rows) + len(unknown),
           "claims": counts, "unknown": len(unknown), "results": rows, "unanswered": unknown[:50],
           "servers": list(s.status.values()), "seconds": round(time.perf_counter() - t0, 3)}
    if total > len(edges):
        out["not_checked"] = f"{total - len(edges)} edges past --max-edges {max_edges}"
    if skipped:
        out["not_checked_budget"] = skipped
    if s.budget is not None:
        out["budget"] = s.budget.as_dict()
    if not total:
        out["reason"] = "no graph call edge has its call site in a file a known language server serves" + \
            (f" under {', '.join(scope)}" if scope else "")
    return out
