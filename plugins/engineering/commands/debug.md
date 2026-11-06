---
name: debug
description: Structured debugging
args: <error message or problem description>
---
Run the debug skill for this project, with the local models only (no connectors; say 'not available locally' where a tool is missing). Follow the skill's steps and output format exactly. Workflow for a crew run: engineering:eng-debug.
Request: {args}
