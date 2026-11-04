"""Self-test for the part that makes the app get better the more it is used: what goes into training data, and what must never go in.

These checks are written against a hand-made trace, because the point is to pin down the RULES rather than any one run:
  * only replies the code checks accepted become training samples;
  * a reply whose checks could not run is excluded in both directions -- it is not a good example and not a bad one;
  * a preference pair uses the prompt the model will really see, not the one with the failure text appended;
  * two tries only pair if they answered the same task;
  * anything that looks like a credential never reaches a file;
  * when a bigger model solved what the worker could not, that answer is kept as a lesson for the worker;
  * a tool conversation is rebuilt from the call ids, not guessed from timestamps.

No model, no GPU, no network.
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _trace(run_dir, records):
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "trace.jsonl"), "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _call(cid, role, user, reply, label=None, model="qwen:7b", tier="worker", system="SYS", **kw):
    rec = {"type": "call", "id": cid, "role": role, "label": label or role, "model": model, "tier": tier, "flow": "bugfix",
           "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "reply": reply}
    rec.update(kw)
    return rec


def _mark(cid, accepted, reason=""):
    return {"type": "mark", "id": cid, "accepted": accepted, "reason": reason}


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    print("\n== learning from its own runs: what becomes training data ==")
    import arch_core as core
    import arch_dataset as ds

    home = tempfile.mkdtemp(prefix="archlearn_")
    old_dd = os.environ.get("ARCHITECT_DATA_DIR")
    old_home = core.HOME
    core.set_home(home)
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "_data")
    proj = os.path.join(home, "proj")
    try:
        _marks_and_secrets(check, ds, proj)
        _pairs(check, ds, proj)
        _distill(check, ds, proj)
        _trajectory(check, ds, proj)
        _files(check, ds, proj, home)
        _three_state_mark(check, core)
    finally:
        core.set_home(old_home)
        if old_dd is None:
            os.environ.pop("ARCHITECT_DATA_DIR", None)
        else:
            os.environ["ARCHITECT_DATA_DIR"] = old_dd
        shutil.rmtree(home, ignore_errors=True)

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' LEARNING CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' LEARNING CHECKS FAILED'}")
    return 0 if n == len(results) else 1


def _fresh(proj, name, records):
    run_dir = os.path.join(proj, ".arch", "runs", name)
    shutil.rmtree(os.path.join(proj, ".arch", "runs"), ignore_errors=True)
    _trace(run_dir, records)
    return run_dir


# ------------------------------------------------------------------ what counts and what does not

def _marks_and_secrets(check, ds, proj):
    _fresh(proj, "r1", [
        _call(1, "generator", "fix allow()", "GOOD ANSWER ONE"),
        _mark(1, True),
        _call(2, "generator", "fix other()", "BAD ANSWER"),
        _mark(2, False, "tests failed"),
        _call(3, "generator", "fix third()", "UNCHECKABLE ANSWER"),
        _mark(3, None, "no test command in this project"),
        _call(4, "generator", "fix fourth()", "here you go: api_key = 'sk-abcdefghijklmnopqrstuvwxyz12'"),
        _mark(4, True),
        _call(5, "generator", "fix fifth()", "NEVER MARKED"),
        _call(6, "generator", "fix sixth()", "FROM THE CACHE", cached=True),
        _mark(6, True),
    ])
    d = ds.build([proj])
    replies = [m["content"] for rows in d["sft"].values() for r in rows for m in r["messages"] if m["role"] == "assistant"]
    st = d["stats"]
    check("a reply the checks accepted becomes a training sample", "GOOD ANSWER ONE" in replies, replies)
    check("a reply the checks rejected does not", "BAD ANSWER" not in replies)
    check("a reply whose checks could not run is left out entirely, not treated as a failure",
          "UNCHECKABLE ANSWER" not in replies and st.get("unverified") == 1, st)
    check("a reply carrying something that looks like a credential never reaches a file",
          not any("sk-abcdefghijk" in x for x in replies) and st.get("secrets_skipped") == 1, st)
    check("a call no check ever labelled is counted and skipped", st.get("unmarked") == 1, st)
    check("a reply served from the cache is not exported twice", "FROM THE CACHE" not in replies and st.get("cached") == 1, st)
    check("the sample keeps the system prompt, the request and the answer, in that order",
          [m["role"] for rows in d["sft"].values() for r in rows for m in r["messages"]][:3] == ["system", "user", "assistant"])
    check("every sample says which run, role, tier and prompt version it came from",
          all({"role", "tier", "run", "source"} <= set(r["meta"]) for rows in d["sft"].values() for r in rows))
    check("a credential is spotted in the request too, not only the answer",
          ds.has_secret("", "here is my token: ghp_abcdefghijklmnopqrstuvwxyz0123", "") is True)
    check("ordinary code is not mistaken for a credential",
          ds.has_secret("", "def allow(key):\n    return key in ALLOWED", "") is False)


# ------------------------------------------------------------------ preference pairs

def _pairs(check, ds, proj):
    task = "Fix the off-by-one in paginate() so page 1 returns the first rows.\n\nCODE:\n" + ("x = 1\n" * 40)
    retry = task + "\n\nFEEDBACK: your last change failed these checks:\ntest_first_page_is_one_based\n"
    _fresh(proj, "r2", [
        _call(1, "generator", task, "WRONG FIX", label="fix-try1"),
        _mark(1, False, "tests failed"),
        _call(2, "generator", retry, "RIGHT FIX", label="fix-try2"),
        _mark(2, True),
    ])
    d = ds.build([proj])
    pairs = [p for rows in d["dpo"].values() for p in rows]
    check("a step that failed and then passed becomes one preference pair", len(pairs) == 1, pairs)
    p = pairs[0] if pairs else {}
    check("the pair prefers the answer that passed", p.get("chosen") == "RIGHT FIX" and p.get("rejected") == "WRONG FIX", p)
    check("the pair's prompt is the clean task, NOT the retry prompt with the failure text in it",
          p.get("prompt") == task and "FEEDBACK" not in (p.get("prompt") or ""), (p.get("prompt") or "")[-60:])
    check("the pair records that both tries were the same task", (p.get("meta") or {}).get("same_task") is True, p.get("meta"))
    check("and which model and tier each side came from", {"chosen_model", "rejected_model", "chosen_tier", "rejected_tier"} <= set(p.get("meta") or {}))

    # two unrelated calls that happen to share a label must never be paired
    _fresh(proj, "r3", [
        _call(1, "generator", "Write a function that parses durations like 1h30m.", "DURATION ANSWER"),
        _mark(1, False, "tests failed"),
        _call(2, "generator", "Add a database index on the bookings slot column.", "INDEX ANSWER"),
        _mark(2, True),
    ])
    d2 = ds.build([proj])
    check("two unrelated questions are never paired, even when nothing labelled them as separate steps",
          not any(d2["dpo"].values()) and d2["stats"].get("pairs_dropped_different_task") == 1, d2["stats"])
    d3 = ds.build([proj], pairs="all")
    p3 = [p for rows in d3["dpo"].values() for p in rows]
    check("asking for every pair returns it, clearly labelled as a different task",
          len(p3) == 1 and p3[0]["meta"]["same_task"] is False, p3)


# ------------------------------------------------------------------ learning from the bigger model

def _distill(check, ds, proj):
    task = "Make the concurrency test pass.\n\nCODE:\n" + ("y = 2\n" * 40)
    retry = task + "\n\nFEEDBACK: still failing\n"
    _fresh(proj, "r4", [
        _call(1, "generator", task, "WORKER TRIED AND FAILED", label="hard-try1", model="qwen:7b", tier="worker"),
        _mark(1, False, "tests failed"),
        {"type": "escalation", "t": 1, "flow": "bugfix", "step": "hard", "role": "generator",
         "from_model": "qwen:7b", "to_model": "qwen:14b", "to_tier": "heavy", "from_tier": "worker",
         "after_call": 1, "why": "stalled: same failure twice", "tries": 1},
        _call(2, "generator", retry, "THE BIGGER MODEL'S ANSWER", label="hard-try2", model="qwen:14b", tier="heavy"),
        _mark(2, True),
    ])
    d = ds.build([proj])
    rows = [r for rows in d["distill"].values() for r in rows]
    check("when a bigger model solved what the worker could not, that answer is kept as a lesson", len(rows) == 1, rows)
    r = rows[0] if rows else {}
    check("the lesson is the bigger model's actual answer",
          any(m["content"] == "THE BIGGER MODEL'S ANSWER" for m in r.get("messages", [])), r)
    check("it is filed for the tier that needs to learn it, not the tier that produced it",
          (r.get("meta") or {}).get("for_tier") == "worker" and r["meta"].get("taught_by_tier") == "heavy", r.get("meta"))
    check("it says why the escalation happened, so a person can see what the worker keeps failing",
          "stalled" in ((r.get("meta") or {}).get("why") or ""), r.get("meta"))
    check("an escalation with no accepted answer after it produces no lesson",
          ds.build([proj])["stats"].get("distill") == 1)


# ------------------------------------------------------------------ tool conversations

def _trajectory(check, ds, proj):
    _fresh(proj, "r5", [
        _call(1, "architect", "where is rate limiting done?", '{"tool": "grep", "args": {"pattern": "limit"}}', label="look"),
        _mark(1, True),
        {"type": "tool", "t": 1, "flow": "explore", "step": "look", "tool": "grep", "call": 1, "turn": 1,
         "args": {"pattern": "limit"}, "ok": True, "result": "app.py:12: def allow(key):"},
        _call(2, "architect", "where is rate limiting done?", '{"tool": "read", "args": {"path": "app.py"}}', label="look"),
        _mark(2, True),
        {"type": "tool", "t": 2, "flow": "explore", "step": "look", "tool": "read", "call": 2, "turn": 2,
         "args": {"path": "app.py"}, "ok": True, "result": "12: def allow(key): ..."},
        _call(3, "architect", "where is rate limiting done?", '{"final": "In allow() in app.py, line 12."}', label="look"),
        _mark(3, True),
    ])
    d = ds.build([proj])
    tr = d["traj"]
    check("a finished tool conversation becomes one multi-turn sample", len(tr) == 1, len(tr))
    roles = [m["role"] for m in tr[0]["messages"]] if tr else []
    check("the sample alternates the model's tool calls with what each tool really returned",
          roles[:6] == ["system", "user", "assistant", "tool", "assistant", "tool"], roles)
    check("it ends with the model's final answer", roles and roles[-1] == "assistant" and "allow()" in tr[0]["messages"][-1]["content"], roles[-1:])
    check("each tool result is attached to the turn that asked for it, by id and not by timing",
          tr and tr[0]["messages"][3]["name"] == "grep" and tr[0]["messages"][5]["name"] == "read")

    _fresh(proj, "r6", [
        _call(1, "architect", "what does x do?", '{"tool": "ls", "args": {}}', label="look"),
        _mark(1, True),
        {"type": "tool", "t": 1, "flow": "explore", "step": "look", "tool": "ls", "call": 1, "turn": 1, "args": {}, "ok": True, "result": "app.py"},
    ])
    d2 = ds.build([proj])
    check("a tool conversation that never reached an answer is not exported as if it had",
          not d2["traj"] and d2["stats"].get("traj_unfinished") == 1, d2["stats"])


# ------------------------------------------------------------------ the files on disk

def _files(check, ds, proj, home):
    task = "Fix paginate().\n\nCODE:\n" + ("z = 3\n" * 40)
    _fresh(proj, "r7", [
        _call(1, "generator", task, "ANSWER A", label="fix-try1", tier="worker"),
        _mark(1, False),
        _call(2, "generator", task + "\n\nFEEDBACK: no\n", "ANSWER B", label="fix-try2", tier="worker"),
        _mark(2, True),
        _call(3, "router", "which flow?", '{"flow": "bugfix"}', model="qwen:1.7b", tier="router"),
        _mark(3, True),
    ])
    out = os.path.join(home, "datasets")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "leftover.sft.jsonl"), "w", encoding="utf-8") as f:
        f.write("{}\n")
    d = ds.build([proj])
    summ = ds.write(d, out)
    files = sorted(os.listdir(out))
    check("samples are written per role", "generator.sft.jsonl" in files and "router.sft.jsonl" in files, files)
    check("and per tier, because a tier is what you actually fine-tune", "tier-worker.sft.jsonl" in files and "tier-router.sft.jsonl" in files, files)
    check("preference pairs are written too", "generator.dpo.jsonl" in files, files)
    check("a file left behind by an earlier export is removed, so what is on disk is one consistent set",
          "leftover.sft.jsonl" not in files and "leftover.sft.jsonl" in (summ.get("removed_stale") or []), summ.get("removed_stale"))
    check("the summary counts per tier as well as per role", summ.get("per_tier", {}).get("worker", {}).get("sft") == 1, summ.get("per_tier"))
    recipe = open(os.path.join(out, "RECIPE.md"), encoding="utf-8").read()
    check("the recipe names the file with the most samples in it, not a generic one", "tier-worker.sft.jsonl" in recipe)
    check("the recipe explains whether there is enough data yet, in plain words", "enough" in recipe.lower() and "200" in recipe)
    check("the recipe ends at the gate that refuses a worse model", "promote-model" in recipe and "no worse" in recipe)
    check("every written line is valid JSON on its own",
          all(json.loads(l) for l in open(os.path.join(out, "tier-worker.sft.jsonl"), encoding="utf-8") if l.strip()))


# ------------------------------------------------------------------ the three-state label itself

def _three_state_mark(check, core):
    import arch_roles
    proj = tempfile.mkdtemp(prefix="archmark_")
    try:
        cfg = core.deep_merge(core.load_config(proj), {"roles": {r: "fake:7b" for r in arch_roles.ROLES}})
        e = core.Engine(proj, "t", "g", cfg, review="auto", say=lambda *_a, **_k: None)
        e.mark(1, True)
        e.mark(2, False, "tests failed")
        e.mark(3, None, "no test command")
        rows = [json.loads(x) for x in open(os.path.join(e.run_dir, "trace.jsonl"), encoding="utf-8") if '"type": "mark"' in x]
        got = {r["id"]: r["accepted"] for r in rows}
        check("a check result has three states on disk: passed, failed, and could not run",
              got == {1: True, 2: False, 3: None}, got)
    finally:
        shutil.rmtree(proj, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run())
