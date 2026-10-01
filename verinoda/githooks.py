"""Git hooks that keep the index current: ``verinoda hooks install | uninstall | status`` (and ``setup --hooks``).

After a commit, a branch switch, a merge or a rebase, the files under the index changed without anyone running
``verinoda update``. Four hooks run it: ``post-commit`` (not while a rebase is in progress: its picks are
intermediate states), ``post-checkout`` (a branch checkout only, not a file checkout), ``post-merge`` and
``post-rewrite`` (the end of a rebase or an amend). Each starts ``<this interpreter> -P -m verinoda update
<project>`` in the background, detached, output discarded, so git is never held up. ``-P`` (``-I`` before Python
3.11) keeps the work tree, which git makes the hook's folder, off ``sys.path``: a project's own ``verinoda/``
folder can never stand in for the installed package. The block runs only for a commit in the project's own work
tree (the hooks folder is shared by every worktree of a repository) and only while the project still has its
``.verinoda/`` folder, so a removed worktree's block does nothing. ``VERINODA_NO_HOOK=1`` skips it for one git
command; the git variables a hook gets (``GIT_DIR``, ``GIT_INDEX_FILE``) are not passed on to the update.

Nothing that is there is overwritten. A hook file is a shell script (its ``#!`` names sh, bash, dash, zsh, ksh
or ash); Verinoda adds one marked block right after the ``#!`` line, so an ``exit`` further down cannot skip it,
keyed by the project so two projects of one repository each have theirs. ``uninstall`` removes exactly that
block (a block whose end marker was changed by hand is reported and left alone), and the file too when Verinoda
created it and nothing else is left. A hook that is not a shell script, a symbolic link (a hook shared with
other repositories) or a file that cannot be written is left alone and reported. When ``core.hooksPath`` points
outside the repository's git folder (a hook manager such as husky or pre-commit owns the hooks), nothing is
written or removed: the report gives the lines to add there. ``status`` lists every project's block in the
hooks and says which point to a folder that is gone; ``uninstall --gone`` removes those.

No merge driver: the graph is in ``.verinoda/``, which git ignores, so it is never merged; a merge only changes
source files, which ``post-merge`` re-reads.
"""
from __future__ import annotations

import shlex
import stat
import sys
from pathlib import Path

from verinoda.snapshot import git

HOOKS = ("post-commit", "post-checkout", "post-merge", "post-rewrite")
BEGIN = "# >>> verinoda update >>>"
END = "# <<< verinoda update <<<"
NO_EOL = "# (verinoda: the #! line had no line end)"
CREATED = "# this hook file was created by verinoda; it is removed when no block is left"
SHELLS = frozenset(("sh", "bash", "dash", "zsh", "ksh", "ash"))


class HookError(ValueError):
    pass


def _q(s: str) -> str:
    """A POSIX-shell word (git runs hooks with sh, on Windows too); forward slashes for a Windows path."""
    return shlex.quote(s.replace("\\", "/"))


def _key(project: Path) -> str:
    return str(Path(project).resolve()).replace("\\", "/")


def hooks_dir(repo: Path) -> tuple[Path, str | None, str]:
    """(the folder git runs hooks from, why Verinoda must not write there or None, the work tree's top)."""
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
            return d, (f"core.hooksPath is {custom}: another tool manages the hooks there; nothing was written "
                       "or removed. Add the lines `verinoda hooks install --print` shows to its post-commit, "
                       "post-checkout, post-merge and post-rewrite hooks"), top
    return d, None, top


def module_argv() -> list[str]:
    """``<this interpreter> -P -m verinoda`` (``-I`` before 3.11): the hook's folder is never on ``sys.path``."""
    return [sys.executable, "-P" if sys.version_info >= (3, 11) else "-I", "-m", "verinoda"]


