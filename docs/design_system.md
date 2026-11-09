# The hub's design system

One stylesheet in `hub_ui.html`, one token set at the top of it, one page that shows every part in both themes. Checked by
`python arch_selftest_design.py` (and still by `arch_selftest_hubcalm.py`, whose pinned rules are part of the sheet).

## How the sheet is laid out
1. **Tokens** (from `<style>` to the line `end of tokens`): a light block (`.th-light,:root`), the dark block written twice
   (the system setting, `:root:not([data-theme=light])` inside the dark media query, and the explicit choice,
   `.th-dark,:root[data-theme=dark]`; the two must be identical), the accents, and two derived tokens.
2. **Base and shell**, then the components and pages in the order they were built, then the workspace and first-visit sections,
   then the Components page, the accessibility rules and the single reduced-motion rule, last.

Rules: no literal colour below the token line (a new colour is a new token, light **and** dark). Earlier layers
(identity, polish, the calm layer, flagship) each redefined tokens further down; those `:root` blocks are gone. The test fails
if one comes back. `.th-light` / `.th-dark` scope a theme to one subtree; that is how the Components page shows both at once.

## Tokens
**Surfaces and text**: `--bg` page, `--paper` the drafting table behind it, `--panel` cards, `--soft` inner boxes and chips,
`--line` hairlines, `--ink` text, `--mute` secondary text, `--faint` labels (AA on a card in both themes).
**Accent**: `--acc`, `--acc2` (its soft fill), `--on-acc` (text on a filled accent). Accents: blueprint (default),
graphite, copper, moss. Each sets `--acc --acc2 --tick --grid --grid2` in light and dark, and never a status colour: `--info`
stays the theme's own blue (copper and moss turned every info note orange or green, which read as a warning or a success).
The test fails any accent rule that sets `--info`, `--good`, `--warn` or `--bad`.
**States**: `--good --goodbg`, `--warn --warnbg`, `--bad --badbg`, `--info --infobg`, `--sec --secbg` (security),
`--infra --infrabg`.
**Kinds of work**: `--llm` (a model step), `--code` (a code check), `--judge` (a reviewer; moved off orange),
`--claude --claude-ink --claude-bg`. The orange means Claude and nothing else.
**Terminal and diff**: `--term --term-ink --add --del --hunk`.
**The blueprint sheet** (Home hero, the same in both themes): `--bp --bp2 --bp-edge --bpline --bp-rule --bpink --bpmute
--on-bp --bp-wash --bp-yes --bp-no`.
**Drafting surface**: `--grid --grid2 --tick`. **Misc**: `--knob` (toggle thumb), `--veil` (behind dialogs).
**Elevation**: `--shadow` (cards; none in dark), `--lift` (a clickable card under the pointer; every theme sets its own: none in
the dark ones, warm brown in Paper, which used to borrow the light theme's navy), `--pop` (menus, the slash
list), `--modal` (dialogs, toasts), `--ring` (focus halo on inputs, derived from the accent), `--grad` (the nav marker).
**Type**: `--font` (text: Segoe UI Variable Text and system fallbacks), `--font-head` (titles and big figures only),
`--mono` (Cascadia). The scale is defined once, in rem, so it follows the browser's text size (it was px, written twice; same
sizes at the default 16px). Fifteen steps, and every font size on the page is one of them (the 2026-11-08 sweep folded about 45
literal values and nine alias names into them; the only literal left is the root size, and two inside the guarded Caches slice):
`--t-art` .46875rem (labels inside drawings), `--t-2xs` .6875rem, `--t-caption` .75rem, `--t-mono` .78125rem, `--t-sm` .8125rem,
`--t-ui` .875rem (chips, tables, small buttons), `--t-body` .90625rem, `--t-lead` .9375rem, `--t-md` 1rem, `--t-head` 1.0625rem (card title),
`--t-sub` 1.1875rem, `--t-title` 1.375rem, `--t-big` 1.625rem (big figures), `--t-hero` 1.75rem, `--t-display` 2rem (page title).
Weights: 400 body, 500 controls, 600 labels, 650 titles. Nothing ships from the web; every face is an OS font.
**Spacing**: `--s1` 4, `--s2` 8, `--s3` 12, `--s4` 16, `--s5` 24, `--s6` 32. Where a page only needs a gap it uses a spacing
class, not an inline margin: `m0`, `mt-1`..`mt-5`, `mb-1`..`mb-5`, `my-1`..`my-5` (one step of the scale each; `mt-0`, `mb-0`),
`ml-1`/`ml-2`/`mr-1`/`mr-2` (gaps between inline chips), `m-auto` (centring), and `.card.card-tight` (a one-line note card). Like
the inline styles they replaced (177 of 203; 21 of the rest are in the guarded slices), they win over a component's own margin (`!important`); the test counts the
inline spacing that is left and the number may only go down.
**Radius**: `--r` 14 (cards, panels), `--rs` 10 (controls, inner boxes, rows); defined once (the sheet said 12/8 and a later
block silently set 14/10, which is what everyone saw, so 14/10 is kept), `--r-xs` 6 (tags, code), `--r-pill`.
**Motion**: `--ease`, `--fast` .12s, `--base` .2s, `--slow` .4s.
**HUD**: `--hud-bg` (the HUD page is dark in every theme; its frame on the Speed page matches it).

