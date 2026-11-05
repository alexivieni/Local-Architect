"""Safe self-improvement: the Architect improves ITSELF the way it improves any project, but never edits the copy that is running.

    python architect.py self-improve "make the doctor report disk space per model"      candidate copy -> flow -> gates -> you confirm -> promote
    python architect.py self-verify [--write]                                           is the install still what was last verified?
    python architect.py self-rollback                                                   undo the last promotion (files restored from its backup)
    python architect.py self-history

How a change gets in:
  1. CANDIDATE   a copy of the install (code only: never your settings, results or run history) in <home>/.arch_self/candidates/.
  2. WORK        any flow runs against the candidate, with the same diff review and undo as any project.
  3. GATES       cheap first, all must pass, decided by code and not by the model that wrote the change:
                   protected files untouched (the network guard, this gate, the manifest);
                   every .py compiles;
                   no more fake-code findings than before in the files it changed;
                   the candidate's own self-test passes in a network-restricted subprocess;
                   and it runs AT LEAST as many checks as the current install (deleting or skipping tests cannot pass).
  4. PROMOTE     only after the gates and your confirmation: files that will be overwritten are zipped first, then copied
                 atomically, and the integrity manifest is rewritten. Restart to use the new version.
  5. ROLLBACK    restores the zip, removes files the change added, rewrites the manifest.
The gate and the safety net cannot be edited by this path (--allow-safety-edits exists for a human who has reviewed the diff).

The iterative loop (loop() below; docs/self-improvement.md): `self-improve --goal "..." --rounds N [--fake]` runs
workflows/self_improve.yaml round by round in one throwaway copy (audit -> plan checked in code -> owned builds -> adversarial
verify -> gates), feeding failures into the next round, and promotes through promote() only on all-pass plus your yes.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

import arch_core as core
import arch_fakecode as fc

HERE = os.path.dirname(os.path.abspath(__file__))
SKIP_DIRS = {"__pycache__", "hub_data", ".arch", ".arch_self", ".git", ".pytest_cache", "node_modules",
             "bin", "models", "._guard"}                                                        # downloaded: the server program and the model files are not source (the installer checks them separately)
SKIP_FILES = {"architect_config.json", "portfolio.json", "hub_config.json", "NOTES.md"}
SKIP_SUFFIX = (".pyc", ".bak", ".tmp", ".log")
GENERATED = {"MANIFEST.sha256", "UPSTREAM.sha256"}                                   # rewritten on install: editing them by hand is meaningless
GUARD = {"netguard.py", "._guard/netguard.py", "._guard/sitecustomize.py", "arch_selfimprove.py"}
PROTECTED = GENERATED | GUARD                                                         # kept for callers; what is actually enforced depends on the setting below


def protected_now(cfg=None):
    """Which files the self-improve path may not edit. behavior.self_protect: none (default; only generated files) | guard (also the network guard and this gate)."""
    import arch_core
    cfg = cfg if cfg is not None else arch_core.load_config(HERE)
    mode = (cfg.get("behavior") or {}).get("self_protect", "none")
    return GENERATED | (GUARD if mode == "guard" else set())


def _state_dir():
    return os.path.join(core.HOME, ".arch_self")


def tracked_files(base):
    """Code files of an install (relative, forward slashes): not settings, results, caches or run history."""
    out = []
    for dp, dn, fs in os.walk(base):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for f in fs:
            if f in SKIP_FILES or f.endswith(SKIP_SUFFIX):
                continue
            out.append(os.path.relpath(os.path.join(dp, f), base).replace(os.sep, "/"))
    return sorted(out)


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest_hashes(base):
    return {rel: sha(os.path.join(base, rel)) for rel in tracked_files(base) if rel != "MANIFEST.sha256"}


def write_manifest(base):
    with open(os.path.join(base, "MANIFEST.sha256"), "w", encoding="utf-8", newline="\n") as f:
        for rel, h in sorted(manifest_hashes(base).items()):
            f.write(f"{h}  {rel}\n")


def verify(base=HERE):
    """-> {"has_manifest", "ok", "modified", "missing", "unlisted"} comparing the install with MANIFEST.sha256 (the last verified state)."""
    p = os.path.join(base, "MANIFEST.sha256")
    if not os.path.isfile(p):
        return {"has_manifest": False, "ok": False, "modified": [], "missing": [], "unlisted": []}
    want = {}
    for ln in open(p, encoding="utf-8"):
        if "  " in ln:
            h, rel = ln.rstrip("\n").split("  ", 1)
            want[rel] = h
    have = manifest_hashes(base)
    mod = sorted(r for r in want if r in have and have[r] != want[r])
    return {"has_manifest": True, "ok": not mod and all(r in have for r in want), "modified": mod,
            "missing": sorted(r for r in want if r not in have), "unlisted": sorted(r for r in have if r not in want)}


def make_candidate(base=HERE):
    root = os.path.join(_state_dir(), "candidates", time.strftime("%Y%m%d_%H%M%S"))
    cand = os.path.join(root, "architect")
    for rel in tracked_files(base):
        dst = os.path.join(cand, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(base, rel), dst)
    return cand


def changes(base, cand):
    b, c = set(tracked_files(base)) - {"MANIFEST.sha256"}, set(tracked_files(cand)) - {"MANIFEST.sha256"}
    return {"added": sorted(c - b), "removed": sorted(b - c),
            "modified": sorted(r for r in b & c if sha(os.path.join(base, r)) != sha(os.path.join(cand, r)))}


def _selftest(root, suite, timeout):
    """Run `architect.py selftest` inside `root` in a network-restricted subprocess with throwaway home/data dirs.
    -> (passed_all: bool, checks_run: int, tail: str)"""
    tmp = tempfile.mkdtemp(prefix="arch_gate_")
    try:
        env = core.guard_env(True)
        env.update(ARCHITECT_HOME=tmp, ARCHITECT_DATA_DIR=os.path.join(tmp, "data"), AGENT_NET_MODE="offline", AGENT_NET_TOOLS="allow", PYTHONDONTWRITEBYTECODE="1")
        env.pop("OLLAMA_HOST", None)
        try:
            r = subprocess.run([sys.executable, os.path.join(root, "architect.py"), "selftest", "--suite", suite], cwd=root, capture_output=True,
                               text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return False, 0, f"self-test timed out after {timeout}s"
        out = r.stdout + r.stderr
        passed = sum(int(m) for m in re.findall(r"ALL (\d+) [A-Z/ -]*PASSED", out))
        failed = len(re.findall(r"^\s*FAIL\b", out, re.M))
        return r.returncode == 0 and failed == 0, passed, out[-1500:]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def baseline_checks(base=HERE, suite="all", timeout=2400, say=print):
    """How many checks the CURRENT install runs and passes. Cached per exact file contents, so it is recomputed only after a change."""
    key = hashlib.sha256(json.dumps(manifest_hashes(base), sort_keys=True).encode() + suite.encode()).hexdigest()[:16]
    cache_p = os.path.join(_state_dir(), "baseline.json")
    cache = core.load_json(cache_p)
    if cache.get("key") == key:
        return cache["checks"]
    say("  running the current install's self-test to learn its baseline (cached afterwards)...")
    ok, n, tail = _selftest(base, suite, timeout)
    if not ok:
        raise RuntimeError("the CURRENT install does not pass its own self-test, so there is no baseline to compare against:\n" + tail)
    os.makedirs(os.path.dirname(cache_p), exist_ok=True)
    with open(cache_p, "w", encoding="utf-8") as f:
        json.dump({"key": key, "checks": n, "suite": suite, "ts": time.time()}, f)
    return n


def gate(base, cand, suite="all", baseline=None, allow_safety=False, timeout=2400, say=print):
    """-> {"ok", "gates": [{"name","ok","detail"}], "changed": {...}, "checks": int|None}. Cheap gates first; stops at the first failure."""
    ch = changes(base, cand)
    gates = []

    def add(name, ok, detail=""):
        gates.append({"name": name, "ok": bool(ok), "detail": detail})
        say(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
        return ok

    def done(checks=None):
        return {"ok": all(g["ok"] for g in gates) and bool(gates), "gates": gates, "changed": ch, "checks": checks}
    touched = ch["added"] + ch["modified"] + ch["removed"]
    if not touched:
        add("the candidate differs from the install", False, "no changes were made")
        return done()
    prot = sorted(set(touched) & (GENERATED if allow_safety else protected_now()))
    if prot:
        add("protected files are untouched", False, ", ".join(prot) + (" (generated files: they are rewritten on install)" if set(prot) <= GENERATED else " (behavior.self_protect = guard keeps the network guard and this gate out of reach; set it to none to allow)"))
        return done()
    add("protected files are untouched", True)
    bad = []
    for rel in ch["added"] + ch["modified"]:
        if rel.endswith(".py"):
            try:
                with open(os.path.join(cand, rel), encoding="utf-8") as f:
                    compile(f.read(), rel, "exec")
            except (SyntaxError, ValueError) as e:
                bad.append(f"{rel}: {e}")
    if not add("every changed Python file compiles", not bad, "; ".join(bad[:4])):
        return done()
    worse = []
    for rel in ch["modified"] + ch["added"]:
        if rel.endswith(".py"):
            was = sum(1 for x in fc.scan(base, [rel]) if x["gate"]) if os.path.isfile(os.path.join(base, rel)) else 0
            now = sum(1 for x in fc.scan(cand, [rel]) if x["gate"])
            if now > was:
                worse.append(f"{rel}: {was} -> {now} fake-code finding(s)")
    if not add("no new fake code (stubs, assertion-free tests, always-true assertions) in changed files", not worse, "; ".join(worse[:4])):
        return done()
    say(f"  running the candidate's self-test (suite {suite}) in a network-restricted subprocess...")
    ok, n, tail = _selftest(cand, suite, timeout)
    if not add("the candidate passes its own self-test", ok, tail.strip().splitlines()[-1] if tail.strip() else "no output"):
        return done(n)
    base_n = baseline if baseline is not None else baseline_checks(base, suite, timeout, say)
    if not add(f"it runs at least as many checks as the current install ({n} vs {base_n}): tests cannot be deleted to pass", n >= base_n, f"{n} < {base_n}"):
        return done(n)
    say("  running the quick eval (oracle fake) on the candidate and comparing it with the install...")
    ev = eval_gate(base, cand, timeout, say)
    add("no eval regression (pass rate, tokens, latency per flow against the install's own quick eval)", ev["ok"], "; ".join(ev.get("regressions") or [ev.get("error", "")])[:300])
    return done(n)


def _eval_fake(root, timeout):
    """`architect.py eval --fake --quick --json` inside `root`, network-restricted, throwaway home. -> report dict or {"error"}."""
    tmp = tempfile.mkdtemp(prefix="arch_evalgate_")
    try:
        env = core.guard_env(True)
        env.update(ARCHITECT_HOME=tmp, ARCHITECT_DATA_DIR=os.path.join(tmp, "data"), AGENT_NET_MODE="offline", AGENT_NET_TOOLS="allow", PYTHONDONTWRITEBYTECODE="1")
        env.pop("OLLAMA_HOST", None)
        try:
            r = subprocess.run([sys.executable, os.path.join(root, "architect.py"), "eval", "--fake", "--quick", "--json", "--label", "gate"], cwd=root,
                               capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return {"error": f"eval timed out after {timeout}s"}
        line = next((ln for ln in reversed(r.stdout.splitlines()) if ln.startswith("{")), "")
        try:
            return json.loads(line)
        except ValueError:
            return {"error": "eval produced no JSON report: " + (r.stdout + r.stderr)[-300:]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def eval_gate(base, cand, timeout=900, say=print):
    """The install's quick eval vs the candidate's, both on the oracle fake. -> {"ok", "regressions", "base", "cand"} (or "error")."""
    import arch_eval
    b = _eval_fake(base, timeout)
    if "error" in b:
        return {"ok": False, "error": "baseline eval failed: " + b["error"]}
    c = _eval_fake(cand, timeout)
    if "error" in c:
        return {"ok": False, "error": "candidate eval failed: " + c["error"]}
    cmp_ = arch_eval.compare(b, c, latency=False)          # oracle seconds measure the machine's load, not the candidate
    say(arch_eval.compare_table(cmp_))
    return {"ok": cmp_["ok"], "regressions": cmp_["regressions"], "base": b.get("summary"), "cand": c.get("summary")}


def history():
    return core.load_json(os.path.join(_state_dir(), "history.json")).get("events", [])


def _log(event):
    p = os.path.join(_state_dir(), "history.json")
    ev = history()
    ev.append(event)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"events": ev[-200:]}, f, indent=1)


def _atomic_copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".arch_new"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def promote(base, cand, gate_result, goal="", confirm=False, say=print):
    """Copy a gated candidate over the install. Refuses without a passing gate AND an explicit confirm. -> {"ok","backup","files"}"""
    if not gate_result.get("ok"):
        raise PermissionError("the candidate did not pass the gates")
    if not confirm:
        raise PermissionError("promotion needs an explicit confirmation")
    ch = gate_result["changed"]
    files = ch["modified"] + ch["added"]
    stamp = time.strftime("%Y%m%d_%H%M%S")
    bdir = os.path.join(_state_dir(), "backups")
    os.makedirs(bdir, exist_ok=True)
    backup = os.path.join(bdir, stamp + ".zip")
    with zipfile.ZipFile(backup, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in ch["modified"] + ["MANIFEST.sha256"]:
            if os.path.isfile(os.path.join(base, rel)):
                z.write(os.path.join(base, rel), "files/" + rel)
        z.writestr("meta.json", json.dumps({"added": ch["added"], "goal": goal, "ts": time.time()}))
    for rel in files:
        _atomic_copy(os.path.join(cand, rel), os.path.join(base, rel))
    write_manifest(base)
    _log({"event": "promote", "ts": time.time(), "goal": goal, "files": files, "backup": backup, "checks": gate_result.get("checks")})
    say(f"  promoted {len(files)} file(s); backup: {backup}")
    if ch["removed"]:
        say(f"  note: the candidate also removed {len(ch['removed'])} file(s); those removals were NOT applied: {', '.join(ch['removed'][:5])}")
    say("  restart the Architect (and the hub) to use the new version.")
    return {"ok": True, "backup": backup, "files": files}


def rollback(base=HERE, say=print):
    """Undo the most recent promotion that has not been rolled back. -> {"ok","files"} or {"ok": False, "error"}"""
    ev = history()
    live = []
    for e in ev:
        if e["event"] == "promote":
            live.append(e)
        elif e["event"] == "rollback" and live:
            live.pop()
    if not live:
        return {"ok": False, "error": "nothing to roll back"}
    last = live[-1]
    if not os.path.isfile(last["backup"]):
        return {"ok": False, "error": f"backup missing: {last['backup']}"}
    restored = []
    with zipfile.ZipFile(last["backup"]) as z:
        meta = json.loads(z.read("meta.json"))
        for name in z.namelist():
            if name.startswith("files/"):
                rel = name[len("files/"):]
                dst = os.path.join(base, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                tmp = dst + ".arch_new"
                with open(tmp, "wb") as f:
                    f.write(z.read(name))
                os.replace(tmp, dst)
                restored.append(rel)
    for rel in meta.get("added", []):
        p = os.path.join(base, rel)
        if os.path.isfile(p):
            os.remove(p)
            restored.append(rel + " (removed)")
    if "MANIFEST.sha256" not in [r for r in restored]:
        write_manifest(base)
    _log({"event": "rollback", "ts": time.time(), "of": last["backup"], "files": restored})
    say(f"  rolled back {len(restored)} file(s) from {os.path.basename(last['backup'])}. Restart to use the restored version.")
    return {"ok": True, "files": restored}


RUN_EXTRA = []        # options from the self-improve command line (--model, --role-model, --set, ...) handed to the run


def _run_flow(cand, goal, flow, review):
    argv = [sys.executable, os.path.join(cand, "architect.py"), "run", goal, "--flow", flow, "--root", cand, "--review", review] + list(RUN_EXTRA)
    env = dict(os.environ, ARCHITECT_HOME=core.HOME, ARCHITECT_SELFDEV="1")   # your models and settings; every call as the Architect developer
    return subprocess.call(argv, cwd=cand, env=env)


def improve(goal, flow="auto", review="ask", yes=False, base=HERE, suite="all", allow_safety=False, keep=False, say=print, runner=None, baseline=None):
    """The whole path. `runner(cand, goal, flow, review)` performs the work (default: the real flow in a subprocess; tests inject a scripted one)."""
    say("[1/4] making a candidate copy of the code (your settings and results are not copied)")
    cand = make_candidate(base)
    say(f"      {cand}")
    say("[2/4] working on the candidate")
    rc = (runner or _run_flow)(cand, goal, flow, review)
    if rc not in (0, None):
        say("      the flow did not complete; the gates will still check whatever it changed")
    say("[3/4] gates")
    g = gate(base, cand, suite=suite, baseline=baseline, allow_safety=allow_safety, say=say)
    ch = g["changed"]
    say(f"      changed: {len(ch['modified'])} modified, {len(ch['added'])} added, {len(ch['removed'])} removed")
    if not g["ok"]:
        say("[4/4] NOT promoted: a gate failed. The running install is unchanged." + (f" The candidate is kept at {cand} for inspection." if keep else ""))
        _log({"event": "rejected", "ts": time.time(), "goal": goal, "gates": [x for x in g["gates"] if not x["ok"]]})
        if not keep:
            shutil.rmtree(os.path.dirname(cand), ignore_errors=True)
        return {"ok": False, "gate": g, "candidate": cand if keep else None}
    go = yes
    if not yes:
        try:
            go = input("      All gates passed. Promote this change into the running install? [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            go = False
    if not go:
        say("[4/4] not promoted (you declined). Candidate kept at " + cand)
        return {"ok": False, "declined": True, "gate": g, "candidate": cand}
    say("[4/4] promoting")
    out = promote(base, cand, g, goal, confirm=True, say=say)
    if not keep:
        shutil.rmtree(os.path.dirname(cand), ignore_errors=True)
    return {"ok": True, "gate": g, **out}


# ================================================================ the iterative loop: a squad works on a throwaway copy, round by round
#
#     python architect.py self-improve --goal "..." [--rounds N] [--fake] [--yes] [--allow-gate-edits] [--full-gates]
#     python architect.py self-improve                 (no goal: picks the top item from failing checks / telemetry errors / the advisor)
#
# One round = workflows/self_improve.yaml in the copy: Audit (3 read-only auditors, parallel) -> Plan (si-planner; the plan is CHECKED
# IN CODE: real paths, one owner per file, never the gates) -> Build (one builder per plan entry, owned files only) -> Verify (an
# adversarial verifier) -> Gates (code). A failing round feeds its failures into the next one; the loop stops when every gate passes,
# when a round makes no progress, or after N rounds. Promotion still needs your yes (or --yes) and goes through promote() above:
# zip backup, atomic copy, manifest, and `self-rollback` undoes it. Every round is a crew run (Orchestration page) + telemetry events.

WORKFLOW = os.path.join(HERE, "workflows", "self_improve.yaml")
# What the loop may not change without --allow-gate-edits: the gates, the guard, the eval it compares with, and this loop's own test.
GATE_FILES = GUARD | GENERATED | {"arch_eval.py", "arch_fakecode.py", "arch_selftest_selfimprove.py", "arch_selftest_self.py",
                                  "workflows/self_improve.yaml", "agents/si-planner.yaml"}
NEW_FILE_EXT = (".py", ".md", ".yaml", ".yml", ".html", ".json", ".txt")
NET_RE = re.compile(r"\burllib\.request\b|\burlopen\s*\(|\bhttp\.client\b|\bsocket\.(?:socket|create_connection)\s*\(|\brequests\.(?:get|post|put|"
                    r"delete|head|request|Session)\b|\bhttpx\.|\bftplib\b|\bsmtplib\b|\bwebsocket\b")
PASS, FAIL, CNR = "pass", "fail", "could-not-run"


def _loops_dir():
    return os.path.join(_state_dir(), "loops")


def _tele(kind, **f):
    try:
        import arch_telemetry
        arch_telemetry.emit(kind, **f)
        arch_telemetry.flush()
    except Exception:
        pass


def save_session(s):
    s["updated"] = time.time()
    os.makedirs(_loops_dir(), exist_ok=True)
    p = os.path.join(_loops_dir(), s["id"] + ".json")
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1, default=str)
    os.replace(p + ".tmp", p)


def load_session(sid):
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,80}", str(sid or "")):
        raise ValueError("bad session id")
    s = core.load_json(os.path.join(_loops_dir(), sid + ".json"))
    if not s:
        raise ValueError("no such self-improve session: " + str(sid))
    return s


def sessions(limit=30):
    """Newest first, without the diffs (the hub's history list)."""
    d = _loops_dir()
    out = []
    for fn in sorted(os.listdir(d), reverse=True)[:limit] if os.path.isdir(d) else []:
        if fn.endswith(".json"):
            s = core.load_json(os.path.join(d, fn))
            if s:
                out.append({k: s.get(k) for k in ("id", "goal", "source", "status", "created", "updated", "fake", "promoted", "backup")}
                           | {"rounds": len(s.get("rounds") or []), "last_ok": bool((s.get("rounds") or [{}])[-1].get("ok"))})
    return out


# ---------------------------------------------------------------- what to work on when no goal is given

def suggest(base=HERE):
    """-> {"goal", "source", "why"}: failing checks first (fake-code gate findings in the install), then the most frequent telemetry
    error, then the advisor's top proposal. Every suggestion names where it came from."""
    try:
        files = [r for r in tracked_files(base) if r.endswith(".py") and not r.startswith(("tools/", "tests/"))]
        bad = [x for x in fc.scan(base, files) if x["gate"]]
        if bad:
            x = bad[0]
            return {"goal": f"Fix the fake-code finding in {x['file']}:{x['line']}: {x['msg']}", "source": "failing check",
                    "why": f"{len(bad)} gate-level fake-code finding(s) in the install", "files": sorted({b['file'] for b in bad})[:6]}
    except Exception as ex:
        _tele("error", where="selfimprove.suggest.fakecode", exc=type(ex).__name__, msg=str(ex)[:200])
    try:
        import arch_telemetry
        errs = arch_telemetry.read(since=time.time() - 7 * 86400, kinds=["error"], limit=500)
        counts = {}
        for e in errs:
            k = (str(e.get("where") or "?"), str(e.get("exc") or ""))
            counts[k] = counts.get(k, 0) + 1
        if counts:
            (where, exc), n = max(counts.items(), key=lambda kv: kv[1])
            return {"goal": f"Stop the {exc or 'error'} that keeps happening in {where} ({n} time(s) this week)", "source": "telemetry errors",
                    "why": f"most frequent error event in the last 7 days: {where} {exc}"}
    except Exception as ex:
        _tele("error", where="selfimprove.suggest.telemetry", exc=type(ex).__name__, msg=str(ex)[:200])
    try:
        import arch_advisor
        props = arch_advisor.advise(base).get("proposals") or []
        if props:
            p = props[0]
            return {"goal": f"{p['title']}: {p['action']}"[:300], "source": "advisor", "why": p.get("why", "")}
    except Exception as ex:
        _tele("error", where="selfimprove.suggest.advisor", exc=type(ex).__name__, msg=str(ex)[:200])
    return {"goal": "", "source": "none", "why": "no failing checks, no recent errors and no advisor proposals"}


# ---------------------------------------------------------------- the plan, checked in code

def check_plan(plan, cand, allow_gate_edits=False):
    """-> (ok, problems, builders). Every owned path must be a relative path inside the copy that exists (or a new file of a plain type
    in a folder that exists); no file belongs to two builders; generated files and (without the flag) the gates are refused."""
    probs, out, seen = [], [], {}
    bl = (plan or {}).get("builders") if isinstance(plan, dict) else None
    if not isinstance(bl, list) or not bl:
        return False, ["the plan names no builders"], []
    if len(bl) > 4:
        probs.append(f"{len(bl)} builders: at most 4 per round")
    tracked = set(tracked_files(cand))
    for i, b in enumerate(bl[:4]):
        owns = [str(x).strip().replace("\\", "/") for x in (b or {}).get("owns") or [] if str(x).strip()]
        if not owns:
            probs.append(f"builder {i + 1} owns no files")
        if len(owns) > 6:
            probs.append(f"builder {i + 1} owns {len(owns)} files: at most 6")
        for rel in owns:
            where = f"builder {i + 1}: {rel}"
            if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel) or ".." in rel.split("/"):
                probs.append(where + " is not a path inside the app")
                continue
            if rel.split("/")[0] in SKIP_DIRS or os.path.basename(rel) in SKIP_FILES:
                probs.append(where + " is not code (settings, results or caches)")
                continue
            if rel in GENERATED:
                probs.append(where + " is generated on install")
                continue
            if rel in GATE_FILES and not allow_gate_edits:
                probs.append(where + " is one of the gates (needs --allow-gate-edits and a person who read the diff)")
                continue
            if rel not in tracked and not (rel.endswith(NEW_FILE_EXT) and os.path.isdir(os.path.join(cand, os.path.dirname(rel) or "."))):
                probs.append(where + " does not exist in the app")
                continue
            if rel in seen:
                probs.append(f"{rel} is owned by builders {seen[rel] + 1} and {i + 1}")
                continue
            seen[rel] = i
        out.append({"owns": owns, "task": str((b or {}).get("task") or "")[:2000]})
    return not probs, probs, out


