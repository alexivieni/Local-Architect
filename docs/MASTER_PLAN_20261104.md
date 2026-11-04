# Master plan: overnight build, 2026-11-04 (for the executing model)

Read this file first, then follow it. Supporting research (read only the section you need):
`proposal_20261104.md` (machine map, scheduler, quant, UX), `proposal_20261104_critique.md`, `research_roles_20261104.md`,
`research_levers_20261104.md`, `research_gaps_20261104.md`, `hardware_upgrade_20261104.md`, `night_20261104.md`.
Rollback: `..\rollback\ROLLBACK.md`.

## Ground rules (never break)
1. **Offline.** No downloads, no network calls (fonts, models, binaries, pip). No role or tier changes saved to
   `architect_config.json`. No system settings. Items that need any of these go in the "Waiting for yes" list in `night_20261104.md`.
2. **One GPU job at a time**, at below-normal priority (`cmd //c "start /BELOWNORMAL /WAIT /B python ..."`), and only on AC power.
   Check AC first with `(Get-CimInstance Win32_Battery).BatteryStatus` (2 = AC). Never start a GPU job while `model_shootout.py` is
   running; check with `Get-CimInstance Win32_Process -Filter "Name='python.exe'"` for `model_shootout`.
3. **Small steps.** After each step, run that module's suite. Test commands:
   - `python arch_selftest_llama.py`
   - `python arch_selftest_win.py`
   - `python arch_hub_check.py`
   - full regression: `python architect.py selftest --suite all`, where only the 3 known port-8000 SECURITY checks may fail
   - `python architect.py eval --fake`, which must stay 109/109
4. **Stop rule.** A step that fails its checks twice gets reverted from the rollback zip or a `.bak` copy, logged in `night_20261104.md`
   under "Not done (why)", and skipped. Never weaken or delete a test to make it pass.
5. **Plain words** in every user-facing string. Four places only (Projects, Workspace, Models, Settings). Every settings write has an
   undo. Never remove, hide or reassign the person's models.
6. **Patching.** Heredocs get mangled in this shell. Write Python patch scripts with the Write tool into the scratchpad and run them.
   Each patch asserts its anchor appears exactly once.
7. **Budget.** If tokens or time run low: stop starting new steps. Then:
   - finish or revert the current step;
   - run the suites;
   - `python architect.py self-verify --write`;
   - rebuild the zip (`package.py` in the old scratchpad);
   - write the morning summary;
   - make sure nothing is running (`python architect.py server stop`).

## Fixed just before hand-off (verified, 72/72 server checks)
On laptops without a MUX switch the NVIDIA card can be asleep or resetting when a server starts. llama-server then silently runs on
the CPU. The model comparison ran that way for hours: the card showed 0%. Now `_start_procs` and the watchdog read the server log
after start (`_gpu_lost`), restart the server once on the card, and mark `on_cpu` in the status if it still fails. The model
comparison was restarted after this fix; its earlier partial results were deleted.
- Add to S7 (machine map): show `on_cpu` plainly ("the worker is running on the CPU, the graphics card was not available").

## State at hand-off
- **Done and verified:**
  - The MoE heavy model loads beside the worker. `server.heavy_beside`: it plans the experts into RAM, and the server suite passes 71/71.
  - The desktop's graphics memory is remembered (`desk_gb` in the server state).
  - Make room (the worker steps aside) passed two reviews.
  - The after-upgrade check now compares before and after.
  - The full regression passes apart from the 3 known security failures. The fake eval was still running at hand-off; confirm it
    at the start.
- **Running:** `..\model_shootout.py`. Results land in `hub_data/shootout_20261104.json` and `docs/model_shootout_20261104.md`; the
  log is `hub_data/shootout_20261104.log`. It covers:
  - A. each model doing every role (17 tasks);
  - B. heavy-model candidates;
  - C. worker + heavy combinations, with calls counted per model.

  Do not interrupt it.
