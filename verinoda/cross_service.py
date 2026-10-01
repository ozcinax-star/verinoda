"""Cross-service edges: a client call linked to the handler that serves it.

A frontend's ``fetch(`/api/users/${id}`)`` and a Flask ``@app.route("/api/users/<int:user_id>")`` are two
separate parts of the graph: no call edge joins them, so ``trace`` from the page to the handler found nothing.
Here route tables are read from the code (Flask, Quart, Sanic, FastAPI and Starlette routes, Flask blueprints and
FastAPI routers with their mount prefixes, Django URLconfs with ``include``, aiohttp, Express, Koa-router,
Fastify, Hono and similar ``app.get('/x', handler)`` routes with ``app.use('/prefix', router)``, NestJS
controllers, Next.js API files, Spring and JAX-RS annotations) and so are client calls with a URL the text spells
(``fetch``, ``axios`` and its ``axios.create({baseURL})`` instances, Angular's ``http``, ``requests``, ``httpx``,
test clients). A call whose path and method fit exactly one handler becomes a ``requests`` edge from the calling
function to the handler. tRPC procedures (router keys), gRPC methods (Python servicers and stubs) and GraphQL
root fields (resolver maps and ``gql`` documents) become ``rpc_calls`` edges; an event emitted by name and the
listeners of that name become ``emits`` edges.

Every edge is ``INFERRED`` with ``_origin=verinoda.cross_service``: it is read from two texts, never verified
(a base URL, a proxy, a gateway or a deployment can send the request elsewhere). Its location is the client
call; ``route_at`` is the route declaration. A call that fits several handlers gets no edge: it is reported as
ambiguous with its candidates (``verinoda routes``, grouped and capped unless ``--all``, and ``trace`` when such a
call lies on the way). A mount prefix is read where the text spells it, also through a constant or a pydantic
settings default of another module; one it does not spell keeps the route, marked ``prefix not resolved``.
"""
from __future__ import annotations

import ast
import bisect
import posixpath
import re
from collections import Counter, defaultdict

ORIGIN = "verinoda.cross_service"
HTTP_RELATION = "requests"
RPC_RELATION = "rpc_calls"
EVENT_RELATION = "emits"
RELATIONS = frozenset({HTTP_RELATION, RPC_RELATION, EVENT_RELATION})
# 4: a parameter or attribute store voids a value, same-file constants cited; 3: prefixes given by an expression
# (Python constants and settings defaults, JavaScript constants); 2: mount
# prefixes that replace, top-level constants only, per-class JVM prefixes
FACTS_VERSION = 4

PY_SUFFIXES = (".py",)
JS_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte")
JVM_SUFFIXES = (".java", ".kt")
SUFFIXES = PY_SUFFIXES + JS_SUFFIXES + JVM_SUFFIXES
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_VERBS = ("get", "post", "put", "patch", "delete", "head", "options")
REPORT_CAP = 200  # ambiguous calls kept in the sidecar
MAX_CHARS = 1_000_000  # a larger source file is generated code, not read

# event names every emitter library uses for its own purposes (streams, sockets, the DOM, processes): an `on`
# and an `emit` of one of these are most likely on unrelated objects, so they are not linked
GENERIC_EVENTS = frozenset("""
abort beforeExit blur change click close complete connect connection data disconnect disconnecting done drain
end error exit finish focus input keydown keyup line listening load message newListener offline online open
pipe progress readable ready removeListener request resize response scroll start stop submit success timeout
uncaughtException unhandledRejection unload unpipe update upgrade value warning SIGINT SIGTERM
""".split())


# -- shared text helpers ------------------------------------------------------------------------------

def _line_starts(text: str) -> list[int]:
    starts = [0]
    for m in re.finditer("\n", text):
        starts.append(m.end())
    return starts


def _line_of(starts: list[int], pos: int) -> int:
    return bisect.bisect_right(starts, pos)


_STRING_BODY = {"'": re.compile(r"'(?:[^'\\\n]|\\.)*"), '"': re.compile(r'"(?:[^"\\\n]|\\.)*')}
_TEMPLATE_STOP = re.compile(r"[\\`$]")


def _skip_string(text: str, i: int) -> int:
    """The index after the quoted string (``'`` or ``"``) starting at ``i``; a string ends at a newline too."""
    return _STRING_BODY[text[i]].match(text, i).end() + 1


def _skip_template(text: str, i: int) -> int:
    """The index after the JavaScript template literal starting at ``i`` (nested ``${...}`` followed)."""
    j, n = i + 1, len(text)
    while j < n:
        m = _TEMPLATE_STOP.search(text, j)
        if not m:
            return n
        j = m.start()
        c = text[j]
        if c == "\\":
            j += 2
        elif c == "`":
            return j + 1
        elif text.startswith("${", j):
            depth, j = 1, j + 2
            while j < n and depth:
                c = text[j]
                if c in "'\"":
                    j = _skip_string(text, j)
                    continue
                if c == "`":
                    j = _skip_template(text, j)
                    continue
                depth += (c == "{") - (c == "}")
                j += 1
        else:
            j += 1
    return n


_JS_SPECIAL = re.compile(r"['\"`/]")
_REGEX_BEFORE = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_AFTER_WORDS = frozenset({"return", "typeof", "case", "do", "else", "in", "of", "void", "yield", "await",
                                "delete", "throw", "new"})


def _regex_start(text: str, i: int) -> bool:
    """Whether the ``/`` at ``i`` (not a comment) starts a regular expression literal rather than a division: what
    precedes it is an operator, an opening bracket, a keyword such as ``return``, or nothing."""
    before = text[max(0, i - 200):i].rstrip()
    if not before:
        return True
    c = before[-1]
    if c in _REGEX_BEFORE:
        return True
    if c.isalpha():
        m = re.search(r"[A-Za-z_$][\w$]*$", before)
        return bool(m) and m.group(0) in _REGEX_AFTER_WORDS and not before[:m.start()].rstrip().endswith(".")
    return False


