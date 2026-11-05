"""Self-test for the second half of the training-data export: one sample per task, a quality score, difficulty tags and the stats report.

Hand-made traces, because the point is the RULES:
  * near-identical prompts (extra whitespace) keep only the best-scoring sample, and the other is counted as a duplicate;
  * a reply whose checks could not run (not_sure) and a cloud reply (transport "claude") never become samples, and are counted;
  * a reply the reply cache gave back is not exported a second time; a call the server merely counted exact prefix tokens for is real work and stays;
  * quality: first-try beats retried, a cut-off reply scores less, `min_quality` drops what falls below it;
  * difficulty: tries, escalated, files touched, flow, and a level; tags for a curriculum;
  * the stats report: counts by flow / role / tier / quality band, and how many were dropped and why.

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


def _call(cid, user, reply, label, model="qwen:7b", tier="worker", **kw):
    rec = {"type": "call", "id": cid, "role": "coder", "label": label, "model": model, "tier": tier, "flow": "bugfix",
           "messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": user}], "reply": reply}
    rec.update(kw)
    return rec


def _mark(cid, accepted):
    return {"type": "mark", "id": cid, "accepted": accepted}


GOOD = "FILE: a.py\n```python\nvalue = 1\n```\n"
THREE = "".join(f"FILE: m{i}.py\n```python\nv{i} = {i}\n```\n" for i in range(3))


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    print("\n== training data, round two: dedupe, quality, difficulty, report ==")
    import arch_core as core
    import arch_dataset as ds

    home = tempfile.mkdtemp(prefix="archdata2_")
    old_dd = os.environ.get("ARCHITECT_DATA_DIR")
    old_home = core.HOME
    core.set_home(home)
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "_data")
    proj = os.path.join(home, "proj")
    try:
        dup_a = "Fix the bug in foo so that it returns the page.\nKeep the signature."
        dup_b = "Fix the bug  in foo so that it\n returns the page.   Keep the signature. "
        recs = [
            _call(1, dup_a, GOOD, "fix-try1"), _mark(1, False),
            _call(2, dup_a, GOOD + "# second", "fix-try2"), _mark(2, True),         # retried: lower quality
            _call(3, dup_b, GOOD, "again"), _mark(3, True),                         # same task, first try: the better one
            _call(4, "unverifiable task one", GOOD, "nosure"), _mark(4, None),
            _call(5, "cloud task one", GOOD, "cloud", transport="claude"), _mark(5, True),
            _call(6, "cached task one", GOOD, "cachedhit", cached=True, transport="cache", done_reason="cached"), _mark(6, True),
            _call(7, "prefix counted task", GOOD, "exact", cached_exact=True, transport="llama"), _mark(7, True),
            _call(8, "cut off task", "ok", "cut", done_reason="length"), _mark(8, True),
            _call(9, "big task across files", "FILE: z.py\n```python\nz=0\n```\n", "big-try1"), _mark(9, False),
            _call(10, "big task across files", THREE, "big-try2"), _mark(10, True),
            _call(11, "esc task", GOOD, "esc-try1"), _mark(11, False),
            _call(12, "esc task", GOOD + "# fixed", "esc-try2", model="big:14b", tier="heavy"), _mark(12, True),
            {"type": "escalation", "step": "esc", "role": "coder", "from_model": "qwen:7b", "to_model": "big:14b",
             "from_tier": "worker", "to_tier": "heavy", "flow": "bugfix"},
        ]
        _trace(os.path.join(proj, ".arch", "runs", "r1"), recs)
        data = ds.build([proj])
        rows = data["sft"].get("coder", [])
        st = data["stats"]

        def find(word):
            return [r for r in rows if word in r["messages"][-2]["content"]]

        dups = find("bug")
        check("near-identical prompts (whitespace differs) keep exactly one sample", len(dups) == 1, len(dups))
        check("the kept duplicate is the best-scoring one (first try, not the retry)", dups and dups[0]["meta"]["quality"] == 1.0
              and dups[0]["messages"][-1]["content"] == GOOD, dups[0]["meta"] if dups else None)
        check("the dropped duplicate is counted", st.get("dup") == 1 and st.get("duplicates") == 1, st)
        check("not_sure (accepted null) is excluded and counted", not find("unverifiable") and st.get("not_sure") == 1, st)
        check("a cloud reply (transport claude) is excluded and counted", not find("cloud task") and st.get("claude") == 1, st)
        check("a reply-cache hit is not exported again, and is counted once", not find("cached task") and st.get("cached") == 1, st)
        check("a call with exact prefix-cache counts (cached_exact) is real work and stays", len(find("prefix counted")) == 1)
        check("the reply cache check reads cached / transport / done_reason",
              ds._is_cache_hit({"cached": True}) and ds._is_cache_hit({"transport": "cache"}) and ds._is_cache_hit({"done_reason": "cached"})
              and not ds._is_cache_hit({"cached_exact": True}))

        q_first = ds.quality_score({}, GOOD, 0)
        q_retry = ds.quality_score({}, GOOD, 1)
        q_late = ds.quality_score({}, GOOD, 5)
        q_cut = ds.quality_score({"done_reason": "length"}, GOOD, 0)
        check("quality ordering: first try > one retry > many retries", q_first > q_retry > q_late, (q_first, q_retry, q_late))
        check("quality: a truncated reply scores below a whole one", q_cut < q_first, (q_cut, q_first))
        check("quality stays within 0..1", all(0 <= q <= 1 for q in (q_first, q_retry, q_late, q_cut, ds.quality_score({}, "", 0))))
        check("every exported sample carries meta.quality", all(isinstance(r["meta"].get("quality"), float) for r in rows))

        cut = find("cut off")
        check("a short truncated reply is in the default export, with a lower score", len(cut) == 1 and cut[0]["meta"]["quality"] < 0.8,
              cut[0]["meta"] if cut else None)
        strict = ds.build([proj], min_quality=0.8)
        check("min_quality drops the low scorer and counts it", strict["stats"].get("low_quality") == 1
              and not [r for r in strict["sft"]["coder"] if "cut off" in r["messages"][-2]["content"]], strict["stats"])
        check("min_quality 0 (the default) drops nothing for quality", not st.get("low_quality"))

        big = find("across files")
        d = big[0]["meta"]["difficulty"] if big else {}
        check("difficulty counts tries and files touched", len(big) == 1 and d.get("tries") == 2 and d.get("files") == 3 and d.get("flow") == "bugfix", d)
        check("tags describe the sample (retried, multi-file, level, flow)",
              {"retried", "multi-file", "flow-bugfix"} <= set(big[0]["meta"]["tags"]), big[0]["meta"]["tags"] if big else None)
        easy = find("prefix counted")[0]["meta"]
        check("a first-try single-file pass is easy", easy["difficulty"]["level"] == "easy" and "first-try" in easy["tags"], easy)
        esc = find("esc task")
        check("an escalated step is tagged escalated and hard", len(esc) == 1 and esc[0]["meta"]["difficulty"]["escalated"]
              and esc[0]["meta"]["difficulty"]["level"] == "hard", esc[0]["meta"] if esc else None)
        dist = data["distill"].get("worker", [])
        check("the distillation row has quality and difficulty too", len(dist) == 1 and "quality" in dist[0]["meta"]
              and dist[0]["meta"]["difficulty"]["escalated"], dist)

        rep = data["report"]
        n_rows = len(rows) + len(dist)
        check("report: samples = sft + distillation rows", rep["samples"] == n_rows, rep)
        check("report: counts by flow, role and tier", rep["by_flow"] == {"bugfix": n_rows} and rep["by_role"] == {"coder": n_rows}
              and rep["by_tier"].get("heavy") == 1 and sum(rep["by_tier"].values()) == n_rows, rep)
        check("report: quality bands add up", sum(rep["by_quality"].values()) == n_rows and "high" in rep["by_quality"], rep["by_quality"])
        dr = rep["dropped"]
        check("report: dropped by reason (dup, not_sure, claude, cached)", dr["duplicate"] == 1 and dr["not_sure"] == 1
              and dr["claude"] == 1 and dr["cached"] == 1, dr)
        check("report: rejected replies are counted too (the failed tries)", dr["rejected"] == 3, dr)
        txt = ds.report_text(rep)
        check("report_text names the drops in plain words", "dropped 1: same task again" in txt and "cloud replies" in txt, txt)

        out = os.path.join(home, "out")
        summary = ds.write(data, out)
        check("write: summary carries the report", summary.get("report", {}).get("samples") == n_rows)
        check("write: REPORT.txt exists and the sft file keeps meta.quality",
              os.path.isfile(os.path.join(out, "REPORT.txt"))
              and all("quality" in json.loads(x)["meta"] for x in open(os.path.join(out, "coder.sft.jsonl"), encoding="utf-8")))

        off = ds.build([proj], dedupe=False)
        check("dedupe=False falls back to exact (prompt + reply) duplicates only", len(off["sft"]["coder"]) > len(rows), len(off["sft"]["coder"]))
    finally:
        core.set_home(old_home)
        if old_dd is None:
            os.environ.pop("ARCHITECT_DATA_DIR", None)
        else:
            os.environ["ARCHITECT_DATA_DIR"] = old_dd
        shutil.rmtree(home, ignore_errors=True)

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' DATA2 CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' DATA2 CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
