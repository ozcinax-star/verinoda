"""Packs and mods loaded together: resource collisions between them, and mod dependencies the set does not meet.

Two mods (or a mod and a datapack) that ship the same file - ``data/<ns>/recipe/x.json``,
``assets/<ns>/textures/item/x.png`` - collide: the game uses one and hides the other, by load order, and neither
source shows it alone. :func:`check` finds the packs of the repository (every folder holding ``data/<ns>/<kind>``
or ``assets/<ns>/<kind>`` files, or a mod manifest) and of the paths given with ``--with`` (a mod jar or zip, a
datapack folder, a mods folder: its jars are one source each), and lists

* **collisions**: a resource path two sources ship with different bytes, with both files as evidence. Files the
  game merges are left out: tags (unless one of the copies says ``"replace": true``), language files,
  ``sounds.json``, atlases, ``pack.mcmeta``. The same bytes in two places are a copy, counted apart. Two sources
  that never load together are not compared: builds of the same mod id (a Fabric and a Forge subproject, a copy
  of the project), and mods of different loaders (Fabric or Quilt against Forge against NeoForge);
* **dependencies**: what ``fabric.mod.json`` / ``quilt.mod.json`` ``depends`` / ``breaks`` and
  ``META-INF/[neoforge.]mods.toml`` ``[[dependencies.<id>]]`` declare, checked against the mods of the set (their
  ``id``, ``provides`` and the manifests of jars nested one level deep): a required mod no source provides, a
  version outside the declared range, a mod the set holds that another declares it breaks. The game, Java and the
  loaders are the platform and are not checked. Without ``--with`` the set is the repository alone, so a
  dependency found nowhere in it is listed as external (not checked), not as missing.

Read from the files as written: which copy of a collision wins, and whether two sources load together at all, the
game decides at run time; a version range is read with the loader's rules as far as this module implements them
(Fabric/Quilt predicates, Maven ranges), and a version it cannot read is left unchecked.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from verinoda.resources import ASSET_KINDS, DATA_REGISTRIES

_SKIP = {"build", ".gradle", "out", "bin", "node_modules", ".git", ".verinoda", "run", ".idea", "graphify-out"}
_SEG = re.compile(r"(?:^|/)(data|assets)/([a-z0-9_.-]+)/([a-z0-9_]+)/[^/]")
_MANIFESTS = {"fabric.mod.json": "fabric", "quilt.mod.json": "quilt", "META-INF/mods.toml": "forge",
              "META-INF/neoforge.mods.toml": "neoforge"}
_NESTED = ("META-INF/jars/", "META-INF/jarjar/")
PLATFORM = frozenset({"minecraft", "java", "fabricloader", "fabric-loader", "quilt_loader", "forge", "neoforge",
                      "javafml", "lowcodefml", "mcp"})
# loaders whose mods load in one game: Quilt loads Fabric mods
_LOADER_FAMILY = {"fabric": "fabric", "quilt": "fabric", "forge": "forge", "neoforge": "neoforge"}


@dataclass
class Dep:
    id: str
    wants: list[str]        # alternatives of the range as written; [] = any version
    at: str
    style: str              # "semver" (Fabric/Quilt) or "maven" (Forge/NeoForge)


@dataclass
class Mod:
    id: str
    version: str | None     # None: not written, or a build placeholder (${version})
    loader: str
    at: str
    provides: list[str] = field(default_factory=list)
    depends: list[Dep] = field(default_factory=list)
    breaks: list[Dep] = field(default_factory=list)


@dataclass
class Source:
    label: str
    kind: str                                        # repo | dir | archive
    files: dict[str, str] = field(default_factory=dict)  # resource path -> evidence path
    mods: list[Mod] = field(default_factory=list)    # its own manifest's mods
    nested: list[Mod] = field(default_factory=list)  # mods of jars it bundles
    read: object = None                              # resource path -> bytes | None

    def record(self) -> dict:
        return {"source": self.label, "kind": self.kind, "resources": len(self.files),
                "mods": [f"{m.id} {m.version}" if m.version else m.id for m in self.mods],
                **({"bundles": [m.id for m in self.nested]} if self.nested else {})}


def _root_of(rel: str) -> tuple[str, str] | None:
    """``(pack root, resource path)`` of a file under ``data/<ns>/<registry>`` or ``assets/<ns>/<kind>``."""
    for m in _SEG.finditer(rel):
        kind = m.group(3)
        if kind in (DATA_REGISTRIES if m.group(1) == "data" else ASSET_KINDS):
            start = m.start(1)
            return rel[:start].rstrip("/"), rel[start:]
    return None


def _manifest_root(rel: str) -> tuple[str, str] | None:
    for name, loader in _MANIFESTS.items():
        if rel == name or rel.endswith("/" + name):
            return rel[:len(rel) - len(name)].rstrip("/"), loader
    return None


def _line_of(lines: list[str], needle: str, start: int = 0) -> int:
    return next((i for i, s in enumerate(lines[start:], start + 1) if needle in s), 0)


def _q(name: str) -> str:
    return f'"{name}"'


def _concrete(v) -> str | None:
    return v if isinstance(v, str) and v and "$" not in v else None


def _alts(v) -> list[str]:
    if isinstance(v, list):
        return [s for s in v if isinstance(s, str) and s.strip() not in ("", "*")] if "*" not in v else []
    return [v] if isinstance(v, str) and v.strip() not in ("", "*") else []


def parse_manifest(text: str, loader: str, at: str) -> list[Mod]:
    """The mods one manifest declares. ``at`` is the manifest's evidence path (``file`` or ``x.jar!/file``)."""
    lines = text.splitlines()
    if loader in ("fabric", "quilt"):
        try:
            data = json.JSONDecoder().raw_decode(text.lstrip("\ufeff \t\r\n"))[0]
        except ValueError:
            return []
        if not isinstance(data, dict):
            return []
        if loader == "quilt":
            ql = data.get("quilt_loader") if isinstance(data.get("quilt_loader"), dict) else {}
            mid, ver = ql.get("id"), ql.get("version")
            provides = [p.get("id") if isinstance(p, dict) else p for p in ql.get("provides") or []]
            deps_raw = [(d, False) for d in ql.get("depends") or []] + [(d, True) for d in ql.get("breaks") or []]
        else:
            mid, ver = data.get("id"), data.get("version")
            provides = data.get("provides") or []
            deps_raw = [({"id": k, "versions": v}, False) for k, v in (data.get("depends") or {}).items()] + \
                       [({"id": k, "versions": v}, True) for k, v in (data.get("breaks") or {}).items()]
        if not isinstance(mid, str) or not mid:
            return []
        mod = Mod(mid, _concrete(ver), loader, f"{at}:{_line_of(lines, _q(mid)) or 1}",
                  [p for p in provides if isinstance(p, str)])
        for d, breaks in deps_raw:
            if isinstance(d, str):
                d = {"id": d}
            if not isinstance(d, dict) or not isinstance(d.get("id"), str) or d.get("optional"):
                continue
            did = d["id"].split(":", 1)[-1]  # quilt's "group:id"
            start = max(_line_of(lines, '"breaks"' if breaks else '"depends"') - 1, 0)
            ln = _line_of(lines, _q(d["id"]), start) or _line_of(lines, _q(d["id"]))
            (mod.breaks if breaks else mod.depends).append(Dep(did, _alts(d.get("versions")), f"{at}:{ln or 1}",
                                                              "semver"))
        return [mod]
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - py3.10
        import tomli as tomllib  # type: ignore[no-redef]
    try:
        data = tomllib.loads(text)
    except Exception:  # noqa: BLE001 - an unreadable manifest declares nothing
        return []
    deps = data.get("dependencies") if isinstance(data.get("dependencies"), dict) else {}
    if loader == "forge" and any(d.get("modId") == "neoforge" for ds in deps.values() if isinstance(ds, list)
                                 for d in ds if isinstance(d, dict)):
        loader = "neoforge"
    out = []
    for m in data.get("mods") or []:
        mid = m.get("modId") if isinstance(m, dict) else None
        if not isinstance(mid, str) or not mid:
            continue
        mod = Mod(mid, _concrete(m.get("version")), loader, f"{at}:{_line_of(lines, _q(mid)) or 1}")
        head = _line_of(lines, f"dependencies.{mid}]")
        for d in deps.get(mid) or []:
            if not isinstance(d, dict) or not isinstance(d.get("modId"), str):
                continue
            kind = str(d.get("type", "")).lower()
            required = kind == "required" or (not kind and d.get("mandatory") is True)
            if not required and kind != "incompatible":
                continue
            ln = _line_of(lines, _q(d["modId"]), head) or head
            dep = Dep(d["modId"], _alts(d.get("versionRange")), f"{at}:{ln or 1}", "maven")
            (mod.breaks if kind == "incompatible" else mod.depends).append(dep)
        out.append(mod)
    return out


