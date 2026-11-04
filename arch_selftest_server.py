"""Self-test for the Ollama-server module and missing-module detection. A scripted server on loopback stands in for Ollama."""
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
GB = 1024 ** 3


class Srv(BaseHTTPRequestHandler):
    loaded = ["other:13b", "mine:8b"]
    generate_calls = []
    like_ollama = True

    def log_message(self, *a):
        pass

    def _j(self, obj, code=200):
        d = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)

    def do_GET(self):
        if not Srv.like_ollama:
            return self._j({"hello": "I am a different web app"}, 404)
        if self.path == "/api/tags":
            return self._j({"models": [{"name": "mine:8b"}, {"name": "other:13b"}]})
        if self.path == "/api/version":
            return self._j({"version": "0.12.1"})
        if self.path == "/api/ps":
            return self._j({"models": [{"name": n, "size": 8 * GB, "size_vram": 8 * GB, "expires_at": "soon"} for n in Srv.loaded]})
        return self._j({}, 404)

    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/api/generate":
            Srv.generate_calls.append(b)
            if b.get("keep_alive") == 0 and b.get("model") in Srv.loaded:
                Srv.loaded.remove(b["model"])
            elif b.get("model") not in Srv.loaded:
                Srv.loaded.append(b["model"])
            return self._j({"model": b.get("model"), "done": True})
        return self._j({}, 404)


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_core as core
    import arch_deps as deps
    import arch_doctor as doc
    import arch_ollama as ol
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Srv)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    old = os.environ.get("OLLAMA_HOST")
    os.environ["OLLAMA_HOST"] = url.replace("http://", "")
    home = tempfile.mkdtemp(prefix="archsrv_")
    quiet = lambda m: None
    try:
        st = ol.status()
        check("status reports up, version, installed models and what is loaded, from the configured host and port",
              st["up"] and st["port"] == srv.server_address[1] and st["version"] == "0.12.1" and st["models"] == ["mine:8b", "other:13b"] and [m["name"] for m in st["loaded"]] == ["other:13b", "mine:8b"]
              and st["loaded"][0]["size_gb"] == 8.0 and st["loaded"][0]["vram_pct"] == 100, st)
        dead = ol.status("http://127.0.0.1:1")
        check("a closed port is reported as nothing listening, with the port number", dead["up"] is False and dead["port_open"] is False and "nothing is listening" in dead["error"] and dead["port"] == 1, dead)
        Srv.like_ollama = False
        other = ol.status()
        check("an open port that is not Ollama is told apart from a closed one", other["up"] is False and other["port_open"] is True and "not answering like Ollama" in other["error"], other)
        Srv.like_ollama = True

        said = []
        freed = ol.free_gpu({"mine:8b"}, say=said.append)
        check("free_gpu unloads only the models outside your team, through Ollama's own keep_alive 0, and says what it did",
              freed == ["other:13b"] and Srv.loaded == ["mine:8b"] and Srv.generate_calls[-1] == {"model": "other:13b", "keep_alive": 0} and any("unloaded other:13b" in m for m in said), (freed, Srv.loaded, said))
        check("a second pass has nothing to unload", ol.free_gpu({"mine:8b"}, say=quiet) == [])
        secs = ol.warm("mine:8b")
        check("warm loads a model ahead of the first call and reports the time", secs is not None and Srv.generate_calls[-1]["model"] == "mine:8b" and Srv.generate_calls[-1]["keep_alive"] == "30m")
        check("warm/unload against a dead server fail quietly (None/False), never raise", ol.warm("x", host="http://127.0.0.1:1", timeout=1) is None and ol.unload("x", host="http://127.0.0.1:1", timeout=1) is False)
        check("wait_up returns at once when the server answers and False when it never does", ol.wait_up(say=quiet) is True and ol.wait_up("http://127.0.0.1:1", seconds=1.5, say=quiet) is False)
        check("the start hint is one plain sentence for this OS", "ollama" in ol.start_hint().lower() and len(ol.start_hint()) < 200)

        Srv.loaded = ["other:13b", "mine:8b"]
        cfg = {"behavior": {"exclusive_gpu": "auto"}}
        st = ol.prepare(cfg, {"mine:8b"}, say=quiet)
        check("prepare: with exclusive_gpu=auto the run starts by freeing memory held by models outside the team", st["freed"] == ["other:13b"], st)
        Srv.loaded = ["other:13b", "mine:8b"]
        st = ol.prepare({"behavior": {"exclusive_gpu": "off"}}, {"mine:8b"}, say=quiet)
        check("prepare: exclusive_gpu=off leaves everything loaded", st["freed"] == [] and Srv.loaded == ["other:13b", "mine:8b"])
        said = []
        st = ol.prepare(cfg, set(), host="http://127.0.0.1:1", say=said.append)
        check("prepare: a server that is down is reported with how to start it, and nothing is attempted", not st["up"] and st["freed"] == [] and any("nothing is listening" in m and "ollama" in m.lower() for m in said), said)

        # ---- the engine does this once per run, before the first call, using the team in the settings
        import arch_selftest as base
        Srv.loaded = ["other:13b", "mine:8b"]
        proj = os.path.join(home, "p")
        os.makedirs(proj)
        c = core.load_config(proj)
        c["roles"] = {r: "mine:8b" for r in c["roles"]}
        c["default_model"] = "mine:8b"
        e = core.Engine(proj, "build", "x", c, review="auto", say=quiet, persist=True)
        e._prepare_server()
        check("the engine frees the GPU for the team before its first call and records it in the run report", Srv.loaded == ["mine:8b"] and any("Freed memory" in ln for ln in e.report_lines), e.report_lines[-2:])
        check("the setting exists, defaults to auto and is validated", c["behavior"]["exclusive_gpu"] == "auto" and __import__("arch_params").is_known_setting("behavior.exclusive_gpu"))

        # ---- missing modules
        check("imports are read from source: absolute names only, dotted names cut to the top package, bad syntax gives nothing",
              deps.imports_of("import os.path, yaml\nfrom bs4.element import Tag\nfrom . import local\nimport numpy as np") == ["bs4", "numpy", "os", "yaml"] and deps.imports_of("def (") == [])
        proj2 = os.path.join(home, "deps")
        os.makedirs(os.path.join(proj2, "pkg"))
        open(os.path.join(proj2, "pkg", "__init__.py"), "w").write("")
        open(os.path.join(proj2, "helper.py"), "w").write("x = 1\n")
        open(os.path.join(proj2, "app.py"), "w").write("import os\nimport json\nimport helper\nimport pkg\nimport yaml\nimport definitely_not_installed_xyz\n")
        m = deps.missing(proj2, ["app.py", "notes.txt"])
        names = [x["module"] for x in m]
        check("stdlib, installed packages and the project's own modules/packages are fine; only truly missing imports are reported, with the pip name",
              "os" not in names and "json" not in names and "helper" not in names and "pkg" not in names and "definitely_not_installed_xyz" in names
              and all(x["pip"] == "pyyaml" for x in m if x["module"] == "yaml"), m)
        fb = deps.feedback([{"file": "app.py", "module": "yaml", "pip": "pyyaml"}])
        check("the feedback names the file, the module, the pip package and tells the model not to delete tests", "app.py" in fb and "pyyaml" in fb and "Do not delete the tests" in fb)
        check("no findings means no text", deps.feedback([]) == "")

        # ---- it reaches the verifier the fix loops use
        import arch_flows as fl
        e2 = core.Engine(proj2, "build", "x", core.load_config(proj2), review="auto", say=quiet, persist=False)
        e2.created = ["app.py"]
        v = fl.verify(e2, ["app.py"], run_tests=False)
        check("verify() fails a changed file whose import cannot be satisfied, with the plain explanation", not v["ok"] and any(r["name"] == "imports are installed" and "definitely_not_installed_xyz" in r["detail"] for r in v["results"]), v["text"][:300])
        e3 = core.Engine(proj2, "build", "x", core.load_config(proj2), review="auto", say=quiet, persist=False)
        v2 = fl.verify(e3, ["app.py"], run_tests=False)
        check("a file the run did not touch is not failed for a missing import (legacy code is not the model's fault)", not any(r["name"] == "imports are installed" for r in v2["results"]))

        # ---- Architect's own code
        bad = deps.self_check()
        check("Architect's own modules all compile and import", bad == [], bad)
        tmpd = os.path.join(home, "selfbad")
        os.makedirs(tmpd)
        open(os.path.join(tmpd, "arch_zz_broken.py"), "w").write("def (\n")
        open(os.path.join(tmpd, "arch_zz_import.py"), "w").write("import module_that_is_not_there_123\n")
        bad = deps.self_check(tmpd)
        check("a module that does not compile and one that does not import are both named with the error",
              {b["module"] for b in bad} == {"arch_zz_broken.py", "arch_zz_import.py"} and any("SyntaxError" in b["error"] for b in bad) and any("ModuleNotFoundError" in b["error"] for b in bad), bad)
        d = doc.check_self_code()
        check("doctor has the self-code check and it passes here", d["ok"] and "compiles and imports" in d["name"])
        p = doc.check_ollama_port()
        check("doctor's port check shows the version and what is loaded", p["ok"] and "0.12.1" in p["detail"] and "mine:8b" in p["detail"], p)
        Srv.like_ollama = False
        p2 = doc.check_ollama_port()
        check("doctor tells you when another program owns the Ollama port", not p2["ok"] and "another program" in p2["detail"], p2)
        Srv.like_ollama = True
    finally:
        if old is None:
            os.environ.pop("OLLAMA_HOST", None)
        else:
            os.environ["OLLAMA_HOST"] = old
        srv.shutdown()
        shutil.rmtree(home, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' SERVER/DEPS CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' SERVER/DEPS CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
