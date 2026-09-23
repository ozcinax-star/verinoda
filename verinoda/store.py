"""SQLite persistence for snapshots, claims, evidence and their history.

Design rules enforced here (and tested in tests/test_store.py):

* Nothing is deleted. Claims change status; memories are invalidated;
  corrections supersede. ``claim_history`` is append-only.
* Every status change goes through :meth:`Store.record_transition`, which the
  claims layer calls after its own rule checks.
* The schema is versioned in ``meta.schema_version``; ``_MIGRATIONS`` is the
  upgrade path for existing databases.
* A claim's ``text`` and ``created_at`` never change (v4 trigger); derived
  caches (``file_facts``, ``resolutions``, ``file_stat``) may be recomputed.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 4

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    repo_root TEXT NOT NULL,
    project TEXT NOT NULL,
    commit_sha TEXT,
    branch TEXT,
    dirty INTEGER NOT NULL DEFAULT 0,
    tree_hash TEXT NOT NULL,
    file_count INTEGER NOT NULL,
    graph_path TEXT,
    graph_nodes INTEGER,
    graph_edges INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_files (
    snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, path)
);

CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    project TEXT NOT NULL,
    snapshot_id TEXT REFERENCES snapshots(id),
    commit_sha TEXT,
    subjects TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    confidence REAL NOT NULL,
    valid_version TEXT,
    valid_env TEXT,
    uncertainties TEXT NOT NULL DEFAULT '[]',
    kind TEXT NOT NULL DEFAULT 'general',
    spec TEXT NOT NULL DEFAULT '{}',
    analysis_id TEXT,
    supersedes TEXT REFERENCES claims(id),
    superseded_by TEXT REFERENCES claims(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    locator TEXT NOT NULL,
    path TEXT,
    line_start INTEGER,
    line_end INTEGER,
    url TEXT,
    commit_sha TEXT,
    version TEXT,
    content_hash TEXT,
    excerpt TEXT,
    meta TEXT NOT NULL DEFAULT '{}',
    collected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claim_evidence (
    claim_id TEXT NOT NULL REFERENCES claims(id),
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    relation TEXT NOT NULL CHECK (relation IN ('supports','refutes','qualifies')),
    note TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (claim_id, evidence_id, relation)
);

CREATE TABLE IF NOT EXISTS claim_history (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL REFERENCES claims(id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    from_confidence REAL,
    to_confidence REAL NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY,
    claim_id TEXT REFERENCES claims(id),
    text TEXT NOT NULL,
    reference TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    verdict TEXT,
    resolution TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    hypothesis TEXT NOT NULL,
    command TEXT NOT NULL,
    cwd TEXT NOT NULL,
    environment TEXT NOT NULL DEFAULT '{}',
    isolation TEXT NOT NULL,
    timeout_s REAL NOT NULL,
    exit_code INTEGER,
    duration_s REAL,
    timed_out INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    summary TEXT,
    stdout_path TEXT,
    stderr_path TEXT,
    evidence_id TEXT REFERENCES evidence(id),
    claim_id TEXT REFERENCES claims(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research (
    id TEXT PRIMARY KEY,
    reference TEXT NOT NULL,
    kind TEXT NOT NULL,
    requested_ref TEXT,
    resolved_commit TEXT,
    resolved_tag TEXT,
    local_path TEXT,
    content_hash TEXT,
    status TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory (
    id TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    version INTEGER NOT NULL,
    source_claim_id TEXT REFERENCES claims(id),
    snapshot_id TEXT REFERENCES snapshots(id),
    valid INTEGER NOT NULL DEFAULT 1,
    invalidated_at TEXT,
    invalidation_reason TEXT,
    superseded_by TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analyses (
    id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    snapshot_id TEXT,
    budget TEXT NOT NULL,
    usage TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
CREATE INDEX IF NOT EXISTS idx_ce_evidence ON claim_evidence(evidence_id);
CREATE INDEX IF NOT EXISTS idx_hist_claim ON claim_history(claim_id);
CREATE INDEX IF NOT EXISTS idx_evidence_path ON evidence(path);
CREATE INDEX IF NOT EXISTS idx_memory_key ON memory(key);

-- History is append-only and claims are never deleted.
CREATE TRIGGER IF NOT EXISTS no_delete_history BEFORE DELETE ON claim_history
BEGIN SELECT RAISE(ABORT, 'claim_history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS no_update_history BEFORE UPDATE ON claim_history
BEGIN SELECT RAISE(ABORT, 'claim_history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_claims BEFORE DELETE ON claims
BEGIN SELECT RAISE(ABORT, 'claims are never deleted; change their status'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_evidence BEFORE DELETE ON evidence
BEGIN SELECT RAISE(ABORT, 'evidence is never deleted'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_feedback BEFORE DELETE ON feedback
BEGIN SELECT RAISE(ABORT, 'feedback is never deleted'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_memory BEFORE DELETE ON memory
BEGIN SELECT RAISE(ABORT, 'memory is invalidated, never deleted'); END;
"""

