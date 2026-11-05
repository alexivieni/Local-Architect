"""Harness packs: a named bundle of parts the Architect already has, checked in code before anything runs.

A pack is a folder harnesses/<name>/ holding
    harness.yaml   the spec it was composed from (the one source of truth; flow.yaml and settings.json are built from it)
    flow.yaml      the flow it runs, validated by arch_dataflow.validate (a copy of an installed flow, or the pack's own steps)
    settings.json  the settings overlay: only keys arch_params knows, each validated; applied for ONE run through --set,
                   never written to the person's settings file
    fleet.json     (only when the spec names agents) the plan arch_fleet.launch takes, already passed arch_fleet.plan_check
    README.md      what the pack does and why each part is there

The spec:
    name         a-z, 0-9, _ (2-31 chars); also the pack flow's id, so it may not be an installed flow's id
    title, blurb plain words
    goal_kind    fix | feature | build | review | explore | refactor | secure | docs | traces | any
    tiers        {router|worker|heavy|draft|cloud: model}; "" = whatever this machine already uses (never blanked); a list = "uses these"
    flow         an installed flow id (flows/<id>.yaml), or {title, kind, steps: [...], verdict: {...}} with prompts named "<flow>/<file>"
    gates        extra code checks that can stand on their own (STANDALONE): tests_green_before / characterization_pinned run first,
                 tests_pass / determinism run last and join the verdict. A check that could not run makes the verdict not_sure.
    tools        the tools a tool_loop step may still call (each step keeps only the ones it had AND you allow)
    claude_mode  off | escalation | chat | plan | all  (online.cloud_tier); any mode but off needs budgets.usd_per_run
    budgets      {calls, tokens, seconds, heavy_calls}  -> the flow's own ceiling (also caps the per-machine overrides)
                 {usd_per_run, usd_per_day}             -> limits.cloud_usd_per_run / _per_day
                 {fleet_usd}                            -> the total cap the agents' plan must fit
    agents       [{template, values, name?, kind?, budget_usd?}]  agent_templates/*.md, turned into a fleet plan (never started here)
    settings     {dotted.path: value}  anything else in arch_params.SETTINGS
    review       ask | auto | dry   how the run applies edits (default ask)
    why          {part: one sentence}  the author's reason for a part, shown in the README

How a pack runs: when it only wraps an installed flow (no extra gates, tools or flow budgets) it runs that flow by id through
`architect.py run --flow <id> --set ...`, so it keeps every protection the built-in flow has. Otherwise its own flow.yaml is
interpreted by arch_dataflow in a fresh process (`arch_harness.py run`). Either way the overlay lives only in that run's memory.

Nothing here downloads, spends or starts an agent: compose() writes files, check() reads them, run() starts one local run.
"""
import argparse
import copy
import glob
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PACKS_DIR = os.path.join(HERE, "harnesses")
ARCHITECT = os.path.join(HERE, "architect.py")
NAME_RE = re.compile(r"[a-z][a-z0-9_]{1,30}")
FILES = ("harness.yaml", "flow.yaml", "settings.json", "README.md")

SPEC_KEYS = ("name", "title", "blurb", "goal_kind", "tiers", "flow", "gates", "tools", "claude_mode", "budgets", "agents", "settings",
             "review", "why")
FLOW_KEYS = ("title", "kind", "blurb", "steps", "verdict", "context", "produces", "uses_roles", "domains", "budget", "overrides")
REVIEW = ("ask", "auto", "dry")

TIERS = {
    "router": "the small model that reads a request and picks the flow; usually on the processor so the graphics card stays free",
    "worker": "the fast model on the graphics card that writes and fixes the code; most steps run here",
    "heavy": "the larger, slower model a stuck step escalates to; only asked when it can answer in time (limits.escalate_max_s)",
    "draft": "a tiny model with the worker's exact tokenizer that guesses ahead so the worker writes faster (opt-in)",
    "cloud": "a claude: model, used only in the steps the Claude mode allows, within the spend caps",
}

GOAL_KINDS = {
    "fix": ("a bug report to a tested fix", ["bugfix", "council_fix"]),
    "feature": ("a new capability in an existing project", ["feature", "plan_local_build", "scout_then_build"]),
    "build": ("a project or module from a request", ["build", "plan_local_build", "new"]),
    "review": ("findings about existing code, nothing changed", ["review", "fanout_review", "hunt"]),
    "explore": ("an answer about the project, found by looking", ["explore"]),
    "refactor": ("the same behaviour in better shape", ["refactor", "purge", "types"]),
    "secure": ("fewer security problems, proven by scanners and tests", ["harden", "pentest", "deps"]),
    "docs": ("documentation that matches the code", ["document", "design"]),
    "traces": ("verified local runs kept as training data (never Claude's text)", ["explore"]),
    "any": ("no particular kind", []),
}

STEP_PURPOSE = {
    "code": "a code check from arch_gates runs; no model is asked",
    "generate": "a model writes FILE/EDIT/DEF blocks, they are applied, and a gate decides; retries and escalation on failure",
    "ask_json": "a model answers in a JSON schema (arch_schemas) that is validated again in code, stored for later steps",
    "judge": "a model scores the work against named criteria; its verdict is re-checked in code and never alone makes a pass",
    "doc": "a model writes DOC blocks that become files, then a gate checks them",
    "sample_n": "N attempts in scratch copies; the first that passes its gate wins",
    "cascade": "the same prompt up the tiers (worker, then heavy) until the gate passes",
    "chunked": "map a prompt over file chunks, then reduce the notes into one answer",
    "foreach": "repeat sub-steps for each item of an earlier step's output (e.g. each sprint of a plan)",
    "tool_loop": "the model calls tools (look, search, read, test, edit, remember) one at a time until it answers",
    "report": "write a Markdown file from the run's results (undoable)",
    "handoff": "save what the next session needs (.arch/handoff.json)",
    "sub_flow": "run another installed flow as one step",
}

CLAUDE_MODES = {
    "off": "Claude is never asked; every step runs on this machine's models",
    "escalation": "Claude only when the local heavy model is also stuck on a step",
    "chat": "escalation, plus Studio chat answers come from Claude",
    "plan": "Claude does the thinking roles (planner, architect, judge, reviewer); the local models write and fix every line",
    "all": "every coding step uses Claude (the router stays local); costs the most",
}

