# symbiosis P1 (data layer) log

## 1. Steps

- 20:40 Read PROTOCOL.md and SPEC.md (in full).
- 20:41 Listed symbiosis\ and symbiosis\kb\ (kb has corpus\ and the verinoda index).
- 20:42 Read corpus\topics.json (31 topics), one passage (mitoz.md) and the head of fotosentez.md as a format sample.
- 20:43 Ran verinoda query (call 1, mitoz relations) from kb\. Result was grep-like hits in topics.json, not relations.
- 20:44 Searched the passages with grep for "oksijen" and "mitoz" to count mentions (plain shell grep, not Verinoda).
- 20:44 Ran verinoda query (call 2, oksijen passages). Same kind of result: topics.json lines.
- 20:45 Wrote symbiosis\query.py (stdlib only, reads kb\corpus). Ran it for mitoz, oksijen, zzkelime.
- 20:45 Fixed one spurious edge: the bare terms "dna"/"rna" were creating a nukleik-asitler edge for every DNA sentence. They stay as query aliases, not as edge terms. Re-ran the three queries.
- 20:46 Wrote this log.

## 2. Files read

- C:\Users\ozcin\biyo-platform\devtest\run2\PROTOCOL.md
- C:\Users\ozcin\biyo-platform\SPEC.md
- C:\Users\ozcin\biyo-platform\symbiosis\kb\corpus\topics.json
- C:\Users\ozcin\biyo-platform\symbiosis\kb\corpus\passages\mitoz.md
- C:\Users\ozcin\biyo-platform\symbiosis\kb\corpus\passages\fotosentez.md (first 30 lines)
- C:\Users\ozcin\biyo-platform\symbiosis\kb\.mcp.json (to confirm the kb folder is the Verinoda project)
- Plain grep counts over symbiosis\kb\corpus\passages\*.md (names of passages mentioning oksijen and mitoz; not content)
- Written by me: C:\Users\ozcin\biyo-platform\symbiosis\query.py

Not opened: anything under biyo-platform\archive\, benchmark\gold\, or baseline\. Nothing under kb\.verinoda was read.

## 3. Tool results

| # | Tool | Question | Approx. result size |
|---|------|----------|---------------------|
| 1 | verinoda CLI `query` (cwd kb\, `--max-items 8`) | which corpus topics relate to mitoz | about 5,300 chars |
| 2 | verinoda CLI `query` (cwd kb\, `--max-items 6`) | which passages mention oksijen, and which topic is it central to | about 5,000 chars |
| 3 | Bash grep (not Verinoda) | which passages contain "oksijen" / "mitoz" | under 1,000 chars |
| 4 | `python query.py mitoz` (test run 1) | the SPEC JSON for mitoz | about 7,600 chars |
| 5 | `python query.py oksijen` | the SPEC JSON for oksijen | about 11,200 chars |
| 6 | `python query.py zzkelime` | the SPEC JSON for an unknown word | about 230 chars |
| 7 | `python query.py mitoz` (after the dna fix) | the SPEC JSON for mitoz | about 5,600 chars |

Calls 4 to 6 are my own script runs, not Verinoda. Call 7 is the final mitoz run.

Verinoda usefulness in this phase: both calls returned topics.json lines and passage snippets that matched by keyword. Neither returned the relation sentences. The relation step was written by me, from sentence scanning in query.py. I did not use `analyze`, `plan`, `challenge` or `evidence` (not needed for a stdlib script, and the cost was not justified).

## 4. Problems

- The first version of query.py produced a spurious nukleik-asitler -> mitoz onkosul edge. The "dna" alias matched every DNA sentence. Fixed by excluding the bare "dna"/"rna" terms from edge matching.
- Verinoda CLI output was not what the question needed. The query returns retrieval hits from topics.json rather than relation sentences.
- Turkish console output: printing the parsed JSON to a cp1254 console garbles the Turkish characters. The file written by query.py is UTF-8 (sys.stdout is reconfigured to utf-8), and the JSON parses correctly from the file. The garbling is only in my own display step.
- "oksijen" is not a topic id, title or alias. The centre is chosen by mention count (hucresel-solunum has 4 passages mentions, the highest). The result carries an unknowns note that says so.
- Relation types are heuristic. Only the "öncesinde" cue is used for onkosul direction, and "destek"/"uygula"/"kullan"/"sağla"/"etki"/"yardım"/"gerekli" for destek. Everything else is ortak. Edges are emitted only with a literal evidence quote, so status "verified" means the quote is in the corpus passage, as the SPEC defines it.
- The output was checked only on three queries.

## 5. Not verified

- Whether the edge set matches the gold relations. The gold file (benchmark\gold\) was not opened, as the rules require. No recall or precision was measured here.
- The direction of onkosul edges beyond the one mitoz example (dna-replikasyonu -> mitoz). Other sentences with "öncesinde"/"önce" were not checked by hand.
- The "önce" cue in "X'ten önce" constructions (the direction rule assumes "X öncesinde" order).
- Queries other than the three listed. Turkish synonyms outside the ALIASES table are not covered; the fallback is a passage-count match.
- Runtime per query was not measured (the scripts ran well under a second each, not timed precisely).
- serve.py and web\ were not written (P2), and the server was not started.
- The query.py output has not been validated against a JSON schema; the shape was checked by hand against the SPEC example.
