"""Self-test for trajectory replay (arch_replay.py, evals/families/replay.py).

No model, no GPU, no network, no Claude. A throwaway git project and a made-up transcript folder stand in for the real ones, so the
suite is repeatable. Proves
  * agents() reads edited files and the suites an agent ran; the fixture is the file at the commit BEFORE the work (git show);
  * size limits: more than 6 files or more than 1500 lines is not a task;
  * a suite that is too slow, or already passes against the fixture, makes no task; one that fails the fixture and passes the
    finished files does; the judge is run for real: solution passes, the unchanged fixture fails, in under 60 s;
  * tasks are tagged training + source:replay + lesson:<id>; whole kinds are held out (~15%) and the kinds are disjoint;
  * NO CLAUDE TEXT: no 20+ character substring of any brief (or agent label) appears in any goal, even when the suite itself
    prints a line that echoes a brief; the goal is a template + names + the check;
  * git is only ever asked read-only commands; the module and the family never touch the cloud module;
  * the family serves the saved tasks, trainable first, and they validate under arch_eval;
  * on the real install (when its saved tasks exist): the same no-brief / holdout / runnable proofs over the real data.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="archreplay_")
os.environ["ARCHITECT_DATA_DIR"] = os.path.join(_TMP, "_data")
os.environ["ARCHITECT_HOME"] = _TMP
os.environ["ARCHITECT_NO_CACHE"] = "1"
os.environ["ARCHITECT_NO_NOTIFY"] = "1"

import arch_replay as R  # noqa: E402

PASS = FAIL = 0


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + (("  -> " + str(extra)[:300]) if extra else ""))


def git(cwd, *a, date=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    if date:
        env["GIT_COMMITTER_DATE"] = date
        env["GIT_AUTHOR_DATE"] = date
    subprocess.run(["git", "-C", cwd, *a], check=True, capture_output=True, env=env)


def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or root, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def transcript(base, near, sid, aid, start, edited, cmds, brief, label):
    slug = R.re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(near))
    d = os.path.join(base, slug, sid, "subagents")
    os.makedirs(d, exist_ok=True)
    recs = [{"type": "user", "timestamp": start, "sessionId": sid, "message": {"role": "user", "content": brief}}]
    blocks = [{"type": "tool_use", "id": "t%d" % i, "name": "Edit", "input": {"file_path": os.path.join(near, e)}} for i, e in enumerate(edited)]
    blocks += [{"type": "tool_use", "id": "c%d" % i, "name": "Bash", "input": {"command": c}} for i, c in enumerate(cmds)]
    recs.append({"type": "assistant", "timestamp": start, "sessionId": sid,
                 "message": {"id": "m" + aid, "model": "claude-x", "content": blocks, "usage": {"input_tokens": 1, "output_tokens": 1}}})
    with open(os.path.join(d, f"agent-{aid}.jsonl"), "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    with open(os.path.join(d, f"agent-{aid}.meta.json"), "w", encoding="utf-8") as f:
        json.dump({"description": label}, f)


BRIEF = ("You OWN the module {m}. Please fix the arithmetic helper so that every caller gets the sum instead of the difference; "
         "do not touch other modules and report back what you changed in two sentences.")
KINDS = ["alpha", "bravo", "cedar", "delta", "ember", "fjord", "gamma", "hazel"]


def make_project(root):
    """8 modules (each one task kind), each with a suite: before the work the module is wrong, after it right."""
    os.makedirs(root, exist_ok=True)
    git(root, "init", "-q")
    for k in KINDS:
        write(root, f"arch_{k}.py", f"def add(a, b):\n    return a - b\n\n\ndef twice(x):\n    return add(x, x)\n")
        write(root, f"arch_selftest_{k}.py", f"import arch_{k} as m\nassert m.add(2, 3) == 5, 'add gives {{}}'.format(m.add(2, 3))\nprint('ok')\n")
    # modules with the traps
    write(root, "arch_big.py", "x = 1\n" * 1600)                         # too many lines
    write(root, "arch_selftest_big.py", "import arch_big\nassert arch_big.x == 2\n")
    for i in range(7):
        write(root, f"arch_many{i}.py", "v = 0\n")                       # too many files
    write(root, "arch_selftest_many.py", "assert False\n")
    write(root, "arch_pass.py", "def f():\n    return 1\n")               # fixture already passes
    write(root, "arch_selftest_pass.py", "import arch_pass\nassert arch_pass.f() >= 1\n")
    write(root, "arch_slow.py", "def f():\n    return 1\n")               # suite too slow
    write(root, "arch_selftest_slow.py", "import time\ntime.sleep(6)\nimport arch_slow\nassert arch_slow.f() == 2\n")
    write(root, "arch_echo.py", "def f():\n    return 1\n")               # suite prints a line that echoes a brief
    write(root, "arch_selftest_echo.py", "import arch_echo\nassert arch_echo.f() == 2, " + repr(
        "FAIL: " + "please make the lookup helper return the cached value instead of recomputing it every time") + "\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "before", date="2026-11-01T10:00:00+00:00")
    for k in KINDS:
        write(root, f"arch_{k}.py", f"def add(a, b):\n    return a + b\n\n\ndef twice(x):\n    return add(x, x)\n")
    write(root, "arch_big.py", "x = 2\n" * 1600)
    for i in range(7):
        write(root, f"arch_many{i}.py", "v = 1\n")
    write(root, "arch_pass.py", "def f():\n    return 5\n")
    write(root, "arch_slow.py", "def f():\n    return 2\n")
    write(root, "arch_echo.py", "def f():\n    return 2\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "after", date="2026-11-03T10:00:00+00:00")


def main():
    print("arch_replay self-test")
    proj = os.path.join(_TMP, "proj")
    base = os.path.join(_TMP, "claude")
    make_project(proj)
    sid = "sess-1"
    briefs = {}
    for i, k in enumerate(KINDS):
        briefs[k] = BRIEF.format(m=f"arch_{k}.py") + f" Ticket number {i} concerns the {k} line."
        transcript(base, proj, sid, f"a{i}", "2026-11-02T12:00:00.000Z", [f"arch_{k}.py", f"arch_selftest_{k}.py"],
                   [f"py -3.12 arch_selftest_{k}.py", f"python arch_selftest_{k}.py --x"], briefs[k], f"Fix {k} add helper")
    briefs["big"] = "Edit the big module and make every constant equal to two across all sixteen hundred lines of it please."
    transcript(base, proj, sid, "b1", "2026-11-02T12:00:00.000Z", ["arch_big.py"], ["py arch_selftest_big.py"], briefs["big"], "Big edit")
    briefs["many"] = "Bump seven small modules at once and tell me when the full suite is green again, thank you very much."
    transcript(base, proj, sid, "b2", "2026-11-02T12:00:00.000Z", [f"arch_many{i}.py" for i in range(7)], ["py arch_selftest_many.py"], briefs["many"], "Many edit")
    briefs["pass"] = "Touch the pass-through module without altering what it promises to the callers who rely on it daily."
    transcript(base, proj, sid, "b3", "2026-11-02T12:00:00.000Z", ["arch_pass.py"], ["py arch_selftest_pass.py"], briefs["pass"], "Pass edit")
    briefs["slow"] = "Make the slow module return two and then run its long running suite to confirm the answer is right."
    transcript(base, proj, sid, "b4", "2026-11-02T12:00:00.000Z", ["arch_slow.py"], ["py arch_selftest_slow.py"], briefs["slow"], "Slow edit")
    briefs["echo"] = "Please make the lookup helper return the cached value instead of recomputing it every time, and report back."
    transcript(base, proj, sid, "b5", "2026-11-02T12:00:00.000Z", ["arch_echo.py"], ["py arch_selftest_echo.py"], briefs["echo"], "Echo edit")
    transcript(base, proj, sid, "b6", "2026-11-02T12:00:00.000Z", ["arch_alpha.py"], ["ls -la"], "Look around only.", "No suite run")   # no suite -> not a task
    # an agent whose work predates any commit of the file contributes nothing
    transcript(base, proj, sid, "b7", "2026-10-01T00:00:00.000Z", ["arch_bravo.py"], ["py arch_selftest_bravo.py"], "Very early work on the bravo module before anything existed.", "Early")

    print("\n-- reading the trajectories")
    ags = R.agents(proj, base, proj)
    ids = {a["id"] for a in ags}
    ok(len(ags) == 8 + 5 + 1 and "b6" not in ids, "agents: those that edited files and ran a suite (the look-around agent is not one)", sorted(ids))
    a0 = next(a for a in ags if a["id"] == "a0")
    ok(a0["suites"] == ["arch_selftest_alpha.py"] and "arch_alpha.py" in a0["edited"], "suites found from `py -3.12 arch_selftest_x.py ...` commands", a0)
    pre = R.before_state(proj, "arch_alpha.py", "2026-11-02T12:00:00.000Z")
    ok(pre is not None and "a - b" in pre, "fixture = the file at the commit before the work, not the finished one", pre)
    ok(R.before_state(proj, "arch_alpha.py", "2026-10-01T00:00:00.000Z") is None, "a file not yet in git is not a fixture")
    try:
        R.git(proj, "add", "-A")
        ok(False, "git refuses write commands")
    except ValueError:
        ok(True, "git refuses write commands")
    src = open(os.path.join(HERE, "arch_replay.py"), encoding="utf-8").read()
    ok("arch_cloud" not in src and "import anthropic" not in src, "arch_replay never touches the cloud module")

    print("\n-- size limits")
    cs = R.candidates(proj, ags=ags)
    srcs = {c["sources"][0] for c in cs}
    ok("arch_big.py" not in srcs, "1600 lines is over the limit")
    ok(not any(len(c["sources"]) > R.MAX_FILES for c in cs) and not any("arch_many0.py" in c["sources"] for c in cs), "7 files is over the limit")
    ok(all(sum(R._lines(t) for t in c["pre"].values()) <= R.MAX_LINES for c in cs), "every fixture within 1500 lines")
    ok(not any(c["agent"] == "b7" for c in cs), "no task for work whose file was not in git before it")

    print("\n-- building (real judge)")
    R.SUITE_SECS = 3                                   # the 6-second suite is too slow under this limit
    t0 = time.time()
    res = R.build(proj, base, proj, data_dir=os.path.join(_TMP, "_data"), ags=ags, log=lambda *_: None, workers=4)
    tasks = res["tasks"]
    st = res["stats"]
    ok(len(tasks) == 9, "9 tasks: the 8 module agents + echo (slow, pass, big, many make none)", [t["id"] for t in tasks] + [st["dropped"]])
    ok("arch_selftest_slow.py" in st["suites_rejected"], "a suite over the time limit is rejected", st)
    ok(st["dropped"].get("fixture already passes (no signal)", 0) >= 1, "a suite the fixture already passes makes no task", st["dropped"])
    ok(all("training" in t["tags"] and "source:replay" in t["tags"] and any(g.startswith("lesson:") for g in t["tags"]) for t in tasks),
       "tags: training + source:replay + lesson:<id>")
    ok(all(t["lesson"] in R.LESSONS for t in tasks), "lessons are known lesson ids")
    held = [t for t in tasks if "holdout" in t["tags"]]
    train = [t for t in tasks if "holdout" not in t["tags"]]
    ok(1 <= len(held) <= 2 and len(train) >= 6, f"about 15% held out ({len(held)} of {len(tasks)})")
    ok(not ({t["kind"] for t in held} & {t["kind"] for t in train}), "holdout kinds are disjoint from trained kinds")
    ok(R.pick_holdout({"a": 1, "b": 1, "c": 1}) == set() and len(R.pick_holdout({k: 3 for k in "abcdefg"})) >= 1, "holdout picks whole kinds, none when too few")
    ok(all(len(t["fixture"]["files"]) <= R.MAX_FILES and sum(R._lines(x) for x in t["fixture"]["files"].values()) <= R.MAX_LINES for t in tasks),
       "fixtures within 6 files / 1500 lines")

    print("\n-- no Claude text in goals")
    texts = R.briefs(ags=ags)
    ok(len(texts) >= 13, "the proof has the briefs and labels to compare with", len(texts))
    ok(not any(R.leaks(R.prose(t["goal"]), texts) for t in tasks), "no 20-character substring of any brief or label in any goal (file names, check commands and definition names in `code spans` are named, as they must be)")
    ok(R.leaks("xx " + briefs["alpha"][10:60] + " yy", texts) and not R.leaks("totally unrelated words", texts), "the leak detector catches a copied fragment")
    ok(all("Ticket number" not in t["goal"] and "report back" not in t["goal"] for t in tasks), "no brief wording at all in the goals")
    ok(all(t["kind"] in KINDS + ["echo"] for t in tasks), "kinds named from the module, not from a brief")
    ex = tasks[0]["goal"]
    ok(f"`python arch_selftest_{tasks[0]['kind']}.py`" in ex and f"arch_{tasks[0]['kind']}.py" in ex and "add" in ex, "goal = template + file + check + changed definitions", ex)
    echo_goal = [t for t in tasks if t["kind"] == "echo"]
    ok(len(echo_goal) == 1 and "lookup helper" not in echo_goal[0]["goal"], "a failure line that echoes a brief is left out of the goal")

    print("\n-- the judge runs")
    t = tasks[0]
    spec = t["replay"]
    for label, files, want in (("finished files", t["solution"]["files"], True), ("unchanged fixture", t["wrong"]["files"], False)):
        d = tempfile.mkdtemp(prefix="archreplayj_")
        try:
            for rel, text in files.items():
                write(d, rel, text)
            s0 = time.time()
            good, rows, why = R.run_judge(d, spec, proj)
            ok(good is want and time.time() - s0 < 60, f"judge: {label} -> {'pass' if want else 'fail'} in {time.time() - s0:.1f}s", (good, why))
        finally:
            shutil.rmtree(d, ignore_errors=True)
    d = tempfile.mkdtemp(prefix="archreplayj_")
    try:
        code = [j for j in t["judge"] if j["type"] == "python_assert"][0]["code"]
        for rel, text in t["solution"]["files"].items():
            write(d, rel, text)
        p = subprocess.run([sys.executable, "-B", "-c", code], cwd=d, capture_output=True, env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
        ok(p.returncode == 0, "the task's python_assert (as eval runs it) passes the finished files", p.stdout[-200:] + p.stderr[-200:])
        for rel, text in t["wrong"]["files"].items():
            write(d, rel, text)
        p = subprocess.run([sys.executable, "-B", "-c", code], cwd=d, capture_output=True, env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
        ok(p.returncode != 0, "and fails the unchanged fixture")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    ok(not os.path.exists(os.path.join(proj, "architect_config.json")), "no settings file was ever written")
    ok(all(any(j["type"] == "python_assert" for j in x["judge"]) and all(x["replay"]["suites"]) for x in tasks), "every task has a runnable judge on named suites")
    ok(all(x["solution"]["files"] and x["wrong"]["files"] for x in tasks), "every task has a reference solution and a wrong answer")

    print("\n-- the family")
    import importlib.util
    spec_ = importlib.util.spec_from_file_location("replay_family", os.path.join(HERE, "evals", "families", "replay.py"))
    fam = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(fam)
    ok(fam.DEFAULT_N == 0 and fam.FLOW == "feature", "family: a normal eval carries none; flow is feature")
    got = fam.generate(100, 0)
    ok(len(got) == len(tasks) and all("holdout" not in x["tags"] for x in got[:len(train)]) and all("holdout" in x["tags"] for x in got[len(train):]),
       "generate serves the saved tasks, trainable first, held out last")
    import arch_eval as E
    ok(all(not E.validate(E.normalize(dict(x))) for x in got), "every task validates under arch_eval")
    ok("arch_cloud" not in open(os.path.join(HERE, "evals", "families", "replay.py"), encoding="utf-8").read(), "the family never touches the cloud module")

    print("\n-- the real install")
    real = os.path.join(HERE, "hub_data", "replay", "tasks.json")
    if not os.path.isfile(real):
        print("  (no saved real tasks: run  py -3.12 arch_replay.py --build)")
    else:
        rt = json.load(open(real, encoding="utf-8")).get("tasks") or []
        try:
            rags = R.agents()
            rtexts = R.briefs(ags=rags)
        except Exception as e:                      # transcripts of another machine: skip, say so
            rags, rtexts = [], []
            print("  (real transcripts unreadable: %s)" % e)
        ok(len(rt) >= 1, f"{len(rt)} real replay tasks saved")
        if rtexts:
            ok(not any(R.leaks(R.prose(x["goal"]), rtexts) for x in rt), "real goals: no 20-character substring of any real brief or label")
        rheld = {x["kind"] for x in rt if "holdout" in x["tags"]}
        rtrain = {x["kind"] for x in rt if "holdout" not in x["tags"]}
        ok(not (rheld & rtrain), "real holdout kinds disjoint", rheld & rtrain)
        ok(all("training" in x["tags"] and "source:replay" in x["tags"] for x in rt), "real tasks tagged training + source:replay")
        ok(all(len(x["fixture"]["files"]) <= R.MAX_FILES and sum(R._lines(v) for v in x["fixture"]["files"].values()) <= R.MAX_LINES for x in rt),
           "real fixtures within the limits")
        ok(all(os.path.isfile(os.path.join(HERE, s)) for x in rt for s in x["replay"]["suites"]), "every real judge suite exists")
        if rt:
            x = rt[0]
            d = tempfile.mkdtemp(prefix="archreplayr_")
            try:
                for rel, text in x["solution"]["files"].items():
                    write(d, rel, text)
                s0 = time.time()
                good, _, why = R.run_judge(d, x["replay"], HERE)
                ok(good is True and time.time() - s0 < 60, f"real judge of {x['id']} runs offline in {time.time() - s0:.0f}s (< 60)", why)
            finally:
                shutil.rmtree(d, ignore_errors=True)

    shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
