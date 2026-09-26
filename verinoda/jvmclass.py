"""What the JVM libraries a Java project compiles against declare (docs/DESIGN.md D43): classes, their
super types, methods (arity, varargs, static, erased return type) and fields, read from class files.

Sources, all read locally and never run:

- the project's classpath: ``code_check.classpath`` in ``.verinoda/config.json`` (paths or globs), or
  the one a Fabric Loom build leaves (``build/loom-cache/argFiles/*``: every jar of the resolved run
  classpath, and the Minecraft jars ``.gradle/loom-cache/launch.cfg`` names);
- the JDK's API from its ``lib/ct.sym`` (class signatures per Java release), for the release the build
  targets (``options.release``, ``JavaLanguageVersion.of(N)``, ``sourceCompatibility``), found through
  ``JAVA_HOME``, ``java`` on PATH, Gradle's toolchains or the usual install folders.

A jar's classes are read once per jar version and kept under ``.verinoda/cache/jvm/``. Nested jars (a
Fabric API jar holds its modules under ``META-INF/jars/``) are read too.

The classpath is ``complete`` when it came from the configuration or from Loom's argument files: a class
missing from a package those jars hold is then not on the classpath at all. From anything less, a missing
library class is ``unknown``, never ``absent``.
"""
from __future__ import annotations

import glob
import hashlib
import io
import json
import os
import re
import shutil
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

INDEX_VERSION = 1
MAX_JARS = 400
MAX_NESTED_DEPTH = 2

ACC_STATIC, ACC_VARARGS, ACC_PRIVATE, ACC_SYNTHETIC, ACC_BRIDGE = 0x0008, 0x0080, 0x0002, 0x1000, 0x0040
ACC_INTERFACE, ACC_ENUM = 0x0200, 0x4000
# release number -> the letter ct.sym names it by (8, 9, then A = 10 ... Z = 35)
_RELEASE_CHAR = {**{r: str(r) for r in (8, 9)}, **{r: chr(ord("A") + r - 10) for r in range(10, 36)}}


# -- class files ------------------------------------------------------------------------------------

def _descriptor_types(desc: str) -> list[str]:
    """Field types of a descriptor list (``ILjava/lang/String;[J`` -> ``I``, ``java/lang/String``, ``[J``)."""
    out, i = [], 0
    while i < len(desc):
        j = i
        while desc[j] == "[":
            j += 1
        if desc[j] == "L":
            k = desc.index(";", j)
            out.append(desc[i:j] + desc[j + 1:k] if j > i else desc[j + 1:k])
            i = k + 1
        else:
            out.append(desc[i:j + 1])
            i = j + 1
    return out


def method_shape(desc: str) -> tuple[int, str]:
    """(parameter count, erased return type) of a method descriptor."""
    close = desc.index(")")
    ret = _descriptor_types(desc[close + 1:])
    return len(_descriptor_types(desc[1:close])), (ret[0] if ret else "V")


def _generic_return(sig: str | None) -> bool:
    """Does a method's generic signature return a type variable (``()TT;``)? Its erasure is then a
    bound, not what the caller gets back."""
    if not sig or ")" not in sig:
        return False
    return sig[sig.rindex(")") + 1:].lstrip("[").startswith("T")


