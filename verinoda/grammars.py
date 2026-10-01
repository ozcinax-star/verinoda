"""Optional grammars: which ones this install has, and the files a build could not extract without one.

Most languages the index reads come with grammars installed alongside Verinoda. Some come only with an extra
(``pip install "verinoda[languages]"``, or one language's extra such as ``verinoda[solidity]``): VB.NET, R,
Erlang and Solidity, and the older optional ones (SQL, Terraform/HCL, OCaml, Common Lisp, DreamMaker, Robot
Framework). Without its grammar such a file's extractor returns an error, never raises, and the file has no
node in the graph. :func:`not_extracted` names those files, grouped by the grammar they need, with the reason
and the extra that provides it, the way the index report counts the files it could not classify.

:func:`signature` is part of the extraction stamp (:func:`verinoda.buildlock.extraction_stamp`): installing or
removing a grammar rebuilds the graph on the next ``update``, so a file skipped for want of its grammar is read
once the grammar is there. Nothing is installed or downloaded here.
"""
from __future__ import annotations

import importlib
import importlib.util
from pathlib import PurePosixPath

# grammar -> (module to import, distribution, language function: None = importing the module is the check)
GRAMMARS: dict[str, tuple[str, str, str | None]] = {
    "vbnet": ("tree_sitter_vb_dotnet", "tree-sitter-vb-dotnet", "language"),
    "solidity": ("tree_sitter_solidity", "tree-sitter-solidity", "language"),
    "r": ("tree_sitter_language_pack", "tree-sitter-language-pack", "r"),
    "erlang": ("tree_sitter_language_pack", "tree-sitter-language-pack", "erlang"),
    "sql": ("tree_sitter_sql", "tree-sitter-sql", "language"),
    "terraform": ("tree_sitter_hcl", "tree-sitter-hcl", "language"),
    "ocaml": ("tree_sitter_ocaml", "tree-sitter-ocaml", "language_ocaml"),
    "commonlisp": ("tree_sitter_commonlisp", "tree-sitter-commonlisp", "language"),
    "dm": ("tree_sitter_dm", "tree-sitter-dm", "language"),
    "robot": ("robot.api", "robotframework", None),
}
# suffix (lower case) -> (language, grammar); the grammar's name is also its extra. The same suffixes as the
# extractor's own table of optional dependencies (project_index/extract.py _EXTRA_FOR_EXTENSION).
SUFFIXES: dict[str, tuple[str, str]] = {
    ".vb": ("VB.NET", "vbnet"), ".sol": ("Solidity", "solidity"), ".r": ("R", "r"),
    ".erl": ("Erlang", "erlang"), ".hrl": ("Erlang", "erlang"), ".escript": ("Erlang", "erlang"),
    ".sql": ("SQL", "sql"), ".tf": ("Terraform", "terraform"), ".tfvars": ("Terraform", "terraform"),
    ".hcl": ("HCL", "terraform"), ".ml": ("OCaml", "ocaml"), ".mli": ("OCaml", "ocaml"),
    ".lisp": ("Common Lisp", "commonlisp"), ".cl": ("Common Lisp", "commonlisp"),
    ".lsp": ("Common Lisp", "commonlisp"), ".asd": ("Common Lisp", "commonlisp"),
    ".dm": ("DreamMaker", "dm"), ".dme": ("DreamMaker", "dm"),
    ".robot": ("Robot Framework", "robot"), ".resource": ("Robot Framework", "robot"),
}
EXAMPLES = 5   # files named per group; the rest are counted
_PROBLEMS: dict[str, str | None] = {}


def problem(grammar: str) -> str | None:
    """Why ``grammar`` cannot be used in this process (``"<distribution> is not installed"``, or that it is
    installed but failed to load, with the error), or None when it loads. Checked once per process."""
    if grammar in _PROBLEMS:
        return _PROBLEMS[grammar]
    module, dist, fn = GRAMMARS[grammar]
    why = None
    try:
        mod = importlib.import_module(module)
    except ImportError as exc:
        top = module.split(".")[0]
        try:
            found = importlib.util.find_spec(top) is not None
        except (ImportError, ValueError):
            found = False
        why = f"{dist} is installed but failed to load: {exc}"[:300] if found else f"{dist} is not installed"
    except Exception as exc:  # noqa: BLE001 - a broken package: the same outcome as a broken grammar
        why = f"{dist} is installed but failed to load: {type(exc).__name__}: {exc}"[:300]
    else:
        if fn is not None:
            try:
                from tree_sitter import Language

                if module == "tree_sitter_language_pack":
                    mod.get_language(fn)
                else:
                    import warnings

                    with warnings.catch_warnings():  # an older binding hands Language an int pointer
                        warnings.simplefilter("ignore", DeprecationWarning)
                        Language(getattr(mod, fn)())
            except Exception as exc:  # noqa: BLE001 - grammar present, its language does not load
                why = f"{dist} is installed but failed to load: {type(exc).__name__}: {exc}"[:300]
    _PROBLEMS[grammar] = why
    return why


def installed(grammar: str) -> bool:
    """Is ``grammar``'s package installed (found on the path, not imported)? An answer :func:`problem` already
    gave in this process is taken instead (a grammar it found broken counts as missing)."""
    if grammar in _PROBLEMS:
        return _PROBLEMS[grammar] is None
    try:
        return importlib.util.find_spec(GRAMMARS[grammar][0].split(".")[0]) is not None
    except (ImportError, ValueError):
        return False


def signature() -> str:
    """The optional grammars installed here, e.g. ``"r,solidity"`` (``""``: none). Cheap: nothing is imported."""
    return ",".join(g for g in sorted(GRAMMARS) if installed(g))


def install_hint(grammar: str) -> str:
    return f'pip install "verinoda[{grammar}]"'


def not_extracted(files, in_graph) -> list[dict]:
    """The files of ``files`` (repo-relative paths) that need an optional grammar this install cannot load and
    that have no node in the graph (``in_graph``: the graph's source files), one group per grammar:
    ``{"language", "grammar", "count", "files", "reason", "install"}``, the largest group first. A file in
    the graph, or one whose grammar loads, is never listed. ``in_graph`` may be a callable that returns them,
    called only when some file needs a grammar that does not load (reading a large graph is not free)."""
    groups: dict[str, dict] = {}
    wanting = []
    for f in sorted(files):
        hit = SUFFIXES.get(PurePosixPath(f).suffix.lower())
        if hit is not None and problem(hit[1]) is not None:
            wanting.append((f, hit))
    if not wanting:
        return []
    seen = set((in_graph() if callable(in_graph) else in_graph) or ())
    for f, (lang, grammar) in wanting:
        if f in seen:
            continue
        why = problem(grammar)
        g = groups.setdefault(grammar, {"language": lang, "grammar": grammar, "count": 0, "files": [],
                                        "reason": why, "install": install_hint(grammar)})
        if lang not in g["language"].split(" / "):
            g["language"] += f" / {lang}"
        g["count"] += 1
        if len(g["files"]) < EXAMPLES:
            g["files"].append(f)
    return sorted(groups.values(), key=lambda g: (-g["count"], g["grammar"]))


def describe(group: dict) -> str:
    """One line for a :func:`not_extracted` group."""
    more = group["count"] - len(group["files"])
    return (f"{group['count']} {group['language']} file(s) not extracted: {group['reason']} "
            f"({group['install']}): {', '.join(group['files'])}" + (f" (+{more} more)" if more > 0 else ""))
