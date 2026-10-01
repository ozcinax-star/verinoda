"""What a Mixin really changed in its target, read from Mixin's debug export.

Started with ``-Dmixin.debug.export=true``, SpongePowered Mixin writes every class it transformed to
``.mixin.out/class/<internal name>.class`` in the game's working folder (``run/`` for a Loom or ForgeGradle run).
That file is the class as the game ran it, after every Mixin of every mod: a method a Mixin merged carries
``@MixinMerged(mixin = "pkg.SomeMixin", priority = 1000, sessionId = ...)``; an injector's handler is merged
under a new name (``handler$zza000$onTick``, ``redirect$...``, ``modify$...``, ``localvar$...``, with Fabric the
mod id inside: ``handler$zza000$mymod$onTick``), and the target method's bytecode calls it where the injection
landed; an ``@Overwrite`` keeps the method's name and gets the annotation. With ``-Dmixin.checks.interfaces=true``
Mixin also writes ``.mixin.out/audit/mixin_implementation_report.csv``: interface methods a class lacks after its
Mixins were applied.

Here those files are read (:func:`verinoda.jvmclass.class_code` and :func:`verinoda.jvmclass.class_annotations`,
nothing run) and compared with the target's class file on the build's classpath (the one ``mixin-check`` already
reads): methods, fields and interfaces the export has and the original does not, methods a Mixin replaced, and the
calls a method of the original gained or lost. Each of the project's injectors gets a row:

- ``applied`` (``observed``): its handler is in the export with ``@MixinMerged`` naming this Mixin, and the
  methods whose bytecode calls it are named; for an ``@Overwrite``, the method carries the annotation.
- ``merged`` (``observed``): the handler is there but no method whose code this reader walked calls it.
- ``not_applied`` (``strong_inference``): the target class is in the export but holds no handler of this injector
  (none of this Mixin at all, or not this one): the run that wrote the export did not apply it. The export may
  be older than the source, so it is not proven for the current code; ``unknown`` when the Mixin's source file
  is newer than the exported class.
- ``unknown``: the export holds no class file for the target (the class was not loaded in that run), with the
  next step. No export at all is one ``unknown`` for the whole check, with how to turn it on; the rows are then
  not written (it is opt-in).
"""
from __future__ import annotations

import csv
import io
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from verinoda import jvmclass

EXPORT_DIR = ".mixin.out"
MERGED = "Lorg/spongepowered/asm/mixin/transformer/meta/MixinMerged;"
AUDIT_CSV = "mixin_implementation_report.csv"
MAX_CLASS = 16 << 20            # an exported class file read
MAX_AUDIT_ROWS = 200
MAX_DIFF = 10                   # calls gained or lost listed per method
MAX_CHANGED = 200               # methods of the original with other calls listed per class
# the prefixes Mixin and MixinExtras give a merged injector handler: prefix$<unique id>$[mod id$]name
HANDLER_PREFIXES = {
    "handler": "Inject", "redirect": "Redirect", "modify": "ModifyArg", "args": "ModifyArgs",
    "localvar": "ModifyVariable", "constant": "ModifyConstant",
    "wrapOperation": "WrapOperation", "wrapWithCondition": "WrapWithCondition",
    "modifyExpressionValue": "ModifyExpressionValue", "modifyReturnValue": "ModifyReturnValue",
    "modifyReceiver": "ModifyReceiver", "wrapMethod": "WrapMethod",
}
NEXT_EXPORT = ("add -Dmixin.debug.export=true to the JVM arguments of the run configuration (Loom: "
               "loom { runs { client { vmArg \"-Dmixin.debug.export=true\" } } }; ForgeGradle: jvmArgs), start the "
               "game until the target classes load, then run mixin-check again (or pass the folder with --export)")
# a JVM internal class name: Java identifiers (with $) split by "/"; no "..", ":", "\" or leading "/"
INTERNAL_NAME = re.compile(r"(?:[^\W\d]|\$)[\w$]*(?:/(?:[^\W\d]|\$)[\w$]*)*")
PRIMITIVES = {"B": "byte", "C": "char", "D": "double", "F": "float", "I": "int", "J": "long", "S": "short",
              "Z": "boolean"}
