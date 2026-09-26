"""Facade over verinoda.project_index (the Graphify-derived extractor).

The rest of Verinoda talks to the knowledge graph only through this module,
so the vendored package can be re-synced from upstream without touching the
analysis layers. All building is AST-only (no LLM, no network).

Build-time work is done once and not repeated at query time (docs/DESIGN.md D21):

* **Symbol spans** come from one pass per file: Python ``ast`` end lines,
  tree-sitter ``end_point`` for the other languages, and for Markdown the
  section of a heading, which ends before the next heading of the same or a
  higher level. Only when none of these knows a symbol does the old
  next-symbol rule (capped at :data:`HEURISTIC_SPAN_CAP` lines) apply, and
  :meth:`Graph.span_basis` says so.
* **Receiver-call edges** (``param.method()`` on annotated parameters) are
  computed by :func:`build` and stored next to the graph in
  ``receiver_calls.json``, keyed by the graph file's identity; :func:`load`
  only applies them. Per-file facts are cached under the file's sha256, so an
  incremental build re-parses only changed files.
* The vendored rebuild's path-identity helpers are memoised by a monkeypatch
  applied from :func:`build` (see :func:`install_path_identity_memo`), and so,
  for the length of a build, are ``Path.resolve`` (:class:`_resolve_once`) and
  the re-anchoring of cached source paths (:class:`_absolutize_once`); the
  graph it writes is unchanged.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import networkx as nx

from verinoda.paths import configure_index_env, ensure_atlas, graph_path, index_dir, receiver_calls_path

configure_index_env()

CODE_RELATIONS = {"calls", "imports", "imports_from", "uses", "inherits", "method", "implements",
                  "extends", "references", "contains"}
FLOW_RELATIONS = {"calls"}
RECEIVER_ORIGIN = "verinoda.receiver"
JAVA_CALL_ORIGIN = "verinoda.java_calls"
RECEIVER_SIDECAR_VERSION = 3   # 3: a receiver's class is the one the calling file can see (_visible_class)
HEURISTIC_SPAN_CAP = 80        # the next-symbol fallback never spans more lines than this
PROSE_SUFFIXES = (".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc")
MARKDOWN_SUFFIXES = (".md", ".markdown", ".mdx")
_CACHE_MAX = 4096              # per-file caches are cleared when they grow past this


def build(repo: Path, *, force: bool = False, changed: list[Path] | None = None,
          quiet: bool = True, prune_missing: bool = False, fresh_caches: bool = False) -> dict:
    """Run the Graphify-derived AST pipeline; return graph stats.

    ``fresh_caches`` (``scan --force``): the per-file Python caches (python_facts.json, python_cross.json)
    are not read; every file is walked again and the caches are written anew.

    After a successful rebuild graph.json is read once and written at most once: its ids
    are made portable (:mod:`verinoda.portable_ids`) and, with ``prune_missing``, the nodes
    of files that do not exist are dropped as :func:`prune_missing_files` would drop them
    (listed in ``pruned_files``). The counts come from that data. Then the receiver-call
    sidecar is refreshed (per-file facts are reused for files whose content did not change).

    A build with ``prune_missing`` (``scan``/``update``; not ``force``, not ``changed``) leaves
    graph.json as it is when the last build rewrote it, the pipeline produced that build's graph
    again and nothing that build left has changed (:class:`_keep_unchanged_graph`; ``graph_kept``).
    """
    from verinoda.project_index.watch import _rebuild_code
    from verinoda.python_cross import python_cross_cache
    from verinoda.python_facts import python_facts_cache

    install_path_identity_memo()
    repo = Path(repo).resolve()
    ensure_atlas(repo)  # the upstream pipeline expects its output directory to exist
    index_dir(repo).mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    keep = _keep_unchanged_graph(repo, active=prune_missing and changed is None and not force)
    empties = _known_empty_json(repo, replay=not force)
    # The upstream pipeline also logs to stderr (e.g. hints to run `graphify
    # label`, which is not a Verinoda command); keep both streams in the log.
    with (redirect_stdout(buf) if quiet else _null()), (redirect_stderr(buf) if quiet else _null()),             _without_report_questions(), _without_upstream_html(), _resolve_once(), _absolutize_once(),             python_facts_cache(index_dir(repo), fresh=fresh_caches), python_cross_cache(index_dir(repo), fresh=fresh_caches), empties, keep:
        ok = _rebuild_code(repo, changed_paths=changed, force=force, block_on_lock=True)
    if keep.failed:  # the full path would have failed making its report: so does this build
        ok = False
    gp = graph_path(repo)
    if not ok and not gp.exists():
        raise RuntimeError("index build failed:\n" + buf.getvalue()[-2000:])
    portable = pruned = data = None
    if ok:  # before the sidecar: it is keyed by the graph file as it ends up
        try:
            portable, pruned, data = _post_process(gp, repo, prune=prune_missing)
        except (OSError, ValueError) as exc:  # the ids stay as the pipeline wrote them
            portable = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    if data is None:
        data = json.loads(gp.read_text(encoding="utf-8"))
    elif keep.kept:  # the kept graph is pruned already; the full path would have pruned these
        pruned = sorted(set(pruned or ()) | set(keep.record.get("pruned") or ()))
    out = {"ok": bool(ok), "graph_path": str(gp), "nodes": len(data.get("nodes", [])),
           "edges": len(data.get("links", data.get("edges", []))), "log": buf.getvalue()[-2000:]}
    rewrote = bool(pruned) or bool((portable or {}).get("changed"))
    keep.finish(data if ok and "error" not in (portable or {}) else None, pruned, rewrote)
    del data  # the sidecar refresh loads the graph again
    if portable is not None:
        out["portable_ids"] = portable
    if pruned is not None:
        out["pruned_files"] = pruned
    if keep.kept:
        out["graph_kept"] = True
    if empties.replayed or empties.recorded:
        out["empty_json"] = {"replayed": empties.replayed, "recorded": empties.recorded}
    if ok:
        try:
            out["receiver_calls"] = refresh_receiver_sidecar(repo)
        except (OSError, ValueError) as exc:  # the sidecar is derived data; load() recomputes it
            out["receiver_calls"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    return out


class _resolve_once:
    """Each path resolved once per build.

    The pipeline calls ``Path.resolve`` on the same paths again and again - about 30,000 calls in
    an update of Verinoda's own 1,163 files, each a file-system call on Windows. During a build the
    tree does not move, so the first answer for a path (with the working directory, for a relative
    one) is kept until the build ends; what raises (``strict=True`` on a missing file) is not kept.
    """

    def __enter__(self):
        import pathlib

        self.cls, self.real, cache = pathlib.Path, pathlib.Path.resolve, {}
        real = self.real

        def resolve(p, strict=False):
            s = str(p)
            key = (type(p), s, strict, None if p.is_absolute() else os.getcwd())
            hit = cache.get(key)
            if hit is None:
                hit = cache[key] = real(p, strict=strict)
            return hit

        self.cls.resolve = resolve
        return self

    def __exit__(self, *a):
        self.cls.resolve = self.real
        return False


class _absolutize_once:
    """Each cached ``source_file`` re-anchored once per build.

    Every AST cache hit makes its items' ``source_file`` / ``definition_file`` absolute again
    (``cache._absolutize_source_files_in``: ``str(root / Path(value))`` per item), about 150,000
    pathlib joins in an update of Verinoda's own repository, although the items of one file share
    a handful of values. The answer depends only on the resolved root and the string, so it is
    kept per pair until the build ends. A value that is not a ``str`` takes the original per-item
    code (and raises where it raises).
    """

    def __enter__(self):
        try:
            from verinoda.project_index import cache as upstream_cache
        except Exception:  # noqa: BLE001 - nothing to wrap
            self.mod = None
            return self
        self.mod, self.real = upstream_cache, upstream_cache._absolutize_source_files_in
        memo: dict = {}
        unknown = object()

        def absolutize(payload: dict, root: Path) -> None:
            try:
                root_resolved = Path(root).resolve()
            except OSError:
                return
            known = memo.setdefault(str(root_resolved), {})  # the spelling, not Path equality
            for bucket in ("nodes", "edges", "hyperedges", "raw_calls"):
                for item in payload.get(bucket, []):
                    if not isinstance(item, dict):
                        continue
                    for key in ("source_file", "definition_file"):
                        source = item.get(key)
                        if not source:
                            continue
                        if type(source) is str:
                            new = known.get(source, unknown)
                            if new is unknown:
                                sp = Path(source)
                                new = None  # None: leave the value as it is
                                if not sp.is_absolute():
                                    try:
                                        new = str(root_resolved / sp)
                                    except (TypeError, OSError):
                                        pass
                                known[source] = new
                            if new is not None:
                                item[key] = new
                            continue
                        sp = Path(source)  # the original code, item by item
                        if sp.is_absolute():
                            continue
                        try:
                            item[key] = str(root_resolved / sp)
                        except (TypeError, OSError):
                            continue

        upstream_cache._absolutize_source_files_in = absolutize
        return self

    def __exit__(self, *a):
        if self.mod is not None:
            self.mod._absolutize_source_files_in = self.real
        return False


EMPTY_JSON_FILE = "empty_json.json"
EMPTY_JSON_VERSION = 2  # 2: kept only when the bytes after extraction are the bytes before
JSON_READ_LIMIT = 1_048_576    # extract_json reads no more; a larger file is an error, never kept
# What is left to extract after the replay goes to the upstream process pool unless it is small:
# fewer files than this and at most this many bytes of source are extracted in this process.
# Measured on Windows (6 cores, busy): a pool costs 0.6-0.8 s before it extracts anything, and
# JavaScript/TypeScript in this process about 2 ms per KB (Python 1.5).
IN_PROCESS_BELOW = 64
IN_PROCESS_BYTES = 256 * 1024
IN_PROCESS_OK = os.name == "nt"  # only measured where a pool spawns each worker from nothing


def _json_digest(path) -> str | None:
    """blake2b of a file's bytes as extract_json reads them; None when the file cannot be read or
    is larger than extract_json reads (its result is then an error, which is never kept)."""
    try:
        with open(path, "rb") as f:
            data = f.read(JSON_READ_LIMIT + 1)
    except OSError:
        return None
    if len(data) > JSON_READ_LIMIT:
        return None
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def _small_batch(work, unread=frozenset()) -> bool:
    """Is ``work`` (``(index, path)`` pairs) cheaper to extract here than to start a pool for?
    Files in ``unread`` (JSON the extractor turns down unparsed: too large) count no bytes."""
    if not IN_PROCESS_OK or len(work) >= IN_PROCESS_BELOW:
        return False
    size = 0
    for _, path in work:
        if str(path) in unread:
            continue
        try:
            size += os.stat(path).st_size
        except OSError:
            continue
        if size > IN_PROCESS_BYTES:
            return False
    return True


class _known_empty_json:
    """Data-shaped ``.json`` files are extracted to nothing on every build; remember that.

    The upstream JSON extractor skips data-shaped JSON on purpose (by the file's name and its
    top-level keys: ``{"nodes": [], "edges": [], "skipped": ...}``), and the upstream AST cache
    never stores an empty result (#1666), so such a file is parsed again on every build: 112
    benchmark and result files in Verinoda's own repository. With the JavaScript files, which
    upstream never caches, they also push every update past the 20-file threshold for a process
    pool, whose start-up on Windows costs more than the work.

    For the build, ``extract._extract_parallel`` and ``_extract_sequential`` are wrapped. A file
    the JSON extractor would read, whose bytes (blake2b) and path gave such a skipped result
    under the same extractor code (``empty_json.json``, dropped when that code or the grammar
    changes), is filled with a copy of that result without parsing; everything else goes to the
    real functions, and their skipped JSON results are remembered, under the bytes read before
    extraction and only when the file holds the same bytes afterwards (a file rewritten while
    it was extracted is left out). A ``force`` build replays nothing. When what is left is small
    (:func:`_small_batch`: fewer than :data:`IN_PROCESS_BELOW` files, at most
    :data:`IN_PROCESS_BYTES` bytes, on Windows), the parallel wrapper hands it back and the caller
    extracts it in this process: the upstream contract of ``_extract_parallel`` returning False,
    as it does for a one-worker pool. A larger batch goes to the pool, as it would without this.
    """

    def __init__(self, repo: Path, *, replay: bool = True):
        self.path = index_dir(Path(repo).resolve()) / EMPTY_JSON_FILE
        self.replay = replay
        self.X = None
        self.replayed = self.recorded = 0

    @staticmethod
    def _stamp(X, json_config) -> str:
        from importlib import metadata

        from verinoda.project_index.extractors import base

        h = hashlib.blake2b(digest_size=16)
        for mod in (json_config, base, X):
            h.update(Path(mod.__file__).read_bytes())
        for dist in ("tree-sitter", "tree-sitter-json"):
            try:
                h.update(f"{dist}={metadata.version(dist)}\0".encode())
            except metadata.PackageNotFoundError:
                h.update(f"{dist}=-\0".encode())
        return h.hexdigest()

    def __enter__(self):
        try:
            from verinoda.project_index import extract as X
            from verinoda.project_index.extractors import json_config

            stamp = self._stamp(X, json_config)
        except Exception:  # noqa: BLE001 - nothing to wrap
            return self
        self.X, self.stamp, extract_json = X, stamp, json_config.extract_json
        self.real_par, self.real_seq = X._extract_parallel, X._extract_sequential
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            ok = isinstance(data, dict) and data.get("version") == EMPTY_JSON_VERSION
            known = data.get("files", {}) if ok and data.get("stamp") == stamp else {}
        except (OSError, ValueError):
            known = {}
        self.known, self.kept, self.digests, self.unread = known, {}, {}, set()

        def prefill(work, per_file) -> None:
            for i, path in work:
                if per_file[i] is not None or X._get_extractor(Path(path)) is not extract_json:
                    continue
                key = str(path)
                digest = _json_digest(path)
                if digest is None:  # too large or unreadable: an error, no parse
                    self.unread.add(key)
                    continue
                self.digests[key] = digest  # the bytes as extraction starts
                hit = self.known.get(key) if self.replay else None
                if isinstance(hit, dict) and hit.get("h") == digest and isinstance(hit.get("r"), dict):
                    per_file[i] = json.loads(json.dumps(hit["r"]))
                    self.kept[key] = hit
                    self.replayed += 1

        def record(work, per_file) -> None:
            for i, path in work:
                key, r = str(path), per_file[i]
                if (key not in self.digests or not isinstance(r, dict) or not r.get("skipped")
                        or r.get("nodes") or r.get("edges") or "error" in r):
                    continue
                if _json_digest(path) != self.digests[key]:
                    continue  # rewritten while it was extracted: which bytes gave r is not known
                try:
                    copy = json.loads(json.dumps(r))
                except (TypeError, ValueError):
                    continue
                if copy == r:  # replayed as it came
                    self.kept[key] = {"h": self.digests[key], "r": copy}
                    self.recorded += 1

        def parallel(uncached_work, per_file, root, max_workers, total_files, cache_location=None):
            prefill(uncached_work, per_file)
            rest = [(i, p) for i, p in uncached_work if per_file[i] is None]
            if not rest:
                return True  # every file replayed
            if _small_batch(rest, self.unread):
                return False  # the caller extracts what is left in this process
            done = self.real_par(rest, per_file, root, max_workers, total_files, cache_location)
            record(rest, per_file)
            return done

        def sequential(uncached_work, per_file, root, total_files, cache_location=None):
            prefill(uncached_work, per_file)
            rest = [(i, p) for i, p in uncached_work if per_file[i] is None]
            self.real_seq(rest, per_file, root, total_files, cache_location)
            record(rest, per_file)

        X._extract_parallel, X._extract_sequential = parallel, sequential
        return self

    def __exit__(self, *a):
        if self.X is None:
            return False
        self.X._extract_parallel, self.X._extract_sequential = self.real_par, self.real_seq
        if self.digests and self.kept != self.known:  # a build that extracted: keep its files only
            try:
                write_json_atomic(self.path, {"version": EMPTY_JSON_VERSION, "stamp": self.stamp,
                                              "files": self.kept})
            except OSError:
                pass
        return False


class _without_upstream_html:
    """No upstream Graphify ``graph.html`` from a build (an old one is removed by the pipeline itself).

    That page loaded vis-network from a CDN when opened; ``verinoda ui`` and ``verinoda ui --export``
    show the project's graph without anything from outside. On the Python standard library as a
    project it cost about a second and 3 MB per build. The upstream switch is used:
    ``GRAPHIFY_VIZ_NODE_LIMIT=0`` for the build, unless the variable is set (a positive number keeps
    the page).
    """

    VAR = "GRAPHIFY_VIZ_NODE_LIMIT"

    def __enter__(self):
        self.ours = self.VAR not in os.environ
        if self.ours:
            os.environ[self.VAR] = "0"
        return self

    def __exit__(self, *a):
        if self.ours:
            os.environ.pop(self.VAR, None)
        return False


class _without_report_questions:
    """Skip the "suggested questions" of the upstream GRAPH_REPORT.md during a build.

    Verinoda never reads the report; on a 2,000-file repository the suggestions took
    8.5 of 42 s of a full rebuild. The graph and every other output are unchanged.
    """

    def __enter__(self):
        try:
            from verinoda.project_index import analyze as upstream_analyze
        except Exception:  # noqa: BLE001 - nothing to skip
            self.mod = None
            return self
        self.mod, self.real = upstream_analyze, upstream_analyze.suggest_questions
        upstream_analyze.suggest_questions = lambda *a, **k: []
        return self

    def __exit__(self, *a):
        if self.mod is not None:
            self.mod.suggest_questions = self.real
        return False


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# -- vendored rebuild: the "topology unchanged" fast path ------------------------------------------

REBUILD_RECORD = "rebuild_record.json"
REBUILD_RECORD_VERSION = 1
_HERE = Path(__file__).resolve().parent
_STAMP: list[str] = []


def _code_files() -> tuple:
    """``(path, size, mtime_ns)`` of each file the code stamp covers: every file of project_index,
    this module and portable_ids (paths relative to the package, sorted)."""
    found = []
    stack = [_HERE / "project_index"]
    while stack:
        with os.scandir(stack.pop()) as entries:
            for e in entries:
                if e.is_dir(follow_symlinks=False):
                    stack.append(Path(e.path))
                elif e.name.endswith(".py"):
                    st = e.stat()
                    rel = Path(e.path).relative_to(_HERE).as_posix()
                    found.append((rel, st.st_size, st.st_mtime_ns))
    for name in ("index.py", "portable_ids.py"):
        st = os.stat(_HERE / name)
        found.append((name, st.st_size, st.st_mtime_ns))
    return tuple(sorted(found))


def _loaded_code_files():
    try:
        return _code_files()
    except OSError:
        return None


_LOADED_CODE = _loaded_code_files()  # as this process loaded them


def _code_stamp() -> str | None:
    """Hash of the code a rebuild runs after extraction (every file of project_index, this module,
    portable_ids), the Python version and the graph libraries' versions.

    None when one of those files changed on disk since this module was imported (a process that
    keeps running, such as the MCP server, while Verinoda is edited or upgraded): the code on disk
    may not be the code that runs, so this process neither trusts nor writes a record.
    """
    if _LOADED_CODE is None or _loaded_code_files() != _LOADED_CODE:
        return None
    if _STAMP:
        return _STAMP[0]
    from importlib import metadata
    import sys

    h = hashlib.sha256(sys.version.encode())
    try:
        for rel, _, _ in _LOADED_CODE:
            h.update(rel.encode() + b"\0" + (_HERE / rel).read_bytes() + b"\0")
    except OSError:
        return None
    for dist in ("networkx", "graspologic-native", "graspologic"):
        try:
            h.update(f"{dist}={metadata.version(dist)}\0".encode())
        except metadata.PackageNotFoundError:
            h.update(f"{dist}=-\0".encode())
    if _loaded_code_files() != _LOADED_CODE:
        return None  # changed while it was read
    _STAMP.append(h.hexdigest())
    return _STAMP[0]


def _file_state(p: Path):
    try:
        st = p.stat()
    except OSError:
        return None
    return st.st_ino, st.st_size, st.st_mtime_ns


def _sha256_bytes(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


class _keep_unchanged_graph:
    """Let the vendored "topology unchanged" fast path fire when the rebuild is the last one again.

    After extraction the vendored rebuild (``watch._rebuild_code``) compares the new graph's
    topology with graph.json; when they are equal it keeps graph.json and skips clustering, the
    report and every rewrite. Verinoda rewrites graph.json after each build (portable ids, nodes
    of missing files pruned), so on a repository whose code names a file that does not exist the
    two never compare equal, and every update re-clusters and rewrites a graph that has not
    changed (about a quarter of an update of Verinoda's own repository).

    Only such a rewrite needs this. When graph.json is the pipeline's own output (nothing to make
    portable, nothing pruned) the vendored comparison works as it is and decides alone, leaving
    the report and backups untouched when it keeps the graph; no record is written then.

    A build that took the full path, wrote its outputs and then rewrote graph.json leaves a record
    (``rebuild_record.json``): a hash of the graph the pipeline built (every node and edge with
    its attributes, in order, and each node's neighbours), the commit, the communities it wrote
    and fingerprints of graph.json, the labels and their signatures as they were left. The record
    is only written when the next full rebuild of the same graph would number the communities
    the same way again (``remap_communities_to_previous`` run against the graph as written).

    On the next build, ``_topology_from_graph`` (which the vendored code calls only for that
    comparison) is wrapped. When the new graph hashes the same, the commit and the code are the
    same (:func:`_code_stamp`: no record is used or written by a process whose code changed on
    disk after it was loaded) and the files the record fingerprints are untouched, the full path
    is known to produce the same clustering, labels and graph.json bytes; the topology is handed
    over as Verinoda wrote it (edge direction as ``to_json`` writes it, ids made portable, nodes
    of missing files pruned), so a file that appeared or vanished since makes the comparison fail
    and the full path runs. When the vendored code then takes its fast path, what the full path
    would still have changed is done here, with the vendored functions: GRAPH_REPORT.md (it
    carries the date and the corpus's file and word counts) and the dated backup of a labelled
    graph. Anything else returns the topology untouched: the full path runs as before.
    """

    def __init__(self, repo: Path, *, active: bool):
        self.repo, self.active = Path(repo).resolve(), active
        self.out = index_dir(self.repo)
        self.key = self.commit = self.record = None
        self.kept = self.failed = False
        self.pending = None      # (G, record) while the vendored comparison runs
        self.clustered = None    # what cluster() returned in this build
        self.written = None      # (communities, labels) handed to to_json
        self.detected = None     # what detect() returned
        self.before = self.after = None
        self._undo: list = []

    def _patch(self, mod, name, fn) -> None:
        self._undo.append((mod, name, getattr(mod, name)))
        setattr(mod, name, fn)

    def __enter__(self):
        if not self.active:
            return self
        try:
            from verinoda.project_index import cluster as cl
            from verinoda.project_index import detect as dt
            from verinoda.project_index import export as ex
            from verinoda.project_index import watch
        except Exception:  # noqa: BLE001 - no fast path; the build runs as before
            self.active = False
            return self
        self.before = _file_state(graph_path(self.repo))
        try:
            rec = json.loads((self.out / REBUILD_RECORD).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rec = None
        real_topology, real_cluster = watch._topology_from_graph, cl.cluster
        real_to_json, real_detect, real_save = ex.to_json, dt.detect, dt.save_manifest

        def topology(G):
            raw = real_topology(G)
            self.pending = None
            try:
                self.key = _graph_key(G, raw)
                self.commit = watch._git_head(cwd=self.repo)
            except Exception:  # noqa: BLE001 - no key: the full path runs and nothing is recorded
                self.key = None
                return raw
            if not (isinstance(rec, dict) and rec.get("rewrote") and self._unchanged(rec)):
                return raw  # graph.json as the pipeline wrote it: its own comparison decides
            try:
                written = _as_written(raw, self.repo)
            except Exception:  # noqa: BLE001
                return raw
            self.pending = (G, rec)
            return written

        def cluster(G, *a, **kw):
            self.pending = None  # the full path runs
            self.clustered = res = real_cluster(G, *a, **kw)
            return res

        def to_json(G, communities, output_path, **kw):
            self.written = (communities, kw.get("community_labels"))
            return real_to_json(G, communities, output_path, **kw)

        def detect(*a, **kw):
            self.detected = res = real_detect(*a, **kw)
            return res

        def save_manifest(*a, **kw):
            if self.pending is not None:  # the vendored fast path: nothing was clustered
                G, rec = self.pending
                self.pending = None
                try:
                    self._as_the_full_path(G, rec)
                except Exception:  # noqa: BLE001 - the full path would have failed here too
                    self.failed = True
                    return None
                self.kept, self.record = True, rec
            return real_save(*a, **kw)

        self._patch(watch, "_topology_from_graph", topology)
        self._patch(cl, "cluster", cluster)
        self._patch(ex, "to_json", to_json)
        self._patch(dt, "detect", detect)
        self._patch(dt, "save_manifest", save_manifest)
        return self

    def __exit__(self, *a):
        while self._undo:
            mod, name, fn = self._undo.pop()
            setattr(mod, name, fn)
        if self.active:
            self.after = _file_state(graph_path(self.repo))
        return False

    def _unchanged(self, rec: dict) -> bool:
        """Is this the build the record describes, with every file it fingerprints untouched?"""
        from verinoda.project_index.exporters.html import _HTML_STALE_MARKER, _viz_node_limit

        out = self.out
        return (rec.get("version") == REBUILD_RECORD_VERSION and rec.get("key") == self.key
                and rec.get("commit") == self.commit and rec.get("root") == str(self.repo)
                and self.detected is not None and rec.get("stamp") is not None
                and rec.get("stamp") == _code_stamp()
                and _viz_node_limit() <= 0 and not (out / "graph.html").exists()
                and not (out / _HTML_STALE_MARKER).exists() and not any(out.glob("*-callflow.html"))
                and _sha256_bytes(out / ".graphify_labels.json") == rec.get("labels")
                and _sha256_bytes(out / ".graphify_labels.json.sig") == rec.get("sig")
                and same_graph(graph_path(self.repo), rec.get("graph")))

    def _as_the_full_path(self, G, rec: dict) -> None:
        """What the vendored full path would still write when the graph and communities are the
        last build's: its report (from the same functions and inputs), the dated backup of a
        labelled graph, and ``.graphify_root``."""
        from verinoda.project_index import analyze, report, watch
        from verinoda.project_index import cluster as cl
        from verinoda.project_index.export import backup_if_protected
        from verinoda.project_index.extract import _get_extractor

        out = self.out
        communities = {int(cid): members for cid, members in rec["communities"]}
        raw = json.loads((out / ".graphify_labels.json").read_text(encoding="utf-8"))
        labels = {int(k): v for k, v in raw.items()}  # what the full path makes of it (see finish)
        # the corpus as _rebuild_code counts it: code files plus the documents it extracts
        code_files = [str(Path(f)) for f in self.detected["files"]["code"]]
        code_files += [str(Path(f)) for f in self.detected["files"].get("document", [])
                       if _get_extractor(Path(f)) is not None]
        detection = {"files": {"code": code_files, "document": [], "paper": [], "image": []},
                     "total_files": len(code_files),
                     "total_words": self.detected.get("total_words", 0),
                     "unclassified": self.detected.get("unclassified", [])}
        text = report.generate(
            G, communities, cl.score_all(G, communities), labels, analyze.god_nodes(G),
            analyze.surprising_connections(G, communities), detection, {"input": 0, "output": 0},
            watch._report_root_label(self.repo),
            suggested_questions=analyze.suggest_questions(G, communities, labels),
            built_at_commit=self.commit,
            learning=report.load_learning_for_report(out / "graph.json"))
        # graph.json is Verinoda's rewrite, never the candidate the full path compares with it:
        # the full path always writes, after its backup
        backup_if_protected(out)
        report_path = out / "GRAPH_REPORT.md"
        old = report_path.read_text(encoding="utf-8") if report_path.exists() else None
        if old != text:
            report_path.write_text(text, encoding="utf-8")
        root_file = out / ".graphify_root"
        if _sha256_bytes(root_file) != hashlib.sha256(str(self.repo).encode("utf-8")).hexdigest():
            root_file.write_text(str(self.repo), encoding="utf-8")

    def finish(self, data: dict | None, pruned: list[str] | None, rewrote: bool) -> None:
        """After the post-processing: record this build when the next full rebuild of the same
        graph is known to write the same outputs; otherwise remove any record."""
        p = self.out / REBUILD_RECORD
        if self.kept:
            return  # graph.json and the files next to it are still the ones recorded
        rec = None
        if self.active and data is not None:
            try:
                rec = self._new_record(data, pruned, rewrote)
            except Exception:  # noqa: BLE001 - no record: the next build takes the full path
                rec = None
        try:
            if rec is None:
                p.unlink(missing_ok=True)
            else:
                write_json_atomic(p, rec)
        except OSError:
            pass

    def _new_record(self, data: dict, pruned: list[str] | None, rewrote: bool) -> dict | None:
        from verinoda.project_index import watch
        from verinoda.project_index.cluster import community_member_sigs
        from verinoda.project_index.cluster import remap_communities_to_previous as remap

        if (not rewrote or self.key is None or self.clustered is None or self.written is None
                or self.before == self.after):  # nothing rewritten, or the full path wrote nothing
            return None
        communities, labels = self.written
        if not isinstance(labels, dict) or set(labels) != set(communities):
            return None
        # The next full rebuild of this graph remaps its clustering to the graph as written now;
        # only when that gives these communities again does it write the same outputs.
        previous = watch._node_community_map(data)
        again = remap(self.clustered, previous) if previous else self.clustered
        if list(again.items()) != list(communities.items()):
            return None
        # and the labels it would read back are the ones written (so it keeps them all)
        lf, sf = self.out / ".graphify_labels.json", self.out / ".graphify_labels.json.sig"
        if (json.loads(lf.read_text(encoding="utf-8")) != {str(k): v for k, v in labels.items()}
                or json.loads(sf.read_text(encoding="utf-8"))
                != {str(k): v for k, v in community_member_sigs(communities).items()}):
            return None
        stamp = _code_stamp()
        if stamp is None:  # the code on disk is not the code that ran
            return None
        return {"version": REBUILD_RECORD_VERSION, "stamp": stamp, "key": self.key,
                "commit": self.commit, "root": str(self.repo),
                "graph": graph_identity(graph_path(self.repo)),
                "labels": _sha256_bytes(lf), "sig": _sha256_bytes(sf), "rewrote": rewrote,
                "pruned": sorted(pruned or ()),
                "communities": [[cid, m] for cid, m in communities.items()]}


def _graph_key(G, topology: dict) -> str:
    """Hash of the graph the pipeline built: its node-link data as it comes (order and attribute
    order kept) and each node's neighbours in the order the graph holds them."""
    h = hashlib.sha256(json.dumps(topology, ensure_ascii=False, default=str).encode("utf-8"))
    adj = [[n, list(nbrs)] for n, nbrs in G.adj.items()]
    h.update(json.dumps(adj, ensure_ascii=False, default=str).encode("utf-8"))
    if G.is_directed():
        h.update(json.dumps([[n, list(p)] for n, p in G.pred.items()], ensure_ascii=False,
                            default=str).encode("utf-8"))
    return h.hexdigest()


def _as_written(topology: dict, repo: Path) -> dict:
    """``topology`` (node-link data of the pipeline's graph) as it ends up in graph.json: edge
    direction and hyperedge order as ``to_json`` writes them, ids made portable, nodes of missing
    files pruned (:func:`_post_process`), and the ``_origin`` the vendored reconcile stamps on the
    graph it reads before comparing (or an unstamped node never compares equal)."""
    import copy

    from verinoda.portable_ids import strip_root_from_ids
    from verinoda.project_index.build import _is_ast_tier

    def order(item) -> str:  # to_json's sort key
        return json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    data = dict(topology)
    key = "links" if "links" in data else "edges"
    data["nodes"] = [dict(n) for n in topology.get("nodes", [])]
    edges = []
    for e in topology.get(key, []):
        e = dict(e)
        src, tgt = e.pop("_src", None), e.pop("_tgt", None)
        if src is not None and tgt is not None:
            e["source"], e["target"] = src, tgt
        edges.append(e)
    data[key] = edges
    hyper = sorted(copy.deepcopy(topology.get("hyperedges") or []), key=order)
    if isinstance(data.get("graph"), dict) and "hyperedges" in data["graph"]:
        data["graph"] = {**data["graph"], "hyperedges": copy.deepcopy(hyper)}
    data["hyperedges"] = hyper
    strip_root_from_ids(data["nodes"], data[key], repo, data["hyperedges"])
    gone = set(_missing_in_nodes(repo, data["nodes"]))
    if gone:
        _drop_files(data, gone)
    for item in (*data["nodes"], *data[key]):
        if isinstance(item, dict):
            item.setdefault("_origin", "ast" if _is_ast_tier(item) else "semantic")
    return data


# -- vendored rebuild: path-identity memo ------------------------------------------------------

def install_path_identity_memo() -> bool:
    """Memoise ``watch._StoredSourcePaths.identity``/``in_watch_root``/``rebase_preserved``.

    The vendored incremental rebuild recomputes a pathlib identity for every
    node and edge (about 60% of a one-file update on graphify_core). All three
    methods are pure functions of the ``source_file`` string and of fields set
    once in ``__init__``, so a per-instance dict memo returns exactly what the
    original would. Applied as a monkeypatch (the vendored file stays
    read-only); idempotent. Returns True when the patch is in place.
    """
    try:
        from verinoda.project_index import watch
    except Exception:  # noqa: BLE001 - a missing/renamed upstream class just means no memo
        return False
    cls = getattr(watch, "_StoredSourcePaths", None)
    if cls is None:
        return False
    if getattr(cls, "_verinoda_memo", False):
        return True
    orig_identity, orig_in_root, orig_rebase = cls.identity, cls.in_watch_root, cls.rebase_preserved

    def _memo(self, name: str) -> dict:
        memo = self.__dict__.get("_verinoda_memo_" + name)
        if memo is None:
            memo = self.__dict__["_verinoda_memo_" + name] = {}
        return memo

    def identity(self, source_file):
        memo = _memo(self, "identity")
        try:
            return memo[source_file]
        except KeyError:
            v = memo[source_file] = orig_identity(self, source_file)
            return v
        except TypeError:  # unhashable source_file: no memo
            return orig_identity(self, source_file)

    def in_watch_root(self, source_file):
        memo = _memo(self, "in_root")
        try:
            return memo[source_file]
        except KeyError:
            v = memo[source_file] = orig_in_root(self, source_file)
            return v
        except TypeError:
            return orig_in_root(self, source_file)

    def rebase_preserved(self, item):
        sf = item.get("source_file")
        memo = _memo(self, "rebase")
        try:
            new = memo[sf]
        except KeyError:
            probe = {"source_file": sf}
            orig_rebase(self, probe)
            new = memo[sf] = probe["source_file"]
        except TypeError:
            orig_rebase(self, item)
            return
        if new is not sf and new != sf:
            item["source_file"] = new

    identity.__doc__ = orig_identity.__doc__
    cls.identity, cls.in_watch_root, cls.rebase_preserved = identity, in_watch_root, rebase_preserved
    cls._verinoda_memo = True
    cls._verinoda_originals = (orig_identity, orig_in_root, orig_rebase)
    return True


def uninstall_path_identity_memo() -> None:
    """Restore the vendored methods (tests compare builds with and without the memo)."""
    try:
        from verinoda.project_index import watch
    except Exception:  # noqa: BLE001
        return
    cls = getattr(watch, "_StoredSourcePaths", None)
    if cls is not None and getattr(cls, "_verinoda_memo", False):
        cls.identity, cls.in_watch_root, cls.rebase_preserved = cls._verinoda_originals
        cls._verinoda_memo = False


# -- graph --------------------------------------------------------------------------------------

@dataclass
class Graph:
    """Directed multigraph with true edge direction restored from _src/_tgt."""

    G: nx.MultiDiGraph
    path: Path
    root: Path
    _spans: dict[str, tuple[int, int]] = field(default_factory=dict)
    _span_how: dict[str, str] = field(default_factory=dict, repr=False)
    # Lazily built lookups; rebuilt when the node count changes.
    _by_file: dict[str, list[str]] = field(default_factory=dict, repr=False)
    _by_file_n: int = field(default=-1, repr=False)
    _docs_by_file: dict[str, list[tuple[int, str]]] = field(default_factory=dict, repr=False)
    _owners: dict[str, list[str | None]] = field(default_factory=dict, repr=False)
    _und: nx.Graph | None = field(default=None, repr=False)
    _und_key: tuple[int, int] = field(default=(-1, -1), repr=False)

    # -- basic lookups ------------------------------------------------------
    def node(self, nid: str) -> dict:
        return dict(self.G.nodes[nid], id=nid)

    def label(self, nid: str) -> str:
        return self.G.nodes[nid].get("label", nid)

    def line(self, nid: str) -> int | None:
        loc = self.G.nodes[nid].get("source_location") or ""
        return int(loc[1:]) if loc.startswith("L") and loc[1:].isdigit() else None

    def file(self, nid: str) -> str | None:
        return self.G.nodes[nid].get("source_file") or None

    def is_symbol(self, nid: str) -> bool:
        d = self.G.nodes[nid]
        return d.get("file_type") == "code" and bool(d.get("source_file")) and not self.is_file_node(nid)

    def is_file_node(self, nid: str) -> bool:
        """The module/file node of a source file.

        Its label is normally the basename; when basenames collide the
        extractor labels it with (a suffix of) the path instead, e.g.
        ``extractors/__init__.py`` or the full ``pkg/extractors/__init__.py``.
        """
        d = self.G.nodes[nid]
        sf = (d.get("source_file") or "").replace("\\", "/")
        if not sf:
            return False
        label = str(d.get("label") or "").replace("\\", "/")
        name = PurePosixPath(sf).name
        return label == name or label == sf or (label.endswith("/" + name) and sf.endswith(label))

    def is_heading(self, nid: str) -> bool:
        """A section heading of a prose file (a document node that is not the page itself)."""
        d = self.G.nodes[nid]
        f = d.get("source_file") or ""
        return (d.get("file_type") == "document" and f.lower().endswith(PROSE_SUFFIXES)
                and not self.is_file_node(nid) and self.line(nid) is not None)

    def edges(self, relations: set[str] | None = None):
        for u, v, d in self.G.edges(data=True):
            if relations is None or d.get("relation") in relations:
                yield u, v, d

    def out_edges(self, nid: str, relations: set[str] | None = None):
        for _, v, d in self.G.out_edges(nid, data=True):
            if relations is None or d.get("relation") in relations:
                yield v, d

    def in_edges(self, nid: str, relations: set[str] | None = None):
        for u, _, d in self.G.in_edges(nid, data=True):
            if relations is None or d.get("relation") in relations:
                yield u, d

    def _index_files(self) -> None:
        if self._by_file_n == len(self.G):
            return
        by: dict[str, list[str]] = {}
        docs: dict[str, list[tuple[int, str]]] = {}
        for n, d in self.G.nodes(data=True):
            f = d.get("source_file")
            if not f:
                continue
            if self.is_symbol(n):
                by.setdefault(f, []).append(n)
            elif self.is_heading(n):
                docs.setdefault(f, []).append((self.line(n) or 0, n))
        for f in by:
            by[f].sort(key=lambda n: (self.line(n) or 0, n))
        for f in docs:
            docs[f].sort()
        self._by_file, self._docs_by_file, self._by_file_n = by, docs, len(self.G)
        self._owners.clear()

    def symbols_in(self, rel_file: str) -> list[str]:
        """Symbols defined in ``rel_file``, by start line (one index built per graph)."""
        self._index_files()
        return list(self._by_file.get(rel_file, ()))

    def headings_in(self, rel_file: str) -> list[str]:
        """Section headings of a prose file, by line."""
        self._index_files()
        return [n for _, n in self._docs_by_file.get(rel_file, ())]

    def symbol_at(self, rel_file: str, line: int) -> str | None:
        """Innermost symbol whose span contains ``line`` (None at module level)."""
        owners = self._owners_of(rel_file)
        return owners[line] if 0 < line < len(owners) else None

    def own_line_count(self, nid: str) -> int:
        """Lines of ``nid``'s span not inside a nested symbol (its own text)."""
        f, sp = self.file(nid), self.span(nid)
        if not f or not sp:
            return 0
        owners = self._owners_of(f)
        return sum(1 for i in range(sp[0], min(sp[1], len(owners) - 1) + 1) if owners[i] == nid)

    def _owners_of(self, rel_file: str) -> list[str | None]:
        """Per-line innermost owner of ``rel_file`` (the per-file interval index)."""
        syms = self.symbols_in(rel_file)  # also resets the owner cache on graph change
        if rel_file in self._owners:
            return self._owners[rel_file]
        spans = [(sp, n) for n in syms if (sp := self.span(n))]
        top = max((sp[1] for sp, _ in spans), default=0)
        owners: list[str | None] = [None] * (top + 1)
        # Paint outer spans first; a later start is nested deeper (innermost wins,
        # the same rule as "latest start among the spans containing the line").
        for (a, b), n in sorted(spans, key=lambda x: (x[0][0], -x[0][1])):
            for i in range(a, b + 1):
                owners[i] = n
        self._owners[rel_file] = owners
        return owners

    def undirected(self) -> nx.Graph:
        """Undirected simple view for Graphify's scorers (cached per graph size)."""
        key = (len(self.G), self.G.number_of_edges())
        if self._und is None or self._und_key != key:
            self._und, self._und_key = nx.Graph(self.G), key
        return self._und

    # -- symbol spans (start/end lines) -------------------------------------
    def span(self, nid: str) -> tuple[int, int] | None:
        """(start, end) lines of a symbol's definition or of a prose section.

        See :meth:`span_basis` for how the end line was found.
        """
        if nid in self._spans:
            return self._spans[nid]
        start, f = self.line(nid), self.file(nid)
        if not start or not f:
            return None
        p = self.root / f
        suffix = p.suffix.lower()
        end, how = None, None
        if f.lower().endswith(PROSE_SUFFIXES) and self.G.nodes[nid].get("file_type") == "document":
            n_lines = _line_count(p)
            if self.is_file_node(nid):
                end, how = (n_lines or start), "file"
            elif suffix in MARKDOWN_SUFFIXES:
                end = _md_section_ends(p).get(start)
                how = "section" if end is not None else None
            if end is None:  # other prose: up to the next heading node
                later = [ln for ln, _ in self._docs_by_file_sorted(f) if ln > start]
                end, how = ((later[0] - 1) if later else (n_lines or start)), "section"
        else:
            if suffix == ".py":
                end = _py_end_line(p, start)
                how = "ast" if end is not None else None
            elif suffix in _TS_LANGS:
                end = _ts_def_ends(p).get(start)
                how = "tree-sitter" if end is not None else None
            if end is None:
                nxt = [self.line(s) for s in self.symbols_in(f)]
                later = sorted(x for x in nxt if x and x > start)
                total = _line_count(p) or start
                end = (later[0] - 1) if later else total
                end = max(start, min(end, start + HEURISTIC_SPAN_CAP))
                how = "heuristic"
        self._spans[nid] = (start, max(start, end))
        self._span_how[nid] = how or "heuristic"
        return self._spans[nid]

    def span_basis(self, nid: str) -> str | None:
        """How :meth:`span` found the end line: ``ast``, ``tree-sitter``, ``section``,
        ``file`` or ``heuristic`` (next symbol, capped); None without a span."""
        if self.span(nid) is None:
            return None
        return self._span_how.get(nid, "heuristic")

    def _docs_by_file_sorted(self, rel_file: str) -> list[tuple[int, str]]:
        self._index_files()
        return self._docs_by_file.get(rel_file, [])

    def source(self, nid: str, max_lines: int = 60) -> tuple[int, int, str] | None:
        sp = self.span(nid)
        f = self.file(nid)
        if not sp or not f:
            return None
        lines = _file_lines(self.root / f)
        if lines is None:
            return None
        s, e = sp
        e = min(e, s + max_lines - 1, len(lines))
        return s, e, "\n".join(lines[s - 1 : e])

    # -- resolution ---------------------------------------------------------
    def resolve(self, query: str) -> tuple[str | None, list[tuple[float, str]]]:
        """Resolve a label / id / file::symbol to a node id (Graphify scoring)."""
        if query in self.G:
            return query, [(1.0, query)]
        if "::" in query:
            f, sym = query.split("::", 1)
            for n in self.symbols_in(f):
                if self.label(n).strip(".()") == sym.strip(".()"):
                    return n, [(1.0, n)]
        for n, d in self.G.nodes(data=True):
            if d.get("source_file") == query and self.is_file_node(n):
                return n, [(1.0, n)]
        if "." in query and " " not in query and not Path(query).suffix[1:] in ("py", "js", "ts", "go", "rs"):
            cls, _, meth = query.rpartition(".")
            for n, d in self.G.nodes(data=True):
                if d.get("_callable_class") and d.get("label") == cls.rpartition(".")[2]:
                    for v, _ in self.out_edges(n, {"method"}):
                        if self.label(v).strip(".()") == meth:
                            return v, [(1.0, v)]
        from verinoda.project_index.serve import _pick_scored_endpoint, _score_nodes, _search_tokens

        und = self.undirected()
        scored = _score_nodes(und, [t.lower() for t in query.replace("::", " ").split()])
        if not scored:
            return None, []
        pick = _pick_scored_endpoint(und, scored, query)
        # The scorer is fuzzy (IDF over sub-tokens): 'definitely_not_a_symbol'
        # can land on some file node. Accept a scored pick only when a
        # meaningful query token really occurs in its label, id or file;
        # otherwise report it as unresolved and let callers show candidates.
        tokens = _search_tokens(query)
        meaningful = [t for t in tokens if len(t) >= 3] or tokens
        d = self.G.nodes[pick]
        hay = " ".join([str(d.get("label") or ""), pick, str(d.get("source_file") or "")]).lower()
        if meaningful and not any(t in hay for t in meaningful):
            return None, scored[:5]
        return pick, scored[:5]


# -- per-file facts (one parse per file version) ------------------------------------------------

_LINES_CACHE: dict[tuple[str, int, int], list[str]] = {}
_PYINFO_CACHE: dict[tuple[str, int, int], "PyInfo"] = {}
_TS_CACHE: dict[tuple[str, int, int], dict[int, int]] = {}
_MD_CACHE: dict[tuple[str, int, int], dict[int, int]] = {}
_AST_CACHE: dict[tuple[str, int, int], ast.AST] = {}


def _stat_key(p: Path) -> tuple[str, int, int]:
    st = p.stat()
    return str(p), st.st_mtime_ns, st.st_size


def _cached(cache: dict, key, make):
    if key not in cache:
        if len(cache) > _CACHE_MAX:
            cache.clear()
        cache[key] = make()
    return cache[key]


@dataclass
class PyInfo:
    """What one ``ast.parse`` of a Python file yields for Verinoda.

    ``ends``: def/class start line (and first decorator line) -> end line; the
    first definition met in ``ast.walk`` order wins a shared line.
    ``docs``: the same start lines -> docstring. ``receivers``: per function
    start line, the facts :func:`receiver_call_edges` resolves against the
    graph (annotated parameters, ``x = Cls(...)`` assignments and
    ``x.method()`` calls, in the order the original per-function pass saw them).
    """

    ends: dict[int, int]
    docs: dict[int, str]
    receivers: dict[int, dict]


def py_file_info(p: Path) -> PyInfo:
    """Parse ``p`` once per version (size, mtime) and keep only small derived facts."""
    key = _stat_key(p)
    return _cached(_PYINFO_CACHE, key, lambda: _py_info(p.read_bytes()))


def _py_info(source: bytes) -> PyInfo:
    tree = ast.parse(source.decode("utf-8", errors="replace"))
    ends: dict[int, int] = {}
    docs: dict[int, str] = {}
    funcs: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            end = getattr(node, "end_lineno", None)
            if end is not None:
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                ends.setdefault(node.lineno, end)
                ends.setdefault(first, end)
            doc = ast.get_docstring(node)
            if doc:
                docs.setdefault(node.lineno, doc)
                for d in node.decorator_list:
                    docs.setdefault(d.lineno, doc)
            if not isinstance(node, ast.ClassDef):
                funcs.setdefault(node.lineno, node)
    return PyInfo(ends, docs, {ln: _receiver_facts(fn) for ln, fn in funcs.items()})


def _receiver_facts(fn) -> dict:
    ann: list[tuple[str, str]] = []
    for a in fn.args.args + fn.args.kwonlyargs:
        an = a.annotation
        name = an.id if isinstance(an, ast.Name) else (
            an.value if isinstance(an, ast.Constant) and isinstance(an.value, str) else None)
        if name:
            ann.append((a.arg, name))
    assigns: list[tuple[str, str]] = []
    for st in ast.walk(fn):
        if isinstance(st, ast.Assign) and isinstance(st.value, ast.Call) and isinstance(st.value.func, ast.Name):
            for t in st.targets:
                if isinstance(t, ast.Name):
                    assigns.append((t.id, st.value.func.id))
    receivers = {v for v, _ in ann} | {v for v, _ in assigns}
    calls: list[tuple[int, str, str]] = []
    if receivers:
        for call in ast.walk(fn):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name) and call.func.value.id in receivers):
                calls.append((call.lineno, call.func.value.id, call.func.attr))
    return {"ann": ann, "assign": assigns, "calls": calls}


def _py_end_line(p: Path, start: int) -> int | None:
    try:
        return py_file_info(p).ends.get(start)
    except (SyntaxError, ValueError, OSError, RecursionError):
        return None


def _py_def_ends(p: Path) -> dict[int, int]:
    """def/class start line (and first decorator line) -> end line, for one file."""
    return py_file_info(p).ends


def _file_lines(p: Path) -> list[str] | None:
    """Lines of a source file, cached per file version (size and mtime)."""
    try:
        return _cached(_LINES_CACHE, _stat_key(p),
                       lambda: p.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return None


def file_lines(p: Path) -> list[str] | None:
    """Public alias of the cached line reader (``None`` when unreadable)."""
    return _file_lines(p)


def _line_count(p: Path) -> int | None:
    lines = _file_lines(p)
    return None if lines is None else len(lines)


def _py_ast(p: Path) -> ast.AST:
    """Parsed module, cached per file version (kept for callers outside the index)."""
    return _cached(_AST_CACHE, _stat_key(p),
                   lambda: ast.parse(p.read_text(encoding="utf-8", errors="replace")))


# Markdown sections -----------------------------------------------------------------------------

_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]|$)")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")


