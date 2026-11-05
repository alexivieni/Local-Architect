# Claude plans, local builds: feature team

Claude writes the plan with an acceptance contract per sprint and judges the result; the local worker writes every line; the whole suite must pass at the end; a read-only Claude reviewer can be launched afterwards from fleet.json.

Goal kind: **feature** (a new capability in an existing project).

## Run it

```
python arch_harness.py run claude_plans_feature_team "your goal" --root PATH
python arch_harness.py check claude_plans_feature_team
```

It runs its own `flow.yaml` through the flow interpreter in a fresh process.
The settings below apply to that one run only (`--set`); your settings file is never changed.
Edits are applied in review mode **ask**.

## Why each part

### Tiers

- **cloud** = the model this machine already uses for it: a claude: model, used only in the steps the Claude mode allows, within the spend caps.
- **heavy** = the model this machine already uses for it: the larger, slower model a stuck step escalates to; only asked when it can answer in time (limits.escalate_max_s).
- **worker** = the model this machine already uses for it: the fast model on the graphics card that writes and fixes the code; most steps run here.

### Flow

A copy of `plan_local_build`, run as `claude_plans_feature_team`.

| step | kind | role | tier | decided by |
|---|---|---|---|---|
| plan_cloud | ask_json | planner | - | - |
| plan_heavy | ask_json | planner | heavy | - |
| sprints | foreach | - | - | - |
| write | generate | generator* | worker | verify_files |
| judge_cloud | judge | - | - | - |
| judge_local | judge | - | heavy | - |
| save | handoff | - | - | - |
| post_tests_pass | code | - | - | tests_pass |

Verdict: pass when `(sprints.ok) and post_tests_pass.ok`. A check that could not run gives **not sure**, never a pass.

### Extra checks

- `tests_pass` (last): The whole suite must pass.

tests_pass at the end: a judge's score never makes a pass on its own; the suite must be green.

### Claude

Mode **plan**: Claude does the thinking roles (planner, architect, judge, reviewer); the local models write and fix every line.
Your code for those steps leaves this machine. Spend cap per run: $1.5, per day: $5.0. Needs ANTHROPIC_API_KEY. Claude's replies are never training data.

plan = Claude only thinks (plans and judges); code is written locally, so most tokens stay on this machine.

### Budgets

- `calls` = 40: most model calls in the whole run.
- `tokens` = 240000: most tokens (prompt + reply) in the whole run.
- `seconds` = 3600: most wall-clock seconds for the whole run.
- `heavy_calls` = 4: most calls to the heavy tier in the whole run.
- `usd_per_run` = 1.5: most dollars of Claude per run; checked before every request.
- `usd_per_day` = 5: most dollars of Claude per day across the machine.
- `fleet_usd` = 2: the total the agents' spend caps may add up to.

Tighter than the flow's own ceiling, and a hard dollar cap per run and per day.

### Settings for this run

| setting | value | what it does |
|---|---|---|
| `behavior.tests_first` | `"on"` | Tests first (from the planner): on = when a build asks for tests, the planner writes them from the acceptance criteria before any code, and the builder codes against tests it may not edit (small builders often write wrong tests and then chase them). |
| `behavior.evaluator` | `"final"` | Strict judge: final = once per build; each = every sprint; off = code checks only |
| `behavior.keep_best_try` | `true` | Undo a try that made things worse |
| `behavior.network` | `"offline"` | Network for code the Architect runs |
| `online.enabled` | `true` | Allow online features |
| `online.cloud_tier` | `"plan"` | Use Claude |
| `limits.cloud_usd_per_run` | `1.5` | Claude spend per run (USD): A run stops asking Claude once this much is spent. |
| `limits.cloud_usd_per_day` | `5.0` | Claude spend per day (USD) |

### Agents

`fleet.json` is a checked plan for `arch_fleet.launch`. `run` never starts it; you do, after reading it.

- **feature_review** (claude, plan, cap $1.5): owns .arch/fleet/notes/review_feature.md

One read-only reviewer, capped at $1.50, owning only its notes file; you start it after the run if you want it.

Rebuild the files from harness.yaml with `python arch_harness.py compose --spec harnesses/claude_plans_feature_team/harness.yaml --force`.
