"""Gates as data: the code checks a flow step can name in its YAML (`verify: {gate: ...}` or `kind: code, gate: ...`), each a small function
that wraps a checker the harness already had. A gate answers {"ok": True | False | None, "text", "metrics", "evidence"}; `ok: None` means the
check could NOT run (no test runner, a crashing scanner), which the interpreter turns into the verdict "not sure", never into a pass.

    GATES[name](e, ctx, step, changed) -> result

`ctx` is the flow's working dict (goal, params, every step's output under its id, ctx["scan"] for before/after scanner counts);
`changed` is the list of files the step just applied (empty for pure code steps). Scan gates come in pairs: `<kind>_scan` records the
findings before a fix step, `<kind>_fewer` re-runs the same scanner and passes only when fewer findings remain and the checks pass.
"""
import os
import re

import arch_core as core

GATES = {}


def gate(name):
    def deco(fn):
        GATES[name] = fn
        return fn
    return deco


def R(ok, text="", metrics=None, evidence=""):
    return {"ok": ok, "text": text or "", "metrics": metrics or {}, "evidence": evidence or ""}


def _flows():
    import arch_flows
    return arch_flows


def _scope(e, ctx, step, exts=(".py",), n=4):
    """Files a step works on: explicit paths in the goal, else ctx['files'] from an earlier step, else the ranked project files."""
    import arch_flows_pro as pro
    exp = pro.explicit_files(e, ctx["goal"], exts)
    if exp:
        return exp[:n]
    if ctx.get("scope"):
        return list(ctx["scope"])[:n]
    return pro.scope_files(e, ctx["goal"], n, exts)


# ------------------------------------------------------------------------------------------------------ verification
@gate("verify_files")
def g_verify_files(e, ctx, step, changed):
    """Syntax/HTML/SQL/OpenAPI checks + fake-code + missing imports on the changed files, then the project's tests."""
    if not changed:
        return R(False, "nothing was applied")
    v = _flows().verify(e, changed, run_tests=step.get("run_tests", True))
    return R(v["ok"], v["text"], {"passed": v["passed"], "total": v["total"], "tests": v["tests"][0]}, (v["tests"][1] or "")[-1500:])


@gate("verify_nontest")
def g_verify_nontest(e, ctx, step, changed):
    if not changed:
        return R(False, "nothing was applied")
    v = _flows().verify(e, changed, run_tests=False)
    return R(v["ok"], v["text"], {"passed": v["passed"], "total": v["total"]})


@gate("tests_pass")
def g_tests_pass(e, ctx, step, changed):
    """The whole suite must pass. ok None when there is no runner at all."""
    import arch_flows_pro as pro
    if not pro.can_test(e):
        return R(None, "no test runner: install pytest or set a test command in Settings")
    st, log = core.run_tests(e.root, e.cfg)
    if st == "none":
        return R(step.get("allow_none", False) or None, "no tests ran", {"tests": st}, log[-800:])
    return R(st == "pass", "" if st == "pass" else "tests fail:\n" + log[-1400:], {"tests": st}, log[-1500:])


@gate("tests_green_before")
def g_tests_green_before(e, ctx, step, changed):
    """A precondition: the existing tests must pass before the flow changes anything (bugfix is the flow for red tests)."""
    import arch_flows_pro as pro
    if not pro.can_test(e):
        return R(None, "no test runner: install pytest or set a test command in Settings")
    st, log = core.run_tests(e.root, e.cfg)
    ctx["tests_before"] = st
    ctx["existing_tests"] = [f for f in core.list_files(e.root, e.cfg) if f.startswith("tests/") or "/tests/" in f]   # a step that writes NEW tests may not edit these
    return R(st != "fail", "" if st != "fail" else "the existing tests already fail; fix them first (bugfix flow):\n" + log[-600:], {"tests": st})


@gate("lint")
def g_lint(e, ctx, step, changed):
    """pyflakes on the changed Python files when it is installed; otherwise the harness's own static scan (high findings only). Advisory by default."""
    files = [f for f in (changed or []) if f.endswith(".py")]
    if not files:
        return R(True, "")
    probs = []
    if core.has_module("pyflakes"):
        import subprocess
        import sys
        for f in files:
            rc, out = core.run_cmd([sys.executable, "-m", "pyflakes", f], e.root, 60, True)
            if rc not in (0, 127) and out.strip():
                probs.append(out.strip()[-600:])
        if not probs and rc == 127:
            return R(True, "pyflakes could not start; skipped")
    else:
        import arch_quality as q
        probs = [f"{x['file']}:{x['line']} {x['msg']}" for x in q.static_scan(e.root, files) if x.get("severity") == "high"]
    ok = not probs if step.get("strict") else True
    return R(ok, ("lint:\n" + "\n".join(probs)) if probs else "", {"problems": len(probs)})


