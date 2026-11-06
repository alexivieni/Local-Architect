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

## 15. Crew builder subtab (Agents place)
- WHAT: New page `crewbuild` ("Crew builder") in the Agents place. It shows one card per agent (model, effort, tools, budget) with an Edit dialog (description, model tier, role, effort, tools, budget calls/tokens/seconds, context policy/max chars). Save posts to `/api/arch/squad/agent`, and `save_agent`'s ValueError is shown inside the dialog. Below the cards is a Workflows list. Each workflow's "Run…" panel takes a goal, the files it may change, slots, a fake toggle and per-agent `agent.field=value` overrides. It posts `/api/arch/squad/run` and opens Live. The run is the ordinary hub job, so Orchestration shows it afterwards. Home's "Run agents" card now opens this page.
- WHY: The owner asked to build and run squads from the hub instead of editing YAML by hand.
- HOW: `crewBuilderPage()`/`loadCrewBuilder()`/`editAgent()`/`runPanel()`. One shared `modal()` (native `<dialog>`, so Esc and focus trapping come free) and `confirmBox()`. If arch_squad or one of its functions is missing, the endpoint returns `{available:false, why}` and the page prints "Not available yet: …". The Agents place now has 7 subtabs and Models has 6. That is past the old 5 cap, but the pills scroll sideways (entry 3), so there is still no overflow menu.

## 16. By job subtab (Models place)
- WHAT: New page `byjob`. It shows one row per job from `arch_select.why(cfg)`: current model, measured best, C (s/pass), pass % with CI and n, tok/s, fits, and the plain reason. An "Apply <model>" button appears only when a suggestion exists. It asks first in a confirm dialog, runs `select --apply JOB MODEL` as a job, and then shows an Undo button (`select --undo`). "Screen models" (with a Fake toggle) runs `select --screen [--fake]` as a job and opens Live.
- WHY: The owner wants model choice per job to be measured and explained, not guessed.
- HOW: `byJobPage()`/`loadByJob()`/`applyJob()`. The table sits in an `overflow-x:auto` box, so it does not overflow at 390 px. A CI is accepted either as `[lo,hi]` or as a ± number.

## 17. Quick-launch options (Home)
- WHAT: The New run, Run agents and Build a workflow cards each have a small "options" button. It opens a dialog with review mode, reasoning (thinking), acceptance tests first, best of N and the flow budget scale. Save writes them through the existing `/api/arch/settings`, and a blank field clears the setting back to the default.
- WHY: These are cheap parameterisation buttons. The dialog says plainly that they are saved settings that apply to every run, because that is what they are.
- HOW: Fields are built from `S.settings_schema` (arch_params.SETTINGS), filtered to `behavior.review_mode`, `behavior.thinking`, `behavior.test_first`, `behavior.best_of_n` and `flows.budget_scale`. A setting that is not in the schema is not shown. The options button sits beside the card, not inside it (no nested buttons). All new CSS uses theme tokens only.

## 18. Hub endpoints (architect_hub.py)
- WHAT: GET `/api/arch/squad/agents`, `/api/arch/squad/workflows`, `/api/arch/select/why`. POST `/api/arch/squad/agent`, `/api/arch/squad/run`, `/api/arch/select/apply`, `/api/arch/select/undo`, `/api/arch/select/screen`.
- WHY: The pages above need them.
- HOW:
  - GETs check the host, like other reads. POSTs go through the base `route()`, so they get the same token, Host and Origin checks.
  - Names are regex-checked. Owned paths may not contain `..` or commas. Slots must be 1-8. Overrides must match `agent.field=value` and also go through `arch_squad.parse_sets` when it exists. The workflow must exist as `workflows/<name>.yaml`. Job names are checked against `arch_jobs.JOBS` when that module exists.
  - Every run is `mh.JOB.start(...)` on the CLI (`crew`, `select --apply/--undo/--screen`), so it streams like a run.
  - `arch_hub_check.py` has 16 new checks: page reachability, response shapes, refusals, token required, and settings that exist.

