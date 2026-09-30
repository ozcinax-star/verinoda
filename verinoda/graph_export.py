"""`verinoda export`: the graph in formats other tools read - GraphML, Neo4j Cypher, an Obsidian vault, SVG.

What is written is the index's graph as Verinoda reads it (``index.load``: edges in their true
direction, parallel edges kept), not Graphify's undirected build graph that `verinoda index export`
writes. Every edge carries what a claim about it would rest on: its relation, the extractor's
``confidence``, the ``file:line`` it was read at and a ``status`` - ``strong_inference`` for an
EXTRACTED edge, ``weak_inference`` for an INFERRED or AMBIGUOUS one, ``unknown`` for one with no
line (a file alone is no line-level evidence). That is the ceiling of an edge nobody has checked
(the call-site line is not read here; `verinoda analyze` does that): graph edges are extractions,
never verification. A node whose file changed since the index is marked ``stale``, and so is an
edge with an end or its location in such a file.

The machine's own paths are taken out of every text (as `verinoda ui --export` does), so the file can
be passed on; the code itself is not in it. Nothing is sent anywhere: each format is a local file (a
folder for the vault) that the user opens or imports.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

FORMATS = ("graphml", "cypher", "obsidian", "svg")
DEFAULT_NAMES = {"graphml": "graph.graphml", "cypher": "graph.cypher", "obsidian": "obsidian", "svg": "graph.svg"}
FORMAT = "verinoda-graph"
VERSION = 1
NOTE = ("edges are extractor output, not verified: status is the ceiling without reading the call site "
        "(strong_inference for EXTRACTED, weak_inference for INFERRED or AMBIGUOUS, unknown without a line); "
        "`verinoda analyze` checks a relation against its line")
MANIFEST = ".verinoda-export.json"
INDEX_NOTE = "_Verinoda export.md"
NOTE_SECTION_CAP = 200      # links listed per section of a vault note; the rest are counted
SVG_MAX_FILES = 300         # files drawn in the SVG (most linked first); a spring layout of more is unreadable
_NOTE_UNSAFE = re.compile(r'[<>:"|?*#^\[\]\\\x00-\x1f]')  # not in a Windows file name or an Obsidian link
_LINE_AT = re.compile(r":\d+$")
_SURROGATE = re.compile(r"[\ud800-\udfff]")
_MARKERS = ("verinoda-graph", "Verinoda graph export")  # in the head of every single-file export


def default_path(repo: Path | str, fmt: str) -> Path:
    """Where an export without ``--out`` goes: ``export/`` beside the index."""
    from verinoda.paths import graph_path

    return graph_path(Path(repo)).parent / "export" / DEFAULT_NAMES[fmt]


def target_path(repo: Path | str, fmt: str, out: Path | str | None) -> Path:
    """``out``, or the format's default name inside it when it is a folder (an existing one, or one named
    with a trailing separator); a vault is always the folder itself."""
    if out is None or str(out) == "":
        return default_path(repo, fmt)
    raw = str(out)
    path = Path(raw)
    if fmt != "obsidian" and (raw.endswith(("/", "\\")) or path.is_dir()):
        return path / DEFAULT_NAMES[fmt]
    return path


def _refuse_foreign_file(path: Path) -> None:
    """A single-file export replaces only a file an earlier export wrote (or nothing)."""
    if not path.is_file():
        return
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(8192)
    except OSError:
        return  # the write itself will say why it cannot
    if not any(m in head for m in _MARKERS):
        raise ValueError(f"{path} exists and was not written by `verinoda export`; "
                         "choose another --out (nothing was written)")


# -- the model: nodes and edges with their evidence --------------------------------------------

def _kind(g, nid: str) -> str:
    d = g.G.nodes[nid]
    if g.is_file_node(nid):
        return "file"
    if d.get("_callable_class"):
        return "class"
    if d.get("_callable"):
        return "callable"
    if not d.get("source_file"):
        return "external"
    if g.is_heading(nid):
        return "section"
    return str(d.get("file_type") or "other")


def edge_status(confidence: str | None, at: str | None) -> str:
    """The status an unchecked graph edge can carry at most: ``unknown`` unless ``at`` names a line."""
    if not at or not _LINE_AT.search(at):
        return "unknown"
    return "strong_inference" if confidence == "EXTRACTED" else "weak_inference"


def _snapshot(repo: Path) -> dict:
    """The latest snapshot's commit and whether the tree was dirty (``{}`` without one)."""
    try:
        from verinoda.store import open_store

        st = open_store(repo)
    except Exception:  # noqa: BLE001 - no atlas.db or one that cannot be opened: the export says "commit unknown"
        return {}
    try:
        snap = st.latest_snapshot() or {}
    finally:
        st.close()
    return {"commit": snap.get("commit_sha"), "dirty": bool(snap.get("dirty"))} if snap else {}


