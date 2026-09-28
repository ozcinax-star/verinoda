"""Verinoda's own files inside a project: never part of the project's corpus (docs/DESIGN.md D68).

``verinoda setup`` and ``verinoda install`` write agent-integration files into the project they set up:
a skill (``.claude/skills/verinoda/SKILL.md`` for Claude Code, ``.agents/skills/verinoda/SKILL.md`` for
Codex) and a server entry in a shared agent config (``mcpServers.verinoda`` in ``.mcp.json``,
``[mcp_servers.verinoda]`` in ``.codex/config.toml``). They describe Verinoda, not the project, and
their text names the interpreter that ran setup, so two installs setting up the same project write them
differently each time.

Left out of the corpus - the snapshot's file list (:func:`verinoda.snapshot.list_files`, which the search
index, the lexicon, the syntax facts, the stale-claim check and the freshness check read), and the graph
(:func:`ignore_patterns`, applied by the graph build as ``--exclude`` rules, so the nodes an older index
has from them are dropped by the next build):

* every file under the skill folders the installer writes (:data:`SKILL_DIRS`), whatever else is there;
* a file the project's install manifest (``.verinoda/install-manifest.json``) lists as written whole by
  Verinoda (``kind: "file"``) while it still carries the ownership marker (:data:`MARKER_PREFIX`); one
  that lost the marker was taken over by the user and is theirs again.

Shared agent configs stay in the corpus: they hold the user's other servers. Verinoda's own server entry
in an MCP config never shapes the graph (``mcp_ingest`` skips it: :func:`is_own_mcp_entry`), and a change
confined to it does not rebuild the graph: :func:`config_digest` is the file's digest with that entry
left out, recorded when the graph is built (:func:`verinoda.buildlock.record_build`) and compared by
:func:`verinoda.workflow._graph_affected`. ``.codex/config.toml`` has no graph extractor, so a change
there never rebuilt the graph; it is re-indexed for search only.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Callable

NAME = "verinoda"
MARKER = "<!-- verinoda-managed v1 -->"
MARKER_PREFIX = b"<!-- verinoda-managed"
MANIFEST_DIR = ".verinoda"
MANIFEST_NAME = "install-manifest.json"
# The folder of each agent's skill, under the project (project scope) or the home folder (user scope).
SKILL_DIRS: dict[str, tuple[str, ...]] = {
    "claude": (".claude", "skills", NAME),
    "codex": (".agents", "skills", NAME),
}
SKILL_PREFIXES = tuple("/".join(parts) + "/" for parts in SKILL_DIRS.values())
# The files an MCP config extractor reads (verinoda.project_index.mcp_ingest.MCP_CONFIG_FILENAMES).
MCP_CONFIG_NAMES = frozenset({".mcp.json", "claude_desktop_config.json", "mcp.json", "mcp_servers.json"})
_MARKER_READ = 1 << 20  # a skill is a few KB; a larger file is read this far for the marker

_MANIFESTS: dict[str, tuple[tuple, tuple[str, ...]]] = {}  # manifest path -> (its stat, listed files)
_MARKED: dict[str, tuple[tuple, bool]] = {}                # file path -> (its stat, carries the marker)


def _stat_key(p: Path) -> tuple | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _manifest_files(manifest: Path) -> tuple[str, ...]:
    """Project-relative paths of the ``kind: "file"`` items of every install the manifest records.
    An unreadable or malformed manifest lists nothing (it never breaks a file listing)."""
    key = _stat_key(manifest)
    if key is None:
        return ()
    memo = _MANIFESTS.get(str(manifest))
    if memo is not None and memo[0] == key:
        return memo[1]
    out: list[str] = []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        installs = data.get("installs") if isinstance(data, dict) else None
        for entry in (installs or {}).values() if isinstance(installs, dict) else ():
            for item in (entry.get("items") or []) if isinstance(entry, dict) else ():
                path = item.get("path") if isinstance(item, dict) and item.get("kind") == "file" else None
                if isinstance(path, str) and path and not os.path.isabs(path):
                    rel = path.replace("\\", "/")
                    if not rel.startswith("../") and rel not in out:
                        out.append(rel)
    except (OSError, ValueError, AttributeError, TypeError):
        out = []
    if len(_MANIFESTS) > 32:
        _MANIFESTS.clear()
    _MANIFESTS[str(manifest)] = (key, tuple(out))
    return tuple(out)


def _has_marker(p: Path) -> bool:
    key = _stat_key(p)
    if key is None:
        return False
    memo = _MARKED.get(str(p))
    if memo is not None and memo[0] == key:
        return memo[1]
    try:
        with open(p, "rb") as f:
            found = MARKER_PREFIX in f.read(_MARKER_READ)
    except OSError:
        found = False
    if len(_MARKED) > 256:
        _MARKED.clear()
    _MARKED[str(p)] = (key, found)
    return found


def owned_files(repo: Path) -> frozenset[str]:
    """Files outside the skill folders that the project's install manifest lists as written whole by
    Verinoda and that still carry the ownership marker (project-relative POSIX paths)."""
    repo = Path(repo)
    listed = _manifest_files(repo / MANIFEST_DIR / MANIFEST_NAME)
    return frozenset(rel for rel in listed if not rel.startswith(SKILL_PREFIXES) and _has_marker(repo / rel))


def is_own(rel: str, owned: frozenset[str] = frozenset()) -> bool:
    """Is the project-relative POSIX path ``rel`` one of Verinoda's own files (``owned``: :func:`owned_files`)?"""
    return rel.startswith(SKILL_PREFIXES) or rel in owned


