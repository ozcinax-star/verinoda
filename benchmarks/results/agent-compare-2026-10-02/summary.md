### All 57 questions

| arm | facts found | pinpointed | all-facts questions | answer tokens (mean) | tool calls (mean, self-reported) | sessions using the index tool (self-reported) |
|---|---|---|---|---|---|---|
| none | 213/218 | 195 | 52/57 | 671 | 5.9 | - |
| verinoda | 208/218 | 187 | 47/57 | 666 | 6.3 | 6 |
| graphify | 207/218 | 190 | 50/57 | 655 | 6.3 | 0 |

| pair | wins | ties | losses | facts difference |
|---|---|---|---|---|
| verinoda-none | 1 | 50 | 6 | -5 |
| graphify-none | 3 | 49 | 5 | -6 |
| verinoda-graphify | 3 | 48 | 6 | +1 |

### Out of sample (glow, forge, heldout)

| arm | facts found | pinpointed | all-facts questions | answer tokens (mean) | tool calls (mean, self-reported) | sessions using the index tool (self-reported) |
|---|---|---|---|---|---|---|
| none | 149/151 | 140 | 34/36 | 606 | 5.5 | - |
| verinoda | 145/151 | 135 | 30/36 | 607 | 5.9 | 2 |
| graphify | 149/151 | 140 | 34/36 | 609 | 5.8 | 0 |

| pair | wins | ties | losses | facts difference |
|---|---|---|---|---|
| verinoda-none | 0 | 32 | 4 | -4 |
| graphify-none | 0 | 36 | 0 | +0 |
| verinoda-graphify | 0 | 32 | 4 | -4 |

### In sample (graphify_core, verinoda_user_tr)

| arm | facts found | pinpointed | all-facts questions | answer tokens (mean) | tool calls (mean, self-reported) | sessions using the index tool (self-reported) |
|---|---|---|---|---|---|---|
| none | 64/67 | 55 | 18/21 | 783 | 6.7 | - |
| verinoda | 63/67 | 52 | 17/21 | 766 | 7.0 | 4 |
| graphify | 58/67 | 50 | 16/21 | 735 | 7.1 | 0 |

| pair | wins | ties | losses | facts difference |
|---|---|---|---|---|
| verinoda-none | 1 | 18 | 2 | -1 |
| graphify-none | 3 | 13 | 5 | -6 |
| verinoda-graphify | 3 | 16 | 2 | +5 |

### Per set (facts found)

| set | none | verinoda | graphify |
|---|---|---|---|
| forge_mod (68 facts) | 67 | 66 | 67 |
| glow_mod (50 facts) | 49 | 48 | 49 |
| graphify_core (37 facts) | 36 | 36 | 31 |
| heldout_repoatlas (33 facts) | 33 | 31 | 33 |
| verinoda_user_tr (30 facts) | 28 | 27 | 27 |
