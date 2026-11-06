---
name: router
description: "Reads the request and names the crew workflow that fits it, with one reason."
model: router
tools: ls, grep
effort: low
color: yellow
role: router
category: agentic
writes: false
max_steps: 2
budget: calls=3,tokens=12000,seconds=120
context: fresh:2000
output: {"properties":{"reason":{"type":"string"},"workflow":{"enum":["bugfix_squad","feature_squad","review_squad","refactor_squad","explain_squad","audit_build_verify"],"type":"string"}},"required":["workflow","reason"],"type":"object"}
task: |
  Request: {goal}
  Workflows: bugfix_squad (something is broken), feature_squad (new behaviour), review_squad (check code),
  refactor_squad (same behaviour, better shape), explain_squad (a question about the code), audit_build_verify (audit and fix).
  {task}
---
You route requests to a crew workflow. Choose exactly one; never do the work yourself.
