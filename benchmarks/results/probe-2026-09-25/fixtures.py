"""Probe fixtures (docs/DESIGN.md D36): mutants of examples/orders_app plus small extra modules, gold labels first.

Every fixture is a git copy of examples/orders_app with ``EXTRA`` added *at the base commit*, then ``edits``
applied to the working tree. ``target`` is what ``verinoda probe`` is asked about, ``options`` its arguments.

Gold (written before the probe ran on these fixtures; run_fixtures.py records this file's sha256):

* ``change``       the edit changes behaviour on some input of the function's domain while every existing test
                   still passes; a detection needs status ``differences_found`` (or ``property_violated``) with at
                   least one class in ``classes`` (any of them) and a counterexample.
* ``equivalent``   the edit preserves behaviour on the whole annotated domain (a refactor, a comment, nothing):
                   any reported difference - numeric drift included - is a false alarm. Run with 5 seeds.
* ``refuse``       the side-effect gate must refuse (``status: refused``) and name ``sink`` in a reason.
* ``run``          the gate must NOT refuse (a read, a print, a pure function): a refusal is an incorrect one.
* ``unsupported``  status ``unsupported`` with a reason.
* ``scaling``      with ``scaling`` the growth change must be flagged (``growth_changed``); run once more without
                   scaling, where no difference may be reported (``also_equivalent_without_scaling``).

Written by the builder of the probe (in-sample by construction: the builder also wrote the tool).
"""

EXTRA = {
    "orders/textutil.py": '''"""Text helpers."""

import re


def customer_key(name: str) -> str:
    return re.sub(r"[^\\w]+", "-", name.lower()).strip("-")


def normalize_city(s: str) -> str:
    return s.strip().casefold()


def display_name(name: str) -> str:
    return " ".join(name.split()).title()


def mask_email(email: str) -> str:
    name, _, domain = email.partition("@")
    return name[:1] + "***@" + domain


def title_ok(title: str) -> bool:
    return 0 < len(title) <= 80
''',
    "orders/mathutil.py": '''"""Number helpers."""


def safe_div(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return a / b


def clamp(x: int, lo: int, hi: int) -> int:
    return max(lo, min(x, hi))


def percent(part: float, whole: float, digits: int = 2) -> float:
    return round(part / whole * 100, digits)


def average(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def bucket(n: int) -> str:
    if n < 10:
        return "small"
    if n < 100:
        return "medium"
    return "large"
''',
    "orders/rules.py": '''"""Business rules."""


def is_eligible(age: int, member: bool) -> bool:
    return age >= 18 and member


def chunk(items: list[int], size: int) -> list[list[int]]:
    if size <= 0:
        raise ValueError("size must be positive")
    return [items[i:i + size] for i in range(0, len(items), size)]


def format_price(amount: float, currency: str = "EUR") -> str:
    return f"{amount:.2f} {currency}"
''',
    "orders/sideeffects.py": '''"""Functions with side effects (gate fixtures)."""

import subprocess
import urllib.request

COUNTER = 0
CACHE = {}


def save_report(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def fetch_status(url: str) -> int:
    with urllib.request.urlopen(url) as resp:
        return resp.status


def run_tool(args: list[str]) -> int:
    return subprocess.run(args, check=False).returncode


def bump(n: int) -> int:
    global COUNTER
    COUNTER += n
    return COUNTER


def remember(key: str, value: int) -> int:
    CACHE[key] = value
    return len(CACHE)


def read_setting(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.readline().strip()


def log_line(msg: str) -> int:
    print(msg)
    return len(msg)


def describe_order(order: dict) -> str:
    return f"{order.get('customer', '?')}: {len(order.get('items', []))} item(s)"


async def fetch_later(url: str) -> str:
    return url
''',
    "tests/test_extra.py": '''from orders.mathutil import average, bucket, clamp, percent, safe_div
from orders.rules import chunk, format_price, is_eligible
from orders.textutil import customer_key, display_name, mask_email, normalize_city, title_ok


def test_text():
    assert customer_key("Ada Lovelace") == "ada-lovelace"
    assert normalize_city(" Paris ") == "paris"
    assert display_name("ada lovelace") == "Ada Lovelace"
    assert mask_email("ada@example.com") == "a***@example.com"
    assert title_ok("A title")


def test_numbers():
    assert safe_div(6.0, 3.0) == 2.0
    assert clamp(5, 0, 10) == 5
    assert percent(1.0, 4.0) == 25.0
    assert average([1.0, 3.0]) == 2.0
    assert bucket(50) == "medium"


def test_rules():
    assert is_eligible(30, True)
    assert not is_eligible(10, False)
    assert chunk([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]
    assert format_price(3.5) == "3.50 EUR"
''',
    "mod/HeatMath.kt": '''package net.example.heat

object HeatMath {
    fun heatAfter(ticks: Int, rate: Double): Double = if (ticks <= 0) 0.0 else ticks * rate
}
''',
}