def _archive(path: Path, label: str) -> Source:
    src = Source(label, "archive")
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            for n in names:
                if n.endswith("/"):
                    continue
                rt = _root_of(n)
                if rt is not None and not rt[0]:
                    src.files[rt[1]] = f"{label}!/{n}"
                loader = _MANIFESTS.get(n)
                if loader:
                    src.mods += parse_manifest(z.read(n).decode("utf-8", "replace"), loader, f"{label}!/{n}")
                if n.startswith(_NESTED) and n.endswith(".jar"):
                    try:
                        with zipfile.ZipFile(io.BytesIO(z.read(n))) as inner:
                            for mn, ld in _MANIFESTS.items():
                                if mn in inner.namelist():
                                    src.nested += parse_manifest(inner.read(mn).decode("utf-8", "replace"), ld,
                                                                 f"{label}!/{n}!/{mn}")
                    except (zipfile.BadZipFile, OSError, KeyError):
                        pass
    except (zipfile.BadZipFile, OSError):
        src.kind = "unreadable"
        return src

    def read(res: str) -> bytes | None:
        try:
            with zipfile.ZipFile(path) as z:
                return z.read(res)
        except (zipfile.BadZipFile, OSError, KeyError):
            return None

    src.read = read
    return src


def _tree(base: Path, rels: list[str], label_of, kind: str) -> list[Source]:
    """Sources of a folder: its files grouped by pack root (``rels`` relative to ``base``)."""
    by_root: dict[str, Source] = {}

    def source(root: str) -> Source:
        s = by_root.get(root)
        if s is None:
            s = by_root[root] = Source(label_of(root), kind)
            s.read = lambda res, r=root: _read_file(base / r / res if r else base / res)
        return s

    for rel in rels:
        if any(p in _SKIP for p in rel.split("/")[:-1]):
            continue
        rt = _root_of(rel)
        if rt is not None:
            source(rt[0]).files[rt[1]] = label_of(rel)
            continue
        mr = _manifest_root(rel)
        if mr is not None:
            text = _read_file(base / rel)
            if text is not None:
                source(mr[0]).mods += parse_manifest(text.decode("utf-8", "replace"), mr[1], label_of(rel))
    return list(by_root.values())


