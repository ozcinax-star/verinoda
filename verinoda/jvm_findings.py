"""JVM checkers' findings read as evidence (``verinoda import-findings``): Error Prone and NullAway diagnostics as
javac prints them, and ``jdeps -jdkinternals`` output.

What is read (nothing is run: the files are logs and reports the build already wrote):

``javac``   an Error Prone or NullAway diagnostic as javac prints it, alone or inside a Gradle, Maven or Ant log
            (a CI timestamp, ANSI colours, Maven's ``[WARNING]`` / ``[ERROR]`` and Ant's ``[javac]`` are
            stripped)::

                /home/runner/work/app/src/main/java/com/ex/Foo.java:12: warning: [NullAway] dereferenced ...
                    return name.length();
                               ^
                    (see http://t.uber.com/nullaway )
                  Did you mean 'return name == null ? 0 : name.length();'?

            and Maven's ``/C:/app/src/main/java/com/ex/Foo.java:[12,16] [NullAway] dereferenced ...``. The check
            in brackets names the tool: ``[NullAway]`` is NullAway, any other check written in CamelCase is an
            Error Prone bug pattern; javac's own ``-Xlint`` categories (lower case: ``[unchecked]``,
            ``[deprecation]``) and messages without a check are counted, not listed.
``jdeps``   ``jdeps -jdkinternals`` / ``--jdk-internals`` (JDK 9 and later: ``com.ex.Foo -> sun.misc.Unsafe
            JDK internal API (jdk.unsupported)``; JDK 8: the class on one line and ``-> sun.misc.Unsafe JDK internal
            API (rt.jar)`` under it) with its "Suggested Replacement" table. A dependence that is not on a JDK
            internal API (plain ``jdeps -verbose`` output) is counted, not listed.
``SARIF``   a file that is JSON is read as SARIF 2.1 by :mod:`verinoda.sarif` (a project that already writes Error
            Prone's findings as SARIF); its tools are named by the file.

A diagnostic's file is tied to the repository by path: an absolute path under the repository, a relative path from
the repository root, else the longest path suffix the two share (a CI runner's checkout, a sub-project's relative
path). A jdeps finding names a class: it is tied to the source file of its top-level class by its package path
(``com.ex.Foo$1`` -> ``.../com/ex/Foo.java``, ``com.ex.UtilKt`` -> ``.../com/ex/Util.kt``) when that file declares
the class's package, and its line is the first code line (outside comments) of that file naming the internal API
by its qualified name or an import of its package; its simple name alone is a guess (``weak_inference``), and is
not used when the file imports another class of that name. A class with no source file in the repository (a
dependency's jar) is counted and named, not listed.

What a log says is the named tool's statement about the tree it compiled, not checked here: ``strong_inference``
while the log is newer than the file, ``weak_inference`` when the file changed after it (its lines may have moved)
or when it was tied to the repository only by a path suffix. javac quotes the source line it reports on: when the
current file still has that text at that line, the finding is ``strong_inference`` even if the file changed or was
tied by suffix (the code the tool saw is still there); when the text moved, the finding is put at the one line that
now has it (``weak_inference``); when it is gone, the finding stays at its line, ``weak_inference``. Nothing is
written.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from verinoda import coverage_import as ci
from verinoda import sarif

TOOLS = ("auto", "errorprone", "nullaway", "jdeps")
TOOL_NAMES = {"errorprone": "Error Prone", "nullaway": "NullAway", "jdeps": "jdeps", "javac": "javac"}
MAX_LOG_BYTES = 200_000_000
MAX_FOLLOW = 12          # lines after a diagnostic read for its quoted source, caret and hints
MAX_MESSAGE = 300
LEVEL_ORDER = {"error": 0, "warning": 1, "note": 2}
DERIVED_BY = "verinoda.jvm_findings (a build log read: the tool's statement, not checked here)"
LIMITS = [
    "a finding is the named tool's statement on the tree it compiled; Verinoda does not check it",
    "a file changed after the log was written is matched by the line javac quoted when the log has it, else by "
    "line number, and may have moved (weak_inference)",
    "a path from another machine (a CI runner's) is tied to the repository by its longest existing suffix, a guess "
    "(weak_inference) unless the quoted source line is still at that line",
    "a jdeps finding names a class: its file is found by the package path of its top-level class, which a Java "
    "file need not follow for a non-public class, and its line is the first code line naming the internal API; "
    "a line naming only its simple name is a guess (weak_inference), and a use through reflection with a "
    "computed name has no line",
    "javac's own -Xlint warnings and compile errors are counted, not listed; Error Prone's and NullAway's versions "
    "are known only when the log names their artifacts",
    "the claims are printed, not stored in the project's claim store",
]

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# a CI timestamp (GitHub Actions, GitLab) or Jenkins Timestamper's bracketed one, and the one space after it: the
# rest of the line keeps its indentation (the caret's column depends on it)
_STAMP = re.compile(r"^(?:\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?"
                    r"|\[\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?\])[ \t]?")
_TAG = re.compile(r"^\[(?P<tag>INFO|WARNING|WARN|ERROR|javac)\][ \t]?", re.I)
# javac's severity word in any locale: English, German, French, Japanese, Chinese (simplified)
_SEV_WORDS = {"error": "error", "warning": "warning", "note": "note",
              "fehler": "error", "warnung": "warning", "hinweis": "note",
              "erreur": "error", "avertissement": "warning", "remarque": "note",
              "\u30a8\u30e9\u30fc": "error", "\u8b66\u544a": "warning", "\u6ce8\u610f": "note",
              "\u9519\u8bef": "error", "\u6ce8": "note"}
_SEV = r"(?:(?P<sev>[^\s:\uff1a\[\]]+)[:\uff1a]\s*)"
# javac: PATH.java:LINE: [severity:] [Check] message
_JAVAC = re.compile(r"^(?P<path>.+?\.java):(?P<line>\d+):\s*" + _SEV + r"?(?:\[(?P<check>[^\]\s]+)\]\s*)?"
                    r"(?P<msg>.*)$")
# Maven's compiler plugin: PATH.java:[LINE,COL] [severity:] [Check] message
_MAVEN = re.compile(r"^(?P<path>.+?\.java):\[(?P<line>\d+)(?:,(?P<col>\d+))?\]\s*" + _SEV +
                    r"?(?:\[(?P<check>[^\]\s]+)\]\s*)?(?P<msg>.*)$")
# a line that looks like a diagnostic but did not parse (a broken line number, a cut path)
_ALMOST = re.compile(r"\.java:\S*\s*[^\s:\uff1a\[\]]+[:\uff1a]\s*\[|\.java:\[[^\]]*\]\s*\[[A-Z]")
_CHECK_EP = re.compile(r"^[A-Z][A-Za-z0-9_]*$")
_CARET = re.compile(r"^\s*\^\s*$")
_SEE = re.compile(r"^\s*\(see\s+(?P<url>\S+)\s*\)\s*$")
_HINT = re.compile(r"^\s*(?P<hint>Did you mean\b.*)$")
_COUNT = re.compile(r"^\s*\d+\s+(?:errors?|warnings?)\s*$")
_GRADLE = re.compile(r"^(?:> Task |> Configure |FAILURE:|BUILD (?:SUCCESSFUL|FAILED)|\* What went wrong)")
_VERSIONS = {"Error Prone": re.compile(r"error_prone_core[-:/](\d+(?:\.\d+)+(?:-[\w.]+)?)"),
             "NullAway": re.compile(r"nullaway[-:/](\d+(?:\.\d+)+(?:-[\w.]+)?)", re.I)}
# jdeps
_NAME = r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*"
_DEP_ONE = re.compile(r"^\s+(?P<cls>" + _NAME + r")(?:\s+\((?P<arch>[^)]*)\))?\s+->\s+(?P<target>" + _NAME +
                      r")\s+(?P<kind>\S.*?)\s*$")
_DEP_CLASS = re.compile(r"^\s+(?P<cls>" + _NAME + r")\s+\((?P<arch>[^)]*)\)\s*$")
_DEP_TARGET = re.compile(r"^\s+->\s+(?P<target>" + _NAME + r")\s+(?P<kind>\S.*?)\s*$")
_DEP_ARCHIVE = re.compile(r"^(?P<arch>\S.*?)\s+->\s+(?P<to>\S.*?)\s*$")
_REPL_HEAD = re.compile(r"^\s*JDK Internal API\s+Suggested Replacement\s*$")
_REPL_ROW = re.compile(r"^\s*(?P<api>" + _NAME + r")\s{2,}(?P<repl>\S.*?)\s*$")
_INTERNAL = re.compile(r"JDK internal API(?:\s*\((?P<where>[^)]*)\))?", re.I)
# path parts that mark a third-party or generated file: a suffix match through them is not tried
_FOREIGN = {"site-packages", "node_modules", ".gradle", ".m2", "vendor", "third_party"}


def _clip(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= MAX_MESSAGE else text[:MAX_MESSAGE - 3] + "..."


def _decode(data: bytes) -> str:
    """A log's text: UTF-16 when it starts with that byte-order mark (PowerShell 5.1's ``>`` writes it), else
    UTF-8 with a BOM dropped, else this machine's ANSI code page or cp1252 (javac on a Windows console writes
    that) when the whole file decodes in it; else UTF-8 with the bytes that do not decode replaced."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    import locale

    for enc in dict.fromkeys([locale.getpreferredencoding(False), "cp1252"]):
        if enc.lower().replace("-", "").replace("_", "") in ("utf8", "utf8sig"):
            continue
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8-sig", "replace")