NEXT_RERUN ="start the game again with -Dmixin.debug.export=true so the export matches the current Mixin, then " \
             "run mixin-check again"


@dataclass
class Export:
    dir: Path
    shown: str                                  # as the evidence names it (relative to the repository)
    _cache: dict[str, dict | None] = field(default_factory=dict)

    def class_path(self, binary: str) -> Path | None:
        """The exported class file of the JVM internal name ``binary``, or None. A target name comes from the
        project's source: only a valid internal name is looked up, and only a file inside ``class/``."""
        if not isinstance(binary, str) or not INTERNAL_NAME.fullmatch(binary):
            return None
        base = self.dir / "class"
        p = base / (binary + ".class")
        try:
            if not p.resolve().is_relative_to(base.resolve()):
                return None
        except (OSError, RuntimeError):
            return None
        return p if p.is_file() else None

    def read(self, binary: str) -> dict | None:
        """The exported class (``code``, ``merged``, ``path``, ``mtime``), or None when the export has none."""
        if binary not in self._cache:
            self._cache[binary] = read_class(self.class_path(binary), self.shown_of(binary))
        return self._cache[binary]

    def shown_of(self, binary: str) -> str:
        return f"{self.shown}/class/{binary}.class"


def _shown(p: Path, repo: Path) -> str:
    try:
        return p.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return p.resolve().as_posix()


def find_exports(repo: Path, roots: list[Path], given: list[str] | None = None) -> tuple[list[Export], list[str]]:
    """The Mixin export folders of the builds (``.mixin.out``, ``run/.mixin.out``, ``runs/*/.mixin.out``,
    ``run/*/.mixin.out``) and those given (an export folder, the folder holding one, or its ``class`` folder)."""
    cands: list[Path] = []
    notes: list[str] = []
    top = repo.resolve()
    for root in roots:
        found = [root / EXPORT_DIR, root / "run" / EXPORT_DIR]
        found += sorted(root.glob(f"runs/*/{EXPORT_DIR}")) + sorted(root.glob(f"run/*/{EXPORT_DIR}"))
        for d in found:
            # a folder found (not given) is read only when it, and its class/ and audit/, stay in the repository
            try:
                inside = all(x.resolve().is_relative_to(top) for x in (d, d / "class", d / "audit"))
            except (OSError, RuntimeError):
                inside = False
            if inside:
                cands.append(d)
            elif (d / "class").is_dir() or (d / "audit").is_dir():
                notes.append(f"{_shown(d, repo)}: not read, it points outside the repository (pass it with "
                             "--export to read it)")
    for g in given or []:
        p = Path(g) if Path(g).is_absolute() else repo / g
        if (p / "class").is_dir() or (p / "audit").is_dir():
            cands.append(p)
        elif (p / EXPORT_DIR).is_dir():
            cands.append(p / EXPORT_DIR)
        elif p.is_dir() and p.name == "class":
            cands.append(p.parent)
        else:
            notes.append(f"--export {g}: no Mixin export there (a folder holding class/ or audit/)")
    out: list[Export] = []
    seen: set[str] = set()
    for d in cands:
        key = str(d.resolve()).lower()
        if key in seen or not ((d / "class").is_dir() or (d / "audit").is_dir()):
            continue
        seen.add(key)
        out.append(Export(d, _shown(d, repo)))
    return out, notes


def read_class(path: Path | None, shown: str) -> dict | None:
    """An exported class file: its members with what each method's bytecode references, the ``@MixinMerged`` of
    every merged method (``(name, descriptor) -> {"mixin", "priority", "sessionId"}``), where it is and when it
    was written; None when there is no such file. A file this reader cannot read is ``{"unreadable": why}``."""
    if path is None:
        return None
    try:
        st = path.stat()
        if st.st_size > MAX_CLASS:
            return {"path": shown, "unreadable": f"{st.st_size} bytes, over the {MAX_CLASS} read here"}
        data = path.read_bytes()
    except OSError as exc:
        return {"path": shown, "unreadable": f"not readable ({exc.__class__.__name__})"}
    code = jvmclass.class_code(data)
    anns = jvmclass.class_annotations(data)
    if not code or not anns:
        return {"path": shown, "unreadable": "not a class file this reader reads"}
    merged: dict[tuple[str, str], dict] = {}
    for name, desc, a in anns["methods"]:
        m = next((x for x in a if x["type"] == MERGED), None)
        if m is not None:
            v = m["values"]
            merged[(name, desc)] = {k: v[k] for k in ("mixin", "priority", "sessionId") if k in v}
    return {"path": shown, "code": code, "merged": merged, "mtime": st.st_mtime}


