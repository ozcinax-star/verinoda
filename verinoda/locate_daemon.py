"""A per-repository local daemon for `verinoda locate` and `verinoda coupled`: the graph and what git read stay loaded
between requests. Loading the graph takes 3 s on seL4 and about 30 s on Home Assistant; a request to the daemon takes
tens of milliseconds to about a second, which is what a prompt hook or the first search of an agent can wait for.

    verinoda locate --daemon start|stop|status --repo R [--json]

* Loopback only (127.0.0.1, a port the system picks). Every request needs the header ``X-Verinoda-Token``: a random
  token per daemon, kept with the port in ``.verinoda/locate-daemon.json`` (readable by the user alone) and compared in
  constant time. Nothing connects anywhere except the commands here to the daemon they started on this machine.
* ``POST /locate`` ``{"text", "anchors", "max_files", "max_chars", "history"}`` and ``POST /coupled`` ``{"files",
  "max_files", "history"}`` answer what the command's ``--json`` prints, plus ``text``: the compact text a model reads.
  ``GET /status``; ``POST /shutdown`` (what ``stop`` uses, so a stop never signals a process id that may have been
  reused). Requests are answered one after another (git and the graph are shared).
* A state file is only believed after the server it names answers as this repository's daemon (its `repo` and `pid`), and
  only 127.0.0.1 is ever connected to, whatever the file says: a repository cloned from anywhere may ship one.
* It listens at once and loads the graph in the background: ``start`` returns when it listens, ``/coupled`` is answered
  meanwhile and ``/locate`` waits for the graph (``/status`` says ``loading``).
* It ends by itself after ``IDLE_TIMEOUT`` seconds without a request, and ``start`` finds one that is running by the
  state file and a status request, never by a process id.
"""

from __future__ import annotations

import hmac
import http.client
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from verinoda import locate
from verinoda.paths import atlas_dir

STATE_NAME = "locate-daemon.json"
LOG_NAME = "locate-daemon.log"
HOST = "127.0.0.1"
IDLE_TIMEOUT = 600.0
START_WAIT = 150.0  # seconds `start` waits for the daemon to listen (not for the graph: that loads meanwhile)
LOAD_WAIT = 900.0  # seconds a `locate` request waits for the graph to load
STOP_WAIT = 20.0
ASK_TIMEOUT = 600.0  # seconds a command waits for the daemon's answer (a big repository's first one waits for the graph)
TOKEN_HEADER = "X-Verinoda-Token"
MAX_BODY = 1_000_000


def state_path(repo: Path | str) -> Path:
    return atlas_dir(Path(repo)) / STATE_NAME


def log_path(repo: Path | str) -> Path:
    return atlas_dir(Path(repo)) / LOG_NAME


def _read_state(repo: Path | str) -> dict | None:
    try:
        data = json.loads(state_path(repo).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ok = (isinstance(data, dict) and isinstance(data.get("port"), int) and 0 < data["port"] < 65536
          and isinstance(data.get("token"), str))
    return data if ok else None


def _remove_state(repo: Path | str, pid: int | None = None) -> None:
    """Remove the state file (only the one of ``pid`` when given: a newer daemon's stays)."""
    try:
        if pid is not None and (_read_state(repo) or {}).get("pid") != pid:
            return
        state_path(repo).unlink()
    except OSError:
        pass


def _write_state(repo: Path | str, state: dict) -> bool:
    """Write the state file unless another daemon's is there (a link to a finished temporary file fails when the target exists,
    so of two daemons started at once exactly one writes it); False when it was not written."""
    from verinoda.mcp.transport import _user_only

    p = state_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(state, indent=2))
    note = _user_only(tmp)
    try:
        os.link(tmp, p)
        written = True
    except FileExistsError:
        written = False
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    if note:
        print(f"verinoda locate daemon: warning: {note}", file=sys.stderr, flush=True)
    return written


