"""Self-test: six places that broke the platform's own rules (review 2026-11-09), each fixed and pinned here.

  1. a candidate that PASSED but lost to a smaller passing change is not a failure (it became a DPO "rejected": a correct answer
     taught as a bad one)
  2. nothing without a code check is "passed": text steps, review chunks, gate-less applies, tool-loop answers (they fed model ok-rates,
     few-shot examples and "known good" tool trajectories)
  3. a template whose dependency is missing could not run its tests: not_sure, never a pass
  4. a crew's verdict is decided by code: a failing test phase or a panel's majority "fail" fails it; files changed with no passing test
     is not_sure; a suite that could not run is not a failure
  5. the Console's box can start a run ("Do it"), and words sent to a running run steer it instead of stopping it
  6. "Run this plan" is offered only where the workflow reads an edited plan

    python arch_selftest_rules.py

A scripted stand-in model (fake:7b) on loopback, a throwaway ARCHITECT_HOME / ARCHITECT_DATA_DIR; nothing leaves the machine.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import types
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_core as core
    import arch_selftest as base

    seen = []

    class Model(base.Fake):
        def script(self, role, user, full, st):
            seen.append(user)
            return "ok"

    fake = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp(prefix="archrules_")
    keys = ("ARCHITECT_HOME", "ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE", "OLLAMA_HOST")
    old_env = {k: os.environ.get(k) for k in keys}
    old_home = core.HOME
    os.environ.update(ARCHITECT_DATA_DIR=os.path.join(tmp, "data"), ARCHITECT_NO_CACHE="1", OLLAMA_HOST=f"127.0.0.1:{fake.server_address[1]}")
    core.set_home(os.path.join(tmp, "home"))
    with open(core.CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"default_model": "fake:7b", "server": {"kind": "ollama"}}, f)
    try:
        src = {n: open(os.path.join(HERE, n), encoding="utf-8").read() for n in
               ("arch_dataflow.py", "arch_gates.py", "arch_flows_pro.py", "architect_hub.py", "hub_ui.html")}

        # 1. a passing loser is None, and the dataset then has no DPO pair that rejects a passing answer
        check("1. a candidate that passed but was not chosen is marked None ('not chosen'), never False",
              'None if x["ok"] is None or x["ok"] is True else False' in src["arch_dataflow.py"])
        import arch_dataset
        proj = os.path.join(tmp, "p1")
        rd = os.path.join(proj, ".arch", "runs", "r1")
        os.makedirs(rd)
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "fix it"}]
        with open(os.path.join(rd, "trace.jsonl"), "w", encoding="utf-8") as f:
            for cid, reply in (("c1", "def f():\n    return 1\n"), ("c2", "def f():\n    return 1  # longer but also right\n")):
                f.write(json.dumps({"type": "call", "id": cid, "role": "generator", "model": "qwen3:8b", "messages": msgs, "reply": reply}) + "\n")
            f.write(json.dumps({"type": "mark", "id": "c1", "accepted": True}) + "\n")
            f.write(json.dumps({"type": "mark", "id": "c2", "accepted": None, "why": "passed, but a smaller change also passed"}) + "\n")
            f.write(json.dumps({"type": "verdict", "verdict": "pass"}) + "\n")
        try:
            ds = arch_dataset.build([proj])
            pairs = sum(len(v) for v in (ds.get("dpo") or {}).values())
            check("   the dataset makes no preference pair out of two passing answers", pairs == 0, ds.get("stats"))
        except Exception as ex:
            check("   the dataset makes no preference pair out of two passing answers", False, f"{type(ex).__name__}: {ex}")

        # 2. a text step is marked None
        check("2. nothing is marked passed without a code check: text steps, review chunks, gate-less applies, tool-loop answers (all were True)",
              'e.mark(r["id"], None, "a text step: no code check")' in src["arch_dataflow.py"]
              and 'e.mark(r["id"], True)' not in src["arch_dataflow.py"]
              and 'e.mark(final_id, g["ok"] if g is not None else None' in src["arch_dataflow.py"])
        import arch_ledger
        led = {}
        try:
            arch_ledger.record_run.__code__                       # the ledger skips None marks (the fix relies on it)
            check("   the ledger leaves None marks out of ok rates and few-shot examples", "ok is None" in open(os.path.join(HERE, "arch_ledger.py"), encoding="utf-8").read())
        except AttributeError:
            check("   the ledger leaves None marks out of ok rates and few-shot examples", False, led)

        # 3. template with a missing dependency: None (gate) and unsure (legacy flow)
        check("3. a template whose dependency is missing ends not_sure, in both flow engines (it was a pass)",
              'return R(None, f"its tests could not run: install' in src["arch_gates.py"]
              and 'return R(True, "", {"template": tid, "files": len(written), "needs": tpl["needs"]})' not in src["arch_gates.py"]
              and 'return {"ok": ok, "files": written, "unsure": unsure}' in src["arch_flows_pro.py"])

        # 4. crew verdicts from code
        import arch_squad as sq
        crew = lambda judged, edited: types.SimpleNamespace(judged=judged, records=[{"edited": ["a.py"] if edited else []}])   # noqa: E731
        check("4. a crew whose test phase failed, or whose panel voted fail, fails",
              sq._code_verdict(crew([("suites", "fail")], True)) == "fail" and sq._code_verdict(crew([("panel", "fail")], False)) == "fail")
        check("   a crew that changed files and was never tested is not_sure (it passed when every agent answered)",
              sq._code_verdict(crew([], True)) == "not_sure" and sq._code_verdict(crew([("suites", "pass")], True)) is None
              and sq._code_verdict(crew([], False)) is None)
        rs = sq.run_suites(tmp, ["no_such_suite.py"])
        check("   a suite that could not run is not_sure, not a failure", rs["verdict"] == "not_sure" and rs["failed"] == 0 and rs["could_not_run"] == 1, rs)

        # 5. the engine follows words sent mid-run; redirect no longer writes STOP by default; the Console has Do it
        root = os.path.join(tmp, "p5")
        os.makedirs(root)
        with open(os.path.join(root, "app.py"), "w", encoding="utf-8") as f:
            f.write("def f():\n    return 1\n")
        e = core.Engine(root, "build", "add a feature", cfg=core.load_config(root), persist=True, say=lambda m: None, review="auto")
        os.makedirs(e.run_dir, exist_ok=True)
        with open(e.inbox_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": 0, "message": "use a dict, not a list"}) + "\n")
        seen.clear()
        try:
            e.call("generator", "write f")
        except Exception as ex:
            seen.append(f"error: {ex}")
        check("5. a message sent mid-run reaches the next model call, at the end of the prompt (the run keeps going)",
              seen and "FROM THE PERSON" in seen[-1] and seen[-1].rstrip().endswith("use a dict, not a list")
              and not os.path.isfile(os.path.join(root, ".arch", "STOP")), seen[-1:] if seen else "no call")
        check("   redirect steers by default (it wrote STOP and ended the run); stopping is an explicit choice",
              'if b.get("stop", False):' in src["architect_hub.py"] and 'if b.get("stop", True):' not in src["architect_hub.py"])
        check("   the Console's box has Do it (a run) next to Ask (the chat, which only talks)",
              "async function cDoIt()" in src["hub_ui.html"] and 'id:"cDo"' in src["hub_ui.html"] and 'api("/api/arch/run",{goal:txt' in src["hub_ui.html"]
              and "it finishes the current call, then follows your words" not in src["hub_ui.html"])

        # 6. the plan is editable only for a workflow that reads an edited plan
        import architect_hub as ah
        rr = os.path.join(tmp, "p6")
        for flow in ("bugfix", "explore"):
            d = os.path.join(rr, ".arch", "runs", flow)
            os.makedirs(d)
            with open(os.path.join(d, "run.json"), "w", encoding="utf-8") as f:
                json.dump({"flow": flow, "goal": "x"}, f)
        import arch_dataflow
        import arch_flows as fl
        legacy_flow = next((k for k, v in fl.FLOWS.items() if not isinstance(v, arch_dataflow.DataFlow)), None)
        data_flow = next((k for k, v in fl.FLOWS.items() if isinstance(v, arch_dataflow.DataFlow)), None)
        res = {}
        for fid in (legacy_flow, data_flow):
            d = os.path.join(rr, ".arch", "runs", "r_" + str(fid))
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "run.json"), "w", encoding="utf-8") as f:
                json.dump({"flow": fid, "goal": "x"}, f)
            res[fid] = ah.plan_view(rr, "r_" + str(fid))
        check("6. 'Run this plan' is offered for a YAML workflow and not for one that runs as built-in code (it always was)",
              legacy_flow and data_flow and res[legacy_flow]["editable"] is False and res[data_flow]["editable"] is True
              and "built-in code" in res[legacy_flow]["why"], {k: (v["editable"], v["why"][:60]) for k, v in res.items()})
    finally:
        fake.shutdown()
        core.set_home(old_home)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' RULES CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' RULES CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
