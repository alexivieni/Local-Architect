---
source: claude-orchestration
kind: playbook
shape: build x2 > verify x2 > build
seen: 1
training: never
---
# Pattern: build x2 > verify x2 > build

Seen in 1 Claude orchestration run(s) that finished without a failed agent.

## When to use

- build x2: one agent changes only the files it owns
- verify x2: something tries to prove the change wrong
- build: one agent changes only the files it owns

Use it for work that changes code and must be checked: the more files it touches, the more the first step earns.

## Run it locally

`python architect.py crew workflows/learned_build_x2_verify_x2_build.yaml --goal "..." --owns <files>`