def md_headings(lines: list[str]) -> list[tuple[int, int]]:
    """(line, level) of every ATX or setext heading outside fenced code and front matter."""
    heads: list[tuple[int, int]] = []
    fence = None
    start = 1
    if lines and lines[0].strip() == "---":  # YAML front matter
        for i in range(1, len(lines)):
            if lines[i].strip() in ("---", "..."):
                start = i + 2
                break
    for i in range(start, len(lines) + 1):
        line = lines[i - 1]
        m = _FENCE.match(line)
        if m:
            if fence is None:
                fence = m.group(1)[0]
            elif m.group(1)[0] == fence:
                fence = None
            continue
        if fence is not None:
            continue
        m = _ATX.match(line)
        if m:
            heads.append((i, len(m.group(1))))
            continue
        m = _SETEXT.match(line)
        if m and i > start and lines[i - 2].strip() and not _ATX.match(lines[i - 2]) \
                and not (heads and heads[-1][0] == i - 1):
            heads.append((i - 1, 1 if m.group(1)[0] == "=" else 2))
    return heads


def md_section_ends(lines: list[str]) -> dict[int, int]:
    """Heading line -> last line of its section.

    A section ends before the next heading of the same or a higher level (so a
    ``##`` release entry keeps its ``###`` sub-sections), trailing blank lines
    excluded. The former rule - up to 80 lines past the heading - let one
    CHANGELOG entry swallow the next ones.
    """
    heads = md_headings(lines)
    ends: dict[int, int] = {}
    for k, (ln, lvl) in enumerate(heads):
        end = len(lines)
        for ln2, lvl2 in heads[k + 1:]:
            if lvl2 <= lvl:
                end = ln2 - 1
                break
        while end > ln and not lines[end - 1].strip():
            end -= 1
        ends[ln] = max(ln, end)
    return ends


