"""LocalIntel layer for the Architect.                                                   [localintel-bridge]

The Architect (arch_*.py, architect.py) is a generic harness. This module makes it know LocalIntel without
editing any harness file except one tagged import block at the end of arch_flows.py (apply_localintel_layer.py
adds or removes it, so a newer harness drops in cleanly).

Everything here switches on only for a project that IS LocalIntel (run.py + localintel/areas.py + localintel/
__init__.py exist). For any other app in the portfolio the wrappers pass straight through.

What it adds for LocalIntel
  * rules     the project's AGENT_NOTES.md and the house rules are added to every role's prompt
  * context   files too large to show whole (index.html is >1 MB) are offered as numbered slices that match the
              goal, so a small model can read and EDIT them with exact SEARCH text
  * guards    files the model may never write (protected_files) and files that always need a person's yes, even
              in --review auto (confirm_files): the approval-gate logic and the safety-net tests
  * checks    code checks, not opinion: no stubs/TODOs/placeholder behaviour added, no approval gate weakened,
              no NEW problems in index.html (its existing ones are tolerated), inline JavaScript parses,
              the page sweep loads every page without errors
  * flow      "checkup": no model; runs the tests, selftest rules, stub scan and page sweep and records what is wrong
  * CLI       status / runs / run-detail / set-roles / checkup-data, JSON on stdout, used by the app's
              Self-development page:   python localintel_arch.py status --root <project>
"""
import argparse
import ast
import collections
import functools
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import arch_checks as ck
import arch_core as core
import arch_flows as fl
import arch_roles

LAYER_VERSION = "1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
MARKERS = ("run.py", os.path.join("localintel", "areas.py"), os.path.join("localintel", "__init__.py"))

DEFAULT_PROTECTED = ["tests/test_invariants.py", "pytest.ini"]
DEFAULT_CONFIRM = ["localintel/areas.py", "localintel/profiles.py", "tests/selftest.py", "AGENT_NOTES.md", "launch.py"]
CODE_EXT = (".py", ".js", ".mjs", ".html", ".htm", ".css", ".sql")
BIG_BYTES = 40000                      # files above this are shown as slices, never whole
NODE_CHECK_MAX = 12                    # inline scripts parsed per file

_LI_CACHE = {}


def is_li(root):
    r = os.path.abspath(root or "")
    if r not in _LI_CACHE:
        _LI_CACHE[r] = all(os.path.isfile(os.path.join(r, m)) for m in MARKERS)
    return _LI_CACHE[r]


def li_opts(cfg):
    o = dict(slice_chars=8000, ui_sweep_on_html_change=True, big_bytes=BIG_BYTES)
    o.update((cfg or {}).get("localintel") or {})
    return o


def _files_cfg(cfg, key, default):
    v = (cfg.get("files") or {}).get(key)
    return [str(x).replace("\\", "/") for x in (v if isinstance(v, list) else default)]


def protected_files(cfg):
    return _files_cfg(cfg, "protected_files", DEFAULT_PROTECTED)


def confirm_files(cfg):
    return _files_cfg(cfg, "confirm_files", DEFAULT_CONFIRM)


# ---------------------------------------------------------------------------------------------------------
# rules for every role
# ---------------------------------------------------------------------------------------------------------
HOUSE_RULES = """HOW TO WORK IN LOCALINTEL
- Big files (localintel/static/index.html is over 1 MB, server.py, run.py) are shown only as SLICES. Change them with EDIT blocks whose SEARCH text you copy exactly from a slice and which occurs once. Never send FILE: for a big file.
- Every new /api route needs a row in localintel/areas.py (method, pattern, area, kind, label), and every new page needs its tab added to the workspace lists in index.html and to AREAS members.
- No stubs: no TODO, NotImplementedError, empty bodies, 'coming soon' or invented data. A feature works against real code or it is not added.
- Approval gates stay as they are. Never turn a gate off or call an approval for the user.
- Tests go under tests/ and run offline (loopback only) with pytest."""


UI_ANATOMY = """HOW A PAGE IS WIRED IN localintel/static/index.html (copy the pattern of an existing page such as 'workshop'):
1. <section id="NAME"> in the page body.   2. a hidden nav button <button data-t="NAME"> in the nav .legacy list.
3. [ 'NAME','Label' ] in the members of a workspace in the WSP array (and 'NAME' in that area's members in localintel/areas.py AREAS).
4. an init function called from the tab-switch map ( ... workshop:()=>workshopInit() ... ).   5. a one-line description in the help map next to it.
6. ICONS.NAME = '<svg path>' if it needs an icon.   Sub-pages: subtabs('NAME',[{k,label}]) plus PANEHOOK['NAME:k'].
Use the existing helpers: api(), post(), esc(), $(), toast(), drawer(), go(), and the CSS variables --bg --card --fg --mut --line --acc --warn --err --ok."""
_UI_WORDS = re.compile(r"(?i)\b(ui|ux|page|pages|tab|panel|screen|button|html|css|popover|drawer|layout|workspace|nav|index\.html)\b")

# ---------------------------------------------------------------------------------------------------------
# personas: what the building agent is working AS (red / blue / network), each with real tasks for this app
# ---------------------------------------------------------------------------------------------------------
PERSONAS = {
    "red": {
        "label": "Red team", "short": "Adversary emulation for the exercise range",
        "stance": ("You are building the app's RED side: adversary emulation for an authorised exercise. Think like an operator planning a "
                   "realistic campaign (ATT&CK techniques, plans, plays, attack paths) but build only what the app already allows: "
                   "plans and plays are data and simulation on the Range; live actions are TCP connect + banner only, inside the active scope, "
                   "STAGED for a person to approve. Never write exploit code, payloads, credential attacks, evasion or persistence tooling."),
        "files": ["localintel/redteam.py", "localintel/attack.py", "localintel/authoring.py", "localintel/range_library.py", "localintel/range_engine.py", "localintel/probes.py", "localintel/probelib.py"],
        "words": "emulation plan technique attack path red console range probe play adversary group",
        "done": "Each new technique, plan or play has an offline test, names its ATT&CK id, and nothing it adds sends traffic without going through a staged action.",
        "tasks": [
            {"id": "red-technique", "label": "Add a technique to the range library", "flow": "build",
             "goal": "Add one realistic ATT&CK technique (pick one the Range library does not have yet) to localintel/range_library.py with its ID, tactic, a safe simulated step sequence and the telemetry it should produce, then add a pytest under tests/ that loads it through the library and checks the fields."},
            {"id": "red-play", "label": "Write a purple-team play", "flow": "build",
             "goal": "Using localintel/authoring.py, add a purple-team play template for one technique: the red step (simulation only), the expected blue detection, and the evidence to record. Add a pytest that generates it and checks the fields."},
            {"id": "red-gaps", "label": "Find gaps in the Red console", "flow": "review",
             "goal": "Read the Red console flow (plans, approval, outcome, staging). List concrete gaps where an operator would be blocked or a staged action is unclear, with the exact files and functions, ranked by value. Do not change code yet."},
            {"id": "red-bug", "label": "Fix a bug in plans or probes", "flow": "bugfix",
             "goal": "Fix this bug in the emulation plans or service probes: DESCRIBE THE SYMPTOM HERE (route, input, wrong output). Write a failing pytest first."},
        {"id": "red-pentest", "label": "Write up this engagement", "flow": "pentest", "needs": "pentest",
         "goal": "Turn the findings LocalIntel has recorded for the current engagement into attack paths and the defences that would stop each one. Use only the findings and scope prepared from the app."},
        {"id": "red-chain", "label": "Chain techniques into an attack path", "flow": "build",
         "goal": "Extend the attack-path logic (localintel/attack.py or range_engine.py) so a plan can chain two or more techniques where the first one's output enables the second, showing the prerequisite and the telemetry each step should leave. Simulation only; add a pytest with a three-step chain and a chain that is broken because a prerequisite is missing."},
        ]},
    "blue": {
        "label": "Blue team", "short": "Detection, triage and coverage",
        "stance": ("You are building the app's BLUE side: detection engineering, triage and coverage. A detection is only real if it fires on a recorded "
                   "sample and stays quiet on a benign one, so every rule or parser you add comes with both in a test. Prefer deterministic logic over "
                   "model output; models only suggest. Never claim coverage that the coverage ledger cannot show."),
        "files": ["localintel/telemetry.py", "localintel/envrules.py", "localintel/events.py", "localintel/proposals.py", "localintel/coverage.py", "localintel/goldens.py", "localintel/datasets.py"],
        "words": "sigma detection rule telemetry hit coverage ledger event proposal golden triage verdict dataset",
        "done": "Every new rule or parser has a positive and a negative sample in its test, and the coverage ledger numbers still add up.",
        "tasks": [
            {"id": "blue-rule", "label": "Add a detection rule with samples", "flow": "build",
             "goal": "Add one detection for a technique the coverage ledger shows uncovered. Include a recorded positive sample and a benign negative sample, register them so the golden tests pick them up, and add a pytest asserting it fires on one and not the other."},
            {"id": "blue-parser", "label": "Support another log format", "flow": "build",
             "goal": "Add a parser in localintel/telemetry.py for one more common log format (for example Apache access, Windows EVTX-as-JSON or Suricata eve.json), with a sample file in the existing sample set and a pytest that loads it and checks field mapping and malformed-line handling."},
            {"id": "blue-triage", "label": "Improve alert triage", "flow": "review",
             "goal": "Read how telemetry hits become events and proposals (telemetry.py, events.py, proposals.py). Propose the smallest changes that cut noise for an analyst: dedup, severity, grouping. List files and functions. Do not change code yet."},
            {"id": "blue-bug", "label": "Fix a detection or coverage bug", "flow": "bugfix",
             "goal": "Fix this bug in detection, hits or the coverage ledger: DESCRIBE THE SYMPTOM HERE. Write a failing pytest first using a recorded sample."},
        {"id": "blue-gap", "label": "Close a coverage gap", "flow": "build",
         "goal": "Read the coverage ledger for techniques that are exercised but have no validated rule, pick the one with the most recorded evidence, and add the detection plus the positive and negative samples that move it to validated. Add a pytest that reads the ledger and asserts the technique's state changed."},
        ]},
    "network": {
        "label": "Networking", "short": "Inventory, scope, scans and drift",
        "stance": ("You are building the app's NETWORK side: inventory, scope matching, scan results, device classification and drift. Scope is containment, "
                   "never expansion: a target outside the active scope is skipped, and huge scopes (0.0.0.0/0, ::/0) must cost nothing until a host is tested. "
                   "Handle IPv4 and IPv6, ranges, CIDRs and wildcard names, and treat every import as untrusted input that may be malformed."),
        "files": ["localintel/netscan.py", "localintel/netdrift.py", "localintel/devices.py", "localintel/dev.py", "localintel/profiles.py"],
        "words": "scope cidr ipv6 inventory scan banner port device drift import nmap csv route trace host service",
        "done": "New parsing or matching code has tests for IPv4, IPv6, malformed input and out-of-scope targets, and runs on loopback only.",
        "tasks": [
            {"id": "net-import", "label": "Add a scanner-output importer", "flow": "build",
             "goal": "Add an importer for one more scanner output format (for example masscan JSON or Nessus-style CSV) beside the nmap XML and CSV importers, feeding the same inventory. Add pytests for good input, empty input and malformed input."},
            {"id": "net-classify", "label": "Improve device classification", "flow": "build",
             "goal": "In localintel/devices.py add classification for one more common device family from open ports and banners (for example printers, cameras, NAS, hypervisors). Add a pytest with banners that must and must not match."},
            {"id": "net-drift", "label": "Review drift detection", "flow": "review",
             "goal": "Read localintel/netdrift.py and how drift is scored. List cases where a real change would be missed or a harmless change would be flagged, with exact functions, ranked. Do not change code yet."},
            {"id": "net-bug", "label": "Fix a scope or scan bug", "flow": "bugfix",
             "goal": "Fix this bug in scope matching, scans or the inventory: DESCRIBE THE SYMPTOM HERE. Write a failing pytest first using loopback only."},
        {"id": "net-ipv6", "label": "Harden IPv6 and range handling", "flow": "refactor",
         "goal": "Audit scope matching, target expansion and the importers for IPv6 (zone ids, compressed forms, ::ffff: mapped addresses), ranges and /0 scopes. Add parametrised tests for every case that is wrong today and fix them with the smallest change."},
        ]},
}

