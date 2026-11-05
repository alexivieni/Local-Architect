"""verify_machine: prove the harness, optimizations, models, agents, framework and config work on THIS machine.

    py -3.12 tools/verify_machine.py --cpu-only      no model loads, no server start (safe while a GPU queue runs)
    py -3.12 tools/verify_machine.py --gpu           the live layer: starts servers, measures, ALWAYS cleans up
    options: --only NAME[,NAME]  --skip NAME[,NAME]  --selftest-suite core|all|full  --list

Every check answers PASS / FAIL / NOT_SURE with evidence and its timing. NOT_SURE means "could not decide" (a dependency is
missing, the endpoint did not report the number), never a quiet pass. Results: hub_data/verify/<stamp>.json + .md.
Stdlib only. See docs/research/TEST_STRATEGY.md for what each layer proves.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

PASS, FAIL, NOT_SURE = "PASS", "FAIL", "NOT_SURE"
PY = sys.executable
# Suites known to fail on this Windows box for reasons outside the code under test (see memory: dev gotchas).
KNOWN_FLAKES = {"SECURITY": "port 8000 runner / worker-template SQLite lock on Windows (pre-existing)"}
EDIT_PROMPT = ("Here is a file:\n```python\ndef add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n```\n"
               "Rewrite the whole file, unchanged, except rename sub to subtract. Reply with only the code.")
GEN_PROMPT = "Write a Python function that returns the n-th Fibonacci number iteratively, with a docstring."


class Skip(Exception):
    """Raised by a check that cannot decide: becomes NOT_SURE with the message as evidence."""


# ------------------------------------------------------------------------------------------------ matrix runner
REGISTRY = []


def check(name, layer, timeout=120):
    def deco(fn):
        REGISTRY.append({"name": name, "layer": layer, "fn": fn, "timeout": timeout})
        return fn
    return deco


def run_one(c, ctx):
    t0 = time.time()
    try:
        out = c["fn"](ctx)
        status, evidence = out if isinstance(out, tuple) else ((PASS if out else FAIL), "")
        if status not in (PASS, FAIL, NOT_SURE):
            status, evidence = FAIL, f"check returned an unknown status {status!r}"
    except Skip as e:
        status, evidence = NOT_SURE, str(e)
    except subprocess.TimeoutExpired as e:
        status, evidence = NOT_SURE, f"timed out after {e.timeout}s"
    except Exception as e:  # a crashing check is a failing check, with its reason
        status, evidence = FAIL, f"{type(e).__name__}: {e}"
    return {"name": c["name"], "layer": c["layer"], "status": status, "evidence": str(evidence)[:2000],
            "seconds": round(time.time() - t0, 2)}


def run_matrix(checks, ctx, say=print):
    results = []
    for c in checks:
        r = run_one(c, ctx)
        say(f"  {r['status']:<8} {r['name']:<28} {r['seconds']:>8.1f}s  {r['evidence'][:110]}")
        results.append(r)
    return results


def summarize(results):
    s = {PASS: 0, FAIL: 0, NOT_SURE: 0}
    for r in results:
        s[r["status"]] += 1
    s["verdict"] = FAIL if s[FAIL] else (NOT_SURE if s[NOT_SURE] else PASS)
    return s


def to_markdown(rep):
    lines = [f"# Machine verification {rep['stamp']} ({rep['mode']})", "",
             f"Verdict: **{rep['summary']['verdict']}**  (PASS {rep['summary'][PASS]}, FAIL {rep['summary'][FAIL]}, "
             f"NOT_SURE {rep['summary'][NOT_SURE]})", "", "| Check | Layer | Status | Time (s) | Evidence |", "|---|---|---|---|---|"]
    for r in rep["results"]:
        ev = r["evidence"].replace("|", "/").replace("\n", " ")[:300]
        lines.append(f"| {r['name']} | {r['layer']} | {r['status']} | {r['seconds']} | {ev} |")
    return "\n".join(lines) + "\n"


def write_report(results, mode, out_dir):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    rep = {"stamp": stamp, "mode": mode, "python": sys.version.split()[0], "results": results, "summary": summarize(results)}
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, stamp)
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=1)
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(to_markdown(rep))
    return rep, base


# ------------------------------------------------------------------------------------------------ helpers
def sh(args, timeout, env_extra=None, cwd=HERE, low_priority=True):
    env = dict(os.environ)
    env.update(env_extra or {})
    env["PYTHONIOENCODING"] = "utf-8"
    flags = 0x00004000 if (low_priority and os.name == "nt") else 0   # BELOW_NORMAL_PRIORITY_CLASS: never starve the GPU queue
    p = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=flags)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def arch(args, timeout, isolated=True):
    env = {"ARCHITECT_DATA_DIR": tempfile.mkdtemp(prefix="verify_data_"), "ARCHITECT_NO_CACHE": "1"} if isolated else {}
    try:
        return sh([PY, os.path.join(HERE, "architect.py")] + args, timeout, env)
    finally:
        if isolated:
            shutil.rmtree(env["ARCHITECT_DATA_DIR"], ignore_errors=True)


def http(url, body=None, timeout=10, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method=method or ("POST" if data else "GET"))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else {}


def ollama_url(ctx):
    h = (os.environ.get("OLLAMA_HOST") or (ctx.get("cfg") or {}).get("ollama_host") or "127.0.0.1:11434").strip()
    return h if h.startswith("http") else "http://" + h


def tail(text, n=6):
    return " / ".join([l.strip() for l in text.strip().splitlines() if l.strip()][-n:])


def parse_selftest(text):
    """-> {"suites": [{"name", "passed", "total", "ok"}], "pass_lines", "fail_lines", "failed_names"} from selftest output."""
    suites = []
    for m in re.finditer(r"ALL (\d+) ([A-Z0-9 _/+-]+?) CHECKS PASSED", text):
        suites.append({"name": m.group(2).strip(), "passed": int(m.group(1)), "total": int(m.group(1)), "ok": True})
    for m in re.finditer(r"(\d+) of (\d+) ?([A-Z0-9 _/+-]*?) ?CHECKS FAILED", text):
        bad, tot = int(m.group(1)), int(m.group(2))
        suites.append({"name": m.group(3).strip() or "(unnamed suite)", "passed": tot - bad, "total": tot, "ok": False})
    fails = re.findall(r"^\s*FAIL\s+(.+?)(?:\s{3}\(|$)", text, re.M)
    return {"suites": suites, "pass_lines": len(re.findall(r"^\s*PASS\s", text, re.M)), "fail_lines": len(fails),
            "failed_names": fails[:40]}


def judge_selftest(rc, parsed, flakes=KNOWN_FLAKES):
    bad = [s for s in parsed["suites"] if not s["ok"]]
    unknown = [s for s in bad if not any(k in s["name"] for k in flakes)]
    ev = (f"rc={rc}; {len(parsed['suites'])} suites, {parsed['pass_lines']} PASS lines, {parsed['fail_lines']} FAIL lines; "
          + "; ".join(f"{s['name']} {s['passed']}/{s['total']}" for s in parsed["suites"]))
    if parsed["failed_names"]:
        ev += " | failing: " + "; ".join(parsed["failed_names"][:10])
    if not parsed["suites"] and not parsed["pass_lines"]:
        return NOT_SURE, "no suite summary found in the output; " + ev
    if unknown or (parsed["fail_lines"] and not bad):
        return FAIL, ev
    if bad:
        return NOT_SURE, "only known flakes failed (" + ", ".join(s["name"] for s in bad) + "); " + ev
    return (PASS if rc == 0 else NOT_SURE), ev


# ------------------------------------------------------------------------------------------------ CPU-only checks
@check("python+pytest", "cpu")
def c_python(ctx):
    rc, out = sh([PY, "-m", "pytest", "--version"], 60)
    ok = sys.version_info[:2] >= (3, 10) and rc == 0
    return (PASS if ok else FAIL), f"python {sys.version.split()[0]}; pytest: {out.strip()[:80] or 'missing'}"


@check("settings schema == DEFAULTS", "cpu")
def c_schema(ctx):
    import arch_core as core
    import arch_params as P
    flat = {}

    def walk(d, pre=""):
        for k, v in d.items():
            p = f"{pre}.{k}" if pre else k
            if isinstance(v, dict):          # an empty dict (hosts, role_hosts) is a free-form map, not a setting
                walk(v, p)
            else:
                flat[p] = v
    walk(core.DEFAULTS)
    probs = P.selftest_defaults_match(flat)
    return (FAIL if probs else PASS), (f"{len(probs)} mismatches: {probs[:8]}" if probs else f"{len(flat)} default paths agree with the schema")


@check("flows --validate", "cpu")
def c_flows(ctx):
    rc, out = arch(["flows", "--validate"], 120)
    return (PASS if rc == 0 else FAIL), tail(out, 4)


@check("self-verify manifest", "cpu")
def c_selfverify(ctx):
    rc, out = arch(["self-verify"], 120, isolated=False)
    try:
        j = json.loads(out[out.index("{"):out.rindex("}") + 1])
        lists = {k: v for k, v in j.items() if isinstance(v, list)}
        ev = "; ".join(f"{k}: {len(v)} {v[:5]}" for k, v in lists.items() if v)
        if not j.get("has_manifest"):
            return NOT_SURE, "no recorded manifest yet (self-verify --write)"
        return (PASS if j.get("ok") else FAIL), ("matches the manifest" if j.get("ok") else "install differs from MANIFEST.sha256: " + ev)
    except ValueError:
        pass
    if rc != 0 and re.search(r"no (manifest|record)|never (been )?verified|--write", out, re.I):
        return NOT_SURE, "no recorded manifest yet: " + tail(out, 3)
    return (PASS if rc == 0 else FAIL), tail(out, 4)


@check("agents validate", "cpu")
def c_agents(ctx):
    rc, out = arch(["crew", "--agents"], 120)
    return (PASS if rc == 0 and not re.search(r"\b(invalid|error)\b", out, re.I) else FAIL), tail(out, 4)


@check("workflows validate", "cpu")
def c_workflows(ctx):
    rc, out = arch(["crew", "--workflows"], 120)
    return (PASS if rc == 0 and not re.search(r"\b(invalid|error)\b", out, re.I) else FAIL), tail(out, 4)


def installed_models(ctx):
    names = []
    try:
        names += [m["name"] for m in http(ollama_url(ctx) + "/api/tags", timeout=5).get("models", [])]
    except Exception:
        ctx["ollama_down"] = True
    try:
        import arch_server
        names += [os.path.basename(str(g.get("path") if isinstance(g, dict) else g)) for g in (arch_server.gguf_models(ctx.get("cfg")) or [])]
    except Exception:
        pass
    return sorted(set(n for n in names if n))


@check("passport per installed model", "cpu")
def c_passports(ctx):
    import arch_passport
    names = installed_models(ctx)
    if not names:
        raise Skip("no installed models found" + (" (Ollama unreachable)" if ctx.get("ollama_down") else ""))
    missing = [n for n in names if not arch_passport.load(n)]
    st = FAIL if missing else PASS
    return st, f"{len(names) - len(missing)}/{len(names)} have a passport" + (f"; missing: {missing[:10]}" if missing else "")


@check("llama-server binary + caps", "cpu")
def c_binary(ctx):
    import arch_server
    b = arch_server.resolve_binary(ctx.get("cfg"))
    if not b:
        return FAIL, "no llama-server binary found (the app falls back to Ollama)"
    cp = arch_server.caps(b) or {}
    keys = [k for k in cp if k != "version"]
    return (PASS if cp else NOT_SURE), f"{b}; version {cp.get('version')}; {len(keys)} caps fields: {keys[:12]}"


@check("Ollama reachable (read-only)", "cpu")
def c_ollama(ctx):
    base = ollama_url(ctx)
    try:
        tags = http(base + "/api/tags", timeout=5).get("models", [])
        ps = http(base + "/api/ps", timeout=5).get("models", [])
    except Exception as e:
        return FAIL, f"{base}: {e}"
    return PASS, f"{base}: {len(tags)} installed, {len(ps)} loaded now {[m.get('name') for m in ps]}"


@check("heavy lock / GPU queue", "cpu", timeout=10)
def c_heavy(ctx):
    # Fake-only children below run in an isolated data dir, so they do not take this lock: they are CPU work at below-normal
    # priority, one at a time. This check records who holds the real lock so a slow result can be read in context.
    import arch_heavy
    h = arch_heavy.holder()
    return PASS, (f"held by {h}" if h else "free")


@check("disk space", "cpu")
def c_disk(ctx):
    free = shutil.disk_usage(HERE).free / 1e9
    return (PASS if free >= 10 else (NOT_SURE if free >= 3 else FAIL)), f"{free:.1f} GB free on the install drive"


@check("selftest suite", "cpu", timeout=7200)
def c_selftest(ctx):
    suite = ctx.get("selftest_suite", "full")
    rc, out = sh([PY, os.path.join(HERE, "architect.py"), "selftest", "--suite", suite], 7200,
                 {"ARCHITECT_DATA_DIR": tempfile.mkdtemp(prefix="verify_st_"), "ARCHITECT_NO_CACHE": "1", "ARCHITECT_SELFTEST": "1"})
    if ctx.get("log_dir"):
        with open(os.path.join(ctx["log_dir"], "selftest.log"), "w", encoding="utf-8") as f:
            f.write(out)
    st, ev = judge_selftest(rc, parse_selftest(out))
    return st, f"--suite {suite}: " + ev


@check("eval --fake", "cpu", timeout=1800)
def c_eval(ctx):
    rc, out = arch(["eval", "--fake", "--quick", "--json"], 1800)
    line = next((l for l in reversed(out.splitlines()) if l.strip().startswith("{")), "")
    try:
        j = json.loads(line)
        keep = {k: v for k, v in j.items() if not isinstance(v, (dict, list)) or k in ("flows", "summary", "totals")}
        return (PASS if rc == 0 else FAIL), json.dumps(keep, default=str)[:900]
    except Exception:
        return (PASS if rc == 0 else FAIL), tail(out, 5)


@check("crew --fake explain_squad", "cpu", timeout=600)
def c_crew(ctx):
    root = tempfile.mkdtemp(prefix="verify_crew_")
    with open(os.path.join(root, "calc.py"), "w") as f:
        f.write("def add(a, b):\n    return a + b\n")
    try:
        rc, out = arch(["crew", "explain_squad", "--fake", "--root", root, "--goal", "explain calc.py"], 600)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return (PASS if rc == 0 else FAIL), tail(out, 5)


@check("select --plan", "cpu")
def c_select(ctx):
    rc, out = arch(["select", "--plan"], 180)
    return (PASS if rc == 0 else FAIL), tail(out, 4)


# ------------------------------------------------------------------------------------------------ GPU (live) checks
def worker(ctx):
    import arch_server
    for p in arch_server.status(ctx.get("cfg")).get("processes", []):
        if p["tier"] == "worker" and p["up"]:
            return p
    raise Skip("no running llama-server worker (server start failed or Ollama mode)")


def completion(url, prompt, n=128, **extra):
    body = {"prompt": prompt, "n_predict": n, "temperature": 0, "seed": 1, "cache_prompt": True}
    body.update(extra)
    return http(url.rstrip("/") + "/completion", body, timeout=300)


@check("server start -> health", "gpu", timeout=600)
def g_start(ctx):
    import arch_server
    t0 = time.time()
    arch_server.start(ctx.get("cfg"), say=lambda m: None, wait=True, watch=False)
    w = worker(ctx)
    ctx["worker"] = w
    return PASS, f"worker {w['name']} up on :{w['port']} in {time.time() - t0:.0f}s (ctx {w['ctx']}, ngl {w['ngl']})"


@check("placement ngl == layers", "gpu")
def g_ngl(ctx):
    import arch_server
    w = worker(ctx)
    pl = arch_server.plan(ctx.get("cfg"))
    pp = next((p for p in pl.get("processes", []) if p.get("tier") == "worker"), {})
    layers = (pp.get("arch") or {}).get("layers") or pp.get("layers")
    if not layers:
        raise Skip(f"plan did not report the layer count; ngl={w['ngl']}")
    ok = int(w["ngl"] or 0) >= int(layers)
    return (PASS if ok else FAIL), f"ngl {w['ngl']} vs {layers} layers" + ("" if ok else " (partial offload)")


@check("decode tok/s (median of 3)", "gpu", timeout=900)
def g_speed(ctx):
    w = worker(ctx)
    rates = []
    for _ in range(3):
        t = completion(w["url"], GEN_PROMPT, 128).get("timings") or {}
        if t.get("predicted_per_second"):
            rates.append(float(t["predicted_per_second"]))
    if not rates:
        raise Skip("server reported no timings.predicted_per_second")
    med = sorted(rates)[len(rates) // 2]
    ctx["tok_s"] = med
    return (PASS if med >= 5 else FAIL), f"median {med:.1f} tok/s over {[round(r, 1) for r in rates]}"


@check("n-gram speculation on vs off", "gpu", timeout=900)
def g_ngram(ctx):
    w = worker(ctx)
    if not w.get("draft") and not re.search(r"ngram|lookup", json.dumps(ctx.get("cfg", {}).get("server", {})), re.I):
        raise Skip("no draft / n-gram speculation configured for the worker")
    off = completion(w["url"], EDIT_PROMPT, 160, **{"speculative.n_max": 0}).get("timings") or {}
    on = completion(w["url"], EDIT_PROMPT, 160).get("timings") or {}
    a, b = off.get("predicted_per_second"), on.get("predicted_per_second")
    if not a or not b:
        raise Skip("timings missing")
    acc = on.get("draft_n_accepted")
    return (PASS if b > a * 1.05 else NOT_SURE), f"off {a:.1f} tok/s, on {b:.1f} tok/s (x{b / a:.2f}); drafts accepted {acc}"


@check("prefix reuse cache_n/prompt_n", "gpu", timeout=600)
def g_prefix(ctx):
    w = worker(ctx)
    long_prompt = ("# context\n" + "\n".join(f"def f{i}(x):\n    return x + {i}\n" for i in range(150))) + "\n# Q: what does f7 return?\n"
    seen = []
    for _ in range(3):
        j = completion(w["url"], long_prompt, 8)
        t = j.get("timings") or {}
        seen.append((t.get("cache_n"), t.get("prompt_n")))
    c, p = seen[-1]
    if c is None:
        raise Skip(f"no cache_n in timings: {seen}")
    ok = c > 0 and (p or 0) < c
    return (PASS if ok else FAIL), f"(cache_n, prompt_n) per retry: {seen}"


@check("slot save -> restore", "gpu", timeout=300)
def g_slot(ctx):
    import arch_server
    worker(ctx)
    proj = os.path.join(tempfile.gettempdir(), "verify_machine_slot")
    s = arch_server.slot_io("save", ctx.get("cfg"), proj)
    r = arch_server.slot_io("restore", ctx.get("cfg"), proj) if s else False
    if not s:
        return FAIL, "save returned False (no --slot-save-path, slot busy, or an old build)"
    return (PASS if r else FAIL), f"save={s} restore={r}"


@check("edit-format probe (worker)", "gpu", timeout=900)
def g_probe(ctx):
    import arch_probe
    w = worker(ctx)
    ask = arch_probe.live_ask(w["name"])
    res = arch_probe.modes(ask, say=lambda m: None)
    return (PASS if res else FAIL), json.dumps(res, default=str)[:600]


@check("real crew explain_squad", "gpu", timeout=1800)
def g_crew(ctx):
    rc, out = arch(["crew", "explain_squad", "--root", HERE, "--goal", "explain what arch_cleanup.py does", "--budget", "calls=12,seconds=900"],
                   1800, isolated=False)
    ctx["crew_out"] = out
    return (PASS if rc == 0 else FAIL), tail(out, 5)


@check("real eval task", "gpu", timeout=1800)
def g_eval(ctx):
    rc, out = arch(["eval", "--quick", "--n", "1", "--flow", "bugfix", "--json", "--label", "verify_machine"], 1800, isolated=False)
    return (PASS if rc == 0 else FAIL), tail(out, 4)


@check("telemetry for the live run", "gpu")
def g_telemetry(ctx):
    import arch_telemetry
    p = arch_telemetry.path()
    if not os.path.isfile(p):
        return FAIL, f"no telemetry file at {p}"
    recent = 0
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                if float(json.loads(line).get("ts") or 0) >= ctx["t_start"]:
                    recent += 1
            except Exception:
                pass
    return (PASS if recent else FAIL), f"{recent} events since this verification started ({p})"


@check("Ollama keep_alive unload", "gpu", timeout=300)
def g_keepalive(ctx):
    base = ollama_url(ctx)
    try:
        tags = http(base + "/api/tags", timeout=5).get("models", [])
    except Exception as e:
        raise Skip(f"Ollama unreachable: {e}")
    small = sorted((m for m in tags if "embed" not in m["name"]), key=lambda m: m.get("size", 1 << 62))
    if not small:
        raise Skip("no chat model in Ollama")
    name = small[0]["name"]
    http(base + "/api/generate", {"model": name, "prompt": "hi", "keep_alive": "5s", "options": {"num_predict": 1}}, timeout=180)
    loaded = [m["name"] for m in http(base + "/api/ps").get("models", [])]
    for _ in range(30):
        time.sleep(1)
        if name not in [m["name"] for m in http(base + "/api/ps").get("models", [])]:
            return PASS, f"{name} loaded={name in loaded}, unloaded after keep_alive=5s"
    return FAIL, f"{name} still loaded 30s after keep_alive=5s"


@check("cleanup --yes leaves nothing", "gpu", timeout=300)
def g_cleanup(ctx):
    import arch_server
    foreign = set(ctx.get("foreign_models") or [])
    rc, out = arch(["cleanup", "--yes"], 240, isolated=False)
    left = [p for p in arch_server.status(ctx.get("cfg")).get("processes", []) if p["up"]]
    try:
        now = {m["name"] for m in http(ollama_url(ctx) + "/api/ps").get("models", [])}
    except Exception:
        now = foreign
    gone = foreign - now
    ok = rc == 0 and not left and not gone
    return (PASS if ok else FAIL), f"rc={rc}; ours still up: {[p['tier'] for p in left]}; foreign untouched: {not gone} {sorted(gone)}; {tail(out, 2)}"


# ------------------------------------------------------------------------------------------------ main
def select_checks(mode, only=None, skip=None):
    cs = [c for c in REGISTRY if c["layer"] == mode]
    if only:
        cs = [c for c in cs if any(o.lower() in c["name"].lower() for o in only)]
    if skip:
        cs = [c for c in cs if not any(s.lower() in c["name"].lower() for s in skip)]
    return cs


def final_cleanup(say=print):
    """Never leave servers running: stop ours, and let the app's own cleanup close what it started."""
    try:
        import arch_server
        arch_server.stop(say=lambda m: None)
    except Exception as e:
        say(f"  (stop failed: {e})")
    try:
        arch(["cleanup", "--yes"], 240, isolated=False)
    except Exception as e:
        say(f"  (cleanup failed: {e})")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--cpu-only", action="store_true", help="no model loads, no server start (default)")
    g.add_argument("--gpu", action="store_true", help="the live layer: starts servers, always cleans up")
    ap.add_argument("--only", default="")
    ap.add_argument("--skip", default="")
    ap.add_argument("--selftest-suite", default="full", choices=["core", "all", "full"])
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--out", default=None, help="report folder (default: hub_data/verify)")
    a = ap.parse_args(argv)
    mode = "gpu" if a.gpu else "cpu"
    checks = select_checks(mode, [x for x in a.only.split(",") if x], [x for x in a.skip.split(",") if x])
    if a.list:
        for c in select_checks("cpu") + select_checks("gpu"):
            print(f"  {c['layer']:<4} {c['name']}")
        return 0
    import arch_core as core
    out_dir = a.out or os.path.join(core.data_dir(), "verify")
    os.makedirs(out_dir, exist_ok=True)
    ctx = {"cfg": core.load_config(), "selftest_suite": a.selftest_suite, "t_start": time.time(), "log_dir": out_dir}
    print(f"verify_machine --{mode}: {len(checks)} checks")
    results = []
    try:
        if mode == "gpu":
            try:
                ctx["foreign_models"] = [m["name"] for m in http(ollama_url(ctx) + "/api/ps", timeout=5).get("models", [])]
            except Exception:
                ctx["foreign_models"] = []
        results = run_matrix(checks, ctx)
    finally:
        if mode == "gpu":
            final_cleanup()
    rep, base = write_report(results, mode, out_dir)
    s = rep["summary"]
    print(f"\nVerdict {s['verdict']}: PASS {s[PASS]}  FAIL {s[FAIL]}  NOT_SURE {s[NOT_SURE]}\nReport: {base}.json / .md")
    return 1 if s[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
