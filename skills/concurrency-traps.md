---
name: concurrency-traps
description: Avoid race conditions, deadlocks and blocked event loops in threaded and async code
when: concurrency, concurrent, race condition, thread, threading, asyncio, async, await, deadlock, lock, multiprocessing, parallel, thread safe
---
# Concurrency traps

Concurrency bugs pass the tests most of the time. Prevent them by design, not by luck.

- **Shared mutable state needs a lock**, or better, no sharing. Pass data through a `queue.Queue` instead of a shared list.
  `x += 1` on a shared counter is not atomic.
- **Check-then-act is a race.** `if not exists(p): create(p)` can run twice. Use an atomic operation: open with `"x"` mode, a
  database unique constraint, or do both steps under one lock.
- **Deadlocks:** always take multiple locks in the same order, hold a lock for as short a time as possible, and never call
  unknown code (callbacks, I/O) while holding one. Use `with lock:` so it is always released.
- **Never block the event loop.** In `async def`, do not call `time.sleep`, `requests`, or slow file I/O. Use `await asyncio.sleep`,
  an async client, or `await asyncio.to_thread(fn)`.
- **Await what you start.** A coroutine called without `await` never runs. Keep a reference to tasks from `create_task` and
  gather them so their exceptions are seen.
- **Threads do not speed up CPU work in Python** because of the GIL; use processes for that, threads for waiting on I/O.
- **Set timeouts** on every wait, join and network call so a stuck worker cannot hang everything.

Test with many iterations and many workers; a race that shows once in 1000 runs is still a bug.
