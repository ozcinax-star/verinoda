"""An update proportional to the change: re-extract the files an edit can affect and patch graph.json.

A full build extracts every file of the corpus (the AST cache makes most of them cheap), runs every
cross-file pass over the whole corpus, builds a NetworkX graph from the result, clusters it and writes
graph.json: about 100 seconds on Verinoda's own repository even when one function was appended. This
module does the same work for only the files an edit can change, behind a switch
(``VERINODA_INCREMENTAL=1`` or the config key ``index.incremental``; off by default):

* **The ledger** (``index/incremental/``), written after every full build with the switch on: the corpus as
  the extractor saw it (its files in extraction order, each one's detect category and AST-cache key, every
  directory with its modification time and entries), the build's extraction - what ``extract()`` returned,
  which is what ``build_from_json`` reads on a fresh scan -, each file's identifier tokens and the non-path
  ids its own extraction makes or ends edges on (:func:`pre_ids`).
* **An update** where only existing files changed content: the affected set A is the edited files, the
  files that name a definition that changed (by token), the files sharing an id stem with one, the files
  that make or end an edge on a non-path id an edited file starts or stops making (or naming as a base) and
  the importers of an edited file whose imports changed. The batch B adds the files defining what A's files
  reference as a stub, an import or a base, the files their imports resolve to, and two other makers of each
  non-path id they make. The unmodified vendored ``extract()`` runs over B, given the rest of the corpus as
  read-only resolution context as the vendored incremental path does; the rows A's files own replace theirs
  in the stored extraction (:func:`_splice`).
* **The graph**: :func:`build_rows` does what ``build_from_json`` and ``to_json`` do to an AST-only
  extraction without NetworkX (a pair of nodes keeps one edge, chosen by the same rules in the same
  order), so graph.json is made from the patched extraction in a few seconds; each node keeps the
  community it had, a new node takes the one most of its file's nodes have.

Any doubt falls back to the full build, and the update says why (``incremental: {used, reason}``):
a file added, removed or renamed (the directory listings differ), a changed file that is not in the
corpus or whose category changed, an affected file in a language the closure was not checked for
(:data:`LANGUAGES`), an extraction stamp or build configuration other than the ledger's, a batch over
:data:`MAX_BATCH`, a vendored builder other than the one this was checked against (:data:`PINNED`), or
anything in the patched extraction the row model does not cover (non-AST nodes, hyperedges, an edited
file's row sharing an id with a different row, a tie of two different edges between an edited file and
another one). The oracle is ``tools/incremental_fuzz.py``: updates compared with a fresh copy's scan.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter
from pathlib import Path

LEDGER_VERSION = 1
LEDGER_DIR = "incremental"
LEDGER_FILE = "ledger.json"
EXTRACTION_FILE = "extraction.json"
ENV = "VERINODA_INCREMENTAL"
# languages whose cross-file passes the closure and the oracle fuzz (tests/test_incremental.py) cover
LANGUAGES = frozenset({".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".go", ".java",
                       ".md", ".markdown"})
MAX_BATCH = 400          # files; a larger batch costs about what the full build costs
MAX_BATCH_SHARE = 0.25   # ... or a larger share of the corpus
# names a changed file may have although it is not in the graph corpus: the extractor reads none of them
_INERT_SUFFIXES = frozenset({".txt", ".csv", ".log", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".lock",
                             ".sqlite", ".db", ".bin", ".zip", ".gz"})
_TOKEN = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_FILE_TOKEN_CAP = 2_000_000  # bytes read per file for its tokens; a larger file counts as naming everything
# blake2b-64 of the vendored files whose build_from_json/to_json build_rows reproduces; others fall back
PINNED = {"build.py": "7c9cdbd14158bb11", "export.py": "f2423c4e0c378860"}
_IMPORT_RELATIONS = frozenset({"imports", "imports_from", "re_exports"})
_BASE_RELATIONS = frozenset({"inherits", "implements", "extends", "mixes_in"})


# --------------------------------------------------------------------------------------------- switch

def enabled(repo: Path) -> bool:
    """The switch: ``VERINODA_INCREMENTAL`` (1/true/yes/on; 0/false/no/off) wins over the config key
    ``index.incremental``; off by default."""
    env = os.environ.get(ENV)
    if env is not None and env.strip():
        return env.strip().lower() in ("1", "true", "yes", "on", "verify")
    try:
        from verinoda.paths import load_config

        return bool((load_config(Path(repo)).get("index") or {}).get("incremental"))
    except Exception:  # noqa: BLE001 - an unreadable config keeps the switch off
        return False


def verify_mode() -> bool:
    """``VERINODA_INCREMENTAL=verify``: after a patch, the vendored builder makes the graph from the same
    extraction and the two are compared (a difference falls back to the full build). For tests and the
    fuzz tool; it costs what build_from_json costs."""
    return (os.environ.get(ENV) or "").strip().lower() == "verify"


def ledger_dir(repo: Path) -> Path:
    from verinoda.paths import index_dir

    return index_dir(Path(repo)) / LEDGER_DIR


def pinned_now() -> dict[str, str]:
    """blake2b-64 of the vendored files whose code :func:`build_rows` reproduces (build_from_json and
    to_json; the whole file, line endings folded)."""
    base = Path(__file__).resolve().parent / "project_index"
    out = {}
    for name in ("build.py", "export.py"):
        try:
            data = (base / name).read_bytes().replace(b"\r\n", b"\n")
        except OSError:
            data = b""
        out[name] = hashlib.blake2b(data, digest_size=8).hexdigest()
    return out


# --------------------------------------------------------------------------------------------- capture

class Capture:
    """During a full build: keep what ``detect()`` and the corpus ``extract()`` returned (the extraction as a
    JSON text, taken before the reconcile changes it)."""

    def __init__(self, repo: Path, *, active: bool):
        self.repo, self.active = Path(repo).resolve(), active
        self.detected = self.paths = self.text = None
        self.pre: dict[str, list[str]] = {}  # each file's own extraction: its ids that are not path-derived
        self.recording = False
        self.seconds = 0.0
        self._undo: list = []

    def __enter__(self):
        if not self.active:
            return self
        from verinoda.project_index import detect as dt
        from verinoda.project_index import extract as em

        real_extract, real_detect = em.extract, dt.detect
        real_load, real_par, real_seq = em.load_cached, em._extract_parallel, em._extract_sequential

        def load_cached(path, *a, **kw):
            r = real_load(path, *a, **kw)
            if self.recording and isinstance(r, dict):
                self.pre[str(path)] = pre_ids(r, self.repo, path)
            return r

        def seen(work, per_file):
            if self.recording:
                for i, path in work:
                    if per_file[i] is not None:
                        self.pre[str(path)] = pre_ids(per_file[i], self.repo, path)

        def extract_parallel(work, per_file, *a, **kw):
            ok = real_par(work, per_file, *a, **kw)
            seen(work, per_file)
            return ok

        def extract_sequential(work, per_file, *a, **kw):
            out = real_seq(work, per_file, *a, **kw)
            seen(work, per_file)
            return out

        def extract(paths, *a, **kw):
            first = self.text is None and not kw.get("resolution_context_nodes")
            self.recording = first
            try:
                res = real_extract(paths, *a, **kw)
            finally:
                self.recording = False
            if first and isinstance(res, dict):
                t = time.monotonic()
                try:
                    self.paths = [str(p) for p in paths]
                    self.text = json.dumps({"nodes": res.get("nodes") or [], "edges": res.get("edges") or [],
                                            "hyperedges": res.get("hyperedges") or [],
                                            "failed_sources": res.get("failed_sources") or []},
                                           ensure_ascii=False)
                except (TypeError, ValueError):
                    self.text = None
                self.seconds += time.monotonic() - t
            return res

        def detect(*a, **kw):
            res = real_detect(*a, **kw)
            if self.detected is None:
                self.detected = res
            return res

        for mod, name, fn in ((em, "extract", extract), (dt, "detect", detect), (em, "load_cached", load_cached),
                              (em, "_extract_parallel", extract_parallel),
                              (em, "_extract_sequential", extract_sequential)):
            self._undo.append((mod, name, getattr(mod, name)))
            setattr(mod, name, fn)
        return self

    def __exit__(self, *a):
        while self._undo:
            mod, name, fn = self._undo.pop()
            setattr(mod, name, fn)
        return False


def pre_ids(result: dict, repo: Path, path) -> list[str]:
    """The ids of a file's own extraction (before the cross-file passes) that are not derived from its path,
    ``n:`` for its nodes and ``t:`` for its edges' ends: a type named by package (Go ``store_store``), a stub
    for a name it references (Java ``glowmod``). Two files that make the same such node are salted apart by
    the corpus pass (``_disambiguate_colliding_node_ids``), and a stub node one file makes can be merged
    into a definition (``_rewire_unique_stub_nodes``) taking the edges of every file that ends on that id
    along: a file that starts or stops making one changes the other files' rows."""
    from verinoda.project_index.extractors.base import _file_stem
    from verinoda.project_index.ids import make_id

    try:
        rel = Path(path).resolve().relative_to(repo)
    except (ValueError, OSError):
        rel = Path(path)
    stem = make_id(_file_stem(rel))
    out = set()
    for n in result.get("nodes") or []:
        nid = n.get("id") if isinstance(n, dict) else None
        if isinstance(nid, str) and nid and stem not in nid:
            out.add("n:" + nid)
    for e in result.get("edges") or []:
        for k in ("source", "target"):
            x = e.get(k) if isinstance(e, dict) else None
            if isinstance(x, str) and x and stem not in x:
                out.add("t:" + x)
        tgt = e.get("target") if isinstance(e, dict) else None
        if e.get("relation") in _BASE_RELATIONS and isinstance(tgt, str) and tgt and stem not in tgt:
            out.add("i:" + tgt)  # a base named by a shared id: the type passes treat that stub apart
    return sorted(out)


