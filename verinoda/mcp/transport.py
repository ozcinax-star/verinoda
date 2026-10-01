"""The MCP server over HTTP (``verinoda mcp serve --transport http``) and the background daemon.

* Streamable HTTP from the installed MCP SDK (``streamable_http_app``; no other dependency), served by uvicorn
  (which the SDK itself depends on) on a socket bound here, ``127.0.0.1`` unless the user passes ``--host``.
* Every request needs ``Authorization: Bearer <token>``; anything else gets 401 before it reaches the SDK. The
  token is generated once (``secrets``, 256 bits), kept in the user config folder readable by the user only
  (POSIX mode 0600; on Windows the file's inherited access is replaced by the user alone, ``icacls``), and
  compared in constant time (``hmac.compare_digest``).
* Two routes of Verinoda's own, behind the same token: ``GET /verinoda/status`` and ``POST /verinoda/shutdown``.
  ``verinoda mcp daemon stop`` uses the second, so a stop never signals a process id that may have been reused.
* Nothing here connects anywhere except the daemon commands to the server they started on this machine.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MCP_PATH = "/mcp"
TOKEN_NAME = "mcp-token"
STATE_NAME = "mcp-daemon.json"
LOG_NAME = "mcp-daemon.log"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
START_WAIT = 90.0  # seconds `daemon start` waits for the server to listen
STOP_WAIT = 20.0


def _config_dir() -> Path:
    from verinoda.paths import user_config_dir

    return user_config_dir()


def token_path() -> Path:
    return _config_dir() / TOKEN_NAME


def state_path() -> Path:
    return _config_dir() / STATE_NAME


def log_path() -> Path:
    return _config_dir() / LOG_NAME


def _user_only(p: Path) -> str | None:
    """Make ``p`` readable and writable by the current user only; a note when that could not be done."""
    if os.name != "nt":
        try:
            os.chmod(p, 0o600)
            return None
        except OSError as exc:
            return f"could not set {p} to mode 0600: {exc}"
    user = os.environ.get("USERNAME")
    if not user:
        return f"could not restrict {p}: USERNAME is not set"
    who = f"{os.environ['USERDOMAIN']}\\{user}" if os.environ.get("USERDOMAIN") else user
    # inherited entries off, the user alone granted, then the entries a new file can carry explicitly (Python
    # creates folders with SYSTEM, Administrators and OWNER RIGHTS) and the broad groups removed
    steps = (["/inheritance:r", "/grant:r", f"{who}:F"], ["/remove:g", *_OTHER_SIDS])
    for step in steps:
        try:
            r = subprocess.run(["icacls", str(p), *step], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"could not restrict {p} with icacls: {exc}"
        if r.returncode != 0:
            return f"icacls could not restrict {p}: {(r.stdout + r.stderr).strip()[:200]}"
    return None


# SYSTEM, Administrators, OWNER RIGHTS, Users, Everyone, Authenticated Users
_OTHER_SIDS = ("*S-1-5-18", "*S-1-5-32-544", "*S-1-3-4", "*S-1-5-32-545", "*S-1-1-0", "*S-1-5-11")


def load_token(*, rotate: bool = False) -> tuple[str, Path]:
    """The server's bearer token (generated on first use, or anew with ``rotate``) and the file it is kept in."""
    p = token_path()
    if not rotate:
        try:
            tok = p.read_text(encoding="utf-8").strip()
        except OSError:
            tok = ""
        if len(tok) >= 32:
            if os.name != "nt" and p.stat().st_mode & 0o077:
                _user_only(p)  # tighten a file someone loosened
            return tok, p
    p.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(tok + "\n")
    note = _user_only(tmp)
    os.replace(tmp, p)
    if note:
        print(f"verinoda mcp: warning: {note}", file=sys.stderr, flush=True)
    return tok, p


# -- the ASGI app ------------------------------------------------------------------------------

async def _send_json(send, status: int, body: dict, headers: list[tuple[bytes, bytes]] | None = None) -> None:
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode()),
                            *(headers or [])]})
    await send({"type": "http.response.body", "body": data})


