"""`verinoda ui --export`: the graph view and the file notes as one HTML file that opens without a server.

The file is the same page as `verinoda ui` (``static/``) with its data embedded: the file-level
graph, the file tree, and a note per source file, document and data file (its links, outline and
claims, the claims on its symbols included, without the code). Symbol links lead to the note of
their file. Nothing is fetched: the page's Content-Security-Policy allows only its own script and
style (by hash) and no connections. The machine's own paths (the repository's location and the
home folder, also as the index folds them into names) are taken out of the text, so the file can
be passed on; what it does contain is the project's names, relative paths, doc text and claims.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from verinoda import usernotes
from verinoda.ui.data import HIDDEN_KINDS, MAX_GLOBAL_NODES, MAX_SECTION_ITEMS, Atlas

FORMAT = "verinoda-export"
VERSION = 1
DEFAULT_NAME = "verinoda-graph.html"
ITEM_FIELDS = ("id", "title", "kind", "file", "line", "relation", "at", "status")
ID_KEYS = frozenset({"id", "source", "target", "center"})  # note ids: relative, never rewritten


def default_path(repo: Path | str) -> Path:
    """Where ``--export`` without a path writes: beside the index."""
    from verinoda.paths import graph_path

    return graph_path(Path(repo)).parent / DEFAULT_NAME


def _static(name: str) -> str:
    return resources.files("verinoda.ui").joinpath("static", name).read_text(encoding="utf-8")


def _files(tree: dict) -> list[dict]:
    out, stack = [], [tree]
    while stack:
        node = stack.pop()
        if "children" in node:
            stack.extend(node["children"])
        elif node.get("id"):
            out.append(node)
    return out


def _prune_tree(node: dict, keep: set[str]) -> dict | None:
    """The tree without files whose note is not in the export (and without folders left empty)."""
    if "children" not in node:
        return node if node.get("id") in keep else None
    kids = [k for k in (_prune_tree(c, keep) for c in node["children"]) if k is not None]
    return {**node, "children": kids} if kids else None


def _item(it: dict, target: dict[str, str], notes: set[str]) -> dict:
    """A link as the page draws it, pointing at a note the file holds: a symbol's link leads to the
    note of its file; a link with no note there is shown as text (no ``id``)."""
    out = {k: it[k] for k in ITEM_FIELDS if it.get(k) not in (None, "")}
    nid = out.get("id")
    if nid is not None and nid not in notes:
        to = target.get(it.get("file") or "")
        if to in notes:
            out["id"] = to
        else:
            out.pop("id")
    if it.get("confidence") not in (None, "EXTRACTED"):  # only an inferred link is marked
        out["confidence"] = it["confidence"]
        if it.get("context"):
            out["context"] = it["context"]
    return out


def _outline(snap, n: dict) -> list[dict]:
    """Every symbol of a file (every heading of a document) by line; the note view keeps 200, the
    exported search needs them all."""
    f = n.get("file")
    if not f or n["kind"] not in ("file", "doc"):
        return [{k: o[k] for k in ("title", "kind", "line") if o.get(k) is not None} for o in n.get("outline") or []]
    g = snap.g
    found = g.symbols_in(f) if n["kind"] == "file" else g.headings_in(f)
    return [{k: v for k, v in (("title", snap.title(s)), ("kind", snap.kind(s)), ("line", g.line(s))) if v is not None}
            for s in found if snap.kind(s) not in HIDDEN_KINDS]


def _file_claims(snap) -> dict[str, list[dict]]:
    """The current claims by the file their subjects are in: a claim on a symbol (``file::label``)
    or on the file itself, newest first; a symbol's label is kept as where it applies."""
    out: dict[str, list[dict]] = {}
    for cid, text, status, conf, names in snap._claim_rows():
        seen = set()
        for name in sorted(names):
            f, _, label = name.partition("::")
            if f in seen:
                continue
            seen.add(f)
            item = {"id": cid, "title": text, "kind": "claim", "status": status, "score": conf}
            if label:
                item["at"] = label
            out.setdefault(f, []).append(item)
    return out


