"""A fleet: several Claude Code agents working on one project at the same time, each on its own files, checked by code at the end.

The lead's part, done here:
  * `plan_check` refuses a plan before anything is spent: two agents sharing a file (or a folder and a file in it), an empty brief,
    a missing or bad budget, budgets that add up past the cap you pass in.
  * `launch` starts every agent at once (arch_claudecode.start) with its brief plus a footer naming the files it owns and
    forbidding all others. The state lives in <data_dir>/fleet/<id>.json.
  * A watcher waits until every agent has finished, then the FLEET runs the test commands itself in the project folder (agents in
    acceptEdits mode cannot run commands, so the proof belongs to the orchestrator), at below-normal priority, with a timeout and
    the output kept. It records pass / fail / could-not-run per command, `git diff --stat`, and any changed file no agent owned.
  * Only test commands from `ALLOWED_PREFIXES` are accepted; shell punctuation is refused.

Kinds: claude (arch_claudecode), local (one `architect.py run` subprocess) and local_agent (arch_localagent.run in this process: the
tool loop on the local models, writes refused outside its own files, its run log in cc_runs like a Claude run). Both local kinds
share the one graphics card the same way (`_GPU_CV`, one writer at a time; a local_agent on the router tier uses the CPU lane).

Three outcomes, never two: a command that timed out or could not start is `ok: None`, and makes the fleet's verdict `not_sure`.
"""
import json
import os
import posixpath
import re
import subprocess
import sys
import threading
import time

import arch_core as core

ALLOWED_PREFIXES = ("python arch_", "py -3.12 arch_", "node --check ")
SHELL_BITS = re.compile(r"[&|;<>`$%^\r\n]")
MODES = ("acceptEdits", "plan")
FINISHED = ("done", "error", "stopped", "lost")
MAX_AGENTS = 16
DEFAULT_TESTS = ("python arch_hub_check.py",)
FLEET_ID_RE = re.compile(r"fl\d{10,16}")
KINDS = ("claude", "local", "local_agent")
LOCAL_KINDS = ("local", "local_agent")  # local = one `architect.py run`; local_agent = arch_localagent.run (the tool loop, own files)
AGENT_TIERS = ("worker", "heavy", "router")
FLOW_RE = re.compile(r"[a-z][a-z0-9_]{0,30}")
CPU_FLOWS = ("route", "explain")        # planning-only steps: the router model on the CPU, so the graphics card stays free
_GPU_CV = threading.Condition()         # ONE graphics card: local agents that write take it one at a time (across fleets too)
_GPU_QUEUE = []                         # (fleet id, agent name) in the order they became ready
_GPU_HOLDER = [None]
_PROCS = {}                             # (fleet id, name) -> the running local subprocess, so stop() can end it
_LOCK = threading.RLock()
_WATCHERS = {}


# ---- the plan --------------------------------------------------------------------------------------------------------------------
def _clean_path(p):
    """A file from the plan -> (shown form, comparison key), or (shown, None) when it is not a plain path inside the project."""
    s = str(p or "").strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    if not s or s.startswith("/") or re.match(r"^[A-Za-z]:", s):
        return s, None
    s = posixpath.normpath(s)
    if s in (".", "") or ".." in s.split("/"):
        return s, None
    return s, (s.lower() if os.name == "nt" else s)


