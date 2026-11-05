"""Your Claude Managed Agents, run from the hub: list them, start a session, watch every event live.

A Managed Agent (made in the Claude Console, e.g. "Local-Dev Architect Agent") runs on Anthropic's side, in its own sandbox. This module
is the hub's window onto it: it starts a session with your agent, optionally mounts your GitHub repository so the agent can read and
change the code, sends your message, and keeps every event the session streams (the agent's messages, its tool calls, status changes)
so the page can show them as they happen. Billed to your API key; needs online features on (same switch as every Claude call).

Tool calls that pause for a decision are answered "deny" unless you approved them on the page: nobody approves a write by default.
"""
import os
import threading
import time

import arch_cloud

_RUNS = {}            # session id -> {"events": [...], "status", "agent", "started", "error"}
_LOCK = threading.Lock()


def _client():
    import arch_core as core
    if not (core.load_config().get("online") or {}).get("enabled") or arch_cloud._testing():
        raise RuntimeError("turn on Settings > Online > Allow online features first")
    import anthropic
    return anthropic.Anthropic()


def _plain(x, depth=0):
    """An SDK object -> plain JSON (for the page)."""
    if depth > 6:
        return str(x)[:200]
    if hasattr(x, "to_dict"):
        try:
            return x.to_dict()
        except Exception:
            pass
    if isinstance(x, dict):
        return {k: _plain(v, depth + 1) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_plain(v, depth + 1) for v in x]
    if isinstance(x, (str, int, float, bool)) or x is None:
        return x
    return {k: _plain(v, depth + 1) for k, v in vars(x).items() if not k.startswith("_")} if hasattr(x, "__dict__") else str(x)


def _model_name(m):
    """'claude-opus-5-5 · medium effort' from the SDK's model config object (or a plain string)."""
    if isinstance(m, str) or m is None:
        return m or ""
    mid, eff = getattr(m, "id", "") or "", getattr(getattr(m, "effort", None), "type", None)
    return mid + (f" · {eff} effort" if eff else "")


def listing():
    """-> {"agents": [{id, name, model, version, description}], "environments": [{id, name}]}"""
    c = _client()
    agents = [{"id": a.id, "name": getattr(a, "name", a.id), "model": _model_name(getattr(a, "model", None)),
               "version": getattr(a, "version", None), "description": (getattr(a, "description", "") or "")[:300]}
              for a in c.beta.agents.list()]
    envs = [{"id": e.id, "name": getattr(e, "name", e.id)} for e in c.beta.environments.list()]
    return {"agents": agents, "environments": envs}


def _summary(ev):
    """One line a person can read, from one session event."""
    t = getattr(ev, "type", "?")
    if t == "agent.message":
        return "".join(getattr(b, "text", "") for b in (getattr(ev, "content", None) or []) if getattr(b, "type", "") == "text")
    if t in ("agent.tool_use", "agent.mcp_tool_use", "agent.custom_tool_use"):
        return f"{getattr(ev, 'name', 'tool')}  {str(_plain(getattr(ev, 'input', '')))[:300]}"
    if t.startswith("session.status"):
        sr = getattr(ev, "stop_reason", None)
        return t.split(".")[-1] + (f" ({getattr(sr, 'type', '')})" if sr else "")
    return ""


def start(agent_id, message, environment_id=None, repo_url=None):
    """Start a session with your agent and send the first message. Runs in the background. -> session id."""
    c = _client()
    kw = {"agent": agent_id}
    if environment_id:
        kw["environment_id"] = environment_id
    if repo_url:
        res = {"type": "github_repository", "url": repo_url, "mount_path": "/workspace/repo", "checkout": {"type": "branch", "name": "main"}}
        if os.environ.get("GITHUB_TOKEN"):
            res["authorization_token"] = os.environ["GITHUB_TOKEN"]
        kw["resources"] = [res]
        message = message + "\n\n(The project is mounted at /workspace/repo.)"
    s = c.beta.sessions.create(**kw)
    with _LOCK:
        _RUNS[s.id] = {"events": [], "status": "starting", "agent": agent_id, "started": time.time(), "error": None, "approve": set()}
    threading.Thread(target=_pump, args=(c, s.id, message), daemon=True).start()
    return s.id


def send(session_id, message):
    """A follow-up message to a session that is waiting for you."""
    c = _client()
    c.beta.sessions.events.send(session_id=session_id, events=[{"type": "user.message", "content": [{"type": "text", "text": message}]}])
    with _LOCK:
        r = _RUNS.get(session_id)
        if r:
            r["events"].append({"t": time.time(), "type": "user.message", "text": message})
            if r["status"] in ("idle", "done"):
                r["status"] = "running"
                threading.Thread(target=_pump, args=(c, session_id, None), daemon=True).start()


