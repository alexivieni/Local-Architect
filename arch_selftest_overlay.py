"""Self-test: the LocalIntel overlay is an explicit opt-in (nothing patched at import), exact prompt sections (llama-server /tokenize, else a
scaled estimate), the swallowed-error recorder and the recipe tag. No model, no GPU. run() returns True when every check passed."""
import json
import os
import re
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


class FakeTok(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path != "/tokenize":
            self.send_response(404)
            self.end_headers()
            return
        toks = list(range(len(b.get("content", "").split())))        # one token per word: easy to check
        d = json.dumps({"tokens": toks}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)


def run():
    fails, ran = [], []

    def check(name, ok, extra=None):
        ran.append(name)
        print(("ok   " if ok else "FAIL ") + name)
        if not ok:
            fails.append(name)

    tmp = tempfile.mkdtemp(prefix="archovl_")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["ARCHITECT_HOME"] = os.path.join(tmp, "home")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    os.environ.pop("ARCH_LOCALINTEL", None)
    srv = None
    try:
        import arch_core as core
        import arch_flows as fl
        orig = (core.Engine.call, core.Engine.context, core.Engine._decide, core.safe_path)
        import localintel_arch as li
        check("importing arch_flows and localintel_arch patches nothing",
              (core.Engine.call, core.Engine.context, core.Engine._decide, core.safe_path) == orig and "checkup" not in fl.FLOWS and not li._INSTALLED)
        src = open(os.path.join(HERE, "arch_flows.py"), encoding="utf-8").read()
        check("arch_flows no longer carries the import hook", "localintel-bridge" not in src and "import localintel_arch" not in src)
        root = os.path.join(tmp, "proj")
        os.makedirs(root)
        e = core.Engine(root, "build", "g", cfg=core.load_config(root), persist=False, say=lambda m: None)
        check("a default Engine does not install the layer", not li._INSTALLED and core.Engine.call is orig[0])
        check("opted_in: off by default, on by setting or env", not li.opted_in({}) and li.opted_in({"localintel": {"enabled": True}}))
        os.environ["ARCH_LOCALINTEL"] = "1"
        check("opted_in via ARCH_LOCALINTEL", li.opted_in({}))
        os.environ.pop("ARCH_LOCALINTEL")
        cfg = core.load_config(root)
        cfg["localintel"] = {"enabled": True}
        core.Engine(root, "build", "g", cfg=cfg, persist=False, say=lambda m: None)
        check("with localintel.enabled the Engine installs it, once",
              li._INSTALLED and core.Engine.call is li.li_call and core.Engine._decide is li.li_decide)
        core.Engine.call, core.Engine.context, core.Engine._decide, core.safe_path = orig        # undo for the rest of this process
        li._INSTALLED = False
        fl.FLOWS.pop("checkup", None)

        msgs = [{"role": "system", "content": "you are a careful engineer"},
                {"role": "user", "content": "## Skills\nuse small steps now\n## Project files\n=== a.py ===\nx = 1\ny = 2\n## Task\nfix the bug please"}]
        s = e._ctx_sections(msgs, 3.5, model="m", prompt_tokens=100, transport="ollama")
        check("no tokenizer: estimate, sections_exact False, scaled to the reported prompt tokens",
              s and s["sections_exact"] is False and s["est"] is True and abs(sum(s[k] for k in ("system_prompt", "skills", "memory", "tools", "project_files", "conversation")) - 100) <= 6, s)
        s = e._ctx_sections(msgs, 3.5)
        check("no token count either: plain estimate", s and s["sections_exact"] is False and s["basis"] == "estimated from characters")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeTok)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        import arch_server
        real = arch_server.serving
        arch_server.serving = lambda m, c=None: {"url": f"http://127.0.0.1:{srv.server_address[1]}", "ctx": 4096, "tier": "worker"}
        try:
            s = e._ctx_sections(msgs, 3.5, model="m", prompt_tokens=40, transport="llama")
        finally:
            arch_server.serving = real
        check("llama-server /tokenize: exact per-section counts", s and s["sections_exact"] is True and s["est"] is False and s["system_prompt"] == 5
              and s["skills"] == 6 and s["basis"] == "llama-server /tokenize", s)
        check("exact: template overhead is what no section owns", s and s["template_overhead"] == 40 - sum(
            s[k] for k in ("system_prompt", "skills", "memory", "tools", "project_files", "conversation")))
        s = e._ctx_sections(msgs, 3.5, model="m", prompt_tokens=40, transport="llama")      # real serving() finds no server: falls back
        check("tokenizer unavailable: falls back to the estimate", s and s["sections_exact"] is False)

        # swallow recorder reaches telemetry
        import arch_telemetry
        arch_telemetry.configure(enabled=True)
        core._tel_swallow("overlay.test", ValueError("boom"))
        arch_telemetry.flush()
        evs = arch_telemetry.read(kinds="error")
        check("a swallowed error is recorded as kind=error", any(x.get("where") == "overlay.test" for x in evs))
        for f in ("arch_core", "arch_flows", "arch_server", "arch_transport", "arch_cache"):
            t = open(os.path.join(HERE, f + ".py"), encoding="utf-8").read()
            n = len(re.findall(r"(?m)^\s*except Exception:\s*\n\s*pass\s*$", t))
            check(f"{f}: no bare 'except Exception: pass' left except the recorder's own", n <= 1)

        os.environ["ARCHITECT_RECIPE"] = "r1"
        check("run_flow reads the recipe from ARCHITECT_RECIPE", 'os.environ.get("ARCHITECT_RECIPE")' in src)
        os.environ.pop("ARCHITECT_RECIPE")
    finally:
        if srv:
            srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"OVERLAY: PASSED ({len(ran)} checks)" if not fails else f"OVERLAY: FAILED {len(fails)} of {len(ran)}: {fails}")
    return not fails


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
