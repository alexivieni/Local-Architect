"""The hybrid benchmark: the SAME ten tasks (evals/families/hybrid.py) under each Use Claude mode -- local only, escalation, plan
(Claude thinks, your models write) and all -- one task at a time, judged by the eval's own code judges. Per mode it reports the pass
rate, seconds, Claude dollars and tokens per finished task, all read from the run traces. See docs/hybrid_benchmark.md.

    python tools/hybrid_bench.py --usd-cap 3                      every mode, at most $3 of Claude in total
    python tools/hybrid_bench.py --modes off,plan --usd-cap 1     two modes
    python tools/hybrid_bench.py --fake                           the plumbing: the oracle model answers, no GPU, no Claude
    python tools/hybrid_bench.py --list                           the tasks
    python tools/hybrid_bench.py --selftest                       this tool's own checks (never calls a model or Claude)

Why not plain `architect.py eval --set online.cloud_tier=<mode>`: the eval command's own `--set` takes only `representative`, and
`arch_eval.run` sets ARCHITECT_EVAL=1, under which `arch_cloud.allowed()` refuses every Claude call (on purpose: the normal benchmark
measures YOUR models). Every mode would silently have been local only. So each task runs through `arch_eval.run_task` -- the same
fixture, flow, judges and verdict rules -- in its own process WITHOUT that flag, and what the flag normally switches off is switched
off here by other means: ARCHITECT_NO_CACHE=1 (both reply caches), behavior.learn_from_runs=off, behavior.postmortem=off, and no
ledger record (a benchmark is a measurement, not experience).

Writes hub_data/learning/hybrid_<date>.md (the table, task by task, and the raw numbers).
"""
import argparse
import glob
import importlib.util
import json
import os
import shutil
import statistics
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FAM = os.path.join(ROOT, "evals", "families")
for _p in (ROOT, FAM):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import arch_core as core  # noqa: E402

MODES = ("off", "escalation", "plan", "all")
MODE_WORDS = {"off": "local only", "escalation": "escalation", "plan": "plan (Claude thinks)", "all": "all (Claude writes)"}
DEFAULT_CAP_USD = 2.0               # the whole benchmark, every cloud mode together
DEFAULT_TASK_USD = 0.5              # one task in one mode (limits.cloud_usd_per_run for that task's process)
CHILD_GRACE_S = 300                 # on top of a task's own time budget before its process is stopped
MIN_LEFT_USD = 0.005                # less than this left of the cap = the cap is reached


