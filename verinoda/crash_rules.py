"""Known crash patterns of a Minecraft / JVM log and the mods its stack frames point at, for ``verinoda trace-log``.

The rules are data (:data:`RULES`): an id, the line patterns that show it, what it means. A rule names the first log
line that matched it (the evidence) and what the pattern captured there. A suspect is the owner of stack frames
outside the game, the loader and the JDK: a mod named by a Mixin handler's name (``handler$zza000$examplemod$tick``),
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
     "patterns": (r"Mod '(?P<mod>[^']+)' \((?P<mod_id>[\w-]+)\) \S+ requires (?:version \S+(?: or later)? of|any "
                  r"version of) (?:mod )?'?(?P<needs>[^',!]+?)'?(?: \((?P<needs_id>[\w-]+)\))?, (?:which is "
                  r"missing|but only)",
                  r"Mod ID: '(?P<needs_id>[\w-]+)', Requested by: '(?P<mod_id>[\w-]+)'",
                  r"Missing or unsupported mandatory dependencies"),
     "means": "a mod needs another mod (or a version of it) that is not installed"},
    {"id": "missing_class",
     "patterns": (r"java\.lang\.(?:NoClassDefFoundError|ClassNotFoundException):\s*(?P<cls>[\w/.$]+)",),
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
                  r"UnsupportedClassVersionError"),
     "means": "a class was compiled for a newer Java than the one running the game: start it with that Java"},
)
_COMPILED = [(r, [re.compile(p) for p in r["patterns"]]) for r in RULES]

# frames of the JDK, the game, the loaders and common libraries: never a suspect
_PLATFORM = ("java.", "javax.", "jdk.", "sun.", "com.sun.", "net.minecraft.", "com.mojang.", "net.fabricmc.",
             "net.minecraftforge.", "net.neoforged.", "cpw.mods.", "org.spongepowered.", "org.lwjgl.", "io.netty.",
             "com.google.", "org.apache.", "it.unimi.", "org.slf4j.", "org.objectweb.", "oshi.", "kotlin.",
             "scala.", "org.quiltmc.", "com.llamalad7.mixinextras.")
_PLATFORM_JAR = re.compile(r"^(?:forge|neoforge|fmlcore|fmlloader|javafmllanguage|lowcodelanguage|mclanguage|"
                           r"minecraft|client|server|mixin|modlauncher|securejarhandler|bootstraplauncher|eventbus|"
                           r"datafixerupper|brigadier|authlib|netty|guava|log4j|lwjgl|jopt|fabric-loader)\b", re.I)
_HANDLER = re.compile(r"^[a-zA-Z]+\$[0-9a-z]+\$(?P<mod>[a-z][a-z0-9_]*)\$")
SCORE_BASIS = "sum of 1/(1+position) over the frames; position 0 is the top of a trace or of a Caused by"


def _found(m: re.Match) -> dict:
    found: dict = {k: v.strip() for k, v in m.groupdict().items() if v}
    for k in [k for k in found if k.endswith("_class_version")]:
        found[k.replace("_class_version", "_java")] = int(found[k]) - 44  # class file 61 is Java 17
    return found


def diagnose(lines: list[str]) -> list[dict]:
    """The rules a log's lines show, in the order of their first matching line."""
    hits: dict[str, dict] = {}
    for i, line in enumerate(lines, 1):
        for rule, pats in _COMPILED:
            for p in pats:
                m = p.search(line)
                if not m:
                    continue
                h = hits.get(rule["id"])
                if h is None:
                    h = hits[rule["id"]] = {"rule": rule["id"], "means": rule["means"], "log_line": i,
                                            "evidence": line.strip()[:200], "hits": 0}
                found = _found(m)
                if found and "found" not in h:  # a later line may name what the first (a bare header) did not
                    h["found"] = found
                    if i != h["log_line"]:
                        h["found_at"] = i
                h["hits"] += 1
                break
    return sorted(hits.values(), key=lambda h: h["log_line"])


def owner(cls: str, meth: str, jar: str | None) -> tuple[str, str] | None:
    """``(suspect, how it was named)`` of a frame, None for a frame of the platform."""
    m = _HANDLER.match(meth)
    if m:
        return f"mod {m.group('mod')}", "a Mixin handler of the mod"
    if cls.startswith(_PLATFORM):
        return None
    if jar and not _PLATFORM_JAR.match(jar):
        return jar, "the jar the frame names"
    pkg = cls.rsplit(".", 1)[0] if "." in cls else cls
    return ".".join(pkg.split(".")[:3]), "the class's package"


def suspects(traces, *, limit: int = 5) -> list[dict]:
    """Suspects of :func:`verinoda.trace_log.parse`'s traces, best first. A trace whose frames repeat the start of
    an earlier one (a crash report's ``-- Head --`` section) is not counted twice."""
    seen: list[tuple] = []
    rows: dict[str, dict] = {}
    for n, t in enumerate(traces):
        key = tuple((f.cls, f.meth, f.line) for f in t.frames)
        if any(k[:len(key)] == key for k in seen):
            continue
        seen.append(key)
        for f in t.frames:
            o = owner(f.cls, f.meth, getattr(f, "jar", None))
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
    for name in [k for k in rows if k.startswith("mod ")]:
        # a Mixin handler of mod `lagfix` and the frames of package `dev.lagfix.chunk` are one suspect
        mod = name[4:]
        same = next((k for k in rows if not k.startswith("mod ") and mod in re.split(r"[.\-]", k)), None)
        if same is None:
            continue
        r, m = rows[same], rows.pop(name)
        r["suspect"] = f"{same} ({name})"
        r["named_by"] += f" and {m['named_by']}"
        for k in ("score", "frames", "top_frames"):
            r[k] += m[k]
        r["traces"] |= m["traces"]
        r["positions"] = sorted(r["positions"] + m["positions"])
    out = []
    for r in sorted(rows.values(), key=lambda r: (-r["score"], r["suspect"]))[:limit]:
        r["score"] = round(r["score"], 2)
        r["traces"] = len(r["traces"])
        r["positions"] = sorted(r["positions"])[:6]
        r["status"] = "strong_inference"
        out.append(r)
    return out
