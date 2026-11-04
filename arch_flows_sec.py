"""Security / infra / pentest flows (registered into arch_flows.FLOWS):

  secure    static security findings (arch_security, local/defensive only) -> security_engineer fixes them ->
            re-scanning the SAME tool proves it, not opinion -> docs/security.md
  config    the same idea for Dockerfile/CI/compose/toml hardening -> config_engineer -> docs/config.md
  network   fixes a network/firewall JSON plan against real CIDR/overlap/shadowed-rule/admin-port math
            (arch_security.check_cidr_plan / check_firewall) -> network_engineer -> docs/network.md
  ml        builds or reviews an ML pipeline with the ml_engineer role; determinism is PROVEN by running the
            tests twice and diffing, not assumed
  pentest   turns findings YOU already gathered (with your own authorized tools) into a scope-checked
            attack-path write-up (red team) and detections/mitigations (blue team). Nothing here scans a
            network or runs an attack tool: it reasons over findings.json and scope.json, both written by
            you, and the scope check (arch_pentest.py) is code, not opinion -- a host missing from scope.json
            is refused, never "fixed" or worked around.

Same principle as every other flow: code decides pass/fail wherever it can; models only propose.
"""
import json
import os
import re

import arch_checks as ck
import arch_core as core
import arch_flows as base
import arch_pentest as pt
import arch_quality as q
import arch_security as sec
from arch_flows import Flow, S, verify, log_checks
from arch_flows_pro import explicit_files, scope_files, capture, restore, can_test


# ------------------------------------------------------------------------------------------------ helpers
def _config_files(e):
    out = []
    for f in core.list_files(e.root, e.cfg):
        base_name = os.path.basename(f).lower()
        if base_name.startswith("dockerfile") or f.lower().endswith((".yml", ".yaml", ".toml")):
            out.append(f)
    return out


def _sec_files(e, goal):
    exp = explicit_files(e, goal, (".py",))
    if exp:
        return exp
    py = [f for f in core.list_files(e.root, e.cfg) if f.endswith(".py") and not f.startswith(("tests/", "bench/"))]
    return py[:40]


SEC_FIX = """These are STATIC security findings from a scanner (the CWE ids and the rule are trustworthy; a finding can
still be a false positive in context -- say so in a one-line comment if one is, and leave that spot unchanged).
Fix every real one with the smallest change that removes the defect WITHOUT changing the function's intended behaviour.

FINDINGS:
{findings}

CODE:
{context}

{feedback}"""


