"""The branch review's findings (reviewer-a, 2026-09-26), run on tiny projects and copies of the examples.

usage: python benchmarks/results/no-silent-ok-2026-09-26/review-repros.py LABEL   (writes review-LABEL.json here)

Each case is a reviewer's repro of branch night/no-silent-ok before its review fixes. `wrong` is True when the
outcome is what the reviewer reported (a false VIOLATED, an ok or exit 0 over something not checked, the
same exit code for "a name is absent" and "a file was not checked", a Java flow hop graded full). It uses
only calls that exist before and after the fixes (guards.check with records, codecheck.check / api, the CLI,
entail.assess, decisions.adr_like_files), so the same file runs on both versions of the code. Run it from
the tree whose code is on the path: the last case reads that tree's own files.
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

from verinoda import cli, codecheck, entail, guards, index, workflow  # noqa: E402
from verinoda import decisions as dm  # noqa: E402
from verinoda import evidence as evmod  # noqa: E402
from verinoda.snapshot import list_files  # noqa: E402
from verinoda.store import open_store  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
GLOW = ROOT / "examples" / "glow_mod"
RITUAL = "src/main/java/com/example/glowmod/ritual/Ritual.java"


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                    "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True)


def write(repo: Path, rel: str, text: str) -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_bytes(text.encode("utf-8"))


def pkg(name: str, dep: str) -> str:
    return f'{{\n  "name": "{name}",\n  "dependencies": {{\n    "{dep}": "^1"\n  }}\n}}\n'


def rec(repo: Path, spec: str) -> dm.Decision:
    return dm.Decision(id="ADR-0009", number=9, title="t", guards=[dm.parse_guard(spec, repo, "g1")])


def summary(res: dict) -> dict:
    return {"status": res.get("status"), "exit": res.get("exit"),
            "violations": sorted(v["at"] for v in res.get("violations") or [])}


def cli_run(*args: str) -> int:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            return cli.main(list(args))
        except SystemExit as exc:
            return int(exc.code or 0)


def main() -> None:
    label = sys.argv[1] if len(sys.argv) > 1 else "run"
    tmp = Path(tempfile.mkdtemp(prefix="review-"))
    cases: dict[str, dict] = {}
    try:
        # 1 (medium): pnpm packages/* and a scaffolder's template below a workspace package
        m = tmp / "h1"
        write(m, "package.json", '{"name": "root", "private": true}\n')
        write(m, "pnpm-workspace.yaml", 'packages:\n  - "packages/*"\n')
        write(m, "packages/create-app/package.json", pkg("create-app", "kleur"))
        write(m, "packages/create-app/template-react/package.json", pkg("tpl", "axios"))
        git(m, "init", "-q")
        r = summary(guards.check(m, records=[rec(m, "dependency absent=axios")]))
        cases["workspace_nested_template"] = {**r, "wrong": bool(r["violations"])}
        # 2 (medium): npm's "./packages/*" form
        m = tmp / "dot"
        write(m, "package.json", '{"name": "root", "private": true, "workspaces": ["./packages/*"]}\n')
        write(m, "packages/a/package.json", pkg("a", "axios"))
        git(m, "init", "-q")
        r = summary(guards.check(m, records=[rec(m, "dependency absent=axios")]))
        cases["workspace_dot_slash"] = {**r, "wrong": not r["violations"]}
        # 3 (medium): a declared workspace package in a folder named build / demo
        m = tmp / "h2"
        write(m, "package.json", '{"name": "root", "private": true, "workspaces": ["packages/*", "apps/*"]}\n')
        write(m, "packages/build/package.json", pkg("@x/build", "axios"))
        write(m, "apps/demo/package.json", pkg("@x/demo", "axios"))
        git(m, "init", "-q")
        git(m, "add", "-A")
        r = summary(guards.check(m, records=[rec(m, "dependency absent=axios")]))
        cases["workspace_build_demo_folders"] = {**r, "wrong": len(r["violations"]) < 2}
        # 4 (medium): check on a Python package with a JS file, with and without an absent name
        m = tmp / "mixed"
        write(m, "app/views.py", "import json\n\n\ndef v():\n    return json.dumps({})\n")
        write(m, "app/static/app.js", "export const x = 1;\n")
        a = codecheck.check(m, ["app"], env="none", use_cache=False)
        write(m, "app/absent.py", "import json\n\njson.loadz('x')\n")
        b = codecheck.check(m, ["app"], env="none", use_cache=False)
        cases["check_exit_not_checked_vs_absent"] = {"exit_not_checked": a["exit"], "exit_absent": b["exit"],
                                                     "wrong": a["exit"] in (0, b["exit"])}
        # 5 (medium): a Java call graded as a flow hop and as a relation
        g = tmp / "glow"
        shutil.copytree(GLOW / "src", g / "src")
        line = next(i for i, ln in enumerate((g / RITUAL).read_text(encoding="utf-8").splitlines(), 1)
                    if ln.strip().startswith("alaniAc(world, altar);"))
        ev = evmod.source_evidence(g, RITUAL, line, None, commit=None)
        ev.setdefault("meta", {})["hop"] = "Ritual.baslat->Ritual.alaniAc"
        flow = entail.assess("flow", g, {"hops": [{"from": "Ritual.baslat", "to": "Ritual.alaniAc",
                                                   "relation": "calls"}]}, ev,
                             text="Ritual.baslat calls Ritual.alaniAc", subjects=[RITUAL, RITUAL])
        rel = entail.assess("relation", g, {"target_label": "alaniAc"},
                            evmod.source_evidence(g, RITUAL, line, None, commit=None),
                            subjects=[f"{RITUAL}::baslat", f"{RITUAL}::alaniAc"])
        cases["java_flow_hop_grade"] = {"flow": flow.grade, "relation": rel.grade,
                                        "wrong": flow.grade == "full" and rel.grade != "full"}
        # 6 (low): no_edge from a Python leaf module
        m = tmp / "leaf"
        write(m, "pkg/__init__.py", "")
        write(m, "pkg/constants.py", "LIMIT = 10\n")
        write(m, "pkg/app.py", "from pkg.constants import LIMIT\n\n\ndef run():\n    return LIMIT\n")
        workflow.init(m)
        st = open_store(m)
        try:
            workflow.scan(st, m)
        finally:
            st.close()
        r = summary(guards.check(m, graph=index.load(m),
                                 records=[rec(m, "no_edge from=pkg/constants.py to=pkg/app.py")]))
        cases["no_edge_leaf_module"] = {**r, "wrong": r["status"] == "unknown"}
        # 7 (low): a changed notebook and Cython file in --diff
        m = tmp / "nb"
        cell = {"cells": [{"cell_type": "code", "source": ["import json\n", "json.loadz('x')\n"], "metadata": {},
                           "outputs": [], "execution_count": None}], "metadata": {}, "nbformat": 4,
                "nbformat_minor": 5}
        write(m, "a.py", "x = 1\n")
        write(m, "nb.ipynb", json.dumps(cell))
        write(m, "fast.pyx", "def f():\n    return 1\n")
        git(m, "init", "-q")
        git(m, "add", "-A")
        git(m, "commit", "-q", "-m", "init")
        write(m, "nb.ipynb", json.dumps(cell).replace("loadz", "loadzz"))
        write(m, "fast.pyx", "def f():\n    return os.getcwdu()\n")
        r = codecheck.check(m, diff="HEAD", env="none")
        cases["notebook_cython_diff"] = {"status": r["status"], "exit": r["exit"], "wrong": r["exit"] == 0}
        # 8 (low): a decisions folder's README, and this repository's own files
        m = tmp / "h9"
        write(m, "app.py", "x = 1\n")
        write(m, "docs/decisions/README.md", "# How we record decisions\n")
        rc = cli_run("decide", "check", "--repo", str(m))
        own = dm.adr_like_files(ROOT, list_files(ROOT))
        cases["adr_readme_and_own_fixture"] = {"exit_readme_only": rc, "own_adr_like": own,
                                               "wrong": rc != 0 or bool(own)}
        # 9 (low): a Python file that does not parse
        m = tmp / "bad"
        write(m, "bad.py", 'def f(:\n    json.loadz("x")\n')
        r = codecheck.check(m, ["bad.py"], env="none", use_cache=False)
        cases["python_file_does_not_parse"] = {"status": r["status"], "exit": r["exit"],
                                               "files": r["summary"]["files"], "wrong": r["exit"] == 0}
        # 10 (low): a configured decisions folder that does not exist
        m = tmp / "h8"
        write(m, "app.py", "x = 1\n")
        rc = cli_run("decide", "check", "--decisions-dir", "docs/missing", "--repo", str(m))
        cases["configured_folder_missing"] = {"exit": rc, "wrong": rc == 0}
        # 11 (low): api on a Java class of the project
        g2 = tmp / "glow2"
        shutil.copytree(GLOW, g2, ignore=shutil.ignore_patterns(".verinoda", "*.db"))
        api = codecheck.api(g2, "com.example.glowmod.ritual.Ritual", env="none")
        chk = codecheck.check(g2, [RITUAL], env="none", use_cache=False)
        cases["api_on_java"] = {"api_exit": api.get("exit"), "check_exit": chk["exit"],
                                "wrong": api.get("exit") == 0}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    wrong = sum(1 for c in cases.values() if c["wrong"])
    out = {"label": label, "cases": cases, "wrong": wrong, "total": len(cases)}
    (HERE / f"review-{label}.json").write_bytes((json.dumps(out, indent=1) + "\n").encode("utf-8"))
    print(f"{label}: {wrong} of {len(cases)} review findings reproduce")
    for name, c in cases.items():
        print(f"  {'WRONG' if c['wrong'] else 'fixed'}  {name}: "
              + ", ".join(f"{k}={v}" for k, v in c.items() if k != "wrong"))


if __name__ == "__main__":
    main()
