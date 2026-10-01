"""Undocumented decisions: structural choices in the code that no decision record covers (``verinoda decide
undocumented``).

The probes of :mod:`verinoda.decision_brief` find the facts; this module keeps the ones that look like a choice
somebody made, and lists those that no record mentions:

* ``single_storage_path``: every storage sink line of a kind (database connection, SQL, ORM write, file write,
  key-value store) in the product code is in one file - storage goes through that file;
* ``exclusive_library``: a declared Python dependency is imported (through any of its modules) by exactly one
  product file - the library is kept behind that file (the guard's import pattern, run on the other product
  files' code with comments and strings removed, must find none; a module another product file names in a
  string literal of its code, as ``importlib.import_module("mod")`` does, gives no candidate);
* ``single_config_reader``: every environment variable read in the product code is in one file.

The fact under each candidate is ``statically_verified`` with ``path:line`` evidence that re-checks now; that the
fact is a *decision* is a guess, so every candidate is ``weak_inference``, for the user to record (``decide
record``, with the guard spec that would keep it, when a guard kind can check it) or to dismiss (``decide
dismiss``). A project of fewer than three product code files gives no candidate: one file holding everything is
no choice.

A candidate is covered when a live record (status accepted or proposed) or a hand-written ADR-like document names
its file, or names its library (the dependency, its module, the database driver) as one: in a code span, an import
or a fenced block, or on a line that says library, package, module, driver or dependency; or when a record's
``only_in`` guard about the same thing (a storage sink, the driver, the module, the environment reader) allows only
a glob that matches the file. The match is cited as ``path:line``. Dismissals are the user's call and stay local, in
``.verinoda/dismissed_decisions.json`` (not committed); a dismissed candidate is no longer listed, and a dismissal
whose candidate is gone is shown as such.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from verinoda import decision_brief as dbr

LIVE_STATUSES = ("accepted", "proposed")
MIN_PRODUCT_FILES = 3
FILE_NAME = "dismissed_decisions.json"
FORMAT = 1
MAX_EVIDENCE = 4
# distributions whose import name is not the distribution name with '-' read as '_'
PY_MODULES = {"pyyaml": ("yaml",), "beautifulsoup4": ("bs4",), "pillow": ("PIL",), "python-dateutil": ("dateutil",),
              "scikit-learn": ("sklearn",), "opencv-python": ("cv2",), "opencv-python-headless": ("cv2",),
              "protobuf": ("google.protobuf",), "attrs": ("attr", "attrs"), "psycopg2-binary": ("psycopg2",),
              "psycopg-binary": ("psycopg",), "mysqlclient": ("MySQLdb",), "mysql-connector-python": ("mysql",),
              "python-dotenv": ("dotenv",), "pyjwt": ("jwt",), "ruamel.yaml": ("ruamel",), "typing-extensions":
              ("typing_extensions",), "msgpack-python": ("msgpack",), "google-cloud-storage": ("google.cloud.storage",)}
LIMITS = [
    "a candidate is a guess that a fact is a decision (weak_inference): the fact is verified, its being a choice "
    "is not",
    "libraries: Python imports only (read from the syntax tree); a module whose name differs from its distribution "
    "and is not in the known list is not matched; npm, Go, Cargo and JVM dependencies are not checked",
    "storage sinks and environment reads are found by patterns (decision_brief's P1 and P2); what they do not "
    "know is not seen",
    "coverage is text: a record or ADR that names the file, or the library as one (code span, import, or next "
    "to 'library', 'package', 'module', 'driver', 'dependency'), covers it, whatever it says about it; one that "
    "describes the choice in other words does not",
]


class UndocumentedError(ValueError):
    """A request refused (an id that is not a current candidate, a dismissal file that cannot be read)."""


def _q(text: str) -> str:
    return '"' + text.replace('"', '\\"') + '"'


def _cmds(cid: str, title: str, guard: str | None) -> dict:
    rec = f"verinoda decide record --title {_q(title)} --chosen \"<the user's choice>\" " \
          "--rationale \"<the user's reason>\"" + (f" --guard {_q(guard)}" if guard else "")
    return {"record_with": rec, "dismiss_with": f"verinoda decide dismiss {cid} --reason \"<the user's reason>\""}


def _arg(rel: str) -> str | None:
    """``rel`` as one word of a guard spec (``decisions._split`` groups quoted words); None when it cannot be."""
    if not re.search(r"""[\s'"]""", rel):
        return rel
    return f"'{rel}'" if "'" not in rel else (f'"{rel}"' if '"' not in rel else None)


def _candidate(cid: str, kind: str, candidate: str, fact: str, evidence: list, scope: str, guard: str | None,
               title: str, files: list[str], names: list[str], about: list[str]) -> dict | None:
    """``names``: what a document must name to cover the candidate; ``about``: the words in a guard's ``calls``
    or ``pattern`` that make the guard one about this choice (its ``allowed`` glob then covers the file)."""
    evs = [e for e in evidence if e]
    if not evs:
        return None
    return {"id": cid, "kind": kind, "status": "weak_inference", "candidate": candidate,
            "fact": {"text": fact, "status": "statically_verified", "evidence": evs[:MAX_EVIDENCE], "scope": scope},
            "guard": guard, **_cmds(cid, title, guard), "_files": files, "_names": names, "_about": about}


# -- the probes -------------------------------------------------------------------------------------------

def _storage(pb) -> list[dict]:
    by_kind: dict[str, list[tuple[str, int, str, str]]] = {}
    for site in pb.facts.get("sink_sites") or []:
        by_kind.setdefault(site[2], []).append(site)
    by_file: dict[str, list[str]] = {}
    for kind, sites in by_kind.items():
        files = {s[0] for s in sites}
        if len(files) == 1:
            by_file.setdefault(files.pop(), []).append(kind)
    out = []
    for rel, kinds in sorted(by_file.items()):
        sites = [s for k in kinds for s in by_kind[k]]
        if len(sites) < 2 and "db-connection" not in kinds:
            continue  # one sink line is no path
        sites.sort(key=lambda s: s[1])
        drivers = sorted({c.group(1).split(".")[0] for r, _line, why in pb.facts.get("connections") or []
                          if r == rel for c in [re.search(r"call to ([\w.]+)", why)] if c})
        names = sorted({*drivers, *(re.sub(r"\d+$", "", d) for d in drivers)} - {""})
        guard = f"only_in sink=db-connection allowed={_arg(rel)}" if "db-connection" in kinds and _arg(rel) else None
        others = sorted({s[0] for k, ss in by_kind.items() if k not in kinds for s in ss})
        c = _candidate(
            f"storage:{rel}", "single_storage_path",
            f"storage ({', '.join(sorted(kinds))}) goes through one file, {rel}",
            f"all {len(sites)} storage sink line(s) of kind {', '.join(sorted(kinds))} in the product code are in "
            f"{rel}" + (f" (other kinds are in {len(others)} file(s))" if others else ""),
            [dbr._ev(pb.repo, r, i, needle=needle) for r, i, _k, needle in sites],
            f"{len(pb.product)} product code file(s), tests excluded; comments and docstrings are not read", guard,
            f"Storage goes through {rel}", [rel], names, names)
        if c:
            out.append(c)
    return out


def _py_modules(name: str) -> tuple[str, ...]:
    key = re.sub(r"[-_.]+", "-", name.split("[")[0]).lower()
    known = {re.sub(r"[-_.]+", "-", k).lower(): v for k, v in PY_MODULES.items()}
    return known.get(key) or (key.replace("-", "_"),)


def _named_elsewhere(pb, py: list[str], rel: str, mod: str) -> bool:
    """Does a product .py file other than ``rel`` hold ``mod`` as a string literal in its code (not a comment)?"""
    rx = re.compile(rf"""['"]{re.escape(mod)}(?:\.[\w.]*)?['"]""")
    for other in py:
        if other != rel and mod in "\n".join(pb.lines(other)) and \
                any(rx.search(ln) for ln in pb.code_lines(other, keep_strings=True)):
            return True
    return False


def _imports_of(imports: list[tuple[int, str]], mods: tuple[str, ...]) -> list[tuple[int, str]]:
    return [(i, mod) for i, m in imports for mod in mods if m == mod or m.startswith(mod + ".")]


def _libraries(pb) -> list[dict]:
    py = [f for f in pb.product if f.endswith(".py")]
    if len(py) < MIN_PRODUCT_FILES:
        return []
    imports = {rel: pb.py_imports(rel) for rel in py}
    out = []
    seen: set[str] = set()
    for it in (pb.facts.get("deps") or {}).get("items") or []:
        if it.get("ecosystem") != "python" or it.get("scope") not in (None, "runtime"):
            continue
        name = it.get("name") or ""
        if not name or name in seen:
            continue
        seen.add(name)
        mods = _py_modules(name)
        # the importers of any of the distribution's modules (attrs is imported as attr and as attrs)
        importers = {rel: _imports_of(imports[rel], mods) for rel in py}
        importers = {rel: found for rel, found in importers.items() if found}
        if len(importers) != 1:
            continue
        (rel, found), = importers.items()
        line, mod = found[0]
        alt = "|".join(re.escape(m) for m in mods)
        pattern = rf"^\s*(?:import|from)\s+{alt if len(mods) == 1 else f'(?:{alt})'}\b"
        # the guard's own pattern on the other product files' code (comments and strings removed; the probe's
        # cached text, not a new scan of the repository)
        rx = re.compile(pattern)
        if any(rx.search(ln) for other in py if other != rel and any(m in "\n".join(pb.lines(other)) for m in mods)
               for ln in pb.code_lines(other, keep_strings=False)):
            continue  # the guard's own pattern sees another product file: not kept behind one file
        if any(_named_elsewhere(pb, py, rel, m) for m in mods):
            continue  # loaded by name elsewhere (importlib.import_module("mod"), a table of module names)
        guard = f"only_in pattern={pattern} allowed={_arg(rel)}" if _arg(rel) else None
        used = sorted({m for _i, m in found})
        c = _candidate(
            f"library:{name}", "exclusive_library", f"{name} is kept behind one file, {rel}",
            f"{name} (declared at {it.get('at')}) is imported as {', '.join(used)} by one product file of "
            f"{len(py)}: {rel}:{line}",
            [dbr._ev(pb.repo, rel, line, needle=re.escape(mod)),
             dbr._ev(pb.repo, it.get("path") or "", it.get("line") or 0,
                     needle=re.escape(name.split("[")[0]).replace("\\-", "[-_.]"), source_type="manifest")],
            f"{len(py)} product .py file(s), tests excluded; imports read from the syntax tree", guard,
            f"Keep {name} behind {rel}", [rel], sorted({name, *mods}), sorted({name, *mods}))
        if c:
            out.append(c)
    return out


def _config(pb) -> list[dict]:
    reads = pb.facts.get("env") or []
    files = sorted({r[1] for r in reads})
    if len(files) != 1 or len({r[0] for r in reads}) < 2:
        return []
    rel = files[0]
    guard = rf"only_in pattern=\bos\.(?:environ|getenv)\b allowed={_arg(rel)}" \
        if rel.endswith(".py") and _arg(rel) else None
    c = _candidate(
        f"config:{rel}", "single_config_reader", f"configuration is read from the environment in one file, {rel}",
        f"all {len(reads)} environment read(s) ({len({r[0] for r in reads})} variables) in the product code are in "
        f"{rel}",
        [dbr._ev(pb.repo, r, i, needle=re.escape(var)) for var, r, i, _d in reads],
        f"{len(pb.product)} product code file(s), tests excluded; comments are not read", guard,
        f"Read the environment only in {rel}", [rel], [], ["environ", "getenv"])
    return [c] if c else []


# -- coverage ---------------------------------------------------------------------------------------------

def _guards(d) -> list[dict]:
    """The record's ``only_in`` guards: the only kind whose ``allowed`` glob says where a thing may be (the
    globs of an edge, layer or public guard are about a dependency direction, not about a storage path, a
    library or the environment)."""
    return [g for g in (d.guards if isinstance(d.guards, list) else [])
            if isinstance(g, dict) and g.get("kind") == "only_in" and g.get("allowed")]


def _about(c: dict, g: dict) -> bool:
    """Is the ``only_in`` guard ``g`` about the candidate's choice: a storage sink for a storage path, or its
    ``calls``/``pattern`` naming the driver, the library's module or the environment reader?"""
    if c["kind"] == "single_storage_path" and g.get("sink"):
        return True
    text = " ".join([*(g.get("calls") or []), str(g.get("pattern") or "")])
    text = re.sub(r"\\[bBsSwWdDAZ]", " ", text).replace("\\", "")
    return any(re.search(rf"(?<![\w]){re.escape(w)}(?![\w])", text) for w in c["_about"])


def _match_line(lines: list[str], rx: re.Pattern) -> int | None:
    return next((i for i, ln in enumerate(lines, 1) if rx.search(ln)), None)


# a line that names a library as one: the word next to it, a code span or an import (a fenced block is code)
_LIB_WORDS = re.compile(r"\b(?:librar(?:y|ies)|packages?|modules?|drivers?|dependenc(?:y|ies)|pip|pypi|"
                        r"requirements)\b", re.I)


def _name_line(lines: list[str], name: str) -> int | None:
    """The first line that names ``name`` as a library, not as an ordinary word ("HTTP requests" does not)."""
    word = rf"(?<![\w.-]){re.escape(name)}(?![\w-])"
    rx, span = re.compile(word, re.I), re.compile(rf"`[^`]*{word}[^`]*`", re.I)
    imp = re.compile(rf"\b(?:import|from)\s+{re.escape(name)}\b")
    fence = False
    for i, ln in enumerate(lines, 1):
        if ln.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if rx.search(ln) and (fence or span.search(ln) or imp.search(ln) or _LIB_WORDS.search(ln)):
            return i
    return None


def _covers(c: dict, rel: str, lines: list[str], guards_: list[dict]) -> dict | None:
    from verinoda import guards

    for f in c["_files"]:
        i = _match_line(lines, re.compile(re.escape(f)))
        if i:
            return {"at": f"{rel}:{i}", "via": f"names {f}"}
        for g in guards_:
            if not _about(c, g):
                continue
            for glob in g["allowed"] if isinstance(g["allowed"], list) else [g["allowed"]]:
                if isinstance(glob, str) and guards.glob_match(f, glob):
                    # cite the guard's own line (its spec), not the first line that happens to hold the glob
                    i = (_match_line(lines, re.compile(re.escape(g["spec"]))) if g.get("spec") else None) or \
                        _match_line(lines, re.compile(re.escape(glob)))
                    return {"at": f"{rel}:{i or 1}",
                            "via": f"guard {g.get('id') or 'only_in'} allows only {glob}, which matches {f}"}
    for n in c["_names"]:
        i = _name_line(lines, n)
        if i:
            return {"at": f"{rel}:{i}", "via": f"names {n}"}
    return None


def _coverage(repo: Path, cands: list[dict], directory: str | None) -> tuple[dict[str, list[dict]], dict]:
    from verinoda import decisions as dm
    from verinoda.snapshot import list_files

    ddir = dm.decisions_dir(repo, directory)
    recs = [d for d in dm.load_all(repo, directory) if d.status in LIVE_STATUSES and d.path]
    docs = dm.adr_like_files(repo, list_files(repo), skip=ddir.resolve() if ddir.is_dir() else None)
    rec_paths = {d.path.resolve() for d in recs}
    sources: list[tuple[str | None, str, list[str], list[dict]]] = []
    for d in recs:
        rel = d.path.resolve().relative_to(Path(repo).resolve()).as_posix() \
            if d.path.resolve().is_relative_to(Path(repo).resolve()) else str(d.path)
        sources.append((d.id, rel, dbr._read_lines(Path(d.path).parent, d.path.name), _guards(d)))
    for rel in docs:
        if (Path(repo) / rel).resolve() not in rec_paths:
            sources.append((None, rel, dbr._read_lines(repo, rel), []))
    out: dict[str, list[dict]] = {}
    for c in cands:
        for rid, rel, lines, gs in sources:
            hit = _covers(c, rel, lines, gs)
            if hit:
                out.setdefault(c["id"], []).append({**({"record": rid} if rid else {"document": rel}), **hit})
    return out, {"records": len(recs), "documents": len(sources) - len(recs)}


# -- dismissals -------------------------------------------------------------------------------------------

def dismissed_path(repo: Path) -> Path:
    from verinoda.paths import atlas_dir

    return atlas_dir(repo) / FILE_NAME


def load_dismissed(repo: Path) -> list[dict]:
    p = dismissed_path(repo)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise UndocumentedError(f"{p.name} cannot be read: {exc}") from None
    if not isinstance(data, dict) or data.get("verinoda-dismissed") != FORMAT or \
            not isinstance(data.get("entries"), list) or \
            not all(isinstance(e, dict) and isinstance(e.get("id"), str) for e in data["entries"]):
        raise UndocumentedError(f"{p.name} is not a Verinoda dismissal list (format {FORMAT})")
    return data["entries"]


def _save_dismissed(repo: Path, entries: list[dict]) -> None:
    from verinoda.paths import ensure_atlas

    ensure_atlas(repo)  # .verinoda/.gitignore keeps the user's reasons out of their commits
    p = dismissed_path(repo)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"verinoda-dismissed": FORMAT, "entries": entries}, ensure_ascii=False, indent=1)
                   + "\n", encoding="utf-8", newline="\n")
    tmp.replace(p)


