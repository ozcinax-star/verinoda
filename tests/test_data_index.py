"""Data files in the search index: data units, what is left out and why, resource links, identical
copies, reference trees, command handlers (search_index + resources + snapshot)."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import index, search_index, snapshot, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

RES = "src/main/resources/"
WISP = """package com.glow.entity;

import com.glow.util.Datapack;

public class Wisp {
    public static Wisp spawn(Object world) {
        return new Wisp();
    }

    public void onDeath(Object server) {
        Datapack.run(server, "wisp_death");
    }
}
"""
DATAPACK = """package com.glow.util;

public final class Datapack {
    public static void run(Object server, String name) {
        System.out.println("glow:" + name);
    }
}
"""
DEATH = """# Runs where a wisp died: drops its heart.
summon minecraft:item ~ ~ ~ {Item:{id:"glow:wisp_heart",count:1}}
advancement grant @a[distance=..8] only glow:wisp_slain
"""


def _write(root: Path, rel: str, text: str | bytes) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))


def _scan(root: Path) -> index.Graph:
    workflow.init(root)
    st = open_store(root)
    try:
        workflow.scan(st, root)
    finally:
        st.close()
    search_index._HANDLES.clear()
    return index.load(root)


def _mod(root: Path) -> None:
    _write(root, RES + "fabric.mod.json", '{"id": "glow"}\n')
    _write(root, "src/main/java/com/glow/entity/Wisp.java", WISP)
    _write(root, "src/main/java/com/glow/util/Datapack.java", DATAPACK)
    _write(root, RES + "data/glow/function/wisp_death.mcfunction", DEATH)
    _write(root, "datapack/pack.mcmeta", '{"pack": {"pack_format": 48}}\n')
    _write(root, "datapack/data/glow/function/wisp_death.mcfunction", DEATH)  # the same pack shipped twice
    _write(root, RES + "data/glow/advancement/wisp_slain.json", '{"criteria": {}}\n')
    _write(root, RES + "assets/glow/models/item/wisp_heart.json",
           '{"parent": "minecraft:item/generated", "textures": {"layer0": "glow:item/wisp_heart"}}\n')
    _write(root, RES + "glow.yml", "# Glow settings\nrepair:\n  blocks_per_tick: 8\nspawn:\n  max_wisps: 4\n")


@pytest.fixture(scope="module")
def mod(tmp_path_factory):
    root = tmp_path_factory.mktemp("didx") / "mod"
    _mod(root)
    _write(root, "config/credentials.json", '{"payment_api_key": "sk_live_SECRET", "password": "hunter2"}\n')
    _write(root, ".graphifyignore", "vendor/\n")
    _write(root, "vendor/legacy_payment.py", "def charge_legacy_gateway():\n    return 1\n")
    _write(root, "target/classes/settings.json", '{"wisp": true}\n')   # build output the graph skips
    _write(root, "benchmarks/results/raw/run1.txt", "wisp death heart drop " * 20)
    _write(root, "exports/big.json", json.dumps({"rows": [{"wisp": i} for i in range(6000)]}, indent=1))
    _write(root, RES + "assets/glow/textures/item/wisp_heart.png", b"\x89PNG\r\n")
    _write(root, "config/server.toml", "[server]\nport = 1\n\n[[mods]]  # one per mod\nmodId = 'glow'\n")
    _write(root, "bom.yml", "﻿server:\n  port: 1\nclient:\n  port: 2\n")
    return _scan(root)


def _db(g) -> sqlite3.Connection:
    return sqlite3.connect(search_index.db_path_for(g))


def test_data_files_become_units_split_by_section(mod):
    conn = _db(mod)
    units = conn.execute("SELECT file, name, a, b FROM units WHERE kind = 'data' ORDER BY file, a").fetchall()
    names = {(f, n) for f, n, _a, _b in units}
    assert (RES + "glow.yml", "repair") in names and (RES + "glow.yml", "spawn") in names
    assert ("bom.yml", "server") in names and ("bom.yml", "client") in names  # the BOM does not hide section 1
    assert ("config/server.toml", "mods") in names  # [[array tables]] (with a comment) start a section too
    assert (RES + "data/glow/function/wisp_death.mcfunction", "glow:wisp_death") in names
    doc = conn.execute("SELECT doc FROM units WHERE file = ?", (RES + "data/glow/function/wisp_death.mcfunction",)
                       ).fetchone()[0]
    assert doc.startswith("Runs where a wisp died")


def test_secrets_ignored_dependency_generated_and_bulk_files_are_left_out_with_a_reason(mod):
    conn = _db(mod)
    indexed = {f for (f,) in conn.execute("SELECT file FROM files WHERE skipped IS NULL")}
    skipped = dict(conn.execute("SELECT file, reason FROM unindexed"))
    for f, why in (("config/credentials.json", "may hold secrets"), ("vendor/legacy_payment.py", ".graphifyignore"),
                   ("target/classes/settings.json", "dependency or output folder"),
                   ("benchmarks/results/raw/run1.txt", "generated output"),
                   ("exports/big.json", "large data file"), (RES + "assets/glow/textures/item/wisp_heart.png", "binary")):
        assert f not in indexed and why in skipped.get(f, ""), (f, skipped.get(f))
    text = "\n".join(t for (t,) in conn.execute("SELECT text FROM passages")) if _has_text(conn) else ""
    assert "sk_live_SECRET" not in text
    out = _render(mod, "payment api key password")
    assert "sk_live" not in out and "hunter2" not in out


def _has_text(conn) -> bool:
    return any(r[1] == "text" for r in conn.execute("PRAGMA table_info(passages)"))


def _render(g, q: str) -> str:
    from verinoda import retrieval

    return retrieval.render_text(retrieval.retrieve(g, q), 6000)


def test_links_carry_how_sure_they_are(mod):
    conn = _db(mod)
    rows = {(f, t, form, sure) for f, _l, t, _rid, form, sure in conn.execute("SELECT * FROM refs")}
    wisp = "src/main/java/com/glow/entity/Wisp.java"
    assert (wisp, RES + "data/glow/function/wisp_death.mcfunction", "bare", 0) in rows  # namespace assumed
    assert (RES + "data/glow/function/wisp_death.mcfunction", RES + "data/glow/advancement/wisp_slain.json",
            "id", 1) in rows  # `advancement grant ... only glow:x` names an advancement
    h = search_index.open_for(mod)
    lk = search_index.describe_links(h, RES + "data/glow/function/wisp_death.mcfunction", 1, 3)
    assert lk["named_by"][0]["file"] == wisp and lk["named_by"][0]["sure"] is False
    assert lk["copies"] == ["datapack/data/glow/function/wisp_death.mcfunction"]
    out = _render(mod, "what happens on wisp death onDeath")
    assert "bare name, inferred" in out


def test_identical_data_files_rank_once_as_the_source_set_copy(mod):
    hits = search_index.rank(mod, "wisp died drops its heart").hits
    files = [h.file for h in hits]
    assert RES + "data/glow/function/wisp_death.mcfunction" in files
    assert "datapack/data/glow/function/wisp_death.mcfunction" not in files


def test_identical_code_files_both_stay_and_a_test_copy_never_hides_the_product_copy(tmp_path):
    root = tmp_path / "copies"
    _write(root, "pkg/core.py", "def apply_discount(total):\n    return total * 0.9\n")
    _write(root, "benchmarks/snapshot/pkg/core.py", "def apply_discount(total):\n    return total * 0.9\n")
    _write(root, "src/gametest/resources/widget_settings.json", '{"refresh_interval": 5, "theme": "dark"}\n')
    _write(root, "src/main/resources/widget_settings.json", '{"refresh_interval": 5, "theme": "dark"}\n')
    g = _scan(root)
    files = {h.file for h in search_index.rank(g, "apply_discount").hits}
    assert {"pkg/core.py", "benchmarks/snapshot/pkg/core.py"} <= files
    for inc in (True, False):
        got = [h.file for h in search_index.rank(g, "widget refresh interval theme", include_tests=inc).hits]
        assert "src/main/resources/widget_settings.json" in got, inc
        assert "src/gametest/resources/widget_settings.json" not in got, inc


def test_reference_trees_rank_lower_unless_the_question_names_them(tmp_path):
    root = tmp_path / "ref"
    _write(root, "src/wisp.py", "def spawn_wisp(world):\n    return world\n")
    _write(root, "legacy-source/plugin/wisp.py", "def spawn_wisp(world):\n    # the original\n    return world\n")
    g = _scan(root)
    cfg = root / ".verinoda" / "config.json"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data["index"] = {"reference": [{"path": "legacy-source/", "aliases": ["orijinal", "İlk"]}]}
    cfg.write_text(json.dumps(data), encoding="utf-8")

    def top(q):
        hits = search_index.rank(g, q).hits
        return hits[0].file, next(h for h in hits if h.file.startswith("legacy-source/"))

    first, ref = top("spawn_wisp")
    assert first == "src/wisp.py" and any("reference tree" in r for r in ref.reasons)
    assert ref.score < search_index.rank(g, "spawn_wisp").hits[0].score
    # named: an alias (also as a stem with a suffix, and with a dotted capital I), or the folder name
    for q in ("orijinaldeki spawn_wisp", "ilk spawn_wisp", "legacy-source spawn_wisp"):
        roots = search_index._reference_roots(root, search_index.analyze_query(q, _db(g)), q)
        assert roots == (), q
    # not named: a word that only shares a prefix with the folder or an alias
    for q in ("legacy spawn_wisp", "orijin spawn_wisp"):
        assert search_index._reference_roots(root, search_index.analyze_query(q, _db(g)), q), q


def test_incremental_update_with_data_files_equals_a_rebuild(tmp_path):
    root = tmp_path / "inc"
    _mod(root)
    g = _scan(root)
    db = search_index.db_path_for(g)
    _write(root, RES + "data/glow/function/wisp_slain.mcfunction", "say slain\n")   # a new id: refs re-resolved
    _write(root, RES + "glow.yml", "repair:\n  blocks_per_tick: 16\n")
    (root / RES / "data/glow/advancement/wisp_slain.json").unlink()
    search_index.update(root, g)
    fresh = tmp_path / "fresh.db"
    search_index.update(root, g, rebuild=True, db=fresh)
    assert search_index.dump(db) == search_index.dump(fresh)
    q = "SELECT file, line, target, rid, form, sure FROM refs ORDER BY 1, 2, 3"
    assert sqlite3.connect(db).execute(q).fetchall() == sqlite3.connect(fresh).execute(q).fetchall()


def test_the_in_memory_fallback_keeps_data_files_and_links(tmp_path, monkeypatch):
    root = tmp_path / "ro"
    _mod(root)
    g = _scan(root)
    search_index.db_path_for(g).unlink()
    search_index._HANDLES.clear()

    def refuse(*a, **k):
        raise PermissionError("read-only")

    monkeypatch.setattr(search_index, "update", refuse)
    h = search_index.open_for(g)
    assert h.memory is not None
    kinds = {r[2] for r in h.units.values()}
    assert "data" in kinds
    assert h.memory.execute("SELECT COUNT(*) FROM refs").fetchone()[0] > 0
    search_index._HANDLES.clear()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True, stdin=subprocess.DEVNULL)


def test_list_files_keeps_tracked_build_folders_and_drops_untracked_build_output(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    root = tmp_path / "git"
    _write(root, "src/main/java/tr/mod/build/Yapi.java", "class Yapi {}\n")   # a Java package named build
    _write(root, "pkg/core.py", "x = 1\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    _write(root, "build/lib/pkg/core.py", "x = 1\n")   # setuptools' in-tree copy, not ignored, not tracked
    files = snapshot.list_files(root)
    assert "src/main/java/tr/mod/build/Yapi.java" in files and "build/lib/pkg/core.py" not in files


def test_command_names_reach_their_handlers(tmp_path):
    root = tmp_path / "cli"
    _write(root, "tool/cli.py", "def cmd_scan(args):\n    return 1\n\n\ndef cmd_init(args):\n    return 2\n\n\n"
                                "def scan_files(root):\n    return []\n")
    g = _scan(root)
    assert search_index.command_words("do the init and scan commands refuse the home directory?") == ["init", "scan"]
    assert search_index.command_words("scan komutu ne yapar?") == ["scan"]
    assert search_index.command_words("what does `tool scan .` print?") == ["scan"]
    assert search_index.command_words("which command writes it?") == []
    q = search_index.analyze_query("does the scan command refuse the home directory?", _db(g))
    assert "cmd_scan" in q.named and any(e["via"] == "command handler" for e in q.expansions)
    assert search_index.rank(g, "does the scan command refuse the home directory?").hits[0].name == "cmd_scan"


def test_java_calls_the_extractor_drops_are_resolved_by_imports(tmp_path):
    root = tmp_path / "java"
    _mod(root)
    # a second Wisp (the original plugin kept for reference) makes `Wisp.spawn` ambiguous by name
    _write(root, "reference/plugin/src/com/old/Wisp.java",
           "package com.old;\n\npublic class Wisp {\n    public static Wisp spawn(Object w) {\n        return null;\n"
           "    }\n}\n")
    _write(root, "src/main/java/com/glow/command/Commands.java", """package com.glow.command;