FLOW_BUDGET = {
    "calls": "most model calls in the whole run",
    "tokens": "most tokens (prompt + reply) in the whole run",
    "seconds": "most wall-clock seconds for the whole run",
    "heavy_calls": "most calls to the heavy tier in the whole run",
}
MONEY = {
    "usd_per_run": ("limits.cloud_usd_per_run", "most dollars of Claude per run; checked before every request"),
    "usd_per_day": ("limits.cloud_usd_per_day", "most dollars of Claude per day across the machine"),
    "fleet_usd": (None, "the total the agents' spend caps may add up to"),
}

# Checks that need nothing from an earlier step, so a pack may add them as steps of their own, and where they go.
STANDALONE = {"tests_green_before": "start", "characterization_pinned": "start", "tests_pass": "end", "determinism": "end"}

# A pack travels between projects and machines: it may not quietly weaken a check that is set to protect the person.
NO_WEAKER = {"behavior.fake_code": ("off",), "behavior.max_shrink_pct": (0,), "behavior.self_protect": ("none",),
             "online.allow_pip": (True,)}
DERIVED = ("online.enabled", "online.cloud_tier", "limits.cloud_usd_per_run", "limits.cloud_usd_per_day")


# ------------------------------------------------------------------------------------------------------ YAML out
_BARE_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")
_YAML_WORDS = {"true", "false", "null", "yes", "no", "on", "off", "y", "n", "~"}


def _key(k):
    k = str(k)
    return k if _BARE_KEY.fullmatch(k) and k.lower() not in _YAML_WORDS else json.dumps(k, ensure_ascii=False)


def _scalar(v):
    if v is None:
        return "null"
    if v is True or v is False:
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        r = repr(v)
        return r if re.fullmatch(r"[-+]?\d*\.\d+", r) else json.dumps(str(v))
    return json.dumps(str(v), ensure_ascii=False)


def _lines(obj, ind):
    pad, out = " " * ind, []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                out.append(f"{pad}{_key(k)}:")
                out += _lines(v, ind + 2)
            elif isinstance(v, (dict, list)):
                out.append(f"{pad}{_key(k)}: {'{}' if isinstance(v, dict) else '[]'}")
            else:
                out.append(f"{pad}{_key(k)}: {_scalar(v)}")
    else:
        for v in obj:
            if isinstance(v, dict) and v:
                sub = _lines(v, ind + 2)
                out.append(f"{pad}- {sub[0][ind + 2:]}")
                out += sub[1:]
            elif isinstance(v, list) and v:
                out.append(f"{pad}-")
                out += _lines(v, ind + 2)
            elif isinstance(v, (dict, list)):
                out.append(f"{pad}- {'{}' if isinstance(v, dict) else '[]'}")
            else:
                out.append(f"{pad}- {_scalar(v)}")
    return out


def dump_yaml(obj):
    """Block-style YAML that the app's own reader (arch_ci.load_yaml, with or without PyYAML) reads back to the same object.
    Every string is double-quoted, so no value can turn into a number, a bool or a comment by accident."""
    return "\n".join(_lines(obj, 0)) + "\n"


def load_yaml(text):
    import arch_ci
    return arch_ci.load_yaml(text)


def _round_trips(obj):
    try:
        return load_yaml(dump_yaml(obj)) == json.loads(json.dumps(obj))
    except Exception:
        return False


# ------------------------------------------------------------------------------------------------------ what exists
def _first_sentence(text, cap=180):
    t = " ".join(str(text or "").split())
    m = re.search(r"(?<=[.!?])\s", t)
    t = t[:m.start()] if m else t
    return t if len(t) <= cap else t[:cap - 3].rstrip() + "..."


def _yaml_flows():
    """{id: path} for every flows/*.yaml whose id can be read."""
    import arch_dataflow
    out = {}
    for p in sorted(glob.glob(os.path.join(arch_dataflow.FLOWS_DIR, "*.yaml"))):
        try:
            doc = arch_dataflow.load_yaml_file(p)
            if isinstance(doc, dict) and doc.get("id"):
                out[str(doc["id"])] = p
        except Exception:
            continue
    return out


def _installed_ids():
    ids = set(_yaml_flows())
    try:
        import arch_flows
        ids |= set(arch_flows.FLOWS)
    except Exception:
        pass
    return ids


def _has_python_class(fid):
    """True when a Python flow class of this id exists: then a YAML copy runs without what the class adds on top of the YAML."""
    try:
        import arch_flows
        f = arch_flows.FLOWS.get(fid)
        return f is not None and not getattr(f, "data", False)
    except Exception:
        return False


def _gate_purpose(name, fn):
    doc = (getattr(fn, "__doc__", None) or "").strip()
    if name.endswith("_scan"):
        return f"counts the {name[:-5]} findings before the change (a code scanner, no model)"
    if name.endswith("_fewer"):
        return f"passes only when the change leaves fewer {name[:-6]} findings than before (a code scanner, no model)"
    return _first_sentence(doc) if doc else f"the code check '{name}' (arch_gates)"


def parts():
    """Every component a pack can name, each with one line on what it is for. -> {category: [{name, purpose, ...}]}"""
    import arch_agentlib
    import arch_dataflow
    import arch_gates
    import arch_params
    import arch_roles
    import arch_tools
    roles = arch_roles.ROLES
    out = {"tiers": [{"name": t, "purpose": p, "roles": sorted(r for r, d in roles.items() if d.get("tier") == t)} for t, p in TIERS.items()]}
    flows = []
    for fid, fl in sorted(arch_dataflow.load_all(say=lambda m: None).items()):
        kinds = [s.get("kind", "generate") for s in arch_dataflow.validate_steps(fl.doc)]
        flows.append({"name": fid, "purpose": _first_sentence(fl.blurb) or fl.title, "title": fl.title, "steps": len(fl.doc.get("steps") or []),
                      "calls_tools": "tool_loop" in kinds})
    out["flows"] = flows
    out["step_kinds"] = [{"name": k, "purpose": STEP_PURPOSE.get(k, "a step kind of the flow interpreter")} for k in sorted(arch_dataflow.STEP_KINDS)]
    out["gates"] = [{"name": g, "purpose": _gate_purpose(g, fn), "standalone": STANDALONE.get(g, "")} for g, fn in sorted(arch_gates.GATES.items())]
    out["tools"] = [{"name": t, "purpose": _first_sentence(d["desc"]), "writes": bool(d["writes"])} for t, d in sorted(arch_tools.TOOLS.items())]
    out["claude_modes"] = [{"name": m, "purpose": p} for m, p in CLAUDE_MODES.items()]
    out["agent_templates"] = [{"name": t["name"], "purpose": t.get("purpose") or t.get("title") or t["name"], "mode": t.get("mode"),
                               "budget_usd": t.get("budget_usd")} for t in arch_agentlib.list_templates() if t.get("kind") == "agent"]
    out["budgets"] = ([{"name": k, "purpose": p, "applies_to": "the flow"} for k, p in FLOW_BUDGET.items()]
                      + [{"name": k, "purpose": p, "applies_to": s or "the agents' plan"} for k, (s, p) in MONEY.items()])
    out["goal_kinds"] = [{"name": k, "purpose": p, "flows": f} for k, (p, f) in GOAL_KINDS.items()]
    out["roles"] = [{"name": r, "purpose": d.get("title") or r, "tier": d.get("tier")} for r, d in sorted(roles.items())]
    out["settings"] = [{"name": row[0], "purpose": row[2], "type": row[1]} for rows in arch_params.SETTINGS.values() for row in rows]
    return out


