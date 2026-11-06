"""distributed: the building blocks of systems that talk over a network, one module each, judged by a hidden acceptance check.

Thirteen templates (rate limiters, backoff with jitter, idempotent handlers, a lease queue, consistent hashing, an in-process
raft-style election, a circuit breaker, bulkheads, a lock with TTL and fencing tokens, a saga, vector clocks, backpressure), each
expanded into five variants (other names, other numbers, other node sets), so 65 tasks in all. Every task states its public interface
exactly, ships a fixture (`fakeclock.py`, a clock moved by hand, and the house rules), and is judged by code the model never sees:
deterministic, standard library only, no real sleeping. Each carries a reference solution (the oracle proves the hidden check can be
met) and a plausible wrong one (it proves the hidden check catches the classic mistake for that pattern).

The ids sort after the e2e family (`train-...`), so `eval --quick` and the representative set are unchanged by this file.

    python evals/families/distributed.py --check            # every task: reference passes every judge, wrong answer is caught
    python evals/families/distributed.py --list
    python evals/families/distributed.py --night --hours 8  # the overnight training run (docs/training_plan.md)
"""
import os
import random
import re
import sys

FLOW = "build"
DEFAULT_N = 1                 # a normal `eval` carries one task of this family; `--n 65 --tags distributed` runs them all
PREFIX = "train-dist"
TAG = "distributed"
MIN_TASKS, MIN_TEMPLATES = 60, 12

FAKECLOCK = '''"""A clock you move by hand, so time-based code can be tested without sleeping. Provided by the project: do not edit."""


class FakeClock:
    def __init__(self, start=0.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        if seconds < 0:
            raise ValueError("time does not go backwards")
        self.now += seconds
        return self.now
'''

CONVENTIONS = """# House rules

- Standard library only.
- Time always comes from an injected `clock`: a zero-argument callable returning seconds as a float. Library code never sleeps
  and never reads the wall clock itself when it was given a clock.
- `fakeclock.FakeClock` is provided for your tests (`clock = FakeClock(); clock.advance(2.5)`). Do not edit it.
- Tests live in tests/ (`tests/test_<module>.py`) and must run under both pytest and `python -m unittest`, so write
  `unittest.TestCase` classes. Put the project root on sys.path at the top of the test file.
"""

TEST_HEAD = '''import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
'''
TEST_TAIL = '''

if __name__ == "__main__":
    unittest.main()
'''
CLOCK = '''class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t
'''


def _fill(text, **marks):
    """Replace @KEY@ markers. Strings go in as they are (names); anything else as its repr (numbers, lists, dicts)."""
    for k, v in marks.items():
        text = text.replace("@" + k + "@", v if isinstance(v, str) else repr(v))
    left = re.findall(r"@[A-Z][A-Z0-9_]*@", text)
    if left:
        raise ValueError(f"unfilled markers: {sorted(set(left))}")
    return text


def _mutate(src, old, new):
    """The wrong answer is the right one with one plausible mistake. A mutation that no longer applies is a bug in this file."""
    if old not in src:
        raise ValueError("wrong-answer mutation does not apply: " + old[:60])
    return src.replace(old, new, 1)


# ================================================================== 1. token bucket
BUCKET_V = [("ratelimit", "TokenBucket", 5, 2.0), ("throttle", "Throttle", 3, 1.0), ("quota", "QuotaBucket", 10, 0.5),
            ("apigate", "RequestBucket", 4, 4.0), ("ingress", "IngressLimiter", 2, 0.25)]
BUCKET_SRC = r'''"""A token-bucket rate limiter: `capacity` tokens at most, refilled continuously at `rate` per second."""


class @CLS@:
    def __init__(self, capacity, rate, clock):
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.capacity = capacity
        self.rate = float(rate)
        self.clock = clock
        self._tokens = float(capacity)
        self._last = clock()

    def _refill(self):
        now = self.clock()
        elapsed = max(0.0, now - self._last)
        self._tokens = min(float(self.capacity), self._tokens + elapsed * self.rate)
        self._last = now

    def tokens(self):
        self._refill()
        return self._tokens

    def allow(self, cost=1):
        if cost < 1 or cost > self.capacity:
            raise ValueError(f"cost must be between 1 and {self.capacity}")
        self._refill()
        if self._tokens >= cost:
            self._tokens -= cost
            return True
        return False
'''
BUCKET_TESTS = r'''from fakeclock import FakeClock  # noqa: E402
from @MOD@ import @CLS@  # noqa: E402


class BucketTest(unittest.TestCase):
    def test_full_bucket_then_refuses(self):
        b = @CLS@(@CAP@, @RATE@, FakeClock())
        self.assertTrue(all(b.allow() for _ in range(@CAP@)))
        self.assertFalse(b.allow())

    def test_refills_over_time(self):
        clock = FakeClock()
        b = @CLS@(@CAP@, @RATE@, clock)
        for _ in range(@CAP@):
            b.allow()
        clock.advance(1.0 / @RATE@)
        self.assertTrue(b.allow())

    def test_bad_cost(self):
        with self.assertRaises(ValueError):
            @CLS@(@CAP@, @RATE@, FakeClock()).allow(0)
'''
BUCKET_HIDDEN = r'''
from @MOD@ import @CLS@
@CLOCK@
C, R = @CAP@, @RATE@
c = Clock(1000.0)
b = @CLS@(C, R, c)
assert [b.allow() for _ in range(C)] == [True] * C, "a full bucket must allow `capacity` calls"
assert b.allow() is False, "an empty bucket must refuse"
c.t += 1.0 / R
assert b.allow() is True and b.allow() is False, "one token comes back every 1/rate seconds"
c.t += 1000.0
assert abs(b.tokens() - C) < 1e-9, ("the refill must stop at capacity", b.tokens())
assert sum(1 for _ in range(C + 5) if b.allow()) == C
c.t += C / R
assert b.allow(C) is True and b.allow(1) is False
for bad in (0, C + 1):
    try:
        b.allow(bad)
        raise SystemExit("cost %r was accepted" % bad)
    except ValueError:
        pass
for args in ((0, R), (C, 0)):
    try:
        @CLS@(args[0], args[1], c)
        raise SystemExit("bad settings were accepted: %r" % (args,))
    except ValueError:
        pass
print("ok")
'''


def t_bucket(v):
    mod, cls, cap, rate = v
    m = dict(MOD=mod, CLS=cls, CAP=cap, RATE=rate, CLOCK=CLOCK)
    goal = (f"Build a Python module `{mod}.py` with a token-bucket rate limiter, with tests in tests/. Public interface, exactly: "
            f"`from {mod} import {cls}`; `{cls}(capacity, rate, clock)` where capacity is the most tokens the bucket holds (it starts "
            f"full), rate is tokens added per second, and clock is a zero-argument callable returning the time in seconds. "
            f"`allow(cost=1)` returns True and takes `cost` tokens when that many are available, else returns False and takes nothing. "
            f"Tokens refill continuously at `rate` and never exceed capacity. `tokens()` returns the current, refilled count as a float. "
            f"A cost below 1 or above capacity raises ValueError; capacity below 1 or a rate <= 0 raises ValueError. Callers here use "
            f"capacity {cap} and {rate} tokens per second. Read CONVENTIONS.md.")
    src = _fill(BUCKET_SRC, **m)
    wrong = _mutate(src, "min(float(self.capacity), self._tokens + elapsed * self.rate)", "self._tokens + elapsed * self.rate")
    return mod, goal, _fill(BUCKET_HIDDEN, **m), src, _fill(BUCKET_TESTS, **m), wrong


# ================================================================== 2. sliding window per key
WINDOW_V = [("window", "SlidingWindow", 3, 10.0), ("perkey", "PerKeyLimiter", 5, 60.0), ("ratewindow", "WindowLimiter", 2, 1.0),
            ("loginguard", "AttemptLimiter", 4, 30.0), ("burst", "BurstGuard", 10, 5.0)]
WINDOW_SRC = r'''"""A sliding-window rate limiter per key: at most `limit` allowed calls in any `window` seconds."""
import collections


class @CLS@:
    def __init__(self, limit, window, clock):
        if limit < 1 or window <= 0:
            raise ValueError("limit must be at least 1 and window positive")
        self.limit = limit
        self.window = window
        self.clock = clock
        self._log = {}

    def _calls(self, key):
        calls = self._log.setdefault(key, collections.deque())
        now = self.clock()
        while calls and now >= calls[0] + self.window:
            calls.popleft()
        return calls

    def count(self, key):
        return len(self._calls(key))

    def allow(self, key):
        calls = self._calls(key)
        if len(calls) >= self.limit:
            return False
        calls.append(self.clock())
        return True
'''
WINDOW_TESTS = r'''from fakeclock import FakeClock  # noqa: E402
from @MOD@ import @CLS@  # noqa: E402


class WindowTest(unittest.TestCase):
    def test_limit_per_key(self):
        lim = @CLS@(@LIMIT@, @WINDOW@, FakeClock())
        for _ in range(@LIMIT@):
            self.assertTrue(lim.allow("a"))
        self.assertFalse(lim.allow("a"))
        self.assertTrue(lim.allow("b"))

    def test_window_slides(self):
        clock = FakeClock()
        lim = @CLS@(@LIMIT@, @WINDOW@, clock)
        for _ in range(@LIMIT@):
            lim.allow("a")
        clock.advance(@WINDOW@)
        self.assertEqual(lim.count("a"), 0)
        self.assertTrue(lim.allow("a"))
'''
WINDOW_HIDDEN = r'''
from @MOD@ import @CLS@
@CLOCK@
N, W = @LIMIT@, @WINDOW@
c = Clock()
lim = @CLS@(N, W, c)
assert all(lim.allow("u1") for _ in range(N)) and lim.allow("u1") is False
assert lim.allow("u2") is True, "every key has its own window"
c.t += W / 2
assert lim.allow("u1") is False, "the first calls are still inside the window"
c.t += W / 2
assert lim.count("u1") == 0, "calls older than the window no longer count, and a refused call is never recorded"
assert lim.allow("u1") is True and lim.count("u2") == 0
try:
    @CLS@(0, W, c)
    raise SystemExit("limit 0 was accepted")
except ValueError:
    pass
print("ok")
'''