def build(repo: Path | str, g=None, *, fresh: dict | None = None) -> dict:
    """The export model: ``{"format", "version", "project", "commit", "dirty", "note", "nodes", "edges",
    "stale_files", "index_freshness"?}``, nodes and edges sorted, machine paths removed."""
    from verinoda import freshness, index
    from verinoda.ui.export import _scrubber

    repo = Path(repo).resolve()
    g = g if g is not None else index.load(repo)
    fresh = fresh if fresh is not None else freshness.check(repo)
    stale = set(fresh.get("files") or ())
    scrub_paths = _scrubber(repo)

    def scrub(text: str) -> str:  # a lone surrogate (a name read from a mis-encoded file) cannot be written
        return _SURROGATE.sub("\ufffd", scrub_paths(text))

    ids: dict[str, str] = {}
    taken: set[str] = set()
    for nid in sorted(g.G.nodes):
        new = scrub(nid)
        if new in taken:  # two ids scrubbed into one: keep them apart
            k = 2
            while f"{new}~{k}" in taken:
                k += 1
            new = f"{new}~{k}"
        ids[nid] = new
        taken.add(new)

    nodes = []
    for nid in sorted(g.G.nodes, key=lambda n: ids[n]):
        f = g.file(nid)
        n = {"id": ids[nid], "label": scrub(str(g.label(nid))), "kind": _kind(g, nid)}
        if f:
            n["file"] = scrub(f)
            if g.line(nid) is not None:
                n["line"] = g.line(nid)
            if f in stale:
                n["stale"] = True
        nodes.append(n)

    stale_ids = {nid for nid in g.G.nodes if g.file(nid) in stale}
    edges = []
    for u, v, d in g.edges():
        f, loc = d.get("source_file"), d.get("source_location") or ""
        at = f"{f}:{loc[1:]}" if f and loc.startswith("L") and loc[1:].isdigit() else f
        e = {"source": ids[u], "target": ids[v], "relation": str(d.get("relation") or "related"),
             "confidence": str(d.get("confidence") or "unknown"), "status": edge_status(d.get("confidence"), at)}
        if at:
            e["at"] = scrub(at)
        if str(d.get("_origin", "")).startswith("verinoda"):
            e["derived_by"] = d["_origin"]
        if (f and f in stale) or u in stale_ids or v in stale_ids:  # an end may be gone or moved
            e["stale"] = True
        edges.append(e)
    edges.sort(key=lambda e: (e["source"], e["target"], e["relation"], e.get("at") or ""))

    out = {"format": FORMAT, "version": VERSION, "project": repo.name, **_snapshot(repo), "note": NOTE,
           "nodes": nodes, "edges": edges, "stale_files": sorted(scrub(f) for f in stale)}
    if not fresh.get("checked") and fresh.get("why"):
        out["index_freshness"] = f"not checked: {fresh['why']}"
    return out


# -- writers -----------------------------------------------------------------------------------

def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# GraphML attribute keys per scope: (name, GraphML type). Written by hand rather than through
# networkx.write_graphml, which took 14 s for Verinoda's own graph (75k edges) against about 4 s here.
_GRAPHML_KEYS = {
    "graph": (("format", "string"), ("version", "int"), ("project", "string"), ("commit", "string"),
              ("dirty", "boolean"), ("note", "string")),
    "node": (("label", "string"), ("kind", "string"), ("file", "string"), ("line", "int"), ("stale", "boolean")),
    "edge": (("relation", "string"), ("confidence", "string"), ("status", "string"), ("at", "string"),
             ("derived_by", "string"), ("stale", "boolean")),
}


