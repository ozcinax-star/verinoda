"""Ask before writing a dependency: may ``source`` depend on ``target`` under the accepted decision guards?

``verinoda decide ask SOURCE TARGET`` and MCP ``dependency_ask`` judge a proposed dependency - one not written
yet - against the same rules ``decide check`` enforces (:mod:`verinoda.guards`): every accepted guard of every
enforced decision record. ``source`` is one project file (it may not exist yet). ``target`` is:

* a project path: an existing file or folder, a path ending in a code suffix, ``./x`` or ``../x`` (read from
  the source's folder, as an import writes it), or a path whose first folder is one of the project's
  (``web/ui/utils``; a code suffix or ``/index.*`` is added when that names an existing file);
* a dotted name read from the repository root, ``src/``, ``lib/`` or a JVM source root (``src/main/java/``):
  the longest leading part that names a module or class file (``app.ui.views.show`` -> ``app/ui/views.py``)
  or a folder (``app.ui`` -> the namespace package ``app/ui``; ``app.ui.new`` -> the new path
  ``app/ui/new``);
* else a package outside the project (``psycopg``, ``lodash/fp``, ``@angular/core``,
  ``github.com/pkg/errors``). A file elsewhere with the same base name (``app/api/logging.py`` for
  ``logging``) is not the target: Python would not import it by that name.

A typed path that differs from an existing file or folder only in letter case is read as that file.

Per guard kind:

* ``no_edge`` / ``layers`` / ``allow_edges`` / ``public``: the two paths are matched against the rule's globs
  (``tag:NAME`` read from :func:`verinoda.decisions.architecture_tags`) as ``check`` places a file: a file two
  layers match is in the higher one, a layer left with no file of its own is unknown, test files leave
  ``allow_edges`` / ``public`` unless ``scope=all``. A package outside the project is no node of the graph,
  so edge rules do not judge it (as in ``check``).
* ``only_in``: a target the rule's calls go through (``sqlite3`` for ``sqlite3.connect``; a ``pattern=`` that
  names a dotted call counts as that call) is ``restricted`` in a source the guard's scope holds
  (:func:`verinoda.guards.scope_files`) outside ``allowed``: the dependency itself may be fine, those calls are
  not. Another ``pattern=`` that names the target is ``unknown``: a text pattern is judged on code.
* ``dependency absent=NAME``: a target package of that name is forbidden anywhere (its npm package, Go module
  prefix, Maven artifact, first dotted part, or the distribution of a known import name: ``yaml`` ->
  PyYAML).

The verdict is ``forbidden`` when a rule forbids it, else ``restricted``, else ``unknown`` when a rule could
not be judged (an undefined tag, a record that cannot be read, a missing decisions folder, no accepted guard
at all), else ``allowed``: no accepted rule forbids it. Each rule that applies is cited at its record's line.
It judges paths and names against the records' text only: it reads no code and no graph, and a rule's
``relations`` are taken to cover the dependency proposed.
"""

from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path

from verinoda import decisions as dm
from verinoda import guards
from verinoda.testcode import is_test_file

FORBIDDEN, RESTRICTED, ALLOWS, WAIVED, UNKNOWN = "forbidden", "restricted", "allows", "waived", "unknown"
LIMITS = ["judged from the paths and names against the accepted guards' text: no code or graph is read, and a "
          "guard's relations are taken to cover this dependency",
          "only_in pattern= guards are judged only when the pattern names a call or the target; dependency "
          "present= guards are not judged for a proposal"]
_INDEX = ("index.ts", "index.tsx", "index.js", "index.jsx", "index.mjs", "index.cjs", "__init__.py")


class AskError(ValueError):
    """A source or target that cannot be judged (empty, a folder or glob as source, outside the repository)."""


class _Tree:
    """The project's files and folders, with a case-insensitive lookup."""

    def __init__(self, files: list[str]):
        self.files = set(files)
        self.dirs = {str(p) for f in files for p in _parents(f)}
        self._low: dict[str, str | None] = {}
        for x in (*self.files, *self.dirs):
            k = x.lower()
            self._low[k] = x if k not in self._low or self._low[k] == x else None  # None: ambiguous

    def real(self, rel: str) -> str:
        """``rel`` in the letter case of the existing file or folder it names (its longest existing folder
        for a new file), else as typed."""
        if rel in self.files or rel in self.dirs:
            return rel
        parts = rel.split("/")
        for k in range(len(parts), 0, -1):
            hit = self._low.get("/".join(parts[:k]).lower())
            if hit:
                return "/".join([hit, *parts[k:]])
        return rel


