"""Text of the documents and images kept next to the code (docs/DESIGN.md D41).

A repository carries more than code: PDF specifications and papers, Word design documents, Excel
tables, PowerPoint decks, screenshots and diagrams. Their text is made searchable here, without a
model and without sending anything anywhere:

- PDF through pypdf (a page's text layer; a scanned page without one has no text here);
- Word ``.docx``, Excel ``.xlsx`` and PowerPoint ``.pptx`` with the standard library (they are zip
  files of XML), screened for zip bombs and refusing XML that declares entities;
- images (``.png``, ``.jpg``, ...) on Windows through the operating system's own OCR engine
  (``Windows.Media.Ocr``, run by ``data/ocr_windows.ps1``): nothing is downloaded or installed.
  Small images and texture folders are left out (icons and game textures hold no text worth
  reading), and so are images once :data:`MAX_OCR_PER_RUN` have been read in one run.

The text is a *view* of the file: lines, with a heading line per page (``# Page 3``), sheet
(``# Sheet: Orders``), slide (``# Slide 2``) or image (``# Text in the image (OCR)``). Search
passages, evidence and quotes name the document's own path and lines of this view. The view is
made from the file's bytes the same way every time, so a quote can be checked again; it is kept
under ``.verinoda/index/doctext/<sha256>.json`` so a document is read once per content. OCR text is
what the engine read, errors included (``l`` read as ``I``): answers built on it say so.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

VERSION = 2  # the text view's format; part of the graph's extraction cache key for documents
DOC_SUFFIXES = (".pdf", ".docx", ".xlsx", ".pptx")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff")
OCR_HEADING = "# Text in the image (OCR)"
MAX_DOC_BYTES = 50 * 1024 * 1024       # larger documents are not read
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 1000
MAX_LINES = 60_000                     # a view is cut here (a table of a million rows is data, not a document)
MAX_SHEET_ROWS = 5_000
MAX_OCR_PER_RUN = 300                  # images read in one index run; the rest wait for the next one
OCR_TIMEOUT_BASE = 60                  # seconds for the OCR process, plus OCR_TIMEOUT_PER_IMAGE each
OCR_TIMEOUT_PER_IMAGE = 5
MIN_OCR_SIDE = 120                     # an image with a side shorter than this is an icon or a texture
MIN_OCR_LONG_SIDE = 300
MIN_OCR_BYTES = 4_000
# Folders of textures, sprites, icons and fonts: pictures without text to read (a Minecraft mod has
# thousands under assets/<mod>/textures/).
NO_OCR_DIRS = frozenset({"textures", "texture", "sprites", "sprite", "icons", "icon", "fonts", "font",
                         "particles", "particle", "blockstates", "models", "emoji", "emojis", "favicon",
                         "favicons", "node_modules"})

_MEMO: dict[tuple, list[str] | None] = {}
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def kind(name: str) -> str | None:
    """``pdf`` / ``docx`` / ``xlsx`` / ``pptx`` / ``image`` for a file this module reads, else None."""
    low = str(name).lower()
    for suf in DOC_SUFFIXES:
        if low.endswith(suf):
            return suf[1:]
    if low.endswith(IMAGE_SUFFIXES):
        return "image"
    return None


def pdf_available() -> bool:
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return False
    return True


def ocr_available() -> bool:
    """Windows with PowerShell and OCR not switched off (``VERINODA_OCR=0``)."""
    if os.environ.get("VERINODA_OCR", "").strip().lower() in ("0", "off", "no", "false"):
        return False
    return sys.platform == "win32" and _powershell() is not None


def status() -> dict:
    """What this installation can read (``verinoda doctor``)."""
    return {"pdf": pdf_available(), "office": True, "ocr": ocr_available(),
            "ocr_engine": "Windows.Media.Ocr" if ocr_available() else None}


def _powershell() -> str | None:
    return shutil.which("powershell.exe") or shutil.which("powershell")


# -- the cache -------------------------------------------------------------------------------------

def _cache_dir(path: Path) -> Path | None:
    """``.verinoda/index/doctext`` of the project holding ``path`` (None outside a project)."""
    from verinoda.paths import ATLAS_DIRNAME

    for d in Path(path).resolve().parents:
        if (d / ATLAS_DIRNAME).is_dir():
            return d / ATLAS_DIRNAME / "index" / "doctext"
    return None


def _cache_file(path: Path, sha: str) -> Path | None:
    d = _cache_dir(path)
    return None if d is None else d / f"{sha}.json"


def _read_cache(path: Path, sha: str) -> dict | None:
    f = _cache_file(path, sha)
    if f is None:
        return None
    try:
        rec = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("v") == VERSION else None


def _write_cache(path: Path, sha: str, rec: dict) -> None:
    f = _cache_file(path, sha)
    if f is None:
        return
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"v": VERSION, **rec}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, f)
    except OSError:
        pass


# -- the view --------------------------------------------------------------------------------------

def text_lines(path: Path, data: bytes | None = None) -> list[str] | None:
    """The text view of a document or image (None: not a kind this reads, unreadable, or no text).

    Images are read from the cache only (OCR runs in batches: :func:`ocr_images`); a document is
    converted when it is not cached yet."""
    path = Path(path)
    k = kind(path.name)
    if k is None:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    memo_key = (str(path), st.st_size, st.st_mtime_ns)
    if memo_key in _MEMO:
        return _MEMO[memo_key]
    limit = MAX_IMAGE_BYTES if k == "image" else MAX_DOC_BYTES
    if st.st_size > limit:
        _MEMO[memo_key] = None
        return None
    try:
        data = data if data is not None else path.read_bytes()
    except OSError:
        return None
    sha = hashlib.sha256(data).hexdigest()
    rec = _read_cache(path, sha)
    if rec is None and k != "image":
        rec = convert(path, data, k)
        _write_cache(path, sha, rec)
    lines = (rec or {}).get("lines") or None
    _MEMO[memo_key] = lines
    return lines


def note(path: Path) -> str | None:
    """Why a document has no text view, when it was read and gave none (from the cache)."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    rec = _read_cache(Path(path), hashlib.sha256(data).hexdigest())
    return (rec or {}).get("note")


