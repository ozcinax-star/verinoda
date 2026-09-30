"""Access wideners (Fabric, Quilt) and access transformers (Forge, NeoForge) checked against the bytecode.

An access widener (``accessible method net/minecraft/world/entity/Mob checkSpawnRules (...)Z``) or an access
transformer (``public net.minecraft.world.entity.Mob m_5545_(...)Z``) names a class, and often a member with its
descriptor, of a jar the mod compiles against. A misspelt name or a descriptor from another game version is not
an error in the source: Loom stops the build on the first one, or the loader ignores the entry and the mod fails
later with an ``IllegalAccessError``. Here every entry is read with its line and looked up in the class files of
the build's classpath (:func:`verinoda.jvmclass.discover`: the configured ``code_check.classpath`` or the one a
Loom build resolved).

Verdicts: ``exists`` (the class, and the member with that descriptor, are in a class file; the evidence is
``jar!class``), ``absent`` (the class file is read and holds no such member, with the nearest real ones; or the
class is on none of the jars of a complete classpath), ``malformed`` (the line does not parse: an unknown access,
a missing name, a dotted class name in a widener, a descriptor that is not one), ``unknown`` (no class file to
compare with: an incomplete or missing classpath, a JDK class, a class of the project's own sources, a widener in
another namespace than ``named``, an SRG name a transformer keeps for the production jar). A member missing from
a class file that was read is ``statically_verified``; a class missing from every jar is ``strong_inference``
(the classpath is what the last build resolved, and may be older than the build file).
"""
from __future__ import annotations

import difflib
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from verinoda import jvmclass

AW_SUFFIXES = (".accesswidener", ".classtweaker")
AW_ACCESS = ("accessible", "extendable", "mutable")
AT_ACCESS = ("public", "protected", "default", "private")
_SKIP = {"build", ".gradle", "out", "bin", "node_modules", ".git", ".verinoda", "run", ".idea", "graphify-out"}
_AT_NAME = re.compile(r"(?:^|/)(?:[^/]*accesstransformer[^/]*|[^/]+_at)\.cfg$", re.I)
_FIELD_DESC = r"\[*(?:[BCDFIJSZ]|L[^;()\[\s]+;)"
_FIELD_RE = re.compile(_FIELD_DESC + r"$")
_METHOD_RE = re.compile(r"\((?:" + _FIELD_DESC + r")*\)(?:V|" + _FIELD_DESC + r")$")
# names a Forge transformer keeps in SRG form, for the production jar; the dev classpath carries other names
_SRG = re.compile(r"^(?:[fm]_\d+_|field_\d+_\w*|func_\d+_\w*)$")
_JDK_PREFIXES = ("java/", "javax/", "jdk/", "sun/", "com/sun/")
_NESTED = ("META-INF/jars/", "META-INF/jarjar/")


@dataclass
class Entry:
    path: str
    line: int
    text: str
    fmt: str                         # accesswidener | accesstransformer
    kind: str = "class"              # class | method | field
    cls: str | None = None           # binary name, a/b/C$D
    name: str | None = None
    desc: str | None = None
    error: str | None = None         # why the line does not parse

    @property
    def at(self) -> str:
        return f"{self.path}:{self.line}"


@dataclass
class AccessFile:
    path: str
    fmt: str
    namespace: str | None = None
    entries: list[Entry] = field(default_factory=list)


# -- finding and reading the files -----------------------------------------------------------------------

