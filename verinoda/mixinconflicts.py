"""Mixins of several mods on the same target method, and the mod behind a failed injection.

Every Mixin a game loads is applied to the same class: two mods that ``@Overwrite`` one method, ``@Redirect`` one
call or change one constant clash, and neither mod's sources show it. Here the project's own Mixins
(:func:`verinoda.mixincheck.read_mixins`) and those of the mod jars found locally are read side by side:

* the jars of the build's classpath (:func:`verinoda.jvmclass.discover`: Loom's remapped mod dependencies),
  ``.gradle/loom-cache/remapped_mods``, the ``run/mods``, ``runs/*/mods`` and ``mods`` folders of the build,
  and the jars or folders given with ``--with``; nested jars (``META-INF/jars``, ``META-INF/jarjar``) are mods
  of their own. Nothing is downloaded.
* A jar's Mixins are the classes its Mixin configs list (the configs ``fabric.mod.json`` / ``quilt.mod.json`` /
  ``mods.toml`` ``[[mixins]]`` / ``MANIFEST.MF`` ``MixinConfigs`` name), read from their class files
  (:func:`verinoda.jvmclass.class_annotations`); a selector is mapped through the config's refmap when it has one.

Two Mixins of different mods (of the same loader family) whose selectors name the same method of the same class
are a pair. What the pair does at run time is predicted from the annotations, so ``strong_inference``:
``conflict`` (one of them is skipped or fails: two ``@Overwrite``, an ``@Overwrite`` and any injector, two
``@Redirect`` of one call), ``order_dependent`` (both apply and the result follows their order: a ``@Redirect``
and another injector on the call it replaces, two ``@ModifyConstant`` of one constant, two ``@ModifyVariable``
of one local, two ``@ModifyArg`` of one argument, a cancellable ``@Inject`` beside another at the same
``HEAD`` / ``RETURN``), else ``compatible`` (a shared target, listed once per method; MixinExtras' wrapping
injectors are taken to chain, also onto a call another Mixin redirects). A Mixin config ``plugin`` may
skip any Mixin at load time: a pair with one is marked, its code is never run. That both name the same
method is read from the files: ``statically_verified`` when both selectors carry the same descriptor,
``strong_inference`` when one names only the method (it matches every overload).

A log (``--log``: ``latest.log``, a crash report) is read for Mixin's failure lines (``InvalidInjectionException``,
``Critical injection failure``, ``@Redirect conflict``, ``Method overwrite conflict``, ``Mixin [...] FAILED``,
``Mixin apply for mod ... failed``): each names the Mixin (``config.json:Class``), observed with the line as
evidence, and its mod through the config that lists it (``statically_verified``, the manifest as evidence), else
the mod the line names (``observed``), else ``unknown``; the pairs found for that Mixin are its likely cause
(``strong_inference``).
"""
from __future__ import annotations

import io
import json
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from verinoda import jvmclass
from verinoda.mixincheck import parse_member, parse_selector

DEFAULT_PRIORITY = 1000                 # Mixin's default priority (of a Mixin and of a config's Mixins)
MAX_NESTED_DEPTH = 2
STATUS = "strong_inference"             # what a pair does at run time is predicted, never observed here
_NESTED = ("META-INF/jars/", "META-INF/jarjar/")
_LOADER_FAMILY = {"fabric": "fabric", "quilt": "fabric", "forge": "forge", "neoforge": "neoforge"}
_INJECTOR_PKGS = ("org/spongepowered/asm/mixin/injection/", "com/llamalad7/mixinextras/injector/")
_INJECTORS = {"Inject", "Redirect", "ModifyArg", "ModifyArgs", "ModifyVariable", "ModifyConstant",
              "ModifyExpressionValue", "ModifyReturnValue", "WrapOperation", "WrapWithCondition", "WrapMethod",
              "ModifyReceiver"}
# MixinExtras' wrapping injectors chain: several wrap one call, and they wrap a call another Mixin @Redirects
_CHAINING = {"WrapOperation", "WrapWithCondition", "ModifyExpressionValue", "ModifyReceiver", "ModifyReturnValue",
             "WrapMethod"}
_SITE_POINTS = ("INVOKE", "FIELD", "NEW")
_RETURNS = ("RETURN", "TAIL")
_INTERMEDIARY = re.compile(r"^(?:class|method|field)_\d+$")
_SRG = re.compile(r"^(?:[mf]_\d+_|func_\d+_\w*|field_\d+_\w*)$")
NEXT_JARS = ("pass the mods the game runs with: verinoda mixin-check --conflicts --with <mods folder or jar>, or "
             "run the Gradle (Loom) build once so its mod dependencies are on the classpath")


# -- mods, their configs and their Mixins ------------------------------------------------------------------

@dataclass
class Config:
    name: str
    evidence: str
    package: str = ""
    classes: list[str] = field(default_factory=list)
    priority: int | None = None          # mixinPriority: the default of its Mixins
    refmap: dict = field(default_factory=dict)
    plugin: str | None = None            # a config plugin may skip any of its Mixins at load time


@dataclass
class Mod:
    id: str
    evidence: str                        # the manifest that names it (or why there is none)
    source: str                          # "project" or the jar's label
    loader: str | None = None
    configs: dict[str, Config] = field(default_factory=dict)
    injections: list[dict] = field(default_factory=list)
    mixins: int = 0

    def record(self) -> dict:
        return {"mod": self.id, "source": self.source, "evidence": self.evidence, "loader": self.loader,
                "configs": sorted(self.configs), "mixins": self.mixins, "injectors": len(self.injections)}


