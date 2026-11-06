---
name: ux-copy
description: Write or review UX copy: CTAs, errors, empty states
when: write copy for, button text, error message, empty state, confirmation dialog, onboarding text, microcopy
---
# UX copy

Write or review UX copy for any interface context. Brand-voice or design-tool lookups are "not available locally"; ask for the voice in a sentence or read the project's existing strings.

## Ask for, if missing
Context (screen, flow, feature), user state (goal, feelings), tone (formal, friendly, playful, reassuring), constraints (character limits, platform guidelines).

## Principles
1. Clear: say exactly what you mean; no jargon. 2. Concise: fewest words for the full meaning. 3. Consistent: same terms for the same things. 4. Useful: every word helps the goal. 5. Human: like a helpful person, not a robot.

## Copy patterns
- CTAs: start with a verb ("Start free trial", "Save changes"); be specific ("Create account", not "Submit"); match label to outcome.
- Error messages: what happened + why + how to fix. "Payment declined. Your card was declined by your bank. Try a different card or contact your bank."
- Empty states: what this is + why it's empty + how to start. "No projects yet. Create your first project to start collaborating with your team."
- Confirmation dialogs: name the action ("Delete 3 files?", not "Are you sure?"); state consequences ("This can't be undone"); label buttons with the action ("Delete files" / "Keep files", not "OK" / "Cancel").
- Tooltips: concise, helpful, never obvious.
- Loading states: set expectations, reduce anxiety.
- Onboarding: progressive disclosure, one concept at a time.

## Voice and tone
Success: celebratory but not over the top. Error: empathetic and helpful. Warning: clear and actionable. Neutral: informative and concise.

## Output

```markdown
## UX Copy: [Context]

### Recommended Copy
**[Element]**: [Copy]

### Alternatives
| Option | Copy | Tone | Best For |
|--------|------|------|----------|
| A | [Copy] | [Tone] | [When to use] |
| B | [Copy] | [Tone] | [When to use] |
| C | [Copy] | [Tone] | [When to use] |

### Rationale
[Why this copy works - user context, clarity, action-orientation]

### Localization Notes
[Anything translators should know - idioms to avoid, character expansion, cultural context]
```

## Tips
Be specific about context, share your brand voice, and consider the user's emotional state: errors need empathy, successes can celebrate.
