# Biyoloji Öğrenme Platformu — ortak sözleşme (SPEC)

Two prototypes are built from this one spec, so the comparison is fair:

- `symbiosis/` — built WITH Verinoda Symbiosis (verinoda-live mod + the verinoda index/tools in the dev loop; the
  runtime retrieval uses the `verinoda` Python package).
- `baseline/` — built WITHOUT any Verinoda tool, package, file or doc. Plain Claude Code, plain Python.

Both serve the same product idea: a high-school student (grades 9–12, Turkish MEB curriculum) types a biology topic;
a neural-network-style graph appears with the searched topic in the centre and related topics on branches; clicking a
node slides a panel in from the right with a short summary, where the topic is used, and what to study (MEB sources
first). The platform is meant to later plug into EBA (eba.gov.tr), so the backend/frontend seam must be explicit.

## Hard rules for both builders

1. Work only inside your own variant folder (`symbiosis/` or `baseline/`). Do not edit `corpus/` or `benchmark/`.
2. Do NOT open anything under `benchmark/gold/`. The gold relations are the test; reading them would contaminate it.
3. Do not invent URLs. Source entries that you cannot verify get `"url": null` and `"verified": false`.
4. Turkish UI text. Code comments and identifiers in English are fine.
5. Keep it runnable on Windows with the standard Python 3 (3.10+). Python stdlib only for the backend, unless the
   variant needs a package (symbiosis may import `verinoda`; baseline must not).
6. Frontend: one `index.html` + plain JS + CSS, served by `serve.py`. A CDN-loaded graph library is allowed if pinned
   to an exact version (for example `d3@7.9.0` from cdn.jsdelivr.net). No build step.

## Shared corpus (read-only for builders)

- `corpus/topics.json` — list of topics. Fields: `id`, `title` (Turkish), `grades` (list of ints from 9–12),
  `passage` (path under `corpus/passages/`).
- `corpus/passages/<id>.md` — 2–4 paragraphs of Turkish explanatory text per topic. Relations between topics show up
  only as ordinary prose (for example, "mitoz öncesinde DNA eşlenir ..."), never as a labelled list. Each passage
  ends with a `## Kaynaklar` section listing MEB sources as `- <ad> — <kaynak türü>` (no URLs unless verified).
- `corpus/topics.json` is the only list of topic ids. Use these ids verbatim.

## Output contract: `query.py`

Each variant has `query.py` at its root. Invocation:

    python query.py "<arama metni>"

It prints ONE JSON object to stdout (nothing else on stdout) with this shape:

```json
{
  "query": "fotosentez",
  "center": "fotosentez",
  "nodes": [
    {"id": "fotosentez", "title": "Fotosentez", "grades": [11, 12], "summary": "...", "uses": ["..."],
     "study": {"order": 1, "why": "...", "sources": [{"ad": "...", "tur": "MEB ders kitabı", "url": null, "verified": false}]}}
  ],
  "edges": [
    {"source": "atp-enerji", "target": "fotosentez", "type": "onkosul",
     "evidence": [{"passage": "corpus/passages/atp-enerji.md", "quote": "..."}], "status": "verified"}
  ],
  "unknowns": ["..."]
}
```

- `source`/`target` are topic ids from `corpus/topics.json`. Node `id`s are those same ids.
- `type` is one of: `onkosul` (the target can only be understood after the source; read `source` → `target`),
  `destek` (one supports or applies the other), `ortak` (shared concept, no order).
- `status` is one of: `verified` (the evidence quote really is in the corpus passage), `inference` (plausible, not
  quoted), `unknown`. An edge with no evidence must not be `verified`.
- `unknowns` lists what the system could not decide (or an empty list).
- The search must work on any topic id, its title, and common Turkish synonyms the corpus supports. An unknown query
  returns `"nodes": []` and an `unknowns` message, not a crash.

## Frontend requirements

- Search box at the top; the query is sent to the backend (`GET /api/graph?q=...`, served by `serve.py`, which calls
  `query.py` and returns its JSON). This `/api/graph` is the seam for EBA later: keep the data provider behind one
  function so it can be swapped.
- Graph: centre node = searched topic; branches = neighbours grouped by edge type, with different styling per type.
  A neural-network look is wanted (nodes as glowing dots, edges as curves). A 3D view (for example 3d-force-graph
  pinned to an exact version) is welcome but a clear 2D view is acceptable if the 3D one is not working.
- Node label: `Title (9-10-11-12)` — grade numbers in parentheses, separated by dashes.
- Click a node: a panel slides in from the RIGHT with a smooth transition. Contents: short summary; "Nerelerde
  kullanılır"; "Hangi kaynaklardan çalışılmalı" (MEB sources first, unverified flagged); "Ne zaman / hangi sırayla
  çalışılmalı" (the `study` field). A close button and Escape close it.
- Show the `status` of each relation on hover or in the panel (verified / inference / unknown), so the student is not
  misled.
- Works on a phone-width screen (panel becomes full width).

## Benchmark (already being written; builders do not see it)

`benchmark/run_benchmark.py <variant-dir>` runs `query.py` for each query in `benchmark/gold/queries.json` and scores
the output against `benchmark/gold/relations.json`. Metrics: recall and precision of gold relations, direction accuracy
for `onkosul`, grade-tag correctness, share of edges with evidence, share of `verified` edges whose quote is really in
the corpus, runtime per query. Results go to `benchmark/results/<variant>.json`.

## Field-test intent

The two prototypes will be shown to students in a classroom. Both must be start-able with one command
(`python serve.py`, then open `http://localhost:8000`). Each variant's `README.md` must say what it does, how to run
it, and what it does NOT do yet (for example: no live EBA link, sources unverified).

## Revizyon 2 (2026-10-09)

Kullanıcının tam isteği ve tasarım kuralları: devtest/run3/BRIEF.md. Bu bölüm SPEC'in üstündedir; 3D grafik ve açık tema zorunludur.
