"""Self-test for the fast lane (Settings > Server > Fast lane, arch_server.plan): with a fake llama-server caps dict and fake hardware,
fast lane on changes exactly what it promises for the WORKER tier -- the heavy tier no longer sits beside it, every layer and the whole
cache on the card (the window shrinks to the largest that fits), n-gram speculative decoding, above-normal priority on the performance
cores, a lower thinking budget, a plain-words note -- and nothing else; and fast lane off is the plan exactly as it was.
No GPU, no binary, no network, no model files: resolve_model / _arch_for / load_profile / _tokenizer are stand-ins."""
import copy
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

GB = 1024 ** 3
CAPS = {"flags": ["--host", "--port", "-m", "-c", "-ngl", "--alias", "-t", "--jinja", "--flash-attn", "-fa", "--cache-type-k", "--cache-type-v",
                  "-ub", "--parallel", "--model-draft", "-ngld", "--spec-type", "--spec-draft-n-max", "--lookup-cache-dynamic",
                  "--reasoning-budget", "--reasoning-budget-message", "--reasoning-format", "--n-cpu-moe", "--cache-ram", "--slot-save-path"],
        "fa_mode": "value", "spec_types": ["none", "draft-simple", "ngram-mod", "ngram-cache"]}
BARE_CAPS = {"flags": ["--host", "--port", "-m", "-c", "-ngl", "--alias", "-t", "--flash-attn", "-fa", "--cache-type-k", "--cache-type-v"],
             "fa_mode": "value"}
MODELS = {   # name -> (size bytes, arch)
    "fw:4b": (int(2.5 * GB), {"layers": 36, "kv_heads": 8, "head_dim": 128, "params_b": 4.0}),
    "fr:1b": (int(1.1 * GB), {"layers": 28, "kv_heads": 8, "head_dim": 128, "params_b": 1.7}),
    "fd:0.6b": (int(0.5 * GB), {"layers": 28, "kv_heads": 8, "head_dim": 128, "params_b": 0.6}),
    "fmoe:35b": (int(12.0 * GB), {"layers": 40, "kv_heads": 2, "head_dim": 128, "params_b": 35.0, "experts": 128, "experts_used": 8}),
    "fbig:14b": (int(9.0 * GB), {"layers": 48, "kv_heads": 8, "head_dim": 128, "params_b": 14.0}),
}
PROFILE = {"name": "fake6", "ctx": 8192, "kv": "q8_0", "router_cpu": True,
           "tiers": {"router": {}, "worker": {"think": True}, "heavy": {}, "draft": {}}}
HW = {"gpu": "fake card", "vram_gb": 6.0, "ram_gb": 32.0, "cpu_cores": 12, "cpu_perf_cores": 4, "vendor": "fake", "unified": False,
      "ecore_mask": 0xF00, "cpu_threads": 12}                     # 4 P-cores x 2 threads (0-7) + 4 E-cores (8-11), like an i5-13420H


def _flagmap(args):
    """[binary, '-c', '8192', '--jinja', ...] -> {'-c': '8192', '--jinja': None, ...}."""
    out, i = {}, 1
    isflag = lambda t: t.startswith("-") and not re.fullmatch(r"-?\d+(\.\d+)?", t)
    while i < len(args):
        t = args[i]
        if isflag(t):
            nxt = args[i + 1] if i + 1 < len(args) and not isflag(args[i + 1]) else None
            out[t] = nxt
            i += 2 if nxt is not None else 1
        else:
            i += 1
    return out


def _changed(a, b):
    fa, fb = _flagmap(a), _flagmap(b)
    return {f for f in set(fa) | set(fb) if fa.get(f, "<absent>") != fb.get(f, "<absent>")}


