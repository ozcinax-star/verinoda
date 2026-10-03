## the mean of 3 runs

| arm | recall | solved | hit@1 | precision | tasks where the tool was used | turns | input tokens | output tokens | cost USD |
|---|---|---|---|---|---|---|---|---|---|
| none | 17.67/21 | 15.7 | 19.3 | 0.853 | 0/21 | 115 | 1,846,076 | 17,273 | 0.97 |
| graphify | 18.28/21 | 16.0 | 19.3 | 0.844 | 12/21 | 114 | 1,906,832 | 18,862 | 1.03 |
| verinoda_mod | 17.78/21 | 16.0 | 19.7 | 0.843 | 0/21 | 113 | 1,863,520 | 17,961 | 1.03 |
| verinoda_setup | 17.56/21 | 15.3 | 19.7 | 0.86 | 0/21 | 121 | 2,031,055 | 19,328 | 1.05 |

| decision (recall, summed over tasks, mean of the runs) | difference [95% CI] | W/T/L |
|---|---|---|
| verinoda_mod - none | +0.11 [-0.50 to +0.67] | 2/18/1 |
| graphify - none | +0.61 [-0.28 to +1.72] | 3/16/2 |
| verinoda_mod - graphify | -0.50 [-1.33 to +0.11] | 1/17/3 |

| each run: recall / sessions that used the tool | none | graphify | verinoda_mod | verinoda_setup |
|---|---|---|---|---|
| run 1 | 17.5 / 0 | 18.33 / 8 | 17.17 / 0 | 17.5 / 0 |
| run 2 | 17.33 / 0 | 18.5 / 9 | 17.83 / 0 | 17.83 / 0 |
| run 3 | 18.17 / 0 | 18.0 / 4 | 18.33 / 0 | 17.33 / 0 |

| noise floor: the same arm, run i - run j | difference [95% CI] |
|---|---|
| none, runs 1-2 | +0.17 [-1.33 to +1.67] |
| none, runs 1-3 | -0.67 [-2.33 to +1.00] |
| none, runs 2-3 | -0.83 [-2.67 to +1.00] |
| graphify, runs 1-2 | -0.17 [-0.50 to +0.00] |
| graphify, runs 1-3 | +0.33 [-0.50 to +1.50] |
| graphify, runs 2-3 | +0.50 [+0.00 to +1.50] |
| verinoda_mod, runs 1-2 | -0.67 [-2.17 to +0.67] |
| verinoda_mod, runs 1-3 | -1.17 [-3.00 to +0.50] |
| verinoda_mod, runs 2-3 | -0.50 [-1.50 to +0.00] |
| verinoda_setup, runs 1-2 | -0.33 [-1.00 to +0.00] |
| verinoda_setup, runs 1-3 | +0.17 [-1.33 to +1.67] |
| verinoda_setup, runs 2-3 | +0.50 [-1.00 to +2.00] |
