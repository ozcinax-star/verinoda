# symbiosis P3 (verify and document) log

## 1. Steps

- 20:47 Read PROTOCOL.md, SPEC.md, symbiosis-p1.md and symbiosis-p2.md (four Read calls in one batch).
- 20:48 Listed symbiosis\ and web\ (one Bash call). Ran `python query.py mitoz`, `oksijen` and `zzkelime` (one batch, output to scratchpad files, sizes and JSON checked with Python json).
- 20:48 Ran the Verinoda CLI `query` twice from symbiosis\kb (mitoz relations, oksijen relations). Results saved to scratchpad files and measured.
- 20:49 Started `python serve.py` in the background (port 8000 was free before the start, checked with netstat).
- 20:49 Checked GET /api/graph?q=mitoz (JSON, 200), GET /, /style.css, /app.js, /index.html (all 200), /api/graph?q=zzkelime and ?q= (200, JSON), /nope.txt (404), and two path-traversal requests (`/../serve.py`, `/..%2fserve.py`), both 404.
- 20:50 Stopped the server with taskkill on its PID. netstat then showed only TIME_WAIT rows on port 8000, no LISTENING; a request to port 8000 got connection refused.
- 20:50 `node --check web/app.js` (passes), Python ast.parse on serve.py (passes). Read web\index.html in full. Grepped web\app.js and web\style.css for the frontend requirements.
- 20:51 Checked that the two chip ids in index.html (`mitoz`, `mendel-kalitimi`) exist in kb\corpus\topics.json (grep).
- 20:51 Checked kb\ for writes made during P3 (find -mmin). See section 4.
- 20:52 Wrote symbiosis\README.md (Turkish).
- 20:53 Wrote this log.

## 2. Files read

- C:\Users\ozcin\biyo-platform\devtest\run2\PROTOCOL.md
- C:\Users\ozcin\biyo-platform\SPEC.md
- C:\Users\ozcin\biyo-platform\devtest\run2\symbiosis-p1.md
- C:\Users\ozcin\biyo-platform\devtest\run2\symbiosis-p2.md
- C:\Users\ozcin\biyo-platform\symbiosis\web\index.html (full)
- C:\Users\ozcin\biyo-platform\symbiosis\web\app.js (grep matches and the Escape/resize block, lines 440-470; not read in full)
- C:\Users\ozcin\biyo-platform\symbiosis\web\style.css (grep matches only)
- C:\Users\ozcin\biyo-platform\symbiosis\kb\corpus\topics.json (grep for two ids and the count of `"id"` fields, not read in full)
- Directory listings of symbiosis\, symbiosis\web\, symbiosis\kb\ and kb\.verinoda\ (names and timestamps)
- Grep of symbiosis\query.py and serve.py for imports (no verinoda import in either file)

Not opened: anything under biyo-platform\archive\, benchmark\gold\, baseline\, and nothing under kb\.verinoda beyond its directory listing.
Written by me: symbiosis\README.md, devtest\run2\symbiosis-p3.md, and scratchpad files (outputs of query.py, the Verinoda CLI and the API call, used only to measure sizes).

## 3. Tool results

| # | Tool | Question | Approx. result size |
|---|------|----------|---------------------|
| 1 | Bash ls, `python query.py mitoz` | the SPEC JSON for mitoz | about 5,600 chars (5,635 bytes) |
| 2 | `python query.py oksijen` | the SPEC JSON for oksijen | about 11,200 chars (11,241 bytes) |
| 3 | `python query.py zzkelime` | unknown word | about 230 chars (226 bytes) |
| 4 | Verinoda CLI `query "mitoz hangi konularla iliskili" --max-items 6` (cwd kb\) | which topics relate to mitoz | about 3,700 chars (3,656 bytes) |
| 5 | Verinoda CLI `query "oksijen hangi konuda kullanilir" --max-items 6` (cwd kb\) | where oksijen is used | about 1,600 chars (1,574 bytes) |
| 6 | curl GET /api/graph?q=mitoz | the endpoint returns JSON | about 4,200 chars (4,245 bytes) |
| 7 | curl GET /, /style.css, /app.js, /index.html, /api/graph?q=zzkelime, /api/graph?q=, /nope.txt, /../serve.py | status codes and content types only (bodies not kept) | under 100 chars each |
| 8 | `node --check` and Python ast.parse | syntax | under 100 chars |
| 9 | grep of topics.json and of web\ and symbiosis\*.py | ids and requirement text | under 2,000 chars in total |

