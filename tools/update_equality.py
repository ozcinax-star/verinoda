"""Prove that a candidate checkout's ``verinoda update`` writes what the baseline checkout's writes.

A developer tool for work that makes ``update`` faster (docs/BACKLOG.md 1.1, stage A): the graph and every
file next to it must stay byte-identical. Nothing here imports the checkouts under test; each run is its own
subprocess started through a small ``-c`` wrapper that checks ``verinoda.__file__`` lies under the intended
checkout (``PYTHONPATH`` and the working directory are set to it) and then runs
``runpy.run_module("verinoda.cli")`` (or the harness's loader code).

How to run
----------
::

    # calibration: the baseline against itself (must exit 0)
    python tools/update_equality.py --baseline ../base --candidate ../base --corpus fixtures,orders --seeds 1,2 \
        --cache warm,cold
    # the real check: the candidate against the baseline
    python tools/update_equality.py --baseline ../base --candidate . --corpus fixtures,orders
    # one edit only, keep the work directories to look at them
    python tools/update_equality.py --baseline ../base --candidate . --only delete_file --keep

Options: ``--corpus fixtures,orders,self`` (``self`` = ``git archive HEAD`` of the candidate, slow),
``--seeds 1,2`` (``PYTHONHASHSEED`` of every run), ``--cache warm,cold`` (cold: the AST cache directory
``.verinoda/index/cache`` is deleted in both copies before each update), ``--work DIR`` (default: a new
temporary directory; keep it outside OneDrive and short: Windows paths), ``--keep`` (keep the work
directories; they are always kept when something differs), ``--only EDIT`` (the scan, then that edit alone),
``--python EXE`` (default: this interpreter), ``--skip ARTIFACT,...`` (leave artifacts out of the comparison),
``--keep-going`` (compare the remaining edits of a case after a difference), ``--no-rules`` (calibration aid,
below). Every command runs alone (one fixed folder per case), so a case takes minutes (fixtures: about 16
update steps, each two updates and three loads); run cases in parallel as separate invocations with their own
``--work``.

Exit status: 0 every case equal, 1 a difference (a summary names the case, the step, the artifact and the first
differing JSON path or line), 2 a harness error: a checkout that does not import from where it should, a
baseline ``init``/``scan``/``update`` that fails, the baseline's loader failing on the baseline's own state, two
baseline scans of the same corpus that differ (then the baseline itself is not deterministic and nothing can be
proved), the comparison changing the state it compares, or a case in which no update step was compared.

What a case does
----------------
For each corpus and seed a corpus is built once (copied, every file's mtime pinned, ``git init`` and one commit
with fixed dates and an isolated git config, objects packed). It is copied three times: ``B`` and ``B2`` are set
up (``verinoda init``, ``verinoda scan``) by the BASELINE, ``C`` by the CANDIDATE (so the candidate's own
rebuild record and caches are what its updates start from). Every command runs with the copy MOVED INTO ONE
FIXED FOLDER (``<case>/r/<corpus>``) and moved back afterwards, one copy after the other: absolute paths in the
state (``.graphify_root``, the rebuild record, ids minted from a path, snapshot rows, the update result) are
the same on every side, so nothing path-shaped needs a rule. ``B`` against ``B2`` is the determinism check of
the scan (exit 2 when they differ); ``B`` against ``C`` is step ``scan``.

Each edit is planned ONCE (on ``B``) as a list of operations (write bytes, delete, rename, delete a folder, git
commit) and the same plan is applied to ``B`` and ``C``. Written files get the step's mtime: a whole second at
least one second after the previous run ended, and every run starts at least ``EDIT_GAP`` seconds after it, so
the stat caches' racy windows (git's rule: a file modified within 2 s of being hashed is hashed again) come out
the same on both sides. Then the BASELINE runs ``update`` in ``B`` and the CANDIDATE in ``C``, and everything
below is compared. Edits are cumulative, in the order of ``DEFAULT_EDITS``; an edit that finds no file of its
kind is skipped and listed at the end.

What is compared
----------------
Every file under ``.verinoda`` except ``EXCLUDED`` (``index/cache/``: the AST cache and stat index, which the
cold mode deletes; ``*.lock``; the SQLite ``-wal``/``-shm`` side files, whose content is read with their
database): as bytes, after the named volatile rules below replaced ONLY the volatile value in the raw text (the
rest of the file, spacing and key order included, is compared byte for byte; the report gives the first
differing JSON path when both sides parse). A file present on one side only is a difference. The dated backup
folder the pipeline writes before overwriting a labelled graph (``index/<YYYY-MM-DD>/``) is named ``<DATE>``
and its files are compared under the rules of the files they copy. SQLite files (``atlas.db``,
``index/search.db``, found by their header) are read from a copy (with their ``-wal``): the pragmas
(user_version, application_id, page_size, encoding, auto_vacuum, journal_mode), ``sqlite_master`` in its own
order, and every table (``sqlite_sequence`` included) in ROWID order with the rowid itself (a WITHOUT ROWID
table in its key order). Also compared, per step:

- ``update_exit`` / ``update_result`` (``scan_exit`` / ``scan_result`` at the scan): the exit status and the
  ``--json`` output as bytes (rules below)
- ``graph_kept``: whether the update left graph.json as it was (same mtime and bytes: the fast path), per side
- ``loaded_graph``: ``verinoda.index.load`` of each side's state, by the BASELINE code, dumped in the loaded
  graph's own node and edge order; ``loaded_graph_candidate``: the baseline's load of ``B`` against the
  CANDIDATE's load of ``C``. Each load runs on a throwaway copy (moved into the fixed folder), after the files
  were collected; ``loader_writes`` / ``loader_writes_candidate`` list what a load changed in that copy (a load
  that repairs a stale receiver sidecar writes it). Every file under the real ``.verinoda`` is hashed before
  the collection and after the loads: a change is a harness error.
- ``corpus``: size, mtime and sha256 of every file outside ``.verinoda`` and ``.git``; ``git_state``: HEAD and
  ``git status --porcelain``

Volatile values (every rule is named; see ``RULES`` for the reason of each)
-------------------------------------------------------------------------
Inputs are pinned rather than outputs hidden where possible: file mtimes, git dates and config,
``PYTHONHASHSEED``, a user-level config and cache folder of each side's own, no auto-index, no OCR, no network.
What is left, found by running the baseline against itself (``--no-rules`` shows what the rules hide):

- clock readings and durations, replaced only when they have the shape of one: ``build_stats_clock``,
  ``lexicon_built_at``, ``manifest_seen``, ``search_meta_built_at``, ``atlas_clock_columns``,
  ``update_timings``, ``report_date``, ``backup_dir_date``
- graph.json's write time where another file records it (``receiver_graph_mtime``,
  ``rebuild_record_graph_mtime``, ``search_meta_graph_mtime``): replaced only by a value THIS side's graph.json
  had after a run the harness watched, named by that run (``<GRAPH_MTIME after add_function>``), so both sides
  must point at the graph of the same step
- snapshot ids (``atlas_snapshot_ids``, ``update_snapshot``): uuid-based, numbered in the order this side's
  atlas.db created them; the update result's id must be one of this side's snapshot ids
- code identities (``extraction_stamp``, ``update_extraction_stamp``, ``code_stamp``, ``python_facts_stamp``,
  ``python_cross_stamp``, ``empty_json_stamp``): replaced only when they are THAT side's own checkout's stamp
  (the baseline's for ``B``, the candidate's for ``C``; ``-vendored`` suffix allowed), so a stale stamp on
  either side is a difference; ``atlas_written_by`` likewise for the build named in atlas.db's
  ``meta.schema_written_by`` (version and commit)

Known limits: SQLite files are compared by content, not by page layout. When the candidate changes the
extractor files, the stamps differ by design and are each accepted on their own side only.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_EDITS = ("noop", "add_function", "body_edit", "comment_only", "add_duplicate_stem", "edit_objc_pair",
                 "edit_go", "edit_go_mod", "edit_doc", "add_data_file", "same_size_keep_mtime", "rename_file",
                 "delete_file", "delete_dir", "commit_edit", "noop_after")
CORPORA = ("fixtures", "orders", "self")
BASE_MTIME = 1_700_000_000  # the corpus files' mtime (long before any run)
EDIT_GAP = 3                # seconds from an edit's mtime to the next run (> the 2 s racy windows)
GIT_DATE = "2024-01-01T00:00:00+00:00"
RUN_TIMEOUT = 3600
WRAPPER_EXIT = 97           # the wrapper found verinoda imported from somewhere else
SQLITE_MAGIC = b"SQLite format 3\x00"
EXCLUDED_PREFIXES = ("index/cache/",)
EXCLUDED_SUFFIXES = (".lock", "-wal", "-shm")
BASELINE_SIDES = ("B", "B2")


class HarnessError(Exception):
    """Something the harness cannot prove anything through (exit 2)."""


# -- pure helpers: JSON value spans and raw-text masks ---------------------------------------------------------

_WS = re.compile(r"[ \t\n\r]*")
_DECODER = json.JSONDecoder()


def json_value_spans(text: str, pattern) -> list[tuple[tuple, int, int]]:
    """``(path, start, end)`` of every value of the JSON document ``text`` whose path matches ``pattern``, in
    text order. A pattern is a sequence of keys (str) or list indexes (int); ``"*"`` matches any one key or
    index, ``"**"`` any number of levels (zero included). Values off the pattern are skipped by the C scanner,
    not parsed into objects. Raises ValueError when ``text`` is not one JSON document."""
    out: set = set()

    def ws(i: int) -> int:
        return _WS.match(text, i).end()

    def skip(i: int) -> int:
        try:
            return _DECODER.scan_once(text, i)[1]
        except StopIteration:
            raise ValueError(f"no JSON value at offset {i}") from None

    def walk(i: int, pat: tuple, path: tuple) -> int:
        i = ws(i)
        if not pat:
            end = skip(i)
            out.add((path, i, end))
            return end
        head = pat[0]
        if head == "**":
            walk(i, pat[1:], path)  # zero levels

            def match(_k):
                return True
            child = pat
        else:
            def match(k):
                return head == "*" or head == k
            child = pat[1:]
        c = text[i:i + 1]
        if c == "{":
            i = ws(i + 1)
            if text[i:i + 1] == "}":
                return i + 1
            while True:
                if text[i:i + 1] != '"':
                    raise ValueError(f"expected a key at offset {i}")
                key, i = json.decoder.scanstring(text, i + 1)
                i = ws(i)
                if text[i:i + 1] != ":":
                    raise ValueError(f"expected ':' at offset {i}")
                i = walk(i + 1, child, path + (key,)) if match(key) else skip(ws(i + 1))
                i = ws(i)
                c = text[i:i + 1]
                if c == ",":
                    i = ws(i + 1)
                elif c == "}":
                    return i + 1
                else:
                    raise ValueError(f"expected ',' or '}}' at offset {i}")
        if c == "[":
            i = ws(i + 1)
            if text[i:i + 1] == "]":
                return i + 1
            n = 0
            while True:
                i = walk(i, child, path + (n,)) if match(n) else skip(i)
                n += 1
                i = ws(i)
                c = text[i:i + 1]
                if c == ",":
                    i = ws(i + 1)
                elif c == "]":
                    return i + 1
                else:
                    raise ValueError(f"expected ',' or ']' at offset {i}")
        return skip(i)

    end = ws(walk(0, tuple(pattern), ()))
    if end != len(text):
        raise ValueError(f"extra data at offset {end}")
    return sorted(out, key=lambda t: (t[1], t[2]))


def replace_spans(text: str, edits) -> str:
    """``text`` with each ``(start, end, replacement)`` applied; spans must not overlap."""
    out, pos = [], 0
    for a, b, rep in sorted(edits):
        if a < pos:
            raise ValueError(f"overlapping spans at offset {a}")
        out.append(text[pos:a])
        out.append(rep)
        pos = b
    out.append(text[pos:])
    return "".join(out)


@dataclass
class SideCtx:
    """What a side's volatile values may legitimately be: its own checkout's stamps, the graph.json mtimes
    seen after its runs (value -> the run's label) and its snapshot ids (value -> ``<SNAPSHOTn>``)."""
    name: str
    stamps: dict = field(default_factory=dict)
    graph_mtimes: dict = field(default_factory=dict)
    snapshots: dict = field(default_factory=dict)


@dataclass
class Rule:
    name: str
    artifacts: tuple
    why: str
    paths: tuple = ()      # JSON value paths (see json_value_spans) ...
    check: object = None   # ... and (value, SideCtx) -> replacement string, or None to keep the value
    text: object = None    # or (text, SideCtx) -> (text, count) for a non-JSON file
    db: object = None      # or (table, row dict, SideCtx) -> count, editing the row in place


def mask_text(artifact: str, text: str, sctx: SideCtx, rules=None) -> tuple[str, list[str]]:
    """``text`` with the volatile values of ``artifact`` replaced in place (the rest untouched, byte for byte);
    returns it and the names of the rules that replaced something. A JSON rule on text that is not one JSON
    document replaces nothing (the text is then compared as it is)."""
    rules = _RULES_BY_ARTIFACT.get(artifact, ()) if rules is None else rules
    fired: list[str] = []
    edits: dict = {}
    for r in rules:
        if r.text is not None:
            text, n = r.text(text, sctx)
            if n and r.name not in fired:
                fired.append(r.name)
            continue
        for pattern in r.paths:
            try:
                spans = json_value_spans(text, pattern)
            except ValueError:
                spans = []
            for _path, a, b in spans:
                if (a, b) in edits:
                    continue
                try:
                    value = json.loads(text[a:b])
                except ValueError:
                    continue
                rep = r.check(value, sctx)
                if rep is not None:
                    edits[(a, b)] = json.dumps(rep, ensure_ascii=False)
                    if r.name not in fired:
                        fired.append(r.name)
    if edits:
        text = replace_spans(text, [(a, b, rep) for (a, b), rep in edits.items()])
    return text, fired


# -- the checks of the volatile rules ---------------------------------------------------------------------------

_ISO_CLOCK = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _clock_number(v, s):
    return "<CLOCK>" if _is_number(v) else None


def _duration(v, s):
    return "<DURATION>" if _is_number(v) else None


def _clock_text(v, s):
    return "<CLOCK>" if isinstance(v, str) and _ISO_CLOCK.match(v) else None


def stamp_token(value, expected, token: str):
    """``token`` (with ``-vendored`` kept) when ``value`` is the ``expected`` stamp, else None."""
    if not isinstance(value, str) or not expected:
        return None
    if value == expected:
        return token
    if value == expected + "-vendored":
        return token + "-vendored"
    return None


def _side_stamp(kind: str, token: str):
    def check(v, s: SideCtx):
        return stamp_token(v, s.stamps.get(kind), token)
    return check


def _graph_mtime(v, s: SideCtx):
    label = s.graph_mtimes.get(v) if isinstance(v, int) and not isinstance(v, bool) else None
    return None if label is None else f"<GRAPH_MTIME after {label}>"


def _snapshot_id(v, s: SideCtx):
    return s.snapshots.get(v) if isinstance(v, str) else None


_REPORT_DATE = re.compile(r"^(# Graph Report - .*\()(\d{4}-\d{2}-\d{2})(\)[ \t]*)$", re.MULTILINE)


def _report_date(text, s):
    return _REPORT_DATE.subn(r"\1<DATE>\3", text)


# -- SQLite rules: fn(table, row dict, SideCtx) -> count, editing the row in place ----------------------------

_ATLAS_CLOCK = {("snapshots", "created_at"): _clock_text, ("file_stat", "recorded_at_ns"): _clock_number,
                ("file_facts", "created_at"): _clock_text}


def _db_atlas_clock(table, row, s):
    n = 0
    for (t, c), check in _ATLAS_CLOCK.items():
        if t == table and c in row:
            rep = check(row[c], s)
            if rep is not None:
                row[c] = rep
                n += 1
    return n


def _db_atlas_ids(table, row, s):
    n = 0
    for c in ("id",) if table == "snapshots" else ("snapshot_id",):
        rep = _snapshot_id(row.get(c), s)
        if rep is not None:
            row[c] = rep
            n += 1
    return n


def _db_written_by(table, row, s):
    if table == "meta" and row.get("key") == "schema_written_by":
        rep = stamp_token(row.get("value"), s.stamps.get("server_version"), "<SERVER_VERSION>")
        if rep is not None:
            row["value"] = rep
            return 1
    return 0


def _db_search_built_at(table, row, s):
    if table == "meta" and row.get("key") == "built_at":
        try:
            float(row.get("value"))
        except (TypeError, ValueError):
            return 0
        row["value"] = "<CLOCK>"
        return 1
    return 0


def _db_search_graph_mtime(table, row, s):
    if table == "meta" and row.get("key") == "graph" and isinstance(row.get("value"), str):
        text, fired = mask_text("", row["value"], s, [Rule("", (), "", (("mtime_ns",),), _graph_mtime)])
        if fired:
            row["value"] = text
            return 1
    return 0


RULES: list[Rule] = [
    Rule("build_stats_clock", ("index/build_stats.json",),
         "`at` is time.time() and `graph_seconds` a duration of the build (numbers only)",
         (("at",), ("graph_seconds",)), _clock_number),
    Rule("extraction_stamp", ("index/build_stats.json",),
         "a hash of the extractor's source: must be this side's checkout's own stamp",
         (("extraction",),), _side_stamp("extraction", "<EXTRACTION_STAMP>")),
    Rule("update_extraction_stamp", ("update_result", "scan_result"),
         "the same stamps in the result's `extraction` block (shown when the recorded one is outdated)",
         (("extraction", "was"), ("extraction", "now")), _side_stamp("extraction", "<EXTRACTION_STAMP>")),
    Rule("code_stamp", ("index/rebuild_record.json",),
         "a hash of project_index + index.py + portable_ids (and the Python version): must be this side's "
         "checkout's own", (("stamp",),), _side_stamp("code", "<CODE_STAMP>")),
    Rule("python_facts_stamp", ("index/python_facts.json",),
         "what the kept facts were made with (upstream module, grammar, own walk): this side's own",
         (("stamp",),), _side_stamp("python_facts", "<PYTHON_FACTS_STAMP>")),
    Rule("python_cross_stamp", ("index/python_cross.json",),
         "what the kept trees were made with: this side's own", (("stamp",),),
         _side_stamp("python_cross", "<PYTHON_CROSS_STAMP>")),
    Rule("empty_json_stamp", ("index/empty_json.json",),
         "the JSON extractor's code and grammar versions: this side's own", (("stamp",),),
         _side_stamp("empty_json", "<EMPTY_JSON_STAMP>")),
    Rule("rebuild_record_graph_mtime", ("index/rebuild_record.json",),
         "graph.mtime_ns is when graph.json was written: must be a value this side's graph.json had after a "
         "watched run (size and sha256 beside it are compared)", (("graph", "mtime_ns"),), _graph_mtime),
    Rule("receiver_graph_mtime", ("index/receiver_calls.json",),
         "graph.mtime_ns (the sidecar's key) is when graph.json was written: as rebuild_record_graph_mtime",
         (("graph", "mtime_ns"),), _graph_mtime),
    Rule("lexicon_built_at", ("index/lexicon.json",), "`built_at` is the clock (lexicon.now())",
         (("built_at",),), _clock_text),
    Rule("manifest_seen", ("index/manifest.json",),
         "`seen` is when the build looked at the file (the file's own mtime is pinned and compared)",
         (("*", "seen"),), _clock_number),
    Rule("update_timings", ("update_result", "scan_result"),
         "`seconds`, `index_seconds` and `ms` (at any depth) are durations",
         (("**", "seconds"), ("**", "index_seconds"), ("**", "ms")), _duration),
    Rule("update_snapshot", ("update_result", "scan_result"),
         "the snapshot's id must be one of this side's atlas.db snapshots (numbered as there); created_at is "
         "the clock", (("snapshot", "id"),), _snapshot_id),
    Rule("update_snapshot_clock", ("update_result", "scan_result"), "the snapshot's created_at is the clock",
         (("snapshot", "created_at"),), _clock_text),
    Rule("report_date", ("index/GRAPH_REPORT.md",), "the report header carries today's date",
         text=_report_date),
    Rule("backup_dir_date", ("<listing>",),
         "the pipeline's backup folder of a labelled graph is named after today's date (index/<YYYY-MM-DD>/): "
         "the name becomes <DATE>; its files are compared under the rules of the files they copy"),
    Rule("atlas_clock_columns", ("atlas.db",),
         "snapshots.created_at, file_stat.recorded_at_ns and file_facts.created_at are clock readings",
         db=_db_atlas_clock),
    Rule("atlas_snapshot_ids", ("atlas.db",),
         "snapshot ids are random: numbered by their rowid order; snapshot_id columns point at those numbers",
         db=_db_atlas_ids),
    Rule("atlas_written_by", ("atlas.db",),
         "meta.schema_written_by names the build that created the schema (buildinfo.server_version(): version "
         "and commit): must be this side's checkout's own", db=_db_written_by),
    Rule("search_meta_built_at", ("index/search.db",), "meta.built_at is time.time() of the search-index build",
         db=_db_search_built_at),
    Rule("search_meta_graph_mtime", ("index/search.db",),
         "meta.graph.mtime_ns is when graph.json was written: as rebuild_record_graph_mtime",
         db=_db_search_graph_mtime),
]
# tables whose row order calibration showed to be nondeterministic: compared sorted (none so far)
SORTED_TABLES: dict[tuple[str, str], str] = {}

_RULES_BY_ARTIFACT: dict[str, list[Rule]] = {}


def _index_rules() -> None:
    _RULES_BY_ARTIFACT.clear()
    for r in RULES:
        if r.check is not None or r.text is not None or r.db is not None:
            for a in r.artifacts:
                _RULES_BY_ARTIFACT.setdefault(a, []).append(r)


_index_rules()

RULE_HITS: dict[str, int] = {}


def _hit(names) -> None:
    for name in names:
        RULE_HITS[name] = RULE_HITS.get(name, 0) + 1


# -- first differences ------------------------------------------------------------------------------------------

def first_json_diff(a, b, path: str = "$"):
    """The first place where two JSON-like values differ, list order and dict key order included, as
    ``(path, a_repr, b_repr)``; None when equal."""
    if type(a) is not type(b):
        return path, _short(a), _short(b)
    if isinstance(a, dict):
        ka, kb = list(a), list(b)
        for k in ka:
            if k not in b:
                return f"{path}.{k}", _short(a[k]), "<missing>"
        for k in kb:
            if k not in a:
                return f"{path}.{k}", "<missing>", _short(b[k])
        for k in ka:
            d = first_json_diff(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        if ka != kb:
            return f"{path} (key order)", _short(ka), _short(kb)
        return None
    if isinstance(a, list):
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_json_diff(x, y, f"{path}[{i}]")
            if d:
                return d
        if len(a) != len(b):
            return f"{path} (length)", str(len(a)), str(len(b))
        return None
    if a != b:
        return path, _short(a), _short(b)
    return None


def first_line_diff(a: str, b: str):
    """``(line_number, a_line, b_line)`` of the first differing line; None when equal."""
    la, lb = a.splitlines(), b.splitlines()
    for i, (x, y) in enumerate(zip(la, lb), 1):
        if x != y:
            return i, _short(x), _short(y)
    if len(la) != len(lb):
        i = min(len(la), len(lb)) + 1
        return i, _short(la[i - 1] if i <= len(la) else "<end>"), _short(lb[i - 1] if i <= len(lb) else "<end>")
    if a != b:  # line endings or a final newline
        return 0, repr(a[-20:]), repr(b[-20:])
    return None


def _short(x, n: int = 160) -> str:
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, sort_keys=True, default=str)
    return s if len(s) <= n else s[:n] + "..."


def bytes_diff(a: bytes, b: bytes) -> str | None:
    """Where two byte strings differ: the first JSON path when both are JSON and differ as JSON, else the first
    line; None when equal."""
    if a == b:
        return None
    ta, tb = a.decode("utf-8", "surrogateescape"), b.decode("utf-8", "surrogateescape")
    try:
        ja, jb = json.loads(ta), json.loads(tb)
    except ValueError:
        ja = jb = None
        both = False
    else:
        both = True
    if both:
        d = first_json_diff(ja, jb)
        if d:
            return f"{d[0]}: {d[1]} != {d[2]}"
        ln = first_line_diff(ta, tb)
        return f"same JSON, bytes differ at line {ln[0]}: {ln[1]} != {ln[2]}"
    ln = first_line_diff(ta, tb)
    if ln is None:
        return f"bytes differ ({len(a)} vs {len(b)} bytes)"
    return f"line {ln[0]}: {ln[1]} != {ln[2]}"


# -- SQLite -----------------------------------------------------------------------------------------------------

_PRAGMAS = ("user_version", "application_id", "page_size", "encoding", "auto_vacuum", "journal_mode")


def sqlite_dump(db: Path, rules, sctx: SideCtx, tmp: Path) -> tuple[dict, list[str]]:
    """The content of an SQLite file, read from a copy (with its -wal and -shm) so the original is never opened:
    the pragmas, ``sqlite_master`` in its own order and every table's rows in rowid order, each row led by its
    rowid (a WITHOUT ROWID table: key order, no rowid). The DB rules edit rows (``rule.db(table, row, sctx)``);
    a table in ``SORTED_TABLES`` is compared sorted (named rule). Returns ``(dump, fired rule names)``."""
    tmp.mkdir(parents=True, exist_ok=True)
    copy = tmp / db.name
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(db) + suffix)
        if src.exists():
            shutil.copyfile(src, Path(str(copy) + suffix))
    fired: list[str] = []
    con = sqlite3.connect(str(copy))
    try:
        out: dict = {"pragmas": {p: con.execute(f"PRAGMA {p}").fetchone()[0] for p in _PRAGMAS}}
        master = con.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY rowid").fetchall()
        out["sqlite_master"] = [list(r) for r in master]
        tables = [name for typ, name, _t, _s in master if typ == "table"]
        if "snapshots" in tables:  # number the ids before any row refers to them
            try:
                for i, (sid,) in enumerate(con.execute("SELECT id FROM snapshots ORDER BY rowid"), 1):
                    sctx.snapshots.setdefault(sid, f"<SNAPSHOT{i}>")
            except sqlite3.OperationalError:
                pass
        for t in tables:
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
            try:
                rows = con.execute(f'SELECT rowid, * FROM "{t}" ORDER BY rowid').fetchall()
                lead = ["<rowid>"]
            except sqlite3.OperationalError:  # WITHOUT ROWID
                rows = con.execute(f'SELECT * FROM "{t}"').fetchall()
                lead = []
            names = lead + cols
            norm = []
            for row in rows:
                d = dict(zip(names, row))
                for r in rules:
                    if r.db(t, d, sctx) and r.name not in fired:
                        fired.append(r.name)
                norm.append([("bytes:" + v.hex()) if isinstance(v, bytes) else v for v in (d[c] for c in names)])
            key = (db.name, t)
            if key in SORTED_TABLES:
                norm.sort(key=lambda r: json.dumps(r[len(lead):], sort_keys=True, default=str))
                fired.append(SORTED_TABLES[key])
            out[t] = {"columns": names, "rows": norm}
        return out, fired
    finally:
        con.close()
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.unlink(str(copy) + suffix)


