"""Ranks the name check's ``unknown`` sites (docs/DESIGN.md D64) so a reader sees the likely mistakes first.

An ``unknown`` site was not decided: the container is open or the receiver's type is not known. Most of them
are correct code; a few are misspellings or invented names that the closed-world rule could not call absent.
Each unknown Python site gets a rank:

``high``    likely a mistake: the name is defined nowhere in the project, its environment or the stubs (a
            word index, below), or the receiver's declared type lacks it and has a close name;
``medium``  worth a look: the declared type lacks the name, a callee's ``**kwargs`` would take a name defined
            nowhere, the receiver comes from a module that is not installed, or an import was not decided;
``low``     the receiver's type is merely not known and the name is defined somewhere.

LOW sites are counted by cause (``unknown_summary``) instead of listed; ``--all`` / ``include_exists`` lists
them. The rank never changes a verdict: a HIGH site stays ``unknown``.

The "defined nowhere" test is a superset test, so it errs towards "defined": every identifier-like word in
the text of the environment's Python sources and stubs (site-packages, the standard library, jedi's bundled
typeshed) and of the project's other files counts, plus option strings as argparse turns them into names
(``"--no-mcp"`` -> ``no_mcp``) and the names of the running interpreter's built-in types and modules. The
files being checked count only with the names they define (definitions, parameters, stores, imports, string
constants), so the misspelling itself does not count. Names that only a compiled extension defines are not
in the index; a receiver bound by an import that is not installed is ranked MEDIUM, never HIGH. The words of
the environment are indexed once per environment fingerprint and kept in memory and, when the project has
``.verinoda/``, in its check cache.
"""

from __future__ import annotations

import ast
import builtins
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from verinoda import codecheck_facts as cf

INDEX_VERSION = "1"
# the environment's word index stops after this many seconds; a name not found in an incomplete index is MEDIUM,
# never HIGH (the index is kept, so the next call does not pay again)
ENV_INDEX_BUDGET_S = float(os.environ.get("VERINODA_NAME_INDEX_BUDGET_S", "120"))
NEAR_HIGH = 0.8          # a member of the declared type this close to the name makes the site HIGH
UNIVERSE_NEAREST = 50    # nearest defined names are looked up for at most this many HIGH sites
RANK_ORDER = {"high": 0, "medium": 1, "low": 2}

_WORD_RX = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*")
_OPTION_RX = re.compile(rb"""["']--?([A-Za-z][A-Za-z0-9_]*(?:-[A-Za-z0-9_]+)*)["']""")
_OPTION_STR = re.compile(r"--?([A-Za-z][A-Za-z0-9_]*(?:-[A-Za-z0-9_]+)*)")


def _words(data: bytes) -> set[str]:
    """Every identifier-like word of a file's text, and the names argparse makes of option strings."""
    out = {w.decode("ascii") for w in _WORD_RX.findall(data)}
    out |= {m.group(1).decode("ascii").replace("-", "_") for m in _OPTION_RX.finditer(data)}
    if not data.isascii():   # identifiers outside ASCII (PEP 3131)
        from verinoda.codecheck import _text_words

        out |= _text_words(data.decode("utf-8", "replace"))
    return out


@dataclass
class EnvNames:
    words: frozenset
    files: int
    complete: bool
    seconds: float


_ENV: dict[str, EnvNames] = {}
_FILE_WORDS: dict[str, tuple] = {}   # path -> (stat key, words) of the project's files
_RUNTIME: set[str] = set()


def _runtime_names() -> set[str]:
    """The names of this interpreter's built-in types and compiled standard-library modules (its own objects;
    nothing from a checked project is imported)."""
    if not _RUNTIME:
        out = set(dir(builtins))
        for v in vars(builtins).values():
            if isinstance(v, type):
                out |= set(dir(v))
        for name in sys.builtin_module_names:
            m = sys.modules.get(name)
            if m is None:
                continue
            out |= set(dir(m))
            for v in vars(m).values():
                if isinstance(v, type):
                    out |= set(dir(v))
        _RUNTIME.update(out)
    return _RUNTIME


def _jedi_version() -> str:
    try:
        import jedi

        return str(jedi.__version__)
    except ImportError:  # pragma: no cover - the ranker runs only with jedi
        return "none"