def t_window(v):
    mod, cls, limit, window = v
    m = dict(MOD=mod, CLS=cls, LIMIT=limit, WINDOW=window, CLOCK=CLOCK)
    goal = (f"Build a Python module `{mod}.py` with a per-key sliding-window rate limiter, with tests in tests/. Public interface, "
            f"exactly: `from {mod} import {cls}`; `{cls}(limit, window, clock)` (clock: zero-argument callable returning seconds). "
            f"`allow(key)` returns True and records the call when fewer than `limit` calls for that key were allowed in the last "
            f"`window` seconds, else returns False (a refused call is not recorded). A call made at time t counts while now < t + window. "
            f"Every key has its own window. `count(key)` returns how many allowed calls for the key are inside the window now. "
            f"limit below 1 or window <= 0 raises ValueError. Callers here use limit {limit} per {window} seconds. Read CONVENTIONS.md.")
    src = _fill(WINDOW_SRC, **m)
    wrong = _mutate(src, "self._log.setdefault(key, collections.deque())", 'self._log.setdefault("*", collections.deque())')
    return mod, goal, _fill(WINDOW_HIDDEN, **m), src, _fill(WINDOW_TESTS, **m), wrong


# ================================================================== 3. retry with capped exponential backoff and full jitter
RETRY_V = [("retry_policy", "call_with_retry", 6, 0.1, 1.0, 2.0), ("backoff", "retry", 5, 0.5, 3.0, 2.0),
           ("resilient", "with_retries", 4, 1.0, 5.0, 3.0), ("redo", "retry_call", 7, 0.05, 0.5, 2.0),
           ("persist_call", "attempt", 5, 0.2, 2.0, 3.0)]
RETRY_SRC = r'''"""Retries with capped exponential backoff and full jitter."""
import random
import time


def @FN@(fn, attempts, base, cap, factor=2.0, retry_on=(Exception,), sleep=time.sleep, rand=random.random):
    """Call fn() up to `attempts` times. After failed try i (counting from 0) that is not the last, wait rand() * min(cap, base * factor**i)."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    for i in range(attempts):
        try:
            return fn()
        except retry_on:
            if i == attempts - 1:
                raise
            sleep(rand() * min(cap, base * factor ** i))
    raise RuntimeError("the loop always returns or raises")
'''
RETRY_TESTS = r'''from @MOD@ import @FN@  # noqa: E402


class RetryTest(unittest.TestCase):
    def test_succeeds_after_failures(self):
        calls, waits = [], []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise IOError("not yet")
            return "done"

        self.assertEqual(@FN@(flaky, @ATT@, @BASE@, @CAP@, factor=@FACTOR@, sleep=waits.append, rand=lambda: 1.0), "done")
        self.assertEqual(len(waits), 2)

    def test_gives_up(self):
        def always():
            raise IOError("down")

        with self.assertRaises(IOError):
            @FN@(always, @ATT@, @BASE@, @CAP@, factor=@FACTOR@, sleep=lambda s: None, rand=lambda: 0.0)
'''
RETRY_HIDDEN = r'''
from @MOD@ import @FN@
A, BASE, CAP, F = @ATT@, @BASE@, @CAP@, @FACTOR@


class Boom(Exception):
    pass


class Fatal(Exception):
    pass


def flaky(n):
    state = {"left": n, "calls": 0}

    def fn():
        state["calls"] += 1
        if state["left"] > 0:
            state["left"] -= 1
            raise Boom("try again")
        return "done"
    return fn, state


slept = []
fn, st = flaky(2)
assert @FN@(fn, A, BASE, CAP, factor=F, retry_on=(Boom,), sleep=slept.append, rand=lambda: 0.5) == "done"
assert st["calls"] == 3 and len(slept) == 2, (st, slept)
assert all(abs(a - b) < 1e-9 for a, b in zip(slept, [0.5 * BASE, 0.5 * BASE * F])), slept
slept = []
fn, st = flaky(1000)
try:
    @FN@(fn, A, BASE, CAP, factor=F, retry_on=(Boom,), sleep=slept.append, rand=lambda: 0.5)
    raise SystemExit("it gave up without raising the last error")
except Boom:
    pass
want = [0.5 * min(CAP, BASE * F ** i) for i in range(A - 1)]
assert st["calls"] == A, st
assert len(slept) == A - 1 and all(abs(a - b) < 1e-9 for a, b in zip(slept, want)), ("delays must be capped", slept, want)
slept = []


def fatal():
    raise Fatal("no")


try:
    @FN@(fatal, A, BASE, CAP, factor=F, retry_on=(Boom,), sleep=slept.append, rand=lambda: 0.5)
    raise SystemExit("a non-retryable error was swallowed")
except Fatal:
    pass
assert slept == [], "a non-retryable error must not wait"
seen = []
fn, st = flaky(1)
@FN@(fn, A, BASE, CAP, factor=F, retry_on=(Boom,), sleep=seen.append, rand=lambda: 1.0)
assert abs(seen[0] - BASE) < 1e-9, "the jitter multiplies the capped delay"
try:
    @FN@(fn, 0, BASE, CAP, sleep=slept.append)
    raise SystemExit("attempts=0 was accepted")
except ValueError:
    pass
print("ok")
'''


def t_retry(v):
    mod, fn, att, base, cap, factor = v
    assert base * factor ** (att - 2) > cap, "every variant must reach the cap, or the wrong answer goes unnoticed"
    m = dict(MOD=mod, FN=fn, ATT=att, BASE=base, CAP=cap, FACTOR=factor)
    goal = (f"Build a Python module `{mod}.py` with retries using capped exponential backoff and full jitter, with tests in tests/. "
            f"Public interface, exactly: `from {mod} import {fn}`; `{fn}(fn, attempts, base, cap, factor=2.0, retry_on=(Exception,), "
            f"sleep=time.sleep, rand=random.random)` calls fn() up to `attempts` times and returns its value. After failed try i "
            f"(counting from 0) that is not the last, it calls sleep(rand() * min(cap, base * factor**i)). When every try fails it "
            f"re-raises the last exception. An exception that is not an instance of retry_on propagates at once, without sleeping. "
            f"attempts below 1 raises ValueError. sleep and rand are injected so tests never wait. Callers here use attempts={att}, "
            f"base={base}, cap={cap}, factor={factor}. Read CONVENTIONS.md.")
    src = _fill(RETRY_SRC, **m)
    wrong = _mutate(src, "sleep(rand() * min(cap, base * factor ** i))", "sleep(rand() * base * factor ** i)")
    return mod, goal, _fill(RETRY_HIDDEN, **m), src, _fill(RETRY_TESTS, **m), wrong


# ================================================================== 4. idempotent message handler
IDEM_V = [("dedupe", "IdempotentHandler", "id", 3), ("once", "OnceOnly", "message_id", 4), ("inbox", "Inbox", "event_id", 2),
          ("exactly_once", "ExactlyOnce", "request_id", 5), ("replay_guard", "ReplayGuard", "key", 3)]
IDEM_SRC = r'''"""Exactly-once effects from at-least-once delivery: a message id already handled returns the stored result instead of running again."""
import collections


class @CLS@:
    def __init__(self, handler, remember=1000):
        if remember < 1:
            raise ValueError("remember must be at least 1")
        self.handler = handler
        self.remember = remember
        self._done = collections.OrderedDict()

    def processed(self, msg_id):
        return msg_id in self._done

    def handle(self, msg):
        if not isinstance(msg, dict) or @KEY@ not in msg:
            raise ValueError("a message needs its " + @KEY@ + " field")
        msg_id = msg[@KEY@]
        if msg_id in self._done:
            return self._done[msg_id]
        result = self.handler(msg)
        self._done[msg_id] = result
        while len(self._done) > self.remember:
            self._done.popitem(last=False)
        return result
'''
IDEM_TESTS = r'''from @MOD@ import @CLS@  # noqa: E402


class IdempotentTest(unittest.TestCase):
    def test_duplicate_is_not_processed_twice(self):
        seen = []
        h = @CLS@(lambda m: seen.append(m[@KEY@]) or len(seen), @LIMIT@)
        self.assertEqual(h.handle({@KEY@: "a"}), 1)
        self.assertEqual(h.handle({@KEY@: "a"}), 1)
        self.assertEqual(seen, ["a"])

    def test_missing_id(self):
        with self.assertRaises(ValueError):
            @CLS@(lambda m: None, @LIMIT@).handle({"body": 1})
'''
IDEM_HIDDEN = r'''
from @MOD@ import @CLS@
K, N = @KEY@, @LIMIT@
calls = []


def work(msg):
    calls.append(msg[K])
    if msg.get("fail"):
        raise RuntimeError("downstream is down")
    return msg[K] * 10


h = @CLS@(work, N)
assert h.handle({K: 1}) == 10 and h.handle({K: 1}) == 10 and calls == [1], calls
try:
    h.handle({K: 2, "fail": True})
    raise SystemExit("the handler's error was swallowed")
except RuntimeError:
    pass
assert not h.processed(2), "a message whose handler failed must not count as processed"
assert h.handle({K: 2}) == 20 and calls == [1, 2, 2], calls
assert h.processed(1) and h.processed(2)
for i in range(100, 100 + N):
    h.handle({K: i})
assert not h.processed(1), "only the most recent ids are remembered"
before = len(calls)
h.handle({K: 1})
assert len(calls) == before + 1, "a forgotten id is processed again"
try:
    h.handle({"other": 1})
    raise SystemExit("a message without an id was accepted")
except ValueError:
    pass
try:
    @CLS@(work, 0)
    raise SystemExit("remember=0 was accepted")
except ValueError:
    pass
print("ok")
'''


def t_idem(v):
    mod, cls, key, limit = v
    m = dict(MOD=mod, CLS=cls, KEY=repr(key), LIMIT=limit)
    goal = (f"Build a Python module `{mod}.py` with an idempotent message handler, with tests in tests/. Messages are delivered "
            f"at least once, so the same message can arrive twice. Public interface, exactly: `from {mod} import {cls}`; "
            f"`{cls}(handler, remember=1000)`; messages are dicts identified by their `{key}` field. `handle(msg)` calls "
            f"handler(msg) the first time an id is seen, stores and returns its result; a repeated id returns the stored result "
            f"without calling the handler. When the handler raises, nothing is recorded (a redelivery runs it again) and the error "
            f"propagates. Only the most recent `remember` ids are kept: recording one more forgets the oldest. `processed(msg_id)` "
            f"says whether an id is remembered. A message without `{key}` raises ValueError; remember below 1 raises ValueError. "
            f"Callers here use remember={limit}. Read CONVENTIONS.md.")
    src = _fill(IDEM_SRC, **m)
    wrong = _mutate(src, "        result = self.handler(msg)\n        self._done[msg_id] = result\n",
                    "        self._done[msg_id] = None\n        result = self.handler(msg)\n        self._done[msg_id] = result\n")
    return mod, goal, _fill(IDEM_HIDDEN, **m), src, _fill(IDEM_TESTS, **m), wrong