# --------------------------------------------------------------------------------------------- tokens

def file_tokens(p: Path) -> set[str] | None:
    """The identifier tokens of a file, folded to lower case (None: unreadable or too large - it names
    everything)."""
    try:
        if p.stat().st_size > _FILE_TOKEN_CAP:
            return None
        text = p.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None
    return {t.lower() for t in _TOKEN.findall(text)}


def _norm_label(label) -> str:
    return str(label or "").strip("()").lstrip(".").lower()


# --------------------------------------------------------------------------------------------- corpus walk

def _skipped_names() -> set[str]:
    from verinoda.project_index.detect import _SKIP_DIRS

    return {d for d in _SKIP_DIRS if "*" not in d} | {".verinoda", ".git"}


def _listing(d: Path, skip: set[str]) -> list[str]:
    """A directory's entries without the ones detect() never reads (``__pycache__`` appearing beside a module
    after a test run changes no corpus)."""
    return sorted(n for n in os.listdir(d) if n not in skip and not n.endswith(".egg-info"))


def _walk_dirs(repo: Path, ignored) -> dict[str, list]:
    """Every directory detect() could read under ``repo``: {rel: [mtime_ns, sorted entry names]}. A directory
    detect skips by name or by a live ignore rule is not entered (a change below it changes no corpus)."""
    skip = _skipped_names()
    out: dict[str, list] = {}
    stack = [repo]
    while stack:
        d = stack.pop()
        try:
            st = d.stat()
            entries = _listing(d, skip)
        except OSError:
            continue
        rel = "." if d == repo else d.relative_to(repo).as_posix()
        out[rel] = [st.st_mtime_ns, entries]
        for name in entries:
            p = d / name
            try:
                if not p.is_dir() or p.is_symlink():
                    continue
            except OSError:
                continue
            try:
                if ignored is not None and ignored(p):
                    continue
            except Exception:  # noqa: BLE001 - when in doubt, look inside
                pass
            stack.append(p)
    return out


def _dirs_unchanged(repo: Path, dirs: dict[str, list]) -> str | None:
    """None when every recorded directory still has the entries it had (stat first, a listing only when the
    modification time moved); else the first directory that differs."""
    skip = _skipped_names()
    for rel, (mtime, entries) in dirs.items():
        d = repo if rel == "." else repo / rel
        try:
            st = d.stat()
        except OSError:
            return rel
        if st.st_mtime_ns == mtime:
            continue
        try:
            if _listing(d, skip) != entries:
                return rel
        except OSError:
            return rel
    return None


def _build_config(out: Path) -> dict:
    from verinoda.project_index.watch import _read_build_excludes, _read_build_gitignore

    return {"excludes": _read_build_excludes(out), "gitignore": bool(_read_build_gitignore(out))}


# --------------------------------------------------------------------------------------------- ledger

def _rel(repo: Path, p: str | Path) -> str | None:
    try:
        return Path(p).resolve().relative_to(repo).as_posix()
    except (ValueError, OSError):
        return None


