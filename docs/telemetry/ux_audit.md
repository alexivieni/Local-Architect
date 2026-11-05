# UX audit of hub_ui.html (read-only audit, 2026-11-05)

Line numbers refer to hub_ui.html (6108 lines) at the time of the audit. I read the shell, tokens, nav registry, Home, palette/keyboard, first-visit, page hints, the Console composer and the Models page. It was not run in a browser, so contrast numbers are estimates from the hex values and the 390px items need a visual check.

AMENDMENT: the person wants MORE colour and life. Item 24 below ("Cap colour use: one accent plus status colours") is SUPERSEDED by section V2 (colour system). Do not apply item 24; apply V2 instead.

## ROOT CAUSES (why it "feels hard")
1. Home is not a single task screen. The `homeHero()` goal box (L1642) is followed by 4 stat cards, 4 quick-launch cards and a feed. Then `overview()` (L3214) still appends `sheet()`, `getStarted()`, `quickCard()`, `teamSummary()` and a "More:" details block. That is about 6 stacked sections, and quick launch exists twice.
2. There are 34 pages behind 10 sidebar places. "Agents" alone has 6 sub-tabs, and the cap of 5 is broken by `agentsp`. Labels repeat: "Live" is both `console` and `monitor`, "Home" is both the place and the sub-tab, "Settings" is both place and sub-tab. Several Machine sub-tabs overlap: Health, Checkup, Readiness check.
3. The main flow is split across three screens that do not point at each other. You ask on Home or New run, you watch on Runs > Live (`console`), and you undo from Home's result card, `runPage` or Tune. Nothing carries you from one to the next.
4. Technical wording is exposed at the first level: tok/s, VRAM, KV cache type, MoE, tiers, harness, gates, verdict, epoch, "llama-server tiers", "Specialists".
5. Controls are in the wrong place. The sidebar footer (L1402-1410) holds six unrelated controls (job chip, Ollama chip, hardware text, Theme, Commands, Shortcuts, 4 accent dots). It has no hierarchy and is the most-looked-at area on every page.

## QUICK WINS (effort S, impact high or med), in apply order

1. Make Home just the goal box, then a recent-runs list.
 - Where: `overview()` L3214-3222 and `render()` page==="overview" L1735-ish (the `put(hb,...overview())` call).
 - Problem: Home shows about six stacked sections, so the first thing a new person sees is a dashboard and not a place to type.
 - Change: Stop appending `sheet()`, `quickCard()` and `teamSummary()` on Home. Keep `getStarted()` only while `fvState().status!=="done"`. Move the "More: one-click starters…" details to the Help place. Keep order: goal box, progress, 4 stat cards (collapse behind "Machine status" `<details>` closed by default), recent activity.
 - Impact: high. Effort: S.

2. Delete the duplicate quick-launch row.
 - Where: `homeHero()` L1650-1652 ("Quick launch" `qgrid` with New run / Run agents / Build a workflow / Check this machine).
 - Problem: It repeats the sidebar and the example chips, and it competes with the one primary action.
 - Change: Remove the `h2.hsec "Quick launch"` and `.qgrid`. If you want a shortcut, add one text link under the Start button: "Need every option? Open New run".
 - Impact: high. Effort: S.

3. Reword the Home hero copy and the primary button.
 - Where: `homeHero()` L1642-1660, `.sp-title` is not rendered at all.
 - Problem: There is no headline, so the page has no first sentence. The button just says "Start", which is vague.
 - Change: Add `h("h2",{class:"sp-title"},"What should your computer work on?")` and a `.sp-sub` line "Describe the job. Your own AI models do it on this PC, check it with tests, and you can undo it." Rename the button to "Do it" (when Chat first is on, "Open chat"). Show a hint under the button: `Ctrl+Enter to start`. Placeholder: "e.g. The login page crashes when the password is empty. Fix it."
 - Impact: high. Effort: S.

4. Make "Project" a real field with a Browse affordance and a clear empty state.
 - Where: L1645 `#spRoot` and `simpleStart()` L1618-1619.
 - Problem: The first-run blocker is "Choose the project folder first" shown as a toast. The field expects a pasted Windows path, which people do not have.
 - Change: Label it "Project folder (the code to work on)". Placeholder "Paste the folder path, e.g. C:\\Users\\you\\my-app". Add a button "Use the practice project" that calls the existing `/api/arch/practice` (as `fvStartPractice` does) so a first run needs zero typing. On a missing folder, replace the toast with an inline `.note warn` under the field, `aria-invalid="true"` on the input, and focus it.
 - Impact: high. Effort: S.

5. Stop Esc from silently stopping a run.
 - Where: global keydown L5272-5278 (`if(EV.running){e.preventDefault();cStop();return;}`) and the Console hint `h("kbd",{},"Esc"),"stop"` L1782.
 - Problem: One stray Escape (closing a menu you thought was open) kills a long run with no confirm.
 - Change: Only stop on Esc when focus is inside `#cIn` and the input is empty, or use a confirm toast: "Press Esc again within 2 s to stop". Relabel the hint "Esc twice: stop the run".
 - Impact: high. Effort: S.

