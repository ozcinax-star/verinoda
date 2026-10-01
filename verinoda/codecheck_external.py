"""The project's own type checker as part of ``verinoda check --checker tsc|pyright|mypy|auto``.

Verinoda's own check reads names (Python, Java, Kotlin; TypeScript and JavaScript imports only). A member of a
TypeScript value, the arguments of a call, a type that does not fit: these need the compiler. When the user asks
for it, this module runs the checker the project already has, with the project's own configuration, and turns
each error it reports on the files and lines being checked into a check *site*:

``tsc``      ``node_modules/.bin/tsc`` from the ``tsconfig.json`` folder up to the repository, else ``tsc`` on
             PATH; run as ``tsc -p tsconfig.json --noEmit --pretty false --listFiles`` in that folder (the whole
             program is compiled; ``--listFiles`` says which files were in it).
``pyright``  ``node_modules/.bin/pyright``, the project's virtual environment, else PATH; ``--outputjson`` on
             the Python files being checked. pyright's PyPI wrapper is not started: on first use (and to look
             for a newer version) it downloads Node.js and the npm package.
``mypy``     the project's virtual environment, else PATH; ``-O json`` from mypy 1.11, the text form before it.
``auto``     tsc for TypeScript/JavaScript files; for Python the checker the project configures (a
             ``pyrightconfig.json`` or ``[tool.pyright]``; ``mypy.ini``, ``.mypy.ini``, ``[tool.mypy]`` or a
             ``setup.cfg`` ``[mypy]`` section), else whichever is installed (pyright, then mypy).

Trust is the boundary (:func:`verinoda.paths.is_trusted`). In a project the user has not trusted, nothing the
repository supplies is started: a checker inside it (``node_modules/.bin``, a virtual environment) is not run
(one on PATH may be), pyright is not run (it starts a Python interpreter it looks up itself), and mypy is not run
when its configuration names ``plugins`` or a ``python_executable`` (both run code). Such a run is ``not_run``
with the ``verinoda trust`` step - the user's to take, never an agent's. Every checker runs with
``NoDefaultCurrentDirectoryInExePath`` set, so a Windows launcher never picks a program from the project folder.

Nothing is installed or downloaded (no ``npx``, no ``pip``), and a checker that is not found, not run, does not
finish within the timeout, stops on a blocking error, or prints output that cannot be read is a note with the
next step - never a pass. Each site is the checker's verdict from that run (``status`` ``observed``), with the
tool, its version, the configuration and the diagnostic code: ``absent`` for a name, a member or an import the
checker did not find (a package import: ``not_installed``; a module whose types are missing: ``unknown``),
``mismatch`` for a call or a type that does not fit. A syntax error is no site: its file is not type-checked,
and the check says so. Only errors become sites; warnings are counted. The MCP server never calls this.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

CHECKERS = ("tsc", "pyright", "mypy")
CHOICES = (*CHECKERS, "auto")
KINDS = ("name", "member", "call", "type", "import")   # and "syntax": never a site
TIMEOUT_S = 300.0            # one checker run; --checker-timeout changes it
VERSION_TIMEOUT_S = 30.0
MAX_OUTPUT = 16 * 1024 * 1024   # bytes a checker may write to each stream; past it the run is stopped
MAX_SITES = 1000             # sites listed per run; the rest are counted
MAX_PROJECTS = 4             # tsconfig.json files run in one check
MAX_ARGV = 24000             # characters of a command line (Windows allows about 32,000)
TS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
JS_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs")
_CMD_UNSAFE = set('"&|<>^%!()\r\n')   # cmd.exe reads these in the path and arguments of a .cmd/.bat file

# tsc diagnostic codes by what they are about (TS1xxx: syntax; the rest are types)
_TS_NAME = {2304, 2503, 2552, 2662, 2663}
_TS_TYPES_MISSING = {2580, 2581, 2582, 2583, 2584, 2591, 2592, 2593}   # Cannot find name 'x': install @types
_TS_IMPORT = {2305, 2306, 2307, 2459, 2460, 2614, 2724, 2792, 7016}
_TS_MEMBER = {2339, 2353, 2551, 2561, 2576, 2694}
_TS_CALL = {2345, 2348, 2349, 2350, 2351, 2554, 2555, 2556, 2557, 2575, 2721, 2722, 2723, 2769}
_MYPY_KIND = {"name-defined": "name", "used-before-def": "name", "attr-defined": "member", "union-attr": "type",
              "import": "import", "import-not-found": "import", "import-untyped": "import",
              "call-arg": "call", "arg-type": "call", "call-overload": "call", "syntax": "syntax"}
_PYRIGHT_KIND = {"reportUndefinedVariable": "name", "reportAttributeAccessIssue": "member",
                 "reportOptionalMemberAccess": "type", "reportFunctionMemberAccess": "member",
                 "reportCallIssue": "call", "reportArgumentType": "call", "reportOptionalCall": "type",
                 "reportMissingImports": "import", "reportMissingModuleSource": "import",
                 "reportPrivateImportUsage": "import"}
_TSC_LINE = re.compile(r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\): (?P<sev>error|warning|message) "
                       r"(?P<code>TS\d+): (?P<msg>.*)$")
_TSC_GLOBAL = re.compile(r"^(?P<sev>error|warning|message) (?P<code>TS\d+): (?P<msg>.*)$")
_MYPY_LINE = re.compile(r"^(?P<file>.+?):(?P<line>\d+):(?:(?P<col>\d+):)?(?:\d+:\d+:)? (?P<sev>error|warning|note): "
                        r"(?P<msg>.*?)(?:  \[(?P<code>[a-z0-9-]+)\])?$")
_MYPY_CONTEXT = re.compile(r"^(?P<file>.+?): (?:note: )?(?:In |At top level)")   # show_error_context lines
_QUOTED = re.compile(r"""['"]([^'"\s]{1,120})['"]""")
_MYPY_RUNS_CODE = re.compile(r"^\s*(plugins|python_executable)\s*=", re.M)


def trust_step(repo: Path) -> str:
    return ("ask the user: if they trust this project's code, they run `verinoda trust "
            f"{repo}` themselves in a terminal; an agent must never run it for them")


@dataclass
class Diag:
    path: Path        # absolute
    line: int         # 1-based
    col: int          # 1-based
    severity: str     # error | warning | note
    code: str
    message: str


@dataclass
class Run:
    tool: str
    status: str = "ran"          # ran | not_found | not_run | timeout | failed
    exe: str = ""
    version: str = ""
    config: str = ""
    cwd: str = ""
    seconds: float = 0.0
    exit: int | None = None
    note: str = ""
    next_step: str = ""
    diags: list[Diag] = field(default_factory=list)
    program: set[str] | None = None   # tsc --listFiles: the files of the program (normcased)
    warnings: int = 0
    unparsed: int = 0
    truncated: bool = False
    files: list[str] = field(default_factory=list)   # the targets this run covers (repository-relative)
    problems: list[str] = field(default_factory=list)   # what makes a run that ran incomplete

    def header(self) -> dict:
        h = {"tool": self.tool, "status": self.status}
        for k in ("version", "exe", "config", "cwd", "note", "next_step"):
            v = getattr(self, k)
            if v:
                h[k] = v
        if self.status in ("ran", "failed", "timeout"):
            h["seconds"] = round(self.seconds, 3)
        if self.exit is not None:
            h["exit"] = self.exit
        if self.status == "ran":
            h["errors"] = sum(1 for d in self.diags if d.severity == "error")
            h["warnings"] = self.warnings
        if self.unparsed:
            h["unparsed_lines"] = self.unparsed
        if self.truncated:
            h["output_cut_at_bytes"] = MAX_OUTPUT
        if self.files:
            h["files"] = len(self.files)
        return h


# -- finding a checker -----------------------------------------------------------------------------------

def _names(name: str) -> list[str]:
    if os.name == "nt":
        exts = [e.lower() for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if e]
        first = [e for e in (".cmd", ".exe", ".bat") if e in exts]
        return [name + e for e in first + [e for e in exts if e not in first]]
    return [name]


def _in_dir(d: Path, name: str) -> Path | None:
    for n in _names(name):
        p = d / n
        if p.is_file() and (os.name == "nt" or os.access(p, os.X_OK)):
            return p
    return None


def on_path(name: str) -> Path | None:
    """``name`` in the absolute directories of PATH; never the current directory (Windows looks there first)."""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        d = d.strip().strip('"')
        if d and os.path.isabs(d):
            p = _in_dir(Path(d), name)
            if p is not None:
                return p
    return None


def _node_bins(start: Path, repo: Path) -> list[Path]:
    out = []
    d = start
    while True:
        out.append(d / "node_modules" / ".bin")
        if d == repo or d.parent == d or not _inside(d, repo):
            break
        d = d.parent
    return out


def _venv_bins(repo: Path, env: str | None) -> list[Path]:
    roots = []
    if env and env not in ("auto", "none"):
        p = Path(env)
        p = p if p.is_absolute() else repo / p
        roots.append(p.parent.parent if p.is_file() else p)   # an interpreter: its venv
    roots += [repo / n for n in (".venv", "venv", "env")]
    return [r / sub for r in roots for sub in (("Scripts",) if os.name == "nt" else ("bin",))]


def _pip_pyright(exe: Path) -> bool:
    """Whether ``exe`` is pyright's PyPI wrapper (``pyright.cli``), which downloads Node.js and pyright's npm
    package on first use and looks for a newer version on each start. Reads the first 8 MB and the last 2 MB
    (a launcher with the script appended as a zip, as pip and uv write them on Windows)."""
    try:
        with exe.open("rb") as f:
            head = f.read(8 * 1024 * 1024)
            size = f.seek(0, os.SEEK_END)
            tail = b""
            if size > len(head):
                f.seek(max(len(head), size - 2 * 1024 * 1024))
                tail = f.read()
    except OSError:
        return False
    for data in (head, tail):
        if b"pyright.cli" in data or b"pyright_python" in data:
            return True
    data = tail or head
    k = data.rfind(b"PK\x05\x06")
    if k >= 0:
        try:
            import io

            with zipfile.ZipFile(io.BytesIO(data)) as z:
                return any(b"pyright.cli" in z.read(n) for n in z.namelist() if n.endswith(".py"))
        except (zipfile.BadZipFile, OSError, ValueError, KeyError):
            return False
    return False


def find(tool: str, repo: Path, start: Path, env: str | None, trusted: bool = True) -> tuple[Path | None, str]:
    """(executable, why not) of ``tool`` for the files under ``start``: the project's own first. In a project
    that is not trusted only PATH is searched, and a program found only inside the project is named in the
    reason."""
    dirs: list[Path] = []
    if tool in ("tsc", "pyright"):
        dirs += _node_bins(start, repo)
    if tool in ("pyright", "mypy"):
        dirs += _venv_bins(repo, env)
    refused = ""
    for d in dirs:
        p = _in_dir(d, tool)
        if p is None:
            continue
        if not trusted and _inside(p, repo):
            refused = refused or (f"{_rel(p, repo)} is inside the repository, which is not trusted: a program it "
                                  "supplies is not started")
            continue
        if tool == "pyright" and "node_modules" not in p.parts and _pip_pyright(p):
            refused = f"{p} is pyright's PyPI wrapper, which downloads Node.js and pyright on use: not started"
            continue
        return p, ""
    p = on_path(tool)
    if p is not None:
        if not trusted and _inside(p, repo):
            refused = refused or f"{p} (on PATH) is inside the repository, which is not trusted: not started"
        elif tool == "pyright" and _pip_pyright(p):
            refused = f"{p} is pyright's PyPI wrapper, which downloads Node.js and pyright on use: not started"
        else:
            return p, ""
    looked = ", ".join(_rel(d, repo) for d in dirs[:4]) + (" ..." if len(dirs) > 4 else "")
    return None, refused or f"{tool} was not found ({looked + ', ' if looked else ''}PATH)"


def install_hint(tool: str) -> str:
    return {"tsc": "install TypeScript in the project (npm install --save-dev typescript), then run check again; "
                   "Verinoda never installs it",
            "pyright": "install pyright's npm package in the project (npm install --save-dev pyright) or put its "
                       "Node CLI on PATH; or use --checker mypy",
            "mypy": "install mypy in the project's virtual environment (pip install mypy), or pass --env"}[tool]


def mypy_runs_code(repo: Path) -> str:
    """The configuration file that makes mypy run code (``plugins``, ``python_executable``), or ''."""
    for n in ("mypy.ini", ".mypy.ini", "setup.cfg", "pyproject.toml"):
        p = repo / n
        try:
            text = p.read_text("utf-8", "replace") if p.is_file() else ""
        except OSError:
            continue
        m = _MYPY_RUNS_CODE.search(text)
        if m:
            return f"{n} sets {m.group(1)}"
    return ""


def gate(tool: str, repo: Path, trusted: bool) -> str:
    """Why ``tool`` is not run in this project ('' when it may be)."""
    if trusted:
        return ""
    if tool == "pyright":
        return ("pyright is not run in a project that is not trusted: it starts a Python interpreter it looks up "
                "itself, which may be one the repository supplies")
    if tool == "mypy":
        why = mypy_runs_code(repo)
        if why:
            return f"mypy is not run in a project that is not trusted when its configuration runs code ({why})"
    return ""


# -- running ---------------------------------------------------------------------------------------------

def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                       stdin=subprocess.DEVNULL)
    else:  # pragma: no cover - POSIX
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover
        proc.kill()


def run_capped(argv: list[str], cwd: Path, timeout: float) -> tuple[int | None, str, str, bool]:
    """(exit code or None on timeout, stdout, stderr, output cut). The output goes to temporary files, so a
    process the checker leaves behind cannot hold a pipe open; on timeout, or when a stream passes
    :data:`MAX_OUTPUT` bytes, the whole process tree is stopped."""
    env = {**os.environ, "NO_COLOR": "1", "FORCE_COLOR": "0", "npm_config_update_notifier": "false",
           "NoDefaultCurrentDirectoryInExePath": "1"}
    kw: dict = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else \
        {"start_new_session": True}
    with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
        proc = subprocess.Popen(argv, cwd=str(cwd), stdout=fo, stderr=fe, stdin=subprocess.DEVNULL, env=env, **kw)
        deadline = time.monotonic() + timeout
        rc: int | None = None
        cut = False
        while True:
            try:
                rc = proc.wait(timeout=min(0.25, max(0.01, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                if max(os.fstat(fo.fileno()).st_size, os.fstat(fe.fileno()).st_size) > MAX_OUTPUT:
                    _kill_tree(proc)
                    rc, cut = proc.returncode, True
                    break
                if time.monotonic() >= deadline:
                    _kill_tree(proc)
                    rc = None
                    break
        texts = []
        for f in (fo, fe):
            f.seek(0)
            data = f.read(MAX_OUTPUT + 1)
            if len(data) > MAX_OUTPUT:
                data, cut = data[:MAX_OUTPUT], True
            texts.append(data.decode("utf-8", "replace"))
    return rc, texts[0], texts[1], cut


def _cmd_safe(exe: Path, args: list[str]) -> bool:
    """A ``.cmd``/``.bat`` launcher is run by cmd.exe, which splits its command line at ``&``, ``|`` and the
    like: neither its path nor an argument may hold one."""
    if exe.suffix.lower() not in (".cmd", ".bat"):
        return True
    return not any(set(a) & _CMD_UNSAFE for a in [str(exe), *args])


def _cmd_refusal(r: Run) -> None:
    r.status = "not_run"
    r.note = (f"{r.exe} is a batch file, and its path or an argument holds a character cmd.exe would interpret "
              "(one of \" & | < > ^ % ! ( )): not started")
    r.next_step = f"run {r.tool} yourself, or from a folder whose path has none of those characters"


def version(exe: Path, cwd: Path) -> str:
    try:
        rc, out, err, _ = run_capped([str(exe), "--version"], cwd, VERSION_TIMEOUT_S)
    except OSError:
        return ""
    m = re.search(r"(\d+\.\d+(?:\.\d+)?(?:[-+.][0-9A-Za-z.]+)?)", out or err or "")
    return m.group(1) if rc == 0 and m else ""


# -- parsing ---------------------------------------------------------------------------------------------

def parse_tsc(text: str, cwd: Path) -> tuple[list[Diag], list[Diag], set[str], int]:
    """(file diagnostics, global diagnostics, files of the program from --listFiles, lines not understood)."""
    diags: list[Diag] = []
    glob: list[Diag] = []
    program: set[str] = set()
    unparsed = 0
    last: Diag | None = None
    for raw in text.splitlines():
        ln = raw.rstrip("\r")
        if not ln.strip():
            continue
        if ln[0] in " \t" and last is not None:   # a message continued on the next lines
            last.message += "\n" + ln.strip()
            continue
        m = _TSC_LINE.match(ln)
        if m:
            p = Path(m["file"])
            last = Diag(p if p.is_absolute() else cwd / p, int(m["line"]), int(m["col"]), _sev(m["sev"]),
                        m["code"], m["msg"])
            diags.append(last)
            continue
        m = _TSC_GLOBAL.match(ln)
        if m:
            last = Diag(Path(), 0, 0, _sev(m["sev"]), m["code"], m["msg"])
            glob.append(last)
            continue
        last = None
        p = Path(ln.strip())
        if (p.is_absolute() or ln.strip().startswith((".", "/"))) and p.suffix:
            program.add(_key(p if p.is_absolute() else cwd / p))
        else:
            unparsed += 1
    return diags, glob, program, unparsed


def parse_pyright(text: str, cwd: Path) -> tuple[list[Diag], str]:
    """(diagnostics, version). ValueError when the output is not pyright's JSON report."""
    i = text.find("{")
    if i < 0:
        raise ValueError("no JSON report in pyright's output")
    data = json.loads(text[i:])
    if not isinstance(data, dict) or not isinstance(data.get("generalDiagnostics"), list):
        raise ValueError("pyright's JSON has no generalDiagnostics list")
    out = []
    for d in data["generalDiagnostics"]:
        if not isinstance(d, dict) or not d.get("file"):
            continue
        start = ((d.get("range") or {}).get("start") or {})
        p = Path(str(d["file"]))
        out.append(Diag(p if p.is_absolute() else cwd / p, int(start.get("line", 0)) + 1,
                        int(start.get("character", 0)) + 1, _sev(str(d.get("severity", "error"))),
                        str(d.get("rule") or ""), str(d.get("message") or "")))
    return out, str(data.get("version") or "")