PERSONAS["tester"] = {
    "label": "Tester", "short": "Tests, edge cases and the suite itself",
    "stance": ("You are the TESTER. You write tests that fail for the right reason before any fix, run offline (loopback only, no sleeps for timing, "
               "no network), exercise the real code instead of mocking the thing under test, and cover empty, huge, malformed and hostile input. "
               "Never weaken or delete an existing test or tests/test_invariants.py; a flaky test is a bug to remove, not to retry."),
    "files": ["tests/selftest.py", "tests/fake_ollama.py", "tests/test_areas.py", "tests/test_scope.py", "devtools/ui_sweep.py", "run.py"],
    "words": "test pytest fixture edge case malformed empty selftest invariant coverage flaky loopback",
    "done": "The new tests fail without the code they cover, pass with it, run in under a second each, and need no network.",
    "defaults": {"flow": "build", "review": "ask", "max_revisions": 3, "domains": "api"},
    "tasks": [
        {"id": "test-route", "label": "Cover an untested API route", "flow": "build",
         "goal": "Find an /api route in run.py or a module's routes() with no test in tests/ (grep the path), and add a pytest that calls it through starlette TestClient with a good request, a missing field, a wrong type and an oversized body, asserting status codes and error text."},
        {"id": "test-edge", "label": "Edge-case a parser", "flow": "build",
         "goal": "Pick one input parser (clean_targets, scope matching, an importer or a telemetry parser) and add parametrised tests for empty, whitespace, unicode, 10k-item, injection-shaped ($(x), ../, NUL) and IPv6 input. Report any case that fails as a bug, with the function name."},
        {"id": "test-flaky", "label": "Hunt flaky or slow tests", "flow": "review",
         "goal": "Read tests/ for sleeps, ordering assumptions, shared global state, real clocks and anything that touches the network. List each with file and line, the risk, and the smallest fix. Do not change code yet."},
        {"id": "test-mutation", "label": "Prove a test can fail", "flow": "test",
         "goal": "Choose one important rule in AGENT_NOTES.md (for example: a live scan is staged, not run) and add a test that would fail if a change broke it, by asserting on observable behaviour through the API, not on source text."},
        {"id": "test-bug", "label": "Reproduce a reported bug", "flow": "bugfix",
         "goal": "Reproduce this bug with the smallest failing pytest and then fix it: DESCRIBE THE SYMPTOM HERE."},
    ]}
PERSONAS["optimizer"] = {
    "label": "Optimizer", "short": "Speed, memory and hidden bugs",
    "stance": ("You are the OPTIMIZATION and BUG-HUNTING engineer. Measure first: find the slow path with a real number (timeit, query count, "
               "payload size), pin the current behaviour with a test, then change the least code that moves the number, and put before and after in your summary. "
               "Look for N+1 queries, missing indexes, work done on every request, unbounded lists, blocking calls in async handlers, and silent exception swallowing. "
               "Never trade correctness or an approval gate for speed."),
    "files": ["run.py", "localintel/support.py", "localintel/knowledge.py", "localintel/collect.py", "localintel/netscan.py", "localintel/telemetry.py"],
    "words": "slow performance query index cache loop async blocking memory bug exception limit timeout",
    "done": "A test pins the behaviour, the summary names the measured before/after, and no gate or invariant changed.",
    "defaults": {"flow": "refactor", "review": "ask", "max_revisions": 3, "domains": "api,data"},
    "tasks": [
        {"id": "opt-hotpath", "label": "Find and fix a slow endpoint", "flow": "optimize",
         "goal": "Time the 10 most used GET routes with a fresh database (starlette TestClient, timeit) and fix the slowest one that has an obvious cause (repeated queries, work on every call). First add a test pinning its response, then change it, then report before and after timings."},
        {"id": "opt-sql", "label": "Audit SQLite queries", "flow": "review",
         "goal": "Search the modules for SQL run in loops, SELECT * on big tables, missing indexes on columns used in WHERE or JOIN, and COUNT(*) on every request. List file, function and the exact index or rewrite proposed. Do not change code yet."},
        {"id": "opt-async", "label": "Find blocking calls in async handlers", "flow": "review",
         "goal": "List async route handlers in run.py that call blocking code (sockets, subprocess, long SQL, file reads) directly instead of via asyncio.to_thread, with file and line, ranked by how long they can block. Do not change code yet."},
        {"id": "opt-swallow", "label": "Remove silent exception swallowing", "flow": "refactor",
         "goal": "Find broad `except Exception: pass` blocks that hide real failures on a user-visible path, and make each report a clear error or log through the ledger, with a pytest forcing the failure. Leave deliberate best-effort cleanups alone and say why."},
        {"id": "opt-bug", "label": "Fix a performance or leak bug", "flow": "bugfix",
         "goal": "Fix this performance or resource bug: DESCRIBE THE SYMPTOM HERE (route or action, size of data, observed time or growth). Write a failing test with a time or size bound first."},
    ]}
PERSONAS["fullstack"] = {
    "label": "Full stack", "short": "A feature from route to page to test",
    "stance": ("You are the FULL STACK engineer. A feature is only done when every layer exists and connects: the module function, its route, its "
               "areas.py governance row, a selftest check, the page wiring in index.html (all six steps) and a test. Reuse the app's helpers and CSS "
               "variables; add nothing that is not reachable from the UI; no placeholder text or fake data."),
    "files": ["run.py", "localintel/areas.py", "localintel/workspace.py", "localintel/support.py", "localintel/static/index.html"],
    "words": "route handler areas governance page tab subtabs drawer api post workspace selftest feature",
    "done": "The feature can be used end to end from the browser, has an areas.py row, a selftest check and a test, and the page sweep stays clean.",
    "defaults": {"flow": "build", "review": "ask", "max_revisions": 4, "domains": "web,api,data"},
    "tasks": [
        {"id": "full-feature", "label": "Build a small feature end to end", "flow": "feature",
         "goal": "Add this feature end to end: DESCRIBE THE FEATURE HERE. Include the module function, the route, the areas.py row, a selftest check in run.py, the index.html wiring and a pytest."},
        {"id": "full-page", "label": "Add a page to a workspace", "flow": "build",
         "goal": "Add a new read-only page to an existing workspace that shows DESCRIBE THE DATA HERE from an existing API, wired with all six steps (section, nav button, WSP member and AREAS member, init map, help map, icon), with an empty state and a test for the API it reads."},
        {"id": "full-export", "label": "Add an export", "flow": "build",
         "goal": "Add a CSV or JSON export for one list the app already shows (hits, inventory, cases or events): the route (kind export, so it follows the export gate), the areas.py row, a Download button on that page, and a pytest checking the file's header and rows."},
        {"id": "full-tidy", "label": "Tidy one page's UI", "flow": "refactor",
         "goal": "Reduce clutter on DESCRIBE THE PAGE HERE: fold rarely used controls into a details element, remove duplicate buttons, keep every id the tests use, keep light and dark themes working. Run the page sweep."},
        {"id": "full-bug", "label": "Fix a bug across layers", "flow": "bugfix",
         "goal": "Fix this bug, wherever in the stack it lives: DESCRIBE THE SYMPTOM HERE. Write a failing test at the lowest layer that shows it first."},
    ]}
