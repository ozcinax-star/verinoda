"""Build localization tasks from ContextBench (human-annotated gold contexts) for the follow-up agent study: the same
tasks.json schema as commit_tasks.py (id, title, body, gold, base_sha, ...) plus "remote" (clone URL), "repo"
(owner/name) and "lang". Nothing is written by a model.

    python contextbench_tasks.py select CONFIG.json OUT_TASKS.json

Run it with the interpreter that has pyarrow (the ContextBench env, C:/vbench/bench-2026-10-02/cb/.venv), because the
dataset is a parquet file; `select()` itself needs only the standard library and takes the rows as a list of dicts.
Needs a GitHub token from `git credential fill` (never printed) for the repository sizes.

CONFIG (every key but "parquet" is optional; the defaults are the pre-registered rules below):
{"parquet": path, "seed": 20261002, "want": 60, "per_repo_max": 3, "gold_min": 2, "gold_max": 6, "statement_min": 150,
"statement_max": 4000, "size_max_mb": 200, "verify_fetch": true, "languages": null}

RULES, fixed before the first run. An instance is a row of the parquet; it is dropped by the first rule it breaks:

1. Gold files. The gold is the DISTINCT SOURCE files the instance's gold patch changes and that exist before it (a
   modified, deleted or renamed file, by its old path; not a file the patch adds), as in the commit-mined studies: the
   files that must change. Each path of a `diff --git` header is normalised to a repository-relative one (backslashes to
   "/", a leading "/testbed/" or "/workspace/<x>/" or "/" or "./" removed), kept when `is_gold_path` is true, sorted.
   Keep an instance only with 2 to 6 gold files. (The dataset's `gold_context` is not used: it lists the code a reader
   needs, which includes files that need no change, where the study asks which files must change.)
2. The problem statement (CRLF turned into LF, stripped) is 150 to 4,000 characters. The first line is the task's
   title when it is at most 200 characters (the SWE-bench statements are "title, newline, body"), the rest is the
   body; a longer first line gives a truncated title and the whole statement as the body.
3. The statement does not name any gold file by its repository path: `commit_tasks.leaks` (the path, or its last
   three components, case-insensitive, appears in title or body).
4. The repository exists and its GitHub size (REST `repos/{owner}/{repo}`, field "size", KB) is at most 200 MB. Sizes
   are cached in `repo_sizes.json` next to the output, so a rerun makes no API call for a repository seen before.
   A repository the API answers 404 for (private, removed) is dropped.
5. Order the survivors by sha256 of "seed:original_inst_id" (seed 20261002), walk that order, and take an instance
   unless its repository already has 3 taken, or its base commit is not fetchable (the "more than 3 per repository"
   count in the filters is what the cap alone removes from all survivors, whatever "want" is): `git fetch --depth 1
   --filter=blob:none <url> <sha>` into a scratch repository succeeds (only the walked instances are tried; the
   answers are cached in `fetchable.json` next to the output). Stop at "want".
6. The same fetch lists the base commit's tree: a gold file that does not exist there (a context of a file the fix
   adds) leaves the gold; an instance whose existing gold files are then not 2 to 6 is dropped.

`is_gold_path` (generalises commit_tasks.is_gold_path, which knew only the seL4 languages): a path is a source file
when ALL of these hold.
  - its basename is "CMakeLists.txt", or its suffix (the text after the last ".", lower-case) is in SOURCE_SUFFIXES
    (programming-language source and header files; no markdown, rst, txt, json, yaml, toml, html, css, lock, snapshot
    or data files, so documents and configuration never count);
  - no DIRECTORY component (lower-case) is in NOT_SOURCE_DIRS: test, tests, spec, specs, __tests__, __test__,
    __mocks__, testdata, docs, doc, .github, .circleci, .gitlab ("examples" is deliberately not listed: examples stay);
  - its basename is not a test file by name: test_*, conftest.py, test.*, tests.*, *_test, *_tests, *_spec, *.test, *.spec (before the
    suffix), or, for Java/Kotlin/Scala/C#, a stem ending in "Test" or "Tests".
The filter counts (candidates, dropped per reason, kept per language and per repository) are written next to the tasks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from commit_tasks import leaks  # the same leak test as the commit-mined tasks

SOURCE_SUFFIXES = frozenset({
    "py", "pyi", "pyx", "js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts", "vue", "svelte", "go", "rs", "c", "h", "cc",
    "cpp", "cxx", "hpp", "hh", "hxx", "inl", "java", "kt", "kts", "scala", "groovy", "rb", "php", "cs", "fs", "swift", "m",
    "mm", "pony", "s", "lua", "dart", "ex", "exs", "erl", "hrl", "cmake", "bf", "zig", "nim"})
NOT_SOURCE_DIRS = frozenset({"test", "tests", "spec", "specs", "__tests__", "__test__", "__mocks__", "testdata", "docs",
                             "doc", ".github", ".circleci", ".gitlab"})
JVM_SUFFIXES = frozenset({"java", "kt", "kts", "scala", "cs"})

SEED = 20261002
DEFAULTS = {"seed": SEED, "want": 60, "per_repo_max": 3, "gold_min": 2, "gold_max": 6, "statement_min": 150,
            "statement_max": 4000, "size_max_mb": 200, "verify_fetch": True, "languages": None}


def normalize_path(path: str) -> str:
    """A dataset file path as a repository-relative one."""
    p = (path or "").strip().replace("\\", "/")
    if p.startswith("/testbed/"):
        p = p[len("/testbed/"):]
    elif p.startswith("/workspace/"):
        rest = p[len("/workspace/"):].split("/", 1)
        p = rest[1] if len(rest) == 2 else rest[0]
    while p.startswith(("/", "./")):
        p = p[1:] if p.startswith("/") else p[2:]
    return p


def is_test_name(base: str) -> bool:
    stem, _, suffix = base.rpartition(".")
    stem = stem or base
    low = stem.lower()
    if (base == "conftest.py" or low in ("test", "tests") or low.startswith("test_")
            or low.endswith(("_test", "_tests", "_spec", ".test", ".spec"))):
        return True
    return suffix.lower() in JVM_SUFFIXES and stem.endswith(("Test", "Tests"))


def is_gold_path(path: str) -> bool:
    """A programming-language source (or CMake build) file that is not a test, a document or a CI file; see the module
    docstring for the exact rule."""
    p = normalize_path(path)
    if not p:
        return False
    *dirs, base = p.split("/")
    if any(d.lower() in NOT_SOURCE_DIRS for d in dirs):
        return False
    if base == "CMakeLists.txt":
        return True
    if "." not in base or base.rsplit(".", 1)[-1].lower() not in SOURCE_SUFFIXES:
        return False
    return not is_test_name(base)


DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def patch_files(patch: str | None) -> list[str]:
    """The files a unified diff changes that exist before it, in order: modified, deleted and renamed ones (by the old
    path); a file the diff adds is not one. A path in quotes (an unusual character) is not read."""
    files: list[list] = []  # [old path, is new]
    for line in (patch or "").splitlines():
        m = DIFF_HEADER.match(line)
        if m:
            files.append([m.group(1), False])
        elif files and line.startswith("new file mode"):
            files[-1][1] = True
    return [path for path, is_new in files if not is_new]


def gold_files(row: dict) -> list[str]:
    """The distinct source files the instance's gold patch changes (those that exist before it), sorted."""
    return sorted({p for p in (normalize_path(x) for x in patch_files(row.get("patch"))) if p and is_gold_path(p)})


