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
    res = _review(orders)
    tests = {t["test"] for t in res["tests"]["static"]}
    assert {"tests/test_pricing.py::test_compute_total", "tests/test_service.py::test_place_and_fetch_roundtrip"} <= tests
    assert res["tests"]["no_test_reaches"] == ["orders/pricing.py::unused"]


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