# ------------------------------------------------------------------------------------------------------ building a pack
def _all_steps(steps):
    for s in steps or []:
        if isinstance(s, dict):
            yield s
            if s.get("kind") == "foreach":
                yield from _all_steps(s.get("steps"))


def _prompt_refs(step):
    """(holder dict, key) for every prompt a step names: its own, and a chunked step's map/reduce prompts."""
    refs = [(step, "prompt")] if step.get("prompt") else []
    for part in ("map", "reduce"):
        if isinstance(step.get(part), dict) and step[part].get("prompt"):
            refs.append((step[part], "prompt"))
    return refs


def _qualify(doc, source_id):
    """A copy runs under the pack's own id, so its prompts are named by their real folder: `fix` -> `bugfix/fix`."""
    for s in _all_steps(doc.get("steps")):
        for holder, k in _prompt_refs(s):
            name = str(holder[k])
            if "/" not in name:
                holder[k] = f"{source_id}/{name}"


def _setting(path, raw):
    """-> (value, problem). Validated by arch_params; a number outside the range is refused, not quietly clamped."""
    import arch_params
    row = arch_params._SETTINGS_BY_PATH.get(path) or arch_params._RUNOPT_BY_PATH.get(path)
    if not row:
        return None, f"settings: '{path}' is not a setting the Architect knows (see parts()['settings'])"
    try:
        val = arch_params.validate_setting(path, raw)
    except arch_params.ValidationError as ex:
        return None, f"settings: {ex}"
    rng = row[4] if len(row) > 4 else None
    if rng and row[1] in ("int", "float"):
        try:
            f = float(raw)
            if f < rng[0] or f > rng[1]:
                return None, f"settings: {path} = {raw!r} is outside {rng[0]}..{rng[1]}"
        except (TypeError, ValueError):
            pass
    if path in NO_WEAKER and val in NO_WEAKER[path]:
        return None, f"settings: a pack may not set {path} = {val!r}; it turns off a check that protects the person's code"
    return val, None


def _num(v, cast):
    if isinstance(v, bool):
        raise ValueError
    x = cast(v)
    if not x > 0:
        raise ValueError
    return x