for _k, _d in {"red": {"flow": "build", "review": "ask", "max_revisions": 3, "domains": "api,data"}, "blue": {"flow": "build", "review": "ask", "max_revisions": 3, "domains": "api,data"},
               "network": {"flow": "build", "review": "ask", "max_revisions": 3, "domains": "api"}}.items():
    PERSONAS[_k]["defaults"] = _d

PERSONAS["security"] = {
    "label": "Security", "short": "Injection, secrets and hardening",
    "stance": ("You are the SECURITY engineer for this app. Assume every request field is hostile: SQL must use placeholders with values bound, never string-built "
               "(column and table names only from fixed lists); no shell with user text; paths stay inside their folder; markup from data is escaped before it reaches the page. "
               "Fix what the static scanner and the hostile-input tests prove, with a test that fails first, and never weaken a gate or loosen scope to make something pass."),
    "files": ["run.py", "localintel/support.py", "localintel/netscan.py", "localintel/collect.py", "localintel/authgate.py", "localintel/websearch.py", "docker-compose.yml"],
    "words": "injection sql parameter placeholder escape sanitize path traversal secret hash docker auth validate hostile input",
    "done": "The scanner finds nothing new, a test sends the hostile input and gets a 4xx or clean data back, and no gate or scope check was loosened.",
    "defaults": {"flow": "secure", "review": "ask", "max_revisions": 3, "domains": "api,data"},
    "tasks": [
        {"id": "sec-scan", "label": "Fix what the scanner found", "flow": "secure",
         "goal": "Run the security scan over localintel/*.py and run.py, take the findings that are NOT in .arch/security_baseline.json, and fix each real one (bind values, whitelist names, escape output). The same scanner must pass afterwards. Add a pytest for each fix that sends the hostile input."},
        {"id": "sec-fuzz", "label": "Harden a route against hostile input", "flow": "bugfix",
         "goal": "This route answers hostile input badly: DESCRIBE THE ROUTE AND PAYLOAD HERE. Write a failing pytest that sends it through the API first, then fix it so it returns a clear 4xx, never a 500 and never echoes SQL or a traceback."},
        {"id": "sec-config", "label": "Harden Docker and compose files", "flow": "config",
         "goal": "Harden docker-compose.yml and anything under deploy/: non-root user, pinned base images, no secrets, read-only filesystem where possible, loopback-only published ports. The config scanner must pass afterwards."},
        {"id": "sec-output", "label": "Escape data before it reaches the page", "flow": "review",
         "goal": "Find places in localintel/static/index.html where text from the API (names, titles, banners, document text) is put into innerHTML without esc(). List each with the function and line, and the exact fix. Do not change code yet."},
        {"id": "sec-input", "label": "Add input validation to a handler", "flow": "build",
         "goal": "Add strict validation (type, length, allowed characters, range) to this handler: DESCRIBE THE ROUTE HERE. Reject bad input with a 400 and a clear message, and add parametrised pytests for good, empty, oversized and injection-shaped values."},
    ]}
PERSONA_FLOWS = ("design", "build", "feature", "bugfix", "refactor", "test", "optimize", "hunt", "review", "secure", "config", "network", "ml", "pentest", "new", "auto")


def current_persona():
    pid = (os.environ.get("LOCALINTEL_PERSONA") or "").strip().lower()
    return PERSONAS.get(pid)


def persona_text(p):
    return (f"\n\nYOU ARE WORKING AS: {p['label']} - {p['short']}.\n{p['stance']}\nStart from these files when relevant: {', '.join(p['files'])}.\nDone means: {p['done']}")


def run_files(run_dir):
    """Every file this whole run has written so far: created ones plus those that have a backup (earlier steps count too)."""
    out = set()
    try:
        out |= {ln for ln in open(os.path.join(run_dir or "", "created.txt"), encoding="utf-8").read().split("\n") if ln}
    except OSError:
        pass
    base = os.path.join(run_dir or "", "backup")
    for d, _, fs in os.walk(base):
        for f in fs:
            out.add(os.path.relpath(os.path.join(d, f), base).replace(os.sep, "/"))
    return out


def check_persona(root, files, run_dir):
    """With a persona selected, a run that changes app code must also add or change a test (red/blue/network work is only real when a test shows it)."""
    p = current_persona()
    if not p or not is_li(root):
        return []
    code = [f for f in files if f.endswith(".py") and not _is_test(f) and f.startswith("localintel/") and run_changed(root, f, run_dir)]
    if not code:
        return []
    tests = [f for f in run_files(run_dir) | set(files) if _is_test(f) and f.endswith(".py") and os.path.isfile(os.path.join(root, f)) and run_changed(root, f, run_dir)]
    return [R(f"{p['label']}: the change comes with a test", bool(tests), "" if tests else f"changed {', '.join(code[:3])} but no test under tests/ was added or changed. {p['done']}")]


def rules_text(root, cfg):
    notes = ""
    try:
        notes = core.read_text(os.path.join(root, "AGENT_NOTES.md"), 3000)[0].strip()
    except OSError:
        pass
    return ("\n\nLOCALINTEL PROJECT NOTES (AGENT_NOTES.md):\n" + notes[:1300] if notes else "") + "\n\n" + HOUSE_RULES


# ---------------------------------------------------------------------------------------------------------
# slices of big files
# ---------------------------------------------------------------------------------------------------------
_START = re.compile(r"^(?:async\s+)?function\s+\w+|^<section\b|^</?(?:style|script)\b|^\s*(?:const|let|var)\s+\w+\s*=\s*(?:async\s*)?(?:\(|function)|^\s*@app\.|^\s*def\s+\w+|^\s*class\s+\w+|^\s*\w+\s*=\s*\[$")


def big_files(root, cfg):
    limit = int(li_opts(cfg)["big_bytes"])
    ignored = core.BUILTIN_IGNORED | set(cfg["files"].get("ignored_dirs") or [])
    exts = set(cfg["files"]["extensions"]) & set(CODE_EXT)
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in ignored and not x.startswith("._"))
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in exts:
                p = os.path.join(d, f)
                try:
                    if os.path.getsize(p) > limit:
                        out.append(os.path.relpath(p, root).replace(os.sep, "/"))
                except OSError:
                    pass
    return out


