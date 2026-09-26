# Verdict audit (wrong 'met')

`verinoda analyze` gives each sub-question a verdict: `met`, `met_with_inference`, `unmet`,
`not_supported`, ... A senior review found `met` on answers that were irrelevant or incomplete:
callers that missed most call sites, env-var reads from a frozen copy of the code, a commit line
given as the reason for a design, definitions given as "the components that use a hook". This set
measures how often that happens.

Run it from a source checkout:

    verinoda benchmark verdict-audit --split dev        # or held_out, all
    verinoda benchmark verdict-audit --split all --work DIR --out result.json

## Cases

`cases.json` has two kinds of cases, on public material only:

- **trap**: a question where `met` was given (or is likely) for a wrong or incomplete answer.
- **control**: a question where `met` is right. Controls measure over-refusal.

Projects: `examples/glow_mod`, `examples/forge_mod`, `examples/orders_app`, three small fixtures under
`tests/fixtures/verdict_audit/` (`pyloop`: Python method calls through `self` and `self._loop`;
`webui`: a function called from a keydown listener and a React hook; `mixmod`: mixins and their
config, an entity without a tick method) and this repository at commit `343a00d` (the part under
`verinoda/`, `tests/`, `docs/` and `benchmarks/corpora/`, which holds a frozen copy of an older
version).

Each case has a `ceiling` (the highest verdict an honest answer can have today: `met`,
`met_with_inference` or `not_met`) and `gold`:

- `must`: strings the answer must contain for `met` to be right (file:line of call sites, names);
- `must_not`: strings that make a `met` wrong (a mixin that *is* listed);
- `met_is_wrong`: the true answer is an absence the tool cannot state ("Wisp has no tick method"),
  so no answer makes `met` right.

The answer is the text and evidence of the claims the sub-questions name as answering.

## Metrics

- `wrong_met`: verdict `met` while the answer misses a `must`, holds a `must_not`, or the case is
  `met_is_wrong`. The main number; `wrong_met_rate` is over all cases of the split.
- `above_ceiling`: verdict above the case's ceiling.
- `controls_kept`: controls judged `met` with a complete answer.

A question's verdict is `met` when all its sub-questions are met, `met_with_inference` when at
least one is met or met with inference, else the first sub-question's verdict.

## Split

The cases were split into `dev` (23) and `held_out` (16) before any verdict rule was changed. Rules
are tuned on `dev` only. The rule author wrote both splits, and the trap shapes come from the
review's list, so held-out is a check on over-fitting to the exact questions, not a blind test.