def _parents(rel: str) -> list[str]:
    parts = rel.split("/")[:-1]
    return ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]


def _rel(repo: Path, value: str, what: str) -> str:
    v = str(value or "").strip().replace("\\", "/")
    p = Path(v)
    if p.is_absolute():
        try:
            v = p.resolve().relative_to(Path(repo).resolve()).as_posix()
        except ValueError:
            raise AskError(f"{what} {value!r} is outside the repository {repo}") from None
    try:
        return dm.rel_path(repo, v, what).rstrip("/")
    except dm.DecisionError as exc:
        raise AskError(str(exc)) from None


def _modules(rel: str) -> set[str]:
    """The dotted names a Python file is imported by (with and without a leading src/ or lib/)."""
    m = guards._module_of(rel)
    if not m:
        return set()
    out = {m}
    head, _, rest = m.partition(".")
    if head in ("src", "lib") and rest:
        out.add(rest)
    return out


def _roots(files: set[str]) -> list[str]:
    """The folders a dotted name is read from: the root, src/, lib/ and each JVM source root."""
    roots = {"", "src/", "lib/"}
    for f in files:
        m = guards._SOURCE_ROOT.search(f)
        if m:
            roots.add(f[:m.end()])
    return sorted(roots)


def _file(rel: str, tree: _Tree, **extra) -> dict:
    return {"kind": "file", "path": rel, "names": sorted(_modules(rel)),
            **({} if rel in tree.files or rel in tree.dirs else {"new": True}), **extra}


def _with_suffix(rel: str, tree: _Tree) -> list[str]:
    """The existing files an import path without its suffix names (``web/ui/utils`` -> ``web/ui/utils.ts``)."""
    if rel in tree.files:
        return [rel]
    out = [rel + s for s in guards.CODE_SUFFIXES if rel + s in tree.files]
    return out or [f"{rel}/{i}" for i in _INDEX if f"{rel}/{i}" in tree.files]


def resolve_target(repo: Path, target: str, files: list[str] | _Tree, source: str = "") -> dict:
    """``{"kind": "file", "path", "names"}`` for a project path or a dotted name that names one (see the
    module), else ``{"kind": "package", "names"}``; ``names`` are the dotted names calls go through.
    ``guessed``: the path is read from a dotted name that also names other files, or names no file yet."""
    tree = files if isinstance(files, _Tree) else _Tree(list(files))
    t = str(target or "").strip().replace("\\", "/")
    if not t:
        raise AskError("target is empty: a project file, a module or a package name")
    if t in (".", "..") or t.startswith(("./", "../")):
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(source), t))
        if joined in (".", "..") or joined.startswith("../"):
            raise AskError(f"target {target!r} from {source or 'the root'} is outside the repository")
        t = joined
    first = tree.real(t.split("/")[0]) if "/" in t else None
    absolute = Path(t).is_absolute() or t.startswith("/") or re.match(r"^[A-Za-z]:/", t) is not None
    if absolute or tree.real(t) in tree.files or tree.real(t) in tree.dirs or t.endswith(guards.CODE_SUFFIXES) \
            or (first is not None and first in tree.dirs):
        rel = tree.real(_rel(repo, t, "target"))
        hits = _with_suffix(rel, tree)
        if hits:
            return _file(hits[0], tree, **({"also": hits[1:5]} if len(hits) > 1 else {}))
        return _file(rel, tree)
    if "/" in t or not re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*", t):
        return {"kind": "package", "names": [t]}
    parts = t.split(".")
    roots = _roots(tree.files)
    sufs = (".py", "/__init__.py", ".pyi", *guards.JVM_SUFFIXES)
    for k in range(len(parts), 0, -1):
        stem = "/".join(parts[:k])
        cands = [r + stem + s for r in roots for s in sufs if r + stem + s in tree.files]
        if cands:
            names = sorted(_modules(cands[0]) | {t})
            return {"kind": "file", "path": cands[0], "names": names,
                    **({"also": cands[1:5], "guessed": True} if len(cands) > 1 else {})}
        folders = [r + stem for r in roots if r + stem in tree.dirs]
        if folders:
            path = "/".join([folders[0], *parts[k:]])
            return {"kind": "file", "path": path, "names": [t],
                    **({} if k == len(parts) else {"new": True, "guessed": True}),
                    **({"also": folders[1:5], "guessed": True} if len(folders) > 1 else {})}
    return {"kind": "package", "names": [t]}