def _manifest(name: str, text: str) -> tuple[str | None, str | None, list[str], int]:
    """``(mod id, loader, mixin configs named, line of the id)`` of a mod manifest."""
    lines = text.splitlines()

    def line_of(needle: str) -> int:
        return next((i for i, s in enumerate(lines, 1) if needle in s), 1)

    if name.endswith("MANIFEST.MF"):
        m = re.search(r"^MixinConfigs:\s*(.+(?:\r?\n [^\r\n]*)*)", text, re.M)
        cfgs = [c.strip() for c in re.sub(r"\r?\n ", "", m.group(1)).split(",") if c.strip()] if m else []
        return None, None, cfgs, 1
    if name.endswith(".json"):
        try:
            data = json.JSONDecoder().raw_decode(text.lstrip("\ufeff \t\r\n"))[0]
        except ValueError:
            return None, None, [], 1
        if not isinstance(data, dict):
            return None, None, [], 1
        if name.endswith("quilt.mod.json"):
            ql = data.get("quilt_loader") if isinstance(data.get("quilt_loader"), dict) else {}
            mid, raw, loader = ql.get("id"), data.get("mixin"), "quilt"
        else:
            mid, raw, loader = data.get("id"), data.get("mixins"), "fabric"
        raw = raw if isinstance(raw, list) else [raw] if raw else []
        cfgs = [c.get("config") if isinstance(c, dict) else c for c in raw]
        mid = mid if isinstance(mid, str) and mid else None
        return mid, loader, [c for c in cfgs if isinstance(c, str)], line_of(f'"{mid}"') if mid else 1
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - py3.10
        import tomli as tomllib  # type: ignore[no-redef]
    try:
        data = tomllib.loads(text)
    except Exception:  # noqa: BLE001 - an unreadable manifest names nothing
        return None, None, [], 1
    mods = [m for m in (data.get("mods") or []) if isinstance(m, dict) and isinstance(m.get("modId"), str)]
    mid = mods[0]["modId"] if mods else None
    cfgs = [m.get("config") for m in (data.get("mixins") or []) if isinstance(m, dict)]
    loader = "neoforge" if name.endswith("neoforge.mods.toml") else "forge"
    return mid, loader, [c for c in cfgs if isinstance(c, str)], line_of(f'"{mid}"') if mid else 1


def _config(name: str, text: str, evidence: str) -> Config | None:
    """A Mixin config (``{"package": ..., "mixins": [...], "client": [...], "server": [...]}``), else None."""
    try:
        data = json.loads(text.lstrip("\ufeff"))
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("package"), str):
        return None
    classes = [c for key in ("mixins", "client", "server") for c in (data.get(key) or [])
               if isinstance(c, str)]
    prio, plugin = data.get("mixinPriority"), data.get("plugin")
    return Config(name, evidence, data["package"], list(dict.fromkeys(classes)),
                  prio if isinstance(prio, int) else None, plugin=plugin if isinstance(plugin, str) else None)


def _dotted(name: str) -> str:
    """``Lnet/a/B$C;``, ``net/a/B$C`` or ``net.a.B.C`` -> ``net.a.B.C``."""
    n = name.strip()
    if n.startswith("L") and n.endswith(";"):
        n = n[1:-1]
    return n.replace("/", ".").replace("$", ".")


def _simple_type(t: str | None) -> str:
    return (t or "").rsplit("/", 1)[-1].rstrip(";").rsplit(".", 1)[-1]


def _injection(mod: Mod, mixin: str, cfg: Config | None, kind: str, member: str, evidence: str, priority: int,
               priority_from: str, targets: list[str], selectors: list[str], values: dict, refmap: dict,
               unread: bool = False, desc: str | None = None) -> dict:
    return {"mod": mod.id, "mod_evidence": mod.evidence, "loader": mod.loader, "source": mod.source,
            "mixin": mixin, "config": cfg.name if cfg else None, "plugin": cfg.plugin if cfg else None,
            "kind": kind, "member": member, "evidence": evidence,
            "priority": priority, "priority_from": priority_from, "targets": targets, "selectors": selectors,
            "values": values, "refmap": refmap, "unread": unread, "overwrite_desc": desc}


def _plain(v):
    """A class file's annotation value in the shape the source reader gives: nested annotations by simple name."""
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, dict) and "type" in v:
        return {"type": _simple_type(v["type"]), "values": {k: _plain(x) for k, x in v["values"].items()}}
    if isinstance(v, dict) and "class" in v:
        return v["class"]
    return v


