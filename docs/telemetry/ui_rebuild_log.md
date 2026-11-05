# UI rebuild log: hub_ui.html, 2026-11-05

Each entry records one decision: WHAT changed, WHY (the audit finding or reference behind it), and HOW (the code approach).
Scope: hub_ui.html only. No Python changed. Nothing was committed.

## 1. One page registry and one sidebar
- WHAT: The old `NAV_GROUPS` ("0. Console" ... "4. Manage"), `TABS` (four places), `SUBNAME` and `PAGE_ICON` were four maps that disagreed with each other. They are now one page registry plus one `SIDEBAR` with three sections:
  - Work: Home, Runs, Workflows, Agents
  - Models: Models, Caches, Cost
  - System: Machine, Settings, Help
- WHY: Audit item "IA / dual nav definitions". It also serves the reference layout (NeuroNest-style grouped dark sidebar).
- HOW:
  - The registry keeps the name `NAV_GROUPS`, because `arch_hub_check.py` regex-reads it and crashes when it is missing. Each row is `[id, icon, label, subtitle]`.
  - `PAGES`, `SUBNAME` and `PAGE_ICON` are derived from the registry with `Object.fromEntries`.
  - `TABS` is `SIDEBAR.flat`, so `tabOf()`, the 1-9 number keys and the art hooks keep working.
  - A script check confirms that every page id sits in exactly one place, with at most 5 subtabs per place.

## 2. LEGACY ids
- WHAT: `go("simple")`, `go("home")`, `go("crew")`, `go("overnight")`, `go("history")` and similar calls now resolve to real pages. An unknown id falls back to Home instead of crashing on `p[2]`.
- WHY: The IA audit asked that every existing `go('x')` keep working. It also covers the "Make ids match labels" rename.
- HOW: `go()` starts with `id=LEGACY[id]||id`. All existing page ids are unchanged, so tests and links still resolve.

## 3. No overflow "More" menu
- WHAT: The `st-menu`/`st-more` overflow menu is removed from `buildNav`.
- WHY: Audit item "Unreachable / hidden". Queue and Launch were hidden behind "More".
- HOW: Each place has at most 5 subtabs, so all of them render.

## 4. Simple mode folded into Home
- WHAT: The separate `simple` page and the `go()` redirect are gone. The redirect turned every page except Settings into Simple, so most pages could not be reached in that mode. Home now carries the one goal box with these parts:
  - quick chips: Fix a bug / Add a feature / Review / Explain
  - a project field
  - a "Chat first" toggle that opens Studio
  - Start, plus Ctrl+Enter
- WHY: Audit item "Duplicates: Simple vs Home vs Console vs Run". It also follows the task's Home spec.
- HOW:
  - `simpleMode()` returns false. `setMode()` now just navigates.
  - `homeHero()` is built once per visit (`built.overview`), so typing survives state refreshes. Only `#homeBody` (the old `overview()` team summary) is redrawn.
  - `simpleStart`/`simpleDraw` are reused, and the progress poll now runs on `page==="overview"`.
  - The dead `simplePage()` is deleted.
  - The hub now opens on Home instead of Console.

## 5. Home stat cards, quick launch and activity feed
- WHAT:
  - Four stat cards: pass rate from the last live benchmark, tok/s, runs this week, GPU free.
  - Quick-launch cards: New run, Run agents, Build a workflow, Check this machine.
  - A Recent activity feed with verdict pills (good/bad/warn).
- WHY: The task's Home spec and the reference dashboards' card row.
- HOW: `loadHome()` reads the existing endpoints `/api/arch/evals`, `/api/arch/machine`, `/api/arch/runs` and `/api/arch/gpu`. An unknown value is shown as "–" with a reason, never as 0. This matches the three-outcome rule.

## 6. Top bar
- WHAT: A top bar above the page title with three parts:
  - a search button that opens the existing Ctrl+K palette (the palette itself is unchanged and already lists every page)
  - a Menu button for phones
  - a live chip: "GPU x% · VRAM a/b GB · Ollama holding n GB [Unload]"
- WHY: Audit item "Top bar": live status was hidden in a sidebar-footer toggle. It also follows the task's chip spec.
- HOW: `liveChip()` polls every 10 s on every page:
  - It reads the live nvidia-smi fields of `/api/arch/gpu`.
  - It reads `/api/arch/ollama` defensively: on a 404 it stops asking, and the Ollama part is hidden.
  - The chip turns amber when a held model's `size_gb - vram_gb > 0.2`, which means part of it spilled to RAM.
  - Unload calls `POST /api/arch/ollama/unload {model}` only for models with `ours` false, and every error goes to a toast.