def _read_file(p: Path) -> bytes | None:
    try:
        return p.read_bytes()
    except OSError:
        return None


def sources(repo: Path, with_paths: list[str] | None = None) -> list[Source]:
    """The repository's packs, then those of each ``--with`` path (a folder's jars and zips one source each)."""
    from verinoda.snapshot import listed_files

    repo = Path(repo)
    out = _tree(repo, listed_files(repo), lambda r: r or ".", "repo")
    for given in with_paths or []:
        p = Path(given)
        label = p.as_posix()
        if p.is_file():
            out.append(_archive(p, label))
            continue
        if not p.is_dir():
            out.append(Source(label, "unreadable"))
            continue
        rels = []
        for f in sorted(p.rglob("*")):
            rel = f.relative_to(p).as_posix()
            if not f.is_file() or any(x in _SKIP for x in rel.split("/")[:-1]):
                continue
            if f.suffix.lower() in (".jar", ".zip"):
                out.append(_archive(f, f"{label}/{rel}"))
            else:
                rels.append(rel)
        out += _tree(p, rels, lambda r, lb=label: f"{lb}/{r}" if r else lb, "dir")
    return out


def _merged(res: str) -> bool:
    """Files the game merges across packs rather than replacing (tags are checked for ``replace`` apart)."""
    parts = res.split("/")
    return (parts[0] == "assets" and len(parts) > 2 and parts[2] in ("lang", "atlases")) or \
        res.endswith(("/sounds.json", "pack.mcmeta", "pack.png"))


