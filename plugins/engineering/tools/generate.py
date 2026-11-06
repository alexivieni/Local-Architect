"""Generates agents/*.yaml and workflows/*.yaml of plugins/engineering from compact specs (run once; output is committed data)."""
import json
import os
import sys

OUT = sys.argv[1]
S = {"type": "string"}
I15 = {"type": "integer", "minimum": 1, "maximum": 5}


def arr(items):
    return {"type": "array", "items": items}


def obj(props, req=None):
    return {"type": "object", "properties": props, "required": list(req if req is not None else props)}


def enum(*v):
    return {"type": "string", "enum": list(v)}


STRS = arr(S)
TL = arr(obj({"time": S, "event": S}))
FINDINGS = obj({"findings": arr(obj({"file": S, "line": {"type": "integer"}, "problem": S,
                                     "severity": enum("Critical", "High", "Medium", "Low"), "fix": S})), "summary": S})

# ---------------- final schemas (one per skill output format; render.py turns each into the skill's Markdown)
SCHEMAS = {
    "code-review": obj({"title": S, "summary": S,
                        "critical_issues": arr(obj({"file": S, "line": {"type": "integer"}, "issue": S, "severity": enum("Critical", "High")})),
                        "suggestions": arr(obj({"file": S, "line": {"type": "integer"}, "suggestion": S,
                                                "category": enum("Security", "Performance", "Correctness", "Maintainability")})),
                        "looks_good": STRS, "verdict": enum("Approve", "Request Changes", "Needs Discussion")}),
    "debug": obj({"issue_summary": S, "expected": S, "actual": S, "steps": S, "root_cause": S, "fix": S,
                  "prevention_tests": STRS, "prevention_guards": STRS}),
    "architecture": obj({"number": {"type": "integer"}, "title": S, "status": enum("Proposed", "Accepted", "Deprecated", "Superseded"),
                         "date": S, "deciders": S, "context": S, "decision": S,
                         "options": arr(obj({"name": S, "complexity": enum("Low", "Med", "High"), "cost": S, "scalability": S,
                                             "team_familiarity": S, "pros": STRS, "cons": STRS})),
                         "tradeoff_analysis": S, "easier": STRS, "harder": STRS, "revisit": STRS, "action_items": STRS}),
    "deploy-checklist": obj({"service": S, "date": S, "deployer": S,
                             "customizations": arr(obj({"section": enum("Pre-Deploy", "Deploy", "Post-Deploy"), "item": S})),
                             "error_rate_pct": {"type": "number"}, "p50_latency_ms": {"type": "number"}, "critical_flow": S,
                             "notes": STRS}),
    "documentation": obj({"doc_type": enum("README", "API Documentation", "Runbook", "Architecture Doc", "Onboarding Guide"), "title": S,
                          "sections": arr(obj({"key": enum("what", "quickstart", "config", "contributing", "endpoints", "auth", "ratelimits",
                                                           "sdk", "when", "prereqs", "procedure", "rollback", "escalation", "context",
                                                           "design", "decisions", "dataflow", "setup", "systems", "tasks", "contacts"),
                                               "body": S}))}),
    "incident-status": obj({"title": S, "severity": enum("SEV1", "SEV2", "SEV3", "SEV4"),
                            "status": enum("Investigating", "Identified", "Monitoring", "Resolved"), "impact": S, "last_updated": S,
                            "current_status": S, "actions_taken": STRS, "next_steps": STRS, "timeline": TL}),
    "postmortem": obj({"title": S, "date": S, "duration": S, "severity": enum("SEV1", "SEV2", "SEV3", "SEV4"), "authors": S, "summary": S,
                       "impact": STRS, "timeline": TL, "root_cause": S, "five_whys": arr(obj({"why": S, "because": S})),
                       "went_well": STRS, "went_poorly": STRS,
                       "action_items": arr(obj({"action": S, "owner": S, "priority": enum("P0", "P1", "P2"), "due": S})),
                       "lessons": S}),
    "standup": obj({"date": S, "yesterday": STRS, "today": STRS, "blockers": arr(obj({"blocker": S, "who_can_help": S}))}),
    "system-design": obj({"title": S, "functional": STRS, "non_functional": STRS, "constraints": STRS, "diagram": S, "components": STRS,
                          "data_flow": S, "api_contracts": STRS, "storage": STRS, "data_model": S, "api_design": S, "caching": S,
                          "queues": S, "error_handling": S, "load_estimate": S, "scaling": S, "failover": S, "monitoring": S,
                          "tradeoffs": arr(obj({"decision": S, "chose": S, "over": S, "why": S})), "assumptions": STRS, "revisit": STRS}),
    "tech-debt": obj({"items": arr(obj({"title": S, "category": enum("Code", "Architecture", "Test", "Dependency", "Documentation",
                                                                      "Infrastructure"),
                                         "location": S, "impact": I15, "risk": I15, "effort": I15, "justification": S})),
                      "phases": arr(obj({"phase": S, "items": STRS, "alongside": S}))}),
    "testing-strategy": obj({"subject": S, "component_type": enum("API endpoints", "Data pipelines", "Frontend", "Infrastructure", "Other"),
                             "areas": arr(obj({"area": S, "test_type": enum("Unit", "Integration", "E2E", "Contract", "Component",
                                                                            "Visual regression", "Accessibility", "Smoke", "Load", "Chaos"),
                                               "coverage_target": S, "example_cases": STRS})),
                             "gaps": STRS, "skip": STRS}),
}

