"""Local agent orchestration with the same shape as a Claude Code workflow: agent(), parallel(), pipeline(), phase().

What an agent IS (here and in Claude Code alike) -- no weights change, it is not a trained model; behaviour comes from these parts:
  1 model      a tier (worker | heavy | router) or a model id                     agents/<name>.yaml  model:, role:
  2 brief      a system prompt + a task prompt template                          agents/<name>.yaml  system:, task:
  3 tools      an allow-list of arch_tools.TOOLS                                 agents/<name>.yaml  tools:, writes:
  4 output     a JSON schema the final answer must match, or free text           agents/<name>.yaml  output:
  5 budgets    model calls / tokens / seconds, plus reasoning effort             agents/<name>.yaml  budget:, effort:, max_steps:
  6 context    fresh, or inherits the parent's results; capped in characters     agents/<name>.yaml  context:
  7 isolation  the files it owns (two agents never own the same file)            workflow entry      owns:
  8 place      its parent and phase in the run                                   workflow phases / phase()

Semantics mirror the Claude workflow tool:
  * agent(...)   one agent, run as an Engine tool loop on the local tier; returns its (schema-checked) result, or None on any failure.
  * parallel([...])  runs side by side, capped by the worker's llama-server slots (Settings > Server > parallel); a failed one is None.
  * pipeline(items, *stages)  every item flows through the stages on its own: no barrier between stages.
  * phase(title)  labels the agents that follow (also a context manager).
  * ownership is checked before anything runs; a clash raises OwnershipClash and nothing starts.
  * budgets go through Engine.push_budget, so an agent that runs out is stopped by the engine, and its result is None.

Telemetry: <data_dir>/orchestration/local/<run_id>/journal.jsonl (launched/phase/started/result/done, the same event names as a Claude
workflow journal) + agent-<id>.json per agent (brief, tools, tokens, cache figures, peak context, result, verdict). arch_orchestration
reads these next to the Claude transcripts with the same columns.

    python architect.py crew workflows/audit_build_verify.yaml --goal "..." [--owns a.py,b.py] [--fake]

Standard library only (YAML through arch_ci.load_yaml).
"""
import contextlib
import json
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
AGENTS_DIR = os.path.join(HERE, "agents")
WORKFLOWS_DIR = os.path.join(HERE, "workflows")
TIERS = ("worker", "heavy", "router")
EFFORTS = {"low": False, "medium": None, "high": True}      # -> Engine.call(think=...): off, the role's default, on
POLICIES = ("fresh", "inherit")
CATEGORIES = ("coding", "end-to-end", "agentic", "ui", "system/it", "engineering", "architecture", "ai/models", "data", "harness", "other")
KEYS = {"name", "description", "model", "role", "category", "system", "task", "tools", "writes", "output", "budget", "effort",
        "max_steps", "context", "owns"}
SCHEMA_BEGIN, SCHEMA_END = "RESULT SCHEMA (JSON):", "END OF SCHEMA"


class OwnershipClash(ValueError):
    """Two agents would own the same file. Raised before any of them runs."""


# ---------------------------------------------------------------- definitions

def _tools():
    import arch_tools
    return arch_tools.TOOLS