def _attach_note(diags: list[Diag], p: Path, line: int, msg: str) -> None:
    """A mypy note explains the error before it on the same line; any other note is left out."""
    if diags and diags[-1].line == line and _key(diags[-1].path) == _key(p):
        diags[-1].message += "\n" + msg


def parse_mypy_json(text: str, cwd: Path) -> tuple[list[Diag], int]:
    diags: list[Diag] = []
    unparsed = 0
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except ValueError:
            unparsed += 1
            continue
        if not isinstance(d, dict) or "file" not in d or "line" not in d:
            unparsed += 1
            continue
        p = Path(str(d["file"]))
        p = p if p.is_absolute() else cwd / p
        col = d.get("column")
        msg = str(d.get("message") or "")
        if d.get("hint"):
            msg += "\n" + str(d["hint"])
        sev = _sev(str(d.get("severity", "error")))
        if sev == "note":
            _attach_note(diags, p, int(d["line"]), msg)
            continue
        diags.append(Diag(p, int(d["line"]), int(col) + 1 if isinstance(col, int) and col >= 0 else 1, sev,
                          str(d.get("code") or ""), msg))
    return diags, unparsed


def parse_mypy_text(text: str, cwd: Path) -> tuple[list[Diag], int]:
    diags: list[Diag] = []
    unparsed = 0
    for ln in text.splitlines():
        ln = ln.rstrip()
        if not ln:
            continue
        m = _MYPY_LINE.match(ln)
        if not m:
            if not (re.match(r"^(Success: no issues found|Found \d+ errors? in)", ln) or _MYPY_CONTEXT.match(ln)):
                unparsed += 1
            continue
        p = Path(m["file"])
        p = p if p.is_absolute() else cwd / p
        if m["sev"] == "note":
            _attach_note(diags, p, int(m["line"]), m["msg"])
            continue
        diags.append(Diag(p, int(m["line"]), int(m["col"] or 1), m["sev"], m["code"] or "", m["msg"]))
    return diags, unparsed


