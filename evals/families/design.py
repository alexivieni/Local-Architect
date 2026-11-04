"""design: a spec with the required headings and testable requirements, an architecture doc with a diagram, and at least one ADR."""
import _bank as B

FLOW = "design"
DEFAULT_N = 4
GOALS = ["Design a booking API for a small gym: members, classes, bookings with capacity limits.",
         "Design a notes service with tags and full-text search, single user, local storage.",
         "Design a parcel tracking backend: shipments, scan events, status derived from events.",
         "Design an inventory API for a bakery: products, stock levels, daily stock takes."]
ARCH = """# Architecture
## Overview
One HTTP service in front of one SQLite database.
## Components
| component | responsibility | owns data | talks to |
|---|---|---|---|
| api | HTTP endpoints, validation (R1, R2) | no | service |
| service | business rules (R3) | no | store |
| store | persistence | yes | sqlite |
## Diagram
```mermaid
flowchart LR
  client[Client] -->|HTTP| api[API]
  api -->|calls| service[Service]
  service -->|reads/writes| store[Store]
  store -->|SQL| db[(SQLite)]
```
## Data flow
1. The client calls the API.
2. The API validates and calls the service.
3. The service applies the rules and uses the store.
## Failure modes
- Database locked: detected by a timeout, the request returns 503.
## Testing strategy
- Unit tests for the service rules; HTTP tests for the API.
## Security and privacy basics
- Input validation at the API; no secrets in the repository.
"""
ADR = """# ADR 0001: SQLite as the store
## Status
Accepted
## Context
Single user, local, small data (R1).
## Decision
Use SQLite through the standard library.
## Alternatives considered
- PostgreSQL: rejected, needs a server for a single-user tool.
- JSON files: rejected, no transactions or indexes.
## Consequences
- Good: zero setup, transactional.
- Bad: one writer at a time.
"""


def _spec(goal):
    return ("# Spec: " + goal[:40] + "\n## Goals\n- deliver the service described\n## Non-goals\n- multi-tenant hosting, billing\n## Users and key scenarios\n- one operator uses it daily\n"
            "## Requirements\n1. R1 The service stores the listed entities durably.\n2. R2 Every endpoint validates its input and returns a clear error.\n3. R3 The main business rule is enforced and tested.\n"
            "## Constraints and assumptions\n- standard library only; single machine\n## Risks and open questions\n- none blocking\n")


def generate(n, seed=0):
    rng = B.rng_for(seed, "design")
    out = []
    for i, goal in enumerate(B.pick(rng, GOALS, n)):
        design = "DOC: docs/architecture.md\n" + ARCH + "\nDOC: docs/adr/0001-sqlite-store.md\n" + ADR
        out.append({"id": f"design-{i:02d}", "tags": ["design"], "goal": goal, "params": {"domains": ["api"]},
                    "fixture": {"files": {}},
                    "budget": {"calls": 10, "tokens": 50000, "seconds": 900},
                    "judge": [{"type": "docs_structure", "kind": "spec", "path": "docs/spec.md"}, {"type": "file_exists", "paths": ["docs/architecture.md", "docs/adr/*.md"]},
                              {"type": "docs_structure", "kind": "mermaid", "path": "docs/architecture.md"}],
                    "oracle": {"spec": _spec(goal), "design": design}, "wrong": {"files": {}}})
    return out
