"""``python -m repoatlas.benchmark``: run a set with progress, or print Markdown tables.

    python -m repoatlas.benchmark run --repo PATH [--questions Q] [--out FILE]
                                      [--graphify-cmd EXE] [--llm none|anthropic] [--repeat N]
    python -m repoatlas.benchmark markdown RESULTS.json
    python -m repoatlas.benchmark sanitize RESULTS.json [...]

``sanitize`` rewrites existing result files (and their delivered contexts) with
machine paths replaced by placeholders, as ``run --out`` already does; run it on
the machine that produced them.

Same engine as ``repoatlas benchmark run``; this entry adds progress lines on
stderr. (The indexer may start worker processes; running as ``-m`` keeps that
safe on Windows.)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="python -m repoatlas.benchmark")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--repo", required=True)
    r.add_argument("--questions")
    r.add_argument("--out")
    r.add_argument("--graphify-cmd")
    r.add_argument("--llm", choices=["none", "anthropic"], default="none")
    r.add_argument("--repeat", type=int, default=2)
    r.add_argument("--keep-workdir", action="store_true")
    r.add_argument("--json", action="store_true")
    m = sub.add_parser("markdown")
    m.add_argument("results")
    z = sub.add_parser("sanitize")
    z.add_argument("results", nargs="+")
    args = p.parse_args(argv)

    from repoatlas.benchmark import markdown, render, run_benchmark

    if args.cmd == "markdown":
        print(markdown(json.loads(Path(args.results).read_text(encoding="utf-8"))))
        return 0
    if args.cmd == "sanitize":
        from repoatlas.benchmark.sanitize import sanitize_result_file

        for f in args.results:
            print(json.dumps(sanitize_result_file(Path(f))))
        return 0
    res = run_benchmark(Path(args.repo), questions=args.questions, out=args.out, graphify_cmd=args.graphify_cmd,
                        llm=args.llm, repeat=args.repeat, keep_workdir=args.keep_workdir,
                        progress=lambda msg: print(f"[bench] {msg}", file=sys.stderr, flush=True))
    if args.json:
        print(json.dumps(res, indent=1, ensure_ascii=False, default=str))
    else:
        render(res)
    return 0 if res.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
