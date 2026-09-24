"""Notes you write about the code, kept beside it and checked against it (``verinoda ui``, ``verinoda notes``).

A note is a Markdown file in ``.verinoda/notes/`` (``notes.dir`` in ``.verinoda/config.json`` puts
them elsewhere, a folder you commit for instance) with a small header::

    ---
    subject: src/billing.py::Invoice.charge()
    file: src/billing.py
    lines: 40-72
    anchor: {"sym": "Invoice.charge", "fp": "...", ...}
    written: 2026-09-24T20:10:00Z
    ---
    What you want to remember. [[Invoice.total()]] links another note.

The subject names what the note is about: ``file`` for a whole file, ``file::Name`` for a symbol,
a heading or a data unit (the view makes it unique in its file: a second ``__init__()`` is
``file::B.__init__()``, a second ``Usage`` heading ``file::Usage#2``). The anchor pins the note to
the code it was written about:

* a whole file: a hash of the file;
* a symbol, a module statement or a Markdown section, in a language with facts
  (:mod:`verinoda.anchors`): that region's fingerprint, found again wherever it moved, so
  whitespace, comments and moving it change nothing;
* anything else: a hash of its lines.

Read back, a note is ``fresh`` (the code is as it was; ``now`` says where it is), ``changed`` (the
code was edited since, or can no longer be read as code: read the note again, then *keep* it,
which anchors it to the code as it is now) or ``gone`` (the symbol, section or file is no longer
there, or no longer in the index: edit the note onto something else, or delete it).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

MAX_TEXT = 64_000
NOTE_SUFFIX = ".md"
STATUSES = ("fresh", "changed", "gone")
REGION_TABLES = (("sym", "symbols", "full"), ("mod", "module", "h"), ("sec", "sections", "h"))


@dataclass
class UserNote:
    subject: str
    file: str
    start: int
    end: int
    text: str
    anchor: dict = field(default_factory=dict)
    written: str = ""
    path: Path | None = None


def notes_dir(repo: Path) -> Path:
    from verinoda.paths import atlas_dir, load_config

    try:
        configured = ((load_config(Path(repo)).get("notes") or {}).get("dir") or "").strip()
    except Exception:  # noqa: BLE001 - an unreadable config: the default folder
        configured = ""
    if configured:
        p = Path(configured)
        return p if p.is_absolute() else Path(repo) / p
    return atlas_dir(Path(repo)) / "notes"


def _slug(subject: str) -> str:
    s = re.sub(r"[^\w.-]+", "-", subject.replace("::", "--"), flags=re.UNICODE).strip("-.")
    return (s or "note")[:150]


# -- reading and writing ------------------------------------------------------------------------

def _parse(path: Path) -> UserNote | None:
    """A note file, or None when it is not one (a header that cannot be read is not a note)."""
    try:
        raw = path.read_text(encoding="utf-8-sig")  # an editor may have added a byte-order mark
    except (OSError, UnicodeDecodeError):
        return None
    raw = raw.replace("\r\n", "\n")
    if not raw.startswith("---\n"):
        return None
    head, sep, body = raw[4:].partition("\n---\n")
    if not sep:
        return None
    meta: dict[str, str] = {}
    for line in head.split("\n"):
        k, colon, v = line.partition(":")
        if colon:
            meta[k.strip()] = v.strip()
    subject, file = meta.get("subject") or "", meta.get("file") or ""
    m = re.fullmatch(r"(\d{1,9})-(\d{1,9})", meta.get("lines") or "")
    if not subject or not file or not m or not 1 <= int(m.group(1)) <= int(m.group(2)):
        return None
    try:
        anchor = json.loads(meta.get("anchor") or "{}")
    except ValueError:
        anchor = {}
    return UserNote(subject, file, int(m.group(1)), int(m.group(2)), body.strip("\n"),
                    anchor if isinstance(anchor, dict) else {}, meta.get("written") or "", path)


def load_all(repo: Path) -> list[UserNote]:
    d = notes_dir(repo)
    if not d.is_dir():
        return []
    return [n for p in sorted(d.glob("*" + NOTE_SUFFIX)) if (n := _parse(p)) is not None]


def find(repo: Path, subject: str) -> UserNote | None:
    return next((n for n in load_all(repo) if n.subject == subject), None)


def _inside(repo: Path, rel: str) -> Path | None:
    if not rel or rel.startswith(("/", "\\")) or ".." in Path(rel).parts or re.match(r"^[A-Za-z]:", rel):
        return None
    root = Path(repo).resolve()
    p = (root / rel).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return None
    return p


def _lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").split("\n")


def _read(repo: Path, file: str):
    """(path, facts, text) of a project file; text None when it is not there."""
    from verinoda import anchors

    p = _inside(repo, file)
    if p is None or not p.is_file():
        return p, None, None
    facts, text = anchors.facts_for_path(p, file)
    return p, facts, text


def _region(facts: dict | None, anchor: dict) -> tuple[dict | None, str | None]:
    """The symbol / module statement / section an anchor names, in these facts, and its fingerprint key."""
    for key, table, fp in REGION_TABLES:
        if key in anchor:
            return ((facts or {}).get(table) or {}).get(anchor[key]), fp
    return None, None


def make_anchor(repo: Path, file: str, start: int, end: int, *, whole_file: bool = False) -> dict:
    """What pins a note to ``file`` lines ``start..end`` now (see the module docstring)."""
    from verinoda import anchors

    _p, facts, text = _read(repo, file)
    if text is None:
        raise ValueError(f"not a file of the project: {file}")
    lines = _lines(text)
    if whole_file:
        return {"file": anchors.text_hash(text), "n_lines": len(lines)}
    a = anchors.make_anchor(facts, text, start, end) if facts is not None else None
    if a is not None:
        return a
    return {"text": anchors.text_hash("\n".join(lines[start - 1:end])), "n_lines": end - start + 1}


def save(repo: Path, subject: str, file: str, start: int, end: int, text: str) -> UserNote | None:
    """Write (or, with blank ``text``, delete) the note on ``subject``, anchored to the code as it is now.
    A note on a whole file (``subject == file``) covers the whole file whatever ``start``/``end`` say."""
    text = (text or "").replace("\r\n", "\n").strip("\n")
    if len(text) > MAX_TEXT:
        raise ValueError(f"a note holds at most {MAX_TEXT} characters")
    if any(c in s for s in (subject, file) for c in "\r\n") or not subject.strip():
        raise ValueError("a subject or file name cannot hold a line break")  # the header is line by line
    if not text.strip():
        delete(repo, subject)
        return None
    whole = subject == file
    if whole:
        _p, _f, current = _read(repo, file)
        if current is None:
            raise ValueError(f"not a file of the project: {file}")
        start, end = 1, max(1, len(_lines(current)))
    if not 1 <= start <= end:
        raise ValueError("bad line range")
    anchor = make_anchor(repo, file, start, end, whole_file=whole)
    written = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = find(repo, subject)
    d = notes_dir(repo)
    d.mkdir(parents=True, exist_ok=True)
    path = old.path if old is not None and old.path is not None else _free_path(d, _slug(subject))
    body = (f"---\nsubject: {subject}\nfile: {file}\nlines: {start}-{end}\n"
            f"anchor: {json.dumps(anchor, separators=(',', ':'), ensure_ascii=False)}\nwritten: {written}\n---\n"
            f"{text}\n")
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return UserNote(subject, file, start, end, text, anchor, written, path)


def delete(repo: Path, subject: str) -> bool:
    """Remove the note on ``subject`` (whatever became of its code); True when there was one."""
    old = find(repo, subject)
    if old is None or old.path is None:
        return False
    old.path.unlink(missing_ok=True)
    return True


def _free_path(d: Path, slug: str) -> Path:
    p = d / (slug + NOTE_SUFFIX)
    k = 2
    while p.exists():
        p = d / f"{slug}-{k}{NOTE_SUFFIX}"
        k += 1
    return p


# -- is it still about the code as it is? ---------------------------------------------------------

def check(repo: Path, note: UserNote, *, resolves: bool | None = None) -> dict:
    """``{"status": fresh | changed | gone, "start", "end", "why"}`` for ``note`` against the files now.
    ``resolves=False``: the index no longer knows the subject (renamed or deleted), so it is gone."""
    from verinoda import anchors

    def out(status, start=None, end=None, why=""):
        return {"status": status, "start": start, "end": end, "why": why}

    if resolves is False:
        return out("gone", why=f"{note.subject} is not in the index any more (renamed or deleted?)")
    _p, facts, text = _read(repo, note.file)
    if text is None:
        return out("gone", why=f"{note.file} is no longer there")
    lines = _lines(text)
    a = note.anchor or {}
    if "file" in a:  # the whole file
        if anchors.text_hash(text) == a["file"]:
            return out("fresh", 1, len(lines))
        return out("changed", 1, len(lines), "the file was edited")
    region, fp_key = _region(facts, a)
    if fp_key is not None:  # a symbol, module statement or section: found wherever it is now
        if facts is None:
            return out("changed", note.start, note.end, f"{note.file} cannot be read as code now (a syntax error?)")
        if region is None:
            return out("gone", why=f"{a.get('sym') or a.get('sec') or a.get('mod')} is no longer in {note.file}")
        span = (region["start"], region["end"])
        if region.get(fp_key) == a.get("fp"):
            return out("fresh", *span, "" if span == (note.start, note.end) else "moved")
        return out("changed", *span, "the code was edited")
    if "text" in a:  # lines: the same text at the same or another place
        try:
            n = int(a.get("n_lines") or 0)
        except (TypeError, ValueError):
            n = 0
        if not 1 <= n <= len(lines):
            n = min(note.end - note.start + 1, len(lines))
        if anchors.text_hash("\n".join(lines[note.start - 1:note.start - 1 + n])) == a["text"]:
            return out("fresh", note.start, note.start + n - 1)
        for s in range(1, len(lines) - n + 2):
            if anchors.text_hash("\n".join(lines[s - 1:s - 1 + n])) == a["text"]:
                return out("fresh", s, s + n - 1, "moved")
        return out("changed", note.start, note.end, "the lines were edited")
    return out("changed", note.start, note.end, "the note has no anchor: keep it to pin it to the code")


def keep(repo: Path, note: UserNote, *, span: tuple[int, int] | None = None,
         resolves: bool | None = None) -> UserNote | None:
    """Anchor ``note`` to the code as it is now (after it was read again), text unchanged.

    A whole-file note covers the file; a symbol / section note the region's current lines; a
    note pinned by lines the ``span`` the caller knows from an up-to-date index (else the lines it
    was found at). Refused when the code is gone or cannot be read as code now."""
    st = check(repo, note, resolves=resolves)
    if st["status"] == "gone":
        raise ValueError(f"the code of {note.subject} is gone: edit the note onto something else or delete it")
    a = note.anchor or {}
    if any(k in a for k, _t, _f in REGION_TABLES):
        _p, facts, _text = _read(repo, note.file)
        if facts is None:
            raise ValueError(f"{note.file} cannot be read as code now: fix it, then keep the note")
        start, end = st["start"], st["end"]
    elif "file" in a or note.subject == note.file:
        start, end = 1, 1  # save() covers the whole file
    else:
        start, end = span or (st["start"] or note.start, st["end"] or note.end)
    return save(repo, note.subject, note.file, start, end, note.text)


def as_dict(repo: Path, note: UserNote, *, resolves: bool | None = None) -> dict:
    try:
        st = check(repo, note, resolves=resolves)
    except Exception as exc:  # noqa: BLE001 - a hand-edited note must not take the page down
        st = {"status": "changed", "start": None, "end": None, "why": f"cannot check this note: {exc}"[:200]}
    return {"subject": note.subject, "file": note.file, "lines": [note.start, note.end], "text": note.text,
            "written": note.written, "status": st["status"], "now": [st["start"], st["end"]] if st["start"] else None,
            "why": st["why"], "path": note.path.name if note.path else None}
