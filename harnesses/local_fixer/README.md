# Local-only fixer

A bug report becomes a failing reproduction test, then a minimal fix, on this machine's models only. Nothing leaves the machine.

Goal kind: **fix** (a bug report to a tested fix).

## Run it

```
python arch_harness.py run local_fixer "your goal" --root PATH
python arch_harness.py check local_fixer
```

It wraps the installed flow `bugfix` and runs it through `architect.py run --flow bugfix`, so every protection of that flow applies.
The settings below apply to that one run only (`--set`); your settings file is never changed.
Edits are applied in review mode **ask**.

## Why each part

### Tiers

- **heavy** = the model this machine already uses for it: the larger, slower model a stuck step escalates to; only asked when it can answer in time (limits.escalate_max_s).
- **worker** = the model this machine already uses for it: the fast model on the graphics card that writes and fixes the code; most steps run here.

The worker writes the fix; the heavy tier is asked only when a step is stuck and it can answer in time.

### Flow

The installed `bugfix`; `flow.yaml` is a copy to read, the run uses the installed flow.

| step | kind | role | tier | decided by |
|---|---|---|---|---|
| locate | code | - | - | suspect_files |
| repro | generate | generator | worker | repro_fails_right_reason |
| fix | generate | generator | worker | verify_files |
| review | generate | reviewer | worker | - |

Verdict: pass when `fix.ok`. A check that could not run gives **not sure**, never a pass.

The installed bugfix flow is used as is (not a copy), so its read-only-tests rule and the reproduction gate keep working.

### Claude

Mode **off**: Claude is never asked; every step runs on this machine's models.
Online features are off for this run: nothing leaves the machine.

Local-only by design: online features are switched off for this run, whatever your settings say.

### Settings for this run

| setting | value | what it does |
|---|---|---|
| `behavior.escalate` | `"stalled"` | Escalate when stuck: stalled = after a fix attempt changes nothing, the step continues on the next larger model you have installed and tuned, then returns to its own. |
| `behavior.keep_best_try` | `true` | Undo a try that made things worse |
| `behavior.fake_code` | `"fail"` | Fake-code check: Stubs, assertion-free tests, placeholder data in files a step changed. |
| `behavior.network` | `"offline"` | Network for code the Architect runs |
| `limits.max_revisions` | `3` | Fix attempts per step: Revisions after a failed check. |
| `limits.max_calls` | `40` | Max model calls per run: Hard stop against runaway runs. |
| `online.enabled` | `false` | Allow online features |
| `online.cloud_tier` | `"off"` | Use Claude |

Fake code fails the step, a worse try is undone, and the tests run without network.

Rebuild the files from harness.yaml with `python arch_harness.py compose --spec harnesses/local_fixer/harness.yaml --force`.
