---
name: standup
description: Generate a standup update from recent activity: yesterday, today, blockers
when: standup, stand-up, daily update, what did i do yesterday, status for the team
---
# Standup

Two ways: **A) pull it** from local activity, or **B) structure what the person tells you** ("worked on the auth migration, reviewed 3 PRs, blocked on rate limiting").

Local activity for option A: `git log --since=yesterday --author=<user> --oneline` (and `git log --since="2 days ago"` on Mondays), `git status` and `git diff --stat` for work in progress, TODO/FIXME notes. Ticket moves, chat threads and CI dashboards are "not available locally"; ask the person for those.

Rules: one line per item, past tense for Yesterday, plan for Today. Mention a ticket id only if it appears in a commit message or the person gave it. Blockers say who can help; with no blocker write "None". If the argument is yesterday, today or blockers, fill only that section.

## Output (exact format)

```markdown
## Standup - [Date]

### Yesterday
- [Completed item with ticket reference if available]
- [Completed item]

### Today
- [Planned item with ticket reference]
- [Planned item]

### Blockers
- [Blocker with context and who can help]
```
Run it every morning. Ask for the Slack, email or tool-specific format if needed.
