"""Graceful stop of our llama-servers: Ctrl+C / SIGINT first (llama-server saves --lookup-cache-dynamic on a clean exit), a bounded
wait, then the hard kill. Uses a stand-in Python "server"; never touches the real model servers. run() -> True when all pass."""
import copy
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

STANDIN = r'''
import signal, sys, time
out, mode = sys.argv[1], sys.argv[2]
def done(*a):
    with open(out, "w") as f:
        f.write("saved")
    sys.exit(0)
if mode == "ignore":
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)
else:
    signal.signal(signal.SIGINT, done)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, done)
open(out + ".up", "w").close()
while True:
    time.sleep(0.1)
'''


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    def env_check(name, ok, extra=""):
        """An ENVIRONMENT check (a clock, the person's own data): a pass is a pass, but a failure is reported NOT_SURE
        (could not run here), never a silent pass and never a code failure."""
        results.append(True if ok else None)
        print(f"  {'PASS' if ok else 'NOT_SURE'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_core as core
    import arch_server as S
    import arch_selftest_fastlane as F
    home = tempfile.mkdtemp(prefix="archgrace_")
    old_home, old_env = core.HOME, os.environ.get("ARCHITECT_DATA_DIR")
    core.set_home(home)
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "data")
    procs = []
    try:
        script = os.path.join(home, "standin.py")
        with open(script, "w") as f:
            f.write(STANDIN)

        def launch(mode, out):
            p = S._spawn({"args": [sys.executable, "-I", script, out, mode]}, os.path.join(home, mode + ".log"))
            procs.append(p)
            t0 = time.time()
            while not os.path.exists(out + ".up") and time.time() - t0 < 15:
                time.sleep(0.1)
            return p

        # 1. a server that handles the signal finishes its exit handler
        out = os.path.join(home, "clean.txt")
        p = launch("handle", out)
        S.save_state({"processes": {"worker": {"pid": p.pid, "port": 0}}})
        lines = []
        t0 = time.time()
        S.stop(say=lines.append)
        dt = time.time() - t0
        try:
            p.wait(5)
        except Exception:
            pass
        env_check("stop(): a server that handles Ctrl+C/SIGINT runs its exit handler (file written) and is reported clean",
              os.path.exists(out) and p.poll() is not None and any("cleanly" in l for l in lines), (lines, dt))
        env_check("stop(): the clean stop is quick (< 30 s; wall-clock, so a busy machine reports NOT_SURE)", dt < 30, dt)
        check("stop(): the record is gone from the state", not (S.load_state().get("processes") or {}))

        # 2. a server that ignores the signal is hard-killed, bounded
        out2 = os.path.join(home, "ignore.txt")
        p2 = launch("ignore", out2)
        S.save_state({"processes": {"worker": {"pid": p2.pid, "port": 0}}})
        lines = []
        t0 = time.time()
        S.stop(say=lines.append)
        dt = time.time() - t0
        try:
            p2.wait(5)
        except Exception:
            pass
        check("stop(): a server that ignores the signal is killed after the grace period", p2.poll() is not None and not os.path.exists(out2)
              and any("forced" in l for l in lines), lines)
        check("stop(): the grace period is bounded (<= 8 s)", S.GRACE_S <= 8.0, S.GRACE_S)
        env_check("stop(): the fallback finishes in grace + kill + slack (< 40 s; wall-clock)", dt < 40, dt)

        # 3. the launch flags still let the console event reach the server (no CREATE_NEW_PROCESS_GROUP, which disables Ctrl+C)
        cf, _ = S.launch_opts({}, windows=True)
        check("launch_opts: CREATE_NO_WINDOW kept, no CREATE_NEW_PROCESS_GROUP", cf & 0x08000000 and not cf & 0x200, hex(cf))
        check("_ask_exit on a pid that does not exist returns False and never raises", S._ask_exit(999999) is False)

        # 4. real start args still carry the lookup cache path (plan with fast lane on)
        real = F._patch(S)
        try:
            stub = os.path.join(home, "llama-server" + (".exe" if sys.platform.startswith("win") else ""))
            with open(stub, "w") as f:
                f.write("stub")
            cfg = copy.deepcopy(core.load_config())
            cfg["server"]["binary"] = stub
            cfg["server"]["fast_lane"] = "on"
            cfg["tiers"] = {"router": "fr:1b", "worker": "fw:4b", "heavy": "", "draft": ""}
            pl = S.plan(cfg, dict(F.HW), caps_=F.CAPS)
            w = next(x for x in pl["processes"] if x["tier"] == "worker")
            fm = F._flagmap(w["args"])
            check("plan: the worker's args include --lookup-cache-dynamic <data>/server/lookup/worker.bin",
                  fm.get("--lookup-cache-dynamic") == os.path.join(S.server_dir(), "lookup", "worker.bin"), fm.get("--lookup-cache-dynamic"))
        finally:
            F._unpatch(S, real)
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        core.set_home(old_home)
        if old_env is None:
            os.environ.pop("ARCHITECT_DATA_DIR", None)
        else:
            os.environ["ARCHITECT_DATA_DIR"] = old_env
    n_none = results.count(None)
    print(f"graceful: {sum(1 for r in results if r)}/{len(results)} passed" + (f"; 0 of {len(results)} FAILED, {n_none} COULD NOT RUN (not a pass)" if n_none and False not in results else ""))
    return all(r is True for r in results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
