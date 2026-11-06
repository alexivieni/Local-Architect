---
name: code-review
description: Review code changes for security, performance, correctness and maintainability; use before merging or when asked if a change is safe
when: review, code review, diff, pull request, before I merge, is this safe, n+1, injection, edge case
---
# Code review

If no file or diff is given, ask what to review. Local sources: `git diff`, `git diff <base>...HEAD`, `git show <sha>`, or the named files. A PR URL cannot be fetched locally ("not available locally"): ask for the diff or branch name. CI status: run the project's tests if asked. Team standards: read CONTRIBUTING / lint config if present.

## Review dimensions (check each, cite file and line for every finding)
- **Security:** SQL injection, XSS, CSRF; authentication and authorization flaws; secrets or credentials in code; insecure deserialization; path traversal; SSRF.
- **Performance:** N+1 queries; unnecessary memory allocations; O(n^2) in hot paths; missing database indexes; unbounded queries or loops; resource leaks.
- **Correctness:** edge cases (empty input, null, overflow); race conditions; error handling and propagation; off-by-one errors; type safety.
- **Maintainability:** naming clarity; single responsibility; duplication; test coverage; documentation for non-obvious logic.

## Rules for small models
- Report only problems you saw in the code. No file and line = drop the finding.
- Critical = exploitable, data loss, crash on common input. Everything else is a Suggestion.
- If the diff is large, review one file at a time and merge the findings at the end.

## Output (exact format)

```markdown
## Code Review: [PR title or file]

### Summary
[1-2 sentence overview of the changes and overall quality]

### Critical Issues
| # | File | Line | Issue | Severity |
|---|------|------|-------|----------|
| 1 | [file] | [line] | [description] | Critical |

### Suggestions
| # | File | Line | Suggestion | Category |
|---|------|------|------------|----------|
| 1 | [file] | [line] | [description] | Performance |

### What Looks Good
- [Positive observations]

### Verdict
[Approve / Request Changes / Needs Discussion]
```
Verdict: any Critical issue = Request Changes. Context like "hot path" or "handles PII" narrows the focus.
