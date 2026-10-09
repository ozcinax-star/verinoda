# Run 4, symbiosis (Verinoda variant): frontend revision

Scope: symbiosis\serve.py and symbiosis\web\ only. query.py, corpus, kb and archive were not changed. Times are
approximate (HH:MM), taken from the clock during the run.

## 1. Steps

- 21:20: read BRIEF.md (run3), SPEC.md, PROTOCOL.md, then listed symbiosis\ and web\.
- 21:20: read the run-3 serve.py and index.html (cat), and the head of web\app.js and web\style.css.
- 21:21: read query.py build() and load_topics(), and timed query.build over all 31 topics in-process (1.9 s).
- 21:21: Verinoda CLI calls on kb (see section 3).
- 21:22: read Verinoda graph3d.js (engine) and grepped/read the parts of Verinoda app.js that drive it.
- 21:22: copied graph3d.js unchanged into web\ (cmp: identical, header comment kept).
- 21:23: rewrote serve.py: /api/overview (in-process query.build per topic, verified edges only, deduped,
  cached in memory behind a lock, warmed in a background thread at start); /api/graph and /api/topics unchanged.
- 21:24: rewrote web\index.html (no CDN libraries any more: three.js and 3d-force-graph are gone), web\style.css
  (light theme, panel, phone breakpoint at 760 px) and web\app.js (NeuronGraph subclass over the engine).
- 21:25: node --check on app.js and graph3d.js; python ast check on serve.py; data checks (section 4).
- 21:26: one serve.py start, static and API checks with curl, stop (section 4). Port 8000 confirmed free.

## 2. Files read

- C:\Users\ozcin\biyo-platform\devtest\run3\BRIEF.md
- C:\Users\ozcin\biyo-platform\SPEC.md
- C:\Users\ozcin\biyo-platform\devtest\run2\PROTOCOL.md
- C:\Users\ozcin\biyo-platform\symbiosis\serve.py (run-3 version, before rewrite)
- C:\Users\ozcin\biyo-platform\symbiosis\web\index.html (run-3 version)
- C:\Users\ozcin\biyo-platform\symbiosis\web\app.js and style.css (first lines only, to satisfy the write rule;
  both were then rewritten)
- C:\Users\ozcin\biyo-platform\symbiosis\query.py (lines 1-30, 60-150, 254-360: build, node_for, load_topics)
- C:\Users\ozcin\biyo-platform\symbiosis\kb\corpus\topics.json (ids, titles, grades)
- C:\Users\ozcin\verinoda-mod\verinoda\ui\static\graph3d.js (whole file, 25 KB; copied)
- C:\Users\ozcin\verinoda-mod\verinoda\ui\static\app.js (grep for Graph3D/flyTo/setActive; lines 1436-1500,
  1545-1560, 1700-1712 only)
- Not opened: anything under benchmark\gold, archive\, baseline\. No kb passage was read directly; Verinoda's
  passage hits are summarised in section 3.

## 3. Tool results (Verinoda and query.py)

Verinoda CLI, run from symbiosis\kb with `C:\Users\ozcin\verinoda-mod\.venv\Scripts\python.exe -P -m verinoda query`:

| # | Question | Result size (approx. characters) | What it returned | How it was used |
|---|---|---|---|---|
| 1 | "mitoz" | more than 600 (I printed only `head -c 600`, so the full size was not measured) | passage leads from mitoz.md | none; a first look at the CLI output shape |
| 2 | "fotosentez öncesinde hangi konular gerekir" | about 3,650 | passage leads from fotosentez.md and mitoz.md, plus an "expanded" line | cross-check only; no relation was read from it |
| 3 | "hangi konular mitoz için gerekli" | about 3,660 | passage leads from mitoz.md | cross-check only |

Verinoda returns passage hits, not relations, so the edges come from query.py (the SPEC contract), not from
Verinoda. The two larger calls were not needed for the frontend work; that is recorded here as a cost.

query.py: not called as a CLI by me; its build() was called in-process from serve.py and from a test script
(section 4). Search results from /api/graph?q=mitoz: 6,123 bytes.

## 4. Decisions

Data (serve.py):
- /api/overview: for each topic, query.build(topic id); the center node carries summary, uses and study with
  sources. Only status "verified" edges are kept. Duplicates are merged by (type, source, target) for onkosul
  and by (type, sorted pair) for destek and ortak. Result: 31 nodes and 97 verified edges (59 ortak, 36 destek,
  2 onkosul). No topic is isolated. Cold computation: 3.6 s; afterwards from memory.
- get_graph (the search) is unchanged: the subprocess call to query.py, which keeps the SPEC contract.
- Note on the data: the corpus has only 2 verified onkosul edges, so the travelling pulse appears on 2 edges on
  the landing network. This is a corpus fact, not a rendering choice.

Engine and drawing:
- graph3d.js is copied unchanged. NeuronGraph (in app.js) extends it and overrides draw(), tick() and moving()
  only: the layout, the camera, the flyTo easing and the pointer handling stay in the engine.