RO = ["ls", "grep", "search", "read", "outline"]
RT = RO + ["run_tests"]

# name: (description, role, tools, system, lens/task line, output schema, effort)
AGENTS = {
    # code-review
    "eng-cr-correctness": ("Code review lens: correctness.", "reviewer", RO,
        "You review code for correctness only. Report only problems you saw in the code, each with file and line. Never edit.",
        "Lens CORRECTNESS: edge cases (empty input, null, overflow), race conditions, error handling and propagation, off-by-one, type safety.",
        FINDINGS, "medium"),
    "eng-cr-security": ("Code review lens: security.", "security_engineer", RO,
        "You review code for security only. Report only problems you saw in the code, each with file and line. Never edit.",
        "Lens SECURITY: SQL injection, XSS, CSRF, authentication and authorization flaws, secrets in code, insecure deserialization, path traversal, SSRF.",
        FINDINGS, "medium"),
    "eng-cr-performance": ("Code review lens: performance.", "optimizer", RO,
        "You review code for performance only. Report only problems you saw in the code, each with file and line. Never edit.",
        "Lens PERFORMANCE: N+1 queries, needless allocations, O(n^2) in hot paths, missing indexes, unbounded queries or loops, resource leaks.",
        FINDINGS, "medium"),
    "eng-cr-maintainability": ("Code review lens: maintainability.", "reviewer", RO,
        "You review code for maintainability only. Report only problems you saw, each with file and line. Never edit.",
        "Lens MAINTAINABILITY: naming clarity, single responsibility, duplication, test coverage, missing docs for non-obvious logic.",
        FINDINGS, "low"),
    "eng-cr-verify": ("Re-reads each merged finding and drops the ones the code does not support.", "evaluator", RT,
        "You are an adversarial checker. For each finding, open the file at that line. Keep it only if the code shows the problem.",
        "Check every finding in the list above. Return only the confirmed ones, with the same fields.",
        FINDINGS, "high"),
    "eng-cr-writer": ("Writes the code review in the skill's exact output format.", "reviewer", ["read"],
        "You write a code review from confirmed findings. Do not invent findings.",
        "Critical issues = severity Critical or High. Everything else goes to suggestions with one category. "
        "Verdict: Request Changes if any critical issue, Needs Discussion if unsure, else Approve.",
        SCHEMAS["code-review"], "medium"),
    # debug
    "eng-dbg-reproduce": ("Debug step 1: reproduce.", "bug_hunter", RT,
        "You reproduce a bug. Find expected vs actual behaviour and the exact steps. Run the tests if they exist.",
        "Step REPRODUCE: expected vs actual, exact reproduction steps, scope (when it started, who is affected).",
        obj({"expected": S, "actual": S, "steps": S, "scope": S, "failing_tests": STRS}), "medium"),
    "eng-dbg-isolate": ("Debug step 2: isolate the code path.", "bug_hunter", RO,
        "You narrow a bug to one component and code path. Cite files and lines you read.",
        "Step ISOLATE: narrow to the component and code path; check recent changes and the error messages.",
        obj({"component": S, "code_path": STRS, "suspects": STRS}), "medium"),
    "eng-dbg-diagnose": ("Debug step 3: hypotheses tested against the code.", "bug_hunter", RT,
        "You test hypotheses about a bug against the code and tests. Separate root cause from symptoms.",
        "Step DIAGNOSE: list hypotheses, test each by reading code or running tests, name the root cause.",
        obj({"hypotheses": arr(obj({"hypothesis": S, "result": enum("confirmed", "rejected", "unknown"), "evidence": S})),
             "root_cause": S}), "high"),
    "eng-dbg-writer": ("Debug step 4: fix and the Debug Report.", "bug_hunter", ["read"],
        "You write the debug report: propose a fix with explanation, side effects, and regression tests. Do not edit files.",
        "Step FIX: write the Debug Report from the steps above.",
        SCHEMAS["debug"], "medium"),
    # architecture
    "eng-adr-context": ("ADR: reads the project for constraints and prior decisions (docs/adr).", "architect", RO,
        "You collect the context for an architecture decision from the project: stack, constraints, prior ADRs under docs/adr.",
        "Collect context: situation, forces, constraints, non-functional needs, prior ADR numbers.",
        obj({"situation": S, "constraints": STRS, "prior_adrs": STRS, "next_number": {"type": "integer"}}), "low"),
    "eng-adr-options": ("ADR: names at least two options and assesses each.", "architect", RO,
        "You compare options for a decision. Always name at least two. Assess complexity, cost, scalability, team familiarity.",
        "Assess every option with pros and cons.",
        obj({"options": SCHEMAS["architecture"]["properties"]["options"]}), "high"),
    "eng-adr-writer": ("ADR: writes the record in the skill's exact format.", "architect", ["read"],
        "You write an Architecture Decision Record from the context and the options. Status Proposed unless told otherwise.",
        "Write the ADR: trade-off analysis, consequences (easier, harder, revisit) and action items.",
        SCHEMAS["architecture"], "medium"),
    # system-design
    "eng-sd-requirements": ("System design 1: requirements gathering.", "planner", RO,
        "You gather requirements for a system design.",
        "Requirements: functional, non-functional (scale, latency, availability, cost), constraints (team, timeline, stack).",
        obj({"functional": STRS, "non_functional": STRS, "constraints": STRS, "assumptions": STRS}), "medium"),
    "eng-sd-highlevel": ("System design 2-3: high-level design and deep dive.", "architect", RO,
        "You design the high-level architecture and its details from the requirements.",
        "High-level: components, data flow, API contracts, storage. Deep dive: data model, API design, caching, queues, error handling and retry.",
        obj({"diagram": S, "components": STRS, "data_flow": S, "api_contracts": STRS, "storage": STRS, "data_model": S,
             "api_design": S, "caching": S, "queues": S, "error_handling": S}), "high"),
    "eng-sd-scale": ("System design 4: scale and reliability.", "reliability_engineer", RO,
        "You assess scale and reliability for a design from the requirements.",
        "Scale and reliability: load estimation, horizontal vs vertical, failover and redundancy, monitoring and alerting.",
        obj({"load_estimate": S, "scaling": S, "failover": S, "monitoring": S}), "medium"),
    "eng-sd-writer": ("System design 5: trade-offs and the design document.", "architect", ["read"],
        "You assemble the design document. Make every trade-off explicit (complexity, cost, familiarity, time to market, maintainability).",
        "Merge the parts above, add trade-offs, assumptions, and what to revisit as the system grows.",
        SCHEMAS["system-design"], "medium"),
    # tech-debt
    "eng-td-code": ("Tech debt scan: code and test debt.", "reviewer", RT,
        "You find technical debt in code and tests. Cite file paths.",
        "Scan for CODE debt (duplication, poor abstractions, magic numbers) and TEST debt (low coverage, flaky or missing tests).",
        obj({"findings": arr(obj({"file": S, "line": {"type": "integer"}, "problem": S, "category": enum("Code", "Test")})), "summary": S}), "medium"),
    "eng-td-system": ("Tech debt scan: architecture and infrastructure debt.", "architect", RO,
        "You find architecture and infrastructure debt. Cite file paths.",
        "Scan for ARCHITECTURE debt (wrong boundaries, wrong data store) and INFRASTRUCTURE debt (manual deploys, no monitoring, no IaC, no CI).",
        obj({"findings": arr(obj({"file": S, "line": {"type": "integer"}, "problem": S, "category": enum("Architecture", "Infrastructure")})),
             "summary": S}), "medium"),
    "eng-td-deps-docs": ("Tech debt scan: dependency and documentation debt.", "dependency_engineer", RO,
        "You find dependency and documentation debt. Cite file paths.",
        "Scan for DEPENDENCY debt (outdated or unpinned libraries in requirements/package files) and DOCUMENTATION debt (missing runbooks, stale README).",
        obj({"findings": arr(obj({"file": S, "line": {"type": "integer"}, "problem": S, "category": enum("Dependency", "Documentation")})),
             "summary": S}), "low"),
    "eng-td-scorer": ("Tech debt: scores Impact, Risk, Effort 1-5 and plans phases (priority is computed in code).", "planner", ["read"],
        "You score technical debt items. Impact: how much it slows the team (1-5). Risk: what happens if not fixed (1-5). "
        "Effort: how hard the fix is (1-5). Do not compute the priority; code does.",
        "Score every finding above, give a business justification, and a phased plan that runs alongside feature work.",
        SCHEMAS["tech-debt"], "medium"),
    # testing-strategy
    "eng-ts-inventory": ("Testing strategy: inventory of existing tests and coverage gaps.", "tester", RT,
        "You inventory a project's tests: what exists, what runs, which modules have none.",
        "Run the tests, list test files and the modules they cover, list modules with no tests.",
        obj({"tests_pass": enum("yes", "no", "unknown"), "test_files": STRS, "untested": STRS, "component_type": S}), "low"),
    "eng-ts-planner": ("Testing strategy: the test plan in the skill's format.", "tester", ["read"],
        "You design a test plan with the testing pyramid: many unit, some integration, few E2E. "
        "Cover business-critical paths, error handling, edge cases, security boundaries, data integrity. "
        "Skip trivial getters/setters, framework code, one-off scripts.",
        "Write the plan: areas with test type, coverage target and example cases; gaps; what to skip.",
        SCHEMAS["testing-strategy"], "medium"),
    # deploy-checklist
    "eng-dc-scan": ("Deploy checklist: facts from the project (tests, migrations, flags, changelog).", "ci_engineer", RT,
        "You collect deploy facts from the project. Run the tests. Look for migrations, feature flags, CHANGELOG, breaking API changes.",
        "Collect the facts. Answer unknown when the project does not show it.",
        obj({"tests_pass": enum("yes", "no", "unknown"), "migrations": STRS, "feature_flags": STRS, "breaking_api": enum("yes", "no", "unknown"),
             "changelog": enum("yes", "no"), "notes": STRS}), "low"),
    "eng-dc-writer": ("Deploy checklist: customizations and rollback triggers.", "ci_engineer", ["read"],
        "You customize a deploy checklist. Migrations add migration checks, flags add flag checks, breaking API adds consumer notices.",
        "Give the extra checklist items and the rollback trigger thresholds.",
        SCHEMAS["deploy-checklist"], "low"),
    # documentation
    "eng-doc-survey": ("Documentation: reads the code the doc is about.", "docs_writer", RO,
        "You survey code for a document: entry points, commands, config, endpoints. Cite files.",
        "Pick the doc type (README, API Documentation, Runbook, Architecture Doc, Onboarding Guide) and collect facts for it.",
        obj({"doc_type": SCHEMAS["documentation"]["properties"]["doc_type"], "facts": STRS, "files": STRS}), "low"),
    "eng-doc-writer": ("Documentation: writes the doc with the type's required sections.", "docs_writer", ["read"],
        "You write technical docs. Write for the reader, most useful first, show with commands and code, link instead of copying.",
        "Write every required section for the doc type, one section per key.",
        SCHEMAS["documentation"], "medium"),
    # incident
    "eng-ir-triage": ("Incident: triage severity with the SEV1-4 table.", "reliability_engineer", RO,
        "You triage an incident. SEV1 service down, all users. SEV2 major feature degraded, many users. "
        "SEV3 minor feature, some users. SEV4 cosmetic. Facts only.",
        "Assess severity, affected systems and users, roles (IC, comms, responders).",
        obj({"severity": enum("SEV1", "SEV2", "SEV3", "SEV4"), "affected": STRS, "roles": STRS, "facts": STRS}), "medium"),
    "eng-ir-status": ("Incident: status update in the skill's format.", "reliability_engineer", ["read"],
        "You write a factual incident status update: what is happening, who is affected, what we are doing, when the next update is. No speculation.",
        "Write the status update.",
        SCHEMAS["incident-status"], "low"),
    "eng-pm-timeline": ("Postmortem: timeline and 5 whys.", "reliability_engineer", RO,
        "You reconstruct an incident timeline and run 5 whys down to a root cause. Blameless: systems and processes, not people.",
        "Rebuild the timeline (UTC) and the 5 whys.",
        obj({"timeline": TL, "five_whys": arr(obj({"why": S, "because": S})), "root_cause": S}), "high"),
    "eng-pm-writer": ("Postmortem: the blameless postmortem document.", "reliability_engineer", ["read"],
        "You write a blameless postmortem. Systems and processes, never individuals. Status Draft.",
        "Write the postmortem with action items that each have an owner, P0/P1/P2 and a due date.",
        SCHEMAS["postmortem"], "medium"),
    # standup
    "eng-standup": ("Standup: yesterday / today / blockers.", "planner", ["ls", "read", "memory_read"],
        "You format a standup update. Short, action-oriented. Use the person's notes; you may read .arch/runs and CHANGELOG for recent work.",
        "Write Yesterday, Today, Blockers (with who can help).",
        SCHEMAS["standup"], "low"),
}

