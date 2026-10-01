"""Sentry events and OpenTelemetry spans read from an exported file, their frames mapped onto the code.

``verinoda trace-log FILE`` takes a JSON export as well as a log: a Sentry event (``exception.values[].stacktrace
.frames`` with ``filename`` / ``abs_path`` / ``lineno`` / ``function`` / ``module`` / ``in_app``, or the API's
``entries`` form), a list of them, JSON lines, or an OpenTelemetry trace in OTLP JSON (``resourceSpans`` /
``scopeSpans`` / ``spans``: the ``code.*`` attributes of a span and the ``exception.stacktrace`` text of its
exception events, read for Python, JVM and Node frames). Only the local file is read; nothing is fetched.

Each frame is mapped onto the repository: its path by whole-suffix matching over the indexed files
(:meth:`verinoda.failsig.PathResolver.matches`; several files with the same suffix are named, not picked), then its
line is checked against the current file (beyond the end, or a recorded ``context_line`` that differs) and against
the definitions the index puts around that line (the frame's function must be one of them). A frame that passes is
an ``observed`` claim scoped to that event - what that event recorded, not what always happens; the others are
reported with the reason they did not map (stale, ambiguous, not in the repository). Library frames (``in_app:
false``, a ``site-packages`` / ``node_modules`` path) are folded into one count.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from verinoda import failsig

MAX_BYTES = 32 * 1024 * 1024          # a bigger file is refused, not read
MAX_EVENTS = 200
MAX_FRAMES = 200                      # per event, the innermost kept
MAX_CLAIMS = 50                       # stored claims per run
MAX_STACK_LINES = 5000                # of one exception.stacktrace text
JSON_SUFFIXES = (".json", ".jsonl", ".ndjson")
SCOPE = "what that event recorded, not what always happens"
_UNNAMED = {"", "?", "<anonymous>", "anonymous", "<lambda>", "<listcomp>", "<genexpr>", "<dictcomp>", "<setcomp>",
            "<clinit>", "<unknown>"}


def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):  # a UTF-16 file (PowerShell's redirect writes one)
        return data.decode("utf-16", "replace").lstrip("﻿")
    return data.decode("utf-8-sig", "replace")


def load(data: bytes, *, name: str) -> list | None:
    """The JSON documents of an export (one, or one per line), or None when the file is not JSON (a log).

    Raises ``ValueError`` for a file meant to be JSON (a ``.json`` name, or text starting with ``{``) that does not
    parse; a ``[``-led text that does not parse is a log (``[14:02:08] [Server thread/INFO] ...``)."""
    meant = name.lower().endswith(JSON_SUFFIXES)
    text = _decode(data)
    s = text.lstrip()
    if not s or s[0] not in "{[":
        if meant:
            raise ValueError("not JSON: it does not start with { or [")
        return None
    try:
        return [json.loads(s)]
    except RecursionError:
        raise ValueError("not read: the JSON is nested too deeply") from None
    except ValueError as e:
        first = e
    docs: list = []
    for ln in text.splitlines():
        if not ln.strip():
            continue
        try:
            docs.append(json.loads(ln))
        except (ValueError, RecursionError):
            docs = []
            break
    if docs:
        return docs
    if meant or s[0] == "{":
        where = (f"line {first.lineno}, column {first.colno}: {first.msg}"
                 if isinstance(first, json.JSONDecodeError) else str(first))
        raise ValueError(f"not valid JSON ({where})")
    return None


# -- reading the export -----------------------------------------------------------------------------------------------

def _int(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def _str(v) -> str | None:
    return v if isinstance(v, str) and v else None


def _frame(ref: str, path, alt, line, fn, module=None, in_app=None, context=None, built: str | None = None) -> dict:
    return {"ref": ref, "path": _str(path), "alt": _str(alt), "line": _int(line), "fn": _str(fn),
            "module": _str(module), "in_app": in_app if isinstance(in_app, bool) else None,
            "context": _str(context), **({"built": built} if built else {})}


def _java_path(cls: str, file_name: str | None) -> str:
    """``pkg.sub.Outer$Inner`` + ``Outer.java`` -> ``pkg/sub/Outer.java`` (a suffix to match)."""
    pkg = cls.rsplit(".", 1)[0] if "." in cls else ""
    top = cls.rsplit(".", 1)[-1].split("$")[0]
    return (pkg.replace(".", "/") + "/" if pkg else "") + (file_name or f"{top}.java")


def _sentry_values(ev: dict) -> list[dict]:
    exc = ev.get("exception")
    vals = exc.get("values") if isinstance(exc, dict) else exc
    if not isinstance(vals, list):
        vals = None
        for ent in ev.get("entries") if isinstance(ev.get("entries"), list) else []:
            if isinstance(ent, dict) and ent.get("type") == "exception" and isinstance(ent.get("data"), dict):
                vals = ent["data"].get("values")
                break
    return [v for v in vals if isinstance(v, dict)] if isinstance(vals, list) else []


def _sentry(ev: dict, where: str) -> dict | None:
    vals = _sentry_values(ev)
    if not vals:
        return None
    frames: list[dict] = []
    for i, v in enumerate(vals):
        st = v.get("stacktrace") if isinstance(v.get("stacktrace"), dict) else v.get("raw_stacktrace")
        fl = st.get("frames") if isinstance(st, dict) else None
        for j, f in enumerate(fl if isinstance(fl, list) else []):
            if not isinstance(f, dict):
                continue
            path, alt, mod = f.get("abs_path"), f.get("filename"), _str(f.get("module"))
            built = None
            fname = _str(alt) or ""
            if mod and "." in mod and "/" not in fname and "\\" not in fname and \
                    fname.endswith((".java", ".kt", ".scala", ".groovy")):  # a JVM frame names its class
                path, built = _java_path(mod, fname), "the class's package"
            frames.append(_frame(f"x{i}.f{j}", path, alt, f.get("lineno"), f.get("function"), mod, f.get("in_app"),
                                 f.get("context_line"), built))
    last = vals[-1]
    title = ": ".join(x for x in (_str(last.get("type")), _str(last.get("value"))) if x) or "(no exception type)"
    eid = next((x for x in (ev.get("event_id"), ev.get("eventID"), ev.get("id")) if _str(x)), None)
    return {"kind": "sentry", "id": eid or where, "title": title[:200], "where": where,
            **({"truncated": True} if len(frames) > MAX_FRAMES else {}), "frames": frames[-MAX_FRAMES:]}


def _attrs(lst) -> dict:
    out = {}
    for a in lst if isinstance(lst, list) else []:
        if not isinstance(a, dict) or not isinstance(a.get("key"), str):
            continue
        v = a.get("value")
        if isinstance(v, dict):
            v = next((v[k] for k in ("stringValue", "intValue", "doubleValue", "boolValue") if k in v), None)
        out[a["key"]] = v
    return out


def _text_frames(text: str, ref: str) -> list[dict]:
    """Python, JVM and Node frames of a stack trace's text, innermost last."""
    py, jv, nd = [], [], []
    for ln in text.splitlines()[:MAX_STACK_LINES]:
        m = failsig._TB_FILE.match(ln)
        if m:
            py.append((m.group(1), int(m.group(2)), m.group(3), None, None))
            continue
        m = failsig._J_FRAME.match(ln)
        if m:
            cls, meth = m.group(1).rsplit("/", 1)[-1], m.group(2)
            fm = re.match(r"^([\w$]+\.\w+):(\d+)$", m.group(3))
            if fm:
                jv.append((_java_path(cls, fm.group(1)), int(fm.group(2)), meth, cls, "the class's package"))
            continue
        m = failsig._N_FRAME.match(ln)
        if m and ("at " in ln or " (" in ln):
            nd.append((m.group(2), int(m.group(3)), m.group(1), None, None))
    rows = py + jv[::-1] + nd[::-1]
    return [_frame(f"{ref}.f{j}", p, None, ln, fn, mod, None, None, built)
            for j, (p, ln, fn, mod, built) in enumerate(rows)]


def _otel(doc: dict, where: str) -> list[dict]:
    out = []
    for r, rs in enumerate(doc.get("resourceSpans") if isinstance(doc.get("resourceSpans"), list) else []):
        if not isinstance(rs, dict):
            continue
        scopes = rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans")
        for s, ss in enumerate(scopes if isinstance(scopes, list) else []):
            spans = ss.get("spans") if isinstance(ss, dict) else None
            for k, sp in enumerate(spans if isinstance(spans, list) else []):
                if not isinstance(sp, dict):
                    continue
                a = _attrs(sp.get("attributes"))
                frames = []
                path = a.get("code.filepath") or a.get("code.file.path")
                line = a.get("code.lineno") if a.get("code.lineno") is not None else a.get("code.line.number")
                fn = a.get("code.function") or a.get("code.function.name")
                ns = _str(a.get("code.namespace"))
                built = None
                if not _str(path) and ns:
                    top = ns.rsplit(".", 1)[-1]
                    path, built = ((_java_path(ns, None), "code.namespace as a JVM class") if top[:1].isupper()
                                   else (ns.replace(".", "/") + ".py", "code.namespace as a Python module"))
                if _str(path) or _str(fn):
                    frames.append(_frame("code", path, None, line, fn, ns, None, None, built))
                title = _str(sp.get("name")) or "(unnamed span)"
                for e, evt in enumerate(sp.get("events") if isinstance(sp.get("events"), list) else []):
                    if not isinstance(evt, dict) or evt.get("name") != "exception":
                        continue
                    ea = _attrs(evt.get("attributes"))
                    exc = ": ".join(x for x in (_str(ea.get("exception.type")), _str(ea.get("exception.message")))
                                    if x)
                    title = f"{title} - {exc}" if exc else title
                    stack = ea.get("exception.stacktrace")
                    if isinstance(stack, str):
                        frames += _text_frames(stack, f"ev{e}")
                if not frames:
                    continue
                sid = _str(sp.get("spanId")) or f"{where}.rs{r}.ss{s}.sp{k}"
                out.append({"kind": "otel", "id": sid, "title": title[:200], "where": f"{where}.rs{r}.ss{s}.sp{k}",
                            **({"trace": sp["traceId"]} if _str(sp.get("traceId")) else {}),
                            **({"truncated": True} if len(frames) > MAX_FRAMES else {}),
                            "frames": frames[-MAX_FRAMES:]})
    return out


def events(docs: list) -> tuple[list[dict], bool]:
    """``(events, truncated)``: every Sentry event and OpenTelemetry span with frames in the documents."""
    out: list[dict] = []

    def walk(o, where: str, depth: int) -> None:
        if len(out) > MAX_EVENTS or depth > 4:
            return
        if isinstance(o, list):
            for i, x in enumerate(o):
                if len(out) > MAX_EVENTS:
                    return
                walk(x, f"{where}[{i}]", depth + 1)
            return
        if not isinstance(o, dict):
            return
        if "resourceSpans" in o:
            out.extend(_otel(o, where))
            return
        ev = _sentry(o, where) if ("exception" in o or "entries" in o) else None
        if ev is not None:
            out.append(ev)
            return
        for k in ("data", "events", "results", "items"):
            if isinstance(o.get(k), list):
                walk(o[k], f"{where}.{k}", depth + 1)

    for i, d in enumerate(docs):
        walk(d, f"doc{i}" if len(docs) > 1 else "doc", 0)
    return out[:MAX_EVENTS], len(out) > MAX_EVENTS


# -- mapping onto the repository --------------------------------------------------------------------------------------

def _short(fn: str | None) -> str | None:
    """The bare name a frame's function compares by (``Object.handle`` -> ``handle``, ``lambda$tick$2`` -> ``tick``,
    ``<init>`` kept); None when the frame has no name worth checking (``<anonymous>``, ``?``)."""
    if not fn:
        return None
    s = fn.strip()
    m = re.match(r"^lambda\$([\w]+?)\$\d+$", s)
    if m:
        s = m.group(1)
    s = re.sub(r"\(.*\)$", "", s)
    s = re.sub(r"(?:\.func\d+)+$", "", s)
    last = re.split(r"[.:#/]+", s)[-1].strip("()*") if s not in ("<module>", "<init>") else s
    if last in _UNNAMED or (last.startswith("<") and last not in ("<module>", "<init>")):
        return None
    return last


class Mapper:
    """Frames onto the repository as it is now: the index's files and definitions, the files' current lines."""

    def __init__(self, repo: Path, g):
        self.repo, self.g = Path(repo), g
        files = {str(d.get("source_file")).replace("\\", "/") for _n, d in g.G.nodes(data=True)
                 if d.get("source_file")}
        self.resolver = failsig.PathResolver(files, roots=[str(self.repo)])
        self._lines: dict[str, list[str] | None] = {}

    def lines(self, rel: str) -> list[str] | None:
        if rel not in self._lines:
            try:
                data = (self.repo / rel).read_bytes()
            except OSError:
                data = None
            self._lines[rel] = None if data is None else _decode(data).splitlines()
        return self._lines[rel]

    def chain(self, rel: str, line: int) -> list[str]:
        """The definitions around ``line``, outermost first."""
        g = self.g
        out = [(sp, n) for n in g.symbols_in(rel) if (sp := g.span(n)) and sp[0] <= line <= sp[1]]
        return [n for _sp, n in sorted(out, key=lambda x: (x[0][0], -x[0][1]))]

    def name(self, n: str) -> str:
        return self.g.label(n).strip().strip(".()").split(".")[-1].split("(")[0]

    def check(self, rel: str, fr: dict) -> tuple[str, str, list[str]]:
        """``(result, why, chain)`` of a frame whose file is ``rel``: ``mapped`` or ``stale``."""
        line, want = fr["line"], _short(fr["fn"])
        cur = self.lines(rel)
        if cur is None:
            return "stale", "file missing: the index names it but it is not on disk", []
        if line is None:
            return "stale", "no line recorded", []
        if not 1 <= line <= len(cur):
            return "stale", f"line {line} is beyond the end of the file ({len(cur)} lines)", []
        if fr["context"] is not None and fr["context"].strip() != cur[line - 1].strip():
            return "stale", f"the recorded source line differs from line {line} now", []
        chain = self.chain(rel, line)
        names = [self.name(n) for n in chain]
        if want is None or (want == "<module>" and not chain):
            return "mapped", "", chain
        if want == "<init>":  # a JVM constructor: any definition of the class around the line
            return ("mapped", "", chain) if chain else ("stale", "a constructor frame at module level", chain)
        if want in names:
            return "mapped", "", chain
        if not self.g.symbols_in(rel):
            return "mapped", "name not checked: the index has no definitions in this file", chain
        where = f"inside `{'.'.join(names)}`" if names else "at module level"
        return "stale", f"function name not at that line: line {line} is {where}, not in `{want}`", chain

    def map_frame(self, fr: dict) -> dict:
        raw = fr["path"] or fr["alt"]
        row = {"ref": fr["ref"], **({"fn": fr["fn"]} if fr["fn"] else {}),
               "recorded": f"{raw}:{fr['line']}" if raw and fr["line"] is not None else (raw or "(no file)")}
        if fr.get("built"):
            row["path_from"] = fr["built"]
        low = (raw or "").replace("\\", "/").lower()
        if fr["in_app"] is False or any(f in low for f in failsig._FOREIGN):
            return {**row, "result": "library"}
        cands = self.resolver.matches(fr["path"]) if fr["path"] else []
        if not cands and fr["alt"]:
            cands = self.resolver.matches(fr["alt"])
        if not cands:
            if fr["in_app"] is True:
                return {**row, "result": "not_in_repo", "why": "file missing: no indexed file ends with that path"}
            return {**row, "result": "library", "why": "no indexed file ends with that path"}
        if len(cands) > 1:
            fits = [c for c in cands if self.check(c, fr)[0] == "mapped"]
            return {**row, "result": "ambiguous", "why": f"{len(cands)} files end with that path; none is chosen",
                    "candidates": cands[:5], **({"fits": fits[:5]} if fits else {})}
        rel = cands[0]
        res, why, chain = self.check(rel, fr)
        row.update(path=rel, line=fr["line"], result=res)
        if chain:
            row["in"] = ".".join(self.name(n) for n in chain)
            row["def_at"] = f"{rel}:{self.g.line(chain[-1])}"
        if why:
            row["why"] = why
        if res == "mapped":
            row["status"] = "observed"
        return row


def analyze(repo: Path, g, docs: list, *, source: str) -> dict | None:
    """The frames of every event in ``docs`` mapped onto the repository; None when there is no event at all."""
    evs, truncated = events(docs)
    if not evs:
        return None
    m = Mapper(repo, g)
    counts = {"mapped": 0, "stale": 0, "ambiguous": 0, "not_in_repo": 0, "library": 0}
    out = []
    for ev in evs:
        rows, lib = [], []
        for fr in ev["frames"]:
            r = m.map_frame(fr)
            counts[r["result"]] += 1
            if r["result"] == "library":
                lib.append(fr["fn"] or r["recorded"])
                continue
            rows.append(r)
        out.append({k: v for k, v in ev.items() if k != "frames"} | {"frames": rows,
                   **({"library": {"frames": len(lib), "first": lib[0], "last": lib[-1]}} if lib else {})})
    kinds = {k: sum(1 for e in evs if e["kind"] == k) for k in ("sentry", "otel")}
    return {"source": source, "format": "+".join(k for k, v in kinds.items() if v), "events": out, "counts": counts,
            **({"truncated": True} if truncated else {}), "scope": SCOPE,
            "note": "read from a local export: each mapped frame is observed for its event only; a stale frame's "
                    "file or line no longer matches the code"}


def render(res: dict) -> str:
    c = res["counts"]
    out = [f"{res['source']}: {len(res['events'])} event(s) ({res['format']}); frames: {c['mapped']} mapped, "
           f"{c['stale']} stale, {c['ambiguous']} ambiguous, {c['not_in_repo']} not in the repository, "
           f"{c['library']} outside it" + ("; events cut at the limit" if res.get("truncated") else "")]
    for e in res["events"]:
        out.append("")
        out.append(f"{'Sentry event' if e['kind'] == 'sentry' else 'span'} {e['id']}: {e['title']}"
                   + (f" (the innermost {MAX_FRAMES} frames read)" if e.get("truncated") else ""))
        for r in e["frames"]:
            if r["result"] == "mapped":
                out.append(f"    observed  {r['path']}:{r['line']}" + (f" in {r['in']}" if r.get("in") else "")
                           + f" ({r['ref']})" + (f" - {r['why']}" if r.get("why") else ""))
            elif r["result"] == "ambiguous":
                out.append(f"    ambiguous {r['recorded']} - {r['why']}: {', '.join(r['candidates'])}"
                           + (f" (the name fits {', '.join(r['fits'])})" if r.get("fits") else ""))
            else:
                where = f"{r['path']}:{r['line']}" if r.get("path") else r["recorded"]
                out.append(f"    {r['result']:<9} {where} - {r['why']}")
        lib = e.get("library")
        if lib:
            out.append(f"    ... {lib['frames']} frame(s) outside the repository: {lib['first']} .. {lib['last']}")
    out.append("")
    out.append(f"scope: {res['scope']}")
    return "\n".join(out)


def store(st, repo: Path, export: Path, res: dict) -> list[dict]:
    """One claim per mapped frame (at most :data:`MAX_CLAIMS`): the frames read from the export, kept under
    ``.verinoda/logs/``, are the evidence. Verinoda did not record the event; the claim gets the status the rules
    allow for that."""
    import hashlib

    from verinoda import workflow
    from verinoda.claims import Claims
    from verinoda.paths import atlas_dir
    from verinoda.scrub import redact

    repo = Path(repo)
    snap, _refresh = workflow._current_snapshot(st, repo)
    if snap is None:
        return []
    # what is kept to be cited is the frames as read, not the export: an event also carries request headers, the
    # user and local variables
    kept = redact(json.dumps({"export": export.name, "events": res["events"]}, indent=1, sort_keys=True,
                             ensure_ascii=False)) + "\n"
    data = kept.encode("utf-8", "replace")
    dest = atlas_dir(repo) / "logs" / f"{export.stem}-{hashlib.sha256(data).hexdigest()[:10]}.frames.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        dest.write_bytes(data)
    rel = dest.relative_to(repo).as_posix()
    cl = Claims(st, repo)
    made = []
    for e in res["events"]:
        what = "Sentry event" if e["kind"] == "sentry" else "OpenTelemetry span"
        for r in e["frames"]:
            if r["result"] != "mapped":
                continue
            if len(made) >= MAX_CLAIMS:
                return made
            excerpt = redact(json.dumps({k: r[k] for k in ("ref", "fn", "recorded") if r.get(k)}, sort_keys=True))
            ev = {"source_type": "agent_report", "locator": f"export {export.name} {e['where']} {r['ref']}",
                  "path": None, "commit_sha": None,
                  "content_hash": "sha256:" + hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
                  "excerpt": excerpt[:400],
                  "meta": {"run_by": "user", "export": str(export), "kept_at": rel, "event": e["id"],
                           "scope": "an export Verinoda did not produce: it observes one event, never verifies"}}
            text = (f"The {what} {str(e['id'])[:32]} ({redact(e['title'])[:60]}) recorded a frame at "
                    f"`{r['path']}:{r['line']}`" + (f" in `{r['in']}`" if r.get("in") else "") + f": {SCOPE}")
            c = cl.create(text, project=snap["project"], snapshot=snap, status="observed",
                          evidence=[(ev, "supports")], subjects=[f"{r['path']}:{r['line']}"], kind="general",
                          spec={"free_text": True, "source": "trace-import", "export": rel, "event": e["id"]},
                          actor="verinoda")
            made.append({"id": c["id"], "status": c["status"], "text": text})
    return made
