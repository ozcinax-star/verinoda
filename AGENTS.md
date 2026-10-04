# Working in this repository (for coding agents)

Verinoda is a Python package (`verinoda/`, the `verinoda` CLI) that answers questions about a codebase with claims that
carry `file:line` evidence and a status. `claude-mods/verinoda-live/` is a Claude Code mod (TypeScript function hooks) that
runs beside the agent in a session.

## Where things are

- `docs/ARCHITECTURE.md`: how the package is laid out. `docs/DESIGN.md`: one numbered section per shipped decision
  (`D1` ... `D174`), each with why, what was decided, what was measured and what was not done.
- `docs/BACKLOG.md`: what is to do. `verinoda backlog 13.9` prints one item.
- `docs/OZELLIKLER-RAPORU.md`: every design section in three lines (Turkish).
- `benchmarks/`: the harnesses and their results. `benchmarks/results/**` is measured data.
- **The mod: read `claude-mods/verinoda-live/AGENTS.md` before touching that folder.** The feature to build next is
  specified in `claude-mods/verinoda-live/IMPROVE-PANE.md`.

## Rules

- **Never edit, regenerate or reformat anything under `benchmarks/results/`.** It is what was measured.
- **Lint only the files you changed**: `ruff check` followed by those files. Never `ruff --fix` or a formatter over a folder:
  a broad fix once rewrote 88 result files.
- Python tests: `.venv/Scripts/python.exe -m pytest tests/test_<area>.py -q` for what you changed (`pytest tests` is the whole
  suite). Two tests failed before your change and are not yours: `tests/test_agents.py::test_skill_templates_are_complete`
  for `claude` and for `codex` (a template is 243 and 242 lines long where the test allows fewer than 240). Check on a clean
  checkout before you blame your change for any other failure.
- When a backlog item ships: add a section to `docs/DESIGN.md` under the next `D` number (why, decisions, measured, not done,
  tests), add a note to `docs/UPGRADING.md`, and delete the item's row from `docs/BACKLOG.md` (a row whose item shipped only
  in part is cut down to what is left). Code comments cite the `D` number, never a backlog id.
- Say what was measured and what was not. A result that was not measured is written "not measured", never implied. A test
  that fails is reported with its output.
- Write code that reads like the code around it: its comment density, its naming, its idiom. Comments say why, not what.
- Do not commit `.claude/`, `p.out` or anything under a `.venv`. Do not push, and do not open a pull request, unless the person
  asks. When you commit, use the author identity the last commits carry (`git log -1 --format='%an <%ae>'`).
- This is a Windows machine: paths may hold backslashes and spaces, and a Python or a shell here-document that carries
  backslashes can lose them. Write files with your file tools, not through a shell string.
