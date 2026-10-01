"""Requirement criteria traced to the code and the tests that back them (``verinoda spec check``).

A spec is a Markdown file in the specs folder (``.verinoda/specs/`` by default; a folder you commit, such as
``docs/specs``, when the check should run in CI: ``[specs] dir`` in a committed ``verinoda.toml``,
``[tool.verinoda.specs] dir`` in ``pyproject.toml``; ``specs.dir`` in ``.verinoda/config.json`` and
``--specs-dir`` take precedence; a setting file that cannot be read, or a setting of the wrong type, is an error,
never the default). Every ``.md`` / ``.markdown`` file under it is read, except in dot folders, vendored
folders (``node_modules``, ``.venv``, ...) and links or junctions, which are not followed. A criterion is a line
that starts (after an optional list marker and task box) with an id in brackets, the id holding a digit; the
text is best written in the EARS style::

    # Checkout

    - [CART-1] WHEN the cart is empty THE SYSTEM SHALL disable checkout
      - evidence: tests/test_cart.py::test_empty_cart_disables_checkout, src/cart.py::Cart.checkout
    - [CART-2] THE SYSTEM SHALL keep the total in cents
      - evidence: clm_0123456789ab

Fenced code (at any indent), indented code, HTML comments and link reference definitions (``[v2]: https://...``)
are not read. An ``evidence:`` line (one or more) under a criterion lists what backs it, separated by commas or
spaces, a reference with spaces in backticks. Each reference is written by the author, never guessed:

* a claim id (``clm_...``, or ``claim:ID``): evidence when the claim's recorded status is verified (observed,
  experiment, static or primary source) and the file lines its evidence cites still hold the content recorded
  for them; a stale or contradicted claim, or one whose cited lines changed, is broken evidence (a newer claim
  superseding it is named), an inference is not evidence;
* a test id ``path::name`` (``path::Class::name``) in a test file, or ``test:ID``: evidence when a test is
  defined there. Python: a ``test*`` function, or a ``test*`` method of a ``Test*`` (or ``TestCase``) class,
  not a fixture; a ``Test*`` class stands for the tests it holds. Go: ``TestX`` and the like; Java, Kotlin,
  C# and Rust: a function with a test annotation or attribute. JavaScript / TypeScript: the title of an
  ``it(...)`` / ``test(...)`` call, found by a text pattern, so ``strong_inference`` (the criterion is then
  ``tested_inferred``). The parameters of a parametrized id are not checked. A helper, fixture or class
  without tests is a code pointer;
* ``path::Name`` in other code, ``path:12`` / ``path:12-30`` or a bare path: where the criterion is implemented.
  A ``pointer``, checked to resolve, never evidence that the behaviour holds.

Definitions are found in the file's own facts (:mod:`verinoda.anchors`: Python, and the languages with a
tree-sitter grammar); in a file without facts the name is searched as a word, and such a hit is
``strong_inference`` and says so. Paths are relative to the repository root, compared with the case they have
on disk.

Each criterion gets one status: ``broken`` (a reference does not resolve, or cites a stale or contradicted
claim, or one whose cited lines changed), ``verified`` (a verified claim), ``tested`` (a test that is defined),
``tested_inferred`` (a test found by a text pattern only), ``unchecked`` (a claim could not be read: no
Verinoda state, or a state this version cannot read) or ``unevidenced`` (nothing above: no reference, only code
pointers or claims that are inference). A criterion's sentence may wrap onto the lines below it; ``none``,
``tbd`` or ``todo`` on an evidence line is no reference. A SHALL line with no id, an evidence line with no
criterion above it, an empty evidence line and a second criterion with an id already used (case-insensitive,
across files) are problems.

The store is opened only when a claim is cited, read-only (``mode=ro``, no migration, nothing written).

The exit code is 1 while a criterion is broken or unevidenced or a spec line has a problem (status ``failed``
when something is broken or malformed, else ``unevidenced``); else 3 when something was not checked (a claim
that could not be read, a spec file that could not be read, a configured folder that does not exist); 2 on an
error; 0 otherwise (``ok``, ``no_criteria``, or ``no_specs`` when the default folder does not exist).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from verinoda import anchors
from verinoda.testcode import is_test_file, is_test_name, lang_of

DEFAULT_DIR = ".verinoda/specs"
DEFAULT_SOURCE = "the default (nothing configured)"
SPEC_EXT = (".md", ".markdown")
MAX_FILE_BYTES = 2_000_000
VERIFIED = ("observed", "experiment_verified", "statically_verified", "primary_source_verified")
STATUSES = ("broken", "verified", "tested", "tested_inferred", "unchecked", "unevidenced")
RECHECK_TYPES = ("source_code", "design_doc", "dependency_source", "reference_repo", "static_resolution")
LIMITS = [
    "a reference is what the author wrote: whether the claim or test really covers the criterion is not judged",
    "a claim's status is read as recorded; its cited file lines are re-read and compared with the recorded "
    "content, other evidence (an experiment, a document URL) is not re-run",
    "a test is checked to be defined, not run; the parameters of a parametrized test id are not checked",
    "a JavaScript / TypeScript test is found by its it()/test() title with a text pattern (tested_inferred)",
    "code pointers show where, not that the behaviour holds",
    "dot folders, vendored folders and links or junctions under the specs folder are not read",
]

_CRIT = re.compile(r"^[ \t]*(?:(?:[-*+]|\d{1,9}[.)])[ \t]+)?(?:\[[ xX]\][ \t]+)?(?:\*\*|__)?"
                   r"\[(?P<id>[A-Za-z][\w.-]{0,63})\](?:\*\*|__)?:?[ \t]+(?P<text>\S.*)$")
_HAS_DIGIT = re.compile(r"\d")
_LINKDEF = re.compile(r"^ {0,3}\[[^\]]{1,999}\]:[ \t]*(?:<[^>\n]*>|\S+)"
                      r"(?:[ \t]+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?[ \t]*$")
_EVID = re.compile(r"^[ \t]*(?:[-*+][ \t]+)?(?:\*\*|__)?evidence(?:\*\*|__)?[ \t]*:(?:\*\*|__)?(?P<refs>.*)$",
                   re.IGNORECASE)
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
_HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
_LIST = re.compile(r"^[ \t]*(?:[-*+]|\d{1,9}[.)])(?:[ \t]+|$)")
_SHALL = re.compile(r"\bSHALL\b")
_CLAIM_ID = re.compile(r"^clm_[0-9a-z]+$")
_DIGITS = re.compile(r"^\d{1,9}$")
_PREFIX = re.compile(r"^(?P<kind>claim|test|code):(?!/)(?P<rest>.+)$", re.IGNORECASE)
_TOKENS = re.compile(r"`([^`]+)`|([^\s,`]+)")
_FIXTURE_DECO = re.compile(r"^\s*@\s*(?:[\w.]+\.)?(?:fixture|yield_fixture)\b")
PLACEHOLDERS = {"none", "tbd", "todo", "-", "n/a"}   # written for "nothing yet": no reference
# vendored and generated folders, as the indexer skips them; dot folders are skipped as well
SKIP_DIRS = {"node_modules", "venv", "__pycache__", "graphify-out", "dist", "build", "site-packages"}
MALFORMED = ("no_id", "orphan_evidence", "empty_evidence", "duplicate_id")


class SpecError(ValueError):
    pass


def _problem(at: str, kind: str, message: str) -> dict:
    return {"at": at, "kind": kind, "message": message}


# -- where -------------------------------------------------------------------
def _committed_dir(repo: Path) -> tuple[str, str] | None:
    """The committed setting, None when none is set. A file that cannot be read, or a setting of the wrong
    type, raises :class:`SpecError`: the folder would otherwise silently fall back to the default."""
    from verinoda.decisions import _committed_tables

    for data, where, problem in _committed_tables(repo, "specs"):
        if problem:
            raise SpecError(f"{problem}; the specs folder setting cannot be known (fix the file or pass "
                            "--specs-dir)")
        if data is None:
            continue
        if not isinstance(data, dict):
            raise SpecError(f"{where} is not a table")
        value = data.get("dir")
        if value is None:
            continue
        if not isinstance(value, str):
            raise SpecError(f"{where} dir must be a string, not {type(value).__name__}")
        if value.strip():
            return value.strip(), f"{where} dir"
    return None


def _config_dir(repo: Path) -> str:
    """``specs.dir`` of the configuration ("" when not set); an unreadable ``.verinoda/config.json`` or a
    ``specs`` that is not an object raises :class:`SpecError`."""
    from verinoda.paths import atlas_dir, load_config

    p = atlas_dir(repo) / "config.json"
    if p.is_file():
        try:
            data = json.loads(p.read_bytes().decode("utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise SpecError(f".verinoda/config.json cannot be read ({type(exc).__name__}); the specs folder "
                            "setting cannot be known (fix the file or pass --specs-dir)") from None
        if not isinstance(data, dict):
            raise SpecError(".verinoda/config.json is not a JSON object")
    specs = load_config(repo).get("specs")
    if specs is None:
        return ""
    if not isinstance(specs, dict):
        raise SpecError(f"specs in .verinoda/config.json must be an object, not {type(specs).__name__}")
    value = specs.get("dir")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SpecError(f"specs.dir in .verinoda/config.json must be a string, not {type(value).__name__}")
    return value.strip()


def specs_dir_source(repo: Path, override: str | None = None) -> tuple[Path, str]:
    """``(folder, where it comes from)``: ``--specs-dir``, ``specs.dir`` in ``.verinoda/config.json``, the
    committed ``verinoda.toml`` / ``pyproject.toml``, else :data:`DEFAULT_DIR`. A folder outside the repository
    or a setting that cannot be read raises :class:`SpecError`."""
    repo = Path(repo).resolve()
    configured, where = str(override or "").strip(), "--specs-dir"
    if not configured:
        configured, where = _config_dir(repo), "specs.dir in .verinoda/config.json"
    if not configured:
        configured, where = _committed_dir(repo) or ("", DEFAULT_SOURCE)
    if configured.startswith("-"):
        raise SpecError(f"specs folder {configured!r} looks like an option")
    p = Path(configured or DEFAULT_DIR)
    p = (p if p.is_absolute() else repo / p).resolve()
    if p != repo and repo not in p.parents:
        raise SpecError(f"specs folder {configured!r} ({where}) is outside the repository {repo}")
    return p, where


# -- reading -----------------------------------------------------------------
def _read(p: Path) -> str:
    return p.read_bytes().decode("utf-8-sig", errors="replace")


def split_refs(text: str) -> list[str]:
    """The references of one evidence line: comma or space separated, one with spaces in backticks."""
    out = []
    for m in _TOKENS.finditer(text):
        tok = (m.group(1) if m.group(1) is not None else m.group(2)).strip().rstrip(".;")
        if tok and tok.lower() not in PLACEHOLDERS:
            out.append(tok)
    return out


def ears(text: str) -> str | None:
    """The EARS pattern of a criterion (``event``, ``state``, ``unwanted``, ``optional``, ``ubiquitous``), None
    without a SHALL."""
    if not re.search(r"\bshall\b", text, re.IGNORECASE):
        return None
    first = text.lstrip().split(None, 1)[0].strip("*_").upper() if text.strip() else ""
    return {"WHEN": "event", "WHILE": "state", "IF": "unwanted", "WHERE": "optional"}.get(first, "ubiquitous")


def _strip_comments(line: str, inside: bool) -> tuple[str | None, bool]:
    """``(the line without its HTML comments, still inside one)``; None when the whole line was comment."""
    if not inside and "<!--" not in line:
        return line, False
    out: list[str] = []
    i = 0
    while i <= len(line):
        if inside:
            j = line.find("-->", i)
            if j < 0:
                break
            i, inside = j + 3, False
        else:
            j = line.find("<!--", i)
            if j < 0:
                out.append(line[i:])
                break
            out.append(line[i:j])
            i, inside = j + 4, True
    rest = "".join(out)
    return (rest if rest.strip() else None), inside


def _indent(line: str) -> int:
    s = line.expandtabs(4)
    return len(s) - len(s.lstrip(" "))


def parse(text: str, rel: str) -> tuple[list[dict], list[dict]]:
    """``(criteria, problems)`` of one spec file. A criterion: ``{"id", "at": "rel:line", "text", "refs":
    [{"ref", "line"}], "ears"}``; a problem: ``{"at", "kind", "message"}``. Lines are split at LF only (a CR
    before it dropped), as editors count them. Fenced code at any indent, indented code (four columns past the
    enclosing list item, after a blank line), HTML comments and link reference definitions are skipped."""
    crits: list[dict] = []
    problems: list[dict] = []
    fence: str | None = None
    comment = False
    cur: dict | None = None
    cur_indent = -1
    wrapping = False
    list_col = 0          # the content column of the enclosing list item (0 outside a list)
    prev_blank = True
    in_code = False
    for n, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip("\r")
        if fence is not None:
            f = _FENCE.match(line)
            if f and f.group(1)[0] == fence[0] and len(f.group(1)) >= len(fence) \
                    and not line.strip().strip(fence[0]):
                fence = None
            continue
        if comment:                # inside an HTML comment: only what follows its end is read
            stripped, comment = _strip_comments(line, True)
            if stripped is None:
                continue
            line = stripped
        if not line.strip():
            prev_blank, wrapping = True, False
            continue
        ind = _indent(line)
        if ind >= list_col + 4 and (prev_blank or in_code):   # an indented code block
            in_code, wrapping = True, False
            continue
        in_code = False
        if prev_blank and ind < list_col and not _LIST.match(line):
            list_col = 0           # a paragraph, comment or fence after the list: the list has ended
        f = _FENCE.match(line)
        if f:
            fence, wrapping, prev_blank = f.group(1), False, False
            continue
        stripped, comment = _strip_comments(line, False)
        if stripped is None:
            continue
        line = stripped
        prev_blank = False
        if _LINKDEF.match(line):
            wrapping = False
            continue
        lm = _LIST.match(line)
        m = _CRIT.match(line)
        if m and _HAS_DIGIT.search(m.group("id")):
            text_ = m.group("text").rstrip()
            cur = {"id": m.group("id"), "at": f"{rel}:{n}", "text": text_, "refs": [], "ears": ears(text_)}
            crits.append(cur)
            cur_indent = ind
            if lm:
                list_col = _indent(lm.group(0))
            wrapping = True
            continue
        if wrapping and not lm and not _EVID.match(line) and not _HEADING.match(line):
            cur["text"] = f"{cur['text']} {line.strip()}"   # the criterion's sentence wrapped onto this line
            cur["ears"] = ears(cur["text"])
            continue
        wrapping = False
        e = _EVID.match(line)
        if e:
            refs = e.group("refs").strip()
            if cur is None:
                problems.append(_problem(f"{rel}:{n}", "orphan_evidence",
                                         "an evidence line with no criterion above it (not read)"))
            elif not refs:
                problems.append(_problem(f"{rel}:{n}", "empty_evidence", f"an empty evidence line ([{cur['id']}])"))
            else:
                cur["refs"].extend({"ref": r, "line": n} for r in split_refs(refs))
            continue
        if _HEADING.match(line):
            cur, list_col = None, 0
        else:
            if lm:
                list_col = _indent(lm.group(0))
            if cur is not None and ind <= cur_indent:
                cur = None         # another item or paragraph: what follows is not under the criterion
            if _SHALL.search(line) and not line.lstrip().startswith(">"):
                problems.append(_problem(f"{rel}:{n}", "no_id", "a SHALL sentence with no [ID]: not checked"))
    return crits, problems


# -- resolving -----------------------------------------------------------------
class _Files:
    """Paths of the repository as they are on disk, case included (a case-insensitive file system answers
    ``exists`` for ``SRC/A.PY`` too)."""

    def __init__(self, repo: Path):
        self.repo = repo
        self._ls: dict[Path, dict[str, str] | None] = {}
        self._text: dict[str, tuple[dict | None, str | None]] = {}

    def _names(self, d: Path) -> dict[str, str] | None:
        """A folder's entries by their NFC form (macOS may store a name decomposed)."""
        if d not in self._ls:
            try:
                self._ls[d] = {unicodedata.normalize("NFC", x): x for x in os.listdir(d)}
            except OSError:
                self._ls[d] = None
        return self._ls[d]

    def rel(self, written: str) -> tuple[str | None, str | None]:
        """``(relative path, problem)``."""
        p = written.strip().replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        if not p or p.startswith("/") or re.match(r"^[A-Za-z]:", p) or ".." in p.split("/"):
            return None, "not a path inside the repository"
        d = self.repo
        parts = [x for x in p.split("/") if x]
        for part in parts:
            names = self._names(d)
            if names is None or unicodedata.normalize("NFC", part) not in names:
                return None, "no such file"
            d = d / names[unicodedata.normalize("NFC", part)]
        if not d.is_file():
            return None, "not a file"
        try:
            inside = self.repo in d.resolve().parents
        except OSError:
            inside = False
        if not inside:
            return None, "a link that leads outside the repository"
        return d.relative_to(self.repo).as_posix(), None

    def facts(self, rel: str) -> tuple[dict | None, str | None]:
        if rel not in self._text:
            self._text[rel] = anchors.facts_for_path(self.repo / rel, rel)
        return self._text[rel]


