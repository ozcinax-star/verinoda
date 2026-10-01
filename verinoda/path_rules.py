"""Path-scoped review rules (``verinoda rules``).

Teams keep review rules next to the code they are about: Cursor's ``BUGBOT.md`` per folder, ``AGENTS.md`` and
``CLAUDE.md`` at any depth. A rule file covers its own folder and everything under it (``.cursor/BUGBOT.md`` and
``.github/...`` cover the folder holding ``.cursor`` / ``.github``), so a rule under ``src/api/`` applies only to
changes there.

Verinoda reads two kinds of rule from these files:

- **checked rules**, in a fenced block with the info string ``verinoda-rules``, one per line::

      error no-print: regex print\\( -- use the logger
      warning no-eval: ast eval($$$A) -- eval runs any string
      off old-name: regex old_api

  ``MODE ID: KIND PATTERN [-- MESSAGE]`` with MODE ``error``, ``warning`` or ``off``, KIND ``regex`` (git's
  extended regular expressions, run by ``git grep -E``: no backtracking engine in this process) or ``ast`` (a
  ``grep-ast`` structural pattern). A rule matches only on the lines the change added. A nearer file's rule with
  the same id replaces a farther one for the files it covers (``off`` turns it off there).
- **prose rules**: everything else in those files. Verinoda does not judge prose; it lists, per changed file, the
  rule files that cover it, nearest first, for the reviewer to read.

A match on an added line is ``statically_verified`` at its ``file:line`` (the pattern is there; whether the rule's
intent is broken is the reader's call). Exit 1 on an ``error`` match, 3 when a search did not finish (a cut
list, a timeout, a git error, an AST rule on a staged file whose working copy differs), else 0.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

RULE_FILES = ("AGENTS.md", "CLAUDE.md", "BUGBOT.md", "REVIEW.md")
# rule files kept in a tool folder cover the folder that holds it
NESTED = {".cursor": ("BUGBOT.md",), ".github": ("copilot-instructions.md",)}
MODES = ("error", "warning", "off")
FENCE = "verinoda-rules"
MAX_FINDINGS = 200
_LINE = re.compile(r"^(error|warning|off)\s+([A-Za-z0-9][A-Za-z0-9_.-]{0,63})\s*:\s*(regex|ast)\s+(.+?)"
                   r"(?:\s+--\s+(.*))?$")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class RulesError(ValueError):
    pass


def rule_files(files: list[str]) -> list[tuple[str, str]]:
    """``(rule file, folder it covers)`` for every rule file among ``files`` (project-relative POSIX paths)."""
    out = []
    for f in files:
        p = PurePosixPath(f)
        folder = p.parent.as_posix()
        if p.parent.name in NESTED and p.name in NESTED[p.parent.name]:
            up = p.parent.parent.as_posix()
            out.append((f, "" if up == "." else up))
        elif p.name in RULE_FILES:
            out.append((f, "" if folder == "." else folder))
    return sorted(out, key=lambda x: (x[1].count("/") if x[1] else -1, x[0]))


def _covers(folder: str, rel: str) -> bool:
    return folder == "" or rel == folder or rel.startswith(folder + "/")


def parse(text: str, src: str) -> tuple[list[dict], list[dict]]:
    """The checked rules of one rule file and the lines that look like rules but do not parse."""
    rules, bad = [], []
    state, fence = None, ""        # None outside a fenced block, else "rules" or "other"
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        m = re.match(r"^(`{3,}|~{3,})(.*)$", s)
        if m and state is None:
            fence = m.group(1)
            state = "rules" if m.group(2).strip().split(" ")[0] == FENCE else "other"
            continue
        if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2).strip():
            state = None
            continue
        if state != "rules" or not s or s.startswith("#"):
            continue
        r = _LINE.match(s)
        if not r:
            bad.append({"at": f"{src}:{n}", "line": s[:200],
                        "why": "not `MODE ID: regex|ast PATTERN [-- MESSAGE]` (MODE error, warning or off)"})
            continue
        rules.append({"mode": r.group(1), "id": r.group(2), "kind": r.group(3), "pattern": r.group(4).strip(),
                      "message": (r.group(5) or "").strip(), "source": f"{src}:{n}"})
    return rules, bad


def _diff(repo: Path, base: str | None, staged: bool) -> tuple[dict[str, set[int]], str]:
    """Added lines per changed file (``{rel: {line, ...}}``) against ``base`` (default HEAD), and the base's sha."""
    from verinoda.snapshot import git
    from verinoda.treestate import _GIT_SAFE

    rev = base or "HEAD"
    sha = git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    if not sha:
        raise RulesError(f"{rev} is not a commit here")
    sha = sha.strip()
    out = git(repo, *_GIT_SAFE, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames", "--relative",
              "--src-prefix=a/", "--dst-prefix=b/", *(["--cached"] if staged else []), sha, timeout=120)
    if out is None:
        raise RulesError("git diff failed")
    added: dict[str, set[int]] = {}
    cur = None
    for ln in out.splitlines():
        if ln.startswith("+++ "):
            name = ln[4:]
            cur = name[2:] if name.startswith("b/") else None
            if cur is not None:
                added.setdefault(cur, set())
            continue
        m = _HUNK.match(ln)
        if m and cur is not None:
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            added[cur].update(range(start, start + count))
    if not staged:      # untracked files the change adds, every line of them
        for rel in (git(repo, *_GIT_SAFE, "ls-files", "-z", "--others", "--exclude-standard") or "").split("\0"):
            if rel:
                try:
                    n = len((repo / rel).read_bytes().splitlines())
                except OSError:
                    continue
                added[rel] = set(range(1, n + 1))
    return {k: v for k, v in added.items() if v}, sha