def _md_section_ends(p: Path) -> dict[int, int]:
    try:
        return _cached(_MD_CACHE, _stat_key(p), lambda: md_section_ends(_file_lines(p) or []))
    except OSError:
        return {}


# Tree-sitter spans (non-Python code) ------------------------------------------------------------

_TS_LANGS: dict[str, tuple[str, str]] = {
    ".js": ("tree_sitter_javascript", "language"), ".jsx": ("tree_sitter_javascript", "language"),
    ".mjs": ("tree_sitter_javascript", "language"), ".cjs": ("tree_sitter_javascript", "language"),
    ".ts": ("tree_sitter_typescript", "language_typescript"), ".mts": ("tree_sitter_typescript", "language_typescript"),
    ".cts": ("tree_sitter_typescript", "language_typescript"), ".tsx": ("tree_sitter_typescript", "language_tsx"),
    ".go": ("tree_sitter_go", "language"), ".rs": ("tree_sitter_rust", "language"),
    ".java": ("tree_sitter_java", "language"), ".groovy": ("tree_sitter_groovy", "language"),
    ".c": ("tree_sitter_c", "language"), ".h": ("tree_sitter_c", "language"),
    ".cpp": ("tree_sitter_cpp", "language"), ".cc": ("tree_sitter_cpp", "language"),
    ".cxx": ("tree_sitter_cpp", "language"), ".hpp": ("tree_sitter_cpp", "language"),
    ".hh": ("tree_sitter_cpp", "language"), ".rb": ("tree_sitter_ruby", "language"),
    ".cs": ("tree_sitter_c_sharp", "language"), ".kt": ("tree_sitter_kotlin", "language"),
    ".kts": ("tree_sitter_kotlin", "language"), ".scala": ("tree_sitter_scala", "language"),
    ".php": ("tree_sitter_php", "language_php"), ".lua": ("tree_sitter_lua", "language"),
    ".swift": ("tree_sitter_swift", "language"),
}
# Node types that define something (the outermost one starting on a line gives its end line).
_TS_DEF_SUFFIXES = ("_definition", "_declaration", "_item", "_declarator", "_spec")
_TS_DEF_TYPES = frozenset({
    "method", "function", "class", "module", "singleton_method", "singleton_class", "arrow_function",
    "function_expression", "generator_function", "func_literal", "decorated_definition",
    "companion_object", "object_definition", "closure_expression", "lambda_expression",
})
_TS_PARSERS: dict[str, object] = {}