# ---------------------------------------------------------------- gates (code decides; status pass | fail | could-not-run)

def unified_diff(base, cand, ch, limit=200_000):
    import difflib
    parts = []
    for rel in ch["modified"] + ch["added"] + ch["removed"]:
        def lines(root):
            p = os.path.join(root, rel)
            if not os.path.isfile(p):
                return []
            try:
                with open(p, encoding="utf-8") as f:
                    return f.read().splitlines(keepends=True)
            except (UnicodeDecodeError, OSError):
                return ["(binary file)\n"]
        parts.append("".join(difflib.unified_diff(lines(base), lines(cand), "a/" + rel, "b/" + rel)))
    d = "".join(parts)
    return d if len(d) <= limit else d[:limit] + "\n... (diff cut)\n"


def affected_suites(cand, changed):
    """The arch_selftest_*.py suites that cover the changed modules: the module's own suite, the changed suite itself, and suites that
    import it. At most 8."""
    suites = sorted(f[:-3] for f in os.listdir(cand) if f.startswith("arch_selftest_") and f.endswith(".py"))
    texts = {}
    out = []
    for rel in changed:
        if "/" in rel or not rel.endswith(".py"):
            continue
        mod = rel[:-3]
        if mod.startswith("arch_selftest_"):
            out.append(mod)
            continue
        own = "arch_selftest_" + (mod[5:] if mod.startswith("arch_") else mod)
        if own in suites:
            out.append(own)
        pat = re.compile(r"(?:^|\n)\s*(?:import|from)\s+" + re.escape(mod) + r"\b|__import__\(\"" + re.escape(mod) + r"\"\)")
        for su in suites:
            if su not in texts:
                try:
                    with open(os.path.join(cand, su + ".py"), encoding="utf-8") as f:
                        texts[su] = f.read()
                except OSError:
                    texts[su] = ""
            if pat.search(texts[su]):
                out.append(su)
    seen = []
    for s in out:
        if s not in seen and os.path.isfile(os.path.join(cand, s + ".py")):
            seen.append(s)
    return seen[:8]


