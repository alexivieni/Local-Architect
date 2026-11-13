# What makes a small model finish a build

Measured live on this laptop; read before touching flows, the engine, servers or tiers. Moved out of CLAUDE.md on 2026-11-08, word for word except the corrections noted in the commit.

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
  the files that decide which tests run (pytest.ini, tox.ini, setup.cfg, pyproject.toml), or add a root conftest.py. Mirrored in
  `flows/bugfix.yaml` since 2026-11-07 (the `guard` step, gate `protect_tests`).
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
  etc. still work. Home = hero + `getStarted()` (4 steps, one action each) + starters; the old 29-role grid lives in
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
- Fixed (both found in the 2026-11-03 review): after a failed escalation the role goes back to its own model on its OWN machine
  (`role_hosts`; `Engine.call` used to reset to `host=None`, fixed 2026-11-09, `arch_selftest_win`), and `arch_server.plan` plans the
  KV cache as f16 unless the cache-type flags and flash attention are really in the args (2026-11-07).
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
