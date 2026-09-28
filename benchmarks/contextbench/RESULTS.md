# ContextBench no-model comparison: results

Run 2026-09-28 02:53-08:48 +03:00. 80 instances of ContextBench `contextbench_verified` (10 per language, seed
20260928), four arms, one shot each, the issue text as the question, about 6,000 characters (1,500 tokens) of
output each, scored with ContextBench's own evaluator. No model anywhere. Pre-registration, with every amendment and
its time: [DESIGN.md](DESIGN.md). Everything below reports against it.

Arms: **Verinoda query** = `verinoda query --max-chars 6000` (freeze 4); **Verinoda analyze** = `verinoda analyze`
(default text); **Graphify query** = `graphify query --budget 2000` (0.9.69, after `graphify update .`);
**BM25 baseline** = BM25 over tracked code files, best 30-line window per top file. Two views of every output:
**CITED** = every file and `path:a-b` location the text names (pointers an agent could follow); **SHOWN** = the
source lines actually printed (Graphify prints none, so its view is one line per node, as the task defines). Primary
numbers use the first 6,000 characters of every output ("capped"); "uncapped" is secondary.
R = recall ("coverage" in ContextBench), P = precision, micro-averaged as the evaluator aggregates; span = the
evaluator's byte-level span. Empty or failed outputs count as zero, never dropped.

## Answer first

| capped, micro, N = 80 | Verinoda query | Verinoda analyze | Graphify query | BM25 baseline |
|---|---|---|---|---|
| CITED file recall | **0.306** | 0.220 | 0.210 | 0.167 |
| CITED file precision | 0.098 | 0.094 | 0.037 | **0.156** |
| CITED file F1 | 0.149 | 0.131 | 0.063 | **0.161** |
| CITED span recall / precision / F1 | **0.197** / 0.035 / 0.059 | 0.102 / **0.080** / **0.090** | 0.004 / 0.026 / 0.007 | 0.028 / 0.057 / 0.037 |
| SHOWN span recall / precision / F1 | 0.025 / **0.119** / **0.042** | 0.008 / 0.109 / 0.014 | 0.004 / 0.026 / 0.007 | **0.028** / 0.057 / 0.037 |
| file recall, mean over instances (CITED) | **0.570** | 0.453 | 0.404 | 0.370 |
| delivered tokens, mean (chars/4) | 1,396 | 1,405 | 1,481 | 1,488 |
| CITED file recall per 1k tokens | **0.219** | 0.156 | 0.142 | 0.112 |
| index build, median / p90 / max (s) | 132 / 599 / 2,451 | (same index) | 53 / 286 / 1,342 | 5 / 54 / 429 |
| query latency, median / p90 (s) | 6.6 / 20.2 | 15.2 / 62.2 | 6.2 / 17.9 | 0.03 / 0.13 |

Analyze's mean of 1,405 tokens includes its 4 exit-2 outputs at 0 characters; over the other 76 it is 1,479
tokens, and its CITED file recall per 1k tokens would be 0.149 instead of 0.156.

1. **Localization (where to look): Verinoda query is first.** At the same budget it names at least one gold file on
   66 of 80 instances (Graphify 54, BM25 50), and its CITED file recall is higher than BM25's on 36 instances,
   equal on 41, lower on 3; against Graphify 33 / 38 / 9. It is first in 6 languages, tied first with analyze in Java; in
   JavaScript analyze is higher and query ties Graphify.
2. **But it points at many files, so its file precision is low**: 0.098 against BM25's 0.156 (BM25 shows 5 files per answer on
   average, Verinoda query names 15, about 10 of them only as pointers in lead lines and caller lists, Graphify
   28). At file F1 BM25 edges it overall (0.161 vs 0.149; per instance 33 BM25
   wins, 31 Verinoda wins, 16 ties). Graphify is last on file precision (0.037): it names the most files, and on
   average 13 % of the files in an answer are not code (CHANGELOG, docs, package.json).
3. **Code actually delivered (SHOWN spans) is poor for every tool**: under 3 % of the gold bytes at 1,500 tokens.
   Verinoda query and BM25 are level (F1 0.042 vs 0.037; recall 0.025 vs 0.028, precision 0.119 vs 0.057);
   Graphify prints no source (0.007); analyze delivers almost none inside the cap (0.014) because its passages start
   after character 6,000 on 27 of 80 instances.
