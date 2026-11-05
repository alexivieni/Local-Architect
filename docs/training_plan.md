# Training plan: verified traces from your own models, overnight

Goal: a large pile of **verified** training samples for the local models, in two areas — distributed-systems building blocks and
agent-harness parts — produced at night by the models on this machine and judged by code. Nothing here calls a cloud model, and
nothing here downloads anything.

## What is in the task set

| family | file | templates | tasks | held out |
|---|---|---|---|---|
| distributed | `evals/families/distributed.py` | 13: token bucket, sliding window, retry with capped backoff + full jitter, idempotent handler, vector clocks, circuit breaker, bulkhead, lock with TTL + fencing tokens, saga, backpressure (hysteresis), consistent hashing, lease queue with dead letters, raft-style election | 65 | 13 |
| harness | `evals/families/harness.py` | 8: verifier gate (True/False/None), token budget checked before the call, stall detector, cost ledger, cache-aware prompt builder, re-ask on an unreadable reply (prefix-stable), tool dispatcher with argument validation, work queue with alternates anchored to the original flow | 40 | 8 |

Each task is one `build`-flow job: a goal that states the public interface exactly, a fixture (`fakeclock.py` or `fakemodel.py`
plus `CONVENTIONS.md`), and four judges: the module and a test file exist, the fixture helper is unchanged, a **hidden** acceptance
check passes (deterministic, standard library only, no real sleeping), and the model's own tests pass. Every task carries a reference
solution and a deliberately wrong one (the classic mistake for that pattern: no cap on the backoff, a lease acked after it expired,
`>=` instead of `>` for a majority, `None` collapsed into `False`, feedback put in front of the prompt, ...). `--check` proves, for
every task, that the reference passes every judge and that the wrong answer is caught.

Variant 4 of every template is tagged `holdout` and is never run by the overnight driver, so the adapter can be measured on 21 tasks
it never trained on. The ids start with `train-`, which sorts after `e2e-`, so `eval --quick` and the representative set are unchanged;
a normal `eval` carries one task of each family (`DEFAULT_N = 1`).

## Before the first night (no GPU needed)

```bash
python evals/families/distributed.py --check      # ends with: ALL N DISTRIBUTED CHECKS PASSED
python evals/families/harness.py --check          # ends with: ALL N HARNESS CHECKS PASSED
python architect.py eval --fake --flow build --tags distributed,harness --n 2 --label night-plumbing
python evals/families/distributed.py --night --fake --n 2 --hours 1
```

The third line runs a few of the tasks through the real build flow with the oracle fake (proves the plan/edit/gate path accepts the
reference solutions). The fourth proves the whole overnight path — keep only passed runs, export — in a folder ending in `_fake`.
**Never train on a `_fake` export**: those samples are `fake:7b` replaying the reference solutions.

## The night

```bash
python architect.py start                                       # resident servers (or Ollama) up, the hub optional
python evals/families/distributed.py --night --hours 8          # both families, one task at a time, holdout excluded
```

What it does, in order:

1. Takes the heavy-job lock (`arch_heavy`, below-normal priority). A `selftest`, `eval`, `tune` or `livetest` started meanwhile waits
   for it, and it waits for them.
2. Sets `ARCHITECT_EVAL=1` for the whole night: no reply cache (`arch_cache` is off), no cloud rescue (`arch_cloud` refuses), nothing
   learned into the few-shot ledger from these runs, no postmortems; `behavior.learn_from_runs = off` on top.
3. Runs each task with `arch_eval.run_task(task, keep=True)` — the same engine, flows, gates and budgets as any run — interleaving the
   two families and round-robin over templates, so a night cut short still covers every pattern.
4. **Keeps a run only when every hidden judge passed** (verdict `pass`). Its project folder, with `.arch/runs/*/trace.jsonl`, moves
   to `<data dir>/training_night/<time>/runs/<task id>/`. Failed and not-sure runs are deleted. One line per task goes to `night.jsonl`.
5. Stops starting new tasks after `--hours`, or when `<data dir>/STOP` exists (the engine also stops between calls on it).
6. Exports from the kept runs with `arch_dataset.build(...)` + `arch_dataset.write(...)` into `<out>/datasets/`
   (`tier-worker.sft.jsonl`, `*.dpo.jsonl`, distillation and tool-trajectory files, `RECIPE.md`). `--min-quality 0.6` there drops
   low-scoring samples at export time; the default keeps all and leaves the choice to `train_lora.py`.

Why a driver instead of `architect.py eval`: `eval` deletes each task's project folder when the task ends (`run_task(keep=False)`), so
its traces never reach `architect.py dataset`, and `dataset` reads one project root (plus the portfolio), not a hundred. The driver
reuses `run_task` and the dataset module unchanged; it only keeps the folders of passed runs and hands all of them to the export.

