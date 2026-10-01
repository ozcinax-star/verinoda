"""Known crash patterns of a Minecraft / JVM log and the mods its stack frames point at, for ``verinoda trace-log``.

The rules are data (:data:`RULES`): an id, the line patterns that show it, what it means. A rule names the first log
line that matched it (the evidence) and what the pattern captured there; what it means is an interpretation, so
``strong_inference``. A suspect is the owner of stack frames outside the game, the loader and the JDK: a mod named by
a Mixin handler's name (``handler$zza000$examplemod$tick``) or by a frame's module (``TRANSFORMER/examplemod@1.0/``),
a mod jar a Forge frame names (``~[examplemod-1.0.jar%2390!/:?]``), else the class's package (its first three
parts). Its score is the sum of ``1 / (1 + position)`` over its frames, position 0 being the top of a trace or of a
``Caused by``: a heuristic, so ``strong_inference`` at most, shown with its inputs.
"""
from __future__ import annotations

import re

RULES: tuple[dict, ...] = (
    {"id": "out_of_memory",
     "patterns": (r"java\.lang\.OutOfMemoryError(?::\s*(?P<kind>[^\r\n]+))?",),
     "means": "the JVM ran out of memory of that kind (Java heap space: raise -Xmx or find what holds the objects)"},
    {"id": "watchdog",
     "patterns": (r"single server tick took (?P<seconds>[\d.]+) seconds",
                  r"Considering it to be crashed, server will forcibly shutdown",
                  r"^Description: Watching Server"),
     "means": "the server thread did not finish a tick in time and the watchdog stopped it: the server thread's top "
              "frames are where it was stuck"},
    {"id": "missing_dependency",
     # Fabric: "requires version 1.0 or later of", "requires any version of", "requires any version between 1.0
     # (inclusive) and 2.0 (exclusive) of"
     "patterns": (r"Mod '(?P<mod>[^']+)' \((?P<mod_id>[\w-]+)\) \S+ requires (?:[^'!]*? )?of (?:mod )?'?"
                  r"(?P<needs>[^',!]+?)'?(?: \((?P<needs_id>[\w-]+)\))?, (?:which is missing|but only)",
                  r"Mod ID: '(?P<needs_id>[\w-]+)', Requested by: '(?P<mod_id>[\w-]+)'",
                  r"Missing or unsupported mandatory dependencies"),
     "means": "a mod needs another mod (or a version of it) that is not installed"},
    {"id": "missing_class",
     "patterns": (r"java\.lang\.(?:NoClassDefFoundError|ClassNotFoundException):\s*(?P<cls>[\w/.$]+)",),
     # a loader's or Mixin's warning about an optional class (a compat target that is not installed) is not a crash
     "quiet": r"Error loading class:|/(?:WARN|INFO|DEBUG|TRACE)\]",
     "means": "a class the code needs is not on the classpath: a dependency missing, of another version, or a "
              "client-only class loaded on a server"},
    {"id": "mixin_apply",
     "patterns": (r"Mixin apply for mod (?P<mod_id>[\w-]+) failed (?P<mixin>\S+?)(?::)? from mod \S+ -> "
                  r"(?P<target>[\w.$]+)",
                  r"Mixin \[(?P<mixin>[^\]]+)\] from phase \[\w+\] in config \[(?P<config>[^\]]+)\] FAILED",
                  r"org\.spongepowered\.asm\.mixin\.(?:transformer\.throwables\.MixinTransformerError|injection\."
                  r"throwables\.(?:InvalidInjectionException|InjectionError))"),
     "means": "a Mixin could not be applied to its target class (the target changed, another mod's Mixin got there "
              "first, or a version mismatch): the mod that owns the Mixin is the one to update or report"},
    {"id": "wrong_java",
     "patterns": (r"UnsupportedClassVersionError: (?P<cls>\S+) has been compiled by a more recent version of the Java "
                  r"Runtime \(class file version (?P<needs_class_version>\d+)\.\d+\), this version of the Java Runtime "
                  r"only recognizes class file versions up to (?P<runs_class_version>\d+)\.\d+",
                  r"UnsupportedClassVersionError",
                  # ASM / Mixin / the loaders reading a class newer than they know; the launcher's own wording
                  r"Unsupported class file major version (?P<needs_class_version>\d+)",
                  r"LinkageError occurred while loading main class"),
     "means": "a class was compiled for a newer Java than the one running the game: start it with that Java"},
)
_COMPILED = [(r, [re.compile(p) for p in r["patterns"]], re.compile(r["quiet"]) if r.get("quiet") else None)
             for r in RULES]

