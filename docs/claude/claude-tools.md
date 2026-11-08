# Rules learned building the Claude tools

Read before touching arch_cloud, arch_claudecode, arch_fleet, arch_agents or training data. Moved out of CLAUDE.md on 2026-11-08, word for word except the corrections noted in the commit.

## Rules learned building the Claude tools (2026-11-04; method: hub_data/learning/METHOD.md)
- **Self-tests never call Claude**, whatever the settings say: every network path asks `arch_cloud.allowed()` (false under eval and the self-tests, `_testing()`); tests pass a fake `client=`. A count of tokens is still a request off the machine, so it is gated too.
- **Claude prompts get the full window.** Local prompts are fitted to a small served window; a Claude call is not, so do not cut its context to a local size. Keep the shared prefix first so prompt caching pays (a 6-call Council answer fell from $0.27 to $0.18).
- **Checks ignore pre-existing findings.** A gate compares against what the file had before the run; otherwise a correct one-line change fails on problems it did not make.
- **Agents own files.** Every brief names its files; no two running agents share one (or a folder and a file in it). A shared file (hub_ui.html) is queued, not parallel. Every brief has a hard `max_budget_usd`; jobs came in at 10-25% of their caps.
- **The orchestrator verifies.** Agents in acceptEdits mode cannot run commands, so the lead (or `arch_fleet`) runs the suites and reports three outcomes; only what passes is committed.
- Claude Code's own `claude -p` prefers a subscription login over the key: keep the empty `claude_code` settings folder. Claude-written text may feed retrieval, playbooks and statistics, never fine-tuning: `arch_dataset.build()` skips every run `arch_learn.exclude_from_training()` names and any run with a `claude` transport or `claude:` model call, and cuts the ledger's FORMAT REFERENCE block unless it quotes the same local model; `arch_ledger` never stores or shows `claude:`/`fake:` or benchmark examples (and none under `ARCHITECT_EVAL`); `Engine.call` asks `example_for(..., model=model)` (only that model's own past replies), skips it under `ARCHITECT_EVAL`, and traces `fewshot_model`. `arch_dataset` drops `fake:` replies as fixtures and `assist`-traced tries; only the eval `train` bucket is read. (`arch_selftest_chain` therefore scripts `chainfix:7b`, in its throwaway data dir.)
- A new setting goes in `arch_params.SETTINGS` and `arch_core.DEFAULTS` (a check compares them).

## Transfer pack
`py -3.12 tools/transfer_pack.py` (docs/TRANSFER.md) exports playbooks, learned workflows, Claude briefs as local agents, the harness contract and cache/model settings into `hub_data/transfer/<stamp>/`. Claude-written, tagged `source: claude-orchestration`, never training data.
