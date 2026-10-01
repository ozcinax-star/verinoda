"""One MCP tool called through Verinoda's Python API (no MCP client, no SDK): prints the response as JSON.

    python -P mcp_probe.py REPO project_query|analyze QUESTION

Run by run.py as a subprocess, so a crash or a hang of the tool is recorded like any other step.
``analyze`` is called with ``run_tests=False`` and ``observe=False``: the repository's code is never run.
"""

from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    repo, tool, question = argv
    from verinoda.mcp.server import AtlasTools

    t = AtlasTools(repo)
    if tool == "project_query":
        res = t.project_query(question)
    elif tool == "analyze":
        res = t.analyze(question, run_tests=False, observe=False)
    else:
        print(f"unknown tool {tool}", file=sys.stderr)
        return 2
    if isinstance(res, dict) and res.get("error"):
        print(f"tool error: {str(res['error'])[:500]}", file=sys.stderr)
    print(json.dumps(res, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
