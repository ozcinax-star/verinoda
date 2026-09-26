"""Minecraft datapacks: function calls, entity tags and scoreboard objectives (docs/DESIGN.md D52).

A mod with a datapack keeps half its behaviour in ``.mcfunction`` files: ``function ns:x`` and ``execute ... run
function ns:x`` call another function, ``schedule function ns:x 20t`` calls it later, ``#minecraft:tick`` runs a
function every tick. Entity tags (``tag @s add x``, ``@e[tag=x]``) and scoreboard objectives (``scoreboard players
set #m k 1``, ``execute if score #m k matches 0``) are state that mcfunction and Java share: Java adds and checks
the same tags (``addTag``, ``entityTags().contains``, often through a ``static final String`` constant) and reads
the same scores. A tag something checks but nothing adds, or a score something writes but nothing reads, is the
kind of bug neither side shows alone.

:func:`functions` finds the function files (``data/<ns>/function[s]/<path>.mcfunction`` outside build output);
:func:`parse_function` reads one; :func:`index` builds the cross-language index of tags and objectives;
:func:`problems` lists the two kinds of mismatch. Read from the text as written: a tag or objective name built at
run time (``"x" + i``, a macro ``$(name)``) is not seen, so a mismatch is a lead to check, not a proof.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_SKIP = {"build", ".gradle", "out", "bin", "node_modules", ".git", ".verinoda", "run", ".idea", "graphify-out"}
_FUNC_DIRS = ("function", "functions")
_ID = r"#?[a-z0-9_.-]+:[a-z0-9_./-]+"
_CALL = re.compile(rf"(?:^|\s)function\s+({_ID})")
_SCHEDULE = re.compile(rf"(?:^|\s)schedule\s+function\s+({_ID})\s+(\d+[tsd]?)")
_TAG_CMD = re.compile(r"(?:^|\s)tag\s+(\S+(?:\[[^\]]*\])?)\s+(add|remove)\s+([A-Za-z0-9_.+-]+)")
_SEL_TAG = re.compile(r"\btag\s*=\s*(!?)([A-Za-z0-9_.+-]+)")
_NBT_TAGS = re.compile(r"\bTags\s*:\s*\[([^\]]*)\]")
_SEL_SCORES = re.compile(r"\bscores\s*=\s*\{([^}]*)\}")
_OBJ_ADD = re.compile(r"(?:^|\s)scoreboard\s+objectives\s+add\s+([A-Za-z0-9_.+-]+)")
_OBJ_REMOVE = re.compile(r"(?:^|\s)scoreboard\s+objectives\s+remove\s+([A-Za-z0-9_.+-]+)")
_PLAYERS = re.compile(r"(?:^|\s)scoreboard\s+players\s+(set|add|remove|reset|get|operation|enable|display)\s+"
                      r"(\S+)(?:\s+([A-Za-z0-9_.+-]+))?(?:\s+(\S+)\s+(\S+)(?:\s+([A-Za-z0-9_.+-]+))?)?")
_IF_SCORE = re.compile(r"\b(?:if|unless)\s+score\s+\S+\s+([A-Za-z0-9_.+-]+)(?:\s+(?:[<>=]+)\s+\S+\s+([A-Za-z0-9_.+-]+))?")
_STORE_SCORE = re.compile(r"\bstore\s+(?:result|success)\s+score\s+\S+\s+([A-Za-z0-9_.+-]+)")

# Java: tags through the entity API, objectives by name
_J_TAG = re.compile(r"\.(addTag|removeTag|addScoreboardTag|removeScoreboardTag)\s*\(\s*([^()]*?)\s*\)")
_J_TAG_CHECK = re.compile(r"\.(?:entityTags|getTags|getScoreboardTags|getCommandTags)\s*\(\s*\)\s*\.\s*contains\s*\(\s*"
                          r"([^()]*?)\s*\)")
_J_STR_CONST = re.compile(r"\bstatic\s+final\s+String\s+([A-Z][A-Z0-9_]*)\s*=\s*\"([^\"\\]*)\"\s*;")
_J_STRING = re.compile(r"\"([A-Za-z0-9_.+-]{2,})\"")


@dataclass(frozen=True)
class Site:
    file: str
    line: int
    lang: str        # "mcfunction" or "java"
    kind: str        # tags: add / remove / check; objectives: define / write / read / use
    text: str

    @property
    def at(self) -> str:
        return f"{self.file}:{self.line}"


@dataclass
class Function:
    id: str
    files: list[str] = field(default_factory=list)
    calls: list[tuple[int, str, str, str | None]] = field(default_factory=list)  # (line, target, how, delay)
    events: list[str] = field(default_factory=list)                              # #minecraft:tick ...


def _skipped(rel_parts) -> bool:
    return any(p in _SKIP for p in rel_parts)


def function_id(path: Path) -> str | None:
    """``.../data/<ns>/function[s]/<a/b>.mcfunction`` -> ``ns:a/b``."""
    parts = path.as_posix().split("/")
    for i in range(len(parts) - 3, -1, -1):
        if parts[i] == "data" and i + 2 < len(parts) and parts[i + 2] in _FUNC_DIRS:
            rest = "/".join(parts[i + 3:])
            return f"{parts[i + 1]}:{rest[:-len('.mcfunction')]}" if rest.endswith(".mcfunction") else None
    return None


def datapack_root(path: Path) -> Path | None:
    parts = list(path.parts)
    for i in range(len(parts) - 3, -1, -1):
        if parts[i] == "data" and i + 2 < len(parts) and parts[i + 2] in _FUNC_DIRS:
            return Path(*parts[:i])
    return None


def resolve_call(root: Path, fid: str) -> Path | None:
    """The file of function ``ns:path`` in the datapack at ``root`` (``function`` or the older ``functions``)."""
    if fid.startswith("#") or ":" not in fid:
        return None
    ns, _, p = fid.partition(":")
    for d in _FUNC_DIRS:
        cand = root / "data" / ns / d / f"{p}.mcfunction"
        if cand.is_file():
            return cand
    return None


def tag_members(root: Path, tag: str) -> list[str]:
    """The functions a function tag (``#minecraft:tick``) lists in the datapack at ``root``."""
    ns, _, p = tag.lstrip("#").partition(":")
    for d in ("function", "functions"):
        f = root / "data" / ns / "tags" / d / f"{p}.json"
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        vals = data.get("values") if isinstance(data, dict) else None
        return [v.get("id") if isinstance(v, dict) else v for v in vals or [] if isinstance(v, (str, dict))]
    return []


def parse_function(text: str) -> tuple[list[tuple[int, str, str, str | None]], list[tuple[int, str, str]],
                                        list[tuple[int, str, str]]]:
    """``(calls, tag sites, objective sites)`` of one mcfunction's text: calls ``(line, target, how, delay)``, tag
    sites ``(line, name, add|remove|check)``, objective sites ``(line, name, define|write|read)``."""
    calls, tags, objs = [], [], []
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("$"):  # a macro line: its $(names) are filled at run time
            line = line[1:]
        for m in _SCHEDULE.finditer(line):
            calls.append((i, m.group(1), "schedule", m.group(2)))
        sched = {m.start(1) for m in _SCHEDULE.finditer(line)}
        for m in _CALL.finditer(line):
            if m.start(1) in sched:
                continue
            how = "execute run" if re.search(r"(?:^|\s)execute\s", line[:m.start()]) else "function"
            calls.append((i, m.group(1), how, None))
        for m in _TAG_CMD.finditer(line):
            tags.append((i, m.group(3), m.group(2)))
        for m in _NBT_TAGS.finditer(line):  # summon ... {Tags:["a","b"]}, data merge entity ... {Tags:[...]}
            for name in re.findall(r'"?([A-Za-z0-9_.+-]+)"?', re.sub(r'\$\([^)]*\)', '', m.group(1))):
                tags.append((i, name, "add"))
        if "$(" in line and re.search(r"(?:Tags\s*:\s*\[[^\]]*\$\(|\s(?:add|remove)\s+\$\()", line):
            tags.append((i, "*", "add"))  # a macro fills in the tag
        for m in _SEL_TAG.finditer(line):
            if m.group(2):
                tags.append((i, m.group(2), "check"))
        for m in _SEL_SCORES.finditer(line):
            for part in m.group(1).split(","):
                name = part.split("=", 1)[0].strip()
                if name:
                    objs.append((i, name, "read"))
        for m in _OBJ_ADD.finditer(line):
            objs.append((i, m.group(1), "define"))
        for m in _OBJ_REMOVE.finditer(line):
            objs.append((i, m.group(1), "define"))
        for m in _PLAYERS.finditer(line):
            verb, obj = m.group(1), m.group(3)
            if obj:
                objs.append((i, obj, "read" if verb in ("get", "display") else "write"))
            if verb == "operation" and m.group(6):
                objs.append((i, m.group(6), "read"))
        for m in _IF_SCORE.finditer(line):
            objs.append((i, m.group(1), "read"))
            if m.group(2):
                objs.append((i, m.group(2), "read"))
        for m in _STORE_SCORE.finditer(line):
            objs.append((i, m.group(1), "write"))
    return calls, tags, objs


def functions(repo: Path) -> dict[str, Function]:
    """Every datapack function of the repository by id (a datapack copied into two places lists both files)."""
    repo = Path(repo)
    out: dict[str, Function] = {}
    for p in sorted(repo.rglob("*.mcfunction")):
        rel = p.relative_to(repo)
        if _skipped(rel.parts[:-1]):
            continue
        fid = function_id(rel)
        if fid is None:
            continue
        fn = out.setdefault(fid, Function(fid))
        fn.files.append(rel.as_posix())
        if len(fn.files) == 1:
            try:
                fn.calls = parse_function(p.read_text(encoding="utf-8", errors="replace"))[0]
            except OSError:
                pass
            root = datapack_root(p)
            if root is not None:
                for ev in ("minecraft:tick", "minecraft:load"):
                    if fid in tag_members(root, ev):
                        fn.events.append("#" + ev)
    return out


def _java_constants(texts: dict[str, str]) -> dict[str, str]:
    """``NAME`` -> value of every ``static final String NAME = "value";`` of the project (by simple name; a name
    two classes give different values is dropped)."""
    seen: dict[str, set[str]] = {}
    for t in texts.values():
        for m in _J_STR_CONST.finditer(t):
            seen.setdefault(m.group(1), set()).add(m.group(2))
    return {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}


def _java_value(expr: str, consts: dict[str, str]) -> str | None:
    e = expr.strip()
    m = re.fullmatch(r"\"([^\"\\]*)\"", e)
    if m:
        return m.group(1)
    m = re.fullmatch(r"(?:[A-Za-z_$][\w$]*\.)*([A-Z][A-Z0-9_]*)", e)
    return consts.get(m.group(1)) if m else None


_READ_HINT = re.compile(r"getPlayerScoreInfo|getScore\b|\.value\(\)|getOrCreatePlayerScore\([^)]*\)\s*\.\s*get\b"
                        r"|\bget\(\)")
_WRITE_HINT = re.compile(r"\.set\(|setScore|\.add\(|increment|resetSinglePlayerScore|resetAllPlayerScores")


def index(repo: Path, *, java_files: list[str] | None = None) -> dict:
    """``{"functions", "tags": {name: [Site]}, "objectives": {name: [Site]}}`` over the datapacks and the Java
    sources (``java_files``, default every ``.java`` outside build output)."""
    repo = Path(repo)
    funcs = functions(repo)
    tags: dict[str, list[Site]] = {}
    objs: dict[str, list[Site]] = {}
    for fn in funcs.values():
        f = fn.files[0]
        try:
            lines = (repo / f).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        _c, ts, os_ = parse_function("\n".join(lines))
        for ln, name, kind in ts:
            tags.setdefault(name, []).append(Site(f, ln, "mcfunction", kind, lines[ln - 1].strip()[:160]))
        for ln, name, kind in os_:
            objs.setdefault(name, []).append(Site(f, ln, "mcfunction", kind, lines[ln - 1].strip()[:160]))
    if java_files is None:
        java_files = [p.relative_to(repo).as_posix() for p in repo.rglob("*.java")
                      if not _skipped(p.relative_to(repo).parts[:-1])]
    texts: dict[str, str] = {}
    for f in java_files:
        try:
            texts[f] = (repo / f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    consts = _java_constants(texts)
    known_objs = set(objs)
    helpers = _tag_helpers(texts)
    for f, t in texts.items():
        lines = t.splitlines()
        starts = [0]
        for k, ch in enumerate(t):
            if ch == "\n":
                starts.append(k + 1)

        def line_of(pos: int) -> int:
            import bisect
            return bisect.bisect_right(starts, pos)

        for m in _J_TAG.finditer(t):
            name = _java_value(m.group(2), consts)
            if name:
                ln = line_of(m.start())
                kind = "add" if m.group(1).startswith("add") else "remove"
                tags.setdefault(name, []).append(Site(f, ln, "java", kind, lines[ln - 1].strip()[:160]))
        for m in _J_TAG_CHECK.finditer(t):
            name = _java_value(m.group(1), consts)
            if name:
                ln = line_of(m.start())
                tags.setdefault(name, []).append(Site(f, ln, "java", "check", lines[ln - 1].strip()[:160]))
        if helpers:  # the project's own tag helpers: `tag(e, "x")`, `etiketle(e, HURDA_ETIKET)`
            for m in re.finditer(r"(?<![\w$.])([a-z][\w$]*)\s*\(([^()]*)\)", t):
                h = helpers.get(m.group(1))
                args = m.group(2).split(",")
                if not h or h[1] >= len(args):
                    continue
                name = _java_value(args[h[1]], consts)
                if name:
                    ln = line_of(m.start())
                    tags.setdefault(name, []).append(Site(f, ln, "java", h[0], lines[ln - 1].strip()[:160]))
        # commands written as strings in Java (run through the server's command dispatcher)
        for m in re.finditer(r'"((?:[^"\\\n]|\\.){6,})"', t):
            s = m.group(1).replace('\\"', '"')
            if not re.search(r"(?:^|\s)(?:tag|scoreboard|summon|execute|function|data)\s", s):
                continue
            ln = line_of(m.start())
            _c, ts, os_ = parse_function(s)
            for _l, name, kind in ts:
                tags.setdefault(name, []).append(Site(f, ln, "java", kind, lines[ln - 1].strip()[:160]))
            for _l, name, kind in os_:
                objs.setdefault(name, []).append(Site(f, ln, "java", kind, lines[ln - 1].strip()[:160]))
        if not known_objs:
            continue
        # an objective named in a call: `helper(server, "k_durum")`, `sb.getObjective("k")`; read or write by what
        # the called method (in this file) or the enclosing statement does with it
        for m in _J_STRING.finditer(t):
            name = m.group(1)
            if name not in known_objs:
                continue
            ln = line_of(m.start())
            stmt = lines[ln - 1]
            call = re.search(r"([A-Za-z_$][\w$]*)\s*\([^()]*$", t[max(0, m.start() - 200):m.start()])
            kind = "use"
            if call:
                body = _method_body(t, call.group(1))
                probe = body if body is not None else stmt
                if _WRITE_HINT.search(probe):
                    kind = "write"
                elif _READ_HINT.search(probe):
                    kind = "read"
            objs.setdefault(name, []).append(Site(f, ln, "java", kind, stmt.strip()[:160]))
    return {"functions": funcs, "tags": tags, "objectives": objs}


def _tag_helpers(texts: dict[str, str]) -> dict[str, tuple[str, int]]:
    """Methods of the project that add, remove or check an entity tag they are given: name -> (kind, index of the
    tag parameter); a name two methods use differently is dropped."""
    out: dict[str, set[tuple[str, int]]] = {}
    decl = re.compile(r"\b(?:static\s+)?(?:boolean|void|[A-Z][\w<>]*)\s+([a-z][\w$]*)\s*\(([^)]*String[^)]*)\)\s*\{")
    for text in texts.values():
        for m in decl.finditer(text):
            body = _method_body(text, m.group(1)) or ""
            if len(body) > 1500:
                continue
            params = [(k, p.split()[-1]) for k, p in enumerate(m.group(2).split(",")) if "String" in p and p.split()]
            for k, prm in params:
                if re.search(rf"\.(?:addTag|addScoreboardTag)\s*\(\s*{re.escape(prm)}\s*\)", body):
                    out.setdefault(m.group(1), set()).add(("add", k))
                elif re.search(rf"\.(?:removeTag|removeScoreboardTag)\s*\(\s*{re.escape(prm)}\s*\)", body):
                    out.setdefault(m.group(1), set()).add(("remove", k))
                elif re.search(rf"(?:entityTags|getTags|getScoreboardTags)\s*\(\s*\)\s*\.\s*contains\s*\(\s*"
                               rf"{re.escape(prm)}\s*\)", body):
                    out.setdefault(m.group(1), set()).add(("check", k))
    return {k: next(iter(v)) for k, v in out.items() if len(v) == 1}


def _method_body(text: str, name: str) -> str | None:
    """The body of method ``name`` declared in ``text`` (brace-matched), or None."""
    m = re.search(rf"\b[\w<>\[\], ]+\s+{re.escape(name)}\s*\([^)]*\)\s*(?:throws [\w., ]+)?\{{", text)
    if not m:
        return None
    depth, i = 0, m.end() - 1
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[m.end():i]
        i += 1
    return None


def problems(ix: dict) -> dict:
    """Tags something checks that nothing adds, tags added that nothing checks, objectives written that nothing
    reads (a Java ``use`` of an objective counts as a possible read), and calls to functions that do not exist."""
    out: dict[str, list] = {"tags_checked_never_added": [], "tags_added_never_checked": [],
                            "scores_written_never_read": [], "missing_functions": []}
    macro = [s.at for s in ix["tags"].get("*", [])]
    if macro:  # tags a macro fills in cannot be matched: said, not guessed
        out["tags_added_by_macros"] = macro[:10]
    for name, sites in sorted(ix["tags"].items()):
        if name == "*":
            continue
        kinds = {s.kind for s in sites}
        if "check" in kinds and "add" not in kinds:
            out["tags_checked_never_added"].append({"name": name, "sites": [s.at for s in sites if s.kind == "check"]})
        elif "add" in kinds and "check" not in kinds:
            out["tags_added_never_checked"].append({"name": name, "sites": [s.at for s in sites if s.kind == "add"]})
    for name, sites in sorted(ix["objectives"].items()):
        kinds = {s.kind for s in sites}
        if "write" in kinds and not kinds & {"read", "use"}:
            out["scores_written_never_read"].append({"name": name,
                                                     "sites": [s.at for s in sites if s.kind == "write"][:10]})
    funcs = ix["functions"]
    for fid, fn in sorted(funcs.items()):
        for ln, target, how, _d in fn.calls:
            if not target.startswith("#") and target not in funcs:
                out["missing_functions"].append({"name": target, "called_at": f"{fn.files[0]}:{ln}", "how": how})
    return out


def _called_by(funcs: dict[str, Function], fid: str) -> list[tuple[str, int, str, str | None]]:
    return [(f.id, ln, how, d) for f in funcs.values() for ln, t, how, d in f.calls if t == fid]


def lookup(repo: Path, what: str | None = None, name: str | None = None) -> dict:
    """``verinoda datapack``: no argument -> the functions, tags and objectives counted and the mismatches;
    ``tag NAME`` / ``score NAME`` -> every site; ``function ID`` -> what it calls and what calls it."""
    ix = index(repo)
    base = {"functions": len(ix["functions"]), "tags": len([t for t in ix["tags"] if t != "*"]),
            "objectives": len(ix["objectives"])}
    if not what:
        if not ix["functions"] and not ix["tags"]:
            return {**base, "status": "no_datapack", "note": "no data/<namespace>/function[s]/*.mcfunction and no "
                                                            "entity tags in Java"}
        return {**base, "status": "found", "kind": "summary", "problems": problems(ix),
                "note": "read from the text: names built at run time are not seen; a mismatch is a lead"}
    if what in ("tag", "score", "objective"):
        table = ix["tags"] if what == "tag" else ix["objectives"]
        sites = table.get(name or "")
        if not sites:
            from difflib import get_close_matches

            return {**base, "status": "not_found", "kind": what, "name": name,
                    "nearest": get_close_matches(name or "", [k for k in table if k != "*"], n=5)}
        return {**base, "status": "found", "kind": what, "name": name,
                "sites": [{"at": s.at, "lang": s.lang, "kind": s.kind, "text": s.text} for s in sites]}
    if what == "function":
        fn = ix["functions"].get(name or "")
        if fn is None:
            from difflib import get_close_matches

            return {**base, "status": "not_found", "kind": "function", "name": name,
                    "nearest": get_close_matches(name or "", list(ix["functions"]), n=5)}
        return {**base, "status": "found", "kind": "function", "name": fn.id, "files": fn.files, "events": fn.events,
                "calls": [{"line": ln, "target": t, "how": how, **({"delay": d} if d else {})}
                          for ln, t, how, d in fn.calls],
                "called_by": [{"function": f, "at": f"{ix['functions'][f].files[0]}:{ln}", "how": how,
                               **({"delay": d} if d else {})} for f, ln, how, d in _called_by(ix["functions"], fn.id)]}
    raise ValueError(f"datapack: unknown lookup {what!r} (tag, score, function)")


def render(res: dict) -> str:
    head = f"{res['functions']} function(s), {res['tags']} entity tag(s), {res['objectives']} objective(s)"
    if res["status"] == "no_datapack":
        return f"{head}: {res['note']}"
    if res["status"] == "not_found":
        near = ", ".join(res.get("nearest") or [])
        return f"{res['kind']} {res['name']}: not found" + (f" (nearest: {near})" if near else "")
    if res["kind"] == "summary":
        out = [head]
        pr = res["problems"]
        labels = {"tags_checked_never_added": "tags checked but never added",
                  "tags_added_never_checked": "tags added but never checked",
                  "scores_written_never_read": "objectives written but never read",
                  "missing_functions": "calls to functions that do not exist"}
        for key, label in labels.items():
            rows = pr.get(key) or []
            out.append(f"{label} ({len(rows)}):")
            for r in rows[:15]:
                where = r.get("called_at") or ", ".join(r.get("sites", [])[:2])
                out.append(f"  {r['name']}  {where}")
            if len(rows) > 15:
                out.append(f"  (+{len(rows) - 15} more: --json)")
        if pr.get("tags_added_by_macros"):
            out.append("tags a macro fills in (not matched): " + ", ".join(pr["tags_added_by_macros"][:3]))
        out.append(f"note: {res['note']}")
        return "\n".join(out)
    if res["kind"] == "function":
        out = [f"{res['name']} ({', '.join(res['files'])})" + (f" - runs on {', '.join(res['events'])}"
                                                               if res["events"] else "")]
        out += [f"  calls {c['target']} ({c['how']}{' ' + c['delay'] if c.get('delay') else ''}) at line {c['line']}"
                for c in res["calls"]]
        out += [f"  called by {c['function']} at {c['at']} ({c['how']}{' ' + c['delay'] if c.get('delay') else ''})"
                for c in res["called_by"]]
        if not res["calls"] and not res["called_by"] and not res["events"]:
            out.append("  no call in or out found in the datapacks (Java may run it as a command string)")
        return "\n".join(out)
    out = [f"{res['kind']} {res['name']}: {len(res['sites'])} site(s)"]
    out += [f"  {s['kind']:<6} {s['lang']:<10} {s['at']}: {s['text']}" for s in res["sites"][:40]]
    return "\n".join(out)