- **Measured facts:**
  - Worker 4B: 68 tok/s, 334 on edits.
  - 14B: 5.1 tok/s.
  - MoE IQ3: 15.4 alone, and 14.2-16.4 beside the worker. Switching between them takes about 1 s; a swap takes 15-20 s.
  - Card idle: 0.2 GB. RAM: 32 GB dual channel, about 20 GB free.
  - Drives: NVMe 1.6-2.0 GB/s with 16.5k 4 KB reads per second. SD card D: 18 MB/s with 0.8 ms random reads, on its own I/O path.
    Stick E: is likely fake capacity, and its writes stall for up to 0.6 s.

## Steps, in order

### S1. Confirm the baseline (no code change)
- Read the fake eval result in the old scratchpad's `oracle16.log`.
- If anything regressed versus 109/109, fix it before continuing.

### S2. Drive inventory and storage roles (low risk, CPU only)
- **Files:** `arch_hw.py`, `arch_params.py`, `arch_core.py` DEFAULTS, `architect_hub.py`, `arch_selftest_llama.py`.
- **Code:**
  - Add `arch_hw.drives()`, returning per volume: letter or mount, label, filesystem, total and free GB, bus (NVMe, SATA, USB),
    removable, and a `suspect` flag.
  - Set `suspect` when a USB drive has an exact round binary capacity (size % 2**20 == 0 and size/2**20 is a round number), or a
    VID/PID with the "ProductCode" placeholder.
  - Use PowerShell `Get-PhysicalDisk`, `Get-Volume` and `Get-Disk` on Windows; `/sys/block` and `lsblk -J` on Linux; `diskutil` on macOS.
- **Measurement:** reuse `scratchpad/drive_speed.py` logic as `arch_hw.measure_drive(path, mb=64)`. It writes, then reads unbuffered
  on Windows, deletes its file, and runs only on explicit call. It must never write to a `suspect` drive beyond 64 MB.
- **Roles:** add `storage.roles` defaults:
  - `models` = the Ollama models dir;
  - `scratch` = data dir;
  - `archive` = "".
  - `archive` may be pointed at a removable drive by the person (setting, with undo). Add the rule: never models on removable or
    under 100 MB/s.
- **Hub:** `GET /api/arch/drives` (read-only, no measurement).
- **Acceptance:**
  - New checks: drives() shape on fake PowerShell output; suspect flag on 1,048,576,000,000 bytes; measure_drive cleans up its
    file. The llama suite passes.
  - Hub check: the endpoint is present.

### S3. Measured bandwidth replaces constants (low risk)
- **Files:** `arch_modelmath.py` (constant 50/192 at line ~62), `model_hub.py:161`, `arch_tune` stats.
- **Code:** derive `ram_bw` and `gpu_bw` from the measured runs stored in passports and stats:
  - gpu_bw = tok/s × bytes on GPU per token;
  - ram_bw = tok/s × bytes in RAM per token, for a split model.
- Fall back to the constants, labelled "estimate".
- **Acceptance:** a unit test with fixture runs gives within 10% of hand-computed values. The advisor labels "measured" vs "estimate".

### S4. Role-specialized defaults and reasoning budgets (medium risk; see research_roles §3-4)
- **Files:** `arch_roles.py`, `arch_core.py` (`_call` think handling), `arch_params.py`.
- **Code:**
  - Add per-role `thinking: off|on` with `max_tokens` caps: planner 3000, debugger 1500, others off.
  - Enforce by num_predict on the reasoning part where the transport supports it; otherwise only on/off.
  - Apply model-card sampling for Qwen3: thinking temp 0.6 / top_p 0.95 / top_k 20; non-thinking temp 0.7 / top_p 0.8 / top_k 20.
    Set it only when the role's sampling is unset, and never greedy for a thinking model.
- **Acceptance:** the selftest proves the planner request carries think=on with its cap and an editor request carries think=off;
  the fake eval stays 109/109.
- **Do not** change which model a role uses.

