# `verinoda check` after the review, 2026-09-25

See docs/BENCHMARKS.md (Update 2026-09-25: name check after review) and docs/DESIGN.md D31.
Windows 11, Python 3.12.0, jedi 0.20.0, at the commit that added these files. The machine was
shared with other runs while these were measured, so the elapsed times are noisy.

- `summary.json`: the fixture metrics again (the same 144 probes and runtime oracle as
  `codecheck-2026-09-25`), the reviewers' probe sets of real code with the absent count before
  (their result files, commit 0bc9616) and after, four packages the check had not seen, and the
  four clean sets of the first measurement.

In-sample: the fixture was written by the rule author; the review probe sets were written by the
reviewers, and the rules were changed while their results were looked at. The unseen packages were
chosen by a reviewer. The probe files and harnesses are not in this repository.