def clean(line: str) -> tuple[str | None, str]:
    """``(Maven/Ant tag, the line without a CI timestamp, colours and the tag)``."""
    s = _ANSI.sub("", line.rstrip("\r\n"))
    s = _STAMP.sub("", s, count=1)
    tag = None
    m = _TAG.match(s.lstrip())
    if m:   # Ant indents its [javac] tag
        tag = m.group("tag").upper()
        s = s.lstrip()[m.end():]
    return tag, s


# -- javac output -------------------------------------------------------------------------------------------

def _header(s: str) -> re.Match | None:
    s2 = s.strip()
    return _MAVEN.match(s2) or _JAVAC.match(s2)


def _ends_block(t: str) -> bool:
    """A line that ends the lines following a diagnostic: the next one, a count, a Gradle marker."""
    return bool(_header(t) or _COUNT.match(t) or _GRADLE.match(t.strip()) or _ALMOST.search(t))


def _level(word: str | None, tag: str | None) -> str:
    if word:   # a severity word javac printed in a language not listed is still a diagnostic's
        return _SEV_WORDS.get(word, _SEV_WORDS.get(word.lower(), "warning"))
    return {"ERROR": "error", "WARNING": "warning", "WARN": "warning", "INFO": "note"}.get(tag or "", "warning")


