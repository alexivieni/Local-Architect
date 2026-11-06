---
name: system-design
description: Design systems, services and architectures: requirements, components, API and data modeling, scale and trade-offs
when: system design, design a system, how should we architect, service boundaries, api design, data model
---
# System design

Work through the five parts in order and keep each short.

1. **Requirements gathering:** functional (what it does); non-functional (scale, latency, availability, cost); constraints (team size, timeline, existing stack).
2. **High-level design:** component diagram (ASCII), data flow, API contracts, storage choices.
3. **Deep dive:** data model; API endpoints (REST, GraphQL, gRPC); caching strategy; queue/event design; error handling and retries.
4. **Scale and reliability:** load estimation; horizontal vs vertical scaling; failover and redundancy; monitoring and alerting.
5. **Trade-off analysis:** every decision has trade-offs, make them explicit (complexity, cost, team familiarity, time to market, maintainability).

## Output
A structured design document with these five headings, an ASCII or described diagram, explicit assumptions, and a trade-off section. End with "What I would revisit as the system grows". Read existing code in the project first and design on top of it. If numbers (users, rps) are missing, state an assumption and label it as one.