# -- the server ---------------------------------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "verinoda-locate"

    def log_message(self, *args: Any) -> None:  # no access log
        return

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict | None | str:
        """The JSON object of the body; None when it is not one; "big" when it is over the limit (read and dropped, so the
        client that is still sending gets its answer)."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                left = min(n, 16 * MAX_BODY)
                while left > 0:
                    chunk = self.rfile.read(min(65536, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                return "big"
            raw = self.rfile.read(n) if n > 0 else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def _route(self, method: str) -> None:
        d: Daemon = self.server.daemon  # type: ignore[attr-defined]
        given = (self.headers.get(TOKEN_HEADER) or "").encode("utf-8")
        if not hmac.compare_digest(given, d.token.encode("utf-8")):
            return self._send(401, {"error": "unauthorized", "message": f"send the daemon's token in {TOKEN_HEADER}"})
        d.begin()
        try:
            return self._answer(d, method)
        finally:
            d.end()

    def _answer(self, d: Daemon, method: str) -> None:
        path = self.path.split("?", 1)[0]
        allowed = {"/status": "GET", "/locate": "POST", "/coupled": "POST", "/shutdown": "POST"}
        if path not in allowed:
            return self._send(404, {"error": "not found", "message": "routes: /status /locate /coupled /shutdown"})
        if method != allowed[path]:
            return self._send(405, {"error": "method not allowed", "message": f"{path} takes {allowed[path]}"})
        if path == "/status":
            return self._send(200, d.status())
        if path == "/shutdown":
            self._send(200, {"stopping": True, "pid": os.getpid()})
            threading.Thread(target=d.shutdown, daemon=True).start()
            return None
        body = self._body()
        if body == "big":
            return self._send(413, {"error": f"the body is over {MAX_BODY:,} bytes"})
        if not isinstance(body, dict):
            return self._send(400, {"error": "the body is a JSON object"})
        kind = path.lstrip("/")
        code, out = d.answer(kind, body)
        return self._send(code, out)

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")


class Daemon:
    """The graph kept loaded behind a loopback HTTP server. ``serve_forever`` runs until ``shutdown``, a ``/shutdown``
    request or ``idle_timeout`` seconds without a request. ``ready`` is set once it listens; the graph loads in the
    background (30 s or more on a big repository) and ``loaded`` is set when it has: ``/coupled`` needs no graph and is
    answered meanwhile, ``/locate`` waits for it."""

    def __init__(self, repo: Path | str, *, token: str | None = None, idle_timeout: float = IDLE_TIMEOUT,
                 state_file: bool = False) -> None:
        self.repo = Path(repo).resolve()
        self.token = token or secrets.token_urlsafe(32)
        self.idle_timeout = float(idle_timeout)
        self.keeper = locate.GraphKeeper(self.repo)
        self.cache: dict = {}
        self.requests = 0
        self.warm = False
        self.ready = threading.Event()
        self.loaded = threading.Event()
        self.stopped = threading.Event()
        self._lock = threading.Lock()
        self._lock_active = threading.Lock()
        self._serving = threading.Event()
        self._stop_asked = False
        self._last = time.monotonic()
        self._started = time.monotonic()
        self._active = 0  # requests being answered: the idle clock does not run while there is one
        self._write = state_file

        class _Server(ThreadingHTTPServer):
            allow_reuse_address = False  # on Windows SO_REUSEADDR would let another process take the port over
            daemon_threads = True

        self.httpd = _Server((HOST, 0), _Handler)
        self.httpd.daemon = self  # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        self.url = f"http://{HOST}:{self.port}"

    def touch(self) -> None:
        self._last = time.monotonic()

    def begin(self) -> None:
        with self._lock_active:
            self._active += 1
        self.touch()

    def end(self) -> None:
        with self._lock_active:
            self._active -= 1
        self.touch()

    def status(self) -> dict:
        return {"ok": True, "pid": os.getpid(), "repo": str(self.repo), "graph_loaded": self.warm,
                "loading": not self.loaded.is_set(), "requests": self.requests, "idle_timeout": self.idle_timeout,
                "uptime_s": round(time.monotonic() - self._started, 1)}

    def answer(self, kind: str, body: dict) -> tuple[int, dict]:
        """One locate or coupled request: (HTTP status, JSON). Errors are answers; the daemon serves the next."""
        if kind == "locate" and not self.loaded.wait(LOAD_WAIT):
            return 503, {"error": "the graph is still loading"}
        with self._lock:
            self.requests += 1
            try:
                max_chars = max(200, int(body.get("max_chars") or 1800))
            except (TypeError, ValueError):
                return 400, {"error": "max_chars is a number"}
            resp = locate.handle({"op": kind, **body}, self.repo, self.keeper, self.cache)
        if not resp.get("ok"):
            return 400, {"error": str(resp.get("error") or "failed")}
        res = resp["result"]
        res["text"] = locate.render(res, max_chars, kind=kind)
        return 200, res

    def _claim_state(self) -> bool:
        """Write this daemon's state file; False when a live daemon of this repository has written its own (a stale file is
        replaced)."""
        state = {"pid": os.getpid(), "url": self.url, "host": HOST, "port": self.port, "token": self.token,
                 "repo": str(self.repo), "idle_timeout": self.idle_timeout,
                 "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        for _ in range(2):
            if _write_state(self.repo, state):
                return True
            if status(self.repo)["running"]:
                return False  # status removes a file nobody answers for, so the second try can write
        return False

    def _watch(self) -> None:
        step = max(0.05, min(1.0, self.idle_timeout / 4))
        while not self.stopped.wait(step):
            if self._active == 0 and time.monotonic() - self._last > self.idle_timeout:
                self.shutdown()
                return

    def shutdown(self) -> None:
        self._stop_asked = True
        if self._serving.is_set():
            self.httpd.shutdown()

    def _load(self) -> None:
        try:
            self.keeper()
            self.warm = True
        except Exception as exc:  # noqa: BLE001 - coupled needs no graph: the daemon still serves it
            print(f"verinoda locate daemon: the graph could not be loaded ({exc}); locate will fail",
                  file=sys.stderr, flush=True)
        finally:
            self.touch()
            self.loaded.set()

    def serve_forever(self) -> None:
        try:
            if self._write and not self._claim_state():
                print("verinoda locate daemon: another daemon serves this repository; ending", file=sys.stderr, flush=True)
                return
            self.touch()
            threading.Thread(target=self._watch, daemon=True).start()
            threading.Thread(target=self._load, daemon=True).start()
            self.ready.set()
            if not self._stop_asked:
                self._serving.set()
                self.httpd.serve_forever(poll_interval=0.1)
        finally:
            self._serving.clear()
            self.stopped.set()
            self.httpd.server_close()
            if self._write:
                _remove_state(self.repo, os.getpid())


def run(repo: Path | str, *, idle_timeout: float = IDLE_TIMEOUT) -> int:
    """The daemon process: serve until stopped or idle (what `start` spawns)."""
    d = Daemon(repo, idle_timeout=idle_timeout, state_file=True)
    print(f"verinoda locate daemon: {d.url} for {d.repo}", file=sys.stderr, flush=True)
    d.serve_forever()
    return 0


# -- the commands -------------------------------------------------------------------------------------------------

def _request(state: dict, method: str, path: str, timeout: float = 15.0) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(HOST, state["port"], timeout=timeout)  # never the file's host
    try:
        conn.request(method, path, body=b"{}" if method == "POST" else None, headers={TOKEN_HEADER: state["token"]})
        r = conn.getresponse()
        raw = r.read()
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        return r.status, data if isinstance(data, dict) else {}
    finally:
        conn.close()


def _post(state: dict, path: str, body: dict, timeout: float) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(HOST, state["port"], timeout=timeout)  # never the file's host
    try:
        conn.request("POST", path, body=json.dumps(body).encode("utf-8"),
                     headers={TOKEN_HEADER: state["token"], "Content-Type": "application/json"})
        r = conn.getresponse()
        raw = r.read()
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        return r.status, data if isinstance(data, dict) else {}
    finally:
        conn.close()


def ask(repo: Path | str, kind: str, body: dict) -> dict | None:
    """What this repository's running daemon answers to a ``locate`` or ``coupled`` request (the JSON `--json` prints), or None
    when none runs or it cannot answer: the command then computes the answer itself. The commands call this first, so a
    caller that runs them (the mod: a host's own HTTP call has a time limit, a command's can be ten minutes) gets the daemon's
    speed without knowing about it."""
    repo = Path(repo).resolve()
    if not status(repo)["running"]:
        return None
    state = _read_state(repo)
    if state is None:
        return None
    try:
        code, data = _post(state, f"/{kind}", body, ASK_TIMEOUT)
    except OSError:
        return None
    return data if code == 200 and isinstance(data.get("files"), list) and isinstance(data.get("text"), str) else None


def _ours(data: dict, repo: Path, state: dict) -> bool:
    """Does the server that answered say it is this repository's daemon, the process the state file names?"""
    served = str(data.get("repo") or "")
    return bool(served) and os.path.normcase(str(Path(served).resolve())) == os.path.normcase(str(repo)) \
        and data.get("pid") == state.get("pid")