# -- the command ------------------------------------------------------------------------------------------

def _all_candidates(repo: Path) -> tuple[list[dict], int]:
    pb = dbr._Probe(repo)
    if len(pb.product) < MIN_PRODUCT_FILES:
        return [], len(pb.product)
    pb.storage()
    pb.config([])
    pb.dependencies([])
    return [*_storage(pb), *_libraries(pb), *_config(pb)], len(pb.product)


def _public(c: dict) -> dict:
    return {k: v for k, v in c.items() if not k.startswith("_")}


def find(repo: Path, directory: str | None = None) -> dict:
    """The candidates no live record or ADR covers and the user has not dismissed; the covered and the
    dismissed ones are listed apart, each with what covers it or the user's reason."""
    repo = Path(repo).resolve()
    cands, n_product = _all_candidates(repo)
    covered, searched = _coverage(repo, cands, directory)
    dismissed = load_dismissed(repo)
    gone = {e["id"] for e in dismissed}
    ids = {c["id"] for c in cands}
    return {
        "candidates": [_public(c) for c in cands if c["id"] not in covered and c["id"] not in gone],
        "covered": [{"id": c["id"], "candidate": c["candidate"], "by": covered[c["id"]][:3]}
                    for c in cands if c["id"] in covered],
        "dismissed": [{**e, "current": e["id"] in ids} for e in dismissed],
        "searched": {**searched, "product_files": n_product,
                     "probes": ["storage sinks", "declared Python dependencies and their imports",
                                "environment reads"]},
        "limits": LIMITS,
    }