def slug(repo_url: str) -> str:
    s = repo_url.split("github.com/", 1)[-1].strip("/")
    return s.removesuffix(".git")


ID_MAX = 48


def task_id(orig: str, repo: str) -> str:
    """The id a task goes by (and its working folders): the dataset's own when it is short; else the repository's name
    and ten hex digits of the sha256 of the dataset's id (some are 100+ characters, and a deep path on Windows fails)."""
    if len(orig) <= ID_MAX:
        return orig
    return f"{repo.rsplit('/', 1)[-1]}-{hashlib.sha256(orig.encode()).hexdigest()[:10]}"


def split_statement(text: str) -> tuple[str, str]:
    first, _, rest = text.partition("\n")
    first = first.strip()
    if 0 < len(first) <= 200:
        return first, rest.strip()
    return first[:117].rstrip() + "...", text


def select(cfg: dict, rows: list[dict], size_kb: Mapping[str, int | None] | Callable[[str], int | None],
           at_base: Callable[[dict, list[str]], list[str] | None] | None = None) -> tuple[list[dict], dict]:
    """The pre-registered filters, then the seeded order, the per-repository cap and the fetch check.

    ``size_kb``: repository slug -> GitHub size in KB (None when the repository does not exist), a mapping or a function;
    it is asked only for repositories with an instance that passed the cheaper filters. ``at_base(row, gold)``: None when
    the row's base commit cannot be fetched, else the gold files that exist at it; asked only for the instances the
    walk would take. Without it nothing is checked."""
    c = {**DEFAULTS, **cfg}
    sizes = size_kb if callable(size_kb) else size_kb.get
    why: dict[str, list[str]] = {}

    def drop(reason: str, row: dict) -> None:
        why.setdefault(reason, []).append(row["original_inst_id"])

    r_gold = f"gold source files not {c['gold_min']}-{c['gold_max']}"
    r_len = f"statement length outside {c['statement_min']}-{c['statement_max']}"
    r_leak = "statement names a gold file's path"
    r_repo = "repository missing or private (GitHub API 404)"
    r_big = f"repository larger than {c['size_max_mb']} MB"
    r_cap = f"more than {c['per_repo_max']} per repository"
    r_fetch = "base commit not fetchable"
    r_missing = f"gold files existing at the base commit not {c['gold_min']}-{c['gold_max']}"
    missing_files: dict[str, list[str]] = {}
    cheap = []
    for row in rows:
        if c["languages"] and row["language"] not in c["languages"]:
            drop("language not selected", row)
            continue
        gold = gold_files(row)
        text = normalize_statement(row["problem_statement"])
        if not c["gold_min"] <= len(gold) <= c["gold_max"]:
            drop(r_gold, row)
        elif not c["statement_min"] <= len(text) <= c["statement_max"]:
            drop(r_len, row)
        elif leaks(text, gold):
            drop(r_leak, row)
        else:
            cheap.append((row, gold, text))
    ok = []
    for row, gold, text in cheap:
        kb = sizes(slug(row["repo_url"]))
        if kb is None:
            drop(r_repo, row)
        elif kb > c["size_max_mb"] * 1024:
            drop(r_big, row)
        else:
            ok.append((row, gold, text))
    ok.sort(key=lambda t: hashlib.sha256(f"{c['seed']}:{t[0]['original_inst_id']}".encode()).hexdigest())
    seen: Counter = Counter()
    for row, _gold, _text in ok:  # what the cap alone removes, were every base commit fetchable and "want" unlimited
        key = slug(row["repo_url"]).lower()
        seen[key] += 1
        if seen[key] > c["per_repo_max"]:
            drop(r_cap, row)
    taken: list[tuple[dict, list[str], str]] = []
    per_repo: Counter = Counter()
    for row, gold, text in ok:  # the walk: the cap again, then the fetch check, until "want"
        if len(taken) >= c["want"]:
            break
        repo = slug(row["repo_url"]).lower()
        if per_repo[repo] >= c["per_repo_max"]:
            continue
        if at_base is not None:
            present = at_base(row, gold)
            if present is None:
                drop(r_fetch, row)
                continue
            if not c["gold_min"] <= len(present) <= c["gold_max"]:
                drop(r_missing, row)
                continue
            if len(present) < len(gold):
                missing_files[row["original_inst_id"]] = sorted(set(gold) - set(present))
            gold = present
        taken.append((row, gold, text))
        per_repo[repo] += 1
    tasks = []
    for row, gold, text in taken:
        title, body = split_statement(text)
        tasks.append({"id": task_id(row["original_inst_id"], slug(row["repo_url"])), "orig_id": row["original_inst_id"],
                      "issue": None, "via": "contextbench", "ref": row["instance_id"],
                      "title": title, "body": body, "gold": gold, "base_sha": row["base_commit"], "labels": [],
                      "issue_created": None, "fixed_at": None, "remote": row["repo_url"], "repo": slug(row["repo_url"]),
                      "lang": row["language"]})
    info = {"candidates": len(rows), "passed_cheap_filters": len(cheap), "passed_size_filter": len(ok),
            "available_after_cap_and_before_fetch_check": len(ok) - len(why.get(r_cap, [])),
            "dropped": {k: len(v) for k, v in why.items()}, "selected": len(tasks),
            "kept_per_language": dict(sorted(Counter(t["lang"] for t in tasks).items())),
            "kept_per_repository": dict(sorted(Counter(t["repo"] for t in tasks).items())),
            "unfetchable": why.get(r_fetch, []), "gold_files_missing_at_base_in_selected": missing_files, "repositories_missing": why.get(r_repo, []),
            "config": {k: c[k] for k in DEFAULTS}}
    return tasks, info


