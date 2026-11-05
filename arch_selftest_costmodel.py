"""Self-test for arch_costmodel: the report's equations reproduce the report's own worked examples, and measure() reads traces right.

    python arch_selftest_costmodel.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run():
    import arch_costmodel as cm
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({extra})" if not ok and extra else ""))

    # Eq. (15): the report's example, an 8k-token context then 300 tokens of feedback -> rho ~ 0.96
    rho = 8000 / 8300
    check("Eq. 15: an 8k cached context plus 300 new tokens gives rho ~ 0.96, and caching cuts the prompt part by that share",
          abs(rho - 0.964) < 0.001 and abs(cm.call_time(8300, 0, rho, 1000, 0) - 0.3) < 1e-9)
    # Eq. (16) and (17)
    check("Eq. 16: C = t / p, independent of the attempt cap", cm.cost_constant(30, 0.5) == 60 and cm.cost_constant(30, 0) is None)
    c, pb = cm.cost(30, [0.5, 0.5, 0.5])
    check("Eq. 17 reduces to Eq. 16 when every attempt has the same p", abs(pb - 0.5) < 1e-9 and abs(c - 60) < 1e-9, (c, pb))
    c2, pb2 = cm.cost(30, [0.4, 0.6, 0.8])
    check("Eq. 17: feedback that raises p on later tries gives p_bar >= p_1, so C <= t / p_1", pb2 >= 0.4 and c2 <= 30 / 0.4, (c2, pb2))
    # Eq. (18)
    check("Eq. 18: J = sum c t / p + lambda S", abs(cm.run_time([(20, 15, 0.5), (1, 30, 0.8)], lam=15, swaps=2) - (600 + 37.5 + 30)) < 1e-9)
    # Eq. (19): the report's worked example
    pays, left, right = cm.planner_pays(20, 15, 0.40, 0.50, 1, 120, 0.9, 30, 0.8, 15)
    check("Eq. 19, the report's example: a 0.40 -> 0.50 builder gain pays for the 4x slower 32B planner (150 s > ~111 s)",
          pays and abs(left - 150) < 1e-6 and abs(right - 110.83) < 0.01, (left, right))
    pays2, left2, _ = cm.planner_pays(20, 15, 0.40, 0.45, 1, 120, 0.9, 30, 0.8, 15)
    check("Eq. 19: a 0.40 -> 0.45 gain does not pay (~83 s < ~111 s)", not pays2 and abs(left2 - 83.33) < 0.01, left2)

    # measure(): a run where try 1 fails and try 2 passes, plus one passed first time and one 'could not run'
    d = tempfile.mkdtemp(prefix="costmodel_")
    try:
        rd = os.path.join(d, ".arch", "runs", "r1")
        os.makedirs(rd)
        recs = []
        for i, (label, ok, t) in enumerate([("build try 1", False, 1), ("build try 2", True, 2), ("plan", True, 3), ("judge", None, 4)], 1):
            role = "generator" if label.startswith("build") else ("planner" if label == "plan" else "evaluator")
            recs.append({"type": "call", "id": i, "t": t, "role": role, "model": "w:8b", "label": label, "secs": 10,
                         "prompt_tokens": 1000, "prefix_reused_est": 900})
            recs.append({"type": "mark", "id": i, "accepted": ok})        # the field Engine.mark writes
        recs.append({"type": "call", "id": 9, "t": 5, "role": "generator", "model": "w:8b", "label": "build try 3", "secs": 0,
                     "done_reason": "cached", "prompt_tokens": 1000})
        recs.append({"type": "mark", "id": 9, "accepted": True})           # a replayed reply is not a new attempt
        with open(os.path.join(rd, "trace.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r) for r in recs))
        rows = cm.measure([d], min_calls=1, power_w=40)
        g = next((r for r in rows if r["role"] == "generator"), None)
        check("measure(): tries of one step are attempts 1, 2, ...; p_i comes from the checks' marks", g and g["p_i"] == [0.0, 1.0], g)
        check("measure(): rho from cached vs new prompt tokens, C and energy E = P * C reported", g and g["rho"] == 0.9 and g["C"] and g["E_joules"] == round(40 * g["C"]), g)
        check("measure(): a reply whose checks could not run (None) is not evidence", not any(r["role"] == "evaluator" for r in rows), rows)
        check("report() names the quantity in plain words", "seconds per ACCEPTED change" in cm.report(rows))
        ev = os.path.join(d, "ev.json")
        with open(ev, "w", encoding="utf-8") as f:
            json.dump({"fake": False, "results": [
                {"flow": "bugfix", "verdict": "pass", "secs": 40, "models": {"a:8b": 3}},
                {"flow": "bugfix", "verdict": "fail", "secs": 60, "models": {"a:8b": 3}},
                {"flow": "bugfix", "verdict": "not_sure", "secs": 20, "models": {"a:8b": 2, "heavy:30b": 1}},
                {"flow": "bugfix", "verdict": "pass", "secs": 90, "models": {"b:14b": 3}}]}, f)
        er = {r["model"]: r for r in cm.from_evals([ev])}
        check("from_evals(): C = seconds spent / tasks passed, 'not sure' counts its seconds but not in p",
              er["a:8b"]["C"] == 120 and er["a:8b"]["p"] == 0.5 and er["a:8b"]["not_sure"] == 1 and er["b:14b"]["C"] == 90, er)
        check("from_evals(): the slower model that passes ranks first (lower C)", cm.from_evals([ev])[0]["model"] == "b:14b")
        check("from_evals(): the kind of task is the family (one flow serves e2e builds and training tasks alike)", {r["kind"] for r in cm.from_evals([ev])} == {"bugfix"} and all(r["flow"] == "bugfix" for r in cm.from_evals([ev])))
        ev2 = os.path.join(d, "ev2.json")
        with open(ev2, "w", encoding="utf-8") as f:
            json.dump({"fake": False, "ts": 2, "label": "later", "results": [
                {"id": "t9", "flow": "bugfix", "verdict": "pass", "secs": 30, "models": {"huihui_ai/qwen3-abliterated:8b": 2}},
                {"id": "t1", "flow": "bugfix", "verdict": "pass", "secs": 30, "models": {"a:8b": 3}}]}, f)
        with open(ev, encoding="utf-8") as f:
            first = dict(json.load(f), ts=1, label="first")
        with open(ev, "w", encoding="utf-8") as f:
            json.dump(first, f)
        two = {r["model"]: r for r in cm.from_evals([ev, ev2])}
        a = two["a:8b"]
        check("from_evals(): history is the point per report, oldest first, so a change that moved it shows",
              [h["label"] for h in a["history"]] == ["first", "later"] and a["history"][0]["C"] == 120 and a["history"][1]["C"] == 30, a["history"])
        check("from_evals(): each point lists its tasks (newest first) and flags orthogonalized models by name",
              a["runs"][0]["id"] == "t1" and len(a["runs"]) == 4 and two["huihui_ai/qwen3-abliterated:8b"]["orthogonalized"] and not a["orthogonalized"], a["runs"][:2])
    finally:
        shutil.rmtree(d, ignore_errors=True)

    pc = cm.p_cheapest([{"model": "a", "kind": "k", "tasks": 20, "passed": 20, "t": 30}, {"model": "b", "kind": "k", "tasks": 20, "passed": 2, "t": 30},
                        {"model": "x", "kind": "j", "tasks": 3, "passed": 3, "t": 40}, {"model": "y", "kind": "j", "tasks": 3, "passed": 3, "t": 40}])
    check("p_cheapest: a clear winner is near certain; two identical records split about evenly (Thompson draws)",
          pc["k"]["a"] > 0.97 and 0.35 < pc["j"].get("x", 0) < 0.65, pc)

    saved_memo, saved_eval = dict(cm._STOP_MEMO), os.environ.pop("ARCHITECT_EVAL", None)
    saved_st = os.environ.pop("ARCHITECT_SELFTEST", None)
    try:
        cm._STOP_MEMO.update(t=1e18, stats={("g", "m"): {1: [3, 20], 2: [2, 15], 3: [0, 12], 4: [0, 11]},
                                            ("g", "n"): {3: [0, 12], 4: [1, 5]}, ("g", "thin"): {3: [0, 4]}})
        check("retry_worth: stops only when 10+ measured attempts at that try all failed and no later try ever passed",
              cm.retry_worth("g", "m", 3) is False and cm.retry_worth("g", "n", 3) and cm.retry_worth("g", "thin", 3) and cm.retry_worth("g", "m", 2))
        os.environ["ARCHITECT_EVAL"] = "1"
        check("retry_worth: never stops in a benchmark, nor with behavior.retry_stop = off",
              cm.retry_worth("g", "m", 3) and (os.environ.pop("ARCHITECT_EVAL") and cm.retry_worth("g", "m", 3, {"behavior": {"retry_stop": "off"}})))
    finally:
        cm._STOP_MEMO.clear()
        cm._STOP_MEMO.update(saved_memo)
        for k, v in (("ARCHITECT_EVAL", saved_eval), ("ARCHITECT_SELFTEST", saved_st)):
            if v is not None:
                os.environ[k] = v

    # the hub's Cost map page (static: the page source, no browser)
    import re
    html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "hub_ui.html"), encoding="utf-8").read()
    a, b = html.find("/* ===== Cost map (Models)"), html.find("function benchPage(){")
    block = html[a:b] if 0 <= a < b else ""
    check("hub: the Cost map is a page in the Models place, with its own name, icon and drawing",
          '["cost","target","Speed",["costmap"' in html and '["costmap","target",' in html
          and 'costmap:"models"' in html and 'if(page==="costmap")' in html)
    check("hub: it draws only with theme colours (no literal colour), so every theme and accent applies",
          bool(block) and not re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", block), re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", block)[:4])
    check("hub: every point is a keyboard button with a spoken description; the chart has a text alternative",
          'tabindex="0" role="button"' in block and 'aria-label="${escx(tip)}"' in block and 'role="img" aria-label="Cost map' in block
          and 'e.key==="Enter"||e.key===" "' in block)
    check("hub: it reads /api/arch/cost and only shows; it never assigns a model",
          'api("/api/arch/cost")' in block and "not a recommendation to switch" in block
          and not re.search(r"/api/(arch/)?(config|roles|tiers|settings)|setRole|saveConfig", block))

    # the same measurement where work is picked: chips on the Gallery's workflow cards, a line under the route preview
    def body(start, stop):
        i = html.find(start)
        j = html.find(stop, i + 1) if i >= 0 else -1
        return html[i:j] if 0 <= i < j else ""
    gal, route = body("function galDraw(){", "\n}\n"), body("async function previewRoute(quiet){", "\n}\n")
    opener = body("function cxOpen(kind,hide){", "\n}\n")
    write_api = r"/api/(arch/)?(config|roles|tiers|settings|team|assign)|setRole|saveConfig"
    check("hub: the Gallery adds one group of measured chips per workflow card, and the route preview one line, from the cached read",
          "cxLoad().then(" in gal and 'c.dataset.flow=f.id' in gal and '!c.querySelector(".gal-costs")' in gal and "cxChips(rows,c.dataset.flow)" in gal
          and "cxLoad().then(" in route and "cxLine(rows,r.flow)" in route and "note.isConnected" in route)
    check("hub: the chips and the line only read (no settings, team or role write; a click never stores a preference)",
          bool(gal and route and opener) and not re.search(write_api, gal + route + opener) and "store.set(" not in opener,
          re.findall(write_api, gal + route + opener)[:3])
    check("hub: each chip is a keyboard button whose spoken name starts with its visible words, in the theme's chip style",
          'h("button",{type:"button",class:"chip gal-cost"' in block and '"aria-label":txt+" ("+says+"). Opens the Cost map."' in block)
    css_rule = body(".gal-costs{", "\n")
    check("hub: the chips' own CSS uses theme tokens only", bool(css_rule) and not re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css_rule), css_rule[:160])
    check("hub: the endpoint is read once and kept for the page session (a failed read is retried after a minute, never shown)",
          'CX.p=api("/api/arch/cost")' in block and ".catch(()=>[])" in block and "Date.now()-CX.at<60000" in block)

    # ... and what they say, run in node on fixture rows (could not run without node: not counted as a pass)
    import shutil as _sh
    import subprocess
    node = _sh.which("node")
    if not node:
        print("  ----  hub: the chips say the right thing on fixture rows   (could not run: node is not installed)")
    else:
        def row(model, kind, flow, tasks, passed, t, ids):
            return {"model": model, "kind": kind, "flow": flow, "tasks": tasks, "passed": passed, "not_sure": 0, "t": t,
                    "p": round(passed / tasks, 2), "C": round(t * tasks / passed, 1) if passed else None,
                    "orthogonalized": "abliterat" in model, "runs": [{"id": i} for i in ids]}
        ab4, ab8 = "huihui_ai/qwen3-abliterated:4b", "huihui_ai/qwen3-abliterated:8b"
        rows = [
            row("q:8b", "bugfix", "bugfix", 3, 3, 38.4, ["b1", "b2", "b3"]),
            row(ab4, "bugfix", "bugfix", 6, 6, 40.4, ["b1", "b2", "b3", "b1", "b2", "b3"]),        # one task set, run twice
            row("g:8b", "bugfix", "bugfix", 3, 1, 42.5, ["b1", "b2", "b3"]),
            row("q:8b", "build", "build", 3, 3, 58.8, ["u1", "u2", "u3"]),
            row(ab4, "build", "build", 3, 1, 36.0, ["u1", "u2", "u3"]),
            row("q:8b", "e2e", "build", 4, 0, 154.7, ["e1", "e2", "e3", "e4"]),                    # the hard family the build flow also runs
            row(ab8, "e2e", "build", 4, 0, 95.9, ["e1", "e2", "e3", "e4"]),
            row(ab4, "distributed", "build", 1, 0, 177.1, ["d1"]),
            row(ab8, "feature", "feature", 3, 3, 38.3, ["f1", "f2", "f3"]),
            row("q:8b", "refactor", "refactor", 2, 1, 137.5, ["r1", "r2"]),
            row(ab4, "refactor", "refactor", 2, 0, 43.0, ["r1", "r2"]),
            row(ab4, "infra", "deploy", 4, 1, 60.0, ["i1", "i2", "i3", "i4"]),                     # no family named "deploy": the fallback
            row("q:8b", "dbmig", "deploy", 1, 1, 30.0, ["m1"]),
            row("q:8b", "zeta", "tie", 2, 1, 10.0, ["z1", "z2"]),
            row("q:8b", "alpha", "tie", 2, 1, 10.0, ["a1", "a2"]),
            row("q:8b", "cicd", "cicd", 2, 0, 50.0, ["c1", "c2"]),                                   # measured only on a non-orthogonalized model
            dict(row(ab4, "docs", "docs", 2, 0, 20.0, ["o1", "o2"]), not_sure=2),                    # checks could not run: not a fail
        ]
        a1, b1 = html.find("const cmName="), html.find("async function loadCostmap(){")
        a2, b2 = html.find("const CX={"), html.find("function benchPage(){")
        script = r'''"use strict";
const h=(tag,attrs,...kids)=>({tag,attrs:attrs||{},kids:kids.flat(2).filter(k=>k!=null&&k!==false)});
const T=e=>e.kids.map(k=>k&&k.tag?T(k):String(k)).join("");
let page=null,toasts=[],apiCalls=0;const go=p=>{page=p;},toast=m=>toasts.push(m);
const ST={};const store={get:(k,d)=>k in ST?ST[k]:d,set:(k,v)=>{ST[k]=v;}};
const CM={flow:"all",sel:"x",orth:false,data:null};
const ROWS=''' + json.dumps(rows) + r''';
const api=async p=>{apiCalls++;return {evals:ROWS};};
''' + html[a1:b1] + "\n" + html[a2:b2] + r'''
(async()=>{
  const out={};
  await cxLoad();await cxLoad();out.calls=apiCalls;
  const b=cxFamily(ROWS,"build");out.buildKind=b.kind;out.buildAlso=b.also.map(a=>a.kind);
  const bc=cxChips(ROWS,"build").kids;out.buildChips=bc.map(c=>[T(c),c.attrs.class,c.attrs["aria-label"]]);out.buildTip=bc[0].attrs.title;
  out.buildLine=T(cxLine(ROWS,"build"));
  out.bugTip=cxChips(ROWS,"bugfix").kids[0].attrs.title;out.bugLine=T(cxLine(ROWS,"bugfix"));
  const d=cxFamily(ROWS,"deploy");out.deployKind=d.kind;out.deployAlso=d.also.map(a=>a.kind);out.deployChip=T(cxChips(ROWS,"deploy").kids[0]);
  out.tieKind=cxFamily(ROWS,"tie").kind;
  out.none=[cxChips(ROWS,"review"),cxLine(ROWS,"review"),cxChips([],"build")];
  const ns=cxChips(ROWS,"docs").kids[0];out.docs=[T(ns),ns.attrs.class,ns.attrs.title];
  out.featTip=cxChips(ROWS,"feature").kids[0].attrs.title;
  ST.cmOrth="1";CM.orth=true;
  cxChips(ROWS,"refactor").kids[0].attrs.onclick();
  out.refClick=[CM.flow,CM.sel,CM.orth,page,ST.cmOrth,toasts.length];
  costmapPage();out.thisVisit=CM.orth;costmapPage();out.laterVisit=CM.orth;
  toasts=[];cxChips(ROWS,"feature").kids[0].attrs.onclick();out.featClick=[CM.flow,CM.orth,toasts.length];
  cxChips(ROWS,"cicd").kids[0].attrs.onclick();out.cicdClick=[CM.flow,CM.orth,toasts.length];
  costmapPage();costmapPage();
  console.log(JSON.stringify(out));
})().catch(e=>{console.log(JSON.stringify({error:String(e&&e.stack||e)}));});
'''
        d = tempfile.mkdtemp(prefix="archcx_")
        try:
            p = os.path.join(d, "cx.js")
            with open(p, "w", encoding="utf-8") as f:
                f.write(script)
            r = subprocess.run([node, p], capture_output=True, text=True, encoding="utf-8", timeout=60)
            try:
                o = json.loads(r.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                o = {"error": (r.stdout + r.stderr)[-300:]}
        finally:
            shutil.rmtree(d, ignore_errors=True)
        check("chips: the helpers run (node)", "error" not in o and all(k in o for k in ("buildChips", "refClick")), o.get("error"))
        if "error" not in o:
            check("chips: /api/arch/cost is asked once, however often the Gallery redraws", o["calls"] == 1, o["calls"])
            check("chips: a workflow's own family comes first; the other families it also ran follow, most runs first",
                  o["buildKind"] == "build" and o["buildAlso"] == ["e2e", "distributed"], (o["buildKind"], o["buildAlso"]))
            bc = o["buildChips"]
            check("chips: the lead chip says it is the best measured; whole projects and the training tasks get their own warning chips",
                  [c[0] for c in bc] == ["Best on your models: 59 s per passed task", "Whole projects: not passed yet on your models",
                                         "Distributed systems (training): not passed yet on your models"]
                  and "warn" not in bc[0][1] and "warn" in bc[1][1] and "warn" in bc[2][1], bc)
            check("chips: each spoken name starts with the visible words and names the model and its runs",
                  all(c[2].startswith(c[0]) and c[2].endswith("Opens the Cost map.") for c in bc) and "(q:8b, 3 of 3 runs passed)" in bc[0][2]
                  and "(0 of 8 runs passed)" in bc[1][2] and "(0 of 1 run passed)" in bc[2][2], [c[2] for c in bc])
            check("chips: the lead tooltip names the cheapest orthogonalized model and the families the workflow also ran",
                  "Cheapest orthogonalized: qwen3-abliterated:4b, 108 s per passed task, 1 of 3 runs passed." in o["buildTip"]
                  and "Also run by this workflow: whole projects, not passed yet (0 of 8 runs passed); distributed systems (training)" in o["buildTip"],
                  o["buildTip"])
            check("chips: the route line says the same, the other families on a second line",
                  o["buildLine"].startswith("Measured on your models: best 59 s per passed task (q:8b, 3 of 3 runs passed); cheapest orthogonalized")
                  and "Also run by this workflow: whole projects, not passed yet" in o["buildLine"], o["buildLine"])
            check("chips: one unit each: runs per model, different tasks per family (6 of 6 runs over 3 tasks is not '6 of 3')",
                  "qwen3-abliterated:4b, 40 s per passed task, 6 of 6 runs passed." in o["bugTip"] and "3 models measured on 3 different tasks, 12 runs in all." in o["bugTip"]
                  and "(q:8b, 3 of 3 runs passed)" in o["bugLine"], o["bugTip"])
            check("chips: with no family named after the workflow, the family it ran most leads and is named; a tie goes alphabetical",
                  o["deployKind"] == "infra" and o["deployAlso"] == ["dbmig"] and o["deployChip"] == "Infra: best 4 min per passed task"
                  and o["tieKind"] == "alpha", (o["deployKind"], o["deployAlso"], o["deployChip"], o["tieKind"]))
            check("chips: nothing is shown for a workflow with no measured rows, or when the read came back empty", o["none"] == [None, None, None], o["none"])
            check("chips: checks that could not run read 'not passed yet', never 'failed'",
                  o["docs"][0] == "Not passed yet on your models" and "warn" in o["docs"][1] and "fail" not in o["docs"][2].lower(), o["docs"])
            check("chips: information only, in plain words (no 'switch', 'use' or 'try')",
                  all("Information only" in t and not re.search(r"\b(switch|use|try)\b", t, re.I) for t in (o["buildTip"], o["bugTip"], o["featTip"])))
            check("chips: a click opens that family; when the saved 'Only orthogonalized' filter would hide the figure it is set aside "
                  "for that visit only, said in a toast, and the saved choice is untouched",
                  o["refClick"] == ["refactor", None, False, "costmap", "1", 1] and o["thisVisit"] is False and o["laterVisit"] is True, o)
            check("chips: an orthogonalized best keeps the filter on; a family never measured on one sets it aside (it would vanish)",
                  o["featClick"] == ["feature", True, 0] and o["cicdClick"] == ["cicd", False, 1], (o["featClick"], o["cicdClick"]))
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' COSTMODEL CHECKS PASSED' + ('' if node else ' (1 could not run: node is not installed)') if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' COSTMODEL CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
