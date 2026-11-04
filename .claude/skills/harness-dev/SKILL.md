---
name: harness-dev
description: Use when adding or changing a role, flow, setting, check or hub page in Architect, or when asked to extend what the harness can do.
---

# Architect development checklist

## Adding a role
1. Add the entry to `arch_roles.ROLES` (title, group, icon, needs, sampling, think) and to `FALLBACK` and the `_prior` tables.
2. Update the role count in `arch_hub_check.py` and the README.
3. If a flow should use it, call `e.call("<role>", ...)` there; wrap fix loops in `arch_progress.Coach`.
4. Run `python architect.py selftest --suite full`.

## Adding a flow
1. A class with `id`, `title`, `produces`, `run(e, params)`; register in `arch_flows.FLOWS`; add KEYWORDS/PRIORITY entries.
2. Record checkpoints with `e.checkpoint(...)` so `resume` works; verify with `arch_flows.verify`.
3. Add routing cases to `arch_selftest_pro.py` and a scripted end-to-end run to the matching suite.

## Adding a setting
1. `arch_params.SETTINGS` (path, type, label, plain description, range) and `arch_core.DEFAULTS`.
2. Read it with `e.cfg["behavior"][...]`; give it a test that proves both values do something.

## Changing anything the hub shows
1. Edit `hub_ui.html` (one file, no build). The script must parse: `arch_hub_check.py` runs `node --check` when node exists.
2. For a new endpoint: `architect_hub.py` `route()`, with input validation, and a hub-check call.
3. Screenshot in light, dark and phone widths before shipping.

## Shipping
`self-verify --write`, `selftest --suite full` green, README check count, zip + patch against the pristine source. Never remove a gate to make a run pass.