@gate("repro_fails_right_reason")
def g_repro(e, ctx, step, changed):
    """The reproduction test must fail by assertion on today's code (not by import/syntax error) -- then it is the bug's proof."""
    new = [x for x in (changed or []) if x.startswith("tests/")]
    if not new:
        return R(False, "no test file was written under tests/")
    rel = new[0]
    st, log = core.run_tests(e.root, e.cfg, [rel])
    wrong = re.search(r"SyntaxError|ModuleNotFoundError|ImportError|NameError|IndentationError", log)
    if st == "fail" and not wrong:
        ctx["repro_path"], ctx["repro_failure"] = rel, log[-1500:]      # the step's own output overwrites ctx["repro"], so the proof lives under its own keys
        return R(True, "", {"repro": rel}, log[-800:])
    why = "it passes already (cannot reproduce)" if st == "pass" else "it fails for the wrong reason:\n" + log[-500:]
    return R(False, why, {"repro": rel})


@gate("e2e_pass")
def g_e2e(e, ctx, step, changed):
    new = [x for x in (changed or []) if x.startswith("tests/")]
    if not new:
        return R(False, "no new test file was written")
    st, log = core.run_tests(e.root, e.cfg)
    if st != "pass":
        for f in new:                                              # a red end-to-end test must not stay behind
            try:
                os.remove(os.path.join(e.root, f))
            except OSError:
                pass
    return R(st == "pass", "" if st == "pass" else "the end-to-end test (or the feature) fails:\n" + log[-1400:], {"tests": st})


@gate("new_tests_pass")
def g_new_tests_pass(e, ctx, step, changed):
    """New test files must pass on the current code (a test that fails today is wrong, or another flow's job); failing new files are removed."""
    new = [x for x in (changed or []) if x.startswith("tests/")]
    if not new:
        return R(False, "no test file was written")
    st, log = core.run_tests(e.root, e.cfg)
    if st != "pass":
        for f in new:
            try:
                os.remove(os.path.join(e.root, f))
            except OSError:
                pass
        return R(False, "your new tests FAIL on the current code; a test that fails on today's code is wrong (or exposes a bug, which another flow handles):\n" + log[-1400:])
    return R(True, "", {"new": new})


# ------------------------------------------------------------------------------------------------------ scanners: before / fewer
def _scan(kind, e, ctx, files):
    import arch_security as sec
    import arch_fakecode as fc
    if kind == "sec":
        return sec.sec_scan(e.root, files, include_low=False)
    if kind == "config":
        return sec.scan_config_files(e.root, files)
    if kind == "fake":
        import arch_flows_fake as ff
        return ff._targets(fc.scan(e.root, files))
    if kind == "cidr":
        out = []
        for f in files:
            text = core.read_text(os.path.join(e.root, f), 200000)[0]
            probs = sec.check_cidr_plan(text, f) if '"subnets"' in text else sec.check_firewall(text, f)
            out += [dict(p, file=f) for p in probs]
        return out
    raise ValueError(kind)


def _scan_files(kind, e, ctx):
    import arch_flows_sec as fs
    import arch_flows_pro as pro
    if kind == "sec":
        return fs._sec_files(e, ctx["goal"])
    if kind == "config":
        return fs._config_files(e)
    if kind == "fake":
        return pro.explicit_files(e, ctx["goal"], (".py",)) or [f for f in core.list_files(e.root, e.cfg) if f.endswith(".py")]
    if kind == "cidr":
        return pro.explicit_files(e, ctx["goal"], (".json",)) or fs.NetworkFlow()._candidates(e)
    raise ValueError(kind)


def _fmt(kind, x):
    if kind == "sec":
        return f"- {x['file']}:{x['line']} [{x['severity']}] {x['rule']} ({x.get('cwe', '')}): {x['msg']}"
    if kind == "cidr":
        return f"- {x.get('file', '')}: [{x['severity']}] {x['rule']}: {x['msg']}"
    return f"- {x['file']}:{x['line']} [{x['severity']}] {x['rule']}: {x['msg']}"