def _restricted_env(tmp):
    env = core.guard_env(True)
    env.update(ARCHITECT_HOME=tmp, ARCHITECT_DATA_DIR=os.path.join(tmp, "data"), AGENT_NET_MODE="offline", AGENT_NET_TOOLS="allow",
               PYTHONDONTWRITEBYTECODE="1", ARCHITECT_NO_CACHE="1")
    env.pop("OLLAMA_HOST", None)
    return env


def run_suite(root, name, timeout=900):
    """One selftest module inside `root`, network-restricted, throwaway home. -> (status, tail)."""
    tmp = tempfile.mkdtemp(prefix="arch_sigate_")
    code = ("import sys; sys.path.insert(0, '.'); import importlib; m = importlib.import_module(%r)\n"
            "f = getattr(m, 'run', None) or getattr(m, 'main')\nr = f()\n"
            "sys.exit(0 if (r is True or r is None or (type(r) is int and r == 0)) else 1)\n") % name
    try:
        r = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=timeout, env=_restricted_env(tmp))
        tail = (r.stdout + r.stderr)[-1200:]
        return (PASS if r.returncode == 0 else FAIL), tail
    except subprocess.TimeoutExpired:
        return CNR, f"{name} timed out after {timeout}s"
    except OSError as ex:
        return CNR, f"{name} could not start: {ex}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _net_count(root, rel):
    p = os.path.join(root, rel)
    if not os.path.isfile(p):
        return 0
    try:
        with open(p, encoding="utf-8") as f:
            return len(NET_RE.findall(f.read()))
    except (OSError, UnicodeDecodeError):
        return 0


