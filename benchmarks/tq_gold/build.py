"""Builds the typed-question gold set: dev.json, held_out.json and MANIFEST.json (sha256 of each file).

Every case below was checked by hand against the source it names, before any typed-question code existed:
``answer`` is what the code does (True / False, a count, the files that define a name), not what a static
graph can see. ``answer`` None means the question has no true or false answer (a route that does not exist):
the only right reply is unknown. ``at`` lists the lines a true answer must cite (``file:line``); for a SQL
statement that is the line of the call that runs it, and a cited line up to two lines below (the statement's
text) counts as a hit. ``writes`` means a row write (INSERT, UPDATE, DELETE, REPLACE, MERGE), ``reads`` a
SELECT; creating a table is neither. ``callers`` is the exact number of distinct definitions that call the
function (itself included when it is recursive).

Repositories: ``qlang`` is ``fixtures/qlang`` (the query-language fixture of tests/test_query_language.py,
plus admin.py with a real SQL injection and dyn.py with calls a static graph cannot see); ``orders_app`` is
examples/orders_app. Neither has a traced test run, so every ``tested`` case has a true answer that only a
trace could show.

A case goes to held_out when the first byte of sha256(id) is below 0x56 (about one in three), so the split
depends on the id alone. Run ``python benchmarks/tq_gold/build.py`` to rewrite the three files; the frozen
hashes are in MANIFEST.json and a test checks them.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
WRITTEN = "2026-10-01"

Q, O = "qlang", "orders_app"

# (id, repo, question, answer, at)
CASES = [
    # -- exists ------------------------------------------------------------------------------------------
    ("q-exists-01", Q, "exists save_order", True, ["app/store.py:6"]),
    ("q-exists-02", Q, "exists unused", True, ["app/store.py:20"]),
    ("q-exists-03", Q, "exists Repo.save", True, ["app/store.py:25"]),
    ("q-exists-04", Q, "exists delete_order", False, []),
    ("q-exists-05", Q, "exists Repo.delete", False, []),
    ("q-exists-06", Q, "exists tri3", True, ["rec.py:56"]),
    ("q-exists-07", Q, "exists WithMethods.run", True, ["rec.py:40"]),
    ("q-exists-08", Q, "exists find_user", True, ["app/admin.py:11"]),
    ("o-exists-01", O, "exists place_order", True, ["orders/service.py:19"]),
    ("o-exists-02", O, "exists OrderRepository.save", True, ["orders/repository.py:15"]),
    ("o-exists-03", O, "exists OrderRepository.delete", False, []),
    ("o-exists-04", O, "exists cancel_order", False, []),
    ("o-exists-05", O, "exists load_settings", True, ["orders/config.py:9"]),
    ("o-exists-06", O, "exists apply_discount", True, ["orders/pricing.py:11"]),
    # -- which -------------------------------------------------------------------------------------------
    ("q-which-01", Q, "which save_order in app/store.py|app/service.py", ["app/store.py"], ["app/store.py:6"]),
    ("q-which-02", Q, "which create_order in app/web.py|app/service.py", ["app/service.py"], ["app/service.py:4"]),
    ("q-which-03", Q, "which leaf in app/store.py|app/web.py", [], []),
    ("o-which-01", O, "which compute_total in orders/pricing.py|orders/service.py", ["orders/pricing.py"],
     ["orders/pricing.py:6"]),
    ("o-which-02", O, "which place_order in orders/api.py|orders/repository.py", [], []),
    # -- calls -------------------------------------------------------------------------------------------
    ("q-calls-01", Q, "calls post_order create_order", True, ["app/web.py:11"]),
    ("q-calls-02", Q, "calls create_order save_order", True, ["app/service.py:6"]),
    ("q-calls-03", Q, "calls create_order check", True, ["app/service.py:5"]),
    ("q-calls-04", Q, "calls post_order save_order", False, []),
    ("q-calls-05", Q, "calls post_order save_order depth=2", True, ["app/web.py:11", "app/service.py:6"]),
    ("q-calls-06", Q, "calls fact fact", True, ["rec.py:2"]),
    ("q-calls-07", Q, "calls ping pong", True, ["rec.py:6"]),
    ("q-calls-08", Q, "calls pong ping", True, ["rec.py:10"]),
    ("q-calls-09", Q, "calls ping leaf", False, []),
    ("q-calls-10", Q, "calls chain1 leaf", False, []),
    ("q-calls-11", Q, "calls chain1 leaf depth=3", True, ["rec.py:20", "rec.py:24", "rec.py:28"]),
    ("q-calls-12", Q, "calls persist Repo.save", True, ["app/service.py:22"]),
    ("q-calls-13", Q, "calls leaf chain3", False, []),
    ("q-calls-14", Q, "calls caller fact", True, ["rec.py:36"]),
    ("q-calls-15", Q, "calls WithMethods.run leaf", True, ["rec.py:41"]),
    ("q-calls-16", Q, "calls get_orders save_order", False, []),
    ("q-calls-17", Q, "calls direct target", True, ["dyn.py:22"]),
    ("q-calls-18", Q, "calls through_variable target", True, ["dyn.py:18"]),
    ("q-calls-19", Q, "calls find_user count_users", False, []),
    ("o-calls-01", O, "calls create_order_handler place_order", True, ["orders/api.py:18"]),
    ("o-calls-02", O, "calls place_order validate_items", True, ["orders/service.py:20"]),
    ("o-calls-03", O, "calls place_order compute_total", True, ["orders/service.py:21"]),
    ("o-calls-04", O, "calls compute_total apply_discount", True, ["orders/pricing.py:8"]),
    ("o-calls-05", O, "calls place_order OrderRepository.save", True, ["orders/service.py:22"]),
    ("o-calls-06", O, "calls fetch_order OrderRepository.get", True, ["orders/service.py:26"]),
    ("o-calls-07", O, "calls get_order_handler place_order", False, []),
    ("o-calls-08", O, "calls apply_discount compute_total", False, []),
    ("o-calls-09", O, "calls create_order_handler apply_discount", False, []),
    ("o-calls-10", O, "calls validate_items compute_total", False, []),
    ("o-calls-11", O, "calls get_order_handler fetch_order", True, ["orders/api.py:25"]),
    # -- reaches -----------------------------------------------------------------------------------------
    ("q-reaches-01", Q, "reaches chain1 leaf", True, ["rec.py:20", "rec.py:24", "rec.py:28"]),
    ("q-reaches-02", Q, "reaches leaf chain1", False, []),
    ("q-reaches-03", Q, "reaches tri1 tri3", True, ["rec.py:49", "rec.py:53"]),
    ("q-reaches-04", Q, "reaches post_order save_order", True, ["app/web.py:11", "app/service.py:6"]),
    ("q-reaches-05", Q, "reaches get_orders save_order", False, []),
    ("q-reaches-06", Q, "reaches not_a_handler write_log", True, ["app/web.py:25", "app/service.py:18"]),
    ("q-reaches-07", Q, "reaches put_item Repo.save", True, ["app/web.py:21", "app/service.py:22"]),
    ("q-reaches-08", Q, "reaches caller leaf", False, []),
    ("q-reaches-09", Q, "reaches outer leaf", False, []),
    ("q-reaches-10", Q, "reaches run_by_name target", True, ["dyn.py:13", "dyn.py:9"]),
    ("o-reaches-01", O, "reaches create_order_handler apply_discount", True,
     ["orders/api.py:18", "orders/service.py:21", "orders/pricing.py:8"]),
    ("o-reaches-02", O, "reaches get_order_handler apply_discount", False, []),
    ("o-reaches-03", O, "reaches create_order_handler OrderRepository.save", True,
     ["orders/api.py:18", "orders/service.py:22"]),
    ("o-reaches-04", O, "reaches get_order_handler OrderRepository.get", True,
     ["orders/api.py:25", "orders/service.py:26"]),
    ("o-reaches-05", O, "reaches apply_discount place_order", False, []),
    ("o-reaches-06", O, "reaches load_settings compute_total", False, []),
    # -- route -------------------------------------------------------------------------------------------
    ("q-route-01", Q, 'route "POST /orders" save_order', True, ["app/web.py:9"]),
    ("q-route-02", Q, 'route "GET /orders" read_orders', True, ["app/web.py:14"]),
    ("q-route-03", Q, 'route "GET /orders" save_order', False, []),
    ("q-route-04", Q, 'route "PUT /items" Repo.save', True, ["app/web.py:19"]),
    ("q-route-05", Q, 'route "DELETE /orders" save_order', None, []),
    ("q-route-06", Q, 'route "GET /admin/find" find_user', True, ["app/admin.py:10"]),
    ("o-route-01", O, 'route "POST /orders" place_order', None, []),
    # -- writes / reads ----------------------------------------------------------------------------------
    ("q-writes-01", Q, "writes post_order orders", True, ["app/store.py:7"]),
    ("q-writes-02", Q, "writes get_orders orders", False, []),
    ("q-writes-03", Q, "writes put_item items", True, ["app/store.py:26"]),
    ("q-writes-04", Q, "writes not_a_handler log", True, ["app/store.py:17"]),
    ("q-writes-05", Q, "writes create_order log", False, []),
    ("q-writes-06", Q, "writes find_user users", False, []),
    ("q-reads-01", Q, "reads get_orders orders", True, ["app/store.py:13"]),
    ("q-reads-02", Q, "reads post_order orders", False, []),
    ("q-reads-03", Q, "reads list_orders orders", True, ["app/store.py:13"]),
    ("q-reads-04", Q, "reads find_user users", True, ["app/admin.py:13"]),
    ("q-reads-05", Q, "reads count_users users", True, ["app/admin.py:18"]),
    ("o-writes-01", O, "writes create_order_handler orders", True, ["orders/repository.py:16"]),
    ("o-writes-02", O, "writes get_order_handler orders", False, []),
    ("o-reads-01", O, "reads get_order_handler orders", True, ["orders/repository.py:23"]),
    ("o-reads-02", O, "reads create_order_handler orders", False, []),
    # -- callers (the exact number of distinct calling definitions) --------------------------------------
    ("q-callers-01", Q, "callers leaf", 2, []),
    ("q-callers-02", Q, "callers fact", 2, []),
    ("q-callers-03", Q, "callers check", 2, []),
    ("q-callers-04", Q, "callers unused", 0, []),
    ("q-callers-05", Q, "callers save_order", 1, []),
    ("q-callers-06", Q, "callers find_user", 0, []),
    ("o-callers-01", O, "callers place_order", 3, []),
    ("o-callers-02", O, "callers apply_discount", 3, []),
    ("o-callers-03", O, "callers load_settings", 0, []),
    ("o-callers-04", O, "callers compute_total", 2, []),
    # -- taint -------------------------------------------------------------------------------------------
    ("q-taint-01", Q, "taint request.args sql", True, ["app/admin.py:13"]),
    ("q-taint-02", Q, "taint request.json sql", False, []),
    ("q-taint-03", Q, "taint request.args shell", False, []),
    ("o-taint-01", O, "taint request sql", False, []),
    # -- tested (no traced run in either repository) -----------------------------------------------------
    ("q-tested-01", Q, "tested tests/test_service.py::test_check check", True, []),
    ("q-tested-02", Q, "tested tests/test_service.py::test_check save_order", False, []),
    ("o-tested-01", O, "tested tests/test_pricing.py::test_compute_total apply_discount", True, []),
    ("o-tested-02", O, "tested tests/test_pricing.py::test_compute_total place_order", False, []),
    # -- q -----------------------------------------------------------------------------------------------
    ("q-q-01", Q, "q 'match (f:function) where f.name = \"unused\" return f' as=bool", True, ["app/store.py:20"]),
    ("q-q-02", Q, "q 'match (c)-[calls]->(x) where x.name = \"leaf\" return count(c)' as=count", 2, []),
    ("q-q-03", Q, "q 'match (f:function) where f.name = \"nothing_here\" return f' as=bool", False, []),
    ("o-q-01", O, "q 'match (f:function) where f.file = \"orders/pricing.py\" return count(f)' as=count", 2, []),
]


def held_out(case_id: str) -> bool:
    return hashlib.sha256(case_id.encode("utf-8")).digest()[0] < 0x56


def _dump(rows: list[dict]) -> str:
    return json.dumps(rows, indent=1, ensure_ascii=False) + "\n"


def build() -> dict:
    ids = [c[0] for c in CASES]
    assert len(ids) == len(set(ids)), "duplicate case id"
    rows = [{"id": i, "repo": r, "question": q, "answer": a, "at": at} for i, r, q, a, at in CASES]
    split = {"dev.json": [x for x in rows if not held_out(x["id"])],
             "held_out.json": [x for x in rows if held_out(x["id"])]}
    files = {}
    for name, part in split.items():
        text = _dump(part)
        (HERE / name).write_text(text, encoding="utf-8", newline="\n")
        files[name] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    fix = HERE / "fixtures"
    fixtures = {p.relative_to(HERE).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(fix.rglob("*")) if p.is_file()}
    manifest = {"written": WRITTEN,
                "note": "sha256 of the gold files and fixtures as frozen before any typed-question code was written",
                "split": "held_out when sha256(id)[0] < 0x56",
                "files": files, "fixtures": fixtures,
                "counts": {k: len(v) for k, v in split.items()}}
    (HERE / "MANIFEST.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8", newline="\n")
    return manifest


if __name__ == "__main__":
    print(json.dumps(build()["counts"]))