def _pump(c, sid, first_message):
    try:
        with c.beta.sessions.events.stream(session_id=sid) as stream:          # stream first, then send (never miss an event)
            if first_message:
                c.beta.sessions.events.send(session_id=sid, events=[{"type": "user.message", "content": [{"type": "text", "text": first_message}]}])
                with _LOCK:
                    _RUNS[sid]["events"].append({"t": time.time(), "type": "user.message", "text": first_message})
            with _LOCK:
                _RUNS[sid]["status"] = "running"
            for ev in stream:
                t = getattr(ev, "type", "?")
                if t == "user.message":                              # the stream echoes our own message: already recorded
                    continue
                rec = {"t": time.time(), "type": t, "text": _summary(ev)[:6000], "raw": _plain(ev)}
                with _LOCK:
                    _RUNS[sid]["events"].append(rec)
                if t in ("agent.tool_use", "agent.mcp_tool_use") and getattr(ev, "evaluated_permission", None) == "ask":
                    ok = getattr(ev, "id", None) in _RUNS[sid]["approve"]
                    c.beta.sessions.events.send(session_id=sid, events=[{"type": "user.tool_confirmation", "tool_use_id": ev.id,
                                                                         "result": "allow" if ok else "deny"}])
                    with _LOCK:
                        _RUNS[sid]["events"].append({"t": time.time(), "type": "hub.decision", "text": ("allowed" if ok else "denied (not approved on the page)") + f": {getattr(ev, 'name', '')}"})
                if t == "session.status_idle":
                    sr = getattr(ev, "stop_reason", None)
                    if getattr(sr, "type", "") != "requires_action":
                        with _LOCK:
                            _RUNS[sid]["status"] = "idle"
                        break
                if t == "session.status_terminated":
                    with _LOCK:
                        _RUNS[sid]["status"] = "done"
                    break
    except Exception as e:
        with _LOCK:
            _RUNS[sid]["status"] = "error"
            _RUNS[sid]["error"] = f"{type(e).__name__}: {str(e)[:400]}"
    finally:
        try:
            _log_finished(sid)
        except Exception:
            pass


# ---- The night shift: crew runs on a clock -------------------------------------------------------------------------------------------
import datetime
import json
import re

_SLOCK = threading.RLock()
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{3,100}$")
_AT_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_REPO_RE = re.compile(r"^https://[A-Za-z0-9.\-]+/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+?(\.git)?/?$")
_REPEATS = ("daily", "once")
_CREW_SIDS = set()          # sessions the clock started (only these are written to the crew log)
_SCHED_THREAD = None


def _schedule_path():
    import arch_core as core
    return os.path.join(core.data_dir(), "crew_schedule.json")


def _log_path():
    import arch_core as core
    return os.path.join(core.data_dir(), "crew_log.jsonl")


def _read_schedule():
    try:
        with open(_schedule_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _write_schedule(items):
    p = _schedule_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=1)
    os.replace(tmp, p)


def schedule_add(agent_id, message, at="02:00", repeat="daily", environment_id=None, repo_url=None):
    """Put a crew run on the clock. -> id. Raises ValueError on a bad input."""
    if not isinstance(agent_id, str) or not _ID_RE.match(agent_id):
        raise ValueError("agent id looks wrong (letters, digits, - and _ only)")
    if not isinstance(message, str) or not message.strip() or len(message) > 8000:
        raise ValueError("the message must be 1-8000 characters")
    if not isinstance(at, str) or not _AT_RE.match(at):
        raise ValueError("time must be HH:MM (24 hours)")
    if repeat not in _REPEATS:
        raise ValueError("repeat must be 'daily' or 'once'")
    if environment_id and (not isinstance(environment_id, str) or not _ID_RE.match(environment_id)):
        raise ValueError("environment id looks wrong")
    if repo_url and (not isinstance(repo_url, str) or not _REPO_RE.match(repo_url)):
        raise ValueError("repository must be an https:// URL like https://github.com/you/project")
    sid = "crew_" + os.urandom(4).hex()
    entry = {"id": sid, "agent_id": agent_id, "message": message.strip(), "at": at, "repeat": repeat,
             "environment_id": environment_id or None, "repo_url": repo_url or None,
             "last_run": None, "last_session": None, "last_status": None,
             "created": time.time(), "last_slot": None}
    with _SLOCK:
        items = _read_schedule()
        items.append(entry)
        _write_schedule(items)
    return sid


