---
name: debug
description: Structured debugging: reproduce, isolate, diagnose, fix; use for an error or stack trace, a regression after a change, or behavior that differs from expected
when: debug, error, stack trace, traceback, exception, broke after, works locally, regression, not working
---
# Debug

Four steps, in order. Do not propose a fix before step 3 names a root cause.

1. **Reproduce:** write Expected vs Actual. Find exact steps (a command or test that fails). Scope: when did it start, what is affected? Share error text exactly, never paraphrase.
2. **Isolate:** narrow to a component or code path. Check recent changes with `git log -n 15 --stat`, `git diff`, dependency or config changes. Read logs and error output in the project (log files, test output). Hosted monitoring is "not available locally".
3. **Diagnose:** form 2-3 hypotheses, test each by reading the code or running a command, trace the code path. Name the root cause, not the symptom.
4. **Fix:** propose a fix with an explanation. Consider side effects and edge cases. Suggest a regression test; if asked, write it and run the tests.

Related bug reports: grep the project for the message and TODO/FIXME notes (ticket trackers are not available locally).

## Output (exact format)

```markdown
## Debug Report: [Issue Summary]

### Reproduction
- **Expected**: [What should happen]
- **Actual**: [What happens instead]
- **Steps**: [How to reproduce]

### Root Cause
[Explanation of why the bug occurs]

### Fix
[Code changes or configuration fixes needed]

### Prevention
- [Test to add]
- [Guard to put in place]
```
Recent deploys, dependency updates and config changes are the top suspects. "Works in A but not B" narrows fast: diff the two environments.
