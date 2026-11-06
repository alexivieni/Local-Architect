---
name: tech-debt
description: Identify, categorize and prioritize technical debt with a scoring formula and a phased remediation plan
when: tech debt, technical debt, refactor, code health, maintenance backlog, what should we refactor
---
# Tech debt management

Scan the named area (ls, grep for TODO/FIXME/HACK, duplicated blocks, large files, outdated pins in requirements, missing tests, missing docs) and file each finding in one category.

| Type | Examples | Risk |
|------|----------|------|
| **Code debt** | Duplicated logic, poor abstractions, magic numbers | Bugs, slow development |
| **Architecture debt** | Monolith that should be split, wrong data store | Scaling limits |
| **Test debt** | Low coverage, flaky tests, missing integration tests | Regressions ship |
| **Dependency debt** | Outdated libraries, unmaintained dependencies | Security vulns |
| **Documentation debt** | Missing runbooks, outdated READMEs, tribal knowledge | Onboarding pain |
| **Infrastructure debt** | Manual deploys, no monitoring, no IaC | Incidents, slow recovery |

## Prioritization framework
Score each item:
- **Impact:** how much does it slow the team down? (1-5)
- **Risk:** what happens if we do not fix it? (1-5)
- **Effort:** how hard is the fix? (1-5, inverted: lower effort = higher priority)

**Priority = (Impact + Risk) x (6 - Effort)**

Compute the number for every item yourself and sort descending. Show the arithmetic.

## Output
A prioritized table (Item | Type | Impact | Risk | Effort | Priority | Where (file:line)), estimated effort and a business justification for each item, then a phased remediation plan (Phase 1 quick wins, Phase 2, Phase 3) that can be done alongside feature work. Every item cites a real file.