def build(spec):
    """The whole pack in memory, files not written. -> {spec, flow, settings, fleet, mode, source, notes, problems}"""
    import arch_dataflow
    import arch_gates
    import arch_tools
    P, notes = [], []
    if not isinstance(spec, dict):
        return {"problems": ["the spec must be a mapping"], "notes": []}
    for k in spec:
        if k not in SPEC_KEYS:
            P.append(f"unknown part {k!r} (parts: {', '.join(SPEC_KEYS)})")
    name = str(spec.get("name") or "").strip()
    if not NAME_RE.fullmatch(name):
        P.append(f"name {name!r}: use a-z, 0-9 and _, starting with a letter, 2-31 characters")
    installed = _installed_ids()
    yaml_flows = _yaml_flows()
    if name in installed:
        P.append(f"name {name!r} is an installed flow's id; a pack's flow runs under its own name, so pick another")
    goal_kind = spec.get("goal_kind") or "any"
    if goal_kind not in GOAL_KINDS:
        P.append(f"goal_kind {goal_kind!r}: one of {', '.join(GOAL_KINDS)}")
    review = spec.get("review") or "ask"
    if review not in REVIEW:
        P.append(f"review {review!r}: one of {', '.join(REVIEW)}")
    why = spec.get("why") or {}
    if not isinstance(why, dict):
        P.append("why: a mapping of part -> one sentence")
        why = {}

    # ---- tiers
    raw_t = spec.get("tiers") or {}
    if isinstance(raw_t, list):
        tiers = {str(t): "" for t in raw_t}
    elif isinstance(raw_t, dict):
        tiers = {str(k): str(v or "").strip() for k, v in raw_t.items()}
    else:
        tiers = {}
        P.append("tiers: a mapping {tier: model} or a list of tiers")
    for t, m in tiers.items():
        if t not in TIERS:
            P.append(f"tiers: unknown tier {t!r} (one of {', '.join(TIERS)})")
        elif t == "cloud" and m and not m.startswith("claude:"):
            P.append(f"tiers: the cloud tier takes a claude: model, not {m!r}")
        elif t != "cloud" and m.startswith("claude:"):
            P.append(f"tiers: {t} is a local tier; {m!r} belongs in the cloud tier")
        elif any(c in m for c in "\r\n"):
            P.append(f"tiers: {t} model name has a line break")

    # ---- Claude
    mode = spec.get("claude_mode") or "off"
    if mode not in CLAUDE_MODES:
        P.append(f"claude_mode {mode!r}: one of {', '.join(CLAUDE_MODES)}")
    if tiers.get("cloud") and mode == "off":
        P.append("tiers: a cloud model is named but claude_mode is off; pick a mode or drop the model")
    if goal_kind == "traces" and mode != "off":
        P.append("goal_kind traces needs claude_mode off: Claude's replies are never training data")

    # ---- budgets
    budgets = spec.get("budgets") or {}
    flow_budget, money = {}, {}
    if not isinstance(budgets, dict):
        P.append("budgets: a mapping")
        budgets = {}
    for k, v in budgets.items():
        if k in FLOW_BUDGET:
            try:
                flow_budget[k] = _num(v, int)
                if float(v) != flow_budget[k]:
                    raise ValueError
            except (TypeError, ValueError):
                P.append(f"budgets: {k} must be a whole number above 0, got {v!r}")
        elif k in MONEY:
            try:
                money[k] = _num(v, float)
            except (TypeError, ValueError):
                P.append(f"budgets: {k} must be a dollar amount above 0, got {v!r}")
        else:
            P.append(f"budgets: unknown budget {k!r} (one of {', '.join(list(FLOW_BUDGET) + list(MONEY))})")
    if mode not in ("off",) and mode in CLAUDE_MODES and "usd_per_run" not in money:
        P.append(f"claude_mode {mode} needs budgets.usd_per_run: a run that may ask Claude must have a dollar cap")
    if mode == "off" and (money.get("usd_per_run") or money.get("usd_per_day")):
        P.append("budgets: a Claude spend cap is set but claude_mode is off")

    # ---- gates
    pre, post = [], []
    gates = spec.get("gates") or []
    if not isinstance(gates, list):
        P.append("gates: a list of check names")
        gates = []
    for g in gates:
        g = g.get("gate") if isinstance(g, dict) else g
        if g not in arch_gates.GATES:
            P.append(f"gates: unknown check {g!r} (see parts()['gates'])")
        elif g not in STANDALONE:
            P.append(f"gates: {g!r} reads what an earlier step produced, so it cannot be a step of its own; "
                     f"use a flow that has it (standalone checks: {', '.join(STANDALONE)})")
        elif g in pre or g in post:
            P.append(f"gates: {g!r} is listed twice")
        else:
            (pre if STANDALONE[g] == "start" else post).append(g)
    if goal_kind == "fix" and "tests_green_before" in pre:
        P.append("gates: tests_green_before stops a run whose tests already fail, which is exactly where a fix starts")

    # ---- tools
    tools = spec.get("tools") or []
    if not isinstance(tools, list):
        P.append("tools: a list of tool names")
        tools = []
    for t in tools:
        if t not in arch_tools.TOOLS:
            P.append(f"tools: unknown tool {t!r} (one of {', '.join(sorted(arch_tools.TOOLS))})")

    # ---- flow
    src = spec.get("flow")
    doc, source, run_mode = None, None, "own"
    if isinstance(src, str) and src:
        if src in yaml_flows:
            doc = copy.deepcopy(arch_dataflow.load_yaml_file(yaml_flows[src]))
            source = src
            run_mode = "own" if (pre or post or tools or flow_budget) else "installed"
        elif src in installed:
            P.append(f"flow {src!r} is built in Python only; a pack wraps flows that are data (flows/*.yaml)")
        else:
            P.append(f"flow {src!r} is not installed (see parts()['flows'])")
    elif isinstance(src, dict):
        for k in src:
            if k not in FLOW_KEYS:
                P.append(f"flow: unknown key {k!r} (one of {', '.join(FLOW_KEYS)})")
        if not src.get("steps"):
            P.append("flow: list the steps")
        doc = {"id": name, "title": src.get("title") or spec.get("title") or name}
        for k in FLOW_KEYS:
            if k in src and k != "title":
                doc[k] = copy.deepcopy(src[k])
    else:
        P.append("flow: an installed flow id, or {steps: [...], verdict: {...}}")
    if doc is not None:
        doc["id"] = name
        if spec.get("title"):
            doc["title"] = str(spec["title"])
        if spec.get("blurb"):
            doc["blurb"] = str(spec["blurb"])
        if source:
            doc["based_on"] = source
            _qualify(doc, source)
        for s in _all_steps(doc.get("steps")):
            for holder, k in _prompt_refs(s):
                pth = arch_dataflow.prompt_path(name, str(holder[k]))
                if not os.path.isfile(pth):
                    P.append(f"flow: step {s.get('id')}: prompt {holder[k]!r} not found (name it as <flow>/<file>, e.g. explore/explore)")
        loops = [s for s in _all_steps(doc.get("steps")) if s.get("kind") == "tool_loop"]
        if tools and not loops:
            P.append("tools: no step of this flow calls tools (no tool_loop step), so the list would do nothing")
        for s in loops:
            had = list(s.get("tools") or arch_tools.TOOLS)
            keep = [t for t in had if t in tools] if tools else had
            if tools and not keep:
                P.append(f"tools: step {s.get('id')} would have no tool left (it uses {', '.join(had)})")
            s["tools"] = keep
        steps = list(doc.get("steps") or [])
        doc["steps"] = ([{"id": f"pre_{g}", "kind": "code", "title": f"Before: {g}", "gate": g, "on_fail": "stop"} for g in pre]
                        + steps
                        + [{"id": f"post_{g}", "kind": "code", "title": f"After: {g}", "gate": g, "on_fail": "continue"} for g in post])
        if post:
            v = dict(doc.get("verdict") or {})
            base = str(v.get("pass_if") or "True")
            v["pass_if"] = f"({base}) and " + " and ".join(f"post_{g}.ok" for g in post)
            doc["verdict"] = v
        if flow_budget:
            b = dict(doc.get("budget") or {})
            b.update(flow_budget)
            doc["budget"] = b
            for over in (doc.get("overrides") or {}).values():          # the pack's ceiling holds on every machine class too
                ob = over.get("budget") if isinstance(over, dict) else None
                for k, cap in flow_budget.items():
                    if isinstance(ob, dict) and isinstance(ob.get(k), (int, float)) and ob[k] > cap:
                        ob[k] = cap
        P += [f"flow: {p}" for p in arch_dataflow.validate(doc)]
        if not _round_trips(doc):
            P.append("flow: some text in it cannot be written to YAML and read back the same (a backslash or tab?)")

    # ---- settings overlay
    overlay = {}
    raw_s = spec.get("settings") or {}
    if not isinstance(raw_s, dict):
        P.append("settings: a mapping {dotted.path: value}")
        raw_s = {}
    for k, v in raw_s.items():
        if str(k).startswith("tiers."):
            P.append(f"settings: {k} is set through tiers, not settings")
            continue
        if k in DERIVED:
            P.append(f"settings: {k} follows from claude_mode / budgets; set it there")
            continue
        val, prob = _setting(k, v)
        if prob:
            P.append(prob)
        else:
            overlay[k] = val
    for t, m in tiers.items():
        if m and t in TIERS:
            overlay[f"tiers.{t}"] = m
    if mode == "off":
        overlay["online.enabled"] = False
        overlay["online.cloud_tier"] = "off"
    elif mode in CLAUDE_MODES:
        overlay["online.enabled"] = True
        overlay["online.cloud_tier"] = mode
    for k, (path, _) in MONEY.items():
        if path and k in money:
            val, prob = _setting(path, money[k])
            if prob:
                P.append(prob.replace("settings: ", "budgets: "))
            else:
                overlay[path] = val

    # ---- agents
    fleet = []
    agents = spec.get("agents") or []
    if not isinstance(agents, list):
        P.append("agents: a list of {template, values}")
        agents = []
    if agents:
        import arch_agentlib
        cap = money.get("fleet_usd")
        if not cap:
            P.append("agents need budgets.fleet_usd: the total their spend caps may add up to")
        else:
            try:
                fleet = arch_agentlib.to_fleet_plan(agents, total_cap_usd=cap)
            except (ValueError, OSError) as ex:
                P.append(f"agents: {ex}")
            for a in fleet:
                if (a.get("kind") or "claude") == "claude" and mode == "off":
                    P.append(f"agents: {a['name']} runs on Claude, but claude_mode is off (a local-only pack cannot have Claude agents)")
    elif "fleet_usd" in money:
        P.append("budgets: fleet_usd is set but no agents are named")

    norm = {"name": name, "title": str(spec.get("title") or (doc or {}).get("title") or name), "blurb": str(spec.get("blurb") or ""),
            "goal_kind": goal_kind, "review": review, "tiers": tiers, "flow": copy.deepcopy(src), "gates": pre + post, "tools": list(tools),
            "claude_mode": mode, "budgets": dict(budgets), "agents": copy.deepcopy(agents), "settings": dict(raw_s), "why": dict(why)}
    if source and goal_kind in GOAL_KINDS and GOAL_KINDS[goal_kind][1] and source not in GOAL_KINDS[goal_kind][1]:
        notes.append(f"flow {source} is not one of the usual flows for '{goal_kind}' ({', '.join(GOAL_KINDS[goal_kind][1])})")
    if source and run_mode == "own" and _has_python_class(source):
        notes.append(f"runs its own copy of {source}; protections the Python {source} class adds on top of flows/{source}.yaml do not apply")
    return {"spec": norm, "flow": doc, "settings": overlay, "fleet": fleet, "mode": run_mode, "source": source, "notes": notes, "problems": P}