def _replaces(data: bytes | None) -> bool:
    try:
        obj = json.loads((data or b"").decode("utf-8", "replace"))
    except ValueError:
        return False
    return isinstance(obj, dict) and obj.get("replace") is True


def _apart(a: Source, b: Source) -> bool:
    """True when two sources never load together: builds of the same mod id, or mods of different loaders."""
    ia, ib = {m.id for m in a.mods}, {m.id for m in b.mods}
    if ia & ib:
        return True
    la = {_LOADER_FAMILY[m.loader] for m in a.mods}
    lb = {_LOADER_FAMILY[m.loader] for m in b.mods}
    return bool(la and lb and not la & lb)


def collisions(srcs: list[Source]) -> tuple[list[dict], list[dict]]:
    """``(collisions, copies)``: resource paths two sources that may load together ship with different bytes
    (every such source listed), and paths shipped in several places with the same bytes."""
    where: dict[str, list[Source]] = {}
    for s in srcs:
        for res in s.files:
            where.setdefault(res, []).append(s)
    found, copies = [], []
    for res, ss in sorted(where.items()):
        if len(ss) < 2 or _merged(res):
            continue
        blobs = [s.read(res) if s.read else None for s in ss]
        if "/tags/" in res and not any(_replaces(b) for b in blobs):
            continue
        digest = [hashlib.sha1(b).hexdigest() if b is not None else None for b in blobs]
        involved: set[int] = set()
        for i in range(len(ss)):
            for j in range(i + 1, len(ss)):
                if digest[i] and digest[j] and digest[i] != digest[j] and not _apart(ss[i], ss[j]):
                    involved |= {i, j}
        row = {"path": res, "sources": [{"source": ss[i].label, "at": ss[i].files[res],
                                         **({"mod": ss[i].mods[0].id} if ss[i].mods else {})}
                                        for i in sorted(involved or range(len(ss)))]}
        if involved:
            found.append({**row, "status": "verified"})
        elif len(set(digest)) == 1 and digest[0]:
            copies.append(row)
    return found, copies


def _ver(s: str) -> tuple[tuple[int, ...], str] | None:
    core, _, pre = s.strip().lstrip("vV").split("+", 1)[0].partition("-")
    nums = core.split(".")
    if not all(n.isdigit() for n in nums):
        return None
    return tuple(int(n) for n in nums), pre


def _cmp(a: tuple[tuple[int, ...], str], b: tuple[tuple[int, ...], str]) -> int:
    n = max(len(a[0]), len(b[0]))
    x, y = a[0] + (0,) * (n - len(a[0])), b[0] + (0,) * (n - len(b[0]))
    if x != y:
        return -1 if x < y else 1
    if a[1] == b[1]:
        return 0
    if not a[1] or not b[1]:
        return 1 if not a[1] else -1  # a release is above its pre-releases
    return -1 if a[1] < b[1] else 1


