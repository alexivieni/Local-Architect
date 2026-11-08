"""Flows as data: a YAML file per flow (flows/<id>.yaml) interpreted over the engine's existing primitives, instead of a Python class per flow.

A flow document:
    id, title, blurb, kind, produces, domains, uses_roles
    budget: {calls, tokens, seconds, heavy_calls}        hard limits for the whole run (Engine.push_budget)
    context: {chars}                                     project-context budget for this flow's prompts
    steps: [step, ...]
    verdict: {pass_if: "<expr>", not_sure_if: "<expr>"}  expressions over the step outputs (safe evaluator, no eval())
A step:
    id, kind, title, when ("<expr>"), on_fail (continue | stop | not_sure), budget: {tokens, seconds, calls}
    kind: code      gate: <arch_gates name>, plus any keys the gate reads; output -> ctx[id]
    kind: generate  role, tier, prompt: <file under prompts/<flow>/>, vars: {...}, context: {chars, always: [...], query: "..."},
                    apply: {deny: [...], expect_new_under: "tests/", none_ok: bool}, verify: {gate, run_tests}, loop: {max, coach, escalate: [heavy]},
                    retries, rollback_on_fail, store_text: <ctx key>, strip_fence
    kind: ask_json  role, prompt, schema: <arch_schemas name>, store: <ctx key>
    kind: judge     what, criteria, evidence: {files: "<ctx key>", tests: bool}, requires: "<expr>"
    kind: doc       like generate, but the reply is DOC: blocks written as files; verify: {gate}
    kind: sample_n  n (default behavior.best_of_n), then a generate step's keys: N attempts in scratch copies, the first that passes its gate wins
    kind: cascade   tiers: [worker, heavy], then a generate step's keys: the same prompt up the tiers until the gate passes
    kind: chunked   files: "<ctx key>" or list, map: {role, prompt}, reduce: {role, prompt}, chunk_lines, store_text
    kind: report    path, template: "<text with {ctx fields}>"            writes a Markdown file through put_file (undoable)
    kind: handoff   fields: {...}
    kind: sub_flow  flow: <id>, params: {...}
The interpreter records a `step` trace record per step, `check` records per gate, and one `verdict` record per run: pass | fail | not_sure.
"""
import ast
import glob
import hashlib
import json
import os
import re
import string
import time


def _compact_turns(turns, full=4, short=10):
    """Context editing for the tool loop (what long-running frontier agents do with old tool results): the newest `full` steps
    verbatim, the next `short` cut to 600 characters, anything older to its first line. A small model's window then holds a long
    exploration instead of overflowing on old file dumps; nothing is lost from the trace, only from the prompt."""
    n, out = len(turns), []
    for i, t in enumerate(turns):
        age = n - i
        if age <= full:
            out.append(t)
        elif age <= full + short:
            out.append(t if len(t) <= 600 else t[:600] + " ... [older result shortened]")
        else:
            out.append(t.splitlines()[0][:160] if t else t)
    return "\n".join(out)

import arch_core as core
import arch_gates
from arch_core import BudgetExceeded, LLMError, LoopDetected

HERE = os.path.dirname(os.path.abspath(__file__))
FLOWS_DIR = os.path.join(HERE, "flows")
PROMPTS_DIR = os.path.join(HERE, "prompts")
KINDS = {"direct", "plan_execute_verify", "cascade", "best_of_n", "tool_loop", "chunked"}
# context keys the gates write: a step may not take one as its id, or the step's own output would hide it from the templates
RESERVED = {"scope", "facts", "hints", "findings", "findings_text", "repro_path", "repro_failure", "confirmed", "confirmed_text", "survivors", "evidence", "bench",
            "baseline", "best", "bench_detail", "bench_target", "plans", "plans_text", "scans_before", "sql_files", "metrics_before", "metrics_after", "pt_scope",
            "refused", "hosts", "attack_paths", "template", "template_files", "domains", "domain_files", "spec", "files", "problems_by_file", "mutation_before",
            "mutation_after", "test_target", "gaps_target", "existing_tests", "tool_calls", "tools", "goal", "params", "cfg", "steps", "budget", "env", "item", "reply", "handoff",
            "scan_rec", "static", "pinned", "tests_before", "kept", "rejected", "is_default_scope", "template_run"}
STEP_KINDS = {"code", "generate", "ask_json", "judge", "doc", "sample_n", "cascade", "chunked", "foreach", "tool_loop", "report", "handoff", "sub_flow"}


# ------------------------------------------------------------------------------------------------------ safe expressions
_ALLOWED = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.Not, ast.UnaryOp, ast.Compare, ast.Eq, ast.NotEq, ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.In, ast.NotIn,
            ast.Is, ast.IsNot, ast.Name, ast.Load, ast.Attribute, ast.Constant, ast.Subscript, ast.Call, ast.List, ast.Tuple, ast.Dict)
_FUNCS = {"len": len, "bool": bool, "int": int, "str": str, "any": any, "all": all, "min": min, "max": max}


class _Ctx(dict):
    """Attribute access over the flow context: `fix.ok`, `params.repro`, `cfg.behavior.evaluator`. Missing names are None, never an error."""

    def __getattr__(self, k):
        return _wrap(self.get(k, NOTHING))

    def __getitem__(self, k):
        return _wrap(dict.get(self, k, NOTHING))


def _wrap(v):
    if isinstance(v, dict) and not isinstance(v, _Ctx):
        return _Ctx(v)
    return v


class _Nothing:
    """What a flow expression gets for a step that has not run or a key no gate wrote: falsy, equal to None, and safe to reach through
    (`missing.thing.deeper` is still nothing), so a `when:` or `pass_if:` never crashes a run over an absent value."""

    def __getattr__(self, k):
        return self

    def __getitem__(self, k):
        return self

    def __bool__(self):
        return False

    def __contains__(self, item):          # without these, `'x' in NOTHING` walks __getitem__(0), (1), ... forever
        return False

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __eq__(self, other):
        return other is None or isinstance(other, _Nothing)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(None)

    def __repr__(self):
        return "nothing"


NOTHING = _Nothing()


class _Env(dict):
    """The names an expression may use. Anything absent reads as NOTHING rather than raising."""

    def __missing__(self, k):
        return NOTHING