def parse_javac(lines: list[str], cleaned: list[tuple[str | None, str]] | None = None) -> tuple[list[dict], dict]:
    """``(diagnostics, counts)`` of javac output: each diagnostic with its path, line, column, level, check,
    message, the source line javac quoted, its "see" link, its "Did you mean" hint and its log line (1-based).
    ``cleaned`` is ``[clean(x) for x in lines]`` when the caller has it already."""
    if cleaned is None:
        cleaned = [clean(x) for x in lines]
    out: list[dict] = []
    counts = {"javac_other": 0, "unreadable": 0}
    i = 0
    while i < len(cleaned):
        tag, s = cleaned[i]
        m = _header(s)
        if m is None:
            if _ALMOST.search(s):
                counts["unreadable"] += 1
            i += 1
            continue
        check = m.group("check")
        d = {"path": m.group("path"), "line": int(m.group("line")), "column": None,
             "level": _level(m.group("sev"), tag),
             "check": check, "message": (m.group("msg") or "").strip(), "quoted": None, "see": None, "hint": None,
             "log_line": i + 1}
        if m.groupdict().get("col"):
            d["column"] = int(m.group("col"))
        # the lines after it, up to the next diagnostic: message lines, the quoted source and its caret, hints. A
        # blank line ends them, unless the caret is still to come (Error Prone's long messages have blank lines)
        follow: list[str] = []
        j = i + 1
        last = min(len(cleaned), i + MAX_FOLLOW + 1)
        while j < last:
            t = cleaned[j][1]
            if _ends_block(t):
                break
            if not t.strip():
                if any(_CARET.match(x) for x in follow):
                    break
                ahead = next((k for k in range(j + 1, last) if _ends_block(cleaned[k][1])
                              or _CARET.match(cleaned[k][1])), None)
                if ahead is None or not _CARET.match(cleaned[ahead][1]):
                    break
                j += 1
                continue
            follow.append(t)
            j += 1
        caret = next((k for k, t in enumerate(follow) if _CARET.match(t)), None)
        rest = follow
        if caret is not None and caret >= 1:
            d["quoted"] = follow[caret - 1]
            if d["column"] is None:
                d["column"] = follow[caret].index("^") + 1
            extra = [t.strip() for t in follow[:caret - 1] if not _SEE.match(t) and not _HINT.match(t)]
            if extra:
                d["message"] = " ".join([d["message"], *extra]).strip()
            rest = follow[caret + 1:]
        for k, t in enumerate(rest):
            sm = _SEE.match(t)
            if sm and not d["see"]:
                d["see"] = sm.group("url")
                continue
            hm = _HINT.match(t)
            if hm and not d["hint"]:
                hint = [hm.group("hint").strip()]
                for t2 in rest[k + 1:k + 4]:   # a hint quoting several lines of code ends with "?"
                    if hint[-1].endswith("?") or _SEE.match(t2):
                        break
                    hint.append(t2.strip())
                d["hint"] = _clip(" ".join(hint))
        i = j
        if not check or not _CHECK_EP.match(check):
            counts["javac_other"] += 1   # javac's own: an -Xlint category, a compile error
            continue
        d["tool"] = "NullAway" if check == "NullAway" else "Error Prone"
        out.append(d)
    return out, counts


# -- jdeps output -------------------------------------------------------------------------------------------

