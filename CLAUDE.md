# Architect

A local, offline agentic engineering team: 46 roles and 29 flows (every one a YAML file under `flows/`, `explore` being the tool loop; counted 2026-11-07), four model tiers on a resident local server (llama-server, Ollama as the fallback), run by the models on this machine. Python standard library only; no cloud
model, no OpenAI-style endpoint. Code decides what passes (tests, AST checks, the verifier, the fake-code gate); a model's verdict is re-validated in code.

## Layout (the real tree)
- `architect.py` — the CLI (`run`, `ready`, `tune`, `optimize`, `ci`, `livetest`, `hosts`, `status`, `doctor`, `selftest`, `self-improve`, ...).
- `architect_hub.py` + `hub_ui.html` — the local web hub (one HTML file, no build step). `model_hub.py` is the server base it extends.
- `architect_embed.py` — the Python API for dropping Architect into another app (`Architect(root).run(...)`).
- `arch_core.py` — the Engine: model calls (`call`, `ask_json`), prompt context (`context`, `rank_files`, `focus`), applying FILE/EDIT blocks (`apply`), run state, traces.
- `arch_roles.py` — the roles: prompt, group, needs, fallback chain, sampling. `arch_flows*.py` — the flows (build, bugfix, feature, cicd, purge, security, ...).
- `arch_checks.py`, `arch_fakecode.py`, `arch_deps.py`, `arch_ci.py` — code-side verification. `arch_schemas.py` — JSON schemas for structured replies.
- `arch_progress.py` (stall detection, escalation), `arch_resume.py`, `arch_ledger.py` (what happened in your runs; few-shot from your own successes).
- `arch_index.py` — hybrid retrieval: SQLite FTS5 + TF-IDF fused by reciprocal rank (`.arch/index.db`); dense ranking optional via `retrieval.embed_model` (see `arch_fts.py`); `index` command.
- `arch_pipeline.py` — the `ship` flow (build -> test -> hunt -> secure -> review, each an existing flow). `arch_dataset.py` — SFT/DPO export from marked traces. `arch_chat.py` — Studio (chat grounded by the index; talks only). `arch_autopilot.py` — the backlog, unattended draining within a budget, proposals from findings (code-checked).
- `arch_chain.py` — swap-aware chains: the per-run swap ledger ("Run economics" in the report), judge batching in the build flow, the team fit sheet (`fit`).
- `arch_hw.py`, `arch_autotune.py`, `arch_profiles.py`, `arch_modelopt.py`, `arch_power.py`, `arch_ollama.py`, `arch_hosts.py` — machine, tuning, server, laptops, other machines.
- `arch_ready.py` (Get ready), `arch_probe.py` (edit-format check), `arch_livetest.py` (real tasks judged by code), `arch_selfimprove.py` (safe self-edit with rollback).
- `arch_transport.py` — the one model-call function behind `core.stream_chat`: Ollama (`/api/chat`) or the resident llama-server (`/v1/chat/completions`, exact prefix-cache counts, JSON-schema output). `arch_server.py` — the llama-server manager: machine class (`profile_for`), `profiles/<class>.json` researched defaults, `plan()` (fit math + the binary's own flag caps), Ollama blob adoption (no second download), start/stop/status/watchdog; falls back to Ollama when the binary is absent. `arch_passport.py` — per-model facts (stdlib GGUF header reader) + measurements, `hub_data/passports/`. Tiers: `cfg.tiers` (router/worker/heavy/draft = any installed model), `arch_roles.TIER_OF`, `Engine.tier_model`.
- `arch_dataflow.py` + `arch_gates.py` + `flows/*.yaml` + `prompts/<flow>/*.md` — flows as DATA: a YAML document per flow (steps, each naming a model tier, a prompt file, its gates, retries and a token/time budget), interpreted over the engine's own primitives. `arch_gates.GATES` is the registry of code checks a step may name; a gate answers ok True/False/**None**, and None ("could not run") makes the run's verdict `not_sure`, never a pass. `flows.engine` (Settings > Flows) chooses: `legacy` (the Python classes, YAML fills only what they do not provide), `data` (YAML wins), `off`.
- `arch_cache.py` — a reply this machine already gave, kept so the same question never costs twice. Only at temperature 0 **or** with a seed set (then whole runs become repeatable); never during `eval`; a hit carries the verdict that reply reached last time, and `why_not()` always explains a miss in plain words.
- `arch_skills.py` + `skills/` — one short Markdown page about one way of working, put in front of the model only for the tasks it matches (`MIN_SCORE` keeps coincidences out). A skill that was loaded when a run passed earns a point, so the ones that help float up.
- `arch_commands.py` + `commands/` — saved requests invoked by name (`/review`, `/fix`, `/secure`). A command expands to **words** and then takes the normal path: same flows, same budgets, same checks. Nothing in a command file can run a program.
- `arch_hooks.py` — *your* commands at fixed points (`before_run`, `after_apply`, `after_check`, `after_verdict`, `after_run`). A blocking hook that fails is handed back to the model exactly like a failing test. A project's `.arch/hooks.json` arrives with its code, so it does **not** run until `hooks --trust`; editing it revokes that.
- `arch_port.py` — picks the four tiers from the models THIS machine actually has, ranking by capability **minus** the known accuracy cost of each quantisation (so a 9B at 2 bits loses to an 8B at 4 bits), and moves your half of an install between computers (`port --pack` / `--adopt`) while deliberately leaving every hardware measurement behind.
- `arch_receipt.py` — one run as a single self-contained HTML page: timeline, tools, checks, verdict, cost. No scripts, nothing from the internet.
- `arch_supervisor.py` — the work queue (`<data_dir>/jobs.db`): each task runs as its own process, `ALTERNATES` retries a failed or not-sure task with a different flow anchored to the ORIGINAL flow (never chained), `ENVIRONMENTAL` failures (server down, model missing) pause the queue instead of consuming approaches, daily run/minute ceilings and a `STOP` file.
- `arch_dist.py` — the offline installer: app + optional server + optional models + `SHA256SUMS` + install scripts that verify before running anything and then `port --apply` on the new machine. Nothing measured on the building machine goes in.
- `arch_gates_more.py` — scanners and `<kind>_scan`/`<kind>_fewer` gate pairs for the `harden`, `types`, `a11y`, `document` and `migrate` flows, plus `deps_check`. Scanners are AST/HTML-parser code, never a model.
- `arch_postmortem.py` — after a failed run the model explains it in plain words from a code-built digest of the trace (`postmortem.md` next to the run); the supervisor hands it to the retry through `ARCHITECT_RETRY_HINT`, which `Engine.context` shows as labelled advice. Never a verdict, never training data, never during `eval`.
- `arch_templates_more.py` — more project templates plus the file-based starters in `static/templates/src/<id>/` (46 in all); `arch_templates_catalog.catalog()` gives each one category, stack, difficulty, colour token, file tree, next steps and an SVG thumbnail (`static/templates/<id>.svg`); `suggest()` checks specific stacks before broad words.
- `evals/families/e2e.py` — whole-project build tasks judged by hidden acceptance tests (over HTTP, through the CLI, across a restart). Each carries a reference solution and a deliberately wrong one; both are proven against the judges.
- `arch_tools.py` — the tools a model may call (`ls`, `grep`, `search`, `read`, `outline`, `run_tests`, `memory_read`, `memory_append`, `apply`), each capped, path-checked and routed through the same review/undo as any edit; `schema()` constrains a tool-loop reply to one call or a final answer. `arch_memory.py` — Markdown memory (`.arch/memory/*.md` per project, `hub_data/memory/*.md` per machine): the model appends one dated line at a time, a lesson is `confirmed` only when a run that was shown it passed its gates, and a line that never helps is dropped.
- `arch_fts.py` + `arch_index.py` — retrieval: SQLite FTS5 over functions and memory notes (`.arch/index.db`), fused with TF-IDF. Dense ranking is optional and OFF by default: `retrieval.embed_model` (e.g. bge-m3) turns on `arch_embed.py` (Ollama `/api/embed`, loopback `retrieval.embed_host` only, `keep_alive 0`, vectors cached in `.arch/embed.db`), fused into the ranking; it only orders what the prompt shows. `architect.py bench-retrieval` = `tools/retrieval_bench.py` (ground truth `evals/retrieval/questions.json`, not eval tasks). `arch_modelmath.py` — the advisor behind `explain` (plain words, numbers, formula; every figure labelled measured or estimate).
- `arch_eval.py` + `evals/` — the fixed task set per flow (`eval`, `eval --fake` oracle, `eval --compare` regression gate). `arch_selftest_eval.py`, `arch_selftest_offline.py` — the eval harness and the offline seal.
  A live task's folder is thrown away, so `keep_trace` copies its run record to `<data>/evals/runs/train|bench/.arch/runs/`: `train` =
  tagged "training" and not "holdout", the ONLY eval bucket `arch_dataset.roots` reads; `bench` = everything else, read by
  `arch_costmodel` (the report's C = t / p̄, `architect.py cost`, hub Models > Cost map = `costmapPage`, read-only, points per task
  FAMILY not flow) and never trained on. The oracle keeps nothing; 1000 newest per bucket.
- `arch_selftest*.py`, `arch_hub_check.py` — the suites; `python architect.py selftest --suite full` runs them all in an isolated home.
- `arch_cloud.py` — Claude as an optional tier (`claude:` models): the one gate `allowed()`, per-run/per-day spend caps checked before each request, a 30-day reply cache, web search only on a person's chat message, fast mode (Opus only, falls back to normal speed). Replies are transport `claude`, never training data.
- `arch_claudecode.py` — the real Claude Code agent via the Agent SDK in the project folder, billed to the API key (own `CLAUDE_CONFIG_DIR` = `<data>/claude_code`, so a subscription login can never be used); modes plan/acceptEdits/default, no bypass; every run kept in `<data>/cc_runs/`.
- `arch_agents.py` — the person's Claude Managed Agents (list, session, live events; tool approvals default to deny) + the night shift (`schedule_add/list/remove`, `crew_schedule.json`, `crew_log.jsonl`).
- `arch_fleet.py` — several Claude Code agents at once, one file owner each; `plan_check` refuses before spending; the FLEET runs the tests (only `ALLOWED_PREFIXES`, no shell punctuation); `ok None` = `not_sure`. `arch_agentlib.py` — briefs that worked as `agent_templates/*.md`, filled in and handed to the fleet.
- `arch_overseer.py` — one incremental reader over agent logs, transcripts, hub runs, fleets and the ledger; views of one session are folded into one agent, nothing is counted twice. `arch_learn.py` — the Logbook (`logbook.db`, FTS5): tasks/steps/files, `by_claude` flag, `exclude_from_training()`.
- `arch_costs.py` — what runs cost, read from traces (Claude dollars, tokens per model, `local_share`). `arch_tokens.py` — price before sending (`count()` is the free counter, refused when `arch_cloud.allowed()` says no; `estimate_local()` labelled estimate). `arch_batch.py` — overnight batches at half price, same gate.
- `arch_server.py` fast lane (`server.fast_lane`, `fast_think_budget`) — worker only: all layers + cache on the card (window shrinks to fit, not below `FAST_MIN_CTX`), n-gram speculation, above-normal priority on performance cores; with it off the plan is unchanged (`arch_selftest_fastlane`).
- `arch_plugins.py` — plugin folders (shipped, machine, project) bundling agents, workflows, skills, commands, hooks; `plugins.enabled`, `plugins.disabled`; a project's plugins load only with `plugins.trust_project`, and a `code:` agent only from a trusted plugin (`agent_allowed`). `architect.py plugins list|enable|trust|install|new|validate|pack`.
- `arch_squad.py` — local agent workflows (`architect.py crew`): agents are `agents/*.md` (`.yaml` still loads; see docs/HARNESS.md), workflows `workflows/*.yaml` (plus enabled plugins' `agent_dirs()`/`workflow_dirs()`), run under a token/time budget with one file owner each. `crew --resume RUN_ID` replays unchanged agents of an earlier run (`squad.replay`, `--no-replay`); a step cache reuses an agent with identical inputs and files for `squad.step_cache_days` (layer "Agent steps" in `arch_cachestats`). Never under `ARCHITECT_NO_CACHE`.
- `arch_telemetry.py` — local-only usage counters and events (`behavior.telemetry`, `telemetry_mb` cap); nothing leaves the machine.
- `arch_select.py` + `arch_jobs.py` — model choice PER JOB by measurement (`select --plan/--screen/--measure/--apply/--undo`; C = seconds per passed task); `arch_jobs.JOBS` maps roles to jobs. `arch_modelknow.py` — family facts per model (`explain --model TAG`); `behavior.model_defaults = auto` lets its sampling/thinking fill only what the person left unset (traced as `model_defaults`), off by default.
- `arch_orchestration.py` (`architect.py orchestration`) — who delegated what to whom: Claude Code sessions/subagents/workflows, crew runs, local runs. Tokens are priced per MESSAGE model at its exact `arch_cloud.PRICES` id (family as fallback), 5-minute cache writes 1.25x, 1-hour writes 2x, `<synthetic>` skipped; "read-only" = no edit-tool calls and no shell writes outside temp. Local: exact cache hit = llama `cache_n > 0`, llama prefix reuse reported apart from Ollama estimates, worker speed = median decode tok/s of tier=worker llama calls (with n).
- `arch_finops.py` + `arch_selftest_finops.py` — Cloud API vs. Local $0 FinOps & Optimization Scorecard (`/api/arch/finops`, `/api/arch/demo`, Hub **Models > Speed > FinOps & savings**): compares measured local tokens and projected team workloads against Claude Opus 4.6, Claude Sonnet 4.6, GPT-4o, Claude Haiku 4.5, and $20/$200 seat plans; surfaces the 10-way optimization matrix ("How we save & compete vs. the start"); exports an executive Markdown report (`markdown_report()`); and provides `create_visual_demo()` for one-click live visual frontend demos during stakeholder calls.
- `Start_Architect_GUI.bat` + `Start_Architect_GUI.vbs` + `arch_launch.py` — GUI-first windowless startup and one-click Desktop shortcut creation (`/api/arch/shortcut`) with `OLLAMA_FLASH_ATTENTION=1` and `OLLAMA_KV_CACHE_TYPE=q8_0` pre-configured.
- `static/hud/architect_harness_hud.html` — the Harness HUD, served at `/hud` and shown at Speed > Harness HUD (an iframe with `?live=1&root=<project>`): the run drawn as a circuit. It listens to the same `/api/arch/events/stream` as the Console; `ArchitectHUD.translate()` turns each real trace record (step, call, check, tool, verdict, escalation) into a node/gate update, so it never guesses a timeline. LIVE follows the current run, REPLAY plays the newest run of the open project from its trace, SIMULATE only draws the layout. The strip at the top polls `/api/arch/gpu`.
- `hub_data/` — measured data (profile, ledger, results, machine card). `.arch/` in a project — runs, backups, undo, handoff.
- `catalog.json` — models worth considering (never pulled without an explicit yes). `MANIFEST.sha256` — install integrity (`self-verify`).

## How to run
```bash
python architect.py ready            # first run: repair, model, tune, optimize, prove, edit-format check, smoke test
python architect.py run "goal"       # one task; the router model picks the flow
python architect.py livetest         # does it work with MY models? real tasks, judged by code
python architect.py fit              # weights + KV cache per team model against this GPU; what a swap costs
python architect.py index --query "add throttling"   # keyword vs vector vs fused ranking for one request
python architect.py start              # detect the machine, start the resident server when installed, open the hub
python architect.py server plan        # which model serves which tier, on which port, with what on the GPU (plain words + numbers)
python architect.py passport           # every installed model: known facts + what was measured here (works with Ollama stopped: it reads the manifests)
python architect.py explain --workload edits   # model options for this machine, plain words first
python architect.py server fetch --yes # the one-time download of llama-server (the only online step; asks first)
python architect.py eval --quick       # 10 fixed tasks per flow with your models: pass rate, tokens, latency; --fake = the oracle, no GPU
python architect.py eval --quick --label candidate --compare baseline   # the regression gate (exit 1 on a regression)
python architect.py port               # which models THIS machine should use for each tier, and why (--apply to save)
python architect.py port --pack me.zip # take your settings, notes, skills, commands and training data to another computer
python architect.py commands           # the saved requests: /review, /fix, /secure, /explain, /speedup, /testgen, /cleanup
python architect.py run "/fix paginate() returns page 0 rows for page 1"
python architect.py hooks --example    # your own commands at fixed points in a run
python architect.py receipt --open     # the last run as one HTML page you can keep
python architect.py dataset            # verified work -> SFT, preference pairs, distillation samples, tool trajectories
python architect.py promote-model --tier worker --model my-worker --compare baseline
python architect.py selftest --suite full
python architect_hub.py              # the hub (or START_HUB.bat)
```

## The one screen
The hub opens on **Console**: what you asked for on the left, what the machine is doing on the right (plan, tool calls, check results,
verdict), and a strip across the top with the resident models, speed, budget and a stop button. It is driven by structured events the run
prints as it goes (`\x1e{json}\n` on stdout when `ARCHITECT_EVENTS=1`, split out by `model_hub._split_events`, served at
`/api/arch/events?since=N` and `/api/arch/events/stream`) — not by scraping console text. `Ctrl+K` is the command palette, `/` opens the
saved commands, `Esc` stops between steps, `j`/`k` walk the plan.

## Conventions (how changes land here)
- A new flow is a `flows/<id>.yaml` plus its prompts under `prompts/<id>/`, and an eval family under `evals/families/`. No Python unless it needs a new gate, which goes in `arch_gates.py` with a selftest proving pass, fail and "could not run". A step id may not be a context key a gate writes (the validator refuses it); `python architect.py flows --validate` checks every file.
- Budgets are real: every step and every flow may set `budget: {calls, tokens, seconds}`, enforced in `Engine.call` before the request goes out, and `.arch/STOP` (or `hub_data/STOP`) stops a run between calls.
- Settings are schema-driven: a new setting goes in `arch_params.py` (name, type, range, plain description) and in `DEFAULTS` in `arch_core.py`; the hub renders it from the schema.
- A new role: `arch_roles.ROLES` entry (title, group, icon, needs, sampling), `FALLBACK`, `_prior` tables, and the hub-check role count. A new flow: a class with `id`, `title`, `produces`, `run(e, params)`, registered in `arch_flows.FLOWS`, with KEYWORDS/PRIORITY entries and a routing test.
- Every write the harness makes to the person's settings must have an undo (`tune --undo`, `optimize --undo`, profile undo records).
- Never remove, hide or reassign the person's installed models; downloads only after an explicit confirmation.
- Nothing remote on the default path: model calls go to the local server (or machines the person added in `hosts`); `behavior.network` defaults to `offline` for code the harness runs; anything the Architect itself would fetch is behind `online.enabled = false` and the app must behave identically with it off. Never add a paid or online dependency on the default path. `arch_selftest_offline` fails on the first non-loopback connect.
- Every feature comes with checks in the matching `arch_selftest_*.py` and, for UI, a hub-check or a Playwright pass. A change under `evals/`, the flows or the prompts is measured with `eval --fake` (plumbing) and `eval --quick` (your models) against the latest `baseline`. Ship with `self-verify --write`, a clean `selftest --suite full`, and the README check count updated.
- Evidence tiers in advice: measured on this machine > vendor card > community. Say which.
- Prompts are prefix-stable: in every retry template the stable part (instructions, project context) comes first and the per-try part (`{feedback}`, failure text) last; `arch_selftest_chain` fails any template that breaks this. The few-shot example goes at the end of the user turn, never in the system prompt.
- The index only orders what the prompt shows; it never decides a pass. The embedding model is asked with `keep_alive 0` by default so it never sits next to the chat model on a small card.

## What makes a small model finish a build (measured live, 2026-11-03)
- **DEF blocks** (`DEF: path.py::name` / `::Class.method`, `core.replace_def`): the model sends one whole definition; every other line of the
  file is kept. Whole-file rewrites were silently dropping functions an earlier sprint built.
- **Interface first**: the planner lists every public name with its arguments (`PLAN.interface`); `interface_lines` puts the request's own
  contract first and only adds planner names the request does not fix; every sprint prompt shows it. The request still decides the gate.
- **One sprint for one module** (`behavior.merge_small_plans`): every hand-off between sprints is a chance to lose finished work.
- **The best try is kept** (`BestState`, `behavior.keep_best_try`): a try that clearly made things worse (broke the code, lost a function,
  fewer tests pass AND more fail) is undone before the next try. Ties go to the newest; it never turns a fail into a pass. Tests that do
  not load yet are only "worse" than tests that ran (judged by the runner's status, so unittest and custom commands count): against a
  version with no tests they are kept (a build used to end with none). A file that no longer parses is always worse.
- A test the harness itself rejected (an unusable bug reproduction) is removed, not left to fail the real fix.
- In `bugfix` the existing tests are read-only: the reproduction must be a new file, and no fix try may edit the tests that judge it,
  the files that decide which tests run (pytest.ini, tox.ini, setup.cfg, pyproject.toml), or add a root conftest.py. Not yet mirrored in
  `flows/bugfix.yaml` (the opt-in data engine).
- **Prompts are fitted at call time** (`Engine._call`): the window is the smaller of the settings and the llama-server's own
  (`_served_ctx`); the whole prompt plus `REPLY_ROOM_TOKENS` (2048) must fit with `EST_MARGIN`; else the format example is dropped,
  then the longest project context built this run (`Engine._contexts`) is cut from its end at a `=== ` section (`arch_tune.cut_context`).
- A FILE block for an existing .py file that the shrink check would refuse, holding only definitions (+ imports) with at least one that
  exists, is applied through `replace_def` (`core.merge_partial`). Anything else is still refused.
- A request that asks for tests (`wants_tests`) gets `test_file_for(sprints)` in the first sprint; a sprint fails while the project has
  no test file (`has_tests`).
- **Code fixes the wiring, the model writes the logic** (`arch_flows.wire_contract`, every build sprint before the checks; 2026-11-05).
  30 live whole-project builds failed about half on the contract's SHAPE: `build_report` written in salesreport/report.py and never
  exported, a CLI in inventory/cli.py with no `__main__.py`, a package folder with no `__init__.py`. Only unambiguous fixes (one
  defining module, one argument-free `main()`), through `put_file` (asks the person unless review is auto), undone when the import still
  fails (the undo writes the bytes back directly: put_file's shrink guard refuses an empty `__init__.py`). A try it helped is traced
  `assist` and `arch_dataset` skips it as a training sample. Replay: the same miswired reply fails without it and passes with it.
- Escalation is offered only when `arch_progress.ready_now` says the larger model's server answers and its measured speed gives a
  ~1500-token answer within `limits.escalate_max_s` (240 s).
- Function-edit merges are pure replacements: a block with a new or repeated name, or a placeholder body over a real one, is refused.
  The served window is re-read after `_prepare_server`/a restart starts the servers; the router's window never sizes project context.
  The YAML loop's own `escalate: [heavy]` asks `ready_now` too. "The test file" means a `test_*.py`/`*_test.py` module.
- **One heavy job at a time** (`arch_heavy.py`): `selftest`, `eval`, `livetest`, `tune`, `self-improve` take an OS file lock
  (`hub_data/heavy.lock`, msvcrt/fcntl, released by the OS on a crash) at below-normal priority; a second one waits with a message.
  Children inherit `ARCHITECT_HEAVY_HELD` and skip the lock, so never build a child env from scratch for an architect.py subprocess.
  `behavior.heavy_jobs = any` turns it off.
- `arch_server.ensure(cfg, wanted=...)` starts a resident tier only when one of the run's role models is its model (the scripted
  benchmark used to start the real servers and leave 3.7 GB of VRAM held). `start` and the crash restart still start everything planned.
- Mixture-of-experts tiers that do not fit get every layer on the GPU and `--n-cpu-moe N` (`_moe_split`, `MOE_SHARED` 0.12 is an
  estimate); any partial offload gets `ram_note` when its RAM part exceeds RAM - `RAM_RESERVE_GB`.
- Draft models: `pick_draft` requires the worker's exact tokenizer (GGUF tokenizer model, pre-tokenizer, vocab size), at most a third of
  its size, dense, and room on the card. `server.draft`: auto = the tier you set, plus a plan note naming a candidate; try = use the
  candidate (what `tune`'s bench_variants measure); off = never.
- Live GPU view: `arch_hw.live()` (nvidia-smi utilisation + memory, RAM), hub `GET /api/arch/gpu` (plan cached 60 s), polled every 5 s
  by the console strip. `eval --set representative` = `arch_eval.representative()` (17 tasks) + `task_changes` against the previous run.
- `ensure(wanted=)` matches a tier the way `serving()` does (tier, `tier:x`, name, GGUF path or file name, the configured model). A
  draft you set is dropped, with a note, when its tokenizer differs from the worker's; an auto-picked one also pays for its own f16
  window cache. "All layers on the GPU" launches with `-ngl layers+1` so the output layer is on the card too (the record keeps `ngl`).
- Hub: `GET /api/arch/evals` (saved reports, the newest live run task by task) feeds the Health > Benchmarks page (SVG chart to scale,
  theme tokens); `GET /api/arch/gpu` reads "up" on every request and caches only the plan.
- **Speculative decoding must be asked for** on current llama.cpp (`--spec-type`, default none). `_parse_help` reads the allowed
  kinds and drops flags the help marks "has been removed" (`--draft-max` made the server refuse to start). Worker: `ngram-cache` with
  the lookup cache (measured here: edits 56 -> 306-343 tok/s), or `draft-simple` with a draft. qwen3:0.6b as a draft measured 66-78
  tok/s on new code and 125 on edits, so n-gram stays the default and the draft stays opt-in (`server.draft = try`).
- Hub navigation: see `TABS` in hub_ui.html (the tab set changed several times; do not trust older lists). Page ids are unchanged, so `go("run")`
  etc. still work. Home = hero + `getStarted()` (4 steps, one action each) + starters + `teamSummary()`; the old 29-role grid lives in
  Models. Next design steps agreed with the user: the Plan card before a run, a Review screen after it, Settings by presets, plain words.
- **Role resolution** (`Engine.model_for`): a model assigned to the role wins; else the role's OWN tier; only with no tiers set up is a
  related role's model borrowed (`FALLBACK`). The old order let an empty router borrow a 14B planner's model.
- **Machine audit fixes (2026-11-03)**: threads = performance cores (`arch_hw.perf_cores`: Windows EfficiencyClass, Linux
  `/sys/devices/cpu_core/cpus` + affinity, macOS perflevel0), router on the CPU gets half; `--cache-ram` capped by RAM
  (`server.cache_ram`, 0 = auto: 1/2/4 GB); the plan warns when the resident tiers need more RAM than is free; `start()` unloads
  Ollama's copy of what llama-server is about to serve (and other models unless `exclusive_gpu = off`); POSIX servers get their own
  session; `wait_health` stops when the server process died; `architect.py start` launches `ollama serve` detached when nothing
  answers locally (`arch_ollama.ensure_running`, loopback only, never a second one on a busy port). Full audit:
  docs/machine_audit_20261103.md.
- **Make room** (`server.make_room`, auto): before a local call to a model no llama-server serves (Ollama loads it into what is
  left of the card), `arch_server.step_aside_for` decides whether our tiers on the card (ngl > 0; never the CPU router) should stop:
  only when the model's size + 0.6 GB exceeds the free graphics memory, WOULD fit once they stop (free + their size x1.15 + 0.3),
  and no slot is busy (`busy()` reads `/slots`). Measured 2026-11-03: the dense 14B gets no faster with the card to itself (2.45
  tok/s beside the worker, 1.9 alone via Ollama at 39% on the card, 2.3-2.7 on our heavy server at 21/40 layers): the half in RAM
  sets the speed on single-channel DDR4, so it is left where it is. A busy check that cannot be read counts as busy; a model
  Ollama already holds counts its own share as free. The engine (`_make_room`) does it only in a run that owns its ending
  (`persist_runs`; Studio/proposal engines never move a server) and never while any call is in flight (`_inflight`); it reloads
  an Ollama copy split card/RAM. The step is written to the servers' state (`aside`, owner pid + run id: `step_aside`);
  `_bring_back` -> `come_back` restarts the tiers (after Ollama has let go of the big model) when a call needs them, in `finish()`,
  in run_flow's BaseException path, after the postmortem, and after each eval/livetest task. A record whose owner process died
  (Stop button, Ctrl+C, timeout) is brought back by the next `ensure()` or the hub's watchdog (`cmd_start` passes `watch=True`);
  `server stop` (no tiers) forgets the records; `stop()` drops a record before killing, so the watchdog never respawns it.
  `start()` runs one process at a time (`_starting`, an OS file lock in hub_data/server/start.lock), so a run's ensure never kills
  a worker the hub is still loading. Restart cost 5-8 s. Test children keep `PYTHONDONTWRITEBYTECODE`:
  a shared bytecode cache was measured slower (1.69 s first / 0.71 s next vs 0.41 s), so it was not adopted.
- **Night of 2026-11-04** (full log: docs/MASTER_PLAN_20261104.md, progress log). Shipped, each tested:
  - **GPU dropout:** `arch_server._gpu_lost` reads the server log after start and crash-restart, retries on the card, and marks
    `on_cpu`. On no-MUX laptops the NVIDIA card can be asleep, and llama-server then silently ran on the CPU.
  - **MoE heavy beside the worker:** `server.heavy_beside`, measured at 14-16 tok/s with switching in about 1 s. The desktop's
    graphics memory is remembered as `desk_gb`.
  - **Drives:** `arch_hw.drives`, `drive_roles` and `measure_drive`, with fake-capacity flags.
  - **Measured bandwidth:** `bandwidth_from`, saved in hub_data/bandwidth.json (GPU 158, RAM 19.5 GB/s here).
  - **Settings:** `behavior.thinking` and `behavior.sampling` (A/B options), `behavior.tests_on_ecores`
    (`arch_hw.ecore_mask`), `behavior.sandbox_mem_mb` (arch_sandbox.py Job Objects), and `storage.archive`
    (arch_archive.py write-behind).
  - **Endpoints:** `/api/arch/drives`, `/api/arch/flowmap`, `/api/arch/machine`.
  - **UI:** the "How this works" step timeline, the machine map on Models, and a polish layer at the end of `<style>` (OS fonts
    Segoe UI Variable and Cascadia Code).
  - **Harness:** the plan check `arch_schemas.validate_plan` and live todo (`Engine.todo_set` / `todo_mark` + a `todo` event);
    the tools `view` / `edit_lines` (refused when the edit breaks syntax); step checkpoints with resume in YAML flows;
    syntax-first and cached tests in `verify`; failure-log trimming; the HANDOVER note on escalation.
- **Launcher**: START_HUB.bat -> start.py H -> `architect.py start` (Ollama if needed, the model servers, the hub). start.py runs it
  with a Python that has pytest (`python_for_tests`): on this laptop `py -3` is 3.15 without pytest, 3.12 has it. Test with 3.12.
- **Crash recorder**: while the hub runs (`behavior.record_live`, on), `arch_hw.start_recorder` writes a 5-second sample (GPU use,
  graphics memory, temperature, power draw, RAM, AC/battery, running servers) to `hub_data/live.log`, fsync'd, rolling at ~2 MB.
  **Measured desktop VRAM**: `plan()` subtracts what the card really holds (nvidia-smi total - free) instead of 0.7 GB when none of
  our servers is up (`desktop_gb` in `_ngl`/`_moe_split`/`pick_draft`). `after_upgrade.py` / AFTER_UPGRADE.bat: what the app sees,
  its plan (current and with the MoE as heavy), measured speeds; changes no setting.
- **Docker / WSL awareness**: `arch_hw.vm_memory()` reads the vmmem/vmmemWSL VM's RAM (tasklist) and `wsl_limit_gb()` its
  `.wslconfig` memory limit (default half the RAM); detect() puts it in `hw["vm"]`, live() in `vm_gb`/`vm_limit_gb`. `plan()` keeps
  the VM's room to grow free and says so; the status strip shows "Docker/WSL x GB" while it runs. Never change the person's WSL/Docker
  settings to make room: plan around them.
- Known, not fixed yet (review 2026-11-03): the escalation fallback sends the worker to the local server even when its role normally
  runs on another machine (`role_hosts`); `arch_server.plan` sizes the KV cache as quantised even when the build lacks the cache flags.
- An escalated role whose larger model gives no answer goes back to its own model for the rest of the step (`Engine.call`), instead
  of ending the run on a machine problem. llama-server's `-c` is the TOTAL KV memory shared by its slots, and a build left to choose
  opens 4: `arch_server.plan` always passes `--parallel N` and `-c ctx*N`. A "Context size has been exceeded" error mid-reply comes back
  from `LlamaTransport` as a reply cut off at its limit (`done_reason: length`), like Ollama, so the finished blocks still apply.
  Live benchmarks run one task at a time: four concurrent runs on one 8192 window broke each other (and a laptop running the full
  suite, a second suite, the oracle and a GPU benchmark at once crashed).
- A reply with no header but a bare path line above a code block is read as `FILE:` (fallback only, `core._bare_path_blocks`).
- The test flow keeps the sound tests of a half-right file (`TestFlow._salvage`: adds a forgotten import, drops only the failing test
  functions, then the full suite and the fake-code check must pass) instead of deleting the file.
- The refactor flow refuses a try whose code is unchanged under the formatting (`code_shape`), and, when the request is about
  duplication, one that leaves functions with the same body (`duplicate_bodies`). Green tests prove nothing changed; not that it was done.

- **Night of 2026-11-05** (each with its own suite, registered in `selftest --suite all`):
  - `arch_cleanup` + `architect.py cleanup [--yes]`; the hub runs it on exit (`behavior.close_on_exit`). A stray llama-server counts
    as ours only when its command line points into THIS data folder: a test hub with another ARCHITECT_DATA_DIR must never stop the
    real servers. Never touches a run whose process holds the job lock.
  - `server.ollama_gpu = auto`: `Engine._keep_off_card` sends `num_gpu 0` for a model no llama-server serves while our worker holds
    layers on the card (Ollama had put the heavy MoE 28% on the card; the worker got 9 of 36 layers, 7.96 tok/s vs 55 for qwen3:8b).
  - `arch_cachestats` (Models > Caches), `arch_cachehygiene` (`caches --prune --yes`, `storage.slot_cache_gb`), slot files named per
    project path. `arch_replybudget` (`behavior.reply_budget`): learned caps 2 x p95, off in benchmarks and after any recent cut-off.
  - `arch_triage`: failure families from the checks' words (no model). A "tests" failure means every OTHER check passed.
  - Test flow: passing tests under another file name are moved to `tests/test_<name>_extra.py` (traced `assist`). Refactor flow:
    "X and Y should become one" fails a try that keeps both names (`merge_names`, `still_both`).
  - Hub windows: `.card.modal` must stay `position: fixed` (a later `.card` rule had made every modal open below the page).
  - Inter is bundled (`static/fonts`, OFL) and served at `/fonts/inter.woff2`; CSP stays `font-src 'self'`.

## A role you assign wins over a tier
A YAML step's `tier:` is the default for that work. A model the person assigned to the role by name wins over it (`_pin_tier`), except
`heavy`, which is an explicit escalation. Before this was fixed, every data-flow step silently overrode the person's role assignments.

## One settings file
Inside the Architect, `model_hub.read_config/write_config` must target `arch_core.CONFIG_FILE`. `model_hub` now follows it automatically when
`arch_core` is loaded; code that writes settings should still set `mh.CONFIG_FILE = core.CONFIG_FILE` at the point of use. Writing to
`hub_config.json` instead made `port --apply` and `promote-model` change a file nothing reads.

## Settings safety: tests isolate ARCHITECT_HOME
A test or tool must never read-modify-write the real `architect_config.json`. Every suite sets a throwaway `ARCHITECT_HOME` **and** `ARCHITECT_DATA_DIR` before importing `arch_core`; anything that calls `write_config`, `port --apply`, `tune`, `select --apply` or `promote-model` in a test does so inside that folder. Back up before any manual config edit.

## Transfer pack
`py -3.12 tools/transfer_pack.py` (docs/TRANSFER.md) exports playbooks, learned workflows, Claude briefs as local agents, the harness contract and cache/model settings into `hub_data/transfer/<stamp>/`. Claude-written, tagged `source: claude-orchestration`, never training data.

## Three outcomes, never two
A check result is `True` (ran and passed), `False` (ran and failed) or `None` (**could not run**). `Engine.mark` keeps all three on disk.
`None` is not a failure: it is excluded from training in both directions, and it makes the run's verdict `not_sure`. Anything that
collapses it to `False` — `bool(gate_ok)` is the easy mistake — teaches the model that unverifiable work is bad work.

## The self-tests own their data folder
Every suite runs against a throwaway `ARCHITECT_DATA_DIR` (`architect.py::_selftest_suites`, and `arch_selftest.run()` for a standalone
run). The suites drive real `Engine` calls with the scripted stand-in `fake:7b` and mark the replies accepted, which is exactly how
`hub_data/ledger.json` is written — without isolation the install learned that a test fixture was "a past reply of yours that passed the
checks" and fed it into real prompts, and the suites stopped being repeatable. A suite that scripts a model must also set
`ARCHITECT_NO_CACHE=1`: a scripted fake answers the same prompt differently on successive calls, which a real temperature-0 model never
does, so `arch_cache` correctly collapses the two and the script falls out of step.

## Rules learned building the Claude tools (2026-11-04; method: hub_data/learning/METHOD.md)
- **Self-tests never call Claude**, whatever the settings say: every network path asks `arch_cloud.allowed()` (false under eval and the self-tests, `_testing()`); tests pass a fake `client=`. A count of tokens is still a request off the machine, so it is gated too.
- **Claude prompts get the full window.** Local prompts are fitted to a small served window; a Claude call is not, so do not cut its context to a local size. Keep the shared prefix first so prompt caching pays (a 6-call Council answer fell from $0.27 to $0.18).
- **Checks ignore pre-existing findings.** A gate compares against what the file had before the run; otherwise a correct one-line change fails on problems it did not make.
- **Agents own files.** Every brief names its files; no two running agents share one (or a folder and a file in it). A shared file (hub_ui.html) is queued, not parallel. Every brief has a hard `max_budget_usd`; jobs came in at 10-25% of their caps.
- **The orchestrator verifies.** Agents in acceptEdits mode cannot run commands, so the lead (or `arch_fleet`) runs the suites and reports three outcomes; only what passes is committed.
- Claude Code's own `claude -p` prefers a subscription login over the key: keep the empty `claude_code` settings folder. Claude-written text may feed retrieval, playbooks and statistics, never fine-tuning: `arch_dataset.build()` skips every run `arch_learn.exclude_from_training()` names and any run with a `claude` transport or `claude:` model call, and cuts the ledger's FORMAT REFERENCE block unless it quotes the same local model; `arch_ledger` never stores or shows `claude:`/`fake:` or benchmark examples (and none under `ARCHITECT_EVAL`); `Engine.call` asks `example_for(..., model=model)` (only that model's own past replies), skips it under `ARCHITECT_EVAL`, and traces `fewshot_model`. `arch_dataset` drops `fake:` replies as fixtures and `assist`-traced tries; only the eval `train` bucket is read. (`arch_selftest_chain` therefore scripts `chainfix:7b`, in its throwaway data dir.)
- A new setting goes in `arch_params.SETTINGS` and `arch_core.DEFAULTS` (a check compares them).

## Gates are the product
Unlike a generic harness, Architect's gates (tests, verifier, fake-code gate, shrink guard, question detector, missing-import check) are the point: a small
local model is only trustworthy because code checks its work. Do not remove them to make a run pass; make the run pass the checks.

## Self-improvement (`self-improve`, `arch_selfimprove.py`)
Architect edits its own code through the same flows, in a throwaway copy, with the full suite as the gate and `self-rollback` as the undo. See `docs/self-improvement.md`.

## Local model guidance
`docs/local-ai-optimization.md`: what the industry notes recommend, and where each item already lives in Architect (or why it was left out).

## v12 Breakthroughs: 10-Way Optimization, Distributed LAN Mesh, Universal Hardware & FinOps (2026-11-07)
All items previously listed as open in `docs/ANTIGRAVITY_HANDOFF.md` §5 and the distributed/hardware audits are shipped and enforced:
1. **Exact Query & Router Memoization (`arch_core.py`, `arch_cache.py`):** Temperature-0 and seeded replies plus router classifications are cached in SQLite (`responses.db`) and never re-run twice.
2. **Prompt Prefix Ordering & KV Slot Persistence (`arch_chain.py`, `arch_server.py`):** Static instructions, repo maps, and AST outlines precede variable feedback (`cache_prompt=True`, disk `.slot` save/restore).
3. **Read-Only Tool Cache & O(1) Tree Fingerprints (`arch_tools.py`, `arch_core.py`):** `ToolEnv._fingerprint()` memoizes `tree_fp` across read-only tool calls and invalidates on write; `Engine._tok_section_cache` avoids re-tokenizing unchanged files.
4. **q8_0 KV Cache Quantization & Flash Attention (`arch_ollama.py`, `arch_server.py`, `arch_launch.py`):** Enabled by default on both `llama-server` (`-ctk q8_0 -ctv q8_0 -fa on`) and `ollama serve` (`OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`), halving KV VRAM and doubling usable context.
5. **100% GPU Residency & Split-Penalty Role Scoring (`arch_roles.py`, `arch_port.py`, `arch_server.py`):** `score_for_role()` penalizes models that spill >30% to system RAM (`gpu_ratio < 0.7`); `arch_server.plan()` subtracts live loaded Ollama VRAM from `desktop_gb` so VRAM isn't double-subtracted; unified memory (Apple Silicon / AMD APU) caps `heavy` at 55% of RAM and disables CPU MoE expert splitting.
6. **Adaptive Role Context Trimming (`arch_server.py`, `arch_core.py`):** `arch_server.plan()` honors `model_overrides[name].num_ctx` and `limits.num_ctx`; router runs at 2k ctx, worker at 6k–8k, and remote context-overflow errors auto-resize and retry.
7. **Subagent Shared-Prefix AST Outlines & `deps_hash` Step Cache (`arch_squad.py`, `arch_flows.py`):** `Crew.shared_header()` injects compact AST symbol outlines for owned files into the shared cacheable header; `step_key()` hashes both owned files and `deps_hash = files_fingerprint(root, exclude=owns)` so changes to shared helpers invalidate downstream writer steps; `arch_flows._tree_hash` hashes all non-ignored files (including `.csv`, `.json`, `.ini`, `.yaml`).
8. **Hybrid CPU P-Core Binding, Universal Windows GPU Detection & 30s Power Cache (`arch_hw.py`, `arch_power.py`, `arch_supervisor.py`):** `_win_gpu()` detects AMD Radeon and Intel Arc GPUs via `Win32_VideoController` when `nvidia-smi` is absent; `arch_power.on_battery()` caches battery checks for 30s (`_BAT_CACHE`) and skips PowerShell in selftests; `arch_supervisor.py` maintains a 2s daemon heartbeat thread (`recover(stale_after=15.0)`) so multi-supervisor runs never double-steal active jobs.
9. **Queue-Aware Distributed LAN Mesh & Instant Local Failover (`arch_hosts.py`, `arch_transport.py`, `arch_core.py`, `arch_squad.py`):** `Crew._loop()` honors remote `role_hosts` with per-host semaphores (`max_parallel`, default 2); `rank_hosts()` load-balances by effective speed `tok_s / (1 + in_flight)`; `is_llama_host()` and `/v1/models` discovery allow remote `llama-server` nodes alongside remote Ollama nodes; `mark_down()` and `Engine._call` fail over seamlessly to local execution mid-run if a LAN host drops.
10. **FinOps & Optimization Scorecard + Live Visual Frontend Demo (`arch_finops.py`, `architect_hub.py`, `hub_ui.html`):** `/api/arch/finops` and Hub **Models > Speed > FinOps & savings** calculate measured and projected team savings vs. Claude Opus 4.6, Claude Sonnet 4.6, GPT-4o, Claude Haiku 4.5, and $20/$200 cloud seats, display the 10-way optimization table, export an executive Markdown report, and provide `/api/arch/demo` (`launchVisualDemo()`) to build, pytest-verify, and launch a live visual web application for stakeholder calls in one click.

## Architecture review 2026-11-07 (docs/ADR-001, suites arch_selftest_review1..4)
- **Process identity is `arch_proc`** (pid + creation time). Never `os.kill(pid, 0)` on Windows: it is TerminateProcess. Every
  "is it alive / is it still ours" question (servers, step-aside owners, supervisor task owners, the Launch runner) asks arch_proc.
- **`server.json` has one writer per tier** (`arch_server._save_tier`); plan() and start() share `ollama_keep()`; plan() runs again
  under the start lock; the KV cache is planned as f16 unless the cache-type flags and flash attention are really in the args.
- **Rollback snapshots** (`arch_flows_pro.capture/restore`) record every file name and never delete a file they did not see.
- **Three outcomes reach the run**: `finish(ok, error, unsure)`, `verify()["tests_ran"]`, `require_tests` on a step's verify, a
  not_sure step makes the data-engine verdict not_sure even when pass_if holds, sub-flows and foreach carry it up.
- Gates write their records under their own keys (`scan_rec`, `existing_files`, `kept_tests`, ...); RESERVED lists every one.
- A YAML `tier:` pin is not an escalation (`Engine.tier_pins`): the role's LAN route still holds. The reply-cache key carries the
  machine that answered. A fleet agent's Engine refuses files outside `ARCHITECT_OWNS`.
- Hub: job epochs start from the clock (never reused after a restart), a finished job's pump never writes into the next job,
  `CONFIG_ROUTES` hold `CONFIG_LOCK` and read strictly (an unreadable settings file is never saved over), a route that answered
  itself never gets a second response, the event stream carries `id:` and honours `Last-Event-ID`, restart relaunches
  architect_hub.py on its own port however the hub was started.
- Tests: a suite started on its own gets a throwaway home (`arch_core._standalone_selftest_home`); the network guard lives in
  `<data dir>/_guard`; `arch_selfimprove.count_passed` reads every summary format (a suite's summary must carry its check count).

## Your versions: agents, crew workflows, flows, prompts, launch profiles are editable (arch_custom, 2026-11-07)
- The shipped `agents/`, `workflows/`, `flows/`, `prompts/` are the BUILT-IN versions and are never written from the hub. A person's
  change is saved in `<data dir>/custom/<kind>/` and wins everywhere a name is looked up (`arch_squad._find_named`/`load_agent`/
  `list_agents`/`list_workflows`, `arch_dataflow.flow_file`/`prompt_path`/`load_all`). Every save is checked by the validator the
  run uses, kept as a version (last 50, `.history/`), restorable; `reset` removes the person's file (versions stay).
- A flow with a custom YAML or custom prompt runs from its YAML whatever `flows.engine` says (`register` keeps the Python class in
  `_PYTHON` and puts it back on reset). `arch_iterate.adopt`/`trial` write into the custom layer, never the installed `flows/`.
- Hub: `/api/arch/custom` (GET read/version, POST check|save|restore|reset), `/api/arch/launch_profile`, `/api/arch/iterate`
  (+`/trials`); UI `cuEdit()` (one editor for every kind), `editAgent` with instructions and task, New agent/workflow, flow detail
  "Edit this workflow" / "Edit these instructions" / "Improve from its runs" (`itPanel`), Start my project "How it starts".
- Per project: `.arch/launch.json` (cmd, port, env, network) with `.arch/launch_history/`; `arch_runner.default_cmd`/`start` use it.
- Suite: `arch_selftest_custom` (also proves no shipped file is written); hub check covers the HTTP side.

## Cross-Platform Compliance: Antigravity <-> Claude Code Anti-Stub & Non-Destruction Protocol
When switching between **Google Antigravity**, **Claude Code**, and **Local-Architect**, every agent MUST obey these non-negotiable rules so no platform ever corrupts or stubs out work created on another:
1. **Zero Stubs / Zero Placeholders (`arch_fakecode.py` Gate):** Never replace working functions, classes, or UI sections with `pass`, `...`, `TODO`, `FIXME`, `NotImplementedError`, hardcoded dummy returns, or `// rest of code here`. `python architect.py fakecode` and `arch_fakecode.scan()` must report **0 gate-level findings**.
2. **Surgical Edits Only (Shrink Guard & `DEF:` Blocks):** Never truncate or rewrite whole modules when changing a function. Existing functions, exports, and comments must be preserved. In `hub_ui.html`, two byte-slices are explicitly guarded by `arch_selftest_cachespage.py` (`/* ===== Caches (Models):` to `/* ---- Benchmarks:`) and `arch_selftest_costmodel.py` (`/* ===== Cost map (Models):` to `function benchPage(){`) — never edit inside those slices, never use `font-size:...px` in `hub_ui.html` (use `rem` or `var(--t-*)`), and keep every `SIDEBAR` place at **<= 5 sub-tabs** (`arch_selftest_hubcalm.py`).
3. **Strict Config & Data Isolation:** Never read-modify-write the workspace's `architect_config.json`, `hub_data/`, `.arch/`, `bin/`, `portfolio.json`, or `.env` during tests or verification. Always set throwaway `ARCHITECT_HOME` and `ARCHITECT_DATA_DIR` in temp directories, and delete `architect/hub_data` if `tools/ci_parallel.py` writes a CI summary file there.
4. **Verify Lessons & Suites Before Handing Off:** Every architectural rule in `data/lessons.json` is verified by `py -3.12 arch_lessons.py` (`0 problems`) and mirrored in `docs/PRINCIPLES.md`. Run `arch_lessons.py`, `arch_selftest_finops.py`, `arch_selftest_hubui.py`, `arch_selftest_hubcalm.py`, `arch_selftest_a11y.py`, and `arch_hub_check.py` before concluding any cross-platform session.

## End-to-end check (2026-11-08, real models on this laptop)
One bug-fix task was run four ways: the CLI, the hub's Home "Do it" (twice), and once with a customized prompt (your version of
`prompts/bugfix/review.md`, which switches the flow to its YAML). All four routed to `bugfix`, wrote the fix, passed 3/3 tests and
ended `pass`; Undo from Home restored the file and removed the generated test. Fixed on the way: Home never pulled the run's events
(`homeEvents`: "Working on it" stayed empty unless the Console was open); legacy flows sent the step dict's repr as the plan title;
a pure text YAML step (`report_text: true`) now puts its advice in the report like the legacy reviewer did; `arch_hub_check` compared
the STOP file against its own environment, which `_standalone_selftest_home` changes after the hub child was spawned (use the child's env).
