"""One unattended night: benchmark your local models, export what they learned, read it back, and leave a note for the morning.

    python tools/overnight.py                      # the whole night (7 h limit by default)
    python tools/overnight.py --hours 5 --quick    # shorter: the first 10 tasks of each family
    python tools/overnight.py --dry-run            # say what would run, run nothing
    python tools/overnight.py --with-claude        # also ask Claude for a short second opinion on the note (gated, capped)

The steps, in order:
  1. the eval families (`distributed`, `harness`, then the representative set) through the existing `architect.py eval` command,
     one at a time, at below-normal priority, while this process holds the heavy-job lock (so no suite or tuning run shares the GPU);
  2. the training dataset export (near-duplicates dropped, samples under the quality floor left out);
  3. the logbook: ingest everything new and rewrite the playbooks;
  4. the overseer's lessons from the agents' own records;
  5. a copy of cold results (eval reports, the export, playbooks, lessons, runs left pending) on the archive drive, if
     Settings > Storage > Archive folder is set: copies only, nothing is moved or deleted;
  6. the morning note: hub_data/learning/morning_<date>.md.

Stopping: create hub_data/OVERNIGHT_STOP during the night and the current eval is ended and the rest skipped (the note is still
written); the file is removed at the end so it does not stop tomorrow's night. hub_data/overnight.pause skips whole nights until you
delete it. The time limit ends an eval that would run past it and skips the evals after it; the cheap steps 2-6 always run.

Nothing here talks to Claude unless --with-claude is given, and even then only through arch_cloud's own gate (online features on,
spend caps) with a hard per-night cap. The evals cannot use Claude at all (arch_cloud refuses during eval). Standard library only.
"""
import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import arch_core as core  # noqa: E402

FAMILIES = ("distributed", "harness")
RESERVE_S = 15 * 60           # kept back from the time limit for the export, logbook, archive and note
POLL_S = 5.0
MIN_QUALITY = 0.5             # arch_dataset.quality_score floor for the export (0-1)
CLAUDE_USD = 0.10             # the most --with-claude may spend in one night


# ------------------------------------------------------------------------------------------------------------- paths and state
def data_dir():
    return core.data_dir()


def learning_dir():
    return os.path.join(data_dir(), "learning")


def stop_path():
    return os.path.join(data_dir(), "OVERNIGHT_STOP")


def pause_path():
    return os.path.join(data_dir(), "overnight.pause")


def state_path():
    return os.path.join(learning_dir(), "overnight_state.json")


def load_state():
    try:
        with open(state_path(), encoding="utf-8") as f:
            j = json.load(f)
        return j if isinstance(j, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(st):
    try:
        os.makedirs(learning_dir(), exist_ok=True)
        core._atomic_write(state_path(), json.dumps(st, indent=1))
    except (OSError, AttributeError):
        try:
            with open(state_path(), "w", encoding="utf-8") as f:
                json.dump(st, f, indent=1)
        except OSError:
            pass


class Night:
    """The clock, the stop file and the record of what happened, shared by every step."""

    def __init__(self, hours, log=None, now=time.time):
        self.now = now
        self.t0 = now()
        self.deadline = self.t0 + max(0.0, float(hours)) * 3600
        self.steps = []             # {"step", "status": ran|skipped|failed|stopped, "detail", ...}
        self.problems = []
        self.log_path = log
        self.stopped = False

    def say(self, msg):
        line = time.strftime("%H:%M:%S ") + msg
        print(line, flush=True)
        if self.log_path:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def left(self):
        return self.deadline - self.now()

    def stop_asked(self):
        """True when OVERNIGHT_STOP was created after this night began (an older one is left over, and is ignored)."""
        if self.stopped:
            return True
        try:
            if os.path.getmtime(stop_path()) >= self.t0 - 1:
                self.stopped = True
        except OSError:
            pass
        return self.stopped

    def record(self, step, status, detail="", **extra):
        self.steps.append({"step": step, "status": status, "detail": detail, **extra})
        self.say(f"[{step}] {status}" + (f": {detail}" if detail else ""))

    def problem(self, text):
        self.problems.append(text)
        self.say("[problem] " + text)


# ------------------------------------------------------------------------------------------------------------- step 1: evals
def family_flow(name, fam_dir=None):
    """-> the FLOW a family file declares (read with ast, the module is not run), its own name when it declares none, or None when
    there is no such family file."""
    p = os.path.join(fam_dir or os.path.join(ROOT, "evals", "families"), name + ".py")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError, ValueError):
        return name
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "FLOW" for t in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    return name