def _py_chunks(text):
    """Top-level functions, classes (big classes split per method) and the module-level gaps between them."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    n = text.count("\n") + 1
    covered = [False] * (n + 2)
    chunks = []

    def add(s_, e_, name):
        chunks.append((s_, e_, name))
        for i in range(s_, min(e_, n) + 1):
            covered[i] = True

    def span(node):
        return min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]), getattr(node, "end_lineno", node.lineno)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(*span(node), node.name)
        elif isinstance(node, ast.ClassDef):
            s_, e_ = span(node)
            kids = [k for k in node.body if isinstance(k, (ast.FunctionDef, ast.AsyncFunctionDef))]
            if e_ - s_ > 60 and kids:
                first = span(kids[0])[0]
                if first - 1 >= s_:
                    add(s_, first - 1, node.name + " (header)")
                for k in kids:
                    add(*span(k), f"{node.name}.{k.name}")
            else:
                add(s_, e_, node.name)
    gap = []
    for i in range(1, n + 1):
        if covered[i]:
            if gap:
                chunks.extend(_split_gap(gap))
                gap = []
        else:
            gap.append(i)
    if gap:
        chunks.extend(_split_gap(gap))
    return sorted(chunks)


def _split_gap(nums, step=45):
    out = []
    for i in range(0, len(nums), step):
        part = nums[i:i + step]
        out.append((part[0], part[-1], "module level"))
    return out


def _generic_chunks(text):
    lines = text.split("\n")
    chunks, start, size, label = [], 1, 0, "top"
    for i, ln in enumerate(lines, 1):
        m = _START.match(ln)
        if (m and size >= 1200) or size >= 3600:
            chunks.append((start, i - 1, label))
            start, size = i, 0
        if m and size == 0:
            label = ln.strip()[:60]
        size += len(ln) + 1
    chunks.append((start, len(lines), label))
    return [c for c in chunks if c[1] >= c[0]]


_INDEX = {}
MIN_CHUNK = 700


def _merge_small(chunks, lines):
    """Join neighbouring tiny chunks so every slice is something a model can actually edit from."""
    out = []
    for s_, e_, name in chunks:
        size = sum(len(x) + 1 for x in lines[s_ - 1:e_])
        if out and (out[-1][3] < MIN_CHUNK or size < MIN_CHUNK) and out[-1][3] + size < 3600:
            ps, pe, pn, psz = out[-1]
            out[-1] = (ps, e_, pn if pn.endswith("+") else pn + " +", psz + size)
        else:
            out.append((s_, e_, name, size))
    return [(s_, e_, n) for s_, e_, n, _ in out]


def index_file(root, rel):
    """lines + chunks with token counts, cached per process on (path, mtime, size)."""
    full = os.path.join(root, rel)
    try:
        st = os.stat(full)
    except OSError:
        return None
    key = (full, st.st_mtime_ns, st.st_size)
    hit = _INDEX.get(full)
    if hit and hit["key"] == key:
        return hit
    text, _ = core.read_text(full)
    lines = text.split("\n")
    ch = (_py_chunks(text) if rel.endswith(".py") else None) or _generic_chunks(text)
    docs = []
    for s_, e_, name in _merge_small(ch, lines):
        body = "\n".join(lines[s_ - 1:e_])
        tf = collections.Counter(core._terms(name * 3 + " " + body[:6000]))
        docs.append({"rel": rel, "s": s_, "e": e_, "name": name, "tf": tf, "n": sum(tf.values()) or 1})
    hit = {"key": key, "lines": lines, "docs": docs}
    _INDEX[full] = hit
    return hit


def chunks_of(root, rel):
    ix = index_file(root, rel)
    return ([], []) if not ix else (ix["lines"], [(d["s"], d["e"], d["name"]) for d in ix["docs"]])


def slices(root, files, query, cap):
    """The best-matching chunks of the given (big) files for a query, as one block of at most ~cap chars.
    BM25 over chunks (identifier-aware: 'popover' also finds 'setuppopover')."""
    if not files or cap < 600:
        return ""
    q = collections.Counter(core._terms(query))
    if not q:
        return ""
    idx = {rel: index_file(root, rel) for rel in files}
    docs = [d for ix in idx.values() if ix for d in ix["docs"]]
    if not docs:
        return ""
    vocab = set()
    for d in docs:
        vocab.update(d["tf"])
    expand = {}
    for w in q:
        terms = {w: 1.0}
        if len(w) >= 4:
            for v in vocab:
                if v != w and v.startswith(w):
                    terms[v] = 0.45
        expand[w] = terms
    df = collections.Counter()
    for d in docs:
        present = set()
        for w, terms in expand.items():
            if any(t in d["tf"] for t in terms):
                present.add(w)
        df.update(present)
    N = len(docs)
    avg = sum(d["n"] for d in docs) / N
    k1, b = 1.2, 0.75
    scored, need = [], min(2, len(q))
    for d in docs:
        sc, hit = 0.0, 0
        for w, terms in expand.items():
            f = sum(d["tf"].get(t, 0) * wt for t, wt in terms.items())
            if f:
                hit += 1
                idf = math.log(1 + (N - df[w] + 0.5) / (df[w] + 0.5))
                sc += idf * q[w] ** 0.5 * f * (k1 + 1) / (f + k1 * (1 - b + b * d["n"] / avg))
        if sc > 0 and hit >= need:
            scored.append((sc, d))
    scored.sort(key=lambda x: -x[0])
    if scored:
        scored = [x for x in scored if x[0] >= scored[0][0] * 0.5]
    out, used = [], 0
    for sc, d in scored:
        body = "\n".join(idx[d["rel"]]["lines"][d["s"] - 1:d["e"]])
        if len(body) > 3400:
            body = body[:3400] + "\n... (slice shortened)"
        block = f"=== {d['rel']}  (slice of a big file: lines {d['s']}-{d['e']}, {d['name']}) ===\n{body}"
        if used + len(block) > cap:
            if out:
                break
            continue
        out.append(block)
        used += len(block)
        if len(out) >= 6:
            break
    if not out:
        return ""
    return (f"BIG FILES ({', '.join(files[:6])}{', ...' if len(files) > 6 else ''}) are shown only as slices; edit them with EDIT blocks copied exactly from a slice.\n\n" + "\n\n".join(out))


# ---------------------------------------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------------------------------------
R = ck.R
STUB_PATTERNS = [
    (re.compile(r"raise\s+NotImplementedError"), "raises NotImplementedError"),
    (re.compile(r"\b(?:TODO|FIXME|XXX)\b"), "TODO/FIXME marker"),
    (re.compile(r"(?i)\bcoming soon\b|\bnot implemented\b|\blorem ipsum\b|\bwork in progress\b"), "placeholder wording"),
    (re.compile(r"(?i)(?:#|//|/\*|<!--)\s*(?:this is |just |a |for now:? )*(?:stub|stubbed)\b|\bdef\s+\w*stub\w*\s*\(|\bfunction\s+\w*stub\w*\s*\("), "stub"),
]
GATE_PATTERNS = [
    (re.compile(r"""["']?gate_(?:live|exports|inbound)["']?\s*[:=,]\s*(?:False|0|false)\b"""), "turns an approval gate off"),
    (re.compile(r"""["']approve["']\s*:\s*True|\bapprove\s*=\s*True"""), "approves an action in code"),
    (re.compile(r"""(?i)\bauto[_-]?approve"""), "auto-approval"),
]


# backup_text/run_changed/added_lines are pure functions of (root, rel, run_dir): the files involved are
# written once (by the run) and only read afterwards, by several independent checks (check_no_stubs,
# check_gates, check_js, check_big_html each call one or more of these for the SAME file). Without caching,
# one pass over a run's files re-reads the backup and the new copy of every changed file 4-5x each -- real
# cost on the >1 MB files (index.html) this layer exists to handle. lru_cache is safe here: results are
# deterministic for given arguments and a bounded maxsize keeps a long-lived hub process from growing without
# bound across many runs/files over time.
@functools.lru_cache(maxsize=512)
def backup_text(run_dir, rel):
    p = os.path.join(run_dir or "", "backup", rel)
    if os.path.isfile(p):
        return core.read_text(p)[0]
    return None


def created_in_run(run_dir, rel):
    p = os.path.join(run_dir or "", "created.txt")
    try:
        return rel in open(p, encoding="utf-8").read().split("\n")
    except OSError:
        return False


@functools.lru_cache(maxsize=512)
def run_changed(root, rel, run_dir):
    """True when this run wrote the file (it differs from its backup, or it was created by the run)."""
    old = backup_text(run_dir, rel)
    if old is None:
        return created_in_run(run_dir, rel)
    try:
        return old != core.read_text(os.path.join(root, rel))[0]
    except OSError:
        return False


@functools.lru_cache(maxsize=512)
def added_lines(root, rel, run_dir):
    """[(line_no, text)] text this run introduced: lines of the new file that the old file did not have (exact multiset
    difference, so it never mis-aligns on a 1 MB file). [] when the run did not change the file."""
    if not run_changed(root, rel, run_dir):
        return []
    try:
        new = core.read_text(os.path.join(root, rel))[0].split("\n")
    except OSError:
        return []
    old = backup_text(run_dir, rel)
    have = collections.Counter((old or "").split("\n"))
    out = []
    for i, ln in enumerate(new, 1):
        if have[ln] > 0:
            have[ln] -= 1
        else:
            out.append((i, ln))
    return out


def _is_test(rel):
    return rel.startswith("tests/") or "/tests/" in rel or os.path.basename(rel).startswith("test_")


def check_no_stubs(root, rel, run_dir):
    if _is_test(rel) or not rel.lower().endswith(CODE_EXT):
        return []
    add = added_lines(root, rel, run_dir)
    if not add:
        return []
    hits = []
    for no, text in add:
        for rx, why in STUB_PATTERNS:
            if rx.search(text):
                hits.append(f"line {no}: {why}: {text.strip()[:70]}")
                break
    if rel.endswith(".py"):
        try:
            tree = ast.parse(core.read_text(os.path.join(root, rel))[0])
            added = {n for n, _ in add}
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno in added and not node.name.startswith("__"):
                    body = [b for b in node.body if not (isinstance(b, ast.Expr) and isinstance(getattr(b, "value", None), ast.Constant) and isinstance(b.value.value, str))]
                    if not body or all(isinstance(b, ast.Pass) or (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant) and b.value.value is Ellipsis) for b in body):
                        hits.append(f"line {node.lineno}: function {node.name} has an empty body")
        except SyntaxError:
            pass
    return [R(f"{rel}: no stubs or placeholders added", not hits, "; ".join(hits[:4]))]


def check_gates(root, rel, run_dir):
    if _is_test(rel) or not rel.lower().endswith(CODE_EXT):
        return []
    add = added_lines(root, rel, run_dir)
    hits = []
    for no, text in add:
        for rx, why in GATE_PATTERNS:
            if rx.search(text):
                hits.append(f"line {no}: {why}: {text.strip()[:70]}")
                break
    return [R(f"{rel}: no approval gate weakened", not hits, "; ".join(hits[:3]) + (" . If this is really intended, make that edit by hand." if hits else ""))] if add else []


def _first_int(s):
    m = re.search(r"\d+", s or "")
    return int(m.group(0)) if m else None


