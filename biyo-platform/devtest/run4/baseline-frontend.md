# Baseline run 4 (frontend revision), phase log

Scope: baseline\serve.py and baseline\web\ only. baseline\query.py and the corpus were not changed. Port 8001, server started once and stopped (port closed afterwards).
Verinoda: not used in any form (no mcp__verinoda__*, no CLI, no package, nothing under verinoda-mod or verinoda).
graphify: not used.
Not read: benchmark\ (incl. gold), archive\, symbiosis\, devtest\run4\curriculum-diff.md, any passage file.

## 1. Steps

- 21:20 Read devtest\run3\BRIEF.md, biyo-platform\SPEC.md, devtest\run2\PROTOCOL.md.
- 21:21 Listed baseline\ and baseline\web\; read the run-3 frontend log (baseline-r2-frontend.md) to see what was there (3d-force-graph from a CDN, HTML labels, no home network).
- 21:21 Read baseline\serve.py, query.py (interface only, not changed), corpus\topics.json (31 topics, ids and titles).
- 21:22 Decided the renderer (section 4) and wrote serve.py: load_topics kept; ask_topic() asks query.py for one topic by its id and rejects a reply whose centre is not that topic; build_overview() keeps verified edges only, each pair once; get_overview() caches in memory under a lock; a background thread warms the cache at start; GET /api/overview added; /api/topics removed (overview replaces it).
- 21:23 Wrote web\geometry.js: hash, seeded rng (xorshift32), 3D spring layout with the centre pinned at the origin, bowed control point per edge, five dendrites per neuron, bezier, perspective projection.
- 21:24 Tuned the layout constants with a node script on synthetic graphs (31 nodes / 45 edges, 13 nodes / 12 edges): first try spread far too wide (max radius about 3x the target radius); repulsion lowered to 0.15 R^2, gravity 0.012, spring 0.05. Result: max radius within about 1.1 R, nearest-neighbour distance 40 to 50 units, 60 to 90 ms per layout.
- 21:24 Wrote web\app.js: home network, topic network, side panel, eased camera (tween, easeInOut cubic), fade on scene change, neuron drawing, pulse on onkosul edges, hover tooltips for nodes and edges, keyboard (H, Esc), zoom buttons and wheel, drag to orbit, reduced-motion fallback.
- 21:25 Wrote web\index.html (top bar with brand button and search, stage with canvas, HUD, tooltip, zoom buttons, panel; footer with legend, scope note, unknowns) and web\style.css (light theme, phone breakpoint at 640 px, panel slide 380 ms).
- 21:25 node --check on web\app.js and web\geometry.js: OK. Python parse of serve.py: OK.
- 21:25 Started serve.py once (port 8001). Static check: GET /, /style.css, /app.js, /geometry.js returned 200 with the right content types. GET /api/overview: 200, about 2.9 s the first time (31 query.py calls, 4 workers). GET /api/graph?q=mitoz: 200, 0.55 s. GET /api/graph?q=zzqxw: 200.
- 21:26 Smoke run of app.js in node (vm with DOM stubs and a fake clock, against the live server): home network loaded (31 nodes, 41 edges); search "Mitoz bölünme" built the topic network (centre + 2 neighbours); click on the centre opened the panel with the 7 sections; Escape closed it; H returned home; unknown query showed the no-match message; wheel and zoom click ran without error. Runtime errors in the frame loop: 0.
- 21:26 Stopped serve.py (PID 8852), port 8001 confirmed closed.
- 21:27 Wrote this log. README.md not updated (see section 5).

## 2. Files read

- C:\Users\ozcin\biyo-platform\devtest\run3\BRIEF.md
- C:\Users\ozcin\biyo-platform\SPEC.md
- C:\Users\ozcin\biyo-platform\devtest\run2\PROTOCOL.md
- C:\Users\ozcin\biyo-platform\devtest\run3\baseline-r2-frontend.md (the previous frontend log, read only)
- C:\Users\ozcin\biyo-platform\baseline\serve.py (before the edit)
- C:\Users\ozcin\biyo-platform\baseline\query.py (read for its output shape; not edited)
- C:\Users\ozcin\biyo-platform\baseline\web\index.html, style.css, app.js (headers only, to allow overwriting; the old contents were replaced)
- C:\Users\ozcin\biyo-platform\corpus\topics.json (ids, titles, grades)

## 3. Files written

- C:\Users\ozcin\biyo-platform\baseline\serve.py (rewritten: /api/overview, ask_topic, build_overview, get_overview, warm_overview; /api/graph and load_graph unchanged in behaviour)
- C:\Users\ozcin\biyo-platform\baseline\web\geometry.js (new)
- C:\Users\ozcin\biyo-platform\baseline\web\app.js (rewritten)
- C:\Users\ozcin\biyo-platform\baseline\web\index.html (rewritten)
- C:\Users\ozcin\biyo-platform\baseline\web\style.css (rewritten)
- This log.

