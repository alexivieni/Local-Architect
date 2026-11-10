"""Claude as an optional cloud tier: the last escalation step after the local heavy model, and only when the person turned it on.

Off by default and invisible when off: the Anthropic SDK is imported only inside `chat()`, which is only reached when
`online.enabled` is true AND `online.cloud_tier` is "escalation" AND a `claude:` model is set as `tiers.cloud`. A fresh install
stays standard-library only and never opens a connection off this machine.

What stays true with it on:
  * code still decides: Claude's reply goes through the same apply/tests/gates as any local reply;
  * a refusal is "could not run" (LLMError), never a pass;
  * spend is capped per process (`limits.cloud_usd_per_run`) and per day across all calls (`limits.cloud_usd_per_day`,
    kept in data_dir()/cloud_spend.json), both checked BEFORE each request: the worst case of the call (input + max_tokens) is
    held under a lock first, so calls in flight together all count, and max_tokens is clamped to what is left. Claude Code runs,
    fleets, Managed Agent sessions and batches hold their money through the same `reserve` and record what they really spent
    with `settle`;
  * a reply Claude already gave to the exact same request is answered from data_dir()/cloud_cache/ for $0 (`online.cloud_cache`,
    30 days; never during eval or the self-tests). Efficiency only: cached replies are still transport "claude", never training data;
  * the key is never stored by the app: the SDK reads ANTHROPIC_API_KEY (or an `ant auth login` profile) itself;
  * replies are tagged transport "claude", and arch_dataset leaves them out of training data (Anthropic's terms restrict
    using outputs to train competing models);
  * web lookups (`online.web_search`, or the per-message `web` flag) are Anthropic's server-side search tool, offered only on a chat
    reply the person sent, only to Opus/Sonnet 5.x, never replayed from the cache; each one is counted in the call record.
"""
import time

from arch_core import LLMError

PREFIX = "claude:"
DEFAULT_MODEL = "claude-opus-5-5"
# USD per million tokens (input, output): Anthropic's first-party API list prices as of 2026-11-03. THE one Claude price list: cost
# estimates, the orchestration view and the FinOps report all read it (FinOps kept its own copy, which drifted to old prices). Cache
# reads are 0.1x the input price and 5-minute cache writes 1.25x (arch_orchestration.CACHE_READ_X / CACHE_WRITE_X).
PRICES = {
    "claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-2": (8.0, 40.0), "claude-fable-5-1": (10.0, 50.0), "claude-opus-5": (5.0, 25.0), "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-4-8": (5.0, 25.0), "claude-opus-4-7": (5.0, 25.0), "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0), "claude-haiku-4-5": (1.0, 5.0),
}
PRICES_AS_OF = "2026-11-03"
_SPENT = {"usd": 0.0, "calls": 0}
import threading as _th
_HERE = _th.local()          # set while ONE chat reply the person explicitly sent to Claude is being answered


class asked_for:
    """`with arch_cloud.asked_for():` -- the person chose Claude for this message, so the Use Claude mode is not needed (online
    features must still be on). Thread-local: it never leaks into a run or another chat."""
    def __init__(self, sink=None, effort=None, web=False):
        self.sink = sink                     # gets each piece of text as it streams (the chat shows Claude typing)
        self.effort = effort if effort in EFFORTS else None
        self.web = bool(web)                 # this message may look things up on the web (see web_wanted)
    def __enter__(self):
        _HERE.on, _HERE.sink, _HERE.effort, _HERE.web = True, self.sink, self.effort, self.web
    def __exit__(self, *a):
        _HERE.on, _HERE.sink, _HERE.effort, _HERE.web = False, None, None, False
        return False


# ---- web lookups for chat answers: Anthropic's own server-side search tool, run on their side, never from this machine
WEB_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}
WEB_SEARCH_USD = 0.01                # vendor price card: $10 per 1,000 searches, on top of tokens


def web_supported(model):
    """The search tool is offered to Opus and Sonnet 5.x only; any other model answers without it."""
    import re as _re
    return bool(_re.match(r"claude-(?:opus|sonnet)-5(?:[-.]|$)", api_model(model)))


def web_setting(cfg):
    v = ((cfg or {}).get("online") or {}).get("web_search", "off")
    return v is True or str(v).strip().lower() in ("on", "true", "1", "yes")


def web_wanted(model, cfg, asked=False):
    """True when this chat answer may search: the person asked for it on this message, or Settings > Online > Web lookups is on;
    and the model supports the tool. Only ever inside a chat reply the person sent (asked_for), never inside a run."""
    return bool(getattr(_HERE, "on", False)) and (bool(asked) or web_setting(cfg)) and web_supported(model)