def check_big_html(root, rel, run_dir):
    """index.html is a JavaScript-assembled page; the generic HTML check already fails it. Hold it to: nothing NEW."""
    try:
        new, _ = core.read_text(os.path.join(root, rel))
    except OSError:
        return []
    if not run_changed(root, rel, run_dir):
        return []
    old = backup_text(run_dir, rel)
    after = {r["name"].split(": ", 1)[-1]: r for r in ck.check_html(new, rel)}
    if old is None:
        return list(after.values())
    before = {r["name"].split(": ", 1)[-1]: r for r in ck.check_html(old, rel)}
    out = []
    for name, r in after.items():
        b = before.get(name)
        if r["ok"]:
            out.append(R(f"{rel}: {name}", True))
        elif b is None or b["ok"]:
            out.append(R(f"{rel}: {name}", False, "new problem: " + r["detail"]))
        else:
            n1, n0 = _first_int(r["detail"]), _first_int(b["detail"])
            worse = n1 is not None and n0 is not None and n1 > n0
            out.append(R(f"{rel}: {name} (existing problem, not made worse)", not worse, f"{n0} before, {n1} now" if worse else ""))
    return out


def inline_scripts(text):
    return [m.group(1) for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", text, re.S | re.I) if m.group(1).strip()]


def check_js(root, rel, run_dir):
    """Inline <script> blocks (and .js files) must parse. Uses node when installed; says so when it cannot."""
    full = os.path.join(root, rel)
    if not rel.lower().endswith((".html", ".htm", ".js", ".mjs")):
        return []
    if not run_changed(root, rel, run_dir):
        return []
    node = shutil.which("node")
    if not node:
        return [R(f"{rel}: JavaScript parses (skipped: node is not installed)", True)]
    text, _ = core.read_text(full)
    blocks = inline_scripts(text) if rel.lower().endswith((".html", ".htm")) else [text]
    bad = []
    with tempfile.TemporaryDirectory() as td:
        for i, b in enumerate(blocks[:NODE_CHECK_MAX]):
            p = os.path.join(td, f"s{i}.js")
            with open(p, "w", encoding="utf-8") as f:
                f.write(b)
            r = subprocess.run([node, "--check", p], capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                msg = (r.stderr.strip().splitlines() or ["syntax error"])
                bad.append(f"script {i + 1}: " + " | ".join(m.strip() for m in msg[:4])[:240])
    return [R(f"{rel}: JavaScript parses", not bad, "; ".join(bad[:2]))]


def sweep_available(root):
    return os.path.isfile(os.path.join(root, "devtools", "ui_sweep.py")) and core.has_module("playwright")


def run_sweep(root, timeout=240):
    """-> (ok|None, problems list, text). None = could not run."""
    if not sweep_available(root):
        return None, [], "page sweep skipped (playwright is not installed or devtools/ui_sweep.py is missing)"
    rc, out = core.run_cmd([sys.executable, os.path.join(root, "devtools", "ui_sweep.py"), "--json"], root, timeout, True)
    j = None
    for ln in reversed(out.splitlines()):
        if ln.startswith("{"):
            try:
                j = json.loads(ln)
                break
            except ValueError:
                continue
    if j is None:
        return None, [], "page sweep could not run: " + out.strip()[-300:]
    return (not j.get("problems")), j.get("problems", []), f"{j.get('pages', '?')} pages"


def check_sweep(root, files, cfg, run_dir):
    if not li_opts(cfg)["ui_sweep_on_html_change"]:
        return []
    if not any(f.endswith("static/index.html") and run_changed(root, f, run_dir) for f in files):
        return []
    ok, problems, text = run_sweep(root)
    if ok is None:
        return [R(f"every page loads without errors (skipped: {text})", True)]
    return [R("every page loads without errors", ok, "; ".join(problems[:4]))]


_orig_check_files = ck.check_files


def li_check_files(root, files, cfg, run_dir=None):
    if not is_li(root):
        return _orig_check_files(root, files, cfg, run_dir)
    lim = int(li_opts(cfg)["big_bytes"])
    generic, extra = [], []
    for f in files:
        full = os.path.join(root, f)
        big_html = f.lower().endswith((".html", ".htm")) and os.path.isfile(full) and os.path.getsize(full) > lim
        if not big_html:
            generic.append(f)
        else:
            extra += check_big_html(root, f, run_dir)
    out = _orig_check_files(root, generic, cfg, run_dir) + extra
    for f in files:
        if os.path.isfile(os.path.join(root, f)):
            out += check_no_stubs(root, f, run_dir) + check_gates(root, f, run_dir) + check_js(root, f, run_dir)
    out += check_sweep(root, files, cfg, run_dir)
    out += check_persona(root, files, run_dir)
    return out


# ---------------------------------------------------------------------------------------------------------
# engine hooks
# ---------------------------------------------------------------------------------------------------------
_orig_call = core.Engine.call
_orig_context = core.Engine.context
_orig_decide = core.Engine._decide
_orig_safe_path = core.safe_path


def li_call(self, role, user, extra_system="", *a, **k):
    if is_li(self.root):
        extra_system = (extra_system or "") + rules_text(self.root, self.cfg)
        if role == "generator_ui" or (role in ("generator", "planner", "architect") and _UI_WORDS.search(user[:3000])):
            extra_system += "\n\n" + UI_ANATOMY
        pr = current_persona()
        if pr:
            extra_system += persona_text(pr)
    return _orig_call(self, role, user, extra_system, *a, **k)


def li_context(self, query, budget=None, always=()):
    if not is_li(self.root):
        return _orig_context(self, query, budget, always)
    budget = budget or self.cfg["limits"]["context_chars"]
    big = big_files(self.root, self.cfg)
    bigset = set(big)
    focus = [a for a in always if a in bigset]
    pr = current_persona()
    sl = slices(self.root, big, query + " " + " ".join(focus) + (" " + pr["words"] if pr else ""), min(int(li_opts(self.cfg)["slice_chars"]), int(budget * 0.45)))
    base = _orig_context(self, query, max(2500, budget - len(sl)), [a for a in always if a not in bigset])
    return base + ("\n\n" + sl if sl else "")


def li_decide(self, rel):
    if is_li(self.root) and self.review != "dry" and rel in confirm_files(self.cfg):
        a = (self.ask(f"  GOVERNANCE FILE {rel} holds approval-gate or safety-test logic, so it always needs your yes.  Apply {rel}?  [y]es  [n]o  [q]uit run > ") or "").strip().lower()
        return "quit" if a in ("q", "quit") else ("yes" if a in ("y", "yes") else "no")
    return _orig_decide(self, rel)


def li_safe_path(root, rel, cfg):
    full = _orig_safe_path(root, rel, cfg)
    if is_li(root):
        r = os.path.relpath(full, os.path.abspath(root)).replace(os.sep, "/")
        if r in protected_files(cfg):
            raise ValueError(f"'{r}': this file guards the app's safety rules and is not edited by the Architect")
    return full


# ---------------------------------------------------------------------------------------------------------
# the checkup flow (no model)
# ---------------------------------------------------------------------------------------------------------
def scan_stubs(root, cfg):
    hits = []
    ignored = core.BUILTIN_IGNORED | set(cfg["files"].get("ignored_dirs") or []) | {"data", "docs"}
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in ignored and not x.startswith("._"))
        for f in sorted(files):
            rel = os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/")
            if not rel.lower().endswith(CODE_EXT) or _is_test(rel) or rel.startswith("devtools/"):
                continue
            try:
                text, _ = core.read_text(os.path.join(root, rel), 3_000_000)
            except OSError:
                continue
            for i, ln in enumerate(text.split("\n"), 1):
                if len(ln) > 2000:
                    continue
                for rx, why in STUB_PATTERNS:
                    if rx.search(ln) and not re.search(r"""(?i)placeholder\s*[=:]|re\.compile|STUB_PATTERNS""", ln):
                        hits.append(f"{rel}:{i}: {why}: {ln.strip()[:80]}")
                        break
    return hits


def parse_selftest(out):
    for ln in out.splitlines():
        if ln.startswith("@@SELFTEST_JSON@@"):
            try:
                return json.loads(ln[len("@@SELFTEST_JSON@@"):])
            except ValueError:
                return None
    return None

# ---------------------------------------------------------------------------------------------------------
# security scan of the app's own source (the harness scanner), with a reviewed baseline
# ---------------------------------------------------------------------------------------------------------
try:
    import arch_security as sec
except ImportError:             # an older harness without the scanner: the item is reported as skipped, never faked
    sec = None
BASELINE = os.path.join(".arch", "security_baseline.json")
SEC_SKIP_DIRS = {"tests", "devtools", "architect", "docs", "data", "static", "fonts", "node_modules"}


def sec_files(root, cfg):
    ignored = core.BUILTIN_IGNORED | set(cfg["files"].get("ignored_dirs") or []) | SEC_SKIP_DIRS
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in ignored and not x.startswith("._"))
        for f in sorted(files):
            low = f.lower()
            if low == "server.py" and os.path.basename(d) == "localintel":
                continue        # retired module, never imported (AGENT_NOTES.md)
            if low.endswith((".py", ".yml", ".yaml", ".toml", ".env")) or low.startswith("dockerfile") or low.endswith(".dockerfile") or low == ".env":
                out.append(os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/"))
    return out


def sec_fp(root, x):
    try:
        line = core.read_text(os.path.join(root, x["file"]))[0].split("\n")[max(int(x["line"]) - 1, 0)].strip()
    except (OSError, IndexError, ValueError):
        line = ""
    import hashlib
    return hashlib.sha256(f"{x['file']}|{x['rule']}|{line}".encode()).hexdigest()[:16]


def security_scan(root, cfg):
    """-> {"available", "total", "reviewed", "new":[finding+fp+text]}; findings listed in the baseline (each with the reason it was reviewed) are not new."""
    if sec is None:
        return {"available": False, "total": 0, "reviewed": 0, "new": []}
    base = {r["fp"]: r for r in (core.load_json(os.path.join(root, BASELINE)).get("reviewed") or []) if isinstance(r, dict) and r.get("fp")}
    found = sec.sec_scan(root, sec_files(root, cfg), include_low=False)
    new, n_rev = [], 0
    for x in found:
        fp = sec_fp(root, x)
        if fp in base:
            n_rev += 1
            continue
        try:
            text = core.read_text(os.path.join(root, x["file"]))[0].split("\n")[max(int(x["line"]) - 1, 0)].strip()[:160]
        except (OSError, IndexError, ValueError):
            text = ""
        new.append({**{k: x.get(k) for k in ("file", "line", "rule", "cwe", "severity")}, "msg": x.get("message") or x.get("msg") or "", "fp": fp, "text": text})
    return {"available": True, "total": len(found), "reviewed": n_rev, "new": new}


def cmd_security(root):
    return security_scan(root, core.load_config(root))


def cmd_security_review(root, fps, why):
    why = str(why or "").strip()
    if len(why) < 8 or len(why) > 300:
        raise ValueError("Say why this is safe (8 to 300 characters): the reason is kept with the finding.")
    cfg = core.load_config(root)
    cur = security_scan(root, cfg)
    pick = [x for x in cur["new"] if fps == "all" or x["fp"] in (fps or [])]
    if not pick:
        raise ValueError("Those findings are not in the current scan.")
    path = os.path.join(root, BASELINE)
    doc = core.load_json(path)
    rows = doc.get("reviewed") or []
    for x in pick:
        rows.append({"fp": x["fp"], "file": x["file"], "rule": x["rule"], "text": x["text"], "why": why, "when": time.strftime("%Y-%m-%d")})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"reviewed": rows}, f, indent=2)
    os.replace(tmp, path)
    return {"ok": True, "marked": len(pick)}



