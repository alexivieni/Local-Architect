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
sizes at the default 16px): `--t-display` 2rem (page title), `--t-title` 1.375rem, `--t-head` 1.0625rem (card title), `--t-body` .90625rem,
`--t-mono` .78125rem, `--t-caption` .75rem. The older names are aliases of those steps: `--t-label` / `--t-sm` / `--t-s` / `--t-ms` .8125rem,
`--t-xs` .75rem, `--t-m` .90625rem, `--t-l` / `--t-card` 1.0625rem, `--t-xl` 1.375rem, `--t-2xl` 2rem.
Weights: 400 body, 500 controls, 600 labels, 650 titles. Nothing ships from the web; every face is an OS font.
**Spacing**: `--s1` 4, `--s2` 8, `--s3` 12, `--s4` 16, `--s5` 24, `--s6` 32.
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
and lane colours that later rules already override, and a few `var(--x,#fallback)` fallbacks. Also several selectors are still
declared in more than one layer with the later one winning (`.card`, `.msg`, `.thread`, `.nav`; `.btn` and the empty states
were merged on 2026-11-08); merge them into one rule each when touching that section. The Cost map's clickable rows sit in a
guarded byte-slice and wait for the owner's yes before they get `clickRow`.