import com.glow.entity.Wisp;
import com.other.Helper;

public class Commands {
    private Wisp last;

    public void summon(Object world) {
        // Wisp.spawn(world) in a comment is not a call
        String s = "Wisp.spawn(world)";
        Wisp w = Wisp.spawn(world);
        w.onDeath(world);
        last.onDeath(world);
        Helper.spawn(world);
    }
}
""")
    _scan(root)
    g = index.load(root, augment=False)   # the extractor's graph alone (load() applies the edges)
    edges = {(g.label(u), g.file(v), d["source_location"], d["context"]) for u, v, d in index.java_call_edges(g)}
    mine = "src/main/java/com/glow/entity/Wisp.java"
    assert (".summon()", mine, "L12", "Wisp.spawn()") in edges          # the imported Wisp, not com.old's
    assert (".summon()", mine, "L13", "w.onDeath() on Wisp") in edges  # a typed local
    assert not any(f.startswith("reference/") for _l, f, _at, _c in edges if _l == ".summon()")
    assert {at for lab, _f, at, _c in edges if lab == ".summon()"} == {"L12", "L13"}  # one edge per target
    # the edges are applied on load and graded as calls through the import
    g2 = index.load(root)
    assert any(d.get("_origin") == index.JAVA_CALL_ORIGIN for _u, _v, d in g2.G.edges(data=True))


def test_generic_data_files_count_less_than_code_unless_the_question_names_them(tmp_path):
    root = tmp_path / "dict"
    _write(root, "app/experiments.py", 'ENV_ALLOW = {"PATH", "HOME"}\n\n\ndef run_command(cmd):\n'
                                       '    """Run an experiment command with the allowed environment variables."""\n'
                                       '    return {k: 1 for k in ENV_ALLOW}\n')
    words = ["environment", "variables", "experiment", "command", "passed", "decided"]
    _write(root, "app/data/glossary.json", json.dumps({f"w{i}": " ".join(words) for i in range(40)}, indent=1))
    _write(root, "config/app.yml", "experiment:\n  environment: variables command\n")
    g = _scan(root)
    hits = search_index.rank(g, "which environment variables are passed to experiment commands?").hits
    order = [h.file for h in hits]
    assert order.index("app/experiments.py") < order.index("app/data/glossary.json")
    gl = next(h for h in hits if h.file == "app/data/glossary.json")
    named = next(h for h in search_index.rank(g, "glossary environment variables experiment command").hits
                 if h.file == "app/data/glossary.json")
    assert named.lex > gl.lex  # naming the file lifts the factor
    assert search_index.CONFIG_SUFFIXES and "config/app.yml" in order  # configs are not reduced


def test_analyze_follows_code_that_names_a_data_function_to_its_loader_and_callers(tmp_path):
    from verinoda import analysis

    root = tmp_path / "chain"
    _mod(root)
    _write(root, "src/main/java/com/glow/ritual/Ritual.java", """package com.glow.ritual;