def parse_jdeps(lines: list[str], cleaned: list[tuple[str | None, str]] | None = None
                ) -> tuple[list[dict], dict[str, str], dict]:
    """``(internal API uses, suggested replacements, counts)`` of jdeps output; each use with its class, the
    internal API, where jdeps placed it (a module, ``JDK removed internal API``, ``rt.jar``), the archive the class
    was read from and its log line. A dependence on no internal API is counted only under an archive header (an
    indented ``a -> b c`` line elsewhere in a build log, a quoted lambda, is not jdeps')."""
    if cleaned is None:
        cleaned = [clean(x) for x in lines]
    uses: list[dict] = []
    repl: dict[str, str] = {}
    counts = {"not_internal": 0}
    archive = None
    cls_now: tuple[str, str | None] | None = None
    in_table = False
    for n, (_tag, s) in enumerate(cleaned, 1):
        if _REPL_HEAD.match(s):
            in_table = True
            continue
        if in_table:
            if not s.strip():
                in_table = False
                continue
            if set(s.strip()) <= {"-", " "}:
                continue
            rm = _REPL_ROW.match(s)
            if rm:
                repl[rm.group("api")] = rm.group("repl")
            continue
        if not s.strip():
            cls_now = None
            continue
        m = _DEP_ONE.match(s)
        if m and not s.lstrip().startswith("->"):
            cls_now = None
            arch = m.group("arch") or archive
            _record(uses, counts, m.group("cls"), m.group("target"), m.group("kind"), arch, n)
            continue
        m = _DEP_CLASS.match(s)
        if m:
            cls_now = (m.group("cls"), m.group("arch"))
            continue
        m = _DEP_TARGET.match(s)
        if m and cls_now:
            _record(uses, counts, cls_now[0], m.group("target"), m.group("kind"), cls_now[1] or archive, n)
            continue
        m = _DEP_ARCHIVE.match(s)
        if m and not s.startswith((" ", "\t")):
            archive = m.group("arch")
            cls_now = None
    return uses, repl, counts


def _record(uses: list[dict], counts: dict, cls: str, target: str, kind: str, arch: str | None, n: int) -> None:
    im = _INTERNAL.search(kind)
    if not im:
        if arch is not None:
            counts["not_internal"] += 1
        return
    where = (im.group("where") or "").strip()
    uses.append({"class": cls, "target": target, "where": where, "archive": arch, "log_line": n,
                 "removed": "removed" in where.lower()})


def replacement_of(target: str, repl: dict[str, str]) -> str | None:
    """The suggested replacement jdeps lists for ``target``: its own row, else its top-level class's, else the
    longest package row above it."""
    top = target.split("$", 1)[0]
    for key in (target, top):
        if key in repl:
            return repl[key]
    parts = top.split(".")
    for i in range(len(parts) - 1, 0, -1):
        if ".".join(parts[:i]) in repl:
            return repl[".".join(parts[:i])]
    return None


# -- the repository -----------------------------------------------------------------------------------------

def _remote(path: str) -> bool:
    """A UNC path (``\\\\host\\share``) or a Windows device path (``\\\\?\\``, ``\\\\.\\``), in either slash."""
    return path.strip().replace("\\", "/").startswith("//")


def _package(lines: list[str] | None) -> str | None:
    """The package a Java or Kotlin file declares (``""`` for none), ``None`` when it cannot be read."""
    if lines is None:
        return None
    for code, _bare in _code_lines(lines[:400]):
        m = re.match(r"^\s*package\s+([\w.]+)\s*;?\s*$", code)
        if m:
            return m.group(1)
        if re.match(r"^\s*(?:import|(?:public\s+|abstract\s+|final\s+)*(?:class|interface|enum|record|object|fun)\b)",
                    code):
            break
    return ""


