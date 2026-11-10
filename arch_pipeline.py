"""The `ship` flow: one goal, end to end. build (or new) -> tests measured by mutation -> bug hunt -> security scan + fix -> review -> docs/ship.md.

Each stage IS an existing flow, run by the same engine with the same gates, so nothing here invents a new way to pass: a stage passes only
when its own code checks pass. The stages are chosen by the builder model's tier (a tiny model cannot afford a bug hunt and a mutation
round on top of a build) and can be given explicitly (`--set` is not needed: `run --flow ship --stages build,test,review`). Every finished
stage is checkpointed, so `resume` continues after a crash; a failed build stops the pipeline, a stage that finds nothing to do does not.
"""
import json
import os
import time

import arch_core as core
import arch_flows as fl

STAGES = ["build", "test", "hunt", "secure", "review"]
BY_TIER = {"tiny": ["build", "review"], "small": ["build", "test", "review"], "mid": ["build", "test", "hunt", "secure", "review"], "large": STAGES}


def tier_of(e):
    try:
        import arch_profiles
        prof = core.load_json(os.path.join(core.data_dir(), "profile.json")) or {}
        t = prof.get("tier")
        if t in BY_TIER:
            return t
        return arch_profiles.tier_for(e.model_for("generator"))[0]
    except Exception:
        return "small"


def stage_goal(stage, goal, files):
    names = ", ".join(files[:6])
    return {"build": goal, "new": goal,
            "test": f"Improve the tests for {names}: cover the behaviour added for: {goal}",
            "hunt": f"Hunt for bugs in {names}",
            "secure": f"Fix security findings in {names}",
            "review": f"Review {names} for: {goal}"}[stage]


class ShipFlow(fl.Flow):
    id, title = "ship", "Ship (end to end)"
    blurb = "One goal, end to end: build, then tests, bug hunt, security and review on what was built -- every stage an existing flow with its own code gates."
    steps = [fl.S("build", "Build the goal (or start the project)", "planner, generator*", "llm"), fl.S("test", "Mutation-measured tests for the new code", "tester", "llm"),
             fl.S("hunt", "Adversarial read; each suspicion becomes a test", "bug_hunter", "llm"), fl.S("secure", "Static security scan, fixes re-scanned", "security_engineer", "llm"),
             fl.S("review", "Facts from tools + reviewer's notes", "reviewer", "llm"), fl.S("ship", "docs/ship.md: what was done and what each gate said", "-", "code")]
    produces = ["source files", "tests/", "docs/bugs.md", "docs/security.md", "docs/review.md", "docs/ship.md"]
    uses_roles = ["planner", "generator", "generator_ui", "generator_sql", "tester", "bug_hunter", "security_engineer", "reviewer", "evaluator"]

    def run(self, e, params):
        goal, t0 = e.goal, time.time()
        rs = params.get("resume") or {}
        want = params.get("stages") or rs.get("stages") or BY_TIER[tier_of(e)]
        want = [s for s in (want.split(",") if isinstance(want, str) else want) if s in STAGES]
        empty = not core.list_files(e.root, e.cfg)
        done = dict(rs.get("ship_done") or {})
        files = list(rs.get("files") or [])
        e.say(f"[ship] stages: {' -> '.join(want)}" + (" (resuming)" if done else ""))
        e.report(f"- Pipeline: {' -> '.join(want)}", "")
        e.checkpoint(stages=want, ship_done=done, files=files)
        for stage in want:
            if stage in done:
                e.say(f"[ship] {stage}: already done, skipped")
                continue
            fid = "new" if (stage == "build" and empty and "new" in fl.FLOWS) else stage
            if stage != "build" and not [f for f in files if f.endswith(".py")]:
                e.report(f"## {stage}", "- Skipped: the build produced no Python files for this stage to work on.", "")
                done[stage] = {"ok": True, "skipped": True}
                e.checkpoint(ship_done=done)
                continue
            e.flow, e.goal = fid, stage_goal(stage, goal, [f for f in files if f.endswith(".py") and not f.startswith("tests/")] or files)
            e.deny_prefixes = []
            e.say(f"\n===== ship: {stage} ({fid}) =====")
            e.report(f"## {stage}", f"- Goal: {e.goal}")
            sub = dict(params)
            sub.pop("resume", None)
            sub.pop("stages", None)
            if stage == "hunt":
                sub.setdefault("autofix", True)
            try:
                out = fl.FLOWS[fid].run(e, sub) or {}
            except (core.LLMError, core.BudgetExceeded, core.LoopDetected) as ex:
                out = {"ok": False, "error": str(ex)}
            if out.get("dry"):
                return {"ok": True, "dry": True, "files": files}
            if out.get("stopped"):
                return {"ok": False, "stopped": True, "files": files}
            files = sorted(set(files + [f for f in (out.get("files") or []) if isinstance(f, str)] + list(e.changed_files)))
            # three outcomes: a stage whose checks could not run is not_sure, never a pass and never a plain fail
            done[stage] = {"ok": bool(out.get("ok")) and not out.get("unsure"), "unsure": bool(out.get("unsure")), "error": out.get("error"),
                           "why": out.get("why")}
            e.checkpoint(ship_done=done, files=files)
            said = "could not be checked" if out.get("unsure") else ("passed its gates" if out.get("ok") else "did NOT pass")
            e.report(f"- {stage}: {said}" + (f" ({(out.get('error') or out.get('why'))[:160]})" if out.get("error") or (out.get("unsure") and out.get("why")) else ""), "")
            if stage == "build" and not out.get("ok"):
                e.say("[ship] the build did not pass its checks; the later stages need working code, so the pipeline stops here (resume continues it)")
                break
        e.flow, e.goal = "ship", goal
        lines = ["# Shipped: " + goal, "", f"Pipeline: {' -> '.join(want)}  ({round(time.time() - t0)} s, {e.calls} model calls)", ""]
        for s in want:
            d = done.get(s)
            lines.append(f"- **{s}**: " + ("not run" if not d else "skipped (nothing to work on)" if d.get("skipped") else
                                            "could not be checked (not sure)" if d.get("unsure") else
                                            "passed" if d["ok"] else "failed" + (f": {d.get('error')}" if d.get("error") else "")))
        docs = [p for p in ("docs/bugs.md", "docs/security.md", "docs/review.md") if os.path.isfile(os.path.join(e.root, p))]
        if docs:
            lines += ["", "Reports: " + ", ".join(f"`{p}`" for p in docs)]
        lines += ["", f"Files touched: {len(files)}"] + [f"- `{f}`" for f in files[:40]]
        e.put_file("docs/ship.md", "\n".join(lines) + "\n", "ship summary", quiet=True)
        failed = any(not d.get("ok") and not d.get("unsure") and not d.get("skipped") for d in done.values())
        unsure = not failed and any(d.get("unsure") for d in done.values())
        ok = "build" in done and not failed and all(d.get("ok") or d.get("unsure") or d.get("skipped") for d in done.values())
        out = {"ok": ok, "files": files, "stages": done}
        if unsure:              # run_flow turns this into the not_sure verdict; a pass needs every stage checked
            out.update(unsure=True, why="; ".join(f"{s}: {d.get('why') or 'its checks could not run'}" for s, d in done.items() if d.get("unsure"))[:400])
        return out


fl.FLOWS["ship"] = ShipFlow()
fl.KEYWORDS["ship"] = r"\bship\b|end[- ]to[- ]end|production[- ]ready|the whole thing|all the way|build and test|build, test"
if "ship" not in fl.PRIORITY:
    fl.PRIORITY.insert(fl.PRIORITY.index("build"), "ship")
