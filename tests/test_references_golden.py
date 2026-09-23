"""Golden TR/EN corpus for references.resolve: class, pin basis, status and mismatch codes (no network).

Setup: a local git fixture stands in for https://github.com/psf/requests (``GitRunner(remote_map=...)``),
the analysed project locks requests 2.31.0 and pins Python 3.12, and registry/doc/arXiv/GitHub answers are
replayed from recorded cassettes. ``python -m pytest tests/test_references_golden.py -s`` prints the scores.
"""

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import importlib.util  # noqa: E402
import json  # noqa: E402
import socket  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda.references import resolve  # noqa: E402
from verinoda.references.evaluate import score  # noqa: E402
from verinoda.references.gitref import GitRunner  # noqa: E402
from verinoda.references.transport import CassetteTransport  # noqa: E402
from verinoda.store import open_store  # noqa: E402

FX = Path(__file__).parent / "fixtures" / "references"
_spec = importlib.util.spec_from_file_location("references_helpers", FX / "helpers.py")
helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helpers)
NOW = "2026-09-23T10:00:00Z"


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("golden")
    shas = helpers.requests_repo(tmp / "requests")
    project = helpers.analysed_project(tmp / "app")
    return {"shas": shas, "project": project, "requests": tmp / "requests", "store": open_store(project)}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access in an offline test")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")  # unmapped remotes fail fast instead of going online


def run_case(world, case):
    text = case["text"].format(pr7_old=world["shas"]["pr7_old"], v230_12=world["shas"]["v2.30.0"][:12],
                               v231=world["shas"]["v2.31.0"])
    git = GitRunner(network="cache", cache_root=world["project"] / ".verinoda" / "research",
                    remote_map={"https://github.com/psf/requests": world["requests"]})
    return resolve(world["store"], world["project"], text, network="cache", now=NOW, git=git,
                   transport=CassetteTransport(FX / "cassettes"))


GOLDEN = json.loads((FX / "golden_questions.json").read_text(encoding="utf-8"))
# Written before its first run (blind): 11/12 exact then; h11 (a version range in the text) exposed a defect.
HELDOUT = json.loads((FX / "heldout_questions.json").read_text(encoding="utf-8"))
CASES = GOLDEN + HELDOUT


def test_corpus_size():
    assert len(GOLDEN) >= 20 and len(HELDOUT) >= 10
    assert {c["lang"] for c in CASES} == {"tr", "en"}
    assert sum(1 for c in CASES if c.get("clean")) >= 20


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_golden_case(world, case):
    res = run_case(world, case)
    got = sorted((r["class"], r["status"], (r.get("pin") or {}).get("basis"),
                  tuple(sorted(m["code"] for m in r["mismatches"]))) for r in res["references"])
    want = sorted((r["class"], r["status"], r["basis"], tuple(sorted(r["mismatches"]))) for r in case["refs"])
    assert got == want, json.dumps([{k: r.get(k) for k in ("class", "status", "pin", "mismatches", "unresolved",
                                                            "warnings")} for r in res["references"]], indent=1)[:3000]
    assert res["summary"]["mentions_accounted"] == res["summary"]["mentions_total"]
    assert res["summary"]["silent_floating_pins"] == 0
    assert len(res["questions_for_user"]) >= case.get("min_questions", 0)


def test_corpus_scores(world):
    results = [run_case(world, c) for c in CASES]
    s = score(CASES, results)
    print("\nreference-resolution golden corpus:", json.dumps({k: v for k, v in s.items() if k != "failures"},
                                                              indent=1))
    assert s["mentions_accounted_ratio"] == 1.0
    assert s["silent_floating_pins"] == 0
    assert s["clean_false_positive_mismatches"] == 0
    assert s["seeded_mismatch_recall"] == 1.0
    for k in ("class", "class_basis", "status", "mismatch"):
        assert s[k]["precision"] == 1.0 and s[k]["recall"] == 1.0, (k, s[k], s["failures"][:3])