# -- files --------------------------------------------------------------------------------------------------------

def excluded(rel: str) -> bool:
    """Is ``rel`` (a path under ``.verinoda``, forward slashes) left out of the comparison?"""
    return rel.startswith(EXCLUDED_PREFIXES) or rel.endswith(EXCLUDED_SUFFIXES)


_DATED = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def artifact_names(rels: list[str]) -> dict[str, tuple[str, str]]:
    """``{rel: (artifact name, rule key)}`` for the files under ``.verinoda``: a dated backup folder
    (``index/<YYYY-MM-DD>/``) becomes ``<DATE>`` (``<DATE+1>`` for a second one, by name order) and its files
    take the rules of the files they copy (rule ``backup_dir_date``)."""
    dated = sorted({r.split("/")[1] for r in rels if r.count("/") >= 2 and r.startswith("index/")
                    and _DATED.match(r.split("/")[1])})
    tags = {d: "<DATE>" if i == 0 else f"<DATE+{i}>" for i, d in enumerate(dated)}
    out = {}
    for r in rels:
        parts = r.split("/")
        if len(parts) >= 3 and parts[0] == "index" and parts[1] in tags:
            out[r] = ("/".join(["index", tags[parts[1]]] + parts[2:]), "index/" + "/".join(parts[2:]))
        else:
            out[r] = (r, r)
    return out


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_state(root: Path, skip_dirs=()) -> dict[str, list]:
    """``{relative path: [size, mtime_ns, sha256]}`` of every file under ``root`` (folders named in
    ``skip_dirs`` left out at any depth)."""
    out: dict[str, list] = {}
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs)
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            st = p.stat()
            out[p.relative_to(root).as_posix()] = [st.st_size, st.st_mtime_ns, sha256_file(p)]
    return dict(sorted(out.items()))


