"""Stack traces and GameTest results in a log, mapped onto the code (docs/DESIGN.md D53).

``verinoda trace-log FILE``: every stack trace of a Minecraft / JVM log (an exception or a ``Thread.dumpStack`` /
``new Throwable().printStackTrace()`` after a marker line such as ``[GUARDREMOVE] DISCARDED ...``) becomes a list of
frames. A frame of the project is mapped to its graph node (the class by its package path, the method by name and
line) with its callers and callees; frames outside the project are folded into one line that names the first and
the last of them and the ones that matter (a GameTest's ``succeed`` / ``fail``, an event dispatch). GameTest result
lines (``... passed``, ``... failed``) are matched to the ``@GameTest`` methods, and a trace that runs through
``GameTestInfo.succeed`` / ``fail`` or ``GameTestHelper.succeed`` is tied to the test that finished at that moment:
the test whose result line is nearest to the trace, or whose method is on the stack.

Read-only on the log. What it says is an observation of a run Verinoda did not make: :func:`store` records it as a
claim with the log lines as evidence, at the status the claim rules allow for such evidence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_FRAME = re.compile(r"^\s*at\s+(?:[\w.$@-]+/{1,2})*(?P<cls>[\w$.]+)\.(?P<meth>[\w$<>]+)\((?P<src>[^)]*)\)")
_EXC = re.compile(r"^(?:Exception in thread \"[^\"]*\"\s+)?(?P<exc>(?:[a-z][\w$]*\.)+[A-Z][\w$]*(?:Exception|Error|"
                  r"Throwable)?)(?::\s*(?P<msg>.*))?$")
_CAUSED = re.compile(r"^\s*Caused by:\s*(?P<rest>.*)$")
_MORE = re.compile(r"^\s*\.\.\.\s*(\d+)\s+more")
_STAMP = re.compile(r"^\[(?P<t>[\d:.]+)\]")
_RESULT = re.compile(r"(?P<name>[\w:./$-]+)\s+(?:has\s+)?(?P<how>passed|failed|succeeded|timed out)\b", re.I)
_NOTABLE = re.compile(r"GameTestInfo\.(?:succeed|fail)|GameTestHelper\.(?:succeed|fail|onEachTick|runAfterDelay)"
                      r"|GameTestTicker|ServerTickEvents|EventFactory|Entity\.(?:discard|remove|kill|setRemoved)"
                      r"|ServerLevel\.tick|MinecraftServer\.tick")


@dataclass
class Frame:
    cls: str
    meth: str
    file: str | None
    line: int | None
    log_line: int
    node: str | None = None
    project: bool = False


@dataclass
class Trace:
    header: str
    header_line: int
    marker: str | None                     # the log line just before a bare `java.lang.Throwable`
    frames: list[Frame] = field(default_factory=list)
    stamp: str | None = None


def parse(text: str) -> tuple[list[Trace], list[dict]]:
    """``(traces, test results)`` of a log's text."""
    lines = text.splitlines()
    traces: list[Trace] = []
    results: list[dict] = []
    cur: Trace | None = None
    last_stamp = None
    for i, raw in enumerate(lines, 1):
        line = raw.rstrip()
        st = _STAMP.match(line)
        if st:
            last_stamp = st.group("t")
        fm = _FRAME.match(line)
        if fm:
            if cur is None:  # frames with no header: the previous line was the header
                prev = lines[i - 2].strip() if i >= 2 else ""
                cur = Trace(prev or "(no header)", i - 1, None, stamp=last_stamp)
                traces.append(cur)
            src = fm.group("src")
            f, _, ln = src.partition(":")
            cur.frames.append(Frame(fm.group("cls"), fm.group("meth"), f or None,
                                    int(ln) if ln.isdigit() else None, i))
            continue
        if _MORE.match(line) or _CAUSED.match(line):
            continue
        body = re.sub(r"^\[[^\]]*\]\s*(?:\[[^\]]*\]\s*)*(?:\([^)]*\)\s*)?:?\s*", "", line).strip()
        ex = _EXC.match(body)
        if ex and (i < len(lines) and _FRAME.match(lines[i])):
            marker = None
            if ex.group("exc").endswith("Throwable") and i >= 2:
                marker = re.sub(r"^\[[^\]]*\]\s*(?:\[[^\]]*\]\s*)*(?:\([^)]*\)\s*)?:?\s*", "",
                                lines[i - 2]).strip() or None
            cur = Trace(body, i, marker, stamp=last_stamp)
            traces.append(cur)
            continue
        cur = None
        rm = _RESULT.search(body)
        if rm and not body.lower().startswith(("at ", "caused")):
            results.append({"line": i, "name": rm.group("name"), "outcome": rm.group("how").lower(),
                            "stamp": last_stamp, "text": body[:200]})
    return traces, results


def _class_file(g, fqn: str) -> str | None:
    """The project file of a class by its package path (``a.b.C$Inner`` -> ``.../a/b/C.java``)."""
    top = fqn.split("$", 1)[0]
    tail = top.replace(".", "/")
    best = None
    for n, d in g.G.nodes(data=True):
        sf = str(d.get("source_file") or "")
        if sf.endswith((f"/{tail}.java", f"/{tail}.kt")) or sf in (f"{tail}.java", f"{tail}.kt"):
            best = sf
            break
    return best