def block(project: Path, hook: str, command: list[str] | None = None, top: str | None = None) -> str:
    """The marked block for ``hook`` of ``project`` (whose work tree's top is ``top``)."""
    key = _key(project)
    cmd = command or [*module_argv(), "update", key]
    run = " ".join(_q(c) for c in cmd)
    # git sets GIT_DIR, GIT_INDEX_FILE and the like for its hooks; the update's own git calls must not inherit them
    body = f"( unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_PREFIX GIT_COMMON_DIR; {run} >/dev/null 2>&1 & )"
    cond = ['[ -z "$VERINODA_NO_HOOK" ]', f"[ -d {_q(key + '/.verinoda')} ]"]
    if top:   # the hooks folder is shared by every worktree: only a git command in this project's work tree
        cond.append(f'[ "$(git rev-parse --show-toplevel 2>/dev/null)" = {_q(top.replace(chr(92), "/"))} ]')
    if hook == "post-checkout":   # $3 = 1: a branch checkout; 0: files checked out, nothing to re-index
        cond.append('[ "$3" = "1" ]')
    if hook == "post-commit":     # a rebase's picks are intermediate states; post-rewrite runs at its end
        cond.append('[ ! -d "$(git rev-parse --git-path rebase-merge)" ]')
        cond.append('[ ! -d "$(git rev-parse --git-path rebase-apply)" ]')
    return (f"{BEGIN} {key}\n"
            "# added by `verinoda hooks install`; `verinoda hooks uninstall` removes this block\n"
            f"if {' && '.join(cond)}; then {body}; fi\n"
            f"{END} {key}\n")


def _is_shell(text: str) -> bool:
    first = text.split("\n", 1)[0].strip()
    if not first.startswith("#!"):
        return False
    words = first[2:].split()
    if not words:
        return False
    prog = words[0].replace("\\", "/").rsplit("/", 1)[-1]
    if prog == "env":
        args = [w for w in words[1:] if not w.startswith("-")]
        prog = args[0].rsplit("/", 1)[-1] if args else ""
    return prog.removesuffix(".exe") in SHELLS


def _blocks(text: str) -> list[tuple[str, bool]]:
    """(project key, whether its end marker is there) of each block in a hook."""
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    out = []
    for i, ln in enumerate(lines):
        if ln.startswith(BEGIN + " "):
            key = ln[len(BEGIN) + 1:]
            out.append((key, any(x == f"{END} {key}" for x in lines[i + 1:])))
    return out


def _strip(text: str, key: str) -> tuple[str, str]:
    """``text`` without this project's block, and ``"removed"``, ``"none"`` or ``"damaged"`` (a block whose end
    marker is not there as written: nothing is removed, so no line of the user's is lost)."""
    lines = text.split("\n")
    begin = next((i for i, ln in enumerate(lines) if ln.rstrip("\r") == f"{BEGIN} {key}"), None)
    if begin is None:
        return text, "none"
    end = next((i for i in range(begin + 1, len(lines)) if lines[i].rstrip("\r") == f"{END} {key}"), None)
    if end is None:
        return text, "damaged"
    inner = lines[begin:end + 1]
    out = lines[:begin] + lines[end + 1:]
    new = "\n".join(out)
    if any(ln.rstrip("\r") == NO_EOL for ln in inner) and new.endswith("\n") and new.count("\n") == 1:
        new = new[:-1]   # the hook was only its #! line, without a line end: given back as it was
    return new, "removed"


def _only_ours(text: str) -> bool:
    """A file Verinoda created that holds nothing else: its ``#!`` line and the created-by note."""
    rest = [ln for ln in text.split("\n")[1:] if ln.strip() and ln.strip() != CREATED]
    return not rest


