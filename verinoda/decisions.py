"""Decision records (docs/DESIGN.md D33): what the human chose, kept beside the code and checked against it.

A record is a Markdown file in ``decisions.dir`` (``.verinoda/decisions/`` by default; set it to a folder
you commit, such as ``docs/decisions``, when ``verinoda decide check`` should run in CI - ``.verinoda/`` is
not committed) with a front matter::

    ---
    verinoda-decision: 1
    id: ADR-0002
    title: Keep SQLite behind OrderRepository
    status: accepted
    decided-by: human
    date: 2026-09-25
    chosen: SQLite
    brief: dbr_0123456789ab
    source: null
    supersedes: null
    superseded-by: null
    governs: [{"id": "v1", "symbol": "orders/repository.py::OrderRepository.__init__", ...}]
    guards: [{"id": "g1", "kind": "only_in", "calls": ["sqlite3.connect"], "allowed": [...], ...}]
    revisit-when: [{"id": "r1", "kind": "dependency_added", "value": "psycopg"}]
    waivers: []
    ---
    # ADR-0002: Keep SQLite behind OrderRepository
    ...

A value after ``key:`` is JSON when it parses as JSON (``null``, lists, objects), else plain text. The
front matter is what ``decide check`` enforces; ``decided-by`` is always ``human`` (a record that says
otherwise is reported and not enforced). Nothing here chooses: a record exists only because a human ran
``decide record`` (or an agent did with the user's words in ``user_statement``).

Every change is also appended to the ``decisions`` table (schema v5) as the whole state after the event
(record, import, guard, accept, waive, supersede); the table is the audit log, the file is what is
checked. A file edited by hand since its last logged event is reported as such, never overwritten
silently: the next event keeps the hand-edited body and rewrites only the front matter and the block
between the ``verinoda:generated`` markers.

Guard specs (``--guard``) are ``KIND key=value ...``; list values are comma-separated; a value with spaces
is quoted; a backslash is kept as written (``allowed=orders\\repository.py``, ``pattern=\\bexecute\\b``):

* ``only_in calls=sqlite3.connect[,...] allowed=PATH|GLOB[,...] [scope=product|all] [exclude=GLOB,...]``
  (or ``sink=db-connection`` / ``pattern=REGEX`` instead of ``calls=``): the call may appear only in the
  allowed files;
* ``no_edge from=GLOB to=GLOB [relations=imports,calls,uses]``: no graph edge from one set of files to
  the other;
* ``dependency absent=NAME`` / ``dependency present=NAME``: a declared dependency must (not) exist.

``--revisit-when dependency_added=NAME`` / ``file_appears=GLOB`` asks for a human review when it fires;
``--governs SYMBOL`` asks for a review when that symbol's code changes. Guards from ``record`` and
``guard`` are the human's own and start ``accepted``; guards proposed from a document's prose
(``decide import``) start ``proposed`` and do nothing until ``decide accept``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath

FORMAT_VERSION = 1
DEFAULT_DIR = ".verinoda/decisions"
STATUSES = ("proposed", "accepted", "superseded", "rejected", "deprecated")
GUARD_KINDS = ("only_in", "no_edge", "dependency")
REVISIT_KINDS = ("dependency_added", "file_appears")
EDGE_RELATIONS = ("imports", "imports_from", "calls", "uses", "inherits", "implements", "references")
DEFAULT_EDGE_RELATIONS = ("imports", "imports_from", "calls", "uses", "inherits", "implements")
SINK_KINDS = ("db-connection", "sql-write", "sql-read", "orm-write", "file-write", "kv/object-store")
HUMAN = "human"
GEN_START = "<!-- verinoda:generated (rewritten by verinoda on every change; edit the front matter or run "\
            "`verinoda decide`) -->"
GEN_END = "<!-- /verinoda:generated -->"
FRONT_ORDER = ("verinoda-decision", "id", "title", "status", "decided-by", "date", "chosen", "brief", "source",
               "supersedes", "superseded-by", "governs", "guards", "revisit-when", "waivers")
_ID_RX = re.compile(r"^\s*(?:adr[-_ ]?)?0*(\d{1,6})\s*$", re.I)
_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DecisionError(ValueError):
    """A request the decision rules refuse (unknown id, bad spec, a path outside the repository)."""


@dataclass
class Decision:
    id: str
    number: int
    title: str
    status: str = "accepted"
    decided_by: str = HUMAN
    date: str = ""
    chosen: str | None = None
    brief: str | None = None
    source: str | None = None
    supersedes: str | None = None
    superseded_by: str | None = None
    governs: list[dict] = field(default_factory=list)
    guards: list[dict] = field(default_factory=list)
    revisit_when: list[dict] = field(default_factory=list)
    waivers: list[dict] = field(default_factory=list)
    body: str = ""
    path: Path | None = None
    problems: list[str] = field(default_factory=list)
    # not enforced for a reason another record gives (a later record supersedes it), though its own file
    # reads fully: verinoda may still rewrite it
    inactive: list[str] = field(default_factory=list)
    # entries that are not applied (a waiver whose `until` is not a date): the rest is enforced
    warnings: list[str] = field(default_factory=list)

    def front(self) -> dict:
        return {"verinoda-decision": FORMAT_VERSION, "id": self.id, "title": self.title, "status": self.status,
                "decided-by": self.decided_by, "date": self.date, "chosen": self.chosen, "brief": self.brief,
                "source": self.source, "supersedes": self.supersedes, "superseded-by": self.superseded_by,
                "governs": self.governs, "guards": self.guards, "revisit-when": self.revisit_when,
                "waivers": self.waivers}

    @property
    def enforced(self) -> bool:
        """Its guards are checked: accepted, decided by the human, readable, and not superseded."""
        return self.status == "accepted" and self.decided_by == HUMAN and not self.problems and not self.inactive


# -- ids, paths and the folder --------------------------------------------------------------------

def norm_id(value: str) -> str:
    """``ADR-1`` / ``adr-0001`` / ``1`` -> ``ADR-0001``."""
    m = _ID_RX.match(str(value or ""))
    if not m:
        raise DecisionError(f"{value!r} is not a decision id (ADR-0001, ADR-1 or 1)")
    return f"ADR-{int(m.group(1)):04d}"


def decisions_dir(repo: Path) -> Path:
    """``decisions.dir`` from ``.verinoda/config.json`` (repository-relative), default ``.verinoda/decisions``.

    A folder outside the repository is refused: the records belong to the project."""
    from verinoda.paths import load_config

    repo = Path(repo).resolve()
    try:
        configured = str((load_config(repo).get("decisions") or {}).get("dir") or "").strip()
    except Exception:  # noqa: BLE001 - an unreadable config: the default folder
        configured = ""
    p = Path(configured or DEFAULT_DIR)
    p = (p if p.is_absolute() else repo / p).resolve()
    if p != repo and repo not in p.parents:
        raise DecisionError(f"decisions.dir {configured!r} is outside the repository {repo}")
    return p


def rel_path(repo: Path, value: str, what: str) -> str:
    """A repository-relative posix path (a glob is allowed) that cannot leave the repository."""
    v = str(value or "").strip().replace("\\", "/")
    if not v or v.startswith("-"):
        raise DecisionError(f"{what} {value!r} is empty or looks like an option")
    if v.startswith("/") or re.match(r"^[A-Za-z]:", v) or any(part == ".." for part in v.split("/")):
        raise DecisionError(f"{what} {value!r} must be a path inside the repository (relative, no '..')")
    return v[2:] if v.startswith("./") else v


def _slug(text: str) -> str:
    s = re.sub(r"[^\w-]+", "-", str(text or "").lower(), flags=re.ASCII).strip("-")
    return (s or "decision")[:60].rstrip("-")


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def content_hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# -- front matter ---------------------------------------------------------------------------------

def _value(raw: str):
    raw = raw.strip()
    if raw == "":
        return None
    if raw[:1] in "[{\"" or raw in ("null", "true", "false") or re.fullmatch(r"-?\d+", raw):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def split_front(text: str) -> tuple[dict | None, str]:
    """(front matter, body) of a Markdown text; None when it has no ``---`` header."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, text
    front: dict = {}
    for i in range(1, len(lines)):
        ln = lines[i]
        if ln.strip() == "---":
            return front, "\n".join(lines[i + 1:])
        key, sep, val = ln.partition(":")
        if sep and key.strip():
            front[key.strip()] = _value(val)
    return None, text