def state_changes(before: dict, after: dict) -> list[str]:
    """What changed between two :func:`tree_state` results, one line per file."""
    out = []
    for rel in sorted(set(before) | set(after)):
        if rel not in after:
            out.append(f"removed {rel}")
        elif rel not in before:
            out.append(f"added {rel}")
        elif before[rel] != after[rel]:
            out.append(f"modified {rel}" + ("" if before[rel][2] != after[rel][2] else " (stat only)"))
    return out


# -- edit plans: computed once, applied alike to both copies ----------------------------------------------------

@dataclass(frozen=True)
class Op:
    kind: str                      # write | delete | rename | rmdir | commit
    rel: str = ""
    data: bytes = b""
    dst: str = ""
    mtime_ns: int | None = None    # write: the mtime to leave (None: the step's time)


def _code_files(repo: Path, suffix: str) -> list[str]:
    return sorted(p.relative_to(repo).as_posix() for p in repo.rglob(f"*{suffix}")
                  if p.is_file() and ".git" not in p.parts and ".verinoda" not in p.parts)


def _pick(repo: Path, prefer: tuple[str, ...], suffix: str, want=None) -> str | None:
    for rel in prefer:
        if (repo / rel).is_file() and (want is None or want(repo / rel)):
            return rel
    for rel in _code_files(repo, suffix):
        if want is None or want(repo / rel):
            return rel
    return None


