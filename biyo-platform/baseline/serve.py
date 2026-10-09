"""Serves web/, GET /api/graph?q=..., GET /api/overview for the baseline prototype (stdlib only).

Run: python serve.py, then open http://localhost:8001
"""
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent
WEB = BASE / "web"
QUERY = BASE / "query.py"
TOPICS = BASE.parent / "corpus" / "topics.json"
PORT = 8001
MAX_QUERY_CHARS = 200
OVERVIEW_WORKERS = 4

_overview = None
_overview_lock = threading.Lock()


class DataError(Exception):
    pass


def load_topics():
    """Every topic of the corpus, without relations, from the same file query.py reads."""
    try:
        topics = json.loads(TOPICS.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DataError("Konu listesi okunamadı.") from exc
    return [{"id": t["id"], "title": t["title"], "grades": t["grades"]} for t in topics]


def load_graph(query):
    """The data provider behind /api/graph and /api/overview (the EBA seam).

    Only this function knows where the graph comes from. EBA can replace it with a call to its own
    service and keep the handlers and the page as they are; the returned dict must keep the SPEC shape.
    """
    try:
        done = subprocess.run(
            [sys.executable, str(QUERY), query],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DataError("Sorgu zaman aşımına uğradı.") from exc
    if done.returncode != 0:
        raise DataError("query.py hata verdi: " + done.stderr.decode("utf-8", "replace").strip()[-500:])
    try:
        return json.loads(done.stdout.decode("utf-8"))
    except ValueError as exc:
        raise DataError("query.py geçersiz JSON üretti.") from exc


def ask_topic(topic_id):
    """Asks query.py for one topic as if a student had searched for it.

    Returns (topic_id, payload, problem). A topic whose own query does not come back as its centre is
    reported, not used, so a wrong match cannot add an edge to the landing network.
    """
    try:
        payload = load_graph(topic_id)
    except DataError as exc:
        return topic_id, None, str(exc)
    if payload.get("center") != topic_id:
        return topic_id, None, "kendi sorgusunda merkez olarak bulunamadı"
    return topic_id, payload, None


def build_overview():
    """The landing network: every topic, and each verified edge once.

    Edges that are only inference or unknown are left out here; they appear on the topic page, with
    their status, where the student sees the evidence next to them.
    """
    topics = load_topics()
    with ThreadPoolExecutor(max_workers=OVERVIEW_WORKERS) as pool:
        results = list(pool.map(ask_topic, [t["id"] for t in topics]))
    edges = {}
    unknowns = []
    for topic_id, payload, problem in results:
        if problem:
            unknowns.append(f"{topic_id}: {problem}")
            continue
        for edge in payload.get("edges", []):
            if edge.get("status") != "verified":
                continue
            key = tuple(sorted((edge["source"], edge["target"])))
            edges.setdefault(key, {"source": edge["source"], "target": edge["target"], "type": edge["type"],
                                   "status": "verified"})
    connected = {node for key in edges for node in key}
    isolated = [t["id"] for t in topics if t["id"] not in connected]
    if isolated:
        unknowns.append("Doğrulanmış ilişkisi olmayan konular: " + ", ".join(isolated))
    unknowns.append("Ortaokul fen bilgisi konuları bu sürümde yok; yalnızca lise 9-12 konuları gösterilir.")
    return {"nodes": topics, "edges": [edges[k] for k in sorted(edges)], "unknowns": unknowns}


def get_overview():
    """Computed once per server run and kept in memory; a request during the first run waits for it."""
    global _overview
    with _overview_lock:
        if _overview is None:
            _overview = build_overview()
        return _overview


def warm_overview():
    try:
        get_overview()
    except DataError as exc:
        print(f"Ana ekran ağı hazırlanamadı: {exc}", file=sys.stderr)


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/graph":
            self.handle_graph(url.query)
            return
        if url.path == "/api/overview":
            self.handle_overview()
            return
        super().do_GET()

    def handle_graph(self, raw_query):
        query = parse_qs(raw_query).get("q", [""])[0][:MAX_QUERY_CHARS]
        try:
            payload = load_graph(query)
        except DataError as exc:
            self.send_json(500, {"error": str(exc)})
            return
        self.send_json(200, payload)

    def handle_overview(self):
        try:
            payload = get_overview()
        except DataError as exc:
            self.send_json(500, {"error": str(exc)})
            return
        self.send_json(200, payload)

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    threading.Thread(target=warm_overview, daemon=True).start()
    handler = partial(Handler, directory=str(WEB))
    server = ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    print(f"Open http://localhost:{PORT} (Ctrl+C stops the server)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
