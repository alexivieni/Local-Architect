"""Self-test for several machines: health checks with timeouts, the tiny benchmark, ranking by measured speed and health, the "fastest"
route with fallback (next machine, then this computer), and the plain-words status. Every machine is a scripted Ollama on 127.0.0.1;
nothing leaves this computer, and nothing is discovered: only the addresses the test adds are contacted."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
GB = 1024 ** 3


def _machine(label, models, eval_count, eval_ns, sleep=0.0):
    """A scripted Ollama: /api/tags lists `models`; /api/chat answers `label` at eval_count / eval_ns tok/s. Records every request."""
    class M(BaseHTTPRequestHandler):
        gets, posts = [], []

        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            M.gets.append(self.path)
            if sleep:
                time.sleep(sleep)
            if self.path == "/api/tags":
                return self._json({"models": [{"name": m, "size": 5 * GB} for m in models]})
            return self._json({}, 404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            M.posts.append(body.get("model"))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            self.wfile.write((json.dumps({"message": {"role": "assistant", "content": label}, "done": False}) + "\n").encode())
            self.wfile.write((json.dumps({"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop", "prompt_eval_count": 10,
                                          "prompt_eval_duration": 10 ** 7, "eval_count": eval_count, "eval_duration": eval_ns, "load_duration": 10 ** 8}) + "\n").encode())
    return M


def _serve(handler):
    s = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, f"http://127.0.0.1:{s.server_address[1]}"


def _stop(s):
    s.shutdown()
    s.server_close()


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_core as core
    import arch_hosts as H

    Fast = _machine("FROM-FAST", ["coder:14b", "big-coder:32b", "nomic-embed-text"], 100, 5 * 10 ** 8)     # 200 tok/s
    Slow = _machine("FROM-SLOW", ["coder:14b"], 50, 10 ** 9)                                                 # 50 tok/s
    Sleepy = _machine("FROM-SLEEPY", ["coder:14b"], 50, 10 ** 9, sleep=1.5)
    Local = _machine("FROM-HERE", ["small:8b"], 50, 10 ** 9)
    fast, fast_url = _serve(Fast)
    slow, slow_url = _serve(Slow)
    sleepy, sleepy_url = _serve(Sleepy)
    local, local_url = _serve(Local)
    running = [fast, slow, sleepy, local]

    home = tempfile.mkdtemp(prefix="archmesh_")
    keys = ("OLLAMA_HOST", "ARCHITECT_HOME", "ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE")
    old_home, old_env = core.HOME, {k: os.environ.get(k) for k in keys}
    core.set_home(home)
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "hub_data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    os.environ["OLLAMA_HOST"] = local_url.replace("http://", "")
    H.forget()
    quiet = lambda m: None
    try:
        cfg = {"hosts": {"fast": fast_url, "slow": slow_url}, "role_hosts": {}, "roles": {"planner": "coder:14b"}, "default_model": "small:8b"}

        # ---- health
        h = H.health("fast", cfg)
        check("a machine that answers is healthy, with its models and how long it took", h["ok"] is True and "coder:14b" in h["models"] and h["ms"] >= 0 and h["error"] is None, h)
        check("a health check is one listing request and never a model call", Fast.gets == ["/api/tags"] and Fast.posts == [], (Fast.gets, Fast.posts))
        saved = core.load_json(H.path())
        check("the answer is kept in hosts.json, and no half-written file is left behind",
              saved.get("health", {}).get("fast", {}).get("ok") is True and not [f for f in os.listdir(core.data_dir()) if f.endswith(".tmp")], saved)
        try:
            H.health("somewhere-else", cfg)
            ok = False
        except ValueError:
            ok = True
        check("a machine that was never added is refused, not contacted", ok)
        dead = H.health("dead", dict(cfg, hosts={"dead": "http://127.0.0.1:1"}))
        check("a machine that is off is reported plainly, not raised", dead["ok"] is False and "not reachable" in dead["error"] and dead["models"] is None, dead)
        t0 = time.time()
        sl = H.health("sleepy", dict(cfg, hosts={"sleepy": sleepy_url}), timeout=0.3)
        took = time.time() - t0
        check("a machine slower than the timeout counts as down for now, and the check does not wait for it", sl["ok"] is False and "did not answer within" in sl["error"] and took < 1.4, (sl, took))

        # ---- nothing is measured until you ask
        check("before any benchmark no machine is ranked: a guess is not a measurement", H.rank_hosts("planner", cfg) == [])
        check("so the 'fastest' route stays on this computer", H.route_of(dict(cfg, role_hosts={"planner": H.FASTEST}), "planner") is None)
        check("and nothing was asked to generate", Fast.posts == [] and Slow.posts == [], (Fast.posts, Slow.posts))

        # ---- the tiny benchmark
        b = H.bench_host("fast", cfg)
        check("bench_host picks the largest chat model there (never the embedder) and measures latency and speed",
              b["ok"] and b["model"] == "big-coder:32b" and b["gen_tok_s"] == 200.0 and b["latency_ms"] >= 0 and b["first_token_s"] is not None and b["kind"] == "tiny", b)
        check("the benchmark is stored where the others are", (H.benched(fast_url, "big-coder:32b") or {}).get("gen_tok_s") == 200.0)
        b1 = H.bench_host("fast", cfg, model="coder:14b")
        b2 = H.bench_host("slow", cfg, model="coder:14b")
        check("a named model is measured on each machine", b1["ok"] and b2["ok"] and b1["gen_tok_s"] == 200.0 and b2["gen_tok_s"] == 50.0, (b1, b2))
        check("each benchmark is one model call on that machine, no more", Fast.posts == ["big-coder:32b", "coder:14b"] and Slow.posts == ["coder:14b"], (Fast.posts, Slow.posts))
        nb = H.bench_host("slow", cfg, model="big-coder:32b")
        check("a model the machine does not have is refused before any call, and nothing is stored",
              nb["ok"] is False and "not installed" in nb["error"] and Slow.posts == ["coder:14b"] and H.benched(slow_url, "big-coder:32b") is None, nb)
        db = H.bench_host("dead", dict(cfg, hosts={"dead": "http://127.0.0.1:1"}), model="coder:14b")
        check("benchmarking a machine that is off fails plainly and stores no speed", db["ok"] is False and "not reachable" in db["error"] and H.benched("http://127.0.0.1:1", "coder:14b") is None, db)

        # ---- ranking
        gets_before = len(Fast.gets) + len(Slow.gets)
        r = H.rank_hosts("planner", cfg)
        check("machines rank by the speed measured with the role's own model", [c["name"] for c in r] == ["fast", "slow"] and r[0]["tok_s"] == 200.0 and r[0]["healthy"] is True and "200.0 tok/s" in r[0]["why"], r)
        check("ranking from measurements contacts no machine", len(Fast.gets) + len(Slow.gets) == gets_before)
        r = H.rank_hosts("planner", cfg, down={"fast"})
        check("a machine found down in this run drops to the end with the reason", [c["name"] for c in r] == ["slow", "fast"] and r[1]["healthy"] is False and "unreachable in this run" in r[1]["why"], r)
        check("a role whose model was never measured anywhere has no ranking", H.rank_hosts("reviewer", dict(cfg, roles={"reviewer": "other:7b"})) == [])
        r = H.rank_hosts("planner", cfg, live=True)
        check("a live ranking checks each machine and keeps the order", [c["name"] for c in r] == ["fast", "slow"] and all(c["healthy"] is True for c in r), r)
        n = len(Fast.gets)
        H.rank_hosts("planner", cfg, live=True)
        check("a health answer is reused for a while instead of asking on every call", len(Fast.gets) == n, Fast.gets)

        # ---- routing
        fcfg = dict(cfg, role_hosts={"planner": H.FASTEST})
        check("the 'fastest' route picks the fastest healthy machine", H.route_of(fcfg, "planner") == ("fast", fast_url))
        check("skipping a machine found down in this run gives the next one", H.route_of(fcfg, "planner", down=("fast",)) == ("slow", slow_url))
        check("skipping all of them gives this computer", H.route_of(fcfg, "planner", down=("fast", "slow")) is None)
        check("a role routed by name is unchanged", H.route_of(dict(cfg, role_hosts={"planner": "slow"}), "planner") == ("slow", slow_url))
        named = dict(cfg, hosts={"fastest": slow_url}, role_hosts={"planner": "fastest"})
        check("a machine you actually named 'fastest' is just that machine", H.route_of(named, "planner") == ("fastest", slow_url))
        c4 = {"hosts": {"fast": fast_url}, "roles": {"planner": "small:8b"}}
        prev = H.route(c4, "planner", H.FASTEST)
        check("route accepts 'fastest' and returns what it replaced", c4["role_hosts"]["planner"] == H.FASTEST and prev["role_host"] is None)
        try:
            H.route({"hosts": {}}, "planner", H.FASTEST)
            ok = False
        except ValueError as e:
            ok = "add one first" in str(e)
        check("'fastest' with no machines added is refused with what to do", ok)

        # ---- through the engine
        proj = os.path.join(home, "proj")
        os.makedirs(proj)

        def engine(tag):
            e = core.Engine(proj, "build", tag, core.load_config(proj), review="auto", say=quiet, persist=False)
            e.cfg["roles"] = {r_: "small:8b" for r_ in core.arch_roles.ROLES}
            e.cfg["roles"]["planner"] = "coder:14b"
            e.cfg["default_model"] = "small:8b"
            e.cfg["hosts"] = dict(cfg["hosts"])
            e.cfg["role_hosts"] = {"planner": H.FASTEST}
            return e

        e = engine("a")
        a = e.call("planner", "plan it")
        g = e.call("generator", "write it")
        check("a role on 'fastest' runs on the fastest machine, the others stay here", a["text"] == "FROM-FAST" and g["text"] == "FROM-HERE" and Fast.posts[-1] == "coder:14b", (a["text"], g["text"]))

        _stop(fast)                                   # the fast machine goes off while it still looks healthy (remembered answer)
        running.remove(fast)
        msgs = []
        e.say = msgs.append
        d = e.call("planner", "plan while fast is off")
        d2 = e.call("planner", "and again")
        check("a machine that dies mid-run: the role continues here, said once, the dead machine is not retried",
              d["text"] == "FROM-HERE" and d2["text"] == "FROM-HERE" and "fast" in e.down_hosts and sum("not reachable" in m for m in msgs) == 1, msgs)

        H.forget()                                    # a new run asks the machines again
        e2 = engine("b")
        c = e2.call("planner", "plan on the next best")
        check("the next run finds the fast machine down and uses the next fastest", c["text"] == "FROM-SLOW" and Slow.posts[-1] == "coder:14b", c["text"])
        check("the down machine is remembered with its reason", (core.load_json(H.path()).get("health", {}).get("fast") or {}).get("ok") is False)

        # ---- plain words
        s = H.status(fcfg)
        txt = "\n".join(s["lines"])
        check("status names each machine up or down, its fastest measured model, and where the role runs",
              "fast: down when checked" in txt and "slow: up" in txt and "50.0 tok/s" in txt and "planner: the fastest machine that answers: slow" in txt
              and s["roles"][0]["runs_on"] == "slow", txt)
        s2 = H.status(dict(cfg, role_hosts={"planner": "fast"}))
        check("a role routed by name to a machine that is down is said to run here", any("planner: runs on fast, which was down" in x for x in s2["lines"]) and s2["roles"][0]["runs_on"] is None, s2["lines"])

        _stop(slow)
        running.remove(slow)
        H.forget()
        check("with every machine off the role runs on this computer", H.route_of(fcfg, "planner") is None)
        e3 = engine("c")
        z = e3.call("planner", "plan with nothing else up")
        check("and the engine answers here", z["text"] == "FROM-HERE", z["text"])
        s3 = H.status(fcfg, live=True)
        check("a live status checks only the machines you added and says the role runs here",
              any("asks for the fastest machine" in x and "this computer" in x for x in s3["lines"]) and {x["name"] for x in s3["hosts"]} == {"fast", "slow"}, s3["lines"])
        s4 = H.status({"hosts": {}})
        check("with no machines added status says so in one line", s4["lines"] == ["No other machines added: everything runs on this computer."], s4)
        check("routing never raises, even with a broken settings value", H.route_of({"hosts": {"x": fast_url}, "role_hosts": {"planner": H.FASTEST}, "roles": {"planner": 3}}, "planner") is None)
        check("no request ever went to the sleepy machine except its own health check", Sleepy.posts == [] and Sleepy.gets == ["/api/tags"], Sleepy.gets)
    finally:
        H.forget()
        core.set_home(old_home)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for s_ in running:
            _stop(s_)
        shutil.rmtree(home, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' MESH CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' MESH CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
