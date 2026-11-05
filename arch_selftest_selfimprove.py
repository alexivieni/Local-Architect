"""Self-test for the iterative self-improve loop (arch_selfimprove.loop + workflows/self_improve.yaml).

The scripted fake model only (arch_squad.start_fake: an Ollama-shaped server on 127.0.0.1, random port); no real model, no model
server, no Claude. The "install" is a small fixture tree in a temp folder, never this one; home, data dir and telemetry are isolated;
ARCHITECT_NO_CACHE=1. The expensive gates (suite runs, full self-test, eval) use scripted runners, so the checks are about the LOOP:
copy, plan check, ownership, gate statuses, feedback, promotion, rollback, journal and telemetry.

    python arch_selftest_selfimprove.py
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
N = [0, 0]


def ok(cond, what, detail=None):
    N[0 if cond else 1] += 1
    print(("[ok] " if cond else "[x] ") + what + ("" if cond or detail is None else f"  -- {str(detail)[:300]}"))


DEMO_V1 = "def double(x):\n    return x * 2\n"
DEMO_STUB = "def double(x):\n    return x * 2\n\n\ndef triple(x):\n    pass\n"
DEMO_GOOD = "def double(x):\n    return x * 2\n\n\ndef triple(x):\n    return x * 3\n"
SUITE = "import arch_demo\n\n\ndef run():\n    return arch_demo.double(2) == 4\n"


def tree_hash(root):
    h = hashlib.sha256()
    for dp, dn, fs in sorted(os.walk(root)):
        dn.sort()
        for f in sorted(fs):
            p = os.path.join(dp, f)
            h.update(os.path.relpath(p, root).encode())
            with open(p, "rb") as fh:
                h.update(fh.read())
    return h.hexdigest()


def make_base(tmp):
    base = os.path.join(tmp, "install")
    os.makedirs(os.path.join(base, "docs"))
    files = {"arch_demo.py": DEMO_V1, "arch_selftest_demo.py": SUITE, "docs/notes.md": "# notes\n",
             "arch_selfimprove.py": "# stands in for the gate module\nX = 1\n"}
    for rel, text in files.items():
        with open(os.path.join(base, rel), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    return base


def script_of(rounds):
    """rounds: {n: (owns, {rel: text})}; a round not listed repeats the last one."""
    def s(n, feedback, goal):
        owns, edits = rounds.get(n) or rounds[max(rounds)]
        s.feedback.append(feedback)
        return {"builders": [{"owns": owns, "task": "scripted"}], "summary": "scripted", "edits": edits}
    s.feedback = []
    return s


def runners(log, suite_status="pass"):
    return {"suite": lambda root, name: (log.append(("suite", root, name)) or (suite_status, f"{name}: scripted {suite_status}")),
            "selftest": lambda root, suite: (log.append(("selftest", root)) or (True, 40, "ALL 40 CHECKS PASSED")),
            "baseline": lambda: 40,
            "eval": lambda b, c: (log.append(("eval", c)) or {"ok": True, "regressions": []})}


def run():
    import arch_core as core
    tmp = tempfile.mkdtemp(prefix="arch_si_loop_")
    keep_env = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE", "OLLAMA_HOST", "ARCHITECT_TELEMETRY")}
    old_home = core.HOME
    try:
        os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
        os.environ["ARCHITECT_NO_CACHE"] = "1"
        os.environ.pop("ARCHITECT_TELEMETRY", None)
        core.set_home(os.path.join(tmp, "home"))
        import arch_selfimprove as si
        import arch_squad as sq
        import arch_telemetry as tel
        import arch_orchestration as orch
        tel.configure(enabled=True)
        dd = os.path.join(tmp, "data")
        quiet = lambda *a, **k: None
        base = make_base(tmp)

        # ---- the workflow and its agent validate (so `crew --workflows` and the squad suite accept them)
        w = sq.load_workflow(si.WORKFLOW)
        ok([p["title"] for p in w["phases"]] == ["Audit", "Plan", "Build", "Verify"] and len(w["phases"][0]["parallel"]) == 3,
           "self_improve.yaml: 3 parallel auditors -> plan -> build -> verify", [p["title"] for p in w["phases"]])
        ok(not sq.validate_workflow(w) and "problems" not in sq.list_agents().get("si-planner", {"problems": 1}),
           "the workflow and the si-planner agent validate", sq.validate_workflow(w))

        # ---- the plan is checked in code
        cand0 = si.make_candidate(base)
        good = si.check_plan({"builders": [{"owns": ["arch_demo.py"], "task": "t"}, {"owns": ["docs/new.md"], "task": "u"}]}, cand0)
        ok(good[0] and len(good[2]) == 2, "a plan with real paths, one owner each, is accepted", good[1])
        clash = si.check_plan({"builders": [{"owns": ["arch_demo.py"]}, {"owns": ["arch_demo.py"]}]}, cand0)
        ok(not clash[0] and any("owned by builders" in p for p in clash[1]), "two builders owning one file is refused", clash[1])
        lost = si.check_plan({"builders": [{"owns": ["nope/missing.py", "../outside.py", "C:/x.py"]}]}, cand0)
        ok(not lost[0] and len(lost[1]) == 3, "missing, escaping and absolute paths are refused", lost[1])
        gate_p = si.check_plan({"builders": [{"owns": ["arch_selfimprove.py"]}]}, cand0)
        ok(not gate_p[0] and "gates" in gate_p[1][0] and si.check_plan({"builders": [{"owns": ["arch_selfimprove.py"]}]}, cand0, True)[0],
           "a plan that touches the gates is refused unless --allow-gate-edits", gate_p[1])
        ok(not si.check_plan({"builders": []}, cand0)[0] and not si.check_plan("fake: done", cand0)[0], "an empty or non-plan is refused")
        shutil.rmtree(os.path.dirname(cand0), ignore_errors=True)

        # ---- the gates, one at a time, on a copy
        log = []
        cand = si.make_candidate(base)
        with open(os.path.join(cand, "arch_selfimprove.py"), "a", encoding="utf-8") as f:
            f.write("Y = 2\n")
        g = si.round_gates(base, cand, ["arch_selfimprove.py"], runners=runners(log), say=quiet)
        ran_expensive = any(e[0] in ("selftest", "eval") for e in log)
        st = {x["name"].split(" (")[0]: x["status"] for x in g["gates"]}
        ok(not g["ok"] and st["the gates themselves are untouched"] == "fail", "an edit to the gates fails without the flag", st)
        g2 = si.round_gates(base, cand, ["arch_selfimprove.py"], allow_gate_edits=True, runners=runners(log), say=quiet)
        ok(next(x for x in g2["gates"] if x["name"].startswith("the gates"))["status"] == "pass", "...and passes that gate with --allow-gate-edits")
        ok(any(x["status"] == "could-not-run" for x in g["gates"]) and not ran_expensive,
           "expensive gates are could-not-run after a cheap gate failed", [x["status"] for x in g["gates"]])
        shutil.rmtree(os.path.dirname(cand), ignore_errors=True)
        cand = si.make_candidate(base)
        with open(os.path.join(cand, "arch_demo.py"), "w", encoding="utf-8") as f:
            f.write(DEMO_V1 + "\n\ndef fetch(u):\n    import urllib.request\n    return urllib.request.urlopen(u).read()\n")
        with open(os.path.join(cand, "docs", "notes.md"), "a", encoding="utf-8") as f:
            f.write("stray\n")
        g = si.round_gates(base, cand, ["arch_demo.py"], runners=runners(log), say=quiet)
        st = {x["name"].split(" (")[0]: x["status"] for x in g["gates"]}
        ok(st["no new network calls on the default path"] == "fail", "a new network call fails its gate", st)
        ok(st["only files the plan gave a builder were changed"] == "fail", "a change outside the owned files fails", st)
        shutil.rmtree(os.path.dirname(cand), ignore_errors=True)
        cand = si.make_candidate(base)
        with open(os.path.join(cand, "arch_demo.py"), "w", encoding="utf-8") as f:
            f.write(DEMO_GOOD)
        log.clear()
        g = si.round_gates(base, cand, ["arch_demo.py"], runners=runners(log, "fail"), say=quiet)
        aff = next(x for x in g["gates"] if x["name"].startswith("the selftest suites"))
        ok(aff["status"] == "fail" and "arch_selftest_demo" in aff["name"] and ("suite", cand, "arch_selftest_demo") in log,
           "the suite that imports the changed module runs in the copy, and its failure fails the gate", aff)
        ok(si.affected_suites(cand, ["arch_demo.py"]) == ["arch_selftest_demo"], "affected suites: found by import")
        g = si.round_gates(base, cand, ["arch_demo.py"], runners=runners(log), say=quiet)
        ok(g["ok"] and all(x["status"] == "pass" for x in g["gates"]) and len(g["gates"]) == 10, "a clean change passes all 10 gates",
           [(x["name"], x["status"]) for x in g["gates"]])
        g = si.round_gates(base, cand, ["arch_demo.py"], full=False, runners={"suite": runners(log)["suite"]}, say=quiet)
        ok(not g["ok"] and sum(x["status"] == "could-not-run" for x in g["gates"]) == 3,
           "without a full self-test runner (--fake) the last 3 gates are could-not-run, so it cannot be promoted")
        shutil.rmtree(os.path.dirname(cand), ignore_errors=True)

        # ---- the loop: round 1 is blocked by a gate, round 2 fixes it, promotion is atomic, rollback restores bytes
        before = tree_hash(base)
        demo_bytes = open(os.path.join(base, "arch_demo.py"), "rb").read()
        sc = script_of({1: (["arch_demo.py"], {"arch_demo.py": DEMO_STUB}), 2: (["arch_demo.py"], {"arch_demo.py": DEMO_GOOD})})
        log.clear()
        asked = []
        s = si.loop("add triple() next to double()", rounds=3, fake=True, base=base, script=sc, runners=runners(log),
                    confirm=lambda sess: asked.append(sess["id"]) or False, say=quiet, data_dir=dd)
        r1, r2 = (s["rounds"] + [{}, {}])[:2]
        g1 = {x["name"].split(" (")[0]: x["status"] for x in r1.get("gates", [])}
        ok(len(s["rounds"]) == 2 and not r1.get("ok") and g1.get("no new fake code") == "fail",
           "round 1: the stub is stopped by the fake-code gate", g1)
        ok("fake code" in r1.get("feedback", "") and len(sc.feedback) == 2 and "fake code" in sc.feedback[1],
           "round 1's failure is fed into round 2", sc.feedback)
        ok(r2.get("ok") and s["status"] == "passed" and asked == [s["id"]], "round 2 fixes it, every gate passes, and the person is asked",
           (s["status"], [(x["name"], x["status"]) for x in r2.get("gates", [])]))
        ok("+    return x * 3" in r2.get("diff", "") and r2.get("plan_check", {}).get("ok"), "the round keeps the diff and the plan check")
        ok(tree_hash(base) == before and not os.path.exists(os.path.join(HERE, "arch_demo.py")),
           "nothing touched the install (or this tree) during the rounds")
        ok(all(e[1] == s["candidate"] for e in log if e[0] in ("suite", "selftest")) and s["candidate"].startswith(core.HOME),
           "every gate ran inside the throwaway copy")
        try:
            si.promote_session(s["id"], confirm=False, say=quiet)
            ok(False, "promotion without a yes is refused")
        except PermissionError:
            ok(True, "promotion without a yes is refused")
        p = si.promote_session(s["id"], confirm=True, say=quiet)
        ok(p["status"] == "promoted" and open(os.path.join(base, "arch_demo.py"), encoding="utf-8").read() == DEMO_GOOD
           and os.path.isfile(p["backup"]) and not [f for f in os.listdir(base) if f.endswith(".arch_new")],
           "promotion: backup zip + atomic copy, no half-written files", p.get("backup"))
        ok(si.verify(base)["ok"], "the manifest is rewritten after promotion")
        rb = si.rollback_session(base, say=quiet)
        ok(rb["ok"] and open(os.path.join(base, "arch_demo.py"), "rb").read() == demo_bytes, "rollback restores the exact bytes", rb)
        ok(si.load_session(s["id"])["status"] == "rolled_back", "the session shows it was rolled back")

        # ---- journal + telemetry
        jp = os.path.join(dd, "orchestration", "local", s["id"] + "-r1", "journal.jsonl")
        evs = [json.loads(ln) for ln in open(jp, encoding="utf-8")] if os.path.isfile(jp) else []
        types = {e.get("type") for e in evs}
        ok({"launched", "phase", "started", "result", "plan_check", "gate", "done"} <= types, "each round is a crew journal with plan and gate events", types)
        pj = orch.parse_crew_journal(jp)
        ok(pj["phases"] == ["Audit", "Plan", "Build", "Verify"] and len(pj["agents"]) >= 6 and pj["done"]["verdict"] == "fail",
           "the Orchestration parser reads round 1: 4 phases, auditors+planner+builder+verifier, verdict fail",
           (pj["phases"], len(pj["agents"]), (pj["done"] or {}).get("verdict")))
        tel.flush()
        kinds = {e.get("kind") for e in tel.read(limit=0)}
        ok({"selfimprove.start", "selfimprove.round", "selfimprove.gate", "selfimprove.promote", "selfimprove.rollback"} <= kinds,
           "telemetry events for start, rounds, gates, promotion and rollback", sorted(k for k in kinds if k.startswith("selfimprove")))

        # ---- a failing loop cannot be promoted; a loop without progress stops; gate files need the flag
        sc2 = script_of({1: (["arch_demo.py"], {"arch_demo.py": DEMO_STUB})})
        s2 = si.loop("make it worse", rounds=4, fake=True, base=base, script=sc2, runners=runners(log), yes=True, say=quiet, data_dir=dd)
        ok(s2["status"] == "no_progress" and len(s2["rounds"]) == 2, "the same failure twice stops the loop (no progress)", (s2["status"], len(s2["rounds"])))
        try:
            si.promote_session(s2["id"], confirm=True, say=quiet)
            ok(False, "a session whose gates failed cannot be promoted, even with yes")
        except PermissionError:
            ok(True, "a session whose gates failed cannot be promoted, even with yes")
        ok(open(os.path.join(base, "arch_demo.py"), "rb").read() == demo_bytes, "...and the install is unchanged")
        sc3 = script_of({1: (["arch_selfimprove.py"], {"arch_selfimprove.py": "X = 2\n"})})
        s3 = si.loop("loosen the gate", rounds=1, fake=True, base=base, script=sc3, runners=runners(log), yes=True, say=quiet, data_dir=dd)
        ok(s3["status"] == "failed" and not s3["rounds"][0]["plan_check"]["ok"] and
           open(os.path.join(base, "arch_selfimprove.py"), encoding="utf-8").read().startswith("# stands in"),
           "a round that plans to edit the gates is refused before any builder runs", s3["rounds"][0]["plan_check"])
        s3b = si.loop("loosen the gate (reviewed)", rounds=1, fake=True, base=base, script=sc3, runners=runners(log), yes=False,
                      allow_gate_edits=True, confirm=lambda _: False, say=quiet, data_dir=dd)
        ok(s3b["rounds"][0]["plan_check"]["ok"] and s3b["status"] == "passed", "with --allow-gate-edits the same change can pass (and still waits for a yes)",
           [(x["name"], x["status"]) for x in s3b["rounds"][0]["gates"]])
        d = si.discard_session(s3b["id"])
        ok(d["status"] == "discarded" and not os.path.isdir(s3b["candidate"]), "discard removes the copy")
        again = si.loop(resume=s2["id"], rounds=1, fake=True, base=base, script=script_of({1: (["arch_demo.py"], {"arch_demo.py": DEMO_GOOD})}),
                        runners=runners(log), confirm=lambda _: False, say=quiet, data_dir=dd)
        ok(again["id"] == s2["id"] and len(again["rounds"]) == 3 and again["status"] == "passed", "another round continues the same session and copy",
           (len(again["rounds"]), again["status"]))
        rows = si.sessions()
        ok(len(rows) >= 4 and {"id", "goal", "status", "rounds"} <= set(rows[0]), "the history lists every session for the hub")

        # ---- no goal: picks the top failing check
        with open(os.path.join(base, "arch_demo.py"), "w", encoding="utf-8") as f:
            f.write(DEMO_STUB)
        sg = si.suggest(base)
        ok(sg["source"] == "failing check" and "arch_demo.py" in sg["goal"], "no goal: the top failing check becomes the goal", sg)
        ok(os.environ.get("OLLAMA_HOST") in (None, keep_env["OLLAMA_HOST"]) or "127.0.0.1" in os.environ.get("OLLAMA_HOST", ""),
           "only the local fake model was used")
    finally:
        core.set_home(old_home)
        for k, v in keep_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{'ALL ' + str(N[0]) + ' SELFIMPROVE CHECKS PASSED' if not N[1] else str(N[1]) + ' SELFIMPROVE CHECK(S) FAILED'}")
    return N[1] == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
