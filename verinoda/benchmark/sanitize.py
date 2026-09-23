"""Replace machine-specific absolute paths in benchmark outputs with placeholders.

Result files and delivered contexts are meant to be committed and compared
across machines, so they must not carry the user's home directory, the temp
directory or where a checkout happens to live. Placeholders (most specific
path wins):

``<TMP>``     the system temp directory (benchmark workdirs live there)
``<CORPUS>``  the analysed repository (``--repo``) when it is outside ``<REPO>``
``<REPO>``    the Verinoda checkout that ran the benchmark (only when it is a
              source checkout, i.e. has a ``pyproject.toml``)
``<HOME>``    the user's home directory

Both separator spellings (``C:\\x\\y`` and ``C:/x/y``) and the Git-Bash form
(``/c/x/y``) are recognised, case-insensitively for Windows drive paths. The
rest of a matched path is rewritten with ``/`` so the output reads the same
on every OS. Only prefixes at a path boundary are replaced
(``<REPO>`` never eats ``verinoda-other``).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

# Characters a path component may contain in the text we write (no whitespace,
# quotes or separators). Deliberately excludes ':' so "file.py:12" keeps its line.
_TAIL = r"(?P<tail>(?:[\\/][^\s\"'`<>|\\/:*?]*)*)"


def verinoda_checkout() -> Path | None:
    """The Verinoda source checkout this code runs from, or None for an installed package."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").exists() else None


def _is_under(p: Path, parent: Path) -> bool:
    try:
        p.relative_to(parent)
        return True
    except ValueError:
        return False


def path_map(corpus: str | Path | None = None, *, home: str | Path | None = None,
             tmp: str | Path | None = None, repo: str | Path | None = "auto") -> list[tuple[str, str]]:
    """``[(absolute path, placeholder)]``, longest path first. Arguments override detection (tests)."""
    entries: list[tuple[str, str]] = []

    def add(p, name: str) -> None:
        if not p:
            return
        s = str(p)
        if s.startswith("<"):  # already a placeholder
            return
        for v in {s, os.path.realpath(s)}:
            v = v.rstrip("\\/")
            # A bare root ("C:/", "/") would swallow every absolute path on the machine.
            if len(Path(v).parts) < 2 or re.fullmatch(r"[A-Za-z]:", v):
                continue
            entries.append((v, name))

    repo_root = verinoda_checkout() if repo == "auto" else (Path(repo) if repo else None)
    tmp_dirs = [tmp] if tmp else list(dict.fromkeys([tempfile.gettempdir(), os.environ.get("TEMP"),
                                                     os.environ.get("TMP"), os.environ.get("TMPDIR")]))
    for t in tmp_dirs:
        add(t, "<TMP>")
    if corpus and not str(corpus).startswith("<"):
        c = Path(corpus).resolve()
        if repo_root is None or not _is_under(c, Path(repo_root).resolve()):
            add(c, "<CORPUS>")
    if repo_root is not None:
        add(Path(repo_root).resolve(), "<REPO>")
    add(home if home else Path.home(), "<HOME>")
    # Most specific first; stable for equal lengths.
    seen: set[str] = set()
    out = []
    for p, name in sorted(entries, key=lambda e: -len(e[0])):
        key = p.lower() if re.match(r"[A-Za-z]:", p) else p
        if key not in seen:
            seen.add(key)
            out.append((p, name))
    return out


def _pattern(path: str) -> re.Pattern:
    s = path.replace("\\", "/")
    drive = re.match(r"([A-Za-z]):/(.*)$", s)
    if drive:
        letter, rest = drive.groups()
        head = rf"(?:{letter}:[\\/]+|/{letter}/)"
        flags = re.IGNORECASE
    else:
        rest = s.lstrip("/")
        head = r"/"
        flags = 0
    body = r"[\\/]+".join(re.escape(c) for c in rest.split("/") if c)
    return re.compile(rf"(?<![\w.\-]){head}{body}(?![\w.\-]){_TAIL}", flags)