def _record_at(repo: Path, d: dm.Decision, g: dict) -> str | None:
    """``path:line`` of the guard in its record (the front matter's guards line), repository-relative."""
    if d.path is None:
        return None
    try:
        rel = Path(d.path).resolve().relative_to(Path(repo).resolve()).as_posix()
        lines = Path(d.path).read_text(encoding="utf-8").split("\n")
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    spec = str(g.get("spec") or "")
    for needle in (json.dumps(spec, ensure_ascii=False)[1:-1], spec, "guards:"):
        for i, ln in enumerate(lines, 1):
            if needle and needle in ln:
                return f"{rel}:{i}"
    return rel


class _Globs:
    def __init__(self, repo: Path, files: set[str] | None = None):
        self.repo = repo
        self.code = sorted(f for f in files or () if f.endswith(guards.CODE_SUFFIXES))
        self._tags: tuple | None = None

    def expand(self, pattern: str) -> list[str] | str:
        """The globs a pattern stands for, or why it cannot be read (an undefined tag)."""
        if not pattern.startswith(dm.TAG_PREFIX):
            return [pattern]
        if self._tags is None:
            self._tags = dm.architecture_tags(self.repo)
        tags, where, problems = self._tags
        name = pattern[len(dm.TAG_PREFIX):]
        if name not in tags:
            return (f"{pattern}: no tag {name!r} in " + (", ".join(where) if where else "verinoda.toml "
                    "[architecture.tags]") + (f" ({'; '.join(problems)})" if problems else ""))
        return tags[name]

    def match(self, path: str, pattern: str) -> bool | str:
        globs = self.expand(pattern)
        if isinstance(globs, str):
            return globs
        return any(guards.glob_match(path, x) for x in globs)


def _judge_edge(gl: _Globs, g: dict, src: str, tgt: dict) -> tuple[str, str] | None:
    """(verdict, why) for an architecture rule, or None when it does not apply."""
    kind = g["kind"]
    if tgt["kind"] != "file":
        return None  # a package outside the project is no node: edge rules do not judge it
    dst = tgt["path"]

    def m(path: str, pat: str) -> bool:
        r = gl.match(path, pat)
        if isinstance(r, str):
            raise _Unknown(r)
        return r

    if kind == "no_edge":
        if m(src, g["from"]) and m(dst, g["to"]):
            return FORBIDDEN, f"no dependency from {g['from']} to {g['to']}"
        return None
    if kind == "layers":
        order = list(g["order"])

        def layer(p: str) -> int | None:
            return next((i for i, pat in enumerate(order) if m(p, pat)), None)
        i, j = layer(src), layer(dst)
        if i is None or j is None:
            return None
        # as check: a layer whose every file a higher layer's glob also matches holds no file of its own
        placed = {f: layer(f) for f in gl.code}
        for k, pat in enumerate(order):
            if any(m(f, pat) for f in gl.code) and k not in placed.values():
                raise _Unknown(f"layer {k + 1}={pat} has no file of its own: each file it matches is in a higher "
                               "layer, so the order cannot be judged at it")
        if j < i:
            return FORBIDDEN, f"layer {i + 1} ({order[i]}) may not depend on layer {j + 1} ({order[j]}) above it"
        return ALLOWS, f"layer {i + 1} ({order[i]}) may use layer {j + 1} ({order[j]})"
    tests_out = g.get("scope") != "all"
    if kind == "allow_edges":
        if not m(src, g["from"]) or (tests_out and is_test_file(src)):
            return None
        if m(dst, g["from"]) or any(m(dst, a) for a in g["allowed"]):
            return ALLOWS, f"{g['from']} may use itself and {', '.join(g['allowed'])}"
        return FORBIDDEN, f"{g['from']} may use only itself and {', '.join(g['allowed'])}"
    if kind == "public":
        if not m(dst, g["module"]) or m(src, g["module"]) or (tests_out and is_test_file(src)):
            return None
        if any(m(dst, a) for a in g["api"]):
            return ALLOWS, f"{dst} is a public file of {g['module']}"
        return FORBIDDEN, f"{g['module']} is reached only through {', '.join(g['api'])}"
    return None