class BearerAuth:
    """ASGI middleware: 401 for any HTTP request without the token; Verinoda's own routes behind it."""

    def __init__(self, app, token: str, routes: dict[tuple[str, str], Callable] | None = None):
        self.app = app
        self._expected = token.encode("utf-8")
        self.routes = routes or {}

    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        if kind == "lifespan":
            return await self.app(scope, receive, send)
        if kind != "http":  # websockets and anything else: not served
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        given = b""
        for k, v in scope.get("headers") or ():
            if k.lower() == b"authorization":
                given = v
                break
        # the scheme is case-insensitive (RFC 7235); the token itself is compared in constant time
        scheme, _, credentials = given.strip().partition(b" ")
        if scheme.lower() != b"bearer" or not hmac.compare_digest(credentials.strip(), self._expected):
            await _send_json(send, 401, {"error": "unauthorized",
                                         "message": "this server needs 'Authorization: Bearer <token>'; the token "
                                                    "is in the file `verinoda mcp token` names"},
                             [(b"www-authenticate", b'Bearer realm="verinoda"')])
            return
        route = self.routes.get((scope.get("method", ""), scope.get("path", "")))
        if route is not None:
            return await route(send)
        await self.app(scope, receive, send)


def _sdk_app(srv, host: str):
    """The SDK's streamable HTTP ASGI app for ``srv`` (mcp 2.x takes the host; 1.x reads its settings)."""
    make = getattr(srv, "streamable_http_app", None)
    if make is None:
        raise RuntimeError("the installed MCP SDK has no streamable HTTP transport; upgrade it (mcp>=1.8) or use "
                           "--transport stdio")
    try:
        return make(host=host, streamable_http_path=MCP_PATH)
    except TypeError:  # mcp 1.x: options live on the server's settings
        try:
            srv.settings.host = host
            srv.settings.streamable_http_path = MCP_PATH
        except AttributeError:  # pragma: no cover - SDK layout changed
            pass
        return make()


def is_loopback(host: str) -> bool:
    return host in LOOPBACK or host.startswith("127.")


def make_http_server(srv, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, token: str,
                     status: Callable[[], dict] | None = None, on_started: Callable[[], None] | None = None):
    """``(uvicorn server, bound socket, url)``; ``server.run(sockets=[sock])`` serves until
    ``server.should_exit`` (set by ``POST /verinoda/shutdown`` too). ``port`` 0 picks a free one.
    ``on_started`` runs once the server accepts connections (after the app's lifespan startup), never when that
    startup failed."""
    import uvicorn

    box: dict[str, Any] = {}
    started = time.time()

    async def status_route(send):
        await _send_json(send, 200, {"pid": os.getpid(), "uptime_seconds": round(time.time() - started, 1),
                                     **(status() if status else {})})

    async def shutdown_route(send):
        await _send_json(send, 200, {"stopping": True, "pid": os.getpid()})
        box["server"].should_exit = True

    app = BearerAuth(_sdk_app(srv, host), token, {("GET", "/verinoda/status"): status_route,
                                                  ("POST", "/verinoda/shutdown"): shutdown_route})
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if os.name != "nt":  # on Windows SO_REUSEADDR would let another process take the port over
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    bound = sock.getsockname()[1]
    config = uvicorn.Config(app, log_level="warning", access_log=False, lifespan="on")

    class _Server(uvicorn.Server):
        async def startup(self, *a, **kw):
            await super().startup(*a, **kw)
            if on_started is not None and self.started and not self.should_exit:
                on_started()

    server = box["server"] = _Server(config)
    shown = f"[{host}]" if ":" in host else host
    return server, sock, f"http://{shown}:{bound}{MCP_PATH}"


def serve_http(srv, *, host: str, port: int, status: Callable[[], dict] | None = None,
               state_file: Path | None = None, label: str = "") -> None:
    """Serve ``srv`` over streamable HTTP until stopped (Ctrl+C, ``POST /verinoda/shutdown``)."""
    token, tpath = load_token()
    box: dict[str, Any] = {}

    def started() -> None:  # the state file says "listening" only once the server accepts connections
        url, port_ = box["url"], box["port"]
        print(f"verinoda mcp: {label} listening on {url} (bearer token in {tpath})", file=sys.stderr, flush=True)
        if state_file is not None:
            state = {"pid": os.getpid(), "url": url, "host": host, "port": port_,
                     "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "log": str(log_path()),
                     **({"projects": status().get("projects")} if status else {})}
            tmp = state_file.with_name(state_file.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            os.replace(tmp, state_file)

    server, sock, url = make_http_server(srv, host=host, port=port, token=token, status=status, on_started=started)
    box.update(url=url, port=sock.getsockname()[1])
    if not is_loopback(host):
        print(f"verinoda mcp: warning: {host} is reachable from other machines; the token is the only protection "
              "and the traffic is not encrypted", file=sys.stderr, flush=True)
    try:
        server.run(sockets=[sock])
    finally:
        sock.close()
        if state_file is not None:
            try:
                if json.loads(state_file.read_text(encoding="utf-8")).get("pid") == os.getpid():
                    state_file.unlink()
            except (OSError, ValueError):
                pass


# -- the daemon ---------------------------------------------------------------------------------

def _read_state() -> dict | None:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("port"), int) else None


