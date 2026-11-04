# Proposal: Role-to-Model Assignment for Architect

## 1. The idea in one paragraph

Architect should stop treating "the model" as one thing. Each role gets its own model, its own reasoning budget, and its own placement in memory. A strong model with reasoning on plans the work. A fast small model writes the edits from that plan. A tiny model routes on the CPU. Code checks decide pass or fail. Each assignment is scored on tasks passed first and speed second, and the defaults for each hardware class come from Architect's own measurements, not from public leaderboards.

## 2. Evidence that the split works

- **A separate planner and editor raises pass rates.** In Aider's architect/editor tests, o1-preview planning with o1-mini or DeepSeek editing (whole-file format) scored 85.0%, against 79.7% for o1-preview alone (https://aider.chat/2024/09/26/architect.html). That was Aider's older code-editing benchmark, not polyglot.
- **Pairing a model with itself also helps.** Sonnet went from 77.4% to 80.5%, GPT-4o from 71.4% to 75.2%, and GPT-4o-mini from 55.6% to 60.2% when each was both architect and editor (same source). Aider does not say why. My guess, which is not verified, is that separating reasoning from edit formatting does much of the work.
- **Reasoning models gain the most, because they format edits badly.** o1-mini rose from 61.1% to 71.4% with a DeepSeek editor in whole format, or 69.2% in diff format (same source).
- **A cheap reasoning planner plus a strong editor can beat an expensive model used alone.** On polyglot, R1 planning with a Sonnet editor scored 64.0% at $13.29. o1 alone scored 61.7% at $186.50, R1 alone 56.9%, and Sonnet alone 51.6% (https://aider.chat/2025/01/24/r1-sonnet.html). Not every pairing helps: no editor improved o1 or R1 when used as the architect. Later, o3 (high) with GPT-4.1 as editor scored 78.2%, against 76.9% for o3 alone, and most of that gain came from well-formed edits going from 93.8% to 100% (https://leaderboard.steel.dev/leaderboards/aider/).
- **Edit format matters for weak editors.** In diff format, DeepSeek improved every architect it was paired with, including Sonnet (77.4 to 78.9). Aider calls whole-file format slow and likely impractical for interactive use.
- **Plans help small coders.** PlanSearch with GPT-4o-mini beat larger models without search after 4 attempts (https://arxiv.org/pdf/2409.03733). Plan distillation raised APPS pass@1 by more than 130% for a model of about 1B parameters (https://aclanthology.org/2024.lrec-main.521.pdf). Iterative planning with test verification beat debug-only baselines (https://arxiv.org/pdf/2411.14503).
- **Routing and cascades save compute.** RouteLLM cut costs by more than 2x "in certain cases" without losing quality (https://arxiv.org/abs/2406.18665). It routed between GPT-4 and Mixtral, so Architect would need to recalibrate the threshold for its own models. FrugalGPT reports up to 98% cost reduction at GPT-4 quality on specific 2023 datasets (https://arxiv.org/abs/2305.05176).
- **Small specialists are a valid default.** An NVIDIA position paper argues that most agent calls are narrow subtasks suited to small models, and describes a "log, cluster, fine-tune" path (https://arxiv.org/abs/2506.02153). It is an argument, not a benchmark.

**Caveat about this machine.** The measured speeds are 4B at 68 tok/s (334 on edits), 35B MoE at 14-16 tok/s, and 14B dense at 5.1 tok/s. Speed clearly favors "MoE plans, 4B writes" over the 14B dense model. Nothing yet shows it wins on quality. Experiment E1 tests that.

## 3. Role assignments

| Role | Model class | Thinking | Placement (gpu6) |
|---|---|---|---|
| router / classifier | 0.5-1.7B, fixed JSON prompt | off | CPU, resident, 0 VRAM |
| planner / architect | heavy (35B MoE, experts in RAM) | on, capped | on demand, partial GPU |
| generator / editor | worker (4B, coder-tuned if available) | off | GPU, resident |
| debugger | worker first, heavy after repeated failure | on, small cap | follows the tier |
| reviewer / evaluator | code checks first, model second | off or low | on demand |
| judge (tie-break only) | small reward model, 0.6-1.7B | off | CPU, on demand |
| retrieval | 0.6B embedding + reranker | n/a | CPU |
| draft | tiny same-family model | n/a | GPU, only if the A/B test wins |

Notes on these rows:
- **Router.** Arch-Router-1.5B is built for mapping a request to a named route (https://arxiv.org/abs/2506.16655). It was tested on conversational data, not coding, and its license is a research license. A small Qwen model with a fixed prompt is the license-safe default.
- **Judge.** Skywork-Reward-V2 small models are reported to rival much larger reward models (https://arxiv.org/abs/2410.18451). Those models are tuned for chat preferences, not code. So the judge only ranks candidates that already passed the tests, lint and type checks. It never replaces them.
- **Retrieval.** Qwen3-Embedding-0.6B scores 75.41 on MTEB-Code (https://arxiv.org/pdf/2506.05176).

**Plan schema (what to do, not how).** Files to touch, function signatures and intent, acceptance checks, and constraints. A code check validates the schema before the worker runs, so a bad plan goes back to the planner cheaply. Vendor write-ups warn that over-specified plans, and executors too weak to follow them, erase the savings (https://www.morphllm.com/multi-agent-model-routing). That is anecdotal.

**Escalation ladder** (Goose uses the same idea with its lead-failure threshold, https://block.github.io/goose/docs/tutorials/lead-worker):
1. Worker does the task directly. The router chooses this for trivial tasks.
2. Worker plans, worker edits. This is self-pairing.
3. Heavy plans, worker edits in diff format. If a diff fails to apply on a small file, retry in whole-file format.
4. Heavy plans and heavy edits.

Move up one rung only when a code check fails N times. Log every rung, so a RouteLLM-style router can later be trained on Architect's own data. The paper's augmentation set was about 1,500 samples.

## 4. Reasoning budgets

- Add `thinking: {mode: on|off, max_tokens}` to each role in the config.
- Starting values: planner 2-4k, debugger 1-2k, everything else off.
- Enforce the cap by stopping the thinking early and injecting a "write the answer now" prompt. Qwen documents this mechanism (https://qwen.readthedocs.io/en/stable/getting_started/quickstart.html).
- The Qwen3 report says more thinking budget improves coding but slightly hurts long-context tasks (https://arxiv.org/html/2505.09388v1). This was not re-checked in our verification pass, so treat it as unverified.
- One anecdote reports that thinking costs about 10x the tokens. That is reason enough to measure the cost rather than assume it.
- Log thinking tokens and pass rate per role, and tune the caps from those numbers.

## 5. Placement: resident vs on demand

- **Memory is the limit, not the number of models.** Each loaded model pays for its full weights plus its KV cache (https://insiderllm.com/guides/managing-multiple-models-ollama/).
- **Keep VRAM headroom** (for example 600 MB on a 6 GB GPU that also drives the display). One Linux/AMD anecdote reports a whole-PC freeze after VRAM overcommit (https://gitea.dmz.rs/coja/dots/raw/branch/main/fl/.config/llamacpp/README.md).
- **One residency manifest per hardware class** (gpu6, cpu-only, gpu12+). For each role it says resident, on demand, or CPU-pinned.
  - In llama.cpp, apply it with router mode: `--models-max` (default 4, 0 means unlimited), `--models-preset` INI files, `--no-models-autoload`, and `--models-memory-margin` (llama.cpp tools/server README).
  - In Ollama, apply it with `keep_alive` per request (0 unloads right away, a negative value keeps the model loaded) and `OLLAMA_MAX_LOADED_MODELS` (default 3 per GPU). On Windows, restart the tray app after changing these (https://docs.ollama.com/faq).
- **MoE heavy tier.** Use `-ncmoe N` to keep the expert weights of the first N layers in RAM.
- **One base model, several specialists.** llama-server can load several LoRA adapters with `--lora-init-without-apply` and pick them per request through a `lora:[{id,scale}]` field. Requests with different adapter sets are not batched together, so group requests by adapter (llama.cpp server README).
- **Load races.** Architect's scheduler should serialize model loads, and should prefetch the next role's model from the flow YAML. One report says that with `--models-max 1`, requesting a second model while another is loading kills the loading instance. That is unverified.

## 6. Running roles at the same time

- The router and retrieval run on the CPU while the worker generates on the GPU.
- Risk: the CPU models compete with the MoE experts in RAM for memory bandwidth.
- No published benchmark covers this on consumer hardware, so Architect has to measure it.
- Speculative decoding: one homelab A/B run reports +50-100% on GPU, but a loss on CPU (4.0 vs 4.9 t/s). Turn the draft model on only after a per-machine A/B test at install time.
- Mixture-of-Agents gains were shown only with 70-140B hosted models (https://github.com/togethercomputer/MoA). Keep it opt-in and sequential.

## 7. How to measure

- **Benchmark set:** 20-40 tasks from the owner's own repos plus a slice of Aider polyglot, all with executable checks.
- **Metrics, in priority order:**
  1. Tasks passed.
  2. Wall time per task passed.
  3. Tokens, including thinking tokens.
  4. Escalation rate.
  5. Edit-apply failure rate.
  6. Peak VRAM and RAM.
- **Selection rule:** pick the configuration with the highest pass rate. If two are within noise (run each 3 times and use the same seeds where possible), pick the faster one.
- **Output:** store the winner per hardware class as that class's defaults.
- **Later:** log every (task, plan, diff, check result) record. Train QLoRA adapters for the 4B base (a "coder" and a "planner-lite") and promote one only if it beats the base model on a held-out flow. No training run has been measured on this hardware yet.

## 8. Experiments using only installed models (no downloads)

Models used: the 4B worker, the 35B MoE IQ3, the 14B dense model, and the draft model.

1. **E1, the key quality question.** Run the same task set three ways: (a) 4B alone, (b) 14B dense alone, (c) MoE plans, 4B edits in diff format. Report pass rate, then time.
2. **E2, self-pairing.** 4B planning plus 4B editing vs 4B alone. Does Aider's 4-5 point gain hold locally?
3. **E3, edit format.** For setup (c), compare diff vs whole-file format on files under 200 lines, measuring the apply-failure rate and pass rate.
4. **E4, planner thinking cap.** MoE planner thinking off, 1k, 2k, 4k. Measure pass rate per thinking token.
5. **E5, editor thinking.** 4B editor thinking off vs 1k. The expectation is that off wins on speed with no loss in quality.
6. **E6, escalation ladder.** Always-heavy vs ladder rungs 1 to 4. Measure pass rate, mean time and escalation rate.
7. **E7, router accuracy.** Use the 4B in CPU-only mode as a stand-in classifier with a fixed JSON prompt. Compare its labels against hand labels on 40 tasks, and compare end-to-end pass rate against a "route everything to the MoE planner" baseline.
8. **E8, contention.** Measure worker and MoE tok/s alone, then with the 4B classifier running on the CPU at the same time.
9. **E9, speculative decoding.** Draft on vs off for the worker and for the 14B dense model, on GPU and on CPU.
10. **E10, residency.** Measure cold start vs prefetch for MoE load time inside a flow, and check peak VRAM against the headroom margin over a 30-minute run.
11. **E11, plan validation.** Schema check on vs off. Count bad plans caught before the worker runs, and the wasted worker tokens they would have cost.