# ------------------------------------------------------------------------------------------------------ files
def pack_dir(pack):
    """A pack name (harnesses/<name>) or a folder path -> the folder."""
    p = str(pack)
    if NAME_RE.fullmatch(p) and not os.path.isdir(p):
        return os.path.join(PACKS_DIR, p)
    return os.path.abspath(p)


def set_args(overlay):
    """settings overlay -> the --set KEY=VALUE strings architect.py reads back to exactly these values."""
    out = []
    for k, v in overlay.items():
        if isinstance(v, bool):
            s = "true" if v else "false"
        elif isinstance(v, (int, float)):
            s = repr(v)
        elif isinstance(v, (list, dict)):
            s = json.dumps(v)
        else:
            s = str(v)
            try:
                json.loads(s)
                s = json.dumps(s)                # "8" or "null" would be read back as a number / None
            except ValueError:
                if s.lower() in ("true", "false"):
                    s = json.dumps(s)
        out.append(f"{k}={s}")
    return out


def command(pack, goal, root=None, review=None):
    """The command line that runs this pack once. -> argv list."""
    d = pack_dir(pack)
    h = load_yaml(_read(os.path.join(d, "harness.yaml")))
    review = review or h.get("review") or "ask"
    if h.get("mode") == "installed":
        argv = [sys.executable, ARCHITECT, "run", goal, "--flow", h["source"], "--review", review]
        for kv in set_args(json.loads(_read(os.path.join(d, "settings.json")))):
            argv += ["--set", kv]
    else:
        argv = [sys.executable, os.path.abspath(__file__), "run", d, goal, "--review", review]
    if root:
        argv += ["--root", os.path.abspath(root)]
    return argv


def _run_hint(name):
    return f'python arch_harness.py run {name} "your goal" --root PATH'


