"""Sharpen a request before it runs: a vague goal becomes a precise one, grounded in THIS project.

`sharpen(root, goal)` -> the goal rewritten precisely, the flow that should run it, the files it touches (each with why, and whether it
exists, is new, or is unknown), checkable acceptance statements plus a draft test file, risks, questions (only when the request is truly
ambiguous) and an estimate of what the run will cost.

The model proposes; code grounds. A file the model names must exist in the project, or be marked new and lie inside it; anything else is
flagged "unknown", never silently trusted. A function name counts only when `arch_chat.hint_items` (the same name match + index Studio
uses) or the target file's own source finds it as a def/class. The flow must be a registered one, else the keyword router's pick is used.
The estimate is code arithmetic, labelled estimate. When no model answers (or the reply breaks the schema) the result still comes back,
built from keywords and code hints alone, with `source = "code"`.

Local by default (the router tier, then the worker on a broken reply). Claude only when use="claude" AND `arch_cloud.allowed()` says yes
for a request the person sent by hand; otherwise it says why and stays local. Nothing here writes a file or changes a setting.
"""
import ast
import os
import re

import arch_core as core
import arch_schemas

ROLES_LOCAL = ("router", "worker")       # the router tier first (fast); the worker gets one more try when the reply breaks the schema
MAX_FILES = 8
MAX_ACCEPT = 8
MAX_QUESTIONS = 3
CTX_LOCAL = 9000                         # characters of project context for a local model (fitted again by Engine._call)
CTX_CLAUDE = 40000                       # a Claude call is not cut to a local window (CLAUDE.md: Claude prompts get the full window)

# Rough calls per flow for the estimate (plan + tries + checks + review). An ESTIMATE, not a measurement: say so wherever it is shown.
FLOW_CALLS = {"bugfix": 6, "build": 10, "new": 12, "feature": 14, "refactor": 6, "test": 6, "optimize": 7, "hunt": 6, "review": 3,
              "design": 4, "secure": 7, "harden": 6, "types": 5, "document": 5, "migrate": 8, "a11y": 5, "deps": 3}
DEFAULT_CALLS = 6
SECONDS_PER_CALL_LOCAL = 45              # estimate: ~1500 tokens out at ~35 tok/s plus the prompt, on a laptop-class card
SECONDS_PER_CALL_CLAUDE = 20
TOKENS_IN_PER_CALL, TOKENS_OUT_PER_CALL = 12000, 1500


def schema(flow_ids):
    """The shape a sharpen reply must have. `flow` is constrained to the registered flows."""
    s = {"type": "string"}
    return {"type": "object", "required": ["goal", "flow", "target_files", "acceptance"], "properties": {
        "goal": {"type": "string", "minLength": 8},
        "flow": {"type": "string", "enum": sorted(flow_ids)},
        "target_files": {"type": "array", "maxItems": 12, "items": {"type": "object", "required": ["path", "why"], "properties": {
            "path": {"type": "string", "minLength": 1}, "why": s, "new": {"type": "boolean"}}}},
        "functions": {"type": "array", "maxItems": 20, "items": s},
        "acceptance": {"type": "array", "minItems": 1, "maxItems": 12, "items": {"type": "string", "minLength": 3}},
        "test_path": s, "test_draft": s,
        "risks": {"type": "array", "maxItems": 8, "items": s},
        "ambiguous": {"type": "boolean"},
        "questions": {"type": "array", "maxItems": 6, "items": s}}}


PROMPT = ("Rewrite a change request for a team of coding models so that it is precise, small and checkable, using ONLY what the PROJECT "
          "CONTEXT and RELEVANT CODE show.\n"
          "- goal: one or two sentences: what changes, where (file and function names), and how anyone can tell it is done.\n"
          "- flow: the workflow that should run it.\n"
          "- target_files: the files to change, each with why. Use paths exactly as shown. A file that does not exist yet: set new=true.\n"
          "- functions: the existing function/class names the change touches, exactly as written in the code.\n"
          "- acceptance: short statements a test or a reader can check (inputs -> expected result).\n"
          "- test_path + test_draft: a small pytest file that would pass once the change is done (test_*.py).\n"
          "- risks: what could break.\n"
          "- ambiguous: true ONLY when two reasonable readings lead to different code; then questions (at most 3). Otherwise "
          "ambiguous=false and no questions: choose the most reasonable reading.\n"
          "Do not invent files or names. Answer ONLY JSON.\n\n")


