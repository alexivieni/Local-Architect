"""Run the training tasks (distributed systems + harness building) on the LOCAL models, one at a time, to produce verified traces.

    python tools/train_traces.py                          # all tasks, ~5 h on an RTX 4050 with the 8B worker
    python tools/train_traces.py --n 10                   # a quick taste
    python tools/train_traces.py --until 06:30            # a data night: stop starting tasks at 06:30
    python tools/train_traces.py --max-tasks 40 --worker qwen3:8b
    python tools/train_traces.py --curriculum --until 06:30 --heavy-retries 12   # + tasks aimed at the failures, heavy local model retries failures

Free (local models only: Claude is never used during eval). Every task that passes its hidden tests becomes training data through
`python architect.py dataset`, which runs at the end. It stops CLEANLY between tasks -- never in the middle of one -- when the end time
would be passed, the task limit is reached, or hub_data/STOP exists. Each night gets a fresh seed for the generated task variants
(recorded in hub_data/data_night.json), so the same night twice does not repeat the same tasks.

--curriculum adds NEW code-generated tasks shaped by the live failure triage (arch_curriculum; each proven against its hidden judge). The worker
runs everything first; then, within a budget (--heavy-retries, --heavy-share of the night) the heavy LOCAL model retries the worker's failures,
labelled "teacher": a failed worker run plus a passed teacher run of the same task becomes a distillation sample for the worker
(arch_dataset: teacher_model / student_model / lesson in its meta). Claude is never used. The end prints counts and samples per lesson and hour.
"""
import argparse
import datetime
import inspect
import json
import os
import subprocess
import sys
import time

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)
TAGS = (("distributed", "traces_distributed"), ("harness", "traces_harness"))


def _data_dir():
    return os.environ.get("ARCHITECT_DATA_DIR") or os.path.join(APP, "hub_data")


def stopped():
    return os.path.exists(os.path.join(_data_dir(), "STOP"))


def until_epoch(hhmm, now=None):
    """'06:30' -> the next 06:30 from now (tomorrow when that time has passed today). None/'' -> None."""
    if not hhmm:
        return None
    h, m = (int(x) for x in str(hhmm).strip().split(":"))
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError("say the end time as HH:MM")
    now = datetime.datetime.fromtimestamp(now if now is not None else time.time())
    end = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if end <= now:
        end += datetime.timedelta(days=1)
    return end.timestamp()


def fresh_seed(record=True):
    """A seed this data folder has not used for a night yet: today's date, bumped past every seed already used."""
    path = os.path.join(_data_dir(), "data_night.json")
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    used = set(st.get("seeds") or [])
    seed = int(time.strftime("%Y%m%d"))
    while seed in used:
        seed += 1
    if record:
        st["seeds"] = sorted(used | {seed})[-400:]
        st["last"] = {"seed": seed, "ts": time.time()}
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(st, f, indent=1)
        except OSError:
            pass
    return seed


def _eval_runner(tasks, label, cfg_over, should_stop, say):
    """Run tasks through arch_eval. When arch_eval.run takes `should_stop` (it checks before each task), one report for the
    night; otherwise one task per call, so the stop still falls between tasks. -> results list."""
    import arch_eval
    if "should_stop" in inspect.signature(arch_eval.run).parameters:
        return (arch_eval.run(tasks=tasks, label=label, cfg_over=cfg_over, say=say, should_stop=should_stop) or {}).get("results") or []
    out = []
    for t in tasks:
        if should_stop():
            break
        out += (arch_eval.run(tasks=[t], label=label, cfg_over=cfg_over, say=say) or {}).get("results") or []
    return out


def _export(say):
    say("=== exporting the training data")
    return subprocess.call([sys.executable, os.path.join(APP, "architect.py"), "dataset"], cwd=APP)


def _heavy_model():
    try:
        import arch_core as core
        return ((core.load_config().get("tiers") or {}).get("heavy")) or None
    except Exception:
        return None


