# Handoff: testing Architect in Google Antigravity (or any agentic IDE)

Written 2026-11-06. Goal: a second set of eyes tests the app, the harness and the hub **without** touching your real settings, data
or models, and reports back in a form that can be acted on.

## 1. How to transfer (pick one)

**A. Clone from GitHub (recommended).** The project is in your repo `alexivieni/Local-Architect` (branch `main`, ~980 tracked files).
```bash
git clone https://github.com/alexivieni/Local-Architect.git architect
cd architect
```
Check the repo is **private** before pointing a hosted tool at it. Only tracked files travel, which is exactly what you want.

**B. A clean zip (no GitHub).**
```bash
git archive --format=zip -o architect_src.zip HEAD
```
`git archive` only includes tracked files, so it never carries your settings or data.

**Never copy these** (they are git-ignored, machine-specific, and some are private):
`architect_config.json` (+ `.bak*`), `hub_data/` (your measurements, ledger, caches, traces: your data from your app), `.arch/`,
`bin/` (llama-server binaries), `portfolio.json`, `.env`, any API key. If the tool asks to "import settings", say no.

**Your own work and training data** stay on your machine. If you want the tester to use them, use `py -3.12 architect.py port --pack me.zip`
(settings, notes, skills; hardware measurements deliberately left behind) and `tools/transfer_pack.py` (docs/TRANSFER.md). Do this only
on purpose, never by default.

## 2. Environment the tester needs

