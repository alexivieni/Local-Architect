"""Self-tests for arch_costs.py (spend summary from run traces) and arch_batch.py (half-price overnight batches).
A fake SDK client stands in for Anthropic: no key, no network. Runs against a throwaway data folder.
    python arch_selftest_costs.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

import arch_batch
import arch_costs


def _run(root, name, calls, verdict, age_days=0):
    d = os.path.join(root, ".arch", "runs", name)
    os.makedirs(d, exist_ok=True)
    t = time.time() - age_days * 86400
    p = os.path.join(d, "trace.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        for i, c in enumerate(calls):
            f.write(json.dumps({"type": "call", "id": i + 1, "t": t, **c}) + "\n")
        f.write("not json\n")
        f.write(json.dumps({"type": "verdict", "t": t, "verdict": verdict}) + "\n")
    os.utime(p, (t, t))


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeBatches:
    def __init__(self):
        self.created, self.status = None, "in_progress"

    def create(self, requests):
        self.created = requests
        return _Obj(id="msgbatch_test1")

    def retrieve(self, bid):
        return _Obj(id=bid, processing_status=self.status)

    def results(self, bid):
        ok = lambda cid, t: _Obj(custom_id=cid, result=_Obj(type="succeeded", message=_Obj(
            content=[_Obj(type="text", text=t)], usage=_Obj(input_tokens=1_000_000, output_tokens=100_000))))
        # reversed: results arrive in any order
        yield _Obj(custom_id="b", result=_Obj(type="errored"))
        yield ok("a", "answer A")


class _FakeClient:
    def __init__(self):
        self.messages = _Obj(batches=_FakeBatches())


def run():
    results = []

    def check(name, ok):
        results.append(bool(ok))
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")

    tmp = tempfile.mkdtemp(prefix="arch_costs_")
    old = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_CLOUD_TEST")}
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["ARCHITECT_CLOUD_TEST"] = "1"
    try:
        root = os.path.join(tmp, "proj")
        _run(root, "r1", [{"model": "claude-sonnet-5-5", "prompt_tokens": 1000, "gen_tokens": 200, "usd": 0.25, "transport": "claude"},
                          {"model": "qwen:7b", "prompt_tokens": 500, "gen_tokens": 100, "usd": None, "transport": "llama"},
                          {"model": "qwen:7b", "prompt_tokens": 500, "gen_tokens": 100, "transport": "ollama"}], "pass")
        _run(root, "r2", [{"model": "qwen:7b", "prompt_tokens": 400, "gen_tokens": 50, "transport": "ollama"}], "fail")
        _run(root, "old", [{"model": "claude-sonnet-5-5", "usd": 9.0, "transport": "claude"}], "pass", age_days=30)
        s = arch_costs.summary(days=7, roots=[root])
        check("two recent runs counted, the old one left out", s["runs"] == 2)
        check("one passed", s["passed"] == 1)
        check("cost is the claude call", abs(s["cost_usd"] - 0.25) < 1e-9)
        check("per_task_usd = cost / passed", abs(s["per_task_usd"] - 0.25) < 1e-9)
        check("local_share = 3 of 4 calls", abs(s["local_share"] - 0.75) < 1e-9)
        bm = {r["model"]: r for r in s["by_model"]}
        check("by_model tokens", bm["qwen:7b"]["calls"] == 3 and bm["qwen:7b"]["tokens_in"] == 1400 and bm["qwen:7b"]["tokens_out"] == 250)
        check("by_model usd, costliest first", s["by_model"][0]["model"] == "claude-sonnet-5-5" and s["by_model"][0]["usd"] == 0.25)
        check("by_day has today", len(s["by_day"]) == 1 and s["by_day"][0]["runs"] == 2 and s["by_day"][0]["passed"] == 1)
        check("wider window sees the old run", arch_costs.summary(days=60, roots=[root])["runs"] == 3)
        e = arch_costs.summary(days=7, roots=[os.path.join(tmp, "nowhere")])
        check("no runs: zeros, no crash", e["runs"] == 0 and e["per_task_usd"] == 0.0 and e["by_model"] == [])

        cfg = {"online": {"enabled": True, "cloud_tier": "all"}}
        cl = _FakeClient()
        items = [{"id": "a", "system": "be brief", "prompt": "hi", "model": "claude-sonnet-5-5"}, {"id": "b", "prompt": "yo"}]
        bid = arch_batch.submit(items, client=cl, cfg=cfg)
        sent = cl.messages.batches.created
        check("submit returns the batch id", bid == "msgbatch_test1")
        check("requests carry custom_id, model, system", sent[0]["custom_id"] == "a" and sent[0]["params"]["model"] == "claude-sonnet-5-5"
              and sent[0]["params"]["system"] == "be brief" and "system" not in sent[1]["params"])
        check("batch stored with its items", len(arch_batch._load()[bid]["items"]) == 2)
        p = arch_batch.poll(bid, client=cl, cfg=cfg)
        check("poll while running: no results", p["status"] == "in_progress" and p["results"] == {})
        cl.messages.batches.status = "ended"
        p = arch_batch.poll(bid, client=cl, cfg=cfg)
        check("results keyed by id, any order", p["status"] == "ended" and p["results"]["a"]["text"] == "answer A" and not p["results"]["b"]["ok"])
        check("batch price is half", abs(p["results"]["a"]["usd"] - 0.5 * (2.0 + 1.0)) < 1e-6)
        p2 = arch_batch.poll(bid, client=object(), cfg=cfg)
        check("an ended batch is answered from the stored copy", p2["results"]["a"]["text"] == "answer A")
        try:
            arch_batch.submit(items, client=cl, cfg={"online": {"enabled": False}})
            refused = False
        except RuntimeError:
            refused = True
        check("refused when Claude is not allowed", refused)
        try:
            arch_batch.submit([{"id": "bad id!", "prompt": "x"}], client=cl, cfg=cfg)
            bad = False
        except ValueError:
            bad = True
        check("a bad item id is refused", bad)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' COSTS CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' COSTS CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
