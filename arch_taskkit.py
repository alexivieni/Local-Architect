"""Task templates: real, ready-to-run goals for the jobs people actually give a coding agent, each tied to the flow that PROVES it.

A template is a goal sentence with one blank ({target}) plus the flow, domains and review mode that suit it. The hub's Task page shows them
grouped; the CLI can list or expand them:  python architect.py tasks [--group G]   and   run "$(architect.py tasks --expand fix-bug 'totals are off by one')".
Nothing here is a model prompt trick: the flow still decides pass or fail with tests, scanners and benchmarks.
"""
import re

GROUPS = ["Build", "Fix", "Quality", "Speed", "Security and infra", "Ship"]

TEMPLATES = [
    # ---- Build
    {"id": "new-project", "group": "Build", "title": "Start a new project", "flow": "new", "needs": "text", "ph": "a command-line tool that converts CSV files to JSON",
     "goal": "Create {target}, with tests", "hint": "Builds a working, tested starter first, then your goal on top of it."},
    {"id": "feature-fullstack", "group": "Build", "title": "Add a full-stack feature", "flow": "feature", "needs": "text", "ph": "comments on posts",
     "goal": "Add a full-stack feature: {target}. Data, API and UI, with an end-to-end test", "domains": ["api", "web", "data"], "hint": "Contract first; the end-to-end test must pass."},
    {"id": "add-endpoint", "group": "Build", "title": "Add an API endpoint", "flow": "build", "needs": "text", "ph": "GET /orders/{id} that returns 404 for an unknown id",
     "goal": "Add an API endpoint: {target}. Validate the input and cover the success and failure paths with tests", "domains": ["api"]},
    {"id": "design-spec", "group": "Build", "title": "Design before building", "flow": "design", "needs": "text", "ph": "a booking service for a small clinic",
     "goal": "Design {target}: a spec, architecture decision records, an OpenAPI file and a database schema", "hint": "Writes docs only; nothing is built."},
    {"id": "db-migration", "group": "Build", "title": "Write a database migration", "flow": "build", "needs": "text", "ph": "add an orders.status column with a default of 'new'",
     "goal": "Write a database migration to {target}, with a rollback, and a test that applies it forwards and backwards", "domains": ["data"]},
    # ---- Fix
    {"id": "fix-bug", "group": "Fix", "title": "Fix a bug (reproduce first)", "flow": "bugfix", "needs": "text", "ph": "totals are off by one when the cart has a coupon",
     "goal": "Fix this bug: {target}. Write a failing reproduction test first, then the smallest fix", "hint": "The repro must fail for the right reason before any fix is accepted."},
    {"id": "hunt-bugs", "group": "Fix", "title": "Hunt for bugs", "flow": "hunt", "needs": "path", "ph": "stats.py",
     "goal": "Hunt for bugs in {target}. Every suspicion must be proven by a failing test", "hint": "Unproven suspicions are discarded; confirmed bugs go to docs/bugs.md."},
    {"id": "flaky-test", "group": "Fix", "title": "Fix a flaky test", "flow": "bugfix", "needs": "path", "ph": "tests/test_queue.py::test_retry",
     "goal": "Find and fix the cause of the flaky test {target}. Show it failing, then passing repeatedly, without loosening what it asserts"},
    # ---- Quality
    {"id": "strengthen-tests", "group": "Quality", "title": "Strengthen the tests", "flow": "test", "needs": "path", "ph": "calc.py",
     "goal": "Strengthen the tests for {target}: cover edge cases and error paths", "hint": "Mutation-tested: broken code must make a new test fail."},
    {"id": "remove-fake-code", "group": "Quality", "title": "Remove fake code", "flow": "purge", "needs": "path", "ph": "the whole project",
     "goal": "Remove the fake code in {target}: implement every stub for real, give every test a real assertion, and replace placeholder text", "hint": "Real tests may not be deleted to pass."},
    {"id": "refactor", "group": "Quality", "title": "Refactor without changing behaviour", "flow": "refactor", "needs": "path", "ph": "pricing.py",
     "goal": "Refactor {target}: remove duplicated logic and long functions. Behaviour must not change", "hint": "Characterization tests pin today's behaviour first."},
    {"id": "error-handling", "group": "Quality", "title": "Add error handling and validation", "flow": "build", "needs": "path", "ph": "the payment module",
     "goal": "Add input validation and clear error handling to {target}, with tests that exercise every failure path"},
    {"id": "review", "group": "Quality", "title": "Review a module", "flow": "review", "needs": "path", "ph": "the payment module",
     "goal": "Review {target} for correctness, error handling and maintainability", "hint": "Tool facts and the reviewer's opinion are kept apart in docs/review.md."},
    # ---- Speed
    {"id": "optimize-code", "group": "Speed", "title": "Make code faster", "flow": "optimize", "needs": "path", "ph": "dedupe.py",
     "goal": "Make {target} faster. Keep a change only if the tests pass and it is measurably quicker", "hint": "Benchmark, change, benchmark again; otherwise rolled back."},
    {"id": "optimize-sql", "group": "Speed", "title": "Speed up slow queries", "flow": "optimize", "needs": "path", "ph": "the reporting queries",
     "goal": "Optimise the slow queries in {target} using their query plans", "domains": ["data"]},
    # ---- Security and infra
    {"id": "fix-security", "group": "Security and infra", "title": "Fix security findings", "flow": "secure", "needs": "path", "ph": "the auth module",
     "goal": "Fix the security findings in {target}", "hint": "The same scanner is re-run to prove each fix."},
    {"id": "harden-config", "group": "Security and infra", "title": "Harden Docker and CI files", "flow": "config", "needs": "path", "ph": "the project",
     "goal": "Harden the Dockerfile, CI and compose files in {target}"},
    {"id": "setup-ci", "group": "Security and infra", "title": "Set up CI for this project", "flow": "cicd", "needs": "text", "ph": "run the tests on every push and pull request",
     "goal": "Set up CI: {target}", "hint": "Written from what the project really has, checked by code, and its tests are run once locally."},
    {"id": "fix-ci", "group": "Security and infra", "title": "Fix a failing or unsafe pipeline", "flow": "cicd", "needs": "text", "ph": "the workflow fails on npm ci",
     "goal": "Fix the CI pipeline: {target}", "hint": "A fix that hides a failure (|| true, continue-on-error) is rejected by the verifier."},
    {"id": "fix-network", "group": "Security and infra", "title": "Fix a network plan", "flow": "network", "needs": "path", "ph": "network/plan.json",
     "goal": "Fix the subnet and firewall plan in {target}", "hint": "Real CIDR and firewall-rule math decides."},
    {"id": "ml-pipeline", "group": "Security and infra", "title": "Build a leak-free ML pipeline", "flow": "ml", "needs": "text", "ph": "scaling and splitting a tabular dataset",
     "goal": "Build a deterministic, leak-free ML pipeline for {target}, with tests", "hint": "Determinism is proven by running the tests twice."},
    {"id": "pentest-writeup", "group": "Security and infra", "title": "Pentest write-up (authorized scope only)", "flow": "pentest", "needs": "path", "ph": "findings.json",
     "goal": "Write up the findings in {target} against scope.json", "review": "dry", "hint": "Scope-checked; only findings you supply are used."},
    # ---- Ship
    {"id": "add-ci", "group": "Ship", "title": "Add CI", "flow": "build", "needs": "text", "ph": "run the tests on every push and pull request",
     "goal": "Add a CI workflow that will {target}"},
    {"id": "dockerize", "group": "Ship", "title": "Add Docker", "flow": "build", "needs": "text", "ph": "this project",
     "goal": "Add a Dockerfile and docker-compose file for {target}: non-root user, pinned base image and a health check"},
    {"id": "document", "group": "Ship", "title": "Document the project", "flow": "design", "needs": "text", "ph": "this project",
     "goal": "Write a README for {target}: what it is, how to install, run and test it, and the main design decisions"},
]


