"""Why a task failed, in one word, from what the checks said: no model call.

Families (first match wins):
  environment  the machine, not the model: a server down or loading, no output, context overflow, a port, a missing tool
  refusal      the model declined (the no_refusal judge)
  wiring       the contract's SHAPE: a module, name, __init__/__main__ or signature the request states is missing or different
  tests        only the model's own tests fail while the hidden behaviour check passed
  timeout      the run or a check ran out of time or budget
  behaviour    everything else that ran and was wrong
Used by arch_costmodel.from_evals (per point), the Cost map and tools/morning_report.py: the biggest family is where a harness fix
pays most (2026-11-05: about half of 30 whole-project failures were wiring, which arch_flows.wire_contract now fixes).
"""
import re

ENV = re.compile(r"not reachable|Loading model|No first token|No further output|Context size has been exceeded|connection refused|"
                 r"server .*(down|died)|address already in use|could not run|No module named 'pytest'|llama-server said", re.I)
WIRING = re.compile(r"ImportError|ModuleNotFoundError|cannot import name|No module named|missing: .*(__init__|__main__)\.py|"
                    r"takes \d+ positional argument|got an unexpected keyword|has no attribute|is not callable|"
                    r"does not fit the signature|python -m .* (has no|cannot)|missing: \S+\.py|is missing$", re.I)
TIMEOUT = re.compile(r"timed out|timeout|BudgetExceeded|budget|ran out of", re.I)
FAMILIES = ("environment", "refusal", "wiring", "tests", "timeout", "behaviour")


def classify(result):
    """One eval result dict (verdict, error, judges[...]) -> a family, or None for a pass."""
    if result.get("verdict") == "pass":
        return None
    judges = result.get("judges") or []
    text = " ".join([str(result.get("error") or "")] + [str(j.get("reason") or "") for j in judges if not j.get("ok")])
    if (result.get("refusal") or {}).get("refused") or any(j.get("type") == "no_refusal" and not j.get("ok") and j.get("could_run") for j in judges):
        return "refusal"
    if ENV.search(text):
        return "environment"
    if WIRING.search(text):
        return "wiring"
    others = [j for j in judges if j.get("type") != "tests_pass"]
    own = [j for j in judges if j.get("type") == "tests_pass"]
    if others and all(j.get("ok") for j in others) and own and not all(j.get("ok") for j in own):
        return "tests"                                      # every other check passed: only the project's tests disagree
    if TIMEOUT.search(text):
        return "timeout"
    return "behaviour"


def summarize(results):
    """-> {family: count} over failed results."""
    out = {}
    for r in results:
        f = classify(r)
        if f:
            out[f] = out.get(f, 0) + 1
    return out


PLAIN = {"environment": "the machine (server, memory, tools), not the model", "refusal": "the model declined a safe task",
         "wiring": "the shape the request states (a name, module, entry point or signature)",
         "tests": "only the model's own tests failed; the hidden check passed",
         "timeout": "ran out of time or budget", "behaviour": "the code ran and did the wrong thing"}
