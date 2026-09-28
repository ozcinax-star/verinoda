"""The package and its tests keep LF line endings.

`.gitattributes` is `* -text`: git stores the bytes as written, so an editor that saves a module with CRLF
rewrites every line of it in the history (review of D66, finding 3: four files, 4,950 diff lines for about
90 real ones) and every other branch touching the file conflicts on all of it.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# CRLF before this check existed; converting them is a separate whole-file rewrite
ALREADY_CRLF = {"verinoda/textnorm.py"}


def test_python_sources_have_no_carriage_returns():
    offenders = []
    for top in ("verinoda", "tests"):
        for p in sorted((ROOT / top).rglob("*.py")):
            rel = p.relative_to(ROOT).as_posix()
            if rel in ALREADY_CRLF:
                continue
            if b"\r" in p.read_bytes():
                offenders.append(rel)
    assert offenders == [], f"CR bytes in {offenders}: save these files with LF line endings"
