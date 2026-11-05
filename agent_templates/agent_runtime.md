---
name: agent_runtime
title: Run agent briefs on the local models
purpose: Builds the runtime that carries out an agent brief or template on the local models through the tool loop, writes only to its own files, stops on budget or stall, logs every step like a Claude run, and joins the fleet.
why: Many of today's briefs suit a small local model with tools, which costs nothing and can run its own tests. The runtime touches the fleet's GPU serialisation and the run-log format other pages read, so it needs a careful, strong model.
# local_ok: no
# tier: heavy
mode: acceptEdits
model:
budget_usd: 4
files: arch_localagent.py, arch_selftest_localagent.py, arch_fleet.py, agent_templates
tests: python arch_selftest_localagent.py; python arch_selftest_fleet.py
footer: common
source: brief_localagents.md
---
You are the local-agent-runtime agent for "{app}". YOUR FILES: NEW arch_localagent.py, NEW arch_selftest_localagent.py,
agent_templates/ (add files; do not delete), arch_fleet.py (only to accept kind "local_agent").
Read CLAUDE.md, arch_tools.py (TOOLS and schema()), arch_dataflow.py (the tool_loop step), arch_agentlib.py and agent_templates/,
arch_fleet.py, arch_claudecode.py (the cc_runs record shape).
1. run(template_or_brief, root, files, budget, tier) carries out the agent through the tool loop, refuses writes outside its files,
   stops on its step budget and on a stall, and writes every step as events in the cc_runs format with by_claude false.
2. Give every brief that has no template one, with a local_ok line (yes for a small worker with tools; no for heavy judgment) and a
   suggested tier.
3. arch_fleet: kind local_agent runs under the same GPU serialisation as kind local.
4. Tests with a scripted local model: the own-files rule, budget and stall stops, events in cc_runs format, fleet integration, every
   template header parses.