def env_names(env, repo: Path) -> EnvNames:
    """The word index of the environment ``env`` (see the module docstring), built once per fingerprint."""
    from verinoda import codecheck_env as cenv
    from verinoda.codecheck import _cache_dir

    key = f"{env.fingerprint}|{_jedi_version()}|{INDEX_VERSION}"
    hit = _ENV.get(key)
    if hit is not None:
        return hit
    d = _cache_dir(repo)
    disk = d / f"names-{env.fingerprint[:32]}.json" if d is not None and env.fingerprint else None
    if disk is not None:
        try:
            data = json.loads(disk.read_text(encoding="utf-8"))
            if data.get("key") == key:
                hit = EnvNames(frozenset(data["words"]), int(data["files"]), bool(data["complete"]),
                               float(data["seconds"]))
        except (OSError, ValueError, KeyError, TypeError):
            hit = None
    if hit is None:
        t0 = time.perf_counter()
        words: set[str] = set()
        files = 0
        complete = True
        seen: set[str] = set()
        ts = cenv.jedi_typeshed_dir()
        roots = [(Path(p), False) for p in env.site_dirs] + [(Path(p), True) for p in env.stdlib_dirs] + \
            ([(ts, False)] if ts else [])
        for root, stdlib in roots:
            k = os.path.normcase(str(root))
            if k in seen or not root.is_dir():
                continue
            seen.add(k)
            for dirpath, dirnames, filenames in os.walk(root):
                # the base interpreter's own site-packages is not part of a virtual environment
                dirnames[:] = [x for x in dirnames if x != "__pycache__" and not x.startswith(".")
                               and not (stdlib and x in ("site-packages", "dist-packages"))]
                for fn in filenames:
                    if fn.endswith((".py", ".pyi")):
                        try:
                            data = Path(dirpath, fn).read_bytes()
                        except OSError:
                            continue
                        words |= _words(data)
                        files += 1
                if time.perf_counter() - t0 > ENV_INDEX_BUDGET_S:
                    complete = False
                    break
            if not complete:
                break
        hit = EnvNames(frozenset(words), files, complete, round(time.perf_counter() - t0, 2))
        if disk is not None:
            try:
                disk.parent.mkdir(parents=True, exist_ok=True)
                tmp = disk.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(json.dumps({"key": key, "files": hit.files, "complete": hit.complete,
                                           "seconds": hit.seconds, "words": sorted(hit.words)}),
                               encoding="utf-8", newline="\n")
                os.replace(tmp, disk)
            except OSError:
                pass
    if len(_ENV) > 4:
        _ENV.clear()
    _ENV[key] = hit
    return hit


def _project_file_words(p: Path) -> frozenset:
    key = cf.stat_key(p)
    hit = _FILE_WORDS.get(str(p))
    if hit is not None and hit[0] == key:
        return hit[1]
    try:
        data = cf.read_text(p).encode("utf-8", "replace")
    except OSError:
        data = b""
    words = frozenset(_words(data))
    if len(_FILE_WORDS) > 20000:
        _FILE_WORDS.clear()
    _FILE_WORDS[str(p)] = (key, words)
    return words


def defined_names(tree: ast.AST) -> set[str]:
    """The names a file defines: functions, classes and their parameters, assigned names and attributes,
    import aliases, identifier-like string constants (setattr names, ``__slots__``, namedtuple fields) and
    the names argparse makes of option strings. A name the file only reads is not among them."""
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        if isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
            out.add(n.id)
        elif isinstance(n, ast.Attribute) and not isinstance(n.ctx, ast.Load):
            out.add(n.attr)
        elif isinstance(n, ast.alias):
            out.add(n.asname or n.name.split(".")[0])
            out.update(n.name.split("."))
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            out.update(n.names)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif type(n).__name__ in ("MatchAs", "MatchStar") and getattr(n, "name", None):
            out.add(n.name)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and len(n.value) < 200:
            v = n.value
            if v.isidentifier():
                out.add(v)
            else:
                m = _OPTION_STR.fullmatch(v)
                if m:
                    out.add(m.group(1).replace("-", "_"))
                elif " " in v or "," in v:   # namedtuple("P", "x y"), _fields strings
                    out.update(w for w in v.replace(",", " ").split() if w.isidentifier())
    return out


