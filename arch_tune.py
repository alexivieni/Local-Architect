"""Calibration helpers: keep every model call inside its context window and give every call a timeout that fits
the model that is actually answering -- a 2B on a fast GPU should give up in seconds, a 32B split across RAM
should be given minutes. Standard library only; pure functions plus one small JSON file of measured speeds.

Why this exists (all three are silent failures on local setups):
  * Ollama TRUNCATES a prompt that does not fit `num_ctx` and still answers 200 OK. The front of the prompt
    (the role's system prompt, the edit protocol) is what gets dropped, so the model keeps sounding fluent but
    stops following its instructions. The reply's `prompt_eval_count` is the only evidence.
  * A flat 900 s timeout is far too slow for a hung small model and can be too short for a big one.
  * `context_chars` and `num_predict` were not tied to `num_ctx`, so the defaults could overflow the window.

What it does:
  * learns, per model, chars-per-token, tokens/s (generation) and tokens/s (prompt processing) from real calls;
  * `context_chars_for()` sizes the prompt budget from the window, the reply reserve and the learned ratio;
  * `clamp_num_predict()` never lets prompt + reply exceed the window;
  * `overflow()` says whether a finished call hit the ceiling (truncated, or no room left to answer);
  * `timeouts_for()` derives first-token, idle and total timeouts from the learned speeds.
Nothing here guesses silently: unknown speeds fall back to conservative values and the caller can still pin a
fixed timeout with `limits.timeout_mode = fixed`.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CPT = 3.0                 # characters per token for mixed code/prose when nothing has been measured
FIXED_OVERHEAD_TOKENS = 1500      # role system prompt + edit protocol + task text around the project context
MIN_REPLY_TOKENS = 256
REPLY_ROOM_TOKENS = 2048          # room a call keeps for its answer before the prompt is trimmed (a multi-file reply needs ~1.5-2k)
EST_MARGIN = 1.15                 # the chars-per-token estimate runs low on code: fit with this much to spare


def cut_context(text, keep):
    """Project context cut to about `keep` characters at a section boundary, dropping from the end: sections are ordered by relevance
    (memory, the files that matter, then outlines), so the least useful go first. Says how much was left out."""
    if len(text) <= keep:
        return text
    secs = text.split("\n\n=== ")
    out = secs[0][:keep]
    for s in secs[1:]:
        if len(out) + len(s) + 6 > keep:
            break
        out += "\n\n=== " + s
    return out + f"\n\n(... {len(text) - len(out)} more characters of project context left out, to leave room for the answer)"
NEAR_FULL = 0.97                  # prompt this close to the window = truncated or no room to answer
UNKNOWN_PREFILL_TOK_S = 40.0      # assume a slow (CPU-ish) machine until measured
UNKNOWN_GEN_TOK_S = 4.0


def data_dir():
    """Same rule as arch_core.data_dir (kept here so this module never imports arch_core)."""
    return (os.environ.get("ARCHITECT_DATA_DIR")
            or os.path.join(os.path.abspath(os.environ.get("ARCHITECT_HOME") or HERE), "hub_data"))


def stats_path():
    return os.path.join(data_dir(), "model_stats.json")


def load_stats():
    try:
        with open(stats_path(), "r", encoding="utf-8") as f:
            s = json.load(f)
            return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def save_stats(stats):
    p = stats_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=1)
        os.replace(tmp, p)
        return True
    except OSError:
        return False


def _ema(old, new, w=0.3):
    return new if not old else round(old * (1 - w) + new * w, 3)


def observe(stats, model, *, prompt_chars=None, prompt_tokens=None, num_ctx=None, gen_tok_s=None,
            prefill_tok_s=None, load_s=None):
    """Fold one finished call into the per-model stats dict (mutates and returns it). Ratios are only learned
    from prompts that were NOT near the window limit, because a truncated prompt would teach a false ratio."""
    m = stats.setdefault(model, {})
    if prompt_chars and prompt_tokens and prompt_tokens > 50 and (not num_ctx or prompt_tokens < NEAR_FULL * num_ctx):
        m["cpt"] = _ema(m.get("cpt"), prompt_chars / prompt_tokens)
    if gen_tok_s and gen_tok_s > 0:
        m["gen_tok_s"] = _ema(m.get("gen_tok_s"), float(gen_tok_s))
    if prefill_tok_s and prefill_tok_s > 0:
        m["prefill_tok_s"] = _ema(m.get("prefill_tok_s"), float(prefill_tok_s))
    if load_s is not None:
        m["load_s"] = max(float(load_s), m.get("load_s") or 0.0) if load_s > 1 else m.get("load_s", 0.0)
    m["calls"] = int(m.get("calls") or 0) + 1
    return stats


def cpt_for(stats, model):
    v = (stats.get(model) or {}).get("cpt")
    return min(6.0, max(1.5, float(v))) if v else DEFAULT_CPT


def reply_reserve(num_ctx):
    """Tokens kept free for the answer when sizing the prompt budget."""
    return max(1024, int(num_ctx) // 4)


def context_chars_for(num_ctx, cpt=DEFAULT_CPT, configured=None):
    """Largest project-context size (characters) that still leaves room for the instructions and the answer.
    Never exceeds `configured` (the user's own limit) when one is given."""
    room = int(num_ctx) - reply_reserve(num_ctx) - FIXED_OVERHEAD_TOKENS
    chars = max(2000, int(room * cpt * 0.85))
    return min(chars, int(configured)) if configured else chars


def estimate_tokens(chars, cpt=DEFAULT_CPT):
    return int(chars / max(cpt, 1.0)) + 1


def clamp_num_predict(num_ctx, est_prompt_tokens, wanted):
    """-> (num_predict, clamped: bool). Keeps prompt + reply inside the window, but never below a usable floor."""
    room = int(num_ctx) - int(est_prompt_tokens) - 64
    if wanted <= room:
        return int(wanted), False
    return max(MIN_REPLY_TOKENS, room), True


def overflow(prompt_tokens, num_ctx, num_predict=0):
    """-> None if the prompt fit with room to answer; else 'truncated' (prompt at/over the window) or 'no_room'
    (it fit, but too little was left for even a short answer)."""
    if not prompt_tokens or not num_ctx:
        return None
    if prompt_tokens >= NEAR_FULL * num_ctx:
        return "truncated"
    if num_ctx - prompt_tokens < min(int(num_predict or MIN_REPLY_TOKENS), MIN_REPLY_TOKENS):
        return "no_room"
    return None


def grow_ctx(num_ctx, ceiling):
    """One step up for a retry after truncation: double, capped at `ceiling`. Returns num_ctx unchanged when
    already at the ceiling (the caller then reports instead of retrying)."""
    return min(int(ceiling), int(num_ctx) * 2) if int(num_ctx) < int(ceiling) else int(num_ctx)


def timeouts_for(stats_for_model, est_prompt_tokens, num_predict, ceiling, first_call=False):
    """-> {"first": s, "idle": s, "total": s}.
    first = wait for the first token (model load if cold + prompt processing, with 2x margin);
    idle  = longest silence allowed between tokens afterwards;
    total = wall clock for the whole call. All three are capped by `ceiling` (limits.request_timeout)."""
    s = stats_for_model or {}
    prefill = float(s.get("prefill_tok_s") or UNKNOWN_PREFILL_TOK_S)
    gen = float(s.get("gen_tok_s") or UNKNOWN_GEN_TOK_S)
    load = float(s.get("load_s") or 0.0)
    cold = max(load * 1.5, 60.0) if (first_call or not s.get("calls")) else 20.0
    first = cold + 2.0 * est_prompt_tokens / prefill + 15.0
    idle = 20.0 + 40.0 / max(gen, 0.2)
    total = first + 2.0 * max(int(num_predict), 1) / max(gen, 0.2) + 30.0
    cap = float(ceiling)
    clamp = lambda v, lo: round(min(cap, max(lo, v)), 1)
    return {"first": clamp(first, 45.0), "idle": clamp(idle, 30.0), "total": clamp(total, 60.0)}
