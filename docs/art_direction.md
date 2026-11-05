# The hub's art direction

Built on the design system (`docs/design_system.md`): it adds no new colour below the token line, only tokens, one icon set, one
drawing per page and a few calm signs of life. Checked by `python arch_selftest_art.py` (and still by `arch_selftest_design.py` and
`arch_selftest_hubcalm.py`). Everything is display only: no page's words, order or behaviour change, and nothing is fetched.

## The idea in one line
A drafting table: every place has a hue, every page a small line drawing, and the machine shows it is working by breathing, not by
blinking. Calm first: hue is a tint, never a fill; motion is a pulse or a count, never a loop you have to watch.

## Themes
Five looks in the side bar's **Appearance** button (a menu, not a cycle any more; the command list still has "Change appearance",
which steps to the next, and "Choose a theme", which opens the menu).

| Look | Selector | What it is |
|---|---|---|
| Auto | no `data-theme` | Light or Dark, following the computer |
| Light | `.th-light,:root` | the drafting table by day |
| Dark | `.th-dark,:root[data-theme=dark]` (and the system-dark copy) | the same table under a desk lamp |
| **Midnight** | `.th-midnight,:root[data-theme=midnight]` | near-black and cool, for a dark room |
| **Paper** | `.th-paper,:root[data-theme=paper]` | warm tracing paper |

Rules that make the two new ones safe:
- Each sets **every token the dark block sets**. Paper therefore wins over the computer's dark setting (`:root:not([data-theme=light])`
  has the same specificity and comes earlier), and Midnight never shows a light value.
- Accents: Midnight is listed in the dark accent rules (`:root[data-theme=midnight][data-accent=x]`), so it shares the dark accents.
  Paper has its own light accent rules (`:root[data-theme=paper][data-accent=x]`) placed **after** the system-dark accents, so a
  dark computer cannot hand Paper a dark accent.
- AA contrast (4.5:1) is checked by the suite for Midnight and Paper with every accent: ink, mute and faint on a card and on the page,
  every chip pair, the accent on a card and on its soft fill, text on the primary button, Claude text, the terminal.
- `.th-midnight` / `.th-paper` scope a look to a subtree, exactly like `.th-light` / `.th-dark`. The picker draws each option as a
  tiny page in its own tokens; Settings > Components shows both new looks in full under "The two curated themes".
- The first-paint script in `<head>` applies a stored look before the first frame; an unknown stored value falls back to Auto.

## Section hues
Derived tokens (in every theme scope), taken from colours the palette already has, with a reason each:

| Place | Token | From | Why |
|---|---|---|---|
| Projects | `--hue-projects` | `--code` | a project is code |
| Workspace | `--hue-workspace` | `--acc` | the main place follows the chosen accent |
| Models | `--hue-models` | `--llm` | models already wear the model colour |
| Settings | `--hue-settings` | `--infra` | settings tune the machine |

`body[data-sec=…]` and each side tab `.nav[data-sec=…]` set `--hue`. It is used for: the side-tab icon and its active marker, the
sub-tab icons and the active sub-tab's underline, the page glyph, the card-header icons, and the drawings' highlighted strokes.
Nothing else. Every hue reads at 3:1 or more on a card in every theme and accent (icons and markers are non-text, WCAG 1.4.11).
The orange still means Claude and nothing else; no hue borrows it.

## Icons
One sprite (`<svg><defs><symbol id="i-…">` right after `<body>`), drawn on a 24-unit grid with the stroke set once by `.ic`
(1.7, round caps and joins, `currentColor`); symbols carry no colour and no stroke width of their own. `icon(name, cls)` makes one.
New symbols: `folder plus crew fleet monitor map library blueprint queue rocket sprout grid badge trophy chart logbook flow sliders steth
gauge compass coin train palette midnight paper inbox`.
- **Places**: Projects `folder`, Workspace `console`, Models `chip`, Settings `sliders` (`TABS`).
- **Pages**: `PAGE_ICON` gives every page its own (sub-tabs, the page glyph beside the title, the command list).
- **Card headers**: every `.card h2` gets a small icon in the section hue, chosen by the first `HEAD_ICON` rule whose words match
  the title (security, Claude, cost, queue, checks, measurements, machine, models, training, flows, settings, agents, projects, ...),
  else the page's icon. Headings that already hold an icon, and anything a model or a program wrote (`ART_SKIP`), are left alone.

## Drawings
`ART` holds one line drawing per page, 160 x 96, a few hundred bytes each (the suite caps them at 1.5 KB), built by `art(name, cls)`.
They are inline SVG with classes only, so they follow the theme and the place:

