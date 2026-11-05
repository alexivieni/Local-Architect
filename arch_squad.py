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
        "max_steps", "context", "owns", "code"}
CODE_AGENTS = ("spec",)                                    # agents whose work is done by code, not a model (code: spec -> arch_spec.spec)
OVERRIDABLE = ("model", "role", "category", "description", "system", "task", "tools", "writes", "output", "budget", "effort",
               "max_steps", "context", "owns")
FIELD_INFO = {"model": "a tier (worker | heavy | router) or a local model id (name:tag)", "role": "an engine role (arch_roles.ROLES)",
              "category": "one of " + ", ".join(CATEGORIES), "description": "one line for people", "system": "the system prompt",
              "task": "the task template ({goal}, {task}, {prev}, {focus}, workflow vars)", "tools": "allowed arch_tools names",
              "writes": "may it change files (only the ones it owns)", "output": "'text' or a JSON schema",
              "budget": "{calls, tokens, seconds}", "effort": "low | medium | high (reasoning on/default/off)",
              "max_steps": "tool-loop turns, 1..40", "context": "{policy: fresh|inherit, max_chars}", "owns": "files it may change by default"}
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
    if d.get("code") is not None and d.get("code") not in CODE_AGENTS:
        p.append("code: one of " + ", ".join(CODE_AGENTS))
    return p


def _plain(d):
    """A definition without the bookkeeping keys load_agent/list_agents add (so it can be validated or saved again)."""
    return {k: v for k, v in d.items() if not str(k).startswith("_") and k not in ("fields", "problems")}


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
        d, src = _plain(name_or_path), name_or_path.get("_source") or "(inline)"
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
                d["fields"] = list(OVERRIDABLE)
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


# ---------------------------------------------------------------- run-time overrides

def apply_overrides(d, overrides):
    """A loaded definition + {"<agent name>" or "*": {field: value}} -> (new definition, the fields applied, problems).
    budget and context merge into the definition's own; every other field replaces it. The result is validated again, so an override
    can never put a crew agent on claude: or give a read-only agent a write tool."""
    applied = {}
    for key in ("*", d["name"]):
        for k, v in ((overrides or {}).get(key) or {}).items():
            applied[k] = v
    if not applied:
        return d, {}, []
    bad = sorted(k for k in applied if k not in OVERRIDABLE)
    if bad:
        return d, applied, ["not overridable: " + ", ".join(bad)]
    nd = dict(d)
    for k, v in applied.items():
        nd[k] = dict(d.get(k) or {}, **v) if k in ("budget", "context") and isinstance(v, dict) else v
    probs = validate(_plain(nd))
    if probs:
        return d, applied, probs
    src = d.get("_source")
    nd = _norm(_plain(nd))
    nd["_source"] = src
    return nd, applied, []


def _value(s):
    s = s.strip()
    try:
        return json.loads(s)
    except ValueError:
        low = s.lower()
        return {"true": True, "false": False, "yes": True, "no": False}.get(low, s)


