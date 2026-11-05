"""Why: every trace record, and every run, said in plain words a newcomer can follow.

    explain_event(ev)    -> one sentence for any trace/event record (call, step, check, mark, tool, escalation, verdict, hook,
                            flaky, best_of_n, postmortem, todo, route, undo, refusal, token, a broker decision, or anything else)
    explain_run(run_dir) -> the run as 5-12 sentences, in the order things happened (a list of strings)
    glossary(term)       -> the app's own words (tier, gate, not sure, escalation, council, crew, fast lane, fleet ...) in one or two
                            sentences; glossary() -> every entry

Every sentence is built from the record's own fields by code. No model is asked anything: an explanation must be exactly as
trustworthy as the record it explains, cost nothing, and work with every server stopped. A field that is missing is simply not
mentioned; nothing is guessed. Three outcomes stay three: a check that could not run is said as "could not run", never as a failure.
"""
import json
import os
import re

MIN_SENTENCES = 5
MAX_SENTENCES = 12

_NUM = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


# ------------------------------------------------------------------------------------------------ small words
def _n(x):
    """2 -> 'two' (up to ten), larger numbers as digits."""
    try:
        i = int(x)
    except (TypeError, ValueError):
        return str(x)
    return _NUM[i] if 0 <= i < len(_NUM) else str(i)


def _times(x):
    try:
        i = int(x)
    except (TypeError, ValueError):
        return ""
    return {1: "once", 2: "twice"}.get(i, f"{_n(i)} times")


def _plural(x, word, many=None):
    try:
        i = int(x)
    except (TypeError, ValueError):
        i = 2
    return f"{_n(i)} {word if i == 1 else (many or word + 's')}"


def _secs(s):
    try:
        s = float(s)
    except (TypeError, ValueError):
        return ""
    if s < 1:
        return "under a second"
    if s < 90:
        return f"{s:.0f} s" if s >= 10 else f"{s:.1f} s"
    return f"{int(s // 60)} min {int(s % 60)} s"


