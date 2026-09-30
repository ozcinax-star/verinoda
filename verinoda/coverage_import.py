"""Coverage reports read into lines and symbols, in any language (``verinoda coverage`` and the review's tests).

Formats: lcov (``lcov.info``: istanbul/nyc, c8, Jest, Vitest, gcov through lcov, cargo-llvm-cov, ...),
Cobertura XML (coverage.py's ``coverage.xml``, istanbul's cobertura, gcovr, Cobertura itself), JaCoCo XML
(Gradle ``jacocoTestReport.xml``, Maven ``jacoco.xml``) and coverage.py's JSON (``coverage.json``, whose
``contexts`` name the test that ran each line when coverage recorded them with ``dynamic_context =
test_function``). A report is found at the usual paths (:data:`REPORT_PATHS`, also one folder down for Gradle
and Maven sub-projects) or given by path; every report found is read and merged (a line run in any of them
ran). A report found in a sub-project folder (``web/coverage/lcov.info``) names its files relative to that
folder: its entries are kept under the folder (``web/src/index.ts``) and fit only files inside it, so two
sub-projects' ``src/index.ts`` stay apart.

A report's file names are tied to the repository's files by path: an absolute path under the repository, a
path joined with a Cobertura ``<source>`` root, else the longest path suffix the two share (JaCoCo names
``com/ex/Foo.java``, the repository ``src/main/java/com/ex/Foo.java``); two report entries that fit one file
equally well leave it ``ambiguous``. Lines map to the innermost definition around them
(:func:`verinoda.treestate.symbol_at` over :func:`verinoda.anchors.compute_facts`).

What a report says is a measurement made by another tool, on the tree that tool saw: a claim built from it is
``strong_inference`` while every report that measured the file is newer than it, ``weak_inference`` when the
file changed after one of them was written (its lines may have moved). Nothing is run; the reports are read,
never written.
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# the paths coverage tools write their reports to by default, relative to the project root
REPORT_PATHS = ("coverage.xml", "reports/coverage.xml", "build/coverage.xml", "coverage.json", "lcov.info",
                "coverage/lcov.info", "coverage/lcov/lcov.info", "coverage/cobertura-coverage.xml",
                "build/reports/jacoco/test/jacocoTestReport.xml", "target/site/jacoco/jacoco.xml")
# searched in each sub-project folder too (multi-module Gradle and Maven builds)
SUBPROJECT_REPORT_PATHS = ("build/reports/jacoco/test/jacocoTestReport.xml", "target/site/jacoco/jacoco.xml",
                           "coverage/lcov.info")
MAX_REPORT_BYTES = 200_000_000
MAX_TESTS_PER_LINE = 20
LIMITS = [
    "a report is what the coverage tool measured on the tree it ran on: a file changed after the report was "
    "written is matched by line number and may have moved (weak_inference)",
    "lines a report does not list are not executable to that tool (blank lines, comments, declarations) or were "
    "not measured; they are neither covered nor uncovered",
    "JaCoCo counts bytecode instructions per line: a line is covered when any of its instructions ran",
    "branches are not read: a covered line may still have a branch no test took",
    "which test ran a line is known only from lcov test names (TN:) and coverage.py contexts; other reports say "
    "that some test ran it",
]


@dataclass
class FileCov:
    key: str                                     # the file as the report names it, posix (under ``scope``)
    lines: dict[int, int] = field(default_factory=dict)   # line -> hits (JaCoCo: covered instructions)
    tests: dict[int, set[str]] = field(default_factory=dict)
    reports: set[str] = field(default_factory=set)
    scope: str = ""   # the sub-project folder whose report named the file relative to it ("" = the root)

    @property
    def name(self) -> str:
        """The path as the report printed it, without the sub-project folder."""
        return self.key[len(self.scope) + 1:] if self.scope and self.key.startswith(self.scope + "/") else self.key


@dataclass
class Coverage:
    root: Path
    reports: list[dict] = field(default_factory=list)   # {"file", "format", "files", "mtime"} or {"file", "error"}
    files: dict[str, FileCov] = field(default_factory=dict)
    _by_name: dict[str, list[str]] | None = None
    _match: dict[str, tuple[str | None, str]] = field(default_factory=dict)

    @property
    def read(self) -> list[dict]:
        return [r for r in self.reports if not r.get("error")]

    def resolve(self, rel: str) -> tuple[str | None, str]:
        """The report entry of repository file ``rel`` and how it was matched (``exact``, ``suffix``), or
        ``(None, "absent" | "ambiguous")``."""
        if rel in self._match:
            return self._match[rel]
        if rel in self.files:
            self._match[rel] = (rel, "exact")
            return self._match[rel]
        if self._by_name is None:
            self._by_name = {}
            for k in self.files:
                self._by_name.setdefault(k.rsplit("/", 1)[-1].lower(), []).append(k)
        best: list[str] = []
        best_n: tuple[int, int] = (0, 0)
        for k in self._by_name.get(rel.rsplit("/", 1)[-1].lower(), []):
            fc = self.files[k]
            # a sub-project's entry fits only files inside that sub-project, matched below its folder
            if fc.scope and not rel.startswith(fc.scope + "/"):
                continue
            rparts = rel[len(fc.scope) + 1:].split("/") if fc.scope else rel.split("/")
            kparts = fc.name.split("/")
            n = 0
            while n < min(len(kparts), len(rparts)) and kparts[-1 - n] == rparts[-1 - n]:
                n += 1
            # the whole of the shorter path must match: `a/Foo.java` does not fit `b/Foo.java`
            if n < min(len(kparts), len(rparts)):
                continue
            # the report of the sub-project the file lies in comes before a report of the whole repository
            score = (fc.scope.count("/") + 1 if fc.scope else 0, n)
            if score > best_n:
                best, best_n = [k], score
            elif score == best_n:
                best.append(k)
        out = (best[0], "suffix") if len(best) == 1 else (None, "ambiguous" if best else "absent")
        self._match[rel] = out
        return out

    def lines_of(self, rel: str) -> FileCov | None:
        key, _how = self.resolve(rel)
        return self.files.get(key) if key else None

    def oldest(self, rel: str) -> float | None:
        """Modification time of the oldest report that measured ``rel`` (its lines are merged with the others')."""
        fc = self.lines_of(rel)
        if fc is None:
            return None
        times = [r["mtime"] for r in self.read if r["file"] in fc.reports]
        return min(times) if times else None

    def stale(self, rel: str) -> bool:
        """Did ``rel`` change after one of the reports that measured it was written?"""
        t = self.oldest(rel)
        try:
            return t is None or (self.root / rel).stat().st_mtime > t
        except OSError:
            return True


# -- finding and reading reports ------------------------------------------------------------------------

def find_reports(root: Path) -> list[Path]:
    root = Path(root)
    out = [root / p for p in REPORT_PATHS if (root / p).is_file()]
    try:
        subs = sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith((".", "_"))
                      and d.name not in ("node_modules", "build", "target", "coverage", "reports"))
    except OSError:
        subs = []
    for d in subs:
        out += [d / p for p in SUBPROJECT_REPORT_PATHS if (d / p).is_file()]
    return out


