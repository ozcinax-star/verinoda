"""Prove that a candidate checkout's ``verinoda update`` produces what the baseline checkout's produces.

A developer tool for work that makes ``update`` faster (docs/BACKLOG.md 1.1, stage A): the graph and every
file next to it must stay byte-identical. Nothing here imports the checkouts under test; each one runs in
its own subprocess (``<python> -m verinoda.cli ...`` with ``PYTHONPATH`` and the working directory set to
that checkout, which is checked before anything runs).

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
temporary directory; keep it outside OneDrive), ``--keep`` (keep the work directories; they are always kept
when something differs), ``--only EDIT`` (the scan, then that edit alone), ``--python EXE`` (default: this
interpreter), ``--skip ARTIFACT,...`` (leave artifacts out of the comparison), ``--keep-going`` (compare
the remaining edits of a case after a difference), ``--serial`` (run the two updates one after the other).

Exit status: 0 every case equal, 1 a difference (a compact summary names the case, the artifact and the
first differing JSON path or line), 2 a harness error (a checkout that does not import from where it
should, a scan that fails, or two baseline scans of the same corpus that differ: then the baseline itself is
not deterministic and nothing can be proved).

What a case does
----------------
For each corpus and seed a corpus is built once (copied, every file's mtime pinned, ``git init`` and one
commit with fixed dates and an isolated git config). It is copied to ``B/<corpus>`` and ``C/<corpus>`` (same
basename: the project name comes from it) and each copy is set up with the BASELINE code (``verinoda init``,
``verinoda scan``). The two scanned states are compared first (step ``scan0``): they must be equal, as both
ran the same code. Absolute paths live in the state (``.graphify_root``, the rebuild record's root, the
snapshot rows), which is why each copy is scanned where it lives instead of copying one scanned state:
a copied state would name the other directory and turn the rebuild record's fast path off.
Then each edit is applied to both copies in the same way (touched files get the same pinned mtime), the
BASELINE runs ``update`` in B and the CANDIDATE runs ``update`` in C, and everything below is compared.
Edits are cumulative, in this order: noop, add_function, body_edit, add_duplicate_stem, edit_objc_pair,
edit_go, edit_go_mod, edit_doc, add_data_file, delete_file, noop_after. An edit that finds no file of its
kind in a corpus is skipped with a message.

Artifacts compared (paths under ``<repo>/.verinoda``)
-----------------------------------------------------
- ``update_exit``: the exit status of ``update``; ``update_result``: its ``--json`` result
- ``index/graph.json`` (the graph); ``communities`` (derived from it: members and name of each community)
- ``loaded_graph``: the graph as ``verinoda.index.load(repo)`` sees it (receiver-call edges applied), loaded
  by the BASELINE code for both copies and dumped as sorted nodes and ``(u, v, key, attrs)`` edges
- ``index/receiver_calls.json`` (receiver sidecar), ``index/.graphify_labels.json`` and its ``.sig``,
  ``index/rebuild_record.json``, ``index/GRAPH_REPORT.md``, ``index/manifest.json``, ``index/lexicon.json``,
  ``index/copies.json``, ``index/python_facts.json``, ``index/python_cross.json``,
  ``index/empty_json.json``, ``index/build_stats.json``, ``index/.graphify_root``
- ``index/search.db``: every table's rows, in a stable order
- ``atlas.db``: every table's rows (snapshots, snapshot_files, file_stat, file_facts, meta, ...)
- ``index/<DATE>/*``: the dated backup the pipeline writes before it overwrites a labelled graph
  (``export.backup_if_protected``: graph.json, labels, report, manifest), each file under its original's rules
- ``index_listing``: the names of the files under ``.verinoda`` (caches, locks, SQLite -wal/-shm excepted) -
  a file only one side writes is a difference too
JSON files are compared as bytes (after the root placeholders) when no volatile rule applies to them, else
as JSON with list order and key order kept. ``index/cache`` (AST cache, stat index) is not compared: it is
a cache, and the cold mode deletes it.

Volatile fields (every rule is named; see ``RULES`` for the reason of each)
-------------------------------------------------------------------------
Inputs are pinned rather than outputs hidden where possible: file mtimes (corpus and edits), git dates and
config, ``PYTHONHASHSEED``, the user-level Verinoda config (``VERINODA_CONFIG_DIR`` points at an empty
folder), no auto-index, no OCR, no network (proxies point at a closed port). The rules left, found by running
the baseline against itself (``--no-rules`` shows what they hide; ``--serial`` makes clock fields differ):

- ``root`` / ``root_slug``: the corpus root (B and C live in different directories) becomes ``<ROOT>`` in
  every artifact, in each spelling (``C:\\a\\b``, ``C:/a/b``, JSON-escaped), and its node-id slug (ids minted
  from an absolute path, as in python_facts.json) ``<ROOT_SLUG>``; nothing else of a path is touched
- clock readings and durations: ``build_stats_clock`` (at, graph_seconds), ``lexicon_built_at``,
  ``manifest_seen``, ``search_meta_built_at``, ``atlas_clock_columns`` (snapshots.created_at,
  file_stat.recorded_at_ns, file_facts.created_at), ``update_timings`` (seconds, index_seconds, ms),
  ``report_date`` (the report header's date), ``backup_dir_date`` (the backup folder's name)
- graph.json's write time where another file records it: ``receiver_graph_mtime``,
  ``rebuild_record_graph_mtime``, ``search_meta_graph_mtime`` (size and sha256 beside it are compared)
- random ids: ``atlas_snapshot_ids``, ``update_snapshot_id`` (uuid4; numbered in creation order)
- root-dependent hashes, replaced only after checking what they hash: ``labels_sig`` (a community signature
  over ids that still held the root: replaced by the signature of the same ids with ``<ROOT_SLUG>``),
  ``rebuild_record_sig`` (the sha256 of this side's .sig file), ``rebuild_record_key`` (only when the record's
  communities hold root-bearing ids)
- code identities, inert when both checkouts are the same: ``extraction_stamp`` / ``update_extraction_stamp``
  (build_stats.json, update result) and ``code_stamp`` (rebuild_record.json) are replaced only when they are the
  baseline's or the candidate's own stamp

Known limits: when the candidate changes the extractor files, its extraction stamp differs and its first
update rebuilds the graph even where the baseline's does not (the run prints a note; update_result and
atlas.db then differ by design). The racy window of stat caches (a file edited within the same clock tick as
the last scan) is not exercised: edits get distinct pinned mtimes.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import functools
import json
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

DEFAULT_EDITS = ("noop", "add_function", "body_edit", "add_duplicate_stem", "edit_objc_pair", "edit_go",
                 "edit_go_mod", "edit_doc", "add_data_file", "delete_file", "noop_after")
CORPORA = ("fixtures", "orders", "self")
# mtimes of the corpus files, then of each edit's files (step n: BASE_MTIME + n * MTIME_STEP): the same in both
# copies, so mtime-derived fields agree without being hidden, and every edit changes a touched file's stat
BASE_MTIME = 1_700_000_000
MTIME_STEP = 1000
GIT_DATE = "2024-01-01T00:00:00+00:00"
RUN_TIMEOUT = 3600

ROOT_TOKEN = "<ROOT>"
SLUG_TOKEN = "<ROOT_SLUG>"


class HarnessError(Exception):
    """Something the harness cannot prove anything through (exit 2)."""


# -- pure helpers: path placeholder, volatile rules, canonical dumps, first difference ------------------------

def root_variants(root: str) -> list[str]:
    """Every spelling of ``root`` that can appear in an artifact, longest first: native, forward slashes,
    and the JSON-escaped native form (backslashes doubled)."""
    native = str(root)
    fwd = native.replace("\\", "/")
    out = {native, fwd, native.replace("\\", "\\\\"), native.replace("/", "\\"),
           native.replace("/", "\\").replace("\\", "\\\\")}
    return sorted((v for v in out if v), key=len, reverse=True)


def root_slug(root: str) -> str:
    """The corpus root as a node-id slug (``verinoda.project_index.ids.make_id`` of the path: casefold +
    NFKC to a fixpoint, every run of non-word characters one ``_``), copied here so that nothing is imported
    from a checkout under test. Ids minted from an absolute path carry it (rule ``root_slug``)."""
    import unicodedata

    cur = str(root)
    for _ in range(6):
        nxt = unicodedata.normalize("NFKC", cur.casefold())
        if nxt == cur:
            break
        cur = nxt
    cur = re.sub(r"[^\w]+", "_", cur, flags=re.UNICODE)
    return re.sub(r"_+", "_", cur).strip("_")


@functools.lru_cache(maxsize=16)
def _root_patterns(root: str):
    # a spelling of the root counts only where the path ends there or goes on below it: ``.../B/fixtures2`` is
    # not ``.../B/fixtures`` (nor is an id ``<slug>2_x`` the slug's)
    path = re.compile("(?:" + "|".join(map(re.escape, root_variants(root))) + r")(?![\w.-])")
    slug = root_slug(root)
    # a slug that short could match inside unrelated ids
    return path, (re.compile(re.escape(slug) + r"(?![^\W_])") if len(slug) >= 8 else None)


def replace_root(text: str, root: str) -> str:
    """``text`` with the corpus root replaced by ``<ROOT>`` (rule ``root``) and its id slug by
    ``<ROOT_SLUG>`` (rule ``root_slug``)."""
    if not text:
        return text
    path, slug = _root_patterns(str(root))
    text = path.sub(ROOT_TOKEN, text)
    return slug.sub(SLUG_TOKEN, text) if slug is not None else text


def replace_root_obj(obj, root: str):
    """``replace_root`` on every string (keys too) of a JSON-like object, order kept."""
    if isinstance(obj, str):
        return replace_root(obj, root)
    if isinstance(obj, list):
        return [replace_root_obj(x, root) for x in obj]
    if isinstance(obj, dict):
        return {replace_root(k, root) if isinstance(k, str) else k: replace_root_obj(v, root)
                for k, v in obj.items()}
    return obj


@dataclass
class Rule:
    name: str
    artifact: str
    why: str
    fn: object  # (obj, ctx) -> (obj, count)


def _set_keys(d, keys, token):
    n = 0
    if isinstance(d, dict):
        for k in keys:
            if k in d:
                d[k] = token
                n += 1
    return d, n


def _rule_build_stats(obj, ctx):
    obj, n = _set_keys(obj, ("at", "graph_seconds"), "<VOLATILE>")
    return obj, n


def _stamp_token(value, known, token):
    if isinstance(value, str):
        base = value[:-len("-vendored")] if value.endswith("-vendored") else value
        if base in known:
            return token + value[len(base):], 1
    return value, 0


def _rule_extraction_stamp(obj, ctx):
    if not isinstance(obj, dict) or "extraction" not in obj:
        return obj, 0
    obj["extraction"], n = _stamp_token(obj.get("extraction"), ctx.get("extraction_stamps", ()),
                                        "<EXTRACTION_STAMP>")
    return obj, n


def _rule_update_extraction(obj, ctx):
    ext = obj.get("extraction") if isinstance(obj, dict) else None
    n = 0
    if isinstance(ext, dict):
        for k in [k for k in ("was", "now") if k in ext]:
            ext[k], m = _stamp_token(ext.get(k), ctx.get("extraction_stamps", ()), "<EXTRACTION_STAMP>")
            n += m
    return obj, n


def _rule_rebuild_record_stamp(obj, ctx):
    if not isinstance(obj, dict) or "stamp" not in obj:
        return obj, 0
    obj["stamp"], n = _stamp_token(obj.get("stamp"), ctx.get("code_stamps", ()), "<CODE_STAMP>")
    return obj, n


def _rule_rebuild_record_mtime(obj, ctx):
    g = obj.get("graph") if isinstance(obj, dict) else None
    if isinstance(g, dict) and "mtime_ns" in g:
        g["mtime_ns"] = "<VOLATILE>"
        return obj, 1
    return obj, 0


def _rule_manifest_seen(obj, ctx):
    n = 0
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, dict) and "seen" in v:
                v["seen"] = "<VOLATILE>"
                n += 1
    return obj, n


_SECONDS_KEYS = ("seconds", "index_seconds", "ms")


def _rule_update_timings(obj, ctx):
    n = 0

    def walk(x):
        nonlocal n
        if isinstance(x, dict):
            for k in list(x):
                if k in _SECONDS_KEYS and isinstance(x[k], (int, float)):
                    x[k] = "<VOLATILE>"
                    n += 1
                else:
                    walk(x[k])
        elif isinstance(x, list):
            for y in x:
                walk(y)
    walk(obj)
    return obj, n


def _rule_update_snapshot(obj, ctx):
    snap = obj.get("snapshot") if isinstance(obj, dict) else None
    n = 0
    if isinstance(snap, dict):
        for k in ("id", "created_at"):
            if k in snap:
                snap[k] = "<VOLATILE>"
                n += 1
    return obj, n


def member_sig(ids) -> str:
    """``cluster.community_member_sigs`` of one community: sha256 of the sorted ids, NUL-separated, 16 hex."""
    import hashlib

    h = hashlib.sha256()
    for nid in sorted(str(n) for n in ids):
        h.update(nid.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def root_bearing_members(members: list[str], owned: set[str], slug: str, target: str,
                         max_tries: int = 4096) -> list[str] | None:
    """The member ids as the pipeline had them before ``portable_ids.strip_root_from_ids`` removed the root
    slug, found as the variant whose :func:`member_sig` is ``target``: an ``_elem_`` id may have been
    ``_elem_<slug>_`` and an id of a node without a source file ``<slug>_<id>``. None when no variant (the
    obvious ones first, then every combination up to ``max_tries``) gives ``target``."""
    import itertools

    lead, mid = slug + "_", "_elem_" + slug + "_"

    def elem(m):
        return m.replace("_elem_", mid)

    opts = []
    for m in members:
        o = [m]
        if "_elem_" in m:
            o.append(elem(m))
        if m not in owned:
            o.append(lead + m)
            if m.endswith("_unresolved"):  # the stripped id was taken: strip_root_from_ids added the suffix
                o.append(lead + m[:-len("_unresolved")])
        opts.append(o)
    guesses = [[o[-1] for o in opts], [o[1] if len(o) > 1 else o[0] for o in opts],
               [elem(m) if "_elem_" in m else m for m in members]]
    for g in guesses:
        if member_sig(g) == target:
            return g
    n = 1
    for o in opts:
        n *= len(o)
    if n > max_tries:
        return None
    for combo in itertools.product(*opts):
        if member_sig(combo) == target:
            return list(combo)
    return None


def _rule_labels_sig(obj, ctx):
    graph, slug = ctx.get("graph"), ctx.get("slug")
    if not isinstance(obj, dict) or not isinstance(graph, dict) or not slug:
        return obj, 0
    members: dict[str, list[str]] = {}
    owned = set()
    for nd in graph.get("nodes", []):
        members.setdefault(str(nd.get("community")), []).append(str(nd.get("id")))
        if nd.get("source_file"):
            owned.add(str(nd.get("id")))
    n = 0
    for cid, val in obj.items():
        ms = members.get(str(cid))
        if not ms or not isinstance(val, str) or val == member_sig(ms):
            continue  # root-independent: compared as it is
        found = root_bearing_members(ms, owned, slug, val)
        if found is not None:
            obj[cid] = "<SIG " + member_sig([m.replace(slug, SLUG_TOKEN) for m in found]) + ">"
            n += 1
    return obj, n


def _rule_record_sig_hash(obj, ctx):
    import hashlib

    atlas = ctx.get("atlas")
    if not isinstance(obj, dict) or atlas is None or not isinstance(obj.get("sig"), str):
        return obj, 0
    try:
        digest = hashlib.sha256((atlas / "index" / ".graphify_labels.json.sig").read_bytes()).hexdigest()
    except OSError:
        return obj, 0
    if obj["sig"] == digest:
        obj["sig"] = "<SHA256 of this side's .graphify_labels.json.sig>"
        return obj, 1
    return obj, 0


def _rule_record_key(obj, ctx):
    if not isinstance(obj, dict) or "key" not in obj:
        return obj, 0
    held = canonical_json(obj.get("communities"))
    if SLUG_TOKEN in held or ROOT_TOKEN in held:
        obj["key"] = "<GRAPH_KEY of a root-bearing pipeline graph>"
        return obj, 1
    return obj, 0


def _rule_lexicon_built_at(obj, ctx):
    return _set_keys(obj, ("built_at",), "<VOLATILE>")


def _rule_receiver_graph_mtime(obj, ctx):
    g = obj.get("graph") if isinstance(obj, dict) else None
    if isinstance(g, dict) and "mtime_ns" in g:
        g["mtime_ns"] = "<VOLATILE>"
        return obj, 1
    return obj, 0


_REPORT_DATE = re.compile(r"^(# Graph Report - .*\()(\d{4}-\d{2}-\d{2})(\)\s*)$", re.M)


def _rule_report_date(text, ctx):
    return _REPORT_DATE.subn(r"\1<DATE>\3", text)


# -- SQLite rules: fn(table, row dict, ids) -> count, editing the row in place --

_ATLAS_CLOCK = {("snapshots", "created_at"), ("file_stat", "recorded_at_ns"), ("file_facts", "created_at")}


def _db_atlas_clock(table, row, ids):
    n = 0
    for (t, c) in _ATLAS_CLOCK:
        if t == table and c in row:
            row[c] = "<VOLATILE>"
            n += 1
    return n


_ATLAS_IDS = {("snapshots", "id"), ("snapshot_files", "snapshot_id")}


def _db_atlas_ids(table, row, ids):
    n = 0
    for (t, c) in _ATLAS_IDS:
        if t == table and row.get(c) is not None:
            row[c] = ids.setdefault(row[c], f"<SNAPSHOT{len(ids) + 1}>")
            n += 1
    return n


def _db_search_built_at(table, row, ids):
    if table == "meta" and row.get("key") == "built_at":
        row["value"] = "<VOLATILE>"
        return 1
    return 0


def _db_search_graph_mtime(table, row, ids):
    if table == "meta" and row.get("key") == "graph":
        try:
            v = json.loads(row["value"])
        except (TypeError, ValueError):
            return 0
        if isinstance(v, dict) and "mtime_ns" in v:
            v["mtime_ns"] = "<VOLATILE>"
            row["value"] = json.dumps(v, sort_keys=True)
            return 1
    return 0


RULES: list[Rule] = [
    Rule("root", "*", "B and C live in different directories; only the corpus root itself is replaced", None),
    Rule("root_slug", "*", "ids minted from an absolute path (python_facts.json's call targets) carry the root "
         "as an id slug; only that slug is replaced", None),
    Rule("build_stats_clock", "index/build_stats.json",
         "`at` is time.time() and `graph_seconds` a duration of the build", _rule_build_stats),
    Rule("extraction_stamp", "index/build_stats.json",
         "a hash of the extractor's source: equal to the baseline's or the candidate's own stamp; it differs "
         "by design when the candidate changes the extractor files (inert when they are the same)",
         _rule_extraction_stamp),
    Rule("update_extraction_stamp", "update_result",
         "the same stamps in the update result's `extraction` block", _rule_update_extraction),
    Rule("code_stamp", "index/rebuild_record.json",
         "a hash of project_index + index.py + portable_ids (and the Python version): the running code's "
         "identity, differs by design when the candidate changes those files (inert when they are the same)",
         _rule_rebuild_record_stamp),
    Rule("rebuild_record_graph_mtime", "index/rebuild_record.json",
         "graph.mtime_ns is when graph.json was written (its size and sha256 are still compared)",
         _rule_rebuild_record_mtime),
    Rule("receiver_graph_mtime", "index/receiver_calls.json",
         "graph.mtime_ns is when graph.json was written (the sidecar's key; size and sha256 still compared); "
         "seen in calibration", _rule_receiver_graph_mtime),
    Rule("labels_sig", "index/.graphify_labels.json.sig",
         "a community's signature hashes the pipeline's member ids, and ids minted from an absolute path "
         "(.dmf `_elem_` ids, path-named nodes) still hold the root there: such a value is replaced by the "
         "signature of the same ids with the root slug as a placeholder, after checking that those ids hash "
         "to the value found; seen in calibration (fixtures, sample.dmf)", _rule_labels_sig),
    Rule("rebuild_record_sig", "index/rebuild_record.json",
         "`sig` is the sha256 of the .sig file (root-dependent, see labels_sig): replaced only when it is the "
         "hash of this side's .sig file, which is compared on its own", _rule_record_sig_hash),
    Rule("rebuild_record_key", "index/rebuild_record.json",
         "`key` hashes the pipeline's graph before its ids were made portable: replaced only when the "
         "record's communities hold root-bearing ids (the graph and the communities are compared)",
         _rule_record_key),
    Rule("backup_dir_date", "index_listing",
         "the pipeline's backup folder of a labelled graph is named after today's date (index/<YYYY-MM-DD>/): "
         "the name becomes <DATE>; the files in it are compared under the rules of the files they copy; "
         "seen in calibration (fixtures)", None),
    Rule("lexicon_built_at", "index/lexicon.json", "`built_at` is the clock (lexicon.now()); seen in a serial "
         "calibration run (parallel runs often finish in the same second)", _rule_lexicon_built_at),
    Rule("manifest_seen", "index/manifest.json",
         "`seen` is the time the build looked at the file (the file's own mtime is pinned and compared)",
         _rule_manifest_seen),
    Rule("update_timings", "update_result", "`seconds`, `index_seconds` and `ms` are durations",
         _rule_update_timings),
    Rule("update_snapshot_id", "update_result",
         "the snapshot id is uuid4 and created_at the clock (the snapshot rows are compared in atlas.db)",
         _rule_update_snapshot),
    Rule("report_date", "index/GRAPH_REPORT.md",
         "the report header carries today's date (differs only when the two runs straddle midnight)",
         _rule_report_date),
    Rule("atlas_clock_columns", "atlas.db",
         "snapshots.created_at, file_stat.recorded_at_ns and file_facts.created_at are clock readings",
         _db_atlas_clock),
    Rule("atlas_snapshot_ids", "atlas.db",
         "snapshot ids are uuid4: replaced by their order of creation, snapshot_files keep pointing at them",
         _db_atlas_ids),
    Rule("search_meta_built_at", "index/search.db", "meta.built_at is time.time() of the search-index build",
         _db_search_built_at),
    Rule("search_meta_graph_mtime", "index/search.db",
         "meta.graph.mtime_ns is when graph.json was written (its size and sha256 are still compared)",
         _db_search_graph_mtime),
]
_RULES_BY_ARTIFACT: dict[str, list[Rule]] = {}
for _r in RULES:
    if _r.fn is not None:
        _RULES_BY_ARTIFACT.setdefault(_r.artifact, []).append(_r)


def apply_rules(artifact: str, obj, ctx: dict) -> tuple[object, list[str]]:
    """Apply the named volatile rules of ``artifact``; return the object and the names of the rules that
    changed something."""
    fired = []
    for r in _RULES_BY_ARTIFACT.get(artifact, ()):
        obj, n = r.fn(obj, ctx)
        if n:
            fired.append(r.name)
    return obj, fired


def canonical_json(obj) -> str:
    """One stable text for a JSON-like object: sorted keys, no whitespace, non-ASCII kept."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


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