def round_gates(base, cand, owned, suite="core", allow_gate_edits=False, runners=None, full=True, say=print):
    """Every gate of one round. -> {"ok", "gates": [{name, status, detail}], "changed", "checks"}. Cheap gates run first and all of
    them run (so the timeline shows every problem at once); the expensive ones are not run after a cheap one failed.
    runners (tests inject scripted ones): suite(root, name) -> (status, tail); selftest(root, suite) -> (ok, n, tail);
    baseline() -> n; eval(base, cand) -> {"ok", "regressions"|"error"}. full=False (the --fake default) leaves the full self-test,
    the check-count floor and the eval as could-not-run, which blocks promotion."""
    rn = dict(runners or {})
    ch = changes(base, cand)
    gates = []

    def add(name, status, detail=""):
        gates.append({"name": name, "status": status, "detail": str(detail)[:600]})
        say(f"      {status.upper():13} {name}" + (f"  ({str(detail)[:200]})" if detail and status != PASS else ""))
        return status == PASS
    touched = ch["added"] + ch["modified"] + ch["removed"]
    add("the copy differs from the install", PASS if touched else FAIL, "" if touched else "no change was made")
    prot = sorted(set(touched) & (GENERATED if allow_gate_edits else GATE_FILES))
    add("the gates themselves are untouched" + (" (--allow-gate-edits: only generated files are refused)" if allow_gate_edits else ""),
        FAIL if prot else PASS, ", ".join(prot))
    stray = sorted(set(touched) - set(owned or []))
    add("only files the plan gave a builder were changed", FAIL if stray else PASS, ", ".join(stray[:8]))
    bad = []
    for rel in ch["added"] + ch["modified"]:
        if rel.endswith(".py"):
            try:
                with open(os.path.join(cand, rel), encoding="utf-8") as f:
                    compile(f.read(), rel, "exec")
            except (SyntaxError, ValueError, UnicodeDecodeError) as e:
                bad.append(f"{rel}: {e}")
    add("every changed Python file compiles", FAIL if bad else PASS, "; ".join(bad[:4]))
    worse = []
    for rel in ch["modified"] + ch["added"]:
        if rel.endswith(".py") and not bad:
            was = sum(1 for x in fc.scan(base, [rel]) if x["gate"]) if os.path.isfile(os.path.join(base, rel)) else 0
            now = sum(1 for x in fc.scan(cand, [rel]) if x["gate"])
            if now > was:
                worse.append(f"{rel}: {was} -> {now} fake-code finding(s)")
    add("no new fake code (stubs, assertion-free tests, always-true assertions)", FAIL if worse else PASS, "; ".join(worse[:4]))
    net = [f"{rel}: {_net_count(base, rel)} -> {_net_count(cand, rel)}" for rel in ch["modified"] + ch["added"]
           if rel.endswith(".py") and _net_count(cand, rel) > _net_count(base, rel)]
    add("no new network calls on the default path", FAIL if net else PASS, "; ".join(net[:4]))
    cheap_ok = all(g["status"] == PASS for g in gates)
    skip = "not run: an earlier gate failed"
    # -- the suites that cover what changed
    aff = affected_suites(cand, ch["modified"] + ch["added"])
    if not cheap_ok:
        add("the selftest suites that cover the changed modules pass", CNR, skip)
    elif not aff:
        add("the selftest suites that cover the changed modules pass", PASS, "no suite covers the changed files")
    else:
        res = [(s,) + tuple((rn.get("suite") or run_suite)(cand, s)) for s in aff]
        st = FAIL if any(r[1] == FAIL for r in res) else (CNR if any(r[1] == CNR for r in res) else PASS)
        add(f"the selftest suites that cover the changed modules pass ({', '.join(aff)})", st,
            "; ".join(f"{s}: {r}" + (f" -- {t.strip().splitlines()[-1] if t.strip() else ''}" if r != PASS else "") for s, r, t in res))
    # -- the full self-test, the check-count floor and the eval (kept from gate() above)
    n = None
    if not cheap_ok or gates[-1]["status"] != PASS:
        for name in ("the copy passes its own self-test (network-restricted)", "it runs at least as many checks as the install", "no eval regression"):
            add(name, CNR, skip)
    elif not full and "selftest" not in rn:
        for name in ("the copy passes its own self-test (network-restricted)", "it runs at least as many checks as the install", "no eval regression"):
            add(name, CNR, "skipped in --fake mode (run with --full-gates, or without --fake, to promote)")
    else:
        ok, n, tail = (rn.get("selftest") or (lambda r, s: _selftest(r, s, 2400)))(cand, suite)
        add(f"the copy passes its own self-test (suite {suite}, network-restricted)", PASS if ok else FAIL,
            tail.strip().splitlines()[-1] if tail and tail.strip() else "no output")
        try:
            base_n = (rn.get("baseline") or (lambda: baseline_checks(base, suite, 2400, say)))()
            add(f"it runs at least as many checks as the install ({n} vs {base_n})", PASS if ok and n >= base_n else FAIL,
                f"{n} < {base_n}" if n < base_n else ("" if ok else "the self-test failed"))
        except RuntimeError as ex:
            add("it runs at least as many checks as the install", CNR, str(ex)[:300])
        if gates[-1]["status"] == PASS and gates[-2]["status"] == PASS:
            ev = (rn.get("eval") or (lambda b, c: eval_gate(b, c, 900, say)))(base, cand)
            if ev.get("error"):
                add("no eval regression (arch_eval.compare against the install)", CNR, ev["error"])
            else:
                add("no eval regression (arch_eval.compare against the install)", PASS if ev.get("ok") else FAIL, "; ".join(ev.get("regressions") or []))
        else:
            add("no eval regression", CNR, skip)
    return {"ok": bool(gates) and all(g["status"] == PASS for g in gates), "gates": gates, "changed": ch, "checks": n}


