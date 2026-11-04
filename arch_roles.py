"""Roles: the different "agents" of the Architect, what each one needs from a model, and how to
recommend which installed model should play which role.

A role is a job (route, plan, design, write backend code, write UI, write SQL, judge, review). Different jobs
reward different model strengths, so each role lists the Scoreboard categories it depends on and how much.
The recommendation is MEASURED when the model has scores for those categories, and a labelled GUESS
(from the model family) when it has none. Nothing here is a benchmark claim.
"""
import math
import re

# Scoreboard categories (shared with the hub). The last four are added by the Architect.
CATEGORIES = [
    ("backend_fix", "Fix a bug"), ("backend_feature", "Write a function"), ("ui", "UI / HTML+CSS"),
    ("reasoning", "Find root cause"), ("security", "Security"), ("format", "Follows format"),
    ("toolcall", "Tool calls"), ("speed", "Speed"),
    ("sql", "SQL & schema"), ("api_design", "API contract"), ("design_doc", "Design docs"), ("refactor", "Safe refactor"),
    ("testing", "Write tests"), ("bughunt", "Expose a bug"), ("optimize", "Speed up code"),
    ("secreview", "Secure code review"), ("netplan", "Network design"), ("configfix", "Config hardening"),
    ("mlaudit", "ML pipeline review"), ("detection", "Detection engineering"), ("attackpath", "Attack-path analysis"),
    ("scope", "Stays inside scope"),
    ("protocol", "Network protocol code"), ("iot", "Embedded / IoT code"),
]

EDIT_PROTOCOL = """How to change files (use ONLY these formats, nothing else changes files):

New file or full rewrite of a small file:
FILE: relative/path.ext
```lang
...complete file...
```

Small edit inside an existing file (SEARCH must match existing text exactly and be unique):
EDIT: relative/path.ext
<<<<<<< SEARCH
...existing lines...
=======
...replacement lines...
>>>>>>> REPLACE

Replace or add ONE Python function or class in an existing file (everything else in the file is kept exactly):
DEF: relative/path.py::function_name
```python
def function_name(...):
    ...the complete new definition...
```
For a method write DEF: relative/path.py::ClassName.method_name and send just that method. One DEF block per function.

Paths are relative to the project root. Never touch files you have not been shown unless creating them."""

COMMON = ("Work like a careful senior engineer. Be specific and concrete. Do not invent facts about code you were not shown. "
          "Follow the requested output format exactly; no preamble, no apologies. When asked to change something, make the change: do not ask for permission or "
          "confirmation. Never repeat an action that just failed; if feedback says it failed, change the approach.")