def _ts_parser(suffix: str):
    spec = _TS_LANGS.get(suffix)
    if spec is None:
        return None
    if suffix not in _TS_PARSERS:
        try:
            import importlib

            from tree_sitter import Language, Parser

            mod = importlib.import_module(spec[0])
            _TS_PARSERS[suffix] = Parser(Language(getattr(mod, spec[1])()))
        except Exception:  # noqa: BLE001 - grammar missing or incompatible: no tree-sitter spans
            _TS_PARSERS[suffix] = None
    return _TS_PARSERS[suffix]


def ts_def_ends(source: bytes, suffix: str) -> dict[int, int]:
    """Start line -> end line of the outermost definition-like node starting there."""
    parser = _ts_parser(suffix)
    if parser is None:
        return {}
    tree = parser.parse(source)
    ends: dict[int, int] = {}
    cursor = tree.walk()
    while True:
        node = cursor.node
        t = node.type
        if node.is_named and (t in _TS_DEF_TYPES or t.endswith(_TS_DEF_SUFFIXES)):
            a, b = node.start_point[0] + 1, node.end_point[0] + 1
            if b > ends.get(a, 0):
                ends[a] = b
        if cursor.goto_first_child():
            continue
        while not cursor.goto_next_sibling():
            if not cursor.goto_parent():
                return ends


