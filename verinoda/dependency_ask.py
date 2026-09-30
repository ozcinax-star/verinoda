"""Ask before writing a dependency: may ``source`` depend on ``target`` under the accepted decision guards?

``verinoda decide ask SOURCE TARGET`` and MCP ``dependency_ask`` judge a proposed dependency - one not written
yet - against the same rules ``decide check`` enforces (:mod:`verinoda.guards`): every accepted guard of every
enforced decision record. ``source`` is a project path (the file may not exist yet); ``target`` is a project
file, a Python module that names one (``app.db.store``), or a package outside the project (``psycopg``).

Per guard kind:

* ``no_edge`` / ``layers`` / ``allow_edges`` / ``public``: the two paths are matched against the rule's globs
  (``tag:NAME`` read from :func:`verinoda.decisions.architecture_tags`) as ``check`` places a file: a file two
  layers match is in the higher one, test files leave ``allow_edges`` / ``public`` unless ``scope=all``. A
  package outside the project is no node of the graph, so edge rules do not judge it (as in ``check``).
* ``only_in``: a target the rule's calls go through (``sqlite3`` for ``sqlite3.connect``) is ``restricted``
  in a file outside ``allowed``: the dependency itself may be fine, those calls are not.
* ``dependency absent=NAME``: a target package of that name is forbidden anywhere.

The verdict is ``forbidden`` when a rule forbids it, else ``restricted``, else ``unknown`` when a rule could
not be judged (an undefined tag, a record that cannot be read, no accepted guard at all), else ``allowed``:
no accepted rule forbids it. Each rule that applies is cited at its record's line. It judges paths and
names against the records' text only: it reads no code and no graph, and a rule's ``relations`` are taken to
cover the dependency proposed.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from verinoda import decisions as dm
from verinoda import guards
from verinoda.testcode import is_test_file

FORBIDDEN, RESTRICTED, ALLOWS, WAIVED, UNKNOWN = "forbidden", "restricted", "allows", "waived", "unknown"
LIMITS = ["judged from the paths and names against the accepted guards' text: no code or graph is read, and a "
          "guard's relations are taken to cover this dependency",
          "only_in pattern= guards and dependency present= guards are not judged for a proposal"]


class AskError(ValueError):
    """A source or target that cannot be judged (empty, outside the repository)."""


def _rel(repo: Path, value: str, what: str) -> str:
    v = str(value or "").strip().replace("\\", "/")
    p = Path(v)
    if p.is_absolute():
        try:
            v = p.resolve().relative_to(Path(repo).resolve()).as_posix()
        except ValueError:
            raise AskError(f"{what} {value!r} is outside the repository {repo}") from None
    try:
        return dm.rel_path(repo, v, what)
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


def resolve_target(repo: Path, target: str, files: list[str]) -> dict:
    """``{"kind": "file", "path", "names"}`` for a project file (existing or path-like) or a Python module that
    names one, else ``{"kind": "package", "names"}``; ``names`` are the dotted names calls go through."""
    t = str(target or "").strip().replace("\\", "/")
    if not t:
        raise AskError("target is empty: a project file, a module or a package name")
    have = set(files)
    if t in have or "/" in t or t.endswith(guards.CODE_SUFFIXES) or Path(t).is_absolute():
        rel = _rel(repo, t, "target")
        return {"kind": "file", "path": rel, "names": sorted(_modules(rel)), **({} if rel in have else {"new": True})}
    stem = t.replace(".", "/")
    cands = [f for f in sorted(have) for c in (f"{stem}.py", f"{stem}/__init__.py", f"{stem}.pyi")
             if f == c or f.endswith("/" + c)]
    if cands:
        out = {"kind": "file", "path": cands[0], "names": sorted(_modules(cands[0]) | {t})}
        if len(cands) > 1:
            out["also"] = cands[1:5]
        return out
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
    def __init__(self, repo: Path):
        self.repo = repo
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


def _judge_only_in(repo: Path, g: dict, src: str, tgt: dict) -> tuple[str, str] | None:
    if g.get("pattern"):
        return None
    calls = list(g.get("calls") or guards.SINK_CALLS.get(g.get("sink"), ()))
    hit = []
    for c in calls:
        for n in tgt["names"]:
            last = n.rpartition(".")[2]
            if c == n or c.startswith(n + ".") or (last[:1].isupper() and c.startswith(last + ".")):
                hit.append(c)
                break
    if not hit:
        return None
    allowed = g.get("allowed") or []
    if any(guards.glob_match(src, a) for a in allowed):
        return ALLOWS, f"{src} is one of the files allowed to call {', '.join(hit)}"
    if any(guards.glob_match(src, x) for x in g.get("exclude") or []):
        return None
    if g.get("scope") != "all" and (is_test_file(src) or guards.not_product_dirs(repo, src)):
        return None  # out of the guard's default scope, as for check
    return RESTRICTED, f"{', '.join(hit)} may be called only in {', '.join(allowed)}"


def _judge_dependency(g: dict, tgt: dict) -> tuple[str, str] | None:
    if not g.get("absent") or tgt["kind"] != "package":
        return None
    key = guards._dep_key(g["absent"])
    name = tgt["names"][0]
    if key in {guards._dep_key(name), guards._dep_key(name.split(".")[0]), guards._dep_key(name.split(":")[-1])}:
        return FORBIDDEN, f"{g['absent']} may not be a dependency of the project"
    return None


def ask(repo: Path, source: str, target: str, *, records: list | None = None,
        decisions_dir: str | None = None) -> dict:
    """The verdict on ``source`` depending on ``target`` and every accepted rule that applies (see the module)."""
    from verinoda.snapshot import list_files

    repo = Path(repo).resolve()
    src = _rel(repo, source, "source")
    if any(c in src for c in "*?["):
        raise AskError(f"source {source!r} is a glob: give one file")
    files = list_files(repo)
    tgt = resolve_target(repo, target, files)
    recs = records if records is not None else dm.load_all(repo, decisions_dir)
    gl = _Globs(repo)
    rules: list[dict] = []
    guards_read = not_applicable = 0
    unreadable: list[str] = []
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
                    got = _judge_only_in(repo, g, src, tgt)
                elif g["kind"] == "dependency":
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
            rules.append({"decision": d.id, "guard": g["id"], "kind": g["kind"], "spec": g.get("spec") or g["kind"],
                          "verdict": verdict, "why": why, "evidence": _record_at(repo, d, g),
                          "status": "unknown" if verdict == UNKNOWN else "statically_verified"})
    order = {FORBIDDEN: 0, RESTRICTED: 1, UNKNOWN: 2, WAIVED: 3, ALLOWS: 4}
    rules.sort(key=lambda r: order[r["verdict"]])
    seen = {r["verdict"] for r in rules}
    limits = list(LIMITS)
    reasons = []
    if unreadable:
        reasons.append(f"{len(unreadable)} decision record(s) cannot be read: {', '.join(unreadable[:5])}")
    if not guards_read:
        reasons.append("no accepted guard in any enforced decision record: nothing rules on it")
    if tgt["kind"] == "package":
        limits.append(f"{tgt['names'][0]} is not a project file: edge rules (no_edge, layers, allow_edges, "
                      "public) do not judge a package outside the project")
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
    return res


def exit_code(res: dict) -> int:
    """1 forbidden, 3 unknown, 0 allowed or restricted."""
    return 1 if res["verdict"] == FORBIDDEN else 3 if res["verdict"] == UNKNOWN else 0

