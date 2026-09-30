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
below). Every command runs alone (one fixed folder per case), so a case takes minutes (fixtures: about 25
update steps, each three updates and five loads); run cases in parallel as separate invocations with their own
``--work``.

Exit status: 0 every case equal, 1 a difference (a summary names the case, the step, the artifact and the first
differing JSON path or line), 2 a harness error: a checkout that does not import from where it should, a
baseline ``init``/``scan``/``claim add``/``decide record``/``update`` that fails, the baseline's loader failing
on the baseline's own state, two baseline scans of the same corpus that differ (then the baseline itself is not
deterministic and nothing can be proved), the comparison changing the state it compares, a background build
that does not end, or a case in which no update step was compared.

What a case does
----------------
For each corpus and seed a corpus is built once (copied, a git-ignored file ``eq_local/notes.py`` added for a
claim to cite, every file's mtime pinned, ``git init`` and one commit with fixed dates and an isolated git config,
objects packed). It is copied three times: ``B`` and ``B2`` are set up (``verinoda init``, ``verinoda scan``) by
the BASELINE, ``C`` by the CANDIDATE (so the candidate's own rebuild record and caches are what its updates start
from). Then every copy gets the same decision record (made once by the baseline's ``decide record`` on a throwaway
copy of ``B``: a guard and a governed function, so every non-noop update runs the decision check) and the same
claims, each added by that side's own checkout with ``verinoda claim add`` (:func:`claim_targets`: one cites the
line ``body_edit`` changes, one the first definition of the ``add_function`` file, one the git-ignored file
``untracked_cited`` changes, one a line only ``human_edit`` changes; claim ids are random, see
``atlas_random_ids``). Every command runs with the copy MOVED INTO ONE FIXED FOLDER (``<case>/r/<corpus>``) and
moved back afterwards, one copy after the other: absolute paths in the state (``.graphify_root``, the rebuild
record, ids minted from a path, snapshot rows, the update result) are the same on every side, so nothing
path-shaped needs a rule. The wall-clock window of every run is recorded per side (the clock rules below). ``B``
against ``B2`` is the determinism check of the set-up (exit 2 when they differ); ``B`` against ``C`` is step
``scan``. Then ``C0`` is made: a copy of ``B`` as the baseline left it (repository, ``.verinoda`` with
``index/cache/``, the stat index, the manifest and the sidecars in the baseline's format, ``.git``, the user
folder), whose updates the CANDIDATE runs: the real upgrade path, where the candidate's first update reads state
an older build wrote. ``C0`` starts with ``B``'s run windows, graph mtimes and stamps (its state was written by
``B``'s runs); the candidate's stamps are accepted there besides the baseline's.

Each edit is planned ONCE (on ``B``) as a list of operations (write bytes, delete, rename, delete a folder, git
commit) and the same plan is applied to ``B``, ``C`` and ``C0``. Written files get the step's mtime: a whole
second at least one second after the previous run ended, and every run starts at least ``EDIT_GAP`` seconds after
it, so the stat caches' racy windows come out the same on both sides. The racy window itself is exercised by its
own two steps: ``racy_prepare`` moves a file's mtime ``RACY_AHEAD`` into the future (the same value on both sides:
every recording from then on lies within the racy margin of it, snapshot.RACY_MARGIN_NS and freshness), and
``racy_same_size`` gives that file a same-size edit that keeps the mtime: only the racy rule finds the change.
Then the BASELINE runs ``update`` in ``B`` and the CANDIDATE in ``C`` and ``C0``, and everything below is
compared, ``C`` with ``B`` and ``C0`` with ``B`` (a difference of ``C0`` is reported as ``[C0: the candidate
updating the baseline's state]``). A step runs ``update --json``, except ``fast_edit`` (``update --fast --json``;
the harness then waits for the background build the result names before anything is compared) and ``human_edit``
(``update`` without ``--json``: the human output is compared). ``retarget_call`` points one call at another
existing function on the same line (:func:`retarget_call`): the node set and the edge count stay, one edge changes
its target, so a candidate that keeps the graph when the counts match is caught. Edits are cumulative, in the
order of ``DEFAULT_EDITS``; an edit that finds no file of its kind is skipped and listed at the end (on
``orders``: the Objective-C, Go, Java and Rust edits, which ``fixtures`` covers; ``edit_json`` and ``edit_ts`` add
a file there instead).

Three more sides are made after the set-up (:meth:`Runner.make_derived`). ``B0``, only when a stamp the
candidate records (``STAMP_FILES``: extraction, code, python_facts, python_cross, empty_json) differs from the
baseline's: a copy of ``B`` with those recorded stamps replaced by ``eq-outdated-<kind>``, updated by the
BASELINE, and ``C0`` is compared with it instead of ``B`` (a candidate that changes an extractor rightly rebuilds
the baseline's state once; the baseline does the same only when its stamps are outdated too). ``BM`` and ``M``:
copies of ``B`` and ``C`` updated by the baseline and the candidate, every update of the case inside ONE
long-lived process of each (:class:`InProc`, the way the MCP server's ``index_update`` runs; the Store the CLI
leaves open is closed after each call, as the server does); ``M`` is compared with ``BM`` (the baseline itself
writes differently in a long-lived process: its stat index is written only at exit), so a memo that outlives
one build is exercised. ``relocate`` runs in a second fixed folder (``<case>/q/<corpus>``, ``RELOCATED``) and
adds a file, so the graph is rebuilt from AST cache entries written in the first folder; the next step moves
back.

Scope (what ``update`` may write, decided in round 4): files under the repository's ``.verinoda``, compared
as below, and nothing else. Writes elsewhere are looked for in the corpus (files and folders, with mode and
attributes), ``.git`` (files, objects, index entry flags), the side's user folder (config, cache, TEMP,
HOME/USERPROFILE, APPDATA, LOCALAPPDATA, XDG), beside the fixed folder, in the case and work folders
(``outside_files``: entries the harness did not make) and the two checkouts (``checkout_writes``, byte-code
caches aside). NOT covered, by decision (a candidate's diff is reviewed for these instead): NTFS alternate data
streams, writes to absolute paths outside the work folder and the redirected user folders, the registry, the
network, file times under ``.verinoda``, SQLite page contents and page_count (beyond freelist_count and
schema_version), handles a CLI run leaves open (the process exits).

Not covered: detect's same-tick guard (``_mtime_may_hide_a_rewrite``: an mtime less than 2 s before the
manifest's ``seen``) cannot be put inside a run on both sides alike (the sides run one after the other); what it
reads, ``seen``, is compared under the clock rule, so a candidate that records it differently is caught.

What is compared
----------------
Every file under ``.verinoda`` (``index/cache/`` included: the AST cache entries by bytes, the stat index
under ``stat_index_clock``; the lock files by content) except the SQLite ``-wal``/``-shm`` side files, whose
content is read with their database, and every folder under it (``verinoda_dirs``, an empty one included): as
bytes, after the named volatile rules below replaced ONLY the volatile value in the
raw text (the rest of the file, spacing and key order included, is compared byte for byte; the report gives the
first differing JSON path when both sides parse). A file present on one side only is a difference. The dated
backup folder the pipeline writes before overwriting a labelled graph (``index/<YYYY-MM-DD>/``) is named
``<DATE>`` when its date is a local date on which a run of that side ran (``backup_dir_date``; a folder named
after another date keeps its name, so it differs), and its files are compared under the rules of the files they
copy. SQLite files (``atlas.db``, ``index/search.db``, found by their header) are read from a copy (with their
``-wal``): the pragmas (user_version, application_id, page_size, encoding, auto_vacuum, journal_mode,
freelist_count, schema_version; not page_count, which the baseline varies in),
``sqlite_master`` in its own order, and every table (``sqlite_sequence`` included) in ROWID order with the rowid
itself (a WITHOUT ROWID table in its key order). Also compared, per step:

- ``update_exit`` / ``update_result`` / ``update_stderr`` (``update_human_*`` for ``human_edit``; ``scan_*`` and
  ``claim_<role>_*`` at the set-up): the exit status, stdout and stderr, as bytes (read without newline
  translation; rules below). The ``stale`` list of each update result names claims by their number, so the same
  claims must go stale on both sides.
- ``graph_kept``: whether the update left graph.json as it was (same mtime and bytes: the fast path), per side
- ``loaded_graph``: ``verinoda.index.load`` of each side's state, by the BASELINE code, dumped in the loaded
  graph's own node and edge order; ``loaded_graph_candidate``: the baseline's load of ``B`` against the
  CANDIDATE's load of ``C``. Each load runs on a throwaway copy (moved into the fixed folder), after the files
  were collected; ``loader_writes`` / ``loader_writes_candidate`` list what a load changed in that copy (a load
  that repairs a stale receiver sidecar writes it). Every file under the real ``.verinoda`` is hashed before
  the collection and after the loads: a change is a harness error.
- ``corpus``: size, mtime and sha256 of every file but the ROOT's ``.verinoda`` and ``.git`` (a nested
  ``sub/.verinoda/`` or ``sub/.git/`` is compared); ``git_files``: size and sha256 of every file under ``.git``
  but ``logs/``, ``objects/info/``, the packs' ``.idx``, ``index``, ``ORIG_HEAD`` and ``FETCH_HEAD`` (config,
  info/, hooks/, refs, HEAD, packed-refs, loose objects, pack files, ...: :func:`git_uncompared`);
  ``git_objects``: every object of the store, loose or packed (``git cat-file --batch-all-objects
  --batch-check``: name, type, size); ``git_state``: HEAD and ``git status --porcelain``
- ``user_files``: every file and folder (size and sha256, no times) under the side's own user folder
  (``<case>/u/<side>``: ``VERINODA_CONFIG_DIR``, ``VERINODA_CACHE_DIR`` and ``TEMP``/``TMP``), so a write outside
  the repository (a trust entry, a cache, a temporary file left behind) is seen; ``outside_files``: what the
  side's runs left beside the fixed folder (in ``<case>/r/``; moved after every run to ``<case>/x/<side>``). A
  load's writes in its own user folder or beside the fixed folder are part of ``loader_writes``.

Volatile values (every rule is named; see ``RULES`` for the reason of each)
-------------------------------------------------------------------------
Inputs are pinned rather than outputs hidden where possible: file mtimes, git dates and config,
``PYTHONHASHSEED``, a user-level config and cache folder of each side's own, no auto-index, no OCR, no network.
What is left, found by running the baseline against itself (``--no-rules`` shows what the rules hide):