# ================================================================== 5. work queue with leases
LEASE_V = [("leasequeue", "LeaseQueue", 30.0, 3), ("jobs", "JobQueue", 10.0, 2), ("taskq", "TaskQueue", 5.0, 4),
           ("workq", "WorkQueue", 60.0, 3), ("visibility", "VisibilityQueue", 2.5, 2)]
LEASE_SRC = r'''"""A work queue with leases: a worker borrows an item for a while; an item nobody acknowledged in time is handed out again."""
import collections


class @CLS@:
    def __init__(self, lease_seconds, max_attempts, clock):
        if lease_seconds <= 0 or max_attempts < 1:
            raise ValueError("lease_seconds must be positive and max_attempts at least 1")
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.clock = clock
        self._next = 1
        self._ready = collections.deque()
        self._items = {}
        self._attempts = {}
        self._leased = {}
        self._dead = []

    def put(self, item):
        item_id = self._next
        self._next += 1
        self._items[item_id] = item
        self._attempts[item_id] = 0
        self._ready.append(item_id)
        return item_id

    def _reclaim(self):
        now = self.clock()
        expired = sorted(i for i, (worker, deadline) in self._leased.items() if now >= deadline)
        back = []
        for item_id in expired:
            del self._leased[item_id]
            if self._attempts[item_id] >= self.max_attempts:
                self._dead.append(self._items.pop(item_id))
                del self._attempts[item_id]
            else:
                back.append(item_id)
        self._ready.extendleft(reversed(back))

    def lease(self, worker):
        self._reclaim()
        if not self._ready:
            return None
        item_id = self._ready.popleft()
        self._attempts[item_id] += 1
        self._leased[item_id] = (worker, self.clock() + self.lease_seconds)
        return item_id, self._items[item_id]

    def ack(self, item_id, worker):
        self._reclaim()
        held = self._leased.get(item_id)
        if held is None or held[0] != worker:
            return False
        del self._leased[item_id]
        del self._attempts[item_id]
        del self._items[item_id]
        return True

    def size(self):
        self._reclaim()
        return len(self._ready)

    def dead(self):
        self._reclaim()
        return list(self._dead)
'''
LEASE_TESTS = r'''from fakeclock import FakeClock  # noqa: E402
from @MOD@ import @CLS@  # noqa: E402


class LeaseTest(unittest.TestCase):
    def test_lease_and_ack(self):
        q = @CLS@(@LEASE@, @MAX@, FakeClock())
        item_id = q.put("job")
        self.assertEqual(q.lease("w1"), (item_id, "job"))
        self.assertIsNone(q.lease("w2"))
        self.assertTrue(q.ack(item_id, "w1"))
        self.assertEqual(q.size(), 0)

    def test_expired_lease_is_redelivered(self):
        clock = FakeClock()
        q = @CLS@(@LEASE@, @MAX@, clock)
        item_id = q.put("job")
        q.lease("w1")
        clock.advance(@LEASE@)
        self.assertEqual(q.lease("w2"), (item_id, "job"))
'''
LEASE_HIDDEN = r'''
from @MOD@ import @CLS@
@CLOCK@
L, M = @LEASE@, @MAX@
c = Clock()
q = @CLS@(L, M, c)
a, b = q.put("a"), q.put("b")
assert a != b
assert q.lease("w1") == (a, "a")
assert q.lease("w2") == (b, "b")
assert q.lease("w3") is None, "nothing is left to lease"
assert q.ack(b, "w1") is False, "only the holder may acknowledge"
assert q.ack(b, "w2") is True and q.ack(b, "w2") is False, "an acknowledged item is gone"
c.t += L
assert q.ack(a, "w1") is False, "a lease that ran out cannot be acknowledged"
assert q.lease("w4") == (a, "a"), "an expired lease is delivered again"
cc = q.put("c")
c.t += L / 2
assert q.lease("w5") == (cc, "c")
assert q.ack(cc, "w5") is True and q.ack(a, "w4") is True
p = q.put("poison")
for i in range(M):
    c.t += L
    got = q.lease("w%d" % i)
    assert got == (p, "poison"), (i, got)
c.t += L
assert q.lease("w-last") is None and q.dead() == ["poison"], ("leased max_attempts times without an ack: dead letter", q.dead())
assert q.size() == 0
print("ok")
'''


def t_lease(v):
    mod, cls, lease, mx = v
    m = dict(MOD=mod, CLS=cls, LEASE=lease, MAX=mx, CLOCK=CLOCK)
    goal = (f"Build a Python module `{mod}.py` with a work queue that hands out items on leases, with tests in tests/. Public "
            f"interface, exactly: `from {mod} import {cls}`; `{cls}(lease_seconds, max_attempts, clock)`. `put(item)` returns a new "
            f"integer id. `lease(worker)` returns `(id, item)` for the next item, first in first out, or None; the lease ends at "
            f"lease time + lease_seconds and has expired once clock() >= that. An expired item is offered again before items never "
            f"leased; but an item that expires after it was already leased max_attempts times goes to the dead-letter list instead. "
            f"`ack(id, worker)` returns True and removes the item for good only when that worker holds an unexpired lease on it, "
            f"else False. `size()` is the number of items waiting to be leased; `dead()` lists the dead items in order. lease_seconds "
            f"<= 0 or max_attempts < 1 raises ValueError. Callers here use a {lease}-second lease and {mx} attempts. Read CONVENTIONS.md.")
    src = _fill(LEASE_SRC, **m)
    wrong = _mutate(src, "    def ack(self, item_id, worker):\n        self._reclaim()\n", "    def ack(self, item_id, worker):\n")
    return mod, goal, _fill(LEASE_HIDDEN, **m), src, _fill(LEASE_TESTS, **m), wrong


# ================================================================== 6. consistent hashing
RING_V = [("hashring", "HashRing", ["n1", "n2", "n3"], 100), ("ring", "ConsistentRing", ["cache-a", "cache-b", "cache-c", "cache-d"], 64),
          ("shards", "ShardRing", ["db1", "db2", "db3", "db4", "db5"], 128), ("placement", "Placement", ["east", "west", "north"], 80),
          ("partitioner", "Partitioner", ["p0", "p1", "p2", "p3"], 160)]
RING_SRC = r'''"""Consistent hashing: each node sits at `replicas` points on a ring; a key belongs to the first point at or after its hash."""
import bisect
import hashlib


def _hash(text):
    return int(hashlib.md5(text.encode("utf-8")).hexdigest()[:16], 16)


class @CLS@:
    def __init__(self, nodes, replicas=100):
        if replicas < 1:
            raise ValueError("replicas must be at least 1")
        self.replicas = replicas
        self._points = []
        self._owner = {}
        self._nodes = set()
        for node in nodes:
            self.add(node)

    def add(self, node):
        if node in self._nodes:
            raise ValueError(f"{node} is already on the ring")
        self._nodes.add(node)
        for i in range(self.replicas):
            p = _hash(f"{node}#{i}")
            if p in self._owner:
                continue
            self._owner[p] = node
            bisect.insort(self._points, p)

    def remove(self, node):
        if node not in self._nodes:
            raise KeyError(node)
        self._nodes.discard(node)
        self._points = [p for p in self._points if self._owner[p] != node]
        self._owner = {p: n for p, n in self._owner.items() if n != node}

    def node_for(self, key):
        if not self._points:
            raise LookupError("the ring has no nodes")
        i = bisect.bisect_left(self._points, _hash(key))
        return self._owner[self._points[i % len(self._points)]]

    def nodes(self):
        return sorted(self._nodes)
'''
RING_WRONG = r'''"""Placement by hash modulo the node count."""
import hashlib


class @CLS@:
    def __init__(self, nodes, replicas=100):
        self.replicas = replicas
        self._nodes = []
        for node in nodes:
            self.add(node)

    def add(self, node):
        if node in self._nodes:
            raise ValueError(f"{node} is already on the ring")
        self._nodes.append(node)

    def remove(self, node):
        if node not in self._nodes:
            raise KeyError(node)
        self._nodes.remove(node)

    def node_for(self, key):
        if not self._nodes:
            raise LookupError("the ring has no nodes")
        h = int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16)
        return self._nodes[h % len(self._nodes)]

    def nodes(self):
        return sorted(self._nodes)
'''
RING_TESTS = r'''from @MOD@ import @CLS@  # noqa: E402


class RingTest(unittest.TestCase):
    def test_stable_and_known_nodes(self):
        ring = @CLS@(@NODES@, @REPLICAS@)
        first = ring.node_for("k1")
        self.assertIn(first, @NODES@)
        self.assertEqual(ring.node_for("k1"), first)

    def test_empty_ring(self):
        with self.assertRaises(LookupError):
            @CLS@([], @REPLICAS@).node_for("k")
'''
RING_HIDDEN = r'''
from @MOD@ import @CLS@
NODES, R = @NODES@, @REPLICAS@
keys = ["user:%d" % i for i in range(2000)]
ring = @CLS@(list(NODES), R)
before = {k: ring.node_for(k) for k in keys}
assert set(before.values()) <= set(NODES)
assert all(ring.node_for(k) == before[k] for k in keys[:50]), "lookups must be stable"
fair = len(keys) / len(NODES)
counts = {n: sum(1 for v in before.values() if v == n) for n in NODES}
assert min(counts.values()) >= 0.4 * fair, ("replicas must spread the keys", counts)
ring.add("newcomer")
after = {k: ring.node_for(k) for k in keys}
moved = [k for k in keys if after[k] != before[k]]
assert moved and all(after[k] == "newcomer" for k in moved), "adding a node may only move keys TO the new node"
assert len(moved) < 0.5 * len(keys), len(moved)
ring.remove("newcomer")
assert {k: ring.node_for(k) for k in keys} == before, "removing it again restores the old placement"
gone = NODES[0]
ring.remove(gone)
again = {k: ring.node_for(k) for k in keys}
assert all(again[k] == before[k] for k in keys if before[k] != gone), "only the removed node's keys may move"
assert gone not in again.values()
try:
    ring.remove("never-there")
    raise SystemExit("removing an unknown node did not raise KeyError")
except KeyError:
    pass
try:
    ring.add(NODES[1])
    raise SystemExit("a node added twice was accepted")
except ValueError:
    pass
try:
    @CLS@([], R).node_for("x")
    raise SystemExit("an empty ring answered")
except LookupError:
    pass
print("ok")
'''


