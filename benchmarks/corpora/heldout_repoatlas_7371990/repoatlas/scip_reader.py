"""Dependency-free reader for SCIP code-intelligence indexes (docs/DESIGN.md D29).

A SCIP index (``index.scip``) is a protobuf ``scip.Index`` message written by a
language's own indexer (scip-typescript, scip-java, scip-go, rust-analyzer,
scip-clang, scip-python, ...). RepoAtlas never runs those tools; when the user
or their CI produced an index, this module reads it with a ~100-line protobuf
wire-format decoder - no ``protobuf`` package - and answers "which definition
does the reference on line L bind to?" through :class:`ScipResolver`, the same
interface as :mod:`repoatlas.precise`.

Decoded (field numbers from sourcegraph/scip ``scip.proto``):

* ``Index``: ``metadata`` (1: tool name/version, project root), ``documents`` (2),
  ``external_symbols`` (3);
* ``Document``: ``relative_path`` (1), ``occurrences`` (2), ``symbols`` (3),
  ``language`` (4), ``text`` (5);
* ``Occurrence``: ``range`` (1, packed int32, deprecated but still the common
  encoding) or the typed ``single_line_range`` (8) / ``multi_line_range`` (9),
  ``symbol`` (2), ``symbol_roles`` (3);
* ``SymbolInformation``: ``symbol`` (1), ``documentation`` (3),
  ``relationships`` (4), ``kind`` (5), ``display_name`` (6),
  ``enclosing_symbol`` (8).

SCIP positions are 0-based and half-open; :class:`Occurrence` stores **1-based
lines** (RepoAtlas and the graph's convention) and 0-based columns. The
vendored ``project_index/scip_ingest.py`` (upstream Graphify, left untouched)
reads a simplified JSON shape and treats the 0-based line as 1-based.

Freshness: an index carries no file hashes (unless it embeds ``text``). The
first time RepoAtlas reads an index it records the sha256 of every indexed file
that is strictly older than the index itself in ``.repoatlas/index/scip_fresh.json``;
later a document is used only while its file still has that hash. Files
modified after the index was written are stale from the start.

Symbol identity in SCIP is name-based (``scheme package descriptors``), so an
answer is at *reference level*: it cannot tell two same-named definitions of
one module apart (it confirmed 13 shadowed-duplicate errors on Graphify's own
code that jedi caught). :mod:`repoatlas.precise` therefore uses SCIP only for
non-Python files.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

ROLE_DEFINITION = 0x1
ROLE_IMPORT = 0x2
ROLE_WRITE = 0x4
ROLE_READ = 0x8
ROLE_GENERATED = 0x10
ROLE_TEST = 0x20
ROLE_FORWARD_DEFINITION = 0x40
KIND_NAMES = {7: "class", 9: "constructor", 11: "enum", 15: "field", 17: "function", 21: "interface",
              26: "method", 29: "module", 30: "namespace", 35: "package", 37: "parameter", 41: "property",
              49: "struct", 53: "trait", 55: "type_alias", 61: "variable", 66: "method", 80: "method",
              70: "method", 67: "method", 68: "method", 69: "method", 74: "method", 76: "method"}
CANDIDATES = ("index.scip", ".repoatlas/index/index.scip")
FRESH_FILE = "scip_fresh.json"


# -- wire format -----------------------------------------------------------------------------

def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = shift = 0
    while True:
        if i >= len(b):
            raise ValueError("truncated varint")
        x = b[i]
        i += 1
        r |= (x & 0x7F) << shift
        if x < 0x80:
            return r, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _fields(b: bytes):
    """Yield (field number, wire type, value) for one message; unknown fields are yielded too."""
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        num, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 2:
            ln, i = _varint(b, i)
            if i + ln > n:
                raise ValueError("truncated length-delimited field")
            v, i = b[i:i + ln], i + ln
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported wire type {wt}")
        if i > n:
            raise ValueError("truncated fixed-width field")
        yield num, wt, v


def _int32(v: int) -> int:
    return v - (1 << 64) if v >= 1 << 63 else v


def _packed(v) -> list[int]:
    if isinstance(v, int):  # one unpacked element
        return [_int32(v)]
    out, i = [], 0
    while i < len(v):
        x, i = _varint(v, i)
        out.append(_int32(x))
    return out


def _str(v) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else ""


# -- model -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Occurrence:
    start_line: int   # 1-based
    start_col: int    # 0-based
    end_line: int     # 1-based
    end_col: int      # 0-based, exclusive
    symbol: str
    roles: int

    @property
    def is_definition(self) -> bool:
        return bool(self.roles & ROLE_DEFINITION)


@dataclass(frozen=True)
class Relationship:
    symbol: str
    is_reference: bool = False
    is_implementation: bool = False
    is_type_definition: bool = False
    is_definition: bool = False


@dataclass
class SymbolInfo:
    symbol: str
    kind: int = 0
    display_name: str = ""
    enclosing_symbol: str = ""
    documentation: list[str] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)

    @property
    def kind_name(self) -> str:
        return KIND_NAMES.get(self.kind, "symbol")


@dataclass
class Document:
    relative_path: str
    language: str = ""
    occurrences: list[Occurrence] = field(default_factory=list)
    symbols: list[SymbolInfo] = field(default_factory=list)
    text: str | None = None


@dataclass
class ScipIndex:
    tool_name: str = ""
    tool_version: str = ""
    project_root: str = ""
    documents: list[Document] = field(default_factory=list)
    external_symbols: list[SymbolInfo] = field(default_factory=list)

    def document(self, path: str) -> Document | None:
        want = path.replace("\\", "/")
        return next((d for d in self.documents if d.relative_path == want), None)


def _range(v: bytes, multi: bool) -> list[int]:
    vals = {num: _int32(x) for num, wt, x in _fields(v) if wt == 0}
    if multi:
        return [vals.get(1, 0), vals.get(2, 0), vals.get(3, 0), vals.get(4, 0)]
    line = vals.get(1, 0)
    return [line, vals.get(2, 0), line, vals.get(3, 0)]


def _occurrence(b: bytes) -> Occurrence | None:
    legacy: list[int] = []
    typed = None
    sym, roles = "", 0
    for num, wt, v in _fields(b):
        if num == 1:
            legacy.extend(_packed(v))
        elif num == 8 and wt == 2:
            typed = _range(v, False)
        elif num == 9 and wt == 2:
            typed = _range(v, True)
        elif num == 2:
            sym = _str(v)
        elif num == 3 and wt == 0:
            roles = _int32(v)
    rng = typed  # the typed range takes precedence over the deprecated packed one
    if rng is None:
        if len(legacy) == 3:
            rng = [legacy[0], legacy[1], legacy[0], legacy[2]]
        elif len(legacy) == 4:
            rng = legacy
        else:
            return None
    return Occurrence(rng[0] + 1, rng[1], rng[2] + 1, rng[3], sym, roles)


def _relationship(b: bytes) -> Relationship:
    d = {num: v for num, _, v in _fields(b)}
    return Relationship(_str(d.get(1, b"")), bool(d.get(2)), bool(d.get(3)), bool(d.get(4)), bool(d.get(5)))


def _symbol_info(b: bytes) -> SymbolInfo:
    info = SymbolInfo("")
    for num, wt, v in _fields(b):
        if num == 1:
            info.symbol = _str(v)
        elif num == 3 and wt == 2:
            info.documentation.append(_str(v))
        elif num == 4 and wt == 2:
            info.relationships.append(_relationship(v))
        elif num == 5 and wt == 0:
            info.kind = int(v)
        elif num == 6:
            info.display_name = _str(v)
        elif num == 8:
            info.enclosing_symbol = _str(v)
    return info


def decode(data: bytes) -> ScipIndex:
    """Decode a serialized ``scip.Index``. Raises ValueError on malformed input."""
    idx = ScipIndex()
    for num, wt, v in _fields(data):
        if wt != 2:
            continue
        if num == 1:
            for mn, mwt, mv in _fields(v):
                if mn == 2 and mwt == 2:
                    for tn, _, tv in _fields(mv):
                        if tn == 1:
                            idx.tool_name = _str(tv)
                        elif tn == 2:
                            idx.tool_version = _str(tv)
                elif mn == 3:
                    idx.project_root = _str(mv)
        elif num == 2:
            doc = Document("")
            for dn, dwt, dv in _fields(v):
                if dn == 1:
                    doc.relative_path = _str(dv).replace("\\", "/")
                elif dn == 2 and dwt == 2:
                    occ = _occurrence(dv)
                    if occ is not None:
                        doc.occurrences.append(occ)
                elif dn == 3 and dwt == 2:
                    doc.symbols.append(_symbol_info(dv))
                elif dn == 4:
                    doc.language = _str(dv)
                elif dn == 5:
                    doc.text = _str(dv)
            idx.documents.append(doc)
        elif num == 3:
            idx.external_symbols.append(_symbol_info(v))
    return idx


def load(path: Path) -> ScipIndex:
    return decode(Path(path).read_bytes())


def find_index(repo: Path) -> Path | None:
    """``index.scip`` at the repository root or under ``.repoatlas/index/``."""
    for rel in CANDIDATES:
        p = Path(repo) / rel
        if p.is_file():
            return p
    return None


# -- symbols ---------------------------------------------------------------------------------

def is_local(symbol: str) -> bool:
    return symbol.startswith("local ")


_SUFFIXES = {"/": "namespace", "#": "type", ".": "term", ":": "meta", "!": "macro"}


def _read_name(s: str, i: int) -> tuple[str, int]:
    """A simple or backtick-escaped identifier starting at ``i`` (``````  escapes a backtick)."""
    if i < len(s) and s[i] == "`":
        j, out = i + 1, []
        while j < len(s):
            if s[j] == "`":
                if s[j + 1:j + 2] == "`":
                    out.append("`")
                    j += 2
                    continue
                return "".join(out), j + 1
            out.append(s[j])
            j += 1
        return "".join(out), j
    j = i
    while j < len(s) and (s[j].isalnum() or s[j] in "_+-$"):
        j += 1
    return s[i:j], j


def descriptors(symbol: str) -> list[tuple[str, str]]:
    """``[(name, suffix kind), ...]`` of a SCIP symbol string (grammar in scip.proto ``Symbol``)."""
    if is_local(symbol):
        return [(symbol[6:], "local")]
    s, i, n, fields = symbol, 0, len(symbol), 0
    while i < n and fields < 4:  # scheme, manager, package name, version; "  " is an escaped space
        if s[i] == " ":
            if s[i + 1:i + 2] == " ":
                i += 2
                continue
            fields += 1
        i += 1
    out: list[tuple[str, str]] = []
    while i < n:
        c = s[i]
        if c in "[(":
            name, i = _read_name(s, i + 1)
            out.append((name, "type_parameter" if c == "[" else "parameter"))
            i += 1  # the closing bracket
            continue
        name, j = _read_name(s, i)
        if j == i:
            break
        i = j
        sfx = s[i] if i < n else ""
        if sfx == "(":  # method: name(disambiguator).
            k = s.find(").", i)
            if k < 0:
                break
            out.append((name, "method"))
            i = k + 2
        elif sfx in _SUFFIXES:
            out.append((name, _SUFFIXES[sfx]))
            i += 1
        else:
            out.append((name, ""))
            break
    return out


def symbol_name(symbol: str) -> str:
    """The last descriptor's name: ``... src/`mod.ts`/Repo#save().`` -> ``save``."""
    ds = descriptors(symbol)
    return ds[-1][0] if ds else ""