class _Repo:
    """Files of the repository, listed once and only when a suffix or a class has to be looked up."""

    def __init__(self, root: Path):
        self.root = root
        self._by_name: dict[str, list[str]] | None = None
        self.places: dict[str, tuple[str | None, str]] = {}
        self.classes: dict[str, tuple[str | None, str]] = {}
        self.texts: dict[str, list[str] | None] = {}
        self.facts: dict[str, dict | None] = {}

    def by_name(self) -> dict[str, list[str]]:
        if self._by_name is None:
            from verinoda import snapshot

            self._by_name = {}
            for rel in snapshot.listed_files(self.root):
                self._by_name.setdefault(rel.rsplit("/", 1)[-1], []).append(rel)
        return self._by_name

    def place(self, path: str) -> tuple[str | None, str]:
        """``(repository-relative path, how)`` of a path a log printed: ``exact``, ``suffix``; ``(None,
        "elsewhere" | "ambiguous")`` when it names no single repository file."""
        if path not in self.places:
            self.places[path] = self._place(path)
        return self.places[path]

    def _place(self, path: str) -> tuple[str | None, str]:
        p = path.strip().replace("\\", "/")
        if p.lower().startswith("file:"):
            p = re.sub(r"^file:/*", "/", p, flags=re.I)
        if re.match(r"^/[A-Za-z]:/", p):   # Maven on Windows: /C:/...
            p = p[1:]
        given_absolute = bool(re.match(r"^[A-Za-z]:/|^/", p))
        key = ci._key(self.root, p)
        parts = [x for x in key.split("/") if x not in ("", ".")]
        if ".." in parts:
            return None, "elsewhere"
        absolute = bool(re.match(r"^[A-Za-z]:/|^/", key))
        if not absolute:
            if (self.root / key).is_file():
                return str(PurePosixPath(key)), "exact"
            if given_absolute:   # a path inside the repository that is not a file now: not another file's
                return None, "elsewhere"
            # a path relative to a sub-project or to the folder the build ran in: the files ending with it
            hits = [r for r in self.by_name().get(parts[-1] if parts else "", [])
                    if r == "/".join(parts) or r.endswith("/" + "/".join(parts))]
            if len(hits) == 1:
                return hits[0], "suffix"
            return None, "ambiguous" if hits else "elsewhere"
        here = False
        if not _remote(p):   # a UNC or device path from a log is never opened: on Windows that contacts its host
            try:
                here = Path(p).exists()
            except (OSError, ValueError):
                here = False
        if here or _FOREIGN.intersection(parts):
            return None, "elsewhere"   # a file on this machine outside the repository, or a dependency's
        for i in range(1, len(parts)):   # a CI runner's checkout: the longest suffix that is a repository file
            cand = "/".join(parts[i:])
            if (self.root / cand).is_file():
                return cand, "suffix"
        return None, "elsewhere"

    def class_file(self, fqn: str) -> tuple[str | None, str]:
        """``(file, how)`` of a class by the package path of its top-level class: ``class`` or ``(None,
        "unknown" | "ambiguous")``."""
        if fqn not in self.classes:
            top = fqn.split("$", 1)[0]
            tail = top.replace(".", "/")
            names = [tail + ".java", tail + ".kt"]
            if tail.endswith("Kt") and len(tail.rsplit("/", 1)[-1]) > 2:
                names.append(tail[:-2] + ".kt")   # Kotlin's file facade: UtilKt is Util.kt
            hits = sorted({r for nm in names for r in self.by_name().get(nm.rsplit("/", 1)[-1], [])
                           if r == nm or r.endswith("/" + nm)})
            # the file must declare the class's package: a default-package class (a dependency's ``Foo``) is not
            # the repository's com/ex/Foo.java
            pkg = top.rpartition(".")[0]
            hits = [r for r in hits if _package(self.lines(r)) == pkg]
            if len(hits) == 1:
                self.classes[fqn] = (hits[0], "class")
            else:
                self.classes[fqn] = (None, "ambiguous" if hits else "unknown")
        return self.classes[fqn]

    def lines(self, rel: str) -> list[str] | None:
        if rel not in self.texts:
            try:
                self.texts[rel] = (self.root / rel).read_bytes().decode("utf-8", "replace").splitlines()
            except OSError:
                self.texts[rel] = None
        return self.texts[rel]

    def symbol(self, rel: str, line: int) -> str | None:
        if rel not in self.facts:
            self.facts[rel] = ci.facts_of(self.root, rel)
        return ci.symbol_of(self.facts[rel], line)

    def newer_than(self, rel: str, mtime: float) -> bool:
        try:
            return (self.root / rel).stat().st_mtime > mtime
        except OSError:
            return True


def _norm(s: str | None) -> str:
    return " ".join((s or "").split())


def _where_quoted(lines: list[str] | None, line: int, quoted: str | None) -> tuple[int, str]:
    """``(line, how)``: ``here`` when the file has the quoted text at ``line``, ``moved`` when one other line has
    it (that line), ``gone`` when none or several do, ``unquoted`` without a quote or a file."""
    q = _norm(quoted)
    if not q or lines is None:
        return line, "unquoted"
    if 1 <= line <= len(lines) and _norm(lines[line - 1]) == q:
        return line, "here"
    hits = [k + 1 for k, t in enumerate(lines) if _norm(t) == q]
    return (hits[0], "moved") if len(hits) == 1 else (line, "gone")


def _code_lines(lines: list[str]) -> list[tuple[str, str]]:
    """Each line of Java or Kotlin source as ``(code, bare)``: ``code`` without its comments (``//``, ``/* */``
    over several lines, javadoc), ``bare`` without its string and char literals (text blocks too) as well."""
    out: list[tuple[str, str]] = []
    in_block = in_text = False
    for t in lines:
        code: list[str] = []
        bare: list[str] = []
        i, n = 0, len(t)
        while i < n:
            if in_block:
                e = t.find("*/", i)
                if e < 0:
                    break
                in_block, i = False, e + 2
                code.append(" ")
                bare.append(" ")
                continue
            if in_text:
                e = t.find('"""', i)
                code.append(t[i:] if e < 0 else t[i:e + 3])
                bare.append(" ")
                if e < 0:
                    break
                in_text, i = False, e + 3
                continue
            if t.startswith("//", i):
                break
            if t.startswith("/*", i):
                in_block, i = True, i + 2
                continue
            if t.startswith('"""', i):
                in_text, i = True, i + 3
                code.append('"""')
                continue
            c = t[i]
            if c in "\"'":
                j = i + 1
                while j < n and t[j] != c:
                    j += 2 if t[j] == "\\" else 1
                code.append(t[i:j + 1])
                bare.append(" ")
                i = j + 1
                continue
            code.append(c)
            bare.append(c)
            i += 1
        out.append(("".join(code), "".join(bare)))
    return out