def _named_by_manifests(repo: Path, listed: list[str]) -> set[str]:
    """Files a ``fabric.mod.json`` / ``quilt.mod.json`` (``accessWidener``, ``access_widener``) or a
    ``neoforge.mods.toml`` (``[[accessTransformers]] file = ...``) names, when they exist beside it."""
    import json

    out: set[str] = set()
    for rel in listed:
        base = rel.rsplit("/", 1)[-1]
        if base not in ("fabric.mod.json", "quilt.mod.json", "neoforge.mods.toml", "mods.toml"):
            continue
        try:
            text = (repo / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        names: list[str] = []
        if base.endswith(".json"):
            try:
                data = json.loads(text)
            except ValueError:
                continue
            if isinstance(data, dict):
                for key in ("accessWidener", "access_widener"):
                    v = data.get(key)
                    names += [v] if isinstance(v, str) else [x for x in v if isinstance(x, str)] \
                        if isinstance(v, list) else []
            root = rel.rsplit("/", 1)[0] if "/" in rel else ""
        else:
            names = re.findall(r"\[\[\s*accessTransformers\s*\]\]\s*file\s*=\s*[\"']([^\"']+)[\"']", text)
            root = rel.rsplit("/", 2)[0] if rel.count("/") >= 2 else ""   # beside META-INF/
        for n in names:
            cand = f"{root}/{n}" if root else n
            if (repo / cand).is_file():
                out.add(cand)
    return out


def access_files(repo: Path) -> list[tuple[str, str]]:
    """``(path, format)`` of every access widener and access transformer file of the project."""
    from verinoda.snapshot import listed_files

    repo = Path(repo)
    listed = [Path(r).as_posix() for r in listed_files(repo)]
    listed = [r for r in listed if not any(x in _SKIP for x in r.split("/")[:-1])]
    out: dict[str, str] = {}
    for rel in listed:
        if rel.lower().endswith(AW_SUFFIXES):
            out[rel] = "accesswidener"
        elif _AT_NAME.search(rel):
            out[rel] = "accesstransformer"
    for rel in _named_by_manifests(repo, listed):
        out.setdefault(rel, "accesstransformer" if rel.lower().endswith(".cfg") else "accesswidener")
    return sorted(out.items())


def _strip(line: str) -> str:
    return line.split("#", 1)[0].strip()


def parse_widener(path: str, text: str) -> AccessFile:
    """An access widener or class tweaker: its header's namespace and one entry per rule line."""
    af = AccessFile(path, "accesswidener")
    header_seen = False
    for no, raw in enumerate(text.splitlines(), 1):
        line = _strip(raw)
        if not line:
            continue
        tokens = line.split()
        if not header_seen:
            header_seen = True
            if tokens[0] in ("accessWidener", "classTweaker"):
                if len(tokens) != 3 or not re.fullmatch(r"v\d+", tokens[1]):
                    af.entries.append(Entry(path, no, raw.strip(), af.fmt, error="the header is "
                                            "'accessWidener v<N> <namespace>'"))
                af.namespace = tokens[2] if len(tokens) == 3 else None
                continue
            # the loader refuses the whole file: its rules are not checked one by one
            af.entries.append(Entry(path, no, raw.strip(), af.fmt,
                                    error="no 'accessWidener v2 named' header before the first rule"))
            break
        e =Entry(path, no, raw.strip(), af.fmt)
        af.entries.append(e)
        access = tokens[0].removeprefix("transitive-")
        if access not in AW_ACCESS:
            e.error = f"unknown access '{tokens[0]}' (accessible, extendable, mutable)"
            continue
        if len(tokens) < 2 or tokens[1] not in ("class", "method", "field"):
            e.error = "the second word is class, method or field"
            continue
        e.kind = tokens[1]
        want = 3 if e.kind == "class" else 5
        if len(tokens) != want:
            e.error = (f"a {e.kind} rule is '<access> {e.kind} <class>" +
                       (" <name> <descriptor>'" if want == 5 else "'") + f"; this one has {len(tokens)} words")
            continue
        e.cls = tokens[2]
        if "." in e.cls:
            e.error = f"class names are written with '/' (a/b/C), not '.': {e.cls}"
            continue
        if e.kind != "class":
            e.name, e.desc = tokens[3], tokens[4]
            if not (_METHOD_RE if e.kind == "method" else _FIELD_RE).match(e.desc):
                e.error = f"'{e.desc}' is not a {e.kind} descriptor"
                continue
        if access == "mutable" and e.kind != "field":
            e.error = "mutable applies to fields only"
        elif access == "extendable" and e.kind == "field":
            e.error = "extendable applies to classes and methods only"
    return af


def parse_transformer(path: str, text: str) -> AccessFile:
    """A Forge / NeoForge access transformer: ``<access>[-f|+f] <class> [<field> | <method>(<descriptor>) | * |
    *()]`` per line."""
    af = AccessFile(path, "accesstransformer")
    for no, raw in enumerate(text.splitlines(), 1):
        line = _strip(raw)
        if not line:
            continue
        tokens = line.split()
        e = Entry(path, no, raw.strip(), af.fmt)
        af.entries.append(e)
        access = re.sub(r"[-+]f$", "", tokens[0])
        if access not in AT_ACCESS:
            e.error = f"unknown access '{tokens[0]}' (public, protected, default, private, with -f or +f)"
            continue
        if len(tokens) not in (2, 3):
            e.error = f"a rule is '<access> <class> [<member>]'; this one has {len(tokens)} words"
            continue
        e.cls = tokens[1].replace(".", "/")
        if len(tokens) == 3:
            member = tokens[2]
            if member in ("*", "*()"):
                e.kind, e.name = ("method" if member == "*()" else "field"), member
                continue
            if "(" in member:
                e.kind = "method"
                e.name, _, rest = member.partition("(")
                e.desc = "(" + rest
                if not _METHOD_RE.match(e.desc):
                    e.error = f"'{e.desc}' is not a method descriptor"
            else:
                e.kind, e.name = "field", member
            if not e.error and not re.fullmatch(r"[\w$<>]+", e.name or ""):
                e.error = f"'{e.name}' is not a member name"
    return af


def read_file(repo: Path, path: str, fmt: str) -> AccessFile:
    try:
        text = (Path(repo) / path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        af = AccessFile(path, fmt)
        af.entries.append(Entry(path, 1, "", fmt, error=f"not readable: {exc.__class__.__name__}"))
        return af
    return parse_widener(path, text) if fmt == "accesswidener" else parse_transformer(path, text)


# -- the class files -------------------------------------------------------------------------------------

class ClassFiles:
    """The class files of a classpath by binary name, read only for the classes an entry names."""

    def __init__(self, jars: list[Path]):
        self.where: dict[str, tuple[Path, str | None]] = {}   # class -> (jar, nested jar entry or None)
        self.unreadable: list[str] = []
        self._nested: dict[tuple[Path, str], bytes] = {}
        self._members: dict[str, dict | None] = {}
        for jar in jars:
            try:
                with zipfile.ZipFile(jar) as z:
                    for n in z.namelist():
                        if n.endswith(".class") and not n.startswith("META-INF/"):
                            self.where.setdefault(n[:-6], (jar, None))
                        elif n.endswith(".jar") and n.startswith(_NESTED):
                            data = z.read(n)
                            self._nested[(jar, n)] = data
                            try:
                                with zipfile.ZipFile(io.BytesIO(data)) as inner:
                                    for m in inner.namelist():
                                        if m.endswith(".class") and not m.startswith("META-INF/"):
                                            self.where.setdefault(m[:-6], (jar, n))
                            except zipfile.BadZipFile:
                                pass
            except (OSError, zipfile.BadZipFile):
                self.unreadable.append(jar.name)

    def evidence(self, cls: str) -> str:
        jar, nested = self.where[cls]
        return f"{jar.name}!" + (f"{nested}!" if nested else "") + f"{cls}.class"

    def members(self, cls: str) -> dict | None:
        if cls not in self._members:
            got = None
            if cls in self.where:
                jar, nested = self.where[cls]
                try:
                    if nested:
                        with zipfile.ZipFile(io.BytesIO(self._nested[(jar, nested)])) as z:
                            got = jvmclass.class_members(z.read(cls + ".class"))
                    else:
                        with zipfile.ZipFile(jar) as z:
                            got = jvmclass.class_members(z.read(cls + ".class"))
                except (OSError, KeyError, zipfile.BadZipFile):
                    got = None
            self._members[cls] = got
        return self._members[cls]

    def nearest_classes(self, cls: str, limit: int = 3) -> list[str]:
        pkg = cls.rpartition("/")[0]
        same = [c for c in self.where if c.rpartition("/")[0] == pkg]
        return difflib.get_close_matches(cls, same or list(self.where), n=limit, cutoff=0.6)


def _project_class(cls: str, java_paths: set[str]) -> bool:
    outer = cls.split("$", 1)[0]
    return any(p.endswith(f"/{outer}{ext}") or p == f"{outer}{ext}" for ext in (".java", ".kt") for p in java_paths)


def _check_entry(e: Entry, ns: str | None, cf: ClassFiles, complete: bool, source: str,
                 java_paths: set[str]) -> dict:
    row = {"at": e.at, "entry": e.text, "format": e.fmt, "kind": e.kind, "class": e.cls, "name": e.name,
           "descriptor": e.desc}

    def done(verdict: str, status: str, why: str, **extra) -> dict:
        row.update(verdict=verdict, status=status, why=why, **extra)
        return row

    if e.error:
        return done("malformed", "statically_verified", e.error)
    if e.fmt == "accesswidener" and ns not in (None, "named"):
        return done("unknown", "unknown", f"the file is written in '{ns}' names; the classpath is read in the "
                                          f"names the build compiles against (named)")
    cls = e.cls or ""
    if cls not in cf.where:
        if cls.startswith(_JDK_PREFIXES):
            return done("unknown", "unknown", "a JDK class: the JDK's class files are not read here")
        if _project_class(cls, java_paths):
            return done("unknown", "unknown", "a class of the project's own sources: its compiled members are "
                                              "not read here")
        if not complete:
            why = "the classpath is not complete" + (f" ({source})" if source else "")
            return done("unknown", "unknown", f"{cls} is on no jar read; {why}")
        near = cf.nearest_classes(cls)
        return done("absent", "strong_inference", f"no class {cls} on any jar of the classpath",
                    nearest=near)
    evidence = cf.evidence(cls)
    if e.kind == "class" or e.name in ("*", "*()"):
        return done("exists", "statically_verified", f"{cls} is in {evidence}", evidence=evidence)
    members = cf.members(cls)
    if members is None:
        return done("unknown", "unknown", f"{evidence} could not be read", evidence=evidence)
    table = members[e.kind + "s"]
    same_name = [d for n, d, _a in table if n == e.name]
    if same_name and (e.desc is None or e.desc in same_name):
        shown = e.desc or same_name[0]
        return done("exists", "statically_verified", f"{e.kind} {e.name}{'' if e.kind == 'method' else ' '}"
                                                      f"{shown} is in {evidence}", evidence=evidence)
    if same_name:
        near = [f"{e.name}{'' if e.kind == 'method' else ' '}{d}" for d in same_name][:5]
        return done("absent", "statically_verified",
                    f"{cls} has no {e.kind} {e.name} with the descriptor {e.desc}; its {e.name} has "
                    + ", ".join(same_name[:5]), evidence=evidence, nearest=near)
    names = sorted({n for n, _d, _a in table})
    near_names = difflib.get_close_matches(e.name or "", names, n=3, cutoff=0.5)
    near = [f"{n}{'' if e.kind == 'method' else ' '}{d}" for nn in near_names for n, d, _a in table if n == nn][:5]
    if e.fmt == "accesstransformer" and _SRG.match(e.name or ""):
        return done("unknown", "unknown", f"{e.name} is an SRG name (the production jar's); the classpath read "
                                          f"here carries other names", evidence=evidence)
    return done("absent", "statically_verified", f"{cls} has no {e.kind} named {e.name}", evidence=evidence,
                nearest=near)


def check(repo: Path, paths: list[str] | None = None, config: dict | None = None,
          cache_dir: Path | None = None) -> dict:
    """Every entry of the project's access widener and transformer files (or of ``paths``) against the
    classpath of the build each file belongs to."""
    import time

    from verinoda.snapshot import listed_files

    t0 = time.perf_counter()
    repo = Path(repo).resolve()
    if paths:
        targets = []
        for p in paths:
            ap = Path(p) if Path(p).is_absolute() else repo / p
            try:
                rel = ap.resolve().relative_to(repo).as_posix()
            except ValueError:
                rel = ap.as_posix()
            fmt = "accesstransformer" if rel.lower().endswith(".cfg") else "accesswidener"
            targets.append((rel, fmt))
    else:
        targets = access_files(repo)
    java_paths = {Path(r).as_posix() for r in listed_files(repo) if r.endswith((".java", ".kt"))}
    by_root: dict[Path, list[AccessFile]] = {}
    for rel, fmt in targets:
        af = read_file(repo, rel, fmt)
        by_root.setdefault(jvmclass.build_root(repo, repo / rel), []).append(af)
    files, entries, builds, notes = [], [], [], []
    for root, group in sorted(by_root.items()):
        cp = jvmclass.discover(root, config if root == repo else None)
        cf = ClassFiles(cp.jars)
        complete = cp.complete and not cf.unreadable
        notes += cp.notes + [f"unreadable jar: {n}" for n in cf.unreadable]
        where = root.relative_to(repo).as_posix() if root != repo else "."
        builds.append({"build": where, "classpath": cp.source, "complete": complete, "jars": len(cp.jars),
                       "classes": len(cf.where)})
        for af in group:
            rows = [_check_entry(e, af.namespace, cf, complete, cp.source, java_paths) for e in af.entries]
            entries += rows
            files.append({"path": af.path, "format": af.fmt, "namespace": af.namespace, "build": where,
                          "entries": len(rows)})
    counts = {v: sum(1 for r in entries if r["verdict"] == v) for v in ("exists", "absent", "malformed", "unknown")}
    return {"files": files, "builds": builds, "entries": entries, "counts": counts,
            "notes": list(dict.fromkeys(notes)), "seconds": round(time.perf_counter() - t0, 3)}


def lookup(repo: Path, paths: list[str] | None = None) -> dict:
    """``verinoda access-check``: the check; ``no_files`` when the project has no widener or transformer."""
    from verinoda.paths import atlas_dir, load_config

    try:
        config = load_config(repo)
    except Exception:  # noqa: BLE001 - no readable config: the build's own classpath is looked for
        config = None
    res = check(repo, paths, config, atlas_dir(repo) / "cache" / "jvm")
    if not res["files"]:
        return {"status": "no_files", **res,
                "note": "no .accesswidener, .classtweaker or access transformer (.cfg) file in the project"}
    return {"status": "found", **res}


def render(res: dict) -> str:
    if res["status"] == "no_files":
        return res["note"]
    c = res["counts"]
    out = [f"{len(res['files'])} file(s), {len(res['entries'])} entries: {c['exists']} found in the bytecode, "
           f"{c['absent']} absent, {c['malformed']} malformed, {c['unknown']} unknown"]
    for b in res["builds"]:
        out.append(f"  classpath of {b['build']}: {b['classpath']}, "
                   f"{'complete' if b['complete'] else 'not complete'}, {b['jars']} jar(s)")
    for n in res["notes"]:
        out.append(f"  note: {n}")
    order = {"malformed": 0, "absent": 1, "unknown": 2}
    for r in sorted((r for r in res["entries"] if r["verdict"] != "exists"), key=lambda r: order[r["verdict"]]):
        out.append(f"  {r['verdict']} [{r['status']}] {r['at']}  {r['entry']}")
        out.append(f"    {r['why']}")
        if r.get("nearest"):
            out.append(f"    nearest: {', '.join(r['nearest'])}")
    return "\n".join(out)
