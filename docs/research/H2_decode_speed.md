# H2: does decoding speed follow the memory-hierarchy bound (Eq. 6)?

First reading, 2026-11-05, RTX 4050 6 GB laptop. Inputs measured on this machine: GPU 158.2 GB/s and RAM 19.5 GB/s
(`hub_data/bandwidth.json`), model sizes from the GGUF headers (`hub_data/passports/`), and each model's mean generation speed
over all its local calls (`hub_data/model_stats.json`, a running average across conditions). For a mixture-of-experts model the
bytes read per token are scaled by active / total parameters.

The bound is t_tok ≈ Σ D_τ / B_τ (Eq. 6), shown here for the two extremes: every byte on the GPU, every byte in RAM.

| Model | File (GB) | Read per token (GB) | Eq. 6 bound, all on GPU (tok/s) | Eq. 6 bound, all in RAM (tok/s) | Measured mean (tok/s) | Calls |
|---|---|---|---|---|---|---|
| huihui_ai/qwen3-abliterated:4b | 2.33 | 2.33 | 67.9 | 8.4 | 138.3 | 823 |
| qwen3:4b | 2.33 | 2.33 | 67.9 | 8.4 | 72.91 | 74 |
| granite3.3:8b | 4.6 | 4.6 | 34.4 | 4.2 | 70.274 | 126 |
| qwen3:8b | 4.87 | 4.87 | 32.5 | 4.0 | 55.477 | 132 |
| eriktonon/granite4.2-uncensored:8b | 5.35 | 5.35 | 29.6 | 3.6 | 35.205 | 75 |
| eriktonon/qwen3.6-moe-uncensored:iq3 | 13.49 | 1.48 | 107.0 | 13.2 | 19.03 | 110 |
| qwen3:1.7b | 1.27 | 1.27 | 124.6 | 15.4 | 15.113 | 22 |
| huihui_ai/qwen3-abliterated:8b | 4.68 | 4.68 | 33.8 | 4.2 | 7.959 | 213 |
| richardyoung/qwen3-14b-abliterated:q4_K_M | 8.38 | 8.38 | 18.9 | 2.3 | 3.936 | 118 |

## Reading

- **The bound holds where nothing else is going on.** The 1.7B router runs on the CPU by design: 15.1 tokens/s measured against a
  15.4 bound from RAM bandwidth alone.
- **Partial offload lands between the bounds, as Eq. 7 predicts.** The 35B mixture-of-experts model (about 3.8B active, experts in
  RAM) measured 19.0, between 13.2 and 107. The 14B (8.4 GB, partly on the card) measured 3.9, between 2.3 and 18.9.
- **The abliterated 8B ran mostly from RAM.** It measured 7.96 against 33.8 on the GPU and 4.2 in RAM, while base qwen3:8b, the same
  size, measured 55.5. On 2026-11-05 the worker server was found with 9 of 36 layers on the card: Ollama had loaded the heavy MoE
  partly onto the GPU for the planner roles. Eq. 7 with g = 9/36 gives about 5.3 tokens/s. Every timing of this model so far
  therefore measures its placement, not the model; `server.ollama_gpu = auto` (same day) keeps Ollama's models off the card.
- **Speculative decoding beats the per-pass bound.** The 4B and 8B models fully on the card measured above their GPU bound (the
  abliterated 4B: 138 against 68). Eq. 6 bounds forward passes; n-gram speculation (`--spec-type ngram-cache`) accepts several
  tokens per pass on code edits, which repeat their input. The bound should be read as per pass, times the mean accepted length.

## Threats to validity

The speeds are running averages over many calls under different conditions (other models loaded, prompt lengths, speculation
on or off). A clean test fixes the placement, the window and the speculation setting per run, and measures the accepted length.
That run is queued after the H1 evaluation.