- clock readings (``build_stats_clock``, ``lexicon_built_at``, ``manifest_seen``, ``search_meta_built_at``,
  ``atlas_clock_columns`` - every ``*_at``/``*_at_ns`` column, ``file_stat.recorded_at_ns`` included -,
  ``output_clocks``): accepted only when the value falls inside the wall-clock window of a run of THAT side, and
  replaced by ``<CLOCK run LABEL SHAPE>`` naming the run and the kind of value (:func:`clock_shape`: ``int``,
  ``float``, ``float-whole``, ``ns``, ``ns-whole-second``, ``text-float``, or an ISO text with its digits written
  ``d``), so both sides must name the same run and write the same kind of value. Several of these are
  inputs of later runs (``seen`` feeds detect's racy-rewrite guard, ``recorded_at_ns`` the racy-clean check): a
  stale one names an earlier run, a future one no run at all, a whole second where the baseline writes a
  fraction another shape. ``report_date`` accepts only a local date on which a run of that side ran.
- durations (``build_stats_duration``, ``update_timings``, ``output_durations``), the background build's pid
  (``update_background_pid``) and the backup folder's date (``backup_dir_date``, above)
- graph.json's write time where another file records it (``receiver_graph_mtime``,
  ``rebuild_record_graph_mtime``, ``search_meta_graph_mtime``): replaced only by a value THIS side's graph.json
  had after a run the harness watched, named by that run (``<GRAPH_MTIME after add_function>``), so both sides
  must point at the graph of the same step
