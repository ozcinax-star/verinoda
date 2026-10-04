"""Does sending the whole prompt to `verinoda locate` cost the answer anything? (the confirmatory study's diagnosis, no model)

`inject` puts the prompt's text, up to 4,000 characters, to `locate` as the query: the bug report and the harness's own
sentences (the repository's name, the folder it sits in, "read-only", "source code", "answer with the files ..."). This
runs `locate` as the mod's command does on each task's mod copy twice, once with that prompt and once with the bug report
alone (title and body), and counts what each answer lists: the gold files, the files that are not the project's own source
(what Verinoda's setup wrote into the copy, build files, release notes), and how many files in all.

    python benchmarks/agent_compare/query_forms.py CONFIG.json OUT.json

CONFIG: the study's run config (`tasks`, `copy_pattern`, `project`) plus {"verinoda": the executable, "arm": "inject" (the arm
whose copy and prompt are used), "max_chars": 1800, "limit": n tasks or null}. The daemon of each copy is started, asked twice
and stopped, as the mod does it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import big_run
from delivery_funnel import LOCATE_LINE

CODE = {"c", "h", "cc", "cpp", "cxx", "hpp", "hh", "py", "js", "jsx", "mjs", "ts", "tsx", "go", "rs", "java", "kt", "scala", "s",
        "rb", "php", "cs", "swift", "m", "mm", "lua", "ex", "exs", "dart", "svelte", "vue", "sh", "pyi"}
OWN = re.compile(r"(^|/)(\.mcp\.json|\.claude/|claude\.md$|agents\.md$|\.verinoda/|\.graphify|graphify-out/)", re.IGNORECASE)


def listed_paths(text: str) -> list[str]:
    """The files an answer's text lists, in order, once each (the lines `locate` prints with a path in them)."""
    out: list[str] = []
    for ln in text.splitlines():
        m = LOCATE_LINE.match(ln)
        if m and ("/" in m.group(1) or "." in m.group(1)) and m.group(1) not in out:
            out.append(m.group(1))
    return out


def kind(path: str) -> str:
    """`own` (Verinoda's or Graphify's files in the copy), `code`, or `other` (build files, release notes, docs)."""
    if OWN.search(path):
        return "own"
    name = path.rsplit("/", 1)[-1]
    return "code" if "." in name and name.rsplit(".", 1)[-1].lower() in CODE else "other"


def score_answer(text: str, gold: list[str]) -> dict:
    paths = listed_paths(text)
    kinds = [kind(p) for p in paths]
    return {"listed": len(paths), "gold_listed": len(set(paths) & set(gold)), "code": kinds.count("code"),
            "other": kinds.count("other"), "own": kinds.count("own"), "paths": paths}


def command(exe: str, repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)


def answer_text(exe: str, repo: str, text: str, max_chars: int) -> str:
    p = command(exe, repo, "locate", "--repo", repo, "--json", "--max-chars", str(max_chars), "--", text)
    try:
        return str(json.loads(p.stdout).get("text") or "")
    except ValueError:
        return ""


def total(rows: list[dict], form: str) -> dict:
    cells = [r[form] for r in rows]
    return {"tasks": len(cells), "gold_files": sum(len(r["gold"]) for r in rows), "gold_listed": sum(c["gold_listed"] for c in cells),
            "tasks_with_a_gold_file_listed": sum(c["gold_listed"] > 0 for c in cells),
            "tasks_listing_nothing": sum(c["listed"] == 0 for c in cells), "files_listed": sum(c["listed"] for c in cells),
            "code": sum(c["code"] for c in cells), "other": sum(c["other"] for c in cells), "own": sum(c["own"] for c in cells)}


def main() -> int:
    cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    tasks = json.loads(Path(cfg["tasks"]).read_text(encoding="utf-8"))["tasks"][: cfg.get("limit")]
    exe, arm, cap = cfg["verinoda"], cfg.get("arm", "inject"), int(cfg.get("max_chars", 1800))
    rows = []
    for t in tasks:
        copy = big_run.copy_of(cfg, t, arm)
        repo = str(copy)
        prompt = big_run.prompt_for(t, copy, cfg.get("project"), "")[:4000]
        report = f"{t['title']}\n\n{t['body']}"[:4000]
        command(exe, repo, "locate", "--daemon", "stop", "--repo", repo, "--json")
        command(exe, repo, "locate", "--daemon", "start", "--repo", repo, "--json")
        whole, alone = answer_text(exe, repo, prompt, cap), answer_text(exe, repo, report, cap)
        command(exe, repo, "locate", "--daemon", "stop", "--repo", repo, "--json")
        rows.append({"id": t["id"], "gold": t["gold"], "prompt": score_answer(whole, t["gold"]), "report": score_answer(alone, t["gold"])})
        print(f"{t['id']}: prompt {rows[-1]['prompt']['gold_listed']}/{len(t['gold'])} of {rows[-1]['prompt']['listed']} listed | "
              f"report {rows[-1]['report']['gold_listed']}/{len(t['gold'])} of {rows[-1]['report']['listed']} listed", flush=True)
    out = {"summary": {"prompt": total(rows, "prompt"), "report": total(rows, "report")}, "rows": rows}
    Path(sys.argv[2]).write_bytes((json.dumps(out, indent=1) + "\n").encode("utf-8"))
    print(json.dumps(out["summary"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
