# Baseline P2 (server and page) log

## 1. Steps

- ~20:46 Read PROTOCOL.md, SPEC.md, baseline-p1.md and baseline\query.py (in one batch).
- ~20:46 Checked baseline\ (only query.py present), created baseline\web\.
- ~20:50 Wrote baseline\serve.py (stdlib: ThreadingHTTPServer on 127.0.0.1:8001, static files from web\, GET /api/graph?q=...; the data provider is the single function load_graph(), which runs query.py with the current Python and returns its parsed JSON).
- ~20:53 Wrote baseline\web\index.html (search box, example buttons, SVG graph, legend, unknowns, right-side panel).
- ~20:56 Wrote baseline\web\style.css (dark neural look, panel slide-in, phone breakpoint at 600px, reduced-motion rule).
- ~21:00 Wrote baseline\web\app.js (fetch via one function, SVG radial layout with neighbours grouped by relation type, curved edges with per-type colour and per-status dash style, node labels "Titel (9-10-11-12)", panel with summary / uses / sources (MEB first, unverified flagged) / study order / relations with status, close by button or Escape).
- ~21:03 Checks, without starting the server: node --check on app.js, Python ast parse of serve.py, and load_graph() called directly for mitoz, oksijen, zzqxw and an empty query.
- ~21:05 Wrote this log.

No server was started. No change was made to query.py (its JSON contract is unchanged).

## 2. Files read

- C:\Users\ozcin\biyo-platform\devtest\run2\PROTOCOL.md
- C:\Users\ozcin\biyo-platform\SPEC.md
- C:\Users\ozcin\biyo-platform\devtest\run2\baseline-p1.md
- C:\Users\ozcin\biyo-platform\baseline\query.py

Files written: C:\Users\ozcin\biyo-platform\baseline\serve.py, C:\Users\ozcin\biyo-platform\baseline\web\index.html, C:\Users\ozcin\biyo-platform\baseline\web\style.css, C:\Users\ozcin\biyo-platform\baseline\web\app.js, this log.
Listed only: baseline\ (directory listing, via Bash ls).
Not read: corpus\ (no topic or passage was opened in P2), benchmark\, archive\, symbiosis\.

## 3. Tool results

Approximate sizes are estimated from the output as shown to me.

| Question / purpose | Tool | Approx. result size |
|---|---|---|
| Read the protocol | Read PROTOCOL.md | ~3,100 chars |
| Read the spec | Read SPEC.md | ~6,500 chars |
| Read the P1 log | Read baseline-p1.md | ~4,600 chars |
| Read the P1 query.py | Read query.py | ~8,300 chars |
| Check folder contents and time, tool versions | Bash (ls, date, which) | ~700 chars |
| Create baseline\web\ | Bash (mkdir) | ~10 chars |
| Syntax checks (node --check, Python ast) | Bash | ~60 chars |
| Data provider test (load_graph for mitoz, oksijen, zzqxw, empty) | Bash (python -I, imports serve.py, no server) | ~1,300 chars |

No Verinoda tool, CLI or package was used. No graphify use. No mcp__verinoda__* call. Nothing under archive\, benchmark\gold\, symbiosis\ or verinoda-mod was opened.

Results of the data provider test (load_graph, same JSON as query.py stdout):
- "mitoz": center mitoz, 3 nodes, 2 edges (mayoz -> mitoz ortak; dna-replikasyonu -> mitoz onkosul), all verified.
- "oksijen": center hucresel-solunum, 12 nodes, 11 edges, all verified; about 15.7 kB of JSON.
- "zzqxw": center null, nodes [], edges [], one unknowns message.
- "" (empty): center null, nodes [], one unknowns message.

Note: the mojibake seen in the shell output for source names is the Windows console encoding in my test print; the JSON itself was produced as UTF-8 by query.py and loaded correctly.

## 4. Problems

- The Windows console shows mojibake for Turkish text in test prints. Not a fault in the JSON or the page; I did not change any encoding.
- The SPEC label rule is "Title (9-10-11-12)" with grade numbers joined by dashes. I followed it literally, so a topic with grades [10] shows "Mitoz (10)", not "(9-10-11-12)".
- The 3D view was not built. I used a plain SVG radial layout (2D), which the SPEC accepts. No CDN library is used, so there is nothing to pin.
- Layout with many neighbours (oksijen: 11) is crowded: labels are placed under each node and alternate between two radii to reduce overlap. Not checked visually.
- Phone labels are sized in viewBox units (19 px at widths up to 600 px). The effective size on a real phone was not measured.
- The panel uses the inert attribute while closed; I checked the JS syntax only.

## 5. Not verified

- The page was not opened in a browser and the server was not started (P3 does that). No visual check of the layout, the panel slide-in, the hover tooltips, or the phone-width layout.
- serve.py's HTTP handler (routing of /api/graph, the JSON headers, the 500 path for a query.py error, and static file serving) was not exercised over a socket. Only load_graph() was called directly.
- Escape and the close button, keyboard activation of nodes (Enter/Space), and the search form were not run.
- The 'inference' and 'unknown' statuses were not exercised: query.py currently emits only 'verified' edges, so their styling is untested.
- Performance (runtime per query) was not measured. The benchmark was not run.
- The 'uses' and 'study' content was not checked against the corpus beyond the P1 spot checks.
