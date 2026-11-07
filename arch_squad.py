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
import hashlib
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
        "max_steps", "context", "owns", "code", "color"}
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


class CrewBudgetExhausted(RuntimeError):
    """The workflow-level budget is spent: no further agent starts; the run ends not_sure."""


# ---------------------------------------------------------------- budget, fingerprints, replay, cache, telemetry, plugins

BUDGET_KEYS = ("calls", "tokens", "seconds")


class Budget:
    """A whole-run ceiling {calls, tokens, seconds}: spent / remaining. Thread-safe."""

    def __init__(self, limits=None):
        self.limits = {k: v for k, v in (limits or {}).items() if k in BUDGET_KEYS and isinstance(v, (int, float))
                       and not isinstance(v, bool) and v > 0}
        self.spent = {"calls": 0, "tokens": 0}
        self.t0 = time.time()
        self._lk = threading.Lock()

    def charge(self, calls=0, tokens=0):
        with self._lk:
            self.spent["calls"] += int(calls or 0)
            self.spent["tokens"] += int(tokens or 0)

    def remaining(self):
        """None when there is no ceiling, else {key: what is left} (may be <= 0)."""
        if not self.limits:
            return None
        with self._lk:
            used = dict(self.spent, seconds=time.time() - self.t0)
        return {k: v - used[k] for k, v in self.limits.items()}

    @property
    def total(self):
        """The workflow tool's budget.total: the ceilings ({} when there are none)."""
        return dict(self.limits)

    def exhausted(self):
        left = self.remaining()
        return left is not None and any(v <= 0 for v in left.values())

    def status(self):
        return {"total": dict(self.limits), "limits": self.limits, "spent": dict(self.spent, seconds=round(time.time() - self.t0, 1)),
                "remaining": self.remaining()}


def parse_budget(text):
    """'calls=20,tokens=50000,seconds=600' -> dict. ValueError on a bad item."""
    out = {}
    for it in [x.strip() for x in str(text or "").split(",") if x.strip()]:
        k, _, v = it.partition("=")
        if k.strip() not in BUDGET_KEYS:
            raise ValueError(f"budget keys: {', '.join(BUDGET_KEYS)} (got {k!r})")
        try:
            out[k.strip()] = float(v) if "." in v else int(v)
        except ValueError:
            raise ValueError(f"budget {k}: a number (got {v!r})")
        if out[k.strip()] <= 0:
            raise ValueError(f"budget {k}: above 0")
    return out


def cache_allowed():
    return os.environ.get("ARCHITECT_NO_CACHE") != "1" and os.environ.get("ARCHITECT_EVAL") != "1"


def why_not(use_cache=None):
    """Plain words: why the agent-step cache is not used (None when it is)."""
    if os.environ.get("ARCHITECT_NO_CACHE") == "1":
        return "ARCHITECT_NO_CACHE=1 turns every cache off"
    if os.environ.get("ARCHITECT_EVAL") == "1":
        return "never during eval: a benchmark must measure the model"
    if use_cache is False:
        return "turned off for this run (--no-cache)"
    return None


SKIP_DIRS = {"__pycache__", "node_modules", "venv", ".venv"}
FP_MAX_FILES = 3000


