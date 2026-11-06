"""Pipelining on one laptop: the GPU writes attempt N+1 while the CPU checks attempt N.

A fix loop spends most of its time waiting on the model, and the checks of the last attempt (tests, lint, the fake-code scan, indexing)
are CPU work that can run in the meantime. `submit_checks(fn, *a)` starts such a check in a worker thread at below-normal priority on
the efficiency cores (arch_hw.ecore_mask) and returns a token at once; `result(token)` waits for that check and hands back what it
returned (or raises what it raised). The caller joins every pending result BEFORE it decides a verdict, so overlap changes how long a
run takes, never what it concludes.

The queue is bounded (`max_pending`): when it is full `submit_checks` waits for a slot instead of piling up work, so memory and CPU
stay flat. `workers=0` runs the check inline (no threads), which is also what happens if a thread cannot be started.
"""
import os
import queue as _queue
import sys
import threading
import time

_BELOW_NORMAL = -1          # SetThreadPriority THREAD_PRIORITY_BELOW_NORMAL


def _lower_this_thread(mask=None):
    """Below-normal priority and the efficiency cores for the CALLING thread. Best effort; never raises. -> what was applied."""
    done = {"priority": False, "ecores": False}
    try:
        if mask is None:
            import arch_hw
            mask = arch_hw.ecore_mask()
        if sys.platform.startswith("win"):
            import ctypes
            k = ctypes.windll.kernel32
            k.GetCurrentThread.restype = ctypes.c_void_p
            k.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
            h = k.GetCurrentThread()
            done["priority"] = bool(k.SetThreadPriority(h, _BELOW_NORMAL))
            if mask:
                k.SetThreadAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
                k.SetThreadAffinityMask.restype = ctypes.c_size_t
                done["ecores"] = bool(k.SetThreadAffinityMask(h, mask))
        else:
            if mask and hasattr(os, "sched_setaffinity"):
                os.sched_setaffinity(0, {i for i in range(mask.bit_length()) if mask >> i & 1})      # pid 0 = the calling thread on Linux
                done["ecores"] = True
            try:
                os.nice(5)                                                                          # per-thread on Linux
                done["priority"] = True
            except OSError:
                pass
    except Exception:
        pass
    return done


class Overlap:
    def __init__(self, workers=1, max_pending=4, low_priority=True, mask=None):
        self.workers = max(0, int(workers))
        self.max_pending = max(1, int(max_pending))
        self.low_priority = low_priority
        self.mask = mask
        self._q = _queue.Queue()
        self._slots = threading.Semaphore(self.max_pending)       # bounded: submit waits when this many are queued or running
        self._lock = threading.Lock()
        self._jobs = {}
        self._next = 1
        self._threads = []
        self._closed = False
        self.applied = []                                          # per worker: what the priority/affinity call managed
        self.stats = {"submitted": 0, "inline": 0, "waited_slot": 0}

    def _start(self):
        while len(self._threads) < self.workers:
            t = threading.Thread(target=self._loop, name=f"overlap-{len(self._threads)}", daemon=True)
            try:
                t.start()
            except RuntimeError:
                break
            self._threads.append(t)

    def _loop(self):
        if self.low_priority:
            self.applied.append(_lower_this_thread(self.mask))
        while True:
            job = self._q.get()
            if job is None:
                return
            self._run(job)
            self._slots.release()

    @staticmethod
    def _run(job):
        job["t0"] = time.time()
        try:
            job["value"] = job["fn"](*job["a"], **job["kw"])
        except BaseException as ex:                                # the caller sees it at result(), in order, not in a dead thread
            job["error"] = ex
        job["t1"] = time.time()
        job["done"].set()

    def submit_checks(self, fn, *a, **kw):
        """Start fn(*a, **kw) in the background. -> a token for result(). Waits for a free slot when the queue is full."""
        if self._closed:
            raise RuntimeError("this Overlap is closed")
        job = {"fn": fn, "a": a, "kw": kw, "done": threading.Event(), "value": None, "error": None, "t0": None, "t1": None}
        with self._lock:
            tok = self._next
            self._next += 1
            self._jobs[tok] = job
            self.stats["submitted"] += 1
        if self.workers == 0:
            return self._inline(tok, job)
        if not self._slots.acquire(blocking=False):
            self.stats["waited_slot"] += 1
            self._slots.acquire()
        self._start()
        if not self._threads:                                      # no thread could be started: do it here, same answer
            self._slots.release()
            return self._inline(tok, job)
        self._q.put(job)
        return tok

    def _inline(self, tok, job):
        self.stats["inline"] += 1
        self._run(job)
        return tok

    def done(self, token):
        return self._jobs[token]["done"].is_set()

    def result(self, token, timeout=None):
        """Wait for that check -> its return value; raises what it raised, or TimeoutError. Can be asked more than once."""
        job = self._jobs[token]
        if not job["done"].wait(timeout):
            raise TimeoutError(f"check {token} did not finish within {timeout} s")
        if job["error"] is not None:
            raise job["error"]
        return job["value"]

    def join_all(self, tokens=None, timeout=None):
        """Every result in the order the tokens were given (default: submission order). A failed check comes back as the exception
        object, so a verdict can be built from all of them."""
        out = []
        for t in (tokens if tokens is not None else sorted(self._jobs)):
            try:
                out.append(self.result(t, timeout))
            except BaseException as ex:
                out.append(ex)
        return out

    def span(self, token):
        j = self._jobs[token]
        return (j["t0"], j["t1"])

    def close(self, wait=True):
        self._closed = True
        for _ in self._threads:
            self._q.put(None)
        if wait:
            for t in self._threads:
                t.join(timeout=30)


_DEFAULT = None
_DLOCK = threading.Lock()


def default(cfg=None):
    """The shared Overlap. Settings behavior.overlap_checks = off makes it run checks inline (workers=0)."""
    global _DEFAULT
    with _DLOCK:
        if _DEFAULT is None:
            on = ((cfg or {}).get("behavior") or {}).get("overlap_checks", "on") != "off"
            _DEFAULT = Overlap(workers=1 if on else 0, max_pending=4)
        return _DEFAULT


def submit_checks(fn, *a, **kw):
    return default().submit_checks(fn, *a, **kw)


def result(token, timeout=None):
    return default().result(token, timeout)
