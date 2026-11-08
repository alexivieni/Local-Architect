# ADR-001: Architecture review of Architect v12 — findings and the proposed order of work

**Status:** Proposed (nothing below has been changed; this is the proposal)
**Date:** 2026-11-07
**Deciders:** the owner
**Method:** six independent read-only reviewers (engine/caching, server/placement/hardware, hub/UI, flows/gates/data engine, orchestration/distribution, tests/CI), every HIGH finding re-verified by hand at the quoted line. Nothing was run that writes; no server was touched.

## Context

Architect is ~26k lines of stdlib Python plus an 8,000-line single-page hub, with 113 self-test suites (~4,060 checks). The last week added the resident llama-server layer, flows-as-data, the LAN mesh, the placement rule, the HUD and a large Antigravity batch. The question was: does it hold together, where does it disagree with itself, and what should change first.

The short answer: the *gates* (tests, fake-code scan, shrink guard, judge re-validation, training-data exclusions, hooks/plugins trust, cleanup fences) are sound and consistently implemented. The weaknesses are almost all in the **seams between subsystems** — six places where the same fact (process alive, memory free, verdict, slot name, cache key) is computed twice with two formulas, and the "three outcomes" rule being honoured at the gate level but collapsed one layer up.

## Findings that must be fixed (verified)

Ranked by how wrong the outcome is, not by how hard the fix is.

| # | Where | What goes wrong | Smallest fix |
|---|---|---|---|
| **1** | `arch_runner.py:105` | `os.kill(pid, 0)` on Windows is `TerminateProcess`. `is_running()` **kills the project it is checking**. Called from `arch_cleanup.plan()` (hub exit, `cleanup` dry run), `arch_runner.start/stop`. | On nt use `arch_server._alive(pid, owner=True)` or `OpenProcess+WaitForSingleObject`. |
| **2** | `arch_server.py:1213-1222` | `stop()` pops the record and saves state *before* `_alive()` looks the image up through that record → image is `""` → any live process holding a reused PID is `taskkill /T /F`'d. Reachable from a stale `server.json` after a reboot (hub exit, idle sweep, `bench_variants`). | Compute `alive` per record before the pop; pass `rec["image"]` into `_alive`. |
| **3** | `arch_dataflow.py:1050-1051` | `if passed and unsure: verdict = "pass"` — a gate that could not run never changes a passing verdict. `not_sure_if` is dead in all 29 flows. | `verdict = "not_sure" if unsure else ("pass" if passed else "fail")`. |
| **4** | `arch_dataflow.py:1012` + `arch_gates.py:211,222` | Step id `scan` (secure, config, purge, network) overwrites the `ctx["scan"]` record the `*_fewer` gate reads → those four flows end `not_sure` after one call whenever findings exist. `RESERVED` lacks `scan` and six other gate-written keys. Reproduced in an isolated home. | Gates write under `ctx["gates"][name]`; derive RESERVED from gate source. |
| **5** | `arch_flows.py:2458` → `arch_core.py:1486,1455` | `finish(ok)` gets a bool: a `not_sure` run attaches **`fail`** to every cached reply and strikes every memory lesson shown. A test-less machine deletes good lessons after PRUNE_AFTER runs. | `finish(ok, error, unsure)`; `None` → skip memory, `attach_verdict(k, "not_sure")`. |
| **6** | `arch_flows.py:463-541`, `arch_checks.py:15` | `verify()` cannot say "could not run": tests `"none"` is neutral, `R()` bool()s. Bugfix with no pytest **passes** in both engines with zero test evidence. | `verify_files` returns `R(None)` when `run_tests` and no test ran. |
| **7** | `static/hud/architect_harness_hud.html:1690` | `innerHTML` with raw trace text (check output, tool args, model names). Same origin as the hub → an injected `<img onerror>` reads `parent.TOKEN` and can POST to every change endpoint. Model output → full hub control. | `textContent`/`createElement`; optionally a per-request nonce CSP for `/hud`. |
| **8** | `arch_tools.py:327-339` | Tool tree fingerprint memoised for the run; only tool writes invalidate. After `Engine._commit`, `put_file`, `BestState.revert`, `wire_contract` undo, `ls/grep/search` serve the **pre-write tree** → model redoes/overwrites finished work. | `arch_tools.invalidate(e, paths)` from `_commit`, `put_file`, `revert`. |
| **9** | `arch_server.py:555-562` vs `1061-1066` | `plan()` subtracts **all** loaded Ollama VRAM as "will be freed"; `start()` keeps models in `cfg.roles/tiers` or everything when `exclusive_gpu=off`. Worker launched with `-ngl layers+1` into memory that is still held → OOM or the "all layers on GPU, 5 tok/s" spill. | One `keep` helper used by both; subtract only what start will actually unload. |
| **10** | `arch_supervisor.py:241-250, 414-416` | `_open()` (runs schema DDL every 2 s) sits outside the `try` in `beat()`; one `database is locked` kills the heartbeat thread silently → `recover()` re-queues a live job → two processes on one project. | `_open()` inside the try; guard `_hb_loop`; store `owner_pid` and check alive before stealing. |
| **11** | `arch_dataflow.py` `_pin_tier` + `arch_core.py:1851,1866` | Tier pins and escalation pins share `e.pins`; any YAML step with `tier:` sets `rt0=None` → **the LAN mesh is never used by a data-engine run**. | Track tier pins separately; only escalation pins disable routing. |
| **12** | `arch_core.py:1923` | Host-down failover picks `default_model` or **the first installed tag** (may be an embedding model or a 14B), not the role's own local model. | `down_roles.add(role)` first, then `self._model_for(role)`. |
| **13** | `arch_fleet.py:415-422` | Fleet `kind: local` has no write fence — "one file owner each" is prompt text + a post-hoc diff. | Pass `ARCHITECT_OWNS` to the child; install the `OwnedEngine._mine` check in `Engine.__init__`. |
| **14** | `model_hub.py:680-711` | `Job._pump` of a finished run writes into the **next** run's buffer/events/`code` (no `self.proc is proc` check). Console can show the previous run's verdict under the new epoch. | `if self.proc is not proc: return` inside each locked block. |
| **15** | `arch_selftest_llama.py:94,804-807` | Sets `ARCHITECT_CLOUD_TEST=1` and never restores it → every later in-process suite runs with the Claude gate flipped. Prime suspect for the "5 FAIL lines only in `--suite full`". | Add the two keys to the saved/restored env list. |
| **16** | `arch_selfimprove.py:147` | "No fewer checks" regex misses 28 suites (~335 checks; digits/underscores in names, four other summary formats). A candidate can delete any of those checks and promote. `run_suite` treats `None` as PASS. | `[A-Z0-9/_ -]*` + the alternate formats; `None` → not_sure. |

