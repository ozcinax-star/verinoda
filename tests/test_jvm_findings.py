"""JVM checkers' findings read as evidence (verinoda.jvm_findings, `verinoda import-findings`): Error Prone and
NullAway diagnostics from javac, Gradle, Maven and Ant logs, jdeps -jdkinternals output, and their SARIF."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import cli  # noqa: E402
from verinoda import jvm_findings as jf  # noqa: E402

FOO = """package com.ex;

public class Foo {
    String name;

    int size() {
        return name.length();
    }

    void check(int x) {
        if (x < 0) {
            new IllegalArgumentException("negative");
        }
    }
}
"""

LEGACY = """package com.ex;

import sun.misc.BASE64Encoder;

public class Legacy {
    String encode(byte[] b) {
        Runnable r = new Runnable() { public void run() {} };
        return new BASE64Encoder().encode(b);
    }
}
"""

UTIL_KT = """package com.ex

// sun.misc.Unsafe in a comment is not the use
import sun.misc.*

fun peek(): Long = Unsafe.ARRAY_BYTE_BASE_OFFSET.toLong()
"""

FOO_REL = "src/main/java/com/ex/Foo.java"
LEGACY_REL = "src/main/java/com/ex/Legacy.java"
UTIL_REL = "app/src/main/kotlin/com/ex/Util.kt"


def _repo(tmp_path: Path) -> Path:
    r = tmp_path / "proj"
    for rel, text in ((FOO_REL, FOO), (LEGACY_REL, LEGACY), (UTIL_REL, UTIL_KT)):
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_text(text, encoding="utf-8", newline="\n")
    old = time.time() - 1000
    for rel in (FOO_REL, LEGACY_REL, UTIL_REL):
        os.utime(r / rel, (old, old))
    return r


def _log(r: Path, name: str, text: str, encoding: str = "utf-8") -> Path:
    p = r / name
    p.write_bytes(text.encode(encoding))
    return p


def _javac(prefix: str) -> str:
    """javac's output with Error Prone and NullAway (their documented format): the diagnostic, the quoted source
    line, the caret, the link and Error Prone's fix."""
    return "\n".join([
        f"{prefix}:7: warning: [NullAway] dereferenced expression name is @Nullable",
        "        return name.length();",
        " " * 19 + "^",
        "    (see http://t.uber.com/nullaway )",
        f"{prefix}:12: error: [DeadException] Exception created but not thrown",
        '            new IllegalArgumentException("negative");',
        "            ^",
        "    (see https://errorprone.info/bugpattern/DeadException)",
        "  Did you mean 'throw new IllegalArgumentException(\"negative\");'?",
        f"{prefix}:4: warning: [rawtypes] found raw type: List",
        "    String name;",
        "    ^",
        f"{prefix}:20: error: cannot find symbol",
        "1 error",
        "2 warnings",
        ""])


def test_javac_output_gives_error_prone_and_nullaway_claims_with_the_tool_named(tmp_path):
    r = _repo(tmp_path)
    _log(r, "build.log", _javac(str(r / FOO_REL)))   # an absolute Windows path on Windows
    res = jf.report(r, ["build.log"])
    by = {c["subject"]: c for c in res["claims"]}
    assert set(by) == {"Error Prone/DeadException", "NullAway/NullAway"}
    ep = by["Error Prone/DeadException"]
    assert ep["at"] == f"{FOO_REL}:12" and ep["level"] == "error" and ep["status"] == "strong_inference"
    assert ep["stated_by"] == "Error Prone" and ep["rule"] == "DeadException" and ep["column"] == 13
    assert ep["suggestion"].startswith("Did you mean 'throw new IllegalArgumentException")
    assert ep["see"] == "https://errorprone.info/bugpattern/DeadException"
    assert ep["evidence_at"] == [f"{FOO_REL}:12", "build.log:5"] and "not checked" in ep["derived_by"]
    assert "check" in ep["symbol"]
    na = by["NullAway/NullAway"]
    assert na["at"] == f"{FOO_REL}:7" and na["level"] == "warning" and na["column"] == 20
    assert "NullAway reports NullAway (warning): dereferenced expression name is @Nullable" in na["claim"]
    assert [c["level"] for c in res["claims"]] == ["error", "warning"]   # errors first
    s = res["summary"]
    assert s["javac_other"] == 2 and s["claims"] == 2 and s["by_tool"] == {"Error Prone": 1, "NullAway": 1}
    assert res["inputs"] == [{"file": "build.log", "format": "log", "found": {"NullAway": 1, "Error Prone": 1}}]


