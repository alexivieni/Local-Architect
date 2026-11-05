# Architecture review, end of 2026-11-04 (chief architect)

What this is: a ranked review of Architect after a day in which 43 Claude Code agents built new parts in parallel. It is written
for the person who owns the machine. Plain words come first, then the evidence.

**Evidence labels, used throughout:**
- **measured**: measured on this laptop (eval reports, ledger, snapshot);
- **code**: read in the code, with the file and function named;
- **report**: taken from one of today's review reports (cited by name, finding id in brackets);
- **vendor**: Anthropic's public documentation and engineering posts;
- **estimate**: arithmetic, not yet measured.

**How it was written:** I read everything for this review and changed nothing. I ran no commands except read-only listings, and no
suite. Nothing below has been re-run.

**The machine** (docs/pc_snapshot_20261104.txt, measured):
- CPU: i5-13420H, 4 performance cores with two threads each plus 4 efficiency cores (12 threads).
- GPU: RTX 4050 Laptop with 6 GB; 0.63 GB free with the servers up.
- RAM: 32 GB, 4.3 GB free (86% used).
- Drives:
  - C: is an NVMe drive (1 TB, 240 GB free);
  - D: is a 32 GB FAT32 USB card ("Generic STORAGE DEVICE");
  - E: is flagged as possibly fake-capacity.
- Models:
  - router: qwen3:1.7b on the CPU;
  - worker: huihui_ai/qwen3-abliterated:4b, all 36 layers on the GPU, 2.92 GB;
  - heavy: eriktonon/qwen3.6-moe-uncensored:iq3 beside the worker, with 11.57 GB of experts in RAM and 2.25 GB on the card.

---

## 0. The five things to do first (one-line summary of everything below)

1. **Claude is set to do all the coding right now, at the highest price, with caps that leak.** `architect_config.json` holds
   `online.enabled: true`, `cloud_tier: "all"`, `cloud_model: claude:claude-opus-5-5` and `cloud_fast: true`.
   - Fast mode bills at 2x (code: `arch_cloud.py:597`).
   - `cloud_usd_per_run` is 2.0, four times the default of 0.5.
   - Claude Code runs, fleets, Managed Agents and batches bypass the daily cap entirely (review_20261104 H1, H2).
   - A day on `all` also yields **zero training samples**: `arch_dataset` drops every `transport == "claude"` reply.
   - Set `cloud_tier` to `escalation` (or `plan`) and `cloud_fast` to off until the hybrid benchmark has been run. It never has: there
     is no `hub_data/learning/hybrid_*.md`.
2. **The worker is the weakest of the qwen models measured here.** docs/model_shootout_20261104.md (measured, 17 tasks), with
   **huihui_ai/qwen3-abliterated:4b**, the worker in use, in **bold**:

   | Model | Passed | Median per task |
   |---|---|---|
   | qwen3:8b | **12/17** | 75 s |
   | qwen3:4b | 10/17 | 213 s |
   | huihui_ai/qwen3-abliterated:8b | 8/17 | 64 s |
   | **huihui_ai/qwen3-abliterated:4b** | **7/17** | 49 s |

   Three tasks is past the one-to-two-task noise band the report itself names. Choose the worker from the measurements (S1).
3. **Make the money caps hold** (H2, H1, H3, H5 in review_20261104): one `arch_cloud.reserve()` helper, a cross-process lock, and a
   per-process temp file.
4. **Fix dataset dedupe before the first training night** (review_20261104 H4). It keys samples on the first 2000 characters of the
   prompt, and prefix-stable prompts make those characters identical across tasks. As it stands, the overnight plan's
   50-sample minimum may never be reached.
5. **Give back 12 GB of RAM.** The heavy MoE sits beside the worker holding 12.5 GB of RAM, mostly to serve
   planner/architect/evaluator. Those three roles are pinned to it in `architect_config.json`, and in `all` mode they go to Claude
   anyway. Tie `heavy_beside` to the Claude mode (S4).

---

## 1. How Claude's own agent tooling organises work, against Architect

Only public material is used here (vendor): Claude Code's documentation, the "building effective agents", "multi-agent research
system", "writing tools for agents" and "Claude Code best practices" posts, and the prompt-caching and effort pages.