def _cache_key(path: Path, repo: Path) -> str | None:
    try:
        from verinoda.project_index.cache import file_hash

        return file_hash(path, repo, cache_root=repo)
    except Exception:  # noqa: BLE001
        return None


def record(repo: Path, cap: Capture, *, stamp: str | None = None) -> dict:
    """Write the ledger of a full build that just ended (``cap`` saw its detect and extract)."""
    from verinoda.paths import graph_path, index_dir

    t0 = time.monotonic()
    repo = Path(repo).resolve()
    d = ledger_dir(repo)
    if cap.text is None or cap.paths is None or cap.detected is None:
        drop(repo)
        return {"recorded": False, "why": "the build's extraction was not seen"}
    gp = graph_path(repo)
    try:
        graph_bytes = gp.read_bytes()
    except OSError:
        drop(repo)
        return {"recorded": False, "why": "no graph.json"}
    from verinoda.project_index.detect import ignored_predicate

    out = index_dir(repo)
    cfg = _build_config(out)
    try:
        from verinoda.selffiles import ignore_patterns as _own_patterns

        excludes = cfg["excludes"] + [p for p in _own_patterns(repo) if p not in cfg["excludes"]]
        ignored = ignored_predicate(repo, extra_excludes=excludes or None, gitignore=cfg["gitignore"])
    except Exception:  # noqa: BLE001 - walk everything
        ignored = None
    paths = []
    for p in cap.paths:
        r = _rel(repo, p)
        if r is None:
            drop(repo)
            return {"recorded": False, "why": f"a corpus file outside the repository: {p}"}
        paths.append(r)
    kinds: dict[str, str] = {}
    for kind, flist in (cap.detected.get("files") or {}).items():
        for f in flist:
            r = _rel(repo, f)
            if r is not None:
                kinds[r] = kind
    files = {}
    tokens = {}
    for r in paths:
        p = repo / r
        toks = file_tokens(p)
        tokens[r] = None if toks is None else " ".join(sorted(toks))
        files[r] = {"k": kinds.get(r), "h": _cache_key(p, repo)}
    pre: dict[str, list[str]] = {}
    for raw, ids in cap.pre.items():
        r = _rel(repo, raw)
        if r is not None and r in files:
            pre[r] = ids
    from verinoda import buildlock

    ledger = {"version": LEDGER_VERSION, "stamp": stamp or buildlock.extraction_stamp(repo),
              "pinned": pinned_now(), "config": cfg,
              "graph_sha": hashlib.blake2b(graph_bytes, digest_size=16).hexdigest(),
              "paths": paths, "files": files, "dirs": _walk_dirs(repo, ignored),
              "corpus": sorted(kinds), "tokens": tokens, "pre": pre}
    d.mkdir(parents=True, exist_ok=True)
    _write_text(d / EXTRACTION_FILE, cap.text)
    _write_text(d / LEDGER_FILE, json.dumps(ledger, ensure_ascii=False))
    return {"recorded": True, "files": len(paths), "seconds": round(time.monotonic() - t0 + cap.seconds, 3)}


def drop(repo: Path) -> None:
    """Forget the ledger (a build this module did not see rewrote graph.json)."""
    d = ledger_dir(Path(repo))
    for name in (LEDGER_FILE, EXTRACTION_FILE):
        try:
            (d / name).unlink()
        except OSError:
            pass


def _write_text(p: Path, text: str) -> None:
    import tempfile

    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------------------------- graph rows

class Unsupported(Exception):
    """The patched extraction has something :func:`build_rows` does not model: fall back."""


_FILE_TYPES = frozenset({"code", "document", "paper", "image", "rationale", "concept"})


def _norm_sf(p):
    """``build._norm_source_file(p, None)``: separators only (no root, nothing made relative)."""
    return p.replace("\\", "/") if p and isinstance(p, str) else p


def _node_attrs(n: dict, synonyms: dict, fold) -> dict:
    n = dict(n)
    fold(n)
    if n.get("file_type") in (None, ""):
        n["file_type"] = "concept"
    ft = n.get("file_type", "")
    if ft and ft not in _FILE_TYPES:
        n["file_type"] = synonyms.get(ft, "concept")
    if "source_file" in n:
        n["source_file"] = _norm_sf(n["source_file"])
    if "definition_file" in n:
        n["definition_file"] = _norm_sf(n["definition_file"])
    n.pop("id", None)
    return n


