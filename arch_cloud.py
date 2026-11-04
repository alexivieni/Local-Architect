"""Claude as an optional cloud tier: the last escalation step after the local heavy model, and only when the person turned it on.

Off by default and invisible when off: the Anthropic SDK is imported only inside `chat()`, which is only reached when
`online.enabled` is true AND `online.cloud_tier` is "escalation" AND a `claude:` model is set as `tiers.cloud`. A fresh install
stays standard-library only and never opens a connection off this machine.

What stays true with it on:
  * code still decides: Claude's reply goes through the same apply/tests/gates as any local reply;
  * a refusal is "could not run" (LLMError), never a pass;
  * spend is capped per process (`limits.cloud_usd_per_run`), checked BEFORE each request;
  * the key is never stored by the app: the SDK reads ANTHROPIC_API_KEY (or an `ant auth login` profile) itself;
  * replies are tagged transport "claude", and arch_dataset leaves them out of training data (Anthropic's terms restrict
    using outputs to train competing models).
"""
import time

from arch_core import LLMError

PREFIX = "claude:"
DEFAULT_MODEL = "claude-opus-5-5"
PRICES = {                       # USD per million tokens (input, output); first-party API rates, 2026-10
    "claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.0, 50.0), "claude-opus-5": (5.0, 25.0),
}
_SPENT = {"usd": 0.0, "calls": 0}
import threading as _th
_HERE = _th.local()          # set while ONE chat reply the person explicitly sent to Claude is being answered


class asked_for:
    """`with arch_cloud.asked_for():` -- the person chose Claude for this message, so the Use Claude mode is not needed (online
    features must still be on). Thread-local: it never leaks into a run or another chat."""
    def __init__(self, sink=None, effort=None):
        self.sink = sink                     # gets each piece of text as it streams (the chat shows Claude typing)
        self.effort = effort if effort in EFFORTS else None
    def __enter__(self):
        _HERE.on, _HERE.sink, _HERE.effort = True, self.sink, self.effort
    def __exit__(self, *a):
        _HERE.on, _HERE.sink, _HERE.effort = False, None, None
        return False


EFFORTS = ("low", "medium", "high", "xhigh", "max")


def effort_for(text, chosen=None):
    """'ultrathink' (or 'think harder') anywhere in the message = max effort; else what the person picked; else high."""
    import re as _re
    if _re.search(r"\bultra-?think\b|\bthink (?:harder|really hard)\b", str(text or ""), _re.I):
        return "max"
    return chosen if chosen in EFFORTS else "high"


def is_cloud(model):
    return str(model or "").startswith(PREFIX)


def api_model(model):
    m = str(model or "")[len(PREFIX):].strip() if is_cloud(model) else str(model or "")
    return m or DEFAULT_MODEL


def allowed(cfg):
    """-> (True, "") when a cloud call may go out now, else (False, plain reason)."""
    import os
    if os.environ.get("ARCHITECT_EVAL") == "1":                 # benchmarks measure YOUR models: a cloud rescue would fake the score
        return False, "Claude is never used during eval (it would measure Claude, not your models)"
    on = (cfg or {}).get("online") or {}
    if not on.get("enabled"):
        return False, "Claude is off: online features are off (Settings > Allow online features)"
    if (on.get("cloud_tier") or "off") not in ("escalation", "chat", "all") and not getattr(_HERE, "on", False):
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
            "model": api_model(chosen_model(cfg)), "spent_usd": round(_SPENT["usd"], 4)}


def mode(cfg):
    """off | escalation | chat | all -- and "off" whenever a cloud call is not allowed right now."""
    return ((cfg or {}).get("online") or {}).get("cloud_tier", "off") if cloud_model(cfg) else "off"


def cost(model, prompt_tokens, gen_tokens):
    pin, pout = PRICES.get(api_model(model), PRICES[DEFAULT_MODEL])
    return (prompt_tokens or 0) * pin / 1e6 + (gen_tokens or 0) * pout / 1e6


def spent():
    return dict(_SPENT)


def _cap(cfg):
    try:
        return float(((cfg or {}).get("limits") or {}).get("cloud_usd_per_run", 0.5))
    except (TypeError, ValueError):
        return 0.5


def chat(model, messages, options=None, think=None, keep_alive=None, timeout=900, fmt=None, on_token=None, cfg=None, client=None):
    """One streamed Messages API call. Same return shape as the local transports. `client` is for tests (a fake SDK client)."""
    ok, why = allowed(cfg)
    if not ok:
        raise LLMError(why)
    cap = _cap(cfg)
    if _SPENT["usd"] >= cap:
        raise LLMError(f"Claude budget for this run is used up (${_SPENT['usd']:.2f} of ${cap:.2f}; Settings > Claude spend per run)")
    if client is None:
        try:
            import anthropic
        except ImportError:
            raise LLMError("Claude needs the Anthropic SDK: run  pip install anthropic  (only needed for the cloud tier)")
        client = anthropic.Anthropic(timeout=float(timeout or 900))
    system = "\n\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
    msgs = [{"role": m["role"], "content": str(m.get("content") or "")} for m in messages if m.get("role") in ("user", "assistant")]
    if not msgs or msgs[0]["role"] != "user":
        msgs.insert(0, {"role": "user", "content": "(continue)"})
    while msgs and msgs[-1]["role"] == "assistant":                  # no prefill on current models
        msgs.pop()
    want = int((options or {}).get("num_predict") or 16000)
    eff = getattr(_HERE, "effort", None) or "high"
    kw = dict(model=api_model(model), max_tokens=max(1024, min(want * 2, 64000)) if eff not in ("xhigh", "max") else 64000, messages=msgs,
              output_config={"effort": eff})
    if system:
        kw["system"] = system
    if isinstance(fmt, dict):                                        # a JSON schema: structured output
        kw["output_config"] = {"effort": eff, "format": {"type": "json_schema", "schema": fmt}}
    elif fmt == "json":
        kw["system"] = (system + "\n\n" if system else "") + "Answer with JSON only."
    t0 = time.time()
    text, first = [], None
    try:
        try:
            stream_cm = client.messages.stream(**kw)
        except Exception as e:                                       # a schema the API will not take: ask again without it
            if "format" in kw.get("output_config", {}) and ("400" in str(e) or "invalid_request" in str(e)):
                kw["output_config"] = {"effort": eff}
                stream_cm = client.messages.stream(**kw)
            else:
                raise
        with stream_cm as stream:
            for piece in stream.text_stream:
                if first is None:
                    first = time.time()
                text.append(piece)
                if on_token:
                    on_token(piece)
                sink = getattr(_HERE, "sink", None)
                if sink:
                    sink(piece)
            final = stream.get_final_message()
    except LLMError:
        raise
    except Exception as e:
        raise LLMError(f"Claude did not answer: {type(e).__name__}: {str(e)[:300]}")
    stop = getattr(final, "stop_reason", None)
    if stop == "refusal":
        raise LLMError("Claude declined this request (refusal); treated as could-not-run, not as an answer")
    u = getattr(final, "usage", None)
    pt, gt = getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0
    usd = cost(model, pt, gt)
    _SPENT["usd"] += usd
    _SPENT["calls"] += 1
    secs = max(time.time() - (first or t0), 1e-6)
    return {"text": "".join(text), "thinking": "", "gen_tokens": gt, "prompt_tokens": pt, "cached_tokens": None,
            "done_reason": "length" if stop == "max_tokens" else "stop", "tok_s": round(gt / secs, 1) if gt else None,
            "prefill_tok_s": None, "load_s": None, "transport": "claude", "usd": round(usd, 5)}
