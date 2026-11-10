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

**Corrections to the list above (audit 2026-11-09, each checked in the code):**
- 1: replies are in `responses.db`; router decisions are in `route_cache.json`, not SQLite.
- 3: there is no `ToolEnv` class; `_fingerprint` is a module function in `arch_tools.py`, the cache covers 4 tools (`CACHED`: ls, grep,
  search, outline), and `tree_fp` is dropped by `invalidate`, so an edit made outside the tools and `Engine.apply` is not seen.
- 4: Ollama gets `OLLAMA_KV_CACHE_TYPE` / `OLLAMA_FLASH_ATTENTION` only when Architect starts it; llama-server gets `-fa` only with
  layers on the card and falls back to an f16 cache when the build lacks the flags.
- 5: unified memory caps heavy at 70% of RAM (`arch_port.py`), not 55%; the split penalty is 6 points (`gpu_ratio < 0.7`) vs 2.
- 6: the router runs at 4096 on every gpu profile (2048 only on cpu8/cpu16); a context overflow comes back as a cut-off reply
  (`done_reason: length`), it is not resized and retried.
- 7: `shared_header` outlines the target files (`vars.target_files`, at most 6), not the owned files.
- 8: the 30 s cache is in `arch_power.battery()`; there is no `on_battery()`.
- 10: the FinOps report read no usage at all (it called `arch_usage.summary`, which does not exist), priced Opus 4.6 at $15/$75 and
  Haiku 4.5 at $0.80/$4, showed 9 layers as "active" by construction, and filled gaps with invented numbers (64% prefix reuse, 85% / 100%
  local share). Fixed on 2026-11-09: see "Honesty audit" below.

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

## Honesty audit (2026-11-09: no stubs, every button works, no misrepresentation)
- Stubs: `architect.py fakecode` finds 0 gate-level findings; the 19 non-error findings are test fakes, the stub detector's own
  patterns and the redaction list. The page has no empty handlers or "coming soon" actions; every request path it uses has a route.
- Buttons: a crawler (throwaway install, one real run with the scripted model, every POST answered in the page and never sent, so
  nothing outside the install changes) went through 45 pages and 733 buttons: 658 pressed, 18 disabled, 57 rendered too late after a
  rebuild to press (timing, not a fault). No uncaught page error, no server error. 20 pressed buttons changed nothing visible: 12
  Components samples (now they answer), the already-selected tab or preset (Build group, Balanced), Refresh with unchanged data,
  "Remove" on the only fleet row (it resets it), and four real gaps, fixed: "Run checks" and "Propose next steps" were answered from
  the 60 s read cache (now they check again), and an empty queue "Add" or plugin "Install" only moved focus (now it says what to type).
  Six toasts that read like errors ("Saved undefined", "Cannot read ... hunks") came from the crawler's stub answers; each real
  endpoint was checked and returns the field. "What will it do?" now reads the route without assuming every field is there.