def parse_sets(items):
    """CLI --set values -> an overrides dict for run_workflow.
    "coder.model=qwen3:8b" -> {"agents": {"coder": {"model": "qwen3:8b"}}}; "coder.budget.calls=5" nests; "*.effort=low" means every
    agent; "slots=2" is top level; "coder.tools=read,grep" is a list. Values are JSON when they parse (numbers, true, [..]), else text.
    ValueError on a malformed item."""
    out = {"agents": {}}
    for raw in items or []:
        if "=" not in str(raw):
            raise ValueError(f"--set wants name.field=value (got {raw!r})")
        key, val = str(raw).split("=", 1)
        parts = [x.strip() for x in key.strip().split(".") if x.strip()]
        v = _value(val)
        if parts == ["slots"]:
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise ValueError("slots=N with N a whole number above 0")
            out["slots"] = v
            continue
        if len(parts) < 2:
            raise ValueError(f"--set wants agent.field=value (got {raw!r})")
        agent_name, field, rest = parts[0], parts[1], parts[2:]
        if field not in OVERRIDABLE:
            raise ValueError(f"{field!r} is not an agent field (one of {', '.join(OVERRIDABLE)})")
        if field in ("tools", "owns") and isinstance(v, str):
            v = [x.strip() for x in v.split(",") if x.strip()]
        slot = out["agents"].setdefault(agent_name, {})
        if rest:
            cur = slot.setdefault(field, {})
            for r in rest[:-1]:
                cur = cur.setdefault(r, {})
            cur[rest[-1]] = v
        else:
            slot[field] = v
    return out


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
                 run_id=None, description="", vars=None, overrides=None):
        import arch_core as core
        self.root = os.path.abspath(root)
        self.goal, self.workflow, self.description = goal, workflow, description
        self.cfg = cfg or core.load_config(self.root)
        self.say = say or _quiet
        self.vars = dict(vars or {})
        self.overrides = {str(k): dict(v or {}) for k, v in ((overrides or {}).get("agents") or {}).items()}
        srv = self.cfg.get("server") or {}
        slots = slots or (overrides or {}).get("slots")
        self.slots = max(1, int(slots or srv.get("parallel") or 1))
        self.phase_times = []
        self.loop_counts = {}
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
                     "slots": self.slots, "overrides": {"agents": self.overrides, "slots": (overrides or {}).get("slots")}})

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
        d, applied, oprobs = apply_overrides(d, self.overrides)
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
               "why": None, "start": time.time(), "overrides": applied}
        if oprobs:
            rec.update(verdict="refused", why="overrides break the definition: " + "; ".join(oprobs))
            self._finish(rec)
            return None
        if d.get("code"):
            rec["model"] = "code"
            self._event({"type": "started", "agentId": aid, "label": label, "phase": ph, "agent": d["name"], "parent": parent,
                         "overrides": applied})
            try:
                rec["result"], rec["verdict"], rec["why"] = self._code_agent(d, task, context, vars)
            except Exception as ex:
                rec.update(verdict="failed", why=f"{type(ex).__name__}: {str(ex)[:300]}", result=None)
            self._finish(rec)
            return rec["result"] if rec["verdict"] == "ok" else None
        if not owns and d["writes"]:
            rec["note"] = "owns nothing: its write tools were removed"
        try:
            self._claim(aid, label, owns)
        except OwnershipClash as ex:
            rec.update(verdict="refused", why=str(ex))
            self._finish(rec)
            raise
        self._event({"type": "started", "agentId": aid, "label": label, "phase": ph, "agent": d["name"], "parent": parent,
                     "overrides": applied})
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

    def _code_agent(self, d, task, context, vars_):
        """An agent whose work is code (no model): code: spec -> arch_spec.spec(goal + task). Its output schema is still checked."""
        import arch_schemas
        if d["code"] == "spec":
            import arch_spec
            val = arch_spec.spec((self.goal + " " + (task or "")).strip())
        else:
            return None, "failed", f"unknown code agent {d['code']!r}"
        if isinstance(d["output"], dict):
            bad = arch_schemas.errors(val, d["output"])
            if bad:
                return None, "schema", "the code result breaks the schema: " + "; ".join(bad[:3])
        return val, "ok", None

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

PHASE_KINDS = ("agent", "parallel", "pipeline", "loop", "panel", "code")
LOOP_MAX = 10


def _code_dedupe_findings(prev, ctx):
    """Merge the findings of several reviewers: one entry per (file, line, problem start); each counts the reviews that saw it."""
    rows = prev if isinstance(prev, list) else [prev]
    seen, out = {}, []
    for r in rows:
        if not isinstance(r, dict):
            continue
        for f in r.get("findings") or []:
            if not isinstance(f, dict):
                continue
            key = (str(f.get("file") or "").replace("\\", "/").lower(), f.get("line"),
                   re.sub(r"\W+", " ", str(f.get("problem") or "")).strip().lower()[:60])
            if key in seen:
                seen[key]["seen_by"] += 1
                continue
            g = dict(f, seen_by=1)
            seen[key] = g
            out.append(g)
    return {"findings": out, "summary": f"{len(out)} distinct findings from {len(rows)} reviews"}


def _code_collect(prev, ctx):
    return {"results": prev if isinstance(prev, list) else [prev]}


CODE_STEPS = {"dedupe_findings": _code_dedupe_findings, "collect": _code_collect}


def _resolve_workflow(path):
    p = str(path)
    if os.path.isfile(p):
        return p
    cand = os.path.join(WORKFLOWS_DIR, p if p.endswith(".yaml") else p + ".yaml")
    return cand if os.path.isfile(cand) else p


def load_workflow(path):
    import arch_ci
    path = _resolve_workflow(path)
    with open(path, encoding="utf-8") as f:
        w = arch_ci.load_yaml(f.read())
    probs = validate_workflow(w)
    if probs:
        raise ValueError(f"{path}: " + "; ".join(probs))
    return w