def _scan_gate(kind):
    def before(e, ctx, step, changed):
        try:
            files = _scan_files(kind, e, ctx)
            if not files:
                return R(False, "no files in scope for this scan")
            finds = _scan(kind, e, ctx, files)
        except Exception as ex:
            return R(None, f"the {kind} scanner could not run: {type(ex).__name__}: {ex}")
        ctx.setdefault("scan", {})[kind] = {"files": files, "before": finds, "after": finds}
        ctx["scope"] = sorted({x.get("file") for x in finds if x.get("file")}) or files
        ctx["findings_text"] = "\n".join(_fmt(kind, x) for x in finds[:25])
        if kind == "fake":
            import arch_flows_fake as ff
            ctx["scan"][kind]["tests_before"] = ff.real_tests(e.root, files)
        e.say(f"[scan] {len(finds)} {kind} finding(s) in {len(files)} file(s)")
        e.report(f"- Scanned ({kind}): {len(files)} file(s); findings: {len(finds)}", *[("  " + _fmt(kind, x)) for x in finds[:12]], "")
        return R(True, "", {"findings": len(finds), "files": len(files)}, ctx["findings_text"])

    def fewer(e, ctx, step, changed):
        rec = (ctx.get("scan") or {}).get(kind)
        if not rec:
            return R(None, f"no {kind} scan was recorded before this step")
        v = _flows().verify(e, changed or [], run_tests=step.get("run_tests", True)) if changed else {"ok": False, "text": "nothing was applied", "passed": 0, "total": 0}
        try:
            after = _scan(kind, e, ctx, rec["files"])
        except Exception as ex:
            return R(None, f"the {kind} scanner could not run: {ex}")
        rec["after"] = after
        gone = len(rec["before"]) - len(after)
        lost = 0
        if kind == "fake":
            import arch_flows_fake as ff
            lost = rec["tests_before"] - ff.real_tests(e.root, rec["files"])
        good = v["ok"] and gone > 0 and lost <= 0
        why = v["text"] or (f"{lost} real test(s) were lost: do not delete tests that check something" if lost > 0 else f"{gone} finding(s) gone, {len(after)} remain")
        return R(good, "" if good else why, {"before": len(rec["before"]), "after": len(after), "lost": lost}, "\n".join(_fmt(kind, x) for x in after[:12]))
    return before, fewer


for _k in ("sec", "config", "fake", "cidr"):
    _b, _f = _scan_gate(_k)
    GATES[f"{_k}_scan"] = _b
    GATES[f"{_k}_fewer"] = _f


@gate("ci_check")
def g_ci_check(e, ctx, step, changed):
    import arch_ci as ci
    out = ci.check_all(e.root)
    blocking = [f for f in out["findings"] if f["gate"]]
    ctx.setdefault("scan", {})["ci"] = {"before": blocking, "after": blocking, "pipelines": out["pipelines"]}
    ctx["scope"] = sorted({f["file"] for f in out["findings"]} | set(out["pipelines"]))
    ctx["findings_text"] = "\n".join(f"- {f['file']}:{f['line']} [{f['severity']}] {f['rule']}: {f['msg']}" for f in out["findings"][:25])
    e.report(f"- Pipelines: {', '.join(out['pipelines']) or 'none'}; blocking findings: {len(blocking)}", "")
    return R(True, "", {"blocking": len(blocking), "pipelines": len(out["pipelines"])}, ctx["findings_text"])


@gate("ci_no_blocking")
def g_ci_no_blocking(e, ctx, step, changed):
    import arch_ci as ci
    v = _flows().verify(e, changed or [], run_tests=False) if changed else {"ok": False, "text": "nothing was applied"}
    out = ci.check_all(e.root)
    blocking = [f for f in out["findings"] if f["gate"]]
    (ctx.setdefault("scan", {}).setdefault("ci", {}))["after"] = blocking
    ok = v["ok"] and not blocking
    return R(ok, "" if ok else (v["text"] or "blocking findings remain:\n" + "\n".join(f"- {f['file']}: {f['rule']}: {f['msg']}" for f in blocking[:8])), {"blocking": len(blocking)})


# ------------------------------------------------------------------------------------------------------ documents
@gate("spec_checks")
def g_spec(e, ctx, step, changed):
    import arch_checks as ck
    p = os.path.join(e.root, "docs", "spec.md")
    if not os.path.isfile(p):
        return R(False, "docs/spec.md was not written")
    s = ck.summarize(ck.check_spec(core.read_text(p, 60000)[0]))
    return R(s["ok"], s["failures"], {"passed": s["passed"], "total": s["total"]})


@gate("design_structure")
def g_design(e, ctx, step, changed):
    written = list(changed or [])
    if not written:
        return R(False, "no DOC: files were found; use the exact 'DOC: path' format")
    v = _flows().verify(e, written, run_tests=False)
    text = v["text"]
    ok = v["ok"]
    if not any(w.endswith("architecture.md") for w in written):
        ok, text = False, text + "\n- docs/architecture.md is missing"
    if not any("/adr/" in w for w in written):
        ok, text = False, text + "\n- no ADR file under docs/adr/"
    return R(ok, text.strip(), {"files": len(written)}, "\n".join(f"=== {w} ===\n{core.read_text(os.path.join(e.root, w), 12000)[0]}" for w in written if os.path.isfile(os.path.join(e.root, w))))