## 19. Colour, type and motion tokens (ux_audit V-B, V-F, V-G)
- WHAT: A token layer at the end of `<style>` adds the V2 category hues (`--c-flow/agent/template/model/cost/system` plus `-ink` and `-bg`) for light/paper, dark (system and explicit) and midnight. It also adds `--live`, `--grad-hero`, `--grad-hero-soft`, `--tint-card/hover` and the accent swatches as tokens. Each of the 10 places gets its own `--hue` through `body[data-sec]`. There are `.chip.flow|agent|template|model|cost|system` variants, and a plain `.chip` stays neutral. For type: `--t-display/title/head/caption/mono`, with the old `--t-*` names kept as aliases and the minimum label size at 12/13 px. 30 stylesheet font sizes below 12 px went up to 12 px. For motion: `--spring`, hover lift on clickable cards, a shimmer, the live-dot pulse, page entry on `#view` only (no per-card `cardIn`), and press scale. The nine scattered reduced-motion blocks are now one.
- WHY: The person wants more colour and life, and an end to "blocky" text. Audit item 24 is superseded by V2.
- HOW: Tokens only, so no colour literals outside token blocks. A hub check counts the dark/light token copies and the single reduced-motion block.

## 20. Home rebuilt (items 1, 2, 3, 4, 14, 20; V-C, V-D)
- WHAT: Home is now the hero and the recent runs. The hero has a gradient-soft surface, the display headline "What should your computer work on?", a one-line sub, examples, the goal box, "Project folder (the code to work on)", "Use the practice project" (`/api/arch/practice`), **Do it**, "Talk it through first" and a Ctrl+Enter hint. The Quick launch row and the "Chat first" checkbox are gone. Stat cards have an icon box, a hue edge, a loading shimmer, and a 7-point sparkline for the test pass rate. `overview()` keeps only `getStarted()` until the first visit is done. The "More: starters / readiness / how it works" block moved to Help. The result card has a status icon that springs in once, a headline, a files-changed count, and the buttons Keep it (primary), See the details, Show changes and Undo. Undo is outline-danger and asks for confirmation first. A missing folder shows an inline alert with `aria-invalid` instead of a toast.
- WHY: Audit root cause 1: Home was six stacked sections with no first sentence.
- HOW: `homeHero()`, `loadHome()`, `simpleStart()`, `usePractice()`, `simpleDraw()`, `homeMore()`. There is one shared `emptyState(art,title,line,button,fn)` (art from `art()`), used on Home, Models and Runs.

## 21. Labels, structure and keyboard (items 5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18, 19, 22)
- WHAT:
  - Labels and nav: page labels follow the jargon table (Watch a run, All agents now, Parallel agents, Connections, Agent recipes, My models, Tests, Model report card, Speed per model, Health check, Optimise for this PC, and so on). The places are renamed Agent teams and Speed. Agents has 5 subtabs: Library moved to Workflows and Orchestration to Help. Registry subtitles are 12 words or fewer.
  - Keyboard: Esc must be pressed twice within 2 s to stop a run. `g`+letter chords replace bare digits and are listed in Shortcuts.
  - Sidebar footer: it keeps the status chip and Appearance. The accents moved into the Appearance picker with `aria-label`s. Commands and "?" moved to the top bar.
  - Toasts and live chip: error toasts are `role=alert`, stay at least 8 s (longer while hovered), and have a Copy details button. The live chip reads "Graphics card N% busy · x of y GB memory used · A model is loaded (z GB)" and its button reads "Free memory".
  - Page hints: each page note shows once overall, then a "?" by the title.
  - Small screens and long runs: the subtab strip scrolls the active tab into view and has a fade cue. There is one phone drawer with a veil and `aria-controls` (the three old row-sidebar rules are deleted). A sticky run bar on other pages reads "Working on … · Watch · Stop", then "Done … · Review".
