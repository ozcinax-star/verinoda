"""Documents and images next to the code (verinoda/doctext.py, docs/DESIGN.md D41): the text view of
PDF, Word, Excel and PowerPoint files, OCR of images on Windows, and how the views reach the graph,
the search index, the lexicon and evidence."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import io  # noqa: E402
import json  # noqa: E402
import struct  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import zipfile  # noqa: E402
import zlib  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from docfixtures import write_docx, write_pdf, write_pptx, write_xlsx  # noqa: E402
from verinoda import doctext, evidence, index, retrieval, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402


def _png(w: int, h: int) -> bytes:
    raw = b"".join(b"\x00" + b"\xff" * (w * 3) for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))  # noqa: E731
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 0)) + chunk(b"IEND", b""))


# -- the text views ---------------------------------------------------------------------------------

def test_pdf_view_has_a_titled_heading_per_page(tmp_path):
    p = write_pdf(tmp_path / "spec.pdf", [["Payment retry policy", "Retries use exponential backoff."],
                                          [], ["Refunds within 5 days."]])
    rec = doctext.convert(p, p.read_bytes())
    assert rec["method"] == "pypdf" and rec["note"] is None
    assert rec["lines"][0] == "# Page 1: Payment retry policy"
    assert "Retries use exponential backoff." in rec["lines"]
    assert "# Page 3: Refunds within 5 days." in rec["lines"]  # the empty page 2 has no heading
    assert not any(ln.startswith("# Page 2") for ln in rec["lines"])


def test_docx_view_keeps_headings_paragraphs_and_tables(tmp_path):
    p = write_docx(tmp_path / "d.docx", [("Heading1", "Order pipeline"), ("", "Orders are validated."),
                                         ("Balk2", "Fiyatlandırma"), ("", "İndirim uygulanır.")],
                   table=[["Field", "Type"], ["total", "Decimal"]])
    lines = doctext.convert(p, p.read_bytes())["lines"]
    assert lines[0] == "# Order pipeline"
    assert "## Fiyatlandırma" in lines  # a Turkish Word's heading style id
    assert "İndirim uygulanır." in lines and "total | Decimal" in lines


def test_xlsx_view_has_a_sheet_heading_and_numbered_rows(tmp_path):
    p = write_xlsx(tmp_path / "l.xlsx", {"Limits": [["name", "value"], ["max_retries", 5]],
                                         "Empty": []})
    lines = doctext.convert(p, p.read_bytes())["lines"]
    assert lines[:4] == ["# Sheet: Limits", "", "row 1: name | value", "row 2: max_retries | 5"]
    assert not any("Empty" in ln for ln in lines)


def test_pptx_view_has_a_titled_heading_per_slide(tmp_path):
    p = write_pptx(tmp_path / "d.pptx", [["Architecture", "Graph first"], ["Roadmap", "OCR on Windows"]])
    lines = doctext.convert(p, p.read_bytes())["lines"]
    assert lines[0] == "# Slide 1: Architecture" and "# Slide 2: Roadmap" in lines and "Graph first" in lines


def test_broken_and_hostile_documents_give_a_note_never_an_exception(tmp_path):
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"not a zip")
    assert doctext.convert(bad, bad.read_bytes())["note"].startswith("refused")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>')
    ent = tmp_path / "ent.docx"
    ent.write_bytes(buf.getvalue())
    rec = doctext.convert(ent, ent.read_bytes())
    assert rec["lines"] == [] and rec["note"].startswith("unreadable")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:  # a zip bomb: 1000x compression
        z.writestr("word/document.xml", "a" * 20_000_000)
    bomb = tmp_path / "bomb.docx"
    bomb.write_bytes(buf.getvalue())
    assert doctext.convert(bomb, bomb.read_bytes())["note"] == "refused: a likely zip bomb"
    scanned = write_pdf(tmp_path / "scan.pdf", [[]])
    assert doctext.convert(scanned, scanned.read_bytes())["note"].startswith("no text layer")


def test_image_size_and_which_images_are_read():
    assert doctext.image_size(_png(640, 200)) == (640, 200)
    assert doctext.image_size(b"GIF89a" + struct.pack("<HH", 32, 16)) == (32, 16)
    jpeg = b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 4) + b"JF" + b"\xff\xc0" + struct.pack(">HBHH", 11, 8, 300, 500)
    assert doctext.image_size(jpeg + b"\x00" * 8) == (500, 300)
    big = _png(800, 400)
    assert doctext.ocr_candidate("docs/screen.png", big, size=50_000) is None
    assert "texture" in doctext.ocr_candidate("assets/mod/textures/block/x.png", big, size=50_000)
    assert "small image (16x16)" == doctext.ocr_candidate("docs/i.png", _png(16, 16), size=10_000)
    assert "icon" in doctext.ocr_candidate("docs/i.png", big, size=100)


# -- a project with documents -------------------------------------------------------------------------

def _scan(root: Path) -> index.Graph:
    workflow.init(root)
    st = open_store(root)
    try:
        workflow.scan(st, root)
    finally:
        st.close()
    return index.load(root)


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    root = tmp_path_factory.mktemp("docs") / "proj"
    (root / "app").mkdir(parents=True)
    (root / "app" / "retry.py").write_text("def backoff(attempt):\n    return 2 ** attempt\n", encoding="utf-8")
    (root / "docs").mkdir()
    write_pdf(root / "docs" / "spec.pdf", [["Payment retry policy", "Retries use exponential backoff (base 2s)."],
                                          ["Refund rules", "Refunds are issued within five days."]])
    write_docx(root / "docs" / "design.docx", [("Heading1", "Shipping rules"),
                                               ("", "Express orders ship the same day when paid before noon.")])
    write_xlsx(root / "docs" / "limits.xlsx", {"Limits": [["name", "value"], ["max_retries", 5]]})
    return _scan(root)


def test_documents_get_graph_nodes_per_page_and_heading(project):
    labels = {d.get("label"): d.get("source_file") for _, d in project.G.nodes(data=True)}
    assert labels.get("Page 1: Payment retry policy") == "docs/spec.pdf"
    assert labels.get("Page 2: Refund rules") == "docs/spec.pdf"
    assert labels.get("Shipping rules") == "docs/design.docx"
    assert labels.get("Sheet: Limits") == "docs/limits.xlsx"
    heads = project.headings_in("docs/spec.pdf")
    assert [project.label(n) for n in heads] == ["Page 1: Payment retry policy", "Page 2: Refund rules"]
    assert project.span(heads[0]) == (1, 4)


def test_a_question_finds_the_document_passage(project):
    res = retrieval.retrieve(project, "payment retry backoff policy", retrieval.Budget(max_items=5, max_chars=4000))
    text = retrieval.render_text(res, 4000)
    assert "docs/spec.pdf:1-4" in text and "Retries use exponential backoff (base 2s)." in text
    res = retrieval.retrieve(project, "when do express orders ship", retrieval.Budget(max_items=5, max_chars=4000))
    assert "docs/design.docx" in retrieval.render_text(res, 4000)


def test_evidence_and_file_lines_read_the_view(project):
    p = project.root / "docs" / "spec.pdf"
    assert evidence.read_lines(p, 3, 4) == "Payment retry policy\nRetries use exponential backoff (base 2s)."
    assert index.file_lines(p)[0] == "# Page 1: Payment retry policy"
    assert (project.root / ".verinoda" / "index" / "doctext").is_dir()  # read once per content


def test_the_lexicon_knows_document_words(project):
    lex = json.loads((project.root / ".verinoda" / "index" / "lexicon.json").read_text(encoding="utf-8"))
    assert "docs/spec.pdf" in lex["files"]
    assert {"backoff", "refunds", "express"} <= set(lex["vocab"])


def test_a_new_document_reaches_the_graph_on_update(project):
    root = project.root
    write_pptx(root / "docs" / "deck.pptx", [["Architecture", "Evidence before answers"]])
    st = open_store(root)
    try:
        res = workflow.update(st, root)
    finally:
        st.close()
    assert res["index_mode"] == "full"
    g = index.load(root)
    assert any(d.get("label") == "Slide 1: Architecture" for _, d in g.G.nodes(data=True))


# -- OCR --------------------------------------------------------------------------------------------

def test_ocr_runs_once_per_image_content_and_feeds_the_index(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / "docs" / "screens").mkdir(parents=True)
    (root / "app.py").write_text("def compute_total():\n    return 1\n", encoding="utf-8")
    shot = root / "docs" / "screens" / "error.png"
    shot.write_bytes(_png(640, 200) + b"\x00" * 8000)
    (root / "assets" / "textures").mkdir(parents=True)
    (root / "assets" / "textures" / "stone.png").write_bytes(_png(640, 200) + b"\x00" * 8000)
    calls = []
    real_run = subprocess.run

    def fake_run(cmd, **kw):
        if "-List" not in cmd:  # git and anything else the scan runs
            return real_run(cmd, **kw)
        lst, out = Path(cmd[cmd.index("-List") + 1]), Path(cmd[cmd.index("-Out") + 1])
        paths = lst.read_text(encoding="utf-8").splitlines()
        calls.append(paths)
        out.write_text(json.dumps([{"path": p, "lang": "tr", "lines": ["Error: compute_total failed",
                                                                        "Sipariş toplamı hesaplanamadı"]}
                                   for p in paths]), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(doctext, "ocr_available", lambda: True)
    monkeypatch.setattr(doctext, "_powershell", lambda: "powershell.exe")
    monkeypatch.setattr(doctext.subprocess, "run", fake_run)
    g = _scan(root)
    assert len(calls) == 1 and [Path(p).name for p in calls[0]] == ["error.png"]  # textures are not read
    res = retrieval.retrieve(g, "sipariş toplamı hesaplanamadı", retrieval.Budget(max_items=5, max_chars=4000))
    text = retrieval.render_text(res, 4000)
    assert "docs/screens/error.png" in text and "Text in the image (OCR)" in text
    st = open_store(root)
    try:
        workflow.update(st, root)
    finally:
        st.close()
    assert len(calls) == 1  # cached by content: not read again


@pytest.mark.skipif(sys.platform != "win32", reason="the OCR engine is Windows'")
def test_windows_ocr_reads_a_rendered_image(tmp_path):
    img = tmp_path / "shot.png"
    ps = ("Add-Type -AssemblyName System.Drawing; $b = New-Object System.Drawing.Bitmap 900,160; "
          "$g = [System.Drawing.Graphics]::FromImage($b); $g.Clear([System.Drawing.Color]::White); "
          "$f = New-Object System.Drawing.Font('Arial', 28); "
          "$g.DrawString('Payment retry failed', $f, [System.Drawing.Brushes]::Black, 20, 40); "
          f"$b.Save('{img}', [System.Drawing.Imaging.ImageFormat]::Png)")
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps], check=True,
                       capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("PowerShell could not draw the test image")
    (tmp_path / ".verinoda").mkdir()
    if not doctext.ocr_available():
        pytest.skip("OCR switched off here")
    got = doctext.ocr_images([img])
    rec = got.get(str(img.resolve()))
    if rec is None or (rec.get("note") or "").startswith("OCR failed"):
        pytest.skip(f"the OCR engine is not usable here: {rec}")
    assert rec["lines"][0] == doctext.OCR_HEADING
    assert "retry" in " ".join(rec["lines"]).lower()
    assert doctext.text_lines(img)[0] == doctext.OCR_HEADING
