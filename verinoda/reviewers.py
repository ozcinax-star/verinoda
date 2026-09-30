"""Who should review a change, and which earlier commits changed the same code: ``review``'s ``reviewers`` key.

Three readings of git, kept apart:

- **Suggested reviewers.** ``git blame -w`` of the base version's lines the change modifies or removes (the
  code as it was before the change): each author with the number of those lines last changed by their commits.
  Who wrote the code being changed is a likely reviewer, not a verified one (a reformat or a moved block credits
  whoever made it), so each is a ``strong_inference`` claim, its evidence the blame. The change's own author
  (``git config user.email``, or the head commit's author for a committed range) is left out and said so.
  Added code has no base lines and names no one.
- **Declared owners.** The CODEOWNERS rule that decides each changed file (:mod:`verinoda.ownership`, GitHub's
  rules), ``statically_verified`` at the rule's line.
- **Related changes.** For each modified or removed definition, the commits that changed its base lines
  (``git log -L`` from the base, :func:`verinoda.history.symbol_commits`), each message quoted; merged by commit,
  newest first. Git history is a primary source for "this commit changed these lines"; that the commit is
  related to the change under review is only that it touched the same code.

Git is only read. A planned change (no diff) and a tree that is not a git work tree get none of this, with the
reason.
"""
from __future__ import annotations

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


def _identity(repo: Path, head_rev: str | None) -> tuple[str | None, str]:
    """(lower-cased e-mail of the change's author, where it was read)."""
    from verinoda.snapshot import git

    if head_rev:
        mail = (git(repo, "log", "-1", "--format=%aE", head_rev) or "").strip().lower()
        return (mail or None), f"the author of {head_rev[:10]}"
    mail = (git(repo, "config", "user.email") or "").strip().lower()
    return (mail or None), "git config user.email"


def _blame_ev(base: str, rel: str, rng: tuple[int, int], excerpt: str) -> dict:
    loc = f"git blame -w -L{rng[0]},{rng[1]} {base} -- {rel}"
    return {"source_type": "git_history", "locator": loc, "commit_sha": base,
            "content_hash": evmod.content_hash(excerpt), "excerpt": excerpt[:300],
            "meta": {"file": rel, "lines": list(rng)}}


