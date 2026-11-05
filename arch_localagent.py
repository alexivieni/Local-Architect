"""A local agent: one brief (or one agent template) carried out by the models on THIS machine, through the tool loop they already use.

    run(template_or_brief, root, files, budget={"calls": 24, "seconds": 900}, tier="worker") -> result

What it is, in plain words:
  * The same job a Claude agent gets from the fleet -- a brief and the files it owns -- done by the local worker (or another tier)
    calling the tools in arch_tools: ls, grep, search, read, outline, view, edit_lines, apply, run_tests. One tool per reply; the reply
    is constrained to arch_tools.schema() and validated again in code (Engine.ask_json).
  * Its files are its own. A write to any other file is refused BEFORE the tool runs (the model is told which files it owns), and a
    guard on the engine's commit refuses it again, so no path through apply/edit_lines can reach another agent's file.
  * It can run the tests itself (run_tests), which a Claude agent in accept-edits mode cannot. Whether it did is kept with three
    outcomes: `checks.ok` True (ran after its last change and passed), False (ran and failed), None (not run after the last change).
    The fleet still runs its own checks afterwards; this is the agent's own evidence, never the verdict.
  * It stops on its budget (model calls, seconds -- enforced by Engine.call before each request, plus the STOP file), on a stall
    (STALL_AFTER steps in a row that got nowhere: an unusable reply, the same call again, a refused write, a failing tool), or when the
    caller's `should_stop()` says so (the fleet's Stop button).
  * Every step is written as events in arch_claudecode's run-log format (<data>/cc_runs/<id>.jsonl + <id>.meta.json; event types
    system.init / tool / tool_result / assistant / result), so the Monitor, the Logbook and the fleet's live board show a local agent
    exactly like a Claude one. The meta says `by_claude: false`, `kind: local_agent`, `cost: 0.0`.

Templates: the library's header (arch_agentlib) plus two lines this module reads, written as header comments so the library's own
parser (which refuses unknown keys) keeps loading them:
    # local_ok: yes | no     yes = a job a 4B worker with tools can do (test wiring, docs, small fixes, templates);
                             no  = it needs heavy judgment: route it to Claude or the heavy tier
    # tier: worker | heavy | router
The plain form (`local_ok: yes`) is read too, for the day the library accepts the keys itself.
"""
import json
import os
import posixpath
import re
import threading
import time

import arch_core as core

TOOLS = ("ls", "grep", "search", "read", "outline", "view", "edit_lines", "apply", "run_tests")
WRITE_TOOLS = ("edit_lines", "apply")
DEFAULT_BUDGET = {"calls": 24, "seconds": 900}
STALL_AFTER = 3                      # steps in a row that got nowhere
HISTORY_FULL = 3                     # the newest turns shown with their whole result; older ones shortened
HISTORY_KEEP = 10                    # turns kept in the prompt at all (older ones become one line each)
OLD_RESULT_CAP = 600
ROLE = "generator"
TIERS = ("worker", "heavy", "router")
RUN_PREFIX = "la"
_ID_LOCK = threading.Lock()
_TAKEN = set()


# ---- template headers ------------------------------------------------------------------------------------------------------------
def header(name, folder=None):
    """-> {"name", "local_ok": True | False | None, "tier": str | None, "title", "purpose"} for one template. None = the header does
    not say (older templates): treat it as not suited to a local worker."""
    import arch_agentlib as lib
    t = lib.load(name, folder)                                   # ValueError in plain words when the template itself is broken
    with open(t["path"], encoding="utf-8") as f:
        raw = f.read()
    head, _ = lib._split(raw, name)
    out = {"name": name, "local_ok": None, "tier": None, "title": t["title"], "purpose": t["purpose"]}
    for line in head:
        s = line.strip().lstrip("#").strip()
        if ":" not in s:
            continue
        key, val = (x.strip() for x in s.split(":", 1))
        if key == "local_ok":
            v = val.lower()
            if v not in ("yes", "no"):
                raise ValueError(f"{name}: local_ok must be yes or no (got {val[:20]!r})")
            out["local_ok"] = v == "yes"
        elif key == "tier":
            if val not in TIERS:
                raise ValueError(f"{name}: tier must be one of {', '.join(TIERS)} (got {val[:20]!r})")
            out["tier"] = val
    return out


