"""Yerel sunucu: web/ klasöründeki dosyaları ve /api/graph?q=... uç noktasını sunar.

Çalıştırma: python serve.py   (varsayılan adres http://localhost:8000)
Yalnızca Python standart kütüphanesi kullanır.
"""
import argparse
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from query import build_graph

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


def graph_provider(query):
    """Veri sağlayıcı tek noktası. EBA entegrasyonunda yalnızca bu fonksiyon değiştirilecek."""
    return build_graph(query)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/graph":
            self.handle_graph(parse_qs(parsed.query).get("q", [""])[0])
        else:
            self.handle_static(unquote(parsed.path))

    def handle_graph(self, query):
        if not query.strip():
            self.send_json(400, {"error": "q parametresi boş", "nodes": [], "edges": []})
            return
        self.send_json(200, graph_provider(query))

    def handle_static(self, path):
        rel = path.lstrip("/") or "index.html"
        target = (WEB / rel).resolve()
        if WEB.resolve() not in target.parents or not target.is_file():
            self.send_error(404, "Not found")
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(target.suffix, mimetypes.guess_type(str(target))[0] or "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("%s %s" % (self.address_string(), fmt % args))


def main():
    parser = argparse.ArgumentParser(description="Biyoloji Öğrenme Platformu (baseline) sunucusu")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Sunucu çalışıyor: http://localhost:{args.port}  (durdurmak için Ctrl+C)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