def test_a_gradle_ci_log_ties_runner_paths_by_suffix_confirmed_by_the_quoted_line(tmp_path):
    r = _repo(tmp_path)
    runner = "/home/runner/work/proj/proj/" + FOO_REL
    lines = ["Download https://repo.maven.apache.org/maven2/com/google/errorprone/error_prone_core/2.36.0/"
             "error_prone_core-2.36.0.jar", "Download .../com/uber/nullaway/nullaway/0.12.3/nullaway-0.12.3.jar",
             "> Task :compileJava"] + _javac(runner).splitlines()
    stamped = "\n".join(f"2026-09-30T10:00:{i:02d}.1234567Z \x1b[33m{ln}\x1b[0m" if ln else ln
                        for i, ln in enumerate(lines))
    _log(r, "ci.log", stamped + "\nBUILD FAILED in 12s\n")
    res = jf.report(r, ["ci.log"])
    ep = next(c for c in res["claims"] if c["rule"] == "DeadException")
    assert ep["at"] == f"{FOO_REL}:12" and ep["status"] == "strong_inference"
    assert "tied to" in ep["claim"] and "still at 12" in ep["claim"]
    assert ep["tool_version"] == "2.36.0" and ep["claim"].startswith("Error Prone 2.36.0 reports DeadException")
    assert ep["evidence_at"][2] == "ci.log:1"
    na = next(c for c in res["claims"] if c["rule"] == "NullAway")
    assert na["tool_version"] == "0.12.3"
    assert res["inputs"][0]["versions"] == {"Error Prone": "2.36.0", "NullAway": "0.12.3"}


def test_a_maven_log_with_windows_paths_reads_level_and_column_and_drops_repeats(tmp_path):
    r = _repo(tmp_path)
    win = str(r / FOO_REL).replace("\\", "/")
    maven = "/" + win if win[1:2] == ":" else win
    log = "\n".join([
        "[INFO] --- maven-compiler-plugin:3.13.0:compile (default-compile) @ proj ---",
        f"[WARNING] {maven}:[7,20] [NullAway] dereferenced expression name is @Nullable",
        "    (see http://t.uber.com/nullaway )",
        "[INFO] -------------------------------------------------------------",
        "[ERROR] COMPILATION ERROR :",
        f"[ERROR] {maven}:[12,13] [DeadException] Exception created but not thrown",
        "    (see https://errorprone.info/bugpattern/DeadException)",
        "  Did you mean 'throw new IllegalArgumentException(\"negative\");'?",
        "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.13.0:compile: Compilation "
        "failure",
        f"[ERROR] {maven}:[12,13] [DeadException] Exception created but not thrown",
        "    (see https://errorprone.info/bugpattern/DeadException)",
        ""])
    _log(r, "mvn.log", log)
    res = jf.report(r, ["mvn.log"])
    assert [(c["rule"], c["level"], c["column"], c["at"]) for c in res["claims"]] == [
        ("DeadException", "error", 13, f"{FOO_REL}:12"), ("NullAway", "warning", 20, f"{FOO_REL}:7")]
    assert res["claims"][0]["suggestion"].startswith("Did you mean")
    assert res["summary"]["duplicates"] == 1
    # a backslash path as javac prints it on Windows
    _log(r, "win.log", f"{r / FOO_REL}:12: error: [DeadException] Exception created but not thrown\n".replace(
        "/", "\\"))
    assert jf.report(r, ["win.log"])["claims"][0]["at"] == f"{FOO_REL}:12"