def _semver_term(term: str, v) -> bool | None:
    m = re.fullmatch(r"(>=|<=|>|<|=|\^|~)?\s*(\S+)", term.strip())
    if not m:
        return None
    op, ref = m.group(1) or "", m.group(2)
    if ref in ("*", "x", "X"):
        return True
    if re.search(r"\.[xX*]$", ref) and not op:
        pref = _ver(re.sub(r"(\.[xX*])+$", "", ref))
        return None if pref is None else v[0][:len(pref[0])] == pref[0]
    r = _ver(ref)
    if r is None:
        return None
    c = _cmp(v, r)
    if op in ("", "="):
        return c == 0
    if op in (">=", ">", "<=", "<"):
        return {">=": c >= 0, ">": c > 0, "<=": c <= 0, "<": c < 0}[op]
    if c < 0:
        return False
    if op == "^":  # same major (0.x: same minor)
        keep = 2 if r[0] and r[0][0] == 0 and len(r[0]) > 1 else 1
    else:          # ~: same minor
        keep = 2
    return v[0][:keep] == (r[0] + (0, 0))[:keep]


def _maven(spec: str, v) -> bool | None:
    spec = spec.strip()
    if not spec.startswith(("[", "(")):
        return True if _ver(spec) is not None else None  # a bare version is a recommendation, not a bound
    any_ok = False
    for m in re.finditer(r"([\[(])\s*([^,\])]*?)\s*(?:(,)\s*([^\])]*?)\s*)?([\])])", spec):
        lo_inc, lo, comma, hi, hi_inc = m.group(1) == "[", m.group(2), m.group(3), m.group(4), m.group(5) == "]"
        if not comma:  # [1.0] exact
            r = _ver(lo)
            if r is None:
                return None
            any_ok |= _cmp(v, r) == 0
            continue
        ok = True
        for bound, inc, low in ((lo, lo_inc, True), (hi, hi_inc, False)):
            if not bound:
                continue
            r = _ver(bound)
            if r is None:
                return None
            c = _cmp(v, r)
            ok &= (c > 0 or (inc and c == 0)) if low else (c < 0 or (inc and c == 0))
        any_ok |= ok
    return any_ok


def satisfies(version: str, wants: list[str], style: str) -> bool | None:
    """Whether ``version`` is in the range (alternatives ORed; a Fabric alternative's space-separated terms
    ANDed); None when the version or the range cannot be read."""
    if not wants:
        return True
    v = _ver(version)
    if v is None:
        return None
    results = []
    for alt in wants:
        if style == "maven":
            results.append(_maven(alt, v))
        else:
            terms = [_semver_term(t, v) for t in alt.split()] or [True]
            results.append(None if None in terms else all(terms))
    if any(r is True for r in results):
        return True
    return None if None in results else False


def dependencies(srcs: list[Source], checked: bool) -> dict:
    """``{"problems", "external", "unchecked"}``: required mods the set lacks (only when ``checked``: the set was
    given with ``--with``), versions outside a declared range, mods the set holds that another declares it breaks;
    without ``--with`` a dependency the repository does not provide is external, not missing."""
    provided: dict[str, list[tuple[Mod, Source]]] = {}
    for s in srcs:
        for m in s.mods + s.nested:
            for pid in [m.id, *m.provides]:
                provided.setdefault(pid, []).append((m, s))
    problems, external, unchecked = [], [], []
    for s in srcs:
        for mod in s.mods:
            for d in mod.depends:
                if d.id in PLATFORM:
                    continue
                wants = " || ".join(d.wants) or "any"
                have = provided.get(d.id)
                row = {"mod": mod.id, "dependency": d.id, "wants": wants, "at": d.at}
                if not have:
                    if checked:
                        problems.append({"kind": "missing", **row, "status": "strong_inference"})
                    else:
                        external.append(row)
                    continue
                fits = [satisfies(m.version, d.wants, d.style) if m.version else None for m, _ in have]
                if True in fits:
                    continue
                found = ", ".join(f"{m.version or 'version unknown'} ({src.label})" for m, src in have)
                if False in fits and None not in fits:
                    problems.append({"kind": "version", **row, "found": found, "status": "strong_inference"})
                elif d.wants:
                    unchecked.append({**row, "found": found})
            for d in mod.breaks:
                for other, src in provided.get(d.id, []):
                    if other is mod:
                        continue
                    fit = satisfies(other.version, d.wants, d.style) if other.version else (None if d.wants else True)
                    if fit is False:
                        continue
                    problems.append({"kind": "breaks", "mod": mod.id, "dependency": d.id,
                                     "wants": " || ".join(d.wants) or "any", "at": d.at,
                                     "found": f"{other.version or 'version unknown'} ({src.label})",
                                     "status": "strong_inference" if fit else "weak_inference"})
    return {"problems": problems, "external": external, "unchecked": unchecked}


