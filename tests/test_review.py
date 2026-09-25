"""`verinoda review` / MCP change_review (docs/DESIGN.md D35): the blast radius of a change by concern.

Runs on git copies of examples/orders_app and of small generated Java/Kotlin projects (never on examples/
itself). Each test edits its own copy of an indexed base."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import ast  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import review as rv  # noqa: E402
from verinoda import review_rules as rr  # noqa: E402
from verinoda import workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ORDERS = ROOT / "examples" / "orders_app"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def _scan(repo: Path) -> Path:
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
    finally:
        st.close()
    return repo


def _rm_ro(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _clone(base: Path, dst: Path) -> Path:
    shutil.copytree(base, dst)
    return dst


def _edit(repo: Path, rel: str, find: str, replace: str) -> None:
    p = repo / rel
    text = p.read_text(encoding="utf-8")
    assert text.count(find) == 1, (rel, find)
    p.write_text(text.replace(find, replace, 1), encoding="utf-8", newline="\n")


def _review(repo: Path, **kw) -> dict:
    st = open_store(repo)
    try:
        return rv.review(repo, store=st, **kw)
    finally:
        st.close()


def _by(res: dict, concern: str, rule: str | None = None) -> list[dict]:
    return [f for f in res["concerns"][concern] if rule is None or f["rule"] == rule]


@pytest.fixture(scope="module")
def orders_base(tmp_path_factory):
    dst = tmp_path_factory.mktemp("review") / "orders_base"
    shutil.copytree(ORDERS, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
    (dst / ".gitignore").write_text("__pycache__/\n*.pyc\n.verinoda/\n", encoding="utf-8")
    return _scan(dst)


@pytest.fixture()
def orders(orders_base, tmp_path):
    return _clone(orders_base, tmp_path / "orders")


JAVA_FILES = {
    "src/main/java/com/ex/net/Network.java": """package com.ex.net;

import com.ex.core.Forge;

public final class Network {
    public static void register(Registrar registrar) {
        registrar.playToServer(Payload.TYPE, Payload.CODEC, Network::handle);
    }

    static void handle(Payload payload, Context context) {
        if (context.player() == null) {
            return;
        }
        if (context.player().distanceTo(payload.pos()) > 8.0) {
            return;
        }
        Forge.stoke(payload.pos(), 1);
    }
}
""",
    "src/main/java/com/ex/core/Forge.java": """package com.ex.core;

public class Forge {
    public static int stoke(Object pos, int amount) {
        return amount * 2;
    }

    public static void tick(Object level, Object pos, Forge forge) {
        forge.cool();
    }

    void cool() {
    }

    public boolean stillValid(Object player) {
        return player != null;
    }
}
""",
    "src/main/java/com/ex/core/ForgeBlock.java": """package com.ex.core;

public class ForgeBlock {
    Object ticker(Object type) {
        return createTickerHelper(type, Forge::tick);
    }

    void poke() {
        Forge.stoke(null, 3);
    }
}
""",
    "reference/other/src/Other.java": """package com.elsewhere;

public class Other {
    void call() {
        Forge.stoke(null, 1);
    }
}
""",
    "src/main/kotlin/com/ex/heat/HeatMath.kt": """package com.ex.heat