def _overlaps(a, b):
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def plan_check(plan, total_cap_usd=20.0):
    """-> a list of problems in plain words; empty when the plan may run."""
    if not isinstance(plan, list) or not plan:
        return ["the plan must be a non-empty list of agents"]
    if len(plan) > MAX_AGENTS:
        return [f"at most {MAX_AGENTS} agents in one fleet (got {len(plan)})"]
    problems, owners, names, total = [], [], set(), 0.0
    for i, a in enumerate(plan):
        if not isinstance(a, dict):
            problems.append(f"agent {i + 1}: must be an object")
            continue
        name = str(a.get("name") or "").strip()
        label = name or f"agent {i + 1}"
        if not name:
            problems.append(f"agent {i + 1}: a name is required")
        elif name in names:
            problems.append(f"{name}: two agents share this name")
        names.add(name)
        kind = a.get("kind") or "claude"
        if kind not in KINDS:
            problems.append(f"{label}: kind must be claude, local or local_agent (got {str(kind)[:20]!r})")
        if kind == "local_agent":
            if not str(a.get("goal") or a.get("brief") or "").strip():
                problems.append(f"{label}: a local agent needs a goal (or a brief)")
            if (a.get("mode") or "acceptEdits") != "acceptEdits":
                problems.append(f"{label}: a local agent edits its own files; it has no plan mode")
            if a.get("tier") and a["tier"] not in AGENT_TIERS:
                problems.append(f"{label}: tier must be one of {', '.join(AGENT_TIERS)}")
            lb = a.get("budget") or {}
            if not isinstance(lb, dict) or any(k not in ("calls", "seconds", "tokens") or not isinstance(v, (int, float))
                                               or isinstance(v, bool) or v <= 0 for k, v in lb.items()):
                problems.append(f"{label}: budget must be {{calls, seconds}} with numbers above 0")
        elif kind == "local":
            if not str(a.get("goal") or "").strip():
                problems.append(f"{label}: a local agent needs a goal")
            if a.get("flow") and not FLOW_RE.fullmatch(str(a["flow"])):
                problems.append(f"{label}: {str(a['flow'])[:30]!r} is not a flow name")
            if (a.get("mode") or "acceptEdits") != "acceptEdits":
                problems.append(f"{label}: a local agent edits its own files; it has no plan mode")
        else:
            if not str(a.get("brief") or "").strip():
                problems.append(f"{label}: the brief is empty")
            mode = a.get("mode") or "acceptEdits"
            if mode not in MODES:
                problems.append(f"{label}: mode must be acceptEdits or plan (got {str(mode)[:20]!r})")
        after = a.get("after") or []
        if isinstance(after, str):
            after = [after]
        if not isinstance(after, list) or any(x not in names or x == name for x in after):
            problems.append(f"{label}: 'after' must name agents listed before it")
        try:
            if kind == "local_agent":
                budget = 0.0                                     # the local models cost no money; its limit is calls/seconds
            elif kind == "local" and a.get("budget_usd") in (None, "", 0):
                budget = 0.0                                     # a local agent costs nothing
            else:
                budget = float(a.get("budget_usd"))
                if not budget > 0 or budget != budget:
                    raise ValueError
            total += budget
        except (TypeError, ValueError):
            problems.append(f"{label}: budget_usd must be a number above 0")
        files = a.get("files")
        if not isinstance(files, list) or not files:
            problems.append(f"{label}: list the files this agent owns")
            continue
        for f in files:
            shown, key = _clean_path(f)
            if key is None:
                problems.append(f"{label}: {shown[:80]!r} is not a path inside the project")
                continue
            for other, okey, oshown in owners:
                if other != label and _overlaps(key, okey):
                    problems.append(f"{label} and {other} both own {shown if len(shown) >= len(oshown) else oshown}")
            owners.append((label, key, shown))
    try:
        cap = float(total_cap_usd)
    except (TypeError, ValueError):
        cap = 0.0
    if total > cap + 1e-9:
        problems.append(f"the budgets add up to ${total:.2f}, above the cap of ${cap:.2f}")
    return problems


def command_problem(cmd):
    """-> None when `cmd` is an allowed test command, else why not."""
    if not isinstance(cmd, str) or not cmd.strip():
        return "an empty test command"
    c = cmd.strip()
    if not c.startswith(ALLOWED_PREFIXES):
        return f"{c[:80]!r} is not an allowed test command (allowed: {', '.join(p.strip() + '...' for p in ALLOWED_PREFIXES)})"
    if SHELL_BITS.search(c):
        return f"{c[:80]!r}: shell punctuation is not allowed in a test command"
    if ".." in c:
        return f"{c[:80]!r}: a test command may not reach outside the project"
    return None


def footer(agent, tests):
    files = "\n".join(f"  - {_clean_path(f)[0]}" for f in agent["files"])
    checks = "; ".join(tests) if tests else "none were listed"
    if (agent.get("mode") or "acceptEdits") == "plan":
        rule = ("This is a read-only task: change no file at all. The files you are responsible for:\n" + files)
    else:
        rule = ("Your files -- edit ONLY these:\n" + files + "\n"
                "Every other file in this project is off limits: other agents are editing them at this moment. Read anything you need; "
                "do not create, edit, rename or delete any file outside your list.")
    return ("\n\n---\n" + rule + "\n"
            f"You cannot run commands here. When every agent has finished, the fleet itself runs these checks: {checks}.\n"
            "End with a short report: what you changed (file: function) and anything left undone.")