def _jar_mods(jar: Path | bytes, label: str, depth: int, notes: list[str], where: str) -> list[Mod]:
    """The mods of a jar (a file, or the bytes of a nested one: its own mod, then those of the jars it nests)
    with the Mixins its configs list. ``label`` starts the evidence (``x.jar!/...``), ``where`` says where the
    jar was found."""
    try:
        z = zipfile.ZipFile(io.BytesIO(jar) if isinstance(jar, bytes) else jar)
    except (zipfile.BadZipFile, OSError):
        notes.append(f"unreadable jar: {label}")
        return []
    out: list[Mod] = []
    with z:
        names = set(z.namelist())

        def text(n: str) -> str | None:
            try:
                return z.read(n).decode("utf-8", "replace")
            except (KeyError, OSError, zipfile.BadZipFile):
                return None

        mid = loader = None
        mid_at = f"{label}!/"
        listed: list[str] = []
        for n in ("fabric.mod.json", "quilt.mod.json", "META-INF/neoforge.mods.toml", "META-INF/mods.toml",
                  "META-INF/MANIFEST.MF"):
            t = text(n) if n in names else None
            if t is None:
                continue
            i, ld, cfgs, ln = _manifest(n, t)
            if i and mid is None:
                mid, loader, mid_at = i, ld, f"{label}!/{n}:{ln}"
            listed += [c for c in cfgs if c not in listed]
        configs = [c for c in listed if c in names]
        if not listed and loader not in ("fabric", "quilt"):
            # a config no manifest names: a Forge build may pass it in a way not read here
            configs = [n for n in sorted(names) if "/" not in n and n.endswith(".json")
                       and re.search(r"mixin", n, re.I)]
        if configs:
            mod = Mod(mid or Path(label.rsplit("!/", 1)[-1]).stem, mid_at if mid else
                      f"{label} names no mod id: the jar's name is used", where, loader)
            for cname in configs:
                cfg = _config(cname, text(cname) or "", f"{label}!/{cname}")
                if cfg is None:
                    continue
                if cname not in listed:
                    cfg.evidence += " (named by no manifest read)"
                rm = (json.loads(text(cname) or "{}") or {}).get("refmap")
                if isinstance(rm, str) and rm in names:
                    try:
                        cfg.refmap = (json.loads(text(rm) or "{}").get("mappings") or {})
                    except (ValueError, AttributeError):
                        notes.append(f"unreadable refmap: {label}!/{rm}")
                mod.configs[cname] = cfg
                for cls in cfg.classes:
                    entry = f"{cfg.package}.{cls}".replace(".", "/") + ".class"
                    if entry not in names:
                        notes.append(f"{label}!/{cname} lists {cls}, which the jar does not hold")
                        continue
                    _read_class(mod, cfg, z.read(entry), f"{label}!/{entry}")
            out.append(mod)
        if depth < MAX_NESTED_DEPTH:
            for n in sorted(names):
                if n.startswith(_NESTED) and n.endswith(".jar"):
                    out += _jar_mods(z.read(n), f"{label}!/{n}", depth + 1, notes, f"{where}, nested {n}")
    return out


def _read_class(mod: Mod, cfg: Config, data: bytes, evidence: str) -> None:
    got = jvmclass.class_annotations(data)
    if not got or not got.get("name"):
        return
    mixin_ann = next((a for a in got["annotations"] if a["type"] == "Lorg/spongepowered/asm/mixin/Mixin;"), None)
    if mixin_ann is None:
        return
    mod.mixins += 1
    name = got["name"]
    vals = mixin_ann["values"]
    targets = [_dotted(v["class"]) for v in (vals.get("value") or []) if isinstance(v, dict) and "class" in v]
    refmap = cfg.refmap.get(name) or {}
    targets += [_dotted(refmap.get(s, s)) for s in (vals.get("targets") or []) if isinstance(s, str)]
    prio, src = (vals["priority"], "@Mixin") if isinstance(vals.get("priority"), int) else \
        (cfg.priority, "config mixinPriority") if cfg.priority is not None else (DEFAULT_PRIORITY, "default")
    for mname, mdesc, anns in got["methods"]:
        for a in anns:
            t = a["type"]
            kind = _simple_type(t)
            if t == "Lorg/spongepowered/asm/mixin/Overwrite;":
                sels, desc = [mname], mdesc
            elif kind in _INJECTORS and t.startswith(tuple("L" + p for p in _INJECTOR_PKGS)):
                raw = a["values"].get("method") or []
                sels, desc = [s for s in (raw if isinstance(raw, list) else [raw]) if isinstance(s, str)], None
            else:
                continue
            mod.injections.append(_injection(
                mod, _dotted(name), cfg, kind, mname, evidence, prio, src, targets, sels,
                {k: _plain(v) for k, v in a["values"].items()}, refmap, desc=desc))


# -- the project's own Mixins ------------------------------------------------------------------------------

