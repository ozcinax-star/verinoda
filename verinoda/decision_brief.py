"""Decision briefs (docs/DESIGN.md D33): what a human needs in order to decide, collected from the code.

A brief never recommends and has no score. It holds:

* ``forces`` - facts from the code, each with evidence that re-checks now (``path:line`` and the text
  that must be on it) and the probe that found it;
* ``absences`` - what was looked for and not found, with the globs or patterns searched and the scope
  ("no Dockerfile in the repository", never "not containerised");
* ``existing_decisions`` - decision documents and records that bear on the question, with their
  sentences that carry a quantifier or must/never;
* ``options`` - the options the user named (or the project uses), whether each is present in the
  project (evidence), the code a change would touch, constraints from installed metadata, and external
  claims only as quote-checked pins; the agent's own arguments are ``weak_inference``;
* ``questions_for_human`` - at most five, in English and Turkish, each saying why it is asked and what
  it would decide between; a question the code answers is never asked;
* ``verdict`` - always ``human_decision_required``.

Probes (each serves some decision kinds): P1 storage sinks and how concentrated they are, P2
environment and configuration reads with their defaults, P3 declared dependencies (Python, npm, Go,
Cargo, Gradle, Maven) and ``requires-python``, P4 decision documents, P5 deployment and CI files, P6
concurrency signals and module-level shared state, P7 tests that pin the implementation, P8 churn from
git, P9 numeric limits in configuration.

External facts: ``quotes`` (``{url, text}``) are fetched through :mod:`verinoda.research` as the
project's ``research.network`` allows; a quote counts only if it occurs verbatim in the fetched text,
and even then it says what the document says, not that it applies here. Without the network the pin
is ``unknown``. Nothing of the analysed project is imported or run.
"""

from __future__ import annotations

import ast
import re
import time
from pathlib import Path, PurePosixPath

from verinoda import textnorm as tn

HUMAN = "human_decision_required"
MAX_QUESTIONS = 5
KINDS = ("datastore", "dependency", "boundary", "scaling", "other")
KIND_RX = {
    "datastore": re.compile(r"\b(database|databases|db|dbs|sql\w*|sqlite\w*|postgres\w*|mysql|mariadb|mongo\w*|"
                            r"redis|dynamodb|cassandra|duckdb|orm|sqlalchemy|storage|store|persist\w*|"
                            r"veritaban\w*|depola\w*|kalici\w*|tablo\w*)\b"),
    "scaling": re.compile(r"\b(scal\w*|traffic|grow\w*|load|throughput|concurren\w*|\d+x|times more|"
                          r"artarsa|artinca|buyut\w*|olcek\w*|trafik|yuk|eszamanli)\b"),
    "dependency": re.compile(r"\b(librar\w*|framework\w*|package\w*|dependenc\w*|upgrade\w*|version\w*|"
                             r"migrat\w*|kutuphane\w*|bagimlilik\w*|surum\w*|fabric|neoforge|forge|quilt|spring|"
                             r"django|flask|fastapi|kotlin|java|toml|yaml|json|format|broker\w*|queue\w*|kuyruk\w*|"
                             r"kafka|rabbitmq|celery)\b"),
    "boundary": re.compile(r"\b(module\w*|service\w*|layer\w*|split\w*|separat\w*|extract\w*|interface\w*|"
                           r"boundar\w*|client|server|package\w*|modul\w*|ayir\w*|katman\w*|servis\w*|istemci\w*)\b"),
}
# option name (folded) -> (kind, dependency names that make it present, Python modules that make it present)
KNOWN_OPTIONS: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "sqlite": ("datastore", (), ("sqlite3", "aiosqlite")),  # in Python's standard library: nothing to declare
    "postgresql": ("datastore", ("psycopg", "psycopg2", "psycopg2-binary", "asyncpg", "pg8000"),
                   ("psycopg", "psycopg2", "asyncpg", "pg8000")),
    "postgres": ("datastore", ("psycopg", "psycopg2", "psycopg2-binary", "asyncpg", "pg8000"),
                 ("psycopg", "psycopg2", "asyncpg", "pg8000")),
    "mysql": ("datastore", ("pymysql", "mysqlclient", "mysql-connector-python"), ("pymysql", "MySQLdb", "mysql")),
    "mariadb": ("datastore", ("mariadb", "pymysql"), ("mariadb", "pymysql")),
    "mongodb": ("datastore", ("pymongo", "motor"), ("pymongo", "motor")),
    "mongo": ("datastore", ("pymongo", "motor"), ("pymongo", "motor")),
    "redis": ("datastore", ("redis",), ("redis",)),
    "duckdb": ("datastore", ("duckdb",), ("duckdb",)),
    "dynamodb": ("datastore", ("boto3",), ("boto3",)),
    "sqlalchemy": ("dependency", ("sqlalchemy",), ("sqlalchemy",)),
    "django": ("dependency", ("django",), ("django",)),
    "kafka": ("dependency", ("kafka-python", "confluent-kafka", "aiokafka"), ("kafka", "confluent_kafka", "aiokafka")),
    "rabbitmq": ("dependency", ("pika", "aio-pika"), ("pika", "aio_pika")),
    "celery": ("dependency", ("celery",), ("celery",)),
    "fabric": ("dependency", ("fabric-loom", "fabric-api"), ()),
    "neoforge": ("dependency", ("neoforge", "net.neoforged.moddev"), ()),
    "kotlin": ("dependency", ("kotlinforforge-neoforge", "kotlin-stdlib"), ()),
    "toml": ("dependency", ("tomli", "toml"), ("tomllib", "tomli", "toml")),
    "yaml": ("dependency", ("pyyaml", "ruamel.yaml"), ("yaml", "ruamel")),
}
# Java/Kotlin packages that make an option present (and whose importers a change would touch)
JVM_PREFIXES = {"neoforge": ("net.neoforged",), "fabric": ("net.fabricmc",), "forge": ("net.minecraftforge",),
                "quilt": ("org.quiltmc",), "spring": ("org.springframework",), "kotlin": ("kotlin.",),
                "postgresql": ("org.postgresql",), "mysql": ("com.mysql",), "sqlite": ("org.sqlite",)}
DISPLAY = {"sqlite": "SQLite", "postgresql": "PostgreSQL", "postgres": "PostgreSQL", "mysql": "MySQL",
           "mariadb": "MariaDB", "mongodb": "MongoDB", "mongo": "MongoDB", "redis": "Redis", "duckdb": "DuckDB",
           "dynamodb": "DynamoDB", "sqlalchemy": "SQLAlchemy", "django": "Django", "kafka": "Kafka",
           "rabbitmq": "RabbitMQ", "celery": "Celery", "fabric": "Fabric", "neoforge": "NeoForge", "kotlin": "Kotlin",
           "toml": "TOML", "yaml": "YAML"}