def files_fingerprint(root, owns=None, exclude=None):
    """sha256 over file contents: the owned files/folders, or (owns None) the project's files outside dot-folders (optionally
    skipping `exclude`). A missing file counts as missing (so creating it changes the fingerprint)."""
    h = hashlib.sha256()
    root = os.path.abspath(root)
    ex_abs = [os.path.normcase(os.path.abspath(os.path.join(root, str(x)))) for x in (exclude or [])]

    def _excluded(p):
        if not ex_abs:
            return False
        np = os.path.normcase(os.path.abspath(p))
        return any(np == ex or np.startswith(ex + os.sep) for ex in ex_abs)

    paths = []
    if owns:
        for o in owns:
            p = os.path.join(root, str(o))
            if os.path.isdir(p):
                for dp, dns, fns in os.walk(p):
                    dns[:] = sorted(x for x in dns if not x.startswith(".") and x not in SKIP_DIRS)
                    paths += [os.path.join(dp, f) for f in sorted(fns) if not _excluded(os.path.join(dp, f))]
            elif not _excluded(p):
                paths.append(p)
    else:
        for dp, dns, fns in os.walk(root):
            dns[:] = sorted(x for x in dns if not x.startswith(".") and x not in SKIP_DIRS and not _excluded(os.path.join(dp, x)))
            paths += [os.path.join(dp, f) for f in sorted(fns)
                      if not f.endswith((".pyc", ".log")) and not _excluded(os.path.join(dp, f))]
            if len(paths) > FP_MAX_FILES:
                break
    for p in paths[:FP_MAX_FILES]:
        rel = os.path.relpath(p, root).replace("\\", "/")
        try:
            st = os.stat(p)
            if st.st_size > 2_000_000:
                h.update(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
                continue
            with open(p, "rb") as f:
                h.update(rel.encode() + b"\0" + hashlib.sha256(f.read()).digest() + b"\n")
        except OSError:
            h.update(rel.encode() + b"\0missing\n")
    return h.hexdigest()


def _cache_get(dd, key, days=30):
    p = os.path.join(dd, "orchestration", "cache", key + ".json")
    try:
        if time.time() - os.path.getmtime(p) > max(1, days) * 86400:
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


STEP_CACHE_MAX = 2000                  # entries kept; the oldest go first
_store_count = [0]


def prune_cache(dd, days=30, max_entries=STEP_CACHE_MAX, now=None):
    """Keep the agent-step cache bounded: entries older than `days`, temp files left by a crash (older than an hour), and anything
    beyond `max_entries` (oldest first) are deleted. -> number of files removed. Never raises."""
    d = os.path.join(dd, "orchestration", "cache")
    now = now or time.time()
    removed, live = 0, []
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    for fn in names:
        p = os.path.join(d, fn)
        try:
            age = now - os.path.getmtime(p)
            if ".tmp" in fn:
                stale = age > 3600
            elif fn.endswith(".json"):
                stale = age > max(1, days) * 86400
                if not stale:
                    live.append((os.path.getmtime(p), p))
            else:
                continue
            if stale:
                os.remove(p)
                removed += 1
        except OSError:
            continue
    for _, p in sorted(live)[:max(0, len(live) - max_entries)]:
        try:
            os.remove(p)
            removed += 1
        except OSError:
            pass
    return removed


def cache_stats(data_dir=None):
    """The 'agent steps' cache layer: {entries, bytes, path} (for arch_cachestats)."""
    import arch_core as core
    d = os.path.join(data_dir or core.data_dir(), "orchestration", "cache")
    n = b = 0
    for fn in os.listdir(d) if os.path.isdir(d) else []:
        if fn.endswith(".json"):
            n += 1
            try:
                b += os.path.getsize(os.path.join(d, fn))
            except OSError:
                pass
    return {"layer": "agent steps", "entries": n, "bytes": b, "path": d, "why_not": why_not()}


def _prior_records(rdir):
    """A finished (or crashed) run's agent records with verdict ok -> {step_key: record}."""
    out = {}
    for fn in sorted(os.listdir(rdir)) if os.path.isdir(rdir) else []:
        if fn.startswith("agent-") and fn.endswith(".json") and ".trace" not in fn:
            try:
                with open(os.path.join(rdir, fn), encoding="utf-8") as f:
                    r = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(r, dict) and r.get("step_key") and r.get("verdict") == "ok" and r.get("result") is not None:
                r["_run"] = os.path.basename(rdir)
                out.setdefault(r["step_key"], r)
    return out


def _find_run(dd, which, workflow=None, exclude=None):
    """'last' -> the newest run of this workflow (any workflow when none matches); an id -> that id when its folder exists."""
    base = os.path.join(dd, "orchestration", "local")
    if which != "last":
        return which if os.path.isdir(os.path.join(base, str(which))) else None
    rows = [r for r in list_runs(dd) if r["run_id"] != exclude]
    same = [r for r in rows if r.get("workflow") == workflow]
    return (same or rows)[0]["run_id"] if (same or rows) else None


def list_runs(data_dir=None, limit=50):
    """-> [{run_id, workflow, started, status, agents, cached, replayed, resume_from}], newest first."""
    import arch_core as core
    base = os.path.join(data_dir or core.data_dir(), "orchestration", "local")
    rows = []
    for rid in os.listdir(base) if os.path.isdir(base) else []:
        jp = os.path.join(base, rid, "journal.jsonl")
        if not os.path.isfile(jp):
            continue
        row = {"run_id": rid, "workflow": None, "started": None, "status": "running", "agents": 0, "cached": 0, "replayed": 0,
               "resume_from": None}
        try:
            with open(jp, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        j = json.loads(line)
                    except ValueError:
                        continue
                    t = j.get("type")
                    if t == "launched":
                        row.update(workflow=j.get("workflow"), started=j.get("t"), resume_from=j.get("resume_from"))
                    elif t == "result":
                        row["agents"] += 1
                    elif t == "cached":
                        row["cached"] += 1
                    elif t == "replayed":
                        row["replayed"] += 1
                    elif t == "done":
                        row["status"] = j.get("verdict")
        except OSError:
            continue
        rows.append(row)
    rows.sort(key=lambda r: (r["started"] or 0, r["run_id"]), reverse=True)
    return rows[:limit]


def _tele(kind, **fields):
    """arch_telemetry.emit when that module exists; never fails a run."""
    try:
        import arch_telemetry
        arch_telemetry.emit(kind, **fields)
    except Exception:
        pass


def _plugin_dirs(kind):
    """[(plugin name, folder)] from enabled plugins (arch_plugins.agent_dirs()/workflow_dirs()), [] when the module is missing."""
    try:
        import arch_plugins
        got = getattr(arch_plugins, kind + "_dirs")()
    except Exception:
        return []
    out = []
    for x in got or []:
        if isinstance(x, (tuple, list)) and len(x) == 2:
            out.append((str(x[0]), str(x[1])))
        elif isinstance(x, str):
            out.append((os.path.basename(os.path.dirname(os.path.abspath(x))), x))
    return [(n, d) for n, d in out if os.path.isdir(d)]


def _project_dir(kind, root=None):
    d = os.path.join(os.path.abspath(root or os.getcwd()), ".arch", kind)
    return d if os.path.isdir(d) else None


def _find_named(name, kind, ext=".yaml", root=None):
    """A bare or plugin:name -> its file: shipped wins a bare-name clash, then the project's .arch/<kind>/, then plugins.
    Agents are found as <name>.md (Claude Code format) first, then <name>.yaml."""
    shipped = AGENTS_DIR if kind == "agents" else WORKFLOWS_DIR
    name = str(name)
    exts = (".md", ".yaml") if kind == "agents" else (ext,)

    def names(bare):
        return [bare] if bare.endswith(exts) else [bare + x for x in exts]
    if ":" in name:
        plug, _, bare = name.partition(":")
        for n, d in _plugin_dirs(kind[:-1]):
            if n == plug:
                for fn in names(bare):
                    if os.path.isfile(os.path.join(d, fn)):
                        return os.path.join(d, fn)
        return None
    for d in [shipped, _project_dir(kind, root)] + [d for _, d in _plugin_dirs(kind[:-1])]:
        for fn in names(name):
            if d and os.path.isfile(os.path.join(d, fn)):
                return os.path.join(d, fn)
    return None


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
    return {k: v for k, v in d.items() if not str(k).startswith("_") and k not in ("fields", "problems", "scope", "plugin")}


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


# -- Claude-Code-style agent files: agents/<name>.md = frontmatter + the markdown body as the system prompt
TOOL_ALIASES = {"read": ["read", "view", "outline"], "grep": ["grep", "search"], "glob": ["ls"], "ls": ["ls"],
                "edit": ["edit_lines", "apply"], "write": ["edit_lines", "apply"], "multiedit": ["edit_lines", "apply"],
                "bash": ["run_tests"], "test": ["run_tests"], "tests": ["run_tests"]}
CLAUDE_MODEL_NAMES = ("sonnet", "opus", "haiku", "fable", "default")
MD_DEFAULT_TASK = "{task}\n\nGoal of the whole job: {goal}\n{prev}"
MD_DEFAULT_BUDGET = {"calls": 12, "tokens": 60000, "seconds": 900}


def _fm_scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        try:
            return json.loads(v) if v[0] == '"' else v[1:-1]
        except ValueError:
            return v[1:-1]
    return v


def parse_frontmatter(text):
    """'---\\nkey: value\\n...\\n---\\nbody' -> (dict of strings/lists, body). Supports `key: |` blocks and `- item` lists.
    ValueError when there is no frontmatter block or a line cannot be read."""
    t = str(text or "").lstrip("﻿").replace("\r\n", "\n")
    if not t.startswith("---\n"):
        raise ValueError("an agent .md file starts with a --- frontmatter block")
    end = t.find("\n---", 3)
    if end < 0:
        raise ValueError("the frontmatter block is not closed with ---")
    head, body = t[4:end].split("\n"), t[end + 4:].lstrip("\n")
    out, i = {}, 0
    while i < len(head):
        ln = head[i]
        i += 1
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", ln)
        if not m:
            raise ValueError(f"frontmatter line {i}: expected key: value (got {ln[:40]!r})")
        k, v = m.group(1).replace("-", "_"), m.group(2)
        if v.strip() in ("|", ">"):
            blk = []
            while i < len(head) and (not head[i].strip() or head[i][:1] in " \t"):
                blk.append(head[i])
                i += 1
            ind = min([len(x) - len(x.lstrip()) for x in blk if x.strip()] or [0])
            out[k] = "\n".join(x[ind:] for x in blk).rstrip("\n") + "\n"
        elif v.strip() == "":
            items = []
            while i < len(head) and re.match(r"^\s+-\s+", head[i]):
                items.append(_fm_scalar(re.sub(r"^\s+-\s+", "", head[i])))
                i += 1
            out[k] = items
        else:
            out[k] = _fm_scalar(v)
    return out, body


def _csv(v):
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v or "").split(",") if x.strip()]


def map_tools(names):
    """Claude-style tool names (Read, Grep, Glob, Edit, Write, Bash) and local names -> arch_tools names; order kept, no repeats.
    A name that maps to nothing stays as it is, so validate() reports it."""
    known, out = _tools(), []
    for n in names:
        low = str(n).strip().lower()
        for t in ([low] if low in known and not str(n).strip()[:1].isupper() else TOOL_ALIASES.get(low, [str(n).strip()])):
            if t not in out:
                out.append(t)
    return out