def _read(p: Path) -> str:
    """The file's text with its line endings as they are (a same-size edit must stay the same size)."""
    return p.read_bytes().decode("utf-8", "surrogateescape")


def _enc(text: str) -> bytes:
    return text.encode("utf-8", "surrogateescape")


def _nl(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


_PY_TOP = re.compile(r"^(?:def|class) (\w+)", re.MULTILINE)
_PY_DEF = re.compile(r"^def (\w+)", re.MULTILINE)


def plan_noop(repo: Path):
    return []


def plan_add_function(repo: Path):
    rel = _pick(repo, ("orders/pricing.py", "sample_calls.py", "sample.py"), ".py",
                lambda p: _PY_TOP.search(_read(p)) is not None)
    if rel is None:
        return None
    text = _read(repo / rel)
    target = _PY_TOP.search(text).group(1)
    nl = _nl(text)
    return [Op("write", rel, _enc(text.rstrip("\r\n") + f"{nl}{nl}def eq_added_function():{nl}    return "
                                  f"{target}(){nl}"))]


_RETURN = re.compile(r"^(\s+)return (\S[^\r\n#]*?)\s*$", re.MULTILINE)


def plan_body_edit(repo: Path):
    """A function body changes and nothing else: ``return X`` becomes ``return (X) if True else None``."""
    rel = _pick(repo, ("orders/service.py", "sample.py"), ".py", lambda p: _RETURN.search(_read(p)) is not None)
    if rel is None:
        return None
    text = _read(repo / rel)
    m = _RETURN.search(text)
    new = f"{m.group(1)}return ({m.group(2)}) if True else None"
    return [Op("write", rel, _enc(text[:m.start()] + new + text[m.end():]))]


def plan_comment_only(repo: Path):
    """A comment line appended to a Python file: no definition moves (the graph can stay as it is)."""
    rel = _pick(repo, ("orders/api.py", "sample.py"), ".py")
    if rel is None:
        return None
    text = _read(repo / rel)
    nl = _nl(text)
    body = text if text.endswith(("\n", "\r")) or not text else text + nl
    return [Op("write", rel, _enc(body + f"# eq: a comment and nothing else{nl}"))]


def plan_add_duplicate_stem(repo: Path):
    """A new file whose module stem is an existing file's, defining a function of the same name as one there."""
    rel = _pick(repo, ("orders/pricing.py", "sample.py"), ".py",
                lambda p: _PY_DEF.search(_read(p)) is not None or _PY_TOP.search(_read(p)) is not None)
    if rel is None:
        return None
    text = _read(repo / rel)
    m = _PY_DEF.search(text) or _PY_TOP.search(text)
    name = Path(rel).name
    return [Op("write", f"eq_dup/{name}", _enc(f'"""A second {name}."""\n\n\ndef {m.group(1)}(*args):\n'
                                                 f"    return None\n"))]


def plan_objc_pair(repo: Path):
    """A method declared in a header and implemented in its .m file."""
    for h in _code_files(repo, ".h"):
        m = h[:-2] + ".m"
        if not (repo / m).is_file():
            continue
        ht, mt = _read(repo / h), _read(repo / m)
        if "@interface" not in ht or "@implementation" not in mt:
            continue
        hi, mi = ht.rfind("@end"), mt.rfind("@end")
        if hi < 0 or mi < 0:
            continue
        nh, nm = _nl(ht), _nl(mt)
        return [Op("write", h, _enc(ht[:hi] + f"- (void)eqReset;{nh}" + ht[hi:])),
                Op("write", m, _enc(mt[:mi] + f"- (void)eqReset {{{nm}    [self render];{nm}}}{nm}" + mt[mi:]))]
    return None


_GO_FUNC = re.compile(r"^func (\w+)\(", re.MULTILINE)


def _go_append(repo: Path, fname: str):
    rel = _pick(repo, ("sample.go",), ".go", lambda p: _GO_FUNC.search(_read(p)) is not None)
    if rel is None:
        return None
    text = _read(repo / rel)
    target = _GO_FUNC.search(text).group(1)
    nl = _nl(text)
    return Op("write", rel, _enc(text.rstrip("\r\n") + f"{nl}{nl}func {fname}() {{{nl}\t{target}(){nl}}}{nl}"))


def plan_go(repo: Path):
    """A Go function added (no go.mod in the corpus yet)."""
    op = _go_append(repo, "EqAdded")
    return None if op is None else [op]


def plan_go_mod(repo: Path):
    """A go.mod appears beside the Go file, and the Go file changes again."""
    op = _go_append(repo, "EqAddedWithMod")
    if op is None:
        return None
    mod = (Path(op.rel).parent / "go.mod").as_posix()
    if (repo / mod).exists():
        return [op]
    return [op, Op("write", mod, b"module example.com/eqcorpus\n\ngo 1.21\n")]


def plan_doc(repo: Path):
    rel = _pick(repo, ("README.md", "sample.md"), ".md")
    if rel is None:
        return None
    text = _read(repo / rel)
    nl = _nl(text)
    return [Op("write", rel, _enc(text.rstrip("\r\n") + f"{nl}{nl}## Equality check{nl}{nl}A section the harness "
                                  f"added.{nl}"))]


def plan_add_data_file(repo: Path):
    """A file the graph does not read (plain text): the update re-indexes it for search only."""
    return [Op("write", "eq_notes/notes.txt", b"Notes the equality harness added.\nThey name compute_total and "
                                               b"Server.\n")]


def plan_same_size_keep_mtime(repo: Path):
    """The stat-cache case: a function renamed by one letter (same size) and the file's old mtime put back, so
    nothing but the bytes changed. What a stat cache then misses, it must miss on both sides."""
    rel = _pick(repo, ("orders/repository.py", "sample_calls.py"), ".py",
                lambda p: _PY_DEF.search(_read(p)) is not None)
    if rel is None:
        return None
    text = _read(repo / rel)
    m = _PY_DEF.search(text)
    end = m.end(1) - 1
    new = "y" if text[end] == "x" else "x"
    return [Op("write", rel, _enc(text[:end] + new + text[end + 1:]), mtime_ns=(repo / rel).stat().st_mtime_ns)]


def plan_rename_file(repo: Path):
    """A file renamed (its mtime travels with it)."""
    rel = _pick(repo, ("orders/api.py", "sample.rb"), ".py")
    if rel is None:
        return None
    p = Path(rel)
    return [Op("rename", rel, dst=p.with_name(p.stem + "_renamed" + p.suffix).as_posix())]


def plan_delete_file(repo: Path):
    rel = _pick(repo, ("orders/config.py", "cookieHelpers.ts", "sample_calls.py"), ".py")
    return None if rel is None else [Op("delete", rel)]


def plan_delete_dir(repo: Path):
    """A folder of files deleted at once."""
    for rel in ("docs", "cpp_logger"):
        if (repo / rel).is_dir():
            return [Op("rmdir", rel)]
    dirs = sorted(p.name for p in repo.iterdir() if p.is_dir() and p.name not in (".git", ".verinoda"))
    return [Op("rmdir", dirs[0])] if dirs else None


def plan_commit_edit(repo: Path):
    """A file changed and everything committed: HEAD moves (the commit is the same on both sides: fixed dates)."""
    rel = _pick(repo, ("orders/pricing.py", "sample.py"), ".py")
    if rel is None:
        return None
    text = _read(repo / rel)
    nl = _nl(text)
    return [Op("write", rel, _enc(text.rstrip("\r\n") + f"{nl}{nl}def eq_committed():{nl}    return 1{nl}")),
            Op("commit", data=b"eq: a committed edit")]


PLANS = {"noop": plan_noop, "add_function": plan_add_function, "body_edit": plan_body_edit,
         "comment_only": plan_comment_only, "add_duplicate_stem": plan_add_duplicate_stem,
         "edit_objc_pair": plan_objc_pair, "edit_go": plan_go, "edit_go_mod": plan_go_mod, "edit_doc": plan_doc,
         "add_data_file": plan_add_data_file, "same_size_keep_mtime": plan_same_size_keep_mtime,
         "rename_file": plan_rename_file, "delete_file": plan_delete_file, "delete_dir": plan_delete_dir,
         "commit_edit": plan_commit_edit, "noop_after": plan_noop}


def apply_plan(repo: Path, ops, when_ns: int, git=None) -> None:
    """Apply a plan to one copy. ``git(repo, *args)`` runs git there (for ``commit``)."""
    for op in ops:
        p = repo / op.rel
        if op.kind == "write":
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "wb") as f:
                f.write(op.data)
            t = op.mtime_ns if op.mtime_ns is not None else when_ns
            os.utime(p, ns=(t, t))
        elif op.kind == "delete":
            p.unlink()
        elif op.kind == "rename":
            (repo / op.dst).parent.mkdir(parents=True, exist_ok=True)
            os.rename(p, repo / op.dst)
        elif op.kind == "rmdir":
            rmtree(p)
        elif op.kind == "commit":
            if git is None:
                raise HarnessError("a commit edit needs git")
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", op.data.decode())
        else:
            raise HarnessError(f"unknown edit operation {op.kind!r}")