def _sev(s: str) -> str:
    s = s.lower()
    return "error" if s == "error" else "note" if s in ("note", "information", "message") else "warning"


def read_jsonc(p: Path) -> dict:
    """A JSON-with-comments file (``tsconfig.json``): comments outside strings and trailing commas removed.
    {} when it cannot be read."""
    try:
        text = p.read_text("utf-8-sig", "replace")
    except OSError:
        return {}
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    clean = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    try:
        data = json.loads(clean)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# -- classifying -----------------------------------------------------------------------------------------

def classify(tool: str, code: str, message: str) -> str:
    """name, member, call, import, type or syntax."""
    if tool == "tsc":
        n = int(code[2:]) if code[2:].isdigit() else 0
        if 1000 <= n < 2000:
            return "syntax"
        if n in _TS_TYPES_MISSING:
            return "name"
        for kind, codes in (("name", _TS_NAME), ("import", _TS_IMPORT), ("member", _TS_MEMBER), ("call", _TS_CALL)):
            if n in codes:
                return kind
        return "type"
    if tool == "mypy" and code in _MYPY_KIND:
        if code == "attr-defined" and message.startswith('Module "'):   # from m import x: x is not in m
            return "import"
        return _MYPY_KIND[code]
    if tool == "pyright" and code in _PYRIGHT_KIND:
        if code == "reportAttributeAccessIssue" and re.match(r"Cannot (assign to|delete) attribute", message):
            return "type"   # the attribute is there; it is read-only or cannot be deleted
        return _PYRIGHT_KIND[code]
    m = message
    if re.search(r"is not defined|Cannot find name|Name .* is not defined", m):
        return "name"
    if re.search(r"Import .* could not be resolved|Cannot find implementation or library stub|No module named|"
                 r"^Module .* has no attribute", m):
        return "import"
    if re.search(r"Cannot (assign to|delete) attribute|is possibly unbound|Item .* of .* has no attribute", m):
        return "type"
    if re.search(r"has no attribute|Cannot access (member|attribute)|is not a known (attribute|member)", m):
        return "member"
    if re.search(r"Too (many|few) arguments|Unexpected keyword argument|Missing positional argument|"
                 r"No parameter named|Argument missing|Expected \d+ positional argument|^Argument \d+ to|"
                 r"cannot be assigned to parameter|No overloads for", m):
        return "call"
    return "type"