def validate(d):
    """An agent definition -> a list of problems in plain words (empty = it may run)."""
    import arch_roles
    if not isinstance(d, dict):
        return ["an agent definition must be a mapping"]
    p = []
    name = str(d.get("name") or "")
    if not re.match(r"^[a-z][a-z0-9_-]{0,40}$", name):
        p.append("name: lower-case letters, digits, - or _ (got %r)" % name[:40])
    extra = sorted(set(d) - KEYS)
    if extra:
        p.append("unknown keys: " + ", ".join(extra))
    m = str(d.get("model") or "")
    if m.startswith("claude:"):
        p.append("model: a crew agent runs on a local tier; claude: models are not allowed here")
    elif not (m in TIERS or ":" in m):
        p.append(f"model: a tier ({', '.join(TIERS)}) or a model id like name:tag (got {m!r})")
    if d.get("role", "generator") not in arch_roles.ROLES:
        p.append(f"role: not an engine role ({d.get('role')!r})")
    if d.get("category", "other") not in CATEGORIES:
        p.append(f"category: one of {', '.join(CATEGORIES)}")
    for k in ("system", "task"):
        if not isinstance(d.get(k), str) or not d[k].strip():
            p.append(f"{k}: a non-empty prompt is required")
    tools = d.get("tools")
    known = _tools()
    if not isinstance(tools, list) or not tools:
        p.append("tools: a non-empty list")
    else:
        bad = [t for t in tools if t not in known]
        if bad:
            p.append("tools: unknown " + ", ".join(map(str, bad)))
        writers = [t for t in tools if t in known and known[t]["writes"]]
        if writers and not d.get("writes"):
            p.append("tools: " + ", ".join(writers) + " change files, but writes is false")
    if not isinstance(d.get("writes", False), bool):
        p.append("writes: true or false")
    out = d.get("output", "text")
    if not (out == "text" or (isinstance(out, dict) and out.get("type") in ("object", "array", "string"))):
        p.append("output: 'text' or a JSON schema with a type")
    b = d.get("budget") or {}
    if not isinstance(b, dict) or not b or any(k not in ("calls", "tokens", "seconds") or isinstance(v, bool)
                                              or not isinstance(v, (int, float)) or v <= 0 for k, v in b.items()):
        p.append("budget: {calls, tokens, seconds} with numbers above 0 (at least one)")
    if d.get("effort", "medium") not in EFFORTS:
        p.append("effort: low, medium or high")
    ms = d.get("max_steps", 8)
    if isinstance(ms, bool) or not isinstance(ms, int) or not 1 <= ms <= 40:
        p.append("max_steps: 1..40")
    c = d.get("context") or {}
    if not isinstance(c, dict) or c.get("policy", "fresh") not in POLICIES or \
            not isinstance(c.get("max_chars", 8000), int) or c.get("max_chars", 8000) <= 0:
        p.append("context: {policy: fresh|inherit, max_chars: a number above 0}")
    if not isinstance(d.get("owns", []), list):
        p.append("owns: a list of files")
    return p


def _norm(d):
    d = dict(d)
    d.setdefault("role", "generator")
    d.setdefault("category", "other")
    d.setdefault("writes", False)
    d.setdefault("output", "text")
    d.setdefault("effort", "medium")
    d.setdefault("max_steps", 8)
    d["context"] = dict({"policy": "fresh", "max_chars": 8000}, **(d.get("context") or {}))
    d.setdefault("owns", [])
    d.setdefault("description", "")
    return d


def load_agent(name_or_path, folder=None):
    """agents/<name>.yaml (or a path, or an already-loaded dict) -> a validated definition. ValueError lists the problems."""
    import arch_ci
    if isinstance(name_or_path, dict):
        d, src = name_or_path, "(inline)"
    else:
        src = name_or_path if os.path.isfile(str(name_or_path)) else \
            os.path.join(folder or AGENTS_DIR, str(name_or_path) + ".yaml")
        with open(src, encoding="utf-8") as f:
            d = arch_ci.load_yaml(f.read())
    probs = validate(d)
    if probs:
        raise ValueError(f"{src}: " + "; ".join(probs))
    d = _norm(d)
    d["_source"] = src
    return d


def list_agents(folder=None):
    """-> {name: definition or {"problems": [...]}} for every agents/*.yaml."""
    out = {}
    folder = folder or AGENTS_DIR
    for fn in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if fn.endswith(".yaml"):
            try:
                d = load_agent(os.path.join(folder, fn))
                out[d["name"]] = d
            except Exception as e:
                out[fn[:-5]] = {"problems": [str(e)]}
    return out


# ---------------------------------------------------------------- ownership

def _owns_list(v):
    if isinstance(v, str):
        v = [x for x in re.split(r"[,\n]", v)]
    return [str(x).strip() for x in (v or []) if str(x).strip()]


def ownership_problems(groups):
    """groups: [(label, [files])] -> problems (a file outside the project, or a file -- or folder -- owned twice)."""
    import arch_fleet
    seen, probs = [], []
    for label, files in groups:
        for f in files:
            shown, key = arch_fleet._clean_path(f)
            if key is None:
                probs.append(f"{label}: {shown!r} is not a plain path inside the project")
                continue
            for other, okey, oshown in seen:
                if other != label and arch_fleet._overlaps(key, okey):
                    probs.append(f"{label} and {other} both own {shown if len(shown) >= len(oshown) else oshown}")
            seen.append((label, key, shown))
    return probs


