## all sessions

| arm | recall | solved | hit@1 | precision | sessions using the tool | turns | input tokens | output tokens | cost USD | seconds |
|---|---|---|---|---|---|---|---|---|---|---|
| none | 35.83/40 | 32 | 35 | 0.657 | 0/40 (0 calls) | 210 | 3,534,557 | 37,057 | 2.43 | 970 |
| graphify | 37.0/40 | 34 | 36 | 0.677 | 7/40 (7 calls) | 201 | 3,295,309 | 34,992 | 2.45 | 790 |
| verinoda_mod | 37.33/40 | 34 | 38 | 0.675 | 6/40 (7 calls) | 212 | 3,507,425 | 37,575 | 2.59 | 775 |
| verinoda_setup | 37.83/40 | 35 | 37 | 0.7 | 2/40 (3 calls) | 216 | 3,582,612 | 38,237 | 2.57 | 742 |

| decision (recall, summed over tasks) | difference [95% CI] | W/T/L | input tokens | turns | seconds |
|---|---|---|---|---|---|
| verinoda_mod - none | +1.50 [+0.00 to +4.00] | 2/38/0 | 0.992 [0.907-1.091] | 1.01 [0.941-1.074] | 0.799 [0.533-1.261] |
| graphify - none | +1.17 [-0.67 to +3.67] | 2/37/1 | 0.932 [0.842-1.034] | 0.957 [0.881-1.043] | 0.815 [0.481-1.444] |
| verinoda_mod - graphify | +0.33 [-1.17 to +2.00] | 2/37/1 | 1.064 [0.972-1.163] | 1.055 [0.976-1.134] | 0.981 [0.729-1.299] |

## without sessions that looked something up on the network

| arm | recall | solved | hit@1 | precision | sessions using the tool | turns | input tokens | output tokens | cost USD | seconds |
|---|---|---|---|---|---|---|---|---|---|---|
| none | 35.83/40 | 32 | 35 | 0.657 | 0/40 (0 calls) | 210 | 3,534,557 | 37,057 | 2.43 | 970 |
| graphify | 37.0/40 | 34 | 36 | 0.677 | 7/40 (7 calls) | 201 | 3,295,309 | 34,992 | 2.45 | 790 |
| verinoda_mod | 37.33/40 | 34 | 38 | 0.675 | 6/40 (7 calls) | 212 | 3,507,425 | 37,575 | 2.59 | 775 |
| verinoda_setup | 37.83/40 | 35 | 37 | 0.7 | 2/40 (3 calls) | 216 | 3,582,612 | 38,237 | 2.57 | 742 |

| decision (recall, summed over tasks) | difference [95% CI] | W/T/L | input tokens | turns | seconds |
|---|---|---|---|---|---|
| verinoda_mod - none | +1.50 [+0.00 to +4.00] | 2/38/0 | 0.992 [0.907-1.091] | 1.01 [0.941-1.074] | 0.799 [0.533-1.261] |
| graphify - none | +1.17 [-0.67 to +3.67] | 2/37/1 | 0.932 [0.842-1.034] | 0.957 [0.881-1.043] | 0.815 [0.481-1.444] |
| verinoda_mod - graphify | +0.33 [-1.17 to +2.00] | 2/37/1 | 1.064 [0.972-1.163] | 1.055 [0.976-1.134] | 0.981 [0.729-1.299] |
