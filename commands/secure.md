---
name: secure
description: Scan for security problems and fix the ones you can prove
flow: secure
args: optional, a folder to narrow the scan
---
Find the security problems in this project{args}.

Look for the ones that actually happen: input reaching a query or a shell without being parameterised, secrets committed to the repo,
authentication that can be skipped, access checks missing on a path that changes data, and dependencies with known holes.
Fix what you can fix safely and show the change. For anything you cannot fix without a decision, say what the decision is.
