"""A fake language server for the tests of verinoda/lsp.py: it speaks LSP over stdio and answers from a table.

    python fake_lsp.py TABLE.json

The table (JSON) holds:

``name``, ``version``   the serverInfo it reports
``mode``                ok (default) | crash (exits on the first query) | hang (never answers a query, ignores
                        shutdown and exit) | garbage (answers a query with bytes that are not LSP) |
                        crash_on_init | hang_on_init
``capabilities``        replaces the advertised capabilities (default: every provider used by the client)
``definition``, ``references``, ``implementation``, ``hover``, ``calls``, ``types``
                        answers keyed by ``path:line:col`` (repository-relative, 1-based, UTF-16 column + 1) or
                        ``path:line``; a location is ``{"path", "line", "col"}`` (``"link": true`` answers a
                        LocationLink, ``"abs"`` an absolute path outside the root)
``diagnostics``         ``{path: [{"line", "col", "message", "severity"}]}`` published when the file is opened
``log``                 a file each received method is appended to (one per line)
``pidfile``             a file the server writes its process id to
``child``               start a sleeping child process (its id goes to ``pidfile`` + ".child"); it inherits the
                        server's stdout, so the client never sees that pipe end while the child runs
``uri_style``           ``vscode``: publish diagnostics under vscode-uri's spelling of the file's URI
                        (``file:///c%3A/...``)
``bad_params``          after ``initialized``, also send a ``workspace/configuration`` request and a
                        ``publishDiagnostics`` whose ``params`` is an array

Standard library only; nothing is installed or reached over a network.
"""

import json
import os
import subprocess
import sys
import time
from urllib.parse import quote, unquote, urlparse
from urllib.request import url2pathname

TABLE = json.load(open(sys.argv[1], encoding="utf-8"))
MODE = TABLE.get("mode", "ok")
ROOT = None
IN = sys.stdin.buffer
OUT = sys.stdout.buffer

if TABLE.get("pidfile"):
    with open(TABLE["pidfile"], "w") as f:
        f.write(str(os.getpid()))