4. **Pointer precision: analyze's claims cite the narrowest correct lines** (CITED span precision 0.080, F1 0.090,
   the best), but it finds fewer of them than query (recall 0.102 vs 0.197). Uncapped (mean 2,625 tokens, 1.75x the
   budget) analyze reaches CITED file recall 0.328, slightly above query's 0.306, at 57 % of query's recall per
   token.
5. **Cost**: Verinoda's index build is about 2.5x Graphify's (median 132 s vs 53 s) and much slower on large
   repositories (material-ui 25-38 min against Graphify's 8-22 min; jackson-databind-3701, whose 691 MB checkout is 99 %
   `docs/` javadoc HTML, 41 min against 11 min). All three tools
   answer in seconds; BM25 in milliseconds.

Absolute levels are low for everything (the best macro file recall is 0.57; ContextBench's published agent runs reach
0.6-0.75 file recall, but those are multi-step agent trajectories with far more tokens, not one 1,500-token shot, and
are not comparable).

## Tables

All tables: capped unless the title says otherwise; micro unless it says macro. `empty` = instances whose prediction
had no file at all. Full per-instance numbers: `results.json` (`per_instance`), evaluator rows:
`evaluator_rows.jsonl`, predictions: `predictions.jsonl`, raw tool outputs: `out/raw/<id>.json`.

### Overall, CITED, capped (N = 80)

| arm | file R | file P | file F1 | span R | span P | span F1 | line R | symbol R | tokens mean | file R /1k tok | span R /1k tok | empty |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Verinoda query | 0.306 | 0.098 | 0.149 | 0.197 | 0.035 | 0.059 | 0.180 | 0.225 | 1396 | 0.219 | 0.141 | 0 |
| Verinoda analyze | 0.220 | 0.094 | 0.131 | 0.102 | 0.080 | 0.090 | 0.096 | 0.124 | 1405 | 0.156 | 0.073 | 4 |
| Graphify query | 0.210 | 0.037 | 0.063 | 0.004 | 0.026 | 0.007 | 0.003 | 0.048 | 1481 | 0.142 | 0.003 | 1 |
| BM25 baseline | 0.167 | 0.156 | 0.161 | 0.028 | 0.057 | 0.037 | 0.023 | 0.059 | 1488 | 0.112 | 0.019 | 0 |

### Overall, CITED, uncapped (N = 80)

| arm | file R | file P | file F1 | span R | span P | span F1 | line R | symbol R | tokens mean | file R /1k tok | span R /1k tok | empty |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Verinoda query | 0.306 | 0.098 | 0.149 | 0.197 | 0.035 | 0.059 | 0.180 | 0.225 | 1396 | 0.219 | 0.141 | 0 |
| Verinoda analyze | 0.328 | 0.075 | 0.123 | 0.220 | 0.036 | 0.062 | 0.203 | 0.240 | 2625 | 0.125 | 0.084 | 4 |
| Graphify query | 0.278 | 0.033 | 0.059 | 0.005 | 0.022 | 0.009 | 0.004 | 0.062 | 2105 | 0.132 | 0.003 | 0 |
| BM25 baseline | 0.167 | 0.156 | 0.161 | 0.028 | 0.057 | 0.037 | 0.023 | 0.059 | 1488 | 0.112 | 0.019 | 0 |

### Overall, SHOWN, capped (N = 80)

| arm | file R | file P | file F1 | span R | span P | span F1 | line R | symbol R | tokens mean | file R /1k tok | span R /1k tok | empty |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Verinoda query | 0.187 | 0.192 | 0.190 | 0.025 | 0.119 | 0.042 | 0.021 | 0.064 | 1396 | 0.134 | 0.018 | 0 |
| Verinoda analyze | 0.076 | 0.185 | 0.108 | 0.008 | 0.109 | 0.014 | 0.006 | 0.022 | 1405 | 0.054 | 0.005 | 36 |
| Graphify query | 0.210 | 0.037 | 0.063 | 0.004 | 0.026 | 0.007 | 0.003 | 0.048 | 1481 | 0.142 | 0.003 | 1 |
| BM25 baseline | 0.167 | 0.156 | 0.161 | 0.028 | 0.057 | 0.037 | 0.023 | 0.059 | 1488 | 0.112 | 0.019 | 0 |