def suggest(repo: Path, changes: list, base: dict | None, *, head_rev: str | None = None) -> dict:
    """The ``reviewers`` block for ``changes`` (``review.Change``) against ``base`` (``{"ref", "commit"}``)."""
    from verinoda import history, ownership
    from verinoda.treestate import project_prefix

    repo = Path(repo)
    out: dict = {"suggested": [], "declared": [], "related_commits": [], "method": (
        "suggested: git blame -w of the base lines the change modifies or removes, authors by lines; declared: "
        "the CODEOWNERS rule of each changed file; related: git log -L from the base over each modified "
        "definition's base lines"),
        "limits": ["blame credits the last commit that changed a line: a reformat or a moved block credits whoever "
                   "made it", "added code has no base lines, so it names no reviewer",
                   f"at most {MAX_RANGES} line ranges blamed, {MAX_DEFS_HISTORY} definitions' history read"]}
    if base is None or not base.get("commit"):
        out["not_checked"] = "a planned change has no base lines to blame"
        return out
    if not history._is_git(repo):
        out["not_checked"] = "not a git work tree with a commit"
        return out
    sha = base["commit"]
    me, me_from = _identity(repo, head_rev)
    # base lines the change modifies or removes, per file
    by_file: dict[str, set[int]] = {}
    for c in changes:
        if c.kind in ("modified", "removed", "signature", "body") or c.old_changed:
            lines = set(c.old_changed) or (set(range(c.old_lines[0], c.old_lines[1] + 1)) if c.old_lines else set())
            if lines:
                by_file.setdefault(c.file, set()).update(lines)
    tally: dict[str, dict] = {}
    blamed = 0
    for rel in sorted(by_file):
        for rng in _ranges(by_file[rel]):
            if blamed >= MAX_RANGES:
                out["truncated"] = True
                break
            blamed += 1
            got = ownership._blame(repo, rel, rng, rev=sha)
            if got is None:
                continue
            for commit, n in got["lines"].items():
                info = got["info"].get(commit) or {}
                if info.get("boundary") and history._shallow(repo):
                    continue   # a shallow clone's boundary commit is not who wrote the line
                key, name = ownership._author_key(info)
                row = tally.setdefault(key, {"author": name, "email": key if "@" in key else None, "lines": 0,
                                             "ranges": []})
                row["lines"] += n
                if (rel, rng) not in row["ranges"]:
                    row["ranges"].append((rel, rng))
    total = sum(r["lines"] for r in tally.values())
    excluded = tally.pop(me, None) if me else None
    ranked = sorted(tally.values(), key=lambda r: (-r["lines"], r["author"]))
    for r in ranked[:MAX_SUGGESTED]:
        where = ", ".join(f"{rel}:{a}-{b}" if a != b else f"{rel}:{a}" for rel, (a, b) in r["ranges"][:4])
        text = (f"{r['author']} last changed {r['lines']} of the {total} base line(s) this change modifies or "
                f"removes (git blame at {sha[:10]}: {where}); a likely reviewer")
        out["suggested"].append({
            "author": r["author"], "email": r["email"], "lines": r["lines"], "of": total,
            "claim": {"kind": "ownership", "status": "strong_inference", "text": text,
                      "evidence": [_blame_ev(sha, rel, rng, f"{r['author']}: {r['lines']} line(s)")
                                   for rel, rng in r["ranges"][:3]],
                      "uncertainties": ["blame credits whoever last touched a line, which may be a reformat"]}})
    if len(ranked) > MAX_SUGGESTED:
        out["suggested_total"] = len(ranked)
    if excluded is not None:
        out["excluded"] = {"author": excluded["author"], "lines": excluded["lines"],
                           "why": f"the change's own author ({me_from})"}
    # declared owners (CODEOWNERS at the top of the work tree; paths matched as repository paths)
    from verinoda.snapshot import git

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
        out["declared"] = list(seen.values())
    # related changes: the history of each modified definition's base lines
    commits: dict[str, dict] = {}
    defs = [c for c in changes if c.qual and c.old_lines and c.kind != "added"][:MAX_DEFS_HISTORY]
    for c in defs:
        try:
            got = history.symbol_commits(repo, c.file, c.old_lines[0], c.old_lines[1], limit=PER_DEF_COMMITS,
                                         rev=sha)
        except ValueError:
            continue
        for k in got.get("commits") or []:
            row = commits.setdefault(k["commit"], {**{x: k[x] for x in ("commit", "date", "author", "subject",
                                                                          "body")}, "touched": [],
                                                   "evidence": k["evidence"]})
            row["touched"].append(f"{c.file}::{c.qual}")
    rel_rows = sorted(commits.values(), key=lambda r: r["date"], reverse=True)
    for r in rel_rows[:MAX_RELATED]:
        r["claim"] = {"kind": "history", "status": "primary_source_verified",
                      "text": f"{r['commit'][:10]} ({r['date'][:10]}, {r['author']}) changed "
                              f"{', '.join(r['touched'][:3])} before: {r['subject']}",
                      "evidence": [r.pop("evidence")]}
        out["related_commits"].append(r)
    if len(rel_rows) > MAX_RELATED:
        out["related_total"] = len(rel_rows)
    if history._shallow(repo):
        out["limits"].append("a shallow clone: older authors and commits are missing")
    return out


def compact(block: dict) -> dict:
    """The block without claim texts and evidence (for the MCP response, which is capped): who, how many lines,
    which rule, which commits; ``review --json`` has the claims."""
    out = {k: block[k] for k in ("not_checked", "excluded", "truncated", "suggested_total", "related_total")
           if k in block}
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
    if not block or not (block.get("suggested") or block.get("declared") or block.get("related_commits")
                         or block.get("excluded")):
        return []
    out = ["", "Reviewers and related changes (git blame and history of the code the change touches):"]
    for s in block.get("suggested") or []:
        out.append(f"  [{s['claim']['status']}] {s['author']}: {s['lines']} of {s['of']} base line(s)")
    if block.get("excluded"):
        e = block["excluded"]
        out.append(f"  (left out: {e['author']}, {e['lines']} line(s): {e['why']})")
    for d in block.get("declared") or []:
        out.append(f"  [{d['status']}] declared owners {' '.join(d['owners'])} ({d['at']}): {', '.join(d['files'][:4])}")
    for r in block.get("related_commits") or []:
        out.append(f"  [{r['claim']['status']}] {r['commit'][:10]} {r['date'][:10]} {r['author']}: {r['subject']}"
                   f"  ({', '.join(r['touched'][:2])})")
    return out
