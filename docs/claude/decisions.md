# Dated decisions (still in force)

What each review or audit changed, newest first in practice; every rule here is enforced by a suite. Moved out of CLAUDE.md on 2026-11-08, word for word except the corrections noted in the commit.

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

## End-to-end check (2026-11-08, real models on this laptop)
One bug-fix task was run four ways: the CLI, the hub's Home "Do it" (twice), and once with a customized prompt (your version of
`prompts/bugfix/review.md`, which switches the flow to its YAML). All four routed to `bugfix`, wrote the fix, passed 3/3 tests and
ended `pass`; Undo from Home restored the file and removed the generated test. Fixed on the way: Home never pulled the run's events
(`homeEvents`: "Working on it" stayed empty unless the Console was open); legacy flows sent the step dict's repr as the plan title;
a pure text YAML step (`report_text: true`) now puts its advice in the report like the legacy reviewer did; `arch_hub_check` compared
the STOP file against its own environment, which `_standalone_selftest_home` changes after the hub child was spawned (use the child's env).

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

## Tech-debt Phase 1 (2026-11-08)
- CLAUDE.md became an index (rules first, inside the 3000 characters a run reads) with these pages; `tools/doc_counts.py` computes the
  counts the docs state and `arch_selftest_docs` fails when one drifts. Release notes keep the numbers of their day.
- `tools/ci_parallel.py` reruns a failed suite on its own: passes alone = LOAD (listed apart, not red), fails alone = FAIL.
  NOT_SURE suites are listed with their reason (templates_catalog: flask is not installed, and nothing is installed without a yes).
- Windows passes "ignore Ctrl+C" to children. `arch_server._spawn` clears it only around a server's start (`_CtrlCForChild`,
  read back from the process's own ConsoleFlags) so the server's clean exit works and this process keeps ignoring Ctrl+C as before:
  clearing it for good let a Ctrl+C meant for another suite's stand-in end the graceful suite.
- `arch_claudecode.write_meta` uses `core._replace` (patient with a reader holding the file) and reports a failed write instead of
  dropping it (proven by a check that fails on the old code). `doctor` says which Python runs the tests. (The localagent failure
  blamed on this here was in fact the fleet race fixed in Phase 2.)

## Tech-debt Phase 2 (2026-11-08)
- `arch_browser` drives the Edge or Chrome already on the machine, headless on 127.0.0.1, over the DevTools protocol with a stdlib
  WebSocket (nothing to install). `arch_selftest_uismoke` clicks through Home with a scripted model: Do it, "Working on it", "It
  worked", Recent runs, Undo, no page error. With the old Home code it fails 3 of 9; with no browser it is NOT_SURE, never a pass.
- Do-nothing broad handlers (`except Exception: pass`) can only go down: `tools/swallow_audit.py` (AST), baseline
  `data/swallow_baseline.json`, ratchet `arch_selftest_swallow`. A comment with "intentional" exempts one. The engine, the servers
  and the hub have none left; they report to `_tel_swallow` (rate-limited). 110 remain in 57 other files, to fix as they are touched.
- Engine extensions: `arch_core.add_extension(obj)`; the engine calls `extra_system`, `context`, `decide`, `check_path`,
  `check_files` (each None = not mine). `localintel_arch` is now one (`EXT`, `install`/`uninstall`) and replaces nothing; its
  behaviour is pinned by `arch_selftest_localintel` (written and passed BEFORE the change, passed unchanged after).
- The fleet race behind arch_selftest_localagent's occasional failure: a local agent's run log says "done" before its thread
  records the result, the watcher took the log's word, finished early and wrote back an agent list without the agent's checks.
  `arch_fleet._settle` takes a local agent's status only from its thread; the watcher settles under the lock (`testing`).
- A check that could not run is not a pass (decided by the person, 2026-11-08). `arch_checks.cannot_run(name, why)` gives
  ok None; `R()` still makes a missing value a failure (many checks pass None for "not there"). `summarize` lists them as `unsure`,
  `verify` returns them and keeps `e.unsure_checks`, `run_flow` turns a pass with any of them into not_sure (and says which), and
  the `verify_files` gate answers None. First user: the optional browser check without playwright, which had counted as passed.
  `behavior.playwright = off` stops asking for it. `eval --fake` stayed 101/101 (no eval task relies on it).
- Caches page (2026-11-08): the KV prefix layer reads `arch_cachestats.trace_roots()` = the cost model's folders plus the projects
  remembered by `arch_usage` (it read only portfolio + this folder + eval runs, so it said "no data" while runs had logged their reuse);
  `cache_n` wins over the estimate; `ledger()` returns `data_dir` and leads with a red finding when the hub reads a temp data folder
  (the 'hub-test' preview in .claude/launch.json); empty route/reply layers explain themselves (reply: `arch_cache.why_not` with this
  install's temperature and seed). `Engine.finish` remembers a kept run's project unless it is throwaway (`keeps_prompt_memory`), so
  CLI, crew and queue runs count too. All in the backend: the page's guarded slice is untouched (it still says "next live eval").

## Design system Phase 1 (2026-11-08; audit: 13 agents, findings verified, score 40/100)
- Home polled run events every 1.5 s forever after a run: its condition used RUNBAR.goal, a label nothing clears. Now `EV.running||!homeEvents._idle`
  (hub check + a browser check that counts event requests after a run). The test-results loop counts failures (refresh() returns true/false),
  skips hidden tabs and redraws only on change (it forced a full redraw every 3 s for up to 4 hours).
- Type tokens: one definition, in rem, in the light block (they were px, written twice). Cards and windows use --shadow/--modal (their
  shadows were tinted with the page colour: invisible in light/paper). Inputs have aria-invalid, disabled and hover states. Animation
  names are unique (pulseRing, shimmerSlide; duplicate viewIn/livePulse gone). The literal copy of the sub-tab pill block, the overridden
  .tfilter rules, the dead .st-menu block and 9 overridden colour literals are gone; 7 rendering literals point at their tokens.
- arch_selftest_design guards it: colour ceiling 88 -> 4, type tokens rem and defined once, no duplicate @keyframes, no --bg-tinted shadow.
  Left for Phase 2: portrait SVG colours and the HUD #07090e (need a token decision), the protected Caches/Cost map slices (owner's yes).

## Design system Phase 2 (2026-11-08)
- Buttons: ONE block (`BUTTONS: the one block`, calm layer) replaces 25 rules in six layers. Proven by a browser snapshot of every
  button's computed style on every page (light/dark, 1280/390 px, plain and hovered): the only changes are the intended ones. Small
  primaries are 30 px again (every `.btn.sm.p` was 44), `.pri` = `.p` (44 px, hover lift), `.btn.ghost` exists (used, never defined),
  a pressed toggle keeps the row's height, a disabled primary no longer lifts on hover.
- Accents never move a status colour: `--info` came out of all 12 accent rules (copper/moss info notes read as warning/success).
- Theme parity: every theme sets its own `--lift` (Paper borrowed the light navy; the dark ones are flat like their `--shadow`).
  Radius is defined once at the 14/10 everyone saw (the sheet said 12/8 and a later block overrode it).
- One focus ring without `!important` (it overrode every component offset, so menu rows' inward ring was clipped). 51 px sizes hidden in
  `font:` shorthands are rem (the font-size check could not see them; the hub check now does).
- Home's progress box and the run bar redraw only on change: they rebuilt every 1.5 s, restarting the shimmer, dropping focus from
  Keep/Undo, wiping "See the details" and re-reading the live region to screen readers (uismoke check, fails on the old page).
- Empty states: one block for `.empty/.st-empty/.bp-empty/.idle`. Clickable table rows use `clickRow()` (Tab, Enter/Space). Portrait
  drawings use `currentColor` + blueprint tokens; the HUD frame uses `--hud-bg`.
- The hubcalm suite anchored its "calm layer" on the header comment, so every "in calm" check searched the whole sheet; fixed (two
  checks then told the truth: the radius pair and the Claude token location).
- Left: the Cost map's clickable row and anything else inside the two guarded slices (owner's yes); Phase 3 (one dialog, SSE, split
  /api/state with an ETag, the a11y observer scope, spacing classes, the type sweep, merging the remaining layers).

## Design system Phase 3 (2026-11-09)
- `/api/state` in two halves: `?part=catalog` (flows, presets, templates, schemas: read once, ETag, a repeat is 304) and `?part=live`
  (28 KB instead of ~140 KB every 9 s); no part = all, for the checks. With Ollama stopped, Windows took ~2 s to refuse each loopback
  connect, so every state build waited 2 s: `model_hub._loopback_down` (0.3 s probe, loopback only) -> 0.39 s. Idle traffic about
  900 KB/min -> about 60 KB/min.
- Live run events are pushed (SSE, `/api/arch/events/stream`, one stream for Home, the Console and the run bar), opened when a run starts
  or `jobBadge` sees one, closed when it ends (the server now sends a frame when the run stops). Polls stay as the fallback. The Console
  no longer polls events while idle (24 requests/min for nothing).
- The a11y observer passes over added elements only (it re-scanned the whole view, forcing a layout per div, 120 ms after any change).
  An audit of every page at 1280 and 390 px finds nothing left unnamed or unreachable; uismoke runs it on Home.
- One dialog: `modal()` (native `<dialog>`, sizes form/wide/drawer). The report, timeline, score detail, text window, What's inside, the
  page explainers and the shortcuts moved onto it; `.card.modal`, `.fi-overlay`, `.hdrawer` are gone.
- Type sweep: 15 `--t-*` steps; ~280 literal sizes and 9 alias names folded in (no size moved more than 1 px but two 24 -> 26 px).
  Spacing classes on the --s scale replaced ~160 inline margins (exact ones proven layout-neutral; 2/6/10/14/20 px snapped).
- Layers merged for `.card` (7 rules), `.thread` (3), `.msg` (2), `.nav` (3), each at its last layer's position; the snapshot caught two
  flips, fixed: empty-state cards keep their tint, and the recommendation card's accent edge (hidden since the transparent !important
  card border) now shows. A dead phone rule for `.card` padding went.
- Guards: design suite (one dialog, every size a token, no alias, spacing ratchet 39), hub check (halves = whole, 304), hubperf
  (Ollama-down under 1 s), uismoke (stream not polled, live half only, dialog behaviour, a11y audit).

## Inside the guarded slices, with the owner's yes (2026-11-09)
- The owner approved one edit inside the Caches and Cost map byte-slices (CLAUDE.md, Cross-Platform Compliance). Done, and nothing
  else in those slices moved: the Cost map's cheapest-per-kind row uses `clickRow` (Tab, Enter/Space, aria-current), the Caches
  page's two .95rem sizes are `--t-lead`, and 22 spacing-only inline styles became spacing classes. Everything outside the slices
  was checked byte-identical by the patch. The rule stands: the next edit inside them needs a new yes.
- The design suite's type check no longer exempts the slices; the a11y suite requires `clickRow` for every clickable row on the
  page; the spacing ratchet is 4. uismoke presses Enter on a `clickRow` row.
- `arch_browser`: Edge on this laptop started handing off to a new browser process and exiting 0 (2026-11-09), so every browser
  check failed to launch. `launch` now keeps waiting for the debugging port when the launcher exits 0, and `close` sends
  `Browser.close` so a handed-off browser shuts down too (it was left running).
