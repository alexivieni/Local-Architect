# From the report to the code: what is implemented

This page checks the technical report, *Engineering Local Agentic Coding Harnesses with Orthogonalized Small Language Models*
([PDF](Engineering_Local_Agentic_Coding_Harnesses_Vieni_2026.pdf)), against this repository. It goes section by section and
equation by equation. Each row names the code that does the work.

Last checked: 2026-11-04, against the code in this commit. Updated 2026-11-05: provenance per run (§7.4), task families (§4, §9),
the scope of the prefix-stability check (§5).

| Status | Meaning |
|---|---|
| **Built** | The code exists, is wired into the running harness, and has self-tests. |
| **Partial** | Part of the idea is built. The row says what is missing. |
| **Not yet** | Specified in the report, not in the code. |

**Summary.** Of the report's 24 numbered equations, 20 are in the code: built into the harness, measured by it, or provided
as a tested calculator. The other four are Eq. 1–3, which compute the refusal direction, and Eq. 13, which splits one model's
layers across machines. Of the 40 rows below, 21 are **Built**, 17 are **Partial** and 2 are **Not yet**. The largest gaps are:
- the orthogonalization procedure itself (the harness runs weights that someone else already orthogonalized),
- splitting layers across machines (Eq. 13),
- the report's evaluation (§9), which has not been run.

---

## §2 Representation geometry (Eq. 1–3)

