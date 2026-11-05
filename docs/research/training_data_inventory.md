# Training data inventory (2026-11-05)

Measured on this machine with `py -3.12 tools/data_inventory.py` (read-only; `--json` for the raw numbers). Rules applied
(CLAUDE.md): Claude-written content is never fine-tuning data; the eval `bench` bucket is never trained on; `not_sure` is excluded both ways.

## Headline

**There is almost no training data yet.** Mining every allowed root gives **0 SFT, 0 DPO, 0 distillation, 0 trajectories.**
What looks like "tons of data" is (a) Claude's own work (355 of 378 logbook tasks, 40 Claude Code sessions, 363 reasoning-corpus lines),
(b) the benchmark holdout (42 runs, 223 calls), and (c) self-test fixtures (`fake:7b`, 126 of the ledger's 206 few-shot examples).
All three are legitimately useful, just not as fine-tuning data.

## 1. Every source

| Source | Volume | Written by | Marks (acc / rej / not_sure / unmarked) |
|---|---|---|---|
| `hub_data/evals/runs/train` | 2 runs, 10 calls (qwen3-abliterated:8b 9, qwen3.6-moe 1), family `harness` | local | 0 / 6 / 0 / 4; both runs failed hidden checks |
| `hub_data/evals/runs/bench` | 42 runs, 223 calls (abliterated:8b 115, qwen3.6-moe 80, qwen3:8b 28); flows build 185, feature 24, bugfix 14; families refusal 28, e2e 8, build 3, feature 3 | local | 21 / 186 / 0 / 16; eval verdict 10 pass / 32 fail; 7 runs hold a rejected->accepted step |
| this project's `.arch/runs` | 0 runs | - | - |
| other projects found (home, depth 6): `Downloads/localintel-1.9.0/localintel`, `.../test123` | 3 runs, 0 model calls (template `new` flow: todo/check records only) | - | - |
| portfolio projects | none beyond the above | - | - |
| `ledger.json` | 288 run summaries, 206 few-shot examples | 126 fake:7b fixtures, 79 local, 1 Claude | per-model role counters (n/ok) |
| `logbook.db` | 378 tasks, 28 501 steps | 355 Claude (`by_claude=1`: cc_run 39, claude_code 56, extra 260), 23 not Claude (`extra`) | statuses only, no accept marks |
| `cc_runs/` | 40 Claude Code sessions, 3 383 events | Claude | - |
| `learning/agent_logs`, `reasoning_corpus.jsonl`, `learning/ledger.jsonl` | 56 logs, 363 lines, 47 cost lines | Claude | - |
| `orchestration/test_data` | 878 files (one test journal) | test fixture | - |
| `crew_log.jsonl` | 0 | - | - |
| Studio threads | 0 on disk (`arch_chat.THREADS` is in memory only) | - | - |
| postmortems | 0 `postmortem.md` files in any run | - | - |
| saved live eval reports | 153 task results (69 pass) | local | verdicts + judges |

## 2. What each source may be used for

| Source | SFT | DPO | Distill | Tool traj. | Few-shot / retrieval / stats | Why |
|---|---|---|---|---|---|---|
| train bucket | yes, once a run passes its hidden checks | yes | yes | yes | yes | the only eval bucket meant for training; today 0 passing runs |
| bench bucket | **no** | **no** | **no** | **no** | cost model, triage, prompt fixes only | holdout integrity: training on it makes the benchmark measure memorisation |
| project runs (any folder) | yes (accepted, not assisted, not cached) | yes (same-task pairs) | yes | yes | yes | the person's own verified local work |
| Claude: logbook by_claude, cc_runs, agent_logs, reasoning corpus, `claude:` calls | **no** | **no** | **no** | **no** | retrieval, playbooks, statistics | provider terms; `exclude_from_training()` |
| logbook non-Claude (23 `extra`) | no | no | no | no | stats only | no prompt/reply/marks stored |
| ledger local examples (79) | no (no full prompt) | no | no | no | few-shot (already used that way) | summaries, not traces |
| `fake:7b` fixtures | **no** | **no** | **no** | **no** | none | scripted test replies |
| any `not_sure` mark | **no** | **no** | no | no | stats | checks could not run |
| anything with a credential pattern | no | no | no | no | no | `has_secret` screen |

## 3. Mining every allowed root (`arch_dataset.build`, in memory)

Roots: this folder, `evals/runs/train`, portfolio, and `discover(~, depth=6)` (now in `arch_dataset`). Result:

| SFT | DPO same-task | DPO any-task | distill | trajectories | runs read | eval runs withheld (hidden checks failed) | accepted marks in them |
|---|---|---|---|---|---|---|---|
| 0 | 0 | 0 | 0 | 0 | 5 | 2 | 0 |

Changes to `arch_dataset.py` (all keep the rules; 7 new checks in `arch_selftest_data2.py`, 37/37 pass):

- `discover(base, depth)` + `roots(..., include_discovered=True)`: project runs outside the current folder (skips node_modules, .git,
  AppData, dotfolders, venvs, build dirs).
- **Holdout guard**: `roots()` and `build()` refuse the bench root, and any run whose `eval_task.json` says `bucket != "train"` is
  skipped wherever it was copied (`bench_skipped`).
- `fake:` models are skipped (`fixture`), and `claude:` models are skipped even if the transport field is not `claude`; trajectories too.
- `eval_not_passed_accepted_marks` counts what the hidden-check rule withholds. Step-level samples from train runs that failed their
  hidden judges stay withheld (review 2026-11-05: in-run gates passed work the acceptance tests then rejected); today that is 0 anyway.
- Rejected->accepted pairs within a run were already mined (same-task prefix rule); nothing to fix there.

## 4. Legitimate non-training uses ("extrapolate")

| Use | Data | Quantity |
|---|---|---|
| Cost model (`arch_costmodel`, C = t / p) | bench calls | 223 call points, 273 845 generated tokens over 22 079 s (12.4 tok/s end to end incl. prompt + MoE heavy) |
| Failure triage families (`arch_triage.classify`) | 153 live eval results | pass 69, behaviour 43, wiring 26, tests 8, environment 7 |
| Prompt / gate fixes | failed bench checks | interface_contract 8, contract_smoke 4, acceptance_drop 3, acceptance_tests 2, project tests 2, fake code 1 |
| Eval task generation | e2e + bench failures | 32 failed bench runs = 32 concrete task shapes to turn into new TRAIN-tagged variants (new seeds, not copies) |
| Few-shot from the person's own accepted local replies | ledger `_examples` | 79 local (126 fixtures should be purged; 1 Claude must stay out of training, may stay as retrieval) |
| Playbooks / retrieval | Claude logbook tasks + reasoning corpus | 355 tasks, 28 501 steps, 363 reasoning lines |

## 5. Fastest legitimate volume

Train-tagged families exist: `distributed` (65 tasks, 52 trainable, 13 templates x 4 variants + 1 holdout each) and `harness`
(40, 32 trainable, 8 templates). 84 fixed trainable tasks; `generate(n, seed)` makes fresh variants, which matters because dedupe keys
on the whole user turn: re-running the same 84 tasks adds no SFT rows after the first pass.

Measured yields per run (bench, same models): 5.3 calls, 1 228 generated tokens per call, 0.5 accepted marks, 4.4 rejected,
17% of runs hold a fix pair, 45% of live results pass hidden checks.

| Assumption | Value |
|---|---|
| worker generation speed | ~70 tok/s (fast lane; measured 55-70 for 8B) |
| per task | ~5 calls x ~1 230 tok = ~6 200 tok = ~90 s generation + prompt eval + tests -> ~3-4 min |
| tasks / hour | ~15-20 (one at a time; four in parallel broke each other) |
| SFT / hour | ~15-20 tasks x 45% pass x ~1-2 accepted steps -> **~7-15** |
| DPO / hour | ~17% of runs x 15-20 -> **~3** |
| one 8 h night | **~60-120 SFT, ~25 DPO**, distillation only where heavy is escalated to (slow: 2-16 tok/s) |

Recommendation: run `eval --tags training` (distributed + harness, holdout excluded) overnight on the fast worker with fresh seeds per
pass; add train-tagged variants of the bench's failure shapes (wiring, interface contract) so the data targets the 26 wiring + 43
behaviour failures; keep the MoE heavy as escalation for distillation on the hard templates only. Expect ~200 SFT rows after two to
three nights, the point where `RECIPE.md` says a LoRA starts to learn formatting; ~1 000 needs about two weeks of nights.