def map_frames(g, traces: list[Trace]) -> None:
    """Fill each project frame's node: the method of that name in the class's file whose span holds the line."""
    files: dict[str, str | None] = {}
    by_file: dict[str, list[str]] = {}
    for n, d in g.G.nodes(data=True):
        sf = d.get("source_file")
        if sf and d.get("_callable"):
            by_file.setdefault(sf, []).append(n)
    for t in traces:
        for fr in t.frames:
            top = fr.cls.split("$", 1)[0]
            if top not in files:
                files[top] = _class_file(g, top)
            f = files[top]
            if not f:
                continue
            fr.project = True
            name = fr.meth
            cands = [n for n in by_file.get(f, []) if g.label(n).strip(".()").split(".")[-1] == name]
            if fr.line:
                spanned = [n for n in cands if (sp := g.span(n)) and sp[0] <= fr.line <= sp[1]]
                cands = spanned or cands
            if not cands and fr.line and name.startswith("lambda$"):  # lambda$tik$3: the method around the line
                cands = [n for n in by_file.get(f, []) if (sp := g.span(n)) and sp[0] <= fr.line <= sp[1]]
            fr.node = cands[0] if cands else None


def gametests(g) -> dict[str, str]:
    """Lower-case test method name (and ``class.method``) -> node of every ``@GameTest`` method."""
    from verinoda import gametests as gt

    out: dict[str, str] = {}
    for cls, e in gt.gametest_classes(g).items():
        cname = g.label(cls).strip().lower()
        for u in e["tests"]:
            m = u.name.strip(".()").split(".")[-1].lower()
            out.setdefault(m, u.node)
            out[f"{cname}.{m}"] = u.node
    return out


def result_test(g, name: str, tests: dict[str, str]) -> str | None:
    key = name.lower().split(":")[-1]
    parts = re.split(r"[./]", key)
    for cand in (key, ".".join(parts[-2:]), parts[-1], parts[-1].replace("_", "")):
        if cand in tests:
            return tests[cand]
    return None


_GT_FRAME = re.compile(r"GameTest(?:Info|Helper)\.(?:succeed|fail)|GameTestInfo\.lambda\$(?:succeed|fail)")


def analyze(g, text: str, *, source: str) -> dict:
    """The report of :func:`parse` over ``text`` (the log at ``source``) mapped onto ``g``."""
    traces, results = parse(text)
    map_frames(g, traces)
    tests = gametests(g)

    def at(n: str | None) -> str | None:
        return f"{g.file(n)}:{g.line(n)}" if n else None

    def name(n: str) -> str:
        own = next((u for u, _d in g.in_edges(n, {"method"})), None)
        base = g.label(n).strip(".()")
        return f"{g.label(own).strip()}.{base}" if own else base

    rows = []
    for r in results:
        n = result_test(g, r["name"], tests)
        rows.append({**r, **({"test": name(n), "test_at": at(n)} if n else {})})
    out = []
    for t in traces:
        frames, folded = [], []

        def flush() -> None:
            if folded:
                notable = [x for x in folded if _NOTABLE.search(x)]
                frames.append({"folded": len(folded), "first": folded[0], "last": folded[-1],
                               "notable": list(dict.fromkeys(notable))[:4]})
                folded.clear()

        for fr in t.frames:
            short = f"{fr.cls.rsplit('.', 1)[-1]}.{fr.meth}"
            if fr.project:
                flush()
                row = {"frame": short, "log_line": fr.log_line, "source": f"{fr.file}:{fr.line}" if fr.line else fr.file}
                if fr.node:
                    row.update(node=name(fr.node), at=at(fr.node),
                               callers=[name(u) for u, _d in g.in_edges(fr.node, {"calls", "registers"})][:3],
                               callees=[name(v) for v, _d in g.out_edges(fr.node, {"calls"})][:3])
                frames.append(row)
            else:
                folded.append(short)
        flush()
        entry = {"header": t.header, "log_line": t.header_line, **({"marker": t.marker} if t.marker else {}),
                 **({"stamp": t.stamp} if t.stamp else {}), "frames": frames}
        gts = [fr for fr in t.frames if _GT_FRAME.search(f"{fr.cls.rsplit('.', 1)[-1]}.{fr.meth}")]
        gt = next((fr for fr in gts if not fr.meth.startswith("lambda$")), gts[0] if gts else None)
        if gt is not None:
            on_stack = next((fr for fr in t.frames if fr.node and fr.node in tests.values()), None)
            if on_stack is not None:
                entry["gametest"] = {"via": f"{gt.cls.rsplit('.', 1)[-1]}.{gt.meth}", "test": name(on_stack.node),
                                     "test_at": at(on_stack.node), "basis": "the test's method is on the stack"}
            else:
                near = sorted((r for r in rows if r.get("test")),
                              key=lambda r: (r.get("stamp") != t.stamp, abs(r["line"] - t.header_line)))
                if near:
                    r = near[0]
                    entry["gametest"] = {"via": f"{gt.cls.rsplit('.', 1)[-1]}.{gt.meth}", "test": r["test"],
                                         "test_at": r["test_at"], "result": f"{r['outcome']} (log line {r['line']})",
                                         "basis": "the test whose result line is nearest"
                                                  + (" at the same time" if r.get("stamp") == t.stamp else "")}
        out.append(entry)
    return {"source": source, "traces": out, "results": rows,
            "note": "frames outside the project are folded; a run Verinoda did not make observes, it does not "
                    "verify"}


