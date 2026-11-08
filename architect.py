"""Architect: design, build, fix, refactor, secure, harden and audit software end to end with local models (Ollama).

    python architect.py run "Add rate limiting to the login API"              # router picks the flow
    python architect.py run "Design a booking service" --flow design --root path/to/project
    python architect.py run "Fix: totals are off by one" --flow bugfix --review auto
    python architect.py run --flow new --template fastapi_rest --name my-api --addons ci,docker --root ./my-api
    python architect.py run "Harden this image" --flow config --review auto
    python architect.py run "" --flow hunt --autofix                          # find + fix real bugs, no goal needed
    python architect.py flows                     list the flows and their steps
    python architect.py templates                 list the starter templates
    python architect.py route "Refactor the db layer"      show which flow would be picked, and why
    python architect.py undo --root path/to/project        restore the files the last run changed
    python architect.py portfolio               list your apps (portfolio.json);  run ... --app NAME
    python architect.py show-config              the settings in effect (after --set overrides)
    python architect.py selftest                 test everything against a fake model (no model needed)
    python architect.py selftest --suite all      also run the pro, security and hub-API self-tests
    python architect.py self-improve              propose + diff-review real changes to the Architect's OWN code (any flow; --root overrides)

The model for each job (router, planner, architect, coders, evaluator, reviewer, security_engineer, ...) is set
in .arch/config.json (or the project-local override), most easily from the hub: python model_hub.py
"""
import argparse
import json
import re
import os
import sys

import arch_core as core
import arch_flows as flows
import arch_roles
import arch_runner as runner
import arch_templates as templates

HERE = os.path.dirname(os.path.abspath(__file__))
PORTFOLIO_FILE = os.path.join(core.HOME, "portfolio.json")


def load_portfolio():
    p = core.load_json(PORTFOLIO_FILE)
    apps = p.get("apps") if isinstance(p.get("apps"), list) else []
    return [a for a in apps if isinstance(a, dict) and a.get("name") and a.get("path")]


def find_app(name):
    for a in load_portfolio():
        if a["name"].lower() == name.lower():
            return a
    raise SystemExit(f"No app named '{name}' in portfolio.json. Known: {', '.join(a['name'] for a in load_portfolio()) or '(none)'}")


def _set_path(d, dotted, value):
    """--set behavior.git=commit -> d['behavior']['git'] = 'commit' (typed: true/false/int/float/json, else string)."""
    parts = dotted.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    key = parts[-1]
    if value.lower() in ("true", "false"):
        parsed = value.lower() == "true"
    else:
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = value
    d[key] = parsed


def build_engine(args, goal, flow_id, persist=True):
    root, overlay = args.root, {}
    if args.app:
        app = find_app(args.app)
        root = app["path"]
        if app.get("test_command"):
            overlay = {"tests": {"command": app["test_command"]}}
    root = os.path.abspath(root or os.getcwd())
    os.makedirs(root, exist_ok=True)
    cfg = core.deep_merge(core.load_config(root), overlay)
    for item in getattr(args, "role_model", None) or []:
        if "=" in item:
            r, m = item.split("=", 1)
            if r in arch_roles.ROLES:
                cfg["roles"][r] = m
    if getattr(args, "model", None):
        cfg["default_model"] = args.model
    if getattr(args, "max_revisions", None) is not None:
        cfg["limits"]["max_revisions"] = args.max_revisions
    if getattr(args, "best_of", None):
        cfg["behavior"]["best_of_n"] = args.best_of
    for kv in getattr(args, "set", None) or []:
        if "=" not in kv:
            raise SystemExit(f"--set needs key.path=value, got: {kv}")
        k, v = kv.split("=", 1)
        _set_path(cfg, k, v)
    extra = {"say": args.say} if getattr(args, "say", None) else {}      # an embedding app supplies its own progress sink
    return core.Engine(root, flow_id, goal, cfg, review=args.review, persist=persist, **extra)


def _flow_params(args):
    p = {}
    if getattr(args, "recipe", None):
        p["recipe"] = args.recipe
    if getattr(args, "domains", None):
        p["domains"] = [d for d in args.domains.split(",") if d in flows.DOMAINS]
    if getattr(args, "template", None):
        p["template"] = args.template
    if getattr(args, "name", None):
        p["name"] = args.name
    if getattr(args, "addons", None):
        p["addons"] = [a for a in args.addons.split(",") if a]
    if getattr(args, "no_build", False):
        p["then_build"] = False
    if getattr(args, "autofix", False):
        p["autofix"] = True
    if getattr(args, "repro", None):
        p["repro"] = args.repro
    return p


def cmd_run(args):
    goal = (args.goal or "").strip()
    forced = None if args.flow == "auto" else args.flow
    if goal.startswith("/"):                       # a saved command: it expands to words and then takes exactly this same path
        import arch_commands
        root_for_cmd = os.path.abspath(args.root or os.getcwd())
        ex = arch_commands.expand(goal, root_for_cmd)
        if not ex["ok"]:
            hint = ("  Did you mean: " + ", ".join("/" + x for x in ex["suggestions"])) if ex.get("suggestions") else ""
            raise SystemExit(ex["why"] + hint + "\nSee them all with:  python architect.py commands")
        print(f"[/{ex['name']}] {ex['description']}")
        goal = ex["goal"]
        forced = forced or (ex["flow"] or None)
    if not goal and forced not in ("new", "hunt", "test", "optimize", "review"):
        raise SystemExit("Give a goal in quotes (or pick --flow new/hunt/test/optimize/review, which can run without one).")
    probe = build_engine(args, goal, "route", persist=False)
    try:
        flow_id, doms, why = flows.route(probe, goal, forced)
    except core.LLMError as e:
        raise SystemExit(str(e))
    print(f"[route] flow={flow_id}  domains={','.join(doms)}  ({why})")
    e = build_engine(args, goal, flow_id)
    params = _flow_params(args)
    if "domains" not in params and doms:
        params["domains"] = doms
    if getattr(args, "stages", None):
        params["stages"] = args.stages
    out = flows.run_flow(e, flow_id, params)
    print(f"\n[done] {'OK' if out.get('ok') else 'NOT COMPLETE'}   report: {os.path.join(e.run_dir, 'report.md')}")
    return 0 if out.get("ok") else 1


def cmd_self_improve(args):
    """Improves the Architect's own code WITHOUT editing the running copy: a candidate copy is worked on with any flow, must pass the gates
    (protected files, compile, no new fake code, its own self-test, at least as many checks as now), and is promoted only after you confirm.
    With --root PATH it behaves like a plain `run` on that folder (the older behaviour)."""
    if args.root:
        return cmd_run(args)
    import arch_selfimprove as si
    goal = (args.goal or "").strip()
    if args.goal_opt or args.rounds or args.fake or args.resume or not goal:     # the iterative squad loop (docs/self-improvement.md)
        out = si.loop((args.goal_opt or goal or "").strip() or None, rounds=args.rounds or 3, fake=args.fake, yes=args.yes,
                      allow_gate_edits=args.allow_gate_edits, suite="core" if args.suite == "all" and args.fake else args.suite,
                      full=True if args.full_gates else None, resume=args.resume)
        return 0 if out.get("status") in ("passed", "promoted") else 1
    extra = []                                    # the run options you gave are passed through to the flow (they were silently dropped)
    if getattr(args, "model", None):
        extra += ["--model", args.model]
    for rm in getattr(args, "role_model", None) or []:
        extra += ["--role-model", rm]
    for kv in getattr(args, "set", None) or []:
        extra += ["--set", kv]
    for opt in ("max_revisions", "best_of"):
        if getattr(args, opt, None) is not None:
            extra += ["--" + opt.replace("_", "-"), str(getattr(args, opt))]
    si.RUN_EXTRA[:] = extra
    out = si.improve(goal, flow=args.flow, review=args.review, yes=args.yes, suite=args.suite, allow_safety=args.allow_safety_edits, keep=args.keep)
    return 0 if out.get("ok") else 1


def cmd_self_verify(args):
    import arch_selfimprove as si
    return si.main(["verify"] + (["--write"] if args.write else []))


def cmd_self_rollback(args):
    import arch_selfimprove as si
    return si.main(["rollback"])


def cmd_self_history(args):
    import arch_selfimprove as si
    return si.main(["history"])


def cmd_flows(args):
    if getattr(args, "validate", False):
        import arch_dataflow as adf
        import os as _os
        bad = 0
        for f in sorted(_os.listdir(adf.FLOWS_DIR)) if _os.path.isdir(adf.FLOWS_DIR) else []:
            if not f.endswith(".yaml"):
                continue
            try:
                probs = adf.validate(adf.load_yaml_file(_os.path.join(adf.FLOWS_DIR, f)))
            except Exception as ex:
                probs = [str(ex)]
            print(f"  {'ok  ' if not probs else 'FAIL'}  {f}" + ("" if not probs else "\n        " + "\n        ".join(probs)))
            bad += bool(probs)
        print((f"\n{bad} flow file(s) with problems." if bad else "\nEvery flow file is valid."))
        return 1 if bad else 0
    return _cmd_flows_list(args)


def _cmd_flows_list(args):
    for f in flows.FLOWS.values():
        print(f"\n{f.id}: {f.title}\n  {f.blurb}")
        for s in f.steps:
            print(f"    - {s['title']}  [{s['kind']}{', ' + s['role'] if s['role'] != '-' else ''}]")
    return 0


def cmd_templates(args):
    for t in templates.listing():
        print(f"\n{t['id']}: {t['title']}  [{t['stack']}]\n  {t['blurb']}\n  domains: {', '.join(t['domains'])}   run: {t['run']}")
        if t.get("needs"):
            print(f"  needs: pip install {' '.join(t['needs'])}")
    print("\naddons: " + ", ".join(templates.ADDONS))
    return 0


def cmd_route(args):
    e = build_engine(args, args.goal, "route", persist=False)
    fid, doms, why = flows.route(e, args.goal)
    print(f"{fid}  domains={','.join(doms)}  ({why})")
    return 0


def cmd_undo(args):
    root = os.path.abspath(args.root or os.getcwd())
    run = core.latest_run(root)
    want = str(getattr(args, "run", "") or "").strip()
    if want:
        import re as _re
        cand = os.path.join(root, ".arch", "runs", want)
        if not _re.fullmatch(r"(?!\.+$)[A-Za-z0-9._-]{1,80}", want) or not os.path.isdir(cand):
            print("No such run:", want)
            return 1
        run = cand
    if not run:
        print("No runs found in", root)
        return 1
    n = core.undo_run(root, run)
    print(f"Restored/removed {n} file(s) from {os.path.basename(run)}")
    return 0


def cmd_portfolio(args):
    apps = load_portfolio()
    if not apps:
        print("portfolio.json has no apps. Example:\n" + json.dumps({"apps": [{"name": "shop-api", "path": "C:/code/shop-api", "stack": "python/fastapi", "test_command": "{python} -m pytest -q",
                                                                            "interfaces": ["REST api/openapi.json"], "notes": "owns orders"}]}, indent=2))
        return 0
    for a in apps:
        print(f"{a['name']:<20} {a['path']}  [{a.get('stack', '')}]")
    return 0


def cmd_show(args):
    cfg = core.load_config(os.path.abspath(args.root or os.getcwd()))
    for kv in getattr(args, "set", None) or []:
        if "=" in kv:
            k, v = kv.split("=", 1)
            _set_path(cfg, k, v)
    print(json.dumps(cfg, indent=2))
    return 0


def cmd_serve(args):
    root = os.path.abspath(args.root or os.getcwd())
    if args.action == "start":
        out = runner.start(root, args.run_cmd, args.port, network=args.network)
    elif args.action == "stop":
        out = runner.stop(root)
    elif args.action == "status":
        out = runner.health(root)
    else:
        print(runner.logs(root, args.tail))
        return 0
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok", True) else 1


def cmd_doctor(args):
    import arch_doctor
    argv = ["--root", os.path.abspath(args.root or os.getcwd()), "--port", str(args.port)]
    if getattr(args, "fix", False):
        argv.append("--fix")
    return arch_doctor.main(argv)


def cmd_assess(args):
    import arch_assess
    return arch_assess.main([])


def cmd_advise(args):
    import arch_advisor
    return arch_advisor.main(["--root", os.path.abspath(args.root or os.getcwd())])