class CheckupFlow(fl.Flow):
    id, title = "checkup", "Health check"
    blurb = "No model. Runs the tests, the app's own selftest rules, a stub scan and the page sweep, and records what is wrong so a fix run can start from it."
    domains = ["api", "data", "web"]
    steps = [fl.S("tests", "Project tests (offline)", "-", "code"), fl.S("rules", "Selftest rules (governance, routes, gates)", "-", "code"),
             fl.S("security", "Security scan of the app source", "-", "code"), fl.S("stubs", "Scan for stubs and placeholders", "-", "code"), fl.S("pages", "Load every page, report errors", "-", "code"),
             fl.S("record", "Save the result for the Self-development page", "-", "code")]
    produces = [".arch/checkup.json"]

    def run(self, e, params):
        items = []

        def add(name, ok, detail="", skipped=False):
            items.append({"name": name, "ok": bool(ok), "detail": str(detail)[:1500], "skipped": bool(skipped)})
            e.say(f"[{'ok' if ok else 'FAIL'}] {name}" + (f"  ({'skipped: ' if skipped else ''}{str(detail)[:120]})" if detail else ""))
        st, log = core.run_tests(e.root, e.cfg)
        add("project tests pass", st == "pass", (log.strip().splitlines() or [""])[-1] if st == "pass" else log[-1200:], skipped=st == "none")
        if is_li(e.root):
            try:    # full output: core.run_cmd keeps only the tail, which cuts the long JSON line
                out = subprocess.run([sys.executable, "run.py", "--selftest", "--json"], cwd=e.root, capture_output=True, text=True,
                                     timeout=e.cfg["limits"]["test_timeout"], env=dict(os.environ, LOCALINTEL_DB=os.path.join(tempfile.mkdtemp(prefix="li-selftest-"), "t.db"))).stdout
            except (subprocess.TimeoutExpired, OSError) as ex:
                out = f"({type(ex).__name__})"
            j = parse_selftest(out)
            if j is None:
                add("selftest rules", False, "no result from run.py --selftest --json: " + out[-300:])
            else:
                rows = j if isinstance(j, list) else (j.get("checks") or [])
                bad = [c for c in rows if isinstance(c, dict) and not c.get("ok")]
                add("selftest rules (governance, routes, gates)", not bad,
                    f"{len(rows) - len(bad)}/{len(rows)} checks pass" if not bad else "failing: " + "; ".join(str(c.get("name"))[:90] for c in bad[:6]))
        if is_li(e.root):
            sc = security_scan(e.root, e.cfg)
            if not sc["available"]:
                add("security scan (injection, secrets, weak crypto, risky config)", True, "skipped: this harness has no security scanner", skipped=True)
            else:
                worst = [f"{x['file']}:{x['line']} {x['rule']}" for x in sc["new"][:5]]
                add("security scan (injection, secrets, weak crypto, risky config)", not sc["new"],
                    f"{len(sc['new'])} new finding(s): " + "; ".join(worst) if sc["new"] else f"nothing new ({sc['reviewed']} reviewed finding(s) on file with a reason)")
        stubs = scan_stubs(e.root, e.cfg)
        add("no stubs or placeholders in the code", not stubs, f"{len(stubs)} found: " + " | ".join(stubs[:6]) if stubs else "none found")
        ok, problems, text = run_sweep(e.root) if is_li(e.root) else (None, [], "not a LocalIntel project")
        add("every page loads without errors", ok if ok is not None else True, "; ".join(problems[:6]) if problems else text, skipped=ok is None)
        bad = [i for i in items if not i["ok"]]
        rec = {"ts": time.time(), "when": time.strftime("%Y-%m-%d %H:%M:%S"), "ok": not bad, "items": items, "run": e.run_id}
        os.makedirs(os.path.join(e.root, ".arch"), exist_ok=True)
        with open(os.path.join(e.root, ".arch", "checkup.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2)
        e.report(*[f"- {'OK ' if i['ok'] else 'FAIL'} {i['name']}" + (f" (skipped)" if i["skipped"] else "") + (f": {i['detail'][:160]}" if i["detail"] and not i["ok"] else "") for i in items],
                 "", f"- Health check {'passed' if not bad else 'found ' + str(len(bad)) + ' problem(s)'}: complete")
        return {"ok": not bad, "items": items}


# ---------------------------------------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------------------------------------
_INSTALLED = False


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    ck.check_files = li_check_files
    core.Engine.call = li_call
    core.Engine.context = li_context
    core.Engine._decide = li_decide
    core.safe_path = li_safe_path
    # the harness's own selftest asserts its exact flow list (15), so the extra flow is not added while it runs
    # (the env var carries that to the hub the selftest starts as a child process)
    if sys.argv[1:2] == ["selftest"]:
        os.environ["LOCALINTEL_ARCH_SELFTEST"] = "1"
    if not os.environ.get("LOCALINTEL_ARCH_SELFTEST"):
        fl.FLOWS["checkup"] = CheckupFlow()


install()


# ---------------------------------------------------------------------------------------------------------
# CLI used by the app (JSON on stdout)
# ---------------------------------------------------------------------------------------------------------
def ollama_state():
    host = core.ollama_host()
    try:
        with urllib.request.urlopen(host + "/api/tags", timeout=3) as r:
            tags = json.loads(r.read().decode()).get("models", [])
        return {"up": True, "host": host, "models": [m["name"] for m in tags], "sizes": {m["name"]: m.get("size") for m in tags}}
    except (urllib.error.URLError, OSError, ValueError):
        return {"up": False, "host": host, "models": [], "sizes": {}}


def measured(models):
    """Hub scoreboard results (architect/hub_data/results.json) -> hub-style model rows for the recommender."""
    res = core.load_json(os.path.join(HERE, "hub_data", "results.json"))
    rows = []
    for m in models:
        agg = {}
        for cat, runs in ((res.get(m) or {}).get("scores") or {}).items():
            last = runs[-3:]
            if last:
                agg[cat] = {"score": round(sum(r["score"] for r in last) / len(last), 1)}
        kind = "coder" if re.search(r"coder|code|devstral|starcoder", m, re.I) else ("reasoning" if re.search(r"r1|think|qwq|reason", m, re.I) else "general")
        rows.append({"name": m, "agg": agg, "know": {"kind": kind}, "params": None, "embed": bool(re.search(r"embed|nomic|bge|minilm", m, re.I))})
    return rows


def effective_model(cfg, role):
    for r in [role] + arch_roles.FALLBACK.get(role, []):
        m = (cfg["roles"].get(r) or "").strip()
        if m:
            return m, ("" if r == role else f"falls back to {r}")
    return (cfg.get("default_model") or ""), "default model" if cfg.get("default_model") else "first installed model"


def _templates():
    try:
        import arch_templates as T
        return {"templates": [{"id": t["id"], "title": t["title"], "blurb": t["blurb"], "stack": t.get("stack", "")} for t in T.TEMPLATES.values()],
                "addons": [{"id": k, "title": v[0]} for k, v in T.ADDONS.items()]}
    except Exception:
        return {"templates": [], "addons": []}


def _sec_summary(root, cfg):
    try:
        d = security_scan(root, cfg)
        return {"available": d["available"], "total": d["total"], "reviewed": d["reviewed"], "new": d["new"][:30], "new_count": len(d["new"])}
    except Exception as e:      # the page must stay alive if the scanner cannot run
        return {"available": False, "error": str(e)[:120], "new": [], "new_count": 0}


def cmd_status(root):
    cfg = core.load_config(root)
    ol = ollama_state()
    ranked, plan = arch_roles.recommend(measured(ol["models"])) if ol["models"] else ({}, {"assignment": {}})
    rows = []
    for rid, r in arch_roles.ROLES.items():
        eff, why = effective_model(cfg, rid)
        rec = (ranked.get(rid) or [None])[0]
        rows.append({"id": rid, "title": r["title"], "group": r.get("group", ""), "blurb": r["blurb"], "assigned": cfg["roles"].get(rid, ""), "effective": eff, "note": why,
                     "suggested": plan["assignment"].get(rid), "basis": rec["basis"] if rec else "", "score": rec["score"] if rec else None})
    last = core.load_json(os.path.join(root, ".arch", "checkup.json"))
    try:
        import arch_training
        tr = arch_training.export([root], write=False)
        training = {"accepted": tr["positive"], "rejected": tr["negative"], "per_role": tr["per_role"], "stats": tr["stats"]}
    except Exception as e:  # keep the page alive if the export code cannot run
        training = {"error": str(e)[:120]}
    return {"layer": LAYER_VERSION, "is_localintel": is_li(root), "root": root, "ollama": ol,
            "options": option_rows(cfg),
            "personas": [{"id": k, "label": v["label"], "short": v["short"], "files": v["files"], "done": v["done"], "tasks": v["tasks"], "defaults": v["defaults"]} for k, v in PERSONAS.items()],
            "flows": [{"id": f.id, "title": f.title, "blurb": f.blurb, "steps": [s["title"] for s in f.steps],
                       "roles": sorted({s["role"].rstrip("*") for s in f.steps if s["role"] != "-"} | set(getattr(f, "uses_roles", []) or []))} for f in fl.FLOWS.values()],
            "templates": _templates(), "security": _sec_summary(root, cfg),
            "roles": rows, "review_mode": cfg["behavior"]["review_mode"], "limits": cfg["limits"],
            "protected_files": protected_files(cfg), "confirm_files": confirm_files(cfg), "ignored_dirs": cfg["files"].get("ignored_dirs") or [],
            "checkup": last or None, "training": training, "node": bool(shutil.which("node")), "sweep": sweep_available(root),
            "pytest": core.has_module("pytest"), "config_file": os.path.join(root, ".arch", "config.json")}


def cmd_runs(root):
    d = os.path.join(root, ".arch", "runs")
    out = []
    if os.path.isdir(d):
        for n in sorted(os.listdir(d), reverse=True)[:40]:
            rp = os.path.join(d, n, "report.md")
            if not os.path.isfile(rp):
                continue
            rep = open(rp, encoding="utf-8", errors="replace").read()
            changed = sorted(set(re.findall(r"`([^`]+)` applied", rep)))
            created = []
            cp = os.path.join(d, n, "created.txt")
            if os.path.isfile(cp):
                created = [x for x in open(cp, encoding="utf-8").read().split("\n") if x]
            calls = 0
            tp = os.path.join(d, n, "trace.jsonl")
            if os.path.isfile(tp):
                calls = sum(1 for ln in open(tp, encoding="utf-8", errors="replace") if '"type": "call"' in ln)
            bad = re.search(r"NOT |FAILED|incomplete|Stopped|open issues|found \d+ problem", rep)
            done = re.search(r"complete|fixed|Wrote:|passed checks|Health check passed", rep, re.I)
            out.append({"name": n, "flow": (re.search(r"- Flow: `(\w+)`", rep) or [None, ""])[1], "goal": (re.search(r"- Goal: (.*)", rep) or [None, ""])[1][:160],
                        "review": (re.search(r"- Review mode: (\w+)", rep) or [None, ""])[1], "calls": calls, "changed": changed, "created": created,
                        "undone": os.path.isfile(os.path.join(d, n, "undone.txt")),
                        "state": "complete" if done and not bad else "open"})
    return {"runs": out}


def cmd_run_detail(root, name):
    if not re.fullmatch(r"\d{8}_\d{6}_[a-z]+", name or ""):
        raise ValueError("bad run name")
    d = os.path.join(root, ".arch", "runs", name)
    if not os.path.isdir(d):
        raise ValueError("no such run")
    rep = open(os.path.join(d, "report.md"), encoding="utf-8", errors="replace").read() if os.path.isfile(os.path.join(d, "report.md")) else ""
    files = []
    bk = os.path.join(d, "backup")
    if os.path.isdir(bk):
        for dd, _, fs in os.walk(bk):
            for f in fs:
                src = os.path.join(dd, f)
                rel = os.path.relpath(src, bk).replace(os.sep, "/")
                cur = os.path.join(root, rel)
                old = core.read_text(src)[0]
                new = core.read_text(cur)[0] if os.path.isfile(cur) else ""
                files.append({"path": rel, "status": "changed" if old != new else "restored", "diff": core.unified(old, new, rel)[:60000]})
    cp = os.path.join(d, "created.txt")
    if os.path.isfile(cp):
        for rel in [x for x in open(cp, encoding="utf-8").read().split("\n") if x]:
            cur = os.path.join(root, rel)
            files.append({"path": rel, "status": "created" if os.path.isfile(cur) else "removed",
                          "diff": core.unified("", core.read_text(cur)[0], rel)[:60000] if os.path.isfile(cur) else ""})
    return {"name": name, "report": rep[:20000], "files": files}


MODEL_OK = re.compile(r"^[A-Za-z0-9._:/@+\-]{1,120}$")


def cmd_set_roles(root, mapping):
    path = os.path.join(root, ".arch", "config.json")
    cfg = core.load_json(path)
    cfg.setdefault("roles", {})
    changed = {}
    for r, m in (mapping or {}).items():
        if r not in arch_roles.ROLES:
            raise ValueError(f"unknown role '{r}'")
        m = str(m or "").strip()
        if m and not MODEL_OK.match(m):
            raise ValueError(f"'{m[:40]}' is not a valid model name")
        if cfg["roles"].get(r, "") != m:
            changed[r] = m
        cfg["roles"][r] = m
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)
    return {"ok": True, "changed": changed}


# ---------------------------------------------------------------------------------------------------------
# run settings: every knob the Self-development page exposes, with its range (one source of truth for UI + validation)
# ---------------------------------------------------------------------------------------------------------
HOSTNAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]{0,98}[A-Za-z0-9])?$")
HOSTPORT = re.compile(r"^(?:\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.\-]{1,100})(?::\d{1,5})?$")
TEST_CMD = re.compile(r"^(?:(?:python3?|py)\s+-m\s+pytest|pytest)(?:\s+[A-Za-z0-9_./=:\-\[\]]+){0,8}$")
OPTIONS = [
    {"key": "limits.max_revisions", "type": "int", "min": 1, "max": 8, "group": "Quality", "label": "Revision rounds", "help": "How many times one step may be redone after a failed check."},
    {"key": "behavior.evaluator", "type": "enum", "choices": ["final", "each", "off"], "group": "Quality", "label": "Evaluator", "help": "final = judge the finished work, each = judge every step, off = skip (fastest)."},
    {"key": "behavior.best_of_n", "type": "int", "min": 1, "max": 3, "group": "Quality", "label": "Candidates per step", "help": "More than 1 asks the model several times and keeps the best. Slower."},
    {"key": "limits.max_files_per_step", "type": "int", "min": 1, "max": 12, "group": "Scope", "label": "Files per step", "help": "Most files one sprint may write. Lower keeps each change small enough to review; higher lets a feature land in one step."},
    {"key": "limits.max_calls", "type": "int", "min": 5, "max": 200, "group": "Scope", "label": "Model calls per run", "help": "Hard stop on model calls in one run, so a retry loop cannot go on forever. Best-of-N and revisions each spend calls."},
    {"key": "limits.num_ctx", "type": "int", "min": 2048, "max": 32768, "step": 1024, "group": "Model", "label": "Context window (tokens)", "help": "Raise only if your VRAM allows; 8192 fits a 6 GB card with a 7-8B model."},
    {"key": "limits.context_chars", "type": "int", "min": 4000, "max": 60000, "step": 1000, "group": "Model", "label": "Project context (characters)", "help": "How much source a step is shown, in characters. Big files are cut to the relevant slices; raise it only if your context window is large."},
    {"key": "localintel.slice_chars", "type": "int", "min": 2000, "max": 16000, "step": 500, "group": "Model", "label": "Slice of a big file (characters)", "help": "How much of index.html, run.py or areas.py a step may see."},
    {"key": "tests.command", "type": "cmd", "group": "Checks", "label": "Test command", "help": "Blank = detect pytest. Only 'python -m pytest ...' or 'pytest ...' is accepted."},
    {"key": "limits.test_timeout", "type": "int", "min": 30, "max": 1800, "step": 30, "group": "Checks", "label": "Test timeout (seconds)", "help": "Longest the project's tests may run before they are stopped and counted as failed. Raise it for a slow suite."},
    {"key": "localintel.ui_sweep_on_html_change", "type": "bool", "group": "Checks", "label": "Sweep every page when index.html changes", "help": "Loads each page in a browser and fails on any error. About 30 s."},
    {"key": "behavior.playwright", "type": "enum", "choices": ["off", "auto"], "group": "Checks", "label": "Browser test of generated UI", "help": "auto = let the harness drive a browser against UI work."},
    {"key": "behavior.mutation", "type": "enum", "choices": ["on", "off"], "group": "Tester", "label": "Mutation check (test flow)", "help": "Breaks the code on purpose to measure whether the tests notice. Slow but honest."},
    {"key": "behavior.mutation_max", "type": "int", "min": 1, "max": 30, "group": "Tester", "label": "Mutations to try", "help": "More mutations give a firmer test-strength score but make the run longer."},
    {"key": "behavior.hunt_max_files", "type": "int", "min": 1, "max": 10, "group": "Tester", "label": "Files per bug hunt", "help": "How many files the bug hunter reads in one run."},
    {"key": "behavior.hunt_findings", "type": "int", "min": 1, "max": 10, "group": "Tester", "label": "Suspicions per bug hunt", "help": "Each one becomes a test; only those that fail by assertion are kept."},
    {"key": "behavior.min_gain_pct", "type": "int", "min": 1, "max": 90, "group": "Optimizer", "label": "Required speed-up (%)", "help": "A change that is not at least this much faster is rolled back."},
    {"key": "behavior.bench_runs", "type": "int", "min": 1, "max": 15, "group": "Optimizer", "label": "Benchmark repetitions", "help": "The median of this many runs is compared before and after."},
    {"key": "behavior.config_strict", "type": "enum", "choices": ["high", "medium", "low"], "group": "Security", "label": "Config scan fails at", "help": "A Dockerfile, CI or compose finding at or above this severity fails the check."},
    {"key": "behavior.network", "type": "enum", "choices": ["offline", "practice", "open"], "group": "Network", "label": "Network for code the Architect runs", "help": "offline = loopback only (tests, benchmarks, model-written scripts). practice = loopback plus the hosts below. open = unrestricted. Your terminal and this app are not affected."},
    {"key": "behavior.net_allow", "type": "list", "group": "Network", "label": "Allowed hostnames (practice)", "help": "Comma separated, e.g. pypi.org, 192.168.1.20. Used only in practice mode."},
    {"key": "behavior.net_block_tools", "type": "bool", "group": "Network", "label": "Also block curl, nmap, ssh and git", "help": "Refuses to launch these from code the Architect runs while the network is offline or practice."},
    {"key": "behavior.git", "type": "enum", "choices": ["off", "commit"], "group": "Checks", "label": "Git checkpoint after a run", "help": "commit = commit the changes when a run succeeds (creates the repository if needed)."},
    {"key": "limits.request_timeout", "type": "int", "min": 30, "max": 3600, "step": 30, "group": "Model", "label": "Model reply timeout (seconds)", "help": "A slow 8B model on a small card may need more."},
    {"key": "limits.keep_alive", "type": "keep", "group": "Model", "label": "Keep the model loaded", "help": "e.g. 10m, 1h or -1 (forever). Saves reloading between steps."},
    {"key": "ollama_host", "type": "host", "group": "Model", "label": "Ollama host", "help": "Blank = this app's model address. Use host:port to reach a GPU box on your network."},
]
_OPT = {o["key"]: o for o in OPTIONS}