Checks on the query.py results (from the saved files):

- mitoz: center mitoz, 3 nodes (mitoz, mayoz, dna-replikasyonu), 2 edges (mitoz -> mayoz ortak; dna-replikasyonu -> mitoz onkosul), both status verified, unknowns: 2 entries (the relation-type guess note and the unverified source note). stdout is only JSON (the file written from stdout parses with json.load).
- oksijen: no topic id, title or alias matches. Center is hucresel-solunum (chosen by passage count), 5 nodes, 4 edges, unknowns: 4 entries including the "oksijen is not a topic" note and a grade note for hucresel-solunum (marked as unverified in the text).
- zzkelime: nodes [] and edges [], center null, unknowns has one message. No crash, exit 0.

Verinoda usefulness in P3:

- Call 4 (mitoz) returned passage headings and the first lines of mitoz.md and dna-replikasyonu.md. It found the right passage pair, which is some help for checking the dna-replikasyonu relation, but it did not return the relation sentence itself. I did not need it for the output check, because query.py's quote is in the passage.
- Call 5 (oksijen) returned the passage snippets that mention oksijen (fotosentez, hucresel-solunum). That is the same fact query.py already found by counting.
- Neither call changed the code or the README. Both were cheap (under 4 KB each) but did not answer a relation question, so the verdict is "helpful for the topic-to-passage lookup, no use for the relation check".

## 4. Problems

- The first curl loop printed `/` paths as `C:/Program Files/Git/`. Git Bash rewrote the path argument of the printf-style format. The URLs themselves were correct: the server log shows GET / and GET /style.css etc. Re-running with `--path-as-is` for the traversal checks worked.
- Turkish output is garbled in my own console display (cp1254), as in P1. The saved files are UTF-8 and parse correctly with Python json. The display issue is not in the program.
- netstat after the stop showed TIME_WAIT rows on port 8000. These are closed connections from my test requests, not a listener. I confirmed no LISTENING row and that a new request is refused.
- The kb\ folder: after my Verinoda CLI calls, timestamps in kb\.verinoda\index\ and kb\.verinoda\index\cache\ were updated at 20:49 (the CLI calls ran just before). I did not open those files and cannot say from timestamps alone which command wrote them. kb\corpus\ was not written (its files are older). The protocol allows writes only through verinoda's own commands; this is the likely source, but I did not verify it.
- query.py marks every edge verified (the same limit as P2). The README now says so.
- The README is written in Turkish and claims nothing that was not checked. It says the page was not tried in a browser.

## 5. Not verified

- The page was not opened in a browser (no browser tool was used). Not checked by eye: the layout, the slide-in of the right panel (the CSS has a transform transition, but its behaviour is untested), the tooltip position, label overlap with many neighbours, a phone-width screen (no horizontal scroll), the Escape key and the close button. Escape and the close button were checked only by reading the code (keydown listener calls closePanel).
- The JS was checked for syntax only (node --check). Runtime errors in the DOM code were not checked.
- The panel and the other code paths in app.js (lines other than the grep hits) were not read in full.
- Whether the edges match the gold relations (the gold file was not opened, as the rules require). No recall or precision was measured.
- Relation directions of onkosul edges beyond the one mitoz example.
- Runtime per request (not timed).
- Whether `localhost` resolves to 127.0.0.1 in the browser (the server binds only to 127.0.0.1; the curl test used localhost and worked, so the name resolved on this machine).
- The /api/graph?q= (empty) response content: only the status (200) and the JSON content type were checked.
- The "unknowns" texts were displayed as garbled Turkish in my console; I did not check their wording on the page.