def build(repo: Path | str) -> dict:
    """The embedded data: what the page's API calls are answered from (one load of the index)."""
    atlas = Atlas(repo)
    snap = atlas.snapshot()  # one snapshot for everything: an update meanwhile cannot mix two indexes
    stats = {k: v for k, v in snap.stats().items() if k != "root"}  # no absolute path of this machine
    tree = snap.tree()
    raw = {}
    for f in _files(tree):
        try:
            raw[f["id"]] = snap.note(f["id"])
        except KeyError:
            continue
    ids = set(raw)
    target = {n["file"]: nid for nid, n in raw.items() if n.get("file") and n["kind"] in ("file", "doc")}
    claims = _file_claims(snap)
    notes = {}
    for nid, n in raw.items():
        code = n.get("code") or {}
        sections = [{**s, "items": [_item(it, target, ids) for it in s["items"]]}
                    for s in n["sections"] if s["key"] != "claims"]
        on_file = claims.get(n.get("file") or "", []) if n["kind"] in ("file", "doc") else \
            next((s["items"] for s in n["sections"] if s["key"] == "claims"), [])
        if on_file:
            sections.append({"key": "claims", "count": len(on_file),
                             "items": [_item(c, target, ids) for c in on_file[:MAX_SECTION_ITEMS]]})
        notes[nid] = {
            "id": nid, "title": n["title"], "kind": n["kind"], "file": n.get("file"), "line": n.get("line"),
            "span": n.get("span"), "signature": n.get("signature"), "doc": n.get("doc"), "test": n.get("test"),
            "degree": n.get("degree"),
            # the code stays in the repository (`verinoda ui` shows it); its length is kept
            "code": None, "code_lines": code.get("total"),
            "breadcrumb": [_item(c, target, ids) for c in n.get("breadcrumb") or []],
            "outline": _outline(snap, n),
            "sections": sections,
        }
    stats["hubs"] = [{**_item(h, target, ids), "degree": h.get("degree")} for h in stats.get("hubs") or []]
    graph = snap.global_graph(tests=True, data=True, cap=None)  # the page filters, then keeps the best
    kept = [n for n in graph["nodes"] if n["id"] in ids]
    kept_ids = {n["id"] for n in kept}
    graph = {**graph, "nodes": kept, "cap": MAX_GLOBAL_NODES,
             "edges": [e for e in graph["edges"] if e["source"] in kept_ids and e["target"] in kept_ids]}
    # notes of your own: on the file's note (a symbol's under its label), read-only, and listed on the start page
    mine = []
    for u in usernotes.load_all(Path(repo)):
        d = usernotes.as_dict(Path(repo), u)
        home = target.get(u.file) or snap.data_note_for(u.file)
        item = {k: d[k] for k in ("subject", "text", "status", "written", "why")}
        item["label"] = u.subject.partition("::")[2] or None
        if home in notes:
            if u.subject == u.file:
                notes[home]["user_note"] = item
            else:
                notes[home].setdefault("user_notes", []).append(item)
        mine.append({**item, "id": home if home in notes else None})
    data = {"stats": stats, "tree": _prune_tree(tree, ids) or {**tree, "children": []}, "global": graph,
            "notes": notes, "user_notes": mine}
    # the file is made to be passed on: no path of this machine in its text
    data = _scrub(data, _scrubber(Path(repo).resolve()))
    return {"format": FORMAT, "version": VERSION,
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **data}


# -- this machine's paths ---------------------------------------------------------------------------

def _slugs(p: Path) -> set[str]:
    """``p`` folded into a name the way the index folds an unresolved import's absolute path
    (``normalize_id``: case-folded, NFKC, every non-word character to ``_``), and as plain ASCII."""
    from verinoda.project_index.ids import normalize_id

    return {s for s in (normalize_id(str(p)), re.sub(r"[^a-z0-9]+", "_", str(p).lower()).strip("_")) if s}