if TABLE.get("child"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(TABLE["pidfile"] + ".child", "w") as f:
        f.write(str(child.pid))


def log(method):
    if TABLE.get("log"):
        with open(TABLE["log"], "a", encoding="utf-8") as f:
            f.write(method + "\n")


def read():
    headers = {}
    while True:
        line = IN.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        k, _, v = line.decode("ascii").partition(":")
        headers[k.strip().lower()] = v.strip()
    n = int(headers["content-length"])
    return json.loads(IN.read(n).decode("utf-8"))


def send(msg):
    body = json.dumps(msg).encode("utf-8")
    OUT.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    OUT.flush()


def path_of(uri):
    p = url2pathname(unquote(urlparse(uri).path))
    if os.name == "nt" and p.startswith("\\") and len(p) > 2 and p[2] == ":":
        p = p[1:]
    return p


def uri_of(path):
    return "file:///" + os.path.abspath(path).replace("\\", "/").lstrip("/")


def rel_of(uri):
    return os.path.relpath(path_of(uri), ROOT).replace("\\", "/")


def key_of(params):
    rel = rel_of(params["textDocument"]["uri"])
    pos = params["position"]
    return [f"{rel}:{pos['line'] + 1}:{pos['character'] + 1}", f"{rel}:{pos['line'] + 1}"]


def look(section, params):
    table = TABLE.get(section) or {}
    for k in key_of(params):
        if k in table:
            return table[k], k
    return None, None


def rng(line, col, end_line=None):
    return {"start": {"line": line - 1, "character": col - 1},
            "end": {"line": (end_line or line) - 1, "character": col + 2}}


def location(loc):
    path = loc["abs"] if loc.get("abs") else os.path.join(ROOT, loc["path"])
    r = rng(loc["line"], loc.get("col", 1))
    if loc.get("link"):
        full = rng(loc.get("span", [loc["line"]])[0], 1, loc.get("span", [loc["line"], loc["line"]])[-1])
        return {"targetUri": uri_of(path), "targetRange": full, "targetSelectionRange": r}
    return {"uri": uri_of(path), "range": r}


def item(it, data=None):
    path = os.path.join(ROOT, it["path"])
    r = rng(it["line"], it.get("col", 1))
    return {"name": it["name"], "kind": it.get("kind", 12), "uri": uri_of(path), "range": r,
            "selectionRange": r, "data": data}


CAPS = {"definitionProvider": True, "referencesProvider": True, "hoverProvider": True,
        "implementationProvider": True, "callHierarchyProvider": True, "typeHierarchyProvider": True,
        "textDocumentSync": 1}


def answer(method, params):
    if method == "textDocument/definition":
        locs, _ = look("definition", params)
        return None if locs is None else [location(x) for x in locs]
    if method == "textDocument/references":
        locs, _ = look("references", params)
        return [location(x) for x in locs or []]
    if method == "textDocument/implementation":
        locs, _ = look("implementation", params)
        return [location(x) for x in locs or []]
    if method == "textDocument/hover":
        text, _ = look("hover", params)
        return None if text is None else {"contents": {"kind": "markdown", "value": text}}
    if method == "textDocument/prepareCallHierarchy":
        entry, k = look("calls", params)
        return None if entry is None else [item(entry["item"], k)]
    if method in ("callHierarchy/outgoingCalls", "callHierarchy/incomingCalls"):
        entry = TABLE["calls"][params["item"]["data"]]
        out = []
        for c in entry["outgoing" if method.endswith("outgoingCalls") else "incoming"]:
            out.append({"to" if method.endswith("outgoingCalls") else "from": item(c),
                        "fromRanges": [rng(ln, col) for ln, col in c.get("from", [])]})
        return out
    if method == "textDocument/prepareTypeHierarchy":
        entry, k = look("types", params)
        return None if entry is None else [item(entry["item"], k)]
    if method in ("typeHierarchy/supertypes", "typeHierarchy/subtypes"):
        entry = TABLE["types"][params["item"]["data"]]
        return [item(c) for c in entry["supertypes" if method.endswith("supertypes") else "subtypes"]]
    raise KeyError(method)


def main():
    global ROOT
    while True:
        msg = read()
        if msg is None:
            return 0
        method = msg.get("method")
        if method is None:          # a response to our own request (workspace/configuration)
            log("response")
            continue
        log(method)
        if method == "initialize":
            if MODE == "crash_on_init":
                return 3
            if MODE == "hang_on_init":
                time.sleep(120)
            p = msg["params"]
            ROOT = path_of(p["rootUri"])
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "capabilities": TABLE.get("capabilities", CAPS),
                "serverInfo": {"name": TABLE.get("name", "fake-ls"), "version": TABLE.get("version", "0.1")}}})
        elif method == "initialized":
            # a request from the server: the client must answer it
            send({"jsonrpc": "2.0", "id": "cfg-1", "method": "workspace/configuration",
                  "params": {"items": [{"section": "fake"}]}})
            send({"jsonrpc": "2.0", "method": "window/logMessage", "params": {"type": 3, "message": "ready"}})
            if TABLE.get("bad_params"):   # JSON-RPC allows array params; the client must not choke on them
                send({"jsonrpc": "2.0", "id": "cfg-2", "method": "workspace/configuration", "params": [1, 2]})
                send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": ["x"]})
        elif method == "textDocument/didOpen":
            td = msg["params"]["textDocument"]
            rel = rel_of(td["uri"])
            diags = [{"range": rng(d["line"], d.get("col", 1)), "message": d["message"],
                      "severity": d.get("severity", 1), "source": "fake"}
                     for d in (TABLE.get("diagnostics") or {}).get(rel, [])]
            uri = td["uri"]
            if TABLE.get("uri_style") == "vscode":   # vscode-uri's spelling: lower-case drive, ':' encoded
                p = os.path.abspath(path_of(uri)).replace("\\", "/")
                if len(p) > 1 and p[1] == ":":
                    p = "/" + p[0].lower() + p[1:]
                uri = "file://" + quote(p, safe="/")
            send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                  "params": {"uri": uri, "diagnostics": diags}})
        elif method == "shutdown":
            if MODE == "hang":
                continue
            send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
        elif method == "exit":
            if MODE == "hang":
                continue
            return 0
        elif "id" in msg:
            if MODE == "crash":
                return 7
            if MODE == "hang":
                continue
            if MODE == "garbage":
                OUT.write(b"this is not a language server\r\n\r\n{]")
                OUT.flush()
                continue
            try:
                send({"jsonrpc": "2.0", "id": msg["id"], "result": answer(method, msg.get("params") or {})})
            except KeyError:
                send({"jsonrpc": "2.0", "id": msg["id"],
                      "error": {"code": -32601, "message": f"unhandled method {method}"}})


if __name__ == "__main__":
    sys.exit(main())