### S5. Pipeline visibility (low-medium risk, UI)
- **Files:** `hub_ui.html` (Workspace place), `architect_hub.py`.
- **Endpoint:** `GET /api/arch/flowmap?flow=<id>` returns the YAML flow's steps, each with its kind, tier, the model that tier
  resolves to on this machine, gates, and budget. It reuses `arch_dataflow` validation; it is read-only.
- **UI:** a "How this works" fold on the Task page and the Plan card. It shows a vertical step list: step name in plain words, the
  model on hover, and check chips.
- **Acceptance:** the hub check has a flowmap shape test; browser check at 1440 and 375 px, light and dark; no console errors.

### S6. Visual system pass (medium risk, UI; one screen per commit)
Owner's direction (2026-11-04): the visuals need much more than tokens. Better workflows on screen, iterative formatting (each run
and step reads as a living timeline that fills in as it goes), more life (subtle motion, live status, progress, hover detail), and
beauty everywhere: every place, page, card, icon and empty state. Keep it calm and readable, and keep the four places.
- **Files:** `hub_ui.html` only.
- **Code:** one type scale (12/13/14/16/20/24/32), 4 px spacing rhythm, refined light and dark tokens, consistent card / button /
  input states, focus rings, empty and error states, `prefers-reduced-motion`. Font stack:
  `"Segoe UI Variable", "Segoe UI", system-ui, -apple-system, Roboto, sans-serif`, and mono
  `"Cascadia Code", "Cascadia Mono", ui-monospace, Consolas, monospace`. Do not add font files.
- **Order:** tokens → Home → Task page → Models → Settings → Workspace pages.
- **Acceptance per screen:**
  - `python arch_hub_check.py` passes, and `node --check` on the extracted script if available.
  - The browser pane is opened on the hub (start it with `python architect.py start`, then stop it after).
  - Screenshots at 1440 and 375 px, light and dark; no horizontal scroll; no console errors.
- Keep the four places. Plain words.

### S7. Machine map screen (medium risk)
- **Files:** `hub_ui.html` (Models place, top), `architect_hub.py`.
- **Endpoint:** `/api/arch/machine` combines `arch_hw.detect()`, `live()`, `drives()` and `server.plan()` processes.
- **UI:**
  - Device cards: GPU, CPU P/E, RAM, each drive with its role and speed.
  - Which model sits on which device (GPU GB, RAM GB).
  - One plain sentence per placement (the `why` text).
  - Live bars from the existing `/api/arch/gpu` poll.
  - Text alternatives for the bars, and colour-blind-safe colours.
- **Acceptance:** hub check; browser at both widths and both themes.

### S8. Overlap: tests on E-cores (low-medium risk)
- **Files:** the gate runner where tests are spawned (`arch_flows.run_cmd` / `arch_tools.run_tests`), `arch_hw.perf_cores()`.
- **Code:** on Windows, set the child's affinity to the E-cores (psutil not allowed: use ctypes
  `SetProcessAffinityMask` on the Popen handle) when `server` tiers run on the GPU and the CPU has an E-core group. Setting:
  `behavior.tests_on_ecores` auto/off.
- **Acceptance:** a selftest with a fake core map computes the right mask; the Windows suite passes; a live check runs one bugfix
  eval task with the setting on and off and records wall time (pass must be equal).

### S9. Templates (low risk, after S6)
- Read `arch_templates.py`. Ensure each template's own tests pass in a temp dir (CPU only).
- Add an `AGENTS.md`-style conventions file and a `check` command to each.
- Do not add new templates unless asked; improve the existing ones.
- **Acceptance:** the template tests pass; the fake eval stays green.

### Done so far (night of 2026-11-04)
- S1: baseline confirmed.
- S2: `arch_hw.drives()`, `drive_roles()` and `measure_drive()`, plus `/api/arch/drives`. 74 server checks and 90 hub checks pass.
- S3: `arch_hw.bandwidth_from()`, `save_bandwidth()` and `load_bandwidth()`. Measured here: card 158 GB/s, RAM 19.5 GB/s. The 14B
  prediction is now 4.0 against 5.1 measured (the old prediction was 9.4).
