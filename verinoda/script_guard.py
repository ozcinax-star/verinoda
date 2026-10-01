"""Script guards: a decision record's guard written as a small Python program (``script path=FILE.py``).

A rule the declarative guards cannot say (``only_in``, ``no_edge``, ``layers`` ...) can be a script in the
repository that defines ``check(guard)``. ``verinoda decide check`` runs it and reports what it found like
any other guard: each finding at a ``file:line`` that is re-read, a script that raises, times out, exits
or prints no result is ``unknown`` (never ok), and an ok says the script ran to its end.

``guard`` is a read-only view (:class:`GuardAPI`): ``repo``, ``spec``, ``files()``, ``nodes(file=None)``,
``node(id)``, ``edges(relations=None, from_file=None, to_file=None)`` (the graph as the index last wrote it,
``.verinoda/index/graph.json``), ``claims(status=None, limit=1000)`` (the stored claims with their
``path:line`` evidence), ``read(path)``, and two ways to report: ``violation(path, line, why)`` and
``possible(path, line, why)``. The return value of ``check`` is not read.

Where it runs, and what that does and does not keep out:

* only in a project the user trusts (``verinoda trust``, recorded outside every repository), and only from
  the CLI (``decide check``, ``decide baseline``): MCP, ``update``'s summary line and ``what-if`` report a
  script guard as ``unknown``, and MCP refuses to record one;
* a child process of Verinoda's own interpreter, never the project's: ``-I`` (no ``PYTHON*`` variables, no
  user site, neither the script's folder nor the working folder on ``sys.path``), ``-B`` and ``-X utf8`` (the
  script's ``open()`` reads UTF-8 whatever the machine's code page); the environment
  is the short allowlist the test runs get (no tokens or keys), ``HOME`` / ``TMP`` a fresh folder deleted
  afterwards; the working folder is the project root; stdin is the request, nothing else;
* a timeout (``timeout=`` seconds, default 60, at most 600): the child is killed and the guard is ``unknown``;
* a Python audit hook (PEP 578) installed before the script is read refuses sockets and name lookups,
  starting processes (``subprocess``, ``os.system`` / ``exec*`` / ``spawn*`` / ``startfile`` / ``fork``),
  ``ctypes`` and ``sqlite3`` connections or extensions, adding another audit hook, and file writes (``open``
  for writing, create, append or truncate; remove, rename, mkdir, rmdir, chmod, link, utime). A refused call
  raises in the script, so the guard is ``unknown`` with the event's name.

This is **not a sandbox**. Audit hooks are not a security boundary (CPython's documentation says so):
native code (an extension module the script imports, a bug in one) does not raise those events, and
``mmap`` or an already open handle is not checked. Reading is not limited: the script can read any file the
user can, and anything it prints is shown back (cut). Memory and CPU are limited only by the timeout. The
point is that a trusted project's own rule cannot quietly reach the network, start programs or change
files, not that a hostile script is contained; an untrusted project's script is not run at all.

This file is also the child program: it is run by path (``python -I -B script_guard.py``) and imports
nothing from Verinoda there.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
MAX_FINDINGS = 500
OUTPUT_TAIL = 2000  # characters of the script's own output kept for the report
_MARK = "verinoda_script_guard"
LIMITS = ["the script's own logic decides what is a violation: Verinoda only re-reads each file:line it cites, "
          "so a VIOLATED of a script guard is strong_inference, not statically verified",
          "not a sandbox: a separate process with a scrubbed environment, a timeout and an audit hook that refuses "
          "network, new processes, ctypes and file writes; native code gets past an audit hook, and reads are "
          "not limited",
          "the graph is .verinoda/index/graph.json as the index last wrote it (the receiver-call edges added "
          "when a graph is loaded are not in it)"]


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


def run(repo: Path, rel: str, *, timeout: int = DEFAULT_TIMEOUT, spec: str = "") -> dict:
    """Run the script guard ``rel`` of ``repo`` in a child process (see the module text for what it may do).

    ``{"status": "ok" | "error" | "timeout", "findings": [{"level", "file", "line", "why"}], "output": the
    tail of what the script printed, "error": why it did not end well, "elapsed_s"}``. The caller checks
    trust first; this function does not."""
    import time

    from verinoda.paths import db_path, graph_path

    repo = Path(repo).resolve()
    path, why = script_file(repo, rel)
    if path is None:
        return {"status": "error", "findings": [], "error": why}
    timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    gp, db = graph_path(repo), db_path(repo)
    req = {"repo": str(repo), "script": str(path), "rel": rel, "spec": spec,
           "graph": str(gp) if gp.is_file() else None, "db": str(db) if db.is_file() else None}
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
    res = None
    for line in reversed(out.decode("utf-8", errors="replace").splitlines()):
        if line.startswith("{\"" + _MARK + "\""):
            try:
                res = json.loads(line)
            except ValueError:
                res = None
            break
    if not isinstance(res, dict):
        return {"status": "error", "findings": [], "output": _tail(err or out), "elapsed_s": elapsed,
                "error": f"the script guard's process ended (exit code {proc.returncode}) without a result"}
    res.setdefault("findings", [])
    res["elapsed_s"] = elapsed
    if proc.returncode != 0 and res.get("status") == "ok":
        res.update(status="error", error=f"the process exit code was {proc.returncode} after the script ended")
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
    "os.symlink", "os.utime", "os.chflags", "os.lchflags", "os.setxattr", "os.removexattr", "shutil.rmtree",
    "shutil.copyfile", "shutil.copymode", "shutil.copystat", "shutil.copytree", "shutil.move",
}
_WRITE_FLAGS = 0
for _name in ("O_WRONLY", "O_RDWR", "O_APPEND", "O_CREAT", "O_TRUNC", "O_EXCL"):
    _WRITE_FLAGS |= getattr(os, _name, 0)


class Refused(PermissionError):
    """A call the script guard's audit hook does not allow."""


