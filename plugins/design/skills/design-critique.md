---
name: design-critique
description: Structured design feedback on usability, hierarchy and consistency
when: review this design, critique this mockup, what do you think of this screen, design feedback
---
# Design critique

Give structured feedback across five dimensions. Local sources only: a file in the project (HTML/CSS/component), a screenshot path, `hub_ui.html`, or the user's description. A Figma link cannot be opened locally: say "not available locally" and ask for a screenshot or the code.

## Ask for, if missing
- The design (file, screenshot, or detailed description), what it is and who it is for, the stage (exploration, refinement, final), and an optional focus.

## Framework (one pass per dimension)
1. First impression (2 seconds): what draws the eye first, is that correct, emotional reaction, is the purpose clear.
2. Usability: can the user reach the goal, is navigation intuitive, are interactive elements obvious, any unnecessary steps.
3. Visual hierarchy: reading order, right emphasis, whitespace, typography hierarchy.
4. Consistency: follows the design system, spacing/colors/type consistent, similar elements behave alike.
5. Accessibility: contrast ratios, touch target sizes, text readability, alt text.

## How to give feedback
- Be specific ("The CTA competes with the navigation", not "the layout is confusing"). Name the element.
- Explain why: tie it to a principle or user need.
- Suggest an alternative for every problem.
- Acknowledge what works.
- Match the stage: exploration gets direction, final gets polish.
- Only report what you saw in the file or screenshot. Do not invent elements.

## Output (exact sections)

```markdown
## Design Critique: [Design Name]

### Overall Impression
[1-2 sentence first reaction - what works, what's the biggest opportunity]

### Usability
| Finding | Severity | Recommendation |
|---------|----------|----------------|
| [Issue] | 🔴 Critical / 🟡 Moderate / 🟢 Minor | [Fix] |

### Visual Hierarchy
- **What draws the eye first**: [Element] - [Is this correct?]
- **Reading flow**: [How does the eye move through the layout?]
- **Emphasis**: [Are the right things emphasized?]

### Consistency
| Element | Issue | Recommendation |
|---------|-------|----------------|
| [Typography/spacing/color] | [Inconsistency] | [Fix] |

### Accessibility
- **Color contrast**: [Pass/fail for key text]
- **Touch targets**: [Adequate size?]
- **Text readability**: [Font size, line height]

### What Works Well
- [Positive observation 1]
- [Positive observation 2]

### Priority Recommendations
1. **[Most impactful change]** - [Why and how]
2. **[Second priority]** - [Why and how]
3. **[Third priority]** - [Why and how]
```

## Local crew
The `design-critique` workflow runs five critics in parallel (one dimension each), then one writer fills the format above.

## Tips
Share context ("checkout flow for B2B SaaS"), state the stage, and ask for a focus ("just the navigation") for more depth.