# v2: close the remaining holes in "nothing is deleted". Without these a
# claim_evidence link (support/refutation) could be removed or flipped
# silently, and experiments/snapshots/analyses/research records - the
# provenance that claims point at - could disappear.
_SCHEMA_V2 = """
CREATE TRIGGER IF NOT EXISTS no_delete_claim_evidence BEFORE DELETE ON claim_evidence
BEGIN SELECT RAISE(ABORT, 'claim_evidence links are never deleted; add refuting evidence instead'); END;
CREATE TRIGGER IF NOT EXISTS no_update_claim_evidence BEFORE UPDATE ON claim_evidence
BEGIN SELECT RAISE(ABORT, 'claim_evidence links are immutable'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_snapshots BEFORE DELETE ON snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_snapshot_files BEFORE DELETE ON snapshot_files
BEGIN SELECT RAISE(ABORT, 'snapshot files are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_experiments BEFORE DELETE ON experiments
BEGIN SELECT RAISE(ABORT, 'experiments are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_analyses BEFORE DELETE ON analyses
BEGIN SELECT RAISE(ABORT, 'analyses are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_research BEFORE DELETE ON research
BEGIN SELECT RAISE(ABORT, 'research records are never deleted'); END;
"""

# v3: one migration for the round-3 subsystems (docs/DESIGN.md sections 1-4), written once so
# the question-plan, reference-resolver, trust-engine and runtime tracks never race on schema.
_SCHEMA_V3 = """
-- D8 question plans: body immutable, revisions via parent_id
CREATE TABLE IF NOT EXISTS question_plans (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES question_plans(id),
    analysis_id TEXT,
    source TEXT NOT NULL CHECK (source IN ('host','fallback','revised')),
    schema_id TEXT NOT NULL,
    user_message TEXT NOT NULL,
    plan TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    check_result TEXT NOT NULL,
    status TEXT NOT NULL,
    snapshot_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qp_analysis ON question_plans(analysis_id);
CREATE INDEX IF NOT EXISTS idx_qp_hash ON question_plans(plan_hash);
CREATE TRIGGER IF NOT EXISTS no_delete_question_plans BEFORE DELETE ON question_plans
BEGIN SELECT RAISE(ABORT, 'question plans are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS no_update_question_plan_body
BEFORE UPDATE OF plan, plan_hash, check_result, user_message, source, parent_id ON question_plans
BEGIN SELECT RAISE(ABORT, 'a plan is immutable; revise it with parent_id'); END;
ALTER TABLE feedback ADD COLUMN plan_id TEXT;
ALTER TABLE analyses ADD COLUMN plan_id TEXT;

-- D23 symbol facts, content-addressed cache (derived data: may be recomputed, not audited)
CREATE TABLE IF NOT EXISTS file_facts (
    sha256 TEXT NOT NULL,
    scheme TEXT NOT NULL,
    facts TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (sha256, scheme)
);

-- D24 claim dependencies at symbol/facet level
CREATE TABLE IF NOT EXISTS claim_deps (
    claim_id TEXT NOT NULL REFERENCES claims(id),
    dep_key TEXT NOT NULL,
    facet TEXT NOT NULL,
    fp TEXT,
    scheme TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (claim_id, dep_key, facet)
);
CREATE INDEX IF NOT EXISTS idx_claim_deps_key ON claim_deps(dep_key, facet);
CREATE TRIGGER IF NOT EXISTS no_delete_claim_deps BEFORE DELETE ON claim_deps
BEGIN SELECT RAISE(ABORT, 'claim dependencies are never deleted'); END;
ALTER TABLE claims ADD COLUMN claim_key TEXT;
ALTER TABLE claims ADD COLUMN verified_at TEXT;
CREATE INDEX IF NOT EXISTS idx_claims_key ON claims(claim_key);

-- D26 evidence groups (FEVER-style evidence sets); NULL = its own group
ALTER TABLE claim_evidence ADD COLUMN grp TEXT;

-- D25 anchored evidence: relocations are appended, evidence rows stay immutable
CREATE TABLE IF NOT EXISTS evidence_locations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    snapshot_id TEXT,
    line_start INTEGER,
    line_end INTEGER,
    status TEXT NOT NULL CHECK (status IN ('same','moved','changed','gone','ambiguous')),
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evloc_ev ON evidence_locations(evidence_id);
CREATE TRIGGER IF NOT EXISTS no_delete_evidence_locations BEFORE DELETE ON evidence_locations
BEGIN SELECT RAISE(ABORT, 'evidence locations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS no_update_evidence_locations BEFORE UPDATE ON evidence_locations
BEGIN SELECT RAISE(ABORT, 'evidence locations are append-only'); END;

-- D28 runtime observation
CREATE TABLE IF NOT EXISTS runtime_runs (
    id TEXT PRIMARY KEY,
    experiment_id TEXT REFERENCES experiments(id),
    snapshot_id TEXT,
    commit_sha TEXT,
    header TEXT NOT NULL,
    complete INTEGER NOT NULL,
    trace_sha256 TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_calls (
    run_id TEXT NOT NULL REFERENCES runtime_runs(id),
    caller_path TEXT NOT NULL,
    caller_line INTEGER NOT NULL,
    caller_qual TEXT,
    callee_path TEXT NOT NULL,
    callee_line INTEGER,
    callee_qual TEXT NOT NULL,
    hits INTEGER NOT NULL,
    tests TEXT NOT NULL DEFAULT '[]',
    flags TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_rtc_callee ON runtime_calls(run_id, callee_path, callee_qual);
CREATE INDEX IF NOT EXISTS idx_rtc_caller ON runtime_calls(run_id, caller_path, caller_line);
CREATE TRIGGER IF NOT EXISTS no_delete_runtime_runs BEFORE DELETE ON runtime_runs
BEGIN SELECT RAISE(ABORT, 'runtime runs are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_runtime_calls BEFORE DELETE ON runtime_calls
BEGIN SELECT RAISE(ABORT, 'runtime calls are never deleted'); END;

-- D29 precise-resolution cache (derived; keyed by file content)
CREATE TABLE IF NOT EXISTS resolutions (
    path TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    line INTEGER NOT NULL,
    token TEXT NOT NULL,
    tool TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (path, file_sha256, line, token, tool)
);

-- D10 reference resolutions (append-only record of how user references were pinned)
CREATE TABLE IF NOT EXISTS reference_resolutions (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    explicit TEXT NOT NULL DEFAULT '[]',
    network TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS no_delete_reference_resolutions BEFORE DELETE ON reference_resolutions
BEGIN SELECT RAISE(ABORT, 'reference resolutions are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS no_update_reference_resolutions BEFORE UPDATE ON reference_resolutions
BEGIN SELECT RAISE(ABORT, 'reference resolutions are immutable'); END;

-- D21 stat-based hash cache (derived, mutable)
CREATE TABLE IF NOT EXISTS file_stat (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    recorded_at_ns INTEGER NOT NULL
);
"""