Medium findings worth doing in the same passes (all verified by their reviewer; not re-checked by me): `server.json` has no single writer so a start's late save resurrects a stopped tier and the watchdog respawns it; KV sized as q8_0 while the flags may not be passed (known, still so); `_win_gpu` caps AMD/Intel at 4 GB (`AdapterRAM` is uint32); `ensure()` undoes another run's live step-aside; SSE stream has no `id:`/`Last-Event-ID`, so every reconnect (and the hourly forced one) replays the whole run into the HUD; four settings handlers read-modify-write without `mh.LOCK` and a momentarily unparsable config is read as `{}` and saved back; restart from `architect.py start` re-runs the whole start (Ollama, servers, second browser tab); `_run_doc` collapses None to False; `rollback_on_fail` skips the rollback when the gate could not run; `new_tests_pass`/`e2e_pass` delete a correct test file when no runner is present; `refactor.yaml` "pins" with no tests; `sub_flow`/`foreach` drop `unsure` and `ship.yaml` passes on `build.ok` alone; checkpoints resume into a stale tree with gate side effects lost; legacy `DesignFlow` never fails and treats an unusable judge as pass; ALTERNATES chain when `max_attempts > 3`; step-cache key omits sampling/thinking/num_ctx/tool text and `files_fingerprint` silently truncates at 3000 files; `memory_append` accepts bare `sk-…`/`ghp_…` tokens; `limits.keep_alive` is written by tune/presets/battery saver and read by nothing; reply-cache key omits the host (a remote Q8 reply replays as the local Q4's); `._guard` is written/deleted inside the repo tree by suites running in parallel; five suites read the repo's real `hub_data` fixtures; `arch_selftest_finops` is not registered in `--suite full`; slot-file orphan detection never matches the new `-<fp>.bin` names.

## What is solid (leave alone)

Training-data exclusions end to end (`arch_dataset`, `arch_ledger`), hooks and plugins trust (fingerprint, revoke-on-edit, argv-only), cleanup fences (our bin **and** our data dir), the heavy OS lock, `_starting` lock, `save_state` atomic writes, `slots_fit`/`launched_slots`, `caps()` from `--help`, `_gpu_lost`, the safe expression evaluator (no path to builtins/attributes), budgets checked before every request incl. in-flight, prefix-stable prompts (measured: all 25 `{feedback}` prompts have it last), `BestState`, pre-run baselines, `_gate` None-wrapping, the change gate in `model_hub` (Host/Origin/token/size), path containment, CSP + bundled fonts, `ci_parallel` isolation and Job Objects, the manifest (complete over the code tree; 9 edited files since the last `--write`).

## Options considered

### Option A — Fix the 16 verified defects, module by module, no structural change
| Dimension | Assessment |
|---|---|
| Complexity | Low per item; ~16 small patches + tests |
| Cost | 1 session |
| Risk | Low; each has a smallest fix and a reproducible scenario |
| Leaves | the six duplicated-fact seams in place, so the same class recurs |

### Option B — Fix the defects *through* three shared primitives
1. `arch_proc.py` — one process identity `(pid, creation_time, image)` + `alive()`; used by `arch_server._alive/_orphaned`, `arch_runner.is_running`, `arch_cleanup`, heavy lock, supervisor `recover`. Closes 1, 2, 10 and the step-aside ownership gap at once.
2. `arch_fit.py` — one `fit(size, arch, hw, ctx, kv_eff, slots, desk, keep)` returning `(ngl, gpu_gb, ram_gb, full)`; used by `plan`, `arch_port`, `step_aside_for`, `model_hub.fit_model`/`score_for_role`. Closes 9, the q8_0 mismatch, and the `port` vs `plan` disagreement.
3. Three outcomes end to end — `R(ok: True|False|None)`, `verify()` → None when nothing ran, `finish(ok, unsure)`, `_run_doc/sub_flow/foreach` propagate `unsure`, verdict formula fixed. Closes 3, 5, 6 and five of the mediums.

| Dimension | Assessment |
|---|---|
| Complexity | Medium; three new ~150-line modules + call-site swaps |
| Cost | 2–3 sessions, each ending in a green full suite |
| Risk | Medium; touches hot paths, but every call site has an existing selftest |
| Gains | the recurring class disappears; CLAUDE.md's invariants become mechanically true |

### Option C — Flip `flows.engine` to `data` now
Rejected until 3, 4, 6 and the bugfix.yaml read-only-tests gap are closed; the flip would regress bugfix/secure/config/purge/network on day one.

## Decision (proposed)

**Option B, delivered in four waves**, each a commit with its own selftests, the full suite green and `self-verify --write`. Wave 1 is small and stops data from being destroyed; it goes first regardless of which option you pick for the rest.

### Wave 1 — stop the bleeding (one session)
- 1, 2 (process kills); 7 (HUD XSS); 5 and 3 (not_sure recorded as fail / discarded); 4 (`scan` id); 8 (tool cache); 15, 16 (test-gate integrity); register `finops`; `arch_runner.py:222` gets the `CREATE_NO_WINDOW` flag the other call got.
- Commit the five Antigravity `CREATE_NO_WINDOW` diffs (reviewed: correct on Windows, no-ops on POSIX).
- `self-verify --write`.

### Wave 2 — one truth per fact
- `arch_proc` (process identity) and `arch_fit` (fit math) as above; `server.json` single-key writer `_save_tier`; `plan()` inside the start lock and side-effect-free for `/api/arch/gpu`.
- 9, 11, 12, 13; `_win_gpu` uint64; keep_alive setting unified.

### Wave 3 — three outcomes everywhere, then parity
- `R`/`verify`/`finish`/`_run_doc`/`sub_flow`/`foreach`; `rollback_on_fail` on None; `new_tests_pass`/`e2e_pass` never delete on "none".
- `bugfix.yaml` gets the legacy read-only-tests protections (`deny_ctx: existing_files`, `rollback_on_fail`); `refactor.yaml` pin rule; `ship.yaml` pass_if; `DesignFlow` honest; checkpoints carry `tree_fp` and re-run `code` steps.
- Parity selftest compares the **verdict** (three values) per flow, not `bool(ok)`. Only then: `flows.engine = data` by default, legacy one release behind.

### Wave 4 — hub and tests hygiene
- SSE `id:`/`Last-Event-ID`; `Job` generation check (14); settings handlers under `mh.LOCK`, unparsable config refuses to save; restart path for `architect.py start`; `showReport` as a real dialog; HUD verdict three-way.
- `arch_selftest_env.isolate()` prologue + one canonical summary line for all 113 suites; `selftest` spawns a subprocess per suite (same path as `ci_parallel`); fixtures out of `hub_data`; `._guard` under `data_dir()`; README/CLAUDE.md counts (46 roles, 29 flows, ~4,060 checks, 123 eval tasks/21 families).

## Consequences
- Easier: a stale PID can no longer kill anything; "could not run" is visible at the run level, so the ledger/memory/cache stop learning from the unverifiable; the LAN mesh actually carries data-engine runs; `port`, `plan` and the hub agree on what fits.
- Harder: three new modules to keep in sync with their callers (the selftests pin them); a parity suite that fails loudly whenever a legacy flow and its YAML twin diverge.
- Revisit: after Wave 3, whether the legacy flow classes can become thin wrappers over `_run_generate` + gates (twelve copies of the fix loop today).

## Action items
1. [ ] Owner picks: Wave 1 only, or Waves 1–4 in order.
2. [ ] Wave 1 (above).
3. [ ] Wave 2, 3, 4 — one session each, each ending `selftest --suite full` green, `eval --fake` 100 %, `eval --quick` vs `baseline` no regression, `self-verify --write`.
4. [ ] After Wave 3: flip `flows.engine` default and measure `eval --quick` against the legacy baseline before shipping.

*Reviewer reports are in this session's transcript; every HIGH line number above was re-read in the file before it went in this document.*