- Recent runs on Home was read only when Home was built or the folder changed, so a run finished on Home appeared there only when
  that first read happened to be slow (the old speed tile's slow request hid it; the uismoke check caught it once that was fast). It
  is now read again once per finished run.
- FinOps: real measured usage (30-day window), Claude list prices with source and date, no invented fallbacks, layer statuses read
  from the settings (on / off / built in / not set up), gains labelled with their basis, the projection labelled as one; the demo
  fills its figures in from this install and reports failed tests and a server that did not start.
- Hub: Stop says it ends the run now; Free memory unloads Architect's models (it targeted the other apps' ones); an unfinished step is
  not "done"; the Models page speed inputs no longer override the measured bandwidth; Home's speed tile reads the usage meter; Tiers says
  estimate; Review, Doctor and Assess keep "could not check"; By job confirms after the job; dead `sheet()` (a false "twenty-three
  specialists") removed. Pinned by `arch_selftest_honesty.py` and the rewritten `arch_selftest_finops.py`.

## Six places that broke the platform's own rules (review 2026-11-09)
- Training: a best-of-N candidate that PASSED but lost to a smaller passing change was marked False, so the DPO export would have
  taught "a correct answer is bad". It is None now (not chosen); only a candidate whose checks failed is False.
- Nothing is "passed" without a code check: YAML text steps, review map/reduce chunks, applies with no gate and tool-loop answers were
  marked True, which fed each model's ok rate, its few-shot "past reply that passed the checks" and "known good" tool trajectories.
  They are None; a tool loop's answer is marked by its step's gate when it has one (explore has none, so it gives no trajectories).
- A template whose dependency is missing (flask_app, fastapi) ends not_sure in both engines (the gate returned a pass, the legacy flow
  said nothing).
- Crews are judged by code: a failing `run_suites` phase or a panel's majority "fail" fails the crew; files changed with no passing test
  is not_sure; a suite file that is missing is "could not run", not a failure (`_code_verdict`, `crew.judged`).
- The Console box has "Do it" (a run; Ctrl+Enter) next to "Ask" (the chat, which only talks). Words sent to a running run now steer
  it: `Engine._call` appends the run's inbox to the end of every later prompt (the cached prefix is unchanged) and traces a `guidance`
  event the live board shows; `/api/arch/redirect` no longer writes STOP unless asked.
- "Run this plan" is offered only when the run's flow is a YAML `DataFlow` (only it reads `plan_override.json`); otherwise the card
  says the workflow runs as built-in code and how to switch. Pinned by `arch_selftest_rules.py`.

## Tech-debt Phase 1 (second audit, 2026-11-09)
- Stop: a run is asked to end before its next model call (the hub writes the project's `.arch/STOP`, which the engine already
  honoured); after `Job.STOP_GRACE_S` (20 s) its process is ended; the stop file the hub wrote is removed when the process is gone
  (nothing removed it before, so a STOP file was a permanent kill switch). Jobs that are not runs, and the hub's own shutdown, end at
  once. A stop file the person put there is never taken over or removed. `arch_selftest_debt`.
- One Claude price list: `arch_cloud.PRICES` (now with the 4.x models and `PRICES_AS_OF`); FinOps builds its Claude rows from it
  (its own copy had drifted to old prices). Orchestration already read it, and now prices 4.x sessions at their exact model.
- Tool cache: the tree fingerprint is reused for `TREE_FP_TTL` (1 s) and dropped after `run_tests`; it was kept until Architect wrote
  a file, so ls/grep/search answered from the old tree after a hook, a test run or an edit in the person's editor.
- The escalation fallback keeps the role on its own machine (`role_hosts`): both the route and the retry call were hard-wired to this
  computer. Open since 2026-11-03.
- `arch_selftest_crawl` (a real browser, a throwaway install, every POST answered inside the page): every page opens with no uncaught
  error and every button on the key pages is pressed; `ARCHITECT_CRAWL_ALL=1` presses all ~730. Its first run caught a race in
  `ccLoad` (it wrote to a chip that a page change had removed: an uncaught TypeError), fixed.
- A refused POST (no token, wrong host or origin, too large) was answered with its body unread, so Windows sometimes reset the
  connection (WinError 10053) and the page got "connection aborted" instead of the reason; it made `arch_selftest_workspace` fail
  intermittently in CI (1 in 30 refused requests measured on the old handler, 0 in 30 now). `Handler._refuse` reads up to 64 KB first.
- Three regexes held a real backspace where a backslash-b (a regex word boundary) was meant (written through a shell heredoc): the design suite's "no type alias left"
  check could never fail, `tools/ci_parallel.py` never named a crashed suite's error, and the crawl's client-gone filter matched
  nothing. Fixed; `arch_selftest_debt` now fails on any control character in the source. The crawl ignores only a browser hanging
  up mid-request (ConnectionAborted/Reset, BrokenPipe in Python's HTTP server); any other traceback fails it.

## Accessibility Phase A (2026-11-09; WCAG 2.1 AA, measured in headless Edge on every page, light and dark)
- **Fields have their own edge** (`--field-line`, 3:1 or more in every theme; 1.4.11): `--line` measured 1.33:1, so a text box had no
  visible edge. Hairlines between cards keep `--line`.
- **Toasts are heard, also inside an open window** (4.1.3): a native `<dialog>` makes the rest of the page inert, so `#srLive` and the
  toast outside it were neither seen nor heard. `srRegion()` puts the live region (polite, or assertive for errors via `srAlert`) inside
  the open window and the toast moves in with it; the visual toast has no role (unhiding and filling at once is often not read, and the
  old a11yWire wrapper put `role` back on every call). Home's goal and folder errors are spoken and set `aria-invalid`.
- **Focus is never thrown onto `<body>`** (2.4.3): `keepFocus` also handles a rebuild that awaits a fetch (it waits for the promise and
  restores by index only when focus fell off the page), and wraps the background jobs, the queue board and the status chip. A page
  change from anywhere but the nav, or closing the palette when its opener is gone, focuses the page heading (`tabIndex -1`). The
  browser tab names the page (2.4.2), without the About button's "?".
- **Live regions speak only on change**: Home's progress card, the run bar and the queue board were live regions rebuilt every step or
  every 2-6 s, so a screen reader re-read them constantly. They are plain regions; one sentence is spoken when the current step or the
  outcome changes (`simpleDraw._said`, `runBar` `_said` keyed by `EV.epoch`).
- **Clickable chips are buttons** (`#sJob`, `#sOllama` with `aria-expanded`, the Try: examples); the y/n/a/q answers are disabled while
  no run asks (they were faded to 45% but still pressable) and named.
- **Contrast at the rule that caused it** (1.4.3): faded filter counts, the terminal's diff colours (the terminal is dark in every theme:
  `--term-add/--term-del/--term-hunk`, 6.8:1 or more), `kbd`, plan steps not reached yet, and the queue's empty line (it borrowed the
  "cancelled" look). One skip link (a11yWire looked for `.skip`, the page's is `.skip-link`, so there were two).
- Checked by `arch_selftest_a11y` (static) plus a headless-Edge behaviour pass (toast inside a window, focus after `go()` and after an
  async rebuild, title, one skip link, no new page errors). Phases B and C wait for the owner; the Cost map's SVG point roles are inside a
  guarded slice and need the owner's yes.

## Accessibility Phase B (2026-11-10; measured in headless Edge on all 45 pages)
- **Single-key shortcuts can be turned off** (2.1.4): `singleKeys()` (`keys.single`, per browser) gates ?, /, j/k, the g-chords,
  Review's a/u/r/j/k and typing straight into Home's box; the switch is in the shortcut list and in Settings. Ctrl+K, Ctrl+Enter,
  Ctrl+. and Esc stay. Space on Home scrolls the page again (it was typed into the goal box).
- **One main heading on every page**: Home and Watch a run hid the whole title row with display:none, so they had no h1; the row is
  now visually hidden (the h1 stays in the outline) and only its visible parts are left out. Markdown shown in a page starts at h3
  (`mdView`), so a document's own "# title" is never a second h1. Focus after a page change goes to a heading you can SEE
  (`focusPageHeading`: the title, or the first visible heading on those two pages).
- **Settings fields** (3.3.1, 3.3.2): a field's name is its label only (`aria-labelledby`); the hint and the recommendation are its
  description. A refused save puts the server's reason under the field it names (`fieldErr`, aria-invalid), keeps "Not saved: ..."
  above (a fading toast was the only trace) and focuses the field.
- **Visible labels** for the 23 fields that had only a placeholder or an aria-label: the field says `data-vlabel`, and `a11yPass`
  wraps it in place with a real `<label for>` (rows and grids keep their shape; the aria-label is dropped so the spoken name is the
  seen one, 2.5.3).
- **Reflow** (1.4.4, 1.4.10): sub-tabs wrap on wide screens instead of running off the page at 200% text (20 pages), the top bar
  wraps at 320 px (the ? button pushed 39 pages sideways) and the search box starts at 180 px so it does not wrap at 1280 px, card
  grids use `minmax(min(340px,100%),1fr)` (Jobs at 320 px). Measured after: 0 pages scroll sideways at 320 px or at 200% text.
- The GPU chip in the top bar was still a live region rebuilt every 10 s (a Phase A miss); it is not any more.
- Checkboxes were reported as failing 3:1 in Phase A's audit: that was the audit reading a CSS border, which native checkboxes do not
  have. Measured from the pixels: #767676 on white (4.5:1) and #858585 on the dark card (4.65:1). Nothing to fix.
- Checked by `arch_selftest_a11y` (static) and a headless-Edge behaviour pass (shortcuts on/off, Space on Home, one h1, visible
  heading focus, Settings name and refused save, visible labels).

## Accessibility Phase C (2026-11-10)
- **Review's verdict is a heading again**: `role=status` on the `<h2>` replaced its heading role, so heading navigation skipped the
  most important line on the page. The verdict is spoken once per run instead (`rvDraw._said`).
- **No 2-second limits** (2.2.1): "press Esc again to stop" waits 5 s and its hint stays up the whole time (the Stop button has no
  limit at all); every toast stays at least 4 s (some were 1.4 s) and, as before, while it is pointed at or focused.
- **Sidebar section names are read** (1.3.1): Work, Models, System were `aria-hidden`; each section is now a group labelled by its
  name (`display:contents`, so the sidebar's layout and every `#nav .nav` rule are unchanged).
- **Reduce motion in the app** (2.3.3): Settings has a switch (`motion` = system | reduce, per browser). "reduce" sets
  `data-motion=reduce`, whose rules mirror the operating-system block, and `reduceMotion()` also stops the JS counters (`artStill`).
- **A refused Settings save survives a redraw**: the page redraws when the recommendations arrive, which wiped the error the moment
  after it appeared. The message and the field errors live in `SET_ERR` and are drawn from it; focus returns to the named field when
  a redraw dropped it. Found by the browser check, not by reading.
- Watch a run's "What I'm doing" is an h2 (`.doing-h`, same look): with the page's h1 kept since Phase B, its h3 skipped a level
  (found by the full audit after Phase B).
- Not done, waiting for the owner's yes because it is inside the guarded Cost map slice: the chart's `<svg role="img">` makes its
  keyboard-operable points (`g.cm-pt`, role=button) invisible to screen readers (children of an image are presentational); the fix is
  `role="group"` on that svg. The same slice scrolls the detail into view with `behavior:"smooth"`, which ignores Reduce motion.