# v4 (trust engine, docs/DESIGN.md D23-D27): a claim's statement and birth time are part of
# its identity - a correction supersedes it, it is never rewritten in place.
_SCHEMA_V4 = """
CREATE TRIGGER IF NOT EXISTS no_update_claim_identity
BEFORE UPDATE OF text, created_at ON claims
WHEN NEW.text IS NOT OLD.text OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'a claim''s text and created_at are immutable; supersede it instead'); END;
CREATE INDEX IF NOT EXISTS idx_claim_deps_claim ON claim_deps(claim_id);
"""

_MIGRATIONS: dict[int, str] = {1: _SCHEMA_V1, 2: _SCHEMA_V2, 3: _SCHEMA_V3, 4: _SCHEMA_V4}

_JSON_COLS = {
    "plan", "check_result", "facts", "header", "tests", "flags", "explicit", "detail",
    "spec", "subjects", "uncertainties", "meta", "payload", "resolution", "environment",
    "notes", "budget", "usage", "result", "command",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _decode(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out: dict[str, Any] = {}
    for k in row.keys():
        v = row[k]
        if k in _JSON_COLS and isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                pass
        out[k] = v
    return out


class Store:
    """Thin data-access layer. Business rules live in claims/evidence modules."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    # -- lifecycle --------------------------------------------------------
    def _migrate(self) -> None:
        self.conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        current = int(row[0]) if row else 0
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"atlas.db schema v{current} is newer than this Verinoda (v{SCHEMA_VERSION}); upgrade verinoda"
            )
        for v in range(current + 1, SCHEMA_VERSION + 1):
            self.conn.executescript(_MIGRATIONS[v])
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(v),)
            )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # -- generic helpers --------------------------------------------------
    def _insert(self, table: str, row: dict) -> None:
        cols = list(row)
        vals = [json.dumps(row[c], sort_keys=True) if c in _JSON_COLS and not isinstance(row[c], str) else row[c] for c in cols]
        self.conn.execute(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals
        )

    def _update(self, table: str, key: str, key_val: str, fields: dict) -> None:
        sets, vals = [], []
        for c, v in fields.items():
            sets.append(f"{c} = ?")
            vals.append(json.dumps(v, sort_keys=True) if c in _JSON_COLS and not isinstance(v, str) else v)
        vals.append(key_val)
        self.conn.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE {key} = ?", vals)

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        return _decode(self.conn.execute(sql, args).fetchone())

    def all(self, sql: str, args: tuple = ()) -> list[dict]:
        return [_decode(r) for r in self.conn.execute(sql, args).fetchall()]  # type: ignore[misc]

    # -- snapshots --------------------------------------------------------
    def add_snapshot(self, snap: dict, files: dict[str, str]) -> None:
        with self.tx():
            self._insert("snapshots", snap)
            self.conn.executemany(
                "INSERT INTO snapshot_files (snapshot_id, path, sha256) VALUES (?, ?, ?)",
                [(snap["id"], p, h) for p, h in sorted(files.items())],
            )

    def latest_snapshot(self) -> dict | None:
        return self.one("SELECT * FROM snapshots ORDER BY created_at DESC, rowid DESC LIMIT 1")

    def snapshot(self, sid: str) -> dict | None:
        return self.one("SELECT * FROM snapshots WHERE id = ?", (sid,))

    def snapshot_files(self, sid: str) -> dict[str, str]:
        return {
            r["path"]: r["sha256"]
            for r in self.conn.execute(
                "SELECT path, sha256 FROM snapshot_files WHERE snapshot_id = ?", (sid,)
            )
        }

    # -- evidence ---------------------------------------------------------
    def add_evidence(self, ev: dict) -> str:
        ev = dict(ev)
        ev.setdefault("id", new_id("evd"))
        ev.setdefault("collected_at", now())
        with self.tx():
            self._insert("evidence", ev)
        return ev["id"]

    def evidence(self, eid: str) -> dict | None:
        return self.one("SELECT * FROM evidence WHERE id = ?", (eid,))

    def update_evidence_meta(self, eid: str, meta: dict) -> None:
        with self.tx():
            self._update("evidence", "id", eid, {"meta": meta})

    def link(self, claim_id: str, evidence_id: str, relation: str, note: str | None = None,
             grp: str | None = None) -> None:
        """Link evidence to a claim; ``grp`` names a FEVER-style evidence group (NULL = its own group)."""
        # INSERT OR IGNORE would also swallow the CHECK violation silently.
        if relation not in ("supports", "refutes", "qualifies"):
            raise ValueError(f"relation must be supports/refutes/qualifies, not {relation!r}")
        with self.tx():
            self.conn.execute(
                "INSERT OR IGNORE INTO claim_evidence (claim_id, evidence_id, relation, note, created_at, grp)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (claim_id, evidence_id, relation, note, now(), grp),
            )

    def claim_evidence(self, claim_id: str) -> list[dict]:
        return self.all(
            "SELECT e.*, ce.relation AS relation, ce.note AS link_note, ce.grp AS grp FROM claim_evidence ce"
            " JOIN evidence e ON e.id = ce.evidence_id WHERE ce.claim_id = ? ORDER BY ce.created_at, ce.rowid",
            (claim_id,),
        )

    # -- trust engine: derived facts, claim dependencies, evidence locations --------
    def file_facts(self, sha256: str, scheme: str) -> dict | None:
        row = self.one("SELECT facts FROM file_facts WHERE sha256 = ? AND scheme = ?", (sha256, scheme))
        return row["facts"] if row else None

    def put_file_facts(self, sha256: str, scheme: str, facts: dict) -> None:
        """Cache derived facts by content; a row for the same content is never rewritten."""
        with self.tx():
            self.conn.execute(
                "INSERT OR IGNORE INTO file_facts (sha256, scheme, facts, created_at) VALUES (?, ?, ?, ?)",
                (sha256, scheme, json.dumps(facts, sort_keys=True), now()),
            )

    def add_claim_deps(self, claim_id: str, deps: list[dict]) -> None:
        """Record ``[{"dep_key", "facet", "fp", "scheme"}]``; an existing (key, facet) keeps its row."""
        ts = now()
        with self.tx():
            self.conn.executemany(
                "INSERT OR IGNORE INTO claim_deps (claim_id, dep_key, facet, fp, scheme, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(claim_id, d["dep_key"], d["facet"], d.get("fp"), d["scheme"], ts) for d in deps],
            )

    def rebase_claim_deps(self, claim_id: str, deps: list[dict]) -> None:
        """Move recorded fingerprints to a newer snapshot (rows are updated, never removed)."""
        with self.tx():
            self.conn.executemany(
                "UPDATE claim_deps SET fp = ?, scheme = ? WHERE claim_id = ? AND dep_key = ? AND facet = ?",
                [(d.get("fp"), d["scheme"], claim_id, d["dep_key"], d["facet"]) for d in deps],
            )

    def claim_deps(self, claim_id: str) -> list[dict]:
        return self.all("SELECT dep_key, facet, fp, scheme FROM claim_deps WHERE claim_id = ?"
                        " ORDER BY dep_key, facet", (claim_id,))

    def add_evidence_location(self, evidence_id: str, *, snapshot_id: str | None, line_start: int | None,
                              line_end: int | None, status: str, detail: dict | None = None) -> bool:
        """Append a relocation unless it repeats the latest one recorded for this evidence."""
        last = self.latest_evidence_location(evidence_id)
        if last and (last["status"], last["line_start"], last["line_end"]) == (status, line_start, line_end):
            return False
        with self.tx():
            self.conn.execute(
                "INSERT INTO evidence_locations (evidence_id, snapshot_id, line_start, line_end, status, detail,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (evidence_id, snapshot_id, line_start, line_end, status, json.dumps(detail or {}, sort_keys=True),
                 now()),
            )
        return True

    def latest_evidence_location(self, evidence_id: str) -> dict | None:
        return self.one("SELECT * FROM evidence_locations WHERE evidence_id = ? ORDER BY seq DESC LIMIT 1",
                        (evidence_id,))

    def evidence_locations(self, evidence_id: str) -> list[dict]:
        return self.all("SELECT * FROM evidence_locations WHERE evidence_id = ? ORDER BY seq", (evidence_id,))

    def claims_for_evidence(self, evidence_id: str) -> list[dict]:
        return self.all(
            "SELECT c.*, ce.relation AS relation FROM claim_evidence ce JOIN claims c ON c.id = ce.claim_id"
            " WHERE ce.evidence_id = ?",
            (evidence_id,),
        )

    # -- claims -----------------------------------------------------------
    def insert_claim(self, claim: dict) -> None:
        with self.tx():
            self._insert("claims", claim)
            self.conn.execute(
                "INSERT INTO claim_history (claim_id, from_status, to_status, from_confidence,"
                " to_confidence, reason, actor, payload, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (claim["id"], None, claim["status"], None, claim["confidence"], "created",
                 "verinoda", "{}", claim["created_at"]),
            )

    def claim(self, cid: str) -> dict | None:
        return self.one("SELECT * FROM claims WHERE id = ?", (cid,))

    def claims(self, status: str | None = None, limit: int = 200) -> list[dict]:
        if status:
            return self.all("SELECT * FROM claims WHERE status = ? ORDER BY created_at DESC LIMIT ?", (status, limit))
        return self.all("SELECT * FROM claims ORDER BY created_at DESC LIMIT ?", (limit,))

    def update_claim(self, cid: str, fields: dict) -> None:
        fields = dict(fields)
        fields["updated_at"] = now()
        with self.tx():
            self._update("claims", "id", cid, fields)

    def record_transition(
        self, cid: str, *, from_status: str | None, to_status: str, from_conf: float | None,
        to_conf: float, reason: str, actor: str, payload: dict | None = None,
        extra_fields: dict | None = None,
    ) -> None:
        ts = now()
        fields = {"status": to_status, "confidence": to_conf, "updated_at": ts}
        fields.update(extra_fields or {})
        with self.tx():
            self._update("claims", "id", cid, fields)
            self.conn.execute(
                "INSERT INTO claim_history (claim_id, from_status, to_status, from_confidence,"
                " to_confidence, reason, actor, payload, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (cid, from_status, to_status, from_conf, to_conf, reason, actor,
                 json.dumps(payload or {}, sort_keys=True), ts),
            )

    def history(self, cid: str) -> list[dict]:
        return self.all("SELECT * FROM claim_history WHERE claim_id = ? ORDER BY seq", (cid,))

    # -- feedback / experiments / research / memory / analyses -------------
    def insert(self, table: str, row: dict) -> None:
        with self.tx():
            self._insert(table, row)

    def update(self, table: str, rid: str, fields: dict) -> None:
        with self.tx():
            self._update(table, "id", rid, fields)

    def get(self, table: str, rid: str) -> dict | None:
        return self.one(f"SELECT * FROM {table} WHERE id = ?", (rid,))


class NotInitialised(FileNotFoundError):
    """The project has no Verinoda state yet (read-only commands never create it)."""


def open_store(repo: Path, *, create: bool = True) -> Store:
    """Open ``<repo>/.verinoda/atlas.db``.

    ``create=False`` is for commands that only read or annotate existing state:
    they must not scatter ``.verinoda`` directories into arbitrary folders.
    """
    from verinoda.paths import db_path, ensure_atlas

    if not create and not db_path(repo).is_file():
        raise NotInitialised(f"{repo} has no Verinoda state; run `verinoda scan {repo}` first")
    ensure_atlas(repo)
    return Store(db_path(repo))
