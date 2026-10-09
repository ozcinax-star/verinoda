"""Local server for the topic graph: python serve.py, then open http://localhost:8000.

Serves web/ (index.html, style.css, app.js) and GET /api/graph?q=... The data comes from one function,
fetch_graph(), which runs query.py. EBA can later replace fetch_graph() with another provider without
touching the frontend.
"""

from __future__ import annotations

import json
import subprocess
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
QUERY = HERE / "query.py"
PORT = 8000
QUERY_TIMEOUT_S = 60

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}


def fetch_graph(query: str) -> dict:
    """The only data provider. Runs query.py and returns its SPEC JSON object."""
    completed = subprocess.run(
        [sys.executable, str(QUERY), query],
        capture_output=True,
        timeout=QUERY_TIMEOUT_S,
        cwd=str(HERE),
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", "replace").strip() or "query.py failed")
    return json.loads(completed.stdout.decode("utf-8"))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/graph":
            self.handle_graph(parse_qs(url.query).get("q", [""])[0])
            return
        if url.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def handle_graph(self, query: str):
        try:
            body = fetch_graph(query.strip())
            status = 200
        except subprocess.TimeoutExpired:
            body = {"error": "Arama zaman aşımına uğradı."}
            status = 504
        except (RuntimeError, ValueError) as exc:
            body = {"error": f"Arama başarısız: {exc}"}
            status = 500
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def guess_type(self, path):
        suffix = Path(path).suffix.lower()
        return CONTENT_TYPES.get(suffix, super().guess_type(path))

    def log_message(self, format, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Biyoloji konu haritası: http://localhost:{PORT}  (durdurmak için Ctrl+C)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