# ---- running commands ------------------------------------------------------------------------------------------------------------
def run_command(cmd, cwd, timeout):
    """The default runner. -> {"rc": int or None, "output": str}; rc None = could not run (missing program, timed out)."""
    kw = {"cwd": cwd, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "timeout": timeout}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        args = cmd                                             # CreateProcess takes the line as written
    else:
        import shlex
        kw["preexec_fn"] = lambda: os.nice(10)
        args = shlex.split(cmd)
    try:
        p = subprocess.run(args, **kw)
        return {"rc": p.returncode, "output": (p.stdout or b"").decode("utf-8", "replace")}
    except subprocess.TimeoutExpired as e:
        out = e.output.decode("utf-8", "replace") if isinstance(e.output, bytes) else (e.output or "")
        return {"rc": None, "output": out + f"\n(stopped after {timeout} s)"}
    except OSError as e:
        return {"rc": None, "output": f"could not start: {e}"}


def _changed_paths(porcelain):
    out = set()
    for line in (porcelain or "").splitlines():
        if len(line) < 4:
            continue
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        shown, key = _clean_path(path)
        if key:
            out.add((key, shown))
    return out


# ---- state -----------------------------------------------------------------------------------------------------------------------
def _fleet_dir():
    return os.path.join(core.data_dir(), "fleet")


def _path(fid):
    if not FLEET_ID_RE.fullmatch(str(fid or "")):
        raise ValueError(f"not a fleet id: {str(fid)[:40]!r}")
    return os.path.join(_fleet_dir(), f"{fid}.json")


