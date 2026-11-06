"""Self-test for the local training-data pipeline: the failure-shaped curriculum, task-level teacher distillation, the data night.

No model, no GPU, no network, no Claude: the night's runner/export are stand-ins and the traces are hand-made. Proves
  * the curriculum family: tasks tagged training + a lesson, held-out templates held out WHOLE, fresh seeds give fresh variants, goals deduped,
    every template's reference passes its hidden judge and its wrong answer is caught (--quick: one task per template);
  * arch_curriculum: the failure profile (live reports, else the measured default), weights, a plan that excludes held-out templates,
    no copy of any bench task (id, goal, template, public names), no template in both buckets;
  * distillation: a worker-failed + teacher-passed pair of kept TRAIN runs becomes a distill sample labelled teacher/student/lesson; a
    bench pair, a Claude teacher, a cut-off reply, an unterminated <think> and a duplicate do not; the lesson reaches SFT/DPO/escalation rows;
  * no Claude text: a run with a claude call is dropped, a FORMAT REFERENCE block that is not the model's own is cut, nothing in the
    curriculum reads playbooks/ or imports the cloud module;
  * tools/train_traces.py --curriculum: the worker runs first, the heavy model retries failures within its budget labelled "teacher",
    held-out tasks never run, the export report prints counts per lesson and samples per hour.
"""
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="archcur_")
os.environ["ARCHITECT_DATA_DIR"] = os.path.join(_TMP, "_data")
os.environ["ARCHITECT_HOME"] = _TMP
os.environ["ARCHITECT_NO_CACHE"] = "1"
os.environ["ARCHITECT_NO_NOTIFY"] = "1"


def _trace(run_dir, records, et=None):
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "trace.jsonl"), "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    if et is not None:
        with open(os.path.join(run_dir, "eval_task.json"), "w", encoding="utf-8") as f:
            json.dump(et, f)


def _call(cid, user, reply, model, tier="worker", label="write", **kw):
    r = {"type": "call", "id": cid, "role": "coder", "label": label, "model": model, "tier": tier, "flow": "build",
         "messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": user}], "reply": reply}
    r.update(kw)
    return r


def _mark(cid, ok=True):
    return {"type": "mark", "id": cid, "accepted": ok}


