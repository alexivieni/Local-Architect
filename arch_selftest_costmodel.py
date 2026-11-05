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

    # the hub's Cost map page (static: the page source, no browser)
    import re
    html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "hub_ui.html"), encoding="utf-8").read()
    a, b = html.find("/* ===== Cost map (Models)"), html.find("function benchPage(){")
    block = html[a:b] if 0 <= a < b else ""
    check("hub: the Cost map is a page in the Models place, with its own name, icon and drawing",
          '["team","chip","Models",["tiers","computers","costmap"' in html and 'costmap:"Cost map"' in html and 'costmap:"target"' in html
          and 'costmap:"models"' in html and 'if(page==="costmap")' in html)
    check("hub: it draws only with theme colours (no literal colour), so every theme and accent applies",
          bool(block) and not re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", block), re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", block)[:4])
    check("hub: every point is a keyboard button with a spoken description; the chart has a text alternative",
          'tabindex="0" role="button"' in block and 'aria-label="${escx(tip)}"' in block and 'role="img" aria-label="Cost map' in block
          and 'e.key==="Enter"||e.key===" "' in block)
    check("hub: it reads /api/arch/cost and only shows; it never assigns a model",
          'api("/api/arch/cost")' in block and "not a recommendation to switch" in block
          and not re.search(r"/api/(arch/)?(config|roles|tiers|settings)|setRole|saveConfig", block))
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' COSTMODEL CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' COSTMODEL CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
