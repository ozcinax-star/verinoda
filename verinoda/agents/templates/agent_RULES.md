# Verinoda: evidence-first answers about this codebase

Use Verinoda before explaining unfamiliar code here: how a feature works, where something is implemented,
how calls or data flow, what a change would affect, or whether an earlier conclusion still holds.

- With the `verinoda` MCP tools: `project_query` (where is X; hits are leads, not answers), `analyze`
  (claims with `path:line` evidence), `index_update` after editing, `code_check` on code you wrote or
  edited, `run_tool` for node_inspect, relation_trace, map_view, claim_list and change_review.
- Without them, run the CLI (the command that runs this build: `{{VERINODA_CLI}}`):

```bash
verinoda query "where is the order total computed?"
verinoda analyze "how does checkout reach the payment service?"
verinoda update .
```

Answer rules:

- Every statement about the code is a claim with a status and `path:line` evidence. Statuses:
  `statically_verified`, `observed`, `experiment_verified`, `primary_source_verified` (verified at the
  index's snapshot, with that evidence), `strong_inference`, `weak_inference`, `unknown`, `contradicted`,
  `stale`.
- Never present `weak_inference` or `unknown` as fact; for `unknown` give the next step instead of a guess.
- Graph edges are extractions, not verification. Scores, rankings and name matches are inference at most.
- Cite the narrowest lines that support a claim. Run `verinoda update .` after big edits, and
  `verinoda doctor --brief` if anything looks wrong.