def _field(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def web_report(final):
    """-> (searches, queries, errors) from a finished message. Errors from the search tool arrive as result blocks inside the reply
    (web_search_tool_result_error), never as exceptions, so they are read here and shown under Show work."""
    queries, errors = [], []
    for b in (_field(final, "content") or []):
        kind = _field(b, "type")
        if kind == "server_tool_use" and _field(b, "name") == "web_search":
            q = _field(_field(b, "input") or {}, "query")
            if q:
                queries.append(str(q)[:200])
        elif kind == "web_search_tool_result":
            c = _field(b, "content")
            if c is not None and not isinstance(c, list) and _field(c, "type") == "web_search_tool_result_error":
                errors.append(str(_field(c, "error_code") or "error"))
    stu = _field(_field(final, "usage"), "server_tool_use")
    n = _field(stu, "web_search_requests") if stu is not None else None
    try:
        n = int(n) if n is not None else len(queries)
    except (TypeError, ValueError):
        n = len(queries)
    return n, queries, errors


EFFORTS = ("low", "medium", "high", "xhigh", "max")


def effort_for(text, chosen=None):
    """'ultrathink' / 'ultracode' (or 'think harder') anywhere in the message = max effort; else what the person picked; else high."""
    import re as _re
    if _re.search(r"\bultra-?(?:think|code)\b|\bthink (?:harder|really hard)\b", str(text or ""), _re.I):
        return "max"
    return chosen if chosen in EFFORTS else "high"


# Use Claude = plan: Claude does the thinking roles, your local models do everything else (ids from arch_roles.ROLES; the
# evaluator is the strict judge).
PLAN_ROLES = ("planner", "architect", "evaluator", "reviewer")
# Effort for a run's role when nobody picked one (an explicit asked_for effort or 'ultrathink' always wins).
ROLE_EFFORT = {"planner": "high", "architect": "high", "evaluator": "medium", "reviewer": "medium"}
ROLE_EFFORT_DEFAULT = "medium"


def note_role(role):
    """Engine.model_for calls this when it hands a role a claude: model, so the very next chat() on this thread knows whose call it
    is (its effort). Read once and cleared by chat(), so it never leaks into an unrelated call."""
    _HERE.role = role


def role_effort(role):
    return ROLE_EFFORT.get(role, ROLE_EFFORT_DEFAULT) if role else None


def is_cloud(model):
    return str(model or "").startswith(PREFIX)


def api_model(model):
    m = str(model or "")[len(PREFIX):].strip() if is_cloud(model) else str(model or "")
    return m or DEFAULT_MODEL


def _testing():
    """True inside the self-tests: they must run on the scripted stand-in model, never on Claude (the suites read your real settings,
    and Use Claude = all would send every test call to the API, cost money and fail the self-improve gate). The cloud checks that
    test this module on purpose set ARCHITECT_CLOUD_TEST=1 and inject a fake client."""
    import os
    import sys
    if os.path.basename(sys.argv[0] or "").startswith(("arch_selftest", "arch_hub_check")):
        os.environ["ARCHITECT_SELFTEST"] = "1"                       # children of a suite inherit it
    return os.environ.get("ARCHITECT_SELFTEST") == "1" and os.environ.get("ARCHITECT_CLOUD_TEST") != "1"


def allowed(cfg):
    """-> (True, "") when a cloud call may go out now, else (False, plain reason)."""
    import os
    if _testing():
        return False, "Claude is never used inside the self-tests"
    if os.environ.get("ARCHITECT_EVAL") == "1":                 # benchmarks measure YOUR models: a cloud rescue would fake the score
        return False, "Claude is never used during eval (it would measure Claude, not your models)"
    on = (cfg or {}).get("online") or {}
    if not on.get("enabled"):
        return False, "Claude is off: online features are off (Settings > Allow online features)"
    if (on.get("cloud_tier") or "off") not in ("escalation", "chat", "plan", "all") and not getattr(_HERE, "on", False):
        return False, "Claude is off (Settings > Online > Use Claude)"
    return True, ""


def chosen_model(cfg):
    """The Claude model for a message the person sent to Claude by hand: needs online features on, not the Use Claude mode."""
    on = (cfg or {}).get("online") or {}
    m = ((((cfg or {}).get("tiers") or {}).get("cloud") or "") or on.get("cloud_model") or DEFAULT_MODEL).strip()
    return m if is_cloud(m) else PREFIX + m


def cloud_model(cfg):
    """The `claude:` model to escalate to, or "" when the cloud tier is off or not set."""
    ok, _ = allowed(cfg)
    m = ((((cfg or {}).get("tiers") or {}).get("cloud") or "") or (((cfg or {}).get("online") or {}).get("cloud_model") or "")).strip()
    if ok and m and not is_cloud(m):
        m = PREFIX + m
    return m if ok and m else ""


def status(cfg):
    """For the hub: is the cloud tier usable right now? Never reads or returns the key itself, only whether one is set."""
    import importlib.util
    import os
    ok, why = allowed(cfg)
    sdk = importlib.util.find_spec("anthropic") is not None
    key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    on_ = bool(((cfg or {}).get("online") or {}).get("enabled"))
    ready = ok and sdk and key
    chat_ready = on_ and sdk and key
    say = ("Claude ready (" + api_model(cloud_model(cfg)) + ")") if ready else (why if not ok else
          "Claude: the Anthropic SDK is not installed" if not sdk else "Claude: no key set (ANTHROPIC_API_KEY)")
    return {"on": ok, "sdk": sdk, "key": key, "ready": ready, "text": say, "mode": mode(cfg), "chat_ready": chat_ready,
            "chat_why": "" if chat_ready else ("turn on Settings > Online > Allow online features" if not on_ else
                        "install the Anthropic SDK (pip install anthropic)" if not sdk else "set ANTHROPIC_API_KEY, then restart the hub"),
            "model": api_model(chosen_model(cfg)), "spent_usd": round(_SPENT["usd"], 4), "spent_today_usd": round(spent_today(), 4),
            "held_today_usd": round(held_today(), 4), "day_cap_usd": _day_cap(cfg)}


def mode(cfg):
    """off | escalation | chat | plan | all -- and "off" whenever a cloud call is not allowed right now."""
    return ((cfg or {}).get("online") or {}).get("cloud_tier", "off") if cloud_model(cfg) else "off"


ULTRA_LENSES = [
    ("solver", "Answer the request directly and concretely. Name files and functions. Give code when it helps."),
    ("critic", "Look for what could go wrong: bugs, edge cases, risks, wrong assumptions in the request or the code. Be specific."),
    ("alternative", "Propose a different, simpler or more robust way to do this, and say when it would be the better choice."),
]


ULTRA_WORKER = "claude:claude-sonnet-5-5"
ULTRA_SKEPTIC = "claude:claude-haiku-5-5"


def _as_cloud(m, default):
    m = str(m or "").strip() or default
    return m if is_cloud(m) else PREFIX + m


def ultra(model, messages, cfg, effort="high", sink=None, worker=None, client=None, skeptic=None, web=False):
    """Ultracode for the app: three Claude subagents work on the same request at once, each from its own angle (solver, critic,
    alternative), then two skeptics try to refute the solver, then the chosen model, as lead, reads their notes and writes the one
    answer. Workers run at medium effort on `online.ultra_worker` (Sonnet), skeptics on `online.ultra_skeptic` (Haiku); only the
    lead's answer streams to the chat. `web` lets the lead (only) use the web lookup tool. -> the lead's reply dict, with usd = the
    whole team's cost."""
    import concurrent.futures as cf
    on = (cfg or {}).get("online") or {}
    worker = _as_cloud(worker or on.get("ultra_worker"), ULTRA_WORKER)
    skeptic = _as_cloud(skeptic or on.get("ultra_skeptic"), ULTRA_SKEPTIC)
    notes, usd, calls_all = {}, 0.0, []

    def one(lens):
        name, how = lens
        with asked_for(None, "medium"):
            msgs = list(messages) + [{"role": "user", "content": f"You are the {name} on a small team. {how} Keep it under 300 words."}]
            _HERE.label = name
            try:
                return name, chat(worker, msgs, {"num_predict": 1200}, cfg=cfg, client=client)
            finally:
                _HERE.label = None

    with cf.ThreadPoolExecutor(max_workers=len(ULTRA_LENSES)) as ex:
        first_f = ex.submit(one, ULTRA_LENSES[0])                    # writes the shared cache ...
        try:
            first_f.result()
        except Exception:
            pass
        futs = [first_f] + [ex.submit(one, l) for l in ULTRA_LENSES[1:]]   # ... the rest read it at ~0.1x
        for fut in cf.as_completed(futs):
            try:
                name, r = fut.result()
                calls_all.extend(r.get("calls") or [])
                notes[name] = r["text"].strip()
                usd += r.get("usd") or 0
                if sink:
                    sink(f"_[{name} done]_ ")
            except Exception as e:                                   # one agent failing never sinks the team
                notes[f"agent{len(notes) + 1}"] = f"(an agent failed: {str(e)[:120]})"
    team = "\n\n".join(f"--- {k.upper()} ---\n{v}" for k, v in notes.items())
    if sink:
        sink("_[verifying]_ ")
    # adversarial verify: two independent skeptics try to REFUTE the solver's answer; their objections go to the lead
    def refute(i):
        with asked_for(None, "medium"):
            _HERE.label = f"skeptic {i}"
            return chat(skeptic, list(messages) + [{"role": "user", "content":
                        "A teammate proposed this answer:\n\n" + notes.get("solver", "") + f"\n\nYou are skeptic #{i}. Try to REFUTE it: list concrete "
                        "errors, wrong assumptions or missing steps, each with why. If it is correct, say 'no objections'. Under 150 words."}],
                        {"num_predict": 600}, cfg=cfg, client=client)
    objections = []
    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        for fut in cf.as_completed([ex.submit(refute, i) for i in (1, 2)]):
            try:
                rr = fut.result()
                calls_all.extend(rr.get("calls") or [])
                usd += rr.get("usd") or 0
                objections.append(rr["text"].strip())
            except Exception as e:
                objections.append(f"(a skeptic failed: {str(e)[:100]})")
    team += "\n\n--- SKEPTICS (tried to refute the solver) ---\n" + "\n\n".join(objections)
    lead_msgs = list(messages) + [{"role": "user", "content": "Your team's notes on the request above (solver, critic, alternative, and two skeptics "
                                   "who tried to refute the solver):\n\n" + team +
                                   "\n\nAs the lead: write the single best answer. Keep only claims that survived the skeptics, fix what they showed is "
                                   "wrong, and mention a real risk if it matters. Do not mention the team."}]
    if sink:
        sink("\n\n")
    with asked_for(sink, effort, web):                               # only the lead may search (one lookup budget, not five)
        _HERE.label = "lead"
        try:
            r = chat(model, lead_msgs, {"num_predict": 4000}, cfg=cfg, client=client)
        finally:
            _HERE.label = None
    r["calls"] = calls_all + (r.get("calls") or [])
    r["usd"] = round((r.get("usd") or 0) + usd, 5)
    r["agents"] = len(notes) + len(objections)
    return r


FAST_MODELS = ("claude-opus-5-5", "claude-opus-5")            # fast mode exists only for these, and only on the Claude API


def cost(model, prompt_tokens, gen_tokens, cache_read=0, cache_write=0, ttl="5m"):
    """USD for one call. A cache read costs 0.1x input; a cache write 1.25x input with the 5-minute lifetime, 2x with 1 hour."""
    pin, pout = PRICES.get(api_model(model), PRICES[DEFAULT_MODEL])
    wmul = 2.0 if ttl == "1h" else 1.25
    return ((prompt_tokens or 0) + 0.1 * (cache_read or 0) + wmul * (cache_write or 0)) * pin / 1e6 + (gen_tokens or 0) * pout / 1e6


def spent():
    return dict(_SPENT)


def _cap(cfg):
    try:
        return float(((cfg or {}).get("limits") or {}).get("cloud_usd_per_run", 0.5))
    except (TypeError, ValueError):
        return 0.5


LONG_LIVED = ("orchestrator", "session", "chat", "planner", "architect")     # prompts reused over more than five minutes
SHORT_LIVED = ("agent", "fleet", "batch", "subagent", "once")                 # a few calls close together, then done


def choose_ttl(cfg, use=None, gap_s=None):
    """Prompt-cache lifetime for one use. online.cloud_cache_ttl: "5m" or "1h" force it; "auto" (default) picks by expected reuse:
    1h for a long-lived orchestrator/session/chat prompt (a 1h write costs 2x input, 5m costs 1.25x, a read 0.1x: it pays when the
    prefix is read again after more than five minutes), 5m for short agents. `gap_s` = the expected seconds between reuses, when
    known: over five minutes and under an hour means 1h. Unknown use = 5m. cost(..., ttl) prices the write accordingly."""
    v = str(((cfg or {}).get("online") or {}).get("cloud_cache_ttl") or "auto").strip().lower()
    if v in ("5m", "1h"):
        return v
    if gap_s is not None:
        try:
            return "1h" if 300 < float(gap_s) < 3600 else "5m"
        except (TypeError, ValueError):
            pass
    return "1h" if str(use or "").lower() in LONG_LIVED else "5m"


def _ttl(cfg, use=None):
    return choose_ttl(cfg, use if use is not None else (getattr(_HERE, "ttl_use", None) or ("chat" if getattr(_HERE, "on", False) else None)))


# ---- read-only agents run on a cheaper model (online.readonly_model = auto | same)
CHEAP = {"sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}


def readonly_model(cfg, model, readonly, tiny=False):
    """-> (model to use, why or ""). A model the person NAMED is never changed. With none named and online.readonly_model = auto
    (default), a read-only agent (no write tools) gets Sonnet and a tiny lookup gets Haiku; "same" leaves the choice to the default."""
    if model and str(model).strip() not in ("", "inherit", "default"):
        return model, ""
    if str(((cfg or {}).get("online") or {}).get("readonly_model") or "auto").strip().lower() != "auto" or not readonly:
        return model, ""
    pick = CHEAP["haiku"] if tiny else CHEAP["sonnet"]
    return pick, ("a tiny read-only lookup" if tiny else "a read-only agent") + f" runs on {pick} (online.readonly_model = same keeps the default)"


def _cache_control(ttl):
    return {"type": "ephemeral", "ttl": "1h"} if ttl == "1h" else {"type": "ephemeral"}


# ---- the daily spend cap, across every Claude call on this machine
# data_dir()/cloud_spend.json: {"YYYY-MM-DD": usd spent, ..., "_held": {hold id: {"usd", "pid", "until", "what"}}}.
# A hold is money set aside BEFORE a request (a Claude Code run's budget, a fleet agent's, a Managed Agent session's estimate, a
# batch's worst case, one chat call's worst case); it counts against today's cap until `settle` swaps it for what was really spent.
# Every read-modify-write takes the in-process lock AND an OS file lock (cloud_spend.json.lock), so two processes never lose a write.
_SPEND_LOCK = _th.RLock()
_HELD_KEY = "_held"
HOLD_HOURS = 12                      # a hold whose owner never settled it (a crash) stops counting after this long
_FILE_LOCK_WAIT_S = 10.0


def _spend_file():
    import os
    import arch_core
    return os.path.join(arch_core.data_dir(), "cloud_spend.json")


def _today():
    return time.strftime("%Y-%m-%d")


def _read_spend():
    import json
    for i in range(3):                                               # a write being swapped in on Windows: read again shortly
        try:
            with open(_spend_file(), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except PermissionError:
            time.sleep(0.02 * (i + 1))
        except (OSError, ValueError):
            return {}
    return {}


def _held(d):
    h = d.get(_HELD_KEY) if isinstance(d, dict) else None
    return h if isinstance(h, dict) else {}


def _day_total(d):
    try:
        return float(d.get(_today()) or 0.0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def spent_today():
    """What Claude has really cost today (settled spend only; money on hold is in `held_today`)."""
    return _day_total(_read_spend())


def _pid_alive(pid):
    """True unless the process is known to be gone. Never signals the process (os.kill(pid, 0) TERMINATES it on Windows)."""
    import os
    import sys
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        if sys.platform.startswith("win"):
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, pid)                    # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return ctypes.GetLastError() == 5                    # access denied: it exists; anything else: gone
            try:
                code = ctypes.c_ulong()
                ok = k.GetExitCodeProcess(h, ctypes.byref(code))
                return (not ok) or code.value == 259                 # STILL_ACTIVE
            finally:
                k.CloseHandle(h)
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True                                                  # cannot tell: keep the hold (the cautious side)


def _live_holds(d, now=None):
    """Holds that still count: not past their time, and their owner process still running (a pid of 0 = not tied to a process)."""
    now = time.time() if now is None else now
    out = {}
    for hid, h in _held(d).items():
        try:
            if float(h.get("until") or 0) < now:
                continue
            if h.get("pid") and not _pid_alive(h.get("pid")):
                continue
            out[hid] = h
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def held_today():
    """USD on hold right now (requests and runs in flight, across every process on this machine)."""
    return round(sum(float(h.get("usd") or 0) for h in _live_holds(_read_spend()).values()), 6)


class _FileLock:
    """The OS's own lock on cloud_spend.json.lock (msvcrt / fcntl, the pattern in arch_heavy.py), released by the OS on a crash."""
    def __enter__(self):
        import os
        self.f, self.locked = None, False
        p = _spend_file() + ".lock"
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            self.f = open(p, "a+b")
        except OSError as e:
            _spend_warn(f"could not open the spend lock ({e}); writing without it")
            return self
        t_end = time.time() + _FILE_LOCK_WAIT_S
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.locked = True
                return self
            except OSError:
                if time.time() > t_end:
                    _spend_warn("another process held the spend lock for 10 s; writing without it")
                    return self
                time.sleep(0.02)

    def __exit__(self, *a):
        import os
        try:
            if self.f and self.locked:
                if os.name == "nt":
                    import msvcrt
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            if self.f:
                self.f.close()
        return False


def _spend_warn(msg):
    import sys
    try:
        print(f"[arch_cloud] spend ledger: {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


_LAST_WRITE_ERROR = {"msg": ""}


def _write_spend(d):
    """Write the ledger whole, under a per-process/per-thread temp name, swapped in with retries (on Windows os.replace fails while
    any reader has the file open). -> True when it reached the disk; a failure is said on stderr, never passed over in silence."""
    import json
    import os
    p = _spend_file()
    days = sorted(k for k in d if k != _HELD_KEY)[-60:]              # keep the last 60 days
    out = {k: d[k] for k in days}
    if _held(d):
        out[_HELD_KEY] = _held(d)
    tmp = f"{p}.{os.getpid()}.{_th.get_ident()}.tmp"
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f)
    except OSError as e:
        _LAST_WRITE_ERROR["msg"] = str(e)
        _spend_warn(f"could not write {tmp}: {e}")
        return False
    err = None
    for _ in range(5):
        try:
            os.replace(tmp, p)
            return True
        except PermissionError as e:                                 # a reader has it open right now: wait 50 ms and try again
            err = e
            time.sleep(0.05)
        except OSError as e:
            err = e
            break
    try:
        os.remove(tmp)
    except OSError:
        pass
    _LAST_WRITE_ERROR["msg"] = str(err)
    _spend_warn(f"could not record spend in {p}: {err}")
    return False


def _mutate(fn):
    """Read-modify-write the ledger under both locks. fn(d) changes d in place and returns a result. -> (result, written)."""
    with _SPEND_LOCK:
        with _FileLock():
            d = _read_spend()
            res = fn(d)
            return res, _write_spend(d)


def _add_spend(usd):
    """Add what was really spent to today's total (keeps the last 60 days). A write that fails is said on stderr; it never fails the
    reply. -> True when recorded."""
    try:
        usd = float(usd or 0)
    except (TypeError, ValueError):
        return False
    if usd <= 0:
        return True

    def add(d):
        d[_today()] = round(_day_total(d) + usd, 6)
    return _mutate(add)[1]


class SpendRefused(LLMError, RuntimeError):
    """A cap would be passed: refused before any money moves. Is an LLMError (could not run) and a RuntimeError (the hub's 'refused')."""


def reserve(cfg, usd, what="Claude", hours=HOLD_HOURS, tied_to_process=True, check_only=False):
    """Set `usd` aside against today's cap (limits.cloud_usd_per_day) before the money is spent. Refuses (SpendRefused, plain words)
    when today's spend + every live hold + `usd` would pass the cap. -> the hold id, to hand to `settle` with what was really spent.
    `check_only` checks without holding anything (a fleet checks its whole bill, then every agent holds its own budget).
    `tied_to_process=False` keeps the hold after this process ends (a batch is paid for long after the command that sent it)."""
    import os
    try:
        usd = max(0.0, float(usd or 0))
    except (TypeError, ValueError):
        raise SpendRefused(f"{what}: the amount to set aside is not a number ({str(usd)[:20]!r})")
    day_cap = _day_cap(cfg)

    def take(d):
        live = _live_holds(d)
        d[_HELD_KEY] = live                                          # finished/dead holds are dropped while we are here
        spent, held = _day_total(d), sum(float(h.get("usd") or 0) for h in live.values())
        if spent + held + usd > day_cap + 1e-9:
            return None, (f"{what} would pass Claude's daily spend cap: ${spent:.2f} spent + ${held:.2f} in flight + ${usd:.2f} "
                          f"for this > ${day_cap:.2f} today (Settings > Claude spend per day)")
        if check_only:
            return "", ""
        hid = f"h{os.getpid()}_{_th.get_ident()}_{time.time_ns()}"
        live[hid] = {"usd": round(usd, 6), "pid": os.getpid() if tied_to_process else 0,
                     "until": time.time() + float(hours) * 3600, "what": str(what)[:80], "t": time.time()}
        return hid, ""
    (hid, why), written = _mutate(take)
    if hid is None:
        raise SpendRefused(why)
    if not check_only and not written:                               # the hold never reached the disk: the cap could not hold it
        raise SpendRefused(f"{what}: could not record the spend hold ({_LAST_WRITE_ERROR['msg'][:120]}); refused so the daily cap holds")
    return hid


def settle(hold_id, actual_usd):
    """Swap a hold for what was really spent (0 when nothing was). Safe to call twice: the second call adds nothing.
    -> True when the actual spend was recorded."""
    try:
        actual = max(0.0, float(actual_usd or 0))
    except (TypeError, ValueError):
        actual = 0.0

    with _SPEND_LOCK:
        if hold_id and hold_id in _SETTLED:
            return True
        if hold_id:
            _SETTLED.add(hold_id)

    def swap(d):
        h = _held(d)
        if hold_id and hold_id in h:
            h.pop(hold_id, None)
            d[_HELD_KEY] = h
        if actual > 0:                                               # what was spent is spent, even when the hold had lapsed
            d[_today()] = round(_day_total(d) + actual, 6)
    return _mutate(swap)[1]


_SETTLED = set()


def worst_case_usd(model, input_chars, max_tokens, fast=False, searches=0):
    """The most one call can cost: every input token at the cache-write price (1.25x), plus `max_tokens` of output."""
    pin, pout = PRICES.get(api_model(model), PRICES[DEFAULT_MODEL])
    est_in = int(input_chars or 0) / 3.0 + 50                        # ~3 characters a token is generous for English and code
    usd = est_in * pin * 1.25 / 1e6 + int(max_tokens or 0) * pout / 1e6
    return usd * (2.0 if fast else 1.0) + searches * WEB_SEARCH_USD


def tokens_affordable(model, input_chars, budget_usd, fast=False, searches=0):
    """How many output tokens `budget_usd` pays for after the worst-case input. Can be 0 or less."""
    pin, pout = PRICES.get(api_model(model), PRICES[DEFAULT_MODEL])
    left = float(budget_usd) / (2.0 if fast else 1.0) - searches * WEB_SEARCH_USD / (2.0 if fast else 1.0)
    left -= (int(input_chars or 0) / 3.0 + 50) * pin * 1.25 / 1e6
    return int(left * 1e6 / pout) if pout else 0


# The per-run (per-process) cap, reserved the same way under _SPEND_LOCK: calls in flight together all count.
_RUN_HELD = {"usd": 0.0}
MIN_REPLY_TOKENS = 1024


def _run_msg(cfg):
    cap = _cap(cfg)
    return f"Claude budget for this run is used up (${_SPENT['usd']:.2f} of ${cap:.2f}; Settings > Claude spend per run)"


def _day_msg(cfg, today):
    return f"Claude's daily spend cap is reached (${today:.2f} of ${_day_cap(cfg):.2f} today; Settings > Claude spend per day)"


def _caps_reached(cfg):
    """Raise LLMError when either cap is already used up by what was spent (holds are weighed in `_hold_for_call`)."""
    if _SPENT["usd"] >= _cap(cfg):
        raise LLMError(_run_msg(cfg))
    today = spent_today()
    if today >= _day_cap(cfg):
        raise LLMError(_day_msg(cfg, today))


def _hold_for_call(cfg, model, input_chars, max_tokens, fast=False, searches=0):
    """Before one request: weigh what is spent AND what calls in flight have set aside, clamp max_tokens to what is left of the
    tighter cap, and hold the worst case (per run in memory, per day in the ledger). -> (hold id, worst usd, max_tokens).
    Refuses (LLMError) when what is left cannot pay for MIN_REPLY_TOKENS of answer."""
    cap, day_cap = _cap(cfg), _day_cap(cfg)
    with _SPEND_LOCK:
        if _SPENT["usd"] >= cap:
            raise LLMError(_run_msg(cfg))
        d = _read_spend()
        today = _day_total(d)
        if today >= day_cap:
            raise LLMError(_day_msg(cfg, today))
        run_left = cap - _SPENT["usd"] - _RUN_HELD["usd"]
        day_left = day_cap - today - sum(float(h.get("usd") or 0) for h in _live_holds(d).values())
        afford = tokens_affordable(model, input_chars, min(run_left, day_left), fast, searches)
        if afford < MIN_REPLY_TOKENS:
            if run_left <= day_left:
                raise LLMError(f"Claude budget for this run cannot pay for this call: ${_SPENT['usd']:.2f} spent + "
                               f"${_RUN_HELD['usd']:.2f} in flight of ${cap:.2f} (Settings > Claude spend per run)")
            raise LLMError(f"Claude's daily spend cap leaves too little for this call: ${today:.2f} spent + "
                           f"${day_cap - today - day_left:.2f} in flight of ${day_cap:.2f} today (Settings > Claude spend per day)")
        max_tokens = max(1, min(int(max_tokens), afford))
        worst = worst_case_usd(model, input_chars, max_tokens, fast, searches)
        try:
            hid = reserve(cfg, worst, what="this Claude call", hours=2)
        except SpendRefused as e:
            raise LLMError(str(e))
        _RUN_HELD["usd"] += worst
    return hid, worst, max_tokens


def _day_cap(cfg):
    try:
        return float(((cfg or {}).get("limits") or {}).get("cloud_usd_per_day", 5.0))
    except (TypeError, ValueError):
        return 5.0


# ---- the local reply cache: the same request to the same model at the same effort is answered once (efficiency, not training data)
CACHE_MAX_AGE = 30 * 86400
_PRUNED = {"done": False}


def _cache_on(cfg):
    """Off during eval, the self-tests (unless the cloud suite asks for it on purpose), ARCHITECT_NO_CACHE, and online.cloud_cache = off."""
    import os
    if os.environ.get("ARCHITECT_EVAL") == "1" or os.environ.get("ARCHITECT_NO_CACHE") == "1" or _testing():
        return False
    if os.environ.get("ARCHITECT_SELFTEST") == "1" and os.environ.get("ARCHITECT_CLOUD_CACHE_TEST") != "1":
        return False
    v = ((cfg or {}).get("online") or {}).get("cloud_cache", True)
    return not (v is False or str(v).strip().lower() in ("off", "false", "0", "no"))


def _cache_dir():
    import os
    import arch_core
    return os.path.join(arch_core.data_dir(), "cloud_cache")


def cache_key(model, effort, system, messages, fmt):
    import hashlib
    import json
    blob = json.dumps([api_model(model), effort, system or "", messages, fmt], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()


def _cache_get(key):
    import json
    import os
    p = os.path.join(_cache_dir(), key + ".json")
    try:
        if time.time() - os.path.getmtime(p) > CACHE_MAX_AGE:
            os.remove(p)
            return None
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and isinstance(d.get("text"), str) else None
    except (OSError, ValueError):
        return None


def _cache_put(key, entry):
    import json
    import os
    d = _cache_dir()
    try:
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, key + f".{os.getpid()}.{_th.get_ident()}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entry, f)
        os.replace(tmp, os.path.join(d, key + ".json"))
        if not _PRUNED["done"]:                                     # once per process: drop entries past their age
            _PRUNED["done"] = True
            now = time.time()
            for de in os.scandir(d):
                try:
                    if de.name.endswith(".json") and now - de.stat().st_mtime > CACHE_MAX_AGE:
                        os.remove(de.path)
                except OSError:
                    pass
    except OSError:
        pass


def chat(model, messages, options=None, think=None, keep_alive=None, timeout=900, fmt=None, on_token=None, cfg=None, client=None):
    """One streamed Messages API call. Same return shape as the local transports. `client` is for tests (a fake SDK client)."""
    role = getattr(_HERE, "role", None)                              # set by Engine.model_for for this call; read once, even if refused
    _HERE.role = None
    ok, why = allowed(cfg)
    if not ok:
        raise LLMError(why)
    eff = getattr(_HERE, "effort", None) or role_effort(role) or "high"   # an explicit choice wins, then the role's, then high
    system = "\n\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
    plain = [{"role": m["role"], "content": str(m.get("content") or "")} for m in messages if m.get("role") in ("user", "assistant")]
    web = web_wanted(model, cfg, getattr(_HERE, "web", False))
    use_cache = _cache_on(cfg) and not web                           # a looked-up answer is about today: never replayed from the cache
    ckey = cache_key(model, eff, system, plain, fmt) if use_cache else None
    hit = _cache_get(ckey) if ckey else None
    if hit:                                                          # asked before: the same answer, no request, no cost
        txt = hit["text"]
        if txt and on_token:
            on_token(txt)
        sink = getattr(_HERE, "sink", None)
        if txt and sink:
            sink(txt)
        rec = {"label": (getattr(_HERE, "label", None) or "answer") + " (cached)", "model": api_model(model), "effort": eff, "in": 0,
               "out": 0, "cached": 0, "cache_write": 0, "local_cache": True, "usd": 0.0, "secs": 0.0,
               "thinking": (hit.get("thinking") or "")[:4000], "text": txt[:4000]}
        return {"calls": [rec], "text": txt, "thinking": hit.get("thinking") or "", "gen_tokens": hit.get("gen_tokens") or 0,
                "prompt_tokens": 0, "cached_tokens": None, "done_reason": hit.get("done_reason") or "stop", "tok_s": None,
                "prefill_tok_s": None, "load_s": None, "transport": "claude", "usd": 0.0, "cached": True}
    _caps_reached(cfg)                                               # the cheap early refusal, before a client is even made
    if client is None:
        try:
            import anthropic
        except ImportError:
            raise LLMError("Claude needs the Anthropic SDK: run  pip install anthropic  (only needed for the cloud tier)")
        if isinstance(timeout, dict):                                # the engine passes {"connect":..,"first":..,"total":..}
            timeout = timeout.get("total") or max([v for v in timeout.values() if isinstance(v, (int, float))] or [900])
        try:
            secs = float(timeout or 900)
        except (TypeError, ValueError):
            secs = 900.0
        client = anthropic.Anthropic(timeout=max(60.0, secs))
    ttl = _ttl(cfg)
    msgs = [dict(m) for m in plain]
    if msgs and msgs[0]["role"] == "user" and len(msgs[0]["content"]) > 3000:     # the project context: shared by every agent of a team
        msgs[0] = {"role": "user", "content": [{"type": "text", "text": msgs[0]["content"], "cache_control": _cache_control(ttl)}]}
    if not msgs or msgs[0]["role"] != "user":
        msgs.insert(0, {"role": "user", "content": "(continue)"})
    while msgs and msgs[-1]["role"] == "assistant":                  # no prefill on current models
        msgs.pop()
    want = int((options or {}).get("num_predict") or 16000)
    kw_thinking = {"type": "adaptive", "display": "summarized"}       # a readable summary of Claude's reasoning, shown under "Show work"
    kw = dict(model=api_model(model), thinking=kw_thinking, max_tokens=max(1024, min(want * 2, 64000)) if eff not in ("xhigh", "max") else 64000, messages=msgs,
              output_config={"effort": eff})
    if system:
        kw["system"] = [{"type": "text", "text": system, "cache_control": _cache_control(ttl)}]   # cached: same prefix next call = ~0.1x price
    if isinstance(fmt, dict):                                        # a JSON schema: structured output
        kw["output_config"] = {"effort": eff, "format": {"type": "json_schema", "schema": fmt}}
    elif fmt == "json":
        kw["system"] = (system + "\n\n" if system else "") + "Answer with JSON only."
    if web:
        kw["tools"] = [dict(WEB_TOOL)]
    t0 = time.time()
    text, first = [], None
    thinking = []
    # fast mode doubles the price: only when a person is waiting (a message they sent from the chat), never for unattended runs
    fast = (bool(((cfg or {}).get("online") or {}).get("cloud_fast")) and api_model(model) in FAST_MODELS and getattr(_HERE, "on", False)
            and not getattr(_HERE, "nofast", False) and hasattr(getattr(client, "beta", None), "messages"))
    pt = gt = cr = cw = 0
    searches, queries, web_errors = 0, [], []
    in_chars = len(system) + sum(len(m["content"]) for m in plain)
    max_searches = WEB_TOOL["max_uses"] * 3 if web else 0            # up to two pause_turn resumes, each re-sending the input
    hid, worst, kw["max_tokens"] = _hold_for_call(cfg, model, in_chars * (3 if web else 1), kw["max_tokens"], fast, max_searches)
    released = [False]

    def _release(spent_usd):
        """Swap this call's hold for what it really cost (per run and per day). Once."""
        if released[0]:
            return
        released[0] = True
        with _SPEND_LOCK:
            _RUN_HELD["usd"] = max(0.0, _RUN_HELD["usd"] - worst)
            _SPENT["usd"] += spent_usd
        settle(hid, spent_usd)

    def _so_far():
        return cost(model, pt, gt, cr, cw, ttl) * (2.0 if fast else 1.0) + searches * WEB_SEARCH_USD
    try:
        for _turn in range(3):                                       # a searching reply may pause (pause_turn) and is resumed, at most twice
            try:
                if fast:                                             # fast mode: up to ~2.5x quicker output at 2x the price (Opus only)
                    stream_cm = client.beta.messages.stream(**kw, speed="fast", betas=["fast-mode-2026-02-01"])
                else:
                    stream_cm = client.messages.stream(**kw)
            except Exception as e:                                   # a schema the API will not take: ask again without it
                bad = "400" in str(e) or "invalid_request" in str(e)
                if bad and "format" in kw.get("output_config", {}):
                    kw["output_config"] = {"effort": eff}
                    stream_cm = client.messages.stream(**kw)
                elif bad and "tools" in kw and ("web_search" in str(e) or "tool" in str(e)):   # search not offered here: answer without it
                    kw.pop("tools", None)
                    web_errors.append("tool_not_accepted")
                    stream_cm = client.messages.stream(**kw)
                elif bad and ("effort" in str(e) or "thinking" in str(e)):   # a model that does not take effort/adaptive thinking
                    kw.pop("output_config", None)
                    kw.pop("thinking", None)
                    stream_cm = client.messages.stream(**kw)
                else:
                    raise
            with stream_cm as stream:
                src = iter(stream) if hasattr(type(stream), "__iter__") else stream.text_stream   # real SDK: events (text + thinking); fakes: text
                for ev in src:
                    kind = getattr(ev, "type", None)
                    if isinstance(ev, str):
                        kind, piece = "text", ev
                    elif kind == "text":
                        piece = getattr(ev, "text", "") or ""
                    elif kind == "thinking":
                        thinking.append(getattr(ev, "thinking", "") or "")
                        continue
                    else:
                        continue
                    if first is None:
                        first = time.time()
                    text.append(piece)
                    if on_token:
                        on_token(piece)
                    sink = getattr(_HERE, "sink", None)
                    if sink:
                        sink(piece)
                final = stream.get_final_message()
            u = getattr(final, "usage", None)
            pt += getattr(u, "input_tokens", 0) or 0
            gt += getattr(u, "output_tokens", 0) or 0
            cr += getattr(u, "cache_read_input_tokens", 0) or 0
            cw += getattr(u, "cache_creation_input_tokens", 0) or 0
            if "tools" in kw:
                n, qs, errs = web_report(final)
                searches, queries, web_errors = searches + n, queries + qs, web_errors + errs
            if getattr(final, "stop_reason", None) != "pause_turn" or "tools" not in kw:
                break
            # the server paused its own search loop: send the turn back as it stands and let it continue
            kw["messages"] = list(kw["messages"]) + [{"role": "assistant", "content": getattr(final, "content", None) or []}]
    except LLMError:
        _release(_so_far())
        raise
    except Exception as e:
        _release(_so_far())                                          # what finished turns cost is counted; the rest of the hold is freed
        if fast:                                                     # fast mode refused (its own rate limit): same call at normal speed
            _HERE.nofast = True
            try:
                return chat(model, messages, options, think, keep_alive, timeout, fmt, on_token, cfg=cfg, client=client)
            finally:
                _HERE.nofast = False
        raise LLMError(f"Claude did not answer: {type(e).__name__}: {str(e)[:300]}")
    except BaseException:
        _release(_so_far())
        raise
    stop = getattr(final, "stop_reason", None)
    usd = _so_far()
    _release(usd)
    if stop == "refusal":                                            # a refusal is still billed for its tokens
        raise LLMError("Claude declined this request (refusal); treated as could-not-run, not as an answer")
    _SPENT["calls"] += 1
    if ckey and stop != "max_tokens" and text:                      # a cut-off reply is never kept
        _cache_put(ckey, {"t": time.time(), "model": api_model(model), "effort": eff, "text": "".join(text),
                          "thinking": "".join(thinking)[:20000], "gen_tokens": gt, "done_reason": "stop"})
    secs = max(time.time() - (first or t0), 1e-6)
    rec = {"label": getattr(_HERE, "label", None) or "answer", "model": api_model(model), "effort": eff, "in": pt + cr + cw, "out": gt,
           "cached": cr, "cache_write": cw,
           "usd": round(usd, 5), "secs": round(time.time() - t0, 1), "thinking": "".join(thinking)[:4000], "text": "".join(text)[:4000]}
    if web:                                                          # Show work lists the lookups (and any the tool could not do)
        rec.update(web_searches=searches, web_queries=queries[:10], web_errors=web_errors[:10])
    return {"calls": [rec], "text": "".join(text), "thinking": "".join(thinking), "gen_tokens": gt, "prompt_tokens": pt, "cached_tokens": None,
            "done_reason": "length" if stop == "max_tokens" else "stop", "tok_s": round(gt / secs, 1) if gt else None,
            "prefill_tok_s": None, "load_s": None, "transport": "claude", "usd": round(usd, 5), "web_searches": searches}