def build_rows(extraction: dict) -> tuple[dict[str, dict], list[dict]]:
    """``build_from_json`` (undirected, no root) then the node/link rows ``to_json`` writes, community fields
    left out, for an extraction of AST nodes only: ``({id: node row}, [link rows])``. Raises
    :class:`Unsupported` for what it does not model."""
    from verinoda.project_index.build import (
        _EDGE_LANG_FAMILY,
        _EXTERNAL_STUB_RELATIONS,
        _FILE_TYPE_SYNONYMS,
        _GENERIC_RELATIONS,
        _doc_twin_remap,
        _file_label_reassignments,
        _fold_edge_aliases,
        _fold_node_aliases,
        _old_file_stems,
    )
    from verinoda.project_index.export import _CONFIDENCE_SCORE_DEFAULTS, _strip_diacritics
    from verinoda.project_index.extractors.base import _file_stem
    from verinoda.project_index.ids import make_id, normalize_id
    from verinoda.project_index.paths import is_absolute_any_platform as _is_abs

    import math

    if extraction.get("hyperedges"):
        raise Unsupported("hyperedges")
    raw_nodes = extraction.get("nodes") or []
    attrs: dict[str, dict] = {}
    for n in raw_nodes:
        if not isinstance(n, dict) or not isinstance(n.get("id"), str):
            raise Unsupported("a node without a string id")
        if n.get("_origin") != "ast":
            raise Unsupported("a node that is not AST output")
        if "source" in n and "source_file" not in n:
            raise Unsupported("a legacy node")
        if n["id"] in attrs:  # G.add_node on a node that exists: its attributes updated, in row order
            attrs[n["id"]].update(_node_attrs(n, _FILE_TYPE_SYNONYMS, _fold_node_aliases))
        else:
            attrs[n["id"]] = _node_attrs(n, _FILE_TYPE_SYNONYMS, _fold_node_aliases)
    if _doc_twin_remap(raw_nodes):
        raise Unsupported("a document twin")
    node_set = set(attrs)
    norm_to_id: dict[str, str] = {}
    norm_amb: set[str] = set()
    normed: dict[str, str] = {}
    for nid in node_set:
        k = normed[nid] = normalize_id(nid)
        if k in norm_to_id and norm_to_id[k] != nid:
            norm_amb.add(k)
        norm_to_id[k] = nid
    stems: dict[str, tuple] = {}
    alias: dict[str, set[str]] = {}
    for nid, a in attrs.items():
        sf = a.get("source_file")
        if not sf:
            continue
        sf = str(sf)
        st = stems.get(sf)
        if st is None:
            if _is_abs(sf):
                st = stems[sf] = ()
            else:
                rel = Path(sf)
                st = stems[sf] = (make_id(_file_stem(rel)), rel.name, _old_file_stems(rel))
        if not st:
            continue
        new_stem, name, olds = st
        nn = normed[nid]
        if str(a.get("label", "")) == name:
            suffix = ""
        else:
            suffix = nn[len(new_stem):] if nn.startswith(new_stem) else ""
        for old_stem in olds:
            if old_stem == new_stem:
                continue
            al = old_stem + suffix
            alias.setdefault(normalize_id(al), set()).add(nid)
            alias.setdefault(al, set()).add(nid)
    for key, cands in alias.items():
        if len(cands) == 1 and key not in norm_to_id:
            norm_to_id[key] = next(iter(cands))

    def resolve(x):
        if x in node_set:
            return x
        k = normalize_id(x)
        if k in norm_amb:
            raise Unsupported(f"an edge end {x} that normalizes to two ids")
        return norm_to_id.get(k, x)

    edges = list(extraction.get("edges") or [])
    for i, e in enumerate(edges):
        if not isinstance(e, dict):
            raise Unsupported("an edge that is not an object")
        conf = e.get("confidence")
        if (not e.get("relation") and "type" in e) or (isinstance(conf, (int, float)) and not isinstance(conf, bool)) \
                or (not conf and e.get("confidence_score") is not None) or "from" in e or "to" in e:
            e = edges[i] = dict(e)  # the legacy shapes, folded on a copy (the input is not changed)
            _fold_edge_aliases(e)
            if "source" not in e and "from" in e:
                e["source"] = e["from"]
            if "target" not in e and "to" in e:
                e["target"] = e["to"]
    order = sorted(range(len(edges)), key=lambda i: (str(edges[i].get("source", "")), str(edges[i].get("target", "")),
                                                     str(edges[i].get("relation", ""))))
    pairs: dict[frozenset, dict] = {}
    stubs: set[str] = set()
    ext_of: dict = {}

    def node_sf(x):
        if x in attrs:
            return attrs[x].get("source_file")
        return ""  # an external stub

    def ext(sf):
        e_ = ext_of.get(sf)
        if e_ is None:
            e_ = ext_of[sf] = Path(sf or "").suffix.lower()
        return e_

    for i in order:
        edge = edges[i]
        if "source" not in edge or "target" not in edge:
            continue
        src, tgt = edge["source"], edge["target"]
        if not isinstance(src, str) or not isinstance(tgt, str):
            raise Unsupported("an edge end that is not a string")
        src, tgt = resolve(src), resolve(tgt)
        src_in = src in node_set or src in stubs
        tgt_in = tgt in node_set or tgt in stubs
        if not (src_in and tgt_in):
            if edge.get("relation") in _EXTERNAL_STUB_RELATIONS and src_in:
                stubs.add(tgt)
            else:
                continue
        a = {k: v for k, v in edge.items() if k not in ("source", "target", "target_file", "local_alias")}
        for key in ("weight", "confidence_score"):
            if key in a:
                try:
                    val = float(a[key])
                except (TypeError, ValueError):
                    val = 1.0
                if not math.isfinite(val) or val < 0:
                    val = 1.0
                a[key] = val
        if not a.get("source_file"):
            a["source_file"] = node_sf(src) or node_sf(tgt) or ""
        if "source_file" in a:
            a["source_file"] = _norm_sf(a["source_file"])
        if a.get("definition_file"):
            a["definition_file"] = _norm_sf(a["definition_file"])
        rel = a.get("relation")
        if rel in ("calls", "imports", "imports_from", "references"):
            src_ext = ext(node_sf(src))
            tgt_ext = ext(node_sf(tgt))
            sfam, tfam = _EDGE_LANG_FAMILY.get(src_ext), _EDGE_LANG_FAMILY.get(tgt_ext)
            if rel == "calls":
                if a.get("confidence") == "INFERRED" and src_ext and tgt_ext and sfam != tfam:
                    continue
            elif sfam is not None and tfam is not None and sfam != tfam:
                continue
        if src == tgt and rel in ("imports", "imports_from", "re_exports"):
            continue
        a["_src"], a["_tgt"] = src, tgt
        key = frozenset((src, tgt))
        cur = pairs.get(key)
        if cur is not None:
            if cur.get("relation") == a.get("relation") and cur.get("_src") == tgt and cur.get("_tgt") == src:
                continue
            if rel in _GENERIC_RELATIONS and cur.get("relation") is not None \
                    and cur.get("relation") not in _GENERIC_RELATIONS:
                continue
            cur.update(a)
        else:
            pairs[key] = a
    for t in stubs:
        attrs[t] = {"label": t, "file_type": "concept", "type": "external", "external": True, "source_file": ""}
    items = [(nid, a.get("label"), a.get("source_file")) for nid, a in attrs.items()]
    for nid, label in _file_label_reassignments(items).items():
        attrs[nid]["label"] = label
    nodes: dict[str, dict] = {}
    for nid, a in attrs.items():
        row = {"id": nid, **a}
        lab = row.get("label", "")
        row["norm_label"] = lab.lower() if isinstance(lab, str) and lab.isascii() else _strip_diacritics(lab).lower()
        nodes[nid] = row
    links = []
    for a in pairs.values():
        row = dict(a)
        if "confidence_score" not in row:
            row["confidence_score"] = _CONFIDENCE_SCORE_DEFAULTS.get(row.get("confidence", "EXTRACTED"), 1.0)
        src, tgt = row.pop("_src"), row.pop("_tgt")
        links.append({"source": src, "target": tgt, **row})
    return nodes, links


def _canonical(item: dict, lead: tuple[str, ...]) -> dict:
    leading = [k for k in lead if k in item]
    return {k: item[k] for k in (*leading, *sorted(k for k in item if k not in leading))}


# --------------------------------------------------------------------------------------------- the update

class _Fallback(Exception):
    pass


