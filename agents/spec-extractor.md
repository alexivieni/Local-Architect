---
name: spec-extractor
description: "Pulls the request's own contract out of its words (arch_spec.spec, code only, no model)."
model: worker
tools: read
effort: low
color: purple
role: planner
category: architecture
code: spec
writes: false
max_steps: 1
budget: calls=1
context: fresh:4000
output: {"properties":{"files":{"type":"array"},"interface":{"type":"object"},"invariants":{"type":"array"},"usages":{"type":"array"}},"required":["interface","usages","invariants","files"],"type":"object"}
task: |
  {goal}
  {task}
---
Code agent: arch_spec.spec reads the interface, usages, invariants and named files from the request.