def t_ring(v):
    mod, cls, nodes, replicas = v
    m = dict(MOD=mod, CLS=cls, NODES=nodes, REPLICAS=replicas)
    goal = (f"Build a Python module `{mod}.py` with consistent hashing, with tests in tests/. Public interface, exactly: "
            f"`from {mod} import {cls}`; `{cls}(nodes, replicas=100)` places every node at `replicas` points on a hash ring, using a "
            f"stable hash (for example hashlib.md5; never Python's hash(), which changes between processes). `node_for(key)` returns "
            f"the node owning the first point at or after the key's hash, wrapping around. `add(node)` and `remove(node)`: adding a "
            f"node may only move keys to that node, removing one may only move that node's keys. A node added twice raises "
            f"ValueError; removing an unknown node raises KeyError; node_for on an empty ring raises LookupError. `nodes()` returns "
            f"the node names sorted. Callers here use nodes {nodes} with {replicas} replicas each. Read CONVENTIONS.md.")
    return mod, goal, _fill(RING_HIDDEN, **m), _fill(RING_SRC, **m), _fill(RING_TESTS, **m), _fill(RING_WRONG, **m)


# ================================================================== 7. raft-style leader election, in one process
ELECT_V = [("election", "Cluster", {"a": 5, "b": 7, "c": 9, "d": 11, "e": 13}),
           ("raftlite", "RaftCluster", {"n1": 8, "n2": 4, "n3": 6, "n4": 10, "n5": 12}),
           ("consensus", "Group", {"alpha": 6, "beta": 9, "gamma": 3, "delta": 12, "eps": 15}),
           ("leader", "Ensemble", {"x": 10, "y": 5, "z": 7, "w": 14, "v": 20}),
           ("quorum", "Quorum", {"s1": 4, "s2": 6, "s3": 8, "s4": 10, "s5": 5})]
ELECT_SRC = r'''"""Leader election in one process, raft style: terms, one vote per term, a strict majority to win, heartbeats, and step-down."""


class _Node:
    def __init__(self, name, timeout):
        self.name = name
        self.timeout = timeout
        self.state = "follower"
        self.term = 0
        self.voted_for = None
        self.timer = 0


class @CLS@:
    def __init__(self, timeouts):
        if not timeouts or min(timeouts.values()) < 3:
            raise ValueError("every election timeout must be at least 3 ticks")
        self.nodes = {name: _Node(name, t) for name, t in timeouts.items()}
        self.groups = None

    def _majority(self, votes):
        return votes * 2 > len(self.nodes)

    def _reach(self, a, b):
        if self.groups is None:
            return True
        return any(a in g and b in g for g in self.groups)

    def partition(self, *groups):
        self.groups = [set(g) for g in groups]

    def heal(self):
        self.groups = None

    def state(self, name):
        return self.nodes[name].state

    def term(self, name):
        return self.nodes[name].term

    def leaders(self):
        return sorted(n for n, node in self.nodes.items() if node.state == "leader")

    def _adopt(self, node, term):
        node.term = term
        node.state = "follower"
        node.voted_for = None

    def _peers(self, name):
        return [self.nodes[p] for p in sorted(self.nodes) if p != name and self._reach(name, p)]

    def _lead(self, leader):
        peers = self._peers(leader.name)
        if not self._majority(len(peers) + 1):
            leader.state = "follower"
            leader.timer = 0
            return
        for p in peers:
            if p.term > leader.term:
                self._adopt(leader, p.term)
                leader.timer = 0
                return
            if p.term < leader.term:
                self._adopt(p, leader.term)
            p.state = "follower"
            p.timer = 0

    def _elect(self, cand):
        cand.term += 1
        cand.state = "candidate"
        cand.voted_for = cand.name
        cand.timer = 0
        votes = 1
        for p in self._peers(cand.name):
            if p.term > cand.term:
                self._adopt(cand, p.term)
                return
            if p.term < cand.term:
                self._adopt(p, cand.term)
            if p.voted_for in (None, cand.name):
                p.voted_for = cand.name
                p.timer = 0
                votes += 1
        if self._majority(votes):
            cand.state = "leader"

    def tick(self):
        for name in sorted(self.nodes):
            node = self.nodes[name]
            if node.state == "leader":
                self._lead(node)
                continue
            node.timer += 1
            if node.timer >= node.timeout:
                self._elect(node)
'''
ELECT_TESTS = r'''from @MOD@ import @CLS@  # noqa: E402

TIMEOUTS = @TIMEOUTS@


class ElectionTest(unittest.TestCase):
    def test_one_leader(self):
        c = @CLS@(dict(TIMEOUTS))
        for _ in range(3 * max(TIMEOUTS.values())):
            c.tick()
        self.assertEqual(len(c.leaders()), 1)

    def test_short_timeout_refused(self):
        with self.assertRaises(ValueError):
            @CLS@({"a": 1, "b": 5})
'''
ELECT_HIDDEN = r'''
from @MOD@ import @CLS@
T = @TIMEOUTS@
names = sorted(T)
first = min(T, key=T.get)
c = @CLS@(dict(T))
for _ in range(3 * max(T.values())):
    c.tick()
assert c.leaders() == [first], ("the node that times out first must win", c.leaders())
assert all(c.term(n) == 1 for n in names), [c.term(n) for n in names]
assert all(c.state(n) == "follower" for n in names if n != first)
for _ in range(50):
    c.tick()
assert c.leaders() == [first] and c.term(first) == 1, "a healthy leader keeps its term"
rest = [n for n in names if n != first]
c.partition([first], rest)
for _ in range(4 * max(T.values())):
    c.tick()
ls = c.leaders()
assert len(ls) == 1 and ls[0] in rest and c.term(ls[0]) >= 2, ("the majority side must elect a new leader", ls)
assert c.state(first) != "leader", "a leader that cannot reach a majority steps down"
c.heal()
for _ in range(10 * max(T.values())):
    c.tick()
assert len(c.leaders()) == 1, ("after healing there is exactly one leader", c.leaders())
four = {n: T[n] for n in names[:4]}
q = @CLS@(four)
q.partition(names[:2], names[2:4])
for _ in range(10 * max(four.values())):
    q.tick()
assert q.leaders() == [], ("two of four is not a majority", q.leaders())
q.heal()
for _ in range(10 * max(four.values())):
    q.tick()
assert len(q.leaders()) == 1, q.leaders()
try:
    @CLS@({"a": 2, "b": 5})
    raise SystemExit("a timeout below 3 ticks was accepted")
except ValueError:
    pass
print("ok")
'''


def t_elect(v):
    mod, cls, timeouts = v
    m = dict(MOD=mod, CLS=cls, TIMEOUTS=timeouts)
    goal = (f"Build a Python module `{mod}.py`: a raft-style leader election simulated in one process, deterministic, with tests in "
            f"tests/. Public interface, exactly: `from {mod} import {cls}`; `{cls}(timeouts)` takes a dict node name -> election "
            f"timeout in ticks (any timeout below 3 raises ValueError). `tick()` advances time by one tick; `state(node)` is "
            f"'follower', 'candidate' or 'leader'; `term(node)` an int; `leaders()` the sorted names of nodes in state 'leader'; "
            f"`partition(*groups)` lets a node reach only nodes in its own group; `heal()` restores full reach. Rules: every node starts "
            f"as a follower in term 0 with no vote. tick() visits the nodes in sorted name order. A leader that can reach a strict "
            f"majority of ALL nodes (counting itself) sends a heartbeat to every node it reaches: a node with a higher term makes the "
            f"leader adopt that term and step down; any other node adopts the leader's term, becomes a follower and resets its timer "
            f"to 0. A leader that cannot reach a majority steps down to follower. Any other node adds 1 to its timer and, when it "
            f"reaches its timeout, starts an election: term + 1, votes for itself, timer 0, state candidate, then asks each node it "
            f"reaches in sorted order: a node with a higher term makes the candidate adopt it and become a follower (it stops asking); "
            f"a node with a lower term adopts the candidate's term, becomes a follower and forgets its vote; a node grants its vote "
            f"if it has not voted in this term (or voted for this candidate), and granting resets its timer. Votes from a strict "
            f"majority of all nodes make the candidate the leader. Callers here use timeouts {timeouts}. Read CONVENTIONS.md.")
    src = _fill(ELECT_SRC, **m)
    wrong = _mutate(src, "return votes * 2 > len(self.nodes)", "return votes * 2 >= len(self.nodes)")
    return mod, goal, _fill(ELECT_HIDDEN, **m), src, _fill(ELECT_TESTS, **m), wrong


# ================================================================== 8. circuit breaker
BREAKER_V = [("breaker", "CircuitBreaker", 3, 30.0), ("fuse", "Fuse", 2, 10.0), ("tripwire", "Tripwire", 4, 5.0),
             ("guardrail", "CallGuard", 3, 60.0), ("relay", "Relay", 5, 2.0)]