object HeatMath {
    @JvmStatic
    fun steps(heat: Int, min: Int): Int {
        if (heat < min) return 0
        return (heat - min) / 50
    }
}
""",
}


@pytest.fixture(scope="module")
def jvm_base(tmp_path_factory):
    dst = tmp_path_factory.mktemp("review") / "jvm_base"
    for rel, text in JAVA_FILES.items():
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        (dst / rel).write_text(text, encoding="utf-8", newline="\n")
    (dst / ".gitignore").write_text(".verinoda/\n", encoding="utf-8")
    return _scan(dst)


@pytest.fixture()
def jvm(jvm_base, tmp_path):
    return _clone(jvm_base, tmp_path / "jvm")


# -- changed symbols ---------------------------------------------------------------------------------------

def test_changed_symbols_by_kind_and_comments_do_not_count(orders):
    _edit(orders, "orders/pricing.py", "    if subtotal > DISCOUNT_THRESHOLD:\n", "    if subtotal >= DISCOUNT_THRESHOLD:\n")
    _edit(orders, "orders/pricing.py", "def compute_total(items: list[dict]) -> float:",
          "def compute_total(items: list[dict], extra: float = 0.0) -> float:")
    _edit(orders, "orders/service.py", "    if not items:\n", "    # at least one item\n    if not items:  # none\n")
    _edit(orders, "orders/service.py", "def fetch_order(repo: OrderRepository, order_id: int):\n",
          "def fetch_order(repo: OrderRepository, order_id: int):\n    \"\"\"A docstring only.\"\"\"\n")
    _edit(orders, "orders/config.py", '"100.0"', '"150.0"')
    _edit(orders, "orders/repository.py", "    def get(self, order_id: int):\n", "    def gone(self) -> None:\n"
          "        pass\n\n    def get(self, order_id: int):\n")
    res = _review(orders)
    got = {(c["symbol"], c["kind"]) for c in res["changes"]}
    assert got == {("orders/pricing.py::apply_discount", "body"), ("orders/pricing.py::compute_total", "signature"),
                   ("orders/config.py::DISCOUNT_THRESHOLD", "module_statement"),
                   ("orders/repository.py::OrderRepository.gone", "added")}
    assert res["mode"] == "worktree" and res["base"]["ref"] == "HEAD" and len(res["base"]["commit"]) == 40


def test_a_comment_only_edit_is_no_change_and_exit_0(orders):
    _edit(orders, "orders/service.py", "    return repo.get(order_id)\n", "    return repo.get(order_id)  # may be None\n")
    res = _review(orders)
    assert res["changes"] == [] and res["counts"]["findings"] == 0 and res["exit"] == 0
    assert "no changed definition" in res["summary"]
    assert all(v.startswith("no finding from rules") for v in res["concerns_checked"].values())


def test_a_change_inside_a_nested_def_reports_the_inner_one(orders):
    _edit(orders, "orders/pricing.py", "def apply_discount(subtotal: float) -> float:\n",
          "def apply_discount(subtotal: float) -> float:\n    def helper(x):\n        return x\n\n")
    _git(orders, "add", "-A")
    _git(orders, "commit", "-q", "-m", "nested")
    _edit(orders, "orders/pricing.py", "        return x\n", "        return x * 2\n")
    res = _review(orders)
    assert [(c["symbol"], c["kind"]) for c in res["changes"]] == [("orders/pricing.py::apply_discount.helper", "body")]


def test_untracked_non_source_files_are_skipped(orders):
    (orders / "orders" / "junk.bin").write_bytes(b"\x00\x01binary")
    (orders / "notes.xyz").write_text("scratch", encoding="utf-8")
    res = _review(orders)
    skipped = {s["file"]: s["why"] for s in res["skipped"]}
    assert "notes.xyz" in skipped and "orders/junk.bin" in skipped
    assert res["changes"] == []


def test_kotlin_body_change_is_not_a_signature_change(jvm):
    _edit(jvm, "src/main/kotlin/com/ex/heat/HeatMath.kt", "/ 50", "/ 25")
    res = _review(jvm)
    assert [(c["symbol"], c["kind"]) for c in res["changes"]] == [
        ("src/main/kotlin/com/ex/heat/HeatMath.kt::HeatMath.steps", "body")]


# -- persistence ------------------------------------------------------------------------------------------

def test_value_carried_to_a_sink_with_its_chain(orders):
    _edit(orders, "orders/pricing.py", "    if subtotal > DISCOUNT_THRESHOLD:\n        return round(subtotal * 0.9, 2)\n",
          "    if subtotal >= DISCOUNT_THRESHOLD:\n        return int(subtotal * 0.9 * 100) / 100\n")
    res = _review(orders)
    [f] = _by(res, "persistence", "value-carried-to-sink")
    assert f["at"] == "orders/service.py:22" and "orders/repository.py:17" in f["evidence_at"]
    assert f["status"] == "strong_inference"
    assert [h["from"] for h in f["chain"]] == ["place_order", "compute_total"]
    assert [h["carries"] for h in f["chain"]] == ["passes it on", "returns it"]
    # the handler that returns place_order's result (a row id) is not a value path
    assert not any("create_order_handler" in f["finding"] for f in res["concerns"]["persistence"])
    [e] = _by(res, "entry_points")
    assert e["at"] == "orders/api.py:16" and e["status"] == "strong_inference"
    [c] = _by(res, "config")
    assert c["at"] == "orders/pricing.py:13" and c["key"] == "ORDERS_DISCOUNT_THRESHOLD"
    assert c["evidence_at"] == ["orders/config.py:7"]
    assert res["exit"] == 3


def test_sink_on_a_changed_line_and_a_changed_call(orders):
    _edit(orders, "orders/repository.py", "        self.conn.commit()\n        return cur.lastrowid\n",
          "        self.conn.commit()\n        with open(\"orders.log\", \"a\") as log:\n"
          "            log.write(str(total))\n        return cur.lastrowid\n")
    _edit(orders, "orders/service.py", "    return repo.save(customer, total)\n",
          "    return repo.save(customer.strip(), total)\n")
    res = _review(orders)
    ats = {f["at"]: f["rule"] for f in res["concerns"]["persistence"]}
    assert ats.get("orders/repository.py:20") == "sink-on-changed-line"
    assert ats.get("orders/service.py:22") == "changed-call-reaches-sink"


def test_py_carry_follows_assignments_but_not_other_calls():
    tree = ast.parse("def f(a):\n    x = g(a)\n    y = round(x, 2)\n    h(y)\n    return other(y)\n")
    c = rr.py_carry(tree.body[0], "g")
    assert c.tainted == {"x", "y"} and not c.returns
    assert [(ln, name) for ln, _call, name in c.passes] == [(4, "h"), (5, "other")]


# -- security ----------------------------------------------------------------------------------------------

def test_guard_removed_is_statically_verified_and_a_value_branch_is_no_guard(orders):
    _edit(orders, "orders/service.py", "    if len(items) > MAX_ITEMS_PER_ORDER:\n"
          "        raise ValidationError(\"too many items\")\n", "")
    _edit(orders, "orders/pricing.py", "    if subtotal > DISCOUNT_THRESHOLD:\n", "    if subtotal >= DISCOUNT_THRESHOLD:\n")
    res = _review(orders)
    guards = _by(res, "security")
    assert [(f["rule"], f["at"], f["status"]) for f in guards] == [
        ("guard-removed", "orders/service.py:15", "statically_verified")]
    assert guards[0]["side"] == "base"


def test_security_operations_on_changed_lines(orders):
    _edit(orders, "orders/api.py", "from orders.repository import OrderRepository\n",
          "import subprocess as sp\n\nfrom orders.repository import OrderRepository\n")
    _edit(orders, "orders/api.py", "    order = fetch_order(get_repo(), order_id)\n",
          "    sp.run(\n        f\"echo {order_id}\",\n        shell=True,\n    )\n"
          "    order = fetch_order(get_repo(), order_id)\n")
    _edit(orders, "orders/repository.py", '"SELECT id, customer, total FROM orders WHERE id = ?", (order_id,)',
          'f"SELECT id, customer, total FROM orders WHERE id = {order_id}"')
    res = _review(orders)
    ops = {(f["at"], f["status"]) for f in _by(res, "security", "op-on-changed-line")}
    assert ("orders/api.py:27", "statically_verified") in ops   # sp.run bound through the alias import
    assert ("orders/api.py:29", "statically_verified") in ops   # the shell=True keyword's own line
    assert ("orders/repository.py:24", "statically_verified") in ops   # SQL built with an f-string
    assert any(f["at"] == "orders/repository.py:24" for f in _by(res, "persistence"))
    [sql] = [f for f in _by(res, "security") if f["at"] == "orders/repository.py:24"]
    assert sql["param_flow"]["from_entry"] == "get_order_handler(order_id) -> fetch_order(order_id) -> get(order_id)"
    assert "orders/api.py:26" in sql["evidence_at"]
    [shell] = [f for f in _by(res, "security") if f["at"] == "orders/api.py:29"]
    assert shell["param_flow"]["from_entry"] == "get_order_handler(order_id)"


def test_java_guard_permission_and_check_made_constant(jvm):
    _edit(jvm, "src/main/java/com/ex/net/Network.java",
          "        if (context.player().distanceTo(payload.pos()) > 8.0) {\n            return;\n        }\n", "")
    _edit(jvm, "src/main/java/com/ex/core/Forge.java", "        return player != null;\n", "        return true;\n")
    res = _review(jvm)
    rules = {(f["rule"], f["at"]) for f in res["concerns"]["security"]}
    assert ("guard-removed", "src/main/java/com/ex/net/Network.java:14") in rules
    assert ("check-made-constant", "src/main/java/com/ex/core/Forge.java:16") in rules
    entries = _by(res, "entry_points", "changed-entry")
    assert entries and entries[0]["entry_kind"] == "client->server packet"
    assert "src/main/java/com/ex/net/Network.java:7" in entries[0]["evidence_at"]
    assert any(u["kind"] == "runtime_tests" for u in res["unknown"])


def test_permission_check_removed_by_text_rule(tmp_path):
    repo = tmp_path / "cmd"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "Cmds.java").write_text(
        "package x;\n\nclass Cmds {\n    void register(D d) {\n        d.register(literal(\"glow\")\n"
        "                .requires(s -> s.hasPermissionLevel(2))\n                .executes(Cmds::run));\n    }\n\n"
        "    static int run(C c) {\n        return 1;\n    }\n}\n", encoding="utf-8", newline="\n")
    _scan(repo)
    _edit(repo, "src/Cmds.java", "                .requires(s -> s.hasPermissionLevel(2))\n", "")
    res = _review(repo)
    [f] = _by(res, "security", "permission-removed")
    assert f["at"] == "src/Cmds.java:6" and f["status"] == "strong_inference" and f["side"] == "base"


# -- performance -------------------------------------------------------------------------------------------

def test_n_plus_one_with_the_loop_bound_found(orders):
    _edit(orders, "orders/service.py", "    total = compute_total(items)\n    return repo.save(customer, total)\n",
          "    total = compute_total(items)\n    for item in items:\n"
          "        last = repo.save(customer, item[\"price\"])\n    return last\n")
    res = _review(orders)
    [f] = _by(res, "performance", "io-in-loop")
    assert f["at"] == "orders/service.py:22" and "orders/service.py:23" in f["evidence_at"]
    assert f["bound"]["at"] == "orders/service.py:15"
    assert not any(u["kind"] == "loop_bound" for u in res["unknown"])


def test_n_plus_one_without_a_bound_is_an_unknown(orders):
    _edit(orders, "orders/service.py", "    if len(items) > MAX_ITEMS_PER_ORDER:\n"
          "        raise ValidationError(\"too many items\")\n", "")
    _edit(orders, "orders/service.py", "    total = compute_total(items)\n    return repo.save(customer, total)\n",
          "    total = compute_total(items)\n    for item in items:\n"
          "        last = repo.save(customer, item[\"price\"])\n    return last\n")
    res = _review(orders)
    [f] = _by(res, "performance", "io-in-loop")
    assert "bound" not in f
    [u] = [u for u in res["unknown"] if u["kind"] == "loop_bound"]
    assert u["at"] == "orders/service.py:20" and u["next_step"]


def test_loop_added_in_a_tick_registered_by_method_reference(jvm):
    _edit(jvm, "src/main/java/com/ex/core/Forge.java", "        forge.cool();\n",
          "        forge.cool();\n        for (int i = 0; i < 100; i++) {\n            forge.cool();\n        }\n")
    res = _review(jvm)
    [f] = _by(res, "performance", "loop-added-hot-path")
    assert f["at"] == "src/main/java/com/ex/core/Forge.java:10"
    assert "src/main/java/com/ex/core/ForgeBlock.java:5" in f["evidence_at"]
    assert "block entity ticker" in f["finding"] and f["status"] == "strong_inference"


# -- public API ---------------------------------------------------------------------------------------------

def test_python_arity_breaks_are_statically_verified(orders):
    _edit(orders, "orders/pricing.py", "def apply_discount(subtotal: float) -> float:",
          "def apply_discount(subtotal: float, rate: float) -> float:")
    res = _review(orders)
    breaks = {(f["at"], f["status"]) for f in _by(res, "public_api", "arity-break")}
    assert breaks == {("orders/pricing.py:8", "statically_verified"), ("tests/test_pricing.py:5", "statically_verified"),
                      ("tests/test_pricing.py:9", "statically_verified")}
    assert all("missing required argument(s): rate" in f["finding"] for f in _by(res, "public_api"))


def test_a_removed_function_still_imported_and_called(orders):
    _edit(orders, "orders/service.py", "\n\ndef fetch_order(repo: OrderRepository, order_id: int):\n"
          "    return repo.get(order_id)\n", "\n")
    res = _review(orders)
    ats = {f["at"] for f in _by(res, "public_api", "removed-still-used")}
    assert {"orders/api.py:4", "orders/api.py:25", "tests/test_service.py:4", "tests/test_service.py:10"} <= ats


def test_a_rewritten_import_is_not_a_removed_name(orders):
    # found on the held-out fixture HO4 (after the rules were frozen): the old import statement's key goes away,
    # the name it bound is still imported by the new statement
    _edit(orders, "orders/pricing.py", "from orders.config import DISCOUNT_THRESHOLD\n",
          "from orders.config import DATABASE_URL, DISCOUNT_THRESHOLD\n")
    res = _review(orders)
    assert {c["kind"] for c in res["changes"]} == {"module_statement"}
    assert res["concerns"]["public_api"] == []


def test_java_arity_break_ignores_a_same_named_class_elsewhere(jvm):
    _edit(jvm, "src/main/java/com/ex/core/Forge.java", "    public static int stoke(Object pos, int amount) {",
          "    public static int stoke(Object pos, int amount, boolean natural) {")
    res = _review(jvm)
    ats = {f["at"] for f in _by(res, "public_api", "arity-break")}
    assert ats == {"src/main/java/com/ex/net/Network.java:17", "src/main/java/com/ex/core/ForgeBlock.java:9"}
    assert not any("reference/" in (f["at"] or "") for f in res["concerns"]["public_api"])


# -- config -------------------------------------------------------------------------------------------------

def test_env_default_change_and_its_readers(orders):
    _edit(orders, "orders/config.py", '"100.0"', '"150.0"')
    res = _review(orders)
    [f] = _by(res, "config")
    assert f["at"] == "orders/config.py:7" and f["key"] == "ORDERS_DISCOUNT_THRESHOLD"
    readers = {r["reader"] for r in res["binding_readers"]}
    assert {"orders/pricing.py::apply_discount", "orders/config.py::load_settings"} <= readers


def test_config_file_key_with_its_reader(tmp_path):
    repo = tmp_path / "cfg"
    (repo / "src").mkdir(parents=True)
    (repo / "settings.yml").write_text("repair:\n  blocks_per_tick: 8\n", encoding="utf-8", newline="\n")
    (repo / "src" / "Conf.java").write_text("package c;\n\nclass Conf {\n    int n(M m) {\n"
                                            "        return m.get(\"blocks_per_tick\");\n    }\n}\n", encoding="utf-8")
    _scan(repo)
    _edit(repo, "settings.yml", "blocks_per_tick: 8", "blocks_per_tick: 500")
    res = _review(repo)
    assert [(c["symbol"], c["kind"]) for c in res["changes"]] == [("settings.yml::repair.blocks_per_tick",
                                                                    "config_key")]
    [f] = _by(res, "config", "config-file-key")
    assert f["at"] == "settings.yml:2" and f["readers"] == ["src/Conf.java:5"]


def test_data_files_are_listed_not_read_as_config_and_a_new_config_file_is_one_change(orders):
    # found by reviewing Verinoda's own branch: every key of new JSON result files became a "config key"
    (orders / "results.json").write_text('{"a": {"b": 1, "c": 2}}\n', encoding="utf-8")
    (orders / "settings.toml").write_text("[x]\nlimit = 3\nname = 'n'\n", encoding="utf-8")
    res = _review(orders)
    assert [(c["symbol"], c["kind"]) for c in res["changes"]] == [("settings.toml", "added")]
    kinds = {f["file"]: f["kind"] for f in res["files"]}
    assert kinds == {"results.json": "data", "settings.toml": "config"}
    assert [f["rule"] for f in res["concerns"]["config"]] == ["config-file"]


def test_a_guard_moved_into_a_new_helper_is_not_removed(orders):
    _edit(orders, "orders/service.py", "    if len(items) > MAX_ITEMS_PER_ORDER:\n"
          "        raise ValidationError(\"too many items\")\n", "    _check_size(items)\n")
    _edit(orders, "orders/service.py", "def place_order(", "def _check_size(items: list[dict]) -> None:\n"
          "    if len(items) > MAX_ITEMS_PER_ORDER:\n        raise ValidationError(\"too many items\")\n\n\n"
          "def place_order(")
    res = _review(orders)
    [f] = _by(res, "security")
    assert f["rule"] == "guard-moved" and f["status"] == "weak_inference" and f["side"] == "base"


# -- tests, dependents, budget ----------------------------------------------------------------------------

def test_static_tests_and_code_no_test_reaches(orders):
    _edit(orders, "orders/pricing.py", "    return subtotal\n",
          "    return subtotal\n\n\ndef unused(x: float) -> float:\n    return x\n")
    _edit(orders, "orders/pricing.py", "    return apply_discount(subtotal)\n", "    return round(apply_discount(subtotal), 2)\n")
    _edit(orders, "orders/api.py", "        _repo = OrderRepository()\n", "        _repo = OrderRepository(\":memory:\")\n")
    res = _review(orders)
    tests = {t["test"] for t in res["tests"]["static"]}
    assert {"tests/test_pricing.py::test_compute_total", "tests/test_service.py::test_place_and_fetch_roundtrip"} <= tests
    # get_repo has callers (the handlers) and no test reaches them; `unused` has no caller at all: its reach is unknown
    assert res["tests"]["no_test_reaches"] == ["orders/api.py::get_repo"]
    assert [r["symbol"] for r in res["tests"]["reach_unknown"]] == ["orders/pricing.py::unused"]


def test_dependents_have_chains_and_truncation_is_reported(orders, monkeypatch):
    monkeypatch.setattr(rv, "MAX_DEPENDENTS", 1)
    _edit(orders, "orders/pricing.py", "        return round(subtotal * 0.9, 2)\n", "        return subtotal * 0.9\n")
    res = _review(orders)
    assert res["dependents_total"] == 3 and len(res["dependents"]) == 1 and res["dependents_truncated"]
    d = res["dependents"][0]
    assert d["symbol"] == "orders/pricing.py::compute_total" and d["via"][0]["at"] == "orders/pricing.py:8"
    assert d["via"][0]["confidence"] == "EXTRACTED" and d["status"] == "possibly affected"
    monkeypatch.setattr(rv, "MAX_DEPENDENTS", 40)
    full = {d["symbol"]: d.get("receives_value") for d in _review(orders)["dependents"]}
    # compute_total returns the value, place_order passes it to save; the handler gets save's row id instead
    assert full == {"orders/pricing.py::compute_total": True, "orders/service.py::place_order": True,
                    "orders/api.py::create_order_handler": False}


def test_read_first_is_packed_to_the_budget(orders):
    _edit(orders, "orders/pricing.py", "        return round(subtotal * 0.9, 2)\n", "        return subtotal * 0.9\n")
    small = _review(orders, max_chars=300)
    assert small["budget"]["used_chars"] <= 300 and small["budget"]["truncated"]
    assert small["budget"]["more_total"] == len(small["budget"]["more"]) or small["budget"]["more_total"] > 20
    big = _review(orders)
    assert big["read_first"][0]["at"].startswith("orders/pricing.py:") and not big["budget"]["truncated"]


# -- modes and safety ---------------------------------------------------------------------------------------

def test_planned_change_before_editing(orders):
    res = _review(orders, targets=["orders/pricing.py::apply_discount"], change="signature")
    assert res["mode"] == "planned" and res["base"] is None
    assert [c["kind"] for c in res["changes"]] == ["signature"]
    assert {d["symbol"] for d in res["dependents"]} >= {"orders/pricing.py::compute_total"}
    assert "security" in res["coverage"]["not_checked"][0]
    rm = _review(orders, targets=["orders/service.py::fetch_order"], change="remove")
    assert {f["at"] for f in _by(rm, "public_api")} >= {"orders/api.py:25", "tests/test_service.py:10"}


@pytest.mark.parametrize("target", ["../outside.py::f", "/etc/passwd::x", ".git/config::x", "C:/x.py::y"])
def test_planned_targets_must_stay_inside_the_repository(orders, target):
    with pytest.raises(ValueError, match="inside the repository"):
        _review(orders, targets=[target])


def test_refs_that_could_be_options_are_refused(orders):
    with pytest.raises(ValueError, match="may not start with '-'"):
        _review(orders, base="--output=/tmp/x")
    with pytest.raises(ValueError, match="does not name a commit"):
        _review(orders, base="no-such-branch")


def test_staged_changes_only(orders):
    _edit(orders, "orders/pricing.py", "        return round(subtotal * 0.9, 2)\n", "        return subtotal * 0.9\n")
    _git(orders, "add", "orders/pricing.py")
    _edit(orders, "orders/service.py", "    validate_items(items)\n", "    validate_items(list(items))\n")
    res = _review(orders, staged=True)
    assert res["mode"] == "staged"
    assert [c["symbol"] for c in res["changes"]] == ["orders/pricing.py::apply_discount"]


def test_the_review_is_recorded_without_touching_the_users_files(orders):
    _edit(orders, "orders/pricing.py", "        return round(subtotal * 0.9, 2)\n", "        return subtotal * 0.9\n")
    before = {p: p.read_bytes() for p in orders.rglob("*.py")}
    index_before = (orders / ".git" / "index").read_bytes()
    res = _review(orders)
    assert res["review_id"].startswith("rev_")
    st = open_store(orders)
    try:
        row = st.get("analyses", res["review_id"])
    finally:
        st.close()
    assert row["result"]["kind"] == "review" and row["question"].startswith("review ")
    assert {p: p.read_bytes() for p in orders.rglob("*.py")} == before
    assert (orders / ".git" / "index").read_bytes() == index_before


def test_a_test_id_that_could_be_an_option_is_never_run(orders, monkeypatch):
    from verinoda import experiments

    ran = []
    monkeypatch.setattr(experiments, "run", lambda st, repo, argv, **kw: ran.append(argv) or {"id": "exp_x",
                                                                                                "outcome": "pass"})
    monkeypatch.setattr(rv, "_static_tests", lambda ctx, changes: (
        {"-p.py::test_x": {"distance": 1, "reaches": []}, "tests/test_pricing.py::test_compute_total":
         {"distance": 1, "reaches": []}}, {}))
    _edit(orders, "orders/pricing.py", "        return round(subtotal * 0.9, 2)\n", "        return subtotal * 0.9\n")
    res = _review(orders, run_tests=True)
    assert res["tests"]["run"]["outcome"] == "pass"
    assert ran and "-p.py::test_x" not in ran[0] and "tests/test_pricing.py::test_compute_total" in ran[0]


def test_unknown_concern_is_refused(orders):
    with pytest.raises(ValueError, match="unknown concern"):
        _review(orders, concerns=["style"])


# -- CLI and MCP ---------------------------------------------------------------------------------------------

def test_cli_review_json_and_text(orders, capsys):
    from verinoda.cli import main

    _edit(orders, "orders/pricing.py", "        return round(subtotal * 0.9, 2)\n", "        return subtotal * 0.9\n")
    assert main(["review", str(orders), "--json"]) == 3
    res = json.loads(capsys.readouterr().out)
    assert res["changes"][0]["symbol"] == "orders/pricing.py::apply_discount"
    assert main(["review", str(orders), "--concerns", "persistence"]) == 3
    text = capsys.readouterr().out
    assert text.startswith("Review of the working tree against HEAD") and "persistence:" in text
    assert "Read first" in text
    assert main(["review", str(orders), "--base", "HEAD", "--staged"]) == 2


def test_mcp_change_review(orders):
    from verinoda.mcp.server import AtlasTools

    _edit(orders, "orders/pricing.py", "def apply_discount(subtotal: float) -> float:",
          "def apply_discount(subtotal: float, rate: float) -> float:")
    t = AtlasTools(orders)
    res = t.change_review()
    assert res["exit"] == 3 and len(res["concerns"]["public_api"]) == 3 and "summary" in res
    assert t.change_review(base="HEAD", staged=True)["error"] == "invalid_argument"
    assert t.change_review(change="body")["error"] == "invalid_argument"
    assert t.change_review(concerns=["nope"])["error"] == "invalid_argument"
    assert t.change_review(base="-x")["error"] == "invalid_argument"
    planned = t.change_review(targets=["orders/pricing.py::compute_total"], change="body")
    assert planned["mode"] == "planned"


# -- the fixtures harness -----------------------------------------------------------------------------------

def test_harness_matching_rules():
    from verinoda.benchmark import review_eval as ev

    f = {"concern": "security", "at": "a.py:10", "evidence_at": ["b.py:3"], "status": "strong_inference"}
    assert ev.matches({"concern": "security", "at": "a.py:9-11"}, f)
    assert ev.matches({"concern": "security", "at": "b.py:3"}, f)
    assert ev.matches({"concern": "security"}, f)
    assert not ev.matches({"concern": "config", "at": "a.py:10"}, f)
    assert not ev.matches({"concern": "security", "at": "a.py:11"}, f)


def test_the_fixture_files_match_their_manifest_or_its_amendments():
    import hashlib

    fx = ROOT / "benchmarks" / "review_fixtures"
    man = json.loads((fx / "MANIFEST.json").read_text(encoding="utf-8"))
    for name in ("dev.json", "heldout.json"):
        sha = hashlib.sha256((fx / name).read_bytes()).hexdigest()
        allowed = {man["files"][name]} | {a["sha256"] for a in man.get("amendments", []) if a["file"] == name}
        assert sha in allowed, name
        data = json.loads((fx / name).read_text(encoding="utf-8"))
        assert data["schema"] == "verinoda.review_fixtures/1"
        assert len(data["fixtures"]) == man["counts"][name]


# -- review round 1: the reviewers' findings (each test reproduces one) ------------------------------------------

def _project(tmp_path, name: str, files: dict[str, str]) -> Path:
    repo = tmp_path / name
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8", newline="\n")
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n.verinoda/\n", encoding="utf-8")
    return _scan(repo)


def test_staged_diff_of_many_files_reads_every_blob(tmp_path):
    # r1: 900 staged files put every path on `git ls-files` (past the Windows 32,767-character command line): each
    # staged file was read as deleted and every definition "removed" (500 paths of 79 characters exceed that limit)
    repo = tmp_path / "many"
    names = [f"pkg/a_rather_long_directory_name_for_paths/module_with_a_long_file_name_{i:04d}.py" for i in range(500)]
    for rel in names:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("def f(x):\n    return x + 1\n", encoding="utf-8", newline="\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    for rel in names:
        (repo / rel).write_text("def f(x):\n    return x + 2\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    head = _git(repo, "rev-parse", "HEAD").strip()
    diffs, skipped = rv._diff_staged(repo, head)
    assert len(diffs) == 500 and not skipped
    assert all(d.old and d.new and "x + 2" in d.new for d in diffs)


def test_a_staged_blob_that_cannot_be_read_is_an_error_not_a_deletion(orders, monkeypatch):
    from verinoda import treestate

    _edit(orders, "orders/pricing.py", "        return round(subtotal * 0.9, 2)\n", "        return subtotal * 0.9\n")
    _git(orders, "add", "orders/pricing.py")
    monkeypatch.setattr(treestate, "read_blobs", lambda repo, specs, **kw: {s: None for s in specs})
    with pytest.raises(treestate.NotAGitTree, match="could not read"):
        _review(orders, staged=True)


PKG_FILES = {
    "pkg/__init__.py": "",
    "pkg/rules.py": "def validate(value):\n    return value\n\n\ndef apply_all(items, validate):\n"
                    "    return [validate(i) for i in items]\n",
    "pkg/use_from.py": "from pkg.rules import validate\n\n\ndef a(v):\n    return validate(v)\n",
    "pkg/use_frommod.py": "from pkg import rules\n\n\ndef b(v):\n    return rules.validate(v)\n",
    "pkg/use_import.py": "import pkg.rules\n\n\ndef c(v):\n    return pkg.rules.validate(v)\n",
    "pkg/use_alias.py": "import pkg.rules as r\n\n\ndef d(v):\n    return r.validate(v)\n",
}


def test_python_call_sites_through_module_imports_and_a_shadowing_parameter(tmp_path):
    # r1: `from pkg import rules; rules.validate()` and `import pkg.rules; pkg.rules.validate()` were missed, and the
    # parameter `validate` of apply_all was read as a call to the changed function (statically_verified)
    repo = _project(tmp_path, "pkg", PKG_FILES)
    _edit(repo, "pkg/rules.py", "def validate(value):", "def validate(value, strict):")
    res = _review(repo)
    ats = {f["at"]: f["status"] for f in _by(res, "public_api", "arity-break")}
    assert ats == {"pkg/use_from.py:5": "statically_verified", "pkg/use_frommod.py:5": "statically_verified",
                   "pkg/use_import.py:5": "statically_verified", "pkg/use_alias.py:5": "statically_verified"}
    _git(repo, "checkout", "--", "pkg/rules.py")
    _edit(repo, "pkg/rules.py", "def validate(value):\n    return value\n\n\n", "")
    rm = {f["at"] for f in _by(_review(repo), "public_api", "removed-still-used")}
    assert {"pkg/use_from.py:1", "pkg/use_from.py:5", "pkg/use_frommod.py:5", "pkg/use_import.py:5",
            "pkg/use_alias.py:5"} <= rm
    assert "pkg/rules.py:6" not in rm


def test_a_removed_method_still_called_through_an_object(orders):
    # r1 + r2: OrderRepository.get removed while fetch_order calls repo.get (repo annotated): exit 0 before
    _edit(orders, "orders/repository.py", "\n    def get(self, order_id: int):\n        row = self.conn.execute(\n"
          "            \"SELECT id, customer, total FROM orders WHERE id = ?\", (order_id,)\n        ).fetchone()\n"
          "        return None if row is None else {\"id\": row[0], \"customer\": row[1], \"total\": row[2]}\n", "")
    res = _review(orders)
    [f] = _by(res, "public_api", "removed-still-used")
    assert f["at"] == "orders/service.py:26" and f["status"] == "strong_inference" and res["exit"] == 3
    assert "tests/test_service.py::test_place_and_fetch_roundtrip" in {t["test"] for t in res["tests"]["static"]}


def test_planned_removal_of_a_method_lists_its_callers(orders):
    res = _review(orders, targets=["orders/repository.py::OrderRepository.get"], change="remove")
    assert "orders/service.py:26" in {f["at"] for f in _by(res, "public_api", "removed-still-used")}
    assert res["exit"] == 3


def test_staged_review_reads_the_index_for_every_file(orders):
    # r2 R18: the staged signature change breaks the staged tests; the working tree's (unstaged) test update hid it
    _edit(orders, "orders/pricing.py", "def apply_discount(subtotal: float) -> float:",
          "def apply_discount(subtotal: float, rate: float) -> float:")
    _edit(orders, "orders/pricing.py", "    return apply_discount(subtotal)\n", "    return apply_discount(subtotal, 0.1)\n")
    _git(orders, "add", "orders/pricing.py")
    _edit(orders, "tests/test_pricing.py", "apply_discount(200.0)", "apply_discount(200.0, 0.1)")
    _edit(orders, "tests/test_pricing.py", "apply_discount(50.0)", "apply_discount(50.0, 0.1)")
    staged = {f["at"] for f in _by(_review(orders, staged=True), "public_api", "arity-break")}
    assert staged == {"tests/test_pricing.py:5", "tests/test_pricing.py:9"}
    assert _by(_review(orders), "public_api", "arity-break") == []


def test_staged_security_ops_are_read_from_the_index(tmp_path):
    # r1: an unstaged subprocess call on a staged line gave a statically_verified finding for code not staged
    repo = _project(tmp_path, "sd", {"app/__init__.py": "", "app/runner.py": "import subprocess\n\n\n"
                                     "def launch(x):\n    return x\n"})
    _edit(repo, "app/runner.py", "    return x\n", "    return x + 1\n")
    _git(repo, "add", "app/runner.py")
    _edit(repo, "app/runner.py", "    return x + 1\n", "    return subprocess.run(x)\n")
    assert _by(_review(repo, staged=True), "security") == []
    assert _by(_review(repo), "security", "op-on-changed-line")


def test_staged_run_tests_run_the_staged_tree_or_are_refused(orders, monkeypatch):
    # r1: --staged --run-tests ran the working tree and reported a pass for a staged change that fails
    from verinoda import experiments

    ran = []
    monkeypatch.setattr(experiments, "run", lambda st, repo, argv, **kw: ran.append(kw) or {"id": "exp_x",
                                                                                             "outcome": "pass"})
    _edit(orders, "orders/pricing.py", "        return round(subtotal * 0.9, 2)\n", "        return 0\n")
    _git(orders, "add", "orders/pricing.py")
    res = _review(orders, staged=True, run_tests=True)
    assert ran and ran[0]["overlay"] == ["orders/pricing.py"] and ran[0]["ref"] == res["base"]["commit"]
    assert res["tests"]["run"]["source"].startswith("the staged tree")
    _edit(orders, "orders/pricing.py", "        return 0\n", "        return round(subtotal * 0.9, 2)\n")
    ran.clear()
    res = _review(orders, staged=True, run_tests=True, observe=True)
    assert not ran and "unstaged changes" in res["tests"]["run"]["refused"]
    assert "differ from the staged tree" in res["tests"]["observe"]["refused"]


def test_a_python_file_with_a_byte_order_mark(tmp_path):
    repo = _project(tmp_path, "bom", {"app/__init__.py": ""})
    p = repo / "app" / "runner.py"
    p.write_bytes(b"\xef\xbb\xbfimport subprocess\n\n\ndef launch(cmd, depth):\n    if depth < 0:\n        return None\n"
                  b"    return cmd\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "bom")
    p.write_bytes(b"\xef\xbb\xbfimport subprocess\n\n\ndef launch(cmd, depth):\n"
                  b"    return subprocess.run(cmd, shell=True)\n")
    res = _review(repo)
    assert [(c["symbol"], c["kind"]) for c in res["changes"]] == [("app/runner.py::launch", "body")]
    assert {f["rule"] for f in _by(res, "security")} >= {"op-on-changed-line", "guard-removed"}


def test_a_new_function_named_like_a_method_is_not_that_method(orders):
    # r1: a new module-level save() took OrderRepository.save's node: false dependents, entry point and tests
    (orders / "orders" / "repository.py").write_text((orders / "orders" / "repository.py").read_text(
        encoding="utf-8") + "\n\ndef save(path):\n    return path\n", encoding="utf-8", newline="\n")
    res = _review(orders)
    assert [(c["symbol"], c["kind"]) for c in res["changes"]] == [("orders/repository.py::save", "added")]
    assert res["dependents"] == [] and res["tests"]["static"] == [] and res["concerns"]["entry_points"] == []


MOD_FILES = {
    "src/main/java/com/ex/mod/Forge.java": """package com.ex.mod;