import com.glow.util.Datapack;

public final class Ritual {
    public static void begin(Object server) {
        Datapack.run(server, "glow:ritual/start");
    }
}
""")
    _write(root, "src/main/java/com/glow/command/Commands.java", """package com.glow.command;

import com.glow.ritual.Ritual;

public final class Commands {
    public static int ritual(Object server) {
        Ritual.begin(server);
        return 1;
    }
}
""")
    _write(root, RES + "data/glow/function/ritual/start.mcfunction", "# Starts the ritual at the altar.\nsay ritual\n")
    _scan(root)
    st = open_store(root)
    try:
        res = analysis.analyze(st, root, "Which Java code runs the glow:ritual/start data pack function?")
    finally:
        st.close()
    texts = [c["text"] for c in res["claims"]]
    assert any(t.startswith("`begin` names `glow:ritual/start`") for t in texts), texts
    assert any("`Ritual.begin()` calls `Datapack.run()`" in t for t in texts), texts        # the loader
    assert any("`Commands.ritual()` calls `Ritual.begin()`" in t for t in texts), texts      # how it is reached
    chain = [c for c in res["claims"] if "calls `Ritual.begin()`" in c["text"]]
    assert chain[0]["status"] == "statically_verified"  # the import binds Ritual: a verified call


def test_the_stem_of_an_inflected_turkish_word_is_searched_even_when_the_form_is_known(tmp_path):
    root = tmp_path / "stem"
    _write(root, "app/geometri.py", "class GeometriModeli:\n    pass\n")
    _write(root, "app/model.py", "def model_yukle(yol):\n    return yol\n")
    g = _scan(root)
    q = search_index.analyze_query("Eşyanın modeli nerede yükleniyor?", _db(g))
    exp = {(e["from"], e["to"]): e["weight"] for e in q.expansions}
    assert exp.get(("modeli", "model")) == search_index.EXPANSION_WEIGHT  # searched although "modeli" is a known word


def test_a_data_file_takes_graph_prior_through_the_code_that_names_it(mod):
    rk = search_index.rank(mod, "what does onDeath do when a wisp dies")
    death = next((h for h in rk.hits if h.file == RES + "data/glow/function/wisp_death.mcfunction"), None)
    assert death is not None and any("graph prior through the link" in r for r in death.reasons)


def test_plan_glosses_get_the_exact_spelling_seed_entries(mod):
    # the question plan reads folded words and cannot tell "öl" (die) from "ol" (be)
    q = search_index.analyze_query("Bir wisp öldüğünde ne oluyor?", _db(mod), expansions={"wisp": ["wisp"]},
                                   repo=mod.root)
    got = {(e["from"], e["to"]) for e in q.expansions}
    assert ("oldugunde", "death") in got and not any(e["from"] == "oluyor" for e in q.expansions)


def test_a_derivational_ending_is_not_stripped_to_reach_a_stem(tmp_path):
    root = tmp_path / "deriv"
    _write(root, "app/oyun.py", "def oyun_baslat():\n    return 1\n\n\ndef kanat_ac(kanatlar):\n    return kanatlar\n")
    g = _scan(root)
    exp = {(e["from"], e["to"]) for e in search_index.analyze_query("Oyuncu kanatları nasıl açıyor?", _db(g)).expansions}
    assert ("oyuncu", "oyun") not in exp and ("Oyuncu", "oyun") not in exp   # oyuncu (player) is not oyun (game)
    assert any(src.lower().startswith("kanat") and to == "kanatlar" for src, to in exp)  # inflection is stripped


def test_kotlin_calls_into_java_classes_are_resolved_by_imports(tmp_path):
    root = tmp_path / "kt"
    _mod(root)
    _write(root, "src/main/kotlin/com/glow/command/Commands.kt", """package com.glow.command