def schedule_list():
    keys = ("id", "agent_id", "message", "at", "repeat", "environment_id", "repo_url", "last_run", "last_session", "last_status")
    with _SLOCK:
        return [{k: x.get(k) for k in keys} for x in _read_schedule()]


def schedule_remove(sid):
    """-> True when something was removed."""
    with _SLOCK:
        items = _read_schedule()
        keep = [x for x in items if x.get("id") != sid]
        if len(keep) == len(items):
            return False
        _write_schedule(keep)
        return True


def _latest_slot(at, now):
    """The most recent moment today-or-yesterday that the clock read `at`, at or before `now`."""
    h, m = int(at[:2]), int(at[3:])
    slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return slot if slot <= now else slot - datetime.timedelta(days=1)


def tick(now=None, starter=None):
    """Start every entry whose time has come and that has not run for that slot yet. -> list of started session ids.
    `now` is a datetime (default: local time); `starter(agent_id, message, environment_id, repo_url) -> session id` is for tests."""
    if starter is None:
        if arch_cloud._testing():
            return []
        starter = start
    now = now or datetime.datetime.now()
    started = []
    with _SLOCK:
        items = _read_schedule()
        keep, changed = [], False
        for x in items:
            try:
                slot = _latest_slot(x["at"], now)
                key = slot.strftime("%Y-%m-%d %H:%M")
                created = datetime.datetime.fromtimestamp(x.get("created") or 0)
            except (KeyError, ValueError, TypeError, OSError):
                keep.append(x)
                continue
            if slot <= created or x.get("last_slot") == key:      # not yet this slot (or added after it) / already ran for it
                keep.append(x)
                continue
            changed = True
            x["last_slot"], x["last_run"] = key, now.strftime("%Y-%m-%d %H:%M:%S")
            try:
                sid = starter(x["agent_id"], x["message"], x.get("environment_id"), x.get("repo_url"))
            except Exception as e:
                x["last_session"], x["last_status"] = None, f"could not start: {type(e).__name__}: {str(e)[:200]}"
                keep.append(x)
                continue
            x["last_session"], x["last_status"] = sid, "running"
            _CREW_SIDS.add(sid)
            started.append(sid)
            if x.get("repeat") != "once":
                keep.append(x)
        if changed:
            _write_schedule(keep)
    return started


def _log_finished(sid):
    """Append the final status and a short summary of the agent's last message to crew_log.jsonl (crew sessions only)."""
    if sid not in _CREW_SIDS:
        return
    with _LOCK:
        r = _RUNS.get(sid)
        if not r or r["status"] not in ("idle", "done", "error"):
            return
        status, err, agent, started = r["status"], r.get("error"), r.get("agent"), r.get("started")
        last = next((e.get("text") for e in reversed(r["events"]) if e.get("type") == "agent.message" and e.get("text")), "")
    line = {"t": time.time(), "session": sid, "agent": agent, "status": status, "started": started,
            "summary": (err if status == "error" and err else last or "")[:600]}
    with _SLOCK:
        p = _log_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
        items = _read_schedule()
        for x in items:
            if x.get("last_session") == sid:
                x["last_status"] = status
                _write_schedule(items)
                break
    _CREW_SIDS.discard(sid)


def crew_log(limit=50):
    """The newest log lines first, for the morning view."""
    try:
        with open(_log_path(), "r", encoding="utf-8") as f:
            rows = [json.loads(s) for s in f if s.strip()]
    except (OSError, ValueError):
        return []
    return rows[-limit:][::-1]


def start_scheduler():
    """One daemon thread calling tick() every 60 s. A second call does nothing."""
    global _SCHED_THREAD
    with _SLOCK:
        if _SCHED_THREAD is not None and _SCHED_THREAD.is_alive():
            return False

        def loop():
            while True:
                try:
                    tick()
                except Exception:
                    pass
                time.sleep(60)
        _SCHED_THREAD = threading.Thread(target=loop, name="crew-clock", daemon=True)
        _SCHED_THREAD.start()
        return True


def snapshot(session_id, since=0):
    with _LOCK:
        r = _RUNS.get(session_id)
        if not r:
            return {"error": "no such session in this hub"}
        return {"status": r["status"], "error": r["error"], "events": r["events"][since:], "n": len(r["events"]),
                "trace": f"https://platform.claude.com/workspaces/default/sessions/{session_id}"}


def sessions():
    with _LOCK:
        return [{"id": k, "agent": v["agent"], "status": v["status"], "started": v["started"], "events": len(v["events"])} for k, v in _RUNS.items()]