def who_runs(name, folder=None):
    """-> {"runner": "local_agent" | "claude", "tier", "why"}: where a template's job should go, in plain words."""
    h = header(name, folder)
    if h["local_ok"]:
        tier = h["tier"] or "worker"
        return {"runner": "local_agent", "tier": tier,
                "why": f"{h['title']}: a job a small local model with tools can finish (it runs the tests itself); tier {tier}"}
    tier = h["tier"] or "heavy"
    why = ("the template says it needs heavy judgment" if h["local_ok"] is False
           else "the template does not say it suits a local worker")
    return {"runner": "claude", "tier": tier, "why": f"{h['title']}: {why}; give it to Claude, or locally to the {tier} tier"}


# ---- own files -------------------------------------------------------------------------------------------------------------------
def _norm(p):
    """A path as the model or the plan wrote it -> a comparable project-relative form, or None when it points outside the project."""
    s = str(p or "").strip().strip("`'\"").replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    if not s or s.startswith("/") or re.match(r"^[A-Za-z]:", s):
        return None
    s = posixpath.normpath(s)
    if s in (".", "") or ".." in s.split("/"):
        return None
    return s.lower() if os.name == "nt" else s


def owns(files, rel):
    """True when `rel` is one of `files` or inside a folder listed there."""
    key = _norm(rel)
    if key is None:
        return False
    for f in files or []:
        k = _norm(f)
        if k and (key == k or key.startswith(k.rstrip("/") + "/")):
            return True
    return False


def write_targets(name, args):
    """The files a tool call would write. apply: every FILE/EDIT/DEF block's path; edit_lines: its path; other tools: none."""
    args = args if isinstance(args, dict) else {}
    if name == "edit_lines":
        return [str(args.get("path") or "")]
    if name == "apply":
        try:
            return [str(b.get("path") or b.get("rel") or "") for b in core.parse_blocks(str(args.get("blocks") or ""))]
        except Exception:
            return []
    return []


def _guard(e, files, refused):
    """Refuse, at the engine's commit, any write outside `files` (the second wall behind the check before each tool)."""
    inner = e._commit

    def commit(p, quiet=False):
        if not owns(files, p.get("rel")):
            refused.append(p.get("rel"))
            p.update(status="error", msg=f"not your file: you may change only {', '.join(files)}")
            e.say(f"[-] {p.get('rel')}: refused (not one of this agent's files)")
            return p
        return inner(p, quiet)
    e._commit = commit


# ---- the run log (arch_claudecode's format) --------------------------------------------------------------------------------------
def _new_id():
    import arch_claudecode as cc
    with _ID_LOCK:
        ms = int(time.time() * 1000)
        while f"{RUN_PREFIX}{ms}" in _TAKEN or os.path.exists(cc._run_path(f"{RUN_PREFIX}{ms}", ".meta.json")):
            ms += 1
        _TAKEN.add(f"{RUN_PREFIX}{ms}")
        return f"{RUN_PREFIX}{ms}"


class _Log:
    def __init__(self, rid):
        import arch_claudecode as cc
        self.cc, self.rid, self.n = cc, rid, 0

    def event(self, **rec):
        rec.setdefault("t", time.time())
        rec["text"] = str(rec.get("text") or "")[:4000] or "(no output)"
        self.cc.record_event(self.rid, rec)
        self.n += 1

    def meta(self, **fields):
        self.cc.write_meta(self.rid, **fields)


def _main_arg(name, args):
    try:
        import arch_claudecode as cc
        return cc._main_arg(name, args)
    except Exception:
        return ""