def sqlite_dump(db: Path, root: str, rules=(), first: tuple[str, ...] = ()) -> tuple[dict, list[str]]:
    """Every table's rows in a stable order (each row as text, sorted), and the schema, with the corpus root
    replaced and the DB rules applied (``rule.fn(table, row, ids) -> count`` edits the row dict in place;
    ``ids`` is shared by all tables). Tables are read in rowid order (``first`` before the others) so a
    rule that numbers random ids numbers them in creation order. Returns ``(dump, fired rule names)``."""
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    fired: list[str] = []
    try:
        out: dict = {"schema": sorted([[t, n, replace_root(s or "", root)] for t, n, s in con.execute(
            "SELECT type, name, sql FROM sqlite_master")])}
        ids: dict[str, str] = {}
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for t in [t for t in first if t in tables] + [t for t in tables if t not in first]:
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
            try:
                rows = con.execute(f'SELECT * FROM "{t}" ORDER BY rowid').fetchall()
            except sqlite3.OperationalError:  # WITHOUT ROWID
                rows = con.execute(f'SELECT * FROM "{t}"').fetchall()
            norm = []
            for row in rows:
                d = dict(zip(cols, row))
                for r in rules:
                    if r.fn(t, d, ids) and r.name not in fired:
                        fired.append(r.name)
                vals = []
                for c in cols:
                    v = d[c]
                    if isinstance(v, str):
                        v = replace_root(v, root)
                    elif isinstance(v, bytes):
                        v = "bytes:" + v.hex()
                    vals.append(v)
                norm.append(vals)
            norm.sort(key=canonical_json)
            out[t] = {"columns": cols, "rows": norm}
        return out, fired
    finally:
        con.close()