6. Fix the colour and size tokens that fail contrast.
 - Where: `--faint` (L24, light `#5d6982` on `--panel #fffffd` is about 5.7:1, fine) but the dark `--faint:#8c9bb6` on `--soft:#172540` is borderline; real offenders are the tiny sizes: `.brand .sub` is 9.5px (L123), 18 rules use 9 to 11px, `--t-label:11px`.
 - Problem: Text under 12px is unreadable on a laptop, especially uppercase with letter-spacing.
 - Change: Set `--t-label:12px`. Change `.brand .sub` to `font:500 11px`, `text-transform:none`, `letter-spacing:0`. Raise `.gal-tag` (11px uppercase) to 12px. Body minimum 12px everywhere (grep `font-size:1[01]px`, 18 hits).
 - Impact: med. Effort: S.

7. Rename the nav and the duplicated labels.
 - Where: `NAV_GROUPS` L1459-1494 and `SIDEBAR` L1498-1510.
 - Problem: Two pages called "Live", sub-tab "Home" under place "Home", "Settings" under "Settings", "Time per pass" under "Cost" is not about cost.
 - Change: `console` label "Live" -> "Watch a run"; `monitor` label "Live" -> "All agents now"; `run` "New run" -> "New run (all options)"; sub-tab `overview` "Home" -> "Start"; `settings` page keep, but the place "Settings" first subtab "Settings" -> "Limits & behaviour"; `costmap` "Time per pass" -> "Speed per model"; place "Cost" -> "Speed" (it measures seconds, not money); `agentsp` place "Agents" -> "Agent teams". See the jargon table.
 - Impact: med. Effort: S.

8. Cut Agents to five sub-tabs and move the odd ones.
 - Where: `SIDEBAR` agentsp L1501, comment at L1497 says "at most 5".
 - Problem: It has 6 (agents, orchestra, fleet, monitor, map, library), which breaks the stated rule and overflows on phones.
 - Change: Move `orchestra` under Models (after Scores) or under Help, and `library` under Workflows (after Gallery). Agents then has agents, fleet, monitor, map.
 - Impact: med. Effort: S.

9. Declutter the sidebar footer.
 - Where: `<div class="sidefoot">` L1402-1410.
 - Problem: Seven mixed controls in the most visible spot, and the accent dots have no text names (only `title`) and no `aria-label`.
 - Change: Keep only the status chip and one "Appearance" button that opens the existing theme picker (`#themeBtn`). Move Commands and Shortcuts into the topbar as an icon button "?" and the existing search. Put the 4 accent buttons inside the theme picker. Give each remaining accent `aria-label="Accent: Blueprint"`. Hide `#sHw` and `#sLoaded` (they duplicate the top-bar `liveChip`).
 - Impact: med. Effort: S.

10. Replace the empty-state bare strings with a one-line reason plus one button.
 - Where: `loadHome` feed L1668 "Choose a project above; its runs show up here." and L1670 "No runs in this project yet.", `runPage` L3377 "No project folder yet. Open “Where, and advanced” on the left and pick one…", `models()` L3239 "No models installed. Use ollama pull <name> in a terminal."
 - Problem: They tell people what is missing but give no action; "Where, and advanced" is not a real, findable control name.
 - Change: Use `.card.empty` with an `h3` and a button. Home: "No runs yet" / "Your finished runs appear here, newest first." / button "Try the practice project". Models: "No models yet" / "Models are the AI brains that run on your PC." / button "Copy: ollama pull llama3.2" (copies to clipboard) plus link "Open Ollama download page". Runs: "Pick a project first" / button "Choose project" that calls `go('overview')` and focuses `#spRoot`.
 - Impact: med. Effort: S.

11. Give errors a consistent shape.
 - Where: `toast()` L1440 and `toast.err` usages (about 25), `loadHome` catch strings ("could not read: "+e.message), `models()` "Ollama is not running" L3238.
 - Problem: Errors disappear in 3.8 s toasts, show raw `e.message`, and the toast has `role="status"` only (not `alert`).
 - Change: `toast.err` -> `role="alert"`, duration 8 s minimum and stays while hovered/focused, and add a "Copy details" button. Message pattern: what happened + what to do. Ollama: "Ollama is not running. Start the Ollama app, then press Refresh." with a Refresh button (`refresh(true)`).
 - Impact: med. Effort: S.

12. Make the live chip readable by humans.
 - Where: `liveChip()` L1684-1712.
 - Problem: "GPU 31% · VRAM 4.1/6.0 GB · Ollama holding 3.2 GB" plus an "Unload" button with no consequence stated.
 - Change: Text "Graphics card 31% busy · 4.1 of 6 GB memory used". If Ollama holds a model: "A model is loaded (3.2 GB)" and the button "Free memory". Add `aria-label` on the button "Free graphics memory by unloading models".
 - Impact: med. Effort: S.

13. Add visible loading states.
 - Where: `loadHome` (stat cards start as "–" with empty sub), `setStat`, feed "Reading recent runs…", `render()` branches that call `loadX()` with no skeleton.
 - Problem: "–" means both "loading" and "unknown", so the page looks broken for 1 to 2 s.
 - Change: Initial stat state `data-tone="loading"` with a shimmer bar (`.statcard[data-tone=loading] b{color:transparent;background:linear-gradient(90deg,var(--soft),var(--line),var(--soft));background-size:200%;animation:sh 1.2s infinite;border-radius:6px}`; respect the existing reduced-motion block). Reserve "–" for "unknown" only.
 - Impact: med. Effort: S.