- The searched topic is pinned at the origin, drawn 1.7 times larger, labelled "Title (grades)" with
  "Sınıf ..." underneath, and the camera flies to it once the layout has settled (alpha < 0.1).
- Neurons: soft body (radial gradient, thin membrane line, faint off-centre nucleus ring); dendrites: 5 to 7
  short filaments in fixed 3D directions per topic id, turned into the camera view (foreshortened, not drawn
  when pointing at the viewer), some with one side branch; axons: quadratic curves between the bodies, bend
  sign fixed by the pair of ids.
- Edge status is the line style: solid = verified, dashed = inference, dotted = unknown. Edge type is colour:
  onkosul blue, destek green, ortak warm grey. onkosul edges carry an arrowhead at the target and a small bead
  that travels source to target over 3.4 s (no glow: a faint halo only).
- Labels: the searched neuron, the selected and the hovered one, then the best connected; a label that would
  overlap an earlier one is skipped.

Palette (light theme, all tokens on :root in style.css):
- page #f5f6f8, card #ffffff, ink #1c2633, muted #5d6a7b, line #dde3ea.
- onkosul #2d6c97 (blue), destek #3a8567 (green), ortak #8b7f6c (warm grey), focus #b25d33 (terracotta).
- soma #ffffff to #c4d3e2 gradient with a #5f7b98 membrane; focus soma warm white to #dca88b.
- No gradient background, no glow, shadow only on the panel edge.

Layout:
- Header: brand button (returns to landing), search box, three example chips, status line. Desktop: two-column
  grid. Phone (under 760 px): one column, chips scroll sideways.
- Stage: canvas fills the stage. Legend (edge type and edge status) bottom left; footnote "Ortaokul fen bilimi
  konuları henüz yok" at the bottom, as required.
- Panel: 400 px from the right on desktop; while open the graph container shifts left by the panel width (CSS
  transition 0.5 s); on phone the panel is full width. Panel order: "Bu konunun ağını aç" action (when the topic is
  not the centre), summary, uses, prerequisites ("önce"), "required for" ("sonra"), study order and why, related
  destek/ortak topics, sources with "Doğrulandı" / "Doğrulanmadı" tags. Each relation shows its type and status
  tag and the evidence quote in italics.
- Transitions: panel slide 0.45 s cubic-bezier; graph change = canvas fade out 0.24 s, data swap, fade in; node
  click = engine flyToNode (eased, 900 ms); the landing re-fits the whole network after settling.
- Keys: H returns to the landing network (ignored while typing); Esc closes the panel first; the brand button
  does the same as H.
- Hover tooltip: the node name and its status to the selected or centre neuron (type and status), or its link
  count on the landing network.

## 5. Problems

- The engine's hit test uses the body radius only; dendrites are not clickable (intended).
- I did not run the page in a browser. Everything below is untested in a real browser (section 6).
- The panel's "Bu konunun ağını aç" re-runs a search with the topic's title; I checked that the titles of
  Mitoz bölünme, Fotosentez and Hücre zarı (madde geçişi) resolve to their topic ids.
- README.md in symbiosis\ still describes the run-3 3D library setup. It is outside the files I was asked to
  change, so it was not edited.

## 6. Not verified

- Rendering: nothing was drawn or looked at. The neuron look, the dendrite directions, the pulse, the label
  placement and the palette are all unchecked by eye.
- Behaviour in a browser: the fly-in on click, the panel slide, the canvas fade, the H and Esc keys, the hover
  tooltip, and the "Bu konunun ağını aç" buttons.
- Phone layout at real widths and the legend overlap on a small screen.
- Performance: the pulse keeps the render loop running while the landing network is shown. Not measured.
- Comparison with the Verinoda 3D graph in quality. Not measured.
- Server: serve.py was started once. Checks run: GET / (3,340 bytes), /app.js (23,192), /style.css (10,285),
  /graph3d.js (25,268, same size as the source), /api/overview (74,723 bytes), /api/graph?q=mitoz (6,123 bytes).
  The server was then stopped with PowerShell (the first taskkill attempt used the bash PID and did not stop it;
  the listener on port 8000 was then stopped and the port checked free).
- query.py output for the search itself was not re-verified in this run; query.py is unchanged.

## Run 4 fixes