class SecureFlow(Flow):
    id, title = "secure", "Secure"
    blurb = "Static security scan (secrets, injection, weak crypto, risky defaults -- local and defensive only) -> fixed by the security engineer -> proven by re-running the SAME scanner, not opinion."
    steps = [S("scan", "Static security scan", "-", "code"), S("fix", "Fix real findings (loop)", "security_engineer", "llm"),
             S("rescan", "Re-scan: fixed findings must be gone + tests still pass", "-", "code"),
             S("detect", "Detections/mitigations for what's left", "blue_team", "llm"), S("report", "Write docs/security.md", "-", "code")]
    produces = ["fixed source files", "docs/security.md"]
    uses_roles = ["security_engineer", "blue_team"]

    def run(self, e, params):
        goal = e.goal
        files = _sec_files(e, goal)
        if not files:
            e.say("[-] no Python files in scope")
            return {"ok": False}
        before = sec.sec_scan(e.root, files, include_low=False)
        e.say(f"[scan] {len(before)} high/medium finding(s) in {len(files)} file(s)")
        e.report(f"- Scanned: {len(files)} file(s)", f"- Findings (high/medium): {len(before)}", "")
        for x in before[:12]:
            e.report(f"  - `{x['file']}:{x['line']}` [{x['severity']}] {x['rule']} ({x['cwe']}): {x['msg']}")
        if not before:
            e.put_file("docs/security.md", "# Security\n\nNo high/medium findings in the scanned files.\n", "security report", quiet=True)
            e.report("", "- Nothing to fix. Report: docs/security.md")
            return {"ok": True, "files": ["docs/security.md"], "findings_before": 0, "findings_after": 0}
        affected = sorted({x["file"] for x in before})
        txt = "\n".join(f"- {x['file']}:{x['line']} [{x['severity']}] {x['rule']} ({x['cwe']}): {x['msg']}" for x in before[:20])
        feedback, ok, after = "", False, before
        for attempt in range(e.cfg["limits"]["max_revisions"] + 1):
            r = e.call("security_engineer", SEC_FIX.format(findings=txt, feedback=feedback, context=e.context(goal, None, always=affected[:4])), label=f"secfix-try{attempt + 1}")
            res = e.apply(r["text"], f"security fix try {attempt + 1}")
            if any(x["status"] == "quit" for x in res):
                return {"ok": False, "stopped": True}
            if e.review == "dry":
                return {"ok": True, "dry": True}
            changed = [x["rel"] for x in res if x["status"] in ("applied", "identical")]
            if not changed:
                feedback = "FEEDBACK: nothing could be applied:\n" + "\n".join(f"- {x['rel']}: {x['msg']}" for x in res) + "\n\n"
                e.mark(r["id"], False, feedback)
                continue
            v = verify(e, changed)
            log_checks(e, f"security fix try {attempt + 1}", v)
            after = sec.sec_scan(e.root, files, include_low=False)
            fixed_n = len(before) - len(after)
            good = v["ok"] and fixed_n > 0
            e.mark(r["id"], good, v["text"] or f"{fixed_n} finding(s) fixed, {len(after)} remain")
            if good:
                ok = True
                break
            feedback = "FEEDBACK: checks failed, or no finding actually went away:\n" + (v["text"] or f"{len(after)} finding(s) still present") + "\n\n"
        e.say(f"[scan] after: {len(after)} finding(s) remain (started with {len(before)})")
        e.report(f"- Findings after fix: {len(after)} (from {len(before)})")
        detect_text = ""
        if after and e.cfg["behavior"]["evaluator"] != "off":
            r2 = e.call("blue_team", "For each remaining finding, give ONE concrete detection (a log field, query or rule) and the lowest-risk mitigation, one short paragraph each.\n\n"
                        + "\n".join(f"- {x['file']}:{x['line']} {x['rule']} ({x['cwe']}): {x['msg']}" for x in after[:10]), label="detect")
            detect_text = core.strip_think(r2["text"]).strip()
        body = ["# Security", "", f"Scanned: {', '.join(files[:20])}", "", f"## Findings before: {len(before)}", ""]
        body += [f"- `{x['file']}:{x['line']}` [{x['severity']}] {x['rule']} ({x['cwe']}): {x['msg']}" for x in before]
        body += ["", f"## Findings after: {len(after)}", ""]
        body += [f"- `{x['file']}:{x['line']}` [{x['severity']}] {x['rule']} ({x['cwe']}): {x['msg']}" for x in after] or ["- none"]
        if detect_text:
            body += ["", "## Detections and mitigations for what remains", "", detect_text]
        e.put_file("docs/security.md", "\n".join(body) + "\n", "security report", quiet=True)
        e.report("", "- Report: docs/security.md")
        return {"ok": ok or not before, "files": e.changed_files + ["docs/security.md"], "findings_before": len(before), "findings_after": len(after)}


CFG_FIX = """These are hardening findings from a scanner (Dockerfile/CI/config hygiene). Fix every real one with the smallest
change that does not change what the service/pipeline is for (pin versions, drop root, remove baked-in secrets to the
environment, add a healthcheck, fix YAML/CI issues). If a finding is a false positive here, leave it and say why in one line.

FINDINGS:
{findings}

CODE:
{context}

{feedback}"""


