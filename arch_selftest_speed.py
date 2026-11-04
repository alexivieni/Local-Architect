"""Self-test for the things that make the harness fast without making it dishonest: the reply cache (arch_cache), the byte-identical
system prompt per flow (so the server's prefix cache actually hits), parallel candidate generation and saved prompt caches.

No model, no GPU, no network. Every check here is about a guarantee a person is entitled to:
  * a creative call is never served from the cache, and an eval never is;
  * a stored reply carries what it cost, so the savings reported are measured, not guessed;
  * the same flow step produces a byte-identical system prompt on every try, or the prefix cache is worthless;
  * turning the cache off changes nothing except the speed.
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    print("\n== speed: reply cache, stable prompts, parallel candidates ==")
    import arch_core as core
    import arch_cache as cache

    home = tempfile.mkdtemp(prefix="archspeed_")
    old_home = core.HOME
    old_env = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_EVAL", "ARCHITECT_NO_CACHE", "ARCHITECT_STRICT")}
    core.set_home(home)
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "data")
    os.environ.pop("ARCHITECT_EVAL", None)
    os.environ.pop("ARCHITECT_NO_CACHE", None)
    os.environ["ARCHITECT_STRICT"] = "1"        # a cache that silently does nothing must fail this suite, not pass it
    try:
        _cache_checks(check, cache)
        _seed_checks(check, cache)
        _engine_checks(check)
        _prompt_checks(check)
        _best_of_n_checks(check)
    finally:
        core.set_home(old_home)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' SPEED CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' SPEED CHECKS FAILED'}")
    return 0 if n == len(results) else 1


# ------------------------------------------------------------------ the reply cache

def _cache_checks(check, cache):
    msgs = [{"role": "system", "content": "you are careful"}, {"role": "user", "content": "add a test for allow()"}]
    cold = {"temperature": 0.0}

    k1 = cache.key("qwen3:4b", msgs, cold)
    k2 = cache.key("qwen3:4b", msgs, cold)
    check("the same question gives the same key", k1 == k2 and len(k1) == 64)
    check("a different model is a different question", cache.key("qwen3:8b", msgs, cold) != k1)
    check("one changed character in a message is a different question",
          cache.key("qwen3:4b", [msgs[0], {"role": "user", "content": "add a test for allow()."}], cold) != k1)
    check("a different JSON schema is a different question",
          cache.key("qwen3:4b", msgs, cold, schema={"type": "object"}) != k1)
    check("a different model file (same tag) is a different question",
          cache.key("qwen3:4b", msgs, cold, fingerprint="sha256:aaa") != k1)
    check("the seed and the keep-alive do not change the key (they do not change a temperature-0 answer)",
          cache.key("qwen3:4b", msgs, dict(cold, seed=7, keep_alive="30m", stream=True)) == k1)
    check("a different temperature is a different key (so a warm reply can never be served cold)",
          cache.key("qwen3:4b", msgs, {"temperature": 0.7}) != k1)

    check("temperature 0 counts as repeatable", cache.deterministic({"temperature": 0}) is True)
    check("any real temperature does not", cache.deterministic({"temperature": 0.7}) is False)
    check("an unstated temperature does not (the harness never guesses)", cache.deterministic({}) is False)

    check("a cold lookup misses", cache.get(k1) is None)
    check("storing a reply works", cache.put(k1, "qwen3:4b", "here is the test", tokens_in=1200, tokens_out=300, secs=9.5, tier="worker") is True)
    hit = cache.get(k1)
    check("the stored reply comes back exactly", hit and hit["reply"] == "here is the test")
    check("a hit is marked as a hit, not passed off as a fresh call", hit and hit["cached"] is True)
    check("a hit carries what the original call cost", hit and hit["tokens_in"] == 1200 and hit["tokens_out"] == 300 and abs(hit["secs"] - 9.5) < 0.01)
    check("a hit names the tier that answered", hit and hit["tier"] == "worker")

    check("a verdict can be attached to a remembered reply", cache.attach_verdict(k1, "fail") is True)
    hit2 = cache.get(k1)
    check("a later hit carries the verdict that reply reached last time", hit2 and hit2["verdict"] == "fail")
    check("hits are counted", hit2 and hit2["hits"] >= 2)

    st = cache.stats()
    check("savings are measured from what the calls really cost", st["tokens_saved"] == 3000 and st["secs_saved"] >= 19.0, st)
    check("misses are counted too, so the hit rate is honest", st["misses"] >= 1 and 0 < st["hit_rate"] < 100, st)
    check("the summary line is plain words with the real numbers in it", "remembered replies" in st["line"] and "tokens" in st["line"], st["line"])

    check("an empty reply is not stored", cache.put("k_empty", "m", "") is False)
    check("an enormous reply is not stored", cache.put("k_big", "m", "x" * (cache.MAX_REPLY + 1)) is False)

    os.environ["ARCHITECT_EVAL"] = "1"
    check("an eval never reads the cache (a score must measure the models)", cache.get(k1) is None)
    check("an eval never writes to it either", cache.put("k_eval", "m", "nope") is False)
    check("and it says why in plain words", "eval" in cache.why_not({}, {"temperature": 0}))
    os.environ.pop("ARCHITECT_EVAL", None)
    check("with the eval flag gone, the reply is there again", cache.get(k1) is not None)

    os.environ["ARCHITECT_NO_CACHE"] = "1"
    check("one environment variable turns it off completely", cache.get(k1) is None and cache.put("k_off", "m", "x") is False)
    os.environ.pop("ARCHITECT_NO_CACHE", None)

    check("turning it off in settings turns it off", cache.get(k1, {"cache": {"responses": False}}) is None)
    check("and says why", "settings" in cache.why_not({"cache": {"responses": False}}, {"temperature": 0}))
    check("a creative call is refused with a reason a person can read", "vary" in cache.why_not({}, {"temperature": 0.8}))
    check("a call with no stated temperature is refused with a reason", "guarantee" in cache.why_not({}, {}))
    check("a repeatable call on the default path is allowed", cache.why_not({}, {"temperature": 0}) == "")

    for i in range(12):
        cache.put(f"bulk{i}", "m", f"reply {i}", tokens_in=10, tokens_out=10, secs=0.1)
    before = cache.stats()["rows"]
    gone = cache.prune(max_rows=5, max_days=0)
    after = cache.stats()["rows"]
    check("pruning keeps the newest and drops the rest", after == 5 and gone == before - 5, f"{before} -> {after}, dropped {gone}")
    check("everything can be forgotten", cache.clear() is True and cache.stats()["rows"] == 0)

    check("the cache is a real, validated setting", _is_setting("cache.responses") and _is_setting("cache.max_rows") and _is_setting("cache.max_days"))
    check("it is on by default, because a repeated question should not cost twice", core_defaults()["cache"]["responses"] is True)


def _seed_checks(check, cache):
    """A fixed seed is the other way a call becomes repeatable: the dice are the same dice. That turns Settings > Behaviour > Seed into a
    repeatable-run mode, which is the only way most steps (which run warm on purpose) ever benefit from the cache."""
    msgs = [{"role": "user", "content": "plan the fix"}]
    check("a warm call is not repeatable", cache.deterministic({"temperature": 0.4}) is False)
    check("a warm call WITH a seed is repeatable", cache.deterministic({"temperature": 0.4, "seed": 11}) is True)
    check("and it is allowed, with no complaint", cache.why_not({}, {"temperature": 0.4, "seed": 11}) == "")
    check("two different seeds on a warm call are two different questions",
          cache.key("m", msgs, {"temperature": 0.4, "seed": 1}) != cache.key("m", msgs, {"temperature": 0.4, "seed": 2}))
    check("two different seeds at temperature 0 are the same question (the seed is never consulted there)",
          cache.key("m", msgs, {"temperature": 0.0, "seed": 1}) == cache.key("m", msgs, {"temperature": 0.0, "seed": 2}))
    check("a warm seeded reply and a cold reply are never confused for each other",
          cache.key("m", msgs, {"temperature": 0.4, "seed": 1}) != cache.key("m", msgs, {"temperature": 0.0}))
    check("the refusal for a warm call points at the seed setting, so a person knows what to do",
          "seed" in cache.why_not({}, {"temperature": 0.4}))


def _engine_checks(check):
    """The cache through the real Engine.call, against a server that counts how often it is actually asked."""
    import json
    import threading
    from http.server import ThreadingHTTPServer

    import arch_cache as cache
    import arch_core as core
    import arch_roles
    import arch_selftest

    cache.clear()
    hits = {"n": 0}

    class Counter(arch_selftest.Fake):
        state = {"calls": [], "n": {}}

        def script(self, role, user, full, st):
            hits["n"] += 1
            return f"PLAN: step one\nreply number {hits['n']}"

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Counter)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    was = os.environ.get("OLLAMA_HOST")
    os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv.server_address[1]}"
    proj = tempfile.mkdtemp(prefix="archspeedproj_")
    try:
        with open(os.path.join(proj, "app.py"), "w", encoding="utf-8") as f:
            f.write("def allow(k):\n    return True\n")
        base = core.deep_merge(core.load_config(proj), {"roles": {r: "fake:7b" for r in arch_roles.ROLES}})

        # the router runs at temperature 0, so it is cacheable with no seed at all
        e1 = core.Engine(proj, "t", "g", base, review="auto", say=lambda *_a, **_k: None)
        a = e1.call("router", "which flow fits: fix the bug in app.py")
        asked_after_first = hits["n"]
        e2 = core.Engine(proj, "t", "g", base, review="auto", say=lambda *_a, **_k: None)
        b = e2.call("router", "which flow fits: fix the bug in app.py")
        check("a repeated temperature-0 call does not reach the model at all", hits["n"] == asked_after_first, hits)
        check("and gives back exactly what the model said the first time", a["text"] == b["text"], (a["text"][:40], b["text"][:40]))
        check("the reused reply says so, so nothing is passed off as fresh work", b.get("cached") is True)
        meter_after_hit = dict(e2.meter)          # snapshot before any real call, so the "a hit costs nothing" check means what it says
        check("the first call was not marked as reused", not a.get("cached"))

        recs = [json.loads(x) for x in open(os.path.join(e2.run_dir, "trace.jsonl"), encoding="utf-8")]
        cached_recs = [r for r in recs if r.get("type") == "call" and r.get("cached")]
        check("a reused reply is still traced in full, so an audit sees every step", len(cached_recs) == 1, len(cached_recs))
        check("a reused reply claims no prefix-cache saving, because no server read the prompt",
              cached_recs and cached_recs[0]["prefix_reused_est"] == 0 and cached_recs[0]["cached_exact"] is False, cached_recs[:1])
        second = e2.call("router", "which flow fits: add a feature to app.py")
        check("a reused reply gets its own trace id, so a later mark cannot label the wrong call",
              b["id"] != second["id"], (b["id"], second["id"]))
        raw = open(os.path.join(e2.run_dir, "trace.jsonl"), encoding="utf-8").read()
        check('trace lines keep the exact "type": "call" spelling other code searches for', '"type": "call"' in raw)
        check("the trace says what the reuse saved, in real tokens and seconds",
              cached_recs and cached_recs[0]["tokens_saved"] > 0 and cached_recs[0]["transport"] == "cache", cached_recs[:1])
        check("a reused reply costs nothing in the spend meter", meter_after_hit["tokens_out"] == 0 and meter_after_hit["tokens_in"] == 0, meter_after_hit)
        check("but it is counted, so a loop of free calls still ends",
              meter_after_hit["calls"] == 1 and e2.reuse["cached"] == 1, (meter_after_hit, e2.reuse))
        check("a real call after a reused one does spend tokens, so the meter is not simply broken",
              e2.meter["tokens_out"] > 0 and e2.meter["calls"] == 2, e2.meter)

        # a warm role is NOT reused, because a retry is meant to try something different
        before = hits["n"]
        e3 = core.Engine(proj, "t", "g", base, review="auto", say=lambda *_a, **_k: None)
        e3.call("planner", "plan the work for app.py")
        e4 = core.Engine(proj, "t", "g", base, review="auto", say=lambda *_a, **_k: None)
        e4.call("planner", "plan the work for app.py")
        check("a warm call is asked again every time, so a retry can still differ", hits["n"] == before + 2, hits)

        # with a seed, the whole run becomes repeatable
        seeded = core.deep_merge(base, {"behavior": {"seed": 1234}})
        before = hits["n"]
        e5 = core.Engine(proj, "t", "g", seeded, review="auto", say=lambda *_a, **_k: None)
        e5.call("planner", "plan the work with a seed set")
        mid = hits["n"]
        e6 = core.Engine(proj, "t", "g", seeded, review="auto", say=lambda *_a, **_k: None)
        f2 = e6.call("planner", "plan the work with a seed set")
        check("setting a seed makes even a warm step repeatable, and then it is reused", mid == before + 1 and hits["n"] == mid, hits)
        check("the reused warm reply is the one the seed produced", f2.get("cached") is True)

        # a changed prompt is a new question: the fix loop is never frozen by the cache
        before = hits["n"]
        e7 = core.Engine(proj, "t", "g", seeded, review="auto", say=lambda *_a, **_k: None)
        e7.call("planner", "plan the work with a seed set\n\nTHE TESTS FAILED: allow() returns True always")
        check("appending a failure to the prompt asks the model again (a retry is a different question)", hits["n"] == before + 1, hits)

        # a verdict is attached when the run finishes, and a later hit carries it
        e2.finish(False)
        e8 = core.Engine(proj, "t", "g", base, review="auto", say=lambda *_a, **_k: None)
        g = e8.call("router", "which flow fits: fix the bug in app.py")
        check("a reused reply carries what it led to last time, so a known failure is not repeated blindly",
              g.get("cached_verdict") == "fail", g.get("cached_verdict"))

        st = cache.stats()
        check("the savings reported add up to real work avoided", st["hits"] >= 3 and st["tokens_saved"] > 0, st)

        # --- the system prompt must be the same bytes every time, or the server's prefix cache never hits
        import arch_dataflow as df
        e9 = core.Engine(proj, "bugfix", "g", base, review="auto", say=lambda *_a, **_k: None)
        e9.call("generator", "first try: write the fix")
        e9.call("generator", "second try: THE TESTS FAILED, here is the output ...")
        e9.call("planner", "plan it")
        rec9 = [json.loads(x) for x in open(os.path.join(e9.run_dir, "trace.jsonl"), encoding="utf-8") if '"type": "call"' in x]
        gen = [r["system_sha"] for r in rec9 if r["role"] == "generator"]
        check("two tries of the same role send a byte-identical system prompt (the per-try text goes in the user turn)",
              len(gen) == 2 and gen[0] == gen[1], gen)
        check("a different role sends a different system prompt", len({r["system_sha"] for r in rec9}) == 2, [(r["role"], r["system_sha"]) for r in rec9])
        check("every call records the exact version of the system prompt it sent", all(len(r.get("system_sha") or "") == 12 for r in rec9))

        # the inline path: some families (gemma, deepseek-r1) get one single message instead of system + user. It must be just as stable.
        inline = core.deep_merge(base, {"roles": {r: "gemma2:9b" for r in arch_roles.ROLES}})
        e10 = core.Engine(proj, "bugfix", "g", inline, review="auto", say=lambda *_a, **_k: None)
        e10.call("generator", "first try: write the fix")
        e10.call("generator", "second try: THE TESTS FAILED, output follows ...")
        def inline_recs():
            rows = [json.loads(x) for x in open(os.path.join(e10.run_dir, "trace.jsonl"), encoding="utf-8") if '"type": "call"' in x]
            return [x for x in rows if x["model"] == "gemma2:9b"]      # the run folder is shared with the engine above, so pick ours

        r10 = inline_recs()
        check("a model that needs its instructions inline still sends a byte-identical system prompt across tries",
              len(r10) == 2 and r10[0]["system_sha"] == r10[1]["system_sha"], [(x["role"], x["system_sha"]) for x in r10])
        check("and that reaches the model as one message, not two (which is why it was never covered before)",
              r10 and len(r10[0]["messages"]) == 1, r10 and len(r10[0]["messages"]))
        e10.call("generator", "third try", extra_system="EXTRA: this project uses tabs")
        r10b = inline_recs()[-1]
        check("adding step-specific instructions changes the recorded system prompt, so the two are never confused",
              r10b["system_sha"] != r10[0]["system_sha"], (r10b["system_sha"], r10[0]["system_sha"]))

        tpl = df.load_prompt("bugfix", "repro", e9) if os.path.isfile(df.prompt_path("bugfix", "repro")) else None
        if tpl is not None:
            check("loading a step's prompt file records which file and which version it is", "bugfix/repro.md@" in e9.prompt_ref, e9.prompt_ref)
            e9.call("generator", "after the prompt file was loaded")
            last = [json.loads(x) for x in open(os.path.join(e9.run_dir, "trace.jsonl"), encoding="utf-8") if '"type": "call"' in x][-1]
            check("and every reply is then traceable to the words that produced it", last.get("prompt_ref") == e9.prompt_ref, last.get("prompt_ref"))
            check("editing a prompt file would change that version id", df.prompt_sha(tpl) != df.prompt_sha(tpl + " "), )
    finally:
        srv.shutdown()
        if was is None:
            os.environ.pop("OLLAMA_HOST", None)
        else:
            os.environ["OLLAMA_HOST"] = was
        shutil.rmtree(proj, ignore_errors=True)
        cache.clear()


def _best_of_n_checks(check):
    """Best of N through the real interpreter and the real engine, against a server that answers differently per candidate."""
    import json
    import re
    import threading
    import time as _time
    from http.server import ThreadingHTTPServer

    import arch_core as core
    import arch_dataflow as df
    import arch_dataset as ds
    import arch_roles
    import arch_selftest

    os.environ["ARCHITECT_NO_CACHE"] = "1"         # three samples of one prompt must reach the model three times
    live = {"now": 0, "peak": 0}
    lock = threading.Lock()
    good_big = "def add(a, b):\n    # a long-winded but correct version\n    total = 0\n    total = total + a\n    total = total + b\n    return total\n"
    good_small = "def add(a, b):\n    return a + b\n"
    wrong = "def add(a, b):\n    return a - b\n"

    class Cands(arch_selftest.Fake):
        state = {"calls": [], "n": {}}

        def script(self, role, user, full, st):
            with lock:
                live["now"] += 1
                live["peak"] = max(live["peak"], live["now"])
            _time.sleep(0.4)                             # long enough that parallel requests visibly overlap
            with lock:
                live["now"] -= 1
            m = re.search(r"\(candidate (\d+) of", user)
            body = {"1": wrong, "2": good_big, "3": good_small}.get(m.group(1) if m else "1", wrong)
            return "FILE: calc.py\n```python\n" + body + "```\n"

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Cands)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    was = os.environ.get("OLLAMA_HOST")
    os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv.server_address[1]}"
    homes = []
    try:
        for par in (1, 3):
            proj = tempfile.mkdtemp(prefix=f"archbon{par}_")
            homes.append(proj)
            with open(os.path.join(proj, "calc.py"), "w", encoding="utf-8") as f:
                f.write("def add(a, b):\n    raise NotImplementedError('write me')\n")
            os.makedirs(os.path.join(proj, "tests"))
            with open(os.path.join(proj, "tests", "test_calc.py"), "w", encoding="utf-8") as f:
                f.write("import os, sys\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
                        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\ndef test_add_negative():\n    assert add(-1, 1) == 0\n")
            with open(os.path.join(proj, "conftest.py"), "w", encoding="utf-8") as f:
                f.write("")
            cfg = core.deep_merge(core.load_config(proj), {"roles": {r: "fake:7b" for r in arch_roles.ROLES},
                                                           "server": {"parallel": par}, "behavior": {"battery_saver": "off"}})
            e = core.Engine(proj, "build", "make add() work", cfg, review="auto", say=lambda *_a, **_k: None)
            os.makedirs(os.path.join(df.PROMPTS_DIR, "_selftest"), exist_ok=True)
            with open(os.path.join(df.PROMPTS_DIR, "_selftest", "bon.md"), "w", encoding="utf-8") as f:
                f.write("Make add(a, b) return the sum. Reply with a FILE: block for calc.py.\n")
            flow = df.DataFlow({"id": "_selftest", "title": "t", "steps": [
                {"id": "write", "kind": "sample_n", "n": 3, "role": "generator", "prompt": "bon",
                 "verify": {"gate": "tests_pass"}}], "verdict": {"pass_if": "write.ok"}})
            live["peak"] = 0
            t0 = _time.time()
            out = flow.run(e, {})
            secs = _time.time() - t0
            st = out["steps"]["write"]
            final = open(os.path.join(proj, "calc.py"), encoding="utf-8").read()
            word = "one after another" if par == 1 else "three at a time"
            check(f"best of 3 ({word}): every answer is checked and the run passes", out["verdict"] == "pass" and st.get("passed") == 2, (out["verdict"], st))
            check(f"best of 3 ({word}): of the two that passed, the smaller change is kept", st.get("winner") == 3 and final == good_small, (st.get("winner"), final))
            recs = [json.loads(x) for x in open(os.path.join(e.run_dir, "trace.jsonl"), encoding="utf-8")]
            marks = {r["id"]: r["accepted"] for r in recs if r.get("type") == "mark"}
            calls = [r for r in recs if r.get("type") == "call"]
            check(f"best of 3 ({word}): all three answers are traced, each with its own id", len(calls) == 3 and len({c["id"] for c in calls}) == 3, len(calls))
            check(f"best of 3 ({word}): the winner is marked passed and both others are marked not chosen",
                  sorted(marks.values(), key=str) == [False, False, True], marks)
            if par == 3:
                check("with three parallel slots, the three answers are generated at the same time, not one after another",
                      live["peak"] >= 2, (live["peak"], round(secs, 2)))
                data = ds.build([proj])
                pairs = [p for rows in data["dpo"].values() for p in rows]
                check("every losing answer becomes a preference pair against the winner, to the same prompt",
                      len(pairs) == 2 and all(good_small.strip() in p["chosen"] for p in pairs)
                      and all(p["meta"]["same_task"] for p in pairs), [(p["meta"].get("same_task"), p["rejected"][-30:]) for p in pairs])
            else:
                check("with one slot, the answers are generated one at a time (nothing is sent the server cannot hold)", live["peak"] == 1, live["peak"])
        # ---- a check that fails and then passes with nothing changed is flaky: "not sure", and no more model calls
        import arch_gates
        seq = {"n": 0}

        def coin(e_, ctx_, step_, changed_):
            seq["n"] += 1
            return {"ok": seq["n"] % 2 == 0, "text": "test_add failed this time"}
        arch_gates.GATES["_coin"] = coin
        proj = tempfile.mkdtemp(prefix="archflaky_")
        homes.append(proj)
        cfg = core.deep_merge(core.load_config(proj), {"roles": {r: "fake:7b" for r in arch_roles.ROLES}, "behavior": {"battery_saver": "off"}})
        e = core.Engine(proj, "build", "x", cfg, review="auto", say=lambda *_a, **_k: None)
        flow = df.DataFlow({"id": "_selftest", "title": "t", "steps": [
            {"id": "fix", "kind": "generate", "role": "generator", "prompt": "bon", "loop": {"max": 3, "coach": False},
             "verify": {"gate": "_coin"}}], "verdict": {"pass_if": "fix.ok"}})
        out = flow.run(e, {})
        recs = [json.loads(x) for x in open(os.path.join(e.run_dir, "trace.jsonl"), encoding="utf-8")]
        check("a check that fails and then passes with nothing changed makes the step 'not sure', not a pass",
              out["verdict"] == "not_sure", (out["verdict"], out.get("why")))
        check("and no further model calls are spent on what was a coin toss", len([r for r in recs if r.get("type") == "call"]) == 1,
              len([r for r in recs if r.get("type") == "call"]))
        check("the flakiness is recorded, so it can be fixed", any(r.get("type") == "flaky" and r.get("gate") == "_coin" for r in recs))
        e2 = core.Engine(proj, "build", "x", core.deep_merge(cfg, {"behavior": {"recheck_before_retry": False}}), review="auto", say=lambda *_a, **_k: None)
        seq["n"] = 0
        out2 = flow.run(e2, {})
        check("with the re-check turned off, it simply retries, and the coin eventually lands", out2["verdict"] == "pass", out2["verdict"])
        arch_gates.GATES.pop("_coin", None)
    finally:
        srv.shutdown()
        if was is None:
            os.environ.pop("OLLAMA_HOST", None)
        else:
            os.environ["OLLAMA_HOST"] = was
        os.environ.pop("ARCHITECT_NO_CACHE", None)
        import arch_dataflow as _df
        shutil.rmtree(os.path.join(_df.PROMPTS_DIR, "_selftest"), ignore_errors=True)
        for h in homes:
            shutil.rmtree(h, ignore_errors=True)


def _is_setting(path):
    import arch_params as params
    return params.is_known_setting(path)


def core_defaults():
    import arch_core as core
    return core.DEFAULTS


# ------------------------------------------------------------------ byte-identical prompts

def _prompt_checks(check):
    """A server's prefix cache only helps when the start of the prompt is the same bytes every time. These checks hold the
    harness to that: the system prompt for a (flow, step) is assembled once and reused, and nothing per-try leaks into it."""
    import arch_dataflow as df

    flows = df.load_all(say=lambda _m: None)
    check("the flow files load", bool(flows), list(flows)[:5])
    if not flows:
        return

    seen = {}
    unstable = []
    for fid, flow in sorted(flows.items()):
        for st in flow.doc.get("steps") or []:
            name = st.get("prompt")
            if not name:
                continue
            try:
                a = df.load_prompt(fid, name)
                b = df.load_prompt(fid, name)
            except Exception as ex:                     # a missing prompt is the validator's job, not this test's
                unstable.append(f"{fid}/{st.get('id')}: {type(ex).__name__}")
                continue
            if a != b:
                unstable.append(f"{fid}/{st.get('id')}: differs between reads")
            seen[f"{fid}/{st.get('id')}"] = a
    check("every step's prompt file reads back byte-identical", not unstable, unstable[:4])
    check("there are prompts to be stable about", len(seen) >= 20, len(seen))

    if hasattr(df, "prompt_sha"):
        shas = {k: df.prompt_sha(v) for k, v in seen.items()}
        check("a prompt hash is stable across two calls", all(df.prompt_sha(seen[k]) == s for k, s in shas.items()))
        check("different prompts get different hashes", len(set(shas.values())) > 1)


if __name__ == "__main__":
    sys.exit(run())