def _lines_of(text: str) -> int:
    return text.count("\n") + (0 if text.endswith("\n") or not text else 1)


def _word(rel: str, text: str, qual: str) -> dict:
    last = re.escape(qual.rsplit(".", 1)[-1])
    for n, line in enumerate(text.split("\n"), 1):
        if re.search(rf"(?<![\w$]){last}(?![\w$])", line):
            return {"state": "ok", "at": f"{rel}:{n}", "status": "strong_inference",
                    "detail": "found as a word, not as a definition (no facts for this language)"}
    return {"state": "broken", "detail": f"{qual} not found in {rel}"}


def _symbol(files: _Files, rel: str, name: str) -> dict:
    """A code pointer ``path::Name``: where the definition is."""
    facts, text = files.facts(rel)
    qual = name.strip().split("(")[0].strip().replace("::", ".")
    if not qual:
        return {"state": "broken", "detail": "no name after ::"}
    if facts is not None and "symbols" in facts:
        hits = anchors.symbols_named(facts, qual)
        if hits:
            _q, s = hits[0]
            return {"state": "ok", "at": f"{rel}:{s['start']}-{s['end']}", "status": "pointer"}
        return {"state": "broken", "detail": f"no definition {qual} in {rel}"}
    if text is None:
        return {"state": "broken", "detail": "cannot be read"}
    return _word(rel, text, qual)


