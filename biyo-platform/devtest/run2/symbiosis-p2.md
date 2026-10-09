# symbiosis P2 (server and page) log

## 1. Steps

- 20:46 Read PROTOCOL.md, SPEC.md (in full), symbiosis-p1.md and query.py (four Read calls in one batch).
- 20:46 Listed symbiosis\ and symbiosis\kb\ (one Bash call). The first call also ran `date /t`, which is invalid in Git Bash and made that call exit 1. The listing still printed. Re-ran the time as `date "+%H:%M"`.
- 20:47 Verinoda CLI query (call 1, see section 3) from symbiosis\kb\ to check whether it could answer a page or relation question. It returned topics.json entries and passage snippets, the same kind of output P1 saw.
- 20:47 Wrote symbiosis\serve.py: stdlib only, `get_graph(q)` as the single data provider (runs query.py as a subprocess and parses its stdout), `/api/graph?q=` JSON endpoint, static files from web\ (127.0.0.1:8000), query length limit, JSON errors.
- 20:48 Wrote symbiosis\web\index.html (search box, chips, status line, notes box, graph container, legend, right-side panel, tooltip).
- 20:48 Wrote symbiosis\web\style.css (dark theme, panel slide-in with transform, phone breakpoint at 520 px, reduced-motion rule).
- 20:49 Wrote symbiosis\web\app.js: search via fetch, 2D SVG graph sized to the container, centre node with label above, neighbours grouped by edge type into sectors with per-type colour and dash, arrow heads for onkosul, hover/focus/tap tooltip with relation status, side panel (summary, uses, sources with MEB first and unverified flagged, study order, relation with status and evidence), close button, Escape, ?q= deep link, redraw on resize.
- 20:50 Checked app.js with `node --check` (passes). Parsed serve.py with the Python ast module (passes).
- 20:51 Imported serve.py (no server started) and called get_graph for mitoz, zzkelime and an empty query (three calls, see section 3).
- 20:52 Wrote this log.

No server was started. P3 starts it.

## 2. Files read

- C:\Users\ozcin\biyo-platform\devtest\run2\PROTOCOL.md
- C:\Users\ozcin\biyo-platform\SPEC.md
- C:\Users\ozcin\biyo-platform\devtest\run2\symbiosis-p1.md
- C:\Users\ozcin\biyo-platform\symbiosis\query.py
- C:\Users\ozcin\biyo-platform\symbiosis\ (directory listing)
- C:\Users\ozcin\biyo-platform\symbiosis\kb\ (directory listing)

Not opened: anything under biyo-platform\archive\, benchmark\gold\, baseline\, or any file under kb\.verinoda. The kb\corpus files were not read in P2 (P1 had already read the format sample).

## 3. Tool results

| # | Tool | Question | Approx. result size |
|---|------|----------|---------------------|
| 1 | Read PROTOCOL.md | (phase rules) | about 3,000 chars |
| 2 | Read SPEC.md | (contract and frontend rules) | about 6,200 chars |
| 3 | Read symbiosis-p1.md | (P1 log) | about 5,600 chars |
| 4 | Read query.py | (JSON producer) | about 10,700 chars |
| 5 | Bash ls on symbiosis\ and kb\ | which folders exist | about 1,000 chars (includes the failed `date /t` message) |
| 6 | Verinoda CLI `query` (cwd kb\, `--max-items 5`) | which corpus passages describe fotosentez and which topics come before it | about 4,000 chars (cut at 4,000 by my `head -c`; the full output was longer) |
| 7 | Bash `date "+%H:%M"` | time for the log | under 50 chars |
| 8 | `serve.get_graph("mitoz")` (python import, no server) | the SPEC JSON, 3 nodes and 2 edges | about 250 chars (summary print only; the JSON itself was not printed) |
| 9 | `serve.get_graph("zzkelime")` | unknown word | included in call 8 output |
| 10 | `serve.get_graph("")` | empty query | included in call 8 output |
| 11 | `node --check app.js`, Python ast.parse on serve.py | syntax | under 100 chars |

Verinoda usefulness in P2: call 6 returned the grade_note fields in topics.json (for fotosentez, hucresel-solunum, and others) and passage headings. These are real facts from the corpus, but they are not relation sentences. The relation step is still in query.py. The grade notes reach the page only as the unknowns text for the centre node, which is what query.py already does. Nothing from call 6 changed the code.

## 4. Problems

- `date /t` is a Windows cmd form and fails in Git Bash. Used the POSIX form.
- The grade label. SPEC's frontend section says the label is `Title (9-10-11-12)`, and the phase brief says `Title (9-10-11-12 grades)`. I followed SPEC (grade numbers in parentheses, joined by dashes, no word "grades"), because the SPEC is the binding text.
- Label size on a phone. The labels are wrapped over several lines (about 16 characters per line) and the layout is computed in pixels from the container width, not scaled from a fixed viewBox, so the text keeps its size. Overlap between neighbours' labels is possible when there are many neighbours; this was not tested in a browser.
- query.py emits every edge with status "verified" (its own unknowns note says the type is a guess). The page shows the status it receives, so it currently never shows "inference" or "unknown". That is a limit of query.py's output, not of the page. I did not change query.py.
- No 3D view. The SPEC allows a clear 2D view, so I built only the 2D SVG view and did not load any CDN library.

## 5. Not verified

- The page was not opened in a browser. Layout, the panel's slide-in, the tooltip position, the phone width (no horizontal scroll), label overlap and the Escape key were not checked by eye. Only `node --check` (syntax) was run on app.js.
- serve.py's HTTP path was not exercised. Only `get_graph` was called (through an import). The request handler, the static file types, the 400 and 500 JSON paths and the 127.0.0.1 binding were not tested with a real request. P3 must check `/api/graph?q=mitoz` and the static files.
- Whether the edges match the gold relations (the gold file was not opened, as the rules require).
- Relation directions for onkosul beyond the one example in P1.
- Runtime per request (the subprocess call per request was not timed).
- Whether `localhost` resolves to 127.0.0.1 on this Windows machine (the server binds to 127.0.0.1 only; a browser that tries ::1 first should fall back, but this was not checked).
- query.py was not changed in P2, so its JSON contract is as P1 left it.