14. Fix the result card so the next step is obvious.
 - Where: `simpleDraw()` L1632-1637.
 - Problem: Three equal buttons (Undo this change, What happened step by step, See all the details). "It worked: every check passed." is plain text with no icon, and a pass looks like a fail card except for border colour.
 - Change: Add a status icon and bigger headline; make "Undo this change" a ghost/danger-outline, "See the details" (rename) the secondary, and add a primary "Keep it" (sets store flag, collapses the card) or "Start another". Show what changed: "3 files changed" if `EV.verdict.files` exists, with a "Show changes" link to `logbook`. Add a confirm before Undo (the Undo in `runPage` L3377 already confirms; this one does not).
 - Impact: high. Effort: S to M.

15. Put the keyboard hints where people type.
 - Where: Home textarea `#spGoal` L1643 (no hint) and Console `.hint` L1778-1788 (five hints in a row).
 - Problem: Home has Ctrl+Enter but nothing says so; Console shows five key hints that crowd the composer on a phone where they are useless.
 - Change: Home: `<small class="mute">Ctrl+Enter to start</small>` right-aligned in `.hero-row`. Console: keep Enter/Shift+Enter, move the rest into the Shortcuts dialog; hide `.hint .pair` for (pointer:coarse).
 - Impact: med. Effort: S.

16. Make number-key navigation honest.
 - Where: keydown handler L5290 `n>=1&&n<=TABS.length` with 10 places but keys 1-9 only.
 - Problem: Settings and Help are unreachable by number, and bare digits fire while you read a page (accidental navigation).
 - Change: Use `g` then a letter chords (Linear style: g h Home, g r Runs, g m Models), or require Alt+digit. List them in `openKeys()`. Disable bare digits.
 - Impact: low to med. Effort: S.

17. Dismissible "What is this?" hints should not repeat.
 - Where: `pageHint()` L5537, `HINTS` L5509.
 - Problem: Each page greets with a banner starting "What is this? <Title>". On a second-level tab it appears again, and the label "Got it" is fine but "Tell me more" opens a modal.
 - Change: Show it once globally (key `hint.seen`) after the first page, then only as a "?" icon beside the page title (`aria-label="About this page"`). Rewrite the lead-in to the description alone, no "What is this?".
 - Impact: low to med. Effort: S.

18. Make the mobile nav behave the way the log claims.
 - Where: CSS L239, L626 and L1066-1073 (three older rules make the sidebar a wrapping or scrolling row) versus the end-of-file `body.nav-open` block (log item 7).
 - Problem: Conflicting rules; at 390px a row of 10 places plus the Theme/Commands/Shortcuts/accent footer can render above the content, pushing the goal box off-screen.
 - Change: Delete L239's `aside{...flex-direction:row;flex-wrap:wrap}`, L626 and L1066-1073 and keep one drawer: `aside{position:fixed;inset:0 auto 0 0;width:min(86vw,300px);transform:translateX(-100%)}` `body.nav-open aside{transform:none}` plus a `.veil` backdrop that closes it, and `aria-controls="nav"` on `#navToggle`. Verify visually at 390px.
 - Impact: high on mobile. Effort: S to M (needs a visual check).

19. Subtab strip needs a scroll cue and an active-tab scroll-into-view.
 - Where: `.subtabs` L129-135 and `buildNav` L1552 (`sb.querySelector("[aria-current=page]")`).
 - Problem: At 390px the pills scroll horizontally with only a 4px scrollbar; the active one can be off-screen.
 - Change: Call `on.scrollIntoView({inline:"center",block:"nearest"})`; add a right-edge fade `mask-image:linear-gradient(to right,#000 85%,transparent)` when scrollable.
 - Impact: med. Effort: S.

20. Fix the "Chat first" checkbox.
 - Where: `homeHero()` L1647.
 - Problem: A checkbox that silently changes what "Start" does is a hidden mode.
 - Change: Replace with two clear buttons: primary "Do it" and secondary "Talk it through first" (calls `go("studio")`). Remove the persisted `chatFirst` flag.
 - Impact: med. Effort: S.

## MEDIUM-EFFORT CHANGES

21. Merge the three "is my machine OK" pages into one "Health" page with sections.
 - Where: `SIDEBAR` machine place L1507 (advisor, doctor, assess, computers, tune); `doctorPage`, `assessPage`, `advisorPage`.
 - Problem: Health, Checkup and Readiness check answer one question three ways.
 - Change: One page "Health" with a single "Run checkup" button and result groups "Needs fixing / Fine / Could be faster". Keep old ids as `LEGACY` aliases. Keep Hardware and Tune as separate.
 - Impact: high. Effort: M-L.

22. Add a progress strip that survives navigation (ask -> watch -> result -> undo).
 - Where: `#fvDockSlot` L1417 and `fvDockShow` L5442 (first-visit only).
 - Problem: Once you start a run from Home you can wander off, and the run is only visible as a small sidebar `#sJob` chip.
 - Change: Generalise the dock: while `EV.running` on any page, show a sticky bar under the topbar "Working on: <goal> · step 3 of ~6 · Watch · Stop"; when the verdict arrives, "Done: it worked · Review · Undo". Reuse `renderStrip` data.
 - Impact: high. Effort: M.

23. Raise the typography scale discipline.
 - Where: `:root` L38 defines 11 sizes (11, 12, 12.5, 13, 13.5, 14, 15, 16, 17, 20, 26) and most rules use hard-coded px or rem instead.
 - Problem: There are 11 near-identical steps, so hierarchy reads as noise; h1 is 24px, h2 17px, body 15px, card sub 13.5px.
 - Change: Reduce to 6 tokens: `--t-xs:12px; --t-sm:13px; --t-body:14.5px; --t-lg:17px; --t-xl:22px; --t-2xl:28px`. Page h1 `--t-xl`/600, card h2 `--t-lg`, `.sub` and `.small` `--t-sm`. Sweep the 18 hard-coded px sizes and the `.sp-*` rem values. (Refined by V4 below; V4 takes precedence.)
 - Impact: med. Effort: M.

