### seL4 development set

21 tasks, 3 runs; recall is summed over tasks (a task's mean over its runs).

| arm | recall | gain over `none` [95 % CI] | W/T/L | solved | hit@1 | sessions where the mod put something in front of the model | sessions that called its tool | sessions that used ToolSearch | median seconds | cost per session |
|---|---|---|---|---|---|---|---|---|---|---|
| none | 17.89/21 | - | - | 15.7 | 19.7 | 0/63 | 0/63 | 0/63 | 15 | $0.048 |
| verinoda_setup | 17.61/21 | -0.28 [-1.11 to +0.33] | 1/18/2 | 15.3 | 19.7 | 0/63 | 0/63 | 0/63 | 21 | $0.049 |
| coupled | 18.00/21 | +0.11 [-0.89 to +1.11] | 3/16/2 | 15.7 | 19.3 | 36/63 | 0/63 | 0/63 | 24 | $0.053 |
| forced | 18.33/21 | +0.44 [+0.00 to +1.17] | 2/19/0 | 15.7 | 19.7 | 0/63 | 63/63 | 0/63 | 24 | $0.061 |
| full | 17.78/21 | -0.11 [-1.17 to +1.00] | 1/18/2 | 15.3 | 19.0 | 35/63 | 44/63 | 0/63 | 23 | $0.053 |
| gate | 18.28/21 | +0.39 [+0.00 to +1.17] | 1/20/0 | 15.7 | 19.7 | 63/63 | 0/63 | 0/63 | 26 | $0.043 |
| inject | 18.44/21 | +0.56 [-0.28 to +1.61] | 3/17/1 | 15.3 | 20.0 | 63/63 | 0/63 | 0/63 | 25 | $0.045 |
| passive | 18.33/21 | +0.44 [-0.22 to +1.22] | 3/17/1 | 16.0 | 20.0 | 63/63 | 0/63 | 0/63 | 26 | $0.042 |
| passive_gate | 18.83/21 | +0.94 [+0.17 to +1.89] | 4/17/0 | 16.3 | 20.0 | 63/63 | 0/63 | 0/63 | 29 | $0.046 |
| strict | 17.89/21 | +0.00 [-0.44 to +0.39] | 2/18/1 | 16.0 | 19.0 | 45/63 | 43/63 | 0/63 | 24 | $0.042 |
| tool | 18.11/21 | +0.22 [-0.83 to +1.33] | 3/16/2 | 16.0 | 18.7 | 0/63 | 51/63 | 0/63 | 24 | $0.056 |

Pairs:

| pair (recall, summed over tasks) | difference [95 % CI] | W/T/L |
|---|---|---|
| coupled - none | +0.11 [-0.89 to +1.11] | 3/16/2 |
| forced - none | +0.44 [+0.00 to +1.17] | 2/19/0 |
| full - none | -0.11 [-1.17 to +1.00] | 1/18/2 |
| gate - none | +0.39 [+0.00 to +1.17] | 1/20/0 |
| inject - none | +0.56 [-0.28 to +1.61] | 3/17/1 |
| passive - none | +0.44 [-0.22 to +1.22] | 3/17/1 |
| passive_gate - none | +0.94 [+0.17 to +1.89] | 4/17/0 |
| strict - none | +0.00 [-0.44 to +0.39] | 2/18/1 |
| tool - none | +0.22 [-0.83 to +1.33] | 3/16/2 |
| coupled - verinoda_setup | +0.39 [-0.78 to +1.56] | 3/16/2 |
| forced - verinoda_setup | +0.72 [+0.00 to +1.83] | 3/18/0 |
| full - verinoda_setup | +0.17 [-0.89 to +1.28] | 3/16/2 |
| gate - verinoda_setup | +0.67 [+0.00 to +1.56] | 3/18/0 |
| inject - verinoda_setup | +0.83 [-0.17 to +2.11] | 4/16/1 |
| passive - verinoda_setup | +0.72 [+0.17 to +1.33] | 5/16/0 |
| passive_gate - verinoda_setup | +1.22 [+0.22 to +2.50] | 5/16/0 |
| strict - verinoda_setup | +0.28 [-0.50 to +1.22] | 2/17/2 |
| tool - verinoda_setup | +0.50 [-0.83 to +2.00] | 4/15/2 |

Mean recall by the number of gold files (what is left for a tool to add is where `none` is low):

| tasks | none | verinoda_setup | coupled | forced | full | gate | inject | passive | passive_gate | strict | tool |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 gold file | 1.00 | 1.00 | 0.98 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0.98 |
| 2 gold files | 0.46 | 0.38 | 0.58 | 0.50 | 0.33 | 0.46 | 0.50 | 0.50 | 0.62 | 0.42 | 0.54 |
| 3 or more gold files | 0.69 | 0.70 | 0.67 | 0.78 | 0.81 | 0.81 | 0.81 | 0.78 | 0.78 | 0.74 | 0.76 |
