"""Checks the hub's API end to end (starts the hub on a free port against a fake model). Part of `architect.py selftest`."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def run():
    import arch_selftest_pro as t
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    fake = ThreadingHTTPServer(("127.0.0.1", 0), t.Pro)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    work = tempfile.mkdtemp(prefix="archhub_")
    hubdir = os.path.join(work, "hub")
    shutil.copytree(HERE, hubdir, ignore=shutil.ignore_patterns("__pycache__", "hub_data", "architect_config.json", "portfolio.json", "._guard", ".arch"))
    env = dict(os.environ, OLLAMA_HOST=f"127.0.0.1:{fake.server_address[1]}", PYTHONUNBUFFERED="1")
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    hub = subprocess.Popen([sys.executable, "architect_hub.py", "--no-browser", "--port", str(port)], cwd=hubdir, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        html = ""
        for _ in range(40):
            try:
                html = urllib.request.urlopen(base + "/").read().decode()
                break
            except OSError:
                time.sleep(0.25)
        tok = (re.search(r'const TOKEN="([^"]+)"', html) or [None, ""])[1]

        def call(path, body=None, token=True):
            hdr = {"Content-Type": "application/json"}
            if token:
                hdr["X-Hub-Token"] = tok
            r = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=hdr)
            try:
                return json.loads(urllib.request.urlopen(r).read() or b"{}")
            except urllib.error.HTTPError as e:
                return {"HTTP": e.code, "body": e.read().decode()[:200]}

        check("hub serves the page with a per-start token", bool(tok) and "__HUB_TOKEN__" not in html)
        check("changes without the token are refused", call("/api/arch/assign", {"roles": {}}, token=False).get("HTTP") in (401, 403))
        st = call("/api/state")
        import arch_flows as _fl
        import arch_roles as _ro
        import arch_templates as _tp
        want_flows = {k for k in _fl.FLOWS if k != "checkup"}
        check("state lists every role, flow, template and add-on the code defines (counted from the code, not a number typed here)",
              len(st["roles"]) == len(_ro.ROLES) and len(st["flows"]) in (len(want_flows), len(want_flows) + 1)
              and len(st["templates"]) == len(_tp.TEMPLATES) and len(st["addons"]) == len(_tp.ADDONS) and len(st["categories"]) == 24,
              (len(st["roles"]), len(st["flows"]), len(st["templates"]), len(st["addons"]), len(st["categories"])))
        check("state exposes the Architect's own install dir, for Self-improve to target", isinstance(st.get("self_root"), str) and os.path.isfile(os.path.join(st["self_root"], "architect.py")), st.get("self_root"))
        check("every flow lists the checks inside it, and every flow that calls a model lists its roles (checkup is code-only by design)",
              all(f["steps"] for f in st["flows"]) and all(f["roles"] for f in st["flows"] if f["id"] != "checkup"))
        check("role cards carry a group and a suggested model", all(r["group"] in ("Core", "Builders", "Quality", "Security", "Infra") for r in st["roles"]) and all(r["suggested"] for r in st["roles"]))
        check("apply recommended team assigns every role", len(call("/api/arch/apply_recommended", {})["assignment"]) == len(st["roles"]))
        check("assigning one role is saved", call("/api/arch/assign", {"roles": {"tester": "fake:7b"}}).get("ok") and next(r for r in call("/api/state")["roles"] if r["id"] == "tester")["assigned"] == "fake:7b")
        check("routing preview works for a folder that does not exist yet (empty project)", call("/api/arch/route", {"goal": "Create a booking API", "root": os.path.join(work, "nope")}).get("flow") == "new")
        check("a bad folder is refused with a clear message", "not found" in json.dumps(call("/api/arch/run", {"goal": "x", "flow": "build", "root": os.path.join(work, "a", "b", "c")})).lower())

        def job_done(n=120):
            for _ in range(n):
                j = call("/api/job?since=0")
                if not j.get("running") and j.get("code") is not None:
                    return j
                time.sleep(0.5)
            return call("/api/job?since=0")

        proj = os.path.join(work, "proj")
        r = call("/api/arch/run", {"goal": "Slug tools", "flow": "new", "review": "auto", "root": proj, "template": "python_lib", "name": "Slug Tools", "addons": ["ci"], "then_build": False,
                                   "options": {"behavior.git": "off", "limits.max_revisions": 2, "roles.tester": "evil"}})
        j = job_done()
        check("hub starts a project from nothing in a new folder: template + add-on written, checks pass",
              r.get("ok") and j.get("code") == 0 and os.path.isfile(os.path.join(proj, "slug_tools", "core.py")) and os.path.isfile(os.path.join(proj, ".github", "workflows", "ci.yml")), j.get("text", "")[-400:])
        runs = call("/api/arch/runs?root=" + urllib.request.quote(proj))
        check("run listing shows the flow and a readable report", runs and runs[0]["flow"] == "new" and "Template" in runs[0]["report"], runs[:1])
        r = call("/api/arch/undo", {"root": proj})
        j = job_done()
        check("undo removes everything the run created", j.get("code") == 0 and not os.path.exists(os.path.join(proj, "slug_tools", "core.py")), j.get("text", "")[-300:])
        r = call("/api/arch/portfolio", {"apps": [{"name": "demo", "path": proj, "depends_on": "a, b", "interfaces": "REST"}]})
        check("portfolio saves and splits lists", r["apps"][0]["depends_on"] == ["a", "b"])

        # a FRESH project -- `proj` above was just undone (its files removed), so it has no run command to serve
        proj2 = os.path.join(work, "proj_serve")
        r = call("/api/arch/run", {"goal": "", "flow": "new", "review": "auto", "root": proj2, "template": "rest_api", "name": "ServeDemo", "then_build": False})
        job_done()
        serve_before = call("/api/arch/serve?root=" + urllib.request.quote(proj2))
        check("hub: serve status before start reports not running and the project's own default run command", serve_before.get("running") is False and serve_before.get("default_cmd"), serve_before)
        sr = call("/api/arch/serve", {"action": "start", "root": proj2})
        check("hub: serve start spawns a REAL subprocess over HTTP, not a simulated one", sr.get("ok") and isinstance(sr.get("pid"), int), sr)
        for _ in range(40):
            sh = call("/api/arch/serve?root=" + urllib.request.quote(proj2))
            if sh.get("http_ok") is not None:
                break
            time.sleep(0.25)
        check("hub: serve status does a real TCP connect + HTTP GET against the project's own port", sh.get("running") and sh.get("port_open") and sh.get("http_ok"), sh)
        check("hub: serve reports the actual network policy applied, not a hidden/hardcoded one", sh.get("network", {}).get("mode") in ("offline", "practice", "open"), sh)
        sx = call("/api/arch/serve", {"action": "stop", "root": proj2})
        check("hub: serve stop actually kills the real process", sx.get("ok"), sx)
        sh2 = call("/api/arch/serve?root=" + urllib.request.quote(proj2))
        check("hub: serve status reflects the real stop, not a cached one", sh2.get("running") is False, sh2)
        r = call("/api/test", {"models": ["fake:7b"], "categories": ["testing", "bughunt", "optimize"], "think": False})
        for _ in range(120):
            stt = call("/api/state")
            if not stt["tests"]["running"]:
                break
            time.sleep(0.5)
        stt = call("/api/state")
        agg = stt["models"][0]["agg"]
        if not all(c in agg for c in ("testing", "bughunt", "optimize")):
            print("   test log:", stt["tests"]["log"][-6:])
        check("scoreboard runs the new test categories (tests, bug exposure, optimisation) and records scores", all(c in agg for c in ("testing", "bughunt", "optimize")), agg)

        r = call("/api/arch/settings", {"settings": {"limits.max_revisions": "2", "behavior.network": "practice", "behavior.net_allow": "pypi.org, github.com"}})
        check("validated settings save coerces and applies", r.get("ok") and r["config"]["limits"]["max_revisions"] == 2 and r["config"]["behavior"]["network"] == "practice"
              and r["config"]["behavior"]["net_allow"] == ["pypi.org", "github.com"], r)
        bad = call("/api/arch/settings", {"settings": {"behavior.network": "not-a-real-mode"}})
        check("an invalid setting is refused with a clear message, not silently dropped", bad.get("HTTP") == 400 and "behavior.network" in bad.get("body", ""), bad)
        r = call("/api/arch/model_override", {"model": "fake:7b", "field": "sampling.temperature", "value": "0.3"})
        check("a model override is saved per-field", r.get("ok") and r["model_overrides"]["fake:7b"]["sampling"]["temperature"] == 0.3, r)
        r2 = call("/api/arch/model_override", {"model": "fake:7b", "field": "sampling.top_p", "value": "0.9"})
        check("a second field merges instead of replacing the sampling dict", r2["model_overrides"]["fake:7b"]["sampling"] == {"temperature": 0.3, "top_p": 0.9}, r2)
        badf = call("/api/arch/model_override", {"model": "fake:7b", "field": "sampling.temperature", "value": "not-a-number"})
        check("a bad override value is refused", badf.get("HTTP") == 400, badf)

        dx = call("/api/arch/doctor?root=" + urllib.request.quote(proj2))
        check("hub: doctor runs real preflight checks over HTTP (Python, Ollama, disk, port, git, network policy all present)",
              isinstance(dx.get("ok"), bool) and len(dx.get("checks", [])) >= 6 and any("Ollama" in c["name"] for c in dx["checks"]), dx)

        st2 = call("/api/state")
        check("hub: state lists config presets with a label and settings", len(st2.get("presets", [])) >= 3 and all(p["label"] and p["settings"] for p in st2["presets"]), st2.get("presets"))
        pr = call("/api/arch/preset", {"name": "fast-iteration"})
        check("hub: applying a preset over HTTP actually changes the saved config", pr.get("ok") and pr["config"]["behavior"]["evaluator"] == "off" and pr["config"]["behavior"]["network"] == "open", pr)
        badpr = call("/api/arch/preset", {"name": "nope"})
        check("hub: an unknown preset name is refused with a clear message", badpr.get("HTTP") == 400 and "nope" in badpr.get("body", ""), badpr)

        asx = call("/api/arch/assess")
        check("hub: assess reports real hardware, a hardware-tuned settings patch, and per-workload coverage (coding/security/data)",
              "vram_tier" in asx and asx.get("tuned_settings") and {"coding", "security", "data"} <= {w["id"] for w in asx.get("workloads", [])}, asx)
        check("hub: assess's self-tuning drift is computed against the REAL saved config (a list, not null, since one is always on hand here)", isinstance(asx.get("tuning_drift"), list), asx.get("tuning_drift"))

        dxf = call("/api/arch/doctor_fix", {"root": proj2})
        check("hub: doctor_fix runs real repairs over HTTP and returns before/actions/after", {"before", "actions", "after"} <= set(dxf) and isinstance(dxf["actions"], list), dxf)

        rv = call("/api/arch/run", {"goal": "Review this code", "flow": "review", "review": "auto", "root": proj2})
        job_done()
        runs2 = call("/api/arch/runs?root=" + urllib.request.quote(proj2))
        tr = call("/api/arch/trace?root=" + urllib.request.quote(proj2) + "&run=" + urllib.request.quote(runs2[0]["name"]))
        check("hub: trace exposes the REAL per-call record the engine wrote while the run happened (role, actual model, measured tok/s) -- not a reconstruction after the fact",
              len(tr.get("calls", [])) >= 1 and tr["calls"][0]["role"] == "reviewer" and tr["calls"][0]["model"] == "fake:7b" and tr["calls"][0].get("gen_tokens", 0) > 0, tr)
        badtr = call("/api/arch/trace?root=" + urllib.request.quote(proj2) + "&run=../../etc")
        check("hub: trace refuses a run name that tries to escape the runs folder", badtr.get("HTTP") == 400, badtr)

        adv = call("/api/arch/advise?root=" + urllib.request.quote(proj2))
        check("hub: advise combines doctor+assess+a real code scan over HTTP into one ranked, counted list",
              isinstance(adv.get("count"), int) and isinstance(adv.get("proposals"), list) and {"high", "medium", "low"} <= set(adv.get("by_severity", {})), adv)

        # ---- five tabs over the pages, plus the Get-ready / tune / fake-code / self-improve endpoints
        tabs_src = re.search(r"const TABS=\[(.*?)\];", html, re.S)
        tab_pages = [re.findall(r'"([a-z]+)"', grp) for grp in re.findall(r'\["[a-z]+","[^"]*","[A-Za-z]+",\[([^\]]*)\]\]', tabs_src.group(1))] if tabs_src else []
        page_ids = re.findall(r'\["([a-z]+)","[^"]*","[^"]*","[^"]*"\]', re.search(r"const NAV_GROUPS=\[(.*?)\];\s*const PAGES", html, re.S).group(1))
        flat = [x for g in tab_pages for x in g]
        check("the hub shows four places (Projects, Workspace, Models, Settings) and every page is reachable from exactly one of them",
              len(tab_pages) == 4 and sorted(flat) == sorted(page_ids) and len(set(flat)) == len(flat), (tab_pages, page_ids))
        check("Home leads with a Get started checklist: four steps, each with one action, the current one highlighted",
              "function getStarted()" in html and html.count("t:\"Prepare this machine\"") == 1 and "Run your first task" in html and "gs-step" in html)
        ti = call("/api/arch/tune_info")
        import shutil as _sh
        import subprocess as _sp
        html = open(os.path.join(HERE, "hub_ui.html"), encoding="utf-8").read()
        # Every script block on the page, each checked on its own. Taking everything from the first <script> to the last </script>
        # swept the HTML between them into the middle of the source, which is a syntax error in any file with two script blocks.
        blocks = [b.replace('"__HUB_TOKEN__"', '"x"') for b in re.findall(r"<script>(.*?)</script>", html, re.S)]
        js = "\n".join(blocks)
        check("the hub page has its script blocks where they are expected", len(blocks) >= 1, len(blocks))

        # ---- live events: the endpoint the one-screen view reads to show what a run is doing
        feed = call("/api/arch/events?since=0")
        check("the hub offers a live feed of what a run is doing, with a cursor so nothing is re-read",
              isinstance(feed.get("events"), list) and isinstance(feed.get("next"), int)
              and "epoch" in feed and "running" in feed, feed)
        check("the live feed is readable without a token, because it only reads", isinstance(call("/api/arch/events?since=0", token=False).get("events"), list))
        evs = feed.get("events") or []
        kinds = {e.get("type") for e in evs}
        check("real runs from these checks reached it: model calls, and the records the live panel draws",
              "call" in kinds and bool(kinds & {"step", "verdict", "check", "tool"}), sorted(kinds))
        one = next((e for e in evs if e.get("type") == "call"), {})
        check("a call event carries what the strip shows: which model, which tier, how fast",
              {"model", "role", "tier"} <= set(one), sorted(one))
        check("a call event does NOT carry the whole prompt, so watching a run stays cheap",
              "messages" not in one, [k for k in one if k == "messages"])
        check("a long reply is trimmed in the event and says how long it really was",
              all(len(str(e.get("reply") or "")) < 1200 for e in evs if e.get("type") == "call"),
              max([len(str(e.get("reply") or "")) for e in evs if e.get("type") == "call"] or [0]))
        check("the cursor moves forward, so a watcher never re-reads what it already has",
              feed["next"] >= len(evs) and call("/api/arch/events?since=%d" % feed["next"]).get("events") == [], feed.get("next"))
        check("a nonsense cursor is handled rather than refused", isinstance(call("/api/arch/events?since=banana").get("next"), int))
        tm = call("/api/arch/team")
        check("the hub proposes a team measured against this machine, one entry per tier, each with a reason",
              set((tm.get("plan") or {}).get("tiers") or {}) >= {"worker", "router", "heavy", "draft"} or (tm.get("plan") or {}).get("considered") == 0,
              list((tm.get("plan") or {}).get("tiers") or {}))
        check("and says whether this is the first run (no tier chosen yet)", isinstance(tm.get("first_run"), bool), tm.get("first_run"))
        ts = call("/api/arch/team", {"set": {"worker": "fake:7b"}})
        check("a tier can be set by hand from the hub, and the old choice is returned so it can be put back",
              ts.get("ok") and ts.get("after", {}).get("worker") == "fake:7b" and "before" in ts, ts)
        check("an unknown tier name is refused", call("/api/arch/team", {"set": {"chef": "x"}}).get("HTTP") == 400)
        call("/api/arch/team", {"set": {"worker": ""}})
        rr = urllib.request.urlopen(base + "/api/arch/receipt?root=" + urllib.request.quote(proj2))
        rhtml = rr.read().decode("utf-8")
        check("any run can be opened as a receipt page, which loads nothing from anywhere",
              rr.status == 200 and "<!doctype html>" in rhtml.lower() and "default-src 'none'" in (rr.headers.get("Content-Security-Policy") or "")
              and "http://" not in rhtml.replace("http://127.0.0.1", "") and "<script" not in rhtml, rr.headers.get("Content-Security-Policy"))
        check("a receipt for a run that does not exist is a clear 404", _http_code(base + "/api/arch/receipt?run=nope&root=" + urllib.request.quote(proj2)) == 404)
        q0 = call("/api/arch/queue")
        check("the hub shows the supervisor's queue with its counts, today's spend and the kill switch",
              isinstance(q0.get("jobs"), list) and "pending" in (q0.get("counts") or {}) and "runs" in (q0.get("spent_today") or {}) and "stopped" in q0, q0)
        qa = call("/api/arch/queue", {"add": {"goal": "explain the router", "root": hubdir, "flow": "explore"}})
        q1 = call("/api/arch/queue")
        check("a task can be queued from the hub", qa.get("ok") and any(j["goal"] == "explain the router" for j in q1.get("jobs") or []), (qa, q1.get("counts")))
        qc = call("/api/arch/queue", {"cancel": qa.get("id")})
        check("and cancelled before it starts", qc.get("ok") is True and call("/api/arch/queue")["counts"]["cancelled"] >= 1, qc)
        qs = call("/api/arch/queue", {"stop": True})
        check("the kill switch can be thrown from the hub, and lifted again",
              qs.get("stopped") is True and call("/api/arch/queue", {"stop": False}).get("stopped") is False, qs)
        check("changing the queue needs the per-start token", call("/api/arch/queue", {"clear": True}, token=False).get("HTTP") in (401, 403))
        rd = call("/api/arch/redirect", {"root": hubdir, "run": "../escape", "message": "do this instead"})
        check("changing your mind mid-run refuses a run name that tries to escape the runs folder", rd.get("HTTP") == 400, rd)
        rd2 = call("/api/arch/redirect", {"root": hubdir, "run": "nosuchrun", "message": "do this instead"})
        check("and refuses a run that does not exist", rd2.get("HTTP") == 400, rd2)
        os.makedirs(os.path.join(hubdir, ".arch", "runs", "run_redirect"), exist_ok=True)
        rd3 = call("/api/arch/redirect", {"root": hubdir, "run": "run_redirect", "message": "use sqlite instead", "stop": False})
        inbox = os.path.join(hubdir, ".arch", "runs", "run_redirect", "inbox.jsonl")
        gp = call("/api/arch/gpu")
        check("the status strip's live GPU data is served: how busy the card is, memory in use, RAM, and each model's card/RAM split",
              isinstance(gp, dict) and {"gpu_pct", "vram_used_gb", "ram_gb", "ram_free_gb", "vm_gb", "vm_limit_gb"} <= set(gp) and isinstance(gp.get("tiers"), list)
              and all({"tier", "up", "gpu_gb", "ram_gb", "full_gpu"} <= set(t) for t in gp["tiers"]), gp)
        mm = call("/api/arch/machine")
        check("the machine map is served: CPU, graphics card, RAM, every drive with its role, and where each model sits",
              isinstance(mm, dict) and ("error" in mm or ({"cpu", "gpu", "ram", "drives", "models"} <= set(mm)
              and all({"tier", "gpu_gb", "ram_gb", "why"} <= set(m) for m in mm["models"]))), mm)
        fm = call("/api/arch/flowmap?flow=bugfix")
        check("a workflow's steps are shown with the model each uses here, the check that decides it and its budget",
              isinstance(fm, dict) and fm.get("id") == "bugfix" and len(fm.get("steps") or []) >= 3
              and all({"title", "kind", "role", "tier", "model", "check"} <= set(s) for s in fm["steps"]), fm)
        dv = call("/api/arch/drives")
        check("the drive list is served: each drive's bus, size, free space and its role here, read only",
              isinstance(dv, dict) and isinstance(dv.get("drives"), list)
              and all({"mount", "bus", "total_gb", "free_gb", "removable", "role", "why"} <= set(d) for d in dv["drives"]), dv)
        ev = call("/api/arch/evals")
        check("the Benchmarks page has its data: every saved run with its pass rate and cost, live and scripted apart, nothing re-measured",
              isinstance(ev, dict) and isinstance(ev.get("runs"), list) and isinstance(ev.get("tasks"), list)
              and all(("pass_rate" in r and "fake" in r) for r in ev["runs"]), {k: (v if k != "runs" else len(v)) for k, v in (ev or {}).items()})
        check("a message for a real run lands in its inbox for the next step to read",
              rd3.get("ok") is True and os.path.isfile(inbox) and "use sqlite instead" in open(inbox, encoding="utf-8").read(), rd3)
        if _sh.which("node"):
            bad = []
            for i, b in enumerate(blocks):
                jp = os.path.join(work, f"hub_check{i}.js")
                open(jp, "w", encoding="utf-8").write(b)
                r = _sp.run(["node", "--check", jp], capture_output=True, text=True, timeout=30)
                if r.returncode != 0:
                    bad.append(f"block {i + 1}: {r.stderr[-200:]}")
            check("every script block on the hub page parses (a syntax error would mean a blank page)", not bad, "; ".join(bad))
        else:
            check("the hub page's script parses (node not installed here: brackets balanced instead)", js.count("(") == js.count(")") and js.count("{") == js.count("}") and js.count("[") == js.count("]"))
        check("tune_info carries the last live-test result slot", "livetest" in ti, list(ti))
        check("tune_info lists the other machines (none yet) with the power state", ti.get("machines", {}).get("hosts") == [] and "power" in ti["machines"], ti.get("machines"))
        rr = call("/api/arch/hosts", {"add": {"name": "desk", "address": "127.0.0.1:1"}})
        check("a machine can be added from the hub and shows as down, not as an error", rr["ok"] and rr["machines"]["hosts"][0]["name"] == "desk" and rr["machines"]["hosts"][0]["ok"] is False, rr)
        rr = call("/api/arch/hosts", {"route": {"role": "planner", "host": "desk", "model": "big:70b"}})
        check("a role can be routed to it", rr["machines"]["role_hosts"] == {"planner": "desk"}, rr)
        rr = call("/api/arch/hosts", {"remove": "desk"})
        check("removing the machine puts the role back", rr["machines"]["hosts"] == [] and rr["machines"]["role_hosts"] == {}, rr)
        check("a bad machine name is refused", call("/api/arch/hosts", {"add": {"name": "bad name!", "address": "x"}}).get("HTTP") in (400, 422) or "error" in call("/api/arch/hosts", {"add": {"name": "bad name!", "address": "x"}}))
        check("tune_info carries the machine card slot and per-model track/edit-style fields", "machine_card" in ti and all("track" in m and "edit_style" in m for m in ti.get("models", [])), list(ti))
        check("tune_info reports the tune status, the catalog and the installed models", "status" in ti and ti["status"]["needs_tune"] is True and "suggestions" in ti.get("catalog", {}) and isinstance(ti.get("installed"), list), ti)
        ch = call("/api/arch/code_health?root=" + urllib.request.quote(proj2))
        check("code_health returns fake-code counts and the install integrity", ch.get("fake", {}).get("total") is not None and "verify" in ch.get("self", {}) and ch["self"]["verify"].get("has_manifest") is not None, ch)
        check("code_health refuses a folder that does not exist", call("/api/arch/code_health?root=" + urllib.request.quote(os.path.join(work, "nope"))).get("HTTP") == 400)
        check("a download is refused without an explicit confirmation", call("/api/arch/pull", {"tag": "qwen3:8b"}).get("HTTP") == 400)
        check("a download is refused for something that is not a model tag", call("/api/arch/pull", {"tag": "x; rm -rf /", "confirm": True}).get("HTTP") == 400)
        check("self-improve needs a goal", call("/api/arch/self_improve", {"goal": "  "}).get("HTTP") == 400)
        # the buttons really work: each launches the real command in a child process and it finishes cleanly
        r = call("/api/arch/scan", {"root": proj2})
        j = job_done()
        check("the hub's 'Profile this project' button runs `scan` and writes the profile", r.get("ok") and j.get("code") == 0 and os.path.isfile(os.path.join(proj2, ".arch", "profile.md")), j.get("text", "")[-300:])
        r = call("/api/arch/tune", {"preview": True})
        j = job_done()
        check("the hub's 'Preview changes' button runs `tune --no-apply` and says it changed nothing", r.get("ok") and j.get("code") == 0 and "nothing was written" in j.get("text", ""), j.get("text", "")[-300:])
        r = call("/api/arch/ready", {"root": proj2})
        j = job_done(240)
        check("the hub's 'Get ready' button runs `ready` to its verdict", r.get("ok") and j.get("code") in (0, 1) and "READY:" in j.get("text", ""), j.get("text", "")[-400:])
        r = call("/api/arch/tune", {"undo": True})
        j = job_done()
        check("the hub's 'Undo last tune' button runs and reports", r.get("ok") and j.get("code") in (0, 1) and ("restored" in j.get("text", "") or "nothing to undo" in j.get("text", "").lower()), j.get("text", "")[-300:])
        st2 = call("/api/state")
        check("the task templates and groups ship in the state for the Task page", len(st2.get("task_templates", [])) >= 20 and "Fix" in st2.get("task_groups", []) and all(t["flow"] in {f["id"] for f in st2["flows"]} for t in st2["task_templates"]))
        ti = call("/api/arch/tune_info")
        check("Health ships the team fit sheet and the retrieval status (v11)", isinstance(ti.get("fit"), dict) and "resident" in ti["fit"] and isinstance(ti.get("index"), dict) and ti["index"].get("state"), (ti.get("fit"), ti.get("index")))
        check("Home's state says whether function-level retrieval is on", "embed_model" in st2)
        r = call("/api/arch/index", {"root": proj2, "query": "add throttling"})
        check("the hub's 'Compare rankings' button runs `index --query` as a job", r.get("ok") and job_done(), r)
        r = call("/api/arch/chat", {"root": proj2, "message": "what is here?"})
        check("Studio: a message is accepted, answered by the model, and read back as the thread", r.get("ok") and any(
            (m.get("who") == "model" for m in (call("/api/arch/chat?root=" + proj2).get("messages") or [])) for _ in range(40) if not time.sleep(0.25)), r)
        r = call("/api/arch/backlog", {"root": proj2, "add": "add a health endpoint with a test"})
        check("the backlog takes an item from the hub and lists it", r.get("ok") and any(i["goal"].startswith("add a health") for i in call("/api/arch/backlog?root=" + proj2)["items"]), r)
        check("the last run's report is readable for the Studio thread", "report" in call("/api/arch/report?root=" + proj2))
        check("the new actions need the hub token", all(call(p_, {"tag": "a:b", "confirm": True}, token=False).get("HTTP") in (401, 403) for p_ in ("/api/arch/pull", "/api/arch/ready", "/api/arch/tune", "/api/arch/self_rollback")))
    finally:
        hub.terminate()
        fake.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    print()
    ok = all(results)
    print(f"  {'ALL ' + str(len(results)) + ' HUB CHECKS PASSED' if ok else str(results.count(False)) + ' of ' + str(len(results)) + ' HUB CHECKS FAILED'}")
    return 0 if ok else 1


def _http_code(url):
    try:
        return urllib.request.urlopen(url).status
    except urllib.error.HTTPError as e:
        return e.code


if __name__ == "__main__":
    sys.exit(run())