# ---------------------------------------------------------------------------------------------------- helpers (pure code)
def _norm(p):
    p = str(p or "").strip().strip("`'\"").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _inside(root, rel):
    if not rel or rel.startswith("/") or (len(rel) > 1 and rel[1] == ":") or ".." in rel.split("/"):
        return False
    full = os.path.abspath(os.path.join(root, rel))
    return full == os.path.abspath(root) or full.startswith(os.path.abspath(root) + os.sep)


def keyword_flow(goal, empty=False):
    """The router's own keyword rule (arch_flows.KEYWORDS / PRIORITY), no model call. -> (flow, why)."""
    import arch_flows
    g = goal.lower()
    score = {k: len(re.findall(p, g)) for k, p in arch_flows.KEYWORDS.items() if k in arch_flows.FLOWS}
    if "Traceback" in goal and "bugfix" in score:
        score["bugfix"] += 3
    if empty:
        for k in ("test", "optimize", "hunt", "review", "refactor", "bugfix"):
            score.pop(k, None)
    if not score or max(score.values()) == 0:
        d = "new" if empty and "new" in arch_flows.FLOWS else "build"
        return d, "default (no keyword matched)"
    pri = arch_flows.PRIORITY
    top = sorted(score.items(), key=lambda kv: (-kv[1], pri.index(kv[0]) if kv[0] in pri else 99))[0][0]
    if empty and top == "build" and "new" in arch_flows.FLOWS:
        top = "new"
    return top, "keyword match"


def _defs_in_items(items):
    """Names defined in code_hints hits ("path:line\\n<lines>") -> {name: "path:line"}."""
    out = {}
    for it in items or []:
        head, _, body = str(it).partition("\n")
        for ln in body.splitlines():
            m = re.match(r"\s*(?:async\s+)?(?:def|class|function|func|fn)\s+([A-Za-z_]\w*)", ln)
            if m:
                out.setdefault(m.group(1), head)
    return out


def _defs_in_file(root, rel):
    """def/class names in one project file (Python by AST, others by the same line pattern code_hints uses). -> {name: "rel:line"}."""
    try:
        text = core.read_text(os.path.join(root, rel), 400000)[0]
    except OSError:
        return {}
    out = {}
    if rel.endswith(".py"):
        try:
            for n in ast.walk(ast.parse(text)):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    out.setdefault(n.name, f"{rel}:{n.lineno}")
            return out
        except (SyntaxError, ValueError):
            pass
    for i, ln in enumerate(text.splitlines(), 1):
        m = re.match(r"\s*(?:async\s+)?(?:def|class|function|func|fn)\s+([A-Za-z_]\w*)", ln)
        if m:
            out.setdefault(m.group(1), f"{rel}:{i}")
    return out


def _hints(e, text, limit=8):
    try:
        import arch_chat
        return arch_chat.hint_items(e, text, limit)
    except Exception:
        return []


def estimate(flow, use="local", model=""):
    """-> {calls, seconds, usd, basis}. Code arithmetic from FLOW_CALLS; always labelled an estimate."""
    calls = FLOW_CALLS.get(flow, DEFAULT_CALLS)
    if use == "claude":
        import arch_cloud
        pin, pout = arch_cloud.PRICES.get(arch_cloud.api_model(model), (4.0, 20.0))
        usd = calls * (TOKENS_IN_PER_CALL * pin + TOKENS_OUT_PER_CALL * pout) / 1e6
        return {"calls": calls, "seconds": calls * SECONDS_PER_CALL_CLAUDE, "usd": round(usd, 2),
                "basis": f"estimate: {calls} calls x ~{TOKENS_IN_PER_CALL} in / {TOKENS_OUT_PER_CALL} out tokens at the vendor price card"}
    return {"calls": calls, "seconds": calls * SECONDS_PER_CALL_LOCAL, "usd": 0.0,
            "basis": f"estimate: {calls} calls x ~{SECONDS_PER_CALL_LOCAL} s each on your own models (no money spent)"}