@gate("hunt_confirm")
def g_hunt_confirm(e, ctx, step, changed):
    """Each tests/hunt/*.py the hunter wrote must fail by assertion on today's code; the others are deleted. Never fails the step: zero confirmed is information."""
    tests = [x for x in (changed or []) if x.startswith("tests/hunt/")]
    confirmed, rejected = [], 0
    for t in tests:
        full = os.path.join(e.root, t)
        doc = re.search(r"BUG:\s*(.+?)(?:\"\"\"|'''|\n)", core.read_text(full, 4000)[0])
        title = (doc.group(1).strip() if doc else t)[:200]
        st, log = core.run_tests(e.root, e.cfg, [t])
        wrong = re.search(r"SyntaxError|ModuleNotFoundError|ImportError|NameError|IndentationError|fixture .* not found", log)
        if st == "fail" and not wrong:
            confirmed.append({"test": t, "title": title, "evidence": log[-500:]})
            e.say(f"[CONFIRMED] {title}")
        else:
            rejected += 1
            e.say(f"[discarded] {title}  ({'passes: not a bug' if st == 'pass' else 'invalid test'})")
            try:
                os.remove(full)
                if t in e.created:
                    e.created.remove(t)
                if t in e.changed_files:
                    e.changed_files.remove(t)
            except OSError:
                pass
    ctx["confirmed"], ctx["rejected"] = confirmed, rejected
    ctx["confirmed_text"] = "\n".join(f"### {c['title']}\n- Proving test: `{c['test']}` (fails today)\n- Evidence:\n```\n{c['evidence'].strip()[-400:]}\n```\n" for c in confirmed) \
        or "None proven in this pass. That is not proof the code is bug-free.\n"
    return R(True, "", {"confirmed": len(confirmed), "rejected": rejected})


@gate("metrics_before")
def g_metrics_before(e, ctx, step, changed):
    import arch_checks as ck
    files = _scope(e, ctx, step)
    ctx["scope"] = files
    ctx["metrics_before"] = ck.metrics_python(e.root, files)
    e.report(f"- In scope: {', '.join(files)}", f"- Structure before: {ctx['metrics_before']}")
    return R(bool(files), "" if files else "no files in scope; name the files in the goal", ctx["metrics_before"])


@gate("metrics_after")
def g_metrics_after(e, ctx, step, changed):
    import arch_checks as ck
    files = ctx.get("scope") or _scope(e, ctx, step)
    ctx["metrics_after"] = ck.metrics_python(e.root, files)
    e.report(f"- Structure after: {ctx['metrics_after']}")
    return R(True, "", ctx["metrics_after"])


@gate("static_facts")
def g_static_facts(e, ctx, step, changed):
    """Facts from tools for a review: static patterns + size/complexity metrics, into ctx['facts']."""
    import arch_checks as ck
    import arch_quality as q
    files = _scope(e, ctx, step, n=int(step.get("n", 6)))
    if not files:
        return R(False, "no Python files to review")
    ctx["scope"] = files
    scan = q.static_scan(e.root, files)
    met = ck.metrics_python(e.root, files)
    ctx["facts"] = f"metrics: {met}\n" + ("\n".join(f"- {x['file']}:{x['line']} [{x['severity']}] {x['rule']}: {x['msg']}" for x in scan) if scan else f"metrics: {met}\n- no static findings")
    ctx["static"] = scan
    ctx["existing_tests"] = [f for f in core.list_files(e.root, e.cfg) if f.startswith("tests/") or "/tests/" in f]   # a step that writes NEW tests may not edit these
    ctx["hints"] = ("STATIC FINDINGS (hints; may be false alarms):\n" + "\n".join(f"- {x['file']}:{x['line']} [{x['rule']}] {x['msg']}" for x in scan[:12]) + "\n\n") if scan else ""
    e.say(f"[facts] {len(scan)} static finding(s); {met}")
    return R(True, "", {"findings": len(scan)}, ctx["facts"])


@gate("scope_files")
def g_scope_files(e, ctx, step, changed):
    exts = tuple(step.get("exts") or [".py"])
    files = _scope(e, ctx, step, exts, int(step.get("n", 4)))
    ctx["scope"] = files
    if files:
        e.say(f"[scope] {', '.join(files)}")
        e.report(f"- In scope: {', '.join(files)}", "")
    return R(bool(files), "" if files else "no files in scope; name the files in the goal", {"files": len(files)})