24. SUPERSEDED by section V2 (colour system): the person wants MORE colour and life. Do NOT cap colour to one accent. Original text, kept for the record only: "Cap colour use: one accent plus status colours. Where: tokens L25-31 (`--llm`, `--code`, `--judge`, `--claude`, `--infra`, `--sec` plus four section hues L93-94, L1140-1149) and `.chip` variants. Problem: About 10 hues compete (section hue, accent, role colours, chips with .good/.warn/.bad/.info/.acc), so nothing is the focal point; every list row has multiple coloured chips. Change: Chips default to neutral; only verdict chips coloured. Drop the per-section hue tints and the underline `::after` on active sub-tabs (L1149) so the accent is the single highlight." What survives from it and is compatible with V2: category hues are used for identity (icon tint, card edge, avatar ring, category chip) and not on every chip; verdict chips keep good/warn/bad colours; plain `.chip` stays neutral.
 - Impact: n/a. Effort: n/a.

25. Icons: add labels to icon-only controls and keep one icon set.
 - Where: `icon()` L1428 draws from the SVG sprite; `a11yPass` L1531 patches missing names with "Button" as the fallback.
 - Problem: A fallback label of "Button" is useless to a screen reader; some controls are just "×" or arrows.
 - Change: Fail loudly in dev (`console.warn` when the fallback fires) and fix the callers; require `aria-label` in the helper `h()` for buttons whose text is a single glyph. Use the `icon()` sprite instead of glyphs "✓ ● ! ?" in `.sp-ico` (L1630) so the step icons are the same family.
 - Impact: low to med. Effort: M.

26. Information density on the Models page.
 - Where: `models()` L3226-3241 and `modelCard()` L3243.
 - Problem: The top card is a form of hardware inputs (VRAM, RAM, KV cache type, GPU GB/s, RAM GB/s) that nobody should edit first. Each model card shows a context table, bars, family notes, JSON sampling.
 - Change: Collapse "This computer" into a `<details>` titled "Hardware assumptions (auto-detected)". Per model show one row: name, size, "Fits your graphics card / Partly on RAM / Too big", speed, one button "Use". Put the context table and notes under "Details".
 - Impact: med. Effort: M.

27. Motion polish: respect user preference everywhere and calm entry animations. (Refined by V5 below; V5 takes precedence.)
 - Where: `.card{animation:cardIn}` L149, `#view` animation reset in `go()` L1556, six separate `prefers-reduced-motion` blocks (L267, 375, 557, 589, 864, 955, 1048).
 - Problem: Every card animates on every visit and every state refresh that rebuilds a page, which feels laggy; reduced-motion handling is scattered.
 - Change: Animate only `#view` once per `go()` (opacity only, 120 ms, `var(--fast)`), remove per-card `cardIn`, keep a single reduced-motion block using `--fast:0s`. Toast should fade, not translate.
 - Impact: low to med. Effort: M.

28. Focus and ARIA consolidation.
 - Where: `:focus-visible` is defined 8 times (L105, 170, 569, 859, plus 3px `!important` in the a11y block ~L1100) and sizes differ (2px/3px, offset 1/2px); `.palette` `aria-modal` dialog has no focus trap; `#toast` is `role=status`; `.card[role=button]` is injected by `a11yPass` for any `[onclick]` element.
 - Problem: Inconsistent focus rings, no focus return after closing the palette/help, and auto-added `role=button` hides non-semantic click targets rather than fixing them.
 - Change: One rule `:focus-visible{outline:2px solid var(--acc);outline-offset:2px}`; remove duplicates. On `closePalette()`/`closeHelp()` return focus to the opener. Trap Tab inside `.palette .box`. Add `aria-live="assertive"` region for errors only. Convert `a("link",{onclick})` ("report" in the feed, L1676) to real `<button class="link">`.
 - Impact: med. Effort: M.

29. Add a skeleton "first run" path on empty install.
 - Where: first-visit `FV_STEPS` L5423 (5 stops) and `getStarted()` L3169.
 - Problem: First-run is a 5-step wizard shown beside a dense Home; skipping it leaves the Home full of "–" cards.
 - Change: Reduce to 3 stops (This computer -> Practice task -> Result) and make the practice task the Home empty state (see item 4). Auto-open only when `fvState()` is empty and no project is set.
 - Impact: med. Effort: M.

30. Separate "pro" pages from everyday ones.
 - Where: `SIDEBAR` as a whole; Help place holds only `components` ("Interface parts", a design-system page).
 - Problem: Internal pages (Interface parts, Training data, Orchestration, Map, Time per pass) sit on the same level as Home and Runs.
 - Change: Add a Settings toggle "Show advanced pages" (default off) which hides: `components`, `train`, `orchestra`, `map`, `costmap`, `assess`, `caches`. Everything stays reachable through the Ctrl+K palette. Rename Help place to "Help" with real content (shortcuts, glossary, "How it works" moved from overview()).
 - Impact: high. Effort: M.

## JARGON -> PLAIN WORDS