import com.glow.entity.Wisp
import com.glow.util.Datapack

object Commands {
    fun register(world: Any) {
        val w: Wisp = Wisp.spawn(world)
        run { Datapack.run(world, "ritual") }
        w.onDeath(world)
    }
}
""")
    _scan(root)
    g = index.load(root, augment=False)
    edges = {(g.label(u), g.label(v), d["source_location"]) for u, v, d in index.java_call_edges(g)
             if g.file(u).endswith(".kt")}
    assert (".register()", ".spawn()", "L8") in edges       # a class-qualified call bound by the import
    assert (".register()", ".onDeath()", "L10") in edges   # a typed Kotlin local
    full = index.load(root)  # with the extractor's own edges: the unique Datapack.run was already there
    calls = {(full.label(u), full.label(v)) for u, v, d in full.G.edges(data=True) if d.get("relation") == "calls"
             and (full.file(u) or "").endswith(".kt")}
    assert (".register()", ".run()") in calls


def test_kotlin_raw_strings_properties_and_commented_imports(tmp_path):
    root = tmp_path / "kt2"
    _mod(root)
    _write(root, "src/main/java/com/other/Wisp.java", "package com.other;\n\npublic class Wisp {\n"
           "    public static Wisp spawn(Object w) { return new Wisp(); }\n    public void onDeath(Object w) { }\n}\n")
    _write(root, "src/main/java/com/other/Caller.java", """package com.other;