@gate("suspect_files")
def g_suspect_files(e, ctx, step, changed):
    """bugfix: files named in a traceback first, then the ranked ones."""
    fl = _flows()
    tb = fl.traceback_files(e, ctx["goal"])
    files = core.list_files(e.root, e.cfg)
    ranked = [f for f, s in core.rank_files(e.root, e.cfg, ctx["goal"], files) if not f.startswith(("tests/", "docs/", "bench/")) and not f.endswith((".md", ".txt", ".json"))][:6]
    focus = tb + [f for f in ranked if f not in tb]
    ctx["scope"] = focus
    e.say(f"[locate] suspect files: {', '.join(focus[:5]) or '(none found)'}")
    e.report(f"- Suspect files: {', '.join(focus[:6])}", "")
    return R(True, "", {"files": len(focus)})


@gate("mutation_before")
def g_mutation_before(e, ctx, step, changed):
    """Break the code on purpose and see what the tests catch. Off when behavior.mutation = off; 'could not run' is not a pass."""
    import arch_quality as q
    files = ctx.get("scope") or _scope(e, ctx, step)
    ctx["scope"] = files
    if files:
        import arch_flows_pro as pro
        stem = pro.slug(os.path.splitext(os.path.basename(files[0]))[0])
        ctx.setdefault("test_target", f"tests/test_{stem}_extra.py")
        ctx.setdefault("gaps_target", f"tests/test_{stem}_gaps.py")
    if e.cfg["behavior"].get("mutation", "on") == "off":
        ctx["mutation_before"] = None
        return R(True, "mutation check is off")
    m = q.mutation_check(e.root, e.cfg, files, int(e.cfg["behavior"].get("mutation_max", 8)), log=e.say)
    ctx["mutation_before"] = m
    if not m.get("ok"):
        return R(None, "mutation check could not run: " + str(m.get("error"))[:160])
    e.report(f"- Mutation score before: {m['killed']}/{m['total']} ({m['score']}%)", *[f"  - missed: `{x['file']}` {x['where']}" for x in m["survived"][:10]])
    ctx["survivors"] = _survivor_block(m["survived"])
    return R(True, "", {"score": m["score"], "killed": m["killed"], "total": m["total"]})


def _survivor_block(survived):
    """The 'these changes went unnoticed' block the test prompt expects, from prompts/test/_survivors.md; '' when nothing survived."""
    if not survived:
        return ""
    items = "\n".join(f"- {x['file']} {x['where']}" for x in survived)
    try:
        import arch_dataflow
        with open(os.path.join(arch_dataflow.PROMPTS_DIR, "test", "_survivors.md"), encoding="utf-8") as f:
            return f.read().replace("{items}", items)
    except OSError:
        return "These small code changes were made on purpose and NO test noticed. Write tests that would fail for each:\n" + items + "\n\n"


@gate("mutation_after")
def g_mutation_after(e, ctx, step, changed):
    import arch_quality as q
    if e.cfg["behavior"].get("mutation", "on") == "off" or not ctx.get("mutation_before"):
        return R(True, "")
    files = ctx.get("scope") or _scope(e, ctx, step)
    m = q.mutation_check(e.root, e.cfg, files, int(e.cfg["behavior"].get("mutation_max", 8)), log=e.say)
    ctx["mutation_after"] = m
    if not m.get("ok"):
        return R(None, "mutation check could not run: " + str(m.get("error"))[:160])
    before = ctx["mutation_before"]
    ctx["survivors"] = _survivor_block(m["survived"])
    e.report(f"- Mutation score after: {m['killed']}/{m['total']} ({m['score']}%)", *[f"  - still missed: `{x['file']}` {x['where']}" for x in m["survived"][:10]])
    return R(m["score"] >= (before.get("score") or 0), "" if m["score"] >= (before.get("score") or 0) else f"the tests did not get better: {before['score']}% -> {m['score']}%",
             {"before": before.get("score"), "after": m["score"]})