def _unread_front_lines(text: str) -> list[int]:
    """Header lines :func:`split_front` does not read (an indented line or a YAML ``- item`` of a block
    list, a line without ``key:``): whatever they held is not in the record."""
    lines = text.split("\n")
    out = []
    for i in range(1, len(lines)):
        ln = lines[i]
        if ln.strip() == "---":
            break
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        key, sep, _ = ln.partition(":")
        if ln[:1].isspace() or ln.startswith("-") or not sep or not key.strip():
            out.append(i + 1)
    return out


def _date_or_none(value) -> str | None:
    """A ``YYYY-MM-DD`` date that exists, else None."""
    if not isinstance(value, str) or not _DATE_RX.match(value):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _dump_front(front: dict) -> str:
    out = ["---"]
    for k in FRONT_ORDER:
        v = front.get(k)
        out.append(f"{k}: " + (v if isinstance(v, str) and v and "\n" not in v and v.strip() == v
                                and _value(v) == v else json.dumps(v, ensure_ascii=False)))
    out.append("---")
    return "\n".join(out)


def _entry_problems(lists: dict[str, list[dict]]) -> list[str]:
    """What a hand edit broke in the entries the check reads (a record with any of these is not enforced)."""
    out: list[str] = []

    def text(x) -> bool:
        return isinstance(x, str) and bool(x.strip())

    ids: dict[str, set] = {}
    for key, need in (("governs", ("id", "file", "qual", "symbol")), ("revisit-when", ("id", "kind", "value")),
                      ("guards", ("id",)), ("waivers", ("guard", "at", "reason"))):
        for i, e in enumerate(lists.get(key) or [], 1):
            missing = [k for k in need if not text(e.get(k))]
            if missing:
                out.append(f"{key} entry {e.get('id') or i}: {', '.join(missing)} missing or not text")
                continue
            if "id" in need:
                if e["id"] in ids.setdefault(key, set()):
                    out.append(f"{key} id {e['id']} is used twice")
                ids[key].add(e["id"])
            if key == "revisit-when" and e["kind"] not in REVISIT_KINDS:
                out.append(f"revisit-when {e['id']}: kind {e['kind']!r} is not one of {', '.join(REVISIT_KINDS)}")
            if key in ("governs", "waivers"):
                p = str(e.get("file") if key == "governs" else e.get("at")).replace("\\", "/")
                if p.startswith("/") or re.match(r"^[A-Za-z]:", p) or ".." in p.split("/"):
                    out.append(f"{key} entry {e.get('id') or i}: {p!r} is not a path inside the repository")
    return out


