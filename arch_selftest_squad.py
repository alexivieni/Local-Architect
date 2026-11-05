"""Self-test for arch_squad (local agents with the Claude workflow's semantics). The scripted fake model only: an Ollama-shaped HTTP
server on 127.0.0.1 (arch_squad.start_fake); no real model, no model server, no Claude. Isolated ARCHITECT_DATA_DIR, ARCHITECT_NO_CACHE=1."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
N = [0, 0]


def ok(cond, what, detail=None):
    N[0 if cond else 1] += 1
    print(("[ok] " if cond else "[x] ") + what + ("" if cond or detail is None else f"  -- {str(detail)[:300]}"))


def resume_checks(sq, orch, core, proj, dd, cfg, tmp):
    W = {"name": "resume3", "phases": [
        {"title": "Audit", "agent": "auditor", "task": "look at app.py"},
        {"title": "Note", "log": "audit said {prev.summary}"},
        {"title": "Review", "agent": "reviewer", "task": "review"},
        {"title": "Verify", "agent": "verifier", "task": "verify"}]}
    ok(not sq.validate_workflow(W), "a workflow with a log: phase validates", sq.validate_workflow(W))

    def default_engine(d, owns):
        e = sq._owned_engine_class()(proj, "crew", "g", cfg, review="auto", say=sq._quiet, persist=False)
        e.owns = tuple(owns)
        return e

    def dying(d, owns):                                  # the run "crashes" at phase 3 (Verify)
        if d["name"] == "verifier":
            raise RuntimeError("benchmark yield")
        return default_engine(d, owns)
    v1, c1 = sq.run_workflow(W, proj, "g", cfg=cfg, data_dir=dd, engine_factory=dying, use_cache=False)
    ok(v1 == "fail" and [r["verdict"] for r in c1.records] == ["ok", "ok", "failed"], "first run: phases 1-2 ok, phase 3 dies",
       [r["verdict"] for r in c1.records])
    jl = open(os.path.join(c1.dir, "journal.jsonl"), encoding="utf-8").read()
    ok('"type": "log"' in jl and "audit said" in jl, "log: line written to the journal")
    v2, c2 = sq.run_workflow(W, proj, "g", cfg=cfg, data_dir=dd, resume_from=c1.run_id, use_cache=False)
    rs = c2.records
    ok(v2 == "pass" and c2.resume_from == c1.run_id, "resume: the run passes", (v2, c2.resume_from))
    ok([bool(r.get("replayed")) for r in rs] == [True, True, False], "resume: phases 1-2 replayed, phase 3 ran",
       [r.get("replayed") for r in rs])
    ok(all(r["totals"]["calls"] == 0 for r in rs[:2]) and rs[2]["totals"]["calls"] > 0, "resume: replayed agents cost 0 model calls",
       [r["totals"]["calls"] for r in rs])
    v3, c3 = sq.run_workflow(W, proj, "g", cfg=cfg, data_dir=dd, resume_from="last", replay=False, use_cache=False)
    ok(not any(r.get("replayed") for r in c3.records), "--no-replay: nothing replayed")
    # a failed/None result is never replayed: phase 3 of run 1 failed, so resuming from it reran phase 3 (above); none-verdicts too
    ok(all(k for k in sq._prior_records(c1.dir)) and len(sq._prior_records(c1.dir)) == 2, "only ok results are replay candidates")
    # writer replay needs its post-state
    WW = {"name": "wr", "phases": [{"title": "Build", "agent": "builder", "task": "FAKE:WRITE gen.py", "owns": "gen.py"},
                                   {"title": "Check", "agent": "reviewer", "task": "check"}]}
    vw, cw = sq.run_workflow(WW, proj, "g", cfg=cfg, data_dir=dd, use_cache=False)
    vw2, cw2 = sq.run_workflow(WW, proj, "g", cfg=cfg, data_dir=dd, resume_from=cw.run_id, use_cache=False)
    ok(vw == "pass" and cw2.records[0].get("replayed"), "writer replays while its file is as it left it",
       (vw, [r["verdict"] for r in cw.records], cw2.records[0].get("replayed")))
    with open(os.path.join(proj, "gen.py"), "w") as f:
        f.write("X = 2  # edited by hand\n")
    vw3, cw3 = sq.run_workflow(WW, proj, "g", cfg=cfg, data_dir=dd, resume_from=cw2.run_id, use_cache=False)
    ok(not cw3.records[0].get("replayed"), "an edited owned file makes the writer re-run")

    # step cache: hit, miss on file change, off by flag/env
    os.environ.pop("ARCHITECT_NO_CACHE", None)
    try:
        a = sq.Crew(proj, "g", cfg=cfg, data_dir=dd, workflow="cache")
        ok(a.use_cache is True and sq.why_not() is None, "cache on when not disabled")
        r1 = a.agent("auditor", "cache me")
        b = sq.Crew(proj, "g", cfg=cfg, data_dir=dd, workflow="cache")
        r2 = b.agent("auditor", "cache me")
        ok(r1 == r2 and b.cached == 1 and b.records[-1]["totals"]["calls"] == 0, "same def+brief+model+files -> cache hit, 0 calls")
        jl = open(os.path.join(b.dir, "journal.jsonl"), encoding="utf-8").read()
        ok('"type": "cached"' in jl, "a cache hit is marked in the journal")
        with open(os.path.join(proj, "app.py"), "a") as f:
            f.write("# changed\n")
        c = sq.Crew(proj, "g", cfg=cfg, data_dir=dd, workflow="cache")
        c.agent("auditor", "cache me")
        ok(c.cached == 0, "a changed project file -> cache miss")
        d_ = sq.Crew(proj, "g", cfg=cfg, data_dir=dd, workflow="cache", use_cache=False)
        d_.agent("auditor", "cache me")
        ok(d_.cached == 0 and sq.why_not(False), "--no-cache: no hit, and why_not says so")
        ok(sq.cache_stats(dd)["entries"] >= 1, "cache_stats reports the agent-steps layer")
    finally:
        os.environ["ARCHITECT_NO_CACHE"] = "1"
    ok(sq.Crew(proj, "g", cfg=cfg, data_dir=dd, workflow="x").use_cache is False and "NO_CACHE" in sq.why_not(),
       "ARCHITECT_NO_CACHE=1 turns the step cache off")

    # workflow budget
    WB = dict(W, name="budget", budget={"calls": 1})
    vb, cb = sq.run_workflow(WB, proj, "g", cfg=cfg, data_dir=dd, use_cache=False)
    ok(vb == "not_sure", "an exhausted workflow budget ends the run not_sure (never pass)", vb)
    ok(len([r for r in cb.records if r["verdict"] not in ("budget",)]) <= 1 and any(r["verdict"] == "budget" for r in cb.records),
       "budget exhaustion stops further agents", [r["verdict"] for r in cb.records])
    ok(sq.parse_budget("calls=3,tokens=100") == {"calls": 3, "tokens": 100}, "parse_budget")
    try:
        sq.parse_budget("calls=0")
        ok(False, "parse_budget refuses 0")
    except ValueError:
        ok(True, "parse_budget refuses 0")
    st = cb.budget.status()
    ok(st["spent"]["calls"] >= 1 and st["remaining"]["calls"] <= 0, "budget status: spent and remaining", st)

    # inputs / returns
    WI = {"name": "inp", "inputs": {"n": {"type": "integer", "required": True}},
          "returns": {"type": "object", "required": ["findings"]}, "phases": [{"title": "A", "agent": "auditor", "task": "n={n}"}]}
    ok(not sq.validate_workflow(WI), "inputs/returns validate", sq.validate_workflow(WI))
    ok(sq.validate_workflow(dict(WI, inputs={"n": {"type": "weird"}})), "a bad input type is refused")
    vi, _ = sq.run_workflow(WI, proj, "g", cfg=cfg, data_dir=dd)
    ok(vi == "refused", "a missing required input refuses the run", vi)
    vi2, ci2 = sq.run_workflow(WI, proj, "g", vars={"n": "3"}, cfg=cfg, data_dir=dd)
    ok(vi2 == "pass" and ci2.vars["n"] == 3, "input converted; result matches returns:", vi2)
    vi3, _ = sq.run_workflow(dict(WI, returns={"type": "object", "required": ["nope"]}), proj, "g", vars={"n": 1}, cfg=cfg, data_dir=dd)
    ok(vi3 == "fail", "a final result that breaks returns: fails the run", vi3)

    # list_runs + orchestration view
    rows = sq.list_runs(dd)
    r2 = next((r for r in rows if r["run_id"] == c2.run_id), None)
    ok(r2 and r2["replayed"] == 2 and r2["resume_from"] == c1.run_id and r2["status"] == "pass", "list_runs shows replayed/resume_from",
       r2)
    tree = orch.build_crew_run(c2.dir)
    ok(tree["replayed"] == 2 and tree["resume_from"] == c1.run_id and tree["logs"], "orchestration tree reads replay markers and logs",
       {k: tree.get(k) for k in ("replayed", "resume_from")})
    ok("telemetry" in orch.crew_section(data_dir=dd), "crew_section carries a telemetry summary slot")

    # plugin dirs (a fixture module standing in for arch_plugins)
    import types
    pdir = os.path.join(tmp, "plugins", "kit")
    os.makedirs(os.path.join(pdir, "agents"))
    os.makedirs(os.path.join(pdir, "workflows"))
    spec_ = dict(sq._plain(sq.load_agent("auditor")), name="kit-auditor", description="from a plugin")
    sq.save_agent("kit-auditor", spec_, folder=os.path.join(pdir, "agents"))
    sq.save_agent("auditor", dict(spec_, name="auditor", description="plugin clash"), folder=os.path.join(pdir, "agents"))
    with open(os.path.join(pdir, "workflows", "kitflow.yaml"), "w") as f:
        f.write("name: kitflow\nphases:\n  - title: A\n    agent: kit-auditor\n    task: look\n")
    old_mod = sys.modules.get("arch_plugins")
    fake = types.ModuleType("arch_plugins")
    fake.agent_dirs = lambda: [("kit", os.path.join(pdir, "agents"))]
    fake.workflow_dirs = lambda: [("kit", os.path.join(pdir, "workflows"))]
    sys.modules["arch_plugins"] = fake
    try:
        ag = sq.list_agents()
        ok("kit-auditor" in ag and ag["kit-auditor"].get("plugin") == "kit", "plugin agents listed", sorted(ag)[:5])
        ok(ag["auditor"].get("scope") == "shipped" and ag.get("kit:auditor", {}).get("description") == "plugin clash",
           "shipped wins a bare-name clash; the plugin's is kit:auditor")
        ok(sq.load_agent("kit-auditor")["description"] == "from a plugin" and
           sq.load_agent("kit:auditor")["description"] == "plugin clash", "load_agent resolves bare and plugin:name")
        ok("kitflow" in sq.list_workflows() and sq.load_workflow("kitflow")["name"] == "kitflow", "plugin workflows load")
    finally:
        if old_mod is None:
            sys.modules.pop("arch_plugins", None)
        else:
            sys.modules["arch_plugins"] = old_mod
    ok("kit-auditor" not in sq.list_agents(), "no plugin module -> no plugin agents")


def run():
    N[0] = N[1] = 0
    tmp = tempfile.mkdtemp(prefix="squad_st_")
    old_env = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE", "OLLAMA_HOST")}
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    srv = None
    try:
        import arch_core as core
        import arch_squad as sq
        import arch_orchestration as orch
        dd = os.path.join(tmp, "data")
        proj = os.path.join(tmp, "proj")
        os.makedirs(proj)
        with open(os.path.join(proj, "app.py"), "w") as f:
            f.write("def add(a, b):\n    return a + b\n")

        # ---- definitions
        defs = sq.list_agents()
        ok(set(defs) >= {"auditor", "builder", "verifier", "planner", "ui-reviewer"} and all("problems" not in d for d in defs.values()),
           "agents/*.yaml all validate", {k: v.get("problems") for k, v in defs.items()})
        ok(defs["auditor"]["writes"] is False and not any(sq._tools()[t]["writes"] for t in defs["auditor"]["tools"]),
           "auditor is read-only")
        ok("run_tests" in defs["verifier"]["tools"] and isinstance(defs["verifier"]["output"], dict), "verifier runs tests, structured verdict")
        bad = dict(defs["auditor"], tools=["read", "apply"], effort="max", budget={"calls": 0}, model="claude:x")
        bad.pop("_source", None)
        probs = sq.validate(bad)
        ok(any("writes is false" in p for p in probs) and any("effort" in p for p in probs) and any("budget" in p for p in probs)
           and any("claude" in p for p in probs), "a bad definition is refused with reasons", probs)
        w = sq.load_workflow(os.path.join(HERE, "workflows", "audit_build_verify.yaml"))
        ok([p["title"] for p in w["phases"]] == ["Audit", "Build", "Verify"] and len(w["phases"][0]["parallel"]) == 3,
           "sample workflow: audit x3 parallel -> build -> verify")

        srv, over = sq.start_fake()
        cfg = core.deep_merge(core.load_config(proj), over)

        def crew(**kw):
            return sq.Crew(proj, "check app.py", cfg=cfg, data_dir=dd, **kw)

        # ---- parallel respects the slot cap
        c = crew(slots=2, workflow="cap")
        c.phase("Audit")
        res = c.parallel([{"agent": "auditor", "task": "FAKE:SLOW look", "label": f"a{i}"} for i in range(4)])
        ok(all(isinstance(r, dict) and "findings" in r for r in res), "parallel: every result schema-valid", res)
        ok(c.peak_concurrency == 2, f"parallel: at most 2 at once with 2 slots (peak {c.peak_concurrency})")
        c1 = crew(slots=1, workflow="cap1")
        c1.parallel([{"agent": "auditor", "task": "FAKE:SLOW", "label": f"b{i}"} for i in range(2)])
        ok(c1.peak_concurrency == 1, "parallel: one slot = one at a time")
        ok(crew().slots == max(1, int(cfg["server"].get("parallel") or 1)), "default cap = Settings > Server > parallel")

        # ---- pipeline has no barrier
        c = crew(slots=4, workflow="pipe")
        log, lk = [], threading.Lock()

        def s1(prev, item):
            time.sleep(0.4 if item == "slow" else 0.01)
            with lk:
                log.append(("s1", item, time.time()))
            return item + ":1"

        def s2(prev, item):
            with lk:
                log.append(("s2", item, time.time()))
            return prev + ":2"
        out = c.pipeline(["slow", "fast"], s1, s2)
        t_fast_s2 = [t for s, i, t in log if s == "s2" and i == "fast"][0]
        t_slow_s1 = [t for s, i, t in log if s == "s1" and i == "slow"][0]
        ok(out == ["slow:1:2", "fast:1:2"], "pipeline: results in item order", out)
        ok(t_fast_s2 < t_slow_s1, "pipeline: no barrier (fast item finished stage 2 before slow item finished stage 1)")
        out = c.pipeline(["x"], {"agent": "auditor", "task": "look at {item}"}, {"agent": "verifier", "task": "check {item}"})
        ok(isinstance(out[0], dict) and out[0].get("verdict") == "pass", "pipeline of agents: verifier gets the auditor's result", out)
        ok(c.pipeline(["y"], lambda p, i: None, s2) == [None], "pipeline: a stage returning None ends that item")

        # ---- failure -> None, not a crash
        c = crew(slots=2, workflow="fail")
        res = c.parallel([{"agent": "auditor", "task": "FAKE:GARBAGE"}, {"agent": "auditor", "task": "fine"}, lambda: 1 / 0])
        ok(res[0] is None and isinstance(res[1], dict) and res[2] is None, "a failing agent yields None; the others still answer", res)
        r0 = [r for r in c.records if "FAKE:GARBAGE" in (r.get("brief") or "")][0]
        ok(r0["verdict"] == "failed", "the failure is recorded with a reason", r0.get("why"))
        bad_engine = crew(engine_factory=lambda d, o: (_ for _ in ()).throw(RuntimeError("no engine")))
        ok(bad_engine.agent("auditor", "x") is None and bad_engine.records[-1]["verdict"] == "failed", "an engine that cannot start: None")

        # ---- schema refusal
        c = crew(workflow="schema")
        ok(c.agent("auditor", "FAKE:BADSCHEMA") is None and c.records[-1]["verdict"] == "schema", "final answer that breaks the schema is refused",
           c.records[-1].get("why"))
        txt = c.agent("ui-reviewer", "look at the page")
        ok(isinstance(txt, str) and txt, "free-text agent returns text")

        # ---- ownership
        c = crew(workflow="own")
        try:
            c.parallel([{"agent": "builder", "owns": ["app.py"], "task": "FAKE:WRITE app.py"},
                        {"agent": "builder", "owns": "app.py", "task": "FAKE:WRITE app.py"}])
            clash = False
        except sq.OwnershipClash:
            clash = True
        ok(clash and not c.records, "ownership clash refused before any agent runs")
        try:
            c.parallel([{"agent": "builder", "owns": ["pkg"]}, {"agent": "builder", "owns": ["pkg/a.py"]}])
            ok(False, "a folder and a file inside it clash")
        except sq.OwnershipClash:
            ok(True, "a folder and a file inside it clash")
        ok(sq.ownership_problems([("x", ["../etc/passwd"])]) != [], "a path outside the project is refused")
        r = c.agent("builder", "FAKE:WRITE other.py", owns=["app.py"])
        ok(not os.path.exists(os.path.join(proj, "other.py")), "a write to a file the agent does not own is refused", r)
        r = c.agent("builder", "FAKE:WRITE new_mod.py", owns=["new_mod.py"])
        ok(os.path.exists(os.path.join(proj, "new_mod.py")) and "new_mod.py" in c.records[-1]["edited"], "a write to an owned file lands",
           c.records[-1])
        c.agent("builder", "FAKE:WRITE app.py")
        ok("apply" not in c.records[-1]["tools"] and "owns nothing" in c.records[-1].get("note", ""),
           "a writer that owns nothing gets no write tools")

        # ---- budgets stop an agent (through Engine.push_budget)
        c = crew(workflow="budget")
        ok(c.agent("auditor", "look", budget={"calls": 1}) is None and c.records[-1]["verdict"] == "budget",
           "a 1-call budget stops a 2-call agent", c.records[-1].get("why"))

        # ---- journal written and read back with categories, same columns as Claude
        verdict, cw = sq.run_workflow(w, proj, "check app.py", vars={"owns": ""}, cfg=cfg, data_dir=dd, slots=2)
        ok(verdict == "pass" and len(cw.records) == 5 and cw.peak_concurrency <= 2, f"fake end-to-end run of the sample workflow: {verdict}",
           [(r["label"], r["verdict"], r["why"]) for r in cw.records])
        jp = os.path.join(cw.dir, "journal.jsonl")
        ev = [json.loads(x)["type"] for x in open(jp, encoding="utf-8")]
        ok(ev[0] == "launched" and ev[-1] == "done" and ev.count("started") == 5 and ev.count("result") == 5, "journal events", ev)
        rec = json.load(open(os.path.join(cw.dir, "agent-a001.json"), encoding="utf-8"))
        ok(all(k in rec for k in ("brief", "tools", "totals", "result", "verdict", "owns", "budget")) and rec["totals"]["calls"] >= 2
           and rec["totals"]["tokens_out"] > 0, "per-agent record: brief, tools, tokens, verdict", rec.get("totals"))
        snap = orch.snapshot(limit=1, claude_base=os.path.join(tmp, "none"), data_dir=dd, roots=[], project_root=proj, categories=list(orch.CATEGORIES))
        wfs = (snap.get("crew") or {}).get("workflows") or []
        mine = [x for x in wfs if x["id"] == cw.run_id]
        ok(mine and [p["label"] for p in mine[0]["children"]] == ["Audit", "Build", "Verify"], "arch_orchestration reads the crew run: phases",
           snap.get("crew"))
        rows = orch.rows(mine[0]) if mine else []
        ok(len(rows) == 5 and all(set(r) == set(orch.COLUMNS) and r["source"] == "local" for r in rows),
           "crew agents have the same columns as Claude agents")
        cats = {r["label"]: r["category"] for r in rows}
        ok(cats.get("audit:correctness") == "engineering" and cats.get("build") == "coding" and cats.get("verify") == "harness",
           "categories from the agent definitions", cats)
        a1 = mine[0]["children"][0]["children"][0] if mine else {}
        ok(a1.get("collapsed_default") is True and a1.get("tokens", {}).get("output", 0) > 0, "tiny read-only agent starts collapsed")
        only = orch.snapshot(limit=1, claude_base=os.path.join(tmp, "none"), data_dir=dd, roots=[], project_root=proj, categories=["coding"])
        m2 = [x for x in only["crew"]["workflows"] if x["id"] == cw.run_id]
        ok(m2 and [p["label"] for p in m2[0]["children"]] == ["Build"] and only["categories"]["folded"].get("harness", 0) >= 1,
           "snapshot(categories=['coding']) keeps the builder and folds the rest", only["categories"])

        # ---- the coding crew: every agent and workflow validates
        crew_names = {"router", "planner", "architect", "spec-extractor", "test-author", "coder", "debugger", "refactorer", "reviewer",
                      "security-reviewer", "docs-writer", "explainer", "ui-reviewer", "perf-tuner", "auditor", "builder", "verifier"}
        ok(crew_names <= set(defs) and all("problems" not in defs[n] for n in crew_names), "all 17 crew agents validate",
           sorted(crew_names - set(defs)))
        ok(all(defs[n]["model"] in sq.TIERS and defs[n]["budget"] and defs[n]["effort"] in sq.EFFORTS and defs[n]["context"]["policy"]
               for n in crew_names), "every agent has a tier, budget, effort and context policy")
        ro = ["reviewer", "security-reviewer", "explainer", "debugger", "perf-tuner", "architect", "router"]
        ok(all(not defs[n]["writes"] and not any(sq._tools()[t]["writes"] for t in defs[n]["tools"]) for n in ro),
           "reviewers, explainer, debugger, perf-tuner, architect, router are read-only")
        ok(defs["test-author"]["owns"] == ["tests"] and defs["spec-extractor"].get("code") == "spec", "test-author owns tests/; spec-extractor is code")
        wfs_l = sq.list_workflows()
        want_wf = {"bugfix_squad", "feature_squad", "review_squad", "refactor_squad", "explain_squad", "audit_build_verify"}
        ok(want_wf <= set(wfs_l) and not any(wfs_l[k].get("problems") for k in want_wf), "all 6 workflows validate",
           {k: wfs_l.get(k, {}).get("problems") for k in want_wf})
        ok(all(p["kind"] and p["title"] for k in want_wf for p in wfs_l[k]["phases"]) and
           any(p["kind"] == "loop" for p in wfs_l["feature_squad"]["phases"]), "list_workflows: phases with kinds for a UI")
        dsc = sq.describe("coder")
        ok(dsc["type"] == "agent" and "model" in dsc["fields"] and "effort" in dsc["fields"], "describe(agent): fields")
        ok(sq.describe("review_squad")["type"] == "workflow", "describe(workflow)")
        ok(sq.validate_workflow({"name": "x", "phases": [{"title": "a", "agent": "auditor", "when": "__import__('os')"}]}) != [],
           "a when: that calls anything outside the safe evaluator is refused")
        ok(sq.validate_workflow({"name": "x", "phases": [{"title": "l", "loop": {"max": 99, "until": "True",
                                                                                 "stages": [{"agent": "auditor"}]}}]}) != [],
           "a loop over the max is refused")

        def jtypes(cw_):
            return [json.loads(x) for x in open(os.path.join(cw_.dir, "journal.jsonl"), encoding="utf-8")]

        # ---- fake end-to-end run of every workflow
        runs = {}
        for name in sorted(want_wf):
            v, cw_ = sq.run_workflow(name, proj, "add a mul(a, b) function to app.py", vars={"owns": "app.py"}, cfg=cfg, data_dir=dd, slots=2)
            runs[name] = cw_
            ok(v == "pass" and cw_.records, f"fake end-to-end: {name} -> {v}",
               [(r["label"], r["verdict"], r["why"]) for r in cw_.records if r["verdict"] != "ok"])
            ok(all(isinstance(p.get("secs"), float) for p in cw_.phase_times) and
               any(e["type"] == "phase_done" and "secs" in e for e in jtypes(cw_)), f"{name}: per-phase timing journaled")
        b = runs["bugfix_squad"]
        ok(b.loop_counts.get("Fix") == 1 and sum(1 for r in b.records if r["agent"] == "verifier") == 2,
           "bugfix: two verifiers agree on pass, so the fix loop stops after 1", b.loop_counts)
        pan = [e for e in jtypes(b) if e["type"] == "panel"]
        ok(pan and pan[0]["majority"] == "pass" and pan[0]["votes"] == {"pass": 2}, "judge panel: majority journaled", pan)
        f = runs["feature_squad"]
        ok(f.loop_counts.get("Fix loop") == 2 and [r["agent"] for r in f.records][:4] == ["spec-extractor", "architect", "test-author", "coder"],
           "feature: spec -> interface -> tests -> code; review findings keep the fix loop going to its max (2)", f.loop_counts)
        sp = [r for r in f.records if r["agent"] == "spec-extractor"][0]
        ok(sp["model"] == "code" and isinstance(sp["result"], dict) and "interface" in sp["result"], "spec-extractor runs arch_spec.spec in code")
        rv = runs["review_squad"]
        dd_ev = [r for r in rv.records if r["label"] == "verify-finding"]
        ok(len([r for r in rv.records if r["phase"] == "Review"]) == 3 and len(dd_ev) == 1,
           "review: 3 lenses in parallel, identical findings merged, each remaining finding verified", [r["label"] for r in rv.records])
        rf = runs["refactor_squad"]
        ok(any(e["type"] == "skipped" and e["title"] == "Review" for e in jtypes(rf)) and not any(r["phase"] == "Review" for r in rf.records),
           "refactor: when: skips the review when the panel passed")
        ok(len(runs["explain_squad"].records) == 4, "explain: 3 parallel readers -> synthesis")

        # ---- loop stops at max; when: skips
        wl = {"name": "loopmax", "phases": [
            {"title": "Skip me", "when": "goal == 'never'", "agent": "auditor"},
            {"title": "Loop", "loop": {"max": 3, "until": "last.verdict == 'fail'", "stages": [{"agent": "verifier", "label": "v"}]}},
            {"title": "After", "when": "prev.verdict == 'pass'", "agent": "auditor", "label": "after"}]}
        ok(sq.validate_workflow(wl) == [], "inline loop workflow validates", sq.validate_workflow(wl))
        v, cl = sq.run_workflow(wl, proj, "x", cfg=cfg, data_dir=dd)
        ev = jtypes(cl)
        ok([e["iteration"] for e in ev if e["type"] == "loop"] == [1, 2, 3] and cl.loop_counts["Loop"] == 3,
           "a loop whose check never passes stops at max", cl.loop_counts)
        ok(any(e["type"] == "skipped" and e["title"] == "Skip me" for e in ev) and not any(r["phase"] == "Skip me" for r in cl.records)
           and any(r["label"] == "after" for r in cl.records), "when: false skips the phase; when: true runs it")

        # ---- overrides: model, effort, budget per agent, journaled
        ov = sq.parse_sets(["coder.model=other:3b", "coder.effort=low", "coder.budget.calls=9", "slots=1"])
        ok(ov == {"agents": {"coder": {"model": "other:3b", "effort": "low", "budget": {"calls": 9}}}, "slots": 1}, "parse_sets", ov)
        ok(sq.parse_sets(["*.tools=read,grep"])["agents"]["*"]["tools"] == ["read", "grep"], "parse_sets: list values and * for every agent")
        for badset in (["coder"], ["coder.nope=1"], ["slots=0"]):
            try:
                sq.parse_sets(badset)
                ok(False, f"parse_sets refuses {badset}")
            except ValueError:
                ok(True, f"parse_sets refuses {badset}")
        v, co = sq.run_workflow("bugfix_squad", proj, "fix add", vars={"owns": "app.py"}, overrides=ov, cfg=cfg, data_dir=dd)
        cr = [r for r in co.records if r["agent"] == "coder"][0]
        ok(cr["model"] == "other:3b" and cr["effort"] == "low" and cr["budget"]["calls"] == 9 and cr["overrides"]["effort"] == "low",
           "an override changes the coder's model, effort and budget", {k: cr.get(k) for k in ("model", "effort", "budget", "why")})
        dbg = [r for r in co.records if r["agent"] == "debugger"][0]
        ok(dbg["model"] != "other:3b" and dbg["overrides"] == {}, "other agents keep their own definition")
        ev = jtypes(co)
        ok(co.slots == 1 and ev[0]["overrides"]["agents"]["coder"]["model"] == "other:3b" and
           any(e["type"] == "started" and e.get("overrides", {}).get("model") == "other:3b" for e in ev), "overrides journaled (launched + started)")
        v, cc = sq.run_workflow("explain_squad", proj, "x", overrides={"agents": {"explainer": {"model": "claude:opus", "tools": ["apply"]}}},
                                cfg=cfg, data_dir=dd)
        ok(v == "fail" and all(r["verdict"] == "refused" and "claude" in r["why"] for r in cc.records),
           "an override to claude: or a write tool is refused", [r["why"] for r in cc.records][:1])

        # ---- save_agent
        af = os.path.join(tmp, "agents_out")
        good = dict(sq._plain(defs["reviewer"]), name="my-reviewer", description="mine")
        p = sq.save_agent("my-reviewer", good, folder=af)
        ok(os.path.isfile(p) and sq.load_agent(p)["description"] == "mine" and sq._plain(sq.load_agent(p)) == sq._plain(sq._norm(good)),
           "save_agent writes a definition that loads back the same")
        for why, spec_ in (("claude model", dict(good, model="claude:x")), ("write tool on a read-only agent", dict(good, tools=["apply"])),
                           ("name mismatch", dict(good, name="other")), ("bad effort", dict(good, effort="max"))):
            try:
                sq.save_agent("my-reviewer2", spec_, folder=af)
                ok(False, f"save_agent refuses: {why}")
            except ValueError:
                ok(not os.path.exists(os.path.join(af, "my-reviewer2.yaml")), f"save_agent refuses: {why} (nothing written)")
        ok(not [x for x in os.listdir(af) if ".tmp" in x], "save_agent leaves no temp file")

        # ---- resume, step cache, workflow budget, log lines, inputs/returns, plugin dirs
        resume_checks(sq, orch, core, proj, dd, cfg, tmp)

        # ---- the CLI
        import architect
        rc = architect.main(["crew", "bugfix_squad", "--goal", "fix", "--root", proj, "--fake", "--owns", "app.py",
                             "--set", "coder.effort=low"])
        ok(rc == 0, "architect.py crew bugfix_squad --fake --set coder.effort=low runs and passes")
        ok(sq.main(["--set", "bad"]) == 2 and sq.main(["--workflows"]) == 0, "main: a bad --set is refused; --workflows lists them")
        rc = architect.main(["crew", os.path.join(HERE, "workflows", "audit_build_verify.yaml"), "--goal", "check", "--root", proj, "--fake",
                             "--slots", "2"])
        ok(rc == 0, "architect.py crew <workflow> --fake runs and passes")
    finally:
        if srv is not None:
            srv.shutdown()
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{N[0]} passed, {N[1]} failed")
    return N[1] == 0


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
