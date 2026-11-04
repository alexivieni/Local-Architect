# Architect: speedup and quality ideas ranked by gain per effort

**How to read this.** "Tonight" means you can test it now on models you already have: abliterated/uncensored Qwen3 4B/8B/14B, the 35B MoE IQ3, Granite, Gemma4 e4b and Llama 3.2 3B. "Needs download" means it can't be tested yet. Quality comes first in every measurement (tasks passed on Architect's eval harness), then speed (tok/s, TTFT, wall time).

**Evidence tags:**
- **[confirmed]**: a verifier checked the source this pass.
- **[partly confirmed]**: the mechanism is confirmed, but the gain figure is not.
- **[unverified]**: not checked, or the gain is an estimate.

**Overall limit.** No gain figure has been measured on your machine. Treat every number as a hypothesis until the measurement next to it is run.

## Tier 1: high gain, low effort (do first)

**1. Find out what your abliterated models cost you.** Tonight, partly.
- **Idea:** Run the full eval suite on each abliterated or uncensored Qwen3 model. Keep the stock model as the default coder, and send work to the uncensored one only when a refusal is detected.
- **Evidence:**
  - arXiv 2512.13655 reports GSM8K changes from +1.51 to -18.81 pp depending on the tool and model, and finds that low KL does not mean capability survives. **[unverified]**: the paper is dated after what the verifier could confirm. https://www.arxiv.org/pdf/2512.13655
  - mlabonne describes small regressions caused by abliteration. https://huggingface.co/blog/mlabonne/abliteration
- **Tonight:** You probably don't have the stock base models at the same quant, so a strict before/after test needs a download. What you can do tonight is rank the installed variants against each other: 4B vs 8B vs 14B abliterated, against Granite and Llama 3.2 3B as stock references.
- **Measure:** Tasks passed per model, plus the count of refusals on your suite (likely close to zero for coding, which would mean abliteration buys nothing for this harness).

**2. Run the 35B MoE with experts on the CPU instead of a plain layer split.** Tonight.
- **Idea:** Load it with `llama-server --n-cpu-moe N`, or with `-ot ".ffn_.*_exps.=CPU"`. Lower N until VRAM is full, so as many experts as fit stay on the GPU.
- **Evidence:**
  - The flags are real: PR #11397 added `-ot` and PR #15077 added `--n-cpu-moe` **[confirmed]**. https://github.com/ggml-org/llama.cpp/pull/11397 https://github.com/ggml-org/llama.cpp/pull/15077
  - The reported 15-30 tok/s is anecdotal and mostly from DDR5 machines. On dual-channel DDR4 (about 40-50 GB/s), expect the low end **[unverified]**.
- **Measure:**
  - Tasks passed: 35B MoE IQ3 vs 14B dense, on the heavy-tier eval subset.
  - Speed: run `llama-bench` and record pp512 and tg128 for N = all, then N lowered step by step.
  - Compare with the 14B at its best `-ngl`.
- **Also check:** whether IQ3 on the MoE beats Q4 on the 14B dense at equal VRAM. Below 4 bits, a larger MoE is often said to beat a small dense model, but that is a heuristic **[unverified]**.

**3. Use the right samplers and the right chat template.** Tonight.
- **Idea:**
  - Qwen3 thinking mode: temp 0.6, top_p 0.95, top_k 20, min_p 0.
  - Qwen3 non-thinking mode: temp 0.7, top_p 0.8, top_k 20.
  - Never greedy-decode a thinking model. No repeat penalty on code; use presence_penalty 0-2 only if loops appear.
  - Gemma, Granite and Llama need their own model-card values.
  - Extra check of mine: make sure each model runs with its own Jinja chat template (`--jinja` on llama-server). A wrong template silently costs a lot of quality, especially on abliterated finetunes that keep the Qwen3 format **[unverified as a measured gain]**.
- **Evidence:** The Qwen3 model card warns that greedy decoding causes degradation and endless repetition **[confirmed]**. https://huggingface.co/Qwen/Qwen3-8B The min-p paper is https://arxiv.org/abs/2407.01082
- **Measure:** Tasks passed, plus the count of runs that hit max_tokens or a repetition loop, under current settings vs card settings.

