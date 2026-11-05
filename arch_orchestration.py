"""Orchestration view: who delegated what to whom, tokens, context, caching, and how much training data the local agents have.

`snapshot(limit=...)` returns one JSON-able dict:
  claude    Claude Code orchestrations read from local transcripts (~/.claude/projects/<slug>/): sessions, subagents, workflows,
            phases; per agent the prompt it was given, tool calls, tokens, peak context and cache hit share. A tree:
            session -> workflow -> phase -> agent (plain subagents hang under the session or the agent that spawned them).
  local     this app's own runs (.arch/runs/*/trace.jsonl of known projects + evals/runs/{train,bench}): per run the calls,
            tier/model, tokens in/out, exact cache hits, prefix reuse, ctx %, verdict.
  training  what arch_dataset would export right now (nothing is written): SFT samples, DPO pairs, distill, trajectories, and
            how many runs were left out and why.
  glossary  plain-words one-liners so the page teaches.

Read-only. Files are parsed once and cached by (mtime, size). Standard library only.
"""
import glob
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROMPT_CHARS = 600
RESULT_CHARS = 600

GLOSSARY = {
    "orchestrator": "The lead agent: it reads the task, splits it, hands pieces to helpers and stitches their answers together.",
    "subagent": "A helper the lead starts for one piece of work; it gets its own prompt and context and reports back one result.",
    "workflow": "A script that runs many subagents in a fixed plan (phases), instead of the lead starting each one by hand.",
    "phase": "One stage of a workflow (e.g. Research, then Verify); the agents in a phase usually run side by side.",
    "context window": "Everything the model reads on one turn (instructions, history, files). Peak context is the largest single turn.",
    "prompt cache (read vs write)": "Text the model already saw is kept for a while. A cache read reuses it cheaply; a cache write stores new text so the next turn can reuse it.",
    "cache hit share": "Cache reads divided by all input on those turns: how much of what the model read was reused rather than paid for fresh.",
    "tokens in/out": "Tokens are word pieces. In = what the model read; out = what it wrote. Output is slower and costlier per token.",
    "training sample": "One accepted (instruction, good answer) pair from a verified local run, used to fine-tune a local model (SFT).",
    "DPO pair": "The same task answered twice, one rejected and one accepted; training on the pair teaches the model which one to prefer.",
    "distill": "A sample whose good answer came from a stronger model, used to teach a smaller one.",
    "local agent (crew)": "The same idea run on this machine: a model tier + a brief + allowed tools + a result schema + budgets + owned files (agents/*.yaml). No weights change.",
    "category": "What kind of work an agent did (coding, ui, harness ...), decided by fixed rules: files edited first, then words in its label, phase and prompt.",
    "tool trajectory": "A whole tool-using episode (call, result, next call ...) kept so an agent learns multi-step work, not just single replies.",
}

_CACHE = {}           # path -> (mtime, size, parsed)


def _cached(path, parse):
    try:
        st = os.stat(path)
    except OSError:
        return None
    hit = _CACHE.get(path)
    key = (st.st_mtime, st.st_size, parse.__name__)
    if hit and hit[0] == key:
        return hit[1]
    val = parse(path)
    _CACHE[path] = (key, val)
    return val


def _int(x):
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


def _text(content):
    """A message content (string or list of blocks) -> plain text (text blocks and tool results only)."""
    if isinstance(content, str):
        return content
    out = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text" and isinstance(b.get("text"), str):
                    out.append(b["text"])
                elif b.get("type") == "tool_result":
                    out.append(_text(b.get("content")))
            elif isinstance(b, str):
                out.append(b)
    return "\n".join(t for t in out if t)


def _cut(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n] + " ..."


def cache_share(read, inp, write):
    tot = _int(read) + _int(inp) + _int(write)
    return round(_int(read) / tot, 4) if tot else 0.0


EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
CATEGORIES = ("coding", "end-to-end", "agentic", "ui", "system/it", "engineering", "architecture", "ai/models", "data", "harness", "other")
TINY_TOKENS = 20000   # an agent that edited nothing and used fewer tokens than this (all kinds together) starts collapsed
# The same columns for a Claude agent and a local crew agent (arch_squad): the page shows both side by side.
COLUMNS = ("source", "label", "agent_type", "model", "category", "turns", "n_tool_calls", "input", "output", "cache_read", "cache_write",
           "peak_context", "cache_hit_share", "verdict")

