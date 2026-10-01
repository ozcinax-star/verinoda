"""``verinoda spec check`` (verinoda/specs.py): requirement criteria with the claims, tests and code pointers their
evidence lines name, each criterion with an evidence status and the exit code a CI check reads."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verinoda import cli, specs
from verinoda.claims import Claims
from verinoda.store import open_store

CART = """class Cart:
    def __init__(self):
        self.items = []

    def checkout(self):
        if not self.items:
            raise ValueError("empty")
        return sum(self.items)
"""

TESTS = """from src.cart import Cart


def test_empty_cart_disables_checkout():
    assert Cart().items == []


class TestTotals:
    def test_total_in_cents(self):
        assert True
"""


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _claim(repo: Path, status: str) -> str:
    st = open_store(repo, create=True)
    try:
        c = Claims(st, repo).create(f"a claim that is {status}", project="p", snapshot=None)
        with st.tx() as cur:
            cur.execute("UPDATE claims SET status = ? WHERE id = ?", (status, c["id"]))
        return c["id"]
    finally:
        st.close()


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "spec ğ repo"
    _write(r, "src/cart.py", CART)
    _write(r, "src/rules.conf", "max_items = 3\n")
    _write(r, "tests/test_cart.py", TESTS)
    return r


def _spec(repo: Path, text: str, rel: str = ".verinoda/specs/cart.md") -> None:
    _write(repo, rel, text)


def _by_id(res: dict) -> dict:
    return {c["id"]: c for c in res["criteria"]}


def test_statuses_of_each_kind_of_evidence(repo):
    ok, stale, weak = _claim(repo, "statically_verified"), _claim(repo, "stale"), _claim(repo, "weak_inference")
    _spec(repo, f"""# Cart

- [CART-1] WHEN the cart is empty THE SYSTEM SHALL refuse checkout
  - evidence: {ok}, src/cart.py::Cart.checkout
- [CART-2] THE SYSTEM SHALL keep the total in cents
  - evidence: tests/test_cart.py::TestTotals::test_total_in_cents[1-2], src/cart.py:5-8
- [CART-3] WHILE a payment is pending THE SYSTEM SHALL lock the cart
  - evidence: src/cart.py::Cart.checkout
- [CART-4] IF the price is negative THEN THE SYSTEM SHALL reject the item
- [CART-5] THE SYSTEM SHALL cap the cart
  - evidence: {stale}
- [CART-6] THE SYSTEM SHALL log checkouts
  - evidence: {weak}
- [CART-7] THE SYSTEM SHALL list items
  - evidence: tests/test_cart.py::test_gone, src/missing.py:3, src/cart.py:40, clm_000000000000