def parse_class(b: bytes) -> dict | None:
    """``{"name", "super", "ifaces", "flags", "methods": {name: [[nparams, flags, ret], ...]},
    "fields": {name: [type, flags]}, "inner": {simple name: binary name}}`` of a class file, or None."""
    if b[:4] != b"\xca\xfe\xba\xbe":
        return None
    u2 = lambda o: (b[o] << 8) | b[o + 1]
    n = u2(8)
    cp: list = [None] * n
    i, k = 10, 1
    while k < n:
        tag = b[i]
        if tag == 1:
            ln = u2(i + 1)
            cp[k] = b[i + 3:i + 3 + ln].decode("utf-8", "replace")
            i += 3 + ln
        elif tag == 7:
            cp[k] = ("class", u2(i + 1))
            i += 3
        elif tag in (8, 16, 19, 20):
            i += 3
        elif tag in (3, 4, 9, 10, 11, 12, 17, 18):
            i += 5
        elif tag in (5, 6):
            i += 9
            k += 1
        elif tag == 15:
            i += 4
        else:
            return None
        k += 1

    def cname(idx: int) -> str | None:
        ref = cp[idx] if idx else None
        return cp[ref[1]] if isinstance(ref, tuple) else None

    flags, this, sup = u2(i), u2(i + 2), u2(i + 4)
    i += 6
    ni = u2(i)
    ifaces = [cname(u2(i + 2 + 2 * j)) for j in range(ni)]
    i += 2 + 2 * ni

    def u4(o: int) -> int:
        return struct.unpack(">I", b[o:o + 4])[0]

    def members(i: int):
        out = []
        cnt = u2(i)
        i += 2
        for _ in range(cnt):
            acc, nm, desc = u2(i), u2(i + 2), u2(i + 4)
            n_attr = u2(i + 6)
            i += 8
            sig = None
            for _ in range(n_attr):
                if cp[u2(i)] == "Signature":
                    sig = cp[u2(i + 6)]
                i += 6 + u4(i + 2)
            out.append((cp[nm], cp[desc], acc, sig))
        return out, i

    fields_raw, i = members(i)
    methods_raw, i = members(i)
    inner: dict[str, str] = {}
    name = cname(this)
    n_attr = u2(i)
    i += 2
    for _ in range(n_attr):
        ln = u4(i + 2)
        if cp[u2(i)] == "InnerClasses":
            o = i + 6
            for j in range(u2(o)):
                e = o + 2 + 8 * j
                inner_c, outer_c, simple = cname(u2(e)), cname(u2(e + 2)), u2(e + 4)
                if outer_c == name and simple and inner_c:
                    inner[cp[simple]] = inner_c
        i += 6 + ln
    methods: dict[str, list] = {}
    for mname, desc, acc, sig in methods_raw:
        if acc & (ACC_SYNTHETIC | ACC_BRIDGE) or mname == "<clinit>":
            continue
        npar, ret = method_shape(desc)
        methods.setdefault(mname, []).append([npar, acc & (ACC_STATIC | ACC_VARARGS | ACC_PRIVATE),
                                              None if _generic_return(sig) else ret])
    fields = {fname: [_descriptor_types(desc)[0] if desc else "?", acc & (ACC_STATIC | ACC_PRIVATE)]
              for fname, desc, acc, _sig in fields_raw if not acc & ACC_SYNTHETIC}
    return {"name": name, "super": cname(sup), "ifaces": [x for x in ifaces if x], "flags": flags,
            "methods": methods, "fields": fields, "inner": inner}


# -- jars and their cache ---------------------------------------------------------------------------

def _jar_classes(data: bytes, depth: int = 0) -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return out
    with z:
        for info in z.infolist():
            n = info.filename
            if n.endswith(".class") and not n.startswith("META-INF/versions/") and n != "module-info.class" \
                    and not n.endswith("package-info.class"):
                c = parse_class(z.read(info))
                if c and c["name"]:
                    out[c["name"]] = c
            elif n.endswith(".jar") and n.startswith("META-INF/jars/") and depth < MAX_NESTED_DEPTH:
                out.update(_jar_classes(z.read(info), depth + 1))
    return out


def _ctsym_classes(ctsym: Path, release: int) -> dict[str, dict]:
    ch = _RELEASE_CHAR.get(release)
    out: dict[str, dict] = {}
    if ch is None:
        return out
    with zipfile.ZipFile(ctsym) as z:
        for info in z.infolist():
            top = info.filename.partition("/")[0]
            if ch not in top or not info.filename.endswith(".sig"):
                continue
            c = parse_class(z.read(info))
            if c and c["name"] and (c["flags"] & 0x0001):  # the public API
                out[c["name"]] = c
    return out


def _cache_key(p: Path, extra: str = "") -> str:
    st = p.stat()
    return hashlib.sha256(f"{p.resolve()}|{st.st_size}|{st.st_mtime_ns}|{extra}|{INDEX_VERSION}".encode()).hexdigest()[:32]


def load_jar(p: Path, cache_dir: Path | None, *, release: int | None = None) -> dict[str, dict]:
    """Classes of a jar (or of ct.sym for ``release``), cached per file version."""
    key = _cache_key(p, str(release or ""))
    f = cache_dir / f"{key}.json" if cache_dir else None
    if f is not None:
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    classes = _ctsym_classes(p, release) if release else _jar_classes(p.read_bytes())
    if f is not None:
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(classes, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, f)
        except OSError:
            pass
    return classes


# -- the classpath ----------------------------------------------------------------------------------

@dataclass
class Classpath:
    jars: list[Path] = field(default_factory=list)
    source: str = "none"          # config | loom | none
    complete: bool = False
    jdk: Path | None = None       # ct.sym
    release: int | None = None
    notes: list[str] = field(default_factory=list)


def _loom_jars(root: Path) -> list[Path]:
    jars: list[Path] = []
    for arg in sorted((root / "build" / "loom-cache" / "argFiles").glob("*")):
        try:
            text = arg.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r"[^\s\"';]+?\.jar", text):
            jars.append(Path(m.group(0)))
    cfg = root / ".gradle" / "loom-cache" / "launch.cfg"
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for m in re.finditer(r"fabric\.gameJarPath(?:\.client)?=(.+)", text):
        p = Path(m.group(1).strip())
        jars += sorted(p.rglob("*.jar")) if p.is_dir() else [p]
    return jars


