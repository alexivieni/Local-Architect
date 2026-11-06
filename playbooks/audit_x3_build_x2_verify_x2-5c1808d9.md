---
source: claude-orchestration
kind: playbook
shape: audit x3+ > build x2 > verify x2
seen: 1
training: never
---
# Pattern: audit x3+ > build x2 > verify x2

Seen in 1 Claude orchestration run(s) that finished without a failed agent.

## When to use

- audit x3: several readers look before anything is changed
- build x2: one agent changes only the files it owns
- verify x2: something tries to prove the change wrong

Use it for work that changes code and must be checked: the more files it touches, the more the first step earns.

## Run it locally

`python architect.py crew workflows/learned_audit_x3_build_x2_verify_x2.yaml --goal "..." --owns <files>`