def all_templates():
    return [dict(t, domains=list(t.get("domains") or []), review=t.get("review", "ask")) for t in TEMPLATES]


def get(tid):
    return next((t for t in all_templates() if t["id"] == tid), None)


def expand(tid, target=None):
    """-> the goal sentence with the blank filled (the template's own example when `target` is empty)."""
    t = get(tid)
    if not t:
        raise KeyError(f"unknown task template: {tid}")
    return t["goal"].replace("{target}", (target or "").strip() or t["ph"])


def check():
    """-> list of problems with the table itself (empty when it is sound). Used by the self-test."""
    import arch_flows as fl
    bad, seen = [], set()
    for t in TEMPLATES:
        if t["id"] in seen:
            bad.append(f"{t['id']}: duplicate id")
        seen.add(t["id"])
        if t["group"] not in GROUPS:
            bad.append(f"{t['id']}: unknown group {t['group']}")
        if t["flow"] not in fl.FLOWS:
            bad.append(f"{t['id']}: unknown flow {t['flow']}")
        if t["goal"].count("{target}") != 1 or re.search(r"\{(?!target\})", t["goal"].replace("{target}", "")):
            bad.append(f"{t['id']}: the goal needs exactly one {{target}} and no other braces")
        if t["needs"] not in ("text", "path"):
            bad.append(f"{t['id']}: needs must be text or path")
        if not t.get("ph"):
            bad.append(f"{t['id']}: needs an example")
        for d in t.get("domains") or []:
            if d not in fl.DOMAINS:
                bad.append(f"{t['id']}: unknown domain {d}")
    return bad
