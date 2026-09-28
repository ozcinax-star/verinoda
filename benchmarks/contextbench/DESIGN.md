# ContextBench no-model comparison: pre-registration

Written 2026-09-28 02:45 +03:00, before any sampled instance was fetched, indexed or queried. Everything below is fixed
from here on; the only permitted changes are parser/plumbing fixes found on the smoke instance (section 9), each logged
with a timestamp in section 10 before the first sampled instance runs. RESULTS.md reports against this file.

Before this file was written, only orientation was done: the three tools were run once on `iamkun__dayjs-1611`
(a pilot instance, NOT in the sample) under `%TEMP%\cbtest` to learn their output formats; the ContextBench repo was
cloned and its evaluator read; the sample below was drawn (it needs only the dataset and the GitHub trees API).

## 1. Benchmark, tools, versions

- Dataset: Hugging Face `Contextbench/ContextBench`, config `contextbench_verified` (500 rows), file
  `data/contextbench_verified.parquet` at dataset revision `c2855792b006af41c67202d33883fb9d46362853`,
  sha256 `e9dcfd504cbfb849ac815a79040c793d0d92f94eecc9b5a4ee3e1445a2f8a791` (byte-identical to the copy in the
  ContextBench GitHub repo). Local copy: `cb/data/contextbench_verified.parquet`.
- Evaluator: `EuniAI/ContextBench` at `1436c28a8eb95496da4ea69ad458b9f8a8eb7d61` (Apache-2.0), cloned to
  `cb/ContextBench`, run unmodified as `python -m contextbench.evaluate --gold <parquet> --pred <file> --cache <dir>
  --out <jsonl>` from `cb/ContextBench` with the venv `cb/.venv` (CPython 3.11.15, `tree-sitter==0.20.4`,
  `tree-sitter-languages==1.10.2`, `datasets`, `pyarrow`, as its `requirements.txt` pins).
- Verinoda: freeze 4 (`private-bench/agentbench/envs/verinoda`, verinoda 0.3.2, source commit 3f0e72c), read-only.
- Graphify: 0.9.69 (`private-bench/agentbench/envs/graphify`), read-only.
- git 2.53.0.windows.1, Windows 11. No model is called anywhere.

## 2. Sample

`cb/scripts/sample.py`, output `cb/sample.json` (sha256 `6a99f152232ffbae3d02e312f1d20f3e5f9f6b4de521d32c4fdfcfc7e955ee4f`).
One `random.Random(20260928)`; for each language in sorted order, that language's `original_inst_id`s sorted
lexicographically are shuffled with `rng.sample`; walk the order and accept an instance unless its depth-1 checkout
would exceed 1.5 GB (sum of blob sizes from the GitHub git-trees API at `base_commit`; if the listing is truncated,
the repository's GitHub size). First 10 accepted per language = the sample; the remainder of each order is the
replacement queue. **Result: 80 instances, 0 skipped by the size rule** (largest checkout: jackson-databind-3701,
691 MB of blobs). Runtime replacement: only if the shallow fetch of an instance fails twice (network/commit gone);
the next instance of that language's queue is used and the replacement is recorded. Any other failure (index timeout,
non-zero exit, empty output) is NOT replaced: the arm scores as an empty prediction for that instance.