def install(repo: Path, *, command: list[str] | None = None, dry_run: bool = False) -> dict:
    """Add this project's block to each hook; returns what was done per hook."""
    repo = Path(repo).resolve()
    d, refuse, top = hooks_dir(repo)
    key = _key(repo)
    res: dict = {"hooks_dir": str(d), "project": key, "hooks": {}}
    if refuse:
        res["refused"] = refuse
        res["lines"] = {h: block(repo, h, command, top) for h in HOOKS}
        return res
    for h in HOOKS:
        p = d / h
        new_block = block(repo, h, command, top)
        if p.is_symlink():
            res["hooks"][h] = "left alone: a symbolic link (a hook shared with other repositories)"
            continue
        if p.exists():
            try:
                text = p.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                res["hooks"][h] = "left alone: not a text file"
                continue
            if not _is_shell(text):
                res["hooks"][h] = "left alone: not a shell script (add the block yourself; `--print` shows it)"
                continue
            base, state = _strip(text, key)
            if state == "damaged":
                res["hooks"][h] = "left alone: its verinoda block has no end marker (edited by hand); fix it first"
                continue
            first, eol, rest = base.partition("\n")
            first = first.rstrip("\r")
            crlf = "\r" if base.split("\n", 1)[0].endswith("\r") else ""
            ours = new_block if eol else new_block.replace(f"{END} {key}\n", f"{NO_EOL}\n{END} {key}\n")
            if crlf:
                ours = ours.replace("\n", "\r\n")
            new = first + crlf + "\n" + ours + rest
            if new == text:
                res["hooks"][h] = "already installed"
                continue
            action = "updated" if state == "removed" else "added to the existing hook"
        else:
            new = "#!/bin/sh\n" + new_block + CREATED + "\n"   # blocks go right after the #! line
            action = "created"
        if not dry_run:
            try:
                d.mkdir(parents=True, exist_ok=True)
                p.write_bytes(new.encode("utf-8"))
                p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except OSError as exc:
                res["hooks"][h] = f"not written: {type(exc).__name__}: {exc}"[:200]
                continue
        res["hooks"][h] = action
    res["note"] = ("each hook starts `verinoda update` in the background after a git command in this work tree; "
                   "VERINODA_NO_HOOK=1 skips it once")
    return res


def uninstall(repo: Path | str, *, gone: bool = False, dry_run: bool = False, hooks_of: Path | None = None) -> dict:
    """Remove this project's block from each hook (and a file Verinoda created that is then empty); ``gone``:
    remove instead every block whose project folder no longer exists. ``hooks_of``: a folder of the repository
    whose hooks to edit (for a project folder that is gone)."""
    where = Path(hooks_of or repo).resolve()
    d, refuse, _top = hooks_dir(where)
    key = _key(Path(repo))
    res: dict = {"hooks_dir": str(d), "project": key if not gone else "(every project folder that is gone)",
                 "hooks": {}}
    if refuse:
        res["refused"] = refuse
        return res
    for h in HOOKS:
        p = d / h
        if not p.exists() or p.is_symlink():
            res["hooks"][h] = "none" if not p.exists() else "left alone: a symbolic link"
            continue
        try:
            text = p.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            res["hooks"][h] = "left alone: not a text file"
            continue
        keys = [k for k, _ in _blocks(text) if not Path(k).exists()] if gone else [key]
        new, states = text, []
        for k in keys:
            new, state = _strip(new, k)
            states.append(state)
        if "damaged" in states:
            res["hooks"][h] = "left alone: a verinoda block has no end marker (edited by hand); fix it first"
            continue
        if "removed" not in states:
            res["hooks"][h] = "no verinoda block"
            continue
        what = "removed (the file was verinoda's)" if CREATED in new and _only_ours(new) else \
            f"{states.count('removed')} block(s) removed" if gone else "block removed"
        if not dry_run:
            try:
                if what.startswith("removed"):
                    p.unlink()
                else:
                    p.write_bytes(new.encode("utf-8"))
            except OSError as exc:
                what = f"not written: {type(exc).__name__}: {exc}"[:200]
        res["hooks"][h] = what
    return res


def status(repo: Path) -> dict:
    repo = Path(repo).resolve()
    d, refuse, _top = hooks_dir(repo)
    key = _key(repo)
    out: dict = {"hooks_dir": str(d), "project": key, "hooks": {}, "other_projects": [], "gone": []}
    others: set[str] = set()
    for h in HOOKS:
        p = d / h
        try:
            text = p.read_bytes().decode("utf-8", "replace") if p.exists() else ""
        except OSError:
            text = ""
        blocks = dict(_blocks(text))
        out["hooks"][h] = ("installed" if blocks[key] else "damaged (no end marker)") if key in blocks else \
            ("other hook, no verinoda block for this project" if text else "none")
        others |= {k for k in blocks if k != key}
    out["other_projects"] = sorted(k for k in others if Path(k).exists())
    out["gone"] = sorted(k for k in others if not Path(k).exists())
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
    for k in res.get("other_projects") or []:
        out.append(f"  also a block for {k}")
    for k in res.get("gone") or []:
        out.append(f"  a block for {k}, which no longer exists (`verinoda hooks uninstall --gone` removes it)")
    if res.get("note"):
        out.append(f"  note: {res['note']}")
    return "\n".join(out)