DRIVER_DEPS = ("psycopg", "psycopg2", "psycopg2-binary", "asyncpg", "pg8000", "pymysql", "mysqlclient",
               "mysql-connector-python", "mariadb", "pymongo", "motor", "redis", "sqlalchemy", "peewee", "tortoise-orm",
               "django", "aiosqlite", "duckdb", "boto3", "org.xerial:sqlite-jdbc", "org.postgresql:postgresql",
               "mysql:mysql-connector-java", "com.h2database:h2", "org.hibernate:hibernate-core")
DEPLOY_GLOBS = ("Dockerfile", "Dockerfile.*", "*.dockerfile", "docker-compose*.yml", "docker-compose*.yaml",
                "compose*.yml", "compose*.yaml", "Procfile", "k8s/**", "kubernetes/**", "helm/**", "charts/**",
                "Chart.yaml", "*.tf", "fly.toml", "app.yaml", "render.yaml", "vercel.json", "netlify.toml",
                "serverless.yml", ".github/workflows/*", ".gitlab-ci.yml", "Jenkinsfile", "azure-pipelines.yml",
                ".circleci/config.yml")
CONCURRENCY_MODULES = ("threading", "asyncio", "multiprocessing", "concurrent.futures", "gevent", "trio", "anyio")
SERVER_DEPS = ("flask", "fastapi", "django", "gunicorn", "uvicorn", "aiohttp", "tornado", "starlette", "sanic",
               "waitress", "hypercorn", "falcon", "bottle")
TEST_PIN_RX = re.compile(r"[\"']:memory:[\"']|\bsqlite3?\b|\.db[\"']|DATABASE_URL|\bpostgres\w*\b|\bmysql\b|"
                         r"\bmongo\w*\b|\bredis\b", re.I)
DEFAULT_RX = re.compile(r"(?:os\.environ\.get|os\.getenv)\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*,\s*"
                        r"(['\"][^'\"]*['\"]|-?\d+(?:\.\d+)?|None|True|False)")
KIND_ENV = {"datastore": re.compile(r"DATABASE|\bDB|DB_|DSN|SQL|STORE|STORAGE|PERSIST|REDIS|MONGO|POSTGRES|_URL$"),
            "scaling": re.compile(r"WORKER|THREAD|POOL|MAX|LIMIT|TIMEOUT|CONCURREN|QUEUE|BATCH")}
ABSENCE_SERVES = {"P1": ("datastore", "scaling", "boundary"), "P2": ("datastore", "scaling"),
                  "P3": ("datastore", "dependency"), "P4": KINDS, "P5": ("datastore", "scaling", "other"),
                  "P6": ("datastore", "scaling")}
LIMITS = ["Verinoda has no load model: performance and scale behaviour are not predicted; absent metrics stay absent",
          "storage sinks are found by patterns (Python/SQL-centric) and connection calls bound through imports; "
          "an ORM or driver the patterns do not know is not seen",
          "deployment may be configured outside the repository (another repository, a platform's settings)",
          "external facts count only as quote-checked pins; the agent's own arguments are weak_inference at most",
          "no recommendation is made: the choice, and every fact about load, budget, hosting and the team, is the "
          "human's"]


# -- small helpers ---------------------------------------------------------------------------------

def _read_lines(repo: Path, rel: str) -> list[str]:
    try:
        return (Path(repo) / rel).read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n").split("\n")
    except OSError:
        return []


def _ev(repo: Path, rel: str, a: int, b: int | None = None, *, needle: str, source_type: str = "source_code"
        ) -> dict | None:
    """Evidence for ``rel:a[-b]``: only when ``needle`` (a regex) occurs in those lines now."""
    lines = _read_lines(repo, rel)
    b = b or a
    if not (0 < a <= b <= len(lines)):
        return None
    shown = "\n".join(lines[a - 1:b])
    if not re.search(needle, shown):
        return None
    return {"locator": f"{rel}:{a}" + (f"-{b}" if b != a else ""), "source_type": source_type,
            "excerpt": lines[a - 1].strip()[:160], "needle": needle}


def recheck(repo: Path, ev: dict) -> bool:
    """Does an evidence item of a brief still hold (its lines exist and carry its needle)?"""
    loc = ev.get("locator") or ""
    m = re.match(r"^(.+?):(\d+)(?:-(\d+))?$", loc)
    if not m or not ev.get("needle"):
        return ev.get("source_type") == "git_history"
    return _ev(repo, m.group(1), int(m.group(2)), int(m.group(3) or m.group(2)), needle=ev["needle"]) is not None


def _fold(text: str) -> str:
    return tn.fold_tr(tn.nfc(text or "")).lower()


def decision_kinds(question: str, options: list[str] = ()) -> list[str]:
    """Kinds of the decision, by rules over the question and the option names (``derived_by: rules``)."""
    text = _fold(" ".join([question, *options]))
    found = [k for k in ("datastore", "scaling", "dependency", "boundary") if KIND_RX[k].search(text)]
    for o in options:
        known = KNOWN_OPTIONS.get(_fold(o).replace(" ", ""))
        if known and known[0] not in found:
            found.append(known[0])
    return found or ["other"]


def options_from_question(question: str) -> list[str]:
    """Option names the user wrote (known technology names), as written, in order."""
    out = []
    for m in re.finditer(r"[A-Za-zÀ-ɏ][\w.+-]*", question or ""):
        w = m.group(0)
        base = re.split(r"['’]", w)[0]
        key = _fold(base).rstrip(".")
        for k in KNOWN_OPTIONS:
            if key == k or (key.startswith(k) and len(key) - len(k) <= 3 and k in ("postgresql", "sqlite", "mysql")):
                if base not in out and not any(_fold(x) == key for x in out):
                    out.append(base)
                break
    return out


# -- probes ------------------------------------------------------------------------------------------

