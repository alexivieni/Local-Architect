"""Why did that run fail? One short explanation in plain words, written next to the run, and handed to the next attempt.

A failed run leaves a trace with everything in it -- every reply, every check, every piece of feedback -- but nobody reads 2,000 lines of
trace, and the next attempt (the supervisor's alternate flow) used to start from nothing. Here the model reads a DIGEST of the failure
(the checks that failed, the feedback the last tries got, how the run ended) and answers three questions: what went wrong, what the next
attempt should do differently, and where the fault most likely is. Code builds the digest; the model only explains it.

This is advice, never a verdict: it cannot turn a fail into a pass, it is not used as training data, and it never runs during a benchmark
(it would add calls the benchmark does not measure). `behavior.postmortem = off` turns it off.
"""
import json
import os
import re
import time


ENVIRONMENTAL = re.compile(r"not reachable|timed out|no first token|is not installed|not found on|connection (?:refused|reset)|out of memory|"
                           r"budget|STOP file|stopped by", re.I)

SCHEMA = {"type": "object", "required": ["cause", "next_try", "fault"], "properties": {
    "cause": {"type": "string", "minLength": 1},
    "next_try": {"type": "string", "minLength": 1},
    "fault": {"type": "string", "enum": ["the code", "the tests the model wrote", "the request", "the machine", "unclear"]}}}

PROMPT = """A coding task failed. Below is a digest of what happened: the request, how the run ended, the checks that failed, and the feedback the last attempts were given. Explain the failure for the person who asked, and for the next attempt.

Answer ONLY JSON: {{"cause": "...", "next_try": "...", "fault": "the code|the tests the model wrote|the request|the machine|unclear"}}
- cause: one or two plain sentences: what actually went wrong. Name the function, file or check. No jargon without its meaning.
- next_try: one or two sentences: the concrete thing the next attempt must do differently.
- fault: where the problem most likely is.

REQUEST: {goal}
FLOW: {flow}
HOW IT ENDED: {why}

DIGEST:
{digest}"""


def digest(run_dir, limit=5000):
    """The failure, condensed from the run's own trace by code: failing checks and rejected replies, newest last. -> str"""
    fails, marks, labels = [], [], []
    try:
        with open(os.path.join(run_dir, "trace.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("type") == "call" and r.get("label"):
                    labels.append(str(r["label"]))
                elif r.get("type") == "check" and r.get("ok") is False:
                    fails.append(f"- check '{r.get('gate')}' failed: {str(r.get('text') or '')[-400:].strip()}")
                elif r.get("type") == "mark" and r.get("accepted") is False and r.get("reason"):
                    marks.append(f"- a reply was rejected: {str(r['reason'])[-500:].strip()}")
    except OSError:
        return ""
    seen, out = set(), []
    for x in (fails[-8:] + marks[-4:]):                           # the same failure repeated is one line, not six
        key = x[:160]
        if key not in seen:
            seen.add(key)
            out.append(x)
    head = f"model calls in order: {', '.join(labels[-20:])}\n" if labels else ""
    text = head + "\n".join(out)
    return text[-limit:]


def path(run_dir):
    return os.path.join(run_dir, "postmortem.md")


def explain(e, verdict, why=""):
    """After a failed or not-sure run. -> {"cause","next_try","fault"} or None (off, a benchmark, a dry run, nothing to explain).
    Never raises: an explanation must not be able to break the run it explains."""
    try:
        if verdict == "pass" or str(e.cfg["behavior"].get("postmortem", "on")) != "on":
            return None
        if os.environ.get("ARCHITECT_EVAL") == "1" or e.review == "dry" or not getattr(e, "run_dir", None):
            return None
        if ENVIRONMENTAL.search(why or ""):              # the machine was the problem, not the work: nothing for a model to explain
            return None
        d = digest(e.run_dir)
        if not d.strip():
            return None
        e.set_step("postmortem")
        small = ((e.cfg.get("behavior") or {}).get("small_helpers", "on") == "on" and (e.cfg.get("tiers") or {}).get("router"))
        r, out =e.ask_json("router" if small else "reviewer", PROMPT.format(goal=(e.goal or "")[:1500], flow=e.flow, why=(why or verdict)[:400], digest=d), schema=SCHEMA)
        if not isinstance(out, dict) or not out.get("cause"):
            return None
        out = {k: str(out.get(k) or "").strip()[:600] for k in ("cause", "next_try", "fault")}
        with open(path(e.run_dir), "w", encoding="utf-8") as f:
            f.write(f"# Why this run did not pass\n\n{out['cause']}\n\n## What the next attempt should do\n\n{out['next_try']}\n\n"
                    f"_Most likely at fault: {out['fault']}. Written by the model from the run's own checks; advice, not a verdict._\n")
        e._trace({"type": "postmortem", "t": time.time(), "flow": e.flow, **out})
        e.say(f"[postmortem] {out['cause']}")
        e.report("", "## Why it did not pass", out["cause"], "", f"- Next attempt: {out['next_try']}")
        return out
    except Exception:
        return None


def read(run_dir):
    """The saved explanation as one short paragraph for the next attempt, or ''."""
    try:
        with open(path(run_dir), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith(("#", "_"))]
    return " ".join(lines)[:900]
