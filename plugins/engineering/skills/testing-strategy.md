---
name: testing-strategy
description: Design test strategies and test plans: what to test, test types, coverage targets, example cases
when: test strategy, test plan, how should we test, what tests do we need, write tests for, coverage
---
# Testing strategy

Balance coverage, speed and maintenance. Pyramid:

```
        /  E2E  \         Few, slow, high confidence
       / Integration \     Some, medium speed
      /    Unit Tests  \   Many, fast, focused
```

## Strategy by component type
- **API endpoints:** unit tests for business logic, integration tests for the HTTP layer, contract tests for consumers.
- **Data pipelines:** input validation, transformation correctness, idempotency tests.
- **Frontend:** component tests, interaction tests, visual regression, accessibility.
- **Infrastructure:** smoke tests, chaos engineering, load tests.

## What to cover
Focus on: business-critical paths, error handling, edge cases, security boundaries, data integrity. Skip: trivial getters/setters, framework code, one-off scripts.

## Steps
1. Inventory: `ls` the tests folder and grep existing test names for the area; note what is covered.
2. List the gaps against "what to cover".
3. Write the plan.

## Output
A test plan with: what to test, test type for each area, coverage targets, example test cases (concrete inputs and expected results), and the gaps in existing coverage. Check the real test runner (pytest, unittest) from the project before naming commands.
