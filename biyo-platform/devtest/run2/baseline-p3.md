# Baseline P3 (verify and document) log

## 1. Steps

- ~20:45 Read PROTOCOL.md, SPEC.md, baseline-p1.md and baseline-p2.md (one batch). Listed baseline\ and web\.
- ~20:48 Ran query.py for "mitoz", "oksijen" and "zzqxw" (stdout to scratchpad files; checked by json.load on the whole file, which fails if anything but JSON is on stdout; stderr checked empty). Also read the first bytes of each file.
- ~20:50 Syntax checks: node --check web\app.js; Python ast parse of serve.py. Brace counts for style.css and app.js; ids in index.html vs app.js.
- ~20:51 Started serve.py once on 127.0.0.1:8001 (background, from PowerShell). Port was free before. Requests: GET /api/graph?q=mitoz, GET /api/graph?q=zzqxw, GET /, /index.html, /style.css, /app.js (all 200), /nope.txt (404).
- ~20:52 Stopped the server (process gone). Port 8001 shows no LISTEN socket; only client-side TIME_WAIT entries remained.
- ~20:53 Wrote baseline\README.md (Turkish).
- ~20:55 Wrote this log.

## 2. Files read

- C:\Users\ozcin\biyo-platform\devtest\run2\PROTOCOL.md
- C:\Users\ozcin\biyo-platform\SPEC.md
- C:\Users\ozcin\biyo-platform\devtest\run2\baseline-p1.md
- C:\Users\ozcin\biyo-platform\devtest\run2\baseline-p2.md
- C:\Users\ozcin\biyo-platform\baseline\serve.py
- C:\Users\ozcin\biyo-platform\baseline\web\index.html
- C:\Users\ozcin\biyo-platform\baseline\web\app.js

Files not read in P3: baseline\query.py (not reopened; its output was tested directly), baseline\web\style.css (only grepped for rule names, media queries and brace balance, not read in full).

Files written: C:\Users\ozcin\biyo-platform\baseline\README.md, C:\Users\ozcin\biyo-platform\devtest\run2\baseline-p3.md.

Listed only: baseline\ and baseline\web\ (directory listing), devtest\run2\ (names).

Not opened: corpus\, benchmark\ (any part), archive\, symbiosis\, verinoda-mod, verinoda. No mcp__verinoda__* call, no verinoda CLI or package, no graphify use.

## 3. Tool results

Approximate sizes are estimated from the output as shown to me.

| Question / purpose | Tool | Approx. result size |
|---|---|---|
| Read the protocol | Read PROTOCOL.md | ~3,600 chars |
| Read the spec | Read SPEC.md | ~6,500 chars |
| Read P1 and P2 logs | Read x2 | ~9,000 chars |
| Read serve.py, index.html, app.js | Read x3 | ~26,000 chars |
| query.py for "mitoz", "oksijen", "zzqxw" (JSON validity, counts) | Bash | ~1,800 chars shown |
| Source and status fields of the outputs | Bash | ~900 chars shown |
| JS syntax, Python ast, HTML refs, id and brace checks, CSS rule grep | Bash, Grep-like shell | ~1,800 chars |
| Server start, 7 HTTP requests, stop, port check | PowerShell | ~1,000 chars |

## 4. Results

### 4.1 query.py (stdout only)

- "mitoz": valid JSON on stdout, exit 0, stderr empty. center mitoz; nodes mitoz, mayoz, dna-replikasyonu; 2 edges, both status verified, type ortak (mayoz) and onkosul (dna-replikasyonu, as in P1). unknowns: one message saying the relation type is a sentence-cue guess and the direction is not verified.
- "oksijen": valid JSON, exit 0, stderr empty. center hucresel-solunum; 12 nodes; 11 edges, all verified and all ortak; 5 unknowns messages about grade levels (for example, fotosentez is 12th grade in the 2018 programme, so it is flagged). The unknowns text is in Turkish and carries grade notes from the topic list.
- "zzqxw": valid JSON, exit 0, stderr empty. nodes [], edges [], center null, one unknowns message.
- The first bytes of all three outputs are "{" followed by CRLF; no extra text on stdout.
- The Turkish text looked like mojibake in the shell display. This is the Windows console code page in the display only; json.load with encoding utf-8 parsed every file.

### 4.2 Server (serve.py on 8001)

- Port 8001 was free before start. Started with `python serve.py` from baseline\.
- GET /api/graph?q=mitoz: 200, application/json; charset=utf-8, 4,101 bytes; JSON center mitoz, 3 nodes, 2 edges.
- GET /api/graph?q=zzqxw: 200, 147 bytes; nodes 0, unknowns 1.
- GET / and GET /index.html: 200, text/html, 1,968 bytes.
- GET /style.css: 200, text/css, 6,750 bytes. GET /app.js: 200, text/javascript, 14,299 bytes.
- GET /nope.txt: 404 (static file handler, expected).
- After stopping the process: process gone, no LISTEN socket on 8001 (only TIME_WAIT entries from the client side).

### 4.3 Page checks (by reading and by tool, no browser)

