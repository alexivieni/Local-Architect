"""Resume an interrupted run. Every run writes `.arch/runs/<id>/run.json` (status running -> done/stopped/incomplete). A run whose status is still
"running" but whose process is gone crashed (power cut, closed window, killed process). `resume` starts the same flow again with the same goal:
the build flow continues from its saved plan and skips the sprints that already passed; the other flows start over with the handoff and the files
already written still in place (a restart, not a mid-step resume: they are short).
"""
import json
import os
import time

import arch_core as core


def _alive(pid):
    try:
        import arch_runner
        return bool(pid) and arch_runner.is_running(int(pid))
    except (ImportError, ValueError, OSError):
        return False


def list_runs(root):
    base = os.path.join(os.path.abspath(root), ".arch", "runs")
    out = []
    if not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base)):
        st = core.load_json(os.path.join(base, name, "run.json"))
        if not st:
            continue
        status = st.get("status")
        if status == "running" and not _alive(st.get("pid")):
            status = "crashed"
        out.append(dict(st, status=status, dir=os.path.join(base, name)))
    return out


def resumable(root):
    """Newest run that did not finish and has not been resumed already, or None."""
    for st in reversed(list_runs(root)):
        if st["status"] in ("crashed", "stopped", "incomplete") and not st.get("resumed_by"):
            return st
    return None


def mark_resumed(st, new_id):
    p = os.path.join(st["dir"], "run.json")
    d = core.load_json(p)
    d["resumed_by"], d["resumed_at"] = new_id, time.time()
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)


def resume_params(st):
    """Parameters for the new run: the saved ones plus what the build flow needs to skip finished sprints."""
    p = dict(st.get("params") or {})
    cp = st.get("checkpoints") or {}
    if cp.get("plan"):
        p["resume"] = {"from": st["run_id"], "plan": cp["plan"], "done": cp.get("sprints_done") or [], "files": cp.get("files") or [], "interface": cp.get("interface") or []}
    if cp.get("steps_done"):                  # a flow defined as data: the steps that already passed are not run (or paid for) again
        p.setdefault("resume", {"from": st["run_id"]})
        p["resume"].update(steps_done=cp["steps_done"], step_outputs=cp.get("step_outputs") or {})
    return p
