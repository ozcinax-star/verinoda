"""SCIP reader (DESIGN D29): wire decoding, positions, freshness and the resolver interface.

The fixture index is written byte by byte by a tiny protobuf encoder in this
file (no protobuf dependency either side), covering the legacy packed
``range`` and the typed single/multi-line ranges, unknown fields, local and
external symbols, and a duplicated definition.
"""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".repoatlas/index")

import time  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from repoatlas import precise, scip_reader  # noqa: E402

PKG = "scip-typescript npm app 1.0.0"
HELPER = f"{PKG} src/`util.ts`/helper()."
DUP = f"{PKG} src/`util.ts`/dup()."
WIDGET = f"{PKG} src/`util.ts`/Widget#"
EXTERNAL = "scip-typescript npm lodash 4.17.21 `lodash.d.ts`/debounce()."
LOCAL = "local 7"


# -- a minimal protobuf encoder ------------------------------------------------------------------

def _varint(n: int) -> bytes:
    if n < 0:
        n += 1 << 64
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _key(num: int, wt: int) -> bytes:
    return _varint(num << 3 | wt)


def _ld(num: int, payload: bytes | str) -> bytes:
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    return _key(num, 2) + _varint(len(data)) + data


def _vi(num: int, n: int) -> bytes:
    return _key(num, 0) + _varint(n)


def occ(line0: int, c0: int, c1: int, symbol: str, roles: int = 0, *, typed: str | None = None,
        end_line0: int | None = None) -> bytes:
    if typed == "single":
        rng = _ld(8, _vi(1, line0) + _vi(2, c0) + _vi(3, c1))
    elif typed == "multi":
        rng = _ld(9, _vi(1, line0) + _vi(2, c0) + _vi(3, end_line0 if end_line0 is not None else line0) + _vi(4, c1))
    else:
        rng = _ld(1, b"".join(_varint(v) for v in (line0, c0, c1)))  # packed repeated int32
    return rng + _ld(2, symbol) + (_vi(3, roles) if roles else b"")


def sym_info(symbol: str, kind: int, display: str = "", rels: list[bytes] = ()) -> bytes:
    return _ld(1, symbol) + _ld(3, "doc") + b"".join(_ld(4, r) for r in rels) + _vi(5, kind) + \
        (_ld(6, display) if display else b"")


def document(path: str, lang: str, occs: list[bytes], syms: list[bytes] = (), text: str | None = None) -> bytes:
    body = _ld(4, lang) + _ld(1, path) + b"".join(_ld(2, o) for o in occs) + b"".join(_ld(3, s) for s in syms)
    if text is not None:
        body += _ld(5, text)
    return body


def index_bytes(docs: list[bytes], *, externals: list[bytes] = ()) -> bytes:
    meta = _vi(1, 1) + _ld(2, _ld(1, "scip-typescript") + _ld(2, "0.3.14") + _ld(3, "index")) + \
        _ld(3, "file:///work/app") + _vi(4, 1)
    unknown = _vi(99, 5) + _key(98, 1) + b"\x00" * 8 + _key(97, 5) + b"\x00" * 4  # skipped by the decoder
    return _ld(1, meta) + unknown + b"".join(_ld(2, d) for d in docs) + b"".join(_ld(3, e) for e in externals)


UTIL_TS = ("export function helper(x: number) {\n"   # 1
           "  return x;\n"                           # 2
           "}\n"                                     # 3
           "export function dup() { return 1; }\n"   # 4
           "export class Widget {}\n")               # 5
MAIN_TS = ("import { helper, dup, Widget } from './util';\n"   # 1
           "import { debounce } from 'lodash';\n"              # 2
           "\n"                                                 # 3
           "const y = helper(1);\n"                             # 4
           "const z = dup();\n"                                 # 5
           "const w = new Widget();\n"                          # 6
           "debounce(() => y, 5);\n"                            # 7
           "function inner() { return 2; }\n"                   # 8
           "inner();\n")                                        # 9
