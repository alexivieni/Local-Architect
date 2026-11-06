"""Playbooks mined from how Claude lead/harness agents orchestrated work, as logic the LOCAL team reuses.

mine() reads arch_orchestration.snapshot() (Claude workflows -> phases -> agents, and local crew runs) and extracts:
  * SHAPES    workflows that completed without a failed agent, as a signature like "audit x3 > build > verify x2"
  * BRIEFS    the section structure of the briefs those agents were given (goal, owned files, rules, schema, report)
  * CHECKS    what the verifying agents were told to check
  * LESSONS   agents that failed, with the reason
and writes them as local files, each tagged `source: claude-orchestration`:
  playbooks/<shape>-<hash>.md        the pattern in plain words + when to use it
  workflows/learned_<shape>.yaml     the shape on local tiers only (auditor/builder/verifier/...), validated before writing
  agents/learned-brief-<hash>.md     a read-only agent whose body is the brief structure
Deterministic and re-runnable: every file name carries a hash of its content, nothing is rewritten when it already exists.

Claude-written text is retrieval/playbook material ONLY. It is never training data: arch_dataset reads `.arch/runs/*/trace.jsonl`
and nothing under playbooks/, workflows/ or agents/ (arch_selftest_playbook asserts that).

    python arch_playbook.py [--dry-run]
"""
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = "claude-orchestration"
MAX_SHAPES = 8
BAD = ("fail", "failed", "refused", "budget", "error", "schema", "not_sure")
KINDS = (("verify", r"verif|adversar|prove|recompute|re-?run|check|validate"), ("audit", r"audit|review|survey|explore|read|inspect|scan"),
         ("plan", r"plan|design|spec|interface"), ("build", r"build|fix|implement|write|port|code|edit|refactor|migrat|add"))
SECTIONS = (("goal", r"\bgoal\b|\bobjective\b|\btask\b"), ("owned files", r"\bown(s|ed)?\b|files you|only (edit|change|touch)"),
            ("rules", r"\brules?\b|\bdo not\b|\bnever\b|\bmust\b"), ("output schema", r"\bschema\b|\bjson\b|return (a|an|the)|respond with"),
            ("report format", r"\breport\b|\bsummar|final (answer|message)"))
TOOLS_FOR = {"audit": "auditor", "plan": "planner", "build": "builder", "verify": "verifier"}


def _walk(n, kind, out):
    if isinstance(n, dict):
        if n.get("kind") == kind:
            out.append(n)
        for c in n.get("children") or []:
            _walk(c, kind, out)
    elif isinstance(n, list):
        for c in n:
            _walk(c, kind, out)


def workflows_of(snap):
    """Every workflow node (Claude sessions and local crew runs) in a snapshot."""
    out = []
    _walk((snap.get("claude") or {}).get("sessions") or [], "workflow", out)
    for w in (snap.get("crew") or {}).get("workflows") or []:
        _walk(w, "workflow", out)
    return out


def kind_of(text):
    low = str(text or "").lower()
    for k, rx in KINDS:
        if re.search(rx, low):
            return k
    return "build"


def shape_of(wf):
    """-> (signature, steps) with steps [(kind, n_agents)], or (None, []) when the workflow is not worth keeping: no agents, or any
    agent failed."""
    steps, bad = [], False
    for ph in wf.get("children") or []:
        ags = []
        _walk(ph, "agent", ags) if ph.get("kind") != "agent" else ags.append(ph)
        if not ags:
            continue
        bad = bad or any(str(a.get("verdict") or "").lower() in BAD for a in ags)
        kind = kind_of(str(ph.get("label") or "") + " " + " ".join(str(a.get("label") or "") for a in ags[:2]))
        if steps and steps[-1][0] == kind and len(ags) == 1:
            continue
        steps.append((kind, min(len(ags), 3)))   # 1, 2 or 3 = "3 or more": exact fan-out is noise
    if bad or len(steps) < 2 or len(steps) > 6:
        return None, []
    return " > ".join(k + ("" if n == 1 else f" x{n}" + ("+" if n == 3 else "")) for k, n in steps), steps


def brief_sections(prompt):
    """The ordered section names a brief covers (by the words it uses)."""
    first = {}
    low = str(prompt or "").lower()
    for name, rx in SECTIONS:
        m = re.search(rx, low)
        if m:
            first[name] = m.start()
    return [k for k, _ in sorted(first.items(), key=lambda kv: kv[1])]


def check_lines(wf):
    """Sentences from verifier-ish agent prompts that say what to check (first 4, trimmed, no paths from the machine)."""
    out = []
    ags = []
    _walk(wf, "agent", ags)
    for a in ags:
        if kind_of(a.get("label")) != "verify":
            continue
        for s in re.split(r"(?<=[.!?])\s+|\n+", str(a.get("prompt") or "")):
            s = s.strip()
            if 15 < len(s) < 220 and re.search(r"verif|check|re-?run|recompute|prove|confirm|compare", s, re.I) and s not in out:
                out.append(re.sub(r"[A-Za-z]:[\\/][^\s]+", "<path>", s))
    return out[:4]


def lessons(wfs):
    out = []
    for wf in wfs:
        ags = []
        _walk(wf, "agent", ags)
        for a in ags:
            v = str(a.get("verdict") or "").lower()
            if v in BAD and a.get("why"):
                s = re.sub(r"\s+", " ", str(a["why"]))[:160]
                if s not in out:
                    out.append(s)
    return sorted(out)[:10]