def _is_test_def(lang: str, q: str, s: dict, symbols: dict, lines: list[str]) -> tuple[bool | None, str]:
    """``(is a test, why not)``; None when this language's rule is not known here."""
    from verinoda import testcode

    if s.get("kind") != "def":
        return False, f"a {s.get('kind') or 'symbol'}, not a test function"
    base = q.split("#")[0]
    parent, _, last = base.rpartition(".")
    if lang == "python":
        if not is_test_name(last, "python") or not last.startswith("test"):
            return False, "not named test*: pytest does not collect it"
        deco = lines[max(0, s["start"] - 1): max(0, s.get("def", s["start"]) - 1)]
        if any(_FIXTURE_DECO.match(x) for x in deco):
            return False, "a pytest fixture, not a test"
        if parent:
            ps = symbols.get(parent)
            if not ps or ps.get("kind") != "class":
                return False, "a function nested in a function, not collected"
            head = lines[ps["start"] - 1: ps.get("def", ps["start"])]
            if not parent.rsplit(".", 1)[-1].startswith("Test") and not any("TestCase" in x for x in head):
                return False, f"a method of {parent}, which is neither a Test* class nor a TestCase"
        return True, ""
    if lang == "go":
        return (True, "") if is_test_name(last, "go") else (False, "not named TestX / BenchmarkX / ExampleX / FuzzX")
    rx = {"jvm": testcode._JVM_ANN, "csharp": testcode._CS_ATTR, "rust": testcode._RS_ATTR}.get(lang)
    if rx is None:
        return None, ""
    head = testcode._head(lines, s["start"], last)
    return (True, "") if rx.search(head) else (False, "no test annotation or attribute")