### Overall, SHOWN, uncapped (N = 80)

| arm | file R | file P | file F1 | span R | span P | span F1 | line R | symbol R | tokens mean | file R /1k tok | span R /1k tok | empty |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Verinoda query | 0.187 | 0.192 | 0.190 | 0.025 | 0.119 | 0.042 | 0.021 | 0.064 | 1396 | 0.134 | 0.018 | 0 |
| Verinoda analyze | 0.179 | 0.191 | 0.185 | 0.025 | 0.126 | 0.042 | 0.021 | 0.063 | 2625 | 0.068 | 0.010 | 4 |
| Graphify query | 0.278 | 0.033 | 0.059 | 0.005 | 0.022 | 0.009 | 0.004 | 0.062 | 2105 | 0.132 | 0.003 | 0 |
| BM25 baseline | 0.167 | 0.156 | 0.161 | 0.028 | 0.057 | 0.037 | 0.023 | 0.059 | 1488 | 0.112 | 0.019 | 0 |

### Per language, CITED file recall (capped, micro)

| language | n | Verinoda query | Verinoda analyze | Graphify query | BM25 baseline |
|---|---|---|---|---|---|
| c | 10 | 0.485 | 0.455 | 0.333 | 0.242 |
| cpp | 10 | 0.513 | 0.359 | 0.333 | 0.282 |
| go | 10 | 0.696 | 0.522 | 0.565 | 0.435 |
| java | 10 | 0.333 | 0.333 | 0.303 | 0.242 |
| javascript | 10 | 0.200 | 0.229 | 0.200 | 0.114 |
| python | 10 | 0.538 | 0.192 | 0.269 | 0.346 |
| rust | 10 | 0.403 | 0.226 | 0.258 | 0.210 |
| typescript | 10 | 0.083 | 0.055 | 0.041 | 0.021 |
| overall | 80 | 0.306 | 0.220 | 0.210 | 0.167 |

### Per language, CITED file precision (capped, micro)

| language | n | Verinoda query | Verinoda analyze | Graphify query | BM25 baseline |
|---|---|---|---|---|---|
| c | 10 | 0.123 | 0.150 | 0.051 | 0.157 |
| cpp | 10 | 0.129 | 0.115 | 0.060 | 0.229 |
| go | 10 | 0.099 | 0.096 | 0.028 | 0.167 |
| java | 10 | 0.097 | 0.092 | 0.046 | 0.174 |
| javascript | 10 | 0.043 | 0.069 | 0.022 | 0.057 |
| python | 10 | 0.117 | 0.060 | 0.033 | 0.200 |
| rust | 10 | 0.184 | 0.125 | 0.061 | 0.260 |
| typescript | 10 | 0.048 | 0.053 | 0.018 | 0.058 |
| overall | 80 | 0.098 | 0.094 | 0.037 | 0.156 |

### Per language, CITED span F1 (capped, micro)

| language | n | Verinoda query | Verinoda analyze | Graphify query | BM25 baseline |
|---|---|---|---|---|---|
| c | 10 | 0.036 | 0.139 | 0.007 | 0.015 |
| cpp | 10 | 0.095 | 0.141 | 0.021 | 0.053 |
| go | 10 | 0.070 | 0.140 | 0.003 | 0.049 |
| java | 10 | 0.068 | 0.125 | 0.005 | 0.061 |
| javascript | 10 | 0.077 | 0.061 | 0.001 | 0.024 |
| python | 10 | 0.070 | 0.071 | 0.004 | 0.056 |
| rust | 10 | 0.076 | 0.035 | 0.008 | 0.045 |
| typescript | 10 | 0.017 | 0.009 | 0.002 | 0.003 |
| overall | 80 | 0.059 | 0.090 | 0.007 | 0.037 |

### Per language, SHOWN file recall (capped, micro)

