"""SARIF 2.1.0 in and out: ``review``, ``check`` and ``decide check`` written as SARIF (``--sarif``) for GitHub
code scanning and other SARIF viewers, and a linter's or CodeQL's SARIF read as evidence (``verinoda sarif``).

Out: one run per command, ``tool.driver.name`` ``Verinoda``, one rule per finding kind (``review/<concern>/<rule>``,
``decide/<decision>/<guard>``, ``check/<verdict>/<site kind>``), each result at the ``file:line`` the finding cites
(a repository-relative ``uri`` under ``%SRCROOT%``; a finding with no file is not written and is counted in the
run's ``not_exported``), with its claim status and evidence in ``properties``. The level follows the status
(:data:`LEVELS`): a statically verified statement is an ``error``, ``strong_inference`` a ``warning``,
``weak_inference`` and ``unknown`` a ``note``. A review finding is something to read, not a defect: it is a
``warning`` at most. A guard violation the baseline lists or a waiver covers is written with an external
suppression (still there, not failing).

In: a SARIF file's results become claims, one per result with a location in the repository. What the file says is
the named tool's statement about the tree it ran on, not checked here: ``strong_inference`` while the SARIF file
is newer than the file it names, ``weak_inference`` when the file changed after it (its lines may have moved) or
when the file was tied to the repository only by a path suffix. Suppressed results (an accepted suppression),
``pass`` / ``notApplicable`` results, results whose baseline state is ``absent`` and results that cannot be read
are counted, not listed. Nothing is run and nothing is written.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlparse
from urllib.request import url2pathname

from verinoda import coverage_import as ci

VERSION = "2.1.0"
SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
LEVELS = {"statically_verified": "error", "observed": "error", "experiment_verified": "error",
          "primary_source_verified": "error", "strong_inference": "warning", "weak_inference": "note",
          "unknown": "note"}
LEVEL_ORDER = {"error": 0, "warning": 1, "note": 2, "none": 3}
# check verdicts: absent is found by the closed-world rule (a static resolver's answer) - verified when the
# container is the standard library or the project's own code, strong_inference when it is an installed package,
# a stub, a classpath or the environment's sys.path (what was installed or built here, which CI's may not match);
# not_installed is the environment checked, which CI's may not be; guarded and exists are not findings
CHECK_STATUS = {"absent": "statically_verified", "not_installed": "strong_inference", "unknown": "unknown"}
SARIF_LEVELS = ("none", "note", "warning", "error")
MAX_MESSAGE = 300
MAX_SARIF_BYTES = 100_000_000
LIMITS = [
    "a SARIF result is the named tool's statement on the tree it ran on; Verinoda does not check it",
    "a file changed after the SARIF file was written is matched by line number and may have moved (weak_inference)",
    "a result is placed at its first physical location; logical-only locations and code flows are not read",
    "an absolute path that exists on no machine here (a CI runner's) is tied to the repository by its longest "
    "existing suffix, a guess (weak_inference); a relative path or a path to a file on this machine is not",
]
# path parts that mark a third-party or generated file: a suffix match through them is not tried
_FOREIGN = {"site-packages", "dist-packages", "node_modules", ".venv", "venv", "vendor", "third_party",
            ".tox", "bower_components"}
_ARG = re.compile(r"\{\{|\}\}|\{(\d+)\}")
_AT = re.compile(r"^(?P<path>.+?):(?P<line>\d+)(?:-(?P<end>\d+)|:(?P<col>\d+))?$")


# -- out ----------------------------------------------------------------------------------------------------

def _version() -> str:
    from verinoda import __version__

    return __version__


def _location(at: str | None) -> dict | None:
    """A SARIF location for ``path:line[:col]``, ``path:line-end`` or a bare ``path``; ``None`` for anything else
    (a symbol, nothing)."""
    if not at or not isinstance(at, str):
        return None
    at = at.strip().replace("\\", "/")
    m = _AT.match(at)
    path = m.group("path") if m else at
    if not path or "::" in path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return None   # a symbol, or a path outside the repository
    if not m and "." not in PurePosixPath(path).name and "/" not in path:
        return None   # a name, not a file
    phys: dict = {"artifactLocation": {"uri": quote(path, safe="/"), "uriBaseId": "%SRCROOT%"}}
    if m:
        region = {"startLine": int(m.group("line"))}
        if m.group("end"):
            region["endLine"] = max(int(m.group("end")), region["startLine"])
        if m.group("col"):
            region["startColumn"] = int(m.group("col"))
        if region["startLine"] < 1:
            return None
        phys["region"] = region
    return {"physicalLocation": phys}


def _clip(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= MAX_MESSAGE else text[:MAX_MESSAGE - 3] + "..."


class _Run:
    def __init__(self, command: str):
        self.command = command
        self.rules: dict[str, dict] = {}
        self.results: list[dict] = []
        self.not_exported = 0

    def add(self, rule: str, description: str, message: str, status: str, at: str | None, *, level: str | None = None,
            props: dict | None = None, suppression: str | None = None, baseline: str | None = None) -> None:
        loc = _location(at)
        if loc is None:
            self.not_exported += 1
            return
        if rule not in self.rules:
            self.rules[rule] = {"id": rule, "shortDescription": {"text": _clip(description)}}
        r = {"ruleId": rule, "level": level or LEVELS.get(status, "note"), "message": {"text": _clip(message)},
             "locations": [loc], "properties": {"status": status, **{k: v for k, v in (props or {}).items() if v}}}
        if suppression:
            r["suppressions"] = [{"kind": "external", "justification": _clip(suppression)}]
        if baseline:
            r["baselineState"] = baseline
        self.results.append(r)

    def log(self, exit_code, **extra) -> dict:
        rules = sorted(self.rules.values(), key=lambda r: r["id"])
        index = {r["id"]: i for i, r in enumerate(rules)}
        for r in self.results:
            r["ruleIndex"] = index[r["ruleId"]]
        run = {"tool": {"driver": {"name": "Verinoda", "version": _version(), "rules": rules}},
               "automationDetails": {"id": f"verinoda/{self.command.replace(' ', '-')}/"},
               "invocations": [{"executionSuccessful": True,
                                **({"exitCode": exit_code} if isinstance(exit_code, int) else {})}],
               "columnKind": "unicodeCodePoints",
               "results": self.results,
               "properties": {"command": f"verinoda {self.command}", "not_exported": self.not_exported,
                              **{k: v for k, v in extra.items() if v}}}
        return {"$schema": SCHEMA, "version": VERSION, "runs": [run]}


def from_review(res: dict) -> dict:
    """``review --json`` as SARIF: every finding listed under ``concerns`` and the changed lines a coverage report
    shows no test ran. A review finding is a ``warning`` at most. Findings past the review's per-concern cap are
    counted in ``not_listed`` and the review's unknowns in ``unknowns`` (neither has a location to write)."""
    run = _Run("review")
    shown = 0
    for concern, items in (res.get("concerns") or {}).items():
        for f in items or []:
            shown += 1
            status = f.get("status") or "unknown"
            level = LEVELS.get(status, "note")
            delta = f.get("delta")
            run.add(f"review/{concern}/{f.get('rule') or 'finding'}", f"{concern}: {f.get('rule') or 'finding'}",
                    f.get("finding") or "", status, f.get("at"), level="warning" if level == "error" else level,
                    props={"evidence_at": f.get("evidence_at"), "basis": f.get("basis"),
                           "derived_by": f.get("derived_by"), "delta": delta},
                    baseline={"introduced": "new", "preexisting": "unchanged"}.get(delta))
    for u in ((res.get("tests") or {}).get("coverage") or {}).get("uncovered") or []:
        status = u.get("status") or "weak_inference"
        run.add("review/tests/uncovered-changed-lines", "tests: changed lines no test ran", u.get("finding") or "",
                status, u.get("at"), level=LEVELS.get(status, "note"),
                props={"evidence_at": u.get("evidence_at"), "derived_by": u.get("derived_by")})
    truncated = res.get("concerns_truncated") or {}
    cut = sum(max(0, n - len((res.get("concerns") or {}).get(k) or [])) for k, n in truncated.items()
              if isinstance(n, int))
    return run.log(res.get("exit"), not_listed=cut, unknowns=len(res.get("unknown") or []))