Scratch files (node test scripts for the layout and the smoke run) were written to the session scratchpad, not to the project.

## 4. Renderer choice and why

- Own renderer on a 2D canvas, with my own perspective projection (geometry.js: rotate by yaw, pitch, then scale by focal / depth). No CDN library. Reason: the user asked for our own 3D renderer; a canvas needs no network and no pinned external file, and the look (soma, dendrites, bowed axons, pulses) is easier to control on a 2D canvas than through a scene graph. The run-3 page used 3d-force-graph from jsdelivr; that is no longer loaded.
- Depth: perspective scale per node and per curve point, draw order far to near, fog (alpha down to 0.55 for far objects), labels drawn last.
- Camera: orbit (drag), wheel and +/- buttons for zoom, slow auto-rotate after 3.5 s without input, eased fly-in to a clicked node (900 ms, easeInOutCubic). The panel shifts the view to the left on wide screens so the clicked node stays visible beside the panel.
- Limit: pinch zoom on phones is not implemented; the zoom buttons and drag-orbit cover phones.

## 5. Palette and layout decisions

- Page background #f4f6f8, surfaces #ffffff, ink #17232d, muted #5a6875, lines #d6dee6. One accent #1f6f82 (centre neuron, search button, panel title).
- Relation colours: prerequisite (onkosul) #2d4a62 slate navy, support (destek) #3d8c7c sea green, common concept (ortak) #8a96a2 grey. The pulse travels only on onkosul edges, from the prerequisite to the target.
- Status is shown by line style and by words: verified solid, inference dashed, unknown dotted; hover tooltip and panel badges say "doğrulandı / çıkarım / bilinmiyor". Sources: "doğrulandı" only when the URL is set and verified; otherwise "doğrulanmadı". All current sources are unverified (url null), so every source shows "doğrulanmadı".
- No glow, no blur, no gradient on the page. The neuron body uses a radial shading (highlight at upper left) and a nucleus dot, which is the only gradient.
- Landing: all 31 topics as one network, computed server-side from query.py (each topic asked as its own search; only verified edges kept, each pair once). The overview has 41 verified edges (16 destek, 23 ortak, 2 onkosul). 8 topics have no verified edge and are placed in the network without links; they are named in the unknowns list.
- Topic view: centre at the origin, big, with its title and "Sınıf: 9-10" label above; neighbours at 8 world units, labels "Title (grades)".
- Panel order: short summary; "Bu konudan önce öğrenilmesi gerekenler"; "Bu konunun gerekli olduğu konular"; "Destek ve ortak kavramlar"; "Hangi kaynaklardan çalışılmalı" with the verified flag; "Nerelerde kullanılır"; "Ne zaman ve hangi sırayla çalışılmalı". Each relation shows its status badge and the corpus sentence it rests on.
- Panel slide: CSS transform, 380 ms, cubic-bezier(0.2, 0.7, 0.2, 1). Full width below 640 px. Reduced-motion users get shorter transitions and no auto-rotate.
- Graph change: fade out 220 ms, swap, fade in 420 ms.
- Home: the brand button and key H both call goHome(). Esc closes the panel only when it is open (it does nothing else).
- Phone: top bar stacks (brand over search), search input 16 px to avoid iOS zoom, panel full width, footer compact.

## 6. Problems and open points

- The overview has only 2 prerequisite edges. The landing network therefore shows mostly destek and ortak links, and 8 isolated topics. This is what query.py gives; it was not changed.
- The panel's prerequisite and required-for lists come only from the current search neighbourhood (query.py returns at most 12 neighbours per search). A relation that exists in the corpus but is not in that neighbourhood does not appear.
- Clicking a node on the landing network runs a search for that topic (its own network opens), then the panel opens for it. Clicking a neighbour in a topic network zooms to it and opens its panel; it does not re-centre the network.
- Nodes are not keyboard-focusable. Keyboard users can search (input with datalist), press Esc, use the panel buttons, and use H. Not fixed.
- The tooltip for an edge shows the first corpus sentence of that edge. For an edge with no evidence it says to click the topic.
- Pulse timing is by the curve parameter, so the pulse runs a little faster on short curves. Not tuned by eye.

## 7. Not verified

- Not checked in a real browser: how the page looks, label overlap in the landing network, the feel of the easing and the pulse speed, touch behaviour on a phone, and the panel on a narrow screen. I did not open a browser (no browser tool was used in this phase).
- The smoke run used DOM stubs: the canvas context and classList were stubbed, so the drawn pixels were not inspected. It checks that the code runs and the state changes (home, topic, panel open/close, H, Esc, unknown query), not the picture.
- Not measured: frame time on a phone, and the first overview time on a slow machine (one run here: about 2.9 s on this machine).
- README.md (baseline\README.md) still describes the run-2 page (SVG, no home network, /api/topics). It belongs to the documentation phase and was not updated in this revision.
- Phone widths were not rendered; the CSS breakpoint at 640 px was written, not tested.