DUP_TS = "export function dup() { return 2; }\n"


def build_index() -> bytes:
    util = document("src/util.ts", "typescript", [
        occ(0, 16, 22, HELPER, scip_reader.ROLE_DEFINITION, typed="single"),
        occ(3, 16, 19, DUP, scip_reader.ROLE_DEFINITION),
        occ(4, 13, 19, WIDGET, scip_reader.ROLE_DEFINITION, typed="multi", end_line0=4),
    ], [sym_info(HELPER, 17, "helper", [_ld(1, WIDGET) + _vi(2, 1) + _vi(5, 1)]), sym_info(WIDGET, 7, "Widget")])
    main = document("src/main.ts", "typescript", [
        occ(0, 9, 15, HELPER, scip_reader.ROLE_IMPORT),
        occ(3, 10, 16, HELPER, scip_reader.ROLE_READ),
        occ(4, 10, 13, DUP),
        occ(5, 14, 20, WIDGET, typed="multi", end_line0=5),
        occ(6, 0, 8, EXTERNAL),
        occ(7, 9, 14, LOCAL, scip_reader.ROLE_DEFINITION),
        occ(8, 0, 5, LOCAL),
    ])
    dup = document("src/dup.ts", "typescript", [occ(0, 16, 19, DUP, scip_reader.ROLE_DEFINITION)])
    return index_bytes([util, main, dup], externals=[sym_info(EXTERNAL, 17, "debounce")])


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "app"
    (root / "src").mkdir(parents=True)
    for rel, text in (("src/util.ts", UTIL_TS), ("src/main.ts", MAIN_TS), ("src/dup.ts", DUP_TS)):
        (root / rel).write_bytes(text.encode("utf-8"))
    time.sleep(0.05)  # the index is written after the sources, like a real indexer run
    (root / "index.scip").write_bytes(build_index())
    precise.reset_caches()
    yield root
    precise.reset_caches()


# -- decoding --------------------------------------------------------------------------------------

def test_decode_documents_occurrences_symbols_and_metadata():
    idx = scip_reader.decode(build_index())
    assert (idx.tool_name, idx.tool_version, idx.project_root) == ("scip-typescript", "0.3.14", "file:///work/app")
    assert [d.relative_path for d in idx.documents] == ["src/util.ts", "src/main.ts", "src/dup.ts"]
    util, main = idx.documents[0], idx.documents[1]
    assert util.language == "typescript"
    # 0-based half-open SCIP ranges become 1-based lines, 0-based columns
    assert util.occurrences[0] == scip_reader.Occurrence(1, 16, 1, 22, HELPER, scip_reader.ROLE_DEFINITION)
    assert util.occurrences[1].start_line == 4 and util.occurrences[1].end_col == 19  # legacy packed range
    assert util.occurrences[2].start_line == util.occurrences[2].end_line == 5        # typed multi-line range
    assert util.occurrences[0].is_definition and not main.occurrences[1].is_definition
    helper = util.symbols[0]
    assert helper.kind_name == "function" and helper.display_name == "helper" and helper.documentation == ["doc"]
    assert helper.relationships == [scip_reader.Relationship(WIDGET, is_reference=True, is_definition=True)]
    assert idx.external_symbols[0].symbol == EXTERNAL and idx.document("src\\util.ts") is util


def test_four_element_legacy_range_and_negative_int32():
    body = _ld(1, b"".join(_varint(v) for v in (2, 1, 4, 3))) + _ld(2, "s") + _vi(3, -1)
    o = scip_reader._occurrence(body)
    assert (o.start_line, o.start_col, o.end_line, o.end_col) == (3, 1, 5, 3) and o.roles == -1
    assert scip_reader._occurrence(_ld(2, "no range")) is None