def plan_text(ops) -> str:
    return ", ".join(o.kind + (f" {o.rel}" if o.rel else "") + (f" -> {o.dst}" if o.dst else "")
                     + (" (old mtime)" if o.mtime_ns is not None else "") for o in ops) or "nothing"


# -- the environment of a run -------------------------------------------------------------------------------------

WRAPPER = r"""
import runpy, sys
from pathlib import Path
tree = Path(sys.argv[1]).resolve()
import verinoda
got = Path(verinoda.__file__).resolve()
if tree not in got.parents:
    sys.stderr.write(f"eq-wrapper: verinoda imports from {got}, not from {tree}\n")
    sys.exit(97)
if sys.argv[2] == "cli":
    sys.argv = ["verinoda"] + sys.argv[3:]
    runpy.run_module("verinoda.cli", run_name="__main__", alter_sys=True)
else:
    code = sys.argv[3]
    sys.argv = ["-c"] + sys.argv[4:]
    exec(compile(code, "<update_equality>", "exec"), {"__name__": "__main__"})
"""


@dataclass
class Env:
    work: Path
    python: str

    def git_env(self) -> dict:
        cfg = self.work / "gitconfig"
        if not cfg.exists():
            cfg.write_text("[user]\n\tname = eq\n\temail = eq@example.invalid\n[core]\n\tautocrlf = false\n"
                           "[init]\n\tdefaultBranch = main\n[gc]\n\tauto = 0\n", encoding="utf-8")
        e = dict(os.environ)
        # the corpus repositories are synthetic: the user's own git config (hooks, autocrlf, signing,
        # templates) must not change them, so a config of their own replaces it
        e.update(GIT_CONFIG_GLOBAL=str(cfg), GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_DATE=GIT_DATE,
                 GIT_COMMITTER_DATE=GIT_DATE, GIT_TERMINAL_PROMPT="0")
        return e

    def run_env(self, tree: Path, seed: int, user: Path) -> dict:
        e = self.git_env()
        for k in list(e):
            if k.startswith(("GRAPHIFY_", "VERINODA_")):
                del e[k]  # an outer setting (GRAPHIFY_OUT above all) would move or change the index
        (user / "config").mkdir(parents=True, exist_ok=True)
        e.update(PYTHONPATH=str(tree), PYTHONHASHSEED=str(seed), PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                 VERINODA_NO_AUTO_INDEX="1", VERINODA_OCR="0", VERINODA_CONFIG_DIR=str(user / "config"),
                 VERINODA_CACHE_DIR=str(user / "cache"),
                 HTTP_PROXY="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9", NO_PROXY="",
                 http_proxy="http://127.0.0.1:9", https_proxy="http://127.0.0.1:9", no_proxy="")
        return e


