# Baseline P1 (data layer) log

## 1. Steps

- ~20:30 Read PROTOCOL.md and SPEC.md.
- ~20:31 Read corpus/topics.json (topic list, grade notes).
- ~20:32 Listed corpus/passages with Glob to see the passage files.
- ~20:33 Grep for "oksijen" and "mitoz" across corpus/passages to find which passages mention them.
- ~20:35 Read corpus/passages/mitoz.md to confirm the passage and Kaynaklar line format.
- ~20:37 Wrote baseline/query.py (stdlib only): topic match by id/title/key, synonym table, term fallback over passages, sentence-based edges with quotes.
- ~20:40 Fixed a leftover helper (other_passage) in query.py.
- ~20:41 Ran query.py for mitoz, oksijen, fotosentez and an unknown word (zzqxw); outputs checked with a small Python summary.
- ~20:44 Wrote this log.

## 2. Files read

- C:\Users\ozcin\biyo-platform\devtest\run2\PROTOCOL.md
- C:\Users\ozcin\biyo-platform\SPEC.md
- C:\Users\ozcin\biyo-platform\corpus\topics.json
- C:\Users\ozcin\biyo-platform\corpus\passages\mitoz.md

Files written: C:\Users\ozcin\biyo-platform\baseline\query.py, this log.
Files listed (names only, not read): corpus\passages\*.md (Glob).

## 3. Tool results

Approximate sizes are estimated from the output as shown to me.

| Question / purpose | Tool | Approx. result size |
|---|---|---|
| Read the protocol | Read PROTOCOL.md | ~3,700 chars |
| Read the spec | Read SPEC.md | ~6,500 chars |
| Topic list and grade notes | Read topics.json | ~5,500 chars |
| Which passages exist | Glob corpus/* | ~3,300 chars |
| Where do "oksijen" and "mitoz" occur | Grep (content) over passages | ~9,500 chars |
| Passage format (Kaynaklar line) | Read mitoz.md | ~2,700 chars |
| Run query "mitoz" (output to a temp file, checked by a short script) | Bash | ~400 chars shown |
| Run queries "oksijen", "zzqxw", "fotosentez" (output to temp files, checked by a short script) | Bash | ~2,600 chars shown |

No Verinoda tool, CLI or package was used. No graphify use. Nothing under archive\, benchmark\gold\, symbiosis\, verinoda-mod or verinoda was opened.

Test results (SPEC JSON, stdout only):
- "mitoz": center mitoz; nodes mitoz, mayoz, dna-replikasyonu; edges mayoz -> mitoz (ortak, quote from mayoz.md), dna-replikasyonu -> mitoz (onkosul, quote "Mitoz öncesinde, interfazın S evresinde DNA eşlenir ..." from mitoz.md).
- "oksijen" (not a topic; term mode): center hucresel-solunum; 11 neighbours, all type ortak, each with a quote containing "oksijen".
- "fotosentez": center fotosentez; 10 edges, mostly destek, a few ortak; all quoted.
- "zzqxw" (unknown): nodes [], edges [], center null, unknowns with one message.

## 4. Problems

- Turkish case folding needed care (I/İ and ı). Handled by a fold function that maps both to ascii forms; the output keeps the original quotes.
- Topic keys derived from titles are noisy ("Hücre ve organeller" gives no usable key because "hücre" is too generic), so GENERIC and EXTRA_KEYS hand lists were added. These lists were written by hand and are only checked against the three queries above.
- Edge type comes from cue words in the quote (öncesinde/önce for onkosul; kullan/sağla/taşı and similar for destek; otherwise ortak). This is a heuristic. For "mitoz" the mayoz edge is typed ortak, which is arguably right but was not checked against any gold data (and gold must not be opened).
- The console showed mojibake when I printed the JSON in the shell; the file written by query.py is UTF-8 (sys.stdout reconfigured to utf-8). Line endings come out as CRLF on Windows.
- I did not re-read any file.

## 5. Not verified

- Only three queries were run. No check of other topic ids, other synonyms, or the full set of topics.
- Edge direction and type were not checked against anything; "verified" only means the quote is a sentence from the corpus passage (it is by construction).
- Performance (runtime per query) was not measured. The benchmark is not run (and must not be read).
- Grade tags come straight from topics.json; the grade_note lines are surfaced in unknowns, not corrected.
- Behaviour with an empty argument was not run (the code returns nodes [] and an unknown message).
- serve.py and web/ were not written (P2).