_JS_CALL = r"(?<![\w$.])(?P<fn>it|test|describe|xit|xtest|fit)(?:\.(?P<mod>only|skip|todo|concurrent|failing))?" \
           r"[ \t\r\n]*\([ \t\r\n]*(?P<q>[\"'`])"


def _js_test(rel: str, text: str, title: str) -> dict:
    """A JS/TS test named by its title: the first argument of an ``it(``/``test(`` call (a text pattern)."""
    title = title.strip()
    if not title:
        return {"state": "broken", "detail": "no test title after ::"}
    found = None
    for m in re.finditer(_JS_CALL + re.escape(title) + r"(?P=q)", text):
        found = found or m
        if m.group("fn") in ("it", "test", "fit") and m.group("mod") not in ("skip", "todo"):
            found = m
            break
    if found is None:
        return {"state": "broken", "detail": f"no it()/test() call titled {title!r} in {rel}"}
    line = text.count("\n", 0, found.start()) + 1
    if found.group("fn") in ("it", "test", "fit") and found.group("mod") not in ("skip", "todo"):
        return {"state": "ok", "at": f"{rel}:{line}", "status": "strong_inference",
                "detail": "an it()/test() call with this title, found by a text pattern, not parsed"}
    what = "a describe() suite" if found.group("fn") == "describe" else "a skipped or todo test"
    return {"state": "ok", "at": f"{rel}:{line}", "status": "pointer", "as_pointer": True,
            "detail": f"{what}, not a test that runs"}