def own_filter(repo: Path) -> Callable[[str], bool]:
    """``rel -> bool``: :func:`is_own` for one project, the manifest read once."""
    owned = owned_files(repo)
    return lambda rel: rel.startswith(SKILL_PREFIXES) or rel in owned


def _escape(rel: str) -> str:
    """``rel`` as a literal gitignore pattern (after the leading ``/`` a ``#`` or ``!`` is literal)."""
    out = "".join("\\" + c if c in "*?[]\\" else c for c in rel)
    kept = out.rstrip(" ")  # a trailing space is dropped from a pattern unless escaped
    return kept + "\\ " * (len(out) - len(kept))


def ignore_patterns(repo: Path) -> list[str]:
    """The ``--exclude`` rules (anchored at the project root) that keep Verinoda's own files out of the graph.
    Given to the build's ``detect`` and to the rule that evicts the nodes of files a rule now excludes, so an
    index built before these rules loses those nodes on its next build."""
    return ["/" + p for p in SKILL_PREFIXES] + ["/" + _escape(rel) for rel in sorted(owned_files(repo))]


def is_own_mcp_entry(name, spec) -> bool:
    """Is ``mcpServers[name] = spec`` Verinoda's own server (``verinoda``, started as ``... mcp serve``)?"""
    if name != NAME or not isinstance(spec, dict):
        return False
    args = spec.get("args") if isinstance(spec.get("args"), list) else []
    return any(args[i] == "mcp" and args[i + 1] == "serve" for i in range(len(args) - 1))


def is_mcp_config(rel: str) -> bool:
    return rel.replace("\\", "/").rsplit("/", 1)[-1] in MCP_CONFIG_NAMES


def config_digest(path: Path) -> str | None:
    """Digest of an MCP config as the graph reads it, Verinoda's own server entry left out: its JSON value
    with keys sorted (whitespace, key order and Verinoda's entry do not count). A file the extractor cannot
    parse is digested byte for byte. None: not an MCP config, or unreadable."""
    path = Path(path)
    if path.name not in MCP_CONFIG_NAMES:
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "b:" + hashlib.sha256(raw).hexdigest()
    if isinstance(doc, dict):  # the two shapes the extractor reads: mcpServers, or mcp.servers
        nested = doc.get("mcp") if isinstance(doc.get("mcp"), dict) else {}
        for servers in (doc.get("mcpServers"), nested.get("servers")):
            if isinstance(servers, dict) and is_own_mcp_entry(NAME, servers.get(NAME)):
                del servers[NAME]
    text = json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "j:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def config_digests(repo: Path, rels) -> dict[str, str]:
    """``{rel: config_digest}`` for the MCP configs among ``rels`` (project-relative paths)."""
    repo = Path(repo)
    out: dict[str, str] = {}
    for rel in rels:
        if is_mcp_config(rel):
            d = config_digest(repo / rel)
            if d is not None:
                out[rel] = d
    return out