class NameIndex:
    """Is a name defined anywhere? The environment's words, the project's other files' words, and the names
    the checked files define."""

    def __init__(self, env, repo: Path, checked: list[tuple[Path, str | None]]):
        """``checked``: the checked Python files, each with the text checked for it (a snippet) or None (the
        file on disk)."""
        from verinoda.codecheck import py_files

        self.env = env_names(env, repo)
        self.runtime = _runtime_names()
        self.checked = {os.path.normcase(str(p)) for p, _t in checked}
        files, self.truncated = py_files(repo, [repo])
        words: set[str] = set()
        own: set[str] = set()
        for p in files:
            if os.path.normcase(str(p)) not in self.checked:
                words |= _project_file_words(p)
        for p, text in checked:
            try:
                tree = ast.parse(text if text is not None else cf.read_text(p))
            except (SyntaxError, ValueError, OSError):
                tree = None
            if tree is None:   # not parsed: its words count, so nothing is flagged because of it
                words |= _words((text or "").encode("utf-8", "replace")) if text is not None else \
                    _project_file_words(p)
            else:
                own |= defined_names(tree)
        self.project = frozenset(words)
        self.own = frozenset(own)

    @property
    def complete(self) -> bool:
        return self.env.complete and not self.truncated

    def defined(self, name: str) -> bool:
        return name in self.own or name in self.project or name in self.env.words or name in self.runtime

    def nearest(self, name: str, limit: int = 3) -> list[dict]:
        """Defined names close to ``name`` (edit distance), best first."""
        from rapidfuzz import process
        from rapidfuzz.distance import OSA

        pool = self.own | self.project
        got = process.extract(name, pool, scorer=OSA.normalized_similarity, limit=limit, score_cutoff=0.75)
        if len(got) < limit:
            got += process.extract(name, self.env.words, scorer=OSA.normalized_similarity, limit=limit,
                                   score_cutoff=0.75)
        out: dict[str, float] = {}
        for cand, score, _ in sorted(got, key=lambda t: -t[1]):
            if cand != name and cand not in out:
                out[cand] = score
        return [{"name": c, "score": round(s, 2)} for c, s in list(out.items())[:limit]]


# -- causes of unknowns ---------------------------------------------------------------------------------------

# (code, label, pattern over `why`, one step that settles such a site)
CAUSES: list[tuple[str, str, re.Pattern, str]] = [
    ("not_installed_receiver", "the receiver comes from a module that is not installed", re.compile(r"^$"),
     "install the module in the project's environment, or pass --env PATH"),
    ("declared_type", "the declared type lacks the name (a subclass or runtime code may add it)",
     re.compile(r"declared type"), "`verinoda api <declared type>` lists its names; narrow with isinstance"),
    ("kwargs", "the callee takes **kwargs", re.compile(r"takes \*\*kwargs"),
     "read where the callee passes **kwargs on (the signature's file:line)"),
    ("parameter", "the receiver is a parameter", re.compile(r"is a parameter"),
     "annotate the parameter (jedi then reads its type), or read what the callers pass"),
    ("bound_several_times", "the local is bound several times", re.compile(r"is bound \d+ times"),
     "read the local's bindings in its function"),
    ("not_call_or_literal", "the local is not assigned from a call or a literal",
     re.compile(r"not assigned from a call"), "read the local's assignment (its right-hand side's type)"),
    ("comprehension", "a comprehension variable", re.compile(r"comprehension variable"),
     "read the iterable the comprehension loops over"),
    ("module_level", "a module-, class-level or enclosing name",
     re.compile(r"module-level or enclosing name|module- or class-level name"),
     "read where the module binds the name"),
    ("no_definition", "jedi found no definition of the receiver", re.compile(r"jedi found no definition"),
     "read where the receiver comes from (an import jedi cannot follow, a star import, a dynamic name)"),
    ("call_result", "the receiver is the result of a call",
     re.compile(r"holds the result of|return value of|result of .*\(\)|result of a call|does not say which class"),
     "read the called function's return type (`verinoda api <function>`)"),
    ("expression", "the receiver is an expression", re.compile(r"the receiver is an expression"),
     "assign the expression to a typed local, or read the operand types"),
    ("open_container", "the container is not closed", re.compile(r"is not closed"),
     "read the container's source where it adds names (the reason names it)"),
    ("runtime_value", "a value assigned at runtime", re.compile(r"value assigned at runtime"),
     "read the assignment of that value"),
    ("several_definitions", "several possible definitions", re.compile(r"possible definitions"),
     "read the definitions jedi found"),
    ("escapes", "the local is handed to other code", re.compile(r"handed to other code|attributes are set on|"
                                                                r"passes it to|is passed to"),
     "read the code the local is handed to"),
    ("decorated_callee", "the callee is decorated", re.compile(r"is decorated with"),
     "read the decorator's wrapper signature"),
    ("callee", "the callee is not resolved to one signature",
     re.compile(r"the callee|callee's|no signature for|not a plain method|is not found in|metaclass|__new__|"
                r"constructor"), "read the callee's definition or documentation"),
    ("import", "an import that was not decided", re.compile(r"sys\.path|not closed|module|package"),
     "check how the code is run (sys.path, the package layout)"),
    ("no_jedi", "jedi is not installed", re.compile(r"jedi is not installed"),
     "install the precise extra: pip install 'verinoda[precise]'"),
]
_OTHER = ("other", "other reasons", re.compile(r""), "read the receiver's type definition, or run the tests that "
                                                      "reach this line")


