---
source: claude-orchestration
kind: playbook
shape: audit x3+ > verify x3+
seen: 6
training: never
---
# Pattern: audit x3+ > verify x3+

Seen in 6 Claude orchestration run(s) that finished without a failed agent.

## When to use

- audit x3: several readers look before anything is changed
- verify x3: something tries to prove the change wrong

Use it for work that changes code and must be checked: the more files it touches, the more the first step earns.

## What the verifiers were told to check

- i feel this would improve the test suite?

## Run it locally

`python architect.py crew workflows/learned_audit_x3_verify_x3.yaml --goal "..." --owns <files>`