def test_a_file_changed_after_the_log_follows_the_quoted_line_or_says_it_is_gone(tmp_path):
    r = _repo(tmp_path)
    _log(r, "build.log", _javac(str(r / FOO_REL)))
    old = time.time() - 100
    os.utime(r / "build.log", (old, old))
    # two lines added above: the dereference moved from 7 to 9, the dead exception was fixed
    changed = FOO.replace("public class Foo {", "// a\n// b\npublic class Foo {").replace(
        '            new IllegalArgumentException("negative");', '            throw new IllegalArgumentException("x");')
    (r / FOO_REL).write_text(changed, encoding="utf-8", newline="\n")
    by = {c["rule"]: c for c in jf.report(r, ["build.log"])["claims"]}
    assert by["NullAway"]["at"] == f"{FOO_REL}:9" and by["NullAway"]["status"] == "weak_inference"
    assert "quoted at 7 is at 9 now" in by["NullAway"]["claim"]
    assert by["DeadException"]["at"] == f"{FOO_REL}:12" and by["DeadException"]["status"] == "weak_inference"
    assert "not in the file now" in by["DeadException"]["claim"]
    # no quoted line (Maven): the file's age decides
    _log(r, "m.log", f"[WARNING] {r / FOO_REL}:[7,20] [NullAway] dereferenced expression name is @Nullable\n")
    os.utime(r / "m.log", (old, old))
    c = jf.report(r, ["m.log"])["claims"][0]
    assert c["status"] == "weak_inference" and "may have moved" in c["claim"]


JDEPS_11 = """app.jar -> JDK removed internal API
   com.ex.Legacy                                      -> sun.misc.BASE64Encoder                             JDK internal API (JDK removed internal API)
   com.ex.Legacy$1                                    -> sun.misc.BASE64Encoder                             JDK internal API (JDK removed internal API)
app.jar -> jdk.unsupported
   com.ex.UtilKt                                      -> sun.misc.Unsafe                                    JDK internal API (jdk.unsupported)
   com.lib.Shaded                                     -> sun.misc.Unsafe                                    JDK internal API (jdk.unsupported)
app.jar -> java.base
   com.ex.Foo                                         -> java.lang.Object                                   java.base

Warning: JDK internal APIs are unsupported and private to JDK implementation that are
subject to be removed or changed incompatibly and could break your application.
Please modify your code to eliminate dependence on any JDK internal APIs.
For the most recent update on JDK internal API replacement, please check:
https://wiki.openjdk.java.net/display/JDK8/Java+Dependency+Analysis+Tool

JDK Internal API                         Suggested Replacement
----------------                         ---------------------
sun.misc.BASE64Encoder                   Use java.util.Base64 @since 1.8
sun.misc.Unsafe                          See http://openjdk.java.net/jeps/260
"""

JDEPS_8 = """classes -> C:\\Program Files\\Java\\jdk1.8.0_202\\jre\\lib\\rt.jar
   com.ex.Legacy (classes)
      -> java.lang.Object
      -> sun.misc.BASE64Encoder                             JDK internal API (rt.jar)
   com.ex.Gone (classes)
      -> sun.misc.Unsafe                                    JDK internal API (rt.jar)
"""


def test_jdeps_internal_api_uses_become_claims_at_the_line_naming_the_api(tmp_path):
    r = _repo(tmp_path)
    _log(r, "jdeps.txt", JDEPS_11)
    res = jf.report(r, ["jdeps.txt"])
    cs = res["claims"]
    assert [(c["class"], c["at"], c["level"]) for c in cs] == [
        ("com.ex.Legacy", f"{LEGACY_REL}:3", "error"), ("com.ex.UtilKt", f"{UTIL_REL}:4", "warning")]
    leg = cs[0]
    assert leg["stated_by"] == "jdeps" and leg["internal_api"] == "sun.misc.BASE64Encoder"
    assert leg["replacement"] == "Use java.util.Base64 @since 1.8" and leg["module"] == "JDK removed internal API"
    assert leg["archive"] == "app.jar" and leg["status"] == "strong_inference"
    assert leg["evidence_at"] == [f"{LEGACY_REL}:3", "jdeps.txt:2"] and "names sun.misc.BASE64Encoder" in leg["claim"]
    kt = cs[1]   # the comment line is skipped; the package import is the line
    assert "imports sun.misc.*" in kt["claim"] and "jeps/260" in kt["replacement"]
    s = res["summary"]
    assert s["duplicates"] == 1   # Legacy$1 is Legacy.java's: the same use at the same line
    assert s["unknown_class"] == 1 and res["unknown_classes"] == ["com.lib.Shaded"] and s["not_internal"] == 1


def test_jdk8_jdeps_output_and_an_unknown_class(tmp_path):
    r = _repo(tmp_path)
    _log(r, "jdeps8.txt", JDEPS_8)
    res = jf.report(r, ["jdeps8.txt"], tool="jdeps")
    assert [(c["class"], c["at"], c["module"], c["level"]) for c in res["claims"]] == [
        ("com.ex.Legacy", f"{LEGACY_REL}:3", "rt.jar", "warning")]
    assert res["claims"][0]["archive"] == "classes" and "replacement" not in res["claims"][0]
    assert res["unknown_classes"] == ["com.ex.Gone"]


