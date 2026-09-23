"""Does a cited source line actually name the call target?

Used by the analysis loop (to decide whether a relation claim is statically
verified) and by critique (to refute a relation whose call site does not name
its target). A plain word match misses calls made through import aliases -
``from graphify.analyze import god_nodes as _god_nodes`` then ``_god_nodes(G)``
- which made true calls look refuted. For Python files this module also
accepts any local alias bound to the target name by an import in that file.

Only aliases are resolved. It still cannot tell *which* definition a name
refers to when several share it; that stays an inference.
"""

from __future__ import annotations

import ast
import re
from functools import lru_cache
from pathlib import Path


def target_token(label: str) -> str:
    """`.save()` -> save, `pkg.mod.func()` -> func, `OrderRepository` -> OrderRepository."""
    return label.strip().lstrip(".").split("(")[0].rpartition(".")[2]


@lru_cache(maxsize=512)
def _aliases(path: str, mtime: float) -> dict[str, frozenset[str]]:
    """original name -> local names bound to it by imports in this file."""
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError, OSError):
        return {}
    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.asname and a.name != "*":
                    out.setdefault(a.name, set()).add(a.asname)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    out.setdefault(a.name.rpartition(".")[2], set()).add(a.asname)
    return {k: frozenset(v) for k, v in out.items()}


def local_names(file: Path, target_label: str) -> set[str]:
    """Names under which ``target_label`` may appear in ``file`` (itself + import aliases)."""
    tok = target_token(target_label)
    names = {tok} if tok else set()
    if file.suffix == ".py" and file.exists():
        names |= set(_aliases(str(file), file.stat().st_mtime).get(tok, ()))
    return names


def line_names_target(file: Path, line_text: str | None, target_label: str) -> tuple[bool, str | None]:
    """(True, name) when the line contains the target or one of its import aliases."""
    if not line_text:
        return False, None
    for name in sorted(local_names(file, target_label)):
        if re.search(rf"(?<![\w.]){re.escape(name)}\b|\.{re.escape(name)}\b", line_text):
            return True, name
    return False, None


def check(repo: Path, at: str | None, target_label: str) -> tuple[bool, str | None, str | None]:
    """Check ``path:line`` in ``repo``. Returns (names_target, matched_name, line_text)."""
    if not at or ":" not in at:
        return False, None, None
    path, _, ln = at.rpartition(":")
    if not ln.isdigit():
        return False, None, None
    f = Path(repo) / path
    try:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False, None, None
    i = int(ln)
    if not 1 <= i <= len(lines):
        return False, None, None
    ok, name = line_names_target(f, lines[i - 1], target_label)
    return ok, name, lines[i - 1]
