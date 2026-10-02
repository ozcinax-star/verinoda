"""What each session of the agent comparison actually ran, read from its transcript (not the self-report): tool
calls, Verinoda and Graphify CLI calls, MCP Verinoda calls, and any reference to the gold, the question files or
another arm's folder.

    python benchmarks/agent_compare/usage.py WORKFLOW_TRANSCRIPT_DIR OUT.json
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# the real-world study runs the build under test from its checkout: that executable is a tool, not a leak
LEAK = re.compile(r"gold\.json|questions\.json|prep\.json|benchmark/questions|benchmarks/results|realworld/gold"
                  r"|verinoda-mod(?!/\.venv/scripts/verinoda\.exe)"
                  r"|(?:agent|rw)/[^/\s\"']+/(?:none|verinoda|graphify)\b")
ARM_DIR = {"none": "none", "verinoda": "verinoda", "graphify": "graphify",
           "verinoda_first": "verinoda", "graphify_first": "graphify", "auto_context": "none"}


def labels(wf_dir: Path) -> dict[str, str]:
    out = {}
    for line in (wf_dir / "journal.jsonl").read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        if d.get("type") == "started":
            out[d["agentId"]] = d["label"]
    return out


def usage(wf_dir: Path) -> dict[str, dict]:
    res = {}
    for aid, label in labels(wf_dir).items():
        files = list(wf_dir.rglob(f"agent-{aid}.jsonl"))
        if not files:
            continue
        study = "rw" if label.startswith("rw ") else "agent"
        key = label.removeprefix("r2 ").removeprefix("r3 ").removeprefix("rw ")
        set_name, rest = key.split("/", 1)
        arm = rest.split(":")[1]
        own = f"{study}/{set_name.lower()}/{ARM_DIR[arm]}"  # the transcript text is lowercased
        u = {"tool_calls": 0, "verinoda_cli": 0, "graphify_cli": 0, "mcp_verinoda": 0, "leaks": []}
        usage_by_msg: dict[str, dict] = {}  # a streamed message is logged on several lines; its last usage counts
        for line in files[0].read_text(encoding="utf-8").splitlines():
            try:
                msg = json.loads(line).get("message") or {}
            except ValueError:
                continue
            if isinstance(msg.get("usage"), dict) and msg.get("id"):
                usage_by_msg[msg["id"]] = msg["usage"]
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for c in content:
                if c.get("type") != "tool_use":
                    continue
                u["tool_calls"] += 1
                text = json.dumps(c.get("input", {})).replace("\\\\", "/").replace("\\", "/").lower()
                u["verinoda_cli"] += "verinoda.exe" in text
                u["graphify_cli"] += "graphify.exe" in text
                u["mcp_verinoda"] += c.get("name", "").startswith("mcp__verinoda")
                for m in LEAK.finditer(text):
                    if m.group(0) != own:
                        u["leaks"].append(m.group(0))
        tok = {k: sum(int(x.get(k) or 0) for x in usage_by_msg.values())
               for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")}
        u["model_calls"] = len(usage_by_msg)
        u["tokens"] = tok
        u["input_total"] = tok["input_tokens"] + tok["cache_creation_input_tokens"] + tok["cache_read_input_tokens"]
        res[key] = u
    return res


def main() -> int:
    wf_dir, out = Path(sys.argv[1]), Path(sys.argv[2])
    res = usage(wf_dir)
    out.write_bytes((json.dumps(res, indent=1, sort_keys=True) + "\n").encode("utf-8"))  # LF on every platform
    arms = sorted({k.split(":")[1] for k in res})
    for arm in arms:
        rows = [v for k, v in res.items() if k.endswith(":" + arm)]
        print(f"{arm}: {len(rows)} sessions, {sum(r['tool_calls'] for r in rows)} tool calls; sessions calling "
              f"verinoda {sum(r['verinoda_cli'] > 0 for r in rows)}, graphify {sum(r['graphify_cli'] > 0 for r in rows)}, "
              f"mcp verinoda {sum(r['mcp_verinoda'] > 0 for r in rows)}; with a leak {sum(bool(r['leaks']) for r in rows)}; "
              f"model calls {sum(r['model_calls'] for r in rows)}, input tokens {sum(r['input_total'] for r in rows):,} "
              f"(cache writes {sum(r['tokens']['cache_creation_input_tokens'] for r in rows):,}), "
              f"output tokens {sum(r['tokens']['output_tokens'] for r in rows):,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