def test_parse_helpers_on_odd_input():
    diags, counts = jf.parse_javac([
        "Foo.java:12a: warning: [Broken] line number with a letter",
        "Foo.java:: error: [Broken] no line",
        "src/Foo.java:3: warning: [MissingOverride] run implements a method; needs @Override",
        "\x00\x01 binary junk \xff",
        "warning: [options] bootstrap class path not set in conjunction with -source 8",
        "    [javac] src/Bar.java:5: warning: [UnusedVariable] The local variable 'x' is never read.",
    ])
    assert [(d["path"], d["line"], d["check"], d["tool"]) for d in diags] == [
        ("src/Foo.java", 3, "MissingOverride", "Error Prone"), ("src/Bar.java", 5, "UnusedVariable", "Error Prone")]
    assert counts == {"javac_other": 0, "unreadable": 2}
    assert diags[0]["quoted"] is None and diags[0]["column"] is None
    uses, repl, c = jf.parse_jdeps(["   a.B -> c.D", "x -> y", "   -> sun.misc.Unsafe  JDK internal API"])
    assert uses == [] and repl == {} and c == {"not_internal": 0}
    assert jf.replacement_of("sun.misc.Signal$Handler", {"sun.misc": "pkg"}) == "pkg"
    assert jf.replacement_of("a.B", {}) is None


def test_unreadable_missing_empty_and_elsewhere_files_are_counted_not_crashes(tmp_path):
    r = _repo(tmp_path)
    other = tmp_path / "elsewhere" / "Other.java"   # a file on this machine outside the repository
    other.parent.mkdir()
    other.write_text("class Other {}\n", encoding="utf-8")
    (r / "a/src/main/java/com/ex").mkdir(parents=True)
    (r / "b/src/main/java/com/ex").mkdir(parents=True)
    (r / "a/src/main/java/com/ex/Dup.java").write_text("class Dup {}\n", encoding="utf-8")
    (r / "b/src/main/java/com/ex/Dup.java").write_text("class Dup {}\n", encoding="utf-8")
    (r / "x/old").mkdir(parents=True)
    (r / "x/old/Foo.java").write_text("class Foo {}\n", encoding="utf-8")
    _log(r, "odd.log", "\n".join([
        f"{other}:1: warning: [ClassCanBeStatic] Inner class is non-static",
        f"{r / 'old' / 'Foo.java'}:1: warning: [ClassCanBeStatic] a deleted file, not x/old/Foo.java",
        "src/main/java/com/ex/Dup.java:1: warning: [ClassCanBeStatic] which module?",
        "/home/runner/work/x/x/src/main/java/com/ex/Missing.java:3: error: [DeadException] gone",
        "Foo.java:x: warning: [Broken] m", ""]))
    _log(r, "empty.log", "")
    (r / "bin.log").write_bytes(bytes(range(256)) * 4)
    res = jf.report(r, ["odd.log", "empty.log", "bin.log", "missing.log"])
    assert res["claims"] == []
    s = res["summary"]
    assert (s["not_in_repository"], s["ambiguous"], s["unreadable"]) == (3, 1, 1)
    errs = {x["file"]: x for x in res["inputs"]}
    assert errs["missing.log"].get("error") and "no Error Prone" in errs["empty.log"]["note"]
    assert "no Error Prone" in errs["bin.log"]["note"]


def test_tool_filter_and_paths(tmp_path):
    r = _repo(tmp_path)
    _log(r, "all.log", _javac(str(r / FOO_REL)) + "\n" + JDEPS_11)
    assert {c["stated_by"] for c in jf.report(r, ["all.log"])["claims"]} == {"Error Prone", "NullAway", "jdeps"}
    res = jf.report(r, ["all.log"], tool="nullaway")
    assert [c["rule"] for c in res["claims"]] == ["NullAway"] and res["summary"]["other_tool"] == 1
    assert {c["stated_by"] for c in jf.report(r, ["all.log"], tool="errorprone")["claims"]} == {
        "Error Prone", "NullAway"}
    assert {c["stated_by"] for c in jf.report(r, ["all.log"], tool="jdeps")["claims"]} == {"jdeps"}
    res = jf.report(r, ["all.log"], ["app"])
    assert [c["at"] for c in res["claims"]] == [f"{UTIL_REL}:4"] and res["summary"]["outside_paths"] == 3
    with pytest.raises(ValueError):
        jf.report(r, ["all.log"], tool="spotbugs")


