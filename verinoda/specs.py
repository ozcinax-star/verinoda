"""Requirement criteria traced to the code and the tests that back them (``verinoda spec check``).

A spec is a Markdown file in the specs folder (``.verinoda/specs/`` by default; a folder you commit, such as
``docs/specs``, when the check should run in CI: ``[specs] dir`` in a committed ``verinoda.toml``,
``[tool.verinoda.specs] dir`` in ``pyproject.toml``; ``specs.dir`` in ``.verinoda/config.json`` and
``--specs-dir`` take precedence). Every ``.md`` / ``.markdown`` file under it is read. A criterion is a line
that starts (after an optional list marker) with an id in brackets, the id holding a digit; the text is best
written in the EARS style::

    # Checkout

    - [CART-1] WHEN the cart is empty THE SYSTEM SHALL disable checkout
      - evidence: tests/test_cart.py::test_empty_cart_disables_checkout, src/cart.py::Cart.checkout
    - [CART-2] THE SYSTEM SHALL keep the total in cents
      - evidence: clm_0123456789ab

An ``evidence:`` line (one or more) under a criterion lists what backs it, separated by commas or spaces, a
reference with spaces in backticks. Each reference is written by the author, never guessed:

* a claim id (``clm_...``, or ``claim:ID``): evidence when the claim's recorded status is verified (observed,
  experiment, static or primary source); a stale or contradicted claim is broken evidence, an inference is not
  evidence;
* a test id ``path::name`` (``path::Class::name``; parameters ignored) in a test file, or ``test:ID``: evidence
  when that test is defined there;
* ``path::Name`` in other code, ``path:12`` / ``path:12-30`` or a bare path: where the criterion is implemented.
  A pointer, checked to resolve, never evidence that the behaviour holds.

Definitions are found in the file's own facts (:mod:`verinoda.anchors`: Python, and the languages with a
tree-sitter grammar); in a file without facts the name is searched as a word, and such a hit is
``strong_inference`` and says so. Paths are relative to the repository root, compared with the case they have
on disk.

Each criterion gets one status: ``broken`` (a reference does not resolve, or cites a stale or contradicted
claim), ``verified`` (a verified claim), ``tested`` (a test that exists), ``unchecked`` (a claim could not be read:
no Verinoda state) or ``unevidenced`` (nothing above: no reference, only code pointers or claims that are
inference). The exit code is 1 while a criterion is broken or unevidenced, or two criteria share an id; else 3
when something was not checked (a claim with no state to read, a configured folder that does not exist);
2 on an error; 0 otherwise. A claim's status is read as recorded: ``verinoda update`` marks claims stale.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from verinoda import anchors
from verinoda.testcode import is_test_file

DEFAULT_DIR = ".verinoda/specs"
DEFAULT_SOURCE = "the default (nothing configured)"
SPEC_EXT = (".md", ".markdown")
MAX_FILE_BYTES = 2_000_000
VERIFIED = ("observed", "experiment_verified", "statically_verified", "primary_source_verified")
STATUSES = ("broken", "verified", "tested", "unchecked", "unevidenced")
LIMITS = [
    "a reference is what the author wrote: whether the claim or test really covers the criterion is not judged",
    "claim statuses are read as recorded; run `verinoda update` first so changed code marks them stale",
    "a test is checked to be defined, not run; code pointers show where, not that the behaviour holds",
]

_CRIT = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?(?:\*\*|__)?\[(?P<id>[A-Za-z][\w.-]*?\d[\w.-]*)\](?:\*\*|__)?"
                   r":?[ \t]+(?P<text>\S.*?)\s*$")
_EVID = re.compile(r"^\s*(?:[-*+]\s+)?(?:\*\*|__)?evidence(?:\*\*|__)?\s*:(?:\*\*|__)?\s*(?P<refs>.*?)\s*$",
                   re.IGNORECASE)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"^ {0,3}#{1,6}(\s|$)")
_SHALL = re.compile(r"\bSHALL\b")
_CLAIM_ID = re.compile(r"^clm_[0-9a-z]+$")
_LINES = re.compile(r"^(?P<path>.+?):(?P<a>\d+)(?:-(?P<b>\d+))?$")
_PREFIX = re.compile(r"^(?P<kind>claim|test|code):(?!/)(?P<rest>.+)$", re.IGNORECASE)
_TOKENS = re.compile(r"`([^`]+)`|([^\s,`]+)")


class SpecError(ValueError):
    pass


# -- where -------------------------------------------------------------------
def _committed_dir(repo: Path) -> tuple[str, str] | None:
    from verinoda.decisions import _committed_tables

    for data, where, _problem in _committed_tables(repo, "specs"):
        value = data.get("dir") if isinstance(data, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip(), f"{where} dir"
    return None


def specs_dir_source(repo: Path, override: str | None = None) -> tuple[Path, str]:
    """``(folder, where it comes from)``: ``--specs-dir``, ``specs.dir`` in ``.verinoda/config.json``, the
    committed ``verinoda.toml`` / ``pyproject.toml``, else :data:`DEFAULT_DIR`. A folder outside the repository
    is refused."""
    from verinoda.paths import load_config

    repo = Path(repo).resolve()
    configured, where = str(override or "").strip(), "--specs-dir"
    if not configured:
        try:
            configured = str((load_config(repo).get("specs") or {}).get("dir") or "").strip()
        except Exception:  # noqa: BLE001 - an unreadable config: the committed setting or the default folder
            configured = ""
        where = "specs.dir in .verinoda/config.json"
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
        if tok:
            out.append(tok)
    return out


def ears(text: str) -> str | None:
    """The EARS pattern of a criterion (``event``, ``state``, ``unwanted``, ``optional``, ``ubiquitous``), None
    without a SHALL."""
    if not re.search(r"\bshall\b", text, re.IGNORECASE):
        return None
    first = text.lstrip().split(None, 1)[0].upper() if text.strip() else ""
    return {"WHEN": "event", "WHILE": "state", "IF": "unwanted", "WHERE": "optional"}.get(first, "ubiquitous")


def parse(text: str, rel: str) -> tuple[list[dict], list[str]]:
    """``(criteria, problems)`` of one spec file. A criterion: ``{"id", "at": "rel:line", "text", "refs":
    [written reference], "ears"}``."""
    crits: list[dict] = []
    problems: list[str] = []
    fence = None
    cur = None
    for n, line in enumerate(text.splitlines(), 1):
        f = _FENCE.match(line)
        if f:
            if fence is None:
                fence = f.group(1)[0]
            elif f.group(1)[0] == fence:
                fence = None
            continue
        if fence is not None:
            continue
        m = _CRIT.match(line)
        if m:
            cur = {"id": m.group("id"), "at": f"{rel}:{n}", "text": m.group("text"), "refs": [],
                   "ears": ears(m.group("text"))}
            crits.append(cur)
            continue
        e = _EVID.match(line)
        if e:
            if cur is None:
                problems.append(f"{rel}:{n}: an evidence line with no criterion above it (not read)")
            elif not e.group("refs"):
                problems.append(f"{rel}:{n}: an empty evidence line ([{cur['id']}])")
            else:
                cur["refs"].extend(split_refs(e.group("refs")))
            continue
        if _HEADING.match(line):
            cur = None
        elif _SHALL.search(line) and not line.lstrip().startswith(">"):
            problems.append(f"{rel}:{n}: a SHALL sentence with no [ID]: not checked")
    return crits, problems


# -- resolving -----------------------------------------------------------------
class _Files:
    """Paths of the repository as they are on disk, case included (a case-insensitive file system answers
    ``exists`` for ``SRC/A.PY`` too)."""

    def __init__(self, repo: Path):
        self.repo = repo
        self._ls: dict[Path, set[str] | None] = {}
        self._text: dict[str, tuple[dict | None, str | None]] = {}

    def _names(self, d: Path) -> set[str] | None:
        if d not in self._ls:
            try:
                self._ls[d] = set(os.listdir(d))
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
            if names is None or part not in names:
                return None, "no such file"
            d = d / part
        if not d.is_file():
            return None, "not a file"
        return "/".join(parts), None

    def facts(self, rel: str) -> tuple[dict | None, str | None]:
        if rel not in self._text:
            self._text[rel] = anchors.facts_for_path(self.repo / rel, rel)
        return self._text[rel]


def _lines_of(text: str) -> int:
    return text.count("\n") + (0 if text.endswith("\n") or not text else 1)


def _symbol(files: _Files, rel: str, name: str, *, exact: bool) -> dict:
    facts, text = files.facts(rel)
    qual = name.strip().split("(")[0].strip().replace("::", ".")
    if not qual:
        return {"state": "broken", "detail": "no name after ::"}
    if facts is not None and "symbols" in facts:
        hits = [(q, s) for q, s in anchors.symbols_named(facts, qual) if not exact or q.split("#")[0] == qual]
        if hits:
            q, s = hits[0]
            return {"state": "ok", "at": f"{rel}:{s['start']}-{s['end']}", "status": "statically_verified"}
        if exact and text is not None:   # a test named by its title: it("adds an item", ...), test('...')
            title = name.strip()
            for n, line in enumerate(text.splitlines(), 1):
                if any(f"{q}{title}{q}" in line for q in "\"'`"):
                    return {"state": "ok", "at": f"{rel}:{n}", "status": "strong_inference",
                            "detail": "found as a quoted test title, not as a definition"}
        return {"state": "broken", "detail": f"no definition {qual} in {rel}"}
    if text is None:
        return {"state": "broken", "detail": "cannot be read"}
    last = re.escape(qual.rsplit(".", 1)[-1])
    for n, line in enumerate(text.splitlines(), 1):
        if re.search(rf"(?<![\w$]){last}(?![\w$])", line):
            return {"state": "ok", "at": f"{rel}:{n}", "status": "strong_inference",
                    "detail": "found as a word, not as a definition (no facts for this language)"}
    return {"state": "broken", "detail": f"{qual} not found in {rel}"}


def resolve_ref(files: _Files, written: str, store) -> dict:
    """One reference: ``{"ref", "kind": claim|test|symbol|lines|file, "state": ok|unverified|stale|broken|
    unchecked, "at"?, "status"?, "detail"?}``."""
    out: dict = {"ref": written}
    tok = written
    forced = None
    m = _PREFIX.match(tok)
    if m:
        forced, tok = m.group("kind").lower(), m.group("rest").strip()
    if forced == "claim" or (forced is None and _CLAIM_ID.match(tok)):
        out["kind"] = "claim"
        if store is None:
            return {**out, "state": "unchecked", "detail": "no Verinoda state to read claims from (verinoda scan)"}
        c = store.claim(tok)
        if c is None:
            return {**out, "state": "broken", "detail": "no such claim"}
        out["status"] = c["status"]
        if c["status"] in VERIFIED:
            return {**out, "state": "ok"}
        if c["status"] in ("stale", "contradicted"):
            newer = store.one("SELECT id, status FROM claims WHERE supersedes = ? ORDER BY created_at DESC LIMIT 1",
                              (tok,))
            det = f"the claim is {c['status']}" + (f"; superseded by {newer['id']} ({newer['status']})"
                                                   if newer else "")
            return {**out, "state": "stale", "detail": det}
        return {**out, "state": "unverified", "detail": f"the claim is {c['status']}, not verified"}
    if "::" in tok:
        path, _, name = tok.partition("::")
        rel, problem = files.rel(path)
        is_test = forced == "test" or (forced is None and is_test_file(path))
        out["kind"] = "test" if is_test else "symbol"
        if rel is None:
            return {**out, "state": "broken", "detail": problem}
        if is_test:
            name = name.split("[", 1)[0]
        return {**out, **_symbol(files, rel, name, exact=is_test)}
    if forced == "test":
        return {**out, "kind": "test", "state": "broken", "detail": "a test id is path::name"}
    m = _LINES.match(tok)
    if m:
        out["kind"] = "lines"
        rel, problem = files.rel(m.group("path"))
        if rel is None:
            return {**out, "state": "broken", "detail": problem}
        a = int(m.group("a"))
        b = int(m.group("b") or a)
        _facts, text = files.facts(rel)
        n = _lines_of(text or "")
        if a < 1 or b < a:
            return {**out, "state": "broken", "detail": f"not a line range: {a}-{b}"}
        if b > n:
            return {**out, "state": "broken", "detail": f"lines {a}-{b} past the end ({n} lines)"}
        return {**out, "state": "ok", "at": f"{rel}:{a}" + (f"-{b}" if b != a else ""),
                "status": "statically_verified"}
    if forced == "code" or "/" in tok or "." in tok:
        out["kind"] = "file"
        rel, problem = files.rel(tok)
        if rel is None:
            return {**out, "state": "broken", "detail": problem}
        return {**out, "state": "ok", "at": rel, "status": "statically_verified"}
    return {**out, "kind": "unknown", "state": "broken",
            "detail": "not a claim id, test id, path::Name, path:line or path"}


def criterion_status(refs: list[dict]) -> str:
    states = {(r["kind"], r["state"]) for r in refs}
    if any(s in ("broken", "stale") for _k, s in states):
        return "broken"
    if ("claim", "ok") in states:
        return "verified"
    if ("test", "ok") in states:
        return "tested"
    if any(s == "unchecked" for _k, s in states):
        return "unchecked"
    return "unevidenced"


# -- the check -----------------------------------------------------------------
def spec_files(folder: Path) -> list[Path]:
    return sorted((p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in SPEC_EXT),
                  key=lambda p: p.as_posix())


def _open_store(repo: Path):
    from verinoda.store import NotInitialised, open_store

    try:
        return open_store(repo, create=False)
    except NotInitialised:
        return None


def check(repo: Path, specs_dir: str | None = None, *, store=None, use_store: bool = True) -> dict:
    """Every criterion of every spec file with its references resolved and its status. Raises
    :class:`SpecError` for a folder outside the repository."""
    repo = Path(repo).resolve()
    folder, where = specs_dir_source(repo, specs_dir)
    rel_dir = folder.relative_to(repo).as_posix() if folder != repo else "."
    res: dict = {"dir": rel_dir, "dir_source": where, "files": 0, "criteria": [], "problems": [],
                 "counts": {s: 0 for s in STATUSES}, "limits": LIMITS}
    if not folder.is_dir():
        configured = where != DEFAULT_SOURCE
        res["problems"].append(f"no specs folder {rel_dir} ({where})")
        res.update(status="unchecked" if configured else "no_specs", exit=3 if configured else 0)
        return res
    files = spec_files(folder)
    res["files"] = len(files)
    fs = _Files(repo)
    st = store if store is not None else (_open_store(repo) if use_store else None)
    seen: dict[str, str] = {}
    try:
        for p in files:
            rel = p.relative_to(repo).as_posix()
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    res["problems"].append(f"{rel}: larger than {MAX_FILE_BYTES} bytes, not read")
                    continue
                text = _read(p)
            except OSError as exc:
                res["problems"].append(f"{rel}: cannot be read ({type(exc).__name__})")
                continue
            crits, problems = parse(text, rel)
            res["problems"].extend(problems)
            for c in crits:
                key = c["id"].casefold()
                if key in seen:
                    c["duplicate_of"] = seen[key]
                    res["problems"].append(f"{c['at']}: id [{c['id']}] already used at {seen[key]}")
                else:
                    seen[key] = c["at"]
                refs = [resolve_ref(fs, r, st) for r in dict.fromkeys(c.pop("refs"))]
                c["status"] = criterion_status(refs)
                c["evidence"] = refs
                res["counts"][c["status"]] += 1
                res["criteria"].append(c)
    finally:
        if st is not None and store is None:
            close = getattr(st, "close", None)
            if close:
                close()
    cnt = res["counts"]
    dup = any("duplicate_of" in c for c in res["criteria"])
    if cnt["broken"] or cnt["unevidenced"] or dup:
        res.update(status="unevidenced", exit=1)
    elif cnt["unchecked"]:
        res.update(status="unchecked", exit=3)
    else:
        res.update(status="ok" if res["criteria"] else "no_criteria", exit=0)
    return res


def render(res: dict) -> str:
    cnt = res["counts"]
    head = (f"spec check: {len(res['criteria'])} criteria in {res['files']} file(s) of {res['dir']} "
            f"[{res['dir_source']}]: " + ", ".join(f"{cnt[s]} {s}" for s in STATUSES))
    out = [head]
    for c in res["criteria"]:
        out.append(f"{c['status'].upper():<12} {c['at']} [{c['id']}] {c['text'][:160]}")
        for r in c["evidence"]:
            bits = ["" if r.get("at") == r["ref"] else r.get("at") or "", r.get("status") or "", r.get("detail") or ""]
            out.append(f"    {r['state']:<10} {r['kind']:<6} {r['ref']}" +
                       "".join(f" - {b}" for b in bits if b))
        if not c["evidence"]:
            out.append("    (no evidence line)")
    for p in res["problems"]:
        out.append(f"problem: {p}")
    out.append(f"exit {res['exit']}")
    return "\n".join(out)