def _read(fid):
    # On Windows a read that meets _write's os.replace mid-swap gets PermissionError (or a half-seen file): retry briefly
    # instead of reporting a live fleet as "no such fleet".
    for i in range(20):
        try:
            with open(_path(fid), encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            time.sleep(0.02 * (i + 1))
    return None


def _write(state):
    path = _path(state["id"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, ensure_ascii=False, default=str)
    for i in range(20):                     # Windows: the swap is refused while a reader holds the file open; it is free a moment later
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 19:
                raise
            time.sleep(0.02 * (i + 1))


def _update(fid, fn):
    with _LOCK:
        st = _read(fid)
        if st is None:
            return None
        fn(st)
        _write(st)
        return st


def _view(rid):
    """What an agent's run looks like now: the live run when this process has it, else its log on disk."""
    import arch_claudecode as cc
    if not rid:
        return None
    s = cc.snapshot(rid, since=1 << 30)
    if "status" in s:
        n = s.get("n") or 0
        last = cc.snapshot(rid, since=max(0, n - 1)).get("events") or [] if n else []
        return {"status": s["status"], "cost": s.get("cost") or 0.0, "turns": s.get("turns"), "error": s.get("error"),
                "last_event": _short(last[-1]) if last else None}
    d = cc.load_run(rid)
    if "error" in d and "events" not in d:
        return {"status": "lost", "cost": 0.0, "turns": None, "error": d["error"], "last_event": None}
    status = d.get("status") or "running"
    if status == "running" and d.get("pid") not in (None, os.getpid()):
        status = "lost"                                          # its process is gone (a hub restart); nothing will finish it
    ev = d.get("events") or []
    return {"status": status, "cost": d.get("cost") or 0.0, "turns": d.get("turns"), "error": d.get("error"),
            "last_event": _short(ev[-1]) if ev else None}


def _short(ev):
    ev = dict(ev)
    if isinstance(ev.get("text"), str) and len(ev["text"]) > 300:
        ev["text"] = ev["text"][:300] + "..."
    return ev


# ---- launch, watch, report -------------------------------------------------------------------------------------------------------
def _new_id():
    with _LOCK:
        ms = int(time.time() * 1000)
        while os.path.exists(_path(f"fl{ms}")) or f"fl{ms}" in _WATCHERS:
            ms += 1
        _WATCHERS[f"fl{ms}"] = None                              # reserved
        return f"fl{ms}"


def launch(plan, root, total_cap_usd=20.0, tests=DEFAULT_TESTS, starter=None, runner=None, poll=3.0, test_timeout=1800,
           max_wait_s=4 * 3600, max_turns=60, local_runner=None, local_timeout=3600, agent_runner=None):
    """Check the plan, start every agent at once, watch them, then test. -> fleet id. ValueError (plain words) when refused.
    `starter(root=, prompt=, mode=, model=, budget=, max_turns=, tag=) -> run id` and `runner(cmd, cwd, timeout) -> {rc, output}`
    are for tests; by default they are arch_claudecode.start and run_command. `agent_runner` stands in for arch_localagent.run
    (kind local_agent), called with the same arguments."""
    tests = list(tests or [])
    problems = plan_check(plan, total_cap_usd) + [p for p in (command_problem(c) for c in tests) if p]
    if not root or not os.path.isdir(root):
        problems.append(f"the project folder {str(root)[:120]!r} does not exist")
    if problems:
        raise ValueError("; ".join(problems))
    if starter is None:
        import arch_claudecode as cc
        starter = cc.start
        claude_bill = sum(float(a.get("budget_usd") or 0) for a in plan if (a.get("kind") or "claude") == "claude")
        if claude_bill > 0:                                      # the whole fleet's Claude bill must fit under today's cap BEFORE
            import arch_cloud                                    # anything starts; each agent then holds its own budget (cc.start)
            try:
                arch_cloud.reserve(core.load_config(), claude_bill, what=f"This fleet's {claude_bill:.2f} USD of agent budgets",
                                   check_only=True)
            except arch_cloud.SpendRefused as e:
                raise ValueError(str(e))
    runner = runner or run_command
    root = os.path.abspath(root)
    fid = _new_id()
    before = runner("git status --porcelain", root, 60)
    agents = []
    for a in plan:
        kind = a.get("kind") or "claude"
        local = kind in LOCAL_KINDS
        after = a.get("after") or []
        after = [after] if isinstance(after, str) else list(after)
        agents.append({"name": a["name"].strip(), "kind": kind,
                       "brief": (a.get("goal") or a.get("brief")) if local else a["brief"],
                       "files": [_clean_path(f)[0] for f in a["files"]],
                       "tier": (a.get("tier") or "worker") if kind == "local_agent" else None,
                       "local_budget": dict(a.get("budget") or {}) if kind == "local_agent" else None,
                       "budget_usd": float(a.get("budget_usd") or 0) if local else float(a["budget_usd"]),
                       "model": a.get("model") or None, "model_why": None, "mode": "acceptEdits" if local else (a.get("mode") or "acceptEdits"),
                       "flow": (a.get("flow") or None) if local else None, "after": after,
                       "lane": (lane_of(a) if local else None), "run": None,
                       "status": "waiting" if after else "starting", "cost": 0.0, "turns": None, "error": None,
                       "waited_s": 0.0, "waiting_for": None, "why": None, "trace": None, "rc": None})
    for ag in agents:                                            # no write tools and no model named -> a cheaper model (never one the person named)
        if ag["kind"] == "claude" and not ag["model"]:
            try:
                import arch_cloud
                import arch_core
                ro = ag["mode"] == "plan"
                m, why = arch_cloud.readonly_model(arch_core.load_config(), None, ro, tiny=len(ag["brief"] or "") < 600 and ag["budget_usd"] <= 0.5)
                if m:
                    ag["model"], ag["model_why"] = m, why
            except Exception:
                pass
    state = {"id": fid, "root": root, "created": time.time(), "ended": None, "phase": "starting", "cap_usd": float(total_cap_usd),
             "tests_planned": tests, "tests": [], "diffstat": None, "strays": [], "verdict": None,
             "dirty_before": sorted(s for _, s in _changed_paths(before.get("output") if before.get("rc") == 0 else ""))}
    with _LOCK:
        _write(state)
    threads = []
    for a in agents:                                             # each start returns at once; the agents run side by side
        if a["kind"] in LOCAL_KINDS or a["after"]:
            continue                                             # started by its own thread below
        try:
            kw = {"files": a["files"]} if getattr(starter, "__name__", "") == "start" and getattr(starter, "__module__", "") == "arch_claudecode" else {}
            a["run"] = starter(root=root, prompt=a["brief"].rstrip() + footer(a, tests), mode=a["mode"], model=a["model"],
                               budget=a["budget_usd"], max_turns=max_turns, tag=fid, **kw)
            a["status"] = "running"
        except Exception as e:
            a["status"], a["error"] = "error", f"could not start: {type(e).__name__}: {str(e)[:300]}"
    ready = [a for a in agents if a["kind"] in LOCAL_KINDS and not a["after"]]
    for a in ready:                                              # plan order, decided here so two local agents never race for the card
        if a["lane"] == "gpu":
            with _GPU_CV:
                _GPU_QUEUE.append((fid, a["name"]))
            a["status"], a["why"] = "queued", "waiting for the graphics card"
        else:
            a["status"] = "running"

    def started(st):
        st["agents"], st["phase"] = agents, "running"
    _update(fid, started)
    local_runner = local_runner or run_local
    for a in agents:
        if a["kind"] in LOCAL_KINDS or a["after"]:
            th = threading.Thread(target=_agent_thread, name=f"fleet-{fid}-{a['name']}", daemon=True,
                                  args=(fid, a["name"], root, tests, starter, local_runner, poll, max_turns, local_timeout,
                                        any(a is r for r in ready), agent_runner))
            threads.append(th)
    for th in threads:
        th.start()
    t = threading.Thread(target=_watch, args=(fid, runner, poll, test_timeout, max_wait_s), daemon=True, name=f"fleet-{fid}")
    with _LOCK:
        _WATCHERS[fid] = t
    t.start()
    return fid


# ---- local agents: the same job on the models of this machine ---------------------------------------------------------------------
def lane_of(agent):
    """'cpu' for planning-only steps (they use the router on the CPU), else 'gpu': the one card, one writer at a time."""
    return ("cpu" if agent.get("lane") == "cpu" or agent.get("flow") in CPU_FLOWS
            or (agent.get("kind") == "local_agent" and agent.get("tier") == "router") else "gpu")


def local_footer(agent):
    files = "\n".join(f"  - {_clean_path(f)[0]}" for f in agent["files"])
    return ("\n\nYour files -- change ONLY these:\n" + files + "\nOther agents are working on every other file right now; "
            "do not create, edit or delete any file outside this list.")


def local_argv(agent, root, goal):
    argv = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "architect.py"), "run", goal,
            "--root", root, "--review", "auto"]
    if agent.get("flow"):
        argv += ["--flow", agent["flow"]]
    if agent.get("model"):
        argv += ["--model", agent["model"]]
    return argv


