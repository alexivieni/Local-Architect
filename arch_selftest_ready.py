"""Self-test for `ready` (one-click setup), `scan` (codebase profile) and the embed facade, against a scripted fake Ollama."""
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
FIXED = ("def paginate(items, page, size):\n    start = (page - 1) * size\n    return items[start:start + size]\n\n"
         "def add_tag(tag, tags=None):\n    tags = [] if tags is None else tags\n    tags.append(tag)\n    return tags\n\n"
         "def dedupe(xs):\n    seen = set()\n    out = []\n    for x in xs:\n        if x not in seen:\n            out.append(x)\n        seen.add(x)\n    return out\n")


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_core as core
    import arch_ready
    import arch_scan
    import arch_selftest_tune as tt
    import model_hub as mh
    from architect_embed import Architect

    class Fake(tt.FakeOllama):
        cfg = dict(tt.FakeOllama.cfg, empty=False, good=True, pulled=[])

        def do_GET(self):
            if self.path == "/api/tags" and self.cfg["empty"]:
                return self._json({"models": []})
            return super().do_GET()

        def do_POST(self):
            if self.path == "/api/chat":
                n = int(self.headers.get("Content-Length") or 0)
                b = json.loads(self.rfile.read(n) or b"{}")
                if b.get("tools") or "```python" in b["messages"][-1]["content"] or "ONE ```python" in b["messages"][-1]["content"]:
                    msg = {"role": "assistant", "content": ""}
                    if b.get("tools") and self.cfg["good"]:
                        msg["tool_calls"] = [{"function": {"name": "run_tests", "arguments": {"path": "tests/"}}}]
                    elif not b.get("tools"):
                        msg["content"] = f"```python\n{FIXED if self.cfg['good'] else 'x = 1'}\n```" if "ONE ```python" in b["messages"][-1]["content"] else "ok"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.end_headers()
                    self.wfile.write((json.dumps({"message": msg, "done": True, "done_reason": "stop", "prompt_eval_count": 50, "prompt_eval_duration": 10 ** 7,
                                                  "eval_count": 100, "eval_duration": 2 * 10 ** 9, "load_duration": 10 ** 9}) + "\n").encode())
                    return
                self.rfile = __import__("io").BytesIO(json.dumps(b).encode())
                self.headers.replace_header("Content-Length", str(len(json.dumps(b).encode())))
            return super().do_POST()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    old_env = {k: os.environ.get(k) for k in ("OLLAMA_HOST", "ARCHITECT_HOME", "ARCHITECT_DATA_DIR")}
    os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv.server_address[1]}"
    os.environ.pop("ARCHITECT_DATA_DIR", None)
    old_home = core.HOME
    tmp = tempfile.mkdtemp(prefix="archready_")
    quiet = lambda m: None
    import arch_probe

    PATCH = "".join(f"EDIT: greeter.py\n<<<<<<< SEARCH\n{a}\n=======\n{b}\n>>>>>>> REPLACE\n" for a, b in (
        ('    return f"hello, {name}"', '    return f"hi, {name}"'), ('    return f"hello, dear {name}"', '    return f"hi, dear {name}"'), ('    return "hello, nobody"', '    return "hi, nobody"')))
    WHOLE = "FILE: greeter.py\n```python\n" + arch_probe.FIXTURE.replace('"hello, ', '"hi, ').replace('f"hello, ', 'f"hi, ') + "```\n"

    def make_ask(patch=PATCH, whole=WHOLE):
        def ask(system, user, fmt):
            if fmt is not None:
                return '{"flow": "bugfix", "why": "it crashes"}'
            return whole if "COMPLETE file" in system else patch
        return ask
    good_ask = make_ask()
    ready_ = lambda *a, **k: arch_ready.ready(*a, **dict({"smoke": False, "ask_model": good_ask}, **k))
    hw16 = {"gpu": "Test GPU", "vendor": "nvidia", "vram_gb": 16.0, "vram_free_gb": 16.0, "ram_gb": 32.0, "ram_free_gb": 24.0, "cpu": "cpu", "os": "linux",
            "notes": [], "gpus": [], "unified": False, "cpu_cores": 8, "disk_free_gb": 200.0, "models_dir": "/x", "fingerprint": "fp-16"}
    try:
        # ---- scan
        proj = os.path.join(tmp, "proj")
        os.makedirs(os.path.join(proj, "tests"))
        open(os.path.join(proj, "app.py"), "w").write("import flask\n\ndef charge(x):\n    pass\n\nif __name__ == '__main__':\n    charge(1)\n")
        open(os.path.join(proj, "tests", "test_app.py"), "w").write("import app\n\ndef test_charge():\n    assert app.charge(1) is None\n")
        os.makedirs(os.path.join(proj, "node_modules", "x"))
        open(os.path.join(proj, "node_modules", "x", "big.js"), "w").write("x" * 9999)
        p = arch_scan.scan(proj, run_tests=True)
        check("scan finds the language, framework, test command and entry point", "Python" in p["languages"] and "Flask" in p["frameworks"] and "pytest" in (p["test_command"] or "")
              and "app.py" in p["entry_points"], p)
        check("scan ignores node_modules and counts the stub as a gate-level fake-code finding", "JavaScript" not in p["languages"] and p["fake_code"]["gate"] == 1, p["fake_code"])
        check("scan can run the tests for a real baseline", p["baseline"] and p["baseline"]["returncode"] in (0, 1, 2, 5) and p["baseline"]["command"] == p["test_command"], p["baseline"])
        arch_scan.write(proj, p)
        md = open(os.path.join(proj, ".arch", "profile.md"), encoding="utf-8").read()
        check("scan writes a readable profile the flows can load", "Test command" in md and "Fake-code findings (gate level / total): 1" in md and os.path.isfile(os.path.join(proj, ".arch", "profile.json")), md)

        eng = core.Engine(proj, "review", "x", core.load_config(proj), review="dry", say=quiet, persist=False)
        ctxt = eng.context("charge")
        check("the flows put what is known about the project (the scan profile, and any memory) at the top of their context",
              ctxt.startswith("=== what is known about this project ===") and "Test command" in ctxt and "app.py" in ctxt, ctxt[:200])
        shutil.rmtree(os.path.join(proj, ".arch"))
        check("with no profile and no memory, nothing is put at the top of the context", "what is known about this project" not in eng.context("charge"))
        arch_scan.write(proj, p)

        # ---- ready: happy path
        core.set_home(os.path.join(tmp, "home1"))
        s = ready_(proj, yes=False, say=quiet, hw=hw16)
        names = [x["name"] for x in s["steps"]]
        check("ready runs every step in order and finishes OK on a working setup", s["ok"] and names[:3] == ["environment", "models", "tune"] and any(n.startswith("toolcall") for n in names), s["steps"])
        res = mh.load_results().get(s["profile"]["builder"], {}).get("scores", {})
        check("the proof is stored in the scoreboard like any other test result", res.get("toolcall") and res["toolcall"][-1]["score"] == 100.0 and "backend_fix" in res, res.keys())
        check("ready.json is saved for the hub, and the team and tier are in it", arch_ready.load_ready().get("ok") is True and s["profile"]["tier"] in ("tiny", "small", "mid", "large"))

        # ---- ready: a builder that cannot do the job is reported, not hidden
        Fake.cfg["good"] = False
        core.set_home(os.path.join(tmp, "home2"))
        s = ready_(proj, say=quiet, hw=hw16)
        check("a builder that fails the proof makes ready say so (not ready, with a plain next step)", not s["ok"] and any("scored" in n for n in s["needs"]), (s["steps"], s["needs"]))
        Fake.cfg["good"] = True

        # ---- ready: how the builder edits files
        core.set_home(os.path.join(tmp, "home2b"))
        s = ready_(proj, say=quiet, hw=hw16, ask_model=make_ask(patch="Which strings do you mean? Would you like me to proceed?", whole=WHOLE))
        cfg_after = mh.read_config()
        check("a builder whose search/replace edits fail but whole-file edits work gets the whole-file style, and ready says so",
              s["ok"] and cfg_after["behavior"]["edit_style"] == "whole" and any(x["name"] == "edit format" and "whole" in x["detail"] for x in s["steps"]), (s["steps"], cfg_after.get("behavior")))
        import arch_autotune as at2
        at2.undo_last(say=quiet)
        check("`tune --undo` puts the edit style back", "edit_style" not in (mh.read_config().get("behavior") or {}), mh.read_config().get("behavior"))
        core.set_home(os.path.join(tmp, "home2c"))
        s = ready_(proj, say=quiet, hw=hw16, ask_model=make_ask(patch="I would change them.", whole="Sure, here is the plan."))
        check("a builder that cannot edit in either style is not 'ready', and the reason is plain", not s["ok"] and any("file blocks" in n or "neither edit style" in n for n in s["needs"]), (s["steps"], s["needs"]))
        core.set_home(os.path.join(tmp, "home2d"))
        s = ready_(proj, say=quiet, hw=hw16)
        mc = open(os.path.join(core.data_dir(), "machine.md"), encoding="utf-8").read()
        check("ready writes a machine card with hardware, per-model speed and every check", "Test GPU" in mc and "tok/s" in mc and "edit format" in mc and s.get("card"), mc[:300])

        # ---- ready: no model installed
        Fake.cfg.update(empty=True, pulled=[])
        core.set_home(os.path.join(tmp, "home3"))
        s = ready_(proj, yes=False, say=quiet, hw=hw16, pull=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not download")))
        check("with no model and no yes, ready suggests one with its size and downloads nothing", not s["ok"] and any("architect.py pull" in n for n in s["needs"]), s)
        got = []

        def fake_pull(tag, confirm=False, say=print):
            got.append((tag, confirm))
            Fake.cfg["empty"] = False
            return True
        core.set_home(os.path.join(tmp, "home4"))
        s = ready_(proj, yes=True, say=quiet, hw=hw16, pull=fake_pull)
        check("with --yes, ready downloads exactly one suggested model (confirmed) and carries on to a ready setup", len(got) == 1 and got[0][1] is True and s["ok"], (got, s["steps"]))
        Fake.cfg["empty"] = True
        asked = []
        core.set_home(os.path.join(tmp, "home5"))
        s = ready_(proj, yes=False, ask=lambda q: asked.append(q) or False, say=quiet, hw=hw16, pull=fake_pull)
        check("ready asks the person (with the size in the question) and respects a no", len(asked) == 1 and "GB" in asked[0] and len(got) == 1 and not s["ok"], asked)
        Fake.cfg["empty"] = False

        # ---- ready: Ollama not running
        os.environ["OLLAMA_HOST"] = "127.0.0.1:1"
        core.set_home(os.path.join(tmp, "home6"))
        s = ready_(proj, say=quiet, hw=hw16)
        check("when Ollama is not running ready stops early and says how to fix it", not s["ok"] and any("Ollama" in n for n in s["needs"]), s)
        os.environ["OLLAMA_HOST"] = f"127.0.0.1:{srv.server_address[1]}"

        # ---- embed
        home = os.path.join(tmp, "embed_home")
        os.makedirs(home)
        json.dump({"default_model": "fake-small:8b", "limits": {"max_revisions": 2}, "behavior": {"best_of_n": 1}}, open(os.path.join(home, "architect_config.json"), "w"))
        a = Architect(home=home, root=proj)
        cfg = a.settings()
        check("an older config file keeps its values and gets the new settings' defaults",
              cfg["limits"]["max_revisions"] == 2 and cfg["default_model"] == "fake-small:8b" and cfg["limits"]["timeout_mode"] == "auto" and cfg["behavior"]["fake_code"] == "fail", cfg["limits"])
        a.settings({"limits.num_ctx": 12288})
        check("settings are validated and written under the embedding app's own home, not next to the code",
              a.settings()["limits"]["num_ctx"] == 12288 and os.path.isfile(os.path.join(home, "architect_config.json")) and not os.path.exists(os.path.join(HERE, "hub_data", "x")))
        try:
            a.settings({"limits.num_ctx": "banana"})
            rejected = False
        except Exception:
            rejected = True
        check("an invalid setting is rejected instead of written", rejected and a.settings()["limits"]["num_ctx"] == 12288)
        st = a.tune_status()
        check("tune status is available to a host app", st["needs_tune"] is True and "never" in st["reason"], st)
        fc = a.fakecode()
        check("fakecode reports the stub in the project", fc["summary"]["gate"] == 1 and fc["findings"][0]["rule"] == "stub_body", fc["summary"])
        check("scan through the facade writes the profile", a.scan()["files"] >= 2)
        msgs = []
        b = Architect(home=home, root=proj, say=msgs.append)
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = b.run("review the code", flow="review", review="dry")
        check("run works in-process, sends progress to the host's callback and prints nothing itself",
              out["flow"] == "review" and os.path.isfile(out["report"]) and len(msgs) > 3 and buf.getvalue() == "", (out, buf.getvalue()[:200]))
        r = a.recommend()
        check("recommend returns the machine summary and suggestions, and downloads nothing", "machine" in r and isinstance(r["suggestions"], list) and Fake.cfg["pulled"] == [], r.keys())
        rec_pulled_before = list(Fake.cfg["pulled"])
        a.recommend()
        check("calling recommend again still downloads nothing", Fake.cfg["pulled"] == rec_pulled_before)
        a2 = Architect(home=os.path.join(tmp, "other_home"), root=proj)
        check("two instances keep separate homes", a.settings()["limits"]["num_ctx"] == 12288 and a2.settings()["limits"]["num_ctx"] != 12288)
        check("self_check is exposed", set(a.self_check()) >= {"ok", "has_manifest"})

        import arch_flows
        import arch_taskkit
        check("the task templates are sound: unique ids, real flows and domains, exactly one blank each", arch_taskkit.check() == [] and len(arch_taskkit.TEMPLATES) >= 20, arch_taskkit.check())
        check("every flow with a goal template can be forced from it, and every template expands to a sentence without leftover braces",
              all("{target}" not in arch_taskkit.expand(t["id"]) and t["flow"] in arch_flows.FLOWS for t in arch_taskkit.TEMPLATES))
        check("every group has templates and the destructive-looking ones default to reviewing each diff or showing only",
              all(any(t["group"] == g for t in arch_taskkit.TEMPLATES) for g in arch_taskkit.GROUPS) and all(t["review"] in ("ask", "dry") for t in arch_taskkit.all_templates()))
        eng = core.Engine(proj, "route", "x", core.load_config(proj), review="dry", say=quiet, persist=False)
        eng.cfg["roles"] = {r: "fake-small:8b" for r in __import__("arch_roles").ROLES}
        check("a request that says 'fix the security findings' goes to secure, not the generic bugfix, even when the router model cannot decide",
              arch_flows.route(eng, "fix the security findings in auth.py")[0] == "secure" and arch_flows.route(eng, "fix the Dockerfile")[0] == "config"
              and arch_flows.route(eng, "Fix: totals are off by one")[0] == "bugfix")

        # ---- every command actually runs (not just parses), against the fake Ollama, in a throwaway home
        import subprocess
        smoke_home = os.path.join(tmp, "smoke_home")
        env = dict(os.environ, ARCHITECT_HOME=smoke_home, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        env.pop("ARCHITECT_DATA_DIR", None)

        def cli(*argv, stdin=""):
            r = subprocess.run([sys.executable, os.path.join(HERE, "architect.py"), *argv], cwd=HERE, env=env, input=stdin, capture_output=True, text=True, timeout=240)
            return r.returncode, r.stdout + r.stderr
        top = subprocess.run([sys.executable, os.path.join(HERE, "architect.py"), "--help"], cwd=HERE, env=env, capture_output=True, text=True).stdout
        cmds = sorted(set(__import__("re").search(r"\{([a-z,-]+)\}", top).group(1).split(",")) - {"selftest"})
        helps = {c: cli(c, "--help")[0] for c in cmds}
        check(f"every one of the {len(cmds)} commands answers --help", len(cmds) >= 20 and all(rc == 0 for rc in helps.values()), {c: rc for c, rc in helps.items() if rc})
        rc, out = cli("doctor", "--root", proj)
        check("doctor runs and lists real checks", rc in (0, 1) and "Python" in out and "Ollama" in out, out[:300])
        rc, out = cli("assess")
        check("assess runs, reports the hardware tier and says where its numbers come from", rc == 0 and '"tuned_from"' in out and '"vram_tier"' in out and '"workloads"' in out, out[:300])
        rc, out = cli("advise", "--root", proj)
        check("advise runs and ranks next steps", rc == 0 and out.strip(), out[:300])
        rc, out = cli("tune", "--no-probe", "--no-apply")
        check("tune --no-apply reports and changes nothing", rc == 0 and "[1/4]" in out and not os.path.isfile(os.path.join(smoke_home, "architect_config.json")), out[:300])
        rc, out = cli("tune", "--no-probe")
        cfgp = os.path.join(smoke_home, "architect_config.json")
        check("tune applies settings to this install's own config", rc == 0 and os.path.isfile(cfgp) and "limits" in json.load(open(cfgp)), out[-300:])
        rc, out = cli("tune", "--undo")
        check("tune --undo restores them", rc == 0 and "restored" in out and "num_ctx_max" not in json.load(open(cfgp)).get("limits", {}), out[-200:])
        check("a second tune --undo says there is nothing to undo and exits 1", cli("tune", "--undo")[0] == 1)
        rc, out = cli("catalog")
        check("catalog lists the machine, your models and suggestions", rc == 0 and "Machine:" in out and "Your models" in out and "fake-big:30b" in out, out[:400])
        rc, out = cli("scan", "--root", proj)
        check("scan writes the project profile", rc == 0 and os.path.isfile(os.path.join(proj, ".arch", "profile.md")), out[:200])
        rc, out = cli("fakecode", "--root", proj)
        check("fakecode exits 1 and names the stub in a project that has one", rc == 1 and "stub_body" in out and "app.py" in out, out[:300])
        rc, out = cli("fakecode", "--root", proj, "--json")
        check("fakecode --json is valid JSON", rc == 1 and isinstance(json.loads(out), (list, dict)), out[:100])
        rc, out = cli("tasks")
        check("tasks lists every group", rc == 0 and all(g in out for g in ("Build", "Fix", "Quality", "Speed", "Security and infra", "Ship")), out[:200])
        rc, out = cli("tasks", "--expand", "fix-bug", "login", "fails")
        check("tasks --expand fills the blank", rc == 0 and out.startswith("Fix this bug: login fails."), out)
        check("tasks --expand with an unknown id fails clearly", cli("tasks", "--expand", "nope")[0] != 0)
        for c in (("flows",), ("templates",), ("show-config",), ("portfolio",), ("self-history",)):
            rc, out = cli(*c)
            check(f"{c[0]} runs", rc == 0, out[:200])
        rc, out = cli("route", "fix the security findings in auth.py")
        check("route picks a flow", rc == 0 and "secure" in out, out[:200])
        rc, out = cli("self-verify")
        check("self-verify prints a verdict as JSON", rc in (0, 1) and "has_manifest" in json.loads(out), out[:200])
        check("self-rollback with nothing promoted exits 1", cli("self-rollback")[0] == 1)
        FakeOllamaPulled = Fake.cfg["pulled"]
        FakeOllamaPulled.clear()
        rc, out = cli("pull", "qwen3:4b", stdin="n\n")
        check("pull asks first and a 'no' downloads nothing", rc == 1 and "Cancelled" in out and FakeOllamaPulled == [], out[:200])
        rc, out = cli("pull", "qwen3:4b", "--yes")
        check("pull --yes downloads through Ollama", rc == 0 and FakeOllamaPulled == ["qwen3:4b"], out[-200:])
        rc, out = cli("ready", "--no-proof", "--root", proj)
        check("ready runs end to end from the command line and prints its verdict", rc in (0, 1) and "READY:" in out and "[5/5]" in out, out[-400:])
    finally:
        core.set_home(old_home)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' READY/SCAN/EMBED CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' READY/SCAN/EMBED CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
