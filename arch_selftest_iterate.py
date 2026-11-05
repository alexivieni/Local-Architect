"""Self-test for the flow iterator (arch_iterate.py): diagnose from fixture traces, propose validated candidates with their evidence,
trial against a fake eval runner (one run after the other, the candidate as <flow>_vN), adopt only on no regression with a backup and an
undo. No model, no GPU, no network: the eval runner is a stand-in and every file lives in throwaway folders."""
import copy
import glob
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DATA = tempfile.mkdtemp(prefix="iter_data_")
os.environ["ARCHITECT_DATA_DIR"] = DATA            # the suite owns its data folder (CLAUDE.md)
os.environ["ARCHITECT_NO_CACHE"] = "1"

import arch_ci            # noqa: E402
import arch_dataflow      # noqa: E402
import arch_eval          # noqa: E402
import arch_iterate as it  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond or not detail else f"  -- {detail}"))


# ------------------------------------------------------------------------------------------------------ fixture traces
def write_run(root, name, records):
    d = os.path.join(root, ".arch", "runs", name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "trace.jsonl"), "w", encoding="utf-8") as f:
        for r in records:
            f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")
    return os.path.join(d, "trace.jsonl")


class Run:
    def __init__(self, flow="refactor"):
        self.flow, self.recs, self.cid = flow, [], 0

    def call(self, pt, gt):
        self.cid += 1
        self.recs.append({"type": "call", "id": self.cid, "flow": self.flow, "role": "generator", "prompt_tokens": pt, "gen_tokens": gt, "secs": 3.0})
        return self.cid

    def mark(self, cid, ok, reason=""):
        self.recs.append({"type": "mark", "id": cid, "accepted": ok, "reason": reason})

    def check(self, step, gate, ok, text=""):
        self.recs.append({"type": "check", "flow": self.flow, "step": step, "gate": gate, "ok": ok, "text": text})

    def step(self, sid, ok, secs=1.0, tries=None, unsure=False, kind="generate"):
        self.recs.append({"type": "step", "flow": self.flow, "step": sid, "kind": kind, "ok": ok, "secs": secs, "unsure": unsure, "tries": tries})

    def verdict(self, v, why="", tokens=60000, secs=300.0, calls=6):
        self.recs.append({"type": "verdict", "flow": self.flow, "verdict": v, "why": why,
                          "meter": {"tokens_in": tokens, "tokens_out": 0, "secs": secs, "calls": calls}})


NAME_ERR = "NameError: name 'helper' is not defined"


def refactor_runs(root):
    """Six runs of `refactor`: pin passes first time near its token budget (r1-r3) or fails all 3 tries (r4-r6); safety_net stops r4/r5
    (only where pin failed); change passes on its 2nd try in r1 and fails all 3 tries in r2, r3, r6 on a NameError."""
    paths = []
    for i in range(1, 7):
        r = Run()
        r.step("pick", True, kind="code")
        if i <= 3:
            c = r.call(50000, 8000)
            r.mark(c, True)
            r.step("pin", True, secs=200, tries=1)
        else:
            for _ in range(3):
                c = r.call(6000, 667)
                r.mark(c, False, "AssertionError: characterization test failed on line 12")
            r.step("pin", False, secs=400, tries=3)
        if i in (4, 5):
            r.check("safety_net", "characterization_pinned", False, "no characterization tests pass")
            r.step("safety_net", False, kind="code")
            r.verdict("fail", "step safety_net failed")
            paths.append(write_run(root, f"r{i}", r.recs))
            continue
        r.check("safety_net", "characterization_pinned", True)
        r.step("safety_net", True, kind="code")
        r.step("before", True, kind="code")
        if i == 1:
            c = r.call(9000, 1000)
            r.mark(c, False, NAME_ERR)
            c = r.call(9000, 1000)
            r.mark(c, True)
            r.check("change", "verify_files", True)
            r.step("change", True, secs=300, tries=2)
            r.step("after", None, unsure=True, kind="code")            # a gate that could not run
            r.verdict("pass")
        else:
            for t in range(3):
                c = r.call(9000, 1000)
                r.mark(c, False, NAME_ERR + f" (try {t + 1})")
                r.check("change", "verify_files", False, f"tests failed:\n  File x.py, line {40 + t}\n{NAME_ERR}")
            r.step("change", False, secs=900, tries=3)
            r.verdict("not_sure", "step change did not pass its checks")
        recs = r.recs
        if i == 1:
            recs = ["{this line is not json"] + recs
        paths.append(write_run(root, f"r{i}", recs))
    other = Run("bugfix")
    other.step("repro", False)
    other.verdict("fail", "step repro failed")
    paths.append(write_run(root, "other", other.recs))
    return paths


