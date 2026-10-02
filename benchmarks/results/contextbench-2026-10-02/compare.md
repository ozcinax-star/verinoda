Instances in both runs: 34 (c 5, cpp 6, go 1, java 6, javascript 4, python 9, rust 1, typescript 2).

Cited view, first 6,000 characters (the primary comparison of 2026-09-28):

| approach | file recall per instance old -> new | file recall micro old -> new | cited span F1 old -> new | paired per instance (file recall): better / same / worse |
|---|---|---|---|---|
| Verinoda query | 0.578 -> 0.568 | 0.484 -> 0.476 | 0.081 -> 0.083 | 0 / 33 / 1 |
| Verinoda analyze | 0.39 -> 0.47 | 0.315 -> 0.379 | 0.123 -> 0.127 | 8 / 26 / 0 |
| Graphify query | 0.376 -> 0.378 | 0.266 -> 0.274 | 0.005 -> 0.005 | 1 / 33 / 0 |
| BM25 (control) | 0.348 -> 0.348 | 0.266 -> 0.266 | 0.048 -> 0.047 | 0 / 34 / 0 |
