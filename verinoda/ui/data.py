"""What the notes-and-graph view shows, built from the project's own index (read-only).

Every symbol, source file, document section and data file of the project is a *note*: its
qualified name, signature and doc text, an excerpt of its code, and its links - calls and
callers, members, imports, references, inheritance, the data files it names by resource id
(``glow:wisp_death``) and those that name it, and the claims recorded about it. The *local
graph* is a note with its neighbours; the *global graph* is the project at file level.

Sources: ``graph.json`` (with Verinoda's own call passes, :func:`verinoda.index.load`),
``search.db`` (qualified names, signatures, doc text, data-file units and resource links)
and ``atlas.db`` (claims). Nothing is written: ``search.db`` is opened without syncing (an
index behind the graph is rebuilt in memory, and :meth:`Atlas.stats` says so) and
``atlas.db`` read-only. Only files inside the project are read or listed.

Each load is a :class:`Snapshot`: the graph, the search index and every cache built from
them. A request works on one snapshot from start to end, so a reload (``verinoda update`` in
another terminal changes ``graph.json`` or ``search.db``) never mixes two indexes.

Note ids are graph node ids; a data-file unit, which has no graph node, is ``data:<file>#<line>``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path, PurePosixPath

from verinoda import index
from verinoda.architecture_map import is_test_file

_is_test = lru_cache(maxsize=1 << 16)(is_test_file)  # asked for every link of every note
RACY_NS = 2_000_000_000  # a file written this close to now may change again within the same clock tick

MAX_CODE_LINES = 160        # lines of a symbol shown in its note
MAX_SNIPPET = 160           # characters of the line a link is written on
STRUCTURAL_SECTIONS = ("defined_in", "members")  # where a link's line is the definition itself
MAX_FILE_LINES = 80         # lines of a file shown in its note (its outline lists the rest)
MAX_SECTION_ITEMS = 60      # links per note section (the section says how many more)
MAX_LOCAL_NODES = 220       # nodes of a local graph
MAX_GLOBAL_NODES = 2500     # file nodes of the global graph (the best connected ones)
MAX_SEARCH = 40
MAX_QUERY_CHARS = 300       # a pasted token is cut here (the ranking's cost grows with it)
MAX_CACHED_FILES = 256
DATA_PREFIX = "data:"
HIDDEN_KINDS = ("external", "comment", "missing")   # not listed; in graphs only on request

# relation -> (section of the note it goes out of, section of the note it points at)
SECTIONS = {
    "calls": ("calls", "called_by"),
    "method": ("members", "defined_in"),
    "contains": ("members", "defined_in"),
    "imports": ("imports", "imported_by"),
    "imports_from": ("imports", "imported_by"),
    "references": ("references", "referenced_by"),
    "uses": ("references", "referenced_by"),
    "inherits": ("extends", "extended_by"),
    "implements": ("extends", "extended_by"),
}
SECTION_ORDER = ["defined_in", "members", "calls", "called_by", "extends", "extended_by", "imports",
                 "imported_by", "references", "referenced_by", "names_data", "named_by", "other_out",
                 "other_in", "claims"]
GRAPH_RELATIONS = ("calls", "method", "contains", "imports", "imports_from", "references", "uses", "inherits",
                   "implements")
MEMBER_RELATIONS = ("method", "contains")
CODE_SUFFIX_LANG = {".py": "python", ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".js": "js", ".jsx": "js",
                    ".mjs": "js", ".cjs": "js", ".ts": "ts", ".tsx": "ts", ".go": "go", ".rs": "rust", ".c": "c",
                    ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".cs": "csharp", ".rb": "ruby",
                    ".php": "php", ".swift": "swift", ".scala": "scala", ".lua": "lua", ".json": "json",
                    ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".md": "markdown", ".mcfunction": "mcfunction",
                    ".sh": "shell", ".sql": "sql", ".groovy": "groovy", ".gradle": "groovy"}


def _conf_rank(d: dict) -> int:
    return 0 if d.get("confidence") == "EXTRACTED" else 1


def _at(d: dict) -> str | None:
    loc = str(d.get("source_location") or "")
    f = d.get("source_file")
    if f and loc.startswith("L") and loc[1:].isdigit():
        return f"{f}:{loc[1:]}"
    return f or None


def _stat(p: Path) -> tuple | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _area(folder: str) -> str:
    """The part of the tree a file belongs to, for colouring the graph: its first two folders."""
    parts = [p for p in folder.split("/") if p and p != "."]
    return "/".join(parts[:2]) or "/"


def _group_names(nodes: list[dict]) -> list[dict]:
    """A name for each community shown: the folder most of its files are in, by its last two parts
    (the index's own community names come from one node's label, often an imported module such as
    ``pathlib``). Two communities with the same name are told apart by their best connected file."""
    folders: dict[int, Counter] = defaultdict(Counter)
    best: dict[int, dict] = {}
    for n in nodes:
        c = n["group"]
        if not isinstance(c, int):
            continue
        parts = [p for p in n["folder"].split("/") if p and p != "."]
        folders[c]["/".join(parts[-2:]) or "/"] += 1
        if c not in best or (n["degree"], n["file"]) > (best[c]["degree"], best[c]["file"]):
            best[c] = n
    names = {c: fc.most_common(1)[0][0] for c, fc in folders.items()}
    taken = Counter(names.values())
    return [{"id": c, "name": names[c] if taken[names[c]] == 1 else f"{names[c]} · {best[c]['title']}"}
            for c in sorted(names)]


class Atlas:
    """The view model of one project: hands out the current :class:`Snapshot` (thread-safe)."""

    def __init__(self, repo: Path | str):
        self.repo = Path(repo).resolve()
        self._lock = threading.Lock()
        self._snap: Snapshot | None = None

    def _key(self) -> tuple:
        from verinoda.paths import graph_path, search_db_path

        gs = _stat(graph_path(self.repo))
        if gs is None:
            raise FileNotFoundError(f"no index in {self.repo}: run `verinoda scan {self.repo}` first")
        return gs, _stat(search_db_path(self.repo))

    def snapshot(self) -> Snapshot:
        """The snapshot of the index as it is now (a new one after the graph or search index changed)."""
        with self._lock:
            key = self._key()
            if self._snap is None or self._snap.key != key:
                self._snap = Snapshot(self.repo, key)
            return self._snap

    def ensure(self) -> index.Graph:
        return self.snapshot().g

    @property
    def g(self) -> index.Graph | None:
        return self._snap.g if self._snap else None

    # the API, each call on one snapshot
    def stats(self) -> dict:
        return self.snapshot().stats()

    def tree(self) -> dict:
        return self.snapshot().tree()

    def search(self, q: str, limit: int = MAX_SEARCH) -> dict:
        return self.snapshot().search(q, limit)

    def note(self, nid: str) -> dict:
        return self.snapshot().note(nid)

    def local_graph(self, nid: str, depth: int = 1, relations: set[str] | None = None, **kw) -> dict:
        return self.snapshot().local_graph(nid, depth, relations, **kw)

    def global_graph(self, **kw) -> dict:
        return self.snapshot().global_graph(**kw)

    def user_notes(self) -> dict:
        return {"notes": self.snapshot().user_notes()}

    def delete_user_note(self, subject: str) -> dict:
        """Remove the note on ``subject``, whatever became of its code (a note whose symbol is gone)."""
        from verinoda import usernotes

        if not usernotes.delete(self.repo, subject):
            raise KeyError(f"no note of your own on {subject!r}")
        return {"user_note": None, "deleted": subject}

    def write_user_note(self, nid: str, text: str, *, keep: bool = False) -> dict:
        """Write, delete (empty ``text``) or keep (re-anchor) the note of your own on ``nid``.

        Refused (ValueError) when ``nid`` has no code in the project, and (LookupError) when its
        file changed since the index was built: the lines the index knows would be the wrong ones.
        """
        from verinoda import search_index, usernotes

        snap = self.snapshot()
        sub = snap.subject_of(nid)
        if sub is None:
            raise KeyError(f"no note of your own can be written on {nid!r}")
        subject, f, start, end = sub
        stale = bool(search_index.stale_files(snap.h, self.repo, [f]))
        if keep:
            found = usernotes.find(self.repo, subject)
            if found is None:
                raise KeyError(f"no note of your own on {nid!r}")
            pinned_by_lines = "text" in (found.anchor or {})
            if pinned_by_lines and stale:
                raise LookupError(f"{f} changed since the index was built: run `verinoda update`, then keep")
            saved = usernotes.keep(self.repo, found, span=(start, end), resolves=True)
        else:
            if text.strip() and stale:
                raise LookupError(f"{f} changed since the index was built: run `verinoda update`, then write")
            saved = usernotes.save(self.repo, subject, f, start, end, text)
        return {"user_note": usernotes.as_dict(self.repo, saved) if saved is not None else None}


class Snapshot:
    """One load of the index and everything derived from it; caches only ever describe this load."""

    def __init__(self, repo: Path, key: tuple):
        from verinoda import search_index

        self.repo, self.key = repo, key
        self._root = repo.resolve()
        self.g = index.load(repo)
        # no sync: a GET never rewrites search.db; an index behind the graph is built in memory
        self.h = search_index.open_for(self.g, sync=False)
        self._lock = threading.RLock()
        self._inside: dict[str, bool] = {}
        self._facts, self._data = self._read_units()
        self._titles: dict[str, str] = {}
        self._kinds: dict[str, str] = {}
        self._lines: dict[str, list[str]] = {}
        self._rows: list[tuple] | None = None
        self._global: dict[tuple, dict] = {}
        self._tree: dict | None = None
        self._file_notes: dict[str, str] | None = None
        self._dbf: dict[str, list] | None = None
        self._claim_cache: tuple | None = None
        self._subject_cache: dict[str, dict[str, str]] = {}
        self._fresh_files: dict[str, tuple] = {}
        self._refs = self._reference_roots()

    # -- files ---------------------------------------------------------------------------------
    def inside(self, f: str | None) -> bool:
        """Is ``f`` a relative path to a file inside the project (not through a link that leaves it)?"""
        if not f:
            return False
        ok = self._inside.get(f)
        if ok is None:
            p = PurePosixPath(f.replace("\\", "/"))
            ok = not p.is_absolute() and ".." not in p.parts and ":" not in (p.parts[0] if p.parts else "")
            if ok:
                try:
                    (self.repo / f).resolve().relative_to(self._root)
                except (ValueError, OSError):
                    ok = False
            self._inside[f] = ok
        return ok

    def line_at(self, at: str | None) -> str | None:
        """The text of ``file:line`` (stripped, at most :data:`MAX_SNIPPET` characters), or None."""
        if not at:
            return None
        f, _, ln = at.rpartition(":")
        if not ln.isdigit() or not self.inside(f) or not self._fresh(f):
            return None  # a file edited since the index: its line numbers point elsewhere now
        lines = self.read_lines(f)
        n = int(ln)
        if not 1 <= n <= len(lines):
            return None
        text = lines[n - 1].strip()
        return (text[:MAX_SNIPPET - 1] + "…" if len(text) > MAX_SNIPPET else text) or None

    def _fresh(self, f: str) -> bool:
        """Is ``f`` as the index saw it (so the index's line numbers point at the right lines)?"""
        from verinoda import search_index

        st = _stat(self._root / f)
        with self._lock:
            hit = self._fresh_files.get(f)
        if hit is not None and hit[0] == st:
            return hit[1]
        try:
            ok = not search_index.stale_files(self.h, self._root, [f])
        except Exception:  # noqa: BLE001 - unknown: do not show a line that may be the wrong one
            ok = False
        with self._lock:
            self._fresh_files[f] = (st, ok)
        return ok

    def read_lines(self, f: str) -> list[str]:
        """Lines as the graph counts them (``\\n`` only; a form feed or U+2028 is not a line break)."""
        if f in self._lines:
            return self._lines[f]
        lines: list[str] = []
        if self.inside(f):
            try:
                text = (self.repo / f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            lines = [ln.removesuffix("\r") for ln in text.split("\n")]
            if lines and lines[-1] == "":
                lines.pop()
        with self._lock:
            if len(self._lines) >= MAX_CACHED_FILES:
                self._lines.clear()
            self._lines[f] = lines
        return lines

    def _read_units(self) -> tuple[dict, dict]:
        facts: dict[str, tuple[str, str, str]] = {}
        data: dict[str, tuple[str, str, int, int]] = {}
        conn = self.h.connect()
        try:
            for nid, qual, sig, doc in conn.execute("SELECT nid, qual, sig, doc FROM units WHERE nid IS NOT NULL"):
                facts.setdefault(nid, (qual or "", sig or "", doc or ""))
            for f, name, a, b in conn.execute("SELECT file, name, a, b FROM units WHERE kind = 'data' ORDER BY file, a"):
                if self.inside(f):  # a data file reached through a link that leaves the project is not shown
                    data[f"{DATA_PREFIX}{f}#{a}"] = (f, name or PurePosixPath(f).name, a, b)
        finally:
            self.h.release(conn)
        return facts, data

    def _reference_roots(self) -> tuple[str, ...]:
        from verinoda.paths import load_config

        try:
            entries = list((load_config(self.repo).get("index") or {}).get("reference") or [])
        except Exception:  # noqa: BLE001 - an unreadable config only means no reference trees
            return ()
        try:  # and the folders detected as copies of the project's own code
            from verinoda import copies

            detected = [c["path"] for c in copies.load(self.repo)]
        except Exception:  # noqa: BLE001 - no detection result: the configured trees only
            detected = []
        entries += detected
        roots = [(e.get("path") if isinstance(e, dict) else e) for e in entries]
        return tuple(r.strip("/").lower() + "/" for r in roots if isinstance(r, str) and r.strip("/"))

    def in_reference(self, f: str | None) -> bool:
        return bool(f and self._refs and f.lower().startswith(self._refs))

    # -- identity of a note --------------------------------------------------------------------
    def kind(self, nid: str) -> str:
        k = self._kinds.get(nid)
        if k is not None:
            return k
        g = self.g
        if nid.startswith(DATA_PREFIX):
            k = "data" if nid in self._data else "missing"
        elif nid not in g.G:
            k = "missing"
        else:
            d = g.G.nodes[nid]
            f = d.get("source_file")
            if d.get("file_type") == "rationale":
                k = "comment"   # a docstring or comment the extractor kept as a node
            elif d.get("external") or d.get("file_type") == "concept" or not f or not self.inside(f):
                k = "external"
            elif g.is_file_node(nid):
                k = "doc" if d.get("file_type") == "document" else "file"
            elif g.is_heading(nid):
                k = "section"
            elif d.get("_callable_class"):
                k = "class"
            elif d.get("_callable"):
                k = "method" if any(True for _ in g.in_edges(nid, {"method"})) else "function"
            else:
                k = "symbol"
        self._kinds[nid] = k
        return k

    def _parent(self, nid: str) -> str | None:
        """The class or function a symbol is defined in (its member link), if any."""
        for u, _d in self.g.in_edges(nid, set(MEMBER_RELATIONS)):
            if u != nid and self.kind(u) in ("class", "function", "method"):
                return u
        return None

    def title(self, nid: str, _depth: int = 0) -> str:
        t = self._titles.get(nid)
        if t is not None:
            return t
        g = self.g
        if nid.startswith(DATA_PREFIX):
            row = self._data.get(nid)
            t = (row[1] or PurePosixPath(row[0]).name) if row else nid[len(DATA_PREFIX):]
        elif nid not in g.G:
            t = nid
        else:
            k = self.kind(nid)
            label = str(g.label(nid))
            if k in ("file", "doc"):
                t = PurePosixPath(g.file(nid) or label).name
            elif k == "comment":
                t = " ".join(label.split())[:80]
            else:
                qual = (self._facts.get(nid) or ("", "", ""))[0]
                bare = label.lstrip(".")
                parent = self._parent(nid) if k in ("function", "method", "class") and _depth < 6 else None
                if parent is not None and self.kind(parent) in ("function", "method"):
                    # a nested function: its enclosing function names it (_extract_generic.walk())
                    t = f"{self.title(parent, _depth + 1).removesuffix('()')}.{bare.removesuffix('()')}()"
                elif qual and k == "function" and "." not in qual:
                    # a module function: its module names it (search_index.rank(), not one of many rank())
                    t = f"{PurePosixPath(g.file(nid) or '').stem}.{qual}()"
                elif qual and k in ("method", "function"):
                    t = qual + "()"
                elif qual:
                    t = qual
                elif k == "method":
                    owner = next((g.label(u) for u, _ in g.in_edges(nid, {"method"})), "")
                    t = f"{owner}.{bare}" if owner else bare
                else:
                    t = bare
        self._titles[nid] = t
        return t

    def own_name(self, nid: str) -> str:
        """The name a symbol is written with in the code (no module, class or parentheses)."""
        if nid.startswith(DATA_PREFIX):
            row = self._data.get(nid)
            name = (row[1] if row else "") or ""
            return name.rsplit(":", 1)[-1].rsplit("/", 1)[-1]
        k = self.kind(nid)
        if k in ("file", "doc"):
            return PurePosixPath(self.g.file(nid) or "").stem
        return str(self.g.label(nid)).lstrip(".").removesuffix("()").rsplit(".", 1)[-1]

    def group(self, nid: str) -> int | str:
        if nid.startswith(DATA_PREFIX):
            return "data"
        k = self.kind(nid)
        if k in ("external", "comment"):
            return "external"
        c = self.g.G.nodes[nid].get("community") if nid in self.g.G else None
        return c if isinstance(c, int) else "other"

    def brief(self, nid: str) -> dict:
        """A link to a note: what a list item or a graph node needs."""
        g = self.g
        if nid.startswith(DATA_PREFIX):
            f, _n, a, _b = self._data.get(nid) or (nid[len(DATA_PREFIX):].rsplit("#", 1)[0], "", 1, 1)
            return {"id": nid, "title": self.title(nid), "kind": "data", "file": f, "line": a, "group": "data"}
        return {"id": nid, "title": self.title(nid), "kind": self.kind(nid), "file": g.file(nid) if nid in g.G else None,
                "line": g.line(nid) if nid in g.G else None, "group": self.group(nid)}

    def data_note_for(self, file: str, line: int | None = None) -> str | None:
        """The data note of ``file`` holding ``line`` (the file's first unit without a line)."""
        best = None
        for nid, (_f, _n, a, b) in self._data_by_file().get(file, []):
            if line is None:
                return nid
            if a <= line <= b and (best is None or b - a < best[1]):
                best = (nid, b - a)
        return best[0] if best else None

    def _data_by_file(self) -> dict[str, list]:
        if self._dbf is None:
            dbf: dict[str, list] = defaultdict(list)
            for nid, row in self._data.items():
                dbf[row[0]].append((nid, row))
            for v in dbf.values():
                v.sort(key=lambda x: x[1][2])
            self._dbf = dbf
        return self._dbf

    def _file_note(self, f: str) -> str | None:
        """The note of a source or document file (its graph node)."""
        if self._file_notes is None:
            fn: dict[str, str] = {}
            for n in self.g.G:
                if self.kind(n) in ("file", "doc") and self.g.file(n):
                    fn.setdefault(self.g.file(n), n)
            self._file_notes = fn
        return self._file_notes.get(f)

    # -- stats and tree --------------------------------------------------------------------------
    def stats(self) -> dict:
        g = self.g
        kinds = Counter(self.kind(n) for n in g.G)
        files = {g.file(n) for n in g.G if self.kind(n) not in HIDDEN_KINDS} | {row[0] for row in self._data.values()}
        hubs = sorted((n for n in g.G if self.kind(n) in ("class", "function", "method", "file")
                       and not _is_test(g.file(n) or "") and not self.in_reference(g.file(n))),
                      key=lambda n: (-g.G.degree(n), self.title(n)))[:16]
        shown = sum(v for k, v in kinds.items() if k not in HIDDEN_KINDS)
        return {"project": self.repo.name, "root": str(self.repo), "notes": shown + len(self._data),
                "files": len(files), "links": g.G.number_of_edges(), "kinds": dict(sorted(kinds.items())),
                "data_notes": len(self._data), "index_notes": list(self.h.notes),
                "hubs": [{**self.brief(n), "degree": g.G.degree(n)} for n in hubs]}

    def tree(self) -> dict:
        """Folders and files with their note ids: ``{"name", "children": [...], "id"?}``."""
        with self._lock:
            if self._tree is not None:
                return self._tree
            self._file_note("")
            file_note: dict[str, str] = dict(self._file_notes or {})
            for f in self._data_by_file():
                file_note.setdefault(f, self.data_note_for(f))
            root: dict = {"name": self.repo.name, "children": {}}
            for f in sorted(file_note):
                node = root
                parts = f.split("/")
                for p in parts[:-1]:
                    node = node["children"].setdefault(p, {"name": p, "children": {}})
                node["children"][parts[-1]] = {"name": parts[-1], "id": file_note[f], "path": f,
                                               "kind": self.kind(file_note[f])}

            def listify(node: dict) -> dict:
                kids = node.get("children")
                if kids is None:
                    return node
                folders = sorted((v for v in kids.values() if "children" in v), key=lambda v: v["name"].lower())
                files = sorted((v for v in kids.values() if "children" not in v), key=lambda v: v["name"].lower())
                return {"name": node["name"], "children": [listify(v) for v in folders + files]}

            self._tree = listify(root)
            return self._tree

    # -- search ----------------------------------------------------------------------------------
    def _search_rows(self) -> list[tuple]:
        """(own name, qualified title, file name, path, id, penalty) per listed note, lower-cased."""
        with self._lock:
            if self._rows is None:
                rows = []
                for n in self.g.G:
                    k = self.kind(n)
                    if k in HIDDEN_KINDS:
                        continue
                    f = self.g.file(n) or ""
                    pen = (0.3 if _is_test(f) else 0.0) + (0.5 if self.in_reference(f) else 0.0)
                    rows.append((self.own_name(n).lower(), self.title(n).lower().removesuffix("()"),
                                 PurePosixPath(f).name.lower() if k in ("file", "doc") else "", f.lower(), n, pen, k))
                for nid, (f, name, _a, _b) in self._data.items():
                    pen = (0.3 if _is_test(f) else 0.0) + (0.5 if self.in_reference(f) else 0.0)
                    rows.append((self.own_name(nid).lower(), (name or "").lower(), "", f.lower(), nid, pen, "data"))
                self._rows = rows
            return self._rows

    def search(self, q: str, limit: int = MAX_SEARCH) -> dict:
        """Names first (exact, prefix, part, path); a question of several words also runs the ranking."""
        g = self.g
        q = " ".join((q or "").split())[:MAX_QUERY_CHARS]
        if not q:
            return {"query": q, "results": []}
        ql = q.lower()
        bonus = {"class": 0.3, "file": 0.25, "doc": 0.2, "function": 0.2, "method": 0.2}
        scored: dict[str, tuple[float, str]] = {}
        for own, title, fname, path, nid, pen, k in self._search_rows():
            if ql in (own, title, fname):
                s, why = 3.0, "name"
            elif own.startswith(ql) or ("." in ql and title.startswith(ql)):  # "Wisp.sp" -> Wisp.spawn()
                s, why = 2.0, "name starts with"
            elif ql in own or (ql in title and "." in ql):
                s, why = 1.5, "name contains"
            elif ql in path:
                s, why = 1.0, "path"
            else:
                continue
            s += bonus.get(k, 0.0) - pen
            if nid not in scored or scored[nid][0] < s:
                scored[nid] = (s, why)
        if len(q.split()) >= 2 or not scored:
            from verinoda import search_index

            try:
                rk = search_index.rank(g, q, limit=25, handle=self.h)
            except Exception:  # noqa: BLE001 - a question the ranking cannot take still gets name hits
                rk = None
            for k, hit in enumerate(rk.hits if rk else []):
                nid = hit.nid if hit.nid and hit.nid in g.G else (
                    self.data_note_for(hit.file, hit.a) if hit.kind == "data" else None)
                if nid is None and hit.file:
                    nid = self._file_note(hit.file)
                if nid is None or self.kind(nid) in HIDDEN_KINDS:
                    continue
                s = 1.4 - 0.02 * k
                if nid not in scored or scored[nid][0] < s:
                    scored[nid] = (s, "question")  # ranked for the question (the note says why it is linked)
        best = sorted(scored.items(), key=lambda kv: (-kv[1][0], self.title(kv[0]).lower(), kv[0]))[:limit]
        return {"query": q, "results": [{**self.brief(n), "why": why} for n, (_s, why) in best]}

    # -- a note ----------------------------------------------------------------------------------
    def note(self, nid: str) -> dict:
        g = self.g
        if nid.startswith(DATA_PREFIX):
            return self._data_note(nid)
        if nid not in g.G or self.kind(nid) == "missing":
            raise KeyError(f"no note {nid!r}")
        k = self.kind(nid)
        qual, sig, doc = self._facts.get(nid) or ("", "", "")
        f = g.file(nid) if self.inside(g.file(nid)) else None
        sections: dict[str, list[dict]] = defaultdict(list)
        seen: set[tuple[str, str, str]] = set()

        def order(x: tuple) -> tuple:
            # the product's own code before tests and reference trees, stated links before inferred ones,
            # then by file and line (71 before 123)
            at = str(_at(x[1]) or "")
            path, _, ln = at.rpartition(":")
            of = g.file(x[0]) or ""
            place = 2 if self.in_reference(of) else 1 if _is_test(of) else 0
            return (place, _conf_rank(x[1]), path if ln.isdigit() else at, int(ln) if ln.isdigit() else 0, x[0])

        edges = sorted([(v, d, True) for v, d in g.out_edges(nid)] + [(u, d, False) for u, d in g.in_edges(nid)],
                       key=order)
        for other, d, out in edges:
            if other == nid or self.kind(other) == "comment":
                continue
            rel = str(d.get("relation") or "related")
            sec = SECTIONS.get(rel, ("other_out", "other_in"))[0 if out else 1]
            if (sec, other, rel) in seen:
                continue
            seen.add((sec, other, rel))
            at = _at(d)
            sections[sec].append({**self.brief(other), "relation": rel, "at": at,
                                  "confidence": d.get("confidence"), "score": d.get("confidence_score"),
                                  "context": d.get("context"),
                                  "derived_by": d.get("_origin") if str(d.get("_origin", "")).startswith("verinoda")
                                  else None,
                                  # the line the link is written on: a call, an import, a reference
                                  "snippet": None, "_snip": None if sec in STRUCTURAL_SECTIONS else at})
        # a file's note is the whole file: its span is not derived (that parses the file)
        span = g.span(nid) if f and k not in ("file", "doc") else None
        code = None
        if f and k in ("file", "doc"):
            lines = self.read_lines(f)
            span = (1, max(1, len(lines)))   # a file's note is the whole file
            code = {"start": 1, "end": min(len(lines), MAX_FILE_LINES), "lines": lines[:MAX_FILE_LINES],
                    "total": len(lines), "lang": self._lang(f)}
        elif f and span:
            lines = self.read_lines(f)
            a, b = span[0], min(span[1], span[0] + MAX_CODE_LINES - 1, len(lines))
            if a <= len(lines):
                code = {"start": a, "end": b, "lines": lines[a - 1:b], "total": span[1] - span[0] + 1,
                        "lang": self._lang(f)}
        if f and k not in HIDDEN_KINDS:
            # a file's note lists the links of the whole file, a symbol's those of its own lines
            self._add_data_links(sections, f, span)
        claims = self._claims([f"{f}::{g.label(nid)}"] + ([f] if k in ("file", "doc") else [])) if f else []
        if claims:
            sections["claims"] = claims
        outline = []
        if k in ("file", "doc") and f:
            outline = [self.brief(s) for s in (g.symbols_in(f) if k == "file" else g.headings_in(f))
                       if self.kind(s) not in HIDDEN_KINDS][:200]
            # the outline already lists the file's own symbols by line: members repeat only the rest
            listed = {o["id"] for o in outline}
            rest = [m for m in sections.get("members", []) if m["id"] not in listed]
            if rest:
                sections["members"] = rest
            else:
                sections.pop("members", None)
        return self._with_user_note(nid, {"id": nid, "title": self.title(nid), "kind": k, "file": f, "line": g.line(nid),
                "span": list(span) if span else None,
                "span_basis": ("file" if k in ("file", "doc") else g.span_basis(nid)) if span else None,
                "signature": sig or None, "doc": doc or None, "qualified": qual or None,
                "breadcrumb": self._breadcrumb(nid), "code": code, "outline": outline,
                "community": {"id": g.G.nodes[nid].get("community"), "name": g.G.nodes[nid].get("community_name")},
                "sections": self._sections(sections), "test": bool(f and _is_test(f)),
                "degree": g.G.degree(nid)})

    # -- notes of your own (verinoda.usernotes) -----------------------------------------------------
    def _subjects_in(self, f: str) -> dict[str, str]:
        """Note id -> subject for the symbols, headings and data units of ``f``, unique in the file:
        the qualified name (``B.__init__()``, not ``.__init__()``), and a second one of the same name
        (a Java overload, a repeated ``Usage`` heading) ``#2`` by line."""
        with self._lock:
            hit = self._subject_cache.get(f)
        if hit is not None:
            return hit
        g = self.g
        named: list[tuple[int, str, str]] = []
        for n in list(g.symbols_in(f)) + list(g.headings_in(f)):
            if self.kind(n) in HIDDEN_KINDS:
                continue
            label = str(g.label(n) or "").strip()
            qual = (self._facts.get(n) or ("",))[0]
            name = (qual or label.lstrip(".")).strip() or n
            if label.endswith("()") and not name.endswith("()"):
                name += "()"
            named.append((g.line(n) or 0, n, name))
        for x, _r in self._data_by_file().get(f, []):
            _f, name, a, _b = self._data[x]
            named.append((a, x, name or f"line {a}"))
        seen: Counter = Counter()
        out: dict[str, str] = {}
        for _line, n, name in sorted(named):
            seen[name] += 1
            out[n] = f"{f}::{name}" if seen[name] == 1 else f"{f}::{name}#{seen[name]}"
        with self._lock:
            self._subject_cache[f] = out
        return out

    def subject_of(self, nid: str) -> tuple[str, str, int, int] | None:
        """What a note of your own on ``nid`` is about: (subject, file, first line, last line); None
        for what has no code in the project (an external type, a comment)."""
        g = self.g
        if nid.startswith(DATA_PREFIX):
            row = self._data.get(nid)
            if row is None or not self.inside(row[0]):
                return None
            f, _name, a, b = row
            return self._subjects_in(f).get(nid, f), f, a, b
        if nid not in g.G:
            return None
        k = self.kind(nid)
        f = g.file(nid)
        if k in HIDDEN_KINDS or not f or not self.inside(f):
            return None
        if k in ("file", "doc"):
            return f, f, 1, max(1, len(self.read_lines(f)))
        subject = self._subjects_in(f).get(nid)
        if subject is None:
            return None
        span = g.span(nid) or ((g.line(nid) or 1),) * 2
        return subject, f, span[0], span[1]

    def note_for_subject(self, subject: str) -> str | None:
        """The note a subject (``file::Name`` or ``file``) is about now, when the index still has it."""
        f, sep, _name = subject.partition("::")
        if not sep:
            return self._file_note(f) or self.data_note_for(f)
        return next((n for n, s in self._subjects_in(f).items() if s == subject), None)

    def _with_user_note(self, nid: str, out: dict) -> dict:
        from verinoda import usernotes

        sub = self.subject_of(nid)
        out["can_note"] = sub is not None
        found = usernotes.find(self.repo, sub[0]) if sub is not None else None
        out["user_note"] = usernotes.as_dict(self.repo, found, resolves=True) if found is not None else None
        return out

    def user_notes(self) -> list[dict]:
        """Every note of your own with its status, those whose code changed or is gone first."""
        from verinoda import usernotes

        order = {"changed": 0, "gone": 1, "fresh": 2}
        out = []
        for n in usernotes.load_all(self.repo):
            nid = self.note_for_subject(n.subject)
            out.append({**usernotes.as_dict(self.repo, n, resolves=nid is not None), "id": nid})
        return sorted(out, key=lambda x: (order.get(x["status"], 3), x["subject"]))

    def _sections(self, sections: dict[str, list[dict]]) -> list[dict]:
        out = []
        for key in SECTION_ORDER:
            items = sections.get(key) or []
            if items:
                kept = items[:MAX_SECTION_ITEMS]
                shown = {id(it) for it in kept}
                for it in items:  # the line a link is written on: read for the links shown only
                    at = it.pop("_snip", None)
                    if at and id(it) in shown:
                        it["snippet"] = self.line_at(at)
                out.append({"key": key, "count": len(items), "items": kept})
        return out

    def _breadcrumb(self, nid: str) -> list[dict]:
        g = self.g
        chain: list[str] = []
        cur, guard = nid, 0
        while guard < 8:
            guard += 1
            parent = next((u for u, d in g.in_edges(cur, set(MEMBER_RELATIONS)) if u != cur), None)
            if parent is None or parent in chain:
                break
            chain.append(parent)
            cur = parent
        f = g.file(nid)
        if f and not any(self.kind(c) in ("file", "doc") for c in chain) and self.kind(nid) not in ("file", "doc"):
            file_note = self._file_note(f)
            if file_note and file_note != nid:
                chain.append(file_note)
        return [self.brief(c) for c in reversed(chain) if self.kind(c) not in HIDDEN_KINDS]

    def _lang(self, f: str) -> str:
        return CODE_SUFFIX_LANG.get(PurePosixPath(f).suffix.lower(), "text")

    def _add_data_links(self, sections: dict, file: str, span: tuple[int, int] | None) -> None:
        """Data files the lines name by resource id, and the lines (code or data) that name this file."""
        if span is None:
            return
        from verinoda import search_index

        try:
            lk = search_index.describe_links(self.h, file, span[0], span[1], limit=MAX_SECTION_ITEMS)
        except Exception:  # noqa: BLE001 - links are an extra
            return
        for e in lk.get("names") or []:
            for t in e.get("targets") or []:
                target = self.data_note_for(t) or self._file_note(t)
                if target and self.kind(target) not in HIDDEN_KINDS:
                    at = f"{file}:{e['line']}"
                    sections["names_data"].append({**self.brief(target), "relation": f"names {e['rid']}",
                                                   "at": at, "sure": e.get("sure"), "snippet": None, "_snip": at,
                                                   "confidence": "EXTRACTED" if e.get("sure") else "INFERRED"})
        for e in lk.get("named_by") or []:
            src = self._note_at(e["file"], e["line"])
            if src and self.kind(src) not in HIDDEN_KINDS:
                at = f"{e['file']}:{e['line']}"
                sections["named_by"].append({**self.brief(src), "relation": f"names {e['rid']}",
                                             "at": at, "sure": e.get("sure"), "snippet": None, "_snip": at,
                                             "confidence": "EXTRACTED" if e.get("sure") else "INFERRED"})

    def _note_at(self, f: str, line: int) -> str | None:
        """The innermost note holding ``f:line``: a symbol, else a data unit, else the file."""
        return self.g.symbol_at(f, line) or self.data_note_for(f, line) or self._file_note(f)

    def _claim_rows(self) -> list[tuple]:
        """Current claims, newest first: (id, text, status, confidence, subjects).

        Read once and kept while atlas.db and its write-ahead log are unchanged (claims change
        without the graph changing); a file written just now is read again on the next call.
        """
        from verinoda.paths import atlas_dir

        db = atlas_dir(self.repo) / "atlas.db"
        key = (_stat(db), _stat(Path(str(db) + "-wal")))
        with self._lock:
            if self._claim_cache is not None and self._claim_cache[0] == key:
                return self._claim_cache[1]
        rows: list[tuple] = []
        if key[0] is not None:
            try:
                conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
            except sqlite3.Error:
                return []
            try:
                for cid, text, status, conf, subj in conn.execute(
                        "SELECT id, text, status, confidence, subjects FROM claims WHERE superseded_by IS NULL "
                        "ORDER BY updated_at DESC"):
                    try:
                        listed = json.loads(subj or "[]")
                    except ValueError:
                        listed = []
                    names = frozenset(s for s in listed if isinstance(s, str)) if isinstance(listed, list)                         else frozenset()
                    rows.append((cid, text, status, conf, names))
            except sqlite3.Error:
                rows = []
            finally:
                conn.close()
        racy = any(st is not None and st[0] >= time.time_ns() - RACY_NS for st in key)
        with self._lock:
            self._claim_cache = None if racy else (key, rows)
        return rows

    def _claims(self, subjects: list[str]) -> list[dict]:
        """Current claims whose subjects list one of ``subjects`` exactly (``file::label`` or ``file``)."""
        want = set(subjects)
        if not want:
            return []
        out = []
        for cid, text, status, conf, names in self._claim_rows():
            if names & want:
                out.append({"id": cid, "title": text, "kind": "claim", "status": status, "score": conf})
                if len(out) >= MAX_SECTION_ITEMS:
                    break
        return out

    def _data_note(self, nid: str) -> dict:
        row = self._data.get(nid)
        if row is None:
            raise KeyError(f"no note {nid!r}")
        f, name, a, b = row
        lines = self.read_lines(f)
        end = min(b, a + MAX_CODE_LINES - 1, len(lines))
        sections: dict[str, list[dict]] = defaultdict(list)
        self._add_data_links(sections, f, (a, b))
        claims = self._claims([f])
        if claims:
            sections["claims"] = claims
        others = [x for x, _r in self._data_by_file().get(f, []) if x != nid]
        return self._with_user_note(nid, {"id": nid, "title": name or PurePosixPath(f).name, "kind": "data", "file": f, "line": a,
                "span": [a, b], "span_basis": "data unit", "signature": None, "doc": None, "qualified": None,
                "breadcrumb": [], "outline": [self.brief(x) for x in others][:200],
                "code": {"start": a, "end": end, "lines": lines[a - 1:end], "total": b - a + 1, "lang": self._lang(f)},
                "community": {"id": None, "name": None}, "sections": self._sections(sections),
                "test": _is_test(f), "degree": sum(len(v) for v in sections.values())})

    # -- graphs ----------------------------------------------------------------------------------
    def local_graph(self, nid: str, depth: int = 1, relations: set[str] | None = None, *,
                    tests: bool = True, external: bool = False, data: bool = True) -> dict:
        """The note and its neighbours up to ``depth`` links away (both directions).

        Past :data:`MAX_LOCAL_NODES` nothing more is expanded; callers, callees, imports and
        references come before a note's own members, so a large module still shows who uses it.
        """
        g = self.g
        rels = set(relations) if relations else set(GRAPH_RELATIONS)
        depth = max(1, min(3, int(depth)))
        if self.kind(nid) == "missing" or (not nid.startswith(DATA_PREFIX) and nid not in g.G):
            raise KeyError(f"no note {nid!r}")

        def keep(n: str) -> bool:
            if n.startswith(DATA_PREFIX):
                return data and n in self._data
            k = self.kind(n)
            if k in HIDDEN_KINDS and not (external and k == "external"):
                return False
            return tests or not _is_test(g.file(n) or "")

        def priority(u: str, item: tuple) -> int:  # a note's own members last; its parent is one node
            rel = str(item[1].get("relation"))
            return 2 if rel in MEMBER_RELATIONS and item[2] == u else 1 if rel == "names" else 0

        dist = {nid: 0}
        edges: dict[tuple[str, str, str], dict] = {}
        frontier = [nid]
        truncated = False
        for level in range(1, depth + 1):
            nxt: list[str] = []
            for u in frontier:
                if len(dist) >= MAX_LOCAL_NODES:
                    truncated = True
                    break
                for v, d, src, dst in sorted(self._neighbours(u, rels, data), key=lambda x, u=u: priority(u, x)):
                    if not keep(v):
                        continue
                    if v not in dist:
                        if len(dist) >= MAX_LOCAL_NODES:
                            truncated = True
                            continue
                        dist[v] = level
                        nxt.append(v)
                    key = (src, dst, str(d.get("relation")))
                    if key not in edges or _conf_rank(d) < _conf_rank(edges[key]):
                        edges[key] = d
            frontier = nxt
            if truncated:
                break
        # links among the nodes already shown (the local graph is an induced subgraph)
        shown = set(dist)
        for u in list(shown):
            if u.startswith(DATA_PREFIX):
                continue
            for v, d in g.out_edges(u):
                rel = str(d.get("relation"))
                if v in shown and v != u and rel in rels and (u, v, rel) not in edges:
                    edges[(u, v, rel)] = d
        nodes = [{**self.brief(n), "depth": dist[n], "degree": g.G.degree(n) if n in g.G else 1} for n in dist]
        return {"center": nid, "depth": depth, "nodes": nodes, "truncated": truncated,
                "edges": [{"source": s, "target": t, "relation": r, "confidence": d.get("confidence"),
                           "at": _at(d)} for (s, t, r), d in edges.items()]}

    def _neighbours(self, u: str, rels: set[str], data: bool):
        g = self.g
        if not u.startswith(DATA_PREFIX):
            for v, d in g.out_edges(u):
                if str(d.get("relation")) in rels and v != u:
                    yield v, d, u, v
            for v, d in g.in_edges(u):
                if str(d.get("relation")) in rels and v != u:
                    yield v, d, v, u
        if not data:
            return
        if u.startswith(DATA_PREFIX):
            f, _n, a, b = self._data[u]
        else:
            f = g.file(u)
            if not f or self.kind(u) in HIDDEN_KINDS:
                return
            sp = g.span(u)
            a, b = sp if sp and self.kind(u) not in ("file", "doc") else (1, 10 ** 9)
        sec: dict = defaultdict(list)
        self._add_data_links(sec, f, (a, b))
        for it in sec.get("names_data", []):
            yield it["id"], {"relation": "names", "confidence": it["confidence"],
                             "source_file": f, "source_location": "L" + it["at"].rsplit(":", 1)[1]}, u, it["id"]
        for it in sec.get("named_by", []):
            ff, _, ln = it["at"].rpartition(":")
            yield it["id"], {"relation": "names", "confidence": it["confidence"], "source_file": ff,
                             "source_location": f"L{ln}"}, it["id"], u

    def global_graph(self, *, tests: bool = True, data: bool = True, relations: set[str] | None = None,
                     cap: int | None = MAX_GLOBAL_NODES) -> dict:
        """Files as nodes; links between them counted over their symbols' links and their resource ids.

        At most ``cap`` files, the best connected (None: every file, for the exported page, which
        applies the same cut after its own filters)."""
        g = self.g
        rels = set(relations) if relations else set(GRAPH_RELATIONS) - set(MEMBER_RELATIONS)
        cache_key = (tests, data, tuple(sorted(rels)), cap)
        with self._lock:
            if cache_key in self._global:
                return self._global[cache_key]
        weight: Counter = Counter()
        rel_of: dict[tuple[str, str], Counter] = defaultdict(Counter)
        size: Counter = Counter()
        community: dict[str, Counter] = defaultdict(Counter)
        for n, d in g.G.nodes(data=True):
            f = d.get("source_file")
            if not f or self.kind(n) in HIDDEN_KINDS or (not tests and _is_test(f)):
                continue
            size[f] += 1
            if isinstance(d.get("community"), int):
                community[f][d["community"]] += 1
        for u, v, d in g.G.edges(data=True):
            rel = str(d.get("relation"))
            if rel not in rels:
                continue
            fu, fv = g.file(u), g.file(v)
            if not fu or not fv or fu == fv or fu not in size or fv not in size:
                continue
            if self.kind(u) in HIDDEN_KINDS or self.kind(v) in HIDDEN_KINDS:
                continue
            weight[(fu, fv)] += 1
            rel_of[(fu, fv)][rel] += 1
        if data:
            conn = self.h.connect()
            try:
                refs = conn.execute("SELECT DISTINCT file, target FROM refs").fetchall()
            except sqlite3.Error:
                refs = []
            finally:
                self.h.release(conn)
            for f, t in refs:
                if f == t or not self.inside(f) or not self.inside(t) or \
                        (not tests and (_is_test(f) or _is_test(t))):
                    continue
                for x in (f, t):
                    size[x] = size[x] or 1
                weight[(f, t)] += 1
                rel_of[(f, t)]["names"] += 1
        degree: Counter = Counter()
        for (a, b), w in weight.items():
            degree[a] += w
            degree[b] += w
        files = [f for f in sorted(size, key=lambda f: (-degree[f], f))
                 if self._file_note(f) or self.data_note_for(f)]   # a file without a note (a texture) is left out
        keep = len(files) if cap is None else cap
        hidden = max(0, len(files) - keep)
        nodes = []
        for f in sorted(files[:keep]):
            nid = self._file_note(f) or self.data_note_for(f)
            grp = community[f].most_common(1)[0][0] if community[f] else ("data" if nid.startswith(DATA_PREFIX)
                                                                          else "other")
            nodes.append({"id": nid, "title": PurePosixPath(f).name, "file": f,
                          "kind": "data" if nid.startswith(DATA_PREFIX) else self.kind(nid), "group": grp,
                          "size": size[f], "degree": degree[f], "test": _is_test(f),
                          "folder": str(PurePosixPath(f).parent), "area": _area(str(PurePosixPath(f).parent))})
        by_file = {n["file"]: n["id"] for n in nodes}
        edges = [{"source": by_file[a], "target": by_file[b], "weight": w,
                  "relation": rel_of[(a, b)].most_common(1)[0][0]}
                 for (a, b), w in sorted(weight.items()) if a in by_file and b in by_file]
        out = {"nodes": nodes, "edges": edges, "hidden_files": hidden, "groups": _group_names(nodes)}
        with self._lock:
            self._global[cache_key] = out
        return out