def ground(root, raw, hint_defs, flow_ids, goal):
    """Check a model's proposal against the project. -> the result dict (without estimate/source). Pure code; never calls a model."""
    flags = []
    files, seen = [], set()
    for tf in (raw.get("target_files") or [])[:12]:
        rel = _norm(tf.get("path") if isinstance(tf, dict) else tf)
        why = str((tf.get("why") if isinstance(tf, dict) else "") or "").strip()
        if not rel or rel in seen:
            continue
        seen.add(rel)
        if not _inside(root, rel):
            flags.append(f"{rel}: outside the project, dropped")
            continue
        if os.path.isfile(os.path.join(root, rel)):
            status = "exists"
        elif isinstance(tf, dict) and tf.get("new") is True:
            status = "new"
        else:
            status = "unknown"
            flags.append(f"{rel}: no such file in the project and not marked new")
        files.append({"path": rel, "why": why or "(no reason given)", "status": status})
    files = files[:MAX_FILES]
    known = dict(hint_defs)
    for f in files:
        if f["status"] == "exists":
            for k, v in _defs_in_file(root, f["path"]).items():
                known.setdefault(k, v)
    funcs = []
    for name in (raw.get("functions") or [])[:20]:
        n = str(name or "").strip().strip("`()")
        short = n.split(".")[-1].split("::")[-1]
        if not short:
            continue
        where = known.get(short)
        funcs.append({"name": n, "found": bool(where), "where": where or ""})
        if not where:
            flags.append(f"{n}: no function or class by that name was found in the project")
    flow = raw.get("flow") if raw.get("flow") in flow_ids else None
    acc = [str(a).strip() for a in (raw.get("acceptance") or []) if str(a or "").strip()][:MAX_ACCEPT]
    tpath = _norm(raw.get("test_path"))
    draft = str(raw.get("test_draft") or "")
    parses = None
    if draft.strip():
        if not tpath or not _inside(root, tpath):
            tpath = "tests/test_" + (re.sub(r"\W+", "_", (files[0]["path"].rsplit("/", 1)[-1].rsplit(".", 1)[0] if files else "change")).strip("_") or "change") + ".py"
        try:
            ast.parse(draft)
            parses = True
        except (SyntaxError, ValueError) as ex:
            parses = False
            flags.append(f"the draft test does not parse yet ({type(ex).__name__}: {str(ex)[:80]})")
    risks = [str(r).strip() for r in (raw.get("risks") or []) if str(r or "").strip()][:6]
    qs = [str(q).strip() for q in (raw.get("questions") or []) if str(q or "").strip()]
    qs = qs[:MAX_QUESTIONS] if raw.get("ambiguous") is True else []
    new_goal = str(raw.get("goal") or "").strip() or goal
    return {"goal": new_goal, "original": goal, "flow": flow, "target_files": files, "functions": funcs,
            "acceptance": {"statements": acc, "test_path": tpath if draft.strip() else "", "test_draft": draft, "test_parses": parses},
            "risks": risks, "questions": qs, "flags": flags}


