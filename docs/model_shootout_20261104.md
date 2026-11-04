# Model shoot-out, 2026-11-04 (quality first: tasks passed, judged by tests)

## A. Each model doing every role (17 representative coding tasks)

| Model | Passed | Pass rate | Median s/task | Total min | Note |
|---|---|---|---|---|---|
| qwen3:8b | 12/17 | 71 | 74.9 | 30.4 |  |
| qwen3:4b | 10/17 | 59 | 212.8 | 67.1 |  |
| huihui_ai/qwen3-abliterated:8b | 8/17 | 47 | 64.2 | 19.4 |  |
| huihui_ai/qwen3-abliterated:4b | 7/17 | 41 | 49.0 | 24.4 |  |
| granite3.3:8b | 3/17 | 18 | 59.3 | 23.6 |  |

## B. Planner / architect / evaluator candidates, worker = the 4B (10 build and feature tasks)

| Heavy model | Passed | Median s/task | Total min | Note |
|---|---|---|---|---|

## C. Worker + heavy combinations (17 tasks); calls by model show which models really did the work

| Worker + heavy | Passed | Median s/task | Total min | Calls by model |
|---|---|---|---|---|

Small samples: a difference of one or two tasks is within noise. Nothing was changed; roles stay as they were.