| Practice (vendor) | What Architect has | Verdict |
|---|---|---|
| **Lead + subagents.** A lead splits the work. Subagents run in their own context windows and hand back a condensed result, not their raw reads. Parallel subagents cost many times a single chat in tokens, so the lead scales the number of agents to the task. | `arch_fleet.launch` (Claude Code agents with `kind: local`), `arch_localagent.run` (the same brief on the local worker), `arch_fleet.hybrid_plan` ("Claude thinks, local writes"). The scout step in `flows/scout_then_build.yaml` (router `tool_loop`, `max_steps: 6`) is the local form of "a subagent reads, the writer gets a brief". | **Matches.** The local version is unique: a GPU queue (`_GPU_QUEUE`, one writer on the card) rather than a money queue. **Behind** on the scaling rule: nothing yet decides how many agents a goal deserves. `arch_broker.route` exists to decide placement, but nothing calls it (release_20261104 §3). |
| **Isolation.** Claude Code isolates parallel agents with git worktrees, so two agents cannot touch the same checkout. | Isolation by **file ownership**: every brief names its files, and `plan_check` refuses overlaps. | **Behind, and it failed today.** ag_viz's own final report (agent_logs/ag_viz.log) says "Another agent was editing `architect_hub.py` and `hub_ui.html` at the same time". That broke METHOD.md rule 2 ("hub_ui.html is queued"). Ownership is checked at plan time, not enforced at write time, for Claude agents. Local agents are guarded twice (`arch_localagent`: refused before the tool, and again at the engine commit). Claude agents are not. A worktree per Claude agent, merged by the lead after the suites pass, closes this (S12). |
| **Prompt caching.** Keep the stable prefix (tools, system, project context) first; a reused prefix costs a fraction of normal input. | Prefix-stable prompts everywhere (`arch_selftest_chain` enforces it for retry templates). `arch_cloud.chat` puts breakpoints on the system prompt and `msgs[0]`. The Council fell from $0.27 to $0.18 (measured, METHOD.md). On the local side, llama-server's `--cache-ram`, `--slot-save-path` and per-project slot restore (`arch_server.py:1000`). | **Matches, and goes further locally.** Two code bugs (claude_api_review §0): the Studio breakpoint includes the question, so the next message can never read it, and `fmt == "json"` drops the system breakpoint. |
| **Effort.** More thinking for planning and review, less for exact fill-in work. | Claude roles carry an effort setting (`arch_cloud`). Local models get `server.think_budget` 800 and `fast_think_budget` 256. The budget is measured: an unlimited planner thought ~3000 tokens before writing a ~200-token plan. | **Matches.** |
| **Tool design.** Few tools, each clearly described, each returning compact, token-efficient output, with errors the model can act on. | `arch_tools`: capped, path-checked, one call per reply, constrained by `schema()`. `edit_lines` is refused when the result does not parse. History is cut (`HISTORY_FULL` 3, `HISTORY_KEEP` 10, `OLD_RESULT_CAP` 600 characters). | **Better** for small models. A reply that breaks the schema cannot reach the disk. |
| **Verification.** Give the agent a way to check its own work (tests, linters, screenshots). | The product's core: gates with three outcomes, the fake-code gate, the shrink guard, `BestState`, hidden judges in evals. The fleet's tests run with `ALLOWED_PREFIXES` and no shell punctuation. | **Clearly better.** A model's verdict is re-checked in code, and "could not run" never becomes a pass. One hole: a fleet agent may own the very script that judges it (report M6). |
| **Plan mode.** Read-only exploration and a plan the person approves before any edit. | Claude Code runs can start in `plan` mode (`arch_claudecode`). The local path has `arch_schemas.validate_plan`, the live todo (`Engine.todo_set`) and `arch_augment.sharpen` (goal, files, acceptance tests, cost estimate). | **Behind in the product.** `sharpen` is not wired: there is no Plan card before a local run, though it was "agreed with the user" (CLAUDE.md, next design steps). |
| **Memory / CLAUDE.md.** Project instructions read every session, plus notes. | `.arch/memory/*.md` (a lesson is `confirmed` only after a passing run), skills, the ledger's few-shot examples, the Logbook (FTS5). | **Better**: lessons are validated by outcomes, not just kept. |
| **Context management.** Compaction or summaries when a long session fills the window. | Fitting at call time (`Engine._call`: `cut_context` drops whole `=== ` sections from the end). The local agent shortens old turns. | **Partly behind.** There is no summary of what was learned earlier in a long local tool loop; old turns just become one line each. For a 4B model at 8192 tokens that is the right default, but the scout's findings should be carried forward as a fixed brief, not as history (see §3). |

**Where Architect is ahead:**
- It is local, free at the margin, and offline by default.
- Every pass is decided by code, with three outcomes.
- It keeps a per-run swap ledger and an honest "not sure".
- It produces receipts.
- The night shift runs entirely on owned hardware.

**Where it is behind:**
- Isolation is enforced only at plan time.
- No component decides how many agents a goal deserves, or where to run it. The broker is not wired.
- There is no plan approval step before local runs.
- Today's own process lost money control. The METHOD.md claim "agent jobs came in at roughly 10-25% of their caps" holds for the
  first 11 agents. The full ledger, all 43 rows, sums to **≈ $80** (measured, `hub_data/learning/ledger.jsonl`). The four agents
  that worked on `hub_ui.html` or the design (ag_design $6.97, ag_viz $6.07, ag_ui $5.45, ag_polish $2.75) cost **$21.2, 27% of
  the day**.

---

## 2. Ranked suggestions for this machine and these models

The columns:
- **Where**: the component that does the work.
- **Who**:
  - **Local** is a job the local worker can do through `arch_localagent`: a small, exact patch in one owned file, with a test.
  - **Claude** is a job that needs judgment across files, touches money or security, or edits a file too large for an
    8192-token window.
  - **Person** is a settings choice, or a run only the owner should start.

Effort: S under an hour, M a few hours, L a day or more.