def _dig(cfg, key, default=None):
    cur = cfg
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def option_rows(cfg):
    return [{**o, "value": _dig(cfg, o["key"], "" if o["type"] in ("cmd", "host", "keep") else ([] if o["type"] == "list" else None)), "default": _dig(core.DEFAULTS, o["key"], "")} for o in OPTIONS]


def clean_option(o, v):
    t = o["type"]
    if t == "bool":
        if isinstance(v, str):
            v = v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)
    if t == "int":
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            raise ValueError(f"{o['label']}: enter a whole number")
        if not o["min"] <= n <= o["max"]:
            raise ValueError(f"{o['label']}: must be between {o['min']} and {o['max']}")
        return n
    if t == "enum":
        if v not in o["choices"]:
            raise ValueError(f"{o['label']}: choose one of {', '.join(o['choices'])}")
        return v
    if t == "list":
        items = [x.strip() for x in (v if isinstance(v, list) else str(v or "").split(",")) if x and str(x).strip()]
        if len(items) > 20 or any(not HOSTNAME.match(x) for x in items):
            raise ValueError(f"{o['label']}: up to 20 plain hostnames or IP addresses, comma separated")
        return items
    if t == "host":
        v = str(v or "").strip()
        if v and not HOSTPORT.match(v):
            raise ValueError(f"{o['label']}: use host or host:port, for example 192.168.1.20:11434")
        return v
    if t == "keep":
        v = str(v or "").strip()
        if v and not re.fullmatch(r"-1|\d{1,4}[smh]", v):
            raise ValueError(f"{o['label']}: use 30m, 1h, 90s or -1")
        return v
    v = str(v or "").strip()
    if v and not TEST_CMD.match(v):
        raise ValueError(f"{o['label']}: only 'python -m pytest ...' or 'pytest ...' with plain arguments is accepted")
    return v