def status(repo: Path | str) -> dict:
    """``{"running": True, url, token, pid, ...}`` when this repository's daemon answers, else ``{"running": False}`` (a state
    file nobody answers for, or a server that is not this repository's daemon, is removed). A daemon that does not answer in
    time is running and busy (``unresponsive``): its file stays."""
    repo = Path(repo).resolve()
    state = _read_state(repo)
    if state is None:
        _remove_state(repo)  # no file, or one that is no state
        return {"running": False}
    url = f"http://{HOST}:{state['port']}"
    try:
        code, data = _request(state, "GET", "/status")
    except (ConnectionRefusedError, FileNotFoundError):
        _remove_state(repo, state.get("pid"))
        return {"running": False}
    except OSError:
        return {"running": True, "unresponsive": True, "url": url, "token": state["token"], "pid": state.get("pid")}
    if code != 200 or not _ours(data, repo, state):
        _remove_state(repo, state.get("pid"))
        return {"running": False}
    return {"running": True, "url": url, "token": state["token"], "pid": data.get("pid", state.get("pid")),
            "graph_loaded": data.get("graph_loaded"), "loading": data.get("loading"), "requests": data.get("requests"),
            "idle_timeout": data.get("idle_timeout"), "uptime_s": data.get("uptime_s")}


