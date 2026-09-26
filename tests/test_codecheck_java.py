"""The Java check (verinoda/codecheck_java.py, verinoda/jvmclass.py, docs/DESIGN.md D43): classes, methods with
their arity, fields, constructors and Mixin targets, against the project's sources, a classpath of jars and the
JDK; ``absent`` only where every place a name could come from is read."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from jvmfixtures import ABSTRACT, INTERFACE, PUBLIC, STATIC, VARARGS, base_classes, class_bytes, write_jar
from verinoda import codecheck, jvmclass

LIB = {
    "lib/Widget": class_bytes("lib/Widget", ifaces=("lib/Shape",), methods=(
        ("<init>", "()V", PUBLIC), ("<init>", "(I)V", PUBLIC),
        ("render", "(I)Llib/Widget;", PUBLIC), ("render", "(II)Llib/Widget;", PUBLIC),
        ("create", "()Llib/Widget;", PUBLIC | STATIC), ("label", "()Ljava/lang/String;", PUBLIC),
        ("tick", "()V", PUBLIC), ("all", "([Ljava/lang/Object;)V", PUBLIC | VARARGS)),
        fields=(("SIZE", "I", PUBLIC | STATIC), ("owner", "Llib/Widget;", PUBLIC))),
    "lib/Widget$Part": class_bytes("lib/Widget$Part", methods=(("<init>", "()V", PUBLIC),)),
    "lib/Shape": class_bytes("lib/Shape", flags=PUBLIC | INTERFACE | ABSTRACT, methods=(
        ("area", "()D", PUBLIC | ABSTRACT),)),
    "lib/Holder": class_bytes("lib/Holder", methods=(("get", "()Ljava/lang/Object;", PUBLIC),)),
    "lib/Opaque": class_bytes("lib/Opaque", super_="gone/Base", methods=(("<init>", "()V", PUBLIC),)),
}

APP = '''package app;

import lib.Widget;
import lib.Widgit;
import lib.Opaque;
import static lib.Widget.create;

public class App {
    private Widget field = new Widget();

    enum Mode { ON, OFF }

    void run(Widget w, String s) {
        w.render(1);
        w.render(1, 2, 3);
        w.rendr(1);
        w.render(1).label().trim();
        w.render(1).label().trimm();
        Widget.create().tick();
        create().area();
        int n = Widget.SIZE + Widget.SIZ;
        field.owner.tick();
        new Widget(1, 2);
        new Widget.Part();
        w.all(1, 2, 3);
        String t = String.format("%d %d", 1, 2);
        new Opaque().anything();
        Mode.values();
        Mode.valueOf("ON").ordinal();
        record Pair(int a, int b) {}
        Pair p = new Pair(1, 2);
        p.a();
        var v = new Widget();
        v.tick(/* not an argument */);
        App.this.run(w, s);
    }
}
'''

MIXIN = '''package app.mixin;

import lib.Widget;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Shadow;
import org.spongepowered.asm.mixin.injection.Inject;

@Mixin(Widget.class)
abstract class WidgetMixin {
    @Shadow public Widget owner;
    @Shadow public Widget ownr;
    @Inject(method = "tick", at = @At("HEAD"))
    private void a() {}
    @Inject(method = "tic" + "k()V")
    private void b() {}
    @Inject(method = {"render", "rendr"})
    private void c() {}
}
'''


def _repo(tmp_path: Path, *, config: bool = True) -> Path:
    repo = tmp_path / "proj"
    write_jar(repo / "libs" / "lib.jar", {**base_classes(), **LIB})
    (repo / "src" / "app" / "mixin").mkdir(parents=True)
    (repo / "src" / "app" / "App.java").write_text(APP, encoding="utf-8")
    (repo / "src" / "app" / "mixin" / "WidgetMixin.java").write_text(MIXIN, encoding="utf-8")
    (repo / ".verinoda").mkdir()
    if config:
        (repo / ".verinoda" / "config.json").write_text(json.dumps({"code_check": {"classpath": ["libs/*.jar"]}}),
                                                        encoding="utf-8")
    return repo


@pytest.fixture(autouse=True)
def _no_machine_jdk(monkeypatch):
    """The tests read their own java.lang, never the machine's JDK."""
    monkeypatch.setattr(jvmclass, "_jdk_homes", lambda: [])


def _sites(res: dict) -> dict[tuple[int, str], dict]:
    """Sites by (line, name); where a type name and a call or constructor share both, the call's."""
    out: dict[tuple[int, str], dict] = {}
    for s in res["sites"]:
        key = (s["line"], s["name"])
        if key not in out or out[key]["kind"] in ("type", "import"):
            out[key] = s
    return out