def convert(path: Path, data: bytes, k: str | None = None) -> dict:
    """``{"kind", "method", "lines", "note"}`` for a document's bytes (never raises)."""
    k = k or kind(path.name)
    try:
        if k == "pdf":
            lines, method = _pdf(data), "pypdf"
        elif k in ("docx", "xlsx", "pptx"):
            if not _zip_ok(data):
                return {"kind": k, "method": "zip", "lines": [], "note": "refused: a likely zip bomb"}
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                lines = {"docx": _docx, "xlsx": _xlsx, "pptx": _pptx}[k](z)
            method = "zip+xml"
        else:
            return {"kind": k, "method": None, "lines": [], "note": "not a document"}
    except _NoPdf:
        return {"kind": k, "method": None, "lines": [], "note": "pypdf is not installed"}
    except Exception as exc:  # noqa: BLE001 - a broken document is reported, never raised
        return {"kind": k, "method": None, "lines": [], "note": f"unreadable: {type(exc).__name__}"}
    lines = _tidy(lines)
    note = None
    if not any(ln.strip() and not ln.startswith("# ") for ln in lines):
        note = "no text layer (a scanned PDF?)" if k == "pdf" else "no text"
        lines = []
    elif len(lines) > MAX_LINES:
        lines, note = lines[:MAX_LINES], f"cut at {MAX_LINES} lines"
    return {"kind": k, "method": method, "lines": lines, "note": note}


def _tidy(lines: list[str]) -> list[str]:
    """Trailing spaces off, control characters out, runs of blank lines kept to one."""
    out: list[str] = []
    for ln in lines:
        ln = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", ln).rstrip()
        if not ln and (not out or not out[-1]):
            continue
        out.append(ln)
    while out and not out[-1]:
        out.pop()
    return out


class _NoPdf(Exception):
    pass


def _zip_ok(data: bytes) -> bool:
    """An Office file is a zip: refuse a likely zip bomb before any XML is read (the limits of the
    upstream office reader, verinoda/project_index/detect.py)."""
    from verinoda.project_index.detect import (_OFFICE_MAX_COMPRESSION_RATIO, _OFFICE_MAX_DECOMPRESSED_BYTES,
                                               _OFFICE_MAX_RAW_BYTES)

    if len(data) > _OFFICE_MAX_RAW_BYTES:
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
    except (zipfile.BadZipFile, OSError, ValueError):
        return False
    total = sum(i.file_size for i in infos)
    packed = sum(i.compress_size for i in infos) or 1
    return total <= _OFFICE_MAX_DECOMPRESSED_BYTES and total / packed <= _OFFICE_MAX_COMPRESSION_RATIO