def _phase_kind(ph):
    kinds = [k for k in PHASE_KINDS if k in ph]
    return kinds[0] if len(kinds) == 1 else None


def _check_expr(expr):
    """None when expr is an expression arch_dataflow's safe evaluator accepts, else the reason."""
    import ast
    import arch_dataflow
    try:
        ast.parse(str(expr), mode="eval")
    except SyntaxError as e:
        return f"not an expression: {e.msg}"
    try:
        arch_dataflow.evaluate(expr, {})           # the whitelist check runs first; missing names are nothing
    except ValueError as e:
        return str(e)
    except Exception:
        pass
    return None


def _validate_phase(ph, where, need_title=True, depth=0):
    p = []
    if not isinstance(ph, dict) or (need_title and not ph.get("title")):
        return [f"{where}: needs a title"]
    kind = _phase_kind(ph)
    if kind is None:
        return [f"{where}: exactly one of " + ", ".join(PHASE_KINDS)]
    if "when" in ph:
        bad = _check_expr(ph["when"])
        if bad:
            p.append(f"{where}: when: {bad}")
    if kind == "code":
        if ph["code"] not in CODE_STEPS:
            p.append(f"{where}: code: one of " + ", ".join(CODE_STEPS))
        return p
    if kind == "loop":
        lp = ph["loop"] if isinstance(ph["loop"], dict) else {}
        st = lp.get("stages")
        if depth or not isinstance(st, list) or not st:
            return p + [f"{where}: loop needs stages (and loops do not nest)"]
        mx = lp.get("max", 2)
        if isinstance(mx, bool) or not isinstance(mx, int) or not 1 <= mx <= LOOP_MAX:
            p.append(f"{where}: loop max: 1..{LOOP_MAX}")
        if not lp.get("until"):
            p.append(f"{where}: loop needs until: (an expression over last, stages, prev, i)")
        else:
            bad = _check_expr(lp["until"])
            if bad:
                p.append(f"{where}: until: {bad}")
        for j, s in enumerate(st):
            p += _validate_phase(s, f"{where} stage {j + 1}", need_title=False, depth=1)
        return p
    if kind == "panel":
        n = ph.get("n", 3)
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 9:
            p.append(f"{where}: panel n: 1..9")
        entries = [{"agent": ph["panel"]}]
    elif kind == "agent":
        entries = [ph]
    elif kind == "parallel":
        entries = ph["parallel"]
    else:
        pl = ph["pipeline"] or {}
        entries = pl.get("stages")
        if "items_from" in pl:
            bad = _check_expr(pl["items_from"])
            if bad:
                p.append(f"{where}: items_from: {bad}")
    if not isinstance(entries, list) or not entries:
        return p + [f"{where}: {kind} needs entries"]
    for en in entries:
        try:
            load_agent(en.get("agent") if isinstance(en, dict) else None)
        except Exception as e:
            p.append(f"{where}: {e}")
    return p


def validate_workflow(w):
    if not isinstance(w, dict) or not w.get("name") or not isinstance(w.get("phases"), list) or not w["phases"]:
        return ["a workflow needs a name and a non-empty list of phases"]
    p = []
    for i, ph in enumerate(w["phases"]):
        p += _validate_phase(ph, f"phase {i + 1}")
    return p


def _majority(results, key):
    """-> (the value a strict majority of the WHOLE panel gave, or None; the vote counts). A judge that failed counts against."""
    votes = {}
    for r in results:
        if isinstance(r, dict) and r.get(key) is not None:
            votes[str(r[key])] = votes.get(str(r[key]), 0) + 1
    win = [v for v, c in votes.items() if c * 2 > len(results)]
    return (win[0] if win else None), votes


