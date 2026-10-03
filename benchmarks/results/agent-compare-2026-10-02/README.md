# Agent comparison with a model in the loop, 2026-10-02

Design, written before the first session: `benchmarks/agent_compare/DESIGN.md` (its addendum, written after round 1
was scored and before round 2 ran, adds round 2). Scripts: `benchmarks/agent_compare/{score,usage,paired}.py`.

Setup: 57 questions, 218 gold facts (glow_mod, forge_mod, heldout_repoatlas out of sample; graphify_core,
verinoda_user_tr in sample); one Claude Code subagent session per question and arm (Claude Opus 5.5, high effort),
the arms of a question started together; Verinoda at 5999912, Graphify 0.9.73 (AST graph, no LLM). Answers scored
with `verinoda.benchmark.metrics.score_facts`. Tool use, token counts and gold access are read from the session
transcripts (`usage.json`), not from the sessions' self-reports.

## Round 1: the tool offered ("use it as you see fit")

| arm | facts found | pinpointed | sessions that called the tool |
|---|---|---|---|
| none | 213/218 | 195 | - |
| verinoda | 208/218 | 187 | **6/57** |
| graphify | 207/218 | 190 | **0/57** |

Offered a tool, the agent almost never used it, and it already finds 98 % of the facts alone: the three arms were
nearly the same agent. 5 of the `verinoda` arm's 6 lost facts came from sessions that never called Verinoda. Round 1
measures the agent's own variance, not the tools (the same finding as the 2026-09-28 adoption study: 14 of 25).

## Round 2: the tool used first ("start with it, then verify")

| arm | facts found | pinpointed | all facts | sessions that called the tool | input tokens | output tokens | tool calls |
|---|---|---|---|---|---|---|---|
| none (second sample) | 212/218 | 184 | 51/57 | - | 17,281,704 | 116,404 | 395 |
| verinoda_first | 212/218 | 199 | 52/57 | 57/57 | 14,486,883 | 90,230 | 282 |
| graphify_first | 214/218 | 191 | 53/57 | 57/57 | 16,914,828 | 99,524 | 333 |

## Paired per question (`paired.json`; ratio of summed tokens, 95 % bootstrap interval over questions)

| comparison | facts found | input tokens | output tokens | tool calls |
|---|---|---|---|---|
| none round 1 vs none round 2 (noise floor) | 213 vs 212 | 1.03 [0.95-1.11] | 1.07 [1.00-1.16] | 1.03 [0.94-1.12] |
| **verinoda_first vs none** | 212 vs 212 | **0.84 [0.78-0.91]** | **0.78 [0.72-0.84]** | **0.71 [0.65-0.79]** |
| graphify_first vs none | 214 vs 212 | 0.98 [0.91-1.05] | 0.86 [0.78-0.94] | 0.84 [0.78-0.92] |
| verinoda_first vs graphify_first | 212 vs 214 | 0.86 [0.79-0.92] | 0.91 [0.84-0.98] | 0.85 [0.77-0.92] |

- **Accuracy:** no arm finds more facts than the agent alone; every difference (at most 2 of 218) is inside the
  noise floor. These corpora (37-226 files) are small enough for the agent to read what it needs.
- **Cost:** used first, Verinoda gets the same facts with 16 % fewer input tokens, 22 % fewer output tokens and 29 %
  fewer tool calls than the agent alone; all three intervals exclude 1 and the noise floor's. Graphify used first
  saves output tokens and tool calls but not input tokens. Verinoda first costs 14 % fewer input tokens than
  Graphify first.
- **Pinpointed** (the cited line, not only the file) is 199 for Verinoda first against 184, but the two samples of
  the agent alone differ by 11 (195 vs 184): not separable from noise here.

## Round 3: search results attached before the session (what a mod's auto-context could do)

Verinoda's `query` on the question (`--max-chars 6000`, median 2.2 s) handed to a session with the `none` arm's tools
(delivered as a file read first: one extra tool call and model turn against the arm). `round3/`, `paired.json`.

| comparison | facts found | input tokens | output tokens | tool calls |
|---|---|---|---|---|
| auto_context vs none round 2 | 207 vs 212 (W3/T49/L5) | 0.88 [0.82-0.95] | 1.00 [0.93-1.08] | 0.88 [0.81-0.96] |
| auto_context vs none round 1 | 207 vs 213 (W2/T50/L5) | 0.86 [0.75-0.97] | 0.93 [0.88-0.99] | 0.86 [0.78-0.93] |
| auto_context vs verinoda_first | 207 vs 212 (W2/T51/L4) | 1.05 [0.98-1.14] | 1.29 [1.22-1.36] | 1.23 [1.13-1.34] |

Pre-registered rule (addendum 2): attach search results by default only if this arm finds at least as many facts as
`none` in both samples and costs less. It finds 5-6 fewer (the two `none` samples differ by 1), so it stays off:
ranked leads handed over up front anchor the agent, which then searches less and misses a few facts. Asking the agent
to start with Verinoda itself (round 2's `verinoda_first`) keeps every fact and costs less on every count; that is
what the `verinoda-live` mod's auto mode does (`nudge`), with attached search results kept as an opt-in (`search`).

## Limits

- One session per cell; 57 questions; two of the five sets are in sample.
- Small corpora; questions written for the retrieval benchmark, not for agents. A ceiling effect hides any accuracy
  gain; larger repositories are where it would show.
- Round 2's "use it first" is an instruction, not Verinoda as shipped (its skill and MCP server were not used: the
  arms ran the CLI). Round 1 is the as-offered condition.
- Tokens are the model's reported usage per session (cache reads included in input); no prices are applied.
- The transcripts show no session reading the gold, the question files or another arm's folder.

Files: `sessions.json`, `scores.json`, `summary.md`, `usage.json` (round 1); `round2/` and `round3/` (the same for rounds 2 and 3; `round3/autoctx.json` holds the attached texts);
`paired.json`; `gold.json`, `questions.json`, `prep.json` (the agent-facing questions, the gold and how the corpora
were prepared).
