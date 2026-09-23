"""Persistent passage-level lexical index and ranking (docs/DESIGN.md D17-D19, D22).

The index lives in ``.verinoda/index/search.db`` next to ``graph.json``. It is
derived, disposable data (kept out of ``atlas.db``, whose rows are never
deleted) and is rebuilt from the graph and the files at any time.

**Units** are what retrieval returns:

* every graph symbol, over its *own* lines (nested symbols excluded);
* module-level blocks: runs of at most :data:`WINDOW` non-blank, non-import
  lines outside every symbol (assigned names go to the name field);
* prose sections: the lines of a Markdown heading up to the next heading.

**Passages** are windows of :data:`WINDOW` own lines with a stride of
:data:`STRIDE` (a run of up to :data:`WINDOW` lines is one passage), so a
3,000-line function becomes hundreds of competing passages instead of one bag
of words.

**Tokens** are identifier-aware: every word is split with
:func:`verinoda.textnorm.split_identifier` (snake_case, camelCase, digits);
a compound identifier is also indexed whole (``query_terms``,
``tokenbudget``); parts are passed through the light English
:func:`word_stem` (``saved``, ``saves`` and ``save`` meet).
Turkish letters are folded first, so ``sipariş`` and ``siparis`` agree.

**Ranking** (:func:`rank`) is BM25F (Robertson & Zaragoza 2009) per passage
with fields name (weight 3.0, b 0.3), path (weight 0.5) and body (b 0.75),
k1 1.2; idf is counted per *unit*; a unit scores its best passage, divided by
the best unit's score. A passage of code (not of prose: documentation
repeats command names side by side), or a unit name, in which two adjacent
words of the question are also adjacent (``home directory``) counts
``1 + 0.3`` times. A unit whose name the question spells out as code
(``_query_terms``, ``OrderRepository.save``) gets lexical score 1.0. A
personalised PageRank computed by local push (Andersen, Chung & Lang 2006;
alpha 0.3, eps 1e-4, at most 20k pushes) from the top lexical units adds
``0.4 * p / (p + 0.02)``: it surfaces callers and callees that do not share
the question's words. Graph edges only move the ranking; they are never
presented as evidence.

**Query side** (:func:`analyze_query`): English and Turkish stopwords, Turkish
folding and apostrophe suffixes (``pricing.py'deki``), Turkish stems confirmed
by the index vocabulary, and abbreviation expansion (a corpus token that is a
strict prefix of a query word - ``env`` for ``environment`` - plus a small
fixed map ``cfg``/``config``, ``db``/``database`` ...). Every expansion is
reported with its origin and weighted below the words the user wrote.

The index is incremental: :func:`update` re-indexes a file when its content
(sha256) or the graph symbols that shape its units changed, drops removed
files, and keeps document frequencies exact. A schema or tokenizer version
change rebuilds it.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from verinoda import textnorm
from verinoda.architecture_map import is_test_file

SCHEMA_VERSION = 2
TOKENIZER_VERSION = 2
DB_NAME = "search.db"

WINDOW, STRIDE = 12, 6            # passage length and stride, in own non-blank lines
K1, B_BODY, B_NAME = 1.2, 0.75, 0.3
W_NAME, W_PATH = 3.0, 0.5
PPR_ALPHA, PPR_EPS, PPR_MAX_PUSH = 0.3, 1e-4, 20_000
PPR_LAMBDA, PPR_KAPPA = 0.4, 0.02
PPR_SEEDS = 8
INFERRED_FACTOR = 0.7
# relation -> (weight along the edge, weight against it)
REL_WEIGHTS = {"calls": (1.0, 0.6), "uses": (0.5, 0.3), "inherits": (0.5, 0.3), "method": (0.3, 0.3),
               "references": (0.4, 0.2), "imports_from": (0.15, 0.1)}
EXPANSION_WEIGHT = 0.5            # abbreviation / prefix / Turkish-stem expansions
PROVIDED_EXPANSION_WEIGHT = 0.7   # expansions supplied by the caller (question plan, lexicon)
PROX_BOOST = 0.3                  # code passage factor when two adjacent question words are adjacent in it
PROX_GAP = 1                      # "adjacent": at most this many tokens apart, in either order
PROX_CANDIDATES = 300             # passages checked for proximity, best term coverage first
MAX_FILE_BYTES = 4_000_000        # larger files are not indexed (reported in stats)
PROSE_SUFFIXES = (".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc")
MAX_SIG_CHARS = 220
MAX_DOC_CHARS = 240

IMPORT_RE = re.compile(r"^\s*(from\s+\S+\s+)?import\s|^\s*(#include|using\s+[\w.]+;|require\s*\()")
ASSIGN_RE = re.compile(r"^(?:export\s+)?(?:const\s+|let\s+|var\s+)?([A-Za-z_$][\w$]*)\s*(?::[^=]*)?=(?![=>])")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?:\(\))?")
FILE_EXTS = {"py", "js", "ts", "tsx", "jsx", "go", "rs", "java", "rb", "md", "rst", "json", "toml", "yaml",
             "yml", "txt", "cfg", "ini", "html", "css", "sql", "sh", "db", "xml", "csv", "lock"}
# (suffix, shortest stem kept); first match wins
_SUFFIXES = (("ations", 4), ("ation", 4), ("ments", 4), ("ment", 4), ("ings", 4), ("ing", 4), ("ies", 4),
             ("ers", 4), ("es", 4), ("ed", 3), ("er", 4), ("s", 3))
_WORD = re.compile(r"\w+")
_TR_CASE = str.maketrans({"İ": "I", "ı": "i", "Ş": "S", "ş": "s", "Ç": "C", "ç": "c", "Ğ": "G", "ğ": "g",
                          "Ö": "O", "ö": "o", "Ü": "U", "ü": "u", "Â": "A", "â": "a", "Î": "I", "î": "i",
                          "Û": "U", "û": "u"})

# English question/filler words (Graphify's _QUERY_STOPWORDS, English part, plus
# textnorm's list). Turkish ones come from textnorm.TR_STOPWORDS.
EN_STOPWORDS = frozenset(textnorm.EN_STOPWORDS | {
    "how", "what", "why", "when", "where", "which", "who", "whom", "whose", "does", "did", "is", "are",
    "was", "were", "be", "been", "being", "can", "could", "should", "would", "will", "shall", "may",
    "might", "must", "has", "have", "had", "the", "and", "but", "not", "for", "from", "with", "without",
    "into", "onto", "off", "that", "this", "these", "those", "there", "here", "its", "their", "them",
    "they", "about", "any", "all", "some", "work", "works", "working", "do", "it", "if", "so", "as",
})
# Abbreviations common in identifiers (short -> long forms). A question word
# is expanded in both directions: ``db`` also searches ``database`` and
# ``database`` also searches ``db``. Values are plain words, stemmed like the index.
ABBREVIATIONS = {
    "env": ("environment",), "cfg": ("config", "configuration"), "conf": ("config", "configuration"),
    "db": ("database",), "msg": ("message",), "ctx": ("context",), "err": ("error",), "idx": ("index",),
    "tmp": ("temporary",), "repo": ("repository",), "param": ("parameter",), "params": ("parameters",),
    "dir": ("directory",), "dirs": ("directories",), "arg": ("argument",), "args": ("arguments",),
}
# short forms only expanded from short to long (the long word is a generic question word)
ABBREVIATIONS_ONE_WAY = {"fn": ("function",), "func": ("function",)}
# Never taken as the abbreviation of a longer word: keywords and short words
# that are words of their own (``def``/``default``, ``over``/``override``).
NOT_ABBREVIATIONS = frozenset("""def out over under set get put add run use end new old all any not and has can for
    key map log tag row col let var_ int str len get_ has_ is_ to from with into self this that the per pre post
    sub non re un""".split())
# A prefix followed by one of these is a derived word, not an abbreviation (graph|ify).
_DERIVATIONAL = ("ify", "ize", "ise", "able", "ible", "ful", "less", "ness", "ship", "hood", "wise", "like")


def stem(term: str) -> str:
    """Light suffix stripping for plain words (identifiers are kept verbatim)."""
    if len(term) < 5 or not term.isalpha():
        return term
    for suf, keep in _SUFFIXES:
        if term.endswith(suf) and len(term) - len(suf) >= keep:
            return term[: -len(suf)]
    return term


_E_KEEP = frozenset({"not", "the", "one", "are", "use", "see", "ref"})


def word_stem(part: str) -> str:
    """:func:`stem`, then a final ``e`` dropped so inflections meet their base form.

    ``stem`` maps ``saved`` to ``sav`` and ``stored`` to ``stor`` but leaves
    ``save`` and ``store``; dropping the ``e`` of the base form (keeping at
    least three letters, and never producing a common short word such as
    ``not`` from ``note``) makes ``save``/``saved``/``saves``,
    ``cache``/``cached``/``caches`` and ``update``/``updated`` one token.
    """
    t = stem(part)
    if len(t) >= 4 and t.endswith("e") and t.isalpha() and t[:-1] not in _E_KEEP:
        return t[:-1]
    return t


@lru_cache(maxsize=262_144)
def word_tokens(word: str) -> tuple[str, ...]:
    """Tokens of one ``\\w+`` word: the whole compound identifier (folded) and its stemmed parts."""
    core = word.strip("_")
    if not core:
        return ()
    if not core.isascii():
        core = core.translate(_TR_CASE)
    parts = textnorm.split_identifier(core)
    out: list[str] = []
    if len(parts) > 1:
        out.append(textnorm.fold_tr(core))
    for p in parts:
        if len(p) >= 2:
            out.append(word_stem(p))
    return tuple(out)


def tokens(text: str) -> list[str]:
    """Identifier-aware tokens of ``text`` (the index and the query use the same function)."""
    out: list[str] = []
    for w in _WORD.findall(text):
        out.extend(word_tokens(w))
    return out


RAW_PREFIX = "="  # names-table rows of unstemmed identifier parts (for abbreviation checks)


def raw_name_parts(text: str) -> list[str]:
    """Unstemmed identifier parts of a name (``ENV_ALLOW`` -> ``env``, ``allow``)."""
    out: list[str] = []
    for w in _WORD.findall(text):
        core = w.strip("_")
        if core and not core.isascii():
            core = core.translate(_TR_CASE)
        out += [x for x in textnorm.split_identifier(core) if len(x) >= 2] if core else []
    return sorted(set(out))


def named_identifiers(question: str) -> list[str]:
    """Names the question spells as code: snake_case, camelCase/CamelCase, ``a.b``, ``f()``."""
    out: list[str] = []
    for m in IDENT_RE.finditer(question):
        tok = m.group(0)
        call = tok.endswith("()")
        tok = tok[:-2] if call else tok
        parts = tok.split(".")
        if len(parts) > 1 and parts[-1].lower() in FILE_EXTS:
            continue  # a file name, not a symbol
        coded = call or len(parts) > 1 or any("_" in p.strip("_") or p.startswith("_")
                                               or re.search(r"[a-z0-9][A-Z]", p) for p in parts)
        if coded:
            out += [p for p in parts if len(p) >= 3]
    return list(dict.fromkeys(out))


def bare_label(label: str) -> str:
    return label.strip().lstrip(".").split("(")[0]


# -- building -------------------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
    file TEXT PRIMARY KEY, sha256 TEXT NOT NULL, gsig TEXT NOT NULL, size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL, n_lines INTEGER NOT NULL, uid_lo INTEGER, uid_hi INTEGER,
    pid_lo INTEGER, pid_hi INTEGER, skipped TEXT
);
CREATE TABLE IF NOT EXISTS units (
    uid INTEGER PRIMARY KEY, file TEXT NOT NULL, nid TEXT, kind TEXT NOT NULL, name TEXT NOT NULL,
    qual TEXT NOT NULL, a INTEGER NOT NULL, b INTEGER NOT NULL, own INTEGER NOT NULL, sig TEXT NOT NULL,
    doc TEXT NOT NULL, consts TEXT NOT NULL, name_len INTEGER NOT NULL, p_lo INTEGER, terms TEXT NOT NULL,
    raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS units_file ON units(file);
CREATE TABLE IF NOT EXISTS passages (
    pid INTEGER PRIMARY KEY, uid INTEGER NOT NULL, a INTEGER NOT NULL, b INTEGER NOT NULL, len INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS body (term TEXT NOT NULL, pid INTEGER NOT NULL, tf INTEGER NOT NULL,
                                 PRIMARY KEY (term, pid)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS names (term TEXT NOT NULL, uid INTEGER NOT NULL, tf INTEGER NOT NULL,
                                  PRIMARY KEY (term, uid)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS paths (term TEXT NOT NULL, file TEXT NOT NULL, tf INTEGER NOT NULL,
                                  PRIMARY KEY (term, file)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS df (term TEXT PRIMARY KEY, df INTEGER NOT NULL) WITHOUT ROWID;
"""