@gate("bench_baseline")
def g_bench_baseline(e, ctx, step, changed):
    """Find or accept a benchmark and measure the baseline. ctx['bench'], ctx['baseline'], ctx['bench_detail']."""
    import arch_quality as q
    import arch_flows_pro as pro
    files = ctx.get("scope") or _scope(e, ctx, step, n=3)
    ctx["scope"] = files
    bench = ctx.get("bench") or next((x for x in pro.explicit_files(e, ctx["goal"]) if x.startswith("bench/")), None) \
        or next((f for f in core.list_files(e.root, e.cfg) if f.startswith("bench/") and f.endswith(".py")), None)
    if changed:
        made = [x for x in changed if x.startswith("bench/")]
        bench = made[0] if made else bench
    if not bench:
        ctx["bench_target"] = f"bench/bench_{pro.slug(os.path.splitext(os.path.basename(files[0]))[0])}.py" if files else "bench/bench_main.py"
        return R(False, "no benchmark yet", {"target": ctx["bench_target"]})
    t, detail = q.run_bench(e.root, e.cfg, bench, int(e.cfg["behavior"].get("bench_runs", 5)))
    if t is None:
        return R(False, "the benchmark does not work: " + detail, {"bench": bench})
    import arch_quality as _q
    scan = _q.static_scan(e.root, files)
    ctx["evidence"] = ("STATIC FINDINGS (hints):\n" + "\n".join(f"- {x['file']}:{x['line']} {x['msg']}" for x in scan[:8]) + "\n\n") if scan else ""
    ctx.update(bench=bench, baseline=t, best=t, bench_detail=detail)
    e.say(f"[bench] baseline {t:.5f}s ({detail})")
    e.report(f"- Benchmark: `{bench}`", f"- Baseline: {t:.5f}s per run ({detail})")
    return R(True, "", {"bench": bench, "baseline": round(t, 5)})


@gate("bench_faster")
def g_bench_faster(e, ctx, step, changed):
    """Keep a change only if the checks pass AND the benchmark improves by behavior.min_gain_pct; otherwise the caller rolls it back."""
    import arch_quality as q
    v = _flows().verify(e, changed or [], run_tests=True) if changed else {"ok": False, "text": "nothing was applied"}
    if not v["ok"]:
        return R(False, v["text"])
    t, detail = q.run_bench(e.root, e.cfg, ctx["bench"], int(e.cfg["behavior"].get("bench_runs", 5)))
    if t is None:
        return R(None, "the benchmark stopped working: " + detail)
    need = float(e.cfg["behavior"].get("min_gain_pct", 10)) / 100.0
    base = ctx.get("best") or ctx["baseline"]
    gain = (base - t) / base if base else 0.0
    e.say(f"[bench] {base:.5f}s -> {t:.5f}s ({gain * 100:+.0f}%)")
    if gain >= need:
        ctx["best"], ctx["kept"] = t, True
        return R(True, "", {"before": round(base, 5), "after": round(t, 5), "gain_pct": round(gain * 100, 1)})
    return R(False, f"only {gain * 100:+.0f}% (needs {need * 100:.0f}%): rolled back", {"after": round(t, 5), "gain_pct": round(gain * 100, 1)})


@gate("sql_plans_before")
def g_sql_before(e, ctx, step, changed):
    import arch_quality as q
    allsql = [f for f in core.list_files(e.root, e.cfg) if f.endswith(".sql")]
    plans = q.sql_plans(e.root, allsql)
    scans = [p for p in plans if p["scan"]]
    ctx.update(sql_files=allsql, plans=plans, scans_before=len(scans))
    ctx["plans_text"] = "\n".join(f"- {p['query']}: {p['plan']}" + ("   <-- SCAN" if p["scan"] else "") for p in plans)
    ctx["scope"] = allsql
    e.say(f"[plans] {len(plans)} queries, {len(scans)} with a full scan")
    e.report(f"- Queries: {len(plans)}; full scans before: {len(scans)}", *[f"  - {p['query']}: {p['plan']}" for p in plans[:10]])
    if not plans:
        return R(None, "no named queries found (use '-- name: x' above each query in a .sql file)")
    return R(True, "", {"queries": len(plans), "scans": len(scans)})


@gate("sql_fewer_scans")
def g_sql_fewer(e, ctx, step, changed):
    import arch_quality as q
    v = _flows().verify(e, changed or []) if changed else {"ok": False, "text": "nothing could be applied"}
    new = q.sql_plans(e.root, [f for f in core.list_files(e.root, e.cfg) if f.endswith(".sql")])
    now = [p for p in new if p["scan"]]
    ok = v["ok"] and len(now) < ctx.get("scans_before", 0)
    if ok:
        e.report(f"- Full scans after: {len(now)}", *[f"  - {p['query']}: {p['plan']}" for p in new[:10]])
    return R(ok, "" if ok else (v.get("text") or f"full scans were {ctx.get('scans_before')}, now {len(now)}: not improved"), {"before": ctx.get("scans_before"), "after": len(now)})


@gate("characterization_pinned")
def g_characterization(e, ctx, step, changed):
    """A refactor needs a safety net: the characterization tests (or the project's own) must pass on the UNMODIFIED code."""
    import arch_flows_pro as pro
    if not pro.can_test(e):
        return R(None, "no test runner: install pytest or set a test command. Refusing to refactor without a safety net.")
    st, log = core.run_tests(e.root, e.cfg)
    if st == "fail":
        return R(False, "the tests fail on the unmodified code, so they do not describe current behaviour:\n" + log[-1200:])
    ctx["pinned"] = True
    e.say("[+] current behaviour is pinned by passing tests")
    return R(True, "", {"tests": st})


