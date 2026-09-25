# `verinoda check` after the second review round, 2026-09-25

See docs/BENCHMARKS.md (Update 2026-09-25: name check, second review round) and docs/DESIGN.md D32.
Windows 11, Python 3.12.0, jedi 0.20.0, at the commit that added these files. The machine was
shared with other runs while these were measured, so the elapsed times are noisy.

- `summary.json`: the fixture metrics again (the same 144 probes and runtime oracle as
  `codecheck-2026-09-25`), the reviewers' probe sets before this round (the branch tip it started
  from) and after, the four packages a reviewer chose, and the four clean sets.

In-sample: the fixture was written by the rule author; the review probe sets were written by the
reviewers, and the rules were changed while their results were looked at. The probe files and
harnesses are not in this repository.