def to_graphml(model: dict) -> str:
    """A directed GraphML document (Gephi, yEd, Cytoscape): parallel edges kept, one ``<edge>`` each."""
    from xml.sax.saxutils import escape, quoteattr

    from verinoda.project_index.export import _strip_xml_illegal

    def text(val) -> str:
        if isinstance(val, bool):
            return "true" if val else "false"
        return escape(_strip_xml_illegal(str(val)))

    def attr(val) -> str:
        return quoteattr(_strip_xml_illegal(str(val)))

    def data(scope: str, obj: dict, indent: str) -> list[str]:
        return [f'{indent}<data key="{scope[0]}_{k}">{text(obj[k])}</data>'
                for k, _ in _GRAPHML_KEYS[scope] if obj.get(k) is not None]

    out = ["<?xml version='1.0' encoding='utf-8'?>",
           '<graphml xmlns="http://graphml.graphdrawing.org/xmlns" '
           'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
           'xsi:schemaLocation="http://graphml.graphdrawing.org/xmlns '
           'http://graphml.graphdrawing.org/xmlns/1.0/graphml.xsd">']
    for scope, keys in _GRAPHML_KEYS.items():
        out += [f'  <key id="{scope[0]}_{k}" for="{scope}" attr.name="{k}" attr.type="{t}"/>' for k, t in keys]
    out.append('  <graph id="G" edgedefault="directed">')
    out += data("graph", model, "    ")  # no commit or dirty flag without a snapshot: none is claimed
    for n in model["nodes"]:
        out.append(f"    <node id={attr(n['id'])}>")
        out += data("node", n, "      ")
        out.append("    </node>")
    for i, e in enumerate(model["edges"]):
        out.append(f"    <edge id=\"e{i}\" source={attr(e['source'])} target={attr(e['target'])}>")
        out += data("edge", e, "      ")
        out.append("    </edge>")
    out += ["  </graph>", "</graphml>"]
    return "\n".join(out) + "\n"


def to_cypher(model: dict) -> str:
    """OpenCypher statements for Neo4j (``cypher-shell < graph.cypher``). Nodes carry the label ``Verinoda``
    and their kind; MERGE on the id and, for an edge, on its relation, location and confidence, so a second
    import of the same export adds nothing while an EXTRACTED and an INFERRED edge on one line stay two."""
    from verinoda.project_index.export import _cypher_escape as esc
    from verinoda.project_index.export import _cypher_label

    def props(d: dict) -> str:
        parts = []
        for k, v in d.items():
            if isinstance(v, bool):
                parts.append(f"{k}: {'true' if v else 'false'}")
            elif isinstance(v, int):
                parts.append(f"{k}: {v}")
            else:
                parts.append(f"{k}: '{esc(str(v))}'")
        return ", ".join(parts)

    lines = [f"// Verinoda graph export of {esc(model['project'])} at commit {model.get('commit') or 'unknown'}",
             f"// {NOTE}",
             "CREATE INDEX verinoda_id IF NOT EXISTS FOR (n:Verinoda) ON (n.id);", ""]
    for n in model["nodes"]:
        kind = _cypher_label(n["kind"].title().replace("_", ""), "Node")
        rest = {k: v for k, v in n.items() if k != "id"}
        lines.append(f"MERGE (n:Verinoda {{id: '{esc(n['id'])}'}}) SET n:{kind} SET n += {{{props(rest)}}};")
    lines.append("")
    for e in model["edges"]:
        rel = _cypher_label(e["relation"].upper(), "RELATED")
        rest = {k: v for k, v in e.items() if k not in ("source", "target", "at", "confidence")}
        key = f"at: '{esc(e.get('at') or '')}', confidence: '{esc(e['confidence'])}'"
        lines.append(f"MATCH (a:Verinoda {{id: '{esc(e['source'])}'}}), (b:Verinoda {{id: '{esc(e['target'])}'}}) "
                     f"MERGE (a)-[r:{rel} {{{key}}}]->(b) SET r += {{{props(rest)}}};")
    return "\n".join(lines) + "\n"


def _note_names(files: list[str]) -> dict[str, str]:
    """A vault note path (without ``.md``) per source file: its relative path, with the characters a Windows
    file name or an Obsidian link cannot hold replaced (a leading dot too: Obsidian hides such a file); two
    files that end up alike are numbered."""
    out: dict[str, str] = {}
    taken: set[str] = set()
    for f in sorted(files):
        parts = [p for p in PurePosixPath(f.replace("\\", "/")).parts if p not in ("", ".", "..", "/")]
        name = "/".join(_note_part(p) for p in parts) or "_"
        base, k = name, 2
        while name.lower() in taken:
            name, k = f"{base}~{k}", k + 1
        taken.add(name.lower())
        out[f] = name
    return out