class _Probe:
    def __init__(self, repo: Path, graph=None):
        from verinoda.architecture_map import is_test_file
        from verinoda.snapshot import list_files

        self.repo = Path(repo).resolve()
        self.graph = graph
        self.files = list_files(self.repo)
        from verinoda import guards

        self.guards = guards
        roots = guards._excluded_roots(self.repo)
        self.product = [f for f in self.files if f.endswith(guards.CODE_SUFFIXES) and not is_test_file(f)
                        and not f.startswith(".verinoda/") and not any(f == r or f.startswith(r + "/") for r in roots)]
        self.tests = [f for f in self.files if is_test_file(f) and f.endswith(guards.CODE_SUFFIXES)]
        self.forces: list[dict] = []
        self.absences: list[dict] = []
        self.facts: dict = {}
        self._lines: dict[str, list[str]] = {}

    def lines(self, rel: str) -> list[str]:
        if rel not in self._lines:
            self._lines[rel] = _read_lines(self.repo, rel)
        return self._lines[rel]

    def force(self, probe: str, fact: str, evidence: list[dict | None], *, status: str = "statically_verified",
              serves: tuple[str, ...] = KINDS, **extra) -> dict | None:
        evs = [e for e in evidence if e]
        if not evs:
            return None
        f = {"id": f"f{len(self.forces) + 1}", "probe": probe, "fact": fact, "status": status, "evidence": evs,
             "serves": list(serves), **extra}
        self.forces.append(f)
        return f

    def absence(self, probe: str, what: str, searched: list[str], scope_note: str, **extra) -> None:
        self.absences.append({"id": f"a{len(self.absences) + 1}", "probe": probe, "what": what,
                              "searched": searched, "scope_note": scope_note, **extra})

    def symbol_at(self, rel: str, line: int) -> str | None:
        """The qualified name of the innermost definition around ``rel:line`` (symbol facts)."""
        from verinoda import anchors

        facts, _ = anchors.facts_for_path(self.repo / rel, rel)
        enc = anchors.enclosing(facts, line) if facts else None
        return enc[1].split("#")[0] if enc and enc[0] == "sym" else None

    # P1 -------------------------------------------------------------------------------------------
    def storage(self) -> None:
        from verinoda.architecture_map import SINK_PATTERNS

        g = {"kind": "only_in", "sink": "db-connection", "allowed": [], "scope": "product"}
        ctx = self.guards._Ctx(self.repo, self.files)
        hits, _scan, _ = self.guards.check_only_in(ctx, g)
        conns = [(rel, line, why) for level, rel, line, why in hits if level == self.guards.VIOLATED]
        for rel, line, why in conns[:4]:
            call = re.search(r"call to ([\w.]+)", why)
            name = call.group(1) if call else "a database driver"
            sym = self.symbol_at(rel, line)
            self.force("P1", f"a database connection is opened with {name} at {rel}:{line}"
                             + (f" (in {sym})" if sym else ""),
                       [_ev(self.repo, rel, line, needle=re.escape(name.rpartition('.')[2]))],
                       serves=("datastore", "scaling", "dependency"))
        sinks: list[tuple[str, int, str]] = []
        any_sink = re.compile("|".join(f"(?:{rx.pattern})" for rx, _ in SINK_PATTERNS), re.I)
        for rel in self.product:
            if not any_sink.search("\n".join(self.lines(rel))):
                continue
            for i, ln in enumerate(self.lines(rel), 1):
                for rx, kind in SINK_PATTERNS:
                    if rx.search(ln):
                        sinks.append((rel, i, kind))
                        break
        self.facts["sinks"] = sinks
        self.facts["connections"] = conns
        files = sorted({s[0] for s in sinks})
        pats = [kind for _rx, kind in SINK_PATTERNS]
        if sinks:
            kinds = sorted({s[2] for s in sinks})
            evs = [_ev(self.repo, rel, i, needle=next(rx.pattern for rx, k in SINK_PATTERNS if k == kind))
                   for rel, i, kind in sinks[:8]]
            where = files[0] if len(files) == 1 else f"{len(files)} files ({', '.join(files[:4])})"
            self.force("P1", f"all {len(sinks)} storage sink line(s) found in the product code are in "
                             f"{'one file: ' if len(files) == 1 else ''}{where} (kinds: {', '.join(kinds)})",
                       evs, serves=("datastore", "scaling", "boundary"), scope_note=f"patterns: {', '.join(pats)}; "
                       f"{len(self.product)} product code file(s), tests excluded")
        else:
            self.absence("P1", "no storage sink (database connection, SQL, ORM write, file write, key-value store) "
                               "in the product code", pats, f"{len(self.product)} product code file(s), tests excluded")

    # P2 / P9 ---------------------------------------------------------------------------------------
    def config(self, kinds: list[str]) -> None:
        from verinoda.architecture_map import ENV_PATTERNS

        reads = []
        for rel in self.product:
            for i, ln in enumerate(self.lines(rel), 1):
                for rx, _lang in ENV_PATTERNS:
                    for m in rx.finditer(ln):
                        d = DEFAULT_RX.search(ln)
                        default = d.group(2) if d and d.group(1) == m.group(1) else None
                        reads.append((m.group(1), rel, i, default))
        self.facts["env"] = reads
        rel_rx = [KIND_ENV[k] for k in kinds if k in KIND_ENV]
        relevant = [r for r in reads if any(rx.search(r[0]) for rx in rel_rx)]
        numeric = [r for r in reads if r[3] and re.fullmatch(r"['\"]?-?\d+(?:\.\d+)?['\"]?", r[3]) and r not in relevant]
        for var, rel, i, default in relevant[:4]:
            self.force("P2", f"{var} is read from the environment at {rel}:{i}"
                             + (f", default {default}" if default else ", no default"),
                       [_ev(self.repo, rel, i, needle=re.escape(var) + (".*" + re.escape(default) if default else ""))],
                       serves=("datastore", "scaling", "dependency"))
        for var, rel, i, default in numeric[:3]:
            self.force("P9", f"numeric limit {var} (default {default.strip(chr(34) + chr(39))}) at {rel}:{i}",
                       [_ev(self.repo, rel, i, needle=re.escape(var))], serves=("scaling", "datastore", "other"))
        if not reads:
            self.absence("P2", "no environment variable read in the product code",
                         [rx.pattern for rx, _ in ENV_PATTERNS][:4], f"{len(self.product)} product code file(s)")

    # P3 -----------------------------------------------------------------------------------------------
    def dependencies(self, kinds: list[str]) -> None:
        deps = self.guards.declared_dependencies(self.repo)
        self.facts["deps"] = deps
        manifests = deps.get("manifests") or []
        items = deps.get("items") or []
        drivers = [it for it in items if any(self.guards._dep_key(d) in (self.guards._dep_key(it.get("name")),
                                                                          self.guards._dep_key(it.get("short") or ""))
                                             for d in DRIVER_DEPS)]
        if "datastore" in kinds:
            if drivers:
                for it in drivers[:4]:
                    self.force("P3", f"{it['name']} {it.get('spec') or ''} is declared ({it.get('scope')}) at {it['at']}",
                               [_ev(self.repo, it["path"], it["line"],
                                    needle=re.escape((it.get("short") or it["name"]).split("[")[0]), source_type="manifest")],
                               serves=("datastore", "dependency"))
            else:
                self.absence("P3", "no database driver or ORM among the declared dependencies"
                                   + ((f" ({manifests[0]} declares none)" if len(manifests) == 1 else
                                       f" ({', '.join(manifests)} declare none)") if manifests else
                                      " (no manifest found)"),
                             list(DRIVER_DEPS), "manifests read: " + (", ".join(manifests) or "none") +
                             "; sqlite3 is in Python's standard library and needs no declaration")
        py = self.repo / "pyproject.toml"
        lines = _read_lines(self.repo, "pyproject.toml") if py.is_file() else []
        for i, ln in enumerate(lines, 1):
            m = re.match(r"\s*requires-python\s*=\s*['\"]([^'\"]+)['\"]", ln)
            if m:
                self.facts["requires_python"] = (m.group(1), i)
                self.force("P3", f"the project requires Python {m.group(1)} (pyproject.toml:{i})",
                           [_ev(self.repo, "pyproject.toml", i, needle="requires-python", source_type="manifest")],
                           serves=("dependency", "datastore"))
                break
        servers = [it for it in items if self.guards._dep_key(it.get("name")) in SERVER_DEPS]
        self.facts["servers"] = servers
        # JVM builds: the Java toolchain and the platform versions in gradle.properties
        for rel in [f for f in self.files if PurePosixPath(f).name in ("build.gradle", "build.gradle.kts")][:2]:
            for i, ln in enumerate(self.lines(rel), 1):
                m = re.search(r"(?:JavaLanguageVersion\.of|jvmToolchain)\(\s*(\d+)\s*\)", ln)
                if m:
                    self.facts.setdefault("java", (m.group(1), rel, i))
                    self.force("P3", f"the build targets Java {m.group(1)} ({rel}:{i})",
                               [_ev(self.repo, rel, i, needle=re.escape(m.group(1)), source_type="manifest")],
                               serves=("dependency",))
                    break
        props = [f for f in self.files if PurePosixPath(f).name == "gradle.properties"][:1]
        for rel in props:
            for i, ln in enumerate(self.lines(rel), 1):
                m = re.match(r"\s*((?:minecraft|neo|neoforge|forge|fabric|loader|loom|kotlin|kff)\w*_version)\s*=\s*(\S+)",
                             ln)
                if m:
                    self.force("P3", f"{m.group(1)} = {m.group(2)} ({rel}:{i})",
                               [_ev(self.repo, rel, i, needle=re.escape(m.group(1)), source_type="manifest")],
                               serves=("dependency",))

    # P4 -----------------------------------------------------------------------------------------------
    def decisions(self, words: list[str], kinds: list[str] = ()) -> list[dict]:
        from verinoda import decisions as dm
        from verinoda.architecture_map import DOC_DECISION_RE

        out = []
        try:
            d_dir = dm.decisions_dir(self.repo)
        except Exception:  # noqa: BLE001
            d_dir = None
        recs = {}
        try:
            recs = {d.source: d for d in dm.load_all(self.repo) if d.source}
            own = [d for d in dm.load_all(self.repo) if not d.source]
        except Exception:  # noqa: BLE001
            own = []
        docs = [f for f in self.files if DOC_DECISION_RE.search(f) and f.lower().endswith((".md", ".rst", ".txt"))
                and re.search(r"(^|/)(adr|adrs|decisions?|rfcs?)/", f, re.I)
                and not (d_dir and (self.repo / f).resolve().is_relative_to(d_dir))]
        for rel in docs:
            lines = self.lines(rel)
            text = "\n".join(lines)
            folded = _fold(text)
            # relevant: it names a word of the question or an option, or it is about the same kind of choice
            # (a Turkish question and an English ADR share no word, but both are about a database)
            if words and not any(re.search(rf"\b{re.escape(w)}", folded) for w in words) and \
                    not any(KIND_RX[k].search(folded) for k in kinds if k in KIND_RX and k != "dependency"):
                continue
            status_i = next((i for i, ln in enumerate(lines, 1) if re.match(r"^\s*status\s*:", ln, re.I)), None)
            status = re.sub(r"(?i)^\s*status\s*:\s*", "", lines[status_i - 1]).strip() if status_i else None
            body = [i for i, ln in enumerate(lines, 1) if ln.strip() and not ln.lstrip().startswith("#")
                    and i != status_i]
            para: list[int] = []
            for i in body:  # the first paragraph of the decision text
                if para and i != para[-1] + 1:
                    break
                para.append(i)
            quant = [{"text": s["text"], "at": f"{rel}:{s['line']}" + (f"-{s['end']}" if s["end"] != s["line"]
                                                                        else "")}
                     for s in dm.quantified_sentences(text)]
            item = {"doc": rel, "status": status, "status_at": f"{rel}:{status_i}" if status_i else None,
                    "quantified_sentences": quant}
            if rel in recs:
                item["record"] = recs[rel].id
            out.append(item)
            if para:
                excerpt = " ".join(lines[i - 1].strip() for i in para)
                first_words = re.escape(lines[para[0] - 1].strip()[:30])
                self.force("P4", f"{rel} (status: {status or 'not stated'}) records: \"{excerpt[:300]}\"",
                           [_ev(self.repo, rel, para[0], para[-1], needle=first_words, source_type="design_doc"),
                            _ev(self.repo, rel, status_i, needle=r"(?i)status", source_type="design_doc")
                            if status_i else None],
                           status="primary_source_verified", serves=KINDS, doc=rel)
                reason = re.search(r"(?i)\b(?:chosen for|because|so that|in order to|to keep|reason:?)\s+([^.;]+)",
                                   excerpt)
                if reason:
                    item["reason"] = reason.group(0).strip()
        for d in own:
            item = {"doc": d.path.resolve().relative_to(self.repo).as_posix() if d.path else None, "record": d.id,
                    "status": d.status, "chosen": d.chosen,
                    "guards": [g.get("spec") for g in d.guards if g.get("status") == "accepted"]}
            out.append(item)
        if not out:
            self.absence("P4", "no decision record or ADR mentions the question's subject",
                         ["adr/", "adrs/", "decisions/", "rfcs/", "ARCHITECTURE.md", "DESIGN.md", "decisions.dir"],
                         "decision documents in the repository and Verinoda's decision records")
        self.facts["decisions"] = out
        return out

    # P5 -----------------------------------------------------------------------------------------------
    def deployment(self) -> None:
        import fnmatch

        def deploy(f: str) -> bool:
            name = f.rpartition("/")[2]
            for g in DEPLOY_GLOBS:
                if g.endswith("/**"):
                    if f.startswith(g[:-2]) or ("/" + g[:-2]) in f:
                        return True
                elif "/" in g:
                    if fnmatch.fnmatchcase(f, g) or fnmatch.fnmatchcase(f, "*/" + g):
                        return True
                elif fnmatch.fnmatchcase(name, g):
                    return True
            return False

        found = [f for f in self.files if deploy(f)]
        self.facts["deploy"] = found
        if found:
            for f in found[:4]:
                self.force("P5", f"deployment/CI file present: {f}", [_ev(self.repo, f, 1, needle=r"\S|^$",
                                                                          source_type="config")],
                           serves=("scaling", "datastore", "other"))
        else:
            self.absence("P5", "no deployment or CI file in the repository (no Dockerfile, compose file, Procfile, "
                               "Kubernetes/Helm, Terraform, platform or CI workflow file)", list(DEPLOY_GLOBS),
                         "the repository's files (git ls-files, tracked and untracked); deployment may be "
                         "configured elsewhere")

    # P6 -----------------------------------------------------------------------------------------------
    def concurrency(self) -> None:
        conc = []
        signals = ("threading", "asyncio", "multiprocessing", "concurrent", "gevent", "trio", "anyio", "async ",
                   "global ")
        for rel in self.product:
            if not rel.endswith((".py", ".pyi")):
                continue
            lines = self.lines(rel)
            text = "\n".join(lines)
            if not any(s in text for s in signals):
                continue
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        if a.name.split(".")[0] in {m.split(".")[0] for m in CONCURRENCY_MODULES}:
                            conc.append((rel, node.lineno, f"import {a.name}", re.escape(a.name.split(".")[0])))
                elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in \
                        {m.split(".")[0] for m in CONCURRENCY_MODULES}:
                    conc.append((rel, node.lineno, f"from {node.module} import ...", re.escape(node.module)))
                elif isinstance(node, ast.AsyncFunctionDef):
                    conc.append((rel, node.lineno, f"async def {node.name}", r"async\s+def"))
            # module-level shared state created on first use through `global`
            module_names = {}
            for stmt in tree.body:
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                    module_names[stmt.targets[0].id] = stmt.lineno
            for fn in [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                for g in [n for n in ast.walk(fn) if isinstance(n, ast.Global)]:
                    for name in g.names:
                        if name in module_names:
                            a, b = fn.lineno, fn.end_lineno or fn.lineno
                            self.force("P6", f"{rel} keeps one module-level `{name}` (line {module_names[name]}) that "
                                             f"{fn.name}() sets through `global {name}` (lines {a}-{b}): every caller "
                                             "in the process shares one instance",
                                       [_ev(self.repo, rel, module_names[name], needle=rf"^\s*{re.escape(name)}\s*="),
                                        _ev(self.repo, rel, a, b, needle=rf"global\s+{re.escape(name)}")],
                                       serves=("scaling", "datastore", "boundary"))
        self.facts["concurrency"] = conc
        for rel, line, what, needle in conc[:3]:
            self.force("P6", f"concurrency in the product code: {what} at {rel}:{line}",
                       [_ev(self.repo, rel, line, needle=needle)], serves=("scaling", "datastore"))
        servers = self.facts.get("servers") or []
        for it in servers[:2]:
            self.force("P6", f"server framework declared: {it['name']} at {it['at']}",
                       [_ev(self.repo, it["path"], it["line"], needle=re.escape(it["name"]), source_type="manifest")],
                       serves=("scaling",))
        if not conc and not servers:
            self.absence("P6", "no threading, asyncio, multiprocessing or concurrent.futures import and no async def "
                               "in the product code, and no server framework among the declared dependencies",
                         [*CONCURRENCY_MODULES, "async def", *SERVER_DEPS],
                         f"{sum(f.endswith('.py') for f in self.product)} product .py file(s); other languages "
                         "are not inspected for concurrency")

    # P7 -----------------------------------------------------------------------------------------------
    def tests_pinning(self) -> None:
        pins = []
        for rel in self.tests:
            for i, ln in enumerate(self.lines(rel), 1):
                m = TEST_PIN_RX.search(ln)
                if m:
                    pins.append((rel, i, m.group(0)))
        self.facts["test_pins"] = pins
        if pins:
            groups: dict[str, list[tuple[str, int]]] = {}
            for rel, i, tok in pins:
                groups.setdefault(tok.strip("'\""), []).append((rel, i))
            for tok, sites in list(groups.items())[:3]:
                at = ", ".join(f"{r}:{i}" for r, i in sites[:6])
                self.force("P7", f"tests pin the implementation: {tok!r} at {at}",
                           [_ev(self.repo, r, i, needle=re.escape(tok)) for r, i in sites[:6]],
                           serves=("datastore", "dependency"))

    # P8 -----------------------------------------------------------------------------------------------
    def churn(self) -> None:
        from verinoda.snapshot import git

        files = sorted({s[0] for s in self.facts.get("sinks") or []})[:3]
        for rel in files:
            out = git(self.repo, "log", "-n", "50", "--format=%H", "--", rel)
            if out is None:
                return
            shas = [ln for ln in out.split("\n") if ln.strip()]
            total = git(self.repo, "rev-list", "--count", "--max-count=50", "HEAD")
            if shas:
                self.force("P8", f"{rel} changed in {len(shas)} of the last {(total or '').strip() or '?'} commit(s)",
                           [{"locator": f"git log -n 50 -- {rel}", "source_type": "git_history",
                             "excerpt": shas[0][:12]}], status="primary_source_verified",
                           serves=("datastore", "boundary", "other"))


# -- options ---------------------------------------------------------------------------------------------

def _installed_meta(repo: Path, names: tuple[str, ...]) -> list[dict]:
    """License / Requires-Python of installed distributions in the project's own venv (metadata only)."""
    from importlib import metadata

    try:
        from verinoda.references.local import project_site_packages
    except Exception:  # noqa: BLE001
        return []
    want = {re.sub(r"[-_.]+", "-", n).lower() for n in names}
    out = []
    for site in project_site_packages(Path(repo)):
        for dist in metadata.distributions(path=[str(site)]):
            try:
                name = dist.metadata["Name"] or ""
            except Exception:  # noqa: BLE001
                continue
            if re.sub(r"[-_.]+", "-", name).lower() not in want:
                continue
            md = dist.metadata
            lic = md.get("License-Expression") or md.get("License") or next(
                (c.split("::")[-1].strip() for c in md.get_all("Classifier") or [] if c.startswith("License ::")), None)
            out.append({"name": name, "version": dist.version, "license": (lic or "not stated")[:80],
                        "requires_python": md.get("Requires-Python"), "site": str(site)})
    return out


def _present(pb: _Probe, name: str) -> tuple[bool | None, list[dict]]:
    key = _fold(name).replace(" ", "")
    known = KNOWN_OPTIONS.get(key)
    evs: list[dict] = []
    deps = pb.facts.get("deps") or {"items": []}
    dep_names = known[1] if known else (key,)
    for it in deps.get("items") or []:
        n = pb.guards._dep_key(it.get("name"))
        if n in {pb.guards._dep_key(d) for d in dep_names} or pb.guards._dep_key(it.get("short") or "") in \
                {pb.guards._dep_key(d) for d in dep_names}:
            e = _ev(pb.repo, it["path"], it["line"], needle=re.escape((it.get("short") or it["name"]).split(":")[-1]),
                    source_type="manifest")
            if e:
                evs.append(e)
    for rel, i, what in importers(pb, key)[:3]:
        evs.append(_ev(pb.repo, rel, i, needle=re.escape(what)))
    if key == "kotlin":
        kt = [f for f in pb.product if f.endswith((".kt", ".kts"))]
        if kt:
            evs.append(_ev(pb.repo, kt[0], 1, needle=r"\S|^$"))
    evs = [e for e in evs if e]
    if evs:
        return True, evs
    return (False if known or key in JVM_PREFIXES else None), []


def importers(pb: _Probe, key: str, limit: int = 200) -> list[tuple[str, int, str]]:
    """``(file, line, module)`` of the product files that import an option's Python modules or JVM packages."""
    known = KNOWN_OPTIONS.get(key)
    mods = known[2] if known else ()
    prefixes = JVM_PREFIXES.get(key, ())
    out = []
    for rel in pb.product:
        if rel.endswith(".py") and mods:
            rx = re.compile(rf"\s*(?:import|from)\s+({'|'.join(re.escape(x) for x in mods)})\b")
        elif rel.endswith((".java", ".kt", ".kts")) and prefixes:
            rx = re.compile(rf"\s*import\s+(?:static\s+)?({'|'.join(re.escape(p.rstrip('.')) for p in prefixes)})[.\w]*")
        else:
            continue
        for i, ln in enumerate(pb.lines(rel), 1):
            m = rx.match(ln)
            if m:
                out.append((rel, i, m.group(1)))
                break
        if len(out) >= limit:
            break
    return out


def _change_surface(pb: _Probe, kinds: list[str], present_keys: list[str] = ()) -> list[dict]:
    out = []
    if "dependency" in kinds or "boundary" in kinds:
        for key in present_keys:
            for rel, i, what in importers(pb, key)[:12]:
                out.append({"at": f"{rel}:{i}", "why": f"imports {what}"})
    if "datastore" in kinds or "scaling" in kinds:
        for rel, line, why in (pb.facts.get("connections") or [])[:4]:
            out.append({"at": f"{rel}:{line}", "why": "opens the database connection"})
        for rel, i, kind in (pb.facts.get("sinks") or [])[:8]:
            if kind != "db-connection":
                out.append({"at": f"{rel}:{i}", "why": f"storage code ({kind})"})
        for var, rel, i, default in (pb.facts.get("env") or []):
            if KIND_ENV["datastore"].search(var):
                out.append({"at": f"{rel}:{i}", "why": f"{var} default {default}" if default else f"reads {var}"})
        for rel, i, tok in (pb.facts.get("test_pins") or [])[:6]:
            out.append({"at": f"{rel}:{i}", "why": f"a test pins {tok}"})
    seen, uniq = set(), []
    for c in out:
        if c["at"] not in seen:
            seen.add(c["at"])
            uniq.append(c)
    return uniq


def _pin_quote(store, repo: Path, url: str, text: str) -> dict:
    """A quote-checked external pin: the page (research, network per config) must contain ``text`` verbatim."""
    from verinoda.paths import network_mode

    pin = {"url": url, "quote": text, "quote_found": False, "status": "unknown"}
    if not re.match(r"^https?://", url or "") or not (text or "").strip():
        pin["why"] = "a pin needs an http(s) URL and the verbatim text"
        return pin
    mode = network_mode(repo)
    body = None
    prev = store.one("SELECT local_path, content_hash FROM research WHERE reference = ? AND status = 'ok' "
                     "ORDER BY created_at DESC LIMIT 1", (url,)) if store is not None else None
    if prev and prev.get("local_path") and Path(prev["local_path"]).is_file():
        body = Path(prev["local_path"]).read_text(encoding="utf-8", errors="replace")
        pin["pin"] = f"cached research {str(prev.get('content_hash') or '')[:23]}"
    elif mode == "off":
        pin["why"] = "research.network is off and the page has not been fetched before"
        return pin
    elif store is not None:
        from verinoda import research

        res = research.research(store, repo, url, kind="official_doc")
        if res.get("status") != "ok" or not res.get("text_path"):
            pin["why"] = res.get("error") or f"research status {res.get('status')}"
            return pin
        body = Path(res["text_path"]).read_text(encoding="utf-8", errors="replace")
        pin["pin"] = res.get("pin")
    if body is None:
        pin["why"] = "no store to record the research in"
        return pin
    norm = " ".join(body.split())
    found = " ".join(text.split()) in norm
    pin.update({"quote_found": found, "status": "primary_source_verified" if found else "unknown",
                "why": "the quote occurs verbatim in the pinned page: it says what the page says, not that it "
                       "applies here" if found else "quote not found in the pinned page: dropped"})
    return pin


# -- questions for the human -------------------------------------------------------------------------------

def _q(qid: str, en: str, tr: str, because: str, options: list[str], *, kind: str, needs: str) -> dict:
    return {"id": qid, "text_en": en, "text_tr": tr, "asked_because": because, "discriminates": options,
            "kind": kind, "needs": needs}


def questions(pb: _Probe, kinds: list[str], options: list[str], decisions: list[dict]) -> tuple[list[dict], list[dict]]:
    """(asked, not asked because the code answers them)."""
    opts = options or ["the options"]
    cands: list[dict] = []
    answered: list[dict] = []
    shared = [f for f in pb.forces if f["probe"] == "P6" and "module-level" in f["fact"]]
    deploy = pb.facts.get("deploy") or []
    if "datastore" in kinds or "scaling" in kinds:
        conc = pb.facts.get("concurrency") or []
        because = ("concurrency is not in the code: " + (f"{len(conc)} concurrency site(s) found"
                   if conc else "no threading/asyncio import found") +
                   (f"; {shared[0]['evidence'][0]['locator']} shares one instance per process" if shared else ""))
        workers = [f for f in deploy if re.search(r"gunicorn|uvicorn|Procfile", f, re.I)]
        q = _q("q1", "How many processes or servers will write at the same time, now and at the growth you expect?",
               "Şu an ve beklediğiniz büyümede aynı anda kaç süreç ya da sunucu yazacak?", because, opts,
               kind="concurrency", needs="concurrent writers")
        (answered if workers else cands).append({**q, **({"answered_by": workers[0]} if workers else {})})
        cands.append(_q("q2", "How many records per day do you expect, and how long must they be kept?",
                        "Günde kaç kayıt bekliyorsunuz ve ne kadar süre saklanmaları gerekiyor?",
                        "data volume and retention are not in the code (no load model)", opts, kind="volume",
                        needs="volume and retention"))
        db_service = []
        for f in deploy:
            if re.search(r"compose|\.tf$|helm|k8s|kubernetes", f, re.I):
                text = "\n".join(_read_lines(pb.repo, f))
                if re.search(r"postgres|mysql|mariadb|mongo|redis|rds|cloudsql", text, re.I):
                    db_service.append(f)
        q = _q("q3", "Where will it run, and is a managed database service available there?",
               "Nerede çalışacak ve orada yönetilen bir veritabanı hizmeti var mı?",
               "no deployment file in the repository" if not deploy else
               f"deployment files ({', '.join(deploy[:3])}) name no database service", opts, kind="hosting",
               needs="hosting")
        (answered if db_service else cands).append({**q, **({"answered_by": db_service[0]} if db_service else {})})
        migrations = [f for f in pb.files if re.search(r"(^|/)(alembic\.ini|migrations?/|flyway|liquibase)", f)]
        q = _q("q4", "Who will run backups and schema migrations, and how?",
               "Yedekleri ve şema geçişlerini kim, nasıl yapacak?",
               "no migration tool or backup script in the repository" if not migrations else
               f"migrations exist ({migrations[0]}) but who runs them is not in the code", opts, kind="operations",
               needs="operations")
        cands.append(q)
        for d in decisions:
            if d.get("reason"):
                num = re.match(r"^(?:adr[-_]?)?(\d{1,6})\b", PurePosixPath(d["doc"]).name, re.I)
                ident = d.get("record") or (f"ADR-{int(num.group(1)):04d}" if num else PurePosixPath(d["doc"]).stem)
                cands.append(_q("q5", f"Is {ident}'s reason (\"{d['reason']}\") still a goal?",
                                f"{ident} kararındaki gerekçe (\"{d['reason']}\") hâlâ bir hedef mi?",
                                f"{d['doc']} states it; whether it still holds is the user's call", opts,
                                kind="adr_reason", needs="the recorded reason"))
                break
        cands.append(_q("q6", "What response time and availability must it meet?",
                        "Hangi yanıt süresini ve erişilebilirliği karşılaması gerekiyor?",
                        "no service-level target is in the code", opts, kind="slo", needs="service levels"))
    if "dependency" in kinds:
        rp = pb.facts.get("requires_python")
        java = pb.facts.get("java")
        q = _q("q7", "Which language and runtime versions must keep working?",
               "Hangi dil ve çalışma ortamı sürümleri çalışmaya devam etmeli?",
               "the build states what it targets, not what the users must be able to run", opts, kind="versions",
               needs="versions")
        part = ([f"pyproject.toml:{rp[1]} (requires-python {rp[0]})"] if rp else []) + \
            ([f"{java[1]}:{java[2]} (Java {java[0]})"] if java else [])
        cands.append({**q, **({"partly_answered_by": part} if part else {})})
        cands.append(_q("q8", "Are the licences and the maintenance status of the options acceptable to you?",
                        "Seçeneklerin lisansları ve bakım durumu sizin için kabul edilebilir mi?",
                        "licence acceptability is a policy decision", opts, kind="licence", needs="licence policy"))
        cands.append(_q("q9", "Who will do the migration work, and how much time is there for it?",
                        "Geçiş işini kim yapacak ve bunun için ne kadar zaman var?",
                        "effort and schedule are not in the code", opts, kind="effort", needs="effort"))
    if "boundary" in kinds:
        cands.append(_q("q10", "Will another team or another deployable own this part?",
                        "Bu parçanın sahibi başka bir ekip ya da ayrı yayımlanan bir birim olacak mı?",
                        "ownership is not in the code", opts, kind="ownership", needs="ownership"))
        cands.append(_q("q11", "Which callers must keep working unchanged?",
                        "Hangi çağıranların değişmeden çalışmaya devam etmesi gerekiyor?",
                        "the code shows the callers, not which of them may change", opts, kind="compatibility",
                        needs="compatibility"))
    if kinds == ["other"] or not cands:
        cands.append(_q("q12", "What matters most for this choice: cost, speed of delivery, safety or simplicity?",
                        "Bu seçimde en çok ne önemli: maliyet, teslim hızı, güvenlik mi, sadelik mi?",
                        "priorities are the user's", opts, kind="priorities", needs="priorities"))
        cands.append(_q("q13", "Which constraints must any option meet (budget, deadline, compliance)?",
                        "Her seçeneğin karşılaması gereken kısıtlar neler (bütçe, süre, uyumluluk)?",
                        "constraints are not in the code", opts, kind="constraints", needs="constraints"))
    seen, asked = set(), []
    for q in cands:
        if q["kind"] in seen:
            continue
        seen.add(q["kind"])
        asked.append(q)
    return asked[:MAX_QUESTIONS], answered


# -- the brief ---------------------------------------------------------------------------------------------

def _content_words(text: str) -> list[str]:
    stop = tn.EN_STOPWORDS | tn.TR_STOPWORDS | {"should", "which", "move", "moving", "if", "grows", "grow", "would",
                                                "could", "choose", "pick", "select", "use", "keep", "switch"}
    out = []
    for w in re.findall(r"[a-z0-9_]+", _fold(text)):
        if len(w) >= 4 and w not in stop and w not in out:
            out.append(w[:6] if len(w) > 6 else w)
    return out


def brief(repo: Path, question: str, *, store=None, graph=None, options: list[str] = (), quotes: list[dict] = (),
          agent_arguments: list[str] = (), record: bool = True) -> dict:
    """Collect the brief for ``question``; stored in ``decision_briefs`` when ``store`` is given."""
    from verinoda.store import new_id, now

    t0 = time.monotonic()
    repo = Path(repo).resolve()
    user_opts = [o.strip() for o in options if str(o).strip()]
    q_opts = options_from_question(question)
    names = list(dict.fromkeys(user_opts + q_opts))
    kinds = decision_kinds(question, names)
    pb = _Probe(repo, graph)
    pb.storage()
    pb.config(kinds)
    pb.dependencies(kinds)
    words = list(dict.fromkeys([_fold(n)[:6] for n in names] + _content_words(question)))
    decs = pb.decisions(words, kinds)
    pb.deployment()
    pb.concurrency()
    pb.tests_pinning()
    pb.churn()
    # the option the project already uses, from the code, when the user named none that it uses
    current = []
    for key, (kind, _deps, mods) in KNOWN_OPTIONS.items():
        if kind in kinds and mods and not any(_fold(n) == key or _fold(n).startswith(key) for n in names):
            present, evs = _present(pb, key)
            if present and key not in ("postgres", "mongo"):
                current.append((key, evs))
    options_out = []
    all_names = names + [DISPLAY.get(c[0], c[0]) for c in current]
    presence = {n: _present(pb, n) for n in all_names}
    present_keys = [_fold(n).replace(" ", "") for n in all_names if presence[n][0] is True]
    for key in present_keys:  # how much of the product code is bound to an option in use
        imps = importers(pb, key)
        if len(imps) >= 2 and ("dependency" in kinds or "boundary" in kinds):
            pb.force("P3", f"{len(imps)} product file(s) import {DISPLAY.get(key, key)} "
                           f"({', '.join(sorted({w for _, _, w in imps})[:3])})",
                     [_ev(repo, r, i, needle=re.escape(w)) for r, i, w in imps[:6]], serves=("dependency", "boundary"))
    surface = _change_surface(pb, kinds, present_keys)
    for n in all_names:
        present, evs = presence[n]
        known = KNOWN_OPTIONS.get(_fold(n).replace(" ", ""))
        meta = _installed_meta(repo, known[1]) if known else []
        constraints = [{"fact": f"{m['name']} {m['version']} is installed in the project's environment, licence "
                                f"{m['license']}, requires Python {m['requires_python'] or 'not stated'}",
                        "evidence": {"locator": m["site"], "source_type": "installed_metadata"}} for m in meta]
        if known and not meta and known[1]:
            constraints.append({"fact": f"no distribution of {', '.join(known[1][:3])} is installed in the "
                                        "project's environment: its requirements and licence are unknown offline",
                                "evidence": None})
        options_out.append({
            "name": n, "proposed_by": "user" if n in names else "project",
            "present_in_project": present, "presence_evidence": evs,
            "change_surface": surface if present is not True else [],
            "what_moving_away_touches": surface if present is True else [],
            "constraints": constraints, "external": [], "agent_arguments": []})
    def target(name: str | None, by: str) -> dict:
        hit = next((o for o in options_out if name and o["name"].lower() == name.strip().lower()), None)
        if hit is None and not name and options_out:
            hit = options_out[0]
        if hit is None:
            hit = {"name": name or "(unnamed)", "proposed_by": by, "present_in_project": None,
                   "presence_evidence": [], "change_surface": [], "what_moving_away_touches": [], "constraints": [],
                   "external": [], "agent_arguments": []}
            options_out.append(hit)
        return hit

    for q in quotes:
        pin = _pin_quote(store, repo, str(q.get("url") or ""), str(q.get("text") or ""))
        target(q.get("option"), "user")["external"].append(pin)
    for a in agent_arguments:
        if str(a).strip():  # the agent's own reasoning: shown, never evidence
            target(None, "agent")["agent_arguments"].append({"text": str(a).strip()[:500],
                                                             "status": "weak_inference", "by": "agent"})
    asked, answered = questions(pb, kinds, [o["name"] for o in options_out], decs)
    # only what bears on this kind of choice (a framework choice gets no "no environment variable" absence)
    relevant = set(kinds) | ({*KINDS} if kinds == ["other"] else set())
    pb.forces = [f for f in pb.forces if relevant & set(f.get("serves") or KINDS)]
    pb.absences = [a for a in pb.absences if relevant & set(ABSENCE_SERVES.get(a["probe"], KINDS))]
    # every cited line re-checks before the brief is returned
    dropped = 0
    forces = []
    for f in pb.forces:
        evs = [e for e in f["evidence"] if recheck(repo, e)]
        if evs:
            forces.append({**f, "evidence": evs})
        else:
            dropped += 1
    for i, f in enumerate(forces, 1):
        f["id"] = f"f{i}"
    for i, a in enumerate(pb.absences, 1):
        a["id"] = f"a{i}"
    lang = tn.detect_language(question)
    kinds_tr = {"datastore": "veri deposu", "dependency": "bağımlılık", "boundary": "sınır", "scaling": "ölçekleme",
                "other": "diğer"}
    opt_txt = ", ".join(o["name"] for o in options_out) or "not named"
    res = {
        "brief_id": new_id("dbr") if record else None, "question": question,
        "understood_as": f"a choice ({', '.join(kinds)}); options: {opt_txt}. Verinoda collects what the code "
                         "says and asks; the human decides.",
        "understood_as_tr": f"bir seçim ({', '.join(kinds_tr[k] for k in kinds)}); seçenekler: {opt_txt}. "
                            "Verinoda kodun söylediklerini toplar ve sorar; kararı insan verir.",
        "language": lang, "decision_kind": kinds[0], "decision_kinds": kinds, "derived_by": "decision_brief.rules/1",
        "verdict": HUMAN, "forces": forces, "absences": pb.absences, "existing_decisions": decs,
        "options": options_out, "questions_for_human": asked, "answered_by_code": answered, "limits": LIMITS,
        "elapsed_s": round(time.monotonic() - t0, 3)}
    if dropped:
        res["dropped_forces"] = dropped
    res["next_step"] = ("ask the user questions_for_human (AskUserQuestion / request_user_input), record each answer "
                        f"(`verinoda decide answer {res['brief_id']} --q qN \"...\"`), and record a decision only "
                        "with the user's explicit choice (`verinoda decide record`)")
    if record and store is not None:
        snap = store.latest_snapshot()
        store.insert("decision_briefs", {"id": res["brief_id"], "question": question, "result": res,
                                         "snapshot_id": (snap or {}).get("id"), "created_at": now()})
    return res


def answer(store, brief_id: str, question_id: str, text: str) -> dict:
    """Record the user's answer to one question of a brief (``answered_by = user``)."""
    from verinoda.store import now

    row = store.get("decision_briefs", brief_id)
    if row is None:
        raise KeyError(f"no decision brief {brief_id}")
    qs = {q["id"]: q for q in (row.get("result") or {}).get("questions_for_human") or []}
    if question_id not in qs:
        raise ValueError(f"brief {brief_id} has no question {question_id} (its questions: {', '.join(qs) or 'none'})")
    if not str(text or "").strip():
        raise ValueError("an answer needs the user's words")
    store.insert("decision_answers", {"brief_id": brief_id, "question_id": question_id, "answer": text.strip(),
                                      "answered_by": "user", "created_at": now()})
    answers = store.all("SELECT question_id, answer, created_at FROM decision_answers WHERE brief_id = ? ORDER BY seq",
                        (brief_id,))
    done = {a["question_id"] for a in answers}
    return {"brief_id": brief_id, "answers": answers, "open_questions": [q for k, q in qs.items() if k not in done],
            "note": "an answer is the user's statement: it can support a decision's rationale, never a claim about "
                    "the code"}


def compact(b: dict, lang: str | None = None) -> dict:
    """The part of a brief an analysis carries (the full brief stays stored under its id)."""
    tr = (lang or b.get("language")) in ("tr", "mixed")
    return {"brief_id": b.get("brief_id"), "decision_kinds": b.get("decision_kinds"), "verdict": b.get("verdict"),
            "understood_as": b.get("understood_as_tr") if tr else b.get("understood_as"),
            "forces": [{"fact": f["fact"], "at": [e["locator"] for e in f["evidence"]], "status": f["status"]}
                       for f in b.get("forces") or []][:10],
            "absences": [{"what": a["what"], "searched": a["searched"][:6]} for a in b.get("absences") or []],
            "options": [{"name": o["name"], "present_in_project": o["present_in_project"]}
                        for o in b.get("options") or []],
            "questions_for_human": [{"id": q["id"], "text": q["text_tr"] if tr else q["text_en"],
                                     "asked_because": q["asked_because"]} for q in b.get("questions_for_human") or []]}
