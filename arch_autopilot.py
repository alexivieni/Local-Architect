"""Autopilot: a backlog the team works through on its own, and the ability to propose its own next items.

The backlog is a plain file in the project (`.arch/backlog.json`): items with a goal, a flow (or auto), a status and the result of the run
that did them. `autopilot` drains it in order, unattended (review = auto by default, every gate still on), inside a budget of wall-clock
time and items, and writes `docs/autopilot.md`. `propose` asks the planner to turn what the tools already found (docs/review.md,
docs/bugs.md, docs/security.md, the scan profile) into concrete items; the reply is schema-checked in code, capped, and never run
without being on the backlog first. `autopilot --propose` closes the loop: do the work, propose from its findings, do that, until the
budget ends. Undo stays per run (`undo`), and `--review ask` keeps a person in the loop for every diff.
"""
import json
import os
import time

import arch_core as core
import arch_flows as fl

PATH = os.path.join(".arch", "backlog.json")
PROPOSE = """From the findings below, write the NEXT engineering tasks for this project as a JSON array (at most {n} items), most valuable first.
Each item: {{"goal": "<one sentence a builder can act on, naming the file(s)>", "flow": "<one of: {flows}>", "why": "<one line>"}}.
Only tasks the findings support; no inventions, no vague ones ("improve quality"). Prefer: a failing test to fix, a named security finding, a named bug, a missing test for a named function.

FINDINGS:
{findings}"""


def load(root):
    j = core.load_json(os.path.join(root, PATH))
    return j if j.get("items") is not None else {"items": []}


def save(root, bl):
    p = os.path.join(root, PATH)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(bl, f, indent=1)
    os.replace(tmp, p)


def add(root, goal, flow="auto", why="", source="you"):
    bl = load(root)
    goal = goal.strip()
    if any(i["goal"].strip().lower() == goal.lower() and i["status"] in ("todo", "doing") for i in bl["items"]):
        return None
    import uuid
    item = {"id": "b" + uuid.uuid4().hex[:8], "goal": goal, "flow": flow if flow in fl.FLOWS or flow == "auto" else "auto",
            "why": why, "source": source, "status": "todo", "added": time.time(), "result": None}
    bl["items"].append(item)
    save(root, bl)
    return item


def findings_text(root, limit=9000):
    parts = []
    for rel, cap in (("docs/review.md", 3000), ("docs/bugs.md", 2500), ("docs/security.md", 2000), ("docs/config.md", 1000), (".arch/profile.md", 1200)):
        p = os.path.join(root, rel)
        if os.path.isfile(p):
            t, _ = core.read_text(p, cap)
            parts.append(f"=== {rel} ===\n{t.strip()}")
    last = core.latest_run(root)
    if last and os.path.isfile(os.path.join(last, "report.md")):
        t, _ = core.read_text(os.path.join(last, "report.md"), 2000)
        parts.append(f"=== last run report ===\n{t.strip()[-2000:]}")
    return "\n\n".join(parts)[:limit]


def propose(root, n=5, say=print, cfg=None):
    """Ask the planner for the next items from the findings on disk; validate in code; add them. -> list of added items."""
    txt = findings_text(root)
    if not txt.strip():
        say("[propose] nothing to propose from: run review, hunt, secure or scan first")
        return []
    e = core.Engine(root, "propose", "propose next tasks", cfg=cfg, review="dry", say=say, persist=False)
    r, j = e.ask_json("planner", PROPOSE.format(n=n, flows="|".join(k for k in fl.FLOWS if k not in ("new", "pentest", "checkup")), findings=txt))
    items = j if isinstance(j, list) else (j or {}).get("tasks") if isinstance(j, dict) else None
    out = []
    for it in (items or [])[:n]:
        if not isinstance(it, dict) or not isinstance(it.get("goal"), str) or len(it["goal"].strip()) < 12:
            continue
        flow = it.get("flow") if it.get("flow") in fl.FLOWS else "auto"
        added = add(root, it["goal"][:300], flow, str(it.get("why", ""))[:200], source="proposed")
        if added:
            out.append(added)
    if r:
        e.mark(r["id"], bool(out), f"{len(out)} usable item(s)")
    say(f"[propose] {len(out)} item(s) added to the backlog" + ("" if out else " (the reply had no usable task)"))
    return out