| Now (where) | Replace with |
|---|---|
| tok/s, "Speed" stat (L1656, 1678) | "Words per second" with sub-line "how fast your model writes" |
| VRAM, "GPU free" (L1656) | "Graphics memory free" |
| VRAM (GB) / RAM (GB) inputs, GPU GB/s, RAM GB/s (L3231-3235) | "Graphics memory (GB)", "Memory (GB)", "Graphics card speed", "Memory speed" (advanced, collapsed) |
| KV cache type (L3234) | "Memory use for long chats" (options: Normal / Smaller / Smallest) |
| Ollama holding 3.2 GB / Unload (L1707, 1711) | "A model is loaded (3.2 GB)" / "Free memory" |
| Ollama (sidebar chip, L1604) | "Local AI engine (Ollama)" on first appearance, "Ollama" after |
| Verdict / pass / fail / unsure (L1634) | "Result: worked / did not work / could not check" |
| Gates, checks (Settings, HINTS) | "Safety checks" |
| Tiers & roles (L1503) | "Which model does what" |
| Specialists (L1503, roles) | "Jobs" (subtitle "23 jobs, each can use a different model") |
| Installed (models page) | "My models" |
| MoE / Mixture-of-experts (L3253) | "Big model that only uses part of itself at a time" |
| Quantisation / quant chip (`m.quant`) | "Compression level" |
| Benchmarks / Scores / Pass rate (L1504-1505) | "Tests" / "Model report card" / "Tests passed" |
| Time per pass / Cost map (L1511) | "Speed per model" |
| Orchestration (L1510) | "How agents hand off work" |
| Fleet (L1500) | "Parallel agents" |
| Map (L1500) | "Connections" |
| Briefs / Library (L1500) | "Agent recipes" |
| Workflows / Blueprints / Build / Mine / Gallery | "My workflows" / "Create" / "Examples" |
| Readiness check (L1514) | "Is this PC ready?" |
| Checkup / Health / Advisor | "Health check" (one page) |
| Tune / Tune & improve | "Optimise for this PC" |
| Harness | "Safety net" |
| Epoch, flow, route (L1736, console strip) | "run", "workflow", "chosen model" |
| Chat first / Studio | "Talk it through first" |
| Run the app (L1556 button) | "Start my project" (it launches the user's app, not Architect) |
| Interface parts / Components (Help) | "Design samples" (hide under Advanced) |
| Training data | "Saved good work (to teach your models)" |
| "local · offline" (brand sub, L1400) | "Runs on this PC" |

## RANKED ORDER TO APPLY
1) Items 1, 2, 3, 4, 5, 14, 18 (the first-run and main-flow set; biggest drop in perceived difficulty).
2) Items 6, 7, 8, 10, 11, 12, 13, 15, 20 (copy, labels, states; all S).
3) Items 9, 16, 17, 19 (S polish).
4) Items 22, 21, 30, 26, 29 (structural, M).
5) Items 23, 25, 27, 28 (system cleanup, M). Item 24 is superseded by V2. Apply the V-section (below) in the order given there; V-B, V-F, V-G, V-D, V-C slot in right after group 1, because they change the whole look at low risk.

## NOTES FOR THE ENGINEER
- `arch_hub_check.py` regex-reads `NAV_GROUPS` and fails by design on the old `TABS` literal (log, "Not done", last bullet). Keep the `NAV_GROUPS` name when relabelling.
- Keep all page ids; relabel only (item 7), and add any retired id to `LEGACY` (L1522).
- The log says the mobile sidebar is behind a Menu button, but three older CSS rules (L239, L626, L1066) still describe a wrapping or scrolling row. I did not see the end-of-file `nav-open` block, so item 18 needs a visual check at 390px before and after.

---

# ADDENDUM: Visual life (V1-V5)

Not browser-verified; the contrast figures are estimates computed from the hex values, so run them through a checker once applied.

Cause of the "blocky" feel: 88 helper paragraphs use `class:"sub"` (13.5px, grey, full card width). About 40 of them run longer than 140 characters. The cards have a single flat surface, a single accent, and chips that are all the same grey/blue. There are no avatars, thumbnails or illustrations apart from the page art in `PAGE_ART`.

## V1. Shorten copy: every dense helper paragraph
Rule: one `.sub` line is at most 12 words and one sentence. The detail goes in one of two places. Use a `title` / info icon (`icon("question")` in `.helpq`, which already exists) for the "why". Use `<details class="more"><summary>Learn more</summary>…</details>` for the "how". Apply the rule to these lines (line numbers are where the h("p",{class:"sub"}) starts):

