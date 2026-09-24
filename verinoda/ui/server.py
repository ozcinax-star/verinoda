"""The local server behind `verinoda ui` (standard library only).

It serves one page (``static/``) and a read-only JSON API over :class:`verinoda.ui.data.Atlas`.
It listens on 127.0.0.1 only and answers only requests addressed to that host and port (a
page elsewhere cannot reach it through DNS rebinding). Only GET and HEAD; nothing is written.
The page loads nothing from outside (``Content-Security-Policy``): no CDN, no fonts, no
telemetry.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from verinoda.ui.data import GRAPH_RELATIONS, Atlas

STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8")}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def _static(name: str) -> bytes:
    return resources.files("verinoda.ui").joinpath("static", name).read_bytes()


def _flag(qs: dict, key: str, default: bool) -> bool:
    v = (qs.get(key) or [None])[0]
    return default if v is None else v not in ("0", "false", "no", "")


def _relations(qs: dict) -> set[str] | None:
    raw = (qs.get("rel") or [""])[0]
    rels = {r for r in raw.split(",") if r in GRAPH_RELATIONS}
    return rels or None


def make_handler(atlas: Atlas, port: list[int]):
    """The request handler bound to ``atlas``; ``port`` holds the listening port once known."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "verinoda-ui"
        sys_version = ""

        def log_message(self, fmt: str, *args) -> None:  # quiet: the terminal shows the URL only
            return

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            ok = {f"127.0.0.1:{port[0]}", f"localhost:{port[0]}", f"[::1]:{port[0]}"}
            if port[0] == 80:  # a browser leaves the default port out of Host
                ok |= {"127.0.0.1", "localhost", "[::1]"}
            return host in ok

        def _send(self, status: int, body: bytes, ctype: str, *, head: bool = False) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def _json(self, status: int, obj, *, head: bool = False) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", head=head)

        def do_HEAD(self) -> None:  # http.server naming
            self._get(head=True)

        def do_GET(self) -> None:
            self._get(head=False)

        def _refuse(self) -> None:
            self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read_only", "message": "only GET and HEAD"})

        do_POST = do_PUT = do_DELETE = do_PATCH = _refuse

        def _get(self, *, head: bool) -> None:
            if not self._host_ok():
                self._json(HTTPStatus.FORBIDDEN, {"error": "host", "message": "requests must be addressed to "
                                                  f"127.0.0.1:{port[0]} or localhost:{port[0]}"}, head=head)
                return
            url = urlparse(self.path)
            if url.path in STATIC:
                name, ctype = STATIC[url.path]
                self._send(HTTPStatus.OK, _static(name), ctype, head=head)
                return
            if not url.path.startswith("/api/"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": url.path}, head=head)
                return
            qs = parse_qs(url.query)
            nid = (qs.get("id") or [""])[0]
            try:
                route = url.path[len("/api/"):]
                if route == "stats":
                    obj = atlas.stats()
                elif route == "tree":
                    obj = atlas.tree()
                elif route == "search":
                    obj = atlas.search((qs.get("q") or [""])[0])
                elif route == "note":
                    obj = atlas.note(nid)
                elif route == "local":
                    obj = atlas.local_graph(nid, int((qs.get("depth") or ["1"])[0] or 1), _relations(qs),
                                            tests=_flag(qs, "tests", True), external=_flag(qs, "external", False),
                                            data=_flag(qs, "data", True))
                elif route == "global":
                    obj = atlas.global_graph(tests=_flag(qs, "tests", True), data=_flag(qs, "data", True),
                                             relations=_relations(qs))
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": url.path}, head=head)
                    return
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": str(exc.args[0] if exc.args
                                                                                        else exc)}, head=head)
                return
            except FileNotFoundError as exc:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "no_index", "message": str(exc)}, head=head)
                return
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "message": str(exc)}, head=head)
                return
            except Exception as exc:  # noqa: BLE001 - one failing request never stops the server
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR,
                           {"error": "internal_error", "message": f"{type(exc).__name__}: {exc}"[:400]}, head=head)
                return
            self._json(HTTPStatus.OK, obj, head=head)

    return Handler


def start(repo: Path | str, *, port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Load the project and start serving in a background thread; returns (server, url)."""
    atlas = Atlas(repo)
    atlas.ensure()  # a missing index fails here, before a browser opens
    holder = [0]
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(atlas, holder))
    server.daemon_threads = True
    holder[0] = server.server_address[1]
    threading.Thread(target=server.serve_forever, name="verinoda-ui", daemon=True).start()
    return server, f"http://127.0.0.1:{holder[0]}/"


def serve(repo: Path | str, *, port: int = 0, open_browser: bool = True, open_at: str = "") -> None:
    """`verinoda ui`: serve until Ctrl+C; ``open_at`` is the page to open first (``#/graph``)."""
    server, url = start(repo, port=port)
    url += open_at
    print(f"Verinoda notes and graph for {Path(repo).resolve()}: {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001 - the URL is printed; opening a browser is a convenience
            print(f"could not open a browser ({type(exc).__name__}); open the address above", file=sys.stderr)
    try:
        while True:  # a short sleep, not Event.wait(): Ctrl+C interrupts it on Windows too
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
    finally:
        server.shutdown()
        server.server_close()
