"""Read-only inventory of every trace / record on this machine that could be training data, and what each one is legally usable for.

    py -3.12 tools/data_inventory.py            # tables
    py -3.12 tools/data_inventory.py --json     # the same, machine-readable

Reads only. Never loads a model, starts a server, writes a dataset or touches a setting. The dataset numbers come from
`arch_dataset.build` run in memory over every ALLOWED root (this folder, the eval `train` bucket, the portfolio, and every project
with `.arch/runs` found under the home folder, depth <= 6); the bench bucket is read only for the "non-training uses" section.
Rules (CLAUDE.md): Claude-written content is never fine-tuning data; the bench bucket is never trained on; not_sure is excluded both ways.
"""
import collections
import glob
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import arch_core as core      # noqa: E402
import arch_dataset as ds     # noqa: E402

TOK_S = 70.0                  # measured worker speed (qwen3:8b class on this laptop), used only for the throughput estimate


def _C():
    return collections.Counter()


def scan_root(root):
    """Every run under one root: calls, marks (True/False/None), by model / flow / verdict, who wrote it."""
    r = {"root": root, "runs": 0, "calls": 0, "local_calls": 0, "claude_calls": 0, "fixture_calls": 0, "cached": 0,
         "marks": _C(), "by_model": _C(), "by_flow": _C(), "verdict": _C(), "eval_verdict": _C(), "family": _C(),
         "escalations": 0, "tool_records": 0, "postmortems": 0, "assisted": 0, "gen_tokens": 0, "secs": 0.0,
         "runs_with_accept": 0, "runs_with_fix_pair": 0}
    for run in ds.runs_of(root):
        r["runs"] += 1
        full = ds.read_full(os.path.join(run, "trace.jsonl"))
        if os.path.isfile(os.path.join(run, "postmortem.md")):
            r["postmortems"] += 1
        try:
            et = json.load(open(os.path.join(run, "eval_task.json"), encoding="utf-8"))
            r["eval_verdict"][str(et.get("verdict"))] += 1
            r["family"][str(et.get("family") or "?")] += 1
        except (OSError, ValueError):
            pass
        r["claude_calls"] += full["claude"]
        r["calls"] += full["claude"]
        v = (full["verdict"] or {}).get("verdict")
        r["verdict"][str(v)] += 1
        r["escalations"] += len(full["escalations"])
        r["tool_records"] += len(full["tools"])
        r["assisted"] += len(full["assisted"])
        acc = False
        steps = collections.defaultdict(set)
        for cid, c in full["calls"].items():
            r["calls"] += 1
            m = str(c.get("model") or "?")
            if m.startswith("fake:"):
                r["fixture_calls"] += 1
            elif m.startswith("claude:"):
                r["claude_calls"] += 1
            else:
                r["local_calls"] += 1
            if ds._is_cache_hit(c):
                r["cached"] += 1
            r["by_model"][m] += 1
            r["by_flow"][str(c.get("flow") or "?")] += 1
            r["gen_tokens"] += int(c.get("gen_tokens") or 0)
            r["secs"] += float(c.get("secs") or 0)
            mk = full["marks"].get(cid)
            a = "unmarked" if not mk else {True: "accepted", False: "rejected", None: "not_sure"}.get(mk.get("accepted"), "other")
            r["marks"][a] += 1
            if a == "accepted":
                acc = True
            if a in ("accepted", "rejected"):
                steps[(c.get("role"), ds._step_key(c.get("label")))].add(a)
        r["runs_with_accept"] += acc
        r["runs_with_fix_pair"] += any(s == {"accepted", "rejected"} for s in steps.values())
    for k in ("marks", "by_model", "by_flow", "verdict", "eval_verdict", "family"):
        r[k] = dict(r[k])
    r["secs"] = round(r["secs"], 1)
    return r


def ledger(dd):
    p = os.path.join(dd, "ledger.json")
    j = core.load_json(p) or {}
    ex = j.get("_examples") or []
    by = _C()
    for e in ex:
        m = str(e.get("model") or "?")
        by["fixture" if m.startswith("fake:") else "claude" if m.startswith("claude:") else "local"] += 1
    stats = {m: {role: {"n": v.get("n"), "ok": v.get("ok")} for role, v in roles.items()}
             for m, roles in j.items() if not m.startswith("_") and isinstance(roles, dict)}
    return {"path": p, "runs": len(j.get("_runs") or []), "examples": len(ex), "examples_by_writer": dict(by), "model_role_stats": stats}


