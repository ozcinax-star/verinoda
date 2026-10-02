Instances in both runs: 40 (c 7, cpp 6, go 1, java 7, javascript 5, python 9, rust 1, typescript 4).

Cited view, first 6,000 characters (the primary comparison of 2026-09-28):

| approach | file recall per instance old -> new | file recall micro old -> new | cited span F1 old -> new | paired per instance (file recall): better / same / worse |
|---|---|---|---|---|
| Verinoda query | 0.561 -> 0.553 | 0.42 -> 0.414 | 0.066 -> 0.064 | 0 / 39 / 1 |
| Verinoda analyze | 0.405 -> 0.465 | 0.293 -> 0.338 | 0.103 -> 0.111 | 8 / 31 / 1 |
| Graphify query | 0.355 -> 0.356 | 0.242 -> 0.248 | 0.004 -> 0.004 | 1 / 39 / 0 |
| BM25 (control) | 0.321 -> 0.321 | 0.217 -> 0.217 | 0.033 -> 0.032 | 0 / 40 / 0 |