def rmtree(p: Path, ignore_errors: bool = False) -> None:
    """``shutil.rmtree`` that also removes read-only files (git objects on Windows)."""
    import stat

    def retry(fn, path, _exc):
        os.chmod(path, stat.S_IWRITE)
        fn(path)
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(p, onexc=retry)
        else:
            shutil.rmtree(p, onerror=retry)
    except OSError:
        if not ignore_errors:
            raise


def git(env: Env, cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, env=env.git_env(), capture_output=True, text=True, check=False,
                       encoding="utf-8", errors="replace")
    if r.returncode:
        raise HarnessError(f"git {' '.join(args)} in {cwd} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def run_tree(env: Env, tree: Path, seed: int, user: Path, *args: str) -> subprocess.CompletedProcess:
    """Run the wrapper for ``tree`` (``cli ARGS...`` or ``code CODE ARGS...``) with the working directory and
    ``PYTHONPATH`` set to it; a wrong import location is a harness error."""
    r = subprocess.run([env.python, "-c", WRAPPER, str(tree), *args], cwd=tree, env=env.run_env(tree, seed, user),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=RUN_TIMEOUT, check=False)
    if r.returncode == WRAPPER_EXIT and "eq-wrapper:" in r.stderr:
        raise HarnessError(r.stderr.strip()[-500:] + " (an installed copy shadows PYTHONPATH?)")
    return r


_PROBE = """
import json, sys
from verinoda import paths
paths.configure_index_env()
import verinoda
out = {"file": verinoda.__file__}
def put(name, fn):
    try:
        out[name] = fn()
    except Exception as exc:
        out[name] = None
        out.setdefault("errors", {})[name] = f"{type(exc).__name__}: {exc}"
def empty_json():
    from verinoda import index
    from verinoda.project_index import extract as X
    from verinoda.project_index.extractors import json_config
    return index._known_empty_json._stamp(X, json_config)
def facts():
    from verinoda import python_facts
    from verinoda.project_index.extractors import resolution as res
    return python_facts._stamp(res)
def cross():
    from verinoda import python_cross
    from verinoda.project_index.extractors import resolution as res
    return python_cross._stamp(res, python_cross._source_digest(res._resolve_cross_file_imports))
def extraction():
    from verinoda import buildlock
    return buildlock.extraction_stamp()
def code():
    from verinoda import index
    return index._code_stamp()
def server_version():
    from verinoda.buildinfo import server_version
    return server_version()
for name, fn in (("extraction", extraction), ("code", code), ("empty_json", empty_json),
                 ("python_facts", facts), ("python_cross", cross), ("server_version", server_version)):
    put(name, fn)
print(json.dumps(out))
"""

_LOADER = """
import json, sys
from pathlib import Path
from verinoda import paths
paths.configure_index_env()
from verinoda import index
G = index.load(Path(sys.argv[1])).G
out = {"nodes": [[n, G.nodes[n]] for n in G.nodes],
       "edges": [[u, v, k, d] for u, v, k, d in G.edges(keys=True, data=True)]}
Path(sys.argv[2]).write_text(json.dumps(out, ensure_ascii=False, default=str, indent=0), encoding="utf-8")
"""


def probe_tree(env: Env, tree: Path) -> dict:
    """The tree's stamps, from a process the wrapper checked imports from ``tree``."""
    r = run_tree(env, tree, 0, env.work / "user" / "probe", "code", _PROBE)
    if r.returncode:
        raise HarnessError(f"probe of {tree} failed: {r.stderr.strip()[-800:]}")
    info = json.loads(r.stdout.strip().splitlines()[-1])
    if tree.resolve() not in Path(info["file"]).resolve().parents:
        raise HarnessError(f"verinoda for {tree} imports from {info['file']}")
    return info


# -- corpora ------------------------------------------------------------------------------------------------------

def pin_mtimes(root: Path, when: int = BASE_MTIME) -> None:
    for p in root.rglob("*"):
        if p.is_file() and ".git" not in p.parts:
            os.utime(p, (when, when))


def build_corpus(env: Env, name: str, baseline: Path, candidate: Path, dest: Path) -> Path:
    """The corpus ``name`` at ``dest`` (a git work tree with one commit, objects packed)."""
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".verinoda")
    if name == "fixtures":
        shutil.copytree(baseline / "tests_upstream" / "fixtures", dest, ignore=ignore)
    elif name == "orders":
        src = baseline / "examples" / "orders_app"
        if not src.is_dir():
            raise HarnessError(f"{src} does not exist")
        shutil.copytree(src, dest, ignore=ignore)
    elif name == "self":
        src = candidate if (candidate / ".git").exists() else baseline
        if not (src / ".git").exists():
            raise HarnessError("corpus self: neither checkout is a git work tree (git archive needs one)")
        dest.mkdir(parents=True)
        tar = dest.parent / f"{name}.tar"
        git(env, src, "archive", "--format=tar", "-o", str(tar), "HEAD")
        with tarfile.open(tar) as tf:
            tf.extractall(dest, filter="data") if sys.version_info >= (3, 12) else tf.extractall(dest)
        tar.unlink()
        print(f"  corpus self: git archive HEAD of {src}")
    else:
        raise HarnessError(f"unknown corpus {name!r} (known: {', '.join(CORPORA)})")
    pin_mtimes(dest)
    git(env, dest, "init", "-q")
    git(env, dest, "add", "-A")
    git(env, dest, "commit", "-q", "-m", "corpus")
    git(env, dest, "repack", "-a", "-d", "-q")  # a few files to copy instead of one per object
    git(env, dest, "prune-packed")
    return dest


# -- running in the fixed folder ----------------------------------------------------------------------------------

def _move(src: Path, dst: Path) -> None:
    """Rename a folder, retrying while Windows holds a file in it open (an antivirus scan of a new file)."""
    last = None
    for _ in range(120):
        try:
            os.rename(src, dst)
            return
        except OSError as exc:
            last = exc
            time.sleep(0.5)
    raise HarnessError(f"cannot move {src} to {dst}: {last}")


@contextlib.contextmanager
def at_fixed(store: Path, fixed: Path):
    """``store`` moved to ``fixed`` for the duration (and back, whatever happens)."""
    if fixed.exists():
        raise HarnessError(f"{fixed} is in use")
    fixed.parent.mkdir(parents=True, exist_ok=True)
    _move(store, fixed)
    try:
        yield fixed
    finally:
        _move(fixed, store)


def graph_state(repo: Path):
    gp = repo / ".verinoda" / "index" / "graph.json"
    try:
        return gp.stat().st_mtime_ns, sha256_file(gp)
    except OSError:
        return None


# -- collecting a side --------------------------------------------------------------------------------------------

@dataclass
class Side:
    """What one copy's artifacts look like after a step: name -> ("bytes", bytes) | ("json", value)."""
    items: dict = field(default_factory=dict)


PREFERRED_ORDER = ("scan_exit", "scan_result", "update_exit", "update_result", "graph_kept", "index/graph.json",
                   "index/receiver_calls.json", "index/.graphify_labels.json", "index/.graphify_labels.json.sig",
                   "index/rebuild_record.json", "index/GRAPH_REPORT.md", "index/manifest.json",
                   "index/build_stats.json", "index/search.db", "atlas.db", "loaded_graph",
                   "loaded_graph_candidate", "loader_writes", "loader_writes_candidate", "corpus", "git_state")