def _scope_of(name: str) -> tuple[str, bool]:
    """The folder a report at repository path ``name`` names its files relative to, and whether that is
    certain: a usual report path (``web/coverage/lcov.info`` -> ``web``), else the report's own folder, tried
    only for files that exist there."""
    for pat in sorted({*REPORT_PATHS, *SUBPROJECT_REPORT_PATHS}, key=len, reverse=True):
        if name == pat:
            return "", True
        if name.endswith("/" + pat):
            return name[:-len(pat) - 1], True
    return (name.rsplit("/", 1)[0] if "/" in name else ""), False


def load(root: Path, paths: list[str | Path] | None = None) -> Coverage:
    """Read the reports at ``paths`` (relative to ``root`` or absolute), or the ones :func:`find_reports`
    finds, into one :class:`Coverage`. A report that cannot be read is listed with its error."""
    root = Path(root).resolve()
    cov = Coverage(root)
    found = [Path(p) if Path(p).is_absolute() else root / p for p in paths] if paths else find_reports(root)
    for p in found:
        name = _display(root, p)
        scope, strict = ("", False) if Path(name).is_absolute() else _scope_of(name)
        try:
            if p.stat().st_size > MAX_REPORT_BYTES:
                cov.reports.append({"file": name, "error": f"larger than {MAX_REPORT_BYTES} bytes"})
                continue
            fmt, entries = parse(p.read_bytes(), root, scope=scope, strict=strict)
            mtime = p.stat().st_mtime
        # a report's content is another tool's output: whatever shape it has, it is an error entry, not a crash
        except (OSError, ValueError, ET.ParseError, TypeError, AttributeError, KeyError, OverflowError,
                RecursionError) as exc:
            cov.reports.append({"file": name, "error": str(exc)[:200] or type(exc).__name__})
            continue
        for key, lines, tests in entries:
            fc = cov.files.setdefault(key, FileCov(key, scope=scope if scope and key.startswith(scope + "/")
                                                   else ""))
            for ln, hits in lines.items():
                fc.lines[ln] = fc.lines.get(ln, 0) + hits
            for ln, names in tests.items():
                fc.tests.setdefault(ln, set()).update(names)
            fc.reports.add(name)
        cov.reports.append({"file": name, "format": fmt, "files": len(entries), "mtime": mtime})
    return cov


