# symbiosis run 3 (revision 2): frontend and server log

Scope: serve.py and web\ only. query.py was not touched (another agent revises it). kb\ was read, never written.
Forbidden folders (archive\, baseline\, benchmark\gold\) were not opened.

## 1. Steps

- 21:02 Read BRIEF.md, SPEC.md, run2 PROTOCOL.md and run2 symbiosis-p2.md (four Bash reads, one batch).
- 21:03 Read symbiosis\serve.py, listed symbiosis\ and web\ (one Bash batch). Checked query.py structure with grep (no edit).
- 21:04 Ran query.py for "mitoz" once to see the JSON shape (stdout to a scratch file; not kept).
- 21:04 One Verinoda CLI query from symbiosis\kb (see section 3, call V1).
- 21:05 Checked the pinned CDN files with curl: three@0.150.1 build/three.min.js and 3d-force-graph@1.73.0 dist/3d-force-graph.min.js both answer HTTP 200. Downloaded the 3d-force-graph bundle to a scratch folder (not into the project) and grepped it for the method names the page uses (all present; the bundle exposes the global ForceGraph3D).
- 21:06 Read web\index.html, web\style.css, web\app.js (needed before overwriting them).
- 21:06 Edited serve.py: added get_topics() (reads kb\corpus\topics.json, the same file query.py reads) and the route GET /api/topics. get_graph and /api/graph are unchanged.
- 21:07 Wrote web\index.html (pinned CDN scripts, light layout, side panel, legend, footnote).
- 21:07 Wrote web\style.css (light palette, panel slide-in, phone breakpoint at 640 px, reduced-motion rule).
- 21:08 Wrote web\app.js (3D graph, home network, search, panel, fallback list). Fixed one status overwrite on topic-load failure.
- 21:08 `node --check web\app.js` passes. serve.py parses with the Python ast module. An id cross-check (every $("id") in app.js exists in index.html) reports no missing ids.
- 21:09 Started serve.py on port 8000 once, requested /, /style.css and /app.js (all 200 with the right types), then stopped it. Port 8000 answers nothing after the stop.

## 2. Files read

- C:\Users\ozcin\biyo-platform\devtest\run3\BRIEF.md
- C:\Users\ozcin\biyo-platform\SPEC.md
- C:\Users\ozcin\biyo-platform\devtest\run2\PROTOCOL.md
- C:\Users\ozcin\biyo-platform\devtest\run2\symbiosis-p2.md
- C:\Users\ozcin\biyo-platform\symbiosis\serve.py
- C:\Users\ozcin\biyo-platform\symbiosis\query.py (structure only: grep for def names, the load_topics function and the output call)
- C:\Users\ozcin\biyo-platform\symbiosis\web\index.html, style.css, app.js (old 2D version, read before overwrite)
- C:\Users\ozcin\biyo-platform\symbiosis\kb\corpus\topics.json (through query.py and through the Verinoda answer; a count of 31 topics was read by a short script)
- Remote, read only: the CDN files of three@0.150.1 and 3d-force-graph@1.73.0 (status check and grep of the bundle)

Not opened: archive\, baseline\, benchmark\gold\, any kb\ file for writing.

## 3. Tool results

| # | Tool | Question | Approx. result size |
|---|------|----------|---------------------|
| V1 | verinoda CLI `query` from symbiosis\kb, `--max-items 3` | which topic ids in topics.json carry grades, and where grades comes from | about 3,500 characters |
| 1 | Bash `python query.py mitoz` (shape check) | the JSON shape the page consumes | about 5,600 characters (scratch file) |
| 2 | Bash curl HEAD-style checks of two CDN files | do the pinned URLs exist | under 100 characters |
| 3 | Bash grep of the 3d-force-graph bundle | method names used by app.js | under 300 characters |

Verinoda V1 answer, in short: grades and grade_note are in topics.json (grade_note is a note on the 2018 programme, for example for difuzyon-ve-osmoz and atp-enerji). query.py's output has no grade_note field, so the page does not show it. It was not added because the /api/graph contract is fixed.

## 4. Decisions

### Palette (stated as asked)

- Page and 3D background: #f6f7f9 (paper). Cards: #ffffff. Text: #1e2530. Secondary text: #5c6675. Lines: #e2e6ec.
- One accent: #1d6a7a (deep teal). Used for the centre node, the "önkoşul" relation, buttons and links.
- The other relation types are lighter tints of the same hue, not new colours: destek #8fb3bd, ortak kavram #b4bdc9. The unknown-status edges are mixed 55 % towards the paper colour.
- Status flags only: verified text on #e6f2eb (muted green), inference on #f7f0df (muted ochre), unknown on grey. These are the only non-teal colours.
- Reason: the run-2 page used dark navy with violet, amber, teal and blue, plus glow filters. The user asked for a light, calm, professional look with no glow. One hue plus neutral greys keeps the hierarchy clear and does not look like a game.

### Typography and spacing

- System font stack only (Segoe UI, system-ui, -apple-system, Roboto, Helvetica Neue, Arial). No web font, so nothing new loads.
- Base 16 px with line-height 1.6. Panel title 1.35 rem. Section headings 1 rem, weight 650, accent colour. Search input 16 px so iOS does not zoom.
- Padding 24 px on desktop, 16 px gutter on phone. Touch targets 36 to 46 px.