| language | instances |
|---|---|
| c | ponylang__ponyc-3973, ponylang__ponyc-2261, jqlang__jq-2654, facebook__zstd-1243, facebook__zstd-637, facebook__zstd-1532, ponylang__ponyc-2201, ponylang__ponyc-1124, ponylang__ponyc-2247, jqlang__jq-2919 |
| cpp | simdjson__simdjson-1695, catchorg__Catch2-1608, fmtlib__fmt-4286, nlohmann__json-4512, nlohmann__json-2989, nlohmann__json-2225, fmtlib__fmt-2394, fmtlib__fmt-1663, simdjson__simdjson-2016, nlohmann__json-3601 |
| go | cli__cli-3608, cli__cli-4253, cli__cli-2351, cli__cli-4676, cli__cli-3270, cli__cli-5648, cli__cli-495, cli__cli-7110, cli__cli-10048, cli__cli-5973 |
| java | alibaba__fastjson2-2775, google__gson-1391, mockito__mockito-3220, fasterxml__jackson-databind-4469, fasterxml__jackson-databind-4015, fasterxml__jackson-databind-4641, fasterxml__jackson-core-183, fasterxml__jackson-databind-4050, fasterxml__jackson-databind-3701, fasterxml__jackson-core-964 |
| javascript | prettier__prettier-11000, sveltejs__svelte-3702, iamkun__dayjs-938, sveltejs__svelte-14629, iamkun__dayjs-734, sveltejs__svelte-13097, sveltejs__svelte-3749, sveltejs__svelte-12098, serverless__serverless-5571, iamkun__dayjs-1319 |
| python | yt-dlp__yt-dlp-5933, pylint-dev__pylint-4604, pydata__xarray-4966, scikit-learn__scikit-learn-25931, django__django-15252, psf__requests-6028, astropy__astropy-14539, sympy__sympy-22914, huggingface__transformers-13989, sympy__sympy-12489 |
| rust | tokio-rs__tracing-2897, tokio-rs__bytes-721, nushell__nushell-13357, clap-rs__clap-3420, clap-rs__clap-4474, clap-rs__clap-2758, tokio-rs__tracing-1236, clap-rs__clap-4248, clap-rs__clap-3684, tokio-rs__tokio-4519 |
| typescript | microsoft__vscode-135197, mui__material-ui-34159, microsoft__vscode-153857, vuejs__core-8538, mui__material-ui-11987, mui__material-ui-34401, mui__material-ui-31998, mui__material-ui-28813, vuejs__core-9213, mui__material-ui-39196 |

## 3. Per-instance procedure

1. Checkout: `cb/repos/<id>/src`: `git init`, `git remote add origin <repo_url>`, `git fetch --depth 1 origin
   <base_commit>`, `git checkout --detach FETCH_HEAD`; HEAD must equal `base_commit`. Two worktrees of it for the
   tools, so neither tool sees the other's index: `cb/repos/<id>/v` (Verinoda) and `cb/repos/<id>/g` (Graphify),
   `git -C src worktree add --detach <dir> <base_commit>`. BM25 reads `src` (writes nothing).
   How "delete a repo's clone unless another sampled instance uses the same repo" is read: every sampled instance
   has its own base commit, so each gets its own depth-1 fetch (a repo shared by several instances only means
   several small downloads); `cb/repos/<id>` is deleted as soon as that instance is scored.
2. Question: the instance's `problem_statement`, verbatim (no sampled statement starts with `-`; the longest is
   6,367 chars, within the Windows command-line limit). Passed as one argv element.
3. Environment for every tool process: inherited, minus `GRAPHIFY_OUT` and every `*_API_KEY` / `*_AUTH_TOKEN`,
   plus `PYTHONIOENCODING=utf-8`, `PYTHONUTF8=1`, `GRAPHIFY_QUERY_LOG_DISABLE=1`. stdout is the delivered text.
4. Arms (commands exactly; `V` = frozen `verinoda.exe`, `G` = frozen `graphify.exe`):
   - index V: `V scan <v>` (cwd `v`), timeout 60 min. Index time = its wall time.
   - **(1) vq, Verinoda query**: `V query "<q>" --repo <v> --max-chars 6000` (default text output; 6000 chars set
     explicitly so a shape-budget default of 4,800 cannot apply), timeout 10 min.
   - **(2) va, Verinoda analyze**: `V analyze "<q>" --repo <v>` (default text output, all defaults). Its
     `--budget-tokens` knob was checked on the orientation instance and does not change the text (7,145 chars
     either way), so no budget flag is passed; the common budget is enforced by the cap (item 5). Timeout 10 min.
     Expected and stated in advance: analyze echoes the question several times, so for long issues the 6,000-char
     prefix ends before its `passages` block; the uncapped secondary is where its retrieval is visible.
   - index G: `G update .` (cwd `g`), timeout 60 min (Graphify's own AST-only rebuild, no LLM; it spawns its
     own 6 extraction workers). Index time = its wall time.
   - **(3) gq, Graphify query**: `G query "<q>" --budget 2000` (cwd `g`); Graphify counts 3 chars per token, so 2000
     = 6,000 chars, the same budget. Timeout 10 min.
   - **(4) bm25**: section 5. Index time = file listing + reading + tokenising; latency = scoring + windows.
   A timeout or non-zero exit gives that arm an empty text for the instance (scored as an empty prediction,
   counted, never excluded). If an index step fails, its arms still run (they may fail) and are recorded.