def collect_files(repo: Path, sctx: SideCtx, tmp: Path) -> dict:
    """Every file under ``repo/.verinoda`` but the excluded ones, masked (SQLite files first: their snapshot
    ids number the update result's)."""
    atlas = repo / ".verinoda"
    rels = sorted(r for r in tree_paths(atlas) if not excluded(r))
    names = artifact_names(rels)
    if any(n != r for r, (n, _k) in names.items()):
        _hit(["backup_dir_date"])
    items: dict = {}
    plain = []
    for rel in rels:
        p = atlas / rel
        with open(p, "rb") as f:
            head = f.read(len(SQLITE_MAGIC))
        name, key = names[rel]
        if head == SQLITE_MAGIC:
            dump, fired = sqlite_dump(p, [r for r in _RULES_BY_ARTIFACT.get(key, ()) if r.db], sctx, tmp)
            _hit(fired)
            items[name] = ("json", dump)
        else:
            plain.append((rel, name, key))
    for rel, name, key in plain:
        data = (atlas / rel).read_bytes()
        if _RULES_BY_ARTIFACT.get(key):
            text, fired = mask_text(key, data.decode("utf-8", "surrogateescape"), sctx)
            _hit(fired)
            data = text.encode("utf-8", "surrogateescape")
        items[name] = ("bytes", data)
    return items


def tree_paths(root: Path) -> list[str]:
    out = []
    if root.is_dir():
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                out.append((Path(dirpath) / fn).relative_to(root).as_posix())
    return out


def run_items(kind: str, run: subprocess.CompletedProcess, sctx: SideCtx) -> dict:
    """``<kind>_exit`` and ``<kind>_result`` (stdout, masked) of a run."""
    text, fired = mask_text(f"{kind}_result", run.stdout, sctx)
    _hit(fired)
    return {f"{kind}_exit": ("json", run.returncode), f"{kind}_result": ("bytes", text.encode("utf-8"))}


def corpus_items(env: Env, repo: Path) -> dict:
    head = git(env, repo, "rev-parse", "HEAD").strip()
    status = git(env, repo, "status", "--porcelain", "--untracked-files=all").splitlines()
    return {"corpus": ("json", tree_state(repo, skip_dirs=(".git", ".verinoda"))),
            "git_state": ("json", {"head": head, "status": status})}


def compare(b: Side, c: Side, skip=()) -> list[tuple[str, str]]:
    """``[(artifact, where)]`` for each artifact that differs."""
    out = []

    def order(n):
        return (PREFERRED_ORDER.index(n) if n in PREFERRED_ORDER else len(PREFERRED_ORDER), n)
    for name in sorted(set(b.items) | set(c.items), key=order):
        if name in skip:
            continue
        if name not in b.items or name not in c.items:
            out.append((name, "only on the " + ("baseline" if name in b.items else "candidate") + " side"))
            continue
        (kb, vb), (kc, vc) = b.items[name], c.items[name]
        if kb == "bytes" and kc == "bytes":
            d = bytes_diff(vb, vc)
        elif kb == kc == "json":
            d = first_json_diff(vb, vc)
            d = d and f"{d[0]}: {d[1]} != {d[2]}"
        else:
            d = f"kinds differ: {kb} != {kc}"
        if d:
            out.append((name, d))
    return out


# -- running the cases --------------------------------------------------------------------------------------------

@dataclass
class Case:
    corpus: str
    seed: int
    cache: str


@dataclass
class CaseResult:
    diffs: list = field(default_factory=list)
    compared: int = 0
    skipped: list = field(default_factory=list)
    cdir: Path | None = None


class Runner:
    def __init__(self, env: Env, case: Case, baseline: Path, candidate: Path, probes: dict, skip: set,
                 keep_going: bool, no_rules: bool):
        self.env, self.case, self.skip, self.keep_going, self.no_rules = env, case, skip, keep_going, no_rules
        self.tag = f"{case.corpus} seed={case.seed} {case.cache}"
        self.cdir = env.work / f"{case.corpus[:3]}{case.seed}{case.cache[0]}"
        self.fixed = self.cdir / "r" / case.corpus
        self.store = {s: self.cdir / "s" / s / case.corpus for s in ("B", "B2", "C")}
        self.trees = {"B": baseline, "B2": baseline, "C": candidate}
        self.baseline, self.candidate = baseline, candidate
        self.ctx = {s: SideCtx(s, stamps=dict(probes[self.trees[s]])) for s in self.store}
        self.t_end = 0.0

    def user(self, name: str) -> Path:
        return self.cdir / "u" / name

    def run_at_fixed(self, side: str, *args: str) -> subprocess.CompletedProcess:
        with at_fixed(self.store[side], self.fixed):
            return run_tree(self.env, self.trees[side], self.case.seed, self.user(side), "cli", *args)

    def note_graph(self, side: str, label: str) -> None:
        st = graph_state(self.store[side])
        if st is not None:
            self.ctx[side].graph_mtimes.setdefault(st[0], label)

    def load(self, side: str, tree: Path, who: str):
        """``verinoda.index.load`` of a throwaway copy of ``side``'s state (in the fixed folder) by ``tree``;
        returns ``(dump bytes or None, error text, changes the load made in the copy)``."""
        if self.fixed.exists():
            raise HarnessError(f"{self.fixed} is in use")
        shutil.copytree(self.store[side], self.fixed, copy_function=shutil.copy2)
        try:
            before = tree_state(self.fixed / ".verinoda")
            out = self.cdir / "t" / "loaded.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                out.unlink()
            r = run_tree(self.env, tree, self.case.seed, self.user(f"load-{who}"), "code", _LOADER, str(self.fixed),
                         str(out))
            writes = state_changes(before, tree_state(self.fixed / ".verinoda"))
            if r.returncode:
                return None, f"<load failed (exit {r.returncode}): {r.stderr.strip()[-600:]}>", writes
            return out.read_bytes(), "", writes
        finally:
            rmtree(self.fixed)

    def observe(self, sides, runs: dict, kind: str) -> dict:
        """Collect each side (before anything loads its state), then load throwaway copies; the real state is
        hashed before and after and must not change."""
        guard = {s: tree_state(self.store[s] / ".verinoda") for s in sides}
        out = {}
        for s in sides:
            side = Side()
            side.items.update(collect_files(self.store[s], self.ctx[s], self.cdir / "t" / s))
            if s in runs:
                side.items.update(run_items(kind, runs[s], self.ctx[s]))
            side.items.update(corpus_items(self.env, self.store[s]))
            out[s] = side
        for s in sides:
            dump, err, writes = self.load(s, self.baseline, s)
            if dump is None and s in BASELINE_SIDES:
                raise HarnessError(f"{self.tag}: the baseline's load of its own state failed: {err}")
            out[s].items["loaded_graph"] = ("bytes", dump if dump is not None else err.encode())
            out[s].items["loader_writes"] = ("json", writes)
            if s in BASELINE_SIDES:
                out[s].items["loaded_graph_candidate"] = out[s].items["loaded_graph"]
                out[s].items["loader_writes_candidate"] = out[s].items["loader_writes"]
            else:
                dump, err, writes = self.load(s, self.candidate, f"{s}-candidate")
                out[s].items["loaded_graph_candidate"] = ("bytes", dump if dump is not None else err.encode())
                out[s].items["loader_writes_candidate"] = ("json", writes)
        for s in sides:
            if tree_state(self.store[s] / ".verinoda") != guard[s]:
                raise HarnessError(f"{self.tag}: collecting or loading changed {s}'s .verinoda: "
                                   + "; ".join(state_changes(guard[s], tree_state(self.store[s] / ".verinoda"))[:5]))
        return out

    def scan(self, res: CaseResult) -> bool:
        """Set up B, B2 (baseline) and C (candidate); compare B with B2 (determinism) and B with C."""
        runs = {}
        for s in ("B", "B2", "C"):
            ri = self.run_at_fixed(s, "init", str(self.fixed), "--json")
            if ri.returncode == 0:
                rs = self.run_at_fixed(s, "scan", str(self.fixed), "--json")
            else:
                rs = ri
            if s in BASELINE_SIDES and (ri.returncode or rs.returncode):
                raise HarnessError(f"{self.tag}: baseline `verinoda {'init' if ri.returncode else 'scan'}` exited "
                                   f"{rs.returncode}: {(rs.stderr or rs.stdout).strip()[-800:]}")
            runs[s] = rs
            self.note_graph(s, "scan")
        self.t_end = time.time()
        sides = self.observe(("B", "B2", "C"), runs, "scan")
        det = compare(sides["B"], sides["B2"])
        if det and not self.no_rules:
            raise HarnessError(f"{self.tag}: the two BASELINE scans differ, so the baseline is not deterministic "
                               "here: " + "; ".join(f"{a}: {w}" for a, w in det[:5]))
        res.diffs += [f"{self.tag} step=scan (B vs B2, --no-rules): {a}: {w}" for a, w in det]
        rmtree(self.store["B2"])
        diffs = compare(sides["B"], sides["C"], self.skip)
        res.diffs += [f"{self.tag} step=scan: {a}: {w}" for a, w in diffs]
        print(f"  {self.tag}: scan: {'equal' if not diffs else f'DIFFERENT ({len(diffs)} artifact(s))'}", flush=True)
        return not diffs

    def step(self, name: str, res: CaseResult) -> bool | None:
        ops = PLANS[name](self.store["B"])
        if ops is None:
            res.skipped.append(name)
            print(f"  {self.tag}: {name}: skipped (no file of its kind in this corpus)", flush=True)
            return None
        when = math.ceil(self.t_end) + 1
        for s in ("B", "C"):
            apply_plan(self.store[s], ops, when * 1_000_000_000, lambda repo, *a: git(self.env, repo, *a))
            if self.case.cache == "cold":
                rmtree(self.store[s] / ".verinoda" / "index" / "cache", ignore_errors=True)
        while time.time() < when + EDIT_GAP:
            time.sleep(min(0.25, max(0.0, when + EDIT_GAP - time.time())))
        pre, runs, kept = {}, {}, {}
        t0 = time.monotonic()
        for s in ("B", "C"):
            pre[s] = graph_state(self.store[s])
            runs[s] = self.run_at_fixed(s, "update", str(self.fixed), "--json")
            post = graph_state(self.store[s])
            kept[s] = pre[s] is not None and post == pre[s]
            self.note_graph(s, name)
        self.t_end = time.time()
        if runs["B"].returncode:
            raise HarnessError(f"{self.tag}: {name}: the baseline's update exited {runs['B'].returncode}: "
                               f"{(runs['B'].stderr or runs['B'].stdout).strip()[-800:]}")
        t1 = time.monotonic()
        sides = self.observe(("B", "C"), runs, "update")
        t2 = time.monotonic()
        for s in ("B", "C"):
            sides[s].items["graph_kept"] = ("json", kept[s])
        diffs = compare(sides["B"], sides["C"], self.skip)
        res.compared += 1
        res.diffs += [f"{self.tag} step={name}: {a}: {w}" for a, w in diffs]
        what = []
        for s in ("B", "C"):
            try:
                r = json.loads(runs[s].stdout)
                what.append(f"{s}: index_mode={r.get('index_mode', '?')} changed={r.get('changed_count', '?')} "
                            f"kept={'yes' if kept[s] else 'no'}")
            except ValueError:
                what.append(f"{s}: no JSON result (exit {runs[s].returncode})")
        status = "equal" if not diffs else f"DIFFERENT ({len(diffs)} artifact(s))"
        print(f"  {self.tag}: {name}: {status} [{plan_text(ops)}] [{'; '.join(what)}] (updates {t1 - t0:.1f}s, "
              f"collect and loads {t2 - t1:.1f}s)", flush=True)
        return not diffs