def cmd_set_options(root, mapping):
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("nothing to save")
    path = os.path.join(root, ".arch", "config.json")
    cfg = core.load_json(path)
    changed = {}
    for k, v in mapping.items():
        o = _OPT.get(k)
        if not o:
            raise ValueError(f"unknown setting '{str(k)[:40]}'")
        val = clean_option(o, v)
        cur = cfg
        parts = k.split(".")
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        if cur.get(parts[-1]) != val:
            changed[k] = val
        cur[parts[-1]] = val
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)
    return {"ok": True, "changed": changed}


def cmd_reset_options(root):
    path = os.path.join(root, ".arch", "config.json")
    cfg = core.load_json(path)
    for k in _OPT:
        cur = cfg
        parts = k.split(".")
        for part in parts[:-1]:
            cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is None:
                break
        if isinstance(cur, dict):
            cur.pop(parts[-1], None)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return {"ok": True}


def _changed_run(d):
    return (os.path.isdir(os.path.join(d, "backup")) and any(files for _, _, files in os.walk(os.path.join(d, "backup")))) or os.path.isfile(os.path.join(d, "created.txt"))


def cmd_undo(root, name):
    """Restore the files one run changed. Only the newest run that still has changes can be undone (an older run's
    backups would overwrite what newer runs did), and a run is undone once."""
    if not re.fullmatch(r"\d{8}_\d{6}_[a-z]+", name or ""):
        raise ValueError("bad run name")
    base = os.path.join(root, ".arch", "runs")
    d = os.path.join(base, name)
    if not os.path.isdir(d):
        raise ValueError("no such run")
    if os.path.isfile(os.path.join(d, "undone.txt")):
        raise ValueError("this run was already undone")
    if not _changed_run(d):
        raise ValueError("this run did not change any files")
    live = [n for n in sorted(os.listdir(base)) if _changed_run(os.path.join(base, n)) and not os.path.isfile(os.path.join(base, n, "undone.txt"))]
    if live and live[-1] != name:
        raise ValueError(f"undo the newer run {live[-1]} first")
    n = core.undo_run(root, d)
    with open(os.path.join(d, "undone.txt"), "w", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
    return {"ok": True, "files": n}


def cmd_export(root):
    import arch_training
    return arch_training.export([root], write=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="LocalIntel layer for the Architect (JSON on stdout).")
    ap.add_argument("cmd", choices=["status", "runs", "run-detail", "set-roles", "set-options", "reset-options", "export", "undo", "security", "security-review"])
    ap.add_argument("--root", required=True)
    ap.add_argument("--run")
    ap.add_argument("--roles", help="JSON object role -> model")
    ap.add_argument("--fps", help="JSON list of finding fingerprints, or the word all")
    ap.add_argument("--why", help="why the findings are safe")
    ap.add_argument("--options", help="JSON object setting key -> value")
    a = ap.parse_args(argv)
    root = os.path.abspath(a.root)
    try:
        if not os.path.isdir(root):
            raise ValueError("project folder not found")
        out = {"status": lambda: cmd_status(root), "runs": lambda: cmd_runs(root), "run-detail": lambda: cmd_run_detail(root, a.run),
               "set-roles": lambda: cmd_set_roles(root, json.loads(a.roles or "{}")), "set-options": lambda: cmd_set_options(root, json.loads(a.options or "{}")), "reset-options": lambda: cmd_reset_options(root), "export": lambda: cmd_export(root),
               "undo": lambda: cmd_undo(root, a.run), "security": lambda: cmd_security(root),
               "security-review": lambda: cmd_security_review(root, json.loads(a.fps or "[]") if a.fps != "all" else "all", a.why)}[a.cmd]()
        print(json.dumps(out))
        return 0
    except (ValueError, OSError) as e:
        print(json.dumps({"error": str(e)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
