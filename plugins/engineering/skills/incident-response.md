---
name: incident-response
description: Run an incident workflow: triage severity, communicate status, and write a blameless postmortem
when: incident, outage, production is down, sev1, sev2, postmortem, status update, on-call
---
# Incident response

Modes: `new [description]` start an incident; `update [status]` post a status update; `postmortem` write the postmortem. If no mode is given, ask which phase the incident is in.

Phases: **1 Triage** (severity SEV1-4, affected systems and users, roles: incident commander, comms, responders). **2 Communicate** (internal update, customer message if needed, cadence). **3 Mitigate** (document steps taken, keep the timeline, confirm resolution). **4 Postmortem** (blameless, timeline, 5 whys, action items with owners).

## Severity classification

| Level | Criteria | Response Time |
|-------|----------|---------------|
| SEV1 | Service down, all users affected | Immediate, all-hands |
| SEV2 | Major feature degraded, many users affected | Within 15 min |
| SEV3 | Minor feature issue, some users affected | Within 1 hour |
| SEV4 | Cosmetic or low-impact issue | Next business day |

Updates are factual: what is happening, who is affected, what we are doing, when the next update is. No speculation. Local evidence: log files, `git log` around the incident time. Paging, chat posting and monitoring graphs are "not available locally": draft the text for the person to send.

## Output - Status Update

```markdown
## Incident Update: [Title]
**Severity:** SEV[1-4] | **Status:** Investigating | Identified | Monitoring | Resolved
**Impact:** [Who/what is affected]
**Last Updated:** [Timestamp]

### Current Status
[What we know now]

### Actions Taken
- [Action 1]

### Next Steps
- [What's happening next and ETA]

### Timeline
| Time | Event |
|------|-------|
| [HH:MM] | [Event] |
```

## Output - Postmortem

```markdown
## Postmortem: [Incident Title]
**Date:** [Date] | **Duration:** [X hours] | **Severity:** SEV[X]
**Authors:** [Names] | **Status:** Draft

### Summary
[2-3 sentence plain-language summary]

### Impact
- [Users affected]
- [Duration of impact]
- [Business impact if quantifiable]

### Timeline
| Time (UTC) | Event |
|------------|-------|
| [HH:MM] | [Event] |

### Root Cause
[Detailed explanation of what caused the incident]

### 5 Whys
1. Why did [symptom]? -> [Because...]
2. Why did [cause 1]? -> [Because...]
3. Why did [cause 2]? -> [Because...]
4. Why did [cause 3]? -> [Because...]
5. Why did [cause 4]? -> [Root cause]

### What Went Well
- [Things that worked]

### What Went Poorly
- [Things that didn't work]

### Action Items
| Action | Owner | Priority | Due Date |
|--------|-------|----------|----------|
| [Action] | [Person] | P0/P1/P2 | [Date] |

### Lessons Learned
[Key takeaways for the team]
```
Start writing immediately and update as you learn more. Postmortems are blameless: systems and processes, not individuals.
