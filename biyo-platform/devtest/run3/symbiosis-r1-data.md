# symbiosis R1 (data layer, run 3 revision) log

Scope: query.py only. web\ and serve.py were not touched. Nothing under benchmark\gold\ or archive\ or baseline\ was opened.
Nothing was written into kb\ (Verinoda was only queried, not indexed or updated).

## 1. Steps (approximate times, 2026-10-09)

- 21:02 Read BRIEF.md, SPEC.md, run2 PROTOCOL.md, run2 symbiosis-p1.md (previous data log).
- 21:02 Listed symbiosis\ and symbiosis\kb\ (kb holds corpus\ and the Verinoda files .mcp.json and .verinoda).
- 21:02 Verinoda call 1 (kb folder, --max-items 6), to find passages with order/prerequisite wording.
- 21:03 Read topics.json and all 32 passages in kb\corpus\passages (Kaynaklar lines excluded from the read).
- 21:03 Backed up the run-2 query.py to the session scratchpad (outside biyo-platform) and saved run-2 outputs for
  mitoz, oksijen, fotosentez there, for before/after comparison.
- 21:04 Verinoda call 2 (kb folder, --max-items 6), to cross-check order cues. Result was again topics.json lines.
- 21:04 Grep over kb\corpus\passages for the order cues (öncesi, önce, den/dan önce). Used as the cross-check instead.
- 21:04 Rewrote symbiosis\query.py (stdlib only; JSON shape unchanged).
- 21:05 Fixed three problems found in the first outputs (see section 4): the "üre" prefix match, shared process terms
  in mitoz, and plain "önce" being read as a prerequisite cue.
- 21:05 Fixed topic id lookup: "dna-replikasyonu" resolved to nukleik-asitler (via the "dna" alias), because only the
  title and the id with spaces were compared.
- 21:05 Full check over all 31 topic ids plus mitoz, oksijen, zzkelime (34 runs). Results in section 5.
- 21:07 Wrote this log.

## 2. Files read

- C:\Users\ozcin\biyo-platform\devtest\run3\BRIEF.md
- C:\Users\ozcin\biyo-platform\SPEC.md
- C:\Users\ozcin\biyo-platform\devtest\run2\PROTOCOL.md
- C:\Users\ozcin\biyo-platform\devtest\run2\symbiosis-p1.md
- C:\Users\ozcin\biyo-platform\symbiosis\query.py (run-2 version, before the rewrite)
- C:\Users\ozcin\biyo-platform\symbiosis\kb\corpus\topics.json
- C:\Users\ozcin\biyo-platform\symbiosis\kb\corpus\passages\*.md (all 32 files, body text only)
- C:\Users\ozcin\biyo-platform\symbiosis\kb\.mcp.json (directory check only; same as run 2)

Written by me:
- C:\Users\ozcin\biyo-platform\symbiosis\query.py (rewritten)
- C:\Users\ozcin\biyo-platform\devtest\run3\symbiosis-r1-data.md (this log)
- Scratch files in the session scratchpad only (run-2 backup, before/after JSON, check output).

## 3. Tool results

| # | Tool | Question / input | Approx. result size | Used? |
|---|------|------------------|---------------------|-------|
| 1 | Verinoda CLI `query` (cwd kb\, --max-items 6) | which passage sentences say one topic must be learned before another | about 5,300 chars | Only to locate passages. Hits were topics.json lines and the nukleik-asitler passage, not relation sentences. Not used for the relation logic. |
| 2 | Verinoda CLI `query` (cwd kb\, --max-items 6) | order cues (öncesinde, önce, öğrenilmesi gerekir, anlaşılmasıyla) | about 5,100 chars | No. Again topics.json lines. |
| 3 | Grep (not Verinoda) over kb\corpus\passages | cue words: öncesi, önce, den/dan önce | about 4,000 chars | Yes, as the cross-check of the order cues (see section 4). |
| 4 | `python query.py` before the rewrite (run-2 version) | mitoz, oksijen, fotosentez | about 5.6k, 11.2k, 16.6k bytes | Only as the before-state for comparison. |
| 5 | `python query.py` after the rewrite | mitoz | about 8.1k bytes | Checked by hand. |
| 6 | `python query.py` after the rewrite | oksijen | about 23.7k bytes | Checked by hand. |
| 7 | `python query.py` after the rewrite | fotosentez, enzimler, mayoz, dna-replikasyonu | about 23k, 29k, 12k, 19k bytes | Checked by hand; these runs exposed the bugs in section 4. |
| 8 | `python query.py zzkelime` | unknown word | about 0.2k bytes | Yes: nodes [] with a message, as the SPEC requires. |
| 9 | Check script over 34 queries (31 topic ids + 3) | see section 5 | not printed | Yes. |

Byte sizes are UTF-8 file sizes; Turkish letters take two bytes, so characters are slightly fewer.

## 4. What I changed, and why

