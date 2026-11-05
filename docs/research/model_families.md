# Model families: what each line is good at (2026-11-05)

Data: `data/model_families.json` (read by `arch_modelknow.py`). Tiers: [M] measured here > [B] vendor card/benchmark > [A] community > [E] estimate.
Priors only ORDER screening and break ties in `arch_select`; measured C (seconds per passed task) still decides every suggestion.

## Key facts by line
- **Hermes 2 Pro / 3 / 4 (NousResearch)** [B]: ChatML even on Llama bases; tools listed in `<tools>` in the system prompt, calls as
  `<tool_call>{"name":..,"arguments":..}</tool_call>`, results in `<tool_response>` (origin of vLLM's `hermes` parser). Trained JSON mode.
  Neutral, system-prompt-steerable alignment. Hermes 4 adds optional `<think>`. Code tracks the base (Llama 3.1 8B: basic). Priors: route, docs, security, review.
- **Qwen3** [B]: hybrid thinking, `/think` `/no_think`; Hermes-style tool calls; never greedy in thinking mode (T 0.6, top_p 0.95, top_k 20; no-think T 0.7, top_p 0.8, presence 1.5).
- **Qwen3.5 / 3.6 / 3.8**: hybrid DeltaNet; 248k vocab (not draft-compatible with Qwen3's 151k); thinking on by default; qwen3_coder tool parser.
  Qwen3.6-35B-A3B SWE-V 73.4 [B]. Small "3.8" community builds have unverified provenance [E].
- **Qwen2.5-Coder / Qwen3-Coder**: code-specialised; Qwen3-Coder is non-thinking with its own XML function format [B].
- **DeepSeek**: R1 distills always think, no system prompt, weak tools [B]; Coder-V2-Lite dated FIM coder [B].
- **Mistral / Devstral / Codestral**: `[TOOL_CALLS]` native JSON; Devstral Small 2 SWE-V ~68 but 24B dense is slow here [B].
- **Gemma 2/3/4**: no system role on 2/3 (folded into the user turn); T 1.0 top_k 64; E-models weak at tools [A].
- **Granite 3/4**: enterprise tool calling (BFCL v3 57.7-64.7 on 4.0) [B]; cheap router.
- **GLM-4.x**: GLM-4.7-Flash SWE-V 59.2, min_p 0.01, fixed GGUF needed [B]/[A]. **gpt-oss**: harmony format, reasoning effort in system, strict refusals [B].
- **Phi-3/4**, **Llama 3.x**, **Nemotron** (thinking toggled in the system prompt), **OpenHermes** (2023 Mistral tune), **Dolphin** (uncensored by data), **Bonsai** (very low bit).
- **Abliterated/uncensored is a modifier**, not a family [A]: a few benchmark points lost, possible looping, can disturb JSON/tool output; test tool calls after it.

## Sources
Vendor cards: huggingface.co/NousResearch/Hermes-3-Llama-3.1-8B, NousResearch/Hermes-2-Pro-Llama-3-8B, NousResearch/Hermes-4-14B,
github.com/NousResearch/Hermes-Function-Calling, Qwen/Qwen3-8B, qwen.readthedocs.io (function calling), Qwen/Qwen3.6-35B-A3B, Qwen/Qwen3.5-9B,
Qwen/Qwen3-Coder-30B-A3B-Instruct, arxiv 2409.12186 (Qwen2.5-Coder), deepseek-ai/DeepSeek-R1-Distill-Qwen-7B, DeepSeek-Coder-V2-Lite-Instruct,
mistralai/Devstral-Small-2507, docs.mistral.ai function calling, google/gemma-3-4b-it, microsoft/phi-4, ibm-granite/granite-4.0-h-small,
zai-org/GLM-4.5, openai/gpt-oss-20b, nvidia/NVIDIA-Nemotron-Nano-9B-v2, cognitivecomputations/Dolphin3.0-Llama3.1-8B,
huggingface.co/blog/mlabonne/abliteration. Fetched today: see model_landscape.md section 5; the rest are recalled vendor cards, not re-fetched this session.