| # | Change | Where | Gain | Effort | Risk | Who |
|---|---|---|---|---|---|---|
| S1 | Choose the worker from measurements | GPU | +3 to +5 tasks of 17 (measured) | S | low | Person |
| S2 | Leave Claude-for-everything mode | Claude | money; training data comes back | S | none | Person |
| S3 | Make the caps hold | Claude (code) | the backstop works | M | low | Claude |
| S4 | Heavy beside only when it is used | RAM, GPU | ~12 GB RAM, 2.25 GB VRAM (snapshot) | S | low | Local |
| S5 | Fix dataset dedupe | CPU | samples per night (training plan estimate 30-300) | S | low | Claude |
| S6 | Hub: Host check on every GET; no subscription token | CPU | closes transcript leak | S | low | Claude |
| S7 | Context path: stop re-chunking and re-tokenising | P-cores | 1-3 s CPU per `context()` on big trees (estimate) | S | low | Local + Claude |
| S8 | `_alive` without `tasklist` + reuse the recorder's sample | P-cores | ~8 process launches per 5 s gone (estimate) | S | low | Local |
| S9 | UI: stop the 9-second full re-render | browser | no scroll jumps, details stay open | S | low | Claude |
| S10 | Run the missing measurements (shoot-out B/C, hybrid, livetest) | GPU, overnight | evidence for S1/S2 | S | none | Person |
| S11 | Wire the six library-only modules, in order | various | features already paid for | M | low-med | Mixed |
| S12 | Enforce file ownership at write time for Claude agents | NVMe | no more concurrent edits | M | med | Claude |
| S13 | Claude prompt-cache fixes + Council starts on the first token | Claude | 15-40 s per Council (estimate), cents | S | low | Claude |
| S14 | YAML flows vs `flows.engine = legacy`: make the iterator honest | CPU | stops tuning files that do not run | S | low | Claude |
| S15 | Background chores on the efficiency cores | E-cores | P-cores stay free for the router | S | low | Local |
| S16 | Archive target: C: first, card as a copy | NVMe, SD | no lost runs when the card is pulled | S | med | Person + Local |
| S17 | Router and judge calls at temperature 0 (cacheable) | CPU, GPU | repeat routing costs 0 | S | low | Claude |
| S18 | Window: 12288 for the worker once heavy is off the card | GPU | fewer context cuts | S | low | Person |
| S19 | Fleet: an agent may not own its judge; a missing script = not sure | CPU | verification integrity | S | low | Local |
| S20 | Author `hub_ui.html` in parts, serve it as one | NVMe | cheaper, parallel UI work | M | med | Claude |
| S21 | Wire the inert settings (`config_strict`, `cache.max_*`) | — | settings that do what they say | S | low | Local |
| S22 | Token events merged in the event ring | RAM | the plan survives a reconnect | S | low | Local |

### S1. Choose the worker from measurements
- **Problem (measured):** in shoot-out A, the abliterated 4B passed 7 of 17; stock qwen3:4b passed 10; qwen3:8b passed 12 with a
  75 s median. The abliterated build was chosen for a reason the data does not show. It is fastest per task (49 s), but it
  finishes the fewest.
- **Change:** run `python architect.py port` and the two candidates below on `eval --set representative` with the heavy model NOT
  beside the worker (S4 frees the card).
  - qwen3:4b: same size, so it fits as today. Its 213 s median suggests it thinks; `think_budget` 800 already caps that.
  - qwen3:8b: about 5 GB at 4 bits. It needs nearly the whole card. `plan()` will show whether every layer fits at 8192 tokens
    with `desktop_gb` subtracted. If not, a few layers go to RAM, and n-gram speculation (the edit path) still helps.

  Promote only through `promote-model --compare`.
- **Expected:**
  - +3 tasks (qwen3:4b) or +5 tasks (qwen3:8b) of 17 at the same harness (measured on the shoot-out set).
  - The edit speed of 306-343 tok/s came from the n-gram cache, not from the model, so most of it should carry over (estimate).
  - The draft note (`qwen3:0.6b` shares the tokenizer) stays valid for the stock qwen3 family.

### S2. Leave Claude-for-everything mode until it has been measured
- **Problem (code + config):**
  - `cloud_tier: all` with Opus 5.5 in fast mode at 2x price (`arch_cloud.py:531`, `:597`) sends every coding step to the most
    expensive option.
  - `docs/hybrid_benchmark.md` exists to answer "what Claude buys you", but no `hybrid_*.md` report exists.
  - Training: `arch_dataset` drops Claude replies, so local training data stops accumulating on this setting.
- **Change:**
  - `cloud_tier = escalation` as the daily default: Claude is reached only after the local heavy model also stalls, and the
    report notes it often costs 0.
  - `cloud_fast = off` except for a hand-started urgent run.
  - Then run `python tools/hybrid_bench.py --modes off,escalation,plan --usd-cap 3` overnight and decide from "$ per finished task".
- **Who:** the person; no agent may edit `architect_config.json`.

### S3. Make the caps hold (review_20261104 H1, H2, H3, H5, M1, M2)
- **Problem (report, confirmed):**
  - `arch_claudecode.start`, `arch_fleet.launch`, `arch_agents.start` and `arch_batch.submit` never check `spent_today()` and
    never add their cost.
  - `arch_claudecode.start` skips `arch_cloud.allowed()`.
  - `cloud_spend.json` loses writes on Windows: `os.replace` meets an open reader, and `except OSError: pass` swallows it.
  - Parallel Council calls all pass the cap check before any of them adds its cost. One Opus call at effort max can write 64,000
    tokens, which the review prices at $1.28.
