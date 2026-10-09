# Baseline revision 1, data layer (query.py) log

## 1. Steps

- 21:02 Started. Read BRIEF.md, SPEC.md, run2 PROTOCOL.md and run2 baseline-p1.md.
- 21:02 Listed biyo-platform folders (corpus, baseline, devtest). Read corpus/topics.json and the current baseline/query.py.
- 21:03 Read nine passages in full (mitoz, mayoz, dna-replikasyonu, fotosentez, hucresel-solunum, atp-enerji, enzimler, protein-sentezi, nukleik-asitler). Grep over corpus/passages for ordering and need words (önce, öncesi, gerek, temel, dayan, hazırla, bilinmesi).
- 21:04 Rewrote the relation part of baseline/query.py (see section 2). Ran it on mitoz, oksijen, fotosentez, zzqxw, empty text, dna-replikasyonu, enzimler, fermantasyon, solunum, enerji, kalitim, "Hücre", sindirim-sistemi.
- 21:04 Fixed two faults seen in the output: a bare "önce" was read as an ordering cue (dna -> enzimler got a wrong onkosul), and the first-word key "solunum" from solunum-sistemi caught hücresel solunum sentences. Reran the same queries.
- 21:05 Checked the JSON keys against SPEC and checked each evidence quote against its own passage text. Logged this file.

## 2. Files read

- C:\Users\ozcin\biyo-platform\devtest\run3\BRIEF.md
- C:\Users\ozcin\biyo-platform\SPEC.md
- C:\Users\ozcin\biyo-platform\devtest\run2\PROTOCOL.md
- C:\Users\ozcin\biyo-platform\devtest\run2\baseline-p1.md
- C:\Users\ozcin\biyo-platform\corpus\topics.json
- C:\Users\ozcin\biyo-platform\baseline\query.py (read before the rewrite)
- C:\Users\ozcin\biyo-platform\corpus\passages\: mitoz.md, mayoz.md, dna-replikasyonu.md, fotosentez.md, hucresel-solunum.md, atp-enerji.md, enzimler.md, protein-sentezi.md, nukleik-asitler.md (full). The other 25 passages were only seen as grep hits (lines matching the cue words); the grep output was cut for some lines ("[Omitted long matching line]"), so those passages were not read in full.

Not opened: anything under benchmark\gold\, archive\, symbiosis\, verinoda, verinoda-mod. No Verinoda tool, CLI or package. No graphify.

Files written: C:\Users\ozcin\biyo-platform\baseline\query.py (rewritten relation layer; the rest of the file kept).

## 3. Tool results

Approximate sizes from the output as shown.

| Question / purpose | Tool | Approx. result size |
|---|---|---|
| Read BRIEF, SPEC, PROTOCOL, baseline-p1 | Read x4 | ~24,000 chars |
| Folder listing | Bash | ~1,500 chars |
| topics.json, query.py | Read x2 | ~13,500 chars |
| Nine passages in full | Bash (cat) | ~19,000 chars |
| Cue words across all passages | Grep | ~9,000 chars |
| Test runs (JSON to scratchpad files) and quote checks | Bash + python | ~18,000 chars shown |
| Key-shape check and one timing | Bash + python | ~1,000 chars |

## 4. What changed and why

Problems in the old query.py that the run 2 numbers point to:
- Every edge was written as other -> centre, whatever the sentence said, so onkosul direction was often wrong.
- Only the first candidate sentence per pair was kept, and neighbours were cut to 12 in topic-list order, not by evidence strength.
- Every edge was marked "verified", whatever the cue.

