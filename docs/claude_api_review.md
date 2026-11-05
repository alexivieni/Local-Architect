# Claude API review: prompt caching, message threads, token counting (2026-11-04)

Scope: how `arch_cloud.py` (chat, `ultra` = the Council, cache breakpoints, cost, fast mode, the local reply cache),
`arch_chat.py` (Studio) and `arch_claudecode.py` / `arch_agents.py` (Claude Code, the Crew) use the Claude API today, and
what three documentation pages say we should change.

Sources: the three pages were fetched today (vendor documentation, quoted where it matters):
- https://platform.claude.com/docs/en/build-with-claude/prompt-caching (about the first half was read word for word; the rest
  only as a summary)
- https://platform.claude.com/docs/en/build-with-claude/message-threads (beta)
- https://platform.claude.com/docs/en/build-with-claude/token-counting

Evidence labels used below: **vendor** = the page says so; **code** = read in this repo; **estimate** = my arithmetic, not
measured. None of the savings below were **measured** on this machine yet. Each recommendation names a check to measure it with.

---

## 0. Findings that are bugs today

1. **`cost()` overprices cache reads on our three main models (code + vendor).** `arch_cloud.cost` charges every cache read at
   0.1x input. The page says reads cost **0.05x on Opus 5.5 and Sonnet 5.5** and **0.025x on Fable 5.1**. Everywhere else it is 0.1x.
   The spend caps are therefore conservative (safe), but the shown price is wrong and the cache looks worth less than it is.
2. **Haiku 5.5 has a second price tier (vendor).** Prompts over 100K tokens cost $0.50/M in, not $0.10. `PRICES` has only the
   low tier. That matters for Studio at `ctx_size` "large" plus a long history.
3. **The first user turn is cached as ONE block that ends with the question (code).** `chat()` puts `cache_control` on
   `msgs[0]`. In Studio that block is `PROJECT CONTEXT ... EARLIER IN THIS CONVERSATION ... PERSON: <question>`
   (`arch_chat.compose`). So the cached prefix includes the question and can never be read by the next message. It only pays off
   inside one Council, where all five helper calls repeat the same first turn.
4. **`fmt == "json"` drops the system breakpoint (code).** The `elif fmt == "json"` branch turns `kw["system"]` back into a plain
   string, so the `cache_control` set two lines earlier is lost.
5. **The Council waits for a whole worker reply before starting the others (code + vendor).** `ultra()` calls
   `first_f.result()`. The page says a cache entry can be read once the first response **starts**, not when it finishes. Today the
   solver's full reply time (an estimated 15–40 s at medium effort) is added to every Council's wall clock.
6. **Both skeptics write the Haiku cache at once (code + vendor).** The two `refute` calls start together. "Cache entries are not
   usable by parallel requests until the first response starts", so both pay the 1.25x write and neither reads.
7. **The fast-mode retry breaks the cache (vendor).** `speed: "fast"` invalidates the system and messages caches. When fast mode
   is refused, `chat()` repeats the call at normal speed, so that retry pays a second cache write. This is small and rare. Leave it,
   but know about it.

---

## 1. Prompt caching

### What the page offers (vendor)
- At most **4 breakpoints** per request (`"cache_control": {"type": "ephemeral"}`; `"ttl": "1h"` for one hour). The prefix
  order is **tools → system → messages**. A top-level `cache_control` turns on automatic caching and uses one of the 4 slots.
- Price multipliers on base input: a 5-minute write costs **1.25x** and a 1-hour write **2x**. A read costs **0.1x**, except
  **0.05x** on Opus 5.5 and Sonnet 5.5 and **0.025x** on Fable 5.1 and Mythos 5.1. Each read refreshes the TTL for free.
- **Minimum cacheable prefix:** **512 tokens** on Fable 5.1, Mythos 5.1, Opus 5.5, Opus 5, Sonnet 5.5, Fable 5, Mythos 5 and
  Haiku 5.5. 1,024 on Sonnet 5, Sonnet 4.x and Opus 4.8/4.1/4. 2,048 on Opus 4.7 and Haiku 3.5. 4,096 on Opus 4.6/4.5 and
  Haiku 4.5. **A shorter prefix is silently not cached, with no error.** You can see it only when `cache_creation_input_tokens`
  and `cache_read_input_tokens` are both 0.