def run_local(argv, cwd, timeout, env=None):
    """The default local runner. -> {"rc": int or None, "output": str}; rc None = could not run (timed out, would not start)."""
    kw = {"cwd": cwd, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "env": env}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    key = (env or {}).get("ARCHITECT_FLEET_AGENT")
    try:
        p = subprocess.Popen(argv, **kw)
    except OSError as e:
        return {"rc": None, "output": f"could not start: {e}"}
    with _LOCK:
        _PROCS[key] = p
    try:
        out, _ = p.communicate(timeout=timeout)
        return {"rc": p.returncode, "output": (out or b"").decode("utf-8", "replace")}
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
        return {"rc": None, "output": (out or b"").decode("utf-8", "replace") + f"\n(stopped after {timeout} s)"}
    finally:
        with _LOCK:
            _PROCS.pop(key, None)


def _end_local_procs(fid):
    with _LOCK:
        procs = [p for k, p in _PROCS.items() if str(k).startswith(fid + ":")]
    for p in procs:
        try:
            p.terminate()
        except OSError:
            pass


def _trace_of(output):
    m = re.findall(r"report:\s*(.+?report\.md)", output or "")
    return os.path.dirname(m[-1].strip()) if m else None


def _set_agent(fid, name, **kw):
    def f(st):
        for a in st.get("agents") or []:
            if a.get("name") == name:
                a.update(kw)
    return _update(fid, f)


def _stopping(fid):
    return bool((_read(fid) or {}).get("stop_requested"))


def _plan_text(run):
    """The last thing a finished planning agent said (best effort), to hand to the local agent that writes."""
    try:
        import arch_claudecode as cc
        ev = (cc.load_run(run) or {}).get("events") or []
        for e in reversed(ev):
            if isinstance(e, dict) and isinstance(e.get("text"), str) and e["text"].strip():
                return e["text"].strip()[:3000]
    except Exception:
        pass
    return ""


