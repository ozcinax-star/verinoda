"""Small PDF, Word, Excel and PowerPoint files written from scratch for the document tests
(verinoda/doctext.py): no Office, no third-party writer."""
from __future__ import annotations

import zipfile
from pathlib import Path

_CT = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'


def write_pdf(path: Path, pages: list[list[str]]) -> Path:
    """A PDF whose pages hold these lines of text (Helvetica, one text object per page)."""
    objs: list[bytes] = []
    kids = []
    font_id = 3 + 2 * len(pages)
    for i, lines in enumerate(pages):
        page_id, content_id = 3 + 2 * i, 4 + 2 * i
        kids.append(f"{page_id} 0 R")
        ops = ["BT", "/F1 12 Tf", "14 TL", "72 720 Td"]
        for ln in lines:
            esc = ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({esc}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        objs.append((page_id, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
                              f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()))
        objs.append((content_id, b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"))
    objs.insert(0, (2, f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>".encode()))
    objs.insert(0, (1, b"<< /Type /Catalog /Pages 2 0 R >>"))
    objs.append((font_id, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))
    objs.sort()
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for oid, body in objs:
        offsets[oid] = len(out)
        out += b"%d 0 obj\n" % oid + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for oid, _ in objs:
        out += b"%010d 00000 n \n" % offsets[oid]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(bytes(out))
    return path


def write_docx(path: Path, blocks: list[tuple[str, str]], table: list[list[str]] | None = None) -> Path:
    """A .docx of ``(style, text)`` paragraphs (style "" or "Heading1"...) and an optional table."""
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = []
    for style, text in blocks:
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        body.append(f"<w:p>{ppr}<w:r><w:t>{_x(text)}</w:t></w:r></w:p>")
    if table:
        rows = "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{_x(c)}</w:t></w:r></w:p></w:tc>" for c in r)
                       + "</w:tr>" for r in table)
        body.append(f"<w:tbl>{rows}</w:tbl>")
    doc = f'{_CT}<w:document {w}><w:body>{"".join(body)}</w:body></w:document>'
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml", doc)
    return path


def write_xlsx(path: Path, sheets: dict[str, list[list[str]]]) -> Path:
    """An .xlsx with these sheets; text cells go through the shared-string table, numbers stay numbers."""
    s = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    r = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    shared: list[str] = []
    wb_sheets, rels = [], []
    files = {}
    for n, (name, rows) in enumerate(sheets.items(), 1):
        xml_rows = []
        for i, row in enumerate(rows, 1):
            cells = []
            for j, v in enumerate(row):
                ref = f"{chr(65 + j)}{i}"
                if isinstance(v, (int, float)):
                    cells.append(f'<c r="{ref}"><v>{v}</v></c>')
                else:
                    shared.append(str(v))
                    cells.append(f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>')
            xml_rows.append(f'<row r="{i}">{"".join(cells)}</row>')
        files[f"xl/worksheets/sheet{n}.xml"] = f'{_CT}<worksheet {s}><sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
        wb_sheets.append(f'<sheet name="{_x(name)}" sheetId="{n}" r:id="rId{n}"/>')
        rels.append(f'<Relationship Id="rId{n}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                    f'relationships/worksheet" Target="worksheets/sheet{n}.xml"/>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml", f'{_CT}<workbook {s} {r}><sheets>{"".join(wb_sheets)}</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", f'{_CT}<Relationships xmlns="http://schemas.openxmlformats.org/'
                   f'package/2006/relationships">{"".join(rels)}</Relationships>')
        z.writestr("xl/sharedStrings.xml", f'{_CT}<sst {s}>' + "".join(f"<si><t>{_x(t)}</t></si>" for t in shared)
                   + "</sst>")
        for name, xml in files.items():
            z.writestr(name, xml)
    return path


def write_pptx(path: Path, slides: list[list[str]]) -> Path:
    """A .pptx whose slides hold these paragraphs."""
    p = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
    a = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    r = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    ids, rels = [], []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for n, paras in enumerate(slides, 1):
            body = "".join(f"<a:p><a:r><a:t>{_x(t)}</a:t></a:r></a:p>" for t in paras)
            z.writestr(f"ppt/slides/slide{n}.xml", f"{_CT}<p:sld {p} {a}><p:cSld><p:spTree><p:sp><p:txBody>"
                       f"{body}</p:txBody></p:sp></p:spTree></p:cSld></p:sld>")
            ids.append(f'<p:sldId id="{255 + n}" r:id="rId{n}"/>')
            rels.append(f'<Relationship Id="rId{n}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                        f'relationships/slide" Target="slides/slide{n}.xml"/>')
        z.writestr("ppt/presentation.xml", f'{_CT}<p:presentation {p} {r}><p:sldIdLst>{"".join(ids)}</p:sldIdLst>'
                   "</p:presentation>")
        z.writestr("ppt/_rels/presentation.xml.rels", f'{_CT}<Relationships xmlns="http://schemas.openxmlformats.org/'
                   f'package/2006/relationships">{"".join(rels)}</Relationships>')
    return path


def _x(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
