"""decisions_v1 (a): guard mutations on git copies of orders_app, glow_mod and forge_mod.

Each case: a decision with guards, then file changes. Truth is fixed in the case (written before the run):
  violating    - every line marked `<<` (a `# <<` / `// <<` comment) must be VIOLATED, nothing else may be
  benign       - no VIOLATED finding at all (POSSIBLE is allowed)
  out_of_reach - a real violation the engine cannot verify: no VIOLATED; the limits (or a POSSIBLE finding)
                 must name the form (`words`)
Baseline: the raw per-line regex the old critique exclusivity check ran (`sqlite3\\.connect` on the old
suffix set, allowed files skipped, every hit counted as definitive), on the orders_app only_in cases.
usage: python benchmarks/results/decide-2026-09-25/guard_mutations.py  (writes guard_mutations.json)
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
V = HERE.parents[2]
sys.path.insert(0, str(V))
WORK = Path(tempfile.mkdtemp(prefix="decide-bench-"))

from verinoda import decisions as dm  # noqa: E402
from verinoda import guards, index, workflow  # noqa: E402
from verinoda.snapshot import list_files  # noqa: E402
from verinoda.store import open_store  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def rmtree(p: Path) -> None:
    def onerr(fn, path, _exc):
        os.chmod(path, stat.S_IWRITE)
        fn(path)

    shutil.rmtree(p, onerror=onerr)


def make(name: str, src: Path) -> Path:
    """A fresh git copy of an example under the work folder, scanned."""
    from verinoda import workflow
    from verinoda.store import open_store

    dst = WORK / name
    if dst.exists():
        rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".verinoda", "__pycache__", "*.pyc", ".pytest_cache",
                                                            "*.db", ".git"))
    _git(dst, "init", "-q")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "init")
    workflow.init(dst)
    st = open_store(dst)
    try:
        workflow.scan(st, dst)
    finally:
        st.close()
    return dst

ORD_G = ["only_in calls=sqlite3.connect allowed=orders/repository.py"]
SINK_G = ["only_in sink=db-connection allowed=orders/repository.py"]
GLOW_G = ["no_edge from=src/main/** to=src/client/**"]
NET = "src/main/java/net/ashvale/emberforge/network/EmberNetwork.java"
FORGE_G = [f"only_in calls=PayloadRegistrar.playToServer,PayloadRegistrar.playToClient allowed={NET}"]
EV = "src/main/java/net/ashvale/emberforge/event/ModEvents.java"
GM = "src/main/java/com/example/glowmod/GlowMod.java"


def py(body: str) -> str:
    return body.strip("\n") + "\n"


def edit(rel: str, old: str, new: str):
    def f(repo: Path) -> None:
        p = repo / rel
        t = p.read_text(encoding="utf-8")
        assert old in t, (rel, old)
        p.write_bytes(t.replace(old, new, 1).encode("utf-8"))
    return f


R = "orders/reports.py"
CASES = [
    # -- orders_app only_in calls=sqlite3.connect: violating ------------------------------------
    ("V01 direct call", "orders", ORD_G, {R: py("import sqlite3\n\ndef daily(u):\n    return sqlite3.connect(u)  # <<")},
     "violating", []),
    ("V02 from-import alias", "orders", ORD_G, {R: py("from sqlite3 import connect as open_db\n\ndef d(u):\n"
                                                      "    return open_db(u)  # <<")}, "violating", []),
    ("V03 module alias", "orders", ORD_G, {R: py("import sqlite3 as sq\n\ndef d(u):\n    return sq.connect(u)  # <<")},
     "violating", []),
    ("V04 from-import", "orders", ORD_G, {R: py("from sqlite3 import connect\n\ndef d(u):\n    return connect(u)  # <<")},
     "violating", []),
    ("V05 assigned alias", "orders", ORD_G, {R: py("import sqlite3\n\nopener = sqlite3.connect\n\n\ndef d(u):\n"
                                                   "    return opener(u)  # <<")}, "violating", []),
    ("V06 local import", "orders", ORD_G, {R: py("def d(u):\n    import sqlite3\n    return sqlite3.connect(u)  # <<")},
     "violating", []),
    ("V07 re-export through a project module", "orders", ORD_G,
     {"orders/dbutil.py": py("from sqlite3 import connect as open_db  # re-exported, not called here"),
      R: py("from orders.dbutil import open_db\n\ndef d(u):\n    return open_db(u)  # <<")}, "violating", []),
    ("V08 existing module edited", "orders", ORD_G,
     {"orders/service.py": edit("orders/service.py", "from orders.config import MAX_ITEMS_PER_ORDER",
                                "import sqlite3\n\nfrom orders.config import MAX_ITEMS_PER_ORDER"),
      "orders/service.py#2": edit("orders/service.py", "    validate_items(items)\n",
                                  "    validate_items(items)\n    sqlite3.connect(':memory:')  # <<\n")},
     "violating", []),
    ("V09 multi-line call", "orders", ORD_G, {R: py("import sqlite3\n\ndef d(u):\n    return sqlite3.connect(  # <<\n"
                                                    "        u,\n        timeout=5,\n    )")}, "violating", []),
    ("V10 relative re-export", "orders", ORD_G,
     {"orders/dbutil.py": py("from sqlite3 import connect as open_db"),
      R: py("from .dbutil import open_db\n\ndef d(u):\n    return open_db(u)  # <<")}, "violating", []),
    ("V11 lambda", "orders", ORD_G, {R: py("import sqlite3\n\nrun = lambda u: sqlite3.connect(u)  # <<")},
     "violating", []),
    ("V12 method in a class", "orders", ORD_G, {R: py("import os, sqlite3\n\nclass Rep:\n    def open(self, u):\n"
                                                      "        return sqlite3.connect(u)  # <<")}, "violating", []),
    ("V13 inside an f-string", "orders", ORD_G, {R: py("import sqlite3\n\ndef d(u):\n"
                                                       "    return f\"{sqlite3.connect(u)}\"  # <<")},
     "violating", []),
    ("V14 sink db-connection: psycopg", "orders", SINK_G, {R: py("import psycopg\n\ndef d(dsn):\n"
                                                                 "    return psycopg.connect(dsn)  # <<")},
     "violating", []),
    ("V15 sink db-connection: alias of sqlite3", "orders", SINK_G, {R: py("import sqlite3 as db\n\ndef d(u):\n"
                                                                          "    return db.connect(u)  # <<")},
     "violating", []),
    # -- benign ---------------------------------------------------------------------------------
    ("B01 comment only", "orders", ORD_G, {R: py("# never call sqlite3.connect here: use OrderRepository\n\n"
                                                 "def d():\n    return 0")}, "benign", []),
    ("B02 docstring only", "orders", ORD_G, {R: py('def d():\n    """Unlike sqlite3.connect(...), this reads a '
                                                   'cache.\n\n    sqlite3.connect is never called.\n    """\n'
                                                   '    return 0')}, "benign", []),
    ("B03 string literal", "orders", ORD_G, {R: py("MSG = 'call sqlite3.connect(url) only in the repository'")},
     "benign", []),
    ("B04 test file (out of scope)", "orders", ORD_G, {"tests/test_reports.py": py(
        "import sqlite3\n\ndef test_x():\n    sqlite3.connect(':memory:')")}, "benign", []),
    ("B05 own function named connect", "orders", ORD_G, {R: py("def connect(u):\n    return u\n\n\n"
                                                               "def d():\n    return connect(1)")}, "benign", []),
    ("B06 socket connect", "orders", ORD_G, {R: py("import socket\n\ndef d():\n    s = socket.socket()\n"
                                                   "    s.connect(('h', 1))\n    return socket.create_connection(('h', 1))")},
     "benign", []),
    ("B07 another package's connect", "orders", ORD_G, {R: py("from redis import connect\n\ndef d():\n"
                                                              "    return connect()")}, "benign", []),
    ("B08 parameter shadows the module", "orders", ORD_G, {R: py("def d(sqlite3):\n    return sqlite3.connect('x')")},
     "benign", []),
    ("B09 the allowed file itself", "orders", ORD_G, {"orders/repository.py": edit(
        "orders/repository.py", "        self.conn = sqlite3.connect(url)",
        "        self.conn = sqlite3.connect(url)\n        self.ro = sqlite3.connect(url)")}, "benign", []),
    ("B10 the repository API", "orders", ORD_G, {R: py("from orders.repository import OrderRepository\n\n"
                                                       "def d():\n    return OrderRepository().get(1)")}, "benign", []),
    ("B11 unrelated object named sq", "orders", ORD_G, {R: py("class Helper:\n    def connect(self):\n"
                                                              "        return 1\n\n\ndef d():\n    sq = Helper()\n"
                                                              "    return sq.connect()")}, "benign", []),
    ("B12 import without a call", "orders", ORD_G, {R: py("import sqlite3\n\nROW = sqlite3.Row")}, "benign", []),
    # added after step 4 (a false VIOLATED found by reading the engine, not by this set)
    ("B20 a def shadows the imported name", "orders", ORD_G, {R: py("from sqlite3 import connect\n\n\n"
                                                                    "def connect(u):\n    return u\n\n\n"
                                                                    "def d():\n    return connect(1)")},
     "benign", []),
    # -- out of reach ---------------------------------------------------------------------------
    ("O01 getattr with a literal name", "orders", ORD_G, {R: py("import sqlite3\n\ndef d(u):\n"
                                                                "    return getattr(sqlite3, 'connect')(u)")},
     "out_of_reach", ["getattr"]),
    ("O02 getattr with a computed name", "orders", ORD_G, {R: py("import sqlite3\n\nNAME = 'con' + 'nect'\n\n\n"
                                                                 "def d(u):\n    return getattr(sqlite3, NAME)(u)")},
     "out_of_reach", ["computed name"]),
    ("O03 importlib", "orders", ORD_G, {R: py("import importlib\n\ndef d(u):\n"
                                              "    return importlib.import_module('sqlite3').connect(u)")},
     "out_of_reach", ["importlib"]),
    ("O04 __import__", "orders", ORD_G, {R: py("def d(u):\n    return __import__('sqlite3').connect(u)")},
     "out_of_reach", ["__import__"]),
    ("O05 exec", "orders", ORD_G, {R: py("def d(u):\n    exec('import sqlite3; sqlite3.connect(u)')")},
     "out_of_reach", ["exec"]),
    ("O06 sys.modules", "orders", ORD_G, {R: py("import sys\nimport sqlite3  # noqa\n\ndef d(u):\n"
                                                "    return sys.modules['sqlite3'].connect(u)")},
     "out_of_reach", ["sys.modules"]),
    ("O07 star import", "orders", ORD_G, {R: py("from sqlite3 import *\n\ndef d(u):\n    return connect(u)")},
     "out_of_reach", ["star imports"]),
    # -- glow_mod no_edge src/main/** -> src/client/** ------------------------------------------
    ("V16 import + call in common code", "glow", GLOW_G,
     {GM: edit(GM, "import com.example.glowmod.command.GlowCommands;",
               "import com.example.glowmod.client.GlowModClient; // <<\nimport com.example.glowmod.command.GlowCommands;"),
      GM + "#2": edit(GM, "        LOGGER.info", "        new GlowModClient().onInitializeClient();\n        LOGGER.info")},
     "violating", []),
    ("V17 unused import in another common file", "glow", GLOW_G,
     {"src/main/java/com/example/glowmod/item/ModItems.java": edit(
         "src/main/java/com/example/glowmod/item/ModItems.java", "package com.example.glowmod.item;",
         "package com.example.glowmod.item;\n\nimport com.example.glowmod.client.GlowModClient; // <<")},
     "violating", []),
    ("B13 comment naming client code", "glow", GLOW_G,
     {GM: edit(GM, "        LOGGER.info", "        // GlowModClient.onInitializeClient() is client only\n"
                   "        LOGGER.info")}, "benign", []),
    ("B14 client code using common code", "glow", GLOW_G,
     {"src/client/java/com/example/glowmod/client/GlowModClient.java": edit(
         "src/client/java/com/example/glowmod/client/GlowModClient.java",
         "import com.example.glowmod.network.SummonWispPayload;",
         "import com.example.glowmod.network.SummonWispPayload;\nimport com.example.glowmod.GlowMod;")},
     "benign", []),
    ("B15 string naming client code", "glow", GLOW_G,
     {GM: edit(GM, "        LOGGER.info(\"Glow Mod ready\");",
               "        LOGGER.info(\"Glow Mod ready; GlowModClient loads on the client\");")}, "benign", []),
    ("O08 reflection by class name", "glow", GLOW_G,
     {GM: edit(GM, "        LOGGER.info", "        try { Class.forName(\"com.example.glowmod.client.GlowModClient\"); }"
                   " catch (Exception e) { }\n        LOGGER.info")}, "out_of_reach", ["string class loading"]),
    # -- forge_mod only_in PayloadRegistrar.playTo* (Java / Kotlin) --------------------------------
    ("V18 registration moved to ModEvents (declared type)", "forge", FORGE_G,
     {EV: edit(EV, "import net.neoforged.neoforge.event.RegisterCommandsEvent;",
               "import net.neoforged.neoforge.event.RegisterCommandsEvent;\n"
               "import net.neoforged.neoforge.network.event.RegisterPayloadHandlersEvent;\n"
               "import net.neoforged.neoforge.network.registration.PayloadRegistrar;"),
      EV + "#2": edit(EV, "    /** Punching", "    @SubscribeEvent\n    public static void onPayloads("
                      "RegisterPayloadHandlersEvent event) {\n        PayloadRegistrar registrar = event.registrar(\"1\");\n"
                      "        registrar.playToClient(A.TYPE, A.CODEC, ModEvents::h); // <<\n    }\n\n    /** Punching")},
     "violating", []),
    ("V19 static field of the class", "forge", FORGE_G,
     {EV: edit(EV, "import net.neoforged.neoforge.event.RegisterCommandsEvent;",
               "import net.neoforged.neoforge.event.RegisterCommandsEvent;\n"
               "import net.neoforged.neoforge.network.registration.PayloadRegistrar;"),
      EV + "#2": edit(EV, "    private ModEvents() {", "    static PayloadRegistrar REG;\n\n    static void late() {\n"
                      "        REG.playToServer(B.TYPE, B.CODEC, ModEvents::h); // <<\n    }\n\n    private ModEvents() {")},
     "violating", []),
    ("V20 Kotlin parameter of the class", "forge", FORGE_G,
     {"src/main/kotlin/net/ashvale/emberforge/heat/HeatMath.kt": edit(
         "src/main/kotlin/net/ashvale/emberforge/heat/HeatMath.kt", "package net.ashvale.emberforge.heat",
         "package net.ashvale.emberforge.heat\n\nimport net.neoforged.neoforge.network.registration.PayloadRegistrar\n\n"
         "fun wire(r: PayloadRegistrar) {\n    r.playToServer(T, C) // <<\n}")}, "violating", []),
    ("B16 comment in ModEvents", "forge", FORGE_G,
     {EV: edit(EV, "    private ModEvents() {", "    // registrar.playToServer(X.TYPE, ...) lives in EmberNetwork\n"
                                                 "    private ModEvents() {")}, "benign", []),
    ("B17 another class's playToServer", "forge", FORGE_G,
     {EV: edit(EV, "    private ModEvents() {", "    static final class MyReg { void playToServer(int x) { } }\n\n"
                                                 "    static void f() {\n        MyReg reg = new MyReg();\n"
                                                 "        reg.playToServer(1);\n    }\n\n    private ModEvents() {")},
     "benign", []),
    ("B18 the allowed file itself", "forge", FORGE_G,
     {NET: edit(NET, "        registrar.playToServer(", "        registrar.playToClient(StokeForgePayload.TYPE, "
                                                         "StokeForgePayload.STREAM_CODEC, EmberNetwork::handleStoke);\n"
                                                         "        registrar.playToServer(")}, "benign", []),
    ("O09 chained receiver", "forge", FORGE_G,
     {EV: edit(EV, "    private ModEvents() {", "    static void f(net.neoforged.neoforge.network.event."
                                                 "RegisterPayloadHandlersEvent e) {\n        e.registrar(\"2\")."
                                                 "playToServer(Y.TYPE, Y.CODEC, ModEvents::h);\n    }\n\n"
                                                 "    private ModEvents() {")}, "out_of_reach", ["receiver"]),
    # added after the first run (a bug found outside the set: a file-node target labelled "X.kt")
    ("V25 Java imports Kotlin client code", "forge",
     ["no_edge from=src/main/java/** to=src/main/kotlin/net/ashvale/emberforge/client/**"],
     {EV: edit(EV, "import net.ashvale.emberforge.EmberForge;",
               "import net.ashvale.emberforge.EmberForge;\nimport net.ashvale.emberforge.client.EmberForgeScreen; // <<")},
     "violating", []),
    # -- dependencies ----------------------------------------------------------------------------
    ("V21 psycopg declared in pyproject", "orders", ["dependency absent=psycopg"],
     {"pyproject.toml": edit("pyproject.toml", 'requires-python = ">=3.10"',
                             'requires-python = ">=3.10"\ndependencies = [\n    "psycopg>=3.1",  # <<\n]')},
     "violating", []),
    ("V22 psycopg in requirements.txt", "orders", ["dependency absent=psycopg"],
     {"requirements.txt": "psycopg[binary]==3.2.1  # <<\n"}, "violating", []),
    ("V23 sqlite-jdbc in build.gradle", "forge", ["dependency absent=org.xerial:sqlite-jdbc"],
     {"build.gradle": edit("build.gradle", "dependencies {\n", "dependencies {\n"
                           "    implementation 'org.xerial:sqlite-jdbc:3.45.0.0' // <<\n")}, "violating", []),
    ("B19 psycopg only in a comment and the README", "orders", ["dependency absent=psycopg"],
     {"pyproject.toml": edit("pyproject.toml", 'requires-python = ">=3.10"',
                             'requires-python = ">=3.10"\n# psycopg would replace sqlite3 here'),
      "README.md": edit("README.md", "# orders_app", "# orders_app\n\nNo psycopg yet.")}, "benign", []),
    # -- JDBC as a db-connection sink ----------------------------------------------------------------
    ("V24 JDBC DriverManager in Java", "forge", [f"only_in sink=db-connection allowed={NET}"],
     {"src/main/java/net/ashvale/emberforge/util/ForgeFunctions.java": edit(
         "src/main/java/net/ashvale/emberforge/util/ForgeFunctions.java", "\n}",
         "\n    static Object db(String u) throws Exception {\n"
         "        return java.sql.DriverManager.getConnection(u); // <<\n    }\n}")}, "violating", []),
]


