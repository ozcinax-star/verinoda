"""Cross-service edges: a client call whose URL (or procedure, or event name) fits exactly one handler is an
INFERRED edge from the calling function to that handler, located at the call, with the route's declaration in
``route_at``; `trace` crosses it. A call that fits several handlers, none, or only with another method gets no
edge and is reported."""

from __future__ import annotations

import json
import os
import time

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, cross_service, index, retrieval, search_index, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

WEB = {
    "web/src/api.js": """import axios from 'axios';

// fetch('/api/users/1') in a comment is no call
export async function loadUser(id) {
  const res = await fetch(`/api/users/${id}`);
  return res.json();
}

export const saveUser = async (user) => {
  return axios.post('/api/users', user);
};

export function deleteUser(id) {
  return fetch(`/api/users/${id}`, { method: 'DELETE' });
}

export function loadItem(itemId) {
  return fetch('/api/items/' + itemId);
}

export function loadNothing() {
  return fetch('http://localhost:5000/api/nothing/here?x=1');
}
""",
    "web/src/view.ts": """import { loadUser } from './api';

export function showUser(id: number) {
  return loadUser(id);
}
""",
    "server/app.py": """from flask import Flask

app = Flask(__name__)


@app.route("/api/users/<int:user_id>", methods=["GET"])
def get_user(user_id):
    return load(user_id)


def load(user_id):
    return {"id": user_id}


@app.get("/api/items/<item_id>")
def get_item(item_id):
    return {}
""",
    "server2/index.js": """const express = require('express');
const app = express();

function createUser(req, res) { res.send('ok'); }

app.post('/api/users', createUser);
app.get('/api/items/:id', (req, res) => { res.send('x'); });
app.get('*', (req, res) => { res.sendFile('index.html'); });

module.exports = app;
""",
}