def _dotted(name: str | None) -> str:
    return (name or "").replace("/", ".").replace("$", ".")


def _render(r: list) -> str:
    kind, owner, name, desc = r
    if kind == "N":
        return f"new {_dotted(owner)}"
    return f"{_dotted(owner).rsplit('.', 1)[-1]}.{name}{desc if kind == 'M' else ':' + desc}"


def handler_kind(name: str) -> str | None:
    """The injector a merged handler's name says it came from (``handler$zza000$onTick`` -> ``Inject``)."""
    head, sep, _rest = name.partition("$")
    return HANDLER_PREFIXES.get(head) if sep else None


def changes(exp: dict, orig: dict | None, orig_evidence: str | None) -> dict:
    """What the export holds that the original class file does not: per Mixin the methods it merged (added or
    replacing one), the fields and interfaces that are new, and per method of the original the calls it gained or
    lost. Without the original only the merged methods are told (``original: null``)."""
    code = exp["code"]
    binary = code.get("name") or ""
    merged = exp["merged"]
    omethods = {(m[0], m[1]): m for m in orig["methods"]} if orig else None
    by_mixin: dict[str, list[dict]] = {}
    for (name, desc), mm in merged.items():
        how = "merged" if omethods is None else "replaced" if (name, desc) in omethods else "added"
        by_mixin.setdefault(_dotted(mm.get("mixin")) or "?", []).append(
            {"method": f"{name}{desc}", "how": how, "priority": mm.get("priority"),
             **({"injector": handler_kind(name)} if handler_kind(name) else {})})
    out: dict = {"class": _dotted(binary), "export": exp["path"], "original": orig_evidence if orig else None,
                 "status": "observed", "by_mixin": by_mixin}
    calls = {}   # merged method -> the methods that call it
    for m in code["methods"]:
        for r in m[3] or []:
            if r[0] == "M" and r[1] == binary and (r[2], r[3]) in merged:
                calls.setdefault(f"{r[2]}{r[3]}", []).append(f"{m[0]}{m[1]}")
    out["calls"] = calls
    if orig is None:
        return out
    ofields = {(f[0], f[1]) for f in orig["fields"]}
    out["fields_added"] = [f"{f[0]}:{f[1]}" for f in code["fields"] if (f[0], f[1]) not in ofields]
    out["methods_added_unmarked"] = [f"{m[0]}{m[1]}" for m in code["methods"]
                                     if (m[0], m[1]) not in omethods and (m[0], m[1]) not in merged]
    emethods = {(m[0], m[1]) for m in code["methods"]}
    out["methods_gone"] = [f"{k[0]}{k[1]}" for k in omethods if k not in emethods]
    oi, ei = set(orig.get("ifaces") or []), code.get("ifaces") or []
    out["interfaces_added"] = [_dotted(i) for i in ei if i not in oi]
    changed = []
    for m in code["methods"]:
        key = (m[0], m[1])
        o = omethods.get(key)
        if o is None or key in merged or not m[3] or not o[3]:
            continue    # new, replaced, or code not walked on one side
        before = {tuple(r) for r in o[3]}
        after = {tuple(r) for r in m[3]}
        gained = [r for r in m[3] if tuple(r) not in before]
        lost = [r for r in o[3] if tuple(r) not in after]
        if gained or lost:
            row = {"method": f"{m[0]}{m[1]}"}
            if gained:
                row["calls_added"] = [_render(r) + (f" ({_dotted(merged[(r[2], r[3])].get('mixin'))})"
                                                    if r[1] == binary and (r[2], r[3]) in merged else "")
                                      for r in gained[:MAX_DIFF]]
            if lost:
                row["calls_removed"] = [_render(r) for r in lost[:MAX_DIFF]]
            changed.append(row)
    out["methods_changed"] = changed[:MAX_CHANGED]
    if len(changed) > MAX_CHANGED:
        out["methods_changed_total"] = len(changed)
    return out


