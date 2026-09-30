"""Git hooks that keep the index current: ``verinoda hooks install | uninstall | status`` (and ``setup --hooks``).

After a commit, a branch switch or a merge, the files under the index changed without anyone running
``verinoda update``. Three hooks run it: ``post-commit``, ``post-checkout`` (a branch checkout only, not a file
checkout) and ``post-merge``. Each starts ``<this interpreter> -m verinoda update <project>`` in the background,
detached, output discarded, so git is never held up; the update's own build lock makes a second one wait or step
aside, and it is incremental (only changed files are re-read). ``VERINODA_NO_HOOK=1`` in the environment skips
it for one git command.

Nothing that is there is overwritten. A hook file is a shell script; Verinoda adds one marked block right after
its first line (the ``#!`` line), so an ``exit`` further down cannot skip it, keyed by the project so two
projects of one repository each have theirs. ``uninstall`` removes exactly that block, and the file too when
Verinoda created it and nothing else is left. A hook that is not a shell script (a binary, another interpreter)
is left alone and reported. When ``core.hooksPath`` points outside the repository's git folder (a hook manager
such as husky or pre-commit owns the hooks), nothing is written: the report gives the line to add there.

No merge driver: the graph lives in ``.verinoda/`` and git ignores it, so it is never merged; a merge only
changes source files, which ``post-merge`` re-reads.
"""
from __future__ import annotations

import shlex
import stat
import sys
from pathlib import Path

from verinoda.snapshot import git

HOOKS = ("post-commit", "post-checkout", "post-merge")
BEGIN = "# >>> verinoda update >>>"
END = "# <<< verinoda update <<<"


class HookError(ValueError):
    pass


def _q(s: str) -> str:
    """A POSIX-shell word (git runs hooks with sh, on Windows too); forward slashes for a Windows path."""
    return shlex.quote(s.replace("\\", "/"))


def hooks_dir(repo: Path) -> tuple[Path, str | None]:
    """(the folder git runs hooks from, why Verinoda must not write there or None)."""
    top = (git(repo, "rev-parse", "--show-toplevel") or "").strip()
    if not top:
        raise HookError(f"{repo} is not in a git work tree")
    got = (git(repo, "rev-parse", "--git-path", "hooks") or "").strip()   # relative to the folder git ran in
    d = (Path(got) if Path(got).is_absolute() else Path(repo) / got).resolve()
    common = (git(repo, "rev-parse", "--git-common-dir") or "").strip()
    common_dir = (Path(common) if Path(common).is_absolute() else Path(repo) / common).resolve()
    custom = (git(repo, "config", "--get", "core.hooksPath") or "").strip()
    if custom:
        try:
            d.relative_to(common_dir)
        except ValueError:
            return d, (f"core.hooksPath is {custom}: another tool manages the hooks there; add the lines "
                       "`verinoda hooks install --print` shows to its post-commit, post-checkout and post-merge "
                       "hooks")
    return d, None


def block(project: Path, hook: str, command: list[str] | None = None) -> str:
    """The marked block for ``hook`` of ``project``."""
    cmd = command or [sys.executable, "-m", "verinoda", "update", str(project)]
    run = " ".join(_q(c) for c in cmd)
    key = str(Path(project).resolve()).replace("\\", "/")
    body = f'( {run} >/dev/null 2>&1 & )'
    if hook == "post-checkout":   # $3 = 1: a branch checkout; 0: files checked out, nothing to re-index
        body = f'if [ "$3" = "1" ]; then {body}; fi'
    return (f"{BEGIN} {key}\n"
            "# added by `verinoda hooks install`; `verinoda hooks uninstall` removes this block\n"
            f'if [ -z "$VERINODA_NO_HOOK" ]; then {body}; fi\n'
            f"{END} {key}\n")


def _is_shell(text: str) -> bool:
    first = text.split("\n", 1)[0].strip()
    return first.startswith("#!") and any(sh in first for sh in ("/sh", "/bash", "/dash", "/zsh", "env sh",
                                                                    "env bash"))


def _strip(text: str, key: str) -> tuple[str, bool]:
    """``text`` without this project's block (and whether one was there)."""
    lines = text.split("\n")
    out, inside, found = [], False, False
    for ln in lines:
        if ln.strip() == f"{BEGIN} {key}":
            inside, found = True, True
            continue
        if inside:
            if ln.strip() == f"{END} {key}":
                inside = False
            continue
        out.append(ln)
    return "\n".join(out), found