def parse(path: Path) -> Decision | None:
    """A decision from a Markdown file with a ``verinoda-decision`` front matter, else None."""
    try:
        # utf-8-sig: an editor that saves with a byte-order mark must not make the record disappear
        text = path.read_bytes().decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    except OSError:
        return None
    front, body = split_front(text)
    if not front or "verinoda-decision" not in front:
        if "verinoda-decision" in text[:4000]:  # meant as a record, but its front matter is not readable
            m = re.match(r"^(ADR-\d{1,6})\b", path.name, re.I)
            try:
                did = norm_id(m.group(1)) if m else path.stem
            except DecisionError:
                did = path.stem
            return Decision(id=did, number=int(did[4:]) if did.startswith("ADR-") else 0, title=path.name,
                            path=path, problems=["the front matter is not readable: the file must start with a "
                                                 "`---` line and the header must end with another `---` line"])
        return None
    problems = []
    unread = _unread_front_lines(text)
    if unread:  # e.g. guards written as a YAML block list: the record would be enforced without them
        problems.append(f"front matter line(s) {', '.join(map(str, unread[:6]))} are not one-line `key: value` "
                        "(YAML block lists and nested values are not read; a list is one line of JSON, such as "
                        "guards: [{...}])")
    try:
        did = norm_id(front.get("id") or "")
    except DecisionError as exc:
        return Decision(id=str(front.get("id")), number=0, title=str(front.get("title") or ""), path=path,
                        problems=[str(exc), *problems])
    lists = {}
    for key in ("governs", "guards", "revisit-when", "waivers"):
        v = front.get(key)
        if v is None:
            v = []
        if not isinstance(v, list) or not all(isinstance(x, dict) for x in v):
            problems.append(f"{key} is not a JSON list of objects")
            v = []
        lists[key] = v
    problems += _entry_problems(lists)
    status = str(front.get("status") or "")
    if status not in STATUSES:
        problems.append(f"status {status!r} is not one of {', '.join(STATUSES)}")
    by = str(front.get("decided-by") or "")
    if by != HUMAN:
        problems.append(f"decided-by is {by!r}: a decision is always the human's (decided-by: human)")
    for g in lists["guards"]:
        try:
            validate_guard(g)
        except DecisionError as exc:
            problems.append(f"guard {g.get('id')}: {exc}")
    # a waiver whose `until` is not a date is not applied (it must never waive forever)
    warnings = [f"waiver of {w.get('guard')} at {w.get('at')}: until {w['until']!r} is not a date (YYYY-MM-DD), so "
                "the waiver is not applied" for w in lists["waivers"]
                if w.get("until") not in (None, "") and _date_or_none(w.get("until")) is None]
    return Decision(id=did, number=int(did[4:]), title=str(front.get("title") or ""), status=status,
                    decided_by=by, date=str(front.get("date") or ""), chosen=front.get("chosen"),
                    brief=front.get("brief"), source=front.get("source"), supersedes=front.get("supersedes"),
                    superseded_by=front.get("superseded-by"), governs=lists["governs"], guards=lists["guards"],
                    revisit_when=lists["revisit-when"], waivers=lists["waivers"], body=body, path=path,
                    problems=problems, warnings=warnings)


def load_all(repo: Path) -> list[Decision]:
    """Every record in ``decisions.dir``, by number.

    Records that contradict each other are not both enforced: two files with the same id both carry a
    problem, and an accepted record that another enforced record supersedes is not enforced (its own file
    may still say accepted, e.g. after a merge)."""
    d = decisions_dir(repo)
    out = [x for p in sorted(d.glob("*.md")) if (x := parse(p)) is not None] if d.is_dir() else []
    by_id: dict[str, list[Decision]] = {}
    for x in out:
        if x.id.startswith("ADR-"):
            by_id.setdefault(x.id, []).append(x)
    for same in by_id.values():
        if len(same) > 1:
            for x in same:
                others = ", ".join(o.path.name if o.path else "?" for o in same if o is not x)
                x.problems.append(f"id {x.id} is also used by {others}: which record holds is not known")
    sup: list[tuple[Decision, str]] = []
    for x in out:
        if x.enforced and x.supersedes:
            try:
                sup.append((x, norm_id(x.supersedes)))
            except DecisionError:
                continue
    pairs = {(x.id, old) for x, old in sup}
    for x, old in sup:
        if (old, x.id) in pairs:
            x.problems.append(f"{x.id} and {old} supersede each other: which one holds is not known")
            continue
        for y in by_id.get(old) or []:
            if y is not x and y.status == "accepted":
                y.inactive.append(f"{x.id} supersedes it (its own file still says status accepted)")
    return sorted(out, key=lambda x: (x.number, str(x.path)))


def find(repo: Path, did: str) -> Decision | None:
    did = norm_id(did)
    hits = [d for d in load_all(repo) if d.id == did]
    return hits[0] if hits else None


# -- specs ----------------------------------------------------------------------------------------

def _split(spec: str) -> list[str]:
    """Shell-like words, but a backslash is kept as written (a Windows path, ``\\b`` or ``\\.`` in a regex):
    only quotes group words."""
    lx = shlex.shlex(spec, posix=True)
    lx.whitespace_split = True
    lx.escape = ""
    lx.commenters = ""
    return list(lx)


