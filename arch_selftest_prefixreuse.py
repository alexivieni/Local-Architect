"""Self-test: local prompt-prefix reuse across a retry loop, and per-project slot save/restore.

llama-server reuses its KV cache only up to the first changed byte. Measured 2026-11-05 on the eval traces: 30-32% of local input
reused, mostly because Engine.context re-picked the practice cards and re-ranked the files on every retry (the query carries the
failure text). With behavior.stable_context the context of one step is pinned within a run, so a 5-call retry loop against a fake
slot reuses >= 60% of its prompt; with it off the old behaviour comes back. server.slot_persist: a run saves its slot after its
first llama-server call (and at the end), restores at the start of the next run; off = only the old start/end pair.
No GPU, no model, no real server: a scripted prefix-cache fake and a fake /slots endpoint.

    py -3.12 arch_selftest_prefixreuse.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


class FakeSlots(BaseHTTPRequestHandler):
    log = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        d = json.dumps([{"id": 0, "is_processing": False}, {"id": 1, "is_processing": False}]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)

    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        self.log.append((self.path, b.get("filename")))
        d = b"{}"
        self.send_response(200)
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)


def _project(home):
    root = tempfile.mkdtemp(prefix="pr_", dir=home)
    files = {
        "calc.py": "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n",
        "parse.py": "def parse_int(s):\n    return int(s.strip())\n\n\ndef parse_list(s):\n    return [parse_int(x) for x in s.split(',')]\n",
        "report.py": "from calc import add\n\n\ndef total(rows):\n    t = 0\n    for r in rows:\n        t = add(t, r)\n    return t\n",
        "io_utils.py": "import json\n\n\ndef load(path):\n    with open(path) as f:\n        return json.load(f)\n",
        "tests/test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    }
    for rel, t in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(t)
    return root


FAILS = ["test_add failed: add(2, 3) returned -1 in calc.py",
         "parse_list in parse.py raised ValueError on ' 1, 2'",
         "report.py total() returned -6 for [1, 2, 3]",
         "io_utils.load could not open tests/data.json",
         "test_add still fails: expected 5, got -1 (calc.add)"]


def _heads(text):
    import re
    return re.findall(r"^=== .*", text, re.M)


def _loop(core, home, stable):
    """5 tries of one fix step against a fake slot that caches the previous prompt. -> (reuse after the first, systems, contexts)."""
    root = _project(home)
    cfg = core.load_config(root)
    cfg["roles"] = {r: "fake:7b" for r in cfg["roles"]}
    cfg["behavior"].update(cache="off", learn_from_runs="off", stable_context=stable)
    slot = {"last": ""}
    seen = []

    def fake(model, msgs, options=None, think=None, ka=None, to=None, fmt=None, on_token=None, host=None, **k):
        s = "".join(f"<|{m['role']}|>{m['content']}" for m in msgs)
        n = 0
        for a, b in zip(slot["last"], s):
            if a != b:
                break
            n += 1
        slot["last"] = s
        seen.append(msgs)
        return {"text": "ok", "thinking": "", "tok_s": 10.0, "gen_tokens": 2, "prompt_tokens": max(1, len(s) // 4),
                "cached_tokens": n // 4, "done_reason": "stop", "prefill_tok_s": None, "load_s": 0.0, "transport": "llama"}

    real = core.stream_chat
    core.stream_chat = fake
    e = core.Engine(root, "bugfix", "Fix add() in calc.py so total() is right", cfg, review="auto", say=lambda m: None, persist=False)
    ctxs, pn, cn = [], 0, 0
    try:
        for i, fb in enumerate(FAILS):
            if i == 3:                                   # a try edited calc.py: that file moves last, the rest stays put
                with open(os.path.join(root, "calc.py"), "a", encoding="utf-8") as f:
                    f.write("\n# tried\n")
            cx = e.context(e.goal + " " + fb, 6000)
            ctxs.append(cx)
            e.call("generator", "Fix this bug with the smallest correct change.\n\nPROJECT CONTEXT:\n" + cx + "\n\nFEEDBACK: " + fb, label=f"fix-try{i + 1}")
        # the per-call numbers the fake reported, as Engine traced them (cached_tokens / prompt_tokens)
    finally:
        core.stream_chat = real
    for m in seen[1:]:
        s = "".join(f"<|{x['role']}|>{x['content']}" for x in m)
        pn += len(s) // 4
    cn = e.reuse["tokens"]
    shutil.rmtree(root, ignore_errors=True)
    return (cn / max(1, pn)), [m[0]["content"] for m in seen], ctxs


def run():
    results = []

    def check(name, ok, detail=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n      {str(detail)[:400]}"))

    home = tempfile.mkdtemp(prefix="arch_prefix_")
    keep = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE", "OLLAMA_HOST")}
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    os.environ["OLLAMA_HOST"] = "127.0.0.1:9"
    try:
        import arch_core as core
        import arch_server
        check("DEFAULTS carry behavior.stable_context (True) and server.slot_persist (auto)",
              core.DEFAULTS["behavior"].get("stable_context") is True and core.DEFAULTS["server"].get("slot_persist") == "auto")
        old, sys_old, ctx_old = _loop(core, home, False)
        new, sys_new, ctx_new = _loop(core, home, True)
        print(f"      prefix reuse over tries 2-5: before {old:.0%}, after {new:.0%}")
        check("5-call retry loop: after the fix at least 60% of each retry's prompt comes from the cached prefix", new >= 0.6, f"{new:.2f}")
        check("...and more than with stable_context off (the old re-ranking)", new > old, f"old {old:.2f} new {new:.2f}")
        check("the system prompt is byte-identical across all five tries", len(set(sys_new)) == 1 and len(sys_new) == 5, len(set(sys_new)))
        check("the project context is byte-identical across tries while no file changed (tries 1-3)", len(set(ctx_new[:3])) == 1)
        check("after calc.py changed, everything before it in the context is unchanged (edited file goes last)",
              _heads(ctx_new[3])[-1] == "=== calc.py ===" and _heads(ctx_new[0])[-1] != "=== calc.py ==="
              and ctx_new[3].startswith(ctx_new[0][:ctx_new[0].find("=== calc.py ===")]),
              (_heads(ctx_new[0]), _heads(ctx_new[3])))

        # ---- slot save / restore per project
        srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeSlots)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        real_serving, real_keep = arch_server.serving, core.keeps_prompt_memory
        arch_server.serving = lambda m, cfg=None: {"url": url, "ctx": 8192, "tier": "worker"} if m in ("tier:worker", "tier:heavy") else None
        core.keeps_prompt_memory = lambda root: True
        try:
            import arch_cachehygiene as hyg
            proj = tempfile.mkdtemp(prefix="slotp_", dir=home)
            name = hyg.slot_name(os.path.abspath(proj))
            sdir = os.path.join(arch_server.server_dir(), "slots", "worker")
            os.makedirs(sdir, exist_ok=True)
            open(os.path.join(sdir, name), "wb").close()                # the saved prefix of the previous run here
            cfg = core.load_config(proj)
            FakeSlots.log.clear()
            e = core.Engine(proj, "bugfix", "g", cfg, review="auto", say=lambda m: None, persist=False)
            e.slot_id = 1
            ok_r = arch_server.slot_io("restore", cfg, os.path.abspath(proj), slot=e.slot_id)   # what _prepare_server does first
            rec = {"transport": "llama", "tier": "worker"}
            e._slot_early_save(rec)
            e._slot_early_save(rec)                                      # second call of the run: no second save
            log = list(FakeSlots.log)
            check("restore at the start, then one save after the first llama call, both on slot 1 with this project's file",
                  ok_r and log == [("/slots/1?action=restore", name), ("/slots/1?action=save", name)], log)
            FakeSlots.log.clear()
            cfg_off = core.load_config(proj)
            cfg_off["server"]["slot_persist"] = "off"
            e2 = core.Engine(proj, "bugfix", "g", cfg_off, review="auto", say=lambda m: None, persist=False)
            e2._slot_early_save(rec)
            check("server.slot_persist off: no save after the first call (the old start/end pair only)", FakeSlots.log == [], FakeSlots.log)
            FakeSlots.log.clear()
            e3 = core.Engine(proj, "bugfix", "g", cfg, review="auto", say=lambda m: None, persist=False)
            e3._slot_early_save({"transport": "ollama", "tier": "worker"})
            e3._slot_early_save({"transport": "llama", "tier": "router"})
            check("no save for an Ollama call or the router tier", FakeSlots.log == [], FakeSlots.log)
        finally:
            arch_server.serving, core.keeps_prompt_memory = real_serving, real_keep
            srv.shutdown()
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' PREFIX-REUSE CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' PREFIX-REUSE CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