def from_decide_check(res: dict) -> dict:
    """``decide check --json`` as SARIF: violations, possible violations, pre-existing ones, the ones a baseline
    lists or a waiver covers (suppressed), and the governed symbols to review."""
    run = _Run("decide check")

    def one(f: dict, *, suppression: str | None = None, baseline: str | None = None) -> None:
        status = f.get("status") or ("statically_verified" if f.get("level") == "VIOLATED" else "weak_inference")
        run.add(f"decide/{f.get('decision')}/{f.get('guard')}",
                f"decision {f.get('decision')} guard {f.get('guard')} ({f.get('kind')}): {f.get('what') or ''}",
                f"{f.get('level')} {f.get('decision')} {f.get('guard')}: {f.get('why') or ''}", status, f.get("at"),
                props={"verdict": f.get("level"), "line": f.get("line"), "since": f.get("since"),
                       "evidence_at": [f["at"]] if f.get("at") else None}, suppression=suppression,
                baseline=baseline)

    def since(f: dict) -> str | None:   # compared with a base: what the finding's own since says
        if not res.get("base"):
            return None
        return "unchanged" if str(f.get("since") or "").startswith("pre-existing") else "new"

    for f in res.get("violations") or []:
        one(f, baseline=since(f))
    for f in res.get("possible") or []:
        one(f, baseline=since(f))
    for f in res.get("pre_existing") or []:
        one(f, baseline="unchanged")
    b = res.get("baseline") or {}
    for f in res.get("baselined") or []:
        one(f, suppression=f"listed in the decision baseline {b.get('file') or ''} (recorded {b.get('recorded')})")
    for f in res.get("waived") or []:
        w = f.get("waiver") or {}
        one(f, suppression=f"waived: {w.get('reason') or ''}" + (f" (until {w['until']})" if w.get("until") else ""))
    for f in res.get("reviews") or []:
        run.add(f"decide/{f.get('decision')}/{f.get('guard')}",
                f"decision {f.get('decision')} governs {f.get('guard')}",
                f"REVIEW {f.get('decision')} {f.get('guard')}: {f.get('why') or ''}", "weak_inference", f.get("at"),
                props={"verdict": "REVIEW", "since": f.get("since")}, baseline=since(f))
    return run.log(res.get("exit"))