BREAKER_SRC = r'''"""A circuit breaker: after `failure_threshold` failures in a row, calls fail fast for `reset_timeout` seconds, then one trial call decides."""


class CircuitOpenError(RuntimeError):
    """Raised instead of calling the function while the circuit is open."""


class @CLS@:
    def __init__(self, failure_threshold, reset_timeout, clock):
        if failure_threshold < 1 or reset_timeout <= 0:
            raise ValueError("failure_threshold must be at least 1 and reset_timeout positive")
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.clock = clock
        self._failures = 0
        self._opened_at = None

    def state(self):
        if self._opened_at is None:
            return "closed"
        if self.clock() - self._opened_at >= self.reset_timeout:
            return "half_open"
        return "open"

    def call(self, fn, *args, **kwargs):
        st = self.state()
        if st == "open":
            raise CircuitOpenError("the circuit is open; not calling")
        try:
            result = fn(*args, **kwargs)
        except Exception:
            if st == "half_open":
                self._opened_at = self.clock()
            else:
                self._failures += 1
                if self._failures >= self.failure_threshold:
                    self._opened_at = self.clock()
            raise
        self._failures = 0
        self._opened_at = None
        return result
'''
BREAKER_TESTS = r'''from fakeclock import FakeClock  # noqa: E402
from @MOD@ import @CLS@, CircuitOpenError  # noqa: E402


def boom():
    raise IOError("down")


class BreakerTest(unittest.TestCase):
    def test_opens_after_threshold(self):
        b = @CLS@(@TH@, @RESET@, FakeClock())
        for _ in range(@TH@):
            with self.assertRaises(IOError):
                b.call(boom)
        self.assertEqual(b.state(), "open")
        with self.assertRaises(CircuitOpenError):
            b.call(lambda: 1)

    def test_trial_after_timeout_closes(self):
        clock = FakeClock()
        b = @CLS@(@TH@, @RESET@, clock)
        for _ in range(@TH@):
            with self.assertRaises(IOError):
                b.call(boom)
        clock.advance(@RESET@)
        self.assertEqual(b.call(lambda: "ok"), "ok")
        self.assertEqual(b.state(), "closed")
'''
BREAKER_HIDDEN = r'''
from @MOD@ import @CLS@, CircuitOpenError
@CLOCK@
N, RESET = @TH@, @RESET@
calls = []


def ok():
    calls.append("ok")
    return "fine"


def bad():
    calls.append("bad")
    raise IOError("down")


def fail_once(b):
    try:
        b.call(bad)
    except IOError:
        return
    raise SystemExit("the failure did not propagate")


c = Clock()
b = @CLS@(N, RESET, c)
assert b.state() == "closed"
for _ in range(N - 1):
    fail_once(b)
assert b.call(ok) == "fine" and b.state() == "closed"
for _ in range(N - 1):
    fail_once(b)
assert b.state() == "closed", "failures must be CONSECUTIVE to open the circuit; a success resets the count"
fail_once(b)
assert b.state() == "open"
n = len(calls)
try:
    b.call(ok)
    raise SystemExit("an open circuit let a call through")
except CircuitOpenError:
    pass
assert len(calls) == n, "an open circuit must not call the function"
c.t += RESET / 2
try:
    b.call(ok)
    raise SystemExit("it closed before reset_timeout")
except CircuitOpenError:
    pass
c.t += RESET / 2
assert b.state() == "half_open"
fail_once(b)
assert b.state() == "open", "a failed trial opens the circuit again"
c.t += RESET - 0.5
try:
    b.call(ok)
    raise SystemExit("the timer must restart after a failed trial")
except CircuitOpenError:
    pass
c.t += 0.5
assert b.call(ok) == "fine" and b.state() == "closed"
fail_once(b)
assert b.state() == "closed", "after closing, the failure count starts again from zero"
print("ok")
'''


def t_breaker(v):
    mod, cls, th, reset = v
    m = dict(MOD=mod, CLS=cls, TH=th, RESET=reset, CLOCK=CLOCK)
    goal = (f"Build a Python module `{mod}.py` with a circuit breaker, with tests in tests/. Public interface, exactly: "
            f"`from {mod} import {cls}, CircuitOpenError`; `{cls}(failure_threshold, reset_timeout, clock)`. `call(fn, *args, "
            f"**kwargs)` returns fn's result and re-raises fn's exceptions. Closed: a success resets the count of failures in a row; "
            f"after failure_threshold failures in a row the circuit opens. Open: until reset_timeout seconds have passed since it "
            f"opened, call raises CircuitOpenError without calling fn. After that `state()` reports 'half_open' and the next call is "
            f"a trial: success closes the circuit (count back to 0), failure opens it again and restarts the timer. `state()` "
            f"returns 'closed', 'open' or 'half_open'. failure_threshold below 1 or reset_timeout <= 0 raises ValueError. Callers "
            f"here use {th} failures and {reset} seconds. Read CONVENTIONS.md.")
    src = _fill(BREAKER_SRC, **m)
    wrong = _mutate(src, "        self._failures = 0\n        self._opened_at = None\n        return result",
                    "        self._opened_at = None\n        return result")
    return mod, goal, _fill(BREAKER_HIDDEN, **m), src, _fill(BREAKER_TESTS, **m), wrong


# ================================================================== 9. bulkhead
BULK_V = [("bulkhead", "Bulkhead", {"db": 2, "api": 3}), ("compartments", "Compartments", {"billing": 2, "search": 1}),
          ("pools", "CallPools", {"cpu": 2, "io": 3, "net": 1}), ("isolation", "Isolator", {"email": 4, "payments": 2}),
          ("partitions", "SlotGuard", {"read": 3, "write": 1})]
BULK_SRC = r'''"""Bulkheads: each compartment has its own number of slots, so one slow dependency cannot take every worker."""
import contextlib
import threading


class BulkheadFull(RuntimeError):
    """Raised at once when a compartment has no free slot: the caller sheds the load instead of queueing."""


class @CLS@:
    def __init__(self, limits):
        if not limits or any(int(v) < 1 for v in limits.values()):
            raise ValueError("every compartment needs at least one slot")
        self.limits = dict(limits)
        self._used = {name: 0 for name in limits}
        self._lock = threading.Lock()

    def in_use(self, name):
        with self._lock:
            return self._used[name]

    def _take(self, name):
        with self._lock:
            if self._used[name] >= self.limits[name]:
                raise BulkheadFull(f"{name}: all {self.limits[name]} slots are busy")
            self._used[name] += 1

    def _give(self, name):
        with self._lock:
            self._used[name] -= 1

    @contextlib.contextmanager
    def enter(self, name):
        if name not in self.limits:
            raise KeyError(name)
        self._take(name)
        try:
            yield self
        finally:
            self._give(name)
'''
BULK_TESTS = r'''from @MOD@ import @CLS@, BulkheadFull  # noqa: E402

LIMITS = @LIMITS@


class BulkheadTest(unittest.TestCase):
    def test_full_compartment_refuses(self):
        b = @CLS@(dict(LIMITS))
        name = sorted(LIMITS)[0]
        held = [b.enter(name) for _ in range(LIMITS[name])]
        for cm in held:
            cm.__enter__()
        with self.assertRaises(BulkheadFull):
            with b.enter(name):
                pass
        for cm in held:
            cm.__exit__(None, None, None)
        self.assertEqual(b.in_use(name), 0)
'''
BULK_HIDDEN = r'''
import contextlib
import threading
import time
from @MOD@ import @CLS@, BulkheadFull
LIM = @LIMITS@
b = @CLS@(dict(LIM))
names = sorted(LIM)
x, y = names[0], names[1]
with contextlib.ExitStack() as st:
    for _ in range(LIM[x]):
        st.enter_context(b.enter(x))
    assert b.in_use(x) == LIM[x]
    try:
        with b.enter(x):
            raise SystemExit("entered a full compartment")
    except BulkheadFull:
        pass
    with b.enter(y):
        assert b.in_use(y) == 1, "compartments are independent"
assert b.in_use(x) == 0 and b.in_use(y) == 0
for _ in range(LIM[x] + 2):
    try:
        with b.enter(x):
            raise ValueError("the work failed")
    except ValueError:
        pass
assert b.in_use(x) == 0, "a failing call must give its slot back"
try:
    with b.enter("nope"):
        pass
    raise SystemExit("an unknown compartment was accepted")
except KeyError:
    pass
inside, peak, lock = [0], [0], threading.Lock()
gate = threading.Event()
rejected = []


def worker():
    try:
        with b.enter(y):
            with lock:
                inside[0] += 1
                peak[0] = max(peak[0], inside[0])
            gate.wait(10)
            with lock:
                inside[0] -= 1
    except BulkheadFull:
        rejected.append(1)


ts = [threading.Thread(target=worker) for _ in range(LIM[y] + 3)]
for t in ts:
    t.start()
end = time.time() + 10
while (len(rejected) < 3 or inside[0] < LIM[y]) and time.time() < end:
    time.sleep(0.01)
assert len(rejected) == 3 and inside[0] == LIM[y], ("never more than the limit inside at once", len(rejected), inside[0])
gate.set()
for t in ts:
    t.join(10)
assert peak[0] == LIM[y] and b.in_use(y) == 0, (peak[0], b.in_use(y))
print("ok")
'''


def t_bulk(v):
    mod, cls, limits = v
    m = dict(MOD=mod, CLS=cls, LIMITS=limits)
    goal = (f"Build a Python module `{mod}.py` with bulkheads (separate concurrency limits per dependency), with tests in tests/. "
            f"Public interface, exactly: `from {mod} import {cls}, BulkheadFull`; `{cls}(limits)` takes a dict compartment name -> "
            f"number of slots (each at least 1, else ValueError). `enter(name)` is a context manager: entering takes a slot, or raises "
            f"BulkheadFull at once when all of that compartment's slots are taken (no waiting); leaving gives the slot back, also when "
            f"the body raised. An unknown compartment raises KeyError on entering. `in_use(name)` returns the slots taken. "
            f"Compartments are independent, and it must be safe to use from several threads. Callers here use {limits}. "
            f"Read CONVENTIONS.md.")
    src = _fill(BULK_SRC, **m)
    wrong = _mutate(src, "        try:\n            yield self\n        finally:\n            self._give(name)\n",
                    "        yield self\n        self._give(name)\n")
    return mod, goal, _fill(BULK_HIDDEN, **m), src, _fill(BULK_TESTS, **m), wrong


# ================================================================== 10. lock with TTL and fencing tokens
LOCK_V = [("locks", "LockService", 10.0), ("lease_lock", "LeaseLock", 5.0), ("mutex", "LockTable", 30.0),
          ("fencing", "FencedLocks", 2.0), ("coordinator", "LockManager", 60.0)]
LOCK_SRC = r'''"""Named locks with a time to live and fencing tokens: a holder that stalls past its TTL loses the lock, and its old token is refused."""


class @CLS@:
    def __init__(self, clock):
        self.clock = clock
        self._locks = {}
        self._last_token = 0

    def _live(self, name):
        lock = self._locks.get(name)
        if lock is not None and self.clock() >= lock["expires"]:
            del self._locks[name]
            lock = None
        return lock

    def acquire(self, name, owner, ttl):
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        lock = self._live(name)
        now = self.clock()
        if lock is None:
            self._last_token += 1
            self._locks[name] = {"owner": owner, "token": self._last_token, "expires": now + ttl}
            return self._last_token
        if lock["owner"] != owner:
            return None
        lock["expires"] = now + ttl
        return lock["token"]

    def holder(self, name):
        lock = self._live(name)
        return lock["owner"] if lock else None

    def release(self, name, owner, token):
        lock = self._live(name)
        if lock is None or lock["owner"] != owner or lock["token"] != token:
            return False
        del self._locks[name]
        return True
'''
LOCK_TESTS = r'''from fakeclock import FakeClock  # noqa: E402
from @MOD@ import @CLS@  # noqa: E402


class LockTest(unittest.TestCase):
    def test_exclusive_until_expiry(self):
        clock = FakeClock()
        s = @CLS@(clock)
        token = s.acquire("job", "w1", @TTL@)
        self.assertIsInstance(token, int)
        self.assertIsNone(s.acquire("job", "w2", @TTL@))
        clock.advance(@TTL@)
        self.assertGreater(s.acquire("job", "w2", @TTL@), token)

    def test_release_needs_the_token(self):
        s = @CLS@(FakeClock())
        token = s.acquire("job", "w1", @TTL@)
        self.assertFalse(s.release("job", "w1", token + 100))
        self.assertTrue(s.release("job", "w1", token))
'''
LOCK_HIDDEN = r'''
from @MOD@ import @CLS@
@CLOCK@
TTL = @TTL@
c = Clock()
s = @CLS@(c)
t1 = s.acquire("orders", "w1", TTL)
assert isinstance(t1, int) and s.holder("orders") == "w1"
assert s.acquire("orders", "w2", TTL) is None, "a held lock is not handed to someone else"
other = s.acquire("billing", "w2", TTL)
assert isinstance(other, int) and other > t1
c.t += TTL / 2
assert s.acquire("orders", "w1", TTL) == t1, "renewing keeps the token"
c.t += TTL / 2
assert s.holder("orders") == "w1", "the renewal must move the expiry"
c.t += TTL / 2
assert s.holder("orders") is None, "an expired lock has no holder"
t2 = s.acquire("orders", "w2", TTL)
assert isinstance(t2, int) and t2 > other > t1, "fencing tokens only go up"
assert s.release("orders", "w1", t1) is False, "the old holder cannot release a lock it lost"
assert s.release("orders", "w2", t1) is False, "a stale token is refused"
assert s.release("orders", "w2", t2) is True and s.holder("orders") is None
assert s.release("orders", "w2", t2) is False
assert s.acquire("orders", "w3", TTL) > t2
try:
    s.acquire("x", "w1", 0)
    raise SystemExit("ttl 0 was accepted")
except ValueError:
    pass
print("ok")
'''