public class Forge {
    int heat;
    int progress;

    public void stoke(Object player) {
        heat = heat + 5;
        setChanged();
    }

    protected void loadAdditional(Tag tag) {
        heat = tag.getInt("Heat");
        progress = tag.getInt("Progress");
    }

    protected void saveAdditional(Tag tag) {
        tag.putInt("Heat", heat);
        tag.putInt("Progress", progress);
    }

    static void handle(Payload payload, Context context) {
        if (context.player() == null) {
            return;
        }
        if (context.player().distanceTo(payload.pos()) > 8.0) {
            return;
        }
        Forge.poke(payload.pos());
    }

    static void poke(Object pos) {
    }

    public static void serverTick(Object level, Object pos, Forge forge) {
        forge.cool();
    }

    void cool() {
    }
}
""",
    "src/main/java/com/ex/mod/ForgeBlock.java": """package com.ex.mod;

public class ForgeBlock {
    Object ticker(Object type) {
        return createTickerHelper(type, Forge::serverTick);
    }
}
""",
    "src/main/java/com/ex/mod/ModConfig.java": """package com.ex.mod;

import java.nio.file.Files;
import java.nio.file.Path;

public record ModConfig(int blocksPerTick, int maxDistance) {
    private static final ModConfig DEFAULTS = new ModConfig(8, 8);