import com.glow.entity.Wisp; // NOPMD: the glow variant, not this package's

public class Caller {
    public void go(Object w) {
        Wisp.spawn(w);
    }
}
""")
    _write(root, "src/main/kotlin/com/glow/Service.kt", '''package com.glow

import com.glow.entity.Wisp

class Service(private val wisp: Wisp) {
    private val backup: Wisp = Wisp.spawn(null)

    fun help(): String = """
        Killing it calls Wisp.spawn(world) again
    """

    fun kill(world: Any) {
        wisp.onDeath(world)
        backup.onDeath(world)
    }
}
''')
    _scan(root)
    g = index.load(root, augment=False)
    edges = {(g.file(u).rsplit("/", 1)[-1], g.label(u), g.label(v), g.file(v)) for u, v, _d in index.java_call_edges(g)}
    # the import with a trailing comment shadows the same-package class
    assert ("Caller.java", ".go()", ".spawn()", "src/main/java/com/glow/entity/Wisp.java") in edges
    assert not any(e[3] == "src/main/java/com/other/Wisp.java" for e in edges if e[0] == "Caller.java")
    kt = {(e[1], e[2]) for e in edges if e[0] == "Service.kt"}
    assert (".kill()", ".onDeath()") in kt                   # through constructor and class properties
    assert (".help()", ".spawn()") not in kt                  # text inside a raw string is not a call


def test_english_words_keep_their_abbreviations_and_turkish_stems_are_long_enough(tmp_path):
    root = tmp_path / "abbr"
    _write(root, "app/factory.py", "def create_app():\n    return 1\n\ndef create_user():\n    return 2\n\n"
                                   "def dedup_rows(rows):\n    return rows\n\ndef sort_rows(rows):\n    return rows\n")
    _write(root, "app/calendar_utils.py", "def cal_days():\n    return 1\n\ndef cal_weeks():\n    return 2\n")
    _write(root, "app/auth.py", "def login(user):\n    return user\n\ndef log_event(e):\n    return e\n")
    _write(root, "app/payments.py", "def process_payment(p):\n    return p\n\ndef refund_payment(p):\n    return p\n")
    g = _scan(root)

    def exps(q):
        return {(e["from"], e["to"], e["via"]) for e in search_index.analyze_query(q, _db(g)).expansions}

    assert ("application", "app", "corpus prefix") in {(f.lower(), t, v) for f, t, v in exps(
        "Where is the application created?")} or any(t == "app" for _f, t, _v in exps("Where is the application created?"))
    assert any(t == "dedup" for _f, t, _v in exps("Which function deduplicates rows?"))
    assert not any(t == "cal" for _f, t, _v in exps("Ödeme nasıl çalışıyor?"))        # çalışıyor is not cal
    assert not any(t == "log" for _f, t, _v in exps("login nasıl çalışıyor?"))        # login is not log + in
    assert search_index.rank(g, "Ödeme nasıl çalışıyor?").hits[0].name in ("process_payment", "refund_payment")


def test_a_resource_id_the_question_spells_finds_every_file_that_writes_it(tmp_path):
    root = tmp_path / "rid"
    _mod(root)
    for name, typ in (("ember_ingot", "glow:forging"), ("wisp_lamp", "glow:forging"), ("ash", "minecraft:smelting")):
        _write(root, RES + f"data/glow/recipe/{name}.json",
               '{\n  "type": "%s",\n  "ingredient": {"item": "glow:wisp_heart"},\n  "result": {"id": "glow:%s"}\n}\n'
               % (typ, name))
    _write(root, "src/main/java/com/glow/forge/ForgeBench.java",
           "package com.glow.forge;\n\npublic class ForgeBench {\n    public void forgeAll() { }\n}\n")
    g = _scan(root)
    hits = search_index.rank(g, "Which recipes use glow:forging?").hits
    named = {h.file.rsplit("/", 1)[-1] for h in hits if "question names glow:forging" in h.reasons}
    assert named == {"ember_ingot.json", "wisp_lamp.json"}
    assert named <= {h.file.rsplit("/", 1)[-1] for h in hits[:4]}  # as high as the best lexical match
    # not a resource namespace of this project: "retry:3" is text, not an id
    assert not any("question names" in r for h in search_index.rank(g, "what does retry:3 mean").hits
                   for r in h.reasons)
