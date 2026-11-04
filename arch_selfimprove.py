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