def _kv(spec: str, what: str) -> tuple[str, dict[str, str]]:
    try:
        toks = _split(spec)
    except ValueError as exc:
        raise DecisionError(f"{what} {spec!r}: {exc}") from None
    if not toks:
        raise DecisionError(f"empty {what}")
    kind, rest = toks[0], toks[1:]
    kv: dict[str, str] = {}
    for t in rest:
        k, sep, v = t.partition("=")
        if not sep or not k:
            raise DecisionError(f"{what} {spec!r}: '{t}' is not key=value")
        kv[k.strip().lower()] = v
    return kind.strip().lower(), kv


def _list(v: str | None) -> list[str]:
    return [x.strip() for x in str(v or "").split(",") if x.strip()]


_DOTTED = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+$")
_NAME = re.compile(r"^[A-Za-z_][\w.+-]*(?::[A-Za-z_][\w.+-]*)?$")  # a package, or Maven group:artifact


def _texts(g: dict, key: str, *, paths: bool = False) -> list[str]:
    """``g[key]`` as a list of non-empty strings (a hand edit may write one string: its characters would be
    read as paths), inside the repository when ``paths``."""
    v = g.get(key)
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise DecisionError(f"{key} must be a JSON list of non-empty strings, such as [\"orders/repository.py\"]; "
                            f"got {json.dumps(v, ensure_ascii=False)[:80]}")
    for p in v if paths else []:
        q = p.strip().replace("\\", "/")
        if q.startswith("/") or re.match(r"^[A-Za-z]:", q) or ".." in q.split("/"):
            raise DecisionError(f"{key} {p!r} is not a path inside the repository (relative, no '..')")
    return v


def validate_guard(g: dict) -> dict:
    """Raise :class:`DecisionError` unless ``g`` is a guard this version can check."""
    kind = g.get("kind")
    if kind not in GUARD_KINDS:
        raise DecisionError(f"guard kind {kind!r} is not one of {', '.join(GUARD_KINDS)}")
    if g.get("status", "accepted") not in ("proposed", "accepted"):
        raise DecisionError(f"guard status {g.get('status')!r} is not proposed/accepted")
    for key in ("pattern", "sink", "from", "to", "absent", "present", "scope"):
        if g.get(key) is not None and not isinstance(g[key], str):
            raise DecisionError(f"{key} must be a string")
    _texts(g, "calls")
    _texts(g, "relations")
    _texts(g, "allowed", paths=True)
    _texts(g, "exclude", paths=True)
    if kind == "only_in":
        what = [k for k in ("calls", "sink", "pattern") if g.get(k)]
        if len(what) != 1:
            raise DecisionError("only_in needs exactly one of calls=, sink= or pattern=")
        for c in g.get("calls") or []:
            if not _DOTTED.match(c):
                raise DecisionError(f"calls={c!r} must be a dotted name such as sqlite3.connect or "
                                    "PayloadRegistrar.playToServer")
        if g.get("sink") and g["sink"] not in SINK_KINDS:
            raise DecisionError(f"sink={g['sink']!r} is not one of {', '.join(SINK_KINDS)}")
        if g.get("pattern"):
            try:
                re.compile(g["pattern"])
            except re.error as exc:
                raise DecisionError(f"pattern={g['pattern']!r} is not a valid regex: {exc}") from None
        if not g.get("allowed"):
            raise DecisionError("only_in needs allowed=PATH[,GLOB...] (the files the call may appear in)")
        if g.get("scope", "product") not in ("product", "all"):
            raise DecisionError("scope must be product (default: no tests, reference trees or copies) or all")
    elif kind == "no_edge":
        if not g.get("from") or not g.get("to"):
            raise DecisionError("no_edge needs from=GLOB and to=GLOB")
        bad = [r for r in g.get("relations") or [] if r not in EDGE_RELATIONS]
        if bad:
            raise DecisionError(f"relations {bad} are not graph relations ({', '.join(EDGE_RELATIONS)})")
    else:
        which = [k for k in ("absent", "present") if g.get(k)]
        if len(which) != 1 or not _NAME.match(str(g[which[0]])):
            raise DecisionError("dependency needs absent=NAME or present=NAME (a package name)")
    return g


def parse_guard(spec: str, repo: Path, gid: str, *, status: str = "accepted") -> dict:
    kind, kv = _kv(spec, "guard")
    g: dict = {"id": gid, "kind": kind, "status": status, "spec": spec.strip()}
    if kind == "only_in":
        if "calls" in kv:
            g["calls"] = _list(kv.pop("calls"))
        if "sink" in kv:
            g["sink"] = kv.pop("sink").strip()
        if "pattern" in kv:
            g["pattern"] = kv.pop("pattern")
        g["allowed"] = [rel_path(repo, a, "allowed") for a in _list(kv.pop("allowed", ""))]
        g["scope"] = kv.pop("scope", "product").strip().lower() or "product"
        excl = kv.pop("exclude", "")
        if excl:
            g["exclude"] = [rel_path(repo, a, "exclude") for a in _list(excl)]
    elif kind == "no_edge":
        for k in ("from", "to"):
            v = kv.pop(k, "")
            g[k] = rel_path(repo, v, k) if v else ""
        g["relations"] = _list(kv.pop("relations", "")) or list(DEFAULT_EDGE_RELATIONS)
        if "imports" in g["relations"] and "imports_from" not in g["relations"]:
            g["relations"].append("imports_from")
    elif kind == "dependency":
        for k in ("absent", "present"):
            if k in kv:
                g[k] = kv.pop(k).strip()
    if kv:
        raise DecisionError(f"guard {spec!r}: unknown key(s) {', '.join(sorted(kv))}")
    return validate_guard(g)