- app.js: `node --check` passes. Brace and paren counts balance (134/134 braces, 286/286 parentheses).
- serve.py: parses (ast).
- style.css: 76 `{` and 76 `}`. Rules for the phone breakpoint (`@media (max-width: 600px)` with `.panel { width: 100% }`), the reduced-motion rule and the panel transition (`transform 0.35s`) exist.
- index.html references only style.css and app.js; both exist in web\. No CDN, so nothing to pin.
- Every id used in app.js exists in index.html: q, search-form, graph, status, unknowns, panel, panel-title, panel-body, panel-close.
- SPEC frontend requirements found in the code:
  - Search box at the top, sent to GET /api/graph?q=... (index.html and app.js fetchGraph).
  - Centre node = searched topic (layout puts the centre at the middle; nodeGroup with type "centre").
  - Branches grouped by edge type with per-type classes (.edge.onkosul, .edge.destek, .edge.ortak; onkosul edges get an arrow marker).
  - Node label "Title (grades joined by -)" via nodeLabel().
  - Right panel slides in (.panel transform transition; `.panel.open`), with sections "Kısa özet", "Nerelerde kullanılır", "Hangi kaynaklardan çalışılmalı" (MEB first, unverified flagged), "Ne zaman / hangi sırayla çalışılmalı" (study), and relations with status badges.
  - Status shown on hover (SVG title on the edge) and in the panel (badge with STATUS_LABEL and STATUS_HINT).
  - Close button and Escape (document keydown listener calls closePanel).
  - Phone width: panel becomes 100% at 600px.
- Keyboard: nodes have tabindex and Enter/Space handlers.

## 5. Problems

- The shell and console showed mojibake for Turkish output. Not a fault in the files (utf-8 parse is fine); it only affected my reading of the printed text.
- Timestamps in the P1 and P2 logs (20:30 to 21:05) are later than the system clock when I ran P3 (about 20:45 to 20:55). I wrote my own times as they appeared to me; the orchestrator should not use these times for timing comparison without checking.
- The SPEC says `http://localhost:8000`, but the protocol gives the baseline port 8001. I followed the protocol (8001) and wrote 8001 in the README. This is a deviation from the SPEC text, not a bug.
- serve.py starts one Python subprocess per request (query.py). Fine for a classroom demo, slow for the benchmark, not measured.
- The `__pycache__` folder inside baseline\ came from importing serve.py during P2 checks. It is not part of the product; the orchestrator may remove it.
- No problems that blocked a P3 step.

## 6. Not verified

- No browser was used. The page was not rendered, the layout was not seen, the slide-in animation was not watched, and hover tooltips were not seen. The phone layout and the horizontal scroll were not checked.
- Escape, the close button, Enter and Space on nodes, the search form submit, and the example buttons were not run in a browser. I only checked by reading the code that the handlers exist.
- The SVG layout with 11 or 12 neighbours (oksijen) was not seen; label overlap is unknown.
- The edge JSON was checked only for the three queries. Other topic ids and synonyms were not run.
- style.css was not read in full (only grepped and brace-counted).
- query.py was not reopened in P3; its behaviour was tested through its stdout only.
- Error paths in serve.py (query.py failing or timing out, returning 500) were not exercised over the socket.
- The 'inference' and 'unknown' statuses were not produced by query.py (only verified), so their styling is untested in practice.
- Runtime per query was not measured. The benchmark was not run and was not read.
- The README statements about "what it does" were checked against the code, not against a live run in a browser.

## Self-report

Files:
- C:\Users\ozcin\biyo-platform\baseline\README.md (new, Turkish: what it does, how to run with `python serve.py` and http://localhost:8001, what it does not do yet).
- C:\Users\ozcin\biyo-platform\devtest\run2\baseline-p3.md (this log).
- No code files were changed in P3. query.py, serve.py and web\ are as P2 left them.

Verified:
- query.py: stdout is one JSON object for "mitoz", "oksijen" and "zzqxw"; exit 0; stderr empty; the unknown word returns nodes [] and an unknowns message.
- serve.py on port 8001: /api/graph?q=mitoz returns 200 JSON; /, /index.html, /style.css and /app.js return 200 with the right content types; the server stopped and the port has no listener afterwards.
- app.js passes `node --check`; serve.py parses; index.html references only existing files; the DOM ids used by app.js exist in index.html.
- The SPEC frontend requirements (search, centre node, grade-tagged labels, status display, right panel, Escape, phone width) are present in the code.

Not verified:
- Anything visual or interactive in a browser (layout, animation, hover, Escape, the phone width, the search flow end to end).
- Error paths over HTTP, other queries beyond the three, and performance.

Estimate of problems left:
- Likely (high confidence): the node layout with many neighbours overlaps labels; the 2D view is cramped at phone width (not seen).
- Likely (medium): the relation types and direction come from a cue-word heuristic and are unchecked by a teacher; the "verified" status only means the quote exists in the passage.
- Possible (low to medium): the grade label rule gives "Mitoz (10)" for single-grade topics, which the SPEC's "(9-10-11-12)" wording may not intend (P2 noted this).
- Minor: the port differs from the SPEC's 8000 text (intentional, per protocol); the __pycache__ folder should be removed before a demo.
