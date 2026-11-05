# From Claude's agents to your local platform (2026-11-04)

What 44 Claude agents did today, why they worked that way, whether each idea applies to this machine (RTX 4050 6 GB, 32 GB RAM,
CPU router qwen3 1.7B, GPU worker 4B, MoE heavy, 32 GB SD card), and where it now lives in the app.

Evidence: `hub_data/logbook.db` (378 tasks, 28,501 steps), `hub_data/learning/` (briefs, scrubbed logs, ledger: $80 over 44 agents),
`hub_data/learning/reasoning_corpus.jsonl` (301 working notes, 43 final reports, 19 thinking summaries), Monitor lessons
(`LESSONS.md`). Claude's raw reasoning is never returned by the API; the record of *why* is the notes agents write between tool
calls, their reports, and thinking summaries where they were requested (1,231 of 1,250 thinking blocks were empty, the Opus 5.5
default). All of it is flagged `by_claude`: use it for playbooks, retrieval and harness design, not for training model weights.

## How the agents worked, and why

| What they did | Why it works | Applies locally? | Where it is now |
|---|---|---|---|
| **Read before writing**: Read 3,425 times, Grep 2,582, Edit 1,159 -- about 5 reads per edit | Edits grounded in the exact text pass on the first try; guessed SEARCH text fails | **Yes, more so**: a 4B model guesses more | skill `read-before-edit`, the line finder for big files (`Engine._quoted_lines`), the scout |
| **Plan in short notes, then act** (178 plan notes, 22 explicit decisions with a reason) | Small explicit steps keep a long task on track and leave a trail | Yes: local tool loop + live todo list | `arch_localagent`, `Engine.todo_set`, the live board |
| **Report what existed vs what changed** (43 reports) | The lead can verify fast; nothing is claimed that was not done | Yes | every agent template's footer, `arch_agentlib` |
| **Stay in their own files** | Zero collisions across 9-18 parallel agents | Yes | `arch_fleet.plan_check`, `arch_localagent` own-files rule |
| **Stop and say "not run"** when they could not test | Honest uncertainty; verification moves to the orchestrator | Yes: `None` = not sure | gates' True/False/None, the fleet runs tests |
| **Shared cached prefix** (107M cached vs ~2k uncached input tokens) | Re-reading the same context costs ~0.1x | **Yes**: llama-server prefix + slot cache, reply cache | skill `cost-and-cache`, `arch_cloud` breakpoints, slots per project |
| **Strong model for judgment, cheap for mechanics** | Spend where reasoning matters | Yes: router/worker/heavy/Claude tiers | `plan` mode, `arch_broker`, templates' `local_ok` |
| **Parallel fan-out + skeptics + one lead** (Council) | Independent views catch what one pass misses | Partly: one GPU writer, but CPU router and Claude in parallel | Council, `fanout_review` flow, fleet GPU scheduling |

## What went wrong, and the fix now in the app

| Seen | Cost | Fix |
|---|---|---|
| Workers tried to run commands 65 times and were refused | wasted turns | briefs say so; the fleet runs tests itself; local agents CAN run tests (`run_tests` tool) |
| UI agents re-read a 245 KB file 11-14 times (up to 32% of their calls) | time + tokens | outline/section briefs; split the UI file (proposal below) |
| A worker overwrote an existing test suite | lost checks | verification caught it; fleet now diffs and runs every suite |
| Claude was handed a prompt cut to the LOCAL 12K window and edited blind | failed edits | Claude prompts get the full window |
| Context outlines ignored the size budget (36k chars for a "4k" request) | cost on every model | outlines count against the budget |
| Self-tests reached the paid model under "all" mode | money + broken gates | self-tests can never call Claude |

## Applied to your local models

1. **Skills** (`skills/lead-and-workers.md`, `read-before-edit.md`, `cost-and-cache.md`): shown to the local models automatically when
   a task matches, so the 4B worker gets today's method in its prompt.
2. **Playbooks** (`hub_data/memory/playbooks.md`) from the Logbook: what worked per kind of task.
3. **Logbook retrieval** (`arch_learn.similar`): past tasks like this one, with the tool sequence that worked.
4. **Agent templates for local runs** (`agent_templates/`, `arch_localagent`): every Claude brief, marked `local_ok` when a 4B worker
   with tools can do it.
5. **Routing** (`arch_broker`, `arch_routelearn`): send each job to GPU / CPU / heavy / Claude by measured speed, cost and past outcomes.

## Next, in order (each small)

1. Split `hub_ui.html` (245 KB) into a shell + one file per page -- the biggest single saving for every UI agent and every page load.
2. Turn on `server.fast_lane` for interactive work and measure tok/s before/after.
3. Run `tools/overnight.py` nightly: verified local traces become training data (local only, never Claude content).
4. Run the hybrid benchmark once to learn when Claude pays for itself on your tasks.
5. Point the broker at the SD card for cold data (archives, logbook backups) via `arch_placement.apply_storage(dry_run=True)` first.
