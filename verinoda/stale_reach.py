"""What a change makes stale: the stored claims, the notes and the decision records it reaches (``review``).

``verinoda review`` calls :func:`reached` with the changed files (both versions) and the decision records
:mod:`verinoda.decision_reach` found. Nothing is written: the claims keep their status until ``verinoda update``
runs the invalidation (docs/DESIGN.md D24), which this reading follows, so a merged change can be read for what
it will make stale before or after it lands (``review --base main`` on the branch, ``review --base HEAD~1`` after
the merge).

Each item says why, at the changed line (``at``), with what it cites (``evidence_at``) and a status for the
statement "the change alters what this item rests on", never for "the item is now wrong":

* a claim: a symbol, binding, module statement or section it depends on has another fingerprint in the new
  version than in the base (``statically_verified``: both versions' facts compared); a file it depends on
  changed (``statically_verified``: the texts compared); a file whose definitions cannot be compared, a claim
  about the whole project when a code file changed, a claim that watches the tests when a test file changed,
  and an older claim without recorded dependencies whose cited file changed (``strong_inference``: the file
  rule ``update`` applies);
* a note (``verinoda notes``): fresh against the base version of its file and changed or gone against the new
  one (``statically_verified``: its anchor checked against both texts);
* a decision record: a change reaches the symbol it governs, or the record's own file changed (the reaches of
  :mod:`verinoda.decision_reach`, with their status).

A dependency the base version already did not match (the claim or note was stale before this change) is
counted, not listed.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

MAX_LISTED = 20
MAX_EVIDENCE = 2
LIMITS = [
    "claims are compared on the dependencies recorded when they were made; their cited lines are not re-read "
    "(`verinoda update` does that too)",
    "claims `update` already marked stale and superseded claims are not listed",
    "a decision record is listed when the change reaches the symbol it governs or its own file; guards are "
    "`decide check`'s answer",
    "a planned change (--target) has no new version: nothing is listed",
]
_RANK = {"statically_verified": 0, "strong_inference": 1}


class _Versions:
    """Both versions of each changed file, their facts and changed lines, computed once."""

    def __init__(self, diffs):
        from verinoda.review import _changed_lines

        self.old = {fd.rel: fd.old for fd in diffs}
        self.new = {fd.rel: fd.new for fd in diffs}
        self.lines = {fd.rel: _changed_lines(fd.old, fd.new) for fd in diffs}
        self._facts: dict[tuple[str, str], dict | None] = {}

    def changed(self, rel: str) -> bool:
        return rel in self.old and self.old[rel] != self.new[rel]

    def facts(self, rel: str, side: str) -> dict | None:
        from verinoda import anchors

        key = (rel, side)
        if key not in self._facts:
            text = (self.old if side == "old" else self.new).get(rel)
            f = None
            if text is not None:
                try:
                    f = anchors.compute_facts(rel, text.encode("utf-8"))
                except Exception:  # noqa: BLE001 - a version that cannot be parsed has no facts
                    f = None
            self._facts[key] = f if anchors.usable(f) else None
        return self._facts[key]

    def first(self, rel: str, span: tuple[int, int] | None = None, side: str = "new") -> str:
        """``file:line`` of the first changed line (inside ``span`` when one is given), ``(base)`` for a line
        of the base version; a file without changed lines on that side is named alone."""
        new_l, old_l = self.lines.get(rel, (set(), set()))
        ls = new_l if side == "new" else old_l
        if span:
            inside = [ln for ln in ls if span[0] <= ln <= span[1]]
            ls = inside or ls
        if not ls and side == "new":
            return self.first(rel, None, "old")
        if not ls:
            return rel
        return f"{rel}:{min(ls)}" + (" (base)" if side == "old" else "")


def _region(facts: dict | None, kind: str, name: str) -> tuple[int, int] | None:
    table = {"sym": "symbols", "mod": "module", "sec": "sections"}.get(kind)
    r = ((facts or {}).get(table) or {}).get(name) if table else None
    return (r["start"], r["end"]) if isinstance(r, dict) and "start" in r else None


def _dep_reach(d: dict, v: _Versions) -> tuple[dict | None, bool]:
    """(what the change does to one claim dependency, or None; True when the base already did not match)."""
    from verinoda import anchors
    from verinoda.claims import CODE_SUFFIXES
    from verinoda.testcode import is_test_file

    kind, path, name = anchors.split_key(d["dep_key"])
    if kind == "scope":
        code = sorted(p for p in v.old if v.changed(p) and p.endswith(CODE_SUFFIXES))
        return ({"at": v.first(code[0]), "status": "strong_inference",
                 "why": f"a claim about the whole project; {len(code)} code file(s) changed ({code[0]}"
                        + (", ..." if len(code) > 1 else "") + ")"} if code else None), False
    if kind == "testset":
        tests = sorted(p for p in v.old if v.changed(p) and is_test_file(p))
        return ({"at": v.first(tests[0]), "status": "strong_inference",
                 "why": f"the claim watches the tests; {len(tests)} test file(s) changed ({tests[0]}"
                        + (", ..." if len(tests) > 1 else "") + ")"} if tests else None), False
    if kind == "commit" or not v.changed(path):
        return None, False
    if kind == "file":
        what = "removes" if v.new[path] is None else "adds" if v.old[path] is None else "edits"
        return {"at": v.first(path), "status": "statically_verified",
                "why": f"the change {what} {path}, which the claim depends on as a whole file"}, False
    fo, fn = v.facts(path, "old"), v.facts(path, "new")
    if (fo is None and v.old[path] is not None) or (fn is None and v.new[path] is not None):
        return {"at": v.first(path), "status": "strong_inference",
                "why": f"{path} changed and its definitions could not be compared (a version does not parse); the "
                       f"claim depends on {name or path}"}, False
    before = anchors.facet_fp(fo, d["dep_key"], d["facet"]) if fo is not None else anchors.ABSENT
    after = anchors.facet_fp(fn, d["dep_key"], d["facet"]) if fn is not None else anchors.ABSENT
    if before == after:
        return None, False
    if fo is not None and d.get("scheme") == fo.get("scheme") and d.get("fp") != before:
        return None, True
    label = {"sym": "symbol", "bind": "name", "mod": "module statement", "sec": "section"}.get(kind, kind)
    if after == anchors.ABSENT:
        span = _region(fo, kind, name)
        return {"at": v.first(path, span, "old"), "status": "statically_verified",
                "why": f"the {label} {path}::{name} is gone from the new version"}, False
    facet = f"the {d['facet']} of " if kind == "sym" else ""
    return {"at": v.first(path, _region(fn, kind, name)), "status": "statically_verified",
            "why": f"{facet}the {label} {path}::{name} changed"}, False


def _evidence_at(store, cid: str) -> list[str]:
    out = []
    for e in store.claim_evidence(cid):
        if e.get("relation") == "supports" and e.get("path") and not (e.get("meta") or {}).get("root"):
            loc = f"{e['path']}:{e['line_start']}" if e.get("line_start") else e["path"]
            if loc not in out:
                out.append(loc)
        if len(out) >= MAX_EVIDENCE:
            break
    return out


def _claims(store, v: _Versions) -> tuple[list[dict], int, int]:
    """(claims the change reaches, how many were checked, how many the base already did not match)."""
    from verinoda.claims import ACTIVE, claim_files

    rows = store.all(f"SELECT * FROM claims WHERE status IN ({','.join('?' * len(ACTIVE))}) "
                     "AND superseded_by IS NULL", ACTIVE)
    if not rows:
        return [], 0, 0
    deps: dict[str, list[dict]] = {}
    for d in store.all("SELECT claim_id, dep_key, facet, fp, scheme FROM claim_deps"):
        deps.setdefault(d["claim_id"], []).append(d)
    out, before = [], 0
    for c in rows:
        reaches, was = [], False
        if c["id"] in deps:
            for d in deps[c["id"]]:
                r, stale_before = _dep_reach(d, v)
                was = was or stale_before
                if r:
                    reaches.append(r)
        else:   # an older claim: the file rule of the invalidation
            for f in sorted(claim_files(store, c)):
                if v.changed(f) and v.old[f] is not None:
                    reaches.append({"at": v.first(f), "status": "strong_inference",
                                    "why": f"the claim cites {f}, which the change "
                                           + ("removes" if v.new[f] is None else "edits")
                                           + " (no recorded dependencies: the whole file counts)"})
        if not reaches:
            before += was
            continue
        seen, uniq = set(), []
        for r in sorted(reaches, key=lambda r: _RANK.get(r["status"], 2)):
            if (r["at"], r["why"]) not in seen:
                seen.add((r["at"], r["why"]))
                uniq.append(r)
        top = uniq[0]
        out.append({"claim": c["id"], "text": (c["text"] or "")[:200], "claim_status": c["status"],
                    "status": top["status"], "at": top["at"], "why": top["why"],
                    "evidence_at": _evidence_at(store, c["id"]),
                    **({"also": [f"{r['why']} ({r['at']})" for r in uniq[1:4]]} if len(uniq) > 1 else {})})
    return out, len(rows), before


def _notes(repo: Path, v: _Versions) -> tuple[list[dict], int, int]:
    from verinoda import usernotes

    notes = usernotes.load_all(repo)
    out, before = [], 0
    for n in notes:
        if not v.changed(n.file):
            continue
        was = usernotes.check_text(n, v.old[n.file], v.facts(n.file, "old"))
        if was["status"] != "fresh":
            before += 1
            continue
        now = usernotes.check_text(n, v.new[n.file], v.facts(n.file, "new"))
        if now["status"] == "fresh":
            continue
        span = (was["start"], was["end"])
        at = v.first(n.file, span, "old") if now["status"] == "gone" else \
            v.first(n.file, (now["start"], now["end"]) if now["start"] else span)
        try:
            where = n.path.resolve().relative_to(repo).as_posix() if n.path else None
        except ValueError:
            where = n.path.name if n.path else None
        out.append({"note": n.subject, "file": n.file, "now": now["status"], "status": "statically_verified",
                    "at": at, "why": f"the note was fresh on the base version; now {now['status']}: {now['why']}",
                    "evidence_at": [where] if where else []})
    return out, len(notes), before


def _decisions(decisions: dict) -> list[dict]:
    out = []
    for r in (decisions or {}).get("records") or []:
        hits = [h for h in r.get("reached_by") or [] if h.get("kind") in ("governs", "record")]
        if not hits:
            continue
        h = hits[0]
        out.append({"decision": r["decision"], "title": r.get("title") or "", "file": r.get("file"),
                    "status": h["status"], "at": h["at"], "why": h["why"],
                    "evidence_at": list(h.get("evidence_at") or ([r["file"]] if r.get("file") else []))})
    return out


def reached(repo: Path, store, diffs, decisions: dict | None = None) -> dict:
    """What the change makes stale. ``diffs``: the review's changed files (``rel``, ``old``, ``new``; empty for
    a planned change); ``store``: the claim store (None: claims are not read); ``decisions``: the review's
    ``decisions`` (:func:`verinoda.decision_reach.reached`)."""
    repo = Path(repo).resolve()
    v = _Versions(diffs)
    res: dict = {"claims": [], "notes": [], "decisions": [], "limits": list(LIMITS)}
    errors = []
    checked = {"claims": 0, "notes": 0}
    before = {"claims": 0, "notes": 0}
    if diffs and store is not None:
        try:
            res["claims"], checked["claims"], before["claims"] = _claims(store, v)
        except Exception as exc:  # noqa: BLE001 - an unreadable store: said, the review goes on
            errors.append(f"claims: {type(exc).__name__}: {exc}"[:200])
    if diffs:
        try:
            res["notes"], checked["notes"], before["notes"] = _notes(repo, v)
        except Exception as exc:  # noqa: BLE001 - a broken notes folder: said, the review goes on
            errors.append(f"notes: {type(exc).__name__}: {exc}"[:200].replace(str(repo), "<repo>"))
    res["decisions"] = _decisions(decisions or {})
    for k in ("claims", "notes"):
        res[k].sort(key=lambda x: (_RANK.get(x["status"], 2), x["at"]))
    res["counts"] = {k: len(res[k]) for k in ("claims", "notes", "decisions")}
    res["checked"] = checked
    if any(before.values()):
        res["stale_before"] = {k: n for k, n in before.items() if n}
    for k in ("claims", "notes"):
        if len(res[k]) > MAX_LISTED:
            res[k] = res[k][:MAX_LISTED]
            res["truncated"] = True
    if store is None and diffs:
        res["not_checked"] = ["claims (no claim store)"]
    if errors:
        res["error"] = "; ".join(errors)
    return res


def render_lines(res: dict) -> list[str]:
    res = res or {}
    n = res.get("counts") or {}
    out = []
    if res.get("error"):
        out += ["", f"Made stale: not everything could be read ({res['error']})"]
    if not any(n.values()):
        return out
    out += ["", f"Made stale by the change ({n.get('claims', 0)} claim(s), {n.get('notes', 0)} note(s), "
                f"{n.get('decisions', 0)} decision record(s); read them again, then `verinoda update`):"]
    for c in (res.get("claims") or [])[:8]:
        out.append(f"  claim {c['claim']} [{c['claim_status']}] {c['text'][:90]}")
        out.append(f"      [{c['status']}] {c['why']}  at {c['at']}")
    for x in (res.get("notes") or [])[:8]:
        out.append(f"  note {x['note']}")
        out.append(f"      [{x['status']}] {x['why']}  at {x['at']}")
    for d in (res.get("decisions") or [])[:8]:
        out.append(f"  decision {d['decision']} {d['title']}")
        out.append(f"      [{d['status']}] {d['why']}  at {d['at']}")
    more = sum(max(0, n.get(k, 0) - 8) for k in ("claims", "notes", "decisions"))
    if more or res.get("truncated"):
        out.append(f"  ... {more} more (--json)" if more else "  ... more (--json)")
    return out


def summary_part(res: dict) -> str:
    n = (res or {}).get("counts") or {}
    parts = [f"{n[k]} {k[:-1] if n[k] == 1 else k}" for k in ("claims", "notes") if n.get(k)]
    if n.get("decisions"):
        parts.append(f"{n['decisions']} decision record(s)")
    return ("Made stale: " + ", ".join(parts) + ".") if parts else ""