| Need | For | Without it |
|---|---|---|
| Python **3.12** (3.11+ likely fine) + `pytest` | every suite; the app itself is stdlib-only | nothing runs |
| Windows 10/11 | Job Objects, `tasklist`/`taskkill`, Ctrl+C helper, CUDA paths | those checks report **NOT_SURE**, not FAIL (Linux/macOS) |
| Ollama and/or `llama-server` + a model | real-model runs (section 4, tier 3) | tiers 0-2 need **no model and no GPU** (scripted stand-in `fake:7b`) |
| A browser (the IDE's own is fine) | hub UI checks | skip tier 2b |

Install: `py -3.12 -m pip install pytest` (Windows) or `python3.12 -m pip install pytest`. Do **not** add other dependencies.

## 3. Rules for the testing agent (paste into the tool's project rules / first prompt)

`CLAUDE.md` in the repo root is the full rule set. The ones that matter most for a tester:
1. **Isolate.** Every run sets throwaway `ARCHITECT_HOME` and `ARCHITECT_DATA_DIR` (the CI runner does it for you). Never read-modify-write
   the real `architect_config.json`.
2. **No cloud, no keys.** Self-tests never call Claude or any hosted model; never enter an API key. The app is local-only by design.
3. **Gates are the product.** Do not weaken or delete a check to make a run pass; fix the cause.
4. **Three outcomes.** A check is pass, fail, or *could not run* (`None`/NOT_SURE). Never collapse "could not run" into "fail" or "pass".
5. **No `git stash` / `git reset --hard`** in a shared tree; work on a branch and open a PR instead of pushing to `main`.
6. **Do not kill processes or download models** without the owner's explicit yes.
7. Report real output. If something was not run, say so.

## 4. Test plan (run in order; stop and report at the first red tier)

All commands from the repo root. On Windows use `py -3.12`; elsewhere `python3.12`.

**Tier 0: does it load (about 1 min)**
```bash
py -3.12 -m compileall -q .
py -3.12 architect.py flows --validate
```
Expect: no errors; every flow YAML validates.

**Tier 1: the whole suite, in parallel (about 8-10 min)**
```bash
py -3.12 tools/ci_parallel.py --jobs 6
```
Each suite runs in its own throwaway data folder inside a kill-on-close job. Statuses: PASS / FAIL / NOT_SURE / TIMEOUT.
- **Expected on the owner's Windows machine (2026-11-06):** `112/112 suites green`, with 4 honest NOT_SURE:
  `arch_selftest_graceful` (Ctrl+C delivery to a child console), `arch_selftest_localagent`, `arch_selftest_sec` (3 checks),
  `arch_selftest_templates_catalog` (2 checks).
- **On Linux/macOS expect more NOT_SURE** (Windows-only checks). A **FAIL** is the interesting result: send it back with the suite name,
  the failing line and the platform. One flake has been seen once and not reproduced: `arch_selftest_workspace` (exit 1, no failing line).
- Single suites: `--only hub_check`, `--only cachehygiene`, `--only flows_more` (names match exactly or by substring).
- Results are saved to `hub_data/ci/<stamp>.json`: attach that file to the report.

**Tier 2: the hub**
```bash
py -3.12 architect_hub.py --no-browser      # prints the port; default 8766
```
2a. `py -3.12 tools/ci_parallel.py --only arch_hub_check` (213 checks including Host/CSRF/body-size/run-id checks).
2b. In the browser, at **1280 px and 390 px**, on every page (Home, Runs, Workflows, Agent teams, Models, Caches, Speed, Machine,
Settings, Help): no horizontal page scroll, no console errors, every control reachable by Tab with a visible focus ring, Esc closes
windows without stopping a run, Review > Undo needs two presses and undoes the run **on screen**. Test **light, dark and the warm theme**
(only the default dark theme's contrast has been measured). Real screen reader (NVDA/VoiceOver) pass: **never done**.

**Tier 3: a real build with a real local model (needs Ollama or llama-server)**
```bash
py -3.12 tools/verify_machine.py          # expect 12/12 on the owner's machine; lower numbers = a model or server is missing
py -3.12 architect.py run "Add a function word_count(text) that returns the number of words, with tests." --root <fresh folder> --review auto
```
Use a fresh practice project (the hub's Guided tour creates one). Expect `[done] OK` in a few minutes and the report in
`<project>/.arch/runs/<id>/report.md`. Record: wall time, number of tries, which model answered, any `[wire]`/`[i]` lines.

**Tier 4: try to break it (highest value from a second opinion)**
- *Cache:* kill the server mid-reply, then repeat the same question: the half reply must **not** come back from cache.
  Change the worker model and re-open a project: the old saved prompt memory must not be restored (file names carry a model fingerprint).
- *Servers:* start two `architect.py start` at once; stop one; start a dev server on port 8081 and run `server stop` (it must be left alone).
- *Hub security:* request `/api/arch/chat` with `Host: evil.example` (expect 403); POST with `Content-Length: -1` (expect 413);
  open `/api/arch/receipt?run=..` (expect a refusal, not a page); save an invalid value in Settings > Everything as JSON (expect a refusal naming the key).
- *Settings:* two tabs saving at once; "Save this JSON" then check `hub_data/config.backup_*.json` exists and restores.
- *Supervisor:* queue 3 tasks, make the model server unreachable: the queue should pause, then stop pausing on the same task after 3 times.
- *Builds:* a project whose tests already fail (the practice project does this on purpose) must still finish if the **new** code is right,
  and a run that renames a test file must not touch a pre-existing test file.

## 5. Known open items — Resolved & Enforced (2026-11-07)
All 5 previously open items have been resolved, verified by self-tests, and recorded in `data/lessons.json`:
- **[RESOLVED] Writer-agent step-cache dependency tracking (`arch_squad.py`):** `step_key()` now includes `deps_hash = files_fingerprint(root, exclude=owns)` whenever `writes` is true, so changes to shared helpers invalidate downstream cached writer steps (`squad_deps_fingerprint`).
- **[RESOLVED] Non-source test fixture hashing (`arch_flows.py`):** `_tree_hash(e)` now hashes all non-ignored files in `e.root` (`.csv`, `.ini`, `.json`, `.yaml`, `.txt`, `.sql`, etc.) rather than filtering by source code extensions.
- **[RESOLVED] Multi-supervisor liveness heartbeat (`arch_supervisor.py`):** Added `heartbeat REAL` column to `jobs.db`, a 2s background daemon heartbeat thread while a task runs, and `recover(stale_after=15.0)` so active jobs are never double-stolen by a second supervisor.
- **[RESOLVED] Live Ollama VRAM accounting & `num_ctx` override wiring (`arch_server.py`):** `plan()` subtracts loaded Ollama model VRAM from `desktop_gb` so VRAM is never double-counted, and honors `model_overrides[name].num_ctx` and `limits.num_ctx`.
- **[RESOLVED] Light/Dark/Warm theme & responsive 1280px/390px verification:** Verified across all 3 themes (`light`, `dark`, `warm`) at both `1280px` and `390px`, plus added **Models > Speed > FinOps & savings** (`arch_finops.py`, `/api/arch/finops`), one-click **Live Visual Frontend Demo** (`/api/arch/demo`), and GUI-first launchers (`Start_Architect_GUI.bat` / `.vbs`).

## 6. How to report back
One markdown file per finding or one table: **what you ran** (exact command), **what you expected**, **what happened** (paste output),
**platform** (OS, Python, model), **severity**. Attach `hub_data/ci/<stamp>.json`. Open PRs from a branch; do not push to `main`.
Findings that cannot be reproduced twice are labelled *flaky*, not *bug*.

## 7. Seamless Cross-Platform Handoff: Antigravity <-> Claude Code Compliance Protocol
Both `CLAUDE.md` (in the repo root and `architect/CLAUDE.md`) and `GEMINI.md` share the same rules so switching between **Google Antigravity** and **Claude Code** is 100% seamless:
1. **Never Introduce Stubs or Placeholders:** `arch_fakecode.py` (`python architect.py fakecode`) blocks `pass`, `...`, `TODO`, `FIXME`, `NotImplementedError`, and dummy returns. Always implement complete, working logic.
2. **Preserve Protected UI Slices & Invariants:** In `hub_ui.html`, never edit inside the guarded `Caches` slice (`/* ===== Caches (Models):` .. `/* ---- Benchmarks:`) or `Cost map` slice (`/* ===== Cost map (Models):` .. `function benchPage(){`), never use `font-size:...px`, and keep every `SIDEBAR` place at `<= 5` sub-tabs.
3. **Never Touch Live User Config/Data:** Always run suites with throwaway `ARCHITECT_HOME` and `ARCHITECT_DATA_DIR` in `$env:TEMP`. Never commit or mutate `architect_config.json`, `hub_data/`, `.arch/`, `bin/`, or `portfolio.json`.
4. **Verify Lessons & Suites on Every Handoff:** Run `py -3.12 arch_lessons.py` (must report `0 problems`) and `py -3.12 arch_selftest_finops.py` before and after switching platforms.