def _handlers(member: str, mine: dict[tuple[str, str], dict], others: set[str] = frozenset()
              ) -> list[tuple[str, str]]:
    """The merged methods that may be an injector's handler ``member``: renamed (``prefix$id$[mod$]member``),
    else as written. A name whose part after ``prefix$id$`` is another of the Mixin's handlers (``others``) is
    that one's, not ``member``'s with a mod id."""
    exact, with_mod = [], []
    for k in sorted(mine):
        parts = k[0].split("$")
        if not handler_kind(k[0]) or len(parts) < 3:
            continue
        rest = "$".join(parts[2:])            # after prefix$id$
        if rest == member:
            exact.append(k)
        elif "$" in rest and rest.split("$", 1)[1] == member and rest not in others:
            with_mod.append(k)
    return exact or with_mod or [k for k in sorted(mine) if k[0] == member]


def _handler(member: str, mine: dict[tuple[str, str], dict], others: set[str] = frozenset()
             ) -> tuple[str, str] | None:
    got = _handlers(member, mine, others)
    return got[0] if got else None


def desc_params(desc: str) -> list[str] | None:
    """A method descriptor's parameter types as simple names (``(FLnet/x/Outer$Inner;[I)V`` -> ``float``,
    ``Inner``, ``int[]``); None when it is not read."""
    if not desc.startswith("("):
        return None
    out, i, dims = [], 1, ""
    while i < len(desc) and desc[i] != ")":
        c = desc[i]
        if c == "[":
            dims += "[]"
            i += 1
            continue
        if c == "L":
            j = desc.find(";", i)
            if j < 0:
                return None
            name, i = desc[i + 1:j].rsplit("/", 1)[-1].rsplit("$", 1)[-1], j + 1
        elif c in PRIMITIVES:
            name, i = PRIMITIVES[c], i + 1
        else:
            return None
        out.append(name + dims)
        dims = ""
    return out if i < len(desc) else None


def params_fit(params: list[str] | None, desc: str) -> bool | None:
    """Whether a handler's parameter types as written (:func:`verinoda.mixincheck._handler_params`) are those of
    the descriptor ``desc``; None when either is not read."""
    d = desc_params(desc)
    if params is None or d is None:
        return None
    if len(params) != len(d):
        return False
    for s, b in zip(params, d):
        if s.startswith("?"):            # a type variable: any reference type once erased
            bdims = b[len(b.rstrip("[]")):]
            base = b[:len(b) - len(bdims)]
            if s[1:] != bdims or (base in PRIMITIVES.values() and not bdims):
                return False
        elif s != b:
            return False
    return True


AMBIGUOUS = ("ambiguous",)


def _assign(mc, mine: dict[tuple[str, str], dict]) -> list:
    """Per injector of ``mc`` (in order) its merged method, None when it has none, or :data:`AMBIGUOUS` when
    injectors that share a handler name cannot be told apart. A merged method goes to one injector only:
    where several share a name, each takes the one whose descriptor has its parameter types."""
    injs = mc.injections
    names = {i["member"] for i in injs}
    groups: dict[tuple[bool, str], list[int]] = {}
    for n, inj in enumerate(injs):
        groups.setdefault((inj["kind"] == "Overwrite", inj["member"]), []).append(n)
    out: list = [None] * len(injs)
    taken: set = set()
    for (overwrite, member), idx in groups.items():
        cands = ([k for k in sorted(mine) if k[0] == member] if overwrite
                 else _handlers(member, mine, names - {member}))
        cands = [k for k in cands if k not in taken]
        fit = {n: {k: params_fit(injs[n].get("params"), k[1]) for k in cands} for n in idx}
        if len(idx) == 1:
            n = idx[0]
            sure = [k for k in cands if fit[n][k]]
            out[n] = (sure or cands or [None])[0]
            if out[n] is not None:
                taken.add(out[n])
            continue
        free, left, moved = set(cands), list(idx), True
        while moved:
            moved = False
            for n in list(left):
                opts = [k for k in cands if k in free and fit[n][k]]
                if len(opts) == 1 and not any(fit[o][opts[0]] for o in left if o != n):
                    out[n] = opts[0]
                    free.discard(opts[0])
                    taken.add(opts[0])
                    left.remove(n)
                    moved = True
        for n in left:
            maybe = [k for k in cands if k in free and fit[n][k] is not False]
            out[n] = AMBIGUOUS if maybe else None
    return out


