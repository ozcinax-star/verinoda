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

Nothing is installed or downloaded (no ``npx``, no ``pip``), and a checker that is not found, does not finish
within the timeout, or prints output that cannot be read is a note with the next step - never a pass. Each site
is the checker's verdict from that run (``status`` ``observed``), with the tool, its version, the configuration
and the diagnostic code: ``absent`` for a name, a member or an import the checker did not find (an import of a
package that is not relative: ``not_installed``), ``mismatch`` for a call or a type that does not fit. Only
errors become sites; warnings are counted. Programs of the project are started here (the checker, and through
it the project's plugins), so the MCP server never calls this.
"""

from __future__ import annotations

import json
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
KINDS = ("name", "member", "call", "type", "import")
TIMEOUT_S = 300.0            # one checker run; --checker-timeout changes it
VERSION_TIMEOUT_S = 30.0
MAX_OUTPUT = 16 * 1024 * 1024   # bytes read from a checker's output; the rest is cut (and said)
MAX_SITES = 1000             # sites listed per run; the rest are counted
MAX_PROJECTS = 4             # tsconfig.json files run in one check
MAX_ARGV = 24000             # characters of a command line (Windows allows about 32,000)
TS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
JS_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs")
_CMD_UNSAFE = set('"&|<>^%!\r\n')   # cmd.exe reads these in the arguments of a .cmd/.bat file

# tsc diagnostic codes by what they are about (the rest are types)
_TS_NAME = {2304, 2503, 2552, 2580, 2581, 2582, 2583, 2584, 2591, 2662, 2663}
_TS_IMPORT = {2305, 2306, 2307, 2459, 2460, 2614, 2724, 2792}
_TS_MEMBER = {2339, 2353, 2551, 2561, 2576, 2694}
_TS_CALL = {2345, 2348, 2349, 2350, 2351, 2554, 2555, 2556, 2557, 2575, 2721, 2722, 2723, 2769}
_MYPY_KIND = {"name-defined": "name", "used-before-def": "name", "attr-defined": "member", "union-attr": "member",
              "import": "import", "import-not-found": "import", "import-untyped": "import",
              "call-arg": "call", "arg-type": "call", "call-overload": "call"}
_PYRIGHT_KIND = {"reportUndefinedVariable": "name", "reportAttributeAccessIssue": "member",
                 "reportOptionalMemberAccess": "member", "reportFunctionMemberAccess": "member",
                 "reportCallIssue": "call", "reportArgumentType": "call", "reportOptionalCall": "call",
                 "reportMissingImports": "import", "reportMissingModuleSource": "import",
                 "reportPrivateImportUsage": "import"}
_TSC_LINE = re.compile(r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\): (?P<sev>error|warning|message) "
                       r"(?P<code>TS\d+): (?P<msg>.*)$")
_TSC_GLOBAL = re.compile(r"^(?P<sev>error|warning|message) (?P<code>TS\d+): (?P<msg>.*)$")
_MYPY_LINE = re.compile(r"^(?P<file>.+?):(?P<line>\d+):(?:(?P<col>\d+):)?(?:\d+:\d+:)? (?P<sev>error|warning|note): "
                        r"(?P<msg>.*?)(?:  \[(?P<code>[a-z0-9-]+)\])?$")
_QUOTED = re.compile(r"""['"]([^'"\s]{1,120})['"]""")


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
        order = [e for e in (".cmd", ".exe", ".bat") if e in exts] + [e for e in exts if e not in (".cmd", ".exe",
                                                                                                   ".bat")]
        return [name + e for e in order]
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
    package on first use and looks for a newer version on each start."""
    try:
        data = exe.read_bytes()[:8 * 1024 * 1024]
    except OSError:
        return False
    if b"pyright.cli" in data or b"pyright_python" in data:
        return True
    k = data.rfind(b"PK\x05\x06")   # a launcher with the script appended as a zip (pip, uv on Windows)
    if k >= 0:
        try:
            import io

            with zipfile.ZipFile(io.BytesIO(data)) as z:
                return any(b"pyright.cli" in z.read(n) for n in z.namelist() if n.endswith(".py"))
        except (zipfile.BadZipFile, OSError, ValueError, KeyError):
            return False
    return False


def find(tool: str, repo: Path, start: Path, env: str | None) -> tuple[Path | None, str]:
    """(executable, why not) of ``tool`` for the files under ``start``: the project's own first."""
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
        if tool == "pyright" and "node_modules" not in p.parts and _pip_pyright(p):
            refused = f"{p} is pyright's PyPI wrapper, which downloads Node.js and pyright on use: not started"
            continue
        return p, ""
    p = on_path(tool)
    if p is not None:
        if tool == "pyright" and _pip_pyright(p):
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
    process the checker leaves behind cannot hold a pipe open; on timeout the whole process tree is stopped."""
    env = {**os.environ, "NO_COLOR": "1", "FORCE_COLOR": "0", "npm_config_update_notifier": "false"}
    kw: dict = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else \
        {"start_new_session": True}
    with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
        proc = subprocess.Popen(argv, cwd=str(cwd), stdout=fo, stderr=fe, stdin=subprocess.DEVNULL, env=env, **kw)
        try:
            rc: int | None = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            rc = None
        cut = False
        texts = []
        for f in (fo, fe):
            f.seek(0)
            data = f.read(MAX_OUTPUT + 1)
            if len(data) > MAX_OUTPUT:
                data, cut = data[:MAX_OUTPUT], True
            texts.append(data.decode("utf-8", "replace"))
    return rc, texts[0], texts[1], cut


def _cmd_safe(exe: Path, args: list[str]) -> bool:
    return exe.suffix.lower() not in (".cmd", ".bat") or not any(set(a) & _CMD_UNSAFE for a in args)


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
        if sev == "note":   # a note explains the error before it
            if diags and _key(diags[-1].path) == _key(p):
                diags[-1].message += "\n" + msg
            continue
        diags.append(Diag(p, int(d["line"]), int(col) + 1 if isinstance(col, int) and col >= 0 else 1, sev,
                          str(d.get("code") or ""), msg))
    return diags, unparsed


def parse_mypy_text(text: str, cwd: Path) -> tuple[list[Diag], int]:
    diags: list[Diag] = []
    unparsed = 0
    last: Diag | None = None
    for ln in text.splitlines():
        ln = ln.rstrip()
        if not ln:
            continue
        m = _MYPY_LINE.match(ln)
        if not m:
            if not re.match(r"^(Success: no issues found|Found \d+ errors? in)", ln):
                unparsed += 1
            continue
        p = Path(m["file"])
        p = p if p.is_absolute() else cwd / p
        if m["sev"] == "note":   # a note explains the error before it
            if last is not None and _key(last.path) == _key(p):
                last.message += "\n" + m["msg"]
            continue
        last = Diag(p, int(m["line"]), int(m["col"] or 1), m["sev"], m["code"] or "", m["msg"])
        diags.append(last)
    return diags, unparsed


def _sev(s: str) -> str:
    s = s.lower()
    return "error" if s == "error" else "note" if s in ("note", "information", "message") else "warning"


# -- classifying -----------------------------------------------------------------------------------------

def classify(tool: str, code: str, message: str) -> str:
    """name, member, call, import or type."""
    if tool == "tsc":
        n = int(code[2:]) if code[2:].isdigit() else 0
        for kind, codes in (("name", _TS_NAME), ("import", _TS_IMPORT), ("member", _TS_MEMBER), ("call", _TS_CALL)):
            if n in codes:
                return kind
        return "type"
    if tool == "mypy" and code in _MYPY_KIND:
        if code == "attr-defined" and message.startswith('Module "'):   # from m import x: x is not in m
            return "import"
        return _MYPY_KIND[code]
    if tool == "pyright" and code in _PYRIGHT_KIND:
        return _PYRIGHT_KIND[code]
    m = message
    if re.search(r"is not defined|Cannot find name|Name .* is not defined", m):
        return "name"
    if re.search(r"Import .* could not be resolved|Cannot find implementation or library stub|No module named|"
                 r"^Module .* has no attribute", m):
        return "import"
    if re.search(r"has no attribute|Cannot access (member|attribute)|is not a known (attribute|member)", m):
        return "member"
    if re.search(r"Too (many|few) arguments|Unexpected keyword argument|Missing positional argument|"
                 r"No parameter named|Argument missing|Expected \d+ positional argument|^Argument \d+ to|"
                 r"cannot be assigned to parameter|No overloads for", m):
        return "call"
    return "type"


def _verdict(tool: str, kind: str, code: str, message: str) -> tuple[str, str]:
    """(verdict, why) of an error of ``kind``."""
    if kind == "import":
        if code == "import-untyped" or code == "reportMissingModuleSource":
            return "unknown", "the module is there but the checker has no types for it (its stubs are missing)"
        q = _QUOTED.findall(message)
        spec = q[0] if q else ""
        if tool == "tsc" and code in ("TS2307", "TS2792") and spec and not spec.startswith((".", "/")) or \
                code in ("import-not-found", "reportMissingImports") and spec and not spec.startswith("."):
            return "not_installed", "the checker found no such package in the project's environment"
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
    if kind == "type":
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


def _js_checked(rel: str, abs_path: Path, cfg: Path) -> bool:
    """Whether tsc type-checks a JavaScript file: ``// @ts-check`` at its top or ``checkJs`` in its tsconfig
    (an ``extends`` chain is not followed: such a file stays imports only)."""
    try:
        head = abs_path.read_text("utf-8", "replace")[:2000]
    except OSError:
        return False
    if re.search(r"^\s*//\s*@ts-check\b", head, re.M):
        return True
    if re.search(r"^\s*//\s*@ts-nocheck\b", head, re.M):
        return False
    try:
        return bool(re.search(r'"checkJs"\s*:\s*true', cfg.read_text("utf-8", "replace")))
    except OSError:
        return False


def run_checkers(repo: Path, choice: str, py_targets: list[tuple[str, Path, set[int] | None]],
                 ts_targets: list[tuple[str, Path, set[int] | None]], *, env: str | None = "auto",
                 timeout: float | None = None) -> dict:
    """Run the chosen checker(s) on the targets and return ``{"runs": [...], "sites": [...], "covered": set of
    TypeScript/JavaScript paths the compiler type-checked, "notes": [...], "incomplete": [...]}``. ``incomplete``
    says what was asked of a checker and not done (a checker not found, timed out or failed)."""
    if choice not in CHOICES:
        raise ValueError(f"--checker {choice!r}: one of {', '.join(CHOICES)}")
    repo = Path(repo).resolve()
    timeout = TIMEOUT_S if timeout is None else float(timeout)
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
        py_tool = next((t for t in order if find(t, repo, repo, env)[0] is not None), None)
        if py_tool is None:
            r = Run(configured or "pyright or mypy", status="not_found",
                    note="neither pyright nor mypy was found (the project's node_modules/.bin and virtual "
                         "environment, then PATH)", next_step=install_hint(order[0]))
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
            r = _run_tsc(repo, cfg, ts, timeout)
            runs.append(r)
            got, cov = _sites(r, repo, ts)
            sites += got
            for rel, p, _l in ts:
                if rel in cov and (p.suffix.lower() not in JS_SUFFIXES or _js_checked(rel, p, cfg)):
                    covered.add(rel)
            if r.status != "ran":
                incomplete.append(f"tsc ({_rel(cfg, repo)}): {r.note}")
            else:
                left = [rel for rel, _p, _l in ts if rel not in covered]
                if left:
                    notes.append(f"{len(left)} file{'s' if len(left) > 1 else ''} not type-checked by tsc "
                                 f"({_rel(cfg, repo)}): not in its program, or JavaScript without checkJs or "
                                 "// @ts-check: " + ", ".join(left[:3]) + (" ..." if len(left) > 3 else ""))
    if py_tool and py_targets:
        r = _run_python(py_tool, repo, py_targets, env, timeout)
        runs.append(r)
        got, _cov = _sites(r, repo, py_targets)
        sites += got
        if r.status != "ran":
            incomplete.append(f"{len(py_targets)} Python file{'s' if len(py_targets) > 1 else ''} not checked by "
                              f"{py_tool}: {r.note}")
    for r in runs:
        if r.status == "ran" and r.unparsed:   # a diagnostic in a form not read may be among them
            incomplete.append(f"{r.tool}: {r.unparsed} output line{'s' if r.unparsed > 1 else ''} not understood "
                              "(not read as diagnostics; run it yourself to see them)")
        if r.truncated:
            incomplete.append(f"{r.tool}: output cut at {MAX_OUTPUT // (1024 * 1024)} MB; later diagnostics are "
                              "not read")
    return {"runs": [r.header() for r in runs], "sites": sites, "covered": covered, "notes": notes,
            "incomplete": incomplete}


def _run_tsc(repo: Path, cfg: Path, ts: list, timeout: float) -> Run:
    cwd = cfg.parent
    r = Run("tsc", config=_rel(cfg, repo), cwd=_rel(cwd, repo) or ".", files=[t[0] for t in ts])
    exe, why = find("tsc", repo, cwd, None)
    if exe is None:
        r.status, r.note, r.next_step = "not_found", why, install_hint("tsc")
        return r
    r.exe = _rel(exe, repo)
    args = ["-p", cfg.name, "--noEmit", "--pretty", "false", "--listFiles"]
    r.version = version(exe, cwd)
    t0 = time.perf_counter()
    try:
        rc, out, err, cut = run_capped([str(exe), *args], cwd, timeout)
    except OSError as exc:
        r.status, r.note = "failed", f"could not be started: {exc}"
        return r
    r.seconds, r.exit, r.truncated = time.perf_counter() - t0, rc, cut
    if rc is None:
        r.status, r.note = "timeout", f"did not finish within {timeout:g} s (stopped; --checker-timeout raises it)"
        r.next_step = "run with a larger --checker-timeout, or check fewer files"
        return r
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
    if fatal:
        r.note = "global errors: " + "; ".join(f"{g.code} {g.message}" for g in fatal[:3])
    return r


def _run_python(tool: str, repo: Path, targets: list, env: str | None, timeout: float) -> Run:
    r = Run(tool, cwd=".", files=[t[0] for t in targets], config=_config_for(tool, repo))
    exe, why = find(tool, repo, repo, env)
    if exe is None:
        r.status, r.note, r.next_step = "not_found", why, install_hint(tool)
        return r
    r.exe = _rel(exe, repo)
    files = [t[0] for t in targets]
    if sum(len(f) + 1 for f in files) > MAX_ARGV:   # too long a command line: the top folders instead
        files = sorted({f.split("/", 1)[0] for f in files})
    r.version = version(exe, repo)
    if tool == "pyright":
        args = ["--outputjson", *files]
    else:
        args = ["--show-column-numbers", "--show-error-codes", "--no-error-summary", "--no-pretty",
                "--no-color-output", *files]
        if _ver(r.version) >= (1, 11):
            args = ["-O", "json", *args]
    if not _cmd_safe(exe, args):
        r.status = "not_run"
        r.note = f"a path holds a character cmd.exe would interpret (one of \" & | < > ^ % !), and {r.exe} is a " \
                 "batch file: not started"
        r.next_step = f"run {tool} yourself on those files"
        return r
    t0 = time.perf_counter()
    try:
        rc, out, err, cut = run_capped([str(exe), *args], repo, timeout)
    except OSError as exc:
        r.status, r.note = "failed", f"could not be started: {exc}"
        return r
    r.seconds, r.exit, r.truncated = time.perf_counter() - t0, rc, cut
    if rc is None:
        r.status, r.note = "timeout", f"did not finish within {timeout:g} s (stopped; --checker-timeout raises it)"
        r.next_step = "run with a larger --checker-timeout, or check fewer files"
        return r
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
        if tool == "mypy" and (rc not in (0, 1, 2) or (rc != 0 and not diags)):   # 2: a blocking error
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


def _sites(r: Run, repo: Path, targets: list[tuple[str, Path, set[int] | None]]) -> tuple[list[dict], set[str]]:
    """The run's errors on the targets' lines as sites, and the targets the run covered."""
    if r.status != "ran":
        return [], set()
    by_key = {_key(p): (rel, lines) for rel, p, lines in targets}
    if r.program is not None:
        cov = {rel for k, (rel, _l) in by_key.items() if k in r.program}
    else:
        cov = {rel for rel, _p, _l in targets}
    out: list[dict] = []
    over = 0
    seen = set()
    for d in r.diags:
        if d.severity != "error":
            continue
        hit = by_key.get(_key(d.path))
        if hit is None:
            continue
        rel, lines = hit
        if lines is not None and d.line not in lines:
            continue
        if (rel, d.line, d.col, d.code, d.message) in seen:
            continue
        seen.add((rel, d.line, d.col, d.code, d.message))
        if len(out) >= MAX_SITES:
            over += 1
            continue
        kind = classify(r.tool, d.code, d.message)
        verdict, why = _verdict(r.tool, kind, d.code, d.message)
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
    return out, cov


def _language(rel: str) -> str:
    low = rel.lower()
    if low.endswith((".py", ".pyi")):
        return "Python"
    return "JavaScript" if low.endswith(JS_SUFFIXES) else "TypeScript"


def merge(sites: list[dict], checker_sites: list[dict]) -> list[dict]:
    """``sites`` plus the checker's: a checker error on the line of an ``absent`` site of the same name and
    kind family confirms that site (``confirmed_by``) instead of being listed twice."""
    absent = {}
    for s in sites:
        if s.get("verdict") == "absent" and s.get("name"):
            absent.setdefault((s["path"], s["line"], str(s["name"]).rsplit(".", 1)[-1]), s)
    out = list(sites)
    for c in checker_sites:
        own = absent.get((c["path"], c["line"], c.get("name") or "")) if c["verdict"] == "absent" else None
        if own is not None:
            ch = c["checker"]
            own["confirmed_by"] = f"{ch['tool']} {ch['version']} {ch['code']}"
            continue
        out.append(c)
    return out