| language | n | Verinoda query | Verinoda analyze | Graphify query | BM25 baseline |
|---|---|---|---|---|---|
| c | 10 | 0.364 | 0.212 | 0.333 | 0.242 |
| cpp | 10 | 0.256 | 0.154 | 0.333 | 0.282 |
| go | 10 | 0.522 | 0.174 | 0.565 | 0.435 |
| java | 10 | 0.273 | 0.152 | 0.303 | 0.242 |
| javascript | 10 | 0.086 | 0.029 | 0.200 | 0.114 |
| python | 10 | 0.346 | 0.000 | 0.269 | 0.346 |
| rust | 10 | 0.194 | 0.032 | 0.258 | 0.210 |
| typescript | 10 | 0.048 | 0.034 | 0.041 | 0.021 |
| overall | 80 | 0.187 | 0.076 | 0.210 | 0.167 |

### Per language, SHOWN span recall (capped, micro)

| language | n | Verinoda query | Verinoda analyze | Graphify query | BM25 baseline |
|---|---|---|---|---|---|
| c | 10 | 0.029 | 0.013 | 0.004 | 0.011 |
| cpp | 10 | 0.016 | 0.007 | 0.012 | 0.035 |
| go | 10 | 0.066 | 0.028 | 0.002 | 0.051 |
| java | 10 | 0.037 | 0.011 | 0.003 | 0.041 |
| javascript | 10 | 0.007 | 0.000 | 0.001 | 0.017 |
| python | 10 | 0.042 | 0.000 | 0.002 | 0.050 |
| rust | 10 | 0.037 | 0.000 | 0.005 | 0.040 |
| typescript | 10 | 0.009 | 0.006 | 0.001 | 0.002 |
| overall | 80 | 0.025 | 0.008 | 0.004 | 0.028 |

### Per language, SHOWN span F1 (capped, micro)

| language | n | Verinoda query | Verinoda analyze | Graphify query | BM25 baseline |
|---|---|---|---|---|---|
| c | 10 | 0.047 | 0.024 | 0.007 | 0.015 |
| cpp | 10 | 0.029 | 0.014 | 0.021 | 0.053 |
| go | 10 | 0.082 | 0.049 | 0.003 | 0.049 |
| java | 10 | 0.066 | 0.021 | 0.005 | 0.061 |
| javascript | 10 | 0.012 | 0.000 | 0.001 | 0.024 |
| python | 10 | 0.060 | 0.000 | 0.004 | 0.056 |
| rust | 10 | 0.054 | 0.000 | 0.008 | 0.045 |
| typescript | 10 | 0.016 | 0.012 | 0.002 | 0.003 |
| overall | 80 | 0.042 | 0.014 | 0.007 | 0.037 |

### Macro (mean over instances), CITED, capped

| arm | file R | file P (non-empty) | span R | span P (non-empty) | span F1 | empty preds |
|---|---|---|---|---|---|---|
| Verinoda query | 0.570 | 0.126 | 0.383 | 0.070 | 0.082 | 0 |
| Verinoda analyze | 0.453 | 0.109 | 0.205 | 0.100 | 0.090 | 4 |
| Graphify query | 0.404 | 0.046 | 0.007 | 0.022 | 0.007 | 1 |
| BM25 baseline | 0.370 | 0.165 | 0.078 | 0.057 | 0.042 | 0 |

### Macro (mean over instances), SHOWN, capped

| arm | file R | file P (non-empty) | span R | span P (non-empty) | span F1 | empty preds |
|---|---|---|---|---|---|---|
| Verinoda query | 0.403 | 0.247 | 0.072 | 0.113 | 0.050 | 0 |
| Verinoda analyze | 0.144 | 0.259 | 0.020 | 0.104 | 0.016 | 36 |
| Graphify query | 0.404 | 0.046 | 0.007 | 0.022 | 0.007 | 1 |
| BM25 baseline | 0.370 | 0.165 | 0.078 | 0.057 | 0.042 | 0 |

### Timings (wall seconds, measured with 3 instances in flight on a shared machine)

| step | median | p90 | max | sum |
|---|---|---|---|---|
| verinoda_scan | 132.030 | 598.770 | 2451.320 | 24186.530 |
| graphify_update | 53.360 | 285.780 | 1342.310 | 11247.660 |
| bm25_index | 5.310 | 53.600 | 428.830 | 2606.940 |
| vq_query | 6.586 | 20.192 | 48.288 | - |
| va_query | 15.234 | 62.158 | 143.603 | - |
| gq_query | 6.197 | 17.899 | 31.972 | - |
| bm25_query | 0.029 | 0.125 | 0.257 | - |

