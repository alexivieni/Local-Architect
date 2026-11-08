# Conventions: how changes land here

Read before adding a flow, role, setting, gate or hub page. Moved out of CLAUDE.md on 2026-11-08, word for word except the corrections noted in the commit.

## Conventions (how changes land here)
- A new flow is a `flows/<id>.yaml` plus its prompts under `prompts/<id>/`, and an eval family under `evals/families/`. No Python unless it needs a new gate, which goes in `arch_gates.py` with a selftest proving pass, fail and "could not run". A step id may not be a context key a gate writes (the validator refuses it); `python architect.py flows --validate` checks every file.
- Budgets are real: every step and every flow may set `budget: {calls, tokens, seconds}`, enforced in `Engine.call` before the request goes out, and `.arch/STOP` (or `hub_data/STOP`) stops a run between calls.
- Settings are schema-driven: a new setting goes in `arch_params.py` (name, type, range, plain description) and in `DEFAULTS` in `arch_core.py`; the hub renders it from the schema.
- A new role: `arch_roles.ROLES` entry (title, group, icon, needs, sampling), `FALLBACK`, `_prior` tables, and the hub-check role count. A new flow is YAML (first bullet). The Python route (a class with `id`, `title`, `produces`, `run(e, params)` in `arch_flows.FLOWS`, KEYWORDS/PRIORITY entries, a routing test) is only for the 18 original flows until each one moves to YAML.
- Every write the harness makes to the person's settings must have an undo (`tune --undo`, `optimize --undo`, profile undo records).
- Never remove, hide or reassign the person's installed models; downloads only after an explicit confirmation.
- Nothing remote on the default path: model calls go to the local server (or machines the person added in `hosts`); `behavior.network` defaults to `offline` for code the harness runs; anything the Architect itself would fetch is behind `online.enabled = false` and the app must behave identically with it off. Never add a paid or online dependency on the default path. `arch_selftest_offline` fails on the first non-loopback connect.
- Every feature comes with checks in the matching `arch_selftest_*.py` and, for UI, a hub-check or a Playwright pass. A change under `evals/`, the flows or the prompts is measured with `eval --fake` (plumbing) and `eval --quick` (your models) against the latest `baseline`. Ship with `self-verify --write`, a clean `selftest --suite full`, and `py -3.12 tools/doc_counts.py` clean (the counts the docs state about the code; `--write` fixes them).
- Evidence tiers in advice: measured on this machine > vendor card > community. Say which.
- Prompts are prefix-stable: in every retry template the stable part (instructions, project context) comes first and the per-try part (`{feedback}`, failure text) last; `arch_selftest_chain` fails any template that breaks this. The few-shot example goes at the end of the user turn, never in the system prompt.
- The index only orders what the prompt shows; it never decides a pass. The embedding model is asked with `keep_alive 0` by default so it never sits next to the chat model on a small card.

## Self-improvement (`self-improve`, `arch_selfimprove.py`)
Architect edits its own code through the same flows, in a throwaway copy, with the full suite as the gate and `self-rollback` as the undo. See `docs/self-improvement.md`.

## Local model guidance
`docs/local-ai-optimization.md`: what the industry notes recommend, and where each item already lives in Architect (or why it was left out).
