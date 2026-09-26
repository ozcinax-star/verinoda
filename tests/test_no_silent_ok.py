"""Never "ok" for what was not looked at (the senior evaluation of 2026-09-25, gaps 3, 4, 11 and 13).

Every test here starts from an evaluator's repro: a decide guard that checked no file, edge or manifest; a
decisions folder a fresh CI clone cannot see; `check` on Java or TypeScript; a broad ``except Exception``
taken for a guard; a config or relation claim outside Python reaching ``statically_verified``. Runs on
copies of the examples (never on examples/ itself).
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import codecheck, entail, guards, workflow  # noqa: E402
from verinoda import decisions as dm  # noqa: E402
from verinoda import evidence as evmod  # noqa: E402
from verinoda.store import open_store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ORDERS = ROOT / "examples" / "orders_app"
GLOW = ROOT / "examples" / "glow_mod"
LANTERN = "src/main/java/com/example/glowmod/event/LanternEvents.java"
GLOW_CLIENT = "src/client/java/com/example/glowmod/client/GlowModClient.java"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def _copy(src: Path, dst: Path, *, scan: bool = False) -> Path:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", ".pytest_cache",
                                                            "*.db"))
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    if scan:
        workflow.init(dst)
        st = open_store(dst)
        try:
            workflow.scan(st, dst)
        finally:
            st.close()
    return dst


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _rec(repo: Path, *specs: str) -> dm.Decision:
    return dm.Decision(id="ADR-0009", number=9, title="t",
                       guards=[dm.parse_guard(s, repo, f"g{i}") for i, s in enumerate(specs, 1)])


def _at(items) -> list[str]:
    return sorted(f["at"] for f in items)


@pytest.fixture(scope="module")
def glow_indexed(tmp_path_factory):
    return _copy(GLOW, tmp_path_factory.mktemp("nso") / "glow", scan=True)


# -- decide: the scope of an only_in guard ---------------------------------------------------------------

def test_sample_folders_count_only_above_a_source_root(tmp_path):
    """A Java package path such as com/example is not an example folder (the Fabric template's package)."""
    _write(tmp_path, "src/com/example/Plain.java", "// header\npackage com.example;\n\nclass Plain {}\n")
    _write(tmp_path, "samples/Loose.java", "class Loose {}\n")
    _write(tmp_path, "examples/demo/Other.kt", "package other.pkg\n")
    cases = {
        "src/main/java/com/example/glowmod/event/LanternEvents.java": set(),
        "app/src/main/kotlin/org/sample/demo/Screen.kt": set(),
        "examples/demo-mod/src/main/java/com/example/Mod.java": {"examples"},
        "src/com/example/Plain.java": set(),               # the package line names com/example
        "samples/Loose.java": {"samples"},                  # no package line: the folder is a sample folder
        "examples/demo/Other.kt": {"examples", "demo"},     # a package that is not the folder path
        "vendor/lib/util.py": {"vendor"},
    }
    for rel, want in cases.items():
        assert guards.not_product_dirs(tmp_path, rel) == want, rel


def test_only_in_checks_a_java_package_named_example_and_says_what_it_checked(tmp_path):
    """JVM persona: an injected MinecraftClient.getInstance() in common code gave `ok` (checked: [])."""
    repo = _copy(GLOW, tmp_path / "glow")
    spec = "only_in calls=net.minecraft.client.MinecraftClient.getInstance allowed=src/client/**"
    clean = guards.check(repo, records=[_rec(repo, spec)])
    (ok,) = clean["ok"]
    assert clean["exit"] == 0 and clean["status"] == "ok" and ok["scope"]["jvm"] >= 10  # the product's files
    assert not any("example, sample" in lim for lim in ok["limits"])
    # the evaluator's inject_client.py
    p = repo / LANTERN
    s = p.read_text(encoding="utf-8")
    s = s.replace("import net.minecraft.block.Blocks;",
                  "import net.minecraft.block.Blocks;\nimport net.minecraft.client.MinecraftClient;\n"
                  "import com.example.glowmod.client.GlowModClient;")
    s = s.replace("            Wisp.spawn((ServerWorld) world, pos.up());",
                  "            Wisp.spawn((ServerWorld) world, pos.up());\n"
                  "            MinecraftClient.getInstance().inGameHud.setOverlayMessage(null, false);\n"
                  "            GlowModClient.flash();")
    p.write_bytes(s.encode("utf-8"))
    res = guards.check(repo, records=[_rec(repo, spec)])
    assert _at(res["violations"]) == [f"{LANTERN}:33"] and res["exit"] == 1
    assert res["violations"][0]["status"] == "statically_verified"


def test_only_in_with_nothing_in_scope_is_unknown_never_ok(tmp_path):
    repo = tmp_path / "r"
    _write(repo, "orders/repository.py", "import sqlite3\n\n\ndef c():\n    return sqlite3.connect('x')\n")
    _write(repo, "tests/test_repo.py", "import sqlite3\n")
    res = guards.check(repo, records=[_rec(repo, "only_in calls=sqlite3.connect allowed=orders/repository.py")])
    assert res["status"] == "unknown" and res["exit"] == 3 and not res["ok"]
    assert "no file was checked" in res["unknown"][0]["why"] and "test file" in res["unknown"][0]["why"]


# -- decide: no_edge ----------------------------------------------------------------------------------

def test_no_edge_to_an_external_package_or_to_nothing_is_unknown(glow_indexed):
    """JVM persona: `no_edge from=src/main/** to=net.minecraft.client.**` said `ok (edges 0)` while common code
    imports that package: external classes are not nodes, so nothing was compared."""
    from verinoda import index

    g = index.load(glow_indexed)
    res = guards.check(glow_indexed, graph=g, records=[_rec(glow_indexed,
                                                            "no_edge from=src/main/** to=net.minecraft.client.**")])
    assert res["status"] == "unknown" and res["exit"] == 3 and not res["ok"]
    why = res["unknown"][0]["why"]
    assert "to=net.minecraft.client.** matches no file in the index" in why and "only_in calls=" in why
    res = guards.check(glow_indexed, graph=g, records=[_rec(glow_indexed, "no_edge from=srcx/** to=src/client/**")])
    assert res["exit"] == 3 and "from=srcx/** matches no file" in res["unknown"][0]["why"]
    ok = guards.check(glow_indexed, graph=g, records=[_rec(glow_indexed, "no_edge from=src/main/** to=src/client/**")])
    scope = ok["ok"][0]["scope"]
    assert ok["exit"] == 0 and scope["from_files"] > 5 and scope["to_files"] == 1 and scope["edges_checked"] > 0


# -- decide: dependency guards ----------------------------------------------------------------------------

LIBS = """[versions]
minecraft = "1.21.1"
fabric-loader = "0.16.5"
fabric-api = "0.105.0+1.21.1"
snakeyaml = "2.2"

[libraries]
minecraft = { module = "com.mojang:minecraft", version.ref = "minecraft" }
fabric-loader = { module = "net.fabricmc:fabric-loader", version.ref = "fabric-loader" }
fabric-api = { module = "net.fabricmc.fabric-api:fabric-api", version.ref = "fabric-api" }
snakeyaml = { module = "org.yaml:snakeyaml", version.ref = "snakeyaml" }

[plugins]
loom = { id = "fabric-loom", version = "1.7-SNAPSHOT" }
"""
KTS = """plugins {
    alias(libs.plugins.loom)
}

dependencies {
    minecraft(libs.minecraft)
    mappings("net.fabricmc:yarn:1.21.1+build.3:v2")
    modImplementation(libs.fabric.loader)
    modImplementation(libs.fabric.api)
    implementation(libs.snakeyaml)
    include(libs.snakeyaml)
}
"""


def test_dependency_guard_reads_version_catalogs_and_cites_the_bundling_line(tmp_path):
    """JVM persona (glow_kts): build.gradle.kts + libs.versions.toml gave `ok (manifests 0)` for absent=snakeyaml;
    on the plain build.gradle the `include` line that ships the library was not cited."""
    repo = _copy(GLOW, tmp_path / "kts")
    (repo / "build.gradle").unlink()
    _write(repo, "build.gradle.kts", KTS)
    _write(repo, "gradle/libs.versions.toml", LIBS)
    specs = ("dependency absent=snakeyaml", "dependency absent=org.yaml:snakeyaml", "dependency present=fabric-api")
    res = guards.check(repo, records=[_rec(repo, *specs)])
    by = {}
    for v in res["violations"]:
        by.setdefault(v["guard"], []).append(v["at"])
    assert by == {"g1": ["build.gradle.kts:10", "build.gradle.kts:11"], "g2": ["build.gradle.kts:10",
                                                                              "build.gradle.kts:11"]}
    assert "through the version catalog (gradle/libs.versions.toml:11)" in res["violations"][0]["why"]
    assert "(include)" in res["violations"][1]["why"]
    (ok,) = res["ok"]
    assert ok["guard"] == "g3" and ok["scope"]["manifests"] == 3  # the catalog, settings.gradle, the build
    plain = _copy(GLOW, tmp_path / "plain")
    res = guards.check(plain, records=[_rec(plain, "dependency absent=snakeyaml")])
    assert _at(res["violations"]) == ["build.gradle:51", "build.gradle:52"] and "(include)" in \
        res["violations"][1]["why"]


def test_dependency_guard_reads_workspace_packages(tmp_path):
    """Web persona (mono): axios added to apps/web/package.json passed `absent=axios` (only the root read)."""
    repo = tmp_path / "mono"
    _write(repo, "package.json", json.dumps({"name": "acme-monorepo", "private": True,
                                             "devDependencies": {"typescript": "^5.6.0"}}, indent=2) + "\n")
    _write(repo, "pnpm-workspace.yaml", 'packages:\n  - "apps/*"\n  - "packages/*"\n')
    web = {"name": "@acme/web", "dependencies": {"@acme/shared": "workspace:*", "axios": "^1.7.0",
                                                 "react": "^18.3.1"}}
    _write(repo, "apps/web/package.json", json.dumps(web, indent=2) + "\n")
    _write(repo, "packages/shared/package.json", json.dumps({"name": "@acme/shared"}, indent=2) + "\n")
    _write(repo, "tools/scratch/package.json", json.dumps({"dependencies": {"left-pad": "1"}}) + "\n")
    _git(repo, "init", "-q")
    res = guards.check(repo, records=[_rec(repo, "dependency absent=axios", "dependency absent=left-pad")])
    assert _at(res["violations"]) == ["apps/web/package.json:5"] and res["exit"] == 1
    lim = res["violations"][0]["limits"]
    assert any("apps/web/package.json, packages/shared/package.json" in x for x in lim)
    # a manifest that is neither the root's nor a workspace package is not read, and the ok says so
    ok = next(o for o in res["ok"] if o["guard"] == "g2")
    assert any("other manifest(s) are not read" in x and "tools/scratch/package.json" in x for x in ok["limits"])
    # package.json "workspaces" (npm, yarn) as well
    (repo / "pnpm-workspace.yaml").unlink()
    _write(repo, "package.json", json.dumps({"name": "m", "workspaces": ["apps/*"]}) + "\n")
    assert _at(guards.check(repo, records=[_rec(repo, "dependency absent=axios")])["violations"]) == \
        ["apps/web/package.json:5"]


def test_a_dependency_guard_that_read_no_manifest_is_unknown(tmp_path):
    repo = tmp_path / "bare"
    _write(repo, "app.py", "print(1)\n")
    res = guards.check(repo, records=[_rec(repo, "dependency absent=psycopg")])
    assert res["status"] == "unknown" and res["exit"] == 3 and not res["ok"]
    assert "no manifest or build file was read" in res["unknown"][0]["why"]


# -- decide: where the records live -------------------------------------------------------------------------

ADR2 = """---
verinoda-decision: 1
id: ADR-0002
title: DB access only in repository
status: accepted
decided-by: human
date: 2026-09-25
chosen: sqlite via repository only
brief: null
source: null
supersedes: null
superseded-by: null
governs: []
guards: [{"id": "g1", "kind": "only_in", "status": "accepted", "spec": "only_in calls=sqlite3.connect allowed=orders/repository.py", "calls": ["sqlite3.connect"], "allowed": ["orders/repository.py"], "scope": "product"}]
revisit-when: []
waivers: []
---
# ADR-0002: DB access only in repository
"""


def test_a_fresh_clone_finds_committed_records_or_fails_loudly(tmp_path, capsys):
    """Lead persona: with decisions.dir only in the git-ignored .verinoda/config.json, a fresh CI clone gave
    '0 violated (0 decision record(s))', exit 0, over a committed violation."""
    from verinoda import cli

    repo = _copy(ORDERS, tmp_path / "orders")
    _write(repo, "docs/decisions/ADR-0002-db-access-only-in-repository.md", ADR2)
    svc = repo / "orders/service.py"
    svc.write_bytes(svc.read_bytes() + b"\n\nimport sqlite3 as _db\n\n\ndef audit(path):\n    conn = _db.connect(path)\n"
                                       b"    return conn\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "violation")
    clone = tmp_path / "orders_ci"
    _git(tmp_path, "clone", "-q", str(repo), str(clone))
    capsys.readouterr()
    assert cli.main(["decide", "check", "--repo", str(clone), "--json"]) == 3
    res = json.loads(capsys.readouterr().out)
    assert res["status"] == "unknown" and res["decisions"] == 0
    (u,) = res["unknown"]
    assert u["adr_like"] == ["docs/adr/0001-sqlite-persistence.md", "docs/decisions/ADR-0002-db-access-only-in-"
                                                                     "repository.md"]
    assert "verinoda.toml" in u["why"] and "--decisions-dir" in u["why"]
    # the flag, a committed verinoda.toml, or [tool.verinoda.decisions] in pyproject.toml
    assert cli.main(["decide", "check", "--repo", str(clone), "--decisions-dir", "docs/decisions"]) == 1
    out = capsys.readouterr().out
    assert "VIOLATED ADR-0002 g1" in out and "in docs/decisions" in out
    listed = dm.listing(None, clone, "docs/decisions")  # `decide list --decisions-dir` (it needs a scan)
    assert [d["id"] for d in listed["decisions"]] == ["ADR-0002"] and listed["dir_from"] == "--decisions-dir"
    _write(repo, "verinoda.toml", '[decisions]\ndir = "docs/decisions"\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "config")
    _git(clone, "pull", "-q")
    res = guards.check(clone)
    assert res["exit"] == 1 and _at(res["violations"]) == ["orders/service.py:33"]
    assert res["decisions_dir"] == {"path": "docs/decisions", "from": "verinoda.toml [decisions] dir"}
    (clone / "verinoda.toml").unlink()
    pp = clone / "pyproject.toml"
    pp.write_bytes(pp.read_bytes() + b'\n[tool.verinoda.decisions]\ndir = "docs/decisions"\n')
    assert guards.check(clone)["exit"] == 1
    assert dm.decisions_dir_source(clone)[1] == "pyproject.toml [tool.verinoda.decisions] dir"
    # never a folder outside the repository, never an option
    assert cli.main(["decide", "check", "--repo", str(clone), "--decisions-dir", "../elsewhere"]) == 2
    assert "outside the repository" in capsys.readouterr().err
    with pytest.raises(dm.DecisionError):
        dm.decisions_dir(clone, "-x")


def test_no_records_and_no_adr_like_file_is_still_ok(tmp_path):
    repo = tmp_path / "r"
    _write(repo, "app.py", "print(1)\n")
    _write(repo, "examples/demo/docs/adr/0001-x.md", "# ADR 1\n")  # a sample's ADR is not the project's
    res = guards.check(repo)
    assert res["status"] == "ok" and res["exit"] == 0 and res["decisions"] == 0 and not res["unknown"]


# -- check / code_check: Python only ----------------------------------------------------------------------

def test_check_never_passes_a_file_it_cannot_read(tmp_path, capsys):
    """JVM persona: `check EmberForgeBlockEntity.java` gave '0 sites in 0 files', exit 0, and `--stdin --as
    Foo.java` parsed Java as Python; web persona: a renamed TS function passed `check --diff`, exit 0."""
    from verinoda import cli
    from verinoda.mcp.server import AtlasTools

    repo = _copy(GLOW, tmp_path / "glow")
    # Java is checked (docs/DESIGN.md D43): sites, never "0 sites in 0 files"; with no classpath found the
    # library's names are unknown and the result says so
    res = codecheck.check(repo, ["src/main/java/com/example/glowmod/ritual/Ritual.java"], env="none")
    assert res["summary"]["files"] == 1 and res["summary"]["sites"] > 0 and "not_checked" not in res
    assert all(s.get("language") == "Java" for s in res["sites"]) and res["java"]["builds"]
    assert any(x.startswith("Java: ") and "no classpath" in x for x in res["limits"])
    snip = codecheck.check(repo, snippet="class Foo { void f() { HeatMath.addHeatClamped(1); } }\n",
                           as_path="src/main/java/Foo.java", env="none")
    assert snip["summary"]["files"] == 1 and snip["sites"] and "not_checked" not in snip
    assert not any("does not parse" in str(f) for f in snip["files"])
    whole = codecheck.check(repo, ["src"], env="none")
    assert whole["summary"]["files"] >= 15 and not any(u["language"] == "Java" for u in whole.get("not_checked", []))
    # `api net.ashvale...EmberForgeBlockEntity` answered "module net is not in the standard library"
    api = codecheck.api(repo, "com.example.glowmod.ritual.Ritual.baslat", env="none")
    assert api["found"] is None and api["decided"] == "unsupported_language" and "Ritual.java" in api["why"]
    assert api["exit"] == 4  # not checked, as in `check` (it was 0)
    # the diff: a TypeScript rename is listed, never passed
    _write(repo, "web/orderService.ts", "export function applyDiscount(x: number) { return x; }\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "ts")
    _write(repo, "web/orderService.ts", "export function applyDiscont(x: number) { return x; }\n")
    _write(repo, "web/new.tsx", "export const A = 1;\n")
    capsys.readouterr()
    assert cli.main(["check", "--repo", str(repo), "--diff", "--json"]) == 4
    d = json.loads(capsys.readouterr().out)
    assert [u["path"] for u in d["not_checked"]] == ["web/new.tsx", "web/orderService.ts"]
    assert d["status"] == "unsupported_language" and d["summary"]["not_checked"] == 2
    assert cli.main(["check", "--repo", str(repo), "web/orderService.ts"]) == 4
    assert "NOT CHECKED (not Python)" in capsys.readouterr().out
    (repo / ".verinoda").mkdir(exist_ok=True)
    m = AtlasTools(repo).code_check(paths=["web/orderService.ts"], env="none")
    assert m["status"] == "unsupported_language" and m["exit"] == 4 and m["not_checked"][0]["language"] == "TypeScript"
    # a Python edit next to them is checked as before; the TS files still keep the exit at 4
    _write(repo, "tools/x.py", "import json\n\njson.loads('1')\n")
    mixed = codecheck.check(repo, diff="HEAD", env="none")
    assert mixed["status"] == "incomplete" and mixed["summary"]["files"] == 1 and mixed["exit"] == 4
    # nothing changed: nothing to check, and the result says so (in CI, compare with the base branch)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "all")
    none = codecheck.check(repo, diff="HEAD", env="none")
    assert none["status"] == "nothing_to_check" and none["exit"] == 0 and "--diff origin/main" in none["limits"][0]


def test_the_languages_check_reads_are_said_in_the_help_the_mcp_descriptions_and_the_skill():
    from verinoda import agents, cli
    from verinoda.mcp import server

    assert "Python, Java and Kotlin" in cli.build_parser().format_help()
    assert server.DESCRIPTIONS["code_check"].startswith("Python, Java, Kotlin")
    assert server.DESCRIPTIONS["api_members"].startswith("Python only")
    for agent in ("claude", "codex"):
        body = agents.render_skill(agent).decode("utf-8")
        assert "Python" in body and "Java" in body and "Kotlin" in body and "not_checked" in body


# -- check: a broad handler is no guard ---------------------------------------------------------------------

SNIPPET2 = '''import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)


def handle_request(payload: dict) -> dict:
    try:
        text = Path(payload["path"]).read_json()
        with ThreadPoolExecutor(thread_prefix="req") as ex:
            ex.submit(print, text)
        return {"ok": True}
    except Exception:
        log.exception("request failed")
        return {"ok": False}
'''


def test_invented_names_inside_except_exception_stay_absent(tmp_path):
    """Backend persona (snippet2.py): '0 absent ... 2 guarded', exit 0, for an invented method and keyword."""
    _write(tmp_path, "snippet2.py", SNIPPET2)
    res = codecheck.check(tmp_path, ["snippet2.py"], env="none", use_cache=False)
    absent = {s["name"]: s for s in res["sites"] if s["verdict"] == "absent"}
    assert set(absent) == {"read_json", "thread_prefix"} and res["exit"] == 3 and res["summary"]["guarded"] == 0
    for s in absent.values():
        assert s["swallowed_by"] == "try/except Exception (line 9)" and "would swallow the error" in s["message"]
    # the guards that stay guards: a specific handler, hasattr, and a broad handler around an import
    _write(tmp_path, "ok.py", "import json\n\ntry:\n    import not_installed_mod\nexcept Exception:\n"
                              "    not_installed_mod = None\n\ntry:\n    json.loadz('1')\nexcept AttributeError:\n"
                              "    pass\nif hasattr(json, 'loadx'):\n    json.loadx('1')\n")
    ok = codecheck.check(tmp_path, ["ok.py"], env="none", use_cache=False)
    assert {s["name"]: s["verdict"] for s in ok["sites"]} == {"not_installed_mod": "guarded", "loadz": "guarded",
                                                              "loadx": "guarded"}


# -- claims: outside Python, strong_inference at most ---------------------------------------------------------

def _src(repo: Path, rel: str, a: int) -> dict:
    ev = evmod.source_evidence(repo, rel, a, None, commit=None)
    ev["id"] = f"ev:{rel}:{a}"
    return ev


def test_non_python_config_and_relation_claims_are_strong_inference_at_most(tmp_path):
    """Web persona: 'jwtSecret is read from DISCOUNT_THRESHOLD' (config.ts:3) became statically_verified 0.90;
    JVM persona: analyze verified a Java relation that `claim add` graded 0.70."""
    from verinoda.claims import Claims

    repo = tmp_path / "r"
    _write(repo, "apps/api/src/config.ts", "export const config = {\n  port: Number(process.env.PORT ?? 3000),\n"
                                           "  discountThreshold: Number(process.env.DISCOUNT_THRESHOLD ?? 100),\n"
                                           "  discountPercent: 10,\n  jwtSecret: process.env.JWT_SECRET ?? '',\n};\n")
    shutil.copytree(GLOW / "src", repo / "src")
    shutil.copytree(ORDERS / "orders", repo / "orders")
    cfg = entail.assess("config", repo, {"env": "DISCOUNT_THRESHOLD", "free_text": True, "symbol": "DISCOUNT_THRESHOLD"},
                        _src(repo, "apps/api/src/config.ts", 3), text="jwtSecret is read from DISCOUNT_THRESHOLD",
                        subjects=["apps/api/src/config.ts"])
    assert cfg.grade == "partial" and "checked for Python only" in cfg.reason
    ritual = "src/main/java/com/example/glowmod/ritual/Ritual.java"
    rel = entail.assess("relation", repo, {"target_label": "spawn"}, _src(repo, ritual, 29),
                        subjects=[f"{ritual}::baslat", "src/main/java/com/example/glowmod/entity/Wisp.java::spawn"])
    assert rel.grade == "partial" and "checked for Python only" in rel.reason and "(imported)" in rel.reason
    # Python keeps its binding checks: still full
    assert entail.grade("relation", repo, {"target_label": "apply_discount"}, _src(repo, "orders/pricing.py", 8),
                        subjects=["orders/pricing.py::compute_total", "orders/pricing.py::apply_discount"]) == "full"
    assert entail.grade("config", repo, {"env": "ORDERS_DISCOUNT_THRESHOLD"}, _src(repo, "orders/config.py", 7)) == \
        "full"
    st = open_store(repo)
    try:
        c = Claims(st, repo).create("jwtSecret is read from DISCOUNT_THRESHOLD", project="p", snapshot=None,
                                    status="statically_verified", kind="config",
                                    evidence=[(_src(repo, "apps/api/src/config.ts", 3), "supports")],
                                    spec={"env": "DISCOUNT_THRESHOLD", "free_text": True,
                                          "symbol": "DISCOUNT_THRESHOLD"}, subjects=["apps/api/src/config.ts"])
        assert c["status"] == "strong_inference" and c["confidence"] <= 0.7
    finally:
        st.close()


# -- review of the branch (reviewer-a, 2026-09-26) ------------------------------------------------------------

def _pkg(name: str, **deps: str) -> str:
    """A package.json whose dependencies sit one per line (line 4 holds the first one)."""
    body = ",\n".join(f'    "{k}": "{v}"' for k, v in deps.items())
    return f'{{\n  "name": "{name}",\n  "dependencies": {{\n{body}\n  }}\n}}\n'


def test_workspace_globs_match_one_path_segment_at_a_time():
    cases = [("packages/a", "packages/*", True), ("packages/a/template-react", "packages/*", False),
             ("packages/a/b", "packages/**", True), ("packages", "packages/**", True),
             ("packages/a", "./packages/*", True), ("packages/a", ".//packages//*/", True),
             ("apps/web", "{apps,libs}/*", True), ("libs/x", "{apps,libs}/*", True),
             ("packages/a", "packages/a", True), ("packages/a/b", "packages/a", False),
             ("packages/a/test/x", "**/test/**", True), ("other/a", "packages/*", False)]
    for path, glob, want in cases:
        assert guards.ws_match(path, glob) is want, (path, glob)


def test_a_template_below_a_workspace_package_is_no_workspace_package(tmp_path):
    """reviewer-a h1: pnpm `packages/*` read packages/create-app/template-react/package.json (a scaffolder's
    template) and gave VIOLATED, exit 1; the package managers match `*` within one path segment."""
    repo = tmp_path / "h1"
    _write(repo, "package.json", '{"name": "root", "private": true}\n')
    _write(repo, "pnpm-workspace.yaml", 'packages:\n  - "packages/*"\n')
    _write(repo, "packages/create-app/package.json", _pkg("create-app", kleur="1"))
    _write(repo, "packages/create-app/template-react/package.json", _pkg("tpl", axios="^1"))
    _git(repo, "init", "-q")
    res = guards.check(repo, records=[_rec(repo, "dependency absent=axios", "dependency absent=kleur")])
    assert _at(res["violations"]) == ["packages/create-app/package.json:4"] and res["violations"][0]["guard"] == "g2"
    (ok,) = res["ok"]
    assert ok["guard"] == "g1" and ok["scope"]["manifests"] == 2
    assert any("other manifest(s) are not read" in x and "template-react/package.json" in x for x in ok["limits"])


def test_npm_dot_slash_workspace_globs_are_read(tmp_path):
    """reviewer-a: `"workspaces": ["./packages/*"]` (the form npm's docs use) matched nothing: ok, exit 0."""
    repo = tmp_path / "dot"
    _write(repo, "package.json", '{"name": "root", "private": true, "workspaces": ["./packages/*", "!./packages/b"]}\n')
    _write(repo, "packages/a/package.json", _pkg("a", axios="^1"))
    _write(repo, "packages/b/package.json", _pkg("b", axios="^1"))
    _git(repo, "init", "-q")
    res = guards.check(repo, records=[_rec(repo, "dependency absent=axios")])
    assert res["exit"] == 1 and _at(res["violations"]) == ["packages/a/package.json:4"]


def test_a_declared_workspace_package_is_read_whatever_its_folder_is_called(tmp_path):
    """reviewer-a h2: packages/build/package.json (package @x/build) was skipped as a build folder, not named
    anywhere, and the guard said ok."""
    repo = tmp_path / "h2"
    _write(repo, "package.json", '{"name": "root", "private": true, "workspaces": ["packages/*", "apps/*"]}\n')
    _write(repo, "packages/build/package.json", _pkg("@x/build", axios="^1"))
    _write(repo, "apps/demo/package.json", _pkg("@x/demo", axios="^1"))
    _write(repo, "examples/starter/package.json", _pkg("starter", axios="^1"))  # declared by nobody
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")  # a tracked build/ folder is source (an untracked one is build output)
    res = guards.check(repo, records=[_rec(repo, "dependency absent=axios")])
    assert _at(res["violations"]) == ["apps/demo/package.json:4", "packages/build/package.json:4"]
    # a manifest a folder rule leaves out is named in the limits, never dropped silently
    assert any("manifest(s) under test, sample" in x and "examples/starter/package.json" in x
               for x in res["violations"][0]["limits"])
    # no git: the file list leaves build/ out, so the declared glob finds nothing - and the ok says so
    bare = tmp_path / "bare"
    _write(bare, "package.json", '{"name": "root", "private": true, "workspaces": ["packages/*"]}\n')
    _write(bare, "packages/build/package.json", _pkg("@x/build", axios="^1"))
    (ok,) = guards.check(bare, records=[_rec(bare, "dependency absent=axios")])["ok"]
    assert any("1 declared workspace glob(s) match no package.json" in x and "packages/*" in x for x in ok["limits"])


def test_no_edge_from_a_leaf_module_was_looked_at(tmp_path):
    """reviewer-a h11: `no_edge from=pkg/constants.py` (a leaf: no import, no call) was unknown, exit 3, on
    every run, although the index extracts Python imports (it has them for pkg/app.py)."""
    from verinoda import index

    repo = tmp_path / "leaf"
    _write(repo, "pkg/__init__.py", "")
    _write(repo, "pkg/constants.py", "LIMIT = 10\n")
    _write(repo, "pkg/app.py", "from pkg.constants import LIMIT\n\n\ndef run():\n    return LIMIT\n")
    _write(repo, "tools/run.sh", "echo hi\n")
    workflow.init(repo)
    st = open_store(repo)
    try:
        workflow.scan(st, repo)
    finally:
        st.close()
    g = index.load(repo)
    res = guards.check(repo, graph=g, records=[_rec(repo, "no_edge from=pkg/constants.py to=pkg/app.py")])
    (ok,) = res["ok"]
    assert res["exit"] == 0 and ok["scope"]["edges_checked"] == 0 and ok["scope"]["from_files_looked_at"] == 1
    assert any("no calls/" in x and "1 was looked at and holds none" in x for x in ok["limits"])
    # a language the index has no such edge for anywhere: nothing shows the file was looked at
    res = guards.check(repo, graph=g, records=[_rec(repo, "no_edge from=tools/run.sh to=pkg/app.py")])
    assert res["exit"] == 3 and "none out of any other file of their language" in res["unknown"][0]["why"]


def test_check_tells_absent_from_not_checked_by_the_exit_code(tmp_path, capsys):
    """reviewer-a: exit 3 meant both "a name is absent" and "a file is not Python", so a Python package that
    ships JS (verinoda/ui, a Django app with static/*.js) could never pass; a Python file that does not parse
    gave exit 0 and counted as checked."""
    from verinoda import cli

    repo = tmp_path / "mixed"
    _write(repo, "app/views.py", "import json\n\n\ndef v():\n    return json.dumps({})\n")
    _write(repo, "app/static/app.js", "export const x = 1;\n")
    res = codecheck.check(repo, ["app"], env="none", use_cache=False)
    assert res["exit"] == 4 and res["status"] == "incomplete" and res["summary"]["files"] == 1
    assert "not checked: language not supported (JavaScript 1" in res["exit_because"]
    _write(repo, "app/absent.py", "import json\n\njson.loadz('x')\n")
    res = codecheck.check(repo, ["app"], env="none", use_cache=False)
    assert res["exit"] == 3 and res["exit_because"].startswith("1 absent; 1 file not checked")
    # a file that does not parse: not checked, never counted, exit 4 (it was exit 0 and "1 file")
    _write(repo, "bad.py", 'def f(:\n    json.loadz("x")\n')
    capsys.readouterr()
    assert cli.main(["check", "bad.py", "--env", "none", "--no-cache", "--repo", str(repo)]) == 4
    out = capsys.readouterr().out
    assert "(0 sites in 0 files; whole files); 1 file NOT CHECKED\n" in out and "exit 4: 1 Python file" in out
    bad = codecheck.check(repo, ["bad.py"], env="none", use_cache=False)
    assert bad["summary"]["files"] == 0 and bad["status"] == "incomplete"
    assert bad["not_checked"] == [{"path": "bad.py", "language": "Python",
                                   "why": bad["files"][0]["error"]}] and "does not parse" in bad["files"][0]["error"]
    snip = codecheck.check(repo, snippet="def f(:\n    pass\n", as_path="x.py", env="none")
    assert snip["exit"] == 4 and snip["not_checked"][0]["path"] == "x.py" and snip["summary"]["files"] == 0


def test_a_changed_notebook_or_cython_file_is_listed_as_not_checked(tmp_path):
    """reviewer-a: a changed .ipynb and .pyx gave nothing_to_check ("no Python file changed"), exit 0."""
    repo = tmp_path / "nb"
    cell = {"cells": [{"cell_type": "code", "source": ["import json\n", "json.loadz('x')\n"], "metadata": {},
                       "outputs": [], "execution_count": None}], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    _write(repo, "a.py", "x = 1\n")
    _write(repo, "nb.ipynb", json.dumps(cell))
    _write(repo, "fast.pyx", "def f():\n    return 1\n")
    _write(repo, "tools/go.sh", "echo 1\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    _write(repo, "nb.ipynb", json.dumps(cell).replace("loadz", "loadzz"))
    _write(repo, "fast.pyx", "def f():\n    return os.getcwdu()\n")
    _write(repo, "tools/go.sh", "echo 2\n")
    res = codecheck.check(repo, diff="HEAD", env="none")
    assert res["exit"] == 4 and res["status"] == "unsupported_language"
    langs = {u["path"]: u["language"] for u in res["not_checked"]}
    assert langs == {"fast.pyx": "Cython", "nb.ipynb": "Jupyter notebook", "tools/go.sh": "Shell"}
    assert "code cells are not read" in next(u["why"] for u in res["not_checked"] if u["path"] == "nb.ipynb")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "all")
    none = codecheck.check(repo, diff="HEAD", env="none")
    assert none["exit"] == 0 and none["limits"][0].startswith("no Python or Java file was checked: no .py or .java "
                                                             "file changed")


def test_a_flow_hop_outside_python_is_strong_inference_at_most(tmp_path):
    """reviewer-a: the same Java call was `partial` as a relation claim and `full` as a flow hop."""
    repo = tmp_path / "r"
    shutil.copytree(GLOW / "src", repo / "src")
    shutil.copytree(ORDERS / "orders", repo / "orders")
    ritual = "src/main/java/com/example/glowmod/ritual/Ritual.java"
    line = next(i for i, ln in enumerate((repo / ritual).read_text(encoding="utf-8").splitlines(), 1)
                if ln.strip().startswith("alaniAc(world, altar);"))
    ev = _src(repo, ritual, line)
    ev.setdefault("meta", {})["hop"] = "Ritual.baslat->Ritual.alaniAc"
    spec = {"hops": [{"from": "Ritual.baslat", "to": "Ritual.alaniAc", "relation": "calls"}]}
    flow = entail.assess("flow", repo, spec, ev, text="Ritual.baslat calls Ritual.alaniAc",
                         subjects=[ritual, ritual])
    rel = entail.assess("relation", repo, {"target_label": "alaniAc"}, _src(repo, ritual, line),
                        subjects=[f"{ritual}::baslat", f"{ritual}::alaniAc"])
    assert flow.grade == rel.grade == "partial" and "checked for Python only" in flow.reason
    # a Python hop keeps its binding checks
    ev = _src(repo, "orders/pricing.py", 8)
    ev.setdefault("meta", {})["hop"] = "compute_total->apply_discount"
    spec = {"hops": [{"from": "compute_total", "to": "apply_discount", "relation": "calls"}]}
    assert entail.assess("flow", repo, spec, ev, text="compute_total calls apply_discount",
                         subjects=["orders/pricing.py", "orders/pricing.py"]).grade == "full"

def test_analyze_caps_a_java_call_path_and_says_why(glow_indexed):
    """reviewer-a: analyze asked `statically_verified` for a flow whose hops are Java calls graded full by the
    syntax tree, while the same call as a relation claim stops at strong_inference."""
    from verinoda import analysis

    st = open_store(glow_indexed)
    try:
        res = analysis.analyze(st, glow_indexed, "What is the call path from the ritual command to spawning a wisp?")
    finally:
        st.close()
    flows = [c for c in res["claims"] if c["text"].startswith("Call path")]
    assert flows, [c["text"] for c in res["claims"]]
    for c in flows:
        assert c["status"] != "statically_verified", c
        assert any("checked for Python only" in u and ".java:" in u for u in c["uncertainties"]), c["uncertainties"]


def test_readmes_templates_and_this_repository_s_fixture_are_no_adr(tmp_path):
    """reviewer-a h9: docs/decisions/README.md alone made `decide check` exit 3; so did the branch's own
    fixture (benchmarks/results/.../adr-0002.md, a verinoda-decision front matter) on Verinoda itself."""
    from verinoda.snapshot import list_files

    repo = tmp_path / "r"
    _write(repo, "app.py", "x = 1\n")
    _write(repo, "docs/decisions/README.md", "# How we record decisions\n")
    _write(repo, "docs/decisions/template.md", "# ADR-NNNN: title\n")
    _write(repo, "docs/adr/adr-template.md", "# Title\n")
    res = guards.check(repo)
    assert res["status"] == "ok" and res["exit"] == 0 and not res["unknown"]
    _write(repo, "docs/decisions/0001-use-sqlite.md", "# 1. Use SQLite\n")
    assert dm.adr_like_files(repo, list_files(repo)) == ["docs/decisions/0001-use-sqlite.md"]
    assert dm.adr_like_files(ROOT, list_files(ROOT)) == []


def test_a_configured_decisions_folder_that_does_not_exist_is_unknown(tmp_path, capsys):
    """reviewer-a h8: `--decisions-dir docs/missing` (or a typo in verinoda.toml) gave "0 decision records",
    exit 0."""
    from verinoda import cli

    repo = tmp_path / "r"
    _write(repo, "app.py", "x = 1\n")
    capsys.readouterr()
    assert cli.main(["decide", "check", "--decisions-dir", "docs/missing", "--repo", str(repo)]) == 3
    assert "the decisions folder docs/missing (--decisions-dir) does not exist" in capsys.readouterr().out
    _write(repo, "verinoda.toml", '[decisions]\ndir = "docs/decisons"\n')
    res = guards.check(repo)
    assert res["exit"] == 3 and "docs/decisons (verinoda.toml [decisions] dir) does not exist" in \
        res["unknown"][0]["why"]
    (repo / "docs" / "decisons").mkdir(parents=True)
    assert guards.check(repo)["exit"] == 0  # an empty folder that exists: no record, nothing to check
