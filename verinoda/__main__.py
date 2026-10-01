import sys

if sys.argv[1:2] == ["tool-hook"] and sys.argv[2:] in ([], ["--agent", "claude"], ["--agent", "codex"],
                                                       ["--agent", "cursor"]):
    # an agent's hook, once per tool call: the CLI's imports are left out
    from verinoda.tool_hook import run

    print(run(sys.argv[3] if len(sys.argv) > 3 else "claude"))
    sys.exit(0)

from verinoda.cli import main

sys.exit(main())
