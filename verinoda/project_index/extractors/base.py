# DO NOT import from verinoda.project_index.extract here — direction is extract.py → extractors/ only.
from __future__ import annotations

from pathlib import Path

from verinoda.project_index.ids import make_id

# Language built-in globals that AST may classify as call targets when used as
# constructors or coercion functions (e.g. String(x), Number(x), Boolean(x)).
# Without this filter they become god-nodes accumulating spurious edges from
# every call site. Filter applied at same-file and cross-file resolution.
# See issue #726.
_LANGUAGE_BUILTIN_GLOBALS: frozenset[str] = frozenset({
    # JavaScript / TypeScript ECMAScript built-ins
    "String", "Number", "Boolean", "Object", "Array", "Symbol", "BigInt",
    "Date", "RegExp", "Error", "TypeError", "RangeError", "SyntaxError",
    "ReferenceError", "EvalError", "URIError",
    "Promise", "Map", "Set", "WeakMap", "WeakSet", "JSON", "Math",
    "Reflect", "Proxy", "Intl",
    "parseInt", "parseFloat", "isNaN", "isFinite",
    "encodeURIComponent", "decodeURIComponent", "encodeURI", "decodeURI",
    # Browser / Node common globals
    "URL", "URLSearchParams", "FormData", "Blob", "File",
    "Headers", "Request", "Response", "AbortController", "AbortSignal",
    "TextEncoder", "TextDecoder", "console",
    # Python built-in callables
    "str", "int", "float", "bool", "list", "dict", "set", "tuple", "bytes",
    "len", "range", "enumerate", "zip", "map", "filter", "sum", "min", "max",
    "print", "open", "isinstance", "type", "super", "sorted", "reversed",
    "any", "all", "abs", "round", "next", "iter", "hash", "id", "repr",
    "callable", "getattr", "setattr", "hasattr", "delattr", "vars", "dir",
    # Swift standard library / Foundation / SwiftUI (#2147). Value-type
    # initializers (Data(x), Int(x), UUID()) and protocol conformance targets
    # appear from virtually every file of a Swift codebase, exactly like the
    # ECMAScript constructors above. String/Date/URL/Error are already listed.
    "Int", "Int8", "Int16", "Int32", "Int64",
    "UInt", "UInt8", "UInt16", "UInt32", "UInt64",
    "Double", "Float", "Bool", "Character",
    "Sendable", "Codable", "Decodable", "Encodable", "Equatable", "Hashable",
    "Identifiable", "Comparable", "CaseIterable", "RawRepresentable",
    "CustomStringConvertible", "CustomDebugStringConvertible", "AnyObject",
    "LocalizedError",
    "Data", "UUID", "Decimal", "Calendar", "Locale", "TimeZone", "Bundle",
    "IndexPath", "IndexSet", "NotificationCenter", "UserDefaults",
    "FileManager", "URLSession", "URLRequest", "URLComponents",
    "JSONDecoder", "JSONEncoder", "DateFormatter", "NumberFormatter",
    "ISO8601DateFormatter",
    "NSObject", "NSString", "NSError", "NSLock", "NSAttributedString",
    "DispatchQueue", "DispatchGroup", "OperationQueue", "RunLoop",
    "View", "Color", "Font",
})


def _make_id(*parts: str) -> str:
    return make_id(*parts)


def _file_stem(path: Path) -> str:
    """Stem used as the node-ID prefix for a file and its symbols.

    The full path (extension dropped) is preserved as path segments; ``make_id``
    later collapses the separators to underscores. Using every segment — not just
    the immediate parent dir (#1504) — means same-named files in different
    directories get distinct IDs instead of colliding into one
    last-writer-wins node:

        docs/v1/api/README.md -> docs/v1/api/README -> docs_v1_api_readme
        docs/v2/api/README.md -> docs/v2/api/README -> docs_v2_api_readme

    Top-level files keep a bare stem (``setup.py`` -> ``setup``). When passed an
    absolute path the whole path is encoded; the extract() id-remap post-pass
    re-derives the canonical repo-relative form from ``source_file`` so the on-disk
    location can't leak into the persisted IDs (#502).

    Returns "" for a path with no name (``Path('.')`` — a source_file that equals
    the scan root, so it has no per-file stem). Guarding here keeps
    ``path.with_suffix("")`` from raising ``ValueError: '.' has an empty name`` and
    protects every caller, not just ``_semantic_id_remap`` (#1618)."""
    # Local change (Verinoda): the stem is kept per (path type, path string), which is all it
    # depends on (the resolution passes ask for the same few thousand files once per node).
    key = (type(path), str(path))
    stem = _FILE_STEM_MEMO.get(key)
    if stem is None:
        if not path.name:
            stem = ""
        else:
            stem = path.with_suffix("").as_posix()
        if len(_FILE_STEM_MEMO) >= 65536:
            _FILE_STEM_MEMO.clear()
        _FILE_STEM_MEMO[key] = stem
    return stem


# Local change (Verinoda): the memo of _file_stem, a lexical function of its key.
_FILE_STEM_MEMO: dict[tuple[type, str], str] = {}


def _read_text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
