"""Advisor: combines signals this toolkit ALREADY measures for real -- the doctor's environment checks,
the hardware/workload assessment (including self-tuning drift), a real static scan of the project's own
code, and (if a scope.json/findings.json pair is sitting in the project, synthetic-lab style) the
Architect's own trusted scope-vs-findings check -- into ONE ranked list of concrete next actions.

This is not a model guessing what might be wrong. Every proposal names the real check, scan finding, or
config value it came from in its "evidence" field, and nothing is proposed that doesn't trace back to one
of those. "Propose based off synthetic situations, combine them" means: if the project has a scope.json
and findings.json (the format security_lab and the pentest flow both already use), those are run through
arch_pentest's real scope-checking logic and folded into the same list as everything else -- not imported
as generated code, just read as the data files they are.

    python architect.py advise [--root PATH]        -- one combined, evidence-cited action list
    GET /api/arch/advise?root=PATH                   -- same, over the hub's API
"""
import os

SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}


def _propose(title, why, severity, action, evidence=None, source=""):
    return {"title": title, "why": why, "severity": severity, "action": action, "evidence": evidence or [], "source": source}


def from_doctor(doctor_out):
    """Failing / advisory environment checks -> proposals. Nothing here is a new check; doctor.run() already
    ran real subprocesses, sockets and file I/O -- this just explains what to do about what it found."""
    props = []
    for c in doctor_out.get("checks", []):
        if c["ok"]:
            continue
        sev = "low" if c["advisory"] else "high"
        props.append(_propose(
            f"Doctor check failed: {c['name']}", c["detail"] or "no detail", sev,
            "run `python architect.py doctor --fix` for the ones that can be auto-repaired, or fix manually for the rest",
            [f"doctor check {c['name']!r}: {c['detail']}"], "doctor"))
    return props


def from_assess(assess_out):
    """Hardware-tuning drift and workload gaps -> proposals. Both numbers come straight out of
    arch_assess.assess(), which itself reads real hardware + the real role/model scoreboard."""
    props = []
    tier_label = assess_out.get("vram_tier_label", "this machine")
    for d in (assess_out.get("tuning_drift") or []):
        props.append(_propose(
            f"Setting {d['path']} is not tuned for {tier_label}",
            f"currently {d['current']!r}, recommended {d['recommended']!r}", "medium",
            "apply the hardware-tuned settings (Assess page -> \"Apply tuned settings\", or `architect.py assess` then save them)",
            [f"{d['path']}: current={d['current']!r} recommended={d['recommended']!r}"], "assess"))
    for w in assess_out.get("workloads", []):
        if w["missing"]:
            props.append(_propose(
                f"{w['label']}: no usable model for {', '.join(w['missing'])}", w["blurb"],
                "high" if not w["covered"] else "medium",
                "assign/install a model for these roles, or run the Scoreboard so a guess becomes a measurement",
                [f"workload {w['id']!r} missing role coverage: {w['missing']}"], "assess"))
        elif w["guessed"]:
            props.append(_propose(
                f"{w['label']}: coverage for {', '.join(w['guessed'])} is a guess, not a measurement", w["blurb"],
                "low", "run the Scoreboard against these roles to confirm (or replace) the guessed scores",
                [f"workload {w['id']!r} guessed roles: {w['guessed']}"], "assess"))
    return props


def from_code_scan(root, files):
    """Real static-analysis findings over the project's OWN files (arch_security.sec_scan -- real AST
    parsing, not pattern guessing) -> proposals, grouped by rule so ten hits on one rule are one proposal."""
    import arch_security as sec
    props = []
    findings = sec.sec_scan(root, files)
    by_rule = {}
    for f in findings:
        by_rule.setdefault(f["rule"], []).append(f)
    for rule, items in sorted(by_rule.items(), key=lambda kv: -len(kv[1])):
        sev = max((i["severity"] for i in items), key=lambda s: SEVERITY_RANK.get(s, 0))
        locs = [f"{i['file']}:{i['line']}" for i in items]
        props.append(_propose(
            f"{len(items)} real {rule} finding(s) in this project's code", items[0]["msg"], sev,
            "run the secure flow (`architect.py run --flow secure`) to fix and re-scan, or fix the lines below by hand",
            locs[:8], "code_scan"))
    return props


def from_scope_findings(root):
    """If scope.json and/or findings.json sit in the project (the format security_lab's template, the
    pentest flow, and a real engagement all share), run them through arch_pentest's own real scope-checking
    logic -- never a generated package's copy of it, so this works the same whether the project came from
    the security_lab template, a hand-written scope.json, or neither."""
    import arch_pentest as pentest
    props = []
    has_scope = os.path.isfile(os.path.join(root, "scope.json"))
    has_findings = os.path.isfile(os.path.join(root, "findings.json"))
    if not has_findings:
        return props  # nothing synthetic to combine here -- not an error, just nothing to report
    findings, ferr = pentest.load_findings(root)
    if ferr:
        props.append(_propose("findings.json could not be read", ferr, "medium",
                               "fix the JSON in findings.json, or remove it if it's not meant to be read yet",
                               [ferr], "scope"))
        return props
    scope, is_default, serr = pentest.load_scope(root)
    if serr:
        props.append(_propose("scope.json is invalid or its authorization window has lapsed", serr, "high",
                               "fix or renew scope.json before trusting any finding against it", [serr], "scope"))
        return props
    if is_default and has_scope:
        pass  # scope.json exists but failed to parse as non-default; load_scope already said why above
    problems = pentest.check_findings(scope, findings)
    if problems:
        props.append(_propose(
            f"{len(problems)} of {len(findings)} findings name a host outside the authorized scope",
            "a finding against an out-of-scope host must not be acted on, even if it looks real",
            "high", "re-check those hosts against scope.json's authorized_targets, or fix the finding's host field",
            problems[:8], "scope"))
    else:
        props.append(_propose(
            f"All {len(findings)} findings in findings.json are within the authorized scope", "",
            "low", "no action needed -- combine with the code-scan and doctor results above for next steps",
            [f"{len(findings)} findings checked against scope.json ({'default loopback-only' if is_default else 'scope.json'})"], "scope"))
    return props


def advise(root, doctor_out=None, assess_out=None, files=None):
    """Combines everything above into one ranked list. doctor_out/assess_out are passed in by callers who
    already have them (the hub does, in one state build); if omitted here, nothing from that source is
    added -- this function never re-runs a check silently and claims it did."""
    import arch_core as core
    root = os.path.abspath(root)
    props = []
    if doctor_out:
        props += from_doctor(doctor_out)
    if assess_out:
        props += from_assess(assess_out)
    if os.path.isdir(root):
        if files is None:
            files = core.list_files(root, core.load_config(root))
        props += from_code_scan(root, files)
        props += from_scope_findings(root)
    props.sort(key=lambda p: -SEVERITY_RANK.get(p["severity"], 0))
    by_sev = {"high": 0, "medium": 0, "low": 0}
    for p in props:
        by_sev[p["severity"]] = by_sev.get(p["severity"], 0) + 1
    return {"root": root, "count": len(props), "by_severity": by_sev, "proposals": props}


def main(argv=None):
    import argparse
    import json
    import arch_doctor as doctor
    import arch_assess as assess
    import arch_core as core
    import model_hub as mh
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)
    st = mh.build_state()
    d_out = doctor.run(root)
    a_out = assess.assess(st.get("models", []), st.get("hw"), core.load_config(root if os.path.isdir(root) else None))
    out = advise(root, d_out, a_out)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