def _loom_minecraft_libraries(root: Path) -> list[Path]:
    """Minecraft's own libraries (LWJGL, Guava, Brigadier...) for the project's ``minecraft_version``, as
    Loom's ``mojang_minecraft_info.json`` lists them, from Gradle's module cache (a run configuration's
    argument file may lack the client's libraries)."""
    try:
        props = (root / "gradle.properties").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    m = re.search(r"^\s*minecraft_version\s*=\s*(\S+)", props, re.MULTILINE)
    if not m:
        return []
    gradle = Path(os.environ.get("GRADLE_USER_HOME") or Path.home() / ".gradle")
    try:
        info = json.loads((gradle / "caches" / "fabric-loom" / m.group(1) / "mojang_minecraft_info.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for lib in info.get("libraries") or ():
        parts = str(lib.get("name") or "").split(":")
        if len(parts) != 3:  # natives-* classifiers hold no classes to compile against
            continue
        group, art, ver = parts
        out += sorted((gradle / "caches" / "modules-2" / "files-2.1" / group / art / ver).glob(f"*/{art}-{ver}.jar"))
    return out


BUILD_FILES = ("settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts", "pom.xml")


def build_root(repo: Path, file: Path) -> Path:
    """The build a Java file belongs to: the outermost folder above it (up to the repository) holding a
    Gradle settings file, else the nearest holding a build file, else the repository."""
    repo = repo.resolve()
    cur = file.resolve().parent
    nearest = settings = None
    while True:
        if nearest is None and any((cur / n).is_file() for n in BUILD_FILES[2:]):
            nearest = cur
        if any((cur / n).is_file() for n in BUILD_FILES[:2]):
            settings = cur
        if cur == repo or cur.parent == cur:
            break
        cur = cur.parent
    return settings or nearest or repo


def _release(root: Path) -> int | None:
    for name in ("build.gradle", "build.gradle.kts", "pom.xml"):
        try:
            text = (root / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pat in (r"options\.release\s*[=(]\s*(\d+)", r"JavaLanguageVersion\.of\(\s*(\d+)\s*\)",
                    r"JavaVersion\.VERSION_(\d+)", r"<maven\.compiler\.release>(\d+)<",
                    r"<release>(\d+)</release>", r"sourceCompatibility\s*=\s*['\"]?(\d+)"):
            m = re.search(pat, text)
            if m:
                return int(m.group(1))
    return None


def _jdk_homes() -> list[Path]:
    homes: list[Path] = []
    if os.environ.get("JAVA_HOME"):
        homes.append(Path(os.environ["JAVA_HOME"]))
    java = shutil.which("java")
    if java:
        homes.append(Path(java).resolve().parent.parent)
    for pattern in ("~/.gradle/jdks/*", "C:/Program Files/Eclipse Adoptium/*", "C:/Program Files/Java/*",
                    "C:/Program Files/Microsoft/jdk-*", "C:/Program Files/Zulu/*", "/usr/lib/jvm/*",
                    "/Library/Java/JavaVirtualMachines/*/Contents/Home"):
        homes += [Path(p) for p in sorted(glob.glob(os.path.expanduser(pattern)), reverse=True)]
    return [h for h in dict.fromkeys(homes) if (h / "lib" / "ct.sym").is_file()]


def discover(repo: Path, config: dict | None = None) -> Classpath:
    """The classpath and JDK a Java check reads for ``repo`` (see the module docstring)."""
    cp = Classpath()
    conf = ((config or {}).get("code_check") or {}) if isinstance(config, dict) else {}
    listed = conf.get("classpath") if isinstance(conf, dict) else None
    if isinstance(listed, list) and listed:
        for spec in listed:
            pat = str(spec) if os.path.isabs(str(spec)) else str(repo / str(spec))
            cp.jars += [Path(p) for p in sorted(glob.glob(os.path.expanduser(pat), recursive=True))]
        cp.source, cp.complete = "config", True
    else:
        got = _loom_jars(repo)
        if got:
            cp.jars += got + _loom_minecraft_libraries(repo)
            cp.source, cp.complete = "loom", True
        if cp.source == "none":
            cp.notes.append("no classpath: set code_check.classpath in .verinoda/config.json, or run the Gradle "
                            "(Loom) build once; library classes are unknown until then")
    cp.jars = [p for p in dict.fromkeys(cp.jars) if p.suffix == ".jar" and p.is_file()]
    if len(cp.jars) > MAX_JARS:
        cp.notes.append(f"{len(cp.jars)} jars on the classpath: the first {MAX_JARS} are read")
        cp.jars, cp.complete = cp.jars[:MAX_JARS], False
    rel = _release(repo)
    homes = _jdk_homes()
    if homes:
        cp.jdk = homes[0] / "lib" / "ct.sym"
        cp.release = rel or _home_release(homes[0])
    else:
        cp.notes.append("no JDK with lib/ct.sym found (JAVA_HOME, java on PATH): JDK classes are unknown")
    return cp


def _home_release(home: Path) -> int | None:
    try:
        text = (home / "release").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r'JAVA_VERSION="(\d+)', text)
    return int(m.group(1)) if m else None
