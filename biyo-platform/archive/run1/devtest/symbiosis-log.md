# Symbiosis (Verinoda variant) build log

Folder: biyo-platform/symbiosis/ only. Times are the shell clock (HH:MM) where a command printed it. Entries marked
"(no time)" were not timed separately, so their order is right but their minute is not known.

## Steps

- 20:28 Read biyo-platform/SPEC.md (full). Listed biyo-platform/ and symbiosis/ (names only).
- 20:28 Listed corpus/ and corpus/passages/ (names only), devtest/, archive/ (names only), symbiosis/kb/ (names only).
- 20:29 Read biyo-platform/corpus/topics.json (31 topics; corpus file, not gold). Created devtest/symbiosis-log.md.
- (no time) Read the first 1500 bytes of symbiosis/query.py, grepped its def/class list (625 lines, left by the
  earlier attempt), read symbiosis/kb/.verinoda/config.json. Decision: REUSE query.py and kb/ as they are. Reason:
  the output already matched the SPEC shape. query.py was not read in full.
- (no time) Ran `python query.py "mitoz"` (python 3.13 on PATH, about 3.4 s wall): valid SPEC JSON. Summarised its
  output with a small inline python check (nodes, edges, unknowns).
- (no time) Ran 9 more queries (fotosentez, oksijen, DNA'sını, hücre zarı, xyzzy, empty string, genetik, solunum,
  mitozda). All SPEC-shaped. Unknown and empty queries return nodes [] with an unknowns message, no crash.
  Notes: "oksijen" resolved to fotosentez; "genetik" is ambiguous and returns only 2 nodes (reported in unknowns).
- (no time) diff -rq of kb/corpus against corpus/: in sync (passages and topics.json identical).
- 20:29 to 20:33 (no exact time) Verinoda call 1: mcp__verinoda__project_query, question "where does
  symbiosis/query.py read the topic passages and build the summary of a node", max_items 5. Result: about 5 items
  shown plus "13138 more candidates", all from verinoda-mod (verinoda/feedback.py, verinoda/research.py,
  SYMBIOSIS.md, README.md). None from symbiosis/. The answer also said 34 files changed since the index.
  Used: NO. Got in the way: the Verinoda MCP server is bound to C:\Users\ozcin\verinoda-mod, so project_query cannot
  answer questions about symbiosis/. Fell back to Read and Grep.
- (no time) MISTAKE: I called the Artifact tool with action "quickstart" by accident (not a Verinoda call). It
  returned the artifact type list, the design-system list and a long page-design guide. Nothing was published and
  its instructions were not followed (the product is a local page, not an artifact). Cost: one large result in
  context.
- (no time) Wrote symbiosis/serve.py: stdlib http.server on 127.0.0.1:8000. Serves web/. GET /api/graph?q= calls
  fetch_graph(), which runs query.py as a subprocess and returns its JSON. Errors come back as JSON with status
  500 or 504.
- (no time) Wrote symbiosis/web/index.html (Turkish UI: search form, canvas, topic chips, legend, unknowns, side
  panel). Wrote symbiosis/web/style.css (dark UI; the panel slides in from the right; at phone width the panel is
  full width).
- (no time) Wrote symbiosis/web/app.js: 2D canvas graph, no CDN, no 3D library. Centre node with its name and grades
  above it. Neighbours in three sectors by relation type (onkosul upper left, destek upper right, ortak bottom).
  Edges are quadratic curves coloured by type and dashed or dotted by status. Hover shows a tip (type, status,
  quote); a tap does the same on touch. A click opens the panel: summary, uses, sources with MEB first and
  "doğrulanmadı" flags, study order, relations with quotes. Escape and the close button close it.