def evaluate(expr, ctx):
    """A boolean/scalar expression over ctx, with no function calls beyond len/bool/any/all/min/max/int/str. Raises ValueError on anything else."""
    if expr in (None, "", True):
        return True
    if expr is False:
        return False
    tree = ast.parse(str(expr), mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ValueError(f"not allowed in a flow expression: {type(node).__name__} in {expr!r}")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in _FUNCS):
            raise ValueError(f"only {', '.join(_FUNCS)} may be called in a flow expression: {expr!r}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError(f"private attribute in a flow expression: {expr!r}")
    env = _Env(_FUNCS)
    env.update({k: _wrap(v) for k, v in ctx.items()})
    env["__builtins__"] = {}
    return eval(compile(tree, "<flow>", "eval"), env)      # the AST was whitelisted above: no names beyond the context and the six helpers; a name nothing answers is None


_MISSING = object()


class _Fmt(string.Formatter):
    """Prompt templates: `{goal}`, `{repro.failure}`, `{steps.fix.ok}` are resolved against the flow context, walking dots through plain
    dicts. A placeholder nothing answers is left exactly as it was, so a template never crashes a run and never silently drops a field."""

    def get_field(self, name, args, kwargs):
        if name in kwargs:
            return kwargs[name], name
        parts = name.split(".")
        cur = kwargs.get(parts[0], _MISSING)
        if cur is _MISSING:
            return "{" + name + "}", name
        for part in parts[1:]:
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return "{" + name + "}", name
        return cur, name

    def format_field(self, value, spec):
        if value is None:
            return ""
        if isinstance(value, list):
            return ", ".join(str(x) for x in value) if all(isinstance(x, str) for x in value) else json.dumps(value, default=str)[:4000]
        if isinstance(value, dict):
            return json.dumps(value, default=str)[:4000]
        try:
            return format(value, spec)
        except (ValueError, TypeError):                 # a number formatted as text, or the other way round: show it rather than lose the prompt
            return str(value)


_FMT = _Fmt()


def render(text, ctx, extra=None):
    """Fill a template from the flow context plus a step's own vars."""
    flat = dict(ctx)
    flat.update(extra or {})
    flat.setdefault("feedback", "")
    try:
        return _FMT.vformat(text, (), flat)
    except (ValueError, IndexError, KeyError, AttributeError, TypeError):
        return text


# ------------------------------------------------------------------------------------------------------ loading
def load_yaml_file(path):
    import arch_ci
    with open(path, encoding="utf-8") as f:
        return arch_ci.load_yaml(f.read())


def validate_steps(doc):
    """Every step of a flow document, including the sub-steps of a foreach."""
    steps = list(doc.get("steps") or [])
    for s in list(steps):
        if isinstance(s, dict) and s.get("kind") == "foreach":
            steps += list(s.get("steps") or [])
    return [s for s in steps if isinstance(s, dict)]


def validate(doc):
    probs = []
    if not isinstance(doc, dict):
        return ["the flow file is not a mapping"]
    for k in ("id", "title", "steps"):
        if not doc.get(k):
            probs.append(f"missing '{k}'")
    if doc.get("kind") and doc["kind"] not in KINDS:
        probs.append(f"unknown kind {doc['kind']!r} (one of {', '.join(sorted(KINDS))})")
    ids = set()
    steps = validate_steps(doc)
    for s in steps:
        if not isinstance(s, dict) or not s.get("id"):
            probs.append("a step has no id")
            continue
        if s["id"] in ids:
            probs.append(f"duplicate step id {s['id']!r}")
        if s["id"] in RESERVED:
            probs.append(f"step id {s['id']!r} would hide the context key of the same name (rename the step)")
        ids.add(s["id"])
        k = s.get("kind", "generate")
        if k not in STEP_KINDS:
            probs.append(f"step {s['id']}: unknown kind {k!r}")
        if k == "code" and s.get("gate") not in arch_gates.GATES:
            probs.append(f"step {s['id']}: unknown gate {s.get('gate')!r}")
        v = s.get("verify") or {}
        if v.get("gate") and v["gate"] not in arch_gates.GATES:
            probs.append(f"step {s['id']}: unknown verify gate {v['gate']!r}")
        if k in ("generate", "doc", "sample_n", "cascade") and s.get("prompt") and not os.path.isfile(prompt_path(doc.get("id"), s["prompt"])):
            probs.append(f"step {s['id']}: prompt file {prompt_path(doc.get('id'), s['prompt'])} is missing")
        if s.get("on_fail") and s["on_fail"] not in ("continue", "stop", "not_sure"):
            probs.append(f"step {s['id']}: on_fail must be continue | stop | not_sure")
        for key in ("when",):
            if s.get(key):
                try:
                    ast.parse(str(s[key]), mode="eval")
                except SyntaxError as ex:
                    probs.append(f"step {s['id']}: bad {key} expression ({ex})")
    stores = {x.get("store_text") or x.get("store") for x in steps if x.get("store_text") or x.get("store")}
    for st_ in sorted(stores & ids):
        probs.append(f"a step stores into {st_!r}, which is also a step id: the step's own output would hide it")
    for key in ("pass_if", "not_sure_if"):
        ex = (doc.get("verdict") or {}).get(key)
        if ex:
            try:
                ast.parse(str(ex), mode="eval")
            except SyntaxError as e_:
                probs.append(f"verdict.{key}: bad expression ({e_})")
    return probs


def prompt_path(flow_id, name):
    name = name if name.endswith(".md") else name + ".md"
    return os.path.join(PROMPTS_DIR, flow_id or "", name) if "/" not in name else os.path.join(PROMPTS_DIR, name)


def _scaled(budget, cfg):
    """A flow's budget times Settings > Flows > budget scale. A slow CPU-only machine needs more seconds for the same work; the calls and
    tokens scale too, so a bigger allowance is a deliberate choice and never an accident of one number."""
    k = float(((cfg or {}).get("flows") or {}).get("budget_scale") or 1.0)
    if abs(k - 1.0) < 1e-9:
        return dict(budget)
    out = dict(budget)
    for key in ("calls", "tokens", "seconds"):
        if out.get(key) is not None:
            out[key] = max(1, int(round(float(out[key]) * k)))
    return out


def prompt_sha(text):
    """A short, stable id for a prompt's exact bytes: the same prompt gives the same id on every machine, forever.

    Two things depend on this. A trace record can name the exact words that produced a reply, so a sample exported for training is never
    attributed to a prompt that has since been edited. And a run can prove its prompts were byte-identical across retries, which is the
    only reason the model server's prefix cache ever hits."""
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:12]


def load_prompt(flow_id, name, e=None):
    """One prompt file's bytes. Pass the engine and every call it then makes records which prompt file, and which version of it, it came from."""
    with open(prompt_path(flow_id, name), encoding="utf-8") as f:
        text = f.read()
    if e is not None:
        try:
            e.prompt_ref = f"{flow_id}/{name if name.endswith('.md') else name + '.md'}@{prompt_sha(text)}"
        except Exception:
            pass
    return text


def load_all(say=None):
    """Every flows/*.yaml -> {id: DataFlow}; a file that does not validate is reported and skipped, never half-registered."""
    out = {}
    for p in sorted(glob.glob(os.path.join(FLOWS_DIR, "*.yaml"))):
        try:
            doc = load_yaml_file(p)
        except Exception as ex:
            (say or print)(f"[flows] {os.path.basename(p)}: {ex}")
            continue
        probs = validate(doc)
        if probs:
            (say or print)(f"[flows] {os.path.basename(p)} skipped: " + "; ".join(probs[:4]))
            continue
        out[doc["id"]] = DataFlow(doc, p)
    return out


