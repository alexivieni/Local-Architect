"""Self-test for batched best-of-N (report §4.5, §5.3; Eq. 11 and Eq. 21): on a server with N slots, the k candidates of a rescue round
(arch_flows.best_of_n) and of a YAML `sample_n` step are sent side by side, so k attempts cost about one request's time.

What it proves, against a scripted stand-in server that sleeps on every request (no real model, no GPU, no network):
  * with N slots, k candidates complete in about one request's time (wall clock, generous margins), more than one in flight at once;
  * every candidate is a full engine call: its own trace id and label (<label>-sample<i>), and each one not returned is marked;
  * budgets hold: a batch is sized to the calls left BEFORE it is sent, and the engine counts calls in flight against a call budget;
  * the first candidate that passes (in order) wins, the files are put back after each try, and applying the winner leaves its files;
  * a failure of the side-by-side path falls back to one request at a time and the round still finishes;
  * with one slot the old path is unchanged: same labels, same order, one request at a time, a check after each call, no marks.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SLEEP = 0.8                       # seconds per request; long enough that overlap is unmistakable on a busy laptop
ORIG = "def add(a, b):\n    raise NotImplementedError('write me')\n"
WRONG = "def add(a, b):\n    return a - b\n"
GOOD_BIG = "def add(a, b):\n    total = 0\n    total = total + a\n    total = total + b\n    return total\n"
GOOD_SMALL = "def add(a, b):\n    return a + b\n"
TESTS = ("import os, sys\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
         "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\ndef test_add_negative():\n    assert add(-1, 1) == 0\n")


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    print("\n== batched best-of-N: candidates side by side on a server with several slots ==")
    import arch_core as core

    home = tempfile.mkdtemp(prefix="archbon_")
    old_home = core.HOME
    keys = ("ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE", "ARCHITECT_EVAL", "OLLAMA_HOST")
    old_env = {k: os.environ.get(k) for k in keys}
    core.set_home(home)
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"          # a scripted model answers the same prompt differently; the cache must not collapse it
    os.environ.pop("ARCHITECT_EVAL", None)
    try:
        _checks(check, home)
    finally:
        core.set_home(old_home)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' BEST-OF-N CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' BEST-OF-N CHECKS FAILED'}")
    return n == len(results)


def _checks(check, home):
    from http.server import ThreadingHTTPServer

    import arch_core as core
    import arch_dataflow as df
    import arch_flows as fl
    import arch_roles
    import arch_selftest

    lock = threading.Lock()
    S = {"plan": {}, "log": [], "now": 0, "peak": 0, "fail_once": set()}

    class Slow(arch_selftest.Fake):
        state = {"calls": [], "n": {}}

        def do_POST(self):
            # an injected failure: the first request for a given attempt number answers 500 (the side-by-side request fails)
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            body = json.loads(raw or b"{}")
            user = (body.get("messages") or [{}])[-1].get("content", "")
            m = re.search(r"(?:attempt|candidate) (\d+) of", user)
            with lock:
                hit = m and int(m.group(1)) in S["fail_once"]
                if hit:
                    S["fail_once"].discard(int(m.group(1)))
            if hit:
                time.sleep(0.1)
                return self._send({"error": "slot busy (injected)"}, 500)
            import io
            self.rfile = io.BytesIO(raw)
            return super().do_POST()

        def script(self, role, user, full, st):
            m = re.search(r"(?:attempt|candidate) (\d+) of", user)
            i = int(m.group(1)) if m else 0
            with lock:
                S["now"] += 1
                S["peak"] = max(S["peak"], S["now"])
                t0 = time.time()
            time.sleep(SLEEP)
            with lock:
                S["now"] -= 1
                S["log"].append({"i": i, "t0": t0, "t1": time.time()})
            return "FILE: calc.py\n```python\n" + S["plan"].get(i, WRONG) + "```\n"

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv.server_address[1]}"
    os.makedirs(os.path.join(df.PROMPTS_DIR, "_selftest_bon"), exist_ok=True)
    with open(os.path.join(df.PROMPTS_DIR, "_selftest_bon", "bon.md"), "w", encoding="utf-8") as f:
        f.write("Make add(a, b) return the sum. Reply with a FILE: block for calc.py.\n")

    def project():
        proj = tempfile.mkdtemp(prefix="p_", dir=home)
        with open(os.path.join(proj, "calc.py"), "w", encoding="utf-8") as f:
            f.write(ORIG)
        os.makedirs(os.path.join(proj, "tests"))
        with open(os.path.join(proj, "tests", "test_calc.py"), "w", encoding="utf-8") as f:
            f.write(TESTS)
        with open(os.path.join(proj, "conftest.py"), "w", encoding="utf-8") as f:
            f.write("")
        return proj

    def engine(par, plan, fail_once=()):
        proj = project()
        cfg = core.deep_merge(core.load_config(proj), {"roles": {r: "fake:7b" for r in arch_roles.ROLES},
                                                       "server": {"parallel": par}, "behavior": {"battery_saver": "off"}})
        S.update(plan=dict(plan), log=[], now=0, peak=0, fail_once=set(fail_once))
        return core.Engine(proj, "build", "make add() work", cfg, review="auto", say=lambda *_a, **_k: None), proj

    def trace(e):
        return [json.loads(x) for x in open(os.path.join(e.run_dir, "trace.jsonl"), encoding="utf-8")]

    def calls(e):
        return [r for r in trace(e) if r.get("type") == "call"]

    def marks(e):
        return {r["id"]: r["accepted"] for r in trace(e) if r.get("type") == "mark"}

    def read(proj):
        return open(os.path.join(proj, "calc.py"), encoding="utf-8").read()

    def span():
        return max(x["t1"] for x in S["log"]) - min(x["t0"] for x in S["log"]) if S["log"] else 0.0

    try:
        # ---- 1. four candidates on four slots: about one request's time; the first that passes wins
        e, proj = engine(4, {1: WRONG, 2: GOOD_BIG, 3: GOOD_SMALL, 4: WRONG})
        check("the slots are read for the role (Settings > Server > Parallel when no llama-server serves it)", fl.bon_slots(e, "generator") == 4)
        r = fl.best_of_n(e, "generator", "Make add(a, b) return the sum.", "rescue", 4, ["calc.py"])
        sp = span()
        check(f"4 candidates on 4 slots finish in about one request's time ({round(sp, 2)} s; one request {SLEEP} s, one after another {4 * SLEEP} s)",
              len(S["log"]) == 4 and sp < 2 * SLEEP, (len(S["log"]), round(sp, 2)))
        check("...because they were in flight at the same time", S["peak"] >= 3, S["peak"])
        cs = calls(e)
        labels = sorted(c["label"] for c in cs)
        check("every candidate is a full engine call with its own trace id and its own label",
              labels == ["rescue-sample1", "rescue-sample2", "rescue-sample3", "rescue-sample4"] and len({c["id"] for c in cs}) == 4, labels)
        by_label = {c["label"]: c["id"] for c in cs}
        check("the first candidate that passes (in order) wins, not a later one", r and r["id"] == by_label["rescue-sample2"] and GOOD_BIG in r["text"],
              (r or {}).get("id"))
        check("the files are put back after the tries (the caller applies the winner)", read(proj) == ORIG, read(proj))
        mk = marks(e)
        check("each candidate not returned is marked: the failed one False, the ones never tried 'not sure' (None)",
              mk.get(by_label["rescue-sample1"]) is False and mk.get(by_label["rescue-sample3"]) is None
              and by_label["rescue-sample3"] in mk and by_label["rescue-sample4"] in mk and mk.get(by_label["rescue-sample4"]) is None
              and by_label["rescue-sample2"] not in mk, mk)
        res = e.apply(r["text"], "winner")
        check("applying the winner leaves exactly that candidate's files", read(proj) == GOOD_BIG and any(x["status"] == "applied" for x in res), read(proj))
        checks_traced = [x for x in trace(e) if x.get("type") == "check" and str(x.get("gate", "")).startswith("best of 4")]
        check("each tried candidate's check is traced, and trying stops at the first pass", len(checks_traced) == 2
              and [x["ok"] for x in checks_traced] == [False, True], [x.get("gate") for x in checks_traced])

        # ---- 2. budgets cap how many launch
        e, proj = engine(4, {2: GOOD_SMALL})
        e.push_budget("test step", calls=2)
        r = fl.best_of_n(e, "generator", "Make add(a, b) return the sum.", "cap", 4, ["calc.py"])
        check("with 2 calls left in the budget, only 2 of the 4 candidates are sent", len(S["log"]) == 2 and len(calls(e)) == 2 and S["peak"] == 2,
              (len(S["log"]), S["peak"]))
        check("...and the passing one among them still wins", r and GOOD_SMALL in r["text"])
        e, proj = engine(4, {})
        e.push_budget("test step", calls=3)
        raised = None
        try:
            fl.best_of_n(e, "generator", "Make add(a, b) return the sum.", "cap3", 4, ["calc.py"])
        except core.BudgetExceeded as ex:
            raised = ex
        check("3 calls left and nothing passes: 3 are sent side by side, then the budget stops the round as it always did",
              len(S["log"]) == 3 and S["peak"] == 3 and raised is not None, (len(S["log"]), S["peak"], raised))
        check("the files are untouched after a round the budget stopped", read(proj) == ORIG)
        # the engine itself: calls already in flight count against a call budget, so a batch checked all at once cannot overrun it
        e, proj = engine(4, {})
        e.push_budget("engine", calls=1)
        got = fl.bon_generate(e, [("generator", f"Q (attempt {j + 1} of 3)", {"label": f"raw{j + 1}"}) for j in range(3)], 3)
        ok_n = sum(1 for r_, ex in got if r_ is not None)
        over = sum(1 for r_, ex in got if isinstance(ex, core.BudgetExceeded))
        check("Engine.call from three threads with one call left in the budget: one is sent, two are refused before sending",
              ok_n == 1 and over == 2 and len(S["log"]) == 1, (ok_n, over, len(S["log"])))
        check("the run's counters stay consistent under concurrent calls", e.meter["calls"] == 1 and sum(e._inflight.values()) == 0,
              (e.meter, dict(e._inflight)))

        # ---- 3. failure of the concurrent path falls back to one at a time
        e, proj = engine(4, {1: WRONG, 2: GOOD_SMALL, 3: WRONG}, fail_once={2})
        r = fl.best_of_n(e, "generator", "Make add(a, b) return the sum.", "fb", 3, ["calc.py"])
        check("a candidate whose side-by-side request failed is asked again on its own, and the round still finds the passing one",
              r and GOOD_SMALL in r["text"] and sum(1 for x in S["log"] if x["i"] == 2) == 1, [x["i"] for x in S["log"]])
        real = fl.bon_generate

        def broken(*_a, **_k):
            raise RuntimeError("can't start new thread")
        fl.bon_generate = broken
        try:
            e, proj = engine(4, {1: WRONG, 2: WRONG, 3: GOOD_SMALL})
            r = fl.best_of_n(e, "generator", "Make add(a, b) return the sum.", "nothreads", 3, ["calc.py"])
        finally:
            fl.bon_generate = real
        check("when the side-by-side machinery itself fails, every candidate runs one after another and the winner is the same",
              r and GOOD_SMALL in r["text"] and S["peak"] == 1 and [x["i"] for x in S["log"]] == [1, 2, 3], (S["peak"], [x["i"] for x in S["log"]]))
        check("...each still traced under its own label", [c["label"] for c in calls(e)] == ["nothreads-sample1", "nothreads-sample2", "nothreads-sample3"],
              [c["label"] for c in calls(e)])

        # ---- 4. one slot: the old path, unchanged
        e, proj = engine(1, {1: WRONG, 2: WRONG, 3: GOOD_SMALL})
        check("one slot reads as one slot", fl.bon_slots(e, "generator") == 1)
        r = fl.best_of_n(e, "generator", "Make add(a, b) return the sum.", "seq", 3, ["calc.py"])
        rec = [x for x in trace(e) if x.get("type") in ("call", "mark") or (x.get("type") == "check" and str(x.get("gate", "")).startswith("best of"))]
        check("one slot: one request at a time, in order", S["peak"] == 1 and [x["i"] for x in S["log"]] == [1, 2, 3], (S["peak"], [x["i"] for x in S["log"]]))
        check("one slot: same labels and order as before, a check after each call, no marks added",
              [(x["type"], x.get("label")) for x in rec] == [("call", "seq-sample1"), ("check", None), ("call", "seq-sample2"), ("check", None),
                                                               ("call", "seq-sample3"), ("check", None)], [(x["type"], x.get("label")) for x in rec])
        cur = ORIG
        tail = ("\n\n(Rescue round 1, attempt 2 of 3. Strategy: First, in a short comment, trace the failing input through the code step by step "
                "to the wrong value; then fix exactly that. The test files are locked for this attempt (tests/test_calc.py): change the CODE so "
                "they pass.\nReply with exactly ONE block, `FILE: calc.py`, containing the complete corrected file. Its current content is:\n"
                f"```python\n{cur}\n```)")
        sent2 = [m["content"] for m in calls(e)[1]["messages"] if m["role"] == "user"][0]
        check("one slot: the prompt of each attempt is byte-for-byte the old one", sent2.startswith("Make add(a, b) return the sum." + tail), sent2[-200:])
        check("one slot: the winner is the first that passes", r and GOOD_SMALL in r["text"] and read(proj) == ORIG)

        # ---- 5. bon_slots reads a llama-server's own slot count
        import arch_server
        saved = (arch_server.serving, arch_server.load_state)
        e, proj = engine(4, {})
        try:
            arch_server.serving = lambda m, c=None: {"url": "http://127.0.0.1:1", "ctx": 8192, "tier": "worker"}
            arch_server.load_state = lambda: {"processes": {"worker": {"args": ["llama-server", "-c", "16384", "--parallel", "2"]}}}
            n2 = fl.bon_slots(e, "generator")
            arch_server.load_state = lambda: {"processes": {"worker": {"args": ["llama-server", "-c", "8192", "--parallel", "1"]}}}
            n1 = fl.bon_slots(e, "generator")
        finally:
            arch_server.serving, arch_server.load_state = saved
        check("a llama-server serving the role answers with the slots it was started with (--parallel 2)", n2 == 2, n2)
        check("...and one started with one slot keeps the old path even when the setting says 4", n1 == 1, n1)
        e.cfg["roles"]["generator"] = "claude:opus"
        check("a cloud model is never batched here", fl.bon_slots(e, "generator") == 1)

        # ---- 6. the YAML sample_n step: side by side, sized to its budget, and a broken batch falls back
        def flow(budget=None):
            st = {"id": "write", "kind": "sample_n", "n": 4, "role": "generator", "prompt": "bon", "verify": {"gate": "tests_pass"}}
            if budget:
                st["budget"] = budget
            return df.DataFlow({"id": "_selftest_bon", "title": "t", "steps": [st], "verdict": {"pass_if": "write.ok"}})
        e, proj = engine(4, {1: WRONG, 2: GOOD_BIG, 3: GOOD_SMALL, 4: WRONG})
        out = flow().run(e, {})
        check(f"sample_n on 4 slots: 4 answers in about one request's time ({round(span(), 2)} s), the smallest passing change kept",
              out["verdict"] == "pass" and span() < 2 * SLEEP and read(proj) == GOOD_SMALL and S["peak"] >= 3, (out["verdict"], round(span(), 2), S["peak"]))
        e, proj = engine(4, {2: GOOD_SMALL})
        out = flow({"calls": 2}).run(e, {})
        check("sample_n with a 2-call step budget sends 2 side by side and no more", len(S["log"]) == 2 and S["peak"] == 2 and out["verdict"] == "pass",
              (len(S["log"]), S["peak"], out["verdict"]))
        fl.bon_generate = broken
        try:
            e, proj = engine(4, {3: GOOD_SMALL})
            out = flow().run(e, {})
        finally:
            fl.bon_generate = real
        check("sample_n whose batch machinery fails falls back to one at a time and still passes",
              out["verdict"] == "pass" and S["peak"] == 1 and len(S["log"]) == 4 and read(proj) == GOOD_SMALL, (out["verdict"], S["peak"], len(S["log"])))
        e, proj = engine(1, {3: GOOD_SMALL})
        out = flow().run(e, {})
        check("sample_n on one slot: one after another, labels in order", S["peak"] == 1 and out["verdict"] == "pass"
              and [c["label"] for c in calls(e)] == ["write-cand1", "write-cand2", "write-cand3", "write-cand4"], [c["label"] for c in calls(e)])
    finally:
        srv.shutdown()
        shutil.rmtree(os.path.join(df.PROMPTS_DIR, "_selftest_bon"), ignore_errors=True)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