# ---------------------------------------------------------------------------------------------------- the entry point
def sharpen(root, goal, use="local", engine=None, cfg=None):
    """-> {goal, original, flow, flow_why, target_files [{path, why, status}], functions [{name, found, where}],
    acceptance {statements, test_path, test_draft, test_parses}, risks, questions, flags, estimate {calls, seconds, usd, basis},
    source ("local" | "claude" | "code"), model, notes}. `engine` is for tests (a scripted Engine); nothing is written."""
    import arch_flows
    goal = str(goal or "").strip()
    e = engine or core.Engine(root, "sharpen", goal, cfg=cfg, review="dry", say=lambda m: None, persist=False)
    root = e.root
    notes = []
    empty = not core.list_files(root, e.cfg)
    kw_flow, kw_why = keyword_flow(goal, empty)
    items = [] if empty else _hints(e, goal, 8)
    hint_defs = _defs_in_items(items)
    flow_ids = list(arch_flows.FLOWS)

    want_claude = str(use or "local").startswith("claude")
    claude_model = ""
    if want_claude:
        import arch_cloud
        with arch_cloud.asked_for():
            ok, why = arch_cloud.allowed(e.cfg)
        if ok:
            claude_model = use if str(use).startswith("claude:") else arch_cloud.chosen_model(e.cfg)
        else:
            notes.append("Claude was not used: " + why + ". Your own models sharpened this instead.")

    try:
        ctx = e.context(goal, CTX_CLAUDE if claude_model else CTX_LOCAL) if not empty else "(the project folder is empty)"
    except Exception as ex:                                  # retrieval trouble never stops a sharpen: the code hints still go in
        ctx = "(project context could not be built)"
        notes.append(f"project context failed: {type(ex).__name__}")
    hints_txt = ("RELEVANT CODE FOUND IN THE PROJECT:\n\n" + "\n\n".join(items) + "\n\n") if items else ""
    user = (PROMPT + f"PROJECT CONTEXT:\n{ctx}\n\n" + hints_txt
            + f"A keyword matcher suggests the flow: {kw_flow} (weak hint).\n\nREQUEST: {goal}")
    sch = schema(flow_ids)

    raw, source, model = None, "code", ""
    if claude_model:
        import arch_cloud
        old = (e.cfg.get("roles") or {}).get("router")
        e.cfg.setdefault("roles", {})["router"] = claude_model
        try:
            with arch_cloud.asked_for():
                r, raw = e.ask_json("router", user, schema=sch)
            source, model = "claude", claude_model
        except (core.LLMError, core.BudgetExceeded, core.LoopDetected) as ex:
            notes.append(f"Claude did not answer ({str(ex)[:100]}); trying your own models.")
        finally:
            if old is None:
                e.cfg["roles"].pop("router", None)
            else:
                e.cfg["roles"]["router"] = old
        if raw is None and source == "claude":
            notes.append("Claude's reply did not have the expected shape; trying your own models.")
            source = "code"
    if raw is None:
        for role in ROLES_LOCAL:
            try:
                r, raw = e.ask_json(role, user, schema=sch)
            except (core.LLMError, core.BudgetExceeded, core.LoopDetected) as ex:
                notes.append(f"the {role} model did not answer ({str(ex)[:100]})")
                raw = None
                if isinstance(ex, core.BudgetExceeded):
                    break
                continue
            if isinstance(raw, dict):
                source, model = "local", str((r or {}).get("model") or role)
                break
            notes.append(f"the {role} model's reply did not have the expected shape")

    if isinstance(raw, dict):
        out = ground(root, raw, hint_defs, flow_ids, goal)
        if out["flow"] is None:
            out["flow"], out["flow_why"] = kw_flow, kw_why + " (the model named no known flow)"
        else:
            out["flow_why"] = "model" + (" (agrees with the keyword hint)" if out["flow"] == kw_flow else f" (keywords suggested {kw_flow})")
        if empty and out["flow"] in ("test", "optimize", "hunt", "review", "refactor", "bugfix"):
            out["flow"], out["flow_why"] = "new", "the project folder is empty: nothing exists to change yet (checked in code)"
    else:
        # No usable model reply: still useful, from code alone. Files = where the code hints point; no invented acceptance.
        paths = []
        for it in items:
            p = str(it).split("\n", 1)[0].rsplit(":", 1)[0]
            if p and p not in paths:
                paths.append(p)
        out = ground(root, {"target_files": [{"path": p, "why": "code found by name/index match"} for p in paths[:4]]},
                     hint_defs, flow_ids, goal)
        out["flow"], out["flow_why"] = kw_flow, kw_why + " (no model answered)"
        out["functions"] = [{"name": n, "found": True, "where": w} for n, w in list(hint_defs.items())[:6]]
        notes.append("No model gave a usable answer: this was put together from keywords and code hints only.")
    out["estimate"] = estimate(out["flow"], "claude" if source == "claude" else "local", model)
    out["source"], out["model"], out["notes"] = source, model, notes
    return out


def summary(res):
    """Plain words for the hub."""
    if not isinstance(res, dict):
        return "Nothing to show."
    lines = [f"Sharpened goal: {res.get('goal', '')}",
             f"Workflow: {res.get('flow', '?')} ({res.get('flow_why', '')})"]
    src = {"local": "your own model", "claude": "Claude", "code": "code only (no model answered)"}.get(res.get("source"), res.get("source"))
    lines.append(f"Written by: {src}" + (f" ({res['model']})" if res.get("model") else ""))
    tf = res.get("target_files") or []
    if tf:
        lines.append("Files:")
        tag = {"exists": "", "new": " [new]", "unknown": " [NOT FOUND - check this]"}
        lines += [f"  - {f['path']}{tag.get(f['status'], '')}: {f['why']}" for f in tf]
    fn = res.get("functions") or []
    if fn:
        lines.append("Functions: " + ", ".join(f"{f['name']} ({f['where']})" if f["found"] else f"{f['name']} (not found)" for f in fn))
    acc = res.get("acceptance") or {}
    if acc.get("statements"):
        lines.append("Done when:")
        lines += [f"  - {a}" for a in acc["statements"]]
    if acc.get("test_draft"):
        lines.append(f"Draft test: {acc.get('test_path')}" + ("" if acc.get("test_parses") else " (does not parse yet)"))
    if res.get("risks"):
        lines.append("Risks: " + "; ".join(res["risks"]))
    if res.get("questions"):
        lines.append("Before running, please answer:")
        lines += [f"  ? {q}" for q in res["questions"]]
    for f in res.get("flags") or []:
        lines.append(f"  ! {f}")
    est = res.get("estimate") or {}
    if est:
        m, s = divmod(int(est.get("seconds") or 0), 60)
        cost = "no money" if not est.get("usd") else f"about ${est['usd']:.2f}"
        lines.append(f"Estimate: about {est.get('calls')} model calls, ~{m} min {s} s, {cost} ({est.get('basis', 'estimate')})")
    for n in res.get("notes") or []:
        lines.append(f"Note: {n}")
    return "\n".join(lines)
