# Where the worker should live: placement design for Architect

Owner's copy, 2026-11-07. "Measured" means a number someone read off this machine; "estimate" means arithmetic.

## 1. Why the worker is off the card today

1. The one setting is `server.parallel = 2` in `architect_config.json`; the shipped default is 1 (`arch_core.py:102`).
2. `plan()` fixes the slot count *before* the fit math: `slots = server.parallel` for the worker (`arch_server.py:550`).
3. `_ngl()` then charges one KV window per slot: `total = weights + kv_per_token x ctx x slots`, `per_layer = total / layers`, `layers_on_card = floor((vram - desk - 0.35) / per_layer)` (`arch_server.py:256-258`). One 8192-token q8_0 window for this worker is about 0.6 GB (`kv_gb_per_slot`, `:343`; the same figure the setting's help text quotes, `arch_params.py:166`).
4. One slot: 4.68 + 0.6 = 5.28 GB, all 36 layers (estimate). Two slots: 5.88 GB, only 33 layers fit (plan says 5.39 GB). Three layers in RAM on a ~19.5 GB/s bus is the measured cliff: 22.6 tok/s today vs ~116 yesterday with 36/36.
5. The fast lane could not save it: `_fast_fit` only shrinks the window and passes `slots` through unchanged (`:393-410`, the call at `:403`); at 2 slots even 6144 tokens does not fit, so it reports "layers stay split" (`:407-408`). The slot count is the one knob nobody turns.

Is the second slot worth anything? Measured: two streams aggregate 31.1 tok/s. One full-card stream at ~116 tok/s would finish the same two requests one after another about 3.7x sooner. There is no measured case on this card where parallel=2 wins, and most sprints are a single stream anyway.

On the "draft at 9%": with fast lane on, the plan drops any draft model (`:694-699`) and skips `pick_draft` (`:705`, `not fast`). The 9% is almost certainly the n-gram speculator's acceptance on new-code text, where there is little to copy. Confirm by reading the launched args in `hub_data/server.json` (`:52`) the way `bon_slots` does (`arch_flows.py:200-204`). Hypothesis, not measured.

## 2. Recommended placement policy

Rule in one line: **on a dense worker, never trade a layer for a slot.** `server.parallel` and `server.ctx` are ceilings; the planner picks the largest slot count at the largest window (>= 6144, steps of 1024) at which every layer fits, and says why.

Inputs: `B = vram - desk_measured - 0.35`, `W` worker weights, `k(ctx)` KV per slot, `cap = server.parallel`, `demand` = streams the run wants (crew slots, best-of-N k, sprint = 1).

| Inputs | parallel | ctx | ngl | speculation | heavy runs | LAN hosts | basis |
|---|---|---|---|---|---|---|---|
| This machine, demand 1 (sprint, bugfix) | 1 | 8192 | 36/36 | n-gram on | on demand only (fast lane already drops "beside", `:596`) | not used | measured 22.6 -> ~116 tok/s |
| This machine, demand 2 (best-of-2, 2-agent crew), prompts ~7k | 1, queue | 8192 | 36/36 | n-gram on | on demand | only if `role_hosts` already names a host (`arch_hosts.route_of`, `:67`) | measured: 31.1 split vs ~116 sequential |
| This machine, demand >= 2, prompts fit 4096 | 2 | 4096 | 36/36 | n-gram on | on demand | as above | estimate; rarely reachable, sprint prompts measured ~7k |
| Heavy escalation (planner/evaluator asks for MoE) | worker unchanged | - | MoE shared part on card, experts in RAM (`_moe_split`, `:266-283`) | off | started on demand beside the worker | - | measured 14-16 tok/s beside worker; 25.5 via Ollama at 28% GPU (different server, not comparable) |
| Role pinned to heavy on every run (`heavy_every_run`, `:594`) | worker unchanged | - | as above | off | stays loaded beside | - | existing behaviour, measured 14-16 |
| Desktop grows so 1 slot at 8192 no longer fits | 1 | 7168 or 6144 | 36/36 | n-gram on | on demand | - | existing `_fast_fit` loop, estimate |
| Nothing fits at 1 slot / 6144 | 1 | 6144 | split, loud note | **off** (each rejected guess re-runs the RAM layers) | on demand | - | estimate; note says "smaller quant needed", never auto-switches |
| Any demand, host dead | as local | | | | | `mark_down` -> local for the run (`arch_hosts.py:36`) | existing |
| Bigger card (>= 12 GB) or smaller worker, `slots_fit >= demand` | min(cap, slots_fit) | 8192 | all | n-gram on | beside if still full | as above | rule is numeric, not fixed to this card |

Draft model: stays off on 6 GB (measured 66-78 tok/s new code, 125 edits, vs n-gram 306-343; its f16 cache would cost the single slot). Opt-in via `server.draft = try` only.

## 3. Run-time adaptation and hysteresis

Placement is decided at `start()`/`ensure()` (`:930`, `:1409`) and inside `tune`. Never mid-run: a crew or best-of-N that wants more slots than the launched server has is **told, not restarted** (restarts cost 5-8 s measured and `_make_room`'s no-in-flight rule applies, `arch_core.py:2113-2121`). The "adaptive" proposal's live re-pick with dwell timers is deliberately not taken: it adds a second scheduler beside fast lane, make_room and heavy_beside, and its 10 min / 1.3x thresholds are guesses.

What does adapt:
- **Verify after start** (new): parse "offloaded N/M layers" and real VRAM used from the tier log (next to `_gpu_lost`, `:991`) into the state record as `ngl_actual`, `vram_used_gb`. If `ngl_actual < planned`, shrink ctx one step and restart once, note it; never loop. The measured overhead replaces the 0.35 GB guess on the next plan.
- **Desk** is already re-measured by nvidia-smi on each plan (`:512-528`), so a grown desktop lowers slots/ctx at the next start, not during a run.
- **Speed check**: if measured decode falls under 50% of the passport's best full-card figure, the strip says "slower than measured before" with no automatic change.
- **Speculation**: record n-gram acceptance per task kind from `timings` (`arch_transport.py:223-225` already parses them). Below 20% over 20 new-code calls, note it. No auto switch until the numbers exist.
- GPU asleep -> existing `_gpu_lost` retry; watchdog caps restarts at 3 per 10 min (`:1455`).

## 4. In the code today vs new

Exists:
- Fit math with slots counted: `_ngl` (`arch_server.py:243-259`); fast-lane window shrink `_fast_fit` (`:393-410`); desk measured (`:512-528`); generic Parallel warning (`:573-575`).
- Draft dropped under fast lane (`:694-699`), n-gram `--spec-type` chosen (`:726-745`); heavy beside logic (`:583-611`).
- `bench_variants`/`_bench_variants_run` already accept a `parallel` key (`:1519`, `:1547-1548`) but drive one stream; `measure_variants` stores rows (`arch_autotune.py:642-653`); tune undo record `undo_last` (`:171`).
- `bon_slots` reads the *launched* `--parallel` (`arch_flows.py:186-207`). Crews do **not**: `Crew.__init__` reads the raw setting (`arch_squad.py:914`), so a crew would open 2 threads against a 1-slot server (llama-server queues them, no crash, but unplanned).
- Config backup on every save (`save_config_checked`, `architect_hub.py:1989`); LAN ranking by `tok_s/(1+in_flight)` (`arch_hosts.rank_hosts`, `:319`).

New: slot derivation from fit (`:550`), slots-before-ctx in `_fast_fit`, after-start verification, concurrent bench probe, Crew slot fix, settings preview + `server --undo`, acceptance recording, `server.placement` gate.

## 5. First week, in order

1. `arch_server.py` `plan()`: replace the fixed `slots` at `:550` with the largest `s <= server.parallel` for which `_ngl(..., slots=s)` is full at the configured ctx, and replace the generic note at `:573-575` with "2 slots keep 3 layers in RAM (measured 22.6 vs ~116 tok/s); 1 slot keeps all 36 on the card (5.28 GB)".
2. `arch_server.py` `_fast_fit()`: try fewer slots before shrinking the window, so the fast lane never says "layers stay split" while a one-slot full-card fit exists.
3. `arch_squad.py` `Crew.__init__` (`:914`): take `self.slots` from the launched `--parallel` via `arch_flows.bon_slots`, never the raw setting.
4. `arch_server.py` `start()`/`wait_health()` (`:917`, `:930`): parse "offloaded N/M layers" and VRAM used into the state record and let `plan()` flag a mismatch and reuse the measured overhead.
5. `arch_server.py` `_bench_variants_run()` (`:1535`): add `{parallel:1}` and `{parallel:2}` rows plus a `streams:k` concurrent probe recording aggregate tok/s, and have `arch_autotune.best_settings()` (`:656`) show the winner labelled measured.
6. `architect_hub.py` save path (`:1989`) and `architect.py`: preview the fit before saving `server.parallel/ctx/ubatch`, warn "this would split the worker: Save anyway / Keep", and add `server --undo` restoring the newest `config.backup_*.json`.

Gate for items 1-2: a `server.placement = manual | throughput` choice in `arch_params.SETTINGS` and `arch_core.DEFAULTS`. Ship it as **`throughput`** on this machine (so the ~116 comes back at the next start without a hand flip) but let `arch_selftest_fastlane` run with `manual` so its plan comparisons stay unchanged. Relabel Parallel/Context help text from "how many" to "at most how many". The stored `parallel` stays 2 until the person says yes to the one-time "Set Parallel to 1 to match?" card.

## 6. Trade-offs and what to revisit

- Reading a typed setting as a ceiling changes its meaning. Mitigated by the note, the relabel, the preview-before-save and undo; the alternative (warn only) keeps a 5x loss on every sprint.
- Best-of-N and crews queue on this card: N candidates cost N x single time, which the measurements say is still ~3.7x faster than two split slots, but a 12-24 GB card decides differently. The rule is numeric, so it will.
- The fit formula still guesses the compute buffer (ubatch 512) inside 0.35 GB; item 4 replaces the guess with a measurement over time, but a first start on a new machine can miss by a layer.
- Heavy escalation speed beside the worker is measured (14-16 tok/s); the "step aside then bring back" alternative (two restarts, MoE alone on the card) is only an Ollama-measured estimate (25.5 at 28% GPU) and is not adopted until `bench_variants` can measure it on llama-server.
- Concurrent-scaling and speculation acceptance are estimates until the new bench rows and counters exist; the plan must label them so.
- Revisit after the first week with data: whether 2 slots at 4096 is ever useful, whether n-gram should switch off per request on new-code steps (depends on the build accepting `speculative.n_max`), and whether LAN overflow for candidates 2..N is worth building for a one-card owner.