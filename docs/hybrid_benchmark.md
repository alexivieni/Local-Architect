# The hybrid benchmark: what Claude buys you, task by task

`tools/hybrid_bench.py` runs the **same ten tasks** under each *Use Claude* mode and puts the results side by side:

| mode | who does what |
|---|---|
| `off` (local only) | your models do everything |
| `escalation` | your models do everything; Claude is asked only when the local heavy model is also stuck on a step |
| `plan` | Claude does the thinking roles (planner, architect, judge, reviewer); your models write and fix the code |
| `all` | Claude does every coding step (the router stays local) |

The tasks live in `evals/families/hybrid.py`: 3 bug fixes, 3 features, 2 refactors and 2 designs. Each one is a small, self-contained
project with honest tests. It is judged by code only: a **hidden** behaviour check the model never sees, the project's tests, and
structure checks when the request is about structure (a refactor must really remove the duplicate; a design must have a spec with
testable requirements, an architecture with a diagram and an ADR). Every task carries a reference solution and a plausible wrong one,
and the tool's self-test proves the judges pass the first and catch the second.

## How to run it

```bash
python tools/hybrid_bench.py --selftest                     # its own checks; never calls a model or Claude
python tools/hybrid_bench.py --fake                         # the plumbing on the oracle model: no GPU, no Claude
python tools/hybrid_bench.py --modes off                    # local only (free; the baseline the others are read against)
python tools/hybrid_bench.py --usd-cap 3                    # all four modes, at most $3 of Claude in total
python tools/hybrid_bench.py --modes off,plan --tasks pager,relay --usd-cap 0.5
python tools/hybrid_bench.py --list                         # the ten tasks
```

What you need for the cloud modes:
- Settings > Online > Allow online features on.
- A Claude model set (`tiers.cloud` or `online.cloud_model`).
- The Anthropic SDK installed.
- `ANTHROPIC_API_KEY` set in the environment.

A mode that cannot reach Claude is reported as **not run, with the reason**. It is never quietly run as local-only.

**Money.** `--usd-cap` (default $2) is the most the whole benchmark may spend on Claude, across every cloud mode. Before each
cloud task the tool checks what is left. Each task may spend at most `min(--task-usd, what is left)` (default `--task-usd` 0.5); that
becomes `limits.cloud_usd_per_run` for the task's own process. When the cap is reached, the remaining cloud tasks are marked
`skipped`, not run. Claude checks a cap before each request, so the last request of a task can overshoot it by that one request's
cost. Your daily cap (`limits.cloud_usd_per_day`) still applies on top, and every dollar is also counted in the app's own daily
spend.

**Time.** Tasks run one at a time, each in its own process. The tool takes the heavy-job lock, like `eval`, so it never runs beside a
test suite or another benchmark. Local-only takes roughly as long as ten `eval` tasks on your machine (minutes per task on a 6 GB
laptop). Run all four modes overnight.

The report goes to `hub_data/learning/hybrid_<date>.md`. A second run on the same day gets a time suffix. The report holds the table
per mode, a task-by-task grid and the raw numbers (a JSON comment at the end).

### Why this is not `architect.py eval --set online.cloud_tier=...`

- `arch_eval.run` sets `ARCHITECT_EVAL=1`, and `arch_cloud.allowed()` refuses every Claude call under it. That is on purpose: the
  normal benchmark measures *your* models, and a cloud rescue would fake its score. Through the eval command, all four modes would
  have measured the same local models.
- The eval command's own `--set` takes only `representative`.

So the tool calls `arch_eval.run_task` directly. That is the same fixture, the same flow, the same judges and the same verdict
rules. What `ARCHITECT_EVAL` normally switches off is switched off another way:
- `ARCHITECT_NO_CACHE=1`: neither the local reply cache nor Claude's 30-day cache answers.
- `behavior.learn_from_runs = off`: no past replies are shown as examples.
- `behavior.postmortem = off`: no extra model call after a failure.
- No ledger record: nothing a benchmark run did is learned.

The family is also hidden from the normal `eval`, so your `baseline` and `representative` runs are unchanged by its existence. To push
it through the normal eval anyway (local only, plumbing):

```bash
ARCHITECT_HYBRID_TASKS=1 python architect.py eval --tags hybrid --fake
```

## What each number means

| column | meaning |
|---|---|
| passed | tasks whose every judge passed, out of the tasks that ran |
| pass rate | passed / ran. Skipped tasks (cap reached, mode not run) are not counted |
| not sure | a judge could not run (no test runner, a crash in a check). Neither a pass nor a fail; investigate it, do not average it away |
| seconds | wall time of the tasks that ran, from the start of the run to the end of the flow (judging excluded) |
| s per finished task | seconds / passed |
| Claude $ | the sum of `usd` over the Claude calls in the run traces (the same field `arch_costs` reads) |
| $ per finished task | Claude $ / passed: **failed tries included**, because you pay for them too |
| local tokens per finished task | prompt + generated tokens of every local call / passed |
| Claude tokens per finished task | fresh input + output tokens of every Claude call / passed. Prompt-cache reads are billed at 0.1x and are already inside the dollars |
| Claude calls | how many requests went to Claude. In `escalation` this is often 0: Claude is reached only after the local heavy model also stalls |

"Per finished task" is `-` when nothing passed. There is no honest per-task figure then: not zero, not infinity.

Evidence tier: everything in the report was **measured on this machine** with your models and your account's prices at the time.
The dollar figures use the price table in `arch_cloud.PRICES`; check it against the current vendor card if they look off.

## How to read the trade-off

Read every cloud row against the local-only row. That row is free, so it is the baseline.

1. **Pass rate first.** A mode that does not finish more tasks than local-only is buying nothing, whatever it costs. With ten tasks
   one task is 10 points, so treat a difference of one task as noise. Look at the task-by-task grid: which tasks did a mode win?
2. **Then $ per finished task.** That is the price of each extra finished task. Compare it with what that task is worth to you, or
   with the time it would take you to finish it by hand.
3. **Then seconds per finished task.** Claude is often faster per step than a local 7-14B model, but every step pays a network round
   trip. On a small card, `plan` can be both faster and better than local-only, because the local model writes against a sound plan
   and takes fewer retries.
4. **Local tokens per finished task** shows where the work went. In `plan` it should stay close to local-only (your models still write
   the code). In `all` it should fall to almost nothing.

Typical shapes, and what to do about them:

| what you see | what it usually means | what to try |
|---|---|---|
| `plan` passes more than local-only at a few cents per task | your models write well but plan badly | keep `plan` for features and designs |
| `plan` = local-only, `all` higher | the writing is the weak part, not the plan | a stronger local worker (`port`, `explain --workload edits`) before paying for `all` |
| `escalation` shows 0 Claude calls | nothing stalled long enough to escalate | cheap insurance; it costs only on hard tasks |
| `all` passes everything at a high $ per task | Claude can do the work; the question is the price | use `all` for the occasional hard task, not as the default |
| many `not sure` in one mode | a check could not run (environment), not a model result | fix the environment and run that mode again |

One run of ten tasks is a measurement, not a verdict. Before changing a setting on the strength of a small difference, run the mode
again (or with `--tasks` on the tasks that differed).