def from_check(res: dict) -> dict:
    """``check --json`` as SARIF: the sites listed as absent, not installed or unknown (guarded and existing sites
    are not findings)."""
    run = _Run("check")
    for s in res.get("sites") or []:
        v = s.get("verdict")
        if v not in CHECK_STATUS:
            continue
        detail = s.get("message") or s.get("why") or ""
        status = CHECK_STATUS[v]
        if v == "absent" and (s.get("source") not in ("stdlib", "project")
                              or str(s.get("container") or "").startswith("sys.path")):
            status = "strong_inference"   # judged against what is installed or built here
        run.add(f"check/{v}/{s.get('kind')}", f"{v} {s.get('kind')}",
                f"{v} {s.get('kind')} {s.get('expr') or ''}" + (f": {detail}" if detail else ""), status,
                s.get("at"), props={"verdict": v, "rank": s.get("rank"), "source": s.get("source"),
                                    "evidence_at": [s["at"]] if s.get("at") else None})
    return run.log(res.get("exit"))


def export(res: dict, command: str) -> dict:
    return {"review": from_review, "check": from_check, "decide check": from_decide_check}[command](res)


def dumps(log: dict) -> str:
    return json.dumps(log, ensure_ascii=False, indent=2)


# -- in -----------------------------------------------------------------------------------------------------

def _text(msg, rule: dict | None) -> str:
    msg = msg if isinstance(msg, dict) else {}
    text = msg.get("text")
    if not text and msg.get("id") and rule:
        text = ((rule.get("messageStrings") or {}).get(msg["id"]) or {}).get("text")
    if isinstance(text, str) and text:
        args = msg.get("arguments") if isinstance(msg.get("arguments"), list) else []

        def fill(m: re.Match) -> str:   # one pass: an argument holding {N} is not filled again
            if m.group(1) is None:
                return m.group(0)[0]
            i = int(m.group(1))
            return str(args[i]) if i < len(args) else m.group(0)

        return _ARG.sub(fill, text) if args or "{{" in text or "}}" in text else text
    rule = rule or {}
    return ((rule.get("shortDescription") or {}).get("text") or (rule.get("fullDescription") or {}).get("text")
            or "")