def _api_line(lines: list[str] | None, target: str) -> tuple[int | None, str, bool]:
    """``(line, how, named)``: the first code line (outside comments) naming internal API ``target`` by its
    qualified name (an import, a qualified use, a ``Class.forName`` string) or by an import of its package
    (``named``); else the first naming its simple name outside strings (a guess: not ``named``), unless the file
    imports another class of that simple name."""
    if not lines:
        return None, "", False
    code = _code_lines(lines)
    top = target.split("$", 1)[0]
    pkg, _, simple = top.rpartition(".")
    qual = re.compile(r"(?<![\w.])" + re.escape(top) + r"(?![\w])")
    for k, (c, _b) in enumerate(code):
        if qual.search(c):
            return k + 1, f"names {top}", True
    if pkg:
        star = re.compile(r"^\s*import\s+(?:static\s+)?" + re.escape(pkg) + r"\.\*\s*;?\s*$")
        for k, (c, _b) in enumerate(code):
            if star.match(c):
                return k + 1, f"imports {pkg}.*", True
    other = re.compile(r"^\s*import\s+([\w.]+)\." + re.escape(simple) + r"\s*;?\s*$")
    for c, _b in code:
        m = other.match(c)
        if m and m.group(1) != pkg:
            return None, f"imports another {simple} ({m.group(1)}.{simple})", False
    plain = re.compile(r"(?<![\w.])" + re.escape(simple) + r"(?![\w])")
    for k, (_c, b) in enumerate(code):
        if plain.search(b):
            return k + 1, f"names {simple} (its simple name only: a guess)", False
    return None, "", False


# -- the command --------------------------------------------------------------------------------------------

def _versions(text: str, name: str) -> dict[str, dict]:
    out = {}
    for tool, rx in _VERSIONS.items():
        m = rx.search(text)
        if m:
            out[tool] = {"version": m.group(1), "at": f"{name}:{text.count(chr(10), 0, m.start()) + 1}"}
    return out


def report(root: Path, files: list[str], paths: list[str] | None = None, *, tool: str = "auto",
           limit: int = 40) -> dict:
    """The Error Prone, NullAway and jdeps findings in the log and report ``files`` as claims (inside ``paths``
    when given), errors first. ``tool`` keeps one tool's findings (``errorprone`` keeps NullAway's too: it is an
    Error Prone plugin); a SARIF file is read whole."""
    if tool not in TOOLS:
        raise ValueError(f"--tool must be one of {', '.join(TOOLS)}")
    root = Path(root).resolve()
    repo = _Repo(root)
    wanted = [ci._norm_path(p) for p in paths or []]
    wanted = [] if "" in wanted else wanted
    inputs: list[dict] = []
    claims: list[dict] = []
    elsewhere: set[str] = set()
    unknown_classes: set[str] = set()
    counts = {"findings": 0, "not_in_repository": 0, "ambiguous": 0, "unknown_class": 0, "outside_paths": 0,
              "other_tool": 0, "duplicates": 0, "javac_other": 0, "not_internal": 0, "unreadable": 0,
              "sarif_results": 0}
    seen: set[tuple] = set()
    for f in files:
        p = Path(f) if Path(f).is_absolute() else root / f
        name = ci._display(root, p)
        try:
            size = p.stat().st_size
            if size > MAX_LOG_BYTES:
                inputs.append({"file": name, "error": f"larger than {MAX_LOG_BYTES} bytes"})
                continue
            data = p.read_bytes()
            mtime = p.stat().st_mtime
        except OSError as exc:
            inputs.append({"file": name, "error": str(exc)[:200] or type(exc).__name__})
            continue
        text = _decode(data)
        if text.lstrip().startswith("{"):
            inputs.append(_read_sarif(root, p, name, wanted, claims, counts))
            continue
        # lines end at "\n" only (as grep -n and _versions count them): a lone "\r" (progress output) or a form
        # feed does not shift every line number after it
        lines = text.split("\n")
        versions = _versions(text, name)
        del data, text
        cleaned = [clean(x) for x in lines]   # once, for both parsers
        del lines
        entry: dict = {"file": name, "format": "log", "found": {}}
        if versions:
            entry["versions"] = {k: v["version"] for k, v in versions.items()}
        if tool in ("auto", "errorprone", "nullaway"):
            diags, c = parse_javac([], cleaned)
            for k, v in c.items():
                counts[k] += v
            for d in diags:
                counts["findings"] += 1
                if tool == "nullaway" and d["tool"] != "NullAway":
                    counts["other_tool"] += 1
                    continue
                entry["found"][d["tool"]] = entry["found"].get(d["tool"], 0) + 1
                row = _javac_claim(repo, d, name, mtime, versions, counts, elsewhere)
                _keep(row, wanted, counts, seen, claims)
        if tool in ("auto", "jdeps"):
            uses, repl, c = parse_jdeps([], cleaned)
            for k, v in c.items():
                counts[k] += v
            for u in uses:
                counts["findings"] += 1
                entry["found"]["jdeps"] = entry["found"].get("jdeps", 0) + 1
                row = _jdeps_claim(repo, u, repl, name, mtime, counts, unknown_classes)
                _keep(row, wanted, counts, seen, claims)
        if not entry["found"]:
            entry["note"] = ("no " + {"auto": "Error Prone, NullAway or jdeps", "errorprone": "Error Prone",
                                      "nullaway": "NullAway", "jdeps": "jdeps JDK-internal"}[tool]
                             + " finding in this file")
        inputs.append(entry)
    claims.sort(key=_order)
    by_status: dict[str, int] = {}
    by_tool: dict[str, int] = {}
    for c in claims:
        by_status[c["status"]] = by_status.get(c["status"], 0) + 1
        by_tool[c["stated_by"]] = by_tool.get(c["stated_by"], 0) + 1
    res = {
        "inputs": inputs,
        "coverage": {"method": "javac diagnostics with an Error Prone or NullAway check (plain, Gradle, Maven, Ant "
                               "logs) and jdeps -jdkinternals output read from files; a diagnostic's file tied to "
                               "the repository by path (absolute, relative, or the longest existing suffix) and "
                               "checked against the source line javac quoted; a jdeps class tied to its source "
                               "file by package path; SARIF files read by verinoda.sarif",
                     "limits": list(LIMITS)},
        "summary": {**counts, "claims": len(claims), "by_status": by_status, "by_tool": by_tool},
        "claims": claims[:limit],
        "not_in_repository": sorted(elsewhere)[:20],
        "unknown_classes": sorted(unknown_classes)[:20],
    }
    if len(claims) > limit:
        res["truncated"] = True
        res["claims_not_shown"] = len(claims) - limit
    return res


