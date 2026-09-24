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
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath

from verinoda import textnorm
from verinoda.architecture_map import is_test_file

SCHEMA_VERSION = 4
TOKENIZER_VERSION = 3  # 3: y-final words meet their -ies/-ied forms (query/queries)
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
TR_STEM_WEIGHT = 0.8              # the vocabulary-confirmed stem of an inflected Turkish word (the user's word)
# Turkish derivational endings (folded) change the meaning of a stem: -ci/-cu (agent), -lik (-ness),
# -siz (without), -li (with), -ce (-ly)
TR_DERIVATIONAL = ("ci", "cu", "lik", "luk", "siz", "suz", "li", "lu", "ce", "ca")
TR_MIN_STEM = 4                   # a shorter "stem" is mostly an English stem (çalışıyor -> cal, veri -> ver)
TR_MIN_STEM_KNOWN = 5             # for words the index knows as they are (login -> log, event -> even)
LINK_PPR_FACTOR = 0.5             # graph prior a data unit takes from the code units linked to it
LINK_FAN_POWER = 0.0              # ... divided by (data units that code unit links to) ** this; 1.0 (a hub
                                  # shares its prior) was tried 2026-09-24: forge_mod JSON -4 facts, not kept
PROX_BOOST = 0.3                  # code passage factor when two adjacent question words are adjacent in it
PROX_GAP = 1                      # "adjacent": at most this many tokens apart, in either order
PROX_CANDIDATES = 300             # passages checked for proximity, best term coverage first
MAX_FILE_BYTES = 4_000_000        # larger files are not indexed (reported in stats)
PROSE_SUFFIXES = (".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc")
# Text files the graph has no nodes for (data packs, configs, shaders, resources) are indexed
# as "data" units, so a question can reach them and the code that names them.
DATA_SUFFIXES = (".mcfunction", ".mcmeta", ".json", ".jsonc", ".json5", ".snbt", ".yml", ".yaml", ".toml",
                 ".ini", ".cfg", ".conf", ".properties", ".xml", ".sql", ".graphql", ".proto", ".fsh", ".vsh",
                 ".glsl", ".hlsl", ".csv", ".tsv", ".lang", ".gradle", ".kts", ".cmake", ".mk", ".bat", ".cmd",
                 ".ps1", ".sh", ".html", ".css", ".scss", *PROSE_SUFFIXES,
                 # source files the graph extractor skipped (it ignores directories such as build/)
                 ".java", ".kt", ".scala", ".groovy", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go",
                 ".rs", ".rb", ".php", ".cs", ".c", ".h", ".cc", ".cpp", ".hpp", ".swift", ".lua", ".dart")
DATA_NAMES = frozenset({"Dockerfile", "Makefile", "Procfile", "Jenkinsfile", ".env.example"})
BINARY_SUFFIXES = frozenset(""".png .jpg .jpeg .gif .webp .avif .ico .bmp .tga .psd .svgz .ogg .wav .mp3 .flac .mp4 .webm
    .nbt .dat .mca .mcr .schem .schematic .litematic .zip .gz .tgz .xz .7z .rar .jar .class .war .bin .exe .dll .so
    .dylib .pdb .pyc .whl .glb .fbx .blend .ttf .otf .woff .woff2 .pdf .db .sqlite""".split())
LOCK_NAMES = frozenset({"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
                        "Cargo.lock", "composer.lock", "Gemfile.lock", "uv.lock", "Pipfile.lock"})
MAX_DATA_BYTES = 512_000          # larger data files are listed as not indexed
MAX_DATA_LINE = 4_000             # a data file with longer lines is minified / generated
# Tabular / tree data outside a resource pack (benchmark results, fixtures, exports) is mostly
# generated; above this size it is listed as not indexed instead of matching every question.
BULK_DATA_SUFFIXES = (".json", ".jsonc", ".json5", ".csv", ".tsv", ".xml", ".snbt", ".sql")
MAX_BULK_DATA_BYTES = 64_000
# Captured tool output kept in the repository (benchmark runs, logs, test snapshots): not written
# by hand, and it repeats the words of the code it describes.
GENERATED_DIRS = frozenset({"results", "raw", "output", "outputs", "logs", "log", "cassettes", "snapshots",
                            "__snapshots__", "reports", "coverage", "htmlcov", "golden", "goldens"})
GENERATED_SUFFIXES = (".txt", ".json", ".csv", ".tsv", ".xml", ".html", ".out", ".snap")
MAX_DATA_SECTIONS = 60            # a config file with more top-level sections stays one unit
LINK_SEEDS = 8                    # top lexical units whose resource links are followed
LINK_LAMBDA = 0.35                # score a linked unit gains, times its source's lexical score
LINK_FANOUT = 12                  # references followed per unit and direction
REFERENCE_FACTOR = 0.6            # score factor for units under config index.reference roots
REFERENCE_REASON = "in a reference tree (config index.reference)"
UNSURE_LINK_FACTOR = 0.5          # a link whose namespace or resource kind the line does not state
# Data files that are neither configuration nor a pack/mod resource (dictionaries, fixtures,
# exports) repeat many words of any question; they count less unless the question names the file.
DATA_FACTOR = 0.6
CONFIG_SUFFIXES = (".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".properties", ".env.example",
                   ".gradle", ".kts", ".mcmeta")
MAX_SIG_CHARS = 220
MAX_DOC_CHARS = 240

IMPORT_RE = re.compile(r"^\s*(from\s+\S+\s+)?import\s|^\s*(#include|using\s+[\w.]+;|require\s*\()")
ASSIGN_RE = re.compile(r"^(?:export\s+)?(?:const\s+|let\s+|var\s+)?([A-Za-z_$][\w$]*)\s*(?::[^=]*)?=(?![=>])")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?:\(\))?")
# "init and scan commands", "`scan` komutu": the words before a command noun are command names
_CMD_WORD = r"`?[A-Za-z][\w-]*`?"
_COMMAND_PHRASE = re.compile(
    rf"({_CMD_WORD}(?:\s*(?:,|\band\b|\bor\b|\bve\b|\bveya\b|\bile\b)\s*{_CMD_WORD})*)\s+"
    r"(?:sub-?|alt\s+)?(?:commands?\b|komut\w*)", re.I)
_NOT_COMMANDS = frozenset("""the this that these those which what each every any all some a an its their
    same other new cli shell terminal git bu su o hangi her tum butun ayni diger bir""".split())
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
    "cmd": ("command",), "cmds": ("commands",),
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
_VOWELS_Y = frozenset("aeiouy")


def word_stem(part: str) -> str:
    """:func:`stem`, then a final ``e`` dropped so inflections meet their base form.

    ``stem`` maps ``saved`` to ``sav`` and ``stored`` to ``stor`` but leaves
    ``save`` and ``store``; dropping the ``e`` of the base form (keeping at
    least three letters, and never producing a common short word such as
    ``not`` from ``note``) makes ``save``/``saved``/``saves``,
    ``cache``/``cached``/``caches`` and ``update``/``updated`` one token.

    A consonant and ``y`` end the same stem as ``-ies``/``-ied``: ``query``, ``queries`` and
    ``querying`` are ``queri``, ``copy``/``copies``/``copied`` are ``copi`` (``stem`` alone made
    ``query``/``quer`` and ``copy``/``copi``, so a question never met the plural in the code); a short
    ``keys``/``days`` loses its ``s``.
    """
    t = stem(part)
    if t.isalpha() and part.isalpha():
        if len(part) >= 5 and part.endswith(("ies", "ied")):
            return part[:-3] + "i"
        if len(t) >= 3 and t.endswith("y") and t[-2] not in _VOWELS_Y:
            return t[:-1] + "i"
        if len(part) == 4 and part.endswith("ys") and part[-3] in _VOWELS_Y:
            return part[:-1]
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