def cmd_fakecode(args):
    import arch_fakecode
    argv = ["--root", os.path.abspath(args.root or os.getcwd())]
    if args.json:
        argv.append("--json")
    if args.all:
        argv.append("--all")
    return arch_fakecode.main(argv)


def cmd_tune(args):
    import arch_autotune
    if args.undo:
        try:
            arch_autotune.undo_last()
        except arch_autotune.TuneError as e:
            print(f"[tune] {e}")
            return 1
        return 0
    try:
        arch_autotune.tune(models=args.model or None, probe=not args.no_probe, apply=not args.no_apply, deep=args.deep)
    except arch_autotune.TuneError as e:
        print(f"[tune] {e}")
        return 1
    return 0


def cmd_hosts(args):
    """Other machines of yours running Ollama: list/add/remove, check them, route roles, benchmark, and a plan."""
    import arch_hosts as H
    import arch_autotune
    import model_hub as mh
    arch_autotune._wire()
    cfg = mh.read_config()
    prev_hosts = json.dumps([cfg.get("hosts"), cfg.get("role_hosts"), cfg.get("roles")], sort_keys=True)
    try:
        for spec in args.add or []:
            name, _, addr = spec.partition("=")
            if not addr or not re.match(r"^[A-Za-z0-9_.-]+$", name):
                print("Use --add name=address, for example --add desktop=192.168.1.20")
                return 2
            cfg.setdefault("hosts", {})[name] = H.normalize(addr)
            print(f"added {name} = {cfg['hosts'][name]}")
        for name in args.remove or []:
            cfg.get("hosts", {}).pop(name, None)
            for r_ in [r for r, h_ in (cfg.get("role_hosts") or {}).items() if h_ == name]:
                H.unroute(cfg, r_)
                print(f"role {r_} is back on this computer")
            print(f"removed {name}")
        for spec in args.route or []:                         # role=host or role=host:model
            role, _, rest = spec.partition("=")
            host, _, model = rest.partition(":")
            if role not in core.arch_roles.ROLES:
                print(f"'{role}' is not a role. Roles: {', '.join(core.arch_roles.ROLES)}")
                return 2
            H.route(cfg, role, host, model or None)
            print(f"{role} -> {host}" + (f" using {model}" if model else ""))
        for role in args.unroute or []:
            H.unroute(cfg, role)
            print(f"{role} is back on this computer")
    except ValueError as e:
        print(f"[hosts] {e}")
        return 2
    if prev_hosts != json.dumps([cfg.get("hosts"), cfg.get("role_hosts"), cfg.get("roles")], sort_keys=True):
        mh.write_config(cfg)
    probes = {n: H.probe(u) for n, u in H.hosts_of(cfg).items()}
    if args.plan or args.apply_plan:
        try:
            local = [r["name"] for r in mh.model_list({}, "f16", 192.0, 50.0)]
        except Exception:
            local = []
        sug = H.plan(cfg, local, probes)
        for s_ in sug:
            print(f"  suggest: {s_['role']} -> {s_['host']} ({s_['model']})  {s_['why']}")
        if not sug:
            print("  no role would clearly gain from another machine" if probes else "  no hosts added yet (use --add name=address)")
        if args.apply_plan:
            for s_ in sug:
                H.route(cfg, s_["role"], s_["host"], s_["model"])
            if sug:
                mh.write_config(cfg)
                print(f"routed {len(sug)} role(s). `hosts --unroute ROLE` puts any of them back.")
    if args.bench:
        for n, p in probes.items():
            if p.get("ok") and p["models"]:
                top = max(p["models"], key=lambda m: m["size_gb"])["name"]
                try:
                    H.bench(H.hosts_of(cfg)[n], top)
                except core.LLMError as e:
                    print(f"    {n}: benchmark failed ({str(e)[:100]})")
    if args.json:
        print(json.dumps({"hosts": H.hosts_of(cfg), "role_hosts": cfg.get("role_hosts") or {}, "probes": probes}, indent=1))
        return 0
    if not H.hosts_of(cfg):
        print("No other machines added. Add one that runs Ollama:  python architect.py hosts --add desktop=192.168.1.20")
        print("(On that machine start Ollama with OLLAMA_HOST=0.0.0.0 so it accepts connections from your network. Only do this on a network you trust.)")
        return 0
    for n, u in H.hosts_of(cfg).items():
        p = probes[n]
        roles = [r for r, h_ in (cfg.get("role_hosts") or {}).items() if h_ == n]
        if p.get("ok"):
            print(f"  {n:12} {u}  up, {p['ms']} ms, {len(p['models'])} model(s): {', '.join(m['name'] for m in p['models'][:4])}" + (f"   roles: {', '.join(roles)}" if roles else ""))
        else:
            print(f"  {n:12} {u}  DOWN: {p['error']}" + (f"   roles {', '.join(roles)} fall back to this computer" if roles else ""))
    return 0


def cmd_livetest(args):
    import arch_livetest
    import arch_autotune
    arch_autotune._wire()
    import arch_hosts as H
    pr = H.probe(core.ollama_host())
    if not pr.get("ok") or not pr.get("models"):
        print(f"The live test needs Ollama with at least one chat model. " + (f"Ollama is not reachable at {core.ollama_host()}: start the Ollama app." if not pr.get("ok") else "Ollama is running but has no models: run `python architect.py ready` first."))
        return 1
    out = arch_livetest.run(only=(args.only.split(",") if args.only else None), quick=args.quick)
    return 0 if out["passed"] == out["total"] else 1


def cmd_shortcut(args):
    import arch_launch
    p = arch_launch.make_shortcut()
    if p:
        print(f"Created {p} - double-click it to open Architect.")
        return 0
    print("Could not create a desktop shortcut (no Desktop folder, or a different file with that name is there). START_HUB.bat in this folder does the same job.")
    return 1


def cmd_status(args):
    """One screen: is Ollama up, which models, power, other machines, last run, anything to resume."""
    import arch_autotune
    import arch_power
    import arch_hosts as H
    import arch_resume
    import model_hub as mh
    arch_autotune._wire()
    cfg = mh.read_config()
    pr = H.probe(core.ollama_host())
    if pr.get("ok"):
        print(f"Ollama:   up at {core.ollama_host()}, {len(pr['models'])} model(s)")
    else:
        print(f"Ollama:   NOT reachable at {core.ollama_host()}  ({pr['error']}). Start the Ollama app.")
    pw = arch_power.battery()
    print("Power:    " + (("laptop, " + ("on battery" if pw["on_battery"] else "plugged in") + (f" ({pw['pct']}%)" if pw.get("pct") is not None else "")) if pw["present"] else "desktop / no battery found"))
    prof = arch_autotune.load_profile()
    print("Tuned:    " + (f"{prof.get('tier_label')} harness, builder {prof.get('builder')}" if prof else "not yet (python architect.py ready)"))
    hs = H.hosts_of(cfg)
    if hs:
        up = sum(1 for u in hs.values() if H.probe(u).get("ok"))
        print(f"Machines: {up} of {len(hs)} other machine(s) reachable; {len(cfg.get('role_hosts') or {})} role(s) routed")
    else:
        print("Machines: none added (hosts --add name=address to offload roles)")
    root = os.path.abspath(args.root or os.getcwd())
    runs = arch_resume.list_runs(root)
    if runs:
        st = runs[-1]
        print(f"Last run: {st['flow']} {st['status']} - {st.get('goal', '')[:60]}")
    rs = arch_resume.resumable(root)
    if rs:
        print(f"Resume:   python architect.py resume   ({rs['flow']}, {rs['status']})")
    return 0


def cmd_resume(args):
    import arch_resume
    root = os.path.abspath(args.root or os.getcwd())
    if args.list:
        runs = arch_resume.list_runs(root)
        for st in runs[-15:]:
            print(f"{st['run_id']}  {st['status']:10} {st['flow']:9} {st.get('goal', '')[:60]}" + (f"   (resumed by {st['resumed_by']})" if st.get("resumed_by") else ""))
        if not runs:
            print("No runs recorded in this project yet.")
        return 0
    st = arch_resume.resumable(root)
    if not st:
        print("Nothing to resume: every run in this project finished, or was already resumed. `resume --list` shows them.")
        return 1
    print(f"[resume] {st['flow']}: {st.get('goal', '')[:70]}   (was {st['status']}, started {st['run_id']})")
    args.root, args.flow = root, st["flow"]
    if args.review == "ask" and st.get("review") in ("auto", "dry"):
        args.review = st["review"]
    e = build_engine(args, st.get("goal", ""), st["flow"])
    arch_resume.mark_resumed(st, e.run_id)
    out = flows.run_flow(e, st["flow"], arch_resume.resume_params(st))
    print(f"\n[done] {'OK' if out.get('ok') else 'NOT COMPLETE'}   report: {os.path.join(e.run_dir, 'report.md')}")
    return 0 if out.get("ok") else 1


def cmd_ci(args):
    import arch_ci
    argv = ["--root", args.root or "."]
    for flag, on in (("--run-local", args.run_local), ("--init", args.init), ("--release", args.release), ("--json", args.json)):
        if on:
            argv.append(flag)
    return arch_ci.main(argv)


def cmd_optimize(args):
    import arch_autotune
    import arch_hw
    import arch_modelopt as mo
    import model_hub as mh
    arch_autotune._wire()
    try:
        if args.undo:
            mo.undo()
            return 0
        hw = arch_hw.detect()
        rows = mh.model_list(hw, "f16", 192.0, 50.0)
        if args.apply:
            mo.apply(rows, hw, models=args.model or None)
            return 0
        p = mo.plan(hw, rows, mh.read_config(), arch_autotune.load_profile())
    except (arch_autotune.TuneError, mh.OllamaError) as e:
        print(f"[optimize] {getattr(e, 'msg', e)}")
        return 1
    intel = {}
    if getattr(args, "online", False) and not (core.load_config().get("online") or {}).get("enabled"):
        print("(--online ignored: online.enabled is off, so no Hugging Face lookup was made. Settings > Online turns it on.)")
        args.online = False
    if getattr(args, "online", False):
        import arch_intel
        intel = arch_intel.lookup([m["name"] for m in p["models"]])
        for m in p["models"]:
            if m["name"] in intel:
                m["notes"] = list(m["notes"]) + arch_intel.notes_for(intel[m["name"]])
        if not intel:
            print("(--online: none of your models come from hf.co, so there was nothing to look up)")
    if args.json:
        print(json.dumps(p, indent=1))
        return 0
    print("Per-model settings (nothing is written until you run `optimize --apply`):")
    for m in p["models"]:
        print(f"\n  {m['name']}   [{m['family']}, {m['tier']} harness]")
        for k_, v in m["proposed"].items():
            print(f"    would set {k_} = {v}")
        for k_, v in m["kept"].items():
            print(f"    keeps your sampling.{k_} = {v}")
        for src, t in m["notes"]:
            print(f"    - ({src}) {t}")
        if m["moe_tip"]:
            print("    * " + m["moe_tip"])
    print("\nOllama server settings (set in the server's environment; this tool cannot change them):")
    for s_ in p["server"]:
        print(f"  {s_['env']}={s_['value']}   ({s_['source']}) {s_['why']}")
    print()
    for ln in p["server_lines"]:
        print("    " + ln)
    print("\nFramework and harness:")
    for a in p["stack"]:
        print(f"  {a['topic']}: {a['advice']}   ({a['source']}) {a['why']}")
    return 0


