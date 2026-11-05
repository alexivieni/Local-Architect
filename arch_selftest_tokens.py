"""Self-tests for arch_tokens.py: the token count before a Claude call, its cost preview, and the local fallback.

A fake SDK client stands in for Anthropic: no key, no network, no cost. Runs against a throwaway data folder.
    python arch_selftest_tokens.py
"""
import math
import os
import shutil
import sys
import tempfile


class _Count:
    def __init__(self, n):
        self.input_tokens = n


class _FakeClient:
    """messages.count_tokens(**kw) -> an object with input_tokens; records every request. `fail` makes it raise."""
    def __init__(self, n=1234, fail=False):
        self.sent, self.n, self.fail = [], n, fail
        self.messages = self

    def count_tokens(self, **kw):
        self.sent.append(kw)
        if self.fail:
            raise RuntimeError("rate_limit_error")
        return _Count(self.n)


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    print("Token count / cost preview checks")
    keys = ("ARCHITECT_DATA_DIR", "ARCHITECT_CLOUD_TEST", "ARCHITECT_SELFTEST", "ARCHITECT_EVAL")
    old_env = {k: os.environ.get(k) for k in keys}
    tmp = tempfile.mkdtemp(prefix="archtokens_")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["ARCHITECT_SELFTEST"] = "1"
    os.environ["ARCHITECT_CLOUD_TEST"] = "1"            # this suite tests the cloud side on purpose, with a fake client
    os.environ.pop("ARCHITECT_EVAL", None)
    try:
        import arch_cloud
        import arch_tokens
        from arch_core import LLMError
        on = {"online": {"enabled": True, "cloud_tier": "chat"}}
        off = {"online": {"enabled": False}}
        model = "claude:claude-sonnet-5-5"
        msgs = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "x" * 700},
                {"role": "assistant", "content": "draft"}]

        # ---- count(): the real counter, shaped like the real call
        fc = _FakeClient(1234)
        n = arch_tokens.count(model, "You are Studio.", msgs, cfg=on, client=fc)
        kw = fc.sent[-1] if fc.sent else {}
        check("count() returns the counter's input_tokens", n == 1234, n)
        check("count() sends the API model name (no claude: prefix)", kw.get("model") == "claude-sonnet-5-5", kw.get("model"))
        check("count() folds system messages into system, like chat()", kw.get("system") == "You are Studio.\n\nBe brief.", kw.get("system"))
        check("count() drops the trailing assistant turn and keeps the user turn",
              [m["role"] for m in kw.get("messages", [])] == ["user"], kw.get("messages"))
        tools = [{"name": "t", "description": "d", "input_schema": {"type": "object"}}]
        arch_tokens.count(model, "", [{"role": "user", "content": "hi"}], cfg=on, client=fc, tools=tools)
        check("count() passes tools (they are part of the prefix) and no empty system", fc.sent[-1].get("tools") == tools and "system" not in fc.sent[-1])
        d = arch_tokens.count(model, "", [{"role": "user", "content": "hi"}], cfg=on,
                              client=type("C", (), {"messages": type("M", (), {"count_tokens": lambda self, **k: {"input_tokens": 7}})()})())
        check("count() reads a dict reply too", d == 7, d)

        # ---- count() refuses whenever a cloud request is not allowed
        fc2 = _FakeClient()
        try:
            arch_tokens.count(model, "", msgs, cfg=off, client=fc2)
            refused = False
        except LLMError:
            refused = True
        check("count() refuses with online features off, and sends nothing", refused and not fc2.sent)
        os.environ["ARCHITECT_EVAL"] = "1"
        try:
            arch_tokens.count(model, "", msgs, cfg=on, client=fc2)
            refused = False
        except LLMError:
            refused = True
        os.environ.pop("ARCHITECT_EVAL", None)
        check("count() refuses during eval", refused and not fc2.sent)
        os.environ.pop("ARCHITECT_CLOUD_TEST", None)
        try:
            arch_tokens.count(model, "", msgs, cfg=on, client=fc2)
            refused = False
        except LLMError:
            refused = True
        os.environ["ARCHITECT_CLOUD_TEST"] = "1"
        check("count() refuses inside the self-tests unless the cloud suite asks for it", refused and not fc2.sent)
        try:
            arch_tokens.count(model, "", msgs, cfg=on, client=_FakeClient(fail=True))
            refused = False
        except LLMError as e:
            refused = "did not answer" in str(e)
        check("a counter that fails is an LLMError in plain words", refused)

        # ---- the local estimate
        s, m = arch_tokens.shape("You are Studio.", msgs)
        chars = len(s) + sum(len(x["content"]) for x in m)
        check("estimate_local() is ceil(chars / 3.5) over what would be sent",
              arch_tokens.estimate_local("You are Studio.", msgs) == math.ceil(chars / 3.5), (chars, arch_tokens.estimate_local("You are Studio.", msgs)))
        blocks = [{"role": "user", "content": [{"type": "text", "text": "a" * 35, "cache_control": {"type": "ephemeral"}}]}]
        check("estimate_local() reads content blocks", arch_tokens.estimate_local("", blocks) == 10, arch_tokens.estimate_local("", blocks))
        check("estimate_local() of an empty prompt counts only the (continue) turn chat() would add",
              arch_tokens.estimate_local("", []) == math.ceil(len("(continue)") / 3.5))

        # ---- estimate_cost(): counted when allowed, labelled estimate otherwise, priced by arch_cloud.cost
        before = arch_cloud.spent()
        est = arch_tokens.estimate_cost(model, "", msgs, expected_out=800, cfg=on, client=_FakeClient(10000))
        want = arch_cloud.cost(model, 10000, 800)
        check("estimate_cost() prices the counted tokens with arch_cloud.cost()",
              est["source"] == "counted" and est["input_tokens"] == 10000 and abs(est["usd"] - round(want, 6)) < 1e-9 and not est["why"], est)
        check("a count is never added to the spend", arch_cloud.spent() == before, (before, arch_cloud.spent()))
        est_c = arch_tokens.estimate_cost(model, "", msgs, expected_out=800, cfg=on, client=_FakeClient(10000), cached_prefix=8000)
        want_c = arch_cloud.cost(model, 2000, 800, cache_read=8000)
        check("estimate_cost() with a cached prefix prices it at the cache-read rate", abs(est_c["usd_cached"] - round(want_c, 6)) < 1e-9
              and est_c["usd_cached"] < est_c["usd"], est_c)
        est_o = arch_tokens.estimate_cost(model, "You are Studio.", msgs, expected_out=800, cfg=off, client=_FakeClient(10000))
        check("offline: estimate_cost() falls back to chars/3.5, says so and why",
              est_o["source"] == "estimate" and est_o["input_tokens"] == math.ceil(chars / 3.5) and "online" in est_o["why"], est_o)
        est_f = arch_tokens.estimate_cost(model, "", msgs, cfg=on, client=_FakeClient(fail=True))
        check("a failing counter falls back to the estimate (default expected_out)",
              est_f["source"] == "estimate" and est_f["expected_out"] == arch_tokens.DEFAULT_OUT, est_f)
        line = arch_tokens.say(est)
        check("say() gives one plain line with $ and the source", line.startswith("about $") and "counted" in line and "10,000" in line, line)
        check("say() labels a fallback as an estimate", "estimate" in arch_tokens.say(est_o))
        check("arch_tokens imports no SDK at module level", "anthropic" not in open(arch_tokens.__file__, encoding="utf-8").read().split("def count")[0])
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' TOKENS CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' TOKENS CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