def _read_rule_file(repo: Path, rel: str, staged: bool) -> str | None:
    from verinoda.snapshot import git

    if staged:
        return git(repo, "show", f":{rel}")
    try:
        return (repo / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _regex_hits(repo: Path, pattern: str, files: list[str], staged: bool) -> tuple[list[dict], str | None]:
    from verinoda.monitors import _git_grep_rows

    rows, why = [], None
    for i in range(0, len(files), 200):
        chunk = files[i:i + 200]
        rc, got, err = _git_grep_rows(repo, ["-n", "-z", "-I", "--no-color", "--no-column",
                                             *(["--cached"] if staged else ["--untracked"]), "-E", "-e", pattern,
                                             "--", *[f":(literal){f}" for f in chunk]])
        if rc == 1:
            continue
        if rc == -1:
            return rows + got, "more matches than can be read"
        if rc != 0:
            return rows, f"git grep failed: {err.strip()[:200]}"
        rows += got
    return rows, why


def _ast_hits(repo: Path, pattern: str, files: list[str], staged: bool) -> tuple[list[dict], str | None]:
    from verinoda import grep_ast
    from verinoda.snapshot import git

    if staged:
        dirty = (git(repo, "diff", "--name-only", "-z", "--relative", "--", *files) or "").split("\0")
        dirty = [d for d in dirty if d in files]
        if dirty:
            return [], f"{len(dirty)} staged file(s) differ from their working copy (an AST rule reads the working copy)"
    try:
        res = grep_ast.run(repo, pattern, files=files, max_results=2000)
    except grep_ast.PatternError as exc:
        return [], f"the pattern cannot be searched: {exc}"
    ns = res.get("not_searched") or {}
    why = [w for w, bad in (("a cut list", res.get("truncated")), ("a file timed out", ns.get("timed_out")),
                            ("the time budget ran out", ns.get("budget_spent")),
                            ("a file too large", ns.get("too_large")), ("a file unreadable", ns.get("unreadable")))
           if bad]
    rows = []
    for x in res["matches"]:
        rel, _, n = x["at"].rpartition(":")
        if n.isdigit():
            rows.append({"at": x["at"], "text": x["text"][:200], "end": int(x.get("end") or n)})
    return rows, "; ".join(why) or None


def check(repo: Path, *, base: str | None = None, staged: bool = False) -> dict:
    """Apply the rules that cover each changed file to the lines the change added."""
    from verinoda.snapshot import list_files

    repo = Path(repo).resolve()
    added, sha = _diff(repo, base, staged)
    files = list_files(repo)
    sources = rule_files(sorted(set(files) | set(added)))
    by_file: dict[str, list[dict]] = {}
    malformed: list[dict] = []
    for src, folder in sources:
        text = _read_rule_file(repo, src, staged)
        if text is None:
            continue
        rules, bad = parse(text, src)
        malformed += bad
        by_file[src] = [{**r, "folder": folder} for r in rules]
    # per changed file: the rule files covering it, nearest first; the rule in force per id is the nearest one
    order = {src: k for k, (src, _) in enumerate(sources)}
    prose: dict[str, list[str]] = {}
    in_force: dict[tuple, list[str]] = {}       # (id, kind, pattern, mode, message, source) -> files
    for rel in sorted(added):
        covering = sorted((s for s, f in sources if _covers(f, rel)), key=lambda s: -order[s])
        if covering:
            prose[rel] = covering
        seen: set[str] = set()
        for s in covering:
            for r in by_file.get(s, []):
                if r["id"] in seen:
                    continue
                seen.add(r["id"])
                if r["mode"] != "off":
                    key = (r["id"], r["kind"], r["pattern"], r["mode"], r["message"], r["source"])
                    in_force.setdefault(key, []).append(rel)
    findings, incomplete = [], []
    for (rid, kind, pattern, mode, message, source), rels in sorted(in_force.items(), key=lambda x: x[0][5]):
        hits, why = (_regex_hits if kind == "regex" else _ast_hits)(repo, pattern, rels, staged)
        if why:
            incomplete.append({"rule": rid, "source": source, "why": why})
        for h in hits:
            rel, _, n = h["at"].rpartition(":")
            lo = int(n)
            hi = h.get("end", lo)
            if not any(x in added.get(rel, ()) for x in range(lo, hi + 1)):
                continue
            findings.append({"rule": rid, "mode": mode, "at": h["at"], "text": h["text"], "message": message,
                             "source": source, "status": "statically_verified"})
    findings.sort(key=lambda f: (f["mode"] != "error", f["at"]))
    n_err = sum(1 for f in findings if f["mode"] == "error")
    exit_code = 1 if n_err else 3 if incomplete else 0
    return {"base": sha[:12], "mode": "staged" if staged else "worktree", "changed_files": len(added),
            "rule_files": [s for s, _ in sources], "rules_in_force": len(in_force),
            "findings": findings[:MAX_FINDINGS], "findings_total": len(findings), "errors": n_err,
            "incomplete": incomplete, "malformed": malformed,
            "prose": [{"file": rel, "read": cov} for rel, cov in sorted(prose.items())],
            "exit": exit_code,
            "method": "rule files (AGENTS.md, CLAUDE.md, BUGBOT.md, REVIEW.md, .cursor/BUGBOT.md, "
                      ".github/copilot-instructions.md) cover their folder; `verinoda-rules` blocks checked on the "
                      "added lines (git grep -E / grep-ast); prose listed, not judged"}


def summary(res: dict) -> dict:
    """What ``review`` carries: the counts, the first matches, and the rule files to read for the change."""
    return {"errors": res["errors"], "warnings": res["findings_total"] - res["errors"],
            "findings": [{k: f[k] for k in ("mode", "rule", "at", "message")} for f in res["findings"][:10]],
            "incomplete": len(res["incomplete"]), "malformed": len(res["malformed"]),
            "read": sorted({s for p in res["prose"] for s in p["read"]}),
            "next_step": "`verinoda rules` (exit 1 on an error rule) for every match and the files each rule file "
                         "covers"}


def render_lines(s: dict) -> list[str]:
    if not s:
        return []
    out = ["", f"Path rules: {s['errors']} error(s), {s['warnings']} warning(s)"
           + (f", {s['incomplete']} rule(s) not checked to the end" if s["incomplete"] else "")
           + (f", {s['malformed']} line(s) not rules" if s["malformed"] else "")]
    for f in s["findings"]:
        out.append(f"  {f['mode'].upper()} [{f['rule']}] {f['at']}" + (f": {f['message']}" if f["message"] else ""))
    if s["read"]:
        out.append(f"  rule files covering the change (read them): {', '.join(s['read'][:8])}"
                   + (f" and {len(s['read']) - 8} more" if len(s["read"]) > 8 else ""))
    return out


def render(res: dict) -> str:
    out = [f"rules: {res['changed_files']} changed file(s) against {res['base']} ({res['mode']}), "
           f"{len(res['rule_files'])} rule file(s), {res['rules_in_force']} checked rule(s) in force: "
           f"{res['errors']} error(s), {res['findings_total'] - res['errors']} warning(s)"]
    for f in res["findings"][:50]:
        out.append(f"  {f['mode'].upper()} [{f['rule']}] {f['at']}: {f['text']}"
                   + (f"  ({f['message']})" if f["message"] else "") + f"  - {f['source']}")
    if res["findings_total"] > 50:
        out.append(f"  ... {res['findings_total'] - 50} more (--json)")
    for x in res["incomplete"]:
        out.append(f"  unknown [{x['rule']}] ({x['source']}): {x['why']}")
    for b in res["malformed"][:20]:
        out.append(f"  not a rule {b['at']}: {b['line']}")
    if res["prose"]:
        out.append("  rule files to read for the changed files (prose is not checked):")
        for p in res["prose"][:30]:
            out.append(f"    {p['file']}: {', '.join(p['read'])}")
        if len(res["prose"]) > 30:
            out.append(f"    ... {len(res['prose']) - 30} more (--json)")
    return "\n".join(out)