def cmd_ready(args):
    import arch_ready

    def ask(q):
        try:
            return input(f"{q} [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            return False
    out = arch_ready.ready(os.path.abspath(args.root or os.getcwd()), yes=args.yes, ask=ask, probe=not args.no_probe, proof=not args.no_proof, smoke=not args.no_smoke)
    return 0 if out["ok"] else 1


def cmd_scan(args):
    import arch_scan
    argv = ["--root", os.path.abspath(args.root or os.getcwd())]
    if args.run_tests:
        argv.append("--run-tests")
    if args.json:
        argv.append("--json")
    return arch_scan.main(argv)


def cmd_index(args):
    """The hybrid retrieval index of a project: build/refresh it, show its state, or compare the rankings for one request."""
    import arch_index
    root = os.path.abspath(args.root or os.getcwd())
    cfg = core.load_config(root)
    installed = core.list_models()
    if args.rebuild:
        import shutil
        shutil.rmtree(os.path.join(root, arch_index.INDEX_DIR), ignore_errors=True)      # the old vector store
        import arch_fts
        arch_fts.Index(root).wipe()
    if args.query:
        out = arch_index.compare(root, cfg, installed, core.ollama_host(), args.query, core.read_text, core.list_files, core.rank_files)
        if args.json:
            print(json.dumps(out, indent=2))
            return 0
        print(f"Request: {out['query']}\nIndex: {out['index']}")
        for title, rows in (("keyword ranking (TF-IDF, files)", out["tfidf"]), ("full-text hits (FTS5, functions)", out["fts"]), ("fused order (what the prompt shows first)", out["fused"])):
            print(f"\n{title}:")
            for i, r in enumerate(rows, 1):
                print(f"  {i:2d}. {r}")
            if not rows:
                print("  (none)")
        return 0
    st = arch_index.status(root, cfg, installed)
    if st["state"] == "on" and not args.status_only:
        files = core.list_files(root, cfg)
        r = arch_index.Retriever(root, cfg, installed, core.ollama_host(), core.read_text, say=print)
        r.ensure(files)
        st = arch_index.status(root, cfg, installed)
        st["refresh"] = r.last
    if args.json:
        print(json.dumps(st, indent=2))
        return 0
    print(f"Retrieval: {st['state']}" + (f"  ({st['model']})" if st.get("model") else ""))
    if st.get("exists"):
        print(f"  index: {st['chunks']} chunks over {st['files']} files, {st['bytes'] // 1024} KB in .arch/index.db")
    rf = st.get("refresh") or {}
    if rf:
        print(f"  refresh: {rf['new']} new, {rf['kept']} reused, {rf['dropped']} dropped, {rf['secs']}s" + (f"  [{rf['error']}]" if rf.get("error") else ""))
    return 0 if st["state"] == "on" else 1


def fit_sheet(root=None, kv="f16"):
    """The team's memory fit sheet (arch_chain.fit_sheet) for this machine. -> dict; raises TuneError/OllamaError when Ollama is down."""
    import arch_autotune
    import arch_chain
    import arch_hw
    import arch_tune
    import model_hub as mh
    arch_autotune._wire()
    hw = arch_hw.detect()
    rows = mh.model_list(hw, kv, 192.0, 50.0)
    cfg = core.load_config(root)
    ledger = arch_chain.ledger_swaps(os.path.join(root, ".arch", "runs")) if root else {}
    return arch_chain.fit_sheet(cfg, hw, rows, arch_tune.load_stats(), ledger)


def cmd_backlog(args):
    """The project's backlog: list, add, propose from findings."""
    import arch_autopilot as ap
    root = os.path.abspath(args.root or os.getcwd())
    if args.add:
        it = ap.add(root, args.add, args.flow or "auto")
        print(f"added {it['id']}: {it['goal']}" if it else "already on the backlog")
        return 0
    if args.propose:
        return 0 if ap.propose(root, n=args.n, cfg=core.load_config(root)) else 1
    bl = ap.load(root)
    if args.json:
        print(json.dumps(bl, indent=1))
        return 0
    if not bl["items"]:
        print("Backlog is empty. `backlog --add \"goal\"` or `backlog --propose` (from review/hunt/secure findings).")
        return 0
    for i in bl["items"]:
        print(f"  {i['status']:6s} {i['id']}  {i['goal'][:90]}  [{i['flow']}, {i['source']}]")
    return 0


def cmd_autopilot(args):
    """Work through the backlog unattended, within a budget; optionally propose new items from what each run finds."""
    import arch_autopilot as ap
    root = os.path.abspath(args.root or os.getcwd())
    s = ap.run(root, hours=args.hours, max_items=args.max, review=args.review, do_propose=args.propose, cfg=core.load_config(root))
    return 0 if not s["failed"] else 1


def cmd_dataset(args):
    """Export per-role SFT sets and DPO pairs from this project's (or the portfolio's) marked traces."""
    import arch_dataset as ds
    root = os.path.abspath(args.root or os.getcwd())
    data = ds.build(ds.roots(root, include_portfolio=args.all), roles=set(args.role) if args.role else None, pairs=getattr(args, "pairs", "same-task"))
    out_dir = args.out or os.path.join(core.data_dir(), "datasets")
    summary = ds.write(data, out_dir)
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    st = summary["stats"]
    print(f"Read {st.get('runs', 0)} run(s) of your own work.")
    print(f"  {st.get('sft', 0)} samples the checks accepted        <- what to train on")
    print(f"  {st.get('dpo', 0)} preference pairs (a try that failed, then the one that passed)")
    print(f"  {st.get('distill', 0)} cases a bigger model solved after the worker could not   <- the most useful ones")
    print(f"  {st.get('traj', 0)} tool conversations from start to finish")
    left_out = [(st.get("rejected", 0), "replies the checks rejected (kept only as the losing half of a pair)"),
                (st.get("unverified", 0), "replies whose checks could not run at all, so they teach nothing either way"),
                (st.get("secrets_skipped", 0), "samples left out because something in them looked like a credential"),
                (st.get("too_long", 0), "samples too long to train on a small card"),
                (st.get("duplicates", 0), "exact duplicates"),
                (st.get("unmarked", 0), "calls no check ever labelled"),
                (st.get("cached", 0), "replies reused from the cache (already exported when first produced)"),
                (st.get("pairs_dropped_different_task", 0), "would-be pairs where the two tries were not the same task")]
    shown = [f"  {n} {why}" for n, why in left_out if n]
    if shown:
        print("Left out:")
        print("\n".join(shown))
    if summary.get("per_tier"):
        print("By tier (a tier is what you fine-tune):")
        for tier, n in sorted(summary["per_tier"].items()):
            print(f"  {tier:10s} {n['sft']:5d} samples   {n['dpo']:5d} pairs")
    for role, n in sorted(summary["per_role"].items()):
        print(f"  {role:20s} sft {n['sft']:5d}   dpo {n['dpo']:5d}")
    if summary.get("removed_stale"):
        print(f"Removed {len(summary['removed_stale'])} file(s) from an earlier export that this one did not produce.")
    print(f"Written to {out_dir}")
    if not st.get("sft"):
        print("Nothing to train on yet: use the app on real work, and these files fill up as the checks accept replies.")
    else:
        print("RECIPE.md there has the exact commands, and says whether you have enough samples yet.")
    return 0            # an empty export is a true statement about a new install, not a failure


def cmd_dist(args):
    """Build an offline installer for a hardware profile, or verify one."""
    import arch_dist
    if args.verify:
        r = arch_dist.verify(args.verify)
        if r.get("why"):
            print(r["why"])
            return 1
        if r["ok"]:
            print(f"All {r['checked']} files match their checksums.")
            if r["extra"]:
                print(f"({len(r['extra'])} file(s) were added after it was built, which is normal once it has been used: {', '.join(r['extra'][:4])})")
            return 0
        print(f"Checked {r['checked']} files. This copy is damaged or was changed:")
        for x in r["changed"][:20]:
            print(f"  changed  {x}")
        for x in r["missing"][:20]:
            print(f"  missing  {x}")
        return 1
    import arch_hw
    import arch_server
    profile = args.profile or arch_server.profile_for(arch_hw.detect())
    out = args.out or os.path.join(os.path.dirname(HERE), f"architect-{profile}-installer")
    res = arch_dist.build(profile, out, with_server=args.with_server, with_models=args.with_models, make_zip=args.zip)
    print(f"\nInstaller for the '{profile}' profile: {res['path']}")
    print(f"  {res['app_files']} app files, {res['files']} checksummed files, {round(res['bytes'] / 1024 ** 2, 1)} MB")
    print(f"  model server: {res['server']}")
    if res["models_included"]:
        print("  models included: " + ", ".join(m["model"] for m in res["models_included"]))
    if res["models_to_fetch"]:
        print("  models to install on the new machine (see MODELS.txt): " + ", ".join(m["model"] for m in res["models_to_fetch"]))
    if res.get("zip"):
        print(f"  zip: {res['zip']} ({round(res['zip_bytes'] / 1024 ** 2, 1)} MB)")
    print("\nOn the new machine: run install.bat (Windows) or install.sh. It checks every file before it runs anything.")
    return 0


def cmd_queue(args):
    """The tasks waiting for the supervisor."""
    import arch_supervisor as sv
    cfg = core.load_config()
    smax = int((cfg.get("supervisor") or {}).get("max_attempts", 3))
    smin = float((cfg.get("supervisor") or {}).get("task_minutes", 15))
    if args.action == "add":
        if not args.goal:
            print('Say what the task is:  python architect.py queue add "fix the failing login test"')
            return 1
        jid = sv.add(args.goal, args.root or os.getcwd(), args.flow or "", args.minutes or smin, smax, args.priority or 0)
        print(f"Queued as task {jid}. Run the queue with:  python architect.py supervise")
        return 0
    if args.action == "cancel":
        ok = sv.cancel(args.goal or 0)
        print("Cancelled." if ok else "Only a task that has not started yet can be cancelled.")
        return 0 if ok else 1
    if args.action == "clear":
        print(f"Removed {sv.clear()} finished task(s) from the list.")
        return 0
    rows = sv.listing(limit=args.limit or 30)
    if args.json:
        print(json.dumps({"jobs": rows, "counts": sv.counts(), "spent_today": sv.spent_today()}, indent=2, default=str))
        return 0
    c, s = sv.counts(), sv.spent_today()
    print(f"{c['pending']} waiting, {c['running']} running, {c['done']} done, {c['failed'] + c['dead']} did not pass, {c['not_sure']} not sure."
          f"  Today: {s['runs']} run(s), {s['minutes']} min." + ("  [STOP is on]" if sv.stopped() else ""))
    mark = {"pending": "·", "running": "▸", "done": "✓", "failed": "×", "dead": "×", "not_sure": "?", "cancelled": "–"}
    for r in rows:
        line = f"  {mark.get(r['state'], ' ')} {r['id']:>4}  {r['state']:<9} {(r['flow'] or 'auto'):<9} {r['goal'][:64]}"
        print(line)
        if r.get("note"):
            print(f"{'':22}{r['note'][:96]}")
        elif r.get("why") and r["state"] not in ("pending", "done"):
            print(f"{'':22}{r['why'][:96]}")
    if not rows:
        print('  Nothing queued.  Add a task:  python architect.py queue add "describe it"')
    return 0


def cmd_supervise(args):
    """Work through the queue on its own, within the day's limits."""
    import arch_supervisor as sv
    if args.stop:
        sv.stop(True)
        print("Stopped. Nothing new will start, and a running flow stops between steps. Resume with:  python architect.py supervise --resume")
        return 0
    if args.resume:
        sv.stop(False)
        print("The stop is lifted.")
        if not args.hours:
            return 0
    res = sv.supervise(hours=args.hours or 1.0, max_tasks=args.max or 20)
    if args.json:
        print(json.dumps(res, indent=2, default=str))
    return 0


def cmd_receipt(args):
    """One run as a single HTML file you can open anywhere, forever."""
    import arch_receipt
    root = os.path.abspath(args.root or os.getcwd())
    run_dir = os.path.join(root, ".arch", "runs", args.run) if args.run else arch_receipt.latest_run(root)
    if not run_dir or not os.path.isdir(run_dir):
        print("No run to write a receipt for yet." if not args.run else f"There is no run called {args.run} here.")
        return 1
    dest = arch_receipt.write(run_dir, root, args.out)
    print(f"Wrote {dest}")
    print("It is one file with nothing loaded from the internet: open it in any browser, now or in five years.")
    if args.open:
        import webbrowser
        webbrowser.open("file://" + os.path.abspath(dest).replace("\\", "/"))
    return 0


def cmd_port(args):
    """Fit the team to this computer, and move your setup between computers."""
    import arch_port
    root = os.path.abspath(args.root or os.getcwd())
    if args.pack:
        res = arch_port.pack(args.pack, root)
        print(f"Packed {round(res['bytes'] / 1024)} KB into {res['path']}\n")
        print("Taken with you:")
        for a in res["added"]:
            print(f"  {a}")
        if res["left_behind"]:
            print("\nDeliberately left behind, because it describes this computer and would be wrong on another:")
            for a in res["left_behind"]:
                print(f"  {a}")
        print("\nOn the other machine:  python architect.py port --adopt " + os.path.basename(args.pack))
        return 0
    if args.adopt:
        if not os.path.isfile(args.adopt):
            print(f"There is no file at {args.adopt}")
            return 1
        res = arch_port.adopt(args.adopt, root)
        print(f"Restored {len(res['restored'])} item(s) from {args.adopt}.")
        print(res["note"] + "\n")
    plan = arch_port.pick_tiers(say=(lambda m: None) if args.json else print)
    if args.json:
        print(json.dumps(plan, indent=2))
        return 0
    print(arch_port.render(plan))
    if args.apply or args.adopt:
        ch = arch_port.apply_plan(plan)
        if ch["changed"]:
            print("\nSaved. What changed:")
            for tier, (was, now) in ch["changed"].items():
                print(f"  {tier:<7} {was or '(none)'}  ->  {now or '(none)'}")
            print("\nMeasure it with:  python architect.py eval --quick")
        else:
            print("\nNothing changed: this is already what is set.")
    else:
        print("\nThis changed nothing. To use these choices:  python architect.py port --apply")
    return 0


def cmd_commands(args):
    """The saved requests you can invoke by name."""
    import arch_commands
    root = os.path.abspath(args.root or os.getcwd())
    rows = arch_commands.summary(root)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if args.show:
        c = arch_commands.load_all(root).get(args.show.lstrip("/").lower())
        if not c:
            print(f"There is no /{args.show.lstrip('/')} here.")
            return 1
        print(f"/{c['name']}  ({c['scope']}{', runs the ' + c['flow'] + ' flow' if c['flow'] else ''})\n{c['path']}\n")
        print(c["body"])
        return 0
    if not rows:
        print("No saved commands yet.")
    else:
        print("Saved requests. Type one in the console, or run it here as:  python architect.py run \"/name what to work on\"\n")
        for r in rows:
            where = "" if r["scope"] == "shipped" else "  [this project]"
            print(f"  /{r['name']:<10} {r['description']}{where}")
            if r["needs_args"] and r["args_hint"]:
                print(f"{'':14} takes: {r['args_hint']}")
    print(f"\nAdd your own: a Markdown file in {os.path.join(root, arch_commands.PROJECT_REL)}")
    return 0


def cmd_plugins(args):
    """Plugins: bundles of agents, workflows, skills, commands and hooks, switched on/off and trusted as one unit."""
    import arch_plugins as P
    root = os.path.abspath(args.root or os.getcwd())
    act, x = args.action, args.target
    need = {"show", "enable", "disable", "trust", "untrust", "install", "remove", "new", "pack"}
    if act in need and not x:
        print(f"plugins {act} needs a name" + (" or a path" if act == "install" else ""))
        return 2
    try:
        if act == "list":
            rows = P.list(root)
            if args.json:
                print(json.dumps(rows, indent=2))
                return 0
            if not rows:
                print("No plugins. Make one with:  python architect.py plugins new NAME")
            for r in rows:
                t = {True: "trusted", False: "not trusted", None: "trust unknown"}[r["trusted"]]
                pv = r["provides"]
                counts = ", ".join(f"{len(pv[k])} {k}" for k in ("agents", "workflows", "skills", "commands", "hooks") if pv[k])
                print(f"  {'[on] ' if r['enabled'] else '[off]'} {r['name']} {r['version']}  ({r['scope']}; "
                      f"{t if r['needs_trust'] else 'no trust needed'})  {counts}")
                print(f"        {r['description']}")
                for pr in r["problems"]:
                    print(f"        problem: {pr}")
            return 0
        if act == "show":
            r = P.show(x, root)
            if not r:
                print(f"no plugin {x}")
                return 1
            print(json.dumps(r, indent=2))
            return 0
        if act in ("enable", "disable"):
            ok = (P.enable if act == "enable" else P.disable)(x, root)
            print(f"{x}: {act}d" if ok else f"no plugin {x} (or the state could not be saved)")
            return 0 if ok else 1
        if act in ("trust", "untrust"):
            r = P.show(x, root)
            if not r:
                print(f"no plugin {x}")
                return 1
            if act == "trust":
                print(f"Approving {x} {r['version']} from {r['dir']}. These commands may run from now on:")
                for h in r["would_run"]:
                    print(f"  {h['on']:<14} {h['run']}" + ("   [blocking]" if h["blocking"] else ""))
                for a in r["code_agents"]:
                    print(f"  code agent     {a}")
                if not (r["would_run"] or r["code_agents"]):
                    print("  (nothing executable: this plugin needs no trust)")
            ok = P.trust(x, root, yes=(act == "trust"))
            print(("Approved. Editing any file of the plugin undoes this." if act == "trust" else "Approval removed.") if ok
                  else "Could not record that.")
            return 0 if ok else 1
        if act == "install":
            print(f"installed {P.install(x)} (off: enable it with  plugins enable NAME)")
            return 0
        if act == "remove":
            ok = P.remove(x)
            print(f"removed {x}" if ok else f"{x} is not a plugin installed on this machine")
            return 0 if ok else 1
        if act == "new":
            print(f"created {P.new(x)}")
            return 0
        if act == "pack":
            print(f"wrote {P.pack(x, args.out, root)}")
            return 0
        if act == "validate":
            res = P.validate(root)
            bad = {k: v for k, v in res.items() if v}
            for k, v in res.items():
                print(f"  {'ok  ' if not v else 'FAIL'} {k}" + "".join("\n        " + p for p in v))
            return 1 if bad else 0
    except ValueError as e:
        print(str(e))
        return 1
    print(f"unknown action {act}")
    return 2


def cmd_hooks(args):
    """Your own commands, run at fixed points in a run."""
    import arch_hooks
    root = os.path.abspath(args.root or os.getcwd())
    cfg = core.load_config(root)
    if args.example:
        print(json.dumps(arch_hooks.EXAMPLE, indent=2))
        print(f"\nSave that as {arch_hooks.project_file(root)} (this project) or {arch_hooks.machine_file()} (everything you do here),")
        print("then approve the project one with:  python architect.py hooks --trust")
        return 0
    summary = arch_hooks.summary(root, cfg)
    if args.trust:
        p = arch_hooks.project_file(root)
        if not os.path.isfile(p):
            print(f"There is no hook file at {p} to approve.")
            return 1
        print(f"These commands will run on this project from now on, from {p}:\n")
        for hk in arch_hooks._read(p):
            print(f"  {hk['on']:<14} {hk['run']}" + ("   [blocking: a failure fails the step]" if hk["blocking"] else ""))
        ok = arch_hooks.trust(p)
        print("\nApproved." if ok else "\nCould not record the approval.")
        print("Editing the file undoes this: an edited file needs approving again.")
        return 0 if ok else 1
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    if summary["active"]:
        print("Running on this project:")
        for hk in summary["active"]:
            print(f"  {hk['on']:<14} {hk['run']}"
                  + (f"   (only {hk['match']})" if hk["match"] else "")
                  + ("   [blocking]" if hk["blocking"] else "") + f"   -- from {hk['where']}")
    else:
        print("No hooks are set up. See an example with:  python architect.py hooks --example")
    for w in summary["waiting_for_approval"]:
        print(f"\nWaiting for your approval: {w['path']} ({w['count']} command(s))")
        for c in w["commands"]:
            print(f"    {c}")
        print("  These came with the project, so they do not run until you have read them.")
        print("  Approve with:  python architect.py hooks --trust")
    return 0


def cmd_promote_model(args):
    """Switch a tier over to a different model, but only if the fixed task set says it is no worse."""
    import arch_eval
    tier, model = args.tier, args.model
    cfg = core.load_config()
    current = (cfg.get("tiers") or {}).get(tier) or "(none)"
    if model == current:
        print(f"The {tier} tier is already {model}.")
        return 0
    installed = set(core.list_models() or [])
    if installed and model not in installed and not model.lower().endswith(".gguf"):
        print(f"'{model}' is not installed here. Installed: {', '.join(sorted(installed)[:12])}")
        return 1
    base = arch_eval.latest(label=args.compare)
    if not base:
        print(f"No saved eval called '{args.compare}' to compare against. Make one first:\n"
              f"  python architect.py eval --quick --label {args.compare}")
        return 1
    cand = arch_eval.latest(label=args.candidate)
    if not cand:
        print(f"No saved eval called '{args.candidate}'. Run the task set with the new model, then try again:\n"
              f"  python architect.py eval --quick --label {args.candidate} --set tiers.{tier}={model}")
        return 1
    cmp = arch_eval.compare(base, cand)
    print(arch_eval.compare_table(cmp) if isinstance(arch_eval.compare_table(cmp), str) else "\n".join(arch_eval.compare_table(cmp)))
    if not cmp["ok"]:
        print(f"\nNot promoting. '{model}' did worse than '{args.compare}' on your own task set:")
        for r in cmp["regressions"]:
            print(f"  - {r}")
        print("A model that only feels better does not get promoted. Fix it, or keep what you have.")
        return 1
    if args.dry_run:
        print(f"\nWould set the {tier} tier to {model} (currently {current}). Nothing changed: this was a dry run.")
        return 0
    import model_hub as mh
    mh.CONFIG_FILE = core.CONFIG_FILE              # the engine's settings file, which is the one that is read
    raw = mh.read_config()
    before = dict(raw.get("tiers") or {})
    raw.setdefault("tiers", {})[tier] = model
    mh.write_config(raw)                       # keeps a .bak, writes atomically, same path the hub and `tune` use
    try:
        os.makedirs(core.data_dir(), exist_ok=True)
        with open(os.path.join(core.data_dir(), f"promote-{tier}.undo.json"), "w", encoding="utf-8") as f:
            json.dump({"tiers": before, "replaced": current, "with": model, "compared_against": args.compare}, f, indent=1)
    except OSError:
        pass
    print(f"\nPromoted: the {tier} tier is now {model} (was {current}).")
    print(f"It measured no worse than '{args.compare}' on every flow your task set covers.")
    print(f"To undo:  python architect.py promote-model --tier {tier} --model {current}")
    return 0


def cmd_fit(args):
    import arch_chain
    import model_hub as mh
    try:
        sheet = fit_sheet(os.path.abspath(args.root) if args.root else None, kv=args.kv)
    except mh.OllamaError as e:
        print(f"Ollama is not reachable: {e.msg}")
        return 2
    if args.json:
        print(json.dumps(sheet, indent=2))
    else:
        print(arch_chain.sheet_text(sheet))
    return 0


def cmd_tasks(args):
    import arch_taskkit as tk
    if args.expand:
        try:
            print(tk.expand(args.expand, " ".join(args.target)))
        except KeyError as e:
            raise SystemExit(str(e).strip("'\""))
        return 0
    for g in tk.GROUPS:
        items = [t for t in tk.all_templates() if t["group"] == g and (not args.group or args.group.lower() in g.lower())]
        if items:
            print(f"\n{g}")
            for t in items:
                print(f"  {t['id']:<20} --flow {t['flow']:<9} {t['title']}")
    print("\nUse one:  python architect.py tasks --expand fix-bug \"totals are off by one\"   (prints the goal to pass to `run`)")
    return 0


def cmd_catalog(args):
    import arch_autotune
    import arch_catalog
    import arch_hw
    if args.verify:
        if not (core.load_config().get("online") or {}).get("enabled"):
            print("catalog --verify needs the online switch: it asks the public Ollama registry. Turn it on with --set online.enabled=true (Settings > Online) if you want that; nothing else changes.")
            return 1
        print("Checking tags against the public Ollama registry (needs internet from this machine):")
        arch_catalog.verify_all()
        return 0
    arch_autotune._wire()
    hw = arch_hw.detect()
    import model_hub as mh
    try:
        rows = mh.model_list(hw, "f16", 192.0, 50.0)
    except mh.OllamaError:
        rows = []
    out = arch_autotune.suggest(hw, rows, mh.read_config(), arch_autotune.load_profile())
    if rows:
        own = [o for o in arch_autotune.owned_models(rows, mh.read_config(), arch_autotune.load_profile())]
        print("Your models (never replaced):  " + ", ".join(f"{o['name']} ({o['eff_b']}B" + (", MoE" if "MoE" in o["tags"] else "") + ")" for o in own))
    print(f"Machine: {out['machine']['vram_gb']} GB VRAM, {out['machine']['ram_gb']} GB RAM, {out['machine']['disk_free_gb']} GB free.  Catalog as of {out['as_of']} (editorial, not a benchmark).")
    if not out["suggestions"]:
        print("Nothing in the catalog is a clear step up that also fits this machine.")
    for s in out["suggestions"]:
        print(f"\n  [{s['slot']}] {s['name']}   {s['size_gb']} GB   {s['pull']}" + ("" if s["verified"] else "   (unverified tag)"))
        for w in s["why"] + s["caveats"]:
            print(f"      - {w}")
    print("\nDownload one with:  python architect.py pull <tag>")
    return 0


def cmd_pull(args):
    import arch_autotune
    import arch_catalog
    arch_autotune._wire()
    e = arch_catalog.lookup(args.tag)
    size = (e or {}).get("size_gb")
    print(f"About to download {args.tag}" + (f" (about {size} GB)" if size else " (size unknown: check the tag first)") + " through your local Ollama.")
    if not args.yes:
        try:
            ans = input("Download it? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("Cancelled; nothing was downloaded.")
            return 1
    return 0 if arch_catalog.pull(args.tag, confirm=True) else 1


def cmd_server(args):
    import arch_server
    return arch_server.main([args.action] + (["--heavy"] if getattr(args, "heavy", False) else []) + (["--yes"] if getattr(args, "yes", False) else []))


def cmd_passport(args):
    import arch_passport
    ps = arch_passport.refresh(names=[args.model] if args.model else None)
    for p in ps.values():
        print("  " + arch_passport.summary_line(p))
        for n in p.get("notes") or []:
            print("      - " + n)
    return 0


def cmd_start(args):
    """Detect the machine, start the resident server when it is installed, and open the hub."""
    import arch_hw
    import arch_server
    hw = arch_hw.detect()
    print(f"machine: {hw.get('gpu') or 'no GPU'}, {hw['vram_gb']:.1f} GB VRAM, {hw['ram_gb']:.1f} GB RAM -> profile {arch_server.profile_for(hw)}")
    import arch_ollama
    try:
        arch_ollama.ensure_running(say=print)             # its own process: it keeps running when this window closes
    except Exception as ex:                               # Ollama is the fallback: never let it stop the hub from starting
        print(f"[ollama] could not check or start it ({type(ex).__name__}: {ex})")
    try:                                                  # a crash or a killed window can leave strays: start from a free machine
        import arch_cleanup
        cp = arch_cleanup.plan(core.load_config())
        cp["items"] = [it for it in cp["items"] if it["kind"] in ("strays", "temp")]
        arch_cleanup.run(cp, yes=True)
    except Exception:
        pass
    st = arch_server.ensure(core.load_config(), say=print, watch=True)   # the hub keeps a watchdog: crashed tiers restart, stepped-aside ones come back
    print("server: " + (st.get("mode") or "ollama"))
    if not args.no_hub:
        import architect_hub
        return architect_hub.main([])
    return 0


def cmd_caches(args):
    """What the caches on disk hold and what may go (arch_cachehygiene). Reads only, unless --prune --yes."""
    import arch_cachehygiene as hyg
    cfg = core.load_config()
    inv = hyg.inventory(cfg)
    plan = hyg.plan_prune(cfg, inv=inv)
    result = hyg.prune(plan, yes=bool(args.yes), cfg=cfg) if args.prune else None
    if args.json:
        print(json.dumps({"inventory": inv, "plan": plan, "result": result}, default=str))
    else:
        print("\n".join(hyg.report_lines(inv, plan, result)))
    return 0


def cmd_select(args):
    """Choose a model per job by measurement (arch_select). Read-only except --apply / --undo."""
    import arch_jobs
    import arch_select as sel
    cfg = core.load_config()
    split = lambda v: [x.strip() for x in v.split(",") if x.strip()] if v else None
    allowed = split(args.allowed)
    if args.apply:
        job, model = args.apply
        rec = arch_jobs.assign(cfg, job, model)
        print(f"{job}: {', '.join(rec['before'])} -> {model} (select --undo puts back what was there)")
        return 0
    if args.undo:
        rec = arch_jobs.undo()
        print("nothing to undo" if not rec else f"restored the {rec['job']} roles to what they were before {rec['model']}")
        return 0
    if args.screen:
        facts = sel._facts()
        models = split(args.models) or [m["name"] for m in sel.candidates(cfg, facts, allowed)]
        sel.screen(models, split(args.jobs), fake=args.fake, cfg=cfg, facts=facts)
        return 0
    if args.measure:
        sel.measure(args.top_k, args.n, [int(x) for x in split(args.seeds) or ["1", "2"]], cfg=cfg, jobs=split(args.jobs), fake=args.fake)
        return 0
    if args.why:
        import arch_hw
        try:
            hw = arch_hw.detect()
        except Exception:
            hw = None
        w = sel.why(cfg, allowed=allowed, facts=sel._facts(), hw=hw, include_fake=args.fake)
        print(json.dumps(w, indent=1) if args.json else sel.why_table(w))
        return 0
    p = sel.plan(cfg, split(args.models), split(args.jobs))
    print(json.dumps(p, indent=1) if args.json else p["text"])
    return 0


def cmd_preset(args):
    """Apply a role-profile preset (data/presets/*.json) to your settings, or undo the last role-profile write."""
    import arch_roleprofile as rp
    if args.undo:
        rec = rp.undo()
        print("nothing to undo" if not rec else f"undone: {rec['what']}")
        return 0
    if not args.name:
        for k, p in rp.presets().items():
            print(f"{k:10} {p.get('blurb', '')}")
        return 0
    try:
        rec = rp.apply_preset(args.name)
    except (KeyError, ValueError) as ex:
        print(f"[x] {ex}")
        return 2
    print(f"applied {rec['what']} (preset --undo puts back what was there)")
    return 0


def cmd_tune_role(args):
    """Try profile values on one role's probe tasks and report pass rate, time and tokens; --apply-best --yes writes the winner (undoable)."""
    import arch_roleprofile as rp
    try:
        out = rp.tune_role(args.role, args.try_ or [], n=args.n, fake=args.fake, apply_best=args.apply_best, yes=args.yes)
    except ValueError as ex:
        print(f"[x] {ex}")
        return 2
    print(json.dumps(out, indent=1) if args.json else rp.report(out))
    return 0


def cmd_recommend(args):
    """Print the recommended steps (role, tier, ctx, thinking, escalation, reason, evidence) for one flow."""
    import arch_roleprofile as rp
    try:
        rec = rp.recommend(args.flow)
    except KeyError as ex:
        print(f"[x] {ex.args[0]}")
        return 2
    print(json.dumps(rec, indent=1) if args.json else rp.recommend_text(rec))
    return 0


def cmd_cost(args):
    """The report's loop cost model (Eq. 15-19) from this machine's traces: seconds per ACCEPTED change, per role and model."""
    import arch_costmodel as cm
    if args.planner:
        v = [float(x) for x in args.planner.split(",")]
        if len(v) not in (9, 10):
            print("--planner wants c_b,t_b,p_b,p_b_new,c_r,t_l,p_l,t_s,p_s[,lambda*dS]  e.g. 20,15,0.40,0.50,1,120,0.9,30,0.8,15")
            return 2
        pays, left, right = cm.planner_pays(*v)
        print(f"builder time saved {left:.1f} s  vs  planner time added {right:.1f} s  ->  "
              + ("the larger planner PAYS" if pays else "keep the small planner"))
        return 0
    import glob as _glob
    roots = [args.root] if args.root else cm.default_roots()
    rows = cm.measure(roots, power_w=args.watts, min_calls=args.min_calls)
    ev = cm.from_evals(sorted(_glob.glob(os.path.join(core.data_dir(), "evals", "*.json"))))
    if args.json:
        print(json.dumps({"calls": rows, "evals": ev}, indent=1))
    else:
        print("Per task, from your saved eval runs:\n" + cm.report_evals(ev) + "\n\nPer model call, from run traces:\n" + cm.report(rows))
    return 0


def cmd_memplan(args):
    """Worker x parallel slots x window: layers on the card, the decode bound and what N candidates cost (Eq. 5, 7, 11, 21)."""
    import arch_memplan
    rows = arch_memplan.plan(models=[m for m in (args.models or "").split(",") if m] or None,
                             windows=tuple(int(w) for w in (args.windows or "4096,8192").split(",")))
    print(json.dumps(rows, indent=1) if args.json else arch_memplan.report(rows))
    return 0


def cmd_cleanup(args):
    """Close what the app started (model servers, Ollama models, Launch runs, leftover eval folders). Without --yes it only lists."""
    import arch_cleanup
    p = arch_cleanup.plan(core.load_config())
    print(arch_cleanup.report(p))
    if args.yes:
        arch_cleanup.run(p, yes=True)
    elif p["items"]:
        print("Nothing was closed. To close it: python architect.py cleanup --yes")
    return 0


def cmd_explain(args):
    """Model options for this machine and a kind of work, in plain words first, numbers second, formula third."""
    if getattr(args, "model", None):              # one model: what its family is known for and the settings it wants (arch_modelknow)
        import arch_modelknow
        print(arch_modelknow.explain(args.model))
        return 0
    import arch_hw
    import arch_modelmath as mm
    import arch_passport
    hw = arch_hw.detect()
    names = [x for x in (args.models or "").split(",") if x]
    ps = [arch_passport.load(n) for n in names] if names else []
    ps = [p for p in ps if p] or list(arch_passport.refresh(say=lambda m: None).values())
    if names:
        ps = [p for p in ps if p["name"] in names]
    if not ps:
        print("No model passports yet: run `python architect.py passport` with your model server up.")
        return 1
    cands = mm.candidates_from_passports(ps)
    adv = mm.advise(hw, cands, workload=args.workload, ctx=args.ctx, kv=args.kv)
    if args.json:
        print(json.dumps(adv, indent=1))
        return 0
    print(mm.render(adv))
    if args.quants:
        base = next((c for c in cands if c["name"] == args.quants), cands[0])
        print(f"\nThe same model at other storage formats ({base['name']}):")
        for r in mm.compare_quants(base, hw, args.ctx):
            print(f"  {r['option']:40} {r['plain']}  [{r['numbers']['total_gb']} GB, {r['numbers']['layers_on_gpu']} layers on the card]")
    return 0


def cmd_eval(args):
    """The fixed task set, through the real flows, judged by code: pass rate, tokens, latency per flow."""
    import arch_eval
    flows_ = [x.strip() for x in (args.flow or "").split(",") if x.strip()] or None
    if args.list:
        for t in (arch_eval.representative() if getattr(args, "set", None) == "representative" else
                  arch_eval.load_tasks(flows_, args.quick, args.n, [x for x in (args.tags or "").split(",") if x])):
            print(f"  {t['flow']:10} {t['id']:28} {t.get('kind', 'flow'):6} {t['goal'][:70]}")
        return 0
    if not args.fake:
        import arch_hosts as H
        import arch_server
        served = [p for p in arch_server.status().get("processes", []) if p.get("up")]
        pr = H.probe(core.ollama_host()) if not served else {"ok": True}
        if not pr.get("ok"):
            print(f"The eval needs a model server. Neither llama-server (`architect.py server start`) nor Ollama at {core.ollama_host()} is answering; or run with --fake to prove the harness plumbing without a model.")
            return 1
    say = (lambda m: None) if args.json else print
    tasks_, prev, label = None, None, args.label or ("fake" if args.fake else "live")
    if getattr(args, "set", None) == "representative":            # the same 17 tasks every time, so runs compare task by task
        tasks_ = arch_eval.representative()
        label = args.label or ("representative-fake" if args.fake else "representative")
        prev = arch_eval.latest(label=label, fake=args.fake)
        say(f"[eval] the representative set: {len(tasks_)} tasks, one at a time" + ("" if args.fake else " (about two hours on a 6 GB laptop; it runs "
            "below normal priority, and another test suite or benchmark waits for it)"))
    rep = arch_eval.run(flows=flows_, quick=args.quick, label=label, fake=args.fake, n=args.n,
                        tags=[x for x in (args.tags or "").split(",") if x] or None, say=say, seed=args.seed, tasks=tasks_)
    if prev:
        ch = arch_eval.task_changes(prev, rep)
        rep["task_changes"] = ch
        say(f"\nCompared with the previous representative run: now passing {', '.join(ch['won']) or 'nothing new'}; "
            f"no longer passing {', '.join(ch['lost']) or 'nothing'}; {ch['same']} unchanged.")
    rc = 0
    if args.compare:
        base = arch_eval.latest(label=args.compare, fake=args.fake) if not os.path.isfile(args.compare) else core.load_json(args.compare)
        if not base:
            print(f"no saved eval to compare with (label or file: {args.compare}); run one with --label {args.compare} first")
            rc = 2
        else:
            cmp_ = arch_eval.compare(base, rep)
            if not args.json:
                print(arch_eval.compare_table(cmp_))
            rep["compare"] = cmp_
            rc = 0 if cmp_["ok"] else 1
    if args.json:
        slim = {k: v for k, v in rep.items() if k != "results"}
        slim["results"] = [{k: v for k, v in r.items() if k != "judges"} for r in rep["results"]]
        print(json.dumps(slim))
    return rc


def cmd_selftest(args):
    """Runs every suite against a throwaway home, so no test can leave a config or measured speeds in the real install."""
    import tempfile
    old_home, old_data = core.HOME, os.environ.get("ARCHITECT_DATA_DIR")
    core.set_home(tempfile.mkdtemp(prefix="arch_home_"))
    os.environ["ARCHITECT_DATA_DIR"] = tempfile.mkdtemp(prefix="arch_data_")
    try:
        return _selftest_suites(args)
    finally:
        core.set_home(old_home)
        if old_data is None:
            os.environ.pop("ARCHITECT_DATA_DIR", None)
        else:
            os.environ["ARCHITECT_DATA_DIR"] = old_data


def _selftest_suites(args):
    suite = getattr(args, "suite", "core") or "core"
    rc = 0
    # Every suite runs against its own throwaway data folder. The suites drive real Engine calls with a scripted stand-in model and then
    # mark the replies accepted, which is how the install's history is written -- so without this, running the tests taught the app that a
    # test fixture was an example of good work, and later real runs were shown it. It also made the suites unrepeatable: once the history
    # held an example, the scripted "first draft is flawed" step stopped firing.
    import tempfile as _tf
    _dd = os.environ.get("ARCHITECT_DATA_DIR")
    _tmp = _tf.mkdtemp(prefix="archsuite_")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(_tmp, "_data")
    os.environ["ARCHITECT_SELFTEST"] = "1"                 # suites never call Claude, whatever your settings say (arch_cloud._testing)
    # ...and its own settings file. Once a person picks a team, the install's settings name real models on a live server; a suite that
    # read them would send its scripted test traffic there.
    _old_home = core.HOME
    core.set_home(os.path.join(_tmp, "_home"))
    os.makedirs(core.HOME, exist_ok=True)
    try:
        return _run_suites(suite)
    finally:
        core.set_home(_old_home)
        if _dd is None:
            os.environ.pop("ARCHITECT_DATA_DIR", None)
        else:
            os.environ["ARCHITECT_DATA_DIR"] = _dd
        import shutil as _sh
        _sh.rmtree(_tmp, ignore_errors=True)


def _run_suites(suite):
    rc = 0
    import arch_selftest
    rc |= arch_selftest.run()
    import arch_selftest_tune
    rc |= arch_selftest_tune.run()
    import arch_selftest_opt
    rc |= arch_selftest_opt.run()
    import arch_selftest_ci
    rc |= arch_selftest_ci.run()
    import arch_selftest_loop
    rc |= arch_selftest_loop.run()
    import arch_selftest_hosts
    rc |= arch_selftest_hosts.run()
    import arch_selftest_live
    rc |= arch_selftest_live.run()
    import arch_selftest_server
    rc |= arch_selftest_server.run()
    import arch_selftest_fake
    rc |= arch_selftest_fake.run()
    import arch_selftest_ready
    rc |= arch_selftest_ready.run()
    import arch_selftest_index
    rc |= arch_selftest_index.run()
    import arch_selftest_chain
    rc |= arch_selftest_chain.run()
    import arch_selftest_eval
    rc |= arch_selftest_eval.run()
    import arch_selftest_offline
    rc |= arch_selftest_offline.run()
    import arch_selftest_llama
    rc |= arch_selftest_llama.run()
    import arch_selftest_dataflow
    rc |= arch_selftest_dataflow.run()
    import arch_selftest_speed
    rc |= arch_selftest_speed.run()
    import arch_selftest_learn
    rc |= arch_selftest_learn.run()
    import arch_selftest_agentic
    rc |= arch_selftest_agentic.run()
    import arch_selftest_supervisor
    rc |= arch_selftest_supervisor.run()
    import arch_selftest_dist
    rc |= arch_selftest_dist.run()
    import arch_selftest_flows_more
    rc |= arch_selftest_flows_more.run()
    import arch_selftest_win
    rc |= arch_selftest_win.run()
    if suite in ("all", "full"):
        import arch_selftest_pro
        rc |= arch_selftest_pro.run()
        try:
            import arch_selftest_sec
            rc |= arch_selftest_sec.run()
        except ImportError:
            print("[i] arch_selftest_sec not present yet -- skipped")
        import importlib
        for _name in ("agentlib", "cloud", "costs", "crew", "data2", "fastlane", "fleet", "hubcalm", "hubui",
                      "localfleet", "logbook", "overseer", "scout", "tokens", "tune2"):
            _mod = importlib.import_module("arch_selftest_" + _name)
            _fn = getattr(_mod, "run", None) or getattr(_mod, "main")     # logbook and overseer expose main()
            rc |= (_fn() or 0)
        # The suites added on 2026-11-04 (docs/release_20261104.md). Some stop with SystemExit/AssertionError on the first failed check
        # instead of returning 1, so each is guarded: one failing suite is counted as a failure and the rest still run.
        for _name in ("augment", "broker", "design", "firstvisit", "harness", "hubperf", "iterate", "localagent", "mesh",
                      "placement", "redact", "routelearn", "templates_extra", "why", "workspace"):
            try:
                _mod = importlib.import_module("arch_selftest_" + _name)
                if _name == "hubperf":                                      # no run(): its checks are backend() + ui()
                    _mod.backend()
                    _mod.ui()
                    print(f"ALL {_mod.N[0]} HUBPERF CHECKS PASSED")
                    continue
                # routelearn's run() is a fixture helper that takes arguments; its entry point is main()
                _fn = getattr(_mod, "main", None) if _name == "routelearn" else (getattr(_mod, "run", None) or getattr(_mod, "main"))
                rc |= (_fn() or 0)
            except SystemExit as _e:
                if _e.code not in (None, 0):
                    print(f"[x] arch_selftest_{_name}: {_e.code}")
                    rc |= 1
            except Exception as _e:
                print(f"[x] arch_selftest_{_name} failed: {type(_e).__name__}: {_e}")
                rc |= 1
        # suites whose run() returns True when every check passed (art returns 0 like the older ones)
        for _name in ("a11y", "art", "bestofn", "cleanup", "costmodel", "graceful", "idle", "memplan", "cachehygiene", "cachespage", "offcard", "ollamaidle", "promptprefix", "prefixreuse", "replybudget", "testname", "triage", "placement_wired", "provenance", "refusal", "smoke", "wiring", "brokerwired", "testfirst", "templates_catalog", "swap", "orchestration", "squad", "select", "plugins", "telemetry", "retrieval", "modelknow", "verifymachine", "selfimprove", "roleprofile", "eta", "notify", "effective", "playbook", "cacheplus", "overlap", "contracts", "curriculum", "harness2", "ourtasks", "reqcache", "cpuworker", "overlay", "mcpclient", "bundle", "gitflow", "onboard", "harness3", "replay", "finops", "review1", "review2", "review3", "review4", "custom", "docs", "uismoke", "swallow", "localintel", "cachestats"):
            try:
                _mod = importlib.import_module("arch_selftest_" + _name)
                _res = (getattr(_mod, "run", None) or _mod.main)()       # retrieval has only main() -> 0/1
                rc |= (0 if (_res is True or (type(_res) is int and _res == 0)) else 1)     # False == 0, so check the type
            except SystemExit as _e:
                rc |= 0 if _e.code in (None, 0) else 1
            except Exception as _e:
                print(f"[x] arch_selftest_{_name} failed: {type(_e).__name__}: {_e}")
                rc |= 1
        try:
            import arch_hub_check
            rc |= arch_hub_check.run()
        except ImportError:
            print("[i] arch_hub_check not present yet -- skipped")
    if suite == "full":                      # the self-improvement pipeline itself (it runs nested self-tests, so it is not part of `all`)
        import arch_selftest_self
        rc |= arch_selftest_self.run()
    return rc


def main(argv=None):
    _argv = list(sys.argv[1:] if argv is None else argv)
    if _argv[:1] == ["bench-retrieval"]:     # keyword vs keyword+embedding retrieval benchmark (tools/retrieval_bench.py)
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))
        import retrieval_bench
        return retrieval_bench.main(_argv[1:])
    if _argv[:1] == ["onboard"]:             # /init: detect commands, index, draft ARCHITECT.md (arch_onboard.py)
        import arch_onboard
        return arch_onboard.main(_argv[1:])
    if _argv[:1] == ["consolidate"]:         # merge/prune project lessons, propose ARCHITECT.md lines
        import arch_memory, json as _j
        _r = arch_memory.consolidate(_argv[_argv.index("--root") + 1] if "--root" in _argv else ".", dry_run="--dry-run" in _argv)
        print(_j.dumps(_r, indent=1, default=str)); return 0 if _r.get("ok", True) else 1
    if _argv[:1] == ["swap"]:                # model swapping (arch_swap.py has its own arguments)
        import arch_swap
        return arch_swap.main(_argv[1:])
    if _argv[:1] == ["crew"]:                # local agent workflows (arch_squad.py has its own arguments)
        import arch_squad
        return arch_squad.main(_argv[1:])
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", help="project folder (default: current folder)")
    common.add_argument("--app", help="an app name from portfolio.json")
    common.add_argument("--review", choices=["ask", "auto", "dry"], default="ask", help="ask = approve each diff (default)")
    common.add_argument("--model", help="use this one model for every role")
    common.add_argument("--role-model", action="append", metavar="ROLE=MODEL", help="e.g. generator_ui=gemma4:e4b (repeatable)")
    common.add_argument("--max-revisions", type=int)
    common.add_argument("--best-of", type=int)
    common.add_argument("--set", action="append", metavar="KEY.PATH=VALUE", help="override any config key, e.g. --set behavior.git=commit (repeatable)")
    r = sub.add_parser("run", parents=[common])
    r.add_argument("goal", nargs="?")
    r.add_argument("--recipe", help="the recipe this run was started from (recorded in run.json)")
    r.add_argument("--flow", default="auto", choices=["auto", *flows.FLOWS])
    r.add_argument("--stages", help="ship flow only: comma-separated stages (build,test,hunt,secure,review); default by machine tier")
    r.add_argument("--domains", help="comma-separated: " + ",".join(flows.DOMAINS))
    r.add_argument("--template", help="for --flow new: a template id (see 'templates'), or 'auto'")
    r.add_argument("--name", help="for --flow new: the project/package name")
    r.add_argument("--addons", help="for --flow new: comma-separated, e.g. ci,docker,editorconfig")
    r.add_argument("--no-build", action="store_true", help="for --flow new: write the template only, don't build the goal on top")
    r.add_argument("--autofix", action="store_true", help="for --flow hunt/review: apply the fix once a finding is confirmed, instead of only reporting it")
    r.add_argument("--repro", help="for --flow bugfix: path to an existing failing test that reproduces the bug")
    si = sub.add_parser("self-improve", parents=[common], help="improve the Architect's OWN code safely: work on a candidate copy, pass the gates (protected files, compile, "
                        "no new fake code, its own self-test, no fewer checks), then promote only after you confirm. --root PATH = plain run on that folder")
    si.add_argument("--yes", action="store_true", help="promote automatically when every gate passes (default: ask)")
    si.add_argument("--suite", choices=["core", "all"], default="all", help="which self-test suite the gate runs (default: all)")
    si.add_argument("--allow-safety-edits", action="store_true", help="let the change touch the network guard / gate / manifest (a human must have reviewed it)")
    si.add_argument("--keep", action="store_true", help="keep the candidate folder after the run")
    si.add_argument("--goal", dest="goal_opt", help="the iterative loop: what to improve (no goal at all = pick the top failing check / error / advice)")
    si.add_argument("--rounds", type=int, help="the iterative loop: at most N audit -> plan -> build -> verify -> gate rounds (default 3)")
    si.add_argument("--fake", action="store_true", help="the iterative loop on the scripted fake model (no real model; gates that need one are could-not-run)")
    si.add_argument("--full-gates", action="store_true", help="with --fake: still run the full self-test, check floor and eval gates")
    si.add_argument("--allow-gate-edits", action="store_true", help="the iterative loop may change the gates themselves (a person must read the diff)")
    si.add_argument("--resume", metavar="SESSION", help="another round on an earlier self-improve session's copy")
    si.add_argument("goal", nargs="?")
    si.add_argument("--flow", default="review", choices=["auto", *flows.FLOWS])
    si.add_argument("--domains", help="comma-separated: " + ",".join(flows.DOMAINS))
    si.add_argument("--autofix", action="store_true", help="for --flow hunt/review: apply the fix once a finding is confirmed, instead of only reporting it")
    si.add_argument("--repro", help="for --flow bugfix: path to an existing failing test that reproduces the bug")
    t = sub.add_parser("route", parents=[common])
    t.add_argument("goal")
    sv2 = sub.add_parser("self-verify", help="is this install still exactly what was last verified? (--write records the current state)")
    sv2.add_argument("--write", action="store_true")
    sub.add_parser("self-rollback", help="undo the last self-improve promotion")
    sub.add_parser("self-history", help="list self-improve promotions, rollbacks and rejections")
    fl_ = sub.add_parser("flows", help="the workflows and the checks inside them; --validate checks every flows/*.yaml")
    fl_.add_argument("--validate", action="store_true")
    sub.add_parser("templates")
    _pu = sub.add_parser("undo", parents=[common])
    _pu.add_argument("--run", default="", help="undo this run id (default: the newest run)")
    sub.add_parser("portfolio")
    sub.add_parser("show-config", parents=[common])
    sv = sub.add_parser("serve", help="start/stop/health-check/tail-logs for a project's own run command (real subprocess, real port)")
    sv.add_argument("action", choices=["start", "stop", "status", "logs"])
    sv.add_argument("--root", help="project folder (default: current folder)")
    sv.add_argument("--run-cmd", dest="run_cmd", help="override the run command (default: from .arch/handoff.json)")
    sv.add_argument("--port", type=int)
    sv.add_argument("--tail", type=int, default=60)
    sv.add_argument("--network", choices=["offline", "practice", "open"], help="override this run's network policy (default: the project's own behavior.network setting)")
    dc = sub.add_parser("doctor", help="real environment preflight: Python version, pytest, Ollama reachability, disk, write access, port, git, NIC presence, network policy")
    dc.add_argument("--root", help="project folder (default: current folder)")
    dc.add_argument("--port", type=int, default=8766, help="hub port to check for availability (default: 8766)")
    dc.add_argument("--fix", action="store_true", help="apply the safe, real repairs (pip install pytest if missing; clean stale .arch/runs/ if disk is low), then re-check")
    sub.add_parser("assess", help="readiness per workload (coding/security/data) from the real scoreboard, plus a hardware-tuned settings suggestion for this machine")
    av = sub.add_parser("advise", help="combines doctor + assess + a real static code scan + scope/findings into one ranked, evidence-cited list of next actions")
    av.add_argument("--root", help="project folder (default: current folder)")
    fk = sub.add_parser("fakecode", help="find stubs, assertion-free tests, swallowed errors and placeholder data (exit 1 on gate-level findings); fix them with: run --flow purge")
    fk.add_argument("--root", help="project folder (default: current folder)")
    fk.add_argument("--json", action="store_true")
    fk.add_argument("--all", action="store_true", help="include low-severity placeholder markers")
    hs_ = sub.add_parser("hosts", help="other machines of yours that run Ollama: add, check, route roles to them, benchmark")
    hs_.add_argument("--add", action="append", metavar="NAME=ADDRESS")
    hs_.add_argument("--remove", action="append", metavar="NAME")
    hs_.add_argument("--route", action="append", metavar="ROLE=HOST[:MODEL]", help="send a role's model calls to that machine")
    hs_.add_argument("--unroute", action="append", metavar="ROLE")
    hs_.add_argument("--plan", action="store_true", help="suggest roles that would gain from a bigger model on another machine (changes nothing)")
    hs_.add_argument("--apply-plan", action="store_true", help="route the suggested roles")
    hs_.add_argument("--bench", action="store_true", help="measure the speed of each machine's largest model")
    hs_.add_argument("--json", action="store_true")
    lt_ = sub.add_parser("livetest", help="run real tasks with YOUR models (route, structured output, build, bugfix, feature, CI) and judge them by code")
    lt_.add_argument("--quick", action="store_true", help="routing, structured output and one build only")
    lt_.add_argument("--only", help="comma list of: route,json,build,bugfix,feature,cicd")
    sub.add_parser("shortcut", help="put an Architect launcher on your Desktop")
    st_ = sub.add_parser("status", help="one screen: Ollama, models, power, other machines, last run")
    st_.add_argument("--root", default=".")
    rs_ = sub.add_parser("resume", parents=[common], help="continue the last run that crashed or stopped (the build flow skips sprints that already passed)")
    rs_.add_argument("--list", action="store_true", help="list recorded runs and their status")
    ci_ = sub.add_parser("ci", help="verify the CI/CD pipeline files against the repository (local, no model); --run-local runs the safe commands offline")
    ci_.add_argument("--root", default=".")
    ci_.add_argument("--run-local", action="store_true")
    ci_.add_argument("--init", action="store_true", help="write a baseline workflow if there is none")
    ci_.add_argument("--release", action="store_true", help="with --init: also a tag-triggered build-and-upload workflow")
    ci_.add_argument("--json", action="store_true")
    op = sub.add_parser("optimize", help="per-model sampling, server settings and framework advice for the models you have installed")
    op.add_argument("--apply", action="store_true", help="write the proposed sampling values (only keys you have not set)")
    op.add_argument("--undo", action="store_true", help="put back what the last --apply replaced")
    op.add_argument("--model", action="append", help="limit --apply to this installed model (repeatable)")
    op.add_argument("--online", action="store_true", help="also read public facts (updated, licence, gated) for your hf.co models from huggingface.co; read-only, nothing is downloaded")
    op.add_argument("--json", action="store_true")
    tn = sub.add_parser("tune", help="measure this machine with your models, then write the context size, timeouts and harness profile that fit")
    tn.add_argument("--model", action="append", help="tune only this model (repeatable); default: the models assigned to roles")
    tn.add_argument("--no-probe", action="store_true", help="fit math only; make no model calls")
    tn.add_argument("--no-apply", action="store_true", help="report only; change no settings")
    tn.add_argument("--undo", action="store_true", help="put back every setting the last tune changed")
    tn.add_argument("--deep", action="store_true", help="also fill most of the context window once to prove nothing is truncated (slow on big windows)")
    rd = sub.add_parser("ready", help="one command from a fresh machine to a working setup: repair, pick/download a model (asks first), tune, prove, list upgrades")
    rd.add_argument("--root", help="project folder to run the environment checks in (default: here)")
    rd.add_argument("--yes", action="store_true", help="agree to downloading a first model if none is installed")
    rd.add_argument("--no-probe", action="store_true", help="skip the speed/context probe in the tune step")
    rd.add_argument("--no-proof", action="store_true", help="skip the tool-call/format proof (this also skips the edit-format check and the smoke test)")
    rd.add_argument("--no-smoke", action="store_true", help="skip the final smoke test (a tiny build with tests through the whole harness)")
    sc = sub.add_parser("scan", help="profile an existing project once (language, tests, size, fake-code, optional test baseline) so runs start informed")
    sc.add_argument("--root", help="project folder to profile (default: here)")
    sc.add_argument("--run-tests", action="store_true", help="also run the detected test command for a baseline")
    sc.add_argument("--json", action="store_true")
    ix = sub.add_parser("index", help="hybrid retrieval index of a project (vectors + keywords): build or refresh it, or --query to compare the rankings for one request")
    ix.add_argument("--root", help="project folder (default: here)")
    ix.add_argument("--query", help="show the keyword, vector and fused rankings for this request")
    ix.add_argument("--rebuild", action="store_true", help="drop the index and embed everything again")
    ix.add_argument("--status-only", action="store_true", help="report without refreshing")
    ix.add_argument("--json", action="store_true")
    bk = sub.add_parser("backlog", help="the project's backlog (.arch/backlog.json): list, --add a goal, --propose items from review/hunt/secure findings")
    bk.add_argument("--root")
    bk.add_argument("--add", metavar="GOAL")
    bk.add_argument("--flow", choices=["auto", *flows.FLOWS], default="auto")
    bk.add_argument("--propose", action="store_true")
    bk.add_argument("--n", type=int, default=5)
    bk.add_argument("--json", action="store_true")
    apl = sub.add_parser("autopilot", help="work through the backlog on its own within a budget (every gate on); --propose keeps adding items from what the runs find")
    apl.add_argument("--root")
    apl.add_argument("--hours", type=float, default=1.0)
    apl.add_argument("--max", type=int, default=5, help="items per session")
    apl.add_argument("--review", choices=["auto", "ask", "dry"], default="auto")
    apl.add_argument("--propose", action="store_true")
    dsp = sub.add_parser("dataset", help="export per-role SFT sets and DPO preference pairs from your marked run traces (hub_data/datasets/, with a QLoRA recipe)")
    dsp.add_argument("--root", help="project folder (default: here)")
    dsp.add_argument("--all", action="store_true", help="also every app in the portfolio")
    dsp.add_argument("--role", action="append", help="only these roles (repeatable)")
    dsp.add_argument("--out", help="output folder (default: hub_data/datasets)")
    dsp.add_argument("--json", action="store_true")
    dsp.add_argument("--pairs", choices=["same-task", "all"], default="same-task",
                     help="same-task (default) keeps only preference pairs where both tries answered the same task; all keeps every failed-then-passed pair and labels it")
    di = sub.add_parser("dist", help="build an offline installer for a hardware profile (gpu6, cpu16, ...), or --verify one")
    di.add_argument("--profile", help="which hardware profile it is for (default: this machine's)")
    di.add_argument("--out", help="where to write the installer folder")
    di.add_argument("--with-server", action="store_true", help="include the model server (about 1.2 GB), so nothing needs downloading")
    di.add_argument("--with-models", action="store_true", help="include the profile's models as GGUF files (several GB)")
    di.add_argument("--zip", action="store_true", help="also write it as one zip")
    di.add_argument("--verify", metavar="DIR", help="check every file of an installer against its checksums")
    qu = sub.add_parser("queue",help="tasks waiting for the supervisor: queue add \"goal\", queue (list), queue cancel ID, queue clear")
    qu.add_argument("action", nargs="?", default="list", choices=["list", "add", "cancel", "clear"])
    qu.add_argument("goal", nargs="?", help="the task (for add), or the task id (for cancel)")
    qu.add_argument("--root", help="the project it is about (default: here)")
    qu.add_argument("--flow", help="pin a flow; leave it out and the router decides")
    qu.add_argument("--minutes", type=float, help="how long this task may take")
    qu.add_argument("--priority", type=int, help="higher goes first")
    qu.add_argument("--limit", type=int)
    qu.add_argument("--json", action="store_true")
    su = sub.add_parser("supervise", help="work through the queue on its own: retries a failed task with a different flow, within daily limits")
    su.add_argument("--hours", type=float, help="how long to keep going (default 1)")
    su.add_argument("--max", type=int, help="at most this many tasks")
    su.add_argument("--stop", action="store_true", help="the kill switch: nothing new starts, a running flow stops between steps")
    su.add_argument("--resume", action="store_true", help="lift the stop")
    su.add_argument("--json", action="store_true")
    rc_ = sub.add_parser("receipt", help="write one run as a single self-contained HTML page: what it did, what it checked, what it cost")
    rc_.add_argument("--root", help="project folder (default: here)")
    rc_.add_argument("--run", help="a run id (default: the most recent)")
    rc_.add_argument("--out", help="where to write it (default: inside the run folder)")
    rc_.add_argument("--open", action="store_true", help="open it in your browser afterwards")
    po = sub.add_parser("port", help="pick the team from the models THIS machine has, and move your setup to another computer")
    po.add_argument("--root", help="project folder (default: here)")
    po.add_argument("--pack", metavar="FILE.zip", help="write your settings, notes, skills, commands and training data to a zip")
    po.add_argument("--adopt", metavar="FILE.zip", help="restore one of those here, then fit the team to this machine")
    po.add_argument("--apply", action="store_true", help="save the choices (without this, it only shows them)")
    po.add_argument("--json", action="store_true")
    cm = sub.add_parser("commands", help="the saved requests you can invoke by name (/review, /fix, ...), here and in the console")
    cm.add_argument("--root", help="project folder (default: here)")
    cm.add_argument("--show", metavar="NAME", help="print one command's full text")
    cm.add_argument("--json", action="store_true")
    hk = sub.add_parser("hooks", help="your own commands, run at fixed points in a run (a linter after every change, and so on)")
    hk.add_argument("--root", help="project folder (default: here)")
    hk.add_argument("--trust", action="store_true", help="approve this project's hook file after reading what it runs")
    hk.add_argument("--example", action="store_true", help="print an example hook file")
    hk.add_argument("--json", action="store_true")
    pg = sub.add_parser("plugins", help="bundles of agents, workflows, skills, commands and hooks: list, show, enable, disable, trust, install, remove, new, validate, pack")
    pg.add_argument("action", nargs="?", default="list",
                    choices=["list", "show", "enable", "disable", "trust", "untrust", "install", "remove", "new", "validate", "pack"])
    pg.add_argument("target", nargs="?", help="a plugin name, or for install a folder or .zip")
    pg.add_argument("--root", help="project folder (default: here)")
    pg.add_argument("--out", help="pack: where to write the zip")
    pg.add_argument("--json", action="store_true")
    pm = sub.add_parser("promote-model", help="switch a tier to a different model, only if your own fixed task set says it is no worse")
    pm.add_argument("--tier", required=True, choices=["router", "worker", "heavy", "draft"])
    pm.add_argument("--model", required=True, help="the model (or .gguf path) to switch that tier to")
    pm.add_argument("--compare", default="baseline", help="the saved eval to beat (default: baseline)")
    pm.add_argument("--candidate", default="candidate", help="the saved eval of the new model (default: candidate)")
    pm.add_argument("--dry-run", action="store_true", help="say what would change and change nothing")
    ft = sub.add_parser("fit", help="the team's memory fit sheet: weights + KV cache per model against this GPU, what is resident, what a swap costs")
    ft.add_argument("--root", help="project folder (its run history gives the measured swaps per run)")
    ft.add_argument("--kv", choices=["f16", "q8_0"], default="f16", help="KV cache type to assume (server setting OLLAMA_KV_CACHE_TYPE)")
    ft.add_argument("--json", action="store_true")
    oc = sub.add_parser("orchestration", help="who delegated what to whom: Claude Code agents, local runs, tokens, caching, training data")
    oc.add_argument("--json", action="store_true")
    oc.add_argument("--limit", type=int, default=5, help="newest Claude sessions to show")
    tk = sub.add_parser("tasks", help="real task templates (goal sentences tied to the flow that proves them); --expand ID TARGET prints one filled in")
    tk.add_argument("--group", help="only this group, e.g. Fix")
    tk.add_argument("--expand", metavar="ID", help="print the goal for this template")
    tk.add_argument("target", nargs="*", help="what to put in the blank (with --expand)")
    ct = sub.add_parser("catalog", help="models you do not have yet that fit this machine, with sizes and the exact pull command")
    ct.add_argument("--verify", action="store_true", help="check every catalog tag/size against the public Ollama registry (needs internet)")
    pl = sub.add_parser("pull", help="download a model through Ollama (asks first; shows the size)")
    pl.add_argument("tag")
    pl.add_argument("--yes", action="store_true", help="do not ask")
    ev = sub.add_parser("eval", help="run the fixed task set (evals/) through the real flows, judged by code: pass rate, tokens and latency per flow; --fake proves the plumbing with no model")
    ev.add_argument("--flow", help="comma-separated flow ids (default: every flow that has tasks)")
    ev.add_argument("--quick", action="store_true", help="the first 10 tasks per flow")
    ev.add_argument("--fake", action="store_true", help="answer with the scripted oracle model (no GPU, no Ollama): checks the harness, not a model")
    ev.add_argument("--n", type=int, help="tasks to generate per family (default: each family's own)")
    ev.add_argument("--tags", help="comma-separated tags to keep")
    ev.add_argument("--seed", type=int, default=0)
    ev.add_argument("--label", help="name this run (the regression gate looks for the latest run named 'baseline')")
    ev.add_argument("--compare", help="a saved run's label (or a report file): print the per-flow comparison and exit 1 on a regression")
    ev.add_argument("--set", choices=["representative"], help="representative = the same 17 tasks every time (4 whole projects, builds, fixes, "
                                                               "features, tests, refactors), compared task by task with the previous run; run it overnight")
    ev.add_argument("--list", action="store_true", help="list the tasks and exit")
    ev.add_argument("--json", action="store_true", help="print the report as one JSON line (for hooks and gates)")
    sv_ = sub.add_parser("server", help="the resident llama-server: start | stop | status | plan (falls back to Ollama when the program is not installed)")
    sv_.add_argument("action", choices=["start", "stop", "status", "plan", "fetch"])
    sv_.add_argument("--heavy", action="store_true")
    sv_.add_argument("--yes", action="store_true", help="fetch: agree to the one-time download")
    pp_ = sub.add_parser("passport", help="describe every installed model (known facts + what was measured here), public or niche")
    pp_.add_argument("--model")
    st0 = sub.add_parser("start", help="detect the machine, start the resident server if installed, open the hub")
    st0.add_argument("--no-hub", action="store_true")
    ex_ = sub.add_parser("explain", help="model options for this machine and a kind of work: plain words first, then the numbers, then the formula")
    ex_.add_argument("--workload", choices=["edits", "long_doc", "reason", "batch", "tools"], default="edits")
    ex_.add_argument("--models", help="comma-separated model names (default: every passport)")
    ex_.add_argument("--model", help="one model tag: its family, strengths per job and recommended settings (arch_modelknow)")
    ex_.add_argument("--ctx", type=int, default=8192)
    ex_.add_argument("--kv", choices=["f16", "q8_0", "q4_0"], default="q8_0")
    ex_.add_argument("--quants", help="also show this model at other storage formats")
    ex_.add_argument("--json", action="store_true")
    mp_ = sub.add_parser("memplan", help="which worker, how many parallel slots, which window: layers on the card and what N candidates cost")
    mp_.add_argument("--models", help="comma-separated model names (default: every passport with an architecture)")
    mp_.add_argument("--windows", help="comma-separated windows, default 4096,8192")
    mp_.add_argument("--json", action="store_true")
    cl_ = sub.add_parser("cleanup", help="close what the app started: model servers, Ollama models, Launch runs, leftover eval folders")
    cl_.add_argument("--yes", action="store_true")
    sw_ = sub.add_parser("swap", help="swap models per tier or job from placement math, measurements and the playbook: --plan/--lint/--trial/--apply/--undo")
    sw_.add_argument("rest", nargs=argparse.REMAINDER, help="passed to arch_swap (e.g. --plan --job code, --lint, --trial worker MODEL --fake, --apply worker MODEL --yes, --undo)")
    se_ = sub.add_parser("select", help="choose a model PER JOB by measurement (C = seconds per passed task); --plan/--screen/--measure/--why/--apply/--undo")
    se_.add_argument("--plan", action="store_true", help="what a screen would run and its estimated GPU minutes (default)")
    se_.add_argument("--screen", action="store_true")
    se_.add_argument("--measure", action="store_true")
    se_.add_argument("--why", action="store_true")
    se_.add_argument("--apply", nargs=2, metavar=("JOB", "MODEL"))
    se_.add_argument("--undo", action="store_true")
    se_.add_argument("--models", help="comma-separated model names")
    se_.add_argument("--jobs", help="comma-separated jobs (route,plan,code,test,fix,review,security,infra,docs)")
    se_.add_argument("--allowed", help="comma-separated models a suggestion may name (e.g. uncensored only); others are still shown")
    se_.add_argument("--fake", action="store_true", help="scripted stand-in, no GPU")
    se_.add_argument("--top-k", type=int, default=2)
    se_.add_argument("--n", type=int, default=5)
    se_.add_argument("--seeds", help="comma-separated seeds, default 1,2")
    se_.add_argument("--json", action="store_true")
    pr_ = sub.add_parser("preset", help="role-profile presets: Fast, Balanced (= shipped), Quality, Battery; --undo puts back the last one")
    pr_.add_argument("name", nargs="?")
    pr_.add_argument("--undo", action="store_true")
    tr_ = sub.add_parser("tune-role", help="try role-profile values on one role's probe tasks (--try key=v1,v2 ...); --apply-best --yes writes the winner")
    tr_.add_argument("role")
    tr_.add_argument("--try", dest="try_", action="append", help="key=v1,v2 (repeatable; combinations capped at 8)")
    tr_.add_argument("--n", type=int, default=4)
    tr_.add_argument("--fake", action="store_true", help="scripted stand-in, no model")
    tr_.add_argument("--apply-best", action="store_true")
    tr_.add_argument("--yes", action="store_true")
    tr_.add_argument("--json", action="store_true")
    rc_ = sub.add_parser("recommend", help="the recommended role steps for a flow (build, bugfix, feature, refactor, test, review, secure, optimize, hunt, design, docs)")
    rc_.add_argument("flow")
    rc_.add_argument("--json", action="store_true")
    co_ = sub.add_parser("cost", help="seconds per ACCEPTED change per role and model (the report's C = t / p-bar), or --planner to test Eq. 19")
    co_.add_argument("--root", help="one project folder (default: every portfolio project + this folder)")
    co_.add_argument("--watts", type=float, help="average power draw, to report energy E = P * C")
    co_.add_argument("--min-calls", type=int, default=3)
    co_.add_argument("--planner", help="c_b,t_b,p_b,p_b_new,c_r,t_l,p_l,t_s,p_s[,lambda*dS]: does a larger planner pay?")
    co_.add_argument("--json", action="store_true")
    ca_ = sub.add_parser("caches", help="what the caches on disk hold (saved prompt memory, reply caches) and what could go; --prune --yes removes exactly that list")
    ca_.add_argument("--prune", action="store_true", help="apply the plan (deletes only with --yes)")
    ca_.add_argument("--yes", action="store_true", help="with --prune: really delete")
    ca_.add_argument("--json", action="store_true")
    st = sub.add_parser("selftest")
    st.add_argument("--suite", choices=["core", "all", "full"], default="core", help="core = engine, flows, tune, catalog, fake-code (default); all = + pro/security/hub-API suites; full = + the self-improvement pipeline")
    a = ap.parse_args(argv)
    fn = {"run": cmd_run, "flows": cmd_flows, "templates": cmd_templates, "route": cmd_route, "undo": cmd_undo,
          "portfolio": cmd_portfolio, "show-config": cmd_show, "serve": cmd_serve, "doctor": cmd_doctor, "assess": cmd_assess, "advise": cmd_advise,
          "self-improve": cmd_self_improve, "selftest": cmd_selftest, "ready": cmd_ready, "scan": cmd_scan, "index": cmd_index, "fit": cmd_fit, "dataset": cmd_dataset, "backlog": cmd_backlog, "autopilot": cmd_autopilot, "tasks": cmd_tasks, "self-verify": cmd_self_verify, "self-rollback": cmd_self_rollback, "self-history": cmd_self_history, "fakecode": cmd_fakecode, "tune": cmd_tune, "optimize": cmd_optimize, "ci": cmd_ci, "resume": cmd_resume, "hosts": cmd_hosts, "livetest": cmd_livetest, "shortcut": cmd_shortcut, "status": cmd_status, "catalog": cmd_catalog, "pull": cmd_pull, "eval": cmd_eval, "server": cmd_server, "passport": cmd_passport, "start": cmd_start, "explain": cmd_explain, "cost": cmd_cost, "select": cmd_select, "preset": cmd_preset, "tune-role": cmd_tune_role, "recommend": cmd_recommend, "swap": lambda a: __import__("arch_swap").main(list(a.rest or [])), "cleanup": cmd_cleanup, "memplan": cmd_memplan, "promote-model": cmd_promote_model, "commands": cmd_commands, "hooks": cmd_hooks, "plugins": cmd_plugins, "port": cmd_port, "receipt": cmd_receipt, "queue": cmd_queue, "supervise": cmd_supervise, "dist": cmd_dist, "caches": cmd_caches, "orchestration": lambda a: __import__("arch_orchestration").main((["--json"] if a.json else []) + ["--limit", str(a.limit)])}.get(a.cmd)
    if not fn:
        ap.print_help()
        return 0
    import arch_heavy
    if a.cmd in arch_heavy.HEAVY and not getattr(a, "list", False):         # suites, benchmarks, tuning: one at a time, below normal priority
        try:
            hcfg = core.load_config(os.path.abspath(getattr(a, "root", None) or "."))
        except Exception:
            hcfg = None
        with arch_heavy.one_at_a_time(a.cmd, hcfg):
            return fn(a)
    return fn(a)


if __name__ == "__main__":
    sys.exit(main())
