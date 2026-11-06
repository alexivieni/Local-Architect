# Transfer pack: apply what the Claude lead taught this project

Made by `py -3.12 tools/transfer_pack.py` (folder `hub_data/transfer/<stamp>/`). Nothing in it needs a network or a Claude key.

## What is inside
- `playbooks/` - patterns (audit > build > verify ...), when to use each, brief structure, what verifiers checked, lessons.
- `workflows/` - `learned_<shape>.yaml` (mined shapes) on local tiers only.
- `agents/` - `learned-brief-*.md` and `claude-brief-*.md`: read-only or owned-file local agents whose body is a brief the Claude lead used (machine paths scrubbed).
- `orchestration/summary.json` - every workflow/agent: prompt, phases, tools, result summary, tokens and cache hit share.
- `harness/HARNESS.md` - the contract; `harness/settings.json` - cache settings in effect and why, model choices (names only), playbook rules.

Every file is tagged `source: claude-orchestration`, `training: never`. `arch_dataset` reads only `.arch/runs/*/trace.jsonl`, so none of this becomes training data.

## Apply it
1. Same machine or another project: copy `playbooks/`, `workflows/`, `agents/` into the target install's folders of the same name (file names carry a content hash, so identical files are skipped). Check with `py -3.12 architect.py flows --validate`.
2. Another machine:
   - old: `py -3.12 architect.py port --pack mysetup.zip` (your settings, memory, skills; hardware measurements stay behind)
   - new: `py -3.12 architect.py port --adopt mysetup.zip`, then copy the three folders above. The new machine re-picks its own team; `harness/settings.json` is a reference, not something to paste over machine-specific model choices.
3. Use a learned shape: `py -3.12 architect.py crew workflows/learned_<shape>.yaml --goal "..." --owns <files>`.
4. Re-run any time: `py -3.12 tools/transfer_pack.py`. Smoke test (fixture data, no network): `py -3.12 tools/transfer_pack.py --selftest`.