def _titled(heading: str, lines: list[str]) -> str:
    """``# Page 3`` with the page's first line: ``# Page 3: Payment retry policy`` (a page's name in
    the graph, search results and notes says what is on it)."""
    first = next((ln.strip() for ln in lines if ln.strip()), "")
    first = re.sub(r"\s+", " ", first)[:80].rstrip()
    return f"{heading}: {first}" if first else heading


def _pdf(data: bytes) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise _NoPdf from exc
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # malformed-but-readable files warn a lot
    reader = PdfReader(io.BytesIO(data))
    lines: list[str] = []
    for i, page in enumerate(reader.pages, 1):
        if i > MAX_PDF_PAGES:
            lines.append(f"# (cut after {MAX_PDF_PAGES} pages)")
            break
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - one bad page does not lose the others
            text = ""
        if text.strip():
            page = text.splitlines()
            lines += ["", _titled(f"# Page {i}", page), ""] + page
    return lines


def _xml(z: zipfile.ZipFile, name: str) -> ET.Element | None:
    try:
        raw = z.read(name)
    except KeyError:
        return None
    if b"<!DOCTYPE" in raw[:4096] or b"<!ENTITY" in raw:
        raise ValueError("XML with a DOCTYPE or entities")
    return ET.fromstring(raw)


def _rels(z: zipfile.ZipFile, name: str) -> dict[str, str]:
    """Relationship id -> target (resolved against ``name``'s folder) of a part."""
    folder, _, base = name.rpartition("/")
    root = _xml(z, f"{folder}/_rels/{base}.rels")
    out: dict[str, str] = {}
    for r in (root if root is not None else []):
        target = r.get("Target") or ""
        if r.get("TargetMode") == "External":
            continue
        parts = (target.lstrip("/") if target.startswith("/") else f"{folder}/{target}").split("/")
        norm: list[str] = []
        for p in parts:
            if p == "..":
                if norm:
                    norm.pop()
            elif p and p != ".":
                norm.append(p)
        out[r.get("Id") or ""] = "/".join(norm)
    return out


_HEADING_STYLE = re.compile(r"(?i)^(?:heading|ba[sş]l[iı]k|balk|titre|berschrift|titolo|encabezado|kop)\s*(\d)$")


def _docx(z: zipfile.ZipFile) -> list[str]:
    root = _xml(z, "word/document.xml")
    body = root.find(f"{_W}body") if root is not None else None
    lines: list[str] = []
    if body is None:
        return lines

    def para_text(p: ET.Element) -> str:
        out = []
        for el in p.iter():
            if el.tag == f"{_W}t" and el.text:
                out.append(el.text)
            elif el.tag == f"{_W}tab":
                out.append("\t")
            elif el.tag in (f"{_W}br", f"{_W}cr"):
                out.append(" ")
        return "".join(out).strip()

    for el in body:
        if el.tag == f"{_W}p":
            text = para_text(el)
            style = el.find(f"{_W}pPr/{_W}pStyle")
            m = _HEADING_STYLE.match((style.get(f"{_W}val") or "") if style is not None else "")
            if text and m:
                lines += ["", "#" * min(int(m.group(1)) or 1, 6) + " " + text, ""]
            else:
                lines.append(text)
        elif el.tag == f"{_W}tbl":
            for tr in el.iter(f"{_W}tr"):
                cells = [" ".join(para_text(p) for p in tc.iter(f"{_W}p")).strip() for tc in tr.iter(f"{_W}tc")]
                if any(cells):
                    lines.append(" | ".join(cells))
            lines.append("")
    return lines


def _cell_value(c: ET.Element, shared: list[str]) -> str:
    t = c.get("t")
    if t == "inlineStr":
        return "".join(x.text or "" for x in c.iter(f"{_S}t"))
    v = c.find(f"{_S}v")
    raw = v.text if v is not None and v.text is not None else ""
    if t == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    if t == "b":
        return "TRUE" if raw == "1" else "FALSE" if raw == "0" else raw
    return raw


def _xlsx(z: zipfile.ZipFile) -> list[str]:
    shared: list[str] = []
    sroot = _xml(z, "xl/sharedStrings.xml")
    for si in (sroot if sroot is not None else []):
        shared.append("".join(t.text or "" for t in si.iter(f"{_S}t")))
    wb = _xml(z, "xl/workbook.xml")
    rels = _rels(z, "xl/workbook.xml")
    lines: list[str] = []
    sheets = wb.find(f"{_S}sheets") if wb is not None else None
    for sh in (sheets if sheets is not None else []):
        target = rels.get(sh.get(f"{_R}id") or "")
        root = _xml(z, target) if target else None
        if root is None:
            continue
        rows = []
        for n, row in enumerate(root.iter(f"{_S}row")):
            if n >= MAX_SHEET_ROWS:
                rows.append(f"(cut after {MAX_SHEET_ROWS} rows)")
                break
            vals = [_cell_value(c, shared).strip() for c in row.iter(f"{_S}c")]
            while vals and not vals[-1]:
                vals.pop()
            if any(vals):
                rows.append(f"row {row.get('r') or n + 1}: " + " | ".join(vals))
        if rows:
            lines += ["", f"# Sheet: {sh.get('name') or '?'}", ""] + rows
    return lines