- **Change:**
  - `arch_cloud.reserve(cfg, usd)`: under a lock that works across processes (the msvcrt/fcntl pattern in `arch_heavy.py`),
    refuse when spent + reserved + worst case is over the cap, and clamp `max_tokens` to what is left.
  - Settle the real cost from each `ResultMessage`, batch result and session usage.
  - Write through a per-process temp file, retrying `os.replace`.
  - Give the cap a scope: each Engine owns its own budget, and Studio is budgeted per message.
- **Tests:** `arch_selftest_cloud`, with a fake client: a concurrent `ultra` at a $0.01 cap; two processes writing spend.

### S4. Heavy model beside the worker only when it is really used
- **Problem (snapshot + config):**
  - One llama-server holds **12.5 GB of RAM** and 2.25 GB of the card for the heavy MoE.
  - RAM is 86% used, and Windows Memory Compression is at 861 MB, which is a symptom of the shortage.
  - The heavy model serves planner/architect/evaluator, which `architect_config.json` pins to it. With `cloud_tier` at `plan` or
    `all`, those roles go to Claude, so the heavy server sits idle.
- **Change:** in `arch_server.plan`, `heavy_beside: auto` should mean "beside only when `online.cloud_tier` is `off` or
  `escalation` AND RAM free after loading stays above `RAM_RESERVE_GB`". Write a plan note saying so.
- **Gain:** about 12 GB RAM and 2.25 GB VRAM back. That also lets the worker use the whole card (S1, S18).
- **Cost:** a 15-20 s load per escalation, measured in pc_plan. Today's measured beside speed of 14-16 tok/s is kept whenever it
  is actually used.
- **Who:** a local agent can make the one-condition change in `plan()` with a pure-function test. The plan is fact-driven, and
  `arch_selftest_fastlane` shows the pattern.

### S5. Fix dataset dedupe (review_20261104 H4)
- **Problem (code):** `arch_dataset.near_key` uses the first 2000 characters of the user turn. CLAUDE.md requires the project
  context first, so every task in one project shares that head. With `dedupe=True`, every sample from one project collapses into
  one, and REPORT.txt says "same task again".
- **Change:** key on a hash of the normalised whole user turn, or on the request taken from the call record.
- **Test:** two goals over the same 3000-character context stay as two samples.
- **Why first:** docs/training_plan.md estimates 30-300 SFT samples per night against a `MIN_SAMPLES` of 50. Losing most of them to
  a key bug would make the training effort look like a failure of the models.

### S6. Hub exposure (report M3, M4)
- **Problem:**
  - `AHandler.do_GET` answers `/api/arch/claudecode`, `/chat`, `/agents`, `/events` and others **before** `model_hub`'s
    `_host_ok()` runs.
  - A page that rebinds its hostname to 127.0.0.1 can read Claude Code transcripts, including any `.env` an agent read.
  - Separately, the Agent SDK likely merges `os.environ`, so a `CLAUDE_CODE_OAUTH_TOKEN` in the hub's own environment can reach
    the child (report: plausible).
- **Change:**
  - Put one `_host_ok()` check at the top of `do_GET` for every `/api/arch/` path.
  - Set `CLAUDE_CODE_OAUTH_TOKEN=""` and `ANTHROPIC_AUTH_TOKEN=""` explicitly in `options.env`.
  - Wrap `int(since)` in a try.

### S7. The context path: stop re-chunking and re-tokenising (perf_report #5, #6, #10)
- **Problem (code; the time is an estimate):**
  - Every new `Engine` creates a `Retriever` with `dirty = True`, so the whole tree is read, `ast.parse`d and sha1'd before the
    index finds nothing new. Studio builds a new Engine per message.
  - `rank_files` re-tokenises every file on every `context()` call, which happens 20+ times per flow.
  - On the Architect tree (136 .py files, 3 MB) that is 1-3 s of CPU each time.
  - The router also runs on the performance cores (it gets half of them). Every second of Python tokenising there competes with
    the router and with the worker's sampling thread.
- **Change:** the exact patches are written out in perf_report: the `_FRESH` signature in `arch_index`, the `_TERMS_CACHE` in
  `arch_core`, and one `list_files` per Studio message.
- **Who:** a local agent for `arch_index.py` and `arch_chat.py` (small files, patches given verbatim, tests specified). A Claude
  agent for `arch_core.py`, which is far beyond an 8192-token window even with `view`/`edit_lines`.

### S8. Process launches on every poll (perf_report #2, #3, #4)
- **Problem:** `arch_server._alive` starts a `tasklist` per tier per check: about 6 every 5 s with 3 tiers. `/api/arch/gpu`
  re-samples what the recorder sampled 0-5 s earlier. `/api/state` runs `nvidia-smi` every 9 s. The perf report estimates each
  launch at 100-400 ms of CPU (community figure, not measured).
- **Change:**
  - `OpenProcess` + `GetExitCodeProcess` (the patch is in the report).
  - `arch_hw.LAST` reused when it is under 7 s old.
  - A 30 s cache on the static part of `detect_hardware`.
- **Measure:** process creates in Process Monitor over 60 s, before and after.

### S9. UI: the page rebuilds itself every 9 seconds (perf_report #1, #7, #8, #9)
- **Problem (code):** `refresh()` keys "same state" on JSON that includes `ram_free_gb`/`vram_free_gb`, which change on every
  poll. So Home, Models, Settings and five more pages are rebuilt every 9 s: the page jumps, and open `<details>` close.
  Hidden tabs keep polling. The console redraws an SVG every 700 ms even when no event arrived.
