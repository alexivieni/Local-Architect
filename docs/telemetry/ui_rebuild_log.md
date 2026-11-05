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