def test_a_powershell_utf16_log_and_a_relative_subproject_path(tmp_path):
    r = _repo(tmp_path)
    # Gradle run from the sub-project folder prints paths relative to it; PowerShell 5.1's > writes UTF-16
    log = "src/main/kotlin/com/ex/Util.kt:6: warning: [Nope] Kotlin is not javac\n" + \
          "src/main/java/com/ex/Foo.java:12: error: [DeadException] Exception created but not thrown\n" + \
          '            new IllegalArgumentException("negative");\n            ^\n'
    _log(r, "ps.log", log, "utf-16")
    res = jf.report(r, ["ps.log"])
    assert [c["at"] for c in res["claims"]] == [f"{FOO_REL}:12"]
    assert res["claims"][0]["status"] == "strong_inference"   # tied exactly: the relative path is a repo file


def test_an_error_prone_sarif_file_is_read_by_the_sarif_reader(tmp_path):
    r = _repo(tmp_path)
    doc = {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "Error Prone", "version": "2.36.0"}},
                                         "results": [{"ruleId": "DeadException", "level": "error",
                                                      "message": {"text": "Exception created but not thrown"},
                                                      "locations": [{"physicalLocation": {
                                                          "artifactLocation": {"uri": FOO_REL},
                                                          "region": {"startLine": 12}}}]}]}]}
    (r / "ep.sarif").write_text(json.dumps(doc), encoding="utf-8")
    (r / "bad.sarif").write_text('{"version": "1.0"}', encoding="utf-8")
    res = jf.report(r, ["ep.sarif", "bad.sarif"])
    c = res["claims"][0]
    assert c["stated_by"] == "Error Prone" and c["rule"] == "DeadException" and c["at"] == f"{FOO_REL}:12"
    ins = {x["file"]: x for x in res["inputs"]}
    assert ins["ep.sarif"]["found"] == {"Error Prone": 1} and ins["ep.sarif"]["versions"] == {"Error Prone": "2.36.0"}
    assert "not a SARIF 2.1 log" in ins["bad.sarif"]["error"]