# ------------------------------------------------------------------------------------------------------ tasks
def _family():
    spec = importlib.util.spec_from_file_location("evals.families.hybrid", os.path.join(FAM, "hybrid.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_tasks(ids=None):
    """The hybrid tasks (forced on: the family is hidden from the normal eval), optionally only some ids (full id or its tail)."""
    import arch_eval
    tasks = [arch_eval.normalize(t) for t in _family().generate(None, force=True)]
    if ids:
        want = [i.strip() for i in ids if i.strip()]
        tasks = [t for t in tasks if any(t["id"] == w or t["id"].endswith("-" + w) for w in want)]
    return tasks


# ------------------------------------------------------------------------------------------------------ settings for one task
def _set_path(d, dotted, value):
    """--set a.b=c, typed the way architect.py types it (true/false, JSON, else the string)."""
    parts = dotted.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    if value.lower() in ("true", "false"):
        d[parts[-1]] = value.lower() == "true"
        return
    try:
        d[parts[-1]] = json.loads(value)
    except ValueError:
        d[parts[-1]] = value


def sets_to_dict(sets):
    out = {}
    for kv in sets or []:
        if "=" not in kv:
            raise SystemExit(f"--set needs key.path=value, got: {kv}")
        k, v = kv.split("=", 1)
        _set_path(out, k.strip(), v.strip())
    return out


def child_cfg(mode, task_usd, fake=False, sets=None):
    """The overrides one task runs with. Your --set values first, then the mode (the mode is what is being measured, so it wins)."""
    over = core.deep_merge(sets_to_dict(sets), {"behavior": {"learn_from_runs": "off", "postmortem": "off"}})
    if mode == "off" or fake:
        return core.deep_merge(over, {"online": {"cloud_tier": "off"}})
    return core.deep_merge(over, {"online": {"cloud_tier": mode}, "limits": {"cloud_usd_per_run": round(float(task_usd), 4)}})


def child_env(base=None):
    """The environment of a task's process: Claude reachable (no ARCHITECT_EVAL), both reply caches off. The heavy-job marker is
    inherited from the parent, which holds the lock, so the child never waits for itself."""
    env = dict(os.environ if base is None else base)
    env.pop("ARCHITECT_EVAL", None)
    env["ARCHITECT_NO_CACHE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def preflight(cfg, mode):
    """-> (ok, plain reason). A cloud mode runs only when a Claude call could really go out; otherwise the mode is reported as
    'not run' instead of quietly measuring your local models a second time."""
    if mode == "off":
        return True, ""
    import arch_cloud
    c = core.deep_merge(cfg, {"online": {"cloud_tier": mode}})
    ok, why = arch_cloud.allowed(c)
    if not ok:
        return False, why
    if not arch_cloud.cloud_model(c):
        return False, "no Claude model is set (Settings > Online > Claude model, or tiers.cloud)"
    if importlib.util.find_spec("anthropic") is None:
        return False, "the Anthropic SDK is not installed (pip install anthropic)"
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return False, "no key: set ANTHROPIC_API_KEY"
    if arch_cloud.spent_today() >= arch_cloud._day_cap(c):
        return False, f"Claude's daily cap is already reached (${arch_cloud.spent_today():.2f} of ${arch_cloud._day_cap(c):.2f})"
    return True, ""


# ------------------------------------------------------------------------------------------------------ traces
def trace_totals(work_dir):
    """Local and Claude use of every run under work_dir, from the traces (the same fields arch_costs reads). A reply answered from
    a cache (done_reason 'cached') cost nothing and is skipped; a broken line is skipped."""
    t = {"local_calls": 0, "local_tokens": 0, "claude_calls": 0, "claude_tokens_in": 0, "claude_tokens_out": 0, "claude_usd": 0.0}
    for p in glob.glob(os.path.join(work_dir, "*", ".arch", "runs", "*", "trace.jsonl")) + \
            glob.glob(os.path.join(work_dir, ".arch", "runs", "*", "trace.jsonl")):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(r, dict) or r.get("type") != "call" or r.get("done_reason") == "cached":
                        continue
                    tin, tout = int(r.get("prompt_tokens") or 0), int(r.get("gen_tokens") or 0)
                    if r.get("transport") == "claude" or str(r.get("model") or "").startswith("claude:"):
                        t["claude_calls"] += 1
                        t["claude_tokens_in"] += tin
                        t["claude_tokens_out"] += tout
                        try:
                            t["claude_usd"] += float(r.get("usd") or 0)
                        except (TypeError, ValueError):
                            pass
                    else:
                        t["local_calls"] += 1
                        t["local_tokens"] += tin + tout
        except OSError:
            continue
    t["claude_usd"] = round(t["claude_usd"], 5)
    return t


# ------------------------------------------------------------------------------------------------------ one task, in its own process
def run_child(task, mode, task_usd, fake=False, sets=None, timeout=None):
    """Runs one task in a fresh process (so the per-run Claude cap and every module state start clean). -> result dict with the
    trace totals. Its project lives in a folder this process chose, so even a task stopped on time still has its spend counted."""
    work = tempfile.mkdtemp(prefix="hybrid_")
    spec_path, out_path = os.path.join(work, "spec.json"), os.path.join(work, "result.json")
    runs = os.path.join(work, "runs")
    os.makedirs(runs, exist_ok=True)
    with open(spec_path, "w", encoding="utf-8") as f:
        json.dump({"task_id": task["id"], "mode": mode, "task_usd": task_usd, "fake": bool(fake), "sets": list(sets or []),
                   "out": out_path, "tmp": runs}, f)
    secs_cap = float(timeout or (float(task["budget"].get("seconds") or 1800) + CHILD_GRACE_S))
    t0 = time.time()
    try:
        rc, out = core.run_bounded([sys.executable, "-B", os.path.abspath(__file__), "--child", spec_path], ROOT, secs_cap, child_env())
    except OSError as e:
        rc, out = 1, f"could not start: {e}"
    res = core.load_json(out_path) if os.path.isfile(out_path) else None
    if not isinstance(res, dict) or not res.get("verdict"):
        timed_out = rc is None
        res = {"id": task["id"], "flow": task["flow"], "verdict": "fail" if timed_out else "not_sure", "secs": round(time.time() - t0, 1),
               "error": (f"stopped after {int(secs_cap)} s" if timed_out else "the task's process ended without a result") + ": " +
                        (out or "").strip()[-300:], "judges": [], "detail": ""}
    res.update(trace_totals(runs))
    res["mode"] = mode
    res["task_usd_cap"] = task_usd
    res.pop("proj", None)
    shutil.rmtree(work, ignore_errors=True)
    return res


def child_main(spec_path):
    """Inside the task's process: run the task, write the result. Never touches the ledger; never learns from the run."""
    spec = core.load_json(spec_path)
    tempfile.tempdir = spec["tmp"]                                   # the project goes where the parent will read its trace
    os.environ.pop("ARCHITECT_EVAL", None)
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    import arch_eval
    import arch_ledger
    arch_ledger.record_run = lambda *a, **k: 0                      # a benchmark is a measurement, not experience
    task = next((t for t in load_tasks() if t["id"] == spec["task_id"]), None)
    if task is None:
        core._atomic_write(spec["out"], json.dumps({"id": spec["task_id"], "verdict": "not_sure", "error": "unknown task id", "secs": 0}))
        return 2
    over = child_cfg(spec["mode"], spec["task_usd"], spec["fake"], spec["sets"])
    srv = prev = None
    if spec["fake"]:                                                 # plumbing: exactly what `eval --fake` does
        import arch_roles
        os.environ["ARCHITECT_EVAL"] = "1"
        srv, prev = arch_eval.start_oracle([task])
        over = core.deep_merge(over, {"roles": {r: "fake:7b" for r in arch_roles.ROLES}, "default_model": "fake:7b"})
    try:
        res = arch_eval.run_task(task, over, keep=True)
    finally:
        if srv:
            arch_eval.stop_oracle(srv, prev)
    res.pop("proj", None)                                            # the parent removes the folder after reading the trace
    core._atomic_write(spec["out"], json.dumps(res))
    return 0


# ------------------------------------------------------------------------------------------------------ the benchmark
def _skipped(task, mode, why):
    return {"id": task["id"], "flow": task["flow"], "mode": mode, "verdict": "skipped", "secs": 0.0, "error": why, "detail": "",
            "local_calls": 0, "local_tokens": 0, "claude_calls": 0, "claude_tokens_in": 0, "claude_tokens_out": 0, "claude_usd": 0.0}


def summarize(results):
    """Per mode. 'Per finished task' = everything the mode spent (failed tries included) divided by the tasks it passed: what one
    finished piece of work really cost. None when nothing passed (no honest per-task figure exists)."""
    ran = [r for r in results if r["verdict"] != "skipped"]
    passed = sum(1 for r in ran if r["verdict"] == "pass")
    secs = round(sum(float(r.get("secs") or 0) for r in ran), 1)
    usd = round(sum(float(r.get("claude_usd") or 0) for r in ran), 5)
    ltok = sum(int(r.get("local_tokens") or 0) for r in ran)
    ctok = sum(int(r.get("claude_tokens_in") or 0) + int(r.get("claude_tokens_out") or 0) for r in ran)

    def per(x, nd=1):
        if not passed:
            return None
        return int(round(x / passed)) if nd == 0 else round(x / passed, nd)
    return {"tasks": len(results), "ran": len(ran), "passed": passed, "failed": sum(1 for r in ran if r["verdict"] == "fail"),
            "not_sure": sum(1 for r in ran if r["verdict"] == "not_sure"), "skipped": len(results) - len(ran),
            "pass_rate": round(100.0 * passed / len(ran), 1) if ran else None,
            "secs_total": secs, "secs_median": round(statistics.median([float(r.get("secs") or 0) for r in ran]), 1) if ran else None,
            "secs_per_pass": per(secs), "usd_total": usd, "usd_per_pass": per(usd, 4),
            "local_tokens": ltok, "claude_tokens": ctok, "local_tokens_per_pass": per(ltok, 0), "claude_tokens_per_pass": per(ctok, 0),
            "claude_calls": sum(int(r.get("claude_calls") or 0) for r in ran)}


def bench(modes=MODES, cap_usd=DEFAULT_CAP_USD, task_usd=DEFAULT_TASK_USD, ids=None, fake=False, sets=None, say=print,
          runner=None, check=None, out_path=None, tasks=None):
    """Every task under every mode, one at a time. The dollar cap is checked before each cloud task, and each task may spend at
    most min(task_usd, what is left). -> report dict; the Markdown table is written to out_path (default hub_data/learning)."""
    runner = runner or run_child
    check = check or preflight
    cfg = core.deep_merge(core.load_config(), sets_to_dict(sets))
    tasks = tasks if tasks is not None else load_tasks(ids)
    spent, per_mode, not_run = 0.0, {}, {}
    t0 = time.time()
    say(f"Hybrid benchmark: {len(tasks)} task(s) x {len(modes)} mode(s), Claude cap ${cap_usd:.2f}" + (" (oracle fake: plumbing only, no Claude)" if fake else ""))
    for mode in modes:
        ok, why = (True, "") if fake else check(cfg, mode)
        if not ok:
            not_run[mode] = why
            per_mode[mode] = [_skipped(t, mode, why) for t in tasks]
            say(f"[{mode}] not run: {why}")
            continue
        cloud = mode != "off" and not fake
        rs = []
        for t in tasks:
            left = cap_usd - spent
            if cloud and left < MIN_LEFT_USD:
                rs.append(_skipped(t, mode, f"the benchmark's Claude cap (${cap_usd:.2f}) was reached"))
                say(f"  SKIP  [{mode}] {t['id']}  the dollar cap is reached")
                continue
            r = runner(t, mode, round(min(task_usd, left), 4) if cloud else 0.0, fake, sets)
            spent += float(r.get("claude_usd") or 0)
            rs.append(r)
            mark = {"pass": "PASS", "fail": "FAIL", "not_sure": "????"}.get(r["verdict"], r["verdict"].upper())
            say(f"  {mark}  [{mode}] {t['id']}  {r.get('secs')}s, ${float(r.get('claude_usd') or 0):.3f}, {r.get('claude_calls', 0)} Claude call(s)"
                + ("" if r["verdict"] == "pass" else f"  - {(r.get('detail') or r.get('error') or '')[:140]}"))
        per_mode[mode] = rs
    report = {"ts": time.time(), "date": time.strftime("%Y-%m-%d %H:%M"), "fake": bool(fake), "cap_usd": cap_usd, "task_usd": task_usd,
              "spent_usd": round(spent, 5), "secs": round(time.time() - t0, 1), "modes": list(modes), "not_run": not_run,
              "tasks": [{"id": t["id"], "flow": t["flow"]} for t in tasks],
              "summary": {m: summarize(rs) for m, rs in per_mode.items()}, "results": per_mode,
              "models": {k: v for k, v in {"worker": (cfg.get("tiers") or {}).get("worker"), "heavy": (cfg.get("tiers") or {}).get("heavy"),
                                           "claude": (cfg.get("tiers") or {}).get("cloud") or (cfg.get("online") or {}).get("cloud_model")}.items() if v}}
    md = render(report)
    path = out_path or default_out()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(md)
        report["path"] = path
    except OSError as e:
        say(f"[!] could not write {path}: {e}")
    say(md.split("\n## Task by task")[0])
    if report.get("path"):
        say(f"Written: {report['path']}")
    return report


def default_out():
    d = os.path.join(core.data_dir(), "learning")
    p = os.path.join(d, f"hybrid_{time.strftime('%Y%m%d')}.md")
    return p if not os.path.exists(p) else os.path.join(d, f"hybrid_{time.strftime('%Y%m%d_%H%M%S')}.md")


def _f(x, money=False):
    if x is None:
        return "-"
    return f"${x:.4f}" if money else (f"{x:,}" if isinstance(x, int) else f"{x}")


def render(report):
    s = report["summary"]
    lines = [f"# Hybrid benchmark, {report['date']}", "",
             f"The same {len(report['tasks'])} tasks under each Use Claude mode, one at a time, judged by code (hidden checks + tests). "
             f"Claude cap ${report['cap_usd']:.2f}, spent ${report['spent_usd']:.4f}, {report['secs']} s in all."
             + (" **Oracle fake: this proves the plumbing only; the numbers say nothing about any model.**" if report["fake"] else ""),
             "", "Models: " + (", ".join(f"{k} `{v}`" for k, v in report["models"].items()) or "(defaults)"), "",
             "| mode | passed | pass rate | not sure | skipped | seconds | s per finished task | Claude $ | $ per finished task "
             "| local tokens per finished task | Claude tokens per finished task | Claude calls |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for m in report["modes"]:
        v = s.get(m) or summarize([])
        if m in report["not_run"]:
            lines.append(f"| {MODE_WORDS.get(m, m)} | not run: {report['not_run'][m]} |  |  |  |  |  |  |  |  |  |  |")
            continue
        lines.append(f"| {MODE_WORDS.get(m, m)} | {v['passed']}/{v['ran']} | {_f(v['pass_rate'])}% | {v['not_sure']} | {v['skipped']} | {_f(v['secs_total'])} "
                     f"| {_f(v['secs_per_pass'])} | {_f(v['usd_total'], True)} | {_f(v['usd_per_pass'], True)} | {_f(v['local_tokens_per_pass'])} "
                     f"| {_f(v['claude_tokens_per_pass'])} | {v['claude_calls']} |")
    lines += ["", "\"Per finished task\" = everything the mode spent, failed tries included, divided by the tasks it passed. "
              "A not-sure verdict is not a pass and not a fail (a check could not run). How to read the trade-off: docs/hybrid_benchmark.md.",
              "", "## Task by task", "", "| task | flow | " + " | ".join(MODE_WORDS.get(m, m) for m in report["modes"]) + " |",
              "|---|---|" + "---|" * len(report["modes"])]
    by = {m: {r["id"]: r for r in report["results"].get(m, [])} for m in report["modes"]}
    for t in report["tasks"]:
        cells = []
        for m in report["modes"]:
            r = by[m].get(t["id"])
            if not r:
                cells.append("-")
            elif r["verdict"] == "skipped":
                cells.append("skipped")
            else:
                cells.append(f"{r['verdict']} {r.get('secs')}s" + (f" ${float(r.get('claude_usd') or 0):.3f}" if r.get("claude_calls") else ""))
        lines.append(f"| {t['id']} | {t['flow']} | " + " | ".join(cells) + " |")
    raw = {"summary": s, "not_run": report["not_run"], "cap_usd": report["cap_usd"], "spent_usd": report["spent_usd"], "fake": report["fake"],
           "results": {m: [{k: r.get(k) for k in ("id", "verdict", "secs", "claude_usd", "claude_calls", "claude_tokens_in", "claude_tokens_out",
                                                  "local_tokens", "local_calls", "error")} for r in rs] for m, rs in report["results"].items()}}
    lines += ["", "<!-- raw numbers (machine-readable) -->", "<!-- " + json.dumps(raw).replace("--", "- -") + " -->", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------------ self-test
def selftest():
    """Never calls a model or Claude: the judges against each task's reference and wrong answers, the trace reader, the per-task
    math, the dollar cap, the settings and environment of a task's process, the report; then one task end to end on the oracle."""
    import arch_eval
    old = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_SELFTEST", "ARCHITECT_EVAL", "ARCHITECT_NO_CACHE", "ARCHITECT_HYBRID_TASKS")}
    os.environ["ARCHITECT_DATA_DIR"] = tempfile.mkdtemp(prefix="arch_data_")
    os.environ["ARCHITECT_SELFTEST"] = "1"                           # arch_cloud.allowed() is False: nothing can reach Claude
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    os.environ.pop("ARCHITECT_EVAL", None)
    os.environ.pop("ARCHITECT_HYBRID_TASKS", None)
    n, bad = [0], []

    def ok(cond, label):
        n[0] += 1
        if not cond:
            bad.append(label)
            print("  FAIL", label)

    def judge_files(task, overlay):
        root = tempfile.mkdtemp(prefix="hyb_judge_")
        try:
            arch_eval._write(root, task["fixture"].get("files"))
            ctx = {"before": {}, "lines_before": {}, "route": None, "json": None}
            for j in task["judge"]:
                for rel in (j.get("paths", []) if j.get("type") == "files_unchanged" else []):
                    ctx["before"][rel] = arch_eval._sha(arch_eval._read(root, rel))
                if j.get("type") == "no_shrink":
                    ctx["lines_before"][j["path"]] = len((arch_eval._read(root, j["path"]) or "").splitlines())
            arch_eval._write(root, overlay)
            out = [arch_eval.JUDGES[j["type"]](root, j, task, ctx) for j in task["judge"]]
            return all(o[0] for o in out), all(o[2] for o in out), "; ".join(o[1] for o in out if o[1])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    try:
        # 1. the family: hidden from the normal eval, ten tasks of the four kinds, valid
        ok(_family().generate(None) == [], "the family is empty unless asked for (normal eval baselines unchanged)")
        tasks = load_tasks()
        ok(len(tasks) == 10, f"ten tasks (got {len(tasks)})")
        ok(len({t["id"] for t in tasks}) == len(tasks), "task ids are unique")
        kinds = {}
        for t in tasks:
            kinds[t["flow"]] = kinds.get(t["flow"], 0) + 1
        ok(kinds == {"bugfix": 3, "feature": 3, "refactor": 2, "design": 2}, f"3 bugfix, 3 feature, 2 refactor, 2 design (got {kinds})")
        ok(all("hybrid" in t["tags"] for t in tasks), "every task is tagged hybrid")
        ok(not [p for t in tasks for p in arch_eval.validate(t)], "every task passes arch_eval.validate")
        ok(all(t.get("solution", {}).get("files") and t.get("wrong", {}).get("files") for t in tasks), "every task has a reference and a wrong answer")
        ok([t["id"] for t in load_tasks(["pager", "hybrid-design-relay"])] == ["hybrid-bugfix-pager", "hybrid-design-relay"], "tasks pick by id or id tail")

        # 2. the judges: the reference passes, the wrong answer is caught, a planted bug really fails before the fix
        for t in tasks:
            good, could, why = judge_files(t, t["solution"]["files"])
            ok(good and could, f"{t['id']}: the reference solution passes every judge ({why})")
            wrong, _, _ = judge_files(t, t["wrong"]["files"])
            ok(not wrong, f"{t['id']}: the wrong answer is caught")
            if t["flow"] in ("bugfix", "feature", "design"):
                before, _, _ = judge_files(t, {})
                ok(not before, f"{t['id']}: the untouched fixture does not already pass")

        # 3. the trace reader
        work = tempfile.mkdtemp(prefix="hyb_trace_")
        rd = os.path.join(work, "eval_x", ".arch", "runs", "r1")
        os.makedirs(rd)
        with open(os.path.join(rd, "trace.jsonl"), "w", encoding="utf-8") as f:
            for rec in ({"type": "call", "model": "qwen:7b", "prompt_tokens": 1000, "gen_tokens": 200, "transport": "llama"},
                        {"type": "call", "model": "claude:claude-opus-5-5", "prompt_tokens": 3000, "gen_tokens": 500, "transport": "claude", "usd": 0.022},
                        {"type": "call", "model": "claude:claude-opus-5-5", "prompt_tokens": 0, "gen_tokens": 0, "transport": "claude", "usd": 0.0, "done_reason": "cached"},
                        {"type": "verdict", "verdict": "pass"}):
                f.write(json.dumps(rec) + "\n")
            f.write("{broken line\n")
        tt = trace_totals(work)
        ok(tt["local_calls"] == 1 and tt["local_tokens"] == 1200, f"local calls and tokens from the trace ({tt})")
        ok(tt["claude_calls"] == 1 and tt["claude_tokens_in"] == 3000 and tt["claude_tokens_out"] == 500, "Claude calls and tokens; a cached reply is skipped")
        ok(abs(tt["claude_usd"] - 0.022) < 1e-9, "Claude dollars from the trace")
        ok(trace_totals(os.path.join(work, "nothing_here"))["claude_usd"] == 0.0, "no trace = zeros, no crash")
        shutil.rmtree(work, ignore_errors=True)

        # 4. per finished task
        rows = [{"id": "a", "verdict": "pass", "secs": 10, "claude_usd": 0.1, "local_tokens": 100, "claude_tokens_in": 50, "claude_tokens_out": 10, "claude_calls": 1},
                {"id": "b", "verdict": "fail", "secs": 30, "claude_usd": 0.3, "local_tokens": 300, "claude_tokens_in": 0, "claude_tokens_out": 0, "claude_calls": 2},
                {"id": "c", "verdict": "not_sure", "secs": 20, "claude_usd": 0.0, "local_tokens": 0},
                {"id": "d", "verdict": "skipped", "secs": 0, "claude_usd": 0.0}]
        sm = summarize(rows)
        ok(sm["ran"] == 3 and sm["passed"] == 1 and sm["not_sure"] == 1 and sm["skipped"] == 1, "skipped tasks are not counted as run")
        ok(sm["pass_rate"] == 33.3, f"pass rate over the tasks that ran ({sm['pass_rate']})")
        ok(sm["usd_per_pass"] == 0.4 and sm["secs_per_pass"] == 60.0, "per finished task includes the failed tries")
        ok(sm["local_tokens_per_pass"] == 400 and sm["claude_tokens_per_pass"] == 60 and sm["claude_calls"] == 3, "tokens per finished task")
        z = summarize([dict(rows[1])])
        ok(z["usd_per_pass"] is None and z["secs_per_pass"] is None, "nothing passed = no per-task figure (not zero, not infinity)")

        # 5. settings and environment of a task's process
        c_off, c_plan = child_cfg("off", 0.0), child_cfg("plan", 0.25, sets=["online.cloud_tier=all", "limits.num_ctx=4096"])
        ok(c_off["online"]["cloud_tier"] == "off" and c_off["behavior"]["learn_from_runs"] == "off", "local only: Claude off, nothing learned")
        ok(c_plan["online"]["cloud_tier"] == "plan" and c_plan["limits"]["cloud_usd_per_run"] == 0.25 and c_plan["limits"]["num_ctx"] == 4096,
           "a cloud mode: the mode wins over --set, the per-task cap is set, other --set values are kept")
        ok(c_plan["behavior"]["postmortem"] == "off", "no postmortem model call inside the benchmark")
        ok(child_cfg("all", 1.0, fake=True)["online"]["cloud_tier"] == "off", "the oracle run never turns Claude on")
        env = child_env({"ARCHITECT_EVAL": "1", "PATH": "x"})
        ok("ARCHITECT_EVAL" not in env and env["ARCHITECT_NO_CACHE"] == "1" and env["PATH"] == "x",
           "a task's process: no eval flag (it would refuse Claude), both caches off")
        ok(preflight(core.load_config(), "off") == (True, ""), "local only always runs")
        pf = preflight(core.load_config(), "plan")
        ok(pf[0] is False and pf[1], f"a cloud mode that cannot reach Claude is refused with a reason ({pf[1]})")
        try:
            sets_to_dict(["no_equals_sign"])
            ok(False, "a --set without = is refused")
        except SystemExit:
            ok(True, "a --set without = is refused")

        # 6. the benchmark loop: the dollar cap, modes not run, the report file
        caps = []

        def fake_runner(t, mode, cap, fake, sets):
            caps.append((mode, cap))
            usd = 0.03 if mode != "off" else 0.0
            return {"id": t["id"], "flow": t["flow"], "mode": mode, "verdict": "pass" if mode == "plan" else "fail", "secs": 5.0, "claude_usd": usd,
                    "claude_calls": 1 if usd else 0, "claude_tokens_in": 100 if usd else 0, "claude_tokens_out": 20 if usd else 0,
                    "local_tokens": 500, "local_calls": 3, "detail": "", "error": ""}

        out_md = os.path.join(os.environ["ARCHITECT_DATA_DIR"], "learning", "hybrid_test.md")
        rep = bench(modes=("off", "plan", "all"), cap_usd=0.05, task_usd=0.5, say=lambda m: None, runner=fake_runner, tasks=tasks[:4],
                    check=lambda cfg, m: (m != "all", "Claude is off (test)"), out_path=out_md)
        ok([c for m, c in caps if m == "off"] == [0.0] * 4, "local only: every task runs, no Claude allowance")
        ok([c for m, c in caps if m == "plan"] == [0.05, 0.02], f"each cloud task may spend min(per task, what is left) ({caps})")
        ok([r["verdict"] for r in rep["results"]["plan"]] == ["pass", "pass", "skipped", "skipped"], "after the cap: the rest is skipped, not run")
        ok(abs(rep["spent_usd"] - 0.06) < 1e-9, "spend is added up from the results")
        ok(rep["not_run"] == {"all": "Claude is off (test)"} and all(r["verdict"] == "skipped" for r in rep["results"]["all"]), "a refused mode is reported as not run")
        ok(rep["summary"]["plan"]["usd_per_pass"] == 0.03 and rep["summary"]["off"]["usd_per_pass"] is None, "the summary per mode")
        txt = open(out_md, encoding="utf-8").read() if os.path.isfile(out_md) else ""
        ok("| local only |" in txt and "| plan (Claude thinks) |" in txt and "not run: Claude is off (test)" in txt, "the report table names every mode")
        ok("## Task by task" in txt and "hybrid-bugfix-pager" in txt and "<!-- {" in txt, "the report has task-by-task rows and the raw numbers")
        ok(default_out().startswith(os.path.join(os.environ["ARCHITECT_DATA_DIR"], "learning")) and "hybrid_" in default_out(),
           "the report goes to hub_data/learning/hybrid_<date>.md")

        # 7. end to end: one task in its own process, answered by the oracle (proves the child path and the trace path)
        pager = load_tasks(["pager"])[0]
        r = run_child(pager, "off", 0.0, fake=True)
        ok(r["verdict"] == "pass", f"the oracle passes the pager task through its own process ({r.get('verdict')}: {r.get('detail') or r.get('error')})")
        ok(r["local_calls"] > 0 and r["claude_calls"] == 0 and r["claude_usd"] == 0.0, f"its trace was read: local calls only ({r.get('local_calls')})")
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    if bad:
        print(f"{len(bad)} of {n[0]} HYBRID BENCH CHECKS FAILED")
        return 1
    print(f"ALL {n[0]} HYBRID BENCH CHECKS PASSED")
    return 0


# ------------------------------------------------------------------------------------------------------ command line
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--modes", default=",".join(MODES), help="comma-separated: " + ",".join(MODES) + " (off = local only)")
    ap.add_argument("--usd-cap", type=float, default=DEFAULT_CAP_USD, help=f"the most Claude may cost across the whole benchmark (default ${DEFAULT_CAP_USD})")
    ap.add_argument("--task-usd", type=float, default=DEFAULT_TASK_USD, help=f"the most one task may cost in one mode (default ${DEFAULT_TASK_USD})")
    ap.add_argument("--tasks", help="comma-separated task ids (or their tails, e.g. pager,relay)")
    ap.add_argument("--set", action="append", metavar="KEY.PATH=VALUE", help="a setting for every task (the mode itself always wins)")
    ap.add_argument("--fake", action="store_true", help="answer with the oracle model: proves the plumbing, never calls Claude")
    ap.add_argument("--out", help="where to write the Markdown (default hub_data/learning/hybrid_<date>.md)")
    ap.add_argument("--list", action="store_true", help="list the tasks and exit")
    ap.add_argument("--selftest", action="store_true", help="this tool's own checks")
    ap.add_argument("--child", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.child:
        return child_main(a.child)
    if a.selftest:
        return selftest()
    ids = [x for x in (a.tasks or "").split(",") if x.strip()] or None
    if a.list:
        for t in load_tasks(ids):
            print(f"  {t['flow']:9} {t['id']:28} {t['goal'][:80]}")
        return 0
    modes = [m.strip() for m in a.modes.split(",") if m.strip()]
    unknown = [m for m in modes if m not in MODES]
    if unknown:
        raise SystemExit(f"unknown mode(s): {', '.join(unknown)}; choose from {', '.join(MODES)}")
    if a.usd_cap < 0 or a.task_usd < 0:
        raise SystemExit("the dollar caps cannot be negative")
    import arch_heavy
    cfg = core.load_config()
    with arch_heavy.one_at_a_time("hybrid benchmark", cfg, say=print):      # one heavy job at a time, below normal priority
        rep = bench(modes, a.usd_cap, a.task_usd, ids, a.fake, a.set, out_path=a.out)
    return 0 if rep.get("path") else 1


if __name__ == "__main__":
    sys.exit(main())
