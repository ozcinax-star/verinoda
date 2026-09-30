"""``api NAME --docs``: the installed docstring and README section, quoted with their lines."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from verinoda import codecheck, libdocs

LIB = '''"""Fancylib: retries made simple."""


class Client:
    """A client that retries.

    Pass ``retries`` to change how often.
    """

    def get(self, url):
        """Fetch ``url``."""
        return url


@staticmethod
def plain(x):
    return x


def real_fn(x, *, retries=3):
    """Call once, retry on failure."""
    return x
'''

README = """Metadata-Version: 2.1
Name: fancylib
Version: 2.1.0
Description-Content-Type: text/markdown

# fancylib

Retries for everyone.

## Install

```sh
# not a heading: a comment in a shell example
pip install fancylib
```

## Using Client

Build a `Client` and call `get`.

## Other

`real_fn` is the functional form.
"""


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")
    return p


def _line(text: str, start: str) -> int:
    return next(i for i, ln in enumerate(text.splitlines(), 1) if ln.startswith(start))


@pytest.fixture(scope="module")
def venv_proj(tmp_path_factory):
    root = tmp_path_factory.mktemp("docs ğ proj")   # a space and non-ASCII, like this repository's own path
    venv = root / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, capture_output=True)
    site = [venv / "Lib" / "site-packages"] if os.name == "nt" else sorted((venv / "lib").glob("python*/site-packages"))
    _write(site[0], "fancylib/__init__.py", LIB)
    _write(site[0], "fancylib-2.1.0.dist-info/METADATA", README)
    _write(site[0], "fancylib-2.1.0.dist-info/RECORD", "fancylib/__init__.py,,\n")
    _write(root, "app.py", "import fancylib\n")
    yield root
    codecheck.reset_caches()


def _quote(res: dict, kind: str) -> dict:
    return next(q for q in res["docs"]["quotes"] if q["kind"] == kind)


def test_class_docs_quote_the_installed_docstring_and_the_readme_section_naming_it(venv_proj):
    res = codecheck.api(venv_proj, "fancylib.Client", docs=True)
    assert res["found"] and res["docs"]["version"] == "fancylib 2.1.0"
    doc = _quote(res, "docstring")
    start = _line(LIB, '    """A client')
    assert doc["at"].endswith(f"site-packages/fancylib/__init__.py:{start}-{start + 3}")
    assert doc["text"].startswith("A client that retries.") and "``retries``" in doc["text"]
    readme = _quote(res, "readme")
    assert readme["heading"] == "Using Client" and readme["text"].splitlines()[-1] == "Build a `Client` and call `get`."
    assert readme["at"].endswith(f"fancylib-2.1.0.dist-info/METADATA:{_line(README, '## Using')}-"
                                 f"{_line(README, 'Build a')}")


def test_a_function_found_by_its_text_and_a_package_by_its_opening_section(venv_proj):
    fn = codecheck.api(venv_proj, "fancylib.real_fn", docs=True)
    assert _quote(fn, "docstring")["text"] == "Call once, retry on failure."
    assert _quote(fn, "readme")["heading"] == "Other"          # named in the text, not in a heading
    top = codecheck.api(venv_proj, "fancylib", docs=True)
    assert _quote(top, "docstring")["text"] == "Fancylib: retries made simple."
    assert _quote(top, "readme")["heading"] == "fancylib"


def test_no_docstring_and_no_readme_mention_are_said_not_invented(venv_proj):
    meth = codecheck.api(venv_proj, "fancylib.plain", docs=True)
    assert [q["kind"] for q in meth["docs"]["quotes"]] == []
    assert "the definition has no docstring" in meth["docs"]["notes"]
    assert any("names fancylib.plain or plain" in n for n in meth["docs"]["notes"])
    # without --docs the answer is unchanged
    assert "docs" not in codecheck.api(venv_proj, "fancylib.Client")
    # a name that is not found gets no docs block
    assert "docs" not in codecheck.api(venv_proj, "fancylib.Nope", docs=True)


def test_standard_library_docs_come_from_its_source_file(tmp_path):
    res = codecheck.api(tmp_path, "json.loads", env="none", docs=True)
    doc = _quote(res, "docstring")
    assert "JSON" in doc["text"] and "json/__init__.py:" in doc["at"]
    assert res["docs"]["notes"] == ["the standard library: no packaged README to quote"]
    codecheck.reset_caches()


def test_headings_skip_fenced_code_and_find_rst_titles(tmp_path):
    info = _write(tmp_path, "x-1.dist-info/METADATA", "Name: x\nVersion: 1\n\n=====\nTitle\n=====\n\nIntro.\n\n"
                                                      "Usage\n-----\n\n.. code::\n\n  x.Widget()\n")
    sec = libdocs.readme_section(info.parent, ["Widget"])
    assert sec["heading"] == "Usage" and sec["start"] == 10 and "x.Widget()" in sec["text"]
    assert libdocs.readme_section(info.parent, ["Absent"]) is None
    md = _write(tmp_path, "y-1.dist-info/METADATA", "Name: y\n\n```\n# comment Widget\n```\n")
    assert libdocs.readme_section(md.parent, ["Widget"])["heading"] is None   # one section, no heading
    empty = _write(tmp_path, "z-1.dist-info/METADATA", "Name: z\nVersion: 1\n")
    assert libdocs.readme_section(empty.parent, ["z"]) is None


def test_long_docs_are_capped_and_marked(tmp_path):
    body = "\n".join(f"line {i}" for i in range(200))
    src = _write(tmp_path, "m.py", f'"""{body}"""\n')
    doc = libdocs.docstring(src)
    assert doc["truncated"] and len(doc["text"].splitlines()) == libdocs.MAX_LINES
    assert libdocs.docstring(_write(tmp_path, "bad.py", "def (:\n")) is None


def test_an_overloaded_function_quotes_its_implementation_docstring(tmp_path):
    src = _write(tmp_path, "o.py", "from typing import overload\n\n\n@overload\ndef f(x: int) -> int: ...\n"
                                   "@overload\ndef f(x: str) -> str: ...\ndef f(x):\n    \"\"\"The real one.\"\"\"\n"
                                   "    return x\n")
    doc = libdocs.docstring(src, 4)
    assert doc["text"] == "The real one." and doc["start"] == 9


def test_a_bare_name_in_prose_is_not_a_mention_but_code_is(tmp_path):
    info = _write(tmp_path, "c-1.dist-info/METADATA",
                  "Name: c\n\nClick\n=====\n\nA Command Line Interface Creation Kit.\n\n"
                  "Commands\n--------\n\nUse ``c.Command`` to group options.\n")
    sec = libdocs.readme_section(info.parent, ["c.core.Command", "Command"])
    assert sec["heading"] == "Commands" and sec["start"] == 8
    prose = _write(tmp_path, "d-1.dist-info/METADATA", "Name: d\n\nA Command Line tool.\n")
    assert libdocs.readme_section(prose.parent, ["d.Command", "Command"]) is None
    code = _write(tmp_path, "e-1.dist-info/METADATA", "Name: e\n\nIntro::\n\n  cmd = Command()\n")
    assert libdocs.readme_section(code.parent, ["e.Command", "Command"])["start"] == 3