def family_args(name, tasks):
    """The narrowest `eval` arguments that cover one family: --flow alone when the family owns that flow's tasks, plus --tags when a
    tag is shared by all of the family's tasks and nobody else's in that flow. -> (args, note); args None when the family has no tasks."""
    mine = [t for t in tasks if t.get("family") == name]
    if not mine:
        return None, f"family {name} produced no tasks"
    flow = mine[0].get("flow")
    others = [t for t in tasks if t.get("flow") == flow and t.get("family") != name]
    args = ["--flow", flow]
    if not others:
        return args, ""
    common = set(mine[0].get("tags") or [])
    for t in mine[1:]:
        common &= set(t.get("tags") or [])
    for tag in sorted(common):
        if not any(tag in (t.get("tags") or []) for t in others):
            return args + ["--tags", tag], ""
    return args, f"the {flow} flow also has {len(others)} task(s) from other families; they run too"


def eval_plan(families, quick=False, load_tasks=None):
    """-> [{"name", "args", "note"}] for the families, then the representative set. A family that is not installed is a skipped entry."""
    plan = []
    tasks = None
    for fam in families:
        if family_flow(fam) is None:
            plan.append({"name": fam, "args": None, "note": f"no evals/families/{fam}.py on this install"})
            continue
        if tasks is None:
            try:
                if load_tasks is None:
                    import arch_eval
                    load_tasks = arch_eval.load_tasks
                tasks = [dict(t) for t in load_tasks()]
            except Exception as e:                       # one broken family never stops the night
                tasks = []
                plan.append({"name": fam, "args": None, "note": f"could not list the eval tasks: {type(e).__name__}: {e}"})
                continue
        args, note = family_args(fam, tasks)
        if args is None:
            plan.append({"name": fam, "args": None, "note": note})
            continue
        plan.append({"name": fam, "args": args + (["--quick"] if quick else []) + ["--label", "overnight-" + fam], "note": note})
    plan.append({"name": "representative", "args": ["--set", "representative"], "note": ""})
    return plan


