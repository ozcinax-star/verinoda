"""Who should review a change, and which earlier commits changed the same code: ``review``'s ``reviewers`` key.

Three readings of git, kept apart:

- **Suggested reviewers.** ``git blame -w`` of the base version's lines the change modifies or removes (the
  code as it was before the change): each author with the number of those lines last changed by their commits.
  Who wrote the code being changed is a likely reviewer, not a verified one (a reformat or a moved block credits
  whoever made it), so each is a ``strong_inference`` claim, its evidence the blame of each range. A change that
  only inserts lines modifies no base line and names no one; a removed definition's base lines are all removed.
  Authors are keyed as git's mailmap gives them, and rows with the same displayed name are one person. The
  change's own author (``git config user.name``/``user.email``, through the mailmap) is left out and said so.
- **Declared owners.** The CODEOWNERS rule that decides each changed file (:mod:`verinoda.ownership`, GitHub's
  rules), ``statically_verified`` at the rule's line, which is the evidence.
- **Related changes.** For each modified or removed definition, the commits that changed its base lines
  (``git log -L`` from the base, :func:`verinoda.history.symbol_commits`), each message quoted; merged by commit,
  newest first by commit time. Git history is a primary source for "this commit changed these lines"; that the
  commit is related to the change under review is only that it touched the same code.

At most :data:`MAX_RANGES` ranges are blamed, taken one per file in turn so every file counts; when the cap cuts,
the claims say how many modified base lines were blamed of how many. Git is only read. A planned change (no diff)
and a tree that is not a git work tree get none of this, with the reason.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from verinoda import evidence as evmod

MAX_RANGES = 20        # base line ranges blamed for one review
MAX_SUGGESTED = 5
MAX_DEFS_HISTORY = 8   # modified definitions whose history is read
MAX_RELATED = 10
PER_DEF_COMMITS = 3


def _ranges(lines: set[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for n in sorted(lines):
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


def _identity(repo: Path) -> tuple[str | None, str | None]:
    """(lower-cased e-mail, name) of the work tree's author as git's mailmap maps them."""
    from verinoda.snapshot import git

    mail = (git(repo, "config", "user.email") or "").strip()
    name = (git(repo, "config", "user.name") or "").strip()
    if not mail:
        return None, name or None
    mapped = (git(repo, "check-mailmap", f"{name or mail} <{mail}>") or "").strip()
    if mapped.endswith(">") and "<" in mapped:
        name, mail = mapped[:mapped.rindex("<")].strip(), mapped[mapped.rindex("<") + 1:-1]
    return mail.lower(), name or None


def _when(date: str) -> float:
    try:
        return datetime.fromisoformat(date).timestamp()
    except ValueError:
        return 0.0


def _modified_lines(changes: list) -> dict[str, set[int]]:
    """Base lines each file's changes modify or remove: the changed old lines; a removed definition's whole span.
    A change that only inserts lines has none."""
    by_file: dict[str, set[int]] = {}
    for c in changes:
        lines = set(c.old_changed or ())
        if not lines and c.kind == "removed" and c.old_lines:
            lines = set(range(c.old_lines[0], c.old_lines[1] + 1))
        if lines:
            by_file.setdefault(c.file, set()).update(lines)
    return by_file


def _take_ranges(by_file: dict[str, set[int]]) -> tuple[list[tuple[str, tuple[int, int]]], int]:
    """Up to :data:`MAX_RANGES` (file, range) pairs, one per file in turn; and how many ranges there are."""
    per = {rel: _ranges(lines) for rel, lines in sorted(by_file.items())}
    total = sum(len(v) for v in per.values())
    out: list[tuple[str, tuple[int, int]]] = []
    k = 0
    while len(out) < MAX_RANGES and any(k < len(v) for v in per.values()):
        for rel, rs in per.items():
            if k < len(rs) and len(out) < MAX_RANGES:
                out.append((rel, rs[k]))
        k += 1
    return out, total


def suggest(repo: Path, changes: list, base: dict | None) -> dict:
    """The ``reviewers`` block for ``changes`` (``review.Change``) against ``base`` (``{"ref", "commit"}``)."""
    from verinoda import history, ownership
    from verinoda.snapshot import git
    from verinoda.treestate import project_prefix

    repo = Path(repo)
    out: dict = {"suggested": [], "declared": [], "related_commits": [], "notes": [], "method": (
        "suggested: git blame -w of the base lines the change modifies or removes, authors by lines (mailmap); "
        "declared: the CODEOWNERS rule of each changed file; related: git log -L from the base over each modified "
        "definition's base lines"),
        "limits": ["blame credits the last commit that changed a line: a reformat or a moved block credits whoever "
                   "made it", "inserted lines have no base lines, so they name no reviewer"]}
    if base is None or not base.get("commit"):
        out["not_checked"] = ["a planned change has no base lines to blame"]
        return out
    if not history._is_git(repo):
        out["not_checked"] = ["not a git work tree with a commit"]
        return out
    sha = base["commit"]
    shallow = history._shallow(repo)
    if shallow:
        out["notes"].append("a shallow clone: older authors and commits are missing")
    me, me_name = _identity(repo)
    by_file = _modified_lines(changes)
    all_lines = sum(len(v) for v in by_file.values())
    picked, n_ranges = _take_ranges(by_file)
    tally: dict[str, dict] = {}
    blamed = 0
    for rel, rng in picked:
        got = ownership._blame(repo, rel, rng, rev=sha)
        if got is None:
            out["notes"].append(f"{rel}:{rng[0]}-{rng[1]} could not be blamed at {sha[:10]}")
            continue
        for commit, n in got["lines"].items():
            info = got["info"].get(commit) or {}
            if info.get("boundary") and shallow:
                continue   # a shallow clone's boundary commit is not who wrote the line
            blamed += n
            key, name = ownership._author_key(info)
            row = tally.setdefault(name.lower(), {"author": name, "emails": [], "lines": 0, "per": {}})
            if "@" in key and key not in row["emails"]:
                row["emails"].append(key)
            row["lines"] += n
            row["per"][(rel, rng)] = row["per"].get((rel, rng), 0) + n
    cut = n_ranges > len(picked)
    if cut:
        out["truncated"] = True
        out["notes"].append(f"{len(picked)} of {n_ranges} modified base line range(s) blamed ({blamed} of "
                            f"{all_lines} line(s)), one per file in turn")
    excluded = None
    for k, row in list(tally.items()):
        if (me and me in row["emails"]) or (me_name and k == me_name.lower() and not row["emails"]):
            excluded = tally.pop(k)
    ranked = sorted(tally.values(), key=lambda r: (-r["lines"], r["author"]))
    scope = f"the {blamed} blamed base line(s) of the {all_lines} this change modifies or removes" if cut else \
        f"the {blamed} base line(s) this change modifies or removes"
    for r in ranked[:MAX_SUGGESTED]:
        per = sorted(r["per"].items(), key=lambda kv: (-kv[1], kv[0]))
        where = ", ".join(f"{rel}:{a}-{b}" if a != b else f"{rel}:{a}" for (rel, (a, b)), _ in per[:4])
        text = (f"{r['author']} last changed {r['lines']} of {scope} (git blame at {sha[:10]}: {where}"
                f"{' ...' if len(per) > 4 else ''}); a likely reviewer")
        evs = []
        for (rel, rng), n in per[:3]:
            size = rng[1] - rng[0] + 1
            ev = {"source_type": "git_history", "locator": f"git blame -w -L{rng[0]},{rng[1]} {sha} -- {rel}",
                  "commit_sha": sha, "meta": {"file": rel, "lines": list(rng), "author": r["author"],
                                              "author_lines": n, "summary_of": "the blame output"}}
            ev["excerpt"] = f"{r['author']}: {n} of the {size} line(s) {rel}:{rng[0]}-{rng[1]}"
            ev["content_hash"] = evmod.content_hash(ev["excerpt"])
            evs.append(ev)
        out["suggested"].append({"author": r["author"], "emails": r["emails"], "lines": r["lines"], "of": blamed,
                                 "claim": {"kind": "ownership", "status": "strong_inference", "text": text,
                                           "evidence": evs, "uncertainties": [
                                               "blame credits whoever last touched a line, which may be a reformat"]}})
    if len(ranked) > MAX_SUGGESTED:
        out["suggested_total"] = len(ranked)
    if excluded is not None:
        out["excluded"] = {"author": excluded["author"], "lines": excluded["lines"],
                           "why": "the change's own author (git config user.name / user.email, through the mailmap)"}
    # declared owners (CODEOWNERS at the top of the work tree; paths matched as repository paths)
    top = (git(repo, "rev-parse", "--show-toplevel") or "").strip()
    co = ownership.read_codeowners(Path(top)) if top else None
    if co is not None:
        prefix = project_prefix(repo) or ""
        seen: dict[int, dict] = {}
        for rel in sorted({c.file for c in changes}):
            rule = ownership.owner_rule(co, prefix + rel)
            if rule is None or not rule["owners"]:
                continue
            row = seen.setdefault(rule["line"], {"owners": rule["owners"], "files": [], "rule": rule["text"],
                                                 "at": f"{co['file']}:{rule['line']}"})
            row["files"].append(rel)
        for row in seen.values():
            row["status"] = "strong_inference" if co.get("sections") else "statically_verified"
            row["evidence"] = [{"source_type": "source_code", "locator": f"{co['file']}:{row['at'].rsplit(':', 1)[1]}",
                                "excerpt": row["rule"], "content_hash": evmod.content_hash(row["rule"]),
                                "meta": {"file": co["file"], "work_tree_top": True}}]
        out["declared"] = list(seen.values())
    # related changes: the history of each modified definition's base lines
    commits: dict[str, dict] = {}
    defs = [c for c in changes if c.qual and c.old_lines and c.kind != "added"]
    if len(defs) > MAX_DEFS_HISTORY:
        out["notes"].append(f"the history of {MAX_DEFS_HISTORY} of {len(defs)} changed definitions read")
    for c in defs[:MAX_DEFS_HISTORY]:
        try:
            got = history.symbol_commits(repo, c.file, c.old_lines[0], c.old_lines[1], limit=PER_DEF_COMMITS,
                                         rev=sha, probe=False)
        except ValueError:
            continue
        for k in got.get("commits") or []:
            row = commits.setdefault(k["commit"], {**{x: k[x] for x in ("commit", "date", "author", "subject",
                                                                          "body")}, "touched": [], "evidence": []})
            row["touched"].append(f"{c.file}::{c.qual}")
            ev = {**k["evidence"], "locator": f"commit {k['commit']} {c.file}"}
            if ev["locator"] not in {e["locator"] for e in row["evidence"]}:
                row["evidence"].append(ev)
    rel_rows = sorted(commits.values(), key=lambda r: _when(r["date"]), reverse=True)
    for r in rel_rows[:MAX_RELATED]:
        named = r["touched"][:3]
        files = {t.split("::", 1)[0] for t in named}
        r["claim"] = {"kind": "history", "status": "primary_source_verified",
                      "text": f"{r['commit'][:10]} ({r['date'][:10]}, {r['author']}) changed "
                              f"{', '.join(named)} before: {r['subject']}",
                      "evidence": [e for e in r.pop("evidence") if e["locator"].split(" ", 2)[2] in files]}
        out["related_commits"].append(r)
    if len(rel_rows) > MAX_RELATED:
        out["related_total"] = len(rel_rows)
    if not out["notes"]:
        del out["notes"]
    return out


def compact(block: dict) -> dict:
    """The block without claim texts and evidence (for the MCP response, which is capped): who, how many lines,
    which rule, which commits, and why a list is empty or partial; ``review --json`` has the claims."""
    out = {k: block[k] for k in ("not_checked", "notes", "excluded", "truncated", "suggested_total",
                                 "related_total") if k in block}
    out["suggested"] = [{"author": s["author"], "lines": s["lines"], "of": s["of"], "status": s["claim"]["status"]}
                        for s in block.get("suggested") or []]
    out["declared"] = [{"owners": d["owners"], "at": d["at"], "files": d["files"][:5], "status": d["status"]}
                       for d in block.get("declared") or []]
    out["related_commits"] = [{"commit": r["commit"][:12], "date": r["date"][:10], "author": r["author"],
                               "subject": r["subject"][:120], "touched": r["touched"][:3],
                               "status": r["claim"]["status"]} for r in block.get("related_commits") or []]
    out["note"] = "blame-based suggestions are strong_inference; `verinoda review --json` has the claims and evidence"
    return out


def render(block: dict) -> list[str]:
    """Text lines for ``verinoda review``."""
    if not block:
        return []
    out = ["", "Reviewers and related changes (git blame and history of the code the change touches):"]
    for why in block.get("not_checked") or []:
        out.append(f"  not checked: {why}")
    for s in block.get("suggested") or []:
        out.append(f"  [{s['claim']['status']}] {s['author']}: {s['lines']} of {s['of']} blamed base line(s)")
    if block.get("excluded"):
        e = block["excluded"]
        out.append(f"  (left out: {e['author']}, {e['lines']} line(s): {e['why']})")
    for d in block.get("declared") or []:
        out.append(f"  [{d['status']}] declared owners {' '.join(d['owners'])} ({d['at']}): "
                   f"{', '.join(d['files'][:4])}")
    for r in block.get("related_commits") or []:
        out.append(f"  [{r['claim']['status']}] {r['commit'][:10]} {r['date'][:10]} {r['author']}: {r['subject']}"
                   f"  ({', '.join(r['touched'][:2])})")
    for n in block.get("notes") or []:
        out.append(f"  note: {n}")
    if len(out) == 2:
        out.append("  none: the change modifies no committed line")
    return out
