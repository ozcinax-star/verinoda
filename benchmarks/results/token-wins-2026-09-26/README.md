# Token wins, 2026-09-26

Per-commit measurements of the `night/token-wins` changes (docs/BENCHMARKS.md, "Update 2026-09-26:
token wins"). Verinoda-only runs on prepared, indexed copies of the eight public sets (86 questions,
319 gold facts); raw search and Graphify were not re-run. Every change here is question-time only
(rendering, response shape, tool registration, budgets), so reusing prepared indexes is valid.

Each approach runs on its own fresh copy of the prepared index, so no approach reuses another's
claims. Scoring: *found* and *pinpointed* are `verinoda.benchmark.runner.score` / `metrics.score_facts`;
*shown* is the gold line's `source.contains` text present in the delivered context
(whitespace-normalised, JSON strings unescaped - the metric `metrics.shown_facts` adds in this
round); tokens are chars/4 (ceil) as the benchmark counts them; `pretok` counts pieces of the cl100k
pre-tokenizer pattern, a lower bound on a BPE count.

Approaches in the `N-*.json` files, `{set: {approach: {found, shown, pin, tokens, pretok, chars,
n_q, total, per_q: {question: {found, pin, shown, tokens, pretok}}}}}`:

- `q_text`: the benchmark's `verinoda_retrieve_text` (what `verinoda query` prints);
- `q_json`: `verinoda_retrieve` (`query --json`);
- `an_bench`: the benchmark's `verinoda_analyze` (from 2-analyze-views on: the default text of
  `verinoda analyze`; before: the `--json` record minus run bookkeeping);
- `mcp_query`, `mcp_analyze`: `AtlasTools.project_query` / `.analyze`, as they go on the wire (the
  text block, else compact JSON);
- `cli_an_text`, `cli_an_json`: `verinoda analyze "<q>"` and `... --json`, captured.

Files:

- `0-main.json`: main at c8da753 (reproduces the token economist's fresh-index numbers exactly:
  query 283/213 at 1,313 tokens per question, analyze 284/219 at 2,349, MCP analyze 272/207 at 2,832);
- `1-query-text.json`: 7e248c6, query text without repeated or decorative text;
- `2-analyze-views.json`: c70f661, analyze's lean MCP response and text output;
- `6a-branch-flag-off.json`: 4f11c90, the branch head (the MCP profile, the skills, the shown metric
  and the flag change no answer: identical to 2 except `cli_an_json`, now compact);
- `6b-shape-budget-on.json`: the branch head with `VERINODA_SHAPE_BUDGET=1` (the question-shape
  budget, not turned on: it loses one shown gold line, heldout h06);
- `7-review-fixes.json`: fd30f5b, the fixes of the review round (the analyze text prints the plan's
  links, MCP analyze recounts hidden claims after its cap, the query note always fits, the follow-up
  names the CLI); compared per fact (the ids in `per_q`, not the totals) with `0-main.json` and
  `6a-branch-flag-off.json`: nothing lost, found or shown, for any approach;
- `oos-*.json`: the same code states on the agent persona's 10 out-of-sample questions (5 on a
  CPython 3.12 standard-library copy, 5 on a Verinoda copy; 32 facts, scored by the persona's regular
  expressions), `{approach: {question: {hits, facts, tokens}}}`; `graphify(saved)` scores the
  persona's saved Graphify answers;
- `fast-*.json`: the fast harness (`{set: {approach: facts found, per_q, negatives}}`) for commit 1,
  the branch head and the review fixes, compared per question with main's run of the same harness:
  only gains (forge q12 +1, heldout h08 +1, for query and analyze; with the review fixes also heldout
  h03 +1 for analyze, the fact the plan's links carry);
- `graphify-baseline-c8da753.txt`: the token economist's fresh-index table at c8da753 that the
  Graphify columns quote (vendored renderer and the upstream CLI 0.9.65); not re-run here;
- `menu-cost.txt`: the standing per-session cost (skills, MCP tools/list and instructions, doctor
  output) at main and at the branch head.