def _selector_names(inj: dict) -> list[str] | None:
    """The method names an injector's selectors name (``Lpkg/X;damage(F)Z`` -> ``damage``), None when one is
    a pattern or not read."""
    if inj.get("unread") or not inj.get("selectors"):
        return None
    out = []
    for s in inj["selectors"]:
        s = s.strip()
        if s.startswith("L") and ";" in s:
            s = s.split(";", 1)[1]
        name = s.split("(", 1)[0].split(":", 1)[0].strip()
        if not name or any(c in name for c in "*/^$[]"):
            return None
        out.append(name)
    return out


def injection_rows(mc, binary: str, shown_target: str, exp: Export, orig: dict | None, source_mtime: float | None
                   ) -> list[dict]:
    """One row per injector or ``@Overwrite`` of Mixin class ``mc`` (:class:`verinoda.mixincheck.MixinClass`) on
    the target ``binary``, from the export ``exp``."""
    got = exp.read(binary)
    rows = []
    mine: dict = {}
    keys: list = [None] * len(mc.injections)
    if got is not None and "unreadable" not in got:
        mine = {k: v for k, v in got["merged"].items() if _dotted(v.get("mixin")) == _dotted(mc.name)}
        keys = _assign(mc, mine)
    for inj, key in zip(mc.injections, keys):
        row = {"at": f"{mc.path}:{inj['line']}", "mixin": mc.name, "kind": f"@{inj['kind']}",
               "member": inj["member"], "target_class": shown_target}
        if got is None:
            row.update(verdict="unknown", status="unknown",
                       why=f"the Mixin export {exp.shown} holds no {shown_target}: the class was not loaded (or not "
                           "transformed) in the run that wrote it",
                       next=f"start the game with -Dmixin.debug.export=true until {shown_target} loads, then run "
                            "mixin-check again")
            rows.append(row)
            continue
        if "unreadable" in got:
            row.update(verdict="unknown", status="unknown", why=f"{got['path']} is {got['unreadable']}",
                       evidence=got["path"])
            rows.append(row)
            continue
        row["evidence"] = got["path"]
        stale = source_mtime is not None and source_mtime > got["mtime"]
        exported = time.strftime("%Y-%m-%d %H:%M", time.localtime(got["mtime"]))
        omethods = {(m[0], m[1]) for m in orig["methods"]} if orig else None
        if key is AMBIGUOUS:
            same = sum(1 for i in mc.injections if i["member"] == inj["member"])
            row.update(verdict="unknown", status="unknown", ambiguous=True,
                       why=f"{same} injectors of {mc.name} have a handler named {inj['member']}, and which merged "
                           f"method of {got['path']} is this one's is not told apart by its parameter types",
                       next="give the handlers different names, or compare the descriptors in the exported class "
                            "with the handler's parameters")
            rows.append(row)
            continue
        if key is None:
            if sum(1 for i in mc.injections if i["member"] == inj["member"]) > 1 and any(
                    k in keys for k in _handlers(inj["member"], mine)):
                why = (f"the method(s) of {got['path']} merged by {mc.name} under the name {inj['member']} are "
                       f"another injector's (their parameter types are not those of this {row['kind']}): this "
                       f"{row['kind']} was not applied in the run that wrote the export")
            elif mine:
                why = (f"{got['path']} holds {len(mine)} method(s) {mc.name} merged, none of them the handler "
                       f"{inj['member']}: this {row['kind']} was not applied in the run that wrote the export "
                       "(require = 0, a failed or skipped injection)")
            else:
                why = (f"no method of {got['path']} carries @MixinMerged naming {mc.name}: it was not applied to "
                       f"{shown_target} in the run that wrote the export (a config that does not list it, a config "
                       "plugin that skipped it, a failed injection)")
            row.update(verdict="not_applied", status="unknown" if stale else "strong_inference", why=why)
            if stale:
                row.update(stale=f"the Mixin source changed after the export was written ({exported})",
                           next=NEXT_RERUN)
            rows.append(row)
            continue
        mm = mine[key]
        calls = [f"{m[0]}{m[1]}" for m in got["code"]["methods"]
                 if any(r[0] == "M" and r[1] == got["code"]["name"] and (r[2], r[3]) == key for r in m[3] or [])]
        merged_ann = {"mixin": mm.get("mixin"), "priority": mm.get("priority")}
        row.update(handler=f"{key[0]}{key[1]}", merged=merged_ann)
        ann = f"@MixinMerged(mixin = \"{mm.get('mixin')}\", priority = {mm.get('priority')})"
        if inj["kind"] == "Overwrite":
            replaced = omethods is not None and key in omethods
            row.update(verdict="applied", status="observed",
                       why=f"{got['path']} has {key[0]}{key[1]} with {ann}: the body is "
                           + ("this Mixin's (it replaced the original's)" if replaced else "this Mixin's"))
        elif calls:
            row.update(verdict="applied", status="observed", called_from=calls,
                       why=f"{got['path']} has the handler {key[0]}{key[1]} with {ann}, called from "
                           + ", ".join(calls[:5]))
            sels = _selector_names(inj)
            if sels and not any(c.split("(", 1)[0] in sels for c in calls):
                row["selector_mismatch"] = (f"the method(s) the injector names ({', '.join(sels)}) are not among "
                                            "those calling the handler (a remapped name, or another target)")
                row["why"] += f"; but {row['selector_mismatch']}"
        else:
            walked = all(m[3] is not False for m in got["code"]["methods"])   # None: abstract or native
            row.update(verdict="merged", status="observed",
                       why=f"{got['path']} has the handler {key[0]}{key[1]} with {ann}, but no method "
                           + ("of it calls the handler" if walked else
                              "whose bytecode this reader walked calls the handler"))
        if stale:
            row["stale"] = (f"the Mixin source changed after the export was written ({exported}): the export shows "
                            "the earlier version")
        rows.append(row)
    return rows


