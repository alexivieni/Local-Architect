"""Self-test for the local learning loop: the "improve my worker" plan, what train_lora.py would train on, and the best-settings summary.

  * tune_plan: the seven steps in order (baseline, export, check, train, convert, eval, promote), each with plain words, a command and
    what it needs; times say measured or estimate; training is blocked (with the reason) on too few samples, too little disk, an
    unknown base checkpoint; the model servers are stopped first when they hold the card; promote only after the eval compare;
  * train_lora curation in a dry run: cloud-made samples never go in (every place the flag can sit), the quality floor, unscored
    samples kept and counted, easy -> hard order with the best first inside a level, high-quality samples repeated beside themselves,
    a real run refuses below 50 samples before touching any GPU package, the dry run writes nothing;
  * best_settings: shape (value / why / evidence on every item), measured beats the planner, the planner beats nothing.

No model, no GPU, no network: every machine fact is passed in.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _row(i, q=0.9, level="easy", flow="bugfix", **meta):
    m = {"role": "coder", "tier": "worker", "flow": flow, "source": "accepted", "run": "r1"}
    if q is not None:
        m["quality"] = q
    if level:
        m["difficulty"] = {"level": level, "tries": 1, "escalated": False, "files": 1}
        m["tags"] = ["level-" + level]
    m.update(meta)
    return {"messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": f"task {i}: fix the thing " + "x" * 50},
                         {"role": "assistant", "content": f"EDIT: a.py\nreply {i}"}], "meta": m}


def _write(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def run():
    results = []

    def check(name, cond, detail=""):
        results.append(bool(cond))
        print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f"   {detail}"))

    home = tempfile.mkdtemp(prefix="arch_tune2_")
    old_dd = os.environ.get("ARCHITECT_DATA_DIR")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "data")
    try:
        import arch_autotune as at
        import arch_port
        import train_lora as tl

        # ------------------------------------------------------------------ curation
        print("train_lora curation")
        rows = ([_row(i, 0.9, "hard") for i in range(3)] + [_row(10 + i, 0.7, "easy") for i in range(3)] +
                [_row(20, 0.95, "easy"), _row(21, 0.6, "medium"), _row(22, None, None)] +
                [_row(30, 0.2, "easy")] +
                [_row(40, 0.99, "easy", by_claude=True), _row(41, 0.99, "easy", taught_by="claude-opus"),
                 dict(_row(42, 0.99, "easy"), transport="claude"), _row(43, 0.99, "easy", transport="claude")] +
                [{"messages": [{"role": "user", "content": "q"}, {"role": "assistant", "content": "  "}], "meta": {"quality": 1.0}}])
        cur = tl.curate(rows, min_quality=0.5, curriculum=True, weight="none")
        texts = [r["messages"][-1]["content"] for r in cur["rows"]]
        check("cloud-made samples never go in (meta.by_claude, taught_by claude, transport claude top-level and in meta)",
              cur["dropped"]["claude"] == 4 and not any(("reply 4" + c) in " ".join(texts) for c in "0123"), cur["dropped"])
        check("the quality floor drops what is below it", cur["dropped"]["low_quality"] == 1 and not any(t.endswith("reply 30") for t in texts))
        check("an unscored sample (older export) is kept and counted", cur["unscored"] == 1 and any(t.endswith("reply 22") for t in texts))
        check("a sample with no assistant reply is dropped", cur["dropped"]["no_reply"] == 1)
        lv = [tl.level_of(r) for r in cur["rows"]]
        order = [tl.LEVELS.get(x, 3) for x in lv]
        check("curriculum: easy -> medium -> hard -> unknown", order == sorted(order) and lv[0] == "easy" and lv[-1] is None, lv)
        check("curriculum: the best quality first inside a level", texts[0].endswith("reply 20"), texts[:3])
        check("unique count and the per-level tally agree", cur["unique"] == 9 and sum(cur["by_level"].values()) == 9, (cur["unique"], cur["by_level"]))
        rep = tl.curate(rows, min_quality=0.5, curriculum=True, weight="repeat")
        rt = [r["messages"][-1]["content"] for r in rep["rows"]]
        check("weight=repeat shows each quality >= 0.8 sample twice, beside itself so the order holds",
              rep["repeated"] == 4 and len(rep["rows"]) == 13 and rt[0] == rt[1] and rt[0].endswith("reply 20"), rt[:4])
        check("curation is pure: the same input gives the same order", [r["messages"][-1]["content"] for r in tl.curate(rows, 0.5, True)["rows"]] == texts)
        tag_only = {"messages": rows[0]["messages"], "meta": {"tags": ["level-medium"]}}
        check("the level is read from a level-* tag when difficulty is absent", tl.level_of(tag_only) == "medium")

        ds = os.path.join(home, "data", "datasets", "tier-worker.sft.jsonl")
        _write(ds, rows)
        out = os.path.join(home, "data", "lora_out")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = tl.main(["--data", ds, "--out", out, "--dry-run", "--min-quality", "0.5", "--curriculum"])
        txt = buf.getvalue()
        check("dry run: exit 0, says what was left out and why, writes nothing",
              rc == 0 and "cloud model" in txt and "quality floor" in txt and "would refuse" in txt and not os.path.exists(out), txt)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = tl.main(["--data", ds, "--out", out, "--min-quality", "0.5"])
        check("a real run refuses below 50 usable samples before touching a GPU package", rc == 1 and "too few" in buf.getvalue()
              and not os.path.exists(out), buf.getvalue())
        lines = tl.describe(cur, ds, 0, 2.0, {"tok_per_s": 500})
        check("dry run with a measured training speed says the time is measured", any("measured" in x for x in lines), lines)

        # ------------------------------------------------------------------ the plan
        print("tune_plan")
        big = [_row(i, 0.9, ("easy", "medium", "hard")[i % 3]) for i in range(60)] + [_row(99, 0.99, "easy", by_claude=True)]
        _write(ds, big)
        dd = os.path.join(home, "data")
        cfg = {"tiers": {"worker": "qwen2.5-coder:7b"}}
        kw = dict(cfg=cfg, data_dir=dd, live={"vram_gb": 8.0, "vram_used_gb": 0.5}, disk_free_gb=200.0, last_eval={"secs": 1200},
                  train_stats={"tok_per_s": 500.0}, servers_up=["worker"])
        p = at.tune_plan(baseline={}, **kw)
        ids = [s["id"] for s in p["steps"]]
        check("seven steps in order", ids == ["baseline", "export", "check", "train", "convert", "eval", "promote"], ids)
        check("every step has plain words, at least one command and its needs",
              all(s["words"] and s["commands"] and all(k in s["needs"] for k in ("gpu", "gpu_free_gb", "disk_gb", "minutes", "evidence"))
                  for s in p["steps"]))
        st = {s["id"]: s for s in p["steps"]}
        check("the plan counts samples after curation (the cloud-made one is not counted)", p["samples"] == 60, p["samples"])
        check("no baseline yet: measuring it is the first thing to do", st["baseline"]["state"] == "ready" and p["next"] == "baseline")
        check("eval time comes from the last live eval, labelled measured",
              st["baseline"]["needs"]["minutes"] == 20 and "measured" in st["baseline"]["needs"]["evidence"] and st["eval"]["needs"]["minutes"] == 20)
        want = max(1, round(p["tokens"] * 2.0 / 500.0 / 60))
        check("training time from the measured training speed", st["train"]["needs"]["minutes"] == want and "measured" in st["train"]["needs"]["evidence"],
              st["train"]["needs"])
        check("enough samples, disk and a known base: training is ready", st["train"]["state"] == "ready" and p["base_known"], st["train"]["why"])
        check("the model servers are stopped before training when they hold the card", st["train"]["commands"][0] == "python architect.py server stop")
        check("training uses the quality floor and the curriculum", "--min-quality 0.6" in st["train"]["commands"][-1]
              and "--curriculum" in st["train"]["commands"][-1] and "Qwen2.5-Coder-7B" in st["train"]["commands"][-1])
        check("the check step is a dry run", st["check"]["commands"][0].endswith("--dry-run") and st["check"]["needs"]["gpu"] == "none")
        check("export runs the dataset command", st["export"]["commands"][0].startswith("python architect.py dataset"))
        check("the eval compares against the baseline, and promote comes after it, only on no regression",
              "--compare baseline" in st["eval"]["commands"][-1] and "promote-model --tier worker" in st["promote"]["commands"][0]
              and "--compare baseline" in st["promote"]["commands"][0] and "regression" in st["promote"]["why"])
        check("training needs a free card and disk, with numbers", st["train"]["needs"]["gpu_free_gb"] > 5 and st["train"]["needs"]["disk_gb"] > 10)
        check("plain-words lines: one header plus a block per step", isinstance(p["lines"], list) and len(p["lines"]) > 7 * 2
              and all(isinstance(x, str) for x in p["lines"]))

        p2 = at.tune_plan(baseline={"summary": {}}, **kw)
        check("a saved baseline: that step is done and the export is next", p2["steps"][0]["state"] == "done" and p2["next"] == "export")
        p3 = at.tune_plan(baseline={"summary": {}}, **dict(kw, disk_free_gb=3.0))
        t3 = next(s for s in p3["steps"] if s["id"] == "train")
        check("too little disk blocks training and says so", t3["state"] == "blocked" and "disk" in t3["why"], t3["why"])
        p4 = at.tune_plan(baseline={"summary": {}}, **dict(kw, cfg={"tiers": {"worker": "some-niche-finetune:9b"}}))
        t4 = next(s for s in p4["steps"] if s["id"] == "train")
        check("an unknown worker: training is blocked until the exact base is named", not p4["base_known"] and "--base" in t4["why"])
        p5 = at.tune_plan(baseline={"summary": {}}, **dict(kw, servers_up=[], live={"vram_gb": 4.0, "vram_used_gb": 2.0}))
        t5 = next(s for s in p5["steps"] if s["id"] == "train")
        check("too little free graphics memory (no server to stop) blocks training", t5["state"] == "blocked" and "graphics memory" in t5["why"]
              and t5["commands"][0].startswith("python train_lora.py"), t5)
        os.remove(ds)
        p6 = at.tune_plan(baseline={"summary": {}}, **dict(kw, train_stats={}, last_eval={}))
        s6 = {s["id"]: s for s in p6["steps"]}
        check("no dataset yet: the check waits for the export, training is blocked on the 50-sample floor",
              s6["check"]["state"] == "after" and s6["train"]["state"] == "blocked" and "50" in s6["train"]["why"])
        check("no measurements: times are labelled estimate / unknown, never measured",
              "estimate" in s6["train"]["needs"]["evidence"] and s6["eval"]["needs"]["minutes"] is None
              and "measured" not in s6["eval"]["needs"]["evidence"])

        # ------------------------------------------------------------------ best settings
        print("best_settings")
        hw = {"cpu_cores": 8, "fingerprint": "t2"}
        prof = {"models": {
            "fast-on-card:4b": {"ctx": 8192, "ctx_max_fast": 16384, "measured": {"gen_tok_s": 40.0, "gpu_pct": 100}},
            "spills:14b": {"ctx": 4096, "measured": {"gen_tok_s": 80.0, "gpu_pct": 60}},
            "slow-on-card:7b": {"ctx": 8192, "measured": {"gen_tok_s": 25.0, "gpu_pct": 100}, "sweep": {"num_thread": 6}}}}
        variants = [{"variant": {"kv": "q8_0", "draft": True}, "gen_tok_s": 70.0}, {"variant": {"kv": "q8_0", "draft": False}, "gen_tok_s": 300.0},
                    {"variant": {"kv": "f16", "draft": False}, "gen_tok_s": None, "error": "did not start"}]
        b = at.best_settings(profile=prof, plan={}, variants=variants, hw=hw)
        check("shape: worker, speedup, kv, threads each with value / why / evidence, plus lines and missing",
              all(all(k in b[x] for k in ("value", "why", "evidence")) for x in ("worker", "speedup", "kv", "threads"))
              and isinstance(b["lines"], list) and isinstance(b["missing"], list))
        check("worker: the fastest model that stayed fully on the card (not the faster one that spills)",
              b["worker"]["value"] == "fast-on-card:4b" and b["worker"]["ctx"] == 16384 and b["worker"]["evidence"] == "measured", b["worker"])
        check("speed-up: the measured variant wins (n-gram 300 vs draft 70); a failed variant is ignored",
              b["speedup"]["value"] == "ngram" and b["speedup"]["evidence"] == "measured" and "70" in b["speedup"]["why"], b["speedup"])
        check("threads: the measured sweep", b["threads"]["value"] == 6 and b["threads"]["evidence"] == "measured")
        check("everything measured: nothing listed as missing", b["missing"] == [], b["missing"])
        plan = {"processes": [{"tier": "worker", "name": "w:7b", "gguf": "C:/m/w-Q4_K_M.gguf", "ctx": 8192, "ngl": 29, "kv": "q8_0",
                               "full_gpu": True, "draft": None, "args": ["--spec-type", "ngram-cache", "--lookup-cache-dynamic", "x.bin"]}]}
        b2 = at.best_settings(profile={}, plan=plan, variants=[], hw=hw)
        check("no measurements: the planner's choice, labelled plan, with what to measure next",
              b2["worker"]["value"] == "w:7b" and b2["worker"]["evidence"] == "plan" and b2["worker"]["quant_file"] == "w-Q4_K_M.gguf"
              and b2["speedup"]["value"] == "ngram" and b2["speedup"]["evidence"] == "plan" and b2["kv"]["value"] == "q8_0"
              and len(b2["missing"]) == 2, b2)
        check("threads without a sweep follow the rule (one per performance core, router half)",
              b2["threads"]["evidence"] == "rule" and b2["threads"]["router"] >= 1)
        b3 = at.best_settings(profile={}, plan={}, variants=[], hw=hw)
        check("nothing set up: says so instead of inventing a worker", b3["worker"]["value"] is None and b3["worker"]["evidence"] == "none"
              and b3["speedup"]["evidence"] == "none" and any("port --apply" in m for m in b3["missing"]))

        print("port")
        check("measured variants and training speed stay on this machine (never packed)",
              "tune_variants.json" in arch_port.MACHINE_ONLY and "lora_out/train_stats.json" in arch_port.MACHINE_ONLY
              and "tune_variants.json" not in arch_port.PORTABLE)
    finally:
        if old_dd is None:
            os.environ.pop("ARCHITECT_DATA_DIR", None)
        else:
            os.environ["ARCHITECT_DATA_DIR"] = old_dd
        shutil.rmtree(home, ignore_errors=True)

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' TUNE2 CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' TUNE2 CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
