---
name: code-review
description: Review code changes for security, performance, correctness and maintainability; use before merging or when asked if a change is safe
args: <PR URL, diff, or file path>
---
Run the code-review skill for this project, with the local models only (no connectors; say 'not available locally' where a tool is missing). Follow the skill's steps and output format exactly. Workflow for a crew run: engineering:eng-code-review.
Request: {args}