def _usd(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return ""
    if v <= 0:
        return ""
    return f"${v:.2f}" if v >= 0.01 else f"${v:.4f}"


def _join(items):
    items = [str(i) for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _clip(s, n=110):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 3].rstrip() + "..."


def _first_line(s, n=100):
    for ln in str(s or "").splitlines():
        ln = ln.strip()
        if ln and not set(ln) <= set("=-_*# "):
            return _clip(ln, n)
    return ""


def _sentence(s):
    s = " ".join(str(s or "").split()).strip()
    if not s:
        return ""
    s = s[0].upper() + s[1:]
    return s if s[-1] in ".!?" else s + "."


# ------------------------------------------------------------------------------------------------ models and tiers
TIER_WORDS = {"router": "the router (the small model that sorts requests)", "worker": "the worker (the model that writes most code)",
              "heavy": "the heavy model (the larger one, for hard steps)", "draft": "the draft model (it guesses ahead to speed the worker)",
              "cloud": "Claude (the optional cloud tier)"}


def is_claude(model):
    return str(model or "").startswith("claude:") or str(model or "").startswith("claude-")


def _size(model):
    m = str(model or "").lower()
    mm = re.search(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)\s*b\b", m.replace("-", " ").replace(":", " ").replace("_", " "))
    if not mm:
        mm = re.search(r"(\d+(?:\.\d+)?)b", m)
    return mm.group(1) if mm else ""


def is_moe(model):
    m = str(model or "").lower()
    return bool(re.search(r"\ba\d+(?:\.\d+)?b\b|[-_]a\d+(?:\.\d+)?b|moe|\d+x\d+b", m))


def short_model(model):
    """'qwen3:4b' -> 'the 4B', 'qwen3:30b-a3b' -> 'the MoE', 'claude:claude-opus-5-5' -> 'Claude'. For chains of models."""
    if not model:
        return "the model"
    if is_claude(model):
        return "Claude"
    if is_moe(model):
        return "the MoE"
    sz = _size(model)
    return f"the {sz.upper()}B" if sz else str(model)


def model_words(model):
    """A model as a newcomer reads it: 'the 4B model (qwen3:4b)', 'Claude (claude-opus-5-5)'."""
    if not model:
        return "the model"
    m = str(model)
    if is_claude(m):
        return f"Claude ({m.split(':', 1)[1] if m.startswith('claude:') else m})"
    sz = _size(m)
    if is_moe(m):
        return f"the {sz.upper()}B mixture-of-experts model ({m})" if sz else f"the mixture-of-experts model ({m})"
    return f"the {sz.upper()}B model ({m})" if sz else f"the model {m}"


# ------------------------------------------------------------------------------------------------ checks
GATE_WORDS = [
    (r"syntax|parses|compil", "the syntax check"),
    (r"fake", "the fake-code check (no stubs or placeholders passed off as work)"),
    (r"shrink", "the shrink guard (a file must not silently lose code)"),
    (r"import", "the missing-import check"),
    (r"question", "the question detector (the model must act, not ask)"),
    (r"interface", "the interface check (every promised function exists)"),
    (r"verif", "the verifier"),
    (r"lint|ruff|flake", "the linter"),
    (r"type", "the type check"),
    (r"secur|bandit|secret", "the security scan"),
    (r"best of", "the best-of comparison"),
    (r"test result reused", "the tests (the earlier result was reused, as no file changed)"),
    (r"test|pytest|unittest", "the tests"),
]


def gate_words(gate):
    g = str(gate or "").strip()
    low = g.lower()
    for rx, words in GATE_WORDS:
        if re.search(rx, low):
            return words
    return f"the '{g}' check" if g else "a check"


def _counts(text):
    t = str(text or "")
    out = {}
    for k, rx in (("passed", r"(\d+) passed"), ("failed", r"(\d+) failed"), ("errors", r"(\d+) errors?\b")):
        found = re.findall(rx, t)
        if found:
            out[k] = int(found[-1])
    return out


def _counts_words(c):
    parts = []
    if c.get("failed"):
        parts.append(f"{_plural(c['failed'], 'test')} failed")
    if c.get("errors"):
        parts.append(f"{_plural(c['errors'], 'test')} could not load")
    if c.get("passed"):
        parts.append(f"{_n(c['passed'])} passed")
    return _join(parts)


def outcome_words(ok):
    """The three outcomes, never two."""
    if ok is True:
        return "passed"
    if ok is False:
        return "failed"
    return "could not run"


# ------------------------------------------------------------------------------------------------ the route
def _route_reason(why):
    """The router's own 'why' string (arch_flows.route) -> the reason in plain words."""
    w = str(why or "").strip()
    low = w.lower()
    if not w:
        return ""
    if low == "chosen by you":
        return "you chose it"
    if low == "default":
        return "nothing in the request pointed elsewhere, so the default was used"
    if low.startswith("best keyword match"):
        return "it was the closest match for the words in your request"
    m = re.match(r"keywords:\s*(.*?)(\s*\(no model answered\))?$", w, re.I | re.S)
    if m:
        words = []
        for x in m.group(1).split(","):
            x = x.strip()
            if x and x not in words:
                words.append(x)
        said = _join(f"'{x}'" for x in words[:4])
        return f"you said {said}" + (" (no router model answered, so the words decided)" if m.group(2) else "")
    m = re.match(r"router model \((agrees with|differs from) the keyword hint\):\s*(.*)$", w, re.I | re.S)
    if m:
        reason = m.group(2).strip().rstrip(".")
        agree = "the keyword match agreed" if m.group(1).lower().startswith("agrees") else "the keyword match suggested something else"
        return (f"the router model judged: {reason} ({agree})" if reason else f"the router model chose it ({agree})")
    return w.rstrip(".")


# ------------------------------------------------------------------------------------------------ one record per type
def _call(ev):
    who = model_words(ev.get("model"))
    role = ev.get("label") or ev.get("role") or ""
    step = ev.get("step") or ""
    if ev.get("cached") or ev.get("transport") == "cache" or ev.get("done_reason") == "cached":
        saved = []
        if ev.get("tokens_saved"):
            saved.append(f"{ev['tokens_saved']} tokens")
        if ev.get("secs_saved"):
            saved.append(_secs(ev["secs_saved"]))
        v = ev.get("cached_verdict")
        return _sentence(f"{who} was not asked again: this machine had already answered exactly this question, so that reply was reused"
                         + (f", saving {_join(saved)}" if saved else "")
                         + (f" (last time it ended in: {str(v).replace('_', ' ')})" if v else ""))
    s = f"{who} answered as the {role}" if role else f"{who} answered"
    host = ev.get("host")
    if host:
        s += f" on the machine '{host}'"
    elif ev.get("transport") == "claude" or is_claude(ev.get("model")):
        s += " over the internet"
    bits = []
    if ev.get("gen_tokens") is not None:
        bits.append(f"{ev['gen_tokens']} tokens")
    if ev.get("secs") is not None:
        bits.append(f"in {_secs(ev['secs'])}")
    if ev.get("tok_s"):
        bits.append(f"({ev['tok_s']} tokens a second)")
    if bits:
        s += ", writing " + " ".join(bits)
    if ev.get("usd"):
        s += f"; it cost {_usd(ev['usd'])}"
    elif not (ev.get("transport") == "claude" or is_claude(ev.get("model"))):
        s += "; it ran on your own hardware, so it cost nothing"
    if ev.get("done_reason") == "length":
        s += "; the reply was cut off at its length limit, so only its finished parts could be used"
    if ev.get("ctx_overflow"):
        s += "; the prompt did not fit the model's window and was shortened"
    elif isinstance(ev.get("ctx_pct"), (int, float)) and ev["ctx_pct"] >= 85:
        s += f"; its window was {ev['ctx_pct']}% full"
    if step:
        s += f" (step '{step}')"
    return _sentence(s)


def _step(ev):
    name = ev.get("step") or "a step"
    st = ev.get("status")
    if st == "started":
        return _sentence(f"Step '{name}' started")
    if st == "skipped":
        return _sentence(f"Step '{name}' was skipped: its condition did not apply to this run")
    if st == "resumed":
        return _sentence(f"Step '{name}' was not done again: an earlier, interrupted run had already finished it")
    extra = []
    if ev.get("tries"):
        extra.append(_plural(ev["tries"], "try", "tries"))
    if ev.get("secs"):
        extra.append(_secs(ev["secs"]))
    tail = f" ({', '.join(extra)})" if extra else ""
    changed = [c for c in (ev.get("changed") or []) if c]
    ch = ""
    if changed:
        ch = "; it changed " + (_join(changed[:3]) + (f" and {_n(len(changed) - 3)} more" if len(changed) > 3 else ""))
    if ev.get("unsure"):
        return _sentence(f"Step '{name}' ended without a firm answer{tail}: a check could not run or gave different answers, so it counts as not sure{ch}")
    if ev.get("ok") is True:
        return _sentence(f"Step '{name}' passed its checks{tail}{ch}")
    if ev.get("ok") is False:
        return _sentence(f"Step '{name}' did not pass its checks{tail}{ch}")
    return _sentence(f"Step '{name}' ended{tail}{ch}")


def _check(ev):
    g = gate_words(ev.get("gate"))
    ok = ev.get("ok")
    step = ev.get("step")
    where = f" in step '{step}'" if step else ""
    if ok is True:
        c = _counts_words(_counts(ev.get("text")))
        return _sentence(f"{g} passed{where}" + (f" ({c})" if c else ""))
    if ok is None:
        why = _first_line(ev.get("text"), 90)
        return _sentence(f"{g} could not run{where}" + (f" ({why})" if why else "")
                         + ", so this is reported as not sure rather than as a pass or a fail")
    c = _counts_words(_counts(ev.get("text")))
    if c:
        return _sentence(f"{g} failed{where}: {c}")
    why = _first_line(ev.get("text"), 100)
    return _sentence(f"{g} failed{where}" + (f": {why}" if why else ""))


def _mark(ev):
    a = ev.get("accepted")
    r = _clip(ev.get("reason"), 120)
    if a is True:
        return _sentence("The checks accepted that reply" + (f" ({r})" if r else "") + "; it may be used as an example of good work")
    if a is False:
        return _sentence("The checks rejected that reply" + (f": {r}" if r else ""))
    return _sentence("That reply could not be checked" + (f" ({r})" if r else "")
                     + ", so it is neither counted as good nor as bad work")


TOOL_WORDS = {"ls": "listed the files in {path}", "grep": "searched the code for '{pattern}'", "search": "searched the project for '{query}'",
              "read": "read {path}", "view": "looked at {path}", "outline": "listed the functions in {path}", "run_tests": "ran the tests",
              "memory_read": "read its notes in {name}", "memory_append": "wrote a note in {name}", "apply": "changed files",
              "edit_lines": "edited lines in {path}"}


def _tool(ev):
    name = str(ev.get("tool") or "a tool")
    args = ev.get("args") if isinstance(ev.get("args"), dict) else {}
    tpl = TOOL_WORDS.get(name)
    if tpl:
        fill = {k: _clip(v, 50) for k, v in args.items()}
        for k in ("path", "pattern", "query", "name"):
            fill.setdefault(k, {"path": "the project", "name": "the notes"}.get(k, "something"))
        try:
            did = tpl.format(**fill)
        except (KeyError, IndexError, ValueError):
            did = f"used the '{name}' tool"
    else:
        did = f"used the '{name}' tool"
    turn = f" (turn {ev['turn']})" if ev.get("turn") else ""
    if ev.get("ok") is False:
        res = _first_line(ev.get("result"), 90)
        return _sentence(f"The model {did}{turn}, but that did not work" + (f": {res}" if res else ""))
    if ev.get("ok") is None:
        return _sentence(f"The model {did}{turn}; whether it worked could not be told")
    return _sentence(f"The model {did}{turn}")


def _stuck_words(why):
    w = str(why or "")
    c = _counts_words(_counts(w))
    if c:
        return f"the same check kept failing ({c})"
    first = _first_line(w, 80)
    return f"the check kept failing ({first})" if first else "it kept failing the checks"


def _escalation(ev):
    to = ev.get("to_model") or ""
    fm = ev.get("from_model") or ""
    tries = ev.get("tries")
    chain = [m for m in (ev.get("chain") or []) if m]
    if not chain and fm:
        chain = [fm]
    stuck_by = _join(short_model(m) for m in chain) if chain else "the smaller model"
    both = " both" if len(chain) == 2 else (" all" if len(chain) > 2 else "")
    stuck = f"{stuck_by}{both} got stuck" + (f" {_times(tries)}" if tries else "")
    step = f" on step '{ev['step']}'" if ev.get("step") else ""
    cost = f"; it cost {_usd(ev['usd'])}" if ev.get("usd") else ""
    if is_claude(to) or ev.get("to_tier") == "cloud":
        return _sentence(f"Claude was asked{step} because {stuck}{cost}")
    tier = ev.get("to_tier")
    target = model_words(to) + (f", the {tier} tier," if tier else "")
    note = "; it was given a handover note saying what had not worked, so it would try a different route" if ev.get("handover") else ""
    reason = f": {_stuck_words(ev.get('why'))}" if ev.get("why") else ""
    return _sentence(f"The work{step} was handed to {target} because {stuck}{reason}{note}{cost}")


VERDICT_WORDS = {"pass": "The run passed: every check it needed ran and passed",
                 "fail": "The run did not pass: at least one check ran and failed",
                 "not_sure": "The run ended as not sure: a check could not run (or gave different answers with nothing changed), so it is not called a pass"}


def _verdict(ev):
    v = ev.get("verdict")
    s = VERDICT_WORDS.get(v, "The run stopped before it reached a verdict")
    why = _clip(ev.get("why"), 140)
    if why and v != "pass":
        s += f" ({why.rstrip('.')})"
    m = ev.get("meter") if isinstance(ev.get("meter"), dict) else {}
    bits = []
    if m.get("calls"):
        bits.append(_plural(m["calls"], "model call"))
    tok = int(m.get("tokens_in") or 0) + int(m.get("tokens_out") or 0)
    if tok:
        bits.append(f"{tok} tokens")
    if m.get("secs"):
        bits.append(_secs(m["secs"]) + " of model time")
    if bits:
        s += "; it used " + _join(bits)
    if m.get("usd"):
        s += f" and {_usd(m['usd'])}"
    return _sentence(s)


def _hook(ev):
    point = str(ev.get("point") or "").replace("_", " ")
    name = ev.get("hook") or ev.get("cmd") or "a command"
    when = f" ({point})" if point else ""
    res = outcome_words(ev.get("ok"))
    out = _first_line(ev.get("output"), 80) if ev.get("ok") is False else ""
    return _sentence(f"Your own command '{_clip(name, 50)}'{when} {'ran and ' + res if res != 'could not run' else res}"
                     + (f": {out}" if out else ""))


def _flaky(ev):
    g = gate_words(ev.get("gate"))
    return _sentence(f"{g} failed and then passed with nothing changed, so it is flaky; that step is called not sure instead of "
                     "spending another try on a coin toss")


def _best_of_n(ev):
    n, p, w = ev.get("n"), ev.get("passed"), ev.get("winner")
    s = (f"{_n(n)} different answers" if n else "Several answers") + " were written" + (" at the same time" if ev.get("parallel") else "")
    s += f" and {_n(p)} passed the checks" if p else " and none passed the checks"
    s += f"; answer {w} was kept as the smallest change that passed" if w else "; none was good enough to keep"
    return _sentence(s)


def _postmortem(ev):
    cause = _clip(ev.get("cause"), 160).rstrip(".")
    nxt = _clip(ev.get("next_try"), 120).rstrip(".")
    s = f"Afterwards the model explained the failure: {cause}" if cause else "Afterwards the model wrote down why the run failed"
    if nxt:
        s += f"; next time: {nxt}"
    return _sentence(s + " (advice, not a verdict)")


def _todo(ev):
    items = ev.get("items") or []
    done = sum(1 for i in items if isinstance(i, dict) and i.get("status") == "done")
    return _sentence(f"The plan has {_plural(len(items), 'item')}" + (f", {_n(done)} done" if done else ""))


def _route(ev):
    flow = ev.get("flow") or ev.get("pick") or ""
    reason = _route_reason(ev.get("why"))
    s = f"The router picked the {flow} flow" if flow else "The router picked a flow"
    if reason == "you chose it":
        s = f"The {flow} flow ran because you chose it" if flow else "The flow you chose ran"
    elif reason:
        s += f" because {reason}"
    facts = []
    if ev.get("has_tests") is True:
        facts.append("the folder has tests")
    elif ev.get("has_tests") is False and ev.get("empty") is not True:
        facts.append("the folder has no tests yet")
    if ev.get("empty") is True:
        facts.append("the folder is empty")
    if facts and reason != "you chose it":
        s += (" and " if reason else " because ") + _join(facts)
    doms = [d for d in (ev.get("domains") or []) if d]
    if doms:
        s += f" (areas: {', '.join(doms[:4])})"
    return _sentence(s)


def _undo_reason(why):
    w = str(why or "")
    m = re.search(r"fewer tests pass \((\d+) -> (\d+)\) and more fail \((\d+) -> (\d+)\)", w)
    if m:
        pb, pa, fb, fa = (int(x) for x in m.groups())
        broke = fa - fb
        return f"it made {_plural(broke, 'test')} fail that passed before" if broke > 0 else f"fewer tests passed ({pb} before, {pa} after)"
    return w.strip().rstrip(".")


def _undo(ev):
    why = ev.get("why") or ev.get("reason")
    if not why and ev.get("failed_after") is not None and ev.get("failed_before") is not None:
        d = int(ev["failed_after"]) - int(ev["failed_before"])
        why = f"it made {_plural(d, 'test')} fail that passed before" if d > 0 else ""
    r = _undo_reason(why)
    return _sentence("This change was undone" + (f": {r}" if r else "") + "; the better earlier version was kept")


def _refusal(ev):
    t = str(ev.get("text") or ev.get("why") or ev.get("error") or "")
    low = t.lower()
    if "budget for this run" in low or "per run" in low:
        return _sentence("Claude was not asked: this run's Claude budget was used up (Settings > Claude spend per run)")
    if "daily spend cap" in low or "per day" in low:
        return _sentence("Claude was not asked: today's Claude spending cap was reached (Settings > Claude spend per day)")
    if "self-tests" in low:
        return _sentence("Claude was not asked: the self-tests never use Claude")
    if "during eval" in low:
        return _sentence("Claude was not asked: a benchmark measures your own models, so Claude is never used during eval")
    if "online features are off" in low:
        return _sentence("Claude was not asked: online features are off (Settings > Allow online features)")
    if "claude is off" in low:
        return _sentence("Claude was not asked: Use Claude is off (Settings > Online > Use Claude)")
    if "declined" in low or "refusal" in low:
        return _sentence("Claude declined this request; that counts as could not run, not as an answer")
    if "sdk" in low:
        return _sentence("Claude could not be asked: the Anthropic SDK is not installed")
    if "did not answer" in low:
        return _sentence("Claude did not answer (a network or service problem), so nothing was spent on a reply")
    return _sentence("A model could not be asked" + (f": {_clip(t, 120)}" if t else ""))


BROKER_WORDS = {"gpu_worker": "the worker model on the graphics card", "heavy": "the larger (heavy) model", "cpu_router": "the small router model on the processor",
                "ecores": "the processor's efficiency cores", "claude": "Claude", "wait": "nothing yet (it waits)"}


def _broker(ev):
    t = ev.get("target") or "wait"
    kind = ev.get("kind") or "job"
    s = f"This {kind} job went to {BROKER_WORDS.get(t, t)}" if t != "wait" else f"This {kind} job is waiting"
    if ev.get("why"):
        s += f" because {str(ev['why']).rstrip('.')}"
    if t == "claude" and ev.get("cost_usd"):
        s += f"; about {_usd(ev['cost_usd'])}"
    if ev.get("eta_s") is not None:
        s += f" (about {_secs(ev['eta_s'])}, {ev.get('evidence', 'estimate')})"
    return _sentence(s)


def _token(ev):
    return _sentence(f"{model_words(ev.get('model'))} is writing" + (f" for the {ev['label']}" if ev.get("label") else ""))


HANDLERS = {"call": _call, "step": _step, "check": _check, "mark": _mark, "tool": _tool, "escalation": _escalation, "verdict": _verdict,
            "hook": _hook, "flaky": _flaky, "best_of_n": _best_of_n, "postmortem": _postmortem, "todo": _todo, "route": _route,
            "undo": _undo, "refusal": _refusal, "cloud": _refusal, "error": _refusal, "broker": _broker, "token": _token}


def explain_event(ev):
    """One trace or live-event record -> one plain sentence. Never raises: a record it cannot read is said as such."""
    if not isinstance(ev, dict):
        return "Something was recorded that is not a record this app writes."
    t = str(ev.get("type") or "")
    if not t and "target" in ev and "considered" in ev:
        t = "broker"
    fn = HANDLERS.get(t)
    try:
        if fn:
            return fn(ev)
    except Exception:
        pass
    extra = f" in step '{ev['step']}'" if ev.get("step") else ""
    return _sentence((f"A {t.replace('_', ' ')} record" if t else "An unnamed record") + f" was written{extra}")


# ------------------------------------------------------------------------------------------------ the whole run
def _read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                if isinstance(j, dict):
                    out.append(j)
    except OSError:
        pass
    return out


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            j = json.load(f)
        return j if isinstance(j, dict) else {}
    except (OSError, ValueError):
        return {}


def _enrich_escalations(recs):
    """An escalation record names one hand-over. To say 'the 4B and the MoE both got stuck', each one is told which models held the
    step before it; to say what it cost, the dollars of the calls the new model made in that step are added up."""
    seen = {}
    out = []
    for i, r in enumerate(recs):
        if r.get("type") != "escalation":
            out.append(r)
            continue
        r = dict(r)
        step = r.get("step") or ""
        prior = seen.setdefault(step, [])
        chain = list(prior)
        if r.get("from_model") and r["from_model"] not in chain:
            chain.append(r["from_model"])
        r["chain"] = chain
        prior[:] = chain + ([r["to_model"]] if r.get("to_model") else [])
        if not r.get("usd"):
            spent = 0.0
            for c in recs[i + 1:]:
                if c.get("type") == "escalation" and (c.get("step") or "") == step:
                    break
                if c.get("type") == "call" and c.get("model") == r.get("to_model"):
                    try:
                        spent += float(c.get("usd") or 0)
                    except (TypeError, ValueError):
                        pass
            if spent:
                r["usd"] = round(spent, 4)
        out.append(r)
    return out


def _calls_summary(calls):
    if not calls:
        return "No model was asked anything in this run."
    real = [c for c in calls if not (c.get("cached") or c.get("transport") == "cache")]
    cached = len(calls) - len(real)
    by = {}
    for c in real:
        k = short_model(c.get("model"))
        by[k] = by.get(k, 0) + 1
    parts = [f"{k} {_times(v)}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])]
    tok = sum(c["gen_tokens"] for c in real if isinstance(c.get("gen_tokens"), int))
    usd = sum(float(c.get("usd") or 0) for c in real if isinstance(c.get("usd"), (int, float)))
    if not real:
        return _sentence(f"Every one of the {_plural(cached, 'answer')} came from replies this machine had already given, at no cost")
    s = f"The models were asked {_plural(len(real), 'time')}" + (f" ({_join(parts[:4])})" if parts else "")
    if tok:
        s += f" and wrote {tok} tokens"
    if cached:
        s += f"; {_plural(cached, 'answer')} came from replies this machine had already given"
    s += f"; Claude cost {_usd(usd)} in total" if usd else "; everything ran on this machine, at no cost"
    return _sentence(s)


def _checks_summary(checks):
    if not checks:
        return "No code check ran in this run, so nothing in it could be proven to work."
    ok = sum(1 for c in checks if c.get("ok") is True)
    bad = sum(1 for c in checks if c.get("ok") is False)
    none = sum(1 for c in checks if c.get("ok") is None)
    parts = [f"{_n(ok)} passed" if ok else "", f"{_n(bad)} failed" if bad else "", f"{_n(none)} could not run" if none else ""]
    return _sentence(f"Code checked the work {_plural(len(checks), 'time')}: {_join(parts)}")


def _tools_summary(tools):
    if not tools:
        return ""
    by = {}
    for t in tools:
        by[t.get("tool") or "?"] = by.get(t.get("tool") or "?", 0) + 1
    parts = [f"{k} ({_times(v)})" if v > 1 else k for k, v in sorted(by.items(), key=lambda kv: -kv[1])]
    return _sentence(f"The model used its tools {_plural(len(tools), 'time')}: {_join(parts[:5])}")


PER_TYPE = {"escalation": 4}       # how many records of one type are said one by one before the rest are counted (default 2)
MORE_WORDS = {"undo": "undone tries", "step": "ended steps", "check": "failed or unrunnable checks", "hook": "failed commands of yours",
              "mark": "unchecked replies", "refusal": "refusals", "escalation": "hand-overs to a larger model", "flaky": "flaky checks",
              "best_of_n": "best-of rounds"}


def run_records(run_dir):
    """-> (meta from run.json, the trace records in order)."""
    meta = _read_json(os.path.join(run_dir, "run.json"))
    recs = _read_jsonl(os.path.join(run_dir, "trace.jsonl"))
    return meta, recs


def explain_run(run_dir):
    """The run as 5-12 plain sentences, in the order things happened. -> list of strings (empty when the folder holds no run)."""
    meta, recs = run_records(run_dir)
    if not meta and not recs:
        return []
    recs = _enrich_escalations(recs)
    items = []          # (order, priority, sentence): a lower priority number survives the trim to MAX_SENTENCES

    goal = _clip(meta.get("goal"), 120)
    flow = meta.get("flow") or next((r.get("flow") for r in recs if r.get("flow")), "")
    opening = f"You asked: \"{goal}\"" if goal else "A run was started"
    if flow and flow != "route":
        opening += f", and it ran as the {flow} flow"
    items.append((-3, 0, _sentence(opening)))

    calls = [r for r in recs if r.get("type") == "call"]
    checks = [r for r in recs if r.get("type") == "check"]
    tools = [r for r in recs if r.get("type") == "tool"]
    verdict = next((r for r in reversed(recs) if r.get("type") == "verdict"), None)
    changed, first_fail_logged = [], set()
    said, more = {}, {}               # per record type: how many were said, and the ones folded into "N more ..."

    def add(order, prio, t, rec):
        said[t] = said.get(t, 0) + 1
        if said[t] <= PER_TYPE.get(t, 2):
            items.append((order, prio, explain_event(rec)))
        else:
            o, p, k = more.get(t, (order, prio, 0))
            more[t] = (o, p, k + 1)

    for i, r in enumerate(recs):
        t = r.get("type")
        if t == "route":
            items.append((-2, 0.5, explain_event(r)))
        elif t in ("escalation", "undo", "flaky", "best_of_n", "refusal"):
            add(i, 1, t, r)
        elif t == "postmortem":
            items.append((len(recs) + 2, 0.5, explain_event(r)))
        elif t == "step" and r.get("status") != "started" and ("ok" in r or r.get("status") in ("skipped", "resumed")):
            changed += [c for c in (r.get("changed") or []) if c and c not in changed]
            add(i, 3 if r.get("ok") is True else 2, "step", r)
        elif t == "check" and r.get("ok") is not True:
            key = (r.get("step"), r.get("gate"))
            if key not in first_fail_logged:              # the first failure of each check per step; repeats are in the summary
                first_fail_logged.add(key)
                add(i, 4, "check", r)
        elif t == "hook" and r.get("ok") is False:
            add(i, 4, "hook", r)
        elif t == "mark" and r.get("accepted") is None:
            add(i, 5, "mark", r)
    for t, (o, p, k) in more.items():
        items.append((o, p, _sentence(f"{_n(k)} more {MORE_WORDS.get(t, t.replace('_', ' ') + ' records')} followed")))

    end = len(recs)
    items.append((-1, 2, _calls_summary(calls)))
    tsum = _tools_summary(tools)
    if tsum:
        items.append((0, 5, tsum))
    items.append((end, 0.5, _checks_summary(checks)))
    if changed:
        rest = f" and {_n(len(changed) - 4)} more" if len(changed) > 4 else ""
        items.append((end + 0.5, 2, _sentence(f"Files changed: {_join(changed[:4])}{rest}")))
    else:
        items.append((end + 0.5, 3, "No file in the project was changed." if calls else "Nothing was changed in the project."))
    if verdict:
        items.append((end + 1, 0, explain_event(verdict)))
    else:
        st = meta.get("status")
        items.append((end + 1, 0, _sentence("The run has not reached a verdict yet" + (f" (status: {st})" if st else "")
                                            + "; until it does, nothing in it counts as passed")))

    if len(items) > MAX_SENTENCES:
        keep = sorted(range(len(items)), key=lambda k: (items[k][1], items[k][0]))[:MAX_SENTENCES]
        items = [items[k] for k in sorted(keep)]
    items.sort(key=lambda x: x[0])
    out = [s for _, _, s in items if s]
    if len(out) < MIN_SENTENCES and meta.get("started") and meta.get("status"):
        out.insert(1, _sentence(f"The run's status is {str(meta['status']).replace('_', ' ')}"))
    while len(out) < MIN_SENTENCES:
        out.insert(-1, "Nothing else was recorded.")
    return out[:MAX_SENTENCES]


# ------------------------------------------------------------------------------------------------ the app's words
GLOSSARY = {
    "tier": "One of the four jobs a model can hold here: router (sorts requests), worker (writes most code), heavy (the larger model for "
            "hard steps) and draft (guesses ahead so the worker writes faster). Any model you have installed can fill any tier.",
    "router": "The small, fast model that reads your request and picks the flow; it also writes short summaries. It never writes code.",
    "worker": "The model that writes most of the code. It stays loaded on the graphics card so each answer starts quickly.",
    "heavy": "The larger model, used when a step is hard or the worker got stuck. Slower, so it is asked only when it can answer in time.",
    "draft": "A small model with the same vocabulary as the worker that guesses the next words; the worker checks the guesses, which "
             "makes it write faster without changing what it writes.",
    "role": "A job in the team (planner, coder, reviewer, tester ...). Each role uses its tier's model unless you assign one by name; "
            "a model you assign wins.",
    "flow": "A recipe for one kind of request (build, bugfix, refactor, test ...): its steps, which tier does each, and which checks must pass.",
    "step": "One stage of a flow, such as writing a failing test or applying the fix. Each step has its own checks and limits.",
    "sprint": "One round of building inside the build flow; small projects are done in one sprint so finished work is never lost in a hand-off.",
    "gate": "A check done by code, not by a model: tests, the syntax check, the fake-code check, the shrink guard. Gates decide what "
            "passes; a model's opinion never does on its own.",
    "check": "Code that tests the model's work and answers passed, failed, or could not run.",
    "pass": "Every check the run needed ran and passed.",
    "fail": "At least one check ran and failed.",
    "not sure": "A check could not run (no tests, a missing tool, a flaky result), so the run is not called a pass. It is not a failure "
                "either, and it is never used to teach a model.",
    "verdict": "The run's conclusion: pass, fail, or not sure. It comes from the checks, never from a model saying it is done.",
    "escalation": "Handing a stuck step to a larger model: the worker to the heavy model, and, only if you turned Claude on, the heavy "
                  "model to Claude. It happens only when the larger model can answer in time here.",
    "handover": "The note a larger model gets when it takes over a stuck step: what was tried, that it did not work, and to take another route.",
    "stalled": "A model is stalled when its last tries changed nothing, repeated an earlier answer, left the same failure, or went in circles.",
    "undo": "Every change the app makes is backed up first, so it can be put back; a try that made things worse is undone automatically.",
    "best try": "The app keeps the best version so far: a try that broke code, lost a function, or made tests fail that passed before is "
                "undone before the next try.",
    "flaky": "A check that failed and then passed with nothing changed. Its answer cannot be trusted, so that step is called not sure.",
    "best of n": "Several answers to the same step are written and checked; the smallest change that passes is kept.",
    "postmortem": "After a run that did not pass, the model explains why in plain words from the run's own records. Advice for the next "
                  "try, never a verdict.",
    "council": "A way to ask Claude hard questions: one answer is written, two critics try to prove it wrong, and a lead writes the final "
               "answer from their notes. It costs several calls, so it is only used when you ask.",
    "crew": "Your Claude agents (Managed Agents) that the app can start and watch; any tool they want to use is refused unless you allow it.",
    "night shift": "Crew jobs scheduled to run unattended, with every run written to a log you can read in the morning.",
    "fleet": "Several Claude Code agents working at once, each owning its own files so they never edit the same one. The app, not the "
             "agents, runs the tests, and the plan is refused before anything is spent if two agents would share a file.",
    "fast lane": "A setting for the worker only: the whole model and its memory stay on the graphics card, it guesses repeated text "
                 "ahead, and it runs at higher priority. Off by default; with it off nothing changes.",
    "make room": "Before a model that is not already loaded runs, the app may stop its own models on the graphics card so the new one "
                 "fits, and brings them back afterwards.",
    "use claude": "The setting that lets Claude take part: off, plan (Claude does the thinking roles), escalation (only when your models "
                  "are stuck), or all. Each request is checked against your per-run and per-day spending caps first.",
    "cap": "A spending limit for Claude, per run and per day. A request that would go over it is not sent.",
    "budget": "A limit on model calls, tokens or seconds for a step or a whole flow, checked before each request goes out.",
    "stop": "A STOP file (or the stop button) ends the run between two model calls, so no answer is cut in half.",
    "context window": "How much text a model can read at once. Prompts are fitted to it; when they do not fit, the least needed project "
                      "text is cut first.",
    "prefix cache": "The server remembers the start of the last prompt, so a prompt that begins the same way is read much faster.",
    "reply cache": "A reply this machine already gave to exactly the same question, reused so it never costs twice. Only used when the "
                   "answer would be the same anyway (temperature 0 or a fixed seed).",
    "moe": "Mixture of experts: a large model that uses only a few of its parts per word, so it is faster than its size suggests.",
    "quantisation": "Storing a model with fewer bits per number so it fits in less memory; fewer bits cost some accuracy.",
    "passport": "The facts about one installed model (size, layers, vocabulary) plus what was measured on this machine.",
    "index": "A searchable list of the project's functions, used to choose which code the model is shown. It never decides a pass.",
    "memory": "Short notes the model keeps per project; a note becomes confirmed only after a run that was shown it passed its checks.",
    "skill": "A short page about one way of working, shown to the model only for tasks it matches.",
    "command": "A saved request you call by name, like /fix or /review. It becomes words and then runs the normal way, with the same checks.",
    "hook": "Your own command that runs at a fixed point in a run (before it, after a change, after the checks ...). A project's hooks "
            "run only after you trust them.",
    "receipt": "One run as a single web page you can keep: what happened, the checks, the verdict and the cost.",
    "ledger": "The record of what happened in your runs; replies that passed the checks become examples for later prompts.",
    "fake code": "Placeholder code (stubs, 'TODO', pass) presented as finished work. The fake-code check refuses it.",
    "shrink guard": "A check that refuses a change which would silently delete much of an existing file.",
    "def block": "A way for the model to replace one whole function while every other line of the file is kept.",
    "studio": "The chat that answers questions about your project from its index. It talks; it does not change files.",
    "autopilot": "Works through your backlog of tasks unattended, within the limits you set.",
    "supervisor": "The work queue: each task runs on its own, a failed task is retried with a different flow, and machine problems pause "
                  "the queue instead of wasting tries.",
    "eval": "A fixed set of tasks run with your own models to measure pass rate, speed and tokens, so a change can be compared fairly.",
    "self-improve": "The app editing its own code in a throwaway copy, with the full test suite as the gate and an undo.",
}

_ALIASES = {"tiers": "tier", "gates": "gate", "checks": "check", "not_sure": "not sure", "notsure": "not sure", "unsure": "not sure",
            "escalate": "escalation", "escalated": "escalation", "fastlane": "fast lane", "fast_lane": "fast lane", "mixture of experts": "moe",
            "quantization": "quantisation", "quant": "quantisation", "window": "context window", "ctx": "context window",
            "best_of_n": "best of n", "best-of-n": "best of n", "night_shift": "night shift", "nightshift": "night shift",
            "claude": "use claude", "cloud": "use claude", "cache": "reply cache", "fakecode": "fake code", "def": "def block",
            "spend cap": "cap", "caps": "cap", "handover note": "handover", "stuck": "stalled", "agents": "crew"}


def glossary(term=None):
    """glossary('fast lane') -> the meaning in plain words ('' for a word it does not know). glossary() -> every entry (a copy)."""
    if term is None:
        return dict(GLOSSARY)
    t = " ".join(str(term).strip().lower().replace("_", " ").split())
    raw = str(term).strip().lower()
    for k in (raw, t, t.replace("-", " "), t.rstrip("s"), t.replace("-", "")):
        k = _ALIASES.get(k, k)
        if k in GLOSSARY:
            return GLOSSARY[k]
    return ""


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]):
        for line in explain_run(sys.argv[1]):
            print(line)
    elif len(sys.argv) > 1:
        print(glossary(" ".join(sys.argv[1:])) or "(not in the glossary)")
    else:
        print("usage: python arch_why.py RUN_DIR | TERM")