def write_eval_report(flow, results_):
    d = os.path.join(DATA, "evals")
    os.makedirs(d, exist_ok=True)
    rep = {"label": "live", "fake": False, "results": results_, "summary": arch_eval.summarize(results_)}
    with open(os.path.join(d, "20261104_010000_live.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f)


def eval_rows(flow, verdicts, tokens=1000, flow_ok=None):
    return [{"id": f"t{i}", "flow": flow, "verdict": v, "flow_ok": (flow_ok[i] if flow_ok else v == "pass"), "tokens_in": tokens, "tokens_out": 0,
             "secs": 1.0, "calls": 2, "detail": "" if v == "pass" else "hidden test failed"} for i, v in enumerate(verdicts)]


# ------------------------------------------------------------------------------------------------------ the checks
def test_yaml_roundtrip():
    files = sorted(glob.glob(os.path.join(arch_dataflow.FLOWS_DIR, "*.yaml")))
    bad = []
    for p in files:
        doc = arch_dataflow.load_yaml_file(p)
        try:
            text = it.to_yaml(doc)
        except ValueError as ex:
            bad.append(f"{os.path.basename(p)}: {ex}")
            continue
        mini = arch_ci._Mini(text).node(-1)
        if json.dumps(mini, sort_keys=True) != json.dumps(doc, sort_keys=True) or arch_dataflow.validate(mini) != arch_dataflow.validate(doc):
            bad.append(os.path.basename(p))
    check(f"every flows/*.yaml ({len(files)}) survives the YAML writer in both readers", files and not bad, "; ".join(bad[:3]))
    tricky = {"id": "x", "title": 'say "hi" # not a comment', "steps": [{"id": "a", "kind": "report", "template": "line one\nline {two}\n",
                                                                          "vars": {"q": 'a "quoted", b'}, "tags": ["on", "a:b", "1"]}]}
    back = arch_ci._Mini(it.to_yaml(tricky)).node(-1)
    check("quotes, colons, YAML words and multi-line text read back exactly", back == tricky, str(back)[:200])


def test_diagnose(root, paths):
    d = it.diagnose("refactor", traces=paths, evals=False)
    s = d["steps"]
    check("diagnose keeps only this flow's runs (and skips a broken line)", d["runs"] == 6, d["runs"])
    check("verdicts are counted three ways", d["verdicts"] == {"pass": 1, "fail": 2, "not_sure": 3}, d["verdicts"])
    check("change: failed 3 of 4, passed 1, tries up to 3", s["change"]["failed"] == 3 and s["change"]["ok"] == 1 and s["change"]["tries_max"] == 3,
          s["change"])
    check("calls are charged to the step they ran in", s["pin"]["tokens_p90"] == 58000 and s["change"]["calls_avg"] == 2.8 and s["pick"]["calls_avg"] == 0,
          (s["pin"]["tokens_p90"], s["change"]["calls_avg"]))
    check("a gate that could not run is 'unsure', not a failure", s["after"]["unsure"] == 1 and s["after"]["failed"] == 0, s["after"])
    check("gate counts per step", s["change"]["gates"]["verify_files"] == {"ok": 1, "fail": 9, "none": 0}, s["change"]["gates"])
    check("the typical failure text is folded and named", s["change"]["failures"] and "NameError" in s["change"]["failures"][0][0]
          and "N" in s["change"]["failures"][0][0], s["change"]["failures"])
    check("runs that did not pass are traced to the step that ended them", d["ended_by"] == {"safety_net": 2, "change": 3}, d["ended_by"])
    check("the top hotspot is the step that fails most", d["hotspots"] and d["hotspots"][0]["step"] == "change", d["hotspots"][:2])
    check("the summary is plain words with the numbers", "6 run(s)" in d["summary"] and "change" in d["summary"], d["summary"])
    d2 = it.diagnose("refactor", roots=[root], logbook=False, evals=False)
    check("traces are found under a project's .arch/runs", d2["runs"] == 6, d2["runs"])
    return d


def test_propose(diag):
    doc, _ = it.load_flow("refactor")
    frozen = json.dumps(doc, sort_keys=True)
    write_eval_report("refactor", eval_rows("refactor", ["fail", "fail", "fail", "pass", "pass"], flow_ok=[True, True, True, True, True]))
    diag = dict(diag, eval=it._eval_evidence("refactor"))
    p = it.propose("refactor", diag=diag, doc=doc)
    by = {c["id"]: c for c in p["candidates"]}
    check("the flow being read is never changed", json.dumps(doc, sort_keys=True) == frozen)
    check("more tries where a later try rescued a run", "retries:change" in by and by["retries:change"]["doc"]["steps"][4]["loop"]["max"] == 4,
          sorted(by))
    check("no extra tries where tries never rescued anything (pin)", "retries:pin" not in by)
    check("a larger model where tries never help (pin escalates)", "tier:pin" in by
          and it._step_ref(by["tier:pin"]["doc"], "pin")["loop"]["escalate"] == ["heavy"], sorted(by))
    check("no escalation added where it already exists (change)", "tier:change" not in by)
    check("a larger budget where the step ran into it (pin tokens 60000 -> 90000)", "budget:pin" in by
          and it._step_ref(by["budget:pin"]["doc"], "pin")["budget"]["tokens"] == 90000, sorted(by))
    sc = by.get("scout:change")
    check("a read-only scout before the step that fails on code it could not see", sc and sc["doc"]["steps"][4]["id"] == "scout"
          and sc["doc"]["steps"][4]["allow_writes"] is False and "{scout_brief}" in sc["doc"]["steps"][5]["context"]["query"], sorted(by))
    check("the flow budget is raised to hold the added step", sc and sc["doc"]["budget"]["tokens"] >= 210000, sc and sc["doc"]["budget"])
    jc = by.get("judge:change_judge")
    check("a judge after the last step when the flow passed work the judges failed", jc and "change_judge.ok == False" in jc["doc"]["verdict"]["pass_if"]
          and "3 of 5" in jc["evidence"], sorted(by))
    check("no step moved when its failures follow the step before it (safety_net)", not any(c["kind"] == "order" for c in p["candidates"]))
    check("every candidate carries evidence and a diff, and validates", p["candidates"] and all(c["evidence"] and c["diff"]
          and not arch_dataflow.validate(c["doc"]) and not it.check_candidate(c["doc"]) for c in p["candidates"]),
          [(c["id"], it.check_candidate(c["doc"])) for c in p["candidates"]])
    check("the diff names the change in plain words", any("loop.max: 3 -> 4" in x for x in by["retries:change"]["diff"]), by["retries:change"]["diff"])
    old = it.SCOUT_PROMPT
    it.SCOUT_PROMPT = "no_such_flow/no_such_prompt"
    try:
        p2 = it.propose("refactor", diag=diag, doc=doc)
    finally:
        it.SCOUT_PROMPT = old
    check("a candidate the validator refuses is listed as refused, never offered",
          "scout:change" not in {c["id"] for c in p2["candidates"]} and any(r["id"] == "scout:change" and "missing" in " ".join(r["problems"])
                                                                           for r in p2["refused"]), p2["refused"])
    few = dict(diag, runs=2, steps={k: dict(v, runs=2) for k, v in diag["steps"].items()}, eval=None)
    check("no candidate from fewer than three runs", not it.propose("refactor", diag=few, doc=doc)["candidates"])
    return by


def test_order(tmp):
    doc = {"id": "refactor", "title": "order fixture", "steps": [
        {"id": "gen", "kind": "generate", "role": "generator", "tier": "worker", "prompt": "characterization", "verify": {"gate": "new_tests_pass"}},
        {"id": "chk", "kind": "code", "gate": "scope_files", "on_fail": "stop"}], "verdict": {"pass_if": "gen.ok"}}
    paths = []
    for i in range(3):
        r = Run()
        r.call(20000, 2000)
        r.step("gen", True, secs=500, tries=1)
        r.step("chk", False, kind="code")
        r.verdict("fail", "step chk failed")
        paths.append(write_run(tmp, f"o{i}", r.recs))
    d = it.diagnose("refactor", traces=paths, evals=False)
    p = it.propose("refactor", diag=d, doc=doc)
    oc = [c for c in p["candidates"] if c["kind"] == "order"]
    check("a cheap stopping check that fails after the expensive step passed is moved first",
          oc and [s["id"] for s in oc[0]["doc"]["steps"]] == ["chk", "gen"] and any("step order" in x for x in oc[0]["diff"]), p["refused"] or oc)


def make_runner(base_verdicts, cand_verdicts, base_tok=1000, cand_tok=1000, seen=None):
    seen = seen if seen is not None else []

    def runner(run_id, doc, tasks, label):
        is_cand = run_id != "refactor"
        seen.append({"run_id": run_id, "doc": copy.deepcopy(doc), "n": len(tasks), "flows": {t["flow"] for t in tasks},
                     "file_there": os.path.isfile(it.flow_path(run_id, FLOWS_TMP)) if is_cand else None})
        vs = cand_verdicts if is_cand else base_verdicts
        rows = [{"id": t["id"], "flow": t["flow"], "verdict": vs[i], "flow_ok": vs[i] == "pass", "tokens_in": cand_tok if is_cand else base_tok,
                 "tokens_out": 0, "secs": 1.0, "calls": 2} for i, t in enumerate(tasks)]
        return {"label": label, "results": rows, "summary": arch_eval.summarize(rows)}
    return runner, seen


FLOWS_TMP = tempfile.mkdtemp(prefix="iter_flows_")


def test_trial_adopt(cands):
    src = arch_dataflow.load_yaml_file(it.flow_path("refactor"))
    orig_text = open(it.flow_path("refactor"), encoding="utf-8").read()
    with open(it.flow_path("refactor", FLOWS_TMP), "w", encoding="utf-8") as f:
        f.write(orig_text)
    doc, base_sha = it.load_flow("refactor", FLOWS_TMP)
    cand = dict(cands["retries:change"], base_sha=base_sha)
    tasks = [{"id": f"t{i}", "flow": "refactor", "kind": "flow", "goal": "x"} for i in range(5)] + [{"id": "route1", "flow": "refactor", "kind": "route", "goal": "x"}]
    quiet = lambda m: None   # noqa: E731

    runner, seen = make_runner(["pass", "pass", "pass", "fail", "fail"], ["pass", "pass", "pass", "pass", "fail"])
    r = it.trial("refactor", cand, runner=runner, tasks=tasks, flows_dir=FLOWS_TMP, say=quiet)
    check("trial runs the current flow, then the candidate, one after the other", [x["run_id"] for x in seen] == ["refactor", "refactor_v2"], seen)
    check("only flow tasks are run, the candidate's under its new id", seen[0]["n"] == 5 and seen[1]["flows"] == {"refactor_v2"}, seen)
    td = seen[1]["doc"]
    check("the candidate is written as flows/refactor_v2.yaml while it runs", seen[1]["file_there"] is True)
    check("the trial copy points at the original prompts and says what it is a trial of",
          td["id"] == "refactor_v2" and td["trial_of"] == "refactor" and all(s["prompt"].startswith("refactor/") for s in td["steps"] if s.get("prompt")),
          [s.get("prompt") for s in td["steps"]])
    check("the trial file is removed afterwards", not os.path.exists(it.flow_path("refactor_v2", FLOWS_TMP)))
    check("a better pass rate reads 'better' and the compare lines up under the original id",
          r["verdict"] == "better" and r["ok"] and [row["flow"] for row in r["compare"]["rows"]] == ["refactor"], (r.get("verdict"), r.get("why")))
    check("task by task changes are kept", r["changes"]["won"] == ["t3"] and not r["changes"]["lost"], r.get("changes"))
    check("a trial changes nothing by itself", open(it.flow_path("refactor", FLOWS_TMP), encoding="utf-8").read() == orig_text)
    check("the trial is saved for adopt", it.load_trial("refactor_v2") and it.load_trial("refactor_v2")["doc"]["steps"][4]["loop"]["max"] == 4)

    runner2, seen2 = make_runner(["pass", "pass", "pass", "pass", "fail"], ["pass", "fail", "fail", "fail", "fail"])
    worse = it.trial("refactor", cand, runner=runner2, tasks=tasks, flows_dir=FLOWS_TMP, say=quiet)
    check("the next trial gets the next id", seen2[1]["run_id"] == "refactor_v3", seen2)
    check("a pass-rate drop reads 'worse' with the regression named", worse["verdict"] == "worse" and not worse["ok"] and "pass rate" in worse["why"],
          worse.get("why"))
    a = it.adopt(worse, flows_dir=FLOWS_TMP, say=quiet)
    check("a regression is never adopted", not a["ok"] and open(it.flow_path("refactor", FLOWS_TMP), encoding="utf-8").read() == orig_text, a)

    runner3, seen3 = make_runner([], [])
    none = it.trial("refactor", cand, runner=runner3, tasks=[], flows_dir=FLOWS_TMP, say=quiet)
    check("no eval tasks: not_sure, nothing run, nothing adoptable", none["verdict"] == "not_sure" and not seen3
          and not it.adopt(none, flows_dir=FLOWS_TMP, say=quiet)["ok"], none.get("why"))

    runner4, _ = make_runner(["pass"] * 5, ["pass"] * 5, base_tok=1000, cand_tok=3000)
    tok = it.trial("refactor", cand, runner=runner4, tasks=tasks, flows_dir=FLOWS_TMP, say=quiet)
    check("tokens tripled at the same pass rate is a regression", tok["verdict"] == "worse" and "tokens" in tok["why"], tok.get("why"))
    runner5, _ = make_runner(["pass"] * 5, ["pass"] * 5, base_tok=1000, cand_tok=700)
    cheap = it.trial("refactor", cand, runner=runner5, tasks=tasks, flows_dir=FLOWS_TMP, say=quiet)
    check("the same pass rate for 30% fewer tokens reads 'better'", cheap["verdict"] == "better", cheap.get("why"))

    stale = dict(cand, base_sha="0" * 16)
    st = it.trial("refactor", stale, runner=runner, tasks=tasks, flows_dir=FLOWS_TMP, say=quiet)
    check("a candidate made from an older file is not trialled", st["verdict"] == "not_sure" and "changed since" in st["why"], st)

    ad = it.adopt(r["trial_id"], flows_dir=FLOWS_TMP, say=quiet)
    now = arch_dataflow.load_yaml_file(it.flow_path("refactor", FLOWS_TMP))
    check("adopt on no regression writes the candidate", ad["ok"] and now["steps"][4]["loop"]["max"] == 4 and not arch_dataflow.validate(now), ad)
    check("the old file is kept as a backup", ad.get("backup") and open(ad["backup"], encoding="utf-8").read() == orig_text)
    check("nothing else in the flow changed", json.dumps(dict(now, steps=None), sort_keys=True) == json.dumps(dict(src, steps=None), sort_keys=True)
          and [s["id"] for s in now["steps"]] == [s["id"] for s in src["steps"]])
    again = it.adopt(r["trial_id"], flows_dir=FLOWS_TMP, say=quiet)
    check("the same trial cannot be adopted over a file that has since changed", not again["ok"] and "changed" in again["why"], again)
    u = it.undo_adopt("refactor", flows_dir=FLOWS_TMP, say=quiet)
    check("undo puts the old file back byte for byte", u["ok"] and open(it.flow_path("refactor", FLOWS_TMP), encoding="utf-8").read() == orig_text, u)
    check("the real flows/refactor.yaml was never touched", open(it.flow_path("refactor"), encoding="utf-8").read() == orig_text)


def main():
    print("arch_iterate self-test")
    root = tempfile.mkdtemp(prefix="iter_proj_")
    tmp = tempfile.mkdtemp(prefix="iter_order_")
    try:
        test_yaml_roundtrip()
        paths = refactor_runs(root)
        diag = test_diagnose(root, paths)
        cands = test_propose(diag)
        test_order(tmp)
        if "retries:change" in cands:
            test_trial_adopt(cands)
        else:
            check("trial/adopt checks need the retries candidate", False)
    except Exception as ex:
        import traceback
        traceback.print_exc()
        check(f"the suite ran to the end ({type(ex).__name__}: {ex})", False)
    finally:
        for d in (root, tmp, FLOWS_TMP, DATA):
            shutil.rmtree(d, ignore_errors=True)
    n = sum(1 for _, ok in results if ok)
    print(f"\n  {'ALL ' + str(n) + ' ITERATE CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' ITERATE CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
