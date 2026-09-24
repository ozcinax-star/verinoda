"""The local server behind `verinoda ui` (standard library only).

It serves one page (``static/``) and a JSON API over :class:`verinoda.ui.data.Atlas`.
It listens on 127.0.0.1 only and answers only requests addressed to that host and port (a
page elsewhere cannot reach it through DNS rebinding). The page loads nothing from outside
(``Content-Security-Policy``): no CDN, no fonts, no telemetry.

The one thing it writes is your own notes (``POST /api/usernote``, :mod:`verinoda.usernotes`),
and only for a request that carries the token of this server run (a random value put into the
page it serves, which a page of another site cannot read), as JSON, from this origin. Nothing
else is written; ``read_only`` (``verinoda ui --read-only``) turns writing off.
"""

from __future__ import annotations

import hmac
import json
import secrets
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


MAX_BODY = 128 * 1024
MAX_DRAIN = 1024 * 1024   # a refused body up to this size is read before the answer
TOKEN_META = '<meta name="verinoda-token" content="">'


def make_handler(atlas: Atlas, port: list[int], token: str = ""):
    """The request handler bound to ``atlas``; ``port`` holds the listening port once known.
    ``token`` (empty: read-only) is what a write must carry in ``X-Verinoda-Token``."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "verinoda-ui"
        sys_version = ""

        def log_message(self, fmt: str, *args) -> None:  # quiet: the terminal shows the URL only
            return

        def _host_ok(self) -> bool:  # a browser leaves the default port out of Host: _hosts knows
            return (self.headers.get("Host") or "").lower() in self._hosts()

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

        do_PUT = do_DELETE = do_PATCH = _refuse

        def _reject(self, status: int, obj: dict, length: int) -> None:
            """Answer an error after reading what the client sends (up to MAX_DRAIN): a socket closed
            with unread data is reset on Windows and the client never sees the answer. More than that
            is not read; the connection closes."""
            if 0 < length <= MAX_DRAIN:
                try:
                    self.rfile.read(length)
                except OSError:
                    pass
            else:
                self.close_connection = True
            self._json(status, obj)

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = -1
            if not self._host_ok():
                self._reject(HTTPStatus.FORBIDDEN, {"error": "host", "message": "wrong host"}, length)
                return
            if urlparse(self.path).path != "/api/usernote" or not token:
                self._reject(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read_only", "message": "only GET and HEAD"},
                             length)
                return
            origin = self.headers.get("Origin")
            if origin is not None and origin.lower() not in {f"http://{h}" for h in self._hosts()}:
                self._reject(HTTPStatus.FORBIDDEN, {"error": "origin", "message": "a write from another origin"}, length)
                return
            sent = self.headers.get("X-Verinoda-Token") or ""
            if not hmac.compare_digest(sent.encode("utf-8"), token.encode("utf-8")):
                self._reject(HTTPStatus.FORBIDDEN, {"error": "token", "message": "reload the page to write"}, length)
                return
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                self._reject(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "type", "message": "JSON only"}, length)
                return
            if not 0 < length <= MAX_BODY:
                self._reject(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "size", "message": "body too large or empty"},
                             length)
                return
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(body, dict):
                    raise TypeError("a JSON object")
                if body.get("delete") is True:  # by subject: a note whose code is gone has no note id left
                    subject = body["subject"]
                    if not isinstance(subject, str):
                        raise TypeError("subject is a string")
                else:
                    nid, text, keep = body["id"], body.get("text", ""), bool(body.get("keep", False))
                    if not isinstance(nid, str) or not isinstance(text, str):
                        raise TypeError("id and text are strings")
            except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "message": str(exc)[:200]})
                return
            try:
                obj = atlas.delete_user_note(subject) if body.get("delete") is True \
                    else atlas.write_user_note(nid, text, keep=keep)
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": str(exc.args[0] if exc.args else exc)})
                return
            except LookupError as exc:  # the file changed since the index was built
                self._json(HTTPStatus.CONFLICT, {"error": "stale_index", "message": str(exc)})
                return
            except (ValueError, OSError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "message": str(exc)[:300]})
                return
            self._json(HTTPStatus.OK, obj)

        def _hosts(self) -> set[str]:
            hosts = {f"127.0.0.1:{port[0]}", f"localhost:{port[0]}", f"[::1]:{port[0]}"}
            if port[0] == 80:
                hosts |= {"127.0.0.1", "localhost", "[::1]"}
            return hosts

        def _get(self, *, head: bool) -> None:
            if not self._host_ok():
                self._json(HTTPStatus.FORBIDDEN, {"error": "host", "message": "requests must be addressed to "
                                                  f"127.0.0.1:{port[0]} or localhost:{port[0]}"}, head=head)
                return
            url = urlparse(self.path)
            if url.path in STATIC:
                name, ctype = STATIC[url.path]
                body = _static(name)
                if name == "index.html" and token:  # the page carries this run's write token
                    body = body.replace(TOKEN_META.encode(), f'<meta name="verinoda-token" content="{token}">'.encode())
                self._send(HTTPStatus.OK, body, ctype, head=head)
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
                elif route == "usernotes":
                    obj = atlas.user_notes()
                elif route == "impact":
                    obj = atlas.impact(nid, int((qs.get("depth") or ["3"])[0] or 3), tests=_flag(qs, "tests", True))
                elif route == "changes":
                    obj = atlas.changes()
                elif route == "answer":
                    obj = atlas.answer((qs.get("q") or [""])[0])
                elif route == "path":
                    obj = atlas.path((qs.get("from") or [""])[0], (qs.get("to") or [""])[0])
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


def start(repo: Path | str, *, port: int = 0, read_only: bool = False) -> tuple[ThreadingHTTPServer, str]:
    """Load the project and start serving in a background thread; returns (server, url)."""
    atlas = Atlas(repo)
    atlas.ensure()  # a missing index fails here, before a browser opens
    holder = [0]
    token = "" if read_only else secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(atlas, holder, token))
    server.daemon_threads = True
    holder[0] = server.server_address[1]
    threading.Thread(target=server.serve_forever, name="verinoda-ui", daemon=True).start()
    return server, f"http://127.0.0.1:{holder[0]}/"


def serve(repo: Path | str, *, port: int = 0, open_browser: bool = True, open_at: str = "",
          read_only: bool = False) -> None:
    """`verinoda ui`: serve until Ctrl+C; ``open_at`` is the page to open first (``#/graph``)."""
    server, url = start(repo, port=port, read_only=read_only)
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
