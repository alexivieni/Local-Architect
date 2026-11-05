---
name: common
title: Common footer
kind: footer
purpose: The shared rules every worker on a parallel job gets, word for word as the launcher used them.
why: Several agents work in the same folder at once; these rules keep them off each other's files, keep the owner's settings and history untouched, and make every report comparable (what existed, what changed, how to test it).
footer: none
source: hub_data/learning/briefs/launch.py (COMMON)
---
Shared rules for every agent on this job (several agents work in parallel on DIFFERENT files):
- Read CLAUDE.md first. Settings: a new setting goes in arch_params.SETTINGS AND arch_core.DEFAULTS (a check compares them).
- Edit ONLY the files your brief names. Others are being edited by other agents right now.
- No commits, pushes, deletions, or edits to architect_config.json / hub_data (except a file your brief names).
- You cannot run commands; the reviewer runs the tests. Write tests that print "ALL N <NAME> CHECKS PASSED" like the other suites.
- Originality: name things in the app's own voice, not after other products' features.
- End with a short report: what already existed, what you changed (file: function), the test command.
