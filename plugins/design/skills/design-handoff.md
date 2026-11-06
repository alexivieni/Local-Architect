---
name: design-handoff
description: Developer handoff spec from a design
when: handoff, dev spec, spec sheet, design tokens, component props, interaction states, breakpoints
---
# Design handoff

Generate developer handoff documentation. Sources: the project's HTML/CSS/components, `hub_ui.html`, a screenshot, or the user's description. Figma extraction is "not available locally"; ask for the code or screenshot and mark gaps "not specified".

## What to include
- Visual: exact measurements (padding, margins, widths), design token references, responsive breakpoints, component variants and states.
- Interaction: click/tap, hover, transitions and animations (duration, easing), gestures.
- Content: character limits, truncation, empty, loading and error states.
- Edge cases: min/max content, international (longer) text, slow connections, missing data.
- Accessibility: focus order, ARIA labels and roles, keyboard interactions, screen reader announcements.

## Principles
1. Don't assume: if it's not specified, the developer will guess. Specify everything, or say "not specified".
2. Use tokens, not values: `spacing-md`, not `16px`.
3. Show all states: default, hover, active, disabled, loading, error, empty.
4. Describe the why: it helps developers make good judgment calls.

## Output

```markdown
## Handoff Spec: [Feature/Screen Name]

### Overview
[What this screen/feature does, user context]

### Layout
[Grid system, breakpoints, responsive behavior]

### Design Tokens Used
| Token | Value | Usage |
|-------|-------|-------|
| `color-primary` | #[hex] | CTA buttons, links |
| `spacing-md` | [X]px | Between sections |
| `font-heading-lg` | [size/weight/family] | Page title |

### Components
| Component | Variant | Props | Notes |
|-----------|---------|-------|-------|
| [Component] | [Variant] | [Props] | [Special behavior] |

### States and Interactions
| Element | State | Behavior |
|---------|-------|----------|
| [CTA Button] | Hover | [Background darken 10%] |
| [CTA Button] | Loading | [Spinner, disabled] |
| [Form] | Error | [Red border, error message below] |

### Responsive Behavior
| Breakpoint | Changes |
|------------|---------|
| Desktop (>1024px) | [Default layout] |
| Tablet (768-1024px) | [What changes] |
| Mobile (<768px) | [What changes] |

### Edge Cases
- **Empty state**: [What to show when no data]
- **Long text**: [Truncation rules]
- **Loading**: [Skeleton or spinner]
- **Error**: [Error state appearance]

### Animation / Motion
| Element | Trigger | Animation | Duration | Easing |
|---------|---------|-----------|----------|--------|
| [Element] | [Trigger] | [Description] | [ms] | [easing] |

### Accessibility Notes
- [Focus order]
- [ARIA labels needed]
- [Keyboard interactions]
```

## Local crew
The `design-handoff` workflow: four readers (visual, interaction, content, accessibility) then one writer for the spec above.

## Tips
Mention edge cases ("100 items?") and the tech stack ("React + Tailwind") for better notes.