def _ts_alias(spec: str, cfg: Path | None) -> str:
    """How a non-relative TypeScript module name relates to the tsconfig: 'alias-found' (a ``paths`` pattern
    names a file that is there), 'alias' (a pattern matches, no file), 'baseurl' (``baseUrl`` is set, the name
    may be the project's), or ''."""
    if cfg is None:
        return ""
    opts = read_jsonc(cfg).get("compilerOptions") or {}
    if not isinstance(opts, dict):
        return ""
    base = cfg.parent / str(opts.get("baseUrl") or ".")
    for pat, targets in (opts.get("paths") or {}).items() if isinstance(opts.get("paths"), dict) else []:
        pre, star, post = str(pat).partition("*")
        if not (spec.startswith(pre) and spec.endswith(post) if star else spec == pre):
            continue
        mid = spec[len(pre):len(spec) - len(post)] if star else ""
        for t in targets if isinstance(targets, list) else []:
            p = base / str(t).replace("*", mid)
            if any(Path(str(p) + ext).is_file() for ext in ("", ".ts", ".tsx", ".d.ts", ".js", "/index.ts",
                                                             "/index.tsx", "/index.js")):
                return "alias-found"
        return "alias"
    return "baseurl" if opts.get("baseUrl") else ""


def _py_project_module(spec: str, repo: Path) -> str:
    """'found' (the module is a file or package of the project), 'top' (its top package is the project's, the
    module is not there), or ''."""
    parts = spec.split(".")
    for root in (repo, repo / "src"):
        if (root / parts[0]).is_dir() or (root / f"{parts[0]}.py").is_file():
            mod = root.joinpath(*parts)
            if mod.is_dir() or mod.with_suffix(".py").is_file() or mod.with_suffix(".pyi").is_file():
                return "found"
            return "top"
    return ""