def _owned_engine_class():
    import arch_core as core

    class OwnedEngine(core.Engine):
        """An Engine whose writes are limited to the files this agent owns. Everything else (review mode, protected paths, undo) is
        the Engine's own."""
        owns = ()

        def _mine(self, rel):
            import arch_fleet
            key = arch_fleet._clean_path(rel)[1]
            mine = [arch_fleet._clean_path(o)[1] for o in self.owns]
            return bool(key) and any(k and (key == k or key.startswith(k + "/")) for k in mine)

        def apply(self, reply_text, label=""):
            blocks = core.parse_blocks(reply_text) or []
            theirs = sorted({str(b.get("path") or b.get("rel") or "") for b in blocks} - {""})
            bad = [p for p in theirs if not self._mine(p)]
            if bad:
                self.say("[-] refused: not owned by this agent: " + ", ".join(bad))
                return [{"status": "error", "rel": p, "msg": "not owned by this agent (it owns: " + (", ".join(self.owns) or "nothing") + ")"}
                        for p in bad]
            return super().apply(reply_text, label)

        def put_file(self, rel, text, label="", quiet=False):
            if not self._mine(rel):
                self.say(f"[-] refused: {rel} is not owned by this agent")
                return False
            return super().put_file(rel, text, label, quiet)
    return OwnedEngine


# ---------------------------------------------------------------- the crew

def _quiet(_m):
    pass


class _Keep(dict):
    def __missing__(self, k):
        return "{" + k + "}"


def render(text, vars_):
    """{name} -> value; unknown names stay as they are (a task may quote braces)."""
    try:
        return str(text or "").format_map(_Keep({k: (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str))
                                                 for k, v in (vars_ or {}).items()}))
    except (ValueError, IndexError, AttributeError):
        return str(text or "")


def _cut(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n] + " ..."


def local_model(e, d):
    """The model an agent runs on: its model id, else the person's model for its role (when local), else its tier's model."""
    if d["model"] not in TIERS:
        return "" if d["model"].startswith("claude:") else d["model"]
    own = ((e.cfg.get("roles") or {}).get(d["role"]) or "").strip()
    if own.startswith("tier:"):
        own = e.tier_model(own[5:])
    if own and not own.startswith("claude:") and d["model"] != "heavy":
        return own
    m = e.tier_model(d["model"])
    return "" if m.startswith("claude:") else m