def parse_revisit(spec: str, repo: Path, rid: str) -> dict:
    k, sep, v = str(spec or "").partition("=")
    k, v = k.strip().lower(), v.strip()
    if not sep or k not in REVISIT_KINDS or not v:
        raise DecisionError(f"revisit-when {spec!r} must be dependency_added=NAME or file_appears=GLOB")
    if k == "dependency_added" and not _NAME.match(v):
        raise DecisionError(f"dependency_added={v!r} is not a package name")
    if k == "file_appears":
        v = rel_path(repo, v, "file_appears")
    return {"id": rid, "kind": k, "value": v, "spec": spec.strip()}


def parse_governs(repo: Path, spec: str, vid: str, graph=None) -> dict:
    """``path/file.py::Qual.name`` (or a name the graph resolves) with the fingerprint of its code now."""
    from verinoda import anchors

    s = str(spec or "").strip()
    if not s or s.startswith("-"):
        raise DecisionError(f"governs {spec!r} is empty or looks like an option")
    path, sep, qual = s.partition("::")
    if not sep:
        if graph is None:
            raise DecisionError(f"governs {spec!r}: write it as path/file.py::Symbol (no index to resolve it)")
        nid, _ = graph.resolve(s)
        if not nid or not graph.file(nid):
            raise DecisionError(f"governs {spec!r}: no symbol of that name in the index; write path::Symbol")
        path, qual = graph.file(nid), graph.label(nid)
    path = rel_path(repo, path, "governs")
    qual = qual.strip().strip(".").replace("()", "")
    facts, _ = anchors.facts_for_path(Path(repo) / path, path)
    hits = anchors.symbols_named(facts, qual) if facts else []
    if len(hits) != 1:
        raise DecisionError(f"governs {spec!r}: {'no' if not hits else len(hits)} symbol(s) named {qual!r} in "
                            f"{path}" + ("" if facts else " (the file cannot be read or has no symbol facts)"))
    q, sym = hits[0]
    return {"id": vid, "symbol": f"{path}::{q}", "file": path, "qual": q, "lines": [sym["start"], sym["end"]],
            "fp": sym.get("full"), "scheme": facts.get("scheme")}


def _next_id(items: list[dict], prefix: str) -> str:
    nums = [int(m.group(1)) for x in items if (m := re.fullmatch(rf"{prefix}(\d+)", str(x.get("id") or "")))]
    return f"{prefix}{max(nums, default=0) + 1}"


# -- writing ----------------------------------------------------------------------------------------

def _guard_line(g: dict) -> str:
    return f"- {g['id']} ({g.get('status', 'accepted')}): `{g.get('spec') or json.dumps(g, ensure_ascii=False)}`"


def _generated(d: Decision) -> str:
    lines = [GEN_START, "## Enforced by `verinoda decide check`", ""]
    lines += [_guard_line(g) for g in d.guards] or ["- no guards"]
    for v in d.governs:
        lines.append(f"- governs {v['id']}: `{v['symbol']}` (a change to its code asks for a review)")
    for r in d.revisit_when:
        lines.append(f"- revisit {r['id']} when {r['kind']}={r['value']}")
    for w in d.waivers:
        lines.append(f"- waiver on {w['guard']} at `{w['at']}`" + (f" until {w['until']}" if w.get("until") else "")
                     + f": {w['reason']}")
    lines.append(GEN_END)
    return "\n".join(lines)


def _with_generated(body: str, d: Decision) -> str:
    gen = _generated(d)
    a, b = body.find(GEN_START), body.find(GEN_END)
    if a != -1 and b > a:
        return body[:a] + gen + body[b + len(GEN_END):]
    return body.rstrip("\n") + "\n\n" + gen + "\n"


def render(d: Decision) -> str:
    return _dump_front(d.front()) + "\n" + _with_generated(d.body, d).lstrip("\n")