""")
    res = specs.check(repo)
    got = {k: c["status"] for k, c in _by_id(res).items()}
    assert got == {"CART-1": "verified", "CART-2": "tested", "CART-3": "unevidenced", "CART-4": "unevidenced",
                   "CART-5": "broken", "CART-6": "unevidenced", "CART-7": "broken"}
    assert res["exit"] == 1 and res["status"] == "unevidenced"
    c = _by_id(res)
    assert c["CART-1"]["evidence"][1]["at"] == "src/cart.py:5-8"
    assert c["CART-1"]["at"] == ".verinoda/specs/cart.md:3" and c["CART-1"]["ears"] == "event"
    assert (c["CART-3"]["ears"], c["CART-4"]["ears"], c["CART-2"]["ears"]) == ("state", "unwanted", "ubiquitous")
    assert c["CART-2"]["evidence"][0]["kind"] == "test" and c["CART-2"]["evidence"][0]["state"] == "ok"
    assert "stale" in c["CART-5"]["evidence"][0]["detail"]
    states = [(r["kind"], r["state"]) for r in c["CART-7"]["evidence"]]
    assert states == [("test", "broken"), ("lines", "broken"), ("lines", "broken"), ("claim", "broken")]
    assert "past the end" in c["CART-7"]["evidence"][2]["detail"]


def test_all_evidenced_passes_and_text_lists_every_criterion(repo, capsys):
    _spec(repo, "- [R1] THE SYSTEM SHALL check out\n"
                "  - evidence: tests/test_cart.py::test_empty_cart_disables_checkout\n")
    assert cli.main(["spec", "check", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "TESTED" in out and "[R1]" in out and "1 tested" in out
    assert cli.main(["spec", "check", "--repo", str(repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["tested"] == 1


def test_missing_folder_default_is_not_an_error_but_a_configured_one_is(repo, capsys):
    res = specs.check(repo)
    assert (res["status"], res["exit"]) == ("no_specs", 0)
    assert cli.main(["spec", "check", "--repo", str(repo), "--specs-dir", "docs/specs", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "unchecked"
    assert cli.main(["spec", "check", "--repo", str(repo), "--specs-dir", "../elsewhere", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_committed_folder_setting(repo):
    _write(repo, "verinoda.toml", '[specs]\ndir = "docs/specs"\n')
    _spec(repo, "- [R1] THE SYSTEM SHALL x\n", "docs/specs/a.md")
    res = specs.check(repo)
    assert res["dir"] == "docs/specs" and "verinoda.toml" in res["dir_source"] and len(res["criteria"]) == 1


def test_claims_without_state_are_unchecked(repo):
    _spec(repo, "- [R1] THE SYSTEM SHALL x\n  - evidence: clm_0123456789ab\n")
    res = specs.check(repo)
    assert res["criteria"][0]["status"] == "unchecked" and res["exit"] == 3
    assert not (repo / ".verinoda" / "atlas.db").exists()   # a read-only check never creates state


def test_malformed_lines_duplicates_fences_and_crlf(repo):
    _spec(repo, "# A\r\n"
                "evidence: src/cart.py\r\n"
                "- [x] a task box is not a criterion\r\n"
                "- [R1] THE SYSTEM SHALL a\r\n"
                "  - evidence:\r\n"
                "The system SHALL be fast, says a line with no id.\r\n"
                "```\r\n- [R9] THE SYSTEM SHALL be an example in a fence\r\n```\r\n"
                "- [r1] THE SYSTEM SHALL b\r\n"
                "  - Evidence: `src/cart.py::Cart.checkout` , src/cart.py\r\n")
    res = specs.check(repo)
    assert [c["id"] for c in res["criteria"]] == ["R1", "r1"]
    assert res["criteria"][1]["duplicate_of"] == ".verinoda/specs/cart.md:4"
    assert [r["state"] for r in res["criteria"][1]["evidence"]] == ["ok", "ok"]
    probs = " | ".join(res["problems"])
    assert "no criterion above" in probs and "empty evidence line" in probs and "no [ID]" in probs
    assert "already used" in probs and res["exit"] == 1


def test_paths_are_checked_inside_the_repo_with_their_case(repo):
    _spec(repo, "- [R1] THE SYSTEM SHALL a\n  - evidence: SRC/cart.py, ../outside.py, /etc/passwd, "
                "src/cart.py::Nope, bogus\n")
    (r1,) = specs.check(repo)["criteria"]
    assert [r["state"] for r in r1["evidence"]] == ["broken"] * 5
    assert r1["evidence"][4]["kind"] == "unknown"


def test_a_file_without_facts_is_searched_as_text_and_says_so(repo):
    _spec(repo, "- [R1] THE SYSTEM SHALL cap\n  - evidence: code:src/rules.conf::max_items\n")
    (ref,) = specs.check(repo)["criteria"][0]["evidence"]
    assert ref["state"] == "ok" and ref["status"] == "strong_inference" and "not as a definition" in ref["detail"]


def test_a_test_named_by_its_title_and_a_code_prefix(repo):
    _write(repo, "web/cart.test.js", 'describe("cart", () => {\n  it("adds an item", () => {});\n});\n')
    _write(repo, "Makefile", "all:\n")
    _spec(repo, "- [R1] THE SYSTEM SHALL add\n  - evidence: `web/cart.test.js::adds an item`, code:Makefile\n")
    test, make = specs.check(repo)["criteria"][0]["evidence"]
    assert (test["kind"], test["state"], test["at"]) == ("test", "ok", "web/cart.test.js:2")
    assert test["status"] == "strong_inference" and "quoted test title" in test["detail"]
    assert (make["kind"], make["state"]) == ("file", "ok")


def test_split_refs():
    assert specs.split_refs("a, b  `c d.py::x`;  clm_1.") == ["a", "b", "c d.py::x", "clm_1"]