### Layout

- Top bar: brand and tagline on the left, search on the right (400 px max). Second row: three example chips, "Ağın tamamı" button, and the status line.
- Stage: a bordered rounded area with the 3D graph filling it. Legend card at bottom left, notes (unknowns) collapsed at top left.
- Footnote under the stage: "Ortaokul fen bilimi konuları henüz yok" (as the brief asks, stated plainly).
- Panel: fixed on the right, 420 px. It slides with transform in 0.38 s (cubic-bezier 0.2, 0.7, 0.2, 1). It is hidden with visibility when closed, so it leaves the tab order. Header keeps the title and grade chips fixed; the body scrolls.
- Desktop: while the panel is open the stage gets a 420 px right margin, so the graph resizes instead of hiding under the panel. The 3D view resizes through a ResizeObserver.
- Phone (640 px and below): one column, the panel covers the full width, the legend hint is hidden, the stage is 58 vh high.

### 3D graph

- Library: 3d-force-graph@1.73.0 (dist/3d-force-graph.min.js) plus three@0.150.1 (build/three.min.js), both from cdn.jsdelivr.net with exact versions. three loads first, as the global THREE that the label code uses.
- Home (the whole network): each high-school topic is pinned on a sphere, so the network does not collapse. The scene turns very slowly (stops in search mode and with reduced motion). A click on a topic runs a search for it.
- Search: the centre is pinned at the origin, big (radius 9), with its name and "Sınıf 9-10" above it. Neighbours (radius 4.6) carry labels "Title (9-10-11-12)", wrapped over lines at 22 characters.
- Neighbours are placed by relation role: before the centre to the left, after it to the right, supporting topics above, common concepts below. Each role is a fan in its own direction.
- Onkosul edges carry an arrowhead (source to target, as the SPEC defines). Hovering an edge shows its type and status, and the sentence explaining the status.
- Click on a node: the camera moves to it (900 ms) and the panel opens. Escape closes the panel and the tip. Closing the panel returns the camera to the overview.
- Labels are canvas textures on sprites that always face the camera, drawn at 2x for sharpness, on a white rounded card so they read on the light background.
- Fallback: if the CDN or WebGL is unavailable, the stage shows a plain list of the same topics as buttons. The panel still works from the list.

### Side panel content (SPEC order, with the brief's additions)

1. Özet (summary, at the top).
2. Nerelerde kullanılır.
3. Hangi kaynaklardan çalışılmalı: MEB sources sorted first (stable). Each source has a "Doğrulandı" or "Doğrulanmadı" flag; a link is shown only when verified and the URL is https. A line says unverified sources get no link.
4. Ne zaman, hangi sırayla çalışılmalı (study.order and study.why).
5. Bu konudan önce öğrenilmesi gerekenler (onkosul edges where this topic is the target).
6. Bu konunun gerektirdiği konular (onkosul edges where this topic is the source).
7. Diğer bağlantılar (destek and ortak edges).

Each relation row shows the other topic (a button that moves the camera and opens its panel when it is in the scene), the type, the status badge with its meaning as a tooltip, and the first evidence sentence with its passage path.

### Server

- serve.py: get_graph is unchanged (the one data provider). get_topics() reads kb\corpus\topics.json. Route GET /api/topics returns id, title and grades for the home network and the datalist. Errors give JSON 500.
- The EBA seam is still the get_graph and get_topics functions; the page only calls /api/graph and /api/topics.

## 5. Problems

- The old page was 2D SVG. The new one is written from scratch (the old files were read first only to satisfy the overwrite rule).
- query.py emits every edge as "verified" (its own unknowns note says so). The page shows the status it receives, so the page never shows "inference" or "unknown" until query.py changes. This is query.py's output, not the page's.
- The home view needs topics from topics.json, because query.py only returns relations for one query. Hence the extra route.
- Two copies of three may be in the page: the global THREE from three@0.150.1 and the copy bundled in 3d-force-graph. The page uses the global one for its own objects. This is a normal pattern for 3d-force-graph, but it was not run in a browser.
- The three CDN files were not run in a browser, so the exact look of the labels, the arrowheads and the camera distance is a judgement from the code, not from a screen.

## 6. Not verified (could not check without a browser)

- The page was not opened in a browser. Nothing was rendered: no 3D view, no label placement, no arrowheads, no slide-in, no tooltip position, no focus behaviour, no Escape key.
- The phone layout: no horizontal scroll, the 58 vh stage, the full-width panel, the label size on a narrow canvas (labels are world-sized sprites, so on a portrait phone the text may be small).
- Label overlap on the home network (31 topics on a sphere) and on crowded searches.
- The three-copies question (see section 5).
- /api/graph was not requested through the server in this phase (the brief allowed only one static check). Its JSON contract was read from query.py's output for mitoz only.
- Whether the verified/unverified flags for sources are correct: all sources come from query.py's output with verified false; no URL was checked.
- The 3d-force-graph version pin: the URL answers 200, but the release date was not checked against the two-week rule. The user named this version explicitly.
- No accessibility test beyond the code (focus order, contrast ratios were chosen by hand, not measured).
- No test of the panel's scroll inside on a real device.
- The server was not started with a /api/topics request; only the static files were checked (200 for /, /style.css, /app.js).