def test_names_are_checked_against_the_classpath(tmp_path):
    repo = _repo(tmp_path)
    res = codecheck.check(repo, ["src/app/App.java"], env="none", include_exists=True)
    s = _sites(res)
    assert s[(3, "Widget")]["verdict"] == "exists"
    assert s[(4, "Widgit")]["verdict"] == "absent" and s[(4, "Widgit")]["nearest"][0]["name"] == "Widget"
    assert s[(14, "render")]["verdict"] == "exists"
    assert s[(15, "render")]["verdict"] == "absent" and "overloads take 1, 2" in s[(15, "render")]["why"]
    assert s[(16, "rendr")]["verdict"] == "absent" and s[(16, "rendr")]["nearest"][0]["name"] == "render"
    assert s[(17, "trim")]["verdict"] == "exists"      # a chain: render() returns Widget, label() a String
    assert s[(18, "trimm")]["verdict"] == "absent"
    assert s[(19, "tick")]["verdict"] == "exists"      # a static call on the class, then its return type
    assert s[(20, "area")]["verdict"] == "exists"      # a static import; area() from the interface
    assert s[(21, "SIZ")]["verdict"] == "absent" and s[(21, "SIZE")]["verdict"] == "exists"
    assert s[(22, "tick")]["verdict"] == "exists"      # a field's declared type
    assert res["exit"] == 3


def test_constructors_varargs_enums_local_types_and_comments(tmp_path):
    repo = _repo(tmp_path)
    s = _sites(codecheck.check(repo, ["src/app/App.java"], env="none", include_exists=True))
    assert s[(23, "Widget")]["verdict"] == "absent" and "they take (); (int)" in s[(23, "Widget")]["why"]
    assert s[(25, "all")]["verdict"] == "exists"       # varargs: any number from the fixed ones on
    assert s[(26, "format")]["verdict"] == "exists"
    assert s[(28, "values")]["verdict"] == "exists" and s[(29, "ordinal")]["verdict"] == "exists"
    assert s[(32, "a")]["verdict"] == "exists"         # a record declared in the method: its accessor
    assert s[(34, "tick")]["verdict"] == "exists"      # a comment is not an argument
    assert s[(35, "run")]["verdict"] == "exists"       # App.this
    assert not any(x["verdict"] == "absent" and 24 <= x["line"] <= 35 for x in s.values())


def test_a_super_type_that_is_not_read_makes_it_unknown(tmp_path):
    repo = _repo(tmp_path)
    s = _sites(codecheck.check(repo, ["src/app/App.java"], env="none", include_exists=True))
    assert s[(27, "anything")]["verdict"] == "unknown" and "gone.Base" in s[(27, "anything")]["why"]


def test_without_a_complete_classpath_library_names_are_unknown(tmp_path):
    repo = _repo(tmp_path, config=False)
    res = codecheck.check(repo, ["src/app/App.java"], env="none", include_exists=True)
    s = _sites(res)
    assert s[(4, "Widgit")]["verdict"] == "unknown" and not any(x["verdict"] == "absent" for x in s.values())
    assert any("no classpath" in x for x in res["limits"])
    assert res["java"]["builds"][0]["classpath"] == "none"


def test_mixin_targets(tmp_path):
    repo = _repo(tmp_path)
    s = _sites(codecheck.check(repo, ["src/app/mixin/WidgetMixin.java"], env="none", include_exists=True))
    assert s[(10, "owner")]["verdict"] == "exists" and s[(11, "ownr")]["verdict"] == "absent"
    assert s[(12, "tick")]["verdict"] == "exists"
    assert s[(14, "tick")]["verdict"] == "exists"      # "tic" + "k()V": one target, its descriptor dropped
    assert s[(16, "render")]["verdict"] == "exists" and s[(16, "rendr")]["verdict"] == "absent"
    assert s[(16, "rendr")]["nearest"][0]["name"] == "render"


def test_a_snippet_and_the_diff_are_checked_as_java(tmp_path):
    repo = _repo(tmp_path)
    snip = codecheck.check(repo, snippet="package app;\nimport lib.Widget;\nclass N { void f(Widget w) { w.tock(); } }\n",
                           as_path="src/app/N.java", env="none")
    assert [x["name"] for x in snip["sites"] if x["verdict"] == "absent"] == ["tock"] and snip["exit"] == 3

    def git(*a):
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True,
                       capture_output=True)

    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    p = repo / "src" / "app" / "App.java"
    p.write_text(p.read_text(encoding="utf-8").replace("w.render(1);", "w.render(1);\n        w.tack();"),
                 encoding="utf-8")
    res = codecheck.check(repo, diff="HEAD", env="none")
    assert [x["name"] for x in res["sites"] if x["verdict"] == "absent"] == ["tack"]  # only the changed line


def test_each_build_has_its_own_classpath(tmp_path):
    repo = _repo(tmp_path)
    other = repo / "tools" / "plugin"
    (other / "src" / "p").mkdir(parents=True)
    (other / "build.gradle").write_text("plugins { id 'java' }\n", encoding="utf-8")
    (other / "src" / "p" / "P.java").write_text("package p;\nimport lib.Widgit;\nclass P {}\n", encoding="utf-8")
    res = codecheck.check(repo, ["tools/plugin/src/p/P.java"], env="none", include_exists=True)
    assert [b["build"] for b in res["java"]["builds"]] == ["tools/plugin"]
    assert res["sites"][0]["verdict"] == "unknown"   # the repository's classpath is not this build's