def verdict_of(tool: str, kind: str, code: str, message: str, *, repo: Path | None = None,
               cfg: Path | None = None) -> tuple[str, str]:
    """(verdict, why) of an error of ``kind``."""
    first = message.split("\n", 1)[0]
    if tool == "tsc" and code[2:].isdigit() and int(code[2:]) in _TS_TYPES_MISSING:
        types = re.search(r"@types/[\w.-]+", message)
        return "not_installed", (f"type definitions are not installed ({types.group(0)})" if types else
                                 "type definitions for it are not installed")
    if kind == "import":
        if code in ("import-untyped", "reportMissingModuleSource", "TS7016"):
            return "unknown", "the module is there but the checker has no types for it (its stubs are missing)"
        q = _QUOTED.findall(first)
        spec = q[0] if q else ""
        if code in ("TS2307", "TS2792") and spec and not spec.startswith((".", "/")):
            how = _ts_alias(spec, cfg)
            if how == "alias-found":
                return "unknown", "a tsconfig paths alias names a file that is there; the compiler did not resolve it"
            if how == "alias":
                return "absent", "a tsconfig paths alias: no file it maps to is there"
            if how == "baseurl":
                return "unknown", "may be a project module under the tsconfig baseUrl, or a package not installed"
            return "not_installed", "no such package where the compiler looks (node_modules, @types)"
        if code in ("import-not-found", "reportMissingImports") and spec and not spec.startswith("."):
            how = _py_project_module(spec, repo) if repo is not None else ""
            if how == "found":
                return "unknown", "the module is in the project; the checker's search path does not reach it"
            if how == "top":
                return "absent", "the project's own package has no such module"
            return "not_installed", "no such package in the environment the checker read"
        return "absent", ""
    if kind in ("call", "type"):
        return "mismatch", ""
    return "absent", ""


def _name_of(kind: str, message: str) -> str:
    """The name a diagnostic is about, when its message quotes it (a type's name is not one)."""
    first = message.split("\n", 1)[0]
    if kind == "call":   # mypy: Argument 1 to "f"; Too many arguments for "f"; pyright: in function "f"
        m = re.search(r'(?:\bto|\bfor|in function) "([^"]+)"', first)
        return m.group(1) if m else ""
    if kind in ("type", "syntax"):
        return ""
    m = re.search(r"""(?:has no attribute|has no exported member(?: named)?) ['"]([^'"]+)['"]""", first)
    if m:
        return m.group(1)
    q = _QUOTED.findall(first)
    return q[0] if q else ""


# -- the check -------------------------------------------------------------------------------------------

def _key(p: Path) -> str:
    try:
        p = p.resolve()
    except OSError:
        pass
    return os.path.normcase(str(p))