AD_OLD = "if subtotal > DISCOUNT_THRESHOLD:\n        return round(subtotal * 0.9, 2)"
CT_OLD = "subtotal = sum(i[\"price\"] * i[\"qty\"] for i in items)"

FIXTURES = [
    # -- behaviour changes the existing tests do not catch -------------------------------------------------
    {"id": "D01", "kind": "boundary off-by-one", "target": "orders/pricing.py::apply_discount",
     "edits": [("orders/pricing.py", "subtotal > DISCOUNT_THRESHOLD", "subtotal >= DISCOUNT_THRESHOLD")],
     "gold": "change", "classes": ["value_changed_at_mined_boundary", "value_changed"]},
    {"id": "D02", "kind": "rounding changed to truncation", "target": "orders/pricing.py::apply_discount",
     "edits": [("orders/pricing.py", "return round(subtotal * 0.9, 2)", "return int(subtotal * 0.9 * 100) / 100")],
     "gold": "change", "classes": ["value_changed", "value_changed_at_mined_boundary", "new_exception"]},
    {"id": "D03", "kind": "B1: off-by-one + truncation", "target": "orders/pricing.py::apply_discount",
     "edits": [("orders/pricing.py", AD_OLD,
                "if subtotal >= DISCOUNT_THRESHOLD:\n        return int(subtotal * 0.9 * 100) / 100")],
     "gold": "change", "classes": ["value_changed_at_mined_boundary", "value_changed", "new_exception"]},
    {"id": "D04", "kind": "B2: len boundary through an imported constant", "target": "orders/service.py::validate_items",
     "edits": [("orders/service.py", "len(items) > MAX_ITEMS_PER_ORDER", "len(items) >= MAX_ITEMS_PER_ORDER")],
     "gold": "change", "classes": ["new_exception"]},
    {"id": "D05", "kind": "removed guard (empty order)", "target": "orders/service.py::validate_items",
     "edits": [("orders/service.py", "    if not items:\n        raise ValidationError(\"order has no items\")\n", "")],
     "gold": "change", "classes": ["exception_removed"]},
    {"id": "D06", "kind": "B3: slice bound drops items", "target": "orders/pricing.py::compute_total",
     "edits": [("orders/pricing.py", CT_OLD, "subtotal = sum(i[\"price\"] * i[\"qty\"] for i in items[:10])")],
     "gold": "change", "classes": ["value_changed", "value_changed_at_mined_boundary"]},
    {"id": "D07", "kind": "B4: unicode mishandling in a regex", "target": "orders/textutil.py::customer_key",
     "edits": [("orders/textutil.py", "r\"[^\\w]+\"", "r\"[^a-z0-9]+\"")],
     "gold": "change", "classes": ["value_changed"]},
    {"id": "D08", "kind": "unicode: casefold -> lower", "target": "orders/textutil.py::normalize_city",
     "edits": [("orders/textutil.py", "s.strip().casefold()", "s.strip().lower()")],
     "gold": "change", "classes": ["value_changed"]},
    {"id": "D09", "kind": "removed guard (division by zero)", "target": "orders/mathutil.py::safe_div",
     "edits": [("orders/mathutil.py", "    if b == 0:\n        return 0.0\n", "")],
     "gold": "change", "classes": ["new_exception"]},
    {"id": "D10", "kind": "swapped condition (and -> or)", "target": "orders/rules.py::is_eligible",
     "edits": [("orders/rules.py", "age >= 18 and member", "age >= 18 or member")],
     "gold": "change", "classes": ["value_changed", "value_changed_at_mined_boundary"]},
    {"id": "D11", "kind": "len off-by-one (<= -> <)", "target": "orders/textutil.py::title_ok",
     "edits": [("orders/textutil.py", "len(title) <= 80", "len(title) < 80")],
     "gold": "change", "classes": ["value_changed_at_mined_boundary", "value_changed"]},
    {"id": "D12", "kind": "changed default (digits 2 -> 1)", "target": "orders/mathutil.py::percent",
     "edits": [("orders/mathutil.py", "digits: int = 2", "digits: int = 1")],
     "gold": "change", "classes": ["value_changed", "value_changed_at_mined_boundary"]},
    {"id": "D13", "kind": "range bound off-by-one", "target": "orders/rules.py::chunk",
     "edits": [("orders/rules.py", "range(0, len(items), size)", "range(0, len(items) - 1, size)")],
     "gold": "change", "classes": ["value_changed", "value_changed_at_mined_boundary"]},
    {"id": "D14", "kind": "swapped min/max", "target": "orders/mathutil.py::clamp",
     "edits": [("orders/mathutil.py", "max(lo, min(x, hi))", "min(hi, max(x, lo))")],
     "gold": "change", "classes": ["value_changed", "value_changed_at_mined_boundary"]},
    {"id": "D15", "kind": "changed default (currency)", "target": "orders/rules.py::format_price",
     "edits": [("orders/rules.py", "currency: str = \"EUR\"", "currency: str = \"USD\"")],
     "gold": "change", "classes": ["value_changed", "value_changed_at_mined_boundary"]},
    {"id": "D16", "kind": "B8: quadratic loop", "target": "orders/pricing.py::compute_total",
     "edits": [("orders/pricing.py", CT_OLD,
                "subtotal = sum(items[items.index(i)][\"price\"] * i[\"qty\"] for i in items)")],
     "options": {"scaling": True}, "gold": "scaling", "also_equivalent_without_scaling": True},
    {"id": "D17", "kind": "B6: save per item (side effects allowed)", "target": "orders/service.py::place_order",
     "edits": [("orders/service.py", "    total = compute_total(items)\n    return repo.save(customer, total)",
                "    oid = None\n    for item in items:\n        oid = repo.save(customer, compute_total([item]))\n"
                "    return oid")],
     "options": {"allow_side_effects": True}, "gold": "change", "classes": ["value_changed"]},
    {"id": "D18", "kind": "whitespace handling", "target": "orders/textutil.py::display_name",
     "edits": [("orders/textutil.py", "\" \".join(name.split()).title()", "name.strip().title()")],
     "gold": "change", "classes": ["value_changed"]},
    {"id": "D19", "kind": "changed module constant", "target": "orders/pricing.py::apply_discount",
     "edits": [("orders/config.py", "\"ORDERS_DISCOUNT_THRESHOLD\", \"100.0\"", "\"ORDERS_DISCOUNT_THRESHOLD\", \"150.0\"")],
     "gold": "change", "classes": ["value_changed", "value_changed_at_mined_boundary"]},
    {"id": "D20", "kind": "boundary off-by-one (< -> <=)", "target": "orders/mathutil.py::bucket",
     "edits": [("orders/mathutil.py", "if n < 10:", "if n <= 10:")],
     "gold": "change", "classes": ["value_changed_at_mined_boundary", "value_changed"]},
    {"id": "D21", "kind": "partition -> split (missing separator)", "target": "orders/textutil.py::mask_email",
     "edits": [("orders/textutil.py", "name, _, domain = email.partition(\"@\")",
                "name, domain = email.split(\"@\")[0], email.split(\"@\")[1]")],
     "gold": "change", "classes": ["new_exception"]},
    {"id": "D22", "kind": "removed guard (empty list)", "target": "orders/mathutil.py::average",
     "edits": [("orders/mathutil.py", "return sum(xs) / len(xs) if xs else 0.0", "return sum(xs) / len(xs)")],
     "gold": "change", "classes": ["new_exception"]},
    # -- behaviour-preserving edits (false alarms if anything is reported) --------------------------------
    {"id": "E01", "kind": "ternary rewrite", "target": "orders/pricing.py::apply_discount",
     "edits": [("orders/pricing.py", AD_OLD + "\n    return subtotal",
                "return round(subtotal * 0.9, 2) if subtotal > DISCOUNT_THRESHOLD else subtotal")],
     "gold": "equivalent"},
    {"id": "E02", "kind": "commuted multiplication", "target": "orders/pricing.py::apply_discount",
     "edits": [("orders/pricing.py", "round(subtotal * 0.9, 2)", "round(0.9 * subtotal, 2)")], "gold": "equivalent"},
    {"id": "E03", "kind": "variable rename", "target": "orders/pricing.py::compute_total",
     "edits": [("orders/pricing.py", CT_OLD + "\n    return apply_discount(subtotal)",
                "total = sum(i[\"price\"] * i[\"qty\"] for i in items)\n    return apply_discount(total)")],
     "gold": "equivalent"},
    {"id": "E04", "kind": "generator -> loop", "target": "orders/pricing.py::compute_total",
     "edits": [("orders/pricing.py", CT_OLD,
                "subtotal = 0\n    for i in items:\n        subtotal += i[\"price\"] * i[\"qty\"]")],
     "gold": "equivalent"},
    {"id": "E05", "kind": "len() refactor", "target": "orders/service.py::validate_items",
     "edits": [("orders/service.py", "    if not items:\n        raise ValidationError(\"order has no items\")\n"
                "    if len(items) > MAX_ITEMS_PER_ORDER:",
                "    n = len(items)\n    if n == 0:\n        raise ValidationError(\"order has no items\")\n"
                "    if n > MAX_ITEMS_PER_ORDER:")],
     "gold": "equivalent"},
    {"id": "E06", "kind": "precompiled regex", "target": "orders/textutil.py::customer_key",
     "edits": [("orders/textutil.py", "import re\n", "import re\n\n_NON_WORD = re.compile(r\"[^\\w]+\")\n"),
               ("orders/textutil.py", "re.sub(r\"[^\\w]+\", \"-\", name.lower())", "_NON_WORD.sub(\"-\", name.lower())")],
     "gold": "equivalent"},
    {"id": "E07", "kind": "and-operands swapped (bool domain)", "target": "orders/rules.py::is_eligible",
     "edits": [("orders/rules.py", "age >= 18 and member", "member and age >= 18")], "gold": "equivalent"},
    {"id": "E08", "kind": "zero test as truthiness", "target": "orders/mathutil.py::safe_div",
     "edits": [("orders/mathutil.py", "if b == 0:", "if not b:")], "gold": "equivalent"},
    {"id": "E09", "kind": "comment + docstring only, unrelated file edited", "target": "orders/pricing.py::apply_discount",
     "edits": [("orders/pricing.py", "    # 10% off above the configured threshold.",
                "    \"\"\"Apply the discount.\"\"\"\n    # ten percent off above the configured threshold"),
               ("orders/rules.py", "\"\"\"Business rules.\"\"\"", "\"\"\"Business rules (edited).\"\"\"")],
     "gold": "equivalent"},
    {"id": "E10", "kind": "strip/casefold order", "target": "orders/textutil.py::normalize_city",
     "edits": [("orders/textutil.py", "s.strip().casefold()", "s.casefold().strip()")], "gold": "equivalent"},
    {"id": "E11", "kind": "guard as early return", "target": "orders/mathutil.py::average",
     "edits": [("orders/mathutil.py", "    return sum(xs) / len(xs) if xs else 0.0",
                "    if not xs:\n        return 0.0\n    return sum(xs) / len(xs)")], "gold": "equivalent"},
    {"id": "E12", "kind": "no edit at all", "target": "orders/mathutil.py::bucket", "edits": [],
     "gold": "equivalent"},
    # -- the side-effect gate ------------------------------------------------------------------------------
    {"id": "R01", "kind": "B6: place_order writes through the repository", "target": "orders/service.py::place_order",
     "edits": [("orders/service.py", "    total = compute_total(items)\n    return repo.save(customer, total)",
                "    oid = None\n    for item in items:\n        oid = repo.save(customer, compute_total([item]))\n"
                "    return oid")],
     "gold": "refuse", "sink": "sql-write", "at": "orders/repository.py:17"},
    {"id": "R02", "kind": "global + database", "target": "orders/api.py::get_repo", "edits": [],
     "gold": "refuse", "sink": "global-state"},
    {"id": "R03", "kind": "file write", "target": "orders/sideeffects.py::save_report", "edits": [],
     "gold": "refuse", "sink": "file-write"},
    {"id": "R04", "kind": "network", "target": "orders/sideeffects.py::fetch_status", "edits": [],
     "gold": "refuse", "sink": "network"},
    {"id": "R05", "kind": "process", "target": "orders/sideeffects.py::run_tool", "edits": [],
     "gold": "refuse", "sink": "process"},
    {"id": "R06", "kind": "global counter", "target": "orders/sideeffects.py::bump", "edits": [],
     "gold": "refuse", "sink": "global-state"},
    {"id": "R07", "kind": "module-level cache", "target": "orders/sideeffects.py::remember", "edits": [],
     "gold": "refuse", "sink": "global-state"},
    {"id": "R08", "kind": "handler through the repository", "target": "orders/api.py::create_order_handler",
     "edits": [], "gold": "refuse", "sink": "sql-write"},
    {"id": "R09", "kind": "method whose recipe connects to a database", "target":
     "orders/repository.py::OrderRepository.get", "edits": [], "gold": "refuse", "sink": "db-connection"},
    {"id": "P01", "kind": "reads a file", "target": "orders/sideeffects.py::read_setting", "edits": [], "gold": "run"},
    {"id": "P02", "kind": "prints", "target": "orders/sideeffects.py::log_line", "edits": [], "gold": "run"},
    {"id": "P03", "kind": "pure, unchanged", "target": "orders/pricing.py::apply_discount", "edits": [],
     "gold": "run"},
    {"id": "P04", "kind": "reads a dict argument", "target": "orders/sideeffects.py::describe_order", "edits": [],
     "gold": "run"},
    # -- not probed -----------------------------------------------------------------------------------------
    {"id": "U01", "kind": "B7: Kotlin", "target": "mod/HeatMath.kt::HeatMath.heatAfter",
     "edits": [("mod/HeatMath.kt", "ticks <= 0", "ticks < 0")], "gold": "unsupported"},
    {"id": "U02", "kind": "coroutine function", "target": "orders/sideeffects.py::fetch_later", "edits": [],
     "gold": "unsupported"},
]