class _Unknown(Exception):
    pass


def _names_in(pattern: str, names: list[str]) -> bool:
    """Whether a text pattern names the target (its first or last dotted part as a word)."""
    text = pattern.replace("\\.", ".")
    parts = {x for n in names for x in (n.split(".")[0], n.rpartition(".")[2], n.split("/")[-1]) if len(x) > 1}
    return any(re.search(r"(?<![\w$])" + re.escape(x) + r"(?![\w$])", text) for x in parts)


def _judge_only_in(repo: Path, g: dict, src: str, tgt: dict, ddir: Path | None) -> tuple[str, str] | None:
    pattern = g.get("pattern")
    call = guards.dotted_call(pattern) if pattern else None
    calls = [call] if call else [] if pattern else list(g.get("calls") or guards.SINK_CALLS.get(g.get("sink"), ()))
    hit = []
    for c in calls:
        for n in tgt["names"]:
            last = n.rpartition(".")[2]
            if c == n or c.startswith(n + ".") or (last[:1].isupper() and c.startswith(last + ".")):
                hit.append(c)
                break
    if not hit and not (pattern and not call and _names_in(pattern, tgt["names"])):
        return None
    allowed = g.get("allowed") or []
    if any(guards.glob_match(src, a) for a in allowed):
        return ALLOWS, f"{src} is one of the files allowed to call {', '.join(hit) or f'/{pattern}/'}"
    if not guards.scope_files(repo, g, [src], ddir)[0]:
        return None  # out of the guard's scope (tests, samples, reference trees, exclude, no code), as for check
    if not hit:
        return UNKNOWN, (f"/{pattern}/ names {tgt['names'][0]} and may be written only in {', '.join(allowed)}: "
                         "a text pattern is judged on the code (decide check after writing it)")
    return RESTRICTED, f"{', '.join(hit)} may be called only in {', '.join(allowed)}"


def _package_keys(name: str) -> set[str]:
    """The dependency names a package target stands for: itself, its npm package (``lodash/fp`` -> lodash,
    ``@angular/core``), Go module prefixes, a Maven artifact, its dotted prefixes and their distributions."""
    from verinoda.depcheck import PY_ALIASES

    keys = {name, name.split(":")[-1]}
    if "/" in name:
        segs = name.split("/")
        if name.startswith("@"):
            keys.add("/".join(segs[:2]))
        elif "." in segs[0]:
            keys.update("/".join(segs[:i]) for i in range(2, len(segs)))
        else:
            keys.add(segs[0])
    elif ":" not in name:
        parts = name.split(".")
        for i in range(1, len(parts) + 1):
            pre = ".".join(parts[:i])
            keys.add(pre)
            if pre in PY_ALIASES:
                keys.add(PY_ALIASES[pre])
    return {guards._dep_key(k) for k in keys if k}


def _judge_dependency(g: dict, tgt: dict) -> tuple[str, str] | None:
    if not g.get("absent") or tgt["kind"] != "package":
        return None
    if guards._dep_key(g["absent"]) in _package_keys(tgt["names"][0]):
        return FORBIDDEN, f"{g['absent']} may not be a dependency of the project"
    return None


