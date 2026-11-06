# The local agent harness contract

Local agents are defined and run the way Claude Code defines subagents and runs the Workflow tool, on local models only
(`arch_squad.py`, `python architect.py crew`). No agent here can call Claude: a `claude:` model, or the names `sonnet`, `opus`,
`haiku`, `fable`, is refused when the file loads.

## 1. Agent file (`agents/<name>.md`)

```
---
name: reviewer                       # lower-case, digits, - or _
description: When to use this agent  # shown to people and to the router
tools: Read, Grep, Glob              # comma list; Claude names or local names (see section 2). Omitted = read-only set
model: worker                        # worker | heavy | router, or a local id like qwen3:8b; inherit = worker
effort: high                         # low | medium | high  (reasoning off | role default | on)
color: orange                        # cosmetic
# optional, beyond Claude's fields:
role: reviewer                       # engine role (arch_roles.ROLES); default generator if it writes, else reviewer
category: engineering
writes: false                        # inferred from tools when absent; true with no write tool is allowed, the reverse is refused
max_steps: 8                         # tool-loop turns 1..40
budget: calls=12,tokens=60000,seconds=900
context: inherit:8000                # fresh | inherit (gets the previous phase's result as {prev}), max characters
owns: tests                          # files it may change by default
output: {"type":"object",...}        # one-line JSON schema; absent = text
task: |                              # task template: {goal} {task} {prev} {focus} and workflow vars
  Goal: {goal}
  {task}
---
The markdown body is the system prompt.
```

`agents/*.yaml` still loads (same fields, `system:` instead of a body). If a `.md` and a `.yaml` share a name in one folder, only
the `.md` registers. Folders searched: `agents/`, the project's `.arch/agents/`, enabled plugins (`plugin:name` on a clash).
`save_agent(name, spec, fmt="md")` validates, writes atomically and checks the file reads back equal.

## 2. Tools and permissions

| Claude name | Local tools |
|---|---|
| Read | read, view, outline |
| Grep | grep, search |
| Glob, LS | ls |
| Edit, Write, MultiEdit | edit_lines, apply |
| Bash | run_tests (only the allowed test commands; no free shell) |

Local names (`read`, `run_tests`, `memory_read`, ...) are accepted as they are. Anything else (WebFetch, ...) is refused.
A read-only agent (`writes: false`) cannot hold a write tool. A writing agent writes only inside the files it **owns**
(`owns:` in the workflow entry); with nothing owned its write tools are removed. Two running agents never own the same file or a
folder and a file in it: `OwnershipClash` is raised before anything starts. All writes go through the Engine's review, undo and
protected-path rules. Every agent runs a tool loop: one tool call or a final answer per turn, checked in code.

## 3. Workflow primitives (exact semantics)

| Primitive | Semantics |
|---|---|
| `agent(defn, task, ...)` | Runs one agent. Returns the schema-validated object (or the text for a text agent); **None** on any failure (bad reply, schema break, budget, engine error). The reason is in the record. |
| `parallel(specs)` | A **barrier**: returns after every item finished, results in order. A thunk that throws or an agent that fails is **None**. At most `slots` agents use a model at once. Ownership of all items is checked first. |
| `pipeline(items, *stages)` | Each item flows through the stages on its own: **no barrier** between stages. A stage that throws (or returns None) drops that item (its result is None); other items go on. A stage is `fn(prev, item)` or an `{agent, task}` dict. |
| `phase(title)` | Labels the agents that follow; usable as `with phase("Audit"):`, restored on exit. |
| `log(text)` | A narrator line in the journal. |
| `budget` | `.total`, `.spent`, `.remaining()`, `.exhausted()`, `.status()`. Once exhausted, no agent starts: `agent()` raises `CrewBudgetExhausted` (inside parallel/pipeline it shows as None), and the run ends **not_sure**, never pass. The check is repeated when an agent gets its slot, so queued agents refuse too. A running agent's own budget is capped to what is left. |
| `resume` | `crew --resume [RUN_ID]` replays every agent whose definition, rendered prompt (which holds upstream results), model and label are unchanged and whose earlier verdict was ok: instantly, zero model calls, journaled `replayed`. A writer replays only while its owned files are as it left them. A changed prompt reruns that agent and everything downstream of it. `--no-replay` turns it off. A cross-run step cache (`squad.step_cache_days`) does the same for identical inputs and files; never under `ARCHITECT_NO_CACHE`. |
| concurrency | `slots` = Settings > Server > parallel (or `--slots`); one counting semaphore around every model-using agent. |

