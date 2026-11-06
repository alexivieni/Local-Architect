"""Permission modes for a run (setting `run.permission_mode`): plan | ask | auto (default auto = today's behaviour).

  plan  nothing is written: write tools are removed from the tool list and refused if called, and Engine.apply refuses every change.
  ask   every change pauses for approval. The request is a file the hub can answer:
            <data_dir>/approvals/<run_id>/<id>.json          {id, run, rel, diff, status: "pending", t, expires}
            <data_dir>/approvals/<run_id>/<id>.answer.json   {allow: true|false, by}   (write it, or call answer())
        No answer before `run.permission_timeout_s` (default 120) means reject. Nothing is ever approved by waiting.
  auto  the Engine's own review mode, protected paths and undo, unchanged.

    mode(cfg, run)           -> "plan" | "ask" | "auto"   (run may be an Engine; an unknown value counts as "ask", the careful side)
    gate_apply(e, change)    -> ("allow"|"deny"|"wait", reason, request_id)   never blocks; "wait" = a request is pending
    wait(e, request_id)      -> ("allow"|"deny", reason)  blocks until answered or timed out
    decide(e, change)        -> ("allow"|"deny", reason)  gate_apply + wait; what Engine.apply calls
    tools_allowed(e, names)  -> the tool names with the writing ones removed in plan mode
    pending(data_dir=None)   -> open requests for the hub;  answer(run_id, req_id, allow, by="hub")
"""
import json
import os
import time

MODES = ("plan", "ask", "auto")
DEFAULT_TIMEOUT_S = 120


def _cfg_of(x):
    return x.get("cfg") if isinstance(x, dict) and "cfg" in x else getattr(x, "cfg", None)


def mode(cfg, run=None):
    """The run's mode: an explicit attribute on the run (`permission_mode`) wins over cfg['run']['permission_mode']; default auto."""
    raw = run.get("permission_mode") if isinstance(run, dict) else getattr(run, "permission_mode", None)
    if raw in (None, ""):
        raw = ((cfg or {}).get("run") or {}).get("permission_mode", "auto")
    raw = str(raw).strip().lower()
    return raw if raw in MODES else "ask"


def timeout_s(cfg):
    try:
        return max(0.0, float(((cfg or {}).get("run") or {}).get("permission_timeout_s", DEFAULT_TIMEOUT_S)))
    except (TypeError, ValueError):
        return float(DEFAULT_TIMEOUT_S)


def _dir(e, run_id=None):
    import arch_core as core
    return os.path.join(core.data_dir(), "approvals", str(run_id or getattr(e, "run_id", "") or "run"))


def tools_allowed(e, names):
    """Plan mode: no writing tool. Other modes: the list as it is."""
    if mode(_cfg_of(e), e) != "plan":
        return list(names)
    import arch_tools
    return [n for n in names if not (arch_tools.TOOLS.get(n) or {}).get("writes")]


def tool_refusal(e, name):
    """'' when the tool may run now, else the reason (plan mode and a writing tool)."""
    import arch_tools
    if mode(_cfg_of(e), e) == "plan" and (arch_tools.TOOLS.get(name) or {}).get("writes"):
        return f"'{name}' changes files and this run is in plan mode (read-only). Look, then describe the change in your answer."
    return ""


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + f".tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def gate_apply(e, change):
    """change = the Engine's prepared block ({rel, diff, ...}) or {"rel": ...}. -> (decision, reason, request_id)."""
    m = mode(_cfg_of(e), e)
    rel = str((change or {}).get("rel") or "")
    if m == "auto":
        return "allow", "", None
    if m == "plan":
        return "deny", f"plan mode: {rel or 'this change'} was not written (the run may only read)", None
    d = _dir(e)
    rid = f"{int(time.time() * 1000):013d}_{abs(hash(rel)) % 10000:04d}"
    n = 0
    while os.path.exists(os.path.join(d, rid + ".json")):
        n += 1
        rid = f"{rid.split('-')[0]}-{n}"
    now = time.time()
    _write_json(os.path.join(d, rid + ".json"),
                {"id": rid, "run": str(getattr(e, "run_id", "") or "run"), "rel": rel, "diff": str((change or {}).get("diff") or "")[:6000],
                 "existed": (change or {}).get("existed"), "status": "pending", "t": now, "expires": now + timeout_s(_cfg_of(e))})
    try:
        e.say(f"[?] waiting for approval: {rel} (answer in the hub; no answer in {int(timeout_s(_cfg_of(e)))} s = rejected)")
    except Exception:
        pass
    return "wait", f"waiting for approval of {rel}", rid


def _answer_path(e, rid):
    return os.path.join(_dir(e), rid + ".answer.json")


def _read_answer(e, rid):
    try:
        with open(_answer_path(e, rid), encoding="utf-8") as f:
            a = json.load(f)
        return bool(a.get("allow")) if isinstance(a, dict) and "allow" in a else None
    except (OSError, ValueError):
        return None


def _close(e, rid, status):
    p = os.path.join(_dir(e), rid + ".json")
    try:
        with open(p, encoding="utf-8") as f:
            rec = json.load(f)
        rec.update(status=status, closed=time.time())
        _write_json(p, rec)
    except (OSError, ValueError):
        pass


def wait(e, rid, poll=0.1):
    t_end = time.time() + timeout_s(_cfg_of(e))
    while True:
        a = _read_answer(e, rid)
        if a is not None:
            _close(e, rid, "approved" if a else "rejected")
            return ("allow", "approved") if a else ("deny", "the person rejected this change")
        if time.time() >= t_end:
            _close(e, rid, "timed_out")
            return "deny", "no answer in time: the change was rejected"
        stop = getattr(e, "stop_requested", None)
        if callable(stop) and stop():
            _close(e, rid, "rejected")
            return "deny", "the run was stopped"
        time.sleep(poll)


def decide(e, change, poll=0.1):
    d, why, rid = gate_apply(e, change)
    if d == "wait":
        return wait(e, rid, poll)
    return d, why


def pending(data_dir=None):
    import arch_core as core
    root = os.path.join(data_dir or core.data_dir(), "approvals")
    out = []
    if not os.path.isdir(root):
        return out
    now = time.time()
    for run in sorted(os.listdir(root)):
        rd = os.path.join(root, run)
        for f in sorted(os.listdir(rd) if os.path.isdir(rd) else []):
            if f.endswith(".json") and not f.endswith(".answer.json") and ".tmp" not in f:
                try:
                    with open(os.path.join(rd, f), encoding="utf-8") as fh:
                        rec = json.load(fh)
                except (OSError, ValueError):
                    continue
                if rec.get("status") == "pending" and rec.get("expires", 0) > now and not os.path.exists(os.path.join(rd, rec["id"] + ".answer.json")):
                    out.append(rec)
    return out


def answer(run_id, req_id, allow, by="hub", data_dir=None):
    import arch_core as core
    p = os.path.join(data_dir or core.data_dir(), "approvals", str(run_id), str(req_id) + ".answer.json")
    if not os.path.isfile(p[:-len(".answer.json")] + ".json"):
        return False
    _write_json(p, {"allow": bool(allow), "by": by, "t": time.time()})
    return True