def _note_part(p: str) -> str:
    """One path part as a note path part: a leading dot or space becomes ``_`` (``.b.py`` -> ``_b.py``, so
    it does not take ``b.py``'s name) and a trailing one is dropped (Windows drops it)."""
    p = _NOTE_UNSAFE.sub("_", p).rstrip(" .")
    rest = p.lstrip(" .")
    return ("_" + rest if rest != p else p) or "_"


def wikilink(name: str, shown: str) -> str:
    """A link to the note ``name`` (its path without ``.md``). The ``.md`` is written out: a note is named
    after its source file (``cli.py.md``), and a link to ``cli.py`` would look for that file itself."""
    return f"[[{name}.md|{_NOTE_UNSAFE.sub('_', shown)}]]"


def _link_line(e: dict, other: str, labels: dict[str, str], names: dict[str, str], files: dict[str, str]) -> str:
    """One edge as a list item: its relation, the other file's note, the two symbols, where and how sure."""
    a, b = labels.get(e["source"], e["source"]), labels.get(e["target"], e["target"])
    f = files.get(other)
    where = wikilink(names[f], f) if f in names else f"{_code(labels.get(other, other))} (outside the project)"
    at = f" at {_code(e['at'])}" if e.get("at") else ""
    stale = ", stale" if e.get("stale") else ""
    return f"- {e['relation']} {where}: {_code(a)} -> {_code(b)}{at} ({e['confidence']}, {e['status']}{stale})"


def _code(s: str) -> str:
    """An inline code span that a backtick in ``s`` (a docstring's label) cannot end early."""
    return "`" + s.replace("`", "'").replace("\n", " ") + "`"


def _section(title: str, rows: list[str]) -> list[str]:
    if not rows:
        return []
    shown = rows[:NOTE_SECTION_CAP]
    more = [f"- ... {len(rows) - len(shown)} more"] if len(rows) > len(shown) else []
    return ["", f"## {title} ({len(rows)})", "", *shown, *more]