- random ids (``atlas_random_ids``, ``output_ids``): snapshot, claim and evidence ids (``prefix_`` and 12 hex
  digits) are numbered by prefix in the rowid order of the atlas.db table whose ``id`` column holds them
  (``<clm#2>``), wherever they appear (a cell, a JSON payload, an update result, the human output); an id this
  side's atlas.db does not hold is compared as it is
- code identities (``extraction_stamp``, ``update_extraction_stamp``, ``code_stamp``, ``python_facts_stamp``,
  ``python_cross_stamp``, ``empty_json_stamp``): replaced only when they are THAT side's own checkout's stamp
  (the baseline's for ``B``, the candidate's for ``C``, either for ``C0``; ``-vendored`` suffix allowed), so a
  stale stamp on ``B`` or ``C`` is a difference; ``atlas_written_by`` likewise for the build named in atlas.db's
  ``meta.schema_written_by`` (version and commit)

Known limits: SQLite files are compared by content and the two layout pragmas (freelist_count, schema_version), not page by page
and not by page_count (the baseline differs from itself in it). A clock's fraction digits are not
compared (``repr(time.time())`` has 3 or fewer about once in 3000 values: the baseline would differ from
itself), so rounding a clock to milliseconds is not caught, dropping the fraction is. When the candidate changes the
extractor files, the stamps differ by design and are each accepted on their own side only. The background build
of ``update --fast`` runs in isolated mode (``-I``), so ``PYTHONHASHSEED`` does not reach it.
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

DEFAULT_EDITS = ("noop", "add_function", "body_edit", "retarget_call", "relocate", "comment_only",
                 "add_duplicate_stem",
                 "edit_objc_pair", "edit_go", "edit_go_mod", "edit_json", "edit_ts", "edit_java", "edit_rust",
                 "edit_doc", "add_data_file", "same_size_keep_mtime", "racy_prepare", "racy_same_size",
                 "untracked_cited", "fast_edit", "human_edit", "rename_file", "delete_file", "delete_dir",
                 "commit_edit", "noop_after")
# how a step runs update: "json" (`update --json`), "fast" (`update --fast --json`, then the harness waits for the
# background build it started) or "human" (`update` without --json: its human output is compared)
STEP_MODES = {"fast_edit": "fast", "human_edit": "human"}
# steps whose runs (and loads) use the second fixed folder <case>/q/<corpus> instead of <case>/r/<corpus>
RELOCATED = frozenset({"relocate"})
CORPORA = ("fixtures", "orders", "self")
BASE_MTIME = 1_700_000_000  # the corpus files' mtime (long before any run)
EDIT_GAP = 3                # seconds from an edit's mtime to the next run (> the 2 s racy windows)
GIT_DATE = "2024-01-01T00:00:00+00:00"
RUN_TIMEOUT = 3600
WRAPPER_EXIT = 97           # the wrapper found verinoda imported from somewhere else
SQLITE_MAGIC = b"SQLite format 3\x00"
EXCLUDED_PREFIXES: tuple = ()
EXCLUDED_SUFFIXES = ("-wal", "-shm")
EXCLUDED_FILES: tuple = ()
# a temporary backup the atomic replace of project_index/paths.py leaves (tempfile.mkstemp: a random name)
_REPLACE_BAK = re.compile(r"(^|/)\.gfy-replace-bak-[^/]+\.tmp$")
BASELINE_SIDES = ("B", "B2", "B0", "BM")


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
    seen after its runs (value -> the run's label), its random ids (value -> ``<prefix#n>``, numbered in the
    rowid order of the atlas.db table that holds them) and the wall-clock window of every run on this side
    (``(label, start, end)`` in seconds since the epoch, in run order). ``also``: stamps accepted besides its
    own (side ``C0``, the baseline's state updated by the candidate, may keep the baseline's)."""
    name: str
    stamps: dict = field(default_factory=dict)
    also: dict = field(default_factory=dict)
    graph_mtimes: dict = field(default_factory=dict)
    ids: dict = field(default_factory=dict)
    windows: list = field(default_factory=list)


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
    for r in rules:  # the JSON value rules first, all on the raw text (their spans are offsets into it) ...
        if r.text is not None:
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
    for r in rules:  # ... then the text rules, in order, on the result
        if r.text is not None:
            text, n = r.text(text, sctx)
            if n and r.name not in fired:
                fired.append(r.name)
    return text, fired


# -- the checks of the volatile rules ---------------------------------------------------------------------------

_ISO_CLOCK = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
CLOCK_SLACK = 0.01  # seconds: a clock value rounded to 1/100 s may fall this far before its run's start


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def clock_run(seconds: float, s: SideCtx, whole_second: bool = False) -> str | None:
    """The label of the run of this side whose wall-clock window holds ``seconds`` (a value truncated to a
    whole second may lie up to a second before the window's start); None when no run of this side was going
    on then: a stale value, one from the future, or one from the other side's runs."""
    for label, start, end in s.windows:
        lo = math.floor(start) if whole_second else start - CLOCK_SLACK
        if lo <= seconds <= end + CLOCK_SLACK:
            return label
    return None


def iso_seconds(text: str) -> tuple[float, bool] | None:
    """``(seconds since the epoch, whole second?)`` of an ISO 8601 date and time (no zone: UTC), else None."""
    if not isinstance(text, str) or not _ISO_CLOCK.fullmatch(text):
        return None
    from datetime import datetime, timezone
    try:
        d = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp(), d.microsecond == 0


def clock_shape(v, ns: bool = False) -> str:
    """The kind of value a clock was written as, kept in its token so both sides must write the same kind:
    ``int`` / ``float`` (seconds), ``float-whole`` (a float with no fraction: ``float(int(time.time()))``),
    ``ns`` / ``ns-whole-second`` (nanoseconds, the second when the value is a whole second), ``float-ns``, and
    for ISO text the text with every digit written ``d`` (``dddd-dd-ddTdd:dd:dd+dd:dd``: separators, fraction
    digits and zone). A fraction's digit count is not kept: ``repr(time.time())`` has 3 digits or fewer about
    once in 3000 values, so it would make the baseline differ from itself; only a whole value is (a real clock
    is a whole second about once in 10**7)."""
    if isinstance(v, str):
        return re.sub(r"\d", "d", v)
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return ("ns-whole-second" if v % 1_000_000_000 == 0 else "ns") if ns else "int"
    if isinstance(v, float):
        return ("float-ns" if ns else "float-whole" if v.is_integer() else "float")
    return type(v).__name__


def _clock_token(label: str | None, shape: str) -> str | None:
    return None if label is None else f"<CLOCK run {label} {shape}>"


def _clock_number(v, s):
    """A clock in seconds (time.time()): accepted only inside a run of this side."""
    return _clock_token(clock_run(float(v), s), clock_shape(v)) if _is_number(v) else None


def _clock_ns(v, s):
    """A clock in nanoseconds (time.time_ns()): accepted only inside a run of this side."""
    return _clock_token(clock_run(v / 1e9, s), clock_shape(v, ns=True)) if _is_number(v) else None


def _clock_text(v, s):
    """An ISO date and time: accepted only inside a run of this side."""
    got = iso_seconds(v)
    return None if got is None else _clock_token(clock_run(got[0], s, whole_second=got[1]), clock_shape(v))


def _clock_numeric_text(v, s):
    """A clock in seconds written as text (``json.dumps(time.time())``): its shape is ``text-`` and the shape of
    the number the text holds (``text-float``; ``text-other`` when it is not a JSON number)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not isinstance(v, str) or not math.isfinite(f):
        return None
    try:
        num = json.loads(v)
    except ValueError:
        num = None
    return _clock_token(clock_run(f, s), "text-" + (clock_shape(num) if _is_number(num) else "other"))


def replace_clock_texts(text: str, s: SideCtx) -> tuple[str, int]:
    """Every ISO date and time inside ``text`` that falls in a run of this side, replaced by its run's
    token (the others are kept, so they are compared)."""
    n = 0

    def sub(m):
        nonlocal n
        tok = _clock_text(m.group(0), s)
        if tok is None:
            return m.group(0)
        n += 1
        return tok
    return _ISO_CLOCK.sub(sub, text), n


def _duration(v, s):
    return "<DURATION>" if _is_number(v) else None


_RANDOM_ID = re.compile(r"\b[a-z]+_[0-9a-f]{12}\b")


def replace_known_ids(text: str, s: SideCtx) -> tuple[str, int]:
    """Every random id (``new_id``: ``prefix_`` and 12 hex digits) of THIS side inside ``text`` replaced by its
    number (``<clm#2>``); ids this side's atlas.db does not hold are kept."""
    n = 0

    def sub(m):
        nonlocal n
        tok = s.ids.get(m.group(0))
        if tok is None:
            return m.group(0)
        n += 1
        return tok
    return _RANDOM_ID.sub(sub, text), n


def number_ids(ids, s: SideCtx) -> None:
    """Give each new random id in ``ids`` (in order) the next number of its prefix: ``<snp#1>``, ``<clm#1>``."""
    counts: dict = {}
    for tok in s.ids.values():
        p = tok[1:tok.index("#")]
        counts[p] = counts.get(p, 0) + 1
    for v in ids:
        if isinstance(v, str) and _RANDOM_ID.fullmatch(v) and v not in s.ids:
            p = v[:v.index("_")]
            counts[p] = counts.get(p, 0) + 1
            s.ids[v] = f"<{p}#{counts[p]}>"


_DURATION_TEXT = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?= ?(?:s|ms|seconds?)\b)")


def replace_durations(text: str, s=None) -> tuple[str, int]:
    """Durations written in text (``0.5s``, ``12 ms``, ``2.1 seconds``) replaced by ``<DURATION>``."""
    return _DURATION_TEXT.subn("<DURATION>", text)


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
        got = stamp_token(v, s.stamps.get(kind), token)
        return got if got is not None else stamp_token(v, s.also.get(kind), token)
    return check


def _graph_mtime(v, s: SideCtx):
    label = s.graph_mtimes.get(v) if isinstance(v, int) and not isinstance(v, bool) else None
    return None if label is None else f"<GRAPH_MTIME after {label}>"


_REPORT_DATE = re.compile(r"^(# Graph Report - .*\()(\d{4}-\d{2}-\d{2})(\)[ \t]*)$", re.MULTILINE)


def _report_date_of_run(date: str, s: SideCtx) -> str | None:
    """The label of a run of this side during which the local date was ``date`` (the report header and the
    backup folder are both named after ``date.today()``, the local date)."""
    for label, start, end in s.windows:
        if date in {time.strftime("%Y-%m-%d", time.localtime(t)) for t in (start, end)}:
            return label
    return None


def _report_date(text, s):
    n = 0

    def sub(m):
        nonlocal n
        if _report_date_of_run(m.group(2), s) is None:
            return m.group(0)
        n += 1
        return m.group(1) + "<DATE>" + m.group(3)
    return _REPORT_DATE.sub(sub, text), n


def _text_ids(text, s):
    return replace_known_ids(text, s)


def _text_clocks(text, s):
    return replace_clock_texts(text, s)


# -- SQLite rules: fn(table, row dict, SideCtx) -> count, editing the row in place ----------------------------

def _is_clock_column(col: str) -> bool:
    return col.endswith(("_at", "_at_ns"))


def _db_atlas_clock(table, row, s):
    """Every ``*_at`` / ``*_at_ns`` column (created_at, updated_at, verified_at, collected_at, recorded_at_ns,
    ...), and ISO clocks written inside other text cells (JSON payloads), accepted only inside a run of this
    side."""
    n = 0
    for c, v in list(row.items()):
        if _is_clock_column(c):
            rep = (_clock_ns(v, s) if c.endswith("_ns") else _clock_number(v, s)) if _is_number(v) \
                else _clock_text(v, s)
            if rep is not None:
                row[c] = rep
                n += 1
        elif isinstance(v, str) and _ISO_CLOCK.search(v):
            text, k = replace_clock_texts(v, s)
            if k:
                row[c] = text
                n += k
    return n


def _db_atlas_ids(table, row, s):
    """Random ids (numbered before the rows are read, see :func:`sqlite_dump`) wherever a cell holds one."""
    n = 0
    for c, v in list(row.items()):
        if isinstance(v, str) and c != "<rowid>":
            text, k = replace_known_ids(v, s)
            if k:
                row[c] = text
                n += k
    return n


def _db_written_by(table, row, s):
    if table == "meta" and row.get("key") == "schema_written_by":
        rep = stamp_token(row.get("value"), s.stamps.get("server_version"), "<SERVER_VERSION>")
        if rep is None:
            rep = stamp_token(row.get("value"), s.also.get("server_version"), "<SERVER_VERSION>")
        if rep is not None:
            row["value"] = rep
            return 1
    return 0


def _db_search_built_at(table, row, s):
    if table == "meta" and row.get("key") == "built_at":
        rep = _clock_numeric_text(row.get("value"), s)
        if rep is not None:
            row["value"] = rep
            return 1
    return 0


def _db_search_graph_mtime(table, row, s):
    if table == "meta" and row.get("key") == "graph" and isinstance(row.get("value"), str):
        text, fired = mask_text("", row["value"], s, [Rule("", (), "", (("mtime_ns",),), _graph_mtime)])
        if fired:
            row["value"] = text
            return 1
    return 0


def _pid(v, s):
    return "<PID>" if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


# the artifacts a run's own output is compared as (see run_items)
JSON_OUT, TEXT_OUT, ERR_OUT = "<json stdout>", "<text stdout>", "<stderr>"

RULES: list[Rule] = [
    Rule("build_stats_clock", ("index/build_stats.json",),
         "`at` is time.time() of the build: accepted only inside a run of this side, named by that run",
         (("at",),), _clock_number),
    Rule("build_stats_duration", ("index/build_stats.json",), "`graph_seconds` is a duration of the build",
         (("graph_seconds",),), _duration),
    Rule("extraction_stamp", ("index/build_stats.json",),
         "a hash of the extractor's source: must be this side's checkout's own stamp",
         (("extraction",),), _side_stamp("extraction", "<EXTRACTION_STAMP>")),
    Rule("update_extraction_stamp", (JSON_OUT,),
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
    Rule("lexicon_built_at", ("index/lexicon.json",),
         "`built_at` is the clock (lexicon.now()): accepted only inside a run of this side",
         (("built_at",),), _clock_text),
    Rule("manifest_seen", ("index/manifest.json",),
         "`seen` is when the build looked at the file (the racy-rewrite guard reads it): accepted only inside a "
         "run of this side, named by that run, so a `seen` kept from an earlier run is a difference",
         (("*", "seen"),), _clock_number),
    Rule("stat_index_clock", ("index/cache/stat-index.json",),
         "`indexed_at_ns` is when the stat index hashed the file (time.time_ns(); the racy-clean check reads it): "
         "accepted only inside a run of this side, named by that run", (("*", "indexed_at_ns"),), _clock_ns),
    Rule("update_timings", (JSON_OUT,),
         "`seconds`, `index_seconds` and `ms` (at any depth) are durations",
         (("**", "seconds"), ("**", "index_seconds"), ("**", "ms")), _duration),
    Rule("update_background_pid", (JSON_OUT,), "`update --fast`: the background build's process id",
         (("background", "pid"),), _pid),
    Rule("output_ids", (JSON_OUT, TEXT_OUT, ERR_OUT, "index/background_update.log"),
         "random ids (snapshots, claims, evidence: prefix_ and 12 hex digits) must be ids of this side's "
         "atlas.db, numbered in the rowid order of their table", text=_text_ids),
    Rule("output_clocks", (JSON_OUT, TEXT_OUT, ERR_OUT, "index/background_update.log"),
         "ISO dates and times (a snapshot's or claim's created_at): accepted only inside a run of this side, "
         "named by that run", text=_text_clocks),
    Rule("output_durations", (TEXT_OUT, ERR_OUT, "index/background_update.log"),
         "durations written in text (`1.2 s`, `30 ms`)", text=replace_durations),
    Rule("report_date", ("index/GRAPH_REPORT.md",),
         "the report header carries the date: accepted only as a date on which a run of this side ran",
         text=_report_date),
    Rule("backup_dir_date", ("<listing>",),
         "the pipeline's backup folder of a labelled graph is named after today's date (index/<YYYY-MM-DD>/): "
         "the name becomes <DATE>; its files are compared under the rules of the files they copy"),
    Rule("atlas_clock_columns", ("atlas.db",),
         "every *_at / *_at_ns column (snapshots.created_at, file_stat.recorded_at_ns - the racy-clean check "
         "reads it -, claims.created_at/updated_at, evidence.collected_at, ...) and ISO clocks inside text cells: "
         "accepted only inside a run of this side, named by that run", db=_db_atlas_clock),
    Rule("atlas_random_ids", ("atlas.db",),
         "random ids (the `id` column of snapshots, claims, evidence, ...) are numbered by prefix in the rowid "
         "order of their table; every cell that holds one of this side's ids names that number", db=_db_atlas_ids),
    Rule("atlas_written_by", ("atlas.db",),
         "meta.schema_written_by names the build that created the schema (buildinfo.server_version(): version "
         "and commit): must be this side's checkout's own", db=_db_written_by),
    Rule("search_meta_built_at", ("index/search.db",),
         "meta.built_at is time.time() of the search-index build: accepted only inside a run of this side",
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
            k = next((j for j, (p, q) in enumerate(zip(x, y)) if p != q), min(len(x), len(y)))
            if k > 60:  # a long line: show it from shortly before the first differing character
                return i, "..." + _short(x[k - 40:]), "..." + _short(y[k - 40:])
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

# freelist_count and schema_version: the file's layout (a scratch table made and dropped leaves free pages and a
# new schema_version even when the content is the same). page_count is not compared: the baseline differs from
# itself in it (calibration 2026-09-30, fixtures: 57 steps, content equal), because the volatile values (random
# ids, clocks with 1-6 fraction digits) change the row lengths and so the page splits
_PRAGMAS = ("user_version", "application_id", "page_size", "encoding", "auto_vacuum", "journal_mode",
            "freelist_count", "schema_version")


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
        if any(r.name == "atlas_random_ids" for r in rules):
            # number the random ids (the `id` column of each table, rowid order) before any row refers to them
            for t in tables:
                if "id" not in [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]:
                    continue
                try:
                    number_ids([v for (v,) in con.execute(f'SELECT id FROM "{t}" ORDER BY rowid')], sctx)
                except sqlite3.OperationalError:  # WITHOUT ROWID
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
    return rel.startswith(EXCLUDED_PREFIXES) or rel.endswith(EXCLUDED_SUFFIXES) or rel in EXCLUDED_FILES


_DATED = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def artifact_names(rels: list[str], sctx: SideCtx, root: Path | None = None) -> dict[str, tuple[str, str]]:
    """``{rel: (artifact name, rule key)}`` for the files under ``.verinoda``: a dated backup folder
    (``index/<YYYY-MM-DD>/``) whose date is a local date on which a run of THIS side ran becomes ``<DATE>``
    (``<DATE+1>`` for a second one, by name order; rule ``backup_dir_date``); a folder named after another date
    keeps its name, so it is compared. The files of either take the rules of the files they copy. A leftover
    backup of the atomic replace (``.gfy-replace-bak-<random>.tmp``) becomes ``.gfy-replace-bak-<n>.tmp``,
    numbered per folder in the order of its sha256 (rule ``replace_bak_name``; its bytes are compared)."""
    dated = sorted({r.split("/")[1] for r in rels if r.count("/") >= 2 and r.startswith("index/")
                    and _DATED.match(r.split("/")[1])})
    ran = [d for d in dated if _report_date_of_run(d, sctx) is not None]
    tags = {d: "<DATE>" if i == 0 else f"<DATE+{i}>" for i, d in enumerate(ran)}
    out = {}
    for r in rels:
        parts = r.split("/")
        if len(parts) >= 3 and parts[0] == "index" and parts[1] in dated:
            out[r] = ("/".join(["index", tags.get(parts[1], parts[1])] + parts[2:]), "index/" + "/".join(parts[2:]))
        else:
            out[r] = (r, r)
    baks: dict = {}
    for r in rels:
        if _REPLACE_BAK.search(r):
            folder = out[r][0].rsplit("/", 1)[0] if "/" in out[r][0] else ""
            baks.setdefault(folder, []).append(r)
    for folder, group in baks.items():
        group.sort(key=lambda r: (sha256_file(root / r) if root is not None else "", r))
        for i, r in enumerate(group, 1):
            out[r] = ((folder + "/" if folder else "") + f".gfy-replace-bak-<{i}>.tmp", out[r][1])
        _hit(["replace_bak_name"])
    return out


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _mode(st) -> list:
    """A file's permission bits and (Windows) attributes: a run that makes a user's file read-only, hidden or
    a system file changes them."""
    return [st.st_mode, getattr(st, "st_file_attributes", 0)]


def tree_state(root: Path, skip_root=(), skip=None, meta: bool = False) -> dict[str, list]:
    """``{relative path: [size, mtime_ns, sha256]}`` of every file under ``root``. Folders named in
    ``skip_root`` are left out at the top level only (a nested ``sub/.verinoda/`` is kept); ``skip(rel)``
    (a relative path, a folder's with a trailing ``/``) leaves out anything it is true for. ``meta``: each
    file's row also carries its mode and attributes (:func:`_mode`), and every folder, an empty one included,
    is listed as ``{"sub/": ["dir", mode, attributes]}`` (no folder mtime: an edit inside moves it)."""
    out: dict[str, list] = {}
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        top = Path(dirpath) == root
        prefix = "" if top else Path(dirpath).relative_to(root).as_posix() + "/"
        dirnames[:] = sorted(d for d in dirnames if not (top and d in skip_root)
                             and not (skip and skip(prefix + d + "/")))
        if meta:
            for d in dirnames:
                out[prefix + d + "/"] = ["dir"] + _mode((Path(dirpath) / d).stat())
        for fn in sorted(filenames):
            if skip and skip(prefix + fn):
                continue
            p = Path(dirpath) / fn
            st = p.stat()
            out[prefix + fn] = [st.st_size, st.st_mtime_ns, sha256_file(p)] + (_mode(st) if meta else [])
    return dict(sorted(out.items()))


# what of .git is not compared: git's own stat index (git status refreshes it), reflogs, fetch/merge leftovers,
# objects/info/ and the packs' .idx files (git may rewrite both without changing an object). The object store
# itself IS compared: loose objects and pack files by name, size and sha256 (the corpus is packed once and
# copied, and commit_edit writes the same objects on both sides: fixed dates), and every object by name, type
# and size (``git_objects``), so a candidate that writes into the user's object store is seen.
GIT_UNCOMPARED_DIRS = ("logs/", "objects/info/")
GIT_UNCOMPARED_FILES = ("index", "index.lock", "ORIG_HEAD", "FETCH_HEAD")


def git_uncompared(rel: str) -> bool:
    """Is ``rel`` (under ``.git``) left out of ``git_files``?"""
    return (rel.startswith(GIT_UNCOMPARED_DIRS) or rel in GIT_UNCOMPARED_FILES
            or (rel.startswith("objects/pack/") and rel.endswith(".idx")))


def listing(root: Path) -> dict[str, object]:
    """``{relative path: [size, sha256]}`` of every file under ``root`` and ``{folder/: "dir"}`` of every folder
    (an empty one included); no mtimes: files written by two sides at different times compare equal."""
    out: dict[str, object] = {}
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        prefix = "" if Path(dirpath) == root else Path(dirpath).relative_to(root).as_posix() + "/"
        dirnames.sort()
        for d in dirnames:
            out[prefix + d + "/"] = "dir"
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            out[prefix + fn] = [p.stat().st_size, sha256_file(p)]
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


def retarget_call(text: str) -> tuple[str, str, str, str] | None:
    """In Python source with at least two top-level functions, a function or method (``caller``) that calls
    one of them (``old``) once and never names another (``new``): ``(new text, caller, old, new)`` with that one
    call pointed at ``new`` on the same line, the line kept the same length (padded with spaces before its line
    end when ``new`` is shorter; a longer ``new`` only when no other fits). The node set and the edge count stay
    as they were, only the edge ``caller -> old`` becomes ``caller -> new``: what a check of the counts alone
    misses. None when the source has no such call."""
    import ast
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    tops = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(set(tops)) < 2:
        return None
    funcs = sorted((n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
                   key=lambda n: (n.lineno, n.col_offset))
    best = None
    for fn in funcs:
        named: dict = {}
        for n in ast.walk(fn):
            if isinstance(n, ast.Name):
                named[n.id] = named.get(n.id, 0) + 1
        for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)):
            old = call.func.id
            if old not in tops or old == fn.name or named.get(old) != 1:
                continue
            for new in tops:
                if new in (fn.name, old) or new in named:
                    continue
                # not longer first, then source order, then the closest length, then the name
                key = (len(new) > len(old), call.lineno, call.col_offset, abs(len(new) - len(old)), new)
                if best is None or key < best[0]:
                    best = (key, fn.name, old, new, call.func)
    if best is None:
        return None
    _key, caller, old, new, node = best
    lines = re.split(r"(?<=\n)|(?<=\r)(?!\n)", text)  # the line ends ast counts, and only those
    i = node.lineno - 1
    raw = _enc(lines[i])  # col_offset counts UTF-8 bytes
    body = raw.rstrip(b"\r\n")
    ending = raw[len(body):]
    a, o, n = node.col_offset, old.encode(), new.encode()
    if body[a:a + len(o)] != o:
        return None
    lines[i] = (body[:a] + n + body[a + len(o):] + b" " * max(0, len(o) - len(n)) + ending).decode(
        "utf-8", "surrogateescape")
    return "".join(lines), caller, old, new


def plan_retarget_call(repo: Path):
    """A call pointed at another existing function on the same line (:func:`retarget_call`): the node set and
    the edge count stay, one edge changes its target (a candidate that keeps the old graph when the counts
    match keeps a stale edge)."""
    for rel in ("orders/service.py", "sample_calls.py") + tuple(_code_files(repo, ".py")):
        if not (_original(rel) and (repo / rel).is_file()):
            continue
        got = retarget_call(_read(repo / rel))
        if got is not None:
            return [Op("write", rel, _enc(got[0]))]
    return None


def plan_relocate(repo: Path):
    """Run in a second fixed folder (``<case>/q/<corpus>``, see ``RELOCATED``): a new Python file, so the update
    rebuilds the graph and every unchanged file comes from the AST cache the runs in the first folder wrote (an
    entry that kept an absolute id or path names the old folder). The next step runs in the first folder again:
    a second move."""
    return [Op("write", "eq_moved/moved.py", b"def eq_moved():\n    return 'written before a move'\n")]


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


_PY_ANY_DEF = re.compile(r"^[ \t]*def (\w+)", re.MULTILINE)


def _original(rel: str) -> bool:
    """A file of the corpus as built (the harness's own files live under ``eq_*`` folders)."""
    return not rel.split("/")[0].startswith("eq_")


def _one_letter_rename(text: str) -> str:
    """``text`` with the first function (at any indentation) renamed by its last letter: the same size."""
    m = _PY_ANY_DEF.search(text)
    end = m.end(1) - 1
    return text[:end] + ("y" if text[end] == "x" else "x") + text[end + 1:]


def _pick_original_def(repo: Path, prefer: tuple[str, ...]) -> str | None:
    for rel in prefer + tuple(_code_files(repo, ".py")):
        if _original(rel) and (repo / rel).is_file() and _PY_ANY_DEF.search(_read(repo / rel)):
            return rel
    return None


def plan_same_size_keep_mtime(repo: Path):
    """The stat-cache case: a function (of a file of the corpus as built, a method counts) renamed by one letter
    (same size) and the file's old mtime put back, so nothing but the bytes changed. The old mtime lies at least
    ``EDIT_GAP`` before every later recording, so the caches may trust it: what a stat cache then misses, it must
    miss on both sides."""
    rel = _pick_original_def(repo, ("orders/repository.py", "sample_calls.py"))
    if rel is None:
        return None
    return [Op("write", rel, _enc(_one_letter_rename(_read(repo / rel))), mtime_ns=(repo / rel).stat().st_mtime_ns)]


RACY_AHEAD = 6 * 3600  # seconds: the racy file's mtime lies this far ahead of the planning time
RACY_TARGETS = ("orders/api.py", "sample.py")


def plan_racy_prepare(repo: Path, now: float | None = None):
    """The racy-clean window, first half: a file's bytes are kept but its mtime is put ``RACY_AHEAD`` ahead
    (the same value on both sides), so every stat recording from now on is made within the racy margin of the
    file's mtime (snapshot.RACY_MARGIN_NS, freshness): the next update must hash it, and must not trust it."""
    rel = _pick_original_def(repo, RACY_TARGETS)
    if rel is None:
        return None
    t = (int(time.time() if now is None else now) + RACY_AHEAD) * 1_000_000_000
    return [Op("write", rel, (repo / rel).read_bytes(), mtime_ns=t)]


def plan_racy_same_size(repo: Path, now: float | None = None):
    """The racy-clean window, second half: the file ``racy_prepare`` moved ahead gets a same-size edit (a function
    renamed by one letter) and keeps that mtime: size and mtime are what the caches recorded, only the racy rule
    finds the change (a candidate that trusts the cache here keeps a stale graph)."""
    now = time.time() if now is None else now
    for rel in RACY_TARGETS + tuple(_code_files(repo, ".py")):
        p = repo / rel
        if _original(rel) and p.is_file() and p.stat().st_mtime > now + 60 and _PY_ANY_DEF.search(_read(p)):
            return [Op("write", rel, _enc(_one_letter_rename(_read(p))), mtime_ns=p.stat().st_mtime_ns)]
    return None


def plan_json(repo: Path):
    """A JSON config file changes (a key added after the first ``{``); a corpus without one gets a new one."""
    rel = _pick(repo, ("sample.json",), ".json", lambda p: _read(p).lstrip().startswith("{"))
    if rel is None:
        return [Op("write", "eq_config/package.json",
                   b'{\n  "name": "eq-config",\n  "version": "1.0.0",\n  "dependencies": {\n    "left-pad": "^1.3.0"\n'
                   b'  }\n}\n')]
    text = _read(repo / rel)
    i = text.index("{") + 1
    nl = _nl(text)
    return [Op("write", rel, _enc(text[:i] + f'{nl}  "eqAdded": "by the equality harness",' + text[i:]))]


_TS_FUNC = re.compile(r"^function (\w+)\(", re.MULTILINE)


def plan_ts(repo: Path):
    """A TypeScript function added that calls one of the file's; a corpus without TypeScript gets a new file."""
    rel = _pick(repo, ("sample.ts",), ".ts", lambda p: _TS_FUNC.search(_read(p)) is not None)
    if rel is None:
        return [Op("write", "eq_web/helpers.ts",
                   b"export function eqFormat(n: number): string {\n    return n.toFixed(2);\n}\n\n"
                   b"export function eqTotal(xs: number[]): string {\n    return eqFormat(xs.reduce((a, b) => a + b, 0));"
                   b"\n}\n")]
    text = _read(repo / rel)
    target = _TS_FUNC.search(text).group(1)
    nl = _nl(text)
    return [Op("write", rel, _enc(text.rstrip("\r\n") + f"{nl}{nl}function eqAdded(): unknown {{{nl}    return "
                                  f"{target}(\"eq\");{nl}}}{nl}"))]


def plan_java(repo: Path):
    """A Java class appended (with a method calling another)."""
    rel = _pick(repo, ("sample.java",), ".java")
    if rel is None:
        return None
    text = _read(repo / rel)
    nl = _nl(text)
    return [Op("write", rel, _enc(text.rstrip("\r\n") + f"{nl}{nl}class EqAdded {{{nl}    int eqOne() {{{nl}        "
                                  f"return eqTwo();{nl}    }}{nl}{nl}    int eqTwo() {{{nl}        return 2;{nl}    }}{nl}}}{nl}"))]


def plan_rust(repo: Path):
    """A Rust function appended (calling another)."""
    rel = _pick(repo, ("sample.rs",), ".rs")
    if rel is None:
        return None
    text = _read(repo / rel)
    nl = _nl(text)
    return [Op("write", rel, _enc(text.rstrip("\r\n") + f"{nl}{nl}pub fn eq_added() -> i32 {{{nl}    eq_helper(){nl}}}"
                                  f"{nl}{nl}fn eq_helper() -> i32 {{{nl}    2{nl}}}{nl}"))]


# a git-ignored file (so no snapshot hashes it) that a claim cites: `update` re-checks such citations itself
UNTRACKED_REL = "eq_local/notes.py"
UNTRACKED_TEXT = b"def local_note():\n    return 'kept outside git'\n"


def plan_untracked_cited(repo: Path):
    """The git-ignored file a claim cites changes (nothing the snapshot hashes does): the no-op path must
    still re-check the untracked citation and mark the claim stale."""
    if not (repo / UNTRACKED_REL).is_file():
        return None
    return [Op("write", UNTRACKED_REL, _enc(_read(repo / UNTRACKED_REL).replace("kept outside git",
                                                                                "changed outside git")))]


def plan_fast_edit(repo: Path):
    """A function added to a graph file, taken in by ``update --fast`` (the deferred path: search, lexicon and
    stale claims now, the graph by a background build the harness waits for)."""
    rel = _pick(repo, ("orders/service.py", "sample_calls.py"), ".py", lambda p: _original(
        p.relative_to(repo).as_posix()))
    if rel is None:
        return None
    text = _read(repo / rel)
    nl = _nl(text)
    return [Op("write", rel, _enc(text.rstrip("\r\n") + f"{nl}{nl}def eq_fast_added():{nl}    return 2{nl}"))]


def plan_human_edit(repo: Path):
    """The line the ``steady`` claim cites (no earlier edit touches it) gets a trailing comment: the human output
    of `update` then shows a stale claim and the decision check's counts."""
    src = claim_targets(repo).get("steady")
    if src is None:
        return None
    rel, line = src[1].rsplit(":", 1)
    text = _read(repo / rel)
    lines = text.splitlines(keepends=True)
    n = int(line.split("-")[0]) - 1
    if n >= len(lines):
        return None
    body = lines[n].rstrip("\r\n")
    lines[n] = body + ("  # eq: edited" if rel.endswith(".py") else "  // eq: edited") + lines[n][len(body):]
    return [Op("write", rel, _enc("".join(lines)))]


# -- the claims and the decision record every case starts with ------------------------------------------------------

def _first_line(text: str, m: re.Match) -> int:
    return text.count("\n", 0, m.start()) + 1


def claim_targets(repo: Path) -> dict[str, tuple[str, str]]:
    """``{role: (claim text, source)}`` of the claims added after the scan (in this order), the same on every
    side: ``body`` cites the ``return`` line that ``body_edit`` changes, ``defn`` the first definition of the
    ``add_function`` file, ``untracked`` the git-ignored file ``untracked_cited`` changes, ``steady`` the first
    line of a file only ``human_edit`` changes. The texts quote the lines (``contains:``)."""
    out = {}
    rel = _pick(repo, ("orders/service.py", "sample.py"), ".py", lambda p: _RETURN.search(_read(p)) is not None)
    if rel is not None:
        text = _read(repo / rel)
        m = _RETURN.search(text)
        out["body"] = (f"the function returns its result: contains: return {m.group(2)}",
                       f"{rel}:{_first_line(text, m)}")
    rel = _pick(repo, ("orders/pricing.py", "sample_calls.py", "sample.py"), ".py",
                lambda p: _PY_TOP.search(_read(p)) is not None)
    if rel is not None:
        text = _read(repo / rel)
        m = _PY_TOP.search(text)
        line = text[m.start():].splitlines()[0]
        out["defn"] = (f"{m.group(1)} is defined here: contains: {line}", f"{rel}:{_first_line(text, m)}")
    if (repo / UNTRACKED_REL).is_file():
        out["untracked"] = ("local_note is defined outside git: contains: def local_note():", f"{UNTRACKED_REL}:1-2")
    for rel in ("orders/__init__.py", "sample.go"):
        if (repo / rel).is_file():
            found = next(((i, ln.strip()) for i, ln in enumerate(_read(repo / rel).splitlines(), 1) if ln.strip()),
                         None)
            if found:
                out["steady"] = (f"the file starts so: contains: {found[1].split('  #')[0].split('  //')[0]}",
                                 f"{rel}:{found[0]}")
            break
    return out


def decision_args(repo: Path) -> list[str]:
    """``verinoda decide record`` arguments of the decision record every case starts with: a guard and a
    governed symbol (the first function of the add_function file), so every non-noop update checks it."""
    args = ["decide", "record", "--chosen", "keep the standard library", "--rationale",
            "fewer dependencies to audit", "--title", "No database driver", "--guard", "dependency absent=psycopg"]
    rel = _pick(repo, ("orders/pricing.py", "sample_calls.py", "sample.py"), ".py",
                lambda p: _PY_DEF.search(_read(p)) is not None)
    if rel is not None:
        args += ["--governs", f"{rel}::{_PY_DEF.search(_read(repo / rel)).group(1)}"]
    return args


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
         "retarget_call": plan_retarget_call, "relocate": plan_relocate,
         "comment_only": plan_comment_only, "add_duplicate_stem": plan_add_duplicate_stem,
         "edit_objc_pair": plan_objc_pair, "edit_go": plan_go, "edit_go_mod": plan_go_mod, "edit_json": plan_json,
         "edit_ts": plan_ts, "edit_java": plan_java, "edit_rust": plan_rust, "edit_doc": plan_doc,
         "add_data_file": plan_add_data_file, "same_size_keep_mtime": plan_same_size_keep_mtime,
         "racy_prepare": plan_racy_prepare, "racy_same_size": plan_racy_same_size,
         "untracked_cited": plan_untracked_cited, "fast_edit": plan_fast_edit, "human_edit": plan_human_edit,
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
                     + (f" (mtime {o.mtime_ns // 10**9})" if o.mtime_ns is not None else "") for o in ops) or "nothing"


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


# the folders every run gets in its user folder (Env.run_env), so their existence is no write of a run
USER_SUBDIRS = ("config", "tmp", "home", "appdata", "localappdata")


@dataclass
class Env:
    work: Path
    python: str
    # the entries of the work folder the harness itself makes (the case folders are added as they start):
    # anything else found there after a run is that run's write (Runner.sweep_outside)
    known: set = field(default_factory=lambda: {"gitconfig", "user"})

    def git_env(self) -> dict:
        cfg = self.work / "gitconfig"
        if not cfg.exists():
            cfg.write_text("[user]\n\tname = eq\n\temail = eq@example.invalid\n[core]\n\tautocrlf = false\n\tlongpaths = true\n"
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
        for sub in USER_SUBDIRS:
            (user / sub).mkdir(parents=True, exist_ok=True)
        # everything a run may write outside the repository lands in the side's own user folder (config, cache,
        # temporary files, the home folder and the per-user application data), which is compared (`user_files`)
        e.update(PYTHONPATH=str(tree), PYTHONHASHSEED=str(seed), PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                 VERINODA_NO_AUTO_INDEX="1", VERINODA_OCR="0", VERINODA_CONFIG_DIR=str(user / "config"),
                 VERINODA_CACHE_DIR=str(user / "cache"), TEMP=str(user / "tmp"), TMP=str(user / "tmp"),
                 TMPDIR=str(user / "tmp"), HOME=str(user / "home"), USERPROFILE=str(user / "home"),
                 APPDATA=str(user / "appdata"), LOCALAPPDATA=str(user / "localappdata"),
                 XDG_CACHE_HOME=str(user / "home" / ".cache"), XDG_CONFIG_HOME=str(user / "home" / ".config"),
                 XDG_DATA_HOME=str(user / "home" / ".local" / "share"),
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
    ``PYTHONPATH`` set to it; a wrong import location is a harness error. stdout and stderr are read as bytes
    (no newline translation) and decoded with ``surrogateescape``, so encoding them again gives the bytes."""
    r = subprocess.run([env.python, "-c", WRAPPER, str(tree), *args], cwd=tree, env=env.run_env(tree, seed, user),
                       capture_output=True, timeout=RUN_TIMEOUT, check=False, stdin=subprocess.DEVNULL)
    r.stdout = r.stdout.decode("utf-8", "surrogateescape")
    r.stderr = r.stderr.decode("utf-8", "surrogateescape")
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


# side M: one long-lived process of the candidate runs every update of the case (the way the MCP server's
# index_update calls workflow.update again and again), so a memo that outlives one build is exercised. Each
# command is a line of JSON on stdin ({"args", "out", "err"}); fd 1 and fd 2 are pointed at the two files for
# the call (os.dup2: the same sys.stdout and sys.stderr objects, the same encoding and newline translation as a
# CLI process writing to a pipe), `verinoda.cli.main(args)` runs, and the exit status is answered on a copy of
# the original stdout. Exit status as `sys.exit(main())` in a process: an exception is printed and gives 1.
_DRIVER = r"""
import json, os, sys, traceback
proto = os.fdopen(os.dup(1), "w", encoding="utf-8")
from verinoda import cli
for line in sys.stdin:
    cmd = json.loads(line)
    sys.stdout.flush()
    sys.stderr.flush()
    s1, s2 = os.dup(1), os.dup(2)
    f1, f2 = open(cmd["out"], "wb"), open(cmd["err"], "wb")
    os.dup2(f1.fileno(), 1)
    os.dup2(f2.fileno(), 2)
    try:
        sys.argv = ["verinoda"] + cmd["args"]
        try:
            rc = cli.main(cmd["args"])
            rc = 0 if rc is None else rc
        except SystemExit as exc:
            code = exc.code
            if code is None:
                rc = 0
            elif isinstance(code, int):
                rc = code
            else:
                print(code, file=sys.stderr)
                rc = 1
        except BaseException:
            traceback.print_exc()
            rc = 1
        # the CLI leaves its atlas.db Store open (cli._store: the process exits after one command); the MCP
        # server closes it after every call (`with self._store()`), so the driver does the same. Any other
        # handle a call leaves open in the repository is kept: the harness cannot move the folder then.
        import gc
        from verinoda import store as _store_mod
        gc.collect()
        for o in gc.get_objects():
            if isinstance(o, _store_mod.Store):
                try:
                    o.close()
                except Exception:
                    pass
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(s1, 1)
        os.dup2(s2, 2)
        os.close(s1)
        os.close(s2)
        f1.close()
        f2.close()
    proto.write(json.dumps({"rc": rc}) + "\n")
    proto.flush()
"""


class InProc:
    """The long-lived process of side M (:data:`_DRIVER`), started through the wrapper (the import location
    is checked as for every other run)."""

    def __init__(self, env: Env, tree: Path, seed: int, user: Path, tmp: Path):
        tmp.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp
        self.err_path = tmp / "driver.err"
        self._err = open(self.err_path, "wb")
        self.p = subprocess.Popen([env.python, "-c", WRAPPER, str(tree), "code", _DRIVER], cwd=tree,
                                  env=env.run_env(tree, seed, user), stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=self._err)

    def run(self, *args: str) -> subprocess.CompletedProcess:
        out, err = self.tmp / "call.out", self.tmp / "call.err"
        try:
            self.p.stdin.write((json.dumps({"args": list(args), "out": str(out), "err": str(err)}) + "\n").encode())
            self.p.stdin.flush()
            line = self.p.stdout.readline()
        except OSError:
            line = b""
        if not line:
            self._err.flush()
            tail = self.err_path.read_bytes().decode("utf-8", "replace")[-800:]
            if "eq-wrapper:" in tail:
                raise HarnessError(tail.strip() + " (an installed copy shadows PYTHONPATH?)")
            raise HarnessError(f"side M: the in-process driver ended (exit {self.p.poll()}): {tail.strip()}")
        rc = json.loads(line)["rc"]
        return subprocess.CompletedProcess(list(args), rc, out.read_bytes().decode("utf-8", "surrogateescape"),
                                           err.read_bytes().decode("utf-8", "surrogateescape"))

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.p.stdin.close()
        try:
            self.p.wait(timeout=120)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
        self._err.close()


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


def add_untracked(dest: Path) -> None:
    """A git-ignored file (``UNTRACKED_REL``) for a claim to cite: its folder is added to ``.gitignore``."""
    gi = dest / ".gitignore"
    old = gi.read_bytes() if gi.exists() else b""
    folder = UNTRACKED_REL.split("/")[0] + "/"
    gi.write_bytes(old + (b"" if not old or old.endswith(b"\n") else b"\n") + folder.encode() + b"\n")
    (dest / UNTRACKED_REL).parent.mkdir(parents=True, exist_ok=True)
    (dest / UNTRACKED_REL).write_bytes(UNTRACKED_TEXT)


def build_corpus(env: Env, name: str, baseline: Path, candidate: Path, dest: Path) -> Path:
    """The corpus ``name`` at ``dest`` (a git work tree with one commit, objects packed), with a git-ignored
    file (:func:`add_untracked`)."""
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
    add_untracked(dest)
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
    raise HarnessError(f"cannot move {src} to {dst}: {last} (when this follows a run of side M: its long-lived "
                       "process keeps a file in the repository open)")


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


PREFERRED_ORDER = ("scan_exit", "scan_result", "scan_stderr", "update_exit", "update_result", "update_stderr",
                   "update_human_exit", "update_human_result", "update_human_stderr", "graph_kept",
                   "index/graph.json", "index/receiver_calls.json", "index/.graphify_labels.json",
                   "index/.graphify_labels.json.sig", "index/rebuild_record.json", "index/GRAPH_REPORT.md",
                   "index/manifest.json", "index/build_stats.json", "index/search.db", "atlas.db", "loaded_graph",
                   "loaded_graph_candidate", "loader_writes", "loader_writes_candidate", "corpus", "git_files",
                   "git_objects", "git_state", "user_files", "outside_files")


def collect_files(repo: Path, sctx: SideCtx, tmp: Path) -> dict:
    """Every file under ``repo/.verinoda`` but the excluded ones, masked (SQLite files first: their snapshot
    ids number the update result's)."""
    atlas = repo / ".verinoda"
    rels = sorted(r for r in tree_paths(atlas) if not excluded(r))
    if any(_REPLACE_BAK.search(r) for r in rels):
        # the fallback of the atomic replace (project_index/paths.py) leaves this backup behind only when a
        # rename was refused at that moment (a file held open): two baseline scans differ in it, so it is not
        # compared (rule replace_bak_leftover)
        rels = [r for r in rels if not _REPLACE_BAK.search(r)]
        _hit(["replace_bak_leftover"])
    names = artifact_names(rels, sctx, atlas)
    if any(n != r and not _REPLACE_BAK.search(r) for r, (n, _k) in names.items()):
        _hit(["backup_dir_date"])
    items: dict = {}
    # every folder under .verinoda (an empty one a run left behind included), under its artifact name
    dirs = set()
    for d in tree_dirs(atlas):
        probe = artifact_names([d + "x"], sctx)[d + "x"][0]
        dirs.add(probe[:-1])
    items["verinoda_dirs"] = ("json", sorted(dirs))
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


def tree_dirs(root: Path) -> list[str]:
    """Every folder under ``root`` (``sub/``, ``sub/inner/``), an empty one included."""
    out = []
    if root.is_dir():
        for dirpath, dirs, _files in os.walk(root):
            for d in dirs:
                out.append((Path(dirpath) / d).relative_to(root).as_posix() + "/")
    return sorted(out)


def run_items(name: str, run: subprocess.CompletedProcess, sctx: SideCtx, json_out: bool = True) -> dict:
    """``<name>_exit``, ``<name>_result`` (stdout, masked under the rules of ``JSON_OUT`` or ``TEXT_OUT``) and
    ``<name>_stderr`` (masked under the rules of ``ERR_OUT``), compared as bytes."""
    out, fired = mask_text(JSON_OUT if json_out else TEXT_OUT, run.stdout, sctx)
    err, fired_err = mask_text(ERR_OUT, run.stderr, sctx)
    _hit(fired + fired_err)
    return {f"{name}_exit": ("json", run.returncode), f"{name}_result": ("bytes", _enc(out)),
            f"{name}_stderr": ("bytes", _enc(err))}


def corpus_items(env: Env, repo: Path) -> dict:
    """``corpus`` (every file and folder outside the root's ``.verinoda`` and ``.git``: size, mtime, sha256,
    mode and attributes; see :func:`tree_state` with ``meta``), ``git_files`` (every file under ``.git`` but
    :func:`git_uncompared`: size and sha256; git's own writes move mtimes) and ``git_state`` (HEAD, ``git
    status`` and ``git ls-files -s -v``: each tracked file's tag - ``S`` skip-worktree, a lower-case letter
    assume-unchanged -, mode, object and stage, so a run that hides a file from git through the index flags is
    seen although ``.git/index`` itself is not compared)."""
    head = git(env, repo, "rev-parse", "HEAD").strip()
    status = git(env, repo, "status", "--porcelain", "--untracked-files=all").splitlines()
    flags = git(env, repo, "ls-files", "-s", "-v").splitlines()
    gitf = {rel: [size, sha] for rel, (size, _mt, sha) in tree_state(repo / ".git", skip=git_uncompared).items()}
    objects = sorted(git(env, repo, "cat-file", "--batch-all-objects", "--batch-check").splitlines())
    return {"corpus": ("json", tree_state(repo, skip_root=(".git", ".verinoda"), meta=True)),
            "git_files": ("json", gitf),
            "git_objects": ("json", objects),
            "git_state": ("json", {"head": head, "status": status, "index_entries": flags})}


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


def wait_pid(pid: int, timeout: float) -> bool:
    """Wait until process ``pid`` (not a child of this one) has exited; False on timeout."""
    deadline = time.monotonic() + timeout
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        synchronize = 0x00100000
        h = k32.OpenProcess(synchronize, False, int(pid))
        if not h:
            return True  # gone already
        try:
            return k32.WaitForSingleObject(h, int(timeout * 1000)) == 0
        finally:
            k32.CloseHandle(h)
    while time.monotonic() < deadline:
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        time.sleep(0.1)
    return False


# the sides an update step may run: B (the baseline on its own state), C (the candidate on its own state), C0
# (the candidate on the baseline's state: the upgrade path), B0 (the baseline on a copy of B whose recorded
# stamps are marked outdated: C0's reference when the candidate's stamps differ, see Runner.make_derived) and M
# (the candidate on a copy of C's state, every update inside ONE long-lived process: InProc). REFERENCE: the side
# each is compared with.
# the entries of a case folder: fixed folders (r, q), stores (s), scratch (t), user folders (u), leftovers (x),
# the corpus being built (src)
CASE_ENTRIES = frozenset({"r", "q", "s", "t", "u", "x", "src"})
SIDE_NOTES = {"C": "", "C0": " [C0: the candidate updating the baseline's state]",
              "M": " [M: the candidate, every update in one process, compared with BM: the baseline so]"}
# the stamps a build records, where, and at which JSON path (the extraction stamp forces a full rebuild when it
# is not the running code's: workflow.update; the others make their own cache start over)
STAMP_FILES = {"extraction": ("index/build_stats.json", ("extraction",)),
               "code": ("index/rebuild_record.json", ("stamp",)),
               "python_facts": ("index/python_facts.json", ("stamp",)),
               "python_cross": ("index/python_cross.json", ("stamp",)),
               "empty_json": ("index/empty_json.json", ("stamp",))}


class Runner:
    def __init__(self, env: Env, case: Case, baseline: Path, candidate: Path, probes: dict, skip: set,
                 keep_going: bool, no_rules: bool, inproc: bool = True):
        self.env, self.case, self.skip, self.keep_going, self.no_rules = env, case, skip, keep_going, no_rules
        self.tag = f"{case.corpus} seed={case.seed} {case.cache}"
        self.cdir = env.work / f"{case.corpus[:3]}{case.seed}{case.cache[0]}"
        self.fixed_main = self.fixed = self.cdir / "r" / case.corpus
        self.fixed_moved = self.cdir / "q" / case.corpus  # the same length: RELOCATED steps
        self.store = {s: self.cdir / "s" / s / case.corpus for s in ("B", "B2", "C", "C0", "B0", "M", "BM")}
        self.trees = {"B": baseline, "B2": baseline, "C": candidate, "C0": candidate, "B0": baseline, "M": candidate,
                      "BM": baseline}
        self.baseline, self.candidate = baseline, candidate
        self.probes = probes
        self.ctx = {s: SideCtx(s, stamps=dict(probes[self.trees[s]])) for s in ("B", "B2", "C")}
        self.t_end = 0.0
        self.use_inproc = inproc
        self.inprocs: dict = {}  # side -> InProc (M and BM)
        self.sides = ["B", "C", "C0"]
        self.reference = {"C": "B", "C0": "B"}

    def close(self) -> None:
        for p in self.inprocs.values():
            p.close()
        self.inprocs = {}

    def user(self, name: str) -> Path:
        """The user-level folder of a side's runs (VERINODA_CONFIG_DIR, VERINODA_CACHE_DIR, TEMP): compared."""
        return self.cdir / "u" / name

    def outside(self, name: str) -> Path:
        """Where what a run of ``name`` left beside the fixed folder (in ``<case>/r/``) is moved: compared."""
        return self.cdir / "x" / name

    def sweep_outside(self, name: str) -> None:
        """Move everything in the fixed folder's parent (the corpus is not there now) to :meth:`outside`, so
        each side's leftovers are its own and the next run starts from an empty parent."""
        moves = []
        parent = self.fixed.parent
        if parent.is_dir():
            for p in sorted(parent.iterdir()):
                if p == self.fixed:
                    raise HarnessError(f"{self.fixed} is still in use")
                moves.append((p, p.name))
        # anything new in the case folder or the work folder (two and three levels above the repository)
        # the tags name folders on disk: no "<" or ">" (not allowed in a Windows file name)
        for root, tag, known in ((self.cdir, "@case", CASE_ENTRIES), (self.env.work, "@work", self.env.known)):
            if root.is_dir():
                moves += [(p, f"{tag}/{p.name}") for p in sorted(root.iterdir()) if p.name not in known]
        for p, rel in moves:
            dst = self.outside(name) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.is_dir():
                rmtree(dst)
            elif dst.exists():
                dst.unlink()
            _move(p, dst)

    def run_at_fixed(self, side: str, label: str, *args: str, after=None) -> subprocess.CompletedProcess:
        """Run the side's checkout on its copy (moved into the fixed folder); ``after(run)`` runs before the
        copy moves back. The run's wall-clock window (``after`` included) is recorded under ``label``: a clock
        value this side writes is accepted only inside the window of one of its runs."""
        with at_fixed(self.store[side], self.fixed):
            start = time.time()
            try:
                if side in self.inprocs:
                    r = self.inprocs[side].run(*args)
                else:
                    r = run_tree(self.env, self.trees[side], self.case.seed, self.user(side), "cli", *args)
                if after is not None:
                    after(r)
            finally:
                self.ctx[side].windows.append((label, start, time.time()))
        self.sweep_outside(side)
        self.note_graph(side, label)
        return r

    def note_graph(self, side: str, label: str) -> None:
        st = graph_state(self.store[side])
        if st is not None:
            self.ctx[side].graph_mtimes.setdefault(st[0], label)

    def wait_background(self, run: subprocess.CompletedProcess) -> None:
        """``update --fast``: wait for the background build the run started (its pid is in the result)."""
        try:
            bg = json.loads(run.stdout).get("background") or {}
        except (ValueError, AttributeError):
            return
        if bg.get("started") and isinstance(bg.get("pid"), int) and not wait_pid(bg["pid"], RUN_TIMEOUT):
            raise HarnessError(f"{self.tag}: the background build (pid {bg['pid']}) did not end in {RUN_TIMEOUT}s")

    def load(self, side: str, tree: Path, who: str):
        """``verinoda.index.load`` of a throwaway copy of ``side``'s state (in the fixed folder) by ``tree``;
        returns ``(dump bytes or None, error text, changes the load made in the copy)``."""
        if self.fixed.exists():
            raise HarnessError(f"{self.fixed} is in use")
        shutil.copytree(self.store[side], self.fixed, copy_function=shutil.copy2)
        user = self.user(f"load-{who}")
        for sub in USER_SUBDIRS:  # the folders every run gets (Env.run_env), so they are no write of a load
            (user / sub).mkdir(parents=True, exist_ok=True)
        try:
            before = tree_state(self.fixed / ".verinoda")
            ubefore = {f"<user>/{k}": v for k, v in listing(user).items()}
            out = self.cdir / "t" / "loaded.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                out.unlink()
            r = run_tree(self.env, tree, self.case.seed, user, "code", _LOADER, str(self.fixed), str(out))
            writes = state_changes(before, tree_state(self.fixed / ".verinoda"))
            # what the load wrote in its user folder (config, cache, TEMP), by content
            writes += state_changes(ubefore, {f"<user>/{k}": v for k, v in listing(user).items()})
        finally:
            rmtree(self.fixed)
        self.sweep_outside(f"load-{who}")
        left = listing(self.outside(f"load-{who}"))
        if left:  # beside the fixed folder
            writes += [f"added <beside the repository>/{k}" for k in left]
            rmtree(self.outside(f"load-{who}"))
        if r.returncode:
            return None, f"<load failed (exit {r.returncode}): {r.stderr.strip()[-600:]}>", writes
        return out.read_bytes(), "", writes

    def observe(self, sides, runs: dict) -> dict:
        """Collect each side (before anything loads its state), then load throwaway copies; the real state is
        hashed before and after and must not change. ``runs``: side -> ``[(item name, run, json output?)]``."""
        guard = {s: tree_state(self.store[s] / ".verinoda") for s in sides}
        out = {}
        for s in sides:
            side = Side()
            side.items.update(collect_files(self.store[s], self.ctx[s], self.cdir / "t" / s))
            for name, run, json_out in runs.get(s, ()):
                side.items.update(run_items(name, run, self.ctx[s], json_out))
            side.items.update(corpus_items(self.env, self.store[s]))
            # what the side's runs wrote outside the repository: its user folder (config, cache, TEMP) and
            # beside the fixed folder
            side.items["user_files"] = ("json", listing(self.user(s)))
            side.items["outside_files"] = ("json", listing(self.outside(s)))
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

    def decision_record(self) -> dict[str, bytes]:
        """The decision record every side starts with, made ONCE by the baseline (``verinoda decide record``) on
        a throwaway copy of B: ``{path under .verinoda: bytes}``. It is an input of `update` (the decision
        check), the same bytes on every side."""
        if self.fixed.exists():
            raise HarnessError(f"{self.fixed} is in use")
        shutil.copytree(self.store["B"], self.fixed, copy_function=shutil.copy2)
        try:
            before = tree_state(self.fixed / ".verinoda")
            r = run_tree(self.env, self.baseline, self.case.seed, self.user("decide"), "cli",
                         *decision_args(self.fixed), "--repo", str(self.fixed), "--json")
            if r.returncode:
                raise HarnessError(f"{self.tag}: baseline `verinoda decide record` exited {r.returncode}: "
                                   f"{(r.stderr or r.stdout).strip()[-600:]}")
            after = tree_state(self.fixed / ".verinoda")
            got = {rel: (self.fixed / ".verinoda" / rel).read_bytes() for rel in after
                   if rel.startswith("decisions/") and before.get(rel) != after[rel]}
            if not got:
                raise HarnessError(f"{self.tag}: `verinoda decide record` wrote no record under .verinoda/decisions")
            return got
        finally:
            rmtree(self.fixed)
            self.sweep_outside("decide")

    def scan(self, res: CaseResult) -> bool:
        """Set up B, B2 (baseline) and C (candidate): init, scan, the decision record, the claims (``verinoda
        claim add`` by each side's own checkout); compare B with B2 (determinism) and B with C. Then C0 is made:
        a copy of B's set-up state (repository, user folder, leftovers), whose updates the CANDIDATE runs."""
        runs: dict = {}
        for s in ("B", "B2", "C"):
            ri = self.run_at_fixed(s, "init", "init", str(self.fixed), "--json")
            if ri.returncode == 0:
                rs = self.run_at_fixed(s, "scan", "scan", str(self.fixed), "--json")
            else:
                rs = ri
            if s in BASELINE_SIDES and (ri.returncode or rs.returncode):
                raise HarnessError(f"{self.tag}: baseline `verinoda {'init' if ri.returncode else 'scan'}` exited "
                                   f"{rs.returncode}: {(rs.stderr or rs.stdout).strip()[-800:]}")
            runs[s] = [("scan", rs, True)]
        records = self.decision_record()
        targets = claim_targets(self.store["B"])
        for s in ("B", "B2", "C"):
            for rel, data in records.items():
                (self.store[s] / ".verinoda" / rel).parent.mkdir(parents=True, exist_ok=True)
                (self.store[s] / ".verinoda" / rel).write_bytes(data)
            for role, (text, src) in targets.items():
                r = self.run_at_fixed(s, f"claim {role}", "claim", "add", text, "--repo", str(self.fixed), "--source",
                                      src, "--json")
                if s in BASELINE_SIDES and r.returncode:
                    raise HarnessError(f"{self.tag}: baseline `verinoda claim add` ({role}, {src}) exited "
                                       f"{r.returncode}: {(r.stderr or r.stdout).strip()[-600:]}")
                runs[s].append((f"claim_{role}", r, True))
        self.t_end = time.time()
        sides = self.observe(("B", "B2", "C"), runs)
        det = compare(sides["B"], sides["B2"])
        if det and not self.no_rules:
            raise HarnessError(f"{self.tag}: the two BASELINE scans differ, so the baseline is not deterministic "
                               "here: " + "; ".join(f"{a}: {w}" for a, w in det[:5]))
        res.diffs += [f"{self.tag} step=scan (B vs B2, --no-rules): {a}: {w}" for a, w in det]
        rmtree(self.store["B2"])
        self.make_derived()
        diffs = compare(sides["B"], sides["C"], self.skip)
        res.diffs += [f"{self.tag} step=scan: {a}: {w}" for a, w in diffs]
        print(f"  {self.tag}: scan (+ {len(targets)} claims, {len(records)} decision record): "
              f"{'equal' if not diffs else f'DIFFERENT ({len(diffs)} artifact(s))'}", flush=True)
        return not diffs

    def copy_side(self, src: str, dst: str) -> None:
        """``dst`` starts as ``src`` is now: the repository with .verinoda and .git, the user folder and the
        leftovers beside the fixed folder."""
        shutil.copytree(self.store[src], self.store[dst], copy_function=shutil.copy2)
        for folder in (self.user, self.outside):
            if folder(src).is_dir():
                shutil.copytree(folder(src), folder(dst), copy_function=shutil.copy2)

    def make_derived(self) -> None:
        """The sides made from the set-up state.

        C0, the upgrade path: a copy of B as the BASELINE left it (index/cache/, the stat index, the manifest,
        the sidecars, atlas.db, .git; the user folder and the leftovers beside the fixed folder), from which the
        CANDIDATE runs every update. Its clock windows, graph mtimes and stamps start as B's (what it holds was
        written by B's runs); the candidate's stamps are accepted besides the baseline's.

        B0, only when a recorded stamp of the candidate differs from the baseline's (it changed the extractors or
        the code a cache is stamped with): the candidate rightly treats B's state as an older build (the
        extraction stamp forces a full rebuild, a cache stamp starts that cache over), which B, the baseline on
        its own state, never does. B0 is a copy of B whose recorded stamps of those kinds (``STAMP_FILES``) are
        replaced by ``eq-outdated-<kind>`` (same bytes otherwise, mtime kept), updated by the BASELINE: the
        baseline upgrading from an older build. C0 is then compared with B0 instead of B; the marker is accepted
        on B0 as a stamp of that kind, like the baseline's own on C0.

        M and BM: a copy of C's set-up state updated by the CANDIDATE, and a copy of B's updated by the BASELINE,
        each inside one long-lived process of its own (:class:`InProc`); M is compared with BM (``--no-inproc``
        leaves both out). Not with B: the baseline itself writes differently in a long-lived process (its stat
        index is loaded once per process, tied to the first root it saw, and written only at exit:
        project_index/cache.py), so only the baseline in the same situation is a fair reference."""
        self.copy_side("B", "C0")
        b = self.ctx["B"]
        self.ctx["C0"] = SideCtx("C0", stamps=dict(self.probes[self.candidate]), also=dict(b.stamps),
                                 graph_mtimes=dict(b.graph_mtimes), windows=list(b.windows))
        pb, pc = self.probes[self.baseline], self.probes[self.candidate]
        outdated = [k for k in STAMP_FILES if pb.get(k) != pc.get(k)]
        if outdated:
            self.copy_side("B", "B0")
            fake = {}
            for kind in outdated:
                rel, path = STAMP_FILES[kind]
                p = self.store["B0"] / ".verinoda" / rel
                if not p.is_file():
                    continue
                st = p.stat()
                text = p.read_bytes().decode("utf-8", "surrogateescape")
                edits = []
                for _path, a, e in json_value_spans(text, path):
                    v = json.loads(text[a:e])
                    if isinstance(v, str) and v in (pb.get(kind), f"{pb.get(kind)}-vendored"):
                        edits.append((a, e, json.dumps(v.replace(pb[kind], f"eq-outdated-{kind}"))))
                if edits:
                    p.write_bytes(replace_spans(text, edits).encode("utf-8", "surrogateescape"))
                    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
                    fake[kind] = f"eq-outdated-{kind}"
            self.ctx["B0"] = SideCtx("B0", stamps=dict(pb), also=fake, graph_mtimes=dict(b.graph_mtimes),
                                     windows=list(b.windows))
            self.sides.append("B0")
            self.reference["C0"] = "B0"
            print(f"  {self.tag}: side B0 (the baseline upgrading its own state): outdated stamps "
                  f"{', '.join(outdated)}; replaced in {', '.join(sorted(fake)) or 'none'}", flush=True)
        if self.use_inproc:
            for side, src in (("BM", "B"), ("M", "C")):
                self.copy_side(src, side)
                c = self.ctx[src]
                self.ctx[side] = SideCtx(side, stamps=dict(c.stamps), graph_mtimes=dict(c.graph_mtimes),
                                         windows=list(c.windows))
                self.inprocs[side] = InProc(self.env, self.trees[side], self.case.seed, self.user(side),
                                            self.cdir / "t" / f"inproc-{side}")
                self.sides.append(side)
            self.reference["M"] = "BM"

    def step(self, name: str, res: CaseResult) -> bool | None:
        ops = PLANS[name](self.store["B"])
        if ops is None:
            res.skipped.append(name)
            print(f"  {self.tag}: {name}: skipped (no file of its kind in this corpus)", flush=True)
            return None
        mode = STEP_MODES.get(name, "json")
        when = math.ceil(self.t_end) + 1
        sides_now = list(self.sides)
        for s in sides_now:
            apply_plan(self.store[s], ops, when * 1_000_000_000, lambda repo, *a: git(self.env, repo, *a))
            if self.case.cache == "cold":
                rmtree(self.store[s] / ".verinoda" / "index" / "cache", ignore_errors=True)
        while time.time() < when + EDIT_GAP:
            time.sleep(min(0.25, max(0.0, when + EDIT_GAP - time.time())))
        self.fixed = self.fixed_moved if name in RELOCATED else self.fixed_main
        try:
            return self._step(name, ops, mode, sides_now, res)
        finally:
            self.fixed = self.fixed_main

    def _step(self, name: str, ops, mode: str, sides_now: list, res: CaseResult) -> bool:
        args = ["update", str(self.fixed)] + {"json": ["--json"], "fast": ["--fast", "--json"], "human": []}[mode]
        item = "update_human" if mode == "human" else "update"
        pre, runs, kept = {}, {}, {}
        t0 = time.monotonic()
        for s in sides_now:
            pre[s] = graph_state(self.store[s])
            runs[s] = self.run_at_fixed(s, name, *args, after=self.wait_background if mode == "fast" else None)
            post = graph_state(self.store[s])
            kept[s] = pre[s] is not None and post == pre[s]
        self.t_end = time.time()
        for s in sides_now:
            if s in BASELINE_SIDES and runs[s].returncode:
                raise HarnessError(f"{self.tag}: {name}: the baseline's update ({s}) exited {runs[s].returncode}: "
                                   f"{(runs[s].stderr or runs[s].stdout).strip()[-800:]}")
        t1 = time.monotonic()
        sides = self.observe(sides_now, {s: [(item, runs[s], mode != "human")] for s in sides_now})
        t2 = time.monotonic()
        for s in sides_now:
            sides[s].items["graph_kept"] = ("json", kept[s])
        diffs = [(s, a, w) for s in sides_now if s in self.reference
                 for a, w in compare(sides[self.reference[s]], sides[s], self.skip)]
        res.compared += 1
        res.diffs += [f"{self.tag} step={name}{SIDE_NOTES[s]}: {a}: {w}" for s, a, w in diffs]
        what = []
        for s in sides_now:
            if mode == "human":
                first = (runs[s].stdout.splitlines() or ["<no output>"])[0]
                what.append(f"{s}: {first[:60]!r} kept={'yes' if kept[s] else 'no'}")
                continue
            try:
                r = json.loads(runs[s].stdout)
                what.append(f"{s}: index_mode={r.get('index_mode', '?')} changed={r.get('changed_count', '?')} "
                            f"stale={len(r.get('stale') or [])} kept={'yes' if kept[s] else 'no'}")
            except ValueError:
                what.append(f"{s}: no JSON result (exit {runs[s].returncode})")
        status = "equal" if not diffs else "DIFFERENT (" + ", ".join(
            f"{s}: {sum(1 for x in diffs if x[0] == s)} artifact(s)" for s in sides_now
            if any(x[0] == s for x in diffs)) + ")"
        how = "" if mode == "json" else f" ({mode})"
        print(f"  {self.tag}: {name}{how}: {status} [{plan_text(ops)}] [{'; '.join(what)}] "
              f"(updates {t1 - t0:.1f}s, collect and loads {t2 - t1:.1f}s)", flush=True)
        return not diffs


def _checkout_skip(rel: str) -> bool:
    """What of a checkout :func:`checkout_state` leaves out: Python's byte-code caches and git's own folder."""
    parts = rel.rstrip("/").split("/")
    return "__pycache__" in parts or ".pytest_cache" in parts or rel.endswith(".pyc") or parts[0] == ".git"


def checkout_state(tree: Path) -> dict:
    """Every file of a checkout (:func:`_checkout_skip` aside): a run that writes into the code it runs from
    (a memo file beside a module) is seen."""
    return tree_state(tree, skip=_checkout_skip)


def run_case(env: Env, case: Case, baseline: Path, candidate: Path, edits: list[str], probes: dict,
             skip: set, keep_going: bool, no_rules: bool = False, inproc: bool = True) -> CaseResult:
    rn = Runner(env, case, baseline, candidate, probes, skip, keep_going, no_rules, inproc)
    env.known.add(rn.cdir.name)
    res = CaseResult(cdir=rn.cdir)
    if rn.cdir.exists():
        rmtree(rn.cdir)
    rn.cdir.mkdir(parents=True)
    trees = {"baseline": baseline} if candidate == baseline else {"baseline": baseline, "candidate": candidate}
    before = {k: checkout_state(t) for k, t in trees.items()}
    try:
        src = build_corpus(env, case.corpus, baseline, candidate, rn.cdir / "src" / case.corpus)
        for s in ("B", "B2", "C"):  # C0, B0 and M are made after the set-up (Runner.make_derived)
            shutil.copytree(src, rn.store[s], copy_function=shutil.copy2)
        rmtree(rn.cdir / "src")
        if rn.scan(res) or keep_going:
            for name in edits:
                ok = rn.step(name, res)
                if ok is False and not keep_going:
                    break
    finally:
        rn.close()
    for k, t in trees.items():
        changes = state_changes(before[k], checkout_state(t))
        if changes:
            res.diffs.append(f"{rn.tag}: checkout_writes: the {k} checkout {t} changed during the case: "
                             + "; ".join(changes[:10]))
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
    ap.add_argument("--no-inproc", action="store_true",
                    help="leave out side M (every update of the candidate inside one long-lived process)")
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
        env.known |= {p.name for p in work.iterdir()}  # what was there before is never swept
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
                                   a.keep_going, a.no_rules, not a.no_inproc)
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
