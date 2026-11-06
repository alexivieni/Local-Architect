---
name: validate-data
description: QA an analysis before sharing
args: <analysis to review>
---
Run the validate-data skill for this project, with the local models only (no connectors; say 'not available locally' where a tool is missing). Follow the skill's steps and output format exactly. Workflow for a crew run: data:validate-data.
Request: {args}
