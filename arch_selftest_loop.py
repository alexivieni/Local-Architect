"""Self-test for loop hardening: schema-constrained JSON, stall detection and targeted feedback, escalation to a larger owned model, resume after a
crash, and the mid-run GPU-spill check. Scripted fake Ollama, no real model."""
import json
import os
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
GB = 1024 ** 3


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_ci as ci
    import arch_core as core
    import arch_flows as fl
    import arch_progress as pg
    import arch_resume as rz
    import arch_roles as roles
    import arch_schemas as sc
    import arch_selftest as base

    # ---- schemas
    good_plan = {"sprints": [{"name": "a", "goal": "g", "acceptance": [{"id": "A1", "text": "t"}]}]}
    check("a valid plan passes its schema", sc.valid(good_plan, sc.PLAN))
    check("a plan with no acceptance criteria, an empty list or a wrong type is rejected",
          not sc.valid({"sprints": [{"name": "a", "goal": "g", "acceptance": []}]}, sc.PLAN) and not sc.valid({"sprints": []}, sc.PLAN) and not sc.valid({"sprints": "x"}, sc.PLAN))
    check("a judge verdict needs integer scores from 1 to 5 and a pass/fail verdict",
          sc.valid({"scores": {"c": {"score": 4}}, "verdict": "pass", "issues": []}, sc.JUDGE) and not sc.valid({"scores": {"c": {"score": 9}}, "verdict": "pass", "issues": []}, sc.JUDGE)
          and not sc.valid({"scores": {"c": {"score": 4.5}}, "verdict": "pass", "issues": []}, sc.JUDGE) and not sc.valid({"scores": {}, "verdict": "maybe", "issues": []}, sc.JUDGE))
    check("the router schema only admits real flow ids", sc.valid({"flow": "cicd", "why": "x"}, sc.router(fl.FLOWS)) and not sc.valid({"flow": "dance", "why": "x"}, sc.router(fl.FLOWS)))

    class Loop(base.Fake):
        formats, ps_pct, reject_schema, tags = [], 100, False, ["fake:7b"]
        bad_json = False

        def do_GET(self):
            if self.path == "/api/tags":
                return self._send({"models": [{"name": n, "digest": "x", "size": 4 * GB, "details": {"parameter_size": n.split(":")[1].upper()}} for n in self.tags]})
            if self.path == "/api/ps":
                return self._send({"models": [{"name": n, "size": 10 * GB, "size_vram": int(10 * GB * self.ps_pct / 100)} for n in self.tags]})
            return self._send({}, 404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            b = json.loads(raw or b"{}")
            Loop.formats.append(b.get("format"))
            if self.reject_schema and isinstance(b.get("format"), dict):
                return self._send({"error": "invalid format: schema not supported"}, 400)
            import io
            self.rfile = io.BytesIO(raw)
            self.headers.replace_header("Content-Length", str(len(raw))) if "Content-Length" in self.headers else None
            return super().do_POST()

        def script(self, role, user, full, st):
            if Loop.bad_json and role == "evaluator":
                return '{"verdict": "pass"}'
            return super().script(role, user, full, st)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Loop)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    old_env = {k: os.environ.get(k) for k in ("OLLAMA_HOST", "ARCHITECT_DATA_DIR")}
    os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv.server_address[1]}"
    home = tempfile.mkdtemp(prefix="archloop_")
    old_home = core.HOME
    core.set_home(home)
    os.environ.pop("ARCHITECT_DATA_DIR", None)
    tmp = tempfile.mkdtemp(prefix="archloopp_")
    quiet = lambda m: None

    def engine(proj, flow="build", goal="x", review="auto", persist=True):
        e = core.Engine(proj, flow, goal, core.load_config(proj), review=review, say=quiet, persist=persist)
        e.cfg["roles"] = {r: "fake:7b" for r in roles.ROLES}
        return e

    try:
        # ---- constrained JSON over the wire
        p0 = os.path.join(tmp, "p0")
        os.makedirs(p0)
        e = engine(p0, persist=False)
        r, j = e.ask_json("planner", "Plan the work as small SPRINTS now. login page", schema=sc.PLAN)
        check("the schema itself is sent to the server as the reply format", any(isinstance(f, dict) and f.get("required") == ["sprints"] for f in Loop.formats), Loop.formats[-3:])
        check("a plan that matches the schema comes back parsed", isinstance(j, dict) and j["sprints"][0]["name"] == "login page", j)
        Loop.bad_json = True
        r, j = e.ask_json("evaluator", "judge this", schema=sc.JUDGE)
        check("a reply that breaks the schema is None, never a half-valid object", j is None, j)
        Loop.bad_json = False
        Loop.reject_schema = True
        r, j = engine(p0, persist=False).ask_json("planner", "Plan the work as small SPRINTS now. login page", schema=sc.PLAN)
        check("an older server that rejects a schema is retried with plain JSON and still validated", isinstance(j, dict) and "json" in Loop.formats, (j, Loop.formats[-3:]))
        Loop.reject_schema = False

        # ---- the coach
        check("fingerprints ignore line numbers, durations, addresses and paths",
              pg.fp("FAILED tests/test_a.py::t - assert 3 == 4 (0.12s) at 0x7f1c") == pg.fp("FAILED tests/test_b.py::t - assert 5 == 6 (9.99s) at 0xabcd"))
        c = pg.Coach(engine(p0, persist=False), "generator")
        s1 = c.observe("fix A", "error one", ["a.py"])
        s2 = c.observe("fix B", "error one", ["a.py"])
        s3 = c.observe("fix B", "error two", ["b.py"])
        s4 = c.observe("fix C", "error one", [])
        check("it tells progress, same failure, repeated reply and cycling apart", (s1, s2, s3, s4) == ("progress", "same_failure", "repeat_reply", "cycling"), (s1, s2, s3, s4))
        c2 = pg.Coach(engine(p0, persist=False), "generator")
        c2.e.cfg["behavior"]["escalate"] = "off"
        txt = c2.after("FEEDBACK: x\n", "r1", "err", ["a.py"]) + c2.after("FEEDBACK: x\n", "r2", "err", ["b.py"])
        check("a stalled step gets a targeted hint naming the files already changed", "STALLED" in txt and "a.py" in txt and "identical failure" in txt, txt)
        check("a step that is making progress gets its feedback unchanged", pg.Coach(c2.e, "generator").after("FEEDBACK: x\n", "r1", "err1", []) == "FEEDBACK: x\n")
        check("a stalled step with nobody bigger to hand it to gets a FRESH attempt: a different route asked for, and a higher "
              "temperature for that role until the step ends", "FRESH ATTEMPT" in txt and (c2.e.retry_temp or {}).get("generator") == 0.8,
              (getattr(c2.e, "retry_temp", None), txt[-200:]))
        c2.close()
        check("the step's end gives the role its own temperature back", "generator" not in (c2.e.retry_temp or {}), c2.e.retry_temp)

        # ---- escalation ladder
        Loop.tags = ["fake:7b", "fake-big:30b", "fake-huge:70b", "untuned:32b", "nomic-embed-text:1b"]
        prof = {"models": {"fake:7b": {"fit_verdict": "fast"}, "fake-big:30b": {"fit_verdict": "fast"}, "fake-huge:70b": {"fit_verdict": "too_big"}, "nomic-embed-text:1b": {"fit_verdict": "fast"}}}
        os.makedirs(core.data_dir(), exist_ok=True)
        json.dump(prof, open(os.path.join(core.data_dir(), "profile.json"), "w"))
        e = engine(p0, persist=False)
        check("the next model is the smallest TUNED one that fits and is clearly bigger (not too big, not untuned, not an embedder)", pg.next_model(e, "fake:7b") == "fake-big:30b", pg.next_model(e, "fake:7b"))
        check("a model that is already the biggest tuned one has nowhere to go", pg.next_model(e, "fake-big:30b") is None)
        c = pg.Coach(e, "generator")
        c.after("F\n", "same", "boom", [])
        c.after("F\n", "same", "boom", [])
        check("two identical attempts move the role to the larger model, and model_for says so", e.pins.get("generator") == "fake-big:30b" and e.model_for("generator") == "fake-big:30b" and e.model_for("planner") == "fake:7b")
        c.close()
        check("when the step ends the role goes back to its own model", e.model_for("generator") == "fake:7b" and not e.pins)
        e.cfg["behavior"]["escalate"] = "off"
        c = pg.Coach(e, "generator")
        c.after("F\n", "same", "boom", [])
        c.after("F\n", "same", "boom", [])
        check("behavior.escalate=off never changes the model", not e.pins)

        # ---- escalation inside a real flow (cicd fix loop): the small model repeats a bad fix, the larger one fixes it
        bad = "name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - run: python -m unittest discover -s tests -q || true\n"
        good = bad.replace(" || true", "").replace("on: [push]", "on: [push]\npermissions:\n  contents: read")
        seen = []

        class Esc(Loop):
            def script(self, role, user, full, st):
                if role == "ci_engineer" and "BLOCKING FINDINGS" in user:
                    seen.append(st["calls"][-1]["model"])
                    wf = good if st["calls"][-1]["model"] == "fake-big:30b" else bad.replace("name: CI", "name: Build")
                    return "FILE: .github/workflows/ci.yml\n```yaml\n" + wf + "```\n"
                return super().script(role, user, full, st)

        srv2 = ThreadingHTTPServer(("127.0.0.1", 0), Esc)
        threading.Thread(target=srv2.serve_forever, daemon=True).start()
        os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv2.server_address[1]}"
        fp = os.path.join(tmp, "ci")
        os.makedirs(os.path.join(fp, ".github", "workflows"))
        os.makedirs(os.path.join(fp, "tests"))
        open(os.path.join(fp, ".github", "workflows", "ci.yml"), "w").write(bad)
        open(os.path.join(fp, "tests", "test_a.py"), "w").write("import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(1, 1)\n")
        e = engine(fp, "cicd", "Fix the CI pipeline")
        e.cfg["limits"]["max_revisions"] = 4
        out = fl.run_flow(e, "cicd", {})
        rep = open(os.path.join(e.run_dir, "report.md"), encoding="utf-8").read()
        check("cicd: the small model repeats itself, the role is escalated, the larger model fixes it", seen[:2] == ["fake:7b", "fake:7b"] and seen[-1] == "fake-big:30b" and out["ok"], (seen, out))
        check("the escalation is written in the run report and the role returns to its own model", "Escalated `ci_engineer`" in rep and not e.pins, rep[-400:])
        srv2.shutdown()
        os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv.server_address[1]}"
        Loop.tags = ["fake:7b"]

        # ---- run state and resume
        sp = os.path.join(tmp, "rz")
        os.makedirs(sp)
        e = engine(sp, "build", "make a slug and a login page")
        st = core.load_json(os.path.join(e.run_dir, "run.json"))
        check("every run records its state, pid and goal on disk from the start", st["status"] == "running" and st["pid"] == os.getpid() and st["goal"] == "make a slug and a login page", st)
        check("a running run with a live process is not offered for resume", rz.resumable(sp) is None)
        d = os.path.join(sp, ".arch", "runs", "20200101_000000_build")
        os.makedirs(d)
        json.dump({"run_id": "20200101_000000_build", "flow": "build", "goal": "make a slug and a login page", "status": "running", "pid": 2 ** 22 + 7, "review": "auto", "params": {"domains": ["api"]},
                   "checkpoints": {"plan": [{"name": "slugify", "kind": "backend", "goal": "slugify function", "files": ["slugify.py"], "acceptance": [{"id": "A1", "text": "x"}]},
                                            {"name": "login page", "kind": "ui", "goal": "login page", "files": ["index.html"], "acceptance": [{"id": "A1", "text": "has labelled email and password fields"}]}],
                                   "sprints_done": ["slugify"], "files": ["slugify.py"]}}, open(os.path.join(d, "run.json"), "w"))
        found = rz.resumable(sp)
        check("a run whose process is gone is reported as crashed and offered for resume", found and found["status"] == "crashed" and found["run_id"] == "20200101_000000_build", found)
        params = rz.resume_params(found)
        Loop.state = {"calls": [], "n": {}}
        e2 = engine(sp, "build", found["goal"])
        e2.cfg["limits"]["max_revisions"] = 2
        out = fl.run_flow(e2, "build", params)
        n = Loop.state["n"]
        check("resume: the saved plan is used (the planner is not asked again) and only the unfinished sprint is built",
              not n.get("planner") and (n.get("generator_ui") or 0) >= 1 and not n.get("generator") and os.path.isfile(os.path.join(sp, "index.html")), (n, out))
        saved = core.load_json(os.path.join(e2.run_dir, "run.json"))
        check("the resumed run records which sprints passed and finishes as done", out["ok"] and saved["status"] == "done" and "login page" in saved["checkpoints"]["sprints_done"], saved)
        rz.mark_resumed(found, e2.run_id)
        check("a run that was resumed is not offered twice", rz.resumable(sp) is None)

        # ---- GPU spill check
        Loop.ps_pct = 55
        e = engine(os.path.join(tmp, "p0"), persist=False)
        e.call("planner", "hello")
        check("a model that is only partly on the GPU mid-run is flagged with the window and the fix", any("[gpu]" in m and "55%" in m for m in e.ctx_events), e.ctx_events)
        n_before = len(e.ctx_events)
        e.call("planner", "hello again")
        check("the check runs once per model and window, not on every call", len(e.ctx_events) == n_before)
        Loop.ps_pct = 100
        e = engine(os.path.join(tmp, "p0"), persist=False)
        e.call("planner", "hello")
        check("a model fully on the GPU produces no note", not [m for m in e.ctx_events if "[gpu]" in m])
        Loop.ps_pct = 0
        os.remove(os.path.join(core.data_dir(), "profile.json"))
        e = engine(os.path.join(tmp, "p0"), persist=False)
        e.call("planner", "hello")
        check("on a machine tune never saw a GPU in, 0% GPU is normal and is not flagged", not [m for m in e.ctx_events if "[gpu]" in m])

        # ---- edit safety
        sp2 = os.path.join(tmp, "safe")
        os.makedirs(sp2)
        big = "".join(f"line_{i} = {i}\n" for i in range(40))
        open(os.path.join(sp2, "big.py"), "w").write(big)
        open(os.path.join(sp2, "small.py"), "w").write("a = 1\nb = 2\n")
        e = engine(sp2, "build")
        r1 = e.apply("FILE: big.py\n```python\nline_0 = 0\nline_1 = 1\n```\n")
        check("a reply that would cut a 40-line file to 2 lines is refused, with the fix named", r1[0]["status"] == "error" and "cut big.py from 40 to 2 lines" in r1[0]["msg"] and "EDIT" in r1[0]["msg"], r1)
        check("...and the file on disk is untouched", open(os.path.join(sp2, "big.py")).read() == big)
        r2 = e.apply("EDIT: big.py\n<<<<<<< SEARCH\nline_3 = 3\n=======\nline_3 = 33\n>>>>>>> REPLACE\n")
        check("a normal edit to the same file is applied", r2[0]["status"] == "applied" and "line_3 = 33" in open(os.path.join(sp2, "big.py")).read(), r2)
        check("small files are not subject to the guard", e.apply("FILE: small.py\n```python\nz = 0\n```\n")[0]["status"] == "applied")
        e.cfg["behavior"]["max_shrink_pct"] = 0
        check("behavior.max_shrink_pct = 0 turns the guard off", e.apply("FILE: big.py\n```python\nonly = 1\n```\n")[0]["status"] == "applied")
        open(os.path.join(sp2, "big.py"), "w").write(big)
        er = engine(sp2, "refactor")
        check("the refactor flow may legitimately shrink a file", er.apply("FILE: big.py\n```python\nline_0 = 0\n```\n")[0]["status"] == "applied")
        e3 = engine(sp2, "build")
        q = e3.apply("I could change that. Would you like me to proceed?")
        check("a reply that asks a question instead of acting is told to act", q[0]["status"] == "error" and "asks a question instead of acting" in q[0]["msg"], q)
        check("a reply with no blocks and no question just says no blocks were found", "asks a question" not in e3.apply("Here is my plan: change it.")[0]["msg"])
        open(os.path.join(sp2, "same.py"), "w").write("x = 1\n")
        e3.apply("FILE: same.py\n```python\nx = 1\n```\n")
        check("a reply whose edits change nothing on disk is recognised as a no-op", e3.last_apply_noop is True)
        cc = pg.Coach(e3, "generator")
        check("...and the coach tells the model its reply changed nothing", "changed nothing on disk" in cc.after("F\n", "r", "boom", []))
        e3.apply("FILE: same.py\n```python\nx = 2\n```\n")
        check("a real change clears the no-op flag", e3.last_apply_noop is False)
        check("the role prompts tell models to act, not ask, and not to repeat a failed action", "do not ask for permission" in roles.COMMON and "Never repeat an action that just failed" in roles.COMMON)

        # ---- track record from your own runs
        import arch_ledger as led
        keep_dd = os.environ.get("ARCHITECT_DATA_DIR")
        os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "led_data")     # a clean history for this check
        lp = os.path.join(tmp, "led")
        os.makedirs(lp)
        e = engine(lp, "build")
        a = e.call("generator", "one")
        b = e.call("generator", "two")
        c3 = e.call("planner", "three")
        e.mark(a["id"], True)
        e.mark(b["id"], False, "tests failed")
        e.mark(b["id"], True)                    # the last label wins: the retry was accepted
        e.mark(c3["id"], False)
        e.finish(True)
        sm = led.summary("fake:7b")
        check("each marked call is counted once per model and role, the last mark wins", sm and sm["n"] == 3 and sm["ok"] == 2 and sm["by_role"]["generator"] == {"n": 2, "ok": 2, "rate": 100}
              and sm["by_role"]["planner"]["ok"] == 0, sm)
        e.finish(True)
        check("finishing a run twice does not double count", led.summary("fake:7b")["n"] == 3)
        check("a model with no marked calls has no record (not a made-up zero)", led.summary("never-run:7b") is None)
        ov = led.roles_overview()
        gen = next(r for r in ov if r["role"] == "generator")
        check("the run history lists each role with calls, accepted rate, median and p95 seconds and seconds per accepted reply", gen["n"] == 2 and gen["rate"] == 100 and gen["p50"] is not None and gen["p95"] is not None and gen["cost_per_success"] is not None, gen)
        ex = led.example_for("generator", "one")
        check("an accepted reply is remembered and found again for a similar request, as a format reference", ex is not None and ex["role"] == "generator" and ex["reply"], ex)
        check("a request with nothing in common gets no example (no random few-shot)", led.example_for("generator", "completely different zebra quantum topic") is None)
        check("a rejected reply is never offered as an example", all(e["role"] != "planner" for e in led.load().get("_examples", [])))
        e4 = engine(lp, "build")
        captured = []
        _orig_sc = core.stream_chat
        core.stream_chat = lambda model, msgs, *a, **k: (captured.append((msgs[0]["content"], msgs[-1]["content"])), _orig_sc(model, msgs, *a, **k))[1]
        try:
            e4.call("generator", "one")
            e4.cfg["behavior"]["learn_from_runs"] = "off"
            e4.call("generator", "one more")
        finally:
            core.stream_chat = _orig_sc
        check("with learn_from_runs on, the role sees its own past accepted reply as a format reference at the END of the user turn; off, it does not",
              "FORMAT REFERENCE" in captured[0][1] and captured[0][1].rstrip().endswith(led.example_for("generator", "one")["reply"].rstrip()) and "FORMAT REFERENCE" not in captured[1][1], [c[1][-200:] for c in captured])
        check("the system prompt is byte-identical across those calls, so the server can reuse the cached prefill", captured[0][0] == captured[1][0])

        # ---- retrieval: the relevant functions of a file, not just its signatures
        rp = os.path.join(tmp, "retr")
        os.makedirs(rp)
        open(os.path.join(rp, "inventory.py"), "w").write("import os\n\ndef add_item(items, name, qty):\n    items[name] = qty\n    return items\n\n\ndef remove_item(items, name):\n    del items[name]\n\n\ndef low_stock(items, threshold):\n    return [n for n, q in items.items() if q < threshold]\n\n\ndef unrelated():\n    return 42\n")
        fo = core.focus(os.path.join(rp, "inventory.py"), "change low_stock so the threshold is inclusive")
        check("focus() returns the matching function in full and the others as signatures", "def low_stock" in fo and "q < threshold" in fo and "def unrelated" in fo and "return 42" not in fo, fo)
        check("focus() returns nothing when nothing matches, and nothing for a file that does not parse", core.focus(os.path.join(rp, "inventory.py"), "zebra quantum") == "" and core.focus(os.path.join(rp, "nope.py"), "x") == "")
        big = "".join(f"def fn_{i}(x):\n    return x + {i}\n\n\n" for i in range(300)) + "def target_fn(y):\n    return y * 2\n"
        for i in range(6):
            open(os.path.join(rp, f"filler{i}.py"), "w").write(big)
        open(os.path.join(rp, "small.py"), "w").write("def target_fn_helper():\n    return 1\n")
        er = engine(rp, "build", "fix target_fn so it doubles correctly")
        er.cfg["limits"]["context_chars"] = 9000
        ctx = er.context("fix target_fn so it doubles correctly")
        check("in a tight window a big file that cannot be shown whole still contributes the relevant function instead of only signatures", "the parts relevant to this request" in ctx and "def target_fn(y)" in ctx, ctx[:400])
        if keep_dd is None:
            os.environ.pop("ARCHITECT_DATA_DIR", None)
        else:
            os.environ["ARCHITECT_DATA_DIR"] = keep_dd

        # ---- smoke test: a tiny real build through the harness
        import arch_ready as rd
        steps, summ = [], {"needs": [], "steps": []}
        stp = lambda name, ok, detail="", advisory=False: steps.append((name, ok, detail))
        rd._smoke({"builder": "fake:7b"}, summ, stp, lambda m: None)
        check("the smoke test builds a tiny function with tests through the whole harness and reports time and calls", steps and steps[-1][0] == "smoke test" and steps[-1][1] and "model calls" in steps[-1][2], steps)
        os.environ["OLLAMA_HOST"] = "127.0.0.1:1"
        steps.clear()
        rd._smoke({"builder": "fake:7b"}, summ, stp, lambda m: None)
        check("with no model answering, the smoke test fails plainly instead of crashing", steps and steps[-1][1] is False, steps)
        os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv.server_address[1]}"
    finally:
        core.set_home(old_home)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        srv.shutdown()
    bad = results.count(False)
    print(f"\n  {'ALL ' + str(len(results)) + ' LOOP-HARDENING CHECKS PASSED' if not bad else str(bad) + ' of ' + str(len(results)) + ' CHECKS FAILED'}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(run())