def _agent_thread(fid, name, root, tests, starter, local_runner, poll, max_turns, local_timeout, queued, agent_runner=None):
    """One agent that does not start at once: it waits for its `after` agents, and a local one for the graphics card."""
    key = (fid, name)
    gpu_taken = False
    try:
        t0 = time.time()
        extra = ""
        while True:                                              # 1. the agents this one comes after
            st = _read(fid) or {}
            by = {x["name"]: x for x in st.get("agents") or []}
            me = by.get(name)
            if me is None:
                return
            if st.get("stop_requested"):
                _set_agent(fid, name, status="stopped", error="stopped before it started", why=None)
                return
            deps = [_settle(dict(by[d])) for d in me.get("after") or [] if d in by]
            if all(d["status"] in FINISHED for d in deps):
                bad = [d for d in deps if d["status"] != "done"]
                if bad:
                    _set_agent(fid, name, status="error", why=None,
                               error=f"not started: {bad[0]['name']} ended as {bad[0]['status']}")
                    return
                extra = "".join(f"\n\nBrief from {d['name']}:\n{t}" for d in deps for t in [_plan_text(d.get("run"))] if t)
                break
            time.sleep(min(poll, 0.5))
        me = dict(me)
        waited_for = None
        if me["kind"] not in LOCAL_KINDS:                        # a Claude agent that only needed to wait its turn
            run = starter(root=root, prompt=me["brief"].rstrip() + footer(me, tests), mode=me["mode"], model=me["model"],
                          budget=me["budget_usd"], max_turns=max_turns, tag=fid)
            _set_agent(fid, name, run=run, status="running", why=None)
            return
        if me["lane"] == "gpu":                                  # 2. the one card: first come, first served
            with _GPU_CV:
                if not queued:
                    _GPU_QUEUE.append(key)
                    queued = True
                _set_agent(fid, name, status="queued", why="waiting for the graphics card")
                while True:
                    if _stopping(fid):
                        _set_agent(fid, name, status="stopped", error="stopped while waiting for the graphics card", why=None)
                        return
                    if _GPU_HOLDER[0] is None and _GPU_QUEUE and _GPU_QUEUE[0] == key:
                        _GPU_HOLDER[0] = key
                        _GPU_QUEUE.remove(key)
                        queued, gpu_taken = False, True
                        break
                    blocker = _GPU_HOLDER[0] or next((k for k in _GPU_QUEUE if k != key), None)
                    if blocker and blocker[1] != waited_for:
                        waited_for = blocker[1]
                        _set_agent(fid, name, waiting_for=waited_for, why=f"one graphics card: waiting for {waited_for}")
                    _GPU_CV.wait(min(poll, 0.5))
        waited = round(time.time() - t0, 2)
        why = (f"waited {waited:.1f} s for {waited_for}: one graphics card, one writer at a time" if waited_for
               else ("ran at once: planning-only work uses the CPU router" if me["lane"] == "cpu" else "had the graphics card free"))
        _set_agent(fid, name, status="running", waited_s=waited, why=why, started=time.time())
        if me["kind"] == "local_agent":                          # the tool loop in this process, its own files, its own tests
            if agent_runner is None:
                import arch_localagent
                agent_runner = arch_localagent.run
            res = agent_runner(str(me["brief"]).rstrip() + extra, root, files=list(me["files"]),
                               budget=me.get("local_budget") or None, tier=me.get("tier") or "worker", tag=fid,
                               should_stop=lambda: _stopping(fid),
                               on_start=lambda rid: _set_agent(fid, name, run=rid)) or {}
            st = res.get("status") if res.get("status") in FINISHED else "error"
            _set_agent(fid, name, status=st, run=res.get("id"), error=None if st == "done" else (res.get("why") or "not complete"),
                       output=str(res.get("answer") or res.get("why") or "")[-4000:], checks=res.get("checks"),
                       refused=res.get("refused") or [], ended=time.time())
            return
        goal = str(me["brief"]).rstrip() + extra + local_footer(me)
        env = dict(os.environ, ARCHITECT_FLEET=fid, ARCHITECT_FLEET_AGENT=f"{fid}:{name}",
                   ARCHITECT_OWNS="|".join(_clean_path(f)[0] for f in me["files"]))      # the child's Engine refuses every other file
        r = local_runner(local_argv(me, root, goal), root, local_timeout, env) or {}
        rc, out = r.get("rc"), str(r.get("output") or "")
        if rc == 0:
            _set_agent(fid, name, status="done", rc=0, trace=_trace_of(out), output=out[-4000:], ended=time.time())
        else:
            err = "could not run" if rc is None else f"the run ended with exit code {rc} (not complete)"
            _set_agent(fid, name, status="error", rc=rc, trace=_trace_of(out), output=out[-4000:], error=err, ended=time.time())
    except Exception as e:                                       # never a silent thread: the agent shows the error
        _set_agent(fid, name, status="error", error=f"{type(e).__name__}: {str(e)[:300]}", ended=time.time())
    finally:
        with _GPU_CV:
            if key in _GPU_QUEUE:
                _GPU_QUEUE.remove(key)
            if gpu_taken and _GPU_HOLDER[0] == key:
                _GPU_HOLDER[0] = None
            _GPU_CV.notify_all()


