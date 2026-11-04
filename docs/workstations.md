# Workstation classes: what each one should run, and what is proposed but not built

Everything measured by `tune`, `fit` and `livetest` beats this page; it is the starting point, not the verdict.

| Class | Team shape | Context | Flows worth running | Retrieval | Notes |
|---|---|---|---|---|---|
| **6 GB GPU, 16 GB RAM** (the laptop this was built for) | one resident chat model (7B Q4 or 4B Q4_K_M), tiny embedding model released after use; judge batched | 8-16k; q8_0 KV cache to stay fully on the GPU | `build`, `bugfix`, `ship --stages build,test,review`; `hunt`/`secure` when the pipeline has time | hybrid on | every role change is a swap: prefer a solo crew unless the Scoreboard shows a specialist earning its load time |
| **12 GB GPU** | 14B Q4 builder, 4B router/judge resident together | 16-32k | the whole `ship` pipeline | hybrid on | two models coexist: specialists are nearly free |
| **24 GB GPU** | 32B Q4 builder or 14B Q8; 7B specialists | 32k+ | all flows, `evaluator = each`, `best_of_n 2` | hybrid on, larger embed model | the tier where RL-from-test-rewards becomes possible (proposed below) |
| **Mac unified 32-64 GB** | 32B-70B Q4 single model | 32k | all flows | hybrid on | fast prompt processing matters more than tok/s: prefix-stable prompts pay most here |
| **CPU only** | 4B Q4_K_M, whole-file edits | 4-8k | `bugfix`, small `build`; skip `hunt` | TF-IDF or all-minilm | timeouts auto-derive from measured speed; expect minutes per call |
| **Several machines** | thinking roles on the biggest box (`hosts --plan`), builder local | per machine | everything | per project | only model calls travel; code always runs locally |

## Proposed, not built (say the word)
1. **Workstation presets** (`preset` command): the table above as one applied, undoable settings patch per class, chosen from `tune`'s measurements. Small; mostly glue over existing settings.
2. **Speculative decoding** beside Ollama: a `llama-server` with a draft model gives 1.5-2.5x generation on the same card for code. Needs a second backend in `arch_ollama` (OpenAI-compatible on localhost; still local-only). Medium.
3. **Parallel roles across hosts**: with two or more machines, the judge and the bug hunter can run while the builder works. Needs the engine's call to become schedulable; the sequential path stays the default. Medium-large.
4. **RL from test rewards** (24 GB+): the DPO pairs `dataset` already exports are the first step; GRPO on pass/fail rewards is the second. Large; off this machine.
5. **Tree-sitter chunking** for the index beyond Python (JS/TS/Go/Rust per function instead of line windows). Small, needs a pip package (optional).
6. **Edit-apply with fuzzy SEARCH** for the smallest models (whitespace- and comment-insensitive matching before refusing). Small; risk of wrong edits, so behind a setting.