@pytest.mark.parametrize("bad", [b"\x12\x05ab", b"\x12", b"\x0b", b"\xff" * 11])
def test_malformed_input_raises_value_error(bad):
    with pytest.raises(ValueError):
        scip_reader.decode(bad)


@pytest.mark.parametrize("symbol,name", [
    (HELPER, "helper"), (WIDGET, "Widget"), (f"{PKG} src/`util.ts`/Widget#render().", "render"),
    (f"{PKG} src/`util.ts`/Widget#render(+1).", "render"), (f"{PKG} src/`util.ts`/CONST.", "CONST"),
    ("scip-python python graphify 0.9 `graphify.build`/build().", "build"), (f"{PKG} src/", "src"),
    (f"{PKG} src/`util.ts`/f().(x)", "x"), (f"{PKG} src/`util.ts`/T#[K]", "K"), (LOCAL, "7"),
    (f"{PKG} src/`we``ird`.", "we`ird"), (f"{PKG} src/macro!", "macro"),
])
def test_symbol_name(symbol, name):
    assert scip_reader.symbol_name(symbol) == name


# -- freshness ---------------------------------------------------------------------------------

def test_freshness_is_recorded_once_and_follows_file_hashes(project):
    idx = scip_reader.load(project / "index.scip")
    fresh = scip_reader.document_freshness(project, project / "index.scip", idx)
    assert fresh == {"src/util.ts": True, "src/main.ts": True, "src/dup.ts": True}
    state = project / ".repoatlas" / "index" / scip_reader.FRESH_FILE
    assert state.is_file()
    (project / "src" / "main.ts").write_bytes(MAIN_TS.replace("helper(1)", "helper(2)").encode())
    fresh = scip_reader.document_freshness(project, project / "index.scip", idx)
    assert fresh["src/main.ts"] is False and fresh["src/util.ts"] is True
    (project / "src" / "dup.ts").unlink()
    assert scip_reader.document_freshness(project, project / "index.scip", idx)["src/dup.ts"] is False


def test_files_newer_than_the_index_are_stale_from_the_start(tmp_path):
    root = tmp_path / "late"
    (root / "src").mkdir(parents=True)
    (root / "index.scip").write_bytes(build_index())
    time.sleep(0.05)
    for rel, text in (("src/util.ts", UTIL_TS), ("src/main.ts", MAIN_TS), ("src/dup.ts", DUP_TS)):
        (root / rel).write_bytes(text.encode())
    idx = scip_reader.load(root / "index.scip")
    assert set(scip_reader.document_freshness(root, root / "index.scip", idx).values()) == {False}


def test_embedded_document_text_is_compared_directly(tmp_path):
    root = tmp_path / "txt"
    root.mkdir()
    (root / "a.ts").write_bytes(b"let a = 1;\r\n")
    data = index_bytes([document("a.ts", "typescript", [], text="let a = 1;\n")])
    idx = scip_reader.decode(data)
    (root / "index.scip").write_bytes(data)
    assert scip_reader.document_freshness(root, root / "index.scip", idx) == {"a.ts": True}  # CRLF-insensitive
    (root / "a.ts").write_bytes(b"let a = 2;\n")
    assert scip_reader.document_freshness(root, root / "index.scip", idx) == {"a.ts": False}


# -- resolver ----------------------------------------------------------------------------------

