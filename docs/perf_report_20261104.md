# Performance review: hub, context path, retrieval, Studio (2026-11-04)

Scope: `architect_hub.py`, `model_hub.py` (the hub's base), `hub_ui.html`, `arch_core.py` (`list_files`, `read_text`, `rank_files`,
`focus`, `Engine.context`), `arch_index.py`, `arch_fts.py`, `arch_chat.py`, plus `arch_hw.py`/`arch_server.py` where the hub calls them.

**Evidence tier.** Every "where" below was read in the code (file and function named). **No timing was measured for this
report**: the brief did not allow running commands. Every saving is an **estimate**, labelled as one, with the way to measure it in
the last section. Typical Windows numbers used for the estimates (community, not measured here): one `tasklist` launch is about
100-250 ms of CPU, one `nvidia-smi` launch about 150-400 ms. Note: `architect_hub.py` and `hub_ui.html` were being edited by other
agents while this was written, so line numbers can drift. Function names are the stable reference.

## Ranking (saving per line changed)

| # | Where | Cost today | Fix size | Expected saving (estimate) |
|---|-------|-----------|----------|---------------------------|
| 1 | `hub_ui.html` `refresh()` change key | 8 pages rebuild their whole DOM every 9 s, because `ram_free_gb` and `vram_free_gb` change on every poll | 1 line | One full page render every 9 s on Home, Models, Roles, Flows, Portfolio, Scoreboard, Settings and Training. Also fixes scroll jumps and `<details>` closing on their own |
| 2 | `arch_server._alive` (Windows) | starts a `tasklist` process for **each tier on each check** (`status()`, the recorder's `servers_up`, `serving()`, `ensure`, watchdog) | ~12 lines | With 3 tiers and the console open: about 6 process launches per 5 s gone, roughly 0.6-1.5 s of CPU per 5 s |
| 3 | `/api/arch/gpu` + `arch_hw.record_once` | the hub's own recorder already samples `live()` every 5 s; the endpoint samples again (`nvidia-smi` + `tasklist vmmem*`) | ~6 lines | 2 process launches per 5 s |
| 4 | `model_hub.detect_hardware` (inside `/api/state`) | `nvidia-smi` every 9 s, on every state refresh | ~7 lines | 1 process launch per 9 s, and 150-400 ms off each `/api/state` |
| 5 | `arch_index.Retriever.ensure` on a new `Engine` | every Studio message (and every run, every eval task) builds a new Engine, so `dirty=True`, so `chunks_for` re-reads, `ast.parse`s and sha1s **every file** | ~15 lines | When the Studio root is Architect itself (136 .py files, 3.0 MB), about 1-3 s of CPU per chat message, before the model sees anything |
| 6 | `arch_core.rank_files` | re-tokenises every project file (two regexes per word) on **every** `context()` call; `read_text` caches bytes, not terms | ~14 lines | Most of the remaining `context()` CPU: about 1-3 s per call on a 3 MB tree, a few ms on a small project. `context()` is called 20+ times per flow |
| 7 | `hub_ui.html` `jobBadge`, `pollConsole` | keep polling in a hidden tab (`pollGpu` and `refresh` already check `document.hidden`) | 2 lines | Every request from a background tab: 1 per 3 s plus 2 per 0.7-2.6 s |
| 8 | `hub_ui.html` `pollConsole` renders | `renderDoing`, `renderStrip` and `renderRoute` (SVG) rebuild every 700 ms during a run and every 2.6 s when idle, even when no event arrived | 3 lines | Most of the console's idle DOM work |
| 9 | `hub_ui.html` `refresh()` overlap | no in-flight guard: when `/api/state` takes longer than 9 s (Ollama busy loading, `/api/show` for a new model with a 15 s timeout), requests pile up | 2 lines | No stacked state builds |
| 10 | `arch_chat._reply` | walks the project tree 4-5 times per message (`list_files` at 199, inside `context`, at 201, inside `hint_items`, inside `Retriever.search`) | 3 lines | 2-3 tree walks per message |
| 11 | `arch_core._READ_CACHE` | capped by count (4000 files), not bytes; the hub process lives for days and visits many project roots | ~8 lines | Memory: limited to a fixed size (64 MB) instead of up to 4000 x 400 KB |
| 12 | `model_hub.Job` event ring | `token` events (about 3 a second, `Engine._call`'s `tok`) fill the 4000-event ring in about 20 min of generation and push out the step, check and verdict events a reconnecting console needs | 4 lines | Memory, and a reconnect that still shows the plan |
| 13 | `arch_index.hybrid`, `Engine.context` | `f not in fused` / `f not in prev` test membership against a list inside a loop: O(n^2) over the files | 2 lines | Small: a few ms at 500 files |
| — | Not worth changing | see "Checked, not a cost" | — | — |

---

## 1. The state change detector never says "same"

**Evidence.** `refresh()`:
```js
const nxt=await api("/api/state?"+q); const key=JSON.stringify(nxt,(k,v)=>k==="time"?undefined:v);
const same=S&&key===refresh.last; ...
side(); if(!same||force)render();
```
`/api/state` includes `hw.ram_free_gb` and `hw.vram_free_gb` (`model_hub.detect_hardware`) and `loaded` from Ollama `/api/ps`, which has
`expires_at` and `size_vram`. Free RAM is different on practically every read, so `same` is false on every 9 s tick. `render()` then
hits its last line for the pages without a `built` guard:
```js
v.innerHTML=""; const fn={overview,start,models,roles,flows,portfolio,score:scoreboard,settings,train}[page]; put(v,...fn());
```
So the page is rebuilt every 9 s. Each rebuild costs DOM work, collapses the scroll height (the page jumps), and closes any open `<details>`.

**Fix** (one line; `side()` still runs on every tick, so the sidebar's live numbers stay current):
```js
const VOLATILE=new Set(["time","ram_free_gb","vram_free_gb","expires_at","size_vram"]);
const key=JSON.stringify(nxt,(k,v)=>VOLATILE.has(k)?undefined:v);
```
**Check.** hub-check / Playwright: open Home, wait 20 s, and assert that one marked element (`$("#view").firstChild`) is the same
node.

## 2. `_alive` on Windows launches `tasklist` per pid

**Evidence.** `arch_server._alive`:
```python
r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=5)
```
It is called for every tier record by `status()` (which `/api/arch/gpu` runs every 5 s), by the recorder's `servers_up` (every 5 s,
`architect_hub` near `start_recorder`), by `serving()` (on every model call that checks for a resident server), and by `ensure` and the
watchdog. With 3 tiers that is about 6 `tasklist` launches every 5 s from the hub alone.

**Fix** (no new process; same answer, including "pid reused" behaving as before):
```python
def _alive(pid):
    if not pid:
        return False
    if sys.platform.startswith("win"):
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x1000, False, int(pid))          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5               # access denied: it exists, it is just not ours
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
```
**Check** (`arch_selftest_server` or `_win`): `_alive(os.getpid())` is True; a child started and waited on is False afterwards;
`_alive(0)` is False.

## 3. `/api/arch/gpu` re-samples what the recorder just sampled

**Evidence.** While the hub runs, `arch_hw.start_recorder` calls `record_once` -> `live()` every 5 s (`nvidia-smi` plus
`tasklist /FI "IMAGENAME eq vmmem*"`). The console's `pollGpu` asks `/api/arch/gpu` every 5 s, and that handler calls `arch_hw.live()`
again.

**Fix.** In `arch_hw.py`:
```python
LAST = {}                                   # the recorder's latest sample, for the hub to reuse

def record_once(path, run=None, max_bytes=2_000_000, extra=None):
    ...
    row = dict(t=round(time.time(), 1), **live(run or _run), **_power_now(), **(extra or {}))
    LAST.clear(); LAST.update(row)
```
In the `/api/arch/gpu` handler, replace `arch_hw.live()`:
```python
s = dict(arch_hw.LAST) if time.time() - arch_hw.LAST.get("t", 0) < 7 else arch_hw.live()
```
When `behavior.record_live` is off, `LAST` goes stale and the handler samples on its own, so the result is the same either way.

## 4. `/api/state` runs `nvidia-smi` every 9 s

**Evidence.** `model_hub.build_state` -> `detect_hardware()` -> `subprocess.run(["nvidia-smi", ...], timeout=8)` on every call, and the
UI calls it every 9 s (`setInterval(...refresh..., 9000)`). The GPU name, total memory and driver do not change while the hub runs.
Free memory is shown live by #3 anyway.

**Fix** (in `detect_hardware`, around the nvidia-smi block):
```python
_SMI = {"t": 0, "out": None}
...
    if time.time() - _SMI["t"] > 30:
        _SMI.update(t=time.time(), out=subprocess.run([...same args...], capture_output=True, text=True, timeout=8))
    out = _SMI["out"]
```
Keep the existing `try/except` around it. `vram_free_gb` can then be up to 30 s old. That is fine for fit math, which is labelled
an estimate.

## 5. Every new Engine re-chunks the whole project

**Evidence.** `Retriever.__init__` sets `self.dirty = True`. The first `rank()` or `search()` then calls `ensure()` ->
`chunks_for(...)`, which, for every file, runs `read_text`, then `ast.parse` (Python) or a line-window split, then a `sha1` per chunk.
Only after all that does `Index.refresh` find that nothing is new. `arch_chat._reply` builds a new `Engine` for **every message**
(`core.Engine(root, "chat", ...)`). So does every run, every eval and livetest task, and every Studio/proposal engine. The Studio's
default root is `HERE` (Architect itself: 136 Python files, 3.0 MB).

**Fix** (`arch_index.py`). Remember, per process, the file signature the index was last brought in line with. A new Engine whose
files did not change skips the work:
```python
_FRESH = {}                                                  # index path -> signature of the files it was last synced with (this process)

def _signature(root, files):
    rels = list(files)
    mdir = os.path.join(root, ".arch", "memory")
    if os.path.isdir(mdir):
        rels += [".arch/memory/" + f for f in sorted(os.listdir(mdir)) if f.endswith(".md")]
    sig = []
    for rel in rels:
        try:
            st = os.stat(os.path.join(root, rel))
            sig.append((rel, st.st_mtime_ns, st.st_size))
        except OSError:
            pass
    return hash(tuple(sig))
```
In `Retriever.ensure`, at the top, after the `if not self.index or not self.dirty: return` line:
```python
        sig = _signature(self.root, files)
        if _FRESH.get(self.index.path) == sig and os.path.isfile(self.index.path):
            self.dirty = False
            return
```
At the end, on success (no `st.get("error")`): `_FRESH[self.index.path] = sig`. In `touch()` add
`_FRESH.pop(self.index.path, None) if self.index else None`, so an edit this run applied always re-syncs, even within the mtime
resolution.
**Check** (`arch_selftest_index`): two Retrievers on one root; the second `ensure()` makes no `chunks_for` call (monkeypatch a
counter); after a file write plus `touch()`, it does.

## 6. `rank_files` re-tokenises every file on every call

**Evidence.** `rank_files` builds `collections.Counter(_terms(f.replace("/", " ") * 3 + " " + t))` for every file, every time.
`_terms` runs `_TOK.findall` and then a second regex per word. `read_text` caches the bytes (`_READ_CACHE`), but the term counts are
rebuilt. `Engine.context` calls it on every invocation (20 call sites in `arch_flows.py`, 17 in `arch_flows_pro.py`), and Studio
calls it on every message.

**Fix** (`arch_core.py`, next to `_READ_CACHE`):
```python
_TERMS_CACHE = {}

def _file_terms(root, f):
    p = os.path.join(root, f)
    st = os.stat(p)
    key = (os.path.abspath(p), f)
    hit = _TERMS_CACHE.get(key)
    if hit is not None and hit[0] == (st.st_mtime_ns, st.st_size):
        return hit[1]
    t, _ = read_text(p, 200000)
    c = collections.Counter(_terms(f.replace("/", " ") * 3 + " " + t))
    if len(_TERMS_CACHE) >= _READ_CACHE_MAX:
        _TERMS_CACHE.pop(next(iter(_TERMS_CACHE)))
    _TERMS_CACHE[key] = ((st.st_mtime_ns, st.st_size), c)
    return c
```
In `rank_files`, replace the read and Counter lines with `c = _file_terms(root, f)`, keeping the `except OSError: continue`. In
`write_text` add `for k in [k for k in _TERMS_CACHE if k[0] == os.path.abspath(path)]: _TERMS_CACHE.pop(k, None)`, for the same reason
`_READ_CACHE` is popped there. The returned Counter is shared, so it must stay read-only. `rank_files` only reads it, so that holds.
The same pattern fits `focus()`'s `ast.parse` (up to 25 outlined files per call), but cache the tree's top-level spans, not the
tree, to keep memory small.

## 7. Background tabs keep polling

**Evidence.** `setInterval(jobBadge,3000)` has no `document.hidden` check. `pollConsole` re-arms itself every 0.7-2.6 s with two
requests (`/api/arch/chat` and `/api/arch/events`), hidden or not. `pollGpu` and the 9 s `refresh` already check.

**Fix:**
```js
async function jobBadge(){ if(document.hidden)return; const b=$("#sJob"); ...
// pollConsole, first line after clearTimeout:
  if(page!=="console")return; if(document.hidden){pollConsole._t=setTimeout(pollConsole,4000);return;}
```

## 8. The console redraws when nothing changed

**Evidence.** `pollConsole` calls `renderDoing(); renderStrip(); renderRoute();` on every tick. Each starts with `innerHTML=""`
(renderRoute redraws an SVG route map). `renderThread` already keys on content, but these three do not.

**Fix:**
```js
const moved=feed.events.length>0||!pollConsole._once; pollConsole._once=1;
applyEvents(feed); renderThread(snap);
if(moved){renderDoing(); renderStrip(); renderRoute();}
```
`pollGpu` calls `renderStrip()` itself, so the strip's live numbers still update.

**Found in passing (correctness).** `renderThread`'s key is the message count and lengths plus `busy`, but not `snap.partial`. So
Claude's "typing…" text does not grow while a reply streams; it only appears when the message lands. Add `+(snap.partial||"").length`
to the key.

## 9. State refreshes can stack

**Fix** (`refresh`): `if(refresh.busy&&!force)return; refresh.busy=1; try{...}finally{refresh.busy=0;}`

## 10. Studio walks the tree 4-5 times per message

**Fix** (`arch_chat._reply`):
```python
files = core.list_files(e.root, e.cfg)
ctx = e.context(text, cap) if files else "(the project folder is empty)"
...
items = hint_items(e, text, files=files) if files else []
```
Then give `hint_items(e, question, limit=6, files=None)` the line `for rel in (files if files is not None else core.list_files(e.root, e.cfg)):`.
`context()` and `Retriever.search` still walk once each, which is acceptable.

## 11. The read cache is limited by count, not bytes

**Evidence.** `_READ_CACHE_MAX = 4000` entries, each up to `limits.max_file_bytes` (400 KB) of raw bytes, kept for the life of the
process. The hub process serves Studio on any root and runs for days.

**Fix:**
```python
_READ_CACHE_BYTES = [0]
_READ_CACHE_CAP = 64 * 1024 * 1024
...
        old = _READ_CACHE.pop(key, None)
        if old: _READ_CACHE_BYTES[0] -= len(old[2])
        while _READ_CACHE and (len(_READ_CACHE) >= _READ_CACHE_MAX or _READ_CACHE_BYTES[0] + len(raw_full) > _READ_CACHE_CAP):
            _READ_CACHE_BYTES[0] -= len(_READ_CACHE.pop(next(iter(_READ_CACHE)))[2])
        _READ_CACHE[key] = (st.st_mtime_ns, st.st_size, raw_full); _READ_CACHE_BYTES[0] += len(raw_full)
```
`write_text`'s pop must subtract its length too.

## 12. Token events crowd the event ring

**Evidence.** `Engine._call`'s `tok` emits a `token` event about every 0.33 s while a model writes. `model_hub.Job` keeps
`MAX_EVENTS = 4000` and, when that fills, drops the oldest half, whatever their type. The UI only ever uses the latest token text
(`EV.live=(...+ev.text).slice(-900)`).

**Fix** (`Job` reader, where it does `self.events.extend(evs)`):
```python
for ev in evs:
    if ev.get("type") == "token" and self.events and self.events[-1].get("type") == "token":
        ev = dict(ev, text=(self.events[-1].get("text", "") + ev.get("text", ""))[-900:])
        self.events[-1] = ev          # one rolling token record between real events; the cursor arithmetic is unchanged
    else:
        self.events.append(ev)
```
Caveat: a watcher whose cursor already passed that slot misses the merged text until the next real event. That matches what the UI
shows (`EV.live` is cosmetic). If that is not acceptable, keep the append and skip only token events in the eviction instead.

## 13. Quadratic membership tests

`arch_index.hybrid`: `seen = set(fused)` before the tail loop, then `if f not in seen: fused.append(f); seen.add(f)`.
`Engine.context`: `pv = set(prev); ranked = prev + [f for f in ranked if f not in pv]`.

---

## Checked, not a cost

- **Imports at request time** (`import arch_chat`, `arch_hw`, `arch_server`, `arch_supervisor` inside handlers). After the first
  request each one is a `sys.modules` lookup (about a microsecond). Only the first hit pays the import, and moving the imports to the
  top would put that cost on hub start instead.
- **The single HTML file (334 KB now, not 245).** It is read from disk and `.replace`d on each page load. That is about 1 ms, once per
  load, not per poll. Gzip over loopback saves nothing worth having. Parsing and compiling the inline script is a one-time cost of
  tens of ms (estimate). Splitting it would break the "one HTML file, no build step" rule for no gain a person would notice. The real
  UI costs are the re-renders (#1, #8), not the file size.
- **`load_results()` four times per `/api/state`.** `results.json` is 24 KB, so about 1 ms each. It could be read once and passed
  down, but the saving is small.
- **`arch_fts.Index._open` runs `create ... if not exists` per operation.** It is deliberate: no handle stays open on Windows. It costs
  well under 1 ms.
- **`/api/arch/events/stream` (SSE) exists, but the UI polls `/events` instead.** Switching `pollConsole` to `EventSource` would remove
  the 0.7 s poll pair during runs. It is worth doing, but it is about 20 lines plus reconnect handling. After #7 and #8 the polling is
  cheap, so it ranks below them.
- **`/api/state` payload.** It resends static schemas (`settings_schema`, `run_options_schema`, `templates`, `task_templates`,
  `presets`, role recs) every 9 s. On loopback the transfer is cheap; the cost is the client's `JSON.stringify` of all of it (#1's key)
  plus `JSON.parse(JSON.stringify(S.config))`. Splitting the static half into a once-fetched `/api/arch/static` is the right long-term
  step (about 30 lines across both files). After #1 and #4, the per-tick cost is mostly the Ollama `/api/tags` round trip.

## How to measure (for the reviewer)

```python
# context() cost on the Architect tree, before/after #5 + #6 (run with: python -I -c ...  from the repo root, ARCHITECT_DATA_DIR=<tmp>)
import time, arch_core as core
e = core.Engine(".", "chat", "where is the plan validated", review="dry", say=lambda m: None, persist=False)
t = time.perf_counter(); e.context("where is the plan validated", 14000); print("first", time.perf_counter() - t)
e2 = core.Engine(".", "chat", "x", review="dry", say=lambda m: None, persist=False)
t = time.perf_counter(); e2.context("where is the plan validated", 14000); print("new engine", time.perf_counter() - t)
```
For #2-#4: count process launches with the hub open on the console for 60 s (Process Monitor filter "Process Create", or
`Get-Counter '\Process(tasklist*)\% Processor Time'`), before and after.
For #1: the Performance panel in the browser on Home for 30 s; "Recalculate Style / Layout" spikes every 9 s go away.
