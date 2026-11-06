---
source: claude-orchestration
kind: playbook
shape: build x3+ > verify x3+
seen: 3
training: never
---
# Pattern: build x3+ > verify x3+

Seen in 3 Claude orchestration run(s) that finished without a failed agent.

## When to use

- build x3: one agent changes only the files it owns
- verify x3: something tries to prove the change wrong

Use it for work that changes code and must be checked: the more files it touches, the more the first step earns.

## What the verifiers were told to check

- Adversarially check this diagnosis of a failed Architect live run (code at <path> trace at <path> ...
- Adversarially check this diagnosis of a failed Architect live run (code at <path> trace at <path> ...
- Adversarially check this diagnosis of a failed Architect live run (code at <path> trace at <path> ...
- Adversarially check this diagnosis of a failed Architect live run (code at <path> trace at <path> ...

## Run it locally

`python architect.py crew workflows/learned_build_x3_verify_x3.yaml --goal "..." --owns <files>`