def hybrid_plan(goal, parts, plan_budget_usd=1.0, review_budget_usd=1.0, review=True):
    """'Claude thinks, local writes', at fleet level. `parts` = [{"name", "files", optional "task", "flow"}]. -> a plan for `launch`:
    one read-only Claude planner per part (all at once), one local writer per part on that part's files (after its planner, one at
    a time on the card, handed the planner's brief), and one read-only Claude reviewer after every writer."""
    if not isinstance(parts, list) or not parts:
        raise ValueError("hybrid_plan needs a non-empty list of parts")
    goal = str(goal or "").strip()
    if not goal:
        raise ValueError("hybrid_plan needs a goal")
    clean = []
    for i, p in enumerate(parts):
        if not isinstance(p, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", str(p.get("name") or "")):
            raise ValueError(f"part {i + 1}: a name of letters, digits, - and _ is required")
        if not isinstance(p.get("files"), list) or not p["files"]:
            raise ValueError(f"{p['name']}: list the files this part owns")
        clean.append(p)
    planners, writers = [], []
    for p in clean:
        n, files = p["name"], list(p["files"])
        task = str(p.get("task") or goal).strip()
        planners.append({"name": f"{n}-plan", "kind": "claude", "mode": "plan", "budget_usd": plan_budget_usd,
                         "files": [f".arch/fleet/briefs/{n}.md"],
                         "brief": (f"Overall goal: {goal}\nYour part: {task}\nRead the code in {', '.join(files)} and what it uses. "
                                   "Do not change anything. Write a short, exact brief for a small local model that will do the "
                                   "edits: which functions to add or change, their signatures, the edge cases, what the tests "
                                   "should check. Plain steps, no more than 25 lines.")})
        writers.append({"name": n, "kind": "local", "goal": task, "flow": p.get("flow"), "files": files,
                        "after": [f"{n}-plan"]})
    out = planners + writers
    if review:
        out.append({"name": "review", "kind": "claude", "mode": "plan", "budget_usd": review_budget_usd,
                    "files": [".arch/fleet/briefs/review.md"], "after": [w["name"] for w in writers],
                    "brief": (f"Overall goal: {goal}\nLocal models just made the edits in {', '.join(p['name'] for p in clean)}. "
                              "Read the changed files and judge them against the goal: what is missing, wrong or risky. "
                              "Change nothing. End with a numbered list of concrete problems, or 'no problems found'.")})
    return out


def _settle(agent):
    if agent.get("kind") == "local_agent":
        # its own thread writes the last word (status, checks, refused). The run log says "done" a moment BEFORE that thread records
        # its result here, so taking the status from the log let the watcher finish early and write back a copy without the agent's
        # checks (arch_selftest_localagent failed now and then, 2026-11-08). The log only feeds what is shown while it runs.
        if agent.get("status") not in FINISHED:
            v = _view(agent.get("run"))
            if v:
                agent.update({k: v[k] for k in ("cost", "turns")})
                agent["last_event"] = v["last_event"]
        return agent
    v = _view(agent.get("run"))
    if v:
        agent.update({k: v[k] for k in ("status", "cost", "turns", "error")})
        agent["last_event"] = v["last_event"]
    return agent


def _watch(fid, runner, poll, test_timeout, max_wait_s):
    t0 = time.time()
    try:
        while True:
            st = _read(fid) or {}
            agents = [_settle(dict(a)) for a in st.get("agents") or []]
            if all(a["status"] in FINISHED for a in agents):
                break
            if st.get("stop_requested") or time.time() - t0 > max_wait_s:
                _stop_runs(agents)
                if time.time() - t0 > max_wait_s + 120 or st.get("stop_requested") and time.time() - st["stop_requested"] > 120:
                    break                                       # the agents did not wind down; test what is there
            time.sleep(poll)

        def testing(s):
            # settled against the state as it is NOW (under the lock), never the copy read before the wait ended: an agent's thread may
            # have written its result in between, and writing the old copy back erased it
            s["agents"], s["phase"] = [_settle(dict(a)) for a in s.get("agents") or []], "testing"
        st = _update(fid, testing)
        agents = st["agents"]
        root = st["root"]
        ran_any = any(a.get("run") or a.get("started") for a in agents)
        results = []
        for cmd in st.get("tests_planned") or []:
            why = command_problem(cmd)                           # checked at launch; checked again before anything runs
            if why or not ran_any:
                results.append({"cmd": cmd, "ok": None, "rc": None, "seconds": 0.0,
                                "output": why or "not run: no agent started"})
                continue
            t1 = time.time()
            r = runner(cmd, root, test_timeout) or {}
            rc = r.get("rc")
            results.append({"cmd": cmd, "ok": None if rc is None else rc == 0, "rc": rc, "seconds": round(time.time() - t1, 1),
                            "output": str(r.get("output") or "")[-4000:]})
            _update(fid, lambda s: s.__setitem__("tests", list(results)))
        diff = runner("git diff --stat", root, 120) or {}
        after = runner("git status --porcelain", root, 60) or {}
        owned = [_clean_path(f)[1] for a in agents for f in a["files"]]
        dirty_before = {_clean_path(p)[1] for p in st.get("dirty_before") or []}
        strays = sorted(shown for key, shown in _changed_paths(after.get("output") if after.get("rc") == 0 else "")
                        if key not in dirty_before and not any(_overlaps(key, o) for o in owned if o))
        oks = [r["ok"] for r in results]
        verdict = "fail" if False in oks else ("not_sure" if None in oks or not oks else "pass")

        def finished(s):
            s.update(tests=results, phase="done", ended=time.time(), verdict=verdict, strays=strays,
                     diffstat=(diff.get("output") or "").strip() if diff.get("rc") == 0 else None)
        _update(fid, finished)
    except Exception as e:                                       # the watcher never dies silently
        _update(fid, lambda s: s.update(phase="done", ended=time.time(), verdict="not_sure",
                                        error=f"watcher: {type(e).__name__}: {str(e)[:300]}"))
    finally:
        with _LOCK:
            _WATCHERS.pop(fid, None)


def _stop_runs(agents):
    import arch_claudecode as cc
    for a in agents:
        if a.get("run") and a.get("status") not in FINISHED:
            cc.stop(a["run"])


def stop(fid):
    """Ask every agent of the fleet to stop; the watcher then tests what was done."""
    st = _update(fid, lambda s: s.setdefault("stop_requested", time.time()))
    if st:
        _stop_runs([_settle(dict(a)) for a in st.get("agents") or []])
        _end_local_procs(fid)
        with _GPU_CV:
            _GPU_CV.notify_all()
    return bool(st)


def status(fid):
    try:
        st = _read(fid)
    except ValueError as e:
        return {"error": str(e)}
    if st is None:
        return {"error": "no such fleet"}
    agents = []
    for a in st.get("agents") or []:
        a = _settle(dict(a)) if st.get("phase") in ("starting", "running") else dict(a)
        agents.append({k: a.get(k) for k in ("name", "run", "status", "cost", "turns", "files", "mode", "budget_usd", "error",
                                              "last_event", "kind", "lane", "flow", "after", "waited_s", "waiting_for", "why",
                                              "trace", "rc", "tier", "checks", "refused")})
    waits = [{"agent": a["name"], "waited_s": a.get("waited_s"), "for": a.get("waiting_for"), "why": a.get("why")}
             for a in agents if a.get("waiting_for") or a.get("status") in ("queued", "waiting")]
    with _LOCK:
        watching = fid in _WATCHERS
    return {"id": fid, "root": st.get("root"), "phase": st.get("phase"), "verdict": st.get("verdict"), "created": st.get("created"),
            "ended": st.get("ended"), "cap_usd": st.get("cap_usd"), "agents": agents, "waits": waits, "tests": st.get("tests") or [],
            "tests_planned": st.get("tests_planned") or [], "total_cost": round(sum(float(a.get("cost") or 0) for a in agents), 4),
            "diffstat": st.get("diffstat"), "strays": st.get("strays") or [], "error": st.get("error"),
            "watched": watching or st.get("phase") == "done"}


def list_fleets(limit=50):
    """Newest first: id, created, phase, verdict, agents, total cost."""
    try:
        names = [n[:-5] for n in os.listdir(_fleet_dir()) if n.endswith(".json") and FLEET_ID_RE.fullmatch(n[:-5])]
    except OSError:
        return []
    out = []
    for fid in names:
        st = _read(fid)
        if not st:
            continue
        ags = st.get("agents") or []
        out.append({"id": fid, "created": st.get("created"), "phase": st.get("phase"), "verdict": st.get("verdict"),
                    "agents": len(ags), "total_cost": round(sum(float(a.get("cost") or 0) for a in ags), 4),
                    "root": st.get("root")})
    out.sort(key=lambda f: f.get("created") or 0, reverse=True)
    return out[:max(0, int(limit))]