def _display(root: Path, p: Path) -> str:
    try:
        return p.resolve().relative_to(root).as_posix()
    except ValueError:
        return p.as_posix()


Entry = tuple[str, dict[int, int], dict[int, set[str]]]


def parse(data: bytes, root: Path, *, scope: str = "", strict: bool = True) -> tuple[str, list[Entry]]:
    """``(format, [(file key, {line: hits}, {line: test names})])`` of one report. ``scope``: the folder the
    report's relative paths are relative to (see :func:`_key`)."""
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    where = _Where(Path(root), scope, strict)
    if head.startswith(b"<"):
        tree = ET.fromstring(data)
        if tree.tag == "report":
            return "jacoco", _jacoco(tree, where)
        if tree.tag == "coverage":
            return "cobertura", _cobertura(tree, where)
        raise ValueError(f"an XML report with root <{tree.tag}> is neither Cobertura nor JaCoCo")
    if head.startswith(b"{"):
        return "coverage.py json", _coverage_json(json.loads(data.decode("utf-8")), where)
    text = data.decode("utf-8", "replace")
    if re.search(r"^SF:", text, re.M):
        return "lcov", _lcov(text, where)
    raise ValueError("not a coverage report this reader knows (lcov, Cobertura XML, JaCoCo XML, coverage.py JSON)")


@dataclass
class _Where:
    root: Path
    scope: str = ""
    strict: bool = True


def _key(root: Path, path: str, scope: str = "", strict: bool = True) -> str:
    """A report's file name as a repository-relative posix path when it lies under ``root``, else as given. A
    relative name from a report of sub-project ``scope`` is put under that folder when the file is there, or
    when ``strict`` (the report lies at the sub-project's usual path) and the name is not a repository file."""
    p = path.strip().replace("\\", "/")
    if re.match(r"^[A-Za-z]:/|^/", p):
        try:
            rel = os.path.relpath(p, root)
        except ValueError:   # another drive
            return p
        rel = rel.replace("\\", "/")
        if not rel.startswith("../"):
            return str(PurePosixPath(rel))
        return p
    while p.startswith("./"):
        p = p[2:]
    p = str(PurePosixPath(p)) if p else p
    if scope and p:
        if (root / scope / p).exists():
            return f"{scope}/{p}"
        if strict and not (root / p).exists():
            return f"{scope}/{p}"
    return p


def _lcov(text: str, w: _Where) -> list[Entry]:
    out: dict[str, tuple[dict[int, int], dict[int, set[str]]]] = {}
    test = ""
    cur: tuple[dict[int, int], dict[int, set[str]]] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("TN:"):
            test = line[3:].strip()
        elif line.startswith("SF:"):
            cur = out.setdefault(_key(w.root, line[3:], w.scope, w.strict), ({}, {}))
        elif line.startswith("DA:") and cur is not None:
            parts = line[3:].split(",")
            try:
                ln, hits = int(parts[0]), int(float(parts[1]))
            except (ValueError, IndexError, OverflowError):
                continue
            cur[0][ln] = cur[0].get(ln, 0) + hits
            if hits > 0 and test:
                cur[1].setdefault(ln, set()).add(test)
        elif line == "end_of_record":
            cur = None
    return [(k, v[0], v[1]) for k, v in out.items()]


