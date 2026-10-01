"""Route paths as the server serves them, and a `routes` report of bounded size.

A FastAPI ``include_router(router, prefix=settings.API_V1_STR)`` whose value is a pydantic settings default in
another module is resolved, and the row says where the value came from; a prefix nothing spells keeps its row
with ``mount: "prefix not resolved: <expr>"`` instead of a path that looks complete. A trailing ``/`` the code
writes stays in the displayed path. Test and example code is labelled; a test client that calls the app it
imports is matched against that app's routes only; ambiguous calls are grouped and capped unless ``--all``."""

from __future__ import annotations

import json
import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli, cross_service, index, search_index, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

PROJECT = {
    # the full-stack-fastapi-template shape: a settings default, a router chain, prefix "/items" + "/"
    "backend/app/core/config.py": """from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    API_V1_STR: str = "/api/v1"
    PROJECT_NAME: str


settings = Settings()
""",
    "backend/app/api/routes/items.py": """from fastapi import APIRouter

router = APIRouter(prefix="/items", tags=["items"])


@router.get("/")
def read_items():
    return []


@router.get("/{id}")
def read_item(id: int):
    return {}
""",
    "backend/app/api/routes/login.py": """from fastapi import APIRouter

router = APIRouter(tags=["login"])


@router.post("/login/access-token")
def login_access_token():
    return {}
""",
    "backend/app/api/main.py": """from fastapi import APIRouter

from app.api.routes import items, login

api_router = APIRouter()
api_router.include_router(login.router)
api_router.include_router(items.router)
""",
    "backend/app/main.py": """from fastapi import FastAPI

from app.api.main import api_router
from app.core.config import settings

app = FastAPI()
app.include_router(api_router, prefix=settings.API_V1_STR)
""",
    "backend/app/client.py": """import httpx


def fetch_item(item_id):
    return httpx.get(f"http://backend:8000/api/v1/items/{item_id}")


def old_path():
    return httpx.get("http://backend:8000/items/1")
""",
    # a prefix nothing spells: a call, and a router whose own prefix is read from the environment
    "svc/routers/orders.py": """import os

from fastapi import APIRouter

router = APIRouter(prefix=os.environ["ORDERS_PREFIX"])


@router.get("/{order_id}")
def read_order(order_id: int):
    return {}
""",
    "svc/main.py": """from fastapi import FastAPI

from .routers import orders
from .prefixes import V2, version_prefix

app = FastAPI()
app.include_router(orders.router, prefix=version_prefix())
app.include_router(orders.router, prefix=f"{V2}/x")
""",
    "svc/prefixes.py": """V2 = "/v2"


def version_prefix():
    return "/v1"
""",
    # Express: two example apps both serve GET /, a test calls the one it imports
    "examples/alpha/index.js": """const express = require('express');
const app = module.exports = express();

app.get('/', function home(req, res) { res.send('a'); });
""",
    "examples/beta/index.js": """const express = require('express');
const app = module.exports = express();

app.get('/', function home(req, res) { res.send('b'); });
""",
    "test/alpha.js": """const request = require('supertest');
const app = require('../examples/alpha');

describe('alpha', function () {
  it('serves /', function (done) {
    request(app).get('/').expect(200, done);
  });
  it('has no /nope', function (done) {
    request(app).get('/nope').expect(404, done);
  });
});
""",
    "test/many.js": """const express = require('express');
const request = require('supertest');

function one(req, res) { res.end(); }
function two(req, res) { res.end(); }

describe('two apps in one file', function () {
  it('one', function (done) {
    const app = express();
    app.post('/', one);
    request(app).post('/').expect(200, done);
  });
  it('two', function (done) {
    const app = express();
    app.post('/', two);
    request(app).post('/').expect(200, done);
    request(app).post('/').expect(200, done);
  });
});
""",
}