def run(root, hours=1.0, max_items=5, review="auto", do_propose=False, say=print, run_one=None, cfg=None):
    """Drain the backlog in order within the budget. `run_one(item) -> result dict` is the Architect run (injectable for tests).
    -> summary dict; also docs/autopilot.md."""
    t_end = time.time() + hours * 3600
    done, bl = [], load(root)
    todo = [i for i in bl["items"] if i["status"] == "todo"]
    if not todo and do_propose:
        propose(root, say=say, cfg=cfg)
        bl = load(root)
        todo = [i for i in bl["items"] if i["status"] == "todo"]
    say(f"[autopilot] {len(todo)} item(s) on the backlog; budget {hours} h / {max_items} item(s); review = {review}")
    while len(done) < max_items and time.time() < t_end:
        bl = load(root)
        item = next((i for i in bl["items"] if i["status"] == "todo"), None)
        if not item:
            if do_propose and propose(root, say=say, cfg=cfg):
                continue
            break
        item["status"], item["started"] = "doing", time.time()
        save(root, bl)
        say(f"\n[autopilot] {item['id']}: {item['goal']}  (flow {item['flow']})")
        try:
            res = run_one(item) if run_one else _run_item(root, item, review, cfg, say)
        except Exception as ex:                                 # one broken item must not stop the queue
            res = {"ok": False, "error": str(ex)[:300]}
        bl = load(root)
        cur = next(i for i in bl["items"] if i["id"] == item["id"])
        cur["status"] = "done" if res.get("ok") else "failed"
        cur["result"] = {k: res.get(k) for k in ("ok", "error", "run", "files") if k in res}
        cur["ended"] = time.time()
        save(root, bl)
        done.append(cur)
        say(f"[autopilot] {item['id']}: {'done' if res.get('ok') else 'failed'}" + (f" ({res.get('error')})" if res.get("error") else ""))
    left = [i for i in load(root)["items"] if i["status"] == "todo"]
    summary = {"done": [d["id"] for d in done if d["status"] == "done"], "failed": [d["id"] for d in done if d["status"] == "failed"], "left": len(left),
               "stopped": "budget" if (time.time() >= t_end or len(done) >= max_items) and left else "empty"}
    _write_doc(root, done, left, summary, hours, review)
    say(f"[autopilot] finished: {len(summary['done'])} done, {len(summary['failed'])} failed, {len(left)} left ({summary['stopped']})")
    return summary


def _run_item(root, item, review, cfg, say):
    import arch_flows
    probe = core.Engine(root, "route", item["goal"], cfg=cfg, review="dry", say=lambda m: None, persist=False)
    flow_id, doms, why = arch_flows.route(probe, item["goal"], None if item["flow"] == "auto" else item["flow"])
    e = core.Engine(root, flow_id, item["goal"], cfg=cfg, review=review, say=say)
    out = arch_flows.run_flow(e, flow_id, {"domains": doms})
    out["run"] = e.run_id
    return out


def _write_doc(root, done, left, summary, hours, review):
    lines = ["# Autopilot", "", f"Budget {hours} h, review `{review}`. Stopped: {summary['stopped']}.", "", "## This session"]
    for d in done:
        r = d.get("result") or {}
        lines.append(f"- {'PASS' if d['status'] == 'done' else 'FAIL'} `{d['id']}` {d['goal']}" + (f" -- run `{r['run']}`" if r.get("run") else "") + (f" -- {r['error']}" if r.get("error") else ""))
    if not done:
        lines.append("- nothing ran")
    lines += ["", f"## Still on the backlog ({len(left)})"] + [f"- `{i['id']}` {i['goal']} ({i['source']})" for i in left[:30]]
    lines += ["", "Every item ran through the normal flows and gates; `python architect.py undo` reverses the last run, `.arch/backlog.json` is the queue."]
    p = os.path.join(root, "docs", "autopilot.md")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