- (no time) Wrote symbiosis/README.md (what it does, how to run, what it does NOT do yet), in Turkish.
- (no time) Verinoda use in the build: none. The Verinoda CLI was not used.
- 20:31 Syntax check: `node --check web/app.js` OK (node 24.16); `python -m py_compile serve.py` OK.
- (no time) Quote check on query.py output: 13 queries (mitoz, fotosentez, DNA replikasyon, hücre zarı, solunum,
  sinir sistemi, enzimler, mayoz, evrim, ekosistem, nükleik asitler, protein sentezi, xyzzy). 88 edges: 87
  verified, 1 inference. Every verified evidence quote is a substring of its corpus passage after whitespace
  normalisation: 0 mismatches. The check was an inline python script that read the corpus passages the edges cite.
  A first try failed on a wrong path for the corpus root (fixed, rerun).
- (no time) Counted topics: 31 in corpus/topics.json (the README says 31).
- 20:33 Started `python serve.py` in the background (port 8000, free before the start). Checked:
  GET /api/graph?q=mitoz returns 200 JSON (center mitoz, 4 nodes, 3 edges). /api/graph?q=xyzzy returns nodes []
  with an unknowns message. /api/graph?q=mitoza returns center mitoz. GET / returns 200 text/html; app.js 200
  application/javascript; style.css 200 text/css; a path traversal request ("/../serve.py") returns 404.
- (no time) Stopped the server (PowerShell: stopped the process listening on port 8000). Port 8000 was free
  afterwards. The background task then reported exit 127; that is the kill, not a start error.
- (no time) Did NOT open a browser. The page was not rendered or clicked in a real browser. Not verified.

## Self-report

- Total tool calls made: 29 up to and including the final log write (counted from the transcript), plus the
  SubagentHandback call that ends the run. Includes the Read calls on SPEC.md and topics.json, Bash, Write, Edit,
  PowerShell, the Verinoda call, the accidental Artifact quickstart, the failed log-writing Bash call, and the two
  Read/Write calls on this log.
- Files read (paths only):
  - C:\Users\ozcin\biyo-platform\SPEC.md (whole)
  - C:\Users\ozcin\biyo-platform\corpus\topics.json (whole)
  - C:\Users\ozcin\biyo-platform\symbiosis\query.py (partial: head, def list, lines 56-80 and 266-290)
  - C:\Users\ozcin\biyo-platform\symbiosis\kb\.verinoda\config.json (whole)
  - the corpus passages cited by the verified edges (read by an inline python check, whole files, under
    C:\Users\ozcin\biyo-platform\corpus\passages\)
  - names only (directory listings): biyo-platform\corpus\passages, archive, devtest, symbiosis\kb
  - diff -rq compared kb\corpus with corpus\ (contents not printed)
  - not read: anything under biyo-platform\benchmark\ (not listed or opened at all), so nothing under benchmark\gold\
- Verinoda calls: 1 (mcp__verinoda__project_query). Items back: about 5 shown plus 13138 unshown candidates, all
  from verinoda-mod. Used: no. No other Verinoda calls.
- Non-Verinoda mistake: 1 Artifact quickstart call (nothing published).
- Token counts: not visible.
- Parts that were hard:
  1. The Verinoda MCP tools are bound to verinoda-mod, not to symbiosis/, so they could not help with this folder.
  2. query.py is 625 lines of earlier code. I reused it without reading it in full, so I rely on its output checks
     rather than on my own reading of it.
  3. The Turkish UI and the canvas layout were written without a browser to look at.
  4. The log-writing Bash call failed because the shell wrapper and my apostrophes clashed; I used the Write tool.
- Verinoda helped: nothing measurable in this build. query.py uses the verinoda package at runtime, with its own
  index in kb/, and its search resolved "oksijen" to fotosentez. That is query.py's behaviour, not a benefit I
  measured against a control.
- What I did NOT verify:
  - the page in a real browser (layout, canvas drawing, hover, the panel slide, phone width);
  - the JavaScript beyond `node --check` (no run, no DOM test);
  - query.py on Python 3.10 to 3.12 (ran on 3.13 only), and its fallback path when the verinoda package is missing
    (the docstring says edges then become "inference"; not tested);
  - whether the relations and their directions are right (only the evidence quotes were checked; the benchmark
    scores that);
  - known weak spots: "genetik" gives 2 nodes; a word shared by two topics (solunum) picks one topic;
  - the MEB sources: all URLs are null and all are marked unverified, as the SPEC requires; nothing was checked online.
