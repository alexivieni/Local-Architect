"""The `delegate` tool: a local agent hands a piece of its work to a squad workflow and gets the digest back (the local equivalent of
Claude Code's Task tool). The tool itself is registered in arch_tools; this module does the work.

    delegate {workflow, goal, owns: [files]}

Rules, all checked in code before anything starts:
  * depth limit 1: a delegated workflow's agents cannot delegate again;
  * a read-only role cannot delegate (the tool is a writing tool, so it is not in a read-only agent's list, and it is refused here too);
  * `owns` must lie inside the files the CALLER owns, a workflow that names a literal `owns:` outside them is refused, and the child's
    engines can only write inside the intersection, so delegating never widens what the caller may touch;
  * the child runs under the caller's remaining budget (at most `SHARE` of it, so the caller can still finish) and what it spends is
    charged back to the caller's crew;
  * plan mode (arch_permissions) refuses a delegation that would write; ask mode still gates every write the children make, because
    they share the run's settings.

It needs the engine to know its crew: `Crew._engine` sets `e.crew`, `e.agent_def` and `e.delegate_depth` (patch in the report).
"""
import json
import os

SHARE = 0.75               # of the caller's remaining budget a delegated workflow may use
MAX_DEPTH = 1


def _key(p):
    import arch_fleet
    return arch_fleet._clean_path(p)[1]


def _inside(rel, owned):
    k = _key(rel)
    return bool(k) and any(o and (k == o or k.startswith(o + "/")) for o in (_key(x) for x in owned))


def _literal_owns(w):
    """Every `owns:` a workflow spells out (not a {template}), anywhere in its phases."""
    out = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k == "owns":
                    items = v if isinstance(v, list) else [s for s in str(v or "").replace("\n", ",").split(",")]
                    out.extend(s.strip() for s in items if str(s).strip() and "{" not in str(s))
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(w.get("phases") or [])
    return out


def _child_budget(crew):
    left = crew.budget.remaining()
    if left is None:
        return None, ""
    if any(v <= 0 for v in left.values()):
        return None, "the budget is spent"
    out = {}
    for k, v in left.items():
        out[k] = max(1, int(v * SHARE)) if k != "seconds" else max(1.0, round(v * SHARE, 1))
    return out, ""


def refusal(e, workflow, owns):
    """-> '' when this delegation may run, else the plain reason it may not."""
    crew = getattr(e, "crew", None)
    if crew is None:
        return "delegate works only inside a crew run (this engine has no crew)"
    if int(getattr(e, "delegate_depth", 0) or 0) >= MAX_DEPTH:
        return "a delegated workflow cannot delegate again (depth limit 1). Do this part yourself."
    d = getattr(e, "agent_def", None) or {}
    if not d.get("writes"):
        return f"the {d.get('name') or 'current'} role is read-only and cannot delegate work"
    mine = list(getattr(e, "owns", ()) or ())
    bad = [o for o in owns if not _inside(o, mine)]
    if bad:
        return f"not delegated: you do not own {', '.join(bad)} (you own: {', '.join(mine) or 'nothing'})"
    try:
        import arch_permissions
        if owns and arch_permissions.mode(getattr(e, "cfg", None), e) == "plan":
            return "this run is in plan mode (read-only): delegate with owns=[] to look, not to write"
    except Exception:
        pass
    return ""


def run(e, workflow="", goal="", owns=None, **_):
    import arch_squad as sq
    if isinstance(owns, str):
        owns = [x.strip() for x in owns.replace("\n", ",").split(",") if x.strip()]
    owns = [str(x) for x in (owns or []) if str(x).strip()]
    why = refusal(e, workflow, owns)
    if why:
        return why
    crew = e.crew
    mine = list(getattr(e, "owns", ()) or ())
    try:
        w = sq.load_workflow(str(workflow))
    except (OSError, ValueError, KeyError) as ex:
        return f"no workflow '{workflow}' ({str(ex)[:160]}). Known: {', '.join(sorted(sq.list_workflows())[:12])}"
    stray = [o for o in _literal_owns(w) if not _inside(o, owns)]
    if stray:
        return f"not delegated: workflow '{workflow}' writes {', '.join(sorted(set(stray)))}, outside what you passed in owns"
    budget, why = _child_budget(crew)
    if why:
        return "not delegated: " + why
    allowed = owns
    depth = int(getattr(e, "delegate_depth", 0) or 0) + 1
    parent_factory = crew.engine_factory

    def factory(d, agent_owns):
        mine_child = [o for o in agent_owns if _inside(o, allowed)]          # never wider than caller's own, narrowed to `owns`
        if parent_factory:
            ce = parent_factory(d, mine_child)
        else:
            ce = sq._owned_engine_class()(crew.root, "crew", goal, crew.cfg, review="auto", say=sq._quiet, persist=False)
        ce.owns = tuple(mine_child)
        ce.delegate_depth = depth
        return ce
    verdict, child = sq.run_workflow(w, root=crew.root, goal=str(goal), vars={"owns": ",".join(owns)}, cfg=crew.cfg, data_dir=crew.data_dir,
                                     slots=1, engine_factory=factory, budget=budget, say=crew.say, use_cache=False, replay=False)
    sp = child.budget.spent
    crew.budget.charge(sp.get("calls", 0), sp.get("tokens", 0))
    crew._event({"type": "delegated", "workflow": workflow, "child_run": child.run_id, "verdict": verdict, "owns": owns,
                 "calls": sp.get("calls", 0), "tokens": sp.get("tokens", 0)})
    edited = sorted({f for r in child.records for f in (r.get("edited") or [])})
    result = sq.digest(getattr(child, "final", None), 1200)
    body = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    return (f"delegated '{workflow}': {verdict}; {len(child.records)} agent(s), {sp.get('calls', 0)} call(s), {sp.get('tokens', 0)} token(s)\n"
            f"files changed: {', '.join(edited) or 'none'}\nresult: {body}")[:3500]