- S4: `behavior.thinking` (roles | plan_debug_only) and `behavior.sampling` (roles | model_card). Defaults are unchanged; 76 server
  checks pass. S10 A/B-tests them.
- S5: `arch_dataflow.flow_map()`, `/api/arch/flowmap`, and the "How this works" timeline. "What will it do?" now shows the steps with
  who does each and its check. 91 hub checks pass; seen in the browser with no console errors.
- S6 (first pass): a polish layer at the end of `<style>`:
  - OS fonts (Segoe UI Variable, Cascadia Code), replacing IBM Plex / Bricolage, which are not installed;
  - a type scale, soft surface gradients, card hover lift, a live pulse on running chips, focus rings, a skeleton shimmer class,
    and scrollbars;
  - reduced-motion respected;
  - fix: the Console starter cards overflowed at phone width.

  91 hub checks pass; checked at 375 px in light mode and at desktop width in dark. Further per-page work is still open.

### S10. When the shoot-out finishes (GPU)
- Read `docs/model_shootout_20261104.md`.
- Then run, from `research_levers` items 3, 4, 7, each as a paired A/B on the 17 representative tasks with the best worker from the
  shoot-out:
  1. model-card sampling vs current;
  2. KV q8_0 vs f16;
  3. thinking only on plan/debug steps.
- Record tasks passed first, then wall time, in `docs/model_shootout_20261104.md`.
- Recommend role assignments in `night_20261104.md` under "Waiting for yes". Do not apply them.

### S12. Approved by the owner (2026-11-04)
1. **SD card as the archive/mirror tier.**
   - Add `storage.archive` (a path; empty = off; setting with undo). Write-behind copies of finished run records (`run.json`,
     `trace.jsonl`, `report.md`) and eval results go to `<archive>/architect_archive/<project>/<run_id>/`, queued in RAM and written
     by a background thread after the run finishes, never on the critical path.
   - If the drive is missing, skip it, keep a pending list, and retry at the next run.
   - Never put models or the only copy of anything there.
   - Use FAT32-safe names and files under 4 GB.
   - Hub: show the archive drive's role and free space on the machine map.
   - Acceptance:
     - a selftest with a temp folder as the archive (copies land; a missing folder is skipped without error; the pending list is
       retried);
     - the live check: set `storage.archive` to D:\ for this owner only after the selftest passes.
2. **GRC ValiDrive for the E: stick.** It is downloaded from https://www.grc.com/validrive.htm into
   C:\Users\alexi\Downloads\ValiDrive. Do not run it: it needs a Windows admin prompt the owner approves. In the morning summary, tell
   the owner to double-click it, pick E:, and return the stick if it shows "no storage" anywhere.

S12.1 status: done. `arch_archive.py` does write-behind copies, with a pending list for unplugged drives; 77 server checks pass. The
owner's `storage.archive` is set to D:\ (config backup: `architect_config.json.bak-20261104-before-archive`).