- WHY: These are the audit's quick wins in its ranked order.
- HOW: The hub check's SIDEBAR label regex now allows spaces and `&`, so a place like "Agent teams" parses. The check's intent is unchanged, and a new check covers the 5-subtab cap.

## 22. Shorter copy, avatars, thumbnails (V-A, V-E)
- WHAT:
  - Copy: `learnMore()` (`details.more`) is added, and `.sub` is capped at 62ch. 34 of the V1 table's dense paragraphs became one short line, with the old text kept under Learn more. Four were not found as plain literals and stay as they were: fake-code, CI check, By job, Studio.
  - Avatars: `avatar()` (initial in a hue ring) on Crew builder cards and Gallery agent cards.
  - Thumbnails: `chainThumb()` (a step-chain SVG) on Gallery workflow cards and Crew builder workflow rows.
- WHY: V1 found 40 blocks of over 140 characters, and there were no avatars or thumbnails anywhere.

## 23. Template catalog (New project, Gallery) and endpoints
- WHAT: GET `/api/arch/templates/catalog` returns `{available, templates:[...fields, thumb]}`, with `available:false` when `arch_templates_catalog` is missing or raises. GET `/templates/<id>.svg` serves `static/templates/<id>.svg` as `image/svg+xml`, with the id checked against `[a-z0-9][a-z0-9_-]{0,63}`.
- The New project picker draws `tplCatalogGrid()` when the catalog is available. Each card shows a thumbnail, a category chip in its hue (the `colour` token name), difficulty, stack tags and Learn more (long + next steps). A category filter sits above the cards. Without the catalog it falls back to the old `tplGallery` list. The Gallery gets a "Project templates" section from the same cards.
- WHY: The owner asked for a colourful, browsable template picker.

## 24. Crew builder reads arch_squad's dicts
- WHAT: `squad_agents()` and `squad_workflows()` accept the dict shape `{name: def}`, so the agents endpoint now lists all 17 agents (a hub check proves it). Workflows carry `title` and `problems`. The Crew builder shows each phase as a coloured chip by kind: agent, parallel, loop (with until/max), panel or code. A workflow with problems is shown as not runnable, and its Run button is disabled.

## Not done (this pass)
- 21 (one Health page) and 30 (the "Show advanced pages" toggle) are not done. The Models place still has 6 subtabs. Items 23 and 25-29 are partly covered by the token work.
- Not checked in a browser: the 390 px drawer and the contrast figures.

## 25. Plugins, Telemetry, Squad runs pages and design iteration 2
- WHAT:
  - New pages: Plugins (Workflows), Telemetry (Speed), Squad runs (Help). Plugins lists each plugin with an avatar, enabled/trusted chips, what it provides (agents, workflows, skills, commands, hooks), its problems, and Enable/Disable, Trust... (an inline warning that its hooks and commands will run code here) and Remove. You can install one from a folder or .zip path. Telemetry has six summary cards (calls, tokens, cache hit share, prefix reuse, errors, GPU peak) with a window picker and a 15 s refresh, a list of recent runs from the events, and a run timeline drawn as nested spans with duration bars. Squad runs shows each run with a status chip, an "n of m cached" chip and Resume.
  - Endpoints: GET `/api/arch/plugins`, POST `/api/arch/plugins/{enable,disable,trust,install,remove}` (trust needs `confirm:true`; install needs a folder or .zip that exists), GET `/api/arch/telemetry/{summary?window=,events?since=&run=&kinds=,timeline?run=}`, GET `/api/arch/squad/runs`, POST `/api/arch/squad/resume {run_id}`, which starts `architect.py crew <workflow> --resume <run_id>` as the hub job. Every one answers `available:false` with a `why` when arch_plugins, arch_telemetry or arch_squad.list_runs is missing. The expensive GETs go through `_ttl()` (5 s, or 3 s for a timeline; `ARCHITECT_NO_CACHE=1` turns it off), and a change clears its key.
  - Audit 21: there is one Health page (page id `doctor`, which keeps the existing first-visit hint test valid). It has one Run checkup button and two groups: Next steps (the advisor) and Checks on this PC (the doctor). The LEGACY map sends advisor, health and checkup to it.
  - Audit 30: there is a "Show advanced pages" toggle in Settings, off by default and stored per browser. When it is off, the sub-tabs hide score, bench, library, map, monitor, orchestra, components and train. Ctrl+K and links still reach them.
  - The Models place now has 5 sub-tabs, because By job moved to Speed. Every place has 5 or fewer, and the hub check now enforces 5 for all of them.
  - The four leftover dense paragraphs (Studio, Fake code, CI, By job) are now one line each, with the full text kept under Learn more or a tooltip.
  - Audit 23: all 183 hard-coded `font-size:Npx` became rem.
  - Audit 27-28: one focus ring. The three duplicate 2 px rules are gone, the 3 px rule stays, and `:focus:not(:focus-visible)` is added. The palette traps Tab and gives focus back to whatever had it on close.
  - Audit 25: the new icon-only buttons carry their own aria-labels; the a11yPass covers the rest.
  - Audit 29: the first run has three stops (Check this PC, Pick a team, Give it a first task).
  - Hub check: `tdz_scan()` is an ordered scan of top-level const/let declarations against load-time reads outside function bodies. It flags the G_CHORDS shape that blanked the hub. It also tests itself on a bad sample and a clean one.