def _only_ours(text: str) -> bool:
    """A file Verinoda created that holds nothing else: its ``#!`` line and the created-by note."""
    rest = [ln for ln in text.split("\n")[1:] if ln.strip() and ln.strip() != CREATED]
    return not rest


CREATED = "# this hook file was created by verinoda; it is removed when no block is left"


def install(repo: Path, *, command: list[str] | None = None, dry_run: bool = False) -> dict:
    """Add this project's block to each hook; returns what was done per hook."""
    repo = Path(repo).resolve()
    d, refuse = hooks_dir(repo)
    key = str(repo).replace("\\", "/")
    res: dict = {"hooks_dir": str(d), "project": key, "hooks": {}}
    if refuse:
        res["refused"] = refuse
        res["lines"] = {h: block(repo, h, command) for h in HOOKS}
        return res
    for h in HOOKS:
        p = d / h
        new_block = block(repo, h, command)
        if p.exists():
            try:
                text = p.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                res["hooks"][h] = "left alone: not a text file"
                continue
            if not _is_shell(text):
                res["hooks"][h] = "left alone: not a shell script (add the block yourself; `--print` shows it)"
                continue
            base, had = _strip(text, key)
            first, _, rest = base.partition("\n")
            new = first + "\n" + new_block + rest
            if new == text:
                res["hooks"][h] = "already installed"
                continue
            action = "updated" if had else "added to the existing hook"
        else:
            new = "#!/bin/sh\n" + new_block + CREATED + "\n"   # blocks go right after the #! line
            action = "created"
        if not dry_run:
            d.mkdir(parents=True, exist_ok=True)
            p.write_bytes(new.encode("utf-8"))
            mode = p.stat().st_mode
            p.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        res["hooks"][h] = action
    res["note"] = ("each hook starts `verinoda update` in the background after the git command; "
                   "VERINODA_NO_HOOK=1 skips it once")
    return res


def uninstall(repo: Path, *, dry_run: bool = False) -> dict:
    """Remove this project's block from each hook (and a file Verinoda created that is then empty)."""
    repo = Path(repo).resolve()
    d, refuse = hooks_dir(repo)
    key = str(repo).replace("\\", "/")
    res: dict = {"hooks_dir": str(d), "project": key, "hooks": {}}
    for h in HOOKS:
        p = d / h
        if not p.exists():
            res["hooks"][h] = "none"
            continue
        try:
            text = p.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            res["hooks"][h] = "left alone: not a text file"
            continue
        new, had = _strip(text, key)
        if not had:
            res["hooks"][h] = "no verinoda block"
            continue
        if CREATED in new and _only_ours(new):
            res["hooks"][h] = "removed (the file was verinoda's)"
            if not dry_run:
                p.unlink()
            continue
        res["hooks"][h] = "block removed"
        if not dry_run:
            p.write_bytes(new.encode("utf-8"))
    if refuse:
        res["note"] = refuse
    return res


def status(repo: Path) -> dict:
    repo = Path(repo).resolve()
    d, refuse = hooks_dir(repo)
    key = str(repo).replace("\\", "/")
    out: dict = {"hooks_dir": str(d), "project": key, "hooks": {}}
    for h in HOOKS:
        p = d / h
        try:
            text = p.read_bytes().decode("utf-8", "replace") if p.exists() else ""
        except OSError:
            text = ""
        out["hooks"][h] = "installed" if f"{BEGIN} {key}" in text else ("other hook, no verinoda block" if text
                                                                          else "none")
    if refuse:
        out["note"] = refuse
    return out


def render(res: dict) -> str:
    out = [f"hooks in {res['hooks_dir']} for {res['project']}:"]
    if res.get("refused"):
        out.append(f"  not written: {res['refused']}")
        for h, b in (res.get("lines") or {}).items():
            out.append(f"  --- {h} ---")
            out += ["  " + ln for ln in b.rstrip("\n").split("\n")]
        return "\n".join(out)
    for h, what in res["hooks"].items():
        out.append(f"  {h}: {what}")
    if res.get("note"):
        out.append(f"  note: {res['note']}")
    return "\n".join(out)