@pytest.fixture(scope="module")
def proj(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("routes_prefix") / "proj"
    for rel, text in PROJECT.items():
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


def _routes(repo: Path, capsys, *extra: str) -> dict:
    capsys.readouterr()
    assert cli.main(["routes", "--repo", str(repo), "--json", *extra]) == 0
    return json.loads(capsys.readouterr().out)


def _row(rep: dict, at: str) -> list[dict]:
    return [r for r in rep["route_table"] if r["at"] == at]


def test_a_settings_default_prefix_is_resolved_and_cited(proj, capsys):
    rep = _routes(proj, capsys)
    (login,) = _row(rep, "backend/app/api/routes/login.py:6")
    assert login["path"] == "/api/v1/login/access-token" and "mount" not in login
    (note,) = login["prefix_from"]
    assert "settings.API_V1_STR" in note and "backend/app/core/config.py:5" in note and "environment" in note
    (item,) = _row(rep, "backend/app/api/routes/items.py:11")
    assert item["path"] == "/api/v1/items/{id}"


def test_the_trailing_slash_is_kept_as_written(proj, capsys):
    rep = _routes(proj, capsys)
    (items,) = _row(rep, "backend/app/api/routes/items.py:6")
    assert items["path"] == "/api/v1/items/"          # FastAPI serves prefix "/items" + "/" at /items/
    # matching ignores the slash: a call to /api/v1/items (no slash) still fits this route
    segs, _notes = cross_service.client_path([["lit", "/api/v1/items"]])
    assert cross_service.match_segments(cross_service.route_segments(items["path"]), segs) is not None


def test_a_client_links_through_the_resolved_prefix(proj, capsys):
    rep = _routes(proj, capsys)
    edges = {(e["from"], e["to"]) for e in rep["edges"]}
    assert ("fetch_item()", "read_item()") in edges
    assert any(u["at"] == "backend/app/client.py:9" for u in rep["unmatched"])   # /items/1 lacks /api/v1


def test_a_prefix_nothing_spells_keeps_the_row_and_says_so(proj, capsys):
    rep = _routes(proj, capsys)
    rows = _row(rep, "svc/routers/orders.py:8")
    by_mount = {r["mount"]: r["path"] for r in rows}
    # include_router(prefix=version_prefix()): a call; the router's own prefix: an environment variable
    assert by_mount["prefix not resolved: version_prefix(); prefix not resolved: os.environ['ORDERS_PREFIX']"] \
        == "/{order_id}"
    # f"{V2}/x": V2 is a constant of the module it is imported from; the router's own prefix is still unknown
    assert by_mount["prefix not resolved: os.environ['ORDERS_PREFIX']"] == "/v2/x/{order_id}"
    text = cross_service.render(rep)
    assert "(mount prefix not resolved: os.environ['ORDERS_PREFIX'])" in text


def test_test_and_example_code_is_labelled_and_a_test_calls_the_app_it_imports(proj, capsys):
    rep = _routes(proj, capsys)
    (alpha,) = _row(rep, "examples/alpha/index.js:4")
    assert alpha["code"] == "example"
    assert all("code" not in r for r in rep["route_table"] if r["at"].startswith("backend/"))
    assert all(r["code"] == "test" for r in rep["route_table"] if r["at"].startswith("test/"))
    edges = {(e["at"], e["route_at"]) for e in rep["edges"]}
    assert ("test/alpha.js:6", "examples/alpha/index.js:4") in edges   # not ambiguous with examples/beta
    nope = next(u for u in rep["unmatched"] if u["at"] == "test/alpha.js:9")
    assert nope["code"] == "test" and "app under test" in nope["why"]


def test_ambiguous_calls_are_grouped_and_all_lists_them(proj, capsys):
    rep = _routes(proj, capsys)
    (grp,) = [a for a in rep["ambiguous"] if a["at"].startswith("test/many.js")]
    assert grp["calls"] == 3 and len(grp["also_at"]) == 2 and grp["code"] == "test"
    assert {c["at"] for c in grp["candidates"]} == {"test/many.js:10", "test/many.js:15"}
    assert rep["ambiguous_calls"] == 3 and rep["ambiguous_by_code"] == {"test": 3} and "bounded" in rep
    full = _routes(proj, capsys, "--all")
    assert len([a for a in full["ambiguous"] if a["at"].startswith("test/many.js")]) == 3
    assert "bounded" not in full and full["ambiguous_calls"] == 3


def test_the_sidecar_keeps_full_counts_and_eight_candidates(proj):
    side = json.loads(index.receiver_calls_path(proj).read_text(encoding="utf-8"))
    block = side["cross_service"]
    assert block["facts_version"] == cross_service.FACTS_VERSION
    assert block["counts"]["ambiguous"] == 3 and all(len(a["candidates"]) <= 8 for a in block["ambiguous"])


def _amb(i: int, n: int) -> dict:
    return {"at": f"t/x.js:{i}", "method": "GET", "url": f"/u{i % 60}", "protocol": "http", "code": "test",
            "candidates": [{"route": "GET /", "at": f"e/{k}.js:1", "handler": f"h{k}"} for k in range(n)]}


def test_bounded_caps_groups_candidates_and_lists_and_counts_every_cut():
    rep = {"routes": 1, "clients": 200, "linked": 0, "unresolved_urls": 0, "route_table": [],
           "ambiguous": [_amb(i, 9) for i in range(130)], "method_mismatch": [],
           "unmatched": [{"at": f"t/y.js:{i}", "method": "GET", "url": "/z"} for i in range(70)]}
    out = cross_service.bounded(rep)
    assert out["ambiguous_groups"] == 60 and len(out["ambiguous"]) == cross_service.AMBIGUOUS_GROUPS
    assert out["ambiguous_more"] == 10 and out["ambiguous_calls"] == 130
    first = out["ambiguous"][0]
    assert first["calls"] == 3 and first["also_at"] == ["t/x.js:60", "t/x.js:120"]
    assert len(first["candidates"]) == cross_service.AMBIGUOUS_CANDIDATES and first["candidates_total"] == 9
    assert len(out["unmatched"]) == cross_service.LIST_CAP and out["unmatched_more"] == 20
    assert out["unmatched_by_code"] == {"other": 70}
    text = cross_service.render(out)
    assert "130 ambiguous, 70 unmatched" in text and "... 4 more candidate(s)" in text and "10 more group(s)" in text
    every = cross_service.bounded(rep, everything=True)
    assert len(every["ambiguous"]) == 130 and len(every["unmatched"]) == 70
    assert len(every["ambiguous"][0]["candidates"]) == 9


@pytest.mark.parametrize("parts, keep, joined", [
    (("/items", "/"), True, "/items/"),
    (("/items", "/"), False, "/items"),
    (("/items", ""), True, "/items"),
    (("", "/"), True, "/"),
    (("/api/v1", "/reset-password/"), True, "/api/v1/reset-password/"),
    (("/shop/", "/products/<int:pk>/"), True, "/shop/products/<int:pk>/"),
    (("/a/", "/b"), True, "/a/b"),
])
def test_join_path_keeps_a_written_trailing_slash(parts, keep, joined):
    assert cross_service.join_path(*parts, keep_slash=keep) == joined


@pytest.mark.parametrize("src, key, value, kind", [
    ('X = "/v1"\n', "X", "/v1", "const"),
    ('X: str = "/v1"\n', "X", "/v1", "const"),
    ('X = "/v1"\nX = "/v2"\n', "X", None, None),                                   # bound twice: no one value
    ('class S(BaseSettings):\n    P: str = "/a"\ns = S()\n', "s.P", "/a", "settings default"),
    ('class S:\n    P = "/a"\ns = S(P="/b")\n', "s.P", "/b", "const"),               # the instance passes its own
    ('class S(BaseSettings):\n    P: str = "/a"\ns = S(**env)\n', "s.P", None, None),   # any value
    ('class C:\n    P = "/a"\n', "C.P", "/a", "default"),
])
def test_py_values(src, key, value, kind):
    import ast

    got = cross_service.py_values(ast.parse(src)).get(key)
    assert (got[0], got[2]) == (value, kind) if value else got is None


def test_flask_url_prefix_from_an_imported_constant():
    fx = cross_service.file_facts('from flask import Flask\nfrom .conf import API\napp = Flask(__name__)\n'
                                  'from .views import bp\napp.register_blueprint(bp, url_prefix=API)\n', "w/app.py")
    (mt,) = fx["mounts"]
    assert mt["prefix_pieces"] == [["ref", "API", ".conf.API"]] and mt["prefix_expr"] == "API" and mt["replaces"]