### S13. If time remains: make Architect able to work on itself (owner's request; inspiration: Claude Code's own harness)
In this order, each step small and tested:
1. **A todo list per run** that the model keeps (like Claude Code's task list). The planner writes the items, each step marks one done,
   and the hub shows it live.
2. **Plan mode.** For a goal touching more than N files, write the plan, show the Plan card, and wait for Start.
3. **Skills.** A passed run can be saved as a reusable skill (YAML flow fragment plus description) in `skills/`, found by FTS5 search.
4. **Hooks.** Before and after an edit, run lint and the matching selftest automatically. A failure blocks the step.
5. **Compaction.** When a long run nears the context window, summarise the older steps into a short state note.
6. **Self-work.** The `self-improve` flow already has gates (compile, selftest, check-count floor, zip backup, rollback). Run it on one
   small, safe issue from this plan **on a copy of the app**, with the eval as gate. Report the result; never promote without the
   owner.

### S14. New specialist profiles and flows (owner's request, 2026-11-04; Claude leads the design)
Each one gets a role in `arch_roles.py` (plain description, tier, reasoning on or off), a YAML flow with code gates, prompts, 2+ eval
tasks, and a selftest. Build them in this order:
1. **AI/agent engineer** (role `ai_engineer`, flow `agent`). It builds LLM or agent features inside an app: prompt files, a tool or
   function schema, a small eval set, and retrieval via SQLite FTS5.
   - Gates: the schema is valid; the agent's own eval set runs offline against a fake model and passes; no network calls in tests.
2. **System optimizer** (role `system_engineer`, flow `sysopt`). It profiles a program or service on this machine: time, memory,
   CPU/GPU/IO through the existing bench gates. It finds the bottleneck, proposes changes, applies them with undo, and re-measures.
   - Gate: faster than the noise band with the tests still passing. It also covers Architect's own placement: drives,
     bandwidth, the machine map.
3. **Component engineer** (role `component_engineer`, flow `component`). It designs and builds a self-contained component (a library
   module, a service, a UI widget) with a clear interface, docs, tests and a usage example.
   - Gates: interface tests, doctests, type/lint, mutation score up.
4. **Architect developer** (role `architect_dev`, flow `selfdev`). It works on Architect itself. It reads CLAUDE.md and this plan,
   works on a copy, and is gated by the full selftest, the fake eval and the manifest. It never promotes without the owner. This is
   the base for S13.6 (self-work).
Order of value: 4, then 2, then 1, then 3. Each one ships only when its eval tasks pass on the fake model, and once live on the worker.

### Progress log (keep this current; newest last)
- **S7 done.** `/api/arch/machine` plus the machine map at the top of Models > Tiers: GPU, RAM, CPU and drive cards, models placed
  on them, `on_cpu` shown. The CPU name now comes from the registry ("13th Gen Intel Core i5-13420H"). Seen in the browser.
- **S8 done.** `arch_hw.ecore_mask()` (here 0xf00 = processors 8-11) and `pin_to_ecores()`. `run_bounded` pins test processes when
  `behavior.tests_on_ecores` = auto (the default). The Windows suite passes 80/80. The wall-time A/B is part of S10.
- **S12.1 done.** **S12.2:** ValiDrive is downloaded (signature valid); the owner runs it.
- **H1 done.**
  - `arch_schemas.validate_plan()` (tested).
  - `Engine.todo_set()` / `todo_mark()` write `state["todo"]` to `run.json` and emit a `todo` trace event.
  - YAML flows tick every step (doing, done, failed, skipped). Python flows set their list at the start and close it at the end.
  - The live panel shows the steps still to come, greyed out.
  - The Windows suite passes 81/81 and the quick fake eval 100/100.
  - Note for the owner: this config uses `flows.engine = legacy`. Switching to `data` gives live per-step ticking everywhere;
    the data engine passed 105/105 earlier. That is the owner's choice.
- **H2 done.** The `view` and `edit_lines` tools. `edit_lines` refuses an edit that would break Python or JSON parsing and returns
  the parser's message (`arch_tools.syntax_error`). The Windows suite passes 82/82 and the flows suite 36/36.
- **H3 done.** YAML flows checkpoint every passed step (`steps_done` + `step_outputs` in `run.json`). `arch_resume.resume_params`
  passes them on, and `DataFlow.run` skips those steps and reuses their saved results. It works through the existing
  `architect.py resume`. The flows suite passes 37/37.
  - Not done yet: a Resume button in the hub's run list (the CLI works) and a separate `journal.jsonl` (`run.json` checkpoints
    cover it).
- **H4/H5 covered** by the existing `self-improve` (copy + gates + confirm-to-promote + rollback) and the `optimize` flow (baseline,
  one change, re-measure, roll back unless faster).
- **H13 done.** In `arch_flows.verify`, a changed file that does not parse fails the step before any test runs. Test results are
  cached per (project file fingerprint, targets), so an unchanged tree is not re-tested. The Windows suite passes 83/83.