## Run 4 fixes

Scope: baseline\web\ (app.js, geometry.js, style.css) only. serve.py, query.py and the corpus were not changed (serve.py was parsed, not edited).

### Changes

- Problem 1 (landing click started a search). Cause: the pointer-up handler on the landing network called showTopic(node.id). Now a click on a landing node flies to it and opens its panel. The panel's data comes from that topic's own search (/api/graph?q=<id>, cached per topic, shown as "Konu ayrıntısı yükleniyor…" until it arrives); the landing network does not change. Relation buttons in the panel open their node the same way.
- New panel button "Bu konuyu merkez yap" (top of the panel, style .centre-btn). It sets the search box to the topic title and starts the search view centred on that node. It is hidden only for the centre of the network already on screen. Works on the landing network and in a topic network.
- Problem 2 (?q=... did not set the centre). Cause: the page always called goHome() on load and never read the URL. Now ?q=<text> runs the search on load (runSearch) and the landing network is loaded behind it with no swap (loadHomeData), so nothing replaces the centre afterwards. The overview's unknowns list no longer overwrites the topic's unknowns, and a failed overview load no longer overwrites the topic status line. Without ?q the page starts on the landing network as before.
- Problem 3 (landing nodes too close, labels overlap). geometry.js layout(): when there is no pinned node (landing network) repulsion goes from 0.15 R^2 to 0.6 R^2, the target edge length from 0.42 R to 0.8 R, and gravity from 0.012 to 0.008. The topic layout (pinned centre) keeps the old values: checked, its positions are identical to the old code (max difference 0). app.js buildScene: landing camera distance from 2.9 to 2.2 times the network radius, so the wider network fills the stage. Hover text on a landing node now says "Tıklayın: panel açılır." (both views).

### Numbers (node, from build_overview() of the current serve.py: 31 nodes, 41 verified edges)

Stage assumed 1280 x 720 px, home camera yaw 0.5, pitch -0.22. Label overlap is an estimate (12 px text at about 6.3 px per character, 22 px box), not a measurement in a browser.

| | before | after |
|---|---|---|
| network radius (world units, max) | 166 | 304 |
| min distance between two nodes | 49 (karbonhidratlar-proteinler) | 80 (proteinler-lipitler) |
| nearest-neighbour distance, median / min | 62 / 49 | 110 / 80 |
| verified edge length, min / median / max | 49 / 65 / 76 | 80 / 116 / 137 |
| projected box x (share of width) | 405-890 (38%) | 289-984 (54%) |
| projected box y (share of height) | 134-584 (63%) | 63-654 (82%) |
| estimated overlapping label pairs | 26 | 11 |

Connectivity: 41 verified edges form 9 groups: one group of 23 topics and 8 topics with no verified edge (data, unchanged; they cannot be connected without changing the corpus or the edge rules). Every verified edge is kept and drawn.

Tried and not kept: repulsion 0.8 with spring 0.9 or 1.0 gave 13-17 estimated overlaps at the same camera (wider spacing also makes the screen scale grow). Camera 1.6 to 1.9 filled the height but pushed the box to the stage edge.

### Checks run

- node --check app.js and geometry.js: OK. Python parse of serve.py: OK.
- Layout numbers: node measure script on the saved overview (scratchpad, not in the project).
- Topic layout parity: pinned layouts identical to the old geometry.js (max difference 0).
- vm smoke test of app.js with DOM stubs and a hand-driven clock, answers from saved JSON (scratchpad smoke.js): (a) ?q=mitoz: HUD ends "Merkez: Mitoz bölünme · 2 ilişki", the requests are /api/graph?q=mitoz then /api/overview, no home swap, 31 datalist options, runtime errors 0. (b) landing: first click opens the panel with the 6 sections and keeps the landing HUD; "Bu konuyu merkez yap" is present and starts the search centred on the clicked topic; runtime errors 0.
- Port 8001: the curl check returned 200 for /, /style.css, /app.js, /geometry.js and the served app.js and geometry.js match the files. /api/overview returned 31 nodes and 41 edges. Port 8001 is shared: another agent's serve.py processes (PIDs 30208 and 34560, started 21:27 from baseline and symbiosis) were already listening on it, and the first curl (before my edits) returned an overview with 97 edges, which this code does not produce (it gives 41). I did not identify that responder and did not stop the other processes. I stopped my own serve.py (PID 23028). PID 30208 is still listening on 8001 and is not mine.
- No verinoda, archive, symbiosis, benchmark\gold, or verinoda-mod path was used.

### Not checked (needs a browser)

- How the landing network looks: label overlap, the fill of the stage at real window sizes, the 1280 x 720 assumption, and phone widths.
- Pixel output of the panel button, the panel with a long summary, and the fly-in to a clicked node.
- Feel of the new camera distance and the spacing; frame time.
- Reaching the page with ?q= in a real browser (the vm test used stubs, not a real DOM).