# ---------------------------------------------------------------- categories (deterministic; the matched reason is kept)
# Files edited decide first (strongest evidence), then keywords in the label (x3), workflow/phase (x2), prompt (x1), then role, then tools.
FILE_RULES = (
    ("ui", re.compile(r"(hub_ui\.html|\.css$|\.html?$|(^|/)static/|\.jsx?$|\.tsx?$)", re.I)),
    ("harness", re.compile(r"(selftest|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)evals?/|arch_eval|hub_check)", re.I)),
    ("agentic", re.compile(r"((^|/)agents/[^/]*\.ya?ml$|(^|/)workflows/|arch_squad|arch_fleet|arch_agent)", re.I)),
    ("data", re.compile(r"(\.sql$|\.csv$|dataset|\.jsonl$)", re.I)),
    ("system/it", re.compile(r"(\.ps1$|\.bat$|\.cmd$|\.sh$|\.reg$)", re.I)),
    ("coding", re.compile(r"\.py$", re.I)),
)
KEYWORDS = (
    ("end-to-end", r"end[- ]to[- ]end|\be2e\b|whole[- ]project|full (run|flow)|\bsmoke\b"),
    ("ui", r"\bui\b|\bux\b|hub page|\bpage\b|layout|\bcss\b|\bhtml\b|a11y|accessib|design pass|\bscreen\b"),
    ("harness", r"selftest|self-test|\beval\b|\bevals\b|verif|test suite|\btests?\b|\baudit"),
    ("agentic", r"\bagents?\b|subagent|orchestrat|workflow|tool loop|\bcrew\b|\bfleet\b"),
    ("ai/models", r"\bmodels?\b|llama|ollama|quanti|gguf|lora|fine-?tun|inference|\bkv\b|\bgpu\b|tok/s|\bvram\b"),
    ("system/it", r"windows|powershell|driver|\binstall|registry|firewall|\bdisk\b|\bdrives?\b|\bwsl\b|docker|\bnetwork"),
    ("architecture", r"\barchitecture\b|\bplan(ner|ning)?\b|\badr\b|system design|\bstructure\b|\binterface\b"),
    ("data", r"dataset|\bsql\b|\bcsv\b|\bdata\b|\bdpo\b|\bsft\b"),
    ("engineering", r"refactor|\bperformance\b|optimi[sz]|\bbugs?\b|\bfix|debug|reliab|security"),
    ("coding", r"implement|\bcode\b|\bfunction|\bpython\b|\.py\b|\bbuild(er)?\b"),
)
_KW = [(c, re.compile(rx, re.I)) for c, rx in KEYWORDS]
ROLE_CATEGORY = {"generator_ui": "ui", "accessibility_engineer": "ui", "tester": "harness", "evaluator": "harness",
                 "ml_engineer": "ai/models", "generator_sql": "data", "planner": "architecture", "architect": "architecture",
                 "architect_dev": "architecture", "generator": "coding", "fullstack": "coding", "bug_hunter": "engineering",
                 "optimizer": "engineering", "reviewer": "engineering", "network_engineer": "system/it", "config_engineer": "system/it",
                 "ci_engineer": "harness", "security_engineer": "engineering"}


def classify(label="", context="", prompt="", edited=(), tools=None, role=None):
    """-> (category, reason). Deterministic: the same inputs always give the same answer."""
    for f in edited or ():
        fs = str(f).replace("\\", "/")
        for cat, rx in FILE_RULES:
            if rx.search(fs):
                return cat, f"edited {os.path.basename(fs)}"
    score, why = {}, {}
    for text, w, where in ((label, 3, "label"), (context, 2, "workflow/phase"), (prompt, 1, "prompt")):
        for cat, rx in _KW:
            m = rx.search(text or "")
            if m:
                score[cat] = score.get(cat, 0) + w
                why.setdefault(cat, f"'{m.group(0).lower()}' in {where}")
    if score:
        best = max(CATEGORIES, key=lambda c: (score.get(c, 0), -CATEGORIES.index(c)))
        return best, "keyword " + why[best]
    if role and role in ROLE_CATEGORY:
        return ROLE_CATEGORY[role], f"role {role}"
    tools = tools or {}
    if any(t in tools for t in ("Agent", "Task")):
        return "agentic", "spawned agents"
    if any(t in tools for t in ("Bash", "PowerShell", "run_tests")):
        return "harness", "ran commands"
    return "other", "no rule matched"


def _tok_all(node):
    t = node.get("tokens") or {}
    return sum(_int(t.get(k)) for k in ("input", "output", "cache_read", "cache_write"))


def tag(node, ctx=""):
    """category + category_reason + collapsed_default on every node. Agents and sessions are classified (a declared category -- a crew
    agent's from its agents/*.yaml -- is kept); workflows and phases take their children's majority."""
    here = " ".join(x for x in (ctx, node.get("label") or "") if x) if node.get("kind") in ("workflow", "phase") else ctx
    for c in node.get("children", []):
        tag(c, here)
    if node.get("kind") in ("agent", "session"):
        if node.get("category") in CATEGORIES and node.get("category_reason"):
            pass
        else:
            node["category"], node["category_reason"] = classify(node.get("label") or "", ctx, node.get("prompt") or "",
                                                                 node.get("edited") or (), node.get("tool_calls") or {}, node.get("role"))
    else:
        votes = {}
        for c in node.get("children", []):
            votes[c.get("category", "other")] = votes.get(c.get("category", "other"), 0) + 1
        best = max(CATEGORIES, key=lambda k: (votes.get(k, 0), -CATEGORIES.index(k))) if votes else "other"
        node["category"], node["category_reason"] = best, (f"most of its {sum(votes.values())} children" if votes else "empty")
    kids = node.get("children", [])
    if node.get("kind") == "agent":
        node["collapsed_default"] = not node.get("edited") and _tok_all(node) < TINY_TOKENS and all(c.get("collapsed_default") for c in kids)
    else:
        node["collapsed_default"] = node.get("kind") != "session" and bool(kids) and all(c.get("collapsed_default") for c in kids)
    return node