def readme(built):
    import arch_params
    import arch_roles
    s, doc = built["spec"], built["flow"] or {}
    why = s.get("why") or {}
    L = [f"# {s['title']}", "", s.get("blurb") or doc.get("blurb") or "", "",
         f"Goal kind: **{s['goal_kind']}** ({GOAL_KINDS.get(s['goal_kind'], ('',))[0]}).", "",
         "## Run it", "", "```", _run_hint(s["name"]), f"python arch_harness.py check {s['name']}", "```", "",
         ("It wraps the installed flow `%s` and runs it through `architect.py run --flow %s`, so every protection of that flow applies."
          % (built["source"], built["source"])) if built["mode"] == "installed" else
         "It runs its own `flow.yaml` through the flow interpreter in a fresh process.",
         "The settings below apply to that one run only (`--set`); your settings file is never changed.",
         f"Edits are applied in review mode **{s['review']}**.", "", "## Why each part", ""]
    L += ["### Tiers", ""]
    used = sorted({st.get("tier") or (arch_roles.ROLES.get(st.get("role") or "", {}) or {}).get("tier") or ""
                   for st in _all_steps(doc.get("steps")) if st.get("kind") not in ("code", "report", "handoff")} - {""})
    for t in sorted(set(s["tiers"]) | set(used)):
        m = s["tiers"].get(t) or "the model this machine already uses for it"
        L.append(f"- **{t}** = {m}: {TIERS.get(t, '')}.")
    if why.get("tiers"):
        L += ["", why["tiers"]]
    head = (f"The installed `{built['source']}`; `flow.yaml` is a copy to read, the run uses the installed flow." if built["mode"] == "installed"
            else (f"A copy of `{built['source']}`" if built["source"] else "Its own steps") + f", run as `{s['name']}`.")
    L += ["", "### Flow", "", head, "",
          "| step | kind | role | tier | decided by |", "|---|---|---|---|---|"]
    for st in _all_steps(doc.get("steps")):
        k = st.get("kind", "generate")
        chk = st.get("gate") or (st.get("verify") or {}).get("gate") or ("tests" if (st.get("verify") or {}).get("run_tests") else "")
        L.append(f"| {st.get('id')} | {k} | {st.get('role') or '-'} | {st.get('tier') or '-'} | {chk or '-'} |")
    L += ["", f"Verdict: pass when `{(doc.get('verdict') or {}).get('pass_if', 'True')}`. A check that could not run gives **not sure**, never a pass."]
    if why.get("flow"):
        L += ["", why["flow"]]
    if s["gates"]:
        import arch_gates
        L += ["", "### Extra checks", ""]
        L += [f"- `{g}` ({'first' if STANDALONE[g] == 'start' else 'last'}): {_gate_purpose(g, arch_gates.GATES[g])}" for g in s["gates"]]
        if why.get("gates"):
            L += ["", why["gates"]]
    if s["tools"]:
        L += ["", "### Tools", "", "The tool steps may call only: " + ", ".join(f"`{t}`" for t in s["tools"]) + "."]
        if why.get("tools"):
            L += ["", why["tools"]]
    L += ["", "### Claude", "", f"Mode **{s['claude_mode']}**: {CLAUDE_MODES.get(s['claude_mode'], '')}."]
    if s["claude_mode"] != "off":
        L.append(f"Your code for those steps leaves this machine. Spend cap per run: ${built['settings'].get('limits.cloud_usd_per_run')}"
                 + (f", per day: ${built['settings']['limits.cloud_usd_per_day']}" if "limits.cloud_usd_per_day" in built["settings"] else "")
                 + ". Needs ANTHROPIC_API_KEY. Claude's replies are never training data.")
    else:
        L.append("Online features are off for this run: nothing leaves the machine.")
    if why.get("claude_mode"):
        L += ["", why["claude_mode"]]
    if s["budgets"]:
        L += ["", "### Budgets", ""]
        for k, v in s["budgets"].items():
            L.append(f"- `{k}` = {v}: {FLOW_BUDGET.get(k) or MONEY.get(k, (None, ''))[1]}.")
        if why.get("budgets"):
            L += ["", why["budgets"]]
    L += ["", "### Settings for this run", "", "| setting | value | what it does |", "|---|---|---|"]
    for k, v in built["settings"].items():
        row = arch_params._SETTINGS_BY_PATH.get(k) or arch_params._RUNOPT_BY_PATH.get(k)
        what = (row[2] + (": " + _first_sentence(row[3], 140) if row[3] else "")) if row else ("model for the tier" if k.startswith("tiers.") else "")
        L.append(f"| `{k}` | `{json.dumps(v)}` | {what.replace('|', '/')} |")
    if why.get("settings"):
        L += ["", why["settings"]]
    if built["fleet"]:
        L += ["", "### Agents", "", "`fleet.json` is a checked plan for `arch_fleet.launch`. `run` never starts it; you do, after reading it.", ""]
        for a in built["fleet"]:
            L.append(f"- **{a['name']}** ({a.get('kind') or 'claude'}, {a.get('mode') or 'acceptEdits'}, cap ${a.get('budget_usd', 0)}): owns "
                     + ", ".join(a.get("files") or []))
        if why.get("agents"):
            L += ["", why["agents"]]
    if built["notes"]:
        L += ["", "## Notes", ""] + [f"- {n}" for n in built["notes"]]
    L += ["", "Rebuild the files from harness.yaml with `python arch_harness.py compose --spec harnesses/%s/harness.yaml --force`." % s["name"], ""]
    return "\n".join(L)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _harness_doc(built):
    h = dict(built["spec"])
    h.update(mode=built["mode"], source=built["source"] or "", run=_run_hint(built["spec"]["name"]))
    return h


def compose(spec, folder=None, overwrite=False):
    """spec -> a pack folder (default harnesses/<name>/). Nothing is written when any part is refused.
    -> {ok, path, problems, notes, mode}"""
    built = build(spec)
    if built["problems"]:
        return {"ok": False, "path": None, "problems": built["problems"], "notes": built.get("notes") or [], "mode": built.get("mode")}
    path = os.path.abspath(folder or os.path.join(PACKS_DIR, built["spec"]["name"]))
    if os.path.exists(os.path.join(path, "harness.yaml")) and not overwrite:
        return {"ok": False, "path": path, "problems": [f"{path} already holds a pack (compose with overwrite=True / --force to replace its files)"],
                "notes": built["notes"], "mode": built["mode"]}
    os.makedirs(path, exist_ok=True)
    _write(os.path.join(path, "harness.yaml"), "# Composed by arch_harness.py; edit this file, then compose again.\n" + dump_yaml(_harness_doc(built)))
    _write(os.path.join(path, "flow.yaml"), f"# The flow of pack {built['spec']['name']} (built from harness.yaml).\n" + dump_yaml(built["flow"]))
    _write(os.path.join(path, "settings.json"), json.dumps(built["settings"], indent=2) + "\n")
    if built["fleet"]:
        _write(os.path.join(path, "fleet.json"), json.dumps({"total_cap_usd": built["spec"]["budgets"].get("fleet_usd"), "plan": built["fleet"]}, indent=2) + "\n")
    _write(os.path.join(path, "README.md"), readme(built))
    return {"ok": True, "path": path, "problems": [], "notes": built["notes"], "mode": built["mode"]}