def _ts_def_ends(p: Path) -> dict[int, int]:
    try:
        return _cached(_TS_CACHE, _stat_key(p), lambda: ts_def_ends(p.read_bytes(), p.suffix.lower()))
    except (OSError, ValueError):
        return {}


# -- load ---------------------------------------------------------------------------------------

def load(repo: Path, gp: Path | None = None, *, augment: bool = True) -> Graph:
    repo = Path(repo).resolve()
    gp = Path(gp) if gp else graph_path(repo)
    if not gp.exists():
        raise FileNotFoundError(f"no graph at {gp}; run `verinoda scan {repo}` first")
    data = json.loads(gp.read_text(encoding="utf-8"))
    G = nx.MultiDiGraph()
    for n in data.get("nodes", []):
        G.add_node(n["id"], **{k: v for k, v in n.items() if k != "id"})
    for e in data.get("links", data.get("edges", [])):
        u, v = e.get("_src", e["source"]), e.get("_tgt", e["target"])
        if u in G and v in G:
            G.add_edge(u, v, **{k: val for k, val in e.items() if k not in ("source", "target")})
    g = Graph(G=G, path=gp, root=repo)
    if augment:
        apply_receiver_calls(g)
    return g


# -- graph identity (shared with the search index) --------------------------------------------------

