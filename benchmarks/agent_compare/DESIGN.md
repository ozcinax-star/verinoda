# Agent comparison with a model in the loop: pre-registration (2026-10-02)

Written before the first session of this study ran. The adoption study of 2026-09-28 (docs/DESIGN.md section
35.3) kept neither its harness nor its questions; this one is new and smaller in its claims.

## Question

On the same questions and the same corpus, does a coding agent answer with more of the gold facts when it can use
Verinoda, or Graphify, than with its own file reading and search alone?

## Corpus and questions

The five public question sets whose gold was checked against their corpus by the benchmark harness, with
`orders_app` (11 files) and the two Turkish paraphrase sets of other sets left out:

| set | questions | facts | in sample (Verinoda developed against it) |
|---|---:|---:|---|
| glow_mod | 14 | 50 | no |
| forge_mod | 14 | 68 | no |
| heldout_repoatlas | 8 | 33 | no (held out) |
| graphify_core | 9 | 37 | yes |
| verinoda_user_tr | 12 | 30 | yes (Turkish questions) |

57 questions, 218 facts. Per set, three copies of the same files are prepared by `verinoda.benchmark.approaches.prepare_workdir`
(the runner's own corpus copy, pins applied): `none`, `verinoda` (indexed as the runner indexes, the set's
`verinoda_config` applied) and `graphify` (`graphify update .`, Graphify 0.9.73, AST only, no LLM). Known gold defect,
recorded at preparation: `verinoda_user_tr` u01.detect's text alternative `copies.detect` occurs nowhere in its
pinned corpus; its location alternative is valid and is what can match.

## Arms

One session per question and arm, the three arms of a question started together; the model and effort are the
session's own (Claude Opus 5.5, the effort the parent session runs at), the same for every arm.

- `none`: file reading and search only (Read, Grep, Glob, shell `ls`/`cat`/`grep`/`git grep`).
- `verinoda`: the same, plus the Verinoda CLI (build 5999912) on the prepared index: `query` and `analyze`.
- `graphify`: the same, plus the Graphify CLI 0.9.73 on its graph: `query`, `explain`, `path`.

Every arm is told to stay inside its corpus folder and not to use the other tools (the session's own Verinoda MCP
server points at another repository). Compliance is self-reported, not enforced: a stated limitation.

## Answer and scoring

Each session returns its final answer, which must cite code as `path:line` or `path:start-end`. The answer text is
scored with the benchmark's own `verinoda.benchmark.metrics.score_facts` against the set's gold (found, pinpointed,
shown), exactly as delivered context is scored. Nothing else is scored.

- **Primary:** facts found per arm, summed and per question; paired per question (verinoda vs none, graphify vs none,
  verinoda vs graphify): wins / ties / losses and the sum of differences.
- **Secondary:** pinpointed facts; out-of-sample sets alone; answer size (chars/4); self-reported tool calls and
  index-tool calls.
- **Not measured:** model tokens and cost (the harness does not expose them per session), wall time per session
  (sessions share the machine with other benchmark runs), any repeat (one session per question and arm; a second
  round is run only if the first leaves the primary comparison within a few facts).

Stated before running: one session per cell makes per-question differences noisy; the totals over 57 questions are
the claim, the per-set rows are descriptive.

## Addendum (written after round 1 was scored, before round 2 ran)

Round 1 (`sessions.json`): the agent alone found 213 of 218 facts; the transcripts show Verinoda called in 6 of 57
`verinoda` sessions and Graphify in 0 of 57 `graphify` sessions, so the three arms were nearly the same agent and
their differences (5 of the `verinoda` arm's 6 lost facts came from sessions that never called Verinoda) measure
session-to-session variance, not the tools. A plain repeat would measure that variance again. Round 2 instead
(exploratory, labelled as such in the results):

- `none` again, unchanged: a second sample of the same arm, so none-vs-none gives the noise floor;
- `verinoda-first`: the `verinoda` arm's prompt plus "Start by running Verinoda's analyze on the question (and query
  for names it surfaces) before any other search; then verify and complete with your own reading as needed.";
- `graphify-first`: the same sentence for Graphify's query (and explain for names it surfaces).

Scored exactly as round 1. Use of the tool is checked in the transcripts, not taken from the self-report.

## Addendum 2 (written after round 2 was scored, before round 3 ran)

Round 3 measures what the `verinoda-live` mod's auto-context would do: Verinoda's `query` (not `analyze`, which took
up to 55 s on a large repository and is too slow to run before a prompt is sent) is run on the question beforehand,
and its text output (`--max-chars 6000`; median 2.2 s, at most 4.5 s here) is attached to the prompt as context. The
session has the `none` arm's tools only (no Verinoda command), so the context is the only difference from `none`.
One arm, `auto_context`, 57 sessions; compared with round 1's and round 2's `none` (two samples of the same arm),
scored and paired exactly as before. Delivery: the context is written to a file outside the corpus that the session is told to read first (the harness cannot put 57 x 6,000 characters into prompts), which adds one tool call and one model turn against the arm. The mod's default stays off unless this arm finds at least as many facts as
`none` in both samples and costs less.