    public static ModConfig load(Path file) throws Exception {
        String text = Files.readString(file);
        return text.isEmpty() ? DEFAULTS : new ModConfig(DEFAULTS.blocksPerTick(), DEFAULTS.maxDistance());
    }
}
""",
    "src/main/java/com/ex/mod/Scheduler.java": """package com.ex.mod;

public final class Scheduler {
    public static void register(Events events) {
        ServerTickEvents.END_SERVER_TICK.register(Scheduler::tick);
    }

    static void tick(Object server) {
        int budget = 8;
        while (budget-- > 0) {
            Object p = next();
        }
    }

    static Object next() {
        return null;
    }
}
""",
    "src/main/java/com/ex/shop/Cart.java": """package com.ex.shop;

public class Cart {
    java.util.List<Integer> items;

    public int get(int i) {
        return items.get(i);
    }

    public static int first(Cart c) {
        return c.items.get(0);
    }

    public int size() {
        return items.size();
    }
}
""",
    "src/main/java/com/ex/shop/Prices.java": """package com.ex.shop;

import java.util.Map;
import java.util.function.ToIntFunction;

public class Prices {
    Map<String, Integer> prices;

    int price(String name, Cart cart) {
        return prices.get(name) + cart.size();
    }

    ToIntFunction<Cart> firstOf() {
        return Cart::first;
    }
}
""",
    "src/main/kotlin/com/ex/mod/Commands.kt": """package com.ex.mod

