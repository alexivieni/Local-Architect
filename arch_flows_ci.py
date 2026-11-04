"""cicd flow (registered into arch_flows.FLOWS): the CI/CD specialist.

  cicd   detect pipelines -> write a correct baseline when there is none (code, no model) -> verify against the repository with arch_ci
         (parsing, pinning, permissions, injection, "does every command refer to something that exists", fake-green patterns)
         -> the ci_engineer fixes blocking findings or makes the change you asked for -> the SAME verifier runs again
         -> the pipeline's safe commands (tests, linters, builds) run locally, offline -> docs/ci.md

Code decides: a finding is gone only when the verifier no longer reports it, and a pipeline is only "verified" if its tests really ran here.
Nothing contacts GitHub or GitLab. Installs and non-test commands are listed as skipped, never run.
"""
import re

import arch_ci as ci
import arch_core as core
import arch_flows as base
import arch_progress
from arch_flows import Flow, S

if "cicd" not in base.PRIORITY:
    base.PRIORITY.insert(0, "cicd")

CI_FIX = """Fix or change this CI/CD pipeline. Reply with FILE:/EDIT: blocks for the pipeline files only.

REQUEST:
{goal}

Rules: keep what the pipeline already does; never turn a failing step green by deleting it, adding `|| true`, `continue-on-error` or an echo; run the commands this
repository really has (see the findings); first-party actions pinned to a major version, third-party actions to a commit SHA only if you know it (otherwise leave the tag);
`permissions: contents: read` unless a job needs more; add `timeout-minutes`; untrusted event text goes through `env:`.

CURRENT PIPELINE FILES AND PROJECT CONTEXT:
{context}

BLOCKING FINDINGS (each is a failure the first run would hit, or a security problem). Fix every one:
{findings}

{feedback}"""

CI_NEW = """Write a CI pipeline for this project as a FILE: block at .github/workflows/ci.yml (GitHub Actions) unless the project already uses GitLab (.gitlab-ci.yml).

REQUEST:
{goal}

PROJECT FACTS: {facts}

Use only commands this repository really has. Read-only permissions, a timeout on every job, first-party actions pinned to a major version, no secrets in the file.

CONTEXT:
{context}"""

CUSTOM = re.compile(r"\b(add|deploy|release|publish|docker|matrix|cache|schedule|cron|environment|staging|production|artifact)\b", re.I)


def _text(findings, n=20):
    return "\n".join(f"- {x['file']}:{x['line']} [{x['severity']}] {x['rule']}: {x['msg']}" for x in findings[:n]) or "(none)"


def _key(x):
    return (x["file"], x["rule"], x["msg"])


