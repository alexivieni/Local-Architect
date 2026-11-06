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
Every run is also kept on disk (<data>/cc_runs/<id>.jsonl, one event per line, + <id>.meta.json): `list_runs`, `load_run`.
"""
import json
import os
import re
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


# ---- subagents (like Claude Code's /agents): <data_dir>/subagents.json, passed to every run as ClaudeAgentOptions(agents=...)
NAME_RE = re.compile(r"[a-z][a-z0-9-]{1,39}")
TOOL_RE = re.compile(r"[A-Za-z][A-Za-z0-9_*.:()/ -]{0,79}")
SUB_MODELS = ("", "inherit", "opus", "sonnet", "haiku", "fable")
MAX_SUBAGENTS = 20
STARTERS = [
    {"name": "code-reviewer", "model": "inherit", "tools": ["Read", "Grep", "Glob"],
     "description": "Reviews a change or a file for correctness bugs, risky edge cases and unclear code. Read-only: never edits. "
                    "Use after code was written or when asked for a review.",
     "prompt": "You are a careful senior code reviewer. Read the code you are pointed at (and what it calls) before judging. "
               "Report only real problems: a concrete input or state that gives a wrong result, a crash, a security hole, or a "
               "broken contract. For each: file:line, what goes wrong, and the smallest fix. Rank by severity. If nothing is "
               "wrong, say so plainly. You cannot and must not edit files."},
    {"name": "test-writer", "model": "inherit", "tools": ["Read", "Grep", "Glob", "Write", "Edit", "Bash"],
     "description": "Writes focused tests for a function or a behaviour, runs them, and keeps only tests that pass for the right "
                    "reason. Use when a change needs tests or a bug needs a reproduction.",
     "prompt": "You write tests. First read the code under test and the project's existing tests, and follow their framework, "
               "layout and naming. Write small tests that each check one behaviour, including edge cases and the failure path. "
               "Run them. A test that fails because the code is wrong is reported, not weakened; a test that fails because the "
               "test is wrong is fixed. Never edit the code under test unless asked."},
]


def _subagents_file():
    return os.path.join(core.data_dir(), "subagents.json")


def validate_subagent(a):
    """One subagent from the page -> a clean record, or ValueError in plain words."""
    if not isinstance(a, dict):
        raise ValueError("a subagent must be an object")
    name = str(a.get("name") or "").strip()
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"subagent name {name[:40]!r}: use 2-40 lowercase letters, digits and dashes, starting with a letter")
    desc, prompt = str(a.get("description") or "").strip(), str(a.get("prompt") or "").strip()
    if not desc or len(desc) > 1000:
        raise ValueError(f"{name}: a description (when to use it, at most 1000 characters) is required")
    if not prompt or len(prompt) > 20000:
        raise ValueError(f"{name}: a system prompt (at most 20000 characters) is required")
    tools = a.get("tools") or []
    if isinstance(tools, str):
        tools = tools.split(",")
    tools = [str(t).strip() for t in tools if str(t).strip()]
    bad = [t for t in tools if not TOOL_RE.fullmatch(t)]
    if bad or len(tools) > 40:
        raise ValueError(f"{name}: tool names look wrong: {', '.join(bad[:3]) or 'too many'}")
    model = str(a.get("model") or "").strip()
    if model not in SUB_MODELS:
        raise ValueError(f"{name}: model must be one of inherit, opus, sonnet, haiku, fable")
    return {"name": name, "description": desc, "prompt": prompt, "tools": tools, "model": model or "inherit",
            "enabled": a.get("enabled", True) is not False}


def load_subagents():
    """The saved subagents; the two starters when nothing was saved yet (an unreadable file also gives the starters)."""
    try:
        with open(_subagents_file(), encoding="utf-8") as f:
            data = json.load(f)
        return [validate_subagent(a) for a in data.get("subagents", [])]
    except (OSError, ValueError, AttributeError):
        return [validate_subagent(a) for a in STARTERS]


def save_subagents(items):
    """Validate every subagent (names unique), then write the file atomically. -> the saved list."""
    if not isinstance(items, list) or len(items) > MAX_SUBAGENTS:
        raise ValueError(f"send a list of at most {MAX_SUBAGENTS} subagents")
    clean = [validate_subagent(a) for a in items]
    names = [a["name"] for a in clean]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise ValueError("two subagents share the name " + dup[0])
    path = _subagents_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"subagents": clean}, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return clean


def agent_definitions(sdk, items=None):
    """Enabled subagents -> {name: sdk.AgentDefinition}, passing only the fields this SDK version knows."""
    import dataclasses
    try:
        known = {f.name for f in dataclasses.fields(sdk.AgentDefinition)}
    except TypeError:
        known = {"description", "prompt", "tools", "model"}
    out = {}
    for a in (load_subagents() if items is None else items):
        if not a.get("enabled", True):
            continue
        kw = {"description": a["description"], "prompt": a["prompt"], "tools": a["tools"] or None,
              "model": a["model"] or "inherit"}
        out[a["name"]] = sdk.AgentDefinition(**{k: v for k, v in kw.items() if k in known})
    return out


def _main_arg(name, inp):
    """The one argument that says what a tool call does (Claude Code shows `Read(path)`, `Bash(command)`)."""
    if not isinstance(inp, dict):
        return ""
    for k in ("command", "file_path", "path", "pattern", "url", "query", "description", "subagent_type", "prompt"):
        if inp.get(k):
            return str(inp[k])[:200]
    return json.dumps(inp, ensure_ascii=False)[:200]


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


# ---- the run log on disk: <data_dir>/cc_runs/<run id>.jsonl (one event per line, appended as it arrives) + <run id>.meta.json.
# Memory stays the live source for the page; the disk copy is what survives a hub restart. A disk problem never stops a run.
RUN_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_DISK = threading.Lock()


def runs_dir():
    return os.path.join(core.data_dir(), "cc_runs")


def _run_path(rid, ext):
    if not RUN_ID_RE.fullmatch(str(rid or "")):
        raise ValueError(f"not a run id: {str(rid)[:40]!r}")
    return os.path.join(runs_dir(), str(rid) + ext)


def record_event(rid, rec):
    """Append one event to the run's log. -> True when it reached the disk."""
    try:
        path = _run_path(rid, ".jsonl")
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with _DISK:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return True
    except (OSError, ValueError, TypeError):
        return False


