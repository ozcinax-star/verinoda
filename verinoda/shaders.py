"""GLSL shaders and the Java that feeds them (docs/DESIGN.md D54).

A mod's shaders read their inputs from uniform blocks (``layout(std140) uniform Frame { mat4 ViewProj; vec4 Weather;
... };``) that Java fills field by field in the same order (``Std140Builder b = ...; b.putMat4f(viewProj);
b.putVec4(rain, Atmosphere.wetness(), dist, thunder);``). Nothing names the link: "where does ``Weather.y`` come
from" needs the block's field order and the writer's call order side by side. Shaders and Java also mirror
constant tables (``#define MAT_STONE 1`` and ``STONE(1, ...)``) that must agree.

:func:`blocks` reads the uniform blocks of the shader files (``.glsl``, ``.fsh``, ``.vsh``, ``.vert``, ``.frag``);
:func:`writers` finds the Java ``Std140Builder`` sequences; :func:`link` pairs each block with the writer whose
sequence of types matches it, so every field (and each ``x/y/z/w`` of a ``vec4``) has the Java expression that
fills it, with its file and line; :func:`constant_tables` and :func:`check` compare mirrored constants and block
sizes and list what disagrees. Read from the text: a writer that fills a block in a loop or through a helper is not
paired (said, not guessed).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

SHADER_SUFFIXES = (".glsl", ".fsh", ".vsh", ".vert", ".frag", ".comp", ".geom")
_SKIP = {"build", ".gradle", "out", "bin", "node_modules", ".git", ".verinoda", "run", ".idea", "graphify-out"}
_BLOCK = re.compile(r"(?:layout\s*\([^)]*\)\s*)?uniform\s+([A-Za-z_]\w*)\s*\{([^}]*)\}\s*(\w+)?\s*;", re.S)
_FIELD = re.compile(r"^\s*(?:(?:lowp|mediump|highp|flat)\s+)*([a-z]\w*)\s+([A-Za-z_]\w*)\s*(\[\s*(\w+)\s*\])?\s*;")
_DEFINE = re.compile(r"^\s*#define\s+([A-Z][A-Z0-9_]*)\s+(-?\d+)\s*$", re.M)
_FUNC = re.compile(r"^\s*(?:[a-z]\w*)\s+([A-Za-z_]\w*)\s*\([^;{]*\)\s*\{", re.M)
_INCLUDE = re.compile(r"^\s*#(?:moj_import|include)\s*[<\"]([^>\"]+)[>\"]", re.M)
_PUT = re.compile(r"\b(\w+)\s*\.\s*(putMat4f|putMat3f|putMat2f|putVec4|putVec3|putVec2|putIVec4|putIVec3|putIVec2|"
                  r"putFloat|putInt|putUVec4)\s*\(")
_PUT_TYPE = {"putMat4f": "mat4", "putMat3f": "mat3", "putMat2f": "mat2", "putVec4": "vec4", "putVec3": "vec3",
             "putVec2": "vec2", "putIVec4": "ivec4", "putIVec3": "ivec3", "putIVec2": "ivec2", "putFloat": "float",
             "putInt": "int", "putUVec4": "uvec4"}
_COMPS = "xyzw"


@dataclass
class Field:
    type: str
    name: str
    line: int
    array: str | None = None


@dataclass
class Block:
    name: str
    file: str
    line: int
    fields: list[Field] = field(default_factory=list)

    @property
    def signature(self) -> list[str]:
        return [f.type for f in self.fields]


@dataclass
class Put:
    type: str
    args: list[str]
    line: int


@dataclass
class Writer:
    file: str
    line: int
    var: str
    method: str | None
    puts: list[Put] = field(default_factory=list)

    @property
    def signature(self) -> list[str]:
        return [p.type for p in self.puts]


def _files(repo: Path, suffixes) -> list[str]:
    out = []
    for p in sorted(Path(repo).rglob("*")):
        if p.suffix.lower() in suffixes and p.is_file():
            rel = p.relative_to(repo)
            if not any(x in _SKIP for x in rel.parts[:-1]):
                out.append(rel.as_posix())
    return out


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)
    return re.sub(r"//[^\n]*", lambda m: " " * len(m.group(0)), text)


def blocks(repo: Path, files: list[str] | None = None) -> list[Block]:
    """The uniform blocks of the shader files (the first definition of a block name wins; an include repeats it)."""
    repo = Path(repo)
    out: dict[str, Block] = {}
    for f in files if files is not None else _files(repo, SHADER_SUFFIXES):
        try:
            text = _strip_comments((repo / f).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        for m in _BLOCK.finditer(text):
            if m.group(1) in out:
                continue
            base = text.count("\n", 0, m.start(2)) + 1
            b = Block(m.group(1), f, text.count("\n", 0, m.start()) + 1)
            for k, ln in enumerate(m.group(2).split("\n")):
                fm = _FIELD.match(ln)
                if fm:
                    b.fields.append(Field(fm.group(1), fm.group(2), base + k, fm.group(4)))
            out[b.name] = b
    return list(out.values())


def _split_args(s: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _call_args(text: str, open_i: int) -> tuple[str, int]:
    depth = 0
    for k in range(open_i, len(text)):
        if text[k] == "(":
            depth += 1
        elif text[k] == ")":
            depth -= 1
            if depth == 0:
                return text[open_i + 1:k], k + 1
    return text[open_i + 1:], len(text)


def writers(repo: Path, files: list[str] | None = None) -> list[Writer]:
    """The runs of ``<var>.putX(...)`` calls on one builder variable in the Java sources, in order: a run ends where
    the variable is assigned again (a new ``Std140Builder``) or its method ends."""
    repo = Path(repo)
    out: list[Writer] = []
    for f in files if files is not None else _files(repo, (".java", ".kt")):
        try:
            raw = (repo / f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "put" not in raw or ("Std140" not in raw and "putVec4" not in raw):
            continue
        text = _strip_comments(raw)
        cur: Writer | None = None
        last_end = 0
        for m in _PUT.finditer(text):
            var = m.group(1)
            line = text.count("\n", 0, m.start()) + 1
            between = text[last_end:m.start()]
            new_run = cur is None or cur.var != var or re.search(rf"\b{re.escape(var)}\s*=", between) \
                or re.search(r"\n\s*(?:public|private|protected|static)\b[^;{]*\(", between)
            if new_run:
                meth = None
                for dm in re.finditer(r"(?:public|private|protected|static)[^;{=]*?\s([a-zA-Z_]\w*)\s*\([^;{]*\)\s*"
                                      r"(?:throws [\w., ]+)?\{", text[:m.start()]):
                    meth = dm.group(1)
                cur = Writer(f, line, var, meth)
                out.append(cur)
            args, end = _call_args(text, m.end() - 1)
            cur.puts.append(Put(_PUT_TYPE[m.group(2)], _split_args(args), line))
            last_end = end
    return [w for w in out if len(w.puts) >= 2]


def link(bl: list[Block], wr: list[Writer]) -> list[dict]:
    """Each block with the writer whose types match it in order: ``{"block", "writer", "exact"}``; a block with
    array fields is paired on the fields before its first array."""
    out = []
    for b in bl:
        sig = b.signature
        first_array = next((k for k, f in enumerate(b.fields) if f.array), None)
        head = sig if first_array is None else sig[:first_array]
        best = None
        for w in wr:
            ws = w.signature
            if ws == sig:
                best = (w, True)
                break
            if head and len(head) >= 3 and ws[:len(head)] == head and best is None:
                best = (w, False)
        if best:
            out.append({"block": b, "writer": best[0], "exact": best[1]})
    return out


def field_sources(pair: dict) -> dict[str, dict]:
    """``"Weather" -> {"at", "expr", "components": {"x": expr, ...}}`` for a linked block."""
    b, w = pair["block"], pair["writer"]
    out = {}
    for f, p in zip(b.fields, w.puts):
        comps = {}
        if f.type.endswith(("vec2", "vec3", "vec4")) and len(p.args) == int(f.type[-1]):
            comps = {_COMPS[k]: a for k, a in enumerate(p.args)}
        out[f.name] = {"type": f.type, "at": f"{w.file}:{p.line}", "expr": ", ".join(p.args), "components": comps,
                       "declared_at": f"{b.file}:{f.line}"}
    return out


def constant_tables(repo: Path, shader_files: list[str] | None = None,
                    java_files: list[str] | None = None) -> list[dict]:
    """Mirrored constant tables: a ``#define PREFIX_NAME n`` group of the shaders and a Java enum whose constants
    take the same names with their number as first argument (``NAME(n, ...)``), or ``static final int NAME = n``
    constants. ``{"prefix", "shader_at", "java_at", "same", "differ", "only_shader", "only_java"}``."""
    repo = Path(repo)
    groups: dict[str, dict[str, tuple[int, str]]] = {}
    for f in shader_files if shader_files is not None else _files(repo, SHADER_SUFFIXES):
        try:
            text = (repo / f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _DEFINE.finditer(text):
            name = m.group(1)
            if "_" not in name:
                continue
            prefix, _, rest = name.rpartition("_") if name.count("_") == 1 else (
                "_".join(name.split("_")[:2]), "_", "_".join(name.split("_")[2:]))
            groups.setdefault(prefix, {})[rest] = (int(m.group(2)), f"{f}:{text.count(chr(10), 0, m.start()) + 1}")
    java: dict[str, dict[str, tuple[int, str]]] = {}
    for f in java_files if java_files is not None else _files(repo, (".java",)):
        try:
            text = (repo / f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for em in re.finditer(r"\benum\s+(\w+)\s*\{", text):
            vals = {}
            for cm in re.finditer(r"^\s*([A-Z][A-Z0-9_]*)\s*\(\s*(-?\d+)\s*[,)]", text[em.end():em.end() + 20000], re.M):
                vals[cm.group(1)] = (int(cm.group(2)), f"{f}:{text.count(chr(10), 0, em.end() + cm.start()) + 1}")
            if vals:
                java[f"{f}#{em.group(1)}"] = vals
    out = []
    for prefix, sv in groups.items():
        for key, jv in java.items():
            common = set(sv) & set(jv)
            if len(common) < 3 or len(common) < 0.6 * min(len(sv), len(jv)):
                continue
            out.append({"prefix": prefix + "_", "java": key.split("#")[1], "shader_at": next(iter(sv.values()))[1],
                        "java_at": next(iter(jv.values()))[1],
                        "same": sorted(k for k in common if sv[k][0] == jv[k][0]),
                        "differ": [{"name": k, "shader": sv[k][0], "shader_at": sv[k][1], "java": jv[k][0],
                                    "java_at": jv[k][1]} for k in sorted(common) if sv[k][0] != jv[k][0]],
                        "only_shader": sorted(set(sv) - common), "only_java": sorted(set(jv) - common)})
    return out


def check(repo: Path) -> dict:
    """What disagrees between the shaders and Java: a block no writer matches exactly (a field added on one side,
    the other not), mirrored constants with different values or on one side only."""
    bl, wr = blocks(repo), writers(repo)
    pairs = link(bl, wr)
    paired = {p["block"].name for p in pairs}
    issues = []
    for p in pairs:
        if not p["exact"]:
            b, w = p["block"], p["writer"]
            issues.append({"kind": "block_writer_mismatch", "block": b.name, "at": f"{b.file}:{b.line}",
                           "writer_at": f"{w.file}:{w.line}",
                           "why": f"{b.name} declares {len(b.fields)} field(s), the writer puts {len(w.puts)}"})
    for b in bl:
        if b.name not in paired and not any(f.array for f in b.fields):
            near = [w for w in wr if abs(len(w.puts) - len(b.fields)) <= 2 and w.signature[:3] == b.signature[:3]]
            if near:
                w = near[0]
                k = next((i for i, (x, y) in enumerate(zip(b.signature, w.signature)) if x != y),
                         min(len(b.fields), len(w.puts)))
                issues.append({"kind": "block_writer_mismatch", "block": b.name, "at": f"{b.file}:{b.line}",
                               "writer_at": f"{w.file}:{w.line}",
                               "why": f"{b.name} declares {len(b.fields)} field(s) ({', '.join(b.signature)}), the "
                                      f"writer puts {len(w.puts)}; they differ from field {k + 1}"
                                      + (f" ({b.fields[k].name})" if k < len(b.fields) else "")})
    for t in constant_tables(repo):
        for d in t["differ"]:
            issues.append({"kind": "constant_differs", "name": t["prefix"] + d["name"], "at": d["shader_at"],
                           "java_at": d["java_at"], "why": f"{d['shader']} in the shader, {d['java']} in {t['java']}"})
        for n in t["only_shader"]:
            issues.append({"kind": "constant_one_sided", "name": t["prefix"] + n, "at": t["shader_at"],
                           "why": f"in the shader's {t['prefix']} table, not in {t['java']}"})
        for n in t["only_java"]:
            issues.append({"kind": "constant_one_sided", "name": n, "at": t["java_at"],
                           "why": f"in {t['java']}, not in the shader's {t['prefix']} table"})
    return {"blocks": len(bl), "writers": len(wr), "paired": len(pairs), "issues": issues}


def where_from(repo: Path, name: str) -> dict:
    """``Weather.y`` / ``Weather`` / ``Frame.Weather``: the Java that fills the field (the component's expression)."""
    m = re.fullmatch(r"(?:(\w+)\.)?([A-Za-z_]\w*)(?:\.([xyzw]))?", name.strip())
    if not m:
        return {"query": name, "status": "not_found", "why": "expected Field, Field.x or Block.Field"}
    blk, fld, comp = m.groups()
    if blk and not comp and len(blk) > 1 and fld in _COMPS:
        blk, fld, comp = None, blk, fld
    pairs = link(blocks(repo), writers(repo))
    for p in pairs:
        if blk and p["block"].name != blk:
            continue
        src = field_sources(p).get(fld)
        if src:
            expr = src["components"].get(comp) if comp else src["expr"]
            return {"query": name, "status": "found", "block": p["block"].name, "field": fld, "component": comp,
                    "expr": expr, "at": src["at"], "declared_at": src["declared_at"], "exact": p["exact"],
                    **({"note": "the block and its writer differ in length: the pairing is by the fields before "
                                "the first difference"} if not p["exact"] else {})}
    return {"query": name, "status": "not_found",
            "why": f"no uniform block field {fld} with a Java writer paired to it (a block filled in a loop or "
                   "through a helper is not paired)"}


def lookup(repo: Path, name: str | None, *, check_only: bool = False) -> dict:
    """``verinoda shader NAME`` (where a uniform field comes from) or ``--check`` (what disagrees)."""
    if check_only or not name:
        res = check(repo)
        res["tables"] = [{k: t[k] for k in ("prefix", "java")} | {"same": len(t["same"])}
                         for t in constant_tables(repo)]
        return {"status": "found", "kind": "check", **res}
    return {"kind": "field", **where_from(repo, name)}


def render(res: dict) -> str:
    if res["kind"] == "check":
        out = [f"{res['blocks']} uniform block(s), {res['writers']} Java writer(s), {res['paired']} paired; "
               + (", ".join(f"{t['prefix']}* = {t['java']} ({t['same']} agree)" for t in res["tables"])
                  or "no mirrored constant table")]
        out += [f"  {i['kind']}: {i.get('block') or i.get('name')} ({i['at']}) - {i['why']}" for i in res["issues"]]
        if not res["issues"]:
            out.append("  nothing disagrees")
        return "\n".join(out)
    if res["status"] != "found":
        return f"{res['query']}: not found - {res.get('why')}"
    what = f"{res['block']}.{res['field']}" + (f".{res['component']}" if res.get("component") else "")
    return (f"{what} (declared at {res['declared_at']}) is filled at {res['at']}: {res['expr']}"
            + (f"\n  note: {res['note']}" if res.get("note") else ""))
