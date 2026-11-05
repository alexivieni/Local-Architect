# Local coding-model landscape and pipeline map (research date 2026-11-05)

Hardware: RTX 4050 6 GB (~4.7 GB free), 32 GB RAM, Ollama + llama.cpp.
No downloads were done; all of this is desk research.

## Evidence tiers
- **[B]** vendor model card / official table (self-reported, own scaffold; not independent)
- **[B-3p]** third-party aggregator or secondary blog copying a model card
- **[A]** community anecdote (Reddit/blog, different GPU)
- **[E]** my estimate or background knowledge, not re-verified this session

Search found little independent benchmark data. There is no Aider-polyglot or BigCodeBench data for the 2026 Qwen/Gemma models in anything I could fetch.
Treat every SWE-bench number as "vendor scaffold, vendor settings".

---
## 1. Headline findings

1. **Qwen3.6-35B-A3B is the current community default for local agentic coding.** [B] SWE-bench Verified 73.4, Multilingual 67.2, Pro 49.5, Terminal-Bench 2.0 51.5, LiveCodeBench v6 80.4, NL2Repo 29.4.
   It beats its predecessor Qwen3.5-35B-A3B (SWE-V 70.0, TB2 40.5, LCB6 74.6) and Gemma4-26B-A4B (SWE-V 17.4).
   Source: https://huggingface.co/Qwen/Qwen3.6-35B-A3B
2. Architecture: 40 layers, 10 blocks of (3x Gated DeltaNet-MoE + 1x Gated Attention-MoE), 256 experts, 8 routed + 1 shared, only 2 KV heads (tiny KV cache), 262k ctx, MTP head included, thinking ON by default.
   **Your installed `eriktonon/qwen3.6-moe-uncensored` (qwen35moe, 34.7B, 256 exp / 8 active, IQ3_XS) is this model family.** It is the strongest coder you have by a wide margin. [B] for base model; the uncensored finetune is unbenchmarked [E: abliteration usually costs a few points].
3. **Qwen3.5 small dense line (0.8B/2B/4B/9B, released 2026-03-02) is the worker/draft family.** Same 248k-vocab tokenizer, hybrid DeltaNet + attention, 262k ctx, MTP, thinking default on.
   Qwen3.5-9B LiveCodeBench v6 = 65.6, 4B = 55.8 (GPT-OSS-20B 74.6 on the same card). No SWE-bench is published for the small models. [B] https://huggingface.co/Qwen/Qwen3.5-9B
4. **Qwen3.8** (GitHub QwenLM/Qwen3.8): only **Qwen3.8-2.4T-A95B (2026-08-12) and Qwen3.8-27B (2026-08-14)** are official, built on the Qwen3.5 architecture. [B] https://github.com/QwenLM/Qwen3.8
   I found no official 9B/4B Qwen3.8. Your `eriktonon/qwen3.8-uncensored` (qwen35 arch, 8.95B) is therefore probably a community distill/finetune or relabel of a Qwen3.5-9B-class model. **Treat its coding strength as unverified [E] and test it before trusting it.**