def qualified_owners(question: str) -> dict[str, set[str]]:
    """``Wisp.spawn`` in the question -> ``{"spawn": {"Wisp"}}``: the owner (class, module or
    object) the question writes before a name."""
    out: dict[str, set[str]] = defaultdict(set)
    for m in IDENT_RE.finditer(question):
        tok = m.group(0)
        tok = tok[:-2] if tok.endswith("()") else tok
        parts = tok.split(".")
        if len(parts) > 1 and parts[-1].lower() not in FILE_EXTS and all(parts[-2:]):
            out[parts[-1].lower()].add(parts[-2])
    return out


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
CREATE TABLE IF NOT EXISTS refs (file TEXT NOT NULL, line INTEGER NOT NULL, target TEXT NOT NULL, rid TEXT NOT NULL,
                                 form TEXT NOT NULL, sure INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS refs_file ON refs(file, line);
CREATE INDEX IF NOT EXISTS refs_target ON refs(target);
CREATE TABLE IF NOT EXISTS unindexed (file TEXT PRIMARY KEY, reason TEXT NOT NULL) WITHOUT ROWID;
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


def _read_lines(p: Path) -> list[str] | None:
    try:
        return p.read_bytes().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None


def _ignore_rules(repo: Path):
    """``rel -> bool``: excluded by the .graphifyignore chain, as the graph extractor reads it.

    Inside git, ``.gitignore`` is already applied by ``git ls-files``; outside git it is read
    here too. Nested ignore files are loaded along each path's own folders.
    """
    from verinoda.project_index import detect

    use_gitignore = not (repo / ".git").exists()
    try:
        patterns = detect._load_graphifyignore(repo, gitignore=use_gitignore)
    except OSError:
        patterns = []
    loaded = {repo}
    cache: dict = {}

    def ignored(rel: str) -> bool:
        anc = repo
        for part in rel.split("/")[:-1]:
            anc = anc / part
            if anc not in loaded:
                loaded.add(anc)
                try:
                    patterns.extend(detect._load_dir_own_ignore(anc, gitignore=use_gitignore))
                except OSError:
                    pass
        return bool(patterns) and detect._is_ignored(repo / rel, repo, patterns, _cache=cache)

    return ignored


def _data_files(repo: Path, graph_files: list[str]) -> tuple[list[str], dict[str, str], list[str]]:
    """``(data files, {not indexed file: reason}, every repository file)``.

    Data files are text files the graph has no node for, by suffix (:data:`DATA_SUFFIXES`);
    the others are listed with the reason they are not indexed.
    """
    from verinoda import resources
    from verinoda.project_index import detect
    from verinoda.snapshot import list_files

    in_graph = set(graph_files)
    files = list_files(repo)
    ignored = _ignore_rules(repo)
    data: list[str] = []
    skipped: dict[str, str] = {}
    for f in files:
        if f in in_graph:
            continue
        name = f.rsplit("/", 1)[-1]
        suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        if suffix in BINARY_SUFFIXES:
            skipped[f] = "binary"
            continue
        if name in LOCK_NAMES or name.endswith((".min.js", ".min.css")):
            skipped[f] = "lock or minified file"
            continue
        if suffix not in DATA_SUFFIXES and name not in DATA_NAMES:
            skipped[f] = f"type not indexed ({suffix or 'no suffix'})"
            continue
        # the graph's own exclusions hold here too: secrets are never read into the index,
        # nor .graphifyignore'd paths or dependency/output folders (a tracked build/ or
        # dist/ is kept: list_files lets only tracked ones through)
        if detect._is_sensitive(Path(f)):
            skipped[f] = "may hold secrets, skipped as the graph skips it"
            continue
        noise = [d for d in f.split("/")[:-1] if d in detect._SKIP_DIRS or d.endswith(".egg-info")]
        if any(d not in ("build", "dist") for d in noise):
            skipped[f] = f"in a dependency or output folder ({next(d for d in noise if d not in ('build', 'dist'))}/)"
            continue
        if ignored(f):
            skipped[f] = "excluded by .graphifyignore"
            continue
        gen = next((d for d in f.split("/")[:-1] if d.lower() in GENERATED_DIRS), None)
        if gen and suffix in GENERATED_SUFFIXES:
            skipped[f] = f"generated output ({gen}/)"
            continue
        try:
            size = (repo / f).stat().st_size
        except OSError:
            continue
        if size > MAX_DATA_BYTES:
            skipped[f] = f"larger than {MAX_DATA_BYTES // 1000} KB"
            continue
        if size > MAX_BULK_DATA_BYTES and suffix in BULK_DATA_SUFFIXES and not resources.RES_PATH.search(f):
            skipped[f] = f"large data file over {MAX_BULK_DATA_BYTES // 1000} KB, likely generated"
            continue
        data.append(f)
    return data, skipped, files


MISALIGNED_PREFIX = "graph-mismatch:"
ALIGN_WINDOW = 8                  # lines after a symbol's start in which its name must appear
ALIGN_SUFFIXES = (".py", ".java", ".kt", ".kts", ".scala", ".groovy", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
                  ".go", ".rs", ".cs", ".rb", ".php", ".swift", ".c", ".cc", ".cpp", ".h", ".hpp", ".lua", ".dart")
_ALIGN_NAME = re.compile(r"[A-Za-z_$][\w$]*")


def _declaration_line(lines: list[str], a: int) -> int:
    """0-based index of the first line at or after line ``a`` that is not an annotation or decorator
    (with its parenthesised arguments), a comment or blank: where the declaration itself begins."""
    i, depth, last = max(0, a - 1), 0, min(len(lines), a - 1 + 400)
    while i < last:
        s = lines[i].strip()
        if depth > 0:
            depth = max(0, depth + s.count("(") + s.count("{") - s.count(")") - s.count("}"))
            i += 1
            continue
        if s.startswith("@"):
            depth = max(0, s.count("(") + s.count("{") - s.count(")") - s.count("}"))
        elif s and not s.startswith(("//", "/*", "*", "#")):
            break
        i += 1
    return i


def _misaligned(units: list["_Unit"], lines: list[str], f: str) -> bool:
    """Most symbols of ``f`` do not start where the graph says: their names are not in the first
    :data:`ALIGN_WINDOW` lines of their declarations (Java/Kotlin spans start at the annotations,
    which may run longer than that: they are skipped first)."""
    if not f.lower().endswith(ALIGN_SUFFIXES):
        return False
    checked = bad = 0
    for u in units:
        if u.kind != "symbol" or not _ALIGN_NAME.fullmatch(u.name or ""):
            continue
        checked += 1
        start = _declaration_line(lines, u.a)
        if u.name not in "\n".join(lines[max(0, u.a - 1): start + ALIGN_WINDOW]):
            bad += 1
    return checked > 0 and bad * 2 > checked


def misaligned_files(db: Path) -> list[str]:
    """Files whose graph spans did not fit their text when they were indexed."""
    try:
        conn = sqlite3.connect(str(db), timeout=30)
        try:
            return [r[0] for r in conn.execute("SELECT file FROM files WHERE sha256 LIKE ? ORDER BY file",
                                               (MISALIGNED_PREFIX + "%",))]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _data_units(f: str, lines: list[str]) -> list["_Unit"]:
    """Units of a data file: config sections (top-level keys / ``[section]``), else the whole file."""
    from verinoda import resources

    keys = resources.resource_keys(f)
    ident = f"{keys[0][0]}:{keys[0][1]}" if keys else Path(f).name
    doc_lines = []
    for ln in lines:  # leading comment block = what the file is for (.mcfunction, yml, sh)
        s = ln.strip()
        if not s.startswith(("#", "//")):
            break
        doc_lines.append(s.lstrip("#/ ").strip())
    doc = _first_paragraph("\n".join(doc_lines))
    starts: list[int] = []
    low = f.lower()
    if low.endswith((".yml", ".yaml")):
        starts = [i for i, ln in enumerate(lines, 1) if re.match(r"^[A-Za-z0-9_.\-\"']+\s*:", ln)]
    elif low.endswith((".toml", ".ini", ".cfg", ".conf")):
        # [section], [[array.table]], with an optional trailing comment
        starts = [i for i, ln in enumerate(lines, 1) if re.match(r"^\[\[?[^\]]+\]\]?\s*([#;].*)?$", ln)]
    units: list[_Unit] = []
    if 2 <= len(starts) <= MAX_DATA_SECTIONS:
        bounds = ([(1, starts[0] - 1)] if starts[0] > 1 else [])
        bounds += [(s, (starts[k + 1] - 1) if k + 1 < len(starts) else len(lines)) for k, s in enumerate(starts)]
        for a, b in bounds:
            own = [i for i in range(a, b + 1) if lines[i - 1].strip()]
            if not own:
                continue
            head = lines[a - 1].strip()
            sec = re.match(r"\[\[?\s*([^\]]+?)\s*\]", head)  # [section] / [[array.table]] # comment
            key = ((sec.group(1) if sec else re.split(r"\s*[:=]", head, maxsplit=1)[0]).strip("\"' ")
                   if a in starts else ident)
            units.append(_Unit(f, None, "data", key[:120] or ident, ident if a in starts else "", a, own[-1], own,
                               sig=head[:MAX_SIG_CHARS], doc=doc if a == 1 else ""))
        return units
    own = [i for i in range(1, len(lines) + 1) if lines[i - 1].strip()]
    if own:
        first = next((lines[i - 1].strip() for i in own if not lines[i - 1].strip().startswith(("#", "//"))),
                     lines[own[0] - 1].strip())
        units.append(_Unit(f, None, "data", ident[:120], "", own[0], own[-1], own, sig=first[:MAX_SIG_CHARS],
                           doc=doc))
    return units


@dataclass
class _Unit:
    file: str
    nid: str | None
    kind: str          # symbol | module | prose | data
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

    def __init__(self, conn: sqlite3.Connection, keymap=None, data_files: set[str] | None = None):
        self.conn = conn
        self.keymap = keymap
        self.data_files = data_files or set()
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
        self.conn.execute("DELETE FROM refs WHERE file = ?", (f,))

    def add_file(self, g, f: str, data: bytes, st: os.stat_result, gsig: str) -> int:
        sha = hashlib.sha256(data).hexdigest()
        lines = data.decode("utf-8", errors="replace").splitlines()
        if lines and lines[0].startswith("\ufeff"):  # a BOM is not part of the first line's text
            lines[0] = lines[0][1:]
        skipped = None
        if len(data) > MAX_FILE_BYTES:
            units: list[_Unit] = []
            skipped = f"larger than {MAX_FILE_BYTES} bytes"
        elif f in self.data_files and not f.lower().endswith(PROSE_SUFFIXES) and \
                any(len(ln) > MAX_DATA_LINE for ln in lines):
            units = []
            skipped = f"minified or generated (a line over {MAX_DATA_LINE} characters)"
        elif f in self.data_files and not f.lower().endswith(PROSE_SUFFIXES):
            units = _data_units(f, lines)
        elif f.lower().endswith(PROSE_SUFFIXES):
            units = _prose_units(g, f, lines)
        else:
            units = _code_units(g, f, lines)
            if _misaligned(units, lines, f):
                # the graph describes another version of this file (it changed while the index
                # was being built): stored so that stale_files and the next update see it
                sha = MISALIGNED_PREFIX + sha
        if skipped is None:
            self.add_refs(f, lines)
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

    def add_refs(self, f: str, lines: list[str]) -> int:
        """Store the resource ids ``f`` names that resolve to repository files (see :mod:`verinoda.resources`)."""
        from verinoda import resources

        if self.keymap is None or not self.keymap.by_key:
            return 0
        data_file = f.lower().endswith(resources.DATA_REF_SUFFIXES)
        rows = []
        for i, text in enumerate(lines, 1):
            if ":" not in text and '"' not in text and "'" not in text:
                continue
            for r in resources.refs_in_line(text, i, data_file=data_file):
                targets = self.keymap.resolve(r)
                sure = int(self.keymap.sure(r, targets))
                for target, _kind in targets:
                    if target != f:
                        rows.append((f, i, target, r.rid, r.form, sure))
        self.conn.executemany("INSERT INTO refs VALUES (?,?,?,?,?,?)", rows)
        return len(rows)

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
        from verinoda import resources

        gfiles = _graph_files(g)
        data_files, not_indexed, repo_files = _data_files(repo, gfiles)
        keymap = resources.KeyMap.build(repo_files)
        key_sig = keymap.signature()
        files = gfiles + data_files
        w = _Writer(conn, keymap, set(data_files))
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
        m = _meta(conn)
        if not fresh and m.get("resource_sig") != key_sig:
            # the defined ids changed: references in unchanged files may now resolve differently
            conn.execute("DELETE FROM refs")
            for (f,) in conn.execute("SELECT file FROM files WHERE skipped IS NULL").fetchall():
                lines = _read_lines(repo / f)
                if lines is not None:
                    w.add_refs(f, lines)
        conn.execute("DELETE FROM unindexed")
        conn.executemany("INSERT INTO unindexed VALUES (?, ?)", sorted(not_indexed.items()))
        # files read but left without units (too large, minified) are not indexed either
        conn.execute("INSERT OR REPLACE INTO unindexed SELECT file, skipped FROM files WHERE skipped IS NOT NULL")
        from verinoda.index import graph_identity

        generation = int(m.get("generation", 0)) + (1 if (indexed or removed or fresh) else 0)
        w.finish(schema_version=SCHEMA_VERSION, tokenizer_version=TOKENIZER_VERSION, resource_sig=key_sig,
                 data_files=len(data_files), not_indexed=len(not_indexed),
                 graph=graph_identity(g.path), generation=generation,
                 built_at=m.get("built_at") if not fresh and m.get("built_at") else time.time())
        conn.commit()
        n_units = w.totals["n_units"]
    finally:
        conn.close()
    _HANDLES.pop(str(db), None)
    return {"mode": "full" if fresh else ("incremental" if indexed or removed else "noop"),
            "files_indexed": indexed, "files_removed": removed, "files_unchanged": unchanged,
            "data_files": len(data_files), "not_indexed": len(not_indexed),
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
    by_file: dict[str, list[tuple[int, int, int, int]]] | None = None   # file -> (span, a, b, uid), innermost first
    copies: dict[str, list[str]] | None = None    # file -> other files with byte-identical content
    unindexed: list[tuple[str, str, frozenset]] | None = None   # (file, reason, name tokens), loaded once
    _pin: threading.local = field(default_factory=threading.local, repr=False, compare=False)

    def connect(self) -> sqlite3.Connection:
        if self.memory is not None:
            return self.memory
        pinned = getattr(self._pin, "conn", None)
        return pinned if pinned is not None else sqlite3.connect(self.db, timeout=30)

    def release(self, conn: sqlite3.Connection) -> None:
        if conn is not self.memory and conn is not getattr(self._pin, "conn", None):
            conn.close()

    @contextmanager
    def pinned(self):
        """This thread's reads share one connection until the block ends (a batch of many small
        lookups, such as an export's notes, would otherwise open and close one per lookup)."""
        if self.memory is not None or getattr(self._pin, "conn", None) is not None:
            yield
            return
        conn = sqlite3.connect(self.db, timeout=30)
        self._pin.conn = conn
        try:
            yield
        finally:
            self._pin.conn = None
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
    from verinoda import resources

    mem = sqlite3.connect(":memory:", check_same_thread=False)
    mem.executescript(_SCHEMA)
    gfiles = _graph_files(g)
    data_files, not_indexed, repo_files = _data_files(Path(g.root), gfiles)
    w = _Writer(mem, resources.KeyMap.build(repo_files), set(data_files))
    for f in gfiles + data_files:
        p = g.root / f
        try:
            w.add_file(g, f, p.read_bytes(), p.stat(), _gsig(g, f))
        except OSError:
            continue
    mem.executemany("INSERT INTO unindexed VALUES (?, ?)", sorted(not_indexed.items()))
    mem.execute("INSERT OR REPLACE INTO unindexed SELECT file, skipped FROM files WHERE skipped IS NOT NULL")
    w.finish(schema_version=SCHEMA_VERSION, tokenizer_version=TOKENIZER_VERSION,
             graph=graph_identity(g.path), generation=1, built_at=time.time(),
             data_files=len(data_files), not_indexed=len(not_indexed))
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


def _load_lexicon(repo: Path | None):
    if repo is None:
        return None
    try:
        from verinoda import lexicon  # other track: may be absent
        return lexicon.load(repo)
    except Exception:  # noqa: BLE001 - no lexicon, or an unreadable one
        return None


def _label_expansions(repo: Path | None, words: list[str]) -> tuple[dict[str, list[str]], set[int]]:
    """Turkish labels from the repository's locale files spelled by adjacent folded words.

    "kor ocağı" under ``block.x.ember_forge`` -> ``{"kor ocagi": ["ember_forge"]}``, and the
    positions of the words it covers. The label names that identifier as surely as if the
    question spelled it; the words' separate associations would only add the other labels
    they occur in.
    """
    lx = _load_lexicon(repo) if words else None
    hits = getattr(lx, "phrase_hits", None)
    if not callable(hits):
        return {}, set()
    out: dict[str, list[str]] = {}
    covered: set[int] = set()
    try:
        for item in hits(words) or []:
            if item.get("targets"):
                src = " ".join(words[item["start"]: item["start"] + item["n"]])
                out.setdefault(src, []).extend(str(x) for x in item["targets"])
                covered.update(range(item["start"], item["start"] + item["n"]))
    except Exception:  # noqa: BLE001 - a malformed lexicon never breaks retrieval
        return {}, set()
    return out, covered


def _lexicon_expansions(repo: Path | None, words: list[str],
                        originals: list[str] | None = None, skip: set[int] | None = None) -> dict[str, list[str]]:
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
            for k, w in enumerate(words):
                if k in (skip or ()):
                    continue  # part of a locale label (_label_expansions)
                parts = [str(a.get("part")) for a in (assoc(w) or [])[:3] if isinstance(a, dict) and a.get("part")]
                if parts:
                    out.setdefault(w, []).extend(parts)
        seed = getattr(lx, "seed", None)
        if callable(seed):
            for item in seed(words) or []:
                src = " ".join(words[item["start"]: item["start"] + item.get("n", 1)])
                if item.get("targets"):
                    out.setdefault(src, []).extend(str(t) for t in item["targets"])
        exact = getattr(lx, "seed_exact", None)
        if callable(exact) and originals:
            for item in exact(originals) or []:
                out.setdefault(words[item["index"]], []).extend(str(x) for x in item["targets"])
    except Exception:  # noqa: BLE001 - a malformed lexicon never breaks retrieval
        return {}
    return {k: list(dict.fromkeys(v)) for k, v in out.items() if v}


def _exact_seed_expansions(repo: Path | None, words: list[str]) -> dict[str, list[str]]:
    """Grounded exact-spelling seed hits (``öldüğünde`` -> die, death) for the words as written."""
    if repo is None or not words:
        return {}
    try:
        from verinoda import lexicon

        lx = lexicon.load(repo)
        hits = lx.seed_exact(words) if lx is not None else []
    except Exception:  # noqa: BLE001 - no lexicon: nothing to add
        return {}
    return {textnorm.fold_tr(words[h["index"]]): list(h["targets"]) for h in hits}


def command_words(question: str) -> list[str]:
    """Command names a question uses as such: ``the scan command``, ``init and scan commands``,
    ``scan komutu``, ``init ve scan komutları``, ```verinoda scan` ``."""
    out: list[str] = []
    for m in _COMMAND_PHRASE.finditer(question):
        for w in re.split(r"\s*(?:,|\band\b|\bor\b|\bve\b|\bveya\b|\bile\b)\s*", m.group(1)):
            w = w.strip("`'\" ").lower()
            if re.fullmatch(r"[a-z][a-z0-9_-]{1,30}", w) and w not in _NOT_COMMANDS:
                out.append(w)
    for m in re.finditer(r"`([\w.-]+)\s+([a-z][\w-]{1,30})(?:\s[^`]*)?`", question):
        out.append(m.group(2).lower())  # `prog sub ...`: the sub-command
    return list(dict.fromkeys(out))


def command_handlers(conn: sqlite3.Connection, words: list[str]) -> dict[str, str]:
    """Symbol names of the handlers of the commands ``words`` that exist in the index: ``cmd_scan``,
    ``scan_command``, ``handle_scan``, ``ScanCommand``, ``cmdScan`` ... -> the command word."""
    cand: dict[str, str] = {}
    for w in words:
        s = w.replace("-", "_")
        camel = "".join(p[:1].upper() + p[1:] for p in s.split("_") if p)
        for name in (f"cmd_{s}", f"{s}_command", f"command_{s}", f"handle_{s}", f"do_{s}", f"{s}_cmd",
                     f"cmd{camel}", f"{camel}Command", f"handle{camel}", f"{camel}Cmd"):
            cand.setdefault(word_tokens(name)[0], w)
    rows = _fetch(conn, "SELECT DISTINCT n.term, u.name FROM names n JOIN units u ON u.uid = n.uid "
                        "WHERE n.term IN ({ph}) AND u.kind = 'symbol'", sorted(cand))
    return {name: cand[term] for term, name in rows if word_tokens(name)[0] == term}


def analyze_query(question: str, conn: sqlite3.Connection, *, expansions: dict[str, list[str]] | None = None,
                  repo: Path | None = None) -> QueryTerms:
    """Question -> weighted index terms, with every expansion reported."""
    named = named_identifiers(question)
    words: list[str] = []
    dropped: list[str] = []
    solo: list[bool] = []   # the word is a whole whitespace token ("graph.json" gives two that are not)
    for raw in question.split():
        base, suffix = textnorm.split_apostrophe(raw.strip(".,;:!?()[]{}\"`"))
        if suffix:
            dropped.append("'" + suffix.rstrip(".,;:!?()[]{}\"`"))
        parts = _WORD.findall(base)
        for w in parts:
            f = textnorm.fold_tr(w)
            if f in EN_STOPWORDS or f in textnorm.TR_STOPWORDS:
                dropped.append(w)
                continue
            if len(f) < 3 and f not in textnorm.SHORT_TECH and "_" not in w:
                dropped.append(w)
                continue
            words.append(w)
            solo.append(len(parts) == 1)
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

    # Inflected Turkish words: the vocabulary-confirmed stem itself is the user's word
    # ("modeli" -> model, "bıçağının" -> bicagi) even when the inflected form also occurs
    # somewhere (GeometriModeli); for words the index does not know, also the stem's corpus terms.
    tr_question = textnorm.has_turkish(question)
    for w in words:
        f = textnorm.fold_tr(w)
        if (tr_question or not w.isascii()) and len(f) >= 4 and f.isalpha():
            # the longest indexed term that leaves only Turkish inflection ("modeli" -> model); an
            # English word typed into a Turkish question (login, event) needs a longer stem
            shortest = TR_MIN_STEM_KNOWN if f in have and w.isascii() else TR_MIN_STEM
            cands = [f[:k] for k in range(len(f) - 1, shortest - 1, -1) if textnorm.is_suffix_chain(f[k:])
                     and not f[k:].startswith(TR_DERIVATIONAL)]  # oyuncu (player) is not oyun (game)
            known = _vocab_has(conn, cands)
            # the longest indexed stem ("eventleri" -> event, not even), unless it is what the English
            # stemmer made of another inflection: the word goes on with the "e" it dropped
            # ("melekten" -> melek, not melekt from "melekte")
            found = [c for c in cands if c in known]
            exact = found[0] if found else None
            if exact and len(found) > 1 and found[1] == exact[:-1] and f[len(exact):len(exact) + 1] == "e" \
                    and known[found[1]] > known[exact]:
                exact = found[1]
            if exact:  # an inflected form the index also knows as a word keeps the lower weight
                add(w, exact, f"turkish stem '{exact}'", EXPANSION_WEIGHT if f in have else TR_STEM_WEIGHT)
            st = textnorm.tr_stem(f, lambda p: bool(_vocab_prefixed(conn, p, 1)))
            if st != f and len(st) >= 3 and not f[len(st):].startswith(TR_DERIVATIONAL):
                if f not in have:
                    for term, _df in _vocab_prefixed(conn, st, 3):
                        add(w, term, f"turkish stem '{st}'", EXPANSION_WEIGHT)
    # Two adjacent words that the code writes as one name ("sipariş oluştur" -> siparis_olustur,
    # "retry policy" -> retry_policy, "order service" -> OrderService): that name, as if spelled
    stems_of: dict[str, set[str]] = defaultdict(set)
    for e in exps:
        if str(e["via"]).startswith("turkish stem"):
            stems_of[e["from"]].add(e["to"])
    for k, (w1, w2) in enumerate(zip(words, words[1:])):
        if k + 1 >= len(solo) or not (solo[k] and solo[k + 1]):
            continue  # a file name or dotted name split into words is not two words of a phrase
        f1 = {textnorm.fold_tr(w1).lower()} | stems_of.get(w1, set())
        f2 = {textnorm.fold_tr(w2).lower()} | stems_of.get(w2, set())
        joined = sorted({a + sep + b for a in f1 for b in f2 for sep in ("_", "") if len(a) >= 3 and len(b) >= 3})
        for term in sorted(_vocab_has(conn, joined)):
            add(f"{w1} {w2}", term, "joined words", 1.0)
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
            # Turkish derivational endings only in Turkish questions (oyuncu is not oyun); in English
            # "applic" -> app and "deduplicat" -> dedup are abbreviations
            endings = _DERIVATIONAL + TR_DERIVATIONAL if tr_question else _DERIVATIONAL
            for k in range(max(3, math.ceil(0.4 * len(t))), len(t)):
                if t[:k] not in NOT_ABBREVIATIONS and not t[k:].startswith(endings):
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
    labels, covered = _label_expansions(repo, [textnorm.fold_tr(w) for w in words]) if tr_question else ({}, set())
    for src, targets in labels.items():
        for tgt in targets:
            add(src, tgt, "locale label", 1.0, indexed=False)
    if not provided and tr_question:
        provided = _lexicon_expansions(repo, [textnorm.fold_tr(w) for w in words], words, skip=covered)
        via_default = "lexicon"
    else:
        via_default = "question plan"
    for src, targets in provided.items():
        for tgt in targets or ():
            add(str(src), str(tgt), via_default, PROVIDED_EXPANSION_WEIGHT, indexed=False)
    if expansions and tr_question:
        # the question plan reads folded words, so it cannot tell "öl" (die) from "ol" (be):
        # the exact-spelling seed entries are added to its glosses here
        for src, targets in _exact_seed_expansions(repo, words).items():
            for tgt in targets:
                add(src, tgt, "lexicon", PROVIDED_EXPANSION_WEIGHT, indexed=False)
    # "the scan command" / "scan komutu": its handler (cmd_scan) counts as named by the question
    for handler, word in command_handlers(conn, command_words(question)).items():
        if handler not in named:
            named.append(handler)
            exps.append({"from": f"{word} (command)", "to": handler, "via": "command handler", "weight": 1.0})
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


_SPELLED_RID = re.compile(r"(?<![\w:/.#@-])#?([a-z0-9_.-]+:[a-z0-9_./-]*[a-z0-9_])(?![\w:/@])")
MAX_RID_FILES = 300               # files re-read to confirm a resource id the question spells


def _spelled_resource_ids(g, conn: sqlite3.Connection, h: "Handle", question: str,
                          acc: dict[int, dict[str, float]], p_uid: dict[int, int]) -> dict[int, tuple[int | None, str]]:
    """uid -> (pid, id) for units that write a resource id the question spells (``emberforge:forging``),
    and units defined as that id. Only ids of a namespace the index knows as a resource namespace
    count, so ``key:value`` prose or a URL scheme is not one. Passage text is re-read to confirm
    the id itself, not just its tokens (``forging`` also occurs as ``forge``)."""
    out: dict[int, tuple[int | None, str]] = {}
    root = Path(getattr(g, "root", None) or ".")
    texts: dict[str, str | None] = {}   # file -> lowered text, shared by every id of the question
    for m in _SPELLED_RID.finditer(question):
        rid = m.group(1).lower()
        # the id itself, not a longer one (minecraft:item/generated) nor another namespace (magic:ores)
        rid_rx = re.compile(r"(?<![a-z0-9_.:#-])#?" + re.escape(rid) + r"(?![a-z0-9_./-])")
        ns = rid.split(":", 1)[0]
        known = conn.execute("SELECT 1 FROM refs WHERE rid LIKE ? LIMIT 1", (ns + ":%",)).fetchone() or \
            conn.execute("SELECT 1 FROM units WHERE kind = 'data' AND (name LIKE ? OR name LIKE ?) LIMIT 1",
                         (ns + ":%", "#" + ns + ":%")).fetchone()
        if not known:
            continue
        for uid, row in h.units.items():  # the unit defined as that id (a data file named by it)
            if row[2] == "data" and (row[3] or "").lower().lstrip("#") == rid:
                out.setdefault(uid, (None, m.group(0)))
        toks = [x for x in dict.fromkeys(tokens(rid)) if x]
        cand = [pid for pid, a_p in acc.items() if toks and all(x in a_p for x in toks)]
        if not cand:
            continue
        rows = _fetch(conn, "SELECT p.pid, p.a, p.b, u.file, u.kind FROM passages p JOIN units u ON u.uid = p.uid "
                            "WHERE p.pid IN ({ph})", sorted(cand))
        by_file: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
        kind_of: dict[str, str] = {}
        for pid, a, b, f, kind in rows:
            by_file[f].append((pid, a, b))
            kind_of[f] = kind
        # data files write ids; read them first, and each file once
        for f in sorted(by_file, key=lambda x: (kind_of[x] != "data", x))[:MAX_RID_FILES]:
            if f not in texts:
                try:
                    texts[f] = (root / f).read_text(encoding="utf-8", errors="replace").lower()
                except OSError:
                    texts[f] = None
            text = texts[f]
            if not text or not rid_rx.search(text):
                continue
            lines = text.splitlines()
            for pid, a, b in sorted(by_file[f], key=lambda x: x[1]):
                if rid_rx.search("\n".join(lines[max(0, a - 1):b])):
                    uid = p_uid.get(pid)
                    if uid is not None and uid not in out:
                        out[uid] = (pid, m.group(0))
    return out


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
        reasons: dict[int, list[str]] = defaultdict(list)
        ref_roots = _reference_roots(getattr(g, "root", None), q, question)

        def ref_factor(uid: int) -> float:
            return REFERENCE_FACTOR if ref_roots and h.units[uid][0].startswith(ref_roots) else 1.0

        own_words = {t for t, w in q.weights.items() if w >= 1.0}

        def data_factor(uid: int) -> float:
            f, kind = h.units[uid][0], h.units[uid][2]
            if kind != "data" or f.lower().endswith(CONFIG_SUFFIXES):
                return 1.0
            from verinoda import resources

            if resources.resource_keys(f) or own_words & set(tokens(f.rsplit("/", 1)[-1].split(".", 1)[0])):
                return 1.0
            return DATA_FACTOR

        # (a 0.8 factor for test files was tried on 2026-09-24: +3 facts on heldout_repoatlas,
        # -2 on glow_mod and -1 on orders_app_tr, whose behaviour questions cite tests; not kept)
        for uid in best:
            best[uid] *= ref_factor(uid) * data_factor(uid)
        top = max(best.values(), default=0.0) or 1.0
        lex = {uid: s / top for uid, s in best.items() if s > 0}
        if q.named:
            want = {nm.strip("_").lower(): nm for nm in q.named}
            rows = _fetch(conn, "SELECT uid, name, qual FROM units WHERE uid IN "
                                "(SELECT uid FROM names WHERE term IN ({ph}))",
                          sorted({t for nm in want for t in word_tokens(nm)}))
            # "Wisp.spawn": of the symbols named spawn, the one Wisp owns (its qualified name or
            # its file); when none is owned by it, every spawn counts as before
            owners = qualified_owners(question)

            def owned(uid: int, name: str, qual: str | None) -> bool:
                xs = owners.get(name.strip("_").lower())
                if not xs:
                    return True
                scope = (qual or "").split(".")[:-1]
                path = PurePosixPath(h.units[uid][0])
                # "Store.save": the class Store (or Store.java); "store.save": the module or package
                # store (store.py, store/__init__.py, a Go package folder), not a class Store
                return any(x in scope or (x == path.stem if x[:1].isupper() else
                                          x.lower() in (path.stem.lower(), path.parent.name.lower())) for x in xs)

            has_owner = {name.strip("_").lower() for uid, name, qual in rows if uid in h.units and owned(uid, name, qual)
                         and name.strip("_").lower() in owners}
            for uid, name, qual in rows:
                nm = want.get(name.strip("_").lower())
                if nm and name.strip("_").lower() in has_owner and not owned(uid, name, qual):
                    continue
                if nm and uid in h.units and h.units[uid][2] == "symbol" and \
                        (include_tests or not is_test_file(h.units[uid][0])):
                    lex[uid] = max(lex.get(uid, 0.0), ref_factor(uid))
                    reasons[uid].append(f"question names '{nm}'")
                    if uid not in per_unit and h.units[uid][7] is not None:
                        per_unit[uid] = [(0.0, h.units[uid][7])]
        for nid, why in (seeds or {}).items():
            uid = h.nid_uid.get(nid)
            if uid is None or (not include_tests and is_test_file(h.units[uid][0])):
                continue
            lex[uid] = max(lex.get(uid, 0.0), ref_factor(uid))
            reasons[uid].append(f"plan: {why}" if why else "plan seed")
            if uid not in per_unit and h.units[uid][7] is not None:
                per_unit[uid] = [(0.0, h.units[uid][7])]
        for uid, (pid, rid) in _spelled_resource_ids(g, conn, h, question, acc, p_uid).items():
            if not include_tests and is_test_file(h.units[uid][0]):
                continue
            lex[uid] = max(lex.get(uid, 0.0), ref_factor(uid))
            reasons[uid].append(f"question names {rid}")
            rest = [x for x in per_unit.get(uid, []) if x[1] != pid]
            per_unit[uid] = [(max((x[0] for x in rest), default=0.0), pid)] + rest if pid is not None else \
                (per_unit.get(uid) or ([(0.0, h.units[uid][7])] if h.units[uid][7] is not None else []))
        # byte-identical data files (a data pack shipped twice) rank once, as their canonical copy
        canon = _canonical(h, conn, ref_roots)
        _fold_copies(h, lex, canon, include_tests)
        score = dict(lex)
        for uid, (bonus, why) in _link_bonus(conn, h, lex, include_tests, canon).items():
            score[uid] = score.get(uid, 0.0) + bonus * ref_factor(uid)
            reasons[uid].append(why)
            if uid not in per_unit and h.units[uid][7] is not None:
                per_unit[uid] = [(0.0, h.units[uid][7])]
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
                    bonus = PPR_LAMBDA * m / (m + PPR_KAPPA) * ref_factor(uid)
                    ppr_mass[uid] = bonus
                    score[uid] = score.get(uid, 0.0) + bonus
        if ppr_mass:
            for uid, (bonus, why) in _linked_prior(conn, h, lex, ppr_mass, include_tests).items():
                score[uid] = score.get(uid, 0.0) + bonus * ref_factor(uid)
                ppr_mass[uid] = bonus
                reasons[uid].append(why)
        _fold_copies(h, score, canon, include_tests)
        for uid in score:
            if ref_factor(uid) < 1.0:
                reasons[uid].append(REFERENCE_REASON)
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


def _copies(h: Handle, conn: sqlite3.Connection) -> dict[str, list[str]]:
    """file -> the other indexed files with the same sha256 (sorted; the first path is the one ranked)."""
    if h.copies is None:
        by_sha: dict[str, list[str]] = defaultdict(list)
        for f, sha in conn.execute("SELECT file, sha256 FROM files WHERE uid_lo IS NOT NULL"):
            by_sha[sha].append(f)
        h.copies = {f: sorted(x for x in fs if x != f) for fs in by_sha.values() if len(fs) > 1 for f in fs}
    return h.copies


def _canonical(h: Handle, conn: sqlite3.Connection, ref_roots: tuple[str, ...] = ()) -> dict[str, str]:
    """file -> the copy that stands for its group of byte-identical files.

    Preference: a file with code units (the graph describes it) over a data copy, product
    code over tests, the project's own files over reference trees, a copy in a source set
    (``src/``) over a loose one, then path order.
    """
    out: dict[str, str] = {}
    by_file = _by_file(h)

    def key(f: str):
        code = any(h.units[u][2] in ("symbol", "module") for *_x, u in by_file.get(f, []))
        return (not code, is_test_file(f), bool(ref_roots) and f.startswith(ref_roots),
                "/src/" not in "/" + f, f)

    for f, others in _copies(h, conn).items():
        if f not in out:
            k = min([f, *others], key=key)
            for x in (f, *others):
                out[x] = k
    return out


def _fold_copies(h: Handle, scores: dict[int, float], canon: dict[str, str], include_tests: bool) -> None:
    """Drop data units of non-canonical copies from ``scores``, carrying their score to the
    matching unit of the canonical copy (same content, same spans). Code units are never dropped:
    two identical code files are two places in the graph."""
    by_file = _by_file(h)
    for uid in [u for u in scores if canon.get(h.units[u][0], h.units[u][0]) != h.units[u][0]]:
        f, _nid, kind, _name, a, b = h.units[uid][:6]
        k = canon[f]
        if kind not in ("data", "prose") or (not include_tests and is_test_file(k)):
            continue
        s = scores.pop(uid)
        ku = next((u for _s, ka, kb, u in by_file.get(k, []) if (ka, kb) == (a, b)), None)
        if ku is not None and s > scores.get(ku, 0.0):
            scores[ku] = s


def copies_of(h: Handle, file: str) -> list[str]:
    """Other indexed files with exactly the same content as ``file``."""
    conn = h.connect()
    try:
        return list(_copies(h, conn).get(file, []))
    finally:
        h.release(conn)


def _by_file(h: Handle) -> dict[str, list[tuple[int, int, int, int]]]:
    if h.by_file is None:
        m: dict[str, list[tuple[int, int, int, int]]] = defaultdict(list)
        for uid, row in h.units.items():
            m[row[0]].append((row[5] - row[4], row[4], row[5], uid))
        h.by_file = {f: sorted(v) for f, v in m.items()}
    return h.by_file


def unit_at(h: Handle, file: str, line: int) -> int | None:
    """The innermost unit of ``file`` whose span contains ``line``."""
    return next((uid for _s, a, b, uid in _by_file(h).get(file, []) if a <= line <= b), None)


def first_unit(h: Handle, file: str) -> int | None:
    units = _by_file(h).get(file, [])
    return min(units, key=lambda x: (x[1], x[0]))[3] if units else None


def links_of(conn: sqlite3.Connection, file: str, a: int, b: int, *, also: list[str] | tuple = ()) -> dict:
    """Resource links of the lines ``a..b`` of ``file``: ids they name (``out``: line, target, rid,
    form, sure) and lines naming the file or one of its identical copies ``also`` (``in``: file,
    line, rid, form, sure). Callers filter first and cap after."""
    out = conn.execute("SELECT line, target, rid, form, sure FROM refs WHERE file = ? AND line BETWEEN ? AND ? "
                       "ORDER BY line, target", (file, a, b)).fetchall()
    targets = [file, *also]
    inn = conn.execute(f"SELECT file, line, rid, form, sure FROM refs WHERE target IN ({','.join('?' * len(targets))}) "
                       "ORDER BY file, line", targets).fetchall()
    return {"out": out, "in": inn}


def describe_links(h: Handle, file: str, a: int, b: int, *, limit: int = 3) -> dict:
    """What lines ``a..b`` of ``file`` name, and which lines name ``file`` (for rendering).

    ``names``: ``[{"rid", "line", "form", "targets": [files]}]`` (one entry per id);
    ``named_by``: ``[{"file", "line", "rid", "form", "unit"}]``; ``copies``: identical files;
    ``more_names`` / ``more_named_by``: how many were left out.
    """
    conn = h.connect()
    try:
        copies = _copies(h, conn).get(file, [])
        lk = links_of(conn, file, a, b, also=copies)
        canon = _canonical(h, conn)
    finally:
        h.release(conn)
    names: dict[str, dict] = {}
    for line, target, rid, form, sure in lk["out"]:
        target = canon.get(target, target)  # a copy of a target stands for the canonical file
        e = names.setdefault(rid, {"rid": rid, "line": line, "form": form, "sure": bool(sure), "targets": []})
        if target not in e["targets"]:
            e["targets"].append(target)
    by: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for sf, line, rid, form, sure in lk["in"]:
        if (sf, line) in seen or canon.get(sf, sf) != sf:
            continue  # a non-canonical copy repeats the canonical copy's line
        seen.add((sf, line))
        u = unit_at(h, sf, line)
        by.append({"file": sf, "line": line, "rid": rid, "form": form, "sure": bool(sure),
                   "unit": h.units[u][3] if u is not None else "",
                   "code": u is not None and h.units[u][2] in ("symbol", "module")})
    # code before data files, links that state what they name before inferred ones, then path order
    by.sort(key=lambda x: (not x["code"], not x["sure"], is_test_file(x["file"]), x["file"], x["line"]))
    return {"names": list(names.values())[:limit], "more_names": max(0, len(names) - limit),
            "named_by": by[:limit], "more_named_by": max(0, len(by) - limit),
            "copies": list(copies)}


def unindexed_matching(h: Handle, q: "QueryTerms", *, limit: int = 3) -> tuple[list[tuple[str, str]], int]:
    """Repository files that were not indexed and whose path carries one of the question's own words."""
    own = {t for t, w in q.weights.items() if w >= 1.0 and len(t) >= 3}
    if not own:
        return [], 0
    if h.unindexed is None:  # tokenized once per handle: asset-heavy repos list 100k+ files here
        conn = h.connect()
        try:
            rows = conn.execute("SELECT file, reason FROM unindexed ORDER BY file").fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            h.release(conn)
        h.unindexed = [(f, r, frozenset(tokens(f.rsplit("/", 1)[-1].split(".", 1)[0]))) for f, r in rows]
    # the file's own name must carry a question word (a folder word matches whole trees)
    hits = [(f, r) for f, r, toks in h.unindexed if own & toks]
    return hits[:limit], len(hits)


def _link_bonus(conn: sqlite3.Connection, h: Handle, lex: dict[int, float],
                include_tests: bool, canon: dict[str, str] | None = None) -> dict[int, tuple[float, str]]:
    """Units linked by a resource id to one of the top lexical units, with the score they gain."""
    out: dict[int, tuple[float, str]] = {}

    def give(uid: int | None, bonus: float, why: str) -> None:
        if uid is None or uid not in h.units or (not include_tests and is_test_file(h.units[uid][0])):
            return
        if uid not in out or out[uid][0] < bonus:
            out[uid] = (bonus, why)

    canon = canon if canon is not None else _canonical(h, conn)
    copies = _copies(h, conn)
    for src in sorted(lex, key=lambda u: (-lex[u], u))[:LINK_SEEDS]:
        f, _nid, _kind, name, a, b = h.units[src][:6]
        bonus = LINK_LAMBDA * lex[src]
        lk = links_of(conn, f, a, b, also=copies.get(f, []))
        # ids this unit names -> their files (a copy stands for its canonical file); stated links first
        done: set[str] = set()
        for line, target, rid, _form, sure in sorted(lk["out"], key=lambda r: (not r[4], r[0], r[1])):
            target = canon.get(target, target)
            if target in done or (not include_tests and is_test_file(target)):
                continue
            done.add(target)
            if len(done) > LINK_FANOUT:
                break
            tu = [u for _s, _a, _b, u in _by_file(h).get(target, []) if u in lex]
            give(max(tu, key=lambda u: lex[u]) if tu else first_unit(h, target),
                 bonus * (1.0 if sure else UNSURE_LINK_FACTOR),
                 f"named by {name} ({f}:{line}) as {rid}" + ("" if sure else " (inferred link)"))
        # lines naming this file: filtered first, product code and stated links first, one per unit
        rows = [r for r in lk["in"] if canon.get(r[0], r[0]) == r[0] and (include_tests or not is_test_file(r[0]))]
        rows.sort(key=lambda r: (not r[4], is_test_file(r[0]), r[0], r[1]))
        got: set[int] = set()
        for sf, line, rid, _form, sure in rows:
            u = unit_at(h, sf, line)
            if u is None or u in got:
                continue
            got.add(u)
            if len(got) > LINK_FANOUT:
                break
            give(u, bonus * (1.0 if sure else UNSURE_LINK_FACTOR),
                 f"names {rid} ({sf}:{line}), defined in {f}" + ("" if sure else " (inferred link)"))
    return out


def _linked_prior(conn: sqlite3.Connection, h: Handle, lex: dict[int, float], ppr_mass: dict[int, float],
                  include_tests: bool, top: int = 60) -> dict[int, tuple[float, str]]:
    """Data units have no graph node, so the PageRank prior never reaches them. A data unit
    among the top lexical candidates takes :data:`LINK_PPR_FACTOR` of the largest prior of a
    code unit that names it or that it names (``wisp_death.mcfunction`` <- the function that
    runs it)."""
    out: dict[int, tuple[float, str]] = {}
    data = [u for u in sorted(lex, key=lambda u: (-lex[u], u))[:top] if h.units[u][2] == "data"]
    found: dict[int, list[tuple[float, int]]] = {}
    fan: dict[int, int] = defaultdict(int)   # code unit -> how many of these data units it links to
    for uid in data:
        f, _nid, _kind, _name, a, b = h.units[uid][:6]
        lk = links_of(conn, f, a, b)
        cands: dict[int, float] = {}
        for sf, line, _rid, _form, sure in lk["in"]:
            u = unit_at(h, sf, line)
            if u is not None and u in ppr_mass and (include_tests or not is_test_file(sf)):
                cands[u] = max(cands.get(u, 0.0), ppr_mass[u] * (1.0 if sure else UNSURE_LINK_FACTOR))
        for _line, target, _rid, _form, sure in lk["out"]:
            for _s, _a, _b, u in _by_file(h).get(target, []):
                if u in ppr_mass:
                    cands[u] = max(cands.get(u, 0.0), ppr_mass[u] * (1.0 if sure else UNSURE_LINK_FACTOR))
        found[uid] = [(m, u) for u, m in cands.items()]
        for u in cands:
            fan[u] += 1
    for uid, cands in found.items():
        if cands:
            # a registry class that names every item passes its prior to all of them: shared, not copied
            m, u = max((m / fan[u] ** LINK_FAN_POWER, u) for m, u in cands)
            out[uid] = (LINK_PPR_FACTOR * m, f"graph prior through the link with {h.units[u][3]} ({h.units[u][0]})")
    return out


def _reference_roots(root, q: "QueryTerms", q_text: str = "") -> tuple[str, ...]:
    """Path prefixes of config ``index.reference`` and of the folders detected as copies of the
    project (``verinoda.copies``) that the question does not name (they count less)."""
    if root is None:
        return ()
    try:
        from verinoda.paths import load_config

        entries = list((load_config(Path(root)).get("index") or {}).get("reference") or [])
    except Exception:  # noqa: BLE001 - an unreadable config only means no reference trees
        return ()
    try:
        from verinoda import copies

        detected = [c["path"] for c in copies.load(Path(root))]
    except Exception:  # noqa: BLE001 - no detection result: the configured trees only
        detected = []
    entries += detected
    words = {textnorm.fold_tr(w) for w in q.words}
    text = textnorm.fold_tr(q_text)
    roots = []
    for e in entries:
        path = e.get("path") if isinstance(e, dict) else e
        if not isinstance(path, str) or not path.strip("/"):
            continue
        aliases = [textnorm.fold_tr(s) for s in (e.get("aliases") or [])] if isinstance(e, dict) else []
        segments = [textnorm.fold_tr(seg) for seg in path.strip("/").split("/") if len(seg) >= 3]
        # an alias counts as a word or a word's stem ("orijinalde" names "orijinal"); a folder name
        # only as the whole word (so "super" does not name "mymod-original")
        named = any(a in words or (len(a) >= 5 and any(w.startswith(a) for w in words)) for a in aliases) \
            or any(s in words or re.search(r"(?<![\w-])" + re.escape(s) + r"(?![\w-])", text) for s in segments)
        if not named:
            roots.append(path.strip("/") + "/")
    return tuple(roots)


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
        if str(row[0]).startswith(MISALIGNED_PREFIX):  # the graph describes another version of it
            out.append(f)
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
