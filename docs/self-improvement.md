# Self-improvement in Architect

The harness can improve its own code. The shape is the same as the generic recipe (plan one improvement, implement, test, review, document), with the
decisions made the way Architect makes them: code gates, undo, nothing remote.

```
python architect.py self-improve "goal"      (hub: Health > Tune & improve > Self-improvement)
   ├─ planner   (your model)   reads the ledger (roles that fail or retry most), the last run reports and the goal; picks ONE change
   ├─ generator (your model)   edits a COPY of the install
   ├─ gate      (code)         the full selftest suite + self-verify on the copy; a red suite stops here
   ├─ reviewer  (your model)   reads the diff; its verdict is advisory, the suite's is binding
   └─ install   (code)         the copy replaces the install; MANIFEST.sha256 is rewritten; the previous version is kept for `self-rollback`
```

Guardrails that are ON (they are the reason this is safe to leave running):
- One improvement per run; protected paths (the selftests, the manifest, the guard) are never edited by the model.
- The whole suite must be green on the copy before anything is installed. `self-rollback` returns to the last good install; `self-history` lists versions.
- Budget: the run's `limits.max_calls` applies; a stalled fix loop escalates to a larger model you own, then stops.

Where improvements come from (the planner reads, in order): `hub_data/ledger.json` (roles and models with the lowest accepted rate, highest p95),
the last `.arch/runs/*/report.md`, then the goal you typed.

Not done: a nightly schedule. Run it by hand a few times, read the reports, then schedule `python architect.py self-improve "..."` with your OS scheduler if you want.