def parse_agent_md(text):
    """An agent .md file -> a definition dict (not yet validated). Frontmatter: name, description (when to use), tools (comma list),
    model (a local tier or model id), effort, color; optional role, category, task, writes, output (JSON on one line), budget
    (calls=,tokens=,seconds=), max_steps, context (policy[:max_chars]), owns, code. The body is the system prompt."""
    fm, body = parse_frontmatter(text)
    d = {"name": fm.get("name", ""), "description": str(fm.get("description", "")).strip(), "system": body.strip() + "\n"}
    m = str(fm.get("model", "worker")).strip() or "worker"
    d["model"] = "worker" if m == "inherit" else ("claude:" + m if m.lower() in CLAUDE_MODEL_NAMES else m)
    d["tools"] = map_tools(_csv(fm["tools"])) if "tools" in fm else ["ls", "grep", "search", "read", "outline", "view"]
    if fm.get("writes") is None:
        writes = any(t in _tools() and _tools()[t]["writes"] for t in d["tools"])
    else:
        writes = str(fm["writes"]).lower() in ("true", "yes", "1")
    d["writes"] = writes
    d["role"] = fm.get("role") or ("generator" if writes else "reviewer")
    for k in ("category", "effort", "color", "code"):
        if k in fm:
            d[k] = str(fm[k]).strip()
    d["task"] = fm.get("task") or MD_DEFAULT_TASK
    if "output" in fm:
        o = str(fm["output"]).strip()
        try:
            d["output"] = o if o == "text" else json.loads(o)
        except ValueError:
            raise ValueError("output: 'text' or a JSON schema on one line")
    d["budget"] = parse_budget(fm["budget"]) if "budget" in fm else dict(MD_DEFAULT_BUDGET)
    if "max_steps" in fm:
        try:
            d["max_steps"] = int(fm["max_steps"])
        except ValueError:
            raise ValueError("max_steps: a whole number")
    if "context" in fm:
        pol, _, mc = str(fm["context"]).partition(":")
        d["context"] = {"policy": pol.strip()}
        if mc.strip():
            d["context"]["max_chars"] = int(mc)
    if "owns" in fm:
        d["owns"] = _csv(fm["owns"])
    return d


def dump_md(d):
    """A definition -> agent .md text (the inverse of parse_agent_md for every field it reads)."""
    d = _plain(d)
    fm = ["---", f"name: {d['name']}", "description: " + json.dumps(str(d.get("description") or ""), ensure_ascii=False),
          f"model: {d['model']}", "tools: " + ", ".join(d["tools"])]
    for k in ("effort", "color", "role", "category", "code"):
        if d.get(k) is not None:
            fm.append(f"{k}: {d[k]}")
    fm.append(f"writes: {'true' if d.get('writes') else 'false'}")
    if d.get("max_steps") is not None:
        fm.append(f"max_steps: {d['max_steps']}")
    if d.get("budget"):
        fm.append("budget: " + ",".join(f"{k}={v}" for k, v in d["budget"].items()))
    c = d.get("context")
    if c:
        fm.append(f"context: {c.get('policy', 'fresh')}:{c.get('max_chars', 8000)}")
    if d.get("owns"):
        fm.append("owns: " + ", ".join(d["owns"]))
    if d.get("output") not in (None, "text"):
        fm.append("output: " + json.dumps(d["output"], separators=(",", ":"), sort_keys=True, ensure_ascii=False))
    fm.append("task: |")
    fm += ["  " + ln if ln else "" for ln in (d.get("task") or MD_DEFAULT_TASK).rstrip("\n").split("\n")]
    return "\n".join(fm) + "\n---\n" + (d.get("system") or "").strip() + "\n"


def load_agent(name_or_path, folder=None):
    """agents/<name>.md (Claude Code format) or agents/<name>.yaml (or a path, or an already-loaded dict) -> a validated definition.
    ValueError lists the problems."""
    import arch_ci
    if isinstance(name_or_path, dict):
        d, src = _plain(name_or_path), name_or_path.get("_source") or "(inline)"
    else:
        src = str(name_or_path)
        if not os.path.isfile(src):
            base = os.path.join(folder or AGENTS_DIR, src)
            src = next((base + x for x in (".md", ".yaml") if os.path.isfile(base + x)), base + ".md")
            if not folder and not os.path.isfile(src):
                src = _find_named(name_or_path, "agents") or src
        with open(src, encoding="utf-8") as f:
            raw = f.read()
        try:
            d = parse_agent_md(raw) if src.endswith(".md") else arch_ci.load_yaml(raw)
        except ValueError as ex:
            raise ValueError(f"{src}: {ex}")
    probs = validate(d)
    if probs:
        raise ValueError(f"{src}: " + "; ".join(probs))
    d = _norm(d)
    d["_source"] = src
    return d


def list_agents(folder=None, root=None):
    """-> {name: definition or {"problems": [...]}} for every agents/*.md and agents/*.yaml (a .yaml with a .md of the same name in
    the same folder is skipped: one registration per agent). Without a folder it also lists the project's .arch/agents/ (scope
    project) and enabled plugins' agents (scope plugin, as plugin:name when the bare name is taken); shipped agents win a bare-name
    clash."""
    out = {}
    sources = [(folder, "folder", None)] if folder else \
        [(AGENTS_DIR, "shipped", None), (_project_dir("agents", root), "project", None)] + \
        [(d, "plugin", n) for n, d in _plugin_dirs("agent")]
    for fold, scope, plug in sources:
        files = sorted(os.listdir(fold)) if fold and os.path.isdir(fold) else []
        for fn in files:
            stem, ext = os.path.splitext(fn)
            if ext not in (".md", ".yaml") or (ext == ".yaml" and stem + ".md" in files):
                continue
            try:
                d = load_agent(os.path.join(fold, fn))
                d["fields"] = list(OVERRIDABLE)
                key = d["name"]
            except Exception as e:
                d, key = {"problems": [str(e)]}, stem
            if scope != "folder":
                d["scope"] = scope
            if plug:
                d["plugin"] = plug
                if key in out:
                    key = f"{plug}:{key}"
            elif key in out:
                continue
            out[key] = d
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
    return None


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
    if own and not own.startswith("claude:"):
        return own
    m = e.tier_model(d["model"])
    return "" if m.startswith("claude:") else m


# ---------------------------------------------------------------- summaries, not transcripts (docs/PRINCIPLES.md 1 and 5)

DIGEST_CHARS = 800                       # what the next phase gets of one result; the full text stays in the journal
_DROP_FIRST = ("evidence", "details", "detail", "reason", "notes", "log", "text", "body", "diff", "output")


def _flat(v, n):
    return _cut(re.sub(r"\s+", " ", v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)), n)