def fold(node, keep, folded):
    """Drop leaf agents whose category is not in `keep` (and workflows/phases left empty); count them in `folded` {category: n}."""
    kids = []
    for c in node.get("children", []):
        fold(c, keep, folded)
        if c.get("kind") == "agent" and c.get("category") not in keep and not c.get("children"):
            folded[c.get("category")] = folded.get(c.get("category"), 0) + 1
            continue
        if c.get("kind") in ("workflow", "phase") and not c.get("children"):
            continue
        kids.append(c)
    node["children"] = kids
    return node


def count_categories(node, out):
    if node.get("kind") == "agent":
        out[node.get("category", "other")] = out.get(node.get("category", "other"), 0) + 1
    for c in node.get("children", []):
        count_categories(c, out)
    return out


def row(node):
    """One agent node -> the shared columns (Claude and local alike)."""
    t = node.get("tokens") or {}
    return {"source": node.get("source"), "label": node.get("label"), "agent_type": node.get("agent_type"), "model": node.get("model"),
            "category": node.get("category"), "turns": node.get("turns", 0), "n_tool_calls": node.get("n_tool_calls", 0),
            "input": _int(t.get("input")), "output": _int(t.get("output")), "cache_read": _int(t.get("cache_read")),
            "cache_write": _int(t.get("cache_write")), "peak_context": node.get("peak_context", 0),
            "cache_hit_share": node.get("cache_hit_share", 0.0), "verdict": node.get("verdict")}


def rows(tree):
    out = [row(tree)] if tree.get("kind") in ("agent", "session") else []
    for c in tree.get("children", []):
        out += rows(c)
    return out


# ---------------------------------------------------------------- local crew runs (arch_squad): same tree, same columns

def parse_crew_journal(path):
    """<data_dir>/orchestration/local/<run>/journal.jsonl -> {meta, agents: {id: {label, phase, verdict}}, phases: [titles]}."""
    out = {"meta": {}, "agents": {}, "phases": [], "done": None}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(j, dict):
                    continue
                t = j.get("type")
                if t == "launched":
                    out["meta"] = j
                elif t == "phase" and j.get("title") not in out["phases"]:
                    out["phases"].append(j.get("title"))
                elif t == "started" and j.get("agentId"):
                    out["agents"].setdefault(j["agentId"], {}).update(label=j.get("label"), phase=j.get("phase"), agent=j.get("agent"))
                elif t == "result" and j.get("agentId"):
                    out["agents"].setdefault(j["agentId"], {})["verdict"] = j.get("verdict")
                elif t == "done":
                    out["done"] = j
    except OSError:
        pass
    return out


def crew_agent(rec, journal_row=None):
    """A crew agent record (agent-<id>.json) -> a node with the same keys as a Claude agent. Cache figures: the server's prefix reuse
    counts as cache_read (prompt text the model did not have to read again); local servers have no paid cache write, so 0."""
    tot = rec.get("totals") or {}
    calls = rec.get("calls") or []
    tin, reused = _int(tot.get("tokens_in")), _int(tot.get("prefix_reused"))
    fresh = max(0, tin - reused)
    res = rec.get("result")
    jr = journal_row or {}
    return {"kind": "agent", "source": "local", "id": rec.get("agentId"), "label": rec.get("label") or jr.get("label"),
            "agent_type": rec.get("agent") or jr.get("agent"), "role": rec.get("role"), "model": rec.get("model"),
            "start": rec.get("start"), "end": rec.get("end"), "tool_calls": rec.get("tools") or {},
            "n_tool_calls": sum((rec.get("tools") or {}).values()), "turns": len(calls),
            "tokens": {"input": fresh, "output": _int(tot.get("tokens_out")), "cache_read": reused, "cache_write": 0},
            "cached_exact": _int(tot.get("cached_exact")), "peak_context": _int(tot.get("peak_ctx")),
            "cache_hit_share": cache_share(reused, fresh, 0), "prompt": _cut(rec.get("brief") or "", PROMPT_CHARS),
            "result": _cut(res if isinstance(res, str) else json.dumps(res, ensure_ascii=False, default=str), RESULT_CHARS) if res is not None else "",
            "verdict": rec.get("verdict") or jr.get("verdict"), "why": rec.get("why"), "edited": list(rec.get("edited") or []),
            "owns": rec.get("owns") or [], "budget": rec.get("budget"), "effort": rec.get("effort"),
            "category": rec.get("category") if rec.get("category") not in (None, "other") else None,
            "category_reason": f"declared in agents/{rec.get('agent')}.yaml" if rec.get("category") not in (None, "other") else None,
            "parent": rec.get("parent"), "children": []}


