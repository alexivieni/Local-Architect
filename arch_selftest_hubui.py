"""Self-tests for the hub additions of 2026-11-04: Blueprints (flow_save + the validator), the Crew cost/schedule endpoints (guarded,
because arch_costs / arch_agents.schedule_* may not be installed), the scheduler start, and the page itself (Council / Crew names,
the Blueprints page, the request diagram, the "?" drawers).

    python arch_selftest_hubui.py

Runs against a throwaway data folder and a throwaway flows folder; nothing is written into the real flows/.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    def refused(fn, *a, **k):
        try:
            fn(*a, **k)
        except ValueError as ex:
            return str(ex) or True
        return False

    tmp = tempfile.mkdtemp(prefix="archhubui_")
    old_env = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE")}
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    saved_mods = {k: sys.modules.get(k) for k in ("arch_costs", "arch_agents")}
    try:
        import arch_core as core
        old_home = core.HOME
        core.set_home(os.path.join(tmp, "home"))
        import arch_ci
        import arch_dataflow as adf
        import architect_hub as hub
        flows = os.path.join(tmp, "flows")
        os.makedirs(flows)

        # ---------------------------------------------------------------- the kit
        kit = hub.blueprint_kit()
        check("blueprint kit: the eight step kinds the palette offers, three tiers, and the registered gates",
              kit["kinds"] == ["generate", "code", "ask_json", "judge", "doc", "sample_n", "cascade", "tool_loop"]
              and kit["tiers"] == ["router", "worker", "heavy"] and len(kit["gates"]) >= 5, (kit["kinds"], len(kit["gates"])))
        check("blueprint kit: every built-in workflow id is listed as taken", {"bugfix", "build", "review"} <= set(kit["taken"]), kit["taken"][:10])
        check("blueprint kit: prompt files are offered as folder/name", kit["prompts"] and all("/" in p for p in kit["prompts"]), kit["prompts"][:5])
        kr = hub.blueprint_kit("review")
        check("blueprint kit for a flow: each step's prompt comes back qualified (review -> review/review), so a loaded copy keeps it",
              kr.get("prompts_of", {}).get("notes") == "review/review" and kr.get("blueprint") is False, kr.get("prompts_of"))
        gate = kit["gates"][0]["name"]
        prompt = "review/review" if "review/review" in kit["prompts"] else kit["prompts"][0]

        def steps(*extra):
            return [{"id": "write", "kind": "generate", "title": "Write it", "tier": "worker", "prompt": prompt, "check": "tests", "retries": 2},
                    {"id": "gate_it", "kind": "code", "title": "Check it", "check": gate}] + list(extra)

        # ---------------------------------------------------------------- refusals before anything is written
        check("flow_save refuses an id that is not [a-z][a-z0-9_]{2,30}",
              all(refused(hub.save_blueprint, {"id": bad, "steps": steps()}, flows) for bad in ("ab", "Bad", "1abc", "a-b-c", "x" * 32, "../evil", "")))
        why = refused(hub.save_blueprint, {"id": "bugfix", "steps": steps()}, flows)
        check("flow_save refuses the id of a built-in workflow, and says so", why and "built-in" in str(why), why)
        check("flow_save refuses a step kind a blueprint cannot hold, a tier that does not exist, a malformed step id, and 31 steps",
              refused(hub.save_blueprint, {"id": "bp_k", "steps": [{"kind": "report"}]}, flows)
              and refused(hub.save_blueprint, {"id": "bp_k", "steps": [{"kind": "generate", "tier": "gpu"}]}, flows)
              and refused(hub.save_blueprint, {"id": "bp_k", "steps": [{"kind": "generate", "id": "Bad Id"}]}, flows)
              and refused(hub.save_blueprint, {"id": "bp_k", "steps": [{"kind": "code", "id": f"s{i}"} for i in range(31)]}, flows)
              and refused(hub.save_blueprint, {"id": "bp_k", "steps": []}, flows))
        check("nothing was written by any refused request", os.listdir(flows) == [], os.listdir(flows))

        # ---------------------------------------------------------------- a valid blueprint
        r = hub.save_blueprint({"id": "bp_ok", "title": 'Say "hi": #1\nnext', "steps": steps()}, flows)
        path = os.path.join(flows, "bp_ok.yaml")
        check("a valid blueprint is saved as flows/<id>.yaml and the validator finds no problems", r["ok"] and r["path"] == "flows/bp_ok.yaml" and os.path.isfile(path)
              and r["problems"] == [], r)
        doc = adf.load_yaml_file(path) if os.path.isfile(path) else {}
        check("the saved file validates on its own (arch_dataflow.validate, as `flows --validate` runs it)", adf.validate(doc) == [], adf.validate(doc))
        check("it is marked as a blueprint, keeps the step order, the tier, the prompt, the check and the retries",
              doc.get("blueprint") is True and [s["id"] for s in doc["steps"]] == ["write", "gate_it"] and doc["steps"][0]["tier"] == "worker"
              and doc["steps"][0]["prompt"] == prompt and doc["steps"][0]["verify"] == {"run_tests": True} and doc["steps"][0]["retries"] == 2
              and doc["steps"][1]["gate"] == gate and doc["verdict"]["pass_if"] == "write.ok and gate_it.ok", doc)
        check("a title with quotes, a colon, a hash and a newline reads back as one safe line", doc.get("title") == "Say 'hi': #1 next", doc.get("title"))
        saved_py = arch_ci._pyyaml
        try:
            arch_ci._pyyaml = None                               # the built-in reader, as on a machine without PyYAML
            with open(path, encoding="utf-8") as f:
                mini = arch_ci.load_yaml(f.read())
        finally:
            arch_ci._pyyaml = saved_py
        check("the file reads back identically with the built-in YAML reader (no PyYAML needed)", mini == doc, (mini, doc))
        check("a blueprint drawn here is the person's own: its id is not counted as built in", "bp_ok" not in hub._bp_builtin_ids(flows))

        # ---------------------------------------------------------------- replacing your own
        why = refused(hub.save_blueprint, {"id": "bp_ok", "steps": steps()}, flows)
        check("saving over an existing blueprint needs 'Replace' (overwrite: true)", why and "Replace" in str(why), why)
        r2 = hub.save_blueprint({"id": "bp_ok", "title": "Reordered", "overwrite": True, "steps": list(reversed(steps()))}, flows)
        doc2 = adf.load_yaml_file(path)
        check("with overwrite the blueprint is replaced, in the new order", r2["ok"] and r2["replaced"] and [s["id"] for s in doc2["steps"]] == ["gate_it", "write"], r2)
        with open(path, encoding="utf-8") as f:
            good_text = f.read()

        # ---------------------------------------------------------------- the validator says no
        r3 = hub.save_blueprint({"id": "bp_bad", "steps": [{"id": "gate_it", "kind": "code", "check": "no_such_gate"}]}, flows)
        check("a blueprint the validator refuses is removed again, and its problems are returned word for word",
              not r3["ok"] and not os.path.exists(os.path.join(flows, "bp_bad.yaml")) and any("unknown gate" in p for p in r3["validator"]), r3)
        r4 = hub.save_blueprint({"id": "bp_ok", "overwrite": True, "steps": [{"id": "goal", "kind": "code", "check": gate}]}, flows)
        with open(path, encoding="utf-8") as f:
            after = f.read()
        check("a refused overwrite puts the earlier blueprint back exactly as it was", not r4["ok"] and r4.get("kept") and after == good_text, r4)
        check("the validator's reserved-name rule reaches the page (a step called 'goal')", any("would hide" in p for p in r4["problems"]), r4["problems"])
        r5 = hub.save_blueprint({"id": "bp_np", "steps": [{"id": "write", "kind": "generate", "tier": "worker"}]}, flows)
        check("a model step without a prompt file is refused (it could not run), and nothing is left on disk",
              not r5["ok"] and any("needs a prompt" in p for p in r5["problems"]) and not os.path.exists(os.path.join(flows, "bp_np.yaml")), r5)
        r6 = hub.save_blueprint({"id": "bp_dry", "dry": True, "steps": steps({"id": "rev", "kind": "judge", "title": "Review"})}, flows)
        check("'Check without saving' validates the same text and writes nothing", r6["ok"] and r6["dry"] and not os.path.exists(os.path.join(flows, "bp_dry.yaml"))
              and "kind: judge" in r6["yaml"], r6)
        why = refused(hub.save_blueprint, {"id": "bp_dup", "steps": [{"id": "a", "kind": "code", "check": gate}, {"id": "a", "kind": "code", "check": gate}]}, flows)
        check("two steps with the same id are refused with a clear message", why and "twice" in str(why), why)

        # ---------------------------------------------------------------- costs (guarded)
        fake_costs = types.ModuleType("arch_costs")

        def broken(days=7):
            raise RuntimeError("no ledger yet")
        fake_costs.summary = broken
        sys.modules["arch_costs"] = fake_costs
        c1 = hub.costs_summary(7)
        check("costs: an error inside arch_costs.summary comes back as available False with the reason, never a crash",
              c1["available"] is False and "no ledger yet" in c1["error"], c1)
        fake_costs.summary = lambda days=7: {"days": days, "runs": 4, "passed": 3, "cost_usd": 0.42, "per_task_usd": 0.14, "local_share": 0.75, "by_model": []}
        c2 = hub.costs_summary(7)
        check("costs: arch_costs.summary(days=7) is passed through with available True", c2["available"] and c2["runs"] == 4 and c2["days"] == 7, c2)
        sys.modules["arch_costs"] = types.ModuleType("arch_costs")       # a module without summary()
        check("costs: a module without summary() is reported, not raised", hub.costs_summary(7)["available"] is False)

        # ---------------------------------------------------------------- schedule (guarded)
        fake = types.ModuleType("arch_agents")
        store = []
        started = []
        fake.schedule_list = lambda: list(store)

        def add(agent_id, message, at="09:00", repeat="daily", environment_id=None, repo_url=None):
            store.append({"id": f"s{len(store) + 1}", "agent_id": agent_id, "message": message, "at": at, "repeat": repeat,
                          "environment_id": environment_id, "repo_url": repo_url})
            return store[-1]

        def remove(sid):
            n = len(store)
            store[:] = [s for s in store if s["id"] != sid]
            return len(store) < n
        fake.schedule_add, fake.schedule_remove = add, remove
        fake.start_scheduler = lambda: started.append(1)
        sys.modules["arch_agents"] = fake
        ok_add = hub.crew_schedule_change({"add": {"agent_id": "agent_abc123", "message": "triage issues", "at": "07:30", "repeat": "daily",
                                                   "repo_url": "https://github.com/you/repo"}})
        check("schedule: a valid job is handed to arch_agents.schedule_add with the exact arguments, and the list comes back",
              ok_add["ok"] and len(ok_add["schedules"]) == 1 and store[0]["at"] == "07:30" and store[0]["repo_url"] == "https://github.com/you/repo"
              and store[0]["environment_id"] is None, ok_add)
        check("schedule: a bad time, a bad agent id, a non-GitHub repo, an unknown repeat and an empty message are refused before anything is called",
              refused(hub.crew_schedule_change, {"add": {"agent_id": "agent_abc123", "message": "x", "at": "25:00"}})
              and refused(hub.crew_schedule_change, {"add": {"agent_id": "evil", "message": "x", "at": "07:00"}})
              and refused(hub.crew_schedule_change, {"add": {"agent_id": "agent_abc123", "message": "x", "at": "07:00", "repo_url": "http://x.example/r"}})
              and refused(hub.crew_schedule_change, {"add": {"agent_id": "agent_abc123", "message": "x", "at": "07:00", "repeat": "hourly"}})
              and refused(hub.crew_schedule_change, {"add": {"agent_id": "agent_abc123", "message": " ", "at": "07:00"}})
              and len(store) == 1)
        rm = hub.crew_schedule_change({"remove": "s1"})
        check("schedule: remove goes to schedule_remove and the list comes back empty", rm["ok"] and rm["schedules"] == [], rm)
        check("schedule: a malformed id is refused", refused(hub.crew_schedule_change, {"remove": "../x"}))
        check("the hub starts the crew scheduler once at start-up", hub.start_crew_scheduler() is True and started == [1])
        sys.modules["arch_agents"] = types.ModuleType("arch_agents")       # functions missing: the hub must still start
        check("with the scheduler functions missing the hub still starts and the page is told why",
              hub.start_crew_scheduler() is False and hub.crew_schedule_list()["available"] is False
              and "missing" in str(refused(hub.crew_schedule_change, {"add": {"agent_id": "agent_abc123", "message": "x", "at": "07:00"}})))
        for k, v in saved_mods.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

        # ---------------------------------------------------------------- the page
        html = open(os.path.join(HERE, "hub_ui.html"), encoding="utf-8").read()
        check("visible names: no 'Ultracode' or 'Claude agents' label is left; Council and Crew are there",
              '"Ultracode"' not in html and '"Claude agents"' not in html and 'h("span",{class:"mmname"},"Council"' in html and '["crewbuild","crew","Crew builder"' in html)  # 2026-11-05: Crew lives on as the Crew builder page
        check("the stored setting key and the old trigger words are untouched (chatUltra is still what the toggle saves)",
              html.count('store.set("chatUltra"') == 1 and "chatUltra" in html)
        check("the word 'council' in a message sends ultra:true (Console and Studio both use chatFlags)",
              r"/\bcouncil\b/" in html and html.count("ultra:fl.ultra") == 2 and 'ultra:store.get("chatUltra","0")==="1"}' not in html)
        check("the Council toggle explains itself: several agents debate, skeptics challenge, one lead rules",
              "skeptics" in html and "one lead" in html and "title:COUNCIL_TIP" in html)
        # 2026-11-05: the places live in SIDEBAR ([section, [[place, icon, label, [sub-tab page ids]]]]); TABS is derived from it.
        side_src = re.search(r"const SIDEBAR=\[(.*?)\n\];", html, re.S)
        places = re.findall(r'\["([a-z]+)","[^"]*","[^"]*",\[([^\]]*)\]\]', side_src.group(1)) if side_src else []
        tab_pages = {pl: re.findall(r'"([a-z]+)"', g) for pl, g in places}
        nav = re.search(r"const NAV_GROUPS=\[(.*?)\];\s*const PAGES", html, re.S)
        page_ids = re.findall(r'\["([a-z]+)","[^"]*","[^"]*","[^"]*"\]', nav.group(1)) if nav else []
        flat = [x for g in tab_pages.values() for x in g]
        check("Blueprints is a page in the Workflows place, and every page is still reachable from exactly one place",
              "blueprints" in page_ids and "blueprints" in tab_pages.get("workflows", []) and sorted(flat) == sorted(page_ids)
              and len(set(flat)) == len(flat), (sorted(set(page_ids) ^ set(flat)), [x for x in flat if flat.count(x) > 1]))
        check("Blueprints: drag and drop with HTML5 events, a palette of the eight kinds, a click-to-edit card, and an SVG line",
              all(s in html for s in ("ondragstart:", "ondragover:bpOver", "ondrop:bpDrop", 'class="bp-wire"', "function bpEditor", "function bpDrawPal"))
              and all(f"{k}:[" in html for k in ("generate", "code", "ask_json", "judge", "doc", "sample_n", "cascade", "tool_loop")))
        check("Blueprints: loads with /api/arch/flowmap, saves to /api/arch/flow_save, shows the validator's answer",
              '"/api/arch/flowmap?flow="+encodeURIComponent(fid)' in html and '"/api/arch/flow_save"' in html and "function bpShowResult" in html)
        check("Blueprints: moving steps also works without a mouse (arrows, Alt+Up/Down) for phones and keyboards",
              "Move step " in html and 'e.altKey&&(e.key==="ArrowUp"' in html)
        check("the request diagram sits on the Console above the live panel, is collapsible, and dims Claude while online is off",
              'h("div",{class:"strip",id:"cStrip"}),routeViz()' in html and '"aria-expanded"' in html and "cloudOn=!!(S.cloud&&S.cloud.on)" in html
              and "EV.nowTier" in html and "EV.lanes" in html and "renderRoute();" in html)
        check("Crew shows the week's costs and the schedule (list, remove, add)",
              '"/api/arch/costs?days=7"' in html and '"/api/arch/crew_schedule"' in html and "{remove:sid}" in html and "Add to the schedule" in html)
        check("a '?' explainer on the Console, Crew and Blueprints pages, closed by Esc before Esc can stop a run",
              all(f'helpBtn("{k}")' in html for k in ("console", "crew", "blueprints")) and html.index("if(HELP_EL){e.preventDefault();closeHelp();return;}") < html.index("if(EV.running){e.preventDefault();"))
        check("error notes have their own colours (note bad was unstyled)", ".note.bad{" in html)
        blocks = [b.replace('"__HUB_TOKEN__"', '"x"') for b in re.findall(r"<script>(.*?)</script>", html, re.S)]
        js = "\n".join(blocks)
        if shutil.which("node"):
            jp = os.path.join(tmp, "hub.js")
            with open(jp, "w", encoding="utf-8") as f:
                f.write(js)
            p = subprocess.run(["node", "--check", jp], capture_output=True, text=True, timeout=60)
            check("the hub page's script parses (node --check)", p.returncode == 0, p.stderr[-400:])
        else:
            check("the hub page's script parses (node not installed: brackets balanced instead)",
                  js.count("(") == js.count(")") and js.count("{") == js.count("}") and js.count("[") == js.count("]"))
        core.set_home(old_home)
    finally:
        for k, v in saved_mods.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' HUB UI CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' HUB UI CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