def _order(c: dict) -> tuple:
    """Errors first, then by file and line as numbers (Foo.java:9 before Foo.java:10)."""
    rel, _, ln = c["at"].rpartition(":")
    if not ln.isdigit():
        rel, ln = c["at"], "0"
    return LEVEL_ORDER.get(c["level"], 9), rel, int(ln), c.get("column") or 0


def _keep(row: dict | None, wanted: list[str], counts: dict, seen: set, claims: list[dict]) -> None:
    if row is None:
        return
    # a build that compiled a file twice prints its warnings twice; jdeps lists an anonymous or inner class's use
    # of an API apart from its top-level class's, which is the same line of the same file. Two findings at two
    # columns of one line are two findings
    key = (row["stated_by"], row["rule"], row["at"], row.get("column"), row.get("internal_api") or row["claim"])
    if key in seen:
        counts["duplicates"] += 1
        return
    seen.add(key)
    rel = row["at"].split(":", 1)[0]
    if wanted and not any(rel == w or rel.startswith(w + "/") for w in wanted):
        counts["outside_paths"] += 1
        return
    claims.append(row)


def _read_sarif(root: Path, p: Path, name: str, wanted: list[str], claims: list[dict], counts: dict) -> dict:
    res = sarif.report(root, [str(p)], wanted or None, limit=10 ** 9)
    log = res["sarif"][0] if res["sarif"] else {"file": name, "error": "not read"}
    if "runs" not in log:
        return {"file": name, "format": "sarif", "error": log.get("error")}
    s = res["summary"]
    counts["sarif_results"] += s["results"]
    counts["not_in_repository"] += s["not_in_repository"]
    counts["outside_paths"] += s["outside_paths"]
    counts["unreadable"] += s["unreadable"]
    claims.extend(res["claims"])
    return {"file": name, "format": "sarif", "found": {r["tool"]: r["results"] for r in log["runs"]},
            **({"versions": {r["tool"]: r["version"] for r in log["runs"] if r.get("version")}}
               if any(r.get("version") for r in log["runs"]) else {})}


