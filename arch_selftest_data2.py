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
            _call(6, "cached task one", GOOD, "cachedhit", cached=True, transport="cache", done_reason="cached"), _mark(6, True),
            _call(7, "prefix counted task", GOOD, "exact", cached_exact=True, transport="llama"), _mark(7, True),
            _call(8, "cut off task", "ok", "cut", done_reason="length"), _mark(8, True),
            _call(9, "big task across files", "FILE: z.py\n```python\nz=0\n```\n", "big-try1"), _mark(9, False),
            _call(10, "big task across files", THREE, "big-try2"), _mark(10, True),
            _call(11, "esc task", GOOD, "esc-try1"), _mark(11, False),
            _call(12, "esc task", GOOD + "# fixed", "esc-try2", model="big:14b", tier="heavy"), _mark(12, True),
            {"type": "escalation", "step": "esc", "role": "coder", "from_model": "qwen:7b", "to_model": "big:14b",
             "from_tier": "worker", "to_tier": "heavy", "flow": "bugfix"},
            _call(13, "slow task three tries", GOOD, "slow-try1"), _mark(13, False),
            _call(14, "slow task three tries", GOOD + "# b", "slow-try2"), _mark(14, False),
            _call(15, "slow task three tries", GOOD + "# c", "slow-try3"), _mark(15, True),     # third try: quality 0.75
            {"type": "verdict", "verdict": "pass"},
        ]
        _trace(os.path.join(proj, ".arch", "runs", "r1"), recs)
        _trace(os.path.join(proj, ".arch", "runs", "r2"), [_call(5, "cloud task one", GOOD, "cloud", transport="claude"), _mark(5, True),
                                                       _call(6, "local call in a run Claude took part in", GOOD, "x"), _mark(6, True),
                                                       {"type": "verdict", "verdict": "pass"}])
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
        check("a run containing a Claude call gives no SFT at all, even its local calls", not find("Claude took part") and st.get("claude_runs") == 1, st)
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

        check("a reply cut off at its limit (done_reason length) is not an SFT sample, and is counted",
              not find("cut off") and st.get("truncated") == 1, st)
        strict = ds.build([proj], min_quality=0.8)
        check("min_quality drops the low scorer and counts it", strict["stats"].get("low_quality") == 1
              and not [r for r in strict["sft"]["coder"] if "slow task" in r["messages"][-2]["content"]], strict["stats"])
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
        check("report: rejected replies are counted too (the failed tries)", dr["rejected"] == 5, dr)
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

        # ---- round three: every allowed root, and the holdout / fixture / Claude rules wherever the run sits
        import arch_eval
        nest = os.path.join(home, "users", "a", "projects", "app2")
        _trace(os.path.join(nest, ".arch", "runs", "r1"), [_call(1, "nested project task", GOOD, "n"), _mark(1, True)])
        _trace(os.path.join(home, "users", "node_modules", "x", ".arch", "runs", "r1"), [_call(1, "dep task", GOOD, "n"), _mark(1, True)])
        found = ds.discover(os.path.join(home, "users"), depth=6)
        check("discover finds a project run folder outside the current folder", os.path.abspath(nest) in found, found)
        check("discover never enters node_modules", not any("node_modules" in p for p in found), found)
        check("discover respects its depth bound", not ds.discover(os.path.join(home, "users"), depth=2), ds.discover(os.path.join(home, "users"), depth=2))
        bench = arch_eval.kept_runs_dir("bench")
        _trace(os.path.join(bench, ".arch", "runs", "b1"), [_call(1, "bench task held out", GOOD, "b"), _mark(1, True)])
        check("roots() drops the bench bucket even when handed it", bench not in ds.roots(bench) and os.path.abspath(bench) not in ds.roots(bench))
        bd = ds.build([bench])
        check("build() refuses the bench bucket as a root and counts it", not bd["sft"] and bd["stats"].get("bench_skipped") == 1, bd["stats"])
        stray = os.path.join(home, "stray")
        _trace(os.path.join(stray, ".arch", "runs", "s1"), [_call(1, "copied bench task", GOOD, "s"), _mark(1, True)])
        with open(os.path.join(stray, ".arch", "runs", "s1", "eval_task.json"), "w", encoding="utf-8") as f:
            json.dump({"bucket": "bench", "verdict": "pass"}, f)
        sd = ds.build([stray])
        check("a bench-bucket run copied elsewhere is still never trained on", not sd["sft"] and sd["stats"].get("bench_skipped") == 1, sd["stats"])
        _trace(os.path.join(stray, ".arch", "runs", "s2"), [_call(1, "train task whose hidden judges failed", GOOD, "t"), _mark(1, True)])
        with open(os.path.join(stray, ".arch", "runs", "s2", "eval_task.json"), "w", encoding="utf-8") as f:
            json.dump({"bucket": "train", "verdict": "fail"}, f)
        sd = ds.build([stray])
        check("a train run that failed its hidden checks exports nothing but its accepted marks are counted",
              not sd["sft"] and sd["stats"].get("eval_not_passed_accepted_marks") == 1, sd["stats"])
        fx = os.path.join(home, "fx")
        _trace(os.path.join(fx, ".arch", "runs", "f1"), [
            _call(1, "fixture task", GOOD, "f", model="fake:7b"), _mark(1, True),
            _call(3, "real local task", GOOD, "r"), _mark(3, True), {"type": "verdict", "verdict": "pass"}])
        _trace(os.path.join(fx, ".arch", "runs", "f2"), [
            _call(2, "claude by model name", GOOD, "c", model="claude:claude-opus-5-5", transport="llama"), _mark(2, True),
            {"type": "verdict", "verdict": "pass"}])
        fd = ds.build([fx])
        check("fake: fixtures and claude: models are never samples, real local work is",
              [r["messages"][-2]["content"] for r in fd["sft"].get("coder", [])] == ["real local task"]
              and fd["stats"].get("fixture") == 1 and fd["stats"].get("claude") == 1, fd["stats"])

        # ---- validation fixes (2026-11-05): Claude work, few-shot leaks, bench content, run verdict, thinking, per-model files, secrets
        import arch_learn
        import arch_ledger
        v = os.path.join(home, "val")
        PASS = {"type": "verdict", "verdict": "pass"}
        _trace(os.path.join(v, ".arch", "runs", "c1"), [_call(1, "logbook says claude made this", GOOD, "a"), _mark(1, True), PASS])
        real = arch_learn.exclude_from_training
        arch_learn.exclude_from_training = lambda: {"trace:" + os.path.abspath(os.path.join(v, ".arch", "runs", "c1"))}
        try:
            vd = ds.build([v])
        finally:
            arch_learn.exclude_from_training = real
        check("build() skips runs arch_learn.exclude_from_training() names", not vd["sft"] and vd["stats"].get("claude_runs") == 1, vd["stats"])
        shutil.rmtree(v, ignore_errors=True)

        ref_own = "FILE: own.py\n```python\nown = 1\n```"
        led = {"_examples": [{"role": "coder", "model": "qwen:7b", "req": "x", "reply": ref_own}]}
        u_own = "Do the task.\n\nFORMAT REFERENCE (a past reply of yours ...; copy its SHAPE, not its content):\n" + ref_own
        u_other = "Do the task.\n\nFORMAT REFERENCE (a past reply of yours ...; copy its SHAPE, not its content):\nclaude wrote this"
        check("a FORMAT REFERENCE quoting this same local model's own ledger reply is kept", ds.strip_format_ref(u_own, "qwen:7b", led["_examples"]) == (u_own, "kept"))
        check("a FORMAT REFERENCE from another model (or unknown) is cut off the prompt",
              ds.strip_format_ref(u_other, "qwen:7b", led["_examples"]) == ("Do the task.", "stripped")
              and ds.strip_format_ref(u_own, "other:7b", led["_examples"])[1] == "stripped")
        _trace(os.path.join(v, ".arch", "runs", "f1"), [_call(1, u_other, GOOD, "a"), _mark(1, True), PASS])
        vd = ds.build([v])
        rows_v = vd["sft"].get("coder", [])
        check("build() trains on the prompt WITHOUT a foreign FORMAT REFERENCE block and counts it",
              len(rows_v) == 1 and "FORMAT REFERENCE" not in rows_v[0]["messages"][-2]["content"] and vd["stats"].get("format_ref_stripped") == 1, vd["stats"])
        shutil.rmtree(v, ignore_errors=True)

        r_, th, un = ds.split_think("<think>plan it</think>\nFILE: a.py")
        check("complete <think> blocks are cut from the reply and returned separately", r_ == "FILE: a.py" and th == "plan it" and not un)
        check("an unterminated <think is detected", ds.split_think("<think>still going")[2] and ds.split_think("ok <think> x")[2])
        _trace(os.path.join(v, ".arch", "runs", "t1"), [
            _call(1, "thinking task done", "<think>hmm</think>\n" + GOOD, "a"), _mark(1, True),
            _call(2, "thinking task cut", "<think>hmm and hmm", "b"), _mark(2, True), PASS])
        vd = ds.build([v])
        rows_v = vd["sft"].get("coder", [])
        check("the thinking is kept in meta.thinking, the trained reply has none; the unterminated one is skipped",
              len(rows_v) == 1 and rows_v[0]["messages"][-1]["content"] == GOOD and rows_v[0]["meta"].get("thinking") == "hmm"
              and vd["stats"].get("think_unterminated") == 1, vd["stats"])
        shutil.rmtree(v, ignore_errors=True)

        _trace(os.path.join(v, ".arch", "runs", "x1"), [
            _call(1, "a step in a failed run: fix the parser so that it reads every row of the file", "FILE: a.py\n```python\nv = 0\n```\n", "s-try1"), _mark(1, False),
            _call(2, "a step in a failed run: fix the parser so that it reads every row of the file", GOOD, "s-try2"), _mark(2, True), {"type": "verdict", "verdict": "fail"}])
        _trace(os.path.join(v, ".arch", "runs", "x2"), [_call(1, "no verdict recorded", GOOD, "a"), _mark(1, True)])
        vd = ds.build([v])
        check("a real-project run whose verdict is not pass gives no SFT sample (counted), its in-run pair stays",
              not vd["sft"] and vd["stats"].get("run_not_passed") == 2 and len(vd["dpo"].get("coder", [])) == 1, vd["stats"])
        shutil.rmtree(v, ignore_errors=True)

        _trace(os.path.join(v, ".arch", "runs", "e1"), [_call(1, "broken eval_task", GOOD, "a"), _mark(1, True), PASS])
        with open(os.path.join(v, ".arch", "runs", "e1", "eval_task.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        _trace(os.path.join(v, "evals", "runs", "bench", ".arch", "runs", "b9"), [_call(1, "under a bench path", GOOD, "a"), _mark(1, True), PASS])
        vd = ds.build([v, os.path.join(v, "evals", "runs", "bench")])
        check("an eval_task.json that does not parse fails closed (skipped, counted)", vd["stats"].get("eval_task_unreadable") == 1, vd["stats"])
        check("any path containing /evals/runs/bench is skipped", not vd["sft"] and vd["stats"].get("bench_skipped") == 1, vd["stats"])
        shutil.rmtree(v, ignore_errors=True)

        _trace(os.path.join(v, ".arch", "runs", "m1"), [
            _call(1, "model a task", GOOD, "a", model="qwen3:4b"), _mark(1, True),
            _call(2, "model b task", GOOD, "b", model="qwen3:8b"), _mark(2, True), PASS])
        vd = ds.build([v])
        outm = os.path.join(home, "outm")
        sm = ds.write(vd, outm, tier_models={"worker": "qwen3:4b"})
        files = sorted(os.listdir(outm))
        one = [json.loads(x) for x in open(os.path.join(outm, "tier-worker.sft.jsonl"), encoding="utf-8")]
        check("tier files are per model, and tier-worker.sft.jsonl holds only the configured worker's rows",
              "tier-worker.qwen3-4b.sft.jsonl" in files and "tier-worker.qwen3-8b.sft.jsonl" in files
              and [r["meta"]["model"] for r in one] == ["qwen3:4b"], files)
        rec = open(os.path.join(outm, "RECIPE.md"), encoding="utf-8").read()
        check("RECIPE targets the configured worker's base and documents the think policy",
              "--model qwen3:4b --base unsloth/Qwen3-4B-Instruct-2507" in rec and "meta.thinking" in rec and "Qwen2.5-Coder-7B" not in rec)
        ds.write(vd, outm, tier_models={"worker": "eriktonon/qwen3.6-moe-uncensored:iq3"})
        rec = open(os.path.join(outm, "RECIPE.md"), encoding="utf-8").read()
        check("RECIPE gives no command when no base is known for the worker", "No known training base" in rec and "train_lora.py --data" not in rec)
        import train_lora
        mixed = [json.loads(x) for x in open(os.path.join(outm, "coder.sft.jsonl"), encoding="utf-8")]
        check("train_lora refuses a mixed-model file without --model, and filters with it",
              train_lora.pick_model(mixed)[1] and len(train_lora.pick_model(mixed, "qwen3:4b")[0]) == 1 and not train_lora.pick_model(mixed, "qwen3:4b")[1])
        check("train_lora refuses a --base that does not match the model",
              train_lora.base_error("qwen3:8b", "unsloth/Qwen3-4B-Instruct-2507") and not train_lora.base_error("qwen3:4b", "unsloth/Qwen3-4B-Instruct-2507"))
        shutil.rmtree(v, ignore_errors=True)

        hits = ['aws_secret_access_key = "AKfakefakefake1234"', "DB_PASSWORD=hunter2hunter2", 'token="abcd1234efgh5678"',
                '{"api_key": "zz9-plural-z-alpha"}', "Authorization: Basic dXNlcjpwYXNzd29yZA==", "auth_token: tok3nvalue99"]
        misses = ["password = get_password()", "secret = settings.SECRET_VALUE", "api_key = os.environ['API_KEY']",
                  "max_tokens = 2048", "if password == stored_password:", 'token = "<your token>"', "password = request_password",
                  "def check_secret(secret_value):"]
        check("secret filter catches literals (underscored names, token=, Authorization: Basic)", all(ds.has_secret(h) for h in hits),
              [h for h in hits if not ds.has_secret(h)])
        check("secret filter ignores calls, attributes, variables, comparisons and placeholders", not any(ds.has_secret(m) for m in misses),
              [m for m in misses if ds.has_secret(m)])

        fake_tasks = [{"template": "t1", "family": "f", "tags": ["training"]}, {"template": "t1", "family": "f", "tags": ["training", "holdout"]},
                      {"template": "t2", "family": "f", "tags": ["training"]}, {"template": "t3", "family": "f", "tags": []}]
        check("template_overlap finds a template in both training and bench", ds.template_overlap(fake_tasks) == ["f:t1"], ds.template_overlap(fake_tasks))
        real_overlap = ds.template_overlap()
        fam_tasks = []
        for fname in ("distributed", "harness"):
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "evals", "families"))
            try:
                import importlib
                fam_mod = importlib.import_module(fname)
            finally:
                sys.path.pop(0)
            fam_tasks += [dict(t, family=fname) for t in fam_mod.all_tasks()]
        fam_overlap = ds.template_overlap(fam_tasks)
        check("distributed and harness hold out whole templates: zero template overlap between training and bench",
              not fam_overlap and any("holdout" in t["tags"] for t in fam_tasks) and any("holdout" not in t["tags"] for t in fam_tasks), fam_overlap[:5])
        check("the real task set reports no overlap for distributed/harness", not [k for k in real_overlap if k.startswith(("distributed:", "harness:"))], real_overlap[:5])
        print(f"  INFO  holdout-by-template on the real families: {len(real_overlap)} template(s) in both training and bench"
              + (": " + ", ".join(real_overlap[:20]) if real_overlap else ""))

        # ledger: never remember or show Claude / fixture / bench / eval examples
        lg = {}
        base_c = {"role": "coder", "flow": "build", "reply": "FILE: q.py\n```python\nq = 1\n```", "messages": [{"role": "user", "content": "make q"}]}
        arch_ledger._remember(lg, dict(base_c, model="claude:claude-opus-5-5"))
        arch_ledger._remember(lg, dict(base_c, model="fake:7b"))
        check("the ledger never remembers a claude: or fake: reply", not lg.get("_examples"), lg)
        arch_ledger._remember(lg, dict(base_c, model="qwen3:4b"))
        lg["_examples"].append({"role": "coder", "model": "claude:x", "req": "make q", "reply": "FILE: c.py\n"})
        lg["_examples"].append({"role": "coder", "model": "qwen3:4b", "req": "make q", "reply": "FILE: b.py\n", "bench": True})
        got = arch_ledger.example_for("coder", "make q", led=lg)
        check("example_for returns only a local, non-bench example", got and got["model"] == "qwen3:4b" and not got.get("bench"), got)
        check("example_for with model= returns only that model's own reply", arch_ledger.example_for("coder", "make q", led=lg, model="other:1b") is None)
        os.environ["ARCHITECT_EVAL"] = "1"
        try:
            check("example_for refuses under ARCHITECT_EVAL", arch_ledger.example_for("coder", "make q", led=lg) is None)
        finally:
            os.environ.pop("ARCHITECT_EVAL", None)
        cl = arch_ledger.clean(lg, save=False)
        check("ledger.clean drops claude and bench-tagged examples", cl["after"] == 1 and cl["dropped"]["not_local"] == 1 and cl["dropped"]["bench"] == 1, cl)
        fps = {"the test suite in tests fails"}
        check("bench fingerprints match benchmark wording, not other text",
              arch_ledger.is_bench_text("Bug: The test suite in tests/ fails (x).", fps) and not arch_ledger.is_bench_text("make q", fps))
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