## 7. Collapsible sidebar on phones
- WHAT: At 820 px and narrower, the 10-item sidebar is hidden behind a Menu button. Choosing a place closes it.
- WHY: The task asks for a sidebar that is "collapsible on phone". The old row strip would have wrapped into three rows.
- HOW: The `body.nav-open` class plus CSS in a new block at the end of `<style>`, using only theme tokens. There is also a reduced-motion rule.

## 8. Renames
- WHAT: The subtab labels now read:
  - Crew -> Agents, Logbook -> History, Overnight report -> Reports, Blueprints -> Build, Flows -> Mine
  - Library -> Briefs, Monitor -> Live, Your models -> Tiers & roles, Scoreboard -> Scores
  - Readiness -> Readiness check, Tune & improve -> Tune, Cost map -> Time per pass, Advisor -> Health
  - Computers -> Hardware, Components -> Interface parts, Studio -> Chat first, Start -> New project
- WHY: Audit item "Non-intuitive labels". "Cost map" measures seconds per passing change, not money.
- HOW: The label field in the registry. The ids are unchanged.

## 9. Places
- WHAT: The places and their subtabs:

  | Place | Subtabs |
  |---|---|
  | Runs | Live (console), New run, History, Reports (overnight), Queue |
  | Machine | Health (advisor), Checkup (doctor), Readiness check, Hardware (computers), Tune |
  | Workflows | Mine, Build, Gallery |
  | Agents | Agents, Fleet, Live, Map, Briefs |
  | Models | Installed, Tiers & roles, Specialists, Benchmarks, Scores |
  | Help | Interface parts (the Components page, which used to have no tab) |

  "Run the app" is also a page-action button on every Runs page.
- WHY: The audit's duplicates findings (Doctor/Assess/Advisor/Tune, Overnight/Benchmarks, Flows/Blueprints/Gallery, Agents) and "Misplaced". Benchmarks went to Models, as the audit says (per-model measurements), because Runs was already at its 5-subtab cap.
- HOW: `SIDEBAR` rows. The late `PAGES.push(["components",...])` is removed, and Components is a registry row.

## 10. Bug: unhandled fetch errors
- WHAT:
  - The tiers-page "Apply recommended team" call is now in try/catch with `toast.err`.
  - The weights slider `onchange` is now in try/catch.
  - `saLoad()` catches its own error and writes it into `#saBox`.
- WHY: Three audit bug items marked "unhandled fetch error". In each one a failed request left the user with no message and no refresh.
- HOW: The patterns given in the audit.

## 11. Bug: Studio poll swallowed every error
- WHAT: After 3 consecutive failures, `pollStudio` shows one note in `#stThread`, or a toast if there is no thread. The count resets on success.
- WHY: Audit bug "silent error swallowing".
- HOW: The counter is `pollStudio.fails`.

## 12. Dead code removed
- WHAT: `modelPicker()`, `chatKnobs()` and `simplePage()` are deleted.
- WHY: Audit "dead code". `pickerOptions()` still has three callers, so it stays.
- HOW: Slice removal, then `node --check` on every script block.

## 13. CSS at 390 px
- WHAT: `min-width:520px` (benchmark chart) and `min-width:540px` (cost map) are removed. The SVGs scale through their viewBox at `width:100%`.
- WHY: Audit "CSS at 390px".
- HOW: Inline style edits.

## 14. Theme token gap
- WHAT: `.fm` and `.fm-dot` now use `var(--acc)`, `var(--mute)` and `var(--bg)` instead of the undefined `--accent`, `--muted` and the `#fff` fallback.
- WHY: Audit "dark/light token gap". These colours never switched in dark mode.
- HOW: Direct token swap.

## Not done in this pass (open)
- The joined Models table is not built, and neither are the per-tier Fallback toggle or the Runs Timeline/Checks/Cost/Trace subtabs. The table needs name, dense/MoE, provider, tier, tok/s and pass rate. The existing Installed page is kept.
- Tiers and Roles are not merged into one page yet; they are two subtabs.
- Settings is not split into sections with subtabs. Training data is a second subtab of Settings.
- `arch_hub_check.py:183` asserts the old four-place `TABS` literal. It now fails by design, because the new IA has 10 places in 3 sections. The Python owner needs to update it to check SIDEBAR. The invariant behind it, that every page sits in exactly one place, was checked with a script and holds.