ceiling (gold entry, micro recall): file 0.987, span 1.000, line 0.994, symbol 1.000
failures: {"va_nonzero": ["scikit-learn__scikit-learn-25931", "yt-dlp__yt-dlp-5933", "tokio-rs__bytes-721", "microsoft__vscode-135197"], "va_empty_text": ["scikit-learn__scikit-learn-25931", "yt-dlp__yt-dlp-5933", "tokio-rs__bytes-721", "microsoft__vscode-135197"]}
not ok: {}

## Skips, failures, replacements

- Sample: 80 of 80 as pre-registered. **0 skipped** by the 1.5 GB rule (largest checkout 691 MB), **0 fetch failures,
  0 replacements**, 0 runner crashes, 17/17 evaluator rows on every instance, evaluator exit 0 on every instance.
- **Verinoda query**: exit 0 on 80/80. **Graphify** update and query: exit 0 on 80/80. **Verinoda scan**: exit 0 on
  80/80, no index timeout (the longest took 41 min of the 60 allowed). **BM25**: 80/80.
- **Verinoda analyze exited 2 on 4/80** (scikit-learn-25931, yt-dlp-5933, tokio-rs bytes-721, vscode-135197) with
  `the plan is invalid; nothing was analysed`: the issue names versions (e.g. `3.1.0`, `0.3.20`) and analyze's
  reference check refuses to answer until each version is carried by a reference. These score as empty, as
  pre-registered. The pre-registered rule "non-zero exit = empty text" happened not to matter: none of the four
  outputs contains a single file location, so scoring their stdout gives the same zeros.
- Graphify's capped prediction is empty on 1/80 (huggingface transformers-13989): its header line (the list of seed
  nodes) was longer than 6,000 characters, so the cap held only the header and nodes with an empty `src=`.
- The evaluator rejected predictions with no files (`no_context_extracted`): the empty cases above and 36 capped
  analyze SHOWN views (the 4 above included) whose first 6,000 characters print no source line; all count as zero.
- Ceiling (the gold context submitted as a prediction): file recall 0.987, span 1.000; the missing files are
  annotators' files that are not in the repository (e.g. a repro script), identical for every arm.

## Deviations from the pre-registration

All in DESIGN.md section 10, with times. Items 1-5 were found on the smoke instance before the first sampled
instance ran (tool processes inherited the runner's `PYTHONHOME`; the evaluator's worktree path was too long for git;
`core.autocrlf=false` so every reader sees the LF files ContextBench's annotators saw; one large instance at a time;
the smoke result). Item 7 happened during the run: after 37 instances the runner was stopped right after a large
instance finished and restarted with two large instances allowed at once; the three instances then in flight
(one started 6 seconds earlier; nothing of theirs recorded) were rerun from scratch. No arm, budget, parser, metric or timeout changed. The
diagnostics below (win/tie/loss counts, heavy-tail and pilot-overlap sensitivity, output pathologies) are descriptive
additions that were not pre-registered.

## What could bias this comparison

- **Who built it**: Verinoda is our tool, the arms, parsers and BM25 baseline were written by the same person who
  develops it. Verinoda's retrieval was developed on its own question sets and a private project, never on
  ContextBench; but 4 sampled instances (dayjs-938, -734, -1319, requests-6028) were in the 15-instance feasibility
  pilot the same night with a development build. Dropping them changes nothing material (Verinoda query CITED file
  R/P 0.305/0.100, SHOWN span F1 0.042; BM25 0.159/0.157, 0.035).
- **The CITED view favours tools that print many ranges.** Verinoda's lead lines cite whole symbols (a class header
  `path:60-254` counts all 195 lines): median 1,074 cited lines per answer against 57 shown. That is why its CITED span
  recall (0.197) is 8x its SHOWN span recall and its CITED span precision is 0.035. It is a fair measure of "where an
  agent would look next", not of code delivered.
