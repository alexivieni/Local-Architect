"""Progress coach for fix loops: notices when attempts are not getting anywhere, writes targeted feedback, and (when allowed) moves the stuck role to a
larger model YOU already have installed for the rest of that step.

A small local model often repeats itself: the same edit, the same failure, round after round. Code can see that (fingerprints of the reply and of the
failure), so the loop does not need a model to notice. What it does about it:
  * repeat_reply   - the model sent the same answer again           -> say so, demand a different approach
  * same_failure   - the change did not alter the failure            -> point at the unchanged failure, list the files already touched
  * cycling        - the failure went A -> B -> A                    -> say the last two fixes undo each other
  * escalate       - after the first stall, with behavior.escalate = "stalled": pin the role to the next larger OWNED model (measured by `tune`,
                     not too big for this machine), for the rest of the step only. Nothing is downloaded; nothing remote is ever used.
"""
import hashlib
import os
import re

import arch_core as core
import arch_profiles

EMBED = re.compile(r"embed|nomic|bge|minilm|arctic|e5-", re.I)
STEP_UP = 1.25                 # the next model must be at least this much bigger (dense-equivalent) to be worth the wait


def _norm(t):
    t = re.sub(r"0x[0-9a-fA-F]+", "0x", str(t or ""))
    t = re.sub(r"\d+(?:\.\d+)?", "#", t)
    t = re.sub(r"(?:[\w.-]*/)+[\w.-]+", "/p", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def fp(t, exact=False):
    """Fingerprint of a failure (numbers, paths and addresses masked) or, with exact=True, of a reply (only whitespace ignored: a different number is a different edit)."""
    body = re.sub(r"\s+", " ", str(t or "")).strip() if exact else _norm(t)[:1500]
    return hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()[:12]


def _profile_models():
    return (core.load_json(os.path.join(core.data_dir(), "profile.json")) or {}).get("models") or {}


def next_model(e, current):
    """-> the model a stalled step goes up to: the heavy tier the person chose, if set and different from `current`; otherwise the
    smallest installed, tuned, fitting model that is clearly bigger than `current`, or None. Seen live (2026-11-04): with the MoE set as
    the heavy tier, escalation still picked the slower 14B because it was 'the next size up', and refused it as too slow."""
    try:
        heavy = (e.tier_model("heavy") or "").strip()
    except Exception:
        heavy = ""
    if heavy and heavy != current:
        return heavy
    import arch_cloud
    c = arch_cloud.cloud_model(getattr(e, "cfg", None))          # no local step up left: the cloud tier, when it is on
    if c and c != current and heavy == current:
        return c
    prof = _profile_models()
    mine = prof.get(current) or {}
    cur = arch_profiles.effective_b(current, None, mine.get("active_b"))
    if not cur:
        return None
    best = None
    for n in e.installed():
        if n == current or EMBED.search(n):
            continue
        p = prof.get(n)
        if not p or p.get("fit_verdict") == "too_big":          # untuned = fit unknown: not offered
            continue
        eff = arch_profiles.effective_b(n, None, p.get("active_b"))
        if eff and eff >= cur * STEP_UP and (best is None or eff < best[0]):
            best = (eff, n)
    return best[1] if best else None


def ready_now(e, model, reply_tokens=1500):
    """Can `model` answer a stalled step now, in a sane time? -> (ok, plain reason when not). Measured, not assumed. Seen live: a 14B
    split between the 6 GB card and RAM loads in ~30 s and then writes ~2.5 tokens a second, so one answer takes ~10 minutes and
    the runs that escalated to it ran out of time instead of being rescued."""
    import json
    import urllib.error
    import urllib.request
    if str(model or "").startswith("claude:"):                  # a cloud model needs no load and no graphics memory
        import arch_cloud
        return arch_cloud.allowed(e.cfg)
    try:
        import arch_server
        srv = arch_server.serving(model, e.cfg)
    except Exception:
        srv = None
    loaded = bool(srv)
    try:
        if srv:
            urllib.request.urlopen(srv["url"].rstrip("/") + "/health", timeout=2).read()
        else:
            with urllib.request.urlopen(core.ollama_host() + "/api/ps", timeout=2) as r:
                names = {m.get("name") or m.get("model") for m in (json.loads(r.read() or b"{}").get("models") or [])}
            loaded = model in names
    except urllib.error.HTTPError:
        pass                                                    # it answers, but cannot say what is loaded: assume a cold load
    except Exception:
        return False, f"{model} is not running here (its server did not answer)"
    s = (getattr(e, "stats", None) or {}).get(model) or {}
    gen = float(s.get("gen_tok_s") or 0)
    if not gen:
        return True, ""                                         # never measured: the call's own timeouts and the step-back cover it
    secs = (0.0 if loaded else float(s.get("load_s") or 60.0)) + reply_tokens / gen
    limit = float((e.cfg.get("limits") or {}).get("escalate_max_s") or 240)
    if secs > limit:
        return False, (f"{model} writes about {gen:.1f} tokens a second here, so one answer would take about {round(secs / 60)} min "
                       f"(the limit is {round(limit)} s: Settings > Limits > Escalation time limit)")
    return True, ""


HINTS = {
    "no_change": "STALLED: your last reply changed nothing on disk (the files already had that content), so the failure cannot change. Change something different.",
    "repeat_reply": "STALLED: you sent the same answer as an earlier attempt. Do not resend it. Say in one line what you now think the root cause is, then change something different.",
    "same_failure": "STALLED: your last change left the identical failure. Do not repeat it. Re-read the failing check and the code it exercises and try a different cause.",
    "cycling": "STALLED: the failure is back to one you already had two attempts ago, so your last two fixes undo each other. Find a change that satisfies both.",
}


class Coach:
    def __init__(self, e, role):
        self.e, self.role = e, role
        self.replies, self.fails, self.files = [], [], set()
        self.stalled, self.pinned, self.notified = 0, False, False

    def observe(self, reply, failure, changed=(), noop=False):
        r, f = fp(reply, exact=True), fp(failure)
        if noop:
            state = "no_change"
        elif r in self.replies:
            state = "repeat_reply"
        elif self.fails and f == self.fails[-1]:
            state = "same_failure"
        elif f in self.fails:
            state = "cycling"
        else:
            state = "progress"
        self.replies.append(r)
        self.fails.append(f)
        self.last_out = str(failure or "")[-4000:]            # the text behind the last fingerprint (the same-failure stop quotes it)
        self.files |= set(changed or [])
        self.stalled = 0 if state == "progress" else self.stalled + 1
        return state

    def escalate(self):
        """Pin the role to the next larger owned model. -> its name, or None (with the reason said once)."""
        if self.e.cfg["behavior"].get("escalate", "stalled") != "stalled":
            return None
        if self.pinned:                                          # already on the heavy tier: the cloud tier is the one step left
            import arch_cloud
            c = arch_cloud.cloud_model(self.e.cfg)
            if not c or self.e.pins.get(self.role) == c or getattr(self, "clouded", False):
                return None
            self.clouded = True
            self.e.pins[self.role] = c
            self.e.say(f"[!] {self.role} is still stuck on the heavy model; handing this step to {c} (cloud, budget-capped)")
            self.e.report(f"- {self.role} escalated to `{c}` (cloud tier) after the local heavy model also stalled")
            return c
        cur = self.e.model_for(self.role)
        nxt = next_model(self.e, cur)
        if not nxt:
            if not self.notified:
                self.notified = True
                self.e.say(f"[!] {self.role} is stalled on {cur}; no larger tuned model is installed to hand it to (run `tune` after pulling one)")
                self.e.report(f"- {self.role} stalled on `{cur}`: no larger tuned model installed to escalate to")
            return None
        ok, why = ready_now(self.e, nxt)
        if not ok:
            if not self.notified:
                self.notified = True
                self.e.say(f"[!] {self.role} is stalled on {cur}; not handing it to {nxt}: {why}")
                self.e.report(f"- {self.role} stalled on `{cur}`; `{nxt}` not used: {why}")
            return None
        self.e.pins[self.role] = nxt
        self.pinned = True
        self.e.say(f"[!] {self.role} is stalled on {cur}: handing the rest of this step to {nxt}")
        self.e.report(f"- Escalated `{self.role}`: `{cur}` -> `{nxt}` (stalled: the same failure twice). Back to `{cur}` after this step.")
        return nxt

    TRIAGE = [(r"SyntaxError|IndentationError|would not parse", "the code does not parse: fix the syntax first, change nothing else"),
              (r"ModuleNotFoundError|ImportError|No module named", "an import is missing or wrong: create or fix that module/import"),
              (r"NameError|AttributeError", "a name is used that does not exist: define it or use the right one"),
              (r"AssertionError|assert .* ==|expected .* got", "the code runs but returns the wrong value: compare the expected and actual values above"),
              (r"Timeout|timed out", "it hangs or is too slow: look for an endless loop or blocking call"),
              (r"FileNotFoundError|No such file", "a file it needs is missing: create it or fix the path")]

    @classmethod
    def triage(cls, failure):
        """Name the kind of failure in one line, by code (no model, no tokens), so the next try starts from the right question."""
        for pat, what in cls.TRIAGE:
            if re.search(pat, str(failure or "")):
                return what
        return ""

    def after(self, base_feedback, reply, failure, changed=()):
        """One call per failed attempt: observe, escalate if stuck, and return the feedback text for the next attempt (targeted when stalled)."""
        state = self.observe(reply, failure, changed, noop=getattr(self.e, "last_apply_noop", False))
        errs = getattr(self.e, "last_apply_errors", None) or []
        if errs and changed and "NOT applied" not in base_feedback:
            # some blocks applied and others did not: say which, or the model resends the broken one unchanged
            base_feedback = (base_feedback.rstrip() + "\nThese blocks of your last reply were NOT applied (fix and resend them):\n"
                             + "\n".join(errs[:8]) + "\n\n")
        kind = self.triage(failure)
        if kind and "FAILURE TYPE" not in base_feedback:
            base_feedback = base_feedback.rstrip() + f"\nFAILURE TYPE: {kind}.\n"
        if state == "progress":
            return base_feedback
        prev = self.e.model_for(self.role)
        nxt = self.escalate()
        extra = HINTS[state]
        if not nxt:
            # Nobody bigger to hand it to (none installed, or too slow to answer in time): the next attempt is a FRESH one -- a higher
            # temperature so the model stops resending the same code, and an explicit order to take another route. Seen live
            # (2026-11-04): the smoke test's 4B resent an identical wrong slugify twice and the run ended.
            fresh = getattr(self.e, "retry_temp", None)
            if fresh is None:
                fresh = self.e.retry_temp = {}
            fresh[self.role] = 0.8
            extra = ("FRESH ATTEMPT: your earlier approach did not work and resending it cannot pass. Start from the failing check: "
                     "state in one line what it expects versus what you produce, then write the code a DIFFERENT way. " + extra)
        if nxt:
            # a structured handover, not the raw history: the bigger model gets what it needs to take a different route
            extra = (f"HANDOVER: you are taking over this step from {prev}, which stalled ({state.replace('_', ' ')}). Its approach did "
                     "not work, so do not repeat it: re-read the failure below and the code it points at, then take a different route. "
                     + extra)
        if self.files:
            extra += " Files you have already changed in this step: " + ", ".join(sorted(self.files)[:8]) + "."
        return base_feedback + extra + "\n\n"

    def close(self):
        """End of the step: the role goes back to its own model (and its own temperature)."""
        (getattr(self.e, "retry_temp", None) or {}).pop(self.role, None)
        if self.pinned:
            self.e.pins.pop(self.role, None)
            self.pinned = False
