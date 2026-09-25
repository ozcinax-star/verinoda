"""The senior evaluators' "said ok without looking" repros (2026-09-25), run on copies of the examples.

usage: python benchmarks/results/no-silent-ok-2026-09-26/repros.py LABEL   (writes repros-LABEL.json here)

Each case is an evaluator's exact repro (gaps 3, 4, 11 and 13 of the synthesis). `silent` is True when the
outcome is what the evaluator reported - a pass, an `ok` or a verified status over something that was not
checked or is false. It uses only calls that exist before and after the change (guards.check with records,
the CLI, codecheck.check, entail.assess), so the same file runs on both versions of the code.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from verinoda import cli, codecheck, entail, guards  # noqa: E402
from verinoda import decisions as dm  # noqa: E402
from verinoda import evidence as evmod  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
GLOW, ORDERS = ROOT / "examples" / "glow_mod", ROOT / "examples" / "orders_app"
LANTERN = "src/main/java/com/example/glowmod/event/LanternEvents.java"


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                    "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True)


def copy(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", "*.db"))
    git(dst, "init", "-q")
    git(dst, "add", "-A")
    git(dst, "commit", "-q", "-m", "init")
    return dst


def write(repo: Path, rel: str, text: str) -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_bytes(text.encode("utf-8"))


def rec(repo: Path, *specs: str):
    return dm.Decision(id="ADR-0009", number=9, title="t",
                       guards=[dm.parse_guard(s, repo, f"g{i}") for i, s in enumerate(specs, 1)])


def summary(res: dict) -> dict:
    return {"status": res.get("status"), "exit": res.get("exit"),
            "violations": sorted(v["at"] for v in res.get("violations") or []),
            "ok": [o.get("scope") for o in res.get("ok") or []], "unknown": len(res.get("unknown") or [])}


def cli_run(*args: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        rc = cli.main(list(args))
    return rc, out.getvalue()


def main() -> None:
    label = sys.argv[1]
    tmp = Path(tempfile.mkdtemp(prefix="nso-"))
    cases: dict[str, dict] = {}
    try:
        # 1 (JVM): only_in over the Fabric template's com.example package, a client call injected
        g = copy(GLOW, tmp / "glow")
        p = g / LANTERN
        s = p.read_text(encoding="utf-8").replace(
            "import net.minecraft.block.Blocks;", "import net.minecraft.block.Blocks;\nimport "
            "net.minecraft.client.MinecraftClient;\nimport com.example.glowmod.client.GlowModClient;").replace(
            "            Wisp.spawn((ServerWorld) world, pos.up());", "            Wisp.spawn((ServerWorld) world, "
            "pos.up());\n            MinecraftClient.getInstance().inGameHud.setOverlayMessage(null, false);\n"
            "            GlowModClient.flash();")
        p.write_bytes(s.encode("utf-8"))
        r = summary(guards.check(g, records=[rec(g, "only_in calls=net.minecraft.client.MinecraftClient."
                                                    "getInstance allowed=src/client/**")]))
        cases["only_in_com_example"] = {**r, "silent": f"{LANTERN}:33" not in r["violations"]}
        # 2 (JVM): no_edge to an external package
        from verinoda import index, workflow
        from verinoda.store import open_store

        g2 = copy(GLOW, tmp / "glow2")
        workflow.init(g2)
        st = open_store(g2)
        try:
            workflow.scan(st, g2)
        finally:
            st.close()
        r = summary(guards.check(g2, graph=index.load(g2),
                                 records=[rec(g2, "no_edge from=src/main/** to=net.minecraft.client.**")]))
        cases["no_edge_external_package"] = {**r, "silent": r["exit"] == 0}
        # 3 (JVM): Kotlin DSL + version catalog
        k = copy(GLOW, tmp / "kts")
        (k / "build.gradle").unlink()
        write(k, "build.gradle.kts", 'plugins {\n    alias(libs.plugins.loom)\n}\n\ndependencies {\n'
              '    minecraft(libs.minecraft)\n    mappings("net.fabricmc:yarn:1.21.1+build.3:v2")\n'
              '    modImplementation(libs.fabric.loader)\n    modImplementation(libs.fabric.api)\n'
              '    implementation(libs.snakeyaml)\n    include(libs.snakeyaml)\n}\n')
        write(k, "gradle/libs.versions.toml", '[versions]\nsnakeyaml = "2.2"\nfabric-api = "0.105.0+1.21.1"\n\n'
              '[libraries]\nminecraft = { module = "com.mojang:minecraft", version = "1.21.1" }\n'
              'fabric-loader = { module = "net.fabricmc:fabric-loader", version = "0.16.5" }\n'
              'fabric-api = { module = "net.fabricmc.fabric-api:fabric-api", version.ref = "fabric-api" }\n'
              'snakeyaml = { module = "org.yaml:snakeyaml", version.ref = "snakeyaml" }\n\n'
              '[plugins]\nloom = { id = "fabric-loom", version = "1.7-SNAPSHOT" }\n')
        r = summary(guards.check(k, records=[rec(k, "dependency absent=snakeyaml")]))
        cases["dependency_version_catalog"] = {**r, "silent": not r["violations"]}
        # 4 (web): a pnpm workspace package adds axios
        m = tmp / "mono"
        write(m, "package.json", '{\n  "name": "acme",\n  "private": true\n}\n')
        write(m, "pnpm-workspace.yaml", 'packages:\n  - "apps/*"\n')
        write(m, "apps/web/package.json", '{\n  "name": "@acme/web",\n  "dependencies": {\n    "axios": "^1.7.0"\n'
                                          '  }\n}\n')
        git(m, "init", "-q")
        r = summary(guards.check(m, records=[rec(m, "dependency absent=axios")]))
        cases["dependency_workspace_package"] = {**r, "silent": not r["violations"]}
        # 5-6 (lead): a fresh CI clone, the records committed under docs/decisions
        o = copy(ORDERS, tmp / "orders")
        adr = (HERE / "adr-0002.md").read_text(encoding="utf-8")
        write(o, "docs/decisions/ADR-0002-db-access-only-in-repository.md", adr)
        svc = o / "orders/service.py"
        svc.write_bytes(svc.read_bytes() + b"\n\nimport sqlite3 as _db\n\n\ndef audit(path):\n"
                                           b"    conn = _db.connect(path)\n    return conn\n")
        git(o, "add", "-A")
        git(o, "commit", "-q", "-m", "violation")
        c = tmp / "orders_ci"
        git(tmp, "clone", "-q", str(o), str(c))
        rc, out = cli_run("decide", "check", "--repo", str(c))
        cases["fresh_clone_no_config"] = {"exit": rc, "first_line": out.split("\n")[0], "silent": rc == 0}
        write(c, "verinoda.toml", '[decisions]\ndir = "docs/decisions"\n')
        rc, out = cli_run("decide", "check", "--repo", str(c))
        cases["fresh_clone_verinoda_toml"] = {"exit": rc, "first_line": out.split("\n")[0], "silent": rc != 1}
        # 7-9 (JVM, web): check on Java and TypeScript
        res = codecheck.check(g, [LANTERN], env="none")
        cases["check_java_file"] = {"exit": res["exit"], "files": res["summary"]["files"],
                                    "status": res.get("status"), "silent": res["exit"] == 0}
        res = codecheck.check(g, snippet="class Foo { void f() { HeatMath.addHeatClamped(1); } }\n",
                              as_path="src/main/java/Foo.java", env="none")
        cases["check_stdin_as_java"] = {"exit": res["exit"], "status": res.get("status"),
                                        "parsed_as_python": any("does not parse" in str(f.get("error") or "")
                                                                for f in res["files"]),
                                        "silent": res["exit"] == 0}
        write(g, "web/orderService.ts", "export function applyDiscount(x: number) { return x; }\n")
        git(g, "add", "-A")
        git(g, "commit", "-q", "-m", "ts")
        write(g, "web/orderService.ts", "export function applyDiscont(x: number) { return x; }\n")
        res = codecheck.check(g, diff="HEAD", env="none")
        cases["check_diff_ts_rename"] = {"exit": res["exit"], "status": res.get("status"), "silent": res["exit"] == 0}
        # 10 (backend): invented names inside `except Exception`
        b = tmp / "backend"
        write(b, "snippet2.py", (HERE / "snippet2.py").read_text(encoding="utf-8"))
        res = codecheck.check(b, ["snippet2.py"], env="none", use_cache=False)
        cases["broad_except_snippet2"] = {"exit": res["exit"], "absent": res["summary"]["absent"],
                                          "guarded": res["summary"]["guarded"], "silent": res["exit"] == 0}
        # 11-12 (web, JVM): claims outside Python
        write(b, "apps/api/src/config.ts", "export const config = {\n  port: Number(process.env.PORT ?? 3000),\n"
                                           "  discountThreshold: Number(process.env.DISCOUNT_THRESHOLD ?? 100),\n"
                                           "  discountPercent: 10,\n  jwtSecret: process.env.JWT_SECRET ?? '',\n};\n")
        ev = evmod.source_evidence(b, "apps/api/src/config.ts", 3, None, commit=None)
        gr = entail.assess("config", b, {"env": "DISCOUNT_THRESHOLD", "free_text": True, "symbol": "DISCOUNT_THRESHOLD"},
                           ev, text="jwtSecret is read from DISCOUNT_THRESHOLD", subjects=["apps/api/src/config.ts"])
        cases["ts_config_claim_false"] = {"grade": gr.grade, "silent": gr.grade == "full"}
        ritual = "src/main/java/com/example/glowmod/ritual/Ritual.java"
        ev = evmod.source_evidence(g, ritual, 29, None, commit=None)
        gr = entail.assess("relation", g, {"target_label": "spawn"}, ev,
                           subjects=[f"{ritual}::baslat", "src/main/java/com/example/glowmod/entity/Wisp.java::spawn"])
        cases["java_relation_grade"] = {"grade": gr.grade, "silent": gr.grade == "full"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    silent = sum(1 for c in cases.values() if c["silent"])
    out = {"label": label, "cases": cases, "silent": silent, "total": len(cases)}
    (HERE / f"repros-{label}.json").write_bytes((json.dumps(out, indent=1) + "\n").encode("utf-8"))
    print(f"{label}: {silent} of {len(cases)} repros still silent")
    for name, c in cases.items():
        print(f"  {'SILENT' if c['silent'] else 'caught'}  {name}: "
              + ", ".join(f"{k}={v}" for k, v in c.items() if k != "silent"))


if __name__ == "__main__":
    main()