def _project_mods(repo: Path, paths: list[str] | None, notes: list[str]) -> tuple[list[Mod], int]:
    """The project's mods (one per manifest id; ``project`` when none names its configs) and how many Mixin
    classes its sources hold."""
    from verinoda.mixincheck import _SKIP, mixin_files, read_mixins
    from verinoda.snapshot import listed_files

    files = [Path(p).as_posix() for p in listed_files(repo)]
    kept = [f for f in files if not any(x in _SKIP for x in f.split("/")[:-1])]
    manifests: list[tuple[str, str | None, list[str], str]] = []
    configs: dict[str, Config] = {}
    for rel in kept:
        base = rel.rsplit("/", 1)[-1]
        if base in ("fabric.mod.json", "quilt.mod.json", "mods.toml", "neoforge.mods.toml") or \
                (rel.endswith(".json") and "mixin" in base.lower()):
            try:
                text = (repo / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if base in ("fabric.mod.json", "quilt.mod.json", "mods.toml", "neoforge.mods.toml"):
                mid, loader, cfgs, ln = _manifest(base, text)
                if mid:
                    manifests.append((mid, loader, cfgs, f"{rel}:{ln}"))
            else:
                cfg = _config(base, text, rel)
                if cfg is not None:
                    configs.setdefault(base, cfg)
    ids = list(dict.fromkeys(m[0] for m in manifests))
    mods: dict[str, Mod] = {}

    def mod_for(config: str | None) -> Mod:
        hit = next((m for m in manifests if config and config in m[2]), None)
        if hit is None and len(ids) == 1:
            hit = manifests[0]
        if hit is None:
            key = "project"
            if key not in mods:
                mods[key] = Mod(key, "no manifest of the project names this Mixin's config", "project")
            return mods[key]
        if hit[0] not in mods:
            mods[hit[0]] = Mod(hit[0], hit[3], "project", hit[1])
        return mods[hit[0]]

    by_class: dict[str, Config] = {f"{c.package}.{x}": c for c in configs.values() for x in c.classes}
    targets = [p for p in paths] if paths else mixin_files(repo)
    count = 0
    for rel in targets:
        p = Path(rel) if Path(rel).is_absolute() else repo / rel
        try:
            src = p.read_bytes()
            rel = p.resolve().relative_to(repo).as_posix()
        except OSError as exc:
            notes.append(f"{rel} is not readable: {exc.__class__.__name__}")
            continue
        except ValueError:
            rel = p.as_posix()
        for mc in read_mixins(rel, src):
            cfg = by_class.get(mc.name)
            mod = mod_for(cfg.name if cfg else None)
            if cfg:
                mod.configs.setdefault(cfg.name, cfg)
            mod.mixins += 1
            count += 1
            tgts = list(dict.fromkeys(_dotted(c) for names in mc.target_names for c in names))
            prio, src_ = (mc.priority, "@Mixin") if mc.priority is not None else \
                (cfg.priority, "config mixinPriority") if cfg and cfg.priority is not None else \
                (DEFAULT_PRIORITY, "default")
            for inj in mc.injections:
                mod.injections.append(_injection(
                    mod, mc.name, cfg, inj["kind"], inj["member"], f"{rel}:{inj['line']}",
                    prio, src_, tgts, inj["selectors"], inj["values"], {}, unread=inj["unread"]))
    for cname, cfg in configs.items():
        if not any(cname in m.configs for m in mods.values()):
            mod_for(cname).configs.setdefault(cname, cfg)
    return list(mods.values()), count


# -- where the mod jars are --------------------------------------------------------------------------------

def mod_jars(repo: Path, roots: list[Path], config: dict | None, with_paths: list[str] | None
             ) -> tuple[list[tuple[Path, str]], list[str]]:
    """``(jar, how it was found)`` of every jar that may hold another mod: the classpath, Loom's remapped mods,
    the mods folders of each build and the paths given."""
    found: list[tuple[Path, str]] = []
    notes: list[str] = []
    for root in roots:
        cp = jvmclass.discover(root, config if root == repo else None)
        found += [(j, f"classpath ({cp.source})") for j in cp.jars]
        found += [(j, "Loom's remapped mods") for j in sorted((root / ".gradle" / "loom-cache" / "remapped_mods")
                                                               .rglob("*.jar"))]
        for pattern in ("run/mods/*.jar", "runs/*/mods/*.jar", "mods/*.jar"):
            found += [(j, "mods folder") for j in sorted(root.glob(pattern))]
    for given in with_paths or []:
        p = Path(given)
        if p.is_file():
            found.append((p, "--with"))
        elif p.is_dir():
            found += [(j, "--with") for j in sorted(p.rglob("*.jar"))]
        else:
            notes.append(f"--with {given}: no such file or folder")
    seen: set[str] = set()
    out = []
    for j, how in found:
        key = str(j.resolve()).lower()
        if key not in seen and j.is_file():
            seen.add(key)
            out.append((j, how))
    return out, notes


# -- pairs -------------------------------------------------------------------------------------------------

def _namespace(cls: str, name: str | None) -> str:
    simple = cls.rsplit(".", 1)[-1]
    if _INTERMEDIARY.match(simple) or (name and _INTERMEDIARY.match(name)):
        return "intermediary"
    if name and _SRG.match(name):
        return "srg"
    return "named"


def _ats(inj: dict) -> list[dict]:
    """The injector's ``@At`` points: ``{"value", "owner", "name", "desc", "ordinal", "written"}`` (targets
    mapped through the refmap)."""
    raw = inj["values"].get("at")
    out = []
    for a in raw if isinstance(raw, list) else [raw] if raw else []:
        if not isinstance(a, dict):
            continue
        v = a.get("values") or {}
        point = str(v.get("value") or "").upper()
        point = "INVOKE" if point.startswith("INVOKE") else point
        tgt = v.get("target") if isinstance(v.get("target"), str) else None
        resolved = inj["refmap"].get(tgt, tgt) if tgt else None
        owner, name, desc = parse_member(resolved) if resolved else (None, None, None)
        out.append({"value": point, "owner": owner, "name": name, "desc": desc,
                    "ordinal": v.get("ordinal") if isinstance(v.get("ordinal"), int) else None,
                    "written": tgt})
    return out


def _same_site(p: dict, q: dict) -> bool:
    if p["value"] != q["value"] and not (p["value"] in _RETURNS and q["value"] in _RETURNS):
        return False
    if p["ordinal"] is not None and q["ordinal"] is not None and p["ordinal"] != q["ordinal"]:
        return False
    if p["value"] in _SITE_POINTS:
        for k in ("owner", "name", "desc"):
            if p[k] and q[k] and _dotted(p[k]) != _dotted(q[k]):
                return False
    return True


def _site_text(at: dict) -> str:
    return f"@At({at['value']}" + (f", {at['written']}" if at.get("written") else "") + \
        (f", ordinal {at['ordinal']}" if at.get("ordinal") is not None else "") + ")"


def _shared_site(a: dict, b: dict, points: tuple[str, ...] | None = None) -> dict | None:
    for p in a["_ats"]:
        for q in b["_ats"]:
            if (points is None or p["value"] in points) and _same_site(p, q):
                return p
    return None


def _slot(inj: dict) -> tuple:
    v = inj["values"]
    return tuple(v.get(k) if isinstance(v.get(k), (int, str)) else None for k in ("ordinal", "index", "name"))


def _constants(inj: dict) -> str:
    raw = inj["values"].get("constant")
    items = raw if isinstance(raw, list) else [raw] if raw else []
    parts = []
    for c in items:
        vals = (c.get("values") or {}) if isinstance(c, dict) else {}
        parts.append(",".join(f"{k}={round(vals[k], 6) if isinstance(vals[k], float) else vals[k]!r}"
                              for k in sorted(vals)))
    return ";".join(sorted(parts))


def classify(a: dict, b: dict) -> tuple[str, str]:
    """``(severity, why)`` of two Mixins of different mods on the same target method."""
    ka, kb = a["kind"], b["kind"]
    pa, pb = f"{a['mod']} {a['priority']}", f"{b['mod']} {b['priority']}"
    if ka == kb == "Overwrite":
        return "conflict", ("both replace the method's body and only one survives: Mixin applies the lower priority "
                            f"first and keeps the body of higher priority ({pa}, {pb}), logging 'Method overwrite "
                            "conflict' for the other; with equal priorities the order the configs load in decides")
    if "Overwrite" in (ka, kb):
        ow, other = (a, b) if ka == "Overwrite" else (b, a)
        site = next((p for p in other["_ats"] if p["value"] in _SITE_POINTS + ("CONSTANT", "LOAD", "STORE", "JUMP")),
                    None)
        if site or other["kind"] in ("Redirect", "ModifyArg", "ModifyArgs", "ModifyConstant", "WrapOperation",
                                     "WrapWithCondition", "ModifyExpressionValue", "ModifyReceiver"):
            what = _site_text(site) if site else "instruction"
            return "conflict", (f"{ow['mod']}'s @Overwrite replaces the body {other['mod']}'s @{other['kind']} was "
                                f"written against: the {what} it looks for may not be in the new body, and the "
                                "injection then fails (a crash when it is required)")
        return "conflict", (f"{ow['mod']}'s @Overwrite replaces the body; {other['mod']}'s @{other['kind']} still "
                            "applies, but into the replacing code, not the code it was written against")
    if ka == kb == "Redirect":
        site = _shared_site(a, b, _SITE_POINTS)
        if site:
            return "conflict", (f"both @Redirect the same {_site_text(site)}: a call is redirected once; Mixin keeps "
                                f"one by priority ({pa}, {pb}) and skips the other with '@Redirect conflict' (a "
                                "crash when the skipped one is required)")
    if "Redirect" in (ka, kb):
        r, o = (a, b) if ka == "Redirect" else (b, a)
        site = _shared_site(r, o, _SITE_POINTS)
        if site and o["kind"] in _CHAINING:
            return "compatible", (f"{o['mod']}'s @{o['kind']} (MixinExtras) wraps the {_site_text(site)} that "
                                  f"{r['mod']}'s @Redirect replaces: MixinExtras' wrapping injectors chain onto a "
                                  "redirected call")
        if site and o["kind"] != "Redirect":
            return "order_dependent", (f"{r['mod']}'s @Redirect replaces the {_site_text(site)} that {o['mod']}'s "
                                       f"@{o['kind']} also acts on: whether that one still finds it depends on the "
                                       f"order Mixin applies them (injector order, priority {pa}, {pb})")
    if ka == kb == "ModifyConstant" and _constants(a) and _constants(a) == _constants(b):
        spec = _constants(a)
        return "order_dependent", (f"both change the same constant ({spec}): the result follows the order they "
                                   f"apply in (priority {pa}, {pb}), and the later may no longer find it")
    if ka == kb == "ModifyVariable" and any(_slot(a)) and _slot(a) == _slot(b) and _shared_site(a, b):
        return "order_dependent", (f"both change the same local (ordinal, index, name = {_slot(a)}) at the same "
                                   f"point: the later one (priority {pa}, {pb}) sees the value the earlier set")
    if ka == kb == "ModifyArg" and isinstance(a["values"].get("index"), int) \
            and a["values"].get("index") == b["values"].get("index") and _shared_site(a, b, _SITE_POINTS):
        return "order_dependent", (f"both change argument {a['values'].get('index')} of the same call: the later "
                                   f"one (priority {pa}, {pb}) sees the value the earlier returned")
    if ka == kb == "Inject" and (a["values"].get("cancellable") is True or b["values"].get("cancellable") is True):
        site = _shared_site(a, b, ("HEAD",) + _RETURNS)
        if site:
            who = " and ".join(x["mod"] for x in (a, b) if x["values"].get("cancellable") is True)
            return "order_dependent", (f"both inject at {site['value']} and {who} can cancel: a cancel returns "
                                       "before the handlers placed after it run, so whether the other mod's "
                                       f"handler runs follows the order (priority {pa}, {pb})")
    return "compatible", f"same method, kinds that coexist (@{ka}, @{kb})"


def _side(x: dict) -> dict:
    out = {"mod": x["mod"], "source": x["source"], "mod_evidence": x["mod_evidence"], "mixin": x["mixin"],
           "config": x["config"],
           "kind": f"@{x['kind']}", "member": x["member"], "evidence": x["evidence"], "priority": x["priority"],
           "priority_from": x["priority_from"], "selector": x["_written"]}
    if x["_resolved"] != x["_written"]:
        out["selector_refmap"] = x["_resolved"]
    if x["_ats"]:
        out["at"] = [_site_text(p) for p in x["_ats"]]
    if x["values"].get("cancellable") is True:
        out["cancellable"] = True
    if x.get("plugin"):
        out["config_plugin"] = x["plugin"]
    return out


def _keys(inj: dict, not_compared: list[dict]) -> list[dict]:
    """One entry per (target class, selector) of an injection, with the selector parsed; a selector this reader
    cannot compare goes to ``not_compared``."""
    out = []
    if inj["unread"]:
        not_compared.append({"at": inj["evidence"], "mixin": inj["mixin"], "kind": f"@{inj['kind']}",
                             "why": "a method selector is not a constant string this reader resolves"})
    for sel in inj["selectors"]:
        resolved = inj["refmap"].get(sel, sel)
        parsed = parse_selector(resolved)
        if parsed is None or parsed[0] is None:
            why = (f"the selector {sel} names every method of the target" if parsed is not None else
                   f"the selector {sel} is a regular expression")
            not_compared.append({"at": inj["evidence"], "mixin": inj["mixin"], "kind": f"@{inj['kind']}",
                                 "why": why + ", not compared here"})
            continue
        name, desc = parsed
        if inj["kind"] == "Overwrite":
            desc = inj["overwrite_desc"]
        owner = parse_member(re.sub(r"\s+", "", resolved))[0]
        for t in inj["targets"]:
            if owner and _dotted(owner) != t:
                continue
            out.append(dict(inj, _cls=t, _name=name, _desc=desc, _written=sel, _resolved=resolved,
                            _ats=_ats(inj)))
    return out


def pairs(mods: list[Mod]) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    """``(conflicts, shared, not_compared, namespaces)``: the pairs of Mixins of different mods on one method."""
    not_compared: list[dict] = []
    groups: dict[tuple[str, str], list[dict]] = {}
    spaces: dict[str, int] = {}
    for m in mods:
        for inj in m.injections:
            for k in _keys(inj, not_compared):
                groups.setdefault((k["_cls"], k["_name"]), []).append(k)
                ns = _namespace(k["_cls"], k["_name"])
                spaces[ns] = spaces.get(ns, 0) + 1
    conflicts, shared = [], []
    for (cls, name), rows in sorted(groups.items()):
        if len({r["mod"] for r in rows}) < 2:
            continue
        clashing, compatible = [], []
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                if a["mod"] == b["mod"]:
                    continue
                fa, fb = _LOADER_FAMILY.get(a["loader"] or ""), _LOADER_FAMILY.get(b["loader"] or "")
                if fa and fb and fa != fb:
                    continue    # mods of different loaders never load together
                if a["_desc"] and b["_desc"] and a["_desc"] != b["_desc"]:
                    continue
                sev, why = classify(a, b)
                same = "statically_verified" if a["_desc"] and a["_desc"] == b["_desc"] else STATUS
                desc = a["_desc"] or b["_desc"] or ""
                row = {"severity": sev, "status": STATUS, "target": f"{cls}.{name}{desc}", "why": why,
                       "same_target": same, "mixins": [_side(a), _side(b)]}
                if same != "statically_verified":
                    row["same_target_why"] = ("a selector without a descriptor matches every overload of "
                                              f"{name}; the target's class file is not read to tell them apart")
                plugins = sorted({x["plugin"] for x in (a, b) if x.get("plugin")})
                if plugins:
                    row["plugin_note"] = (f"a Mixin config plugin ({', '.join(plugins)}) decides at load time "
                                          "whether its Mixins apply, and may skip this one when the other mod is "
                                          "present: its code is not run or read here")
                (compatible if sev == "compatible" else clashing).append(row)
        conflicts += clashing
        if compatible and not clashing:
            seen, sides = set(), []
            for r in compatible:
                for s in r["mixins"]:
                    key = (s["mod"], s["mixin"], s["member"], s["kind"])
                    if key not in seen:
                        seen.add(key)
                        sides.append(s)
            shared.append({"severity": "compatible", "status": STATUS, "target": f"{cls}.{name}",
                           "why": "same method, kinds that coexist: " + ", ".join(
                               sorted({s["kind"] for s in sides})),
                           "mods": sorted({s["mod"] for s in sides}), "mixins": sides})
    order = {"conflict": 0, "order_dependent": 1}
    conflicts.sort(key=lambda r: (order[r["severity"]], r["target"]))
    return conflicts, shared, not_compared, sorted(spaces)


# -- the log -----------------------------------------------------------------------------------------------

_REF = r"(?P<{0}cfg>[\w.-]+?\.json):(?P<{0}cls>[\w$]+(?:\.[\w$]+)*)(?:->@(?P<{0}kind>\w+)::(?P<{0}h>[\w$<>]+))?"
_LOG_RULES = (
    ("redirect_conflict", re.compile(r"@Redirect conflict\. Skipping " + _REF.format("a") + r"\S* with priority "
                                     r"(?P<ap>-?\d+), already redirected by " + _REF.format("b") +
                                     r"\S* with priority (?P<bp>-?\d+)")),
    ("overwrite_conflict", re.compile(r"Method overwrite conflict for (?P<method>\S+) in " + _REF.format("a") +
                                      r", previously written by (?P<bfqn>[\w.$]+)")),
    ("apply_failed", re.compile(r"Mixin \[" + _REF.format("a") + r"\] from phase \[\w+\] in config \[[^\]]+\] "
                                r"FAILED during (?P<phase>\w+)")),
    ("apply_failed", re.compile(r"Mixin apply for mod (?P<mod>[\w-]+) failed " + _REF.format("a") +
                                r":? from mod [\w-]+ -> (?P<target>[\w.$]+)")),
    ("injection_failed", re.compile(r"(?:InvalidInjectionException|InjectionError|InvalidMixinException|"
                                    r"Critical injection failure)")),
)
_ANY_REF = re.compile(_REF.format("a"))
_FROM_MOD = re.compile(r"\bfrom mod (?P<mod>[\w-]+)")
_TARGETS = re.compile(r"could not find any targets matching '(?P<sel>[^']+)' in (?:the target class )?'?"
                      r"(?P<target>[\w./$]*\w)")
_ON = re.compile(r"@(?P<kind>\w+) annotation on (?P<h>[\w$<>]+)")


def read_log(lines: list[str]) -> list[dict]:
    """Mixin's failure lines of a log, one row per Mixin (the first line that names it, later lines adding what
    they name)."""
    from verinoda.trace_log import _unprefixed

    rows: dict[tuple, dict] = {}
    for i, raw in enumerate(lines, 1):
        line = _unprefixed(raw).strip()
        for what, pat in _LOG_RULES:
            m = pat.search(line)
            if not m:
                continue
            g = m.groupdict()
            if what == "injection_failed":
                r = _ANY_REF.search(line)
                if not r:
                    break
                g = {**r.groupdict(), **g}
            key = (g["acfg"], g["acls"])
            row = rows.get(key)
            if row is None:
                row = rows[key] = {"what": what, "log_line": i, "evidence": line[:300], "status": "observed",
                                   "config": g["acfg"], "mixin_written": g["acls"], "lines": 0}
            row["lines"] += 1
            on = _ON.search(line)
            if g.get("akind") and "injector" not in row:
                row["injector"], row["handler"] = "@" + g["akind"], g["ah"]
            elif on and "injector" not in row:
                row["injector"], row["handler"] = "@" + on.group("kind"), on.group("h")
            fm = g.get("mod") or (_FROM_MOD.search(line).group("mod") if _FROM_MOD.search(line) else None)
            if fm and "log_mod" not in row:
                row["log_mod"], row["log_mod_line"] = fm, i
            tm = _TARGETS.search(line)
            if tm and "target" not in row:
                row["target"], row["selector"] = _dotted(tm.group("target")), tm.group("sel")
            elif g.get("target") and "target" not in row:
                row["target"] = g["target"]
            if what in ("redirect_conflict", "overwrite_conflict"):
                row["what"] = what
                row["other"] = ({"config": g["bcfg"], "mixin_written": g["bcls"], "priority": int(g["bp"])}
                                if what == "redirect_conflict" else {"mixin_written": g["bfqn"]})
                if what == "redirect_conflict":
                    row["priority"] = int(g["ap"])
                if g.get("method"):
                    row["method"] = g["method"]
            break
    return sorted(rows.values(), key=lambda r: r["log_line"])


def _who(ref: dict, mods: list[Mod]) -> None:
    """Fill ``ref`` (a Mixin the log names) with its class and mod from the configs read."""
    cfg_name, written = ref.get("config"), ref["mixin_written"]
    holders = [m for m in mods if cfg_name and cfg_name in m.configs]
    if len(holders) > 1:   # a config name several mods use: the one whose config lists the class
        holders = [m for m in holders if written in m.configs[cfg_name].classes] or holders
    if len(holders) > 1:
        ref.update(mixin=written, mod=None, mod_status="unknown", candidates=sorted(m.id for m in holders),
                   next=f"several mods read have a config named {cfg_name}; the log line does not tell which")
        return
    for m in holders:
        cfg = m.configs[cfg_name]
        ref.update(mixin=f"{cfg.package}.{written}", mod=m.id, mod_status="statically_verified",
                   mod_evidence=f"{cfg.evidence} (listed by {m.evidence})")
        return
    for m in mods:
        if not cfg_name and any(inj["mixin"] == _dotted(written) for inj in m.injections):
            ref.update(mixin=_dotted(written), mod=m.id, mod_status="statically_verified",
                       mod_evidence=next(inj["evidence"] for inj in m.injections if inj["mixin"] == _dotted(written)))
            return
    ref["mixin"] = _dotted(written) if not cfg_name else written


def failures(lines: list[str], mods: list[Mod], conflicts: list[dict], log_name: str) -> list[dict]:
    out = read_log(lines)
    for row in out:
        _who(row, mods)
        if row.get("mod_status") == "unknown" and row.get("log_mod"):
            row.pop("next", None)
            row.update(mod=row["log_mod"], mod_status="observed",
                       mod_evidence=f"{log_name}:{row['log_mod_line']} names the mod")
        if "mod" not in row:
            if row.get("log_mod"):
                row.update(mod=row["log_mod"], mod_status="observed",
                           mod_evidence=f"{log_name}:{row['log_mod_line']} names the mod")
            else:
                row.update(mod=None, mod_status="unknown", next=NEXT_JARS + (
                    f" (the config {row['config']} is in no mod read)" if row.get("config") else ""))
        row.pop("log_mod", None)
        row.pop("log_mod_line", None)
        row["evidence"] = f"{log_name}:{row['log_line']}: {row['evidence']}"
        if row.get("other"):
            _who(row["other"], mods)
        names = {row.get("mixin")} | ({row["other"].get("mixin")} if row.get("other") else set())
        related = [c for c in conflicts if any(s["mixin"] in names for s in c["mixins"])]
        if related:
            row["likely_cause"] = [{"target": c["target"], "severity": c["severity"], "status": STATUS,
                                    "mods": [s["mod"] for s in c["mixins"]], "why": c["why"]} for c in related[:5]]
    return out


# -- the command -------------------------------------------------------------------------------------------

def check(repo: Path, paths: list[str] | None = None, with_paths: list[str] | None = None,
          log: Path | None = None, config: dict | None = None) -> dict:
    from verinoda.mixincheck import mixin_files

    t0 = time.perf_counter()
    repo = Path(repo).resolve()
    notes: list[str] = []
    project, n_project = _project_mods(repo, paths, notes)
    roots = list(dict.fromkeys([repo] + [jvmclass.build_root(repo, repo / f) for f in mixin_files(repo)]))
    jars, jnotes = mod_jars(repo, roots, config, with_paths)
    notes += jnotes
    by_id = {m.id: m for m in project}
    others: list[Mod] = []
    nested_copies = 0
    for jar, how in jars:
        for m in _jar_mods(jar, jar.name, 0, notes, f"{jar.as_posix()} ({how})"):
            if m.id in by_id:
                prev = by_id[m.id]
                if any(f"!/{d}" in m.evidence for d in _NESTED) and prev.source != "project":
                    nested_copies += 1      # a library several mods bundle: the loader keeps one copy
                    continue
                notes.append(f"{m.evidence.split(':')[0]}: mod {m.id} is already read from "
                             + ("the project's sources" if prev.source == "project" else prev.source.split(" (")[0])
                             + "; this copy is skipped")
                continue
            by_id[m.id] = m
            others.append(m)
    if nested_copies:
        notes.append(f"{nested_copies} nested jar(s) hold a mod already read (a library several mods bundle; "
                     "the loader keeps one copy): read once")
    mods = project + others
    conflicts, shared, not_compared, spaces = pairs(mods)
    if len(spaces) > 1:
        notes.append(f"the targets are named in several namespaces ({', '.join(spaces)}): names of different "
                     "namespaces are not compared (a production jar without a refmap, or a jar not remapped to the "
                     "build's names)")
    fails = []
    if log is not None:
        lines = Path(log).read_text(encoding="utf-8", errors="replace").splitlines()
        fails = failures(lines, mods, conflicts, Path(log).name)
    mods_with = [m for m in mods if m.mixins]
    if not [m for m in others if m.mixins]:
        notes.append("no mod jar with Mixins found (classpath, Loom's remapped mods, run/mods, mods, --with): "
                     "conflicts with other mods are unknown; " + NEXT_JARS)
    counts = {"conflict": sum(r["severity"] == "conflict" for r in conflicts),
              "order_dependent": sum(r["severity"] == "order_dependent" for r in conflicts),
              "compatible": len(shared), "failures": len(fails),
              "unknown": sum(f["mod_status"] == "unknown" for f in fails)}
    return {"status": "found" if mods_with or fails else "no_mixins", "project_mixins": n_project,
            "mods": [m.record() for m in mods], "jars_read": len(jars), "other_mods": sum(1 for m in others if
                                                                                        m.mixins),
            "conflicts": conflicts, "shared": shared, "failures": fails, "not_compared": not_compared,
            "counts": counts, "notes": list(dict.fromkeys(notes)), "seconds": round(time.perf_counter() - t0, 3)}


def lookup(repo: Path, paths: list[str] | None = None, with_paths: list[str] | None = None,
           log: Path | None = None) -> dict:
    """``verinoda mixin-check --conflicts``."""
    from verinoda.paths import load_config

    try:
        config = load_config(repo)
    except Exception:  # noqa: BLE001 - no readable config: the build's own classpath is looked for
        config = None
    res = check(repo, paths, with_paths, log, config)
    if res["status"] == "no_mixins":
        res["note"] = "no @Mixin class in the project's sources or in any mod jar read" + (
            "; no Mixin failure in the log" if log is not None else "")
    return res


def exit_code(res: dict) -> int:
    """2: no Mixin read; 3: a clash or a failure in the log; 4: something unknown (no other mod's Mixins read,
    a failure whose mod is not found); 0 otherwise."""
    if res["status"] == "no_mixins":
        return 2
    c = res["counts"]
    if c["conflict"] or c["order_dependent"] or c["failures"]:
        return 3
    return 4 if c["unknown"] or not res["other_mods"] else 0


def _side_line(s: dict) -> str:
    at = f" {', '.join(s['at'])}" if s.get("at") else ""
    canc = " cancellable" if s.get("cancellable") else ""
    return (f"    {s['mod']}: {s['kind']} {s['mixin']}.{s['member']}{at}{canc} priority {s['priority']} "
            f"({s['priority_from']})  {s['evidence']}")


def render(res: dict) -> str:
    if res["status"] == "no_mixins":
        return res["note"]
    c = res["counts"]
    out = [f"{len(res['mods'])} mod(s) with Mixins or configs read ({res['other_mods']} besides the project's, "
           f"{res['jars_read']} jar(s) looked at): {c['conflict']} conflict(s), {c['order_dependent']} "
           f"order-dependent, {c['compatible']} shared target(s) with compatible kinds"
           + (f", {c['failures']} Mixin failure(s) in the log" if "failures" in c and res["failures"] else "")]
    for note in res["notes"]:
        out.append(f"  note: {note}")
    for r in res["conflicts"]:
        out.append(f"  {r['severity']} [{r['status']}] {r['target']}  (same method: {r['same_target']})")
        out.append(f"    {r['why']}")
        if r.get("plugin_note"):
            out.append(f"    note: {r['plugin_note']}")
        out += [_side_line(s) for s in r["mixins"]]
    for r in res["shared"]:
        out.append(f"  compatible [{r['status']}] {r['target']}  ({', '.join(r['mods'])})")
        out += [_side_line(s) for s in r["mixins"]]
    for f in res["failures"]:
        who = f"{f['mod']} [{f['mod_status']}]" if f.get("mod") else "mod unknown"
        out.append(f"  failed [{f['status']}] {f.get('mixin')}" + (f" {f['injector']}::{f['handler']}"
                                                                   if f.get("injector") else "")
                   + f" of {who}  ({f['what']})")
        out.append(f"    {f['evidence']}")
        if f.get("mod_evidence"):
            out.append(f"    mod from: {f['mod_evidence']}")
        if f.get("other"):
            o = f["other"]
            out.append(f"    with: {o.get('mixin')} of {o.get('mod') or 'mod unknown'}")
        for lc in f.get("likely_cause") or []:
            out.append(f"    likely cause [{lc['status']}]: {lc['severity']} on {lc['target']} "
                       f"({', '.join(lc['mods'])})")
        if f.get("next"):
            out.append(f"    next: {f['next']}")
    if res["not_compared"]:
        out.append(f"  {len(res['not_compared'])} selector(s) not compared (a regular expression, or not a constant "
                   "string)")
    return "\n".join(out)
