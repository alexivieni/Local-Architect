---
name: deploy-checklist
description: Pre-deployment verification checklist for shipping a release, migrations, feature flags, rollback triggers
when: deploy, release, ship, rollout, rollback, migration, go live, pre-deploy
---
# Deploy checklist

Produce the checklist below for the named service or release. Customize it, then fill in what you can verify locally.

## Customization (add items under the right section)
- Feature flags -> add flag verification steps.
- Database migration -> add migration checks (run on a copy, reversible, backfill time).
- Breaking API change -> add consumer notification steps.

## Local checks
- Release diff and PR approval: `git log <last-tag>..HEAD --oneline`, `git status` (clean tree?). Approvals in a hosting service: "not available locally".
- CI status: run the project's test command and report pass/fail. A remote pipeline is "not available locally".
- Rollback thresholds: use numbers the user gives; otherwise leave `[X]` for them to fill. Never invent baselines.

## Output (exact format)

```markdown
## Deploy Checklist: [Service/Release]
**Date:** [Date] | **Deployer:** [Name]

### Pre-Deploy
- [ ] All tests passing in CI
- [ ] Code reviewed and approved
- [ ] No known critical bugs in release
- [ ] Database migrations tested (if applicable)
- [ ] Feature flags configured (if applicable)
- [ ] Rollback plan documented
- [ ] On-call team notified

### Deploy
- [ ] Deploy to staging and verify
- [ ] Run smoke tests
- [ ] Deploy to production (canary if available)
- [ ] Monitor error rates and latency for 15 min
- [ ] Verify key user flows

### Post-Deploy
- [ ] Confirm metrics are nominal
- [ ] Update release notes / changelog
- [ ] Notify stakeholders
- [ ] Close related tickets

### Rollback Triggers
- Error rate exceeds [X]%
- P50 latency exceeds [X]ms
- [Critical user flow] fails
```
Run it before every deploy, even routine ones. Decide rollback criteria before deploying, not during.
