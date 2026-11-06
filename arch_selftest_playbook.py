"""Self-test for arch_playbook: fixture orchestration snapshots only (no Claude, no model, no network). Writes into a temp root."""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
N = [0, 0]


def ok(cond, what, detail=None):
    N[0 if cond else 1] += 1
    print(("[ok] " if cond else "[x] ") + what + ("" if cond or detail is None else f"  -- {str(detail)[:300]}"))


def ag(label, prompt="", verdict="ok", why=None):
    return {"kind": "agent", "label": label, "prompt": prompt, "verdict": verdict, "why": why, "children": []}


BRIEF = ("Goal: fix the parser. You own only parser.py (any other write is refused). Rules: do not touch tests; never delete a function. "
         "Return JSON matching this schema. Then report what you changed in a short summary.")


def fixture():
    good = {"kind": "workflow", "label": "audit-build-verify", "children": [
        {"kind": "phase", "label": "Audit", "children": [ag("audit:a", BRIEF), ag("audit:b"), ag("audit:c")]},
        {"kind": "phase", "label": "Build", "children": [ag("build", BRIEF)]},
        {"kind": "phase", "label": "Verify", "children": [
            ag("verify:1", "Recompute the totals from the raw file. Check every changed line. Confirm the tests pass on C:\\Users\\bob\\x\\y.py."),
            ag("verify:2")]}]}
    bad = {"kind": "workflow", "label": "broken", "children": [
        {"kind": "phase", "label": "Build", "children": [ag("build", verdict="failed", why="no final answer within the step limit")]}]}
    return {"claude": {"sessions": [{"kind": "session", "children": [good, dict(good), bad]}]}, "crew": {"workflows": []}}


def run():
    N[0] = N[1] = 0
    tmp = tempfile.mkdtemp(prefix="playbook_st_")
    try:
        import arch_playbook as pb
        import arch_squad as sq
        snap = fixture()
        wfs = pb.workflows_of(snap)
        ok(len(wfs) == 3, "workflows_of finds Claude workflows in a snapshot", len(wfs))
        sig, steps = pb.shape_of(wfs[0])
        ok(sig == "audit x3+ > build > verify x2", "shape: audit x3 > build > verify x2", sig)
        ok(pb.shape_of(wfs[2])[0] is None, "a workflow with a failed agent is not kept as a pattern")
        ok(pb.brief_sections(BRIEF) == ["goal", "owned files", "rules", "output schema", "report format"], "brief sections in order",
           pb.brief_sections(BRIEF))
        cl = pb.check_lines(wfs[0])
        ok(cl and not any("bob" in c for c in cl), "verifier checks extracted, machine paths scrubbed", cl)
        ok(pb.lessons(wfs) == ["no final answer within the step limit"], "lessons come from failed agents")

        r = pb.mine(snap, root=tmp)
        ok(r["shapes"] == ["audit x3+ > build > verify x2"] and len(r["written"]) >= 3 and not r["skipped"], "mine writes playbook, workflow and brief",
           r)
        files = [os.path.relpath(p, tmp).replace("\\", "/") for p in r["written"]]
        ok(any(f.startswith("playbooks/") for f in files) and any(f.startswith("workflows/learned_") for f in files)
           and any(f.startswith("agents/learned-brief-") for f in files), "files land in playbooks/, workflows/, agents/", files)
        for p in r["written"]:
            ok(pb.SOURCE in open(p, encoding="utf-8").read(), "tagged source: claude-orchestration: " + os.path.basename(p))
        wy = [p for p in r["written"] if "learned_" in p][0]
        w = sq.load_workflow(wy)
        ok(not sq.validate_workflow(w) and [p["title"] for p in w["phases"]] == ["Audit 1", "Build 2", "Verify 3"], "learned workflow validates", w)
        ag_ = [p for p in r["written"] if "learned-brief-" in p][0]
        d = sq.load_agent(ag_)
        ok(not d["writes"] and not d["model"].startswith("claude:"), "learned brief agent is local and read-only")
        agents_used = set()
        for ph in w["phases"]:
            for en in (ph.get("parallel") or [ph]):
                a = en.get("agent") or en.get("panel")
                if a:
                    agents_used.add(a)
        ok(agents_used and all(sq.load_agent(a)["model"] in sq.TIERS for a in agents_used), "learned workflow uses local tiers only", agents_used)

        r2 = pb.mine(snap, root=tmp)
        ok(not r2["written"] and len(r2["same"]) == len(r["written"]), "re-running changes nothing (dedupe by content hash)", r2)
        before = sorted(os.listdir(os.path.join(tmp, "playbooks")))
        pb.mine(snap, root=tmp)
        ok(sorted(os.listdir(os.path.join(tmp, "playbooks"))) == before, "deterministic file names")
        r3 = pb.mine(snap, root=os.path.join(tmp, "dry"), dry_run=True)
        ok(r3["written"] and not os.path.exists(os.path.join(tmp, "dry")), "dry run writes nothing")
        ok(pb.mine({"claude": {}, "crew": {}}, root=tmp)["shapes"] == [], "an empty snapshot mines nothing")

        # Claude-written text is never training data: the dataset reader only reads .arch/runs/*/trace.jsonl
        src = open(os.path.join(HERE, "arch_dataset.py"), encoding="utf-8").read()
        ok("playbooks" not in src and '".arch", "runs"' in src, "arch_dataset reads only .arch/runs (never playbooks/, workflows/, agents/)")
        ok(os.path.isdir(os.path.join(HERE, "playbooks")), "playbooks/ exists in the install")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{N[0]} passed, {N[1]} failed")
    return N[1] == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