def t_lock(v):
    mod, cls, ttl = v
    m = dict(MOD=mod, CLS=cls, TTL=ttl, CLOCK=CLOCK)
    goal = (f"Build a Python module `{mod}.py` with named locks that expire, plus fencing tokens, with tests in tests/. Public "
            f"interface, exactly: `from {mod} import {cls}`; `{cls}(clock)`. `acquire(name, owner, ttl)` returns a fencing token, an "
            f"int that is larger than every token this service handed out before, or None when another owner holds an unexpired "
            f"lock on that name. The same owner acquiring again renews the lock (the expiry moves to now + ttl) and gets the SAME "
            f"token. A lock has expired once clock() >= its expiry. `holder(name)` returns the owner or None. `release(name, owner, "
            f"token)` returns True only for the current owner with the current token before expiry, else False. ttl <= 0 raises "
            f"ValueError. Callers here use a TTL of {ttl} seconds. Read CONVENTIONS.md.")
    src = _fill(LOCK_SRC, **m)
    wrong = _mutate(src, '        lock["expires"] = now + ttl\n        return lock["token"]', '        return lock["token"]')
    return mod, goal, _fill(LOCK_HIDDEN, **m), src, _fill(LOCK_TESTS, **m), wrong


# ================================================================== 11. saga with compensation
SAGA_V = [("saga", "Saga", ["reserve_stock", "charge_card", "ship_order"]),
          ("booking", "TripBooking", ["book_flight", "book_hotel", "book_car", "send_receipt"]),
          ("transfer", "Transfer", ["debit_source", "credit_target", "record_ledger"]),
          ("onboarding", "Onboarding", ["create_account", "provision_storage", "grant_access", "welcome"]),
          ("checkout", "Checkout", ["hold_seat", "take_payment", "issue_ticket"])]
SAGA_SRC = r'''"""A saga: steps that each know how to undo themselves. When a step fails, the finished ones are undone, newest first."""


class SagaFailed(Exception):
    def __init__(self, step, compensated, errors):
        super().__init__(f"step {step!r} failed; undid {compensated}")
        self.step = step
        self.compensated = compensated
        self.errors = errors


class @CLS@:
    def __init__(self):
        self.steps = []

    def add_step(self, name, action, compensate):
        self.steps.append((name, action, compensate))
        return self

    def run(self, ctx):
        done, results = [], {}
        for name, action, compensate in self.steps:
            try:
                results[name] = action(ctx)
            except Exception as exc:
                compensated, errors = [], []
                for prev_name, prev_comp in reversed(done):
                    try:
                        prev_comp(ctx)
                        compensated.append(prev_name)
                    except Exception as undo_exc:
                        errors.append((prev_name, undo_exc))
                raise SagaFailed(name, compensated, errors) from exc
            done.append((name, compensate))
        return results
'''
SAGA_TESTS = r'''from @MOD@ import @CLS@, SagaFailed  # noqa: E402


class SagaTest(unittest.TestCase):
    def test_all_steps_run(self):
        s = @CLS@().add_step("a", lambda ctx: 1, lambda ctx: None).add_step("b", lambda ctx: 2, lambda ctx: None)
        self.assertEqual(s.run({}), {"a": 1, "b": 2})

    def test_failure_undoes_finished_steps(self):
        undone = []

        def fail(ctx):
            raise RuntimeError("no")

        s = @CLS@()
        s.add_step("a", lambda ctx: 1, lambda ctx: undone.append("a"))
        s.add_step("b", fail, lambda ctx: undone.append("b"))
        with self.assertRaises(SagaFailed):
            s.run({})
        self.assertEqual(undone, ["a"])
'''
SAGA_HIDDEN = r'''
from @MOD@ import @CLS@, SagaFailed
STEPS = @STEPS@


def build(fail_at=None, comp_fail=None):
    log = []
    s = @CLS@()
    for name in STEPS:
        def act(ctx, name=name):
            log.append("do " + name)
            if name == fail_at:
                raise RuntimeError(name + " failed")
            return name.upper()

        def undo(ctx, name=name):
            log.append("undo " + name)
            if name == comp_fail:
                raise RuntimeError("cannot undo " + name)
        s.add_step(name, act, undo)
    return s, log


s, log = build()
assert s.run({}) == {n: n.upper() for n in STEPS}
assert log == ["do " + n for n in STEPS]
k = len(STEPS) - 1
s, log = build(fail_at=STEPS[k])
try:
    s.run({})
    raise SystemExit("a failing step did not raise SagaFailed")
except SagaFailed as e:
    assert e.step == STEPS[k], e.step
    assert e.compensated == list(reversed(STEPS[:k])), ("newest first", e.compensated)
    assert isinstance(e.__cause__, RuntimeError), "the original error is the cause"
assert log == ["do " + n for n in STEPS] + ["undo " + n for n in reversed(STEPS[:k])], log
s, log = build(fail_at=STEPS[0])
try:
    s.run({})
    raise SystemExit("no SagaFailed")
except SagaFailed as e:
    assert e.compensated == [] and log == ["do " + STEPS[0]], ("the failed step itself is never compensated", log)
s, log = build(fail_at=STEPS[2], comp_fail=STEPS[1])
try:
    s.run({})
    raise SystemExit("no SagaFailed")
except SagaFailed as e:
    assert [n for n, _ in e.errors] == [STEPS[1]], e.errors
    assert "undo " + STEPS[0] in log, "one failed compensation must not stop the others"
    assert e.compensated == [STEPS[0]], e.compensated
print("ok")
'''


def t_saga(v):
    mod, cls, steps = v
    m = dict(MOD=mod, CLS=cls, STEPS=steps)
    goal = (f"Build a Python module `{mod}.py` with a saga (a sequence of steps that undo themselves on failure), with tests in "
            f"tests/. Public interface, exactly: `from {mod} import {cls}, SagaFailed`; `{cls}()`; `add_step(name, action, compensate)` "
            f"returns the saga so calls chain. `run(ctx)` calls action(ctx) for every step in order and returns {{name: result}}. When "
            f"a step raises, it calls compensate(ctx) for every step that had finished, newest first (never for the failed step), then "
            f"raises SagaFailed with `.step` (the failed step's name), `.compensated` (names whose compensation succeeded, in the "
            f"order run), `.errors` (a list of (name, exception) for compensations that raised; the remaining ones still run), and "
            f"the original exception as `__cause__`. The project's checkout uses the steps {steps}. Read CONVENTIONS.md.")
    src = _fill(SAGA_SRC, **m)
    wrong = _mutate(src, "for prev_name, prev_comp in reversed(done):", "for prev_name, prev_comp in done:")
    return mod, goal, _fill(SAGA_HIDDEN, **m), src, _fill(SAGA_TESTS, **m), wrong


# ================================================================== 12. vector clocks
VCLOCK_V = [("vclock", ["a", "b", "c"]), ("causality", ["web", "api", "db"]), ("versions", ["n1", "n2", "n3"]),
            ("happened", ["alice", "bob", "carol"]), ("vectortime", ["r1", "r2", "r3"])]
VCLOCK_SRC = r'''"""Vector clocks: which of two events could have caused the other, and which happened independently."""


def increment(clock, node):
    out = dict(clock)
    out[node] = out.get(node, 0) + 1
    return out


def merge(a, b):
    return {k: max(a.get(k, 0), b.get(k, 0)) for k in set(a) | set(b)}


def receive(local, remote, node):
    return increment(merge(local, remote), node)


def compare(a, b):
    keys = set(a) | set(b)
    less = any(a.get(k, 0) < b.get(k, 0) for k in keys)
    more = any(a.get(k, 0) > b.get(k, 0) for k in keys)
    if less and more:
        return "concurrent"
    if less:
        return "before"
    if more:
        return "after"
    return "equal"
'''
VCLOCK_TESTS = r'''from @MOD@ import compare, increment, merge  # noqa: E402


class VectorClockTest(unittest.TestCase):
    def test_order(self):
        a1 = increment({}, @N0@)
        a2 = increment(a1, @N0@)
        self.assertEqual(compare(a1, a2), "before")
        self.assertEqual(compare(a2, a1), "after")

    def test_concurrent_and_merge(self):
        a = increment({}, @N0@)
        b = increment({}, @N1@)
        self.assertEqual(compare(a, b), "concurrent")
        self.assertEqual(merge(a, b), {@N0@: 1, @N1@: 1})
'''
VCLOCK_HIDDEN = r'''
from @MOD@ import increment, merge, compare, receive
A, B, C = @NODES@
a0 = {}
a1 = increment(a0, A)
assert a0 == {} and a1 == {A: 1}, "increment must not change its input"
a2 = increment(a1, A)
b1 = increment({}, B)
assert compare(a1, a2) == "before" and compare(a2, a1) == "after"
assert compare(a2, dict(a2)) == "equal"
assert compare(a2, b1) == "concurrent" and compare(b1, a2) == "concurrent"
assert compare({A: 1}, {A: 1, B: 1}) == "before", "a missing entry counts as 0, on either side"
assert compare({A: 1, B: 1}, {A: 1}) == "after"
assert compare({A: 1, B: 0}, {A: 1}) == "equal", "an explicit 0 equals a missing entry"
m = merge(a2, b1)
assert m == {A: 2, B: 1} and a2 == {A: 2} and b1 == {B: 1}, "merge is the element-wise max and changes neither input"
r = receive(b1, a2, B)
assert r == {A: 2, B: 2}, r
assert compare(a2, r) == "before" and compare(r, a2) == "after"
assert compare(r, increment({}, C)) == "concurrent"
assert compare({}, {}) == "equal"
print("ok")
'''