def read_audit(exp: Export) -> list[dict]:
    """The rows of Mixin's interface audit (``audit/mixin_implementation_report.csv``: class, method, descriptor,
    interface), each ``observed`` with its line as evidence."""
    p = exp.dir / "audit" / AUDIT_CSV
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    reader = csv.reader(io.StringIO(text))
    end = 0
    for cells in reader:
        first, i, end = end == 0, end + 1, reader.line_num   # i: the line the record starts on
        cells = [c.strip() for c in cells]
        if len(cells) < 4 or (first and cells[0].lower() == "class"):
            continue
        out.append({"class": _dotted(cells[0]), "method": cells[1] + cells[2], "interface": _dotted(cells[3]),
                    "status": "observed", "evidence": f"{exp.shown}/audit/{AUDIT_CSV}:{i}",
                    "why": f"after its Mixins were applied, {_dotted(cells[0])} lacks {cells[1]}{cells[2]} of "
                           f"{_dotted(cells[3])}"})
        if len(out) >= MAX_AUDIT_ROWS:
            break
    return out


def resolve(cands: list[str], exports: list[Export], where: set | dict) -> str:
    """The binary name a ``@Mixin`` target is: the first candidate an export holds, else the first on the
    classpath, else the first."""
    for c in cands:
        if any(e.class_path(c) for e in exports):
            return c
    return next((c for c in cands if c in where), cands[0] if cands else "")


def section(exports: list[Export], notes: list[str], rows: list[dict], classes: list[dict]) -> dict:
    """The ``export`` part of a ``mixin-check`` result."""
    counts = {v: sum(1 for r in rows if r["verdict"] == v) for v in ("applied", "merged", "not_applied", "unknown")}
    if not exports:
        return {"status": "unknown", "dirs": [], "rows": [], "classes": [], "audit": [], "counts": counts,
                "notes": notes,
                "why": ("no Mixin debug export (.mixin.out) found: what each Mixin really changed at run time is "
                        "unknown"),
                "next": NEXT_EXPORT}
    audit = [a for e in exports for a in read_audit(e)]
    return {"status": "found", "dirs": [e.shown for e in exports], "rows": rows, "classes": classes,
            "audit": audit, "counts": counts, "notes": notes}


