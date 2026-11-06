---
name: accessibility-review
description: WCAG 2.1 AA accessibility audit of a page or design
when: audit accessibility, check a11y, is this accessible, contrast, keyboard navigation, touch target, screen reader
---
# Accessibility review (WCAG 2.1 AA)

Audit a page or design. Read the HTML/CSS/components first; report only what you saw, with file and line. A design-tool link is "not available locally": ask for the code or a screenshot. Compute contrast with `tools/contrast.py` (`ratio("#767676", "#ffffff")`), never by eye.

## Quick reference
### Perceivable
- 1.1.1 Non-text content has alt text
- 1.3.1 Info and structure conveyed semantically
- 1.4.3 Contrast >= 4.5:1 (normal text), >= 3:1 (large text)
- 1.4.11 Non-text contrast >= 3:1 (UI components, graphics)
### Operable
- 2.1.1 All functionality available via keyboard
- 2.4.3 Logical focus order
- 2.4.7 Visible focus indicator
- 2.5.5 Touch target >= 44x44 CSS pixels
### Understandable
- 3.2.1 Predictable on focus (no unexpected changes)
- 3.3.1 Error identification (describe the error)
- 3.3.2 Labels or instructions for inputs
### Robust
- 4.1.2 Name, role, value for all UI components

## Common issues
1. Insufficient color contrast 2. Missing form labels 3. No keyboard access to interactive elements 4. Missing alt text on meaningful images 5. Focus traps in modals 6. Missing ARIA landmarks 7. Auto-playing media without controls 8. Time limits without extension options

## Testing approach
1. Automated scan (catches about 30% of issues) 2. Keyboard-only navigation 3. Screen reader testing (VoiceOver, NVDA) 4. Color contrast verification 5. Zoom to 200%: does layout break? Locally you can do 1, 4 and a code reading of 2; say plainly that 3 and 5 need a person.

## Output

```markdown
## Accessibility Audit: [Design/Page Name]
**Standard:** WCAG 2.1 AA | **Date:** [Date]

### Summary
**Issues found:** [X] | **Critical:** [X] | **Major:** [X] | **Minor:** [X]

### Findings

#### Perceivable
| # | Issue | WCAG Criterion | Severity | Recommendation |
|---|-------|---------------|----------|----------------|
| 1 | [Issue] | [1.4.3 Contrast] | 🔴 Critical | [Fix] |

#### Operable
| # | Issue | WCAG Criterion | Severity | Recommendation |
|---|-------|---------------|----------|----------------|
| 1 | [Issue] | [2.1.1 Keyboard] | 🟡 Major | [Fix] |

#### Understandable
| # | Issue | WCAG Criterion | Severity | Recommendation |
|---|-------|---------------|----------|----------------|
| 1 | [Issue] | [3.3.2 Labels] | 🟢 Minor | [Fix] |

#### Robust
| # | Issue | WCAG Criterion | Severity | Recommendation |
|---|-------|---------------|----------|----------------|
| 1 | [Issue] | [4.1.2 Name, Role, Value] | 🟡 Major | [Fix] |

### Color Contrast Check
| Element | Foreground | Background | Ratio | Required | Pass? |
|---------|-----------|------------|-------|----------|-------|
| [Body text] | [color] | [color] | [X]:1 | 4.5:1 | ✅/❌ |

### Keyboard Navigation
| Element | Tab Order | Enter/Space | Escape | Arrow Keys |
|---------|-----------|-------------|--------|------------|
| [Element] | [Order] | [Behavior] | [Behavior] | [Behavior] |

### Screen Reader
| Element | Announced As | Issue |
|---------|-------------|-------|
| [Element] | [What SR says] | [Problem if any] |

### Priority Fixes
1. **[Critical fix]** - Affects [who] and blocks [what]
2. **[Major fix]** - Improves [what] for [who]
3. **[Minor fix]** - Nice to have
```

## Local crew
The `accessibility-review` workflow: four auditors (Perceivable, Operable, Understandable, Robust), one reporter in the format above, one checker that re-reads each cited line.

## Tips
Start with contrast and keyboard. Prioritize by impact. Real assistive-technology testing still catches what a code reading cannot.
