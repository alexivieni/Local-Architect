# Self-improvement in Architect

The app can work on its own code the way a lead runs a team: audit, plan, build, verify, review, with every step journaled. It does
this on your local models, in a throwaway copy, and code (not a model) decides what may go in.

```
python architect.py self-improve --goal "make the doctor report disk space per model" [--rounds 3] [--fake] [--yes]
python architect.py self-improve                     no goal: picks the top failing check, then the most frequent telemetry error, then the advisor
python architect.py self-improve --resume SESSION    one more round on an earlier session's copy
python architect.py self-rollback | self-history | self-verify
hub: Machine > Improve itself
```

## One round (`workflows/self_improve.yaml`, run by `arch_selfimprove.loop`)
```
copy      make_candidate: the code only (never settings, results or run history), under <home>/.arch_self/candidates/
Audit     3 auditors in parallel, read-only (correctness, safety, tests)
Plan      si-planner -> {builders: [{owns, task}]}; check_plan (code): real paths in the copy, one owner per file, at most 4 builders x 6 files,
          never generated files, never the gates (GATE_FILES) without --allow-gate-edits
Build     one builder per plan entry, in parallel, each may write only its owned files (enforced by the squad engine)
Verify    an adversarial verifier reads the plan and the diff; its verdict is advice fed to the next round
Gates     round_gates (code), each pass | fail | could-not-run:
            the copy differs; the gates themselves are untouched; only owned files changed; it compiles; no new fake code;
            no new network calls (urllib/http.client/socket/requests/... counted per changed file); the selftest suites that cover
            the changed modules pass (own suite + suites that import it, run in the copy, network-restricted);
            the copy's own self-test; at least as many checks as the install; no eval regression (arch_eval.compare, oracle fake)
```
Cheap gates all run so the timeline shows every problem; the expensive ones are `could-not-run` after a cheap one failed. A round
promotes only when every gate is `pass`: `could-not-run` blocks, as it does everywhere else in Architect.

## The loop
A failed round's gate failures (and a verifier "fail") become `{feedback}` in the next round's planner brief, in the same copy. The loop
stops when every gate passes, when a round fails the same gates with the same diff as the one before (no progress), or after N rounds.
On all-pass it asks you (or `--yes`); `promote_session` re-checks that the copy and install still match what the gates saw, zips what it
will overwrite, copies atomically, rewrites MANIFEST.sha256, and `self-rollback` restores the bytes.

## Visible
- Each round is a crew run `<session>-r<n>` in `<data>/orchestration/local/` (phases, agents, `plan_check` and `gate` events, log lines):
  the Orchestration and Squad runs pages show it.
- Telemetry: `selfimprove.start`, `.round`, `.gate`, `.end`, `.promote`, `.discard`, `.rollback`.
- The session (rounds, phases, agents, plan, gates, diff, status) is `<home>/.arch_self/loops/<id>.json`; the hub page reads it through
  `GET /api/arch/selfimprove/sessions|session?id=|suggest`; `POST .../start|again|promote|discard|rollback` need the hub token, and
  promote needs `confirm: true`. The hub never passes `--yes`.

## --fake
The scripted fake model (`arch_squad.start_fake`, loopback) runs every agent; a scripted plan and edit stand in for the planner and
builder (round 1 adds a stub, which the fake-code gate stops; round 2 implements it). The full self-test, check floor and eval are
`could-not-run` in --fake unless `--full-gates`, so a fake session cannot be promoted by accident.

## Still true from the first version
`improve()`/`gate()` (positional goal, any flow, `--flow`) are kept unchanged. Tests: `arch_selftest_selfimprove.py` (the loop, fixture
install, scripted runners) and `arch_selftest_self.py` (the original pipeline, `selftest --suite full`).