def digest(result, limit=DIGEST_CHARS):
    """Condense one agent's result for the next phase. A result already within the limit passes unchanged. A schema object keeps its
    short scalar fields (verdict ...), a summary cut to fit, the file list, and findings as one line each; a list is digested item by item;
    text is cut. The result always fits `limit` characters as JSON. The full result is never lost: the record and the journal hold it."""
    def size(x):
        return len(x) if isinstance(x, str) else len(json.dumps(x, ensure_ascii=False, default=str))
    if result is None or size(result) <= limit:
        return result
    if isinstance(result, str):
        return _cut(result, limit - 4)
    if isinstance(result, list):
        per = max(60, (limit - 40) // max(1, len(result)))
        out = {"n": len(result), "items": [digest(x, per) for x in result[:12]]}
        if len(result) > 12:
            out["more"] = len(result) - 12
        return out if size(out) <= limit else {"n": len(result), "items": [_flat(x, 40) for x in result[:6]]}
    if not isinstance(result, dict):
        return _cut(str(result), limit - 4)
    out, longs = {}, []
    for k, v in result.items():
        if isinstance(v, (bool, int, float)) or v is None or (isinstance(v, str) and len(v) <= 60):
            out[k] = v
        else:
            longs.append((k, v))
    for k, v in longs:
        if k in ("changed", "files", "edited", "files_changed") and isinstance(v, list):
            files = [str(x) for x in v]
            out[k] = files[:15] + ([f"... {len(files) - 15} more"] if len(files) > 15 else [])
        elif k == "findings" and isinstance(v, list):
            out["findings_n"] = len(v)
            out["findings"] = [_flat(f, 90) for f in v[:5]]
        elif isinstance(v, list):
            out[k + "_n"] = len(v)
    room = limit - size(out) - 60
    for k, v in sorted((x for x in longs if x[0] not in out and x[0] != "findings"),
                       key=lambda x: (x[0] in _DROP_FIRST, x[0] != "summary")):
        if isinstance(v, str) and room > 80:
            out[k] = _cut(v, min(len(v), max(80, room // 2)))
            room -= len(out[k])
    while size(out) > limit and out.get("findings"):
        out["findings"] = out["findings"][:-1]
    while size(out) > limit and isinstance(out.get("changed"), list) and len(out["changed"]) > 3:
        out["changed"] = out["changed"][:len(out["changed"]) // 2]
    if size(out) > limit:
        return _cut(json.dumps(out, ensure_ascii=False, default=str), limit - 4)
    return out


def context_cap(cfg, d):
    """Characters of the previous phase's digest an agent may be shown: its own context.max_chars, never above half of its role
    profile's prompt window (ctx_max tokens x 4 chars / 2). No profile window = no extra cap. squad.role_context = off skips it."""
    cap = int(d["context"]["max_chars"])
    if str((cfg.get("squad") or {}).get("role_context", "on")) == "off":
        return cap
    try:
        import arch_roleprofile as rp
        mine = ((cfg.get("role_profiles") or {}).get(d["role"]) or {}).get("ctx_max")
        win = mine if mine else (rp.shipped().get(d["role"]) or {}).get("ctx_max")
    except Exception:
        win = None
    return min(cap, int(win) * 2) if win else cap



class Crew:
    """One local orchestration run. Thread-safe: parallel() and pipeline() call agent() from worker threads."""

    def __init__(self, root, goal="", workflow="adhoc", cfg=None, slots=None, data_dir=None, say=None, engine_factory=None,
                 run_id=None, description="", vars=None, overrides=None, resume_from=None, use_cache=None, replay=None,
                 budget=None):
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
        self._host_sems = {}
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
        base_id, n = self.run_id, 1
        while not run_id and os.path.exists(os.path.join(dd, "orchestration", "local", self.run_id)):
            n += 1                                   # two runs in the same second must never share a journal
            self.run_id = f"{base_id}-{n}"
        self.dir = os.path.join(dd, "orchestration", "local", self.run_id)
        os.makedirs(self.dir, exist_ok=True)
        self.phase_title = None
        self.data_dir = dd
        sq = self.cfg.get("squad") or {}
        self.replay = sq.get("replay", True) if replay is None else bool(replay)
        self.use_cache = cache_allowed() if use_cache is None else bool(use_cache)
        self.cache_days = int(sq.get("step_cache_days", 30) or 30)
        self.budget = Budget(budget)
        self.budget_hit = False
        self.resume_from = None
        self._prior = {}
        if resume_from and self.replay:
            self.resume_from = _find_run(dd, resume_from, workflow, exclude=self.run_id)
            self._prior = _prior_records(os.path.join(dd, "orchestration", "local", self.resume_from)) if self.resume_from else {}
        self.replayed = self.cached = 0
        self._event({"type": "launched", "workflow": workflow, "description": description, "goal": goal, "root": self.root,
                     "slots": self.slots, "overrides": {"agents": self.overrides, "slots": (overrides or {}).get("slots")},
                     "resume_from": self.resume_from, "cache": self.use_cache, "budget": self.budget.limits})
        _tele("crew.launched", run=self.run_id, workflow=workflow, resume_from=self.resume_from)

    def log(self, text):
        """A narrator line in the journal (the workflow tool's log())."""
        self._event({"type": "log", "phase": self.phase_title, "text": str(text)[:2000]})
        self.say("[log] " + str(text)[:300])

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
        e.crew = self
        e.agent_def = d
        if not hasattr(e, "delegate_depth"):
            e.delegate_depth = 0
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
        raw_ctx = context
        if context is not None and d["context"]["policy"] == "inherit" and str((self.cfg.get("squad") or {}).get("digest", "on")) != "off":
            context = digest(context, int((self.cfg.get("squad") or {}).get("digest_chars") or DIGEST_CHARS))
        ph = phase or getattr(self._local, "phase", None) or self.phase_title
        parent = parent or getattr(self._local, "parent", None)
        rec = {"agentId": aid, "label": label, "agent": d["name"], "phase": ph, "parent": parent, "model": d["model"],
               "role": d["role"], "category": d["category"], "owns": owns, "effort": d["effort"], "budget": b,
               "context_policy": d["context"]["policy"], "output": "schema" if isinstance(d["output"], dict) else "text",
               "tools_allowed": list(d["tools"]), "tools": {}, "edited": [], "calls": [], "verdict": None, "result": None,
               "why": None, "start": time.time(), "overrides": applied}
        if context is not None and d["context"]["policy"] == "inherit":
            rec["context_chars"] = {"raw": len(json.dumps(raw_ctx, ensure_ascii=False, default=str)),
                                    "shown": len(json.dumps(context, ensure_ascii=False, default=str))}
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
        # -- workflow budget: an exhausted run stops further agents (the run ends not_sure, never pass)
        if self._budget_refuse(rec, aid, label):
            raise CrewBudgetExhausted(rec["why"])
        b = self._cap_budget(b, rec)
        # -- replay (resume) and the step cache
        writer = bool(d["writes"] and owns)
        try:
            key = self.step_key(d, task, owns, context, vars, label)
            ckey = self.step_key(d, task, owns, context, vars, label, files=True)
        except Exception:
            key = ckey = None
        rec["step_key"], rec["cache_key"] = key, ckey
        hit = self._lookup(key, ckey, owns, writer) if key else None
        if hit is not None:
            src, old = hit
            rec.update(verdict="ok", result=old.get("result"), why=None, edited=old.get("edited") or [],
                       model=old.get("model") or rec["model"], brief=old.get("brief"), post_hash=old.get("post_hash"))
            rec["replayed" if src != "cache" else "cached"] = old.get("_run") if src != "cache" else True
            with self._lock:
                if src == "cache":
                    self.cached += 1
                else:
                    self.replayed += 1
            self._event({"type": "replayed" if src != "cache" else "cached", "agentId": aid, "label": label, "phase": ph,
                         "agent": d["name"], "key": key, "from_run": old.get("_run")})
            _tele("crew.agent", run=self.run_id, agent=d["name"], label=label, outcome=src, calls=0, tokens=0)
            self._finish(rec)
            return rec["result"]
        try:
            self._claim(aid, label, owns)
        except OwnershipClash as ex:
            rec.update(verdict="refused", why=str(ex))
            self._finish(rec)
            raise
        self._event({"type": "started", "agentId": aid, "label": label, "phase": ph, "agent": d["name"], "parent": parent,
                     "overrides": applied})
        e = None
        _sem = self._sem
        try:
            import arch_hosts
            _rt = arch_hosts.route_of(self.cfg, d["role"]) if not self.engine_factory else None
            if _rt and _rt[0]:
                with self._lock:
                    _sem = self._host_sems.setdefault(_rt[0], threading.Semaphore(1))
        except Exception:
            _sem = self._sem
        try:
            with _sem:
                with self._lock:
                    self.running += 1
                    self.peak_concurrency = max(self.peak_concurrency, self.running)
                try:
                    if self.budget.exhausted():          # a slot freed up after others spent the budget
                        self._budget_refuse(rec, aid, label)
                        raise CrewBudgetExhausted(rec["why"])
                    b = self._cap_budget(b, rec)
                    e = self._engine(d, owns)
                    rec["result"], rec["verdict"], rec["why"] = self._loop(e, d, task, owns, context, vars, b, rec)
                finally:
                    with self._lock:
                        self.running -= 1
        except CrewBudgetExhausted:
            with self._lock:
                self._owners.pop(aid, None)
            raise
        except Exception as ex:                        # a failing agent is None, never a crash of the run
            rec.update(verdict="failed", why=f"{type(ex).__name__}: {str(ex)[:300]}", result=None)
        finally:
            with self._lock:
                self._owners.pop(aid, None)
            if e is not None:
                self._harvest(e, rec)
        if writer:
            rec["post_hash"] = files_fingerprint(self.root, owns)
            rec["deps_hash"] = files_fingerprint(self.root, exclude=owns)
        self._finish(rec)
        t = rec["totals"]
        self.budget.charge(t["calls"], t["tokens_in"] + t["tokens_out"])
        if rec["verdict"] == "ok" and ckey and self.use_cache:
            self._store(ckey, rec)
        _tele("crew.agent", run=self.run_id, agent=d["name"], label=label, outcome=rec["verdict"], calls=t["calls"],
              tokens=t["tokens_in"] + t["tokens_out"], secs=rec.get("secs"))
        return rec["result"] if rec["verdict"] == "ok" else None

    def _budget_refuse(self, rec, aid, label):
        if not self.budget.exhausted():
            return False
        self.budget_hit = True
        rec.update(verdict="budget", why="the workflow budget is spent: " + json.dumps(self.budget.status()))
        self._event({"type": "budget_exhausted", "agentId": aid, "label": label, "status": self.budget.status()})
        self._finish(rec)
        return True

    def _cap_budget(self, b, rec):
        left = self.budget.remaining()
        if left:
            b = dict(b)
            for k, v in left.items():
                b[k] = min(b[k], v) if k in b else v
                b[k] = int(b[k]) if k != "seconds" else max(1, b[k])
            b = {k: v for k, v in b.items() if v > 0}
            rec["budget"] = b
        return b

    # -- replay / cache
    def _model_id(self, d):
        if d["model"] not in TIERS:
            return d["model"]
        own = str((self.cfg.get("roles") or {}).get(d["role"]) or "")
        return own + "|" + str((self.cfg.get("tiers") or {}).get(d["model"]) or d["model"])

    def step_key(self, d, task, owns, context, vars_, label, files=False):
        """sha256 over the definition (after overrides), the rendered brief (which holds the previous phase's result, so a changed
        upstream result changes every key after it) and the resolved model. files=True (the cross-run step cache) adds the files the
        agent depends on: its owned files, else the project's files. A writer never hashes its files into the key: it replays only
        when its owned files are already in the state it left them (post_hash)."""
        system, body = self._brief(d, task, owns, context, vars_)
        parts = {"def": _plain(d), "system": system, "body": body, "model": self._model_id(d), "label": label}
        if files:                                      # the cross-run key never crosses projects
            parts["root"] = os.path.normcase(os.path.abspath(self.root))
            if not (d["writes"] and owns):
                parts["files"] = files_fingerprint(self.root, owns or None)
            else:
                parts["deps"] = files_fingerprint(self.root, exclude=owns)
        parts["def"].pop("_source", None)
        return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def _usable(self, old, owns, writer):
        if not isinstance(old, dict) or old.get("verdict") != "ok" or old.get("result") is None:
            return False
        if not writer:
            return True
        if not (old.get("post_hash") and old["post_hash"] == files_fingerprint(self.root, owns)):
            return False
        if old.get("deps_hash") and old["deps_hash"] != files_fingerprint(self.root, exclude=owns):
            return False
        return True

    def _lookup(self, key, ckey, owns, writer):
        if self.replay and key in self._prior and ckey and self._prior[key].get("cache_key") == ckey                 and self._usable(self._prior[key], owns, writer):   # same project, same files it read: never a stale replay
            return "resume", self._prior[key]
        if self.use_cache and ckey:
            old = _cache_get(self.data_dir, ckey, self.cache_days)
            if self._usable(old, owns, writer):
                return "cache", old
        return None

    def _store(self, key, rec):
        try:
            d = os.path.join(self.data_dir, "orchestration", "cache")
            os.makedirs(d, exist_ok=True)
            row = {k: rec.get(k) for k in ("verdict", "result", "edited", "model", "brief", "post_hash", "deps_hash", "agent", "label")}
            row.update(_run=self.run_id, saved=time.time())
            tmp = os.path.join(d, key + f".tmp{os.getpid()}_{threading.get_ident()}")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(row, f, ensure_ascii=False, default=str)
            os.replace(tmp, os.path.join(d, key + ".json"))
            _store_count[0] += 1
            if _store_count[0] % 25 == 1:                 # now and then: the cache must not grow without end
                prune_cache(self.data_dir)
        except OSError:
            pass

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

    def shared_header(self):
        """One byte-identical block that leads EVERY agent's system prompt in a run (squad.shared_prefix = on, default): the team's
        rules, the project's name (+ vars.project_summary when a workflow gives one; never the live file list, so a resumed run keeps the same prefix) and the contract (vars.contract, else the goal). Agent-specific text comes after it, so
        parallel agents of one phase share one prompt-cache prefix (the local server's prefix cache, and Claude's prompt cache where
        a launcher sends it). Built once per run, so it cannot drift while agents write files. '' when switched off."""
        if str((self.cfg.get("squad") or {}).get("shared_prefix", "on")) == "off":
            return ""
        with self._lock:
            h = getattr(self, "_shared_hdr", None)
            if h is None:
                contract = str(self.vars.get("contract") or self.goal or "").strip()[:2000]
                tfiles = self.vars.get("target_files")
                outline_blk = ""
                if isinstance(tfiles, (list, tuple)) and tfiles:
                    try:
                        import arch_core as core
                        lines = []
                        for tf in tfiles[:6]:
                            p = os.path.join(self.root, str(tf))
                            if os.path.isfile(p):
                                lines.append(f"--- {tf} ---\n" + (core.outline(p) or "")[:600])
                        if lines:
                            outline_blk = "\nTarget Outlines:\n" + "\n".join(lines)[:2400]
                    except Exception:
                        outline_blk = ""
                h = ("[crew team context]\nRules: stay inside the files you own; never claim a check passed that you did not see; "
                     "answer in the form the task asks for; say plainly when you are not sure.\n"
                     f"Project: {os.path.basename(os.path.normpath(self.root))} {(' - ' + str(self.vars.get('project_summary'))[:600]) if self.vars.get('project_summary') else ''}\n"
                     f"Contract:\n{contract}{outline_blk}\n[end team context]")
                self._shared_hdr = h
            return h

    def _brief(self, d, task, owns, context, vars_):
        v = dict(self.vars)
        v.update(goal=self.goal, task=task or "", prev="", focus="")
        v.update(vars_ or {})
        if d["context"]["policy"] == "inherit" and context is not None:
            v["prev"] = _cut(context if isinstance(context, str) else json.dumps(context, ensure_ascii=False, default=str),
                             context_cap(self.cfg, d))
        body = render(d["task"], v).strip()
        body = f"[crew-agent:{d['name']}]\n" + body
        if owns:
            body += "\n\nYOU OWN THESE FILES (any other write is refused): " + ", ".join(owns)
        if isinstance(d["output"], dict):
            body += (f"\n\nWhen you are done, your final answer must be ONE JSON value matching this schema, as the text of \"final\".\n"
                     f"{SCHEMA_BEGIN}\n{json.dumps(d['output'], sort_keys=True)}\n{SCHEMA_END}")
        system = render(d["system"], v).strip()
        hdr = self.shared_header()
        return (hdr + "\n\n" + system if hdr else system), body

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
        if not (self.cfg.get("role_hosts") or {}).get(role):
            e.pins[role] = m
        else:
            e.cfg.setdefault("roles", {})[role] = m
        rec["model"] = m
        schema = arch_tools.schema(allowed)
        tool_text = arch_tools.describe(allowed)
        think = EFFORTS[d["effort"]]
        turns, answer = [], None
        e.push_budget("agent " + rec["label"], **budget)
        try:
            for i in range(d["max_steps"]):
                last = i == d["max_steps"] - 1
                if turns and (self.cfg.get("compact") or {}).get("enabled", True):   # long tool loops: older steps become a digest
                    try:
                        import arch_compact
                        _cc = self.cfg.get("compact") or {}
                        turns, _ci = arch_compact.compact_turns(turns, arch_compact.window_tokens(self.cfg, d.get("role"), e),
                                                                pct=int(_cc.get("pct", 80)), keep_tail=int(_cc.get("keep_tail", 4)))
                        if _ci.get("compacted"):
                            arch_compact.record(e, _ci)
                    except Exception:
                        pass                         # compaction is an optimisation: never break the loop
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
        if verdict is None and self.budget_hit:
            verdict = "not_sure"
        verdict = verdict or ("pass" if self.records and not bad else ("fail" if self.records else "empty"))
        self._event({"type": "done", "verdict": verdict, "agents": len(self.records), "failed": bad,
                     "peak_concurrency": self.peak_concurrency, "replayed": self.replayed, "cached": self.cached,
                     "budget": self.budget.status()})
        _tele("crew.done", run=self.run_id, workflow=self.workflow, verdict=verdict, agents=len(self.records),
              replayed=self.replayed, cached=self.cached)
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


def log(text):
    """A narrator line in the journal (the workflow tool's log())."""
    return _cur().log(text)


def budget():
    """The current run's Budget: .total, .spent, .remaining(), .exhausted()."""
    return _cur().budget


# ---------------------------------------------------------------- workflows as data

PHASE_KINDS = ("agent", "parallel", "pipeline", "loop", "panel", "code", "log")
INPUT_TYPES = ("string", "integer", "number", "boolean", "list")
LOOP_MAX = 10


def _code_dedupe_findings(prev, ctx, args=None):
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


def _code_collect(prev, ctx, args=None):
    return {"results": prev if isinstance(prev, list) else [prev]}


# -- deterministic steps the lead does in code (docs/PRINCIPLES.md 2): arithmetic, test running, diff counting, JSON checks

_SUITE_OK = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-/]*\.py$")
_SKIP_DIRS = {".git", ".arch", "__pycache__", "node_modules", ".venv", "venv", "hub_data"}


def _inside(root, rel):
    """rel -> the absolute path when it stays inside root, else None."""
    p = os.path.realpath(os.path.join(root, str(rel)))
    r = os.path.realpath(root)
    return p if p == r or p.startswith(r + os.sep) else None


def run_suites(root, suites, jobs=4, timeout=600, env_extra=None):
    """Run test scripts in parallel, each in its own throwaway ARCHITECT_HOME / ARCHITECT_DATA_DIR (like tools/ci_parallel.py), so none can
    touch real settings. Only .py files inside root, never a shell. -> {"results": [...], "passed", "failed", "verdict", "summary"}.
    A suite passes when it exits 0; a timeout is a failure; a missing file is `rc: None` and counts as failed (could not run)."""
    import concurrent.futures as cf
    import subprocess
    import tempfile

    def one(name):
        path = _inside(root, name) if _SUITE_OK.match(str(name)) and ".." not in str(name) else None
        if not path or not os.path.isfile(path):
            return {"suite": name, "rc": None, "secs": 0, "summary": "not found inside the project"}
        home = tempfile.mkdtemp(prefix="crew_suite_")
        env = dict(os.environ, ARCHITECT_DATA_DIR=os.path.join(home, "data"), ARCHITECT_HOME=home, ARCHITECT_NO_CACHE="1",
                   PYTHONDONTWRITEBYTECODE="1", ARCHITECT_SELFTEST="1", **(env_extra or {}))
        t = time.time()
        try:
            p = subprocess.run([sys.executable, path], cwd=root, env=env, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout, creationflags=0x00004000 if os.name == "nt" else 0)
            out, rc = (p.stdout or "") + (p.stderr or ""), p.returncode
        except subprocess.TimeoutExpired:
            out, rc = "timed out", -1
        finally:
            shutil.rmtree(home, ignore_errors=True)
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        summ = next((l for l in reversed(lines) if re.search(r"passed|failed|PASSED|FAILED|OK", l)), lines[-1] if lines else "")
        return {"suite": name, "rc": rc, "secs": round(time.time() - t, 1), "summary": summ[:140]}
    names = [str(x) for x in (suites or [])]
    with cf.ThreadPoolExecutor(max(1, min(int(jobs or 1), 16))) as ex:
        res = list(ex.map(one, names))
    bad = [r for r in res if r["rc"] != 0]
    return {"results": res, "passed": len(res) - len(bad), "failed": len(bad), "verdict": "pass" if res and not bad else "fail",
            "summary": f"{len(res) - len(bad)}/{len(res)} suites green" + (": failing " + ", ".join(r["suite"] for r in bad) if bad else "")}


def diff_stats(root, base=None, files=None):
    """Lines added and removed per file, ignoring line endings. base = a folder holding the earlier copy (compared with difflib on
    splitlines, so CRLF vs LF is not a change); no base = `git diff --numstat --ignore-cr-at-eol` in root. -> {"files", "added",
    "removed", "changed_files", "summary"}, or {"error": ...} when there is nothing to compare."""
    import difflib
    rows = []
    if base:
        b = os.path.realpath(str(base))
        if not os.path.isdir(b):
            return {"error": "base folder not found"}
        names = [str(f).replace("\\", "/") for f in files or []]
        if not names:
            for top in (root, b):
                for dp, dn, fn in os.walk(top):
                    dn[:] = [x for x in dn if x not in _SKIP_DIRS]
                    names += [os.path.relpath(os.path.join(dp, f), top).replace("\\", "/") for f in fn
                              if f.endswith((".py", ".md", ".yaml", ".json", ".html", ".txt"))]
            names = sorted(set(names))

        def lines(top, rel):
            pth = _inside(top, rel)
            try:
                with open(pth, encoding="utf-8", errors="replace", newline="") as f:
                    return f.read().splitlines()
            except (OSError, TypeError):
                return []
        for rel in names:
            a, c = lines(b, rel), lines(root, rel)
            if a == c:
                continue
            add = rem = 0
            for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, c, autojunk=False).get_opcodes():
                rem += i2 - i1 if op in ("replace", "delete") else 0
                add += j2 - j1 if op in ("replace", "insert") else 0
            if add or rem:
                rows.append({"file": rel, "added": add, "removed": rem})
    else:
        import subprocess
        try:
            p = subprocess.run(["git", "-C", root, "diff", "--numstat", "--ignore-cr-at-eol", "--"] + [str(f) for f in files or []],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        except (OSError, subprocess.SubprocessError) as ex:
            return {"error": f"git not available: {ex}"}
        if p.returncode != 0:
            return {"error": "not a git repository (or git failed); pass base: <folder>"}
        for ln in p.stdout.splitlines():
            a, _, rest = ln.partition("\t")
            r, _, f = rest.partition("\t")
            if a.isdigit() and r.isdigit() and (int(a) or int(r)):
                rows.append({"file": f, "added": int(a), "removed": int(r)})
    ta, tr = sum(x["added"] for x in rows), sum(x["removed"] for x in rows)
    return {"files": rows, "added": ta, "removed": tr, "changed_files": len(rows),
            "summary": f"{len(rows)} files changed, +{ta} -{tr} (line endings ignored)"}


def _json_source(prev, args, root):
    if args.get("path"):
        pth = _inside(root, args["path"])
        if not pth or not os.path.isfile(pth):
            return None, "file not found inside the project"
        try:
            with open(pth, encoding="utf-8-sig") as f:
                return json.load(f), None
        except (OSError, ValueError) as ex:
            return None, f"not valid JSON: {str(ex)[:120]}"
    if isinstance(prev, str):
        try:
            return json.loads(prev), None
        except ValueError as ex:
            return None, f"not valid JSON: {str(ex)[:120]}"
    return prev, None


def _code_count_json(prev, ctx, args=None):
    """Count items (a list, or a list found at args.key, or the keys of an object) -- so no model does the counting."""
    args = args or {}
    val, err = _json_source(prev, args, ctx.get("root") or os.getcwd())
    if err:
        return {"count": None, "error": err}
    for k in str(args.get("key") or "").split("."):
        if k and isinstance(val, dict):
            val = val.get(k)
    if isinstance(val, (list, dict, str)):
        return {"count": len(val), "summary": f"{len(val)} items"}
    return {"count": None, "error": "nothing countable at that key"}


def _code_validate_json(prev, ctx, args=None):
    """Check JSON against a schema (args.schema, an arch_schemas schema). -> {"valid", "errors"}."""
    import arch_schemas
    args = args or {}
    val, err = _json_source(prev, args, ctx.get("root") or os.getcwd())
    if err:
        return {"valid": False, "errors": [err], "summary": err}
    bad = arch_schemas.errors(val, args["schema"]) if isinstance(args.get("schema"), dict) else []
    return {"valid": not bad, "errors": bad[:10], "summary": "valid" if not bad else f"{len(bad)} problems"}


def _code_run_suites(prev, ctx, args=None):
    args = args or {}
    return run_suites(ctx.get("root") or os.getcwd(), args.get("suites") or [], args.get("jobs", 4), args.get("timeout", 600))


def _code_diff_stats(prev, ctx, args=None):
    args = args or {}
    return diff_stats(ctx.get("root") or os.getcwd(), args.get("base"), args.get("files"))


CODE_STEPS = {"dedupe_findings": _code_dedupe_findings, "collect": _code_collect, "run_suites": _code_run_suites,
              "diff_stats": _code_diff_stats, "count_json": _code_count_json, "validate_json": _code_validate_json}


# -- the default shape: whatever writes is followed by a verifier that can only read and run tests

def shape_problems(w):
    """-> problems when the workflow can write but its last agent phase is not read-only. A phase writes when an entry names an agent
    that writes AND gives it owns:. Read-only = writes false (so no edit tool; read, grep, run_tests only)."""
    def entries(ph, tail=False):
        k = _phase_kind(ph)
        if k == "loop" and tail:
            return entries((ph["loop"].get("stages") or [{}])[-1])
        if k == "agent":
            return [ph]
        if k == "panel":
            return [{"agent": ph["panel"]}]
        if k == "parallel":
            return [e for e in ph["parallel"] if isinstance(e, dict)]
        if k == "pipeline":
            return [e for e in (ph["pipeline"] or {}).get("stages") or [] if isinstance(e, dict)]
        if k == "loop":
            return [e for st in ph["loop"].get("stages") or [] for e in entries(st)]
        return []

    def writes(e):
        try:
            return bool(load_agent(e.get("agent"))["writes"]) and bool(e.get("owns"))
        except Exception:
            return False

    phases = [ph for ph in w.get("phases") or [] if isinstance(ph, dict) and _phase_kind(ph) not in ("log", "code")]
    if not phases or not any(writes(e) for ph in phases for e in entries(ph)):
        return []
    last = entries(phases[-1], tail=True)
    if last and not any(writes(e) for e in last):
        return []
    return [f"{w.get('name')}: it can write, so its last phase must be read-only verifiers (agents with writes false), not "
            + ", ".join(sorted({str(e.get('agent')) for e in last})) + " (docs/PRINCIPLES.md 4)"]



def _resolve_workflow(path):
    p = str(path)
    if os.path.isfile(p):
        return p
    cand = os.path.join(WORKFLOWS_DIR, p if p.endswith(".yaml") else p + ".yaml")
    if os.path.isfile(cand):
        return cand
    return _find_named(p, "workflows") or p


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
    if kind == "log":
        return p if isinstance(ph["log"], str) and ph["log"].strip() else p + [f"{where}: log: a line of text"]
    if kind == "code":
        if ph["code"] not in CODE_STEPS:
            p.append(f"{where}: code: one of " + ", ".join(CODE_STEPS))
        if "with" in ph and not isinstance(ph["with"], dict):
            p.append(f"{where}: with: a mapping of arguments")
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
    if "budget" in w and Budget(w["budget"]).limits != (w["budget"] or {}):
        p.append("budget: {calls, tokens, seconds} with numbers above 0")
    ins = w.get("inputs")
    if ins is not None:
        if not isinstance(ins, dict):
            p.append("inputs: a mapping name -> {type, required, default}")
        else:
            for k, spec_ in ins.items():
                if not isinstance(spec_, dict) or spec_.get("type", "string") not in INPUT_TYPES:
                    p.append(f"inputs.{k}: type one of " + ", ".join(INPUT_TYPES))
    if "returns" in w and not (isinstance(w["returns"], dict) and w["returns"].get("type") in ("object", "array", "string")):
        p.append("returns: a JSON schema with a type")
    return p


def check_inputs(w, vars_):
    """Workflow inputs: -> (vars with defaults and converted types, problems)."""
    out, probs = dict(vars_ or {}), []
    for k, spec_ in (w.get("inputs") or {}).items():
        t = spec_.get("type", "string")
        if out.get(k) in (None, ""):
            if "default" in spec_:
                out[k] = spec_["default"]
            elif spec_.get("required"):
                probs.append(f"input {k} is required")
            continue
        v = out[k]
        try:
            if t == "integer":
                out[k] = int(v)
            elif t == "number":
                out[k] = float(v)
            elif t == "boolean":
                out[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            elif t == "list":
                out[k] = v if isinstance(v, list) else [x.strip() for x in str(v).split(",") if x.strip()]
            else:
                out[k] = str(v)
        except (TypeError, ValueError):
            probs.append(f"input {k}: not a {t} ({v!r})")
    return out, probs


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
        a = ph.get("with") if isinstance(ph.get("with"), dict) else {}
        return CODE_STEPS[ph["code"]](prev, ctx, {k: (render(v, vars_) if isinstance(v, str) else v) for k, v in a.items()})
    if kind == "log":
        crew.log(render(ph["log"], dict(vars_, prev=prev if isinstance(prev, str) else json.dumps(prev, default=str)[:500],
                                        **{"prev.summary": (prev or {}).get("summary", "") if isinstance(prev, dict) else ""})))
        return prev
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
    vars_, iprobs = check_inputs(w, vars)
    vars_["goal"] = goal
    vars_.setdefault("owns", "")
    if "budget" not in kw or kw.get("budget") is None:
        kw["budget"] = w.get("budget")
    crew = Crew(root, goal, workflow=w["name"], description=w.get("description", ""), vars=vars_, overrides=overrides, **kw)
    if iprobs:
        crew.say("[x] " + "; ".join(iprobs))
        crew._event({"type": "refused", "why": "; ".join(iprobs)})
        return crew.close("refused"), crew
    prev, phases = None, {}
    for ph in w["phases"]:
        ctx = {"goal": goal, "vars": vars_, "prev": prev, "phases": phases, "root": root}
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
        except CrewBudgetExhausted as ex:
            crew.say(f"[-] {ph['title']}: {ex}")
            crew.budget_hit = True
            return crew.close("not_sure"), crew
        if crew.budget_hit:
            crew.say(f"[-] {ph['title']}: the workflow budget ran out; the run stops here (not_sure)")
            return crew.close("not_sure"), crew
        phases[pid] = prev
        secs = round(time.time() - t0, 3)
        crew.phase_times.append({"title": ph["title"], "kind": _phase_kind(ph), "secs": secs, "skipped": False})
        crew._event({"type": "phase_done", "title": ph["title"], "kind": _phase_kind(ph), "secs": secs, "ok": prev not in (None, [])})
        crew.say(f"[{'ok' if prev not in (None, []) else '-'}] {ph['title']}: " + _cut(json.dumps(prev, ensure_ascii=False, default=str), 200))
    crew.final = prev
    if isinstance(w.get("returns"), dict):
        import arch_schemas
        bad = arch_schemas.errors(prev, w["returns"]) if prev is not None else ["no result"]
        crew._event({"type": "returns", "ok": not bad, "problems": bad[:5]})
        if bad:
            crew.say("[x] the workflow result breaks its returns: schema: " + "; ".join(bad[:3]))
            return crew.close("fail"), crew
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
    out = {}
    folders = [(folder, None)] if folder else [(WORKFLOWS_DIR, None), (_project_dir("workflows"), "project")] + \
        [(d, n) for n, d in _plugin_dirs("workflow")]
    for fn, path, plug in [(fn, os.path.join(fo, fn), pl) for fo, pl in folders if fo and os.path.isdir(fo)
                           for fn in sorted(os.listdir(fo))]:
        if not fn.endswith(".yaml") or (plug == "project" and fn[:-5] in out):
            continue
        if plug and plug != "project":
            if fn[:-5] in out:
                fn = f"{plug}:{fn}"
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


def _save_agent_md(name, d, folder):
    d = _norm(d)
    if d["name"] != name:
        raise ValueError(f"the definition is called {d['name']!r}, not {name!r}")
    probs = validate(d)
    if probs:
        raise ValueError("; ".join(probs))
    text = dump_md(d)
    if _norm(parse_agent_md(text)) != d:
        raise ValueError("would not read back the same")
    folder = folder or AGENTS_DIR
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name + ".md")
    tmp = path + f".tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


def save_agent(name, spec, folder=None, fmt="yaml"):
    """Validate a definition, then write agents/<name>.yaml atomically (temp file + os.replace). -> the path.
    ValueError with the problems when it is invalid, when its name is not `name`, or when the YAML would not read back the same;
    nothing is written then."""
    import arch_ci
    if not re.match(r"^[a-z][a-z0-9_-]{0,40}$", str(name or "")):
        raise ValueError(f"name: lower-case letters, digits, - or _ (got {str(name)[:40]!r})")
    d = _plain(dict(spec or {}))
    d.setdefault("name", name)
    if fmt == "md":
        return _save_agent_md(name, d, folder)
    if d["name"] != name:
        raise ValueError(f"the definition is called {d['name']!r}, not {name!r}")
    probs = validate(d)
    if probs:
        raise ValueError("; ".join(probs))
    order = ["name", "description", "model", "role", "category", "code", "system", "task", "tools", "writes", "output", "budget",
             "effort", "color", "max_steps", "context", "owns"]
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
    ap.add_argument("--resume", nargs="?", const="last", default=None, metavar="RUN_ID",
                    help="replay the unchanged agents of an earlier run (default: the last run of this workflow)")
    ap.add_argument("--no-replay", action="store_true", help="never replay earlier results, even with --resume")
    ap.add_argument("--no-cache", action="store_true", help="do not use the agent-step cache")
    ap.add_argument("--budget", default=None, metavar="calls=,tokens=,seconds=", help="a ceiling for the whole run")
    ap.add_argument("--runs", action="store_true", help="list the local crew runs, then stop")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.runs:
        rows = list_runs()
        for r in rows:
            print(f"{r['run_id']}: {r['workflow']} {r['status']}, {r['agents']} agents ({r['replayed']} replayed, {r['cached']} cached)")
        return 0
    try:
        budget = parse_budget(a.budget) if a.budget else None
    except ValueError as ex:
        print(f"[x] --budget: {ex}")
        return 2
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
                                    slots=a.slots, say=print, resume_from=a.resume, replay=False if a.no_replay else None,
                                    use_cache=False if a.no_cache else None, budget=budget)
    finally:
        if srv is not None:
            srv.shutdown()
    out = {"verdict": verdict, "run_id": crew.run_id, "journal": os.path.join(crew.dir, "journal.jsonl"),
           "resume_from": crew.resume_from, "replayed": crew.replayed, "cached": crew.cached, "budget": crew.budget.status(),
           "agents": [{k: r.get(k) for k in ("agentId", "label", "phase", "verdict", "why", "totals", "model", "effort", "overrides")}
                      for r in crew.records], "phases": crew.phase_times}
    print(json.dumps(out, indent=1, default=str) if a.json else
          f"{w['name']}: {verdict}; {len(crew.records)} agents, peak {crew.peak_concurrency} at once (cap {crew.slots}); journal {out['journal']}")
    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