class ConfigFlow(Flow):
    id, title = "config", "Harden config"
    blurb = "Scans Dockerfile/CI/compose/toml files with the same rules used on the user's projects, fixes real findings with the config engineer, and proves it by re-scanning."
    steps = [S("scan", "Scan Dockerfile / CI / toml files", "-", "code"), S("fix", "Fix real findings (loop)", "config_engineer", "llm"),
             S("rescan", "Re-scan: fixed findings must be gone", "-", "code"), S("report", "Write docs/config.md", "-", "code")]
    produces = ["fixed Dockerfile/CI/config files", "docs/config.md"]
    uses_roles = ["config_engineer"]

    def run(self, e, params):
        goal = e.goal
        files = explicit_files(e, goal, (".yml", ".yaml", ".toml")) or _config_files(e)
        if not files:
            e.say("[-] no Dockerfile/CI/compose/toml files found")
            e.report("- Nothing to scan: no Dockerfile, .yml/.yaml or .toml files in the project.")
            return {"ok": False}
        before = sec.scan_config_files(e.root, files)
        e.say(f"[scan] {len(before)} finding(s) in {len(files)} file(s)")
        e.report(f"- Scanned: {', '.join(files)}", f"- Findings (high/medium): {len(before)}", "")
        for x in before[:12]:
            e.report(f"  - `{x['file']}:{x['line']}` [{x['severity']}] {x['rule']}: {x['msg']}")
        if not before:
            e.put_file("docs/config.md", "# Config hardening\n\nNo high/medium findings.\n", "config report", quiet=True)
            e.report("", "- Nothing to fix. Report: docs/config.md")
            return {"ok": True, "files": ["docs/config.md"], "findings_before": 0, "findings_after": 0}
        txt = "\n".join(f"- {x['file']}:{x['line']} [{x['severity']}] {x['rule']}: {x['msg']}" for x in before[:20])
        feedback, ok, after = "", False, before
        for attempt in range(e.cfg["limits"]["max_revisions"] + 1):
            r = e.call("config_engineer", CFG_FIX.format(findings=txt, feedback=feedback, context=e.context(goal, None, always=files[:4])), label=f"cfgfix-try{attempt + 1}")
            res = e.apply(r["text"], f"config fix try {attempt + 1}")
            if any(x["status"] == "quit" for x in res):
                return {"ok": False, "stopped": True}
            if e.review == "dry":
                return {"ok": True, "dry": True}
            changed = [x["rel"] for x in res if x["status"] in ("applied", "identical")]
            if not changed:
                feedback = "FEEDBACK: nothing could be applied:\n" + "\n".join(f"- {x['rel']}: {x['msg']}" for x in res) + "\n\n"
                e.mark(r["id"], False, feedback)
                continue
            v = verify(e, [c for c in changed if not c.endswith((".yml", ".yaml", ".toml")) and "dockerfile" not in os.path.basename(c).lower()], run_tests=can_test(e))
            after = sec.scan_config_files(e.root, files)
            fixed_n = len(before) - len(after)
            good = v["ok"] and fixed_n > 0
            e.mark(r["id"], good, v["text"] or f"{fixed_n} fixed, {len(after)} remain")
            log_checks(e, f"config fix try {attempt + 1}", v)
            if good:
                ok = True
                break
            feedback = "FEEDBACK: checks failed, or nothing actually went away:\n" + (v["text"] or f"{len(after)} still present") + "\n\n"
        e.report(f"- Findings after fix: {len(after)} (from {len(before)})")
        body = ["# Config hardening", "", f"Scanned: {', '.join(files)}", "", f"## Before: {len(before)}", ""]
        body += [f"- `{x['file']}:{x['line']}` {x['rule']}: {x['msg']}" for x in before] + ["", f"## After: {len(after)}", ""]
        body += [f"- `{x['file']}:{x['line']}` {x['rule']}: {x['msg']}" for x in after] or ["- none"]
        e.put_file("docs/config.md", "\n".join(body) + "\n", "config report", quiet=True)
        e.report("", "- Report: docs/config.md")
        return {"ok": ok or not before, "files": e.changed_files + ["docs/config.md"], "findings_before": len(before), "findings_after": len(after)}


NET_FIX = """This network/firewall plan has problems found by checking real CIDR math (overlap, host capacity, gateway
placement) and firewall-rule math (default policy, any-any rules, admin ports exposed, shadowed rules). Fix them.
Keep the same JSON shape (top-level "subnets" for a network plan, or "default_inbound"/"rules" for a firewall plan).

FILE: {name}
PROBLEMS:
{problems}

CURRENT CONTENT:
{content}

{feedback}"""


