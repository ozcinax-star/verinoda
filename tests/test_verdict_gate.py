"""Honest verdicts (docs/DESIGN.md D-honest-verdicts): the verdict gate only lowers or refuses.

The shape rules are tested on their own; the rules that need an index run `verinoda analyze` on the
fixtures under tests/fixtures/verdict_audit/ (copied, committed and scanned once per module), and the
verdict audit runs on the fixture cases of its dev split.
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import itertools  # noqa: E402
import shutil  # noqa: E402

import pytest  # noqa: E402

from verinoda import analysis, verdict_gate as vg  # noqa: E402
from verinoda.benchmark import verdict_audit as va  # noqa: E402
from verinoda.store import open_store  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


# -- the question's shape ------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Which React components use the useAuth hook?", "what calls BaseLoop.call_soon?", "who uses OrderRepository?",
    "where is place_order called?", "useAuth hook'unu hangi bileşenler kullanıyor?",
    "Wisp.spawn kimler tarafından çağrılıyor?"])
def test_usage_questions(text):
    assert vg.usage_question(text)


@pytest.mark.parametrize("text", ["where is Wisp defined?", "What does apply_discount do?",
                                  "Where is the SQLite connection opened?", "useAuth nerede tanımlı?"])
def test_not_usage_questions(text):
    assert not vg.usage_question(text)


def test_lifecycle_questions_name_the_subject_and_the_callback():
    assert vg.action_question("where is the Wisp ticked?") == (["wisp"], "tick")
    assert vg.action_question("where is the EmberForgeBlockEntity ticked?") == (["ember", "forge", "block",
                                                                                 "entity"], "tick")
    assert vg.action_question("Where does the RepairScheduler tick?") == (["repair", "scheduler"], "tick")
    assert vg.action_question("Where does the HUD render?") == (["hud"], "render")
    assert vg.action_question("Wisp nerede tick'leniyor?") == (["wisp"], "tick")
    # where it is (a definition answers), who uses it (the usage rule), other verbs (code that does them is
    # seldom named after the thing), a subject that points back
    for text in ("where is Wisp defined?", "Where is max_heat declared?", "where is place_order called?",
                 "where is the LoginButton rendered?", "Where is an order written to the database?",
                 "Where are the mod's items registered?", "Where is the SQLite connection opened?",
                 "where are those registers attached to the mod event bus?", "where is it ticked?",
                 "where do its default values come from?", "where is it?"):
        assert vg.action_question(text) is None, text


@pytest.mark.parametrize("text,word", [
    ("Which mixin classes are not listed in mixmod.mixins.json?", "are not listed"),
    ("Which functions in orders/service.py are not called by the HTTP handlers?", "are not called"),
    ("which tests are missing for the parser?", "missing"),
    ("What functions are never called?", "are never called"),
    ("which entities have no loot table?", "have no"),
    ("Which files aren’t in the index?", "aren't in"),
    ("Which unused imports are there?", "unused"),
    ("mixmod.mixins.json dosyasında listelenmemiş mixin sınıfları hangileri?", "listelenmemis"),
    ("Hangi fonksiyonlar hiç çağrılmayan fonksiyonlar?", "cagrilmayan"),
    ("hangi mixinler eksik?", "eksik"),
])
def test_set_difference_questions(text, word):
    assert word in (vg.asks_set_difference(text) or "")


@pytest.mark.parametrize("text", [
    "What happens when an order is submitted with no items?", "what happens if the file is not found?",
    "Why is the cache not invalidated?", "Is OrderRepository used anywhere else?", "where is Wisp defined?",
    "What does the app do without a network?", "What does the missing-key handler do?",
    "Which error is raised when a key is missing?", "hangi testler geçmiyor?", "neden kullanılmayan bir alan var?",
    "Bu fonksiyon neden async değil?", "Who calls place_order?"])
def test_not_set_difference_questions(text):
    assert vg.asks_set_difference(text) is None


# -- small rules -----------------------------------------------------------------------------------------------

def test_word_matching_uses_stems_and_identifier_parts():
    assert vg.word_in("tick", vg.ident_parts("serverTick"))
    assert vg.word_in("registered", vg.ident_parts("register"))
    assert vg.word_in("applied", vg.ident_parts("apply_discount"))
    assert vg.word_in("items", vg.ident_parts("ModItems"))
    assert not vg.word_in("wisp", vg.ident_parts("onEndTick", "GlowModClient"))
    assert not vg.word_in("an", vg.ident_parts("and"))


def test_copy_roots_and_vendored_folders():
    roots = ("reference/", "benchmarks/corpora/old_7371990/")
    assert vg.in_copy("reference/oldloop/events.py", roots)
    assert vg.in_copy("benchmarks/corpora/old_7371990/pkg/paths.py", roots)
    assert vg.in_copy("web/node_modules/react/index.js", ())
    assert vg.in_copy("third_party/zlib/zlib.h", ())
    assert not vg.in_copy("loop/base.py", roots)
    assert not vg.in_copy("references.py", roots)
    assert vg.copy_root_of("web/node_modules/react/index.js", ()) == "web/node_modules/"
    assert vg.copy_root_of("reference/a.py", roots) == "reference/"


def test_cited_files_come_from_evidence_and_the_call_site():
    claim = {"spec": {"at": "reference/x.py:4"}, "subjects": ["a.py::f", "b.py::g"]}
    ev = [{"source_type": "source_code", "locator": "reference/x.py:4"},
          {"source_type": "graph_edge", "locator": "graph.json edge"}]
    assert vg.cited_files(claim, ev) == ["reference/x.py"]
    assert vg.cited_files({"subjects": ["loop/base.py::BaseLoop"]}, []) == ["loop/base.py"]


def test_receivers_that_cannot_be_the_target():
    owners = {"GlowConfig"}
    # a static member: only its class, or a bare call in its own file (or with a static import)
    assert vg._receiver_rules_out("Map root = new Yaml().", "static", owners, True, True, False)
    assert vg._receiver_rules_out("x = reader.", "static", owners, False, False, False)
    assert not vg._receiver_rules_out("GlowConfig.", "static", owners, False, False, False)
    assert not vg._receiver_rules_out("    ", "static", owners, True, True, False)
    assert vg._receiver_rules_out("    ", "static", owners, False, False, False)
    assert not vg._receiver_rules_out("    ", "static", owners, False, False, True)
    # another class's static call is never the target
    assert vg._receiver_rules_out("RepairScheduler.", "method", {"Wisp"}, False, False, False)
    # a method: any object may be it
    assert not vg._receiver_rules_out("self._loop.", "method", {"BaseLoop"}, False, False, False)
    # a module function: never through self
    assert vg._receiver_rules_out("self.", "function", set(), True, True, False)
    assert not vg._receiver_rules_out("sched.", "function", set(), False, False, False)


def test_declarations_strings_and_comments_are_not_call_sites():
    assert vg._is_declaration("    def call_soon(self, cb):", 8, "call_soon")
    assert vg._is_declaration("  function openPalette(text) {", 11, "openPalette")
    assert vg._is_declaration("    public static void load() {", 23, "load")
    assert vg._is_declaration("  openPalette(text) {", 2, "openPalette")
    assert not vg._is_declaration("    private static final X A = load();", 31, "load")
    assert not vg._is_declaration("        self.call_soon(cb)", 13, "call_soon")
    assert vg._in_string_or_comment('log("call_soon(x)")', 5)
    assert vg._in_string_or_comment("x = 1  # call_soon(x)", 9)
    assert not vg._in_string_or_comment("self.call_soon(x)", 5)


# -- judge only lowers ---------------------------------------------------------------------------------------

def _claim(cid, kind, status="statically_verified"):
    return {"id": cid, "kind": kind, "status": status}


def test_weak_claims_never_make_a_subquestion_met():
    sq = {"id": "q1", "intent": "locate"}
    rows = [_claim("c1", "location"), _claim("c2", "location")]
    assert analysis.judge(sq, rows, {}) == "met"
    assert analysis.judge(sq, rows, {"weak": ["c1"]}) == "met"
    assert analysis.judge(sq, rows, {"weak": ["c1", "c2"]}) == "met_with_inference"
    assert analysis.judge(sq, rows, {"capped": ["callers: 3 call sites unresolved"]}) == "met_with_inference"


def test_the_gate_never_raises_a_verdict():
    level = {"met": 3, "met_with_inference": 2}
    sqs = [{"id": "q1", "intent": i} for i in ("locate", "callers", "why", "config", "flow", "architecture")]
    kinds = ("location", "relation", "decision", "history", "config", "flow")
    statuses = ("statically_verified", "strong_inference", "weak_inference", "stale")
    base_flags = [{}, {"not_supported": "x"}, {"off_subject": ["c1"]}, {"not_found": ["m1"]},
                  {"exclusive": {"unchecked": "a set difference"}}, {"may_ask_for_choice": "best"}]
    gates = [{"weak": ["c1"]}, {"weak": ["c1", "c2"]}, {"capped": ["x"]}, {"weak": ["c2"], "capped": ["x"]}]
    for sq, (k1, k2), (s1, s2), flags, gate in itertools.product(
            sqs, itertools.product(kinds, repeat=2), itertools.product(statuses, repeat=2), base_flags, gates):
        rows = [_claim("c1", k1, s1), _claim("c2", k2, s2)]
        before = analysis.judge(sq, rows, flags)
        after = analysis.judge(sq, rows, {**flags, **gate})
        assert level.get(after, 1) <= level.get(before, 1), (sq, rows, flags, gate, before, after)
        if level.get(before, 1) == 1:
            assert after == before  # unknown stays unknown: a refusal or unmet is kept as it was


# -- on the fixtures ---------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    data = va.load_cases()
    work = tmp_path_factory.mktemp("verdict_gate")
    out = {}
    for name in ("pyloop", "webui", "mixmod"):
        base, why = va.base_copy(name, data["projects"][name], work)
        assert base is not None, why
        out[name] = (base, open_store(base))
    yield out
    for _repo, st in out.values():
        st.close()


def _sub(res):
    (s,) = res["subquestions"]
    return s


def _unknowns(s):
    return " | ".join(u["why"] for u in s.get("unknowns") or [])


def test_callers_through_self_and_an_attribute_are_named_and_cap_the_verdict(fixtures):
    repo, st = fixtures["pyloop"]
    s = _sub(analysis.analyze(st, repo, "what calls BaseLoop.call_soon?"))
    assert s["status"] == "met_with_inference"
    assert "3 call sites unresolved: loop/transport.py:9, loop/transport.py:12, loop/transport.py:19" in _unknowns(s)
    assert "reference/" not in _unknowns(s)  # the reference tree's own call_soon is not the project's


def test_clean_callers_stay_met(fixtures):
    repo, st = fixtures["pyloop"]
    s = _sub(analysis.analyze(st, repo, "what calls schedule_task?"))
    assert s["status"] == "met" and "unresolved" not in _unknowns(s)


def test_a_listener_caller_is_named(fixtures):
    repo, st = fixtures["webui"]
    s = _sub(analysis.analyze(st, repo, "What calls openPalette?"))
    assert s["status"] == "met_with_inference"
    assert "1 call site unresolved: static/app.js:35" in _unknowns(s)
    s = _sub(analysis.analyze(st, repo, "What calls renderList?"))
    assert s["status"] == "met"


def test_definitions_do_not_answer_which_code_uses_a_hook(fixtures):
    repo, st = fixtures["webui"]
    res = analysis.analyze(st, repo, "Which React components use the useAuth hook?")
    verdict = va.overall([s["status"] for s in res["subquestions"]])
    answer = va.answer_text(res)
    # met only when the answer names the components that call the hook
    assert verdict != "met" or ("LoginButton" in answer and "ProfileMenu" in answer), (verdict, answer)


def test_a_set_difference_is_refused(fixtures):
    repo, st = fixtures["mixmod"]
    res = analysis.analyze(st, repo, "Which mixin classes are not listed in mixmod.mixins.json?")
    s = _sub(res)
    assert s["status"] == "not_supported"
    assert "set difference" in _unknowns(s)


def test_where_is_x_ticked_needs_a_tick_of_x(fixtures):
    repo, st = fixtures["mixmod"]
    s = _sub(analysis.analyze(st, repo, "where is the EmberLamp ticked?"))
    assert s["status"] == "met"


def test_a_commit_line_is_not_a_reason(fixtures):
    repo, st = fixtures["pyloop"]
    s = _sub(analysis.analyze(st, repo, "why does Transport pause reading?"))
    assert s["status"] == "met_with_inference"
    assert "the reason was not found" in _unknowns(s)


def test_an_answer_only_from_a_reference_tree_is_not_met(fixtures):
    repo, st = fixtures["pyloop"]
    s = _sub(analysis.analyze(st, repo, "which environment variable sets the timer resolution?"))
    assert s["status"] == "met_with_inference"
    assert "reference/" in _unknowns(s)
    # naming the tree makes it what was asked
    s = _sub(analysis.analyze(st, repo, "which environment variable sets the timer resolution in the old loop?"))
    assert "cite only reference/" not in _unknowns(s)


def test_plan_audit_judges_the_same_as_the_analysis(fixtures):
    repo, st = fixtures["pyloop"]
    res = analysis.analyze(st, repo, "what calls BaseLoop.call_soon?")
    audit = analysis.audit(st, repo, res["analysis_id"], refresh=False)
    assert [s["recomputed"] for s in audit["subquestions"]] == [s["status"] for s in res["subquestions"]]


def test_the_audit_on_the_fixture_cases(tmp_path):
    """Both splits of the fixture cases (the held-out ones were run first with the rules frozen; now they
    guard against regressions)."""
    data = va.load_cases()
    ids = [c["id"] for c in data["cases"] if c["project"] in ("pyloop", "webui", "mixmod")]
    res = va.evaluate("all", work=tmp_path, only=ids)
    got = res["summary"]["all"]
    assert got["cases"] == len(ids) and got["skipped"] == 0
    assert got["wrong_met"] == 0, got["wrong_met_ids"]
    assert got["above_ceiling"] == 0
    # the ADR's quoted line negates ("never inside the call"): its claim is graded below verified (an entail
    # rule, not this gate), and the commit line that made it met before is not a reason
    assert set(got["controls_lost_ids"]) <= {"pyloop-why-adr"}


def test_audit_scoring():
    case = {"ceiling": "met_with_inference", "gold": {"must": ["a.py:3"], "must_not": ["b.py"]}}
    assert va.score_case(case, "met", "x a.py:3")["right_met"]
    assert va.score_case(case, "met", "x a.py:4")["wrong_met"]
    assert va.score_case(case, "met", "a.py:3 b.py:1")["wrong_met"]
    assert not va.score_case(case, "met_with_inference", "")["wrong_met"]
    assert va.score_case(case, "met", "a.py:3")["above_ceiling"]
    assert va.score_case({"ceiling": "met", "gold": {"met_is_wrong": True}}, "met", "")["wrong_met"]
    assert va.overall(["met", "met"]) == "met"
    assert va.overall(["met", "unmet"]) == "met_with_inference"
    assert va.overall(["not_supported", "unmet"]) == "not_supported"