Problem from run 2: edge recall 0.38 and direction accuracy 0.03 (from the brief). The run-2 mitoz output had only 2 edges,
and the corpus has very few explicit order statements.

Changes in query.py:

1. More edges. A second evidence level was added. When two topics share a paragraph but no sentence, the edge is
   "ortak" with the quote of the sentence that names the other topic and status "inference". Run 2 had no such edges.
2. Separate relation terms. EDGE_TERMS holds concept terms used only for relation matching (for example enzyme names,
   ADP, "kemosentez", "nefron"). ALIASES, used for search (pick_center), is unchanged, so search behaviour does not move.
3. Direction. Only the "öncesinde/öncesi" pattern and "X'den/X'dan/X'meden/X'madan önce" give onkosul. Plain "önce"
   (adverb, step order) is no longer a cue. The first version read "Eşlenme sırasında önce helikaz enzimi ..." as
   enzimler -> dna-replikasyonu onkosul. That is a step order, not a prerequisite.
4. Conflicting directions. When passages give a pair opposite onkosul directions, the majority direction is used, the
   status is "inference", and an unknowns note counts the conflicts. (No conflict occurs on the current corpus.)
5. Status. "verified" only for a sentence-level quote with no conflict. Paragraph-level quotes are "inference".
   The quote is always a real corpus sentence, checked verbatim (section 5).
6. Ranking. Edges are ranked by type (onkosul > destek > ortak), then status, then evidence count. The neighbour cap
   went from 12 to 16 (MAX_NEIGHBOURS). This is the main reason outputs are bigger (about 18 KB average, from about 6-11 KB).
7. Topic id lookup. pick_center now also accepts the exact topic id ("dna-replikasyonu"). In run 2 and in the first rewrite
   it resolved to nukleik-asitler through the "dna" alias.
8. Two term collisions removed, found in the outputs:
   - "üre" matched the prefix of "üreme" and "üretmesi" and gave false bosaltim-sistemi edges. Removed.
   - "interfaz", "hücre döngüsü", "kardeş kromatit" are shared with mayoz. With the centre mitoz they produced a reversed
     onkosul (mitoz -> mayoz, from "Mayoz öncesindeki interfazda DNA eşlenir"). Removed; phase names were added instead.
9. The JSON shape is the same as SPEC (keys query, center, nodes, edges, unknowns; edges keep source, target, type,
   evidence, status). The internal "conflict" flag is not printed.

Corpus finding (matters for direction): a grep over all passages found only two explicit order sentences:
"Mitoz öncesinde, interfazın S evresinde DNA eşlenir" (mitoz.md) and "Mayoz öncesindeki interfazda DNA eşlenir"
(mayoz.md). Two more say "hücre bölünmeden/bölünmesinden önce DNA ... eşler" (dna-replikasyonu.md, nukleik-asitler.md), but
the sentence has no mitoz or mayoz term, so no edge can use it. Direction therefore cannot be grounded much further from
this corpus with cue rules. I did not add a rule for prerequisite phrasing ("anlaşılmasıyla mümkün olmuştur" in
genetik-muhendisligi.md), because that sentence has two topic terms before the cue and its direction is not decidable.

## 5. Verification actually run

- Check script (Python, run from symbiosis\), over all 31 topic ids plus mitoz, oksijen, zzkelime (34 runs):
  - edges total 264: 201 verified, 63 inference; 5 onkosul edges in total.
  - every evidence quote is found verbatim in its passage file: 0 failures.
  - no verified edge without evidence: 0 failures.
  - every edge endpoint is a node in the output: 0 failures.
  - centre is always the first node; node count is at most 17: no failures.
  - average runtime about 0.30 s per query (wall time of the subprocess); average output 17.9 KB.
- Manual reading of the outputs for mitoz, mayoz, dna-replikasyonu, fotosentez, enzimler, oksijen, zzkelime after each fix.

## 6. Not verified / not done

- The benchmark was NOT run (the orchestrator runs it). Recall, precision, direction accuracy and the other metrics are
  NOT measured for this revision. No improvement is claimed.
- Only 5 onkosul edges exist over the whole corpus, so direction accuracy is still limited by the corpus, not only by query.py.
- Sentence-level "ortak" edges (a sentence names both topics, no cue) are marked "verified" under the SPEC definition (the quote
  is in the corpus), but the relation type is weak. Some of these may be noise, for example fotosentez -> dna-replikasyonu
  from a sentence about chloroplast DNA. I did not separate them into a lower status.
- Paragraph-level "ortak" edges (inference) can be many for a topic that appears in a long passage. Their count per query was
  not measured against the gold.
- README.md in symbiosis\ was not edited (outside my scope). It may still describe the run-2 edge rules and the 12-neighbour cap.
- Synonym coverage was not extended beyond ALIASES; only "oksijen" and the unknown word were checked as queries.
- Output encoding: query.py still reconfigures stdout to UTF-8. Printed Turkish in the Windows console was not checked.
- No linter or formatter was run.