# ---------------------------------------------------------------- the scripted fake (tests and --fake): no model writes code

def demo_script(n, feedback, goal):
    """The --fake demo: round 1 adds a helper with a stub body (the fake-code gate must stop it), round 2 implements it."""
    body = "    pass\n" if n == 1 else "    words = [w for w in str(text).split() if w]\n    return \" \".join(words[:limit])\n"
    return {"builders": [{"owns": ["si_notes.py"], "task": "add a helper that shortens a goal for the timeline"}],
            "summary": "fake: one helper, one builder",
            "edits": {"si_notes.py": f'"""Shortens a self-improve goal for the timeline (written by the --fake demo for: {goal[:60]!r})."""\n\n\n'
                                     f"def short_goal(text, limit=8):\n{body}"}}


def _fake_cfg(cand):
    import arch_squad
    srv, over = arch_squad.start_fake()
    return srv, core.deep_merge(core.load_config(cand), over)


# ---------------------------------------------------------------- one round, then the loop

def run_round(s, n, cand, base, crew_kw, script=None, runners=None, full=True, allow_gate_edits=False, suite="core", say=print):
    import arch_squad as sq
    w = sq.load_workflow(WORKFLOW)
    ph = {p.get("id"): p for p in w["phases"]}
    fb = (s["rounds"][-1].get("feedback") if s["rounds"] else "") or "(first round)"
    vars_ = {"goal": s["goal"], "owns": "", "feedback": fb}
    crew = sq.Crew(cand, s["goal"], workflow="self-improve", description=f"self-improve round {n}: {s['goal'][:120]}", vars=vars_,
                   run_id=f"{s['id']}-r{n}", use_cache=False, **crew_kw)
    rd = {"n": n, "run_id": crew.run_id, "started": time.time(), "phases": [], "plan": None, "plan_check": None, "gates": [],
          "ok": False, "feedback": "", "diff": ""}
    s["rounds"].append(rd)
    save_session(s)
    _tele("selfimprove.round", sid=s["id"], round=n, state="started", run=crew.run_id, goal=s["goal"][:200])

    def phase(title):
        crew.phase(title)
        rd["phases"].append({"title": title, "t": time.time(), "agents": []})
        save_session(s)

    def note_agents():
        rd["phases"][-1]["agents"] = [{"label": r["label"], "agent": r["agent"], "verdict": r["verdict"], "why": r.get("why"),
                                       "secs": r.get("secs")} for r in crew.records if r.get("phase") == rd["phases"][-1]["title"]]
        save_session(s)
    ctx = {"goal": s["goal"], "vars": vars_, "prev": None, "phases": {}}
    say(f"  [round {n}] Audit: 3 auditors, read-only, side by side")
    phase("Audit")
    audit = sq._run_phase(crew, ph["audit"], ctx, "Audit")
    note_agents()
    say(f"  [round {n}] Plan")
    phase("Plan")
    ctx["prev"] = audit
    plan = sq._run_phase(crew, ph["plan"], ctx, "Plan")
    ok, probs, builders = check_plan(plan, cand, allow_gate_edits)
    scripted = script(n, fb, s["goal"]) if script else None
    if not ok and scripted:
        crew.log("the model's plan was refused (" + "; ".join(probs[:3]) + "); fake mode: using the scripted plan")
        plan = {k: scripted[k] for k in ("builders", "summary")}
        ok, probs, builders = check_plan(plan, cand, allow_gate_edits)
    rd["plan"], rd["plan_check"] = plan, {"ok": ok, "problems": probs}
    crew._event({"type": "plan_check", "ok": ok, "problems": probs, "builders": builders})
    crew.log(("plan accepted: " if ok else "plan refused: ") + ("; ".join(probs) if probs else ", ".join(",".join(b["owns"]) for b in builders)))
    note_agents()
    say(f"      plan {'accepted' if ok else 'refused'}: " + ("; ".join(probs) if probs else " | ".join(", ".join(b["owns"]) for b in builders)))
    owned = sorted({f for b in builders for f in b["owns"]})
    if ok:
        say(f"  [round {n}] Build: {len(builders)} builder(s), owned files only")
        phase("Build")
        specs = [{"agent": ph["build"]["agent"], "task": b["task"], "label": f"build:{i + 1}", "owns": b["owns"], "context": plan}
                 for i, b in enumerate(builders)]
        crew.parallel(specs)
        if scripted:                                   # fake mode: the scripted edits stand in for the model's, owned files only
            for rel, text in (scripted.get("edits") or {}).items():
                if rel not in owned:
                    crew.log(f"scripted edit to {rel} refused: no builder owns it")
                    continue
                p = os.path.join(cand, rel)
                os.makedirs(os.path.dirname(p) or cand, exist_ok=True)
                with open(p, "w", encoding="utf-8", newline="\n") as f:
                    f.write(text)
        note_agents()
    ch = changes(base, cand)
    rd["changed"] = ch
    rd["diff"] = unified_diff(base, cand, ch)
    say(f"  [round {n}] Verify: adversarial review, then the gates")
    phase("Verify")
    review = crew.agent(ph["verify"]["agent"], ph["verify"].get("task", ""), label=ph["verify"].get("label"),
                        context={"plan": plan, "diff": rd["diff"][:8000]})
    rd["review"] = review
    note_agents()
    if ok:
        g = round_gates(base, cand, owned, suite=suite, allow_gate_edits=allow_gate_edits, runners=runners, full=full, say=say)
    else:
        g = {"ok": False, "changed": ch, "checks": None,
             "gates": [{"name": "the plan passes the code check", "status": FAIL, "detail": "; ".join(probs)}]}
    rd["gates"], rd["checks"] = g["gates"], g.get("checks")
    for x in g["gates"]:
        crew._event({"type": "gate", "name": x["name"], "status": x["status"], "detail": x["detail"]})
        _tele("selfimprove.gate", sid=s["id"], round=n, run=crew.run_id, gate=x["name"][:120], status=x["status"])
    rv_fail = isinstance(review, dict) and review.get("verdict") == "fail"
    rd["ok"] = g["ok"]                                  # the gates are binding; the verifier's verdict is advice fed to the next round
    fails = [x for x in g["gates"] if x["status"] != PASS]
    rd["feedback"] = "\n".join([f"- gate {x['status']}: {x['name']}: {x['detail']}" for x in fails] +
                               ([f"- the verifier said fail: {str(review.get('evidence'))[:400]}"] if rv_fail else []))
    crew.log(f"round {n}: " + ("every gate passed" if g["ok"] else f"{len(fails)} gate(s) not passed"))
    rd["verdict"] = crew.close("pass" if g["ok"] else "fail")
    rd["ended"] = time.time()
    save_session(s)
    _tele("selfimprove.round", sid=s["id"], round=n, state="done", ok=g["ok"], run=crew.run_id,
          failed=[x["name"][:80] for x in fails][:6])
    return rd