**4. Quantize the KV cache to q8_0 with flash attention.** Tonight.
- **Idea:** `-fa on -ctk q8_0 -ctv q8_0`. Never set K below q8_0. V can try q4_0.
- **Evidence:** PR #7412 shows q8_0 with negligible perplexity change, K more sensitive than V, and quantized V requiring flash attention **[confirmed]**. https://github.com/ggml-org/llama.cpp/pull/7412
- **Gain:** About half the KV memory compared with f16. That does not mean double the model size. Fitting an 8B with 16k context fully on 6 GB only works with roughly Q4 weights, and it is tight.
- **Measure:** Tasks passed f16 vs q8_0 (should be equal). Then the free VRAM, and how many extra GPU layers or how much extra context that frees.

**5. Keep the prompt prefix byte-stable, so the prompt cache hits.** Tonight.
- **Idea:** Put static content first (system prompt, tool schemas, repo map), with deterministic file order, and volatile content last. Turn on `--cache-reuse 256`.
- **Evidence:** `cache_prompt` and `--cache-reuse` (PR #9866) exist **[confirmed]**. https://github.com/ggml-org/llama.cpp/pull/9866
- **Caveat:** cache reuse is limited on SWA or hybrid architectures, which may include Gemma4 and the 35B.
- **Measure:** In the server's timings output, compare `prompt_n` (tokens re-processed) with cached tokens, and record TTFT per call over a 10-step flow, before and after.

**6. Pin threads to the P-cores for any CPU-side work.** Tonight.
- **Idea:** `-t 4` with `--cpu-mask` set to the P-cores, and `-tb 8` for prompt processing. Use the High Performance power plan so Windows EcoQoS does not push the server onto E-cores.
- **Evidence:** The mechanism is real: decode is memory-bound, and threads wait at a barrier for the slowest core. https://github.com/ggml-org/llama.cpp/discussions/4167 The 10-40% figure is **[unverified]**.
- **Applies to:** the MoE and offloaded tiers only. With full GPU offload the gain is about zero.
- **Measure:** `llama-bench -t 4,6,8,12` on the 35B MoE configuration, tg128.

**7. Turn thinking off per step.** Tonight.
- **Idea:** Use `/no_think` (or `enable_thinking=false`) for routing, edits and formatting. Keep thinking for planning and debugging.
- **Evidence:** The Qwen3 technical report describes thinking and non-thinking modes and budget scaling. https://arxiv.org/abs/2505.09388 The budget-forcing idea is from s1: https://arxiv.org/abs/2501.19393 **[partly confirmed]**: both sources are real, but not checked line by line this pass.
- **Measure:** Tasks passed and wall time per flow, with thinking on all steps vs only on plan/debug steps. Capped budgets with forced end-of-thinking are a medium-effort follow-up.

**8. Use KL divergence as a quick screen for any new variant.** Tonight.
- **Idea:** Use `llama-perplexity --kl-divergence-base` on a code corpus from your repo, then `--kl-divergence` per variant (quant level or abliterated). Throw out clear outliers before running a full eval.
- **Evidence:** The flags exist and also report top-token agreement **[confirmed]**. https://github.com/ggml-org/llama.cpp/tree/master/tools/perplexity
- **Use it as a screen only.** KL and task damage can come apart **[unverified]**.
- **Tonight:** You can compare quant levels you already have of the same base, or abliterated 8B vs 14B top-token agreement. A proper base needs the stock model.
- **Measure:** Mean KL and top-1 agreement, and whether they rank variants in the same order as tasks passed.

**9. Keep the worker resident and store models on NVMe only.** Tonight.
- **Idea:** No GGUF on USB or SD storage. Keep mmap on. Set Ollama `keep_alive: -1` for the router. Don't load the same model in both Ollama and llama-server.
- **Evidence:** https://github.com/ollama/ollama/blob/main/docs/faq.md **[confirmed for keep_alive]**.
- **Measure:** Tier-swap latency, cold vs warm, per tier.

**10. Measure the router on its own.** Tonight.
- **Idea:** Put Granite (tool-tuned), Llama 3.2 3B and Qwen3 4B /no_think through the router role. Build a confusion matrix on labelled routing cases from your eval logs.
- **Evidence:** BFCL shows small tool-tuned models are competitive on tool calling. https://gorilla.cs.berkeley.edu/leaderboard.html This is **[unverified for your router task]**.
- **Measure:** Routing accuracy and per-call latency.

## Tier 2: high gain, medium effort

**11. Best-of-N with your code checks as the verifier, sampled in parallel.** Tonight.
- **Idea:** Generate 2-4 candidates at temp 0.7 and keep the first that passes the checks. Run them in one call with `-np N` parallel slots, which costs little extra while decode is memory-bound.
- **Evidence:**
  - Large Language Monkeys: coverage grows log-linearly with samples when a verifier exists. https://arxiv.org/abs/2407.21787
  - CodeT: filtering by tests works. https://arxiv.org/abs/2207.10397
  - Both **[confirmed]**. The batch-cost claim is **[unverified]**: measure it with `llama-batched-bench`.
- **Gain:** Only where the checks really tell good code from bad.
- **Measure:** Tasks passed for N = 1, 2, 4 on the worker vs N = 1 on the heavy tier, plus wall time.

**12. Grammar-constrain the final block only.** Tonight.
- **Idea:** Use GBNF or `json_schema` for router decisions, tool calls and search/replace edits. Let the model reason freely first.
- **Evidence:**
  - llama.cpp grammars **[confirmed]**: https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md
  - "Let Me Speak Freely?" finds strict formats hurt reasoning: https://arxiv.org/abs/2408.02442
- **Caveat:** Grammars guarantee syntax, not correct content. They also add CPU cost per token, and their interaction with speculation needs testing.
- **Measure:** Malformed-output rate, retries, and tasks passed, with and without the grammar, on the 4B worker.

**13. Mask old tool outputs instead of summarizing them.** Tonight.
- **Idea:** Keep the last few tool outputs and replace older ones with placeholders.
- **Evidence:** JetBrains (arXiv 2508.21433) found that masking matched or beat LLM summarization at about half the cost, on large models **[confirmed]**. https://arxiv.org/abs/2508.21433 Carryover to 4-14B models is not shown.
- **Measure:** Tasks passed and prompt tokens per flow.

**14. Keep effective context short and leave YaRN off by default.** Tonight.
- **Idea:** Aim for 8-16k tokens at most on small models, with the key code at the end of the prompt. Enable rope scaling only in a long-context profile.
- **Evidence:**
  - RULER: https://arxiv.org/abs/2404.06654
  - Lost in the Middle: https://arxiv.org/abs/2307.03172
  - The Qwen3 card says static YaRN may hurt short texts **[confirmed]**.
- **Measure:** Tasks passed vs retrieval budget at 4k, 8k, 16k and 32k.

**15. Stream-parse and abort early.** Tonight.
- **Idea:** Cancel a generation on the first parse error or off-format output. Run ruff and tree-sitter checks in a warm daemon while the GPU generates the next candidate.
- **Evidence:** Stream cancel works by closing the connection (server README). The 20-60% saving is **[unverified]**.
- **Measure:** Decode tokens wasted on failed attempts, and the time spent in the check step.

**16. Pick tests from the edit, and keep scratch space on fast storage.** Tonight.
- **Idea:**
  - Use pytest-testmon in the inner loop (https://testmon.org/) and run the full suite only at the final gate.
  - Put worktrees and temp dirs on NVMe or a Dev Drive, and add Defender exclusions for model and work dirs (https://learn.microsoft.com/en-us/windows/dev-drive/).
- **Gain figures are vendor claims:** 5-50x and up to 30% **[unverified]**.
- **Measure:** Wall time of the check step.

**17. Persist KV slots across restarts.** Tonight.
- **Idea:** `--slot-save-path` plus `POST /slots/{id}?action=save|restore` for the fixed harness prefix.
- **Evidence:** PR #6341 **[confirmed]**.
- **Caveat:** A restore only works with the same model, context size and KV type, and it still reads hundreds of MB from disk.
- **Measure:** TTFT on a cold restore vs re-prefill.

**18. Use a static n-gram cache built from your repo.** Tonight.
- **Idea:** Add the repo-built cache (`llama-lookup -lcs`) to your existing n-gram speculation.
- **Source:** https://github.com/ggml-org/llama.cpp/tree/master/examples/lookup **[unverified]**: check that the server path supports it before building on it.
- **Measure:** Draft acceptance rate and tok/s on edit tasks.

**19. Build a machine-profile picker so the setup works on any machine.** Tonight on this laptop; the point is other machines.
- **Idea:** Run `llama-bench` sweeps over quant, `-ngl` / `--n-cpu-moe`, KV type, threads and context, and turn them into a table keyed on VRAM and RAM. Later, add bandit tuning from logged runs.
- **Measure:** On a second machine, tok/s and tasks passed with picker defaults vs hand-tuned settings.

**20. Thermal and power awareness.** Tonight.
- **Idea:** Poll NVML for throttle reasons. When throttling or on battery, drop a tier or reduce `-np`.
- **Source:** https://docs.nvidia.com/deploy/nvml-api/group__nvmlClocksEventReasons.html **[confirmed]**.
- **Measure:** tok/s over a 30-minute eval run, at the start vs the end.

## Tier 3: needs downloads or high effort

**21. Strict before/after test of abliteration against the stock base.** Needs download.
- **Idea:** Get the stock Qwen3 8B and 14B at the same quant, then run the same eval and KL tests as in items 1 and 8.

**22. Draft model for speculative decoding.** Needs download.
- **Idea:** Qwen3 0.6B or 1.7B drafting for the 8B or 14B worker. The abliterated finetunes keep the Qwen3 tokenizer, so the vocab matches. Qwen2.5-Coder only works as a draft if its vocab matches.
- **Evidence:** Draft support is in PR #10455 **[confirmed]**. The 1.5-2.5x figure is **[unverified]** on 6 GB, where the draft model and its KV compete for VRAM.
- **Measure:** tok/s and acceptance rate, with tasks passed unchanged.

**23. imatrix or dynamic quants.** Needs download.
- **Idea:** Unsloth Dynamic 2.0 or bartowski imatrix quants at Q4_K_M or higher for coders. Don't go below Q4 on dense models of 8B or less.
- **Evidence:**
  - arXiv 2410.14766: 2-bit loses the most syntax-correct code. https://arxiv.org/pdf/2410.14766
  - arXiv 2303.05378: small pass@1 loss for quantized code models. https://arxiv.org/pdf/2303.05378
  - Unsloth's own claims are vendor numbers. https://docs.unsloth.ai/basics/unsloth-dynamic-2.0-ggufs
  - Overall: **[partly confirmed]**.
- **Measure:** Tasks passed and KL against the current quant.

**24. Aider-style repo map alongside FTS5.** Medium effort, no download.
- **Idea:** Tree-sitter tags ranked by PageRank on the reference graph, kept within a token budget.
- **Source:** https://aider.chat/2023/10/22/repomap.html **[unverified gain]**.
- **Measure:** First-try edit success and context tokens.

**25. Cache tool results by content hash.** Medium effort, no download.
- **Idea:** Key file reads, greps, lint and test results on file hashes plus the command, shared across steps and runs.
- **Measure:** Count of repeated tool calls skipped.

**26. Self-distillation with LoRA from verified traces.** High effort, needs download.
- **Idea:** Train small LoRAs for the router and worker on steps that passed the code checks, using Unsloth QLoRA on the 4B.
- **Evidence:** STaR: https://arxiv.org/abs/2203.14465 That this fits in 6 GB comes from Unsloth's docs **[unverified]**.
- **Measure:** Tasks passed on held-out flows, and format errors.

## Suggested order for tonight

1. **Samplers and templates (item 3) and per-step thinking (item 7).** These change what every later test measures, so set them first.
2. **Rank the installed models (items 1 and 10).** This is the quality baseline per tier.
3. **MoE offload sweep with thread pinning (items 2 and 6), then KV q8_0 (item 4).** These set the speed baseline.
4. **Best-of-N with `-np` (item 11).** This is the largest likely gain in tasks passed that needs no new model.

All four work on installed models. Log every run with model, quant, flags, tasks passed, tok/s and TTFT, so the results also feed the machine-profile picker (item 19).