- The lookback for a breakpoint is **20 positions**. A hit needs a 100% identical prefix up to the breakpoint.
- What invalidates which cache:
  - Changing tool definitions invalidates all three (tools, system, messages).
  - Toggling web search or citations, or changing `speed`, invalidates system and messages.
  - Changing `tool_choice` or adding/removing images invalidates messages only.
  - Changing thinking parameters or `output_config.effort` invalidates messages. Setting effort explicitly to the model's
    default does not.
- **Mid-conversation system messages:** on Opus 5.5, Sonnet 5.5, Haiku 5.5, Opus 5, Opus 4.8, Fable 5/5.1 and Mythos 5/5.1 you
  may add `{"role": "system", ...}` inside `messages` instead of editing `system`, and the system and message caches stay valid.
  **Not on Sonnet 5.** Mid-conversation tools exist behind the beta `inline-tools-2026-09-15` (`tool_addition` block).
- **Pre-warming:** send `max_tokens: 0` and you get `content: []`, `stop_reason: "max_tokens"`. It is refused with `stream: true`,
  `thinking.type: "enabled"`, `output_config.format`, a forced `tool_choice`, or inside batches.
- Usage: `input_tokens` counts only what comes after the last breakpoint. `cache_creation.ephemeral_5m_input_tokens` and
  `ephemeral_1h_input_tokens` split the write by TTL.

### What the app already does (code)
- There are two breakpoints: the system block and the first user turn when it is over 3,000 characters (about 850 tokens).
- TTL is `online.cloud_cache_ttl` (5m default, 1h opt-in). `cost()` prices a 1h write at 2x.
- `ultra()` runs the solver first "so the rest read it at ~0.1x".
- `usage.cache_read_input_tokens` / `cache_creation_input_tokens` go into the call record (`cached`, `cache_write`).
- The local reply cache (`cloud_cache/`, 30 days, sha256 of model+effort+system+messages+fmt) answers an identical request for
  $0 and is never used for a web-search answer. This is separate from Anthropic's prompt cache, and it is the bigger saving for
  repeated questions.

### What it should do next (proposed exact changes to `arch_cloud.py`; I did not edit it)

**a. Per-model cache-read price** (fixes finding 1):
```python
CACHE_READ_MUL = {"claude-opus-5-5": 0.05, "claude-sonnet-5-5": 0.05, "claude-fable-5-1": 0.025}   # vendor, 2026-11
PRICE_TIERS = {"claude-haiku-5-5": (100_000, (0.50, 2.50))}   # input above 100K tokens; output price: CHECK the page first

def cost(model, prompt_tokens, gen_tokens, cache_read=0, cache_write=0, ttl="5m"):
    m = api_model(model)
    pin, pout = PRICES.get(m, PRICES[DEFAULT_MODEL])
    tier = PRICE_TIERS.get(m)
    if tier and (prompt_tokens or 0) + (cache_read or 0) + (cache_write or 0) > tier[0]:
        pin, pout = tier[1]
    rmul = CACHE_READ_MUL.get(m, 0.1)
    wmul = 2.0 if ttl == "1h" else 1.25
    return ((prompt_tokens or 0) + rmul * (cache_read or 0) + wmul * (cache_write or 0)) * pin / 1e6 + (gen_tokens or 0) * pout / 1e6
```
(The Haiku output price above 100K was not on the part of the page I read. Fill it in from the page; do not guess.) Add a
selftest_cloud check: `cost("claude-sonnet-5-5", 0, 0, cache_read=1_000_000) == 0.10`.

