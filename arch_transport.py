"""The two ways a model is called, behind one function. Every caller sees the same reply dict whichever server answered.

    chat(model, messages, options, think, keep_alive, timeout, fmt, on_token, host) -> {
        "text", "thinking", "gen_tokens", "prompt_tokens", "cached_tokens", "done_reason", "tok_s", "prefill_tok_s", "load_s", "transport"}

  * OllamaTransport  -- Ollama's /api/chat (NDJSON stream). The original client, moved here unchanged in behaviour.
  * LlamaTransport   -- llama-server's OpenAI-compatible /v1/chat/completions (SSE). Adds exact prefix-cache accounting (`timings.cache_n`),
                        JSON-schema constrained output (`response_format: json_schema`), thinking on/off via `chat_template_kwargs`, and
                        `cache_prompt: true` so a repeated prefix is not re-read.

Which one answers is decided by `route(model, host)`: a remote host the person added is always Ollama; a model the local llama-server is
serving (arch_server.serving) goes to it; everything else is Ollama. Nothing here opens a connection off this machine unless `host`
names one of the person's own machines.

Timeouts are the engine's {"first", "idle", "total"} seconds (arch_tune.timeouts_for) or one number: wait up to `first` for the first
token, allow `idle` of silence between tokens, and `total` wall-clock for the whole reply.
"""
import json
import re
import time
import urllib.error
import urllib.request


from arch_core import LLMError   # one exception class for every caller, whichever server answered


def _swallow(where, ex):
    """Record a fault a caller deliberately swallows (arch_telemetry.swallow, rate-limited). Never raises."""
    try:
        import arch_telemetry
        arch_telemetry.swallow(where, ex)
    except Exception:                      # intentional: the recorder itself must never break the caller
        pass


def _timeouts(timeout):
    if isinstance(timeout, dict):
        return timeout["first"], timeout["idle"], timeout.get("total")
    return timeout, timeout, None


def _set_idle_timeout(resp, seconds):
    """Switch an open streaming response from the first-token wait to the shorter between-tokens limit (CPython http.client layout;
    if the layout differs the first-token limit simply stays in force, which is the safe direction)."""
    try:
        resp.fp.raw._sock.settimeout(seconds)
    except (AttributeError, OSError):
        pass


def _http_error_text(e):
    """The server's error text from an HTTP error body: Ollama sends {"error": "..."}; llama-server sends {"error": {"message": ...}}."""
    try:
        err = json.loads(e.read().decode()).get("error", "")
    except (ValueError, OSError, AttributeError):
        return ""
    if isinstance(err, dict):
        return str(err.get("message") or err.get("type") or err)
    return str(err or "")