def run_case(env: Env, case: Case, baseline: Path, candidate: Path, edits: list[str], probes: dict,
             skip: set, keep_going: bool, no_rules: bool = False) -> CaseResult:
    rn = Runner(env, case, baseline, candidate, probes, skip, keep_going, no_rules)
    res = CaseResult(cdir=rn.cdir)
    if rn.cdir.exists():
        rmtree(rn.cdir)
    rn.cdir.mkdir(parents=True)
    src = build_corpus(env, case.corpus, baseline, candidate, rn.cdir / "src" / case.corpus)
    for s in rn.store:
        shutil.copytree(src, rn.store[s], copy_function=shutil.copy2)
    rmtree(rn.cdir / "src")
    if not rn.scan(res) and not keep_going:
        return res
    for name in edits:
        ok = rn.step(name, res)
        if ok is False and not keep_going:
            break
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--baseline", required=True, type=Path)
    ap.add_argument("--candidate", required=True, type=Path)
    ap.add_argument("--corpus", default="fixtures,orders")
    ap.add_argument("--seeds", default="1")
    ap.add_argument("--cache", default="warm")
    ap.add_argument("--work", type=Path)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--only", metavar="EDIT")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--skip", default="", help="artifacts left out of the comparison")
    ap.add_argument("--keep-going", action="store_true")
    ap.add_argument("--no-rules", action="store_true",
                    help="calibration aid: apply no volatile rule, and report the differences of the two baseline "
                         "scans instead of stopping (shows what each rule hides)")
    a = ap.parse_args(argv)
    t_all = time.monotonic()
    results: list[tuple[str, CaseResult, float]] = []
    try:
        baseline, candidate = a.baseline.resolve(), a.candidate.resolve()
        for t in (baseline, candidate):
            if not (t / "verinoda" / "cli.py").is_file():
                raise HarnessError(f"{t} is not a Verinoda checkout (no verinoda/cli.py)")
        corpora = [c.strip() for c in a.corpus.split(",") if c.strip()]
        for c in corpora:
            if c not in CORPORA:
                raise HarnessError(f"unknown corpus {c!r} (known: {', '.join(CORPORA)})")
        seeds = [int(s) for s in a.seeds.split(",") if s.strip()]
        caches = [c.strip() for c in a.cache.split(",") if c.strip()]
        if any(c not in ("warm", "cold") for c in caches):
            raise HarnessError("--cache takes warm and/or cold")
        if a.only and a.only not in PLANS:
            raise HarnessError(f"unknown edit {a.only!r} (known: {', '.join(PLANS)})")
        edits = [a.only] if a.only else list(DEFAULT_EDITS)
        skip = {s.strip() for s in a.skip.split(",") if s.strip()}
        if a.no_rules:
            RULES.clear()
            _index_rules()
        work = (a.work.resolve() if a.work else Path(tempfile.mkdtemp(prefix="verinoda-eq-")))
        for t in (baseline, candidate):
            if work == t or t in work.parents:
                raise HarnessError(f"--work {work} is inside the checkout {t}")
        work.mkdir(parents=True, exist_ok=True)
        env = Env(work=work, python=a.python)
        probes = {baseline: probe_tree(env, baseline)}
        probes[candidate] = probes[baseline] if candidate == baseline else probe_tree(env, candidate)
        for label, t in (("baseline ", baseline), ("candidate", candidate)):
            p = probes[t]
            print(f"{label} {t} (extraction {p['extraction']}, code {str(p['code'])[:12]})"
                  + ("  [the same tree: calibration]" if candidate == baseline and label == "candidate" else ""))
            if p.get("errors"):
                print(f"  note: stamps the probe could not compute: {p['errors']}")
        if probes[baseline]["extraction"] != probes[candidate]["extraction"]:
            print("  note: the extraction stamps differ: the candidate's updates may rebuild where the baseline's "
                  "do not")
        print(f"work      {work}")
        for corpus in corpora:
            for seed in seeds:
                for cache in caches:
                    t0 = time.monotonic()
                    res = run_case(env, Case(corpus, seed, cache), baseline, candidate, edits, probes, skip,
                                   a.keep_going, a.no_rules)
                    results.append((f"{corpus} seed={seed} {cache}", res, time.monotonic() - t0))
                    if not res.diffs and not a.keep:
                        rmtree(res.cdir, ignore_errors=True)
                    elif res.diffs:
                        print(f"  kept for inspection: {res.cdir}")
    except HarnessError as exc:
        print(f"harness error: {exc}", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired as exc:
        print(f"harness error: timed out: {exc}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - a bug of the harness or an unreadable artifact: not a verdict
        import traceback

        traceback.print_exc()
        print("harness error: unexpected exception (above)", file=sys.stderr)
        return 2
    total = time.monotonic() - t_all
    print("cases:")
    for tag, res, secs in results:
        print(f"  {tag}: {res.compared} update step(s) compared, {len(res.diffs)} difference(s), {secs:.1f}s"
              + (f"; skipped: {', '.join(res.skipped)}" if res.skipped else ""))
    print(f"total {total:.1f}s")
    print("volatile rules applied (artifact reads): "
          + (", ".join(f"{k} x{v}" for k, v in sorted(RULE_HITS.items())) or "none"))
    all_diffs = [d for _t, res, _s in results for d in res.diffs]
    if all_diffs:
        print(f"DIFFERENT: {len(all_diffs)} difference(s)")
        for d in all_diffs:
            print(f"  {d}")
        return 1
    empty = [tag for tag, res, _s in results if res.compared == 0]
    if empty:
        print(f"harness error: no update step was compared in: {', '.join(empty)}", file=sys.stderr)
        return 2
    print("EQUAL: every case produced the same artifacts")
    if not a.keep and not a.work:
        rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