def _pptx(z: zipfile.ZipFile) -> list[str]:
    pres = _xml(z, "ppt/presentation.xml")
    rels = _rels(z, "ppt/presentation.xml")
    lines: list[str] = []
    ids = pres.find(f"{_P}sldIdLst") if pres is not None else None
    for n, sid in enumerate(ids if ids is not None else [], 1):
        target = rels.get(sid.get(f"{_R}id") or "")
        root = _xml(z, target) if target else None
        if root is None:
            continue
        paras = []
        for p in root.iter(f"{_A}p"):
            text = "".join(t.text or "" for t in p.iter(f"{_A}t")).strip()
            if text:
                paras.append(text)
        if paras:
            lines += ["", _titled(f"# Slide {n}", paras), ""] + paras
    return lines


# -- images: OCR -----------------------------------------------------------------------------------

def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a PNG, GIF, BMP or JPEG header; None when unknown."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if data[:2] == b"BM" and len(data) >= 26:
            w, h = struct.unpack("<ii", data[18:26])
            return abs(w), abs(h)
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    except struct.error:
        return None
    return None


def ocr_candidate(rel: str, head: bytes, *, size: int | None = None) -> str | None:
    """None when an image is worth reading, else why it is not. ``head``: the file's first bytes (the
    header gives the dimensions); ``size``: the file's size (default ``len(head)``)."""
    size = len(head) if size is None else size
    if any(part.lower() in NO_OCR_DIRS for part in rel.split("/")[:-1]):
        return "image in a texture, icon or font folder"
    if size < MIN_OCR_BYTES:
        return "small image (an icon?)"
    if size > MAX_IMAGE_BYTES:
        return f"image larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB"
    dims = image_size(head)
    if dims and (min(dims) < MIN_OCR_SIDE or max(dims) < MIN_OCR_LONG_SIDE):
        return f"small image ({dims[0]}x{dims[1]})"
    return None


def ocr_images(paths: list[Path]) -> dict[str, dict]:
    """Read the text of images not in the cache yet, in one OCR process; returns ``{path: record}``
    for those read now. Images already cached are skipped (their text is in the cache)."""
    todo: list[tuple[Path, str]] = []
    for p in paths:
        try:
            sha = hashlib.sha256(Path(p).read_bytes()).hexdigest()
        except OSError:
            continue
        if _read_cache(Path(p), sha) is None:
            todo.append((Path(p).resolve(), sha))
    if not todo or not ocr_available():
        return {}
    todo = todo[:MAX_OCR_PER_RUN]
    script = Path(__file__).with_name("data") / "ocr_windows.ps1"
    out: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="verinoda-ocr-") as tmp:
        lst, res = Path(tmp) / "list.txt", Path(tmp) / "out.json"
        lst.write_text("\n".join(str(p) for p, _ in todo), encoding="utf-8")
        try:
            subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-File", str(script), "-List", str(lst), "-Out", str(res)],
                           capture_output=True, timeout=OCR_TIMEOUT_BASE + OCR_TIMEOUT_PER_IMAGE * len(todo),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
            got = json.loads(res.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, subprocess.SubprocessError):
            return {}
    by_path = {str(p): sha for p, sha in todo}
    for item in got if isinstance(got, list) else [got]:
        if not isinstance(item, dict) or str(item.get("path")) not in by_path:
            continue
        p = str(item["path"])
        if item.get("error"):
            rec = {"kind": "image", "method": "Windows.Media.Ocr", "lines": [], "note": f"OCR failed: {item['error']}"[:200]}
        else:
            text = [str(x) for x in (item.get("lines") or []) if str(x).strip()]
            rec = {"kind": "image", "method": "Windows.Media.Ocr", "lang": item.get("lang"),
                   "lines": ([OCR_HEADING, ""] + _tidy(text)) if text else [],
                   "note": None if text else "no text found"}
        _write_cache(Path(p), by_path[p], rec)
        out[p] = rec
    _MEMO.clear()
    return out