def _uri_path(uri: str) -> str:
    """A file URI as a local path; a relative URI reference unquoted, posix."""
    if uri.lower().startswith("file:"):
        p = urlparse(uri)
        return url2pathname((("//" + p.netloc) if p.netloc else "") + p.path)
    return unquote(uri.split("?", 1)[0].split("#", 1)[0])


def _repo_path(root: Path, al: dict, run: dict) -> tuple[str | None, bool]:
    """``(repository-relative path, tied by suffix)`` of an artifactLocation; the path is ``None`` when it names
    no repository file."""
    uri = al.get("uri")
    if not uri and isinstance(al.get("index"), int):
        arts = run.get("artifacts") or []
        if 0 <= al["index"] < len(arts):
            uri = ((arts[al["index"]] or {}).get("location") or {}).get("uri")
    if not isinstance(uri, str) or not uri:
        return None, False
    path = _uri_path(uri)
    bases = {k: _uri_path(v["uri"]).replace("\\", "/").rstrip("/") for k, v in
             (run.get("originalUriBaseIds") or {}).items() if isinstance(v, dict) and isinstance(v.get("uri"), str)}
    base = bases.get(al.get("uriBaseId") or "")
    if base and not re.match(r"^[A-Za-z]:|^/|^\\\\", path) and not uri.lower().startswith("file:"):
        path = base + "/" + path
    posix = path.replace("\\", "/")
    for b in bases.values():   # an absolute path under the run's own source root: exact, not a guess
        if b and re.match(r"^[A-Za-z]:|^/", b) and posix.startswith(b + "/"):
            rel = PurePosixPath(posix[len(b) + 1:]).as_posix()
            if ".." not in rel.split("/") and (root / rel).is_file():
                return rel, False
    key = ci._key(root, path)
    parts = [p for p in key.replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts:
        return None, False
    absolute = bool(re.match(r"^[A-Za-z]:|^/", key))
    if not absolute:
        return (key, False) if (root / key).is_file() else (None, False)
    # an absolute path from another machine (a CI runner's): the longest suffix that is a repository file; not
    # for a file that exists here (it is that file, outside the repository) nor one under a third-party folder
    try:
        here = Path(path).exists()
    except (OSError, ValueError):
        here = False
    if here or _FOREIGN.intersection(parts):
        return None, False
    for i in range(1, len(parts)):
        cand = "/".join(parts[i:])
        if (root / cand).is_file():
            return cand, True
    return None, False


def parse(data: bytes) -> list[dict]:
    """The runs of one SARIF file (a ValueError when it is not SARIF 2.1)."""
    doc = json.loads(data.decode("utf-8-sig"))
    if not isinstance(doc, dict) or not str(doc.get("version", "")).startswith("2.1") or \
            not isinstance(doc.get("runs"), list):
        raise ValueError("not a SARIF 2.1 log (a JSON object with version 2.1.0 and runs)")
    return [r for r in doc["runs"] if isinstance(r, dict)]


def report(root: Path, files: list[str], paths: list[str] | None = None, *, limit: int = 40) -> dict:
    """The results of the SARIF ``files`` as claims (inside ``paths`` when given), errors first."""
    root = Path(root).resolve()
    wanted = [ci._norm_path(p) for p in paths or []]
    wanted = [] if "" in wanted else wanted
    logs: list[dict] = []
    claims: list[dict] = []
    elsewhere: list[str] = []
    counts = {"results": 0, "suppressed": 0, "passed": 0, "absent": 0, "no_location": 0, "not_in_repository": 0,
              "outside_paths": 0, "unreadable": 0}
    facts: dict[str, dict | None] = {}
    for f in files:
        p = Path(f) if Path(f).is_absolute() else root / f
        name = ci._display(root, p)
        try:
            if p.stat().st_size > MAX_SARIF_BYTES:
                logs.append({"file": name, "error": f"larger than {MAX_SARIF_BYTES} bytes"})
                continue
            runs = parse(p.read_bytes())
            mtime = p.stat().st_mtime
        # the file is another tool's output: whatever shape it has, it is an error entry, not a crash
        except (OSError, ValueError, TypeError, AttributeError, RecursionError) as exc:
            logs.append({"file": name, "error": str(exc)[:200] or type(exc).__name__})
            continue
        tools = []
        for run in runs:
            try:
                n = _read_run(root, run, name, mtime, wanted, claims, elsewhere, counts, facts)
            except (TypeError, AttributeError, ValueError, KeyError) as exc:
                n = {"tool": "unreadable run", "results": 0, "error": f"a run could not be read: {str(exc)[:160]}"}
            tools.append(n)
        logs.append({"file": name, "runs": tools})
    why = counts.pop("_why", None)
    claims.sort(key=lambda c: (LEVEL_ORDER.get(c["level"], 9), c["at"]))
    by_status: dict[str, int] = {}
    for c in claims:
        by_status[c["status"]] = by_status.get(c["status"], 0) + 1
    res = {
        "sarif": logs,
        "coverage": {"method": "SARIF 2.1 results read at their first physical location, the file tied to the "
                               "repository by path (absolute, under a uriBaseId, or the longest existing suffix), "
                               "the line to the innermost definition around it", "limits": list(LIMITS)},
        "summary": {**counts, "claims": len(claims), "by_status": by_status},
        "claims": claims[:limit],
        "not_in_repository": sorted(set(elsewhere))[:20],
    }
    if why:
        res["unreadable_example"] = why
    if len(claims) > limit:
        res["truncated"] = True
        res["claims_not_shown"] = len(claims) - limit
    return res


def _rules_of(run: dict) -> tuple[list[dict], list[list[dict]]]:
    """The driver's rules and each extension's rules (CodeQL keeps its queries in ``tool.extensions``)."""
    tool = run.get("tool") or {}

    def rules(component) -> list[dict]:
        rs = component.get("rules") if isinstance(component, dict) else None
        return [r for r in rs if isinstance(r, dict)] if isinstance(rs, list) else []

    exts = tool.get("extensions") if isinstance(tool.get("extensions"), list) else []
    return rules(tool.get("driver")), [rules(e) for e in exts]


def _index(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _rule_of(r: dict, driver: list[dict], extensions: list[list[dict]]) -> dict | None:
    """The rule a result names: ``rule.toolComponent.index`` picks the extension, ``rule.index`` / ``ruleIndex``
    the entry, else the id is looked up (that component first, then the driver and every extension)."""
    ref = r.get("rule") if isinstance(r.get("rule"), dict) else {}
    comp = ref.get("toolComponent") if isinstance(ref.get("toolComponent"), dict) else {}
    cidx = _index(comp.get("index"))
    rules = extensions[cidx] if cidx is not None and cidx < len(extensions) else driver
    idx = _index(ref.get("index", r.get("ruleIndex")))
    if idx is not None and idx < len(rules):
        return rules[idx]
    rid = r.get("ruleId") or ref.get("id")
    if isinstance(rid, str):
        for rs in (rules, driver, *extensions):
            hit = next((x for x in rs if x.get("id") == rid), None)
            if hit is not None:
                return hit
    return None


def _suppressed(r: dict) -> bool:
    """A suppression counts unless its status is ``rejected`` or ``underReview`` (SARIF 2.1.0, 3.35.3)."""
    sup = r.get("suppressions")
    return isinstance(sup, list) and any(isinstance(x, dict) and x.get("status") in (None, "accepted")
                                         for x in sup)


def _level(value) -> str | None:
    return value if isinstance(value, str) and value in SARIF_LEVELS else None


def _read_run(root: Path, run: dict, name: str, mtime: float, wanted: list[str], claims: list[dict],
              elsewhere: list[str], counts: dict, facts: dict) -> dict:
    driver = ((run.get("tool") or {}).get("driver")) or {}
    tool = str(driver.get("name") or "unnamed tool")
    rules = _rules_of(run)
    places: dict = {}   # (uri, uriBaseId, index) -> (path, tied by suffix, stale): one lookup per file
    results = run.get("results")
    n = 0
    for r in results if isinstance(results, list) else []:
        if not isinstance(r, dict):
            continue
        counts["results"] += 1
        n += 1
        try:   # a malformed result is counted; the rest of the run is still read
            row = _read_result(root, r, run, tool, name, mtime, wanted, elsewhere, counts, facts, places, rules)
        except (TypeError, AttributeError, ValueError, KeyError) as exc:
            counts["unreadable"] += 1
            counts.setdefault("_why", str(exc)[:160] or type(exc).__name__)
            continue
        if row is not None:
            claims.append(row)
    return {"tool": tool, **({"version": str(driver["version"])} if driver.get("version") else {}), "results": n}


def _read_result(root: Path, r: dict, run: dict, tool: str, name: str, mtime: float, wanted: list[str],
                 elsewhere: list[str], counts: dict, facts: dict, places: dict,
                 rules: tuple[list[dict], list[list[dict]]]) -> dict | None:
    if _suppressed(r):
        counts["suppressed"] += 1
        return None
    if r.get("kind") in ("pass", "notApplicable"):
        counts["passed"] += 1
        return None
    if r.get("baselineState") == "absent":
        counts["absent"] += 1
        return None
    rule = _rule_of(r, *rules)
    ref = r.get("rule") if isinstance(r.get("rule"), dict) else {}
    rule_id = str(r.get("ruleId") or ref.get("id") or (rule or {}).get("id") or "result")
    default = ((rule or {}).get("defaultConfiguration") or {}).get("level")
    level = _level(r.get("level")) or _level(default) or "warning"
    if r.get("kind") == "informational":
        level = "note"
    phys = next((loc.get("physicalLocation") for loc in r.get("locations") or []
                 if isinstance(loc, dict) and isinstance(loc.get("physicalLocation"), dict)), None)
    al = (phys or {}).get("artifactLocation") or {}
    if not al:
        counts["no_location"] += 1
        return None
    key = (str(al.get("uri")), str(al.get("uriBaseId")), str(al.get("index")))
    if key not in places:
        rel, by_suffix = _repo_path(root, al, run)
        stale = True
        if rel is not None:
            try:
                stale = (root / rel).stat().st_mtime > mtime
            except OSError:
                stale = True
        places[key] = (rel, by_suffix, stale)
    rel, by_suffix, stale = places[key]
    if rel is None:
        counts["not_in_repository"] += 1
        elsewhere.append(str(al.get("uri") or al.get("index")))
        return None
    if wanted and not any(rel == w or rel.startswith(w + "/") for w in wanted):
        counts["outside_paths"] += 1
        return None
    line = ((phys or {}).get("region") or {}).get("startLine")
    line = line if isinstance(line, int) and not isinstance(line, bool) and line >= 1 else None
    at = f"{rel}:{line}" if line else rel
    status = "weak_inference" if stale or by_suffix else "strong_inference"
    msg = _clip(_text(r.get("message"), rule))
    text = f"{tool} reports {rule_id} ({level})" + (f": {msg}" if msg else "")
    if stale:
        text += " (the file changed after the SARIF file was written: lines may have moved)"
    if by_suffix:
        text += f" (named {_clip(str(al.get('uri')))}, tied to {rel} by its path suffix: a guess)"
    row = {"subject": f"{tool}/{rule_id}", "at": at, "claim": text, "status": status, "stated_by": tool,
           "rule": rule_id, "level": level, "evidence_at": [at, name],
           "derived_by": "verinoda.sarif (a SARIF file read: the tool's statement, not checked here)"}
    if line:
        if rel not in facts:
            facts[rel] = ci.facts_of(root, rel)
        sym = ci.symbol_of(facts[rel], line)
        if sym:
            row["symbol"] = sym
    return row


def render_text(res: dict) -> str:
    s = res["summary"]
    out = [f"SARIF: {s['claims']} result(s) in the repository from "
           + ", ".join(f"{x['file']} (" + (", ".join(f"{t['tool']}: {t['results']}" for t in x["runs"])
                                         if "runs" in x else "error: " + str(x.get("error"))) + ")"
                       for x in res["sarif"])]
    skipped = [f"{s[k]} {k.replace('_', ' ')}" for k in ("suppressed", "passed", "absent", "no_location",
                                                          "not_in_repository", "outside_paths", "unreadable")
               if s.get(k)]
    if skipped:
        out.append("  not listed: " + ", ".join(skipped))
    out.append("  each result is the named tool's statement, not checked by Verinoda")
    out.append("")
    for c in res["claims"][:30]:
        out.append(f"  [{c['status']}] {c['claim']}")
        out.append(f"      at {c['at']}" + (f" ({c['symbol']})" if c.get("symbol") else ""))
    if res.get("truncated"):
        out.append(f"  ... {res['claims_not_shown']} more (--limit, --json)")
    return "\n".join(out)