def _run_phase(crew, ph, ctx, title):
    """One phase (or loop stage). -> its result (prev for the next phase). OwnershipClash propagates."""
    import arch_dataflow
    kind = _phase_kind(ph)
    vars_, prev = ctx["vars"], ctx["prev"]

    def spec(en, label_extra=""):
        v = dict(vars_, **(en.get("vars") or {}))
        owns = en.get("owns")
        lab = render(en.get("label") or "", v) or None
        return {"agent": en["agent"], "task": render(en.get("task", ""), v), "label": (lab + label_extra) if lab else None,
                "owns": render(owns, v) if isinstance(owns, str) else owns, "context": prev, "vars": en.get("vars"),
                "budget": en.get("budget")}
    if kind == "code":
        return CODE_STEPS[ph["code"]](prev, ctx)
    if kind == "parallel":
        return [r for r in crew.parallel([spec(en) for en in ph["parallel"]]) if r is not None]
    if kind == "pipeline":
        pl = ph["pipeline"]
        items = pl.get("items") or []
        if pl.get("items_from"):
            got = arch_dataflow.evaluate(pl["items_from"], ctx)
            items = list(got) if isinstance(got, (list, tuple)) else []
        return [r for r in crew.pipeline(items, *[spec(s) for s in pl["stages"]]) if r is not None]
    if kind == "panel":
        n = int(ph.get("n", 3))
        base = dict(ph, agent=ph["panel"])
        base["label"] = ph.get("label") or ph["panel"]
        res = crew.parallel([spec(base, f"#{k + 1}") for k in range(n)])
        key = ph.get("vote", "verdict")
        win, votes = _majority(res, key)
        crew._event({"type": "panel", "title": title, "agent": ph["panel"], "n": n, "votes": votes, "majority": win})
        return {key: win, "votes": votes, "results": [r for r in res if r is not None], "agreed": votes.get(win, 0) if win else 0}
    if kind == "loop":
        lp = ph["loop"]
        mx = int(lp.get("max", 2))
        last, passed, i = prev, False, 0
        for i in range(1, mx + 1):
            stages = {}
            for j, st in enumerate(lp["stages"]):
                sctx = dict(ctx, prev=last, last=last, stages=stages, i=i)
                stitle = st.get("title") or st.get("label") or st.get("agent") or st.get("panel") or f"stage {j + 1}"
                if "when" in st and not arch_dataflow.evaluate(st["when"], sctx):
                    crew._event({"type": "skipped", "title": f"{title}/{stitle}", "when": st["when"], "iteration": i})
                    continue
                last = _run_phase(crew, st, sctx, f"{title}/{stitle}")
                stages[st.get("id") or re.sub(r"\W+", "_", str(stitle).lower()).strip("_")] = last
            try:
                passed = bool(arch_dataflow.evaluate(lp["until"], dict(ctx, prev=last, last=last, stages=stages, i=i)))
            except Exception:
                passed = False
            crew._event({"type": "loop", "title": title, "iteration": i, "max": mx, "passed": passed})
            if passed:
                break
        crew.loop_counts[title] = i
        return last
    s = spec(ph)
    return crew.agent(s["agent"], s["task"], label=s["label"], owns=s["owns"], context=s["context"], budget=s["budget"])


def run_workflow(w, root=None, goal="", vars=None, overrides=None, **kw):
    """Run a workflow (a dict, a path, or a workflows/ name). -> (verdict, crew).
    Each phase gets the previous phase's result as {prev}. A phase with `when:` runs only when that expression (arch_dataflow's safe
    evaluator over goal, vars, prev, phases.<id>) is true. overrides = {"agents": {"<name>"|"*": {field: value}}, "slots": N}
    (parse_sets builds it from --set); every agent record and the journal say which overrides applied."""
    import arch_dataflow
    if not isinstance(w, dict):
        w = load_workflow(w)
    root = root or os.getcwd()
    vars_ = dict(vars or {}, goal=goal)
    vars_.setdefault("owns", "")
    crew = Crew(root, goal, workflow=w["name"], description=w.get("description", ""), vars=vars_, overrides=overrides, **kw)
    prev, phases = None, {}
    for ph in w["phases"]:
        ctx = {"goal": goal, "vars": vars_, "prev": prev, "phases": phases}
        pid = ph.get("id") or re.sub(r"\W+", "_", ph["title"].lower()).strip("_")
        t0 = time.time()
        if "when" in ph:
            try:
                go = bool(arch_dataflow.evaluate(ph["when"], ctx))
            except Exception as ex:
                go = False
                crew.say(f"[-] {ph['title']}: when: could not be read ({ex}); skipped")
            if not go:
                crew._event({"type": "skipped", "title": ph["title"], "when": ph["when"]})
                crew.phase_times.append({"title": ph["title"], "kind": _phase_kind(ph), "secs": 0.0, "skipped": True})
                crew.say(f"[-] {ph['title']}: skipped (when: {ph['when']})")
                continue
        crew.phase(ph["title"])
        try:
            prev = _run_phase(crew, ph, ctx, ph["title"])
        except OwnershipClash as ex:
            crew.say(f"[x] {ph['title']}: refused before running: {ex}")
            return crew.close("refused"), crew
        phases[pid] = prev
        secs = round(time.time() - t0, 3)
        crew.phase_times.append({"title": ph["title"], "kind": _phase_kind(ph), "secs": secs, "skipped": False})
        crew._event({"type": "phase_done", "title": ph["title"], "kind": _phase_kind(ph), "secs": secs, "ok": prev not in (None, [])})
        crew.say(f"[{'ok' if prev not in (None, []) else '-'}] {ph['title']}: " + _cut(json.dumps(prev, ensure_ascii=False, default=str), 200))
    return crew.close(), crew


