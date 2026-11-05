"""The real Claude Code agent (via the Claude Agent SDK), run by the hub, billed to YOUR API key -- never to a Claude subscription.

`claude -p` (headless Claude Code) runs in the project folder with:
  * ANTHROPIC_API_KEY from your environment, and
  * CLAUDE_CONFIG_DIR = <data>/claude_code, a settings folder with no subscription login in it,
so the API key is the only way it can sign in. (Measured 2026-11-04: with the normal settings folder it silently used the person's
Max subscription instead of the key.)
Every line it streams (`--output-format stream-json`) is kept, so the page shows the same things the Claude Code app does: its
messages, thinking, every tool call and result, cost and turns. A follow-up continues the same session (`--resume`).
Permission modes: plan (reads only), acceptEdits (edits files, no other risky tool), default (risky tools are refused in headless
mode). There is no "bypass" here on purpose.
"""
import json
import os
import shutil
import subprocess
import threading
import time

import arch_core as core

_RUNS, _LOCK = {}, threading.Lock()
MODES = ("plan", "acceptEdits", "default")


def binary():
    for name in ("claude.cmd", "claude", "claude.exe"):
        p = shutil.which(name)
        if p:
            return p
    p = os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd")
    return p if os.path.isfile(p) else None


def status():
    key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    try:
        import claude_agent_sdk  # noqa: F401
        b = True
    except ImportError:
        b = False
    return {"installed": bool(b), "key": key, "ready": bool(b and key),
            "why": "" if b and key else ("install the Claude Agent SDK: pip install claude-agent-sdk" if not b else "set ANTHROPIC_API_KEY and restart the hub")}


def _env():
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = os.path.join(core.data_dir(), "claude_code")
    os.makedirs(env["CLAUDE_CONFIG_DIR"], exist_ok=True)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)                     # the key, and only the key
    return env


def _summary(ev):
    t = ev.get("type")
    if t == "assistant":
        out = []
        for b in (ev.get("message") or {}).get("content") or []:
            if b.get("type") == "text":
                out.append(b.get("text", ""))
            elif b.get("type") == "thinking":
                out.append("(thinking) " + (b.get("thinking") or "")[:2000])
            elif b.get("type") == "tool_use":
                out.append(f"⚙ {b.get('name')} {json.dumps(b.get('input'), ensure_ascii=False)[:400]}")
        return "\n".join(x for x in out if x)
    if t == "user":
        for b in (ev.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                c = b.get("content")
                c = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                return ("✗ " if b.get("is_error") else "↳ ") + (c or "")[:800]
    if t == "result":
        return (ev.get("result") or "")[:4000]
    if t == "system" and ev.get("subtype") == "init":
        return f"session {ev.get('session_id')} · model {ev.get('model')} · {len(ev.get('tools') or [])} tools · mode {ev.get('permissionMode')}"
    return ""


def start(root, prompt, mode="plan", model=None, resume=None, max_turns=40):
    """Run one Claude Code turn in `root` in the background. -> run id (follow-ups pass the session id as `resume`)."""
    if not status()["ready"]:
        raise RuntimeError(status()["why"])
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("set ANTHROPIC_API_KEY, then restart the hub")
    if mode not in MODES:
        mode = "plan"
    args = {"prompt": prompt, "mode": mode, "model": model, "resume": resume, "max_turns": int(max_turns)}
    rid = f"cc{int(time.time() * 1000)}"
    with _LOCK:
        _RUNS[rid] = {"events": [], "status": "running", "session": resume, "cost": 0.0, "started": time.time(), "error": None, "proc": None}
    threading.Thread(target=_pump, args=(rid, args, root), daemon=True).start()
    return rid


def _pump(rid, args, root):
    """Runs Claude Code through the Claude Agent SDK (your API credit covers the Agent SDK; the plain `claude` command is
    'Claude Code' usage, which promotional credit does not cover -- seen 2026-11-04: 'Credit balance is too low')."""
    import asyncio
    import claude_agent_sdk as sdk

    def add(rec, session=None, cost=None, turns=None, error=None):
        with _LOCK:
            r = _RUNS[rid]
            if session:
                r["session"] = session
            if cost is not None:
                r["cost"] = cost
            if turns is not None:
                r["turns"] = turns
            if error:
                r["error"] = error
            if rec and rec.get("text"):
                r["events"].append(rec)

    async def go():
        env = {k: v for k, v in _env().items() if k in ("CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY")}
        opts = sdk.ClaudeAgentOptions(cwd=root, permission_mode=args["mode"], max_turns=args["max_turns"], env=env,
                                      model=args["model"] or None, resume=args["resume"] or None)
        async for m in sdk.query(prompt=args["prompt"], options=opts):
            name, t = type(m).__name__, time.time()
            if name == "SystemMessage":
                d = getattr(m, "data", {}) or {}
                if getattr(m, "subtype", "") == "init":
                    add({"t": t, "type": "system.init", "text": f"session {d.get('session_id')} · model {d.get('model')} · {len(d.get('tools') or [])} tools · mode {d.get('permissionMode')}"},
                        session=d.get("session_id"))
            elif name == "AssistantMessage":
                parts = []
                for b in m.content or []:
                    bn = type(b).__name__
                    if bn == "TextBlock":
                        parts.append(b.text)
                    elif bn == "ThinkingBlock" and getattr(b, "thinking", ""):
                        parts.append("(thinking) " + b.thinking[:2000])
                    elif bn == "ToolUseBlock":
                        parts.append(f"⚙ {b.name} {json.dumps(b.input, ensure_ascii=False)[:400]}")
                u = getattr(m, "usage", None) or {}
                add({"t": t, "type": "assistant", "text": "\n".join(x for x in parts if x), "model": getattr(m, "model", None),
                     "usage": {k: u.get(k) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}})
            elif name == "UserMessage":
                for b in (m.content if isinstance(m.content, list) else []):
                    if type(b).__name__ == "ToolResultBlock":
                        c = b.content if isinstance(b.content, str) else json.dumps(b.content, ensure_ascii=False, default=str)
                        add({"t": t, "type": "user", "text": ("✗ " if b.is_error else "↳ ") + (c or "")[:800]})
            elif name == "ResultMessage":
                add({"t": t, "type": "result", "text": (m.result or "")[:4000]}, session=m.session_id, cost=m.total_cost_usd or 0,
                    turns=m.num_turns, error=(m.result or m.subtype) if m.is_error else None)

    try:
        asyncio.run(go())
        with _LOCK:
            _RUNS[rid]["status"] = "error" if _RUNS[rid]["error"] else "done"
    except Exception as e:
        with _LOCK:
            _RUNS[rid]["status"], _RUNS[rid]["error"] = "error", f"{type(e).__name__}: {str(e)[:500]}"


def stop(rid):
    with _LOCK:
        if rid in _RUNS and _RUNS[rid]["status"] == "running":
            _RUNS[rid]["status"], _RUNS[rid]["error"] = "stopped", "stopped from the page (the current tool call may still finish)"


def snapshot(rid, since=0):
    with _LOCK:
        r = _RUNS.get(rid)
        if not r:
            return {"error": "no such run"}
        return {k: r[k] for k in ("status", "session", "cost", "error")} | {"events": r["events"][since:], "n": len(r["events"]), "turns": r.get("turns")}
