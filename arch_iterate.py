"""The flow iterator: improve a YAML flow from what its own runs show, and change nothing until a measured trial says it is not worse.

    diagnose(flow_id)            where runs of this flow fail or cost most: per step its outcomes (pass / fail / could not run), tries,
                                 seconds, tokens, which gate said no and the failure text it said most often. Evidence comes from the run
                                 traces (.arch/runs/*/trace.jsonl, located through the project folders and the Logbook) and from the
                                 newest saved eval report for the flow (a task the flow called a pass that the hidden judges failed).
    propose(flow_id)             candidate edits to flows/<id>.yaml, each motivated by a number from diagnose(): more tries where tries
                                 have rescued runs, a larger budget where the step ran into it, a larger model where tries never help, a
                                 cheaper model where the heavy one never needed a second try, a cheap stopping check moved before the
                                 expensive work it does not depend on, a read-only scout before a step that fails on code it could not
                                 see, a judge after the last step when the flow passed work the judges failed. Every candidate is checked
                                 by arch_dataflow.validate (as itself and as its trial copy) and must survive a YAML round trip through
                                 both readers; anything that does not is listed under `refused`, never offered.
    trial(flow_id, candidate)    writes the candidate as a NEW flow (<id>_v2, _v3 ...), runs the flow's eval tasks for the current file
                                 and for the candidate, one after the other under the heavy-job lock, and returns arch_eval.compare.
    adopt(trial)                 only when that compare found no regression on at least one task, and only if flows/<id>.yaml is still
                                 the file the candidate was made from. The old file is kept in <data>/iterate/backups/; undo_adopt() puts
                                 it back. Nothing here adopts anything by itself.

A gate that could not run stays "could not run" in every count here: it is neither a pass nor a failure (CLAUDE.md, three outcomes).
"""
import copy
import glob
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter

import arch_dataflow

MIN_RUNS = 3                     # fewer runs than this and a rate is an anecdote: no candidate is built on it
SCOUT_PROMPT = "scout_then_build/scout"
GENERATIVE = ("generate", "doc", "sample_n", "cascade")
TIER_UP = {"router": "worker", "worker": "heavy", None: "heavy", "": "heavy"}
CONTEXT_MISS = re.compile(r"NameError|AttributeError|ImportError|ModuleNotFoundError|not defined|has no attribute|no such file|"
                          r"not found|unknown (?:name|function|module)|undefined|missing (?:function|import|name)|cannot import", re.I)
BUDGET_WORDS = re.compile(r"budget|exceeded", re.I)
TOP_ORDER = ("id", "title", "kind", "blurb", "trial_of", "domains", "produces", "uses_roles", "budget", "context", "overrides")
_PLAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]*")
_PLAIN_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_WORDS = {"yes", "no", "on", "off", "true", "false", "null", "none", "y", "n", "~"}