def _test(files: _Files, rel: str, name: str) -> dict:
    """A test id ``path::name``: a test defined there, by the language's rule."""
    facts, text = files.facts(rel)
    if text is None:
        return {"state": "broken", "detail": "cannot be read"}
    lang = lang_of(rel)
    if lang == "js":
        return _js_test(rel, text, name)
    qual = name.strip().replace("::", ".")
    if not qual:
        return {"state": "broken", "detail": "no name after ::"}
    if facts is None or "symbols" not in facts:
        return _word(rel, text, qual)
    symbols = facts["symbols"]
    hits = [(q, s) for q, s in anchors.symbols_named(facts, qual) if q.split("#")[0] == qual]
    if not hits:
        return {"state": "broken", "detail": f"no definition {qual} in {rel}"}
    q, s = hits[0]
    lines = text.split("\n")
    at = f"{rel}:{s['start']}-{s['end']}"
    if s.get("kind") == "class":
        tests = [q2.split("#")[0] for q2, s2 in symbols.items()
                 if q2.split("#")[0].rpartition(".")[0] == qual
                 and _is_test_def(lang, q2, s2, symbols, lines)[0]]
        if tests:
            return {"state": "ok", "at": at, "status": "statically_verified",
                    "detail": f"a test class with {len(tests)} test(s)", "tests": tests[:20]}
        return {"state": "ok", "at": at, "status": "pointer", "as_pointer": True,
                "detail": "a class with no test in it, not a test"}
    ok, why = _is_test_def(lang, q, s, symbols, lines)
    if ok:
        return {"state": "ok", "at": at, "status": "statically_verified"}
    if ok is None:
        return {"state": "ok", "at": at, "status": "strong_inference",
                "detail": "defined in a test file; whether it is a test is not checked for this language"}
    return {"state": "ok", "at": at, "status": "pointer", "as_pointer": True, "detail": f"not a test: {why}"}