def _scrubber(root: Path):
    """A function that takes this machine's paths out of a text: the project root (as a path, in
    either slash form, or folded into a name such as ``c_users_me_src_proj_mod``) and then the home
    folder (``~``). A path matches only as a whole path and a name only as whole ``_`` parts, so
    ``src/components/home/user.ts`` or ``sync_users_members`` are left alone."""
    home = Path.home().resolve()
    rules = []
    for base, into, into_slug in ((root, "", ""), (home, "~/", "home_")):
        for form in {str(base), base.as_posix()}:
            if len(form.strip("/\\")) < 2:  # a root of "/" or "C:" would match every path
                continue
            rx = r"(?<![\w.~-])" + re.escape(form) + r"(?:[\\/]|(?=$|[^\w.-]))"
            rules.append((re.compile(rx, re.IGNORECASE), into))
        for slug in _slugs(base):
            # only a folded path of three parts or more (c_users_me, home_me_src): a shorter one
            # (workspace, home_me) is an ordinary name as often as a path, and names no user
            if slug.count("_") >= 2 and len(slug) >= 8:
                rules.append((re.compile(r"(?<!\w)" + re.escape(slug) + r"(?:_|(?!\w))", re.IGNORECASE), into_slug))

    def scrub(text: str) -> str:
        for rx, into in rules:
            text = rx.sub(into, text)
        return text

    return scrub


def _scrub(obj, scrub, key: str | None = None):
    """Every text in ``obj`` scrubbed, except note ids (relative paths folded into names; changing
    one could make it another note's) and dict keys (field names and those ids)."""
    if isinstance(obj, str):
        return obj if key in ID_KEYS else scrub(obj)
    if isinstance(obj, list):
        return [_scrub(v, scrub, key) for v in obj]
    if isinstance(obj, dict):
        return {k: _scrub(v, scrub, k) for k, v in obj.items()}
    return obj


# -- the page -----------------------------------------------------------------------------------------

def _json_for_html(obj) -> str:
    """JSON that cannot end its <script> element or open a comment: ``<``, ``>`` and ``&`` escaped."""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _sha256(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii") + "'"


def render(data: dict) -> str:
    """The page with its script, style and data inline."""
    page, css, js = _static("index.html"), _static("app.css"), _static("app.js")
    if "</script" in js.lower() or "</style" in css.lower():
        raise ValueError("the page's script or style would end its own element")
    csp = (f"default-src 'none'; script-src {_sha256(js)}; style-src {_sha256(css)}; img-src data:; "
           "base-uri 'none'; form-action 'none'")
    title = f"{data['stats'].get('project') or 'project'} · Verinoda"
    esc = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    head_old = '<meta charset="utf-8">'
    replacements = [
        (head_old, f'{head_old}\n<meta http-equiv="Content-Security-Policy" content="{csp}">'),
        ("<title>Verinoda</title>", f"<title>{esc}</title>"),
        ('<link rel="stylesheet" href="app.css">', f"<style>{css}</style>"),
        ('<script src="app.js"></script>',
         f'<script type="application/json" id="verinoda-data">{_json_for_html(data)}</script>\n<script>{js}</script>'),
    ]
    for old, new in replacements:
        if page.count(old) != 1:
            raise ValueError(f"the page template changed: {old!r}")
        page = page.replace(old, new)
    return page


def target_path(repo: Path | str, out: Path | str | None) -> Path:
    """The file to write: ``out``, or ``DEFAULT_NAME`` inside it when it is a folder (an existing one,
    or one named with a trailing separator), or the default beside the index."""
    if out is None or str(out) == "":
        return default_path(repo)
    raw = str(out)
    path = Path(raw)
    if raw.endswith(("/", "\\")) or path.is_dir():
        return path / DEFAULT_NAME
    return path


def write(repo: Path | str, out: Path | str | None = None) -> dict:
    """Build and write the file (whole or not at all); returns where it went and what it holds."""
    path = target_path(repo, out)
    data = build(repo)
    html = render(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(html)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return {"path": str(path.resolve()), "bytes": len(html.encode("utf-8")), "notes": len(data["notes"]),
            "graph_files": min(len(data["global"]["nodes"]), MAX_GLOBAL_NODES),
            "graph_links": len(data["global"]["edges"])}
