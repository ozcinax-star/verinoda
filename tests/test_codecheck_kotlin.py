"""The Kotlin check (verinoda/codecheck_kotlin.py, docs/DESIGN.md D45): members and properties on receivers
of known type, imports and types, against the project's Kotlin and Java sources, the classpath and the JDK;
extensions, smart casts, Kotlin's built-in types and files that do not parse keep a name ``unknown``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jvmfixtures import PUBLIC, STATIC, base_classes, class_bytes, write_jar
from verinoda import codecheck, jvmclass

STDLIB = {
    # what marks kotlin-stdlib as read, and a library extension (`fun Widget.shout()`) in a file facade
    "kotlin/collections/CollectionsKt": class_bytes("kotlin/collections/CollectionsKt", methods=(
        ("listOf", "()Ljava/util/List;", PUBLIC | STATIC),)),
    "lib/Widget": class_bytes("lib/Widget", methods=(("<init>", "()V", PUBLIC), ("render", "()V", PUBLIC),
                                                     ("getSize", "()I", PUBLIC))),
    "lib/WidgetsKt": class_bytes("lib/WidgetsKt", methods=(("shout", "(Llib/Widget;)V", PUBLIC | STATIC),)),
}

ENGINE = '''package app

import lib.Widget

open class Engine(val power: Int, var label: String) {
    fun start(): Boolean = true
    companion object {
        fun create(): Engine = Engine(1, "x")
    }
}

class Turbo(power: Int) : Engine(power, "t") {
    fun boostMode() {}
}

object Registry {
    fun register(e: Engine) = e
}

fun Engine.boost(): Int = power * 2

fun use(e: Engine, w: Widget) {
    e.start()
    e.stop()
    val p = e.power
    val q = e.powr
    e.boost()
    Engine.create().start()
    Registry.register(e)
    Registry.unregister(e)
    e.let { it.start() }
    w.render()
    w.size
    w.sise
    w.shout()
    "x".uppercasee()
    if (e is Turbo) e.boostMode()
}
'''

USER = '''package app;

class User {
    void f() {
        Engine e = Engine.Companion.create();
        e.start();
        e.getPower();
        e.stopp();
        Registry.INSTANCE.register(e);
        Missing m = null;
    }
}
'''


def _repo(tmp_path: Path, *, stdlib: bool = True) -> Path:
    repo = tmp_path / "proj"
    write_jar(repo / "libs" / "lib.jar", {**base_classes(), **{k: v for k, v in STDLIB.items()
                                                               if stdlib or not k.startswith("kotlin/")}})
    (repo / "src" / "app").mkdir(parents=True)
    (repo / "src" / "app" / "Engine.kt").write_text(ENGINE, encoding="utf-8")
    (repo / "src" / "app" / "User.java").write_text(USER, encoding="utf-8")
    (repo / ".verinoda").mkdir()
    (repo / ".verinoda" / "config.json").write_text(json.dumps({"code_check": {"classpath": ["libs/*.jar"]}}),
                                                    encoding="utf-8")
    return repo


@pytest.fixture(autouse=True)
def _no_machine_jdk(monkeypatch):
    monkeypatch.setattr(jvmclass, "_jdk_homes", lambda: [])


def _by(res: dict) -> dict[tuple[int, str], dict]:
    out: dict = {}
    for s in res["sites"]:
        key = (s["line"], s["name"])
        if key not in out or out[key]["kind"] in ("type", "import"):
            out[key] = s
    return out


def test_members_and_properties_on_known_receivers(tmp_path):
    res = codecheck.check(_repo(tmp_path), ["src/app/Engine.kt"], env="none", include_exists=True)
    s = _by(res)
    assert s[(23, "start")]["verdict"] == "exists" and s[(23, "start")]["language"] == "Kotlin"
    assert s[(24, "stop")]["verdict"] == "absent"
    assert s[(25, "power")]["verdict"] == "exists"
    assert s[(26, "powr")]["verdict"] == "absent" and s[(26, "powr")]["nearest"][0]["name"] == "power"
    assert s[(27, "boost")]["verdict"] == "exists"          # the project's extension on Engine
    assert s[(28, "start")]["verdict"] == "exists"          # a companion object's function, then its type
    assert s[(30, "unregister")]["verdict"] == "absent"      # an object's members
    assert s[(31, "let")]["verdict"] == "exists"
    assert s[(32, "render")]["verdict"] == "exists" and s[(33, "size")]["verdict"] == "exists"  # a Java getter
    assert s[(34, "sise")]["verdict"] == "absent"
    assert res["exit"] == 3


def test_extensions_builtins_and_smart_casts_stay_unknown(tmp_path):
    s = _by(codecheck.check(_repo(tmp_path), ["src/app/Engine.kt"], env="none", include_exists=True))
    assert s[(35, "shout")]["verdict"] == "unknown" and "extension" in s[(35, "shout")]["why"]   # a library's
    assert s[(36, "uppercasee")]["verdict"] == "unknown"    # a Kotlin built-in type: the stdlib adds members
    assert s[(37, "boostMode")]["verdict"] != "absent"      # `e is Turbo`: smart-cast


def test_without_kotlin_stdlib_a_missing_member_is_unknown(tmp_path):
    s = _by(codecheck.check(_repo(tmp_path, stdlib=False), ["src/app/Engine.kt"], env="none", include_exists=True))
    assert s[(24, "stop")]["verdict"] == "unknown" and "kotlin-stdlib" in s[(24, "stop")]["why"]


def test_java_sees_the_projects_kotlin_types(tmp_path):
    s = _by(codecheck.check(_repo(tmp_path), ["src/app/User.java"], env="none", include_exists=True))
    assert s[(5, "create")]["verdict"] == "exists" and s[(6, "start")]["verdict"] == "exists"
    assert s[(7, "getPower")]["verdict"] == "exists" and s[(8, "stopp")]["verdict"] == "absent"
    assert s[(9, "register")]["verdict"] == "exists"        # an object's INSTANCE
    assert s[(10, "Missing")]["verdict"] == "absent"         # the package holds Java and Kotlin, all read


def test_a_kotlin_file_that_does_not_parse_decides_nothing(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src" / "app" / "Broken.kt").write_text(
        "package app\n\npublic class Broken\nprivate constructor(val a: Int) {\n  fun ok() = 1\n}\n"
        "fun use2(e: Engine) { e.stopp( }\n", encoding="utf-8")
    res = codecheck.check(repo, ["src/app/Broken.kt", "src/app/User.java"], env="none", include_exists=True)
    assert not any(x["verdict"] == "absent" and x["path"].endswith("Broken.kt") for x in res["sites"])
    # the package is not closed any more: a name that is not found may be one the broken file declares
    s = _by({"sites": [x for x in res["sites"] if x["path"].endswith("User.java")]})
    assert s[(10, "Missing")]["verdict"] == "unknown"


def test_kotlin_imports(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src" / "app" / "Imp.kt").write_text(
        "package app\n\nimport lib.Widget\nimport lib.Widgit\nimport lib.shout\nimport kotlin.collections.List\n"
        "import app.boost\n", encoding="utf-8")
    s = _by(codecheck.check(repo, ["src/app/Imp.kt"], env="none", include_exists=True))
    assert s[(3, "Widget")]["verdict"] == "exists" and s[(4, "Widgit")]["verdict"] == "absent"
    assert s[(5, "shout")]["verdict"] == "exists"            # a library's top-level function
    assert s[(6, "List")]["verdict"] == "exists"             # a built-in type
    assert s[(7, "boost")]["verdict"] == "exists"            # the project's extension function