5. **Speculative decoding on MoE targets is mostly a loss with a classic draft model.** [A] hackmd RTX 3090 test: Qwen3.5-0.8B draft, ngram-cache and ngram-mod all gave -3% to -12% on 35B-A3B (but +15-45% on the 122B-A10B). Source: https://hackmd.io/@thc1006/SJly6IE6Wx.md
   A 6 GB-laptop poster says MTP/spec is not useful once experts sit in system RAM. The one reported win is **built-in MTP** (`--spec-type draft-mtp`, llama.cpp PR #22673, Unsloth MTP GGUFs, "up to 1.8x") [A]. It needs an MTP-bearing GGUF; check whether your eriktonon GGUF kept the MTP tensors [E: probably not].
   Draft models do help dense targets (qwen3.5-9b, qwen3-14b, 27b).
6. **6 GB laptop reality for 35B-A3B** [A]: one 6 GB-laptop user got ~17 t/s at huge ctx with `--n-cpu-moe 36-41`; `--no-mmap` took 10 -> 13.5 t/s in another test; another claims ~30 t/s with `--n-cpu-moe 99` on a faster setup. None is a 4050. Expect roughly 8-18 t/s decode for you [E]. Source: https://dev.to/jovan_chan_9500711396d4e6/qwen36-35b-a3b-local-setup-2026-ollama-and-24gb-vram-3joj plus Reddit-mirror threads.

---
## 2. Model catalogue

Sizes: GGUF file sizes in GB from Hugging Face repo listings fetched this session unless marked [E].

### 2a. Qwen family (core)

| Model | Params (tot/act) | Arch | Code? | Thinking | Tools | Ctx | Evidence |
|---|---|---|---|---|---|---|---|
| Qwen3.6-35B-A3B | 35B/3B (256 exp, 8+1) | qwen35moe hybrid DeltaNet+attn, MTP | general, strongest agentic coder in class | default ON; `enable_thinking:false` for off; no /think switch | "excels"; parser `qwen3_coder`; nested objects fixed | 262k (YaRN 1M) | [B] SWE-V 73.4, LCB6 80.4, TB2 51.5 |
| Qwen3.6-27B (dense) | 27B | same family | same | same | same | 262k | Ollama `qwen3.6:27b` 17-18 GB; too big to be fast here (est <3 t/s) [E] |
| Qwen3.5-35B-A3B | 35B/3B | qwen35moe | general | on | good | 262k | [B] SWE-V 70.0, LCB6 74.6; Ollama `qwen3.5:35b` 22 GB |
| Qwen3.5-9B | 9B dense | qwen35 hybrid | general | on by default | decent [E] | 262k | [B] LCB6 65.6; Ollama `qwen3.5:9b` 6.6-7.6 GB |
| Qwen3.5-4B | 4B | qwen35 | general | on | okay [E] | 262k | [B] LCB6 55.8; Ollama `qwen3.5:4b` 3.3-4.0 GB |
| Qwen3.5-2B / 0.8B | 2B / 0.8B | qwen35 | general | on | weak | 262k | Artificial Analysis index 16 / 9 [B-3p]; Ollama 2.7-3.1 / 1.2-1.3 GB |
| Qwen3-Coder-30B-A3B-Instruct | 30.5B/3.3B (128 exp, 8 act, 48 L, 4 KV heads) | qwen3moe classic attention | **code-specialised** | non-thinking only | "excels", dedicated function-call format | 262k | no numbers on card. Ollama `qwen3-coder:30b` 19 GB (Q4_K_M) |
| Qwen3-Coder-Next | 80B/3B | Qwen3-Next base | code | non-thinking | strong | 256k | [B-3p] SWE-V 70.6 (one source 74.2). Smallest usable quants UD-IQ1_S 21.5 GB, UD-IQ2_XXS 23.3 GB: **too big/slow for 32 GB RAM + 6 GB** in practice |
| Qwen3.8-27B / 2.4T-A95B | 27B dense / 2.4T | Qwen3.5 arch | general | n/a | n/a | n/a | official per GitHub; no small variant found |
| Qwen2.5-Coder 1.5/3/7/14B | dense | qwen2 | code | non-thinking | weak-moderate | 32k (128k YaRN) | [E] background: 7B HumanEval ~88; obsolete for agentic work, still fine for FIM/autocomplete. Not re-verified |
| Qwen3 0.6/1.7/4/8/14B (installed) | dense | qwen3 | general | hybrid think/no-think | OK | 32-40k | superseded by Qwen3.5 |

### 2b. GGUF quants (unsloth repos, sizes in GB)

| Model | IQ2/Q2 | IQ3 / Q3 | IQ4_XS | Q4_K_M | Q5_K_M | Q6_K | Q8_0 |
|---|---|---|---|---|---|---|---|
| Qwen3.6-35B-A3B (`unsloth/Qwen3.6-35B-A3B-GGUF`) | IQ2_M 11.5, Q2_K_XL 12.3 | IQ3_XXS 13.2, IQ3_S 13.7, Q3_K_M 16.6 | 17.7 | 22.1 (UD) | 26.5 | 29.3 | 36.9 |
| Qwen3-Coder-30B-A3B (`unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF`) | IQ2_M 10.8, Q2_K 11.3 | IQ3_XXS 12.8, Q3_K_M 14.7 | 16.4 | 18.6 | 21.7 | 25.1 | 32.5 |
| Qwen3.5-9B (`unsloth/Qwen3.5-9B-GGUF`) | IQ2_M 3.65 | IQ3_XXS 4.02, Q3_K_M 4.67 | 5.17 | 5.68 | 6.58 | 7.46 | 9.53 |
| Qwen3-Coder-Next (`unsloth/Qwen3-Coder-Next-GGUF`) | IQ2_XXS 23.3 | IQ3_XXS 28.5 | 38.4 (UD) | 48.5 | 56.8 | 65.6 | 84.8 |
| gpt-oss-20b (`unsloth/gpt-oss-20b-GGUF`) | Q2_K 11.5 | Q3_K_M 11.5 | n/a | 11.6 | 11.7 | 12.0 | 12.1 (native MXFP4, all ~11.5-12) |

Fit on 6 GB (4.7 GB free):
- **Nothing in the 30-35B MoE class fits fully on GPU.** Use `--n-cpu-moe N` (or `-ot "exps=CPU"`), with attention/shared/KV on GPU. The file must fit in RAM: IQ4_XS 17.7 GB (Qwen3.6) leaves ~12 GB for OS + KV on 32 GB. Fine.
- Qwen3.5-9B Q4_K_M 5.68 GB does **not** fit fully (4.7 GB budget + KV); IQ3_XXS 4.02 or Q3_K_M 4.67 only with small ctx. Partial offload (`-ngl ~24-28`) is realistic [E]. Hybrid DeltaNet keeps KV tiny (only 8 of 32 layers have KV).
- Qwen3.5-4B Q4_K_M (~2.6 GB [E]), 2B and 0.8B fit fully with large ctx.

### 2c. Uncensored / abliterated publishers (relevant)

| Source | Model | Ollama / HF tag | Notes |
|---|---|---|---|
| huihui-ai | Qwen3.5-35B-A3B-abliterated | Ollama `huihui_ai/qwen3.5-abliterated:35b` (needs Ollama >= 0.17.5); HF `huihui-ai/Huihui-Qwen3.5-35B-A3B-abliterated`; 19 community quants | seen on model card |
| huihui-ai | Qwen3.5-27B-abliterated | HF `huihui-ai/Huihui-Qwen3.5-27B-abliterated` | seen in search |
| huihui-ai | Qwen3-Coder-Next-abliterated | Ollama `huihui_ai/qwen3-coder-next-abliterated`; GGUF `bartowski/huihui-ai_Qwen3-Coder-Next-abliterated-GGUF` | too big for this box |
| huihui-ai | Qwen3.8-27B-abliterated | Ollama `huihui_ai/Qwen3.8-abliterated` | dense 27B, too slow here |
| huihui-ai | Qwen3.6-35B-A3B abliterated | not confirmed in my search | **unverified**: check ollama.com/huihui_ai |
| eriktonon | qwen3.6-moe-uncensored | installed | |
| richardyoung, mlabonne | no 2026 coder-relevant releases surfaced | | not found |

Method note from the huihui card: "crude, proof-of-concept" abliteration. Expect some accuracy loss and occasional looping; test tool-calling after abliteration since refusal-direction removal can disturb structured output [E].

### 2d. Non-Qwen candidates

| Model | Params | Arch | Code / think | Evidence | Fits 6 GB? | Verdict here |
|---|---|---|---|---|---|---|
| GLM-4.7-Flash | 30B / ~3B MoE | GLM MoE | general+agentic; thinking | [B] SWE-V 59.2 vs Qwen3-30B-A3B-Thinking 22.0, GPT-OSS-20B 34.0. https://huggingface.co/AaryanK/GLM-4.7-Flash-GGUF . Needs a re-downloaded fixed GGUF (llama.cpp looping bug) and `--min-p 0.01` | expert offload, ~17 GB Q4 [E] | Good second-opinion reviewer; below Qwen3.6 (73.4). No abliterated version found |
| gpt-oss-20b | 21B / 3.6B MoE | MXFP4 native, harmony format | general; reasoning low/med/high | [B] LCB6 74.6 (Qwen3.5 card); SWE-V 34.0 (GLM card). 11.6 GB all quants. Apache 2.0 | needs `--n-cpu-moe`; file fits RAM easily | Fast "plan/review" thinker; heavily censored; needs harmony template |
| Devstral Small 2 (2512) | 24B dense | Mistral | code-agent specialist | [B] SWE-V 68.0 (one site 65.8). Ollama `devstral-small-2:24b-instruct-2512-q4_K_M`, ~14 GB. 256k ctx | no; dense 24B mostly on CPU, est 2-4 t/s [E] | Too slow here. Devstral Small 1.1 (SWE-V 53.6) same story |
| Gemma 4 (E2B / E4B / 26B-A4B / 31B) | 2/4 effective, 26B/4B, 31B | gemma4, Apache 2.0 | general | [B-3p] LCB6: E2B 44, E4B 52, 26B-A4B 77.1, 31B 80.0. SWE-V 26B-A4B 17.4, 31B 52.0 (Qwen card). E4B passed 2/6 agent-readiness scenarios [A] https://dev.to/vystartasv/google-said-it-had-native-function-calling-i-tested-it-54i7 | E4B yes | Installed gemma4 e4b (MTP): OK for docs/summaries, **not** for code/tools |
| IBM Granite 4.0 H-Tiny / H-Small | 7B/1B, 32B/9B hybrid Mamba MoE | granite4 | general enterprise; non-thinking | [B] HumanEval 83 / 88, HumanEval+ 76 / 83, BigCodeBench 41.1 / 46.2, BFCL v3 57.7 / 64.7. https://huggingface.co/ibm-granite/granite-4.0-h-small | H-Tiny yes | Cheap routing/docs; not an agentic coder |
| Granite 3.3 2B/8B, Llama 3.2 3B | dense | | general | older | yes | Fallbacks only |
| Phi-4 / Phi-4-mini | 14B / 3.8B | | reasoning/code ok | [E] | 3.8B yes | Not re-verified; below Qwen3.5-4B for agentic use [E] |
| DeepSeek-Coder-V2-Lite | 16B / 2.4B MoE | deepseek2 | code, FIM | [E] dated 2024 | offload | Obsolete vs Qwen3.x |
| DeepSeek-R1 distills (Qwen 1.5/7/14B) | | | reasoning | [E] | | Obsolete vs Qwen3.5 thinking |
| Codestral 22B, Mistral Small, Ministral | | | | [E] | dense | Not worth the speed cost here |
| OpenCoder, Seed-Coder 8B, StarCoder2 3/7/15B | | | code, FIM | [E] 2024-25 tier | 3B yes | Only for FIM autocomplete; no agentic ability |
| Qwen2.5-VL 3B, bge-m3 | | | | | | Keep (vision, embeddings) |

I found no 2026 releases or benchmarks for Phi, OpenCoder, Seed-Coder, StarCoder2 or Llama code tunes: marked [E], low relevance.

### 2e. Which installed models are actually code-strong
| Installed | Verdict | Tier |
|---|---|---|
| eriktonon/qwen3.6-moe-uncensored (IQ3_XS) | **Yes.** Best coder, agentic/tool capable; abliteration side effects unknown | [B] base, [E] finetune |
| eriktonon/qwen3.8-uncensored 8.95B (qwen35) | Unknown provenance (no official Qwen3.8 9B). If Qwen3.5-9B-class: LCB6 ~65 | [E] |
| eriktonon/bonsai-2-uncensored 9b Q2_0 | Q2_0 on a 9B is heavily degraded; avoid for code | [E] |
| richardyoung/qwen3-14b-abliterated Q4_K_M | Decent older dense coder; ~9 GB, partial offload, slow | [E] |
| qwen3 8b/4b + huihui abliterated 4b/8b | Adequate small workers; weaker than Qwen3.5 equivalents | [E] |
| gemma4 e4b mtp | weak at tools/code | [A] |
| granite4.2 8b, granite3.3 | docs/route only | [B]/[E] |
| qwen3 0.6/1.7b | draft only for qwen3 targets (151k vocab, incompatible with qwen35/3.6 248k vocab) | [E] |

Tokenizer caveat: a Reddit report claims even Qwen3.5-0.8B -> 35B shows a "vocabs not compatible" warning while still running [A]; verify in the llama-server log.

---
## 3. Pipeline map for THIS machine

Assumption: one big resident worker (Qwen3.6-35B-A3B, experts in RAM) plus a small model. The 4.7 GB GPU budget means the big model's non-expert weights + KV take most of it [E], so swap small models in rather than co-residing.

| Job | Best installed | Best community candidate (exact tag) | Size | Quant | Why | Tier |
|---|---|---|---|---|---|---|
| **route** (classify intent, JSON out) | qwen3 1.7b or 4b (non-thinking) | `qwen3.5:2b` (thinking OFF) or Granite 4 H-Tiny | 2.7-3.1 GB | Ollama default | Tiny, fast, fits GPU; routing needs no deep reasoning | [E] choice; sizes [B-3p] |
| **plan** | eriktonon qwen3.6-moe (thinking ON) | `unsloth/Qwen3.6-35B-A3B-GGUF` UD-IQ4_XS; `gpt-oss-20b` (medium effort) as fast second opinion | 17.7 / 11.6 GB | IQ4_XS | Best reasoning on card (GPQA 86, AIME26 92.7); thinking on, temp 1.0 | [B] |
| **code** | eriktonon qwen3.6-moe (thinking ON, temp 0.6, presence 0) | `unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ3_S` (13.7) for speed or `UD-IQ4_XS` (17.7) for quality; alt `qwen3-coder:30b` (19 GB) non-thinking for faster edits | 13.7-17.7 GB | IQ3_S/IQ4_XS | Official coding sampling set; SWE-V 73.4; `--n-cpu-moe 36..41`, `--no-mmap`, ctx 32-64k | [B] model; [A] flags |
| **test-writing** | same, thinking OFF | `qwen3-coder:30b` non-thinking, or unsloth Coder-30B IQ4_XS 16.4 GB | 16.4-19 GB | IQ4_XS | Pattern-heavy; non-thinking speed wins; no published Coder-30B numbers | [E] |
| **fix / debug** | eriktonon qwen3.6-moe thinking ON | same; use `preserve_thinking` across turns | | | Terminal-Bench 2.0 51.5 is the closest published proxy to fix loops | [B] |
| **review** | qwen3.6-moe (different sampling) | `AaryanK/GLM-4.7-Flash-GGUF:Q4_K_M` as an independent second reviewer (~17-18 GB [E]); fixed GGUF + `--min-p 0.01` | ~17 GB | Q4_K_M | Different family catches different bugs; SWE-V 59.2 [B] | [B] score, [E] fit |
| **security** (uncensored preferred) | eriktonon qwen3.6-moe-uncensored | `huihui_ai/qwen3.5-abliterated:35b` (Ollama, ~20 GB int4) as second uncensored voice; watch for a huihui Qwen3.6 abliteration | ~20 GB | int4 | Only abliterated 35B-A3B I could confirm | [B] base, [E] abliteration quality |
| **docs** | granite4.2 8b / qwen3.8-uncensored | `qwen3.5:4b` or `qwen3.5:9b` non-thinking | 3.3-7.6 GB | | Fluency not agentic skill; cheap on GPU | [E] |
| **draft** | qwen3 0.6b (only for qwen3 targets) | For qwen3.6 MoE: **built-in MTP** if the GGUF has it (`--spec-type draft-mtp`; reference Ollama `qwen3.6:35b-a3b-mtp-q4_K_M` 23 GB) or ngram only; skip draft model. For dense `qwen3.5:9b`: `qwen3.5:0.8b` (1.2 GB) or `qwen3.5:2b` | 1.2-3 GB | Q4/Q8 | Classic draft gave -3..-12% on 35B-A3B (RTX 3090) [A]; gains only on dense / larger-active targets | [A] |

### Suggested llama-server starting points [E, derived from [A] reports]
- Qwen3.6-35B-A3B IQ3_S/IQ4_XS: `-ngl 99 --n-cpu-moe 38 --no-mmap -c 32768 -fa on --jinja --temp 0.6 --top-p 0.95 --top-k 20 --min-p 0`; lower `--n-cpu-moe` until VRAM is ~4.5 GB. Tune per ctx.
- Quantised KV (`--cache-type-k q8_0`) only after a quality test; KV is already small (2 KV heads).
- The card advises >=128k context to preserve thinking quality; run 32-64k and test whether that hurts.

---
## 4. Gaps / not verified
- No independent Aider polyglot, BigCodeBench or HumanEval+ data for Qwen3.5/3.6 (only Granite 4 has those from its card).
- No benchmark for Qwen3-Coder-30B-A3B in fetched sources; aggregator pages disagree on whether it beats Qwen3.6 non-reasoning.
- Whether a huihui_ai Qwen3.6-35B-A3B abliteration exists; mlabonne/richardyoung 2026 releases.
- Ollama `qwen3.6:35b-a3b-coding` tags (23-24 GB) exist; unclear what differs (probably the sampling preset) [E].
- Whether the eriktonon GGUF retains MTP tensors; Qwen3.8 9B provenance.
- Real 4050 numbers: all speed claims come from other GPUs. Measure with `llama-bench --n-cpu-moe`.

## 5. Sources
- https://huggingface.co/Qwen/Qwen3.6-35B-A3B
- https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF
- https://huggingface.co/Qwen/Qwen3.5-9B , https://huggingface.co/unsloth/Qwen3.5-9B-GGUF
- https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct , https://huggingface.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF
- https://huggingface.co/unsloth/Qwen3-Coder-Next-GGUF , https://huggingface.co/unsloth/gpt-oss-20b-GGUF
- https://github.com/QwenLM/Qwen3.8
- https://ollama.com/library/qwen3.6/tags , https://ollama.com/library/qwen3-coder/tags , https://ollama.com/library/qwen3.5/tags
- https://huggingface.co/huihui-ai/Huihui-Qwen3.5-35B-A3B-abliterated , https://huggingface.co/huihui-ai/Huihui-Qwen3.8-27B-abliterated
- https://huggingface.co/AaryanK/GLM-4.7-Flash-GGUF
- https://huggingface.co/ibm-granite/granite-4.0-h-small
- https://hackmd.io/@thc1006/SJly6IE6Wx.md (spec decoding test)
- https://dev.to/jovan_chan_9500711396d4e6/qwen36-35b-a3b-local-setup-2026-ollama-and-24gb-vram-3joj
- https://dev.to/vystartasv/google-said-it-had-native-function-calling-i-tested-it-54i7
- https://dev.to/jovan_chan_9500711396d4e6/devstral-small-2-review-2026-68-swe-bench-on-rtx-4090-5abh
- https://artificialanalysis.ai/articles/qwen3-5-small-models