def db_path_for(g) -> Path:
    """``search.db`` next to the graph file of ``g`` (an :class:`verinoda.index.Graph`)."""
    return Path(g.path).parent / DB_NAME


def _connect(db: Path | str, *, timeout: float = 30.0) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db), timeout=timeout)
    conn.execute("PRAGMA journal_mode = WAL" if str(db) != ":memory:" else "PRAGMA journal_mode = MEMORY")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _meta(conn: sqlite3.Connection) -> dict:
    try:
        return {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM meta")}
    except sqlite3.Error:
        return {}


def _set_meta(conn: sqlite3.Connection, **kv) -> None:
    conn.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                     [(k, json.dumps(v, sort_keys=True)) for k, v in kv.items()])


def _gsig(g, f: str) -> str:
    """Hash of the graph facts that shape ``f``'s units (symbols, their classes, headings)."""
    rows = []
    for n in g.symbols_in(f):
        cls = next((g.label(u) for u, _ in g.in_edges(n, {"method"})), "")
        rows.append([n, g.label(n), g.line(n), cls])
    rows.append(["#", *[[n, g.line(n)] for n in g.headings_in(f)]])
    return hashlib.sha1(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()


def _graph_files(g) -> list[str]:
    from verinoda.index import _local_source

    out = []
    for f in sorted({d["source_file"] for _, d in g.G.nodes(data=True)
                     if isinstance(d.get("source_file"), str) and d.get("source_file")}):
        if _local_source(g.root, f) is not None:
            out.append(f)
    return out


@dataclass
class _Unit:
    file: str
    nid: str | None
    kind: str          # symbol | module | prose
    name: str
    qual: str
    a: int
    b: int
    own: list[int]
    sig: str = ""
    doc: str = ""
    consts: list[tuple[str, int]] = field(default_factory=list)


def _sig(lines: list[str], a: int, b: int) -> str:
    out = []
    for i in range(a, min(b, a + 8, len(lines)) + 1):
        s = lines[i - 1].strip()
        out.append(s)
        if s.endswith(":") or "{" in s or s.endswith(";"):
            break
    sig = re.sub(r"\s+", " ", " ".join(out)).rstrip(":{ ")
    return sig[:MAX_SIG_CHARS]


def _first_paragraph(doc: str) -> str:
    return re.sub(r"\s+", " ", doc.strip().split("\n\n")[0]).strip()[:MAX_DOC_CHARS]


def _code_units(g, f: str, lines: list[str]) -> list[_Unit]:
    from verinoda.index import py_file_info

    syms = g.symbols_in(f)
    owners = g._owners_of(f) if syms else []
    docs: dict[int, str] = {}
    if f.endswith(".py"):
        try:
            docs = py_file_info(g.root / f).docs
        except (SyntaxError, ValueError, OSError, RecursionError):
            docs = {}

    def owner(i: int):
        return owners[i] if i < len(owners) else None

    consts: dict[str, int] = {}
    for i, line in enumerate(lines, 1):
        if owner(i) is None:
            m = ASSIGN_RE.match(line)
            if m and not IMPORT_RE.match(line):
                consts.setdefault(m.group(1), i)
    units: list[_Unit] = []
    for n in syms:
        sp = g.span(n)
        if not sp or sp[0] > len(lines):
            continue
        a, b = sp[0], min(sp[1], len(lines))
        own = [i for i in range(a, b + 1) if owner(i) == n and lines[i - 1].strip()]
        if not own and lines[a - 1].strip():
            own = [a]
        label = bare_label(g.label(n))
        cls = next((bare_label(g.label(u)) for u, _ in g.in_edges(n, {"method"})), "")
        u = _Unit(f, n, "symbol", label, f"{cls}.{label}" if cls else label, a, b, own,
                  sig=_sig(lines, a, b), doc=_first_paragraph(docs.get(a, "")))
        if consts:
            body_words = set(_WORD.findall("\n".join(lines[a - 1:b])))
            u.consts = sorted(((c, ln) for c, ln in consts.items() if c in body_words and not a <= ln <= b),
                              key=lambda x: x[1])[:4]
        units.append(u)
    block: list[int] = []

    def flush() -> None:
        if block:
            names = [m.group(1) for i in block if (m := ASSIGN_RE.match(lines[i - 1]))]
            units.append(_Unit(f, None, "module", " ".join(names[:6]), "", block[0], block[-1], list(block),
                               sig=_sig(lines, block[0], block[0])))
            block.clear()

    for i, line in enumerate(lines, 1):
        mod = owner(i) is None
        if not mod or not line.strip():
            flush()
            continue
        if IMPORT_RE.match(line):
            continue
        block.append(i)
        if len(block) >= WINDOW:
            flush()
    flush()
    return units


def _prose_units(g, f: str, lines: list[str]) -> list[_Unit]:
    from verinoda.index import MARKDOWN_SUFFIXES, md_headings

    heads = md_headings(lines) if f.lower().endswith(MARKDOWN_SUFFIXES) else []
    by_line = {g.line(n): n for n in g.headings_in(f)}
    starts = [ln for ln, _ in heads]
    units: list[_Unit] = []
    bounds = ([(1, starts[0] - 1)] if starts and starts[0] > 1 else [] if starts else [(1, len(lines))])
    bounds += [(ln, (starts[k + 1] - 1) if k + 1 < len(starts) else len(lines)) for k, ln in enumerate(starts)]
    for a, b in bounds:
        own = [i for i in range(a, b + 1) if lines[i - 1].strip()]
        if not own:
            continue
        is_head = a in starts
        name = lines[a - 1].strip().lstrip("#").strip() if is_head else Path(f).name
        units.append(_Unit(f, by_line.get(a) if is_head else None, "prose", name[:120], "", a, own[-1], own,
                           sig=lines[a - 1].strip()[:MAX_SIG_CHARS]))
    return units


def _windows(own: list[int]) -> list[tuple[int, int]]:
    runs: list[list[int]] = []
    for i in own:
        if runs and i == runs[-1][-1] + 1:
            runs[-1].append(i)
        else:
            runs.append([i])
    out: list[tuple[int, int]] = []
    for run in runs:
        if len(run) <= WINDOW:
            out.append((run[0], run[-1]))
        else:
            out += [(run[k], run[min(k + WINDOW, len(run)) - 1]) for k in range(0, len(run) - STRIDE, STRIDE)]
    return out


def _path_tf(f: str) -> dict[str, int]:
    tf: dict[str, int] = defaultdict(int)
    for t in tokens(f.replace("/", " ").rsplit(".", 1)[0]):
        tf[t] += 1
    return dict(tf)


class _Writer:
    """Inserts one file's units, passages and postings; keeps df and the totals exact."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        m = _meta(conn)
        self.totals = {k: int(m.get(k, 0)) for k in ("n_units", "n_passages", "sum_plen", "sum_name_len")}
        self.next_uid = (conn.execute("SELECT COALESCE(MAX(uid), 0) FROM units").fetchone()[0] or 0) + 1
        self.next_pid = (conn.execute("SELECT COALESCE(MAX(pid), 0) FROM passages").fetchone()[0] or 0) + 1
        self.df_delta: dict[str, int] = defaultdict(int)

    def remove_file(self, f: str) -> None:
        row = self.conn.execute("SELECT uid_lo, uid_hi, pid_lo, pid_hi FROM files WHERE file = ?", (f,)).fetchone()
        if row is None:
            return
        uid_lo, uid_hi, pid_lo, pid_hi = row
        if uid_lo is not None:
            units = self.conn.execute("SELECT uid, terms, name_len, raw FROM units WHERE uid BETWEEN ? AND ?",
                                      (uid_lo, uid_hi)).fetchall()
            all_terms: set[str] = set()
            raw_terms: set[str] = set()
            for _uid, terms, name_len, raw in units:
                ts = terms.split()
                all_terms.update(ts)
                raw_terms.update(RAW_PREFIX + r for r in raw.split())
                for t in ts:
                    self.df_delta[t] -= 1
                self.totals["sum_name_len"] -= name_len
            self.totals["n_units"] -= len(units)
            if pid_lo is not None:
                n_p, s_len = self.conn.execute("SELECT COUNT(*), COALESCE(SUM(len), 0) FROM passages "
                                               "WHERE pid BETWEEN ? AND ?", (pid_lo, pid_hi)).fetchone()
                self.totals["n_passages"] -= n_p
                self.totals["sum_plen"] -= s_len
                self.conn.executemany("DELETE FROM body WHERE term = ? AND pid BETWEEN ? AND ?",
                                      [(t, pid_lo, pid_hi) for t in sorted(all_terms)])
                self.conn.execute("DELETE FROM passages WHERE pid BETWEEN ? AND ?", (pid_lo, pid_hi))
            self.conn.executemany("DELETE FROM names WHERE term = ? AND uid BETWEEN ? AND ?",
                                  [(t, uid_lo, uid_hi) for t in sorted(all_terms | raw_terms)])
            self.conn.execute("DELETE FROM units WHERE uid BETWEEN ? AND ?", (uid_lo, uid_hi))
        self.conn.executemany("DELETE FROM paths WHERE term = ? AND file = ?", [(t, f) for t in _path_tf(f)])
        self.conn.execute("DELETE FROM files WHERE file = ?", (f,))

    def add_file(self, g, f: str, data: bytes, st: os.stat_result, gsig: str) -> int:
        sha = hashlib.sha256(data).hexdigest()
        lines = data.decode("utf-8", errors="replace").splitlines()
        skipped = None
        if len(data) > MAX_FILE_BYTES:
            units: list[_Unit] = []
            skipped = f"larger than {MAX_FILE_BYTES} bytes"
        elif f.lower().endswith(PROSE_SUFFIXES):
            units = _prose_units(g, f, lines)
        else:
            units = _code_units(g, f, lines)
        path_tf = _path_tf(f)
        line_tokens: dict[int, list[str]] = {}

        def toks(i: int) -> list[str]:
            if i not in line_tokens:
                line_tokens[i] = tokens(lines[i - 1])
            return line_tokens[i]

        uid_lo = self.next_uid if units else None
        pid_lo = self.next_pid
        u_rows, p_rows, b_rows, n_rows = [], [], [], []
        for u in units:
            uid = self.next_uid
            self.next_uid += 1
            name_tf: dict[str, int] = defaultdict(int)
            for t in tokens(u.name + " " + u.qual):
                name_tf[t] += 1
            seen = set(name_tf) | set(path_tf)
            p_first = None
            for a, b in _windows(u.own):
                pid = self.next_pid
                self.next_pid += 1
                p_first = pid if p_first is None else p_first
                tf: dict[str, int] = defaultdict(int)
                n = 0
                for i in range(a, b + 1):
                    for t in toks(i):
                        tf[t] += 1
                        n += 1
                p_rows.append((pid, uid, a, b, n))
                b_rows += [(t, pid, c) for t, c in tf.items()]
                seen.update(tf)
                self.totals["n_passages"] += 1
                self.totals["sum_plen"] += n
            name_len = sum(name_tf.values())
            n_rows += [(t, uid, c) for t, c in name_tf.items()]
            raw = raw_name_parts(u.name + " " + u.qual)
            n_rows += [(RAW_PREFIX + r, uid, 1) for r in raw]
            for t in seen:
                self.df_delta[t] += 1
            self.totals["n_units"] += 1
            self.totals["sum_name_len"] += name_len
            u_rows.append((uid, f, u.nid, u.kind, u.name, u.qual, u.a, u.b, len(u.own), u.sig, u.doc,
                           json.dumps(u.consts), name_len, p_first, " ".join(sorted(seen)), " ".join(raw)))
        c = self.conn
        c.executemany("INSERT INTO units VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", u_rows)
        c.executemany("INSERT INTO passages VALUES (?,?,?,?,?)", p_rows)
        c.executemany("INSERT INTO body VALUES (?,?,?)", b_rows)
        c.executemany("INSERT INTO names VALUES (?,?,?)", n_rows)
        c.executemany("INSERT OR REPLACE INTO paths VALUES (?,?,?)", [(t, f, n) for t, n in path_tf.items()])
        c.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (f, sha, gsig, st.st_size, st.st_mtime_ns, len(lines), uid_lo,
                   (self.next_uid - 1) if units else None, pid_lo if p_rows else None,
                   (self.next_pid - 1) if p_rows else None, skipped))
        return len(units)

    def finish(self, **meta) -> None:
        if self.df_delta:
            items = sorted(self.df_delta.items())
            self.conn.executemany("INSERT INTO df(term, df) VALUES (?, 0) ON CONFLICT(term) DO NOTHING",
                                  [(t,) for t, _ in items])
            self.conn.executemany("UPDATE df SET df = df + ? WHERE term = ?", [(d, t) for t, d in items if d])
            self.conn.execute("DELETE FROM df WHERE df <= 0")
        _set_meta(self.conn, **self.totals, **meta)


def _open_for_write(db: Path, rebuild: bool) -> tuple[sqlite3.Connection, bool]:
    """Open (creating when needed) and say whether the index must be built from scratch."""
    fresh = rebuild or not db.exists()
    if db.exists() and not fresh:
        conn = _connect(db)
        m = _meta(conn)
        if m.get("schema_version") != SCHEMA_VERSION or m.get("tokenizer_version") != TOKENIZER_VERSION:
            fresh = True
        else:
            conn.executescript(_SCHEMA)
            return conn, False
        conn.close()
    if fresh and db.exists():
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(db) + suffix).unlink()
            except FileNotFoundError:
                pass
            except OSError:  # held open elsewhere: empty it in place instead
                conn = _connect(db)
                for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                    conn.execute(f"DROP TABLE IF EXISTS {name}")
                conn.commit()
                conn.close()
                break
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db)
    conn.executescript(_SCHEMA)
    return conn, True


def update(repo: Path, graph=None, changed=None, *, rebuild: bool = False, db: Path | None = None) -> dict:
    """Bring ``search.db`` in line with the graph and the files; return what was done.

    ``changed`` (repo-relative paths or absolute Paths) is a hint from the
    snapshot diff; correctness does not depend on it: every indexed file whose
    stat changed is re-hashed, a file is re-indexed when its sha256 or its
    graph signature differs, and files that left the graph are removed.
    ``rebuild`` (or a schema/tokenizer version change) builds from scratch.
    """
    from verinoda import index as ix

    t0 = time.perf_counter()
    repo = Path(repo).resolve()
    g = graph if graph is not None else ix.load(repo, augment=False)
    db = Path(db) if db else db_path_for(g)
    conn, fresh = _open_for_write(db, rebuild)
    try:
        stored = {} if fresh else {r[0]: r[1:] for r in conn.execute(
            "SELECT file, sha256, gsig, size, mtime_ns FROM files")}
        hint = set()
        for p in changed or ():
            pp = Path(p)
            try:
                hint.add(pp.resolve().relative_to(repo).as_posix() if pp.is_absolute() else pp.as_posix())
            except ValueError:
                continue
        files = _graph_files(g)
        w = _Writer(conn)
        indexed = removed = unchanged = 0
        for f in files:
            p = repo / f
            try:
                st = p.stat()
            except OSError:
                continue
            gsig = _gsig(g, f)
            prev = stored.get(f)
            if prev and prev[1] == gsig and f not in hint and prev[2] == st.st_size and prev[3] == st.st_mtime_ns:
                unchanged += 1
                continue
            try:
                data = p.read_bytes()
            except OSError:
                continue
            if prev and prev[1] == gsig and prev[0] == hashlib.sha256(data).hexdigest():
                conn.execute("UPDATE files SET size = ?, mtime_ns = ? WHERE file = ?",
                             (st.st_size, st.st_mtime_ns, f))
                unchanged += 1
                continue
            if prev:
                w.remove_file(f)
            w.add_file(g, f, data, st, gsig)
            indexed += 1
        live = set(files)
        for f in stored:
            if f not in live or not (repo / f).exists():
                w.remove_file(f)
                removed += 1
        from verinoda.index import graph_identity

        m = _meta(conn)
        generation = int(m.get("generation", 0)) + (1 if (indexed or removed or fresh) else 0)
        w.finish(schema_version=SCHEMA_VERSION, tokenizer_version=TOKENIZER_VERSION,
                 graph=graph_identity(g.path), generation=generation,
                 built_at=m.get("built_at") if not fresh and m.get("built_at") else time.time())
        conn.commit()
        n_units = w.totals["n_units"]
    finally:
        conn.close()
    _HANDLES.pop(str(db), None)
    return {"mode": "full" if fresh else ("incremental" if indexed or removed else "noop"),
            "files_indexed": indexed, "files_removed": removed, "files_unchanged": unchanged,
            "units": n_units, "generation": generation, "db": str(db),
            "seconds": round(time.perf_counter() - t0, 3)}


def build(repo: Path, graph=None) -> dict:
    """Build ``search.db`` from scratch."""
    return update(repo, graph, rebuild=True)


# -- opening for queries --------------------------------------------------------------------------

@dataclass
class Handle:
    """What a query needs from an index, cached per process and generation."""

    db: str
    meta: dict
    units: dict[int, tuple]            # uid -> (file, nid, kind, name, a, b, name_len, p_lo)
    nid_uid: dict[str, int]
    memory: sqlite3.Connection | None = None   # in-memory fallback (read-only index directory)
    notes: list[str] = field(default_factory=list)

    def connect(self) -> sqlite3.Connection:
        return self.memory if self.memory is not None else sqlite3.connect(self.db, timeout=30)

    def release(self, conn: sqlite3.Connection) -> None:
        if conn is not self.memory:
            conn.close()

    @property
    def n_units(self) -> int:
        return int(self.meta.get("n_units", 0))


_HANDLES: dict[str, Handle] = {}


def _load_handle(db: str, conn: sqlite3.Connection, memory: sqlite3.Connection | None = None) -> Handle:
    meta = _meta(conn)
    units = {r[0]: r[1:] for r in conn.execute(
        "SELECT uid, file, nid, kind, name, a, b, name_len, p_lo FROM units")}
    nid_uid: dict[str, int] = {}
    for uid, row in sorted(units.items()):
        if row[1] and row[1] not in nid_uid:
            nid_uid[row[1]] = uid
    return Handle(db, meta, units, nid_uid, memory)


def open_for(g, *, sync: bool = True) -> Handle:
    """The index for graph ``g``, brought up to date first when it describes another graph.

    A missing, outdated or foreign ``search.db`` is updated in place (only the
    files that changed are re-indexed). When the index directory cannot be
    written, an in-memory index is built for this process and a note says so.
    The loaded unit table is cached per process and index generation.
    """
    from verinoda.index import graph_identity, same_graph

    db = db_path_for(g)
    key = str(db)
    cached = _HANDLES.get(key)
    if cached is not None and cached.memory is not None and cached.meta.get("_graph") == str(g.path):
        return cached
    notes: list[str] = []
    for attempt in (0, 1):
        m: dict = {}
        if db.exists():
            conn = sqlite3.connect(str(db), timeout=30)
            try:
                m = _meta(conn)
                ok = (m.get("schema_version") == SCHEMA_VERSION and m.get("tokenizer_version") == TOKENIZER_VERSION
                      and same_graph(g.path, m.get("graph")))
                if ok:
                    if (cached is not None and cached.memory is None
                            and cached.meta.get("generation") == m.get("generation")
                            and cached.meta.get("graph") == m.get("graph")):
                        return cached
                    h = _load_handle(key, conn)
                    h.notes = notes
                    _HANDLES[key] = h
                    return h
            finally:
                conn.close()
        if attempt or not sync:
            break
        try:
            res = update(g.root, g, db=db)
            notes.append(f"search index {res['mode']} update ({res['files_indexed']} files, {res['seconds']} s)")
        except (OSError, sqlite3.Error) as exc:
            notes.append(f"search index not writable ({type(exc).__name__}); built in memory")
            break
    mem = sqlite3.connect(":memory:", check_same_thread=False)
    mem.executescript(_SCHEMA)
    w = _Writer(mem)
    for f in _graph_files(g):
        p = g.root / f
        try:
            w.add_file(g, f, p.read_bytes(), p.stat(), _gsig(g, f))
        except OSError:
            continue
    w.finish(schema_version=SCHEMA_VERSION, tokenizer_version=TOKENIZER_VERSION,
             graph=graph_identity(g.path), generation=1, built_at=time.time())
    mem.commit()
    h = _load_handle(":memory:", mem, memory=mem)
    h.meta["_graph"] = str(g.path)
    h.notes = notes or ["search index built in memory"]
    _HANDLES[key] = h
    return h


# -- query side -----------------------------------------------------------------------------------

@dataclass
class QueryTerms:
    weights: dict[str, float]               # term -> weight (1.0 for the user's own words)
    words: list[str]                        # content words kept from the question
    named: list[str]                        # identifiers the question spells as code
    expansions: list[dict]                  # {"from", "to", "via", "weight"}
    dropped: list[str]                      # stopwords and fragments removed


def _vocab_has(conn: sqlite3.Connection, terms: list[str]) -> dict[str, int]:
    if not terms:
        return {}
    out: dict[str, int] = {}
    for k in range(0, len(terms), 900):
        chunk = terms[k:k + 900]
        out.update(conn.execute(f"SELECT term, df FROM df WHERE term IN ({','.join('?' * len(chunk))})",
                                chunk).fetchall())
    return out


def _vocab_prefixed(conn: sqlite3.Connection, prefix: str, limit: int = 4) -> list[tuple[str, int]]:
    rows = conn.execute("SELECT term, df FROM df WHERE term >= ? AND term < ? ORDER BY df DESC, term LIMIT ?",
                        (prefix, prefix + "￿", limit)).fetchall()
    return [(t, d) for t, d in rows]


def _lexicon_expansions(repo: Path | None, words: list[str]) -> dict[str, list[str]]:
    """Repo-learned associations and grounded seed glosses (docs/DESIGN.md D6) for folded words.

    Uses :mod:`verinoda.lexicon` when it is installed and a lexicon was built;
    otherwise returns nothing. Lexicon pairs only ever widen the search; they
    are never evidence.
    """
    if repo is None or not words:
        return {}
    try:
        from verinoda import lexicon  # other track: may be absent
        lx = lexicon.load(repo)
    except Exception:  # noqa: BLE001 - no lexicon, or an unreadable one: no expansions
        return {}
    if lx is None:
        return {}
    out: dict[str, list[str]] = {}
    try:
        assoc = getattr(lx, "associations", None)
        if callable(assoc):
            for w in words:
                parts = [str(a.get("part")) for a in (assoc(w) or [])[:3] if isinstance(a, dict) and a.get("part")]
                if parts:
                    out.setdefault(w, []).extend(parts)
        seed = getattr(lx, "seed", None)
        if callable(seed):
            for item in seed(words) or []:
                src = " ".join(words[item["start"]: item["start"] + item.get("n", 1)])
                if item.get("targets"):
                    out.setdefault(src, []).extend(str(t) for t in item["targets"])
    except Exception:  # noqa: BLE001 - a malformed lexicon never breaks retrieval
        return {}
    return {k: list(dict.fromkeys(v)) for k, v in out.items() if v}


def analyze_query(question: str, conn: sqlite3.Connection, *, expansions: dict[str, list[str]] | None = None,
                  repo: Path | None = None) -> QueryTerms:
    """Question -> weighted index terms, with every expansion reported."""
    named = named_identifiers(question)
    words: list[str] = []
    dropped: list[str] = []
    for raw in question.split():
        base, suffix = textnorm.split_apostrophe(raw.strip(".,;:!?()[]{}\"`"))
        if suffix:
            dropped.append("'" + suffix.rstrip(".,;:!?()[]{}\"`"))
        for w in _WORD.findall(base):
            f = textnorm.fold_tr(w)
            if f in EN_STOPWORDS or f in textnorm.TR_STOPWORDS:
                dropped.append(w)
                continue
            if len(f) < 3 and f not in textnorm.SHORT_TECH and "_" not in w:
                dropped.append(w)
                continue
            words.append(w)
    if not words:  # a question made only of stopwords still searches on something
        words = [w for raw in textnorm.raw_tokens(question) for w in _WORD.findall(raw) if len(w) >= 2]
    weights: dict[str, float] = {}
    for w in words:
        for t in word_tokens(w):
            weights.setdefault(t, 1.0)
    have = _vocab_has(conn, list(weights))
    exps: list[dict] = []

    def add(src: str, term: str, via: str, weight: float, *, indexed: bool = True) -> None:
        """``term`` is an index token (``indexed``) or a plain word to tokenize first."""
        for t in (term,) if indexed else word_tokens(term):
            if t in weights or t == src:
                continue
            weights[t] = weight
            exps.append({"from": src, "to": t, "via": via, "weight": weight})

    # Turkish words the index does not know: vocabulary-confirmed stem, then its corpus terms.
    tr_question = textnorm.has_turkish(question)
    for w in words:
        f = textnorm.fold_tr(w)
        if (tr_question or not w.isascii()) and f not in have and len(f) >= 4 and f.isalpha():
            st = textnorm.tr_stem(f, lambda p: bool(_vocab_prefixed(conn, p, 1)))
            if st != f and len(st) >= 3:
                for term, _df in _vocab_prefixed(conn, st, 3):
                    add(w, term, f"turkish stem '{st}'", EXPANSION_WEIGHT)
    # Abbreviations: fixed map (both directions) and corpus prefixes of long words.
    base_terms = list(weights)
    cand: dict[str, tuple[str, str]] = {}
    prefixes: dict[str, str] = {}
    for t in base_terms:
        if not t.isalpha():
            continue
        for long_form in ABBREVIATIONS.get(t, ()) + ABBREVIATIONS_ONE_WAY.get(t, ()):
            cand.setdefault(word_stem(long_form), (t, "abbreviation"))
        for short, longs in ABBREVIATIONS.items():
            if t in {word_stem(x) for x in longs}:
                cand.setdefault(short, (t, "abbreviation"))
        if len(t) >= 6:
            for k in range(max(3, math.ceil(0.4 * len(t))), len(t)):
                if t[:k] not in NOT_ABBREVIATIONS and not t[k:].startswith(_DERIVATIONAL):
                    prefixes.setdefault(t[:k], t)
    known = _vocab_has(conn, [c for c in cand if c not in weights])
    for c, (src, via) in cand.items():
        if c in known and c not in weights:
            add(src, c, "abbreviation", EXPANSION_WEIGHT)
    # A strict prefix of a long word counts as its abbreviation only when some
    # symbol or constant is named with it (``env`` in ``ENV_ALLOW``), not when
    # it merely occurs in text (``comma`` for ``command``).
    names = _fetch(conn, "SELECT DISTINCT term FROM names WHERE term IN ({ph})",
                   sorted(RAW_PREFIX + c for c in prefixes if c not in weights))
    for (c,) in names:
        c = c[len(RAW_PREFIX):]
        if c in known or _vocab_has(conn, [c]):
            add(prefixes[c], c, "corpus prefix", EXPANSION_WEIGHT)
    provided = dict(expansions or {})
    if not provided and tr_question:
        provided = _lexicon_expansions(repo, [textnorm.fold_tr(w) for w in words])
        via_default = "lexicon"
    else:
        via_default = "question plan"
    for src, targets in provided.items():
        for tgt in targets or ():
            add(str(src), str(tgt), via_default, PROVIDED_EXPANSION_WEIGHT, indexed=False)
    return QueryTerms(weights, words, named, exps, dropped)


# -- ranking ----------------------------------------------------------------------------------------

@dataclass
class Hit:
    uid: int
    file: str
    nid: str | None
    kind: str
    name: str
    qual: str
    a: int
    b: int
    own: int
    sig: str
    doc: str
    consts: list[tuple[str, int]]
    score: float
    lex: float
    ppr: float
    passages: list[tuple[float, int, int, int]]   # (score, pid, a, b), best first
    name_terms: list[str]
    body_terms: list[str]
    reasons: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.nid or f"{self.file}:{self.a}"


@dataclass
class Ranking:
    hits: list[Hit]
    query: QueryTerms
    candidates: int
    pushes: int
    seconds: float
    notes: list[str]


def _adjacency_cache(g) -> dict:
    """Per-graph cache of weighted neighbour lists (reset when the graph grows or shrinks)."""
    key = (id(g.G), len(g.G))   # verinoda.index drops the cache when it adds edges
    cache = g.__dict__.get("_ppr_adj")
    if cache is None or cache.get("_key") != key:
        cache = g.__dict__["_ppr_adj"] = {"_key": key}
    return cache


def _neighbours(g, nid: str, nid_uid: dict[str, int], cache: dict) -> list[tuple[str, float]]:
    nb = cache.get(nid)
    if nb is None:
        agg: dict[str, float] = defaultdict(float)
        for v, d in g.out_edges(nid):
            w = REL_WEIGHTS.get(d.get("relation"))
            if w and v in nid_uid and v != nid:
                agg[v] += w[0] * (1.0 if d.get("confidence") == "EXTRACTED" else INFERRED_FACTOR)
        for u, d in g.in_edges(nid):
            w = REL_WEIGHTS.get(d.get("relation"))
            if w and u in nid_uid and u != nid:
                agg[u] += w[1] * (1.0 if d.get("confidence") == "EXTRACTED" else INFERRED_FACTOR)
        nb = cache[nid] = sorted(agg.items())
    return nb


def personalized_pagerank(g, seeds: dict[str, float], nid_uid: dict[str, int], *, alpha: float = PPR_ALPHA,
                          eps: float = PPR_EPS, max_push: int = PPR_MAX_PUSH) -> tuple[dict[str, float], int]:
    """Andersen-Chung-Lang local push on the weighted symbol graph (pure Python).

    A node is active while its residual is at least ``eps`` times its weighted
    degree; pushing it keeps ``alpha`` of the residual as PageRank mass and
    spreads the rest to its neighbours by edge weight. Stops when no node is
    active or after ``max_push`` pushes. Deterministic for a given graph.
    """
    tot = sum(seeds.values()) or 1.0
    cache = _adjacency_cache(g)
    r: dict[str, float] = {n: w / tot for n, w in seeds.items()}
    p: dict[str, float] = defaultdict(float)
    deg: dict[str, float] = {}

    def degree(n: str) -> float:
        d = deg.get(n)
        if d is None:
            d = deg[n] = sum(w for _, w in _neighbours(g, n, nid_uid, cache)) or 1.0
        return d

    queue = [n for n in r if r[n] >= eps * degree(n)]
    queued = set(queue)
    pushes = 0
    while queue and pushes < max_push:
        u = queue.pop()
        queued.discard(u)
        ru = r.get(u, 0.0)
        du = degree(u)
        if ru < eps * du:
            continue
        pushes += 1
        p[u] += alpha * ru
        r[u] = 0.0
        spread = (1 - alpha) * ru
        nb = _neighbours(g, u, nid_uid, cache)
        if not nb:
            p[u] += spread
            continue
        for v, w in nb:
            r[v] = r.get(v, 0.0) + spread * w / du
            if v not in queued and r[v] >= eps * degree(v):
                queue.append(v)
                queued.add(v)
    return dict(p), pushes


def _query_pairs(words: list[str]) -> list[tuple[frozenset, frozenset]]:
    """Token sets of consecutive content words of the question ("home directory")."""
    sets = [frozenset(word_tokens(w)) for w in words]
    return [(a, b) for a, b in zip(sets, sets[1:]) if a and b and not (a & b)]


def _near(seq: list[str], a: frozenset, b: frozenset, gap: int = PROX_GAP) -> bool:
    """Do tokens of ``a`` and ``b`` occur at most ``gap`` positions apart in ``seq``?"""
    last_a = last_b = -(10 ** 9)
    for i, t in enumerate(seq):
        if t in a:
            if i - last_b <= gap:
                return True
            last_a = i
        elif t in b:
            if i - last_a <= gap:
                return True
            last_b = i
    return False


def _proximity(g, conn: sqlite3.Connection, h: "Handle", q: "QueryTerms",
               acc: dict[int, dict[str, float]], name_tf: dict[int, dict[str, int]],
               qw: dict[str, float]) -> tuple[set[int], set[int]]:
    """Passages (pids) and unit names (uids) where two adjacent question words occur near each other.

    Only the user's own words count (not expansions). Passage text is re-read from the
    working tree, so a file edited since indexing can only lose or gain this bonus.
    """
    pairs = _query_pairs(q.words)
    if not pairs:
        return set(), set()
    want = set().union(*(a | b for a, b in pairs))

    def has_pair(keys) -> bool:
        return any(keys & a and keys & b for a, b in pairs)

    cand = [pid for pid, a_p in acc.items() if len(want & a_p.keys()) >= 2 and has_pair(a_p.keys())]
    cand.sort(key=lambda pid: (-sum(qw.get(t, 0.0) for t in acc[pid]), pid))
    cand = cand[:PROX_CANDIDATES]
    hit_p: set[int] = set()
    if cand:
        rows = _fetch(conn, "SELECT p.pid, p.a, p.b, u.file, u.kind FROM passages p JOIN units u ON u.uid = p.uid "
                            "WHERE p.pid IN ({ph})", cand)
        lines_of: dict[str, list[str] | None] = {}
        root = Path(getattr(g, "root", None) or ".")
        for pid, a, b, f, kind in rows:
            if kind == "prose":
                continue  # documentation repeats command names ("graphify update") next to each other
            if f not in lines_of:
                try:
                    lines_of[f] = (root / f).read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    lines_of[f] = None
            lines = lines_of[f]
            if not lines:
                continue
            seq = tokens("\n".join(lines[max(0, a - 1):b]))
            if any(_near(seq, x, y) for x, y in pairs):
                hit_p.add(pid)
    hit_u: set[int] = set()
    for uid, ntf in name_tf.items():
        if has_pair(ntf.keys()) and uid in h.units:
            seq = tokens(h.units[uid][3] or "")
            if any(_near(seq, x, y) for x, y in pairs):
                hit_u.add(uid)
    return hit_p, hit_u


def _fetch(conn: sqlite3.Connection, sql: str, keys: list, extra: tuple = ()) -> list:
    out: list = []
    for k in range(0, len(keys), 900):
        chunk = keys[k:k + 900]
        out += conn.execute(sql.format(ph=",".join("?" * len(chunk))), (*extra, *chunk)).fetchall()
    return out


def rank(g, question: str, *, include_tests: bool = True, seeds: dict[str, str] | None = None,
         expansions: dict[str, list[str]] | None = None, limit: int = 60, handle: Handle | None = None,
         ppr: bool = True) -> Ranking:
    """Rank the index's units for ``question`` (lexical BM25F, then the graph prior)."""
    t0 = time.perf_counter()
    h = handle or open_for(g)
    conn = h.connect()
    try:
        q = analyze_query(question, conn, expansions=expansions, repo=g.root)
        dfs = _vocab_has(conn, list(q.weights))
        n = max(1, h.n_units)
        idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in dfs.items()}
        terms = [t for t in q.weights if t in idf]
        avg_len = (h.meta.get("sum_plen", 0) / max(1, h.meta.get("n_passages", 1))) or 1.0
        avg_name = (h.meta.get("sum_name_len", 0) / n) or 1.0
        body = _fetch(conn, "SELECT b.term, b.pid, b.tf, p.uid, p.len FROM body b JOIN passages p "
                            "ON p.pid = b.pid WHERE b.term IN ({ph})", terms) if terms else []
        names = _fetch(conn, "SELECT term, uid, tf FROM names WHERE term IN ({ph})", terms) if terms else []
        paths = _fetch(conn, "SELECT term, file, tf FROM paths WHERE term IN ({ph})", terms) if terms else []
        acc: dict[int, dict[str, float]] = defaultdict(dict)
        p_uid: dict[int, int] = {}
        for t, pid, tf, uid, plen in body:
            acc[pid][t] = tf / ((1 - B_BODY) + B_BODY * plen / avg_len)
            p_uid[pid] = uid
        name_tf: dict[int, dict[str, int]] = defaultdict(dict)
        for t, uid, tf in names:
            name_tf[uid][t] = tf
        path_tf: dict[str, dict[str, int]] = defaultdict(dict)
        for t, f, tf in paths:
            path_tf[f][t] = tf
        unit_pids: dict[int, list[int]] = defaultdict(list)
        for pid, uid in p_uid.items():
            unit_pids[uid].append(pid)
        for uid in name_tf:
            if uid not in unit_pids and uid in h.units and h.units[uid][7] is not None:
                unit_pids[uid].append(h.units[uid][7])
                acc.setdefault(h.units[uid][7], {})
        best: dict[int, float] = {}
        per_unit: dict[int, list[tuple[float, int]]] = {}
        qw = {t: q.weights[t] * idf[t] for t in terms}
        order_of = {t: k for k, t in enumerate(terms)}
        prox_p, prox_u = _proximity(g, conn, h, q, acc, name_tf, qw)
        empty: dict = {}
        for uid, pids in unit_pids.items():
            row = h.units.get(uid)
            if row is None:
                continue
            if not include_tests and is_test_file(row[0]):
                continue
            ntf, ptf = name_tf.get(uid, empty), path_tf.get(row[0], empty)
            bn = (1 - B_NAME) + B_NAME * (row[6] or 1) / avg_name
            fixed = {t: W_NAME * ntf.get(t, 0) / bn + W_PATH * ptf.get(t, 0) for t in {*ntf, *ptf}}
            scored = []
            for pid in pids:
                s = 0.0
                a_p = acc[pid]
                # only the query terms present in this passage or the unit's name/path fields,
                # summed in query order so scores do not depend on dict order
                for t in sorted(a_p.keys() | fixed.keys(), key=order_of.__getitem__):
                    tf = a_p.get(t, 0.0) + fixed.get(t, 0.0)
                    s += qw[t] * tf / (K1 + tf)
                if pid in prox_p or uid in prox_u:
                    s *= 1 + PROX_BOOST
                scored.append((s, pid))
            scored.sort(key=lambda x: (-x[0], x[1]))
            per_unit[uid] = scored
            best[uid] = scored[0][0] if scored else 0.0
        top = max(best.values(), default=0.0) or 1.0
        lex = {uid: s / top for uid, s in best.items() if s > 0}
        reasons: dict[int, list[str]] = defaultdict(list)
        if q.named:
            want = {nm.strip("_").lower(): nm for nm in q.named}
            rows = _fetch(conn, "SELECT uid, name FROM units WHERE uid IN (SELECT uid FROM names WHERE term IN ({ph}))",
                          sorted({t for nm in want for t in word_tokens(nm)}))
            for uid, name in rows:
                nm = want.get(name.strip("_").lower())
                if nm and uid in h.units and h.units[uid][2] == "symbol" and \
                        (include_tests or not is_test_file(h.units[uid][0])):
                    lex[uid] = max(lex.get(uid, 0.0), 1.0)
                    reasons[uid].append(f"question names '{nm}'")
                    if uid not in per_unit and h.units[uid][7] is not None:
                        per_unit[uid] = [(0.0, h.units[uid][7])]
        for nid, why in (seeds or {}).items():
            uid = h.nid_uid.get(nid)
            if uid is None or (not include_tests and is_test_file(h.units[uid][0])):
                continue
            lex[uid] = max(lex.get(uid, 0.0), 1.0)
            reasons[uid].append(f"plan: {why}" if why else "plan seed")
            if uid not in per_unit and h.units[uid][7] is not None:
                per_unit[uid] = [(0.0, h.units[uid][7])]
        score = dict(lex)
        ppr_mass: dict[int, float] = {}
        pushes = 0
        if ppr and lex:
            seed_w: dict[str, float] = {}
            for uid in sorted(lex, key=lambda u: (-lex[u], u))[:PPR_SEEDS]:
                nid = h.units[uid][1]
                if nid and nid in g.G:
                    seed_w[nid] = lex[uid] ** 2
            for nid in seeds or {}:
                if nid in g.G and nid in h.nid_uid:
                    seed_w[nid] = max(seed_w.get(nid, 0.0), 1.0)
            if seed_w:
                mass, pushes = personalized_pagerank(g, seed_w, h.nid_uid)
                for nid, m in mass.items():
                    uid = h.nid_uid.get(nid)
                    if uid is None or (not include_tests and is_test_file(h.units[uid][0])):
                        continue
                    bonus = PPR_LAMBDA * m / (m + PPR_KAPPA)
                    ppr_mass[uid] = bonus
                    score[uid] = score.get(uid, 0.0) + bonus
        order = sorted(score, key=lambda u: (-round(score[u], 9), h.units[u][0], h.units[u][4], h.units[u][2],
                                             h.units[u][3], u))
        chosen = order[:limit]
        full = {r[0]: r for r in _fetch(conn, "SELECT uid, qual, own, sig, doc, consts FROM units WHERE uid IN ({ph})",
                                        chosen)}
        need = sorted({pid for uid in chosen for _, pid in per_unit.get(uid, [])[:6]}
                      | {h.units[uid][7] for uid in chosen if h.units[uid][7] is not None})
        prng = {r[0]: (r[1], r[2]) for r in _fetch(conn, "SELECT pid, a, b FROM passages WHERE pid IN ({ph})", need)}
        hits: list[Hit] = []
        for uid in chosen:
            file, nid, kind, name, a, b, _nl, p_lo = h.units[uid]
            _, qual, own, sig, doc, consts = full[uid]
            ps = [(s, pid, *prng[pid]) for s, pid in per_unit.get(uid, [])[:6] if pid in prng]
            if not ps and p_lo in prng:
                ps = [(0.0, p_lo, *prng[p_lo])]
            best_pid = ps[0][1] if ps else None
            hits.append(Hit(uid, file, nid, kind, name, qual, a, b, own, sig, doc,
                            [tuple(c) for c in json.loads(consts or "[]")], score[uid], lex.get(uid, 0.0),
                            ppr_mass.get(uid, 0.0), ps, sorted(name_tf.get(uid, {})),
                            sorted(acc.get(best_pid, {})) if best_pid is not None else [], reasons.get(uid, [])))
        return Ranking(hits, q, len(score), pushes, time.perf_counter() - t0, list(h.notes))
    finally:
        h.release(conn)


