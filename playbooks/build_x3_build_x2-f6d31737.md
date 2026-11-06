---
source: claude-orchestration
kind: playbook
shape: build x3+ > build x2
seen: 1
training: never
---
# Pattern: build x3+ > build x2

Seen in 1 Claude orchestration run(s) that finished without a failed agent.

## When to use

- build x3: one agent changes only the files it owns
- build x2: one agent changes only the files it owns

Use it for work that changes code and must be checked: the more files it touches, the more the first step earns.

## Run it locally

`python architect.py crew workflows/learned_build_x3_build_x2.yaml --goal "..." --owns <files>`