Scope: symbiosis\web\app.js, symbiosis\web\style.css. graph3d.js unchanged (cmp: identical to Verinoda's copy).
serve.py, query.py, the corpus and kb were not changed. No browser was used.

### 1. Steps

- 21:28: read this log, serve.py, index.html, app.js, style.css and graph3d.js (engine), and PROTOCOL.md.
- 21:29: Problem 2 check: query.py mitoz gives center mitoz (4 nodes, 3 edges). The page never read ?q=; the
  landing network was also loaded asynchronously, so a late overview could replace a search.
- 21:30: Problem 1 check in code: a landing click goes only to onNodeClick, which opens the panel. The only
  search control in the panel was its first button ("Bu konuyu ağın merkezine al"), above the summary.
- 21:31: Problem 3: headless run of the engine (node, spread.js in scratchpad, outside the project) over the
  landing overview (31 nodes, 97 verified edges), with a grid of charge, distance and gravity (section 3).
- 21:33: app.js: parameters set to charge -500, distance 110, gravity 0.015 (comment gives the before and
  after numbers); viewSeq guard in showLanding and runSearch; ?q= read on load; panel button renamed and moved.
- 21:34: style.css: .btn-centre. node --check on app.js and graph3d.js; python ast on serve.py.
- 21:34: port 8000 was already held by a serve.py process (PID 34560, started 21:27, not started in this
  phase). It was NOT stopped; it serves web\ from disk and serve.py is unchanged, so the curl checks ran on it
  (section 4). Left as found.

### 2. Changes

- Problem 1 (landing click starts a search): a landing click only opens the panel (summary, prerequisites,
  required-for, sources with verified flags). The panel button is now "Bu konuyu merkez yap", placed under the
  summary, and it starts the search centred on that node. It is hidden when the node is already the centre of
  the search view. Related-topic buttons "Bu konunun ağını aç" are unchanged.
  Cause, from the code: the first control in the panel on the landing network was the search button, so a
  click that opened the panel looked as if it started a search. Not confirmed in a browser.
- Problem 2 (?q=...): on load, ?q=... runs the search and the landing network is not requested. If the text
  finds nothing, the landing network opens with a note. A view counter (S.viewSeq) makes a late overview or a
  late search answer drop out when a newer view has started, so nothing replaces the search. The search box
  is filled with the query.
- Problem 3 (spacing): charge -170 -> -500, distance 56 -> 110, gravity 0.03 -> 0.015. The page uses the same
  parameters in the search view (checked with query.py output for mitoz and fotosentez; the spacing grows there too).

### 3. Numbers (headless engine, settled at alpha < 0.004, 219 ticks; 1440 x 800 stage for the screen numbers)

| Setting (charge / distance / gravity) | min 3D distance | median nearest 3D | core radius 3D | min screen distance px | min screen gap px (discs) | screen span x / y |
|---|---|---|---|---|---|---|
| before: -170 / 56 / 0.03 (landing) | 48.4 | 66.3 | 211 | 3.2 | -21.7 | 0.28 / 0.45 |
| after: -500 / 110 / 0.015 (landing) | 76.1 | 121.2 | 390 | 13.4 | +2.1 | 0.29 / 0.47 |

Other points of the grid (landing, min 3D distance): -300/80/0.03 59.3; -500/80/0.03 69.3; -700/110/0.015 91.3
(but min screen gap -4.7); -800/110/0.01 78.1 (gap -7.9). Chosen: the largest gain in 3D spacing with the
screen gap turned positive; the bigger settings did not keep the screen gap.
Search view, same parameters, mitoz (4 nodes): min 3D distance 61.9 -> 119.8; fotosentez (11 nodes): 58.8 -> 112.7.
Edges: all 97 verified edges kept (the parameters change no data).

### 4. Checks

- node --check web\app.js: ok. node --check web\graph3d.js: ok. python ast parse of serve.py: ok.
- curl on port 8000 (the pre-existing server, see step 1): / 200 3,340 bytes; /?q=mitoz 200 3,340 bytes;
  /app.js 200 24,630 bytes (contains the new button text and initialQ); /style.css 200 10,507;
  /graph3d.js 200 25,268 (same as the source); /api/overview 200 74,723 bytes; /api/graph?q=mitoz 200 6,123
  bytes, center mitoz.

### 5. Verinoda calls and other tool results

| # | Question | Tool | Result size (approx. characters) | Used for |
|---|---|---|---|---|
| 1 | "mitoz" (run from symbiosis\kb) | verinoda query | about 3,650 (3,656 bytes measured) | a check of the centre passage; no relation read from it |
| - | query.py "mitoz" (not Verinoda) | in-process / CLI | 8,069 bytes | Problem 2: centre is mitoz |
| - | query.py "fotosentez" (not Verinoda) | CLI | 23,214 bytes | Problem 3, search-view check only |

Verinoda was called once. It returns passage leads, so it does not decide the centre or the edges here.

### 6. Problems

- The port 8000 process described in step 1 was already there. Not stopped (not started by this phase).
- The spread check is headless. The screen numbers assume the engine's own fit camera at 1440 x 800.
- The engine's fit frames a sphere, so the network fills about 28% of the stage width and 45% of the height
  at this size. Changing that needs an engine change or a page-side camera call, which were out of scope.
- The closest projected pair still nearly touches (+2.1 px, the disc edges). Depth overlap in 3D cannot be
  removed by these parameters alone.

### 7. Not verified

- Anything in a browser: the click on a landing node, the panel slide, the "Bu konuyu merkez yap" button, the
  ?q=mitoz load (the centre and the fly-in), the late-response guard under real timing, the look of the new
  spacing, and the phone layout.
- The 3D spacing numbers are from a headless run with the engine; the page's own timing (the fit when alpha is
  below 0.1) was not run.
