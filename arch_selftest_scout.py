"""Self-tests for the chat's web lookups (arch_cloud: online.web_search, the per-message `web` flag) and the local scout (arch_chat:
online.local_scout, compose/scout).

A fake SDK client stands in for Anthropic and a fake local call stands in for the worker model: no key, no network, no GPU, no cost.
Runs against a throwaway home and data folder.
    python arch_selftest_scout.py
"""
import os
import shutil
import sys
import tempfile
import time

import arch_core as core


class _Final:
    def __init__(self, stop, usage, content):
        self.stop_reason, self.content = stop, content
        self.usage = type("U", (), dict(usage))()


class _FakeStream:
    def __init__(self, parts, final):
        self.parts, self.final = parts, final

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def text_stream(self):
        return iter(self.parts)

    def get_final_message(self):
        return self.final


class _FakeClient:
    """Records every request. `script` = a list of (parts, stop, usage, content), one per request (the last one repeats);
    `fail_tools` = the first request that carries tools raises a 400 about the tool."""
    def __init__(self, script=None, fail_tools=False):
        self.sent, self.fail_tools = [], fail_tools
        self.script = script or [(("It is ", "fine."), "end_turn", {"input_tokens": 1000, "output_tokens": 100}, [])]
        self.messages = self

    def stream(self, **kw):
        if self.fail_tools and "tools" in kw:
            self.fail_tools = False
            raise RuntimeError("Error code: 400 invalid_request_error: tool web_search_20260209 is not supported")
        self.sent.append(kw)
        parts, stop, usage, content = self.script[min(len(self.sent) - 1, len(self.script) - 1)]
        return _FakeStream(parts, _Final(stop, usage, content))


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    print("Scout + web lookup checks")
    keys = ("ARCHITECT_DATA_DIR", "ARCHITECT_CLOUD_TEST", "ARCHITECT_CLOUD_CACHE_TEST", "ARCHITECT_SELFTEST", "ARCHITECT_EVAL", "ARCHITECT_NO_CACHE")
    old_env, old_home = {k: os.environ.get(k) for k in keys}, core.HOME
    home = tempfile.mkdtemp(prefix="archscout_")
    core.set_home(home)
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "data")
    os.environ["ARCHITECT_SELFTEST"] = "1"
    os.environ["ARCHITECT_CLOUD_TEST"] = "1"                  # this suite tests the cloud module on purpose, with a fake client
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    os.environ.pop("ARCHITECT_EVAL", None)
    import arch_chat
    import arch_cloud

    def reset():
        arch_cloud._SPENT.update(usd=0.0, calls=0)
        arch_cloud._HERE.role = None

    def cfg(**online):
        on = {"enabled": True, "cloud_tier": "escalation", "cloud_model": "claude:claude-opus-5-5", "cloud_cache": False}
        on.update(online)
        return {"online": on, "tiers": {"cloud": "claude:claude-opus-5-5"}, "limits": {"cloud_usd_per_run": 50, "cloud_usd_per_day": 50}}

    MSG = [{"role": "user", "content": "What changed in the latest Python release?"}]

    def send(model="claude:claude-opus-5-5", c=None, web=False, chat_reply=True, client=None):
        reset()
        client = client or _FakeClient()
        if chat_reply:
            with arch_cloud.asked_for(None, "low", web):
                r = arch_cloud.chat(model, MSG, {"num_predict": 500}, cfg=c or cfg(), client=client)
        else:
            r = arch_cloud.chat(model, MSG, {"num_predict": 500}, cfg=c or cfg(), client=client)
        return r, client

    try:
        # ---- 1. web lookups: which models, when
        check("web tool offered to Opus 5.x and Sonnet 5.x", arch_cloud.web_supported("claude:claude-opus-5-5")
              and arch_cloud.web_supported("claude-sonnet-5-5") and arch_cloud.web_supported("claude:claude-opus-5"))
        check("web tool not offered to Haiku, Fable or a non-5.x model", not arch_cloud.web_supported("claude:claude-haiku-5-5")
              and not arch_cloud.web_supported("claude:claude-fable-5-1") and not arch_cloud.web_supported("claude:claude-opus-4-5"))
        r, cl = send(c=cfg(web_search="off"))
        check("setting off, no per-message flag: no tools in the request", "tools" not in cl.sent[0], cl.sent[0].keys())
        check("... and no web_searches field in the call record", "web_searches" not in r["calls"][0])
        r, cl = send(c=cfg(web_search="on"))
        check("setting on + Opus: the web_search server tool is in the request",
              cl.sent[0].get("tools") == [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}], cl.sent[0].get("tools"))
        r, cl = send(model="claude:claude-haiku-5-5", c=cfg(web_search="on"))
        check("setting on + Haiku: skipped (model does not support it)", "tools" not in cl.sent[0])
        r, cl = send(c=cfg(web_search="off"), web=True)
        check("per-message web flag turns it on for that message", bool(cl.sent[0].get("tools")))
        r, cl = send(c=cfg(web_search="on"), chat_reply=False)
        check("never inside a run (no chat reply the person sent), even with the setting on", "tools" not in cl.sent[0])
        r, cl = send(c=cfg(web_search=True))
        check("a boolean True setting also counts as on", bool(cl.sent[0].get("tools")))

        # ---- 2. searches counted, tool errors read from result blocks (not exceptions)
        content = [{"type": "server_tool_use", "name": "web_search", "input": {"query": "python 3.15 release notes"}},
                   {"type": "web_search_tool_result", "content": [{"type": "web_search_result", "url": "https://example.org"}]},
                   {"type": "server_tool_use", "name": "web_search", "input": {"query": "python 3.15 changes"}},
                   {"type": "web_search_tool_result", "content": {"type": "web_search_tool_result_error", "error_code": "max_uses_exceeded"}},
                   {"type": "text", "text": "Answer."}]
        usage = {"input_tokens": 1000, "output_tokens": 100, "server_tool_use": {"web_search_requests": 2}}
        client = _FakeClient([(("Answer.",), "end_turn", usage, content)])
        r, cl = send(c=cfg(web_search="on"), client=client)
        rec = r["calls"][0]
        check("searches used are recorded in the call record (web_searches: 2)", rec.get("web_searches") == 2 and r.get("web_searches") == 2, rec)
        check("the queries are listed for Show work", rec.get("web_queries") == ["python 3.15 release notes", "python 3.15 changes"], rec.get("web_queries"))
        check("a search-tool error arrives as a result block and is recorded, not raised",
              rec.get("web_errors") == ["max_uses_exceeded"] and r["text"] == "Answer.", rec.get("web_errors"))
        want = arch_cloud.cost("claude:claude-opus-5-5", 1000, 100) + 2 * arch_cloud.WEB_SEARCH_USD
        check("each search is priced on top of tokens", abs(r["usd"] - round(want, 5)) < 1e-6, (r["usd"], want))

        # ---- 3. pause_turn: the server paused its search loop -> resumed with the turn so far
        first = [{"type": "server_tool_use", "name": "web_search", "input": {"query": "q1"}}]
        client = _FakeClient([(("Looking ",), "pause_turn", {"input_tokens": 500, "output_tokens": 20,
                                                             "server_tool_use": {"web_search_requests": 1}}, first),
                              (("it up.",), "end_turn", {"input_tokens": 700, "output_tokens": 30,
                                                         "server_tool_use": {"web_search_requests": 1}}, [])])
        r, cl = send(c=cfg(web_search="on"), client=client)
        check("pause_turn is resumed: two requests, the second carries the paused assistant turn",
              len(cl.sent) == 2 and cl.sent[1]["messages"][-1] == {"role": "assistant", "content": first}, len(cl.sent))
        check("... text, tokens and searches add up across both", r["text"] == "Looking it up." and r["gen_tokens"] == 50
              and r["web_searches"] == 2, (r["text"], r["gen_tokens"], r.get("web_searches")))
        client = _FakeClient([(("x",), "pause_turn", {"input_tokens": 1, "output_tokens": 1}, [])])
        r, cl = send(c=cfg(web_search="off"), client=client)
        check("pause_turn without the tool is not resumed (one request)", len(cl.sent) == 1)

        # ---- 4. the tool refused by the API: same answer without it
        client = _FakeClient(fail_tools=True)
        r, cl = send(c=cfg(web_search="on"), client=client)
        check("a 400 about the tool: asked again without tools, answer kept, error recorded",
              len(cl.sent) == 1 and "tools" not in cl.sent[0] and r["text"] == "It is fine."
              and "tool_not_accepted" in (r["calls"][0].get("web_errors") or []), r["calls"][0])

        # ---- 5. a looked-up answer is never replayed from the local reply cache
        os.environ.pop("ARCHITECT_NO_CACHE", None)
        os.environ["ARCHITECT_CLOUD_CACHE_TEST"] = "1"
        try:
            client = _FakeClient()
            send(c=cfg(cloud_cache=True), client=client)
            send(c=cfg(cloud_cache=True), client=client)
            check("control: without web the second identical call is a cache hit (one request)", len(client.sent) == 1, len(client.sent))
            client = _FakeClient()
            send(c=cfg(cloud_cache=True, web_search="on"), client=client)
            send(c=cfg(cloud_cache=True, web_search="on"), client=client)
            check("with web the same call goes out each time (no cache read or write)", len(client.sent) == 2, len(client.sent))
        finally:
            os.environ["ARCHITECT_NO_CACHE"] = "1"
            os.environ.pop("ARCHITECT_CLOUD_CACHE_TEST", None)

        # ---- 6. Council: only the lead may search
        reset()
        client = _FakeClient()
        with arch_cloud.asked_for():
            arch_cloud.ultra("claude:claude-opus-5-5", [{"role": "user", "content": "q"}], cfg(), "low", None, client=client, web=True)
        with_tools = [k for k in client.sent if k.get("tools")]
        check("Council with web: exactly one request (the lead's, on Opus) carries the tool",
              len(client.sent) == 6 and len(with_tools) == 1 and with_tools[0]["model"] == "claude-opus-5-5", (len(client.sent), len(with_tools)))

        # ---- 7. the local scout
        big_ctx = "=== a.py\n" + ("def helper():\n    return 42\n" * 400)
        items = [f"pkg/m{i}.py:{10 * i}\ndef f{i}():\n    pass" for i in range(6)]
        brief = "Files: pkg/m0.py holds f0 at pkg/m0.py:0. " + "The question needs the throttle logic in f0 and its caller. " * 12
        calls = []

        def fake_local(msgs):
            calls.append(msgs)
            return brief

        user, rec = arch_chat.compose("how is throttling done?", big_ctx, items, "", cfg(), True, worker="qwen3:8b", call=fake_local)
        full_chars = len(big_ctx) + len(arch_chat._hints_block(items))
        check("big context + Claude: the local scout is asked once, with the full context", len(calls) == 1
              and big_ctx in calls[0][1]["content"])
        check("the brief replaces the big context in Claude's prompt", big_ctx not in user and "def helper()" not in user
              and "PROJECT BRIEF" in user and brief.strip()[:40] in user)
        check("only the top few code hints go along", all(items[i] in user for i in range(arch_chat.SCOUT_HINTS))
              and not any(items[i] in user for i in range(arch_chat.SCOUT_HINTS, 6)))
        check("the question itself is still last in the prompt", user.rstrip().endswith("PERSON: how is throttling done?"))
        check("scout_chars vs full_chars recorded, and the saving", rec and rec["full_chars"] == full_chars
              and 0 < rec["scout_chars"] < rec["full_chars"] and rec["saved_chars"] == full_chars - rec["scout_chars"]
              and rec["usd"] == 0.0 and rec["local"], rec)
        calls.clear()
        user, rec = arch_chat.compose("q about things", "small context", items[:2], "", cfg(), True, worker="qwen3:8b", call=fake_local)
        check("small context: no scout, full context kept", rec is None and not calls and "small context" in user)
        user, rec = arch_chat.compose("how is throttling done?", big_ctx, items, "", cfg(), False, worker="qwen3:8b", call=fake_local)
        check("a local answer never uses the scout", rec is None and not calls and big_ctx in user)
        user, rec = arch_chat.compose("how is throttling done?", big_ctx, items, "", cfg(local_scout="off"), True, worker="qwen3:8b", call=fake_local)
        check("online.local_scout = off: full context", rec is None and not calls and big_ctx in user)
        check("local_scout defaults to on", arch_chat.scout_on({}) and arch_chat.scout_on({"online": {}}))

        def boom(msgs):
            raise RuntimeError("server down")
        user, rec = arch_chat.compose("how is throttling done?", big_ctx, items, "hist line", cfg(), True, worker="qwen3:8b", call=boom)
        check("scout failure falls back to the full context", big_ctx in user and all(it in user for it in items) and "PROJECT BRIEF" not in user)
        check("... and the fallback is recorded with why and full_chars", rec and rec["scout_chars"] is None
              and "failed" in rec["why"] and rec["full_chars"] == full_chars and "fell back" in rec["label"], rec)

        def slow(msgs):
            time.sleep(2)
            return brief
        t0 = time.time()
        user, rec = arch_chat.compose("how is throttling done?", big_ctx, items, "", cfg(), True, worker="qwen3:8b", call=slow, timeout=0.3)
        check("scout timeout falls back without waiting for the model", big_ctx in user and time.time() - t0 < 1.5
              and "longer than" in (rec or {}).get("why", ""), rec)
        user, rec = arch_chat.compose("how is throttling done?", big_ctx, items, "", cfg(), True, worker="qwen3:8b", call=lambda m: "ok")
        check("an empty/too-short brief falls back", big_ctx in user and "short" in (rec or {}).get("why", ""), rec)
        user, rec = arch_chat.compose("how is throttling done?", big_ctx, items, "", cfg(), True, worker="", call=fake_local)
        check("no local worker set: falls back", big_ctx in user and "worker" in (rec or {}).get("why", ""), rec)
        user, rec = arch_chat.compose("how is throttling done?", big_ctx, items, "", cfg(), True, worker="claude:claude-opus-5-5", call=fake_local)
        check("a cloud model as the worker is never used as the scout", big_ctx in user and rec["scout_chars"] is None)
        b, why = arch_chat.scout("q", big_ctx, "", "qwen3:8b", call=lambda m: "word " * 2000)
        check("the brief is capped at 600 words", len(b.split()) == arch_chat.SCOUT_MAX_WORDS and not why, len(b.split()))

        import inspect
        check("arch_chat.ask takes web=False", inspect.signature(arch_chat.ask).parameters.get("web") is not None
              and inspect.signature(arch_chat.ask).parameters["web"].default is False)
    finally:
        reset()
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        core.set_home(old_home)
        shutil.rmtree(home, ignore_errors=True)

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' SCOUT CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' SCOUT CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
