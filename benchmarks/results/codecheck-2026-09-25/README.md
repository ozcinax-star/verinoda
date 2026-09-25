# `verinoda check` measurements, 2026-09-25

See docs/BENCHMARKS.md (Update 2026-09-25: name-existence check) and docs/DESIGN.md D32.
Windows 11, Python 3.12.0, jedi 0.20.0, at the commit that added these files.

- `summary.json`: the fixture metrics (confusion of the runtime oracle against `check`, precision,
  recall on closed containers, decided share, nearest-name hits, the environment-regression bar,
  timings) and the clean-set counts per verdict (with the absent and not_installed sites listed).
- `fixture_rows.json`: every probe - mutation kind, whether its container is closed by the design's
  categories (labelled when the probe was written, not by the tool), the intended real name, the
  oracle's verdict and exception, `check`'s verdict, nearest names and `elsewhere`.

The fixture set is in-sample: the probes were written by the author of the rules, and several rules
changed while the results were looked at. The harness that generated the probes, ran the oracle and
the clean sets is not in this repository; paths in the oracle's messages are shortened to
`<fixture>`.
