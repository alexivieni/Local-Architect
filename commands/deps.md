---
name: deps
description: Audit dependencies: unused, missing, pinned, risky
flow: deps
args: optional, a requirements or package file
---
Audit the dependencies of this project{args}.

Compare what the code imports with what is declared. Report: packages declared but never imported, imports with no declared package,
application dependencies not pinned, dev tools mixed into runtime requirements, and anything with a known vulnerability or a suspicious
lookalike name. Give the file and line for each and the change you would make. Say when an audit tool was not available rather than
guessing. Do not install, upgrade or remove anything.