def test_scip_resolver_kinds(project):
    r = scip_reader.ScipResolver.load(project, project / "index.scip")
    assert r.tool == "scip:scip-typescript 0.3.14"
    ok = r.resolve("src/main.ts", 4, "helper")
    assert ok["kind"] == "definitive" and ok["level"] == "reference" and ok["name_based"] is True
    assert ok["targets"] == [{"path": "src/util.ts", "line": 1, "name": "helper", "qualname": HELPER,
                              "type": "function", "in_repo": True}]
    assert ok["col"] == 10 and "name-based" in ok["uncertainty"]
    amb = r.resolve("src/main.ts", 5, "dup")
    assert amb["kind"] == "ambiguous" and sorted(t["path"] for t in amb["targets"]) == ["src/dup.ts", "src/util.ts"]
    assert r.resolve("src/main.ts", 6, "Widget")["targets"][0]["type"] == "class"
    assert r.resolve("src/main.ts", 7, "debounce")["kind"] == "external"
    local = r.resolve("src/main.ts", 9, "7")
    assert local["kind"] == "definitive" and local["targets"][0]["line"] == 8
    assert r.resolve("src/main.ts", 4, "nothing")["kind"] == "unresolved"
    assert r.resolve("src/other.ts", 1, "helper") is None  # not indexed
    # definitions are not references; a line with only a definition resolves nothing
    assert r.resolve("src/util.ts", 1, "helper")["kind"] == "unresolved"


def test_stale_documents_give_no_answer(project):
    (project / "src" / "main.ts").write_bytes((MAIN_TS + "// edited\n").encode())
    r = scip_reader.ScipResolver.load(project, project / "index.scip")
    assert r.resolve("src/main.ts", 4, "helper") is None
    assert r.resolve("src/util.ts", 1, "helper")["kind"] == "unresolved"  # other documents still usable


def test_precise_resolve_call_uses_scip_for_non_python_files(project):
    res = precise.resolve_call(project, "src/main.ts", 4, "helper()", target_path="src/util.ts", target_line=1)
    assert res["kind"] == "definitive" and res["verdict"] == "confirms" and res["tool"].startswith("scip:")
    wrong = precise.resolve_call(project, "src/main.ts", 4, "helper", target_path="src/dup.ts", target_line=1)
    assert wrong["verdict"] == "refutes"
    amb = precise.resolve_call(project, "src/main.ts", 5, "dup", target_path="src/dup.ts", target_line=1)
    assert amb["verdict"] == "undetermined"


def test_python_files_never_use_the_name_based_scip_answer(project, monkeypatch):
    (project / "m.py").write_bytes(b"def f():\n    return 1\n\n\nf()\n")
    data = index_bytes([document("m.py", "python", [occ(0, 4, 5, "scip-python python p 1 m/f().", 1),
                                                    occ(4, 0, 1, "scip-python python p 1 m/f().")])])
    (project / ".repoatlas" / "index").mkdir(parents=True, exist_ok=True)
    (project / "index.scip").unlink()
    time.sleep(0.05)
    (project / ".repoatlas" / "index" / "index.scip").write_bytes(data)
    assert scip_reader.find_index(project) == project / ".repoatlas" / "index" / "index.scip"
    monkeypatch.setattr(precise, "available", lambda: (False, "jedi is not installed"))
    precise.reset_caches()
    assert precise.resolve_call(project, "m.py", 5, "f") is None  # not SCIP: no answer, current behaviour
    # the resolver itself still reads it when used directly
    r = scip_reader.ScipResolver.load(project, scip_reader.find_index(project))
    assert r.resolve("m.py", 5, "f")["kind"] == "definitive"


def test_corrupt_index_is_reported_once(project):
    (project / "index.scip").write_bytes(b"\x12\xff\xff\xff\xff\x0f")
    res = precise.resolve_call(project, "src/main.ts", 4, "helper")
    assert res["kind"] == "unresolved" and "unreadable" in res["reason"]
    assert precise.resolve_call(project, "src/main.ts", 4, "helper") is None


def test_real_index_round_trip_when_available():
    """Optional: the 7.6 MB scip-python index from the research run, if it is on this machine."""
    p = Path(os.environ.get("REPOATLAS_SCIP_SAMPLE", "")) if os.environ.get("REPOATLAS_SCIP_SAMPLE") else None
    if p is None or not p.is_file():
        pytest.skip("set REPOATLAS_SCIP_SAMPLE to a real index.scip to run this check")
    idx = scip_reader.load(p)
    assert idx.documents and sum(len(d.occurrences) for d in idx.documents) > 0