# ------------------------------------------------------------------------------------------------------ small helpers
def _data_dir():
    try:
        import arch_core
        return arch_core.data_dir()
    except Exception:
        return os.environ.get("ARCHITECT_DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "hub_data")


def iterate_dir():
    return os.path.join(_data_dir(), "iterate")


def _sha(text):
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:16]


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _write_atomic(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _p(values, q):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    return vals[min(len(vals) - 1, int(round(q * (len(vals) - 1))))]


def _norm_sid(sid):
    """`sprints[2].code` -> `sprints.code`: every pass of a foreach counts toward the same step."""
    return re.sub(r"\[[^\]]*\]", "", str(sid or "")).strip()


def _norm_fail(text):
    """The line of a failure that says what went wrong, with numbers folded so the same failure counts as one."""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    pick = (next((ln for ln in lines if re.search(r"\w+(?:Error|Exception)\b|^assert|not defined", ln)), None)
            or next((ln for ln in lines if re.search(r"FAILED|failed|refused|missing|not found", ln)), lines[0]))
    pick = re.sub(r"\d+", "N", pick)
    return re.sub(r"\s+", " ", pick)[:160]


# ------------------------------------------------------------------------------------------------------ YAML out (both readers must agree)
def _str(s):
    if _PLAIN.fullmatch(s) and s.lower() not in _WORDS:
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _key(k):
    k = str(k)
    if _PLAIN_KEY.fullmatch(k) and k.lower() not in _WORDS:
        return k
    return '"' + k.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _flow_val(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return _str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_flow_val(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{_key(k)}: {_flow_val(x)}" for k, x in v.items()) + "}"
    return _str(str(v))


def _flow_safe(v):
    """The built-in reader splits a flow collection on commas and brackets without honouring escapes: no quote, backslash or newline inside."""
    if isinstance(v, str):
        return not any(ch in v for ch in '"\\\n') and v.count("{") == v.count("}") and v.count("[") == v.count("]")
    if isinstance(v, dict):
        return all(_flow_safe(x) for x in v.values()) and all(_flow_safe(str(k)) for k in v)
    if isinstance(v, (list, tuple)):
        return all(_flow_safe(x) for x in v)
    return True


def _emit_map(d, ind, lines, blocks, first=None):
    for i, (k, v) in enumerate(d.items()):
        pre = first if (i == 0 and first is not None) else " " * ind
        key = _key(k)
        if blocks and isinstance(v, str) and "\n" in v and not v.startswith((" ", "\n")):
            body = v[:-1] if v.endswith("\n") else v
            lines.append(f"{pre}{key}: |" + ("" if v.endswith("\n") else "-"))
            lines += [(" " * (ind + 2) + ln) if ln else "" for ln in body.split("\n")]
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            lines.append(f"{pre}{key}:")
            for item in v:
                if item:
                    _emit_map(item, ind + 4, lines, blocks, first=" " * (ind + 2) + "- ")
                else:
                    lines.append(" " * (ind + 2) + "- {}")
        elif isinstance(v, list) and v and not _flow_safe(v) and all(not isinstance(x, (dict, list)) for x in v):
            lines.append(f"{pre}{key}:")
            lines += [" " * (ind + 2) + "- " + _flow_val(x) for x in v]
        elif isinstance(v, dict) and v and (len(_flow_val(v)) > 140 or not _flow_safe(v) or any(isinstance(x, list) and x and isinstance(x[0], dict) for x in v.values())):
            lines.append(f"{pre}{key}:")
            _emit_map(v, ind + 2, lines, blocks)
        else:
            lines.append(f"{pre}{key}: {_flow_val(v)}")


def _ordered(doc):
    out = {k: doc[k] for k in TOP_ORDER if k in doc}
    out.update({k: v for k, v in doc.items() if k not in out and k not in ("steps", "verdict")})
    for k in ("steps", "verdict"):
        if k in doc:
            out[k] = doc[k]
    return out


def _canon(x):
    return json.dumps(x, sort_keys=True, default=str)


def _readers(text):
    """The document as each available YAML reader sees it: PyYAML when installed, and always the built-in reader."""
    import arch_ci
    out = []
    try:
        out.append(arch_ci.load_yaml(text))
    except Exception as ex:
        out.append(ex)
    try:
        p = arch_ci._Mini(text)
        obj = p.node(-1)
        out.append(obj if p.peek() is None else ValueError("trailing text"))
    except Exception as ex:
        out.append(ex)
    return out


def to_yaml(doc):
    """A flow document as YAML that reads back identical in PyYAML and in the built-in reader. Raises ValueError when no form does."""
    doc = _ordered(doc)
    want = _canon(doc)
    for blocks in (True, False):
        lines = []
        _emit_map(doc, 0, lines, blocks)
        text = "\n".join(lines) + "\n"
        if all(not isinstance(r, Exception) and _canon(r) == want for r in _readers(text)):
            return text
    raise ValueError("this document does not survive a YAML round trip with the built-in reader")


# ------------------------------------------------------------------------------------------------------ evidence: traces
def read_trace(path):
    """One run's trace.jsonl -> {flow, verdict, why, meter, steps: {sid: {...}}, order, ended_by}. Unreadable lines are skipped."""
    run = {"path": path, "flow": "", "verdict": None, "why": "", "meter": {}, "steps": {}, "order": [], "ended_by": None, "budget_hit": False}
    pending, call_step, marks, started, last_sid = [], {}, [], None, None

    def st(sid):
        if sid not in run["steps"]:
            run["steps"][sid] = {"ran": 0, "ok": 0, "failed": 0, "unsure": 0, "skipped": 0, "secs": 0.0, "tries": 0, "calls": 0, "tokens": 0,
                                 "gates": [], "failures": [], "escalations": 0, "flaky": 0}
            run["order"].append(sid)
        return run["steps"][sid]

    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            t = r.get("type")
            if r.get("flow") and not run["flow"] and t in ("step", "verdict", "call", "check"):
                run["flow"] = str(r["flow"])
            if t == "call":
                pending.append(r)
            elif t == "step":
                sid = _norm_sid(r.get("step"))
                if not sid:
                    continue
                status = r.get("status")
                if status == "started":
                    started = sid
                    continue
                if status in ("skipped", "resumed"):
                    st(sid)["skipped" if status == "skipped" else "ok"] += 1
                    continue
                s = st(sid)
                s["ran"] += 1
                for c in pending:
                    call_step[c.get("id")] = sid
                    s["calls"] += 1
                    s["tokens"] += int(c.get("prompt_tokens") or 0) + int(c.get("gen_tokens") or 0)
                pending = []
                s["secs"] += float(r.get("secs") or 0)
                tries = r.get("tries")
                s["tries"] = max(s["tries"], tries if isinstance(tries, int) and not isinstance(tries, bool) else 0)
                if r.get("ok") is True:
                    s["ok"] += 1
                elif r.get("ok") is None or r.get("unsure"):
                    s["unsure"] += 1                       # could not be checked: not a failure
                else:
                    s["failed"] += 1
                last_sid = sid
            elif t == "check":
                sid = _norm_sid(r.get("step"))
                if sid:
                    ok = r.get("ok")
                    st(sid)["gates"].append((str(r.get("gate") or "?"), ok, r.get("text") or ""))
                    if ok is False and r.get("text"):
                        st(sid)["failures"].append(r["text"])
            elif t == "mark":
                marks.append(r)
            elif t in ("escalation", "flaky"):
                sid = _norm_sid(r.get("step"))
                if sid:
                    st(sid)["escalations" if t == "escalation" else "flaky"] += 1
            elif t == "verdict":
                run["verdict"], run["why"], run["meter"] = r.get("verdict"), r.get("why") or "", r.get("meter") or {}
    if pending:                                         # calls after the last finished step: the step the run died in
        sid = started or "(unfinished)"
        s = st(sid)
        for c in pending:
            call_step[c.get("id")] = sid
            s["calls"] += 1
            s["tokens"] += int(c.get("prompt_tokens") or 0) + int(c.get("gen_tokens") or 0)
        run["ended_by"] = sid
    for m in marks:
        if m.get("accepted") is False and m.get("reason") and m.get("id") in call_step:
            run["steps"][call_step[m["id"]]]["failures"].append(m["reason"])
    m = re.search(r"step (\S+?) (?:failed|did not pass|could not be checked)", run["why"] or "")
    if m:
        run["ended_by"] = _norm_sid(m.group(1))
    elif run["verdict"] in ("fail", "not_sure") and not run["ended_by"]:
        run["ended_by"] = last_sid
    if BUDGET_WORDS.search(run["why"] or ""):
        run["budget_hit"] = True
    return run


def _roots(roots):
    if roots:
        return list(roots)
    out = [os.getcwd()]
    try:
        import arch_learn
        out += [r for r in arch_learn._portfolio_roots() if r not in out]
    except Exception:
        pass
    return out


def _logbook_traces(flow_id):
    """Trace files the Logbook has seen for this flow (read-only: the Logbook is never created or changed from here)."""
    try:
        import arch_learn
        path = arch_learn.db_path()
    except Exception:
        return []
    if not os.path.isfile(path):
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT origin FROM tasks WHERE kind=? AND source IN ('trace','extra')", (flow_id,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [r[0] for r in rows if r[0] and str(r[0]).endswith("trace.jsonl") and os.path.isfile(r[0])]


def find_traces(flow_id, roots=None, logbook=True, limit=200):
    paths = set()
    for root in _roots(roots):
        paths.update(glob.glob(os.path.join(root, ".arch", "runs", "*", "trace.jsonl")))
    if logbook:
        paths.update(_logbook_traces(flow_id))
    paths = {os.path.abspath(p) for p in paths}
    return sorted(paths, key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)[:limit]


def _eval_evidence(flow_id):
    """The newest saved eval report that ran this flow (the iterator's own trial runs excluded)."""
    d = os.path.join(_data_dir(), "evals")
    for p in sorted(glob.glob(os.path.join(d, "*.json")), reverse=True)[:60]:
        try:
            with open(p, encoding="utf-8") as f:
                rep = json.load(f)
        except (OSError, ValueError):
            continue
        if str(rep.get("label") or "").startswith("iterate_"):
            continue
        rs = [r for r in rep.get("results") or [] if r.get("flow") == flow_id]
        if not rs:
            continue
        false_pass = [r for r in rs if r.get("flow_ok") and r.get("verdict") == "fail"]
        return {"path": p, "label": rep.get("label"), "fake": bool(rep.get("fake")), "n": len(rs),
                "passed": sum(1 for r in rs if r.get("verdict") == "pass"), "failed": sum(1 for r in rs if r.get("verdict") == "fail"),
                "not_sure": sum(1 for r in rs if r.get("verdict") == "not_sure"), "false_pass": len(false_pass),
                "false_pass_ids": [r.get("id") for r in false_pass][:10],
                "details": Counter(_norm_fail(r.get("detail") or r.get("error")) for r in rs if r.get("verdict") != "pass").most_common(3)}
    return None


# ------------------------------------------------------------------------------------------------------ diagnose
def diagnose(flow_id, roots=None, traces=None, logbook=True, evals=True, limit=200):
    """-> {flow, runs, verdicts, steps: {sid: stats}, order, ended_by, hotspots: [...], eval, sources, summary}."""
    paths = list(traces) if traces is not None else find_traces(flow_id, roots, logbook, limit)
    runs = [r for r in (read_trace(p) for p in paths) if r and r["flow"] == flow_id]
    agg, order = {}, []
    for run in runs:
        for sid in run["order"]:
            s = run["steps"][sid]
            if sid not in agg:
                agg[sid] = {"runs": 0, "ran": 0, "ok": 0, "failed": 0, "unsure": 0, "skipped": 0, "secs": [], "tokens": [], "calls": [], "tries": [],
                            "gates": {}, "failures": Counter(), "escalations": 0, "flaky": 0, "at_cap": 0, "at_cap_failed": 0, "rescued": 0,
                            "_per_run": []}
                order.append(sid)
            a = agg[sid]
            a["runs"] += 1
            for k in ("ran", "ok", "failed", "unsure", "skipped", "escalations", "flaky"):
                a[k] += s[k]
            if s["ran"]:
                a["secs"].append(round(s["secs"], 1))
                a["tokens"].append(s["tokens"])
                a["calls"].append(s["calls"])
                a["tries"].append(s["tries"])
            for gate, ok, _ in s["gates"]:
                g = a["gates"].setdefault(gate, {"ok": 0, "fail": 0, "none": 0})
                g["ok" if ok is True else "fail" if ok is False else "none"] += 1
            for txt in s["failures"]:
                n = _norm_fail(txt)
                if n:
                    a["failures"][n] += 1
            outcome = "ok" if s["ok"] and not s["failed"] else ("failed" if s["failed"] else ("unsure" if s["unsure"] else "skipped"))
            a["_per_run"].append((run["path"], outcome, s["tries"]))
    total_tok = sum(sum(a["tokens"]) for a in agg.values()) or 0
    total_secs = sum(sum(a["secs"]) for a in agg.values()) or 0.0
    steps = {}
    for sid in order:
        a = agg[sid]
        steps[sid] = {
            "runs": a["runs"], "ok": a["ok"], "failed": a["failed"], "unsure": a["unsure"], "skipped": a["skipped"],
            "fail_rate": round(a["failed"] / a["ran"], 2) if a["ran"] else 0.0,
            "secs_avg": round(sum(a["secs"]) / len(a["secs"]), 1) if a["secs"] else 0.0, "secs_p90": _p(a["secs"], 0.9) or 0.0,
            "tokens_avg": int(sum(a["tokens"]) / len(a["tokens"])) if a["tokens"] else 0, "tokens_p90": _p(a["tokens"], 0.9) or 0,
            "calls_avg": round(sum(a["calls"]) / len(a["calls"]), 1) if a["calls"] else 0.0,
            "tries_avg": round(sum(a["tries"]) / len(a["tries"]), 2) if a["tries"] else 0.0, "tries_max": max(a["tries"] or [0]),
            "token_share": round(sum(a["tokens"]) / total_tok, 2) if total_tok else 0.0,
            "time_share": round(sum(a["secs"]) / total_secs, 2) if total_secs else 0.0,
            "gates": a["gates"], "escalations": a["escalations"], "flaky": a["flaky"],
            "failures": a["failures"].most_common(3), "per_run": a["_per_run"]}
    ended = Counter(r["ended_by"] for r in runs if r["ended_by"] and r["verdict"] != "pass")
    meters = [r["meter"] for r in runs if r["meter"]]
    diag = {"flow": flow_id, "runs": len(runs), "traces_read": len(paths),
            "verdicts": dict(Counter(r["verdict"] or "unknown" for r in runs)), "steps": steps, "order": order,
            "ended_by": dict(ended), "budget_hits": Counter(r["ended_by"] or "(flow)" for r in runs if r["budget_hit"]),
            "run_tokens_p90": _p([int(m.get("tokens_in") or 0) + int(m.get("tokens_out") or 0) for m in meters], 0.9) or 0,
            "run_secs_p90": _p([float(m.get("secs") or 0) for m in meters], 0.9) or 0.0,
            "run_calls_p90": _p([int(m.get("calls") or 0) for m in meters], 0.9) or 0,
            "eval": _eval_evidence(flow_id) if evals else None}
    diag["budget_hits"] = dict(diag["budget_hits"])
    diag["hotspots"] = _hotspots(diag)
    diag["summary"] = explain(diag)
    return diag


def _hotspots(diag):
    out = []
    for sid, s in diag["steps"].items():
        if s["failed"] or s["unsure"]:
            gate = max(s["gates"].items(), key=lambda kv: kv[1]["fail"], default=(None, {"fail": 0}))
            why = f"{sid}: failed {s['failed']} of {s['runs']} run(s)" + (f", could not be checked {s['unsure']}" if s["unsure"] else "")
            if gate[0] and gate[1]["fail"]:
                why += f"; gate {gate[0]} said no {gate[1]['fail']}x"
            if s["failures"]:
                why += f"; most often: \"{s['failures'][0][0]}\" ({s['failures'][0][1]}x)"
            out.append({"step": sid, "what": "fails", "score": s["failed"] + 0.5 * s["unsure"] + s["fail_rate"], "why": why})
        if s["token_share"] >= 0.3 or s["time_share"] >= 0.3:
            out.append({"step": sid, "what": "costs", "score": max(s["token_share"], s["time_share"]) * max(1, s["runs"]) / 2,
                        "why": f"{sid}: {int(100 * s['token_share'])}% of the tokens, {int(100 * s['time_share'])}% of the time "
                               f"(about {s['tokens_avg']} tokens and {s['secs_avg']} s a run, {s['tries_avg']} tries)"})
    return sorted(out, key=lambda h: -h["score"])


def explain(diag):
    v = diag["verdicts"]
    lines = [f"{diag['flow']}: {diag['runs']} run(s) found in {diag['traces_read']} trace(s) -- "
             f"{v.get('pass', 0)} passed, {v.get('fail', 0)} failed, {v.get('not_sure', 0)} could not be checked."]
    if not diag["runs"]:
        lines.append("No runs of this flow to learn from yet: run it a few times (or point --root at a project that has).")
    for h in diag["hotspots"][:5]:
        lines.append("  - " + h["why"])
    if diag["ended_by"]:
        lines.append("  Runs that did not pass ended at: " + ", ".join(f"{k} ({n})" for k, n in Counter(diag["ended_by"]).most_common(4)))
    ev = diag.get("eval")
    if ev:
        lines.append(f"  Newest eval ('{ev['label']}'{', oracle' if ev['fake'] else ''}): {ev['passed']}/{ev['n']} passed"
                     + (f"; {ev['false_pass']} task(s) the flow called a pass were failed by the hidden judges" if ev["false_pass"] else ""))
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------------ propose
def flow_path(flow_id, flows_dir=None):
    return os.path.join(flows_dir or arch_dataflow.FLOWS_DIR, f"{flow_id}.yaml")


def load_flow(flow_id, flows_dir=None):
    path = flow_path(flow_id, flows_dir)
    text = _read(path)
    if text is None:
        raise FileNotFoundError(f"no flow file {path}")
    return arch_dataflow.load_yaml_file(path), _sha(text)


def _step_ref(doc, sid):
    """The step dict for a diagnosed step id (`sprints.code` finds `code` inside the foreach), or None."""
    want = sid.split(".")[-1]
    for s in arch_dataflow.validate_steps(doc):
        if s.get("id") == want:
            return s
    return None


def _top_index(doc, sid):
    for i, s in enumerate(doc.get("steps") or []):
        if isinstance(s, dict) and s.get("id") == sid:
            return i
    return None


def _cap(step):
    if (step.get("loop") or {}).get("max"):
        return int(step["loop"]["max"])
    if step.get("retries"):
        return int(step["retries"]) + 1
    return None


def _rescued(s, cap):
    return sum(1 for _, outcome, tries in s["per_run"] if outcome == "ok" and tries and tries >= 2)


def _at_cap(s, cap):
    rows = [(o, t) for _, o, t in s["per_run"] if cap and t and t >= cap]
    return len(rows), sum(1 for o, _ in rows if o == "failed")


def _cand(kind, step, change, evidence, doc):
    return {"kind": kind, "step": step, "change": change, "evidence": evidence, "doc": doc}


def _rule_retries(doc, diag):
    out = []
    for sid, s in diag["steps"].items():
        step = _step_ref(doc, sid)
        if not step or step.get("kind", "generate") not in GENERATIVE + ("tool_loop",):
            continue
        cap = _cap(step)
        if not cap or s["runs"] < MIN_RUNS or cap >= 6:
            continue
        n_cap, n_cap_failed = _at_cap(s, cap)
        rescued = _rescued(s, cap)
        if n_cap_failed >= 2 and n_cap_failed / s["runs"] >= 0.25 and rescued >= 1:
            new = copy.deepcopy(doc)
            ns = _step_ref(new, sid)
            if ns.get("loop", {}).get("max"):
                ns["loop"]["max"] = cap + 1
                change = f"{sid}: loop.max {cap} -> {cap + 1}"
            else:
                ns["retries"] = int(ns["retries"]) + 1
                change = f"{sid}: retries {ns['retries'] - 1} -> {ns['retries']}"
            out.append(_cand("retries", sid, change,
                             f"{n_cap_failed} of {s['runs']} run(s) used all {cap} tries and still failed, while {rescued} run(s) passed only "
                             f"on a later try: more tries have turned failures into passes here", new))
    return out


def _rule_budget(doc, diag):
    out = []
    for sid, s in diag["steps"].items():
        step = _step_ref(doc, sid)
        if not step or not step.get("budget") or s["runs"] < MIN_RUNS:
            continue
        b = step["budget"]
        hits = int((diag["budget_hits"] or {}).get(sid, 0))
        near = []
        if b.get("tokens") and s["tokens_p90"] >= 0.9 * b["tokens"]:
            near.append(("tokens", s["tokens_p90"]))
        if b.get("seconds") and s["secs_p90"] >= 0.9 * b["seconds"]:
            near.append(("seconds", s["secs_p90"]))
        if hits and not near:
            near = [(k, None) for k in ("tokens", "seconds") if b.get(k)]
        if not near:
            continue
        new = copy.deepcopy(doc)
        nb = _step_ref(new, sid)["budget"]
        parts, ev = [], []
        for k, seen in near:
            nb[k] = int(round(b[k] * 1.5))
            parts.append(f"budget.{k} {b[k]} -> {nb[k]}")
            if seen is not None:
                ev.append(f"its {k} reached {seen} of the {b[k]} allowed in the slowest 10% of runs")
        if hits:
            ev.append(f"{hits} run(s) stopped at this step on a budget")
        _fit_flow_budget(new)
        out.append(_cand("budget", sid, f"{sid}: " + ", ".join(parts), "; ".join(ev), new))
    fb = doc.get("budget") or {}
    flow_hits = int((diag["budget_hits"] or {}).get("(flow)", 0))
    near = [(k, seen) for k, seen in (("tokens", diag["run_tokens_p90"]), ("seconds", diag["run_secs_p90"]), ("calls", diag["run_calls_p90"]))
            if fb.get(k) and seen and seen >= 0.9 * fb[k]]
    if diag["runs"] >= MIN_RUNS and (near or flow_hits) and fb:
        near = near or [(k, None) for k in ("tokens", "seconds") if fb.get(k)]
        new = copy.deepcopy(doc)
        parts = []
        for k, _ in near:
            new["budget"][k] = int(round(fb[k] * 1.5))
            parts.append(f"budget.{k} {fb[k]} -> {new['budget'][k]}")
        ev = "; ".join([f"whole runs reached {seen} {k} of the {fb[k]} allowed (slowest 10%)" for k, seen in near if seen]
                       + ([f"{flow_hits} run(s) stopped on the flow's budget"] if flow_hits else []))
        out.append(_cand("budget", "(flow)", "flow: " + ", ".join(parts), ev, new))
    return out


def _fit_flow_budget(doc):
    """A step may not be allowed more than the whole flow: raise the flow's tokens/seconds to the sum of its steps when needed."""
    fb = doc.get("budget")
    if not isinstance(fb, dict):
        return
    for k in ("tokens", "seconds"):
        total = sum(int((s.get("budget") or {}).get(k) or 0) for s in arch_dataflow.validate_steps(doc))
        if fb.get(k) and total > fb[k]:
            fb[k] = total


def _rule_tier(doc, diag):
    out = []
    for sid, s in diag["steps"].items():
        step = _step_ref(doc, sid)
        if not step or step.get("kind", "generate") not in GENERATIVE or s["runs"] < MIN_RUNS:
            continue
        tier = step.get("tier")
        cap = _cap(step)
        rescued = _rescued(s, cap)
        loop = step.get("loop") or {}
        if s["failed"] >= 2 and s["fail_rate"] >= 0.5 and rescued == 0 and tier != "heavy" and "heavy" not in (loop.get("escalate") or []):
            new = copy.deepcopy(doc)
            ns = _step_ref(new, sid)
            if loop:
                ns["loop"]["escalate"] = list(loop.get("escalate") or []) + ["heavy"]
                change = f"{sid}: escalate to the heavy tier after a failed try (loop.escalate: [heavy])"
            else:
                ns["tier"] = TIER_UP.get(tier, "heavy")
                change = f"{sid}: tier {tier or '(role default)'} -> {ns['tier']}"
            out.append(_cand("tier", sid, change,
                             f"failed {s['failed']} of {s['runs']} run(s) and no run was rescued by another try on the same model", new))
        elif tier == "heavy" and s["runs"] >= 5 and s["failed"] == 0 and s["unsure"] == 0 and s["tries_avg"] <= 1.2:
            new = copy.deepcopy(doc)
            _step_ref(new, sid)["tier"] = "worker"
            out.append(_cand("tier", sid, f"{sid}: tier heavy -> worker",
                             f"passed all {s['runs']} run(s), {s['tries_avg']} tries on average, costing {s['secs_avg']} s a run on the heavy "
                             "model: the worker may do it for less (the trial decides)", new))
    return out


def _rule_order(doc, diag):
    """A cheap check that stops the run, failing even when the expensive step before it passed, is moved in front of that step."""
    out = []
    steps = doc.get("steps") or []
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or step.get("kind") != "code" or step.get("on_fail") != "stop":
            continue
        s = diag["steps"].get(step["id"])
        if not s or s["failed"] < 2:
            continue
        mine = {p: o for p, o, _ in s["per_run"]}
        for j in range(i):
            g = steps[j]
            if not isinstance(g, dict) or g.get("kind", "generate") not in GENERATIVE:
                continue
            gs = diag["steps"].get(g["id"])
            if not gs:
                continue
            g_ok_runs = [p for p, o, _ in gs["per_run"] if o == "ok" and p in mine]
            failed_after_ok = sum(1 for p in g_ok_runs if mine[p] == "failed")
            if len(g_ok_runs) >= 2 and failed_after_ok >= 2 and failed_after_ok / len(g_ok_runs) >= 0.5 and g["id"] not in json.dumps(step):
                new = copy.deepcopy(doc)
                moved = new["steps"].pop(i)
                new["steps"].insert(j, moved)
                out.append(_cand("order", step["id"], f"move {step['id']} before {g['id']}",
                                 f"{step['id']} stopped {failed_after_ok} of {len(g_ok_runs)} run(s) in which {g['id']} had passed, after "
                                 f"{g['id']} spent about {gs['tokens_avg']} tokens and {gs['secs_avg']} s: it does not look like it needs "
                                 f"{g['id']}'s work, so checking first saves that work", new))
                break
    return out


def _rule_scout(doc, diag):
    out = []
    steps = doc.get("steps") or []
    if any(isinstance(s, dict) and s.get("kind") == "tool_loop" for s in steps) or _top_index(doc, "scout") is not None:
        return out
    for sid, s in diag["steps"].items():
        idx = _top_index(doc, sid)
        if idx is None or steps[idx].get("kind", "generate") not in GENERATIVE:
            continue
        texts = list(s["failures"])
        total = sum(n for _, n in texts)
        miss = sum(n for t, n in texts if CONTEXT_MISS.search(t))
        if s["failed"] < 2 or not total or miss / total < 0.5:
            continue
        new = copy.deepcopy(doc)
        new["steps"].insert(idx, {"id": "scout", "kind": "tool_loop", "title": "Look around first (read-only) and write a brief", "role": "architect",
                                  "tier": "router", "prompt": SCOUT_PROMPT, "tools": ["ls", "grep", "search", "read", "outline"],
                                  "allow_writes": False, "max_steps": 6, "store_text": "scout_brief", "on_fail": "continue",
                                  "budget": {"tokens": 40000, "seconds": 480}})
        target = new["steps"][idx + 1]
        c = dict(target.get("context") or {})
        c["query"] = (c.get("query") or "{goal}") + " {scout_brief}"
        target["context"] = c
        _fit_flow_budget(new)
        out.append(_cand("scout", sid, f"add a read-only scout before {sid}; its brief steers which code {sid} is shown",
                         f"{miss} of {total} failure message(s) of {sid} name code the model could not see (most often: \"{texts[0][0]}\")", new))
        break
    return out


def _rule_judge(doc, diag):
    ev = diag.get("eval") or {}
    if not ev or ev.get("false_pass", 0) < 2 or ev["false_pass"] / max(1, ev["n"]) < 0.2:
        return []
    steps = doc.get("steps") or []
    gens = [i for i, s in enumerate(steps) if isinstance(s, dict) and s.get("kind", "generate") in GENERATIVE]
    if not gens or any(isinstance(s, dict) and s.get("kind") == "judge" for s in steps[gens[-1]:]):
        return []
    last = steps[gens[-1]]["id"]
    jid = f"{last}_judge"
    if _top_index(doc, jid) is not None or jid in arch_dataflow.RESERVED:
        return []
    new = copy.deepcopy(doc)
    new["steps"].insert(gens[-1] + 1, {"id": jid, "kind": "judge", "title": f"Judge {last} against the request", "tier": "heavy",
                                       "what": last, "criteria": "the request is done as asked\nno_stubs (no placeholder or fake behaviour)",
                                       "requires": f"{last}.ok", "evidence": {"tests": True}, "on_fail": "continue",
                                       "budget": {"tokens": 30000, "seconds": 600}})
    v = dict(new.get("verdict") or {})
    v["pass_if"] = f"({v.get('pass_if') or 'True'}) and not ({jid}.ok == False)"
    new["verdict"] = v
    _fit_flow_budget(new)
    return [_cand("judge", jid, f"add a judge after {last}; a failed judgement fails the run",
                  f"in eval '{ev['label']}' the flow called {ev['false_pass']} of {ev['n']} task(s) a pass that the hidden judges failed "
                  f"({', '.join(str(x) for x in ev['false_pass_ids'][:3])})", new)]


RULES = (_rule_retries, _rule_budget, _rule_tier, _rule_order, _rule_scout, _rule_judge)


def trial_doc(doc, trial_id):
    """The candidate as its own flow: new id, and every prompt pointed back at the original flow's prompt folder."""
    flow_id = doc["id"]
    new = copy.deepcopy(doc)
    new["id"], new["title"], new["trial_of"] = trial_id, f"{doc.get('title') or flow_id} (trial)", flow_id

    def walk(x):
        if isinstance(x, dict):
            for k, v in list(x.items()):
                if k == "prompt" and isinstance(v, str) and "/" not in v:
                    x[k] = f"{flow_id}/{v}"
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(new.get("steps"))
    return new


def check_candidate(doc):
    """-> problems (empty = fine): the flow validator on the candidate and on its trial copy, prompts every tool loop names, and the
    YAML round trip through both readers."""
    probs = list(arch_dataflow.validate(doc))
    probs += [f"as a trial: {p}" for p in arch_dataflow.validate(trial_doc(doc, doc.get("id", "x") + "_v2"))]
    for s in arch_dataflow.validate_steps(doc):
        if s.get("kind") == "tool_loop" and s.get("prompt") and not os.path.isfile(arch_dataflow.prompt_path(doc.get("id"), s["prompt"])):
            probs.append(f"step {s['id']}: prompt file {arch_dataflow.prompt_path(doc.get('id'), s['prompt'])} is missing")
    try:
        to_yaml(doc)
    except ValueError as ex:
        probs.append(str(ex))
    return sorted(set(probs))


def diff(a, b):
    """Plain lines naming what a candidate changes."""
    out = []

    def rec(x, y, path):
        if isinstance(x, dict) and isinstance(y, dict):
            for k in list(x) + [k for k in y if k not in x]:
                if k not in y:
                    out.append(f"{path}{k}: removed")
                elif k not in x:
                    out.append(f"{path}{k}: added {_flow_val(y[k])[:120]}")
                else:
                    rec(x[k], y[k], f"{path}{k}.")
        elif x != y:
            out.append(f"{path.rstrip('.')}: {_flow_val(x)[:80]} -> {_flow_val(y)[:80]}")

    rec({k: v for k, v in a.items() if k != "steps"}, {k: v for k, v in b.items() if k != "steps"}, "")
    sa = {s["id"]: s for s in arch_dataflow.validate_steps(a) if s.get("id")}
    sb = {s["id"]: s for s in arch_dataflow.validate_steps(b) if s.get("id")}
    ia, ib = [s.get("id") for s in a.get("steps") or []], [s.get("id") for s in b.get("steps") or []]
    for sid in sb:
        if sid not in sa:
            out.append(f"step {sid}: added ({sb[sid].get('kind', 'generate')})")
    for sid in sa:
        if sid not in sb:
            out.append(f"step {sid}: removed")
        else:
            rec(sa[sid], sb[sid], f"{sid}.")
    if [x for x in ia if x in ib] != [x for x in ib if x in ia]:
        out.append(f"step order: {', '.join(map(str, ia))} -> {', '.join(map(str, ib))}")
    return out


def propose(flow_id, diag=None, doc=None, flows_dir=None, roots=None, traces=None):
    """-> {flow, base_sha, candidates: [...], refused: [...], diagnosis}. Each candidate: {id, kind, step, change, evidence, diff, doc}."""
    base_sha = None
    if doc is None:
        doc, base_sha = load_flow(flow_id, flows_dir)
    else:
        text = _read(flow_path(flow_id, flows_dir))
        base_sha = _sha(text) if text is not None else None
    diag = diag if diag is not None else diagnose(flow_id, roots=roots, traces=traces)
    frozen = _canon(doc)
    cands, refused, seen = [], [], set()
    for rule in RULES:
        if rule is not _rule_judge and diag.get("runs", 0) < MIN_RUNS:
            continue                                   # too few runs for any rate to mean something; the eval evidence stands alone
        for c in rule(doc, diag):
            c["id"] = f"{c['kind']}:{c['step']}"
            if c["id"] in seen:
                continue
            seen.add(c["id"])
            c["diff"] = diff(doc, c["doc"])
            c["base_sha"] = base_sha
            c["flow"] = flow_id
            probs = check_candidate(c["doc"]) if c["diff"] else ["changes nothing"]
            if probs:
                refused.append({"id": c["id"], "change": c["change"], "problems": probs})
            else:
                cands.append(c)
    assert _canon(doc) == frozen, "a rule changed the flow it was reading"
    return {"flow": flow_id, "base_sha": base_sha, "candidates": cands, "refused": refused, "diagnosis": diag}


# ------------------------------------------------------------------------------------------------------ trial
def next_trial_id(flow_id, flows_dir=None):
    tdir = os.path.join(iterate_dir(), "trials")
    n = 2
    while os.path.exists(flow_path(f"{flow_id}_v{n}", flows_dir)) or os.path.exists(os.path.join(tdir, f"{flow_id}_v{n}.json")):
        n += 1
    return f"{flow_id}_v{n}"


def eval_runner(fake=False, say=print):
    """The real runner: arch_eval.run over the given tasks with `doc` registered as flow `run_id` for the duration (one run at a time;
    the caller holds the heavy-job lock)."""
    def run(run_id, doc, tasks, label):
        import arch_eval
        import arch_flows
        missing = object()
        prev = arch_flows.FLOWS.get(run_id, missing)
        arch_flows.FLOWS[run_id] = arch_dataflow.DataFlow(doc, None)
        try:
            return arch_eval.run(tasks=tasks, label=label, fake=fake, say=say)
        finally:
            if prev is missing:
                arch_flows.FLOWS.pop(run_id, None)
            else:
                arch_flows.FLOWS[run_id] = prev
    return run


def _as_flow(report, from_id, to_id):
    """The candidate's report with its flow named as the original, so arch_eval.compare lines the two up flow by flow."""
    rep = copy.deepcopy(report or {})
    flows = (rep.get("summary") or {}).get("flows") or {}
    if from_id in flows:
        flows[to_id] = flows.pop(from_id)
    for r in rep.get("results") or []:
        if r.get("flow") == from_id:
            r["flow"] = to_id
    return rep


def trial(flow_id, candidate, quick=True, fake=False, runner=None, tasks=None, flows_dir=None, keep=False, say=print):
    """Run the eval tasks of `flow_id` on the current file and on the candidate (written as flows/<flow_id>_vN.yaml), one after the other.
    -> {ok, flow, trial_id, verdict: better | no_regression | worse | not_sure, compare, changes, n, why, ...}. Adopts nothing."""
    import arch_eval
    doc = candidate["doc"]
    probs = check_candidate(doc)
    if probs:
        return {"ok": False, "flow": flow_id, "verdict": "not_sure", "why": "the candidate does not validate: " + "; ".join(probs)}
    base_doc, base_sha = load_flow(flow_id, flows_dir)
    if candidate.get("base_sha") and candidate["base_sha"] != base_sha:
        return {"ok": False, "flow": flow_id, "verdict": "not_sure", "why": f"flows/{flow_id}.yaml changed since this candidate was made: propose again"}
    trial_id = next_trial_id(flow_id, flows_dir)
    tdoc = trial_doc(doc, trial_id)
    if tasks is None:
        tasks = arch_eval.load_tasks(flows=[flow_id], quick=quick)
    tasks = [t for t in tasks if t.get("kind", "flow") == "flow" and t.get("flow") == flow_id]
    base_tasks = copy.deepcopy(tasks)
    cand_tasks = copy.deepcopy(tasks)
    for t in cand_tasks:
        t["flow"] = trial_id
    path = flow_path(trial_id, flows_dir)
    rec = {"flow": flow_id, "trial_id": trial_id, "ts": time.time(), "candidate": {k: candidate.get(k) for k in ("id", "kind", "step", "change", "evidence", "diff")},
           "doc": doc, "base_sha": base_sha, "quick": bool(quick), "fake": bool(fake), "n": len(tasks), "path": path}
    if not tasks:
        rec.update(ok=False, verdict="not_sure", why=f"there are no eval tasks for {flow_id}: nothing can show the candidate is not worse "
                                                      f"(add some under evals/{flow_id}/ or evals/families/)")
        _save_trial(rec)
        return rec
    _write_atomic(path, to_yaml(tdoc))
    try:
        back = arch_dataflow.validate(arch_dataflow.load_yaml_file(path))
        if back:
            raise ValueError("; ".join(back))
        if runner is None:
            import arch_core
            import arch_heavy
            runner = eval_runner(fake=fake, say=say)
            with arch_heavy.one_at_a_time(f"iterate trial {trial_id}", arch_core.load_config(), say=say):
                base_rep = runner(flow_id, base_doc, base_tasks, f"iterate_{flow_id}_base")
                cand_rep = runner(trial_id, tdoc, cand_tasks, f"iterate_{trial_id}")
        else:
            base_rep = runner(flow_id, base_doc, base_tasks, f"iterate_{flow_id}_base")
            cand_rep = runner(trial_id, tdoc, cand_tasks, f"iterate_{trial_id}")
    finally:
        if not keep:
            try:
                os.remove(path)
            except OSError:
                pass
    cand_named = _as_flow(cand_rep, trial_id, flow_id)
    cmp = arch_eval.compare(base_rep, cand_named, latency=not fake)
    changes = arch_eval.task_changes(base_rep, cand_named)
    b = ((base_rep or {}).get("summary") or {}).get("flows", {}).get(flow_id) or {}
    c = ((cand_named or {}).get("summary") or {}).get("flows", {}).get(flow_id) or {}
    if not b or not c or not b.get("n") or not c.get("n"):
        verdict, why = "not_sure", "one of the two runs measured no task"
    elif not cmp["ok"]:
        verdict, why = "worse", "; ".join(cmp["regressions"])
    elif (c.get("pass_rate") or 0) > (b.get("pass_rate") or 0) or ((c.get("pass_rate") or 0) >= (b.get("pass_rate") or 0) and b.get("tokens_p50")
                                                                    and c.get("tokens_p50") and c["tokens_p50"] <= 0.9 * b["tokens_p50"]):
        verdict, why = "better", f"pass rate {b.get('pass_rate')}% -> {c.get('pass_rate')}%, tokens p50 {b.get('tokens_p50')} -> {c.get('tokens_p50')}"
    else:
        verdict, why = "no_regression", f"pass rate {b.get('pass_rate')}% -> {c.get('pass_rate')}%: no regression, no clear gain"
    rec.update(ok=verdict in ("better", "no_regression"), verdict=verdict, why=why, compare=cmp, changes=changes,
               base=b, cand=c, base_report=(base_rep or {}).get("path"), cand_report=(cand_rep or {}).get("path"), kept=bool(keep))
    _save_trial(rec)
    say(f"[iterate] {trial_id}: {verdict} -- {why}")
    return rec


def _save_trial(rec):
    try:
        d = os.path.join(iterate_dir(), "trials")
        os.makedirs(d, exist_ok=True)
        _write_atomic(os.path.join(d, f"{rec['trial_id']}.json"), json.dumps(rec, indent=1, default=str))
        with open(os.path.join(iterate_dir(), "trials.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({k: rec.get(k) for k in ("ts", "flow", "trial_id", "verdict", "why", "n")}, default=str) + "\n")
    except OSError:
        pass


def load_trial(trial_id):
    p = os.path.join(iterate_dir(), "trials", f"{trial_id}.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------------------------------------------ adopt / undo
def adopt(trial_rec, flows_dir=None, say=print):
    """Replace flows/<flow>.yaml with a trialled candidate. Refused unless the trial measured at least one task with no regression and
    the file is still the one the candidate was made from. The old file goes to <data>/iterate/backups/ (undo_adopt puts it back)."""
    rec = load_trial(trial_rec) if isinstance(trial_rec, str) else trial_rec
    if not rec:
        return {"ok": False, "why": f"no trial named {trial_rec}"}
    flow_id = rec["flow"]
    if rec.get("verdict") not in ("better", "no_regression") or not (rec.get("compare") or {}).get("ok") or not rec.get("n"):
        return {"ok": False, "flow": flow_id, "why": f"the trial said {rec.get('verdict')!r} ({rec.get('why') or 'no measurement'}): "
                                                     "only a trial with no regression can be adopted"}
    path = flow_path(flow_id, flows_dir)
    old = _read(path)
    if old is None:
        return {"ok": False, "flow": flow_id, "why": f"{path} is missing"}
    if _sha(old) != rec.get("base_sha"):
        return {"ok": False, "flow": flow_id, "why": f"flows/{flow_id}.yaml changed after the trial: the measurement no longer applies"}
    probs = check_candidate(rec["doc"])
    if probs:
        return {"ok": False, "flow": flow_id, "why": "the candidate no longer validates: " + "; ".join(probs)}
    bdir = os.path.join(iterate_dir(), "backups")
    os.makedirs(bdir, exist_ok=True)
    backup = os.path.join(bdir, f"{flow_id}.{time.strftime('%Y%m%d_%H%M%S')}.{int(time.time() * 1000) % 1000:03d}.yaml")
    _write_atomic(backup, old)
    _write_atomic(path, to_yaml(rec["doc"]))
    try:
        back = arch_dataflow.validate(arch_dataflow.load_yaml_file(path))
    except Exception as ex:
        back = [str(ex)]
    if back:
        _write_atomic(path, old)
        return {"ok": False, "flow": flow_id, "why": "the written file did not validate, the old one is back: " + "; ".join(back)}
    try:
        with open(os.path.join(iterate_dir(), "adopted.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "flow": flow_id, "trial_id": rec.get("trial_id"), "backup": backup,
                                "change": (rec.get("candidate") or {}).get("change"), "why": rec.get("why")}) + "\n")
    except OSError:
        pass
    note = _engine_note(flow_id)
    say(f"[iterate] adopted {rec.get('trial_id')} as flows/{flow_id}.yaml (old file kept: {backup})" + (f"\n  {note}" if note else ""))
    return {"ok": True, "flow": flow_id, "path": path, "backup": backup, "note": note}


def _engine_note(flow_id):
    try:
        import arch_core
        import arch_flows
        mode = ((arch_core.load_config().get("flows") or {}).get("engine") or "legacy")
        fl = arch_flows.FLOWS.get(flow_id)
        if mode != "data" and fl is not None and not getattr(fl, "data", False):
            return (f"Settings > Flows > engine is '{mode}': {flow_id} still runs from its Python class; the YAML change is used with engine = data.")
    except Exception:
        pass
    return ""


def undo_adopt(flow_id, flows_dir=None, say=print):
    """Put back the newest backup of flows/<flow_id>.yaml (the file it replaces is itself backed up first)."""
    bdir = os.path.join(iterate_dir(), "backups")
    files = sorted(glob.glob(os.path.join(bdir, f"{flow_id}.*.yaml")))
    files = [f for f in files if not f.endswith(".undone.yaml")]
    if not files:
        return {"ok": False, "why": f"no backup of {flow_id} to put back"}
    path = flow_path(flow_id, flows_dir)
    cur = _read(path)
    if cur is not None:
        _write_atomic(files[-1][:-5] + ".replaced.undone.yaml", cur)
    old = _read(files[-1])
    _write_atomic(path, old)
    os.replace(files[-1], files[-1][:-5] + ".undone.yaml")
    say(f"[iterate] flows/{flow_id}.yaml is back to {os.path.basename(files[-1])}")
    return {"ok": True, "flow": flow_id, "path": path, "restored": files[-1]}


# ------------------------------------------------------------------------------------------------------ command line
def main(argv=None):
    """python arch_iterate.py diagnose|propose <flow> [--root DIR]  |  trial <flow> <candidate-id> [--fake] [--full] [--keep]
    |  adopt <trial-id>  |  undo <flow>"""
    a = list(sys.argv[1:] if argv is None else argv)
    if len(a) < 2:
        print(main.__doc__)
        return 2
    cmd, target = a[0], a[1]
    roots = [a[i + 1] for i, x in enumerate(a) if x == "--root" and i + 1 < len(a)] or None
    if cmd == "diagnose":
        print(diagnose(target, roots=roots)["summary"])
        return 0
    if cmd in ("propose", "trial"):
        p = propose(target, roots=roots)
        if cmd == "propose":
            print(p["diagnosis"]["summary"] + "\n")
            for c in p["candidates"]:
                print(f"[{c['id']}] {c['change']}\n    because: {c['evidence']}\n    " + "\n    ".join(c["diff"]))
            for r in p["refused"]:
                print(f"[refused {r['id']}] {r['change']}: {'; '.join(r['problems'][:3])}")
            if not p["candidates"]:
                print("No candidate: the evidence does not point at a change yet.")
            return 0
        want = a[2] if len(a) > 2 else ""
        c = next((c for c in p["candidates"] if c["id"] == want), None)
        if not c:
            print(f"no candidate {want!r}; run: python arch_iterate.py propose {target}")
            return 2
        r = trial(target, c, quick="--full" not in a, fake="--fake" in a, keep="--keep" in a)
        print(r.get("why"))
        if r.get("compare"):
            import arch_eval
            print(arch_eval.compare_table(r["compare"]))
        if r.get("ok"):
            print(f"Adopt with: python arch_iterate.py adopt {r['trial_id']}")
        return 0 if r.get("ok") else 1
    if cmd == "adopt":
        r = adopt(target)
        print(r.get("why") or "")
        return 0 if r["ok"] else 1
    if cmd == "undo":
        r = undo_adopt(target)
        print(r.get("why") or "")
        return 0 if r["ok"] else 1
    print(main.__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