class CicdFlow(Flow):
    id, title = "cicd", "CI/CD pipeline"
    blurb = ("Finds or writes the pipeline, verifies it against the repository (pinning, permissions, injection, commands that point at real files, tests that can fail), "
             "has the CI/CD engineer fix what is blocking, and runs the safe commands locally to prove it.")
    steps = [S("detect", "Find pipelines and project facts", "-", "code"), S("baseline", "Write a correct baseline if there is none", "-", "code"),
             S("verify", "Verify against the repository", "-", "code"), S("fix", "Fix blocking findings / make the requested change (loop)", "ci_engineer", "llm"),
             S("reverify", "Verify again: findings must be gone, none new", "-", "code"), S("local", "Run the safe commands locally, offline", "-", "code"),
             S("report", "Write docs/ci.md", "-", "code")]
    produces = [".github/workflows/*.yml", ".gitlab-ci.yml", "docs/ci.md"]
    uses_roles = ["ci_engineer"]

    def run(self, e, params):
        goal = e.goal or "set up CI"
        pipes = ci.find_pipelines(e.root)
        had_pipeline = bool(pipes)
        facts = ci.detect_project(e.root)
        e.say(f"[detect] pipelines: {', '.join(r for _, r in pipes) or 'none'}")
        e.report(f"- Pipelines found: {', '.join(r for _, r in pipes) or 'none'}", "- Project: " + (", ".join(k for k in ("python", "node", "tests") if facts.get(k)) or "not recognised"))
        if not pipes:
            files = ci.baseline(e.root, release=bool(params.get("release") or re.search(r"\brelease\b", goal, re.I)))
            if files:
                for rel, txt in files.items():
                    e.put_file(rel, txt, "baseline pipeline")
            elif e.review != "dry":
                r = e.call("ci_engineer", CI_NEW.format(goal=goal, facts=", ".join(k for k, v in facts.items() if v is True) or "unknown", context=e.context(goal, 6000)), label="ci-new")
                e.apply(r["text"], "new pipeline")
            pipes = ci.find_pipelines(e.root)
            if not pipes:
                e.report("- No pipeline was written (nothing recognised and the model gave no usable file).")
                return {"ok": False, "files": []}
        if e.review == "dry":
            res = ci.check_all(e.root)
            e.report(f"- Dry run: {len(res['findings'])} finding(s); nothing changed.")
            return {"ok": True, "dry": True}
        before = ci.check_all(e.root)
        blocking = [x for x in before["findings"] if x["gate"]]
        e.say(f"[verify] {len(blocking)} blocking, {len(before['findings']) - len(blocking)} advisory (parser: {before['parser']})")
        e.report(f"- Verify: {len(blocking)} blocking, {len(before['findings']) - len(blocking)} advisory", *[f"  - `{x['file']}:{x['line']}` [{x['severity']}] {x['rule']}: {x['msg']}" for x in before["findings"][:15]])
        after, ok = before, not blocking
        asks_for_change = had_pipeline and bool(CUSTOM.search(goal))
        if blocking or asks_for_change:
            feedback, prev_keys = "", {_key(x) for x in blocking}
            coach = arch_progress.Coach(e, "ci_engineer")
            for attempt in range(e.cfg["limits"]["max_revisions"] + 1):
                ctx = e.context(goal, None, always=[r for r in before["pipelines"]][:3])
                r = e.call("ci_engineer", CI_FIX.format(goal=goal, findings=_text(blocking if blocking else after["findings"]), feedback=feedback, context=ctx), label=f"ci-fix-try{attempt + 1}")
                res = e.apply(r["text"], f"pipeline fix try {attempt + 1}")
                if any(x["status"] == "quit" for x in res):
                    return {"ok": False, "stopped": True}
                changed = [x["rel"] for x in res if x["status"] in ("applied", "identical")]
                if not changed:
                    feedback = "FEEDBACK: nothing could be applied:\n" + "\n".join(f"- {x['rel']}: {x['msg']}" for x in res if x["status"] == "error") + "\nResend with the exact FILE:/EDIT: formats (DEF: for one Python function).\n\n"
                    e.mark(r["id"], False, feedback)
                    feedback = coach.after(feedback, r["text"], feedback, [])
                    continue
                after = ci.check_all(e.root)
                now = [x for x in after["findings"] if x["gate"]]
                new = [x for x in now if _key(x) not in prev_keys]
                good = not now
                e.mark(r["id"], good, "" if good else _text(now, 8))
                e.say(f"[verify] try {attempt + 1}: {len(now)} blocking ({len(new)} new)")
                if good:
                    ok = True
                    break
                feedback = coach.after("FEEDBACK: the verifier still reports these (and does not accept removing the step or masking the failure):\n" + _text(now) +
                            ("\nThese are NEW since your last change:\n" + _text(new) if new else "") + "\n\n", r["text"], _text(now), changed)
                blocking = now
            coach.close()
        ok = ok and not [x for x in after["findings"] if x["gate"]]
        local = []
        for rel in after["pipelines"]:
            e.say(f"[local] {rel}")
            local += ci.run_local(e.root, rel, say=lambda m: e.say(m), timeout=e.cfg["limits"].get("test_timeout", 300))
        ls = ci.local_summary(local) if local else {"pass": 0, "fail": 0, "skipped": 0, "ok": False}
        e.report(f"- Local run: {ls['pass']} passed, {ls['fail']} failed, {ls['skipped']} skipped")
        for r_ in local:
            e.report(f"  - {r_['status']}: `{r_['cmd'][:80]}`" + (f" ({r_['why']})" if r_["why"] else ""))
        body = ["# CI/CD report", "", f"Pipelines: {', '.join(after['pipelines'])}", f"YAML parser: {after['parser']}", "",
                f"## Verification: {sum(1 for x in after['findings'] if x['gate'])} blocking, {sum(1 for x in after['findings'] if not x['gate'])} advisory", ""]
        body += [f"- `{x['file']}:{x['line']}` {'**BLOCKING** ' if x['gate'] else ''}[{x['severity']}] {x['rule']}: {x['msg']}" for x in after["findings"]] or ["- none"]
        body += ["", f"## Local run: {ls['pass']} passed, {ls['fail']} failed, {ls['skipped']} skipped", ""]
        body += [f"- {r_['status'].upper()} `{r_['cmd'][:90]}`" + (f": {r_['why']}" if r_["why"] else "") for r_ in local] or ["- nothing was run"]
        body += ["", "## Not verified here", "",
                 "- Anything that needs the hosted runner: secrets, environments, deployments, services, matrix entries for other operating systems.",
                 "- Commands that install packages or are not tests/linters/builds (listed as skipped above)."]
        e.put_file("docs/ci.md", "\n".join(body) + "\n", "CI report", quiet=True)
        e.report("", "- Report: docs/ci.md")
        if ls["fail"]:
            e.say("[!] the pipeline is valid but one of its own commands fails locally: that is a code problem, not a pipeline problem (try the bugfix flow)")
        return {"ok": ok and ls["fail"] == 0, "files": e.changed_files + ["docs/ci.md"], "blocking": sum(1 for x in after["findings"] if x["gate"]), "local": ls}


for _f in (CicdFlow(),):
    base.FLOWS[_f.id] = _f
