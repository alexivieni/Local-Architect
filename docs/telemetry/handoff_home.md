# Handoff Spec: Home ("one box") and the top bar

Source: design critique of 2026-11-05 (hub at commit 6bfd93c) + docs/telemetry/ux_audit.md. Tokens are the ones already defined
in `hub_ui.html` `:root` and the dark/midnight blocks; never use a literal colour outside those blocks.

### Overview
Home is where a person says what they want done and starts it. Everything that is not the question, the box, the project and the
start button is secondary and sits below the fold or behind a disclosure. Why: first-time users stalled on a page with six stacked
sections; the job is one sentence and one click.

### Layout
- Content column: `max-width: 920px`, centred, side gutter `--s4` (16px) on phones, `--s6` on desktop.
- Order (top to bottom): **headline** → **one-line helper** → **goal box with the primary button** → **"Try:" suggestion chips**
  → **project row** → **recent runs** → **stat cards** (collapsed under "This computer" `<details>`, closed by default once a run exists).
- Nothing above the headline on Home: no page-tip banner (it becomes a `?` button next to the headline), no subtab strip.

### Design Tokens Used
| Token | Value (light / dark) | Usage |
|---|---|---|
| `--t-display` | 32px, clamp(24px, 3.2vw, 32px) | Headline `#spTitle` |
| `--t-body` | 14.5px | Helper line, box text |
| `--t-label` | 13px | Buttons, chips, field labels |
| `--t-caption` | 12px | Keyboard hint, meta |
| `--c-flow-ink` → `--c-agent-ink` → `--c-model-ink` | theme inks | Headline gradient (readable in every theme) |
| `--acc` | #1549c9 / #7da2ff | Primary button, focus ring, links |
| `--panel`, `--soft`, `--line` | theme surfaces | Box background, chip background, borders |
| `--s2` 8px, `--s3` 12px, `--s4` 16px, `--s5` 24px, `--s6` 32px | spacing | chip gap / inside card / between rows / card padding / between sections |
| `--rs` 10px, `--r` 14px, `--r-pill` | radius | inputs / cards / chips |
| `--fast` .12s, `--base` .2s, `--ease`, `--spring` | motion | see Motion |

### Components
| Component | Variant | Props | Notes |
|---|---|---|---|
| Headline `h2#spTitle` | — | text "What should your computer work on?" | Gradient text; `?` icon button at its end opens the page help (aria-label "About this page") |
| Helper `p.sp-sub` | — | max 12 words, one line at ≥1024px | "Describe the job. Your models do it, test it, and you can undo it." |
| Goal box `textarea#spGoal` | default / focus / error | placeholder "e.g. The login page crashes when the password is empty. Fix it." | min-height 96px, autosize to 240px; `Ctrl+Enter` starts |
| Primary button **Do it** | default / hover / loading / disabled | inside the box's bottom-right corner on ≥768px, full width below the box on phones | height 40px (44px on touch) |
| Secondary **Talk it through first** | ghost | — | goes to Studio with the text |
| "Try:" chips | suggestion | Fix a bug · Add a feature · Review · Explain | below the box; clicking fills the box, never starts |
| Project row | has-project / empty / invalid | combobox: recent folders (last 5, `store` key `recentRoots`) + "Use the practice project" + "Paste a path…" | remembers the last folder; invalid path shows inline `.note warn`, `aria-invalid="true"` |
| Recent runs | list / empty / loading / error | 5 newest, verdict chip, relative time, "Open" | empty: art + "No runs yet" + "Try the practice project" |
| Stat cards | collapsed group | Tests passed · Words per second · Runs this week · Graphics memory free | inside `<details>` "This computer" |

### Top bar live chip
| State | Text | Detail (tooltip / `title`) |
|---|---|---|
| idle | "5.2/6 GB" | "Graphics card 0% busy · 5.2 of 6 GB used" |
| model loaded | "5.2/6 GB · model loaded" + button **Free memory** | "A model is loaded (19.6 GB in Ollama). Free memory unloads Architect's models only." |
| busy | "GPU 74%" with live dot | same |
Max chip width 360px; `text-overflow: ellipsis`; never wraps the top bar. Why: at 1440px the long sentence was cut off mid-word.

### States and Interactions
| Element | State | Behavior |
|---|---|---|
| Do it | hover | background `color-mix(--acc 88%, black)`, lift 1px |
| Do it | loading | spinner + "Starting…", disabled, box read-only |
| Do it | disabled | when box is empty; tooltip "Type what you want done" |
| Goal box | error | `--bad` border, message below: "Choose a project first" with focus moved to the project row |
| Project row | open | listbox of recent folders; Enter selects; Esc closes |
| Chip | click | fills the box with its template text and focuses it at the end |
| Recent run | click | opens the run's Review screen |

### Responsive Behavior
| Breakpoint | Changes |
|---|---|
| Desktop (>1024px) | Do it inside the box corner; chips one row; stat group collapsed |
| Tablet (768–1024px) | same, helper may wrap to 2 lines |
| Mobile (<768px) | Do it full-width under the box; chips scroll horizontally with edge fade; touch targets ≥44px; sidebar is the drawer |

### Edge Cases
- **Empty install:** no project → project row shows "Use the practice project" as the primary choice.
- **Long goal text:** box autosizes to 240px then scrolls; no character limit, but >4000 chars shows a caption "Long requests work better split into steps."
- **Long folder path:** middle-ellipsis (`C:\Users\…\my-app`), full path in `title`.
- **Slow connection / hub busy:** recent runs and stats shimmer; after 8s show "Still reading…" with Retry.
- **Missing data:** a stat with no measurement shows "—" with caption "after your first run", never an empty card.

### Animation / Motion
| Element | Trigger | Animation | Duration | Easing |
|---|---|---|---|---|
| Page | `go()` | opacity 0→1, translateY 4px→0 | `--base` (200ms) | `--ease` |
| Do it | press | scale .98 | `--fast` | `--ease` |
| Result icon | verdict arrives | scale .8→1 once | 300ms | `--spring` |
| Stat / list loading | while loading | shimmer | 1.4s loop | linear |
All inside the single `prefers-reduced-motion` block (durations → .01ms).

### Accessibility Notes
- Focus order: headline `?` → goal box → Do it → Talk it through → chips → project row → recent runs → "This computer".
- `textarea` has a visible label (visually-hidden is not enough: "What do you want done?").
- Live chip button `aria-label="Free graphics memory by unloading Architect's models"`.
- Start/finish announced through the existing `aria-live="polite"` region; errors through `role="alert"`.
- One focus ring everywhere: `outline: 2px solid var(--acc); outline-offset: 2px`.