def _hook(event: str, args: tuple) -> None:
    if event in _BLOCKED:
        raise Refused(f"refused in a script guard: {event} (no network, processes, native code or writes)")
    if event == "open" and len(args) >= 3 and not isinstance(args[0], int):
        mode, flags = args[1], args[2]
        if (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                (isinstance(flags, int) and flags & _WRITE_FLAGS):
            raise Refused(f"refused in a script guard: opening {str(args[0])[:120]} for writing")


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


class GuardAPI:
    """What ``check(guard)`` gets: the project's graph and claims to read, and two ways to report."""

    def __init__(self, req: dict, conn=None, conn_error: str = "") -> None:
        self.repo = req["repo"]
        self.spec = req.get("spec") or ""
        self._graph_file = req.get("graph")
        self._conn = conn
        self._conn_error = conn_error
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
        """The files the index has nodes for."""
        self._load()
        return sorted({n["file"] for n in self._nodes.values() if n["file"]})

    def nodes(self, file: str | None = None) -> list[dict]:
        self._load()
        return [dict(n) for n in self._nodes.values() if file is None or n["file"] == file]

    def node(self, nid: str) -> dict | None:
        self._load()
        n = self._nodes.get(nid)
        return dict(n) if n else None

    def edges(self, relations=None, from_file: str | None = None, to_file: str | None = None) -> list[dict]:
        """Edges in their true direction, each with the file and line the index cites for it."""
        self._load()
        rels = {relations} if isinstance(relations, str) else set(relations) if relations else None
        return [dict(e) for e in self._edges if (rels is None or e["relation"] in rels)
                and (from_file is None or e["source_file"] == from_file)
                and (to_file is None or e["target_file"] == to_file)]

    def claims(self, status: str | None = None, limit: int = 1000) -> list[dict]:
        """Stored claims, newest first, each with its supporting evidence as ``path:line``."""
        if self._conn is None:
            raise RuntimeError(self._conn_error or "no claims store: run `verinoda init` / `verinoda scan` first")
        q = "SELECT id, text, status, kind FROM claims" + (" WHERE status = ?" if status else "") + \
            " ORDER BY created_at DESC LIMIT ?"
        rows = self._conn.execute(q, ((status,) if status else ()) + (max(1, int(limit)),)).fetchall()
        out = []
        for cid, text, st, kind in rows:
            ev = self._conn.execute(
                "SELECT e.path, e.line_start FROM claim_evidence ce JOIN evidence e ON e.id = ce.evidence_id "
                "WHERE ce.claim_id = ? AND ce.relation = 'supports' AND e.path IS NOT NULL", (cid,)).fetchall()
            out.append({"id": cid, "text": text, "status": st, "kind": kind,
                        "evidence": [f"{p}:{ln}" if ln else p for p, ln in ev]})
        return out

    def read(self, path: str) -> str:
        """A file of the repository as text."""
        q = str(path).replace("\\", "/")
        if q.startswith("/") or (len(q) > 1 and q[1] == ":") or ".." in q.split("/"):
            raise ValueError(f"{path!r} is not a path inside the repository")
        with open(os.path.join(self.repo, q), encoding="utf-8", errors="replace") as fh:
            return fh.read()

    # reporting
    def violation(self, path: str, line: int, why: str) -> None:
        self._found.append({"level": "VIOLATED", "file": str(path), "line": line, "why": str(why)[:300]})

    def possible(self, path: str, line: int, why: str) -> None:
        self._found.append({"level": "POSSIBLE", "file": str(path), "line": line, "why": str(why)[:300]})


def _child() -> int:
    import runpy
    import traceback

    req = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    result = sys.stdout.buffer  # the result channel; the script's prints go to a tail kept in memory
    tail = _Tail(OUTPUT_TAIL)
    sys.stdout = sys.stderr = tail  # type: ignore[assignment]
    conn, conn_error = None, ""
    if req.get("db"):
        import sqlite3

        try:  # opened before the hook (which refuses new connections), read-only
            conn = sqlite3.connect(Path(req["db"]).as_uri() + "?mode=ro", uri=True)
            conn.execute("PRAGMA query_only = ON")
        except sqlite3.Error as exc:
            conn, conn_error = None, f"the claims store could not be opened read-only: {exc}"
    api = GuardAPI(req, conn, conn_error)
    sys.addaudithook(_hook)
    res: dict = {_MARK: 1, "status": "ok"}
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
    res["findings"] = api._found[:MAX_FINDINGS]
    if len(api._found) > MAX_FINDINGS:
        res["cut"] = len(api._found) - MAX_FINDINGS
    res["output"] = tail.text.strip()
    result.write(b"\n" + json.dumps(res, ensure_ascii=True, default=str).encode("ascii") + b"\n")
    result.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(_child())