class Sanitizer:
    def __init__(self, mapping: list[tuple[str, str]]):
        self.mapping = mapping
        self._rx = [(_pattern(p), name) for p, name in mapping]

    def text(self, s: str) -> str:
        if not s:
            return s
        for rx, name in self._rx:
            s = rx.sub(lambda m, n=name: n + m.group("tail").replace("\\", "/"), s)
        return s

    def obj(self, o):
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, list):
            return [self.obj(x) for x in o]
        if isinstance(o, tuple):
            return tuple(self.obj(x) for x in o)
        if isinstance(o, dict):
            return {self.text(k) if isinstance(k, str) else k: self.obj(v) for k, v in o.items()}
        return o


PLACEHOLDERS = ("<TMP>", "<CORPUS>", "<REPO>", "<HOME>")


def used_placeholders(obj) -> list[str]:
    """Placeholders that occur in ``obj`` (serialised), in :data:`PLACEHOLDERS` order."""
    blob = json.dumps(obj, ensure_ascii=False, default=str)
    return [p for p in PLACEHOLDERS if p in blob]


def _corpus_source(res: dict):
    """The analysed repository of a result: ``corpus.source`` (benchmark runs) or ``corpus_path`` (harnesses)."""
    c = res.get("corpus")
    if isinstance(c, dict):
        return c.get("source")
    return res.get("corpus_path")


def for_result(res: dict) -> Sanitizer:
    """Sanitizer for a result dict (its analysed repository becomes ``<CORPUS>`` or ``<REPO>/...``)."""
    return Sanitizer(path_map(_corpus_source(res)))


def write_json(path: str | Path, obj: dict, *, corpus: str | Path | None = None) -> dict:
    """Write ``obj`` as sanitised JSON (UTF-8, LF, sorted keys) and return what was written.

    Used by the question-independent harnesses (staleness, critique_eval):
    ``corpus`` is the repository they read, if any.
    """
    san = Sanitizer(path_map(corpus if corpus is not None else _corpus_source(obj)))
    clean = san.obj(obj)
    clean["paths_sanitized"] = {"placeholders": used_placeholders(clean),
                                "rule": "absolute home / temp / checkout paths replaced when written; see "
                                        "verinoda/benchmark/sanitize.py"}
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes((json.dumps(clean, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n")
                  .encode("utf-8"))
    return clean


def sanitize_result_file(path: str | Path) -> dict:
    """Rewrite an existing result JSON and its delivered contexts in place. Returns what changed.

    Delivered contexts (``raw/<stem>/...``) are rewritten only where they
    contain a machine path; each such slot gets ``score.context_sanitized``
    because its recorded ``sha256`` is of the text as scored, before sanitising.
    Must run on the machine that produced the file (it uses this machine's paths).
    """
    path = Path(path)
    res = json.loads(path.read_text(encoding="utf-8"))
    san = for_result(res)
    changed_contexts = 0
    for row in res.get("questions") or []:
        for slot in row.get("approaches", {}).values():
            sc = slot.get("score") or {}
            rel = sc.get("context_file")
            if not rel:
                continue
            p = path.parent / rel
            if not p.exists():
                continue
            before = p.read_text(encoding="utf-8")
            after = san.text(before)
            if after != before:
                p.write_text(after, encoding="utf-8", newline="\n")
                sc["context_sanitized"] = True
                changed_contexts += 1
    before_json = json.dumps(res, ensure_ascii=False, sort_keys=True)
    clean = san.obj(res)
    changed_json = json.dumps(clean, ensure_ascii=False, sort_keys=True) != before_json
    prev = clean.pop("paths_sanitized", None) or {}
    clean["paths_sanitized"] = {"placeholders": used_placeholders(clean),
                                "rule": prev.get("rule") or "absolute home / temp / checkout paths replaced after "
                                        "the run by `python -m verinoda.benchmark sanitize`; see "
                                        "verinoda/benchmark/sanitize.py"}
    path.write_text(json.dumps(clean, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8",
                    newline="\n")
    return {"file": path.name, "json_changed": changed_json, "contexts_changed": changed_contexts,
            "placeholders": [name for _, name in san.mapping]}