## Self-report

Files:
- C:\Users\ozcin\biyo-platform\symbiosis\query.py (P1, not changed in P3)
- C:\Users\ozcin\biyo-platform\symbiosis\serve.py (P2, not changed in P3)
- C:\Users\ozcin\biyo-platform\symbiosis\web\index.html, style.css, app.js (P2, not changed in P3)
- C:\Users\ozcin\biyo-platform\symbiosis\README.md (written in P3)
- C:\Users\ozcin\biyo-platform\devtest\run2\symbiosis-p3.md (this log)

Verified:
- query.py for mitoz, oksijen and zzkelime: JSON only on stdout, shape as in SPEC, unknown word gives nodes [] and an unknowns message, exit 0.
- serve.py on port 8000: GET /api/graph?q=mitoz returns 200 JSON with center mitoz, 3 nodes, 2 edges. GET /, /style.css, /app.js, /index.html return 200 with correct types. An unknown word and an empty query return 200 JSON. A missing file returns 404. Path traversal returns 404. The server was stopped and port 8000 has no listener.
- node --check on app.js and Python ast.parse on serve.py: pass.
- index.html references only files that exist (style.css, app.js). Both chip ids exist in topics.json.
- By reading the code: the grade label is `Title (9-10-11-12)`; the centre node is the searched topic; the panel has Özet, Nerelerde kullanılır, Hangi kaynaklardan çalışılmalı (MEB first, unverified flagged), Ne zaman / hangi sırayla çalışılmalı and the relation block with its status; a close button and an Escape handler; a CSS transform transition for the panel; a phone breakpoint at 520 px; a reduced-motion rule; the status legend (verified, inference, unknown) exists in the code.

Not verified: the browser behaviour of the page (see section 5 of this log), the gold relations, the runtime, and whether the kb\.verinoda index writes came from the CLI calls.

Estimate of problems left:
- High: the edges are all "verified" from query.py, so the "inference" and "unknown" states never appear. A student could read a guessed direction as fact. The README says so, but the product is weaker than SPEC asks for.
- High: the relation edges are heuristic (one "öncesinde" cue for direction). Recall is probably low (mitoz has 2 neighbours). Not measured.
- Medium: the page's visual behaviour is unknown (overlap, phone width, panel). A 10-minute browser check would close most of this.
- Medium: aliases are limited to a hand-made table; "oksijen" falls back to a passage-count centre, which is a weak answer for a student.
- Low: the grade note for hucresel-solunum in unknowns is a claim from outside the corpus, marked unverified.

Where Verinoda helped:
- A quick check that a topic is in a passage (the CLI query returns the passage headings and the first lines, under 4 KB per call). It helped me find the right passage pair for mitoz and dna-replikasyonu.
- It showed the topic-to-passage structure of the corpus in P2 (grade notes in topics.json), which is real corpus information.

Where Verinoda got in the way:
- The CLI query does not return relation sentences. Both P1 and P3 calls returned passage snippets, so the relation step had to be written by hand in query.py anyway. I spent calls on a question it could not answer.
- Its output is a list of leads, not claims with status, so it gave no evidence check for the relations. Verifying a quote needed grep.
- Writes under kb\.verinoda\index happen during a query call. That is a side effect I did not expect for a read; I could not confirm it from the timestamps alone.
- The runtime does not use the Verinoda package (query.py is stdlib). So the "Verinoda" part of the product is the corpus folder and the dev-loop calls, not a retrieval layer. The README states this.

## Final message

Phase P3 (verify and document) of run 2, symbiosis variant.
Files written: symbiosis\README.md and devtest\run2\symbiosis-p3.md (self-report at its end). Verified and unverified items are in the log.
Phase log: C:\Users\ozcin\biyo-platform\devtest\run2\symbiosis-p3.md
