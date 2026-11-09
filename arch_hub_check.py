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
import urllib.parse
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
        pr = call("/api/arch/practice", {"start": False})
        ev = json.loads(urllib.request.urlopen(base + "/api/arch/events?since=999999999").read().decode() or "{}")
        check("practice create-only ({start:false}) prepares the folder and starts NO run", pr.get("started") is False and os.path.isfile(os.path.join(pr.get("root") or "", "textkit.py")) and not ev.get("running"), (pr, ev.get("running")))
        check("Home 'Use the practice project' is create-only and focuses the goal; 'Try a practice task' is the separate run action",
              'api("/api/arch/practice",{start:false})' in html and "function tryPractice" in html and '"Try a practice task",tryPractice' in html
              and 'onclick:usePractice},icon("sprout"),"Use the practice project"' in html)
        check("Console panel and strip read the same EV.running (no 'Nothing running' while the strip says running)",
              "!EV.steps.length&&!EV.now&&!EV.verdict&&EV.running" in html and "EV.running!==pollConsole._run" in html)
        check("Home pulls the run's events itself (a run started with Do it fills 'Working on it' without opening the Console)",
              "async function homeEvents()" in html and 'homeEvents().then(simpleDraw,simpleDraw)' in html and 'toast("Started");homeEvents._idle=false;simpleDraw();' in html
              and "RUNBAR.goal||!homeEvents._idle" not in html)   # a label nothing clears kept Home polling forever (2026-11-08)
        check("first-visit tour: 3 stops, Skip sticks in memory and in store, no async path re-opens it, no empty grey box",
              'FV_STEPS=["This computer","Practice task","Result"]' in html and "FV.closed=true;fvSave({status:\"skipped\"})" in html
              and "if(FV.closed||s2.status!==\"open\"" in html and "Checking this computer" in html)
        check("keys: Home types into #spGoal when nothing is focused; g-chords need nothing focused for both keys",
              "nothingFocused()&&!e.ctrlKey" in html and "if(!nothingFocused()){G_AT=0;return;}" in html and "if(typing(e)){G_AT=0;return;}" in html)
        check("live chip says where the model is (RAM vs card), not just 'model loaded'", "model in RAM " in html and "model on card " in html and '" · model loaded"' not in html)
        check("Claude use is explicit from the real settings with a one-click setting link, on the strip and under Do it",
              "function cloudPlan()" in html and 'goSetting("online.cloud_tier")' in html and "ta,doIt),cloudLine()" in html and '"Claude: "+({escalation' not in html)
        check("changes without the token are refused", call("/api/arch/assign", {"roles": {}}, token=False).get("HTTP") in (401, 403))
        st = call("/api/state")
        # 2026-11-08 design Phase 3: the page reads /api/state in two halves; the catalog carries an ETag and answers a repeat with 304
        live, cat = call("/api/state?part=live"), call("/api/state?part=catalog")
        r1 = urllib.request.urlopen(base + "/api/state?part=catalog")
        tag = r1.headers.get("ETag") or ""
        try:
            code = urllib.request.urlopen(urllib.request.Request(base + "/api/state?part=catalog", headers={"If-None-Match": tag})).status
        except urllib.error.HTTPError as e:
            code = e.code
        check("the live and catalog halves of /api/state together hold exactly what the whole does, and a repeated catalog read is 304",
              set(live) | set(cat) == set(st) and "settings_schema" not in live and "models" not in cat and bool(tag) and code == 304,
              (sorted(set(st) ^ (set(live) | set(cat)))[:5], tag, code))
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
        import arch_uireg
        reg = arch_uireg.load(os.path.join(HERE, "hub_ui.html"))
        tab_pages = [p["pages"] for p in reg["places"]]
        groups = [s["section"] for s in reg["sidebar"]]
        page_ids = list(reg["page_ids"])
        flat = [x for g in tab_pages for x in g]
        check("the sidebar groups places under Work, Models and System and every page is reachable from exactly one of them",
              groups == ["Work", "Models", "System"] and len(tab_pages) >= 8 and sorted(flat) == sorted(page_ids) and len(set(flat)) == len(flat), (groups, tab_pages, page_ids))
        check("the Crew builder and By job pages are reachable from the sidebar", {"crewbuild", "byjob"} <= set(flat) and "function crewBuilderPage()" in html and "function byJobPage()" in html, flat)
        # ---- Crew builder + By-job endpoints: arch_squad / arch_select may not exist yet, so "available: false" is a pass too
        sa, sw, sy = call("/api/arch/squad/agents"), call("/api/arch/squad/workflows"), call("/api/arch/select/why")
        check("squad agents/workflows and select why answer with available + a list, never a crash",
              all(isinstance(d.get("available"), bool) for d in (sa, sw, sy)) and isinstance(sa.get("agents"), list)
              and isinstance(sw.get("workflows"), list) and isinstance(sy.get("jobs"), list), (sa, sw, sy))
        if sa.get("available"):
            import arch_squad as _sq
            check("the squad agents endpoint lists every agent of arch_squad (a dict of definitions, read by .values())", len(sa.get("agents") or []) == len(_sq.list_agents()) >= 18, len(sa.get("agents") or []))
            check("squad workflows carry phases with a kind", all(all(isinstance(p, dict) and p.get("kind") for p in w["phases"]) for w in sw.get("workflows") or []), sw)
        check("no sidebar place has more than 5 sub-tabs (Models included), and Agent teams has at most 5",
              all(len(g) <= 5 for g in tab_pages) and any(set(g) >= {"agents", "fleet"} and len(g) <= 5 for g in tab_pages), tab_pages)
        check("the Plugins, Telemetry, Squad runs and one Health page are in the sidebar; old checkup ids land on Health",
              {"plugins", "telemetry", "squadruns", "doctor"} <= set(flat) and "advisor" not in flat
              and 'advisor:"doctor"' in html and 'health:"doctor"' in html and "function healthPage()" in html and '"Run checkup"' in html, flat)
        check("Settings has a 'Show advanced pages' toggle, off by default, and the palette traps focus and returns it",
              "Show advanced pages" in html and 'store.get("showAdvanced","0")' in html and 'e.key==="Tab"' in html and "openPalette.back" in html)
        check("no hard-coded px font sizes are left on the page (they are rem, so the browser's text size applies)",
              not re.search(r"font-size:\d+(\.\d+)?px", html), re.findall(r"font-size:\d+(?:\.\d+)?px", html)[:5])
        px_short = re.findall(r"font:[^;}\"]*\d+(?:\.\d+)?px[^;}\"]*", html)
        check("no px size hides inside a font: shorthand either (51 did; the font-size check could not see them)", not px_short, px_short[:5])
        pl = call("/api/arch/plugins")
        check("plugins answers available + a list, never a crash", isinstance(pl.get("available"), bool) and isinstance(pl.get("plugins"), list), pl)
        check("trusting a plugin without confirm, a bad name, and installing from a missing folder are refused",
              call("/api/arch/plugins/trust", {"name": "x"}).get("HTTP") == 400 and call("/api/arch/plugins/enable", {"name": "../x"}).get("HTTP") == 400
              and call("/api/arch/plugins/install", {"path": os.path.join(work, "nope")}).get("HTTP") == 400
              and call("/api/arch/plugins/enable", {"name": "x"}, token=False).get("HTTP") in (401, 403))
        # ---- plugin commands in the slash palette and on the Plugins page
        pdir = os.path.join(work, "plug_cmd")
        os.makedirs(os.path.join(pdir, "commands"), exist_ok=True)
        with open(os.path.join(pdir, "plugin.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "cmdcheck", "version": "1.0.0", "description": "One command for the hub check."}, f)
        with open(os.path.join(pdir, "commands", "hello.md"), "w", encoding="utf-8") as f:
            f.write("---" + chr(10) + "description: Say hello to a file" + chr(10) + "args: a file path" + chr(10) + "---" + chr(10) + "Explain {args} in two sentences." + chr(10))
        inst = call("/api/arch/plugins/install", {"path": pdir})
        cm0 = call("/api/arch/commands")
        check("a disabled plugin's commands are NOT in the slash list", inst.get("HTTP") is None and not any(c.get("name") == "hello" for c in cm0.get("commands", [])), (inst, cm0))
        pl2 = call("/api/arch/plugins")
        mine = next((x for x in pl2.get("plugins", []) if x["name"] == "cmdcheck"), {})
        check("the Plugins list carries each plugin's commands (name, description, args) even while disabled",
              any(c["name"] == "hello" and "hello" in c["description"].lower() and c["args_hint"] == "a file path" and c["needs_args"] for c in mine.get("command_info", [])), mine)
        call("/api/arch/plugins/enable", {"name": "cmdcheck"})
        cm1 = call("/api/arch/commands")
        got = next((c for c in cm1.get("commands", []) if c.get("plugin") == "cmdcheck"), {})
        check("an enabled plugin's command is listed with name, description, arguments and its plugin",
              got.get("name") == "hello" and got.get("args_hint") == "a file path" and got.get("needs_args") is True and "hello" in got.get("description", "").lower(), cm1)
        check("shipped commands are still listed beside it", any(c.get("name") == "review" for c in cm1.get("commands", [])) or len(cm1.get("commands", [])) > 1, len(cm1.get("commands", [])))
        check("the slash palette shows the plugin colour, arguments and plugin name; the Plugins page lists commands per plugin",
              "plHue(c.plugin)" in html and "c.args_hint" in html and "p.command_info" in html and "function plHue" in html)
        call("/api/arch/plugins/remove", {"name": "cmdcheck"})
        # ---- presets: what changes before applying, apply, undo
        rv = call("/api/arch/rpresets")
        ids = [p["id"] for p in rv.get("presets", [])]
        check("presets answer Fast, Balanced, Quality and Battery, each with its list of changes",
              rv.get("available") and {"fast", "balanced", "quality", "battery"} <= set(ids) and all(isinstance(p["changes"], list) for p in rv["presets"]), rv)
        fast0 = next((p for p in rv.get("presets", []) if p["id"] == "fast"), {"changes": []})
        ap = call("/api/arch/rpreset", {"name": "fast"})
        rv2 = call("/api/arch/rpresets")
        fast1 = next((p for p in rv2.get("presets", []) if p["id"] == "fast"), {})
        check("applying a preset puts it in force, leaves nothing to change, and offers Undo",
              ap.get("ok") and fast0["changes"] and fast1.get("active") is True and "fast" in (rv2.get("undo") or "").lower(), (ap, rv2.get("undo")))
        un = call("/api/arch/rpreset", {"undo": True})
        rv3 = call("/api/arch/rpresets")
        check("Undo puts the previous settings back", un.get("ok") and next((p for p in rv3["presets"] if p["id"] == "fast"), {}).get("active") is False, un)
        check("an unknown preset or a call without the token is refused",
              call("/api/arch/rpreset", {"name": "nope"}).get("HTTP") == 400 and call("/api/arch/rpreset", {"name": "fast"}, token=False).get("HTTP") in (401, 403))
        check("Presets are in the top bar and in Settings, with the changes shown first and an Undo",
              'id="presetBtn"' in html and "function rpresetCard" in html and "out.push(rpresetCard())" in html and "Applying " in html and '"Undo: "' in html)
        # ---- ETA, effective settings, data night
        et = call("/api/arch/eta?preview=1&until=06:30&max=20")
        check("the ETA endpoint answers the queue, the running job and a data-night preview with a line",
              isinstance(et.get("queue"), dict) and "job" in et and "left" in et.get("preview", {}).get("line", "") + " left" and et["preview"].get("line"), et)
        check("a bad end time in the preview is refused", call("/api/arch/eta?preview=1&until=99:99").get("HTTP") == 400)
        check("the ETA shows in the run bar, the Console strip and Runs > Queue",
              "rb-eta" in html and 'id:"cEta"' in html and 'id:"qEta"' in html)
        ef = call("/api/arch/effective?role=planner")
        check("effective settings answer per role with value and layer for each key; a bad role is refused",
              ef.get("role") == "planner" and ef.get("keys") and all({"value", "layer"} <= set(v) for v in ef["keys"].values()) and len(ef.get("roles", [])) > 5
              and call("/api/arch/effective?role=zzz").get("HTTP") == 400, ef)
        check("Settings has the Effective settings page with a role picker", "function effectivePage" in html and 'id:"effRole"' in html)
        dn = call("/api/arch/datanight", {"until": "6pm"})
        check("a data night with a bad end time or model name is refused before anything starts",
              dn.get("HTTP") == 400 and call("/api/arch/datanight", {"worker": "bad name;rm"}).get("HTTP") == 400)
        check("the data night card (worker, end time, ETA preview, local-only note) is in Runs > Queue",
              "...dataNightCard()];" in html and "Claude is never used" in html and "dnPreview" in html and 'id:"dnWorker"' in html)
        # ---- 2026-11-05 evening: registry endpoint, lessons, overnight card, one-click data night, train the worker, Settings order
        rg = call("/api/arch/ui/registry")
        check("GET /api/arch/ui/registry serves the places, pages (id, icon, label, description), the old-id map and the page functions",
              len(rg.get("places", [])) >= 8 and len(rg.get("pages", [])) >= 30 and all({"id", "icon", "label", "description"} <= set(p) for p in rg["pages"])
              and rg.get("legacy", {}).get("simple") == "overview" and "lessons" in rg.get("page_ids", []) and "lessonsPage" in rg.get("page_functions", [])
              and all(f"{p['id']}" for p in rg["places"]), list(rg)[:8])
        ls = call("/api/arch/lessons")
        check("GET /api/arch/lessons returns every lesson with its enforced-by and checked-by targets marked present or missing",
              ls.get("available") and len(ls.get("lessons", [])) >= 5 and all({"enforced_by", "checked_by", "trains_via", "samples", "evidence"} <= set(x) for x in ls["lessons"])
              and all(isinstance(t.get("ok"), bool) for x in ls["lessons"] for t in x["enforced_by"]), ls.get("why"))
        on = call("/api/arch/overnight")
        check("GET /api/arch/overnight answers with ran, samples, basis and the running state (an empty state on a fresh data folder)",
              isinstance(on.get("ran"), bool) and {"by_lesson", "total"} <= set(on.get("samples", {})) and isinstance(on.get("running"), dict), on)
        dv = call("/api/arch/datanight")
        check("GET /api/arch/datanight gives the running state and the plain defaults (end time 07:00, curriculum on)",
              dv.get("state", {}).get("running") is False and dv.get("defaults", {}).get("until") == "07:00" and dv["defaults"].get("curriculum") is True, dv)
        stp = call("/api/arch/datanight/stop", {})
        # the HUB's data folder (its env was fixed at Popen); os.environ may have gained a throwaway dir since (arch_core import below)
        stop_file = os.path.join(env.get("ARCHITECT_DATA_DIR") or os.path.join(hubdir, "hub_data"), "STOP")
        check("Stop safely writes the STOP file (and a call without the token is refused)",
              stp.get("ok") and os.path.exists(stop_file) and call("/api/arch/datanight/stop", {}, token=False).get("HTTP") in (401, 403))
        try:
            os.remove(stop_file)
        except OSError:
            pass
        tw = call("/api/arch/trainworker")
        check("Train the worker is disabled with a reason while there are too few samples, and a start is then refused",
              tw.get("samples", -1) >= 0 and (tw.get("can_dry") or tw.get("why_dry")) and (tw.get("can_real") or tw.get("why_real"))
              and (tw.get("can_dry") or call("/api/arch/trainworker", {}).get("HTTP") == 400), tw)
        check("the Data night card has one big Start data night, a Stop safely, End time default 07:00, Teacher and Curriculum switches, and live announcements",
              all(s in html for s in ('"Start data night"', '"Stop safely"', 'value:"07:00"', 'id:"dnTeacher"', 'id:"dnCur"', 'id:"dnLive"', '"Train the worker"', 'id:"twWhy"')))
        check("Home has the Last night report card with an empty state, Wilson ranges and the fastest worker",
              all(s in html for s in ('id:"homeNight"', "async function loadNight", "95% range", "Fastest worker measured", "Set up a data night")))
        check("Settings: Presets first, no second Quick presets card, everything else under a closed Advanced section",
              html.index("out.push(rpresetCard())") < html.index('id:"setAdv"') and "Quick presets" not in html and "function presetsCard" not in html
              and 'open:store.get("setAdvOpen","0")==="1"' in html)
        check("the Lessons page is in Help and is a labelled table with present / missing marks in words",
              "function lessonsPage" in html and '"/api/arch/lessons"' in html and '" (present)"' in html and "Samples so far" in html)
        check("Effective settings names each layer in plain words",
              "const EFF_WORDS=" in html and "Your own role setting" in html and "The job's built-in profile" in html)
        check("a recipe's Use this starts the run with the recipe id and Review opens by itself when it finishes",
              "recipe:r.id" in html and "function recipeReviewTick" in html and 'go("review")' in html)
        tsum, tev = call("/api/arch/telemetry/summary?window=600"), call("/api/arch/telemetry/events?kinds=llm.call,BAD%20KIND")
        check("telemetry summary and events answer available + their shape, never a crash",
              isinstance(tsum.get("available"), bool) and tsum.get("window") == 600 and isinstance(tev.get("available"), bool) and isinstance(tev.get("events"), list), (tsum, tev))
        if tsum.get("available"):
            check("the telemetry summary carries the card fields", {"calls", "tokens_in", "tokens_out", "errors"} <= set(tsum), tsum)
        ttl = call("/api/arch/telemetry/timeline?run=..%2Fx")
        check("a timeline for an unsafe run id is refused as not available, not a crash", ttl.get("available") is False and ttl.get("spans") == [], ttl)
        srn = call("/api/arch/squad/runs")
        check("squad runs answer available + a list; resuming an unknown or unsafe run is refused",
              isinstance(srn.get("available"), bool) and isinstance(srn.get("runs"), list)
              and call("/api/arch/squad/resume", {"run_id": "../x"}).get("HTTP") == 400
              and call("/api/arch/squad/resume", {"run_id": "no_such_run_1"}).get("HTTP") == 400, srn)
        tc = call("/api/arch/templates/catalog")
        check("the template catalog answers available + a list, never a crash", isinstance(tc.get("available"), bool) and isinstance(tc.get("templates"), list), tc)
        check("each catalog template carries the card fields", all({"id", "name", "category", "difficulty", "colour", "thumb"} <= set(t) for t in tc.get("templates") or []), tc)
        check("a template thumbnail with an unsafe id is refused", call("/templates/..%2Fhub_ui.svg").get("HTTP") in (400, 404))
        check("Home leads with one headline, a Do it button, the practice project and no duplicate quick launch",
              "What should your computer work on?" in html and '"Do it"' in html and "Use the practice project" in html and '"Quick launch"' not in html)
        check("Home: nothing above #spTitle (no subtabs, no page tip, no page title on Home)",
              "body.sheet-wrap #subtabs,body.sheet-wrap #pageHint{display:none}" in html and ".sheet-wrap .top{display:none}" in html
              and 'h("div",{class:"sp-head"},h("h2",{class:"sp-title",id:"spTitle"}' in html and '"aria-label":"About this page"' in html)
        check("Home: Do it is disabled while the goal box is empty, with a tooltip",
              'b.disabled=!g;b.title=g?"Start (Ctrl+Enter)":"Type what you want done"' in html and 'id:"spGo"' in html)
        check("Home: the project combobox lists recentRoots (last 5) + practice + paste",
              'store.get("recentRoots"' in html and '.slice(0,5)' in html and 'role:"combobox"' in html and '"Paste a path…"' in html and 'role:"listbox"' in html)
        check("top bar live chip is capped at 360px with ellipsis and never wraps",
              "#liveChip.livechip{max-width:360px;" in html and "text-overflow:ellipsis" in html and "white-space:nowrap" in html)
        check("Esc stops a run only when pressed twice, and bare digits no longer navigate", "Press Esc again within 2 s" in html and "n<=TABS.length" not in html)
        check("the V2 category colour tokens exist in light and dark, and the reduced-motion rule is one block",
              html.count("--c-agent-bg:") >= 3 and html.count("prefers-reduced-motion:reduce") == 1, html.count("prefers-reduced-motion:reduce"))
        check("each agent card carries model, effort, tools and budget", all({"name", "model", "effort", "tools", "budget"} <= set(a) for a in sa.get("agents") or []), sa)
        check("each job row carries the measured columns", all({"job", "current", "best", "C", "pass_rate", "ci", "tok_s", "fits", "reason", "suggestion"} <= set(j) for j in sy.get("jobs") or []), sy)
        check("saving an agent needs the token", call("/api/arch/squad/agent", {"name": "x", "spec": {}}, token=False).get("HTTP") in (401, 403))
        check("an agent with a bad name is refused", call("/api/arch/squad/agent", {"name": "../evil", "spec": {}}).get("HTTP") == 400)
        check("an agent spec that is not an object is refused", call("/api/arch/squad/agent", {"name": "builder", "spec": "x"}).get("HTTP") == 400)
        check("a crew run needs a goal and a real workflow", call("/api/arch/squad/run", {"workflow": "nope_wf", "goal": ""}).get("HTTP") == 400
              and call("/api/arch/squad/run", {"workflow": "../x", "goal": "g"}).get("HTTP") == 400
              and call("/api/arch/squad/run", {"workflow": "nope_wf_zz", "goal": "g"}).get("HTTP") == 400)
        check("a crew run refuses owned paths that climb out and bad slot counts",
              call("/api/arch/squad/run", {"workflow": (sw.get("workflows") or [{"name": "x"}])[0]["name"], "goal": "g", "owns": ["../../etc"]}).get("HTTP") == 400
              and call("/api/arch/squad/run", {"workflow": (sw.get("workflows") or [{"name": "x"}])[0]["name"], "goal": "g", "slots": 99}).get("HTTP") == 400)
        check("a crew run needs the token", call("/api/arch/squad/run", {"workflow": "x", "goal": "g"}, token=False).get("HTTP") in (401, 403))
        # ---- Review / plan / session / recipes (read from a synthetic run; a missing record leaves its part empty)
        rc_ = call("/api/arch/recipes")
        check("recipes answer available + a list, and fill needs the token and a real recipe",
              isinstance(rc_.get("available"), bool) and isinstance(rc_.get("recipes"), list)
              and call("/api/arch/recipes/fill", {"recipe": "x"}, token=False).get("HTTP") in (401, 403)
              and call("/api/arch/recipes/fill", {"recipe": "../x"}).get("HTTP") == 400, rc_)
        if rc_.get("available") and rc_.get("recipes"):
            fl_ = call("/api/arch/recipes/fill", {"recipe": rc_["recipes"][0]["id"], "root": hubdir, "params": {}})
            check("a recipe fill returns a goal, a flow and what the project lacks", bool(fl_.get("goal")) and bool(fl_.get("flow")) and isinstance(fl_.get("missing_text"), list), fl_)
        rvroot = os.path.join(work, "rvproj")
        rd = os.path.join(rvroot, ".arch", "runs", "r1")
        os.makedirs(rd)
        with open(os.path.join(rd, "trace.jsonl"), "w", encoding="utf-8") as f_:
            for rec in ({"type": "diff", "file": "a.py", "added": 2, "removed": 1, "hunks": [], "diff": "@@ -1 +1,2 @@\n-x\n+y\n+z\n"},
                        {"type": "call", "id": 1, "num_ctx": 8192, "prompt_tokens": 1000, "gen_tokens": 100, "secs": 3.0, "model": "m",
                         "messages": [{"role": "system", "content": "s" * 400}, {"role": "user", "content": "u" * 800}]},
                        {"type": "check", "gate": "pytest", "ok": True}, {"type": "check", "gate": "tests B", "ok": False, "text": "boom"},
                        {"type": "verdict", "verdict": "fail", "why": "tests"}, {"type": "diff", "garbage": 1}, {"type": "todo", "items": [{"id": "1", "title": "do it", "status": "pending"}]}):
                f_.write(json.dumps(rec) + "\n")
            f_.write("not json\n")
        with open(os.path.join(rd, "run.json"), "w", encoding="utf-8") as f_:
            json.dump({"goal": "fix it", "flow": "bugfix", "status": "done"}, f_)
        rv = call("/api/arch/review?root=" + urllib.parse.quote(rvroot) + "&run=r1")
        check("review lists files with diffs, tests with pass/fail, the verdict and a local cost",
              [x["file"] for x in rv.get("files", [])] == ["a.py"] and rv["files"][0]["added"] == 2 and [t["ok"] for t in rv.get("tests", [])] == [True, False]
              and (rv.get("verdict") or {}).get("verdict") == "fail" and rv["cost"]["local"] is True and rv["cost"]["calls"] == 1 and len(rv.get("plan", [])) == 1, rv)
        check("review refuses a run id that climbs out", call("/api/arch/review?root=" + urllib.parse.quote(rvroot) + "&run=..%2Fx").get("HTTP") == 400)
        pv = call("/api/arch/plan?root=" + urllib.parse.quote(rvroot) + "&run=r1")
        check("the plan is read, and says plainly when editing is not available", len(pv.get("items", [])) == 1 and (pv.get("editable") or "not available" in (pv.get("why") or "")), pv)
        # ---- live-critique fixes: one verdict mapping, a clean plan, a stale-server guard, the page wiring
        for rid_, recs_, st_ in (("r2", [{"type": "verdict", "verdict": "pass", "why": ""}], {"status": "done", "goal": "g2"}),
                                 ("r3", [{"type": "verdict", "verdict": "not_sure", "why": "no tests ran"}], {"status": "done", "goal": "g3"}),
                                 ("r4", [{"type": "call", "id": 1}], {"status": "stopped", "goal": "g4"}),
                                 ("r5", [{"type": "call", "id": 1}], {"status": "running", "goal": "g5"}),
                                 ("r6", [{"type": "todo", "items": [str({"id": "plan", "title": "Write the plan", "role": "planner", "kind": "model"}),
                                                                     {"id": "b", "title": "Build", "role": "coder"}, "plain text step"]},
                                         {"type": "verdict", "verdict": "fail", "why": "x"}], {"status": "done", "goal": "g6"})):
            d_ = os.path.join(rvroot, ".arch", "runs", rid_)
            os.makedirs(d_)
            with open(os.path.join(d_, "trace.jsonl"), "w", encoding="utf-8") as f_:
                f_.write("".join(json.dumps(r_) + "\n" for r_ in recs_))
            with open(os.path.join(d_, "run.json"), "w", encoding="utf-8") as f_:
                json.dump(st_, f_)
            with open(os.path.join(d_, "report.md"), "w", encoding="utf-8") as f_:
                f_.write("- Flow: `bugfix`\n- Goal: " + st_["goal"] + "\n" + ("run is complete and passed checks\n" if rid_ == "r3" else "NOT done\n"))
        lst_ = {x["name"]: x for x in call("/api/arch/runs?root=" + urllib.parse.quote(rvroot))}
        rvs_ = {k: (call("/api/arch/review?root=" + urllib.parse.quote(rvroot) + "&run=" + k).get("verdict") or {}).get("verdict", "") for k in lst_}
        check("Recent runs and Review give every run the same verdict (pass / fail / could not check / stopped / none, from the verdict record)",
              {k: v["verdict"] for k, v in lst_.items()} == {"r2": "pass", "r3": "not_sure", "r4": "stopped", "r5": "", "r6": "fail"}
              and all(lst_[k]["verdict"] == rvs_[k] for k in lst_) and lst_["r3"]["verdict_word"] == "Could not check"
              and lst_["r5"]["verdict_word"] == "No result yet" and all(isinstance(v.get("ts"), (int, float)) for v in lst_.values()), (lst_, rvs_))
        pl_ = call("/api/arch/review?root=" + urllib.parse.quote(rvroot) + "&run=r6").get("plan")
        check("plan items are clean rows (title, role, kind), also when they arrive as the text of a dict",
              pl_ and [p_["title"] for p_ in pl_] == ["Write the plan", "Build", "plain text step"] and pl_[0]["role"] == "planner" and pl_[0]["kind"] == "model"
              and not any("{" in p_["title"] for p_ in pl_), pl_)
        rv1_ = call("/api/arch/review?root=" + urllib.parse.quote(rvroot) + "&run=r1")
        check("review names the first failing check for the outcome line", "tests B" in (rv1_.get("first_failure") or "") and "boom" in rv1_["first_failure"], rv1_.get("first_failure"))
        check("review with no run named opens the newest run of the project", bool(call("/api/arch/review?root=" + urllib.parse.quote(rvroot)).get("run")))
        vs_ = call("/api/arch/version")
        check("the version endpoint says the code is current at start", vs_.get("stale") is False and vs_.get("fingerprint_at_start") == vs_.get("fingerprint_now") and len(vs_.get("fingerprint_at_start", "")) >= 8, vs_)
        check("restarting the hub needs the token", call("/api/arch/restart", {}, token=False).get("HTTP") in (401, 403))
        import http.client as _hc
        def _raw(method, path, host=None, headers=None):
            c_ = _hc.HTTPConnection("127.0.0.1", port, timeout=10)
            c_.putrequest(method, path, skip_host=True)
            c_.putheader("Host", host or f"127.0.0.1:{port}")
            for k_, v_ in (headers or {}).items():
                c_.putheader(k_, v_)
            c_.endheaders()
            r_ = c_.getresponse(); code_ = r_.status; r_.read(); c_.close()
            return code_
        check("a GET API route answers only to a loopback Host (DNS rebinding)",
              _raw("GET", "/api/arch/chat", host="evil.example") == 403 and _raw("GET", "/api/arch/events?since=0", host="evil.example:80") == 403
              and _raw("GET", "/api/arch/events?since=0") == 200)
        check("a negative Content-Length is refused", _raw("POST", "/api/arch/undo", headers={"Content-Length": "-1", "X-Hub-Token": tok, "Content-Type": "application/json"}) == 413)
        _hud = urllib.request.urlopen(base + "/hud").read().decode("utf-8", "replace")
        check("the Harness HUD is served at /hud, wires itself to the hub's event stream and has a page in the Help group",
              "ArchitectHUD" in _hud and "/api/arch/events/stream" in _hud and "translate(" in _hud
              and any(p.get("id") == "hud" for p in call("/api/arch/ui/registry").get("pages", [])), len(_hud))
        # ---- your versions (arch_custom) over HTTP: read, refuse without the token, refuse a bad name, save, list, reset
        _cu = call("/api/arch/custom?kind=agents&name=explainer")
        check("an agent can be read for editing, marked built-in, with its versions", _cu.get("source") == "built-in" and "name: explainer" in (_cu.get("text") or ""), _cu)
        check("saving your version needs the hub token", call("/api/arch/custom", {"action": "save", "kind": "agents", "name": "explainer", "text": "x"}, token=False).get("HTTP") == 403)
        check("a name that would leave its folder is refused", call("/api/arch/custom?kind=prompts&name=../../x").get("HTTP") == 400
              and call("/api/arch/custom", {"action": "save", "kind": "flows", "name": "../x", "text": "id: x"}).get("HTTP") == 400)
        _sv = call("/api/arch/custom", {"action": "save", "kind": "agents", "name": "explainer", "text": (_cu.get("text") or "").replace("effort: low", "effort: high")})
        _ag = {a["name"]: a for a in call("/api/arch/squad/agents").get("agents", [])}
        check("a saved agent is the one the crew list shows, as your version", _sv.get("ok") and _ag.get("explainer", {}).get("effort") == "high"
              and _ag["explainer"].get("scope") == "custom", (_sv, _ag.get("explainer", {}).get("effort")))
        check("reset to built-in brings the shipped agent back", call("/api/arch/custom", {"action": "reset", "kind": "agents", "name": "explainer"}).get("ok")
              and {a["name"]: a for a in call("/api/arch/squad/agents").get("agents", [])}["explainer"].get("effort") == "low")
        check("the page has the editors: agent instructions, text and versions, crew workflow edit, flow edit, step instructions, improve, launch profile",
              all(x in html for x in ("function cuEdit(", "Instructions (what this agent is and how it works)", '"Text & versions"', '"Edit this workflow"',
                                       '"Edit these instructions"', "function itPanel(", "function lnProfileCard(", '"New agent"', '"New workflow"')))
        import arch_core as _core0
        import model_hub as _mh0
        _iso = tempfile.mkdtemp()                       # these two saves write a config: never the person's real one
        _old_cf, _old_ccf, _old_dd = _mh0.CONFIG_FILE, _core0.CONFIG_FILE, os.environ.get("ARCHITECT_DATA_DIR")
        _mh0.CONFIG_FILE = _core0.CONFIG_FILE = os.path.join(_iso, "architect_config.json")
        os.environ["ARCHITECT_DATA_DIR"] = _iso
        try:
            open(_mh0.CONFIG_FILE, "w").write("{}")
            _bad = call("/api/config", {"config": {"behavior": {"network": "wide-open-please"}}})
            check("Save-this-JSON refuses a value the settings schema does not allow, and changes nothing",
                  _bad.get("HTTP") == 400 and "behavior.network" in (_bad.get("body") or "") and open(_mh0.CONFIG_FILE).read() == "{}", _bad)
            _ok = call("/api/config", {"config": {"behavior": {"network": "offline"}, "tiers": {"keep": "me"}}})
            check("a valid JSON save goes through (unknown keys such as tiers pass), and keeps the previous file as a backup",
                  _ok.get("ok") is True and (_ok.get("config") or {}).get("tiers") == {"keep": "me"} and os.path.isfile(_ok.get("backup") or ""), _ok)
            if _ok.get("backup"):
                try:
                    os.remove(_ok["backup"])
                except OSError:
                    pass
        finally:
            _mh0.CONFIG_FILE, _core0.CONFIG_FILE = _old_cf, _old_ccf
            if _old_dd is None:
                os.environ.pop("ARCHITECT_DATA_DIR", None)
            else:
                os.environ["ARCHITECT_DATA_DIR"] = _old_dd
            shutil.rmtree(_iso, ignore_errors=True)
        _j = _mh0.Job()
        _j.events, _j.ev_dropped, _j.epoch = [{"t": "a"}, {"t": "b"}, {"t": "c"}], 0, 5
        check("an event cursor from an earlier run (older epoch) is read from 0, so the new run's first events are not dropped",
              len(_j.event_feed(3, 4)["events"]) == 3 and len(_j.event_feed(3, 5)["events"]) == 0 and len(_j.event_feed(3)["events"]) == 0)
        import architect_hub as _ah0
        _bad_ids = 0
        for _rid in ("..", "."):
            try:
                _ah0._run_dir_of(os.getcwd(), _rid)
            except ValueError:
                _bad_ids += 1
        check("a run id of '.' or '..' is not a run id (it would resolve to .arch itself)", _bad_ids == 2)
        import architect_hub as _ah
        rc1_ = _ah.restart_command(["C:/x/architect_hub.py", "--port", "8000", "--no-browser", "--foo"], port=8123)
        rc2_ = _ah.restart_command(["architect.py", "start"], port=8123)
        # started through `architect.py start` (START_HUB.bat), the restart is the hub itself: re-running `start` brought Ollama, the
        # servers and a second browser tab back and could land on another port
        check("the restart command relaunches the hub on its own port with no new tab, however it was started",
              rc1_[1:] == ["C:/x/architect_hub.py", "--foo", "--port", "8123", "--no-browser"] and os.path.isabs(rc2_[1])
              and os.path.basename(rc2_[1]) == "architect_hub.py" and rc2_[2:] == ["--port", "8123", "--no-browser"], (rc1_, rc2_))
        _t0_ = time.time() + 5
        os.utime(os.path.join(hubdir, "arch_usage.py"), (_t0_, _t0_))
        time.sleep(3.3)
        vs2_ = call("/api/arch/version")
        check("after a code file changes, the version endpoint says stale", vs2_.get("stale") is True and vs2_["fingerprint_now"] != vs2_["fingerprint_at_start"], vs2_)
        check("the page polls the version, shows the restart banner and treats a 404 from /api/arch/ as a reason to check",
              all(k in html for k in ("function staleCheck(", "The app was updated - restart the hub", "/api/arch/restart", "r.status===404&&/^\\/api\\/arch\\/")) and "setInterval(()=>{if(document.visibilityState===\"visible\")staleCheck();},60000)" in html)
        check("the page has one verdict mapping and uses it for Recent runs, Review and the run bar",
              "function verd(" in html and html.count("verd(r.verdict)") >= 2 and "const RV_WORD=VERD" in html and "verd(v)[1].toLowerCase()" in html)
        check("Home: 'Review my code' chip, 'Review this run' row buttons, Recent runs above Last night, chip hidden after 10 minutes",
              '["Review my code",' in html and html.count('"Review this run') >= 2 and html.index('h("h2",{class:"hsec"},"Recent runs")') < html.index('h("div",{id:"homeNight"})')
              and "Date.now()/1000-j.ended<=600" in html)
        check("Last night shows its source and hides the headline under 5 runs; usage sends the known folders",
              "Too few runs to say" in html and "d.latest.source" in html and "function usageRoots(" in html and "/api/arch/usage?window=day\"+usageRoots()" in html)
        check("Review leads with a large verdict, the first failing line and 'Retry with note'; the plan has 36 px controls and Alt+Arrow",
              "rv-verdict" in html and "d.first_failure" in html and "Retry with note" in html and ".rv-step .rv-mv{min-width:36px;min-height:36px" in html
              and "e.altKey&&(e.key===\"ArrowUp\"" in html and "planNorm(" in html)
        us2_ = call("/api/arch/usage?window=day&root=" + urllib.parse.quote(rvroot))
        check("usage reads the folders the page sends (the project has runs from today)", us2_.get("runs", 0) >= 5 and us2_.get("roots", 0) >= 1, us2_.get("runs"))
        sv_ = call("/api/arch/session?root=" + urllib.parse.quote(rvroot) + "&run=r1")
        check("the session panel splits the context, labels an estimate and lists servers and jobs",
              sv_.get("window") == 8192 and sv_.get("estimated") is True and [x["name"] for x in sv_.get("sections", [])][:1] == ["system prompt"]
              and isinstance(sv_.get("servers"), list) and isinstance(sv_.get("jobs"), list) and sv_["changes"]["added"] == 2, sv_)
        check("accepting and saving a plan need the token",
              call("/api/arch/review/accept", {"root": rvroot, "run": "r1"}, token=False).get("HTTP") in (401, 403)
              and call("/api/arch/plan", {"root": rvroot, "run": "r1", "items": []}).get("HTTP") == 400)
        ok_ = call("/api/arch/review/accept", {"root": rvroot, "run": "r1"})
        check("accepting marks the run and changes no file", ok_.get("ok") is True and os.path.isfile(os.path.join(rd, "accepted.json")), ok_)
        check("the Review page, plan card, session panel, queue board and recipe cards are in the hub page",
              all(k in open(os.path.join(HERE, "hub_ui.html"), encoding="utf-8").read() for k in ("function reviewPage()", "function rvPlan(", "function rvSession(", "function queueBoard(", "function loadRecipes(", "function tplContract(")))
        # ---- Per-hunk Keep / Drop, background jobs, hooks, usage, bundle, git commit, onboarding
        rd2 = os.path.join(rvroot, ".arch", "runs", "r2")
        os.makedirs(os.path.join(rd2, "backup"))
        pre_ = "".join(f"line{i}\r\n" for i in range(40))
        post_ = pre_.replace("line3\r\n", "LINE3\r\n").replace("line30\r\n", "LINE30\r\nextra\r\n")
        with open(os.path.join(rd2, "backup", "b.txt"), "wb") as f_:
            f_.write(pre_.encode())
        with open(os.path.join(rvroot, "b.txt"), "wb") as f_:
            f_.write(post_.encode())
        with open(os.path.join(rvroot, "c.txt"), "w", encoding="utf-8") as f_:
            f_.write("no copy from before\n")
        with open(os.path.join(rd2, "trace.jsonl"), "w", encoding="utf-8") as f_:
            for fn_ in ("b.txt", "c.txt"):
                f_.write(json.dumps({"type": "diff", "file": fn_, "added": 1, "removed": 1, "diff": "@@ -1 +1 @@\n-a\n+b\n"}) + "\n")
        with open(os.path.join(rd2, "run.json"), "w", encoding="utf-8") as f_:
            json.dump({"goal": "g", "flow": "build", "status": "done"}, f_)
        q2 = "root=" + urllib.parse.quote(rvroot) + "&run=r2"
        rv2 = call("/api/arch/review?" + q2)
        fb = {x["file"]: x for x in rv2.get("files", [])}
        check("review splits a file with a pre-run copy into hunks and says why another cannot be split",
              fb.get("b.txt", {}).get("splittable") is True and len(fb["b.txt"].get("hunks", [])) == 2 and all(h_["kept"] for h_ in fb["b.txt"]["hunks"])
              and fb.get("c.txt", {}).get("splittable") is False and "before the run" in fb["c.txt"].get("split_why", ""), rv2)
        hk0 = {"root": rvroot, "run": "r2", "file": "b.txt", "keep": [0]}
        check("Keep / Drop needs the token and refuses a bad hunk number, a path that climbs out and a file with no pre-run copy",
              call("/api/arch/review/hunks", hk0, token=False).get("HTTP") in (401, 403)
              and call("/api/arch/review/hunks", dict(hk0, keep=[9])).get("HTTP") == 400
              and call("/api/arch/review/hunks", dict(hk0, file="../x")).get("HTTP") == 400
              and call("/api/arch/review/hunks", dict(hk0, file="c.txt")).get("supported") is False)
        r_h = call("/api/arch/review/hunks", hk0)
        cur_ = open(os.path.join(rvroot, "b.txt"), "rb").read().decode()
        check("dropping a hunk rebuilds the file from the pre-run copy plus the kept hunks, line endings kept",
              r_h.get("ok") is True and "LINE3\r\n" in cur_ and "LINE30" not in cur_ and "line30\r\n" in cur_ and "\n" not in cur_.replace("\r\n", ""), r_h)
        r_h2 = call("/api/arch/review/hunks", dict(hk0, keep=[0, 1]))
        check("keeping every hunk again restores the run's own result exactly",
              open(os.path.join(rvroot, "b.txt"), "rb").read().decode() == post_ and r_h2.get("kept") == 2)
        bj = call("/api/arch/bgjobs")
        check("background jobs answer a list and stopping needs the token and a real job",
              isinstance(bj.get("jobs"), list) and call("/api/arch/bgjobs/stop", {"id": "hub"}, token=False).get("HTTP") in (401, 403)
              and call("/api/arch/bgjobs/stop", {"id": "../x"}).get("HTTP") == 400, bj)
        hq = "?root=" + urllib.parse.quote(rvroot)
        hv = call("/api/arch/hooks" + hq)
        check("hooks lists the points and both files", len(hv.get("points", [])) == 5 and "project" in hv and "machine" in hv, hv)
        hook_ = {"on": "after_apply", "run": "echo hi", "blocking": True}
        check("adding or trusting a hook needs the token and an explicit confirm",
              call("/api/arch/hooks", {"root": rvroot, "action": "add", "scope": "project", "hook": hook_, "confirm": True}, token=False).get("HTTP") in (401, 403)
              and call("/api/arch/hooks", {"root": rvroot, "action": "add", "scope": "project", "hook": hook_}).get("HTTP") == 400
              and call("/api/arch/hooks", {"root": rvroot, "action": "add", "scope": "project", "hook": dict(hook_, on="nope"), "confirm": True}).get("HTTP") == 400)
        ha = call("/api/arch/hooks", {"root": rvroot, "action": "add", "scope": "project", "hook": hook_, "confirm": True})
        check("a new project hook waits for trust, trusting runs it, and removing takes the yes back",
              ha.get("ok") is True and ha["view"]["project"]["hooks"][0]["run"] == "echo hi" and ha["view"]["project"]["trusted"] is False
              and call("/api/arch/hooks", {"root": rvroot, "action": "trust", "scope": "project", "confirm": True})["view"]["project"]["trusted"] is True
              and call("/api/arch/hooks", {"root": rvroot, "action": "remove", "scope": "project", "index": 0})["view"]["project"]["hooks"] == [], ha)
        us = call("/api/arch/usage?window=week")
        check("usage answers a headline and the figures", isinstance(us.get("headline"), str) and "local" in us and us.get("window") == "week", us)
        bz = call("/api/arch/bundle", {"root": rvroot, "run": "r1"})
        check("a report bundle is made for a run, refuses a bad run id and needs the token",
              os.path.exists(str(bz.get("path") or "")) and call("/api/arch/bundle", {"root": rvroot, "run": "../x"}).get("HTTP") == 400
              and call("/api/arch/bundle", {"root": rvroot, "run": "r1"}, token=False).get("HTTP") in (401, 403), bz)
        gc = call("/api/arch/git/commit", {"root": rvroot, "run": "r2"})
        check("the commit button answers in plain words when this is not a git project, and needs the token",
              gc.get("ok") is False and bool(gc.get("why")) and bool(gc.get("todo")) and call("/api/arch/git/commit", {"root": rvroot, "run": "r2"}, token=False).get("HTTP") in (401, 403), gc)
        ob = call("/api/arch/onboard", {"root": rvroot})
        check("set up this project reads the project and offers a draft; accept needs the token",
              ob.get("ok") is True and "draft" in ob and call("/api/arch/onboard", {"root": rvroot, "accept": True}, token=False).get("HTTP") in (401, 403), ob)
        check("the hub page has the Keep/Drop buttons, the jobs panel, the Hooks page, the usage chip, the commit and bundle buttons and the setup card",
              all(k in html for k in ("function rvHunkBox(", "function bgJobsCard(", "function hooksPage(", "id=\"usageChip\"", "function rvCommit(", "function rvBundle(", "function setupProject(",
                                      "function usageCard(", "aria-pressed", "\"hooks\",\"bolt\",\"Hooks\"")))
        check("select apply refuses a bad job or model name", call("/api/arch/select/apply", {"job": "Bad Job!", "model": "m"}).get("HTTP") == 400
              and call("/api/arch/select/apply", {"job": "coding", "model": "x; rm -rf /"}).get("HTTP") == 400)
        check("select apply, undo and screen need the token", all(call(p_, {"job": "coding", "model": "m"}, token=False).get("HTTP") in (401, 403)
              for p_ in ("/api/arch/select/apply", "/api/arch/select/undo", "/api/arch/select/screen")))
        check("select screen refuses a bad model name", call("/api/arch/select/screen", {"fake": True, "models": ["a b;c"]}).get("HTTP") == 400)
        check("Home quick-launch options write only settings that exist", "function qlOptions(" in html and all(
            any(r[0] == k for rows in (st.get("settings_schema") or {}).values() for r in rows) for k in
            ("behavior.review_mode", "behavior.thinking", "behavior.test_first", "behavior.best_of_n", "flows.budget_scale")))
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
        tdz = tdz_scan(js)
        check("no top-level const/let is read at load time before its declaration (a TDZ error blanks the whole hub)", not tdz, tdz[:5])
        check("the TDZ scan itself catches the G_CHORDS shape and ignores reads inside functions",
              tdz_scan("const K=[...Object.entries(G)];\nconst G={};") == [("G", 1)] and tdz_scan("function f(){return G;}\nconst h=()=>G;\nconst G=1;") == [])
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
        # Improve itself (Machine > Improve itself): the page, its reads, and every change behind the token
        si_posts = ("/api/arch/selfimprove/start", "/api/arch/selfimprove/again", "/api/arch/selfimprove/promote",
                    "/api/arch/selfimprove/discard", "/api/arch/selfimprove/rollback")
        check("Improve itself: every change needs the hub token",
              all(call(p_, {"id": "si00000000", "confirm": True}, token=False).get("HTTP") in (401, 403) for p_ in si_posts))
        sis = call("/api/arch/selfimprove/sessions")
        check("Improve itself: the session list and history are readable", isinstance(sis.get("sessions"), list) and isinstance(sis.get("history"), list), sis)
        check("Improve itself: a bad session id is refused", call("/api/arch/selfimprove/session?id=../x").get("HTTP") == 400)
        check("Improve itself: promote refuses without a confirmation or a passed session",
              call("/api/arch/selfimprove/promote", {"id": "si00000000"}).get("HTTP") == 400
              and call("/api/arch/selfimprove/promote", {"id": "si00000000", "confirm": True}).get("HTTP") == 400)
        check("Improve itself: the page is under Machine (at most 5 sub-tabs) with goal, Suggest, timeline, diff, Promote/Discard/Another round, Rollback",
              '["machine","server","Machine",["doctor","assess","computers","tune","selfimprove"]]' in html
              and all(x in html for x in ("function selfImprovePage", "Suggest something", "/api/arch/selfimprove/suggest", "si-diff",
                                          '"Promote"', '"Discard"', '"Another round"', "Roll back last promotion", "could not run", "emptyState(")))
    finally:
        hub.terminate()
        fake.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    print()
    ok = all(results)
    print(f"  {'ALL ' + str(len(results)) + ' HUB CHECKS PASSED' if ok else str(results.count(False)) + ' of ' + str(len(results)) + ' HUB CHECKS FAILED'}")
    return 0 if ok else 1


_ID = re.compile(r"[A-Za-z_$][\w$]*")
_REGEX_BEFORE = set("(,=:[!&|?{};+-*%<>~^")


def tdz_scan(js):
    """-> [(name, line)] for every top-level const/let read before its declaration by code that runs at load time.

    A simple ordered scan, not a parser: strings, template literals, comments and regex literals are skipped; code inside a
    function body (function ... {}, method(...) {}, x => {...}, or an expression arrow up to its end) is deferred and ignored.
    It can miss a use (a top-level call into a function that reads the name), but a hit is a real load-time read."""
    toks = []                      # (kind, text, pos, fdepth)
    i, n = 0, len(js)
    stack = []                     # per open bracket: True when it is a function body
    arrow = []                     # bracket depths at which an expression-bodied arrow is open
    pend_fn = False                # saw `function`; the next "{" after its params is a body
    prev = ""                      # last significant char/token for regex detection
    prev_tok = ""
    fdepth = 0

    def depth():
        return len(stack)
    while i < n:
        c = js[i]
        if c in " \t\r\n":
            i += 1
            continue
        if js.startswith("//", i):
            j = js.find("\n", i)
            i = n if j < 0 else j
            continue
        if js.startswith("/*", i):
            j = js.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "\"'":
            j = i + 1
            while j < n and js[j] != c:
                j += 2 if js[j] == "\\" else 1
            i = j + 1
            prev, prev_tok = "a", ""
            continue
        if c == "`":
            j, lvl = i + 1, 0
            while j < n:
                if js[j] == "\\":
                    j += 2
                    continue
                if js.startswith("${", j):
                    lvl += 1
                    j += 2
                    continue
                if js[j] == "}" and lvl:
                    lvl -= 1
                elif js[j] == "`" and not lvl:
                    break
                j += 1
            i = j + 1
            prev, prev_tok = "a", ""
            continue
        if c == "/" and (prev in _REGEX_BEFORE or prev == "" or prev_tok in ("return", "typeof", "case")):
            j, cls = i + 1, False
            while j < n and js[j] != "\n":
                if js[j] == "\\":
                    j += 2
                    continue
                if js[j] == "[":
                    cls = True
                elif js[j] == "]":
                    cls = False
                elif js[j] == "/" and not cls:
                    break
                j += 1
            j += 1
            while j < n and (js[j].isalpha()):
                j += 1
            i = j
            prev, prev_tok = "a", ""
            continue
        m = _ID.match(js, i)
        if m:
            w = m.group(0)
            if w == "function":
                pend_fn = True
            toks.append(("id", w, i, fdepth, js[i - 1] == "." if i else False))
            prev, prev_tok = "a", w
            i = m.end()
            continue
        if js.startswith("=>", i):
            k = i + 2
            while k < n and js[k] in " \t\r\n":
                k += 1
            if k < n and js[k] == "{":
                stack.append(True)
                fdepth += 1
                i = k + 1
            else:
                arrow.append(depth())
                fdepth += 1
                i += 2
            prev, prev_tok = "=", ""
            continue
        if c in "([{":
            # close expression arrows never happens on open
            body = False
            if c == "{":
                if pend_fn:
                    body, pend_fn = True, False
                elif prev == ")":
                    body = True                 # method(...) { } / if(...) { }: treated as deferred (may miss, never false hit)
            stack.append(body)
            if body:
                fdepth += 1
            prev, prev_tok = c, ""
            i += 1
            continue
        if c in ")]}":
            while arrow and arrow[-1] == depth():
                arrow.pop()
                fdepth -= 1
            if stack:
                if stack.pop():
                    fdepth -= 1
            prev, prev_tok = c, ""
            i += 1
            continue
        if c in ",;":
            while arrow and arrow[-1] == depth():
                arrow.pop()
                fdepth -= 1
        prev, prev_tok = c, ""
        i += 1
    decl = {}
    for k, t in enumerate(toks):
        if t[1] in ("const", "let") and t[3] == 0 and k + 1 < len(toks) and toks[k + 1][0] == "id":
            decl.setdefault(toks[k + 1][1], toks[k + 1][2])
    hits = []
    for t in toks:
        _, w, pos, fd, dotted = t
        if fd == 0 and not dotted and w in decl and pos < decl[w]:
            hits.append((w, js.count("\n", 0, pos) + 1))
    return hits


def _http_code(url):
    try:
        return urllib.request.urlopen(url).status
    except urllib.error.HTTPError as e:
        return e.code


if __name__ == "__main__":
    sys.exit(run())