WORKFLOWS = {
    "eng-code-review": ("code-review", "Four review lenses in parallel, merged in code, each finding re-checked, written in the code-review format.", [
        {"title": "Lenses", "parallel": [{"agent": a, "label": "lens:" + a[7:]} for a in
                                         ("eng-cr-correctness", "eng-cr-security", "eng-cr-performance", "eng-cr-maintainability")]},
        {"title": "Dedupe", "code": "dedupe_findings"},
        {"title": "Verify", "agent": "eng-cr-verify", "label": "verify"},
        {"title": "Write", "agent": "eng-cr-writer", "label": "write"}]),
    "eng-debug": ("debug", "Reproduce, isolate, diagnose, fix: one agent per step.", [
        {"title": "Reproduce", "agent": "eng-dbg-reproduce", "label": "reproduce"},
        {"title": "Isolate", "agent": "eng-dbg-isolate", "label": "isolate"},
        {"title": "Diagnose", "agent": "eng-dbg-diagnose", "label": "diagnose"},
        {"title": "Fix", "agent": "eng-dbg-writer", "label": "fix"}]),
    "eng-architecture": ("architecture", "Context, options, then the ADR.", [
        {"title": "Context", "agent": "eng-adr-context", "label": "context"},
        {"title": "Options", "agent": "eng-adr-options", "label": "options"},
        {"title": "ADR", "agent": "eng-adr-writer", "label": "adr"}]),
    "eng-system-design": ("system-design", "Requirements, then high-level design and scale side by side, then trade-offs.", [
        {"title": "Requirements", "agent": "eng-sd-requirements", "label": "requirements"},
        {"title": "Design", "parallel": [{"agent": "eng-sd-highlevel", "label": "design:high-level"},
                                         {"agent": "eng-sd-scale", "label": "design:scale"}]},
        {"title": "Document", "agent": "eng-sd-writer", "label": "document"}]),
    "eng-tech-debt": ("tech-debt", "Three debt scanners in parallel, merged in code, then scored and planned.", [
        {"title": "Scan", "parallel": [{"agent": "eng-td-code", "label": "scan:code-test"},
                                       {"agent": "eng-td-system", "label": "scan:arch-infra"},
                                       {"agent": "eng-td-deps-docs", "label": "scan:deps-docs"}]},
        {"title": "Merge", "code": "dedupe_findings"},
        {"title": "Score", "agent": "eng-td-scorer", "label": "score"}]),
    "eng-testing-strategy": ("testing-strategy", "Inventory of the tests, then the test plan.", [
        {"title": "Inventory", "agent": "eng-ts-inventory", "label": "inventory"},
        {"title": "Plan", "agent": "eng-ts-planner", "label": "plan"}]),
    "eng-deploy-checklist": ("deploy-checklist", "Deploy facts from the project, then the customized checklist.", [
        {"title": "Facts", "agent": "eng-dc-scan", "label": "facts"},
        {"title": "Checklist", "agent": "eng-dc-writer", "label": "checklist"}]),
    "eng-documentation": ("documentation", "Survey the code, then write the document.", [
        {"title": "Survey", "agent": "eng-doc-survey", "label": "survey"},
        {"title": "Write", "agent": "eng-doc-writer", "label": "write"}]),
    "eng-incident": ("incident-status", "Triage, then a status update.", [
        {"title": "Triage", "agent": "eng-ir-triage", "label": "triage"},
        {"title": "Update", "agent": "eng-ir-status", "label": "update"}]),
    "eng-postmortem": ("postmortem", "Timeline and 5 whys, then the blameless postmortem.", [
        {"title": "Timeline", "agent": "eng-pm-timeline", "label": "timeline"},
        {"title": "Postmortem", "agent": "eng-pm-writer", "label": "postmortem"}]),
    "eng-standup": ("standup", "One agent: the standup update.", [
        {"title": "Standup", "agent": "eng-standup", "label": "standup"}]),
}


