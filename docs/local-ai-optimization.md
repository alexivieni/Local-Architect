# Local AI optimization: what the notes recommend, and where it lives in Architect

The industry checklist for a local-Ollama harness, item by item, with the honest status here. "Measured" means `tune`/`livetest` can show it on your machine.

| Recommendation | In Architect | Where |
|---|---|---|
| One model per role, sized to the job | yes: 23 roles, per-role model, fallback chains, tier profiles | `arch_roles.py`, `arch_profiles.py`, hub Team > Roles |
| Quant by VRAM, not habit | advisory: a higher quant is suggested only when it fits with room for the KV cache (estimates, labelled) | `optimize` (`arch_modelopt.quant_advice`) |
| KV cache q8_0 + flash attention when context is tight | advised with the exact env lines, never set for you | `optimize` server plan |
| `num_ctx` per role / per model | per model (tune measures the window that fits), overridable per role | `tune`, `arch_params.OVERRIDE_FIELDS` |
| Retrieval over stuffing | TF-IDF file ranking, full text for the best files, **relevant functions** (AST) for the rest, signatures for the tail | `arch_core.context`, `rank_files`, `focus` |
| Hybrid/BM25 + vector + rerank | **yes (v11)**: a function-level vector index (Ollama `/api/embed`, a 45-670 MB embedding model asked with `keep_alive 0`), fused with TF-IDF by reciprocal rank fusion; the functions the vectors found are shown in full. Incremental by content hash; off (and says what to pull) without an embedding model. `index --query` shows both rankings side by side | `arch_index.py`, `index` command, Health > "What the prompt shows" |
| Prompt-prefix (KV) caching | **yes (v11)**: every retry prompt keeps its stable part (instructions, project context) first and the per-try part (feedback, failure text) last, and the few-shot example sits at the end of the user turn, so the server reuses the cached prefill on a retry instead of re-reading the context. Detected per call (`prefix_reused_est` in the trace), reported per run, kept out of the chars-per-token calibration | `arch_flows*` templates, `arch_core.call`, "Run economics" in the run report |
| Minimise model swaps on one small GPU | **yes (v11)**: swaps and their measured loading seconds are counted per run; the build flow judges all sprints at the end when the judge is a different model on a <10 GB card (report-only step, judged from the evidence saved at each sprint, one swap instead of two per sprint); the fit sheet prices the team's swaps | `arch_chain.py`, `behavior.swap_aware`, `fit` command |
| VRAM budget per model (weights + KV at the real window) | **yes (v11)**: the fit sheet lists weights, f16 and q8_0 KV at each model's own context, on-GPU share, what is resident, and the fix per model | `fit`, Health > "Does the team fit?" |
| Structured output with a JSON schema | yes: plan, judge, router, template pick; validated again in code | `arch_schemas.py`, `Engine.ask_json` |
| Temperature per role, repeat penalty for coders | yes: role sampling + family sampling from vendor cards | `arch_roles`, `optimize` |
| Stop sequences | not used: replies carry several FILE/EDIT blocks, so a stop string would cut them; `num_predict` is clamped to the window instead | `arch_tune.clamp_num_predict` |
| Few-shot from your own successful runs | **yes (new)**: an accepted past reply for a similar request is shown to the role as a format reference; `behavior.learn_from_runs` | `arch_ledger.example_for` |
| Async orchestration | no: calls are sequential by design (one GPU, one loaded model); offload to other machines is per role. What is done instead is swap-aware ordering (above) | `arch_hosts.py`, `arch_chain.py` |
| Idempotent tasks, checkpoints, resume | yes: `run.json` checkpoints, `resume` skips passed sprints | `arch_resume.py` |
| Retry with escalation, not the same model five times | yes: stall detection, escalation to a larger model you own and tuned | `arch_progress.py` |
| One tool dispatcher with timeouts and output caps | yes: tests run offline under the network guard with capped output | `arch_core.run_tests`, `netguard.py` |
| Every call logged (role, model, tokens, latency, success) | yes: `trace.jsonl` per run; per model/role in the ledger with p50/p95 and cost per accepted call | `arch_ledger.roles_overview`, hub Health |
| A fixed eval set | yes: Scoreboard (24 categories) + `livetest` (6 real tasks judged by code) | `arch_livetest.py` |
| Cost per successful task, not tok/s | yes: seconds per accepted call, per role | Health > Run history |
| `OLLAMA_MAX_LOADED_MODELS`, `NUM_PARALLEL`, keep-alive | advised per machine; keep-alive is set per request; models outside the team are unloaded before a run | `optimize`, `arch_ollama.prepare` |
| Models on NVMe, pinned versions | advice only; `catalog --verify` checks tags against the registry | `arch_catalog.py` |

Left out on purpose: a cloud model as the planner/reviewer (Architect is local-only; a bigger model on another machine of yours is the equivalent, see `hosts`),
and "no gates unless asked": the gates are what make a small local model's output trustworthy.
