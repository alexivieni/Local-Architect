# Architect: ranked roadmap of the merged ideas

I merged 48 raw ideas into 30. The first group needs nothing new from the internet and each item is small. Ranking is by value to a solo builder who ships paid apps on a 6GB RTX 4050 laptop and wants work that is local, verified, and can spread across machines.

Merges made:
- Speculative decoding appeared twice and is now one item.
- The secrets scan appeared twice and is merged with the license guard.
- Nightly eval, batched eval, idle/AC jobs, battery routing and thermal limits are now one "power-aware night shift".
- LoRA fine-tune, distillation from verified traces, eval-gated promotion and hot-swap adapters are now one "learn from my own work" track.

---

## 1. Build next (offline, small, no download)

Order matters. The first five add to each other.

### 1. Grammar-constrained output for every structured flow step (S)
Pass a GBNF grammar or JSON schema to llama.cpp on every step that expects structured output. Small models often break JSON, and each retry costs time and VRAM. Removing those retries makes the step both faster and more reliable. It is already built into llama.cpp, so no new tools are needed. Sources: [llama.cpp grammars](https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md), [XGrammar](https://arxiv.org/abs/2411.15100).

### 2. Per-repo conventions and "do not" lists from accepted and rejected diffs (S to M)
After the owner accepts a diff, write one line about what it showed (naming, error style, test layout) to `CONVENTIONS.md`. When the owner rejects a diff, ask for one line on why and add it to a "do not" list. Both files are plain text the owner can read and edit, and both get added to prompts. For small models, explicit repo rules are the cheapest quality gain there is, and the owner can always see what was learned. Precedent: [Aider conventions](https://aider.chat/docs/usage/conventions.html), [Cursor rules](https://docs.cursor.com/context/rules). Rejections work as preference data without any training ([DPO](https://arxiv.org/abs/2305.18290)).

### 3. Semantic result cache (S)
Hash the flow step, its inputs, the hashes of the files it reads, and the model id. On a match, skip the model call. Flows get re-run constantly during iteration, and a step that already ran on the same input should cost nothing the second time. Precedent: [Bazel caching](https://bazel.build/remote/caching), [GPTCache](https://github.com/zilliztech/GPTCache).

### 4. Save the KV cache to disk per project (S)
Use llama-server `--slot-save-path` with the slot save and restore endpoints, so the long system prompt and repo map are processed once and then loaded from disk. With long context on a small GPU, prompt processing takes most of the time. Source: [llama-server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).

### 5. Prompt-lookup (n-gram) drafting for edit tasks (S)
This speeds up generation by guessing tokens from the file being edited, with no draft model and no extra VRAM. Edits mostly copy the source, which suits it well. It is the free half of speculative decoding. The half that needs a draft model is in group 3. Sources: [llama.cpp lookup](https://github.com/ggml-org/llama.cpp/tree/master/examples/lookup), [prompt-lookup-decoding](https://github.com/apoorvumang/prompt-lookup-decoding).

### 6. Capability-tiered command allowlist (S)
Three tiers:
- Read-only commands run without asking.
- Writes need the flow step's permission.
- Network or install commands always ask.

This puts the "downloads only with a yes" rule into code rather than relying on habit, and it removes most prompts. Precedent: [Deno permissions](https://docs.deno.com/runtime/fundamentals/security/), [Claude Code settings](https://docs.anthropic.com/en/docs/claude-code/settings).

### 7. Power-aware night shift (S to M, merged)
One scheduler that does the following:
- **Plugged in and idle:** re-run the eval harness on all tiers and flows, using parallel slots (`-np`) for throughput. Refresh the code index. Record test baselines. Pre-warm caches.
- **On battery:** prefer the smaller tier and put heavy jobs off until later.
- **Thermal:** read GPU temperature and power from `nvidia-smi` and lower the batch size before throttling starts.
- **Morning report:** regressions, and the best model for each task type, so tier choice comes from data rather than guesses.

This turns the eval harness from something the owner runs by hand into a regular check for drift. Sources: [Task Scheduler idle conditions](https://learn.microsoft.com/en-us/windows/win32/taskschd/task-idle-conditions), [GetSystemPowerStatus](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getsystempowerstatus), [nvidia-smi](https://docs.nvidia.com/deploy/nvidia-smi/index.html), [continuous batching (Orca)](https://www.usenix.org/conference/osdi22/presentation/yu), [Aider leaderboard method](https://aider.chat/docs/leaderboards/).

### 8. Watts, time and tokens per task (S)
Sample `nvidia-smi` power during each run and show Wh, seconds and tokens for each task and each tier on the Models page. That makes the cost of each tier concrete on a laptop, and it feeds the router in item 15. Precedent: [CodeCarbon](https://github.com/mlco2/codecarbon).

### 9. Time log per client, exported as invoice lines (S)
Task runs already record when they start and stop. Group them by project and client, then export a list of lines for an invoice draft. This links the tool straight to income. Precedent: [WakaTime](https://wakatime.com/).

### 10. Built-in secrets prefilter (S, merged)
Run one regex pass, using rules modelled on gitleaks, over prompts, tool output and diffs before any accept. It takes milliseconds and protects client work. The full gitleaks binary and the license scan are in group 3. Sources: [gitleaks](https://github.com/gitleaks/gitleaks), [GitHub push protection](https://docs.github.com/en/code-security/secret-scanning/push-protection-for-repositories-and-organizations).

### 11. MoE expert offload settings (S, config only)
Add tier presets that keep sparse-MoE expert tensors on the CPU (`-ot` / `--n-cpu-moe`) and attention layers on the GPU. On 6GB this gives the most model quality per GB, but it only matters once an MoE model is on disk (see group 3). Source: [llama.cpp PR 11397](https://github.com/ggml-org/llama.cpp/pull/11397).

---

## 2. Build later (offline, bigger or depends on the items above)

### 12. Best-of-N attempts in git worktrees (M)
Run N attempts in separate worktrees, keep the one that passes the checks, and keep the others so they can be compared. This is the cheapest quality boost for small models, but it needs the cache, grammar and power items first, or it will be slow and drain the battery. Sources: [git worktree](https://git-scm.com/docs/git-worktree), [AlphaCode](https://arxiv.org/abs/2203.07814).

### 13. Local PR review page in the hub (M)
Show each attempt as a PR: the diff, check results, the model's reasoning, and approve / merge / request-changes buttons. This is the natural place to capture accept and reject reasons for item 2. Precedent: [Gitea](https://about.gitea.com/).

### 14. Teach-it-once skills (M)
After a task succeeds, offer "save as skill". It writes the steps, the files touched and the checks into a YAML flow with a description of when to use it. Freelance chores come back again and again (a new endpoint, a Stripe webhook, a CSV import). Precedent: [Anthropic Agent Skills](https://www.anthropic.com/news/skills), [Voyager](https://arxiv.org/abs/2305.16291).

### 15. Learned router without an LLM (M)
Train a logistic or embedding classifier on past routing decisions and their eval outcomes. Routing then takes milliseconds and can be checked against labels. Run it on the CPU with EcoQoS so the P-cores stay free for builds and tests. RouteLLM reports cost cut by more than 2x at the same quality ([paper](https://arxiv.org/abs/2406.18665), [repo](https://github.com/lm-sys/RouteLLM), [EcoQoS](https://devblogs.microsoft.com/performance-diagnostics/introducing-ecoqos/)).

### 16. Test-impact selection (M)
Build a map from changed files to their tests in the background. Run the affected tests first, and the full suite when the machine is idle. Checked steps then finish in seconds. Precedent: [Meta predictive test selection](https://engineering.fb.com/2018/11/21/developer-tools/predictive-test-selection/), [pytest-testmon](https://github.com/tarpas/pytest-testmon).

### 17. Portfolio view and batch flows (M)
One page listing every repo with its health: tests, old dependencies, open tasks, last touched. Batch flows such as "bump X everywhere" run across repos, and each change still goes through the checks. Precedent: [Renovate](https://docs.renovatebot.com/), [Sourcegraph batch changes](https://sourcegraph.com/docs/batch_changes).

### 18. Client handoff pack (M)
One click builds a zip with the README, setup steps, env template, license, changelog and test report. Adding an SBOM needs Syft (group 3). Clients see professional work and come back. Precedent: [keep-a-changelog](https://keepachangelog.com/).

### 19. LAN work-stealing queue for evals (M)
Idle machines that the owner has paired pull whole eval jobs from the laptop's queue. This is task-level distribution, not splitting a model across machines. It is sturdy and scales almost linearly. It is the "distributed" goal in its simplest form. Precedent: [BOINC](https://boinc.berkeley.edu/), [Ray](https://docs.ray.io/).

### 20. Speculative next-step execution (M)
While the owner reads a plan or diff, run the likely next step (lint, tests) in a throwaway worktree, and keep the result only if that path is chosen. It needs item 12 and the sandbox (item 26). Source: [Ye et al. 2025](https://arxiv.org/abs/2510.04371).

### 21. Hybrid code search index (M)
Combine ripgrep with embeddings, refreshed by a commit hook, to retrieve better context for small windows. It needs an embedding model; if none is installed, that falls under group 3. Precedent: [Aider repo map](https://aider.chat/docs/repomap.html).

### 22. Auto-tune prompts and flow settings overnight (L)
Use a DSPy-style search against the eval set. Keep only changes that beat the baseline, and show each one as a diff for approval. Do this only after the night shift (item 7) is stable. Sources: [DSPy](https://dspy.ai/), [OPRO](https://arxiv.org/abs/2309.03409).

### 23. Router on the iGPU via Vulkan (M)
Run the router on the integrated GPU and leave all 6GB of the 4050 to the worker. Value depends on how good the iGPU is, so measure it first. Source: [llama.cpp Vulkan build](https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md#vulkan).

### 24. Carbon-aware heavy jobs (M, optional)
Use a cached grid profile to move heavy jobs into low-carbon hours. Low value for this owner, so keep it as an option to switch on. Source: [Carbon Aware SDK](https://github.com/Green-Software-Foundation/carbon-aware-sdk).

---

## 3. Needs a yes (downloads models, binaries or packages)

Ask before each one, and state the name, source and size.

### 25. Speculative decoding with a tiny draft model (S plus one download)
For example, Qwen2.5-Coder 0.5B drafting for the 7B through `--model-draft`. Output is identical and generation is about 1.5 to 2.5x faster on code. This is the highest-value download, but check the VRAM headroom on 6GB first. Sources: [Leviathan et al.](https://arxiv.org/abs/2211.17192), [llama.cpp speculative](https://github.com/ggml-org/llama.cpp/tree/master/examples/speculative).

### 26. Fast sandbox: Windows Sandbox or WSL2 (M)
Run agent commands on a copy-on-write copy of the repo, with the network off by default. Turning on the Windows feature or a WSL distro needs a yes. Items 12 and 20 depend on this. Sources: [.wsb config](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-configure-using-wsb-file), [Codex CLI](https://github.com/openai/codex).

### 27. LSP diagnostics as a check (M)
Run language servers headless (pyright, tsserver) and feed type errors back before the owner sees a diff. This feedback is stronger than lint alone. Sources: [multilspy](https://github.com/microsoft/multilspy), [Serena](https://github.com/oraios/serena).

### 28. Learn from my own work: verified distillation and LoRA (L, merged)
Keep only heavy-tier outputs that passed the checks and tests. Train a QLoRA adapter for the worker on request or overnight. A new adapter is promoted only if it beats the current one on the held-out eval set; otherwise it rolls back automatically. llama-server `--lora` can then hot-swap adapters per project or domain (SQL, PowerShell, math). This is the boldest item on the list, and the eval gate stops it from ever shipping something worse. It needs Unsloth and PyTorch, which are several GB. Sources: [STaR](https://arxiv.org/abs/2203.14465), [QLoRA](https://arxiv.org/abs/2305.14314), [Unsloth](https://github.com/unslothai/unsloth), [S-LoRA](https://arxiv.org/abs/2311.03285), [model collapse](https://www.nature.com/articles/s41586-024-07566-y).

### 29. Data and UI checks (M each)
- **DuckDB scratch space:** one binary. Drop in CSV or Parquet files; the agent writes SQL and the query is saved with the project ([duckdb.org](https://duckdb.org/)).
- **Notebook execute-and-check:** needs a Jupyter kernel. The agent edits cells, runs them, and checks shapes, nulls and asserts ([jupyter-client](https://jupyter-client.readthedocs.io/)).
- **Visual check step:** needs Playwright browsers. Compare before and after screenshots ([Playwright snapshots](https://playwright.dev/docs/test-snapshots)).
- **Screenshot-to-bug:** needs a vision model through Ollama ([Ollama vision](https://ollama.com/blog/vision-models)).

### 30. Remaining downloads, lower priority
- Full **gitleaks and ScanCode** for license guarding ([ScanCode](https://github.com/aboutcode-org/scancode-toolkit)).
- **Syft** for the SBOM in the handoff pack ([Syft](https://github.com/anchore/syft)).
- An **MoE coder model** to use item 11.
- **Local MCP servers** with per-server allow lists ([MCP servers](https://github.com/modelcontextprotocol/servers)).
- **whisper.cpp** push-to-talk with a spoken-spec mode ([whisper.cpp](https://github.com/ggml-org/whisper.cpp)).
- **LAN model pooling** with llama.cpp RPC: high effort and fragile, so prefer item 19 ([rpc](https://github.com/ggml-org/llama.cpp/tree/master/tools/rpc), [exo](https://github.com/exo-explore/exo)).
- A **VS Code extension**: large effort, and it pulls focus away from the hub ([Continue](https://github.com/continuedev/continue), [Cline](https://github.com/cline/cline)).

---

## Suggested first sprint

Items 1, 2, 3, 4 and 6. Together they make every run faster, safer and more tied to the repo, with nothing to download. Then build item 7, so every later change is measured by the night shift rather than judged by feel.