def check(repo: Path, with_paths: list[str] | None = None) -> dict:
    """The packs of the repository and of ``with_paths``, their collisions and the dependency problems."""
    srcs = sources(repo, with_paths)
    found, copies = collisions(srcs)
    return {"sources": [s.record() for s in srcs if s.files or s.mods or s.kind == "unreadable"],
            "collisions": found, "copies": copies, "dependencies": dependencies(srcs, bool(with_paths)),
            "with": list(with_paths or [])}


def collision_line(r: dict) -> str:
    """``data/ns/recipe/x.json: a/data/ns/recipe/x.json (moda) <> b.jar!/data/ns/recipe/x.json (modb)``."""
    return f"{r['path']}: " + " <> ".join(f"{s['at']}" + (f" ({s['mod']})" if s.get("mod") else "")
                                          for s in r["sources"])


def dependency_line(r: dict) -> str:
    what = {"missing": "no mod in the set provides it",
            "version": f"found {r.get('found')}",
            "breaks": f"declared incompatible, and the set holds {r.get('found')}"}[r["kind"]]
    verb = "breaks with" if r["kind"] == "breaks" else "needs"
    return f"{r['mod']} {verb} {r['dependency']} {r['wants']}: {what} ({r['at']})"


def render(res: dict) -> list[str]:
    """The lines of ``datapack packs``."""
    out = [f"{len(res['sources'])} pack(s) and mod(s)" + (f" (with {', '.join(res['with'])})" if res["with"] else
                                                          " in the repository")]
    for s in res["sources"]:
        mods = f" - {', '.join(s['mods'])}" if s["mods"] else ""
        bundles = f"; bundles {len(s['bundles'])}" if s.get("bundles") else ""
        out.append(f"  {s['source']} ({s['kind']}, {s['resources']} resource file(s){bundles}){mods}")
    out.append(f"resource collisions ({len(res['collisions'])}):")
    out += ["  " + collision_line(r) for r in res["collisions"][:40]]
    if len(res["collisions"]) > 40:
        out.append(f"  (+{len(res['collisions']) - 40} more: --json)")
    if res["copies"]:
        out.append(f"same file with the same bytes in several places ({len(res['copies'])}, not a collision): "
                   + ", ".join(r["path"] for r in res["copies"][:3]) + (" ..." if len(res["copies"]) > 3 else ""))
    dep = res["dependencies"]
    out.append(f"mod dependencies not met ({len(dep['problems'])}):")
    out += ["  " + dependency_line(r) for r in dep["problems"]]
    if dep["external"]:
        out.append(external_line(dep["external"]))
    for r in dep["unchecked"]:
        out.append(f"  unchecked: {r['mod']} needs {r['dependency']} {r['wants']}, found {r['found']} ({r['at']})")
    return out


def external_line(rows: list[dict]) -> str:
    names = sorted({r["dependency"] for r in rows})
    return (f"dependencies outside the repository, not checked ({len(names)}; give --with the mods folder): "
            + ", ".join(names[:8]) + (" ..." if len(names) > 8 else ""))