def render(res: dict) -> str:
    out = [f"{res['source']}: {len(res['traces'])} stack trace(s), {len(res['results'])} test result line(s)"]
    for t in res["traces"]:
        out.append("")
        out.append(f"line {t['log_line']}: {t.get('marker') or t['header']}" + (f"  ({t['header']})" if t.get("marker")
                                                                                 else ""))
        gt = t.get("gametest")
        if gt:
            out.append(f"  via {gt['via']}: the test {gt['test']} ({gt['test_at']})"
                       + (f", {gt['result']}" if gt.get("result") else "") + f" - {gt['basis']}")
        for fr in t["frames"]:
            if "folded" in fr:
                note = f"; {', '.join(fr['notable'])}" if fr["notable"] else ""
                out.append(f"    ... {fr['folded']} frame(s) outside the project: {fr['first']} .. {fr['last']}{note}")
            elif fr.get("node"):
                nb = ("; called by " + ", ".join(fr["callers"])) if fr.get("callers") else ""
                out.append(f"    {fr['node']} ({fr['at']}, line {fr['source'].rsplit(':', 1)[-1]} in the trace){nb}")
            else:
                out.append(f"    {fr['frame']} ({fr['source']}) - not in the index")
    tested = [r for r in res["results"] if r.get("test")]
    if tested:
        out.append("")
        out.append("test results: " + "; ".join(f"{r['test']} {r['outcome']} (line {r['line']})" for r in tested[:12]))
    return "\n".join(out)


def _log_in_repo(repo: Path, log: Path) -> tuple[str, list[str]]:
    """The log's path inside the repository (a log outside it is copied to ``.verinoda/logs/`` so its lines can be
    cited and re-checked) and its lines."""
    import hashlib

    data = log.read_bytes()
    try:
        return log.resolve().relative_to(repo.resolve()).as_posix(), data.decode("utf-8", "replace").splitlines()
    except ValueError:
        pass
    from verinoda.paths import atlas_dir

    dest = atlas_dir(repo) / "logs" / f"{log.stem}-{hashlib.sha256(data).hexdigest()[:10]}{log.suffix or '.log'}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        dest.write_bytes(data)
    return dest.relative_to(repo).as_posix(), data.decode("utf-8", "replace").splitlines()


def store(st, repo: Path, log: Path, res: dict) -> list[dict]:
    """One claim per trace tied to a test, and one per test result line mapped to a test: the log lines are the
    evidence. A log Verinoda did not produce observes a run; the claim gets the status the rules allow for it."""
    import hashlib

    from verinoda import workflow
    from verinoda.claims import Claims

    snap, _refresh = workflow._current_snapshot(st, repo)
    if snap is None:
        return []
    rel, lines = _log_in_repo(Path(repo), log)
    cl = Claims(st, repo)
    made = []
    for t in res["traces"]:
        gt = t.get("gametest")
        top = next((f for f in t["frames"] if f.get("node")), None)
        if not gt and not top:
            continue
        last = max([t["log_line"]] + [f.get("log_line") or 0 for f in t["frames"]])
        a_ln = max(1, t["log_line"] - (1 if t.get("marker") else 0))
        b_ln = min(len(lines), last)
        excerpt = "\n".join(lines[a_ln - 1:b_ln])
        if gt and gt.get("result"):
            rl = int(re.search(r"line (\d+)", gt["result"]).group(1))
            excerpt += "\n...\n" + (lines[rl - 1] if 0 < rl <= len(lines) else "")
        ev = {"source_type": "agent_report", "locator": f"log {log.name} lines {a_ln}-{b_ln}", "path": None,
              "commit_sha": None, "content_hash": "sha256:" + hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
              "excerpt": excerpt[:400],
              "meta": {"run_by": "user", "log": str(log), "kept_at": rel, "lines": [a_ln, b_ln],
                       "scope": "a log Verinoda did not produce: it observes a run and never verifies a claim"}}
        what = t.get("marker") or t["header"]
        text = (f"The log {log.name} (line {t['log_line']}) shows `{what[:80]}` "
                + (f"reached through `{gt['via']}` while the test `{gt['test']}` finished" if gt else
                   f"inside `{top['node']}`"))
        c = cl.create(text, project=snap["project"], snapshot=snap, status="observed", evidence=[(ev, "supports")],
                      subjects=[x for x in (gt.get("test_at") if gt else None, top.get("at") if top else None) if x],
                      kind="general", spec={"free_text": True, "source": "trace-log", "log": rel}, actor="verinoda")
        made.append({"id": c["id"], "status": c["status"], "text": text})
    return made