ROLES = {
    "router": {
        "title": "Router", "icon": "R", "group": "Core",
        "blurb": "Reads the request and picks the workflow (design, build, bugfix, refactor) and the domain.",
        "needs": {"format": 55, "speed": 45}, "think": False, "sampling": {"temperature": 0.0},
        "uses": ["all"], "latency": "must be fast",
        "system": "You classify engineering requests. Answer ONLY a JSON object.",
    },
    "planner": {
        "title": "Planner", "icon": "P", "group": "Core",
        "blurb": "Turns a short request into a spec and a list of small, checkable steps with acceptance criteria.",
        "needs": {"reasoning": 45, "format": 35, "design_doc": 20}, "think": True, "sampling": {"temperature": 0.4},
        "uses": ["design", "build"], "latency": "can be slow",
        "system": "You are the planner. Stay high-level about deliverables and checks; do not prescribe fine technical detail (errors early in a plan cascade).",
    },
    "architect": {
        "title": "Architect", "icon": "A", "group": "Core",
        "blurb": "Writes the design: decision records, component diagram, API contract and data model.",
        "needs": {"design_doc": 35, "api_design": 25, "reasoning": 25, "sql": 15}, "think": True, "sampling": {"temperature": 0.4},
        "uses": ["design"], "latency": "can be slow",
        "system": "You are the software architect. Make decisions explicit, name alternatives you rejected and why, and design for failure, testing and change.",
    },
    "generator": {
        "title": "Backend coder", "icon": "B", "group": "Builders",
        "blurb": "Writes and edits server-side and library code (any language you ask for), with tests.",
        "needs": {"backend_feature": 35, "backend_fix": 30, "format": 25, "speed": 10}, "think": False, "sampling": {"temperature": 0.2},
        "uses": ["build", "bugfix", "refactor"], "latency": "medium",
        "system": "You are the implementer. Write complete, working code with tests; no stubs or TODO placeholders. Keep each change small.",
    },
    "generator_ui": {
        "title": "UI coder", "icon": "U", "group": "Builders",
        "blurb": "Writes front-end HTML/CSS/JS: accessible, responsive, clean.",
        "needs": {"ui": 65, "format": 25, "speed": 10}, "think": False, "sampling": {"temperature": 0.5},
        "uses": ["build"], "latency": "medium",
        "system": "You are the front-end implementer. Produce accessible (labels, focus styles, contrast), responsive, dependency-free UI unless told otherwise.",
    },
    "generator_sql": {
        "title": "Data / SQL coder", "icon": "D", "group": "Builders",
        "blurb": "Writes schemas, migrations and queries and checks them on fixture data.",
        "needs": {"sql": 60, "backend_feature": 20, "format": 20}, "think": False, "sampling": {"temperature": 0.1},
        "uses": ["build", "refactor"], "latency": "medium",
        "system": "You are the data engineer. Write portable, explicit SQL with constraints, keys and indexes where they matter; migrations must be re-runnable or versioned.",
    },
    "fullstack": {
        "title": "Full-stack engineer", "icon": "F", "group": "Builders",
        "blurb": "Builds one feature across data, API and UI together, contract first, so the layers agree.",
        "needs": {"backend_feature": 30, "ui": 25, "sql": 15, "api_design": 15, "format": 15}, "think": False, "sampling": {"temperature": 0.3},
        "uses": ["feature", "build"], "latency": "medium",
        "system": "You are a full-stack engineer. Work contract-first: settle the data shape and API contract, then implement the data layer, the API and the UI so they match that contract exactly. Keep each layer thin and tested.",
    },
    "tester": {
        "title": "Test engineer", "icon": "T", "group": "Quality",
        "blurb": "Writes tests that pin behaviour and catch regressions: edge cases, error paths, properties. Tests are judged by whether broken code makes them fail.",
        "needs": {"testing": 40, "backend_feature": 20, "reasoning": 20, "format": 20}, "think": False, "sampling": {"temperature": 0.3},
        "uses": ["test", "feature"], "latency": "medium",
        "system": "You are a test engineer. Write pytest tests that assert behaviour, not implementation: edge cases, boundaries, error paths, invalid input. Each test has one clear reason to fail. Never weaken or delete an existing assertion to make something pass.",
    },
    "optimizer": {
        "title": "Optimization engineer", "icon": "O", "group": "Quality",
        "blurb": "Measures first, then speeds things up: algorithms, queries, indexes, caching. A change counts only if tests stay green and the benchmark improves.",
        "needs": {"optimize": 40, "reasoning": 25, "backend_fix": 15, "sql": 10, "refactor": 10}, "think": True, "sampling": {"temperature": 0.2},
        "uses": ["optimize"], "latency": "can be slow",
        "system": "You are a performance engineer. Never guess: reason from the measurement you are given. Prefer the algorithmic or data-structure change over micro-tweaks, change one thing at a time, and keep behaviour identical.",
    },
    "bug_hunter": {
        "title": "Bug hunter", "icon": "H", "group": "Quality",
        "blurb": "Reads code adversarially to find real defects, then proves each one with a failing test. Unproven suspicions are discarded.",
        "needs": {"bughunt": 40, "reasoning": 30, "security": 15, "format": 15}, "think": True, "sampling": {"temperature": 0.4},
        "uses": ["hunt", "review"], "latency": "can be slow",
        "system": "You are a bug hunter. Look for what breaks: boundaries, empty and huge input, concurrency, resource leaks, wrong error handling, unvalidated input. Report only defects you can demonstrate with a concrete input and expected-versus-actual result; for each, write a small pytest test that fails today because of the bug.",
    },
    "security_engineer": {
        "title": "Security engineer", "icon": "S", "group": "Security",
        "blurb": "Hardens code and config: fixes the defects a static scan found (secrets, injection, weak crypto, bad defaults) without changing behaviour.",
        "needs": {"secreview": 45, "backend_fix": 20, "format": 20, "reasoning": 15}, "think": True, "sampling": {"temperature": 0.2},
        "uses": ["secure", "review"], "latency": "can be slow",
        "system": "You are a security engineer. Fix only the concrete findings you were given (file, line, CWE). Prefer the standard library fix (parameterized queries, constant-time compare, vetted crypto) over inventing your own. Never weaken a check to make it pass; never remove validation.",
    },
    "red_team": {
        "title": "Red teamer", "icon": "X", "group": "Security",
        "blurb": "Thinks like an attacker: chains findings into a realistic attack path and ranks it by real impact, strictly inside the declared scope.",
        "needs": {"attackpath": 45, "scope": 25, "secreview": 15, "reasoning": 15}, "think": True, "sampling": {"temperature": 0.4},
        "uses": ["pentest"], "latency": "can be slow",
        "system": "You are a red teamer working only against the scope and evidence you were given (no made-up hosts, creds or CVEs). For each attack path: preconditions, concrete steps, what it proves, and the business impact if exploited. Never suggest an action outside the declared scope, and never recommend testing a target without written authorization.",
    },
    "blue_team": {
        "title": "Blue teamer", "icon": "D", "group": "Security",
        "blurb": "Defensive side of the same findings: detections, monitoring rules and mitigations, written as the file the stack can actually load.",
        "needs": {"detection": 45, "secreview": 20, "configfix": 15, "format": 15}, "think": True, "sampling": {"temperature": 0.2},
        "uses": ["pentest", "secure"], "latency": "can be slow",
        "system": "You are a blue teamer. For each finding or attack path, write a concrete detection (log field, query or rule) and the lowest-risk mitigation. Prefer fixing root cause over adding an alert. State what you cannot detect with the evidence given, rather than guessing.",
    },
    "pentester": {
        "title": "Penetration tester", "icon": "T", "group": "Security",
        "blurb": "Runs the scoped recon/attack-path workflow against the sim lab, or a live target only inside a signed scope.json, and writes the findings up with evidence.",
        "needs": {"scope": 35, "attackpath": 30, "secreview": 15, "format": 20}, "think": True, "sampling": {"temperature": 0.2},
        "uses": ["pentest"], "latency": "can be slow",
        "system": "You are a penetration tester bound by scope.json. Cite the command/output that produced each finding; a claim with no evidence is dropped. Refuse and say so if asked to touch anything outside the authorized scope.",
    },
    "ioc_analyst": {
        "title": "IOC analyst", "icon": "I", "group": "Security",
        "blurb": "Matches indicators of compromise (file hashes, IPs, domains) against real file hashes and log lines -- an exact match or nothing, never a guess at intent.",
        "needs": {"detection": 35, "secreview": 25, "format": 25, "reasoning": 15}, "think": False, "sampling": {"temperature": 0.1},
        "uses": ["secure", "pentest"], "latency": "medium",
        "system": "You are an IOC analyst. Work only from the indicator list and the evidence (file hashes, log lines) you were given: an IOC either matches exactly (same hash, same IP/CIDR, same domain) or it doesn't. Never infer compromise from a near-match or from behaviour alone, and never invent an indicator that was not supplied. State plainly which indicators had no hits, rather than stretching a weak match to look like one.",
    },
    "network_engineer": {
        "title": "Network engineer", "icon": "N", "group": "Infra",
        "blurb": "Designs and hardens network layout: segmentation, firewall rules, CIDR plans, exposed ports — checked against the declared topology, not guessed.",
        "needs": {"netplan": 50, "configfix": 20, "reasoning": 15, "format": 15}, "think": True, "sampling": {"temperature": 0.2},
        "uses": ["network"], "latency": "can be slow",
        "system": "You are a network engineer. Default-deny, least privilege, name the blast radius of every opened port or rule. Work only from the topology/CIDR you were given; flag anything undefined instead of inventing addresses.",
    },
    "ml_engineer": {
        "title": "ML engineer", "icon": "M", "group": "Infra",
        "blurb": "Reviews and builds ML/data pipelines: leakage, reproducibility, eval methodology, serving safety — judged by a runnable check, not vibes.",
        "needs": {"mlaudit": 45, "backend_feature": 25, "reasoning": 15, "format": 15}, "think": True, "sampling": {"temperature": 0.3},
        "uses": ["ml", "build"], "latency": "can be slow",
        "system": "You are an ML engineer. Watch for train/test leakage, unset seeds, silent NaN handling, metrics that don't match the task, and unsafe deserialization of models/pickles. State assumptions about data you were not shown instead of inventing numbers.",
    },
    "config_engineer": {
        "title": "Config engineer", "icon": "C", "group": "Infra",
        "blurb": "Fixes infra config (Dockerfile, compose, CI yaml, env/toml) against the concrete scan findings: no root containers, no latent secrets, pinned versions.",
        "needs": {"configfix": 50, "secreview": 20, "format": 15, "reasoning": 15}, "think": False, "sampling": {"temperature": 0.1},
        "uses": ["config", "secure"], "latency": "medium",
        "system": "You are a config/DevOps engineer. Fix only the findings you were given (pin versions, drop root, remove secrets to env/secret store, add healthchecks) without changing what the service does. State the operational trade-off of any change that affects behaviour.",
    },
    "ci_engineer": {
        "title": "CI/CD engineer", "icon": "I", "group": "Infra",
        "blurb": "Writes and repairs build, test and release pipelines (GitHub Actions, GitLab CI) so they run what the repository really has: pinned actions, least-privilege permissions, timeouts, caches that exist, tests that can actually fail.",
        "needs": {"configfix": 45, "format": 20, "secreview": 15, "reasoning": 20}, "think": False, "sampling": {"temperature": 0.1},
        "uses": ["cicd"], "latency": "medium",
        "system": "You are a CI/CD engineer. A pipeline must run the commands this repository really has, fail when they fail, and give each job the least privilege it needs. Pin actions (first-party to a major version, third-party to a commit SHA when you know it), set permissions and timeouts, cache only what exists. NEVER make a pipeline green by deleting a test step, adding `|| true`, `continue-on-error`, or echoing success. Never put a secret in the file; use repository secrets. Pass untrusted event text through `env:`, never straight into `run:`. Change only what the findings and the request ask for.",
    },
    "protocol_engineer": {
        "title": "Protocol engineer", "icon": "W", "group": "Infra",
        "blurb": "Writes wire-level network code: framing, length-prefixing, checksums, partial reads/writes, backoff -- judged by round-tripping real bytes over a real socket, not by reading well.",
        "needs": {"protocol": 45, "backend_feature": 20, "reasoning": 20, "format": 15}, "think": True, "sampling": {"temperature": 0.2},
        "uses": ["network", "build"], "latency": "medium",
        "system": "You are a network protocol engineer. A socket read/write is never guaranteed to match one message: handle partial frames, multiple frames in one buffer, and a length prefix that lies before trusting it (bound it). Validate every byte from a peer as hostile input. State the failure mode of any framing choice you make (what a corrupt or truncated stream does to it) instead of assuming the happy path.",
    },
    "iot_dev": {
        "title": "IoT / embedded engineer", "icon": "G", "group": "Infra",
        "blurb": "Device-fleet and embedded-adjacent code: OTA update safety (version + checksum gates), sensor-reading staleness/bounds checks, simple explicit state machines -- judged by real version/hash and timing edge cases, not by style.",
        "needs": {"iot": 45, "backend_feature": 20, "reasoning": 20, "format": 15}, "think": True, "sampling": {"temperature": 0.2},
        "uses": ["network", "build"], "latency": "medium",
        "system": "You are an embedded/IoT engineer. Assume intermittent connectivity, limited memory and a device that cannot be easily reflashed if bricked: never accept an OTA update without checking both its version is strictly newer and its checksum matches, never trust a single sensor reading without checking it is fresh and physically plausible, and prefer a small explicit state machine over a clever abstraction. Say what a device should do when the network simply isn't there.",
    },
    "evaluator": {
        "title": "Evaluator", "icon": "E", "group": "Core",
        "blurb": "Judges the result against the agreed criteria, strictly. Only used where code checks cannot decide.",
        "needs": {"reasoning": 40, "design_doc": 20, "format": 40}, "think": True, "sampling": {"temperature": 0.1},
        "uses": ["design", "build"], "latency": "can be slow",
        "system": "You are a strict, skeptical reviewer. Models tend to praise their own work: do not. Fail anything that is stubbed, unverified or unsupported by evidence you were shown. Cite the evidence for each score.",
    },
    "reviewer": {
        "title": "Reviewer", "icon": "V", "group": "Core",
        "blurb": "Reads a finished diff for correctness risks, edge cases and maintainability; advice only.",
        "needs": {"reasoning": 40, "backend_fix": 30, "format": 30}, "think": True, "sampling": {"temperature": 0.2},
        "uses": ["bugfix", "refactor"], "latency": "can be slow",
        "system": "You review diffs. Report concrete risks with file and line context, ordered by severity. No style nitpicks unless they hide a bug.",
    },
    "architect_dev": {
        "title": "Architect developer", "icon": "A", "group": "Quality",
        "blurb": "Works on Architect itself: small changes that follow its conventions, each one tested, on a copy that you approve.",
        "needs": {"reasoning": 40, "format": 30, "design_doc": 30}, "think": False, "sampling": {"temperature": 0.2},
        "uses": ["build", "bugfix", "refactor"], "latency": "fast",
        "system": "You are improving Architect, a local, offline agentic coding app (Python standard library only; a single-file web hub). "
                  "Follow its conventions exactly: plain words in anything a person reads; no new dependency and no network on the "
                  "default path; every setting has a schema entry and an undo; every change comes with a selftest that proves it; "
                  "keep each change small and touch as few files as possible; never remove a check or a test to make something pass. "
                  "You work on a copy: the owner reviews and promotes.",
    },
    "docs_writer": {
        "title": "Documentation writer", "icon": "D", "group": "Quality",
        "blurb": "Writes docstrings and READMEs from what the code actually does, not what its names suggest.",
        "needs": {"design_doc": 45, "reasoning": 30, "format": 25}, "think": False, "sampling": {"temperature": 0.2},
        "uses": ["document"], "latency": "fast",
        "system": "You write documentation from the code itself. Say what a function does, what it returns, and what it raises, in plain sentences. "
                  "Never describe behaviour you did not see in the code. Do not change any behaviour while documenting it.",
    },
    "reliability_engineer": {
        "title": "Reliability engineer", "icon": "H", "group": "Quality",
        "blurb": "Hardens code: narrow exception handling, validation at the boundary, timeouts, no swallowed errors.",
        "needs": {"backend_fix": 45, "security": 25, "reasoning": 30}, "think": False, "sampling": {"temperature": 0.15},
        "uses": ["harden"], "latency": "fast",
        "system": "You make code fail safely and loudly. Catch the narrowest exception that can actually happen, never silence an error, validate "
                  "input where it enters, give every network call a timeout. Behaviour on valid input must not change, and the tests must still pass.",
    },
    "accessibility_engineer": {
        "title": "Accessibility engineer", "icon": "A", "group": "Builders",
        "blurb": "Makes pages usable with a keyboard and a screen reader: labels, alt text, focus, landmarks.",
        "needs": {"ui": 60, "format": 20, "reasoning": 20}, "think": False, "sampling": {"temperature": 0.2},
        "uses": ["a11y"], "latency": "fast",
        "system": "You fix accessibility in HTML. Every image gets alt text (empty for decoration), every field a label, every control a name a "
                  "screen reader can say, headings in order, the page a language and a title. Use real buttons, not clickable divs. Do not redesign.",
    },
    "migration_engineer": {
        "title": "Migration engineer", "icon": "M", "group": "Builders",
        "blurb": "Moves code off an old API or library onto a new one, call site by call site, with the tests green.",
        "needs": {"refactor": 50, "backend_fix": 30, "reasoning": 20}, "think": False, "sampling": {"temperature": 0.15},
        "uses": ["migrate"], "latency": "fast",
        "system": "You migrate code from an old API to a new one. Change every call site, keep behaviour identical, and keep the tests passing. "
                  "If the new API behaves differently in an edge case, preserve the old behaviour explicitly and say so.",
    },
    "type_engineer": {
        "title": "Type engineer", "icon": "T", "group": "Quality",
        "blurb": "Adds precise type hints to public functions without changing what they do.",
        "needs": {"refactor": 40, "backend_fix": 30, "format": 30}, "think": False, "sampling": {"temperature": 0.1},
        "uses": ["types"], "latency": "fast",
        "system": "You add type hints. Annotate every parameter and the return value of public functions with the narrowest honest type; use "
                  "Optional only where None really occurs. Never change runtime behaviour, and keep the code working on Python 3.10.",
    },
    "dependency_engineer": {
        "title": "Dependency engineer", "icon": "P", "group": "Infra",
        "blurb": "Audits what a project imports and installs: missing, unused, unpinned, or risky dependencies.",
        "needs": {"configfix": 45, "security": 30, "reasoning": 25}, "think": False, "sampling": {"temperature": 0.1},
        "uses": ["deps"], "latency": "fast",
        "system": "You audit dependencies. Report imports that are not installed, packages that are declared but never imported, versions that are "
                  "not pinned, and anything known to be risky. Prefer the standard library. Never add a dependency to fix a problem you can avoid.",
    },
}

