---
name: documentation
description: Write and maintain technical documentation: README, API docs, runbook, architecture doc, onboarding guide
when: document, docs, readme, runbook, onboarding guide, api docs, write docs, technical writing
---
# Technical documentation

Pick one document type, read the real code first (ls, grep, read), then write with exactly these sections.

| Type | Sections, in order |
|------|--------------------|
| README | What this is and why it exists; Quick start (under 5 minutes to first success); Configuration and usage; Contributing guide |
| API Documentation | Endpoint reference with request/response examples; Authentication and error codes; Rate limits and pagination; SDK examples |
| Runbook | When to use this runbook; Prerequisites and access needed; Step-by-step procedure; Rollback steps; Escalation path |
| Architecture Doc | Context and goals; High-level design with diagrams; Key decisions and trade-offs; Data flow and integration points |
| Onboarding Guide | Environment setup; Key systems and how they connect; Common tasks with walkthroughs; Who to ask for what |

## Principles
1. Write for the reader: who reads this and what do they need?
2. Start with the most useful information; do not bury the lede.
3. Show, do not tell: code examples and commands (copy real ones from the project, never invent).
4. Keep it current: outdated docs are worse than no docs.
5. Link, do not duplicate: reference other docs.

Small-model rule: if a fact is not in the project, write "TODO: confirm" instead of guessing.