class ClaimReader:
    """The claims of ``.verinoda/atlas.db``, opened on first use, read-only (``mode=ro``): no migration, no
    journal setting, nothing created. ``problem`` says why claims cannot be read."""

    def __init__(self, repo: Path):
        self.repo = repo
        self.conn: sqlite3.Connection | None = None
        self.problem: str | None = None
        self.opened = False
        self.index_at: str | None = None

    def open(self) -> bool:
        if not self.opened:
            self.opened = True
            from verinoda.paths import db_path
            from verinoda.store import SCHEMA_VERSION

            db = db_path(self.repo)
            if not db.is_file():
                self.problem = "no Verinoda state to read claims from (verinoda scan)"
                return False
            conn = None
            try:
                conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
                version = int(row[0]) if row else 0
                if version != SCHEMA_VERSION:
                    self.problem = (f".verinoda/atlas.db has schema v{version}, this Verinoda reads v{SCHEMA_VERSION}"
                                    " (a `verinoda update` by the matching version brings it level)")
                    conn.close()
                    return False
                self.index_at = conn.execute("SELECT max(created_at) FROM snapshots").fetchone()[0]
                self.conn = conn
            except (sqlite3.Error, ValueError) as exc:
                self.problem = f".verinoda/atlas.db cannot be read ({type(exc).__name__}: {exc})"[:300]
                if conn is not None:
                    conn.close()
                return False
        return self.conn is not None

    def one(self, sql: str, args: tuple) -> dict | None:
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, args: tuple) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def claim(self, cid: str) -> dict | None:
        return self.one("SELECT * FROM claims WHERE id = ?", (cid,))

    def successors(self, c: dict) -> list[dict]:
        """The claims that supersede ``c``, one after the other, to the live one."""
        chain: list[dict] = []
        seen = {c["id"]}
        cur = c
        while len(chain) < 20:
            nxt = cur.get("superseded_by")
            if not nxt:
                row = self.one("SELECT id FROM claims WHERE supersedes = ? ORDER BY created_at DESC LIMIT 1",
                               (cur["id"],))
                nxt = row["id"] if row else None
            if not nxt or nxt in seen:
                break
            row = self.claim(nxt)
            if row is None:
                break
            seen.add(nxt)
            chain.append({"id": row["id"], "status": row["status"]})
            cur = row
        return chain

    def evidence(self, cid: str) -> list[dict]:
        rows = self.all("SELECT e.* FROM evidence e JOIN claim_evidence ce ON ce.evidence_id = e.id "
                        "WHERE ce.claim_id = ? AND ce.relation = 'supports' ORDER BY e.id", (cid,))
        for r in rows:
            try:
                r["meta"] = json.loads(r.get("meta") or "{}")
            except ValueError:
                r["meta"] = {}
            if not isinstance(r["meta"], dict):
                r["meta"] = {}
        return rows

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None


def _claim(repo: Path, out: dict, tok: str, claims: ClaimReader | None) -> dict:
    out["kind"] = "claim"
    if claims is None or not claims.open():
        why = claims.problem if claims is not None else "claims not read"
        return {**out, "state": "unchecked", "detail": why}
    try:
        c = claims.claim(tok)
        if c is None:
            return {**out, "state": "broken", "detail": "no such claim"}
        out["status"] = c["status"]
        if c["status"] in ("stale", "contradicted"):
            chain = claims.successors(c)
            det = f"the claim is {c['status']}"
            if chain:
                out["superseded_by"] = chain
                det += "; superseded by " + " -> ".join(f"{x['id']} ({x['status']})" for x in chain) + \
                       f": cite {chain[-1]['id']}"
            return {**out, "state": "stale", "detail": det}
        if c["status"] not in VERIFIED:
            return {**out, "state": "unverified", "detail": f"the claim is {c['status']}, not verified"}
        evs = claims.evidence(tok)
    except sqlite3.Error as exc:
        return {**out, "state": "unchecked", "detail": f"claims cannot be read ({type(exc).__name__}: {exc})"[:300]}
    from verinoda import evidence as evmod

    seen, changed, reread = [], [], 0
    for ev in evs:
        item = {"at": ev.get("locator"), "type": ev.get("source_type")}
        if ev.get("source_type") in RECHECK_TYPES and ev.get("path") and ev.get("line_start") \
                and ev.get("content_hash"):
            sc = evmod.check_source(repo, ev)
            item["check"] = sc.status if sc.ok else f"{sc.status}: {sc.reason}"
            reread += 1
            if not sc.ok:
                changed.append(f"{ev.get('locator')} ({sc.reason})")
        else:
            item["check"] = "not re-read"
        seen.append(item)
    out["claim_evidence"] = seen
    if changed:
        return {**out, "state": "stale", "detail": "the cited lines changed since the claim was recorded: "
                + "; ".join(changed[:3]) + " (run `verinoda update`)"}
    if not reread:
        return {**out, "state": "ok", "detail": "no file lines to re-read: the status is as recorded"}
    return {**out, "state": "ok", "detail": f"{reread} cited file range(s) re-read: unchanged"}