def test_cli_text_json_and_exit_codes(tmp_path, capsys):
    r = _repo(tmp_path)
    assert cli.main(["import-findings", "--repo", str(r), "nope.log"]) == 4
    assert "no file read" in capsys.readouterr().err
    _log(r, "build.log", _javac(str(r / FOO_REL)) + "\n" + JDEPS_11)
    assert cli.main(["import-findings", "--repo", str(r), "build.log", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["summary"]["by_tool"] == {"Error Prone": 1, "NullAway": 1, "jdeps": 2}
    assert cli.main(["import-findings", "--repo", str(r), "build.log", "--limit", "1"]) == 0
    text = capsys.readouterr().out
    assert "[strong_inference] Error Prone reports DeadException (error)" in text
    assert "Did you mean" in text and "log build.log:5" in text
    assert "not checked by Verinoda" in text and "3 more (--limit, --json)" in text
    assert cli.main(["import-findings", "--repo", str(r), "build.log", "--tool", "jdeps"]) == 0
    text = capsys.readouterr().out
    assert "[strong_inference] jdeps reports com.ex.Legacy uses JDK internal API sun.misc.BASE64Encoder" in text
    assert "classes with no source file here: com.lib.Shaded" in text
    assert cli.main(["import-findings", "--repo", str(r), "build.log", "--tool", "nullaway"]) == 0
    assert "NullAway reports NullAway" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        cli.main(["import-findings", "--repo", str(r), "build.log", "--tool", "spotbugs"])
    assert e.value.code == 2
    capsys.readouterr()
    assert cli.main(["import-findings", "--repo", str(r), "build.log", "--limit", "0"]) == 2


# -- review round -------------------------------------------------------------------------------------------

def _write(r: Path, rel: str, text: str) -> None:
    (r / rel).parent.mkdir(parents=True, exist_ok=True)
    (r / rel).write_text(text, encoding="utf-8", newline="\n")
    old = time.time() - 1000
    os.utime(r / rel, (old, old))


def test_jdeps_line_is_strong_only_when_named_on_a_code_line(tmp_path):
    r = _repo(tmp_path)
    # the public java.lang.ref.Cleaner is not jdk.internal.ref.Cleaner; the internal DirectBuffer is named
    _write(r, "src/main/java/com/ex/Cl.java", "\n".join([
        "package com.ex;", "", "import java.lang.ref.Cleaner;", "import java.nio.ByteBuffer;", "",
        "public class Cl {", "    private static final Cleaner CLEANER = Cleaner.create();",
        "    void free(ByteBuffer bb) { ((sun.nio.ch.DirectBuffer) bb).cleaner().clean(); }", "}", ""]))
    # a javadoc line with no leading "*", and a use by reflection with a computed name
    _write(r, "src/main/java/com/ex/Sig.java", "\n".join([
        "package com.ex;", "", "/**", " Handles the Signal from the OS. Uses sun.misc.Signal", " */",
        "public class Sig {", '    Object h = load("sun.misc." + "Sig" + "nal");', "}", ""]))
    # the simple name only, no import: a guess
    _write(r, "src/main/java/com/ex/Un.java", "\n".join([
        "package com.ex;", "", "class Un {", '    String s = "Unsafe";  /* Unsafe */',
        "    Object u = Unsafe.getUnsafe();", "}", ""]))
    # a default-package class in the repository, and one from a dependency with the name of com/ex/Foo.java
    _write(r, "src/main/java/Top.java", "class Top {\n    long o = sun.misc.Unsafe.ARRAY_BYTE_BASE_OFFSET;\n}\n")
    _log(r, "jdeps.txt", "\n".join([
        "app.jar -> java.base",
        "   com.ex.Cl -> jdk.internal.ref.Cleaner   JDK internal API (java.base)",
        "   com.ex.Cl -> sun.nio.ch.DirectBuffer   JDK internal API (java.base)",
        "app.jar -> jdk.unsupported",
        "   com.ex.Sig -> sun.misc.Signal   JDK internal API (jdk.unsupported)",
        "   com.ex.Un -> sun.misc.Unsafe   JDK internal API (jdk.unsupported)",
        "   Top -> sun.misc.Unsafe   JDK internal API (jdk.unsupported)",
        "lib.jar -> jdk.unsupported",
        "   Foo -> sun.misc.Unsafe   JDK internal API (jdk.unsupported)", ""]))
    res = jf.report(r, ["jdeps.txt"])
    by = {(c["class"], c["internal_api"]): c for c in res["claims"]}
    cl = by[("com.ex.Cl", "jdk.internal.ref.Cleaner")]
    assert cl["at"] == "src/main/java/com/ex/Cl.java" and cl["status"] == "weak_inference"
    assert "imports another Cleaner (java.lang.ref.Cleaner)" in cl["claim"]
    db = by[("com.ex.Cl", "sun.nio.ch.DirectBuffer")]
    assert db["at"] == "src/main/java/com/ex/Cl.java:8" and db["status"] == "strong_inference"
    sig = by[("com.ex.Sig", "sun.misc.Signal")]
    assert sig["at"] == "src/main/java/com/ex/Sig.java" and sig["status"] == "weak_inference"
    un = by[("com.ex.Un", "sun.misc.Unsafe")]   # not the string, not the comment: line 5, a guess
    assert un["at"] == "src/main/java/com/ex/Un.java:5" and un["status"] == "weak_inference"
    assert "simple name only" in un["claim"]
    top = by[("Top", "sun.misc.Unsafe")]
    assert top["at"] == "src/main/java/Top.java:2" and top["status"] == "strong_inference"
    assert res["unknown_classes"] == ["Foo"] and ("Foo", "sun.misc.Unsafe") not in by


def test_ant_and_stamped_lines_keep_the_source_indentation_for_the_column(tmp_path):
    r = _repo(tmp_path)
    foo = r / FOO_REL
    _log(r, "ant.log", "\n".join([
        "compile:",
        f"    [javac] {foo}:7: warning: [NullAway] dereferenced expression name is @Nullable",
        "    [javac]         return name.length();",
        "    [javac]                    ^",
        "    [javac]     (see http://t.uber.com/nullaway )", ""]))
    c = jf.report(r, ["ant.log"])["claims"][0]
    assert c["column"] == 20 and c["status"] == "strong_inference" and c["see"] == "http://t.uber.com/nullaway"
    # GitHub Actions' timestamp and Jenkins Timestamper's bracketed one take one space, not the indentation
    _log(r, "gh.log", "\n".join([
        f"2026-09-30T10:00:00.1234567Z {foo}:7: warning: [NullAway] dereferenced expression name is @Nullable",
        "2026-09-30T10:00:00.1234567Z         return name.length();",
        "2026-09-30T10:00:00.1234567Z                    ^",
        f"[2026-09-30T10:00:01.123Z] {foo}:12: error: [DeadException] Exception created but not thrown",
        '[2026-09-30T10:00:01.123Z]             new IllegalArgumentException("negative");',
        "[2026-09-30T10:00:01.123Z]             ^", ""]))
    by = {c["rule"]: c for c in jf.report(r, ["gh.log"])["claims"]}
    assert by["NullAway"]["column"] == 20 and by["DeadException"]["column"] == 13
    assert by["DeadException"]["at"] == f"{FOO_REL}:12" and by["DeadException"]["status"] == "strong_inference"


def test_localized_javac_severity_words_keep_the_findings(tmp_path):
    r = _repo(tmp_path)
    foo = r / FOO_REL
    _log(r, "de.log", "\n".join([
        f"{foo}:7: Warnung: [NullAway] dereferenced expression name is @Nullable",
        f"{foo}:12: Fehler: [DeadException] Exception created but not thrown",
        f"{foo}:4: Warnung: [rawtypes] raw type", ""]))
    res = jf.report(r, ["de.log"])
    assert [(c["rule"], c["level"]) for c in res["claims"]] == [("DeadException", "error"), ("NullAway", "warning")]
    assert res["summary"]["javac_other"] == 1
    _log(r, "ja.log", "\n".join([
        f"{foo}:7: 警告: [NullAway] dereferenced expression name is @Nullable",
        f"{foo}:12: エラー: [DeadException] Exception created but not thrown",
        f"{foo}:12:  错误：[DeadException] zh, a full-width colon",
        f"{foo}:6: Varoit: [MissingOverride] a word not in the list is a warning", ""]))
    res = jf.report(r, ["ja.log"])
    assert sorted((c["rule"], c["level"], c["at"]) for c in res["claims"]) == [
        ("DeadException", "error", f"{FOO_REL}:12"), ("DeadException", "error", f"{FOO_REL}:12"),
        ("MissingOverride", "warning", f"{FOO_REL}:6"), ("NullAway", "warning", f"{FOO_REL}:7")]


TRIM = "package com.ex;\n" + "// pad\n" * 13 + 'class Trim { void f() {\n        "x".trim();\n} }\n'


def test_a_blank_line_inside_a_message_keeps_the_quoted_line_caret_and_link(tmp_path):
    r = _repo(tmp_path)
    _write(r, "src/main/java/com/ex/Trim.java", TRIM)
    assert TRIM.splitlines()[15] == '        "x".trim();'
    _log(r, "ep.log", "\n".join([
        "/home/runner/work/p/p/src/main/java/com/ex/Trim.java:16: error: [CheckReturnValue] The result of `trim()` "
        "must be used",
        "If you really don't want to use the result, then assign it to a variable: `var unused = ...`.",
        "",
        "If callers of `trim()` shouldn't be required to use its result, then annotate it with @CanIgnoreReturnValue.",
        '        "x".trim();',
        " " * 16 + "^",
        "    (see https://errorprone.info/bugpattern/CheckReturnValue)",
        "",
        "1 error", ""]))
    c = jf.report(r, ["ep.log"])["claims"][0]
    assert c["at"] == "src/main/java/com/ex/Trim.java:16" and c["status"] == "strong_inference"
    assert c["column"] == 17 and c["quoted"] == '"x".trim();'
    assert c["see"] == "https://errorprone.info/bugpattern/CheckReturnValue"
    assert "@CanIgnoreReturnValue" in c["message"] and "var unused" in c["message"]
    # a blank line with no caret after it still ends the block: the next lines are not the finding's
    diags, _ = jf.parse_javac(["a/B.java:3: warning: [X] m", "", "  y();"])
    assert diags[0]["quoted"] is None


def test_unc_and_device_paths_from_a_log_are_never_opened(tmp_path, monkeypatch):
    r = _repo(tmp_path)
    opened: list[str] = []
    real = Path.exists

    def spy(self, *a, **k):
        opened.append(str(self))
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "exists", spy)
    _log(r, "unc.log", "\n".join([
        "\\\\evil.example.invalid\\share\\src\\main\\java\\com\\ex\\Foo.java:7: warning: [NullAway] m",
        "//evil.example.invalid/share/com/ex/Foo.java:9: warning: [NullAway] n",
        "\\\\?\\UNC\\evil.example.invalid\\s\\Foo.java:9: warning: [NullAway] o",
        "\\\\.\\pipe\\x\\Foo.java:9: warning: [NullAway] p", ""]))
    res = jf.report(r, ["unc.log"])
    assert [c["at"] for c in res["claims"]] == [f"{FOO_REL}:7"]   # still tied by its suffix
    assert res["summary"]["not_in_repository"] == 3
    doc = {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "Error Prone"}}, "results": [
        {"ruleId": "X", "message": {"text": "m"}, "locations": [{"physicalLocation": {
            "artifactLocation": {"uri": "file://evil.example.invalid/share/src/main/java/com/ex/Foo.java"},
            "region": {"startLine": 7}}}]}]}]}
    (r / "unc.sarif").write_text(json.dumps(doc), encoding="utf-8")
    assert jf.report(r, ["unc.sarif"])["claims"][0]["at"] == f"{FOO_REL}:7"
    assert not [p for p in opened if p.replace("\\", "/").startswith("//") or "evil" in p]