def y(v, ind=0):
    """Minimal block YAML emitter (strings JSON-quoted, which is valid YAML)."""
    sp = "  " * ind
    out = []
    if isinstance(v, dict):
        for k, x in v.items():
            if isinstance(x, (dict, list)) and x:
                out.append(f"{sp}{k}:")
                out.append(y(x, ind + 1))
            elif isinstance(x, str) and "\n" in x:
                out.append(f"{sp}{k}: |")
                out.extend(sp + "  " + ln for ln in x.rstrip("\n").split("\n"))
            else:
                out.append(f"{sp}{k}: {json.dumps(x)}")
    else:
        for x in v:
            if isinstance(x, dict):
                body = y(x, ind + 1).split("\n")
                out.append(sp + "- " + body[0].lstrip())
                out.extend(body[1:])
            else:
                out.append(f"{sp}- {json.dumps(x)}")
    return "\n".join(out)


os.makedirs(os.path.join(OUT, "agents"), exist_ok=True)
os.makedirs(os.path.join(OUT, "workflows"), exist_ok=True)
for name, (desc, role, tools, system, lens, schema, effort) in AGENTS.items():
    d = {"name": name, "description": desc, "model": "worker", "role": role, "category": "engineering",
         "system": system + "\nAnswer with the JSON object the schema asks for and nothing else.\n",
         "task": "Goal: {goal}\n" + lens + "\nWhat came before:\n{prev}\n{task}\n",
         "tools": tools, "writes": any(t in ("memory_append", "apply", "edit_lines") for t in tools), "output": schema,
         "budget": {"calls": 8, "tokens": 40000, "seconds": 900}, "effort": effort, "max_steps": 6,
         "context": {"policy": "inherit", "max_chars": 8000}}
    with open(os.path.join(OUT, "agents", name + ".yaml"), "w", encoding="utf-8", newline="\n") as f:
        f.write("# Generated from the engineering plugin spec (local, worker tier). Adapted from Anthropic's engineering plugin, Apache-2.0.\n")
        f.write(y(d) + "\n")
for name, (fmt, desc, phases) in WORKFLOWS.items():
    w = {"name": name, "description": desc, "phases": phases, "returns": SCHEMAS[fmt]}
    with open(os.path.join(OUT, "workflows", name + ".yaml"), "w", encoding="utf-8", newline="\n") as f:
        f.write(f"# Output format: {fmt} (render.py turns the result into the skill's Markdown).\n")
        f.write(y(w) + "\n")
with open(os.path.join(OUT, "schemas.json"), "w", encoding="utf-8", newline="\n") as f:
    json.dump({"schemas": SCHEMAS, "workflows": {n: v[0] for n, v in WORKFLOWS.items()}}, f, indent=1)
print(len(AGENTS), "agents", len(WORKFLOWS), "workflows")