def _skip_regex(text: str, i: int) -> int:
    """The index after the regular expression literal starting at ``i`` (it ends at its line's end at the latest)."""
    j, n, in_class = i + 1, len(text), False
    while j < n and text[j] != "\n":
        ch = text[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        elif ch == "/" and not in_class:
            break
        j += 1
    return j + 1


def strip_js(text: str) -> str:
    """``text`` with JavaScript/TypeScript comments replaced by spaces (newlines kept, so lines and columns stay);
    strings, template literals and regular expression literals are kept as they are. Jumps from one quote or
    slash to the next, so a file costs about one regular-expression scan."""
    pieces: list[str] = []
    i, n, last = 0, len(text), 0
    while True:
        m = _JS_SPECIAL.search(text, i)
        if not m:
            break
        i = m.start()
        c = text[i]
        if c in "'\"":
            i = _skip_string(text, i)
        elif c == "`":
            i = _skip_template(text, i)
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            pieces += [text[last:i], " " * (j - i)]
            i = last = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            pieces += [text[last:i], re.sub(r"[^\n]", " ", text[i:j])]
            i = last = j
        else:
            i = _skip_regex(text, i) if _regex_start(text, i) else i + 1
    pieces.append(text[last:])
    return "".join(pieces)


_TOKEN = re.compile(r"['\"`/()\[\]{}]")
_OPENER = {")": "(", "]": "[", "}": "{"}
_SEP_RE = {sep: re.compile("[" + re.escape(sep) + r"'\"`/(\[{]") for sep in (",", "+")}


class _Tokens:
    """One linear pass over comment-free code: for every bracket the index of its partner, for every string,
    template and regular expression literal the index after it. A call's arguments are then found by lookups;
    scanning from each call to its closing bracket was quadratic on a file of unclosed or regex-heavy calls.
    A closing bracket of the wrong kind closes the nearest opener of its kind among the last eight (the openers
    between stay unclosed), or nothing."""

    def __init__(self, code: str, *, js: bool = True):
        self.code = code
        jump: dict[int, int] = {}
        stack: list[int] = []
        i = 0
        while True:
            m = _TOKEN.search(code, i)
            if not m:
                break
            i = m.start()
            c = code[i]
            if c in "'\"":
                e = _skip_string(code, i)
            elif c == "`" and js:
                e = _skip_template(code, i)
            elif c == "/" and js and _regex_start(code, i):
                e = _skip_regex(code, i)
            elif c in "([{":
                stack.append(i)
                i += 1
                continue
            elif c in ")]}":
                want = _OPENER[c]
                for k in range(len(stack) - 1, max(-1, len(stack) - 9), -1):
                    if code[stack[k]] == want:
                        jump[stack[k]] = i
                        del stack[k:]
                        break
                i += 1
                continue
            else:
                i += 1
                continue
            jump[i] = e
            i = e
        self.jump = jump

    def close(self, open_i: int) -> int:
        """Index of the bracket closing the one at ``open_i``; -1 when it is unclosed."""
        return self.jump.get(open_i, -1) if self.code[open_i:open_i + 1] in ("(", "[", "{") else -1

    def split(self, a: int, b: int, sep: str = ",") -> list[str]:
        """``code[a:b]`` split at ``sep`` outside brackets, strings, templates and regular expressions."""
        code, jump, pat = self.code, self.jump, _SEP_RE[sep]
        parts, start, i = [], a, a
        while True:
            m = pat.search(code, i, b)
            if not m:
                break
            p = m.start()
            c = code[p]
            if c == sep:
                if not (sep == "+" and (code[p + 1:p + 2] == "+" or code[p - 1:p] == "+")):
                    parts.append(code[start:p])
                    start = p + 1
                i = p + 1
            elif p in jump:
                i = jump[p] + 1 if c in "([{" else jump[p]
            else:
                i = p + 1
        parts.append(code[start:b])
        return parts

    def top_level(self, pos: int) -> bool:
        """Whether ``pos`` is outside every closed bracket pair (a module-level statement)."""
        spans = self.__dict__.get("_spans")
        if spans is None:
            spans, end = [], -1
            for k in sorted(self.jump):
                if k > end and self.code[k] in "([{":
                    spans.append((k, self.jump[k]))
                    end = self.jump[k]
            self._spans = spans
        i = bisect.bisect_right(spans, (pos, float("inf"))) - 1
        return i < 0 or not (spans[i][0] < pos < spans[i][1])

    def args(self, open_i: int) -> tuple[list[str], int] | None:
        """The top-level arguments of the call whose ``(`` is at ``open_i``, and the index of its ``)``."""
        close = self.close(open_i)
        if close < 0:
            return None
        if not self.code[open_i + 1:close].strip():
            return [], close
        return [a.strip() for a in self.split(open_i + 1, close) if a.strip()], close


_SCAN_LIMIT = 50_000  # how far _close looks for a closing bracket in a small text (a GraphQL document)
_SMALL = 4_000        # an argument longer than this is no URL or option object worth splitting


def _close(code: str, open_i: int) -> int:
    """Index of the bracket closing the one at ``open_i`` (strings and templates skipped); -1 when unclosed or
    further than ``_SCAN_LIMIT`` away. For small texts only: a file's code goes through :class:`_Tokens`."""
    depth, i, n = 0, open_i, min(len(code), open_i + _SCAN_LIMIT)
    while i < n:
        c = code[i]
        if c in "'\"":
            i = _skip_string(code, i)
            continue
        if c == "`":
            i = _skip_template(code, i)
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _split_top(s: str, sep: str = ",") -> list[str]:
    """``s`` split at ``sep`` outside brackets, strings and templates."""
    parts, depth, i, start, n = [], 0, 0, 0, len(s)
    while i < n:
        c = s[i]
        if c in "'\"":
            i = _skip_string(s, i)
            continue
        if c == "`":
            i = _skip_template(s, i)
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == sep and depth == 0 and not (sep == "+" and (s[i + 1:i + 2] == "+" or s[i - 1:i] == "+")):
            parts.append(s[start:i])
            start = i + 1
        i += 1
    parts.append(s[start:])
    return parts


_JS_LIT = re.compile(r"""^(?:'((?:[^'\\\n]|\\.)*)'|"((?:[^"\\\n]|\\.)*)")$""", re.S)


def _js_literal(s: str) -> str | None:
    m = _JS_LIT.match(s.strip())
    if not m:
        t = s.strip()
        if len(t) >= 2 and t[0] == t[-1] == "`" and "${" not in t:
            return t[1:-1]
        return None
    return m.group(1) if m.group(1) is not None else m.group(2)


def _template_pieces(body: str) -> list[list[str]]:
    out: list[list[str]] = []
    i, start, n = 0, 0, len(body)
    while i < n:
        if body[i] == "\\":
            i += 2
            continue
        if body.startswith("${", i):
            if i > start:
                out.append(["lit", body[start:i]])
            depth, j = 1, i + 2
            while j < n and depth:
                depth += (body[j] == "{") - (body[j] == "}")
                j += 1
            out.append(["dyn", body[i + 2:j - 1].strip()[:60]])
            i = start = j
            continue
        i += 1
    if start < n:
        out.append(["lit", body[start:]])
    return out


def js_pieces(expr: str, consts: dict[str, str]) -> list[list[str]] | None:
    """A URL expression as literal and dynamic pieces: ``'/a/' + id``, `` `/a/${id}` ``, a same-file constant."""
    pieces: list[list[str]] = []
    if len(expr) > _SMALL:
        return None
    for part in _split_top(expr.strip(), "+"):
        p = part.strip()
        if not p:
            return None
        lit = _js_literal(p)
        if lit is not None:
            pieces.append(["lit", lit])
        elif len(p) >= 2 and p[0] == p[-1] == "`":
            pieces += _template_pieces(p[1:-1])
        elif p in consts:
            pieces.append(["lit", consts[p]])
        else:
            pieces.append(["dyn", p[:60]])
    return pieces or None


def py_pieces(node: ast.AST, consts: dict[str, str]) -> list[list[str]] | None:
    """The same for a Python expression: a string, an f-string, ``a + b``, a module-level string constant."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [["lit", node.value]]
    if isinstance(node, ast.JoinedStr):
        out = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                out.append(["lit", v.value])
            elif (isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name) and v.value.id in consts
                  and v.format_spec is None and v.conversion == -1):
                out.append(["lit", consts[v.value.id]])
            else:
                out.append(["dyn", _dotted(getattr(v, "value", v)) or "{...}"])
        return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = py_pieces(node.left, consts), py_pieces(node.right, consts)
        return a + b if a is not None and b is not None else None
    if isinstance(node, ast.Name) and node.id in consts:
        return [["lit", consts[node.id]]]
    if isinstance(node, (ast.Name, ast.Attribute, ast.Call, ast.Subscript)):
        return [["dyn", (_dotted(node) or "expr")[:60]]]
    return None


def _dotted(node) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


# -- paths --------------------------------------------------------------------------------------------

_ABSOLUTE = re.compile(r"^(?:[A-Za-z][\w+.-]*:)?//")


def join_base(base: list[list[str]] | None, pieces: list[list[str]]) -> list[list[str]]:
    """A client's base URL and a call's URL as the clients join them (axios ``combineURLs``, httpx ``base_url``):
    an absolute URL keeps no base; otherwise the two are joined with exactly one ``/``."""
    if not base:
        return pieces
    if pieces and pieces[0][0] == "lit" and _ABSOLUTE.match(pieces[0][1]):
        return pieces
    b = [list(p) for p in base]
    u = [list(p) for p in pieces]
    if b[-1][0] == "lit":
        b[-1][1] = b[-1][1].rstrip("/")
    if u and u[0][0] == "lit":
        u[0][1] = u[0][1].lstrip("/")
    b = [p for p in b if p[0] == "dyn" or p[1]]
    u = [p for p in u if p[0] == "dyn" or p[1]]
    if not u:
        return b or [["lit", "/"]]
    return b + [["lit", "/"]] + u

_ORIGIN_RE = re.compile(r"^(?:[A-Za-z][\w+.-]*:)?//[^/?#]*")
DYN = "\x00"
MIXED = "\x00mixed"  # a client segment of text and a computed value
EXTERNAL = "another host"
_INTERNAL_SUFFIXES = (".local", ".localhost", ".internal", ".svc", ".lan", ".home", ".test", ".localdomain")


def _public_host(host: str) -> bool:
    """A host name on the public internet (``api.github.com``): a call there is another service's, never linked to a
    route of this repository by path. A name without a dot (``backend``, ``localhost``), an IP address or an
    internal suffix (``.local``, ``.svc``, ``.internal``, ...) is taken for one of the project's own services."""
    h = host.lower()
    h = h[1:h.index("]")] if h.startswith("[") and "]" in h else h.rpartition(":")[0] if h.count(":") == 1 else h
    if not h or "." not in h or re.fullmatch(r"[\d.]+", h) or ":" in h:
        return False
    return not any(h.endswith(s) for s in _INTERNAL_SUFFIXES) and ".svc." not in h


def client_path(pieces: list[list[str]] | None) -> tuple[list[str | None] | None, list[str]]:
    """The path segments a client URL names (``None`` for a dynamic segment) and notes on what was assumed;
    ``(None, [reason])`` when the URL is not a path the text spells."""
    notes: list[str] = []
    if not pieces:
        return None, ["no URL"]
    if pieces[0][0] == "dyn":
        if len(pieces) > 1 and pieces[1][0] == "lit" and pieces[1][1].startswith("/"):
            notes.append(f"`{pieces[0][1]}` taken for the server's origin")
            pieces = pieces[1:]
        else:
            return None, ["the URL starts with a value the text does not spell"]
    s = "".join(t if k == "lit" else DYN for k, t in pieces)
    m = _ORIGIN_RE.match(s)
    if m:
        host = m.group(0).rpartition("//")[2].rpartition("@")[2]
        if DYN in host:
            notes.append("the host is computed")
        elif _public_host(host):
            return None, [f"{EXTERNAL}: {host}"]
        s = s[m.end():] or "/"
    if not s.startswith("/"):
        return None, ["a relative URL"]
    s = re.split(r"[?#]", s, maxsplit=1)[0]
    raw = [seg for seg in s.split("/") if seg]
    segs: list[str | None] = []
    for k, seg in enumerate(raw):
        if DYN not in seg:
            segs.append(seg)
        elif not seg.replace(DYN, ""):
            segs.append(None)  # wholly computed: any value, matched by a parameter
        elif k == len(raw) - 1 and not seg.startswith(DYN) and DYN not in seg.rstrip(DYN):
            # '/api/users' + qs: a value after the last literal is most often a query string or fragment
            lit = seg.rstrip(DYN)
            notes.append(f"the value after `{lit}` taken for a query string or fragment")
            segs.append(lit)
        else:
            segs.append(MIXED)  # text and a value in one segment: matches no literal and no parameter
    return segs, notes


_REST = re.compile(r"\*\w*|\*\*|\{\*\w*\}|\(\.\*\)|\{\w+:path\}|<path:\w+>|\{\w+:\.\*\}|:\w+[*+]|\[\.\.\.\w+\]|"
                   r"\[\[\.\.\.\w+\]\]")
_PARAM = re.compile(r":\w+\??|\{[^{}]*\}|<[^<>]*>|\[\w+\]|\(\?P<\w+>[^)]*\)")


def route_segments(path: str) -> list[list[str]]:
    """A route pattern as ``["lit", text]``, ``["param", converter, "opt"?]`` and ``["rest"]`` segments
    (``:id``, ``:id?``, ``{id}``, ``{id:int}``, ``<int:id>``, ``[id]``, ``*``, ``{*path}``, ``<path:p>``)."""
    out: list[list[str]] = []
    for seg in path.split("/"):
        if not seg:
            continue
        if _REST.fullmatch(seg):
            out.append(["rest"])
        elif _PARAM.fullmatch(seg):
            conv = "int" if re.fullmatch(r"<int:\w+>|\{\w+:int\}", seg) else ""
            out.append(["param", conv] + (["opt"] if seg.startswith(":") and seg.endswith("?") else []))
        elif any(ch in seg for ch in ":{<[*("):
            out.append(["param", ""])  # a literal and a parameter in one segment (`{id}.json`): any value
        else:
            out.append(["lit", seg])
    return out


def match_segments(route: list[list[str]], client: list[str | None]) -> int | None:
    """How many literal segments a route matches a client path with; None when it does not match. A dynamic
    client segment matches a parameter only (it may hold any value, so a literal segment is not assumed)."""
    lits = 0
    for i, r in enumerate(route):
        if r[0] == "rest":
            return lits
        if i >= len(client):
            return lits if i == len(route) - 1 and r[0] == "param" and r[2:] == ["opt"] else None
        c = client[i]
        if c == MIXED:
            return None
        if r[0] == "lit":
            if c is None or c != r[1]:
                return None
            lits += 1
        elif r[1] == "int" and c is not None and not c.isdigit():
            return None
    return lits if len(client) == len(route) else None


def join_path(*parts: str, keep_slash: bool = False) -> str:
    """The parts joined with one ``/`` between them. ``keep_slash``: a trailing ``/`` of the last part that has
    one is kept (FastAPI serves ``prefix="/items"`` + ``"/"`` at ``/items/``, Flask and Django as written); the
    matching ignores it either way, only the displayed path keeps what the code says."""
    segs = [p.strip("/") for p in parts if p and p.strip("/")]
    out = "/" + "/".join(segs)
    last = next((p for p in reversed(parts) if p), "")
    return out + "/" if keep_slash and segs and last.endswith("/") else out


def code_kind(path: str) -> str | None:
    """``"test"`` for test code, ``"example"`` for example, sample, demo or tutorial code (by its path), else None."""
    from verinoda.testcode import is_test_file

    if is_test_file(path):
        return "test"
    return "example" if _EXAMPLE_PATH.search(path.replace("\\", "/")) else None


_EXAMPLE_PATH = re.compile(r"(^|/)(examples?|samples?|demos?|docs_src|tutorials?)/")


# -- Python -------------------------------------------------------------------------------------------

# a file is parsed only when its text has one of these: a route decorator or table, a mount, an HTTP client
# library, a call whose first argument looks like a URL, a gRPC servicer or stub, an emitted event. Plain
# substring tests come first: one regular expression with all of them cost seconds on a repository of 900 files.
_PY_WORDS = ("include_router", "register_blueprint", "urlpatterns", "add_url_rule", "add_api_route", "add_route",
             "add_get", "add_post", "add_put", "add_patch", "add_delete", "TestClient", "test_client",
             "ClientSession", "requests.", "httpx.", "Servicer", "Stub(", ".emit(")
_PY_DECOR = re.compile(r"@[\w.]+\.(?:route|api_route|get|post|put|patch|delete|head|options|on)\s*\(")
_PY_URL_MARKS = ('("/', "('/", '(f"', "(f'", '("http', "('http", '", "/', "', '/")
_PY_URL_CALL = re.compile(r"\.(?:get|post|put|patch|delete|head|options|request)\s*\(\s*"
                          r"(?:['\"](?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)['\"]\s*,\s*)?"
                          r"[fFrRbBuU]{0,2}['\"](?:/|https?:|\{[^}'\"\n]*\}/)|\bRoute\s*\(\s*['\"]/")


def _py_hinted(text: str) -> bool:
    return (any(w in text for w in _PY_WORDS) or ("@" in text and _PY_DECOR.search(text) is not None)
            or (any(m in text for m in _PY_URL_MARKS) and _PY_URL_CALL.search(text) is not None))


_PY_APPS = {"Flask": "flask", "Quart": "quart", "Sanic": "sanic", "FastAPI": "fastapi", "Starlette": "starlette",
            "APIRouter": "fastapi", "Blueprint": "flask", "RouteTableDef": "aiohttp", "Application": "aiohttp"}
_PY_ROUTERS = {"APIRouter", "Blueprint", "RouteTableDef"}
_PY_CLIENT_CTORS = {"Client": "httpx", "AsyncClient": "httpx", "Session": "requests", "session": "requests",
                    "ClientSession": "aiohttp", "TestClient": "test client", "test_client": "test client"}
_PY_CLIENT_NAME = re.compile(r"(?i)(?:^|_)(?:client|session|http|api)$|^(?:client|session|http|api)(?:_|$)")
_PY_FRAMEWORKS = ("flask", "fastapi", "quart", "sanic", "starlette", "aiohttp", "django")


def _const_str(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _kw(call: ast.Call, *names: str):
    return next((k.value for k in call.keywords if k.arg in names), None)


def _methods_of(node) -> list[str] | None:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        ms = [_const_str(e) for e in node.elts]
        return sorted({m.upper() for m in ms if m}) or None
    s = _const_str(node)
    return [s.upper()] if s else None


def _target_name(t) -> str | None:
    return _dotted(t) if isinstance(t, (ast.Name, ast.Attribute)) else None


def _py_imports(nodes, frameworks: set[str] | None = None) -> dict[str, str]:
    """Local name -> dotted origin (``pkg.mod`` or ``pkg.mod.name``, leading dots for a relative import) of the
    imports among ``nodes``; the web frameworks imported are added to ``frameworks``."""
    imports: dict[str, str] = {}
    fws = frameworks if frameworks is not None else set()
    for node in nodes:
        if isinstance(node, ast.Import):
            for a in node.names:
                imports[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
                fws.update(f for f in _PY_FRAMEWORKS if a.name.split(".")[0] == f)
        elif isinstance(node, ast.ImportFrom):
            mod = "." * node.level + (node.module or "")
            fws.update(f for f in _PY_FRAMEWORKS if (node.module or "").split(".")[0] == f)
            for a in node.names:
                imports[a.asname or a.name] = f"{mod}.{a.name}" if mod and not mod.endswith(".") else f"{mod}{a.name}"
    return imports


def _name_bindings(nodes) -> Counter:
    """How often each name is bound among ``nodes``: assignments and other stores, and function parameters (a
    parameter shadows a module constant of the same name inside its function)."""
    out = Counter(n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
    out.update(n.arg for n in nodes if isinstance(n, ast.arg))
    return out


def py_values(tree: ast.Module) -> dict[str, list]:
    """The string values a module spells once, by the name another module reads them with:
    ``NAME`` (a module-level constant), ``Cls.ATTR`` (a class attribute default) and ``obj.ATTR`` for a module-level
    ``obj = Cls(...)`` that passes no other value for it (a pydantic ``Settings()`` instance). Each is
    ``[value, line, kind]``, kind ``"const"``, ``"default"`` or ``"settings default"`` (a ``BaseSettings`` class:
    the environment can give another value at run time). A name bound more than once, also as a function's
    parameter, has no entry. An attribute assigned anywhere (``obj.ATTR = ...``, ``self.ATTR = ...`` in the
    class) has no default; nor has ``obj.ATTR`` when the class defines ``__init__`` (unless ``BaseSettings``)."""
    nodes = list(ast.walk(tree))
    stores = _name_bindings(nodes)
    attr_stores = {_dotted(n) for n in nodes if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)}
    out: dict[str, list] = {}
    classes: dict[str, tuple[dict[str, list], bool]] = {}

    def assigned(node) -> tuple[str | None, ast.AST | None]:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            return node.targets[0].id, node.value
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            return node.target.id, node.value
        return None, None

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            settings = any((_dotted(b) or "").rpartition(".")[2] == "BaseSettings" for b in node.bases)
            attrs: dict[str, list] = {}
            seen = Counter(assigned(s)[0] for s in node.body)
            # an attribute a method assigns (self.ATTR = ..., cls.ATTR = ...) is not the class default's
            set_inside = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
                          and isinstance(n.ctx, ast.Store) and isinstance(n.value, ast.Name)}
            for s in node.body:
                name, value = assigned(s)
                if name and seen[name] == 1 and _const_str(value) is not None and name not in set_inside:
                    attrs[name] = [_const_str(value), s.lineno, "settings default" if settings else "default"]
            has_init = any(isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and s.name == "__init__"
                           for s in node.body)
            # an __init__ may set any attribute of the instance: only the class's own attribute is known
            classes[node.name] = ({} if has_init and not settings else attrs, settings)
            for a, v in attrs.items():
                if f"{node.name}.{a}" not in attr_stores:
                    out[f"{node.name}.{a}"] = v
    for node in tree.body:
        name, value = assigned(node)
        if not name or stores[name] != 1:
            continue
        if _const_str(value) is not None:
            out[name] = [_const_str(value), node.lineno, "const"]
        elif (isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in classes
              and not value.args):
            attrs, _settings = classes[value.func.id]
            given = {k.arg: k.value for k in value.keywords}
            if None in given:  # Settings(**overrides): any attribute may be another value
                continue
            for a, v in attrs.items():
                if f"{name}.{a}" in attr_stores:  # CFG.PREFIX = "..." somewhere: no one value
                    continue
                if a in given:
                    s = _const_str(given[a])
                    if s is not None:
                        out[f"{name}.{a}"] = [s, value.lineno, "const"]
                else:
                    out[f"{name}.{a}"] = v
    return out


def _py_prefix(node, values: dict[str, list], imports: dict[str, str], rel: str) -> dict:
    """A mount or router prefix expression: ``{"value": str}`` when this file spells it (with a ``"from"`` note
    for each constant or default it took), else ``{"expr": source, "pieces": [...]}`` whose ``["ref",
    dotted, origin]`` pieces name another module's value (``origin``: where the name's head is imported from)
    and whose ``["dyn", ...]`` pieces are values nothing here spells."""
    s = _const_str(node)
    if s is not None:
        return {"value": s}
    try:
        src = ast.unparse(node)[:120]
    except Exception:  # an expression ast cannot write back: the name of its type
        src = type(node).__name__
    # no constants given: every name goes through ``values``, so even a same-file constant gets its note
    pieces = py_pieces(node, {}) or [["dyn", "expr"]]
    out, notes, resolved = [], [], True
    for p in pieces:
        if p[0] == "lit":
            out.append(p)
            continue
        name = p[1]
        if name in values:
            v = values[name]
            out.append(["lit", v[0]])
            notes.append(_value_note(name, v, rel))
            continue
        head = name.partition(".")[0]
        if name not in ("expr", "{...}") and head in imports:
            out.append(["ref", name, imports[head]])
        else:
            out.append(["dyn", name])
        resolved = False
    if resolved:
        return {"value": "".join(t for _k, t in out), "from": notes, "expr": src}
    return {"expr": src, "pieces": out, **({"from": notes} if notes else {})}


def _value_note(name: str, v: list, file: str | None) -> str:
    where = f"{file}:{v[1]}" if file else f"line {v[1]}"
    if v[2] == "settings default":
        return (f"`{name}` = {v[0]!r}: the class default at {where}; the environment can set another value at "
                f"run time")
    return f"`{name}` = {v[0]!r} at {where}" + (" (a class default)" if v[2] == "default" else "")


def py_facts(text: str, rel: str) -> dict:
    """Routes, mounts, client calls, events and gRPC services of one Python file (JSON-ready)."""
    if not _py_hinted(text):
        return {}
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return {}
    frameworks: set[str] = set()
    consts: dict[str, str] = {}
    objs: dict[str, dict] = {}
    client_vars: dict[str, dict] = {}
    stub_vars: dict[str, str] = {}
    nodes = list(ast.walk(tree))
    imports = _py_imports(nodes, frameworks)
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            s = _const_str(node.value)
            if s is not None:
                consts[node.targets[0].id] = s
    bindings = _name_bindings(nodes)
    if consts:  # a name bound more than once (anywhere in the module, also as a parameter) has no one value
        consts = {k: v for k, v in consts.items() if bindings[k] == 1}
    # an imported name a parameter or an assignment rebinds is not the imported value where it is read
    prefix_imports = {k: v for k, v in imports.items() if not bindings[k]}

    values: dict[str, list] | None = None

    def prefix_fields(node) -> tuple[str | None, dict]:
        """A prefix expression's value (None: not spelled here) and the fields that say where it came from."""
        nonlocal values
        if _const_str(node) is not None:
            return _const_str(node), {}
        if values is None:
            values = py_values(tree)
        p = _py_prefix(node, values, prefix_imports, rel)
        extra = {"prefix_expr": p["expr"]} | ({"prefix_from": p["from"]} if p.get("from") else {})
        if "value" in p:
            return p["value"], extra
        return None, extra | {"prefix_pieces": p["pieces"]}

    def ctor(call) -> str | None:
        f = call.func
        return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None

    def note_assign(name: str | None, call) -> None:
        if not name or not isinstance(call, ast.Call):
            return
        c = ctor(call)
        if c in _PY_APPS:
            given = _kw(call, "prefix", "url_prefix")
            prefix, extra = prefix_fields(given) if given is not None else ("", {})
            objs[name] = {"kind": "router" if c in _PY_ROUTERS else "app", "prefix": prefix or "", "fw": _PY_APPS[c],
                          **extra}
        elif c in _PY_CLIENT_CTORS:
            base = _kw(call, "base_url")
            client_vars[name] = {"lib": _PY_CLIENT_CTORS[c],
                                 "base": py_pieces(base, consts) if base is not None else None}
        elif c and c.endswith("Stub") and len(c) > 4:
            stub_vars[name] = c[:-4]

    for node in nodes:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            note_assign(_target_name(node.targets[0]), node.value)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for it in node.items:
                if it.optional_vars is not None:
                    note_assign(_target_name(it.optional_vars), it.context_expr)

    out: dict = defaultdict(list)
    fw_default = next(iter(sorted(frameworks)), "python")
    decorators = {id(d) for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                  for d in n.decorator_list}

    def route(obj, attr, call, handler, line) -> None:
        path_node = call.args[0] if call.args else _kw(call, "path", "rule")
        if attr == "add_route" and len(call.args) >= 2:  # aiohttp: add_route(method, path, handler)
            path_node = call.args[1]
        path = _const_str(path_node)
        if path is None or not (path.startswith("/") or (path == "" and objs.get(obj, {}).get("kind") == "router")):
            return
        if attr in ("route", "api_route", "add_url_rule", "add_api_route"):
            methods = _methods_of(_kw(call, "methods")) or ["GET"]
        elif attr == "add_route":
            methods = _methods_of(call.args[0]) if call.args else None
            methods = None if methods == ["*"] else methods
        else:
            methods = [attr.replace("add_", "").upper()]
        fw = objs.get(obj, {}).get("fw") or fw_default
        out["routes"].append({"fw": fw, "obj": obj, "methods": methods, "path": path, "line": line,
                              "handler": handler})

    def mount_record(parent: str, expr, prefix: str | None, line: int, replaces: bool = False,
                     fields: dict | None = None) -> None:
        d = _dotted(expr)
        if not d:
            return
        # Flask: a url_prefix given to register_blueprint replaces the blueprint's own (None: not given)
        extra = ({"replaces": True} if replaces else {}) | (fields or {})
        head, _, tail = d.partition(".")
        if tail:   # users.router: the module users (imported) holds router
            origin = imports.get(head, head)
            out["mounts"].append({"on": parent, "prefix": prefix, "mods": [origin], "obj": tail, "line": line, **extra})
        elif d in objs:
            out["mounts"].append({"on": parent, "prefix": prefix, "local": True, "obj": d, "line": line, **extra})
        elif d in imports:  # from .routers.users import router
            origin = imports[d]
            out["mounts"].append({"on": parent, "prefix": prefix, "mods": [origin.rpartition(".")[0] or origin],
                                  "obj": origin.rpartition(".")[2], "line": line, **extra})

    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                    continue
                obj, attr = _dotted(dec.func.value), dec.func.attr
                if attr in ("route", "api_route") + _VERBS and obj:
                    route(obj, attr, dec, {"kind": "def", "line": node.lineno}, dec.lineno)
                elif attr == "on" and dec.args and _const_str(dec.args[0]):  # python-socketio / Flask-SocketIO
                    out["events"].append({"role": "listen", "name": _const_str(dec.args[0]), "line": dec.lineno,
                                          "handler": {"kind": "def", "line": node.lineno}})
        elif isinstance(node, ast.ClassDef):
            for b in node.bases:
                bn = _dotted(b) or ""
                last = bn.rpartition(".")[2]
                if last.endswith("Servicer") and len(last) > 8:
                    methods = [{"name": f.name, "line": f.lineno} for f in node.body
                               if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name[:1].isupper()]
                    if methods:
                        out["grpc_services"].append({"service": last[:-8], "methods": methods, "line": node.lineno})
        elif isinstance(node, ast.Call) and id(node) not in decorators:
            f = node.func
            if isinstance(f, ast.Attribute):
                recv, attr = _dotted(f.value), f.attr
                if attr == "include_router" and node.args:
                    given = _kw(node, "prefix")
                    prefix, fields = prefix_fields(given) if given is not None else ("", {})
                    # a prefix not spelled here keeps the mount: its rows say "prefix not resolved"
                    mount_record(recv or "", node.args[0], prefix or "", node.lineno, fields=fields)
                elif attr == "register_blueprint" and node.args:
                    given = _kw(node, "url_prefix")
                    prefix, fields = prefix_fields(given) if given is not None else (None, {})
                    mount_record(recv or "", node.args[0], prefix if given is None or prefix is not None else "",
                                 node.lineno, replaces=True, fields=fields)
                elif attr in ("add_url_rule", "add_api_route", "add_route") or attr in tuple("add_" + v for v in _VERBS):
                    h = _kw(node, "view_func", "endpoint", "handler")
                    if h is None:
                        rest = node.args[2:] if attr == "add_route" else node.args[1:]
                        h = rest[0] if rest else None
                    hn = _dotted(h) if h is not None else None
                    if hn:
                        route(recv, attr, node, {"kind": "name", "name": hn.rpartition(".")[2],
                                                 "module": hn.rpartition(".")[0] or None}, node.lineno)
                elif attr == "emit" and node.args and _const_str(node.args[0]):
                    out["events"].append({"role": "emit", "name": _const_str(node.args[0]), "line": node.lineno})
                elif recv in stub_vars and attr[:1].isupper():
                    out["grpc_calls"].append({"service": stub_vars[recv], "method": attr, "line": node.lineno})
                elif attr in _VERBS + ("request",) and recv and recv not in objs:
                    _py_client(node, recv, attr, client_vars, consts, out)
            elif isinstance(f, ast.Name) and f.id in ("path", "re_path", "url") and node.args and "urlpatterns" in text:
                _django(node, f.id, out)
            elif isinstance(f, ast.Name) and f.id == "Route" and node.args and _const_str(node.args[0]):
                h = node.args[1] if len(node.args) > 1 else _kw(node, "endpoint")
                hn = _dotted(h) if h is not None else None
                if hn and _const_str(node.args[0]).startswith("/"):
                    out["routes"].append({"fw": "starlette", "obj": None, "path": _const_str(node.args[0]),
                                          "methods": _methods_of(_kw(node, "methods")), "line": node.lineno,
                                          "handler": {"kind": "name", "name": hn.rpartition(".")[2],
                                                      "module": hn.rpartition(".")[0] or None}})
    if objs:
        out["objs"] = objs
    return dict(out)


def _py_client(node: ast.Call, recv: str, attr: str, client_vars: dict, consts: dict, out) -> None:
    lib = None
    if recv in ("requests", "httpx"):
        lib = recv
    elif recv in client_vars:
        lib = client_vars[recv]["lib"]
    elif _PY_CLIENT_NAME.search(recv.rpartition(".")[2]):
        lib = "client"
    if lib is None:
        return
    args = list(node.args)
    method = attr.upper()
    if attr == "request":
        method = (_const_str(args[0]) or "").upper() or None if args else None
        args = args[1:]
    url = args[0] if args else _kw(node, "url")
    if url is None:
        return
    pieces = py_pieces(url, consts)
    if not pieces:
        return
    base = (client_vars.get(recv) or {}).get("base")
    if base:
        pieces = join_base(base, pieces)
    first = pieces[0]
    if lib == "client" and not (first[0] == "lit" and (first[1].startswith("/") or first[1].startswith("http"))):
        return  # a name that only looks like a client: its first argument must look like a URL
    out["clients"].append({"lib": lib, "method": method, "pieces": pieces, "line": node.lineno,
                           "url": _show(pieces)})


def _django(node: ast.Call, fn: str, out) -> None:
    pat = _const_str(node.args[0])
    if pat is None or len(node.args) < 2:
        return
    if fn != "path":  # re_path / url: only plain patterns and named groups
        pat = pat.lstrip("^").rstrip("$")
        pat = re.sub(r"\(\?P<(\w+)>[^)]*\)", r"<\1>", pat)
        if re.search(r"[\\()|?+*\[\]{}]", pat):
            return
    tgt = node.args[1]
    if isinstance(tgt, ast.Call) and isinstance(tgt.func, ast.Name) and tgt.func.id == "include" and tgt.args:
        mod = _const_str(tgt.args[0])
        if mod:
            out["includes"].append({"prefix": pat, "module": mod, "line": node.lineno})
        return
    if isinstance(tgt, ast.Call) and isinstance(tgt.func, ast.Attribute) and tgt.func.attr == "as_view":
        tgt = tgt.func.value
    hn = _dotted(tgt)
    if hn:
        out["routes"].append({"fw": "django", "obj": None, "path": "/" + pat, "methods": None, "line": node.lineno,
                              "handler": {"kind": "name", "name": hn.rpartition(".")[2],
                                          "module": hn.rpartition(".")[0] or None}, "urlconf": True})


def _show(pieces: list[list[str]]) -> str:
    return "".join(t if k == "lit" else "{" + t + "}" for k, t in pieces)[:160]


# -- JavaScript / TypeScript --------------------------------------------------------------------------

_JS_HINT = re.compile(r"fetch|axios|\.(?:get|post|put|patch|delete|all|route|use|emit|on|once|request)\s*[<(]|"
                      r"@(?:Controller|Get|Post|Put|Patch|Delete|OnEvent|EventPattern|MessagePattern)\b|"
                      r"router\s*\(|Router\s*\(|procedure|trpc|gql|graphql|(?:Query|Mutation|Subscription)\s*:|"
                      r"setGlobalPrefix")
_JS_FRAMEWORK = re.compile(r"""(?:from\s+|require\s*\(\s*)['"](express|koa-router|@koa/router|koa|fastify|hono|"""
                           r"""restify|polka|elysia|@nestjs/[\w-]+|socket\.io)['"]""")
_JS_SERVER_CTOR = re.compile(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::\s*[\w.<>]+\s*)?=\s*(?:new\s+)?"
                             r"(express\.Router|express|Router|KoaRouter|Fastify|fastify|Hono|Elysia|polka|"
                             r"restify\.createServer)\s*\(")
_JS_SERVER_NAMES = {"app", "router", "server", "api", "routes", "fastify"}
_JS_CONST = re.compile(r"""(?:^|[;\n])\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"""
                       r"""(['"`])([^'"`\n$]*)\2""")
# these start at the dot (a literal start is searched fast); the object before it is read by _receiver()
_JS_ROUTE = re.compile(r"\.\s*(get|post|put|patch|delete|del|all|head|options)\s*\(")
_JS_ROUTE_CHAIN = re.compile(r"\.\s*route\s*\(")
_JS_USE = re.compile(r"\.\s*use\s*\(")
_JS_FETCH = re.compile(r"(?<![\w$.])(?:(?:window|globalThis|self)\s*\.\s*)?fetch\s*\(")
# found from the dot (a literal start is searched fast), the receiver read backwards from it
_JS_CLIENT = re.compile(r"\.\s*(get|post|put|patch|delete|head|options|request)\s*(?:<[^>()]*>)?\s*\(")
_JS_RECEIVER = re.compile(r"(?<![\w$.])((?:this\s*\.\s*)?[A-Za-z_$][\w$]*)\s*$")
_JS_SUPERTEST = re.compile(r"(?<![\w$.])request\s*\(\s*[\w$.]+\s*\)\s*\.\s*(get|post|put|patch|delete|head|options)"
                           r"\s*\(")
_JS_AXIOS_CALL = re.compile(r"(?<![\w$.])axios\s*(?:\.\s*request\s*)?\(")
_JS_CLIENT_NAMES = re.compile(r"^(?:this\.)?(?:axios|ky|got|superagent|http|httpClient|\$http|api|apiClient|client|"
                              r"httpService|fetcher|request|instance)$")
_JS_AXIOS_CREATE = re.compile(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*axios\s*\.\s*create\s*\(")
_JS_IMPORT_DEFAULT = re.compile(r"""import\s+([A-Za-z_$][\w$]*)\s*(?:,\s*\{[^}]*\})?\s*from\s*['"](\.[^'"]+)['"]""")
_JS_IMPORT_NAMED = re.compile(r"""import\s*(?:[A-Za-z_$][\w$]*\s*,\s*)?\{([^}]*)\}\s*from\s*['"](\.[^'"]+)['"]""")
_JS_REQUIRE = re.compile(r"""(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\s*\(\s*['"](\.[^'"]+)['"]\s*\)""")
_JS_EMIT = re.compile(r"\.\s*emit\s*\(")
_JS_LISTEN = re.compile(r"\.\s*(?:on|once|addListener|prependListener)\s*\(")
_JS_DECOR = re.compile(r"@(Controller|Get|Post|Put|Patch|Delete|Head|Options|All|OnEvent|EventPattern|"
                       r"MessagePattern)\s*\(")
_JS_METHOD_DECL = re.compile(r"(?:(?:public|private|protected|static|async|override|readonly)\s+)*"
                             r"([A-Za-z_$][\w$]*)\s*(?:<[^>()]*>)?\s*\(")
_JS_GLOBAL_PREFIX = re.compile(r"\.\s*setGlobalPrefix\s*\(\s*(['\"])([^'\"]*)\1")
_NEXT_EXPORT = re.compile(r"export\s+(?:async\s+)?function\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b|"
                          r"export\s+const\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s*=")
_NEXT_DEFAULT = re.compile(r"export\s+default\b")
_TRPC_ROUTER = re.compile(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*"
                          r"(?:t\s*\.\s*router|router|createTRPCRouter|createRouter)\s*\(\s*\{")
_TRPC_CALL = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\.\s*((?:[A-Za-z_$][\w$]*\s*\.\s*)+?)"
                        r"(useQuery|useSuspenseQuery|useInfiniteQuery|useMutation|useSubscription|query|mutate|"
                        r"mutation|subscribe|fetch|prefetch|ensureData|invalidate)\s*\(")
_TRPC_ROOTS = {"trpc", "api", "client", "utils", "trpcClient"}
_GQL_RESOLVERS = re.compile(r"(?<![\w$.])(Query|Mutation|Subscription)\s*:\s*\{")
_GQL_DOC = re.compile(r"(?<![\w$])(?:gql|graphql)\s*(?:\(\s*)?`")


def _key_items(tok: _Tokens, open_i: int) -> list[tuple[str, int, str]]:
    """``(key, offset, value)`` of the object literal whose ``{`` is at ``open_i`` (method shorthand: value ``(``)."""
    close = tok.close(open_i)
    if close < 0:
        return []
    out = []
    pos = open_i + 1
    for part in tok.split(open_i + 1, close):
        m = re.match(r"\s*(?:async\s+)?(?:(['\"])([^'\"]+)\1|([A-Za-z_$][\w$]*))\s*(:|\()", part)
        if m:
            key = m.group(2) or m.group(3)
            lead = len(part) - len(part.lstrip())
            value = part[m.end():].strip() if m.group(4) == ":" else "("
            out.append((key, pos + lead, value))
        pos += len(part) + 1
    return out


_JS_DECLARED = re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)")
_JS_ASSIGNED = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*(?:[-+*/%]|\?\?|\|\||&&)?=(?![=>])")


def _js_consts(code: str, tok: _Tokens) -> dict[str, str]:
    """Module-level ``const NAME = 'text'`` (or let / var) the file never declares again nor assigns: a name
    declared in two functions, or reassigned, has no one value to read."""
    found: dict[str, str] = {}
    for m in _JS_CONST.finditer(code):
        if tok.top_level(m.start(1)):
            found[m.group(1)] = m.group(3)
    if not found:
        return {}
    declared = Counter(m.group(1) for m in _JS_DECLARED.finditer(code))
    assigned = Counter(m.group(1) for m in _JS_ASSIGNED.finditer(code))
    return {name: value for name, value in found.items() if declared[name] == 1 and assigned[name] == 1}


def _js_handler(arg: str, line: int) -> dict:
    a = arg.strip()
    if re.fullmatch(r"[A-Za-z_$][\w$]*", a):
        return {"kind": "name", "name": a}
    m = re.fullmatch(r"([A-Za-z_$][\w$]*)\s*\.\s*([A-Za-z_$][\w$]*)(?:\s*\.\s*bind\s*\([^)]*\))?", a)
    if m:
        return {"kind": "name", "name": m.group(2), "module": m.group(1)}
    return {"kind": "inline", "line": line}


def js_facts(text: str, rel: str) -> dict:
    """Routes, mounts, client calls, events, tRPC and GraphQL of one JavaScript/TypeScript file (JSON-ready)."""
    next_api = _next_route(rel) is not None
    if not (_JS_HINT.search(text) or next_api):
        return {}
    code = strip_js(text)
    tok = _Tokens(code)
    starts = _line_starts(code)
    out: dict = defaultdict(list)
    consts = _js_consts(code, tok)
    framework = bool(_JS_FRAMEWORK.search(code))
    objs: dict[str, dict] = {}
    for m in _JS_SERVER_CTOR.finditer(code):
        kind = "router" if "Router" in m.group(2) else "app"
        prefix = ""
        a = tok.args(m.end() - 1)
        if a and a[0] and kind == "router":
            pm = re.search(r"prefix\s*:\s*(['\"])([^'\"]*)\1", a[0][0])
            prefix = pm.group(2) if pm else ""
        objs[m.group(1)] = {"kind": kind, "prefix": prefix, "fw": m.group(2).split(".")[0].lower()}
    imports: dict[str, list[str]] = {}
    for m in _JS_IMPORT_DEFAULT.finditer(code):
        imports[m.group(1)] = [m.group(2), "default"]
    for m in _JS_IMPORT_NAMED.finditer(code):
        for part in m.group(1).split(","):
            bits = part.strip().split(" as ")
            if bits[0].strip():
                imports[bits[-1].strip()] = [m.group(2), bits[0].strip()]
    for m in _JS_REQUIRE.finditer(code):
        imports[m.group(1)] = [m.group(2), "default"]

    def server(obj: str) -> bool:
        return obj in objs or (framework and obj in _JS_SERVER_NAMES)

    def ln(pos: int) -> int:
        return _line_of(starts, pos)

    def receiver(m) -> str | None:
        rm = _JS_RECEIVER.search(code, max(0, m.start() - 80), m.start())
        return re.sub(r"\s+", "", rm.group(1)) if rm else None

    def found(pat, gate: bool):
        """``(match, receiver)`` of ``pat`` in the code, none when ``gate`` is false (most files skip the scan)."""
        return ((m, receiver(m)) for m in pat.finditer(code)) if gate else ()

    has_server = bool(objs) or framework
    for m, obj in found(_JS_ROUTE, has_server):
        verb = m.group(1)
        if not obj or not server(obj):
            continue
        a = tok.args(m.end() - 1)
        if not a or len(a[0]) < 2:
            continue
        path = _js_literal(a[0][0])
        if path is None and a[0][0] in consts:
            path = consts[a[0][0]]
        if path is None or not (path.startswith("/") or path == "" or path == "*"):
            continue
        methods = None if verb == "all" else ["DELETE" if verb == "del" else verb.upper()]
        out["routes"].append({"fw": objs.get(obj, {}).get("fw") or "express", "obj": obj, "methods": methods,
                              "path": path, "line": ln(m.start()), "handler": _js_handler(a[0][-1], ln(m.start()))})
    for m, obj in found(_JS_ROUTE_CHAIN, has_server):  # router.route('/x').get(h).post(h)
        a = tok.args(m.end() - 1)
        if not obj or not server(obj) or not a or not a[0]:
            continue
        path = _js_literal(a[0][0])
        if path is None:
            continue
        i = a[1] + 1
        while True:
            cm = re.compile(r"\s*\.\s*(get|post|put|patch|delete|all|head|options)\s*\(").match(code, i)
            if not cm:
                break
            ca = tok.args(cm.end() - 1)
            if not ca:
                break
            if ca[0]:
                verb = cm.group(1)
                out["routes"].append({"fw": objs.get(obj, {}).get("fw") or "express", "obj": obj,
                                      "methods": None if verb == "all" else [verb.upper()], "path": path,
                                      "line": ln(cm.start() + 1), "handler": _js_handler(ca[0][-1], ln(cm.start()))})
            i = ca[1] + 1
    for m, obj in found(_JS_USE, has_server):
        a = tok.args(m.end() - 1)
        if not a or len(a[0]) < 2 or not obj or not server(obj):
            continue
        prefix = _js_literal(a[0][0])
        if prefix is None and a[0][0].strip() in consts:  # app.use(API_PREFIX, router)
            prefix = consts[a[0][0].strip()]
        if prefix is None or not prefix.startswith("/"):
            continue
        tgt = re.sub(r"\s*\.\s*(?:routes|middleware)\s*\(\s*\)\s*$", "", a[0][-1].strip())
        rm = re.fullmatch(r"""require\s*\(\s*['"](\.[^'"]+)['"]\s*\)""", tgt)
        rec = {"on": obj, "prefix": prefix, "line": ln(m.start())}
        if rm:
            rec.update(path=rm.group(1))
        elif tgt in objs:
            rec.update(local=True, obj=tgt)
        elif tgt in imports:
            rec.update(path=imports[tgt][0], export=imports[tgt][1])
        else:
            continue
        out["mounts"].append(rec)
    # NestJS controllers and event handlers, Next.js route files
    controller = ""
    for m in _JS_DECOR.finditer(code):
        kind = m.group(1)
        a = tok.args(m.end() - 1)
        if a is None:
            continue
        arg = _js_literal(a[0][0]) if a[0] else ""
        if kind == "Controller":
            controller = arg or ""
            if a[0] and arg is None:
                pm = re.search(r"path\s*:\s*(['\"])([^'\"]*)\1", a[0][0])
                controller = pm.group(2) if pm else ""
            continue
        j = a[1] + 1
        while True:  # other decorators between this one and the method
            dm = re.compile(r"\s*@[\w$.]+\s*").match(code, j)
            if not dm:
                break
            j = dm.end()
            if code[j:j + 1] == "(":
                c = tok.close(j)
                j = c + 1 if c > 0 else j
        mm = _JS_METHOD_DECL.match(code, j + len(code[j:]) - len(code[j:].lstrip()))
        if not mm or arg is None:
            continue
        handler = {"kind": "def", "line": ln(mm.start(1))}
        if kind in ("OnEvent", "EventPattern", "MessagePattern"):
            out["events"].append({"role": "listen", "name": arg, "line": ln(m.start()), "handler": handler})
        else:
            out["routes"].append({"fw": "nestjs", "obj": None, "methods": None if kind == "All" else [kind.upper()],
                                  "path": join_path(controller, arg), "line": ln(m.start()), "handler": handler,
                                  "nest": True})
    gp = _JS_GLOBAL_PREFIX.search(code)
    if gp:
        out["nest_prefix"] = gp.group(2)
    if next_api:
        for m in _NEXT_EXPORT.finditer(code):
            out["next"].append({"method": m.group(1) or m.group(2), "line": ln(m.start())})
        dm = _NEXT_DEFAULT.search(code)
        if dm and "/pages/" in "/" + rel:
            out["next"].append({"method": None, "line": ln(dm.start())})
    # clients
    instances: dict[str, list | None] = {}
    for m in _JS_AXIOS_CREATE.finditer(code):
        a = tok.args(m.end() - 1)
        base = None
        if a and a[0]:
            bm = re.search(r"baseURL\s*:\s*", a[0][0])
            if bm:
                rest = _split_top(a[0][0][bm.end():bm.end() + _SMALL])[0]
                base = js_pieces(rest.strip().rstrip("}").strip(), consts)
            else:
                base = []
        else:
            base = []
        instances[m.group(1)] = base
    if instances:
        out["instances"] = instances
    if imports:
        out["imports"] = imports
    if objs:
        out["objs"] = objs

    def add_client(lib: str, method, url_arg: str, pos: int, recv: str | None = None) -> None:
        pieces = js_pieces(url_arg, consts)
        if not pieces:
            return
        first = pieces[0]
        if lib == "client" and not (first[0] == "lit" and (first[1].startswith("/") or first[1].startswith("http"))):
            if not (first[0] == "dyn" and len(pieces) > 1 and pieces[1][0] == "lit" and pieces[1][1].startswith("/")):
                return
        rec = {"lib": lib, "method": method, "pieces": pieces, "line": ln(pos), "url": _show(pieces)}
        if recv:
            rec["recv"] = recv
        out["clients"].append(rec)

    for m, _r in found(_JS_FETCH, "fetch" in code):
        a = tok.args(m.end() - 1)
        if not a or not a[0]:
            continue
        method = "GET"
        if len(a[0]) > 1:
            mm = re.search(r"\bmethod\s*:\s*(['\"`])(\w+)\1", a[0][1])
            method = mm.group(2).upper() if mm else (None if re.search(r"\bmethod\b", a[0][1]) else "GET")
        add_client("fetch", method, a[0][0], m.start())
    for m, recv in found(_JS_CLIENT, True):
        if not recv:
            continue
        verb = m.group(1)
        if server(recv) and not (recv in instances or recv == "axios"):
            continue
        lib = "axios" if recv == "axios" or recv in instances else None
        if lib is None and recv in imports:
            lib = "instance?"
        if lib is None and _JS_CLIENT_NAMES.match(recv):
            lib = "client"
        if lib is None:
            continue
        a = tok.args(m.end() - 1)
        if not a or not a[0]:
            continue
        if verb == "request":
            obj = a[0][0]
            um = re.search(r"\burl\s*:\s*", obj)
            if not obj.startswith("{") or not um:
                continue
            url = _split_top(obj[um.end():um.end() + _SMALL])[0].strip().rstrip("}").strip()
            mm = re.search(r"\bmethod\s*:\s*(['\"`])(\w+)\1", obj)
            method = mm.group(2).upper() if mm else (None if re.search(r"\bmethod\b", obj) else "GET")
            add_client(lib if lib != "instance?" else "client", method, url, m.start(), recv)
        else:
            add_client(lib if lib != "instance?" else "client", verb.upper(), a[0][0], m.start(), recv)
    for m, _r in found(_JS_SUPERTEST, "request" in code):
        a = tok.args(m.end() - 1)
        if a and a[0]:
            add_client("supertest", m.group(1).upper(), a[0][0], m.start())
    for m, _r in found(_JS_AXIOS_CALL, "axios" in code):
        a = tok.args(m.end() - 1)
        if not a or not a[0] or not a[0][0].startswith("{"):
            continue
        obj = a[0][0]
        um = re.search(r"\burl\s*:\s*", obj)
        if not um:
            continue
        url = _split_top(obj[um.end():um.end() + _SMALL])[0].strip().rstrip("}").strip()
        mm = re.search(r"\bmethod\s*:\s*(['\"`])(\w+)\1", obj)
        method = mm.group(2).upper() if mm else (None if re.search(r"\bmethod\b", obj) else "GET")
        add_client("axios", method, url, m.start())
    # events
    for m, _r in found(_JS_EMIT, "emit" in code):
        a = tok.args(m.end() - 1)
        nm = _js_literal(a[0][0]) if a and a[0] else None
        if nm:
            out["events"].append({"role": "emit", "name": nm, "line": ln(m.start())})
    for m, _r in found(_JS_LISTEN, True):
        a = tok.args(m.end() - 1)
        if not a or len(a[0]) < 2:
            continue
        nm = _js_literal(a[0][0])
        if nm:
            out["events"].append({"role": "listen", "name": nm, "line": ln(m.start()),
                                  "handler": _js_handler(a[0][-1], ln(m.start()))})
    # tRPC
    for m, _r in found(_TRPC_ROUTER, "outer" in code):
        keys = []
        for key, off, value in _key_items(tok, m.end() - 1):
            if re.fullmatch(r"[A-Za-z_$][\w$]*", value):
                keys.append({"key": key, "ref": value, "line": ln(off)})
            elif re.search(r"\.\s*(query|mutation|subscription)\s*\(", value):
                kind = re.findall(r"\.\s*(query|mutation|subscription)\s*\(", value)[-1]
                keys.append({"key": key, "proc": kind, "line": ln(off)})
        if keys:
            out["trpc_routers"].append({"var": m.group(1), "line": ln(m.start()), "keys": keys})
    if "@trpc" in code or "trpc" in code:
        for m in _TRPC_CALL.finditer(code):
            root = m.group(1)
            if root not in _TRPC_ROOTS:
                continue
            path = re.sub(r"\s+", "", m.group(2)).strip(".")
            out["trpc_calls"].append({"root": root, "path": path, "op": m.group(3), "line": ln(m.start())})
    # GraphQL
    if "Query" in code or "Mutation" in code or "Subscription" in code:
        for m in _GQL_RESOLVERS.finditer(code):
            for key, off, _value in _key_items(tok, m.end() - 1):
                out["gql_resolvers"].append({"type": m.group(1), "field": key, "line": ln(off)})
    for m, _r in found(_GQL_DOC, "gql" in code or "graphql" in code):
        tick = m.end() - 1
        end = _skip_template(code, tick)
        for typ, field, off in _gql_root_fields(code[tick + 1:end - 1]):
            out["gql_ops"].append({"type": typ, "field": field, "line": ln(tick + 1 + off)})
    return {k: v for k, v in out.items() if v}


def _gql_root_fields(doc: str) -> list[tuple[str, str, int]]:
    """``(Query|Mutation|Subscription, field, offset)`` of each operation's top-level fields (fragments skipped)."""
    doc = re.sub(r"#[^\n]*", lambda m: " " * len(m.group(0)), doc)
    doc = re.sub(r"\$\{[^}]*\}", lambda m: " " * len(m.group(0)), doc)
    out = []
    i, n = 0, len(doc)
    while i < n:
        m = re.compile(r"\s*(?:(query|mutation|subscription)\b[^{]*)?\{|\s*fragment\b[^{]*\{").match(doc, i)
        if not m:
            j = doc.find("{", i)
            if j < 0:
                break
            m = re.compile(r"\{").match(doc, j)
            typ = "Query" if not re.search(r"\bfragment\b[^{]*$", doc[i:j]) else None
            mm = re.search(r"\b(query|mutation|subscription)\b[^{]*$", doc[i:j])
            if mm:
                typ = mm.group(1).capitalize()
        else:
            typ = None if "fragment" in m.group(0) else (m.group(1) or "query").capitalize()
        open_i = m.end() - 1
        close = _close(doc, open_i)
        if close < 0:
            break
        if typ:
            depth, k = 0, open_i + 1
            while k < close:
                c = doc[k]
                if c in "{(":
                    depth += 1
                elif c in "})":
                    depth -= 1
                elif depth == 0:
                    fm = re.compile(r"(?:[A-Za-z_]\w*\s*:\s*)?([A-Za-z_]\w*)").match(doc, k)
                    if fm and not doc[max(0, k - 3):k].endswith("..."):
                        if fm.group(1) not in ("__typename", "on"):
                            out.append((typ, fm.group(1), k))
                        k = fm.end()
                        continue
                k += 1
        i = close + 1
    return out


def _next_route(rel: str) -> str | None:
    """The URL path a Next.js API file serves (``pages/api/users/[id].ts``, ``app/api/users/[id]/route.ts``)."""
    p = rel.replace("\\", "/")
    stem, ext = posixpath.splitext(p)
    if ext not in (".js", ".ts", ".mjs", ".jsx", ".tsx"):
        return None
    parts = stem.split("/")
    if "pages" in parts:
        i = len(parts) - 1 - parts[::-1].index("pages")
        rest = parts[i + 1:]
        if not rest or rest[0] != "api":
            return None
    elif "app" in parts and parts[-1] == "route":
        i = len(parts) - 1 - parts[::-1].index("app")
        rest = parts[i + 1:-1]
    else:
        return None
    segs = []
    for k, s in enumerate(rest):
        if s == "index" and k == len(rest) - 1:
            continue
        if (s.startswith("(") and s.endswith(")")) or s.startswith("@"):
            continue
        segs.append(s)
    return "/" + "/".join(segs)


# -- Java / Kotlin ------------------------------------------------------------------------------------

_JVM_ANN = re.compile(r"@(RequestMapping|GetMapping|PostMapping|PutMapping|PatchMapping|DeleteMapping|Path|GET|POST|"
                      r"PUT|PATCH|DELETE|HEAD|OPTIONS)\b")
_JVM_STR = re.compile(r'"((?:[^"\\\n]|\\.)*)"')


_JVM_CLASS = re.compile(r"(?<![\w.])(?:class|interface|object|record|enum)\b")  # not `Pet.class`
_JVM_BODY = re.compile(r"[{;(=]|\bfun\b|\b(?:class|interface|object)\b")


def _jvm_class_bodies(tok: _Tokens) -> list[tuple[int, int, int]]:
    """``(keyword offset, body start, body end)`` of every class, interface, object, record and enum (Java or
    Kotlin); a declaration without a body is left out."""
    code, n, out = tok.code, len(tok.code), []
    for m in _JVM_CLASS.finditer(code):
        i = m.end()
        while True:
            b = _JVM_BODY.search(code, i)
            if not b:
                break
            c = code[b.start()]
            if c == "(":  # a Kotlin primary constructor or a super class call
                e = tok.close(b.start())
                if e < 0:
                    break
                i = e + 1
                continue
            if c == "{":
                e = tok.close(b.start())
                out.append((m.start(), b.start(), n if e < 0 else e))
            break
    return out


def _skip_annotations(tok: _Tokens, i: int) -> int:
    """The index after the whitespace, annotations (with their arguments) and modifiers from ``i``."""
    code = tok.code
    n = len(code)
    while i < n:
        m = re.compile(r"\s*@[\w.]+\s*").match(code, i)
        if not m:
            m = re.compile(r"\s*(?:public|protected|private|static|final|abstract|open|override|suspend|"
                           r"synchronized|default)\b").match(code, i)
            if not m:
                break
            i = m.end()
            continue
        i = m.end()
        if code[i:i + 1] == "(":
            c = tok.close(i)
            if c < 0:
                return n
            i = c + 1
    return i


def jvm_facts(text: str, rel: str) -> dict:
    """Spring (``@GetMapping``, ``@RequestMapping``) and JAX-RS (``@Path`` with ``@GET``...) routes of a file."""
    if "Mapping" not in text and "@Path" not in text:
        return {}
    from verinoda.jvm_mixins import strip_comments

    code = strip_comments(text)
    tok = _Tokens(code, js=False)
    starts = _line_starts(code)
    bodies = _jvm_class_bodies(tok)
    class_prefix: dict[int, str] = {}  # class keyword offset -> its class-level @RequestMapping / @Path

    def prefix_at(pos: int) -> str:
        """The prefix of the innermost class whose body holds ``pos`` (none: "")."""
        inner = max((b for b in bodies if b[1] < pos < b[2]), key=lambda b: b[1], default=None)
        return class_prefix.get(inner[0], "") if inner else ""

    methods: dict[int, dict] = {}  # declaration offset -> what its annotations say
    for m in _JVM_ANN.finditer(code):
        name, args, after = m.group(1), "", m.end()
        j = after + len(code[after:after + 200]) - len(code[after:after + 200].lstrip())
        if code[j:j + 1] == "(":
            close = tok.close(j)
            if close < 0:
                continue
            args, after = code[j + 1:close], close + 1
        k = _skip_annotations(tok, after)
        hm = re.compile(r"[^({;=]*").match(code, k)
        head = hm.group(0)
        paths = _JVM_STR.findall(re.sub(r"\b(?:produces|consumes|params|headers|name)\s*=\s*(?:\{[^}]*\}|\"[^\"]*\")",
                                        "", args))
        cm = _JVM_CLASS.search(head)
        if cm:
            if name in ("RequestMapping", "Path"):
                class_prefix[k + cm.start()] = paths[0] if paths else ""
            continue
        dm = re.search(r"([A-Za-z_$][\w$]*)\s*$", head)
        if not dm or code[hm.end():hm.end() + 1] != "(":
            continue
        pos = k + dm.start(1)
        rec = methods.setdefault(pos, {"paths": None, "verbs": set(), "line": _line_of(starts, m.start()),
                                       "decl": _line_of(starts, pos), "prefix": prefix_at(pos), "fw": "spring",
                                       "any": False})
        if name.endswith("Mapping"):
            rec["paths"] = (rec["paths"] or []) + (paths or [""])
            if name == "RequestMapping":
                vs = re.findall(r"RequestMethod\s*\.\s*(\w+)", args)
                rec["verbs"].update(v.upper() for v in vs)
                rec["any"] = rec["any"] or not vs
            else:
                rec["verbs"].add(name[:-7].upper())
        else:
            rec["fw"] = "jax-rs"
            if name == "Path":
                rec["paths"] = paths or [""]
            else:
                rec["verbs"].add(name)
    out = []
    for rec in methods.values():
        if rec["fw"] == "jax-rs":
            if not rec["verbs"]:
                continue  # a sub-resource locator: no HTTP method of its own
            paths = rec["paths"] or [""]
        elif rec["paths"] is None:
            continue
        else:
            paths = rec["paths"]
        verbs = None if rec["any"] or not rec["verbs"] else sorted(rec["verbs"])
        for p in paths:
            out.append({"fw": rec["fw"], "obj": None, "methods": verbs, "path": join_path(rec["prefix"], p),
                        "line": rec["line"], "handler": {"kind": "def", "line": rec["decl"]}})
    return {"routes": out} if out else {}


def file_facts(text: str, rel: str) -> dict:
    """The cross-service facts of one file, by its suffix (an empty dict when it has none)."""
    low = rel.lower()
    if len(text) > MAX_CHARS or low.endswith((".min.js", ".bundle.js")) or (
            len(text) > 20_000 and text.count("\n") * 400 < len(text)):  # generated or minified
        return {}
    try:
        if low.endswith(PY_SUFFIXES):
            return py_facts(text, rel)
        if low.endswith(JS_SUFFIXES):
            return js_facts(text, rel)
        if low.endswith(JVM_SUFFIXES):
            return jvm_facts(text, rel)
    except RecursionError:
        return {}
    return {}


# -- linking ------------------------------------------------------------------------------------------

def _bare(label: str) -> str:
    return str(label or "").strip().lstrip(".").split("(")[0]


class _Linker:
    def __init__(self, g, facts: dict[str, dict], read=None):
        self.g = g
        self.facts = facts
        self.read = read or (lambda f: _read_text(g, f))
        self._values: dict[str, tuple[dict, dict]] = {}
        self._py_pool: list[str] | None = None
        self._scopes: dict[str, frozenset[str]] = {}
        self.files: dict[str, str] = {}
        for n, d in g.G.nodes(data=True):
            f = d.get("source_file")
            if f and f in facts and f not in self.files and g.is_file_node(n):
                self.files[f] = n
        self.edges: list[tuple[str, str, dict]] = []
        self.report: dict = {"routes": 0, "clients": 0, "linked": 0, "ambiguous": [], "unmatched": [],
                             "method_mismatch": [], "unresolved_urls": 0, "external": 0, "route_table": [],
                             "events": {"linked": 0, "names": 0}, "rpc": {"linked": 0, "ambiguous": 0}}
        self._all_stems: dict[str, set[str]] | None = None

    # nodes
    def node_at(self, f: str, line: int) -> str | None:
        return self.g.symbol_at(f, line) or self.files.get(f) or self._file_node(f)

    def _file_node(self, f: str) -> str | None:
        return self.files.get(f)

    def named(self, f: str, name: str, module: str | None = None) -> str | None:
        g = self.g
        own = [n for n in g.symbols_in(f) if _bare(g.label(n)) == name]
        if len(own) == 1 and not module:
            return own[0]
        fn = self._file_node(f)
        cands: list[str] = []
        if fn:
            for v, d in g.out_edges(fn, {"imports"}):
                if _bare(g.label(v)) == name and g.file(v):
                    cands.append(v)
            if not cands:
                imported = sorted({g.file(v) for v, _d in g.out_edges(fn, {"imports_from"}) if g.file(v)})
                stem = (module or "").rpartition(".")[2]
                for only_stem in ((True, False) if module else (False,)):
                    for vf in imported:
                        if only_stem and posixpath.splitext(posixpath.basename(vf))[0] != stem:
                            continue
                        cands += [n for n in g.symbols_in(vf) if _bare(g.label(n)) == name]
                    if cands:
                        break
        if not cands and module:  # views.detail: a file named views, the nearest one first
            stem = module.rpartition(".")[2]
            here = posixpath.dirname(f)
            files = sorted(self._stem_files(stem), key=lambda x: (posixpath.dirname(x) != here, x))
            for vf in files:
                hit = [n for n in g.symbols_in(vf) if _bare(g.label(n)) == name]
                if hit:
                    cands = hit
                    break
        if not cands and own:
            cands = own
        cands = sorted(set(cands))
        return cands[0] if len(cands) == 1 else None

    def _stem_files(self, stem: str) -> list[str]:
        """Files named ``stem`` (any suffix among those read here), from the graph's files."""
        if self._all_stems is None:
            self._all_stems = defaultdict(set)
            for _n, d in self.g.G.nodes(data=True):
                sf = d.get("source_file") or ""
                if sf.lower().endswith(SUFFIXES):
                    self._all_stems[posixpath.splitext(posixpath.basename(sf))[0]].add(sf)
        return sorted(self._all_stems.get(stem, ()))

    def handler(self, f: str, h: dict, line: int) -> tuple[str | None, str]:
        k = h.get("kind")
        if k == "def":
            return self.g.symbol_at(f, h["line"]), "declared"
        if k == "name":
            n = self.named(f, h["name"], h.get("module"))
            if n:
                return n, f"named `{h['name']}`"
            return self.node_at(f, line), f"`{h['name']}` not resolved; the enclosing code"
        return self.node_at(f, h.get("line") or line), "inline"

    # files
    def resolve_js(self, f: str, spec: str) -> str | None:
        base = posixpath.normpath(posixpath.join(posixpath.dirname(f), spec))
        for cand in [base] + [base + e for e in JS_SUFFIXES] + [base + "/index" + e for e in JS_SUFFIXES]:
            if cand in self.facts:
                return cand
        return None

    def resolve_py(self, f: str, mod: str, pool=None) -> list[str]:
        """The files of ``pool`` (default: the files with facts) module ``mod`` imported from ``f`` can be."""
        pool = self.facts if pool is None else pool
        level = len(mod) - len(mod.lstrip("."))
        body = mod.lstrip(".").replace(".", "/")
        if level:
            base = posixpath.dirname(f)
            for _ in range(level - 1):
                base = posixpath.dirname(base)
            target = posixpath.join(base, body) if body else base
            cands = [target + ".py", target + "/__init__.py"]
            return [c for c in cands if c in pool]
        return sorted(x for x in pool if x.endswith("/" + body + ".py") or x == body + ".py"
                      or x.endswith("/" + body + "/__init__.py") or x == body + "/__init__.py")

    # prefixes another module spells (`prefix=settings.API_V1_STR`)
    def _module_values(self, f: str) -> tuple[dict[str, list], dict[str, str]]:
        """The values (:func:`py_values`) and imports of a Python file, read once; empty when it cannot be read."""
        if f not in self._values:
            vals: tuple[dict, dict] = ({}, {})
            try:
                text = self.read(f)
                if text is not None and len(text) <= MAX_CHARS:
                    tree = ast.parse(text)
                    vals = (py_values(tree), _py_imports(tree.body))
            except (OSError, SyntaxError, ValueError, RecursionError):
                pass
            self._values[f] = vals
        return self._values[f]

    def _py_files(self) -> list[str]:
        if self._py_pool is None:
            self._py_pool = sorted({f for f in candidate_files(self.g) if f.lower().endswith(PY_SUFFIXES)}
                                   | {f for f in self.facts if f.lower().endswith(PY_SUFFIXES)})
        return self._py_pool

    def imported_value(self, f: str, name: str, origin: str, depth: int = 0) -> tuple[str, str] | None:
        """The value of ``name`` (``settings.API_V1_STR``) in ``f``, whose head is imported from ``origin``, and a
        note on where it is spelled; None when no one module spells it (re-exports followed three times)."""
        _head, _, rest = name.partition(".")
        last = origin.rstrip(".").rpartition(".")[2]
        mod = origin[:len(origin) - len(last)]
        mod = mod[:-1] if mod.strip(".") else mod
        tries = ([(origin, rest)] if rest else []) + ([(mod, last + ("." + rest if rest else ""))] if mod else [])
        for m, key in tries:
            hits = self.resolve_py(f, m, self._py_files())
            if len(hits) != 1:
                continue
            vals, imps = self._module_values(hits[0])
            if key in vals:
                return vals[key][0], _value_note(key, vals[key], hits[0])
            khead = key.partition(".")[0]
            if khead in imps and depth < 3:
                got = self.imported_value(hits[0], key, imps[khead], depth + 1)
                if got:
                    return got
        return None

    def _prefix(self, f: str, rec: dict, field: str = "prefix") -> tuple[str | None, tuple[str, ...], str | None]:
        """A mount's or a router's prefix: ``(value, notes, expression not resolved)``; the value is None for a
        Flask ``register_blueprint`` without ``url_prefix``."""
        notes = tuple(rec.get("prefix_from") or ())
        pieces = rec.get("prefix_pieces")
        if not pieces:
            return rec.get(field), notes, None
        out = []
        for p in pieces:
            if p[0] == "lit":
                out.append(p[1])
                continue
            got = self.imported_value(f, p[1], p[2]) if p[0] == "ref" and len(p) > 2 else None
            if got is None:
                return "", notes, rec.get("prefix_expr") or p[1]
            out.append(got[0])
            notes += (got[1],)
        return "".join(out), notes, None

    # route table
    def routes(self) -> list[dict]:
        mounts: dict[tuple[str, str], list[tuple]] = defaultdict(list)
        for f, fx in self.facts.items():
            for mt in fx.get("mounts") or []:
                targets = self._mount_targets(f, mt)
                if targets:
                    mp, notes, unresolved = self._prefix(f, mt)
                for tf, tobj in targets:
                    mounts[(tf, tobj)].append((f, mt.get("on") or "", mp, bool(mt.get("replaces")), notes,
                                               (unresolved,) if unresolved else ()))

        def prefixes(f: str, obj: str | None, seen: frozenset) -> list[tuple[str, bool, tuple, tuple, frozenset]]:
            """``(prefix, mount known, notes, expressions not resolved, files of the chain)`` for each chain of
            mounts; the files are where the route's objects and their mounts are, up to the app."""
            if obj is None:
                return [("", True, (), (), frozenset({f}))]
            info = (self.facts[f].get("objs") or {}).get(obj) or {}
            own, own_notes, own_unres = self._prefix(f, info)
            own, own_unres = own or "", (own_unres,) if own_unres else ()
            ups = mounts.get((f, obj)) or []
            if not ups or (f, obj) in seen:
                # an object this file does not construct is a router by its name (`router`, `bp`), else an app
                kind = info.get("kind") or ("router" if re.search(r"(?i)router|blueprint|^bp$|_bp$", obj) else "app")
                return [(own, kind != "router", own_notes, own_unres, frozenset({f}))]
            out = []
            for pf, pobj, mp, replaces, m_notes, m_unres in ups:
                for pp, known, notes, unres, chain in prefixes(pf, pobj or None, seen | {(f, obj)}):
                    chain = chain | {f}
                    # Flask's register_blueprint(url_prefix=) replaces the blueprint's own prefix; FastAPI's
                    # include_router(prefix=) and Express's app.use('/p', router) come before it
                    if replaces and mp is not None:
                        out.append((join_path(pp, mp), known, notes + m_notes, unres + m_unres, chain))
                    else:
                        out.append((join_path(pp, mp or "", own), known, notes + m_notes + own_notes,
                                    unres + m_unres + own_unres, chain))
            return out

        # Django: a URLconf's prefix is the chain of includes that name it
        includes: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for f, fx in self.facts.items():
            for inc in fx.get("includes") or []:
                mod = inc["module"]
                for tf in self.resolve_py(f, mod):
                    includes[tf].append((f, inc["prefix"]))

        def dj_prefixes(f: str, seen: frozenset) -> list[tuple[str, frozenset]]:
            ups = includes.get(f)
            if not ups or f in seen:
                return [("", frozenset({f}))]
            return [(join_path(pp, p), chain | {f}) for uf, p in ups for pp, chain in dj_prefixes(uf, seen | {f})]

        nest = sorted({fx["nest_prefix"] for fx in self.facts.values() if fx.get("nest_prefix")})
        nest_prefix = nest[0] if len(nest) == 1 else ""
        table = []
        for f, fx in sorted(self.facts.items()):
            # Python frameworks serve the path as written: FastAPI's prefix="/items" + "/" is /items/ (Express
            # serves a router's "/" at the mount path itself)
            keep = f.lower().endswith(PY_SUFFIXES)
            kind = code_kind(f)
            for r in fx.get("routes") or []:
                node, how = self.handler(f, r["handler"], r["line"])
                if node is None:
                    continue
                if r.get("urlconf"):
                    pres = [(p, True, (), (), chain) for p, chain in dj_prefixes(f, frozenset())]
                elif r.get("nest"):
                    pres = [(nest_prefix, True, (), (), frozenset({f}))]
                else:
                    pres = prefixes(f, r.get("obj"), frozenset())
                for pre, known, notes, unresolved, chain in pres:
                    path = join_path(pre, r["path"], keep_slash=keep) if r["path"] not in ("", "/") or pre else "/"
                    if r["path"] == "*":
                        path = join_path(pre, "*")
                    mount = ([] if known else ["not found"]) + [f"prefix not resolved: {u}" for u in unresolved]
                    table.append({"fw": r["fw"], "methods": r["methods"], "path": path, "at": f"{f}:{r['line']}",
                                  "handler": node, "how": how, "segs": route_segments(path), "chain": chain,
                                  **({"mount": "; ".join(dict.fromkeys(mount))} if mount else {}),
                                  **({"prefix_from": list(dict.fromkeys(notes))} if notes else {}),
                                  **({"code": kind} if kind else {})})
            nx_path = _next_route(f)
            if nx_path is not None:
                for e in fx.get("next") or []:
                    node = self.g.symbol_at(f, e["line"]) or self._file_node(f)
                    if node:
                        table.append({"fw": "next.js", "methods": [e["method"]] if e["method"] else None,
                                      "path": nx_path, "at": f"{f}:{e['line']}", "handler": node, "how": "file route",
                                      "segs": route_segments(nx_path), "chain": frozenset({f}),
                                      **({"code": kind} if kind else {})})
        return table

    def _mount_targets(self, f: str, mt: dict) -> list[tuple[str, str]]:
        if mt.get("local"):
            return [(f, mt["obj"])]
        if "path" in mt:  # JavaScript
            tf = self.resolve_js(f, mt["path"])
            if not tf:
                return []
            fx = self.facts[tf]
            objs = [o for o, d in (fx.get("objs") or {}).items() if d.get("kind") == "router"]
            exp = mt.get("export")
            if exp and exp != "default" and exp in objs:
                return [(tf, exp)]
            used = sorted({r.get("obj") for r in fx.get("routes") or [] if r.get("obj") in objs})
            return [(tf, used[0])] if len(used) == 1 else [(tf, objs[0])] if len(objs) == 1 else []
        hits = []
        for mod in mt.get("mods") or []:
            found = [(tf, mt["obj"]) for tf in self.resolve_py(f, mod)] or self._reexported(f, mod, mt["obj"])
            for tf, obj in found:
                fx = self.facts[tf]
                if obj in (fx.get("objs") or {}) or any(r.get("obj") == obj for r in fx.get("routes") or []):
                    hits.append((tf, obj))
        return hits if len(hits) == 1 else []

    def _reexported(self, f: str, mod: str, name: str, depth: int = 0) -> list[tuple[str, str]]:
        """``(file, name there)`` for ``name`` of module ``mod`` (imported from ``f``) when that module only
        re-exports it (a package ``__init__.py`` with ``from .main import api_router``); three re-exports at most."""
        hits = self.resolve_py(f, mod, self._py_files())
        if len(hits) != 1 or depth >= 3:
            return []
        _vals, imps = self._module_values(hits[0])
        origin = imps.get(name)
        if not origin:
            return []
        src_mod, _, src_name = origin.rpartition(".")
        if not src_mod or not src_mod.strip("."):  # `from . import x`: the name is a module, not an object
            return []
        found = [(tf, src_name) for tf in self.resolve_py(hits[0], src_mod)]
        return found or self._reexported(hits[0], src_mod, src_name, depth + 1)

    def link_http(self) -> None:
        table = self.routes()
        self.report["routes"] = len(table)
        self.report["route_table"] = [{k: v for k, v in r.items() if k not in ("segs", "chain")} for r in table]
        by_len: dict[int, list[dict]] = defaultdict(list)
        rest: list[dict] = []
        for r in table:
            if any(s[0] == "rest" for s in r["segs"]) or any(s[2:] == ["opt"] for s in r["segs"]):
                rest.append(r)
            else:
                by_len[len(r["segs"])].append(r)
        instances: dict[str, dict] = {f: fx.get("instances") or {} for f, fx in self.facts.items() if fx.get("instances")}
        for f, fx in sorted(self.facts.items()):
            for c in fx.get("clients") or []:
                self.report["clients"] += 1
                pieces = c["pieces"]
                base = self._client_base(f, fx, c, instances)
                if base:
                    pieces = join_base(base, pieces)
                segs, notes = client_path(pieces)
                at = f"{f}:{c['line']}"
                if segs is None:
                    self.report["external" if notes and notes[0].startswith(EXTERNAL) else "unresolved_urls"] += 1
                    continue
                cands = []
                for r in by_len.get(len(segs), []) + rest:
                    lits = match_segments(r["segs"], segs)
                    if lits is not None:
                        cands.append((r, lits))
                # a catch-all (`*`, `{path:path}`) gives way to a route that names the path; one that names nothing
                # before the wildcard (an SPA fallback, `app.get('*')`) is no link at all
                lits_of = {id(r): lits for r, lits in cands}
                named = [r for r, _ in cands if not any(s[0] == "rest" for s in r["segs"])]
                cands = named or [r for r, lits in cands if lits > 0]
                kind = code_kind(f)
                call = {"at": at, "method": c["method"], "url": _show(pieces), "lib": c["lib"],
                        **({"code": kind} if kind else {})}
                dropped = 0
                if cands and (kind or c["lib"] == "supertest"):
                    # test and example code calls the app it builds or imports: a route of an unrelated example
                    # app is no candidate when that app is known. A route is the app's when a file of its mount
                    # chain (its own file, the files that mount it, up to the app) is in the test's import closure
                    scope = self._scope(f)
                    local = [r for r in cands if r["chain"] & scope]
                    other = [r for r in cands if not r["chain"] & scope]
                    if local and other and (max(lits_of[id(r)] for r in other)
                                            > max(lits_of[id(r)] for r in local)):
                        pass  # a route outside names more of the path than any inside: no narrowing, no guess
                    elif local:
                        cands, dropped = local, len(other)
                    elif (c["lib"] == "supertest" and (len(scope) > 1 or self._serves(scope))
                          and all("not found" not in (r.get("mount") or "") for r in cands)):
                        # request(app): the app is built here or imported, and every route that fits is mounted
                        # on another app; one whose mount was not found might still be this app's
                        self._cap("unmatched", dict(call, why=f"no route read in the app under test fits (this file "
                                                              f"and the {len(scope) - 1} it imports); "
                                                              f"{len(cands)} on other apps do"))
                        continue
                if not cands:
                    self._cap("unmatched", call)
                    continue
                fit = [r for r in cands if c["method"] is None or r["methods"] is None or c["method"] in r["methods"]
                       or (c["method"] == "HEAD" and "GET" in r["methods"])]
                if not fit:
                    self._cap("method_mismatch", dict(call, routes=[_route_row(r) for r in cands[:5]]))
                    continue
                # a route whose prefix the text does not spell matches without it: no edge from that alone
                unres = [r for r in fit if "prefix not resolved" in (r.get("mount") or "")]
                if unres and len(unres) < len(fit):
                    fit = [r for r in fit if r not in unres]
                elif unres:
                    shown = "; ".join(f"{_route_row(r)['route']} ({r['mount']})" for r in unres[:3])
                    self._cap("unmatched", dict(call, why=f"only routes whose prefix is not resolved fit: {shown}"))
                    continue
                handlers = sorted({r["handler"] for r in fit})
                caller = self.node_at(f, c["line"])
                if len(handlers) > 1:
                    # every candidate is kept here; `routes` shows a bounded view (bounded()), the sidecar 8
                    self.report["ambiguous"].append(dict(call, caller=caller, protocol="http", candidates=[
                        dict(_route_row(r), handler=r["handler"], **({"code": r["code"]} if r.get("code") else {}))
                        for r in fit]))
                    continue
                r = fit[0]
                if caller is None or caller == r["handler"]:
                    continue
                d = {"relation": HTTP_RELATION, "confidence": "INFERRED", "confidence_score": 0.6, "_origin": ORIGIN,
                     "source_file": f, "source_location": f"L{c['line']}", "protocol": "http",
                     "method": c["method"] or "?", "url": _show(pieces), "route": _route_row(r)["route"],
                     "route_at": r["at"], "framework": r["fw"],
                     "context": f"{c['method'] or 'any method'} {_show(pieces)} -> {_route_row(r)['route']} "
                                f"({r['fw']}, {r['at']})"}
                extra = list(notes)
                if "not found" in (r.get("mount") or ""):
                    extra.append("the router's mount point was not found: its path is the router's own")
                if unres:
                    extra.append(f"{len(unres)} route(s) whose prefix is not resolved also fit without it")
                if dropped:
                    extra.append(f"{dropped} route(s) outside the app under test also match the path")
                if c["method"] is None:
                    extra.append("the call's method is computed")
                if extra:
                    d["notes"] = extra
                self.edges.append((caller, r["handler"], d))
                self.report["linked"] += 1

    def _client_base(self, f: str, fx: dict, c: dict, instances: dict) -> list | None:
        recv = c.get("recv")
        if not recv:
            return None
        own = fx.get("instances") or {}
        if recv in own:
            return own[recv] if own[recv] is not None else [["dyn", "baseURL"]]
        imp = (fx.get("imports") or {}).get(recv)
        if imp:
            tf = self.resolve_js(f, imp[0])
            inst = instances.get(tf) or {}
            if imp[1] in inst:
                b = inst[imp[1]]
            elif imp[1] == "default" and len(inst) == 1:
                b = next(iter(inst.values()))
            else:
                return None
            return b if b is not None else [["dyn", "baseURL"]]
        return None

    def _scope(self, f: str) -> frozenset[str]:
        """``f`` and every file it imports, directly or through others: relative JavaScript imports and
        ``require``s, and the graph's ``imports_from`` edges."""
        if f in self._scopes:
            return self._scopes[f]
        seen, frontier = {f}, [f]
        while frontier:
            nxt = []
            for x in frontier:
                deps = {self.resolve_js(x, imp[0]) for imp in ((self.facts.get(x) or {}).get("imports") or {}).values()
                        if isinstance(imp, list) and imp}
                fn = self.files.get(x) or self._graph_file(x)
                if fn:
                    deps |= {self.g.file(v) for v, _d in self.g.out_edges(fn, {"imports_from"})}
                for d in deps:
                    if d and d not in seen:
                        seen.add(d)
                        nxt.append(d)
            frontier = nxt
        self._scopes[f] = frozenset(seen)
        return self._scopes[f]

    def _graph_file(self, f: str) -> str | None:
        if not hasattr(self, "_file_nodes"):
            self._file_nodes = {d.get("source_file"): n for n, d in self.g.G.nodes(data=True)
                                if d.get("source_file") and self.g.is_file_node(n)}
        return self._file_nodes.get(f)

    def _serves(self, scope: frozenset[str]) -> bool:
        """Whether a file of ``scope`` builds an app or declares a route."""
        return any((self.facts.get(x) or {}).get("routes") or (self.facts.get(x) or {}).get("objs") for x in scope)

    def _cap(self, key: str, entry: dict) -> None:
        """Every entry is kept: the cuts are made, and counted, where the report is shown (:func:`bounded`) or
        stored (the sidecar keeps :data:`REPORT_CAP` ambiguous calls)."""
        self.report[key].append(entry)

    def link_events(self) -> None:
        listeners: dict[str, list[tuple[str, str]]] = defaultdict(list)
        emits: list[tuple[str, str, int]] = []
        for f, fx in sorted(self.facts.items()):
            for e in fx.get("events") or []:
                if e["name"] in GENERIC_EVENTS:
                    continue
                if e["role"] == "listen":
                    node, _how = self.handler(f, e.get("handler") or {"kind": "inline"}, e["line"])
                    if node:
                        listeners[e["name"]].append((node, f"{f}:{e['line']}"))
                else:
                    emits.append((e["name"], f, e["line"]))
        names = set()
        for name, f, line in emits:
            caller = self.node_at(f, line)
            targets = listeners.get(name) or []
            for node, at in targets:
                if caller is None or caller == node:
                    continue
                self.edges.append((caller, node, {
                    "relation": EVENT_RELATION, "confidence": "INFERRED", "confidence_score": 0.5, "_origin": ORIGIN,
                    "source_file": f, "source_location": f"L{line}", "protocol": "event", "event": name,
                    "route_at": at, "listeners": len(targets),
                    "context": f"emits '{name}' -> listener at {at}" + (f" (one of {len(targets)})"
                                                                        if len(targets) > 1 else "")}))
                self.report["events"]["linked"] += 1
                names.add(name)
        self.report["events"]["names"] = len(names)

    def _rpc_edge(self, f: str, line: int, cands: list[tuple[str, str]], protocol: str, what: str) -> None:
        nodes = sorted({n for n, _ in cands})
        caller = self.node_at(f, line)
        if len(nodes) > 1:
            self.report["rpc"]["ambiguous"] += 1
            self._cap("ambiguous", {"at": f"{f}:{line}", "protocol": protocol, "url": what, "caller": caller,
                                    "candidates": [{"route": what, "at": at, "handler": n} for n, at in cands]})
            return
        if not nodes:
            self._cap("unmatched", {"at": f"{f}:{line}", "protocol": protocol, "url": what})
            return
        if caller is None or caller == nodes[0]:
            return
        self.edges.append((caller, nodes[0], {
            "relation": RPC_RELATION, "confidence": "INFERRED", "confidence_score": 0.6, "_origin": ORIGIN,
            "source_file": f, "source_location": f"L{line}", "protocol": protocol, "route": what,
            "route_at": cands[0][1], "context": f"{protocol} {what} -> {cands[0][1]}"}))
        self.report["rpc"]["linked"] += 1

    def link_rpc(self) -> None:
        facts = self.facts
        # tRPC: router keys flattened from the routers no other router nests
        routers: dict[str, list[tuple[str, dict]]] = defaultdict(list)
        for f, fx in facts.items():
            for r in fx.get("trpc_routers") or []:
                routers[r["var"]].append((f, r))
        nested = {k["ref"] for lst in routers.values() for _f, r in lst for k in r["keys"] if "ref" in k}
        procs: dict[str, list[tuple[str, str]]] = defaultdict(list)

        def walk(var: str, prefix: str, seen: frozenset) -> None:
            if len(routers.get(var) or []) != 1 or var in seen:
                return
            f, r = routers[var][0]
            for k in r["keys"]:
                path = f"{prefix}{k['key']}"
                if "ref" in k:
                    walk(k["ref"], path + ".", seen | {var})
                else:
                    node = self.node_at(f, k["line"])
                    if node:
                        procs[path].append((node, f"{f}:{k['line']}"))

        for var in routers:
            if var not in nested:
                walk(var, "", frozenset())
        if procs:
            for f, fx in sorted(facts.items()):
                for c in fx.get("trpc_calls") or []:
                    if c["path"] in procs or c["root"] == "trpc":
                        self._rpc_edge(f, c["line"], procs.get(c["path"]) or [], "trpc", c["path"])
        # gRPC (Python): servicer methods by service name
        grpc: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        for f, fx in facts.items():
            for s in fx.get("grpc_services") or []:
                for m in s["methods"]:
                    node = self.g.symbol_at(f, m["line"])
                    if node:
                        grpc[(s["service"], m["name"])].append((node, f"{f}:{m['line']}"))
        for f, fx in sorted(facts.items()):
            for c in fx.get("grpc_calls") or []:
                self._rpc_edge(f, c["line"], grpc.get((c["service"], c["method"])) or [], "grpc",
                               f"{c['service']}.{c['method']}")
        # GraphQL: an operation's root fields to the resolver maps
        gql: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        for f, fx in facts.items():
            for r in fx.get("gql_resolvers") or []:
                node = self.node_at(f, r["line"])
                if node:
                    gql[(r["type"], r["field"])].append((node, f"{f}:{r['line']}"))
        if gql:
            for f, fx in sorted(facts.items()):
                for o in fx.get("gql_ops") or []:
                    self._rpc_edge(f, o["line"], gql.get((o["type"], o["field"])) or [], "graphql",
                                   f"{o['type']}.{o['field']}")


def _route_row(r: dict) -> dict:
    ms = "|".join(r["methods"]) if r.get("methods") else "ANY"
    return {"route": f"{ms} {r['path']}", "at": r["at"], "framework": r["fw"]}


def _read_text(g, f: str, read=None) -> str | None:
    """A project file's text (``read`` as :func:`collect` takes it, else from ``g.root``); None when unreadable."""
    try:
        data = read(f) if read else (g.root / f).read_bytes()
    except (OSError, AttributeError, TypeError):
        return None
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    return data.lstrip("﻿") if isinstance(data, str) else None


def link(g, facts: dict[str, dict], read=None) -> tuple[list[tuple[str, str, dict]], dict]:
    """The cross-service edges of the graph and a report (route table, ambiguous, unmatched, method mismatches).
    ``read`` reads a file a prefix names (``settings.API_V1_STR``) that has no facts of its own."""
    lk = _Linker(g, {f: fx for f, fx in facts.items() if fx}, read=lambda f: _read_text(g, f, read))
    lk.link_http()
    lk.link_events()
    lk.link_rpc()
    return lk.edges, lk.report


def candidate_files(g) -> list[str]:
    """The graph's code files this pass reads."""
    files = set()
    for _n, d in g.G.nodes(data=True):
        f = d.get("source_file")
        if f and d.get("file_type") == "code" and f.lower().endswith(SUFFIXES):
            files.add(f)
    return sorted(files)


def collect(g, read=None, old: dict | None = None) -> tuple[dict[str, dict], list, dict, int]:
    """Facts of every candidate file (reused from ``old`` when the sha256 is the same), the edges and the report.
    Returns ``(files, edges, report, parsed)``, ``files`` being ``{path: {"sha256", "facts"}}``."""
    import hashlib

    old = old or {}
    files: dict[str, dict] = {}
    parsed = 0
    for f in candidate_files(g):
        try:
            data = read(f) if read else (g.root / f).read_bytes()
        except OSError:
            continue
        if data is None:
            continue
        if isinstance(data, str):
            data = data.encode("utf-8")
        sha = hashlib.sha256(data).hexdigest()
        prev = old.get(f)
        if prev and prev.get("sha256") == sha:
            files[f] = prev
            continue
        parsed += 1
        text = data.decode("utf-8", errors="replace")
        if text.startswith("﻿"):
            text = text[1:]
        files[f] = {"sha256": sha, "facts": file_facts(text, f)}
    edges, report = link(g, {f: v["facts"] for f, v in files.items()}, read=read)
    return files, edges, report, parsed


def ambiguous_on_way(g, reach: set[str], target: str, D) -> list[dict]:
    """Ambiguous cross-service calls made from ``reach`` (nodes the trace's source reaches) with a candidate
    handler that is ``target`` or reaches it in ``D``: the reason a trace stops there."""
    out = []
    import networkx as nx

    for a in g.__dict__.get("_cross_ambiguous") or []:
        caller = a.get("caller")
        cands = [c for c in a.get("candidates") or [] if isinstance(c, dict)]
        if not isinstance(caller, str) or caller not in reach:
            continue
        hits = []
        for c in cands:
            h = c.get("handler")
            if not isinstance(h, str):
                continue
            if h == target or (h in D and target in D and nx.has_path(D, h, target)):
                hits.append(c)
        if hits:
            out.append({k: v for k, v in a.items() if k != "candidates"} | {"candidates": cands})
    return out[:5]


AMBIGUOUS_GROUPS = 50      # groups of ambiguous calls `routes` shows unless --all
AMBIGUOUS_CANDIDATES = 5   # candidates shown per group
AMBIGUOUS_ATS = 5          # further call sites named per group
LIST_CAP = 50              # unmatched calls and method mismatches shown unless --all


def bounded(report: dict, *, everything: bool = False) -> dict:
    """``report`` bounded for ``verinoda routes``: ambiguous calls with the same method, URL and candidates are one
    entry (the first call, ``calls`` in all, ``also_at`` for the next few), at most :data:`AMBIGUOUS_GROUPS` groups
    of :data:`AMBIGUOUS_CANDIDATES` candidates, at most :data:`LIST_CAP` unmatched calls and method mismatches;
    every cut is counted (``candidates_total``, ``also_at_more``, ``<list>_more``) and ``<list>_by_code`` counts
    the calls made from test and example code. ``everything``: every call and candidate, nothing grouped."""
    amb = [a for a in report.get("ambiguous") or [] if isinstance(a, dict)]
    out = dict(report, ambiguous_calls=len(amb))
    for key in ("ambiguous", "unmatched", "method_mismatch"):
        rows = [a for a in report.get(key) or [] if isinstance(a, dict)]
        if rows:
            out[key + "_by_code"] = dict(sorted(Counter(a.get("code") or "other" for a in rows).items()))
        if not everything and len(rows) > LIST_CAP and key != "ambiguous":
            out.update({key: rows[:LIST_CAP], key + "_more": len(rows) - LIST_CAP})
    if everything:
        return out
    groups: dict[tuple, dict] = {}
    for a in amb:
        cands = a.get("candidates") or []
        key = (a.get("protocol"), a.get("method"), a.get("url"),
               tuple(sorted((str(c.get("handler")), str(c.get("at"))) for c in cands if isinstance(c, dict))))
        g = groups.get(key)
        if g is None:
            groups[key] = dict(a, calls=1, candidates=cands[:AMBIGUOUS_CANDIDATES],
                               **({"candidates_total": len(cands)} if len(cands) > AMBIGUOUS_CANDIDATES else {}))
            continue
        g["calls"] += 1
        if len(g.setdefault("also_at", [])) < AMBIGUOUS_ATS:
            g["also_at"].append(a.get("at"))
        else:
            g["also_at_more"] = g.get("also_at_more", 0) + 1
    shown = list(groups.values())
    out.update(ambiguous=shown[:AMBIGUOUS_GROUPS], ambiguous_groups=len(shown),
               ambiguous_more=max(0, len(shown) - AMBIGUOUS_GROUPS))
    if not out["ambiguous_more"]:
        out.pop("ambiguous_more")
    out["bounded"] = (f"ambiguous calls grouped by method, URL and candidates, at most {AMBIGUOUS_GROUPS} groups of "
                      f"{AMBIGUOUS_CANDIDATES} candidates; at most {LIST_CAP} unmatched calls and method mismatches; "
                      f"`routes --all` lists every call and candidate")
    return out


def render(report: dict, *, show_routes: bool = True) -> str:
    """Plain text for ``verinoda routes``."""
    n_amb = report.get("ambiguous_calls", len(report["ambiguous"]))
    n_un = len(report["unmatched"]) + report.get("unmatched_more", 0)
    n_mm = len(report["method_mismatch"]) + report.get("method_mismatch_more", 0)
    out = [f"{report['routes']} route(s), {report['clients']} client call(s) with a URL: {report['linked']} linked, "
           f"{n_amb} ambiguous, {n_un} unmatched, "
           f"{n_mm} method mismatch(es), {report['unresolved_urls']} URL(s) not spelled "
           f"in the text, {report.get('external', 0)} to a public host (another service's)"]
    ev, rpc = report.get("events") or {}, report.get("rpc") or {}
    if ev.get("linked") or rpc.get("linked"):
        out.append(f"events: {ev.get('linked', 0)} edge(s) over {ev.get('names', 0)} name(s); "
                   f"tRPC/gRPC/GraphQL: {rpc.get('linked', 0)} edge(s)")
    if show_routes and report.get("route_table"):
        out.append("routes:")
        for r in report["route_table"]:
            ms = "|".join(r["methods"]) if r.get("methods") else "ANY"
            out.append(f"  {ms} {r['path']}  {r['at']}  [{r['fw']}]" + (f" ({r['code']})" if r.get("code") else "")
                       + (f" (mount {r['mount']})" if r.get("mount") else ""))
    grouped = "bounded" in report
    for key, title in (("ambiguous", "ambiguous (no edge; " + ("grouped, capped: --all lists every call)"
                                                               if grouped else "every candidate listed)")),
                       ("method_mismatch", "path matches, method does not (no edge)"),
                       ("unmatched", "no route matches (no edge)")):
        rows = report.get(key) or []
        if rows:
            out.append(f"{title}:")
            for a in rows:
                calls = f"  ({a['calls']} calls)" if a.get("calls", 1) > 1 else ""
                code = f"  [{a['code']}]" if a.get("code") else ""
                out.append(f"  {a['at']}  {a.get('method') or ''} {a.get('url', '')}".rstrip() + code + calls)
                if a.get("why"):
                    out.append(f"    {a['why']}")
                if a.get("also_at"):
                    more = f" (+{a['also_at_more']} more)" if a.get("also_at_more") else ""
                    out.append(f"    also at {', '.join(str(x) for x in a['also_at'])}{more}")
                for c in (a.get("candidates") or a.get("routes") or []):
                    out.append(f"    {c['route']}  {c['at']}" + (f"  [{c['code']}]" if c.get("code") else ""))
                if a.get("candidates_total"):
                    out.append(f"    ... {a['candidates_total'] - len(a.get('candidates') or [])} more candidate(s)")
            if report.get(key + "_more"):
                unit = " group(s)" if key == "ambiguous" and grouped else ""
                out.append(f"  ... {report[key + '_more']} more{unit}")
    out.append("every edge is INFERRED (derived_by=verinoda.cross_service): read from the two texts, not verified")
    return "\n".join(out)