def _atomic_write(path: Path, text: str) -> bytes:
    data = text.replace("\r\n", "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".md", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return data


def _last_row(store, did: str) -> dict | None:
    return store.one("SELECT * FROM decisions WHERE id = ? ORDER BY seq DESC LIMIT 1", (did,))


def _log(store, repo: Path, d: Decision, event: str, data: bytes | None, *, user_statement: str | None = None,
         rationale: str | None = None) -> None:
    prev = _last_row(store, d.id) or {}
    store.insert("decisions", {
        "id": d.id, "number": d.number, "event": event, "status": d.status, "title": d.title,
        "brief_id": d.brief, "chosen": d.chosen,
        "rationale": rationale if rationale is not None else prev.get("rationale"),
        "decided_by": d.decided_by, "user_statement": user_statement,
        "doc_path": d.path.resolve().relative_to(Path(repo).resolve()).as_posix() if d.path else None,
        "doc_hash": content_hash(data) if data is not None else None, "source_doc": d.source,
        "guards": d.guards, "waivers": d.waivers, "governs": d.governs, "revisit_when": d.revisit_when,
        "supersedes": d.supersedes, "superseded_by": d.superseded_by, "created_at": _now()})


def _save(store, repo: Path, d: Decision, event: str, **kw) -> dict:
    data = _atomic_write(d.path, render(d))
    _log(store, repo, d, event, data, **kw)
    return as_dict(repo, d, store=store)


def _next_number(store, repo: Path) -> int:
    from verinoda.architecture_map import DOC_DECISION_RE
    from verinoda.snapshot import list_files

    nums = [d.number for d in load_all(repo)]
    row = store.one("SELECT MAX(number) AS n FROM decisions")
    if row and row.get("n"):
        nums.append(int(row["n"]))
    for rel in list_files(repo):  # a hand-written ADR's number is taken too (docs/adr/0001-...)
        if DOC_DECISION_RE.search(rel) and rel.lower().endswith((".md", ".rst", ".txt")):
            m = re.match(r"^(?:adr[-_]?)?(\d{1,6})\b", PurePosixPath(rel).name, re.I)
            if m:
                nums.append(int(m.group(1)))
    return max(nums, default=0) + 1


def _context_md(brief: dict | None, answers: list[dict]) -> str:
    """The brief's forces, absences and the user's answers (each labelled), for a record's Context."""
    if not brief:
        return "No decision brief was recorded with this decision."
    out = [f"From decision brief {brief.get('brief_id')} ({brief.get('understood_as') or ''}):", ""]
    for f in brief.get("forces") or []:
        ev = ", ".join(e.get("locator", "") for e in f.get("evidence") or [])
        out.append(f"- [{f.get('status')}] {f.get('fact')}" + (f" ({ev})" if ev else ""))
    for a in brief.get("absences") or []:
        out.append(f"- [absent] {a.get('what')} (searched: {', '.join(a.get('searched') or [])})")
    for a in answers:
        out.append(f"- [answered by the user] {a.get('question_id')}: {a.get('answer')}")
    return "\n".join(out)


def record(store, repo: Path, *, chosen: str, rationale: str, title: str | None = None, brief_id: str | None = None,
           guards: list[str] = (), governs: list[str] = (), revisit_when: list[str] = (),
           supersedes: str | None = None, user_statement: str | None = None, graph=None) -> dict:
    """Record the human's choice as a new decision (status accepted, decided-by human)."""
    repo = Path(repo).resolve()
    chosen, rationale = str(chosen or "").strip(), str(rationale or "").strip()
    if not chosen or not rationale:
        raise DecisionError("a decision needs what was chosen (--chosen) and why (--rationale), in the human's words")
    brief = None
    answers: list[dict] = []
    if brief_id:
        row = store.get("decision_briefs", brief_id)
        if row is None:
            raise DecisionError(f"no decision brief {brief_id}")
        brief = row.get("result") or {}
        answers = store.all("SELECT question_id, answer, created_at FROM decision_answers WHERE brief_id = ? "
                            "ORDER BY seq", (brief_id,))
    old = None
    if supersedes:
        old = find(repo, supersedes)
        if old is None:
            raise DecisionError(f"no decision record {norm_id(supersedes)} in {decisions_dir(repo)} "
                                "(`verinoda decide import` a hand-written ADR first)")
        if old.status == "superseded":
            raise DecisionError(f"{old.id} is already superseded by {old.superseded_by}")
        _refuse_unreadable(old)
    n = _next_number(store, repo)
    did = f"ADR-{n:04d}"
    title = str(title or "").strip() or (f"Use {chosen}" if not brief else str(brief.get("question") or "")[:100]
                                          or f"Use {chosen}")
    d = Decision(id=did, number=n, title=title, status="accepted", decided_by=HUMAN, date=_today(),
                 chosen=chosen, brief=brief_id, supersedes=old.id if old else None)
    for spec in guards:
        d.guards.append(parse_guard(spec, repo, _next_id(d.guards, "g")))
    for spec in revisit_when:
        r = parse_revisit(spec, repo, _next_id(d.revisit_when, "r"))
        from verinoda import guards

        # a condition that already holds now is not news later: it fires only once it starts to hold
        r["baseline"] = bool(guards.revisit_holds(repo, r))
        d.revisit_when.append(r)
    for spec in governs:
        d.governs.append(parse_governs(repo, spec, _next_id(d.governs, "v"), graph))
    said = f"\n\nIn the user's words: \"{user_statement.strip()}\"" if user_statement and user_statement.strip() else ""
    d.body = (f"# {did}: {title}\n\n## Decision\n\nChosen: **{chosen}** (decided by the human, {d.date})."
              f"{' Supersedes ' + old.id + '.' if old else ''}\n\n{rationale}{said}\n\n## Context\n\n"
              f"{_context_md(brief, answers)}\n")
    d.path = decisions_dir(repo) / f"{did}-{_slug(title)}.md"
    out = _save(store, repo, d, "record", user_statement=user_statement, rationale=rationale)
    if old is not None:
        old.status, old.superseded_by = "superseded", did
        _save(store, repo, old, "supersede", user_statement=user_statement)
        out["superseded"] = old.id
    return out


def _require(repo: Path, did: str) -> Decision:
    d = find(repo, did)
    if d is None:
        raise DecisionError(f"no decision record {norm_id(did)} in {decisions_dir(repo)} (`verinoda decide list`)")
    _refuse_unreadable(d)
    return d


def _refuse_unreadable(d: Decision) -> None:
    """A record whose front matter could not be read fully is never rewritten (it would lose what was there)."""
    if d.problems:
        raise DecisionError(f"{d.id} ({d.path}) has problems: {'; '.join(d.problems)}. Fix its front matter by hand "
                            "first; verinoda does not rewrite a record it cannot read fully")


def add_guards(store, repo: Path, did: str, specs: list[str], *, user_statement: str | None = None,
               status: str = "accepted") -> dict:
    """Add guards (the human's own: accepted) to an existing record."""
    repo = Path(repo).resolve()
    d = _require(repo, did)
    for spec in specs:
        d.guards.append(parse_guard(spec, repo, _next_id(d.guards, "g"), status=status))
    return _save(store, repo, d, "guard", user_statement=user_statement)


def accept(store, repo: Path, did: str, guard_ids: list[str], *, user_statement: str | None = None) -> dict:
    """Activate proposed guards (``decide accept ADR-1 g1``)."""
    repo = Path(repo).resolve()
    d = _require(repo, did)
    by = {g["id"]: g for g in d.guards}
    missing = [g for g in guard_ids if g not in by]
    if missing or not guard_ids:
        raise DecisionError(f"{d.id} has no guard {', '.join(missing) or '(none given)'}; its guards: "
                            f"{', '.join(by) or 'none'}")
    for gid in guard_ids:
        by[gid]["status"] = "accepted"
    return _save(store, repo, d, "accept", user_statement=user_statement)


def waive(store, repo: Path, did: str, guard_id: str, *, at: str, reason: str, until: str | None = None,
          user_statement: str | None = None) -> dict:
    """Excuse one site from one guard (a waiver is the human's call; it may expire)."""
    repo = Path(repo).resolve()
    d = _require(repo, did)
    if guard_id not in {g["id"] for g in d.guards}:
        raise DecisionError(f"{d.id} has no guard {guard_id}")
    path, _, line = str(at or "").rpartition(":") if re.search(r":\d+$", str(at or "")) else (at, "", "")
    path = rel_path(repo, path, "--at")
    if not str(reason or "").strip():
        raise DecisionError("a waiver needs a reason (--reason)")
    if until and _date_or_none(until) is None:
        raise DecisionError(f"--until {until!r} must be a date (YYYY-MM-DD)")
    d.waivers.append({"guard": guard_id, "at": f"{path}:{line}" if line else path, "reason": reason.strip(),
                      "until": until or None, "date": _today()})
    return _save(store, repo, d, "waive", user_statement=user_statement)


def waived(d: Decision, guard_id: str, path: str, line: int | None, today: str | None = None) -> dict | None:
    """The waiver that excuses ``path[:line]`` from ``guard_id`` today, if any."""
    today = today or _today()
    for w in d.waivers:
        until = w.get("until")
        if w.get("guard") != guard_id:
            continue
        if until not in (None, ""):  # an unreadable date never waives (parse() lists it as a warning)
            if _date_or_none(until) is None or _date_or_none(until) < today:
                continue
        wp, _, wl = str(w.get("at") or "").rpartition(":") if re.search(r":\d+$", str(w.get("at") or "")) \
            else (w.get("at"), "", "")
        if wp == path and (not wl or (line is not None and int(wl) == line)):
            return w
    return None


# -- a hand-written ADR ---------------------------------------------------------------------------

_MUST = re.compile(r"\b(?:only|no other|must|must not|never|may not|shall not|cannot|sadece|yaln[ıi]zca|asla|"
                   r"hi[cç]bir)\b", re.I)
_PERSIST = re.compile(r"\b(?:database|db|connection|connections|sqlite\w*|sql|persist\w*|storage|veritaban\w*)\b",
                      re.I)
# a sentence: up to a . ! ? ; followed by white space (not the dot of orders/config.py), within a paragraph
_SENT = re.compile(r"(?:[^.!?;\n]|[.!?;](?!\s|$)|\n(?![ \t]*\n))+(?:[.!?;](?=\s|$))?")


def quantified_sentences(text: str) -> list[dict]:
    """Sentences of a document that carry a quantifier or must/never (``{text, line, end}``), in order."""
    import bisect

    from verinoda.entail import QUANTIFIERS

    starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
    out: list[dict] = []
    for m in _SENT.finditer(text):
        raw = m.group(0)
        s = " ".join(raw.split())
        if not s or s.startswith(("#", "|", "```")) or re.match(r"^status\s*:", s, re.I):
            continue
        words = {w.lower() for w in re.findall(r"[\w']+", s)}
        if words & QUANTIFIERS or _MUST.search(s):
            a = m.start() + len(raw) - len(raw.lstrip())
            b = m.start() + len(raw.rstrip()) - 1
            out.append({"text": s, "line": bisect.bisect_right(starts, a), "end": bisect.bisect_right(starts, b)})
    return out


def import_doc(store, repo: Path, rel: str, *, graph=None, user_statement: str | None = None) -> dict:
    """A record for a hand-written ADR (the document itself is never edited).

    Its id is the document's number, its status the document's ``Status:`` line. Guards are only
    *proposed*, from sentences that carry a quantifier or must/never and name code the index knows
    (``only X`` + a persistence word -> ``only_in sink=db-connection allowed=<X's file>``); they do
    nothing until the human accepts them. Every other such sentence is listed as not turned into a guard.
    """
    repo = Path(repo).resolve()
    rel = rel_path(repo, rel, "document")
    p = (repo / rel).resolve()
    if repo not in p.parents or not p.is_file():
        raise DecisionError(f"{rel} is not a file in the repository")
    text = p.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")
    m = re.match(r"^(?:adr[-_]?)?(\d{1,6})\b", p.name, re.I) or re.search(r"\bADR[-\s]?(\d{1,6})\b", text)
    if not m:
        raise DecisionError(f"{rel}: no number in its name or title (ADR-0001, 0001-...md); record it with "
                            "`verinoda decide record` instead")
    did = norm_id(m.group(1))
    if find(repo, did) is not None:
        raise DecisionError(f"{did} already has a record ({find(repo, did).path}); add guards with "
                            f"`verinoda decide guard {did} SPEC`")
    title_m = re.search(r"^#\s+(.+)$", text, re.M)
    status_m = re.search(r"^\s*status\s*:\s*(\w+)", text, re.M | re.I)
    status = (status_m.group(1).lower() if status_m else "accepted")
    status = status if status in STATUSES else "accepted"
    title = re.sub(r"^(?:ADR)?[-\s]*\d+\s*[:.\-]\s*", "", title_m.group(1).strip(), flags=re.I) if title_m else did
    d = Decision(id=did, number=int(did[4:]), title=title or did, status=status,
                 decided_by=HUMAN, date=_today(), source=rel)
    not_guards = []
    for s in quantified_sentences(text):
        names = []
        if graph is not None:
            for tok in dict.fromkeys(re.findall(r"`([^`]+)`|\b([A-Z][A-Za-z0-9_]{3,})\b", s["text"])):
                name = next(t for t in tok if t)
                nid, _ = graph.resolve(name)
                if nid and graph.file(nid) and graph.label(nid).strip(".()").rpartition(".")[2] == \
                        name.strip(".()").rpartition(".")[2]:
                    names.append((name, graph.file(nid)))
        at = f"{rel}:{s['line']}" + (f"-{s['end']}" if s["end"] != s["line"] else "")
        if names and re.search(r"\b(?:only|sadece|yaln[ıi]zca)\b", s["text"], re.I) and _PERSIST.search(s["text"]):
            allowed = sorted({f for _, f in names})
            spec = f"only_in sink=db-connection allowed={','.join(allowed)}"
            g = parse_guard(spec, repo, _next_id(d.guards, "g"), status="proposed")
            g["from_sentence"] = {"text": s["text"], "at": at}
            d.guards.append(g)
        else:
            not_guards.append({"text": s["text"], "at": at,
                               "why": "names no code the index knows" if not names else
                               "no rule turns this sentence into a guard"})
    d.body = (f"# {did}: {d.title}\n\nRecord for the hand-written decision document `{rel}` (the document is "
              "not changed). Guards proposed from its sentences are inactive until the human accepts them "
              f"(`verinoda decide accept {did} g1`).\n")
    d.path = decisions_dir(repo) / f"{did}-{_slug(d.title)}.md"
    out = _save(store, repo, d, "import", user_statement=user_statement)
    out["not_turned_into_guards"] = not_guards
    return out


# -- reading --------------------------------------------------------------------------------------

def as_dict(repo: Path, d: Decision, *, store=None) -> dict:
    out = {"id": d.id, "title": d.title, "status": d.status, "decided_by": d.decided_by, "date": d.date,
           "chosen": d.chosen, "brief": d.brief, "source": d.source, "supersedes": d.supersedes,
           "superseded_by": d.superseded_by, "guards": d.guards, "governs": d.governs,
           "revisit_when": d.revisit_when, "waivers": d.waivers, "enforced": d.enforced,
           "file": d.path.resolve().relative_to(Path(repo).resolve()).as_posix() if d.path else None}
    if d.problems:
        out["problems"] = d.problems
    if d.inactive:
        out["not_enforced_because"] = d.inactive
    if d.warnings:
        out["warnings"] = d.warnings
    if store is not None and d.path is not None:
        row = _last_row(store, d.id)
        try:
            now_hash = content_hash(d.path.read_bytes().replace(b"\r\n", b"\n"))
        except OSError:
            now_hash = None
        if row is None:
            out["log"] = "not in this database's decision log (written elsewhere, or the database is new)"
        elif row.get("doc_hash") and now_hash != row["doc_hash"]:
            out["log"] = f"edited by hand since its last logged event ({row['event']}, {row['created_at']}); " \
                         "the file as it is now is what is checked"
    return out


def listing(store, repo: Path) -> dict:
    """Every record, plus hand-written decision documents that have no record (candidates for import)."""
    from verinoda.architecture_map import DOC_DECISION_RE
    from verinoda.snapshot import list_files

    repo = Path(repo).resolve()
    recs = load_all(repo)
    d_dir = decisions_dir(repo)
    sources = {d.source for d in recs if d.source}
    docs = []
    for rel in list_files(repo):
        p = (repo / rel).resolve()
        if d_dir in p.parents or rel in sources or not DOC_DECISION_RE.search(rel):
            continue
        if re.search(r"(^|/)(adr|adrs|decisions?)/", rel, re.I) and rel.lower().endswith(".md"):
            docs.append(rel)
    return {"dir": str(d_dir), "decisions": [as_dict(repo, d, store=store) for d in recs],
            "unrecorded_docs": docs,
            "note": "records are checked by `verinoda decide check`; a hand-written ADR has no guards until "
                    "`verinoda decide import` proposes some and the human accepts them"}