| Class | Draws |
|---|---|
| `a-pl` | the soft plate behind the drawing (8% of the hue) |
| `a-ln` | ordinary lines (`currentColor` = `--faint`) |
| `a-hi` | the line that matters (`--hue`) |
| `a-fl` | a card or sheet (`--panel` with a line edge) |
| `a-tn` | the one tinted object (hue mixed into the panel) |
| `a-dt` | a dot in the hue |
| `a-sf` | a soft fill (`--soft`) |

The drawings: `console` (a terminal with a passed check), `task` (a request sheet), `studio` (a conversation), `crew` (three agents
and the night star), `fleet` (three lanes joining at one check), `map` (connected boxes, one dashed: could not run), `monitor` (a
live trace), `logbook` (an open book with a tick), `library` (a shelf of briefs), `blueprints` (steps on a grid with a ruler),
`queue` (a running ticket above two waiting), `models` (router, worker and heavy stacked), `settings` (sliders), `projects` (a
folder of tiles), `journey` (a dotted path to a flag: the first run).

Where they appear: at the right of every page heading (`#pageArt`, hidden under 1100 px), in empty states (`.empty`, `.st-empty`,
`.bp-empty` at least 260 px wide, that do not draw their own), on Home > Get started (`journey`) and on each step of the first-visit
tour (machine, team, practice, live board, result). Pages without their own drawing borrow their place's (`PAGE_ART`, `SEC_ART`).

## Signs of life
- **Busy**: while a run is live on the board (`EV.running`) or the side chip shows a running job, `body.is-busy` makes the Workspace
  tab's dot breathe and the page glyph pulse. The words are still there: the side chip names the job and the strip names the step.
- **Loading**: a loading sentence in a card (`.st-load`) gets two shimmering lines under it, from tokens. Existing `.skeleton`
  blocks are unchanged.
- **Count-ups**: dashboard figures (`.btile b`, `.ws-stat b`, `.qstats b`) count from the last value shown in the same place (from
  0 the first time) for about half a second, then the exact original text is put back. Unchanged figures never move, so a poll
  that rebuilds a panel does not replay it.
- **Hidden tab**: `html.tab-away` pauses every animation while the tab is not visible; count-ups write the final value at once.
  The art layer starts no timer of its own.
- **Reduced motion**: with reduced motion asked for, the design system's single rule (last in the sheet) stops every animation and transition the layer adds, and
  count-ups check `prefers-reduced-motion: reduce` and write the figure at once.

## By page
- **Every page**: hue per place; page glyph beside the title; drawing at the right of the heading; icons on sub-tabs and card headers;
  empty states drawn; loading shimmer.
- **Side bar**: tab icons Projects `folder`, Models `chip`, Settings `sliders`; hue-tinted icons and active marker; the Workspace
  tab breathes while work runs; Appearance opens the theme picker (five looks, previews in their own tokens, keyboard menu).
- **Console**: drawing `console`; its existing idle hero (`blueprintArt`) is kept as it was; the busy pulse.
- **New task / Run the app**: drawing `task`; the existing skeletons for runs and the queue are kept.
- **Studio**: drawing `studio`. **Crew**: `crew`. **Fleet**: `fleet`; its count tiles count up. **Map**: `map`. **Monitor**:
  `monitor`; its figures count up. **Logbook**: `logbook`; its figures count up. **Library**: `library`.
- **Blueprints**: drawing `blueprints`, also in the empty canvas. **Queue**: `queue`; its counts count up.
- **Projects** (Home, Start, All projects): drawing `projects`; Get started carries `journey`.
- **Models** (Your models, Benchmarks, Scoreboard, Installed, Specialists): drawing `models`; benchmark tiles count up.
- **Settings** (and Tune, Checkup, Readiness, Advisor, Workflows, Training): drawing `settings`.
- **Components**: an "Art direction" card (the four hues, every icon, every drawing) and both new looks in full.
- **Command list**: each page shows its own icon; "Choose a theme" added; "Change appearance" names all five looks.
- **First-visit tour**: a drawing on each of the five steps.

## Adding to it
A new page: an entry in `PAGE_ICON` (a symbol that exists) and, if it deserves its own, in `ART` + `PAGE_ART`; otherwise it borrows
its place's drawing. A new symbol: 24 x 24, strokes only, no colour. A new theme: a full block (every token the dark block sets), its
accent rules, a `.th-<name>` scope, an entry in `THEMES` / `THEME_LABEL` / `THEME_ICON` / `THEME_DESC`, and its pairs in the suite.