def normalize_statement(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


# ---- the network side: sizes, fetch check, the parquet -----------------------------------------------------------------

def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.write_bytes((json.dumps(data, indent=1, sort_keys=True) + "\n").encode("utf-8"))


def cached_sizes(cache_path: Path, api=None) -> Callable[[str], int | None]:
    """A size_kb function that asks the GitHub API once per repository and remembers the answer in ``cache_path``."""
    cache = _load_json(cache_path)
    holder = {"api": api}

    def size_kb(repo: str) -> int | None:
        if repo in cache:
            return cache[repo]
        if holder["api"] is None:
            from big_tasks import Api
            holder["api"] = Api()
        data = holder["api"].get(f"repos/{repo}")
        cache[repo] = None if not data else int(data.get("size", 0))
        _save_json(cache_path, cache)
        time.sleep(0.2)
        return cache[repo]

    return size_kb


def git_env() -> dict:
    e = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    e["GIT_TERMINAL_PROMPT"] = "0"  # a private repository must fail, not ask for a password
    return e


def fetch_check(url: str, sha: str, paths: list[str], timeout: int = 300, tries: int = 2) -> list[str] | None:
    """The ``paths`` that exist at ``sha``, or None when the server does not hand ``sha`` out: a depth-1, blob-less
    fetch of it into a scratch repository, then `git ls-tree` (trees only, no blob is downloaded)."""
    for i in range(tries):
        with tempfile.TemporaryDirectory(prefix="cbfetch-") as d:
            try:
                steps = [subprocess.run(argv, cwd=d, env=git_env(), capture_output=True, timeout=timeout, check=False)
                         for argv in (["git", "init", "-q"], ["git", "fetch", "-q", "--depth", "1", "--filter=blob:none", url, sha])]
                ok = all(r.returncode == 0 for r in steps)
                if ok:
                    ls = subprocess.run(["git", "--literal-pathspecs", "ls-tree", "-r", "-z", "--name-only", "FETCH_HEAD", "--", *paths],
                                        cwd=d, env=git_env(), capture_output=True, timeout=timeout, check=False)
                    if ls.returncode == 0:
                        listed = set(ls.stdout.decode("utf-8", "replace").split("\0"))
                        return [p for p in paths if p in listed]
            except subprocess.TimeoutExpired:
                pass
        if i + 1 < tries:
            time.sleep(5)
    return None


def cached_at_base(cache_path: Path, check: Callable[[str, str, list[str]], list[str] | None] = fetch_check
                   ) -> Callable[[dict, list[str]], list[str] | None]:
    cache = _load_json(cache_path)

    def at_base(row: dict, gold: list[str]) -> list[str] | None:
        key = f"{row['repo_url']}@{row['base_commit']}@{','.join(gold)}"
        if key not in cache:
            cache[key] = check(row["repo_url"], row["base_commit"], gold)
            _save_json(cache_path, cache)
        return cache[key]

    return at_base


def read_parquet(path: str) -> list[dict]:
    import pyarrow.parquet as pq  # only in the ContextBench env
    return pq.read_table(path).to_pylist()


def main(argv: list[str] | None = None) -> int:
    a = sys.argv[1:] if argv is None else argv
    if len(a) != 3 or a[0] != "select":
        print(__doc__.split("\n\n")[1].strip())
        return 2
    cfg = json.loads(Path(a[1]).read_text(encoding="utf-8"))
    out = Path(a[2])
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = read_parquet(cfg["parquet"])
    sizes = cached_sizes(out.parent / "repo_sizes.json")
    fetch = cached_at_base(out.parent / "fetchable.json") if cfg.get("verify_fetch", True) else None
    tasks, info = select(cfg, rows, sizes, fetch)
    out.write_bytes((json.dumps({"filters": info, "tasks": tasks}, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
    print(json.dumps({k: v for k, v in info.items() if k not in ("unfetchable", "repositories_missing", "config")}, indent=1))
    print(len(tasks), "tasks ->", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