def test_class_files_are_read_with_arity_varargs_and_generic_returns():
    b = class_bytes("x/Y", methods=(("m", "(ILjava/lang/String;[J)V", PUBLIC), ("v", "([I)V", PUBLIC | VARARGS)))
    c = jvmclass.parse_class(b)
    assert c["name"] == "x/Y" and c["super"] == "java/lang/Object"
    # the access bits and the parameter types are kept (docs/DESIGN.md D51)
    assert c["methods"]["m"] == [[3, PUBLIC, "V", ["I", "java/lang/String", "[J"]]]
    assert c["methods"]["v"][0][:2] == [1, PUBLIC | VARARGS] and c["methods"]["v"][0][3] == ["[I"]
    assert jvmclass.method_shape("(IJLa/B;[[Lc/D;)La/E;") == (4, "a/E")
    assert jvmclass._generic_return("<T:Ljava/lang/Object;>()TT;") and not jvmclass._generic_return("()La/B;")


ACCESS_LIB = {
    "lib/Mob": class_bytes("lib/Mob", methods=(
        ("<init>", "()V", PUBLIC), ("isImmobile", "()Z", 0x0004), ("secret", "()V", 0x0002),
        ("pkgOnly", "()V", 0), ("health", "()F", PUBLIC))),
    "lib/Chunk": class_bytes("lib/Chunk", methods=(("<init>", "(J)V", PUBLIC), ("<init>", "(II)V", PUBLIC))),
    "lib/Pos": class_bytes("lib/Pos", methods=(("<init>", "()V", PUBLIC),)),
}

ACCESS_APP = '''package app;

import lib.Chunk;
import lib.Mob;
import lib.Pos;

class Outside {
    void run(Mob m, Pos pos, long packed) {
        m.isImmobile();
        m.secret();
        m.pkgOnly();
        m.health();
        new Chunk(pos);
        new Chunk(packed);
        new Chunk(1, 2);
        new Chunk(Long.valueOf(3));
    }
}

class Inside extends Mob {
    void run() {
        isImmobile();
        this.isImmobile();
    }
}
'''


def test_access_and_constructor_parameter_types(tmp_path):
    """docs/DESIGN.md D51: a member the code cannot reach, or a constructor no argument list fits, is absent."""
    repo = tmp_path / "acc"
    write_jar(repo / "libs" / "lib.jar", {**base_classes(), **ACCESS_LIB})
    (repo / "src" / "app").mkdir(parents=True)
    (repo / "src" / "app" / "Outside.java").write_text(ACCESS_APP, encoding="utf-8")
    (repo / ".verinoda").mkdir()
    (repo / ".verinoda" / "config.json").write_text(json.dumps({"code_check": {"classpath": ["libs/*.jar"]}}),
                                                    encoding="utf-8")
    res = codecheck.check(repo, ["src/app/Outside.java"], env="none", include_exists=True)
    s = _sites(res)
    assert s[(9, "isImmobile")]["verdict"] == "absent" and s[(9, "isImmobile")]["access"] == "protected"
    assert "not a subclass, another package" in s[(9, "isImmobile")]["why"]
    assert s[(10, "secret")]["verdict"] == "absent" and s[(10, "secret")]["access"] == "private"
    assert s[(11, "pkgOnly")]["verdict"] == "absent" and s[(11, "pkgOnly")]["access"] == "package-private"
    assert s[(12, "health")]["verdict"] == "exists"
    assert s[(13, "Chunk")]["verdict"] == "absent"
    assert "takes (Pos): they take (long); (int, int)" in s[(13, "Chunk")]["why"]
    assert s[(14, "Chunk")]["verdict"] == "exists" and s[(15, "Chunk")]["verdict"] == "exists"
    assert s[(16, "Chunk")]["verdict"] == "exists"     # a boxed Long unboxes to long
    assert s[(22, "isImmobile")]["verdict"] == "exists" and s[(23, "isImmobile")]["verdict"] == "exists"


def test_api_lists_the_real_members_of_a_classpath_class(tmp_path):
    """docs/DESIGN.md D51: `verinoda api` reads a Java class as the build sees it (the invented-API guard)."""
    repo = tmp_path / "api"
    write_jar(repo / "libs" / "lib.jar", {**base_classes(), **ACCESS_LIB})
    (repo / "src" / "app").mkdir(parents=True)
    (repo / "src" / "app" / "Outside.java").write_text(ACCESS_APP, encoding="utf-8")
    (repo / ".verinoda").mkdir()
    (repo / ".verinoda" / "config.json").write_text(json.dumps({"code_check": {"classpath": ["libs/*.jar"]}}),
                                                    encoding="utf-8")
    res = codecheck.api(repo, "lib.Chunk", env="none")
    assert res["found"] is True and res["source"] == "jar"
    assert {m["signature"] for m in res["members"] if m["kind"] == "constructor"} == {"Chunk(long)", "Chunk(int, int)"}
    mob = codecheck.api(repo, "Mob.isImmobile", env="none")
    assert [(m["signature"], m["access"]) for m in mob["members"]] == [("isImmobile() -> boolean", "protected")]
    assert codecheck.api(repo, "lib.Chunkk", env="none")["found"] is not True