# ---------------------------------------------------------------- catalogue for a UI: list, describe, save

def _phase_summary(ph):
    kind = _phase_kind(ph)
    if kind == "parallel":
        agents = [en.get("agent") for en in ph["parallel"] if isinstance(en, dict)]
    elif kind == "pipeline":
        agents = [en.get("agent") for en in (ph["pipeline"] or {}).get("stages") or [] if isinstance(en, dict)]
    elif kind == "loop":
        agents = [a for st in (ph["loop"] or {}).get("stages") or [] if isinstance(st, dict) for a in _phase_summary(st)["agents"]]
    elif kind == "panel":
        agents = [ph["panel"]] * int(ph.get("n", 3))
    elif kind == "agent":
        agents = [ph["agent"]]
    else:
        agents = []
    row = {"title": ph.get("title") or ph.get("label") or ph.get("agent") or ph.get("panel"), "kind": kind, "agents": agents}
    if "when" in ph:
        row["when"] = ph["when"]
    if kind == "loop":
        row.update(max=ph["loop"].get("max", 2), until=ph["loop"].get("until"))
    if kind == "code":
        row["code"] = ph["code"]
    return row


def list_workflows(folder=None):
    """-> {name: {name, title, description, path, phases: [{title, kind, agents, when?, ...}], problems?}} for workflows/*.yaml."""
    import arch_ci
    folder = folder or WORKFLOWS_DIR
    out = {}
    for fn in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if not fn.endswith(".yaml"):
            continue
        path = os.path.join(folder, fn)
        try:
            with open(path, encoding="utf-8") as f:
                w = arch_ci.load_yaml(f.read()) or {}
        except Exception as e:
            out[fn[:-5]] = {"name": fn[:-5], "path": path, "problems": [str(e)], "phases": []}
            continue
        row = {"name": fn[:-5], "title": w.get("name"), "description": w.get("description", ""), "path": path,
               "phases": [_phase_summary(ph) for ph in w.get("phases") or [] if isinstance(ph, dict) and _phase_kind(ph)]}
        probs = validate_workflow(w)
        if probs:
            row["problems"] = probs
        out[fn[:-5]] = row
    return out


def describe(name):
    """An agent or workflow name -> a dict for a person or a UI: what it is, its fields (agents) or its phases (workflows)."""
    ag = list_agents()
    if name in ag:
        d = ag[name]
        if "problems" in d:
            return {"type": "agent", "name": name, "problems": d["problems"]}
        row = {k: d.get(k) for k in ("name", "description", "model", "role", "category", "code", "tools", "writes", "effort", "budget",
                                      "max_steps", "context", "owns", "output")}
        return dict(row, type="agent", fields={k: FIELD_INFO[k] for k in OVERRIDABLE})
    wf = list_workflows()
    key = name[:-5] if str(name).endswith(".yaml") else name
    hit = wf.get(key) or next((v for v in wf.values() if v.get("title") == name), None)
    if hit:
        return dict(hit, type="workflow")
    raise KeyError(f"no agent or workflow called {name!r}")


def _scalar_yaml(x):
    if isinstance(x, bool):
        return "true" if x else "false"
    if x is None:
        return "null"
    if isinstance(x, (int, float)):
        return str(x)
    if isinstance(x, dict):
        return "{}"
    if isinstance(x, list):
        return "[]"
    return json.dumps(str(x), ensure_ascii=False)


def _dump_yaml(v, ind=0):
    """A small YAML writer for agent definitions (mappings, lists, strings, numbers, booleans); block text for multi-line strings.
    save_agent reads the text back with arch_ci.load_yaml and refuses anything that does not come back equal."""
    pad = " " * ind
    lines = []
    if isinstance(v, dict):
        for k, x in v.items():
            if isinstance(x, (dict, list)) and x:
                lines.append(f"{pad}{k}:")
                lines += _dump_yaml(x, ind + 2)
            elif isinstance(x, str) and "\n" in x:
                lines.append(f"{pad}{k}: |")
                lines += [(pad + "  " + ln) if ln else "" for ln in x.rstrip("\n").split("\n")]
            else:
                lines.append(f"{pad}{k}: {_scalar_yaml(x)}")
        return lines
    for x in v:
        if isinstance(x, dict) and x:
            sub = _dump_yaml(x, ind + 2)
            lines.append(pad + "- " + sub[0].lstrip())
            lines += sub[1:]
        else:
            lines.append(pad + "- " + _scalar_yaml(x))
    return lines