@gate("template_render")
def g_template(e, ctx, step, changed):
    """`new`: choose a template (code, no model needed), write the files that do not exist, and run its own tests."""
    import arch_templates as T
    params = ctx.get("params") or {}
    name = params.get("name") or os.path.basename(e.root.rstrip("/\\")) or "app"
    tid = params.get("template") or ctx.get("template_pick")
    why = "chosen by you" if params.get("template") else "chosen by the router model" if ctx.get("template_pick") else ""
    if not tid or tid == "auto" or tid not in T.TEMPLATES:
        tid, why = T.suggest(ctx["goal"])
    addons = [a for a in (params.get("addons") or []) if a in T.ADDONS]
    files = T.render(tid, name, addons)
    todo = [p for p in sorted(files) if not os.path.exists(os.path.join(e.root, p))]
    tpl = T.TEMPLATES[tid]
    e.say(f"[template] {tpl['title']}  ({why})")
    e.report(f"- Template: `{tid}` ({why}); addons: {', '.join(addons) or 'none'}", f"- Files to create: {len(todo)}", "")
    if e.review == "dry":
        return R(True, "", {"template": tid, "files": len(todo)})
    if todo and e.review == "ask":
        a = (e.ask(f"  Create these {len(todo)} files in {e.root}?  [y]es  [n]o > ") or "").strip().lower()
        if a not in ("y", "yes", "a", "all"):
            return R(False, "not approved")
    os.makedirs(e.root, exist_ok=True)
    written = [p for p in todo if e.put_file(p, files[p], "template file", quiet=True)["status"] == "applied"]
    v = _flows().verify(e, [w for w in written if not w.startswith("tests/")], run_tests=True)
    ctx.update(template=tid, template_files=written, template_run=T.rendered_run(tid, name), domains=tpl["domains"])
    if tpl["needs"] and not all(core.has_module(m) for m in tpl["needs"]):
        e.say(f"[i] this template needs: pip install -r requirements.txt ({', '.join(tpl['needs'])}); its tests are skipped until then")
        return R(True, "", {"template": tid, "files": len(written), "needs": tpl["needs"]})
    return R(v["ok"], v["text"], {"template": tid, "files": len(written), "passed": v["passed"], "total": v["total"]})


@gate("scope_checked")
def g_scope_checked(e, ctx, step, changed):
    """pentest: load findings.json and scope.json and refuse anything outside the authorized scope, in code, before any model call."""
    import arch_pentest as pt
    params = ctx.get("params") or {}
    scope, is_default, err = pt.load_scope(e.root, params.get("scope_file") or "scope.json")
    if err:
        return R(False, err)
    if params.get("live") and is_default:
        return R(False, "live: true needs a real scope.json naming authorized targets and a window; refused")
    findings, ferr = pt.load_findings(e.root, params.get("findings_file") or "findings.json")
    if ferr:
        return R(False, ferr)
    allowed, refused = [], []
    for f in findings:
        (allowed if pt.in_scope(f.get("host"), scope) else refused).append(f)
    ctx.update(pt_scope=scope, findings=allowed, refused=refused, is_default_scope=is_default)
    ctx["hosts"] = ", ".join(scope.get("authorized_targets") or [])
    ctx["findings_text"] = "\n".join(f"- {f.get('id')}: {f.get('host')}: {f.get('desc')}" for f in allowed)
    e.say(f"[scope] {len(allowed)} finding(s) in scope, {len(refused)} refused" + (" (default loopback/lab scope)" if is_default else ""))
    e.report(f"- In scope: {len(allowed)} finding(s); refused (out of scope): {len(refused)}", *[f"  - refused: {f.get('id')} ({f.get('host')})" for f in refused[:8]], "")
    return R(bool(allowed), "" if allowed else "no finding is inside the authorized scope", {"in_scope": len(allowed), "refused": len(refused)})


