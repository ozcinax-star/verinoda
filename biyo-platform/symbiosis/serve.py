"""Symbiosis web sunucusu: web/ klasörünü ve şu uç noktaları sunar.

  GET /api/overview  ana ağ: tüm konular ve yalnızca kanıtlı (verified) kenarlar; bellekte bir kez hesaplanır.
  GET /api/graph?q=  arama: query.py'nin SPEC JSON'u (kanıtlı, çıkarım ve bilinmeyen kenarlar).
  GET /api/topics    arama önerileri için konu listesi.

Yalnızca standart kütüphane. Çalıştırma: `python serve.py`, sonra http://localhost:8000.
Veri sağlayıcılar bu dosyada iki fonksiyondur (get_graph, get_overview). EBA'ya bağlanırken yalnızca
onlar değişir; tarayıcı tarafı aynı JSON şekillerini bekler.
"""

import json
import subprocess
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import query as query_mod

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
QUERY = HERE / "query.py"
TOPICS = HERE / "kb" / "corpus" / "topics.json"
PORT = 8000
MAX_QUERY_LEN = 100
QUERY_TIMEOUT_S = 30

_overview = None
_overview_lock = threading.Lock()


def get_graph(q):
    """Veri sağlayıcı (EBA seam): arama metnini query.py'ye verir, SPEC JSON nesnesi döner."""
    proc = subprocess.run(
        [sys.executable, str(QUERY), q],
        capture_output=True,
        timeout=QUERY_TIMEOUT_S,
        cwd=str(HERE),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip() or "query.py hata verdi")
    return json.loads(proc.stdout.decode("utf-8"))


def get_overview():
    """Ana ağ verisi. İlk istekte hesaplanır, sonra bellekten döner (her konu için query.py bir kez çalışır)."""
    global _overview
    with _overview_lock:
        if _overview is None:
            _overview = build_overview()
    return _overview


def build_overview():
    """Her konuyu merkez alıp query.build çalıştırır; her kenarın yalnızca kanıtlı olanını alır.

    Aynı kenar iki konunun sorgusunda da çıkabilir: ilişki türü ve yön (önkoşul) anahtarıyla tekilleştirilir.
    Çıkarım ve bilinmeyen kenarlar ana ağa girmez; bu, ana ekranın kanıtlı ilişkileri göstermesi kuralıdır.
    """
    topics = query_mod.load_topics()
    nodes, edges, seen = [], [], set()
    for t in topics:
        result = query_mod.build(t["id"], topics)
        center = next((n for n in result["nodes"] if n["id"] == t["id"]), None)
        if center is None:
            continue
        nodes.append(center)
        for e in result["edges"]:
            if e["status"] != "verified":
                continue
            pair = (e["source"], e["target"]) if e["type"] == "onkosul" else tuple(sorted((e["source"], e["target"])))
            key = (e["type"],) + pair
            if key in seen:
                continue
            seen.add(key)
            edges.append(e)
    return {
        "nodes": nodes,
        "edges": edges,
        "unknowns": [
            f"Ana ağda {len(nodes)} konu ve {len(edges)} kanıtlı ilişki var. Çıkarım ve bilinmeyen ilişkiler "
            "arama sonucunda gösterilir.",
            "Ortaokul fen bilimi konuları henüz yok. Bu ağda yalnızca lise biyolojisi konuları vardır.",
        ],
    }


def get_topics():
    """Ana ağdaki ve arama önerilerindeki konular: corpus/topics.json (query.py ile aynı kaynak)."""
    topics = json.loads(TOPICS.read_text(encoding="utf-8"))
    return [{"id": t["id"], "title": t["title"], "grades": t["grades"]} for t in topics]


class Handler(SimpleHTTPRequestHandler):
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
    }

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/graph":
            self.api_graph(url.query)
            return
        if url.path == "/api/overview":
            self.api_overview()
            return
        if url.path == "/api/topics":
            self.api_topics()
            return
        super().do_GET()

    def api_topics(self):
        try:
            data = get_topics()
        except (OSError, ValueError, KeyError) as exc:
            self.send_json(500, {"error": "topics_failed", "message": f"Konu listesi okunamadı: {exc}"})
            return
        self.send_json(200, data)

    def api_overview(self):
        try:
            data = get_overview()
        except (OSError, ValueError, KeyError) as exc:
            self.send_json(500, {"error": "overview_failed", "message": f"Ana ağ hazırlanamadı: {exc}"})
            return
        self.send_json(200, data)

    def api_graph(self, raw_query):
        q = parse_qs(raw_query).get("q", [""])[0].strip()
        if len(q) > MAX_QUERY_LEN:
            self.send_json(400, {"error": "q_too_long", "message": f"Arama en fazla {MAX_QUERY_LEN} karakter olabilir."})
            return
        try:
            data = get_graph(q)
        except subprocess.TimeoutExpired:
            self.send_json(504, {"error": "timeout", "message": "Arama zaman aşımına uğradı."})
            return
        except (RuntimeError, ValueError) as exc:
            self.send_json(500, {"error": "query_failed", "message": str(exc)})
            return
        self.send_json(200, data)

    def send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def warm_overview():
    """Ana ağı açılışta arka planda hesaplar; hata olursa istek anında yeniden denenir."""
    try:
        get_overview()
    except (OSError, ValueError, KeyError):
        pass


def main():
    threading.Thread(target=warm_overview, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), partial(Handler, directory=str(WEB)))
    print(f"Symbiosis: http://localhost:{PORT}  (durdurmak için Ctrl+C)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