def _reach(host: str | None) -> str:
    """Where this machine reaches a server bound to ``host`` (a wildcard bind on its loopback)."""
    if not host or host == "0.0.0.0":
        return DEFAULT_HOST
    return "::1" if host == "::" else host


def _request(state: dict, method: str, path: str, timeout: float = 5.0) -> tuple[int, dict]:
    import http.client

    token, _ = load_token()
    conn = http.client.HTTPConnection(_reach(state.get("host")), state["port"], timeout=timeout)
    try:
        conn.request(method, path, headers={"Authorization": f"Bearer {token}"})
        r = conn.getresponse()
        body = r.read()
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = {}
        return r.status, data if isinstance(data, dict) else {}
    finally:
        conn.close()


def _token_mismatch(state: dict) -> dict:
    pid = state.get("pid")
    end = f"taskkill /PID {pid} /T /F" if os.name == "nt" else f"kill {pid}"
    return {"running": True, "token_mismatch": True, "url": state.get("url"), "pid": pid,
            "state_file": str(state_path()),
            "why": f"the server on port {state['port']} refuses the current token (it was rotated after the server "
                   "started)",
            "hint": f"end the server's process ({end}), then `verinoda mcp daemon start` again"}


def daemon_status() -> dict:
    state = _read_state()
    if state is None:
        return {"running": False, "state_file": str(state_path())}
    try:
        code, data = _request(state, "GET", "/verinoda/status")
    except OSError as exc:
        return {"running": False, "stale_state": True, "state_file": str(state_path()), "url": state.get("url"),
                "why": f"no answer on port {state['port']}: {type(exc).__name__}",
                "hint": "`verinoda mcp daemon stop` removes the state file"}
    if code == 401:  # a server answers on the port but refuses the current token: kept, never forgotten
        return _token_mismatch(state)
    if code != 200:
        return {"running": False, "url": state.get("url"), "why": f"HTTP {code} from port {state['port']}",
                "hint": "another server holds the port"}
    return {"running": True, "url": state.get("url"), "log": state.get("log"), **data}


def daemon_start(serve_args: list[str], *, host: str, port: int) -> dict:
    """Start ``verinoda mcp serve ... --transport http`` in the background and wait until it listens."""
    current = daemon_status()
    if current.get("running"):
        return {"started": False, "already_running": True, **current}
    sp = state_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    try:
        sp.unlink()
    except OSError:
        pass
    load_token()  # created here, before the child needs it
    argv = [sys.executable, "-m", "verinoda", "mcp", "serve", *serve_args, "--transport", "http", "--host", host,
            "--port", str(port), "--state-file", str(sp)]
    kw: dict[str, Any] = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    with open(log_path(), "ab") as log:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=str(sp.parent),
                                close_fds=True, **kw)
    # the server writes the state file once it listens (its own pid: a venv's python.exe on Windows is a launcher
    # whose child is the server, so proc.pid is not it)
    deadline = time.monotonic() + START_WAIT
    while time.monotonic() < deadline:
        state = _read_state()
        if state:
            return {"started": True, **state}
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    if proc.poll() is None:  # never listened: do not leave it (or the launcher's child) running
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        else:
            proc.kill()
        proc.wait(10)
    tail = ""
    try:
        tail = log_path().read_text(encoding="utf-8", errors="replace")[-1500:]
    except OSError:
        pass
    return {"started": False, "exit_code": proc.returncode, "log": str(log_path()), "log_tail": tail}


def daemon_stop() -> dict:
    state = _read_state()
    if state is None:
        return {"stopped": False, "running": False, "state_file": str(state_path())}
    try:
        code, data = _request(state, "POST", "/verinoda/shutdown")
    except OSError:
        code, data = 0, {}
    if code == 401:  # still listening: the state file stays, so the server can still be found
        return {"stopped": False, **_token_mismatch(state)}
    if code != 200:
        try:
            state_path().unlink()
        except OSError:
            pass
        return {"stopped": False, "running": False, "stale_state_removed": True,
                "why": f"no Verinoda server answered on port {state['port']}"}
    deadline = time.monotonic() + STOP_WAIT
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((_reach(state.get("host")), state["port"]), timeout=0.5):
                pass
        except OSError:
            break
        time.sleep(0.2)
    else:
        return {"stopped": False, "running": True, "why": "the server did not close its port in time",
                "pid": data.get("pid")}
    if state_path().exists():
        try:
            if (_read_state() or {}).get("pid") == state.get("pid"):
                state_path().unlink()
        except OSError:
            pass
    return {"stopped": True, "pid": data.get("pid"), "url": state.get("url")}