def _javac_claim(repo: _Repo, d: dict, name: str, mtime: float, versions: dict, counts: dict,
                 elsewhere: set) -> dict | None:
    rel, how = repo.place(d["path"])
    if rel is None:
        counts["ambiguous" if how == "ambiguous" else "not_in_repository"] += 1
        elsewhere.add(d["path"])
        return None
    stale = repo.newer_than(rel, mtime)
    line, quote = _where_quoted(repo.lines(rel), d["line"], d["quoted"])
    notes = []
    if quote == "here":
        status = "strong_inference"   # the code the tool reported on is still at that line
        if stale or how == "suffix":
            notes.append(f"the line javac quoted is still at {line}")
    elif quote == "moved":
        status = "weak_inference"
        notes.append(f"the line javac quoted at {d['line']} is at {line} now")
    elif quote == "gone":
        status = "weak_inference"
        notes.append(f"the line javac quoted at {d['line']} is not in the file now")
    else:
        status = "weak_inference" if stale or how == "suffix" else "strong_inference"
        if stale:
            notes.append("the file changed after the log was written: lines may have moved")
    if how == "suffix":
        notes.append(f"named {_clip(d['path'])}, tied to {rel} by its path suffix")
    at = f"{rel}:{line}"
    tool = d["tool"]
    ver = (versions.get(tool) or {})
    text = f"{tool}{(' ' + ver['version']) if ver else ''} reports {d['check']} ({d['level']}): {_clip(d['message'])}"
    if notes:
        text += " (" + "; ".join(notes) + ")"
    row = {"subject": f"{tool}/{d['check']}", "at": at, "claim": text, "status": status, "stated_by": tool,
           "rule": d["check"], "level": d["level"], "message": _clip(d["message"]),
           "evidence_at": [at, f"{name}:{d['log_line']}"], "derived_by": DERIVED_BY}
    if ver:
        row["tool_version"] = ver["version"]
        row["evidence_at"].append(ver["at"])
    if d["column"]:
        row["column"] = d["column"]
    for k in ("hint", "see"):
        if d[k]:
            row["suggestion" if k == "hint" else "see"] = d[k]
    if d["quoted"]:
        row["quoted"] = _clip(d["quoted"])
    sym = repo.symbol(rel, line)
    if sym:
        row["symbol"] = sym
    return row


def _jdeps_claim(repo: _Repo, u: dict, repl: dict[str, str], name: str, mtime: float, counts: dict,
                 unknown: set) -> dict | None:
    rel, how = repo.class_file(u["class"])
    if rel is None:
        counts["ambiguous" if how == "ambiguous" else "unknown_class"] += 1
        unknown.add(u["class"])
        return None
    line, found, named = _api_line(repo.lines(rel), u["target"])
    stale = repo.newer_than(rel, mtime)
    level = "error" if u["removed"] else "warning"
    where = f" ({u['where']})" if u["where"] else ""
    text = f"jdeps reports {u['class']} uses JDK internal API {u['target']}{where}"
    replacement = replacement_of(u["target"], repl)
    if replacement:
        text += f"; suggested replacement: {_clip(replacement)}"
    notes = [f"the class is tied to {rel} by its package path"]
    if line:
        notes.append(f"line {line} {found}")
    else:
        notes.append("no line of the file names it" + (f": it {found}" if found else ""))
    if stale:
        notes.append("the file changed after the report was written")
    text += " (" + "; ".join(notes) + ")"
    at = f"{rel}:{line}" if line else rel
    row = {"subject": f"jdeps/{u['target']}", "at": at, "claim": text,
           "status": "strong_inference" if line and named and not stale else "weak_inference", "stated_by": "jdeps",
           "rule": "jdk-internal-api", "level": level, "class": u["class"], "internal_api": u["target"],
           "evidence_at": [at, f"{name}:{u['log_line']}"], "derived_by": DERIVED_BY}
    if u["where"]:
        row["module"] = u["where"]
    if u["archive"]:
        row["archive"] = u["archive"]
    if replacement:
        row["replacement"] = replacement
    if line:
        sym = repo.symbol(rel, line)
        if sym:
            row["symbol"] = sym
    return row


def render_text(res: dict) -> str:
    s = res["summary"]
    parts = []
    for x in res["inputs"]:
        if x.get("error"):
            parts.append(f"{x['file']} (error: {x['error']})")
        else:
            found = ", ".join(f"{k}: {v}" for k, v in x.get("found", {}).items()) or x.get("note") or "nothing"
            parts.append(f"{x['file']} ({found})")
    out = [f"JVM checker findings: {s['claims']} in the repository from " + ", ".join(parts)]
    labels = {"not_in_repository": "not in the repository", "unknown_class": "class(es) with no source file",
              "ambiguous": "matching several files", "outside_paths": "outside the paths",
              "other_tool": "of another tool", "duplicates": "repeated", "javac_other": "javac's own messages",
              "not_internal": "jdeps dependences on no internal API", "unreadable": "unreadable lines"}
    skipped = [f"{s[k]} {label}" for k, label in labels.items() if s.get(k)]
    if skipped:
        out.append("  not listed: " + ", ".join(skipped))
    if res.get("unknown_classes"):
        out.append("  classes with no source file here: " + ", ".join(res["unknown_classes"][:5])
                   + (" ..." if len(res["unknown_classes"]) > 5 else ""))
    out.append("  each finding is the named tool's statement, not checked by Verinoda")
    out.append("")
    for c in res["claims"][:30]:
        out.append(f"  [{c['status']}] {c['claim']}")
        out.append(f"      at {c['at']}" + (f" ({c['symbol']})" if c.get("symbol") else "")
                   + f"  log {c['evidence_at'][1]}")
        if c.get("suggestion"):
            out.append(f"      {c['suggestion']}")
    if res.get("truncated"):
        out.append(f"  ... {res['claims_not_shown']} more (--limit, --json)")
    return "\n".join(out)