# Which model TIER a role runs on when no model is assigned to it directly (Settings > Tiers fills the tiers; see arch_server / profiles).
# router: the tiny classifier. worker: every coding role. Thinking roles stay on the worker and escalate to heavy through the cascade.
TIER_OF = {"router": "router"}
for _r in ROLES:
    ROLES[_r].setdefault("tier", TIER_OF.get(_r, "worker"))

# Fallback chain when a role has no model assigned.
FALLBACK = {"architect_dev": ["generator"], "docs_writer": ["reviewer", "generator"], "reliability_engineer": ["generator"], "accessibility_engineer": ["generator_ui", "generator"],
            "migration_engineer": ["generator"], "type_engineer": ["generator"], "dependency_engineer": ["config_engineer", "reviewer", "generator"],
            "router": ["planner", "generator"], "planner": ["generator"], "architect": ["planner", "generator"],
            "generator_ui": ["generator"], "generator_sql": ["generator"], "evaluator": ["planner", "generator"],
            "reviewer": ["evaluator", "planner", "generator"], "generator": [],
            "fullstack": ["generator"], "tester": ["generator"], "optimizer": ["reviewer", "evaluator", "generator"],
            "bug_hunter": ["reviewer", "evaluator", "generator"],
            "security_engineer": ["reviewer", "generator"], "red_team": ["security_engineer", "bug_hunter", "evaluator"],
            "blue_team": ["security_engineer", "reviewer"], "pentester": ["red_team", "security_engineer"],
            "ioc_analyst": ["blue_team", "security_engineer"],
            "network_engineer": ["security_engineer", "generator"], "ml_engineer": ["generator", "reviewer"],
            "config_engineer": ["security_engineer", "generator"], "ci_engineer": ["config_engineer", "generator"],
            "protocol_engineer": ["network_engineer", "generator"], "iot_dev": ["protocol_engineer", "network_engineer", "generator"]}