- **Change:** a `VOLATILE` key set (one line), `document.hidden` guards, redraw only when events arrived, and an in-flight guard.
  This is the cheapest UX win in the whole review: people read random scroll jumps as "the app is flaky".
- **Who:** Claude, queued on `hub_ui.html` (see S20).

### S10. Run the measurements the decisions depend on
- **Problem (measured):**
  - Shoot-out tables B (heavy candidates) and C (worker + heavy) are **empty** in docs/model_shootout_20261104.md. The pinning of
    planner/architect/evaluator to the MoE therefore rests on no measured comparison.
  - `hub_data/livetest.json` still describes 14B models from an earlier setup: 0/3, the router "gave no usable answer", and a JSON
    call stopped after 427 s.
  - No `training_night/` folder and no `night-baseline`/`holdout-baseline` eval exist yet.
- **Change** (overnight, under the heavy-job lock, one at a time):
  1. `livetest` with the current tiers.
  2. Shoot-outs B and C.
  3. `eval --flow build --tags holdout --label holdout-baseline`.
  4. The hybrid benchmark local row (`--modes off`, free).
- **Machine time:** about 7 minutes per task (measured), so ~2 h per 17-task set.

### S11. Wire the six library-only modules, in this order
- **Problem (release_20261104 §3; code):** `arch_broker`, `arch_routelearn`, `arch_augment`, `arch_placement`, `arch_why` and
  `arch_redact` (≈ 2,600 lines with suites) are called by nothing on the run path. A grep of the main modules finds only
  `arch_fleet.py:515` importing `arch_localagent`.
- **Order, and who does each step:**
  1. **`arch_redact.scrub` in `arch_dataset` export and `arch_learn.ingest`.** This is safety. Claude Code transcripts in
     `hub_data/claude_code/projects` may hold secrets an agent read. Local agent: a two-line call plus a test.
  2. **`arch_why.explain_event` in the Console's "doing" column** (`renderDoing`), served from the events endpoint. Claude
     (`hub_ui.html`).
  3. **`arch_augment.sharpen` as the Plan card** before a run: goal, files, acceptance statements, cost estimate, with Run /
     Edit / Cancel. It runs on the router tier on the CPU, so it never touches the card. Claude.
  4. **`arch_routelearn.suggest_for`: the one line documented in its own docstring**, in `arch_flows.route`. It is labelled
     "measured on this machine", and the router still decides. Local agent. Before it pays off, it needs `MIN_OK` judged runs per
     kind.
  5. **`arch_placement.apply_storage(dry_run=True)` as a Settings > Storage card**, offered once; nothing moves without a click.
     Claude.
  6. **`arch_broker.route` last**, first as a logged opinion next to the current choice (`broker.jsonl`), so its decisions can be
     checked against outcomes before it decides anything.

### S12. Enforce file ownership at write time for Claude agents
- **Problem:** see §1: ownership is checked by `plan_check` only, and today it was broken on `hub_ui.html`/`architect_hub.py`.
- **Change:** each Claude fleet agent works in its own git worktree (`git worktree add`, which is stdlib-callable via
  `subprocess`). The fleet runs the suites in the worktree, then merges only the owned paths into the main tree. A diff outside
  `files` gives the verdict `fail`, with the paths named.
- **Fallback, for when git is missing:** a hash of every non-owned file before and after the run; any change gives `not_sure`.

### S13. Claude prompt caching and Council latency (claude_api_review §0)
- **Change:**
  - In Studio, put the breakpoint on the context block and keep the question in a second, uncached block.
  - Keep `cache_control` in the `fmt == "json"` branch.
  - Start the Council's critic and alternative when the solver's first token arrives, not its last.
  - Drop the breakpoint for the two concurrent Haiku skeptics.
  - Fix `cost()` to read the per-model cache-read rates.
- **Gain (estimate in the report):** one worker reply time of 15-40 s per Council, and about $0.004 per Council.

### S14. YAML flows vs the default engine
- **Problem (code):**
  - `arch_core.DEFAULTS` sets `flows.engine: "legacy"`: YAML fills only flow ids that no Python class provides.
  - `arch_iterate` diagnoses, proposes and adopts edits to `flows/<id>.yaml`, including ids such as `build` and `bugfix` that the
    legacy engine never runs from YAML.
  - `flows/bugfix.yaml` still lacks the read-only-tests rule (release §3).
  - Result: the iterator can "improve" a file the person's runs never execute. Its trial copy `<id>_v2` does run, from YAML, so the
    trial measures a different engine than the one in use.
- **Change:** `arch_iterate.propose` labels a flow "shadowed by the Python class under the legacy engine" and refuses `adopt` for it
  unless `flows.engine = data`. Add the read-only rule to `bugfix.yaml` before anyone turns the data engine on.

### S15. Background chores on the efficiency cores
- **Problem (code):** `behavior.tests_on_ecores` puts tests on cores 8-11. The fast lane pins the worker to 0-7. Meanwhile, the
  Logbook ingest (`arch_learn.ingest`), the overseer scan, index refreshes and dataset export run at normal priority on any core,
  in the hub process or a child.
- **Change:** run those four as child processes with `arch_hw.ecore_mask()` and below-normal priority (the `arch_heavy` pattern).
  `arch_placement` already names them as efficiency-core work; this does it without waiting for the broker.