def _cobertura(tree: ET.Element, w: _Where) -> list[Entry]:
    root = w.root
    sources = [s.text.strip() for s in tree.iter("source") if s.text and s.text.strip()]
    out: dict[str, dict[int, int]] = {}
    for cls in tree.iter("class"):
        fn = cls.get("filename")
        if not fn:
            continue
        key = None
        for src in sources:   # a source root: the file under it, when it lies in the repository
            joined = os.path.join(src, fn)
            if os.path.isabs(joined) and os.path.exists(joined):
                key = _key(root, joined)
                break
            if not os.path.isabs(joined):
                cand = _key(root, joined, w.scope, False)
                if (root / cand).exists():
                    key = cand
                    break
        key = key or _key(root, fn, w.scope, w.strict)
        lines = out.setdefault(key, {})
        # a class's own <lines> (its methods repeat them)
        for block in cls.findall("lines"):
            for ln in block.findall("line"):
                try:
                    n, hits = int(ln.get("number", "")), int(float(ln.get("hits", "0")))
                except (ValueError, OverflowError):
                    continue
                lines[n] = max(lines.get(n, 0), hits)
    return [(k, v, {}) for k, v in out.items()]


def _jacoco(tree: ET.Element, w: _Where) -> list[Entry]:
    out: list[Entry] = []
    for pkg in tree.iter("package"):
        pname = (pkg.get("name") or "").strip("/")
        for sf in pkg.findall("sourcefile"):
            name = sf.get("name")
            if not name:
                continue
            lines: dict[int, int] = {}
            for ln in sf.findall("line"):
                try:
                    n, mi, ci = int(ln.get("nr", "")), int(ln.get("mi", "0")), int(ln.get("ci", "0"))
                except ValueError:
                    continue
                if mi or ci:
                    lines[n] = ci
            out.append((_key(w.root, f"{pname}/{name}" if pname else name, w.scope, w.strict), lines, {}))
    return out


def _coverage_json(doc: dict, w: _Where) -> list[Entry]:
    files = doc.get("files")
    if not isinstance(files, dict):
        raise ValueError("a JSON report without a files object is not coverage.py's")
    out: list[Entry] = []
    for fn, info in files.items():
        if not isinstance(info, dict):
            continue
        lines = {int(n): 1 for n in info.get("executed_lines") or []}
        lines.update({int(n): 0 for n in info.get("missing_lines") or []})
        tests: dict[int, set[str]] = {}
        for n, ctxs in (info.get("contexts") or {}).items():
            names = {c.split("|", 1)[0] for c in ctxs or [] if c and c.split("|", 1)[0]}
            if names and str(n).isdigit():
                tests[int(n)] = names
        out.append((_key(w.root, fn, w.scope, w.strict), lines, tests))
    return out


# -- lines and symbols ----------------------------------------------------------------------------------

def ranges(lines) -> str:
    """``3-5, 9`` for {3, 4, 5, 9}."""
    xs = sorted(set(lines))
    out: list[str] = []
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[j + 1] == xs[j] + 1:
            j += 1
        out.append(str(xs[i]) if i == j else f"{xs[i]}-{xs[j]}")
        i = j + 1
    return ", ".join(out)


def facts_of(root: Path, rel: str) -> dict | None:
    from verinoda import anchors

    try:
        f = anchors.compute_facts(rel, (Path(root) / rel).read_bytes())
    except OSError:
        return None
    return f if anchors.usable(f) else None


def symbol_of(facts: dict | None, line: int) -> str | None:
    from verinoda import treestate

    return treestate.symbol_at(facts, line) if facts else None


def tests_of(fc: FileCov, lines) -> list[str]:
    names: set[str] = set()
    for ln in lines:
        names |= fc.tests.get(ln, set())
    return sorted(names)


def status_for(cov: Coverage, rel: str) -> str:
    return "weak_inference" if cov.stale(rel) else "strong_inference"


# -- the command --------------------------------------------------------------------------------------

