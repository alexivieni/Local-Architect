# How a team of agents builds Architect: the method

*Written 2026-11-04 from the real material: `hub_data/learning/METHOD.md`, the briefs in `hub_data/learning/briefs/`, the
launcher `briefs/launch.py`, `hub_data/learning/ledger.jsonl` and `arch_fleet.py`. The reusable form of the briefs is
`agent_templates/` (read and filled by `arch_agentlib.py`).*

Two kinds of source are mixed below, and each section says which one it is:

- **[Anthropic]**: public Anthropic guidance or documented product behaviour (prompt caching, effort, subagents, the Claude
  Agent SDK, Anthropic's published notes on agent design). This is written from my own knowledge of those pages. I did not
  fetch them for this document, so check the current pages before relying on an exact number.
- **[Ours]**: this project's own practice, learned and measured on this machine.

---

## 1. In one paragraph

One **lead** splits the goal into **briefs**. Each brief goes to one **worker** agent, which owns a short list of files and
nothing else. Every worker has a spend cap and the same rules footer. The workers edit files but cannot run commands. When
they have all finished, the **orchestrator** (the lead by hand before; `arch_fleet.py` now) runs the tests itself, lists any
file changed that no worker owned, and keeps only what passes. Wherever two workers meet, the interface between them is
written into both briefs before either starts.

---

## 2. Lead and workers

**[Ours]** The lead was Claude Code in the desktop app. Each worker was the real Claude Code agent, started through the Claude
Agent SDK (`arch_claudecode.py`, `briefs/launch.py`). Workers bill to the API key, never the subscription: they use their own
settings folder, `hub_data/claude_code`. Up to 9 workers ran at once here.

**[Anthropic]** This is the orchestrator-workers shape from Anthropic's notes on building agents. A lead model breaks a task
down, hands the parts to workers and combines what comes back. Anthropic's write-up of its multi-agent research system adds
that each worker needs a precise task: an objective, the output expected, the tools and sources to use, and clear boundaries.
Vague tasks lead to duplicated or overlapping work. Claude Code's *subagents* follow the same idea inside one session. Each
runs in its own context window, so the lead keeps the conclusion and not the file dumps.

**[Ours]** The brief is where that precision lives. Every brief in `briefs/brief_*.md` has the same parts:
1. who you are ("You are the cost agent for Architect"),
2. **YOUR FILES**, an explicit list,
3. what to read first, marked read-only,
4. numbered deliverables, with exact function names and return shapes,
5. what the self-test must prove,
6. the common footer.

`agent_templates/` keeps exactly that shape, with `{slots}` for the parts that change.

## 3. Why each brief names its files

**[Ours]** Several agents write in one folder at the same moment. If two of them edit the same file, one overwrites the other,
or both work from a stale copy. Nobody notices until the tests fail, and by then nobody can tell which edit was right.

So:
- `arch_fleet.plan_check` refuses a plan in which two agents share a file, or a folder and a file inside it. It does this
  before anything is spent.
- The footer the fleet appends names the agent's files and forbids every other file.
- After the run, the fleet lists **strays**: files that changed but that no agent owned and that were not already dirty
  before the run.

Naming the files also makes the brief cheaper. The agent does not have to discover its scope, and it reads files instead of
searching the whole tree.

## 4. Queueing on shared files

**[Ours]** Some files cannot be split: `hub_ui.html` is the whole hub in one file. Work on it is **queued** instead of run in
parallel. Tonight the order was:

**UI build** (brief_ui) → **UX review** (brief_ux) → **visual workspace** (brief_viz).

Each brief tells the next agent what the previous one built and asks it to read that code before adding anything. Everything
else ran side by side. In `arch_fleet` the queue is the `after: [...]` field. `arch_agentlib.to_fleet_plan` refuses two
templates on one shared file unless you give one of them other files or chain them with `after`.

## 5. Why spend caps

**[Ours]** Every worker has a hard cap (`max_budget_usd` in the SDK, `budget_usd` in a fleet plan), and the fleet has a total
cap that `plan_check` enforces. Two reasons:
- **A stuck agent stops itself.** An agent in a loop (re-reading a big file, retrying a refused command) would otherwise keep
  spending until someone noticed.
- **The owner knows the worst case before starting.** The plan's total is a ceiling, not a guess.

The measured spend was far below the caps: jobs came in at about **10–25% of their caps** (METHOD.md). The template defaults
($1.5–$5) follow from that: roughly 3–5 times the spend measured for each kind of job.

**[Anthropic]** The SDK's own per-run limits (`max_budget_usd`, `max_turns`) and its permission modes (`acceptEdits`, `plan`)
are what make a cap a real stop rather than a request.

## 6. Agents edit; the orchestrator verifies

**[Ours]** Workers run in `acceptEdits` mode: they may change files but cannot run commands. That is deliberate:
- **A worker cannot mark its own homework.** The proof comes from code that the worker did not write and cannot steer: the
  test suites run by the orchestrator. This is the same rule as the rest of Architect: *code decides what passes*.
- **The orchestrator runs the commands itself.** It runs every test command at below-normal priority with a timeout, and
  accepts only commands from an allow-list (`python arch_`, `py -3.12 arch_`, `node --check`), with no shell punctuation.
- **Three outcomes, never two.** A test that timed out or could not start is `None`, so the fleet's verdict is `not_sure`,
  never a pass.

The footer says this outright ("You cannot run commands; the reviewer runs the tests"). A worker that knows this writes the
test and moves on, instead of spending turns on commands that will be refused. `arch_overseer` counts such refusals in the
agent logs, if you want the number for a given night.

## 7. Interfaces fixed up front

**[Ours]** When two agents meet, the brief of each names the exact functions, arguments and return shapes. Example from
tonight: the UI agent called `arch_costs.summary(days=7)` and `arch_agents.schedule_add(agent_id, message, at="HH:MM", ...)`
while the cost and crew agents were still writing them. Both sides read the same contract, so they met in the middle. The UI
guarded every call with `try/except`, so the hub still starts if one side is missing.

It is the same rule CLAUDE.md gives small local models (**Interface first**: the planner lists every public name with its
arguments). The `builder` template's `{interface}` slot and the `hub_builder` template's `{backend}` slot exist for this.

## 8. What it cost (measured today, `hub_data/learning/ledger.jsonl`)

| agent | cost | turns |
|---|---:|---:|
| ag_crew (scheduled runs) | $0.23 | 8 |
| ag_costs (spend summary + batches) | $0.28 | 10 |
| ag_learn (logbook) | $0.36 | 10 |
| ag_data (dataset polish) | $0.40 | 21 |
| ag_localorch (local fleet) | $0.49 | 23 |
| ag_fleet (dispatcher) | $0.95 | 13 |
| ag_scout (web search + local scout) | $1.09 | 23 |
| delegate (earlier delegated job) | $1.12 | 42 |
| delegate2 | $1.59 | 54 |
| ag_fastlane (fast lane + PC plan) | $1.78 | 42 |
| ag_tune (tuning loop) | $1.79 | 25 |
| **11 agents** | **$10.07** | |

*Measured on this machine.* The nine brief-launched agents (`ag_*`) cost $7.36 together, **$0.82 each on average**, ranging
from $0.23 to $1.79. The jobs that ran longest touched existing large modules (`arch_server.py`, `arch_autotune.py`) or
needed outside reading. New, self-contained modules were the cheapest.

**[Ours, measured]** Other numbers from METHOD.md:
- The first Claude Code turn costs about **$0.10–0.17**, because its system prompt and tools are written to the cache. Later
  turns read them at about 0.1x.
- Prompt caching on a shared prefix cut a 6-call Council answer from **$0.27 to $0.18**. Respecting the context budget cut
  it further.

**[Anthropic]** *Prompt caching*: a reused prefix (system prompt, tool definitions, project context) is billed at a fraction
of the input price when read from cache. Writing it costs a little more than a normal input once. The cache lives about 5
minutes by default (1 hour as an option). A prefix shorter than the model's minimum silently does not cache. For briefs this
means: keep the stable part (rules, footer, project context) first and identical, and put the part that changes last.
`arch_agentlib` puts the common footer at the end so that the same rules sit in the same place for every agent. Architect's
own prompts follow the same "prefix-stable" rule (CLAUDE.md).

**[Anthropic]** *Effort*: the effort setting trades thinking depth for cost and speed. Use high effort for planning and
review roles. Fill-in work with an exact brief needs less.

## 9. Lessons (from METHOD.md, measured)

- Claude Code's `claude -p` uses the logged-in subscription before an API key. An empty settings folder forces the key.
- Promotional API credit covers the Agent SDK and the API, not "Claude Code" usage, so Claude Code is run through the SDK.
- Big files must be searched for the lines a request points at; otherwise any model, Claude included, edits blind. (Hence
  "read first: X (function Y)" in every brief.)
- Checks must ignore problems that existed before the run, or a correct one-line change fails. (Hence the fleet's
  `dirty_before`.)
- Self-tests must never reach a paid model, whatever the settings say.
- Jobs came in at roughly 10–25% of their caps.
- Do not fine-tune model weights on Claude-written content (Anthropic's terms). Retrieval, playbooks, memory and better briefs
  may use it. Your local models' own verified work is the training data.

## 10. The same pattern on LOCAL models, with one 6 GB graphics card

**[Ours]** The fleet is not only for Claude. `arch_fleet` runs agents of kind `local` (`architect.py run` on this machine's
models, at no cost). The constraint is different: the limit is graphics memory, not money.

- **One GPU writer at a time.** A 6 GB card holds one worker model with its KV cache. Two local writers would evict each
  other's models, and every swap costs a server restart (5–8 s measured here, CLAUDE.md) plus a cold cache. Local agents that write are therefore queued on the card
  (`_GPU_QUEUE`, first come, first served, across fleets too). The state says who waited, for whom and why.
- **CPU router planning in parallel.** Planning-only steps (`route`, `explain`) use the small router model on the CPU, so they
  run beside the GPU writer without touching the card.
- **Claude for the thinking roles.** `arch_fleet.hybrid_plan` puts "Claude thinks, local writes" into practice. A read-only
  Claude planner per part writes a short, exact brief (at most 25 lines). The local writer for that part runs after it and
  receives the brief. One read-only Claude reviewer comes last. Planning and review need breadth of knowledge; edits to named
  files under a tight brief are what small local models do well when code checks them.
- **The same gates.** Local work is judged exactly like Claude's: tests, the verifier, the fake-code gate, three outcomes.

Rough shape of one night on this machine: Claude planners all at once (seconds to minutes, ~$0.1–0.5 each). Local writers one
after another on the card, with the CPU router planning the next part meanwhile. One Claude review at the end. Then the fleet's
own test run.

Every template except the read-only ones can run as a local agent:
`to_fleet_plan([{"template": "builder", "values": {...}, "kind": "local"}])`. Its goal is the filled brief without the common
footer; the fleet adds the local footer that names the owned files.

## 11. Using the library

```python
import arch_agentlib as lib
lib.list_templates()                       # what is there
print(lib.explain("bug_hunt"))             # what it does, why, what goes in
plan = lib.to_fleet_plan([
    {"template": "builder",  "values": {...}},
    {"template": "reviewer", "values": {...}, "after": ["builder"]},
], total_cap_usd=10)                       # ValueError in plain words if arch_fleet would refuse it
tests = lib.fleet_tests([...])             # the test commands the fleet should run afterwards
# arch_fleet.launch(plan, root, total_cap_usd=10, tests=tests)
```

From the command line: `python arch_agentlib.py list`, `python arch_agentlib.py explain reviewer`,
`python arch_agentlib.py fill builder module=arch_x suite=x ...`.

The templates:
- **General patterns:** builder, reviewer, ux_pass, research_doc, monitor, migration, bug_hunt, perf_pass, plus `common` (the
  footer).
- **Cut from tonight's briefs:** api_review, spend_report, scheduler, dataset_polish, setting_switch, dispatcher,
  knowledge_store, template_library, local_twin, online_option, step_plan, hub_builder.

Each template's header records which brief it came from.
