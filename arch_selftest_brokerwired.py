"""Self-test for the broker wired into a run's start (arch_broker.advise, arch_flows.broker_advice): advice picks by C = t / p and says
how sure; with no measured data it says so and suggests nothing; the run prints one line, traces a {"type": "broker"} record, writes
one report line and logs to broker.jsonl; behavior.broker = off does nothing; the model that runs is never changed.
Isolated ARCHITECT_DATA_DIR, fixture eval reports, no model is called (the flow is a stand-in that makes no call)."""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _report(path, results, fake=False):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ts": 1, "label": os.path.basename(path), "fake": fake, "results": results}, f)


def _task(model, verdict, secs, family="build", flow="build"):
    return {"id": f"{model}-{verdict}-{secs}", "flow": flow, "family": family, "verdict": verdict, "secs": secs, "models": {model: 5}}


class _Flow:
    id, title, produces, steps = "build", "stand-in", "", []

    def run(self, e, params):
        return {"ok": True, "dry": True}


def run():
    results = []

    def check(name, ok, detail=""):
        results.append(bool(ok))
        print(f"  [{'ok' if ok else 'FAIL'}] {name}" + ("" if ok else f"  -- {detail}"))

    home = tempfile.mkdtemp(prefix="arch_brokerwired_")
    keys = ("ARCHITECT_DATA_DIR", "ARCHITECT_SELFTEST", "ARCHITECT_NO_CACHE")
    old_env = {k: os.environ.get(k) for k in keys}
    os.environ.update(ARCHITECT_DATA_DIR=os.path.join(home, "data"), ARCHITECT_SELFTEST="1", ARCHITECT_NO_CACHE="1")
    try:
        import arch_core as core
        import arch_broker as B
        import arch_flows as F
        import arch_costmodel as CM
        data = core.data_dir()
        os.makedirs(os.path.join(data, "evals"), exist_ok=True)
        # fast:a passes 1 of 4 at 20 s each -> C = 80; slow:b passes 4 of 4 at 50 s -> C = 50 (cheapest); a fake report is ignored
        _report(os.path.join(data, "evals", "r1.json"),
                [_task("fast:a", "pass", 20)] + [_task("fast:a", "fail", 20) for _ in range(3)] + [_task("slow:b", "pass", 50) for _ in range(4)])
        _report(os.path.join(data, "evals", "fake.json"), [_task("oracle:x", "pass", 1) for _ in range(9)], fake=True)
        check("the fixture reports are found", len(B.eval_paths()) == 2, B.eval_paths())
        rows = CM.from_evals(B.eval_paths())
        cfg = {"behavior": {"broker": "suggest"}}
        a = B.advise(cfg, "build", "goal", current="mine:7b", state={})
        check("advice picks the lowest C, not the fastest model", a and a["suggest"] == "slow:b" and a["C"] == 50.0, a)
        check("advice states how sure, as a percentage", a and a["sure"] is not None and "% sure" in a["why"] and "% sure" in a["line"], a)
        check("advice names the model that will actually run", a and a["current"] == "mine:7b" and "mine:7b runs it" in a["line"], a)
        check("the oracle (fake) report is not evidence", a and all(r["model"] != "oracle:x" for r in rows))
        check("family by the same id first, else by flow", B.family_rows([{"kind": "e2e", "flow": "build"}], "build") == [{"kind": "e2e", "flow": "build"}]
              and B.family_rows([{"kind": "build", "flow": "x"}, {"kind": "e2e", "flow": "build"}], "build") == [{"kind": "build", "flow": "x"}])
        n = B.advise(cfg, "secure", "goal", current="mine:7b", state={})
        check("no data: it says so and suggests nothing", n and n["suggest"] is None and "nothing" in n["line"] and "no measured" in n["why"], n)
        check("advice is logged to broker.jsonl", any(r.get("target") == "advice" for r in B.recent(10)), B.recent(10))
        check("off: advise returns nothing", B.advise({"behavior": {"broker": "off"}}, "build") is None)

        proj = os.path.join(home, "proj")
        os.makedirs(proj)
        said = []
        rcfg = core.deep_merge(core.load_config(proj), {"roles": {"generator": "mine:7b"}, "behavior": {"broker": "suggest", "git": "off"}})
        e = core.Engine(proj, "build", "make a thing", rcfg, review="auto", say=lambda m, *_a, **_k: said.append(str(m)))
        before = e.model_for("generator")
        F.FLOWS["_bw_test"] = _Flow()
        try:
            F.run_flow(e, "_bw_test")
        finally:
            F.FLOWS.pop("_bw_test", None)
        line = [s for s in said if s.startswith("Measured:")]
        check("the run prints one Measured line", len(line) == 1, said)
        recs = [json.loads(x) for x in open(os.path.join(e.run_dir, "trace.jsonl"), encoding="utf-8") if '"broker"' in x]
        check("the run traces a broker record", any(r.get("type") == "broker" and r.get("current") == "mine:7b" for r in recs), recs)
        rep = open(os.path.join(e.run_dir, "report.md"), encoding="utf-8").read()
        check("report.md has the one broker line", rep.count("- Broker:") == 1, rep[-400:])
        check("the person's assigned model is never changed", e.model_for("generator") == before == "mine:7b"
              and e.cfg["roles"]["generator"] == "mine:7b", (before, e.model_for("generator")))
        import architect_hub as H
        econ = H.trace_economics(proj, os.path.basename(e.run_dir))
        check("the hub's trace endpoint carries the broker record", len(econ.get("broker") or []) == 1, econ.get("broker"))

        said.clear()
        ocfg = core.deep_merge(rcfg, {"behavior": {"broker": "off"}})
        e2 = core.Engine(proj, "build", "make a thing", ocfg, review="auto", say=lambda m, *_a, **_k: said.append(str(m)))
        r = F.broker_advice(e2, "build")
        t2 = os.path.join(e2.run_dir, "trace.jsonl")
        check("off: no line, no trace record", r is None and not any(s.startswith("Measured:") for s in said)
              and not (os.path.isfile(t2) and '"broker"' in open(t2, encoding="utf-8").read()))
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' BROKER-WIRED CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' BROKER-WIRED CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