def logbook(dd):
    p = os.path.join(dd, "logbook.db")
    if not os.path.isfile(p):
        return {"path": p, "missing": True}
    c = sqlite3.connect("file:" + p.replace("\\", "/") + "?mode=ro", uri=True)
    try:
        out = {"path": p, "tasks": c.execute("select count(*) from tasks").fetchone()[0],
               "steps": c.execute("select count(*) from steps").fetchone()[0],
               "by_claude": {str(k): n for k, n in c.execute("select by_claude, count(*) from tasks group by 1")},
               "by_source": {f"{s}|claude={b}": n for s, b, n in c.execute("select source, by_claude, count(*) from tasks group by 1,2")},
               "by_status": {str(s): n for s, n in c.execute("select status, count(*) from tasks group by 1")}}
    finally:
        c.close()
    return out


def jsonl_count(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def other_sources(dd):
    out = {}
    cc = glob.glob(os.path.join(dd, "cc_runs", "*.jsonl"))
    out["cc_runs"] = {"sessions": len(cc), "events": sum(jsonl_count(p) for p in cc), "writer": "claude"}
    al = glob.glob(os.path.join(dd, "learning", "agent_logs", "*"))
    out["agent_logs"] = {"files": len(al), "writer": "claude"}
    out["reasoning_corpus"] = {"lines": jsonl_count(os.path.join(dd, "learning", "reasoning_corpus.jsonl")), "writer": "claude"}
    out["learning_ledger"] = {"lines": jsonl_count(os.path.join(dd, "learning", "ledger.jsonl")), "writer": "claude (agent costs)"}
    out["crew_log"] = {"lines": jsonl_count(os.path.join(dd, "crew_log.jsonl")), "writer": "claude (managed agents)"}
    orch = glob.glob(os.path.join(dd, "orchestration", "**", "*"), recursive=True)
    out["orchestration"] = {"files": len([p for p in orch if os.path.isfile(p)]), "dirs": os.listdir(os.path.join(dd, "orchestration"))
                            if os.path.isdir(os.path.join(dd, "orchestration")) else []}
    out["eval_reports"] = {"files": len(glob.glob(os.path.join(dd, "evals", "*.json"))), "evals_jsonl": jsonl_count(os.path.join(dd, "evals.jsonl"))}
    out["studio_threads"] = {"persisted": 0, "note": "arch_chat.THREADS is in memory only; nothing on disk"}
    return out


def collect(base=None, depth=6):
    import arch_eval
    dd = core.data_dir()
    train, bench = arch_eval.kept_runs_dir("train"), arch_eval.kept_runs_dir("bench")
    allowed = ds.roots(HERE, include_portfolio=True, include_eval_training=True, include_discovered=True,
                       base=base or os.path.expanduser("~"), depth=depth)
    if os.path.isdir(os.path.join(train, ".arch", "runs")) and os.path.abspath(train) not in allowed:
        allowed.append(os.path.abspath(train))
    scanned = {p: scan_root(p) for p in allowed}
    scanned_bench = scan_root(bench)
    built = ds.build(allowed)                       # in memory: nothing is written
    st = built["stats"]
    mined = {"stats": st, "report": built["report"],
             "sft_by_role": {k: len(v) for k, v in built["sft"].items()},
             "dpo_by_role": {k: len(v) for k, v in built["dpo"].items()},
             "dpo_same_task": sum(1 for v in built["dpo"].values() for r in v if r["meta"]["same_task"]),
             "distill_by_tier": {k: len(v) for k, v in built["distill"].items()},
             "traj": len(built["traj"]),
             "sft_by_model": dict(collections.Counter(r["meta"].get("model") for v in built["sft"].values() for r in v))}
    all_pairs = ds.build(allowed, pairs="all")
    mined["dpo_any_task"] = sum(len(v) for v in all_pairs["dpo"].values())
    # Non-training uses of the bench bucket and Claude records (cost model, triage, prompt fixes, few-shot, eval generation)
    b = scanned_bench
    uses = {"bench_cost_points": b["local_calls"], "bench_runs": b["runs"], "bench_families": b["family"],
            "bench_eval_verdicts": b["eval_verdict"], "bench_rejected_marks": b["marks"].get("rejected", 0),
            "bench_gen_tokens": b["gen_tokens"], "bench_secs": b["secs"],
            "bench_tok_s": round(b["gen_tokens"] / b["secs"], 1) if b["secs"] else None}
    try:
        import arch_triage
        fam, live, live_pass, task_fam = _C(), 0, 0, _C()
        for p in glob.glob(os.path.join(dd, "evals", "*.json")):     # saved LIVE eval reports (the oracle's --fake ones skipped)
            rep = core.load_json(p) or {}
            if rep.get("fake"):
                continue
            for res in rep.get("results") or []:
                live += 1
                live_pass += res.get("verdict") == "pass"
                task_fam[str(res.get("family") or "?")] += 1
                fam[str(arch_triage.classify(res) or "pass")] += 1
        uses["live_eval_results"] = live
        uses["live_eval_passes"] = live_pass
        uses["live_eval_by_family"] = dict(task_fam)
        uses["triage_families"] = dict(fam)
    except Exception as e:
        uses["triage_families"] = f"arch_triage not importable: {e.__class__.__name__}"
    gates = _C()
    for run in ds.runs_of(bench):
        try:
            for line in open(os.path.join(run, "trace.jsonl"), encoding="utf-8"):
                j = json.loads(line)
                if j.get("type") == "check" and j.get("ok") is False:
                    gates[str(j.get("gate"))] += 1
        except (OSError, ValueError):
            pass
    uses["bench_failed_gates"] = dict(gates.most_common(12))
    return {"data_dir": dd, "allowed_roots": allowed, "bench_root": bench, "roots": scanned, "bench": scanned_bench,
            "ledger": ledger(dd), "logbook": logbook(dd), "other": other_sources(dd), "mined": mined, "non_training": uses,
            "tok_s_assumed": TOK_S}


def _row(cells):
    return "| " + " | ".join(str(c) for c in cells) + " |"


def text(rep):
    L = [f"data dir: {rep['data_dir']}", "", "## Trace roots (allowed for training)",
         _row(["root", "runs", "calls", "local", "claude", "fake", "accepted", "rejected", "not_sure", "unmarked", "fix-pair runs"]),
         _row(["---"] * 11)]
    for p, r in list(rep["roots"].items()) + [(rep["bench_root"] + "  [BENCH, not for training]", rep["bench"])]:
        m = r["marks"]
        L.append(_row([p, r["runs"], r["calls"], r["local_calls"], r["claude_calls"], r["fixture_calls"], m.get("accepted", 0),
                       m.get("rejected", 0), m.get("not_sure", 0), m.get("unmarked", 0), r["runs_with_fix_pair"]]))
    lg, lb, ot = rep["ledger"], rep["logbook"], rep["other"]
    L += ["", f"ledger.json: {lg['runs']} runs, {lg['examples']} few-shot examples {lg['examples_by_writer']}",
          f"logbook.db: {lb.get('tasks')} tasks / {lb.get('steps')} steps, by_claude {lb.get('by_claude')}, by source {lb.get('by_source')}"]
    for k, v in ot.items():
        L.append(f"{k}: {v}")
    mi = rep["mined"]
    L += ["", "## Mined (arch_dataset.build, in memory, all allowed roots)",
          f"SFT {mi['stats'].get('sft', 0)} {mi['sft_by_role']}  DPO {mi['stats'].get('dpo', 0)} (same-task {mi['dpo_same_task']}, any-task {mi['dpo_any_task']})"
          f"  distill {mi['stats'].get('distill', 0)}  trajectories {mi['traj']}",
          f"stats: {mi['stats']}", "", "## Non-training uses (bench + Claude records)", json.dumps(rep["non_training"], indent=1)]
    return "\n".join(L)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    depth = 6
    if "--depth" in argv:
        depth = int(argv[argv.index("--depth") + 1])
    rep = collect(depth=depth)
    print(json.dumps(rep, indent=1, default=str) if "--json" in argv else text(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
