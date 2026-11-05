"""Self-tests for the Claude side (arch_cloud.py + Engine.model_for): the local reply cache, Use Claude = plan, the ultracode
helper models, the prompt-cache lifetime and its price, the daily spend cap, and effort per role.

A fake SDK client stands in for Anthropic: no key, no network, no cost. Runs against a throwaway home and data folder.
    python arch_selftest_cloud.py
"""
import json
import os
import shutil
import sys
import tempfile

import arch_core as core


class _FakeStream:
    def __init__(self, parts, stop, usage):
        self.parts, self.stop, self.usage = parts, stop, usage

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def text_stream(self):
        return iter(self.parts)

    def get_final_message(self):
        return type("M", (), {"stop_reason": self.stop, "usage": type("U", (), dict(self.usage))()})()


class _FakeClient:
    """Records every request; messages.stream(**kw) -> a context manager with text_stream and get_final_message."""
    def __init__(self, parts=("def f():\n", "    return 1\n"), stop="end_turn", usage=None):
        self.sent, self.parts, self.stop = [], parts, stop
        self.usage = usage or {"input_tokens": 1000, "output_tokens": 500}
        self.messages = self

    def stream(self, **kw):
        self.sent.append(kw)
        return _FakeStream(self.parts, self.stop, self.usage)


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    print("Cloud (Claude) checks")
    keys = ("ARCHITECT_DATA_DIR", "ARCHITECT_CLOUD_TEST", "ARCHITECT_CLOUD_CACHE_TEST", "ARCHITECT_SELFTEST", "ARCHITECT_EVAL", "ARCHITECT_NO_CACHE")
    old_env, old_home = {k: os.environ.get(k) for k in keys}, core.HOME
    home = tempfile.mkdtemp(prefix="archcloud_")
    core.set_home(home)
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "data")
    os.environ["ARCHITECT_SELFTEST"] = "1"
    os.environ["ARCHITECT_CLOUD_TEST"] = "1"                  # this suite tests the cloud module on purpose, with a fake client
    os.environ["ARCHITECT_CLOUD_CACHE_TEST"] = "1"            # ... and the reply cache on purpose
    os.environ.pop("ARCHITECT_EVAL", None)
    os.environ.pop("ARCHITECT_NO_CACHE", None)
    import arch_cloud
    import arch_params

    def reset():
        arch_cloud._SPENT.update(usd=0.0, calls=0)
        arch_cloud._HERE.role = None

    def cfg(**online):
        on = {"enabled": True, "cloud_tier": "escalation", "cloud_model": "claude:claude-opus-5-5", "cloud_cache": True}
        on.update(online)
        return {"online": on, "tiers": {"cloud": "claude:claude-opus-5-5"}, "limits": {"cloud_usd_per_run": 50, "cloud_usd_per_day": 5}}

    def refused(fn):
        try:
            fn()
            return ""
        except core.LLMError as ex:
            return str(ex) or "refused"

    try:
        # ---- 1. the local reply cache
        reset()
        msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "cache me"}]
        c_on = cfg()
        f1, f2, toks = _FakeClient(), _FakeClient(), []
        r1 = arch_cloud.chat("claude:claude-opus-5-5", msgs, cfg=c_on, client=f1)
        today_after_first = arch_cloud.spent_today()
        r2 = arch_cloud.chat("claude:claude-opus-5-5", msgs, cfg=c_on, client=f2, on_token=toks.append)
        files = os.listdir(os.path.join(core.data_dir(), "cloud_cache")) if os.path.isdir(os.path.join(core.data_dir(), "cloud_cache")) else []
        check("cache: the same request again is answered from this machine: no request, $0, marked cached (reply and record), same text, streamed",
              len(f1.sent) == 1 and f2.sent == [] and r2["usd"] == 0 and r2.get("cached") is True and r2["calls"][0].get("local_cache") is True
              and r2["calls"][0]["usd"] == 0 and r2["text"] == r1["text"] and "".join(toks) == r1["text"] and r2["transport"] == "claude"
              and r1["usd"] > 0 and len([f for f in files if f.endswith(".json")]) == 1, (f2.sent, r2.get("usd"), files))
        check("cache: a hit is not counted in today's spend", abs(arch_cloud.spent_today() - today_after_first) < 1e-12)
        f3 = _FakeClient()
        arch_cloud.chat("claude:claude-opus-5-5", msgs, cfg=cfg(cloud_cache_ttl="1h"), client=f3)   # unrelated settings: still a hit
        f4 = _FakeClient()
        with arch_cloud.asked_for(None, "low"):
            arch_cloud.chat("claude:claude-opus-5-5", msgs, cfg=c_on, client=f4)
        f5 = _FakeClient()
        arch_cloud.chat("claude:claude-sonnet-5-5", msgs, cfg=c_on, client=f5)
        check("cache: the key covers effort and model (a different effort or model asks Claude)", f3.sent == [] and len(f4.sent) == 1 and len(f5.sent) == 1)
        f6 = _FakeClient()
        arch_cloud.chat("claude:claude-opus-5-5", msgs, cfg=cfg(cloud_cache=False), client=f6)
        check("cache: Settings online.cloud_cache = off -> no hit, the request goes out", len(f6.sent) == 1)
        os.environ["ARCHITECT_EVAL"] = "1"
        f7 = _FakeClient()
        ev_off = arch_cloud._cache_on(c_on) is False
        ev_ref = refused(lambda: arch_cloud.chat("claude:claude-opus-5-5", msgs, cfg=c_on, client=f7))
        os.environ.pop("ARCHITECT_EVAL", None)
        check("cache: during eval the cache is off and no cached reply comes back (Claude itself is refused)", ev_off and "eval" in ev_ref and f7.sent == [])
        os.environ.pop("ARCHITECT_CLOUD_CACHE_TEST", None)
        f8 = _FakeClient()
        st_off = arch_cloud._cache_on(c_on) is False
        arch_cloud.chat("claude:claude-opus-5-5", msgs, cfg=c_on, client=f8)
        os.environ["ARCHITECT_CLOUD_CACHE_TEST"] = "1"
        check("cache: inside the self-tests (no explicit cache-test marker) the cache is off", st_off and len(f8.sent) == 1)
        fl = _FakeClient(stop="max_tokens")
        cut = [{"role": "user", "content": "cut off"}]
        arch_cloud.chat("claude:claude-opus-5-5", cut, cfg=c_on, client=fl)
        fl2 = _FakeClient()
        arch_cloud.chat("claude:claude-opus-5-5", cut, cfg=c_on, client=fl2)
        check("cache: a reply cut off at its token limit is never kept", len(fl2.sent) == 1)
        old = os.path.join(core.data_dir(), "cloud_cache", arch_cloud.cache_key("claude:claude-opus-5-5", "high", "S", [{"role": "user", "content": "cache me"}], None) + ".json")
        os.utime(old, (1, 1))
        f9 = _FakeClient()
        arch_cloud.chat("claude:claude-opus-5-5", msgs, cfg=c_on, client=f9)
        check("cache: an entry older than 30 days is a miss", len(f9.sent) == 1)

        # ---- 2. Use Claude = plan
        reset()
        root = tempfile.mkdtemp(prefix="p_", dir=home)
        pc = core.load_config(root)
        pc["online"] = dict(pc["online"], enabled=True, cloud_tier="plan", cloud_model="claude:claude-opus-5-5", cloud_cache=False)
        pc["tiers"] = dict(pc["tiers"], worker="w:4b", router="r:1b", cloud="")
        pc["roles"] = {r: "" for r in pc["roles"]}
        pc["limits"] = dict(pc["limits"], cloud_usd_per_run=50)
        e = core.Engine(root, "x", "g", pc, review="auto", say=lambda m: None, persist=False)
        e.cfg = pc
        got = {r: e.model_for(r) for r in ("planner", "architect", "evaluator", "reviewer", "generator", "tester", "router")}
        check("plan mode: planner, architect, judge (evaluator) and reviewer go to Claude; generator, tester and the router stay local",
              all(got[r] == "claude:claude-opus-5-5" for r in ("planner", "architect", "evaluator", "reviewer"))
              and got["generator"] == "w:4b" and got["tester"] == "w:4b" and got["router"] == "r:1b", got)
        pc["online"]["enabled"] = False
        check("plan mode: with online features off every role falls back to your local models", e.model_for("planner") == "w:4b")
        pc["online"]["enabled"] = True
        check("plan mode is a valid Settings choice and the cloud tier allows it",
              arch_params.validate_setting("online.cloud_tier", "plan") == "plan" and arch_cloud.allowed(pc)[0]
              and arch_cloud.mode(pc) == "plan")

        # ---- 6. effort per role (asked through the engine's role resolution)
        def effort_of(role, explicit=None, text=None):
            fc = _FakeClient()
            m = e.model_for(role)
            if explicit or text:
                with arch_cloud.asked_for(None, arch_cloud.effort_for(text, explicit)):
                    arch_cloud.chat(m, [{"role": "user", "content": "plan " + role}], cfg=pc, client=fc)
            else:
                arch_cloud.chat(m, [{"role": "user", "content": "plan " + role}], cfg=pc, client=fc)
            return fc.sent[0]["output_config"]["effort"]
        effs = {r: effort_of(r) for r in ("planner", "architect", "evaluator", "reviewer")}
        pc["online"]["cloud_tier"] = "all"
        effs["generator"] = effort_of("generator")
        pc["online"]["cloud_tier"] = "plan"
        check("effort per role when nobody picked one: planner/architect high, judge/reviewer medium, everything else medium",
              effs == {"planner": "high", "architect": "high", "evaluator": "medium", "reviewer": "medium", "generator": "medium"}, effs)
        check("effort: an explicit choice and 'ultrathink' still win over the role's effort",
              effort_of("reviewer", explicit="low") == "low" and effort_of("planner", text="ultrathink this") == "max")
        fr = _FakeClient()
        e.model_for("reviewer")
        arch_cloud.chat("claude:claude-opus-5-5", [{"role": "user", "content": "one"}], cfg=pc, client=fr)
        arch_cloud.chat("claude:claude-opus-5-5", [{"role": "user", "content": "two"}], cfg=pc, client=fr)
        check("effort: the role is read once (a later unrelated call is not given the reviewer's effort; it gets the old default, high)",
              [k["output_config"]["effort"] for k in fr.sent] == ["medium", "high"] and getattr(arch_cloud._HERE, "role", None) is None,
              [k["output_config"]["effort"] for k in fr.sent])

        # ---- 3. ultracode helpers on cheaper models
        reset()
        fu = _FakeClient()
        r = arch_cloud.ultra("claude:claude-opus-5-5", [{"role": "user", "content": "ultra q"}], cfg(cloud_cache=False), "high", None, client=fu)
        models = sorted(k["model"] for k in fu.sent)
        check("ultracode: three workers on Sonnet, the two skeptics on Haiku (the default), the lead on the chosen model",
              models == sorted(["claude-sonnet-5-5"] * 3 + ["claude-haiku-5-5"] * 2 + ["claude-opus-5-5"]) and r.get("agents") == 5, models)
        fu2 = _FakeClient()
        arch_cloud.ultra("claude:claude-opus-5-5", [{"role": "user", "content": "ultra q2"}],
                         cfg(cloud_cache=False, ultra_worker="claude-haiku-5-5", ultra_skeptic="claude:claude-opus-5"), "high", None, client=fu2)
        models2 = sorted(k["model"] for k in fu2.sent)
        check("ultracode: online.ultra_worker / online.ultra_skeptic choose the helper models (with or without the claude: prefix)",
              models2 == sorted(["claude-haiku-5-5"] * 3 + ["claude-opus-5"] * 2 + ["claude-opus-5-5"]), models2)

        # ---- 4. prompt-cache lifetime
        reset()
        long_user = [{"role": "system", "content": "S"}, {"role": "user", "content": "context " * 600}]
        fw = _FakeClient(usage={"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1000})
        rw = arch_cloud.chat("claude:claude-opus-5-5", long_user, cfg=cfg(cloud_cache=False, cloud_cache_ttl="1h"), client=fw)
        kw = fw.sent[0]
        want_cc = {"type": "ephemeral", "ttl": "1h"}
        check("1h cache lifetime reaches both breakpoints (system and the shared first user turn) and a write is priced 2x input",
              kw["system"][0]["cache_control"] == want_cc and kw["messages"][0]["content"][0]["cache_control"] == want_cc
              and abs(rw["usd"] - 1000 * 2.0 * 4.0 / 1e6) < 1e-9, (kw.get("system"), rw.get("usd")))
        f5m = _FakeClient(usage={"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1000})
        r5 = arch_cloud.chat("claude:claude-opus-5-5", long_user, cfg=cfg(cloud_cache=False), client=f5m)
        check("5m (default) lifetime: no ttl field is sent and a write is priced 1.25x input",
              f5m.sent[0]["system"][0]["cache_control"] == {"type": "ephemeral"} and abs(r5["usd"] - 1000 * 1.25 * 4.0 / 1e6) < 1e-9
              and abs(arch_cloud.cost("claude:claude-opus-5-5", 0, 0, 0, 1000, "1h") - 0.008) < 1e-12)

        # ---- 5. the daily spend cap
        reset()
        fd = _FakeClient()
        before = arch_cloud.spent_today()
        rd = arch_cloud.chat("claude:claude-opus-5-5", [{"role": "user", "content": "count me"}], cfg=cfg(cloud_cache=False), client=fd)
        check("daily cap: each call's cost is added to today's total in data_dir()/cloud_spend.json",
              abs(arch_cloud.spent_today() - before - rd["usd"]) < 1e-6 and os.path.isfile(os.path.join(core.data_dir(), "cloud_spend.json")))
        with open(os.path.join(core.data_dir(), "cloud_spend.json"), "w", encoding="utf-8") as f:
            json.dump({arch_cloud._today(): 5.0, "2026-01-01": 99.0}, f)
        fx = _FakeClient()
        why = refused(lambda: arch_cloud.chat("claude:claude-opus-5-5", [{"role": "user", "content": "over"}], cfg=cfg(cloud_cache=False), client=fx))
        reset()
        fy = _FakeClient()
        okd = not refused(lambda: arch_cloud.chat("claude:claude-opus-5-5", [{"role": "user", "content": "over"}],
                                                  cfg={**cfg(cloud_cache=False), "limits": {"cloud_usd_per_run": 50, "cloud_usd_per_day": 6}}, client=fy))
        check("daily cap: once today's total reaches limits.cloud_usd_per_day the call is refused before any request (a plain error); "
              "another day's spend does not count; a higher cap lets it through",
              "daily" in why and fx.sent == [] and okd and len(fy.sent) == 1, why)

        # ---- the settings schema and the defaults agree on every new setting
        paths = {f[0] for sec in arch_params.SETTINGS.values() for f in sec}
        new = ("online.cloud_cache", "online.cloud_cache_ttl", "online.ultra_worker", "online.ultra_skeptic", "limits.cloud_usd_per_day")

        def in_defaults(p):
            d = core.DEFAULTS
            for part in p.split("."):
                if not isinstance(d, dict) or part not in d:
                    return False
                d = d[part]
            return True
        check("settings: every new setting is in arch_params.SETTINGS and in arch_core.DEFAULTS, with the asked-for defaults",
              all(p in paths and in_defaults(p) for p in new) and core.DEFAULTS["online"]["cloud_cache"] is True
              and core.DEFAULTS["online"]["cloud_cache_ttl"] == "5m" and core.DEFAULTS["limits"]["cloud_usd_per_day"] == 5.0
              and core.DEFAULTS["online"]["ultra_skeptic"] == "claude:claude-haiku-5-5", [p for p in new if p not in paths or not in_defaults(p)])
    finally:
        reset()
        core.set_home(old_home)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' CLOUD CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' CLOUD CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