def resolve_ref(files: _Files, written: str, claims: ClaimReader | None) -> dict:
    """One reference: ``{"ref", "kind": claim|test|symbol|lines|file, "state": ok|unverified|stale|broken|
    unchecked, "at"?, "status"?, "detail"?}``. A code pointer's status is ``pointer``."""
    out: dict = {"ref": written}
    tok = written
    forced = None
    m = _PREFIX.match(tok)
    if m:
        forced, tok = m.group("kind").lower(), m.group("rest").strip()
    if forced == "claim" or (forced is None and _CLAIM_ID.match(tok)):
        return _claim(files.repo, out, tok, claims)
    if "::" in tok:
        path, _, name = tok.partition("::")
        rel, problem = files.rel(path)
        is_test = forced == "test" or (forced is None and is_test_file(path))
        out["kind"] = "test" if is_test else "symbol"
        if rel is None:
            return {**out, "state": "broken", "detail": problem}
        if not is_test:
            return {**out, **_symbol(files, rel, name)}
        if lang_of(rel) != "js" and "[" in name:
            name, _, params = name.partition("[")
            out["params"] = "[" + params
        res = _test(files, rel, name)
        if res.pop("as_pointer", False):
            out["kind"] = "symbol"
        if "params" in out and res.get("state") == "ok":
            res["detail"] = "; ".join(x for x in (res.get("detail"), "the parameters are not checked") if x)
        return {**out, **res}
    if forced == "test":
        return {**out, "kind": "test", "state": "broken", "detail": "a test id is path::name"}
    path, sep, rng = tok.rpartition(":")
    a, _, b = rng.partition("-")
    if sep and path and _DIGITS.match(a) and (not b or _DIGITS.match(b)):
        out["kind"] = "lines"
        rel, problem = files.rel(path)
        if rel is None:
            return {**out, "state": "broken", "detail": problem}
        lo, hi = int(a), int(b or a)
        _facts, text = files.facts(rel)
        n = _lines_of(text or "")
        if lo < 1 or hi < lo:
            return {**out, "state": "broken", "detail": f"not a line range: {lo}-{hi}"}
        if hi > n:
            return {**out, "state": "broken", "detail": f"lines {lo}-{hi} past the end ({n} lines)"}
        return {**out, "state": "ok", "at": f"{rel}:{lo}" + (f"-{hi}" if hi != lo else ""), "status": "pointer"}
    if forced == "code" or "/" in tok or "." in tok:
        out["kind"] = "file"
        rel, problem = files.rel(tok)
        if rel is None:
            return {**out, "state": "broken", "detail": problem}
        return {**out, "state": "ok", "at": rel, "status": "pointer"}
    return {**out, "kind": "unknown", "state": "broken",
            "detail": "not a claim id, test id, path::Name, path:line or path"}


def criterion_status(refs: list[dict]) -> str:
    if any(r["state"] in ("broken", "stale") for r in refs):
        return "broken"
    ok = [r for r in refs if r["state"] == "ok"]
    if any(r["kind"] == "claim" for r in ok):
        return "verified"
    tests = [r for r in ok if r["kind"] == "test"]
    if any(r.get("status") == "statically_verified" for r in tests):
        return "tested"
    if tests:
        return "tested_inferred"
    if any(r["state"] == "unchecked" for r in refs):
        return "unchecked"
    return "unevidenced"


# -- the check -----------------------------------------------------------------
def _is_link(p: Path) -> bool:
    """A symbolic link or a Windows junction (another reparse point, such as a cloud file, is not one)."""
    try:
        st = os.lstat(p)
    except OSError:
        return True
    if stat.S_ISLNK(st.st_mode):
        return True
    tag = getattr(st, "st_reparse_tag", 0)
    return bool(tag) and tag in (getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
                                 getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003))


def spec_files(folder: Path, repo: Path | None = None) -> tuple[list[Path], list[str]]:
    """``(spec files, links skipped)``: the ``.md`` / ``.markdown`` files under ``folder``, walked without
    following links or junctions, dot folders and vendored folders left out, each file inside ``repo``."""
    repo = (repo or folder).resolve()
    out: list[Path] = []
    skipped: list[str] = []

    def rel(p: Path) -> str:
        try:
            return p.relative_to(repo).as_posix()
        except ValueError:
            return p.as_posix()

    for root, dirs, names in os.walk(folder, followlinks=False):
        r = Path(root)
        keep = []
        for d in sorted(dirs):
            if d.startswith(".") or d in SKIP_DIRS:
                continue
            if _is_link(r / d):
                skipped.append(rel(r / d))
                continue
            keep.append(d)
        dirs[:] = keep
        for nm in names:
            p = r / nm
            if p.suffix.lower() not in SPEC_EXT:
                continue
            try:
                inside = repo in p.resolve().parents
            except OSError:
                inside = False
            if not inside or (_is_link(p) and not p.is_file()):
                skipped.append(rel(p))
                continue
            out.append(p)
    return sorted(out, key=lambda p: p.as_posix()), skipped


def _age(stamp: str | None) -> str | None:
    if not stamp:
        return None
    try:
        t = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    hours = (datetime.now(timezone.utc) - t).total_seconds() / 3600
    return f"{hours:.0f} h" if hours < 48 else f"{hours / 24:.0f} days"