def cause_of(site: dict) -> tuple[str, str, str]:
    """(code, label, next step) of an unknown site, from its ``why``."""
    if site.get("_missing_root"):
        c = CAUSES[0]
        return c[0], c[1], c[3]
    if site.get("declared"):
        c = CAUSES[1]
        return c[0], c[1], f"`verinoda api {site['declared']}` lists its names; narrow with isinstance"
    why = str(site.get("why") or "")
    for code, label, rx, step in CAUSES[2:]:
        if code == "import" and site.get("kind") != "import":
            continue
        if rx.search(why):
            return code, label, step
    return _OTHER[0], _OTHER[1], _OTHER[3]


# -- ranking --------------------------------------------------------------------------------------------------

def _missing_roots(sites: list[dict], repo: Path) -> dict[str, dict[str, str]]:
    """path -> {name bound by an import of that file that is not installed / absent / not decided -> module}."""
    lines: dict[str, dict[int, str]] = {}
    for s in sites:
        if s.get("kind") == "import" and s["verdict"] in ("not_installed", "absent", "guarded", "unknown"):
            lines.setdefault(s["path"], {})[s["line"]] = s.get("expr") or s.get("name") or "?"
    out: dict[str, dict[str, str]] = {}
    for rel, at in lines.items():
        tree = cf.parse_file((repo / rel).resolve())[0]
        if tree is None:
            continue
        names: dict[str, str] = {}
        for n in ast.walk(tree):
            if isinstance(n, (ast.Import, ast.ImportFrom)) and n.lineno in at:
                for al in n.names:
                    if al.name == "*":
                        continue
                    bound = al.asname or (al.name.split(".")[0] if isinstance(n, ast.Import) else al.name)
                    names[bound] = (n.module or al.name) if isinstance(n, ast.ImportFrom) else al.name
        out[rel] = names
    return out


def _root(expr: str) -> str | None:
    m = re.match(r"[A-Za-z_]\w*", expr or "")
    return m.group(0) if m else None


def _lookup_name(site: dict) -> str:
    """The name looked up in the index: a Django-style lookup keyword (``num__lte``) is its first part."""
    n = str(site.get("name") or "")
    if site.get("kind") == "kwarg" and "__" in n.strip("_"):
        return n.strip("_").split("__")[0]
    return n