def start(repo: Path | str, *, idle_timeout: float = IDLE_TIMEOUT, wait: float = START_WAIT) -> dict:
    """Start the daemon in the background (unless one runs) and wait until it listens: ``status`` plus ``started``."""
    repo = Path(repo).resolve()
    current = status(repo)
    if current["running"]:
        return {**current, "started": False}
    _remove_state(repo)
    log = log_path(repo)
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "-m", "verinoda", "locate", "--serve-http", "--repo", str(repo), "--idle-timeout", str(idle_timeout)]
    kw: dict[str, Any] = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    with open(log, "ab") as out:
        # cwd is the index folder, not the repository: `python -m` puts the cwd first on sys.path, and a repository
        # that is Verinoda's own source would then import its own verinoda/
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out, cwd=str(log.parent), close_fds=True, **kw)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        found = status(repo)
        if found["running"]:
            return {**found, "started": True}
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    if proc.poll() is None:  # never listened: do not leave it (or the launcher's child) running
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=30, check=False)
        else:
            proc.kill()
        proc.wait(10)
    try:
        tail = log.read_text(encoding="utf-8", errors="replace")[-1500:]
    except OSError:
        tail = ""
    return {"running": False, "started": False, "exit_code": proc.returncode, "log": str(log), "log_tail": tail}


def stop(repo: Path | str) -> dict:
    """Ask the daemon to end and wait until its port closes: ``{"running": False}`` (also when none ran); ``{"running": True,
    "error": ...}`` when it refused or did not answer (its state file stays)."""
    repo = Path(repo).resolve()
    state = _read_state(repo)
    if state is None:
        return {"running": False}
    try:
        code, _ = _request(state, "POST", "/shutdown")
    except (ConnectionRefusedError, FileNotFoundError):
        _remove_state(repo, state.get("pid"))
        return {"running": False}
    except OSError as exc:
        return {"running": True, "error": f"no answer to the stop request ({type(exc).__name__})"}
    if code == 401:
        return {"running": True, "error": "the daemon refuses the token in the state file"}
    if code != 200:
        return {"running": True, "error": f"the daemon answered the stop request with HTTP {code}"}
    deadline = time.monotonic() + STOP_WAIT
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((HOST, state["port"]), timeout=0.5):
                pass
        except OSError:
            break
        time.sleep(0.2)
    _remove_state(repo, state.get("pid"))
    return {"running": False}


def render(res: dict) -> str:
    """The one line `--daemon` prints without --json (the token is only in the JSON)."""
    if not res.get("running"):
        tail = f"\n{res['log_tail']}" if res.get("log_tail") else ""
        return "locate daemon: not running" + tail
    if res.get("error"):
        return f"locate daemon: still running ({res['error']})"
    if res.get("unresponsive"):
        return f"locate daemon: running at {res.get('url')} (pid {res.get('pid')}), busy: it did not answer in time"
    loaded = "graph loaded" if res.get("graph_loaded") else "graph loading" if res.get("loading") else "graph not loaded"
    return (f"locate daemon: running at {res.get('url')} (pid {res.get('pid')}, {loaded}, {res.get('requests') or 0} "
            f"requests, ends after {res.get('idle_timeout')} s idle)")