# -- freshness -------------------------------------------------------------------------------

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def document_freshness(repo: Path, index_path: Path, index: ScipIndex, *,
                       state_dir: Path | None = None) -> dict[str, bool]:
    """{relative path: usable} for every document of ``index`` (see the module docstring)."""
    repo = Path(repo).resolve()
    index_path = Path(index_path)
    raw = index_path.read_bytes()
    isha = _sha256(raw)
    state_dir = state_dir or (repo / ".repoatlas" / "index")
    state_file = state_dir / FRESH_FILE
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    rec = state.get(isha)
    if rec is None:
        mtime = index_path.stat().st_mtime_ns
        docs: dict[str, str | None] = {}
        for d in index.documents:
            p = repo / d.relative_path
            try:
                st = p.stat()
                # strictly older: file times tick coarsely (~16 ms on Windows), a tie may be an edit
                docs[d.relative_path] = _sha256(p.read_bytes()) if st.st_mtime_ns < mtime else None
            except OSError:
                docs[d.relative_path] = None
        rec = {"index": str(index_path), "docs": docs}
        state[isha] = rec
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            state_file.write_bytes(json.dumps(state, sort_keys=True, indent=1).encode("utf-8"))
        except OSError:
            pass
    out: dict[str, bool] = {}
    for d in index.documents:
        p = repo / d.relative_path
        try:
            cur = p.read_bytes()
        except OSError:
            out[d.relative_path] = False
            continue
        if d.text is not None:
            out[d.relative_path] = cur.decode("utf-8", "replace").replace("\r\n", "\n") == \
                d.text.replace("\r\n", "\n")
        else:
            want = rec["docs"].get(d.relative_path)
            out[d.relative_path] = want is not None and want == _sha256(cur)
    return out