class Crew:
    """One local orchestration run. Thread-safe: parallel() and pipeline() call agent() from worker threads."""

    def __init__(self, root, goal="", workflow="adhoc", cfg=None, slots=None, data_dir=None, say=None, engine_factory=None,
                 run_id=None, description="", vars=None):
        import arch_core as core
        self.root = os.path.abspath(root)
        self.goal, self.workflow, self.description = goal, workflow, description
        self.cfg = cfg or core.load_config(self.root)
        self.say = say or _quiet
        self.vars = dict(vars or {})
        srv = self.cfg.get("server") or {}
        self.slots = max(1, int(slots or srv.get("parallel") or 1))
        self._sem = threading.Semaphore(self.slots)
        self._lock = threading.RLock()
        self._local = threading.local()
        self.running = 0
        self.peak_concurrency = 0
        self.records = []
        self._n = 0
        self._owners = {}                       # agent id -> owned files, while it runs
        self.engine_factory = engine_factory
        dd = data_dir or core.data_dir()
        self.run_id = run_id or time.strftime("%Y%m%d_%H%M%S") + f"_{os.getpid() % 10000:04d}_{re.sub(r'[^A-Za-z0-9]+', '-', workflow)[:30]}"
        self.dir = os.path.join(dd, "orchestration", "local", self.run_id)
        os.makedirs(self.dir, exist_ok=True)
        self.phase_title = None
        self._event({"type": "launched", "workflow": workflow, "description": description, "goal": goal, "root": self.root,
                     "slots": self.slots})

    # -- journal
    def _event(self, rec):
        rec = dict(rec, t=round(time.time(), 3))
        with self._lock:
            with open(os.path.join(self.dir, "journal.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    @contextlib.contextmanager
    def _phase_cm(self, title, prev):
        try:
            yield self
        finally:
            self.phase_title = prev

    def phase(self, title):
        """Label the agents that follow. Usable plainly (crew.phase('Audit')) or as `with crew.phase('Audit'):`."""
        prev = self.phase_title
        self.phase_title = str(title)
        self._event({"type": "phase", "title": self.phase_title})
        return self._phase_cm(title, prev)

    # -- engine
    def _engine(self, d, owns):
        if self.engine_factory:
            return self.engine_factory(d, owns)
        cls = _owned_engine_class()
        e = cls(self.root, "crew", self.goal, self.cfg, review="auto", say=_quiet, persist=False)   # persist=False: never starts a server
        e.owns = tuple(owns)
        return e

    def _claim(self, aid, label, owns):
        with self._lock:
            probs = ownership_problems([(label, owns)] + [(f"agent {k}", v) for k, v in self._owners.items()])
            if probs:
                raise OwnershipClash("; ".join(probs))
            self._owners[aid] = list(owns)

    # -- one agent
    def agent(self, defn, task="", label=None, owns=None, context=None, vars=None, parent=None, phase=None, budget=None):
        """Run one agent. -> its result (a dict/list matching the output schema, or text), or None when it failed for any reason.
        The record (tokens, cache, verdict ...) is appended to self.records and written as agent-<id>.json."""
        d = load_agent(defn)
        b = dict(d.get("budget") or {}, **(budget or {}))
        owns = _owns_list(owns if owns is not None else d.get("owns"))
        with self._lock:
            self._n += 1
            aid = f"a{self._n:03d}"
        label = label or d["name"]
        ph = phase or getattr(self._local, "phase", None) or self.phase_title
        parent = parent or getattr(self._local, "parent", None)
        rec = {"agentId": aid, "label": label, "agent": d["name"], "phase": ph, "parent": parent, "model": d["model"],
               "role": d["role"], "category": d["category"], "owns": owns, "effort": d["effort"], "budget": b,
               "context_policy": d["context"]["policy"], "output": "schema" if isinstance(d["output"], dict) else "text",
               "tools_allowed": list(d["tools"]), "tools": {}, "edited": [], "calls": [], "verdict": None, "result": None,
               "why": None, "start": time.time()}
        if not owns and d["writes"]:
            rec["note"] = "owns nothing: its write tools were removed"
        try:
            self._claim(aid, label, owns)
        except OwnershipClash as ex:
            rec.update(verdict="refused", why=str(ex))
            self._finish(rec)
            raise
        self._event({"type": "started", "agentId": aid, "label": label, "phase": ph, "agent": d["name"], "parent": parent})
        e = None
        try:
            with self._sem:
                with self._lock:
                    self.running += 1
                    self.peak_concurrency = max(self.peak_concurrency, self.running)
                try:
                    e = self._engine(d, owns)
                    rec["result"], rec["verdict"], rec["why"] = self._loop(e, d, task, owns, context, vars, b, rec)
                finally:
                    with self._lock:
                        self.running -= 1
        except Exception as ex:                        # a failing agent is None, never a crash of the run
            rec.update(verdict="failed", why=f"{type(ex).__name__}: {str(ex)[:300]}", result=None)
        finally:
            with self._lock:
                self._owners.pop(aid, None)
            if e is not None:
                self._harvest(e, rec)
        self._finish(rec)
        return rec["result"] if rec["verdict"] == "ok" else None

    def _brief(self, d, task, owns, context, vars_):
        v = dict(self.vars)
        v.update(goal=self.goal, task=task or "", prev="", focus="")
        v.update(vars_ or {})
        if d["context"]["policy"] == "inherit" and context is not None:
            v["prev"] = _cut(context if isinstance(context, str) else json.dumps(context, ensure_ascii=False, default=str),
                             d["context"]["max_chars"])
        body = render(d["task"], v).strip()
        body = f"[crew-agent:{d['name']}]\n" + body
        if owns:
            body += "\n\nYOU OWN THESE FILES (any other write is refused): " + ", ".join(owns)
        if isinstance(d["output"], dict):
            body += (f"\n\nWhen you are done, your final answer must be ONE JSON value matching this schema, as the text of \"final\".\n"
                     f"{SCHEMA_BEGIN}\n{json.dumps(d['output'], sort_keys=True)}\n{SCHEMA_END}")
        return render(d["system"], v).strip(), body

    def _loop(self, e, d, task, owns, context, vars_, budget, rec):
        """The tool loop (same contract as arch_dataflow's tool_loop step: one tool call or a final answer per turn, checked in code).
        -> (result, verdict, why)."""
        import arch_core as core
        import arch_schemas
        import arch_tools
        allowed = [t for t in d["tools"] if owns or not arch_tools.TOOLS[t]["writes"]]
        system, base = self._brief(d, task, owns, context, vars_)
        rec["brief"] = _cut(system + "\n---\n" + base, 1500)
        role = d["role"]
        m = local_model(e, d)                    # always pinned: a crew agent runs on a local tier, never on a cloud tier
        if not m:
            return None, "failed", f"no local model for tier/model {d['model']!r} (Settings > Tiers)"
        e.pins[role] = m
        rec["model"] = m
        schema = arch_tools.schema(allowed)
        tool_text = arch_tools.describe(allowed)
        think = EFFORTS[d["effort"]]
        turns, answer = [], None
        e.push_budget("agent " + rec["label"], **budget)
        try:
            for i in range(d["max_steps"]):
                last = i == d["max_steps"] - 1
                user = (base + "\n\nTOOLS (reply with {\"tool\": name, \"args\": {...}} or {\"final\": \"...\"}):\n" + tool_text +
                        ("\n\nWHAT YOU HAVE DONE SO FAR:\n" + "\n".join(turns[-6:]) if turns else "") +
                        ("\n\nTHIS IS YOUR LAST STEP: answer now with {\"final\": \"...\"}." if last else ""))
                r = e.call(role, user, system, json_out=True, schema=schema, think=think)
                j = core.parse_json(r["text"])
                if not isinstance(j, dict) or arch_schemas.errors(j, schema):
                    turns.append(f"[step {i + 1}] your reply was not usable; answer with one tool call or your final answer")
                    e.mark(r["id"], False, "unusable reply")
                    continue
                if j.get("final") is not None and not j.get("tool"):
                    answer = j["final"]
                    e.mark(r["id"], True)
                    break
                name, args = str(j.get("tool") or ""), j.get("args") or {}
                out, ok = arch_tools.call(e, name, args, allowed)
                rec["tools"][name] = rec["tools"].get(name, 0) + 1
                e.mark(r["id"], ok, "" if ok else out[:200])
                turns.append(f"[step {i + 1}] {name}({json.dumps(args, default=str)[:300]})\nRESULT:\n{out[:3000]}")
        except core.BudgetExceeded as ex:
            return None, "budget", str(ex)
        finally:
            e.pop_budget()
        if answer is None:
            return None, "failed", "no final answer within the step limit"
        if isinstance(d["output"], dict):
            val = answer
            if isinstance(answer, str):
                val = core.parse_json(answer)
            if val is None:
                return None, "schema", "the final answer is not JSON"
            bad = arch_schemas.errors(val, d["output"])
            if bad:
                return None, "schema", "the final answer breaks the schema: " + "; ".join(bad[:3])
            return val, "ok", None
        return str(answer), "ok", None

    def _harvest(self, e, rec):
        """Per-call numbers from the engine's own trace (the same parser the orchestration page uses), then the throwaway run folder is
        removed: the crew journal keeps what matters."""
        import arch_orchestration as orch
        tp = os.path.join(getattr(e, "run_dir", "") or "", "trace.jsonl")
        if os.path.isfile(tp):
            r = orch.parse_run(tp)
            rec["calls"] = [{k: c.get(k) for k in ("tier", "model", "tokens_in", "tokens_out", "cached_exact", "prefix_reused", "ctx_pct", "secs")}
                            for c in r.get("calls") or []]
            try:
                shutil.copyfile(tp, os.path.join(self.dir, f"agent-{rec['agentId']}.trace.jsonl"))
            except OSError:
                pass
        rec["edited"] = sorted(set(getattr(e, "changed_files", []) or []))
        if not getattr(e, "persist_runs", True) and getattr(e, "run_dir", None) and "arch_tmp_" in e.run_dir:
            shutil.rmtree(e.run_dir, ignore_errors=True)

    def _finish(self, rec):
        rec["end"] = time.time()
        rec["secs"] = round(rec["end"] - rec["start"], 3)
        calls = rec.get("calls") or []
        rec["totals"] = {"calls": len(calls), "tokens_in": sum(int(c.get("tokens_in") or 0) for c in calls),
                         "tokens_out": sum(int(c.get("tokens_out") or 0) for c in calls),
                         "cached_exact": sum(1 for c in calls if c.get("cached_exact")),
                         "prefix_reused": sum(int(c.get("prefix_reused") or 0) for c in calls),
                         "peak_ctx": max([int(c.get("tokens_in") or 0) for c in calls] or [0]),
                         "peak_ctx_pct": max([int(c.get("ctx_pct") or 0) for c in calls] or [0])}
        with self._lock:
            self.records.append(rec)
            with open(os.path.join(self.dir, f"agent-{rec['agentId']}.json"), "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=1, default=str)
        self._event({"type": "result", "agentId": rec["agentId"], "verdict": rec["verdict"], "why": rec["why"],
                     "result": rec["result"]})

    # -- composition
    def _thunk(self, spec):
        if callable(spec):
            return spec
        if isinstance(spec, dict):
            s = dict(spec)
            return lambda: self.agent(s.get("agent"), s.get("task", ""), label=s.get("label"), owns=s.get("owns"),
                                      context=s.get("context"), vars=s.get("vars"), budget=s.get("budget"))
        raise TypeError("parallel() takes callables or {agent, task, ...} dicts")

    def _specs_owns(self, specs):
        groups = []
        for i, s in enumerate(specs):
            if isinstance(s, dict) and s.get("agent") is not None:
                d = load_agent(s["agent"])
                groups.append((s.get("label") or f"{d['name']}#{i + 1}", _owns_list(s.get("owns") if s.get("owns") is not None else d.get("owns"))))
        return groups

    def parallel(self, specs):
        """Run side by side (at most `slots` model-using agents at once). -> results in order; a failed one is None.
        Ownership of every dict spec is checked first; a clash raises OwnershipClash and nothing runs."""
        specs = list(specs)
        probs = ownership_problems(self._specs_owns(specs))
        if probs:
            self._event({"type": "refused", "phase": self.phase_title, "why": "; ".join(probs)})
            raise OwnershipClash("; ".join(probs))
        thunks = [self._thunk(s) for s in specs]
        ph = self.phase_title

        def run(t):
            self._local.phase = ph
            try:
                return t()
            except OwnershipClash:
                raise
            except Exception:
                return None
        if not thunks:
            return []
        with ThreadPoolExecutor(max_workers=max(1, len(thunks))) as ex:    # the semaphore in agent() is the real cap
            return list(ex.map(run, thunks))

    def pipeline(self, items, *stages):
        """Each item goes through every stage in order, independently of the others (no barrier: item 1 may be in stage 3 while item 2
        is still in stage 1). A stage is fn(prev, item) or a {agent, task, ...} dict whose task may use {item} and {prev}. A stage that
        returns None ends that item's chain. -> the last stage's result per item."""
        items = list(items)
        ph = self.phase_title

        def stage_fn(st):
            if callable(st):
                return st
            s = dict(st)
            return lambda prev, item: self.agent(s.get("agent"), render(s.get("task", ""), {"item": item}), label=render(s.get("label") or "", {"item": item}) or None,
                                                 owns=s.get("owns"), context=prev, vars=dict(s.get("vars") or {}, item=item), budget=s.get("budget"))
        fns = [stage_fn(s) for s in stages]

        def chain(item):
            self._local.phase = ph
            prev = item
            for fn in fns:
                try:
                    prev = fn(prev, item)
                except OwnershipClash:
                    raise
                except Exception:
                    prev = None
                if prev is None:
                    return None
            return prev
        if not items:
            return []
        with ThreadPoolExecutor(max_workers=max(1, len(items))) as ex:
            return list(ex.map(chain, items))

    def close(self, verdict=None):
        bad = sum(1 for r in self.records if r["verdict"] != "ok")
        verdict = verdict or ("pass" if self.records and not bad else ("fail" if self.records else "empty"))
        self._event({"type": "done", "verdict": verdict, "agents": len(self.records), "failed": bad,
                     "peak_concurrency": self.peak_concurrency})
        return verdict


# ---------------------------------------------------------------- module-level API (one current crew, like the workflow tool)

_CURRENT = [None]


def start(root, goal="", **kw):
    _CURRENT[0] = Crew(root, goal, **kw)
    return _CURRENT[0]


def _cur():
    if _CURRENT[0] is None:
        raise RuntimeError("no crew started: call arch_squad.start(root, goal) first")
    return _CURRENT[0]


def agent(defn, task="", **kw):
    return _cur().agent(defn, task, **kw)


def parallel(specs):
    return _cur().parallel(specs)


def pipeline(items, *stages):
    return _cur().pipeline(items, *stages)


def phase(title):
    return _cur().phase(title)


# ---------------------------------------------------------------- workflows as data

def load_workflow(path):
    import arch_ci
    with open(path, encoding="utf-8") as f:
        w = arch_ci.load_yaml(f.read())
    probs = validate_workflow(w)
    if probs:
        raise ValueError(f"{path}: " + "; ".join(probs))
    return w


def validate_workflow(w):
    if not isinstance(w, dict) or not w.get("name") or not isinstance(w.get("phases"), list) or not w["phases"]:
        return ["a workflow needs a name and a non-empty list of phases"]
    p = []
    for i, ph in enumerate(w["phases"]):
        where = f"phase {i + 1}"
        if not isinstance(ph, dict) or not ph.get("title"):
            p.append(f"{where}: needs a title")
            continue
        kinds = [k for k in ("agent", "parallel", "pipeline") if k in ph]
        if len(kinds) != 1:
            p.append(f"{where}: exactly one of agent, parallel, pipeline")
            continue
        entries = [ph] if kinds[0] == "agent" else (ph["parallel"] if kinds[0] == "parallel" else (ph["pipeline"] or {}).get("stages"))
        if not isinstance(entries, list) or not entries:
            p.append(f"{where}: {kinds[0]} needs entries")
            continue
        for en in entries:
            try:
                load_agent(en.get("agent") if isinstance(en, dict) else None)
            except Exception as e:
                p.append(f"{where}: {e}")
    return p


def run_workflow(w, root, goal, vars=None, **kw):
    """Run a workflow dict. Each phase gets the previous phase's results as {prev} (inherit policy). -> (verdict, crew)."""
    vars_ = dict(vars or {}, goal=goal)
    crew = Crew(root, goal, workflow=w["name"], description=w.get("description", ""), vars=vars_, **kw)
    prev = None
    for ph in w["phases"]:
        crew.phase(ph["title"])

        def spec(en):
            v = dict(vars_, **(en.get("vars") or {}))
            owns = en.get("owns")
            return {"agent": en["agent"], "task": render(en.get("task", ""), v), "label": render(en.get("label") or "", v) or None,
                    "owns": render(owns, v) if isinstance(owns, str) else owns, "context": prev, "vars": en.get("vars"),
                    "budget": en.get("budget")}
        try:
            if "parallel" in ph:
                res = crew.parallel([spec(en) for en in ph["parallel"]])
                prev = [r for r in res if r is not None]
            elif "pipeline" in ph:
                pl = ph["pipeline"]
                res = crew.pipeline(pl.get("items") or [], *[spec(s) for s in pl["stages"]])
                prev = [r for r in res if r is not None]
            else:
                s = spec(ph)
                prev = crew.agent(s["agent"], s["task"], label=s["label"], owns=s["owns"], context=s["context"], budget=s["budget"])
        except OwnershipClash as ex:
            crew.say(f"[x] {ph['title']}: refused before running: {ex}")
            return crew.close("refused"), crew
        crew.say(f"[{'ok' if prev not in (None, []) else '-'}] {ph['title']}: " + _cut(json.dumps(prev, ensure_ascii=False, default=str), 200))
    return crew.close(), crew


# ---------------------------------------------------------------- the scripted fake model (tests and --fake)

def example_for(schema):
    """The smallest value that satisfies a JSON schema (enough for arch_schemas.errors to accept it)."""
    t = (schema or {}).get("type")
    if "enum" in (schema or {}):
        return schema["enum"][0]
    if t == "object":
        props = schema.get("properties") or {}
        return {k: example_for(props.get(k) or {}) for k in schema.get("required") or props}
    if t == "array":
        return [example_for(schema.get("items") or {"type": "string"})] if schema.get("items") else []
    if t == "integer":
        return 1
    if t == "number":
        return 1.0
    if t == "boolean":
        return True
    return "fake: done"


def fake_reply(user):
    """What the scripted fake model answers to a crew prompt. Stateless: the turn is read off the prompt.
    Markers a test can put in the task: FAKE:GARBAGE (never usable), FAKE:BADSCHEMA (final breaks the schema), FAKE:WRITE <path> (writes
    that file through the apply tool), FAKE:SLOW (the fake server waits a little: see FakeHandler)."""
    if "FAKE:GARBAGE" in user:
        return "this is not json"
    done = "WHAT YOU HAVE DONE SO FAR" in user
    m = re.search(r"FAKE:WRITE (\S+)", user)
    if m and "apply(" not in user.split("WHAT YOU HAVE DONE SO FAR")[-1]:
        return json.dumps({"tool": "apply", "args": {"blocks": f"FILE: {m.group(1)}\n```python\n# written by a crew agent\nX = 1\n```\n"}})
    if not done:
        return json.dumps({"tool": "ls", "args": {}})
    if "FAKE:BADSCHEMA" in user:
        return json.dumps({"final": json.dumps({"nope": 1})})
    s = re.search(re.escape(SCHEMA_BEGIN) + r"\s*(\{.*?\})\s*" + re.escape(SCHEMA_END), user, re.S)
    if s:
        try:
            return json.dumps({"final": json.dumps(example_for(json.loads(s.group(1))))})
        except ValueError:
            pass
    return json.dumps({"final": "fake: looked at the project, nothing more to say"})


def start_fake():
    """Start the scripted fake model (an Ollama-shaped HTTP server on 127.0.0.1, random port) and point OLLAMA_HOST at it.
    -> (server, cfg overrides). It never touches a real model server."""
    from http.server import ThreadingHTTPServer
    import arch_selftest

    class FakeHandler(arch_selftest.Fake):
        state = {"calls": [], "n": {}}

        def script(self, role, user, full, st):
            if "FAKE:SLOW" in user:
                time.sleep(0.25)
            return fake_reply(user)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv.server_address[1]}"
    os.environ.setdefault("ARCHITECT_NO_CACHE", "1")
    import arch_roles
    over = {"roles": {r: "fake:7b" for r in arch_roles.ROLES}, "tiers": {"worker": "fake:7b", "heavy": "fake:7b", "router": "fake:7b"},
            "server": {"kind": "ollama"}, "behavior": {"playwright": "off"},
            "online": {"enabled": False, "cloud_tier": "off"}}
    return srv, over


# ---------------------------------------------------------------- CLI

def main(argv=None):
    import argparse
    import arch_core as core
    ap = argparse.ArgumentParser(prog="architect.py crew", description="Run a local agent workflow (agents/*.yaml, workflows/*.yaml).")
    ap.add_argument("workflow", nargs="?", help="a workflows/*.yaml file (or its name)")
    ap.add_argument("--goal", default="", help="what the whole job is for")
    ap.add_argument("--owns", default="", help="comma-separated files the builder may change")
    ap.add_argument("--root", default=None, help="project folder (default: current folder)")
    ap.add_argument("--slots", type=int, default=None, help="agents at once (default: Settings > Server > parallel)")
    ap.add_argument("--fake", action="store_true", help="use the scripted fake model (no real model is loaded)")
    ap.add_argument("--agents", action="store_true", help="list and validate agents/*.yaml, then stop")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.agents or not a.workflow:
        rows = list_agents()
        for n, d in rows.items():
            print(f"{'[x]' if 'problems' in d else '[ok]'} {n}: " + ("; ".join(d['problems']) if 'problems' in d else
                  f"{d['model']}/{d['role']}, tools {', '.join(d['tools'])}, output {'schema' if isinstance(d['output'], dict) else 'text'}"))
        return 0 if all("problems" not in d for d in rows.values()) else 1
    path = a.workflow
    if not os.path.isfile(path):
        cand = os.path.join(WORKFLOWS_DIR, path if path.endswith(".yaml") else path + ".yaml")
        path = cand if os.path.isfile(cand) else path
    w = load_workflow(path)
    root = os.path.abspath(a.root or os.getcwd())
    cfg = core.load_config(root)
    srv = None
    if a.fake:
        srv, over = start_fake()
        cfg = core.deep_merge(cfg, over)
    try:
        verdict, crew = run_workflow(w, root, a.goal, vars={"owns": a.owns}, cfg=cfg, slots=a.slots, say=print)
    finally:
        if srv is not None:
            srv.shutdown()
    out = {"verdict": verdict, "run_id": crew.run_id, "journal": os.path.join(crew.dir, "journal.jsonl"),
           "agents": [{k: r.get(k) for k in ("agentId", "label", "phase", "verdict", "why", "totals")} for r in crew.records]}
    print(json.dumps(out, indent=1, default=str) if a.json else
          f"{w['name']}: {verdict}; {len(crew.records)} agents, peak {crew.peak_concurrency} at once (cap {crew.slots}); journal {out['journal']}")
    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
