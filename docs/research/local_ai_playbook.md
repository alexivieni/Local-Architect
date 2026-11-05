# Local AI playbook: the rules `arch_swap` checks

Evidence tiers (CLAUDE.md order): **[M]** measured on this machine > **[B]** vendor card / llama.cpp docs / well-replicated benchmark >
**[C]** community practice. `arch_swap.lint(cfg)` checks the current settings against these rules; `arch_swap.plan()` applies the
per-model ones to every option. Each rule id below is the `rule` field of a lint finding.

| rule | the rule | why | evidence |
|---|---|---|---|
| `worker_on_card` | The worker runs 100% on the card, or it is not the worker. | It makes most calls; a dense model with layers in RAM is bound by RAM bandwidth (19.5 GB/s here vs 158 on the card): 3-10x slower per token. | [M] bandwidth.json; qwen3-14b split measured 2.3-2.8 tok/s vs 8B on card 55+ |
| `code_quant` | Q4_K_M or better for code; never Q2/IQ2 for code; Q3/IQ3 only with a measured pass. | Low-bit quants lose exact-token fidelity first, and edits must reproduce lines exactly. | [B] llama.cpp quant perplexity tables; [C] community coding reports |
| (plan) | The builder is at least ~4B effective. | Sub-4B models fail edit formats often enough that their speed does not pay. | [C]; measure with `swap --trial` |
| `kv_q8` | KV cache at q8_0 by default; f16 only when there is room. | Halves cache memory at near-zero quality cost with flash attention. | [B] llama.cpp KV-quant PR measurements |
| `ctx_per_role` | Router ~4k, planner/reviewer ~8k, builder the full window the worker server holds. | Window size is KV memory and prompt time; a planner rarely needs more than the plan + interface. | [B] KV arithmetic; [M] served windows in `server plan` |
| `moe_cpu_ollama` | A MoE heavy model goes beside the worker with only its experts in RAM (`--n-cpu-moe`), never CPU-only via Ollama while our worker holds the card. | Attention + shared weights on the card, few experts read per token from RAM. CPU-only reads everything from RAM. | [M] heavy beside the worker 14-16 tok/s vs 8.2 tok/s via Ollama |
| `thinking` | Thinking off for code edits, on for plan/review. | Thinking costs time and drifts the edit format; it helps decomposition and judging. | [B] Qwen3 card (thinking/non-thinking modes); [C] |
| `draft` | A draft only with the worker's identical tokenizer (model, pre-tokenizer, vocab) and <= 1/3 its size; else n-gram or MTP. | A draft must propose exactly the tokens the worker would; a big draft costs more than it saves. | [B] llama.cpp speculative docs; [M] qwen3:0.6b draft 66-125 tok/s vs n-gram 306-343 on edits |
| `swap_churn` | Avoid per-call model swaps; a role model outside the tiers swaps on every call. | One load costs ~5-15 s on this machine class. | [M] restart cost 5-8 s, load_s 24 s for the 13 GB MoE |
| `vram_headroom` | Leave >= 300 MiB of graphics memory free. | The driver pages to shared memory and decode collapses without warning. | [C]; [M] desktop share measured as desk_gb |
| `ram_headroom` | Leave >= 4 GB of RAM after models, Windows (~6 GB) and Docker/WSL. | Windows pages to disk and every tier crawls. Plan around Docker/WSL, never change it. | [C]; [M] `arch_hw.vm_memory` |

## How a swap is decided
1. `swap --plan --tier T | --job J`: per installed model, placement math (layers on card, KV at q8_0 for the role's window, MoE
   expert offload and its RAM, VRAM/RAM headroom), decode tok/s (bandwidth model [E] against the passport's measured figure [M]; the
   lower is used, because the passport does not record the placement it was measured at), measured C / pass rate with Wilson CI from
   `select` results, the family prior, speculation fit, recommended settings. Score = measured C when >= 8 decided tasks exist,
   else a prior-adjusted estimate (labelled on every option).
2. `swap --trial T MODEL`: edit-format probe + 3 tasks of the job + decode speed, under the heavy-job lock. pass = format ok and
   >= 2/3 passed and speed over the target's floor; not_sure otherwise unless nothing passed (fail).
3. `swap --apply T MODEL --yes`: refused without a passed trial in the last 7 days (unless `--force`, recorded), writes with an
   undo record; `swap --undo` restores.
