"""Self-test for Engine._keep_off_card: a model that Ollama answers stays in RAM (num_gpu 0) while our worker server holds the card.
Seen 2026-11-05: the heavy MoE (planner/evaluator roles) loaded 28% onto the card through Ollama and the worker got 9 of 36 layers.

    python arch_selftest_offcard.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({extra})" if not ok and extra else ""))

    home = tempfile.mkdtemp(prefix="offcard_")
    old = os.environ.get("ARCHITECT_DATA_DIR")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "data")
    try:
        import arch_core as core
        import arch_server
        real_state, real_serving = arch_server.load_state, arch_server.serving
        state = {"processes": {"worker": {"ngl": 36, "pid": 1, "name": "w:8b"}}}
        serving = {"tier:worker": {"url": "x", "ctx": 8192, "tier": "worker"}, "w:8b": {"url": "x", "ctx": 8192, "tier": "worker"}}
        arch_server.load_state = lambda: state
        arch_server.serving = lambda m, cfg=None: serving.get(m)
        try:
            def eng(**server):
                root = os.path.join(home, f"p{len(os.listdir(home))}")
                os.makedirs(root)
                cfg = core.load_config(root)
                cfg["tiers"] = dict(cfg.get("tiers") or {}, worker="w:8b", heavy="moe:35b")
                cfg["server"] = dict(cfg.get("server") or {}, **server)
                return core.Engine(root, "build", "g", cfg, review="auto", say=lambda *a, **k: None, persist=False)
            e = eng()
            check("a model Ollama answers stays off the card while our worker server holds layers on it", e._keep_off_card("moe:35b"))
            check("the worker itself is never affected", not e._keep_off_card("w:8b"))
            check("a dense model that is not the heavy tier keeps the card (it would crawl from RAM)", not e._keep_off_card("dense:14b"))
            check("a Claude model is never affected", not e._keep_off_card("claude:opus"))
            check("server.ollama_gpu = allow leaves Ollama's own choice", not eng(ollama_gpu="allow")._keep_off_card("moe:35b"))
            state["processes"]["worker"]["ngl"] = 0
            check("a worker running on the CPU only (ngl 0) does not hold the card: no change", not eng()._keep_off_card("moe:35b"))
            state["processes"]["worker"]["ngl"] = 36
            serving.pop("tier:worker")
            check("no worker server running: Ollama decides as before", not eng()._keep_off_card("moe:35b"))
            src = open(core.__file__, encoding="utf-8").read()
            check("the call options get num_gpu 0 only for a model no llama-server serves",
                  'if not srv_ctx and self._keep_off_card(model):\n            options["num_gpu"] = 0' in src)
            check("the setting exists in DEFAULTS and in the settings schema",
                  core.DEFAULTS["server"].get("ollama_gpu") == "auto" and "server.ollama_gpu" in open(
                      os.path.join(os.path.dirname(core.__file__), "arch_params.py"), encoding="utf-8").read())
        finally:
            arch_server.load_state, arch_server.serving = real_state, real_serving
    finally:
        if old is None:
            os.environ.pop("ARCHITECT_DATA_DIR", None)
        else:
            os.environ["ARCHITECT_DATA_DIR"] = old
        shutil.rmtree(home, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' OFF-CARD CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' OFF-CARD CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