def communities_of(graph: dict) -> dict:
    """``{community id: {"name": ..., "members": sorted ids}}`` from graph.json's nodes."""
    out: dict = {}
    for n in graph.get("nodes", []):
        cid = n.get("community")
        c = out.setdefault(str(cid), {"names": set(), "members": []})
        c["names"].add(str(n.get("community_name")))
        c["members"].append(n.get("id"))
    return {k: {"names": sorted(v["names"]), "members": sorted(map(str, v["members"]))}
            for k, v in sorted(out.items())}


# -- the environment of a run ---------------------------------------------------------------------------------

@dataclass
class Env:
    work: Path
    python: str

    def git_env(self) -> dict:
        cfg = self.work / "gitconfig"
        if not cfg.exists():
            cfg.write_text("[user]\n\tname = eq\n\temail = eq@example.invalid\n[core]\n\tautocrlf = false\n"
                           "[init]\n\tdefaultBranch = main\n", encoding="utf-8")
        e = dict(os.environ)
        # the corpus repositories are synthetic: the user's own git config (hooks, autocrlf, signing,
        # templates) must not change them, so a config of their own replaces it
        e.update(GIT_CONFIG_GLOBAL=str(cfg), GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_DATE=GIT_DATE,
                 GIT_COMMITTER_DATE=GIT_DATE, GIT_TERMINAL_PROMPT="0")
        return e

    def run_env(self, tree: Path, seed: int) -> dict:
        e = self.git_env()
        for k in list(e):
            if k.startswith(("GRAPHIFY_", "VERINODA_")):
                del e[k]  # an outer setting (GRAPHIFY_OUT above all) would move or change the index
        cfgdir = self.work / "user-config"
        cfgdir.mkdir(parents=True, exist_ok=True)
        e.update(PYTHONPATH=str(tree), PYTHONHASHSEED=str(seed), PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                 VERINODA_NO_AUTO_INDEX="1", VERINODA_OCR="0", VERINODA_CONFIG_DIR=str(cfgdir),
                 VERINODA_CACHE_DIR=str(self.work / "user-cache"),
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
    r = subprocess.run(["git", *args], cwd=cwd, env=env.git_env(), capture_output=True, text=True)
    if r.returncode:
        raise HarnessError(f"git {' '.join(args)} in {cwd} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def run_verinoda(env: Env, tree: Path, seed: int, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([env.python, "-m", "verinoda.cli", *args], cwd=tree, env=env.run_env(tree, seed),
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=RUN_TIMEOUT)


def run_python(env: Env, tree: Path, seed: int, code: str, *args: str) -> str:
    r = subprocess.run([env.python, "-c", code, *args], cwd=tree, env=env.run_env(tree, seed),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=RUN_TIMEOUT)
    if r.returncode:
        raise HarnessError(f"python in {tree} failed: {r.stderr.strip()[-800:]}")
    return r.stdout


_PROBE = """
import json, verinoda
from verinoda import buildlock, index
print(json.dumps({"file": verinoda.__file__, "extraction": buildlock.extraction_stamp(),
                  "code": index._code_stamp()}))
"""

_LOADER = """
import json, sys
from pathlib import Path
from verinoda import index
g = index.load(Path(sys.argv[1]))
G = g.G
nodes = [[n, dict(G.nodes[n])] for n in sorted(G.nodes, key=str)]
edges = sorted(([u, v, k, d] for u, v, k, d in G.edges(keys=True, data=True)),
               key=lambda e: json.dumps(e[:3], default=str))
Path(sys.argv[2]).write_text(json.dumps({"nodes": nodes, "edges": edges}, sort_keys=True, ensure_ascii=False,
                                        default=str, indent=0), encoding="utf-8")
"""


def probe_tree(env: Env, tree: Path) -> dict:
    """Where ``verinoda`` imports from when run for ``tree``, and the tree's stamps."""
    info = json.loads(run_python(env, tree, 0, _PROBE).strip().splitlines()[-1])
    got = Path(info["file"]).resolve()
    if tree.resolve() not in got.parents:
        raise HarnessError(f"verinoda for {tree} imports from {got}, not from that tree "
                           f"(an installed copy shadows PYTHONPATH?)")
    return info


# -- corpora --------------------------------------------------------------------------------------------------

def pin_mtimes(root: Path, paths=None, when: int = BASE_MTIME) -> None:
    targets = paths if paths is not None else [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts]
    for p in targets:
        p = root / p if not Path(p).is_absolute() else Path(p)
        if p.is_file():
            os.utime(p, (when, when))


def build_corpus(env: Env, name: str, baseline: Path, candidate: Path, dest: Path) -> Path:
    """The corpus ``name`` at ``dest`` (a git work tree with one commit)."""
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
    return dest


# -- edits ----------------------------------------------------------------------------------------------------

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
    return p.read_text(encoding="utf-8", errors="surrogateescape")


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(text)


def _nl(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


_PY_TOP = re.compile(r"^(?:def|class) (\w+)", re.M)


def edit_noop(repo: Path):
    return []


def edit_add_function(repo: Path):
    rel = _pick(repo, ("orders/pricing.py", "sample_calls.py", "sample.py"), ".py",
                lambda p: _PY_TOP.search(_read(p)) is not None)
    if rel is None:
        return None
    text = _read(repo / rel)
    target = _PY_TOP.search(text).group(1)
    nl = _nl(text)
    add = f"{nl}{nl}def eq_added_function():{nl}    return {target}(){nl}"
    _write(repo / rel, text.rstrip("\r\n") + add)
    return [rel]


_RETURN = re.compile(r"^(\s+)return (\S[^\r\n#]*?)\s*$", re.M)


def edit_body_edit(repo: Path):
    """A function body changes and nothing else: ``return X`` becomes ``return (X) if True else None``
    (same lines, no new name, no new call)."""
    rel = _pick(repo, ("orders/service.py", "sample.py"), ".py", lambda p: _RETURN.search(_read(p)) is not None)
    if rel is None:
        return None
    text = _read(repo / rel)
    m = _RETURN.search(text)
    new = f"{m.group(1)}return ({m.group(2)}) if True else None"
    _write(repo / rel, text[:m.start()] + new + text[m.end():])
    return [rel]


def edit_add_duplicate_stem(repo: Path):
    """A new file whose module stem (and file label) is an existing file's, defining a function of the same
    name as one there."""
    rel = _pick(repo, ("orders/pricing.py", "sample.py"), ".py",
                lambda p: re.search(r"^def (\w+)", _read(p), re.M) is not None
                or _PY_TOP.search(_read(p)) is not None)
    if rel is None:
        return None
    text = _read(repo / rel)
    m = re.search(r"^def (\w+)", text, re.M) or _PY_TOP.search(text)
    new_rel = f"eq_dup/{Path(rel).name}"
    _write(repo / new_rel, f'"""A second {Path(rel).name}."""\n\n\ndef {m.group(1)}(*args):\n    return None\n')
    return [new_rel]


def edit_delete_file(repo: Path):
    rel = _pick(repo, ("orders/config.py", "cookieHelpers.ts", "sample_calls.py"), ".py")
    if rel is None:
        return None
    (repo / rel).unlink()
    return []


def edit_objc_pair(repo: Path):
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
        _write(repo / h, ht[:hi] + f"- (void)eqReset;{nh}" + ht[hi:])
        _write(repo / m, mt[:mi] + f"- (void)eqReset {{{nm}    [self render];{nm}}}{nm}" + mt[mi:])
        return [h, m]
    return None


_GO_FUNC = re.compile(r"^func (\w+)\(", re.M)


def _go_append(repo: Path, fname: str):
    rel = _pick(repo, ("sample.go",), ".go", lambda p: _GO_FUNC.search(_read(p)) is not None)
    if rel is None:
        return None
    text = _read(repo / rel)
    target = _GO_FUNC.search(text).group(1)
    nl = _nl(text)
    _write(repo / rel, text.rstrip("\r\n") + f"{nl}{nl}func {fname}() {{{nl}\t{target}(){nl}}}{nl}")
    return rel


def edit_go(repo: Path):
    """A Go function added (no go.mod in the corpus yet)."""
    rel = _go_append(repo, "EqAdded")
    return None if rel is None else [rel]


def edit_go_mod(repo: Path):
    """A go.mod appears beside the Go file, and the Go file changes again."""
    rel = _go_append(repo, "EqAddedWithMod")
    if rel is None:
        return None
    mod = (Path(rel).parent / "go.mod").as_posix()
    if (repo / mod).exists():
        return [rel]
    _write(repo / mod, "module example.com/eqcorpus\n\ngo 1.21\n")
    return [rel, mod]


def edit_doc(repo: Path):
    rel = _pick(repo, ("README.md", "sample.md"), ".md")
    if rel is None:
        return None
    text = _read(repo / rel)
    nl = _nl(text)
    _write(repo / rel, text.rstrip("\r\n") + f"{nl}{nl}## Equality check{nl}{nl}A section the harness added.{nl}")
    return [rel]


def edit_add_data_file(repo: Path):
    """A file the graph does not read (plain text): the update re-indexes it for search only."""
    rel = "eq_notes/notes.txt"
    _write(repo / rel, "Notes the equality harness added.\nThey name compute_total and Server.\n")
    return [rel]


EDITS = {"noop": edit_noop, "add_function": edit_add_function, "body_edit": edit_body_edit,
         "add_duplicate_stem": edit_add_duplicate_stem, "edit_objc_pair": edit_objc_pair, "edit_go": edit_go,
         "edit_go_mod": edit_go_mod, "edit_doc": edit_doc, "add_data_file": edit_add_data_file,
         "delete_file": edit_delete_file, "noop_after": edit_noop}


# -- collecting and comparing the artifacts -------------------------------------------------------------------

JSON_ARTIFACTS = ("index/graph.json", "index/receiver_calls.json", "index/.graphify_labels.json",
                  "index/.graphify_labels.json.sig", "index/rebuild_record.json", "index/manifest.json",
                  "index/lexicon.json", "index/copies.json", "index/python_facts.json",
                  "index/python_cross.json", "index/empty_json.json", "index/build_stats.json")
TEXT_ARTIFACTS = ("index/GRAPH_REPORT.md", "index/.graphify_root")
DB_ARTIFACTS = ("index/search.db", "atlas.db")
DERIVED = ("update_exit", "update_result", "communities", "loaded_graph", "index_listing")
ALL_ARTIFACTS = DERIVED[:2] + JSON_ARTIFACTS + ("communities", "loaded_graph") + TEXT_ARTIFACTS + DB_ARTIFACTS \
    + ("index_listing",)
_NOT_LISTED = ("index/cache/", "build.lock", "background_update.log")


@dataclass
class Side:
    """What one copy's artifacts look like after a step."""
    items: dict = field(default_factory=dict)   # name -> ("bytes"|"json"|"text", value)


_DATED = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _dated_dirs(index: Path) -> list[Path]:
    """The dated backup folders the pipeline writes before overwriting a labelled graph (export.backup_if_
    protected: ``index/<today>/``), oldest first."""
    try:
        return sorted(d for d in index.iterdir() if d.is_dir() and _DATED.match(d.name))
    except OSError:
        return []


def _listing(atlas: Path) -> tuple[list[str], int]:
    """Files under ``.verinoda`` (caches, locks and SQLite side files aside), a dated folder's name replaced
    by ``<DATE>`` (rule ``backup_dir_date``); and how many names that rule changed."""
    out, n = [], 0
    for p in atlas.rglob("*"):
        if p.is_file():
            rel = p.relative_to(atlas).as_posix()
            if not rel.startswith(_NOT_LISTED) and not rel.endswith((".tmp", "-journal", "-wal", "-shm")):
                parts = rel.split("/")
                if len(parts) > 2 and parts[0] == "index" and _DATED.match(parts[1]):
                    parts[1] = "<DATE>"
                    n += 1
                out.append("/".join(parts))
    return sorted(out), n


RULE_HITS: dict[str, int] = {}


def _hit(names) -> None:
    for name in names:
        RULE_HITS[name] = RULE_HITS.get(name, 0) + 1


def _json_item(rule_key: str, p: Path, root: str, ctx: dict):
    """``(kind, value)`` of a JSON artifact, and the parsed object (None when absent or not JSON)."""
    if not p.exists():
        return ("text", "<absent>"), None
    text = replace_root(p.read_bytes().decode("utf-8", errors="surrogateescape"), root)
    try:
        obj = json.loads(text)
    except ValueError:
        return ("text", text), None
    obj2, fired = apply_rules(rule_key, json.loads(text), ctx)
    _hit(fired)
    # no rule touched it: compare the bytes (placeholder aside) - key order, spacing, escapes included; the
    # parsed object still gives the first differing JSON path
    return (("json", obj2) if fired else ("bytes", (text, obj))), obj


def _text_item(rule_key: str, p: Path, root: str, ctx: dict):
    if not p.exists():
        return "text", "<absent>"
    text = replace_root(p.read_bytes().decode("utf-8", errors="surrogateescape"), root)
    for r in _RULES_BY_ARTIFACT.get(rule_key, ()):
        text, n = r.fn(text, ctx)
        if n:
            _hit([r.name])
    return "text", text


def collect(repo: Path, update: subprocess.CompletedProcess | None, loaded: Path | None, ctx: dict,
            skip: set[str]) -> Side:
    root = str(repo)
    atlas = repo / ".verinoda"
    side = Side()
    items = side.items
    ctx = dict(ctx, root=root, slug=root_slug(root), atlas=atlas)
    try:  # the graph as written (ids unreplaced): rule labels_sig reconstructs member ids from it
        ctx["graph"] = json.loads((atlas / "index" / "graph.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ctx["graph"] = None
    if update is not None:
        items["update_exit"] = ("json", update.returncode)
        try:
            res = json.loads(update.stdout)
        except ValueError:
            res = {"<stdout>": update.stdout[-2000:], "<stderr>": update.stderr[-2000:]}
        res = replace_root_obj(res, root)
        res, fired = apply_rules("update_result", res, ctx)
        _hit(fired)
        items["update_result"] = ("json", res)
    for name in JSON_ARTIFACTS:
        items[name], obj = _json_item(name, atlas / name, root, ctx)
        if name == "index/graph.json" and obj is not None:
            items["communities"] = ("json", communities_of(obj))
    for name in TEXT_ARTIFACTS:
        items[name] = _text_item(name, atlas / name, root, ctx)
    # the dated backups: each file compared under the rules of the file it copies (backup_dir_date)
    for i, d in enumerate(_dated_dirs(atlas / "index")):
        tag = "<DATE>" if i == 0 else f"<DATE+{i}>"
        _hit(["backup_dir_date"])
        for f in sorted(x for x in d.iterdir() if x.is_file()):
            key = f"index/{f.name}"
            name = f"index/{tag}/{f.name}"
            if f.suffix == ".json":
                items[name], _ = _json_item(key, f, root, dict(ctx, graph=None))
            else:
                items[name] = _text_item(key, f, root, ctx)
    for name in DB_ARTIFACTS:
        p = atlas / name
        if p.exists():
            dump, fired = sqlite_dump(p, root, _RULES_BY_ARTIFACT.get(name, ()), first=("snapshots",))
            _hit(fired)
            items[name] = ("json", dump)
        else:
            items[name] = ("text", "<absent>")
    if loaded is not None:
        items["loaded_graph"] = ("json", json.loads(replace_root(loaded.read_text(encoding="utf-8"), root)))
    listing, n = _listing(atlas)
    if n:
        _hit(["backup_dir_date"])
    items["index_listing"] = ("json", listing)
    for s in skip:
        for k in [k for k in items if k == s or (s.startswith("index/") and k.endswith("/" + s[6:])
                                                 and "<DATE" in k)]:
            items.pop(k, None)
    return side


def compare(b: Side, c: Side) -> list[tuple[str, str]]:
    """``[(artifact, where)]`` for each artifact that differs."""
    out = []
    for name in sorted(set(b.items) | set(c.items), key=lambda n: (ALL_ARTIFACTS.index(n)
                                                                   if n in ALL_ARTIFACTS else 99, n)):
        if name not in b.items or name not in c.items:
            out.append((name, "only on one side"))
            continue
        (kb, vb), (kc, vc) = b.items[name], c.items[name]
        if kb == "bytes" and kc == "bytes":
            if vb[0] == vc[0]:
                continue
            d = first_json_diff(vb[1], vc[1])
            if d:
                out.append((name, f"{d[0]}: {d[1]} != {d[2]}"))
            else:
                ln = first_line_diff(vb[0], vc[0])
                out.append((name, f"same JSON, bytes differ at line {ln[0]}: {ln[1]} != {ln[2]}"))
            continue
        if kb == "bytes":
            vb = vb[1]
        if kc == "bytes":
            vc = vc[1]
        if isinstance(vb, str) and isinstance(vc, str):
            ln = first_line_diff(vb, vc)
            if ln:
                out.append((name, f"line {ln[0]}: {ln[1]} != {ln[2]}"))
            continue
        d = first_json_diff(vb, vc)
        if d:
            out.append((name, f"{d[0]}: {d[1]} != {d[2]}"))
    return out


# -- running the cases ----------------------------------------------------------------------------------------

@dataclass
class Case:
    corpus: str
    seed: int
    cache: str


def _both(serial: bool, fb, fc):
    if serial:
        return fb(), fc()
    with concurrent.futures.ThreadPoolExecutor(2) as ex:
        a, b = ex.submit(fb), ex.submit(fc)
        return a.result(), b.result()


def _setup_copy(env: Env, baseline: Path, repo: Path, seed: int) -> None:
    for cmd in (("init", str(repo), "--json"), ("scan", str(repo), "--json")):
        r = run_verinoda(env, baseline, seed, *cmd)
        if r.returncode:
            raise HarnessError(f"baseline `verinoda {cmd[0]}` of {repo} exited {r.returncode}: "
                               f"{(r.stderr or r.stdout).strip()[-800:]}")


def _loader(env: Env, baseline: Path, repo: Path, seed: int, out: Path) -> Path:
    run_python(env, baseline, seed, _LOADER, str(repo), str(out))
    return out


def run_case(env: Env, case: Case, baseline: Path, candidate: Path, edits: list[str], ctx: dict,
             skip: set[str], serial: bool, keep_going: bool) -> tuple[list[str], Path]:
    """Run one case; return the difference lines (empty when equal) and the case directory."""
    tag = f"{case.corpus} seed={case.seed} {case.cache}"
    cdir = env.work / f"{case.corpus}-s{case.seed}-{case.cache}"
    if cdir.exists():
        rmtree(cdir)
    cdir.mkdir(parents=True)
    t0 = time.monotonic()
    src = build_corpus(env, case.corpus, baseline, candidate, cdir / "src" / case.corpus)
    rb, rc = cdir / "B" / case.corpus, cdir / "C" / case.corpus
    for r in (rb, rc):
        shutil.copytree(src, r, copy_function=shutil.copy2)
    rmtree(cdir / "src")
    _both(serial, lambda: _setup_copy(env, baseline, rb, case.seed), lambda: _setup_copy(env, baseline, rc, case.seed))
    lb, lc = _both(serial, lambda: _loader(env, baseline, rb, case.seed, cdir / "B.loaded.json"),
                   lambda: _loader(env, baseline, rc, case.seed, cdir / "C.loaded.json"))
    diffs = compare(collect(rb, None, lb, ctx, skip), collect(rc, None, lc, ctx, skip))
    out: list[str] = []
    if diffs and not _RULES_BY_ARTIFACT:  # --no-rules: what the rules would have hidden is the point
        out += [f"{tag} step=scan0: {a}: {w}" for a, w in diffs]
    elif diffs:
        raise HarnessError(f"{tag}: the two BASELINE scans differ, so the baseline is not deterministic here: "
                           + "; ".join(f"{a}: {w}" for a, w in diffs[:5]))
    print(f"  {tag}: scan0 {'equal' if not diffs else 'DIFFERENT'} ({time.monotonic() - t0:.1f}s)", flush=True)
    for step, name in enumerate(edits, 1):
        t1 = time.monotonic()
        touched_b, touched_c = EDITS[name](rb), EDITS[name](rc)
        if touched_b is None or touched_c is None:
            print(f"  {tag}: {name}: skipped (no file of its kind in this corpus)", flush=True)
            continue
        when = BASE_MTIME + step * MTIME_STEP
        pin_mtimes(rb, touched_b, when)
        pin_mtimes(rc, touched_c, when)
        if case.cache == "cold":
            for r in (rb, rc):
                rmtree(r / ".verinoda" / "index" / "cache", ignore_errors=True)
        ub, uc = _both(serial, lambda: run_verinoda(env, baseline, case.seed, "update", str(rb), "--json"),
                       lambda: run_verinoda(env, candidate, case.seed, "update", str(rc), "--json"))
        t_upd = time.monotonic() - t1
        lb, lc = _both(serial, lambda: _loader(env, baseline, rb, case.seed, cdir / "B.loaded.json"),
                       lambda: _loader(env, baseline, rc, case.seed, cdir / "C.loaded.json"))
        diffs = compare(collect(rb, ub, lb, ctx, skip), collect(rc, uc, lc, ctx, skip))
        try:
            res = json.loads(ub.stdout)
            what = f"index_mode={res.get('index_mode', '?')} changed={res.get('changed_count', '?')}"
        except ValueError:
            what = "no JSON result"
        if ub.returncode:
            what += f" EXIT {ub.returncode}"  # both sides failing alike is equal, but worth seeing
        status = "equal" if not diffs else f"DIFFERENT ({len(diffs)} artifact(s))"
        print(f"  {tag}: {name}: {status} [baseline {what}] (updates {t_upd:.1f}s)", flush=True)
        for a, w in diffs:
            out.append(f"{tag} step={name}: {a}: {w}")
        if diffs and not keep_going:
            break
    return out, cdir


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
    ap.add_argument("--serial", action="store_true")
    ap.add_argument("--no-rules", action="store_true",
                    help="calibration aid: apply no volatile rule but the root placeholders, and report the scan0 "
                         "differences instead of stopping (shows what each rule hides)")
    a = ap.parse_args(argv)
    t_all = time.monotonic()
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
        if a.only and a.only not in EDITS:
            raise HarnessError(f"unknown edit {a.only!r} (known: {', '.join(EDITS)})")
        edits = [a.only] if a.only else list(DEFAULT_EDITS)
        skip = {s.strip() for s in a.skip.split(",") if s.strip()}
        if a.no_rules:
            _RULES_BY_ARTIFACT.clear()
        unknown = skip - set(ALL_ARTIFACTS)
        if unknown:
            raise HarnessError(f"unknown artifact(s) in --skip: {', '.join(sorted(unknown))}")
        work = (a.work.resolve() if a.work else Path(tempfile.mkdtemp(prefix="verinoda-eq-")))
        for t in (baseline, candidate):
            if work == t or t in work.parents:
                raise HarnessError(f"--work {work} is inside the checkout {t}")
        work.mkdir(parents=True, exist_ok=True)
        env = Env(work=work, python=a.python)
        pb = probe_tree(env, baseline)
        pc = pb if candidate == baseline else probe_tree(env, candidate)
        ctx = {"extraction_stamps": {pb["extraction"], pc["extraction"]},
               "code_stamps": {s for s in (pb["code"], pc["code"]) if s}}
        print(f"baseline  {baseline} (extraction {pb['extraction']})")
        print(f"candidate {candidate} (extraction {pc['extraction']})"
              + ("  [the same tree: calibration]" if candidate == baseline else ""))
        if pb["extraction"] != pc["extraction"]:
            print("  note: the extraction stamps differ: the candidate's first update rebuilds the graph even "
                  "where the baseline's does not (update_result and atlas.db will say so)")
        print(f"work      {work}")
        all_diffs: list[str] = []
        timings = []
        for corpus in corpora:
            for seed in seeds:
                for cache in caches:
                    t0 = time.monotonic()
                    diffs, cdir = run_case(env, Case(corpus, seed, cache), baseline, candidate, edits, ctx, skip,
                                           a.serial, a.keep_going)
                    timings.append((f"{corpus} seed={seed} {cache}", time.monotonic() - t0))
                    all_diffs += diffs
                    if not diffs and not a.keep:
                        rmtree(cdir, ignore_errors=True)
                    elif diffs:
                        print(f"  kept for inspection: {cdir}")
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
    print("timings: " + ", ".join(f"{k} {v:.1f}s" for k, v in timings) + f"; total {total:.1f}s")
    print("volatile rules applied (artifact reads): "
          + (", ".join(f"{k} x{v}" for k, v in sorted(RULE_HITS.items())) or "none"))
    if all_diffs:
        print(f"DIFFERENT: {len(all_diffs)} difference(s)")
        for d in all_diffs:
            print(f"  {d}")
        return 1
    print("EQUAL: every case produced the same artifacts")
    if not a.keep and not a.work:
        rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