def dismiss(repo: Path, cid: str, reason: str | None, *, undo: bool = False, directory: str | None = None) -> dict:
    """Record the user's dismissal of a listed candidate (``undo``: remove it) in the local dismissal list."""
    repo = Path(repo).resolve()
    entries = load_dismissed(repo)
    if undo:
        left = [e for e in entries if e["id"] != cid]
        if len(left) == len(entries):
            raise UndocumentedError(f"{cid} is not dismissed")
        _save_dismissed(repo, left)
        return {"id": cid, "undone": True, "file": dismissed_path(repo).as_posix()}
    if not (reason or "").strip():
        raise UndocumentedError("a dismissal needs --reason: the user's reason, in their words")
    if any(e["id"] == cid for e in entries):
        raise UndocumentedError(f"{cid} is already dismissed")
    res = find(repo, directory)
    cand = next((c for c in res["candidates"] if c["id"] == cid), None)
    if cand is None:
        listed = ", ".join(c["id"] for c in res["candidates"]) or "none"
        raise UndocumentedError(f"{cid} is not a current candidate (candidates: {listed})")
    entry = {"id": cid, "reason": reason.strip(), "date": date.today().isoformat(), "fact": cand["fact"]["text"]}
    _save_dismissed(repo, [*entries, entry])
    return {**entry, "file": dismissed_path(repo).as_posix()}