def test_two_findings_on_one_line_at_two_columns_are_both_kept(tmp_path):
    r = _repo(tmp_path)
    foo = r / FOO_REL
    _log(r, "two.log", "\n".join([
        f"{foo}:7: warning: [NullAway] dereferenced expression is @Nullable",
        "        return name.length();", " " * 19 + "^",
        f"{foo}:7: warning: [NullAway] dereferenced expression is @Nullable",
        "        return name.length();", " " * 15 + "^",
        f"{foo}:7: warning: [NullAway] dereferenced expression is @Nullable",   # a reprint: dropped
        "        return name.length();", " " * 19 + "^", ""]))
    res = jf.report(r, ["two.log"])
    assert [c["column"] for c in res["claims"]] == [16, 20] and res["summary"]["duplicates"] == 1


def test_log_lines_order_encodings_and_jdeps_noise(tmp_path):
    r = _repo(tmp_path)
    foo = r / FOO_REL
    # a lone "\r" (progress output) and a form feed do not shift the log line numbers
    _log(r, "cr.log", f"Downloading 10%\rDownloading 100%\n\x0c\n{foo}:10: warning: [NullAway] m\n"
                      f"{foo}:9: warning: [NullAway] n\n")
    cs = jf.report(r, ["cr.log"])["claims"]
    assert [c["at"] for c in cs] == [f"{FOO_REL}:9", f"{FOO_REL}:10"]   # 9 before 10
    assert cs[1]["evidence_at"][1] == "cr.log:3" and cs[0]["evidence_at"][1] == "cr.log:4"
    # a log in a Windows ANSI code page with an accented folder under the repository: tied exactly
    acc = tmp_path / "café"
    _write(acc, FOO_REL, FOO)
    line = f"{acc / FOO_REL}:7: warning: [NullAway] m\n"
    try:
        data = line.encode("cp1252")
    except UnicodeEncodeError:
        pytest.skip("the temporary folder is not cp1252")
    (acc / "cp.log").write_bytes(data)
    c = jf.report(acc, ["cp.log"])["claims"][0]
    assert c["at"] == f"{FOO_REL}:7" and "tied to" not in c["claim"]
    # a SARIF file PowerShell wrote as UTF-16
    doc = {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "Error Prone"}}, "results": [
        {"ruleId": "X", "message": {"text": "m"}, "locations": [{"physicalLocation": {
            "artifactLocation": {"uri": FOO_REL}, "region": {"startLine": 7}}}]}]}]}
    (r / "u16.sarif").write_bytes(json.dumps(doc).encode("utf-16"))
    res = jf.report(r, ["u16.sarif"])
    assert res["claims"][0]["at"] == f"{FOO_REL}:7" and res["inputs"][0]["found"] == {"Error Prone": 1}
    # a quoted lambda in a javac log is not a jdeps dependence
    _log(r, "lam.log", f"{foo}:7: warning: [NullAway] m\n    list.forEach(x -> y.z foo);\n"
                       "    handler    -> some.Thing  other\n")
    assert jf.report(r, ["lam.log"])["summary"]["not_internal"] == 0