def system_prompt(role, extra=""):
    r = ROLES[role]
    edit = "\n\n" + EDIT_PROTOCOL if (role.startswith("generator") or role in
                                      ("fullstack", "tester", "optimizer", "bug_hunter",
                                       "security_engineer", "network_engineer", "ml_engineer", "config_engineer", "ci_engineer", "blue_team", "ioc_analyst",
                                       "protocol_engineer", "iot_dev", "docs_writer", "reliability_engineer", "accessibility_engineer",
                                       "migration_engineer", "type_engineer")) else ""
    return f"{COMMON}\n\n{r['system']}{edit}{extra}\n\n[role:{role}]"


def edit_style_note(style):
    """Extra instruction for roles that write files, set by the harness profile (behavior.edit_style).
    patch -> nothing extra; whole/auto -> prefer complete-file blocks, which small models apply far more reliably than search/replace."""
    keep = (" To change one function or method of an existing Python file, send a DEF: block with its complete new definition: "
            "nothing else in the file can be lost that way.")
    if style == "whole":
        return "Edit style: for a new file, or a non-Python file under about 200 lines, write the COMPLETE file with a FILE: block instead of EDIT: blocks." + keep
    if style == "auto":
        return ("Edit style: for a new file, or a non-Python file under about 80 lines, write the COMPLETE file with a FILE: block; "
                "use EDIT: blocks for larger non-Python files." + keep)
    return ""