# ---- the brief -------------------------------------------------------------------------------------------------------------------
def brief_for(template_or_brief, files=None, values=None, folder=None):
    """-> (name, brief, files, tests). A template name is filled (slots from `values`); anything else is the brief itself."""
    import arch_agentlib as lib
    text = str(template_or_brief or "").strip()
    if not text:
        raise ValueError("give a template name or a brief")
    if lib.NAME_RE.fullmatch(text) and os.path.isfile(os.path.join(lib._folder(folder), text + ".md")):
        parts = lib.fill_parts(text, folder=folder, **dict(values or {}))
        if parts["kind"] != "agent":
            raise ValueError(f"{text}: a footer is appended to briefs; it is not an agent")
        return text, parts["body"], list(files or parts["files"]), parts["tests"]
    return "brief", text, list(files or []), []


def preamble(brief, files, tools, can_write):
    """The stable part of every prompt in this run (instructions, tools, own files, the job); the steps so far come after it."""
    import arch_tools
    own = "\n".join(f"  - {f}" for f in files)
    write_rule = (("YOUR FILES -- change ONLY these:\n" + own + "\nEvery other file is off limits (other agents are editing them now); "
                   "a change to any other file is refused.\n")
                  if can_write else "This is read-only work: change no file.\n")
    return ("You are a local agent working inside a project folder. You work by calling tools, ONE per reply, as JSON:\n"
            '  {"tool": "<name>", "args": {...}}   or, when you are finished:   {"final": "<your report>"}\n\n'
            "TOOLS:\n" + arch_tools.describe(list(tools)) + "\n\n"
            + write_rule +
            ("How to work: look first (ls, outline, grep), read before you change anything, change one thing at a time (view gives "
             "line numbers for edit_lines; apply takes FILE:/EDIT:/DEF: blocks), then run the tests yourself with run_tests and fix "
             "what fails. Do not repeat a call that already answered. When you are done, reply with final: what you changed "
             "(file: function), the test result, and anything left undone.\n\n" if can_write else
             "How to work: look first (ls, outline, grep), read what matters, and do not repeat a call that already answered. "
             "When you are done, reply with final: what you found, with files and lines.\n\n") +
            "THE JOB:\n" + brief.strip() + "\n")


def _history(turns):
    """The steps so far, newest last: the newest few in full, older ones shortened, the oldest as one line each."""
    out = []
    n = len(turns)
    for i, (head, result) in enumerate(turns):
        age = n - i
        if age > HISTORY_KEEP:
            out.append(head + " -> " + (result.splitlines()[0][:100] if result else ""))
        elif age > HISTORY_FULL:
            out.append(head + "\nRESULT:\n" + (result if len(result) <= OLD_RESULT_CAP else result[:OLD_RESULT_CAP] + " ... [shortened]"))
        else:
            out.append(head + "\nRESULT:\n" + result)
    return "\n".join(out)


def _test_status(out):
    m = re.match(r"result:\s*(\S+)", out or "")
    st = (m.group(1) if m else "").lower()
    if st in ("pass", "passed", "ok"):
        return True, st
    if st in ("fail", "failed", "error"):
        return False, st
    return None, st or "unknown"