def rank_site(site: dict, idx: NameIndex | None, missing: dict[str, str]) -> tuple[str, str]:
    """(rank, why) of one unknown Python site."""
    name = _lookup_name(site)
    why = str(site.get("why") or "")
    kind = site.get("kind")
    if kind == "import":
        return "medium", "an import that was not decided: it may fail at run time"
    if kind == "dict_key":
        return "low", "the dict's keys are not all known"
    root = _root(str(site.get("expr") or ""))
    if root and root in missing:
        site["_missing_root"] = True
        return "medium", f"`{root}` comes from {missing[root]}, which is not installed or not found here"
    dyn = "__getattr__" in why or "__getattribute__" in why
    declared = site.get("declared")
    near = site.get("nearest") or []
    if declared and near and near[0].get("score", 0) >= NEAR_HIGH and not dyn:
        return "high", f"`{site.get('name')}` is not in the declared type {declared}; a close name is: " \
                       f"{near[0]['name']}"
    if idx is not None and not dyn and name and not idx.defined(name):
        if not idx.complete:   # a name found is defined; one not found may be in the part not read
            return "medium", f"`{name}` is not in the part of the name index that was read (it stopped early)"
        where = "the project, its environment or the stubs"
        if kind == "kwarg" and "takes **kwargs" in why:
            return "medium", f"`{name}` is defined nowhere in {where}; the callee's **kwargs would take it"
        return "high", f"`{name}` is defined nowhere in {where}"
    if declared:
        return "medium", f"`{site.get('name')}` is not in the declared type {declared}: a subclass or runtime " \
                         "code may add it"
    return "low", "the receiver's type is not known and the name is defined elsewhere"


def rank_sites(env, repo: Path, sites: list[dict], checked: list[tuple[Path, str | None]], jedi: bool) -> dict:
    """Rank every unknown Python site in place (``rank``, ``rank_why``, a ``next_step`` when it had none)
    and return the ``unknown_summary``."""
    todo = [s for s in sites if s["verdict"] == "unknown" and not s.get("language")]
    idx = None
    idx_note = None
    if todo and jedi and env is not None:
        try:
            idx = NameIndex(env, repo, checked)
            if not idx.complete:
                idx_note = ("the name index stopped early (" + ("the project's walk limit" if idx.truncated else
                                                                f"{ENV_INDEX_BUDGET_S:g} s budget") +
                            "): a name not found in it is MEDIUM, never HIGH")
        except Exception as exc:  # noqa: BLE001 - a rank is advice; the check's verdicts stand without it
            idx, idx_note = None, f"the name index could not be built ({type(exc).__name__}: {exc})"[:300]
    missing = _missing_roots(sites, repo) if todo else {}
    highs: list[dict] = []
    for s in todo:
        if not jedi:
            s["rank"], s["rank_why"] = "low", "jedi is not installed"
        else:
            s["rank"], s["rank_why"] = rank_site(s, idx, missing.get(s["path"], {}))
        code, _label, step = cause_of(s)
        if not s.get("next_step"):
            s["next_step"] = step
        if s["rank"] == "high" and s.get("declared") and s.get("nearest"):
            s["next_step"] = f"use a name of the declared type: {s['nearest'][0]['name']} " \
                             f"(`verinoda api {s['declared']}` lists them)"
        elif s["rank"] == "high" and not s.get("nearest"):
            highs.append(s)
        s.pop("_missing_root", None)
    if idx is not None:
        for s in highs[:UNIVERSE_NEAREST]:
            near = idx.nearest(_lookup_name(s))
            if near:
                s["nearest"] = near
                s["next_step"] = "check the spelling against the nearest defined names"
    counts = {r: sum(1 for s in todo if s.get("rank") == r) for r in RANK_ORDER}
    groups: dict[str, dict] = {}
    for s in todo:
        if s.get("rank") != "low":
            continue
        code, label, step = cause_of(s)
        g = groups.setdefault(code, {"cause": label, "sites": 0, "example": f"{s['at']} {s['expr']}",
                                     "next_step": step})
        g["sites"] += 1
    out: dict = {**counts, "low_by_cause": sorted(groups.values(), key=lambda g: (-g["sites"], g["cause"]))}
    if idx is not None:
        out["name_index"] = {"environment_files": idx.env.files, "seconds": idx.env.seconds,
                             "complete": idx.complete}
    if idx_note:
        out["note"] = idx_note
    return out


def order_key(site: dict) -> tuple:
    """Sort key: absent, HIGH unknown, not installed, MEDIUM (and unranked) unknown, guarded, LOW unknown,
    exists; then path, line and column."""
    v = site["verdict"]
    if v == "unknown":
        slot = {"high": 1, "low": 5}.get(site.get("rank") or "", 3)
    else:
        slot = {"absent": 0, "not_installed": 2, "guarded": 4, "exists": 6}.get(v, 3)
    return slot, site["path"], site["line"], site["col"]
