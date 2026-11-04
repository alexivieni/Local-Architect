"""One heavy job at a time: test suites, benchmarks, tuning and self-improvement, run below normal priority.

Seen live (2026-11-03): the full test suite, a second suite, the scripted benchmark and a live GPU benchmark ran at once on a 16 GB laptop
and the machine crashed. Now a heavy command takes a lock in the data folder before it starts. A second one waits, saying which job it is
waiting for, and starts when that one ends. The lock is the operating system's own (msvcrt / fcntl), so a crashed or killed job never
leaves a stale lock behind. Child processes inherit the hold (ARCHITECT_HEAVY_HELD), so a suite that runs the benchmark inside itself
never waits on its own lock. Settings > Behaviour > Heavy jobs = any turns all of this off.
"""
import contextlib
import json
import os
import sys
import time

import arch_core as core

HEAVY = ("selftest", "eval", "livetest", "tune", "self-improve")
HELD = "ARCHITECT_HEAVY_HELD"


def _paths():
    d = core.data_dir()
    return os.path.join(d, "heavy.lock"), os.path.join(d, "heavy.json")


def _try_lock(f):
    try:
        f.seek(0)
        if sys.platform.startswith("win"):
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(f):
    try:
        f.seek(0)
        if sys.platform.startswith("win"):
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def lower_priority():
    """Below normal priority for this process and everything it starts (children inherit it), so the desktop stays usable."""
    try:
        if sys.platform.startswith("win"):
            import ctypes
            k = ctypes.windll.kernel32
            k.SetPriorityClass(k.GetCurrentProcess(), 0x4000)          # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(5)
    except Exception:
        pass


def holder():
    """-> {"job", "pid", "started"} of the heavy job running now, or None (best effort: only for the message)."""
    try:
        with open(_paths()[1], encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


@contextlib.contextmanager
def one_at_a_time(job, cfg=None, say=print, wait_s=7200, poll=3.0):
    mode = ((cfg or {}).get("behavior") or {}).get("heavy_jobs", "one")
    if mode == "any" or os.environ.get(HELD):
        yield
        return
    lower_priority()
    lock, info = _paths()
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    f = open(lock, "a+")
    t0, told = time.time(), False
    while not _try_lock(f):
        h = holder() or {}
        if time.time() - t0 > wait_s:
            f.close()
            raise SystemExit(f"[!] {h.get('job', 'another heavy job')} is still running after {int(wait_s / 60)} min; {job} did not start. "
                             "Run it when that one ends.")
        if not told:
            when = time.strftime("%H:%M", time.localtime(h["started"])) if h.get("started") else "earlier"
            say(f"[wait] {h.get('job', 'another heavy job')} is already running (pid {h.get('pid', '?')}, since {when}); {job} starts when it "
                "ends, so this machine is never asked to do both. Settings > Behaviour > Heavy jobs = any turns this off.")
            told = True
        time.sleep(poll)
    try:
        core._atomic_write(info, json.dumps({"job": job, "pid": os.getpid(), "started": time.time()}))
    except OSError:
        pass
    os.environ[HELD] = job
    try:
        yield
    finally:
        os.environ.pop(HELD, None)
        try:
            if (holder() or {}).get("pid") == os.getpid():
                os.remove(info)
        except OSError:
            pass
        _unlock(f)
        f.close()
