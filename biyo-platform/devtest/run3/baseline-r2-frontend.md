# Baseline run 3 (revision), frontend and server phase log

Scope: serve.py and web\ only, in C:\Users\ozcin\biyo-platform\baseline\. query.py was not touched (the other agent owns it).
Verinoda: not used in any form (no mcp__verinoda__*, no CLI, no package, nothing under verinoda-mod or verinoda).
graphify: not used.

## 1. Steps

- 21:02 Read BRIEF.md, SPEC.md, run2\PROTOCOL.md, run2\baseline-p2.md.
- 21:02 Listed baseline\ and baseline\web\ (existing run-2 files: serve.py, query.py, README.md, web\index.html, style.css, app.js).
- 21:02 Checked corpus: 31 topics in corpus\topics.json (grades 9: 9, 10: 7, 11: 11, 12: 10); read via Python, counts only.
- 21:02 Checked from the shell that the pinned CDN files answer: 3d-force-graph@1.73.0 dist and three@0.150.1 build (HTTP 200).
- 21:03 Read the current serve.py, index.html, style.css, app.js (needed before overwriting them).
- 21:04 serve.py: added load_topics() (reads corpus\topics.json, next to query.py's corpus) and GET /api/topics. /api/graph and load_graph() unchanged.
- 21:04 Wrote web\index.html (new structure: top bar with search and examples, 3D stage, footer with legend, scope note and unknowns; loads the pinned 3d-force-graph script from cdn.jsdelivr.net).
- 21:04 Wrote web\style.css (light theme, one accent, system font stack, phone breakpoint at 640 px, reduced-motion rule).
- 21:05 Wrote web\app.js (3D force graph, HTML labels projected from the camera, relation tooltip, side panel, home network).
- 21:05 Checks: node --check web\app.js (OK); Python ast parse of serve.py (OK); load_topics() called directly (31 topics).
- 21:05 One static-file check: started serve.py on port 8001, requested /, /index.html, /style.css, /app.js (all 200, correct types) and /api/topics (JSON with the topics); stopped the server (PID 7156), port 8001 checked closed.
- 21:07 Wrote this log.

## 2. Files read

- C:\Users\ozcin\biyo-platform\devtest\run3\BRIEF.md
- C:\Users\ozcin\biyo-platform\SPEC.md
- C:\Users\ozcin\biyo-platform\devtest\run2\PROTOCOL.md
- C:\Users\ozcin\biyo-platform\devtest\run2\baseline-p2.md
- C:\Users\ozcin\biyo-platform\baseline\serve.py (before and after the edit)
- C:\Users\ozcin\biyo-platform\baseline\web\index.html, style.css, app.js (the run-2 versions, overwritten)
- C:\Users\ozcin\biyo-platform\corpus\topics.json (read through Python for the count and grade distribution only; passages not opened)
- Not read: query.py (contract taken from SPEC.md only; the run-2 log describes its output, the revision agent owns the file), benchmark\, archive\, symbiosis\, any gold file.

## 3. Files written

- C:\Users\ozcin\biyo-platform\baseline\serve.py (edited: /api/topics, load_topics)
- C:\Users\ozcin\biyo-platform\baseline\web\index.html (rewritten)
- C:\Users\ozcin\biyo-platform\baseline\web\style.css (rewritten)
- C:\Users\ozcin\biyo-platform\baseline\web\app.js (rewritten)
- This log.
- Not updated: baseline\README.md (still describes the run-2 page; it belongs to phase 3, see section 5).

## 4. Design decisions and reasons

Palette (all neutral except one accent):
- Page and 3D background: #f4f6f8 (very light grey-blue). Surfaces: #ffffff. Text: #1c2630 (contrast well above 7:1 on white). Secondary text: #46535f / #5f6b77.
- Accent (the only colour that is not grey): #1b6a7d (calm teal-blue). Used for the searched topic node, its label border, the grade line, the search button and the summary bar.
- Relation colours are slate tones, not hues: prerequisite (onkosul) #2c3b48 dark with arrows; support/application (destek) #6b7f90; common concept (ortak) #a7b4c0 light.
- Status is shown by line opacity in 3D (verified 0.9, inference 0.5, unknown 0.22) and by text badges in the panel (doğrulandı green-tinted, çıkarım amber-tinted, bilinmiyor grey, doğrulanmadı red-tinted for sources). Colour is never the only carrier: every status also has a word.
- Reason: the user rejected glow and weak colour. Light background, one accent, no blur, no particles, no gradients keeps it calm and official.

Typography and hierarchy:
- System font stack (system-ui, Segoe UI, Roboto, Helvetica Neue, Arial): no web font request, readable on Windows and phones.
- Body 16px, line-height 1.6. Brand 22px; panel title 22px; section headings 15px bold; labels 13px; centre label 19px bold with grade line 14px.
- Reason: a clear three-level hierarchy (title, section, body) and text sizes that a student can read without zooming.

Layout:
- Top bar: small kicker, title, search (46px high, 16px font to avoid iOS zoom), four example chips.
- Stage: fills the rest of the viewport (flex). 3D canvas underneath; labels as HTML over it; status line top-left; tooltip on relation hover.
- Footer: legend, the scope note ("Ortaokul fen bilgisi konuları henüz yok..."), and a collapsible "Belirsizlikler" list.
- Panel: 440 px wide on desktop, full width below 640 px; slides in with transform over 0.38 s (cubic-bezier), no transition under prefers-reduced-motion. Header with title, grades and close button; the body scrolls inside the panel (overscroll contained).
- Panel order: summary at the top, then "Bu konudan önce öğrenilmesi gerekenler" (prerequisites), "Bu konunun gerekli olduğu konular" (required for), "Destek ve ortak kavramlar", "Nerelerde kullanılır", "Hangi kaynaklardan çalışılmalı", "Ne zaman ve hangi sırayla". This follows the brief (summary, then the two lists), with the rest after.

3D and labels:
- 3d-force-graph@1.73.0 pinned, loaded from cdn.jsdelivr.net. The dist bundle was checked to contain the API names used (linkOpacity, onLinkHover, cameraPosition, d3Force, cooldownTime, enableNodeDrag and others). Whether it bundles its own three.js was not confirmed; three@0.150.1 is not loaded separately, so there is no second copy to conflict with.
- Labels are HTML elements over the canvas, moved each frame to the projected 3D position (projection with the camera's matrices, column-major). Reason: readable text with no glow, and no dependency on a second library for sprites.
- Centre node pinned at the origin (fx, fy, fz = 0), big, label above it: the title and "Sınıf: 9-10-11-12" (only the grades it has). Other nodes: "Title (grades joined by dashes)".
- Click a node: panel opens and the camera moves to it (900 ms, none under reduced motion). Home-screen node click runs a search for that topic.
- Escape and the close button close the panel; focus goes back to the previous element.

Home screen:
- The brief asks for all topics in one neural network on the home screen. /api/graph with an empty query returns no nodes, so the page takes the list from the new /api/topics (corpus\topics.json, 31 topics) and draws the nodes without relations.

## 5. Problems and open points

- The home network shows the 31 topics as nodes only. The relations between them are not shown until a search, because query.py only returns a topic's neighbourhood and no all-pairs call was made. Not measured: the time and the result size of 31 query calls. If the brief means relations on the home screen too, that needs a new endpoint from the data side.
- The label for each neighbour is placed above its node; with many neighbours (for example oksijen, 11 or more) labels can overlap. Not checked visually.
- The camera zoom to a node moves the camera toward it, but the open panel (440 px on desktop) can cover part of the node. Not fixed.
- Fen bilgisi (middle school) data does not exist in the corpus; the page states this in the footer. No per-topic flag is shown because the contract has no field for it.
- baseline\README.md still describes the run-2 page (it says "SVG", the dark look and no /api/topics). It is for phase 3 and was not changed in this phase.
- The unknown-query case (zzqxw) now shows the home network with the unknowns list and a status message; this path was not run in a browser.

## 6. What I could not check without a browser

- Nothing has been rendered. I have not seen the 3D view, the label positions, the tooltip placement, the panel slide, the phone layout, the scroll inside the panel, or the focus behaviour.
- The label projection math is written from the three.js camera layout but not run against a live camera. A wrong sign or a wrong matrix order would put every label in the wrong place.
- WebGL availability on the classroom machines is unknown. If WebGL is missing, the page shows the message "Bu tarayıcı 3B görünümü açamadı." and nothing else in the graph area.
- The CDN script load (from cdn.jsdelivr.net) was checked only with curl (HTTP 200), not from a browser page.
- The panel with a long summary and many relations: the scroll is written but not measured.
- The /api/graph path was not run in this phase (only the static files and /api/topics were requested). query.py was not run through the server, and its output was not re-checked against the page.
- Not measured: page load time, the size of the 3D bundle on a phone connection (the min.js is about 0.67 MB as fetched by curl).