def _patch(S):
    """Stand-ins on an arch_server module; -> the originals, for _unpatch."""
    real = (S.resolve_model, S._arch_for, S.load_profile, S._tokenizer)
    S.resolve_model = lambda spec, cfg=None: ({"name": spec, "gguf": os.path.join(S.server_dir(), spec.replace(":", "_") + ".gguf"),
                                               "size": MODELS[spec][0], "source": "file"} if spec in MODELS else None)
    S._arch_for = lambda spec, show=None: dict(MODELS[spec][1]) if spec in MODELS else {}
    S.load_profile = lambda name: copy.deepcopy(PROFILE)
    S._tokenizer = lambda gguf: ("gpt2", "qwen2", 151936)
    return real


def _unpatch(S, real):
    S.resolve_model, S._arch_for, S.load_profile, S._tokenizer = real


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:400]})" if extra and not ok else ""))

    import arch_core as core
    import arch_params
    import arch_server as S
    home = tempfile.mkdtemp(prefix="archfast_")
    old_home, old_env = core.HOME, {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE")}
    core.set_home(home)
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    real = _patch(S)
    try:
        stub = os.path.join(home, "llama-server" + (".exe" if sys.platform.startswith("win") else ""))
        with open(stub, "w") as f:
            f.write("stub")
        S.save_state({})
        base = core.load_config()
        base["server"]["binary"] = stub
        base["tiers"] = {"router": "fr:1b", "worker": "fw:4b", "heavy": "", "draft": ""}

        def cfg_with(**srv):
            c = copy.deepcopy(base)
            c["server"].update(srv)
            return c

        def worker(pl):
            return next(p for p in pl["processes"] if p["tier"] == "worker")

        def tier(pl, t):
            return next((p for p in pl["processes"] if p["tier"] == t), None)

        dump = lambda pl: json.dumps(pl, sort_keys=True, default=str)

        # ---- settings: schema and defaults agree, off by default
        flat = {r[0]: r for rows in arch_params.SETTINGS.values() for r in rows}
        check("settings: server.fast_lane (off|on) and server.fast_think_budget are in the schema and in DEFAULTS, off and 256 by default",
              flat.get("server.fast_lane", (0, ""))[1] == "choice:off,on" and flat.get("server.fast_think_budget", (0, ""))[1] == "int"
              and core.DEFAULTS["server"].get("fast_lane") == "off" and core.DEFAULTS["server"].get("fast_think_budget") == 256)

        # ---- off is the plan as it was
        today = copy.deepcopy(base)
        today["server"].pop("fast_lane", None)
        today["server"].pop("fast_think_budget", None)
        p_today, p_off = S.plan(today, dict(HW), caps_=CAPS), S.plan(cfg_with(fast_lane="off"), dict(HW), caps_=CAPS)
        check("off: a config without the setting and one with fast_lane = off give byte-identical plans", dump(p_today) == dump(p_off))
        check("off: no fast-lane key anywhere (no priority, affinity, fast_lane in a process or in the plan) and no fast-lane note",
              "fast_lane" not in p_off and not any(k in p for p in p_off["processes"] for k in ("priority", "affinity", "fast_lane"))
              and not any("Fast lane" in n for n in p_off["notes"]), p_off["notes"])
        cf_off, mask_off = S.launch_opts(worker(p_off), windows=True)
        check("off: the worker is launched exactly as before (CREATE_NO_WINDOW only, no affinity); POSIX gets no flags",
              cf_off == getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) and mask_off == 0 and S.launch_opts(worker(p_off), windows=False) == (0, 0))
        # The plan as the last committed arch_server.py makes it, with the same stand-ins: the strongest form of "byte-identical".
        head = None
        try:
            r = subprocess.run(["git", "show", "HEAD:arch_server.py"], cwd=HERE, capture_output=True, timeout=20)
            if r.returncode == 0 and b"def plan(" in r.stdout:
                hp = os.path.join(home, "arch_server_head.py")
                with open(hp, "wb") as f:
                    f.write(r.stdout)
                spec = importlib.util.spec_from_file_location("arch_server_head", hp)
                head = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(head)
        except (OSError, subprocess.SubprocessError, Exception):
            head = None
        if head is not None and not hasattr(head, "_fast_fit"):
            hreal = _patch(head)
            try:
                h_plan = head.plan(today, dict(HW), caps_=CAPS)
                h_heavy = head.plan(core.deep_merge(today, {"tiers": {"heavy": "fmoe:35b"}}), dict(HW), caps_=CAPS)
            finally:
                _unpatch(head, hreal)
            n_heavy = S.plan(core.deep_merge(cfg_with(fast_lane="off"), {"tiers": {"heavy": "fmoe:35b"}}), dict(HW), caps_=CAPS)
            check("off: byte-identical to the plan the committed arch_server.py makes (worker + router, and with a MoE heavy beside)",
                  dump(h_plan) == dump(p_off) and dump(h_heavy) == dump(n_heavy))
        else:
            print("  SKIP  off vs the committed arch_server.py: could not run (no git, or HEAD already has the fast lane)")

        # ---- on: exactly the listed changes on the worker, nothing on the router
        p_on = S.plan(cfg_with(fast_lane="on", ngram="off"), dict(HW), caps_=CAPS)
        p_off2 = S.plan(cfg_with(fast_lane="off", ngram="off"), dict(HW), caps_=CAPS)
        w_on, w_off = worker(p_on), worker(p_off2)
        check("on: the worker's flags differ ONLY in speculative decoding (n-gram cache + lookup file) and the thinking budget",
              _changed(w_off["args"], w_on["args"]) == {"--spec-type", "--lookup-cache-dynamic", "--reasoning-budget"}, _changed(w_off["args"], w_on["args"]))
        fm = _flagmap(w_on["args"])
        check("on: --spec-type ngram-cache with the lookup cache, --reasoning-budget 256 (was 800)",
              fm.get("--spec-type") == "ngram-cache" and fm.get("--lookup-cache-dynamic") and fm.get("--reasoning-budget") == "256"
              and _flagmap(w_off["args"]).get("--reasoning-budget") == "800", fm)
        same = ("tier", "name", "gguf", "size_gb", "port", "ctx", "ngl", "kv", "draft", "resident", "arch", "est_gpu_gb", "full_gpu", "moe_cpu_layers", "ram_gb")
        check("on: the worker's placement facts are unchanged when it already fits (same window, layers, GB on the card, RAM part)",
              all(w_on[k] == w_off[k] for k in same), {k: (w_off[k], w_on[k]) for k in same if w_on[k] != w_off[k]})
        check("on: the worker gains exactly priority=above_normal, affinity=P-cores (0-7 here), fast_lane=True and a 'fast lane:' reason",
              set(w_on) - set(w_off) == {"priority", "affinity", "fast_lane"} and w_on["priority"] == "above_normal" and w_on["affinity"] == 0xFF
              and w_on["why"] == "fast lane: " + w_off["why"], (set(w_on) - set(w_off), w_on.get("affinity")))
        check("on: the router (on the CPU) is identical, key for key", dump(tier(p_on, "router")) == dump(tier(p_off2, "router")))
        cf_on, mask_on = S.launch_opts(w_on, windows=True)
        check("on: Windows launch adds ABOVE_NORMAL_PRIORITY_CLASS to CREATE_NO_WINDOW and pins to the P-cores; POSIX gets no priority flag",
              cf_on == (cf_off | 0x8000) and mask_on == 0xFF and S.launch_opts(w_on, windows=False) == (0, 0xFF), (hex(cf_on), mask_on))
        check("on: plan() says 'fast lane' first, in plain words, naming each change; the plan lists them",
              p_on["notes"][0].startswith("Fast lane is on") and "priority" in p_on["notes"][0] and "performance cores (processors 0-7)" in p_on["notes"][0]
              and "speculative decoding is on" in p_on["notes"][0] and "256" in p_on["notes"][0] and "power plan" in p_on["notes"][0]
              and isinstance(p_on.get("fast_lane"), list) and len(p_on["fast_lane"]) >= 4, p_on["notes"][:1])
        p_on_auto = S.plan(cfg_with(fast_lane="on"), dict(HW), caps_=CAPS)
        check("on, with the n-gram cache already on (the default): only the thinking budget changes among the flags",
              _changed(worker(p_off)["args"], worker(p_on_auto)["args"]) == {"--reasoning-budget"}, _changed(worker(p_off)["args"], worker(p_on_auto)["args"]))
        pnh = S.plan(cfg_with(fast_lane="on"), dict(HW, ecore_mask=0), caps_=CAPS)
        check("on, a CPU that is not hybrid: no pinning (affinity None), still above-normal priority, and the note says why",
              worker(pnh)["affinity"] is None and worker(pnh)["priority"] == "above_normal" and "every core of this processor" in pnh["notes"][0])

        # ---- thinking budget arithmetic
        check("thinking budget: fast 256 under 800; fast 0 -> the normal budget; never more than the normal budget; normal 0 (no limit) -> 256",
              S._think_budget({"think_budget": 800, "fast_think_budget": 256}, True) == 256 and S._think_budget({"think_budget": 800, "fast_think_budget": 0}, True) == 800
              and S._think_budget({"think_budget": 800, "fast_think_budget": 2000}, True) == 800 and S._think_budget({"think_budget": 0, "fast_think_budget": 256}, True) == 256
              and S._think_budget({"think_budget": 800, "fast_think_budget": 256}, False) == 800)

        # ---- the draft gives way to the n-gram cache
        cd = core.deep_merge(base, {"tiers": {"draft": "fd:0.6b"}})
        dw_off = worker(S.plan(core.deep_merge(cd, {"server": {"fast_lane": "off"}}), dict(HW), caps_=CAPS))
        pd_on = S.plan(core.deep_merge(cd, {"server": {"fast_lane": "on"}}), dict(HW), caps_=CAPS)
        dw_on = worker(pd_on)
        check("on: a draft model the off plan uses is replaced by the n-gram cache (measured faster on edits here), and the note says so",
              "--model-draft" in dw_off["args"] and _flagmap(dw_off["args"]).get("--spec-type") == "draft-simple"
              and "--model-draft" not in dw_on["args"] and _flagmap(dw_on["args"]).get("--spec-type") == "ngram-cache" and dw_on["draft"] is None
              and "instead of the draft model" in pd_on["notes"][0], (dw_off["args"], dw_on["args"]))

        # ---- the heavy tier no longer sits beside the worker
        ch = core.deep_merge(base, {"tiers": {"heavy": "fmoe:35b"}})
        h_off = tier(S.plan(core.deep_merge(ch, {"server": {"fast_lane": "off"}}), dict(HW), caps_=CAPS), "heavy")
        h_alone = tier(S.plan(core.deep_merge(ch, {"server": {"fast_lane": "off", "heavy_beside": "off"}}), dict(HW), caps_=CAPS), "heavy")
        ph_on = S.plan(core.deep_merge(ch, {"server": {"fast_lane": "on"}}), dict(HW), caps_=CAPS)
        h_on = tier(ph_on, "heavy")
        check("off: the MoE heavy stays loaded beside the worker (the measured 14-16 tok/s arrangement)", h_off and h_off["resident"] and "beside" in h_off["why"], h_off and h_off["why"])
        check("on: the heavy tier is exactly the 'heavy beside = off' heavy (loads only when escalated), key for key",
              h_on and not h_on["resident"] and dump(h_on) == dump(h_alone), (h_on or {}).get("why"))
        check("on: the note says the heavy model starts only when a step escalates to it", "escalates" in ph_on["notes"][0], ph_on["notes"][0])

        # ---- the largest window that keeps the whole worker on the card
        small = dict(HW, vram_gb=4.6)
        c16 = core.deep_merge(base, {"server": {"ctx": 16384}})
        s_off = worker(S.plan(core.deep_merge(c16, {"server": {"fast_lane": "off"}}), small, caps_=CAPS))
        ps_on = S.plan(core.deep_merge(c16, {"server": {"fast_lane": "on"}}), small, caps_=CAPS)
        s_on = worker(ps_on)
        arch, size = MODELS["fw:4b"][1], MODELS["fw:4b"][0]
        nxt_fits = S._ngl(size, arch, small, s_on["ctx"] + 1024, "q8_0", desktop_gb=ps_on["desk_gb"])[2]
        check("on: a 16384 window that does not fit whole shrinks to the largest 1024-multiple that does: every layer (+ output) and the whole cache on the card",
              not s_off["full_gpu"] and s_on["full_gpu"] and S.FAST_MIN_CTX <= s_on["ctx"] < 16384 and s_on["ctx"] % 1024 == 0 and not nxt_fits
              and _flagmap(s_on["args"])["-c"] == str(s_on["ctx"]) and _flagmap(s_on["args"])["-ngl"] == str(arch["layers"] + 1), (s_off["ctx"], s_on["ctx"], s_on["est_gpu_gb"]))
        check("on: the window change is said in plain words with both numbers", f"{s_on['ctx']} tokens instead of 16384" in ps_on["notes"][0], ps_on["notes"][0])
        check("on: the shrunk window changes only the window and the listed fast-lane flags",
              _changed(s_off["args"], s_on["args"]) <= {"-c", "-ngl", "--reasoning-budget"} and "-c" in _changed(s_off["args"], s_on["args"]), _changed(s_off["args"], s_on["args"]))
        cb = core.deep_merge(base, {"tiers": {"worker": "fbig:14b"}})
        b_off = worker(S.plan(core.deep_merge(cb, {"server": {"fast_lane": "off"}}), dict(HW), caps_=CAPS))
        pb_on = S.plan(core.deep_merge(cb, {"server": {"fast_lane": "on"}}), dict(HW), caps_=CAPS)
        b_on = worker(pb_on)
        check("on: a worker too big for the card even at the smallest fast window keeps its window and split, and says so plainly",
              b_on["ctx"] == b_off["ctx"] and b_on["ngl"] == b_off["ngl"] and not b_on["full_gpu"] and "does not fit on the graphics card whole" in pb_on["notes"][0])

        # ---- the binary's own caps still decide every flag
        allp = [p_on, p_on_auto, pnh, pd_on, ph_on, ps_on, pb_on]
        bad = [t for pl in allp for p in pl["processes"] for t in p["args"][1:] if t.startswith("--") and t not in CAPS["flags"]]
        check("caps: every flag in every fast-lane plan is one this build advertises", not bad, bad)
        pbare = S.plan(cfg_with(fast_lane="on"), dict(HW), caps_=BARE_CAPS)
        wb = worker(pbare)
        check("caps: a build without --spec-type / --lookup-cache-dynamic / --reasoning-budget is never given them, and the note says speculation stays off",
              not ({"--spec-type", "--lookup-cache-dynamic", "--reasoning-budget"} & set(wb["args"])) and "stays off" in pbare["notes"][0], wb["args"])

        # ---- small helpers
        check("perf_mask: hybrid 12 threads with E-cores 8-11 -> 0xFF; not hybrid -> 0; _mask_list says '0-7' and '0,2-3'",
              S.perf_mask(HW) == 0xFF and S.perf_mask(dict(HW, ecore_mask=0)) == 0 and S._mask_list(0xFF) == "0-7" and S._mask_list(0b1101) == "0,2-3")
        S.resolve_binary, rb = (lambda c=None: None), S.resolve_binary
        try:
            pn = S.plan(cfg_with(fast_lane="on"), dict(HW))
        finally:
            S.resolve_binary = rb
        check("on without llama-server: nothing changes (Ollama serves every model) and no process gets a priority", pn["mode"] == "ollama" and not pn["processes"])
    finally:
        _unpatch(S, real)
        core.set_home(old_home)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' FASTLANE CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' FASTLANE CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