**b. Split the first user turn into a stable block and a per-message block** (fixes finding 3). `chat()` cannot know where the
context ends, so let the caller say so with a marker that never reaches the API:
```python
CACHE_CUT = "\x1fARCH_CACHE_CUT\x1f"      # arch_chat.compose puts it between the context/hints and the history+question
...
first = msgs[0]["content"] if msgs and msgs[0]["role"] == "user" else ""
if CACHE_CUT in first:
    head, tail = first.split(CACHE_CUT, 1)
    blocks = [{"type": "text", "text": head}] + ([{"type": "text", "text": tail}] if tail.strip() else [])
    if est_tokens(system + head, model) >= cache_min(model):
        blocks[0]["cache_control"] = _cache_control(ttl)
    msgs[0] = {"role": "user", "content": blocks}
elif msgs and msgs[0]["role"] == "user" and len(first) > 3000:
    ...  # today's behaviour
```
Also strip `CACHE_CUT` from `plain` before `cache_key()`, so the local reply cache is unchanged. A follow-up in Studio then
reads the cache only when the context is the same. `Engine.context(text, cap)` ranks files by the question, so for a different
question it often is not. The saving across messages is therefore real only for "ask again / rephrase / follow up within 5 min"
on the same files. This needs measuring with `cached > 0` in the call records before it is advertised.

**c. Respect the minimum** so the record can tell "too short" apart from "missed":
```python
CACHE_MIN = {"claude-opus-5-5": 512, "claude-sonnet-5-5": 512, "claude-haiku-5-5": 512, "claude-opus-5": 512,
             "claude-fable-5-1": 512}                       # vendor; unknown model -> 1024 (the safe side)
def cache_min(model): return CACHE_MIN.get(api_model(model), 1024)
```
In the record, write `rec["cache"] = "too short" | "written" | "read" | "missed"`. "missed" means the prefix was big enough but
both usage fields were 0, which is a real bug signal (a prefix that changes between calls). A breakpoint on a short prefix costs
nothing (no write is billed), so this is about honest reporting, not money.

**d. Keep the breakpoint when `fmt == "json"`** (finding 4): append `"Answer with JSON only."` as a second, uncached system block
after the cached one, instead of rebuilding `system` as a string.

**e. Council breakpoints and timing** (findings 5 and 6). The shared prefix is `[system SYSTEM][user: context + question]`. Each
lens adds a different user turn after it. Caches are per model, so the sharing groups are:
- 3 workers (Sonnet): one write, two reads.
- 2 skeptics (Haiku): today two writes. Proposed: start skeptic 2 when skeptic 1's first token arrives (same trick as below),
  or put no breakpoint on Haiku at all. At $0.10/M a 4K prefix costs $0.0004, so caching it saves almost nothing and two
  concurrent writes cost 25% more.
- the lead (Opus): it is alone, so a 5m write is +25% on the prefix with no read in this Council. Proposed: no breakpoint on the
  lead's first turn unless Studio shows a follow-up pattern (measure it first). Keep the system breakpoint (it is cheap and may
  be reused by the next lead within 5 minutes).

Replace `first_f.result()` with "wait until the first streamed piece of the solver arrives (a `threading.Event` set from
`on_token`), then submit the critic and the alternative". Pre-warming with `max_tokens: 0` would be cleaner, but it is refused
with `stream: true` and with some thinking settings. Test it on adaptive thinking before relying on it.

**f. 5m vs 1h.** A Council finishes in a few minutes, so 5m is right (1h costs 2x to write and never gets the extra time back).
1h pays off only when the same prefix is read again between 5 and 60 minutes later. Over a 4K-token Opus 5.5 prefix (estimate):

| reads of that prefix in the hour | 5m TTL (re-written each time it lapsed) | 1h TTL |
|---|---|---|
| 1 read 20 min later | 2 writes = 2.5x | 1 write + 1 read = 2.05x |
| 3 reads spread over the hour | 4 writes = 5x | 2x + 3×0.05x = 2.15x |

