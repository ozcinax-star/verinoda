"""Golden URL/reference-shape table for repoatlas.references.classify (no network, no git).

The table lives in tests/fixtures/references/golden_urls.json: each row names an input and the
fields classification must produce (class, the ref the URL names and its namespace hint, path,
#L lines, compare range, PR/issue number, package version, docs version slot, ...).
"""

import os

os.environ.setdefault("GRAPHIFY_OUT", ".repoatlas/index")

import json  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from repoatlas import research as rs  # noqa: E402
from repoatlas.references.classify import (  # noqa: E402
    REF_REQUIRED,
    classify,
    to_legacy,
)

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "references" / "golden_urls.json").read_text(encoding="utf-8"))


def test_golden_table_is_large_enough():
    assert len(GOLDEN) >= 40
    assert len({g["class"] for g in GOLDEN}) >= 14


@pytest.mark.parametrize("row", GOLDEN, ids=[g["input"][:60] for g in GOLDEN])
def test_golden_url_shape(row):
    spec = classify(row["input"])
    req, ident = spec["requested"], spec["identity"]
    assert spec["class"] == row["class"], spec
    checks = {
        "url_ref": req.get("url_ref"), "hint": req.get("ref_namespace_hint"), "kind": req.get("url_ref_kind"),
        "path": req.get("path"), "lines": req.get("lines"), "number": req.get("number"),
        "canonical": ident.get("canonical_url"), "immutable": spec.get("immutable"),
        "floating": spec.get("floating"), "requires_ref": spec.get("requires_ref"),
        "docs_version": req.get("docs_version"), "floating_slot": req.get("floating_slot"),
        "plain": req.get("plain"), "host_assumed": ident.get("host_assumed"),
        "source_type": spec.get("source_type") or to_legacy(spec).get("source_type"),
        "commit": req.get("commit"), "spec": req.get("version_spec"),
    }
    for key, got in checks.items():
        if key in row:
            assert got == row[key], (key, got, row)
    if "version" in row:
        assert req.get("version_text") == row["version"], spec
    if "ecosystem" in row:
        assert ident["package"]["ecosystem"] == row["ecosystem"]
    if "compare" in row:
        c = req["compare"]
        assert [c["base"], c["head"], c["three_dot"]] == row["compare"]
        if "head_owner" in row:
            assert c["head_owner"] == row["head_owner"]
    if "candidates" in row:
        assert [c[0] for c in req["ref_candidates"]][: len(row["candidates"])] == row["candidates"]
    if "legacy_type" in row:
        assert to_legacy(spec)["type"] == row["legacy_type"]


def test_classes_that_name_a_version_require_one():
    assert REF_REQUIRED == {"git_compare", "git_archive", "pull_request", "merge_request", "release"}
    no_ref = classify("https://github.com/psf/requests/compare")
    assert no_ref["requires_ref"] and not no_ref["requested"]["ref_candidates"]
    legacy = rs.parse_reference("https://github.com/psf/requests/compare")
    assert legacy["requires_ref"] and legacy["ref"] is None and legacy["type"] == "git"


def test_parse_reference_is_a_compatibility_wrapper():
    got = rs.parse_reference("https://github.com/psf/requests/blob/main/src/requests/sessions.py#L100-L120")
    assert got["type"] == "git" and got["ref"] == "main" and got["subpath"] == "src/requests/sessions.py"
    assert got["class"] == "git_file" and got["lines"] == [100, 120] and got["ref_namespace_hint"] == "heads_first"
    pr = rs.parse_reference("https://github.com/psf/requests/pull/6963")
    assert pr["type"] == "git" and pr["ref_candidates"] == [("refs/pull/6963/head", None)]
    assert rs.parse_reference("requests==2.31.0")["type"] == "package"
    doc = rs.parse_reference("arXiv:1706.03762v5")
    assert doc["type"] == "document" and doc["url"] == "https://arxiv.org/abs/1706.03762v5"
    issue = rs.parse_reference("psf/requests#6963")
    assert issue["type"] == "document" and issue["url"] == "https://github.com/psf/requests/issues/6963"
    with pytest.raises(ValueError):
        rs.parse_reference("ftp://example.com/x")


def test_mentions_classify_like_strings():
    from repoatlas.references.mentions import extract

    ms = {m["kind"]: m for m in extract("see psf/requests#12, PR #7, !3 and `app.py` plus Python 3.11")}
    assert classify(ms["xref_issue"])["class"] == "issue"
    assert classify(ms["xref_pr"])["requested"]["url_ref"] == "refs/pull/7/head"
    assert classify(ms["xref_mr"])["class"] == "merge_request"
    assert classify(ms["file"])["class"] == "local_file"
    assert classify(ms["name_candidate"])["class"] == "runtime"
    with pytest.raises(ValueError):
        classify(ms["version"])
