# Cleanup report: dead and low-value code (2026-11-04)

Read-only review. No code was changed. Every item below names the search that found it. Items are ranked **low risk first**.

**How the searches were done.** The Grep tool (ripgrep) over `*.py`, `hub_ui.html`, `flows/*.yaml`, `prompts/`, `commands/`, `skills/`,
`agent_templates/`, `*.bat` and `Makefile`. Running Python needed approval in this session, so there was no AST sweep of every
definition. Coverage is targeted: the base hub (`model_hub.py`), every endpoint in the hub against `hub_ui.html`, about 60 of the
~140 settings in `arch_params.SETTINGS`, the top-level functions of 12 smaller modules, and the stale files in the root.

**Left out on purpose** (the brief's exclusions): anything a self-test calls, and anything reached by a name string. That covers
`GATES`, `FLOWS`, `TOOLS`, `ROLES`, `ACTIONS`, page dispatch in the hub, `getattr`, and `mh.X = ...` patches that model_hub really
reads. Examples: `/api/arch/drives` (the UI never calls it, but `arch_hub_check.py:264` does) and `arch_cache.prune` (only
`arch_selftest_speed.py:124` calls it).

**Files other agents are editing right now.** `architect_hub.py` and `hub_ui.html` shifted by several hundred lines while I read
them. The line numbers given for them are from the last read, so find each spot by the quoted name before cutting. New files that
appeared during the review were not judged: `arch_broker.py`, `arch_placement.py`, `arch_routelearn.py`, `arch_selftest_mesh.py`,
`arch_selftest_broker.py`, `arch_selftest_placement.py` and `tools/overnight.py`.

---

## Low risk

### L1. `model_hub.composite`: never called
- **Where:** `model_hub.py:314-320`
- **Evidence:** a Grep for `\bcomposite\b` over `*.{py,html}` finds only the `def` line. The neighbouring `aggregate` has callers
  (`arch_passport.py:206`, `architect_hub.py:181`); `composite` has none.
- **Removal:** delete lines 314-320, the whole `def composite(agg, weights):` block.

### L2. `architect_hub._flatten`, `mh.agent_defaults`, `mh.check_deps`: patches that nothing reads
- **Where:** `architect_hub.py:253` (`mh.check_deps = lambda: ...`), `architect_hub.py:256-263` (`def _flatten`) and
  `architect_hub.py:266` (`mh.agent_defaults = lambda: ...`).
- **Evidence:** a Grep for `agent_defaults|check_deps` over `*.{py,html}` finds only these two assignments. `model_hub.py` never
  calls either name, so the patches go nowhere. `_flatten` is used only by the `agent_defaults` lambda; the copies in
  `arch_selftest_offline.py:122` and `arch_selftest_pro.py:319` are each suite's own.
- **Removal:** delete line 253, lines 256-263 and line 266 (with the blank lines around them).

### L3. `v12_studio.png`: an image in the repo that nothing references
- **Where:** repo root (tracked in git).
- **Evidence:** a Grep for `v12_studio` over `*.{py,html,bat,md,json}` finds nothing; the README does not embed it.
- **Removal:** `git rm v12_studio.png`.

### L4. Eight dated backups of the person's settings in the app folder
- **Where:** repo root:
  - `architect_config.json.bak`
  - `architect_config.json.bak-20261103_174745-before-roles-fix`
  - `.bak-20261104-before-archive`, `-before-claude`, `-before-claude-all`, `-before-fast`, `-before-moe-heavy`, `-before-timeout`
- **Evidence:** they are not tracked (`git ls-files` does not list them) and no code reads them. Only the exact name
  `architect_config.json.bak` is excluded anywhere (`arch_dist.py:32`).
- **Why they matter:**
  - `arch_selfimprove.py:40` skips `SKIP_SUFFIX = (".pyc", ".bak", ...)` with `endswith`, so a `.bak-2026...` file is **copied into
    every self-improve candidate**.
  - `arch_hub_check.py:31` ignores only `"architect_config.json"`, so they go into the hub-check copy too.
  - `arch_dist.py:51` already catches them (`".bak" in fn`).
- **Removal:** these are the person's own files, so move them out of the folder (for example to `hub_data/backups/`) rather than
  delete them. Separately, the person could make `arch_selfimprove.collect` use `".bak" in f` like `arch_dist` does. That is a code
  change, outside this brief.
- **Risk:** low for the code; the person should approve the move.

### L5. Duplicate colour-contrast helpers
- **Where:** `model_hub.py:438-456` (`_lum`, `contrast`) and `arch_checks.py:150-168` (`_lum`, `contrast`).
- **Evidence:** the two pairs are identical line for line (same sRGB formula, same `None` on a bad hex, same `(hi+0.05)/(lo+0.05)`).
  `model_hub.contrast` is used by `model_hub._check_ui`; `arch_checks.contrast` by `check_html`, `arch_flows.py:185,665` and
  `arch_roles.py:89`. (`arch_selftest_hubcalm.py:19` has a third copy; it belongs to the suite, so leave it.)
- **Removal:** keep the copy in `model_hub` (the base module, which imports nothing from `arch_*`). Replace `arch_checks.py:150-168`
  with `from model_hub import _lum, contrast  # noqa: F401`. `arch_tasks.py:25` already imports `model_hub` the same way.

### L6. Stale reference in netguard's docstring
- **Where:** `netguard.py:5`, "Loaded in two places by autonomous_agent_optimized.py".
- **Evidence:** a Glob for `autonomous_agent*` finds nothing. The guard is loaded by `arch_core.guard_env` (`arch_core.py:858-866`).
- **Removal:** reword the line to "Loaded by arch_core.guard_env() through a generated sitecustomize.py." This only touches a
  docstring, but `netguard.py` is in `arch_selfimprove.GUARD`, so make the edit by hand rather than through self-improve.

---

## Medium risk

### M1. The setting `behavior.config_strict` is shown and saved, and changes nothing
- **Where:**
  - `arch_params.py:69` (SETTINGS)
  - `arch_params.py:182` (RUN_OPTIONS)
  - `arch_params.py:340` (the `strict-security` preset)
  - `arch_core.py:93` (DEFAULTS)
  - also mirrored in `localintel_arch.py:1175`
- **Evidence:**
  - A Grep for `config_strict` over `*.{py,html,yaml,json}` finds only those definition sites. Nothing reads it.
  - `ConfigFlow.run` (`arch_flows_sec.py:151-`) always treats every high **and** medium finding from `sec.scan_config_files` as one to fix.
  - So "high" does not relax the scan and "low" does not tighten it.
  - The `strict-security` preset's blurb promises "fail the config scan on medium findings". That is true only because medium is
    the hard-coded behaviour.
- **Two ways to fix it** (the person picks):
  - (a) **Wire it** (better; it is a gate setting). In `ConfigFlow.run`, filter `before` and `after` to findings whose severity rank
    is at or above `e.cfg["behavior"].get("config_strict", "medium")`, using the order high > medium > low. Add a selftest that shows
    "high" lets a medium-only file pass and "medium" does not.
  - (b) **Remove it.** Delete `arch_params.py:69`, `:182` and `:340`, plus the `"config_strict": "medium"` entry in `arch_core.py:93`.
    Both SETTINGS and DEFAULTS must change together, because the check that compares them fails otherwise.
- **Risk:** medium. A person's saved config may hold the key; `deep_merge` keeps unknown keys, so removing it is harmless.

### M2. The legacy agent plumbing in `model_hub.py` points at files that do not exist
The base hub came from an older app built around `autonomous_agent_optimized.py`. A Glob finds none of
`autonomous_agent*`, `hub_selftest*`, `AGENT_NOTES.md` or `.agent_runs`.

| Spot | What it does | Who reaches it |
|---|---|---|
| `model_hub.py:45` `AGENT = .../autonomous_agent_optimized.py` | overwritten by `architect_hub.py:43` | nothing reads `mh.AGENT` (Grep `\bAGENT\b`) |
| `model_hub.py:756-763` default `ACTIONS` (9 entries, all `autonomous_agent_optimized.py`) | replaced wholesale by `architect_hub.py:247-252` (`mh.ACTIONS.clear()`) | dead text |
| `model_hub.py:997-1012` `POST /api/run` | starts `autonomous_agent_optimized.py` (a crash) | UI calls only `/api/arch/run`; Grep `"/api/run"` = only this line. `AHandler.route` falls through to it via `super().route` |
| `model_hub.py:832-843` `list_runs()` + `GET /api/runs` (`:930`) | reads `.agent_runs/` | UI never calls `/api/runs`; the Architect's runs are `/api/arch/runs` |
| `model_hub.py:989-996` `POST /api/clear_results` | clears a model's scores | Grep `clear_results` = only this line |
| `model_hub.py:932-937`, `:1025-1028` `GET/POST /api/notes` + `NOTES_FILE` (`:52`, re-pointed at `architect_hub.py:49`, `arch_core.py:54`) | reads/writes NOTES.md | UI never calls `/api/notes` |
| `model_hub.py:1057-1070` `main()` + `__main__` | `--selftest` imports `hub_selftest` (absent); `serve()` gives the Architect UI without any `/api/arch/*` route | nothing calls `mh.main` (Grep `mh\.main`); the launcher is `architect_hub.main` |

- **Removal:**
  - Delete the `/api/run`, `/api/runs`, `/api/clear_results` and both `/api/notes` branches, plus `list_runs()` and `AGENT`.
  - Empty the `ACTIONS` default to `{}`.
  - Drop the `--selftest` branch from `model_hub.main` (or replace `main` with a message to run `architect_hub.py`).
  - Then delete `mh.NOTES_FILE = ...` at `architect_hub.py:49` and `arch_core.py:54`.
- **Risk:** medium. `model_hub` is the base every hub request goes through. Run `arch_hub_check` and the hub UI suites after the
  change. Keep `JOB`, `/api/job`, `/api/stop`, `/api/input`, `/api/action`, `/api/config`, `/api/weights`, `/api/probe`, `/api/test`,
  `/api/results` and `/api/state`: the UI calls all of them.

### M3. `GET /api/arch/events/stream` (Server-Sent Events): served, never consumed
- **Where:** `architect_hub.py`, route `if u.path == "/api/arch/events/stream"` (about line 1687) and `AHandler._event_stream`
  (about lines 1729-1764, ~36 lines).
- **Evidence:**
  - A Grep for `events/stream|EventSource` over `*.{html,py,js}` finds only the route line.
  - The Console polls `/api/arch/events?since=N`.
  - No self-test calls the stream; `arch_hub_check.py` does not mention it.
- **Removal:** delete the route branch and `_event_stream`, and drop "and `/api/arch/events/stream`" from CLAUDE.md line 77.
  The other option is to make the Console use it (`new EventSource(...)`, falling back to polling), which saves the polling every
  5 s.
- **Risk:** medium, because CLAUDE.md documents it as part of the Console's design. Ask the person which way to go.

---

## Higher risk (refactor, not removal)

### H1. Oversized functions worth splitting
Line spans are from the last read, measured from one `def` to the next.

| Function | Lines | Suggested split |
|---|---|---|
| `architect_hub.AHandler.route` (about 1766-2201) | ~435 | one `_post_<area>(b)` per group (runs, settings/models, queue/crew, fleet, blueprints), dispatched from a dict `{path: method}` like `GATES`. Each branch already returns early, so the split is mechanical. |
| `arch_core.Engine._call` (1262-1585) | ~325 | prompt fitting (`_fit_prompt`: window, `REPLY_ROOM_TOKENS`, `cut_context`), cache lookup and store, transport plus restart/escalation (`_send_with_recovery`), trace and accounting. Every piece is covered by `arch_selftest_chain` and `arch_selftest`. |
| `architect_hub.AHandler.do_GET` (about 1421-1728) | ~308 | the same dict dispatch as `route` |
| `arch_core.replace_def` (512-656) | ~145 | its nested `is_doc`/`indent`/`kind` helpers can move to module level |

- **Risk:** high. These are the hottest paths (every model call, every hub request), and two of them are in a file other agents
  are editing now. Do one at a time, after this round's edits land, with `selftest --suite full` and `arch_hub_check` around each.

---

## Checked and found in use (no action)
- **Modules that look orphaned but are not:**
  - `arch_advisor`, `arch_assess`, `arch_intel`, `arch_launch`: imported by `architect.py` / `architect_hub.py`.
  - `arch_scan`, `arch_taskkit`, `arch_runner`, `arch_training`, `arch_quality`, `arch_pentest`: also imported by
    `architect.py` / `architect_hub.py`.
  - `train_lora.py`: `arch_autotune.py:486,532`, hub ACTIONS.
  - `netguard.py`: `arch_core.py:860`.
  - `apply_localintel_layer.py` and `localintel_arch.py`: an app-specific layer, deliberately outside the harness
    (`arch_deps.py:98`).
  - `UPSTREAM.sha256`: `arch_selfimprove.GENERATED`.
- **Settings, spot-checked (all read by code):** `stall_samples`, `small_helpers`, `battery_saver`, `bench_runs`, `hunt_max_files`,
  `retrieval.chunk_lines`/`max_chunks`/`mode`, `server.ubatch`/`slots`/`ngram`/`parallel`, `memory.prune_after`/`top_lines`,
  `online.cloud_cache_ttl`/`ultra_*`/`allow_pip`/`cloud_fast`, `max_files_per_step`, `self_protect`, `swap_aware`, `fast_tests`,
  `offline_tests`, `tests_first`, `recheck_before_retry`, `fold_failed_sprints`, `learn_from_runs`, `edit_style`,
  `context_chars`, `ctx_autogrow`, `max_file_bytes`, `protected_dirs`, `sandbox_mem_mb`, `tests_on_ecores`, `mutation`,
  `mutation_max`, `hunt_findings`, `budget_scale`, `trust_project`, `num_ctx_max`.
- **Settings, borderline:** `cache.max_rows` and `cache.max_days` are not read by any code path. `arch_cache.prune` falls back to its
  constants `MAX_ROWS`/`MAX_DAYS` and is called only from a self-test. It is excluded above (test-used), but the setting is
  effectively inert. Wiring `prune(cfg["cache"]["max_rows"], cfg["cache"]["max_days"])` into `Engine.finish` would make it real.
- **Top-level functions of** `arch_tune`, `arch_progress`, `arch_ledger`, `arch_resume`, `arch_catalog`, `arch_power`,
  `arch_profiles`, `arch_intel`, `arch_heavy`, `arch_archive`, `arch_sandbox`, `arch_launch`: every one has a caller.
- **Every `/api/arch/*` GET and POST in `architect_hub.py`,** as of the first read, is called by `hub_ui.html`, except
  `/api/arch/drives` (test-used) and `/api/arch/events/stream` (M3).

---

## Report
- **What already existed:** no earlier cleanup report. The settings/DEFAULTS comparison (`params.selftest_defaults_match`) already
  keeps the two lists in step, which is why only one setting turned out to be dead.
- **What I changed:** `docs/cleanup_report_20261104.md` (new). No code edited.
- **Test command:** nothing to run for a document. After acting on M1-M3, run
  `python architect.py selftest --suite full` and `python arch_hub_check.py`.