- WHY: The owner asked for local plugins, telemetry and resumable cached squad runs to be visible and usable. Iteration 2 closes the audit's open items. The TDZ bug passed every check before this.
- HOW: The pages use only the hub's own parts (`emptyState`, `learnMore`, `avatar`, chips and the theme tokens), plus one shared `notYet()` for the "not available yet" state.

## Not done (iteration 2)
- `architect.py crew --resume <id>` must exist in arch_squad's argparse for Resume to work. Until then, the job ends with an argparse error in the Console.
- The colour/contrast and 390 px checks have not been done in a browser. The TDZ scan treats `if(...){}` blocks at the top level as deferred, so it can miss a read inside one.
- Pre-existing failures that are not from this pass: arch_selftest_hubui.py (its regex expects `const TABS=[...]` literals, and there is a "Claude agents" label in the orchestration stats), and arch_selftest_firstvisit.py "colours come from the theme tokens" (`#fff` in that CSS region at HEAD too).

## 26. Home "one box" (docs/telemetry/handoff_home.md) + live chip + passport column
- WHAT: Home now goes headline (with a "?" About this page button) -> one-line helper -> visible label "What do you want done?" -> goal box with Do it in its bottom-right corner (full width under the box below 768px, 48px tall) -> Talk it through first (carries the text into Studio) -> "Try:" chips below the box (scroll sideways with an edge fade on phones) -> project combobox (recent folders from store key recentRoots, last 5, middle-ellipsis with full path in title; Use the practice project; Paste a path...; arrows/Enter/Esc) -> recent runs (5 newest, verdict chip, relative time, Open) -> stat cards inside <details> "This computer" (closes itself once a run exists unless the person toggled it). On Home the subtab strip and page tip are hidden (the page title was already hidden). Do it is disabled with the tooltip "Type what you want done" while the box is empty; while starting it shows a spinner + "Starting..." and the box is read-only; no project puts "Choose a project first" under the box (role=alert) and moves focus to the project field (aria-invalid). The box autosizes 96-240px, then scrolls; over 4000 characters a caption suggests splitting. After 8 s still loading, recent runs show "Still reading..." + Retry and stat cards say so; a stat with no measurement shows an em dash and "after your first run". The live chip shows "5.2/6 GB", "5.2/6 GB · model loaded" or "GPU 74%" with a live dot; the full sentence is in title and a sr-only span; max-width 360px, ellipsis, never wraps; the Free memory button has the spec aria-label. Models table: new "Family · best jobs" column from passport.family_line with passport.explain as title + Learn more (tune_info now attaches identity/family_line/explain from arch_passport.load when a passport exists). Models > By job: the Why cell shows Prior + prior_reasons when /api/arch/select/why sends them.
- WHY: design critique of 2026-11-05: first-time users stalled on a page with six stacked sections; chips above the box read as the primary action; the live chip was cut mid-word at 1440px.
- HOW: theme tokens only (hover uses color-mix with --mask, the existing black token); motion uses --fast/--base/--ease/--spring and stays under the single reduced-motion block; 44px touch targets below 768px. Squad runs Resume already posts to /api/arch/squad/resume, which runs architect.py crew <workflow> --resume <run_id>. Hub checks added: nothing above #spTitle on Home, Do it disabled when empty, recentRoots combobox, chip max-width rule.
- Not checked in a real browser yet (layout at 390/768/1440 px, combobox keyboard behaviour).

