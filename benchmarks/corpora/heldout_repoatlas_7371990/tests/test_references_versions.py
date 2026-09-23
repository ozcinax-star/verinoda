"""Version text <-> tag matching and the precedence ladder (pure functions, no git, no network)."""

import pytest

from repoatlas.references import pin as pinmod
from repoatlas.references import versions as vm

TAGS = ["v2.30.0", "v2.31.0", "v2.31.1", "v2.32.0", "v2.33.0rc1", "requests-2.29.0", "release-1.0", "sub/v1.2.0"]


@pytest.mark.parametrize("text, how, chosen", [
    ("2.31", "exact", "v2.31.0"),          # PEP 440: 2.31 == 2.31.0
    ("v2.31.0", "exact", "v2.31.0"),
    ("2.29.0", "exact", "requests-2.29.0"),
    ("2.32", "exact", "v2.32.0"),
    ("2.3", "none", None),                  # 2.3 is not a prefix of 2.31 (series compare by component)
    ("1.2.0", "exact", "sub/v1.2.0"),
    ("9.9", "none", None),
])
def test_match_tags(text, how, chosen):
    m = vm.match_tags(text, TAGS, name="requests")
    assert (m["how"], m["chosen"]) == (how, chosen)


def test_series_prefix_picks_the_highest_release_and_reports_all():
    m = vm.match_tags("2.31", ["v2.31.1", "v2.31.2", "v2.31.3rc1"])
    assert m["how"] == "prefix" and m["chosen"] == "v2.31.2" and m["candidates"] == ["v2.31.1", "v2.31.2", "v2.31.3rc1"]


def test_newest_skips_prereleases_and_satisfies_ranges():
    assert vm.newest(TAGS, name="requests") == "v2.32.0"
    assert vm.newest(["v1.0rc1"]) == "v1.0rc1"
    assert vm.satisfies(">=2.31,<3", "2.31.0") is True and vm.satisfies(">=2.32", "2.31.0") is False
    assert vm.satisfies("^18.2.0", "18.3.1", ecosystem="npm") and not vm.satisfies("^18.2.0", "19.0.0", ecosystem="npm")
    assert vm.satisfies("~1.2", "1.2.9", ecosystem="cargo") and not vm.satisfies("~1.2", "1.3.0", ecosystem="cargo")
    assert vm.satisfies("^0.2.3", "0.2.9", ecosystem="npm") and not vm.satisfies("^0.2.3", "0.3.0", ecosystem="npm")


def test_ladder_order_and_the_branch_demotion():
    def c(basis, value):
        return pinmod.candidate(basis, pin=pinmod.make_pin("tag", value, value, basis, immutable=True))

    cands = [c("default", "d"), c("url_branch", "b"), c("local_project_version", "l"), c("text_date", "t")]
    win, alts, block = pinmod.choose(cands, demote_branch=False)
    assert win["basis"] == "url_branch" and block is None
    win, _, _ = pinmod.choose(cands, demote_branch=True)
    assert win["basis"] == "local_project_version"
    full = cands + [c("url_tag", "u"), c("explicit_floating_intent", "f"), c("explicit_text_version", "x"),
                    c("immutable_in_reference", "i")]
    assert [x["basis"] for x in pinmod.order(full, demote_branch=True)] == [
        "immutable_in_reference", "explicit_text_version", "explicit_floating_intent", "url_tag",
        "local_project_version", "url_branch", "text_date", "default"]


def test_a_named_version_that_does_not_exist_blocks_the_lower_rungs():
    ok = pinmod.candidate("url_branch", pin=pinmod.make_pin("branch", "b", "b", "url_branch", immutable=False))
    missing = pinmod.candidate("explicit_text_version", error="no tag matches 2.29")
    win, alts, block = pinmod.choose([ok, missing], demote_branch=False)
    assert win is None and block is missing and [a["basis"] for a in alts] == ["url_branch"]
    soft = pinmod.candidate("local_project_version", error="no tag for the local version", blocking=False)
    win, _, block = pinmod.choose([ok, soft], demote_branch=True)
    assert win is ok and block is None


def test_mismatch_catalogue_is_complete():
    assert set(pinmod.MISMATCHES) == {"M1", "M1b", "M1c", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9", "M10",
                                      "M11", "M12"}
    m = pinmod.mismatch("M1b", "x", "y")
    assert m["severity"] == "error" and m["name"] == "path_missing_at_pin"
    assert pinmod.worst_severity([pinmod.mismatch("M7", "a", "b"), m]) == "error"