def check(pack):
    """Validate a pack folder as it is on disk. -> {ok, problems, notes, mode}. Problems refuse a run; notes do not."""
    import arch_dataflow
    import arch_fleet
    d = pack_dir(pack)
    P, notes = [], []
    for f in FILES:
        if not os.path.isfile(os.path.join(d, f)):
            P.append(f"{f} is missing in {d}")
    if P:
        return {"ok": False, "problems": P, "notes": notes, "mode": None}
    try:
        h = load_yaml(_read(os.path.join(d, "harness.yaml")))
    except Exception as ex:
        return {"ok": False, "problems": [f"harness.yaml cannot be read: {ex}"], "notes": notes, "mode": None}
    if not isinstance(h, dict):
        return {"ok": False, "problems": ["harness.yaml is not a mapping"], "notes": notes, "mode": None}
    built = build({k: v for k, v in h.items() if k not in ("mode", "source", "run")})
    P += [f"harness.yaml: {p}" for p in built["problems"]]
    notes += built.get("notes") or []
    if h.get("mode") != built.get("mode") or (h.get("source") or None) != built.get("source"):
        P.append(f"harness.yaml says mode {h.get('mode')!r} / source {h.get('source')!r}, but the spec makes it {built.get('mode')!r} / "
                 f"{built.get('source')!r}; compose again")
    try:
        flow = load_yaml(_read(os.path.join(d, "flow.yaml")))
        probs = arch_dataflow.validate(flow)
        P += [f"flow.yaml: {p}" for p in probs]
        if isinstance(flow, dict) and flow.get("id") != h.get("name"):
            P.append(f"flow.yaml: id {flow.get('id')!r} is not the pack's name {h.get('name')!r}")
        if not probs and built.get("flow") is not None and flow != json.loads(json.dumps(built["flow"])):
            notes.append("flow.yaml differs from what harness.yaml builds today (hand-edited, or the installed flow changed); compose again to refresh")
    except Exception as ex:
        P.append(f"flow.yaml cannot be read: {ex}")
    try:
        overlay = json.loads(_read(os.path.join(d, "settings.json")))
        if not isinstance(overlay, dict):
            raise ValueError("not an object")
        for k, v in overlay.items():
            if k.startswith("tiers."):
                if k[6:] not in TIERS or not isinstance(v, str) or not v.strip():
                    P.append(f"settings.json: {k} = {v!r} is not a tier with a model")
                continue
            _, prob = _setting(k, v)
            if prob:
                P.append("settings.json: " + prob.replace("settings: ", ""))
        if overlay != built.get("settings"):
            P.append("settings.json differs from what harness.yaml asks for; compose again (a pack's overlay comes from its spec only)")
    except Exception as ex:
        P.append(f"settings.json cannot be read: {ex}")
    fpath = os.path.join(d, "fleet.json")
    if built.get("fleet") and not os.path.isfile(fpath):
        P.append("fleet.json is missing (the spec names agents)")
    if os.path.isfile(fpath):
        try:
            fj = json.loads(_read(fpath))
            P += [f"fleet.json: {p}" for p in arch_fleet.plan_check(fj.get("plan"), fj.get("total_cap_usd") or 0)]
        except Exception as ex:
            P.append(f"fleet.json cannot be read: {ex}")
    return {"ok": not P, "problems": P, "notes": notes, "mode": built.get("mode")}


def list_packs(folder=None):
    out = []
    base = folder or PACKS_DIR
    for d in sorted(glob.glob(os.path.join(base, "*", "harness.yaml"))):
        try:
            h = load_yaml(_read(d))
            out.append({"name": h.get("name"), "title": h.get("title"), "goal_kind": h.get("goal_kind"), "mode": h.get("mode"),
                        "claude_mode": h.get("claude_mode"), "path": os.path.dirname(d)})
        except Exception as ex:
            out.append({"name": os.path.basename(os.path.dirname(d)), "error": str(ex), "path": os.path.dirname(d)})
    return out


# ------------------------------------------------------------------------------------------------------ running
def run(pack, goal, root=None, review=None, dry=False, runner=None):
    """Check the pack, then run it once with its overlay (--set, this run only). dry = only return the command.
    -> {ok, argv, rc?, problems?}"""
    c = check(pack)
    if not c["ok"]:
        return {"ok": False, "problems": c["problems"], "argv": None}
    argv = command(pack, goal, root=root, review=review)
    if dry:
        return {"ok": True, "argv": argv, "dry": True}
    rc = (runner or (lambda a: subprocess.run(a, cwd=HERE, env=dict(os.environ)).returncode))(argv)   # env inherited: heavy-lock + data dir carry over
    return {"ok": rc == 0, "rc": rc, "argv": argv}


def _run_inline(pack, goal, root=None, review=None):
    """A pack with its own flow: register its flow for this process only and run it through the normal run_flow path."""
    import arch_dataflow
    import arch_flows
    import architect
    d = pack_dir(pack)
    c = check(d)
    if not c["ok"]:
        print("This pack does not pass its checks:\n  - " + "\n  - ".join(c["problems"]))
        return 2
    h = load_yaml(_read(os.path.join(d, "harness.yaml")))
    fpath = os.path.join(d, "flow.yaml")
    doc = arch_dataflow.load_yaml_file(fpath)
    overlay = json.loads(_read(os.path.join(d, "settings.json")))
    args = argparse.Namespace(root=root, app=None, review=review or h.get("review") or "ask", model=None, role_model=None,
                              max_revisions=None, best_of=None, set=set_args(overlay))
    print(f"[harness] {h.get('name')}: flow {doc['id']}" + (f" (a copy of {h.get('source')})" if h.get("source") else "")
          + f", {len(overlay)} settings for this run only")
    e = architect.build_engine(args, goal, doc["id"])
    arch_flows.FLOWS[doc["id"]] = arch_dataflow.DataFlow(doc, fpath)
    out = arch_flows.run_flow(e, doc["id"], {})
    print(f"\n[done] {(out.get('verdict') or ('pass' if out.get('ok') else 'fail')).upper()}   report: {os.path.join(e.run_dir, 'report.md')}")
    return 0 if out.get("ok") else 1


