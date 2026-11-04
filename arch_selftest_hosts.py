"""Self-test for laptop awareness and multi-machine offload. Uses two scripted Ollama servers on loopback; nothing leaves this computer."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
GB = 1024 ** 3


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_core as core
    import arch_hosts as H
    import arch_power as pw
    import arch_selftest_tune as t

    class Desk(t.FakeOllama):
        got = []

        def do_GET(self):
            if self.path == "/api/tags":
                return self._json({"models": [{"name": "big-coder:70b", "size": 40 * GB}, {"name": "nomic-embed-text", "size": GB // 4}]})
            if self.path == "/api/ps":
                return self._json({"models": [{"name": "big-coder:70b", "size": 40 * GB, "size_vram": 30 * GB}]})
            if self.path == "/api/version":
                return self._json({"version": "0.9.9"})
            return self._json({}, 404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            Desk.got.append(body.get("model"))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            self.wfile.write((json.dumps({"message": {"role": "assistant", "content": "FROM-DESK"}, "done": False}) + "\n").encode())
            self.wfile.write((json.dumps({"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop", "prompt_eval_count": 10, "prompt_eval_duration": 10 ** 7,
                                          "eval_count": 50, "eval_duration": 10 ** 9, "load_duration": 10 ** 8}) + "\n").encode())

    class Here(t.FakeOllama):
        got = []

        def do_GET(self):
            if self.path == "/api/tags":
                return self._json({"models": [{"name": "small:8b", "size": 5 * GB}]})
            return self._json({}, 404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            Here.got.append(body.get("model"))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            self.wfile.write((json.dumps({"message": {"role": "assistant", "content": "FROM-HERE"}, "done": False}) + "\n").encode())
            self.wfile.write((json.dumps({"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop", "prompt_eval_count": 10, "prompt_eval_duration": 10 ** 7,
                                          "eval_count": 50, "eval_duration": 10 ** 9, "load_duration": 10 ** 8}) + "\n").encode())

    desk = ThreadingHTTPServer(("127.0.0.1", 0), Desk)
    here = ThreadingHTTPServer(("127.0.0.1", 0), Here)
    for s in (desk, here):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    desk_url, here_url = f"http://127.0.0.1:{desk.server_address[1]}", f"http://127.0.0.1:{here.server_address[1]}"
    home = tempfile.mkdtemp(prefix="archhosts_")
    old_home, old_env = core.HOME, {k: os.environ.get(k) for k in ("OLLAMA_HOST", "ARCHITECT_HOME", "ARCHITECT_DATA_DIR")}
    core.set_home(home)
    os.environ.pop("ARCHITECT_DATA_DIR", None)
    os.environ["OLLAMA_HOST"] = here_url.replace("http://", "")
    quiet = lambda m: None
    try:
        # ---- addresses and sizes
        check("an address is completed with http and the default port", H.normalize("192.168.1.20") == "http://192.168.1.20:11434" and H.normalize("http://box:9000/") == "http://box:9000")
        try:
            H.normalize("  ")
            ok = False
        except ValueError:
            ok = True
        check("an empty address is refused", ok)
        check("model size comes from the name, MoE tags are sized between active and total", H.size_b("qwen3:32b") == 32.0 and H.size_b("x-30b-a3b:q4") < 30 and H.size_b("x-30b-a3b:q4") > 3 and H.size_b("mystery") is None)

        # ---- probing
        p = H.probe(desk_url)
        check("a reachable machine reports its models, what is loaded, its version and how long it took", p["ok"] and p["models"][0]["name"] == "big-coder:70b" and p["loaded"] == ["big-coder:70b"] and p["version"] == "0.9.9" and p["ms"] >= 0, p)
        dead = H.probe("http://127.0.0.1:1")
        check("an unreachable machine is reported plainly, not raised", dead["ok"] is False and "not reachable" in dead["error"], dead)

        # ---- plan
        cfg = {"roles": {"planner": "small:8b", "reviewer": "small:8b"}, "default_model": "small:8b", "role_hosts": {}, "hosts": {"desk": desk_url}}
        sug = H.plan(cfg, ["small:8b"], {"desk": p})
        check("the plan suggests the thinking roles go to the clearly larger model, never the embedder, and changes nothing",
              {s["role"] for s in sug} >= {"planner", "reviewer"} and all(s["model"] == "big-coder:70b" for s in sug) and cfg["role_hosts"] == {}, sug)
        check("a machine that is down contributes no suggestions", H.plan(cfg, [], {"desk": dead}) == [])
        cfg2 = dict(cfg, role_hosts={"planner": "desk"})
        check("a role already routed is not suggested again", "planner" not in {s["role"] for s in H.plan(cfg2, [], {"desk": p})})
        small = dict(cfg, roles={"planner": "big-coder:70b"}, default_model="big-coder:70b")
        check("no suggestion when the local model is already as large", H.plan(small, [], {"desk": p}) == [])

        # ---- routing through the engine
        proj = os.path.join(home, "proj")
        os.makedirs(proj)
        e = core.Engine(proj, "build", "x", core.load_config(proj), review="auto", say=quiet, persist=False)
        e.cfg["roles"] = {r: "small:8b" for r in core.arch_roles.ROLES}
        e.cfg["hosts"] = {"desk": desk_url}
        e.cfg["role_hosts"] = {"planner": "desk"}
        e.cfg["roles"]["planner"] = "big-coder:70b"
        a = e.call("planner", "plan it")
        b = e.call("generator", "write it")
        check("a routed role's call goes to that machine and the others stay here", a["text"] == "FROM-DESK" and b["text"] == "FROM-HERE" and Desk.got == ["big-coder:70b"] and Here.got == ["small:8b"], (Desk.got, Here.got))
        check("speeds are remembered per machine, so the remote model does not distort the local numbers", "big-coder:70b@desk" in e.stats and "big-coder:70b" not in e.stats, list(e.stats))
        e.pins["planner"] = "small:8b"
        c = e.call("planner", "plan again")
        check("an escalated role runs on the local model it was handed to, not on its machine", c["text"] == "FROM-HERE")
        e.pins.clear()

        # ---- fallback when the machine is off
        e2 = core.Engine(proj, "build", "y", core.load_config(proj), review="auto", say=quiet, persist=False)
        e2.cfg["roles"] = {r: "small:8b" for r in core.arch_roles.ROLES}
        e2.cfg["default_model"] = "small:8b"
        e2.cfg["hosts"] = {"desk": "http://127.0.0.1:1"}
        e2.cfg["role_hosts"] = {"planner": "desk"}
        e2.cfg["roles"]["planner"] = "big-coder:70b"
        msgs = []
        e2.say = msgs.append
        d = e2.call("planner", "plan while the desk is off")
        d2 = e2.call("planner", "and again")
        check("when the machine is unreachable the role continues on this computer, says so once and does not retry the dead machine", d["text"] == "FROM-HERE" and d2["text"] == "FROM-HERE"
              and sum("not reachable" in m for m in msgs) == 1 and "desk" in e2.down_hosts, msgs)

        # ---- route / unroute
        cfg3 = {"hosts": {"desk": desk_url}, "roles": {"planner": "small:8b"}}
        prev = H.route(cfg3, "planner", "desk", "big-coder:70b")
        check("route sets the machine and the model and returns what it replaced", cfg3["role_hosts"]["planner"] == "desk" and cfg3["roles"]["planner"] == "big-coder:70b" and prev["model"] == "small:8b")
        H.unroute(cfg3, "planner")
        check("unroute puts the role back on this computer", "planner" not in cfg3["role_hosts"])
        try:
            H.route(cfg3, "planner", "nowhere")
            ok = False
        except ValueError:
            ok = True
        check("routing to a machine that was never added is refused", ok)

        # ---- benchmark
        r = H.bench(desk_url, "big-coder:70b", say=quiet)
        check("a benchmark makes a real call on that machine and stores the speed", r["gen_tok_s"] and H.benched(desk_url, "big-coder:70b")["gen_tok_s"] == r["gen_tok_s"])

        # ---- the command line, against a real config file
        env = dict(os.environ, ARCHITECT_HOME=home, OLLAMA_HOST=here_url.replace("http://", ""))
        def cli(*a):
            return subprocess.run([sys.executable, os.path.join(HERE, "architect.py"), *a], capture_output=True, text=True, env=env, cwd=proj, timeout=60)
        o = cli("hosts", "--add", f"desk={desk_url}")
        check("hosts --add saves the machine and reports it up with its models", o.returncode == 0 and "added desk" in o.stdout and "up," in o.stdout and "big-coder:70b" in o.stdout, o.stdout + o.stderr)
        o = cli("hosts", "--route", "planner=desk:big-coder:70b")
        check("hosts --route sends a role to it", "planner -> desk using big-coder:70b" in o.stdout, o.stdout + o.stderr)
        saved = json.load(open(os.path.join(home, "architect_config.json"))) if os.path.exists(os.path.join(home, "architect_config.json")) else {}
        check("the routing is written to the settings file", saved.get("role_hosts", {}).get("planner") == "desk" and saved.get("roles", {}).get("planner") == "big-coder:70b", saved)
        o = cli("hosts", "--route", "nonsense=desk")
        check("an unknown role is refused with the list of real roles", o.returncode == 2 and "is not a role" in o.stdout, o.stdout)
        o = cli("hosts", "--remove", "desk")
        check("removing a machine brings its roles back here", "role planner is back on this computer" in o.stdout and "removed desk" in o.stdout, o.stdout)
        o = cli("status")
        check("status shows Ollama, power, tuning and machines on one screen", all(w in o.stdout for w in ("Ollama:", "Power:", "Tuned:", "Machines:")), o.stdout + o.stderr)

        # ---- laptop awareness
        bat = os.path.join(home, "ps")
        os.makedirs(os.path.join(bat, "BAT0"))
        open(os.path.join(bat, "BAT0", "status"), "w").write("Discharging\n")
        open(os.path.join(bat, "BAT0", "capacity"), "w").write("63\n")
        b = pw.battery(sys_root=bat, platform="linux")
        check("a Linux laptop on battery is detected with its charge", b == {"present": True, "on_battery": True, "pct": 63, "source": "sysfs"}, b)
        open(os.path.join(bat, "BAT0", "status"), "w").write("Charging\n")
        check("plugged in is not on battery", pw.battery(sys_root=bat, platform="linux")["on_battery"] is False)
        check("a desktop has no battery and nothing is guessed", pw.battery(sys_root=os.path.join(home, "none"), platform="linux")["present"] is False)
        mac = pw.battery(run=lambda c: (0, "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t81%; discharging; 3:10 remaining"), platform="darwin")
        check("macOS pmset output is read", mac["present"] and mac["on_battery"] and mac["pct"] == 81, mac)
        win = pw.battery(run=lambda c: (0, '{"BatteryStatus":1,"EstimatedChargeRemaining":44}'), platform="win32")
        check("Windows battery status is read", win["present"] and win["on_battery"] and win["pct"] == 44, win)
        cfgp = {"limits": {"num_ctx": 32768, "keep_alive": "30m"}, "behavior": {"best_of_n": 2, "evaluator": "each", "battery_saver": "auto"}}
        done = pw.saver(cfgp, {"present": True, "on_battery": True, "pct": 50})
        check("on battery the run gets a shorter context and keep-alive, no sampled retries and a final-only judge", cfgp["limits"]["num_ctx"] == 8192 and cfgp["limits"]["keep_alive"] == "2m"
              and cfgp["behavior"]["best_of_n"] == 1 and cfgp["behavior"]["evaluator"] == "final" and len(done) == 4, (cfgp, done))
        cfgq = {"limits": {"num_ctx": 32768, "keep_alive": "30m"}, "behavior": {"battery_saver": "off"}}
        check("battery_saver = off leaves everything as you set it, and plugged in changes nothing", pw.saver(cfgq, {"on_battery": True}) == [] and pw.saver(dict(cfgp), {"on_battery": False}) == [] and cfgq["limits"]["num_ctx"] == 32768)
        tp = pw.tips({"vram_gb": 0, "ram_gb": 16, "cpu_cores": 8}, {"present": True, "on_battery": True})
        check("tips for a small laptop name the CPU-only reality, the RAM limit, the plugged-in tune and offload",
              any("CPU" in x for x in tp) and any("16 GB of RAM" in x for x in tp) and any("plugged in" in x for x in tp) and any("hosts --add" in x for x in tp), tp)
        os.environ["OLLAMA_HOST"] = here_url.replace("http://", "")
        pe = core.Engine(proj, "build", "z", core.load_config(proj), review="auto", say=quiet, persist=False)
        check("the battery saver setting exists, defaults to auto, and is a validated setting", pe.cfg["behavior"]["battery_saver"] == "auto" and __import__("arch_params").is_known_setting("behavior.battery_saver"))
    finally:
        core.set_home(old_home)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        desk.shutdown()
        here.shutdown()
        shutil.rmtree(home, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' LAPTOP/OFFLOAD CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' LAPTOP/OFFLOAD CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