ZOO = {
    # FastAPI: a router with its own prefix, mounted under another one in main.py
    "svc/app/routers/orders.py": """from fastapi import APIRouter

router = APIRouter(prefix="/orders")


@router.get("/{order_id}")
def read_order(order_id: int):
    return {}


@router.post("")
def create_order():
    return {}
""",
    "svc/app/main.py": """from fastapi import FastAPI

from .routers import orders

app = FastAPI()
app.include_router(orders.router, prefix="/v1")
""",
    "svc/client.py": """import httpx

BASE = "http://svc:8000"


def fetch_order(order_id):
    return httpx.get(f"{BASE}/v1/orders/{order_id}")


def place_order(payload):
    with httpx.Client(base_url="http://svc:8000/v1") as c:
        return c.post("/orders", json=payload)
""",
    # Django URLconf with include
    "site/urls.py": """from django.urls import include, path

urlpatterns = [
    path("shop/", include("shop.urls")),
]
""",
    "shop/urls.py": """from django.urls import path

from . import views

urlpatterns = [
    path("products/<int:pk>/", views.product_detail),
]
""",
    "shop/views.py": """def product_detail(request, pk):
    return None
""",
    "shop/sync.py": """import requests


def pull_product(pk):
    return requests.get("https://shop.example.com/shop/products/%d/" % pk)


def pull_first():
    return requests.get("/shop/products/1/")
""",
    # Express router mounted with app.use and an axios instance with a baseURL
    "api/routes/books.js": """const express = require('express');
const router = express.Router();

function listBooks(req, res) { res.json([]); }

router.get('/', listBooks);

module.exports = router;
""",
    "api/server.js": """const express = require('express');
const books = require('./routes/books');
const app = express();
app.use('/api/books', books);
""",
    "ui/client.js": """import axios from 'axios';

export const api = axios.create({ baseURL: '/api' });
""",
    "ui/books.js": """import { api } from './client';

export function getBooks() {
  return api.get('/books');
}
""",
    # NestJS controller with a global prefix
    "nest/src/main.ts": """import { NestFactory } from '@nestjs/core';

async function bootstrap() {
  const app = await NestFactory.create(AppModule);
  app.setGlobalPrefix('rest');
}
""",
    "nest/src/cats.controller.ts": """import { Controller, Get, Post } from '@nestjs/common';

@Controller('cats')
export class CatsController {
  @Get(':id')
  findOne(id: string) {
    return id;
  }

  @Post()
  create() {
    return 1;
  }
}
""",
    "nest/web/cats.ts": """export function loadCat(id) {
  return fetch(`/rest/cats/${id}`);
}
""",
    # Spring
    "jvm/src/main/java/com/example/PetController.java": """package com.example;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/pets")
public class PetController {

    @GetMapping("/{id}")
    public Pet getPet(@PathVariable long id) {
        return null;
    }

    @RequestMapping(value = "/search", method = RequestMethod.POST)
    public Pet search(String q) {
        return null;
    }
}
""",
    "jvm/web/pets.js": """export function showPet(id) {
  return fetch('/pets/' + id);
}
""",
    # Next.js API file
    "next/pages/api/hello/[name].ts": """export default function handler(req, res) {
  res.status(200).json({ ok: true });
}
""",
    "next/components/Hello.tsx": """export function Hello({ name }) {
  return fetch(`/api/hello/${name}`);
}
""",
    # events
    "bus/emitter.js": """export function placeOrder(bus, order) {
  bus.emit('order:placed', order);
  bus.emit('error', new Error('x'));
}
""",
    "bus/mailer.js": """function sendReceipt(order) { return order; }

export function wire(bus) {
  bus.on('order:placed', sendReceipt);
  bus.on('error', sendReceipt);
}
""",
    # tRPC
    "trpc/server/router.ts": """import { initTRPC } from '@trpc/server';
const t = initTRPC.create();

const userRouter = t.router({
  byId: t.procedure.input(String).query(({ input }) => input),
});

export const appRouter = t.router({
  user: userRouter,
});
""",
    "trpc/client/page.tsx": """import { trpc } from '../utils/trpc';

export function UserPage() {
  return trpc.user.byId.useQuery('1');
}
""",
    # gRPC (Python)
    "rpc/server.py": """import greeter_pb2_grpc


class Greeter(greeter_pb2_grpc.GreeterServicer):
    def SayHello(self, request, context):
        return None
""",
    "rpc/client.py": """import grpc
import greeter_pb2_grpc


def greet(name):
    channel = grpc.insecure_channel("localhost:50051")
    stub = greeter_pb2_grpc.GreeterStub(channel)
    return stub.SayHello(name)
""",
    # GraphQL
    "gql/resolvers.js": """export const resolvers = {
  Query: {
    book: (_, { id }) => id,
  },
};
""",
    "gql/client.js": """import { gql } from '@apollo/client';

export function bookQuery() {
  return gql`
    query GetBook($id: ID!) {
      book(id: $id) { title }
    }
  `;
}
""",
}


def _make(tmp_path_factory, name: str, files: dict[str, str]) -> Path:
    root = tmp_path_factory.mktemp(name) / "proj"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    workflow.init(root)
    st = open_store(root)
    try:
        workflow.scan(st, root)
    finally:
        st.close()
    search_index._HANDLES.clear()
    return root


@pytest.fixture(scope="module")
def web(tmp_path_factory) -> Path:
    return _make(tmp_path_factory, "xs_web", WEB)


@pytest.fixture(scope="module")
def zoo(tmp_path_factory) -> Path:
    return _make(tmp_path_factory, "xs_zoo", ZOO)


def _cross(g) -> dict[tuple[str, str], dict]:
    return {(g.label(u).strip(".()"), g.label(v).strip(".()")): d for u, v, d in g.edges(cross_service.RELATIONS)}