def check(repo: Path, specs_dir: str | None = None, *, use_store: bool = True) -> dict:
    """Every criterion of every spec file with its references resolved and its status. Raises
    :class:`SpecError` for a folder outside the repository or a folder setting that cannot be read."""
    repo = Path(repo).resolve()
    folder, where = specs_dir_source(repo, specs_dir)
    rel_dir = folder.relative_to(repo).as_posix() if folder != repo else "."
    res: dict = {"dir": rel_dir, "dir_source": where, "files": 0, "criteria": [], "problems": [],
                 "counts": {s: 0 for s in STATUSES}, "claims": {"read": False}, "limits": LIMITS}
    if not folder.is_dir():
        configured = where != DEFAULT_SOURCE
        res["problems"].append(_problem(rel_dir, "no_folder", f"no specs folder {rel_dir} ({where})"))
        res.update(status="unchecked" if configured else "no_specs", exit=3 if configured else 0)
        return res
    files, skipped = spec_files(folder, repo)
    res["files"] = len(files)
    if skipped:
        res["skipped_links"] = skipped[:50]
    fs = _Files(repo)
    claims = ClaimReader(repo) if use_store else None
    seen: dict[str, str] = {}
    malformed = unread = 0
    try:
        for p in files:
            rel = p.relative_to(repo).as_posix()
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    res["problems"].append(_problem(rel, "too_large", f"larger than {MAX_FILE_BYTES} bytes, not read"))
                    unread += 1
                    continue
                text = _read(p)
            except OSError as exc:
                res["problems"].append(_problem(rel, "unreadable", f"cannot be read ({type(exc).__name__})"))
                unread += 1
                continue
            crits, problems = parse(text, rel)
            res["problems"].extend(problems)
            malformed += len(problems)
            for c in crits:
                key = c["id"].casefold()
                if key in seen:
                    c["duplicate_of"] = seen[key]
                    res["problems"].append(_problem(c["at"], "duplicate_id",
                                                    f"id [{c['id']}] already used at {seen[key]}"))
                else:
                    seen[key] = c["at"]
                written: dict[str, int] = {}
                for r in c.pop("refs"):
                    written.setdefault(r["ref"], r["line"])
                refs = [{**resolve_ref(fs, r, claims), "line": f"{rel}:{n}"} for r, n in written.items()]
                c["status"] = criterion_status(refs)
                c["evidence"] = refs
                res["counts"][c["status"]] += 1
                res["criteria"].append(c)
    finally:
        if claims is not None:
            res["claims"] = {"read": claims.conn is not None, "problem": claims.problem,
                             "index_at": claims.index_at, "index_age": _age(claims.index_at)} \
                if claims.opened else {"read": False, "problem": None, "detail": "no claim cited"}
            claims.close()
    cnt = res["counts"]
    dup = any("duplicate_of" in c for c in res["criteria"])
    if cnt["broken"] or cnt["unevidenced"] or dup or malformed:
        res.update(status="failed" if cnt["broken"] or dup or malformed else "unevidenced", exit=1)
    elif cnt["unchecked"] or unread:
        res.update(status="unchecked", exit=3)
    else:
        res.update(status="ok" if res["criteria"] else "no_criteria", exit=0)
    return res


def render(res: dict) -> str:
    cnt = res["counts"]
    head = (f"spec check: {len(res['criteria'])} criteria in {res['files']} file(s) of {res['dir']} "
            f"[{res['dir_source']}]: " + ", ".join(f"{cnt[s]} {s}" for s in STATUSES))
    out = [head]
    cl = res.get("claims") or {}
    if cl.get("read"):
        out.append(f"claims: read from .verinoda/atlas.db (read-only); last index {cl.get('index_at') or 'none'}"
                   + (f", {cl['index_age']} ago" if cl.get("index_age") else ""))
    elif cl.get("problem"):
        out.append(f"claims: not read: {cl['problem']}")
    for c in res["criteria"]:
        out.append(f"{c['status'].upper():<15} {c['at']} [{c['id']}] {c['text'][:160]}")
        for r in c["evidence"]:
            bits = ["" if r.get("at") == r["ref"] else r.get("at") or "", r.get("status") or "", r.get("detail") or ""]
            out.append(f"    {r['state']:<10} {r['kind']:<6} {r['ref']}" + "".join(f" - {b}" for b in bits if b))
            for ev in r.get("claim_evidence") or []:
                out.append(f"        evidence {ev['at']} ({ev['type']}): {ev['check']}")
        if not c["evidence"]:
            out.append("    (no evidence line)")
    for p in res["problems"]:
        out.append(f"problem: {p['at']}: {p['message']}")
    for s in res.get("skipped_links") or []:
        out.append(f"skipped (a link or junction, not followed): {s}")
    out.append("limits:")
    out.extend(f"  - {x}" for x in res.get("limits") or [])
    out.append(f"status {res['status']}, exit {res['exit']}")
    return "\n".join(out)