### S16. The archive target is the USB card
- **Problem (config + snapshot):** `storage.archive = "D:\\"`. D: is a removable 32 GB FAT32 card, which has a 4 GB per-file
  limit. pc_plan_20261104 §2 and `arch_placement` both say: datasets, archives and backups are **copied** to a slow removable drive,
  never kept only there.
  - Plausible: if `arch_archive`'s write-behind moves runs off C:, a pulled card loses them. I did not trace `arch_archive` end to
    end.
- **Change:** confirm in `arch_archive` that the C: copy stays until the card copy has been verified byte for byte (the
  `arch_placement.apply_storage` rule). If it does not, adopt that rule.

### S17. Make routing and judging cacheable
- **Problem (config + code):**
  - Every model in `architect_config.json` samples at temperature 0.6.
  - `arch_cache` answers only at temperature 0 or with a seed.
  - So the router's classification, which is asked the same question again and again by evals, retries, the supervisor's
    alternates and the Plan card, never hits the reply cache.
- **Change:** a per-role sampling override of temperature 0 for `router` and for structured judge calls (`ask_json` with a schema),
  through the existing `behavior.sampling` A/B setting so it can be measured. Generators keep 0.6: the qwen3 card's
  recommendation, and diversity helps `best_of_n` retries.

### S18. A larger window for the worker once the card is its own
- **Problem:**
  - `architect_config.json` asks for `num_ctx` 12288, but the server plan serves the worker at 8192, and `Engine._call` fits to
    the smaller. The setting promises more than the run gets.
  - CLAUDE.md measured sprint prompts at ~7k tokens of the 8192.
- **Change:** after S4, `server.ctx = 12288` for the worker (pc_plan estimate: ~0.6 GB of q8_0 KV per 8192 tokens, 4.9 GB usable).
  Check the fit with `server plan` first, and show the gap between the asked and served windows in the plan's plain words.

### S19. Fleet verification holes (report M6, L6, M5)
- **Change:**
  - `launch` refuses when an agent's owned paths overlap the script of a planned test command.
  - A test script missing from `root` is recorded as `ok: None`.
  - `_read` takes `_LOCK`, and `os.replace` retries on `PermissionError`.
- **Who:** a local agent, with three small changes in `arch_fleet.py` and three tests.

### S20. Author `hub_ui.html` in parts, serve it as one
- **Problem (measured):**
  - The file grew from 245 KB (perf_report) to 334 KB during the day and is **484 KB now**.
  - It is the only shared file in the method. It took 27% of today's agent spend, and its queue rule was broken.
  - No local model can hold it; even a Claude agent spends turns finding its place (ag_viz's log names the file 57 times in 94
    tool calls).
- **Change:** keep "one HTML file, no build step" for the person: the browser still receives one file. Author it as
  `hub_ui/*.part.html` (shell, style, one script part per page). `architect_hub` already reads the file and `.replace`s it on each
  load; it can join the parts in a fixed order at the same point, which costs about 1 ms per load (perf_report).
- **Ownership:** each page becomes a file an agent can own, and parallel UI work becomes safe.
- **Risk:** medium. Every hub-check that reads `hub_ui.html` as one file must read the joined text.

### S21. Inert settings (cleanup_report M1, "borderline")
- **Change:**
  - Wire `behavior.config_strict` into `ConfigFlow.run`; it is a gate setting, so wire it, do not remove it.
  - Call `arch_cache.prune(cfg["cache"]["max_rows"], cfg["cache"]["max_days"])` in `Engine.finish`.
- **Who:** a local agent: one owned file each, and each comes with a selftest.

### S22. Token events crowd out the plan (perf_report #12)
- **Problem:** about 3 token events per second fill the 4000-event ring in about 20 minutes of generation, pushing out the step,
  check and verdict events a reconnecting Console needs.
- **Change:** merge consecutive token events into one rolling record in `model_hub.Job`.
- **Who:** a local agent.

---

## 3. Inference strategy

### Which model for which step (on this machine, today's numbers)

| Step | Model | Why (evidence) |
|---|---|---|
| Route, classify, Plan card (`sharpen`), scout reads (`tool_loop` in `scout_then_build`) | router qwen3:1.7b on the performance cores | Never takes graphics memory (plan). Short structured answers. Make it cacheable (S17). |
| Sprint edits, tests, fixes, local agents | worker on the GPU, all layers | 56 tok/s writing; 306-343 tok/s on edits with the n-gram lookup cache (measured). DEF blocks keep small replies small. Choose the worker per S1. |
| Plan, architecture, evaluation | heavy MoE **when beside** (14-16 tok/s, ~1 s switch, measured); otherwise a 15-20 s load | Planning quality needs breadth. Its speed is fine for a ~200-token plan with `think_budget` 800. |
| Escalation of a stalled fix | heavy, only if `ready_now` says ~1500 tokens fit within 240 s | Dense 14B models at 1.9-2.7 tok/s are effectively never eligible (measured). |
| Claude | `escalation` by default; `plan` for features and designs if the hybrid benchmark shows a gain | Measured: a Council costs $0.18. A Claude Code first turn costs $0.10-0.17; today's agent jobs averaged ≈ $1.86 ($80 / 43). Unmeasured: the pass-rate gain. |

