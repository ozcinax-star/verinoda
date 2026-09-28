"""Verinoda's own files inside a project: never part of the project's corpus (docs/DESIGN.md D68).

``verinoda setup`` and ``verinoda install`` write agent-integration files into the project they set up:
a skill (``.claude/skills/verinoda/SKILL.md`` for Claude Code, ``.agents/skills/verinoda/SKILL.md`` for
Codex) and a server entry in a shared agent config (``mcpServers.verinoda`` in ``.mcp.json``,
``[mcp_servers.verinoda]`` in ``.codex/config.toml``). They describe Verinoda, not the project, and
their text names the interpreter that ran setup, so two installs setting up the same project write them
differently each time.

Left out of the corpus - the snapshot's file list (:func:`verinoda.snapshot.list_files`, which the search
index, the lexicon, the syntax facts, the stale-claim check, the freshness check and the debug ledger's
trees (:mod:`verinoda.treestate`, the commit side too) read), and the graph (:func:`ignore_patterns`,
applied by the graph build as ``--exclude`` rules, so the nodes an older index has from them are dropped
by the next build) - is a file that carries the ownership marker (:data:`MARKER_PREFIX`, the installer's
own test of "managed by Verinoda") and is

* under a folder of the shape the installer writes its skills to (:data:`SKILL_DIRS`:
  ``<dir>/.claude/skills/verinoda/``, ``<dir>/.agents/skills/verinoda/``), at any depth (a project nested
  in the one indexed, set up on its own, has its skill there) and in any letter case (a case-insensitive
  file system puts the installer's ``.claude`` into an existing ``.Claude``); or
* listed by the project's install manifest (``.verinoda/install-manifest.json``) as written whole by
  Verinoda (``kind: "file"``).

The marker decides, as it does for the installer: a file without it is the user's (a ``SKILL.md`` the
installer refuses to overwrite, a taken-over one, a ``reference.md`` a user keeps next to the managed
skill) and stays indexed. No manifest is needed, so a teammate's fresh clone of a project with a
committed skill leaves it out too.

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
# the skill folders of a project-scope install, at the project's root
SKILL_PREFIXES = tuple("/".join(parts) + "/" for parts in SKILL_DIRS.values())
_SKILL_DIR_PARTS = frozenset(tuple(p.lower() for p in parts) for parts in SKILL_DIRS.values())
_SKILL_NEEDLE = f"skills/{NAME}/"  # in every skill-folder path, lower-cased: a cheap test before the split
# git pathspecs of the files under a skill folder at any depth, any case (paths relative to the -C folder)
_SKILL_PATHSPECS = tuple(":(glob,icase)**/" + "/".join(parts) + "/**" for parts in SKILL_DIRS.values())
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


def in_skill_dir(rel: str) -> bool:
    """Is the POSIX path ``rel`` under a folder of the installer's skill shape (:data:`SKILL_DIRS`), at any
    depth and in any letter case? Only where a file may be Verinoda's; the marker decides (:func:`own_filter`)."""
    low = rel.lower()
    if _SKILL_NEEDLE not in low:
        return False
    parts = low.split("/")
    return any(tuple(parts[i:i + 3]) in _SKILL_DIR_PARTS for i in range(len(parts) - 3))


def own_filter(repo: Path) -> Callable[[str], bool]:
    """``rel -> bool``: is the project-relative POSIX path ``rel`` one of Verinoda's own files - under a
    skill folder (:func:`in_skill_dir`) or listed by the install manifest as written whole by Verinoda, and
    carrying the ownership marker now (the manifest is read once; each marker check is remembered by the
    file's size and time)."""
    repo = Path(repo)
    listed = frozenset(_manifest_files(repo / MANIFEST_DIR / MANIFEST_NAME))

    def own(rel: str) -> bool:
        return (rel in listed or in_skill_dir(rel)) and _has_marker(repo / rel)
    return own


def is_own(repo: Path, rel: str) -> bool:
    """Is the project-relative POSIX path ``rel`` one of Verinoda's own files in ``repo``?"""
    return own_filter(repo)(rel)


def could_be_own(repo: Path) -> Callable[[str], bool]:
    """A cheap test on any spelling of a path (absolute or relative, either slash) with no file-system call:
    False means it is not one of Verinoda's own files; True means :func:`own_filter` has to decide."""
    listed = tuple(_manifest_files(Path(repo) / MANIFEST_DIR / MANIFEST_NAME))

    def maybe(path: str) -> bool:
        s = path.replace("\\", "/")
        return in_skill_dir(s) or any(s == rel or s.endswith("/" + rel) for rel in listed)
    return maybe


def _skill_dir_candidates(repo: Path) -> set[str]:
    """Project-relative paths of the files under skill folders (:func:`in_skill_dir`) at any depth: git's
    tracked and untracked-not-ignored files (one ``ls-files`` limited to those folders), outside git a walk
    as :func:`verinoda.snapshot.list_files` walks, and the root's own skill folders read from disk whatever
    git ignores (a build that does not honour .gitignore still leaves them out)."""
    from verinoda.snapshot import _SKIP_DIRS, git

    out: set[str] = set()
    listed = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *_SKILL_PATHSPECS)
    if listed is not None:
        out.update(r for r in listed.split("\0") if r)
    else:
        for dirpath, dirnames, filenames in os.walk(repo):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.endswith(".egg-info")]
            for fn in filenames:
                rel = (Path(dirpath) / fn).relative_to(repo).as_posix()
                if in_skill_dir(rel):
                    out.add(rel)
    for parts in _SKILL_DIR_PARTS:
        dirs = [repo]
        for part in parts:  # each level as spelled on disk (a case-insensitive file system may hold .Claude)
            found: list[Path] = []
            for d in dirs:
                try:
                    with os.scandir(d) as it:
                        found += [Path(e.path) for e in it if e.name.lower() == part and e.is_dir()]
                except OSError:
                    pass
            dirs = found
        for d in dirs:
            for dirpath, _dirnames, filenames in os.walk(d):
                out.update((Path(dirpath) / fn).relative_to(repo).as_posix() for fn in filenames)
    return out


def own_files(repo: Path) -> list[str]:
    """Verinoda's own files present in the project (project-relative POSIX paths, sorted)."""
    repo = Path(repo)
    own = own_filter(repo)
    cands = _skill_dir_candidates(repo) | set(_manifest_files(repo / MANIFEST_DIR / MANIFEST_NAME))
    return sorted(rel for rel in cands if own(rel))


def _escape(rel: str) -> str:
    """``rel`` as a literal gitignore pattern (after the leading ``/`` a ``#`` or ``!`` is literal)."""
    out = "".join("\\" + c if c in "*?[]\\" else c for c in rel)
    kept = out.rstrip(" ")  # a trailing space is dropped from a pattern unless escaped
    return kept + "\\ " * (len(out) - len(kept))


def ignore_patterns(repo: Path) -> list[str]:
    """The ``--exclude`` rules (anchored at the project root) that keep Verinoda's own files out of the graph:
    one literal pattern per file (:func:`own_files`; the marker decides, which no folder pattern can say).
    Given to the build's ``detect`` and to the rule that evicts the nodes of files a rule now excludes, so an
    index built before these rules loses those nodes on its next build."""
    return ["/" + _escape(rel) for rel in own_files(repo)]


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