# ------------------------------------------------------------------------------------------------------ the examples
def examples():
    """The three packs shipped in harnesses/ (compose --examples rebuilds them)."""
    return [
        {"name": "local_fixer", "title": "Local-only fixer", "goal_kind": "fix", "review": "ask",
         "blurb": "A bug report becomes a failing reproduction test, then a minimal fix, on this machine's models only. Nothing leaves the machine.",
         "tiers": {"worker": "", "heavy": ""}, "flow": "bugfix", "claude_mode": "off",
         "settings": {"behavior.escalate": "stalled", "behavior.keep_best_try": True, "behavior.fake_code": "fail", "behavior.network": "offline",
                      "limits.max_revisions": 3, "limits.max_calls": 40},
         "why": {"flow": "The installed bugfix flow is used as is (not a copy), so its read-only-tests rule and the reproduction gate keep working.",
                 "tiers": "The worker writes the fix; the heavy tier is asked only when a step is stuck and it can answer in time.",
                 "claude_mode": "Local-only by design: online features are switched off for this run, whatever your settings say.",
                 "settings": "Fake code fails the step, a worse try is undone, and the tests run without network."}},
        {"name": "claude_plans_feature_team", "title": "Claude plans, local builds: feature team", "goal_kind": "feature", "review": "ask",
         "blurb": "Claude writes the plan with an acceptance contract per sprint and judges the result; the local worker writes every line; "
                  "the whole suite must pass at the end; a read-only Claude reviewer can be launched afterwards from fleet.json.",
         "tiers": {"worker": "", "heavy": "", "cloud": ""}, "flow": "plan_local_build", "claude_mode": "plan", "gates": ["tests_pass"],
         "budgets": {"calls": 40, "tokens": 240000, "seconds": 3600, "heavy_calls": 4, "usd_per_run": 1.5, "usd_per_day": 5, "fleet_usd": 2},
         "agents": [{"template": "reviewer", "name": "feature_review",
                     "values": {"topic": "feature", "targets": "the files the last run changed (its report.md lists them)",
                                "goal": "the feature asked for in the last run", "focus": "acceptance criteria not met, stubs, untested paths"}}],
         "settings": {"behavior.tests_first": "on", "behavior.evaluator": "final", "behavior.keep_best_try": True, "behavior.network": "offline"},
         "why": {"claude_mode": "plan = Claude only thinks (plans and judges); code is written locally, so most tokens stay on this machine.",
                 "gates": "tests_pass at the end: a judge's score never makes a pass on its own; the suite must be green.",
                 "budgets": "Tighter than the flow's own ceiling, and a hard dollar cap per run and per day.",
                 "agents": "One read-only reviewer, capped at $1.50, owning only its notes file; you start it after the run if you want it."}},
        {"name": "overnight_trace_factory", "title": "Overnight trace factory", "goal_kind": "traces", "review": "auto",
         "blurb": "Unattended, read-only runs that look through a project with tools and answer a question; every run is a tool trajectory "
                  "judged by code, kept as training data. The project's tests must still pass at the end.",
         "tiers": {"worker": ""}, "claude_mode": "off", "gates": ["tests_pass"],
         "tools": ["ls", "grep", "search", "read", "outline", "run_tests", "memory_read", "memory_append"],
         "flow": {"title": "Overnight trace factory", "kind": "tool_loop", "produces": ["an answer", "a tool trajectory in the trace"],
                  "uses_roles": ["architect"],
                  "steps": [
                      {"id": "look", "kind": "tool_loop", "title": "Look around and answer", "role": "architect", "tier": "worker",
                       "prompt": "explore/explore", "tools": ["ls", "grep", "search", "read", "outline", "run_tests", "memory_read"],
                       "allow_writes": False, "max_steps": 8, "store_text": "answer", "budget": {"tokens": 100000, "seconds": 780}},
                      {"id": "remember", "kind": "tool_loop", "title": "Remember one thing worth keeping", "role": "architect", "tier": "worker",
                       "prompt": "explore/remember", "tools": ["memory_append"], "max_steps": 2, "when": "look.ok", "on_fail": "continue",
                       "budget": {"tokens": 20000, "seconds": 180}}],
                  "verdict": {"pass_if": "look.ok"}},
         "budgets": {"calls": 14, "tokens": 120000, "seconds": 900},
         "settings": {"behavior.seed": 1234, "behavior.network": "offline", "behavior.learn_from_runs": "off", "limits.max_calls": 20},
         "why": {"claude_mode": "Claude's replies are never training data, so the factory is local-only (a code rule for goal_kind traces).",
                 "tools": "Read-only tools plus one memory line: a trajectory that edits nothing can run unattended with review auto.",
                 "settings": "A fixed seed makes runs repeatable; past replies are not shown as examples, so each trace stands on its own.",
                 "gates": "tests_pass last: a run that left the project broken is not kept as a pass."}},
    ]


# ------------------------------------------------------------------------------------------------------ CLI
def _load_spec(path):
    text = _read(path)
    spec = json.loads(text) if path.lower().endswith(".json") else load_yaml(text)
    if isinstance(spec, dict):
        spec = {k: v for k, v in spec.items() if k not in ("mode", "source", "run")}     # what compose added; anything else is judged
    return spec


def main(argv=None):
    ap = argparse.ArgumentParser(prog="arch_harness.py", description="Compose, check and run harness packs (harnesses/<name>/).")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("parts", help="every component a pack can name, with its purpose")
    p.add_argument("--json", action="store_true")
    sub.add_parser("list", help="the packs in harnesses/")
    c = sub.add_parser("compose", help="spec -> pack folder")
    c.add_argument("--spec", help="a .json or .yaml spec (a pack's harness.yaml works)")
    c.add_argument("--examples", action="store_true", help="rebuild the shipped example packs")
    c.add_argument("--force", action="store_true", help="replace the files of an existing pack")
    k = sub.add_parser("check", help="validate a pack")
    k.add_argument("pack")
    r = sub.add_parser("run", help="run a pack once (its settings apply to this run only)")
    r.add_argument("pack")
    r.add_argument("goal")
    r.add_argument("--root")
    r.add_argument("--review", choices=REVIEW)
    r.add_argument("--dry", action="store_true", help="print the command, run nothing")
    a = ap.parse_args(argv)
    if a.cmd == "parts":
        cat = parts()
        if a.json:
            print(json.dumps(cat, indent=2))
        else:
            for sec, items in cat.items():
                print(f"\n{sec} ({len(items)})")
                for it in items:
                    print(f"  {it['name']:<28} {it['purpose']}")
        return 0
    if a.cmd == "list":
        for pk in list_packs():
            print(f"  {pk.get('name', '?'):<30} {pk.get('mode') or '':<10} claude={pk.get('claude_mode') or '-':<10} {pk.get('title') or pk.get('error')}")
        return 0
    if a.cmd == "compose":
        specs = examples() if a.examples else ([_load_spec(a.spec)] if a.spec else [])
        if not specs:
            print("give --spec FILE or --examples")
            return 2
        rc = 0
        for sp in specs:
            out = compose(sp, overwrite=a.force)
            print(f"[{'ok' if out['ok'] else 'refused'}] {sp.get('name') if isinstance(sp, dict) else '?'} -> {out.get('path') or '-'}")
            for pr in out["problems"]:
                print(f"    - {pr}")
            for n in out.get("notes") or []:
                print(f"    note: {n}")
            rc = rc or (0 if out["ok"] else 1)
        return rc
    if a.cmd == "check":
        out = check(a.pack)
        print(("OK " if out["ok"] else "REFUSED ") + pack_dir(a.pack) + (f"  (runs as: {out['mode']})" if out.get("mode") else ""))
        for pr in out["problems"]:
            print(f"  - {pr}")
        for n in out["notes"]:
            print(f"  note: {n}")
        return 0 if out["ok"] else 1
    if a.cmd == "run":
        c = check(a.pack)
        if not c["ok"]:
            print("This pack does not pass its checks:\n  - " + "\n  - ".join(c["problems"]))
            return 2
        argv2 = command(a.pack, a.goal, root=a.root, review=a.review)
        if a.dry:
            print(subprocess.list2cmdline(argv2))
            return 0
        if c["mode"] == "own":
            return _run_inline(a.pack, a.goal, root=a.root, review=a.review)
        return subprocess.run(argv2, cwd=HERE, env=dict(os.environ)).returncode
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