### When Claude pays for itself
A rule, to be replaced by the hybrid benchmark's "$ per finished task":
- **Pays:** a run the local stack has already failed, after `ALTERNATES` and the postmortem retry.
  - Each local retry costs about 7 minutes of machine time (measured) and nothing in money.
  - A Claude `plan` step costs cents and removes the most expensive local failure: a bad plan that the worker then implements
    faithfully.
- **Does not pay:**
  - Bulk edits under an exact brief: the worker plus gates already does these.
  - Anything at night when the goal is training data, because Claude output is excluded from it.
  - Fast mode by default, which doubles the price for wall-clock time nobody is watching at night.

### Caching, end to end (in the order a call meets them)
1. **Reply cache** (`arch_cache`, NVMe). This is an exact repeat at temperature 0 or with a seed, and it costs nothing. Today it
   rarely hits, because every model samples at 0.6 (S17). It is off under eval, by design.
2. **Claude's 30-day reply cache** (`arch_cloud`). It never stores a cut-off reply or a web answer (report, checked).
3. **Claude prompt cache.** The system and first-turn breakpoints, with a 5-minute (or 1-hour) lifetime. It is worth $0.27 → $0.18
   per Council (measured), with two bugs (S13).
4. **llama-server prompt cache in RAM** (`--cache-ram`, auto 2048 MiB per server on 32 GB). The worker's 2.3 GB of RAM is mostly
   this. Keep it: it is the local equivalent of item 3.
5. **Slot files on NVMe** (`--slot-save-path`, per tier, per-project restore in `arch_server.py:1000`). Reopening a project skips the
   prefill of its stable prefix. Keep these on C: (pc_plan), never on the card.
6. **N-gram lookup cache** (`hub_data/server/lookup/worker.bin`). This made edits 5-6x faster (measured).
7. **Retrieval** (`arch_fts` FTS5 + TF-IDF; the Logbook for past tasks; confirmed memory lines; ledger few-shot examples). It only
   orders what the prompt shows and never decides a pass.

   Its hidden cost today is CPU: every new Engine rebuilds the index work (S7).

### The speed path
- **N-gram speculation is the proven lever.** Measured: 56 → 306-343 tok/s on edits. A qwen3:0.6b draft was worse (66-78 tok/s on
  new code, 125 on edits), so it stays opt-in. That is correct.
- **Fast lane** (`server.fast_lane`): all layers and the cache on the card, `FAST_MIN_CTX` 6144, above-normal priority, P-core
  pinning.
  - Its own effect is **not measured** (pc_plan); estimate: small when idle, larger under load.
  - Measure it on the same edit task with the tok/s in the console strip, with and without a test suite running on the efficiency
    cores.
  - It also turns `heavy_beside` off, which is S4's gain on its own.
  - On Linux/macOS the priority change is not applied (report L10). Say "Windows only" in its plan note.
- **Scout briefs.** In `scout_then_build`, the router scout reads files and hands back findings, and the worker writes. Do not
  feed the scout's turns to the worker as history. Carry them as a fixed brief at the start of the per-try part, so the stable
  prefix stays cacheable (slot reuse, item 4). `arch_iterate.propose` already suggests a scout where failures match `CONTEXT_MISS`.
  That is the right trigger, once S14 makes its adoption honest.

---

## 4. Training strategy

**What to train:** the worker that will actually be served (S1 first). Training the abliterated 4B would polish the weakest
measured option. If qwen3:8b becomes the worker, QLoRA of an 8B model at 6 GB is beyond the plan's tested recipe ("4B at batch 1
and 2048 tokens is meant for ~6 GB"). Train the 4B stock model, and compare it, adapted, against the 8B unadapted on the holdout.

**Data, from verified local traces only** (docs/training_plan.md, plus these fixes):
- **SFT:** replies marked True whose run passed the hidden judges. Night driver: `evals/families/distributed.py --night`. The
  families are distributed (65 tasks, 13 held out) and harness (40 tasks, 8 held out).
- **Preference pairs:** a failed try followed by a passed try on the same task. `BestState` already records which try was undone
  as "clearly worse". That is a ready-made rejected sample, and it is the most valuable signal for a small model.
- **Tool trajectories:** from `arch_localagent` runs whose `checks.ok` is True *and* whose fleet check passed.
- **Never:**
  - Claude output: `transport == "claude"`; `by_claude`; overseer and Logbook records are `use: "analysis"`.
  - Replies whose checks returned `None`.
  - `_fake` exports.
  - Holdout tasks.
- **Before export:** apply S5 (dedupe) and S11-1 (`arch_redact.scrub`).

**Curriculum** (`train_lora.py --curriculum`), easiest first:
1. One DEF block that replaces a single function to make one test pass.
2. A module plus its tests from an exact interface (the distributed and harness templates).
3. Bug fixes with read-only tests.
4. Multi-module builds (the e2e family).

Mix in about 20% of each earlier level at every later level, so stage 1 is not forgotten.

