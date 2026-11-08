"""Self-test for your versions (arch_custom): agents, crew workflows, flows, prompts and the launch profile can be edited, checked,
saved, tried and taken back, and a saved change is what the next run actually uses. The shipped files are never written. No model,
no GPU, no network: everything runs in a throwaway home."""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="archcustom_")
_KEYS = ("ARCHITECT_HOME", "ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE", "ARCHITECT_SELFTEST")
_OLD = {k: os.environ.get(k) for k in _KEYS}


def _shipped_snapshot():
    out = {}
    for d in ("agents", "workflows", "flows", "prompts"):
        for dp, _dn, fns in os.walk(os.path.join(HERE, d)):
            for fn in fns:
                p = os.path.join(dp, fn)
                st = os.stat(p)
                out[p] = (st.st_size, st.st_mtime_ns)
    return out


def run():
    os.environ["ARCHITECT_HOME"] = os.path.join(_TMP, "home")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(_TMP, "data")
    os.environ["ARCHITECT_NO_CACHE"] = os.environ["ARCHITECT_SELFTEST"] = "1"
    for k in ("ARCHITECT_HOME", "ARCHITECT_DATA_DIR"):
        os.makedirs(os.environ[k], exist_ok=True)
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_core as core
    old_home = core.HOME
    core.set_home(os.environ["ARCHITECT_HOME"])
    before = _shipped_snapshot()
    try:
        import arch_custom as C
        import arch_squad as S
        import arch_flows
        import arch_dataflow as df
        check("your versions live in the data folder, not the install", C.root_dir().startswith(os.environ["ARCHITECT_DATA_DIR"]), C.root_dir())

        # ---- names never leave the folder
        bad = 0
        for kind, n in (("agents", "../x"), ("flows", "a/b"), ("prompts", "../../x"), ("workflows", "x/../../y"), ("agents", "C:/x")):
            try:
                C.custom_path(kind, n)
            except ValueError:
                bad += 1
        check("a name that would leave its folder is refused", bad == 5, bad)

        # ---- agents: the edit is what loads next
        cur = S.load_agent("app-scaffolder")
        r0 = C.read("agents", "app-scaffolder")
        check("an untouched agent reads as built-in", r0["source"] == "built-in" and r0["builtin"], r0["source"])
        import architect_hub as ah
        res = ah.squad_save_agent({"name": "app-scaffolder", "spec": {"budget": dict(cur["budget"], calls=15)}})
        now = S.load_agent("app-scaffolder")
        check("an agent edited in Crew builder is the one that loads next (it used to be silently ignored)",
              res["ok"] and now["budget"]["calls"] == 15 and now["_source"].startswith(C.custom_dir("agents")), (res, now["_source"]))
        check("fields the form does not show are kept (the agent's instructions)", now.get("system") == cur.get("system"))
        listed = S.list_agents()["app-scaffolder"]
        check("the agent list shows your version", listed.get("scope") == "custom" and listed["budget"]["calls"] == 15, listed.get("scope"))
        txt = C.read("agents", "app-scaffolder")["text"].replace(cur["system"].strip().splitlines()[0], "You start projects. Edited.")
        sv = C.save("agents", "app-scaffolder", txt, note="instructions")
        check("an agent's instructions can be edited as text, and load", sv["ok"] and "You start projects. Edited." in S.load_agent("app-scaffolder")["system"], sv)
        bad_agent = C.save("agents", "app-scaffolder", "---\nname: app-scaffolder\nmodel: worker\neffort: extreme\n---\nx\n")
        check("an invalid agent is refused with the reason, and nothing is written", not bad_agent["ok"] and bad_agent["problems"]
              and "Edited." in S.load_agent("app-scaffolder")["system"], bad_agent)
        vs = C.versions("agents", "app-scaffolder")
        check("every save is kept as a version, newest first, the built-in starting point included",
              len(vs) == 3 and "built-in" in vs[-1]["note"], [v["note"] for v in vs])
        C.restore("agents", "app-scaffolder", vs[-1]["id"])
        check("restoring the built-in version brings its budget back (and is itself a new version)",
              S.load_agent("app-scaffolder")["budget"]["calls"] == cur["budget"]["calls"] and len(C.versions("agents", "app-scaffolder")) == 4)
        C.reset("agents", "app-scaffolder")
        check("reset to built-in: the shipped file loads again", S.load_agent("app-scaffolder")["_source"].endswith(os.path.join("agents", "app-scaffolder.md")))
        new = ah.squad_save_agent({"name": "my-helper", "spec": {"description": "A helper of mine", "model": "worker", "system": "You help.",
                                                                  "tools": ["ls", "read"], "budget": {"calls": 5, "tokens": 20000, "seconds": 300}}})
        check("a NEW agent can be made from the hub, and is listed", new["ok"] and "my-helper" in S.list_agents(), new)

        # ---- crew workflows
        wname = sorted(S.list_workflows())[0]
        wtext = C.read("workflows", wname)["text"]
        check("a crew workflow reads as built-in with its text", C.read("workflows", wname)["source"] == "built-in" and wtext)
        sv = C.save("workflows", wname, wtext.replace("description:", "description: (mine)", 1) if "description:" in wtext else wtext + "description: mine\n")
        lw = S.list_workflows()[wname]
        check("an edited crew workflow is the one listed and run (its path is yours)", sv["ok"] and lw["source"] == "custom"
              and lw["path"].startswith(C.custom_dir("workflows")) and ah._workflow_path(wname).startswith(C.custom_dir("workflows")), (sv, lw.get("path")))
        check("a broken crew workflow is refused", not C.save("workflows", wname, "name: x\nphases: []\n")["ok"])

        # ---- flows and prompts
        ftext = C.read("flows", "review")["text"]
        check("a built-in flow can be read for editing", "id: review" in ftext)
        wrong = C.save("flows", "review", ftext.replace("id: review", "id: other", 1))
        check("a flow whose id is not its name is refused", not wrong["ok"] and "id" in wrong["problems"][0], wrong)
        mine = ftext.replace("title: ", "title: My ", 1)
        sv = C.save("flows", "review", mine)
        check("an edited flow is saved as your version", sv["ok"] and df.flow_file("review").startswith(C.custom_dir("flows")), sv)
        reg = {"review": arch_flows.FLOWS.get("review")}
        df.register(reg, "legacy")
        check("with the Python engine, a flow you changed still runs from your YAML (your change takes effect)",
              isinstance(reg["review"], df.DataFlow) and reg["review"].customized and reg["review"].title.startswith("My "), type(reg["review"]).__name__)
        check("the flow page reads your version", df.flow_detail("review").get("customized") is True)
        C.reset("flows", "review")
        df.register(reg, "legacy")
        check("reset to built-in: the Python class runs again under the Python engine", not isinstance(reg["review"], df.DataFlow), type(reg["review"]).__name__)
        pname = sorted(f for f in os.listdir(os.path.join(HERE, "prompts", "bugfix")) if f.endswith(".md"))[0][:-3]
        pt = C.read("prompts", f"bugfix/{pname}")["text"]
        bad_p = C.save("prompts", f"bugfix/{pname}", "{feedback}\nthen more text\n")
        check("an instructions file with {feedback} before the end is refused (it would break the model server's prefix cache)", not bad_p["ok"], bad_p)
        C.save("prompts", f"bugfix/{pname}", "MY WORDS FIRST.\n" + pt)
        check("an edited instructions file is what the step loads", df.load_prompt("bugfix", pname).startswith("MY WORDS FIRST."))
        check("a flow with one of your prompts runs from its YAML too", "bugfix" in C.customized_flow_ids())
        reg2 = {"bugfix": arch_flows.FLOWS.get("bugfix")}
        df.register(reg2, "legacy")
        check("... under the Python engine as well", isinstance(reg2["bugfix"], df.DataFlow))
        C.reset("prompts", f"bugfix/{pname}")
        check("reset: the shipped instructions load again", df.load_prompt("bugfix", pname) == pt)

        # ---- the flow iterator adopts into your versions, never the install
        import arch_iterate
        check("the iterator reads the flow a run uses (yours when you have one)", arch_iterate.flow_path("review") == df.flow_file("review"))

        # ---- launch profile
        proj = os.path.join(_TMP, "proj")
        os.makedirs(proj, exist_ok=True)
        import arch_runner
        check("no profile: no run command", arch_runner.default_cmd(proj) is None)
        bad_l = C.launch_save(proj, {"cmd": "", "port": "99999", "env": {"1BAD": "x"}, "network": "everywhere"})
        check("a launch profile with mistakes is refused with each one named", not bad_l["ok"] and len(bad_l["problems"]) == 4, bad_l)
        ok_l = C.launch_save(proj, {"cmd": "python app.py", "port": "8123", "env": {"MODE": "dev"}, "network": "offline"})
        check("a saved launch profile is what Start uses (command, port, environment, network)",
              ok_l["ok"] and arch_runner.default_cmd(proj) == "python app.py" and arch_runner.launch_profile(proj)["port"] == 8123
              and arch_runner.launch_profile(proj)["env"] == {"MODE": "dev"}, ok_l)
        C.launch_save(proj, {"cmd": "python other.py"})
        vs = C.launch_read(proj)["versions"]
        C.launch_restore(proj, vs[-1])
        check("an earlier launch profile can be restored", arch_runner.default_cmd(proj) == "python app.py" and len(C.launch_read(proj)["versions"]) == 3)
        lp = ah.launch_profile_post({"root": proj, "profile": {"cmd": "npm start", "env": "A=1\nB = two\n# note\n"}})
        check("the page's NAME=value lines become the environment", lp["ok"] and arch_runner.launch_profile(proj)["env"] == {"A": "1", "B": "two"}, lp)
        check("a malformed environment line is refused", not ah.launch_profile_post({"root": proj, "profile": {"cmd": "x", "env": "no equals"}})["ok"])

        # ---- nothing shipped was written
        check("no shipped agent, workflow, flow or prompt file was written by any of this", _shipped_snapshot() == before)
    finally:
        core.set_home(old_home)
        for k, v in _OLD.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' CUSTOM CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' CUSTOM CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
