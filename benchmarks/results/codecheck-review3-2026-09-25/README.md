# `verinoda check` after the third review round, 2026-09-25

See docs/BENCHMARKS.md (Update 2026-09-25: name check, third review round) and docs/DESIGN.md D32.
Windows 11, Python 3.12.0, jedi 0.20.0, at the commit that added these files. Other runs shared the
machine while the clean sets ran, so their times are noisy; the `--diff` timing ran on an idle machine,
alternating with the code this round started from.

- `summary.json`: the fixture metrics (the same 144 probes and runtime oracle as
  `codecheck-2026-09-25`), the `--diff` timing before and after, each reproduced finding before and
  after, the reviewers' probe sets and sweeps, the four packages a reviewer chose, and the clean sets.

In-sample: the fixture was written by the rule author; the review probe sets were written by the
reviewers, and the rules were changed while their results were looked at. The probe files and
harnesses are not in this repository.