5. **Cap**: primary = the first 6,000 characters of the delivered text, for every arm. Secondary = the uncapped
   text. Tokens = characters/4 of the text being scored.
6. Parse each text into two views (section 4), write ContextBench predictions, score them (section 6), save the raw
   texts, predictions, timings and evaluator rows to `cb/out/` BEFORE deleting `cb/repos/<id>` and the evaluator's
   worktree.
7. Parallelism: 3 worker threads, each processing one whole instance at a time (so at most 3 index builds run at
   once; Graphify's update adds its own worker processes). Latencies are therefore measured under load and are
   reported as such.

## 4. From delivered text to predictions: SHOWN and CITED

Location syntax recognised everywhere: `PATH:A` or `PATH:A-B` (also `L`-prefixed numbers and an en dash), where PATH is
`[\w.@+$-]+(/[\w.@+$-]+)*\.[A-Za-z0-9_+]+`; a range with B < A or longer than 5,000 lines keeps A only. Paths are
posix-normalised; the evaluator then resolves them to repository files by suffix and drops non-files.
`pred_files` is always the union of the view's files (the evaluator's file metric reads only `pred_files`), and
`pred_spans` the view's line sets as maximal runs.

- Verinoda (vq and va, same rules). SHOWN = the source lines actually printed: a line `^<n>( |$)` counts as line n
  of the current file, where the current file is set by a header `## PATH:A-B ...` or by an indented line that is
  only a location (`  PATH:A-B`), and is cleared by any other non-indented, non-numbered line. CITED = every location
  anywhere in the text (headers, sub-headers, `called by (...)`, lead lines, claims, plan links) plus SHOWN.
  Expected on the orientation instance (gold `src/plugin/duration/index.js:84-90`): CITED covers it through the lead
  lines `src/plugin/duration/index.js:61-102` / `:60-254`; SHOWN has none of the gold lines. This is the parser's
  unit test.