# -- resolver --------------------------------------------------------------------------------

class ScipResolver:
    """Reference-level resolution from a SCIP index (the :mod:`repoatlas.precise` interface)."""

    def __init__(self, repo: Path, index: ScipIndex, fresh: dict[str, bool] | None = None):
        self.repo = Path(repo).resolve()
        self.index = index
        self.tool = f"scip:{index.tool_name or 'unknown'} {index.tool_version}".strip()
        self.fresh = fresh if fresh is not None else {d.relative_path: True for d in index.documents}
        self._defs: dict[str, list[tuple[str, int]]] = {}
        self._info: dict[str, SymbolInfo] = {}
        for d in index.documents:
            for o in d.occurrences:
                if o.is_definition and o.symbol:
                    key = f"{d.relative_path}\x00{o.symbol}" if is_local(o.symbol) else o.symbol
                    self._defs.setdefault(key, []).append((d.relative_path, o.start_line))
            for s in d.symbols:
                self._info.setdefault(s.symbol, s)
        self._external = {s.symbol for s in index.external_symbols}

    @classmethod
    def load(cls, repo: Path, index_path: Path) -> "ScipResolver":
        index = load(index_path)
        return cls(repo, index, document_freshness(repo, index_path, index))

    def resolve(self, path: str, line: int, token: str) -> dict | None:
        """Same result shape as :func:`repoatlas.precise.resolve_call`; None if not indexed or stale."""
        rel = path.replace("\\", "/")
        doc = self.index.document(rel)
        if doc is None or not self.fresh.get(rel, False):
            return None
        base = {"tool": self.tool, "path": rel, "line": int(line), "token": token, "level": "reference",
                "name_based": True, "uncertainty": "symbol identity is name-based"}
        refs = [o for o in doc.occurrences if o.start_line == int(line) and not o.is_definition and o.symbol
                and symbol_name(o.symbol) == token]
        if not refs:
            return {**base, "kind": "unresolved", "targets": [], "reason": f"no reference to {token!r} on line {line}"}
        targets: dict[tuple[str, int], dict] = {}
        external = False
        for o in refs:
            key = f"{rel}\x00{o.symbol}" if is_local(o.symbol) else o.symbol
            defs = self._defs.get(key, [])
            if not defs:
                external = external or o.symbol in self._external or not is_local(o.symbol)
            info = self._info.get(o.symbol)
            for dpath, dline in defs:
                targets[(dpath, dline)] = {"path": dpath, "line": dline, "name": symbol_name(o.symbol),
                                           "qualname": o.symbol, "type": info.kind_name if info else "symbol",
                                           "in_repo": True}
        col = refs[0].start_col
        if not targets:
            kind = "external" if external else "unresolved"
            return {**base, "col": col, "kind": kind, "targets": [],
                    "reason": "the symbol is defined outside the index" if external else "no definition found"}
        if len(targets) > 1:
            return {**base, "col": col, "kind": "ambiguous", "targets": list(targets.values()),
                    "reason": f"{len(targets)} definition occurrences"}
        return {**base, "col": col, "kind": "definitive", "targets": list(targets.values()), "reason": None}