**Measuring progress:**
- The honest number is the 21 holdout tasks: `eval --tags holdout --compare holdout-baseline`.
- The guard is `eval --set representative` (17 tasks), so a gain on the patterns does not hide a loss everywhere else.
- Promote only when the holdout gains **≥ 3 tasks** (one or two is noise, by the shoot-out's own rule) and representative is
  not worse.
- Track samples per night, accepted per run, and pass rate per template, from `night.jsonl`.

**How often:**
- Collect data every night the machine is idle.
- Train when at least 150 new accepted samples exist since the last adapter. By the plan's middle estimate (~120 per night), that
  is about weekly.
- Training needs the servers stopped (6 GB card), so a training night is a night without collection. Alternate them, under the
  heavy-job lock.
- Keep each night's export on C:, with a copy on D: (pc_plan).

---

## 5. UX: the five changes that would most improve how easy the app feels

Judged against `TABS` in hub_ui.html as it is now: 4 places and 27 pages. The Workspace place alone has **11** sub-pages: console,
run, studio, agents, fleet, monitor, map, library, blueprints, queue, launch.

1. **Stop the page from rebuilding itself every 9 seconds** (S9). It is one line. Today, scrolling jumps and open sections close
   while you read. Nothing else on this list costs so little for so much calm.
2. **Fold Workspace from 11 sub-pages to 4:**
   - **Console**, with "New task" as its input box rather than a separate page;
   - **Chat** (Studio);
   - **Agents**: Crew, Fleet, Monitor, Library and Blueprints as sections of one page;
   - **Queue**.

   Map and Run-the-app become actions inside the Console. The page ids stay, so `go("fleet")` still works, as CLAUDE.md requires.
3. **A Plan card before every run** (`arch_augment.sharpen`, already built and tested). It shows the sharpened goal, the files with
   exists/new/unknown, the acceptance statements, the flow, and an estimate labelled as one, with Run / Edit / Cancel. This was
   the next design step agreed with the user. It runs on the router on the CPU, so it costs no graphics memory.
4. **Plain-word "why" lines in the Console** (`arch_why.explain_event`, built, no model call). Every check and escalation reads as
   one sentence, with "could not run" kept distinct from "failed". Add a **Review screen** after the run that reuses
   `arch_why.explain_run` (5-12 sentences) above the receipt.
5. **One honest money line in the status strip, next to the stop button:**
   - "Claude: all · Opus fast · $x of $5 today";
   - fed by `cloud_spend.json` plus `cc_runs/*.meta.json` plus batches (report L9; S3 makes the number true);
   - plus a one-click "local only" toggle.

   Today the most expensive mode is on and nothing on the main screen says so.

---

## 6. Risks and debts introduced today, and the order to pay them down

| Order | Debt | Source | Why this order |
|---|---|---|---|
| 1 | Caps that leak: H1, H2, H3, H5, M1, M2 | review_20261104 | Money is being spent now. `cloud_tier: all` and fast mode are on. |
| 2 | Hub GETs without the Host check (M3); a subscription token may reach the SDK child (M4) | review_20261104 | Transcripts can hold secrets; the change is small. |
| 3 | Nothing from today has been run by its author | agent logs ("none of it has been run": ag_viz, and every acceptEdits agent); release_20261104 §4 | Run release §4 in order with `py -3.12`: flows validate, the full suite, both family checks, hybrid selftest, `check_overnight`, `eval --fake`, then `self-verify --write`. Commit only what passed. Report COULD NOT RUN (node missing) as such. |
| 4 | Training data: dedupe (H4), redact not wired, `exclude_from_training` documented but not called (L12) | review_20261104, release §3 | Before the first night, or the nights are wasted. |
| 5 | Fleet verification: M6 (own judge), M5 (Windows race), L6 (missing script = fail) | review_20261104 | The fleet is the method; its verdict must be trustworthy. |
| 6 | Six modules built but not wired | release_20261104 §3 | Paid for and tested; wire in the S11 order. |
| 7 | `hub_ui.html` at 484 KB, edited concurrently | measured, ag_viz.log | Fix the authoring (S20) before the next UI round, or the next round costs another ~$20. |
| 8 | YAML flows vs the legacy engine; `bugfix.yaml` missing the read-only rule | code, release §3 | The iterator must not tune files that do not run. |
| 9 | Older open items | CLAUDE.md | Escalation ignores `role_hosts`; the KV cache is sized as quantised without the cache flags. |
| 10 | Small debts | cleanup_report, release §3 | Dead code (L1-L3, L5, L6, model_hub's legacy agent plumbing M2, unused SSE route M3); eight `.bak-2026…` config backups copied into every self-improve candidate (L4: `arch_selfimprove.SKIP_SUFFIX` uses `endswith`); the README check count; `MANIFEST.sha256`; four suites that stop at the first failure instead of counting. |
| 11 | Low-severity robustness: L1-L5, L7, L8, L10, L11, L13 | review_20261104 | The memory growth of `_RUNS`/`THREADS` matters for a hub left up for days; batch these together. |
| 12 | Splitting the four giant functions | cleanup_report H1 | `AHandler.route` is ~435 lines, `Engine._call` ~325, `do_GET` ~308 and `replace_def` ~145. Do this last, one at a time, with the full suite around each, once nothing else edits those files. |

---

## Report
- **What already existed:**
  - Today's eleven review and plan documents, METHOD.md, the cost ledger and the agent logs.
  - The shoot-out and eval reports (`hub_data/evals.jsonl`), the machine snapshot, and the modules listed in the brief.
  - `LESSONS.md` does not exist yet. `arch_overseer.lessons()` writes it on its first scan into `hub_data/learning/`.
- **What I changed:** `docs/architecture_review_20261104.md` (new). No code edited, no settings touched.
- **Test command:** none for a document. Run the steps named in §6 row 3, in that order.
