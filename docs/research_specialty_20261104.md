# Architect specialty playbook: models, harnesses, flows, pages, experiments

*Dated 2026-11-04. Every claim either has a source inline or is marked **[unverified]**. Public benchmark numbers come from cloud or full-precision runs. They do not carry over to IQ3/Q4 quants on a 6 GB card, so anything below that is not measured on this machine is a starting guess for local evals to replace.*

---

## 0. Ground rules for routing

1. **Use measured numbers before leaderboard numbers.** The secondary roundups disagree on scores and on which Qwen3.6 variant is stronger. The often-quoted 73.4% SWE-bench Verified for Qwen3.6-35B-A3B was produced with Alibaba's internal scaffold, and benchlm lists the dense Qwen3.6-27B higher at 77.2% (https://benchlm.ai/blog/posts/best-llm-coding; https://dev.to/zyvop/the-best-open-source-llms-for-coding-right-now-june-2026-n10). Your installed copy is IQ3, so it will score lower.
2. **Every loop gates on an external signal: test, linter, executor, scanner or pixel diff.** When a model critiques itself with no outside signal, results can get worse (Huang et al., ICLR 2024, https://arxiv.org/abs/2310.01798). Self-repair gains are modest once you match the sampling cost, and they grow when the feedback is better (Olausson et al., https://arxiv.org/abs/2306.09896).
3. **Most of the gain comes in rounds 1–3.** After that, spend the budget on a fresh sample, not another repair (Self-Refine, https://arxiv.org/abs/2303.17651; Snell et al., https://arxiv.org/abs/2408.03314). Default `max_rounds: 3`, `no_progress_rounds: 2`.
4. **The reviewer comes from a different family than the generator.** LLM judges favor their own outputs (Panickssery et al., https://arxiv.org/abs/2404.13076). Qwen generates, and Granite or Gemma reviews.
5. **The interface around the model matters as much as model size.** Lint-on-edit and windowed file views were key ablation gains in SWE-agent (https://arxiv.org/abs/2405.15793).
6. **Plain pipelines beat agent chat on a budget.** Agentless (localize → repair → validate with reproduction tests) was competitive at low cost: 27.3% Lite at $0.34 in the original paper, 32% at $0.70 in v1.5 (https://arxiv.org/abs/2407.01489). Multi-agent systems often fail on role misalignment, and many barely beat best-of-N (Cemri et al., https://arxiv.org/abs/2503.13657).

**Hardware tiering (generalizes to any machine).** Classify the machine as cpu / gpu6 / gpu12 / gpu24+ / remote. On gpu6 (RTX 4050 6 GB, 32 GB RAM), the setup is:
- **Heavy:** 35B-A3B IQ3 with offload. About 3B parameters are active, so it is slow but workable. Plan for it, but don't assume it.
- **Worker:** Qwen3 8B or 14B.
- **Reviewer:** Granite 4.2 8B or Gemma4 e4b.
- **Draft/router:** qwen3 0.6B or 1.7B.

Model swaps on 6 GB VRAM cost real seconds. Batch the steps that use the same model, and measure the swap cost (experiment E7).

---

## 1. Specialty matrix

"Try first" means the installed model to benchmark first, not a proven winner. "Harness" means the open tool worth A/B-ing against Architect's native loop. The harness descriptions are **[unverified]** this pass and come from each project's documented positioning.

| Specialty | Model class (try first) | Reasoning | Helpful tools | Open harness to compare | Architect flow | Pass check |
|---|---|---|---|---|---|---|
| **Frontend / UI** | 8–14B coder for code (Qwen3 14B abliterated or qwen3 8B). Small VL model as critic only (Qwen2.5-VL 3B) | Off for the edit, on for the layout plan | Headless browser (Playwright), screenshots at 2 viewports, axe-core | Playwright-MCP for a11y-tree driving; v0/Lovable-style preview UX **[unverified]** | `design` → new `ui_polish` | Build passes + axe has 0 serious issues + SSIM ≥ threshold vs reference. VL verdict is advisory |
| **Backend / API** | Qwen3 14B (worker); 35B-A3B for multi-file | On for the plan, off for edits | Test runner, HTTP client, OpenAPI validator | Aider (diff edits, repo map) **[unverified]** | `feature` / new `tdd` | Frozen tests pass + type-check + contract test |
| **Full stack** | 35B-A3B plans, Qwen3 8B/14B edits (Aider architect/editor pattern, https://aider.chat/2024/09/26/architect.html, shown with cloud models only) | On for planner, off for editor | Browser + DB snapshot + test runner | OpenHands (sandboxed multi-step) **[unverified]** | `feature` → `ship` | Unit + one e2e smoke + reviewer cites a check |
| **SQL / data** | Granite 3.3 8B or Granite 4.2 8B vs qwen3 8B (measure) | Off. Use multiple candidates, not long thought | sqlite/duckdb sandbox, schema introspection, EXPLAIN | none needed. Execution-guided loop | new `sql` (under `ml`/`feature`) | Executes + result-set matches the expected one or the majority vote (BIRD scores by execution accuracy, https://bird-bench.github.io/) |
| **Testing** | Qwen3 8B with execution; heavy model only for test design | On for property design | pytest/vitest, Hypothesis/fast-check, mutmut/Stryker/cargo-mutants | Meta TestGen-LLM filter pattern (https://arxiv.org/abs/2402.09171) | `test` (+ mutation gate) | Tests build, pass 3× (flake check) and raise mutation score. Coverage alone is not enough |
| **Debugging / bugfix** | Qwen3 14B; 35B-A3B on a hunt | On | Repro runner, git bisect, stack traces | SWE-agent / Agentless pattern **[unverified as tools]** | `bugfix`, `hunt` | Repro test fails on HEAD and passes after the patch, with no regressions |
| **Refactor / types / migration** | 35B-A3B (cross-file) or Qwen3 14B | Off per batch | Snapshot tests, tsc/mypy/cargo check, codemods | Aider **[unverified]** | `refactor` | Characterization tests unchanged + type-check clean per batch |
| **Security / pentest** | Qwen3 14B abliterated or qwen3.8 uncensored (fewer refusals on offensive analysis; local only) | On | Semgrep, Bandit, gitleaks, npm/pip audit, ZAP (local targets only) | none. Scanners are the ground truth | `secure`, `pentest` | No new findings on rescan + tests still pass |
| **Config / CI-CD / IaC / network** | Granite 4.2 8B or qwen3 8B | Off | actionlint, hadolint, tflint/checkov, `docker build`, yamllint | Goose (light tool loop) **[unverified]** | `config`, `cicd`, `network` | Linters clean + dry-run/plan succeeds |
| **ML / math / numeric** | qwen3 8B or 14B in thinking mode | On | Sandboxed Python (numpy, sympy) | smolagents code-as-action **[unverified]** | `ml` | Sandboxed recompute matches within tolerance |
| **Docs** | Gemma4 e4b or Granite 3.3 8B | Off | doctest, rustdoc tests, link checker | none | new `docs` | Doc examples execute (https://docs.python.org/3/library/doctest.html; https://doc.rust-lang.org/rustdoc/write-documentation/documentation-tests.html) |
| **Accessibility** | Qwen3 8B + VL 3B critic | Off | axe-core, keyboard-nav script, contrast check | Playwright-MCP **[unverified]** | `design` sub-step | 0 serious/critical axe issues + tab order script passes |
| **Performance / optimize** | Qwen3 14B | On | Benchmark harness (hyperfine, pytest-benchmark), profiler | none | `optimize` | p50 better than baseline by more than the noise band (k runs) |
| **Exploration / Q&A over repo** | qwen3 4B + bge-m3 retrieval (https://arxiv.org/abs/2402.03216) | Off | Embedding index, ripgrep | Aider repo-map **[unverified]** | `explore` | Answers cite file:line that exists |
| **Routing / classification** | qwen3 1.7B or Granite 3.3 2B | Off | none | none | router tier | Agreement with a labeled set of 50 prompts |
| **Draft / speculative** | qwen3 0.6B (same tokenizer family as the worker) | Off | none | llama.cpp speculative decoding | draft tier | Acceptance rate and tok/s gain (measure) |
| **Vision tasks (screenshots, diagrams)** | Qwen2.5-VL 3B (https://arxiv.org/abs/2502.13923) | n/a | Screenshot capture | none | used inside UI flows | Agreement with human labels on the E5 set |

**Why the frontend row uses a VL critic and not a VL generator:** Design2Code found that self-revision on a rendered screenshot helped, but the gains were modest and uneven across models, clearest with GPT-4V (https://arxiv.org/abs/2403.03163). A 3B VL model judging UI differences reliably is a **hypothesis**, so code-checked metrics (SSIM, axe) gate the step and the VL model only explains.

**Optional big tiers:** DeepSeek V4 Pro, GLM-5, MiniMax M2.5 and Kimi K2.6 lead the open agentic leaderboards (https://benchlm.ai/blog/posts/best-llm-coding; https://www.faros.ai/blog/open-weight-models), but they need multi-GPU or API-scale hardware. Expose them as `remote` or `gpu24+` tiers. Their scores were not re-checked this pass.

---

## 2. Iterative flows per role

Shared defaults for every flow. The repair micro-loop follows the SWE-agent edit-with-lint pattern and the CORE proposer/ranker results (~59% of Python static-analysis issues resolved with GPT-class models, https://arxiv.org/abs/2309.12938, so expect less locally):

```yaml
defaults:
  budgets: {max_rounds: 3, no_progress_rounds: 2, max_wallclock: 20m, max_tokens: 60k}
  regression_guard: rollback_if_previously_passing_fails
  escalate_to: next_tier          # draft -> worker -> heavy -> remote(optional)
  lint_loop:                      # runs after every edit, every code role
    run: [format, lint, typecheck]
    feed_back: first_5_diagnostics
    fixer: draft_or_4b
    escalate_after: 2 stuck rounds
  reviewer: {family: different_from_generator, must_cite: check_or_line, cannot_override_failed_check: true}
```

### planner / architect: `plan`
```yaml
- planner(heavy, think): acceptance criteria + file list + risks
- check: every criterion is testable (has verb + observable); files exist or are marked new
- architect(heavy): interface sketch / ADR when >3 files touched
- gate: user approval in ask-mode; auto in autopilot only for non-destructive flows
stop: criteria accepted | 2 rewrites without new testable criteria
```

### evaluator + generator: `tdd` (new)
Supported by AlphaCodium (19% → 44% pass@5 on CodeContests with GPT-4, https://arxiv.org/abs/2401.08500) and Mathews & Nagappan on tests in the prompt (https://arxiv.org/abs/2402.13521).
```yaml
- evaluator(worker): write failing tests from criteria
- check: tests fail for the right reason (assertion error, not import/syntax)
- freeze: hash test files; generator edits to them = step fail
- generator(worker): implement
- run tests -> repair loop fed only failing assertion + trace
- reviewer(other family)
stop: all pass | no new test passing for 2 rounds -> escalate tier once -> stop
```

### evaluator: `test` (mutation gate)
Mutation feedback works in industry: Meta's ACH had 73% of generated tests accepted (https://arxiv.org/abs/2501.12862), and MuTAP feeds surviving mutants back into the prompt (https://arxiv.org/abs/2308.16557).
```yaml
- evaluator: tests for changed files (example + property tests for pure functions)
- filter (TestGen-LLM): builds -> passes 3x -> adds coverage or kills a mutant; else discard
- mutation run on changed files only, time-boxed (e.g. 5 min)
- feed top 5 surviving mutants -> evaluator adds tests
stop: mutation score delta < 2 pts for 2 rounds | budget
gate: mutation score >= previous; zero flaky tests
```

### debugger: `bugfix` / `hunt` (Agentless shape)
```yaml
- debugger: write repro test
- check: repro FAILS on HEAD (else stop: "cannot reproduce")
- localize: bge-m3 retrieval + stack trace + optional git bisect -> top 3 files
- generator: N=2 candidate patches (temperature spread)
- validate: repro passes + full suite no regressions; rank by checks passed, then diff size
stop: one candidate green | 3 rounds -> hand to heavy tier -> report
```

### generator_ui + accessibility_engineer: `ui_polish` (new)
```yaml
- generator_ui(worker 14B): code
- build + render (Playwright) at 375px and 1280px
- metrics: SSIM/pixel diff vs reference, axe-core
- critic(Qwen2.5-VL 3B): list top 3 visual differences (advisory text only)
- fix with metrics + critic notes
stop: axe 0 serious AND SSIM >= target | SSIM gain < 1% between rounds | 4 rounds
```

### generator_sql: `sql` (new)
Based on execution-guided self-debug (https://arxiv.org/abs/2304.05128), DIN-SQL (https://arxiv.org/abs/2304.11015) and CHASE-SQL's multi-candidate selection (https://arxiv.org/abs/2410.01943).
```yaml
- schema_link: introspect tables/cols/FKs, keep relevant subset
- generator_sql: 3 candidates
- execute on sandbox snapshot with LIMIT + EXPLAIN
- reject: errors, empty when non-empty expected, row explosion (> 10x estimate)
- vote: majority by result set; error text -> repair (max 2)
- optional: invariant assertion queries (sums, counts, no nulls in keys)
stop: winner agrees with >=2 candidates or assertions pass
```

### type_engineer / migration_engineer: `refactor`
LLM refactors often change behavior without verification (https://arxiv.org/abs/2411.04444; RefactorBench https://arxiv.org/abs/2503.07832).
```yaml
- capture characterization/snapshot tests on HEAD
- split change into batches (<= 5 files)
- per batch: edit -> typecheck -> snapshot diff
- mismatch -> revert batch, retry once with diff in prompt, else skip batch & report
stop: all batches applied | 2 consecutive reverted batches
```

### reliability_engineer + reviewer: `secure` / `pentest`
Pearce et al. found roughly 40% of Copilot outputs vulnerable (https://arxiv.org/abs/2108.09293). Fixes can introduce new findings, so a rescan is mandatory.
```yaml
- baseline scan: semgrep, bandit/eslint-security, gitleaks, dep audit -> finding IDs to ledger
- fix targeted IDs only
- rescan: fail if ANY new ID appears
- tests must still pass
- pentest: only against localhost/declared targets; destructive steps need approval
stop: targeted IDs closed | 3 rounds
```

### dependency_engineer: `deps` (new)
```yaml
- audit + outdated list -> one package per batch
- bump -> install -> build -> tests
- fail -> revert that package, record reason
stop: list exhausted | wallclock
```

### docs_writer: `docs` (new)
```yaml
- write/update docs from diff
- run doctest / cargo test --doc / ts example compile
- link check
stop: all examples execute | 3 rounds; failing examples are removed, never faked
```

### reliability_engineer: `optimize`
```yaml
- baseline benchmark k=10 runs -> p50 + noise band (IQR)
- profile -> top hotspot
- generator: change -> tests -> benchmark
gate: p50 improves beyond noise band; else revert
stop: 2 reverted attempts | target hit
```

### `parallel_attempts` (meta-flow, sized to hardware)
This is the Cursor 2.0 worktree pattern (up to 8 agents in parallel, https://cursor.com/changelog/2-0). On gpu6, run the attempts one after another with different models or temperatures. Rank them by checks passed, then by diff size. On gpu24+ machines, run them in parallel.

---

## 3. Pages for the four places, ranked by value

1. **Models › Role × Model Matrix** (highest). Rows are roles or specialties and columns are installed models. Each cell shows the measured pass rate, tok/s, VRAM fit and the evidence date, with a "Re-benchmark" button. This page is the routing source of truth, and it generalizes to any machine because it is measured there. Aider's leaderboard does the same per model and edit format, but you need local numbers (https://aider.chat/docs/leaderboards/).
2. **Workspace › Run History with evidence.** Shows the flow graph, pass/fail for each check, the model per step, tokens and time, a per-step diff and revert-to-step. Includes a "compare two runs" view that feeds the matrix. Pattern from Codex's per-task logs and test output (https://openai.com/index/introducing-codex/) and Zed's multibuffer review (https://zed.dev/docs/ai/agent-panel).
3. **Projects › Onboarding + calibration.** Detect the hardware class, scan Ollama/GGUF models, run a 5-minute calibration, pick a template from the detected stack, then do a guided `explore` run. The goal is visible value in under 10 minutes on any machine.
4. **Workspace › Approval mode per flow.** Options are plan-only, ask per step, auto with checks, or autopilot. Destructive flows (purge, config, network, pentest) always ask. Pattern from Junie and Claude Code permission modes (https://www.jetbrains.com/junie/).
5. **Models › "Best harness for this task" advisor.** For each specialty, a card shows the flow, role chain, model pair and an optional external harness to A/B. Each pick is labeled *measured* or *claimed*, with a date.
6. **Projects › Templates gallery.** The flows from section 2 as YAML recipes. Each card shows its local success rate from history (Warp Workflows pattern, https://docs.warp.dev/knowledge-and-collaboration/warp-drive/workflows).
7. **Workspace › UI Studio.** Live preview, click an element to edit (https://docs.lovable.dev/features/visual-edit), 2-viewport screenshots, SSIM and axe badges, and VL critique notes.
8. **Workspace › Test Studio.** Coverage map plus mutation score per file. Each surviving mutant is a one-click `hunt` task (https://stryker-mutator.io/docs/).
9. **Projects › Autopilot Queue.** Nightly test+hunt, weekly deps audit and purge, run when the machine is idle and plugged in, throttled by VRAM and thermals. Results land as accept/reject/rerun cards (https://docs.cursor.com/background-agent).
10. **Settings › Learning.** Per-project conventions, failing patterns, "model X won task type Y", and rules promoted from repeated reviewer feedback. Every entry is editable, records which run it was learned from, and is searchable with bge-m3 (Windsurf Memories pattern, https://docs.windsurf.com/windsurf/cascade/memories).
11. **Workspace › Data Studio.** SQL cells with inline result grids, a schema browser and saved queries (Hex reactive DAG, https://learn.hex.tech/docs/explore-data/projects/project-execution/graph).
12. **Models › Drift and cost.** Pass rate over time per role, plus time and energy per role. Suggests swaps with one-click apply.

All the Studios are views over the same flows, not separate engines.

---

## 4. Experiments to fill the matrix (quality first)

The method is the same for every experiment: fixed seeds, temperature 0.2 (thinking models at their recommended settings), and 3 repeats. Report pass rate with a 95% CI, then tok/s, time-to-first-token, peak VRAM and model-swap time. Store the results in the run ledger and draw the matrix from them.

| # | Experiment | Models | Fixture | Metric that decides |
|---|---|---|---|---|
| E1 | Code-gen baseline | all ≥ 4B text models | 40 tasks from HumanEval+/MBPP+ plus 20 repo-style tasks in your own languages | pass@1 on hidden tests |
| E2 | Heavy tier viability | 35B-A3B IQ3 vs Qwen3 14B vs 8B | 15 multi-file feature/bugfix tasks from your own repos | resolve rate, wallclock per task. Is the 35B worth its speed cost? |
| E3 | Reasoning on/off | qwen3 4B/8B/14B, qwen3.6 | E1 subset + 10 math/numeric | pass delta vs token and time cost, per specialty |
| E4 | SQL | Granite 3.3 8B, Granite 4.2 8B, qwen3 8B, Qwen3 14B | ~50 BIRD/Spider dev items on sqlite (subset; licensing **[unverified]**) | execution accuracy, single-shot vs 3-candidate vote |
| E5 | VL critic reliability | Qwen2.5-VL 3B, Gemma4 e4b (if vision works in your build, **[unverified]**) | 40 screenshot pairs labeled by hand (same/different + what changed) | agreement with labels. Below about 70% means keep it advisory-only |
| E6 | Reviewer family effect | Granite 4.2 8B, Gemma4 e4b, Bonsai 9B, Qwen3 8B reviewing Qwen3 output | 30 patches with seeded bugs | bug catch rate and false-alarm rate, same family vs other family |
| E7 | Swap cost on gpu6 | worker ↔ reviewer ↔ VL | none | seconds per swap. This decides batching and whether architect/editor pairing pays off |
| E8 | Speculative decoding | qwen3 0.6B / 1.7B drafting for qwen3 8B/14B | E1 prompts | acceptance rate, tok/s gain, no change in output quality |
| E9 | Router accuracy | qwen3 0.6B, 1.7B, Granite 3.3 2B, Llama 3.2 3B | 100 labeled prompts → flow | accuracy and latency |
| E10 | Test quality | Qwen3 8B vs 14B in the `test` flow | 10 modules with mutmut | mutation-score delta per minute |
| E11 | Abliterated vs stock | Qwen3 abliterated 8B/14B vs stock qwen3 8B | E1 + security fixtures | does abliteration cost coding accuracy? (**[unverified]** either way) |
| E12 | Native loop vs external harness | Architect `bugfix` vs Aider headless on the same model | E2 tasks | resolve rate and time. Validates the adapter idea |
| E13 | Embeddings | bge-m3 vs ripgrep-only localization | 30 bugs with known files | top-3 file recall |

Order to run them: E7 → E1 → E3 → E2 → E6 → E4 → E5 → E10 → the rest. Swap cost and baselines shape every other design decision.

**Generalizing:** ship the fixtures with Architect and run E1, E7 and E9 as the 5-minute onboarding calibration. Run the rest from the matrix page's "Re-benchmark". Key every result by hardware class and quant, so a gpu24 machine fills its own column instead of inheriting gpu6 guesses.

---

### Known errors corrected in the input research
- The 2402.13521 paper is Mathews & Nagappan, not TiCoder.
- The CORE numbers belong to Wadhwa et al., not "Copiloting the Copilots".
- 2406.16386 is DCGen, a divide-and-conquer generation method, not a fix loop.
- The Agentless headline cost-and-score figure comes from v1.5.
- "No SQL benchmark exists" is doubtful, because the BIRD and Spider 2.0 leaderboards are maintained.