def report(root: Path, paths: list[str] | None = None, *, reports: list[str] | None = None,
           base_reports: list[str] | None = None, base: str | None = None, limit: int = 40) -> dict:
    """Per symbol of the measured files (or of ``paths``): the measured lines that ran and the ones that did not,
    with the tests that ran them when the report names them. With ``base_reports``: the lines whose coverage
    changed between the two reports, split into the files the working tree changed against ``base``
    (default HEAD) and the others (indirect changes)."""
    root = Path(root).resolve()
    cov = load(root, reports)
    if not cov.read:
        why = "; ".join(f"{r['file']}: {r['error']}" for r in cov.reports) or \
            "no report at " + ", ".join(REPORT_PATHS)
        raise ValueError(f"no coverage report read ({why}); write one with your test runner (pytest --cov "
                         "--cov-report=xml, jest --coverage, gradle jacocoTestReport) or pass --report FILE")
    # "." and "./" are the whole repository; "./a.py" is "a.py"
    wanted = [_norm_path(p) for p in paths or []]
    repo_files, unmatched, ambiguous = _repo_files(cov, [] if "" in wanted else wanted)
    paths_unmatched = [p for p, w in zip(paths or [], wanted)
                       if w and not any(f == w or f.startswith(w + "/") for f in repo_files)]
    claims: list[dict] = []
    per_file = []
    for rel in sorted(repo_files):
        fc = cov.lines_of(rel)
        facts = facts_of(root, rel)
        status = status_for(cov, rel)
        measured = {ln for ln in fc.lines}
        ran = {ln for ln, h in fc.lines.items() if h > 0}
        per_file.append({"file": rel, "measured": len(measured), "covered": len(ran),
                         "percent": _pct(len(ran), len(measured)), "stale": status == "weak_inference"})
        by_sym: dict[str, set[int]] = {}
        for ln in measured:
            by_sym.setdefault(symbol_of(facts, ln) or "<module>", set()).add(ln)
        for sym, lns in by_sym.items():
            ok = lns & ran
            miss = lns - ran
            at = f"{rel}:{_def_line(facts, sym) or min(lns)}"
            name = f"{rel}::{sym}"
            text = (f"{len(ok)} of {len(lns)} measured line(s) of `{sym}` ran under the tests"
                    + (f"; not run: {ranges(miss)}" if miss else ""))
            if status == "weak_inference":
                text += " (the file changed after the report was written: lines may have moved)"
            row = {"subject": name, "at": at, "claim": text, "status": status, "measured": len(lns),
                   "covered": len(ok), "not_run": ranges(miss), "evidence_at": [at, *sorted(fc.reports)],
                   "derived_by": "verinoda.coverage_import (a coverage report read by line)"}
            tests = tests_of(fc, ok)
            if tests:
                row["tests"] = tests[:MAX_TESTS_PER_LINE]
                if len(tests) > MAX_TESTS_PER_LINE:
                    row["tests_total"] = len(tests)
            claims.append(row)
    claims.sort(key=lambda c: (-(c["measured"] - c["covered"]), c["covered"] / max(c["measured"], 1), c["at"]))
    measured_total = sum(f["measured"] for f in per_file)
    covered_total = sum(f["covered"] for f in per_file)
    res = {
        "reports": [{k: r[k] for k in ("file", "format", "files", "error") if k in r} for r in cov.reports],
        "coverage": {"method": "coverage reports read by line (lcov, Cobertura XML, JaCoCo XML, coverage.py "
                               "JSON), their files tied to the repository's by path, lines to the innermost "
                               "definition around them", "limits": list(LIMITS)},
        "summary": {"files": len(per_file), "measured_lines": measured_total, "covered_lines": covered_total,
                    "percent": _pct(covered_total, measured_total), "symbols": len(claims),
                    "report_files_not_in_repository": len(unmatched), "ambiguous": len(ambiguous)},
        "files": per_file,
        "claims": claims[:limit],
        "report_files_not_in_repository": unmatched[:20],
        "paths_not_measured": paths_unmatched,
        "ambiguous": ambiguous[:20],
    }
    if len(claims) > limit:
        res["truncated"] = True
        res["claims_not_shown"] = len(claims) - limit
    if base_reports:
        res["changes"] = _changes(root, cov, load(root, base_reports), base or "HEAD")
    return res


def _norm_path(p: str) -> str:
    p = p.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    p = p.rstrip("/")
    return "" if p in ("", ".") else p


def _pct(a: int, b: int) -> float | None:
    return round(100.0 * a / b, 1) if b else None


def _def_line(facts: dict | None, sym: str) -> int | None:
    s = ((facts or {}).get("symbols") or {}).get(sym)
    return s.get("def") or s.get("start") if s else None


