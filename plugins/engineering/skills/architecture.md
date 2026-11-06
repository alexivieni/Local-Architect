---
name: architecture
description: Create or evaluate an architecture decision record (ADR): choosing between technologies, documenting a decision with trade-offs, reviewing a design proposal
when: adr, architecture decision, kafka vs, which database, trade-off, decision record
---
# Architecture decision record (ADR)

Three modes: **Create an ADR** ("Kafka or SQS for the event bus?"), **Evaluate a design** ("review this microservices proposal"), **System design** (see the system-design skill for requirements, scale and trade-off frameworks).

## Steps (small model: do them in order)
1. Restate the decision in one line. List the constraints you were given (deadline, load, team skills, budget). If none, write "none stated".
2. Name at least two options. Rate each on Complexity (Low/Med/High), Cost, Scalability, Team familiarity.
3. Weigh the trade-offs, then decide. Say what gets easier, what gets harder, what to revisit.
4. Local context: grep the project for existing ADRs (`docs/adr`, `adr/`, `decisions/`) and read the nearest one for numbering. Issue trackers and wikis are not available locally.

## Output - ADR format (use exactly these headings)

```markdown
# ADR-[number]: [Title]

**Status:** Proposed | Accepted | Deprecated | Superseded
**Date:** [Date]
**Deciders:** [Who needs to sign off]

## Context
[What is the situation? What forces are at play?]

## Decision
[What is the change we're proposing?]

## Options Considered

### Option A: [Name]
| Dimension | Assessment |
|-----------|------------|
| Complexity | [Low/Med/High] |
| Cost | [Assessment] |
| Scalability | [Assessment] |
| Team familiarity | [Assessment] |

**Pros:** [List]
**Cons:** [List]

### Option B: [Name]
[Same format]

## Trade-off Analysis
[Key trade-offs between options with clear reasoning]

## Consequences
- [What becomes easier]
- [What becomes harder]
- [What we'll need to revisit]

## Action Items
1. [ ] [Implementation step]
2. [ ] [Follow-up]
```

## Tips
State constraints upfront, name your options, include non-functional needs (latency, cost, expertise, maintenance). Never invent numbers; write "unknown" instead.
