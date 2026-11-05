# Architect — architecture

A local-first agentic coding harness. Local models do the work on this machine; code (tests, parsers, scanners) decides what is
kept; Claude is an optional, capped tier. This document maps the system: the harness, the agents, the components, how work is
spread across the hardware, and how tokens and caches are managed. Numbers are measured on the reference machine (RTX 4050 6 GB,
32 GB RAM, Intel 13th-gen P+E cores, NVMe + 32 GB SD card) unless marked otherwise.

---

## 1. The one rule

**A model proposes; code decides.** Every step ends in a check that returns `True` (passed), `False` (failed) or `None` (could not
run). `None` makes the verdict *not sure* — never a pass, never training data. Gates are the product: a 4B model is only
trustworthy because code checks its work.

```
request ─► router ─► flow (YAML steps) ─► model call ─► apply edit ─► gates (tests, parse, scanners) ─► verdict ─► trace
              ▲                                │                              │
              └── learning router (logbook) ◄──┴──── retries / escalation ◄────┘
```

---

## 2. Layers

| Layer | What it does | Main modules |
|---|---|---|
| **Interface** | Hub (Simple mode by default, Advanced = full hub), CLI, Python API | `hub_ui.html`, `architect_hub.py`, `model_hub.py`, `architect.py`, `architect_embed.py` |
| **Orchestration** | Routing, flows as data, budgets, retries, escalation, fleets of agents, queue | `arch_flows.py`, `arch_dataflow.py`, `arch_progress.py`, `arch_fleet.py`, `arch_supervisor.py`, `arch_broker.py` |
| **Engine** | Model calls, prompt assembly, context, edits, undo, traces | `arch_core.py` (`Engine`), `arch_tools.py` |
| **Verification** | Tests, parsers, fake-code, scanners, gates registry | `arch_gates*.py`, `arch_checks.py`, `arch_fakecode.py`, `arch_deps.py` |
| **Inference** | Local servers (llama-server, Ollama fallback), Claude tier | `arch_transport.py`, `arch_server.py`, `arch_cloud.py`, `arch_claudecode.py`, `arch_agents.py` |
| **Hardware** | Detection, placement, fast lane, idle unloading, multi-machine | `arch_hw.py`, `arch_placement.py`, `arch_server.py`, `arch_hosts.py` |
| **Learning** | Traces → logbook, playbooks, dataset, tuning, routing that learns | `arch_learn.py`, `arch_overseer.py`, `arch_dataset.py`, `arch_autotune.py`, `train_lora.py`, `arch_routelearn.py`, `arch_iterate.py` |
| **Safety** | Offline seal, spend caps, secret scrubbing, file ownership, sandbox | `arch_cloud.allowed/reserve`, `arch_redact.py`, ownership hooks, `arch_sandbox.py` |

---

## 3. The harness

### Flows as data
A flow is a YAML file (`flows/*.yaml`): steps of kinds `generate`, `code`, `ask_json`, `judge`, `doc`, `sample_n`, `cascade`,
`tool_loop`, `foreach`, `sub_flow`. Each step names a tier, a prompt file (`prompts/<flow>/*.md`), its gate, retries, and a budget
`{calls, tokens, seconds}` enforced in `Engine.call` before the request leaves. Blueprints (hub) builds flows visually and saves
through the same validator (`architect.py flows --validate`).

### Patterns encoded as flows
`fanout_review` (several reviewers, a judge keeps what survives), `scout_then_build` (a cheap local step briefs the builder),
`council_fix` (local tries, then a council), `plan_local_build` (thinking tier plans, local worker writes).

### What makes a small model finish (measured)
- **Ground before editing:** a line edit is refused until the model has looked at those lines this run (`behavior.ground_edits`).
  Today's agents averaged ~5 reads per edit; blind edits miss.
- **Context editing:** old tool results are shortened in the prompt (newest 4 in full, next 10 to 600 chars, older to one line), so
  an 8K window holds a long exploration (`arch_dataflow._compact_turns`, `arch_localagent._history`).
- **Repeat reads collapse:** the third identical read of unchanged lines returns a pointer, not the text.
- **Big files:** the lines a request points at (quotes or rare words) are always shown (`Engine._quoted_lines`).
- **DEF blocks, interface first, one sprint per module, best try kept** (see CLAUDE.md).
- **Checks ignore problems that predate the run**, so a correct one-line change is not failed for old findings.

### Escalation ladder
worker (local GPU) → heavy (local MoE) → Claude (only when `online.cloud_tier` allows: `escalation`, `plan`, `chat`, `all`).
`plan` = Claude does the thinking roles (planner, architect, judge, reviewer); local models write the code.

---

## 4. Agents

| Agent type | Runs where | Billed | Module |
|---|---|---|---|
| Local agent (tool loop on local models) | this PC, GPU worker | free | `arch_localagent.py` |
| Claude Code (Agent SDK) | this PC, project folder | API key, never a subscription (own `CLAUDE_CONFIG_DIR`) | `arch_claudecode.py` |
| Managed Agent (Anthropic sandbox) | Anthropic | API key | `arch_agents.py` |
| Council (solver, critic, alternative, 2 skeptics, lead) | Claude API | API key | `arch_cloud.ultra` |
| Subagents (code-reviewer, test-writer, yours) | inside Claude Code | API key | `<data>/subagents.json` |