- Graphify. SHOWN = for every `NODE label [src=PATH loc=L<a>[-L<b>] ...]` line, file PATH and line(s) a(-b) (the
  node's label is the only code content Graphify prints, so its location is what it shows, as the task defines);
  a node with an empty loc contributes its file only. CITED = SHOWN plus every EDGE `at=PATH:L<n>` location.
  Asymmetry stated in advance: at file level Graphify's SHOWN credits every node's file (a label and a location),
  while Verinoda's SHOWN credits only files with printed source lines (its label+location lines are CITED only). The
  CITED view is the like-for-like file-level comparison; SHOWN span numbers compare delivered code (Graphify: one
  line per node).
- BM25: its output is only headers plus numbered lines, so CITED = SHOWN.

## 5. Lexical baseline (BM25), fully specified

- Documents: files from `git ls-files` in `src` with extension in {py pyi pyx pxd js jsx mjs cjs ts tsx mts cts vue
  svelte go java kt kts scala groovy rs c h cc cpp cxx c++ hpp hh hxx h++ inl ipp tcc cs rb php swift m mm pony jq
  sh}, size <= 1 MB, decoded as UTF-8 with replacement; a file containing a NUL byte is skipped.
- Tokeniser (query and documents alike): regex `[A-Za-z_][A-Za-z0-9_]*`; each match emits itself lowercased and,
  if it contains `_` or a case/digit boundary, also its parts (split on `_`, camelCase and digit boundaries)
  lowercased; tokens shorter than 2 characters and a fixed English stopword list (in `cb/scripts/bm25.py`) are
  dropped. A document's tokens = tokens of its path + tokens of its content.
- Query = distinct tokens of the problem statement. Okapi BM25, k1 = 1.2, b = 0.75,
  idf = ln(1 + (N - df + 0.5)/(df + 0.5)). Files ranked by score desc, ties by path asc; files scoring 0 are never
  used.
- Window per file: 30 consecutive lines (the whole file if shorter); score = sum of idf over the distinct query
  terms occurring in the window's lines; best score wins, ties to the earliest start; if every window scores 0,
  lines 1-30.
- Output: in rank order, per file `## PATH:A-B` then each line as `<n> <text>` (right-stripped, cut at 200 chars),
  one window per file. A block is appended while the total stays <= 6,000 chars; the first block that does not fit
  is cut to the lines that fit if at least 5 do, and output stops there.

## 6. Scoring

- One evaluator call per instance on a JSONL of 17 predictions: 4 arms x {shown, cited} x {capped, uncapped} plus a
  `gold` entry whose prediction is the gold context itself (gives the gold sizes and the attainable ceiling: gold
  files that are not repository files, e.g. annotators' repro scripts, are dropped by the evaluator from any
  prediction). Each entry carries `"repo_url"` = the local `src` clone (it has a `.git` directory, so the evaluator
  keeps it), `"commit"`, and `"instance_id"` = the `original_inst_id`. `--cache` points to `cb/work/evalcache` and
  `CONTEXTBENCH_TMP_ROOT` to `cb/work/evaltmp`; the evaluator's worktree is removed after the call.
- Metrics per instance from the evaluator's `final` block: file, span (byte-level), line and symbol
  intersection / gold_size / pred_size. An entry the evaluator rejects (`no_context_extracted`, any error) counts
  as intersection 0, pred 0 with the gold entry's gold sizes. `editloc` is ignored (no patch is produced; the
  evaluator would fall back to the gold patch).
- **Primary**: micro average as the evaluator aggregates (sum of intersections / sum of gold = recall ("coverage"),
  / sum of pred = precision), F1 = 2PR/(P+R), for file and span, per language and overall, per arm, for the
  capped CITED and capped SHOWN views. Secondary: the same on uncapped texts; line and symbol granularity; macro
  (mean over instances of per-instance recall; per-instance precision averaged over instances with a non-empty
  prediction, and the number of empty predictions reported).
- Delivered tokens = chars/4 of the scored text (mean and median per arm). Recall per 1k tokens = micro recall /
  (mean delivered tokens / 1000), for file and span.
- Index time per tool (V scan, G update, BM25 tokenising) and query latency per arm: median and p90 over instances.
- Primary comparison = arms side by side on capped CITED file recall / precision and capped SHOWN span F1. No
  significance test is pre-registered; with 10 instances per language, per-language numbers are descriptive only.

## 7. Bias notes fixed in advance

- Verinoda's retrieval was developed on other question sets (its own benchmarks, a private project), never on
  ContextBench; but four sampled instances (iamkun__dayjs-938, -734, -1319, psf__requests-6028) were in the
  15-instance feasibility pilot run on 2026-09-28 01:00 with a development build; freeze 4 was made the same day.