def render(sec: dict) -> list[str]:
    if sec["status"] != "found":
        return ([f"  export: {sec['why']}"] + [f"    note: {n}" for n in sec.get("notes") or []]
                + [f"    next: {sec['next']}"])
    c = sec["counts"]
    out = [f"  Mixin export ({', '.join(sec['dirs'])}): {c['applied']} applied, {c['merged']} merged, "
           f"{c['not_applied']} not applied, {c['unknown']} unknown"]
    for n in sec["notes"]:
        out.append(f"    note: {n}")
    for r in sec["rows"]:
        out.append(f"  {r['verdict']} [{r['status']}] {r['at']}  {r['kind']} {r['mixin']}.{r['member']}  "
                   f"(in {r['target_class']})")
        out.append(f"    {r['why']}")
        if r.get("stale"):
            out.append(f"    note: {r['stale']}")
        if r.get("next"):
            out.append(f"    next: {r['next']}")
    for k in sec["classes"]:
        changed = k.get("methods_changed_total") or len(k.get("methods_changed") or [])
        parts = [f"{len(v)} by {m}" for m, v in sorted(k["by_mixin"].items(), key=lambda x: (-len(x[1]), x[0]))]
        parts = parts[:8] + ([f"{len(parts) - 8} more Mixin(s)"] if len(parts) > 8 else [])
        out.append(f"  {k['class']}: merged methods {', '.join(parts) or 'none'}"
                   + (f"; {changed} original method(s) with other calls" if changed else "")
                   + (f"; fields added {', '.join(k['fields_added'][:5])}" if k.get("fields_added") else "")
                   + (f"; interfaces added {', '.join(k['interfaces_added'])}" if k.get("interfaces_added") else "")
                   + ("" if k["original"] else " (no original class file read to compare with)"))
    for a in sec["audit"][:20]:
        out.append(f"  audit [{a['status']}] {a['why']}  {a['evidence']}")
    return out


# -- a claim about a Mixin --------------------------------------------------------------------------------

def claim_evidence(repo: Path, path: str, line: int | None, member: str, target_class: str) -> dict:
    """What the export says about the handler ``member`` of the Mixin in ``path`` (its annotation at ``line``)
    injecting into ``target_class`` (dotted): ``{"verdict", "why", ...}`` as :func:`injection_rows` gives it,
    ``{"verdict": "none"}`` when no export is found, ``{"verdict": "unmatched", ...}`` when one is but no Mixin of
    the file names that handler on that class."""
    from verinoda import mixincheck

    repo = Path(repo).resolve()
    try:
        src = (repo / path).read_bytes()
        mtime = (repo / path).stat().st_mtime
    except OSError:
        return {"verdict": "none"}
    roots = list(dict.fromkeys([jvmclass.build_root(repo, repo / path), repo]))
    exports, _notes = find_exports(repo, roots)
    exports = [e for e in exports if (e.dir / "class").is_dir()]
    if not exports:
        return {"verdict": "none", "next": NEXT_EXPORT}
    want = target_class.replace(".", "/")
    for mc in mixincheck.read_mixins(path, src):
        injs = [i for i in mc.injections if i["member"] == member]
        if not injs:
            continue
        hit = [i for i in injs if i["line"] == line] or injs
        for cands in mc.target_names:
            if not any(c.replace("$", "/") == want for c in cands):
                continue
            binary = next((c for c in cands if any(e.class_path(c) for e in exports)), cands[0])
            holders = [e for e in exports if e.class_path(binary)]
            for e in sorted(holders, key=lambda e: -e.class_path(binary).stat().st_mtime)[:1]:   # the newest
                at = f"{mc.path}:{hit[0]['line']}"
                rows = injection_rows(mc, binary, _dotted(binary), e, None, mtime)
                got = next((r for r in rows if r["at"] == at and r["member"] == member), None)
                if got:
                    return got
            return {"verdict": "unknown", "why": f"no Mixin export found holds {target_class}",
                    "next": f"start the game with -Dmixin.debug.export=true until {target_class} loads"}
    return {"verdict": "unmatched",
            "why": f"a Mixin export is there ({', '.join(e.shown for e in exports[:3])}), but no @Mixin of {path} "
                   f"read here names the handler {member} on {target_class}, so it is not looked up in the export",
            "next": f"run verinoda mixin-check {path} to see each injector of the file against the export"}