def stale_files(h: Handle, root: Path, files: list[str]) -> list[str]:
    """Files among ``files`` whose content differs from what the index saw."""
    conn = h.connect()
    try:
        rows = {r[0]: r[1:] for r in _fetch(conn, "SELECT file, sha256, size, mtime_ns FROM files WHERE file IN ({ph})",
                                            sorted(set(files)))}
    finally:
        h.release(conn)
    out = []
    for f in sorted(set(files)):
        row = rows.get(f)
        p = Path(root) / f
        try:
            st = p.stat()
        except OSError:
            out.append(f)
            continue
        if row is None:
            continue
        if row[1] == st.st_size and row[2] == st.st_mtime_ns:
            continue
        try:
            if hashlib.sha256(p.read_bytes()).hexdigest() != row[0]:
                out.append(f)
        except OSError:
            out.append(f)
    return out


def dump(db: Path) -> dict:
    """Canonical content of an index, independent of uid/pid numbering (tests compare builds)."""
    conn = sqlite3.connect(str(db))
    try:
        units = {r[0]: r[1:] for r in conn.execute(
            "SELECT uid, file, nid, kind, name, qual, a, b, own, sig, doc, consts, name_len, terms, raw FROM units")}
        ukey = {uid: (r[0], r[5], r[6], r[2], r[3]) for uid, r in units.items()}
        passages = {r[0]: (ukey[r[1]], r[2], r[3], r[4]) for r in conn.execute("SELECT pid, uid, a, b, len FROM passages")}
        def key(row):
            return tuple("" if v is None else v for v in row)

        return {
            "units": sorted(units.values(), key=key),
            "passages": sorted(passages.values(), key=lambda r: (key(r[0]), r[1:])),
            "body": sorted(((t, passages[pid], tf) for t, pid, tf in conn.execute("SELECT term, pid, tf FROM body")),
                           key=lambda r: (r[0], key(r[1][0]), r[1][1:], r[2])),
            "names": sorted(((t, ukey[uid], tf) for t, uid, tf in conn.execute("SELECT term, uid, tf FROM names")),
                            key=lambda r: (r[0], key(r[1]), r[2])),
            "paths": sorted(conn.execute("SELECT term, file, tf FROM paths").fetchall()),
            "df": sorted(conn.execute("SELECT term, df FROM df").fetchall()),
            "totals": {k: v for k, v in _meta(conn).items() if k in ("n_units", "n_passages", "sum_plen", "sum_name_len")},
        }
    finally:
        conn.close()