class NetworkFlow(Flow):
    id, title = "network", "Network design"
    blurb = "Checks a network/CIDR plan and firewall rules against real math (overlaps, capacity, default-deny, admin ports, shadowed rules), then has the network engineer fix them -- no network is touched."
    steps = [S("scope", "Find network/firewall JSON files", "-", "code"), S("check", "CIDR + firewall-rule checks", "-", "code"),
             S("fix", "Fix real problems (loop)", "network_engineer", "llm"), S("recheck", "Re-check the same math", "-", "code"),
             S("report", "Write docs/network.md", "-", "code")]
    produces = ["fixed network/firewall JSON", "docs/network.md"]
    uses_roles = ["network_engineer"]

    def _candidates(self, e):
        out = []
        for f in core.list_files(e.root, e.cfg):
            if not f.lower().endswith(".json") or "/node_modules/" in f or f.startswith(".arch/"):
                continue
            try:
                text = core.read_text(os.path.join(e.root, f), 60000)[0]
            except OSError:
                continue
            if '"subnets"' in text or ('"rules"' in text and ("default_inbound" in text or '"action"' in text)):
                out.append(f)
        return out

    def run(self, e, params):
        goal = e.goal
        files = explicit_files(e, goal, (".json",)) or self._candidates(e)
        if not files:
            e.say("[-] no network-plan or firewall-rule JSON found (needs a top-level 'subnets' list, or 'rules' + 'default_inbound')")
            e.report("- Nothing to check: no recognisable network/firewall JSON file.")
            return {"ok": False}
        total_before, per_file = 0, {}
        for f in files:
            text = core.read_text(os.path.join(e.root, f), 60000)[0]
            probs = sec.check_cidr_plan(text, f) if '"subnets"' in text else sec.check_firewall(text, f)
            per_file[f] = probs
            total_before += len(probs)
        e.say(f"[check] {total_before} problem(s) across {len(files)} file(s)")
        e.report(f"- Checked: {', '.join(files)}", f"- Problems: {total_before}", "")
        for f, probs in per_file.items():
            for p in probs[:8]:
                e.report(f"  - `{f}`: {p['msg']}")
        if not total_before:
            e.put_file("docs/network.md", "# Network design\n\nNo problems found.\n", "network report", quiet=True)
            e.report("", "- Nothing to fix. Report: docs/network.md")
            return {"ok": True, "files": ["docs/network.md"], "problems_before": 0, "problems_after": 0}
        fixed_files, total_after = [], total_before
        for f, probs in per_file.items():
            if not probs:
                continue
            feedback, ok = "", False
            content = core.read_text(os.path.join(e.root, f), 60000)[0]
            for attempt in range(e.cfg["limits"]["max_revisions"] + 1):
                txt = "\n".join(f"- {p['msg']}" for p in probs)
                r = e.call("network_engineer", NET_FIX.format(name=f, problems=txt, feedback=feedback, content=content), label=f"netfix-{os.path.basename(f)}-try{attempt + 1}")
                reply = core.strip_think(r["text"])
                m = re.search(r"```(?:json)?\n(.*?)```", reply, re.S)
                candidate = (m.group(1) if m else reply).strip()
                try:
                    json.loads(candidate)
                except ValueError as ex:
                    feedback = f"FEEDBACK: invalid JSON: {ex}\n\n"
                    e.mark(r["id"], False, feedback)
                    continue
                if e.review == "dry":
                    return {"ok": True, "dry": True}
                now_probs = sec.check_cidr_plan(candidate, f) if '"subnets"' in content else sec.check_firewall(candidate, f)
                good = len(now_probs) < len(probs)
                e.mark(r["id"], good, "; ".join(p["msg"] for p in now_probs[:3]) or "fixed")
                if good:
                    p2 = e.put_file(f, candidate + ("\n" if not candidate.endswith("\n") else ""), "network fix", quiet=True)
                    if p2["status"] == "applied":
                        fixed_files.append(f)
                    ok, probs = True, now_probs
                    break
                feedback = "FEEDBACK: still has problems (or new ones):\n" + "\n".join(f"- {p['msg']}" for p in now_probs[:10]) + "\n\n"
            per_file[f] = probs
        total_after = sum(len(v) for v in per_file.values())
        e.report(f"- Problems after fix: {total_after} (from {total_before})")
        body = ["# Network design", "", f"Checked: {', '.join(files)}", "", f"## Before: {total_before}", ""]
        for f, probs in per_file.items():
            body += [f"- `{f}`: {len(probs)} problem(s) remain"] + [f"  - {p['msg']}" for p in probs]
        e.put_file("docs/network.md", "\n".join(body) + "\n", "network report", quiet=True)
        e.report("", "- Report: docs/network.md")
        return {"ok": total_after < total_before, "files": fixed_files + ["docs/network.md"], "problems_before": total_before, "problems_after": total_after}