# ------------------------------------------------------------------------------------------------------ the interpreter
class DataFlow:
    """A Flow built from a YAML document. Same interface as arch_flows.Flow so routing, the hub and run_flow do not care."""

    def __init__(self, doc, path=None):
        self.doc, self.path = doc, path
        self.id, self.title = doc["id"], doc.get("title", doc["id"])
        self.blurb = doc.get("blurb", "")
        self.kind = doc.get("kind", "plan_execute_verify")
        self.domains = doc.get("domains") or ["api", "data", "web"]
        self.produces = doc.get("produces") or []
        self.uses_roles = doc.get("uses_roles") or sorted({s.get("role") for s in doc["steps"] if s.get("role")})
        self.steps = [{"id": s["id"], "title": s.get("title") or s["id"], "role": s.get("role") or "-", "kind": {"code": "code", "judge": "judge"}.get(s.get("kind", "generate"), "llm")} for s in doc["steps"]]
        self.data = True

    # ---- helpers
    def _say_step(self, e, step, title):
        e.say(f"\n--- {title} ---")

    def _context(self, e, ctx, step, extra_query=""):
        c = step.get("context") or {}
        q = render(c.get("query") or "{goal}", ctx) + (" " + extra_query if extra_query else "")
        always = []
        for a in c.get("always") or []:
            if a.startswith("ctx:"):
                v = ctx.get(a[4:]) or []
                always += [v] if isinstance(v, str) else list(v)
            elif os.path.isfile(os.path.join(e.root, a)):
                always.append(a)
        chars = c.get("chars") or (ctx.get("flow_ctx_chars"))
        return e.context(q, chars, always=always[:int(c.get("max_always", 4))])

    def _pin_tier(self, e, role, tier):
        """A step's `tier:` says which tier normally does this work. A model the person assigned to the role by name wins over it -- the
        same rule the engine applies everywhere else -- except for the heavy tier, which is an explicit escalation, not a default."""
        if not tier:
            return None
        if tier != "heavy" and ((e.cfg.get("roles") or {}).get(role) or "").strip():
            return None
        m = e.tier_model(tier)
        if m and e.pins.get(role) != m:
            prev = e.pins.get(role)
            e.pins[role] = m
            return ("set", prev)
        return None

    def _unpin(self, e, role, token):
        if token:
            if token[1]:
                e.pins[role] = token[1]
            else:
                e.pins.pop(role, None)

    def _gate(self, e, ctx, step, gate_name, changed, spec=None):
        fn = arch_gates.GATES[gate_name]
        t0 = time.time()
        try:
            r = fn(e, ctx, dict(step, **(spec or {})), changed)
        except (LLMError, BudgetExceeded, LoopDetected):
            raise
        except Exception as ex:                                       # a crashing check proves nothing: not sure, never pass
            r = arch_gates.R(None, f"the check '{gate_name}' crashed: {type(ex).__name__}: {str(ex)[:200]}")
        e._trace({"type": "check", "t": time.time(), "flow": e.flow, "step": step["id"], "gate": gate_name, "ok": r["ok"], "text": (r.get("text") or "")[:2000],
                  "metrics": r.get("metrics") or {}, "secs": round(time.time() - t0, 1)})
        return r

    # ---- step kinds
    def _run_generate(self, e, ctx, step, flow_id, prompt_text=None):
        role = step.get("role") or "generator"
        if role == "generator*":                                        # the coder that fits this piece of work: UI, SQL or backend
            import arch_flows
            item = ctx.get("item") if isinstance(ctx.get("item"), dict) else {}
            by_kind = {"ui": "generator_ui", "sql": "generator_sql", "backend": "generator"}.get(str(item.get("kind") or ""))
            role = by_kind or arch_flows.gen_role_for([x for x in (item.get("files") or ctx.get("scope") or []) if isinstance(x, str)], ctx.get("domains"))
        tpl = prompt_text or load_prompt(flow_id, step["prompt"], e)
        token = self._pin_tier(e, role, step.get("tier"))
        loop = step.get("loop") or {}
        tries = int(loop.get("max", e.cfg["limits"]["max_revisions"]) if loop else step.get("retries", 0)) + 1
        coach = None
        if loop and loop.get("coach", True):
            import arch_progress
            coach = arch_progress.Coach(e, role)
        feedback, changed_all, out = "", [], {"ok": False, "changed": [], "tries": 0}
        apply_spec = step.get("apply")
        verify_spec = step.get("verify") or {}
        deny = [render(d, ctx) for d in (apply_spec or {}).get("deny") or []]
        deny = [d for d in deny if d and not d.startswith("{")]
        if (apply_spec or {}).get("deny_ctx"):                               # a list of paths from the context (the test files that already exist, say)
            deny += [str(x) for x in (ctx.get(apply_spec["deny_ctx"]) or []) if isinstance(x, str)]
        try:
            for attempt in range(tries):
                out["tries"] = attempt + 1
                extra = {k: (render(v, ctx) if isinstance(v, str) else v) for k, v in (step.get("vars") or {}).items()}
                extra["feedback"] = feedback
                extra["attempt"] = attempt + 1
                if "{context}" in tpl:
                    extra["context"] = self._context(e, ctx, step, render(step.get("context", {}).get("extra", ""), ctx))
                user = render(tpl, ctx, extra)
                label = f"{step['id']}-try{attempt + 1}" if tries > 1 else step["id"]
                cap = None
                if step.get("rollback_on_fail"):
                    import arch_flows_pro as pro
                    cap = pro.capture(e)
                r = e.call(role, user, label=label, think=step.get("think"))
                text = core.strip_think(r["text"]).strip()
                if step.get("strip_fence"):
                    text = re.sub(r"^```(?:markdown|md)?\n|\n```$", "", text)
                if step.get("store_text"):
                    ctx[step["store_text"]] = text
                    out["text"] = text
                if apply_spec is None and "apply" not in step and not verify_spec:   # a pure text step (a review, a detection list)
                    e.mark(r["id"], True)
                    out["ok"] = True
                    break
                no_apply = "apply" in step and not apply_spec                    # `apply: null`: the reply is not FILE blocks (a whole JSON file, say); the gate writes it
                if no_apply:
                    applied = []
                else:
                    e.deny_prefixes = deny
                    res = e.apply(r["text"], label)
                    e.deny_prefixes = []
                    if any(x["status"] == "quit" for x in res):
                        out["stopped"] = True
                        return out
                    if e.review == "dry":
                        out["dry"], out["ok"] = True, True
                        return out
                    applied = [x["rel"] for x in res if x["status"] in ("applied", "identical")]
                    under = (apply_spec or {}).get("expect_new_under")
                    if under:
                        applied = [x for x in applied if x.startswith(under)]
                if not applied and not no_apply:
                    feedback = "FEEDBACK: none of your changes could be applied:\n" + "\n".join(f"- {x['rel']}: {x['msg']}" for x in res if x.get("msg")) + "\nResend using the exact FILE:/EDIT:/DEF: formats.\n\n"
                    e.mark(r["id"], False, feedback)
                    if coach:
                        feedback = coach.after(feedback, r["text"], feedback, [])
                    continue
                changed_all = sorted(set(changed_all + applied))
                out["changed"] = changed_all
                gate_name = verify_spec.get("gate")
                if not gate_name:
                    e.mark(r["id"], True)
                    out["ok"] = True
                    break
                g = self._gate(e, ctx, step, gate_name, applied, verify_spec)
                self._log_gate(e, f"{step['id']} try {attempt + 1}", g)
                e.mark(r["id"], g["ok"], g.get("text", ""))      # g["ok"] may be None ("could not run"), which must stay None, not become False
                if g["ok"] is None:
                    out.update(ok=False, unsure=True, why=g.get("text"))
                    break
                if g["ok"]:
                    out["ok"] = True
                    out.update({k: v for k, v in (g.get("metrics") or {}).items() if k not in out})
                    break
                if attempt + 1 < tries and verify_spec.get("recheck", e.cfg["behavior"].get("recheck_before_retry", True)):
                    # Before paying for another model call, ask the same check the same question again, with nothing changed. If it now
                    # passes, the check is flaky: the honest verdict is "not sure", and another attempt would only be a coin toss.
                    g2 = self._gate(e, ctx, step, gate_name, applied, verify_spec)
                    if g2["ok"] is True:
                        why = (f"the '{gate_name}' check failed and then passed with nothing changed, so it is flaky; this step cannot be "
                               "called passed or failed until that check is reliable")
                        e.say(f"[check] {why}")
                        e.mark(r["id"], None, why)
                        e._trace({"type": "flaky", "t": time.time(), "flow": self.id, "step": step["id"], "gate": gate_name,
                                  "first": (g.get("text") or "")[:400]})
                        out.update(ok=False, unsure=True, flaky=True, why=why)
                        break
                if cap is not None:
                    import arch_flows_pro as pro
                    pro.restore(e, cap)
                    changed_all = [c for c in changed_all if c not in applied]
                fb = "FEEDBACK: your last change failed these checks. Fix them (smallest change):\n" + (g.get("text") or "") + "\n\n"
                feedback = coach.after(fb, r["text"], g.get("text") or "", applied) if coach else fb
                if coach and loop.get("escalate") and coach.stalled >= 1 and not coach.pinned:
                    for tier in loop["escalate"]:
                        m = e.tier_model(tier)
                        if m and m != e.model_for(role):
                            ok, why = arch_progress.ready_now(e, m)
                            if not ok:                          # it cannot answer in time here (measured): the step stays where it is
                                e.say(f"[!] {role} is stalled; not handing it to the {tier} tier ({m}): {why}")
                                continue
                            was = e.model_for(role)             # before the pin, or the "from" model would read back as the new one
                            e.pins[role] = m
                            coach.pinned = True
                            e.say(f"[!] {role} is stalled: handing the rest of this step to the {tier} tier ({m})")
                            e.report(f"- Escalated `{role}` to the {tier} tier (`{m}`) for the rest of step {step['id']}")
                            e.meter["escalations"] = e.meter.get("escalations", 0) + 1
                            # Recorded, not just printed: "the small model could not do this and the big one could" is the single most
                            # valuable lesson a local setup produces, and it is what teaches the worker the cases it keeps failing.
                            e._trace({"type": "escalation", "t": time.time(), "flow": self.id, "step": step["id"], "role": role,
                                      "from_model": was, "to_model": m, "to_tier": tier,
                                      "after_call": r["id"], "why": (g.get("text") or "")[:400], "tries": attempt + 1})
                            break
            out["feedback"] = feedback
        finally:
            if coach:
                coach.close()
            self._unpin(e, role, token)
            e.deny_prefixes = []
        return out

    def _log_gate(self, e, title, g):
        m = g.get("metrics") or {}
        e.say(f"[check] {title}: {'ok' if g['ok'] else ('could not run' if g['ok'] is None else 'FAILED')}" + (f" ({m['passed']}/{m['total']})" if "passed" in m else "") + ("" if g["ok"] else "\n" + (g.get("text") or "")[:600]))
        e.report(f"  - {title}: {'passed' if g['ok'] else ('not sure (' + (g.get('text') or '')[:120] + ')' if g['ok'] is None else 'failed: ' + (g.get('text') or '')[:200])}")

    def _run_ask_json(self, e, ctx, step, flow_id):
        import arch_schemas
        role = step.get("role") or "planner"
        token = self._pin_tier(e, role, step.get("tier"))
        try:
            extra = dict(step.get("vars") or {})
            tpl = load_prompt(flow_id, step["prompt"], e)
            if "{context}" in tpl:
                extra["context"] = self._context(e, ctx, step)
            schema = getattr(arch_schemas, step["schema"], None) if isinstance(step.get("schema"), str) else step.get("schema")
            if callable(schema):
                import arch_flows
                schema = schema(arch_flows.FLOWS)
            r, j = e.ask_json(role, render(tpl, ctx, extra), schema=schema)
            e.mark(r["id"], j is not None)
            if step.get("store"):
                ctx[step["store"]] = j
            return {"ok": j is not None, "json": j, "model": r["model"]}
        finally:
            self._unpin(e, role, token)

    def _run_judge(self, e, ctx, step, flow_id):
        import arch_flows
        if step.get("requires") and not evaluate(step["requires"], ctx):
            return {"ok": True, "skipped": True, "verdict": "skipped"}
        if e.cfg["behavior"].get("evaluator") == "off":
            return {"ok": True, "skipped": True, "verdict": "off"}
        ev_spec = step.get("evidence") or {}
        files = ctx.get(ev_spec.get("files")) if ev_spec.get("files") else (e.changed_files or [])
        if isinstance(files, dict):
            files = files.get("changed") or []
        ev = "\n".join(f"=== {f} ===\n{core.read_text(os.path.join(e.root, f), 9000)[0]}" for f in (files or [])[:6] if os.path.isfile(os.path.join(e.root, f)))
        if ev_spec.get("tests", True):
            t = core.run_tests(e.root, e.cfg)
            ev += f"\n=== test result: {t[0]} ===\n{(t[1] or '')[-1500:]}"
        if ev_spec.get("text"):
            ev = render(ev_spec["text"], ctx) + "\n\n" + ev
        token = self._pin_tier(e, "evaluator", step.get("tier"))
        try:
            j = arch_flows.judge(e, render(step.get("what", "the work"), ctx), ev, render(step.get("criteria", "the goal is met"), ctx))
        finally:
            self._unpin(e, "evaluator", token)
        e.say(f"[judge] {j['verdict']} avg={j['avg']}")
        e.report(f"- Judge ({step['id']}): {j['verdict']} (avg {j['avg']})" + (f"; issues: {'; '.join(j['issues'])[:300]}" if j["issues"] else ""))
        out = {"ok": j["verdict"] == "pass", "verdict": j["verdict"], "issues": j["issues"], "avg": j["avg"]}
        if j["verdict"] == "unknown":
            out["unsure"], out["ok"] = True, False                      # an unusable judge reply is "not sure", never a pass
        return out

    def _run_doc(self, e, ctx, step, flow_id):
        import arch_flows
        role = step.get("role") or "architect"
        token = self._pin_tier(e, role, step.get("tier"))
        tpl = load_prompt(flow_id, step["prompt"], e)
        loop = step.get("loop") or {}
        tries = int(loop.get("max", e.cfg["limits"]["max_revisions"]) if loop else 0) + 1
        feedback, written, last = "", [], ""
        out = {"ok": False, "written": []}
        try:
            for attempt in range(tries):
                extra = dict(step.get("vars") or {})
                extra.update(feedback=feedback, previous=last[:9000])
                if "{context}" in tpl:
                    extra["context"] = self._context(e, ctx, step)
                r = e.call(role, render(tpl, ctx, extra), label=f"{step['id']}-try{attempt + 1}" if tries > 1 else step["id"])
                last = r["text"]
                docs = arch_flows.parse_docs(r["text"])
                if not docs:
                    feedback = "No DOC: files were found. Use the exact 'DOC: path' format."
                    e.mark(r["id"], False, feedback)
                    continue
                for path, content in docs.items():
                    p = e.put_file(path, content, "design file")
                    if p["status"] in ("applied", "identical") and path not in written:
                        written.append(path)
                out["written"] = written
                gname = (step.get("verify") or {}).get("gate")
                g = self._gate(e, ctx, step, gname, written) if gname else arch_gates.R(True)
                self._log_gate(e, f"{step['id']} round {attempt + 1}", g)
                judged = None
                if g["ok"] and step.get("judge"):
                    judged = self._run_judge(e, ctx, dict(step["judge"], id=step["id"] + "-judge", evidence={"text": "SPEC:\n{spec}\n\n" + g.get("evidence", ""), "tests": False}), flow_id)
                ok = bool(g["ok"]) and (judged is None or judged.get("ok") or judged.get("verdict") in ("skipped", "off"))
                e.mark(r["id"], ok, g.get("text") or "; ".join((judged or {}).get("issues") or []))
                if ok:
                    out["ok"] = True
                    out["judge"] = (judged or {}).get("verdict")
                    break
                if judged and judged.get("unsure"):
                    out["unsure"] = True
                feedback = ("Your previous output was rejected. Fix ALL of these and resend every file in full:\n" + (g.get("text") or "") + "\n" + "\n".join("- " + i for i in (judged or {}).get("issues") or [])).strip()
        finally:
            self._unpin(e, role, token)
        return out

    def _run_sample_n(self, e, ctx, step, flow_id):
        """Best of N: several answers to the SAME prompt, each one applied and checked in code, the best one kept.

        Not "retry until something passes" -- every candidate is judged, so the step keeps the best of what the model could do rather than
        the first thing that scraped through. Two things fall out of that:
          * when the model server holds several conversations at once (Settings > Server > Parallel), the candidates are generated side
            by side, so N answers cost roughly the time of one;
          * every candidate is marked passed or failed against the same prompt, which is the cleanest preference data there is: the
            training export turns "this answer beat that one, to this exact question" into a pair with no guesswork.
        """
        import arch_flows_pro as pro
        import threading
        n = max(1, int(step.get("n") or e.cfg["behavior"].get("best_of_n") or 1))
        if n <= 1:
            return self._run_generate(e, ctx, dict(step, kind="generate"), flow_id)
        role = step.get("role") or "generator"
        tpl = load_prompt(flow_id, step["prompt"], e)
        token = self._pin_tier(e, role, step.get("tier"))
        verify_spec = step.get("verify") or {}
        gate_name = verify_spec.get("gate")
        deny = [d for d in (render(d, ctx) for d in ((step.get("apply") or {}).get("deny") or [])) if d and not d.startswith("{")]
        try:
            extra = {k: (render(v, ctx) if isinstance(v, str) else v) for k, v in (step.get("vars") or {}).items()}
            extra.update(feedback="", attempt=1)
            if "{context}" in tpl:
                extra["context"] = self._context(e, ctx, step, render(step.get("context", {}).get("extra", ""), ctx))
            base = render(tpl, ctx, extra)
            import arch_flows as fl
            # the slots the server answering this role really has (a llama-server's own --parallel; else Settings > Server > Parallel)
            par = max(1, min(n, fl.bon_slots(e, role)))
            base_t = float(step.get("temperature", 0.7))
            replies = [None] * n

            def ask(i):
                # The prompt is identical; only the candidate number at the very END differs, so the server's prefix cache still covers
                # all of it, and the engine does not mistake N deliberate samples for a model stuck in a loop.
                return (role, base + f"\n\n(candidate {i + 1} of {n})",
                        {"label": f"{step['id']}-cand{i + 1}", "think": step.get("think"), "temperature": min(1.2, base_t + 0.1 * i)})

            def one(i):
                r_, text_, kw_ = ask(i)
                replies[i] = e.call(r_, text_, **kw_)

            def alone(i):                                # the one-after-another path, exactly as it has always run
                t = threading.Thread(target=one, args=(i,))
                t.start()
                t.join()
            e.say(f"[best of {n}] {'generating ' + str(par) + ' at a time' if par > 1 else 'generating one after another'}")
            if par < 2:
                for i in range(n):
                    alone(i)
            else:
                # Side by side (report Eq. 21): a batch is never larger than the calls the budgets still allow (sized BEFORE sending,
                # since requests launched together all pass the per-call check at once); a candidate whose concurrent request failed,
                # or a batch that would hold fewer than two, runs on its own path above.
                lo = 0
                while lo < n:
                    nb = min(par, n - lo, fl.bon_calls_left(e))
                    if nb < 2:
                        alone(lo)
                        lo += 1
                        continue
                    try:
                        got = fl.bon_generate(e, [ask(j) for j in range(lo, lo + nb)], nb)
                    except Exception as ex:          # the side-by-side machinery itself failed: every one of them, on its own
                        got = [(None, ex)] * nb
                    for j, (r_, ex_) in zip(range(lo, lo + nb), got):
                        if ex_ is None:
                            replies[j] = r_
                        elif not isinstance(ex_, KeyboardInterrupt):
                            e.say(f"[best of {n}] candidate {j + 1} failed side by side ({type(ex_).__name__}); asking again on its own")
                            alone(j)
                        else:
                            raise ex_
                    lo += nb

            cap = pro.capture(e)
            scored = []
            for i, r in enumerate(replies):
                pro.restore(e, cap)
                if not r:
                    continue
                e.deny_prefixes = deny
                res = e.apply(r["text"], f"{step['id']}-cand{i + 1}")
                e.deny_prefixes = []
                applied = [x["rel"] for x in res if x["status"] in ("applied", "identical")]
                if not applied:
                    e.mark(r["id"], False, "none of its changes could be applied")
                    scored.append({"i": i, "ok": False, "r": r, "why": "nothing applied", "size": 0, "applied": []})
                    continue
                g = self._gate(e, ctx, step, gate_name, applied, verify_spec) if gate_name else {"ok": True, "text": ""}
                size = sum(len(x.get("new") or "") for x in res) if res and isinstance(res[0], dict) else len(r["text"])
                scored.append({"i": i, "ok": g["ok"], "r": r, "why": g.get("text") or "", "size": size or len(r["text"]),
                               "applied": applied, "metrics": g.get("metrics") or {}})
            pro.restore(e, cap)

            passed = [x for x in scored if x["ok"] is True]
            # Among the ones that passed, the smallest change wins: the same result for less edited code is easier to review.
            winner = min(passed, key=lambda x: (x["size"], x["i"])) if passed else None
            for x in scored:
                e.mark(x["r"]["id"], (True if x is winner else (None if x["ok"] is None else False)),
                       "" if x is winner else ("passed, but a smaller change also passed" if x["ok"] else x["why"][:300]))
            e._trace({"type": "best_of_n", "t": time.time(), "flow": self.id, "step": step["id"], "n": n, "parallel": par,
                      "passed": len(passed), "winner": (winner["i"] + 1) if winner else None})
            if not winner:
                unsure = any(x["ok"] is None for x in scored)
                return {"ok": False, "unsure": unsure, "sampled": n, "passed": 0,
                        "why": "none of the %d answers passed the checks" % n}
            res = e.apply(winner["r"]["text"], f"{step['id']}-best")
            changed = [x["rel"] for x in res if x["status"] in ("applied", "identical")]
            e.say(f"[best of {n}] {len(passed)} passed; kept candidate {winner['i'] + 1}, the smallest change")
            out = {"ok": True, "changed": changed, "sampled": n, "passed": len(passed), "winner": winner["i"] + 1, "tries": n}
            out.update({k: v for k, v in winner["metrics"].items() if k not in out})
            return out
        finally:
            self._unpin(e, role, token)

    def _run_cascade(self, e, ctx, step, flow_id):
        tiers = step.get("tiers") or ["worker", "heavy"]
        last = {"ok": False}
        for t in tiers:
            if t == "heavy" and e.meter.get("heavy_calls", 0) >= int((ctx.get("budget") or {}).get("heavy_calls", 3)):
                e.say("[cascade] heavy budget used up; not escalating")
                break
            m = e.tier_model(t)
            if not m:
                continue
            e.say(f"[cascade] trying the {t} tier ({m})")
            last = self._run_generate(e, ctx, dict(step, kind="generate", tier=t, id=f"{step['id']}@{t}"), flow_id)
            if last.get("ok"):
                last["tier"] = t
                return last
        return last

    def _run_chunked(self, e, ctx, step, flow_id):
        import arch_index
        files = ctx.get(step["files"]) if isinstance(step.get("files"), str) else (step.get("files") or ctx.get("scope") or [])
        n = int(step.get("chunk_lines") or 120)
        role = (step.get("map") or {}).get("role") or "reviewer"
        token = self._pin_tier(e, role, step.get("tier"))
        parts = []
        try:
            for f in files[:int(step.get("max_files", 6))]:
                text = core.read_text(os.path.join(e.root, f), 300000)[0]
                for c in arch_index._window_chunks(f, text, n):
                    tpl = load_prompt(flow_id, step["map"]["prompt"], e)
                    r = e.call(role, render(tpl, ctx, {"chunk": c["text"], "path": f, "where": c["name"]}), label=f"{step['id']}-map")
                    parts.append(f"### {f} ({c['name']})\n{core.strip_think(r['text']).strip()}")
                    e.mark(r["id"], True)
            red = step.get("reduce")
            if red and parts:
                tpl = load_prompt(flow_id, red["prompt"], e)
                r = e.call(red.get("role") or role, render(tpl, ctx, {"parts": "\n\n".join(parts)[:60000]}), label=f"{step['id']}-reduce")
                text = core.strip_think(r["text"]).strip()
                e.mark(r["id"], True)
            else:
                text = "\n\n".join(parts)
        finally:
            self._unpin(e, role, token)
        if step.get("store_text"):
            ctx[step["store_text"]] = text
        return {"ok": bool(parts), "chunks": len(parts), "text": text}

    def _run_foreach(self, e, ctx, step, flow_id):
        """Run a list of sub-steps once per item (build's sprints, hunt's confirmed bugs). The item is ctx['item'] while it runs;
        `stop_on_fail` ends the loop at the first item that fails, as a build does when a sprint will not pass."""
        items = ctx.get(step["over"]) if isinstance(step.get("over"), str) else (step.get("over") or [])
        if isinstance(items, dict):
            items = items.get(step.get("key") or "items") or []
        items = [x for x in (items or step.get("fallback") or []) if x is not None][: int(step.get("max", 8))]
        done, ok_all, prev = [], True, ctx.get("item")
        skip_done = set((ctx.get("params") or {}).get("resume", {}).get("done") or [])
        for i, item in enumerate(items, 1):
            name = (item.get("name") if isinstance(item, dict) else str(item)) or f"{step['id']} {i}"
            if name in skip_done:
                e.say(f"[resume] {name}: already passed, skipped")
                done.append({"name": name, "ok": True, "skipped": True})
                continue
            ctx["item"], ctx["item_n"], ctx["item_total"] = _wrap(item) if isinstance(item, dict) else item, i, len(items)
            acc = [a for a in ((item or {}).get("acceptance") or []) if isinstance(a, dict)] if isinstance(item, dict) else []
            ctx["item_criteria"] = "\n".join(f"- {a.get('id', '?')}: {a.get('text', '')}" for a in acc) or "- the goal works and tests pass"
            ctx["item_files"] = ", ".join(str(x) for x in ((item or {}).get("files") or []) if isinstance(x, str)) if isinstance(item, dict) else ""
            ctx["item_goal"] = (item.get("goal") if isinstance(item, dict) else None) or ctx["goal"]
            ctx["item_text"] = ""
            if isinstance(item, str) and os.path.isfile(os.path.join(e.root, item)):
                ctx["item_text"] = core.read_text(os.path.join(e.root, item), 60000)[0]
            e.say(f"\n=== {step.get('title') or step['id']} {i}/{len(items)}: {name} ===")
            e.report(f"### {name}")
            sub_ok, sub_out = True, {}
            for sub in step["steps"]:
                if sub.get("when") and not evaluate(sub["when"], ctx):
                    continue
                out = self._dispatch(e, ctx, sub)
                ctx["steps"][f"{sub['id']}@{i}"] = out
                ctx[sub["id"]] = out
                sub_out[sub["id"]] = out
                e._trace({"type": "step", "t": time.time(), "flow": self.id, "step": f"{step['id']}[{name}].{sub['id']}", "ok": out.get("ok"), "unsure": bool(out.get("unsure"))})
                if out.get("stopped") or out.get("dry"):
                    ctx["item"] = prev
                    return dict(out, items=done)
                if not out.get("ok"):
                    sub_ok = False
                    if sub.get("on_fail", "stop") != "continue":
                        break
            done.append({"name": name, "ok": sub_ok, "changed": sorted({c for o in sub_out.values() for c in (o.get("changed") or [])})})
            ok_all = ok_all and sub_ok
            e.checkpoint(**{step["id"] + "_done": [d["name"] for d in done if d["ok"]], "files": e.changed_files})
            e.report(f"- Result: {'passed its checks' if sub_ok else 'FAILED its checks'}", "")
            if not sub_ok and step.get("stop_on_fail", True):
                e.say(f"[!] '{name}' still failing; stopping so you can look at it")
                break
        ctx["item"] = prev
        ctx[step["id"] + "_results"] = done
        return {"ok": ok_all and bool(done), "items": done, "n": len(done), "passed": sum(1 for d in done if d["ok"])}

    def _run_tool_loop(self, e, ctx, step, flow_id):
        """The model works by calling tools (look, search, read, run the tests, change files, remember) until it answers or runs out of
        steps. Its reply is constrained to one tool call or a final answer, and validated again in code: an unusable reply costs a step
        and is explained, never guessed at."""
        import arch_schemas
        import arch_tools
        role = step.get("role") or "generator"
        allowed = step.get("tools") or list(arch_tools.TOOLS)
        if not step.get("allow_writes", True):
            allowed = [t for t in allowed if not arch_tools.TOOLS[t]["writes"]]
        import arch_roleprofile as _rp
        allowed, _removed = _rp.tool_allow(e.cfg, role, allowed)       # the role profile's allow-list (off = unchanged)
        if _removed:
            e.say(f"    [tools] role profile removed: {', '.join(_removed)}")
        token = self._pin_tier(e, role, step.get("tier"))
        tpl = load_prompt(flow_id, step["prompt"], e)
        max_steps = int(step.get("max_steps", 8))
        # An EMPTY list means "no tools" here; arch_tools.schema/call read an empty list as "all", so it is handled before them.
        schema = (arch_tools.schema(allowed) if allowed else
                  {"type": "object", "properties": {"final": {"type": "string"}}, "required": ["final"]})
        turns, used, answer, transcript, seen_calls = [], [], None, [], set()
        try:
            extra = {k: (render(v, ctx) if isinstance(v, str) else v) for k, v in (step.get("vars") or {}).items()}
            extra["tools"] = arch_tools.describe(allowed) if allowed else "(no tools: answer from what you were shown)"
            extra["max_steps"] = max_steps
            if "{context}" in tpl:
                extra["context"] = self._context(e, ctx, step)
            base = render(tpl, ctx, extra)
            for i in range(max_steps):
                last = i == max_steps - 1
                user = base + ("\n\nWHAT YOU HAVE DONE SO FAR:\n" + _compact_turns(turns) if turns else "") + \
                    ("\n\nTHIS IS YOUR LAST STEP: answer now, from what you have seen, with {\"final\": \"...\"}." if last else "")
                r, j = e.ask_json(role, user, schema=schema)
                if not isinstance(j, dict):
                    turns.append(f"[step {i + 1}] your reply was not usable; answer with one tool call or your final answer")
                    e.mark(r["id"], False, "unusable reply")
                    continue
                if j.get("final") and not j.get("tool"):
                    answer = str(j["final"])
                    e.mark(r["id"], True)
                    break
                name, args = str(j.get("tool") or ""), j.get("args") or {}
                sig = name + json.dumps(args, sort_keys=True, default=str)
                if not allowed:
                    out, ok = f"'{name}' is not allowed: the {role} role has no tools in this step. Answer with {{\"final\": \"...\"}}.", False
                else:
                    ok_, why = _rp.check_tool(e.cfg, role, name, args)
                    out, ok = (arch_tools.call(e, name, args, allowed) if ok_ else (why, False))
                if sig in seen_calls:                              # the same call gets the same answer: say so rather than let it loop
                    out += ("\n\nYou have already made this exact call and this is the same answer. Do something different: another tool, or the "
                            "same tool with different arguments. If you have enough to answer, answer.")
                seen_calls.add(sig)
                used.append(name)
                e.mark(r["id"], ok, "" if ok else out[:200])
                e._trace({"type": "tool", "t": time.time(), "flow": self.id, "step": step["id"], "tool": name,
                          # `call` is the id of the model turn that asked for this tool. Without it a trajectory can only be rebuilt from
                          # timestamp order, which is guesswork, and the tool loop is the one thing worth exporting as a multi-turn sample.
                          "call": r["id"], "turn": i + 1,
                          "args": json.loads(json.dumps(args, default=str)), "ok": ok, "result": out[:2000]})
                e.say(f"    [tool] {name}({', '.join(f'{k}={str(v)[:30]}' for k, v in args.items())}) -> {out.splitlines()[0][:90] if out else ''}")
                turns.append(f"[step {i + 1}] {name}({json.dumps(args, default=str)[:300]})\nRESULT:\n{out}")
                transcript.append({"tool": name, "args": args, "result": out[:2000]})
            if answer is None and turns:                          # out of steps but the evidence is gathered: ask for the answer rather than throw the work away
                r, j = e.ask_json(role, base + "\n\nWHAT YOU FOUND:\n" + "\n".join(turns) +
                                  "\n\nYou are out of steps. Answer now from what you found, with {\"final\": \"...\"}. Name the files and lines you saw, "
                                  "and say plainly what you could not establish.", schema={"type": "object", "properties": {"final": {"type": "string"}}, "required": ["final"]})
                answer = str((j or {}).get("final") or "").strip() or "stopped: step limit reached without an answer"
                e.mark(r["id"], bool((j or {}).get("final")))
        finally:
            self._unpin(e, role, token)
        if step.get("store_text"):
            ctx[step["store_text"]] = answer or ""
        ctx["tool_calls"] = used
        gate_name = (step.get("verify") or {}).get("gate")
        g = self._gate(e, ctx, step, gate_name, sorted(set(e.changed_files))) if gate_name else None
        if g is not None:
            self._log_gate(e, step.get("title") or step["id"], g)
        ok = (g["ok"] if g is not None else (answer is not None and not str(answer).startswith("stopped:")))
        return {"ok": bool(ok), "unsure": bool(g and g["ok"] is None), "answer": answer, "tools": used, "steps_used": len(turns), "transcript": transcript,
                "why": (g.get("text") if g else None) if not ok else None}

    def _run_report(self, e, ctx, step):
        ctx["files"] = sorted(e.changed_files)
        body = render(step["template"], ctx)
        p = e.put_file(render(step["path"], ctx), body if body.endswith("\n") else body + "\n", step.get("label", "report"), quiet=True)
        e.report("", f"- Report: {render(step['path'], ctx)} ({p['status']})")
        return {"ok": p["status"] in ("applied", "identical"), "path": render(step["path"], ctx)}

    # ---- run
    def run(self, e, params):
        import arch_flows
        doc = self.doc
        params = dict(params or {})
        budget = dict(doc.get("budget") or {})
        prof = ctx_profile(e)
        for over_name, over in (doc.get("overrides") or {}).items():
            if over_name == prof:
                budget.update(over.get("budget") or {})
        ctx = {"goal": e.goal, "params": params, "cfg": e.cfg, "steps": {}, "budget": budget, "flow": self.id, "profile": prof,
               "env": {"can_test": core.has_module("pytest") or bool(e.cfg["tests"].get("command")), "empty": not core.list_files(e.root, e.cfg)},
               "domains": params.get("domains") or arch_flows.detect_domains(e, e.goal), "flow_ctx_chars": (doc.get("context") or {}).get("chars")}
        ctx["handoff"] = arch_flows.handoff_text(e)
        e.push_budget(f"flow {self.id}", **_scaled(budget, e.cfg))
        e.report(f"- Flow definition: `{os.path.basename(self.path or self.id)}` (data)", "")
        verdict, stop_reason = None, None
        skip = set((doc.get("overrides") or {}).get(prof, {}).get("skip") or [])
        try:
            e.todo_set([{"id": s["id"], "title": s.get("title") or s["id"]} for s in doc["steps"]])
        except Exception:
            pass
        res = params.get("resume") or {}
        done_before, outs_before = set(res.get("steps_done") or []), res.get("step_outputs") or {}
        steps_done, step_outputs = [], {}
        try:
            step_ids = {s_["id"] for s_ in doc["steps"]}
            for step in doc["steps"]:
                sid = step["id"]
                try:                                      # the person's edits to the plan, read before every step
                    ov = e.plan_override()
                    skip |= ov["skipped"]
                    for aid in ov["added"]:               # an item the person added is not a step of this flow: say so, do not leave it pending
                        if aid not in step_ids:
                            e.todo_mark(aid, "skipped")
                            e.say(f"[plan] added item {aid!r} is not a step of the {self.id} flow; marked skipped")
                except Exception:
                    pass
                if sid in done_before and isinstance(outs_before.get(sid), dict):
                    # resumed run: this step passed before the interruption, and its files are already on disk
                    ctx["steps"][sid] = ctx[sid] = dict(outs_before[sid])
                    steps_done.append(sid)
                    step_outputs[sid] = outs_before[sid]
                    e.todo_mark(sid, "done")
                    e.say(f"[resume] {step.get('title') or sid}: already passed, skipped")
                    e._trace({"type": "step", "t": time.time(), "flow": self.id, "step": sid, "status": "resumed", "ok": True})
                    continue
                if sid in skip:
                    e.todo_mark(sid, "skipped")
                    ctx["steps"][sid] = {"ok": True, "skipped": True}
                    ctx[sid] = ctx["steps"][sid]
                    continue
                try:
                    if step.get("when") and not evaluate(step["when"], ctx):
                        ctx["steps"][sid] = {"ok": True, "skipped": True}
                        ctx[sid] = ctx["steps"][sid]
                        e._trace({"type": "step", "t": time.time(), "flow": self.id, "step": sid, "status": "skipped"})
                        e.todo_mark(sid, "skipped")
                        continue
                except ValueError as ex:
                    e.say(f"[flow] bad 'when' on step {sid}: {ex}; running the step")
                self._say_step(e, step, step.get("title") or sid)
                e.todo_mark(sid, "doing")
                e.report(f"## {step.get('title') or sid}")
                t0 = time.time()
                sb = step.get("budget") or {}
                if sb:
                    e.push_budget(f"step {sid}", **_scaled(sb, e.cfg))
                try:
                    out = self._dispatch(e, ctx, step)
                except BudgetExceeded as ex:
                    out = {"ok": False, "unsure": True, "why": str(ex), "budget_exceeded": True}
                    e.say(f"[!] {ex}")
                    e.report(f"- Budget: {ex}")
                except LoopDetected as ex:
                    # The model kept giving the same answer to the same question. That is a finding about this step, not a crash:
                    # the step fails, the run carries on to its verdict and its report, and the trace says why.
                    out = {"ok": False, "why": str(ex), "looped": True}
                    e.say(f"[!] {ex}")
                    e.report(f"- Stopped repeating: {ex}")
                finally:
                    if sb:
                        e.pop_budget()
                ctx["steps"][sid] = out
                ctx[sid] = out
                e.todo_mark(sid, "done" if out.get("ok") else "failed")
                if out.get("ok") and not out.get("dry"):
                    steps_done.append(sid)
                    try:
                        step_outputs[sid] = json.loads(json.dumps(out, default=str))
                        e.checkpoint(steps_done=list(steps_done), step_outputs=step_outputs)
                    except (TypeError, ValueError):
                        pass
                e._trace({"type": "step", "t": time.time(), "flow": self.id, "step": sid, "kind": step.get("kind", "generate"), "ok": out.get("ok"), "secs": round(time.time() - t0, 1),
                          "unsure": bool(out.get("unsure")), "changed": out.get("changed") or out.get("written") or [], "tries": out.get("tries")})
                if out.get("stopped"):
                    return {"ok": False, "stopped": True, "verdict": "fail", "files": e.changed_files}
                if out.get("dry"):
                    return {"ok": True, "dry": True, "verdict": "pass", "files": e.changed_files}
                if not out.get("ok"):
                    of = step.get("on_fail", "continue")
                    if out.get("unsure") and of != "continue":
                        verdict, stop_reason = "not_sure", out.get("why") or f"step {sid} could not be checked"
                        break
                    if of == "stop":
                        verdict, stop_reason = "fail", f"step {sid} failed"
                        break
                    if of == "not_sure":
                        verdict, stop_reason = "not_sure", f"step {sid} did not pass its checks"
                        break
        finally:
            e.pop_budget()
        if verdict is None:
            v = doc.get("verdict") or {}
            try:
                passed = evaluate(v.get("pass_if", "True"), ctx)
                unsure = evaluate(v.get("not_sure_if", "False"), ctx) or any(isinstance(o, dict) and o.get("unsure") for o in ctx["steps"].values())
            except ValueError as ex:
                passed, unsure = False, True
                stop_reason = f"bad verdict expression: {ex}"
            # three outcomes, never two: a step whose gate could not run (or a true not_sure_if) makes the run not_sure even when
            # pass_if holds -- a pass with unverifiable evidence is not a pass (this used to be overridden to "pass")
            verdict = "not_sure" if unsure else ("pass" if passed else "fail")
        e._trace({"type": "verdict", "t": time.time(), "flow": self.id, "verdict": verdict, "why": stop_reason or "", "meter": dict(e.meter)})
        e.say(f"\n[verdict] {verdict}" + (f": {stop_reason}" if stop_reason else ""))
        e.report("", f"- Verdict: **{verdict}**" + (f" ({stop_reason})" if stop_reason else ""))
        outputs = {k: v for k, v in ctx.items() if k in (doc.get("outputs") or [])}
        return {"ok": verdict == "pass", "verdict": verdict, "why": stop_reason, "files": e.changed_files, "steps": {k: {kk: vv for kk, vv in v.items() if kk in ("ok", "skipped", "verdict", "tries", "unsure", "sampled", "passed", "winner")} for k, v in ctx["steps"].items() if isinstance(v, dict)}, **outputs}

    def _dispatch(self, e, ctx, step):
        k = step.get("kind", "generate")
        if k == "code":
            g = self._gate(e, ctx, step, step["gate"], [])
            self._log_gate(e, step.get("title") or step["id"], g) if not step.get("quiet") else None
            out = {"ok": bool(g["ok"]), "text": g.get("text"), "unsure": g["ok"] is None, "why": g.get("text") if g["ok"] is None else None}
            out.update({kk: vv for kk, vv in (g.get("metrics") or {}).items() if kk not in out})
            return out
        if k == "generate":
            return self._run_generate(e, ctx, step, self.id)
        if k == "ask_json":
            return self._run_ask_json(e, ctx, step, self.id)
        if k == "judge":
            return self._run_judge(e, ctx, step, self.id)
        if k == "doc":
            return self._run_doc(e, ctx, step, self.id)
        if k == "sample_n":
            return self._run_sample_n(e, ctx, step, self.id)
        if k == "cascade":
            return self._run_cascade(e, ctx, step, self.id)
        if k == "chunked":
            return self._run_chunked(e, ctx, step, self.id)
        if k == "foreach":
            return self._run_foreach(e, ctx, step, self.id)
        if k == "tool_loop":
            return self._run_tool_loop(e, ctx, step, self.id)
        if k == "report":
            return self._run_report(e, ctx, step)
        if k == "handoff":
            import arch_flows
            fields = {kk: render(str(vv), ctx) if isinstance(vv, str) else vv for kk, vv in (step.get("fields") or {}).items()}
            arch_flows.write_handoff(e, flow=self.id, goal=e.goal, files=e.changed_files, **fields)
            return {"ok": True}
        if k == "sub_flow":
            import arch_flows
            fid = render(step["flow"], ctx)
            if fid not in arch_flows.FLOWS:
                return {"ok": False, "why": f"no such flow {fid}"}
            out = arch_flows.FLOWS[fid].run(e, dict(step.get("params") or {}, **{kk: ctx.get(vv) for kk, vv in (step.get("params_from") or {}).items()})) or {}
            return {"ok": bool(out.get("ok")), "sub": {kk: vv for kk, vv in out.items() if kk != "files"}, "changed": out.get("files") or []}
        raise ValueError(k)