def _load_ledger(repo: Path) -> tuple[dict, dict]:
    d = ledger_dir(repo)
    try:
        ledger = json.loads((d / LEDGER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise _Fallback("no ledger of the last full build (it is written by a full build with the switch on)")
    if not isinstance(ledger, dict) or ledger.get("version") != LEDGER_VERSION:
        raise _Fallback("a ledger of another version")
    try:
        extraction = json.loads((d / EXTRACTION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise _Fallback("the ledger's extraction is missing")
    return ledger, extraction


def _closure_inputs(repo: Path, rel: str, cache_root: Path) -> dict:
    """What a file's own (pre-resolution) extraction references: stub labels, raw call names, import
    targets. Through the AST cache (the same entry the batch's extract() then reads)."""
    from verinoda.project_index import extract as em

    p = repo / rel
    try:
        _, r = em._extract_single_file((0, str(p), str(repo), str(cache_root)))
    except Exception as exc:  # noqa: BLE001
        raise _Fallback(f"{rel} could not be extracted alone ({type(exc).__name__})")
    if not isinstance(r, dict) or r.get("error"):
        raise _Fallback(f"{rel} did not extract")
    own = str(p)
    labels: set[str] = set()
    targets: set[str] = set()
    defs: set[tuple] = set()
    for n in r.get("nodes") or []:
        sf = n.get("source_file")
        if not sf:
            labels.add(_norm_label(n.get("label")))
        elif n.get("file_type") != "rationale" and _same_file(sf, own, repo):
            defs.add(_def_entry(n))
        else:
            labels.add(_norm_label(n.get("label")))
    # Call names are not followed: the shared call pass and the language resolvers read the whole corpus
    # through the resolution context, so their ambiguity counts are a full build's without the definers here.
    for e in r.get("edges") or []:
        tf = e.get("target_file")
        if tf:
            t = _rel(repo, tf)
            if t:
                targets.add(t)
        if e.get("relation") in _IMPORT_RELATIONS or e.get("relation") in ("inherits", "implements", "extends"):
            tgt = str(e.get("target") or "")
            parts = tgt.split("_")
            for i in range(len(parts)):
                labels.add("_".join(parts[i:]))
    labels.discard("")
    return {"labels": labels, "targets": targets, "defs": defs, "pre": set(pre_ids(r, repo, p))}


def _same_file(sf: str, own: str, repo: Path) -> bool:
    try:
        a = Path(sf)
        if not a.is_absolute():
            a = repo / a
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(own))
    except (OSError, ValueError):
        return False


def _def_entry(n: dict) -> tuple:
    """What other files can see of a definition: everything but its id, place and file."""
    d = {k: v for k, v in n.items() if k not in ("id", "source_file", "source_location", "_origin",
                                                 "origin_file", "definition_file")}
    return (_norm_label(n.get("label")), json.dumps(d, sort_keys=True, default=str))


def _post_defs(rows: list[dict]) -> set[tuple]:
    return {_def_entry(n) for n in rows if n.get("source_file") and n.get("file_type") != "rationale"}


def _import_rows(edges: list[dict]) -> set[tuple]:
    return {(e.get("relation"), e.get("target")) for e in edges if e.get("relation") in _IMPORT_RELATIONS}


def attempt(repo: Path, diff: dict) -> dict:
    """Try the update proportional to the change. Returns ``{"used": True, ...stats}`` after graph.json and
    the ledger were rewritten, or ``{"used": False, "reason": ...}`` having changed nothing."""
    t0 = time.monotonic()
    repo = Path(repo).resolve()
    try:
        res = _attempt(repo, diff)
    except _Fallback as fb:
        return {"used": False, "reason": str(fb), "seconds": round(time.monotonic() - t0, 3)}
    except Unsupported as un:
        return {"used": False, "reason": f"the patched extraction has {un}", "seconds": round(time.monotonic() - t0, 3)}
    except Exception as exc:  # noqa: BLE001 - never a broken graph: the full build runs
        import traceback

        where = traceback.extract_tb(exc.__traceback__)[-1]
        return {"used": False, "reason": f"error ({type(exc).__name__}: {exc} at {Path(where.filename).name}:"
                                         f"{where.lineno})"[:300],
                "seconds": round(time.monotonic() - t0, 3)}
    res["seconds"] = round(time.monotonic() - t0, 3)
    return res


def _attempt(repo: Path, diff: dict) -> dict:
    from verinoda import buildlock
    from verinoda.paths import graph_path, index_dir

    times: dict[str, float] = {}
    tick = [time.monotonic()]

    def lap(name: str) -> None:
        now = time.monotonic()
        times[name] = round(now - tick[0], 3)
        tick[0] = now

    if diff.get("added") or diff.get("removed"):
        raise _Fallback("files were added or removed")
    modified = sorted(set(diff.get("modified") or []))
    if not modified:
        raise _Fallback("no file changed")
    ledger, X = _load_ledger(repo)
    if ledger.get("stamp") != buildlock.extraction_stamp(repo):
        raise _Fallback("the extraction stamp is not the ledger's")
    if ledger.get("pinned") != pinned_now() or pinned_now() != PINNED:
        raise _Fallback("the vendored builder is not the version this was checked against")
    out = index_dir(repo)
    if _build_config(out) != ledger.get("config"):
        raise _Fallback("the build configuration changed")
    gp = graph_path(repo)
    try:
        graph_bytes = gp.read_bytes()
    except OSError:
        raise _Fallback("no graph.json")
    if hashlib.blake2b(graph_bytes, digest_size=16).hexdigest() != ledger.get("graph_sha"):
        raise _Fallback("graph.json is not the one the ledger describes")
    paths: list[str] = ledger["paths"]
    files: dict = ledger["files"]
    order = {r: i for i, r in enumerate(paths)}
    corpus = set(ledger.get("corpus") or ())
    M: list[str] = []
    for f in modified:
        if f in order:
            M.append(f)
            continue
        if f in corpus:
            raise _Fallback(f"{f} is in the corpus but was not extracted")
        if Path(f).suffix.lower() not in _INERT_SUFFIXES:
            raise _Fallback(f"{f} changed and is not a graph file (the extractor may read it)")
    if not M:
        raise _Fallback("no graph file changed")
    for f in M:
        if Path(f).suffix.lower() not in LANGUAGES:
            raise _Fallback(f"{f}: a language the incremental closure is not checked for")
    moved = _dirs_unchanged(repo, ledger.get("dirs") or {})
    if moved is not None:
        raise _Fallback(f"the files of {moved} changed (added, removed or renamed)")
    from verinoda.project_index.detect import classify_file

    for f in M:
        kind = classify_file(repo / f)
        if (kind.value if kind is not None else None) != files[f].get("k"):
            raise _Fallback(f"{f} is classified differently now")
    lap("checks")

    # the extraction's rows by file
    nodes_by_file: dict[str, list[dict]] = {}
    edges_by_file: dict[str, list[dict]] = {}
    for n in X["nodes"]:
        nodes_by_file.setdefault(n.get("source_file") or "", []).append(n)
    for e in X["edges"]:
        edges_by_file.setdefault(e.get("source_file") or "", []).append(e)
    node_file = {n["id"]: (n.get("source_file") or "") for n in X["nodes"] if isinstance(n.get("id"), str)}
    definers: dict[str, set[str]] = {}
    for sf, rows in nodes_by_file.items():
        if not sf:
            continue
        for n in rows:
            if n.get("file_type") == "rationale":
                continue
            lab = _norm_label(n.get("label"))
            if lab:
                definers.setdefault(lab, set()).add(sf)
    tokens = ledger["tokens"]
    token_sets: dict[str, set[str] | None] = {}

    def toks(f: str):
        if f not in token_sets:
            t = tokens.get(f)
            token_sets[f] = None if t is None else set(t.split())
        return token_sets[f]

    def users(keys: set[str]) -> set[str]:
        if not keys:
            return set()
        out_ = set()
        for f in paths:
            t = toks(f)
            if t is None or not keys.isdisjoint(t):
                out_.add(f)
        return out_

    from verinoda.project_index.extractors.base import _file_stem
    from verinoda.project_index.ids import make_id

    stem_of = {}
    by_stem: dict[str, set[str]] = {}
    for f in paths:
        s = make_id(_file_stem(Path(f)))
        stem_of[f] = s
        by_stem.setdefault(s, set()).add(f)
        # a file "defines" its module: its id stem and its bare name (an import names it so)
        definers.setdefault(s, set()).add(f)
        definers.setdefault(Path(f).stem.lower(), set()).add(f)

    def importers(f: str) -> set[str]:
        ids = {n["id"] for n in nodes_by_file.get(f, [])}
        return {sf for sf, rows in edges_by_file.items() if sf and sf != f
                and any(e.get("target") in ids for e in rows)}

    pre_by_file: dict[str, list[str]] = ledger.get("pre") or {}
    by_pre: dict[str, set[str]] = {}
    for f, ids in pre_by_file.items():
        for i in ids:
            by_pre.setdefault(i, set()).add(f)

    def colliders(ids) -> set[str]:
        """The files whose own extraction makes a node with one of these ids or ends an edge on one."""
        out_ = set()
        for i in ids:
            bare = i[2:]
            out_ |= by_pre.get("n:" + bare, set()) | by_pre.get("t:" + bare, set())
        return out_

    def makers(ids, own: str) -> set[str]:
        """Files whose own extraction makes a node with one of these ids, enough of them for the corpus pass to
        treat the id as it does in a full build: an id two or more files make is salted with each file's path
        (``_disambiguate_colliding_node_ids``), so the first two other makers in the build's order stand for all
        of them (a ``Path`` stub made by 500 Python files does not bring 500 files into the batch)."""
        out_ = set()
        for i in ids:
            got = by_pre.get("n:" + i[2:], set()) - {own}
            out_ |= set(sorted(got, key=lambda f: order.get(f, 0))[:2]) if len(got) > 2 else got
        return out_

    cache_root = repo
    pre = {f: _closure_inputs(repo, f, cache_root) for f in M}
    lap("own_extraction")
    # A: what an edited file's changed definitions or imports can change
    A = set(M)
    for f in M:
        old_defs = _post_defs(nodes_by_file.get(f, []))
        changed = old_defs ^ pre[f]["defs"]
        A |= users({lab for lab, _ in changed if lab})
        A |= by_stem.get(stem_of[f], set())
        # a file that makes an id this one starts or stops making: the corpus pass salts both apart
        # (or starts or stops naming as a base: ``i:``, the Java type passes resolve such a stub differently)
        A |= colliders({i for i in set(pre_by_file.get(f) or ()) ^ pre[f]["pre"] if i[:2] in ("n:", "i:")})
    new_toks = {f: file_tokens(repo / f) for f in M}
    for f in M:
        old_t, new_t = toks(f), new_toks[f]
        if old_t is None or new_t is None:
            raise _Fallback(f"{f} is too large to read for names")
    A = _with_stem_siblings(A, by_stem, stem_of)

    why: Counter = Counter()  # what brought the most files into the batch (said when it is too large)

    def batch(A_: set[str]) -> set[str]:
        B_ = set(A_)
        why.clear()
        for f in A_:
            info = pre.get(f) or _closure_inputs(repo, f, cache_root)
            pre[f] = info
            for lab in info["labels"]:
                got = definers.get(lab, set())
                if len(got) > 5:
                    why[f"name {lab}"] = len(got)
                B_ |= got
            B_ |= {t for t in info["targets"] if t in order}
            got = makers(info["pre"], f)
            if len(got) > 5:
                why["ids it makes or ends on"] = len(got)
            B_ |= got
            # what the file imported before (its rows in the stored extraction)
            for e in edges_by_file.get(f, []):
                if e.get("relation") in _IMPORT_RELATIONS or e.get("relation") in ("inherits", "implements"):
                    sf = node_file.get(e.get("target"))
                    if sf:
                        B_.add(sf)
        return B_

    B = batch(A)
    lap("closure")
    cap = max(100, min(MAX_BATCH, int(len(paths) * MAX_BATCH_SHARE)))
    for _round in range(3):
        if len(B) > cap:
            top = ", ".join(f"{k} ({v})" for k, v in why.most_common(4))
            raise _Fallback(f"the batch has {len(B)} files (more than {cap})" + (f": {top}" if top else ""))
        for f in A:  # the rows taken from the batch are A's: their languages must be the checked ones
            if Path(f).suffix.lower() not in LANGUAGES:
                raise _Fallback(f"{f} would be extracted again: a language the closure is not checked for")
        new = _extract_batch(repo, B, order, X)
        # the definitions the batch made for A's files, against the ledger's: a change seen only now widens A
        grown = set(A)
        for f in A:
            changed = _post_defs([n for n in new["nodes"] if n.get("source_file") == f]) ^ \
                _post_defs(nodes_by_file.get(f, []))
            if f in M:
                grown |= users({lab for lab, _ in changed if lab})
            elif changed:
                raise _Fallback(f"{f} defines something else now although it did not change")
            if f in M and _import_rows([e for e in new["edges"] if e.get("source_file") == f]) != \
                    _import_rows(edges_by_file.get(f, [])):
                grown |= importers(f)
        grown = _with_stem_siblings(grown, by_stem, stem_of)
        if grown <= A:
            break
        A = grown
        B = batch(A)
    else:
        raise _Fallback("the affected set did not settle")
    lap("extract")
    def stub_owners(nid: str) -> set[str]:
        """The files whose own extraction makes this (stub) id, the edited ones as they are now."""
        got = {f for f in by_pre.get("n:" + nid, set()) if f not in M}
        return got | {f for f in M if "n:" + nid in pre[f]["pre"]}

    Xn = _splice(X, A, new, stub_owners)
    lap("splice")
    from verinoda.case_ids import split_case_collisions

    split_case_collisions(Xn["nodes"], Xn["edges"], repo)
    old = json.loads(graph_bytes)
    if old.get("directed"):
        raise _Fallback("a directed graph")
    if old.get("hyperedges") or (old.get("graph") or {}).get("hyperedges"):
        raise _Fallback("the graph has hyperedges")
    lap("load_graph")
    nodes, links = build_rows(Xn)
    lap("rows")
    data = _graph_data(repo, old, nodes, links)
    lap("communities")
    if verify_mode():
        diff_ = _verify(repo, Xn, data)
        if diff_:
            raise _Fallback(f"verify: the patched graph differs from the vendored build ({diff_})")
        lap("verify")
    from verinoda.portable_ids import strip_root_from_ids

    strip_root_from_ids(data["nodes"], data["links"], repo, data.get("hyperedges"))
    # as index.build's post-processing after a full build: the nodes of files that do not exist (an import of
    # a missing file) and of Verinoda's own files are dropped
    from verinoda.index import _drop_files, _missing_in_nodes, _own_in_nodes

    gone = set(_missing_in_nodes(repo, data["nodes"])) | set(_own_in_nodes(repo, data["nodes"]))
    if gone:
        _drop_files(data, gone)
    text = json.dumps(data, ensure_ascii=False) + "\n"
    _write_text(gp, text)
    lap("write_graph")
    _after_write(repo, out)
    # the ledger follows
    pre_store = ledger.setdefault("pre", {})
    for f in M:
        files[f] = {**files[f], "h": _cache_key(repo / f, repo)}
        tokens[f] = " ".join(sorted(new_toks[f]))
        pre_store[f] = sorted(pre[f]["pre"])
    ledger["graph_sha"] = hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()
    dirs = ledger.get("dirs") or {}
    for f in M:
        parent = Path(f).parent.as_posix()
        rel = "." if parent in ("", ".") else parent
        dd = repo if rel == "." else repo / rel
        if rel in dirs:
            try:
                dirs[rel] = [dd.stat().st_mtime_ns, _listing(dd, _skipped_names())]
            except OSError:
                pass
    d = ledger_dir(repo)
    _write_text(d / EXTRACTION_FILE, json.dumps(Xn, ensure_ascii=False))
    _write_text(d / LEDGER_FILE, json.dumps(ledger, ensure_ascii=False))
    lap("ledger")
    return {"used": True, "reason": None, "changed": M, "affected": sorted(A), "batch": len(B),
            "batch_files": sorted(B) if len(B) <= 40 else None,
            "nodes": len(data["nodes"]), "edges": len(data["links"]), "times": times}


def _with_stem_siblings(A: set[str], by_stem: dict, stem_of: dict) -> set[str]:
    """A and every file sharing an id stem with one of its files (``a.h``/``a.c``, ``a_b.py``/``a/b.py``): their
    ids are salted apart together."""
    A = set(A)
    for f in list(A):
        A |= by_stem.get(stem_of.get(f, ""), set())
    return A


def _context(X: dict, B: set[str]) -> tuple[list[dict], list[dict]]:
    """The read-only resolution context the vendored incremental path hands extract(): the AST nodes of every
    file outside the batch with their resolver markers, and their contains/method/inherits edges."""
    from verinoda.project_index.build import _is_ast_tier

    nodes, edges = [], []
    for node in X["nodes"]:
        sf = node.get("source_file")
        if not node.get("id") or not sf or sf in B or not _is_ast_tier(node):
            continue
        c = {"id": node["id"], "label": node.get("label"), "source_file": sf, "file_type": node.get("file_type"),
             "type": node.get("type")}
        for marker in ("_callable", "_callable_class", "_elixir_module", "_rust_impl_key", "_rust_declaration_count"):
            if node.get(marker):
                c[marker] = node[marker]
        md = node.get("metadata")
        if isinstance(md, dict):
            rb = {k: md[k] for k in ("ruby_resolution_schema", "ruby_method_kind", "ruby_lookup_unsafe",
                                     "ruby_reopened", "ruby_external_method_owners") if k in md}
            if rb:
                c["metadata"] = rb
        nodes.append(c)
    for e in X["edges"]:
        if e.get("relation") not in ("contains", "method", "inherits") or not _is_ast_tier(e):
            continue
        sf = e.get("source_file")
        if not sf or sf in B:
            continue
        edges.append({"source": e.get("source"), "target": e.get("target"), "relation": e.get("relation"),
                      "source_file": sf})
    return nodes, edges


def _extract_batch(repo: Path, B: set[str], order: dict[str, int], X: dict) -> dict:
    """The vendored extract() over the batch, in the full build's order, with the rest of the corpus as
    read-only resolution context; source paths made repository-relative as the rebuild makes them."""
    from verinoda.project_index import extract as em
    from verinoda.project_index.watch import _rebase_relative_source_files, _relativize_source_files

    ctx_nodes, ctx_edges = _context(X, B)
    batch = [repo / f for f in sorted(B, key=lambda f: order[f])]
    res = em.extract(batch, cache_root=repo, resolution_context_nodes=ctx_nodes or None,
                     resolution_context_edges=ctx_edges or None)
    if res.get("failed_sources"):
        raise _Fallback("a file of the batch failed to extract")
    _rebase_relative_source_files(res, repo, repo)
    _relativize_source_files(res, repo, scope=repo)
    return json.loads(json.dumps({"nodes": res.get("nodes") or [], "edges": res.get("edges") or []}))


def _splice(X: dict, A: set[str], new: dict, stub_owners=None) -> dict:
    """The stored extraction with the rows of A's files replaced by the batch's. A node without a file (a stub
    for a name no file defines) stays while an edge of a file outside A still ends on it, and comes from the
    batch when an edge of A's files does; the two must agree."""
    kept_edges = [e for e in X["edges"] if (e.get("source_file") or "") not in A]
    new_edges = [e for e in new["edges"] if (e.get("source_file") or "") in A]
    if any(not e.get("source_file") for e in new["edges"]):
        raise _Fallback("the batch made an edge without a file")
    ends_kept = {e.get(k) for e in kept_edges for k in ("source", "target")}
    ends_new = {e.get(k) for e in new_edges for k in ("source", "target")}
    ends_all_old = {e.get(k) for e in X["edges"] for k in ("source", "target")}
    ends_batch = {e.get(k) for e in new["edges"] for k in ("source", "target")}
    # the stored rows keep their order: two rows with one id are merged by the builder in that order
    nodes: list[dict] = []
    old_stubs: dict[str, list[dict]] = {}
    for n in X["nodes"]:
        sf = n.get("source_file") or ""
        if sf:
            if sf not in A:
                nodes.append(n)
            continue
        if n["id"] in ends_kept or (n["id"] not in ends_all_old and not _only_in(n["id"], A, stub_owners)):
            nodes.append(n)  # an edge outside A ends on it, or nothing does and a file outside A makes it
            old_stubs.setdefault(n["id"], []).append(n)
    added: list[dict] = []
    for n in new["nodes"]:
        sf = n.get("source_file") or ""
        if sf:
            if sf in A:
                added.append(n)
            continue
        if n["id"] not in ends_new and not (n["id"] not in ends_batch and _only_in(n["id"], A, stub_owners)):
            continue
        prev = old_stubs.get(n["id"])
        if prev is not None:
            if any(x != n for x in prev):
                raise _Fallback(f"the stub {n['id']} is described differently by the batch")
            continue
        if any(x["id"] == n["id"] and x != n for x in added if not x.get("source_file")):
            raise _Fallback(f"the stub {n['id']} is described twice by the batch")
        if not any(x["id"] == n["id"] for x in added if not x.get("source_file")):
            added.append(n)
    # an id an edited file's row shares with another row: where the full build puts it among them is not known
    # here, so only rows that say the same thing are allowed
    by_id: dict[str, list[dict]] = {}
    for n in nodes + added:
        by_id.setdefault(n["id"], []).append(n)
    for n in added:
        group = by_id[n["id"]]
        if len(group) > 1 and any(x != group[0] for x in group):
            files = sorted({x.get("source_file") or "" for x in group})
            raise _Fallback(f"the id {n['id']} is made by two rows ({', '.join(files)[:200]})")
    nodes += added
    # a tie of two edges with the same ends and relation, one from A and one from elsewhere: the full
    # build's order between them is not known here
    keyed: dict[tuple, dict] = {}
    for e in kept_edges:
        keyed.setdefault((e.get("source"), e.get("target"), e.get("relation")), e)
    for e in new_edges:
        other = keyed.get((e.get("source"), e.get("target"), e.get("relation")))
        if other is not None and (other.get("source_file") or "") not in A and other != e:
            raise _Fallback("an edited file and another one make two different edges with the same ends")
    return {"nodes": nodes, "edges": kept_edges + new_edges, "hyperedges": []}


def _only_in(nid: str, A: set[str], stub_owners) -> bool:
    """Is a stub no edge ends on made only by files of A (so A's rows decide whether it is there)?"""
    if stub_owners is None:
        return False
    owners = stub_owners(nid)
    return bool(owners) and owners <= A


def _graph_data(repo: Path, old: dict, nodes: dict[str, dict], links: list[dict]) -> dict:
    """graph.json from the rows: each node keeps the community (and its name) it had; a new one takes the
    community most nodes of its file have (none when its file had none)."""
    prev: dict[str, tuple] = {}
    by_file: dict[str, Counter] = {}
    names: dict = {}
    for n in old.get("nodes") or []:
        cid = n.get("community")
        prev[n["id"]] = (cid, n.get("community_name"), "community_name" in n)
        if cid is not None:
            by_file.setdefault(n.get("source_file") or "", Counter())[cid] += 1
            if "community_name" in n:
                names[cid] = n["community_name"]
    has_names = any(p[2] for p in prev.values())
    out_nodes = []
    for nid, row in nodes.items():
        if nid in prev:
            cid = prev[nid][0]
        else:
            c = by_file.get(row.get("source_file") or "")
            cid = c.most_common(1)[0][0] if c else None
        row = dict(row)
        row["community"] = cid
        if cid is not None and has_names:
            row["community_name"] = names.get(cid, f"Community {cid}")
        out_nodes.append(_canonical(row, ("id", "label")))
    out_links = [_canonical(r, ("source", "target", "relation")) for r in links]
    out_nodes.sort(key=lambda r: r["id"])
    out_links.sort(key=lambda r: (r["source"], r["target"], str(r.get("relation"))))
    data = {k: v for k, v in old.items() if k not in ("nodes", "links", "edges")}
    data["nodes"] = out_nodes
    data["links"] = out_links
    try:
        from verinoda.project_index.watch import _git_head

        commit = _git_head(cwd=repo)
        if commit:
            data["built_at_commit"] = commit
    except Exception:  # noqa: BLE001
        pass
    return data


def _verify(repo: Path, Xn: dict, data: dict) -> str | None:
    """The vendored builder on the same extraction, compared with the rows (community left out)."""
    import io
    import sys
    import tempfile
    from contextlib import redirect_stderr, redirect_stdout

    from verinoda.project_index.build import build_from_json
    from verinoda.project_index.export import to_json

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    try:
        import graph_equal
    except ImportError:
        return None
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        G = build_from_json(json.loads(json.dumps(Xn)))
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "g.json"
            to_json(G, {}, str(p), force=True, built_at_commit="x")
            ref = json.loads(p.read_text(encoding="utf-8"))
    res = graph_equal.compare(ref, data)
    if res["equal"]:
        return None
    return json.dumps(res["counts"]) + " " + json.dumps({k: res[k][:2] for k in (
        "nodes_only_in_a", "nodes_only_in_b", "edges_only_in_a", "edges_only_in_b")})[:600]


def _after_write(repo: Path, out: Path) -> None:
    """What a full build leaves beside graph.json and this one does not remake: the record of the "graph
    unchanged" keeper describes another graph now, and the graph page is stale."""
    from verinoda.index import REBUILD_RECORD

    try:
        (out / REBUILD_RECORD).unlink()
    except OSError:
        pass