def sha256_path(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def graph_identity(gp: Path, *, with_sha: bool = True) -> dict:
    """``{"size", "mtime_ns"[, "sha256"]}`` of a graph file."""
    st = Path(gp).stat()
    out = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    if with_sha:
        out["sha256"] = sha256_path(Path(gp))
    return out


def same_graph(gp: Path, recorded: dict | None) -> bool:
    """Is ``gp`` the graph ``recorded`` describes? Stat first, content hash when the stat differs."""
    if not recorded:
        return False
    try:
        st = Path(gp).stat()
    except OSError:
        return False
    if recorded.get("size") != st.st_size:
        return False
    if recorded.get("mtime_ns") == st.st_mtime_ns:
        return True
    return bool(recorded.get("sha256")) and recorded["sha256"] == sha256_path(Path(gp))


def write_json_atomic(p: Path, obj) -> None:
    """Write JSON (LF, utf-8) through a temporary file and an atomic replace."""
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            # json.dumps, not json.dump: the same text, through the C encoder (json.dump to a file
            # always takes the pure-Python one)
            f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# -- receiver-call edges ----------------------------------------------------------------------------

def _receiver_candidates(g: Graph) -> list[tuple[str, str]]:
    """(node id, file) of the Python callables the receiver pass looks into, in graph order."""
    out = []
    for n, d in g.G.nodes(data=True):
        f = d.get("source_file") or ""
        if f.endswith(".py") and d.get("_callable") and not d.get("_callable_class"):
            out.append((n, f))
    return out


def _top_folder(f: str) -> str:
    return f.split("/", 1)[0] + "/" if "/" in f else ""


def _file_imports(g: Graph) -> dict[str, set[str]]:
    """file -> the files its import statements resolve to (``imports`` / ``imports_from`` edges)."""
    out: dict[str, set[str]] = {}
    for u, v, d in g.G.edges(data=True):
        if d.get("relation") in ("imports", "imports_from"):
            fu, fv = g.file(u), g.file(v)
            if fu and fv and fu != fv:
                out.setdefault(fu, set()).add(fv)
    return out


def _copy_roots(g: Graph) -> tuple[str, ...]:
    """Folders detected as copies of the project and configured reference trees (the last detection)."""
    try:
        from verinoda import copies
        from verinoda.paths import load_config

        ref = (load_config(g.root).get("index") or {}).get("reference") or []
        roots = [c["path"] for c in copies.load(g.root)]
        roots += [(e.get("path") if isinstance(e, dict) else e) or "" for e in ref]
    except Exception:  # noqa: BLE001 - no config or detection: the import rule alone
        return ()
    return tuple(r.strip("/") + "/" for r in roots if isinstance(r, str) and r.strip("/"))


def _visible_class(g: Graph, f: str, cands, imports: dict[str, set[str]], copy_roots: tuple[str, ...]) -> str | None:
    """The class among ``cands`` (same label) that a name in file ``f`` refers to, or None.

    Its own file first, then a file ``f`` imports, then a class under a top-level folder ``f``
    imports from or lives in; a class across a copy's boundary (either way) never counts."""
    if not cands:
        return None
    mine = next((r for r in copy_roots if f.startswith(r)), None)
    seen = imports.get(f, set())
    tops = {_top_folder(x) for x in seen} | {_top_folder(f)}
    best: list[tuple[int, str]] = []
    for cid in cands:
        cf = g.file(cid) or ""
        if next((r for r in copy_roots if cf.startswith(r)), None) != mine:
            continue
        tier = 0 if cf == f else 1 if cf in seen else 2 if _top_folder(cf) in tops else None
        if tier is not None:
            best.append((tier, cid))
    if not best:
        return None
    top = min(t for t, _ in best)
    return next(c for t, c in best if t == top)


def receiver_call_edges(g: Graph, facts_for=None) -> list[tuple[str, str, dict]]:
    """``calls`` edges for ``param.method()`` where ``param: SomeClass`` (not applied).

    The Graphify-derived extractor records ``place_order -uses-> OrderRepository``
    from the annotation but not ``repo.save(...)``. This pass resolves such
    calls when the receiver is a parameter annotated with (or a local assigned
    from) a class that has that method in the graph. Edges are marked
    ``confidence=INFERRED`` and ``_origin=verinoda.receiver`` so they can never
    be mistaken for extractor facts. ``facts_for(file) -> {start line: facts}``
    supplies per-file facts (default: parse the file now).

    The class a name means is the one the calling file can see (:func:`_visible_class`): its own,
    one from a file it imports, then one under a top-level folder it imports from or shares. A
    class in another top-level package the file never imports from (the project's own ``Claims``
    for a frozen copy's ``cl: Claims``) is never linked, nor is a detected copy linked to the
    code outside it, either way.
    """
    classes: dict[str, list[str]] = {}
    methods: dict[tuple[str, str], str] = {}
    for n, d in g.G.nodes(data=True):
        if d.get("_callable_class") and d.get("file_type") == "code":
            classes.setdefault(d.get("label", ""), []).append(n)
    for cids in classes.values():
        for cid in cids:
            for v, _ in g.out_edges(cid, {"method"}):
                methods[(cid, g.label(v).strip(".()"))] = v
    imports = _file_imports(g)
    copy_roots = _copy_roots(g)
    if facts_for is None:
        def facts_for(f: str) -> dict | None:
            try:
                return py_file_info(g.root / f).receivers
            except (SyntaxError, ValueError, OSError, RecursionError):
                return None
    have = {(u, v) for u, v, d in g.G.edges(data=True) if d.get("relation") == "calls"}
    out: list[tuple[str, str, dict]] = []
    per_file: dict[str, dict | None] = {}
    for n, f in _receiver_candidates(g):
        if f not in per_file:
            per_file[f] = facts_for(f)
        facts = per_file[f]
        fx = facts.get(g.line(n)) if facts else None
        if fx is None:
            fx = facts.get(str(g.line(n))) if facts else None  # facts read back from JSON
        if not fx:
            continue
        typed: dict[str, str] = {}
        for arg, name in fx["ann"]:
            cid = _visible_class(g, f, classes.get(name) or (), imports, copy_roots)
            if cid:
                typed[arg] = cid
        for var, cname in fx["assign"]:
            cid = _visible_class(g, f, classes.get(cname) or (), imports, copy_roots)
            if cid:
                typed[var] = cid
        for line, var, attr in fx["calls"]:
            cid = typed.get(var)
            target = methods.get((cid, attr)) if cid else None
            if target and (n, target) not in have:
                have.add((n, target))
                out.append((n, target, {"relation": "calls", "confidence": "INFERRED", "confidence_score": 0.7,
                                        "_origin": RECEIVER_ORIGIN, "source_file": f, "source_location": f"L{line}",
                                        "context": f"{var}.{attr}() on {g.label(cid)}"}))
    return out


# -- Java calls the extractor left out ---------------------------------------------------------------

_JAVA_STRING = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
_JAVA_CALL = re.compile(r"(?<![\w.$])([A-Za-z_$][\w$]*)\s*\.\s*([a-z_$][\w$]*)\s*\(")
_JAVA_DECL = re.compile(r"(?<![\w.$])([A-Z][\w$]*)(?:<[^<>;()]*(?:<[^<>;()]*>[^<>;()]*)*>)?(?:\[\])?\s+([a-z_$][\w$]*)\s*(?=[=;,):])")
# matched on comment-free lines (_java_code_lines): `import a.B; // NOPMD`, Kotlin `import a.B as C`
_JAVA_IMPORT = re.compile(r"\s*import\s+(static\s+)?([\w.]+)\.([\w$]+|\*)\s*(?:;|\s+as\s+\w+|$)")
_JAVA_PACKAGE_DECL = re.compile(r"\s*package\s+([\w.]+)\s*(?:;|$)")
# Kotlin: `val x: Type`, `var x: Type = ...`, a parameter `x: Type`, `val x = Type(...)`
_KOTLIN_DECL = re.compile(r"(?:\b(?:val|var)\s+|[(,]\s*)([a-z_]\w*)\s*:\s*([A-Z]\w*)"
                          r"|\b(?:val|var)\s+([a-z_]\w*)\s*=\s*([A-Z]\w*)\s*\(")
# Kotlin properties (class body or primary constructor): the fields of the class
_KOTLIN_PROP = re.compile(r"\b(?:val|var)\s+([a-z_]\w*)\s*(?::\s*([A-Z]\w*)|=\s*([A-Z]\w*)\s*\()")
JVM_SUFFIXES = (".java", ".kt")


def _java_code_lines(text: str, *, kotlin: bool = False) -> list[str]:
    """The file's lines with string literals and comments blanked (same line numbers).

    ``kotlin``: Kotlin raw strings (three double quotes), which may span lines, are blanked too.
    """
    out, in_block, in_raw = [], False, False
    for ln in text.splitlines():
        s = ln
        if kotlin:
            if in_raw:
                end = s.find('"""')
                if end < 0:
                    out.append("")
                    continue
                s, in_raw = '""' + s[end + 3:], False
            while '"""' in s:
                a = s.find('"""')
                b = s.find('"""', a + 3)
                if b < 0:
                    s, in_raw = s[:a] + '""', True
                    break
                s = s[:a] + '""' + s[b + 3:]
        s = _JAVA_STRING.sub('""', s)
        if in_block:
            end = s.find("*/")
            if end < 0:
                out.append("")
                continue
            s, in_block = s[end + 2:], False
        while "/*" in s:
            a = s.find("/*")
            b = s.find("*/", a + 2)
            if b < 0:
                s, in_block = s[:a], True
                break
            s = s[:a] + " " + s[b + 2:]
        out.append(s.split("//", 1)[0])
    return out


def java_call_edges(g: Graph, read=None) -> list[tuple[str, str, dict]]:
    """``calls`` edges for Java ``Cls.method(...)`` and ``var.method(...)`` the extractor did not record.

    Graphify's Java pass drops a call when the class name is ambiguous in the repository (a
    reference copy of the code, two ``Wisp`` classes) and never follows typed variables. Here a
    class is resolved the way javac does it for the file: an explicit import of that class, the
    file's own package, or a ``pkg.*`` import; with none of these, or with an import from another
    package, no edge is made. ``var.method()`` follows the declared type of a parameter, local or
    field (``Wisp w = ...``). Edges are ``INFERRED`` with ``_origin=verinoda.java_calls``.
    """
    classes: dict[str, list[tuple[str, str]]] = {}
    methods: dict[tuple[str, str], str] = {}
    for n, d in g.G.nodes(data=True):
        f = d.get("source_file") or ""
        if f.endswith(JVM_SUFFIXES) and d.get("_callable_class"):
            classes.setdefault(d.get("label", ""), []).append((n, f))
    if not classes:
        return []
    by_file: dict[str, list[str]] = {}
    for n, d in g.G.nodes(data=True):
        f = d.get("source_file") or ""
        if f.endswith(JVM_SUFFIXES) and d.get("_callable") and not d.get("_callable_class"):
            by_file.setdefault(f, []).append(n)
    # a method belongs to the innermost class whose span holds it (the extractor's `method` edge
    # can be lost when the method also calls its own class's constructor)
    class_spans: dict[str, list[tuple[int, int, str]]] = {}
    for lst in classes.values():
        for cid, cf in lst:
            sp = g.span(cid)
            if sp:
                class_spans.setdefault(cf, []).append((sp[0], sp[1], cid))
    for f, ms in by_file.items():
        for m in ms:
            line = g.line(m)
            owners = [(b - a, cid) for a, b, cid in class_spans.get(f, []) if line and a <= line <= b]
            if owners:
                methods.setdefault((min(owners)[1], g.label(m).strip(".()")), m)
    if read is None:
        def read(f: str) -> str | None:
            try:
                return (g.root / f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
    have = {(u, v) for u, v, d in g.G.edges(data=True) if d.get("relation") == "calls"}
    out: list[tuple[str, str, dict]] = []
    for f in sorted(by_file):
        text = read(f)
        if text is None:
            continue
        kotlin = f.endswith(".kt")
        code = _java_code_lines(text, kotlin=kotlin)
        imports = [m.groups() for ln in code if (m := _JAVA_IMPORT.match(ln))]
        pkg = next((m.group(1) for ln in code if (m := _JAVA_PACKAGE_DECL.match(ln))), None)
        here = str(PurePosixPath(f).parent)

        def resolve(cls: str) -> str | None:
            cands = classes.get(cls) or []
            if not cands:
                return None

            def pkg_of(cf: str, name: str) -> bool:
                return str(PurePosixPath(cf).parent).replace("\\", "/").endswith("/" + name.replace(".", "/")) \
                    or str(PurePosixPath(cf).parent) == name.replace(".", "/")

            explicit = [p for st, p, c in imports if c == cls and not st]
            if explicit:
                hit = [cid for cid, cf in cands if any(pkg_of(cf, p) for p in explicit)]
            else:
                hit = [cid for cid, cf in cands if str(PurePosixPath(cf).parent) == here
                       or (pkg is not None and pkg_of(cf, pkg))]
                if not hit:
                    wild = [p for st, p, c in imports if c == "*" and not st]
                    hit = [cid for cid, cf in cands if any(pkg_of(cf, p) for p in wild)]
            return hit[0] if len(hit) == 1 else None

        def decls(line: str) -> list[tuple[str, str]]:
            if kotlin:
                return [(m.group(1) or m.group(3), m.group(2) or m.group(4)) for m in _KOTLIN_DECL.finditer(line)]
            return [(m.group(2), m.group(1)) for m in _JAVA_DECL.finditer(line)]

        if kotlin:  # `class S(private val repo: Repo)`, `private val backup: Repo = Repo()`
            fields = {m.group(1): m.group(2) or m.group(3) for ln in code for m in _KOTLIN_PROP.finditer(ln)}
        else:
            fields = {m.group(2): m.group(1) for ln in code for m in _JAVA_DECL.finditer(ln)
                      if re.match(r"\s*(?:(?:private|protected|public|static|final|volatile|transient)\s+)+", ln)}
        for n in by_file[f]:
            sp = g.span(n)
            if not sp:
                continue
            a, b = sp[0], min(sp[1], len(code))
            local = dict(fields)
            for i in range(a, b + 1):
                for var, typ in decls(code[i - 1]):
                    local[var] = typ
            for i in range(a, b + 1):
                for m in _JAVA_CALL.finditer(code[i - 1]):
                    recv, meth = m.group(1), m.group(2)
                    if recv[0].isupper():
                        cls, how = recv, "class"
                    elif recv in local and recv not in ("this", "super"):
                        cls, how = local[recv], "typed"
                    else:
                        continue
                    cid = resolve(cls)
                    target = methods.get((cid, meth)) if cid else None
                    if not target or target == n or (n, target) in have:
                        continue
                    have.add((n, target))
                    ctx = f"{recv}.{meth}()" + ("" if how == "class" else f" on {cls}")
                    out.append((n, target, {"relation": "calls", "confidence": "INFERRED",
                                            "confidence_score": 0.9 if how == "class" else 0.75,
                                            "_origin": JAVA_CALL_ORIGIN, "source_file": f,
                                            "source_location": f"L{i}", "context": ctx}))
    return out


def _apply_edges(g: Graph, edges) -> int:
    g.__dict__.pop("_ppr_adj", None)  # search_index caches weighted neighbours per graph
    g.__dict__.pop("_qp_ix", None)    # and question_plan its lookup index
    added = 0
    for u, v, d in edges:
        if u not in g.G or v not in g.G:
            continue
        if any(dd.get("relation") == "calls" for dd in (g.G.get_edge_data(u, v) or {}).values()):
            continue
        g.G.add_edge(u, v, **d)
        added += 1
    return added


def augment_python_receiver_calls(g: Graph) -> int:
    """Compute and add the receiver-call (and Java call) edges in memory; returns the number added."""
    return _apply_edges(g, receiver_call_edges(g) + java_call_edges(g))


_SIDECAR_FACTS_SINCE = 2   # the per-file facts have this shape since v2 (v3 changed only how edges are made)


def _read_sidecar(repo: Path, *, facts_only: bool = False) -> dict | None:
    """The receiver-call sidecar of this version; ``facts_only``: also an older one whose per-file
    facts can be reused (its edges cannot)."""
    try:
        data = json.loads(receiver_calls_path(repo).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    v = data.get("version")
    if v == RECEIVER_SIDECAR_VERSION or (facts_only and isinstance(v, int) and _SIDECAR_FACTS_SINCE <= v):
        return data
    return None


def refresh_receiver_sidecar(repo: Path, g: Graph | None = None) -> dict:
    """Recompute ``receiver_calls.json`` for the current graph (per-file facts reused by sha256)."""
    repo = Path(repo).resolve()
    g = g or load(repo, augment=False)
    old = _read_sidecar(repo, facts_only=True) or {}
    old_files = old.get("files") or {}
    files: dict[str, dict] = {}
    parsed = 0

    def facts_for(f: str) -> dict | None:
        nonlocal parsed
        p = repo / f
        try:
            data = p.read_bytes()
        except OSError:
            return None
        sha = hashlib.sha256(data).hexdigest()
        prev = old_files.get(f)
        if prev and prev.get("sha256") == sha:
            files[f] = prev
            return prev.get("facts")
        try:
            facts = _py_info(data).receivers
        except (SyntaxError, ValueError, RecursionError):
            facts = None
        parsed += 1
        files[f] = {"sha256": sha, "facts": None if facts is None else {str(k): v for k, v in facts.items()}}
        return files[f]["facts"]

    edges = receiver_call_edges(g, facts_for) + java_call_edges(g)
    sidecar = {"version": RECEIVER_SIDECAR_VERSION, "graph": graph_identity(g.path),
               "files": files, "edges": [[u, v, d] for u, v, d in edges]}
    write_json_atomic(receiver_calls_path(repo), sidecar)
    return {"edges": len(edges), "files_parsed": parsed, "files_reused": len(files) - parsed}


def apply_receiver_calls(g: Graph) -> int:
    """Add the stored receiver-call edges when they describe this graph file, else compute them.

    A missing or stale sidecar (older Verinoda, graph rewritten elsewhere) is
    recomputed in memory and, when the index directory is writable, stored.
    """
    side = _read_sidecar(g.root)
    if side and same_graph(g.path, side.get("graph")):
        return _apply_edges(g, [(u, v, d) for u, v, d in side.get("edges") or []])
    try:
        if g.path == graph_path(g.root):
            refresh_receiver_sidecar(g.root, g)
            side = _read_sidecar(g.root)
            if side and same_graph(g.path, side.get("graph")):
                return _apply_edges(g, [(u, v, d) for u, v, d in side.get("edges") or []])
    except OSError:
        pass
    return augment_python_receiver_calls(g)


# -- deleted files ------------------------------------------------------------------------------------

_REMOTE_SOURCE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]+:/")


def _local_source(repo: Path, sf) -> Path | None:
    """The file a node's ``source_file`` names inside ``repo`` (None for URLs/virtual/outside)."""
    if not isinstance(sf, str) or not sf or _REMOTE_SOURCE.match(sf):
        return None
    p = Path(sf)
    if not p.is_absolute():
        p = repo / p
    try:
        p.resolve().relative_to(repo)
    except (ValueError, OSError):
        return None
    return p


def _missing_in_nodes(repo: Path, nodes) -> list[str]:
    seen: dict[str, bool] = {}
    for n in nodes:
        sf = n.get("source_file")
        if not isinstance(sf, str) or sf in seen:
            continue
        p = _local_source(repo, sf)
        seen[sf] = p is not None and not p.exists()
    return sorted(sf for sf, gone in seen.items() if gone)


def _drop_files(data: dict, gone: set[str]) -> None:
    """Remove, in place, the nodes of the files ``gone`` and the edges and hyperedges on them."""
    drop = {n["id"] for n in data.get("nodes", []) if n.get("source_file") in gone}
    data["nodes"] = [n for n in data.get("nodes", []) if n["id"] not in drop]
    for key in ("links", "edges"):
        if key in data:
            data[key] = [e for e in data[key]
                         if e.get("source") not in drop and e.get("target") not in drop
                         and e.get("_src") not in drop and e.get("_tgt") not in drop
                         and e.get("source_file") not in gone]
    if isinstance(data.get("hyperedges"), list):
        data["hyperedges"] = [h for h in data["hyperedges"]
                              if not (isinstance(h, dict) and h.get("source_file") in gone)]


def _write_pruned(gp: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix="graph.", suffix=".tmp", dir=str(gp.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, gp)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _post_process(gp: Path, repo: Path, *, prune: bool) -> tuple[dict, list[str] | None, dict]:
    """graph.json after a build, read once and written at most once: the ids made portable
    (:func:`verinoda.portable_ids.make_graph_portable`) and, with ``prune``, the nodes of
    missing files dropped (:func:`prune_missing_files`). The file ends byte for byte as those
    two steps one after the other leave it: in the format of the last one that wrote.
    Returns ``({"changed": ids rewritten}, files pruned or None, the graph data)``."""
    from verinoda.portable_ids import strip_root_from_ids, write_graph

    repo = Path(repo).resolve()
    data = json.loads(gp.read_text(encoding="utf-8"))
    edges = data.get("links") if isinstance(data.get("links"), list) else data.get("edges") or []
    n = strip_root_from_ids(data.get("nodes") or [], edges, repo, data.get("hyperedges"))
    gone = _missing_in_nodes(repo, data.get("nodes", [])) if prune else []
    if gone:
        _drop_files(data, set(gone))
        _write_pruned(gp, data)
    elif n:
        write_graph(gp, data)
    return {"changed": n}, (gone if prune else None), data


def missing_source_files(repo: Path, gp: Path | None = None) -> list[str]:
    """``source_file`` values of graph nodes whose file no longer exists in ``repo``."""
    repo = Path(repo).resolve()
    gp = Path(gp) if gp else graph_path(repo)
    try:
        data = json.loads(gp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return _missing_in_nodes(repo, data.get("nodes", []))


def prune_missing_files(repo: Path, gp: Path | None = None) -> list[str]:
    """Drop nodes (and their edges) whose source file was deleted; returns the files pruned.

    The last line of defence behind a forced rebuild: the graph must never
    describe a file that no longer exists. The receiver sidecar is refreshed
    when anything was removed.
    """
    repo = Path(repo).resolve()
    gp = Path(gp) if gp else graph_path(repo)
    gone = set(missing_source_files(repo, gp))
    if not gone:
        return []
    data = json.loads(gp.read_text(encoding="utf-8"))
    _drop_files(data, gone)
    _write_pruned(gp, data)
    try:
        refresh_receiver_sidecar(repo)
    except (OSError, ValueError):
        pass
    return sorted(gone)


def graph_source_files(repo: Path, gp: Path | None = None) -> set[str]:
    """The files graph.json has nodes from, read from the JSON alone: the ``source_file``
    values :func:`load` would give its nodes (a repeated id keeps the last value it was
    given), with no graph built and no receiver pass."""
    gp = Path(gp) if gp else graph_path(Path(repo).resolve())
    data = json.loads(gp.read_text(encoding="utf-8"))
    last: dict = {}
    for n in data.get("nodes", []):
        nid = n["id"]
        if "source_file" in n:
            last[nid] = n["source_file"]
        else:
            last.setdefault(nid, None)
    return {sf for sf in last.values() if sf}


def graphify_query_text(repo: Path, question: str, budget: int = 2000) -> str:
    """Graphify's own query renderer - the baseline the benchmark compares to."""
    from verinoda.project_index.serve import _load_graph, _query_graph_text

    gp = graph_path(Path(repo).resolve())
    G = _load_graph(str(gp))
    return _query_graph_text(G, question, token_budget=budget, graph_path=str(gp))

