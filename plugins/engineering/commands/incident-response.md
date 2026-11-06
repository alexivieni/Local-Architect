---
name: incident-response
description: Run an incident workflow
args: <incident description or alert>
---
Run the incident-response skill for this project, with the local models only (no connectors; say 'not available locally' where a tool is missing). Follow the skill's steps and output format exactly. Workflow for a crew run: engineering:eng-incident.
Request: {args}
