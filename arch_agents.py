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