def build_crew_run(rdir):
    j = _cached(os.path.join(rdir, "journal.jsonl"), parse_crew_journal) or {}
    meta = j.get("meta") or {}
    rid = os.path.basename(rdir)
    wf = {"kind": "workflow", "source": "local", "id": rid, "label": meta.get("workflow") or rid, "description": meta.get("description"),
          "goal": meta.get("goal"), "slots": meta.get("slots"), "verdict": (j.get("done") or {}).get("verdict"),
          "peak_concurrency": (j.get("done") or {}).get("peak_concurrency"), "tokens": {}, "children": []}
    phases = {}
    for t in j.get("phases") or []:
        phases[t] = {"kind": "phase", "source": "local", "id": rid + ":" + str(t), "label": t, "tokens": {}, "children": []}
    for path in sorted(glob.glob(os.path.join(rdir, "agent-*.json"))):
        rec = _cached(path, _load_json)
        if not isinstance(rec, dict):
            continue
        a = crew_agent(rec, (j.get("agents") or {}).get(rec.get("agentId")))
        ph = a.pop("phase", None) or rec.get("phase") or "(no phase)"
        node = phases.setdefault(ph, {"kind": "phase", "source": "local", "id": rid + ":" + str(ph), "label": ph, "tokens": {}, "children": []})
        a["parent"] = a.get("parent") or node["id"]
        node["children"].append(a)
    wf["children"] = [v for v in phases.values() if v["children"]]
    _rollup(wf)
    return wf


def crew_section(limit=10, data_dir=None):
    base = os.path.join(data_dir or _data_dir(), "orchestration", "local")
    runs = []
    for jp in glob.glob(os.path.join(base, "*", "journal.jsonl")):
        try:
            runs.append((os.path.getmtime(jp), os.path.dirname(jp)))
        except OSError:
            pass
    runs.sort(reverse=True)
    trees = [build_crew_run(d) for _, d in runs[:max(1, int(limit or 10))]]
    tot = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    agents, peak = 0, 0
    for t in trees:
        for k in tot:
            tot[k] += _int(t["total"].get(k))
        agents += t["total"]["agents"]
        peak = max(peak, t["total"]["peak_context"])
    return {"runs_found": len(runs), "workflows": trees,
            "totals": dict(tot, agents=agents, workflows=len(trees), peak_context=peak, all_tokens=sum(tot.values()),
                           cache_hit_share=cache_share(tot["cache_read"], tot["input"], tot["cache_write"]))}


def local_category(run):
    """A local run (local_section row) -> (category, reason) from its flow, goal and the roles its calls used."""
    roles = [c.get("role") for c in run.get("calls") or [] if c.get("role")]
    role = max(sorted(set(roles)), key=roles.count) if roles else None
    return classify(run.get("flow") or "", "", run.get("goal") or "", (), {}, role)


# ---------------------------------------------------------------- Claude transcripts