def _h(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _slug(sig):
    return re.sub(r"[^a-z0-9]+", "_", sig.lower()).strip("_")


def workflow_yaml(sig, steps):
    """A local-tier workflow for a shape (validated by the caller). build phases own {owns}; verify x2+ is a majority panel."""
    L = ["# source: " + SOURCE + " (mined by arch_playbook; local tiers only)",
         f"name: learned-{_slug(sig).replace('_', '-')}", f"description: \"Mined shape: {sig}\"", "phases:"]
    for i, (k, n) in enumerate(steps, 1):
        agent = TOOLS_FOR.get(k, "auditor")
        L.append(f"  - title: \"{k.capitalize()} {i}\"")
        if k == "verify" and n > 1:
            L += [f"    panel: verifier", f"    n: {n}", "    vote: verdict", f"    label: verify{i}"]
        elif n > 1:
            L.append("    parallel:")
            for j in range(n):
                L += [f"      - agent: {agent}", f"        label: \"{k}{i}:{j + 1}\""]
        else:
            L += [f"    agent: {agent}", f"    label: {k}{i}"]
            if k == "build":
                L.append("    owns: \"{owns}\"")
    return "\n".join(L) + "\n"


def playbook_md(sig, steps, count, briefs, checks, lessons_):
    L = ["---", "source: " + SOURCE, "kind: playbook", f"shape: {sig}", f"seen: {count}", "training: never", "---", f"# Pattern: {sig}", "",
         f"Seen in {count} Claude orchestration run(s) that finished without a failed agent.", "", "## When to use", ""]
    why = {"audit": "several readers look before anything is changed", "plan": "the work is split into owned files first",
           "build": "one agent changes only the files it owns", "verify": "something tries to prove the change wrong"}
    for k, n in steps:
        L.append(f"- {k}{f' x{n}' if n > 1 else ''}: {why.get(k, k)}")
    L += ["", "Use it for work that changes code and must be checked: the more files it touches, the more the first step earns.", ""]
    if briefs:
        L += ["## Brief structure that worked", "", "A good brief names, in this order: " + ", ".join(briefs) + ".", ""]
    if checks:
        L += ["## What the verifiers were told to check", ""] + [f"- {c}" for c in checks] + [""]
    if lessons_:
        L += ["## Lessons (failures seen in similar runs)", ""] + [f"- {x}" for x in lessons_] + [""]
    L += ["## Run it locally", "", f"`python architect.py crew workflows/learned_{_slug(sig)}.yaml --goal \"...\" --owns <files>`", ""]
    return "\n".join(L)


def brief_agent_md(sections):
    body = ("<!-- source: " + SOURCE + " -->\nYou follow a brief with this structure: " + ", ".join(sections) + ".\n"
            "Read the real files first. Respect the files you own, obey every rule, and finish with the report asked for. "
            "Never edit anything.\n")
    return ("---\nname: learned-brief-{h}\ndescription: \"Brief structure mined from Claude orchestration (" + ", ".join(sections) +
            "). Read-only reviewer.\"\nmodel: worker\ntools: ls, grep, search, read, outline, view\neffort: medium\nrole: reviewer\n"
            "category: harness\nwrites: false\nsource: " + SOURCE + "\ntask: |\n  Goal: {goal}\n  {prev}\n  {task}\n---\n" + body)


def _write(path, text, dry):
    """Write once. -> 'new' | 'same' (an identical file is already there)."""
    if os.path.isfile(path) and open(path, encoding="utf-8").read() == text:
        return "same"
    if not dry:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    return "new"


def mine(snap=None, root=None, dry_run=False, **snapshot_kw):
    """Mine a snapshot (default: arch_orchestration.snapshot()) -> {shapes, written: [path], same: [path], skipped: [why]}."""
    root = root or HERE
    if snap is None:
        import arch_orchestration
        snap = arch_orchestration.snapshot(**snapshot_kw)
    import arch_squad
    groups, allw = {}, workflows_of(snap)
    for wf in allw:
        sig, steps = shape_of(wf)
        if sig:
            g = groups.setdefault(sig, {"steps": steps, "wfs": []})
            g["wfs"].append(wf)
    res = {"shapes": sorted(groups), "written": [], "same": [], "skipped": [], "workflows_seen": len(allw)}
    les = lessons(allw)
    brief_seen = set()
    keep = sorted(groups, key=lambda k: (-len(groups[k]["wfs"]), k))[:MAX_SHAPES]     # the most-seen shapes only
    res["shapes"] = sorted(keep)
    for sig in sorted(keep):
        g = groups[sig]
        secs = []
        for wf in g["wfs"]:
            ags = []
            _walk(wf, "agent", ags)
            for a in ags:
                s = brief_sections(a.get("prompt"))
                if len(s) >= 3 and tuple(s) not in brief_seen and len(brief_seen) < 3:
                    brief_seen.add(tuple(s))
                    secs.append(s)
        checks = []
        for wf in g["wfs"]:
            checks += [c for c in check_lines(wf) if c not in checks]
        md = playbook_md(sig, g["steps"], len(g["wfs"]), secs[0] if secs else [], checks[:4], les)
        wy = workflow_yaml(sig, g["steps"])
        try:
            import arch_ci
            probs = arch_squad.validate_workflow(arch_ci.load_yaml(wy))
        except Exception as ex:
            probs = [str(ex)]
        if probs:
            res["skipped"].append(f"{sig}: {'; '.join(probs)[:200]}")
            continue
        outs = [(os.path.join(root, "playbooks", f"{_slug(sig)}-{_h(md)}.md"), md),
                (os.path.join(root, "workflows", f"learned_{_slug(sig)}.yaml"), wy)]
        for s in secs:
            t = brief_agent_md(s)
            h = _h(t)
            outs.append((os.path.join(root, "agents", f"learned-brief-{h}.md"), t.replace("{h}", h)))
        for path, text in outs:
            res["same" if _write(path, text, dry_run) == "same" else "written"].append(path)
    return res


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    r = mine(dry_run="--dry-run" in argv)
    print(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