def t_vclock(v):
    mod, nodes = v
    m = dict(MOD=mod, NODES=nodes, N0=repr(nodes[0]), N1=repr(nodes[1]))
    goal = (f"Build a Python module `{mod}.py` with vector clocks, with tests in tests/. A clock is a dict node name -> int, and a "
            f"missing entry means 0. Public interface, exactly: `from {mod} import increment, merge, compare, receive`. "
            f"`increment(clock, node)` returns a new clock with that node's count + 1; `merge(a, b)` returns the element-wise "
            f"maximum; `receive(local, remote, node)` is increment(merge(local, remote), node); `compare(a, b)` returns 'before' "
            f"(a happened before b), 'after', 'equal' or 'concurrent'. No function changes its inputs. The services here are "
            f"{nodes}. Read CONVENTIONS.md.")
    src = _fill(VCLOCK_SRC, **m)
    wrong = _mutate(src, "    keys = set(a) | set(b)\n    less", "    keys = set(a)\n    less")
    return mod, goal, _fill(VCLOCK_HIDDEN, **m), src, _fill(VCLOCK_TESTS, **m), wrong


# ================================================================== 13. backpressure
BACK_V = [("channel", "BoundedChannel", 10, 8, 3), ("buffer", "PressureBuffer", 6, 5, 2), ("mailroom", "Mailroom", 20, 15, 5),
          ("pipe", "Pipe", 4, 4, 1), ("stream_buffer", "StreamBuffer", 8, 6, 2)]
BACK_SRC = r'''"""A bounded channel with backpressure: producers are told to pause at high water and to resume only at low water."""
import collections


class @CLS@:
    def __init__(self, capacity, high_water, low_water):
        if not 0 < low_water < high_water <= capacity:
            raise ValueError("need 0 < low_water < high_water <= capacity")
        self.capacity = capacity
        self.high_water = high_water
        self.low_water = low_water
        self._items = collections.deque()
        self._paused = False
        self._stats = {"accepted": 0, "rejected": 0, "pauses": 0}

    def __len__(self):
        return len(self._items)

    def _update(self):
        n = len(self._items)
        if not self._paused and n >= self.high_water:
            self._paused = True
            self._stats["pauses"] += 1
        elif self._paused and n <= self.low_water:
            self._paused = False

    def offer(self, item):
        if len(self._items) >= self.capacity:
            self._stats["rejected"] += 1
            return False
        self._items.append(item)
        self._stats["accepted"] += 1
        self._update()
        return True

    def poll(self):
        if not self._items:
            return None
        item = self._items.popleft()
        self._update()
        return item

    def paused(self):
        return self._paused

    def stats(self):
        return dict(self._stats)
'''
BACK_TESTS = r'''from @MOD@ import @CLS@  # noqa: E402


class BackpressureTest(unittest.TestCase):
    def test_fifo_and_full(self):
        ch = @CLS@(@CAP@, @HI@, @LO@)
        for i in range(@CAP@):
            self.assertTrue(ch.offer(i))
        self.assertFalse(ch.offer("extra"))
        self.assertEqual(ch.poll(), 0)

    def test_pauses_at_high_water(self):
        ch = @CLS@(@CAP@, @HI@, @LO@)
        for i in range(@HI@):
            ch.offer(i)
        self.assertTrue(ch.paused())
'''
BACK_HIDDEN = r'''
from @MOD@ import @CLS@
CAP, HI, LO = @CAP@, @HI@, @LO@
f = @CLS@(CAP, HI, LO)
for i in (1, 2, 3):
    f.offer(i)
assert [f.poll(), f.poll(), f.poll(), f.poll()] == [1, 2, 3, None], "first in, first out; None when empty"
ch = @CLS@(CAP, HI, LO)
for i in range(HI - 1):
    assert ch.offer(i) is True
assert ch.paused() is False
assert ch.offer("x") is True and ch.paused() is True, "reaching high water pauses the producer"
while len(ch) > LO + 1:
    ch.poll()
assert ch.paused() is True, "it stays paused until it drains to low water (no flapping)"
ch.poll()
assert len(ch) == LO and ch.paused() is False
while ch.offer("fill"):
    pass
assert len(ch) == CAP and ch.offer("more") is False
st = ch.stats()
assert st["rejected"] == 2 and st["pauses"] == 2 and st["accepted"] == HI + (CAP - LO), st
for bad in ((CAP, CAP + 1, LO), (CAP, HI, HI), (CAP, HI, 0)):
    try:
        @CLS@(*bad)
        raise SystemExit("bad water marks were accepted: %r" % (bad,))
    except ValueError:
        pass
print("ok")
'''


def t_back(v):
    mod, cls, cap, hi, lo = v
    assert lo + 1 < hi <= cap, "the hysteresis must be observable"
    m = dict(MOD=mod, CLS=cls, CAP=cap, HI=hi, LO=lo)
    goal = (f"Build a Python module `{mod}.py`: a bounded channel with backpressure, with tests in tests/. Public interface, exactly: "
            f"`from {mod} import {cls}`; `{cls}(capacity, high_water, low_water)` (needs 0 < low_water < high_water <= capacity, "
            f"else ValueError). `offer(item)` stores the item and returns True, or returns False when the channel holds capacity items "
            f"(counted as rejected). `poll()` returns the oldest item, or None when empty. `paused()` turns True once the size reaches "
            f"high_water and stays True until the size drops to low_water or below (no flapping between the two). `len(channel)` is "
            f"the number of items. `stats()` returns {{'accepted': n, 'rejected': n, 'pauses': n}} where pauses counts the times it "
            f"became paused. Callers here use capacity {cap}, high water {hi}, low water {lo}. Read CONVENTIONS.md.")
    src = _fill(BACK_SRC, **m)
    wrong = _mutate(src, "        elif self._paused and n <= self.low_water:", "        elif self._paused and n < self.high_water:")
    return mod, goal, _fill(BACK_HIDDEN, **m), src, _fill(BACK_TESTS, **m), wrong


# ------------------------------------------------------------------ the family
# (key, level, variants, builder). Level is a curriculum hint: the tag `level-<x>` lets `eval --tags level-easy` pick a night's slice.
TEMPLATES = [
    ("ratelimit", "easy", BUCKET_V, t_bucket),
    ("window", "easy", WINDOW_V, t_window),
    ("retry", "easy", RETRY_V, t_retry),
    ("idempotent", "easy", IDEM_V, t_idem),
    ("vclock", "easy", VCLOCK_V, t_vclock),
    ("breaker", "medium", BREAKER_V, t_breaker),
    ("bulkhead", "medium", BULK_V, t_bulk),
    ("lock", "medium", LOCK_V, t_lock),
    ("saga", "medium", SAGA_V, t_saga),
    ("backpressure", "medium", BACK_V, t_back),
    ("ring", "medium", RING_V, t_ring),
    ("lease", "hard", LEASE_V, t_lease),
    ("election", "hard", ELECT_V, t_elect),
]


HOLDOUT_TEMPLATES = ("vclock", "ring", "election")   # held out WHOLE: no variant of these is ever trained on; `eval --tags holdout` measures them
HOLDOUT = 4         # legacy name (the old per-variant index); no longer used to hold anything out


def _task(key, level, k, built):
    mod, goal, hidden, src, tests, wrong = built
    tpath = f"tests/test_{mod}.py"
    test_src = TEST_HEAD + tests + TEST_TAIL
    return {
        "id": f"{PREFIX}-{key}-{k:02d}", "template": key,
        "tags": [TAG, "training", "python", "from-nothing", key, "level-" + level] + (["holdout"] if key in HOLDOUT_TEMPLATES else []),
        "goal": goal, "params": {"domains": ["api"]},
        "fixture": {"files": {"fakeclock.py": FAKECLOCK, "CONVENTIONS.md": CONVENTIONS}},
        "budget": {"calls": 24, "tokens": 90000, "seconds": 1200},
        "judge": [{"type": "file_exists", "paths": [f"{mod}.py", "tests/test_*.py"]},
                  {"type": "files_unchanged", "paths": ["fakeclock.py"]},
                  {"type": "python_assert", "code": hidden, "timeout": 90},
                  {"type": "tests_pass"}],
        "solution": {"files": {f"{mod}.py": src, tpath: test_src}},
        "wrong": {"files": {f"{mod}.py": wrong, tpath: test_src}},
    }


def all_tasks():
    """Every task of the family, template by template (the order `--check` reports in)."""
    return [_task(key, level, k, build(v)) for key, level, variants, build in TEMPLATES for k, v in enumerate(variants)]


def generate(n, seed=0):
    """n tasks, round-robin over the templates in a seed-stable order, so a short night still covers every pattern once."""
    by_key = {}
    for t in all_tasks():
        by_key.setdefault(t["template"], []).append(t)
    keys = sorted(by_key)
    random.Random(f"{seed}:{TAG}").shuffle(keys)
    out, k = [], 0
    while len(out) < sum(len(v) for v in by_key.values()):
        out += [by_key[key][k] for key in keys if k < len(by_key[key])]
        k += 1
    return out[:max(0, n)]


# ================================================================== checks and the overnight run (shared with harness.py)
def _here():
    return os.path.dirname(os.path.abspath(__file__))


def _repo():
    return os.path.dirname(os.path.dirname(_here()))