def _read_meta(path):
    try:
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def write_meta(rid, **fields):
    """Merge `fields` into the run's meta file (written whole, then swapped in). -> the merged meta ({} when it could not be written)."""
    try:
        path = _run_path(rid, ".meta.json")
        with _DISK:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            meta = _read_meta(path)
            meta.update(fields)
            meta["id"] = rid
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=1, ensure_ascii=False, default=str)
            os.replace(tmp, path)
        return meta
    except (OSError, ValueError, TypeError):
        return {}


def load_run(rid):
    """One run from disk: its meta plus every event, in order. A torn last line (a crash mid-write) is skipped."""
    try:
        meta_path, log_path = _run_path(rid, ".meta.json"), _run_path(rid, ".jsonl")
    except ValueError as e:
        return {"error": str(e)}
    with _DISK:
        meta = _read_meta(meta_path)
        events = []
        try:
            with open(log_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(ev, dict):
                        events.append(ev)
        except OSError:
            if not meta:
                return {"error": "no such run"}
    with _LOCK:
        live = rid in _RUNS
    return dict(meta, id=rid, events=events, n=len(events), live=live)


def list_runs(limit=50):
    """The newest runs first (meta only, no events)."""
    try:
        names = [n for n in os.listdir(runs_dir()) if n.endswith(".meta.json")]
    except OSError:
        return []
    out = []
    for n in names:
        meta = _read_meta(os.path.join(runs_dir(), n))
        if meta:
            meta.setdefault("id", n[:-len(".meta.json")])
            out.append(meta)
    out.sort(key=lambda m: m.get("started") or 0, reverse=True)
    return out[:max(0, int(limit))]


def _new_run_id():
    """cc<milliseconds>, moved on by one while taken (a fleet starts many runs in the same millisecond). Call under _LOCK."""
    ms = int(time.time() * 1000)
    while f"cc{ms}" in _RUNS or os.path.exists(_run_path(f"cc{ms}", ".meta.json")):
        ms += 1
    return f"cc{ms}"


WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def owns(root, files, path):
    """Is `path` one of the agent's own files? Globs and folders allowed ('docs/*.md', 'agent_templates/'). No list = no rule."""
    import fnmatch
    if not files:
        return True
    rel = os.path.relpath(os.path.abspath(os.path.join(root, path)), os.path.abspath(root)).replace("\\", "/")
    for f in files:
        f = str(f).replace("\\", "/").strip().lstrip("./").replace("NEW ", "").strip()
        if rel == f or fnmatch.fnmatch(rel, f) or (f.endswith("/") and rel.startswith(f)):
            return True
    return False


def _ownership_hooks(sdk, root, files):
    """A PreToolUse hook: an Edit/Write to a file the agent does not own is denied before it happens, with the reason (the brief
    names the files; this makes the rule hold even when the agent forgets it). No files given = no hook."""
    if not files:
        return None

    async def guard(inp, tool_use_id, context):
        ti = (inp or {}).get("tool_input") or {}
        path = ti.get("file_path") or ti.get("notebook_path") or ti.get("path") or ""
        if path and not owns(root, files, path):
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": f"{path} is not one of your files ({', '.join(files)}). "
                                                                       "Propose the change in your report instead."}}
        return {}

    return {"PreToolUse": [sdk.HookMatcher(matcher="|".join(WRITE_TOOLS), hooks=[guard])]}