def _inside(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _rel(p: Path, root: Path) -> str:
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(p)


def nearest_tsconfig(p: Path, repo: Path) -> Path | None:
    d = p.parent
    while True:
        cfg = d / "tsconfig.json"
        if cfg.is_file():
            return cfg
        if d == repo or d.parent == d or not _inside(d, repo):
            return None
        d = d.parent


def python_config(repo: Path) -> tuple[str | None, str]:
    """(the checker the project configures or None, the file that says so)."""
    py = repo / "pyproject.toml"
    text = ""
    try:
        text = py.read_text("utf-8", "replace") if py.is_file() else ""
    except OSError:
        pass
    if (repo / "pyrightconfig.json").is_file():
        return "pyright", "pyrightconfig.json"
    if re.search(r"^\[tool\.pyright\]", text, re.M):
        return "pyright", "pyproject.toml [tool.pyright]"
    for n in ("mypy.ini", ".mypy.ini"):
        if (repo / n).is_file():
            return "mypy", n
    if re.search(r"^\[tool\.mypy\]", text, re.M):
        return "mypy", "pyproject.toml [tool.mypy]"
    try:
        cfg = (repo / "setup.cfg").read_text("utf-8", "replace") if (repo / "setup.cfg").is_file() else ""
    except OSError:
        cfg = ""
    if re.search(r"^\[mypy\]", cfg, re.M):
        return "mypy", "setup.cfg [mypy]"
    return None, ""


def _config_for(tool: str, repo: Path) -> str:
    chosen, where = python_config(repo)
    if chosen == tool:
        return where
    if chosen is not None:   # the project configures the other checker; this one may still read a shared file
        return f"{tool}'s own discovery from the repository root (the project configures {chosen}: {where})"
    return f"{tool}'s defaults (no configuration of it found at the repository root)"


def _type_checked(abs_path: Path, cfg: Path) -> bool:
    """Whether tsc type-checks a file of its program: not with ``// @ts-nocheck`` at its top; a JavaScript file
    only with ``// @ts-check`` or ``checkJs`` true in its own tsconfig (comments read as comments; an ``extends``
    chain is not followed, so a ``checkJs`` it would bring leaves the file imports only)."""
    try:
        head = abs_path.read_text("utf-8", "replace")[:4000]
    except OSError:
        return False
    if re.search(r"^\s*//\s*@ts-nocheck\b", head, re.M):
        return False
    if abs_path.suffix.lower() not in JS_SUFFIXES:
        return True
    if re.search(r"^\s*//\s*@ts-check\b", head, re.M):
        return True
    opts = read_jsonc(cfg).get("compilerOptions") or {}
    return isinstance(opts, dict) and opts.get("checkJs") is True


def run_checkers(repo: Path, choice: str, py_targets: list[tuple[str, Path, set[int] | None]],
                 ts_targets: list[tuple[str, Path, set[int] | None]], *, env: str | None = "auto",
                 timeout: float | None = None, trusted: bool | None = None) -> dict:
    """Run the chosen checker(s) on the targets and return ``{"runs": [...], "sites": [...], "covered": set of
    TypeScript/JavaScript paths the compiler type-checked, "notes": [...], "incomplete": [...]}``. ``incomplete``
    says what was asked of a checker and not done (not found, not run, timed out, failed, a blocking or syntax
    error, output not understood or cut). ``trusted``: whether the project is trusted (default: asked)."""
    if choice not in CHOICES:
        raise ValueError(f"--checker {choice!r}: one of {', '.join(CHOICES)}")
    repo = Path(repo).resolve()
    timeout = TIMEOUT_S if timeout is None else float(timeout)
    if not (math.isfinite(timeout) and timeout > 0):
        raise ValueError("the checker timeout is a positive, finite number of seconds")
    if trusted is None:
        from verinoda import paths as vpaths

        trusted = vpaths.is_trusted(repo)
    runs: list[Run] = []
    sites: list[dict] = []
    covered: set[str] = set()
    notes: list[str] = []
    incomplete: list[str] = []
    want_ts = choice in ("tsc", "auto")
    py_tool = choice if choice in ("pyright", "mypy") else None
    if choice == "auto" and py_targets:
        configured, _where = python_config(repo)
        order = [configured] if configured else []
        order += [t for t in ("pyright", "mypy") if t not in order]
        usable = [t for t in order if not gate(t, repo, trusted)]
        py_tool = next((t for t in usable if find(t, repo, repo, env, trusted)[0] is not None), None)
        if py_tool is None:
            gated = [gate(t, repo, trusted) for t in order if gate(t, repo, trusted)]
            r = Run(configured or "pyright or mypy", status="not_found" if not gated else "not_run",
                    note="; ".join(gated + ["no usable pyright or mypy was found (the project's node_modules/.bin "
                                            "and virtual environment, then PATH)"]),
                    next_step=trust_step(repo) if gated else install_hint(order[0]))
            runs.append(r)
            incomplete.append(f"{len(py_targets)} Python file{'s' if len(py_targets) > 1 else ''} not checked by a "
                              "type checker: " + r.note)
    if choice == "tsc" and not ts_targets:
        notes.append("--checker tsc: no TypeScript or JavaScript file in scope")
    if py_tool and not py_targets:
        notes.append(f"--checker {py_tool}: no Python file in scope")
    if choice in ("pyright", "mypy") and ts_targets:
        notes.append(f"--checker {choice} reads Python: TypeScript/JavaScript files are checked for imports only "
                     "(--checker tsc or auto runs the compiler)")
    if want_ts and ts_targets:
        groups: dict[str, list[tuple[str, Path, set[int] | None]]] = {}
        no_cfg: list[str] = []
        for t in ts_targets:
            cfg = nearest_tsconfig(t[1], repo)
            if cfg is None:
                no_cfg.append(t[0])
            else:
                groups.setdefault(str(cfg), []).append(t)
        if no_cfg:
            incomplete.append(f"{len(no_cfg)} TypeScript/JavaScript file{'s' if len(no_cfg) > 1 else ''} with no "
                              f"tsconfig.json above {'them' if len(no_cfg) > 1 else 'it'} in the repository, not "
                              f"compiled (tsc without the project's options would judge other code): "
                              + ", ".join(no_cfg[:3]) + (" ..." if len(no_cfg) > 3 else ""))
        for n, (cfg_s, ts) in enumerate(sorted(groups.items())):
            cfg = Path(cfg_s)
            if n >= MAX_PROJECTS:
                rest = sum(len(v) for _k, v in sorted(groups.items())[MAX_PROJECTS:])
                incomplete.append(f"tsc runs at most {MAX_PROJECTS} tsconfig.json projects per check: {rest} "
                                  f"file{'s' if rest > 1 else ''} of {len(groups) - MAX_PROJECTS} more not compiled "
                                  "(check fewer paths)")
                break
            r = _run_tsc(repo, cfg, ts, timeout, trusted)
            runs.append(r)
            got, cov, broken = _sites(r, repo, ts, cfg)
            sites += got
            for rel, p, _l in ts:
                if rel in cov and rel not in broken and _type_checked(p, cfg):
                    covered.add(rel)
            if r.status != "ran":
                incomplete.append(f"tsc ({_rel(cfg, repo)}): {r.note}")
            else:
                left = [rel for rel, _p, _l in ts if rel not in covered and rel not in broken]
                if left:
                    notes.append(f"{len(left)} file{'s' if len(left) > 1 else ''} not type-checked by tsc "
                                 f"({_rel(cfg, repo)}): not in its program, // @ts-nocheck, or JavaScript without "
                                 "checkJs or // @ts-check: " + ", ".join(left[:3]) + (" ..." if len(left) > 3 else ""))
            incomplete += [f"tsc ({_rel(cfg, repo)}): {b}" for b in broken.values()]
    if py_tool and py_targets:
        r = _run_python(py_tool, repo, py_targets, env, timeout, trusted)
        runs.append(r)
        got, _cov, broken = _sites(r, repo, py_targets, None)
        sites += got
        if r.status != "ran":
            incomplete.append(f"{len(py_targets)} Python file{'s' if len(py_targets) > 1 else ''} not checked by "
                              f"{py_tool}: {r.note}")
        incomplete += [f"{py_tool}: {b}" for b in broken.values()]
    for r in runs:
        incomplete += [f"{r.tool}: {p}" for p in r.problems]
        if r.status == "ran" and r.unparsed:   # a diagnostic in a form not read may be among them
            incomplete.append(f"{r.tool}: {r.unparsed} output line{'s' if r.unparsed > 1 else ''} not understood "
                              "(not read as diagnostics; run it yourself to see them)")
        if r.truncated:
            incomplete.append(f"{r.tool}: its output passed {MAX_OUTPUT // (1024 * 1024)} MB and the run was "
                              "stopped; later diagnostics are not read")
    return {"runs": [r.header() for r in runs], "sites": sites, "covered": covered, "notes": notes,
            "incomplete": incomplete}


def _start(r: Run, exe: Path, args: list[str], cwd: Path, timeout: float, ver: str | None = None
           ) -> tuple[str, str] | None:
    """Start the checker (its version first, unless given); (stdout, stderr), or None with ``r`` saying why
    not."""
    if not _cmd_safe(exe, args):
        _cmd_refusal(r)
        return None
    r.version = version(exe, cwd) if ver is None else ver
    t0 = time.perf_counter()
    try:
        rc, out, err, cut = run_capped([str(exe), *args], cwd, timeout)
    except OSError as exc:
        r.status, r.note = "failed", f"could not be started: {exc}"
        return None
    r.seconds, r.exit, r.truncated = time.perf_counter() - t0, rc, cut
    if rc is None:
        r.status, r.note = "timeout", f"did not finish within {timeout:g} s (stopped; --checker-timeout raises it)"
        r.next_step = "run with a larger --checker-timeout, or check fewer files"
        return None
    return out, err


def _run_tsc(repo: Path, cfg: Path, ts: list, timeout: float, trusted: bool) -> Run:
    cwd = cfg.parent
    r = Run("tsc", config=_rel(cfg, repo), cwd=_rel(cwd, repo) or ".", files=[t[0] for t in ts])
    exe, why = find("tsc", repo, cwd, None, trusted)
    if exe is None:
        r.status, r.note = ("not_run" if "not trusted" in why else "not_found"), why
        r.next_step = trust_step(repo) if "not trusted" in why else install_hint("tsc")
        return r
    r.exe = _rel(exe, repo)
    got = _start(r, exe, ["-p", cfg.name, "--noEmit", "--pretty", "false", "--listFiles"], cwd, timeout)
    if got is None:
        return r
    out, err = got
    rc = r.exit
    diags, glob, program, unparsed = parse_tsc(out, cwd)
    r.unparsed = unparsed   # stderr (Node's own warnings) is shown only when the run fails
    fatal = [g for g in glob if g.severity == "error"]
    if (rc not in (0, 1, 2)) or (rc != 0 and not diags and not fatal) or (fatal and not program):
        r.status = "failed"
        text = "; ".join(f"{g.code} {g.message}" for g in fatal[:2]) or (err or out).strip()[:300] or "no output"
        r.note = f"exit {rc}, its output not understood as tsc's report: {text}"
        r.next_step = f"run {r.exe} -p {r.config} --noEmit yourself and read its message"
        return r
    r.diags, r.program = diags, program
    r.warnings = sum(1 for d in diags if d.severity == "warning")
    if fatal:   # the program was built, but not with the configuration as written (a missing extends base...)
        r.note = "global errors: " + "; ".join(f"{g.code} {g.message}" for g in fatal[:3])
        r.problems.append(f"global errors, the configuration was not read as written: {r.note[15:]}")
    return r


def _run_python(tool: str, repo: Path, targets: list, env: str | None, timeout: float, trusted: bool) -> Run:
    r = Run(tool, cwd=".", files=[t[0] for t in targets], config=_config_for(tool, repo))
    why = gate(tool, repo, trusted)
    if why:
        r.status, r.note, r.next_step = "not_run", why, trust_step(repo)
        return r
    exe, why = find(tool, repo, repo, env, trusted)
    if exe is None:
        r.status, r.note = ("not_run" if "not trusted" in why else "not_found"), why
        r.next_step = trust_step(repo) if "not trusted" in why else install_hint(tool)
        return r
    r.exe = _rel(exe, repo)
    files = [t[0] for t in targets]
    if sum(len(f) + 3 for f in files) > MAX_ARGV:   # too long a command line: the top folders instead
        files = sorted({f.split("/", 1)[0] for f in files})
    files = ["./" + f for f in files]   # a file named like an option (--python-executable=...) stays a file
    if tool == "pyright":
        args = ["--outputjson", *files]
    else:
        args = ["--show-column-numbers", "--show-error-codes", "--no-error-summary", "--no-pretty",
                "--no-color-output"]
    if not _cmd_safe(exe, args + files):
        _cmd_refusal(r)
        return r
    ver = None
    if tool == "mypy":
        ver = version(exe, repo)
        args = (["-O", "json"] if _ver(ver) >= (1, 11) else []) + args + ["--", *files]
    got = _start(r, exe, args, repo, timeout, ver)
    if got is None:
        return r
    out, err = got
    rc = r.exit
    try:
        if tool == "pyright":
            if rc not in (0, 1):
                raise ValueError(f"exit {rc}")
            diags, ver = parse_pyright(out, repo)
            r.version = r.version or ver
            unparsed = 0
        elif "-O" in args:
            diags, unparsed = parse_mypy_json(out, repo)
        else:
            diags, unparsed = parse_mypy_text(out, repo)
        if tool == "mypy" and rc == 2 and diags:   # a blocking error: mypy stopped before type-checking
            r.status = "failed"
            r.note = "mypy stopped on a blocking error, so the files were not type-checked: " + "; ".join(
                f"{_rel(d.path, repo)}:{d.line}: {d.message.splitlines()[0]}" for d in diags[:2])
            r.next_step = "fix that error (a syntax error, a file that cannot be read, a duplicate module), then run " \
                          "check again"
            return r
        if tool == "mypy" and (rc not in (0, 1) or (rc == 1 and not diags)):
            raise ValueError(f"exit {rc}")
    except ValueError as exc:
        r.status = "failed"
        r.note = f"its output was not understood ({exc}): " + ((err or out).strip()[:300] or "no output")
        r.next_step = f"run {r.exe} on the files yourself and read its message"
        return r
    r.diags, r.unparsed = diags, unparsed
    r.warnings = sum(1 for d in diags if d.severity == "warning")
    return r


def _ver(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def _sites(r: Run, repo: Path, targets: list[tuple[str, Path, set[int] | None]], cfg: Path | None
           ) -> tuple[list[dict], set[str], dict[str, str]]:
    """The run's errors on the targets' lines as sites, the targets the run covered, and the targets with a
    syntax error (not type-checked) with the error."""
    if r.status != "ran":
        return [], set(), {}
    by_key = {_key(p): (rel, lines) for rel, p, lines in targets}
    if r.program is not None:
        cov = {rel for k, (rel, _l) in by_key.items() if k in r.program}
    else:
        cov = {rel for rel, _p, _l in targets}
    out: list[dict] = []
    broken: dict[str, str] = {}
    over = 0
    seen = set()
    for d in r.diags:
        if d.severity != "error":
            continue
        hit = by_key.get(_key(d.path))
        if hit is None:
            continue
        rel, lines = hit
        kind = classify(r.tool, d.code, d.message)
        if kind == "syntax":   # anywhere in the file: the file was not type-checked as written
            broken.setdefault(rel, f"{rel}:{d.line}: {d.code} {d.message.splitlines()[0]} (a syntax error: the "
                                   "file was not type-checked)")
            continue
        if lines is not None and d.line not in lines:
            continue
        if (rel, d.line, d.col, d.code, d.message) in seen:
            continue
        seen.add((rel, d.line, d.col, d.code, d.message))
        if len(out) >= MAX_SITES:
            over += 1
            continue
        verdict, why = verdict_of(r.tool, kind, d.code, d.message, repo=repo, cfg=cfg)
        name = _name_of(kind, d.message)
        first = d.message.split("\n", 1)[0]
        cite = f"{r.tool}{' ' + r.version if r.version else ''}" + (f", {r.config}" if r.config else "")
        s = {"at": f"{rel}:{d.line}:{d.col}", "path": rel, "line": d.line, "col": d.col, "kind": kind,
             "expr": (name or first)[:120], "name": name, "verdict": verdict,
             "language": _language(rel), "status": "observed", "source": "checker",
             "checker": {"tool": r.tool, "version": r.version or "unknown", "config": r.config,
                         "code": d.code or "(none)"},
             "message": f"{d.code + ': ' if d.code else ''}{d.message[:600]} ({cite})"}
        if why:
            s["why"] = why
        out.append(s)
    if over:
        r.note = (r.note + "; " if r.note else "") + f"{over} more errors in scope not listed (at most {MAX_SITES})"
    return out, cov, broken


def _language(rel: str) -> str:
    low = rel.lower()
    if low.endswith((".py", ".pyi")):
        return "Python"
    return "JavaScript" if low.endswith(JS_SUFFIXES) else "TypeScript"


_IMPORT_KINDS = {"import"}
_CALL_KINDS = {"kwarg", "call", "constructor"}


def _family(kind: str) -> str:
    return "import" if kind in _IMPORT_KINDS else "call" if kind in _CALL_KINDS else "name"


def _same_name(own: dict, name: str, family: str) -> bool:
    on = str(own.get("name") or "")
    if not name or not on:
        return False
    if family == "import":   # a module by its spec ('./nope', 'pkg.mod'), or a name it should export
        return on == name or on.endswith("." + name) or str(own.get("expr") or "") == name
    return on.rsplit(".", 1)[-1] == name


def merge(sites: list[dict], checker_sites: list[dict]) -> list[dict]:
    """``sites`` plus the checker's, one site per finding. A checker error on the line of Verinoda's own site of
    the same kind family (import, name or member) and name joins it: the same verdict confirms it
    (``confirmed_by``); Verinoda's ``unknown`` gives way to the checker's ``absent`` or ``not_installed``
    (``own_verdict`` keeps what it was); otherwise Verinoda's site stays and carries ``checker_says``. A site
    Verinoda found to exist is not joined: the disagreement is listed. Calls and types are never joined."""
    out = list(sites)
    index: dict[tuple, list[int]] = {}
    for i, s in enumerate(sites):
        if s.get("verdict") in ("absent", "not_installed", "unknown", "guarded") and s.get("source") != "checker":
            index.setdefault((s.get("path"), s.get("line"), _family(str(s.get("kind")))), []).append(i)
    for c in checker_sites:
        fam = _family(c["kind"])
        own_i = None
        if c["kind"] in ("import", "name", "member"):
            own_i = next((i for i in index.get((c["path"], c["line"], fam), [])
                          if _same_name(out[i], c.get("name") or "", fam)), None)
        if own_i is None:
            out.append(c)
            continue
        own = out[own_i]
        ch = c["checker"]
        tag = f"{ch['tool']} {ch['version']} {ch['code']}"
        if own["verdict"] == c["verdict"]:
            own["confirmed_by"] = tag
        elif own["verdict"] == "unknown" and c["verdict"] in ("absent", "not_installed"):
            out[own_i] = {**c, "own_verdict": "unknown", "own_why": own.get("why") or own.get("rank_why") or ""}
        else:
            own["checker_says"] = f"{c['verdict']} ({tag}): {c['message'][:300]}"
    return out