| Line | Page | Now (starts) | Short copy (<=12 words) | Detail goes to |
|---|---|---|---|---|
| 2333 | Agents, cloud | "Your crew of Managed Agents runs on Anthropic's side…" | "Cloud agents work in their own sandbox." | Learn more |
| 2336 | Agents, schedule | "Give an agent the same job every day…" | "Repeat a job daily, or run it once." | tooltip: "Runs while this app is open." |
| 2339 | Agents, Claude Code | "The real Claude Code agent, working in your project folder…" | "Claude Code works in your project folder." | tooltip: billed to your API key |
| 2342 | Agents, helpers | "Specialists Claude Code can hand work to…" | "Helpers Claude Code can hand work to." | Learn more |
| 2508 | Tiers | "Each installed model was measured against this card…" | "Models measured against your graphics card." | Learn more |
| 2575 | Queue | "Add tasks and let it work through them…" | "Add tasks. They run one by one." | tooltip: daily limits apply |
| 2708 | Studio | "Ask anything about your project: answers come from its own files…" | "Ask about your project. Nothing changes." | tooltip |
| 2729 | Overnight/Queue | "Goals the team works through in order…" | "Goals run in order, on their own." | Learn more |
| 2811 | Get ready | "What Get ready does, step by step: repair, a model…" | "Get ready sets up this PC in five steps." | steps list stays, behind Learn more |
| 2834 | Roles | "From your own runs: how often each role's reply passed…" | "How often each job passed its checks." | tooltip |
| 2880 | Context | "A small context window only carries a few files…" | "Small models read few files, so ranking matters." | Learn more |
| 2896 | Sampling | "Each model family has sampling its makers recommend…" | "Recommended settings for each model family." | Learn more |
| 2922 | Models (Specialists) | "Everything installed here, whatever the family (merges, abliterated…" | "Every model installed on this PC." | Learn more |
| 2968 | Fake-code check | "Stubs, tests that assert nothing, always-true assertions…" | "Finds code that only pretends to work." | Learn more |
| 2977 | CI check | "Checked by code against this project: pinned actions, permissions…" | "Checks your project's automation for risks." | Learn more |
| 2987 | Self-improve | "It edits a copy, never the running app. The copy must compile…" | "Edits a copy, never the running app." | Learn more |
| 3062 | New project | "Pick what you are making, say what it should do, give it a name…" | "Pick a type, describe it, name it." | Learn more |
| 3219 | Home "How it works" | "Workflows call specialised models. Code checks…" | "Models do the work. Tests decide if it passed." | Learn more |
| 3236 | Models, computer | "Fit = weights + context memory, spread over layers…" | "How well each model fits this PC." | Learn more (formula) |
| 3271 | Roles | "Each role lists the test categories it depends on…" | "Each job lists the tests it relies on." | tooltip |
| 3331 | Blueprints | "Current practice is to start with the simplest workflow…" | "Start with the simplest workflow that works." | Learn more |
| 3440 | Launch | "Starts the project's own run command as a real process…" | "Starts your project like you would yourself." | Learn more |
| 3486 | Doctor | "Real probes, not assumptions: a real HTTP call to Ollama…" | "Real tests of this PC, not guesses." | Learn more |
| 3533 | Assess | "Reads your real hardware and the Scoreboard's measured…" | "Is this PC ready for each kind of work?" | Learn more |
| 3551 | Tune | "Keeps this specific card/RAM from thrashing…" | "Keeps your graphics card from overloading." | Learn more |
| 3574 | Advisor | "Combines the Doctor's real environment checks…" | "Your next steps, ranked by impact." | Learn more |
| 3895, 4084 | Bench empty | "No live benchmark run yet. architect.py eval --set representative measures…" | Title "No tests run yet" + button "Run a quick test" (command text moves into a copyable `<code>` under Learn more) | Learn more |
| 4269 | Logbook | "Every try of every step, read from the run's own trace…" | "Every try, with its cost and result." | Learn more |
| 4280 | Portfolio | "List each app once: where it lives, how to test it…" | "List each app once." | Learn more |
| 4442 | Components | "Every part of the interface in the light and the dark theme…" | "Every interface part, light and dark." | none |
| 4612, 4615 | Training | "Every model call is traced and later labelled by…", "train_lora.py has not been run on real hardware…" | "Good work becomes training data." and a `.note warn` "Experimental: read train_lora.py first." | Learn more |
| 4875 | Computers | "Got another PC or a gaming rig with a graphics card?…" | "Add another PC to share the work." | Learn more |
| 4927 | Build | "Draw your own workflow: drag steps in, put them in order…" | "Drag steps in, set the order, save." | Learn more |
| 5727 | Fleet | "Each agent gets a name, a brief, the files it owns and a spending cap…" | "Each agent owns its own files and has a cap." | Learn more |
| 5833 | Monitor | "Read from the agents' own records on this machine, every 5 seconds…" | "Live view of every agent. Refreshes every 5 s." | none |
| 5887 | Map | "Pick a run, a fleet, a workflow or a Claude Code run. Arrows go in order…" | "Pick something to see how it connects." | Learn more |
| 6051 | Logbook | "Your models' runs, Claude Code runs and fleets, read into one logbook…" | "Everything your models and Claude did." | Learn more |

Component to add once (S): a helper `learnMore(summary,...kids)` returning `<details class="more"><summary>Learn more</summary><div class="sub">…</div></details>`, with CSS `.more>summary{cursor:pointer;color:var(--acc);font-size:var(--t-sm);list-style:none}.more>summary::before{content:"+ "}.more[open]>summary::before{content:"− "}.more>div{max-width:62ch;margin-top:var(--s2)}`. Also add `.sub{max-width:62ch}` globally so no paragraph exceeds a readable line. Also shorten the page subtitles in `NAV_GROUPS` (L1459-1494) to <=12 words. The worst offenders are `logbook`, `agents`, `fleet`, `monitor`, `launch` (rows 4th field), and `#pageSub`.

## V2. Colour system (tokens, light + dark, AA)
This supersedes item 24. Keep the existing `--acc`, `--good/warn/bad` and the theme/accent switcher. Add these tokens. Hues are for category identity only (icon tint, card top edge, avatar ring, chip), never for body text. Text on a tint always uses the `-ink` token.