# ------------------------------------------------------------------------------------------------------------ Ollama
class OllamaTransport:
    kind = "ollama"

    def __init__(self, host):
        self.host = host.rstrip("/")

    def chat(self, model, messages, options=None, think=None, keep_alive=None, timeout=900, fmt=None, on_token=None):
        if keep_alive in (None, ""):                              # one default everywhere: server.ollama_keep_alive (60s)
            import arch_core as core
            keep_alive = core.ollama_keep_alive()
        t_first, t_idle, t_total = _timeouts(timeout)
        attempt_think = think
        for attempt in range(3):
            body = {"model": model, "messages": messages, "stream": True, "keep_alive": keep_alive, "options": options or {}}
            if attempt_think is not None:
                body["think"] = attempt_think
            if fmt:
                body["format"] = fmt
            req = urllib.request.Request(self.host + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            try:
                text, thinking, last = [], [], {}
                t_start, got_first = time.time(), False
                with urllib.request.urlopen(req, timeout=t_first) as r:
                    for line in r:
                        line = line.strip()
                        if not line:
                            continue
                        if not got_first:
                            got_first = True
                            _set_idle_timeout(r, t_idle)
                        if t_total and time.time() - t_start > t_total:
                            raise LLMError(f"{model} is still generating after {int(t_total)}s (overall limit for this call). "
                                           "Raise Settings > Limits > Model reply timeout, or use a faster model or smaller context.")
                        j = json.loads(line)
                        if j.get("error"):
                            raise LLMError(j["error"])
                        m = j.get("message") or {}
                        if m.get("content"):
                            text.append(m["content"])
                            if on_token:
                                on_token(m["content"])
                        if m.get("thinking"):
                            thinking.append(m["thinking"])
                        if j.get("done"):
                            last = j
                ec, ed = last.get("eval_count"), last.get("eval_duration")
                pc, pd = last.get("prompt_eval_count"), last.get("prompt_eval_duration")
                ld = last.get("load_duration")
                return {"text": "".join(text), "thinking": "".join(thinking), "gen_tokens": ec or 0, "prompt_tokens": pc or 0, "cached_tokens": None,
                        "done_reason": last.get("done_reason"), "tok_s": round(ec / (ed / 1e9), 1) if ec and ed else None,
                        "prefill_tok_s": round(pc / (pd / 1e9), 1) if pc and pd and pc > 20 else None,
                        "load_s": round(ld / 1e9, 1) if ld is not None else None, "transport": "ollama"}
            except urllib.error.HTTPError as e:
                msg = _http_error_text(e)
                if isinstance(fmt, dict) and "format" in msg.lower():       # an older server that cannot take a schema: ask for plain JSON, validate in code
                    fmt = "json"
                    continue
                if attempt_think is not None and "think" in msg.lower():
                    attempt_think = bool(attempt_think) if isinstance(attempt_think, str) else None
                    continue
                raise LLMError(f"Ollama said: {msg or e}")
            except (urllib.error.URLError, OSError) as e:
                why = getattr(e, "reason", e)
                if isinstance(e, TimeoutError) or isinstance(why, TimeoutError):
                    wait = int(t_idle if got_first else t_first)
                    raise LLMError(f"No {'further output' if got_first else 'first token'} from {model} for {wait}s, so the call was stopped "
                                   f"({'the model went quiet mid-reply' if got_first else 'still loading or processing the prompt'}). "
                                   "Try a smaller context, a smaller model, or raise Settings > Limits > Model reply timeout "
                                   "(or set Timeout mode to fixed).")
                raise LLMError(f"Ollama is not reachable at {self.host} ({why}). Start the Ollama app.")
        raise LLMError("The model server kept rejecting the request.")


# ------------------------------------------------------------------------------------------------------------ llama-server
_OPT_MAP = {"temperature": "temperature", "top_p": "top_p", "top_k": "top_k", "repeat_penalty": "repeat_penalty", "seed": "seed",
            "min_p": "min_p", "presence_penalty": "presence_penalty", "frequency_penalty": "frequency_penalty"}


class LlamaTransport:
    """llama-server (llama.cpp), OpenAI-compatible endpoint, SSE stream. The server is started with --jinja so the model's own chat template
    applies; `num_ctx` is a launch flag there (not per request), so the caller's window is reported back as the server's."""
    kind = "llama"

    def __init__(self, host, ctx=None):
        self.host = host.rstrip("/")
        self.ctx = ctx

    def _body(self, model, messages, options, think, fmt):
        options = options or {}
        if think is False and _thinks_in_tags(model) and messages and messages[-1].get("role") == "user":
            messages = list(messages[:-1]) + [dict(messages[-1], content=messages[-1]["content"].rstrip() + "\n/no_think")]   # Qwen3-style soft switch; the per-request flag is not honoured by every template
        body = {"model": model, "messages": messages, "stream": True, "stream_options": {"include_usage": True}, "cache_prompt": True}
        if (options or {}).get("id_slot") is not None:          # same slot for a whole run, so its cached prompt is reused
            body["id_slot"] = int(options["id_slot"])
        for k, v in options.items():
            if k in _OPT_MAP and v is not None:
                body[_OPT_MAP[k]] = v
        if options.get("num_predict"):
            body["max_tokens"] = int(options["num_predict"])
        if think is not None:
            body["chat_template_kwargs"] = {"enable_thinking": bool(think)}
        if isinstance(fmt, dict):
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "reply", "schema": fmt, "strict": True}}
        elif fmt == "json":
            body["response_format"] = {"type": "json_object"}
        return body

    def chat(self, model, messages, options=None, think=None, keep_alive=None, timeout=900, fmt=None, on_token=None):
        t_first, t_idle, t_total = _timeouts(timeout)
        body = self._body(model, messages, options, think, fmt)
        for attempt in range(2):
            req = urllib.request.Request(self.host + "/v1/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
            try:
                text, thinking, usage, timings, finish = [], [], {}, {}, None
                t_start, got_first, t_token, n_tok = time.time(), False, None, 0
                with urllib.request.urlopen(req, timeout=t_first) as r:
                    for raw in r:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        if not got_first:
                            got_first, t_token = True, time.time()
                            _set_idle_timeout(r, t_idle)
                        if t_total and time.time() - t_start > t_total:
                            raise LLMError(f"{model} is still generating after {int(t_total)}s (overall limit for this call). "
                                           "Raise Settings > Limits > Model reply timeout, or use a faster model or smaller context.")
                        try:
                            j = json.loads(data)
                        except ValueError:
                            continue
                        if j.get("error"):
                            err = j["error"]
                            msg = str(err.get("message") if isinstance(err, dict) else err)
                            if text and re.search(r"context size|context length|exceeds the available context", msg, re.I):
                                # The window filled up mid-reply. Seen live: a multi-file answer ran past an 8192 window and the error
                                # ended the whole run. Return what was written as a reply cut off at its limit, which is what Ollama
                                # does; a block left unfinished is refused with "send one smaller change", the finished ones apply.
                                finish = "length"
                                usage = {"prompt_tokens": (err.get("n_prompt_tokens") if isinstance(err, dict) else None) or 0,
                                         "completion_tokens": n_tok}   # what it wrote still counts against the budgets
                                break
                            raise LLMError(msg)
                        for ch in j.get("choices") or []:
                            d = ch.get("delta") or {}
                            if d.get("content") or d.get("reasoning_content"):
                                n_tok += 1                       # one streamed delta is one token
                            if d.get("content"):
                                text.append(d["content"])
                                if on_token:
                                    on_token(d["content"])
                            if d.get("reasoning_content"):
                                thinking.append(d["reasoning_content"])
                            if ch.get("finish_reason"):
                                finish = ch["finish_reason"]
                        if j.get("usage"):
                            usage = j["usage"]
                        if j.get("timings"):
                            timings = j["timings"]
                pt = int(usage.get("prompt_tokens") or timings.get("prompt_n", 0) + timings.get("cache_n", 0) or 0)
                gt = int(usage.get("completion_tokens") or timings.get("predicted_n") or n_tok or 0)
                cached = timings.get("cache_n")
                if cached is None and timings.get("prompt_n") is not None and pt:
                    cached = max(0, pt - int(timings["prompt_n"]))
                tok_s = timings.get("predicted_per_second")
                pre = timings.get("prompt_per_second")
                first_s = round(t_token - t_start, 1) if t_token else None
                return {"text": "".join(text), "thinking": "".join(thinking), "gen_tokens": gt, "prompt_tokens": pt, "cached_tokens": cached,
                        "done_reason": {"stop": "stop", "length": "length"}.get(finish, finish), "tok_s": round(float(tok_s), 1) if tok_s else None,
                        "prefill_tok_s": round(float(pre), 1) if pre and (timings.get("prompt_n") or 0) > 20 else None,
                        # llama-server keeps the model resident and reports no load time at all, so `load_s` stays None ("nothing reported")
                        # rather than borrowing the first-token latency, which is prefill work and would be counted as a model reload.
                        "load_s": None, "transport": "llama", "first_token_s": first_s}
            except urllib.error.HTTPError as e:
                msg = _http_error_text(e) or str(e)
                if attempt == 0 and "response_format" in body and ("schema" in msg.lower() or "grammar" in msg.lower() or e.code == 400):
                    body.pop("response_format", None)                        # a build without schema support: plain text, validated in code
                    continue
                raise LLMError(f"llama-server said: {msg}")
            except (urllib.error.URLError, OSError) as e:
                why = getattr(e, "reason", e)
                if isinstance(e, TimeoutError) or isinstance(why, TimeoutError):
                    wait = int(t_idle if got_first else t_first)
                    raise LLMError(f"No {'further output' if got_first else 'first token'} from {model} for {wait}s, so the call was stopped "
                                   f"({'the model went quiet mid-reply' if got_first else 'still loading or processing the prompt'}).")
                raise LLMError(f"llama-server is not reachable at {self.host} ({why}). Run `python architect.py server start`.")
        raise LLMError("llama-server kept rejecting the request.")


_THINK_CACHE = {}


def _thinks_in_tags(model):
    """Does this model reason inside <think> tags (Qwen3 family and friends)? From its passport; cached per process."""
    if model in _THINK_CACHE:
        return _THINK_CACHE[model]
    style = None
    try:
        import arch_passport
        import arch_server
        srv = arch_server.serving(model)
        name = model
        if srv:                                                   # a tier alias: look the served model up by its real name
            st = arch_server.load_state().get("processes", {}).get(srv["tier"], {})
            name = st.get("name") or model
        p = arch_passport.load(name) or arch_passport.load(model)
        style = (p or {}).get("think_style")
    except Exception as _swx:
        _swallow("transport._thinks_in_tags", _swx)
        style = None
    _THINK_CACHE[model] = style == "tags" or bool(re.search(r"qwen3|deepseek-r1|qwq", model, re.I))
    return _THINK_CACHE[model]


# ------------------------------------------------------------------------------------------------------------ routing
def route(model, host=None, cfg=None):
    """-> a transport for this model. A named remote host is Ollama; a model the local llama-server serves goes there; else local Ollama."""
    import arch_core as core
    if host:
        return OllamaTransport(host)
    try:
        import arch_server
        srv = arch_server.serving(model, cfg)
    except Exception as _swx:
        _swallow("transport.route", _swx)
        srv = None
    if srv:
        return LlamaTransport(srv["url"], srv.get("ctx"))
    return OllamaTransport(core.ollama_host())


def chat(model, messages, options=None, think=None, keep_alive=None, timeout=900, fmt=None, on_token=None, host=None, cfg=None):
    if str(model or "").startswith("claude:"):          # the optional cloud tier (arch_cloud.py): refused unless the person turned it on
        import arch_cloud
        import arch_core as core
        return arch_cloud.chat(model, messages, options, think, keep_alive, timeout, fmt, on_token, cfg=cfg or core.load_config())
    return route(model, host, cfg).chat(model, messages, options, think, keep_alive, timeout, fmt, on_token)
