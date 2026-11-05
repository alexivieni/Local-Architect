"""Self-test for arch_harness (harness packs: parts -> spec -> harnesses/<name>/ -> check -> one run with an overlay).
No model, no network, nothing is launched: runs are only planned (dry), packs are composed into a throwaway folder.
Run: python arch_selftest_harness.py
"""
import copy
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

EXAMPLES = ("local_fixer", "claude_plans_feature_team", "overnight_trace_factory")


def run():
    results = []

    def check(name, cond, detail=None):
        results.append(bool(cond))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond or detail is None else f"  -> {str(detail)[:500]}"))

    tmp = tempfile.mkdtemp(prefix="arch_harness_")
    old_dd, old_nc = os.environ.get("ARCHITECT_DATA_DIR"), os.environ.get("ARCHITECT_NO_CACHE")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    try:
        import arch_dataflow
        import arch_gates
        import arch_harness as H
        import arch_params
        import arch_tools

        # ---- the catalogue
        cat = H.parts()
        for sec in ("tiers", "flows", "step_kinds", "gates", "tools", "claude_modes", "agent_templates", "budgets"):
            check(f"parts() lists {sec}", isinstance(cat.get(sec), list) and cat[sec], sec)
        every = [it for items in cat.values() for it in items]
        check("every part has a name and a one-line purpose", all(it.get("name") and str(it.get("purpose") or "").strip() and "\n" not in str(it["purpose"]) for it in every),
              [it for it in every if not str(it.get("purpose") or "").strip()][:3])
        check("every gate is in the catalogue", {g["name"] for g in cat["gates"]} == set(arch_gates.GATES))
        check("every tool is in the catalogue, writes marked", {t["name"] for t in cat["tools"]} == set(arch_tools.TOOLS)
              and any(t["writes"] for t in cat["tools"]) and any(not t["writes"] for t in cat["tools"]))
        check("every step kind is described", {k["name"] for k in cat["step_kinds"]} == set(arch_dataflow.STEP_KINDS)
              and all(k["name"] in H.STEP_PURPOSE for k in cat["step_kinds"]))
        check("the five Claude modes match the setting's choices",
              [m["name"] for m in cat["claude_modes"]] == arch_params._SETTINGS_BY_PATH["online.cloud_tier"][1][7:].split(","))
        check("installed flows include bugfix, plan_local_build and explore", {"bugfix", "plan_local_build", "explore"} <= {f["name"] for f in cat["flows"]})
        check("standalone checks are real gates", set(H.STANDALONE) <= set(arch_gates.GATES))
        check("the worker tier names the roles that use it", any(t["name"] == "worker" and t["roles"] for t in cat["tiers"]))

        # ---- YAML out, read back the same
        tricky = {"a": "colon: inside", "b": "hash # inside", "c": "quote \" and 'single'", "d": "line\nbreak", "e": [1, 2.5, True, None, ""],
                  "f": {}, "g": [], "h": [{"x": {"y": [1, {"z": "on"}]}}], "on": "key that YAML 1.1 reads as a bool", "i": "8", "j": "true"}
        back = H.load_yaml(H.dump_yaml(tricky))
        check("dump_yaml round-trips tricky values through the app's own reader", back == tricky, back)

        # ---- the three examples compose, validate and say how they run
        specs = {s["name"]: s for s in H.examples()}
        check("three example specs", tuple(specs) == EXAMPLES, list(specs))
        expected_mode = {"local_fixer": "installed", "claude_plans_feature_team": "own", "overnight_trace_factory": "own"}
        out_dir = os.path.join(tmp, "packs")
        for name in EXAMPLES:
            b = H.build(specs[name])
            check(f"{name}: builds with no problem", not b["problems"], b["problems"])
            check(f"{name}: runs as {expected_mode[name]}", b.get("mode") == expected_mode[name], b.get("mode"))
            c = H.compose(specs[name], folder=os.path.join(out_dir, name))
            check(f"{name}: composes", c["ok"], c["problems"])
            d = os.path.join(out_dir, name)
            check(f"{name}: has harness.yaml, flow.yaml, settings.json, README.md", all(os.path.isfile(os.path.join(d, f)) for f in H.FILES))
            flow = arch_dataflow.load_yaml_file(os.path.join(d, "flow.yaml"))
            check(f"{name}: flow.yaml passes arch_dataflow.validate", arch_dataflow.validate(flow) == [], arch_dataflow.validate(flow))
            check(f"{name}: flow id is the pack name", flow.get("id") == name)
            ov = json.load(open(os.path.join(d, "settings.json"), encoding="utf-8"))
            check(f"{name}: every overlay key is a known setting or a tier",
                  all(arch_params.is_known_setting(k) or k.startswith("tiers.") for k in ov), list(ov))
            ck = H.check(d)
            check(f"{name}: check() passes, no drift", ck["ok"] and not any("differs" in n for n in ck["notes"]), ck)
            readme = open(os.path.join(d, "README.md"), encoding="utf-8").read()
            check(f"{name}: README says how to run it and why the parts are there", "arch_harness.py run" in readme and "## Why each part" in readme)

        # ---- the shipped packs in harnesses/
        for name in EXAMPLES:
            ck = H.check(name)
            check(f"shipped harnesses/{name} passes check()", ck["ok"], ck["problems"])
            if ck["notes"]:
                print(f"         note: {ck['notes']}")
        listed = {p["name"] for p in H.list_packs()}
        check("list_packs() finds the three shipped packs", set(EXAMPLES) <= listed, listed)

        # ---- what each example promises, in code
        fx = H.build(specs["local_fixer"])
        check("fixer: local-only (online off, Claude off)", fx["settings"].get("online.enabled") is False and fx["settings"].get("online.cloud_tier") == "off")
        check("fixer: an empty tier never blanks the person's model", not any(k.startswith("tiers.") for k in fx["settings"]))
        ft = H.build(specs["claude_plans_feature_team"])
        check("feature team: Claude plans, with dollar caps", ft["settings"].get("online.cloud_tier") == "plan"
              and ft["settings"].get("limits.cloud_usd_per_run") == 1.5 and ft["settings"].get("limits.cloud_usd_per_day") == 5.0)
        check("feature team: prompts point at their real folder", all(s.get("prompt", "plan_local_build/").startswith("plan_local_build/")
                                                                       for s in arch_dataflow.validate_steps(ft["flow"]) if s.get("prompt")))
        check("feature team: tests_pass joins the verdict", "post_tests_pass.ok" in ft["flow"]["verdict"]["pass_if"]
              and ft["flow"]["steps"][-1]["gate"] == "tests_pass")
        check("feature team: the pack's ceiling caps every machine-class override",
              all((o.get("budget") or {}).get("seconds", 0) <= 3600 for o in ft["flow"]["overrides"].values()), ft["flow"]["overrides"])
        check("feature team: one read-only Claude reviewer in the fleet plan", len(ft["fleet"]) == 1 and ft["fleet"][0]["mode"] == "plan")
        ctx_unsure = {"sprints": {"ok": True}, "post_tests_pass": {"ok": False, "unsure": True}}
        check("a final check that could not run is no pass", arch_dataflow.evaluate(ft["flow"]["verdict"]["pass_if"], ctx_unsure) is False)
        check("...and it passes when both hold", arch_dataflow.evaluate(ft["flow"]["verdict"]["pass_if"],
                                                                       {"sprints": {"ok": True}, "post_tests_pass": {"ok": True}}) is True)
        tf = H.build(specs["overnight_trace_factory"])
        loops = [s for s in tf["flow"]["steps"] if s.get("kind") == "tool_loop"]
        check("trace factory: no tool that writes code survives", not any(arch_tools.TOOLS[t]["writes"] and t != "memory_append"
                                                                          for s in loops for t in s["tools"]), [s["tools"] for s in loops])
        check("trace factory: seed fixed, Claude off", tf["settings"].get("behavior.seed") == 1234 and tf["settings"]["online.cloud_tier"] == "off")

        # ---- the run command and the overlay for one run only
        fixer_dir = os.path.join(out_dir, "local_fixer")
        argv = H.command(fixer_dir, "fix paginate", root=os.path.join(tmp, "proj"))
        check("installed pack runs through architect.py run --flow bugfix", argv[1].endswith("architect.py") and argv[2] == "run"
              and argv[argv.index("--flow") + 1] == "bugfix", argv)
        check("...with every overlay key as --set", sorted(argv[i + 1] for i, a in enumerate(argv) if a == "--set")
              == sorted(H.set_args(fx["settings"])), argv)
        own = H.command(os.path.join(out_dir, "overnight_trace_factory"), "where is the retry logic?")
        check("own-flow pack runs through arch_harness.py run", own[1].endswith("arch_harness.py") and own[2] == "run" and "--review" in own, own)
        check("review mode comes from the pack (auto for the factory)", own[own.index("--review") + 1] == "auto")
        import arch_core as core
        import architect
        cfg_path = core.CONFIG_FILE
        before = open(cfg_path, encoding="utf-8").read() if os.path.isfile(cfg_path) else None
        cfg = copy.deepcopy(core.load_config(os.path.join(tmp, "proj")))
        for kv in H.set_args(ft["settings"]):
            k, v = kv.split("=", 1)
            architect._set_path(cfg, k, v)
        got = {k: cfg[k.split(".")[0]][k.split(".")[1]] for k in ft["settings"]}
        check("--set reads every overlay value back exactly (types kept)", got == ft["settings"], got)
        check("text that looks like a number stays text", H.set_args({"limits.keep_alive": "30"}) == ['limits.keep_alive="30"'])
        after = open(cfg_path, encoding="utf-8").read() if os.path.isfile(cfg_path) else None
        check("applying the overlay never touches the settings file", before == after)
        dr = H.run(fixer_dir, "fix it", dry=True)
        check("run(dry) returns the command and starts nothing", dr["ok"] and dr["dry"] and dr["argv"] == H.command(fixer_dir, "fix it"))
        seen = []
        rr = H.run(fixer_dir, "fix it", runner=lambda a: seen.append(a) or 0)
        check("run() hands exactly that command to the runner", rr["ok"] and seen and seen[0] == rr["argv"])

        # ---- bad parts are refused, each in plain words
        base = specs["local_fixer"]

        def refused(label, change, needle):
            sp = copy.deepcopy(base)
            change(sp)
            b = H.build(sp)
            hit = any(needle in p for p in b["problems"])
            check(f"refused: {label}", hit, b["problems"])
            return b

        refused("unknown check", lambda s: s.update(gates=["no_such_gate"]), "unknown check")
        refused("a check that needs an earlier step", lambda s: s.update(gates=["repro_fails_right_reason"]), "cannot be a step of its own")
        refused("a check listed twice", lambda s: s.update(gates=["tests_pass", "tests_pass"]), "listed twice")
        refused("tests_green_before on a fix", lambda s: s.update(gates=["tests_green_before"]), "exactly where a fix starts")
        refused("unknown tool", lambda s: s.update(tools=["shell"]), "unknown tool")
        refused("tools on a flow with no tool step", lambda s: s.update(tools=["read"]), "no tool_loop step")
        refused("tools that leave a step with none", lambda s: s.update(flow="explore", goal_kind="explore", tools=["apply"]), "no tool left")
        refused("unknown setting", lambda s: s["settings"].update({"behavior.nope": 1}), "not a setting")
        refused("setting out of range (not clamped)", lambda s: s["settings"].update({"limits.max_calls": 99999}), "outside")
        refused("bad choice", lambda s: s["settings"].update({"behavior.fake_code": "maybe"}), "must be one of")
        refused("a setting that weakens a check", lambda s: s["settings"].update({"behavior.fake_code": "off"}), "turns off a check")
        refused("online switch set by hand", lambda s: s["settings"].update({"online.enabled": True}), "follows from claude_mode")
        refused("a tier set through settings", lambda s: s["settings"].update({"tiers.worker": "x"}), "set through tiers")
        refused("unknown Claude mode", lambda s: s.update(claude_mode="sometimes"), "claude_mode")
        refused("Claude without a dollar cap", lambda s: s.update(claude_mode="plan"), "needs budgets.usd_per_run")
        refused("a dollar cap with Claude off", lambda s: s.update(budgets={"usd_per_run": 1}), "claude_mode is off")
        refused("unknown flow", lambda s: s.update(flow="nope"), "not installed")
        refused("no flow at all", lambda s: s.update(flow=None), "flow:")
        refused("unknown tier", lambda s: s.update(tiers={"turbo": ""}), "unknown tier")
        refused("a non-Claude model in the cloud tier", lambda s: s.update(tiers={"cloud": "gpt-x"}, claude_mode="plan",
                                                                           budgets={"usd_per_run": 1}), "claude: model")
        refused("a Claude model in a local tier", lambda s: s.update(tiers={"worker": "claude:claude-opus-5-5"}), "local tier")
        refused("a cloud model with Claude off", lambda s: s.update(tiers={"cloud": "claude:claude-sonnet-5-5"}), "claude_mode is off")
        refused("traces with Claude", lambda s: s.update(goal_kind="traces", claude_mode="all", budgets={"usd_per_run": 1}), "never training data")
        refused("bad name", lambda s: s.update(name="Bad Name"), "name")
        refused("a name that is an installed flow", lambda s: s.update(name="bugfix"), "installed flow's id")
        refused("unknown goal kind", lambda s: s.update(goal_kind="magic"), "goal_kind")
        refused("unknown review mode", lambda s: s.update(review="yolo"), "review")
        refused("unknown spec part", lambda s: s.update(rocket=True), "unknown part")
        refused("budget not a whole number above 0", lambda s: s.update(budgets={"calls": -1}), "whole number above 0")
        refused("budget that is a bool", lambda s: s.update(budgets={"tokens": True}), "whole number above 0")
        refused("unknown budget", lambda s: s.update(budgets={"hours": 3}), "unknown budget")
        refused("agents without a fleet cap", lambda s: s.update(agents=["reviewer"]), "fleet_usd")
        refused("fleet cap without agents", lambda s: s.update(budgets={"fleet_usd": 2}), "no agents")
        refused("unknown agent template", lambda s: s.update(agents=[{"template": "nobody_here", "values": {}}], budgets={"fleet_usd": 5}), "agents:")
        refused("a Claude agent in a local-only pack", lambda s: s.update(budgets={"fleet_usd": 5}, agents=[{"template": "reviewer", "values": {
            "topic": "t", "targets": "a.py", "goal": "g", "focus": "f"}}]), "runs on Claude")
        refused("agents over the fleet cap", lambda s: s.update(claude_mode="plan", budgets={"usd_per_run": 1, "fleet_usd": 0.5}, agents=[
            {"template": "reviewer", "values": {"topic": "t", "targets": "a.py", "goal": "g", "focus": "f"}}]), "above the cap")
        own_flow = {"steps": [{"id": "look", "kind": "tool_loop", "role": "architect", "prompt": "explore", "tools": ["read"]}]}
        refused("an own step whose prompt is not <flow>/<file>", lambda s: s.update(flow=own_flow), "not found")
        bad_id = {"steps": [{"id": "goal", "kind": "code", "gate": "tests_pass"}]}
        refused("an own step id that hides a context key", lambda s: s.update(flow=bad_id), "would hide")
        bad_gate = {"steps": [{"id": "x", "kind": "code", "gate": "made_up"}]}
        refused("an own step with an unknown gate", lambda s: s.update(flow=bad_gate), "unknown gate")
        bad_key = {"steps": [{"id": "x", "kind": "code", "gate": "tests_pass"}], "shell": "rm -rf /"}
        refused("an own flow with an unknown key", lambda s: s.update(flow=bad_key), "unknown key")
        check("a Python-only flow is refused in plain words", any("built in Python only" in p or "not installed" in p
                                                                  for p in H.build(dict(base, flow="design_python_only_x"))["problems"]))

        # ---- compose writes nothing when refused; never overwrites by accident
        bad_dir = os.path.join(tmp, "packs", "refused")
        r = H.compose(dict(base, gates=["no_such_gate"]), folder=bad_dir)
        check("a refused spec writes no folder", not r["ok"] and not os.path.exists(bad_dir), r)
        r2 = H.compose(base, folder=fixer_dir)
        check("an existing pack is not overwritten without overwrite=True", not r2["ok"] and "already holds a pack" in r2["problems"][0])
        r3 = H.compose(base, folder=fixer_dir, overwrite=True)
        check("overwrite=True replaces the files", r3["ok"], r3["problems"])

        # ---- check() catches a pack edited by hand
        def broken_copy(label, edit, needle):
            d = os.path.join(tmp, "broken", label.replace(" ", "_"))
            shutil.copytree(fixer_dir, d)
            edit(d)
            ck = H.check(d)
            check(f"check() refuses: {label}", not ck["ok"] and any(needle in p for p in ck["problems"]), ck["problems"])
            rn = H.run(d, "x", dry=True)
            return rn

        def put(d, f, text):
            with open(os.path.join(d, f), "w", encoding="utf-8") as fh:
                fh.write(text)

        rn = broken_copy("unknown key in settings.json", lambda d: put(d, "settings.json", json.dumps({"behavior.nope": 1})), "not a setting")
        check("run() refuses a pack that fails check()", not rn["ok"] and rn["argv"] is None)
        broken_copy("settings.json not from the spec", lambda d: put(d, "settings.json", json.dumps(dict(fx["settings"], **{"limits.max_calls": 7}))),
                    "differs from what harness.yaml asks for")
        broken_copy("a weakened check in settings.json", lambda d: put(d, "settings.json", json.dumps(dict(fx["settings"], **{"behavior.fake_code": "off"}))),
                    "turns off a check")
        broken_copy("flow.yaml with an unknown gate", lambda d: put(d, "flow.yaml", "id: local_fixer\ntitle: x\nsteps:\n  - id: a\n    kind: code\n    gate: made_up\n"),
                    "unknown gate")
        broken_copy("flow.yaml under another id", lambda d: put(d, "flow.yaml", "id: other\ntitle: x\nsteps:\n  - id: a\n    kind: code\n    gate: tests_pass\n"),
                    "is not the pack's name")
        broken_copy("README.md missing", lambda d: os.remove(os.path.join(d, "README.md")), "README.md is missing")
        broken_copy("harness.yaml with a bad gate", lambda d: put(d, "harness.yaml", open(os.path.join(d, "harness.yaml"), encoding="utf-8").read()
                                                                 .replace("gates: []", "gates:\n  - \"no_such_gate\"")), "unknown check")
        broken_copy("harness.yaml with a made-up part", lambda d: put(d, "harness.yaml", open(os.path.join(d, "harness.yaml"), encoding="utf-8").read()
                                                                     + "rocket: true\n"), "unknown part")
        ft_dir = os.path.join(out_dir, "claude_plans_feature_team")
        d = os.path.join(tmp, "broken", "fleet_overlap")
        shutil.copytree(ft_dir, d)
        fj = json.load(open(os.path.join(d, "fleet.json"), encoding="utf-8"))
        fj["plan"].append(dict(fj["plan"][0], name="twin"))
        put(d, "fleet.json", json.dumps(fj))
        ck = H.check(d)
        check("check() refuses a fleet.json where two agents own one file", not ck["ok"] and any("both own" in p for p in ck["problems"]), ck["problems"])
        check("check() of a missing folder says what is missing", not H.check(os.path.join(tmp, "nowhere"))["ok"])

        # ---- CLI
        check("CLI: parts", H.main(["parts"]) == 0)
        check("CLI: check a shipped pack", H.main(["check", "local_fixer"]) == 0)
        check("CLI: run --dry prints and runs nothing", H.main(["run", fixer_dir, "fix it", "--dry"]) == 0)
        spec_file = os.path.join(tmp, "spec.json")
        with open(spec_file, "w", encoding="utf-8") as fh:
            json.dump(dict(base, name="from_file"), fh)
        prev = H.PACKS_DIR
        H.PACKS_DIR = os.path.join(tmp, "cli_packs")
        try:
            check("CLI: compose --spec writes a pack", H.main(["compose", "--spec", spec_file]) == 0
                  and H.check(os.path.join(H.PACKS_DIR, "from_file"))["ok"])
            check("CLI: compose --spec from a pack's own harness.yaml", H.main(["compose", "--spec", os.path.join(fixer_dir, "harness.yaml"), "--force"]) == 0)
        finally:
            H.PACKS_DIR = prev
    except Exception as e:
        import traceback
        traceback.print_exc()
        check(f"no crash ({type(e).__name__}: {e})", False)
    finally:
        for k, v in (("ARCHITECT_DATA_DIR", old_dd), ("ARCHITECT_NO_CACHE", old_nc)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    n, ok = len(results), sum(results)
    print(f"\n  {'ALL ' + str(n) + ' HARNESS CHECKS PASSED' if ok == n else str(n - ok) + ' of ' + str(n) + ' HARNESS CHECKS FAILED'}\n")
    return 0 if ok == n else 1


if __name__ == "__main__":
    sys.exit(run())