def params_b(name_or_text):
    m = re.search(r"(\d+(?:\.\d+)?)\s*[bB]\b", name_or_text or "")
    return float(m.group(1)) if m else None


def _prior(role, model):
    """A labelled GUESS for a model with no measurements, from its family and size. Low confidence."""
    kind = (model.get("know") or {}).get("kind", "general")
    base = {"coder": {"generator": 62, "generator_ui": 52, "generator_sql": 58, "planner": 46, "architect": 46, "evaluator": 46, "reviewer": 52, "router": 55, "fullstack": 56, "tester": 58, "optimizer": 50, "bug_hunter": 50,
                       "security_engineer": 54, "red_team": 46, "blue_team": 50, "pentester": 46, "ioc_analyst": 52, "network_engineer": 50, "ml_engineer": 50, "config_engineer": 56, "ci_engineer": 56, "protocol_engineer": 54, "iot_dev": 52},
            "reasoning": {"generator": 52, "generator_ui": 48, "generator_sql": 50, "planner": 64, "architect": 64, "evaluator": 64, "reviewer": 62, "router": 40, "fullstack": 50, "tester": 52, "optimizer": 62, "bug_hunter": 64,
                          "security_engineer": 58, "red_team": 62, "blue_team": 58, "pentester": 58, "ioc_analyst": 54, "network_engineer": 56, "ml_engineer": 58, "config_engineer": 50, "ci_engineer": 50, "protocol_engineer": 56, "iot_dev": 54},
            "general": {"generator": 50, "generator_ui": 52, "generator_sql": 48, "planner": 55, "architect": 55, "evaluator": 52, "reviewer": 50, "router": 55, "fullstack": 50, "tester": 50, "optimizer": 50, "bug_hunter": 50,
                        "security_engineer": 48, "red_team": 46, "blue_team": 46, "pentester": 44, "ioc_analyst": 46, "network_engineer": 48, "ml_engineer": 46, "config_engineer": 50, "ci_engineer": 50, "protocol_engineer": 48, "iot_dev": 46}}[kind if kind in ("coder", "reasoning") else "general"].get(role, 52 if kind == "coder" else 50)
    p = params_b(model.get("params") or model["name"]) or 7
    return base + max(-8, min(10, 4 * math.log2(max(p, 0.5) / 7)))