def flow_map(flow_id, cfg=None):
    """How a flow works on THIS machine, for the hub: each step in order with its plain title, kind, which role and tier run it, the
    model that tier resolves to here, the check that decides it, and its budget. Read-only. -> {"id", "title", "blurb", "budget",
    "steps": [...]} or {"error"}."""
    import arch_core as core
    import arch_roles
    path = os.path.join(FLOWS_DIR, f"{flow_id}.yaml")
    if not os.path.isfile(path):
        return {"error": f"no flow named {flow_id}"}
    doc = load_yaml_file(path)
    cfg = cfg or core.load_config()
    tiers = cfg.get("tiers") or {}
    roles = cfg.get("roles") or {}
    steps = []
    for st in doc.get("steps") or []:
        role = st.get("role") or ""
        tier = st.get("tier") or ((arch_roles.ROLES.get(role) or {}).get("tier") if role else "")
        model = (roles.get(role) or "").strip() or (tiers.get(tier) or "") if (role or tier) else ""
        ver = st.get("verify") or {}
        steps.append({"id": st.get("id"), "title": st.get("title") or st.get("id"), "kind": st.get("kind"), "role": role, "tier": tier or "",
                      "model": model, "check": ver.get("gate") or st.get("gate") or ("tests" if ver.get("run_tests") else ""),
                      "when": st.get("when") or "", "retries": st.get("retries") or (st.get("loop") or {}).get("max") or 0,
                      "budget": st.get("budget") or {}})
    return {"id": doc.get("id") or flow_id, "title": doc.get("title") or flow_id, "blurb": doc.get("blurb") or "",
            "budget": doc.get("budget") or {}, "steps": steps}

