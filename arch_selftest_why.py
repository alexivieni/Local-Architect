"""Self-test for arch_why: every trace record type said in one plain sentence, a whole run in 5-12 sentences, and the glossary.

Fixture records only (shaped exactly like the ones arch_core / arch_dataflow / arch_flows / arch_hooks / arch_postmortem write).
No model, no server, no network: arch_why must never ask anything to explain anything.

    python arch_selftest_why.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import arch_why as W   # noqa: E402

# ---------------------------------------------------------------- fixtures (the fields the real writers use)
CALL = {"type": "call", "id": "c1", "t": 1.0, "flow": "bugfix", "role": "coder", "model": "qwen3:4b", "label": "coder", "usd": None,
        "messages": [], "reply": "FILE: a.py", "thinking_chars": 0, "tok_s": 41.5, "gen_tokens": 412, "secs": 9.9, "done_reason": "stop",
        "prompt_tokens": 3000, "num_ctx": 8192, "ctx_pct": 37, "ctx_overflow": None, "gpu_pct": 100, "prompt_est": 3000,
        "prefix_reused_est": 0, "load_s": None, "transport": "llama", "cached_exact": True, "tier": "worker", "prompt_ref": "", "system_sha": "x"}
CALL_CACHED = {"type": "call", "id": "c2", "t": 2.0, "flow": "bugfix", "role": "coder", "model": "qwen3:4b", "label": "coder",
               "reply": "x", "gen_tokens": 300, "secs": 0.0, "done_reason": "cached", "transport": "cache", "cached": True,
               "cached_verdict": "pass", "tokens_saved": 3300, "secs_saved": 12.0}
CALL_CUT = dict(CALL, id="c3", done_reason="length", ctx_pct=96)
CALL_CLAUDE = dict(CALL, id="c4", model="claude:claude-opus-5-5", transport="claude", usd=0.03, tok_s=None, tier="cloud")
CALL_HOST = dict(CALL, id="c5", host="desk")
STEP_START = {"type": "step", "t": 1.0, "flow": "bugfix", "step": "repro", "status": "started"}
STEP_OK = {"type": "step", "t": 3.0, "flow": "bugfix", "step": "fix", "ok": True, "unsure": False, "changed": ["app/pager.py"], "tries": 2, "secs": 41.2}
STEP_FAIL = {"type": "step", "t": 3.0, "flow": "bugfix", "step": "fix", "ok": False, "unsure": False, "changed": [], "tries": 4, "secs": 80}
STEP_UNSURE = {"type": "step", "t": 3.0, "flow": "bugfix", "step": "fix", "ok": False, "unsure": True}
STEP_SKIP = {"type": "step", "t": 3.0, "flow": "build", "step": "docs", "status": "skipped"}
STEP_RESUMED = {"type": "step", "t": 3.0, "flow": "build", "step": "plan", "status": "resumed", "ok": True}
CHECK_OK = {"type": "check", "t": 4.0, "flow": "bugfix", "step": "fix", "gate": "tests", "ok": True, "text": "===== 7 passed in 0.31s ====="}
CHECK_FAIL = {"type": "check", "t": 4.0, "flow": "bugfix", "step": "fix", "gate": "tests", "ok": False,
              "text": "FAILED test_pager.py::test_page1\n===== 2 failed, 5 passed in 0.40s ====="}
CHECK_NONE = {"type": "check", "t": 4.0, "flow": "bugfix", "step": "fix", "gate": "lint", "ok": None, "text": "ruff is not installed"}
CHECK_SYNTAX = {"type": "check", "t": 4.0, "gate": "syntax first", "ok": False, "text": "app/pager.py parses"}
CHECK_FAKE = {"type": "check", "t": 4.0, "gate": "fake_code", "ok": False, "text": "stub body in paginate()"}
MARK_T = {"type": "mark", "id": "c1", "accepted": True, "reason": "", "t": 5.0}
MARK_F = {"type": "mark", "id": "c1", "accepted": False, "reason": "2 tests failed", "t": 5.0}
MARK_N = {"type": "mark", "id": "c1", "accepted": None, "reason": "no test command", "t": 5.0}
TOOL_READ = {"type": "tool", "t": 6.0, "flow": "explore", "step": "look", "tool": "read", "call": "c1", "turn": 2,
             "args": {"path": "app/pager.py", "start": 1, "end": 40}, "ok": True, "result": "def paginate(...)"}
TOOL_GREP_BAD = {"type": "tool", "t": 6.0, "flow": "explore", "step": "look", "tool": "grep", "call": "c1", "turn": 3,
                 "args": {"pattern": "def paginate("}, "ok": False, "result": "bad regex: missing )"}
TOOL_ODD = {"type": "tool", "t": 6.0, "tool": "teleport", "args": {}, "ok": None, "result": ""}
ESC_LOCAL = {"type": "escalation", "t": 7.0, "flow": "bugfix", "step": "fix", "role": "coder", "from_model": "qwen3:4b",
             "to_model": "qwen3:30b-a3b", "to_tier": "heavy", "after_call": "c1", "why": "2 failed, 5 passed in 0.4s", "tries": 2}
ESC_CLOUD = {"type": "escalation", "t": 8.0, "flow": "bugfix", "step": "fix", "role": "coder", "from_model": "qwen3:30b-a3b",
             "to_model": "claude:claude-opus-5-5", "to_tier": "cloud", "after_call": "c6", "why": "same failure", "tries": 2}
VERDICT_PASS = {"type": "verdict", "t": 9.0, "flow": "bugfix", "verdict": "pass", "why": "",
                "meter": {"calls": 6, "tokens_in": 12000, "tokens_out": 2400, "secs": 95.0, "heavy_calls": 2, "escalations": 1}}
VERDICT_FAIL = dict(VERDICT_PASS, verdict="fail", why="tests still failing after 4 tries")
VERDICT_UNSURE = dict(VERDICT_PASS, verdict="not_sure", why="no test command found")
HOOK_OK = {"type": "hook", "t": 5.0, "flow": "build", "point": "after_apply", "hook": "format", "cmd": "black .", "ok": True, "secs": 0.4, "output": ""}
HOOK_FAIL = dict(HOOK_OK, hook="lint", cmd="ruff check .", ok=False, output="E501 line too long")
FLAKY = {"type": "flaky", "t": 5.0, "flow": "bugfix", "step": "fix", "gate": "tests", "first": "1 failed"}
BEST = {"type": "best_of_n", "t": 5.0, "flow": "build", "step": "code", "n": 3, "parallel": True, "passed": 2, "winner": 2}
BEST_NONE = dict(BEST, passed=0, winner=None)
POST = {"type": "postmortem", "t": 10.0, "flow": "bugfix", "cause": "The fix changed the offset but not the page size.",
        "next_try": "Start from test_page1 and compute the slice by hand.", "fault": "worker"}
TODO = {"type": "todo", "t": 1.0, "flow": "bugfix", "items": [{"id": "s1", "title": "Repro", "status": "done"},
                                                              {"id": "s2", "title": "Fix", "status": "doing"}]}
ROUTE_KW = {"type": "route", "flow": "bugfix", "why": "keywords: fails, broken", "domains": ["python"], "has_tests": True}
ROUTE_MODEL = {"type": "route", "flow": "refactor", "why": "router model (agrees with the keyword hint): split the big module"}
ROUTE_YOU = {"type": "route", "flow": "test", "why": "chosen by you"}
ROUTE_DEFAULT = {"type": "route", "flow": "new", "why": "default", "empty": True}
UNDO = {"type": "undo", "t": 5.0, "step": "fix", "why": "fewer tests pass (5 -> 3) and more fail (0 -> 2)"}
UNDO_LOST = {"type": "undo", "t": 5.0, "why": "it removed paginate from app/pager.py, which an earlier sprint built"}
REF_RUN = {"type": "refusal", "text": "Claude budget for this run is used up ($0.50 of $0.50; Settings > Claude spend per run)"}
REF_DAY = {"type": "refusal", "text": "Claude's daily spend cap is reached ($5.00 of $5.00 today; Settings > Claude spend per day)"}
REF_TEST = {"type": "refusal", "text": "Claude is never used inside the self-tests"}
REF_EVAL = {"type": "refusal", "text": "Claude is never used during eval (it would measure Claude, not your models)"}
REF_OFF = {"type": "refusal", "text": "Claude is off (Settings > Online > Use Claude)"}
REF_DECL = {"type": "refusal", "text": "Claude declined this request (refusal); treated as could-not-run, not as an answer"}
BROKER = {"target": "gpu_worker", "why": "the worker is loaded and idle", "eta_s": 12.0, "cost_usd": 0.0, "evidence": "measured",
          "considered": [], "kind": "generate", "ts": 1.0}
BROKER_CLAUDE = dict(BROKER, target="claude", why="no local model can answer before the deadline", cost_usd=0.02, evidence="estimate")
TOKEN = {"type": "token", "role": "coder", "model": "qwen3:4b", "label": "coder", "text": "def pa"}


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:400]})" if extra and not ok else ""))

    def says(name, rec, *must, absent=()):
        s = W.explain_event(rec)
        low = s.lower()
        ok = (isinstance(s, str) and s and s[0].isupper() and s.endswith(".") and "\n" not in s
              and all(m.lower() in low for m in must) and not any(a.lower() in low for a in absent))
        check(name, ok, s)
        return s

    print("arch_why: one sentence per record")
    # -- calls
    says("call: model, role, tokens, speed, free", CALL, "4B model (qwen3:4b)", "coder", "412 tokens", "41.5 tokens a second", "cost nothing")
    says("call: cached reply reused, saving named", CALL_CACHED, "already answered", "3300 tokens", "pass", absent=("cost nothing",))
    says("call: cut off at the limit, window nearly full", CALL_CUT, "cut off", "96% full")
    says("call: Claude says over the internet and its cost", CALL_CLAUDE, "claude (claude-opus-5-5)", "internet", "$0.03", absent=("cost nothing",))
    says("call: another machine is named", CALL_HOST, "'desk'")
    # -- steps
    says("step: started", STEP_START, "repro", "started")
    says("step: passed, tries, time, files", STEP_OK, "'fix' passed", "two tries", "41 s", "app/pager.py")
    says("step: did not pass", STEP_FAIL, "did not pass", "four tries")
    says("step: not sure is not called a failure", STEP_UNSURE, "not sure", absent=("did not pass",))
    says("step: skipped", STEP_SKIP, "skipped")
    says("step: resumed from an earlier run", STEP_RESUMED, "already finished")
    # -- checks: three outcomes, never two
    says("check: tests passed with the count", CHECK_OK, "the tests passed", "seven passed")
    says("check: tests failed with counts", CHECK_FAIL, "failed", "two tests failed", "five passed")
    s = says("check: could not run is not sure, never failed", CHECK_NONE, "could not run", "not sure", "ruff is not installed")
    check("check: could-not-run sentence never says 'failed'", "failed" not in s.lower().replace("pass or a fail", ""), s)
    says("check: syntax gate named", CHECK_SYNTAX, "syntax check", "failed")
    says("check: fake-code gate named", CHECK_FAKE, "fake-code check", "stub")
    # -- marks
    says("mark: accepted", MARK_T, "accepted")
    says("mark: rejected with reason", MARK_F, "rejected", "2 tests failed")
    says("mark: None is neither good nor bad", MARK_N, "could not be checked", "neither", absent=("rejected",))
    # -- tools
    says("tool: read names the file", TOOL_READ, "read app/pager.py", "turn 2")
    says("tool: a failed tool says so", TOOL_GREP_BAD, "searched the code for", "did not work", "bad regex")
    says("tool: unknown tool still a sentence", TOOL_ODD, "teleport")
    # -- escalation
    says("escalation: local hand-over names both models and the reason", ESC_LOCAL, "30B mixture-of-experts", "heavy tier", "the 4B got stuck twice", "two tests failed")
    says("escalation: to Claude", ESC_CLOUD, "claude was asked", "the moe got stuck twice")
    says("escalation: handover note only when recorded", dict(ESC_LOCAL, handover=True), "handover note")
    check("escalation: no handover claim when the record has none", "handover" not in W.explain_event(ESC_LOCAL).lower())
    # -- verdicts
    says("verdict: pass with what it used", VERDICT_PASS, "passed", "six model calls", "14400 tokens")
    says("verdict: fail with the reason", VERDICT_FAIL, "did not pass", "tests still failing")
    says("verdict: not sure explained", VERDICT_UNSURE, "not sure", "could not run", "no test command", absent=("did not pass",))
    says("verdict: missing verdict word", {"type": "verdict"}, "before it reached a verdict")
    # -- the rest
    says("hook: ran and passed", HOOK_OK, "your own command", "format", "after apply", "passed")
    says("hook: ran and failed with output", HOOK_FAIL, "failed", "E501")
    says("hook: could not run", dict(HOOK_OK, ok=None), "could not run", absent=("ran and",))
    says("flaky: named and not sure", FLAKY, "flaky", "not sure")
    says("best of n: winner", BEST, "three different answers", "at the same time", "two passed", "answer 2 was kept")
    says("best of n: none kept", BEST_NONE, "none was good enough")
    says("postmortem: cause, next try, advice only", POST, "page size", "next time", "advice, not a verdict")
    says("todo: items and done", TODO, "two items", "one done")
    says("route: keywords + tests folder", ROUTE_KW, "picked the bugfix flow", "you said 'fails' and 'broken'", "the folder has tests")
    says("route: router model's own reason", ROUTE_MODEL, "refactor", "split the big module", "keyword match agreed")
    says("route: your choice", ROUTE_YOU, "because you chose it")
    says("route: default on an empty folder", ROUTE_DEFAULT, "new flow", "default", "folder is empty")
    says("route: no model answered", {"type": "route", "flow": "bugfix", "why": "keywords: traceback (no model answered)"}, "'traceback'", "no router model answered")
    says("undo: tests that passed before now fail", UNDO, "this change was undone", "made two tests fail that passed before")
    says("undo: lost code", UNDO_LOST, "undone", "removed paginate")
    says("undo: from counts", {"type": "undo", "failed_before": 0, "failed_after": 1}, "made one test fail")
    says("refusal: per-run cap", REF_RUN, "claude was not asked", "this run's claude budget")
    says("refusal: per-day cap", REF_DAY, "today's claude spending cap")
    says("refusal: self-tests", REF_TEST, "self-tests never use claude")
    says("refusal: eval", REF_EVAL, "benchmark", "your own models")
    says("refusal: off", REF_OFF, "use claude is off")
    says("refusal: Claude declined is could not run", REF_DECL, "declined", "could not run")
    says("broker: decision without a type field", BROKER, "generate job", "worker model on the graphics card", "measured")
    says("broker: Claude with cost", BROKER_CLAUDE, "claude", "$0.02", "estimate")
    says("token: live writing", TOKEN, "4B model", "writing")
    says("unknown type: still one sentence", {"type": "server_restart", "step": "fix"}, "server restart", "'fix'")

    # every handler answers its own type (none falls through to the generic sentence)
    generic = [t for t in W.HANDLERS if W.explain_event({"type": t}).startswith("A " + t.replace("_", " ") + " record")]
    check("every known record type has its own sentence", not generic, generic)
    # never raises on junk
    junk = [None, 3, "x", [], {}, {"type": None}, {"type": "call", "gen_tokens": "many", "secs": "slow", "model": None},
            {"type": "check", "ok": "maybe", "text": None}, {"type": "escalation", "chain": None, "tries": "x"},
            {"type": "tool", "args": "notadict"}, {"type": "verdict", "meter": "x"}, {"type": "step", "changed": None, "tries": "?"}]
    bad = []
    for j in junk:
        try:
            s = W.explain_event(j)
            if not (isinstance(s, str) and s.strip()):
                bad.append(repr(j))
        except Exception as ex:
            bad.append(f"{j!r}: {ex}")
    check("junk records never raise and always give a sentence", not bad, bad)

    print("arch_why: model and number words")
    check("short_model: 4B / MoE / Claude", (W.short_model("qwen3:4b"), W.short_model("qwen3:30b-a3b"), W.short_model("claude:claude-opus-5-5"))
          == ("the 4B", "the MoE", "Claude"), (W.short_model("qwen3:4b"), W.short_model("qwen3:30b-a3b")))
    check("model_words: decimals and dotted family names", W.model_words("qwen2.5-coder:14b") == "the 14B model (qwen2.5-coder:14b)"
          and W.model_words("llama3.2:1.5b").startswith("the 1.5B"), (W.model_words("qwen2.5-coder:14b"), W.model_words("llama3.2:1.5b")))
    check("money: cents and fractions of a cent", (W._usd(0.03), W._usd(1.5), W._usd(0.0042), W._usd(0)) == ("$0.03", "$1.50", "$0.0042", ""))

    print("arch_why: the whole run")
    home = tempfile.mkdtemp(prefix="arch_why_")
    try:
        rd = os.path.join(home, "run1")
        os.makedirs(rd)
        with open(os.path.join(rd, "run.json"), "w", encoding="utf-8") as f:
            json.dump({"run_id": "r1", "flow": "bugfix", "goal": "paginate() returns page 0 rows for page 1; the tests fail",
                       "status": "done", "started": 1.0}, f)
        recs = [ROUTE_KW, TODO, STEP_START, CALL, CHECK_FAIL, MARK_F, UNDO, dict(CALL, id="c6"), CHECK_FAIL, ESC_LOCAL,
                dict(CALL, id="c7", model="qwen3:30b-a3b", tier="heavy"), CHECK_FAIL, ESC_CLOUD,
                dict(CALL_CLAUDE, id="c8", usd=0.03), TOOL_READ, CHECK_OK, CHECK_NONE, MARK_T, STEP_OK, VERDICT_PASS]
        with open(os.path.join(rd, "trace.jsonl"), "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
            f.write("this line is not json\n")
        out = W.explain_run(rd)
        text = " ".join(out)
        check("run: a list of 5-12 sentences", isinstance(out, list) and W.MIN_SENTENCES <= len(out) <= W.MAX_SENTENCES, len(out))
        check("run: every item is one sentence", all(isinstance(s, str) and s.endswith(".") and "\n" not in s for s in out), out)
        check("run: opens with the request", out and out[0].startswith('You asked: "paginate()') and "bugfix flow" in out[0], out[:1])
        check("run: ends with the verdict", out and out[-1].startswith("The run passed"), out[-1:])
        check("run: the router's reason is said", "you said 'fails' and 'broken'" in text, text)
        check("run: the Claude escalation names both stuck models and its cost",
              "Claude was asked on step 'fix' because the 4B and the MoE both got stuck twice; it cost $0.03." in text, text)
        check("run: the undo is said in plain words", "made two tests fail that passed before" in text, text)
        check("run: Claude's total cost appears in the calls summary", "Claude cost $0.03 in total" in text, text)
        check("run: the could-not-run check is counted", "one could not run" in text, text)
        check("run: the changed file is named", "app/pager.py" in text, text)
        pos = [text.find(x) for x in ("You asked", "you said 'fails'", "made two tests fail", "Claude was asked", "The run passed")]
        check("run: sentences are in the order things happened", all(p >= 0 for p in pos) and pos == sorted(pos), pos)

        # a long run is trimmed to the limit, and the verdict survives the trim
        rd2 = os.path.join(home, "run2")
        os.makedirs(rd2)
        with open(os.path.join(rd2, "run.json"), "w", encoding="utf-8") as f:
            json.dump({"flow": "build", "goal": "a todo app", "status": "failed"}, f)
        with open(os.path.join(rd2, "trace.jsonl"), "w", encoding="utf-8") as f:
            for i in range(30):
                f.write(json.dumps(dict(STEP_FAIL, step=f"sprint{i}")) + "\n")
                f.write(json.dumps(dict(CHECK_FAIL, step=f"sprint{i}")) + "\n")
                f.write(json.dumps(dict(UNDO, step=f"sprint{i}")) + "\n")
            f.write(json.dumps(VERDICT_FAIL) + "\n")
            f.write(json.dumps(POST) + "\n")
        out2 = W.explain_run(rd2)
        check("run: a long run is cut to at most 12", len(out2) == W.MAX_SENTENCES, len(out2))
        check("run: the trim keeps the opening and the verdict", out2[0].startswith('You asked: "a todo app"')
              and any(s.startswith("The run did not pass") for s in out2), out2)
        check("run: the postmortem comes after the verdict", any("advice, not a verdict" in s for s in out2)
              and out2.index(next(s for s in out2 if s.startswith("The run did not pass"))) <
              out2.index(next(s for s in out2 if "advice, not a verdict" in s)), out2)

        # a run that has only started still says at least five true things
        rd3 = os.path.join(home, "run3")
        os.makedirs(rd3)
        with open(os.path.join(rd3, "run.json"), "w", encoding="utf-8") as f:
            json.dump({"flow": "review", "goal": "review the code", "status": "running", "started": 1.0}, f)
        out3 = W.explain_run(rd3)
        t3 = " ".join(out3)
        check("run: an unfinished run still gives 5+ sentences", W.MIN_SENTENCES <= len(out3) <= W.MAX_SENTENCES, out3)
        check("run: an unfinished run is not called a pass", "not reached a verdict" in t3 and "passed" not in t3.replace("counts as passed", ""), out3)
        check("run: no model and no check are said honestly", "No model was asked" in t3 and "No code check ran" in t3, out3)
        check("run: an empty folder gives nothing", W.explain_run(os.path.join(home, "missing")) == [])
    finally:
        shutil.rmtree(home, ignore_errors=True)

    print("arch_why: glossary")
    for term in ("tier", "gate", "not sure", "escalation", "council", "crew", "fast lane", "fleet", "flow", "verdict", "handover",
                 "night shift", "make room", "moe", "fake code", "cap", "best of n", "flaky", "postmortem"):
        g = W.glossary(term)
        check(f"glossary: '{term}'", isinstance(g, str) and len(g) > 30 and g.endswith("."), g)
    check("glossary: spellings fold together", W.glossary("Not_Sure") == W.glossary("not sure") == W.glossary("unsure")
          and W.glossary("Fast-Lane") == W.glossary("fast lane") and W.glossary("gates") == W.glossary("gate")
          and W.glossary("mixture of experts") == W.glossary("moe"))
    check("glossary: an unknown word gives an empty string", W.glossary("flux capacitor") == "")
    allg = W.glossary()
    check("glossary(): every entry, as a copy", isinstance(allg, dict) and len(allg) >= 40 and allg is not W.GLOSSARY)
    check("glossary: not sure is said to be neither pass nor failure", "not a failure" in W.glossary("not sure"))

    print("arch_why: built by code alone")
    with open(os.path.join(HERE, "arch_why.py"), encoding="utf-8") as f:
        src = f.read()
    banned = [w for w in ("import arch_core", "import arch_cloud", "import arch_transport", "urllib", "socket", "http.client",
                          "stream_chat", "ask_json", ".call(") if w in src]
    check("no model, server or network is reachable from arch_why", not banned, banned)

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' WHY CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' WHY CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
