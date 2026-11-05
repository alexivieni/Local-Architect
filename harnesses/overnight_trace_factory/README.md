# Overnight trace factory

Unattended, read-only runs that look through a project with tools and answer a question; every run is a tool trajectory judged by code, kept as training data. The project's tests must still pass at the end.

Goal kind: **traces** (verified local runs kept as training data (never Claude's text)).

## Run it

```
python arch_harness.py run overnight_trace_factory "your goal" --root PATH
python arch_harness.py check overnight_trace_factory
```

It runs its own `flow.yaml` through the flow interpreter in a fresh process.
The settings below apply to that one run only (`--set`); your settings file is never changed.
Edits are applied in review mode **auto**.

## Why each part

### Tiers

- **worker** = the model this machine already uses for it: the fast model on the graphics card that writes and fixes the code; most steps run here.

### Flow

Its own steps, run as `overnight_trace_factory`.

| step | kind | role | tier | decided by |
|---|---|---|---|---|
| look | tool_loop | architect | worker | - |
| remember | tool_loop | architect | worker | - |
| post_tests_pass | code | - | - | tests_pass |

Verdict: pass when `(look.ok) and post_tests_pass.ok`. A check that could not run gives **not sure**, never a pass.

### Extra checks

- `tests_pass` (last): The whole suite must pass.

tests_pass last: a run that left the project broken is not kept as a pass.

### Tools

The tool steps may call only: `ls`, `grep`, `search`, `read`, `outline`, `run_tests`, `memory_read`, `memory_append`.

Read-only tools plus one memory line: a trajectory that edits nothing can run unattended with review auto.

### Claude

Mode **off**: Claude is never asked; every step runs on this machine's models.
Online features are off for this run: nothing leaves the machine.

Claude's replies are never training data, so the factory is local-only (a code rule for goal_kind traces).

### Budgets

- `calls` = 14: most model calls in the whole run.
- `tokens` = 120000: most tokens (prompt + reply) in the whole run.
- `seconds` = 900: most wall-clock seconds for the whole run.

### Settings for this run

| setting | value | what it does |
|---|---|---|
| `behavior.seed` | `1234` | Seed: Same seed = more repeatable. |
| `behavior.network` | `"offline"` | Network for code the Architect runs |
| `behavior.learn_from_runs` | `"off"` | Learn from your own runs: on = a role sees one of its own past replies that passed the checks for a similar request, as a format reference (your successes become few-shot examples). |
| `limits.max_calls` | `20` | Max model calls per run: Hard stop against runaway runs. |
| `online.enabled` | `false` | Allow online features |
| `online.cloud_tier` | `"off"` | Use Claude |

A fixed seed makes runs repeatable; past replies are not shown as examples, so each trace stands on its own.

Rebuild the files from harness.yaml with `python arch_harness.py compose --spec harnesses/overnight_trace_factory/harness.yaml --force`.