def ctx_profile(e):
    try:
        import arch_server
        st = arch_server.load_state()
        return st.get("profile") or arch_server.profile_for(__import__("arch_hw").detect())
    except Exception:
        return "gpu6"


def register(flows_dict, mode, say=None):
    """Put every valid flows/*.yaml into arch_flows.FLOWS according to `mode`: data = YAML wins over the Python class of the same id;
    legacy = YAML flows only fill ids no class provides; off = none. -> list of ids registered from YAML."""
    if mode == "off":
        return []
    done = []
    for fid, fl in load_all(say).items():
        if mode == "data" or fid not in flows_dict:
            flows_dict[fid] = fl
            done.append(fid)
    return done


# ---- "What's inside": everything a workflow and each step consists of, for the hub's inspector -------------------------------
KIND_PLAIN = {"generate": "A model writes or changes code.", "code": "Code runs one check. No model, no tokens.",
              "ask_json": "A model answers in a fixed shape (a decision or a plan).", "judge": "A strict reviewer model grades the work; "
              "an unusable verdict counts as 'not sure', never a pass.", "doc": "A model writes a document.",
              "sample_n": "Several attempts at once; the first that passes the check wins.", "cascade": "The same job goes up the "
              "helpers (main, then expert) until it passes.", "tool_loop": "A model explores: lists, searches, reads and runs tests, "
              "one tool at a time.", "foreach": "Repeats its steps for every item of a list.", "sub_flow": "Runs another workflow.",
              "report": "Writes the run's report.", "handoff": "Saves progress for the next session.", "chunked": "Works through a big "
              "input in parts."}