def export_report(out_dir=None, elapsed_s=0.0, say=print):
    """Read the datasets the export wrote. -> {"files", "sft", "dpo", "distill", "per_lesson", "samples_per_hour"}, and print it.
    Counts come from the per-model tier files (one row, once)."""
    out_dir = out_dir or os.path.join(_data_dir(), "datasets")
    try:
        names = sorted(fn for fn in os.listdir(out_dir) if fn.endswith(".jsonl"))
    except OSError:
        names = []
    files, lessons, counts = {}, {}, {"sft": 0, "dpo": 0, "distill": 0}
    for fn in names:
        rows = []
        try:
            with open(os.path.join(out_dir, fn), encoding="utf-8") as f:
                for line in f:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            continue
        files[fn] = len(rows)
        per_model = fn.startswith("tier-") and fn.count(".") >= 3          # tier-<tier>.<model>.sft|dpo.jsonl
        if per_model and fn.endswith(".sft.jsonl"):
            counts["sft"] += len(rows)
            for r in rows:
                l = (r.get("meta") or {}).get("lesson") or "(none)"
                lessons[l] = lessons.get(l, 0) + 1
        elif per_model and fn.endswith(".dpo.jsonl"):
            counts["dpo"] += len(rows)
        elif fn.startswith("distill-"):
            counts["distill"] += len(rows)
    total = counts["sft"] + counts["dpo"]
    hours = max(float(elapsed_s), 1.0) / 3600.0
    rep = {"files": files, "per_lesson": lessons, "samples_per_hour": round(total / hours, 1), **counts}
    say(f"=== exported: {counts['sft']} SFT (includes {counts['distill']} distilled), {counts['dpo']} DPO pairs; per-model files: "
        + (", ".join(f"{k} {v}" for k, v in files.items() if k.startswith("tier-") and k.count(".") >= 3) or "none"))
    say("=== samples per lesson: " + (", ".join(f"{k} {v}" for k, v in sorted(lessons.items())) or "none"))
    say(f"=== {rep['samples_per_hour']} samples/hour over {elapsed_s / 3600.0:.2f} h")
    return rep