# ---- the run ---------------------------------------------------------------------------------------------------------------------
def run(template_or_brief, root, files=None, budget=None, tier="worker", values=None, engine=None, cfg=None, tag=None,
        should_stop=None, on_start=None, say=None, folder=None, tools=TOOLS, stall_after=STALL_AFTER):
    """Carry out one brief on the local models. -> {"id", "status": done|stopped|error, "why", "answer", "steps", "seconds",
    "changed", "refused", "checks": {"ok": True|False|None, "status"}, "tools", "model", "tier", "by_claude": False}.
    `engine` (an arch_core.Engine) and `say` are for tests and embedding; by default an engine is made for `root`.
    `on_start(run_id)` is called as soon as the run log exists (the fleet shows it live); `should_stop()` is asked before every step."""
    import arch_tools
    root = os.path.abspath(root or ".")
    if not os.path.isdir(root):
        raise ValueError(f"the project folder {root[:120]!r} does not exist")
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {', '.join(TIERS)}")
    name, brief, files, tests = brief_for(template_or_brief, files, values, folder)
    tools = [t for t in tools if t in arch_tools.TOOLS]
    bad = [f for f in files if _norm(f) is None]
    if bad:
        raise ValueError(f"{str(bad[0])[:80]!r} is not a path inside the project")
    if not files:
        tools = [t for t in tools if t not in WRITE_TOOLS]          # no files owned: a read-only agent
    b = dict(DEFAULT_BUDGET)
    b.update({k: v for k, v in (budget or {}).items() if k in ("calls", "seconds", "tokens") and v})
    max_steps = max(1, int(b["calls"]))

    rid = _new_id()
    log = _Log(rid)
    started, clock0 = time.time(), time.perf_counter()
    quiet = say or (lambda m: None)
    e = engine or core.Engine(root, "local_agent", brief[:200], cfg, review="auto", say=quiet)
    refused, changed_by_tool, used, turns, seen = [], [], [], [], set()
    _guard(e, files, refused)
    pinned = None
    if not ((e.cfg.get("roles") or {}).get(ROLE) or "").strip():  # a model the person assigned to the role wins over the tier
        m = e.tier_model(tier)
        if m:
            pinned = (e.pins.get(ROLE),)
            e.pins[ROLE] = m
    try:
        model = e.model_for(ROLE)
    except Exception:
        model = None
    log.meta(prompt=brief, mode="acceptEdits" if files else "plan", model=model, budget=0.0, max_turns=max_steps, root=root,
             resume=None, tag=tag, started=started, ended=None, status="running", cost=0.0, turns=0, session=None, error=None,
             pid=os.getpid(), by_claude=False, kind="local_agent", transport="local", tier=tier, template=name, files=files,
             tests=tests, local_budget=b)
    log.event(type="system.init", text=f"local agent · model {model or '?'} · tier {tier} · {len(tools)} tools · "
                                       f"own files: {', '.join(files) or 'none (read-only)'} · budget {b['calls']} calls, {b['seconds']} s")
    if on_start:
        try:
            on_start(rid)
        except Exception:
            pass
    base = preamble(brief, files, tools, bool(files))
    schema = arch_tools.schema(tools)
    status, why, answer, stall = "running", None, None, 0
    checks = {"ok": None, "status": "not run"}
    e.push_budget("local_agent", calls=b["calls"], seconds=b["seconds"], tokens=b.get("tokens"))
    try:
        for i in range(max_steps):
            if should_stop and should_stop():
                status, why = "stopped", "stopped from the page"
                break
            if stall >= stall_after:
                status, why = "stopped", f"stalled: {stall} steps in a row got nowhere"
                break
            if time.perf_counter() - clock0 >= float(b["seconds"]):   # also checked by Engine.call; this clock is finer on Windows
                status, why = "stopped", f"local_agent: {b['seconds']} seconds used up"
                break
            last = i == max_steps - 1
            user = base + ("\nWHAT YOU HAVE DONE SO FAR:\n" + _history(turns) if turns else "") + \
                ('\n\nTHIS IS YOUR LAST STEP: reply now with {"final": "..."}.' if last else "")
            r, j = e.ask_json(ROLE, user, schema=schema)
            if not isinstance(j, dict) or not (j.get("tool") or j.get("final")):
                stall += 1
                e.mark(r["id"], False, "unusable reply")
                turns.append((f"[step {i + 1}] (unusable reply)", "answer with one tool call as JSON, or your final report"))
                log.event(type="assistant", text="(the reply was not one tool call or a final answer)", model=model, error=True)
                continue
            if j.get("final") and not j.get("tool"):
                answer = str(j["final"]).strip()
                e.mark(r["id"], True)
                log.event(type="assistant", text=answer, model=model)
                status = "done"
                break
            tname, args = str(j.get("tool") or ""), j.get("args") if isinstance(j.get("args"), dict) else {}
            sig = tname + json.dumps(args, sort_keys=True, default=str)
            log.event(type="tool", name=tname, id=f"{rid}-{i + 1}", arg=_main_arg(tname, args),
                      text=json.dumps(args, ensure_ascii=False, default=str))
            outside = [p for p in write_targets(tname, args) if p and not owns(files, p)] if tname in WRITE_TOOLS else []
            if outside:
                refused.extend(outside)
                out, ok = (f"refused: {', '.join(outside)} is not your file. You may change only: {', '.join(files)}. "
                           "Nothing was changed."), False
            else:
                before = len(e.changed_files)
                out, ok = arch_tools.call(e, tname, args, tools)
                fresh = [f for f in e.changed_files[before:] if f not in changed_by_tool]
                changed_by_tool.extend(fresh)
                if fresh:
                    checks = {"ok": None, "status": "not run since the last change"}
                if ok and tname in WRITE_TOOLS and (out.startswith("refused") or "nothing was applied" in out):
                    ok = False
            if tname == "run_tests" and ok:
                t_ok, st = _test_status(out)
                checks = {"ok": t_ok, "status": st}
            repeated = sig in seen
            seen.add(sig)
            if repeated:
                out += ("\n\nYou already made this exact call and got this answer. Do something different, or give your final report.")
            stall = stall + 1 if (repeated or not ok) else 0
            used.append(tname)
            e.mark(r["id"], ok, "" if ok else out[:200])
            e._trace({"type": "tool", "t": time.time(), "flow": "local_agent", "step": "agent", "tool": tname, "call": r["id"],
                      "turn": i + 1, "args": json.loads(json.dumps(args, default=str)), "ok": ok, "result": out[:2000]})
            log.event(type="tool_result", id=f"{rid}-{i + 1}", error=not ok, text=out)
            turns.append((f"[step {i + 1}] {tname}({json.dumps(args, default=str)[:300]})", out))
            log.meta(turns=i + 1)
        else:
            status, why = "stopped", f"step budget used up ({max_steps} model calls)"
    except core.BudgetExceeded as ex:
        status, why = "stopped", str(ex)[:300]
    except core.LoopDetected as ex:
        status, why = "stopped", f"stalled: {str(ex)[:200]}"
    except core.LLMError as ex:
        status, why = "error", f"the local model could not answer: {str(ex)[:300]}"
    except Exception as ex:                                       # never a silent end: the run log says what happened
        status, why = "error", f"{type(ex).__name__}: {str(ex)[:300]}"
    finally:
        e.pop_budget()
        if pinned is not None:
            if pinned[0]:
                e.pins[ROLE] = pinned[0]
            else:
                e.pins.pop(ROLE, None)
    secs = round(time.time() - started, 2)
    steps = len(turns) + (1 if answer is not None else 0)
    changed = sorted(set(changed_by_tool))
    summary = (answer or why or "") + (f"\n\nchanged: {', '.join(changed)}" if changed else "") + \
        f"\nown tests: {'passed' if checks['ok'] else ('failed' if checks['ok'] is False else 'not run after the last change')}"
    log.event(type="result", text=summary, error=status != "done")
    log.meta(ended=time.time(), status=status, error=None if status == "done" else why, turns=steps, cost=0.0, n_events=log.n,
             changed=changed, refused=sorted(set(refused)), checks=checks, seconds=secs)
    if engine is None:
        try:
            e.finish(status == "done", None if status == "done" else why)
        except Exception:
            pass
    with _ID_LOCK:
        _TAKEN.discard(rid)
    return {"id": rid, "status": status, "why": why, "answer": answer, "steps": steps, "seconds": secs, "changed": changed,
            "refused": sorted(set(refused)), "checks": checks, "tools": used, "model": model, "tier": tier, "template": name,
            "files": files, "tests": tests, "by_claude": False}