def start(root, prompt, mode="plan", model=None, resume=None, max_turns=40, budget=5.0, tag=None, files=None):
    """Run one Claude Code turn in `root` in the background. -> run id (follow-ups pass the session id as `resume`).
    `tag` is free text kept in the run's meta (a fleet puts its id there).
    Gated like every Claude call (arch_cloud.allowed: never in the self-tests or eval, only with online features on), and the
    run's whole budget is held against the daily cap (arch_cloud.reserve) before it starts; what it really cost is recorded at the end."""
    import arch_cloud
    cfg = core.load_config()
    with arch_cloud.asked_for():                 # the person started this run by hand: online features must be on, not Use Claude
        ok, why = arch_cloud.allowed(cfg)
    if not ok:
        raise RuntimeError(why)
    if not status()["ready"]:
        raise RuntimeError(status()["why"])
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("set ANTHROPIC_API_KEY, then restart the hub")
    if mode not in MODES:
        mode = "plan"
    try:
        budget = float(budget)
    except (TypeError, ValueError):
        raise RuntimeError(f"the budget must be a number of dollars (got {str(budget)[:20]!r})")
    if not budget > 0:
        raise RuntimeError("the budget must be above $0 (it is the run's hard spend limit)")
    use = "agent" if tag else "session"                      # a fleet's agent is short-lived; a session you resume lives on
    ttl = arch_cloud.choose_ttl(cfg, use)
    if not model and mode == "plan":           # a read-only run with no model named: Sonnet (online.readonly_model)
        model = arch_cloud.readonly_model(cfg, None, True)[0] or model
    hold = arch_cloud.reserve(cfg, budget, what="This Claude Code run")   # SpendRefused (a RuntimeError) past the daily cap
    args = {"prompt": prompt, "mode": mode, "model": model, "resume": resume, "max_turns": int(max_turns), "budget": budget, "ttl": ttl,
            "files": [str(f) for f in (files or []) if str(f).strip()]}
    with _LOCK:
        rid = _new_run_id()
        started = time.time()
        _RUNS[rid] = {"events": [], "status": "running", "session": resume, "cost": 0.0, "started": started, "error": None, "proc": None,
                      "hold": hold, "usage_usd": 0.0, "ttl": ttl}
    write_meta(rid, prompt=prompt, mode=mode, model=model, budget=args["budget"], max_turns=args["max_turns"], root=os.path.abspath(root),
               resume=resume, tag=tag, started=started, ended=None, status="running", cost=0.0, turns=None, session=resume,
               error=None, pid=os.getpid())
    threading.Thread(target=_pump, args=(rid, args, root), daemon=True).start()
    return rid


def _settle_run(rid):
    """Swap the run's hold for what it really cost: the ResultMessage's total when one came, else the usage seen so far (a run
    stopped or broken part-way was still billed for the turns it took). Once per run."""
    import arch_cloud
    with _LOCK:
        r = _RUNS.get(rid) or {}
        hold = r.pop("hold", None)
        if not hold:
            return
        actual = r.get("cost") if r.get("result_cost") else max(float(r.get("cost") or 0), float(r.get("usage_usd") or 0))
    arch_cloud.settle(hold, actual or 0.0)


def _pump(rid, args, root):
    try:
        _drive(rid, args, root)
    finally:
        _settle_run(rid)
        with _LOCK:
            r = _RUNS.get(rid) or {}
            final = {k: r.get(k) for k in ("status", "cost", "turns", "session", "error")}
            final["n_events"] = len(r.get("events") or [])
        if final["status"] == "running":                 # the drive ended without saying how (an import failed, say)
            final["status"] = "error"
            final["error"] = final["error"] or "the run ended unexpectedly"
        write_meta(rid, ended=time.time(), **final)


def _count_usage(rid, model, usage):
    """Running estimate of what the run has cost, from each assistant message's token counts (used only when no result came)."""
    try:
        import arch_cloud
        u = usage or {}
        with _LOCK:
            ttl = (_RUNS.get(rid) or {}).get("ttl") or "5m"
        usd = arch_cloud.cost(model or "", u.get("input_tokens") or 0, u.get("output_tokens") or 0,
                              u.get("cache_read_input_tokens") or 0, u.get("cache_creation_input_tokens") or 0, ttl)
    except Exception:
        return
    with _LOCK:
        r = _RUNS.get(rid)
        if r is not None:
            r["usage_usd"] = float(r.get("usage_usd") or 0) + usd


