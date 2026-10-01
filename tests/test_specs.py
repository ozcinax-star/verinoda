"""``verinoda spec check`` (verinoda/specs.py): requirement criteria with the claims, tests and code pointers their
evidence lines name, each criterion with an evidence status and the exit code a CI check reads."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from verinoda import cli, specs
from verinoda import evidence as evmod
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

TESTS = """import pytest
from src.cart import Cart


def test_empty_cart_disables_checkout():
    assert Cart().items == []


@pytest.fixture
def test_cart_fixture():
    return Cart()


def make_cart():
    return Cart()


class TestTotals:
    def test_total_in_cents(self):
        assert "empty title" != ""

    def helper(self):
        pass


class Empty:
    def test_not_collected(self):
        pass
"""


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _claim(repo: Path, status: str, evidence: list | None = None) -> str:
    st = open_store(repo, create=True)
    try:
        c = Claims(st, repo).create(f"a claim that is {status}", project="p", snapshot=None,
                                    evidence=[(e, "supports") for e in evidence or []])
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


def _messages(res: dict) -> str:
    return " | ".join(p["message"] for p in res["problems"])


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
    assert res["exit"] == 1 and res["status"] == "failed"
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
    assert "limits:" in out and "parametrized" in out
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


@pytest.mark.parametrize("name,text", [
    ("verinoda.toml", "[specs\ndir = 'docs/specs'\n"),
    ("pyproject.toml", "[tool.verinoda.specs]\ndir = = 1\n"),
    ("verinoda.toml", "[specs]\ndir = 3\n"),
    ("verinoda.toml", "specs = 'docs/specs'\n"),
    (".verinoda/config.json", '{"specs": "docs/specs"}'),
    (".verinoda/config.json", '{"specs": {"dir": 3}}'),
    (".verinoda/config.json", '{"specs": '),
])
def test_a_broken_folder_setting_is_an_error_never_the_default(repo, capsys, name, text):
    _write(repo, name, text)
    _spec(repo, "- [R1] THE SYSTEM SHALL x\n  - evidence: tests/test_cart.py::test_empty_cart_disables_checkout\n")
    with pytest.raises(specs.SpecError):
        specs.check(repo)
    assert cli.main(["spec", "check", "--repo", str(repo), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_claims_without_state_are_unchecked(repo):
    _spec(repo, "- [R1] THE SYSTEM SHALL x\n  - evidence: clm_0123456789ab\n")
    res = specs.check(repo)
    assert res["criteria"][0]["status"] == "unchecked" and res["exit"] == 3
    assert "no Verinoda state" in res["claims"]["problem"]
    assert not (repo / ".verinoda" / "atlas.db").exists()   # a read-only check never creates state


def test_the_store_is_opened_read_only_and_only_for_a_claim(repo, capsys):
    db = repo / ".verinoda" / "atlas.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"")                       # an empty file: not migrated, not touched
    _spec(repo, "- [R1] THE SYSTEM SHALL x\n  - evidence: clm_0123456789ab\n")
    res = specs.check(repo)
    assert res["criteria"][0]["status"] == "unchecked" and res["exit"] == 3
    assert "cannot be read" in res["claims"]["problem"]
    assert db.read_bytes() == b"" and not (repo / ".verinoda" / ".gitignore").exists()
    assert not (repo / ".verinoda" / "atlas.db-wal").exists()
    db.write_bytes(b"not a database" * 100)   # corrupt: claims unchecked, never an error
    res = specs.check(repo)
    assert res["criteria"][0]["status"] == "unchecked" and res["exit"] == 3
    assert cli.main(["spec", "check", "--repo", str(repo), "--json"]) == 3
    capsys.readouterr()
    db.unlink()
    import sqlite3

    con = sqlite3.connect(db)
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    con.commit()
    con.close()
    res = specs.check(repo)                   # another schema: unchecked with the reason, never migrated
    assert res["exit"] == 3 and "schema v1" in res["claims"]["problem"]
    con = sqlite3.connect(db)
    assert [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")] == ["meta"]
    con.close()
    _spec(repo, "- [R1] THE SYSTEM SHALL x\n  - evidence: tests/test_cart.py::test_empty_cart_disables_checkout\n")
    res = specs.check(repo)                   # no claim cited: the corrupt store is not opened
    assert res["exit"] == 0 and res["claims"]["read"] is False and res["claims"]["problem"] is None


def test_a_claim_whose_cited_lines_changed_is_stale(repo, capsys):
    ev = evmod.source_evidence(repo, "src/cart.py", 5, 8, commit=None)
    cid = _claim(repo, "statically_verified", [ev])
    _spec(repo, f"- [R1] THE SYSTEM SHALL x\n  - evidence: {cid}\n")
    res = specs.check(repo)
    (ref,) = res["criteria"][0]["evidence"]
    assert (ref["state"], res["exit"]) == ("ok", 0) and ref["claim_evidence"][0]["at"] == "src/cart.py:5-8"
    assert res["claims"]["read"] is True
    _write(repo, "src/cart.py", CART.replace("return sum(self.items)", "return 0"))
    res = specs.check(repo)
    (ref,) = res["criteria"][0]["evidence"]
    assert ref["state"] == "stale" and "changed" in ref["detail"] and res["exit"] == 1
    assert res["criteria"][0]["status"] == "broken"
    assert cli.main(["spec", "check", "--repo", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "evidence src/cart.py:5-8" in out and "claims: read from" in out and "limits:" in out


def test_a_superseded_claim_names_the_live_one(repo):
    old, mid, live = _claim(repo, "stale"), _claim(repo, "stale"), _claim(repo, "statically_verified")
    st = open_store(repo, create=True)
    try:
        with st.tx() as cur:
            cur.execute("UPDATE claims SET superseded_by = ? WHERE id = ?", (mid, old))
            cur.execute("UPDATE claims SET supersedes = ? WHERE id = ?", (mid, live))
    finally:
        st.close()
    _spec(repo, f"- [R1] THE SYSTEM SHALL x\n  - evidence: {old}\n")
    (ref,) = specs.check(repo)["criteria"][0]["evidence"]
    assert [x["id"] for x in ref["superseded_by"]] == [mid, live] and f"cite {live}" in ref["detail"]


def test_only_real_tests_count_as_tested(repo):
    _spec(repo, "- [R1] THE SYSTEM SHALL a\n  - evidence: tests/test_cart.py::test_cart_fixture\n"
                "- [R2] THE SYSTEM SHALL b\n  - evidence: tests/test_cart.py::make_cart\n"
                "- [R3] THE SYSTEM SHALL c\n  - evidence: tests/test_cart.py::TestTotals::helper\n"
                "- [R4] THE SYSTEM SHALL d\n  - evidence: tests/test_cart.py::Empty::test_not_collected\n"
                "- [R5] THE SYSTEM SHALL e\n  - evidence: tests/test_cart.py::Empty\n"
                "- [R6] THE SYSTEM SHALL f\n  - evidence: tests/test_cart.py::TestTotals\n")
    c = _by_id(specs.check(repo))
    for k in ("R1", "R2", "R3", "R4", "R5"):
        assert c[k]["status"] == "unevidenced", k
        assert c[k]["evidence"][0]["kind"] == "symbol" and c[k]["evidence"][0]["status"] == "pointer"
    assert "fixture" in c["R1"]["evidence"][0]["detail"]
    assert c["R6"]["status"] == "tested" and c["R6"]["evidence"][0]["tests"] == ["TestTotals.test_total_in_cents"]


def test_a_quoted_title_is_a_js_test_call_only(repo):
    _write(repo, "web/cart.test.js", 'describe("cart", () => {\n  beforeEach(() => setup());\n'
                                     '  it("adds an item", () => {});\n  expect("drops an item");\n'
                                     '  it.skip("skipped", () => {});\n});\nfunction setup() {}\n')
    _spec(repo, "- [R1] THE SYSTEM SHALL a\n  - evidence: `web/cart.test.js::adds an item`\n"
                "- [R2] THE SYSTEM SHALL b\n  - evidence: `web/cart.test.js::drops an item`\n"
                "- [R3] THE SYSTEM SHALL c\n  - evidence: web/cart.test.js::setup\n"
                "- [R4] THE SYSTEM SHALL d\n  - evidence: web/cart.test.js::cart\n"
                "- [R5] THE SYSTEM SHALL e\n  - evidence: web/cart.test.js::skipped\n"
                "- [R6] THE SYSTEM SHALL f\n  - evidence: `tests/test_cart.py::empty title`\n")
    c = _by_id(specs.check(repo))
    r1 = c["R1"]["evidence"][0]
    assert c["R1"]["status"] == "tested_inferred" and r1["at"] == "web/cart.test.js:3"
    assert r1["status"] == "strong_inference" and "text pattern" in r1["detail"]
    assert c["R2"]["status"] == "broken" and c["R3"]["status"] == "broken"
    assert c["R4"]["status"] == "unevidenced" and "describe" in c["R4"]["evidence"][0]["detail"]
    assert c["R5"]["status"] == "unevidenced"
    assert c["R6"]["status"] == "broken"     # a string literal in a Python test is no test


def test_a_test_file_and_a_code_prefix(repo):
    _write(repo, "Makefile", "all:\n")
    _spec(repo, "- [R1] THE SYSTEM SHALL add\n  - evidence: code:Makefile, src/cart.py, src/cart.py:2\n")
    refs = specs.check(repo)["criteria"][0]["evidence"]
    assert [(r["kind"], r["state"], r["status"]) for r in refs] == [("file", "ok", "pointer"),
                                                                   ("file", "ok", "pointer"),
                                                                   ("lines", "ok", "pointer")]
    assert [r["line"] for r in refs] == [".verinoda/specs/cart.md:2"] * 3


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
    probs = _messages(res)
    assert "no criterion above" in probs and "empty evidence line" in probs and "no [ID]" in probs
    assert "already used" in probs and res["exit"] == 1 and res["status"] == "failed"
    assert {p["kind"] for p in res["problems"]} == {"orphan_evidence", "empty_evidence", "no_id", "duplicate_id"}
    assert all(set(p) == {"at", "kind", "message"} for p in res["problems"])


def test_markdown_that_is_not_a_criterion(repo):
    _spec(repo, "[v2]: https://example.com/v2\n"
                "[RFC2119]: https://www.rfc-editor.org/rfc/rfc2119 \"Key words\"\n"
                "- [ ] [R21] THE SYSTEM SHALL be a task-box criterion\n"
                "- [R1] THE SYSTEM SHALL wrap\n"
                "    ```\n    - [R8] THE SYSTEM SHALL be in a fence inside the item\n    ```\n"
                "\n"
                "        - [R7] THE SYSTEM SHALL be indented code\n"
                "\n"
                "<!-- - [R6] THE SYSTEM SHALL be commented out -->\n"
                "<!--\n- [R5] THE SYSTEM SHALL be in a long comment\n-->\n"
                "\n"
                "    - [R4] THE SYSTEM SHALL be indented code too\n")
    res = specs.check(repo)
    assert [c["id"] for c in res["criteria"]] == ["R21", "R1"]
    assert _by_id(res)["R1"]["text"] == "THE SYSTEM SHALL wrap" and res["problems"] == []


def test_an_evidence_line_under_another_item_is_not_the_criterion_s(repo):
    _spec(repo, "- [R1] THE SYSTEM SHALL a\n"
                "- Notes on the cart\n"
                "  - evidence: src/cart.py\n")
    res = specs.check(repo)
    assert res["criteria"][0]["evidence"] == [] and res["problems"][0]["kind"] == "orphan_evidence"


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


def test_wrapped_criteria_placeholders_and_long_fences(repo):
    _spec(repo, "- [R1] WHEN the cart is empty\n"
                "  THE SYSTEM SHALL refuse checkout\n"
                "  - evidence: TBD, none\n"
                "````md\n```\n- [R9] THE SYSTEM SHALL stay in the fence\n````\n"
                "- [R2] THE SYSTEM SHALL x still one line\n  - evidence: src/cart.py:3\n")
    res = specs.check(repo)
    r1, r2 = res["criteria"]
    assert r1["text"] == "WHEN the cart is empty THE SYSTEM SHALL refuse checkout" and r1["ears"] == "event"
    assert r1["evidence"] == [] and r1["status"] == "unevidenced" and res["status"] == "unevidenced"
    assert r2["at"] == ".verinoda/specs/cart.md:8" and r2["evidence"][0]["state"] == "ok"
    assert res["problems"] == []


def test_a_requirement_with_no_id_fails_the_check(repo):
    _spec(repo, "# Cart\n\nThe system SHALL refuse an empty checkout.\n")
    res = specs.check(repo)
    assert (res["criteria"], res["exit"]) == ([], 1) and "no [ID]" in res["problems"][0]["message"]


def test_an_unread_file_is_never_a_pass(repo):
    _spec(repo, "- [R1] THE SYSTEM SHALL x\n  - evidence: tests/test_cart.py::test_empty_cart_disables_checkout\n")
    _spec(repo, "x" * (specs.MAX_FILE_BYTES + 1), ".verinoda/specs/huge.md")
    res = specs.check(repo)
    assert res["exit"] == 3 and "not read" in res["problems"][0]["message"]


def test_dot_and_vendored_folders_are_not_read(repo):
    for rel in ("node_modules/pkg/README.md", ".venv/lib/x.md", ".git/x.md", "docs/.drafts/x.md"):
        _spec(repo, "- [V1] THE SYSTEM SHALL be vendored\n", rel)
    _spec(repo, "- [R1] THE SYSTEM SHALL x\n", "docs/a.md")
    res = specs.check(repo, ".")
    assert [c["id"] for c in res["criteria"]] == ["R1"]


def _link(target: Path, link: Path) -> None:
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def test_links_and_junctions_are_not_followed(repo, tmp_path):
    outside = tmp_path / "outside"
    _write(outside, "leak.md", "- [OUT1] THE SYSTEM SHALL not be read\n")
    _spec(repo, "- [R1] THE SYSTEM SHALL x\n", ".verinoda/specs/a.md")
    folder = repo / ".verinoda" / "specs"
    try:
        _link(outside, folder / "out")
        _link(folder, folder / "loop")
    except OSError as exc:
        pytest.skip(f"cannot create a link here: {exc}")
    res = specs.check(repo)
    assert [c["id"] for c in res["criteria"]] == ["R1"]
    assert sorted(res["skipped_links"]) == [".verinoda/specs/loop", ".verinoda/specs/out"]


def test_long_lines_do_not_backtrack(repo):
    lines = ["[a" + "1" * 20000, " " * 20000 + "x", "- [R1]" + " " * 20000 + "x" + " " * 20000 + "!",
             "evidence:" + " " * 20000 + "x" + " " * 20000, "- " + "\t " * 10000 + "[R2 " + "1" * 20000,
             "a:" + "1:" * 10000 + "x"]
    _spec(repo, "- [R0] THE SYSTEM SHALL x\n  - evidence: " + lines[-1] + "\n" + "\n".join(lines[:-1]) + "\n")
    t = time.perf_counter()
    res = specs.check(repo)
    assert time.perf_counter() - t < 2.0
    assert [c["id"] for c in res["criteria"]] == ["R0", "R1"]


def test_split_refs():
    assert specs.split_refs("a, b  `c d.py::x`;  clm_1.") == ["a", "b", "c d.py::x", "clm_1"]