class MlFlow(base.BuildFlow):
    id, title = "ml", "ML pipeline"
    blurb = "Builds or fixes an ML/data pipeline with the ML engineer role (leakage, reproducibility, eval methodology); determinism is PROVEN by running the tests twice and diffing output, not assumed."
    all_role = "ml_engineer"
    steps = [S("plan", "Plan sprints (contract first)", "planner", "llm"), S("code", "Implement each layer", "ml_engineer", "llm"),
             S("checks", "Syntax + tests", "-", "code"), S("fix", "Fix loop on failure", "ml_engineer", "llm"),
             S("determinism", "Run tests twice, diff output", "-", "code"), S("judge", "Judge against the contract", "evaluator", "judge")]
    produces = ["pipeline/model files", "tests/", ".arch/contract.json"]
    uses_roles = ["planner", "ml_engineer", "evaluator"]

    def run(self, e, params):
        out = base.BuildFlow.run(self, e, params)
        if not out.get("ok") or e.review == "dry" or not can_test(e):
            return out
        st1, log1 = core.run_tests(e.root, e.cfg)
        st2, log2 = core.run_tests(e.root, e.cfg)
        # pytest's own summary line ("N passed in 0.0Xs") carries wall-clock timing that varies between two
        # runs of IDENTICAL code -- diffing it as-is would flag the test harness's own noise as the model's
        # non-determinism. Strip just that duration before comparing, so this proves the generated pipeline's
        # own behaviour repeats, not that two runs happened to take the same number of milliseconds.
        _detime = lambda s: re.sub(r"\bin \d+\.\d+s\b", "in Ns", s)
        deterministic = st1 == st2 == "pass" and _detime(log1[-4000:]) == _detime(log2[-4000:])
        e.say(f"[determinism] two runs {'match' if deterministic else 'DIFFER'}")
        e.report("", f"- Determinism: two test runs {'match' if deterministic else 'differ -- check for unseeded randomness or unordered iteration'}")
        out["deterministic"] = deterministic
        return out


# ------------------------------------------------------------------------------------------------ PENTEST
ATTACKPATH_WRITE = """Authorized scope (ONLY these hosts -- never name any other): {hosts}
Findings you may use (cite by id; invent nothing else):
{findings}

Write realistic attack paths that chain these findings. Reply with ONE ```json array, each item:
{{"finding_ids": ["..."], "host": "...", "steps": "concrete steps", "impact": "business impact if exploited"}}
Use only the finding ids above and only hosts in the authorized scope. Chain at least 2 findings where the evidence supports it.

{feedback}"""

BLUE_WRITE = """For each attack path below, write ONE concrete detection (log field, query, or rule) and the lowest-risk
mitigation (prefer fixing root cause). State plainly if you cannot detect it with the evidence given.

ATTACK PATHS:
{paths}"""


