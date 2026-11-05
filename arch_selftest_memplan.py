"""Self-test for arch_memplan: the planner's numbers follow Eq. 5/7/11/21 on a fixed fake machine.

    python arch_selftest_memplan.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({extra})" if not ok and extra else ""))

    home = tempfile.mkdtemp(prefix="memplan_")
    old = os.environ.get("ARCHITECT_DATA_DIR")
    os.environ["ARCHITECT_DATA_DIR"] = home
    try:
        os.makedirs(os.path.join(home, "passports"))
        arch = {"layers": 36, "kv_heads": 8, "head_dim": 128}
        for name, gb in (("small:4b", 2.33), ("big:8b", 4.68)):
            json.dump({"name": name, "size_gb": gb, "arch": arch}, open(os.path.join(home, "passports", name.replace(":", "_") + ".json"), "w"))
        json.dump({"name": "moe:35b", "size_gb": 13.5, "moe": True, "arch": arch}, open(os.path.join(home, "passports", "moe.json"), "w"))
        json.dump({"gpu_bw": 158.0, "ram_bw": 19.5}, open(os.path.join(home, "bandwidth.json"), "w"))
        import arch_memplan as M
        rows = M.plan(hw={"vram_gb": 6.0, "ram_gb": 32}, windows=(8192,))
        by = {(r["model"], r["slots"]): r for r in rows}
        check("mixture-of-experts models are left out (their experts live in RAM; batching is not the question)",
              not any(r["model"] == "moe:35b" for r in rows))
        s1, s4 = by.get(("small:4b", 1)), by.get(("small:4b", 4))
        check("a 4B fits whole with 1 and 4 slots on a 6 GB card, and 4 candidates cost more than one call but far less than 4",
              s1 and s4 and s1["full"] and s4["full"] and 1.0 < s4["n_candidates_cost"] < 2.0, (s1, s4))
        b1, b4 = by.get(("big:8b", 1)), by.get(("big:8b", 4))
        check("an 8B loses layers as slots grow (each slot pays for its own window) and gets no cheap batch",
              b1 and b4 and int(b4["layers"].split("/")[0]) < int(b1["layers"].split("/")[0]) and b4["n_candidates_cost"] is None, (b1, b4))
        check("the decode bound is the all-on-card one when every layer fits (158 / 2.33 = 67.8 tok/s)", abs(s1["tok_s_bound"] - 67.8) < 0.3, s1)
        check("fewer layers on the card mean a lower bound (Eq. 7)", b4["tok_s_bound"] < b1["tok_s_bound"], (b1, b4))
    finally:
        if old is None:
            os.environ.pop("ARCHITECT_DATA_DIR", None)
        else:
            os.environ["ARCHITECT_DATA_DIR"] = old
        shutil.rmtree(home, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' MEMPLAN CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' MEMPLAN CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