So 1h is worth it for the Crew/night shift and for runs where Claude is the planner every sprint (`cloud_tier = plan`). Proposed:
choose the TTL per call (`ttl="1h"` when the caller says the prefix will come back after >5 min, e.g. plan-mode roles), and keep
`online.cloud_cache_ttl` as the person's override. Mixed TTLs are allowed, but the 1h breakpoints must come before the 5m ones
(vendor ordering rule; confirm on the page's "mixing TTLs" section, which I only read in summary).

**g. Mid-conversation system messages instead of editing `system`.** Today `chat()` joins every system message into the one
top-level `system`. In a run, `Engine.call(role, ..., extra_system=...)` gives each role a different system text, and system
comes before messages, so **a role change throws away the cached project context in the first user turn**. Proposed, for the
models on the vendor list (`MIDCONV_SYSTEM = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5",
"claude-fable-5-1"}`; never Sonnet 5):
- `system` = the first system message only (the stable Architect preamble), with a breakpoint;
- every later system message stays where it is in `messages` as `{"role": "system", "content": ...}`;
- for other models, keep today's join.

This needs `Engine.call` to put the stable part first and the role prompt as a second system message. That change is in
`arch_core`, which is outside this brief, so it is listed here and not implemented. The Council lenses already use user turns
after the breakpoint, so they are unaffected.

**Tools.** The web-search tool is the only tool `chat()` sends, and only for the lead and Studio. Tools sit first in the prefix,
and toggling web search invalidates system and messages. A lead with web and one without never share a cache. That is fine.
Never reorder `WEB_TOOL`'s keys between calls (the request is built from the same dict each time, which is correct today).

### Expected saving (estimate; prices from the page)
A Council over Studio's "normal" context (14,000 chars ≈ 4,000 tokens, plus a short system):
- workers, 3× Sonnet 5.5 at $2/M: no cache $0.024; cached today 1 write ($0.010) + 2 reads ($0.0008 at 0.05x) = **$0.0108**.
  `cost()` currently shows $0.0116 because it charges reads at 0.1x.
- skeptics, 2× Haiku: $0.0008 uncached against $0.0010 with two concurrent writes. **Drop the breakpoint: about -$0.0002.**
- lead, Opus 5.5: $0.016 uncached against $0.020 with a write nobody reads. **Drop it: about -$0.004 per Council**, unless
  follow-ups prove otherwise.
- latency: starting the critic and alternative on the solver's first token instead of its last saves **one worker's reply time**
  (estimate 15–40 s) per Council. This is the biggest win the person will feel.

### Risks
- A per-model table goes stale when a model launches. Unknown models fall back to 0.1x reads and a 1,024 minimum, which is the
  safe side for the caps.
- The cut marker must never reach the API or the local reply-cache key. A selftest should assert both.
- Starting the other workers on the first token relies on the vendor's "usable once the first response starts". If that is not
  true for streamed responses, we only lose the read (correctness is unaffected). Measure `cached` on worker 2 and 3.
- Mid-conversation system messages on a model not in the list are a 400. Gate on the explicit set and never default to on.

---

## 2. Message threads (beta)

### What the page offers (vendor)
- The server keeps the conversation. Turn 1 sends everything with `"thread": {"type": "create"}`. Later turns send only the new
  messages with `"thread": {"type": "continue", "previous_message_id": "<id>"}` and must **omit `system` and `tools`** (sending
  either, even null, is a 400).
- Headers: `anthropic-beta: message-threads-2026-08-12` on every request of the thread. SDK: `client.beta.messages.thread(...)`
  and `thread.send(...)`, which replays on errors.
- Fixed for the life of a thread (else `thread_fingerprint_mismatch`): `model`, `tool_choice`, `thinking`, `output_config`
  (so **effort and the JSON schema**), `mcp_servers`, `context_management`, top-level `cache_control`, and the beta set.
- Each response id can be continued **once**, so there is no branching. Continue only after `message_stop`.
- A thread expires **5 minutes** after its last request and is deleted within an hour. There is no list/read/delete API. A 404
  `thread_not_found` means replay with a new create.
- **Limits:** no Message Batches, **no token counting (400)**, no compaction on demand (to compact, start a new thread from a
  summary), almost no context editing, not ZDR, not HIPAA.
- **Threads do not change pricing or rate limits** and **do not cache by themselves.** You still need breakpoints (at most 4
  including the top-level one, and the create request's system/tools breakpoints count again on every continue).

### What the app already does (code)
- `chat()` sends the whole conversation every call (stateless). The local reply cache is keyed on that whole conversation.
- The Council is fan-out: five calls branch off one shared prefix, then one lead call.
- The Crew (`arch_agents.py`) already uses Managed Agents sessions (`client.beta.sessions`), which keep their own state on the
  server. Threads do not apply there.
- Claude Code (`arch_claudecode.py`) continues with `--resume` and keeps its own state. Threads do not apply there either.

### What it should do next
- **Not for the Council.** Its shape is a branch (three lenses and two skeptics off one prefix), and a thread id can be continued
  only once. Prompt caching already covers the shared prefix.
- **Maybe for Studio follow-ups and Claude-as-a-run-role tool loops** (an `explore`-style loop where Claude is the model and each
  turn adds a tool result). Each turn would upload only the new tool result instead of the whole history. That saves **upload
  bytes and request-building time, not dollars** (vendor: pricing unchanged). Effort and the output format must stay fixed for
  the thread, which matches one role's loop but not Studio, where the person can change effort per message. Studio would need a
  new thread whenever effort changes.
- If adopted: a small `arch_cloud.Thread` wrapper that falls back to the stateless call on `thread_not_found`, on any
  `thread_*` 400, and whenever `online.cloud_threads` is off. That is a new setting in `arch_params.SETTINGS` and
  `arch_core.DEFAULTS`, default off, because it is beta and not ZDR. Keep breakpoints as today. Count tokens on the stateless
  shape (see §3).
- **Recommendation: do not build it yet.** The payoff is latency on long tool loops, and we have no Claude-driven tool loop
  that is long today. Revisit when one exists, and measure request size and time to first token with and without.

### Risks
- Beta header churn, and a 5-minute expiry that our slow local steps between Claude calls (a test run can take minutes) would
  often exceed. Every expiry costs a full replay.
- It is not ZDR. If the person's org needs ZDR, the setting must stay off.
- Token counting returns 400 on a thread, so a cost preview has to rebuild the full conversation locally anyway.

---

## 3. Token counting

### What the page offers (vendor)
- `POST /v1/messages/count_tokens`, SDK `client.messages.count_tokens(...)`. It takes the same inputs as a message (system,
  tools, images and PDFs as base64, thinking) and returns `{"input_tokens": N}`.
- **Free.** It has its own rate limit (5,000 / 10,000 / 20,000 RPM by tier), separate from message creation.
- It is "an estimate" that can differ by a small amount, and it does **not** apply caching logic (it counts the whole prompt).
- Not supported: **server tools (web search, web fetch, code execution)**, the MCP connector, and url/file-sourced images or
  documents.
- Claude 4.7 and later use a tokenizer that makes about 30% more tokens for the same text, so recount per model.

### What the app already does (code)
- Nothing before a call. The per-run (`limits.cloud_usd_per_run`) and per-day (`limits.cloud_usd_per_day`) caps are checked
  against what was **already** spent, so one large call can overshoot a cap by its whole cost.
- Studio shows the cost after the reply (`usd` on the message).

### What it should do next (done in this change: `arch_tokens.py`)
- `arch_tokens.count(model, system, messages, cfg, client=None, tools=None)` calls `client.messages.count_tokens` on **the same
  shape `chat()` sends** (`arch_tokens.shape`: system messages folded in, user first, no trailing assistant). It imports the SDK
  lazily and is refused with `LLMError` whenever `arch_cloud.allowed(cfg)` says no (online off, eval, the self-tests). It never
  touches the spend.
- `arch_tokens.estimate_cost(model, system, messages, expected_out, cfg, client=None, cached_prefix=0)` returns `usd` via
  `arch_cloud.cost()` and `usd_cached` with a cached prefix, plus `source` = "counted" or "estimate". When the counter is not
  allowed or fails, it falls back to characters / 3.5 and says why.
- `arch_tokens.say(est)` gives the hub one line, e.g. "about $0.012 (3,400 tokens in, ~1,500 out; counted)".

Proposed wiring (outside this brief):
- **Hub preview:** a `POST /api/arch/cloud/estimate` in `architect_hub.py` that builds the same user turn as `arch_chat._reply`
  (context + hints + history + question) and calls `estimate_cost` inside `arch_cloud.asked_for()`, since the person is about to
  send this message to Claude. Debounce it to about 1 call per second while typing. The free RPM is generous, but it is still a
  request off the machine. For the Council, show 3× worker + 2× skeptic + lead (with `expected_out` 1,200 / 600 / 4,000, the
  `num_predict` values `ultra()` uses).
- **Web search:** the counter refuses server tools, so count **without** `WEB_TOOL` and add `WEB_TOOL["max_uses"] ×
  WEB_SEARCH_USD` as the most it can add. Search results also arrive as input tokens on the resumed turn, so label that part an
  estimate.
- **Budget check before the call** (proposed for `arch_cloud.chat`, just after today's cap checks):
  ```python
  import arch_tokens
  guess = arch_tokens.estimate_local(system, plain)                       # local: no extra round trip on every call
  worst = cost(model, guess, kw["max_tokens"]) * (2.0 if fast else 1.0)  # the most this call can cost (thinking is output)
  if _SPENT["usd"] + worst > cap or today + worst > day_cap:
      raise LLMError(f"this Claude call could cost up to ${worst:.2f}, over what is left of the cap ...")
  ```
  This uses the local estimate, not `count()`, so the hot path gets no extra request and works for the self-tests. It uses
  `max_tokens` as the output bound, so it is a ceiling rather than a guess. Risk: at 64,000 max tokens on Opus 5.5 the ceiling
  is $1.28 of output, above the default $0.50 run cap, so **every xhigh/max call would be refused.** Use
  `min(max_tokens, 2 × num_predict)` for the bound and say so in the message. Or check only that the input fits the remainder,
  and keep today's after-the-fact check for the output.
- chars/3.5 is the local rule. Because the newer tokenizer makes about 30% more tokens, chars/3.5 probably **undercounts** on
  5.x models. Measure the ratio once (count a few real prompts, compare with `len/3.5`), and if it is consistently off, change
  `CHARS_PER_TOKEN` and label it measured.

### Expected saving
Counting saves nothing by itself. It prevents overshoot: today a single call can pass a cap by its whole cost (for example a
large Opus answer with max effort at about $1 against the $0.50 run cap). It also lets the person see "about $0.04" before
choosing the Council over a single answer. The Council costs roughly 2–3x a single Opus answer over the same context (estimate
from the numbers in §1).

### Risks
- It is one more request when online is on. The SDK client is created per count (cheap). A failure falls back to the estimate,
  never blocks sending.
- Counts are "an estimate" too, so the preview must keep saying "about".
- `allowed()` without `asked_for()` refuses in Use Claude = off mode. The hub preview must wrap the call the same way the send
  does, or it will always show the local estimate.

---

## Summary of proposed `arch_cloud.py` changes (for the agent who owns it)
1. `cost()`: per-model read multiplier (`CACHE_READ_MUL`) and Haiku's >100K tier (`PRICE_TIERS`), plus a selftest.
2. `chat()`: split the first user turn at `CACHE_CUT`, keep the marker out of `cache_key`, and set `rec["cache"]` from the usage
   fields and `cache_min(model)`.
3. `chat()`: the `fmt == "json"` branch appends a second system block instead of replacing the cached one.
4. `ultra()`: start the critic and alternative on the solver's first token. Stagger the skeptics, or no breakpoint on Haiku. No
   first-turn breakpoint for the lead unless measured follow-ups justify it.
5. `chat()`: keep later system messages in place for `MIDCONV_SYSTEM` models (needs the matching `Engine.call` change).
6. `chat()`: a pre-send budget check with `arch_tokens.estimate_local` and a bounded output guess.
7. Message threads: not now. Revisit when a Claude-driven tool loop exists, behind a default-off setting.