Changes:
1. Candidate edges come from every sentence that names two topics (any two, not only the centre). The pair keeps the strongest type: onkosul > destek > ortak. Within a type, the pair with the most supporting sentences wins. Neighbours are ranked on that and cut to MAX_NEIGHBOURS = 12.
2. onkosul only when an ordering cue ("öncesinde", "öncesi", "önceden") follows a topic in the same sentence within 25 characters. That topic is the later one (target); the other topic named in the sentence is the prerequisite (source). Bare "önce" is not a cue (it marks sequence of steps, e.g. "gen aktarımında önce kesici enzimler").
3. destek when a use or support word is in the sentence (kullan, sağla, taşı, üret, yardım, destek, uygula, düzenle, sağlar). ortak otherwise. For destek and ortak the direction is the order of mention in the sentence. This is a heuristic and is not checked against anything.
4. Extra keys (hand-picked, checked against the passage text only): dna-replikasyonu "replikasyon"; enzimler "enzim"; hucresel-solunum "fermantasyon", "oksijenli solunum"; karbonhidratlar "karbonhidrat"; endokrin-sistem "hormon"; mendel-kalitimi "mendel". Also "solunum" added to GENERIC so that the title word of solunum-sistemi does not catch hücresel solunum sentences.
5. status is "verified" only when every evidence quote of the edge is found in its passage text (the check runs at query time); otherwise "inference". In practice all edges pass, because quotes are sentences cut from the passage.
6. Node study.order: 1 prerequisites, 2 centre when it has prerequisites (else 1), 3 topics the centre is a prerequisite for, 4 other related topics. Only the values change; the JSON shape does not.
7. The last unknowns line now says how many onkosul edges there are and that direction and type are rule-based.

JSON shape: unchanged. Checked by key listing: top keys query, center, nodes, edges, unknowns; node keys id, title, grades, summary, uses, study; study keys order, why, sources; edge keys source, type, evidence, status (the internal "support" count is deleted before output). An empty query and an unknown query return nodes [] and one unknown message, no crash.

Results of the test runs (my reading of the prose, not gold):
- mitoz: onkosul dna-replikasyonu -> mitoz ("Mitoz öncesinde, interfazın S evresinde DNA eşlenir"); ortak mayoz -> mitoz. Correct direction for the one prerequisite in the prose.
- dna-replikasyonu: onkosul dna-replikasyonu -> mayoz (from "Mayoz öncesindeki interfazda DNA eşlenir") and -> mitoz. Correct.
- enzimler, fotosentez, fermantasyon, solunum, enerji: destek and ortak edges with quotes. Edge counts went up (mitoz 2, dna 5, enzimler 8, fotosentez 5, hucresel-solunum/oksijen 11).

## 5. Problems

- The prose has very few real prerequisite sentences. Only two onkosul edges exist in the corpus that I could find by reading (both into mitoz and mayoz from DNA replication). Edge recall can only rise so far with this method. Edges such as "Doğal seçilimin etki edebilmesi için kalıtsal çeşitlilik gerekir" (evrim) have no topic name on the prerequisite side, so they are not made.
- Two false onkosul edges were found and removed during this phase (bare "önce" cue). Other false edges may remain in topics I did not read in full.
- Some co-mention edges are noisy: "enzim" and "karbonhidrat" are generic words and join many passages; "Hücre" (generic) as a query falls into term mode and returns many ortak edges.
- The Turkish console prints mojibake; the files written are UTF-8 and were checked through Python with explicit UTF-8.
- The first draft of my own quote checker compared only the first evidence passage. It was corrected to check each evidence passage. The query.py check itself was always per passage.

## 6. Not verified

- Gold relations and direction accuracy: not checked (gold must not be opened, and the benchmark is the orchestrator's run).
- Runtime per query: one timing only (mitoz 0.57 s including Python start, zzqxw 0.20 s). Not a measured benchmark.
- The 25 passages not read in full: their relations were only seen through cue-word grep, so their onkosul or destek direction is unchecked.
- destek and ortak direction (order of mention) is not checked against anything.
- The benchmark was not run, per instructions.
- The "verified" test is a substring check of a sentence cut from the same passage text, so it cannot fail for this code path. It only guards future edits.
- serve.py and web\ were not touched (other agent). README.md in baseline was not updated.