def _tt():
    spec = importlib.util.spec_from_file_location("train_traces", os.path.join(HERE, "tools", "train_traces.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


TASK = ("Build a Python package `ember relay` ... " * 12)           # a long shared prompt: the pair is recognised as one task
ANS = "FILE: a.py\n```python\nvalue = 1\n```\n"


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_core as core
    import arch_curriculum as cur
    import arch_dataset as ds
    import arch_eval as E
    core.set_home(_TMP)
    fam = cur.family_module()

    print("\n== the curriculum family ==")
    tasks = [E.normalize(dict(t, flow=fam.FLOW)) for t in fam.all_tasks(0)]
    keys = sorted({t["template"] for t in tasks})
    check("at least 14 build templates plus repair variants, every task validates", len(keys) >= 18 and not [p for t in tasks for p in E.validate(t)], keys)
    check("ids unique and sort after e2e (eval --quick unchanged)", len({t["id"] for t in tasks}) == len(tasks) and all(t["id"] > "e2e-~" for t in tasks))
    check("every task: training tag, a known lesson tag, a lesson field that agrees",
          all("training" in t["tags"] and cur.lesson_of(t) in fam.LESSONS and cur.lesson_of(t) == t["lesson"] for t in tasks),
          [t["id"] for t in tasks if cur.lesson_of(t) not in fam.LESSONS][:3])
    check("all five lessons are covered", {t["lesson"] for t in tasks} >= {"wiring", "contract_first", "repair_focus", "edge_cases", "tests_present"})
    check("held-out templates are held out WHOLE", not cur.held_out_whole(tasks) and {t["template"] for t in tasks if "holdout" in t["tags"]} == set(fam.HOLDOUT_TEMPLATES),
          cur.held_out_whole(tasks))
    check("goals are distinct within a seed", len({t["goal"] for t in tasks}) == len(tasks))
    g0 = {t["goal"] for t in fam.all_tasks(0)}
    g1 = {t["goal"] for t in fam.all_tasks(1)}
    check("a fresh seed gives fresh variants (at least three in four goals differ)", len(g0 & g1) <= len(g0) // 4, len(g0 & g1))
    check("generate() is seed-stable and a short run touches many templates",
          [t["id"] for t in fam.generate(10, 3)] == [t["id"] for t in fam.generate(10, 3)] and len({t["template"] for t in fam.generate(10, 3)}) == 10)
    check("loader picks the family up through arch_eval", any(t["id"].startswith("train-cur-") for t in E.load_tasks(tags=["curriculum"])))
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fam.check(quick=True)
    check("every template: reference passes the hidden judge and its tests, the wrong answer is caught", rc == 0, buf.getvalue()[-600:])

    print("\n== arch_curriculum ==")
    d = cur.profile(reports=[])
    check("no saved report -> the measured live profile (wiring 26, behaviour 43, tests 8, environment 7 of 153)",
          d["source"] == "default" and (d["wiring"], d["behaviour"], d["tests"], d["environment"], d["total"]) == (26, 43, 8, 7, 153), d)
    rep = {"results": [
        {"id": "x1", "verdict": "fail", "error": "ImportError: cannot import name 'f'", "judges": [{"type": "python_assert", "ok": False, "reason": "ImportError"}]},
        {"id": "x2", "verdict": "fail", "error": "", "judges": [{"type": "python_assert", "ok": False, "reason": "assert 1 == 2"}]},
        {"id": "x3", "verdict": "pass", "judges": []}]}
    pl = cur.profile(reports=[rep])
    check("a live report is classified by arch_triage", pl["source"] == "live" and pl["wiring"] == 1 and pl["behaviour"] == 1 and pl["total"] == 3, pl)
    w = cur.weights(d)
    check("weights sum to 1 and wiring/behaviour lead", abs(sum(w.values()) - 1) < 0.01 and w["behaviour"] > 0 and w["wiring"] > 0, w)
    p = cur.plan(60, 5, d)
    check("the plan sums to n and never plans a held-out template", sum(c for _, c in p) == 60 and not {k for k, _ in p} & set(fam.HOLDOUT_TEMPLATES), p)
    ts = cur.tasks(30, 11, d)
    check("tasks(): n new trainable tasks, no holdout, deduped", len(ts) == 30 and all("holdout" not in t["tags"] for t in ts) and len({t["goal"] for t in ts}) == 30,
          len(ts))
    more = cur.tasks(30, 11, d, used_goals={t["goal"] for t in ts})
    check("goals already used on an earlier night are not generated again", not ({t["goal"] for t in more} & {t["goal"] for t in ts}))
    probs = cur.bench_overlap()
    check("no curriculum task copies a bench task (id, goal, template, public names)", not probs, probs[:3])
    check("no template is in both the training and the bench bucket", not [k for k in ds.template_overlap() if k.startswith("curriculum:")], ds.template_overlap()[:3])
    src = open(os.path.join(HERE, "evals", "families", "curriculum.py"), encoding="utf-8").read() + open(os.path.join(HERE, "arch_curriculum.py"), encoding="utf-8").read()
    check("the curriculum imports no cloud/playbook module and reads no playbooks/ folder",
          not re.search(r"^\s*(import|from)\s+arch_(cloud|playbook|claudecode|agents|fleet)", src, re.M) and "playbooks/" not in src.replace("playbooks/ folder", ""))
    pb = os.path.join(HERE, "playbooks")
    leak = []
    if os.path.isdir(pb):
        lines = {ln.strip() for fn in os.listdir(pb) if fn.endswith(".md") for ln in open(os.path.join(pb, fn), encoding="utf-8", errors="replace") if len(ln.strip()) > 60}
        leak = [ln for ln in lines if any(ln in t["goal"] for t in tasks)]
    check("no playbook line appears in a curriculum goal", not leak, leak[:1])

    print("\n== teacher distillation ==")
    root = os.path.join(_TMP, "trainroot")
    runs = os.path.join(root, ".arch", "runs")
    TAGS = ["training", "curriculum", "lesson:wiring"]

    def pair(name, student_model="qwen3:8b", teacher_model="qwen3.6-moe", label="teacher", verdict="pass", reply=ANS, teacher_extra=None, bucket="train",
             base=runs, tid="train-cur-pkgexport-s1-00", t_tier="worker"):
        _trace(os.path.join(base, name + "_w"), [_call(1, TASK, "FILE: a.py\n```python\nbroken(\n```\n", student_model), _mark(1, False)],
               {"id": tid, "bucket": bucket, "verdict": "fail", "tags": TAGS, "label": "traces_curriculum"})
        rec = _call(1, TASK, reply, teacher_model, tier=t_tier, **(teacher_extra or {}))
        _trace(os.path.join(base, name + "_t"), [rec, _mark(1, True)],
               {"id": tid, "bucket": bucket, "verdict": verdict, "tags": TAGS, "label": label})

    pair("good")
    got = ds.build([root])
    dis = (got["distill"].get("worker") or [])
    check("a worker-failed + teacher-passed pair makes one distill sample for the worker", len(dis) == 1 and got["stats"].get("task_distill") == 1, got["stats"])
    m = dis[0]["meta"] if dis else {}
    check("labelled: teacher_model, student_model, lesson, for_model, source",
          m.get("teacher_model") == "qwen3.6-moe" and m.get("student_model") == "qwen3:8b" and m.get("lesson") == "wiring" and m.get("for_model") == "qwen3:8b"
          and m.get("source") == "task-distill", m)
    check("the sample teaches the TEACHER's reply to the shared prompt", dis and dis[0]["messages"][-1]["content"] == ANS and dis[0]["messages"][-2]["content"] == TASK)
    check("the passing run's own SFT row carries the lesson", any((r["meta"].get("lesson") == "wiring") for v in got["sft"].values() for r in v), got["sft"])

    shutil.rmtree(root)
    pair("len", reply=ANS, teacher_extra={"done_reason": "length"})
    check("a teacher reply cut off at its limit is not taught", not ds.build([root])["distill"])
    shutil.rmtree(root)
    pair("think", reply="<think>hmm " + ANS)
    check("a teacher reply with an unterminated <think> is not taught", not ds.build([root])["distill"])
    shutil.rmtree(root)
    pair("think2", reply="<think>plan it</think>\n" + ANS)
    g2 = ds.build([root])
    r2 = (g2["distill"].get("worker") or [{}])[0]
    check("a complete <think> block is cut from the taught reply and kept in meta.thinking",
          r2.get("messages", [{}])[-1].get("content") == ANS and "plan it" in (r2.get("meta", {}).get("thinking") or ""), r2.get("meta"))
    shutil.rmtree(root)
    pair("cl", teacher_model="claude:opus")
    pair("fk", student_model="fake:7b", tid="other-task")
    g3 = ds.build([root])
    check("a Claude teacher and a fake: student never become distillation", not g3["distill"] and g3["stats"].get("claude_runs", 0) >= 1, g3["stats"])
    shutil.rmtree(root)
    pair("tr", verdict="fail")
    check("a teacher run that also failed teaches nothing", not ds.build([root])["distill"])
    shutil.rmtree(root)
    pair("nolabel", label="", teacher_model="other-model")
    check("a passing run that is neither labelled teacher nor the heavy tier's model is not a teacher", not ds.build([root])["distill"])
    shutil.rmtree(root)
    pair("dup1")
    pair("dup2")
    g4 = ds.build([root])
    check("the same pair twice is exported once (dedupe)", len(g4["distill"].get("worker") or []) == 1 and g4["stats"].get("duplicates", 0) >= 1, g4["stats"])
    shutil.rmtree(root)
    benchdir = os.path.join(_TMP, "evals_runs_bench")
    bench_runs = os.path.join(benchdir, "evals", "runs", "bench", ".arch", "runs")
    pair("bn", base=bench_runs, bucket="bench")
    check("a bench-bucket pair is never read", not ds.build([os.path.join(benchdir, "evals", "runs", "bench")])["distill"])
    shutil.rmtree(root, ignore_errors=True)
    pair("lbl")
    _trace(os.path.join(runs, "esc"), [_call(1, TASK + " other", "bad", "qwen3:8b", label="s1-try1"), _mark(1, False),
                                       _call(2, TASK + " other", ANS, "qwen3.6-moe", tier="heavy", label="s1-try2"), _mark(2, True),
                                       {"type": "escalation", "step": "s1", "role": "coder", "from_model": "qwen3:8b", "to_model": "qwen3.6-moe", "from_tier": "worker",
                                        "to_tier": "heavy", "flow": "build"}],
           {"id": "esc-task", "bucket": "train", "verdict": "pass", "tags": ["training", "lesson:contract_first"], "label": "x"})
    g5 = ds.build([root])
    esc = [r for r in (g5["distill"].get("worker") or []) if r["meta"].get("source") == "escalation"]
    check("in-run escalation distillation fires for a train-bucket run, with teacher/student/lesson labels",
          len(esc) == 1 and esc[0]["meta"]["teacher_model"] == "qwen3.6-moe" and esc[0]["meta"]["student_model"] == "qwen3:8b"
          and esc[0]["meta"]["lesson"] == "contract_first", [r["meta"] for r in esc])
    out_dir = os.path.join(_TMP, "ds_out")
    ds.write(g5, out_dir, tier_models={"worker": "qwen3:8b"})
    wrote = sorted(os.listdir(out_dir))
    check("export writes per-model files (tier-worker.<model>.sft.jsonl) and the distill file", any(f.startswith("tier-worker.") and f.endswith(".sft.jsonl") for f in wrote)
          and "distill-worker.sft.jsonl" in wrote, wrote)

    print("\n== no Claude text ==")
    shutil.rmtree(root, ignore_errors=True)
    FREF = "\n\nFORMAT REFERENCE (a past reply that passed):\nFILE: z.py\n```python\nclaude_wrote_this = 1\n```\n"
    _trace(os.path.join(runs, "fmt"), [_call(1, TASK + FREF, ANS, "qwen3:8b"), _mark(1, True)],
           {"id": "fmt", "bucket": "train", "verdict": "pass", "tags": ["training"], "label": "x"})
    _trace(os.path.join(runs, "cc"), [_call(1, TASK + " claude", ANS, "claude:opus", transport="claude"), _mark(1, True)],
           {"id": "cc", "bucket": "train", "verdict": "pass", "tags": ["training"], "label": "x"})
    _trace(os.path.join(runs, "cc2"), [_call(1, TASK + " second", ANS, "qwen3:8b"), _call(2, TASK + " third", ANS, "claude:sonnet"), _mark(1, True), _mark(2, True)],
           {"id": "cc2", "bucket": "train", "verdict": "pass", "tags": ["training"], "label": "x"})
    g6 = ds.build([root])
    blob = json.dumps(g6["sft"]) + json.dumps(g6["dpo"]) + json.dumps(g6["distill"])
    check("no sample holds a claude: model, a claude transport call, or a foreign FORMAT REFERENCE block",
          "claude" not in blob and "FORMAT REFERENCE" not in blob and g6["stats"].get("claude_runs", 0) >= 2, g6["stats"])

    print("\n== the data night ==")
    tt = _tt()
    cur_tasks = [{"id": f"train-cur-a-s1-{i:02d}", "tags": ["training", "curriculum", "lesson:wiring"]} for i in range(4)] + \
                [{"id": "train-cur-hold-s1-00", "tags": ["training", "curriculum", "holdout"]}]
    fam_tasks = {"distributed": [{"id": "train-dist-x-00", "tags": ["training"]}, {"id": "train-dist-x-04", "tags": ["training", "holdout"]}],
                 "harness": [{"id": "train-harn-x-00", "tags": ["training"]}]}
    calls = []
    fail_worker = {"train-cur-a-s1-01", "train-cur-a-s1-03", "train-dist-x-00"}

    def runner(tasks, label, cfg_over, should_stop, say):
        out = []
        for t in tasks:
            if should_stop():
                break
            heavy = label == "teacher"
            calls.append((t["id"], label, (cfg_over or {}).get("tiers", {}).get("worker")))
            v = "pass" if (heavy or t["id"] not in fail_worker) else "fail"
            say(f"  {v.upper()}  {t['id']}")
            out.append({"id": t["id"], "verdict": v})
        return out
    say_log = []
    # a datasets folder the (fake) export "wrote"
    data = tt._data_dir()
    dsd = os.path.join(data, "datasets")
    os.makedirs(dsd, exist_ok=True)
    for fn, rows in {"tier-worker.qwen3-8b.sft.jsonl": [{"meta": {"lesson": "wiring"}}, {"meta": {"lesson": "wiring"}}, {"meta": {"lesson": "edge_cases"}}, {"meta": {}}],
                     "tier-worker.qwen3-8b.dpo.jsonl": [{"meta": {}}], "distill-worker.sft.jsonl": [{"meta": {"lesson": "wiring"}}]}.items():
        with open(os.path.join(dsd, fn), "w", encoding="utf-8") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)
    clock = [1000.0]
    r = tt.night(seed=7, say=say_log.append, runner=runner, export=lambda say: 0, load=lambda tag: list(fam_tasks[tag]), curriculum=True, curriculum_n=5,
                 cur_load=lambda k, sd: list(cur_tasks), heavy="qwen3.6-moe", heavy_retries=2, notify=False, now=lambda: clock[0] + (3600 if any('stopped by' in m for m in say_log) else 0))
    ids = [c[0] for c in calls]
    check("held-out tasks never run", "train-cur-hold-s1-00" not in ids and "train-dist-x-04" not in ids, ids)
    check("curriculum first, then the training families, worker before teacher",
          ids[:4] == [f"train-cur-a-s1-{i:02d}" for i in range(4)] and ids[4:6] == ["train-dist-x-00", "train-harn-x-00"], ids)
    teach = [c for c in calls if c[1] == "teacher"]
    check("the heavy model retries failures only, labelled teacher, within --heavy-retries", len(teach) == 2 and all(c[2] == "qwen3.6-moe" for c in teach)
          and {c[0] for c in teach} <= fail_worker and r["heavy_ran"] == 2 and r["teacher_passed"] == 2, calls)
    check("the night result counts ran/passed and carries the report", r["ran"] == 6 and r["passed"] == 3 and r["exported"] and r["report"]["sft"] == 4, r)
    text = "\n".join(say_log)
    check("it prints counts, samples per lesson and samples per hour", "samples per lesson" in text and "wiring 2" in text and "samples/hour" in text and "5.0 samples/hour" in text,
          text[-500:])
    calls.clear()
    tt.night(seed=8, say=lambda m: None, runner=runner, export=lambda say: 0, load=lambda tag: list(fam_tasks[tag]), heavy_retries=0, notify=False,
             report=lambda *a: {})
    check("without --curriculum / --heavy-retries it is the old night (no curriculum, no teacher)", not [c for c in calls if c[1] == "teacher"] and
          not [c for c in calls if c[0].startswith("train-cur")], calls)

    shutil.rmtree(_TMP, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' CURRICULUM CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' CURRICULUM CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