Workflow YAML (`workflows/*.yaml`) phases: `agent`, `parallel`, `pipeline`, `loop` (max, until), `panel` (n judges, majority vote),
`code`, `log`, with `when:`, `inputs:`, `returns:`, `budget:`. See `describe(name)`.

## 4. Journal and telemetry

`<data_dir>/orchestration/local/<run_id>/journal.jsonl`: `launched`, `phase`, `started`, `result`, `log`, `replayed`, `cached`,
`skipped`, `loop`, `panel`, `budget_exhausted`, `phase_done`, `done` (the same event names as a Claude workflow journal), plus
`agent-<id>.json` (brief, tools, tokens in/out, cache and prefix-reuse figures, peak context, result, verdict, why, edited files,
step key) and `agent-<id>.trace.jsonl`. `arch_orchestration` reads these next to Claude transcripts with the same columns. Local
counters go to `arch_telemetry` (`crew.launched`, `crew.agent`, `crew.done`); nothing leaves the machine.

## 5. Playbooks learned from Claude

`python arch_playbook.py` (`arch_playbook.mine()`) reads `arch_orchestration.snapshot()` and writes `playbooks/*.md` (pattern, when to
use, brief structure, what verifiers checked, lessons), `workflows/learned_<shape>.yaml` (local tiers) and
`agents/learned-brief-<hash>.md`, each tagged `source: claude-orchestration`, named by content hash, so re-runs change nothing.
Claude-written text is retrieval/playbook material only: `arch_dataset` reads only `.arch/runs/*/trace.jsonl`, never these folders.

## 6. Claude Code feature -> local equivalent

| Claude Code | Here |
|---|---|
| `.claude/agents/<name>.md` (frontmatter + prompt) | `agents/<name>.md`, same frontmatter keys; `.arch/agents/` per project |
| `description` (when to delegate) | `description`; the `router` agent and the hub list use it |
| `tools:` allow-list | `tools:` mapped to `arch_tools`; read-only enforced in `validate()` |
| `model: sonnet/opus/haiku/inherit` | `worker` / `heavy` / `router` tier or a local id; `inherit` = worker; Claude models refused |
| `effort` | `effort: low/medium/high` -> thinking off / default / on |
| Task / Agent tool (subagent) | `Crew.agent()` |
| Workflow tool: `agent`, `parallel`, `pipeline`, `phase`, `log`, `budget` | the same names in `arch_squad` (section 3) |
| Workflow resume from journal | `crew --resume`, `replayed` events, step cache |
| Workflow files | `workflows/*.yaml` and `describe()`, or a Python script using `arch_squad.start()` |
| Subagent isolation / worktrees | one file owner per agent, `OwnedEngine` refuses other writes |
| Permission modes (plan, acceptEdits) | read-only vs `owns:`; Engine review/undo; no bypass |
| Hooks | `arch_hooks.py` (`before_run`, `after_apply`, ...) |
| Skills | `arch_skills.py` + `skills/` |
| Slash commands | `arch_commands.py` + `commands/` |
| Plugins | `arch_plugins.py` (agents/workflows from enabled plugins) |
| Sessions / transcripts | `.arch/runs`, the crew journal, `arch_orchestration` |
| Cost / token accounting | per-agent record totals, `Budget`, `arch_costs` (local share) |
| Lead learns from past orchestration | `arch_playbook.mine()` |

## 7. Shipped profiles

planner, architect, spec-writer (text spec), spec-extractor (code), interface-designer, coder, test-author, debugger, refactorer,
reviewer, security-reviewer, perf-tuner, ui-builder, ux-reviewer, docs-writer, explainer, summarizer, router, data-engineer,
sql-author, release-manager, incident-responder, dependency-auditor, migration-planner, harness-engineer (changes this app by the
CLAUDE.md conventions), app-scaffolder (starts a project from `arch_templates_catalog`; `workflows/app_start.yaml` then adds the
first feature, and `feature_squad` continues from there), plus auditor, builder, verifier, si-planner, ui-reviewer.
Writers: coder, test-author, refactorer, docs-writer, ui-builder, data-engineer, sql-author, harness-engineer, app-scaffolder, builder.