def _yaml(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # a JSON string is a YAML double-quoted scalar


def to_obsidian(model: dict) -> dict[str, str]:
    """A vault as ``{relative note path: text}``: a note per source file (its symbols, its links to other
    files as wikilinks with each edge's location and status) and an index note."""
    by_file: dict[str, list[dict]] = defaultdict(list)
    for n in model["nodes"]:
        if n.get("file"):
            by_file[n["file"]].append(n)
    names = _note_names(list(by_file))
    labels = {n["id"]: n["label"] for n in model["nodes"]}
    files = {n["id"]: n["file"] for n in model["nodes"] if n.get("file")}
    outs: dict[str, list[dict]] = defaultdict(list)
    ins: dict[str, list[dict]] = defaultdict(list)
    inner: Counter = Counter()
    for e in model["edges"]:
        fa, fb = files.get(e["source"]), files.get(e["target"])
        if fa and fa == fb:
            inner[fa] += 1
            continue
        if fa:
            outs[fa].append(e)
        if fb:
            ins[fb].append(e)
    commit = model.get("commit") or "unknown"
    notes: dict[str, str] = {}
    for f, ns in sorted(by_file.items()):
        head = next((n for n in ns if n["kind"] == "file"), None)
        syms = sorted((n for n in ns if n is not head), key=lambda n: (n.get("line") or 0, n["label"]))
        stale = any(n.get("stale") for n in ns)
        text = ["---", f"file: {_yaml(f)}", f"kind: {_yaml(head['kind'] if head else ns[0]['kind'])}",
                f"commit: {_yaml(commit)}", *(["stale: true"] if stale else []), "tags: [verinoda]", "---",
                f"# {f}", ""]
        if stale:
            text.append("> Changed since the index: run `verinoda update` and export again.\n")
        text.append(f"Links inside this file: {inner[f]}.")
        text += _section("Symbols", [f"- {_code(n['label'])} ({n['kind']}, line {n['line']})" if n.get("line") else
                                     f"- {_code(n['label'])} ({n['kind']})" for n in syms])
        text += _section("Links out", [_link_line(e, e["target"], labels, names, files) for e in outs[f]])
        text += _section("Links in", [_link_line(e, e["source"], labels, names, files) for e in ins[f]])
        notes[names[f] + ".md"] = "\n".join(text) + "\n"
    by_status = Counter(e["status"] for e in model["edges"])
    index = ["---", f"project: {_yaml(model['project'])}", f"commit: {_yaml(commit)}", "tags: [verinoda]", "---",
             f"# {model['project']}: Verinoda graph export", "", model["note"] + ".", "",
             f"{len(notes)} file notes, {len(model['nodes'])} nodes, {len(model['edges'])} edges "
             f"({', '.join(f'{k}: {v}' for k, v in sorted(by_status.items()))}).", ""]
    if model.get("stale_files"):
        index += ["Changed since the index (their notes may be out of date):", "",
                  *[f"- {s}" for s in model["stale_files"][:50]], ""]
    index += ["## Files", "", *[f"- {wikilink(names[f], f)}" for f in sorted(by_file)]]
    notes[INDEX_NOTE] = "\n".join(index) + "\n"
    return notes


def _write_vault(out: Path, notes: dict[str, str]) -> None:
    """Write the vault into ``out``: a new or empty folder, or one an earlier export wrote (its manifest names
    the notes it owns; those no longer produced are removed, nothing else is touched)."""
    man = out / MANIFEST
    owned: list[str] = []
    if out.exists():
        if not out.is_dir():
            raise ValueError(f"{out} is a file; a vault is written into a folder")
        if man.is_file():
            try:
                owned = list(json.loads(man.read_text(encoding="utf-8")).get("notes") or [])
            except (OSError, ValueError, AttributeError):
                raise ValueError(f"{man} cannot be read; remove the folder or choose another --out") from None
        elif any(out.iterdir()):
            raise ValueError(f"{out} is not empty and was not written by `verinoda export`; "
                             "choose another --out (nothing was written)")
    root = out.resolve()
    for rel in notes:
        if root not in (out / rel).resolve().parents:  # a note name never leaves the folder
            raise ValueError(f"note {rel!r} would be written outside {out}")

    def manifest(names) -> None:
        _atomic_text(man, json.dumps({"format": "verinoda-obsidian", "version": VERSION, "notes": sorted(names)},
                                     indent=1) + "\n")

    # the manifest first, naming what is there and what is about to be: a write cut off halfway leaves a
    # folder the next export still owns
    manifest(set(owned) | set(notes))
    written = set()
    for rel, text in notes.items():
        p = (out / rel).resolve()
        _atomic_text(p, text)
        written.add(_file_key(p))
    for rel in set(owned) - set(notes):
        p = (out / rel).resolve()
        # on a case-folding disk an old name that differs only in case is the note just written
        if root in p.parents and p.is_file() and p.suffix == ".md" and _file_key(p) not in written:
            p.unlink()
    manifest(notes)


def _file_key(p: Path) -> tuple[int, int]:
    st = p.stat()
    return st.st_dev, st.st_ino


def to_svg(model: dict) -> tuple[str, dict]:
    """A file-level drawing (``(svg, {"files", "files_total", "links"})``): the most linked files, each link
    between two of them one line (dashed when every edge behind it is inferred), laid out by a seeded spring
    layout so the same graph draws the same picture."""
    import networkx as nx

    files = {n["id"]: n["file"] for n in model["nodes"] if n.get("file")}
    pair: dict[tuple[str, str], bool] = {}
    for e in model["edges"]:
        fa, fb = files.get(e["source"]), files.get(e["target"])
        if fa and fb and fa != fb:
            key = (fa, fb) if fa < fb else (fb, fa)
            pair[key] = pair.get(key, False) or e["confidence"] == "EXTRACTED"
    deg: Counter = Counter()
    for a, b in pair:
        deg[a] += 1
        deg[b] += 1
    all_files = sorted(set(files.values()))
    keep = sorted(sorted(all_files, key=lambda f: (-deg[f], f))[:SVG_MAX_FILES])
    kept = set(keep)
    H = nx.Graph()
    H.add_nodes_from(keep)
    H.add_edges_from((a, b) for a, b in pair if a in kept and b in kept)
    size, pad = 1600, 60
    pos = nx.spring_layout(H, seed=7, k=1.5 / math.sqrt(max(len(H), 1))) if len(H) > 1 else {f: (0.0, 0.0)
                                                                                              for f in keep}

    def xy(f: str) -> tuple[float, float]:
        x, y = pos[f]
        return pad + (float(x) + 1) / 2 * (size - 2 * pad), pad + (float(y) + 1) / 2 * (size - 2 * pad)

    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    top = max(deg.values(), default=1) or 1
    title = f"{model['project']} - {len(keep)} of {len(all_files)} files, commit {model.get('commit') or 'unknown'}"
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
             f'font-family="sans-serif" font-size="9">',
             "<style>svg{background:#fff}.l{stroke:#8a8f98;stroke-width:.6;opacity:.55}.i{stroke-dasharray:3 3}"
             ".n{fill:#3b6fd4;stroke:#fff;stroke-width:.8}text{fill:#222}"
             "@media (prefers-color-scheme:dark){svg{background:#16181d}.l{stroke:#6b7280}.n{stroke:#16181d}"
             "text{fill:#ddd}}</style>",
             f"<title>{esc(title)}</title>", f'<desc>{esc(NOTE)}</desc>',
             f"<metadata>{FORMAT}</metadata>"]
    for (a, b), extracted in sorted(pair.items()):
        if a in kept and b in kept:
            (x1, y1), (x2, y2) = xy(a), xy(b)
            parts.append(f'<line class="l{"" if extracted else " i"}" x1="{x1:.1f}" y1="{y1:.1f}" '
                         f'x2="{x2:.1f}" y2="{y2:.1f}"/>')
    for f in keep:
        x, y = xy(f)
        r = 2.5 + 7 * math.sqrt(deg[f] / top)
        parts.append(f'<g><title>{esc(f)} ({deg[f]} linked files)</title><circle class="n" cx="{x:.1f}" '
                     f'cy="{y:.1f}" r="{r:.1f}"/><text x="{x + r + 2:.1f}" y="{y + 3:.1f}">'
                     f'{esc(PurePosixPath(f).name)}</text></g>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n", {"files": len(keep), "files_total": len(all_files),
                                     "links": sum(1 for a, b in pair if a in kept and b in kept)}