object Commands {
    fun register(d: Dispatcher) {
        d.register(literal("reset").requires { it.hasPermission(2) && EmberConfig.ALLOW_RESET.get() })
    }
}
""",
    "src/main/resources/fabric.mod.json": """{
  "schemaVersion": 1,
  "id": "exmod",
  "entrypoints": {
    "main": ["com.ex.mod.Main"],
    "fabric-gametest": ["com.ex.mod.test.Tests"]
  }
}
""",
    "src/main/ts/users.ts": """export function update(users: string[]): number {
  return users.length;
}
""",
}


@pytest.fixture(scope="module")
def mod_base(tmp_path_factory):
    dst = tmp_path_factory.mktemp("review") / "mod_base"
    for rel, text in MOD_FILES.items():
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        (dst / rel).write_text(text, encoding="utf-8", newline="\n")
    (dst / ".gitignore").write_text(".verinoda/\n", encoding="utf-8")
    return _scan(dst)


@pytest.fixture()
def mod(mod_base, tmp_path):
    return _clone(mod_base, tmp_path / "mod")


FORGE = "src/main/java/com/ex/mod/Forge.java"


def test_java_removed_method_with_a_common_name_is_not_strong_on_another_receiver(mod):
    # r1: Cart.get removed; `prices.get(name)` (a Map) was a strong_inference removed-still-used
    _edit(mod, "src/main/java/com/ex/shop/Cart.java", "    public int get(int i) {\n        return items.get(i);\n"
          "    }\n\n", "")
    res = _review(mod)
    found = {(f["at"], f["status"]) for f in _by(res, "public_api", "removed-still-used")}
    assert ("src/main/java/com/ex/shop/Prices.java:10", "strong_inference") not in found
    assert not any(rr.at_least_strong(s) for _a, s in found)


def test_java_method_reference_to_a_removed_method_is_still_used(mod):
    # r2 R47: a method renamed while `Cart::first` (a registration) still names the old one
    _edit(mod, "src/main/java/com/ex/shop/Cart.java", "public static int first(Cart c)", "public static int head(Cart c)")
    res = _review(mod)
    assert ("src/main/java/com/ex/shop/Prices.java:14", "strong_inference") in {
        (f["at"], f["status"]) for f in _by(res, "public_api", "removed-still-used")}
    [added] = [c for c in res["changes"] if c["kind"] == "added"]
    assert added["renamed_from"] == "Cart.first"
    assert not any(r["at"].endswith("(base)") and "Cart.java" in r["at"] for r in res["read_first"])


def test_ts_function_named_update_is_not_a_game_tick_and_export_is_one_change(mod):
    # r1: `export function update` got a module_statement change too, and a strong "called every tick" hot path
    _edit(mod, "src/main/ts/users.ts", "  return users.length;\n", "  let n = 0;\n  for (const u of users) {\n"
          "    n += u.length;\n  }\n  return n;\n")
    res = _review(mod)
    assert [(c["symbol"], c["kind"]) for c in res["changes"]] == [("src/main/ts/users.ts::update", "body")]
    assert res["concerns"]["performance"] == []


def test_permission_removed_is_reported_once_for_the_method(tmp_path):
    repo = _project(tmp_path, "door", {"src/main/java/com/example/Door.java": "package com.example;\n\n"
                    "public class Door {\n    int limit = 3;\n\n    public void open(Player p) {\n"
                    "        if (!p.hasPermission(\"door.open\")) {\n            return;\n        }\n"
                    "        swing();\n    }\n\n    void swing() {\n    }\n}\n"})
    _edit(repo, "src/main/java/com/example/Door.java", "int limit = 3;", "int limit = 5;")
    _edit(repo, "src/main/java/com/example/Door.java", "        if (!p.hasPermission(\"door.open\")) {\n"
          "            return;\n        }\n", "")
    [f] = _by(_review(repo), "security", "permission-removed")
    assert f["for"].endswith("Door.open")


def test_removed_dirty_flag_and_nbt_write_with_other_sinks_left(mod):
    # r2 R19/R21: setChanged() removed, and one tag.putInt removed while another stays
    _edit(mod, FORGE, "        heat = heat + 5;\n        setChanged();\n", "        heat = heat + 5;\n")
    _edit(mod, FORGE, "        tag.putInt(\"Heat\", heat);\n        tag.putInt(\"Progress\", progress);\n",
          "        tag.putInt(\"Heat\", heat);\n")
    res = _review(mod)
    removed = {f["at"] for f in _by(res, "persistence", "sink-line-removed")}
    assert {f"{FORGE}:9", f"{FORGE}:19"} <= removed


def test_saved_data_key_read_that_is_never_written_is_persistence_not_config(mod):
    # r2 R20: loadAdditional reads "heat" while saveAdditional writes "Heat"
    _edit(mod, FORGE, "heat = tag.getInt(\"Heat\");", "heat = tag.getInt(\"heat\");")
    res = _review(mod)
    [f] = _by(res, "persistence", "saved-data-key-mismatch")
    assert f["at"] == f"{FORGE}:13" and f"{FORGE}:18" in f["evidence_at"]
    assert res["concerns"]["config"] == []


def test_guard_extracted_into_a_helper_or_folded_into_the_next_if_is_not_removed(mod):
    # r2 R22/R23: behaviour-preserving refactors of a guard were "statically_verified: the guard is gone"
    _edit(mod, FORGE, "        if (context.player().distanceTo(payload.pos()) > 8.0) {\n",
          "        if (tooFar(context, payload.pos())) {\n")
    _edit(mod, FORGE, "    static void poke(Object pos) {\n",
          "    private static boolean tooFar(Context context, Object pos) {\n"
          "        return context.player().distanceTo(pos) > 8.0;\n    }\n\n    static void poke(Object pos) {\n")
    sec = _by(_review(mod), "security")
    assert [f["rule"] for f in sec if f["rule"].startswith("guard")] == ["guard-moved"]
    assert sec[0]["status"] == "weak_inference" and "tooFar" in sec[0]["finding"]
    _git(mod, "checkout", "--", FORGE)
    _edit(mod, FORGE, "        if (context.player().distanceTo(payload.pos()) > 8.0) {\n            return;\n        }\n"
          "        Forge.poke(payload.pos());\n", "        if (context.player().distanceTo(payload.pos()) <= 8.0) {\n"
          "            Forge.poke(payload.pos());\n        }\n")
    [g] = [f for f in _by(_review(mod), "security") if f["rule"].startswith("guard")]
    assert g["rule"] == "guard-restructured" and g["status"] == "weak_inference"


def test_a_guard_split_in_two_and_a_renamed_parameter(orders):
    # r2 R33: `a or b` split into two guards; R53: a parameter renamed changes no guard and reads no new config
    _edit(orders, "orders/service.py", "    if not items:\n        raise ValidationError(\"order has no items\")\n"
          "    if len(items) > MAX_ITEMS_PER_ORDER:\n        raise ValidationError(\"too many items\")\n",
          "    if not items or len(items) > MAX_ITEMS_PER_ORDER:\n        raise ValidationError(\"bad order\")\n")
    _git(orders, "commit", "-qam", "one guard")
    _edit(orders, "orders/service.py", "    if not items or len(items) > MAX_ITEMS_PER_ORDER:\n"
          "        raise ValidationError(\"bad order\")\n", "    if not items:\n        raise ValidationError(\"bad order\")\n"
          "    if len(items) > MAX_ITEMS_PER_ORDER:\n        raise ValidationError(\"bad order\")\n")
    [f] = _by(_review(orders), "security")
    assert f["rule"] == "guard-split" and f["status"] == "weak_inference"
    _git(orders, "checkout", "--", "orders/service.py")
    text = (orders / "orders" / "service.py").read_text(encoding="utf-8")
    text = text.replace("def validate_items(items: list[dict])", "def validate_items(order_items: list[dict])")
    text = text.replace("    if not items or len(items) > MAX", "    if not order_items or len(order_items) > MAX")
    (orders / "orders" / "service.py").write_text(text, encoding="utf-8", newline="\n")
    res = _review(orders)
    assert res["concerns"]["security"] == [] and res["concerns"]["config"] == []


def test_a_removed_call_to_a_validator(orders):
    # r2 R37: place_order no longer calls validate_items, which raises on bad orders
    _edit(orders, "orders/service.py", "    validate_items(items)\n    total", "    total")
    [f] = _by(_review(orders), "security", "check-call-removed")
    assert f["at"] == "orders/service.py:20" and f["status"] == "strong_inference" and f["side"] == "base"
    assert {"orders/service.py:13", "orders/service.py:15"} <= set(f["evidence_at"])


def test_yaml_load_imported_by_name_and_a_new_handler_is_an_entry(orders):
    # r2 R03/R41: `from yaml import load` was not yaml.load; a new handler had no entry point or parameter flow
    _edit(orders, "orders/api.py", "from orders.repository import OrderRepository\n",
          "from yaml import Loader, load\n\nfrom orders.repository import OrderRepository\n")
    (orders / "orders" / "tools.py").write_text("import subprocess\n\n\ndef export_orders(target: str) -> int:\n"
                                                "    return subprocess.run(\"dump > \" + target, shell=True).returncode\n",
                                                encoding="utf-8", newline="\n")
    (orders / "orders" / "api.py").write_text((orders / "orders" / "api.py").read_text(encoding="utf-8")
                                              + "\n\ndef import_orders_handler(body: str) -> tuple[int, dict]:\n"
                                              "    data = load(body, Loader=Loader)\n    return 200, {\"n\": len(data)}\n"
                                              "\n\ndef export_handler(target: str) -> tuple[int, dict]:\n"
                                              "    from orders.tools import export_orders\n\n"
                                              "    return 200, {\"rc\": export_orders(target)}\n",
                                              encoding="utf-8", newline="\n")
    res = _review(orders)
    kinds = {(f["at"], f["finding"].split(":")[0]) for f in _by(res, "security", "op-on-changed-line")}
    assert ("orders/api.py:34", "a changed line adds deserialization") in kinds
    [shell] = [f for f in _by(res, "security") if f["at"] == "orders/tools.py:5"]
    assert shell["param_flow"]["from_entry"] == "export_handler(target) -> export_orders(target)"
    entries = {f["at"] for f in _by(res, "entry_points", "changed-entry")}
    assert {"orders/api.py:33", "orders/api.py:38"} <= entries


def test_prose_with_select_and_from_is_not_sql(orders):
    # r2 R05: an error message "Select ... from ..." was statically_verified SQL built from strings
    _edit(orders, "orders/api.py", "        return 400, {\"error\": str(exc)}\n",
          "        return 400, {\"error\": \"Select at least one item from the catalogue: \" + str(exc)}\n")
    res = _review(orders)
    assert res["concerns"]["security"] == [] and res["concerns"]["persistence"] == []
    assert not rr.sql_shaped("Could not delete from cache") and not rr.sql_shaped("Select one item from the list")
    assert rr.sql_shaped("SELECT id FROM t") and rr.sql_shaped("select name from users where id = ?")


def test_sql_built_with_plus_and_executed_is_verified(orders):
    _edit(orders, "orders/repository.py", "        row = self.conn.execute(\n"
          "            \"SELECT id, customer, total FROM orders WHERE id = ?\", (order_id,)\n        ).fetchone()\n",
          "        sql = \"SELECT id, customer, total FROM orders WHERE id = \" + str(order_id)\n"
          "        row = self.conn.execute(sql).fetchone()\n")
    [f] = [f for f in _by(_review(orders), "security") if "sql" in f["finding"]]
    assert f["status"] == "statically_verified" and "passed to a database call at line 24" in f["finding"]


def test_an_existing_security_call_on_a_changed_line_is_not_added(tmp_path):
    repo = _project(tmp_path, "adds", {"app/__init__.py": "", "app/runner.py": "import subprocess\n\n\n"
                                       "def launch(cmd):\n    return subprocess.run(cmd)\n"})
    _edit(repo, "app/runner.py", "    return subprocess.run(cmd)", "    out = subprocess.run(cmd)\n    return out")
    [f] = _by(_review(repo), "security", "op-on-changed-line")
    assert f["status"] == "weak_inference" and "had on its changed lines too" in f["finding"]
    # r2-2 C26: the same call with other arguments "changes" it (a keyword with a constant: strong_inference)
    _git(repo, "checkout", "--", "app/runner.py")
    _edit(repo, "app/runner.py", "subprocess.run(cmd)", "subprocess.run(cmd, timeout=5)")
    [f] = _by(_review(repo), "security", "op-on-changed-line")
    assert f["status"] == "strong_inference" and "changes process-exec" in f["finding"] and "adds" not in f["finding"]


def test_a_security_call_whose_constant_argument_became_a_parameter(tmp_path):
    # r2-2 C26: subprocess.run(["git", "status"]) -> subprocess.run(cmd, shell=...) and pickle.loads(b"...") ->
    # pickle.loads(blob) were "holds ... too" (weak_inference, exit 0): only the operation kinds were compared
    repo = _project(tmp_path, "c26", {"app/__init__.py": "", "app/tasks.py": "import pickle\nimport subprocess\n\n\n"
                                      "def status(cmd):\n    return subprocess.run([\"git\", \"status\"], capture_output="
                                      "True)\n\n\ndef restore(blob):\n    return pickle.loads(b\"\\x80\\x04N.\")\n"})
    _edit(repo, "app/tasks.py", "subprocess.run([\"git\", \"status\"], capture_output=True)",
          "subprocess.run(cmd, capture_output=True, shell=isinstance(cmd, str))")
    _edit(repo, "app/tasks.py", "pickle.loads(b\"\\x80\\x04N.\")", "pickle.loads(blob)")
    res = _review(repo)
    got = {f["at"]: f for f in _by(res, "security", "op-on-changed-line")}
    assert {a: f["status"] for a, f in got.items()} == {"app/tasks.py:6": "statically_verified",
                                                        "app/tasks.py:10": "statically_verified"}
    assert "changes process-exec" in got["app/tasks.py:6"]["finding"] and "now read cmd" in got["app/tasks.py:6"]["finding"]
    assert "now read blob" in got["app/tasks.py:10"]["finding"] and res["exit"] == 3


def test_security_calls_through_an_assigned_alias_or_a_renamed_re_export(tmp_path):
    # r2-2 C08: `run_cmd = subprocess.run` / `unpack = pickle.loads` called on changed lines were no longer found once
    # the engine was skipped for lines without a target's name; C22: `from app.compat import run_command` (compat:
    # `from subprocess import run as run_command`) was never found
    repo = _project(tmp_path, "c08", {
        "app/__init__.py": "",
        "app/compat.py": "import pickle\nfrom subprocess import run as run_command\n\nload_blob = pickle.loads\n",
        "app/tasks.py": "import pickle\nimport subprocess\n\nfrom app import compat\nfrom app.compat import load_blob, "
                        "run_command\n\nrun_cmd = subprocess.run\nunpack = pickle.loads\n\n\ndef launch(cmd):\n"
                        "    return cmd\n\n\ndef restore(blob):\n    return blob\n\n\ndef launch2(cmd):\n    return cmd\n\n\n"
                        "def restore2(blob):\n    return blob\n\n\ndef launch3(cmd):\n    return cmd\n"})
    text = (repo / "app" / "tasks.py").read_text(encoding="utf-8")
    for fn, call in (("launch", "run_cmd(cmd)"), ("restore", "unpack(blob)"), ("launch2", "run_command(cmd)"),
                     ("restore2", "load_blob(blob)"), ("launch3", "compat.run_command(cmd)")):
        arg = "blob" if "blob" in call else "cmd"
        text = text.replace(f"def {fn}({arg}):\n    return {arg}\n", f"def {fn}({arg}):\n    return {call}\n")
    (repo / "app" / "tasks.py").write_text(text, encoding="utf-8", newline="\n")
    got = {f["at"]: f["status"] for f in _by(_review(repo), "security", "op-on-changed-line")}
    assert got == {"app/tasks.py:12": "statically_verified", "app/tasks.py:16": "statically_verified",
                   "app/tasks.py:20": "statically_verified", "app/tasks.py:24": "statically_verified",
                   "app/tasks.py:28": "statically_verified"}


def test_a_caller_committed_after_the_last_snapshot_is_searched_and_named(tmp_path):
    # r2-2 C05: the Python searches read only the graph's importing files: a caller added after the last scan was
    # silently missed (arity break, removed name, binding reader), with no graph_stale unknown
    repo = _project(tmp_path, "c05", {"pkg/__init__.py": "", "pkg/rules.py": "LIMIT = 3\n\n\ndef validate(value):\n"
                                      "    return value > 0\n", "pkg/old_user.py": "from pkg.rules import validate\n\n\n"
                                      "def check_old(v):\n    return validate(v)\n"})
    (repo / "pkg" / "new_user.py").write_text("from pkg.rules import LIMIT, validate\n\n\ndef check_new(v):\n"
                                              "    return validate(v) and v < LIMIT\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "pkg/new_user.py")
    _git(repo, "commit", "-qm", "a new caller")
    _edit(repo, "pkg/rules.py", "def validate(value):\n", "def validate(value, strict):\n")
    res = _review(repo)
    assert {f["at"] for f in _by(res, "public_api", "arity-break")} == {"pkg/old_user.py:5", "pkg/new_user.py:5"}
    assert res["graph"]["stale_files"] == ["pkg/new_user.py"] and "graph_stale" in {u["kind"] for u in res["unknown"]}
    _git(repo, "checkout", "--", "pkg/rules.py")
    _edit(repo, "pkg/rules.py", "\n\ndef validate(value):\n    return value > 0\n", "\n")
    assert {"pkg/new_user.py:1", "pkg/new_user.py:5"} <= {f["at"] for f in _by(_review(repo), "public_api")}
    _git(repo, "checkout", "--", "pkg/rules.py")
    _edit(repo, "pkg/rules.py", "LIMIT = 3\n", "LIMIT = 30\n")
    assert [b["at"] for b in _review(repo)["binding_readers"]] == ["pkg/new_user.py:5"]


def test_value_bound_on_a_changed_line_and_written_later(orders):
    # r2 R01: the changed total is saved by repo.save on the next (unchanged) line
    _edit(orders, "orders/service.py", "    total = compute_total(items)\n", "    total = compute_total(items) * 1.18\n")
    [f] = _by(_review(orders), "persistence", "changed-value-to-sink")
    assert f["at"] == "orders/service.py:22" and "orders/repository.py:17" in f["evidence_at"]


def test_value_bound_on_a_changed_line_and_written_by_the_next_statement(tmp_path):
    # r2 R30: `vals` changed on one line, passed to the INSERT on the next
    repo = _project(tmp_path, "ins", {"db/__init__.py": "", "db/store.py": "import json\n\n\nclass Store:\n"
                    "    def insert(self, table, row):\n        cols = list(row)\n"
                    "        vals = [json.dumps(row[c], sort_keys=True) for c in cols]\n"
                    "        self.conn.execute(\n            f\"INSERT INTO {table} VALUES ({len(cols)})\", vals\n        )\n"})
    _edit(repo, "db/store.py", "json.dumps(row[c], sort_keys=True)", "json.dumps(row[c])")
    [f] = _by(_review(repo), "persistence", "changed-value-to-sink")
    assert f["at"] == "db/store.py:9"


def test_the_removed_commit_is_reported_while_the_insert_stays(orders):
    # r2 R02
    _edit(orders, "orders/repository.py", "        self.conn.commit()\n        return cur.lastrowid\n",
          "        return cur.lastrowid\n")
    [f] = _by(_review(orders), "persistence", "sink-line-removed")
    assert f["at"] == "orders/repository.py:19" and f["side"] == "base"


def test_swapped_positional_parameters(orders):
    # r2 R11
    _edit(orders, "orders/repository.py", "    def save(self, customer: str, total: float) -> int:",
          "    def save(self, total: float, customer: str) -> int:")
    [f] = _by(_review(orders), "public_api", "positional-order-changed")
    assert f["at"] == "orders/service.py:22" and "was customer, is now total" in f["finding"]


def test_new_and_renamed_functions_reached_by_tests_in_the_same_diff(orders):
    # r2 R08: a new function and its new test; R07: fetch_order renamed with its callers and its test
    _edit(orders, "orders/pricing.py", "    return subtotal\n", "    return subtotal\n\n\n"
          "def bulk_discount(subtotal: float, count: int) -> float:\n    return subtotal * 0.95 if count >= 10 else subtotal\n")
    _edit(orders, "tests/test_pricing.py", "from orders.pricing import apply_discount, compute_total\n",
          "from orders.pricing import apply_discount, bulk_discount, compute_total\n\n\ndef test_bulk_discount():\n"
          "    assert bulk_discount(100.0, 10) == 95.0\n")
    for rel, a, b in (("orders/service.py", "def fetch_order(repo", "def load_order(repo"),
                      ("orders/api.py", "fetch_order, place_order\n", "load_order, place_order\n"),
                      ("orders/api.py", "order = fetch_order(", "order = load_order("),
                      ("tests/test_service.py", "fetch_order, place_order\n", "load_order, place_order\n"),
                      ("tests/test_service.py", "assert fetch_order(", "assert load_order(")):
        _edit(orders, rel, a, b)
    res = _review(orders)
    reach = {t["test"]: t["reaches"] for t in res["tests"]["static"]}
    assert "orders/pricing.py::bulk_discount" in reach["tests/test_pricing.py::test_bulk_discount"]
    assert "orders/service.py::load_order" in reach["tests/test_service.py::test_place_and_fetch_roundtrip"]
    assert res["tests"]["no_test_reaches"] == []
    assert res["concerns"]["persistence"] == [] and res["concerns"]["public_api"] == []


def test_a_nested_function_is_reached_through_its_enclosing_function(orders):
    # r2 R35: a change inside a nested function had no dependents and "no test reaches"
    _edit(orders, "orders/pricing.py", "    subtotal = sum(i[\"price\"] * i[\"qty\"] for i in items)\n",
          "    def line(i):\n        return i[\"price\"] * i[\"qty\"]\n\n    subtotal = sum(line(i) for i in items)\n")
    _git(orders, "commit", "-qam", "nested")
    _edit(orders, "orders/pricing.py", "        return i[\"price\"] * i[\"qty\"]\n", "        return i[\"price\"] * i[\"qty\"] * 1\n")
    res = _review(orders)
    assert [c["symbol"] for c in res["changes"]] == ["orders/pricing.py::compute_total.line"]
    assert "orders/service.py::place_order" in {d["symbol"] for d in res["dependents"]}
    assert "tests/test_pricing.py::test_compute_total" in {t["test"] for t in res["tests"]["static"]}


def test_config_key_renamed_reports_both_keys_and_the_old_reader(tmp_path):
    # r2 R25: the (at, rule) de-duplication dropped the removed key and its reader
    repo = _project(tmp_path, "cfgr", {"config/mod.toml": "[forge]\n\tstoke_heat = 15\n",
                                       "src/Conf.java": "package c;\n\nclass Conf {\n    int n(M m) {\n"
                                       "        return m.getInt(\"stoke_heat\");\n    }\n}\n"})
    _edit(repo, "config/mod.toml", "\tstoke_heat = 15\n", "\tstoke_heat_amount = 15\n")
    fs = {f["key"]: f for f in _by(_review(repo), "config", "config-file-key")}
    assert set(fs) == {"forge.stoke_heat", "forge.stoke_heat_amount"}
    assert fs["forge.stoke_heat"]["readers"] == ["src/Conf.java:5"] and fs["forge.stoke_heat"]["side"] == "base"


def test_a_config_record_default_and_a_config_method_call(mod):
    # r2 R26: a default of a config record changed; R44: ModConfig.load() is not "a config value"
    _edit(mod, "src/main/java/com/ex/mod/ModConfig.java", "new ModConfig(8, 8);", "new ModConfig(8, 64);")
    [f] = _by(_review(mod), "config", "config-default-changed")
    assert f["at"] == "src/main/java/com/ex/mod/ModConfig.java:7" and "DEFAULTS" in f["finding"]
    assert "src/main/java/com/ex/mod/ModConfig.java:11" in f["evidence_at"]


def test_file_io_added_in_a_tick_loop(mod):
    # r2 R44: a call reaching Files.readString added inside the loop of a tick-registered method
    _edit(mod, "src/main/java/com/ex/mod/Scheduler.java", "            Object p = next();\n",
          "            Object p = next();\n            ModConfig.load(null);\n")
    res = _review(mod)
    [f] = _by(res, "performance", "io-in-loop")
    assert f["status"] == "strong_inference" and "tick event" in f["finding"]
    assert not any("ModConfig.load" in f["finding"] for f in res["concerns"]["config"])


def test_a_changed_mod_manifest_entrypoint_is_an_entry_point_change(mod):
    # r2 R29: fabric.mod.json was "data, not reviewed" with exit 0
    _edit(mod, "src/main/resources/fabric.mod.json", "    \"main\": [\"com.ex.mod.Main\"],\n"
          "    \"fabric-gametest\": [\"com.ex.mod.test.Tests\"]\n", "    \"main\": [\"com.ex.mod.Main\"]\n")
    res = _review(mod)
    [f] = _by(res, "entry_points", "registration-manifest-changed")
    assert "fabric-gametest" in f["finding"] and f["side"] == "base" and res["exit"] == 3
    assert res["concerns"]["config"] == []


def test_kotlin_permission_change_is_not_a_config_read(mod):
    # r2 R24: the unchanged config read beside the changed permission level
    _edit(mod, "src/main/kotlin/com/ex/mod/Commands.kt", "hasPermission(2)", "hasPermission(0)")
    res = _review(mod)
    assert [f["rule"] for f in res["concerns"]["security"]] == ["permission-changed"]
    assert res["concerns"]["config"] == []


def test_a_rename_does_not_report_the_unchanged_call_beside_it(orders):
    # r2 R07: get_repo() on the renamed line was "the changed call ... reaches a sql-write"
    _edit(orders, "orders/api.py", "    order = fetch_order(get_repo(), order_id)\n",
          "    order = fetch_order(get_repo(), int(order_id))\n")
    assert _by(_review(orders), "persistence", "changed-call-reaches-sink") == []


def test_performance_on_a_literal_and_a_loop_without_io(orders):
    # r2 R16/R45: a loop over a 2-item literal and a no-IO loop in a handler are no strong findings
    _edit(orders, "orders/api.py", "    try:\n        order_id = place_order(",
          "    for item in payload[\"items\"]:\n        item[\"qty\"] = int(item[\"qty\"])\n"
          "    try:\n        order_id = place_order(")
    _edit(orders, "orders/repository.py", "        self.conn.execute(\n            \"CREATE TABLE IF NOT EXISTS orders "
          "(id INTEGER PRIMARY KEY, customer TEXT, total REAL)\"\n        )\n",
          "        for ddl in (\n            \"CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, customer TEXT, "
          "total REAL)\",\n            \"CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, note TEXT)\",\n"
          "        ):\n            self.conn.execute(ddl)\n")
    perf = _by(_review(orders), "performance")
    assert perf and not any(rr.at_least_strong(f["status"]) for f in perf)


def test_documentation_is_no_dependent_and_vendored_copies_are_other_projects(tmp_path):
    # r2 R28/R30: a README heading as a dependent; a vendored copy importing its own `store`
    repo = _project(tmp_path, "vend", {
        "README.md": "# Guide\n\nCall `compute()` from `use()`.\n",
        "app/__init__.py": "", "app/core.py": "def compute(x):\n    return x\n",
        "app/use.py": "from app.core import compute\n\n\ndef use(v):\n    return compute(v)\n",
        "app/store.py": "class Store:\n    def insert(self, row):\n        return row\n",
        "vendor/old/repoatlas/__init__.py": "", "vendor/old/repoatlas/store.py": "class Store:\n    def insert(self, row):\n"
        "        return row\n",
        "vendor/old/repoatlas/user.py": "from repoatlas.store import Store\n\n\ndef keep(store: Store):\n"
        "    return store.insert(1)\n"})
    _edit(repo, "app/core.py", "    return x\n", "    return x + 1\n")
    _edit(repo, "app/store.py", "        return row\n", "        return [row]\n")
    res = _review(repo)
    assert {d["symbol"] for d in res["dependents"]} == {"app/use.py::use"}
    st = open_store(repo)
    try:
        ctx = rv._Ctx(repo, None, st, {})
        assert rv._imports_namesake(ctx, "vendor/old/repoatlas/user.py", "app/store.py")
        assert not rv._imports_namesake(ctx, "app/use.py", "app/core.py")
    finally:
        st.close()


def test_cli_review_usage_errors_and_the_project_root_from_a_subdirectory(orders, capsys, monkeypatch):
    from verinoda.cli import main

    assert main(["review", str(orders), "--target", "orders/pricing.py::apply_discount", "--staged"]) == 2
    assert main(["review", str(orders), "--target", "orders/pricing.py::apply_discount", "--base", "HEAD"]) == 2
    assert main(["review", str(orders), "--max-chars", "-5"]) == 2
    capsys.readouterr()
    monkeypatch.chdir(orders / "orders")
    assert main(["review", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["changes"] == []


def test_planned_signature_change_lists_the_call_sites(orders):
    res = _review(orders, targets=["orders/pricing.py::apply_discount"], change="signature")
    sites = {f["at"]: f["status"] for f in _by(res, "public_api", "call-site-of-changed-signature")}
    assert sites == {"orders/pricing.py:8": "weak_inference", "tests/test_pricing.py:5": "weak_inference",
                     "tests/test_pricing.py:9": "weak_inference"}


# -- review round 2: the second reviewers' findings (each test reproduces one) -----------------------------------

ADMIN_BASE = '''import sqlite3

conn = sqlite3.connect(":memory:")


def is_admin(user):
    return user is not None and user.get("role") == "admin"


def delete_order(user, order_id):
    if user is None or user.get("role") != "admin":
        raise PermissionError("admin only")
    conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
    conn.commit()
    if is_admin(user):
        print("audit: admin deleted", order_id)


def archive_order(user, order_id):
    conn.execute("UPDATE orders SET archived = 1 WHERE id = ?", (order_id,))
    conn.commit()
'''
ADMIN_GUARD = '''    if user is None or user.get("role") != "admin":
        raise PermissionError("admin only")
'''
ADMIN_WRITE = '''    conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
    conn.commit()
'''
IS_ADMIN_GUARD = "    if not is_admin(user):\n        raise PermissionError(\"admin only\")\n"


@pytest.fixture()
def admin(tmp_path):
    return _project(tmp_path, "admin", {"app/__init__.py": "", "app/orders.py": ADMIN_BASE,
                                        "app/jobs.py": "from app.orders import delete_order\n\n\n"
                                                       "def _purge(user, oid):\n    return delete_order(user, oid)\n"})


def _guards_of(res: dict) -> list[tuple[str, str, str]]:
    return [(f["rule"], f["status"], f["at"]) for f in _by(res, "security") if "guard" in f["rule"]
            or f["rule"].startswith("check-call")]


def test_a_removed_guard_is_not_restructured_by_a_condition_the_base_already_had(admin):
    # r2-2 C25: `if not is_admin(user): raise` deleted; the unchanged `if is_admin(user): audit` after the write held
    # its negation and made the removal a weak guard-restructured (exit 0)
    _edit(admin, "app/orders.py", ADMIN_GUARD, IS_ADMIN_GUARD)
    _git(admin, "commit", "-qam", "is_admin guard")
    _edit(admin, "app/orders.py", IS_ADMIN_GUARD, "")
    res = _review(admin)
    assert _guards_of(res) == [("guard-removed", "statically_verified", "app/orders.py:11")] and res["exit"] == 3
    # a new negated condition that gates only a new line (not the work the guard preceded) is no restructuring
    _git(admin, "checkout", "--", "app/orders.py")
    _edit(admin, "app/orders.py", IS_ADMIN_GUARD, "")
    _edit(admin, "app/orders.py", "    conn.commit()\n    if is_admin(user):\n        print(",
          "    conn.commit()\n    if is_admin(user):\n        print(user)\n    if is_admin(user):\n        print(")
    assert [r for r, _s, _a in _guards_of(_review(admin))] == ["guard-removed"]


def test_a_guard_split_extracted_or_moved_below_the_write_is_after_work(admin):
    # r2-2 C04b: the second half of a split guard, or a guard calling an extracted helper, placed after the DELETE
    # and commit was "the same check" (weak, exit 0)
    _edit(admin, "app/orders.py", ADMIN_GUARD + ADMIN_WRITE, "    if user is None:\n        raise PermissionError(\"x\")\n"
          + ADMIN_WRITE + "    if user.get(\"role\") != \"admin\":\n        raise PermissionError(\"x\")\n")
    res = _review(admin)
    assert _guards_of(res) == [("guard-after-work", "statically_verified", "app/orders.py:15")] and res["exit"] == 3
    assert "conn.execute('DELETE FROM orders" in _by(res, "security")[0]["finding"]
    _git(admin, "checkout", "--", "app/orders.py")
    _edit(admin, "app/orders.py", ADMIN_GUARD + ADMIN_WRITE, ADMIN_WRITE + "    if not_admin(user):\n"
          "        raise PermissionError(\"x\")\n")
    (admin / "app" / "orders.py").write_text((admin / "app" / "orders.py").read_text(encoding="utf-8")
                                             + "\n\ndef not_admin(user):\n    return user is None or user.get(\"role\") != "
                                             "\"admin\"\n", encoding="utf-8", newline="\n")
    assert _guards_of(_review(admin)) == [("guard-after-work", "statically_verified", "app/orders.py:13")]
    # the same guard moved below the write; the split kept in front of it is still the weak guard-split
    _git(admin, "checkout", "--", "app/orders.py")
    _edit(admin, "app/orders.py", ADMIN_GUARD + ADMIN_WRITE, ADMIN_WRITE + ADMIN_GUARD)
    assert _guards_of(_review(admin)) == [("guard-after-work", "statically_verified", "app/orders.py:13")]
    _git(admin, "checkout", "--", "app/orders.py")
    _edit(admin, "app/orders.py", ADMIN_GUARD, "    if user is None:\n        raise PermissionError(\"x\")\n"
          "    if user.get(\"role\") != \"admin\":\n        raise PermissionError(\"x\")\n")
    assert _guards_of(_review(admin)) == [("guard-split", "weak_inference", "app/orders.py:11")]


def test_a_validator_call_moved_after_the_save(orders):
    # r2-2 C15: validate_items(items) moved below repo.save(...) gave no security finding (the call still exists)
    _edit(orders, "orders/service.py", "    validate_items(items)\n    total = compute_total(items)\n"
          "    return repo.save(customer, total)\n", "    total = compute_total(items)\n"
          "    oid = repo.save(customer, total)\n    validate_items(items)\n    return oid\n")
    [f] = _by(_review(orders), "security")
    assert (f["rule"], f["status"], f["at"]) == ("check-call-after-work", "strong_inference", "orders/service.py:22")
    assert "orders/service.py:21" in f["evidence_at"] and "orders/service.py:13" in f["evidence_at"]


def test_a_guard_moved_to_a_function_off_its_call_path_is_removed(admin):
    # r2-2 C04 moved_to_unrelated: the guard deleted from delete_order and added to archive_order (which neither
    # calls nor is called by delete_order) was a weak guard-moved
    _edit(admin, "app/orders.py", ADMIN_GUARD + ADMIN_WRITE, ADMIN_WRITE)
    _edit(admin, "app/orders.py", "def archive_order(user, order_id):\n", "def archive_order(user, order_id):\n"
          + ADMIN_GUARD)
    assert _guards_of(_review(admin)) == [("guard-removed", "statically_verified", "app/orders.py:11")]