def ask(repo: Path, source: str, target: str, *, records: list | None = None,
        decisions_dir: str | None = None) -> dict:
    """The verdict on ``source`` depending on ``target`` and every accepted rule that applies (see the module)."""
    from verinoda.snapshot import list_files

    repo = Path(repo).resolve()
    tree = _Tree(list_files(repo))
    src = _rel(repo, source, "source")
    if any(c in src for c in "*?["):
        raise AskError(f"source {source!r} is a glob: give one file")
    src = tree.real(src)
    if src in ("", ".") or src in tree.dirs or (repo / src).is_dir():
        raise AskError(f"source {source!r} is a folder: give the one file that would depend")
    tgt = resolve_target(repo, target, tree, src)
    try:
        ddir, ddir_from = dm.decisions_dir_source(repo, decisions_dir)
    except dm.DecisionError as exc:
        raise AskError(str(exc)) from None
    recs = records if records is not None else dm.load_all(repo, decisions_dir)
    gl = _Globs(repo, tree.files)
    rules: list[dict] = []
    guards_read = not_applicable = 0
    absent_read = False
    unreadable: list[str] = []
    guessed = bool(tgt.get("guessed"))
    for d in recs:
        if not d.enforced:
            if d.problems:
                unreadable.append(d.id)
            continue
        for g in d.guards:
            if g.get("status") != "accepted":
                continue
            guards_read += 1
            try:
                if g["kind"] in dm.EDGE_KINDS:
                    got = _judge_edge(gl, g, src, tgt)
                elif g["kind"] == "only_in":
                    got = _judge_only_in(repo, g, src, tgt, ddir)
                elif g["kind"] == "dependency":
                    absent_read = absent_read or bool(g.get("absent"))
                    got = _judge_dependency(g, tgt)
                else:
                    got = None
            except _Unknown as u:
                got = (UNKNOWN, str(u))
            except (KeyError, TypeError) as exc:  # a hand-edited guard missing a key: never a silent allow
                got = (UNKNOWN, f"the guard cannot be read: {type(exc).__name__}: {exc}"[:200])
            if got is None:
                not_applicable += 1
                continue
            verdict, why = got
            if verdict in (FORBIDDEN, RESTRICTED) and dm.waived(d, g["id"], src, None) is not None:
                verdict, why = WAIVED, f"{why} (waived for {src} in {d.id})"
            edge = g["kind"] in dm.EDGE_KINDS
            rules.append({"decision": d.id, "guard": g["id"], "kind": g["kind"], "spec": g.get("spec") or g["kind"],
                          "verdict": verdict, "why": why, "evidence": _record_at(repo, d, g),
                          "status": "unknown" if verdict == UNKNOWN else
                          "strong_inference" if edge and guessed else "statically_verified"})
    order = {FORBIDDEN: 0, RESTRICTED: 1, UNKNOWN: 2, WAIVED: 3, ALLOWS: 4}
    rules.sort(key=lambda r: order[r["verdict"]])
    seen = {r["verdict"] for r in rules}
    limits = list(LIMITS)
    reasons = []
    if unreadable:
        reasons.append(f"{len(unreadable)} decision record(s) cannot be read: {', '.join(unreadable[:5])}")
    if records is None and not recs and ddir_from != dm.DEFAULT_SOURCE and not ddir.is_dir():
        reasons.append(f"the decisions folder {ddir} ({ddir_from}) does not exist, so no decision record was read")
    elif not guards_read:
        reasons.append("no accepted guard in any enforced decision record: nothing rules on it")
    if tgt["kind"] == "package":
        limits.append(f"{tgt['names'][0]} is not a project file: edge rules (no_edge, layers, allow_edges, "
                      "public) do not judge a package outside the project")
        if absent_read and "dependency" not in {r["kind"] for r in rules}:
            limits.append("dependency absent= is matched by the manifest name; an import named otherwise is "
                          "matched only for known pairs (yaml: PyYAML)")
    if tgt.get("new"):
        limits.append(f"{tgt['path']} is not among the project's files yet: judged by its path")
    if tgt.get("also"):
        limits.append(f"{target} also names {', '.join(tgt['also'])}; {tgt['path']} was judged")
    verdict = (FORBIDDEN if FORBIDDEN in seen else RESTRICTED if RESTRICTED in seen else
               UNKNOWN if UNKNOWN in seen or reasons else "allowed")
    res = {"verdict": verdict, "source": src, "target": target,
           "target_kind": tgt["kind"], **({"target_path": tgt["path"]} if tgt["kind"] == "file" else {}),
           "rules": rules,
           "scope": {"decisions": len(recs), "guards": guards_read, "not_applicable": not_applicable},
           "limits": limits}
    if reasons:
        res["unknown"] = reasons
    res["next_step"] = {
        FORBIDDEN: "do not write it: choose another design, or ask the user whether the decision should change "
                   "(never edit, waive or supersede a decision on your own)",
        RESTRICTED: "the dependency may stay, but the named calls belong only in the allowed files",
        UNKNOWN: "a rule could not be judged (see rules and unknown): ask the user before relying on it",
        "allowed": "no accepted rule forbids it; decide check after writing it still checks the code",
    }[verdict]
    res["exit"] = exit_code(res)
    return res


def exit_code(res: dict) -> int:
    """1 forbidden, 3 unknown, 0 allowed or restricted."""
    return 1 if res["verdict"] == FORBIDDEN else 3 if res["verdict"] == UNKNOWN else 0