def template(name: str) -> Path:
    src = {"orders": "orders_app", "glow": "glow_mod", "forge": "forge_mod"}[name]
    tpl = WORK / f"tpl_{name}"
    if not (tpl / ".verinoda" / "index" / "graph.json").exists():
        make(f"tpl_{name}", V / "examples" / src)
    return tpl


def marked(repo: Path) -> set[tuple[str, int]]:
    out = set()
    for rel in list_files(repo):
        if rel.startswith(".verinoda"):
            continue
        try:
            lines = (repo / rel).read_text(encoding="utf-8").split("\n")
        except (OSError, UnicodeDecodeError):
            continue
        for i, ln in enumerate(lines, 1):
            if re.search(r"(#|//) <<", ln):
                out.add((rel, i))
    return out


def raw_regex(repo: Path, allowed: set[str]) -> set[tuple[str, int]]:
    rx = re.compile(r"sqlite3\.connect")
    hits = set()
    for rel in list_files(repo):
        if rel in allowed or Path(rel).suffix not in (".py", ".js", ".ts", ".go", ".rs", ".java", ".rb", ".php", ".cs"):
            continue
        for i, ln in enumerate((repo / rel).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if rx.search(ln):
                hits.add((rel, i))
    return hits


def main() -> None:
    work = WORK / "bench"
    if work.exists():
        rmtree(work)
    work.mkdir(parents=True)
    rows = []
    for n, (cid, fx, specs, changes, cls, words) in enumerate(CASES):
        repo = work / f"c{n:02d}"
        shutil.copytree(template(fx), repo)
        st = open_store(repo)
        try:
            dm.record(st, repo, chosen=cid, rationale="bench", guards=specs)
            for rel, ch in changes.items():
                if callable(ch):
                    ch(repo)
                else:
                    p = repo / rel.split("#")[0]
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(ch.encode("utf-8"))
            graph = None
            if any(s.startswith("no_edge") for s in specs):
                workflow.update(st, repo)
                graph = index.load(repo)
        finally:
            st.close()
        t0 = time.perf_counter()
        res = guards.check(repo, graph=graph)
        dt = time.perf_counter() - t0
        viol = {(f["at"].rpartition(":")[0], int(f["at"].rpartition(":")[2])) if re.search(r":\d+$", f["at"])
                else (f["at"], 0) for f in res["violations"]}
        # a dependency "present" guard cites a manifest without a line; not used here
        expect = marked(repo) if cls == "violating" else set()
        limits_text = " ".join(" ".join(o.get("limits") or []) for o in res["ok"]) + " " + \
            " ".join(" ".join(f.get("limits") or []) + " " + f["why"] for f in res["possible"])
        row = {"case": cid, "class": cls, "violated": sorted(f"{r}:{i}" for r, i in viol),
               "expected": sorted(f"{r}:{i}" for r, i in expect),
               "possible": [f["at"] for f in res["possible"]], "ms": round(dt * 1000, 1),
               "tp": len(viol & expect), "fp": len(viol - expect), "fn": len(expect - viol)}
        if cls == "out_of_reach":
            row["named_in_limits"] = all(w.lower() in limits_text.lower() for w in words)
        if fx == "orders" and specs == ORD_G:
            base = raw_regex(repo, {"orders/repository.py"})
            row["regex"] = {"tp": len(base & expect), "fp": len(base - expect),
                            "fn": len(expect - base), "hits": sorted(f"{r}:{i}" for r, i in base)}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False))
    vt = sum(r["tp"] for r in rows)
    vf = sum(r["fp"] for r in rows)
    in_reach = [r for r in rows if r["class"] == "violating"]
    sites = sum(len(r["expected"]) for r in in_reach)
    summary = {
        "cases": len(rows), "violating": len(in_reach), "benign": sum(r["class"] == "benign" for r in rows),
        "out_of_reach": sum(r["class"] == "out_of_reach" for r in rows),
        "violated_precision": round(vt / (vt + vf), 3) if vt + vf else None,
        "recall_sites_in_reach": round(vt / sites, 3) if sites else None, "tp": vt, "fp": vf,
        "fn_sites": sum(r["fn"] for r in in_reach),
        "cases_fully_found": sum(r["fn"] == 0 and r["fp"] == 0 for r in in_reach),
        "benign_with_violated": [r["case"] for r in rows if r["class"] == "benign" and r["violated"]],
        "out_of_reach_named": sum(bool(r.get("named_in_limits")) for r in rows if r["class"] == "out_of_reach"),
        "out_of_reach_with_violated": [r["case"] for r in rows if r["class"] == "out_of_reach" and r["violated"]],
        "check_ms_max": max(r["ms"] for r in rows), "check_ms_median": sorted(r["ms"] for r in rows)[len(rows) // 2],
    }
    rx_rows = [r["regex"] for r in rows if "regex" in r]
    exp_rows = [r for r in rows if "regex" in r]
    summary["baseline_raw_regex_on_orders_calls_cases"] = {
        "cases": len(rx_rows), "tp": sum(x["tp"] for x in rx_rows), "fp": sum(x["fp"] for x in rx_rows),
        "fn": sum(x["fn"] for x in rx_rows),
        "fp_in_benign_or_out_of_reach": sum(len(x["hits"]) for x, r in zip(rx_rows, exp_rows) if r["class"] != "violating")}
    eng = [r for r in rows if "regex" in r]
    summary["engine_on_the_same_cases"] = {"tp": sum(r["tp"] for r in eng), "fp": sum(r["fp"] for r in eng),
                                           "fn": sum(r["fn"] for r in eng)}
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    (HERE / "guard_mutations.json").write_bytes((json.dumps({"rows": rows, "summary": summary}, indent=1,
                                                            ensure_ascii=False) + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