| Report | Status | In the code |
|---|---|---|
| Refusal direction r̂ = (μ_refused − μ_complied)/‖·‖ (Eq. 1), projection P = I − r̂r̂ᵀ (Eq. 2–3) | **Partial** | The harness **runs** orthogonalized ("abliterated") weights: worker `huihui_ai/qwen3-abliterated:8b`, heavy `eriktonon/qwen3.6-moe-uncensored:iq3`. `arch_port` ranks them like any other model. The procedure itself (collecting activations, computing r̂, rewriting the weights) is **not in this repo**. |
| §2.3 What orthogonalization costs | **Partial** | Capability loss can be measured with `eval --compare` (base vs edited model on the same tasks). No base-vs-edited pair has been measured yet. |
| §2.4 Deployment gate: keep the edit only if over-refusal falls **and** pass rates hold | **Built** (instrument; not yet measured) | The pass-rate half: `arch_eval.compare` blocks a regression, and `promote-model` refuses without a passing compare. The over-refusal half (2026-11-05): `evals/families/refusal.py`, 10 safe coding tasks whose wording trips refusal filters (stop a hung worker, strip XSS, hash passwords against cracking, rate-limit brute-force logins, read attack probes in one's own logs, fuzz one's own parser, close a path traversal, redact card numbers, parse one's own binaries, check localhost ports), judged by code plus the `no_refusal` judge; the report gives a refusal rate per model, and `compare` flags a rise of more than 5 points. No base-vs-edited pair has been measured with it yet. The healing data is built: `arch_dataset` exports rejected → accepted pairs labelled by the verifier. |

## §3 Pipeline and harness

| Report | Status | In the code |
|---|---|---|
| Offline sandbox for model-written code | **Built** | `behavior.network = offline`, the socket guard (`guard_env` / netguard), `arch_sandbox.py` (Windows Job Objects, memory cap). `arch_selftest_offline` fails on the first connection that leaves the machine. |
| Retrieval of a repository skeleton | **Built** | `Engine.focus` / `outline` (AST signatures, matching functions in full), `arch_fts.py` (SQLite FTS5), and the `ls`/`grep`/`search`/`read`/`outline` tools. |
| Verify by code: lint, compile, test | **Built** | `arch_gates.GATES` (30+ named gates), `arch_checks`, `arch_fakecode`. A gate answers True, False or None. None means "could not run" and gives a `not_sure` verdict, never a pass. |
| Feedback loop with escalation (Algorithm 1) | **Built** | `arch_dataflow.py` interprets `flows/*.yaml`. Each step has retries, failure feedback in the prompt tail, a stall check and `escalate: [heavy]`. |

## §4 Hardware model

| Report | Status | In the code |
|---|---|---|
| KV bytes per token κ = 2·L·h_kv·d_h·b (Eq. 4) | **Built** | `arch_modelmath.kv_per_token`, `model_hub.kv_bytes_per_token` (from the GGUF header via `arch_passport`). |
| Fit M + κn + O ≤ V, window n* (Eq. 5) | **Built** | `arch_modelmath.fit`, `arch_server.plan` (fit math checked against the binary's own flags). The fast lane shrinks the window until every layer fits (`FAST_MIN_CTX`). |
| Decode time t_tok ≈ Σ D_τ / B_τ (Eq. 6), GPU/RAM split (Eq. 7) | **Built** for GPU + RAM | `model_hub.fit_model`: `est_tok_s = 1 / (gpu_gb/B_gpu + cpu_gb/B_ram)`. Bandwidths are **measured** on the machine (`arch_hw.bandwidth_from`, `hub_data/bandwidth.json`: GPU 158 GB/s, RAM 19.5 GB/s here). The storage term is left out of the estimate on purpose: models stay on NVMe and the plan keeps them in RAM. |
| MoE: read only the active experts; keep expert tensors, not whole layers, in RAM | **Built** | `arch_server._moe_split` → `--n-cpu-moe N`: attention and shared weights on the GPU, experts in RAM. The heavy tier is a 35B mixture-of-experts model with about 3.8B parameters active per token (256 experts, 8 used). |
| Prompt rate r_p ≈ ηΠ / 2\|θ\| (Eq. 8) | **Partial** | Prefill speed is **measured** per model (`arch_tune.observe`, `prefill_tok_s` in `model_stats.json`). It is not predicted from FLOPs. |
| Swap cost λ (Eq. 9), warm swaps while Σ M_m ≤ R − R₀ (Eq. 10) | **Built** | `arch_chain` swap ledger ("Run economics" in each report), `arch_server` RAM note when a split exceeds RAM − reserve, `make_room` / `step_aside_for`, idle unloading (`server.idle_free_min`). |
| Batching: t_step(N), N ≤ N* (Eq. 11) | **Partial** | The cost of each extra slot is computed (`arch_server`, slot GB, `--parallel N` with `-c ctx·N`). `sample_n` draws N candidates, but **one after another** in scratch copies. The default is `server.parallel = 1`. |
| Roles on different machines; overlap Σ max ≤ Σ sum (Eq. 12) | **Partial** | Roles run on other machines (`arch_hosts`, `role_hosts`, ranked by a measured benchmark, fallback to local). `arch_chain.defer_judge` overlaps the judge with the next step **on one machine**. Concurrent builder and judge on two machines is not built. |
| Layers on different machines (Eq. 13) | **Not yet** | No llama.cpp RPC backend. |
| Choose size, precision and window: max p̄/t (Eq. 14) | **Partial** | `arch_port` ranks models by capability minus the known accuracy cost of each quantization. `arch_modelmath.compare_quants` and `explain` show the trade-offs. p̄/t per model and task family is now measured (`arch_costmodel.from_evals`, below). The automatic search over (\|θ\|, q, n) is not built. |

## §5 Loop model

| Report | Status | In the code |
|---|---|---|
| Call time t = (1−ρ)·n_in/r_p + n_out·t_tok (Eq. 15) | **Built** | `arch_costmodel.call_time`. Prompts are prefix-stable (stable part first, per-try tail last). `arch_selftest_chain` enforces it for the retry templates in the Python flow modules; `arch_selftest_promptprefix` checks the 47 YAML prompt files (`prompts/<flow>/*.md`) the same way (all in order, 2026-11-05). ρ is **measured**: llama-server's exact `cache_n`, recorded per call as `prefix_reused_est`. KV slots persist on disk per project. |
| C = t/p (Eq. 16); C = t/p̄, p̄ = Σ Q_{i−1}p_i / Σ Q_{i−1} (Eq. 17) | **Built** (2026-11-04) | `arch_costmodel.cost`, `p_bar`, `measure` (per role and model from `trace.jsonl` call + mark records, including kept live-eval runs), `from_evals` (per task from saved eval reports). `python architect.py cost`, hub **Models → Cost map** (`GET /api/arch/cost`): each model on each family of tasks as (p, t) with rays of equal C, click-through to the tasks, history across runs. Energy E = P·C with `--watts`. |
| Role placement J = Σ c_r·t_r/p_r + λS (Eq. 18) | **Built** as a calculator | `arch_costmodel.run_time`. The live router uses measured success per flow (`arch_routelearn`) rather than J. |
| Planner placement inequality (Eq. 19) | **Built** as a calculator | `arch_costmodel.planner_pays`, `architect.py cost --planner …`. Self-tests reproduce the report's worked example: 150 s > 110.8 s pays; 83.3 s < 110.8 s does not. Not yet applied automatically. |
| Retries with feedback beat unbatched sampling (Eq. 20); batched candidates (Eq. 21) | **Partial** | Retries with feedback are the default loop. `sample_n` and `behavior.best_of_n` exist. The policy "a batch of N per attempt, then feedback" needs concurrent slots (see Eq. 11). |
| Loop detection h_i = h_{i−1} ∨ f_i = f_{i−1} ∨ h_i = h_{i−2} (Eq. 22) | **Built** | `arch_progress.Coach` (reply and failure fingerprints, four stall states), `LoopDetected` in the tool loop. A repeated identical read returns a pointer instead of the text. |
| Reciprocal rank fusion, k₀ = 60 (Eq. 23) | **Built** | `arch_index.rrf(rankings, k=60)` fuses FTS5 BM25 with TF-IDF. |

## §7 Control plane

| Control | Status | In the code |
|---|---|---|
| 1. Sandbox | **Built** | See §3. `online.enabled = false` by default. Self-tests can never reach a paid model (`arch_cloud.allowed`). |
| 2. Review and undo | **Built** | Every edit has an undo (`.arch/` backups). Settings writes are checked and undoable. Each run keeps a full trace and a one-page receipt (`arch_receipt`). |
| 3. Verification | **Built** | Gates decide. The judge's own verdict field is ignored and recomputed in code. |
| 4. Provenance: model, quantization and weight edit per run; base beside edited; licenses checked | **Partial** | Built per run (2026-11-05): the first answer from each model in a run writes one `provenance` trace record (`arch_core.model_provenance`, `Engine._provenance`) with quantization, family, size, weight edit, base model, license, source and digest, read from the model's passport or its GGUF header on this computer, never the network. The same facts go under `provenance` in `run.json`, and the receipt has a "Models and provenance" section. The license check (`arch_core.license_terms`, shown in the receipt) flags only clear non-commercial or research-only terms; it is a note, never a gate. Every call also records model, tier, transport, prompt reference and system-prompt hash. Not built: keeping the base weights beside the edited ones, which is a storage policy. |
| 5. Unattended operation | **Built** | `arch_supervisor` (SQLite queue, per-job and daily budgets, `STOP` file, alternate flows), spend reserve/settle for any paid step, host fallback to local. |
| 6. Self-improvement gate Gates(H′) ∧ \|checks(H′)\| ≥ \|checks(H)\| (Eq. 24) | **Built** | `arch_selfimprove.gate`: compile, no new fake code, full self-tests in a throwaway copy, a check-count floor, eval regression; zip backup, atomic promote, one-step rollback. |

## §9 Proposed evaluation (H1–H6)

| Hypothesis | Status | What exists |
|---|---|---|
| H1 Orthogonalization: over-refusal falls, pass rate holds (14B, 32B) | **Not yet** | The pass-rate side runs today (`eval`). There is no refusal suite, and no 14B/32B base-vs-edited pair is installed. |
| H2 Decoding follows Eq. 6 | **Partial** (first reading) | [H2_decode_speed.md](H2_decode_speed.md): the CPU router matches the RAM bound (15.1 vs 15.4 tok/s); partial offloads land between the bounds; the abliterated 8B ran mostly from RAM (7.96 tok/s, 9 of 36 layers on the card), fixed by `server.ollama_gpu`; n-gram speculation exceeds the per-pass bound. A controlled run is queued. |
| H3 Swaps warm when Eq. 10 holds | **Partial** | The swap ledger measures each swap's seconds. No study yet. |
| H4 Feedback beats unbatched sampling | **Partial** | `from_evals` gives p and C per model and task family. No matched-calls comparison yet. |
| H5 Prompt processing falls by ≈ 1−ρ | **Partial** | ρ and prefill are recorded per call. No report compares them. |
| H6 Placement sign matches Eq. 19 | **Partial** | The calculator is built and tested. The measured run with a 32B planner has not been done. |

The instrument is ready: 105 fixed tasks with hidden tests in `evals/` (plus 105 training tasks), `eval --compare` as the
regression gate, and now `cost` to turn the results into the report's units.

## §11 Phases

| Phase | Status |
|---|---|
| 1. Dependable loop | **Built**: loop detection, prefix-stable prompts, schema-bound output, hybrid retrieval, code-decided gates, fit sheet, swap ledger, other machines, unattended backlog, gated self-improvement. |
| 2. Measure the model | **Partial**: per-workstation presets are built (`profiles/*.json`, `arch_recommend`). Orthogonalizing a 14B/32B and running §9 has not been done. |
| 3. Throughput | **Partial**: n-gram speculation is built and measured (edits 56 → 306–343 tok/s). Draft speculation is opt-in. Batched candidates with a shared prefix, tree-sitter chunking beyond Python, and roles overlapped across machines are not built. |
| 4. Learning from tests | **Partial**: verified traces → SFT/DPO/trajectories (`arch_dataset`), LoRA (`train_lora.py`), and promotion only when `eval --compare` shows no regression. GRPO is not built. Claude-written work is never used as training data. |

---

## What this machine has measured so far

`python architect.py cost` reads the saved live eval reports in `hub_data/evals/` and groups them by family of tasks. One flow
serves several families: whole-project (e2e) builds and the distributed-systems training tasks both run the build flow. The hub
draws the same numbers as **Models → Cost map**. Below is a selection (RTX 4050 6 GB, 2026-11-04). These are **small samples**
(3–6 tasks per cell), so read them as early readings, not results.

| Model (most calls) | Kind of task | Tasks | Passed | t (s/task) | p | **C (s per passed task)** |
|---|---|---|---|---|---|---|
| huihui_ai/qwen3-abliterated:8b | feature | 3 | 3 | 38.3 | 1.00 | **38.3** |
| huihui_ai/qwen3-abliterated:4b | bugfix | 6 | 6 | 40.4 | 1.00 | **40.4** |
| qwen3:8b | build | 3 | 3 | 58.8 | 1.00 | **58.8** |
| huihui_ai/qwen3-abliterated:4b | build | 3 | 1 | 36.0 | 0.33 | **107.9** |
| huihui_ai/qwen3-abliterated:8b | build | 3 | 1 | 48.5 | 0.33 | **145.6** |
| every model (5) | whole projects (e2e) | 4 each | 0 | 96–274 | 0.00 | **never passed** |

The report says a model should be judged by C, and these rows show why. On `build`, the abliterated 4B is the fastest per task
(36 s), but it passes 1 time in 3, so it costs more per accepted change than base qwen3:8b, which passed all 3 (108 s vs 59 s).
That is one measurement on 3 tasks. Before reading it as a cost of orthogonalization (H1), it needs a refusal suite, more tasks
and repeated seeds. Whole-project builds stop every model so far, so they are the clearest target for harness work.

## Next steps, ranked by what they unlock

1. **Run H1 on the installed 8B pair** (base qwen3:8b vs abliterated 8B): the full build and feature families, three seeds, plus a
   small safe-prompt refusal suite. This makes §2.4's deployment gate complete.
2. **Concurrent sample_n on `--parallel N` slots** (Eq. 11, 21), measured with `cost`.
3. **Apply Eq. 19 automatically** in the broker once enough C data exists for the planner on both tiers.
4. Eq. 13 (llama.cpp RPC) and tree-sitter chunking: Phase 3, larger work.

**Done 2026-11-05: live eval tasks keep their traces.** Each task's folder is temporary, so its trace used to be deleted with it.
That left the per-call cost model with nothing to read. It also meant the training-task runs (`tools/train_traces.py`) never
reached `dataset`. Now `arch_eval.keep_trace` copies each live task's run record to `<data>/evals/runs/<bucket>/.arch/runs/`:
- `train` holds training tasks that are not held out. It is the only bucket `dataset` reads.
- `bench` holds benchmark and held-out tasks. The cost model reads it, but it is never used for training, so the benchmark cannot
  learn its own answers.

Oracle runs keep nothing, and each bucket keeps its newest 1000 runs.

## Measured 2026-11-05 (morning, RTX 4050 6 GB)

| Change | Before | After |
|---|---|---|
| Worker placement (`server.ollama_gpu = auto`, heavy MoE kept in RAM) | 7.96 tok/s, 9/36 layers on card | **70.9 tok/s, 36/36 layers** (~9x) |
| n-gram speculation on edits | 56 tok/s | 306-343 tok/s |
| No-think planning / routing | 120-260 s / 17 s | 19 s / 5 s |
| MoE heavy beside the worker | full swap | 14-16 tok/s, ~1 s switch |
| Claude Council answer (shared prefix first) | $0.27 | $0.18 |

Pass rate has not moved yet: whole-project e2e 0/4 with the abliterated 8B (wiring, circular import, hidden behaviour);
H1 partial 2/6. Throughput is ahead of accuracy. Next: `behavior.test_first` A/B and 4Bx4 vs 8Bx1, both queued.

Shipped this morning: trace fixes (failed eval runs excluded from training, unique kept-run folders), cleanup limited to
our own servers/models and idle slots, broker advice line per run (`behavior.broker`), spec extractor + test-first
acceptance tests (`behavior.test_first`, off by default).