## Components
Buttons come from ONE block in the stylesheet (`BUTTONS: the one block`); a page may place a button but not restyle it.
`.btn` (secondary, 34 px), `.btn.p` = `.btn.pri` (the one filled primary, 44 px), `.btn.sm` (30 px; a small primary stays 30),
`.btn.ghost` (see-through), `.btn.danger` and `.btn.ghost-danger` (red text), `.btn.link` (inline), disabled. A pressed toggle
(`aria-pressed`, the selected preset) keeps the row's height. Phone and touch: every button at least 36 px. Chips `.chip`
plus `good warn bad info acc live`, `.cbadge` for Claude. Cards `.card` (still) and `.card[role=button]` (lifts). Tabs: the
sub-tab pills `.subtabs` (`aria-current=page`) and filters `.tfilter` (`aria-pressed`). Inputs: `label.f` (label above,
`.hint` below), `label.sw` for a checkbox, `.fgrid`. Menus: `.mmbtn` + `.mmenu` / `.mmrow` / `.mmside`. Tables with `td.n`
for figures; a row that opens something uses `clickRow(fn, current)` (Tab reaches it, Enter or Space opens it, `aria-current`
marks the open one). Empty states have ONE look (`EMPTY STATES` block): `.empty`, `.st-empty` (a "No ... yet" sentence tagged by
`markState`), `.bp-empty`, `.idle` (with an icon or live dot) share the box; `emptyState()` builds the whole-page card;
`.idle.hero` and `.bp-empty` are only roomier. Loading and errors: `.st-load`, `.st-err`, `.note.*`.
Windows: ONE dialog, `modal(title, [options], ...body)`, a native `<dialog>` opened with `showModal()`: Esc, the focus
trap, the inert page and focus back to the opener come from the browser; a click on the backdrop closes it. Sizes: a form
(640 px), `wide` (a report, a timeline, what's inside a workflow), `drawer` (the page explainers, the shortcuts). The command
palette, the first-visit tour and the presets popover are the only other overlays, on purpose.
Focus: one ring, `:focus-visible` 3 px in the accent, 2 px out, no `!important`; a component may change only its offset
(menu rows pull it inward so the scrolling menu cannot clip it). The live board:
`.doing` with `.plan`, `.lane`, `.gates`, `.verdictbox`.

**The Components page** (`go("components")`; Settings > Components, or Ctrl+K) draws every one of these in a light and a
dark frame side by side, from the same classes the pages use. It is in no tab (`PAGES` entry with `"settings"` as its tab).

## Keyboard and screen readers
- A skip link is the first Tab stop; `#view` takes focus. Changing page keeps focus on the tab you used and announces the page.
- One polite live region (`#srLive`, `announce()`). Streaming model text is never read token by token: a finished step,
  check, verdict, a new reply, a moved Blueprint step are announced.
- Panels rebuilt on every poll (live board, strip, thread) put focus back where it was (`keepFocus`).
- Menus (`menuNav`): Up/Down/Home/End walk the rows, Enter or Space chooses, Right opens a side panel, Left or Esc closes it,
  Esc closes the menu and returns to its button, Tab leaves. Rows carry `menuitemradio`/`aria-checked`. Esc on a menu never
  also stops a run.
- The command list and the slash list are comboboxes with `aria-activedescendant`. Dialogs keep focus inside while open.
- Custom buttons (`role=button`) answer Space as well as Enter; plan steps open with Enter or Space and say `aria-expanded`.
- Drag and drop always has another way: Blueprint steps have up/down buttons and Alt+Up/Down, palette kinds add on click;
  Map boxes move with the arrow keys.

## Never by colour alone
Every outcome has words or a shape as well: checks say passed / failed / not sure with three icons; a check that could not run
is dashed on the Map and the request diagram; a stopped model is a hollow ring, a failed tool a square; the strip says
"model server not reachable". In forced-colours mode the status dots and bars keep their colours and controls get borders.

## Reduced motion
One rule, last in the sheet: with reduced motion every animation and transition stops and scrolling is instant (the
staggered workflow steps are shown at once). Older per-layer copies above it say the same and can go.

## Still to fold (known, counted by the test, may only go down)
Literal colours remain in a few page sections above the workspace section: the blueprint sheet's whites and rgba lines,
the toast/modal/palette/slash/drawer shadows (use `--pop` / `--modal`), the old dark model-menu colours, the Claude rgba lines
and lane colours that later rules already override, and a few `var(--x,#fallback)` fallbacks. `.card`, `.thread`, `.msg`, `.nav`, `.btn` and the empty states are one rule each (2026-11-08: each merged at the
position of its last layer, verified by a computed-style snapshot and a layout fingerprint of every page). Other selectors
still appear in several rules, mostly on purpose (one rule sets the face of a list of titles, another their weight); fold a
component's base look into one rule when touching its section. The Cost map's clickable rows sit in a
guarded byte-slice and wait for the owner's yes before they get `clickRow`.