### Fleet method (lead + workers)
`arch_fleet.py` runs many agents at once: each owns its files (`plan_check` refuses overlaps, a PreToolUse hook denies writes
outside them), each has a spend cap, local agents share the one GPU in turn while CPU and Claude work run in parallel, and **the
fleet — not the agents — runs the tests**. Templates for every agent used to build this app: `agent_templates/` (`arch_agentlib.py`),
each marked whether a local 4B worker can do it.

---

## 5. Components and placement

| Component | Role | Notes (measured) |
|---|---|---|
| GPU (RTX 4050, 6 GB, 158 GB/s) | worker tier | 8B abliterated, all 36 layers + 8K KV on the card (~5.3 GB) with fast lane |
| CPU performance cores | router (qwen3 1.7B), test runs | router never competes for graphics memory |
| CPU efficiency cores | indexing, logbook ingest, scans, exports | below-normal priority (`arch_placement.run_low`) |
| RAM (32 GB, 19.5 GB/s) | MoE experts of the heavy tier, caches | heavy kept beside the worker only when used (`server.heavy_beside`) |
| NVMe | models, KV slot caches, hot data | never the SD card for models |
| SD card (32 GB, `D:`) | cold data: run archives, learning archive, backups | `storage.archive`, `tools/sync_learning.py` |
| Other PCs (optional) | extra model hosts | `arch_hosts.py`: measured, ranked, fallback to local |

**Broker** (`arch_broker.route`): per job, picks GPU / heavy / CPU / E-cores / Claude / wait from measured speed, current load, money
caps and policy (local first); every decision is logged. **Fast lane** (`server.fast_lane`): the worker gets the whole card,
n-gram speculation (edits measured 56 → 306–343 tok/s), above-normal priority on performance cores.
**Free memory when idle** (`server.idle_free_min`, default 15): idle model servers and Ollama copies are unloaded and reload on
demand (~5–8 s).

---

## 6. Tokens and caching

Caches in the order a call meets them:

| # | Cache | Where | Hit cost | Rule |
|---|---|---|---|---|
| 1 | Route decisions | `<data>/route_cache.json` | no model call | same request + folder state |
| 2 | Local reply cache | `arch_cache.py` | 0 | temperature 0 or a seed; never during eval |
| 3 | Claude reply cache | `<data>/cloud_cache/` | $0 | 30 days; off in eval/self-tests |
| 4 | llama-server prefix (KV) cache | in memory | prompt not re-read | prompts are prefix-stable: stable part first, per-try part last |
| 5 | KV slot on disk per project | `--slot-save-path` | first answer after reopen as fast as the tenth | saved at run end, restored at start |
| 6 | Claude prompt cache | Anthropic | 0.1× input | system + shared context are breakpoints; Council agents share one prefix; 5 min or 1 h |
| 7 | Logbook retrieval | `<data>/logbook.db` | free lookup | similar past tasks + the tool sequence that worked |

**Measured today:** ~107 M Claude input tokens served from cache vs a few thousand uncached; Council cost 0.27 → 0.18 $ with a shared
prefix. A new Claude Code session writes ~30–34K tokens of its own system prompt/tools to cache once (~$0.10–0.17), so fewer,
longer agent sessions are cheaper than many short ones.

**Context budgets are real:** `Engine.context(budget)` counts outlines too; prompts are fitted to the window the model really has
(local) or the full window (Claude); local scouts hand Claude a ~600-word brief instead of the files.

**Money:** `arch_cloud.reserve()` holds the worst case before every Claude call / Claude Code run / fleet / Managed Agent and
`settle()` records the real cost; per-run and per-day caps; fast mode only when a person waits; `arch_tokens` prices a message
before it is sent.

---

## 7. Data and learning

```
every call/step/check ──► trace.jsonl ──► logbook.db (FTS) ──► playbooks.md, similar(), routelearn
                                     └──► dataset (verified LOCAL samples only, dedup on the task part, quality score, tags)
                                               └──► LoRA (train_lora.py) ──► eval --compare ──► promote only on no regression
```
- Training data: only the app's own verified local work. Claude-written content (`transport: claude`, `by_claude`) is used for
  analysis, playbooks and retrieval, never for weights.
- Agent work from building the app: `hub_data/learning/` (briefs, scrubbed logs, ledger, reasoning corpus, METHOD, LESSONS) and the
  SD-card archive `D:\Architect\learning\`.
- Training workload: 105 tasks (65 distributed systems, 40 harness building) with hidden tests (`tools/train_traces.py`).

---

## 8. Safety and privacy
- Offline by default; anything off-machine sits behind `online.enabled`. Self-tests can never reach a paid model.
- Secrets are scrubbed before logs are stored or archived (`arch_redact.py`); the API key lives only in the environment.
- Every edit has an undo; settings writes are checked and undoable; agents cannot write outside their own files.

---

## 9. Where to look next
`CLAUDE.md` (developer rules and measured lessons), `docs/architecture_review_20261104.md` (22 ranked improvements),
`docs/claude_to_local_synthesis.md` (what Claude's agents did and how it applies locally), `docs/pc_plan_20261104.md` (this machine).