Light (`:root`, add after L40):
```
--c-flow:#2f6fe0;   --c-flow-ink:#1f4fb0;   --c-flow-bg:#e6eefc;
--c-agent:#7a4fd6;  --c-agent-ink:#5a35b0;  --c-agent-bg:#efe9fb;
--c-template:#0d8a6f;--c-template-ink:#0a6a55;--c-template-bg:#dff3ee;
--c-model:#c4467d;  --c-model-ink:#9a2d60;  --c-model-bg:#fae6ef;
--c-cost:#c77712;   --c-cost-ink:#7e4a00;   --c-cost-bg:#fcefd6;
--c-system:#4d6a8f; --c-system-ink:#3a536f; --c-system-bg:#e8eef5;
--tint-card:color-mix(in srgb,var(--hue,var(--acc)) 6%,var(--panel));
--tint-hover:color-mix(in srgb,var(--hue,var(--acc)) 12%,var(--panel));
--grad-hero:linear-gradient(135deg,#1549c9 0%,#5a43d6 55%,#c4467d 120%);
--grad-hero-soft:linear-gradient(135deg,color-mix(in srgb,var(--acc) 14%,var(--panel)),color-mix(in srgb,var(--c-agent) 10%,var(--panel)));
--on-grad:#ffffff;
```
Dark (put the same names inside both existing dark blocks, L41-48 and L49-56, plus `[data-theme=midnight]` L59-66):
```
--c-flow:#7da2ff;   --c-flow-ink:#a9c0ff;   --c-flow-bg:#16264a;
--c-agent:#b79cff;  --c-agent-ink:#cdbbff;  --c-agent-bg:#251c47;
--c-template:#4fd1a5;--c-template-ink:#8be3c4;--c-template-bg:#0f2f27;
--c-model:#f08cc6;  --c-model-ink:#f6b2d9;  --c-model-bg:#35172a;
--c-cost:#f2b255;   --c-cost-ink:#f7cb86;   --c-cost-bg:#33260f;
--c-system:#8fa8c8; --c-system-ink:#b4c5dc; --c-system-bg:#1a2536;
--grad-hero:linear-gradient(135deg,#1a2c66 0%,#3b2a7a 55%,#6e2a55 120%);
--on-grad:#f4f7ff;
```
Contrast (estimates): each `-ink` on its `-bg` is >=4.5:1 (light: ink around #1f4fb0 on #e6eefc is about 7:1; dark: #a9c0ff on #16264a is about 8:1). `--on-grad` white on the light gradient start `#1549c9` is about 7:1, and on the end `#c4467d` about 4.6:1, so cap gradient text to the first 70% (`background-size:140%`) or keep hero text on the left where it is the bluest. `--c-*` (the saturated ones) are only for graphics, 3:1 minimum (icons, borders, avatar rings), which they meet on both panels.

Usage (assign in the registry and card builders):
- Section hues L93-94 become: Work = `--c-flow`, Agents = `--c-agent`, Models = `--c-model`, Workflows templates/gallery = `--c-template`, Cost/speed = `--c-cost`, System/Settings/Help = `--c-system`. Set `--hue` per page via `body[data-sec=…]` (L1140-1143, extend it to all 10 places).
- Chips: `.chip.flow{background:var(--c-flow-bg);color:var(--c-flow-ink)}` and the same for agent/template/model/cost/system. Plain `.chip` stays neutral.
- Status stays on the existing `--good/--warn/--bad` plus `-bg`; add `--live:#16a34a` (dark `#4ade80`) for the pulsing "running" dot only.
- Hero: `.hero{background:var(--grad-hero-soft);border-color:color-mix(in srgb,var(--acc) 25%,var(--line))}`. Use the full `--grad-hero` only on the Home banner and the first-visit card, with `color:var(--on-grad)`.
- Remove the page-background radial wash `body{background:radial-gradient…}` L99-101, or keep it at 4% so tinted cards do not clash.

## V3. Where icons, illustrations, avatars, thumbnails go
| Place | What | How |
|---|---|---|
| Agent cards (Agents, Fleet, Monitor rows) | Avatar: 36px circle, initial letter or glyph, ring in role hue | `h("span",{class:"avatar",style:"--hue:var(--c-agent)"},initial)`; CSS `.avatar{width:36px;height:36px;border-radius:50%;display:grid;place-items:center;font:600 14px var(--font-head);background:var(--c-agent-bg);color:var(--c-agent-ink);box-shadow:0 0 0 2px var(--panel),0 0 0 3px var(--hue)}`; Claude agents use `--claude-bg/--claude-ink` and a small cloud badge; a `.dot.busy` pulse at its corner when running |
| Model rows and cards (Models, Tiers) | 32px rounded-square badge with family initial (Q, L, G…) in `--c-model` | same `.avatar` shape, `border-radius:10px` |
| Workflow/template cards (Gallery, Mine, Library, New project) | 56px tall thumbnail strip: the step chain drawn as 4 to 6 small dots joined by a line, in `--c-flow` or `--c-template`; kind icon top-left | tiny inline SVG built from the step list, `.thumb{height:56px;border-radius:var(--rs);background:var(--c-flow-bg)}`; card top edge 3px `--hue` |
| Stat cards (Home) | Leading 28px icon in a tinted square, and a 7-point sparkline under the number for pass rate and speed | `.statcard .ic-box{background:var(--c-flow-bg);color:var(--c-flow-ink);border-radius:8px;padding:6px}`; sparkline from `/api/arch/evals` runs |
| Empty states (everywhere an `.empty` is used, L199 and L844) | 96px illustration from `PAGE_ART` (already exists) above a title, a one-line reason, and one button | `.empty` becomes a grid: art, `h3`, `p.sub`, `.btn.p`; art is tinted with `--hue` at 18% |
| Result card (Home, `simpleDraw`) | Large status icon: check-circle in `--good`, x-circle in `--bad`, alert in `--warn`, 40px, with a soft ring animation once | replaces the plain bordered box |
| Sidebar | Active place icon tinted with its `--hue`; inactive icons stay `--mute` | `.nav[aria-current=page] .ic{color:var(--hue)}` |
| Feed rows (Recent activity) | 28px icon for kind (run, fleet, workflow) plus the verdict chip on the right | replaces the bare chips at L1674 |
| Topbar | The brand mark gets the gradient: `.brandmark{background:var(--grad-hero)}` | S |

## V4. Type scale, weights, spacing rhythm
Replace the 11-step scale with these (rename `--t-*`, keep old names as aliases to avoid a big sweep). This supersedes the token list in item 23:
```
--t-display:32px/1.15 700   /* Home headline only, letter-spacing:-.02em */
--t-title:22px/1.25  650    /* page h1, letter-spacing:-.015em */
--t-head:17px/1.3    600    /* card h2 */
--t-body:14.5px/1.55 400    /* paragraphs, inputs */
--t-label:13px/1.4   550    /* buttons, labels, tabs, chips at 12.5 */
--t-caption:12px/1.4 400    /* helper, meta; never smaller */
--t-mono:12.5px/1.5         /* code, ids */
```
Aliases: `--t-xs:var(--t-caption)`, `--t-sm:13px`, `--t-s:13px`, `--t-ms:13.5px`→13px, `--t-m:14.5px`, `--t-l:17px`, `--t-card:17px`, `--t-xl:22px`, `--t-2xl:32px`. Weights: 400 body, 550 labels, 600 headings, 700 display only. Numbers in stat cards: `font:700 28px/1 var(--font-head);font-variant-numeric:tabular-nums`. Eyebrow labels (section names in the sidebar): 12px, 600, `letter-spacing:.04em`, uppercase only there.

Spacing rhythm (4px base, already `--s1..--s6`): add `--s7:48px`. Rules: card padding `var(--s5)` (24) on desktop, `var(--s4)` (16) on phones (currently 18/20); gap between cards `var(--s4)`; between sections `var(--s6)`; inside a card, heading to content `var(--s3)`; chip gap `var(--s2)`. Radius: cards `--r:14px`, controls `--rs:10px`, pills `--r-pill`. Use one shadow elevation per state: rest `--shadow`, hover `--lift`. Max readable line `62ch` for any text block.

## V5. Tasteful motion (all behind reduced-motion)
This supersedes item 27. Tokens: `--fast:.12s; --base:.2s; --slow:.4s; --ease:cubic-bezier(.2,.7,.2,1)` (already exist); add `--spring:cubic-bezier(.34,1.56,.64,1)`.
- Hover lift: `.card[role=button],.qcard,.gal-card,.tcard{transition:transform var(--base) var(--ease),box-shadow var(--base) var(--ease),border-color var(--fast)}` with `:hover{transform:translateY(-2px);box-shadow:var(--lift);border-color:color-mix(in srgb,var(--hue,var(--acc)) 45%,var(--line))}` and `:active{transform:none}`. Plain read-only cards do not lift (L795 already says so).
- Progress shimmer: for running steps and loading stats: `@keyframes shimmer{to{background-position:-200% 0}}` `.shimmer{background:linear-gradient(90deg,var(--soft) 25%,color-mix(in srgb,var(--acc) 12%,var(--soft)) 50%,var(--soft) 75%);background-size:200% 100%;animation:shimmer 1.4s linear infinite}`. Apply to `.statcard[data-tone=loading] b`, the active `.sp-step.now .sp-ico`, and the strip progress bar fill.
- Live dot: `@keyframes pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--live) 55%,transparent)}100%{box-shadow:0 0 0 8px transparent}}` on `.dot.busy` every 1.6 s.
- Page entry: only `#view` fades and rises 4px in `--base` once per `go()`; remove per-card `cardIn` (L149).
- Result reveal: the status icon scales in with `--spring` (0.8 to 1) once, 300 ms.
- Button press: `.btn:active{transform:scale(.98)}`.
- Toast: opacity plus 8px rise, 160 ms.
- One reduced-motion block replaces the seven scattered ones: `@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important;scroll-behavior:auto!important}.shimmer{animation:none;background:var(--soft)}}`.

## Ranked additions to the main list (impact / effort)
- V-A. Add `learnMore()` plus `.sub{max-width:62ch}` and shorten the 40 paragraphs above: high / M (mechanical, a table is given).
- V-B. Add the colour tokens, chip variants and per-place `--hue`: high / S.
- V-C. Home hero: `--grad-hero-soft` surface, `--t-display` headline, stat cards with icon box and sparkline: high / S to M.
- V-D. Empty states rebuilt as art + title + one line + button: high / S (one CSS change plus a helper).
- V-E. Avatars for agents and models; thumbnails for workflow cards: med / M.
- V-F. Type scale collapse with aliases: med / S (aliases mean no sweep needed).
- V-G. Motion tokens and a single reduced-motion block: low to med / S.

Order: V-B, V-F, V-G, V-D, V-C first (all token or CSS level, low risk, and they change the whole look at once); then V-A, V-E.