Measure first, then train: the same tasks as a benchmark (traces discarded, pass rate per flow saved):

```bash
python architect.py eval --flow build --tags distributed,harness --n 65 --label night-baseline
python architect.py eval --flow build --tags holdout --n 65 --label holdout-baseline     # the 21 never-trained tasks
```

## Expected volume per night (RTX 4050 Laptop, 6 GB)

What is **measured on this machine** (CLAUDE.md, 2026-11-03/04):

- the representative live set: 17 tasks, about 2 hours, one at a time -> **~7 minutes per task** (4 of those 17 are whole multi-module
  projects, so single-module tasks like these should be at or below that);
- the worker: ~56 tok/s writing, 306-343 tok/s on edits with the n-gram lookup cache;
- the MoE heavy tier beside the worker: 14-16 tok/s; a dense 14B half in RAM: 1.9-2.7 tok/s (escalation is only offered when the
  bigger model can answer ~1500 tokens within `limits.escalate_max_s` = 240 s, so the dense 14B is effectively never used at night).

What is an **estimate** (not measured yet; the first night's `night.jsonl` replaces it):

| | low | expected | high |
|---|---|---|---|
| minutes per task (single module + tests, 1-3 tries) | 9 | 6 | 4 |
| tasks attempted in 8 h (84 trainable tasks available) | ~53 | ~80 | 84, all of them |
| pass rate of a 4B worker on these tasks | 30 % | 50 % | 70 % |
| passed runs kept | ~16 | ~40 | ~59 |
| accepted SFT samples per passed run (plan + sprint replies + test-file replies) | 2 | 3 | 5 |
| **SFT samples per night** | **~30** | **~120** | **~300** |
| preference pairs (a try that failed, then one that passed, same task) | ~5 | ~20 | ~50 |

`train_lora.py` wants at least 50 samples (`MIN_SAMPLES`), so plan on **two or three nights** before the first adapter, and keep every
night's folder: train on the union. The first night's file answers both open numbers: `secs` and `verdict` per task are in
`night.jsonl`, and the export prints the sample counts.

If a night attempts all 84 tasks with time left, run it again with `--seed 1`: the order changes, the tasks do not (re-running a passed
task adds near-duplicates that `dedupe` collapses to the best sample; a task that failed last night may pass tonight and adds new ones).

## From traces to training: what goes in, and what never does

```
run (local model) -> gates mark each reply  True / False / None
                  -> hidden judges on the finished project: pass?  no -> folder deleted
                  -> arch_dataset: accepted (True) replies -> SFT; failed-then-passed same task -> DPO; worker-failed-then-bigger-tier
                     passed -> distillation; tool loops -> trajectories
                  -> train_lora.py curate: quality filter, curriculum, second check for cloud samples
```

- **Only passed runs.** Two locks: every exported SFT reply passed the run's own gates (tests, fake-code gate, shrink guard, ...), and
  the run as a whole passed a hidden check the model never saw.
- **Only local.** `ARCHITECT_EVAL=1` blocks cloud rescue for the night; `arch_dataset` drops any reply whose transport is `claude`;
  `train_lora.py` drops `meta.by_claude` / a claude teacher again. The oracle fake only answers with `--fake`, and that export lands in a
  `_fake` folder.
- **Never "could not run".** A reply whose checks returned `None` is unverified: not a good example and not a bad one, so it is in
  neither SFT nor DPO.
- **Never a credential**, never a sample too long for a small card (`MAX_CHARS`), never an exact duplicate.
- **Never the holdout.** The 21 `holdout` tasks are not run by the driver.

Then, with the servers stopped (`python architect.py server stop`), first the dry run, which needs no GPU and no packages:

```bash
python train_lora.py --dry-run --data "<data dir>/training_night/<time>/datasets/tier-worker.sft.jsonl" --min-quality 0.6 --curriculum
```

It prints what it would train on, in what order, and why anything was left out. The real run drops `--dry-run` (see the header of
`train_lora.py`: QLoRA of a 4B model at batch 1 and 2048 tokens is meant for ~6 GB; the training part is not yet run on this machine).
Several nights: concatenate their `tier-worker.sft.jsonl` files into one before `--data`.

## Proving the adapter helped

Point the worker tier at the new model for the measurement (hub: Models), then:

```bash
python architect.py eval --flow build --tags holdout --n 65 --label candidate --compare holdout-baseline   # exit 1 on a regression
python architect.py promote-model --tier worker --model my-worker --compare holdout-baseline --dry-run
```

(`eval --set` only takes `representative`; a tier is not overridden from the eval command line.) The holdout tasks are the honest
measure (never trained on). Run the representative set as well
(`python architect.py eval --set representative --label candidate`) so an adapter that learned these patterns but got worse at
everything else is caught. Keep the old model; promote only on a pass of the regression gate.
