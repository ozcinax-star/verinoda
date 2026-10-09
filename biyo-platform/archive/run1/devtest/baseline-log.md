# Baseline test log (WITHOUT Verinoda)

Format: time | step | files read (paths) | graphify calls | notes (stuck / re-reads)

- 20:31 | Read SPEC.md in full; listed biyo-platform top level; baseline/ did not exist yet, created baseline/data and baseline/web. | read: C:\Users\ozcin\biyo-platform\SPEC.md | graphify: none | notes: none
- 20:31 | Listed corpus/passages (31 files) and corpus/topics.json. | read: C:\Users\ozcin\biyo-platform\corpus\topics.json | graphify: none | notes: none
- 20:31 | Read all 31 passages once through a single bash cat (output too large, persisted to a temp file; I read the first 2 KB preview of it and then the rest by Read). | read: corpus\passages\*.md (31 files, via cat) | graphify: none | notes: the cat output was truncated in the tool result, so I read 30 of the passages again individually with Read (re-read, see self-report)
- 20:31 | Read the 30 passages again one by one with Read so each passage text was fully visible (second read of each). | read: corpus\passages\*.md (30 files, second time) | graphify: none | notes: re-read caused by the truncated cat output
- (note) The four lines above were written together in one bash call at about the time of the cat step; their timestamps are the same value and are not per-step times.
- 20:34 | Wrote data/terms.json (search terms for 31 topics, Turkish; matching is accent-insensitive) and data/relations.json (60 hand-written relations from my reading of the passages; each names a passage and a verbatim quote). No graphify. | read: none new | notes: an earlier batch of relation phrases was not checked until the test run.
- 20:34 | Wrote query.py (stdlib: loads corpus, matches center, checks each quote against its passage, builds nodes/edges/unknowns, prints JSON). | read: none | notes: a stray redirect to /tmp/x failed (path is a directory); nothing written outside baseline.
- 20:34 | First test run: every query failed with FileNotFoundError because topics.json passage paths are relative to biyo-platform root, not corpus/. Fixed the path. | read: none | notes: stuck here for one round.
- 20:34 | Second test run: all 60 quotes found in their passages (60 verified, 0 inference). Centre matching failed for hyphenated ids (for example canlilarin-ortak-ozellikleri). Fixed by folding hyphens to spaces. Re-checked: every topic id and title gives its own topic as centre. | read: none | notes: none
- 20:34 | Wrote serve.py (ThreadingHTTPServer, /api/graph, static files, path check). | read: none | notes: none
- 20:34 | Wrote web/index.html, web/style.css, web/app.js (SVG 2D graph, right-side panel, Escape closes). Checked JS syntax with node --check equivalent (new Function). Did not open a browser. | read: none | notes: none
- 20:34 | Started serve.py --port 8001 in background; curl: /api/graph?q=mitoz returned JSON (center mitoz, 5 nodes, 4 edges); / and /style.css returned 200; empty q returned 400. | read: none | notes: first run crashed the handler on a missing file (send_error reason with a Turkish character is not latin-1). Fixed by using "Not found". Server log file read once (tail) to find this.
- 20:34 | Path-traversal request returned 404 after the fix. Stopped the server (port 8001 not listening). Deleted the two scratch output files I had created (devtest/.serve-8001.out and baseline/.serve-test.out) and baseline/__pycache__. | read: the server stderr log (devtest/.serve-8001.out), once | notes: I created scratch files in devtest/ and baseline/ by mistake; both were removed.
- 20:34 | Wrote baseline/README.md (what it does, how to run, what it does NOT do). | read: none | notes: none
- (note) The build-step lines above were appended in one bash call at 20:34, so their timestamps are not the times the steps happened; the order of the lines is the order of the work.

## Self-report

- Total tool calls made: about 62, counted by hand from the transcript (not from a tool counter). No graphify calls.
- Files read (distinct): SPEC.md, corpus/topics.json, the 31 files in corpus/passages/*.md, and one server stderr log I had written (devtest/.serve-8001.out). That is 34 distinct files.
- Re-reads: 30 passages were read twice (once in a bash cat, whose output was too large and was saved to a temp file, then again one by one with Read). The server log was read once. No other file was re-read.
- Did not open: archive/, benchmark/ (including gold/), symbiosis/, the verinoda folders, any verinoda docs.
- Hard parts: (1) the cat of all passages was truncated, so I re-read them; (2) topics.json passage paths are relative to biyo-platform root, which caused a first-run failure; (3) hyphenated topic ids did not match as search input until hyphens were folded to spaces; (4) a Turkish character in an HTTP reason phrase crashed the handler for 404s (fixed); (5) relations had to be written by hand from prose, because the corpus has no labelled list, so the 60 relation types and directions are my judgement.
- Tokens: not visible.
- Not verified: the page was never opened in a browser (no click, panel slide, hover or phone-width check; app.js was only parsed); search ranking was spot-checked on about 12 queries only; relation types and directions are not checked for correctness or completeness; source URLs were not checked (none given, all null); runtime per query not measured; the server was run only on port 8001 and stopped afterwards.