- **H14 done.** `arch_sandbox.py`:
  - Windows Job Object with a per-process memory cap and kill-on-close (the whole tree is stopped when it ends);
  - POSIX RLIMIT_AS;
  - a plain message when the cap is hit.

  `run_bounded` applies it from `behavior.sandbox_mem_mb` (default 4096). The Windows suite passes 84/84: a 900 MB grab is stopped
  at 300 MB, a 20 MB process is fine.
- **H15 covered.** State already lives in files: `run.json` (todo, checkpoints), the handoff file (`write_handoff` /
  `handoff_text`, read into every data flow's context) and `.arch/memory`. No separate state.md was added.
- **H16 done.** Retry feedback is replaced, not accumulated (existing). When the prompt still does not fit after cutting the format
  example and context, the middle of a huge failure log is cut, keeping its start and its last 2000 characters. The server suite
  passes 78/78.
- **H17 covered.** Schema calls are sent as `response_format: json_schema` (constrained; degrades to json_object), and the
  chain/prefix selftest enforces a byte-stable prompt prefix.
- **H18 done (part).** When the coach escalates a stalled step, the bigger model gets a HANDOVER note: who stalled, how, and "take a
  different route". The loop suite passes 55/55, learning 37/37 and hosts 32/32. Not done: an escalation ladder beyond one step (H8).
- **S9 done.** All 23 templates rendered into temp folders; their own tests run. 22 pass. `fastapi` skips because FastAPI is not
  installed (installing it needs the owner's yes). Script: scratchpad `s9_templates.py`; copy it into the repo if needed again.
- **S10 in progress.** First comparison result, as soon as it was measured: `huihui_ai/qwen3-abliterated:4b` doing every role
  passes 7/17 (41%), median 49 s per task, 24 min in total. It fails mostly multi-file builds, e2e and refactor tasks, which is
  where the combination runs (part C) test a planner.
- **Known flaky check:** "serve status does a real TCP connect" (port 8000/8001) fails sometimes; it is the same environmental
  family as the 3 security checks. Not a regression.

### S15. The harness, approved by the owner (2026-11-04). Lead design: Plan -> Act -> Verify -> Learn
Rules for every item: keep it small; add a selftest (pass, fail and edge case); run the module's suite plus the fake eval (must stay
green). Defaults must not change behaviour on existing flows unless the item says so. Log each item in the progress log.

**Tonight, in this order:**

**H1. Structured plan + live todo list** (`arch_schemas.py`, `arch_core.py`, `hub_ui.html`)
- Add a PLAN_V2 schema: `{goal, steps:[{id, title, files:[...], interface:"signatures", checks:[...]}], risks:[...]}`.
- Add `validate_plan(plan, root)` in code: files are inside the project, every step has a check, at most 12 steps. A plan that fails
  goes back to the planner once with the reason.
- Engine: `self.todo = [{id, title, status: pending|doing|done|failed}]`, written to `run.json` (`state["todo"]`) whenever it changes.
  Flows that already plan (build, feature) fill it from the plan; other flows fill it from their YAML step titles.
- Hub: on the Workspace console, a "Todo" card that polls `run.json` through the existing run endpoint and ticks items live.
- Acceptance:
  - selftest: validate_plan accepts a good plan and rejects outside paths and missing checks;
  - the fake eval stays green;
  - the browser shows ticking items during a fake run.

**H2. Safe editing tools** (`arch_tools.py`)
- `view(path, line, n=100)` shows numbered lines; `search(q, max=20)` caps results.
- `edit_lines(path, start, end, text)` applies an edit only if the file still parses afterwards (Python: `ast.parse`; JSON:
  `json.loads`; other types: accept as-is). Otherwise it refuses with the parser's message and line.
- Register them in the tool-loop schema.
- Acceptance: selftest covers a good edit, a refused syntax-breaking edit (the file is unchanged), and the result caps.

**H3. Step journal + resume** (new `arch_journal.py`, `arch_dataflow.py`)
- Write an append-only `journal.jsonl` per run. Each record: `{step_id, input_hash, status, output_ref, t}`, written after each step
  completes (input_hash = sha of the step id plus the files it reads).
- `architect.py resume <run_id>` re-enters the flow. Steps whose journal record is `done` with the same input_hash are skipped and
  their outputs reloaded.
- The hub's run list gets a "Resume" button for runs whose status is not done.
- Acceptance: a selftest kills a fake flow after step 2, resumes, and checks that only steps 3+ call the model; the fake eval stays
  green.

**H4. Architect developer profile** (`arch_roles.py`, `flows/selfdev.yaml`, prompts)
- Role `architect_dev`: reads CLAUDE.md and the MASTER_PLAN progress log as context.
- Flow `selfdev`:
  1. copy the app to `hub_data/selfdev/<ts>/` (no `hub_data`, `bin` or models);
  2. run the task there;
  3. gates: `py_compile` all files, the module's selftest, `eval --fake --quick`, and the manifest of unchanged safety files;
  4. write a report and a diff;
  5. **never copy back**: the owner promotes with `architect.py self-promote <ts>` (it may reuse self-improve's promote with zip backup).
- Acceptance: the flow validates; a fake run on a trivial task (fix a typo in a docstring of the copy) passes its gates; the original
  tree is untouched (manifest check).

**H5. System optimizer profile + flow** (`arch_roles.py`, `flows/sysopt.yaml`, prompts)
- Role `system_engineer`.
- Flow `sysopt`:
  1. baseline benchmark (existing bench gates: k runs, median);
  2. profile (cProfile for Python);
  3. the model proposes one change at a time;
  4. apply;
  5. re-measure;
  6. keep the change only if it is faster than the noise band and the tests pass; otherwise roll it back;
  7. at most 4 rounds;
  8. report the before/after table.
- Acceptance: the flow validates; a fake-model eval task (a slow function with an obvious fix) passes; the eval family is added.

**H4 status:** `self-improve` already exists (`arch_selfimprove.py`: candidate copy -> gates [protected files, compile, full selftest
in a network-restricted subprocess, eval-fake regression] -> promote only with an explicit confirm, zip backup, rollback). So H4 is
covered. Still to add: an `architect_dev` role prompt that reads CLAUDE.md and this plan's progress log as its context.

**H13-H18 (owner's request, 2026-11-04), in this order, each small and tested:**
- **H13. Fast verification.**
  - Before running tests, check the syntax of changed files (`ast` / `json`); a syntax error fails the step without a test run.
  - Cache test results by `hash(test file + files under test)`, so unchanged code is not re-tested.
  - Run the tests most related to the changed files first (the existing `fast_tests` or name matching) and stop at the first
    failure on a retry.
  - Acceptance: a selftest shows a syntax error caught without running pytest, and a cache hit on an unchanged file.
- **H14. Subprocess sandboxing (Windows Job Objects).**
  - Run test and app processes in a job with a memory limit (`behavior.sandbox_mem_mb`, default 4096), kill-on-close, and the
    existing network guard.
  - On POSIX, use `resource.setrlimit` in preexec.
  - Acceptance: a selftest where a child allocating more than the limit is stopped and reported plainly, and a normal child is
    unaffected.
- **H15. State externalization.** The run's working state lives in files, not in prompts:
  - `run.json` (todo, checkpoints) — done;
  - a `state.md` the planner writes (decisions, open questions), which each step reads;
  - prompts reference it instead of repeating history.
  - Acceptance: state.md is written after the plan and appears in later steps' context.
- **H16. Context shrinking (compaction).** When a step's prompt nears the window, replace older feedback and attempts with a short
  summary: the last failure verbatim, earlier ones as one line each. The project context already cuts least-relevant files first.
  - Acceptance: a selftest with a long retry history shows the prompt shrinks below the window while the last failure stays verbatim.
- **H17. Data prompting.**
  - Structured steps (plan, router, judge) always use a JSON schema; llama-server gets it as a grammar through `response_format`,
    so replies cannot be malformed.
  - Inputs go in as labelled data blocks (FILE, FAILURE, CHECKS) in a fixed order, so the prompt prefix stays byte-stable.
  - Acceptance: a selftest shows the transport passes the schema for structured calls, and the prompt prefix bytes are identical
    across two retries.
- **H18. Smart model handoffs.** When a step moves between models (worker -> heavy escalation, planner -> worker), hand over a short
  structured brief (goal, plan item, files, last failure, what was tried) rather than the raw conversation, and record which model
  did what.
  - Combine with the escalation ladder (H8).
  - Acceptance: a selftest shows the escalated call receives the brief and stays under the window.

**Then: the S11 close-out (test everything, live builds, benchmark).**

**Next session (planned, not tonight unless time remains):**
- H6. Skills library: save a passed run as a skill, found with FTS5.
- H7. Lessons and conventions: failure lessons, conventions from accepted and rejected diffs.
- H8. Escalation ladder.
- H9. Night shift: idle + AC scheduler, nightly eval, morning report.
- H10. AI/agent engineer and component engineer profiles (S14.1 and S14.3).
- H11. Review with a different model family.
- H12. Plan card and Review screen UI.

### S16. First real self-development (owner approved 2026-11-04; about 40-60 min, after the live test frees the GPU)
1. **Role `architect_dev`** in `arch_roles.py`, the "Architect developer".
   - Its system prompt: work only on this app, follow CLAUDE.md conventions (plain words, stdlib only, tests for every change,
     settings with undo), and keep changes small.
   - Its context: CLAUDE.md (head) + this plan's progress log.
   - Add a selftest that the role resolves and its prompt includes both.
2. **Wire it into `self-improve`:** the generating role for self-improve runs becomes `architect_dev` (setting
   `behavior.selfdev_role`, default `architect_dev`, undoable).
3. **First run:** `architect.py self-improve "add a Resume button to the hub run list for runs whose status is not done; it calls
   the existing resume endpoint or CLI"`.
   - It runs on a copy; the gates are compile + full selftest + fake eval.
   - Report the diff and the gate results to the owner. **Never promote without the owner.**
4. **If the models fail it,** report "not yet" with the reason. Do not force it.

Live prototype test (2026-11-04, saved setup: abliterated 4B worker + uncensored MoE heavy). After 7 of 9 tasks: bugfix 3/3,
build 1/3 (the failures miss one test each), feature 0/1.

### S11. Close-out
Owner's request (2026-11-04): at the end, test everything. Run every suite and the harnesses. Then run live builds through the app in
several ways: bugfix, feature, build from scratch, test-writing, refactor, and a data/SQL task. Benchmark coding on the 17
representative tasks with the best setup from S10, and report tasks passed, time and tokens next to the earlier baselines.

1. Full regression and the fake eval.
2. `self-verify --write`, then the release zip, then the backup (`make_backup.py` in the old scratchpad).
3. Update CLAUDE.md with a short section per shipped change.
4. Update the memory file `architect-distributed-direction.md` with what shipped.
5. Write the morning summary in `night_20261104.md`: what changed, measured numbers with their source files, what is not done and why,
   and the waiting-for-yes list.
6. Stop all servers. Confirm the card is at 0 GB used and no `python` jobs of ours are left.

## Only build what the owner asked for
No extra features, documents or checks beyond this plan. Research docs in docs/research_*.md are reference only.

## Waiting for the owner's yes (do not do)
1. **Make the MoE the planner/architect/evaluator model** (best measured with the shoot-out B/C).
2. **Downloads:**
   - Qwen3.6-35B-A3B UD-IQ4_XS (~17.7 GB; check the RAM sum first, see the critique);
   - a newer 4B coder;
   - a llama.cpp update;
   - Inter and JetBrains Mono fonts.
3. **Using the SD card** as the archive/mirror tier.
4. **Running GRC ValiDrive** on the E: stick, or returning it.
5. **Stock (non-abliterated) Qwen3 base models**, for a clean abliteration-cost measurement.