def _repo_files(cov: Coverage, wanted: list[str]) -> tuple[set[str], list[str], list[str]]:
    """Repository files the reports measured (inside ``wanted`` when given), the report entries that fit no
    repository file, and the files two entries fit equally well."""
    from verinoda import treestate

    listed = treestate._git(cov.root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if listed is not None:
        files = [p for p in listed.split("\0") if p]
    else:
        files = [p.relative_to(cov.root).as_posix() for p in cov.root.rglob("*") if p.is_file()
                 and ".git" not in p.parts]
    names = {k.rsplit("/", 1)[-1].lower() for k in cov.files}
    out: set[str] = set()
    ambiguous: list[str] = []
    used: set[str] = set()
    for rel in files:
        if rel.rsplit("/", 1)[-1].lower() not in names:
            continue
        key, how = cov.resolve(rel)
        if key is None:
            if how == "ambiguous":
                ambiguous.append(rel)
            continue
        used.add(key)
        if not wanted or any(rel == w or rel.startswith(w + "/") for w in wanted):
            out.add(rel)
    unmatched = sorted(k for k in cov.files if k not in used)
    return out, unmatched, sorted(ambiguous)


def _changes(root: Path, head: Coverage, old: Coverage, base: str) -> dict:
    """Lines whose coverage differs between the base reports and the head reports."""
    from verinoda import treestate

    if not old.read:
        return {"error": "no base report read: " + "; ".join(f"{r['file']}: {r.get('error')}" for r in old.reports)}
    # the head coverage stays when the base cannot be named (no git, no commit yet, a bad ref)
    try:
        base_sha = treestate.resolve_commit(root, base)
        changed = set(treestate.changes_vs_base(root, base_sha)["tree_files"])
    except (ValueError, OSError) as exc:
        return {"error": f"the base {base!r} cannot be compared with the working tree: {exc}"}
    files, _u, _a = _repo_files(head, [])
    indirect, direct = [], []
    for rel in sorted(files):
        a, b = old.lines_of(rel), head.lines_of(rel)
        if a is None or b is None:
            continue
        lost = {ln for ln, h in a.lines.items() if h > 0 and b.lines.get(ln) == 0}
        gained = {ln for ln, h in a.lines.items() if h == 0 and b.lines.get(ln, 0) > 0}
        if not lost and not gained:
            continue
        facts = facts_of(root, rel)
        row = {"file": rel, "no_longer_run": ranges(lost), "now_run": ranges(gained),
               "symbols_no_longer_run": sorted({symbol_of(facts, ln) or "<module>" for ln in lost}),
               "evidence_at": [f"{rel}:{min(lost | gained)}"]}
        (direct if rel in changed else indirect).append(row)
    return {"base": {"ref": base, "commit": base_sha},
            "indirect": indirect,
            "in_changed_files": direct,
            "basis": "lines measured in both reports whose run/not-run state differs; indirect = in a file the "
                     "working tree did not change against the base, so the change reached it through other code "
                     "(or the tests changed)",
            "limits": ["the base reports are taken to measure the base commit and the head reports the working "
                       "tree; in changed files line numbers can move, so only indirect rows compare the same "
                       "lines"]}


def render_text(res: dict) -> str:
    s = res["summary"]
    out = [f"Coverage: {s['covered_lines']} of {s['measured_lines']} measured line(s) ran"
           + (f" ({s['percent']}%)" if s["percent"] is not None else "") + f" in {s['files']} file(s), from "
           + ", ".join(f"{r['file']} ({r.get('format') or 'error: ' + str(r.get('error'))})" for r in res["reports"])]
    if res.get("paths_not_measured"):
        out.append("  no report measured a file under: " + ", ".join(res["paths_not_measured"][:6]))
    if s["report_files_not_in_repository"] or s["ambiguous"]:
        out.append(f"  {s['report_files_not_in_repository']} report file(s) fit no file of the repository; "
                   f"{s['ambiguous']} file(s) fit two report entries (--json)")
    out.append("")
    out.append("Least covered first:")
    for c in res["claims"][:20]:
        out.append(f"  [{c['status']}] {c['claim']}")
        out.append(f"      at {c['at']}" + (f"; tests: {', '.join(c['tests'][:4])}" if c.get("tests") else ""))
    if res.get("truncated"):
        out.append(f"  ... {res['claims_not_shown']} more (--limit, --json)")
    ch = res.get("changes")
    if ch:
        out.append("")
        if ch.get("error"):
            out.append(f"Coverage changes: {ch['error']}")
        else:
            out.append(f"Indirect coverage changes (files unchanged against {ch['base']['ref']}): "
                       f"{len(ch['indirect'])}")
            for r in ch["indirect"][:10]:
                out.append(f"  {r['file']}: no longer run {r['no_longer_run'] or '-'}; now run {r['now_run'] or '-'}")
            if ch["in_changed_files"]:
                out.append(f"  and {len(ch['in_changed_files'])} changed file(s) whose coverage changed (--json)")
    return "\n".join(out)