def save_agent(name, spec, folder=None):
    """Validate a definition, then write agents/<name>.yaml atomically (temp file + os.replace). -> the path.
    ValueError with the problems when it is invalid, when its name is not `name`, or when the YAML would not read back the same;
    nothing is written then."""
    import arch_ci
    if not re.match(r"^[a-z][a-z0-9_-]{0,40}$", str(name or "")):
        raise ValueError(f"name: lower-case letters, digits, - or _ (got {str(name)[:40]!r})")
    d = _plain(dict(spec or {}))
    d.setdefault("name", name)
    if d["name"] != name:
        raise ValueError(f"the definition is called {d['name']!r}, not {name!r}")
    probs = validate(d)
    if probs:
        raise ValueError("; ".join(probs))
    order = ["name", "description", "model", "role", "category", "code", "system", "task", "tools", "writes", "output", "budget",
             "effort", "max_steps", "context", "owns"]
    d = {k: d[k] for k in order if k in d}
    for k in ("system", "task"):
        if not d[k].endswith("\n") and "\n" in d[k]:
            d[k] += "\n"
    text = f"# {name}: saved by arch_squad.save_agent (validated before writing)\n" + "\n".join(_dump_yaml(d)) + "\n"
    back = arch_ci.load_yaml(text)
    if back != d:
        diff = sorted(k for k in d if back.get(k) != d.get(k))
        raise ValueError("would not read back the same (fields: " + ", ".join(diff) + ")")
    folder = folder or AGENTS_DIR
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name + ".yaml")
    tmp = path + f".tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


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
    ap.add_argument("--set", action="append", default=[], metavar="AGENT.FIELD=VALUE",
                    help="override an agent field for this run (coder.model=qwen3:8b, coder.effort=low, *.budget.calls=4, slots=2)")
    ap.add_argument("--workflows", action="store_true", help="list and validate workflows/*.yaml, then stop")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        overrides = parse_sets(a.set)
    except ValueError as ex:
        print(f"[x] --set: {ex}")
        return 2
    if a.workflows:
        rows = list_workflows()
        for n, w in rows.items():
            print(f"{'[x]' if w.get('problems') else '[ok]'} {n}: " + ("; ".join(w['problems']) if w.get('problems') else
                  " -> ".join(f"{p['title']}({p['kind']})" for p in w['phases'])))
        return 0 if all(not w.get("problems") for w in rows.values()) else 1
    if a.agents or not a.workflow:
        rows = list_agents()
        for n, d in rows.items():
            print(f"{'[x]' if 'problems' in d else '[ok]'} {n}: " + ("; ".join(d['problems']) if 'problems' in d else
                  f"{d['model']}/{d['role']}, tools {', '.join(d['tools'])}, output {'schema' if isinstance(d['output'], dict) else 'text'}"))
        return 0 if all("problems" not in d for d in rows.values()) else 1
    w = load_workflow(a.workflow)
    root = os.path.abspath(a.root or os.getcwd())
    cfg = core.load_config(root)
    srv = None
    if a.fake:
        srv, over = start_fake()
        cfg = core.deep_merge(cfg, over)
    try:
        verdict, crew = run_workflow(w, root, a.goal, vars={"owns": a.owns}, overrides=overrides, cfg=cfg,
                                    slots=a.slots, say=print)
    finally:
        if srv is not None:
            srv.shutdown()
    out = {"verdict": verdict, "run_id": crew.run_id, "journal": os.path.join(crew.dir, "journal.jsonl"),
           "agents": [{k: r.get(k) for k in ("agentId", "label", "phase", "verdict", "why", "totals", "model", "effort", "overrides")}
                      for r in crew.records], "phases": crew.phase_times}
    print(json.dumps(out, indent=1, default=str) if a.json else
          f"{w['name']}: {verdict}; {len(crew.records)} agents, peak {crew.peak_concurrency} at once (cap {crew.slots}); journal {out['journal']}")
    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