def flow_detail(flow_id):
    """-> {id, title, blurb, kind, produces, inputs, budget, context, overrides, roles, tiers, steps:[{id, title, kind, kind_plain,
    tier, role, prompt_name, prompt_text, check, check_plain, when, retries, budget, context, apply, params, on_fail}]}"""
    path = os.path.join(FLOWS_DIR, f"{flow_id}.yaml")
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", flow_id or "") or not os.path.isfile(path):
        return {"error": f"no workflow called {flow_id!r}"}
    doc = load_yaml_file(path)
    try:
        import arch_gates
        gates = getattr(arch_gates, "GATES", {})
    except Exception:
        gates = {}

    def gate_plain(name):
        fn = gates.get(name)
        d = (getattr(fn, "__doc__", "") or "").strip().split("\n\n")[0] if fn else ""
        return " ".join(d.split())[:400] or f"the check '{name}'"

    steps, tiers, roles = [], set(), set()

    def walk(lst, depth=0):
        for st in lst or []:
            if not isinstance(st, dict):
                continue
            g = (st.get("verify") or {}).get("gate") or st.get("gate") or ""
            pname = st.get("prompt") or ""
            ptext = ""
            if pname:
                for cand in (pname, pname + ".md"):
                    p = prompt_path(doc.get("id"), cand)
                    if os.path.isfile(p):
                        ptext = open(p, encoding="utf-8", errors="replace").read()[:6000]
                        break
            if st.get("tier"):
                tiers.add(st["tier"])
            if st.get("role"):
                roles.add(st["role"])
            steps.append({"id": st.get("id"), "title": st.get("title") or st.get("id"), "kind": st.get("kind"), "depth": depth,
                          "kind_plain": KIND_PLAIN.get(st.get("kind"), ""), "tier": st.get("tier") or "", "role": st.get("role") or "",
                          "prompt_name": pname, "prompt_text": ptext, "check": g, "check_plain": gate_plain(g) if g else "",
                          "when": st.get("when") or "", "retries": st.get("retries") or 0, "budget": st.get("budget") or {},
                          "context": st.get("context") or {}, "apply": st.get("apply") or {}, "params": st.get("vars") or st.get("params") or {},
                          "on_fail": st.get("on_fail") or "", "loop": st.get("loop") or {}})
            walk(st.get("steps"), depth + 1)

    walk(doc.get("steps"))
    return {"id": doc.get("id"), "title": doc.get("title"), "blurb": doc.get("blurb") or "", "kind": doc.get("kind") or "",
            "produces": doc.get("produces") or [], "inputs": doc.get("inputs") or {}, "budget": doc.get("budget") or {},
            "context": doc.get("context") or {}, "overrides": doc.get("overrides") or {}, "verdict": doc.get("verdict") or {},
            "roles": sorted(roles | set(doc.get("uses_roles") or [])), "tiers": sorted(tiers), "steps": steps,
            "file": os.path.relpath(path, HERE)}