- Graphify's `query` is a BFS graph traversal from label-matched seeds, not a ranker; on the orientation instance its
  seeds included CHANGELOG.md and docs/*.md headings. It prints no source lines, only labels with locations.
- Issue texts are long natural-language questions; both tools are built for shorter developer questions.
- Graphify's "complete answer over budget" mode can exceed the budget several times; the cap handles it for the
  primary numbers.
- Micro span numbers are dominated by heavy-tail instances (sveltejs__svelte-14629: 291 gold spans;
  mui__material-ui-34401: 131); macro is reported for that reason.
- Query latency is measured with 3 instances in flight and Graphify's own extraction workers.
- The comparison is one-shot retrieval; ContextBench's leaderboard numbers are agent trajectories and are not
  comparable.

## 8. Outputs

`cb/out/raw/<id>.json` (every arm's full text, exit codes, timings), `cb/out/preds/<id>.jsonl` (the 17 predictions),
`cb/out/eval/<id>.jsonl` + `.stderr.txt` (evaluator output), `cb/out/runs.jsonl` (one line per instance),
`cb/out/summary.json` (aggregates), `cb/RESULTS.md`.

## 9. Smoke instance and amendment policy

Smoke: `iamkun__dayjs-1611` (not in the sample) through the whole pipeline including the evaluator call; not
scored in the results. Parser or plumbing fixes found there are logged in section 10 with a timestamp before the
first sampled instance starts; nothing in sections 2-7 changes after that.

## 10. Amendments (smoke run)

All made on the smoke instance before the first sampled instance started (2026-09-28 02:55 +03:00). None changes
the sample, the arms, the budgets, the parsers or the metrics; they are plumbing.

1. 02:49, smoke run 1: every Verinoda and Graphify call failed (`AssertionError: SRE module mismatch`): the runner's
   own uv venv sets `PYTHONHOME`, which the tools' venvs inherited. Fix: the tools' environment also drops
   `__PYVENV_LAUNCHER__`, `PYTHONHOME`, `PYTHONPATH`, `VIRTUAL_ENV`, `PYTHONEXECUTABLE`.
2. 02:49, smoke run 1: the evaluator's own `git worktree add` failed (`fatal: '$GIT_DIR' too big`, path length).
   Fix: each prediction's `repo_url` is the path of the `src` clone relative to the evaluator's cwd
   (`../repos/<id>/src`, still a local clone with a `.git` directory), and `CONTEXTBENCH_TMP_ROOT` is
   `%TEMP%\cbe\<first 8 hex of sha1(id)>` (removed after each instance) instead of `cb/work/evaltmp/<id>`.
3. The machine's system git config has `core.autocrlf=true`; ContextBench's byte spans assume the LF checkout its
   Linux annotators saw. Every git process (the runner's and the evaluator's) runs with `core.autocrlf=false` and
   `core.longpaths=true` (via `GIT_CONFIG_COUNT`), so the tools, BM25 and the evaluator all read LF files.
4. Scheduling (memory; the shared machine has 32 GB with ~7 GB free): at most one instance whose checkout exceeds
   60 MB of blobs (9 instances: 2 vscode, 5 material-ui, fastjson2, jackson-databind-3701) is in flight; others run
   beside it, 3 at a time in total; largest first. Affects only wall times.
5. Smoke run 2 (02:51, exit 0, 94 s): the parser unit test holds (vq CITED span coverage 1.0 of gold
   `src/plugin/duration/index.js:84-90`, SHOWN 0.0); the gold entry scores 1.0/1.0; 17 evaluator rows for 17
   predictions; the evaluator worktree and the clone were removed. Smoke outputs are kept in `cb/work/smoke/` and
   are not part of the results.

6. Scripts as launched for the sample (sha256):

```
1cf92bb7f42ba2781552d08ab7de3dad3cde8b42fe0f3094f9b2664edbe5470a  scripts/run.py
485d08be5400a0644cb416d669aa54d3c6118723a57e09801561e69727987426  scripts/views.py
d3945250e9df40996996e7031e47ce9cf0f58a91840ae0b91ca165e3a59630a4  scripts/bm25.py
```

7. Post-launch, scheduling only (logged 05:25 +03:00, 27 of 80 instances done). Heavy instances run far slower than
   planned on the shared machine (jackson-databind-3701: 71 min; material-ui-39196: fetch+checkout 17 min, Verinoda
   scan 38 min); with one heavy at a time the 6 remaining heavy instances would run serially for ~7 h. The runner is
   stopped right after material-ui-39196 finishes (the light instances then in flight are re-run from scratch; nothing
   of theirs was recorded) and restarted with at most 2 heavy instances in flight (`--heavy 2`, still 3 workers, so
   still at most 3 index builds). No arm, budget, parser, metric or timeout changes; wall times of later instances are
   measured under different load, as all latencies here are. The launched `run.py` is kept as
   `cb/work/run_v1_launched.py`; the only difference is the `--heavy` option. As it happened (05:42): the runner was stopped
   6 s after material-ui-39196 was recorded, with two light instances and alibaba__fastjson2-2775 (heavy, started 6 s
   earlier) in flight; all three were rerun from scratch after the restart at 05:42:47.