def write(repo: Path | str, fmt: str = "graphml", out: Path | str | None = None, *, g=None,
          fresh: dict | None = None) -> dict:
    """Build the model and write it in ``fmt``; returns where it went and what it holds."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}; choose one of: {', '.join(FORMATS)}")
    repo = Path(repo).resolve()
    path = target_path(repo, fmt, out)
    if fmt != "obsidian":
        _refuse_foreign_file(path)
    model = build(repo, g, fresh=fresh)
    res: dict = {"format": fmt, "path": str(path.resolve()), "nodes": len(model["nodes"]),
                 "edges": len(model["edges"]),
                 "by_status": dict(sorted(Counter(e["status"] for e in model["edges"]).items())),
                 "commit": model.get("commit"), "note": NOTE}
    if fmt == "graphml":
        _atomic_text(path, to_graphml(model))
    elif fmt == "cypher":
        _atomic_text(path, to_cypher(model))
    elif fmt == "obsidian":
        notes = to_obsidian(model)
        _write_vault(path, notes)
        res["notes"] = len(notes)
    else:
        svg, info = to_svg(model)
        _atomic_text(path, svg)
        res.update(info)
        if info["files"] < info["files_total"]:
            res["truncated"] = True
    if model["stale_files"]:
        res["stale_count"] = len(model["stale_files"])
        res["stale_files"] = model["stale_files"][:20]
    if model.get("index_freshness"):
        res["index_freshness"] = model["index_freshness"]
    return res


def render(res: dict) -> str:
    what = {"obsidian": f"{res.get('notes')} notes", "svg": f"{res.get('files')} of {res.get('files_total')} files, "
                                                             f"{res.get('links')} links"}.get(res["format"], "")
    lines = [f"wrote {res['path']} ({res['format']}: {res['nodes']} nodes, {res['edges']} edges"
             + (f"; {what}" if what else "") + ")",
             "edge status: " + ", ".join(f"{k} {v}" for k, v in res["by_status"].items()),
             f"note: {res['note']}"]
    if res.get("stale_count"):
        lines.append(f"{res['stale_count']} file(s) changed since the index (marked stale): run `verinoda update` "
                     "and export again for a current graph")
    if res.get("index_freshness"):
        lines.append(f"index freshness {res['index_freshness']}")
    hint = {"graphml": "open it in Gephi, yEd or Cytoscape", "cypher": f"import: cypher-shell < \"{res['path']}\"",
            "obsidian": "open the folder as a vault in Obsidian", "svg": "open it in a browser or embed it"}
    lines.append(hint[res["format"]])
    return "\n".join(lines)