def _family(name):
    import importlib.util
    if _here() not in sys.path:
        sys.path.insert(0, _here())
    spec = importlib.util.spec_from_file_location("night_" + name, os.path.join(_here(), name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _setup_paths(isolate):
    if isolate:                                  # the checks own their data folder, like every suite (CLAUDE.md)
        import tempfile
        os.environ["ARCHITECT_DATA_DIR"] = tempfile.mkdtemp(prefix="famcheck_")
        os.environ["ARCHITECT_NO_CACHE"] = "1"
    if _repo() not in sys.path:
        sys.path.insert(0, _repo())


def _judge_all(E, task, files, skip=()):
    """Write the fixture, then `files` over it, run the task's judges the way run_task does. -> list of (type, ok, reason, could_run)."""
    import shutil
    import tempfile
    proj = tempfile.mkdtemp(prefix="famjudge_")
    try:
        E._write(proj, task["fixture"].get("files"))
        ctx = {"before": {}, "lines_before": {}, "route": None, "json": None}
        for j in task["judge"]:
            for rel in (j.get("paths") or []) if j.get("type") == "files_unchanged" else []:
                ctx["before"][rel] = E._sha(E._read(proj, rel))
        E._write(proj, files)
        out = []
        for j in task["judge"]:
            if j["type"] in skip:
                continue
            ok, reason, could = E.JUDGES[j["type"]](proj, j, task, ctx)
            out.append((j["type"], ok, reason, could))
        unit = E._py(proj, ["-m", "unittest", "discover", "-s", "tests", "-q"], timeout=120)
        return out, unit
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def check(family="distributed", quick=False):
    """-> exit code. Every task: valid, the reference passes every judge (and its tests also run under plain unittest), the
    reference is clean under the fake-code gate, and the deliberately wrong answer is caught by a judge."""
    _setup_paths(isolate=True)
    import arch_eval as E
    import arch_fakecode as F
    fam = _family(family)
    results = []

    def ok(name, cond, extra=""):
        results.append(bool(cond))
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   ({str(extra)[:400]})" if extra and not cond else ""))

    tasks = [E.normalize(dict(t, flow=getattr(fam, "FLOW", "build"))) for t in fam.all_tasks()]
    keys = sorted({t["template"] for t in tasks})
    ok(f"{family}: {len(tasks)} tasks from {len(keys)} templates (at least {fam.MIN_TASKS} from {fam.MIN_TEMPLATES})",
       len(tasks) >= fam.MIN_TASKS and len(keys) >= fam.MIN_TEMPLATES, (len(tasks), keys))
    ids = [t["id"] for t in tasks]
    ok("every id is unique and sorts after the e2e family (eval --quick and the representative set are unchanged)",
       len(ids) == len(set(ids)) and all(i > "e2e-~" for i in ids), [i for i in ids if ids.count(i) > 1][:3])
    probs = [p for t in tasks for p in E.validate(t)]
    ok("every task validates (known flow, a goal, known judges)", not probs, probs[:3])
    g1, g2 = [t["id"] for t in fam.generate(len(tasks), 3)], [t["id"] for t in fam.generate(len(tasks), 3)]
    ok("generate() is seed-stable, covers every task, and a short run touches every template first",
       g1 == g2 and set(g1) == set(ids) and {t["template"] for t in fam.generate(len(keys), 0)} == set(keys) and len(fam.generate(1, 0)) == 1)
    ok("the loader picks the family up (FLOW, DEFAULT_N, generate)", fam.FLOW == "build" and fam.DEFAULT_N >= 1 and callable(fam.generate))
    held = [t for t in tasks if "holdout" in t["tags"]]
    ok("held-out templates are held out WHOLE: every variant of a held-out template is tagged holdout, no variant of any other is",
       {t["template"] for t in held} == set(fam.HOLDOUT_TEMPLATES) and len(held) == sum(1 for t in tasks if t["template"] in fam.HOLDOUT_TEMPLATES)
       and set(fam.HOLDOUT_TEMPLATES) <= set(keys), [t["id"] for t in held][:4])
    for t in (tasks if not quick else [next(x for x in tasks if x["template"] == k) for k in keys]):
        fake = [f"{rel}:{x['line']} {x['rule']}" for rel, text in t["solution"]["files"].items() for x in F.scan_text(rel, text) if x.get("gate")]
        right, unit = _judge_all(E, t, t["solution"]["files"])
        bad_right = [(ty, why) for ty, good, why, could in right if not good or not could]
        wrong, _ = _judge_all(E, t, t["wrong"]["files"], skip=("file_exists",))
        caught = [ty for ty, good, why, could in wrong if not good and could]
        differs = t["wrong"]["files"] != t["solution"]["files"]
        ok(f"{t['id']}: the reference passes every judge, its tests run under unittest too, no fake-code finding; the wrong answer is caught",
           not bad_right and unit[0] and not fake and caught and differs,
           {"reference": bad_right, "unittest": None if unit[0] else unit[1][-200:], "fakecode": fake, "wrong caught by": caught})
    n, failed = len(results), results.count(False)
    if failed:
        print(f"\n{failed} of {n} {family.upper()} CHECKS FAILED")
        return 1
    print(f"\nALL {n} {family.upper()} CHECKS PASSED")
    return 0


def night(families=("distributed", "harness"), hours=8.0, n=None, out=None, fake=False, min_quality=0.0, seed=0, say=print):
    """The overnight run: each task through the real flow with YOUR local models, one at a time; a task is kept only when every
    hidden judge passed; then the training export reads exactly those runs. -> summary dict.

    Benchmarks rules apply (ARCHITECT_EVAL=1): no reply cache, no cloud rescue, nothing learned into the ledger from these runs.
    Tasks tagged `holdout` are never run here, so `eval --tags holdout` can measure an adapter on work it did not train on.
    <data dir>/STOP stops it between tasks (the engine also stops between calls); --hours stops it starting new ones."""
    import json
    import shutil
    import time
    _setup_paths(isolate=False)
    import arch_core as core
    import arch_dataset as ds
    import arch_eval as E
    import arch_heavy
    lists = []
    for name in families:
        fam = _family(name)
        lists.append([E.normalize(t) for t in fam.generate(len(fam.all_tasks()), seed) if "holdout" not in t["tags"]][:n])
    tasks, i = [], 0                             # interleave the families, so a night cut short still has some of each
    while any(i < len(x) for x in lists):
        tasks += [x[i] for x in lists if i < len(x)]
        i += 1
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = os.path.abspath(out or os.path.join(core.data_dir(), "training_night", stamp + ("_fake" if fake else "")))
    runs_dir = os.path.join(out, "runs")
    os.makedirs(runs_dir, exist_ok=True)
    over = {"behavior": {"learn_from_runs": "off"}}
    srv = prev = None
    if fake:
        import arch_roles
        srv, prev = E.start_oracle(tasks)
        over = core.deep_merge(over, {"roles": {r: "fake:7b" for r in arch_roles.ROLES}, "default_model": "fake:7b"})
        say("[night] the oracle fake answers: this proves the plumbing, and its samples are NOT training data (fake:7b replays references)")
    else:
        try:
            import arch_hosts as H
            import arch_server
            served = [p for p in arch_server.status().get("processes", []) if p.get("up")]
            pr = H.probe(core.ollama_host()) if not served else {"ok": True}
        except Exception as ex:
            pr = {"ok": False, "error": str(ex)}
        if not pr.get("ok"):
            say(f"[night] no model server answers (llama-server or Ollama at {core.ollama_host()}). Start one: python architect.py start")
            return {"ok": False, "error": "no model server"}
    deadline = time.time() + float(hours) * 3600
    stops = [os.path.join(core.data_dir(), "STOP")]
    log_path = os.path.join(out, "night.jsonl")
    kept, counts = [], {"pass": 0, "fail": 0, "not_sure": 0}
    prev_eval = os.environ.get("ARCHITECT_EVAL")
    os.environ["ARCHITECT_EVAL"] = "1"
    say(f"[night] {len(tasks)} task(s), at most {hours} h, one at a time; kept runs go to {runs_dir}")
    try:
        with arch_heavy.one_at_a_time("training night", core.load_config(), say=say):
            for i, t in enumerate(tasks, 1):
                if time.time() > deadline:
                    say(f"[night] the {hours} h window is over; {len(tasks) - i + 1} task(s) left for another night")
                    break
                if any(os.path.exists(p) for p in stops):
                    say("[night] STOP file found; stopping between tasks")
                    break
                r = E.run_task(t, over, keep=True)
                proj = r.pop("proj", None)
                counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
                if r["verdict"] == "pass" and proj:
                    dest = os.path.join(runs_dir, t["id"])
                    shutil.rmtree(dest, ignore_errors=True)
                    shutil.move(proj, dest)
                    kept.append(dest)
                elif proj:
                    shutil.rmtree(proj, ignore_errors=True)
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({k: r.get(k) for k in ("id", "verdict", "secs", "calls", "tokens_in", "tokens_out", "detail", "error", "models")}) + "\n")
                say(f"  {r['verdict'].upper():8} {t['id']:34} {r['secs']:>7}s  {r['calls']} calls  {r['tokens_in'] + r['tokens_out']} tokens")
    finally:
        if prev_eval is None:
            os.environ.pop("ARCHITECT_EVAL", None)
        else:
            os.environ["ARCHITECT_EVAL"] = prev_eval
        if srv:
            E.stop_oracle(srv, prev)
    data = ds.build(kept, min_quality=min_quality) if kept else None
    summary = ds.write(data, os.path.join(out, "datasets")) if data else {"stats": {}}
    st = summary.get("stats") or {}
    say(f"\n[night] passed {counts.get('pass', 0)}, failed {counts.get('fail', 0)}, not sure {counts.get('not_sure', 0)}. "
        f"Exported from the {len(kept)} passed run(s): {st.get('sft', 0)} SFT samples, {st.get('dpo', 0)} preference pairs, "
        f"{st.get('distill', 0)} distillation samples, {st.get('traj', 0)} tool conversations -> {os.path.join(out, 'datasets')}")
    if not fake and kept:
        say(f"Next: python train_lora.py --dry-run --data \"{os.path.join(out, 'datasets', 'tier-worker.sft.jsonl')}\" --min-quality 0.6 --curriculum")
    return {"ok": True, "out": out, "kept": len(kept), "counts": counts, "stats": st}


def main(argv=None, family="distributed"):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="prove every task: the reference passes, the wrong answer is caught")
    ap.add_argument("--quick", action="store_true", help="with --check: one task per template")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--night", action="store_true", help="the overnight training run with your local models")
    ap.add_argument("--families", default="distributed,harness")
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--n", type=int, help="tasks per family (default: all of them)")
    ap.add_argument("--out", help="where the kept runs and the datasets go (default: <data dir>/training_night/<time>)")
    ap.add_argument("--fake", action="store_true", help="the oracle answers: proves the plumbing, produces no real training data")
    ap.add_argument("--min-quality", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args(argv)
    if a.check:
        return check(family, quick=a.quick)
    if a.night:
        res = night([f.strip() for f in a.families.split(",") if f.strip()], a.hours, a.n, a.out, a.fake, a.min_quality, a.seed)
        return 0 if res.get("ok") else 1
    for t in _family(family).all_tasks():
        print(f"  {t['id']:36} {t['goal'][:90]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(family="distributed"))