- **Graphify's query is a graph traversal from label-matched seeds, not a ranker**, and prints labels and locations,
  not code. With an issue text as the question it seeds on many words: a TRUNCATED notice on 71/80 answers, a
  "complete answer over budget" notice on 6/80, 582 of 5,755 nodes with no source file, seeds such as CHANGELOG and
  docs headings. Its SHOWN view is by the task's definition one line per node, which credits every node's file
  (generous at file level: Graphify's SHOWN file recall 0.210 is above Verinoda query's 0.187) and almost nothing at
  span level. Graphify's MCP tools (`get_node`, `shortest_path`) and its LLM-labelled graph were not used.
- **Issue texts are long natural-language questions** (median 405 characters, max 6,367), pasted verbatim with their
  templates and logs; both tools are built for shorter developer questions. Verinoda analyze echoes the question in
  its `understood as`, the sub-question line and every unknown: median output 10,566 characters, passages start at
  character 4,759 (median). That is the main reason analyze loses inside the cap.
- **Micro averages are dominated by a few instances.** material-ui-34401 alone holds 116 of the 396 gold files; the
  median instance has 3. Without the two largest (material-ui-34401, nushell-13357; descriptive only) CITED file
  recall is Verinoda query 0.448, analyze 0.326, Graphify 0.310, BM25 0.249 (same order). The macro table is there for
  the same reason. TypeScript numbers are near zero for every arm largely because of that one instance and the two
  vscode instances.
- **The BM25 baseline is one reasonable design, not the strongest possible**: code files only, 30-line windows, one
  window per file. A different window or including docs would move its numbers; its file precision advantage comes
  from showing only 5 files.
- **Wall times were measured on a busy shared machine** (another benchmark run and several test suites ran all night;
  CPU at 100 % when sampled; 3 of our instances in flight, later up to 2 large ones at once). Absolute seconds are
  inflated and noisy; the ratios between tools on the same instance are more meaningful than the values.
- **N = 10 per language** and no significance test was pre-registered: per-language numbers are descriptive.
- **Not comparable to the ContextBench leaderboard**: those are agent trajectories with many tool calls on the same
  or a related split; this is one 1,500-token shot per tool.

## Tool observations worth acting on (from this run's outputs)

- **Verinoda scan is slow on large repositories because the facts pass commits SQLite once per file.** `py-spy dump`
  of the material-ui-39196 scan (about 36 minutes in) showed the main thread idle in `store.tx` commit called from
  `store.put_file_facts` <- `anchors.facts_for` <- `anchors.update_facts` (3 of 4 samples), with the process using
  0.03 CPU-seconds per second. One transaction per batch of files would likely remove most of this (not measured).
- **Verinoda analyze refuses issues that mention version numbers** (4/80 exit 2, "the plan is invalid; nothing was
  analysed") where query answers the same text.
- **Verinoda analyze answered in Turkish for 4 English issues** (`understood as: Anladığım (kurallarla)`, sub-question
  kinds `[etki]`, `[akış]`: svelte-13097, svelte-12098, nushell-13357, yt-dlp-5933). Language detection misfires on
  English issue text.
- **Verinoda analyze's output grows with the question** (the question is echoed several times), so its evidence
  arrives after 6,000 characters on 27/80 issue-length questions.
- **Graphify's header (the seed list) is outside its budget and unbounded**: over 3,000 characters on 5/80, over 6,000
  on transformers-13989, where node ids embed the absolute checkout path.

## Files

- `DESIGN.md` - the pre-registration and its amendments.
- `results.json` - summary (every table above, overall and per language, capped and uncapped, micro and macro),
  diagnostics and per-instance intersection/gold/pred sizes for file, span, line and symbol, timings, exit codes.
- `predictions.jsonl` - the 1,360 ContextBench predictions (80 x 17, each with `_label` arm/view/cap/chars).
- `evaluator_rows.jsonl` - the evaluator's output rows, labelled.
- `out/raw/<id>.json` - every arm's full output text, exit code, stderr tail and timing; `out/runs.jsonl`,
  `out/run.log`; `out/tables.md` (the tables above as generated); `out/eval/<id>.stderr.txt` (evaluator details).
- `scripts/` - `sample.py`, `run.py`, `views.py`, `bm25.py`, `aggregate.py`, `diagnostics.py`; `sample.json`.
- Reproduce the numbers: `.venv\Scripts\python.exe scripts\aggregate.py` then `scripts\diagnostics.py` (from `cb`).