# frames of the JDK, the game, the loaders and common libraries: never a suspect
_PLATFORM = ("java.", "javax.", "jdk.", "sun.", "com.sun.", "net.minecraft.", "com.mojang.", "net.fabricmc.",
             "net.minecraftforge.", "net.neoforged.", "cpw.mods.", "org.spongepowered.", "org.lwjgl.", "io.netty.",
             "com.google.", "org.apache.", "it.unimi.", "org.slf4j.", "org.objectweb.", "oshi.", "kotlin.",
             "scala.", "org.quiltmc.", "com.llamalad7.mixinextras.")
_PLATFORM_JAR = re.compile(r"^(?:forge|neoforge|fmlcore|fmlloader|javafmllanguage|lowcodelanguage|mclanguage|"
                           r"minecraft|client|server|mixin|modlauncher|securejarhandler|bootstraplauncher|eventbus|"
                           r"datafixerupper|brigadier|authlib|netty|guava|log4j|lwjgl|jopt|fabric-loader)\b", re.I)
_HANDLER = re.compile(r"^[a-zA-Z]+\$[0-9a-z]+\$(?P<mod>[a-z][a-z0-9_-]*)\$")
_PLATFORM_MODULE = re.compile(r"^(?:java\.|jdk\.|minecraft$|forge$|neoforge$|fml|cpw\.mods\.|net\.minecraftforge\.|"
                              r"net\.neoforged\.|org\.spongepowered\.|mixinextras|com\.mojang\.)")
# mod ids too common to tie a package to a mod by name
_GENERIC_IDS = frozenset({"api", "common", "core", "client", "server", "lib", "util", "utils", "mod", "mods", "main",
                          "mixin", "mixins", "impl", "internal", "shared", "base", "fabric", "forge", "neoforge"})
SCORE_BASIS = "sum of 1/(1+position) over the frames; position 0 is the top of a trace or of a Caused by"


def _found(m: re.Match) -> dict:
    found: dict = {k: v.strip()[:200] for k, v in m.groupdict().items() if v}
    for k in [k for k in found if k.endswith("_class_version")]:
        found[k.replace("_class_version", "_java")] = int(found[k]) - 44  # class file 61 is Java 17
    return found


def diagnose(lines: list[str]) -> list[dict]:
    """The rules a log's lines (as written, a logger's prefix included) show, in the order of their first matching
    line."""
    from verinoda.trace_log import _unprefixed

    hits: dict[str, dict] = {}
    for i, raw in enumerate(lines, 1):
        line = _unprefixed(raw)
        for rule, pats, quiet in _COMPILED:
            if quiet is not None and quiet.search(raw):
                continue
            for p in pats:
                m = p.search(line)
                if not m:
                    continue
                h = hits.get(rule["id"])
                if h is None:
                    h = hits[rule["id"]] = {"rule": rule["id"], "means": rule["means"], "log_line": i,
                                            "evidence": line.strip()[:200], "hits": 0, "status": "strong_inference"}
                found = _found(m)
                if found and "found" not in h:  # a later line may name what the first (a bare header) did not
                    h["found"] = found
                    if i != h["log_line"]:
                        h["found_at"] = i
                h["hits"] += 1
                break
    return sorted(hits.values(), key=lambda h: h["log_line"])


def owner(cls: str, meth: str, jar: str | None, module: str | None = None) -> tuple[str, str] | None:
    """``(suspect, how it was named)`` of a frame, None for a frame of the platform or of a class with no package
    (an obfuscated class of the game: ``dzv.a(SourceFile:123)``)."""
    m = _HANDLER.match(meth)
    if m:
        return f"mod {m.group('mod')}", "a Mixin handler of the mod"
    if cls.startswith(_PLATFORM) or "." not in cls:
        return None
    if module and not _PLATFORM_MODULE.match(module):
        return f"mod {module}", "the module the frame names"
    if jar and not _PLATFORM_JAR.match(jar):
        return jar, "the jar the frame names"
    return ".".join(cls.rsplit(".", 1)[0].split(".")[:3]), "the class's package"