def _report(repo: Path, capsys) -> dict:
    capsys.readouterr()
    assert cli.main(["routes", "--repo", str(repo), "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_trace_crosses_from_a_frontend_fetch_to_the_flask_handler(web):
    g = index.load(web)
    res = retrieval.trace(g, "showUser", "load")
    assert res["status"] == "found"
    hops = res["paths"][0]
    assert [h["to"] for h in hops] == ["loadUser()", "get_user()", "load()"]
    x = hops[1]
    assert x["relation"] == "requests" and x["kind"] == "cross_service"
    assert x["confidence"] == "INFERRED" and x["derived_by"] == "verinoda.cross_service"
    assert x["at"] == "web/src/api.js:5"            # the client call
    assert x["route_at"] == "server/app.py:6"       # the route declaration
    assert x["method"] == "GET" and x["route"] == "GET /api/users/<int:user_id>" and x["framework"] == "flask"
    assert "inferred, not verified" in res["note"]


def test_axios_post_reaches_the_express_handler(web, capsys):
    res = retrieval.trace(index.load(web), "saveUser", "createUser")
    assert res["status"] == "found"
    (hop,) = res["paths"][0]
    assert hop["at"] == "web/src/api.js:10" and hop["route_at"] == "server2/index.js:6"
    assert hop["route"] == "POST /api/users"
    capsys.readouterr()
    assert cli.main(["trace", "saveUser", "createUser", "--repo", str(web)]) == 0
    out = capsys.readouterr().out
    assert "cross-service http: POST /api/users; handler declared at server2/index.js:6" in out


def test_a_call_no_route_matches_gets_no_edge(web, capsys):
    g = index.load(web)
    assert not [k for k in _cross(g) if k[0] == "loadNothing"]
    rep = _report(web, capsys)
    assert any(u["at"] == "web/src/api.js:22" and u["url"] == "http://localhost:5000/api/nothing/here?x=1"
               for u in rep["unmatched"])


def test_a_call_two_routes_fit_is_ambiguous_not_picked(web, capsys):
    g = index.load(web)
    assert not [k for k in _cross(g) if k[0] == "loadItem"]
    rep = _report(web, capsys)
    (amb,) = [a for a in rep["ambiguous"] if a["at"] == "web/src/api.js:18"]
    assert sorted(c["at"] for c in amb["candidates"]) == ["server/app.py:15", "server2/index.js:7"]
    res = retrieval.trace(g, "loadItem", "get_item")
    assert res["status"] == "no directed path"
    (way,) = res["cross_service_ambiguous"]
    assert way["at"] == "web/src/api.js:18" and len(way["candidates"]) == 2
    assert "names several handlers" in res["next_step"]


def test_a_method_mismatch_gets_no_edge(web, capsys):
    g = index.load(web)
    assert not [k for k in _cross(g) if k[0] == "deleteUser"]
    rep = _report(web, capsys)
    (mm,) = [m for m in rep["method_mismatch"] if m["at"] == "web/src/api.js:14"]
    assert mm["method"] == "DELETE" and mm["routes"][0]["route"] == "GET /api/users/<int:user_id>"


def test_the_comment_and_the_linked_calls(web, capsys):
    g = index.load(web)
    assert set(_cross(g)) == {("loadUser", "get_user"), ("saveUser", "createUser")}
    rep = _report(web, capsys)
    assert rep["linked"] == 2 and rep["clients"] == 5 and rep["derived_by"] == "verinoda.cross_service"
    assert {r["path"] for r in rep["route_table"]} == {"/api/users/<int:user_id>", "/api/items/<item_id>",
                                                      "/api/users", "/api/items/:id", "/*"}


def test_a_catch_all_route_links_nothing_and_gives_way(web, capsys):
    # app.get('*') (an SPA fallback) matches every GET path: it neither makes loadNothing's call a link nor
    # makes loadUser's call ambiguous
    rep = _report(web, capsys)
    assert not any(c["route"] == "GET /*" for a in rep["ambiguous"] for c in a["candidates"])
    assert any(u["at"] == "web/src/api.js:22" for u in rep["unmatched"])


def test_the_sidecar_keeps_the_edges_and_reuses_the_facts(web):
    side = json.loads(index.receiver_calls_path(web).read_text(encoding="utf-8"))
    block = side["cross_service"]
    assert len(block["edges"]) == 2 and block["counts"]["ambiguous"] == 1
    assert block["files"]["server/app.py"]["facts"]["routes"]
    g = index.load(web, augment=False)
    files, edges, _rep, parsed = cross_service.collect(g, old=block["files"])
    assert parsed == 0 and len(edges) == 2


@pytest.mark.parametrize("src, dst, at, route_at", [
    ("fetch_order", "read_order", "svc/client.py:7", "svc/app/routers/orders.py:6"),      # FastAPI mount prefixes
    ("place_order", "create_order", "svc/client.py:12", "svc/app/routers/orders.py:11"),  # httpx base_url
    ("pull_first", "product_detail", "shop/sync.py:9", "shop/urls.py:6"),                 # Django include
    ("getBooks", "listBooks", "ui/books.js:4", "api/routes/books.js:6"),                  # app.use + axios baseURL
    ("loadCat", "findOne", "nest/web/cats.ts:2", "nest/src/cats.controller.ts:5"),        # NestJS + global prefix
    ("showPet", "getPet", "jvm/web/pets.js:2", "jvm/src/main/java/com/example/PetController.java:9"),  # Spring
    ("Hello", "handler", "next/components/Hello.tsx:2", "next/pages/api/hello/[name].ts:1"),  # Next.js file route
])
def test_http_frameworks(zoo, src, dst, at, route_at):
    g = index.load(zoo)
    d = _cross(g)[(src, dst)]
    assert d["relation"] == "requests" and d["_origin"] == "verinoda.cross_service"
    assert f"{d['source_file']}:{d['source_location'][1:]}" == at and d["route_at"] == route_at


def test_a_url_built_with_percent_is_not_spelled(zoo):
    assert not [k for k in _cross(index.load(zoo)) if k[0] == "pull_product"]


@pytest.mark.parametrize("src, dst, relation, protocol", [
    ("placeOrder", "sendReceipt", "emits", "event"),
    ("UserPage", "trpc/server/router.ts:5", "rpc_calls", "trpc"),
    ("greet", "rpc/server.py:5", "rpc_calls", "grpc"),
    ("bookQuery", "gql/resolvers.js:3", "rpc_calls", "graphql"),
])
def test_events_and_rpc(zoo, src, dst, relation, protocol):
    g = index.load(zoo)
    hits = [d for k, d in _cross(g).items() if k[0] == src]
    assert len(hits) == 1, f"edges from {src}: {sorted(_cross(g))}"
    d = hits[0]
    assert d["relation"] == relation and d["protocol"] == protocol
    if protocol != "event":
        assert d["route_at"] == dst
    else:
        assert g.label(next(v for u, v, dd in g.edges({relation}) if dd is d)) == "sendReceipt()"


def test_a_generic_event_name_is_not_linked(zoo):
    g = index.load(zoo)
    assert all(d.get("event") != "error" for d in _cross(g).values())


@pytest.mark.parametrize("route, client, fits", [
    ("/api/users/:id", ["api", "users", "7"], True),
    ("/api/users/:id", ["api", "users", None], True),
    ("/api/users/{id}", ["api", "users"], False),
    ("/api/users/<int:id>", ["api", "users", "me"], False),
    ("/api/users/me", ["api", "users", None], False),   # a computed segment is not assumed to be a literal
    ("/files/{path:path}", ["files", "a", "b"], True),
    ("/api/users/:id?", ["api", "users"], True),
    ("/api/users/", ["api", "users"], True),
])
def test_match_segments(route, client, fits):
    assert (cross_service.match_segments(cross_service.route_segments(route), client) is not None) is fits


@pytest.mark.parametrize("expr, path", [
    ("`/api/users/${id}?full=1`", ["api", "users", None]),
    ("'/api/users/' + id", ["api", "users", None]),
    ("`${API}/api/x`", ["api", "x"]),
    ("'https://h:8080/v1/a'", ["v1", "a"]),
])
def test_client_path(expr, path):
    segs, _notes = cross_service.client_path(cross_service.js_pieces(expr, {}))
    assert segs == path


@pytest.mark.parametrize("url, linked", [
    ("'https://api.github.com/users'", False),          # a public host is another service's
    ("'http://backend:8000/users'", True),
    ("'http://localhost/users'", True),
    ("'http://10.0.0.5:80/users'", True),
    ("'http://users.default.svc.cluster.local/users'", True),
])
def test_a_public_host_is_not_this_repository(url, linked):
    segs, notes = cross_service.client_path(cross_service.js_pieces(url, {}))
    assert (segs == ["users"]) is linked
    if not linked:
        assert notes == ["another host: api.github.com"]


def test_an_angular_client_and_a_property_chain():
    src = ("export class UserService {\n"
           "  load(id) { return this.http.get<User>(`${this.base}/users/${id}`); }\n"
           "  other() { return store.cache.get('/users/1'); }\n"
           "}\n")
    (c,) = cross_service.file_facts(src, "web/user.service.ts")["clients"]
    assert c["recv"] == "this.http" and c["method"] == "GET" and c["line"] == 2
    segs, notes = cross_service.client_path(c["pieces"])
    assert segs == ["users", None] and notes == ["`this.base` taken for the server's origin"]


def test_route_decorators_are_not_client_calls():
    src = ('from fastapi import FastAPI\n\napi = FastAPI()\n\n\n@api.get("/items/{item_id}")\n'
           'def read_item(item_id: int):\n    return {}\n')
    fx = cross_service.file_facts(src, "svc/main.py")
    assert [r["path"] for r in fx["routes"]] == ["/items/{item_id}"] and "clients" not in fx
    assert fx["routes"][0]["handler"] == {"kind": "def", "line": 7} and fx["routes"][0]["line"] == 6


def test_minified_files_are_not_read():
    text = "fetch('/api/users');" * 2000
    assert cross_service.file_facts(text, "dist/app.js") == {}
    assert cross_service.file_facts("fetch('/api/users');\n", "dist/app.min.js") == {}
    assert cross_service.file_facts("fetch('/api/users');\n", "src/app.js")["clients"][0]["url"] == "/api/users"


def test_relative_and_computed_urls_are_not_paths():
    assert cross_service.client_path(cross_service.js_pieces("url", {}))[0] is None
    assert cross_service.client_path(cross_service.js_pieces("'users/1'", {}))[0] is None


def test_strip_js_keeps_strings_templates_and_regexes():
    src = "a = 'x // y'; // gone\nb = `http://h/${c /* in */}`; /* z\n */ d = /\\/\\//.test(e);"
    out = cross_service.strip_js(src)
    assert "'x // y'" in out and "gone" not in out and "`http://h/${c /* in */}`" in out
    assert "/\\/\\//" in out and out.count("\n") == src.count("\n")


# -- review round ---------------------------------------------------------------------------------------

REVIEW = {
    "py/srv.py": """from flask import Flask, Blueprint

app = Flask(__name__)
bp = Blueprint("x", __name__, url_prefix="/x")
yp = Blueprint("y", __name__, url_prefix="/y")


@bp.route("/thing")
def thing():
    return "t"


@yp.route("/other")
def other():
    return "o"


app.register_blueprint(bp, url_prefix="/api")
app.register_blueprint(yp)
""",
    "py/caller.py": """import requests


def call_thing():
    return requests.get("http://localhost:5000/api/thing")


def call_wrong():
    return requests.get("http://localhost:5000/api/x/thing")


def call_other():
    return requests.get("http://localhost:5000/y/other")
""",
    "py/client.py": """import httpx

client = httpx.Client(base_url="http://localhost:8000")


def get_root_things():
    return client.get("things")
""",
    "server/index.js": """const express = require('express');
const app = express();
function home(req, res) { res.send('h'); }
function users(req, res) { res.send('u'); }
function posts(req, res) { res.send('p'); }
function userPosts(req, res) { res.send('up'); }
app.get('/', home);
app.get('/api/users', users);
app.get('/api/posts', posts);
app.get('/api/:id/posts', userPosts);
module.exports = app;
""",
    "web/api.js": """import axios from 'axios';
const api = axios.create({ baseURL: 'http://localhost:3000' });
const rel = axios.create({ baseURL: '/api/' });
const USERS = '/api/users';
let MOVING = '/api/posts';
MOVING = '/elsewhere';

export function loadA() {
  const url = '/api/users';
  return fetch(url);
}
export function loadB() {
  const url = '/api/posts';
  return fetch(url);
}
export function topLevel() {
  return fetch(USERS);
}
export function moved() {
  return fetch(MOVING);
}
export function listThings() {
  return api.get('things');
}
export function relUsers() {
  return rel.get('users');
}
export function absolute() {
  return rel.get('http://localhost:3000/api/posts');
}
export function search(qs) {
  return fetch('/api/users' + qs);
}
export function mixed(id) {
  return fetch(`/api/u${id}/posts`);
}
""",
    "srv/Controllers.kt": """package x

@RestController
@RequestMapping("/a")
class AController {
    @GetMapping("/list")
    fun listA(): String = "a"
}

@RestController
class BController {
    @GetMapping("/items")
    fun listB(): String = "b"
}
""",
    "srv/Outer.java": """package x;

@RestController
@RequestMapping("/outer")
public class Outer {
    @GetMapping("/o")
    public String o() { return ""; }

    @RestController
    @RequestMapping("/inner")
    public static class Inner {
        @GetMapping("/i")
        public String i() { return ""; }
    }

    @GetMapping("/after")
    public String after() { return ""; }
}
""",
    "web/c.ts": """export function loadItems() { return fetch('/items'); }
export function loadAfter() { return fetch('/outer/after'); }
export function loadInner() { return fetch('/inner/i'); }
""",
}


@pytest.fixture(scope="module")
def review(tmp_path_factory) -> Path:
    return _make(tmp_path_factory, "xs_review", REVIEW)


def test_review_round_edges(review):
    edges = set(_cross(index.load(review)))
    assert edges == {
        ("call_thing", "thing"),       # register_blueprint(url_prefix="/api") replaces the blueprint's "/x"
        ("call_other", "other"),       # no url_prefix given: the blueprint's own "/y"
        ("topLevel", "users"),         # a module-level constant
        ("relUsers", "users"),         # baseURL '/api/' + 'users': one slash
        ("absolute", "posts"),         # an absolute URL keeps no base
        ("search", "users"),           # '/api/users' + qs: the value is taken for a query string
        ("loadItems", "listB"),        # BController has no class prefix
        ("loadAfter", "after"),        # after the nested class, Outer's prefix again
        ("loadInner", "i"),            # the nested class's own prefix
    }, sorted(edges)


def test_review_round_report(review, capsys):
    rep = _report(review, capsys)
    unmatched = {u["at"]: u for u in rep["unmatched"]}
    assert "py/caller.py:9" in unmatched                      # /api/x/thing is not served
    assert unmatched["py/client.py:7"]["url"] == "http://localhost:8000/things"
    assert unmatched["web/api.js:23"]["url"] == "http://localhost:3000/things"   # not '/' on 'localhost:3000things'
    assert "web/api.js:35" in unmatched                       # `/api/u${id}/posts` does not fit /api/:id/posts
    paths = {r["path"] for r in rep["route_table"]}
    assert {"/api/thing", "/y/other", "/a/list", "/items", "/outer/o", "/inner/i", "/outer/after"} <= paths
    assert "/api/x/thing" not in paths and "/a/items" not in paths and "/inner/after" not in paths
    assert rep["unresolved_urls"] == 3                        # two function-local `url`s, a reassigned constant
    search = next(e for e in rep["edges"] if e["from"] == "search()")
    assert search["notes"] == ["the value after `users` taken for a query string or fragment"]


@pytest.mark.parametrize("base, url, joined", [
    ("http://localhost:3000", "things", "http://localhost:3000/things"),
    ("http://localhost:3000/", "/things", "http://localhost:3000/things"),
    ("/api", "users", "/api/users"),
    ("/api/", "/users", "/api/users"),
    ("/api", "https://h/x", "https://h/x"),
    ("/api", "//cdn/x", "//cdn/x"),
    ("/api", "", "/api"),
])
def test_join_base(base, url, joined):
    pieces = cross_service.join_base([["lit", base]], [["lit", url]])
    assert "".join(t for _k, t in pieces) == joined


@pytest.mark.parametrize("expr, segs", [
    ("'/api/users' + qs", ["api", "users"]),
    ("`/api/u${id}/posts`", ["api", cross_service.MIXED, "posts"]),
    ("`/api/${a}${b}`", ["api", None]),
    ("`/files/${id}.json`", ["files", cross_service.MIXED]),
])
def test_mixed_segments(expr, segs):
    assert cross_service.client_path(cross_service.js_pieces(expr, {}))[0] == segs
    route = cross_service.route_segments("/api/:id/posts")
    assert cross_service.match_segments(route, ["api", cross_service.MIXED, "posts"]) is None


def test_constants_are_module_level_and_assigned_once():
    code = cross_service.strip_js("const A = '/a';\nfunction f() { const B = '/b'; }\nfunction g() { const B = '/c'; }\n"
                                  "let C = '/c'; C = '/d';\nvar D = '/d';\nfunction h() { var D = '/e'; }\n")
    assert cross_service._js_consts(code, cross_service._Tokens(code)) == {"A": "/a"}
    fx = cross_service.file_facts('import requests\nU = "/u"\nV = "/v"\nV = "/w"\n\n\ndef f():\n'
                                  '    return requests.get(U), requests.get(V)\n', "c.py")
    assert [c["url"] for c in fx["clients"]] == ["/u", "{V}"]


def test_jvm_prefixes_are_per_class():
    kt = REVIEW["srv/Controllers.kt"]
    java = REVIEW["srv/Outer.java"]
    assert {r["path"] for r in cross_service.file_facts(kt, "C.kt")["routes"]} == {"/a/list", "/items"}
    assert {r["path"] for r in cross_service.file_facts(java, "Outer.java")["routes"]} == {
        "/outer/o", "/inner/i", "/outer/after"}


ODD_JS = {
    "regex quotes": lambda: "".join(f"x = api.get('/a/{i}', s.replace(/'/g, ''));\n" for i in range(3000)),
    "unterminated templates": lambda: "api.get(`/x\n" * 6000,
    "unclosed objects": lambda: "api.get('/x', {\n" * 6000,
    "unclosed start": lambda: "api.get('/x', (\n" + "".join(f"function f{i}() {{ return api.get('/b/{i}'); }}\n"
                                                           for i in range(3000)),
}


@pytest.mark.parametrize("name", sorted(ODD_JS))
def test_odd_javascript_is_linear(name):
    # each took minutes before the bracket table (a scan from every call to the end of the file)
    text = ODD_JS[name]()
    t = time.perf_counter()
    cross_service.file_facts(text, "odd.js")
    assert time.perf_counter() - t < 1.0, name


def test_tokens_match_brackets_past_strings_and_regexes():
    code = "f(a, '(', /[)]/g, `)${x(1)}`, {b: [1, 2]})\ng(\n"
    tok = cross_service._Tokens(code)
    assert tok.close(1) == code.index("\n") - 1 and tok.close(code.index("g(") + 1) == -1
    args, _close = tok.args(1)
    assert args == ["a", "'('", "/[)]/g", "`)${x(1)}`", "{b: [1, 2]}"]


def test_a_damaged_sidecar_block_is_skipped(web):
    g = index.load(web, augment=False)
    good = next(iter(json.loads(index.receiver_calls_path(web).read_text(encoding="utf-8"))
                     ["cross_service"]["edges"]))
    index._apply_cross_service(g, {"edges": [["a"], 5, ["x", "y", {}], None, good],
                                   "ambiguous": ["bad", {"caller": ["x"], "candidates": "no"}]})
    assert retrieval.trace(g, "loadItem", "get_item")["status"] == "no directed path"
    assert retrieval.trace(g, good[0], good[1])["status"] == "found"
    index._apply_cross_service(g, ["not", "a", "block"])
