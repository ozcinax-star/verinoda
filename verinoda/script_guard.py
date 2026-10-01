"""Script guards: a decision record's guard written as a small Python program (``script path=FILE.py``).

A rule the declarative guards cannot say (``only_in``, ``no_edge``, ``layers`` ...) can be a script in the
repository that defines ``check(guard)``. ``verinoda decide check`` runs it and reports what it found like
any other guard: each finding at a ``file:line`` that is re-read, a script that raises, times out, exits
or prints no result is ``unknown`` (never ok), and an ok says the script ran to its end.

``guard`` is a read-only view (:class:`GuardAPI`): ``repo``, ``spec``, ``files()``, ``nodes(file=None)``,
``node(id)``, ``edges(relations=None, from_file=None, to_file=None)`` (the graph as the index last wrote it,
``.verinoda/index/graph.json``, without the receiver-call edges), ``claims(status=None, limit=1000)`` (the
stored claims with their ``path:line`` evidence, read by Verinoda before the script starts), ``read(path)``,
and two ways to report: ``violation(path, line, why)`` and ``possible(path, line, why)``. The return value of
``check`` is not read.

What it is: a script guard runs the project's own code with the user's privileges, exactly like the
project's tests under ``experiment run`` in a trusted project. Trust is the boundary:

* it runs only in a project the user trusts (``verinoda trust``, recorded outside every repository), and only
  from the CLI (``decide check``, ``decide baseline``): MCP, ``update``'s summary line and ``what-if`` report a
  script guard as ``unknown``, and MCP neither records one nor accepts a proposed one;
* a child process of Verinoda's own interpreter, never the project's: ``-I`` (no ``PYTHON*`` variables, no
  user site, neither the script's folder nor the working folder on ``sys.path``), ``-B`` and ``-X utf8`` (the
  script's ``open()`` reads UTF-8 whatever the machine's code page); the environment is the short allowlist
  the test runs get (no tokens or keys), ``HOME`` / ``TMP`` a fresh folder deleted afterwards; the working
  folder is the project root; stdin is the request, read whole before the script loads;
* a timeout (``timeout=`` seconds, default 60, at most 600): the child is killed and the guard is ``unknown``;
* no database connection in the child: the claims are read by the parent and handed over as data;
* the result goes back on a private copy of the stdout pipe, with a per-run nonce read from the request
  before the script loads; file descriptor 1, ``sys.stdout`` and ``sys.__stdout__`` no longer reach that pipe,
  and a result line without the nonce is not a result.

The audit hook (PEP 578), installed before the script is read, is a tripwire against accidental writes,
network and processes, **not a sandbox**: a refused call raises in the script, so the guard is ``unknown``
with the event's name. It refuses sockets and name lookups; starting processes (``subprocess``,
``os.system`` / ``exec*`` / ``spawn*`` / ``startfile`` / ``fork``, ``kill``); ``ctypes``; ``sqlite3`` connections
and extensions; adding another audit hook; file writes (``open`` for writing, create, append or truncate;
remove, rename, mkdir, rmdir, chmod, link, utime, the ``shutil`` copy/move/rmtree calls); a writable
file ``mmap``; on Windows ``_winapi`` CreateFile, CreateJunction, CreateNamedPipe, CreatePipe, OpenProcess
and TerminateProcess, ``msvcrt.open_osfhandle``, the registry writes (``winreg`` CreateKey, SetValue(Ex),
DeleteKey, DeleteValue, SaveKey, LoadKey, ConnectRegistry, the reflection calls, OpenKey with write access),
and ``open`` / ``os.listdir`` / ``os.scandir`` / ``os.chdir`` of a UNC path (``\\\\server\\share``,
``\\\\.\\pipe\\...``; ``\\\\?\\C:\\...`` is local and allowed).

What gets past it (audit hooks are not a security boundary; CPython's documentation says so): calls that
raise no audit event - ``os.stat`` and the ``exists`` / ``is_file`` family (which reach a UNC path or a mapped
network drive too), ``_winapi.WriteFile`` / ``CreateFileMapping`` on a handle the process already has, named
anonymous ``mmap`` memory, a drive letter mapped to a network share -, native code (an extension module the
script imports), handles opened before the hook (the inherited standard streams), and a script that digs
through the interpreter (frames, monkeypatching ``json`` or ``os``) to forge its own result. Reading is not
limited: the script can read any file the user can, and anything it prints is shown back (cut). Memory and
CPU are limited only by the timeout. An untrusted project's script is not run at all.

This file is also the child program: it is run by path (``python -I -B script_guard.py``) and imports
nothing from Verinoda there.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
MAX_FINDINGS = 500
MAX_CLAIMS = 20000  # the newest stored claims handed to the script
OUTPUT_TAIL = 2000  # characters of the script's own output kept for the report
_MARK = "verinoda_script_guard"
LIMITS = ["the script's own logic decides what is a violation: Verinoda only re-reads each file:line it cites, "
          "so a VIOLATED of a script guard is strong_inference, not statically verified",
          "it runs the project's own code with the user's privileges, like the project's tests in a trusted "
          "project: trust is the boundary. The audit hook is a tripwire against accidental writes, network and "
          "processes, not a sandbox: native code, calls with no audit event (os.stat and exists() reach UNC "
          "paths and mapped network drives too) and handles opened before the hook get past it, and reads are "
          "not limited",
          "the result line must carry a nonce the script is not given; a script that digs through the "
          "interpreter (frames, monkeypatching) can still forge its result",
          "the graph is .verinoda/index/graph.json as the index last wrote it: the receiver-call edges added "
          "when a graph is loaded (receiver_calls.json) are not in guard.edges()"]


# -- the parent side --------------------------------------------------------------------------------

def script_file(repo: Path, rel: str) -> tuple[Path | None, str]:
    """``(the script's real path, "")`` when ``rel`` is a ``.py`` file inside ``repo`` after links are
    resolved, else ``(None, why)``."""
    repo = Path(repo).resolve()
    q = str(rel or "").replace("\\", "/").strip()
    if not q or q.startswith("/") or (len(q) > 1 and q[1] == ":") or ".." in q.split("/"):
        return None, f"script path {rel!r} is not a path inside the repository (relative, no '..')"
    p = (repo / q).resolve()
    if p != repo and repo not in p.parents:
        return None, f"script {rel} resolves to {p}, outside the repository (a link?): not run"
    if p.suffix.lower() != ".py":
        return None, f"script {rel} is not a .py file"
    if not p.is_file():
        return None, f"script {rel} does not exist"
    return p, ""


def _env(home: Path) -> dict[str, str]:
    from verinoda.experiments import ENV_ALLOW  # the same short allowlist the test runs get

    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_ALLOW}
    env.update({"HOME": str(home), "USERPROFILE": str(home), "TMP": str(home), "TEMP": str(home),
                "TMPDIR": str(home), "VERINODA_SCRIPT_GUARD": "1"})
    return env


def _tail(data: bytes, n: int = OUTPUT_TAIL) -> str:
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
    return text if len(text) <= n else "..." + text[-n:]


def read_claims(db: Path) -> tuple[list[dict] | None, str, int]:
    """``(claims, error, left_out)``: the newest :data:`MAX_CLAIMS` stored claims with their supporting
    evidence as ``path:line``, read here (the child gets data, never a database connection). ``None`` with
    no error when there is no store."""
    import sqlite3

    db = Path(db)
    if not db.is_file():
        return None, "", 0
    try:
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return None, f"the claims store could not be opened read-only: {exc}"[:300], 0
    try:
        total = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        rows = conn.execute("SELECT id, text, status, kind FROM claims ORDER BY created_at DESC LIMIT ?",
                            (MAX_CLAIMS,)).fetchall()
        ev: dict[str, list[str]] = {}
        for cid, p, ln in conn.execute(
                "SELECT ce.claim_id, e.path, e.line_start FROM claim_evidence ce JOIN evidence e "
                "ON e.id = ce.evidence_id WHERE ce.relation = 'supports' AND e.path IS NOT NULL AND ce.claim_id IN "
                "(SELECT id FROM claims ORDER BY created_at DESC LIMIT ?)", (MAX_CLAIMS,)):
            ev.setdefault(cid, []).append(f"{p}:{ln}" if ln else p)
    except sqlite3.Error as exc:
        return None, f"the claims store could not be read: {exc}"[:300], 0
    finally:
        conn.close()
    claims = [{"id": cid, "text": text, "status": st, "kind": kind, "evidence": ev.get(cid, [])}
              for cid, text, st, kind in rows]
    return claims, "", max(0, int(total) - len(claims))


def _result(out: bytes, nonce: str) -> tuple[dict | None, int]:
    """The child's result line (the one carrying ``nonce``) and how many other result-like lines there were."""
    res, stray = None, 0
    for line in reversed(out.decode("utf-8", errors="replace").splitlines()):
        if not line.startswith("{\"" + _MARK + "\""):
            continue
        try:
            cand = json.loads(line)
        except ValueError:
            cand = None
        if res is None and isinstance(cand, dict) and cand.get("nonce") == nonce:
            res = cand
        else:
            stray += 1
    return res, stray


def run(repo: Path, rel: str, *, timeout: int = DEFAULT_TIMEOUT, spec: str = "") -> dict:
    """Run the script guard ``rel`` of ``repo`` in a child process (see the module text for what it may do).

    ``{"status": "ok" | "error" | "timeout", "findings": [{"level", "file", "line", "why"}], "output": the
    tail of what the script printed, "error": why it did not end well, "elapsed_s", "claims_left_out"}``.
    The caller checks trust first; this function does not."""
    import secrets
    import time

    from verinoda.paths import db_path, graph_path

    repo = Path(repo).resolve()
    path, why = script_file(repo, rel)
    if path is None:
        return {"status": "error", "findings": [], "error": why}
    timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    gp = graph_path(repo)
    claims, claims_error, left_out = read_claims(db_path(repo))
    nonce = secrets.token_hex(16)
    req = {"repo": str(repo), "script": str(path), "rel": rel, "spec": spec,
           "graph": str(gp) if gp.is_file() else None, "claims": claims, "claims_error": claims_error,
           "nonce": nonce}
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="vn-guard-") as home:
        try:
            # -X utf8: the script's open() reads UTF-8 on every machine, not the locale's code page
            proc = subprocess.Popen([sys.executable, "-I", "-B", "-X", "utf8", str(Path(__file__).resolve())],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    cwd=str(repo), env=_env(Path(home)))
        except OSError as exc:
            return {"status": "error", "findings": [], "error": f"the child process did not start: {exc}"[:300]}
        try:
            out, err = proc.communicate(json.dumps(req).encode("utf-8"), timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                out, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                out, err = b"", b""
            return {"status": "timeout", "findings": [], "output": _tail(err),
                    "error": f"the script did not finish within {timeout} s and was stopped",
                    "elapsed_s": round(time.monotonic() - t0, 3)}
    elapsed = round(time.monotonic() - t0, 3)
    res, stray = _result(out, nonce)
    ignored = f"; {stray} result-like line(s) without this run's nonce were ignored" if stray else ""
    if res is None:
        return {"status": "error", "findings": [], "output": _tail(err), "elapsed_s": elapsed,
                "error": f"the script guard's process ended (exit code {proc.returncode}) without a result{ignored}"}
    res.pop("nonce", None)
    res.pop(_MARK, None)
    res.setdefault("findings", [])
    res["elapsed_s"] = elapsed
    if left_out:
        res["claims_left_out"] = left_out
    if res.get("status") == "ok":
        if proc.returncode != 0:
            res.update(status="error", error=f"the process exit code was {proc.returncode} after the script ended")
        elif stray:  # something besides the child wrote to the result channel
            res.update(status="error", error=f"the script wrote to the result channel{ignored}")
    return res


def cited_line(repo: Path, f: dict) -> tuple[str, int] | str:
    """``(rel, line)`` of a finding a script reported, or why it cannot be cited: the file must be a file of
    the repository and the line one of its lines."""
    rel = str(f.get("file") or "").replace("\\", "/").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    line = f.get("line")
    shown = f"{f.get('file')!r}:{line!r}"[:200]
    if not rel or rel.startswith("/") or (len(rel) > 1 and rel[1] == ":") or ".." in rel.split("/"):
        return f"cited {shown}, which is not a path inside the repository (relative, no '..')"
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        return f"cited {shown} without a line number (1 or more)"
    root = Path(repo).resolve()
    p = (root / rel).resolve()
    if root not in p.parents:  # a link or junction out of the repository is no evidence about it
        return f"cited {shown}, which resolves outside the repository"
    try:
        with open(p, "rb") as fh:
            n = len(fh.read().splitlines())
    except OSError:
        return f"cited {shown}, which is not a readable file of the repository"
    if line > n:
        return f"cited {shown}, but the file has {n} line(s)"
    return p.relative_to(root).as_posix(), line  # the file's own spelling (case on Windows)


# -- the child side (stdlib only; nothing from Verinoda) ----------------------------------------------

_BLOCKED = {
    "socket.__new__", "socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg", "socket.getaddrinfo",
    "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo",
    "urllib.Request", "http.client.connect",
    "subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.startfile", "os.fork",
    "os.forkpty", "os.kill", "os.killpg", "_winapi.CreateProcess", "pty.spawn",
    "ctypes.dlopen", "ctypes.dlsym", "ctypes.dlsym/handle", "ctypes.call_function", "ctypes.cdata",
    "ctypes.addressof", "ctypes.create_string_buffer",
    "sqlite3.connect", "sqlite3.enable_load_extension", "sqlite3.load_extension", "sys.addaudithook",
    "os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.truncate", "os.link",
    "os.symlink", "os.utime", "os.chflags", "os.lchflags", "os.setxattr", "os.removexattr", "os.mkfifo",
    "os.mknod", "shutil.rmtree", "shutil.copyfile", "shutil.copymode", "shutil.copystat", "shutil.copytree",
    "shutil.move",
    # Windows: files, junctions, pipes and other processes through _winapi, a C runtime file from a raw handle
    "_winapi.CreateFile", "_winapi.CreateJunction", "_winapi.CreateNamedPipe", "_winapi.CreatePipe",
    "_winapi.OpenProcess", "_winapi.TerminateProcess", "msvcrt.open_osfhandle",
    # the registry: every call that writes it (reads stay allowed; OpenKey is checked for write access below)
    "winreg.CreateKey", "winreg.SetValue", "winreg.DeleteKey", "winreg.DeleteValue", "winreg.SaveKey",
    "winreg.LoadKey", "winreg.ConnectRegistry", "winreg.DisableReflectionKey", "winreg.EnableReflectionKey",
}
_WRITE_FLAGS = 0
for _name in ("O_WRONLY", "O_RDWR", "O_APPEND", "O_CREAT", "O_TRUNC", "O_EXCL"):
    _WRITE_FLAGS |= getattr(os, _name, 0)
# KEY_SET_VALUE, KEY_CREATE_SUB_KEY, KEY_CREATE_LINK, DELETE, WRITE_DAC, WRITE_OWNER, MAXIMUM_ALLOWED,
# GENERIC_ALL, GENERIC_WRITE
_REG_WRITE = 0x2 | 0x4 | 0x20 | 0x10000 | 0x40000 | 0x80000 | 0x02000000 | 0x10000000 | 0x40000000
_LOCAL_LONG = re.compile(r"^[\\/]{2}\?[\\/][A-Za-z]:")  # \\?\C:\... is a local path


class Refused(PermissionError):
    """A call the script guard's audit hook does not allow."""


def _remote(path) -> bool:
    """A Windows UNC or device path (``\\\\server\\share``, ``//server/share``, ``\\\\.\\pipe\\x``), not a local
    ``\\\\?\\C:\\`` one."""
    if os.name != "nt" or isinstance(path, int):
        return False
    try:
        p = os.fsdecode(os.fspath(path))
    except (TypeError, ValueError):
        return False
    return len(p) > 1 and p[0] in "\\/" and p[1] in "\\/" and not _LOCAL_LONG.match(p)


def _hook(event: str, args: tuple) -> None:
    if event in _BLOCKED:
        raise Refused(f"refused in a script guard: {event} (no network, processes, native code or writes)")
    if event == "open" and args and not isinstance(args[0], int):
        if _remote(args[0]):
            raise Refused(f"refused in a script guard: opening the network path {str(args[0])[:120]}")
        mode, flags = (args[1], args[2]) if len(args) >= 3 else (None, None)
        if (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                (isinstance(flags, int) and flags & _WRITE_FLAGS):
            raise Refused(f"refused in a script guard: opening {str(args[0])[:120]} for writing")
    elif event in ("os.listdir", "os.scandir", "os.chdir") and args and _remote(args[0]):
        raise Refused(f"refused in a script guard: {event} of the network path {str(args[0])[:120]}")
    elif event == "winreg.OpenKey" and len(args) >= 3 and isinstance(args[2], int) and args[2] & _REG_WRITE:
        raise Refused("refused in a script guard: winreg.OpenKey with write access")
    elif event == "mmap.__new__" and len(args) >= 3 and args[0] != -1 and args[2] in (0, 2):
        # ACCESS_DEFAULT (0) and ACCESS_WRITE (2) of a file write through to it; ACCESS_READ and ACCESS_COPY don't
        raise Refused("refused in a script guard: a writable mmap of a file")


class _Tail:
    """A text stream that keeps the last characters written (the script's prints)."""

    def __init__(self, n: int) -> None:
        self.n, self.text = n, ""

    def write(self, s) -> int:
        s = str(s)
        self.text = (self.text + s)[-self.n:]
        return len(s)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def _line_of(line):
    """A finding's line as given when it is a plain int, else a short text of it (cited_line says why it is
    not a line)."""
    return line if type(line) is int or line is None else str(line)[:40]


class GuardAPI:
    """What ``check(guard)`` gets: the project's graph and claims to read, and two ways to report."""

    def __init__(self, req: dict, claims: list | None = None, claims_error: str = "") -> None:
        self.repo = req["repo"]
        self.spec = req.get("spec") or ""
        self._graph_file = req.get("graph")
        self._claims = claims
        self._claims_error = claims_error
        self._nodes: dict[str, dict] | None = None
        self._edges: list[dict] | None = None
        self._found: list[dict] = []

    # reading
    def _load(self) -> None:
        if self._nodes is not None:
            return
        if not self._graph_file:
            raise RuntimeError("no index: run `verinoda scan` (or `verinoda update`) first")
        with open(self._graph_file, encoding="utf-8") as fh:
            data = json.load(fh)
        nodes: dict[str, dict] = {}
        for n in data.get("nodes", []):
            loc = str(n.get("source_location") or "")
            nodes[n["id"]] = {"id": n["id"], "label": n.get("label", n["id"]),
                              "file": n.get("source_file") or None, "type": n.get("file_type"),
                              "line": int(loc[1:]) if loc.startswith("L") and loc[1:].isdigit() else None}
        edges = []
        for e in data.get("links", data.get("edges", [])):
            u, v = e.get("_src", e.get("source")), e.get("_tgt", e.get("target"))
            if u not in nodes or v not in nodes:
                continue
            loc = str(e.get("source_location") or "")
            edges.append({"source": u, "target": v, "relation": e.get("relation"), "confidence": e.get("confidence"),
                          "file": e.get("source_file") or nodes[u]["file"],
                          "line": int(loc[1:]) if loc.startswith("L") and loc[1:].isdigit() else None,
                          "source_file": nodes[u]["file"], "target_file": nodes[v]["file"]})
        self._nodes, self._edges = nodes, edges

    def files(self) -> list[str]:
        """The files the index has nodes for (repository-relative, ``/``-separated)."""
        self._load()
        return sorted({n["file"] for n in self._nodes.values() if n["file"]})

    def nodes(self, file: str | None = None) -> list[dict]:
        """The index's nodes, those of one file when ``file`` is given (an exact string: case counts)."""
        self._load()
        return [dict(n) for n in self._nodes.values() if file is None or n["file"] == file]

    def node(self, nid: str) -> dict | None:
        """One node by its id, or None."""
        self._load()
        n = self._nodes.get(nid)
        return dict(n) if n else None

    def edges(self, relations=None, from_file: str | None = None, to_file: str | None = None) -> list[dict]:
        """Edges of ``graph.json`` in their true direction, each with the file and line the index cites for it.

        Only the edges the index wrote to ``graph.json``: the receiver-call edges Verinoda adds when it loads a
        graph (``receiver_calls.json``, ``obj.method()`` calls resolved through the receiver's type) are not
        here. ``from_file`` / ``to_file`` are exact strings (case counts, also on Windows)."""
        self._load()
        rels = {relations} if isinstance(relations, str) else set(relations) if relations else None
        return [dict(e) for e in self._edges if (rels is None or e["relation"] in rels)
                and (from_file is None or e["source_file"] == from_file)
                and (to_file is None or e["target_file"] == to_file)]

    def claims(self, status: str | None = None, limit: int = 1000) -> list[dict]:
        """Stored claims, newest first, each with its supporting evidence as ``path:line`` (read by Verinoda
        before the script started: the newest 20,000 at most)."""
        if self._claims is None:
            raise RuntimeError(self._claims_error or "no claims store: run `verinoda init` / `verinoda scan` first")
        n = max(1, int(limit))
        out = [c for c in self._claims if status is None or c.get("status") == status][:n]
        return [{**c, "evidence": list(c.get("evidence") or [])} for c in out]

    def read(self, path: str) -> str:
        """A file of the repository as text."""
        q = str(path).replace("\\", "/")
        if q.startswith("/") or (len(q) > 1 and q[1] == ":") or ".." in q.split("/"):
            raise ValueError(f"{path!r} is not a path inside the repository")
        with open(os.path.join(self.repo, q), encoding="utf-8", errors="replace") as fh:
            return fh.read()

    # reporting
    def violation(self, path: str, line: int, why: str) -> None:
        self._found.append({"level": "VIOLATED", "file": str(path), "line": _line_of(line), "why": str(why)[:300]})

    def possible(self, path: str, line: int, why: str) -> None:
        self._found.append({"level": "POSSIBLE", "file": str(path), "line": _line_of(line), "why": str(why)[:300]})


def _child() -> int:
    import runpy
    import traceback

    req = json.loads(sys.stdin.buffer.read().decode("utf-8"))  # read whole: nothing is left on stdin
    nonce = str(req.pop("nonce", ""))
    claims, claims_error = req.pop("claims", None), str(req.pop("claims_error", "") or "")
    # the result goes back on a private copy of the stdout pipe; fd 1, sys.stdout and sys.__stdout__ no longer
    # reach it, so a line the script writes there is not read as a result
    result_fd = os.dup(1)
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    os.close(null)
    tail = _Tail(OUTPUT_TAIL)
    sys.stdout = sys.stderr = sys.__stdout__ = tail  # type: ignore[assignment]
    api = GuardAPI(req, claims, claims_error)
    del claims
    sys.addaudithook(_hook)
    res: dict = {_MARK: 1, "nonce": nonce, "status": "ok"}
    try:
        ns = runpy.run_path(req["script"], run_name="__verinoda_guard__")
        fn = ns.get("check")
        if not callable(fn):
            res.update(status="error", error="the script defines no check(guard) function")
        else:
            fn(api)
    except BaseException as exc:  # noqa: BLE001 - SystemExit and KeyboardInterrupt too: never a pass
        tb = traceback.extract_tb(exc.__traceback__)
        mine = [fr for fr in tb if os.path.normcase(fr.filename) == os.path.normcase(req["script"])]
        where = f" (line {mine[-1].lineno} of {req['rel']})" if mine else ""
        res.update(status="error", error=f"{type(exc).__name__}: {exc}"[:300] + where)
    found = api._found
    res["findings"] = found[:MAX_FINDINGS]
    if len(found) > MAX_FINDINGS:
        res["cut"] = len(found) - MAX_FINDINGS
    res["output"] = tail.text.strip()
    data = b"\n" + json.dumps(res, ensure_ascii=True, default=str).encode("ascii") + b"\n"
    while data:
        data = data[os.write(result_fd, data):]
    os.close(result_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(_child())
