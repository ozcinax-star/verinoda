"""Local server for the baseline prototype (Python stdlib only).

    python serve.py              -> http://localhost:8000
    python serve.py --port 8080 --host 0.0.0.0   (to reach it from other devices on the LAN)

Routes:
    GET /api/graph?q=<arama>   JSON from query.build_graph (the SPEC output contract)
    GET /api/health            {"ok": true}
    GET /...                   static files from web/
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
sys.path.insert(0, HERE)

import query  # noqa: E402  (local module next to this file)


def get_graph(q: str) -> dict:
    """The data provider seam. The frontend only knows /api/graph; to serve EBA content later,
    replace the body of this function (or query.load_corpus) and keep the returned shape."""
    return query.build_graph(q)


class Handler(SimpleHTTPRequestHandler):
    # Windows can map .js to text/plain through the registry; browsers then refuse the script.
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def do_GET(self):  # noqa: N802 (http.server naming)
        url = urlparse(self.path)
        if url.path == "/api/graph":
            q = parse_qs(url.query).get("q", [""])[0][:200]
            try:
                payload, status = get_graph(q), 200
            except Exception as exc:  # report, do not drop the connection
                payload = {"query": q, "center": None, "nodes": [], "edges": [],
                           "unknowns": [f"Sunucu hatası: {type(exc).__name__}"]}
                status = 500
            return self._json(payload, status)
        if url.path == "/api/health":
            return self._json({"ok": True}, 200)
        return super().do_GET()

    def _json(self, payload: dict, status: int):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("[serve] " + (fmt % args) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Biyoloji öğrenme platformu (baseline) yerel sunucu")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    # A console code page without Turkish letters must not crash the server on a print.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    query.load_corpus()  # fail fast if the corpus is missing
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    shown = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    print(f"Sunucu hazır: http://{shown}:{args.port}  (durdurmak için Ctrl+C)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
