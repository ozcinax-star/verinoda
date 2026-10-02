### All 57 questions

| arm | facts found | pinpointed | all-facts questions | answer tokens (mean) | tool calls (mean, self-reported) | sessions using the index tool (self-reported) |
|---|---|---|---|---|---|---|
| none | 212/218 | 184 | 51/57 | 666 | 5.9 | - |
| verinoda_first | 212/218 | 199 | 52/57 | 653 | 4.2 | 57 |
| graphify_first | 214/218 | 191 | 53/57 | 689 | 5.1 | 57 |

| pair | wins | ties | losses | facts difference |
|---|---|---|---|---|
| verinoda_first-none | 3 | 51 | 3 | +0 |
| graphify_first-none | 5 | 49 | 3 | +2 |
| verinoda_first-graphify_first | 2 | 52 | 3 | -2 |

### Out of sample (glow, forge, heldout)

| arm | facts found | pinpointed | all-facts questions | answer tokens (mean) | tool calls (mean, self-reported) | sessions using the index tool (self-reported) |
|---|---|---|---|---|---|---|
| none | 147/151 | 133 | 32/36 | 617 | 5.6 | - |
| verinoda_first | 149/151 | 144 | 34/36 | 607 | 3.9 | 36 |
| graphify_first | 150/151 | 140 | 35/36 | 637 | 4.5 | 36 |

| pair | wins | ties | losses | facts difference |
|---|---|---|---|---|
| verinoda_first-none | 3 | 32 | 1 | +2 |
| graphify_first-none | 3 | 33 | 0 | +3 |
| verinoda_first-graphify_first | 0 | 35 | 1 | -1 |

### In sample (graphify_core, verinoda_user_tr)

| arm | facts found | pinpointed | all-facts questions | answer tokens (mean) | tool calls (mean, self-reported) | sessions using the index tool (self-reported) |
|---|---|---|---|---|---|---|
| none | 65/67 | 51 | 19/21 | 750 | 6.3 | - |
| verinoda_first | 63/67 | 55 | 18/21 | 731 | 4.8 | 21 |
| graphify_first | 64/67 | 51 | 18/21 | 778 | 6.0 | 21 |

| pair | wins | ties | losses | facts difference |
|---|---|---|---|---|
| verinoda_first-none | 0 | 19 | 2 | -2 |
| graphify_first-none | 2 | 16 | 3 | -1 |
| verinoda_first-graphify_first | 2 | 17 | 2 | -1 |

### Per set (facts found)

| set | none | verinoda_first | graphify_first |
|---|---|---|---|
| forge_mod (68 facts) | 65 | 67 | 67 |
| glow_mod (50 facts) | 49 | 50 | 50 |
| graphify_core (37 facts) | 37 | 37 | 37 |
| heldout_repoatlas (33 facts) | 33 | 32 | 33 |
| verinoda_user_tr (30 facts) | 28 | 26 | 27 |