def parse_transcript(path):
    """One transcript (.jsonl) -> per-agent numbers. Streamed assistant lines that share a message id are counted once."""
    r = {"model": None, "start": None, "end": None, "prompt": "", "result": "", "tools": {}, "turns": 0,
         "input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "peak_context": 0,
         "spawns": {}, "edited": [], "session_id": None, "agent_id": None, "title": None}
    seen = set()
    try:
        f = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return r
    with f:
        for line in f:
            if not line.strip():
                continue
            try:
                j = json.loads(line)
            except ValueError:
                continue
            if not isinstance(j, dict):
                continue
            ts = j.get("timestamp")
            if isinstance(ts, str):
                r["start"] = r["start"] or ts
                r["end"] = ts
            r["session_id"] = r["session_id"] or j.get("sessionId")
            r["agent_id"] = r["agent_id"] or j.get("agentId")
            if j.get("type") in ("summary", "custom-title") and not r["title"]:
                r["title"] = j.get("summary") or j.get("customTitle") or j.get("title")
            m = j.get("message") if isinstance(j.get("message"), dict) else {}
            if j.get("type") == "user" and not r["prompt"] and not j.get("isMeta"):
                t = _text(m.get("content"))
                if t.strip():
                    r["prompt"] = _cut(t, PROMPT_CHARS)
            if j.get("type") != "assistant":
                continue
            r["model"] = m.get("model") or r["model"]
            for b in (m.get("content") or []) if isinstance(m.get("content"), list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    name = b.get("name") or "?"
                    r["tools"][name] = r["tools"].get(name, 0) + 1
                    bi = b.get("input") if isinstance(b.get("input"), dict) else {}
                    fp = bi.get("file_path") or bi.get("notebook_path")
                    if name in EDIT_TOOLS and isinstance(fp, str) and fp not in r["edited"] and len(r["edited"]) < 50:
                        r["edited"].append(fp)
                    if name in ("Agent", "Task") and b.get("id"):
                        inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                        r["spawns"][b["id"]] = inp.get("description") or ""
            t = _text(m.get("content"))
            if t.strip():
                r["result"] = _cut(t, RESULT_CHARS)
            mid = m.get("id") or j.get("uuid")
            u = m.get("usage") if isinstance(m.get("usage"), dict) else None
            if not u or mid in seen:
                continue
            seen.add(mid)
            i, o = _int(u.get("input_tokens")), _int(u.get("output_tokens"))
            cr, cw = _int(u.get("cache_read_input_tokens")), _int(u.get("cache_creation_input_tokens"))
            r["turns"] += 1
            r["input_tokens"] += i
            r["output_tokens"] += o
            r["cache_read"] += cr
            r["cache_write"] += cw
            r["peak_context"] = max(r["peak_context"], i + cr + cw)
    return r


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def parse_journal(path):
    """workflow journal.jsonl -> {agentId: {label, phase, result}}."""
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(j, dict) or not j.get("agentId"):
                    continue
                a = out.setdefault(j["agentId"], {"label": None, "phase": None, "result": None})
                if j.get("type") == "started":
                    a["label"], a["phase"] = j.get("label"), j.get("phase")
                elif j.get("type") == "result":
                    res = j.get("result")
                    a["result"] = _cut(res if isinstance(res, str) else json.dumps(res, ensure_ascii=False), RESULT_CHARS)
    except OSError:
        pass
    return out


def parse_script_meta(path):
    """workflow script .js -> {name, description, phases:[title]} from its `export const meta = {...}`."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            src = f.read(20000)
    except OSError:
        return {}
    m = re.search(r"meta\s*=\s*\{(.*?)\n\}", src, re.S)
    body = m.group(1) if m else src
    def field(k):
        mm = re.search(k + r"\s*:\s*(['\"`])(.*?)(?<!\\)\1", body, re.S)
        return mm.group(2) if mm else None
    return {"name": field("name"), "description": field("description"),
            "phases": re.findall(r"title\s*:\s*['\"`](.*?)['\"`]", body)}


def _agent(path, meta, extra=None):
    p = _cached(path, parse_transcript) or {}
    aid = os.path.basename(path)[len("agent-"):-len(".jsonl")] if os.path.basename(path).startswith("agent-") else None
    a = {"kind": "agent", "id": aid or p.get("agent_id"), "label": (meta or {}).get("description") or "",
         "agent_type": (meta or {}).get("agentType"), "model": p.get("model"), "start": p.get("start"), "end": p.get("end"),
         "tool_calls": p.get("tools") or {}, "n_tool_calls": sum((p.get("tools") or {}).values()), "turns": p.get("turns", 0),
         "tokens": {"input": p.get("input_tokens", 0), "output": p.get("output_tokens", 0), "cache_read": p.get("cache_read", 0),
                    "cache_write": p.get("cache_write", 0)},
         "peak_context": p.get("peak_context", 0),
         "cache_hit_share": cache_share(p.get("cache_read"), p.get("input_tokens"), p.get("cache_write")),
         "prompt": p.get("prompt", ""), "result": p.get("result", ""), "edited": list(p.get("edited") or []),
         "source": "claude", "verdict": None,
         "_tool_use_id": (meta or {}).get("toolUseId"), "_spawns": p.get("spawns") or {}, "children": []}
    if extra:
        a.update({k: v for k, v in extra.items() if v})
    return a


def _sum_into(tot, node):
    t = node.get("tokens") or {}
    for k in ("input", "output", "cache_read", "cache_write"):
        tot[k] = tot.get(k, 0) + _int(t.get(k))


def _rollup(node):
    """Totals for a node = itself + children (sessions, workflows, phases have no own tokens except the session's main loop)."""
    tot = dict(node.get("tokens") or {})
    agents = 1 if node.get("kind") == "agent" else 0
    peak = node.get("peak_context", 0)
    for c in node.get("children", []):
        ct, ca, cp = _rollup(c)
        for k, v in ct.items():
            tot[k] = tot.get(k, 0) + v
        agents += ca
        peak = max(peak, cp)
    node["total"] = dict(tot, agents=agents, peak_context=peak,
                         cache_hit_share=cache_share(tot.get("cache_read"), tot.get("input"), tot.get("cache_write")))
    return tot, agents, peak


def _slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


def project_dirs(base=None, near=HERE):
    """Transcript folders for this project: those whose slug is a prefix of this folder's slug (Claude keys a project by the folder
    it was opened in, which may be a parent). Falls back to every project folder."""
    base = base or os.environ.get("ARCH_CLAUDE_PROJECTS") or os.path.join(os.path.expanduser("~"), ".claude", "projects")
    try:
        names = [n for n in os.listdir(base) if os.path.isdir(os.path.join(base, n))]
    except OSError:
        return []
    me = _slug(near).lower()
    mine = [n for n in names if me.startswith(n.lower())]
    return [os.path.join(base, n) for n in (mine or names)]


def build_session(pdir, sid):
    """One session -> tree node: session -> (subagents | workflow -> phase -> agent)."""
    main = os.path.join(pdir, sid + ".jsonl")
    sdir = os.path.join(pdir, sid)
    p = _cached(main, parse_transcript) or {}
    title = p.get("title")
    tj = _load_json(os.path.join(sdir, "custom-title.json"))
    if isinstance(tj, dict):
        title = tj.get("title") or tj.get("customTitle") or title
    sess = _agent(main, {"description": title or "main session"}) if os.path.isfile(main) else \
        {"kind": "agent", "tokens": {}, "children": [], "_spawns": {}, "tool_calls": {}}
    sess.update({"kind": "session", "id": sid, "label": title or ("session " + sid[:8]), "agent_type": "orchestrator"})
    by_tool_use = {k: sess for k in sess.get("_spawns", {})}
    plain = []
    for path in sorted(glob.glob(os.path.join(sdir, "subagents", "agent-*.jsonl"))):
        a = _agent(path, _load_json(path[:-len(".jsonl")] + ".meta.json"))
        plain.append(a)
        for k in a["_spawns"]:
            by_tool_use[k] = a
    for a in plain:                      # parent = whoever issued the Agent tool call; else the session
        parent = by_tool_use.get(a["_tool_use_id"]) or sess
        if parent is a:
            parent = sess
        a["parent"] = parent.get("id")
        parent["children"].append(a)
    scripts = {}
    for s in glob.glob(os.path.join(pdir, sid, "workflows", "scripts", "*.js")) + \
            glob.glob(os.path.join(pdir, "*", "workflows", "scripts", "*.js")):
        m = re.search(r"(wf_[0-9a-f-]+)\.js$", s)
        if m:
            scripts.setdefault(m.group(1), s)
    for wdir in sorted(glob.glob(os.path.join(sdir, "subagents", "workflows", "wf_*"))):
        wid = os.path.basename(wdir)
        jr = _cached(os.path.join(wdir, "journal.jsonl"), parse_journal) or {}
        sm = _cached(scripts[wid], parse_script_meta) if wid in scripts else {}
        sm = sm or {}
        wf = {"kind": "workflow", "id": wid, "label": sm.get("name") or wid, "description": sm.get("description"),
              "parent": sid, "source": "claude", "tokens": {}, "children": []}
        phases = {}
        for title in sm.get("phases") or []:
            phases[title] = {"kind": "phase", "id": wid + ":" + title, "label": title, "tokens": {}, "children": []}
        for path in sorted(glob.glob(os.path.join(wdir, "agent-*.jsonl"))):
            meta = _load_json(path[:-len(".jsonl")] + ".meta.json") or {}
            aid = os.path.basename(path)[len("agent-"):-len(".jsonl")]
            j = jr.get(aid) or {}
            a = _agent(path, meta, {"label": j.get("label"), "result": j.get("result")})
            ph = j.get("phase") or meta.get("workflowPhase") or "(no phase)"
            node = phases.setdefault(ph, {"kind": "phase", "id": wid + ":" + ph, "label": ph, "tokens": {}, "children": []})
            a["parent"] = node["id"]
            node["children"].append(a)
        wf["children"] = [v for v in phases.values() if v["children"]]
        sess["children"].append(wf)
    _rollup(sess)
    return sess


def _strip(node):
    for k in ("_tool_use_id", "_spawns"):
        node.pop(k, None)
    for c in node.get("children", []):
        _strip(c)
    return node


def claude_section(limit=5, base=None, near=HERE):
    sessions = []
    for pdir in project_dirs(base, near):
        for main in glob.glob(os.path.join(pdir, "*.jsonl")):
            try:
                sessions.append((os.path.getmtime(main), pdir, os.path.basename(main)[:-len(".jsonl")]))
            except OSError:
                pass
    sessions.sort(reverse=True)
    trees = [_strip(build_session(pdir, sid)) for _, pdir, sid in sessions[:max(1, int(limit or 5))]]
    tot = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    agents = workflows = 0
    peak = 0
    for t in trees:
        for k in tot:
            tot[k] += _int(t["total"].get(k))
        agents += t["total"]["agents"]
        peak = max(peak, t["total"]["peak_context"])
        workflows += sum(1 for c in t["children"] if c.get("kind") == "workflow")
    return {"sessions_found": len(sessions), "sessions": trees,
            "totals": dict(tot, agents=agents, workflows=workflows, peak_context=peak,
                           all_tokens=sum(tot.values()),
                           cache_hit_share=cache_share(tot["cache_read"], tot["input"], tot["cache_write"]))}


# ---------------------------------------------------------------- local runs

def _data_dir():
    try:
        import arch_core
        return arch_core.data_dir()
    except Exception:
        return os.environ.get("ARCHITECT_DATA_DIR") or os.path.join(HERE, "hub_data")


def run_roots(data_dir=None, roots=None):
    """(bucket, root) pairs: known projects (arch_costs' portfolio + cwd) and the kept eval runs."""
    out = []
    if roots is None:
        try:
            import arch_costs
            roots = arch_costs._roots(None)
        except Exception:
            roots = [os.getcwd()]
    for r in roots:
        out.append(("project", os.path.abspath(r)))
    dd = data_dir or _data_dir()
    for b in ("train", "bench"):
        p = os.path.join(dd, "evals", "runs", b)
        if os.path.isdir(p):
            out.append((b, os.path.abspath(p)))
    seen, uniq = set(), []
    for b, r in out:
        if r not in seen:
            seen.add(r)
            uniq.append((b, r))
    return uniq


def parse_run(trace):
    """trace.jsonl -> the run's flow (one entry per model call) and verdict."""
    calls, verdict, flow, meter = [], None, None, None
    try:
        f = open(trace, "r", encoding="utf-8", errors="replace")
    except OSError:
        return {"calls": [], "verdict": None}
    with f:
        for line in f:
            if '"call"' not in line and '"verdict"' not in line:
                continue
            try:
                j = json.loads(line)
            except ValueError:
                continue
            if not isinstance(j, dict):
                continue
            if j.get("type") == "call":
                flow = flow or j.get("flow")
                calls.append({"id": j.get("id"), "t": j.get("t"), "role": j.get("role"), "tier": j.get("tier"),
                              "model": j.get("model"), "transport": j.get("transport"),
                              "tokens_in": _int(j.get("prompt_tokens")), "tokens_out": _int(j.get("gen_tokens")),
                              "cached_exact": bool(j.get("cached_exact")) or j.get("done_reason") == "cached",
                              "prefix_reused": _int(j.get("prefix_reused") or j.get("prefix_reused_est")),
                              "ctx_pct": j.get("ctx_pct"), "num_ctx": j.get("num_ctx"), "secs": j.get("secs")})
            elif j.get("type") == "verdict":
                verdict, flow, meter = j.get("verdict"), flow or j.get("flow"), j.get("meter")
    return {"calls": calls, "verdict": verdict, "flow": flow, "meter": meter}


def local_section(limit=20, data_dir=None, roots=None):
    runs = []
    for bucket, root in run_roots(data_dir, roots):
        for tp in glob.glob(os.path.join(root, ".arch", "runs", "*", "trace.jsonl")):
            try:
                runs.append((os.path.getmtime(tp), bucket, root, tp))
            except OSError:
                pass
    runs.sort(reverse=True)
    out, tot = [], {"runs": len(runs), "calls": 0, "tokens_in": 0, "tokens_out": 0, "cached_exact": 0, "prefix_reused": 0}
    by_tier = {}
    for m, bucket, root, tp in runs:
        r = _cached(tp, parse_run) or {}
        calls = r.get("calls") or []
        for c in calls:
            tot["calls"] += 1
            tot["tokens_in"] += c["tokens_in"]
            tot["tokens_out"] += c["tokens_out"]
            tot["cached_exact"] += 1 if c["cached_exact"] else 0
            tot["prefix_reused"] += c["prefix_reused"]
            row = by_tier.setdefault(c.get("tier") or "?", {"calls": 0, "tokens_in": 0, "tokens_out": 0, "models": {}})
            row["calls"] += 1
            row["tokens_in"] += c["tokens_in"]
            row["tokens_out"] += c["tokens_out"]
            mk = c.get("model") or "?"
            row["models"][mk] = row["models"].get(mk, 0) + 1
        if len(out) < max(1, int(limit or 20)):
            ti = sum(c["tokens_in"] for c in calls)
            st = _cached(os.path.join(os.path.dirname(tp), "run.json"), _load_json)
            goal = st.get("goal") if isinstance(st, dict) else None
            out.append({"run": os.path.basename(os.path.dirname(tp)), "bucket": bucket, "root": root, "mtime": m,
                        "flow": r.get("flow"), "verdict": r.get("verdict"), "n_calls": len(calls),
                        "tokens_in": ti, "tokens_out": sum(c["tokens_out"] for c in calls),
                        "cached_exact": sum(1 for c in calls if c["cached_exact"]),
                        "prefix_reused": sum(c["prefix_reused"] for c in calls),
                        "prefix_reuse_share": round(sum(c["prefix_reused"] for c in calls) / ti, 4) if ti else 0.0,
                        "peak_ctx_pct": max([_int(c.get("ctx_pct")) for c in calls] or [0]), "goal": _cut(goal or "", 200), "calls": calls})
            out[-1]["category"], out[-1]["category_reason"] = local_category(out[-1])
    tot["prefix_reuse_share"] = round(tot["prefix_reused"] / tot["tokens_in"], 4) if tot["tokens_in"] else 0.0
    return {"totals": tot, "by_tier": by_tier, "runs": out}


# ---------------------------------------------------------------- training data

def training_section(data_dir=None, project_root=None):
    """Counts from arch_dataset.build() (in memory, nothing written) over this project + the kept train-bucket runs."""
    try:
        import arch_dataset
    except Exception as e:
        return {"error": f"arch_dataset unavailable: {e}"}
    dd = data_dir or _data_dir()
    train = os.path.join(dd, "evals", "runs", "train")
    rl = [os.path.abspath(project_root or os.getcwd())]
    if os.path.isdir(os.path.join(train, ".arch", "runs")) and os.path.abspath(train) not in rl:
        rl.append(os.path.abspath(train))
    try:
        data = arch_dataset.build(rl)
        rep = arch_dataset.report(data)
    except Exception as e:
        return {"error": f"dataset build failed: {type(e).__name__}: {e}"}
    st = data.get("stats") or {}

    def per_role(key):
        d = data.get(key) or {}
        return {k: len(v) for k, v in d.items()} if isinstance(d, dict) else {}
    counts = {"sft": sum(per_role("sft").values()), "dpo": sum(per_role("dpo").values()),
              "distill": sum(per_role("distill").values()), "trajectories": len(data.get("traj") or [])}
    train_runs = len(arch_dataset.runs_of(train))
    return {"roots": rl, "counts": counts, "sft_by_role": per_role("sft"), "dpo_by_role": per_role("dpo"),
            "distill_by_role": per_role("distill"), "by_tier": rep.get("by_tier", {}), "by_flow": rep.get("by_flow", {}),
            "by_quality": rep.get("by_quality", {}),
            "runs_read": _int(st.get("runs")), "train_bucket_runs": train_runs,
            "excluded": {"not_passed": _int(st.get("eval_not_passed")), "assist": _int(st.get("assisted")),
                         "by_claude": _int(st.get("claude")), "not_sure": _int(st.get("not_sure")),
                         "rejected": _int(st.get("rejected")), "duplicate": _int(st.get("dup")),
                         "low_quality": _int(st.get("low_quality")), "cached": _int(st.get("cached")),
                         "secrets": _int(st.get("secrets_skipped")), "too_long": _int(st.get("too_long"))},
            "dropped": rep.get("dropped", {})}


# ---------------------------------------------------------------- entry points

def _keep(categories):
    if categories is None:
        return [c for c in CATEGORIES if c != "other"]
    cs = [categories] if isinstance(categories, str) else list(categories)
    return [c for c in CATEGORIES if c in cs]


def apply_categories(out, categories=None):
    """Tag every tree (Claude sessions, crew workflows) and local run; keep the chosen categories (default: all but "other") and fold
    the rest into counts. Totals are computed before folding, so they stay honest."""
    keep = _keep(categories)
    seen, folded = {}, {}
    for key, lst in (("claude", "sessions"), ("crew", "workflows")):
        sec = out.get(key) or {}
        for t in sec.get(lst) or []:
            tag(t)
            count_categories(t, seen)
            fold(t, keep, folded)
        if sec.get(lst) is not None:
            sec[lst] = [t for t in sec[lst] if t.get("kind") == "session" or t.get("children")]
    loc = out.get("local") or {}
    if loc.get("runs") is not None:
        shown = []
        for r in loc["runs"]:
            seen[r.get("category", "other")] = seen.get(r.get("category", "other"), 0) + 1
            if r.get("category") in keep:
                shown.append(r)
            else:
                folded[r.get("category")] = folded.get(r.get("category"), 0) + 1
        loc["runs"] = shown
    out["categories"] = {"all": list(CATEGORIES), "shown": keep, "counts": seen, "folded": folded,
                         "folded_total": sum(folded.values())}
    return out


def snapshot(limit=5, run_limit=20, claude_base=None, data_dir=None, roots=None, project_root=None, near=HERE, categories=None,
             crew_limit=10):
    """categories: a list from CATEGORIES to show; None = every one except "other", which is folded into one collapsed count."""
    t0 = time.time()
    out = {"generated": t0, "glossary": dict(GLOSSARY), "columns": list(COLUMNS)}
    for key, fn in (("claude", lambda: claude_section(limit, claude_base, near)),
                    ("crew", lambda: crew_section(crew_limit, data_dir)),
                    ("local", lambda: local_section(run_limit, data_dir, roots)),
                    ("training", lambda: training_section(data_dir, project_root))):
        try:
            out[key] = fn()
        except Exception as e:                      # one broken source never blanks the page
            out[key] = {"error": f"{type(e).__name__}: {e}"}
    try:
        apply_categories(out, categories)
    except Exception as e:
        out["categories"] = {"error": f"{type(e).__name__}: {e}"}
    out["secs"] = round(time.time() - t0, 3)
    return out


def headline(s):
    c = (s.get("claude") or {}).get("totals") or {}
    l = (s.get("local") or {}).get("totals") or {}
    t = s.get("training") or {}
    tc = t.get("counts") or {}
    w = (s.get("crew") or {}).get("totals") or {}
    cat = s.get("categories") or {}
    return "\n".join([
        f"Claude: {(s.get('claude') or {}).get('sessions_found', 0)} sessions, {c.get('agents', 0)} agents, "
        f"{c.get('workflows', 0)} workflows (newest shown); tokens in {c.get('input', 0):,} out {c.get('output', 0):,} "
        f"cache read {c.get('cache_read', 0):,} write {c.get('cache_write', 0):,}; cache hit share {c.get('cache_hit_share', 0):.1%}; "
        f"peak context {c.get('peak_context', 0):,}",
        f"Local: {l.get('runs', 0)} runs, {l.get('calls', 0)} calls, tokens in {l.get('tokens_in', 0):,} out {l.get('tokens_out', 0):,}, "
        f"exact cache hits {l.get('cached_exact', 0)}, prefix reuse {l.get('prefix_reuse_share', 0):.1%}",
        f"Crew: {(s.get('crew') or {}).get('runs_found', 0)} local workflows, {w.get('agents', 0)} agents; tokens in {w.get('input', 0):,} "
        f"out {w.get('output', 0):,} cache read {w.get('cache_read', 0):,}; peak context {w.get('peak_context', 0):,}",
        f"Categories: {cat.get('counts', {})}; hidden {cat.get('folded_total', 0)}",
        (f"Training: SFT {tc.get('sft', 0)}, DPO {tc.get('dpo', 0)}, distill {tc.get('distill', 0)}, trajectories "
         f"{tc.get('trajectories', 0)}; train-bucket runs {t.get('train_bucket_runs', 0)}; excluded {t.get('excluded', {})}")
        if not t.get("error") else f"Training: {t['error']}"])


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    limit = 5
    if "--limit" in argv:
        try:
            limit = int(argv[argv.index("--limit") + 1])
        except (IndexError, ValueError):
            pass
    cats = None
    if "--categories" in argv:
        try:
            cats = [c.strip() for c in argv[argv.index("--categories") + 1].split(",") if c.strip()]
        except IndexError:
            pass
    s = snapshot(limit=limit, categories=cats)
    print(json.dumps(s, indent=1, ensure_ascii=False, default=str) if as_json else headline(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