def score_for_role(role, model, weights=None):
    """-> (score 0-100, basis, detail). Measured when at least half of the role's weight has scores."""
    needs = weights or ROLES[role]["needs"]
    agg = model.get("agg") or {}
    have = {c: w for c, w in needs.items() if c in agg}
    cover = sum(have.values()) / (sum(needs.values()) or 1)
    detail = {c: round(agg[c]["score"]) for c in have}
    if cover >= 0.5:
        s = sum(agg[c]["score"] * w for c, w in have.items()) / sum(have.values())
        basis = "measured" if cover >= 0.99 else "partly measured"
    else:
        s, basis = _prior(role, model), "guess"
    verdict = (model.get("rec") or {}).get("verdict")
    note = []
    if verdict == "too_big":
        s, note = 0, ["does not fit this PC"]
    elif verdict == "slow":
        s -= 15 if ROLES[role]["latency"] == "must be fast" else 6
        note.append("mostly runs from RAM, slow")
    elif verdict == "split":
        s -= 8 if ROLES[role]["latency"] == "must be fast" else 2
        note.append("splits GPU and RAM")
    return round(max(s, 0), 1), basis, {"cats": detail, "cover": round(cover, 2), "notes": note}


def recommend(models, current=None, tolerance=5.0):
    """models: hub model rows (name, embed, agg, rec, know, params). -> {role: [ranked...]}, plan."""
    usable = [m for m in models if not m.get("embed")]
    out = {}
    for role in ROLES:
        ranked = []
        for m in usable:
            s, basis, d = score_for_role(role, m)
            ranked.append({"model": m["name"], "score": s, "basis": basis, **d})
        ranked.sort(key=lambda r: -r["score"])
        out[role] = ranked
    # assignment that avoids needless model swaps: reuse an already-chosen model if it is within `tolerance`
    chosen, assign = [], {}
    order = sorted(ROLES, key=lambda r: -(out[r][0]["score"] if out[r] else 0))
    for role in order:
        if not out[role]:
            continue
        best = out[role][0]
        pick = best
        for c in chosen:
            alt = next((x for x in out[role] if x["model"] == c), None)
            if alt and alt["score"] >= best["score"] - tolerance and alt["score"] > 0:
                pick = alt
                break
        assign[role] = pick["model"]
        if pick["model"] not in chosen:
            chosen.append(pick["model"])
    return out, {"assignment": assign, "distinct_models": len(chosen), "models": chosen}