def _kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        else:
            proc.kill()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.wait(timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def run_child(cmd, night, limit_s, popen=subprocess.Popen):
    """Run one command, one line at a time into the night's log, ending it at the time limit or when the stop file appears.
    -> {"rc", "lines", "why"}; why is "" (finished), "time" or "stop". The child inherits this process's priority and heavy-job hold."""
    env = dict(os.environ)              # never from scratch: ARCHITECT_HEAVY_HELD must reach the child (CLAUDE.md, heavy jobs)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    lines, why = [], ""
    import threading
    proc = popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")

    def pump():
        for ln in proc.stdout:
            ln = ln.rstrip("\n")
            lines.append(ln)
            if night.log_path:
                try:
                    with open(night.log_path, "a", encoding="utf-8") as f:
                        f.write("    " + ln + "\n")
                except OSError:
                    pass

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    end = night.now() + max(0.0, limit_s)
    while proc.poll() is None:
        if night.stop_asked():
            why = "stop"
        elif night.now() >= end:
            why = "time"
        if why:
            _kill_tree(proc)
            break
        time.sleep(POLL_S)
    th.join(10)
    return {"rc": proc.returncode, "lines": lines, "why": why}


def last_json(lines):
    for ln in reversed(lines):
        ln = ln.strip()
        if ln.startswith("{") and ln.endswith("}"):
            try:
                return json.loads(ln)
            except ValueError:
                continue
    return None


def step_evals(night, families, quick=False, python=None, dry=False, popen=subprocess.Popen, load_tasks=None):
    out = []
    for item in eval_plan(families, quick, load_tasks):
        name = item["name"]
        if item["args"] is None:
            night.record("eval " + name, "skipped", item["note"])
            out.append({"name": name, "status": "skipped", "detail": item["note"]})
            continue
        cmd = [python or sys.executable, os.path.join(ROOT, "architect.py"), "eval"] + item["args"] + ["--json"]
        if item["note"]:
            night.say(f"[eval {name}] note: {item['note']}")
        if dry:
            night.record("eval " + name, "skipped", "dry run: " + " ".join(cmd[2:]))
            out.append({"name": name, "status": "skipped", "detail": "dry run"})
            continue
        if night.stop_asked():
            night.record("eval " + name, "stopped", "the stop file was created")
            out.append({"name": name, "status": "stopped"})
            continue
        room = night.left() - RESERVE_S
        if room < 60:
            night.record("eval " + name, "skipped", "the time limit leaves no room for it")
            out.append({"name": name, "status": "skipped", "detail": "time limit"})
            continue
        night.say(f"[eval {name}] starting: {' '.join(cmd[2:])} (at most {int(room / 60)} min)")
        t = night.now()
        try:
            r = run_child(cmd, night, room, popen=popen)
        except OSError as e:
            night.problem(f"eval {name} could not start: {e}")
            out.append({"name": name, "status": "failed", "detail": str(e)})
            continue
        rep = last_json(r["lines"]) or {}
        s = rep.get("summary") or {}
        row = {"name": name, "secs": round(night.now() - t), "rc": r["rc"], "n": s.get("n"), "passed": s.get("passed"),
               "not_sure": s.get("not_sure"), "pass_rate": s.get("pass_rate"), "tokens": s.get("tokens"), "path": rep.get("path"),
               "task_changes": rep.get("task_changes"), "per_flow": {k: v.get("pass_rate") for k, v in (s.get("flows") or {}).items()}}
        if r["why"] == "stop":
            row["status"] = "stopped"
            night.record("eval " + name, "stopped", "ended early: the stop file was created")
        elif r["why"] == "time":
            row["status"] = "stopped"
            night.problem(f"eval {name} was ended at the time limit; its results are incomplete")
        elif not rep:
            row["status"] = "failed"
            tail = " | ".join(x for x in r["lines"][-3:] if x.strip())
            night.problem(f"eval {name} gave no report (exit {r['rc']}): {tail[:300]}")
        else:
            row["status"] = "ran"
            night.record("eval " + name, "ran", f"{s.get('passed')}/{s.get('n')} passed ({s.get('pass_rate')}%), {s.get('not_sure')} not sure")
        out.append(row)
    return out


# ------------------------------------------------------------------------------------------------------------- steps 2-5
def step_dataset(night, min_quality=MIN_QUALITY, ds=None):
    """The export with near-duplicates dropped and a quality floor. -> {"sft", "dpo", "distill", "traj", "new_sft", ...} or None."""
    try:
        ds = ds or __import__("arch_dataset")
        data = ds.build(ds.roots(ROOT, include_portfolio=True), min_quality=min_quality, dedupe=True)
        summary = ds.write(data, os.path.join(data_dir(), "datasets"))
    except Exception as e:
        night.problem(f"dataset export failed: {type(e).__name__}: {e}")
        night.record("dataset", "failed", str(e))
        return None
    st = summary.get("stats") or {}
    state = load_state()
    before = state.get("dataset") or {}
    res = {k: int(st.get(k) or 0) for k in ("runs", "sft", "dpo", "distill", "traj", "duplicates", "secrets_skipped", "too_long", "low_quality")}
    res["new_sft"] = max(0, res["sft"] - int(before.get("sft") or 0))
    res["new_dpo"] = max(0, res["dpo"] - int(before.get("dpo") or 0))
    res["first_time"] = not before
    state["dataset"] = {k: res[k] for k in ("sft", "dpo", "distill", "traj")}
    save_state(state)
    night.record("dataset", "ran", f"{res['sft']} samples ({res['new_sft']} new), {res['dpo']} pairs, {res['duplicates']} near-duplicates dropped")
    return res


def step_logbook(night, include_claude=True, learn=None):
    try:
        learn = learn or __import__("arch_learn")
        ing = learn.ingest()
        path = learn.playbooks(include_claude=include_claude)
    except Exception as e:
        night.problem(f"logbook failed: {type(e).__name__}: {e}")
        night.record("logbook", "failed", str(e))
        return None
    night.record("logbook", "ran", f"{ing.get('added', 0)} new, {ing.get('updated', 0)} updated of {ing.get('files', 0)} records; playbooks: {path}")
    return {"ingest": ing, "playbooks": path}


def step_lessons(night, overseer=None):
    try:
        overseer = overseer or __import__("arch_overseer")
        out = overseer.lessons(write=True) or []
    except Exception as e:
        night.problem(f"overseer lessons failed: {type(e).__name__}: {e}")
        night.record("lessons", "failed", str(e))
        return None
    night.record("lessons", "ran", f"{len(out)} lesson(s)")
    return list(out)


def _copy_if_new(src, dest, max_bytes):
    """Copy one file unless the archive already has it at the same size. -> True when copied."""
    try:
        size = os.path.getsize(src)
        if size > max_bytes:
            return False
        if os.path.isfile(dest) and os.path.getsize(dest) == size:
            return False
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(src, dest)
        return True
    except OSError:
        return False


def step_archive(night, cfg, extra_files=()):
    """Copies of cold results on the archive drive. Never moves or deletes anything here. -> {"copied", "pending_left"} or None."""
    archive = ((cfg or {}).get("storage") or {}).get("archive") or ""
    if not archive:
        night.record("archive", "skipped", "no archive folder is set (Settings > Storage > Archive folder)")
        return None
    if not os.path.isdir(archive):
        night.problem(f"the archive folder {archive} is not there (unplugged?); nothing was copied, the next night tries again")
        night.record("archive", "skipped", "archive drive not found")
        return None
    import arch_archive as A
    base = os.path.join(archive, "architect_archive", "_overnight")
    copied = 0
    d = data_dir()
    groups = [(os.path.join(d, "evals"), "evals"), (os.path.join(d, "datasets"), "datasets")]
    for src_dir, sub in groups:
        if not os.path.isdir(src_dir):
            continue
        for dirpath, _dirs, files in os.walk(src_dir):
            rel = os.path.relpath(dirpath, src_dir)
            for fn in files:
                parts = [A._safe(p) for p in ([] if rel == "." else rel.split(os.sep))] + [A._safe(fn)]
                if _copy_if_new(os.path.join(dirpath, fn), os.path.join(base, sub, *parts), A.MAX_BYTES):
                    copied += 1
    for p in list(extra_files) + [os.path.join(d, "learning", "LESSONS.md"), os.path.join(d, "evals.jsonl")]:
        if p and os.path.isfile(p) and _copy_if_new(p, os.path.join(base, "notes", A._safe(os.path.basename(p))), A.MAX_BYTES):
            copied += 1
    left = []
    for item in A._load_pending():            # runs whose write-behind copy was missed (drive unplugged at the time)
        if not A.copy_run(archive, item.get("run_dir"), item.get("project"), item.get("run_id")) and os.path.isdir(item.get("run_dir") or ""):
            left.append(item)
    A._save_pending(left)
    night.record("archive", "ran", f"{copied} file(s) copied to {base}" + (f"; {len(left)} run(s) still waiting" if left else ""))
    return {"copied": copied, "pending_left": len(left), "where": base}


# ------------------------------------------------------------------------------------------------------------- step 6: the note
def _pct(v):
    return "-" if v is None else f"{v}%"


def morning_note(night, evals, dataset, logbook, lessons, archive, claude=None, date=None):
    date = date or time.strftime("%Y-%m-%d", time.localtime(night.t0))
    took = round((night.now() - night.t0) / 60)
    L = [f"# Morning note, {date}", "",
         f"The night started at {time.strftime('%H:%M', time.localtime(night.t0))} and took {took} min."
         + (" It was stopped by the stop file." if night.stopped else ""), ""]
    L += ["## What ran", "", "| Benchmark | Status | Passed | Pass rate | Not sure | Tokens | Minutes |", "|---|---|---|---|---|---|---|"]
    for r in evals:
        passed = f"{r.get('passed')}/{r.get('n')}" if r.get("n") is not None else "-"
        mins = round((r.get("secs") or 0) / 60) if r.get("secs") is not None else "-"
        L.append(f"| {r['name']} | {r['status']} | {passed} | {_pct(r.get('pass_rate'))} | {r.get('not_sure') if r.get('not_sure') is not None else '-'} "
                 f"| {r.get('tokens') if r.get('tokens') is not None else '-'} | {mins} |")
    for r in evals:
        ch = r.get("task_changes")
        if ch:
            L.append(f"\n{r['name']} against its previous run: now passing {', '.join(ch.get('won') or []) or 'nothing new'}; "
                     f"no longer passing {', '.join(ch.get('lost') or []) or 'nothing'}; {ch.get('same', 0)} unchanged.")
        if r.get("per_flow"):
            L.append(f"\n{r['name']} by flow: " + ", ".join(f"{k} {_pct(v)}" for k, v in sorted(r["per_flow"].items())))
    skipped = [r for r in evals if r["status"] == "skipped"]
    for r in skipped:
        L.append(f"\n{r['name']} was skipped: {r.get('detail') or ''}")
    L += ["", "## Training data", ""]
    if dataset:
        L.append(f"- {dataset['sft']} samples the checks accepted ({dataset['new_sft']} new since the last night"
                 + (", first night counted" if dataset.get("first_time") else "") + ")")
        L.append(f"- {dataset['dpo']} preference pairs ({dataset['new_dpo']} new), {dataset['distill']} cases a bigger model solved, "
                 f"{dataset['traj']} tool conversations")
        L.append(f"- left out: {dataset['duplicates']} near-duplicates, {dataset['low_quality']} under the quality floor, "
                 f"{dataset['secrets_skipped']} that looked like they held a credential, {dataset['too_long']} too long")
    else:
        L.append("- the export did not run (see Problems)")
    L += ["", "## Logbook and lessons", ""]
    if logbook:
        ing = logbook["ingest"]
        L.append(f"- logbook: {ing.get('added', 0)} new records, {ing.get('updated', 0)} updated; playbooks rewritten at {logbook['playbooks']}")
    if lessons:
        L += [f"- {x}" for x in lessons[:10]]
    elif lessons is not None:
        L.append("- the overseer found nothing new to say")
    L += ["", "## Archive", "", (f"- {archive['copied']} file(s) copied to {archive['where']}" if archive else
                                 "- nothing copied (no archive folder set, or the drive was not there)")]
    usd = (claude or {}).get("usd") or 0.0
    L += ["", "## Cost", "", f"- local models: $0 (they run on this machine)",
          f"- Claude: ${usd:.4f}" + ("" if claude else " (not used; run with --with-claude to ask it for a second opinion)")]
    if claude and claude.get("text"):
        L += ["", "## Claude's second opinion", "", claude["text"].strip()]
    L += ["", "## Problems", ""] + ([f"- {p}" for p in night.problems] or ["- none"])
    return "\n".join(L) + "\n"


def _spent_usd(cloud):
    try:
        return float((cloud.spent() or {}).get("usd") or 0.0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def claude_opinion(night, cfg, note, cap_usd=CLAUDE_USD, cloud=None):
    """One gated Claude call that reads the note and names what to look at first. -> {"text", "usd"} or None (refused or failed)."""
    cloud = cloud or __import__("arch_cloud")
    ok, why = cloud.allowed(cfg)
    if not ok:
        night.problem("--with-claude was asked for, but " + why)
        return None
    model = cloud.chosen_model(cfg)
    rough = cloud.cost(model, len(note) // 3 + 400, 600)
    if rough > cap_usd:
        night.problem(f"Claude was not asked: the note would cost about ${rough:.3f}, over the ${cap_usd:.2f} cap for one night")
        return None
    before = _spent_usd(cloud)
    msgs = [{"role": "system", "content": "You read a night of local-model benchmarks for a developer. Answer in at most six short bullet "
                                          "points: what to look at first, and why. Plain words; no praise."},
            {"role": "user", "content": note}]
    try:
        r = cloud.chat(model, msgs, options={"num_predict": 600}, cfg=cfg)
    except Exception as e:
        night.problem(f"Claude's second opinion failed: {type(e).__name__}: {e}")
        return None
    usd = max(0.0, _spent_usd(cloud) - before)            # 0 on a cache hit: the same note asked before costs nothing
    night.record("claude", "ran", f"about ${usd:.4f}")
    return {"text": (r or {}).get("text") or "", "usd": usd}


def write_note(text, date):
    os.makedirs(learning_dir(), exist_ok=True)
    p = os.path.join(learning_dir(), f"morning_{date}.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


# ------------------------------------------------------------------------------------------------------------- the night
def night(args):
    date = time.strftime("%Y-%m-%d")
    if os.path.isfile(pause_path()) and not args.dry_run:
        print(f"Paused: {pause_path()} exists, so tonight does nothing. Delete it to resume.")
        return 0
    if not args.dry_run:
        os.makedirs(learning_dir(), exist_ok=True)
    nt = Night(args.hours, log=None if args.dry_run else os.path.join(learning_dir(), f"overnight_{date}.log"))
    cfg = core.load_config()
    nt.say(f"Overnight run: up to {args.hours} h, stop with: type nul > \"{stop_path()}\"")
    fams = [f.strip() for f in (args.families or "").split(",") if f.strip()] or list(FAMILIES)
    if args.dry_run:
        evals = step_evals(nt, fams, quick=args.quick, python=args.python, dry=True)
    else:
        import arch_heavy                     # the whole night holds the heavy-job lock; the eval children inherit the hold
        arch_heavy.lower_priority()
        with arch_heavy.one_at_a_time("overnight", cfg, say=nt.say, wait_s=max(60, nt.left() - RESERVE_S)):
            evals = step_evals(nt, fams, quick=args.quick, python=args.python)
    dataset = logbook = lessons = archive = claude = None
    if args.dry_run:
        for s in ("dataset", "logbook", "lessons", "archive"):
            nt.record(s, "skipped", "dry run")
    elif nt.stop_asked():
        for s in ("dataset", "logbook", "lessons", "archive"):
            nt.record(s, "stopped", "the stop file was created")
    else:
        dataset = step_dataset(nt, min_quality=args.min_quality)
        logbook = step_logbook(nt, include_claude=True)
        lessons = step_lessons(nt)
        archive = step_archive(nt, cfg, extra_files=[(logbook or {}).get("playbooks")])
    text = morning_note(nt, evals, dataset, logbook, lessons, archive, date=date)
    if args.with_claude and not args.dry_run and not nt.stopped:
        claude = claude_opinion(nt, cfg, text, cap_usd=args.claude_usd)
        text = morning_note(nt, evals, dataset, logbook, lessons, archive, claude=claude, date=date)
    if args.dry_run:
        print("\n" + text)
        return 0
    p = write_note(text, date)
    nt.say(f"Morning note: {p}")
    try:
        if nt.stopped:
            os.remove(stop_path())            # our own control file: tomorrow's night must not stop on it
    except OSError:
        pass
    return 1 if any(r["status"] == "failed" for r in evals) else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="One unattended night of local benchmarks, export, logbook and a morning note.")
    ap.add_argument("--hours", type=float, default=7.0, help="time limit for the whole night (default 7)")
    ap.add_argument("--families", default=",".join(FAMILIES), help="comma-separated eval families before the representative set")
    ap.add_argument("--quick", action="store_true", help="only the first 10 tasks of each family")
    ap.add_argument("--min-quality", type=float, default=MIN_QUALITY, help="quality floor for exported samples, 0-1 (default 0.5)")
    ap.add_argument("--python", help="the Python that runs the evals (default: this one; overnight.bat passes one that has pytest)")
    ap.add_argument("--with-claude", action="store_true", help="also ask Claude for a short second opinion on the note (gated by "
                                                                "Settings > Online, capped by --claude-usd)")
    ap.add_argument("--claude-usd", type=float, default=CLAUDE_USD, help="the most --with-claude may spend (default 0.10)")
    ap.add_argument("--dry-run", action="store_true", help="say what would run; run nothing, write nothing")
    return night(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