def night(n=None, until=None, max_tasks=None, worker=None, seed=None, say=print, runner=None, export=None, load=None, now=time.time,
          notify=True, curriculum=False, curriculum_n=None, cur_load=None, heavy=None, heavy_retries=8, heavy_share=0.25, report=None):
    """One data night. -> {"ran", "passed", "stopped_by", "seed", "exported", "heavy_ran", "teacher_passed", "report"}.
    runner/export/load/cur_load/report replace the real eval in tests. With `curriculum`, new tasks from arch_curriculum come first (they
    target the failures), then the training families (held-out variants never run). The worker runs everything; the heavy local model
    (`heavy`, default the configured heavy tier) then retries up to `heavy_retries` of the failures within `heavy_share` of the night,
    labelled "teacher"."""
    import arch_eta
    t_end = until_epoch(until) if isinstance(until, str) else until
    t_start = now()
    seed = fresh_seed() if seed is None else seed
    cfg_over = {"tiers": {"worker": worker}} if worker else None
    if load is None:
        import arch_eval
        load = lambda tag: arch_eval.load_tasks(tags=[tag], n=n, seed=seed)       # noqa: E731
    plan = []
    if curriculum:
        if cur_load is None:
            import arch_curriculum
            cur_load = lambda k, sd: arch_curriculum.tasks(k, sd)                 # noqa: E731
        plan.append(("curriculum", "traces_curriculum", cur_load(int(curriculum_n or n or 40), seed)))
    plan += [(tag, label, load(tag)) for tag, label in TAGS]
    plan = [(tag, label, [t for t in ts if "holdout" not in (t.get("tags") or [])]) for tag, label, ts in plan]   # held-out work is never run here
    total = sum(len(ts) for _, _, ts in plan)
    if max_tasks:
        total = min(total, int(max_tasks))
    heavy = heavy if heavy is not None else (_heavy_model() if heavy_retries else None)
    teach = bool(heavy and heavy_retries)
    say(f"=== data night: {total} task(s), seed {seed}, local models only" + (f", worker {worker}" if worker else "")
        + (", curriculum on" if curriculum else "") + (f", teacher {heavy} (up to {heavy_retries} retries)" if teach else "")
        + (f", until {time.strftime('%H:%M', time.localtime(t_end))}" if t_end else ""))
    # the worker phase leaves `heavy_share` of the time to the teacher's retries (one model swap, not one per failure)
    t_worker_end = t_end - (t_end - t_start) * float(heavy_share) if (t_end and teach) else t_end
    secs, results, why = [], [], ""
    state = {"t0": now()}

    def should_stop(limit=None):
        nonlocal why
        lim = t_worker_end if limit is None else limit
        if stopped():
            why = "the STOP file"
        elif limit is None and max_tasks and len(results) + state.get("in_batch", 0) >= int(max_tasks):
            why = f"the task limit ({max_tasks})"
        elif lim:
            per = arch_eta.per_task(family=state.get("tag"), model=worker, own=secs)["p50"] or 0
            if now() + per > lim:
                why = "the end time (the next task would not finish before it)"
        return bool(why)

    tasks_by_id = {}
    for tag, label, tasks in plan:
        if should_stop():
            break
        state["tag"] = tag
        say(f"=== {tag}")

        def say_progress(m, _tag=tag):
            say(m)
            if m.lstrip().startswith(("PASS", "FAIL", "????")):
                state["in_batch"] = state.get("in_batch", 0) + 1
                el = now() - state["t0"]
                secs.append(el)
                state["t0"] = now()
                say("    " + arch_eta.estimate(len(results) + state["in_batch"], total, family=_tag, model=worker, own=secs, until=t_end)["line"])
        state["in_batch"] = 0
        state["t0"] = now()
        tasks_by_id.update({t["id"]: t for t in tasks})
        got = (runner or _eval_runner)(tasks, label, cfg_over, should_stop, say_progress)
        state["in_batch"] = 0
        results += got
    passed = sum(1 for r in results if r.get("verdict") == "pass")
    why = why or "every task ran"

    # ---- the teacher: the heavy LOCAL model retries what the worker failed (a distillation pair needs both halves)
    heavy_ran = teacher_passed = 0
    failed = [tasks_by_id[r["id"]] for r in results if r.get("verdict") == "fail" and r.get("id") in tasks_by_id]
    if teach and failed and not stopped():
        why_worker, why = why, ""
        say(f"=== teacher: {heavy} retries up to {heavy_retries} of the {len(failed)} task(s) the worker failed")
        state["tag"] = "teacher"
        over = {"tiers": {"worker": heavy}}
        for t in failed[:int(heavy_retries)]:
            if should_stop(limit=t_end or 0):
                break
            got = (runner or _eval_runner)([t], "teacher", over, lambda: should_stop(limit=t_end or 0), say)
            heavy_ran += len(got)
            teacher_passed += sum(1 for r in got if r.get("verdict") == "pass")
        why = why or why_worker
        say(f"=== teacher: {teacher_passed} of {heavy_ran} retried task(s) passed (each pair of worker-failed + teacher-passed is a distillation sample)")
    say(f"=== stopped by {why}: {len(results)} task(s), {passed} passed")
    code = (export or _export)(say)
    rep = (report or export_report)(None, now() - t_start, say)
    if notify:
        try:
            import arch_notify
            arch_notify.notify("Architect: data night finished", f"{passed} of {len(results)} tasks passed; stopped by {why}")
        except Exception:
            pass
    return {"ran": len(results), "passed": passed, "stopped_by": why, "seed": seed, "exported": code == 0, "heavy_ran": heavy_ran,
            "teacher_passed": teacher_passed, "report": rep}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, help="tasks to generate per family (default: each family's own)")
    ap.add_argument("--until", help="HH:MM: start no task that would not finish before this time")
    ap.add_argument("--max-tasks", type=int, help="stop after this many tasks")
    ap.add_argument("--worker", help="the local model to use as the worker tier tonight (default: the current worker)")
    ap.add_argument("--seed", type=int, help="the task-variant seed (default: a fresh one per night)")
    ap.add_argument("--curriculum", action="store_true", help="add new tasks shaped by the failure triage (arch_curriculum)")
    ap.add_argument("--curriculum-n", type=int, help="how many curriculum tasks to generate (default --n or 40)")
    ap.add_argument("--heavy", help="the teacher: the heavy local model that retries the worker's failures (default: the heavy tier)")
    ap.add_argument("--heavy-retries", type=int, default=8, help="at most this many retries by the heavy model (0 = none)")
    ap.add_argument("--heavy-share", type=float, default=0.25, help="with --until: the share of the night kept for the teacher's retries")
    a = ap.parse_args(argv)
    r = night(n=a.n, until=a.until, max_tasks=a.max_tasks, worker=a.worker, seed=a.seed, curriculum=a.curriculum, curriculum_n=a.curriculum_n,
              heavy=a.heavy, heavy_retries=a.heavy_retries if (a.curriculum or a.heavy) else 0, heavy_share=a.heavy_share)
    return 0 if r["exported"] else 1


if __name__ == "__main__":
    sys.exit(main())
