"""What a Claude message will cost, before it is sent: so the hub can say "this message will cost about $x".

Two ways to know the size of a prompt:
  * count()      -- asks Anthropic's token counter (client.messages.count_tokens). Exact for that model's tokenizer, free of charge
                    (it has its own rate limit), and it never sends the prompt to a model. It is still a request off this machine, so
                    it is refused whenever arch_cloud.allowed() says no (online features off, eval, the self-tests).
  * estimate_local() -- characters / 3.5, on this machine, no request. Labelled "estimate" everywhere it is shown.
estimate_cost() uses the first when it may and the second otherwise, and always says which (`source`).

Nothing here spends: a count is not a model call, so it is never added to arch_cloud's per-run or per-day spend. Standard library
only until count() is reached; the Anthropic SDK is imported there and nowhere else.
"""
import math

from arch_core import LLMError

CHARS_PER_TOKEN = 3.5          # local rule of thumb for English prose and code (estimate, not measured per model)
DEFAULT_OUT = 1500             # expected reply length when the caller gives none (a typical chat answer; adaptive thinking adds more)


def _text_of(content):
    """The text inside one message's content: a string, or a list of content blocks ({"type": "text", "text": ...})."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict):
                out.append(str(b.get("text") or b.get("content") or ""))
            else:
                out.append(str(getattr(b, "text", "") or ""))
        return "\n".join(out)
    return str(content or "")


def shape(system, messages):
    """-> (system_text, messages) in the form arch_cloud.chat() sends: every system message folded into `system`, only user and
    assistant turns kept, the first turn a user turn, no trailing assistant turn (no prefill on current models). A count taken on
    a different shape than the real call would be a count of a different prompt."""
    sys_parts = [str(system)] if system else []
    msgs = []
    for m in messages or []:
        role = m.get("role")
        if role == "system":
            sys_parts.append(_text_of(m.get("content")))
        elif role in ("user", "assistant"):
            c = m.get("content")
            msgs.append({"role": role, "content": c if isinstance(c, list) else _text_of(c)})
    if not msgs or msgs[0]["role"] != "user":
        msgs.insert(0, {"role": "user", "content": "(continue)"})
    while len(msgs) > 1 and msgs[-1]["role"] == "assistant":
        msgs.pop()
    return "\n\n".join(p for p in sys_parts if p), msgs


def estimate_local(system, messages):
    """Input tokens by characters / 3.5 (rounded up). No request; works with everything off."""
    s, msgs = shape(system, messages)
    chars = len(s) + sum(len(_text_of(m["content"])) for m in msgs)
    return int(math.ceil(chars / CHARS_PER_TOKEN)) if chars else 0


def count(model, system, messages, cfg=None, client=None, tools=None):
    """Exact input tokens for this prompt on this Claude model, from Anthropic's counter. -> int.
    Raises LLMError when a cloud request is not allowed now (arch_cloud.allowed) or the counter does not answer. `client` is for
    tests (a fake SDK client with messages.count_tokens)."""
    import arch_cloud
    ok, why = arch_cloud.allowed(cfg)
    if not ok:
        raise LLMError(why)
    s, msgs = shape(system, messages)
    if client is None:
        try:
            import anthropic
        except ImportError:
            raise LLMError("counting tokens needs the Anthropic SDK: pip install anthropic")
        client = anthropic.Anthropic(timeout=30.0)
    kw = {"model": arch_cloud.api_model(model), "messages": msgs}
    if s:
        kw["system"] = s
    if tools:
        kw["tools"] = list(tools)
    try:
        r = client.messages.count_tokens(**kw)
    except Exception as e:
        raise LLMError(f"Claude's token counter did not answer: {type(e).__name__}: {str(e)[:200]}")
    n = r.get("input_tokens") if isinstance(r, dict) else getattr(r, "input_tokens", None)
    try:
        return int(n)
    except (TypeError, ValueError):
        raise LLMError("Claude's token counter gave no input_tokens")


def estimate_cost(model, system, messages, expected_out=None, cfg=None, client=None, cached_prefix=0, tools=None):
    """What one call would cost, before it is sent. -> dict:
        model, input_tokens, expected_out, usd (no prompt cache), usd_cached (when `cached_prefix` tokens are read from the prompt
        cache at 0.1x; equals usd when 0), source ("counted" = Anthropic's counter, "estimate" = chars/3.5 on this machine), why
        (plain words when it fell back to the estimate).
    Output tokens are never known ahead: `expected_out` (default DEFAULT_OUT) is the caller's guess, and the price shown is only as
    good as that guess. Thinking tokens are billed as output."""
    import arch_cloud
    out = DEFAULT_OUT if expected_out is None else max(0, int(expected_out))
    why = ""
    try:
        n, source = count(model, system, messages, cfg=cfg, client=client, tools=tools), "counted"
    except LLMError as e:
        n, source, why = estimate_local(system, messages), "estimate", str(e)
    cached = max(0, min(int(cached_prefix or 0), n))
    usd = arch_cloud.cost(model, n, out)
    usd_cached = arch_cloud.cost(model, n - cached, out, cache_read=cached) if cached else usd
    return {"model": arch_cloud.api_model(model), "input_tokens": n, "expected_out": out, "usd": round(usd, 6),
            "usd_cached": round(usd_cached, 6), "source": source, "why": why}


def say(est):
    """One line for the hub: 'about $0.012 (3,400 tokens in, ~1,500 out; counted)'."""
    tag = "counted" if est.get("source") == "counted" else "estimate"
    return (f"about ${est.get('usd', 0):.3f} ({est.get('input_tokens', 0):,} tokens in, ~{est.get('expected_out', 0):,} out; {tag})")