def loop(goal=None, rounds=3, fake=False, yes=False, base=HERE, allow_gate_edits=False, suite="core", full=None, script=None,
         runners=None, confirm=None, resume=None, say=print, data_dir=None):
    """The whole iterative path. -> the session dict (also saved under <home>/.arch_self/loops/<id>.json for the hub).
    full: run the full self-test + check floor + eval (default: on, off with fake unless runners are given).
    confirm(session) -> bool asks the person (default: input()); yes=True skips the question."""
    base = os.path.abspath(base)
    if resume:
        s = load_session(resume)
        if s.get("status") in ("promoted", "discarded"):
            raise ValueError(f"session {resume} is {s['status']}: start a new one")
        cand = s.get("candidate")
        if not cand or not os.path.isdir(cand):
            raise ValueError("the copy of that session is gone; start a new one")
    else:
        source = "you"
        if not (goal or "").strip():
            sg = suggest(base)
            if not sg["goal"]:
                say("Nothing to improve was found: " + sg["why"])
                return {"status": "nothing", "suggestion": sg}
            goal, source = sg["goal"], sg["source"]
            say(f"No goal given; picked from {sg['source']}: {goal}")
        sid = time.strftime("si%Y%m%d_%H%M%S") + f"_{os.getpid() % 10000:04d}"
        while os.path.exists(os.path.join(_loops_dir(), sid + ".json")):
            sid += "x"
        say(f"[copy] making a throwaway copy of the app (code only; your settings and results are not copied)")
        cand = make_candidate(base)
        s = {"id": sid, "goal": goal.strip(), "source": source, "status": "running", "created": time.time(), "fake": bool(fake),
             "base": base, "candidate": cand, "rounds": [], "allow_gate_edits": bool(allow_gate_edits), "suite": suite}
    s["status"] = "running"
    save_session(s)
    _tele("selfimprove.start", sid=s["id"], goal=s["goal"][:200], fake=bool(fake), rounds=rounds)
    srv = None
    full = (not fake) if full is None else full
    crew_kw = {"say": (lambda m: None), "data_dir": data_dir}
    if fake:
        srv, crew_kw["cfg"] = _fake_cfg(cand)
        script = script or demo_script
    try:
        last_sig = None
        for _ in range(int(rounds)):
            n = len(s["rounds"]) + 1
            say(f"\n== round {n}: {s['goal'][:100]}")
            rd = run_round(s, n, cand, base, crew_kw, script=script, runners=runners, full=full, allow_gate_edits=allow_gate_edits,
                           suite=s.get("suite", suite), say=say)
            if rd["ok"]:
                s["status"] = "passed"
                break
            sig = (tuple(sorted(x["name"] for x in rd["gates"] if x["status"] != PASS)), hashlib.sha256(rd["diff"].encode()).hexdigest())
            if sig == last_sig:
                s["status"] = "no_progress"
                say("  the round changed nothing and failed the same gates: stopping (no progress)")
                break
            last_sig = sig
            s["status"] = "failed"
    finally:
        if srv is not None:
            srv.shutdown()
        save_session(s)
    _tele("selfimprove.end", sid=s["id"], status=s["status"], rounds=len(s["rounds"]))
    if s["status"] != "passed":
        say(f"\n[end] not promotable ({s['status']}). The install is unchanged. Copy kept for another round: {cand}")
        _log({"event": "rejected", "ts": time.time(), "goal": s["goal"], "session": s["id"],
              "gates": [x for x in s["rounds"][-1]["gates"] if x["status"] != PASS] if s["rounds"] else []})
        return s
    say(f"\n[end] every gate passed in round {len(s['rounds'])}.")
    go = yes
    if not yes:
        try:
            go = confirm(s) if confirm else input("      Promote this change into the running install? [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            go = False
    if go:
        return promote_session(s["id"], confirm=True, say=say)
    say("      not promoted yet: promote it from the hub (Machine > Improve itself) or run again with --yes.")
    return s


def promote_session(sid, confirm=False, say=print):
    """Promote the session's copy: only when its LAST round passed every gate, and only with confirm=True."""
    s = load_session(sid)
    if s.get("status") != "passed" or not s.get("rounds") or not s["rounds"][-1].get("ok"):
        raise PermissionError("only a session whose last round passed every gate can be promoted")
    if not confirm:
        raise PermissionError("promotion needs an explicit confirmation")
    base, cand = s["base"], s["candidate"]
    now = changes(base, cand)
    if now != s["rounds"][-1]["changed"]:
        raise PermissionError("the install or the copy changed since the gates ran: run another round")
    out = promote(base, cand, {"ok": True, "changed": now, "checks": s["rounds"][-1].get("checks")}, s["goal"], confirm=True, say=say)
    s.update(status="promoted", promoted=time.time(), backup=out["backup"], files=out["files"])
    save_session(s)
    _tele("selfimprove.promote", sid=sid, files=len(out["files"]))
    shutil.rmtree(os.path.dirname(cand), ignore_errors=True)
    return s


def discard_session(sid):
    s = load_session(sid)
    if s.get("status") == "promoted":
        raise ValueError("already promoted: use Roll back instead")
    if s.get("candidate"):
        shutil.rmtree(os.path.dirname(s["candidate"]), ignore_errors=True)
    s["status"] = "discarded"
    save_session(s)
    _tele("selfimprove.discard", sid=sid)
    return s


def rollback_session(base=HERE, say=print):
    out = rollback(base, say=say)
    _tele("selfimprove.rollback", ok=out.get("ok"), files=len(out.get("files") or []))
    if out.get("ok"):
        for row in sessions(200):
            if row.get("status") == "promoted":
                s = load_session(row["id"])
                s["status"] = "rolled_back"
                save_session(s)
                break
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["verify", "rollback", "history"])
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    if a.what == "verify":
        if a.write:
            write_manifest(HERE)
            print("manifest written")
            return 0
        v = verify(HERE)
        print(json.dumps(v, indent=1))
        return 0 if v["ok"] else 1
    if a.what == "rollback":
        return 0 if rollback(HERE)["ok"] else 1
    for e in history():
        print(time.strftime("%Y-%m-%d %H:%M", time.localtime(e["ts"])), e["event"], e.get("goal", ""), len(e.get("files", [])) or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