## 2026-11-05 — six blockers from the lead's real-hub walk-through
1. **Practice project vs practice task.** WHAT: Home "Use the practice project" now only fills the folder and focuses #spGoal; "Try a practice task" (runs empty state, tour) starts the run. WHY: it started a bugfix run and jumped to Console. HOW: `/api/arch/practice {start:false}` is create-only (returns `started:false`, never rewrites files while a job runs); `usePractice()` vs new `tryPractice()`.
2. **One source of truth for "running".** WHAT: the "What I'm doing" panel shows "Running <flow>. Getting started…" when EV.running is true and no step event exists yet. WHY: strip said "running bugfix", panel said "Nothing running". HOW: both read EV.running (from the events feed, replayed from since=0 after reload); pollConsole also redraws when EV.running flips without new events.
3. **Tour.** WHAT: 3 stops (This computer → Practice task → Result; team step only via Models; the dock shows during the run as stop 2); text placeholder instead of the grey skeleton; Skip/Later/Finish set FV.closed in memory and in store. WHY: grey box, and the tour re-opened after Skip. HOW: the culprit was fvTick awaiting the events probe and then calling fvOpen(4) after Skip; it now re-reads state after the await and stops.
4. **Keys.** WHAT: on Home a printable key with nothing focused focuses #spGoal and inserts it; g-chords need both keys with nothing focused, any other key cancels, never in fields. HOW: `nothingFocused()` + `setRangeText`.
5. **Live chip.** WHAT: "x/6 GB · model in RAM 14.8 GB" or "· model on card 3.1 GB" from Ollama's size vs size_vram. WHY: "model loaded" hid a 14.8 GB RAM model.
6. **Claude explicit.** WHAT: `cloudPlan()` from online.enabled + online.cloud_tier (+ model) e.g. "Planning, review and judging → Claude Opus 5.5 (billed)"; strip chip is a button to `goSetting("online.cloud_tier")`; the same line shows under Do it on Home. Nothing shown only when every step is local.
Checks: arch_hub_check +7 (142 total), firstvisit 3-stop + skip-sticks (50). All six UI suites green; node --check OK.