def _drive(rid, args, root):
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
        if rec and rec.get("text"):                       # the disk copy, outside the memory lock (the page keeps polling)
            record_event(rid, rec)
        if session or cost is not None or turns is not None or error:
            write_meta(rid, **{k: v for k, v in (("session", session), ("cost", cost), ("turns", turns), ("error", error))
                               if v is not None and v != ""})

    async def go():
        env = {k: v for k, v in _env().items() if k in ("CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY")}
        if args.get("ttl") == "1h":
            env["ENABLE_PROMPT_CACHING_1H"] = "1"            # NOT_SURE: the Claude Code build must honour this switch; ignored otherwise
        opts = sdk.ClaudeAgentOptions(cwd=root, permission_mode=args["mode"], max_turns=args["max_turns"], env=env,
                                      model=args["model"] or None, resume=args["resume"] or None,
                                      max_budget_usd=args.get("budget") or 5.0,                  # a hard stop on spend per task
                                      agents=agent_definitions(sdk) or None,                      # your subagents (/agents)
                                      hooks=_ownership_hooks(sdk, root, args.get("files")))       # writes outside its own files are refused
        async for m in sdk.query(prompt=args["prompt"], options=opts):
            with _LOCK:
                if _RUNS[rid]["status"] == "stopped":     # Stop: leaving the loop closes the query and ends the agent process
                    break
            name, t = type(m).__name__, time.time()
            if name == "SystemMessage":
                d = getattr(m, "data", {}) or {}
                if getattr(m, "subtype", "") == "init":
                    add({"t": t, "type": "system.init", "text": f"session {d.get('session_id')} · model {d.get('model')} · {len(d.get('tools') or [])} tools · mode {d.get('permissionMode')}"},
                        session=d.get("session_id"))
            elif name == "AssistantMessage":
                u = getattr(m, "usage", None) or {}
                usage = {k: u.get(k) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
                _count_usage(rid, getattr(m, "model", None), usage)
                sub = getattr(m, "parent_tool_use_id", None)          # set when a subagent (Task tool) is the one talking
                for b in m.content or []:
                    bn = type(b).__name__
                    if bn == "TextBlock" and b.text:
                        add({"t": t, "type": "assistant", "text": b.text, "model": getattr(m, "model", None), "usage": usage, "parent": sub})
                    elif bn == "ThinkingBlock" and getattr(b, "thinking", ""):
                        add({"t": t, "type": "thinking", "text": b.thinking[:6000], "parent": sub})
                    elif bn == "ToolUseBlock":
                        add({"t": t, "type": "tool", "name": b.name, "id": b.id, "arg": _main_arg(b.name, b.input), "parent": sub,
                             "text": json.dumps(b.input, ensure_ascii=False, default=str)[:4000]})
            elif name == "UserMessage":
                for b in (m.content if isinstance(m.content, list) else []):
                    if type(b).__name__ == "ToolResultBlock":
                        c = b.content if isinstance(b.content, str) else json.dumps(b.content, ensure_ascii=False, default=str)
                        add({"t": t, "type": "tool_result", "id": b.tool_use_id, "error": bool(b.is_error), "text": (c or "(no output)")[:4000]})
            elif name == "ResultMessage":
                with _LOCK:
                    _RUNS[rid]["result_cost"] = True
                add({"t": t, "type": "result", "text": (m.result or "")[:4000]}, session=m.session_id, cost=m.total_cost_usd or 0,
                    turns=m.num_turns, error=(m.result or m.subtype) if m.is_error else None)
                _settle_run(rid)                                      # the real cost goes into today's total now

    try:
        asyncio.run(go())
        with _LOCK:
            if _RUNS[rid]["status"] != "stopped":
                _RUNS[rid]["status"] = "error" if _RUNS[rid]["error"] else "done"
    except Exception as e:
        with _LOCK:
            if _RUNS[rid]["status"] != "stopped":          # closing the query after Stop may raise; it is still a stop
                _RUNS[rid]["status"], _RUNS[rid]["error"] = "error", f"{type(e).__name__}: {str(e)[:500]}"


def stop(rid):
    with _LOCK:
        if rid in _RUNS and _RUNS[rid]["status"] == "running":
            _RUNS[rid]["status"], _RUNS[rid]["error"] = "stopped", "stopped from the page (the current tool call may still finish)"
            stopped = True
        else:
            stopped = False
    if stopped:
        write_meta(rid, status="stopped", error="stopped from the page (the current tool call may still finish)")


def snapshot(rid, since=0):
    with _LOCK:
        r = _RUNS.get(rid)
        if not r:
            return {"error": "no such run"}
        return {k: r[k] for k in ("status", "session", "cost", "error")} | {"events": r["events"][since:], "n": len(r["events"]), "turns": r.get("turns")}