def _mod_id(suspect: str) -> str | None:
    """The mod id a suspect names: ``mod X`` -> X, ``bigstorage-2.4.1.jar`` -> bigstorage; None for a package."""
    if suspect.startswith("mod "):
        return suspect[4:].lower()
    if suspect.endswith(".jar"):
        return re.sub(r"[-_+]v?\d.*$", "", suspect[:-4]).lower() or None
    return None


def _repeats(traces) -> set[int]:
    """Traces not to count again: one printed twice (the same header and frames), and a crash report section's
    ``Stacktrace:`` (``-- Head --``, ``-- Block entity being ticked --``, ...) whose frames are a run of an earlier
    trace's frames."""
    skip: set[int] = set()
    seen: set[tuple] = set()
    at: dict[tuple, list[tuple[int, int]]] = {}  # frame -> (trace, index) of its earlier occurrences
    keys = [[(f.cls, f.meth, f.line) for f in t.frames] for t in traces]
    for n, t in enumerate(traces):
        key = keys[n]
        whole = (t.header, tuple(key))
        if whole in seen:
            skip.add(n)
            continue
        if key and t.header.strip() == "Stacktrace:" and any(
                keys[m][i:i + len(key)] == key for m, i in at.get(key[0], ())):
            skip.add(n)
            continue
        seen.add(whole)
        for i, k in enumerate(key):
            at.setdefault(k, []).append((n, i))
    return skip


def suspects(traces, *, limit: int = 5) -> list[dict]:
    """Suspects of :func:`verinoda.trace_log.parse`'s traces, best first. A crash report section that repeats part
    of an earlier trace, or a trace printed twice, is not counted again (:func:`_repeats`)."""
    skip = _repeats(traces)
    rows: dict[str, dict] = {}
    for n, t in enumerate(traces):
        if n in skip:
            continue
        for f in t.frames:
            o = owner(f.cls, f.meth, getattr(f, "jar", None), getattr(f, "module", None))
            if o is None:
                continue
            r = rows.setdefault(o[0], {"suspect": o[0], "named_by": o[1], "score": 0.0, "frames": 0, "traces": set(),
                                       "top_frames": 0, "positions": [], "first": f"{f.cls.rsplit('.', 1)[-1]}."
                                                                                  f"{f.meth} (log line {f.log_line})"})
            pos = getattr(f, "pos", 0)
            r["score"] += 1 / (1 + pos)
            r["frames"] += 1
            r["traces"].add(n)
            r["top_frames"] += pos == 0
            r["positions"].append(pos)
            if getattr(f, "project", False):
                r["in_project"] = True
    # a Mixin handler or module of mod `lagfix`, its jar `lagfix-1.0.jar` and the frames of package
    # `dev.lagfix.chunk` are one suspect; a mod id too common to tell (core, api, ...) ties nothing
    groups: dict[str, list[str]] = {}
    for k in rows:
        mid = _mod_id(k)
        if mid and mid not in _GENERIC_IDS:
            groups.setdefault(mid, []).append(k)
    claimed: set[str] = set()
    for mid, named in groups.items():
        pkgs = [k for k in rows if _mod_id(k) is None and k not in claimed and mid in k.lower().split(".")]
        claimed.update(pkgs)
        members = sorted(named, key=lambda k: not k.endswith(".jar")) + pkgs
        if len(members) < 2:
            continue
        lead = next((k for k in members if k.endswith(".jar")), pkgs[0] if pkgs else members[0])
        others = [k for k in members if k != lead]
        r = rows[lead]
        r["suspect"] = f"{lead} ({', '.join(others[:3])}{', ...' if len(others) > 3 else ''})"
        for k in others:
            m = rows.pop(k)
            if m["named_by"] not in r["named_by"]:
                r["named_by"] += f" and {m['named_by']}"
            for f in ("score", "frames", "top_frames"):
                r[f] += m[f]
            r["traces"] |= m["traces"]
            r["positions"] += m["positions"]
            if m.get("in_project"):
                r["in_project"] = True
    out = []
    for r in sorted(rows.values(), key=lambda r: (-r["score"], r["suspect"]))[:limit]:
        r["score"] = round(r["score"], 2)
        r["traces"] = len(r["traces"])
        r["positions"] = sorted(r["positions"])[:6]
        r["status"] = "strong_inference"
        out.append(r)
    return out
