### Home Assistant development set

40 tasks, 2 runs; recall is summed over tasks (a task's mean over its runs).

| arm | recall | gain over `none` [95 % CI] | W/T/L | solved | hit@1 | sessions where the mod put something in front of the model | sessions that called its tool | sessions that used ToolSearch | median seconds | cost per session |
|---|---|---|---|---|---|---|---|---|---|---|
| none | 36.75/40 | - | - | 33.5 | 35.0 | 0/80 | 0/80 | 0/80 | 13 | $0.061 |
| verinoda_setup | 37.83/40 | +1.08 [+0.00 to +2.50] | 3/37/0 | 35.0 | 34.0 | 0/40 | 0/40 | 0/40 | 21 | $0.066 |
| coupled | 36.83/40 | +0.08 [-0.75 to +1.00] | 1/38/1 | 33.0 | 36.0 | 25/40 | 0/40 | 0/40 | 20 | $0.066 |
| full | 36.83/40 | +0.08 [-0.75 to +1.00] | 1/38/1 | 33.0 | 35.0 | 28/40 | 11/40 | 0/40 | 20 | $0.070 |
| gate | 37.00/40 | +0.25 [-0.75 to +1.50] | 1/38/1 | 34.0 | 35.0 | 38/40 | 0/40 | 0/40 | 74 | $0.073 |
| inject | 37.33/40 | +0.58 [-0.50 to +2.00] | 2/37/1 | 34.0 | 36.0 | 40/40 | 0/40 | 0/40 | 69 | $0.066 |
| passive | 36.83/40 | +0.08 [-0.75 to +1.00] | 1/38/1 | 33.0 | 37.0 | 40/40 | 0/40 | 0/40 | 73 | $0.070 |
| passive_gate | 37.33/40 | +0.58 [-0.50 to +2.00] | 2/37/1 | 34.0 | 38.0 | 40/40 | 0/40 | 0/40 | 104 | $0.077 |
| strict | 37.33/40 | +0.58 [-0.50 to +2.00] | 2/37/1 | 34.0 | 35.0 | 38/40 | 15/40 | 0/40 | 51 | $0.076 |
| tool | 36.83/40 | +0.08 [-2.50 to +2.17] | 3/36/1 | 34.0 | 36.0 | 0/40 | 22/40 | 0/40 | 27 | $0.070 |

Pairs:

| pair (recall, summed over tasks) | difference [95 % CI] | W/T/L |
|---|---|---|
| coupled - none | +0.08 [-0.75 to +1.00] | 1/38/1 |
| full - none | +0.08 [-0.75 to +1.00] | 1/38/1 |
| gate - none | +0.25 [-0.75 to +1.50] | 1/38/1 |
| inject - none | +0.58 [-0.50 to +2.00] | 2/37/1 |
| passive - none | +0.08 [-0.75 to +1.00] | 1/38/1 |
| passive_gate - none | +0.58 [-0.50 to +2.00] | 2/37/1 |
| strict - none | +0.58 [-0.50 to +2.00] | 2/37/1 |
| tool - none | +0.08 [-2.50 to +2.17] | 3/36/1 |
| coupled - verinoda_setup | -1.00 [-2.50 to +0.00] | 0/38/2 |
| full - verinoda_setup | -1.00 [-2.50 to +0.00] | 0/38/2 |
| gate - verinoda_setup | -0.83 [-2.17 to +0.00] | 0/38/2 |
| inject - verinoda_setup | -0.50 [-1.50 to +0.00] | 0/39/1 |
| passive - verinoda_setup | -1.00 [-2.50 to +0.00] | 0/38/2 |
| passive_gate - verinoda_setup | -0.50 [-1.50 to +0.00] | 0/39/1 |
| strict - verinoda_setup | -0.50 [-1.50 to +0.00] | 0/39/1 |
| tool - verinoda_setup | -1.00 [-3.00 to +0.00] | 0/39/1 |

Mean recall by the number of gold files (what is left for a tool to add is where `none` is low):

| tasks | none | verinoda_setup | coupled | full | gate | inject | passive | passive_gate | strict | tool |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 gold file | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0.97 |
| 2 gold files | 0.62 | 0.75 | 0.58 | 0.58 | 0.67 | 0.67 | 0.58 | 0.67 | 0.67 | 0.75 |
| 3 or more gold files | 0.67 | 0.78 | 0.78 | 0.78 | 0.67 | 0.78 | 0.78 | 0.78 | 0.78 | 0.78 |