class PentestFlow(Flow):
    id, title = "pentest", "Pentest write-up"
    blurb = ("Turns findings YOU already gathered (with your own authorized recon/scan tools) into a scope-checked attack-path write-up and defenses. "
             "Nothing here scans a network or runs an attack tool: a host missing from scope.json is refused, never worked around.")
    steps = [S("scope", "Load + validate scope.json (code, not opinion)", "-", "code"), S("findings", "Load findings.json", "-", "code"),
             S("redteam", "Chain findings into attack paths (scope-checked)", "red_team", "llm"),
             S("verify", "Every id and host checked against scope.json", "-", "code"),
             S("blueteam", "Detections + mitigations", "blue_team", "llm"), S("report", "Write docs/pentest.md", "-", "code")]
    produces = ["docs/pentest.md"]
    uses_roles = ["pentester", "red_team", "blue_team"]

    def run(self, e, params):
        live = bool(params.get("live"))
        scope, is_default, err = pt.load_scope(e.root, params.get("scope_file") or "scope.json")
        if err:
            e.say(f"[!] {err}")
            e.report(f"- Stopped: {err}", "", f"Example scope.json:\n```json\n{json.dumps(pt.SCOPE_EXAMPLE, indent=2)}\n```")
            return {"ok": False}
        if live and is_default:
            msg = "live mode needs a real scope.json (authorized_targets, window) -- refusing to run against the default loopback-only scope"
            e.say(f"[!] {msg}")
            e.report(f"- Stopped: {msg}", "", f"Example scope.json:\n```json\n{json.dumps(pt.SCOPE_EXAMPLE, indent=2)}\n```")
            return {"ok": False}
        hosts = scope.get("authorized_targets") or []
        e.say(f"[scope] {'live' if live else 'sim/lab'}: {', '.join(str(h) for h in hosts)}" + ("" if scope.get("window") else " (no time window set)"))
        e.report(f"- Mode: {'LIVE (signed scope.json)' if live else 'sim/lab'}", f"- Authorized targets: {', '.join(str(h) for h in hosts)}", "")
        findings, ferr = pt.load_findings(e.root, params.get("findings_file") or "findings.json")
        if ferr:
            e.say(f"[!] {ferr}")
            e.report(f"- Stopped: {ferr}")
            return {"ok": False}
        scope_problems = pt.check_findings(scope, findings)
        if scope_problems:
            e.say(f"[!] {len(scope_problems)} finding(s) name an out-of-scope host")
            for p in scope_problems:
                e.report(f"  - REFUSED: {p}")
            findings = [f for f in findings if f not in [x for x in findings if not pt.in_scope(scope, x.get("host", ""))]]
        e.say(f"[findings] {len(findings)} in-scope finding(s)")
        e.report(f"- In-scope findings used: {len(findings)}", "")
        if not findings:
            e.put_file("docs/pentest.md", "# Pentest write-up\n\nNo in-scope findings to work from.\n", "pentest report", quiet=True)
            return {"ok": False, "files": ["docs/pentest.md"]}
        ftxt = "\n".join(f"[{f['id']}] {f['host']}: {f['desc']}" for f in findings)
        feedback, paths = "", None
        for attempt in range(e.cfg["limits"]["max_revisions"] + 1):
            r = e.call("red_team", ATTACKPATH_WRITE.format(hosts=", ".join(str(h) for h in hosts), findings=ftxt, feedback=feedback), label=f"attackpath-try{attempt + 1}")
            text = core.strip_think(r["text"])
            m = re.search(r"```(?:json)?\n(.*?)```", text, re.S)
            candidate = (m.group(1) if m else text).strip()
            try:
                items = json.loads(candidate)
            except ValueError as ex:
                feedback = f"FEEDBACK: invalid JSON: {ex}\n\n"
                e.mark(r["id"], False, feedback)
                continue
            if not isinstance(items, list) or not items:
                feedback = "FEEDBACK: expected a non-empty JSON array of attack-path objects.\n\n"
                e.mark(r["id"], False, feedback)
                continue
            ids = {f["id"] for f in findings}
            bad = next((it for it in items if not isinstance(it, dict) or any(i not in ids for i in (it.get("finding_ids") or []))
                        or it.get("host") not in [h for h in hosts] or not (it.get("steps") and it.get("impact"))), None)
            if bad:
                feedback = f"FEEDBACK: every item must cite only given finding ids, name only an authorized host, and include steps + impact. Rejected: {json.dumps(bad)[:300]}\n\n"
                e.mark(r["id"], False, feedback)
                continue
            e.mark(r["id"], True, f"{len(items)} attack path(s), all scope-checked")
            paths = items
            break
        if not paths:
            e.say("[!] could not produce a scope-compliant attack path")
            e.report("- Stopped: no scope-compliant attack path could be produced after the allowed attempts.")
            return {"ok": False}
        e.say(f"[redteam] {len(paths)} attack path(s), all within scope")
        path_txt = "\n".join(f"- host {p['host']}, findings {p['finding_ids']}: {p['steps']} -> impact: {p['impact']}" for p in paths)
        r2 = e.call("blue_team", BLUE_WRITE.format(paths=path_txt), label="blueteam")
        blue = core.strip_think(r2["text"]).strip()
        body = ["# Pentest write-up", "", f"Mode: {'LIVE' if live else 'sim/lab'}", f"Authorized targets: {', '.join(str(h) for h in hosts)}", "",
                "## Findings used", ""] + [f"- [{f['id']}] {f['host']}: {f['desc']}" for f in findings]
        if scope_problems:
            body += ["", "## Findings refused (out of scope)", ""] + [f"- {p}" for p in scope_problems]
        body += ["", "## Attack paths (red team, scope-checked)", ""]
        for p in paths:
            body += [f"### {p['host']}  (findings {', '.join(p['finding_ids'])})", f"- Steps: {p['steps']}", f"- Impact: {p['impact']}", ""]
        body += ["## Detections and mitigations (blue team)", "", blue, ""]
        e.put_file("docs/pentest.md", "\n".join(body) + "\n", "pentest report", quiet=True)
        e.report("", "- Report: docs/pentest.md")
        return {"ok": True, "files": ["docs/pentest.md"], "paths": len(paths)}


for _f in (SecureFlow(), ConfigFlow(), NetworkFlow(), MlFlow(), PentestFlow()):
    base.FLOWS[_f.id] = _f
