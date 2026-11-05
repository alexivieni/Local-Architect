# Research

## Engineering Local Agentic Coding Harnesses with Orthogonalized Small Language Models
*Systems Architecture, Representation Geometry, and Airgapped Security Analysis* — Alexi Vieni, Technical Report, draft for review,
November 2026. [PDF](Engineering_Local_Agentic_Coding_Harnesses_Vieni_2026.pdf)

> Agentic coding systems put a language model in a loop: retrieve context, propose an edit, run checks, try again. This report
> engineers that loop to run entirely on local, airgapped hardware from three components: small language models orthogonalized
> against refusal, a deterministic client-side harness, and a quantitative design model for the machines they run on. […] The
> model proposes; code decides.

### Where the paper lives in this code

| Paper | What it says | Where it is implemented |
|---|---|---|
| §2 Representation geometry, refusal direction, `P = I − r̂r̂ᵀ` | Orthogonalized (abliterated) small models remove false-positive refusals that halt unattended pipelines | The worker and heavy tiers run orthogonalized models (`huihui_ai/qwen3-abliterated:8b`, the MoE "uncensored" heavy); `arch_port.py` ranks installed models by measured capability |
| §2.4 Deployment gate | Measure before trusting a model | `arch_eval.py` (`eval --compare`), `promote-model`, the 17-task representative set |
| §3 Pipeline and harness | Retrieve → propose → check → retry; code decides | `arch_flows.py`, `arch_dataflow.py` (flows as data), `arch_gates.py`, `arch_tools.py`; see [ARCHITECTURE.md](../ARCHITECTURE.md) |
| §4.1 Memory fit | Weights + KV + overhead must fit | `arch_server.plan()` fit math, `arch_modelmath.py` (`explain`) |
| §4.2 Memory hierarchy (GPU, RAM, storage, network) | Keep the builder on the GPU; never page weights in a loop | Fast lane (`server.fast_lane`), `arch_placement.py`, MoE experts in RAM (`--n-cpu-moe`), SD card for cold data only |
| §4.3 Prompt processing, prefix reuse ρ | Stable text first so retries reuse the cached prefix | Prefix-stable prompts (checked by `arch_selftest_chain`), llama-server prefix cache, per-project KV slots on disk, Claude prompt caching |
| §4.4 Loading and swapping | Fewer swaps; warm before a run | Resident tiers, swap ledger (`arch_chain.py`), idle unloading (`server.idle_free_min`) |
| §4.5 Batching | Parallel candidates on shared prefixes | `sample_n` steps, best-of-N rescue (`arch_flows.best_of_n`) |
| §4.6 Several machines | Route whole roles to other machines | `arch_hosts.py`, hub **Models → Computers** |
| §5.1 Cost of an accepted change, `C = t / p̄` | Time per accepted change = call time / attempt-weighted pass rate | Run traces + `arch_costs.py` (cost per finished task); `arch_routelearn.py` picks the route with the best measured outcome |
| §5.2 Placing roles across models | Thinking roles vs builder roles | Tiers (router/worker/heavy/Claude), `arch_broker.py`, Claude "plan" mode |
| §5.3 Retries, sampling, batching | Strategy-diverse retries, escalation | `arch_progress.py` (Coach, escalation), best-of-N |
| §5.4 Loop detection | Stop repeating the same call | `core.LoopDetected`, stall detection, repeat-read pointer |
| §5.5 Retrieval fusion | BM25 ⊕ TF-IDF | `arch_fts.py`, `arch_index.py` |
| §7 Control plane | Budgets, kill switch, queue | Engine budgets, `.arch/STOP`, `arch_supervisor.py`, spend caps (`arch_cloud.reserve`) |
| §9 Proposed evaluation | 14B and 32B, base vs orthogonalized | `evals/` families + `tools/hybrid_bench.py` + `tools/train_traces.py` |
| §11 Phase 3: speculative decoding, shared-prefix batches, cross-machine overlap | Throughput | n-gram speculation (measured 56 → 306–343 tok/s on edits), draft models (`server.draft`), `arch_fleet.py` GPU scheduling |
| §11 Phase 4: learning from tests | RL from pass/fail rewards on labelled runs | Verified traces → `arch_dataset.py` → `train_lora.py` (SFT/DPO today; GRPO needs a 24 GB GPU) |