## 2026-11-05 — ETA, notifications, effective settings, data night, plugin commands, presets
- WHAT:
  - **ETA** (`arch_eta.py`): "12/40, ~1.4 h left (1.1-1.9 h), ends 03:40" from seconds MEASURED per task (the run's own pace after 3 tasks, else saved eval points by family+model, else finished queue tasks, else a stated 4-minute guess, said out loud). Shown in Runs > Queue (`#qEta`), the sticky run bar (`.rb-eta`) and the Console strip (`#cEta`); `GET /api/arch/eta` also takes `preview=1&until=HH:MM&max=N&worker=` for a data night.
  - **Notifications** (`arch_notify.py`): Windows toast through the built-in PowerShell (balloon fallback), macOS osascript, Linux notify-send; text travels in environment variables, never in the script; never raises; gated by `behavior.notify` (default on) and `ARCHITECT_NO_NOTIFY`. Fired by the hub when any job ends (runs, self-improve rounds), by the supervisor at queue end and by the data night at its end.
  - **Effective settings** (`arch_effective.py`, Settings > Effective settings, role picker): final value per key and the layer (explicit / your role profile / preset / shipped role profile / model family default / built-in). With `behavior.role_profiles = on` it overlays `arch_roleprofile.resolve`; a value is "preset" only when it matches a preset you applied (read from the undo log).
  - **Data night** (`tools/train_traces.py --until HH:MM --max-tasks N --worker M`): fresh seed per night (`data_night.json`), stops between tasks when the next one would not finish before the end time, dataset export at the end. Runs > Queue has the card (worker, end time, most tasks, ETA preview, "uses only local models").
  - **Slash palette** lists every enabled plugin's commands (name, description, arguments, plugin name and colour); a plugin command whose name is taken shows as `plugin:name`. The Plugins page lists each plugin's commands (also while disabled, marked "after you enable it").
  - **Presets** (Fast / Balanced / Quality / Battery = `architect.py preset NAME`): top-bar Presets popover and a Settings card. Each shows what would change (setting: now -> new) before Apply; Undo puts back the last write. Endpoints `GET /api/arch/rpresets`, `POST /api/arch/rpreset {name}` or `{undo:true}`.
- Tests: arch_selftest_eta.py (25), arch_selftest_notify.py (16), arch_selftest_effective.py (17); arch_hub_check.py +22.
- Not done: `arch_eval.run` has no `should_stop`, so the data night runs one task per call (the stop still falls between tasks). Not checked in a real browser (popover position at 390 px).

## 2026-11-05 — Review screen, editable plan, session panel, queue board, recipes
- WHAT:
  - **Review** (Runs > Review, `reviewPage`): after a run, files changed as diff cards (+/- counts, new-file chip), tests with Passed/Failed, verdict chip, cost line "$0 · local · N calls · T s", and Accept / Undo / Retry with a note. Keys: A, U (press twice; the first press arms it), R (jumps to the note; Ctrl+Enter sends), J/K between files. Home's recent runs have a Review button. "Reports" moved to Help to keep Runs at 5 sub-tabs.
  - **Plan card**: the run's todo as a checklist. Editable (tick, up/down reorder, add a step, Run this plan) only when the engine reads an override; otherwise read-only with "Edit is not available yet."
  - **Session panel** (right column, stacks under 900 px, each section collapsible, state remembered): context bar by section (system prompt, skills, memory, tools, project files, conversation, free space) with window size and a compaction mark; plugins/connectors, outputs, changes (+/-), servers (tier, port, card or RAM), background jobs with durations. No chart library.
  - **Queue board**: Waiting / Running / Done columns above the list, with time estimates from arch_eta.
  - **Recipes**: Home "Recipes that worked before" cards (title, summary, flow chip, "4/8 · flow-level" or "not measured"); Use this fills #spGoal and sets the flow Do it uses, and shows "needs: ..." from missing_text. Template cards show the contract (routes, commands, test files) under Learn more.
  - **Hub check fixes**: the agent-count check compares with arch_squad.list_agents() instead of a fixed 18; the crew-run checks now pass; `_workflow_path` accepts .yaml or .md.
- WHY: the agreed next design steps (Review after a run, plan before it), Claude Code's context panel, and the owner's queue/recipe requests.
- HOW: new endpoints GET /api/arch/review, /plan, /session, /recipes; POST /api/arch/review/accept, /plan, /recipes/fill. All read the run's trace.jsonl defensively (unknown records skipped; no diff record -> `git diff` of the files the steps changed, read-only). Theme tokens only, one reduced-motion block.
- Engine patches needed: (1) `Engine.plan_override()` reading `<run>/plan_override.json` ({items:[{id,title,status}]}) so plan edits apply; until then /api/arch/plan POST answers supported:false. (2) Call records could carry `sections: {system_prompt, skills, memory, tools, project_files, conversation}` token counts; until then the panel estimates from message text and says so. (3) Optional: a `recipe` field on run.json.
- Not checked in a real browser (390 px, keyboard).

## 2026-11-05 (evening) — registry, Presets first, Lessons, Last night, one-click data night
- WHAT:
  - **Registry**: `arch_uireg.py` parses the SIDEBAR / NAV_GROUPS / LEGACY / PAGE_ART blocks of hub_ui.html once (cached by file time) and `GET /api/arch/ui/registry` serves it (places, pages with id/icon/label/description, legacy map, page functions). art, cachespage, firstvisit, hubui, workspace and hub_check read it instead of carrying their own regexes.
  - **Settings**: the Presets card (now -> new preview, Undo) is first; the old "Quick presets" card and `applyPreset` are gone (one preset system, `architect.py preset`). The recommendations card, every schema card and the JSON box sit under one `Advanced` details, closed by default (state remembered). A button leads to Effective settings, whose layer chips now read in plain words.
  - **Lessons** (Help, 5th sub-tab): `GET /api/arch/lessons` = arch_lessons.load()+check(); each target is a green chip when present, red when missing (words for screen readers); samples so far from the tier SFT files' `meta.lesson` ("not counted yet" when no file carries lesson tags).
  - **Last night** (Home, above Recent runs): `GET /api/arch/overnight`: samples by lesson, pass rate with a Wilson 95% range against the previous one (basis says "holdout" only when an eval is labelled holdout, else "latest runs"), fastest worker by measured seconds per passing task (arch_costmodel), empty state with a "Set up a data night" button.
  - **Data night** (Runs > Queue): one `Start data night` (End time 07:00, Worker, Teacher on, Curriculum on, optional most tasks, ETA and expected passing tasks), `Stop safely` while it runs (`POST /api/arch/datanight/stop` writes STOP; start removes an old STOP), live state with announcements. **Train the worker** (`tools/train_worker.py`: dataset export, then train_lora dry run, or `--real` with a checkbox and a confirm); disabled with the reason in text under 10 samples (dry) / 50 (real).
  - **Recipes**: "Use this" now starts the run with `recipe: id` (the hub passes `--recipe ID` only when architect.py knows that flag) and Review opens by itself when the verdict arrives.
- WHY: the owner trains while asleep and needs clear, large, announced buttons; renames kept breaking regex-based tests.
- Needs elsewhere: `architect.py run --recipe ID` (add to `_flow_params`: `p["recipe"]=args.recipe`) so run.json records it; until then the run starts without the id. `arch_eval.run` should tag holdout evals with label "holdout" for the card to say holdout.
- Not checked in a real browser (390 px, focus order by hand).

## 2026-11-05 (night) — Keep/Drop hunks, background jobs, Hooks, request cache, usage, bundle, commit, set up
- WHAT:
  - **Review, per-hunk Keep/Drop**: a file with a pre-run copy (`<run>/backup/<file>`, else `created.txt` = new file, else the parent of checkpoint 1) is split into parts computed from pre-run vs the run's result (so ids are stable). Each part has a Keep/Drop toggle (`aria-pressed`, 44 px, announced in `#rvLive`). `POST /api/arch/review/hunks {root,run,file,keep:[ids]}` rebuilds the file as pre-run plus kept parts (line endings kept), keeps the run's own result once in `<run>/post/` so a dropped part can come back, and records `hunks.json`. A file with no pre-run copy is refused in words ("Accept or Undo the whole run instead"). Dropping every part of a new file removes the file (said on the card).
  - **Running now** (Runs > Queue, top): `GET /api/arch/bgjobs` = the hub's own job (tail of its output) + the queue's running/waiting tasks (tail of the newest run's trace). Elapsed time, Stop button per job (`POST /api/arch/bgjobs/stop`: hub job stops, a waiting task is cancelled, a running task gets `.arch/STOP` in its project = stops after the step). Starts/ends are announced in one polite region; the tail is `role="log"`, not live.
  - **Settings > Hooks**: list/add/remove/trust via arch_hooks (`GET/POST /api/arch/hooks`). Plain warning that a hook runs a real command; adding needs a ticked "I have read this command" box and `confirm:true`; a project's hooks show "Waiting for your yes" and trust needs a confirm dialog; editing or removing in the project file takes the yes back (the fingerprint changes), said on screen.
  - **Request cache** in `BuildFlow.run` (arch_flows): `arch_reqcache.lookup(root, goal, cfg=)` before the planner (not for the feature flow's own prompt, not on resume, not for the single-file shortcut); a hit is traced in the report ("Planner skipped: ..."); the project is snapshotted before the sprints and `store(..., validated=True)` runs only when `ok_all` (every sprint's gates passed) and the run is not a dry run.
  - **Usage**: top-bar chip with the headline (`GET /api/arch/usage`), full figures in plain words on Telemetry (Today / This week).
  - **Report bundle**: `POST /api/arch/bundle {run}`; button on Review for fail / not-sure runs. `run_flow` calls `arch_bundle.auto` after the verdict record for a non-pass run (not dry, not stopped).
  - **Commit to a branch** (after Accept): `POST /api/arch/git/commit {run}` -> arch_gitflow.commit_run + pr_summary; makes the branch when the run had none; refusals come back as `why` + `todo`.
  - **Set up this project** (Home): `POST /api/arch/onboard {root}` shows test/run commands, entry points, gotchas and the ARCHITECT.md draft; "Accept ARCHITECT.md" asks before replacing an existing file.
- Checks: arch_hub_check +17 (hunks, bgjobs, hooks, usage, bundle, commit, onboard, page markers).
- Not checked in a real browser (390 px, focus order by hand, screen reader).

## 2026-11-05 (late) — fixes from a live critique
- WHAT: (1) **Stale-server guard**: `architect_hub.py` records a code fingerprint at start (names, sizes, mtimes of architect_hub.py, model_hub.py, arch_*.py); `GET /api/arch/version` -> {fingerprint_at_start, fingerprint_now, stale}; the page polls it every 60 s while visible (and at once on any 404 from /api/arch/, including a 404 on the version call itself) and shows "The app was updated - restart the hub" with a Restart button. `POST /api/arch/restart` (token-checked, refuses while a job runs unless force) replies, then a detached helper waits for the port and starts the same command line; this process exits with os._exit (Windows-safe, no signals). (2) **Plan rows** are normalised on the server (`_plan_items`: dicts, or str(dict) via ast.literal_eval) and drawn "title · role" with a kind chip. (3) **One verdict**: `run_verdict()` reads the run's verdict record (trace, else run.json) -> pass / fail / not_sure / stopped / none; Recent runs, Runs table, Review, run bar and the Console box all use `verd()` (Passed / Did not pass / Could not check / Stopped / No result yet). (4) **Usage** reads every known project (`arch_usage.known_roots`: portfolio, hub_data/recent_roots.json, the page's recentRoots, practice, evals/runs/train|bench). (5) Home "Review" chip is "Review my code"; run-row buttons are "Review this run" with the goal in aria-label. (6) Sidebar job chip only while running or <= 10 min after. (7) Last night shows "From a 1-task check at 21:50" and "Too few runs to say" under 5. (8) Review with no run opens the newest run of this project; it leads with a large verdict, the first failing check line and "Retry with note" prefilled with it; actions sit above the files. (9) Plan up/down are 36 px, Alt+Up/Down reorders the focused step. (10) Home: Recent runs above Last night.
- Tests: arch_hub_check +14 (207), arch_selftest_bundle +4 (35). node --check OK on all three script blocks.
- Needs elsewhere: the engine's `finish` has no "stopped" verdict record; `stopped` is read from run.json status. Not checked in a real browser after this change.