@gate("json_plan_fix")
def g_json_plan_fix(e, ctx, step, changed):
    """network: the reply is a whole JSON plan, not FILE blocks. Take the fenced JSON from ctx['reply'], write it to the file being fixed,
    and pass only when the same CIDR/firewall math reports fewer problems."""
    import json as _json
    import arch_security as sec
    rel = ctx.get("item") or (ctx.get("scope") or [None])[0]
    reply = ctx.get("reply") or ""
    if not rel:
        return R(False, "no plan file to fix")
    m = re.search(r"```(?:json)?\s*\n(.*?)```", reply, re.S) or re.search(r"(\{.*\})", reply, re.S)
    if not m:
        return R(False, "no JSON object in the reply; send the whole corrected file inside a ```json fence")
    candidate = m.group(1).strip()
    try:
        _json.loads(candidate)
    except ValueError as ex:
        return R(False, f"that is not valid JSON ({ex}); send the whole corrected file")
    before = ctx.get("problems_by_file", {}).get(rel)
    if before is None:
        text = core.read_text(os.path.join(e.root, rel), 200000)[0]
        before = len(sec.check_cidr_plan(text, rel) if '"subnets"' in text else sec.check_firewall(text, rel))
    now = sec.check_cidr_plan(candidate, rel) if '"subnets"' in candidate else sec.check_firewall(candidate, rel)
    if len(now) >= before:
        return R(False, f"the same math still reports {len(now)} problem(s) (was {before}):\n" + "\n".join(f"- {p['rule']}: {p['msg']}" for p in now[:6]))
    p = e.put_file(rel, candidate + ("\n" if not candidate.endswith("\n") else ""), "network fix", quiet=True)
    ctx.setdefault("problems_by_file", {})[rel] = len(now)
    e.report(f"- `{rel}`: problems {before} -> {len(now)}")
    return R(p["status"] in ("applied", "identical"), "", {"before": before, "after": len(now), "file": rel})


@gate("attackpath_valid")
def g_attackpath(e, ctx, step, changed):
    """pentest: the write-up must cite only findings you supplied and only hosts inside the authorized scope -- checked structurally, in code."""
    import json as _json
    import arch_pentest as pt
    reply = ctx.get("reply") or ""
    m = re.search(r"```(?:json)?\s*\n(.*?)```", reply, re.S) or re.search(r"(\[.*\])", reply, re.S)
    if not m:
        return R(False, "no JSON array of attack paths in the reply")
    try:
        paths = _json.loads(m.group(1).strip())
    except ValueError as ex:
        return R(False, f"that is not valid JSON ({ex})")
    if not isinstance(paths, list) or not paths:
        return R(False, "expected a non-empty JSON array of attack paths")
    ids = {str(f.get("id")) for f in (ctx.get("findings") or [])}
    bad = []
    for p in paths:
        for cited in (p.get("findings") or p.get("evidence") or []):
            if str(cited) not in ids:
                bad.append(f"cites '{cited}', which is not one of the findings you supplied")
        for host in (p.get("hosts") or ([p.get("host")] if p.get("host") else [])):
            if not pt.in_scope(host, ctx.get("pt_scope") or {}):
                bad.append(f"names '{host}', which is outside the authorized scope")
    ctx["attack_paths"] = paths
    return R(not bad, "; ".join(bad[:5]), {"paths": len(paths), "problems": len(bad)})


@gate("spec_write")
def g_spec_write(e, ctx, step, changed):
    """design: the spec came back as text; write it to docs/spec.md and check its structure."""
    import arch_checks as ck
    spec = (ctx.get("spec") or "").strip()
    if len(spec) < 80:
        return R(False, "the spec is empty or far too short")
    e.put_file("docs/spec.md", spec if spec.endswith("\n") else spec + "\n", "spec")
    s = ck.summarize(ck.check_spec(spec))
    e.say(f"[check] spec: {s['passed']}/{s['total']}" + ("" if s["ok"] else "\n" + s["failures"]))
    return R(s["ok"], s["failures"], {"passed": s["passed"], "total": s["total"]})


@gate("domain_files")
def g_domain_files(e, ctx, step, changed):
    """design: which extra contract files this project's domains ask for (API, data, web)."""
    import arch_flows
    doms = ctx.get("domains") or arch_flows.detect_domains(e, ctx["goal"])
    ctx["domains"] = doms
    ctx["domain_files"] = "\n".join(arch_flows.DOMAIN_FILES[d] for d in doms if d in arch_flows.DOMAIN_FILES)
    e.report(f"- Domains: {', '.join(doms)}", "")
    return R(True, "", {"domains": len(doms)})


@gate("determinism")
def g_determinism(e, ctx, step, changed):
    a = core.run_tests(e.root, e.cfg)
    b = core.run_tests(e.root, e.cfg)
    same = a[0] == b[0] and re.sub(r"\d+\.\d+s", "", a[1]) == re.sub(r"\d+\.\d+s", "", b[1])
    return R(same and a[0] == "pass", "" if same else "two identical test runs gave different results: the pipeline is not deterministic", {"tests": a[0]})


import arch_gates_more  # noqa: E402,F401  (harden/types/a11y/docs/migrate scan pairs and deps_check)
