"""harness: the parts an agent harness is made of, one module each, judged by a hidden acceptance check.

Eight templates -- a tool dispatcher that validates arguments, a token budget checked before the call, a re-ask on an unreadable
reply that keeps the prompt prefix stable, a cache-aware prompt builder, a verifier gate with three outcomes, a work queue with
alternates anchored to the original flow, a stall detector and a cost ledger -- each in five variants (40 tasks). They are the same
ideas Architect itself is built on, so a model trained on the passed runs learns this harness's conventions: three outcomes never
two, the stable part of a prompt first, an environmental failure is not a failed approach.

Same shape as every family: goal with the exact interface, a fixture (`fakemodel.py`, a scripted stand-in model, plus the house
rules), a hidden deterministic stdlib-only check, a reference solution and a deliberately wrong one.

    python evals/families/harness.py --check
    python evals/families/harness.py --night --hours 8      # same overnight driver as distributed.py (both families)
"""
import os
import random
import sys

if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from distributed import HOLDOUT, TEST_HEAD, TEST_TAIL, _fill, _mutate  # noqa: E402  (one folder, plain import, like _bank)

FLOW = "build"
DEFAULT_N = 1
PREFIX = "train-harness"
TAG = "harness"
MIN_TASKS, MIN_TEMPLATES = 40, 8

FAKEMODEL = '''"""A scripted stand-in for a model: answers with the replies it was given, in order, and remembers every prompt. Do not edit."""


class ScriptedModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.replies:
            raise RuntimeError("the script ran out of replies")
        return self.replies.pop(0)
'''

CONVENTIONS = """# House rules for harness code

- Standard library only.
- A check has three outcomes: True (ran and passed), False (ran and failed), None (could not run). Never collapse None into
  False or True.
- Prompts are prefix-stable: what stays the same between tries comes first, what changes per try (feedback, errors) comes last.
- `fakemodel.ScriptedModel(["reply 1", "reply 2"])` is a model stand-in for your tests: calling it with a prompt returns the next
  reply and records the prompt in `.prompts`. Do not edit it.
- Tests live in tests/ (`tests/test_<module>.py`) and must run under both pytest and `python -m unittest`, so write
  `unittest.TestCase` classes. Put the project root on sys.path at the top of the test file.
"""


# ================================================================== 1. tool dispatcher with argument validation
TOOLS_V = [("tools", "ToolRegistry", ("read_file", "scale")), ("dispatch", "Dispatcher", ("grep", "multiply")),
           ("toolbox", "Toolbox", ("outline", "weigh")), ("actions", "ActionTable", ("view", "resize")),
           ("calls", "CallRouter", ("search", "stretch"))]
TOOLS_SRC = r'''"""Tool calls from a model, checked against each tool's declared arguments before anything runs. dispatch() never raises."""
import json

TYPES = {"str": (str,), "int": (int,), "float": (int, float), "bool": (bool,), "list": (list,), "dict": (dict,)}


def _type_ok(value, kind):
    if isinstance(value, bool) and kind != "bool":
        return False
    return isinstance(value, TYPES[kind])


class @CLS@:
    def __init__(self):
        self._tools = {}

    def names(self):
        return sorted(self._tools)

    def register(self, name, fn, params):
        if name in self._tools:
            raise ValueError(f"a tool named {name!r} is already registered")
        for arg, spec in params.items():
            if spec.get("type") not in TYPES:
                raise ValueError(f"{name}.{arg}: unknown type {spec.get('type')!r}")
        self._tools[name] = (fn, params)

    def _problem(self, params, args):
        for arg, spec in params.items():
            if arg not in args:
                if spec.get("required", True):
                    return f"missing required argument {arg!r}"
                continue
            if not _type_ok(args[arg], spec["type"]):
                return f"argument {arg!r} must be {spec['type']}, got {type(args[arg]).__name__}"
        extra = sorted(set(args) - set(params))
        if extra:
            return "unknown argument(s): " + ", ".join(extra)
        return None

    def dispatch(self, call):
        if isinstance(call, str):
            try:
                call = json.loads(call)
            except ValueError as e:
                return {"ok": False, "error": f"the call is not valid JSON: {e}"}
        if not isinstance(call, dict) or not isinstance(call.get("args", {}), dict):
            return {"ok": False, "error": 'a call is an object {"tool": name, "args": {...}}'}
        name, args = call.get("tool"), call.get("args", {})
        if not isinstance(name, str) or name not in self._tools:
            return {"ok": False, "error": f"unknown tool {name!r}; the tools are: {', '.join(self.names())}"}
        fn, params = self._tools[name]
        problem = self._problem(params, args)
        if problem:
            return {"ok": False, "error": f"{name}: {problem}"}
        try:
            return {"ok": True, "result": fn(**args)}
        except Exception as e:
            return {"ok": False, "error": f"{name} failed: {type(e).__name__}: {e}"}
'''
TOOLS_TESTS = r'''from @MOD@ import @CLS@  # noqa: E402


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.reg = @CLS@()
        self.reg.register(@T1@, lambda text, n=1: text * n, {"text": {"type": "str"}, "n": {"type": "int", "required": False}})

    def test_good_call(self):
        self.assertEqual(self.reg.dispatch({"tool": @T1@, "args": {"text": "ab", "n": 2}}), {"ok": True, "result": "abab"})

    def test_bad_calls_never_raise(self):
        for call in ({"tool": "missing", "args": {}}, {"tool": @T1@, "args": {}}, "{oops"):
            self.assertFalse(self.reg.dispatch(call)["ok"])
'''
TOOLS_HIDDEN = r'''
import json
from @MOD@ import @CLS@
T1, T2 = @T1@, @T2@
reg = @CLS@()
reg.register(T1, lambda path, limit=10: path[:limit], {"path": {"type": "str"}, "limit": {"type": "int", "required": False}})
reg.register(T2, lambda x, scale: x * scale, {"x": {"type": "float"}, "scale": {"type": "float"}})
reg.register("explode", lambda: 1 / 0, {})
assert reg.names() == sorted([T1, T2, "explode"])
r = reg.dispatch({"tool": T1, "args": {"path": "abcdefghijklmnop", "limit": 3}})
assert r == {"ok": True, "result": "abc"}, r
assert reg.dispatch(json.dumps({"tool": T1, "args": {"path": "xy"}})) == {"ok": True, "result": "xy"}, "a JSON string is a call too"
assert reg.dispatch({"tool": T2, "args": {"x": 2, "scale": 1.5}})["result"] == 3.0, "an int is a valid float"


def bad(call, why):
    r = reg.dispatch(call)
    assert isinstance(r, dict) and r.get("ok") is False and isinstance(r.get("error"), str) and r["error"], (why, r)
    return r["error"]


assert T1 in bad({"tool": "nope", "args": {}}, "unknown tool"), "the error lists the tools that exist"
bad({"tool": T1, "args": {}}, "missing required argument")
bad({"tool": T1, "args": {"path": "a", "extra": 1}}, "unknown argument")
bad({"tool": T1, "args": {"path": 5}}, "wrong type")
bad({"tool": T1, "args": {"path": "a", "limit": True}}, "a bool is not an int")
bad({"tool": T1, "args": {"path": "a", "limit": 2.5}}, "a float is not an int")
bad({"tool": T2, "args": {"x": False, "scale": 1.0}}, "a bool is not a float")
assert "ZeroDivisionError" in bad({"tool": "explode", "args": {}}, "the tool raised"), "the error names what the tool raised"
bad("{not json", "bad JSON")
bad(["tool"], "not an object")
bad({"tool": T1, "args": "path"}, "args not an object")
try:
    reg.register(T1, len, {})
    raise SystemExit("a duplicate tool name was accepted")
except ValueError:
    pass
try:
    reg.register("odd", len, {"a": {"type": "complex"}})
    raise SystemExit("an unknown schema type was accepted")
except ValueError:
    pass
print("ok")
'''


def t_tools(v):
    mod, cls, (t1, t2) = v
    m = dict(MOD=mod, CLS=cls, T1=repr(t1), T2=repr(t2))
    goal = (f"Build a Python module `{mod}.py`: the tool dispatcher of an agent harness, with tests in tests/. Public interface, "
            f"exactly: `from {mod} import {cls}`; `{cls}()`; `register(name, fn, params)` where params maps each argument name to "
            f"{{'type': 'str'|'int'|'float'|'bool'|'list'|'dict', 'required': bool (default True)}}; a name registered twice or an "
            f"unknown type raises ValueError. `names()` returns the tool names sorted. `dispatch(call)` takes a dict "
            f"{{'tool': name, 'args': {{...}}}} or the same as a JSON string, calls fn(**args) and returns {{'ok': True, 'result': value}}; "
            f"it NEVER raises: every problem comes back as {{'ok': False, 'error': text}} -- invalid JSON, a call or args that is not "
            f"an object, an unknown tool (the error lists the tools that exist), a missing required argument, an argument not in the "
            f"schema, a value of the wrong type (a bool is not an int or a float; an int is a valid float), and a tool that raised "
            f"(the error names the exception type). The first tools will be `{t1}` and `{t2}`. Read CONVENTIONS.md.")
    src = _fill(TOOLS_SRC, **m)
    wrong = _mutate(src, '    if isinstance(value, bool) and kind != "bool":\n        return False\n', "")
    return mod, goal, _fill(TOOLS_HIDDEN, **m), src, _fill(TOOLS_TESTS, **m), wrong


# ================================================================== 2. token budget, checked before the call
BUDGET_V = [("budget", "TokenBudget", 8000, 5, 1000), ("allowance", "Allowance", 20000, 10, 2048), ("spend", "SpendGuard", 4096, 3, 512),
            ("meter", "CallMeter", 12000, 6, 1500), ("tokencap", "TokenCap", 30000, 8, 4000)]
BUDGET_SRC = r'''"""A token and call budget, checked BEFORE a model call goes out: the prompt plus the room kept for the reply must fit."""
import math


class BudgetExceeded(RuntimeError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


class @CLS@:
    def __init__(self, max_tokens, max_calls):
        if max_tokens < 1 or max_calls < 1:
            raise ValueError("max_tokens and max_calls must be at least 1")
        self.max_tokens = max_tokens
        self.max_calls = max_calls
        self.spent = 0
        self.calls = 0

    @staticmethod
    def estimate(text):
        return math.ceil(len(text) / 4)

    def before_call(self, prompt_tokens, reply_room):
        need = self.spent + prompt_tokens + reply_room
        if need > self.max_tokens:
            raise BudgetExceeded("tokens", f"this call needs {need} tokens of a {self.max_tokens} budget")
        if self.calls + 1 > self.max_calls:
            raise BudgetExceeded("calls", f"the budget allows {self.max_calls} calls")

    def after_call(self, prompt_tokens, reply_tokens):
        self.spent += prompt_tokens + reply_tokens
        self.calls += 1

    def remaining(self):
        return {"tokens": self.max_tokens - self.spent, "calls": self.max_calls - self.calls}
'''
BUDGET_TESTS = r'''from @MOD@ import @CLS@, BudgetExceeded  # noqa: E402


class BudgetTest(unittest.TestCase):
    def test_spend_and_remaining(self):
        b = @CLS@(@MT@, @MC@)
        b.before_call(10, 10)
        b.after_call(10, 5)
        self.assertEqual(b.remaining(), {"tokens": @MT@ - 15, "calls": @MC@ - 1})

    def test_refuses_before_the_call(self):
        b = @CLS@(@MT@, @MC@)
        with self.assertRaises(BudgetExceeded):
            b.before_call(@MT@, 1)
'''
BUDGET_HIDDEN = r'''
from @MOD@ import @CLS@, BudgetExceeded
MT, MC, ROOM = @MT@, @MC@, @ROOM@
b = @CLS@(MT, MC)
assert b.estimate("") == 0 and b.estimate("abcd") == 1 and b.estimate("abcde") == 2, "about 4 characters per token, rounded up"
b.before_call(MT - ROOM, ROOM)
try:
    b.before_call(MT - ROOM + 1, ROOM)
    raise SystemExit("a prompt plus its reply room over the budget was allowed")
except BudgetExceeded as e:
    assert e.reason == "tokens", e.reason
assert b.remaining() == {"tokens": MT, "calls": MC}, "a check must not spend anything"
b.after_call(100, 50)
assert b.remaining() == {"tokens": MT - 150, "calls": MC - 1}
try:
    b.before_call(MT - 150 - ROOM + 1, ROOM)
    raise SystemExit("the reply room must be counted")
except BudgetExceeded:
    pass
for _ in range(MC - 1):
    b.after_call(1, 1)
try:
    b.before_call(1, 1)
    raise SystemExit("one call too many was allowed")
except BudgetExceeded as e:
    assert e.reason == "calls", e.reason
try:
    @CLS@(0, 1)
    raise SystemExit("a zero budget was accepted")
except ValueError:
    pass
print("ok")
'''


def t_budget(v):
    mod, cls, mt, mc, room = v
    m = dict(MOD=mod, CLS=cls, MT=mt, MC=mc, ROOM=room)
    goal = (f"Build a Python module `{mod}.py`: the budget an agent harness enforces before every model call, with tests in tests/. "
            f"Public interface, exactly: `from {mod} import {cls}, BudgetExceeded`; `{cls}(max_tokens, max_calls)` (either below 1 "
            f"raises ValueError). `estimate(text)` returns ceil(len(text) / 4). `before_call(prompt_tokens, reply_room)` changes "
            f"nothing and raises BudgetExceeded with `.reason == 'tokens'` when tokens spent so far + prompt_tokens + reply_room would "
            f"exceed max_tokens, or with `.reason == 'calls'` when one more call would exceed max_calls (tokens are checked first). "
            f"`after_call(prompt_tokens, reply_tokens)` adds both to the tokens spent and counts the call. `remaining()` returns "
            f"{{'tokens': n, 'calls': n}}. Callers here use {mt} tokens, {mc} calls and keep {room} tokens of room for each reply. "
            f"Read CONVENTIONS.md.")
    src = _fill(BUDGET_SRC, **m)
    wrong = _mutate(src, "need = self.spent + prompt_tokens + reply_room", "need = self.spent + prompt_tokens")
    return mod, goal, _fill(BUDGET_HIDDEN, **m), src, _fill(BUDGET_TESTS, **m), wrong


# ================================================================== 3. re-ask on an unreadable reply, prefix kept stable
REASK_V = [("structured", "ask_structured", 3), ("reask", "ask_until_valid", 2), ("jsonreply", "get_json", 4),
           ("parse_retry", "ask_parsed", 3), ("strict_reply", "ask_strict", 5)]
REASK_SRC = r'''"""Ask a model for a JSON object and, when the reply cannot be read, ask again: the same prompt first, the one latest problem last."""
import json
import re

FENCE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.S)


class ReplyUnreadable(ValueError):
    def __init__(self, replies, last_error):
        super().__init__(f"no readable JSON object after {len(replies)} tries: {last_error}")
        self.replies = replies


def extract_json(text):
    text = text or ""
    m = FENCE.search(text)
    if m:
        candidate = m.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object in the reply")
        candidate = text[start:end + 1]
    value = json.loads(candidate)
    if not isinstance(value, dict):
        raise ValueError("the reply holds JSON, but not an object")
    return value


def @FN@(model, prompt, tries=3):
    if tries < 1:
        raise ValueError("tries must be at least 1")
    replies, ask, last = [], prompt, None
    for attempt in range(1, tries + 1):
        reply = model(ask)
        replies.append(reply)
        try:
            return extract_json(reply), attempt
        except ValueError as e:
            last = (str(e).splitlines() or [type(e).__name__])[0]
            ask = prompt + "\n\n" + f"Your last reply could not be read ({last}). Reply with one JSON object only."
    raise ReplyUnreadable(replies, last)
'''
REASK_TESTS = r'''from fakemodel import ScriptedModel  # noqa: E402
from @MOD@ import @FN@, extract_json, ReplyUnreadable  # noqa: E402


class ReaskTest(unittest.TestCase):
    def test_extract(self):
        self.assertEqual(extract_json('here: {"a": 1}'), {"a": 1})
        with self.assertRaises(ValueError):
            extract_json("nothing")

    def test_second_try_reads(self):
        model = ScriptedModel(["no json", '{"ok": true}'])
        self.assertEqual(@FN@(model, "Give JSON.", 2), ({"ok": True}, 2))
        self.assertTrue(model.prompts[1].startswith("Give JSON."))

    def test_gives_up(self):
        with self.assertRaises(ReplyUnreadable):
            @FN@(ScriptedModel(["x"] * @TRIES@), "Give JSON.", @TRIES@)
'''
REASK_HIDDEN = r'''
from @MOD@ import @FN@, extract_json, ReplyUnreadable
TRIES = @TRIES@
assert extract_json('noise {"a": 1} tail') == {"a": 1}
assert extract_json('text\n```json\n{"b": [1, 2]}\n```\nmore {x}') == {"b": [1, 2]}, "a fenced block wins"
for bad in ("no json here", "[1, 2]", "{broken"):
    try:
        extract_json(bad)
        raise SystemExit("accepted %r" % bad)
    except ValueError:
        pass


def scripted(replies):
    seen = []

    def model(prompt):
        seen.append(prompt)
        return replies[len(seen) - 1]
    return model, seen


P = "Return the plan as JSON with a key 'steps'."
model, seen = scripted(["bad reply %d" % i for i in range(TRIES - 1)] + ['{"steps": [1]}'])
obj, used = @FN@(model, P, TRIES)
assert obj == {"steps": [1]} and used == TRIES, (obj, used)
assert seen[0] == P, "the first try is the prompt itself"
assert all(s.startswith(P + "\n\n") and len(s) > len(P) + 2 for s in seen[1:]), "the original prompt stays the unchanged prefix of every retry"
assert all("\n" not in s[len(P) + 2:].strip() for s in seen[1:]), "the feedback is one line"
assert all(("bad reply %d" % j) not in seen[k] for k in range(len(seen)) for j in range(k - 1)), "only the latest problem is appended, never the history"
model, seen = scripted(["nope %d" % i for i in range(TRIES)])
try:
    @FN@(model, P, TRIES)
    raise SystemExit("it gave up without raising")
except ReplyUnreadable as e:
    assert list(e.replies) == ["nope %d" % i for i in range(TRIES)] and len(seen) == TRIES, (e.replies, len(seen))
try:
    @FN@(model, P, 0)
    raise SystemExit("tries=0 was accepted")
except ValueError:
    pass
print("ok")
'''


def t_reask(v):
    mod, fn, tries = v
    m = dict(MOD=mod, FN=fn, TRIES=tries)
    goal = (f"Build a Python module `{mod}.py` that asks a model for a JSON object and asks again when the reply cannot be read, with "
            f"tests in tests/. Public interface, exactly: `from {mod} import {fn}, extract_json, ReplyUnreadable`. `extract_json(text)` "
            f"returns the JSON object in a reply: the content of the first ``` (or ```json) fenced block if there is one, else the text "
            f"from the first '{{' to the last '}}'; it raises ValueError when nothing parses or the value is not an object. "
            f"`{fn}(model, prompt, tries=3)`: model is a callable taking the prompt and returning the reply text. It returns "
            f"(object, attempts_used). When a reply cannot be read, the next prompt is EXACTLY the original prompt, then two newlines, "
            f"then ONE line of feedback naming the latest problem -- never the history of earlier problems (the stable part first keeps "
            f"the model server's prefix cache warm). After `tries` unreadable replies it raises ReplyUnreadable (a ValueError) whose "
            f"`.replies` lists every reply. tries below 1 raises ValueError. Callers here use tries={tries}. Read CONVENTIONS.md.")
    src = _fill(REASK_SRC, **m)
    wrong = _mutate(src, 'ask = prompt + "\\n\\n" + f"Your last reply could not be read ({last}). Reply with one JSON object only."',
                    'ask = f"Your last reply could not be read ({last}). Reply with one JSON object only." + "\\n\\n" + prompt')
    return mod, goal, _fill(REASK_HIDDEN, **m), src, _fill(REASK_TESTS, **m), wrong


# ================================================================== 4. a prompt builder laid out for the prefix cache
PROMPT_V = [("prompts", "PromptBuilder", 2000), ("prefix", "PrefixBuilder", 1200), ("assemble", "PromptAssembler", 3000),
            ("cacheable", "CacheablePrompt", 1600), ("layout", "PromptLayout", 2400)]
PROMPT_SRC = r'''"""Prompts laid out for the reply cache: what stays the same between tries comes first, what changes per try comes last."""
import hashlib


class @CLS@:
    def __init__(self, max_chars):
        if max_chars < 1:
            raise ValueError("max_chars must be positive")
        self.max_chars = max_chars
        self._sections = []

    def add(self, name, text, stable=True, optional=False):
        if any(s["name"] == name for s in self._sections):
            raise ValueError(f"section {name!r} was added twice")
        self._sections.append({"name": name, "text": text, "stable": stable, "optional": optional})
        return self

    @staticmethod
    def _block(section):
        return f"=== {section['name']} ===\n{section['text']}\n"

    def _layout(self):
        ordered = [s for s in self._sections if s["stable"]] + [s for s in self._sections if not s["stable"]]
        dropped = set()
        while len("".join(self._block(s) for s in ordered if s["name"] not in dropped)) > self.max_chars:
            left = [s for s in ordered if s["optional"] and s["name"] not in dropped]
            if not left:
                raise ValueError(f"the prompt does not fit in {self.max_chars} characters even without its optional sections")
            dropped.add(max(left, key=lambda s: len(s["text"]))["name"])
        return [s for s in ordered if s["name"] not in dropped]

    def render(self):
        return "".join(self._block(s) for s in self._layout())

    def prefix_key(self):
        stable = "".join(self._block(s) for s in self._layout() if s["stable"])
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()
'''
PROMPT_TESTS = r'''from @MOD@ import @CLS@  # noqa: E402


class PromptTest(unittest.TestCase):
    def test_stable_first(self):
        p = @CLS@(@MAX@).add("feedback", "it failed", stable=False).add("rules", "be brief")
        text = p.render()
        self.assertLess(text.index("=== rules ==="), text.index("=== feedback ==="))

    def test_same_prefix_key(self):
        a = @CLS@(@MAX@).add("rules", "be brief").add("feedback", "one", stable=False)
        b = @CLS@(@MAX@).add("rules", "be brief").add("feedback", "two", stable=False)
        self.assertEqual(a.prefix_key(), b.prefix_key())
'''
PROMPT_HIDDEN = r'''
from @MOD@ import @CLS@
M = @MAX@


def make(feedback):
    p = @CLS@(M)
    p.add("feedback", feedback, stable=False)
    p.add("instructions", "Fix the failing test. Reply with EDIT blocks.")
    p.add("project", "def add(a, b):\n    return a + b\n")
    return p


a, b = make("try 1 failed: AssertionError"), make("try 2 failed: NameError x")
ra, rb = a.render(), b.render()
assert ra.index("=== instructions ===") < ra.index("=== project ===") < ra.index("=== feedback ==="), ra
assert ra.startswith("=== instructions ===\nFix the failing test."), ra[:80]
stable = ra[:ra.index("=== feedback ===")]
assert rb.startswith(stable), "two tries share the whole stable prefix"
assert a.prefix_key() == b.prefix_key() and len(a.prefix_key()) == 64
c = @CLS@(M)
c.add("instructions", "Different rules.")
assert c.prefix_key() != a.prefix_key()
big = @CLS@(M)
big.add("instructions", "short rules")
big.add("examples", "x" * (M // 2), optional=True)
big.add("history", "y" * (M // 3), optional=True)
big.add("feedback", "z" * (M // 3), stable=False)
out = big.render()
assert len(out) <= M, len(out)
assert "x" * 10 not in out and "y" * 10 in out and "z" * 10 in out, "the longest optional section is dropped first, and only as many as needed"
t = @CLS@(M)
t.add("rules", "r" * (M + 1))
try:
    t.render()
    raise SystemExit("a prompt that cannot fit was rendered")
except ValueError:
    pass
try:
    t.add("rules", "again")
    raise SystemExit("a section added twice was accepted")
except ValueError:
    pass
print("ok")
'''


def t_prompt(v):
    mod, cls, mx = v
    m = dict(MOD=mod, CLS=cls, MAX=mx)
    goal = (f"Build a Python module `{mod}.py`: a prompt builder laid out for the model server's prefix cache, with tests in "
            f"tests/. Public interface, exactly: `from {mod} import {cls}`; `{cls}(max_chars)`; `add(name, text, stable=True, "
            f"optional=False)` returns the builder (a name added twice raises ValueError). `render()` writes every stable section in "
            f"the order added, THEN every non-stable section in the order added, each as '=== NAME ===' + newline + text + newline. "
            f"When the result is longer than max_chars, optional sections are left out, longest first, only until it fits; when it "
            f"still does not fit it raises ValueError. `prefix_key()` returns the sha256 hex digest of the rendered stable part only, "
            f"so two tries that differ only in their feedback share it. Callers here use max_chars={mx}. Read CONVENTIONS.md.")
    src = _fill(PROMPT_SRC, **m)
    wrong = _mutate(src, 'ordered = [s for s in self._sections if s["stable"]] + [s for s in self._sections if not s["stable"]]',
                    "ordered = list(self._sections)")
    return mod, goal, _fill(PROMPT_HIDDEN, **m), src, _fill(PROMPT_TESTS, **m), wrong


# ================================================================== 5. a verifier gate with three outcomes
GATE_V = [("gate", ("pass", "fail", "not_sure")), ("tri_state", ("ok", "broken", "unverified")), ("checks", ("green", "red", "grey")),
          ("outcome", ("passed", "failed", "could_not_run")), ("judge", ("accept", "reject", "not_sure"))]
GATE_SRC = r'''"""A verifier gate with three outcomes: True (ran and passed), False (ran and failed), None (could not run). None is never a pass."""


class CouldNotRun(Exception):
    """Raised by a check that could not run at all (a tool missing, nothing to check)."""


LABELS = {True: @P@, False: @F@, None: @U@}


def run_check(fn):
    try:
        return bool(fn())
    except (CouldNotRun, OSError):
        return None
    except Exception:
        return False


def combine(results):
    results = list(results)
    if any(r is False for r in results):
        return False
    if not results or any(r is None for r in results):
        return None
    return True


def verdict(results):
    return LABELS[combine(results)]
'''
GATE_TESTS = r'''from @MOD@ import CouldNotRun, combine, run_check, verdict  # noqa: E402


def cannot():
    raise CouldNotRun("no runner")


class GateTest(unittest.TestCase):
    def test_three_outcomes(self):
        self.assertIs(run_check(lambda: True), True)
        self.assertIs(run_check(lambda: False), False)
        self.assertIsNone(run_check(cannot))

    def test_none_is_not_a_pass(self):
        self.assertIsNone(combine([True, None]))
        self.assertEqual(verdict([True, None]), @U@)
'''
GATE_HIDDEN = r'''
from @MOD@ import run_check, combine, verdict, CouldNotRun
P, F, U = @P@, @F@, @U@
assert run_check(lambda: True) is True and run_check(lambda: 1) is True
assert run_check(lambda: False) is False and run_check(lambda: 0) is False


def missing():
    raise CouldNotRun("pytest is not installed")


def no_tool():
    raise FileNotFoundError("ruff")


def crash():
    raise ZeroDivisionError("a bug in the code under test")


assert run_check(missing) is None and run_check(no_tool) is None, "a check that could not run is None"
assert run_check(crash) is False, "code that crashes failed; it did not fail to run"
assert combine([True, True]) is True
assert combine([True, None]) is None, "could not run is not a pass"
assert combine([None, False]) is False and combine([False, None, True]) is False
assert combine([]) is None, "no checks prove nothing"
assert combine([None]) is None
assert combine(x for x in [True]) is True, "any iterable"
assert verdict([True]) == P and verdict([True, False]) == F and verdict([True, None]) == U and verdict([]) == U
print("ok")
'''


def t_gate(v):
    mod, (p, f, u) = v
    m = dict(MOD=mod, P=repr(p), F=repr(f), U=repr(u))
    goal = (f"Build a Python module `{mod}.py`: the verifier gate of an agent harness, where a check has THREE outcomes, with tests "
            f"in tests/. Public interface, exactly: `from {mod} import run_check, combine, verdict, CouldNotRun`. `CouldNotRun` is an "
            f"exception a check raises when it cannot run. `run_check(fn)` calls fn() and returns True when the result is truthy, False "
            f"when falsy, None when fn raised CouldNotRun or an OSError (a tool is missing), and False for any other exception (the "
            f"code under check crashed). `combine(results)` takes any iterable of True/False/None: False if any is False, else None "
            f"if any is None or there are none, else True. `verdict(results)` maps combine's answer to '{p}' (True), '{f}' (False) "
            f"or '{u}' (None). None must never be turned into True or False. Read CONVENTIONS.md.")
    src = _fill(GATE_SRC, **m)
    wrong = _mutate(src, "    if not results or any(r is None for r in results):\n        return None\n    return True\n",
                    "    if not results:\n        return None\n    return all(bool(r) for r in results)\n")
    return mod, goal, _fill(GATE_HIDDEN, **m), src, _fill(GATE_TESTS, **m), wrong


# ================================================================== 6. a work queue with alternates anchored to the original flow
ALT_V = [("supervisor", "Supervisor", "bugfix", {"bugfix": ["feature", "build"], "feature": ["refactor"], "build": ["feature"]}),
         ("jobrunner", "JobRunner", "a_fix", {"a_fix": ["b_rewrite", "c_rebuild"], "b_rewrite": ["d_split"]}),
         ("approaches", "ApproachQueue", "build", {"build": ["scaffold", "feature"], "scaffold": ["test"], "feature": ["build"]}),
         ("fallbacks", "FallbackQueue", "repair", {"repair": ["patch", "rewrite", "rebuild"], "patch": ["hotfix"]}),
         ("taskpool", "TaskPool", "hunt", {"hunt": ["review", "test"], "review": ["secure"], "test": ["hunt"]})]
ALT_SRC = r'''"""A work queue that retries a failed task with other approaches -- always the alternates of the task's ORIGINAL flow -- and that
pauses on trouble with the machine (a server down) instead of spending an approach on it."""
import collections

DONE = "pass"
ENVIRONMENTAL = "env"


class @CLS@:
    def __init__(self, alternates, run):
        self.alternates = {k: list(v) for k, v in alternates.items()}
        self.run = run
        self.paused = False
        self._queue = collections.deque()

    def submit(self, task, flow):
        self._queue.append({"task": task, "flow": flow, "tried": []})

    def pending(self):
        return len(self._queue)

    def resume(self):
        self.paused = False

    def _approaches(self, flow):
        return [flow] + [f for f in self.alternates.get(flow, []) if f != flow]

    def drain(self):
        finished = []
        while self._queue and not self.paused:
            entry = self._queue[0]
            result = None
            for f in self._approaches(entry["flow"])[len(entry["tried"]):]:
                result = self.run(entry["task"], f)
                if result == ENVIRONMENTAL:
                    self.paused = True
                    break
                entry["tried"].append(f)
                if result == DONE:
                    break
            if self.paused:
                break
            self._queue.popleft()
            finished.append({"task": entry["task"], "flow": entry["flow"], "tried": list(entry["tried"]), "result": result})
        return finished
'''
ALT_WRONG_APPROACHES = r'''    def _approaches(self, flow):
        chain, current = [flow], flow
        while self.alternates.get(current):
            nxt = [f for f in self.alternates[current] if f not in chain]
            if not nxt:
                break
            current = nxt[0]
            chain.append(current)
        return chain
'''
ALT_TESTS = r'''from @MOD@ import @CLS@  # noqa: E402

ALT = @ALT@


class AlternatesTest(unittest.TestCase):
    def test_pass_first_time(self):
        s = @CLS@(ALT, lambda task, flow: "pass")
        s.submit("t", @FIRST@)
        self.assertEqual(s.drain(), [{"task": "t", "flow": @FIRST@, "tried": [@FIRST@], "result": "pass"}])

    def test_environment_pauses(self):
        s = @CLS@(ALT, lambda task, flow: "env")
        s.submit("t", @FIRST@)
        self.assertEqual(s.drain(), [])
        self.assertTrue(s.paused)
        self.assertEqual(s.pending(), 1)
'''
ALT_HIDDEN = r'''
from @MOD@ import @CLS@
ALT, first = @ALT@, @FIRST@
alts = ALT[first]
script, log = {}, []


def run(task, flow):
    log.append((task, flow))
    outcomes = script.get(task, [])
    return outcomes.pop(0) if outcomes else "fail"


s = @CLS@(ALT, run)
script["t1"] = ["fail"] + ["not_sure"] * (len(alts) - 1) + ["pass"]
s.submit("t1", first)
s.submit("t2", first)
recs = s.drain()
assert [r["task"] for r in recs] == ["t1", "t2"], recs
assert recs[0]["tried"] == [first] + alts and recs[0]["result"] == "pass", ("alternates of the ORIGINAL flow, in order", recs[0])
assert recs[1]["tried"] == [first] + alts and recs[1]["result"] == "fail", ("never an alternate of an alternate", recs[1])
assert all(r["flow"] == first for r in recs)
del log[:]
script["t3"] = ["fail", "env"]
script["t4"] = ["pass"]
s.submit("t3", first)
s.submit("t4", first)
recs = s.drain()
assert recs == [] and s.paused and s.pending() == 2, (recs, s.paused, s.pending())
assert log == [("t3", first), ("t3", alts[0])], log
assert s.drain() == [] and len(log) == 2, "a paused queue runs nothing"
s.resume()
script["t3"] = ["pass"]
recs = s.drain()
assert [r["task"] for r in recs] == ["t3", "t4"], recs
assert recs[0]["tried"] == [first, alts[0]] and recs[0]["result"] == "pass", ("an environmental failure spends no approach", recs[0])
assert log[2] == ("t3", alts[0]), "after the pause it goes on with the approach that was interrupted"
assert s.pending() == 0 and not s.paused
print("ok")
'''


def t_alt(v):
    mod, cls, first, alt = v
    chain = [first, alt[first][0]] + [f for f in alt.get(alt[first][0], []) if f != first][:1]
    assert len(alt[first]) >= 2 and chain[:3] != ([first] + alt[first])[:3], "the wrong answer must take a different path"
    m = dict(MOD=mod, CLS=cls, FIRST=repr(first), ALT=alt)
    goal = (f"Build a Python module `{mod}.py`: the work queue of an agent harness that retries a failed task with other "
            f"approaches, with tests in tests/. Public interface, exactly: `from {mod} import {cls}`; `{cls}(alternates, run)` where "
            f"alternates maps a flow name to the list of flows to try instead, and run(task, flow) returns 'pass', 'fail', 'not_sure' "
            f"or 'env'. `submit(task, flow)` queues a task; `pending()` counts queued tasks; `drain()` works through the queue first in "
            f"first out and returns one record per finished task: {{'task', 'flow' (the original), 'tried' (flows that gave an outcome, "
            f"in order), 'result' (the last outcome)}}. A task tries its original flow, then on 'fail' or 'not_sure' each alternate OF "
            f"THE ORIGINAL FLOW in order (never the alternates of an alternate), stopping at the first 'pass'. 'env' means the machine "
            f"is in trouble (a server down): drain stops at once, `paused` (an attribute) becomes True, the task stays at the front "
            f"and that attempt is not counted, so after `resume()` the next drain() retries the same approach. A paused queue runs "
            f"nothing. Callers here use {alt}. Read CONVENTIONS.md.")
    src = _fill(ALT_SRC, **m)
    wrong = _mutate(src, "    def _approaches(self, flow):\n        return [flow] + [f for f in self.alternates.get(flow, []) if f != flow]\n",
                    ALT_WRONG_APPROACHES)
    return mod, goal, _fill(ALT_HIDDEN, **m), src, _fill(ALT_TESTS, **m), wrong


# ================================================================== 7. a stall detector
STALL_V = [("stall", "StallDetector", 3, 2), ("progress", "ProgressWatch", 2, 3), ("plateau", "PlateauGuard", 4, 2),
           ("spin", "SpinCheck", 3, 3), ("treadmill", "Treadmill", 2, 2)]
STALL_SRC = r'''"""Is a fix loop still getting anywhere? Stalled = no new best score for `window` tries, or the same reply `repeat_limit` times in a row."""


def _norm(reply):
    return " ".join(str(reply).split())


class @CLS@:
    def __init__(self, window=3, repeat_limit=2):
        if window < 1 or repeat_limit < 2:
            raise ValueError("window must be at least 1 and repeat_limit at least 2")
        self.window = window
        self.repeat_limit = repeat_limit
        self.scores = []
        self.replies = []

    def record(self, score, reply):
        self.scores.append(score)
        self.replies.append(_norm(reply))

    def reason(self):
        tail = self.replies[-self.repeat_limit:]
        if len(tail) == self.repeat_limit and len(set(tail)) == 1:
            return "repeating"
        if len(self.scores) > self.window:
            best_before = max(self.scores[:-self.window])
            if max(self.scores[-self.window:]) <= best_before:
                return "no progress"
        return None

    def stalled(self):
        return self.reason() is not None
'''
STALL_TESTS = r'''from @MOD@ import @CLS@  # noqa: E402


class StallTest(unittest.TestCase):
    def test_improving_is_not_stalled(self):
        d = @CLS@(@W@, @K@)
        for i in range(10):
            d.record(i, "reply %d" % i)
        self.assertFalse(d.stalled())

    def test_repeating_reply(self):
        d = @CLS@(@W@, @K@)
        for i in range(@K@):
            d.record(i, "same")
        self.assertEqual(d.reason(), "repeating")
'''
STALL_HIDDEN = r'''
from @MOD@ import @CLS@
W, K = @W@, @K@
d = @CLS@(W, K)
d.record(1, "a")
d.record(5, "b")
for i in range(W - 1):
    d.record(3, "c%d" % i)
assert not d.stalled(), "not yet a full window without progress"
d.record(4, "z")
assert d.stalled() and d.reason() == "no progress", "the last tries never beat the BEST before them (5), only the first (1)"
d2 = @CLS@(W, K)
for i in range(W + 3):
    d2.record(i, "r%d" % i)
assert not d2.stalled() and d2.reason() is None, "steady improvement is not a stall"
d3 = @CLS@(W, K)
d3.record(0, "x")
for i in range(K):
    assert not d3.stalled(), i
    d3.record(10 + i, "same reply" + " " * i + "\n" * (i % 2))
assert d3.stalled() and d3.reason() == "repeating", "the same reply, whitespace aside, K times in a row"
d4 = @CLS@(W, K)
for i in range(2 * K + 2):
    d4.record(i, "ab"[i % 2])
assert not d4.stalled(), "the same reply must repeat IN A ROW"
try:
    @CLS@(W, 1)
    raise SystemExit("repeat_limit 1 was accepted")
except ValueError:
    pass
print("ok")
'''


def t_stall(v):
    mod, cls, w, k = v
    m = dict(MOD=mod, CLS=cls, W=w, K=k)
    goal = (f"Build a Python module `{mod}.py`: the stall detector of an agent's fix loop, with tests in tests/. Public interface, "
            f"exactly: `from {mod} import {cls}`; `{cls}(window=3, repeat_limit=2)` (window below 1 or repeat_limit below 2 raises "
            f"ValueError). `record(score, reply)` is called after each try (score: a number, higher is better, e.g. passing tests; "
            f"reply: the model's text). `reason()` returns 'repeating' when the last repeat_limit replies are the same once whitespace "
            f"is normalised; else 'no progress' when more than `window` tries are recorded and none of the last `window` scores beats "
            f"the BEST score of all the tries before them; else None. `stalled()` is reason() is not None. Callers here use "
            f"window={w}, repeat_limit={k}. Read CONVENTIONS.md.")
    src = _fill(STALL_SRC, **m)
    wrong = _mutate(src, "best_before = max(self.scores[:-self.window])", "best_before = self.scores[0]")
    return mod, goal, _fill(STALL_HIDDEN, **m), src, _fill(STALL_TESTS, **m), wrong


# ================================================================== 8. a cost ledger
LEDGER_V = [("ledger", "CostLedger", "planner", "qwen3:4b"), ("spendlog", "SpendLog", "router", "gemma3:1b"),
            ("usage", "UsageBook", "generator", "qwen2.5-coder:7b"), ("costbook", "CostBook", "tester", "phi4-mini"),
            ("meterlog", "MeterLog", "reviewer", "llama3.2:3b")]
LEDGER_SRC = r'''"""What each model call cost: one JSON line per call, appended (a crash loses at most the line being written), totals on demand."""
import json
import os

GROUPS = ("role", "model", "run_id")


class @CLS@:
    def __init__(self, path):
        self.path = path

    def record(self, run_id, role, model, tokens_in, tokens_out, seconds, cached=False):
        if tokens_in < 0 or tokens_out < 0 or seconds < 0:
            raise ValueError("tokens and seconds cannot be negative")
        row = {"run_id": run_id, "role": role, "model": model, "tokens_in": int(tokens_in), "tokens_out": int(tokens_out),
               "seconds": float(seconds), "cached": bool(cached)}
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
            f.flush()
        return row

    def rows(self):
        if not os.path.isfile(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
        return out

    def totals(self, by="role"):
        if by not in GROUPS:
            raise ValueError(f"group by one of {GROUPS}")
        out = {}
        for row in self.rows():
            t = out.setdefault(row.get(by), {"calls": 0, "tokens_in": 0, "tokens_out": 0, "seconds": 0.0, "cached": 0})
            t["calls"] += 1
            if row.get("cached"):
                t["cached"] += 1
            else:
                t["tokens_in"] += row.get("tokens_in", 0)
                t["tokens_out"] += row.get("tokens_out", 0)
            t["seconds"] = round(t["seconds"] + row.get("seconds", 0.0), 3)
        return out
'''
LEDGER_TESTS = r'''import tempfile  # noqa: E402

from @MOD@ import @CLS@  # noqa: E402


class LedgerTest(unittest.TestCase):
    def test_totals_survive_a_new_instance(self):
        path = os.path.join(tempfile.mkdtemp(), "costs.jsonl")
        @CLS@(path).record("r1", @ROLE@, @MODEL@, 100, 20, 1.5)
        t = @CLS@(path).totals("role")
        self.assertEqual(t[@ROLE@]["calls"], 1)
        self.assertEqual(t[@ROLE@]["tokens_in"], 100)

    def test_negative_tokens(self):
        with self.assertRaises(ValueError):
            @CLS@(os.path.join(tempfile.mkdtemp(), "c.jsonl")).record("r", "x", "m", -1, 0, 0)
'''
LEDGER_HIDDEN = r'''
import os
import tempfile
from @MOD@ import @CLS@
ROLE, MODEL = @ROLE@, @MODEL@
p = os.path.join(tempfile.mkdtemp(), "sub", "costs.jsonl")
L = @CLS@(p)
L.record("r1", ROLE, MODEL, 1000, 200, 2.5)
L.record("r1", "generator2", MODEL, 3000, 800, 10.25)
L.record("r2", "generator2", MODEL, 3000, 800, 0.0, cached=True)
L.record("r2", "reviewer2", "other:2b", 500, 100, 1.125)
t = L.totals("role")
assert t["generator2"] == {"calls": 2, "tokens_in": 3000, "tokens_out": 800, "seconds": 10.25, "cached": 1}, ("a cached call counts, its tokens do not", t["generator2"])
assert t[ROLE]["calls"] == 1 and t["reviewer2"]["tokens_out"] == 100
with open(p, "a", encoding="utf-8") as f:
    f.write('{"run_id": "r3", "role": "plan')
    f.write("\n")
L2 = @CLS@(p)
m = L2.totals("model")
assert m[MODEL]["calls"] == 3 and m[MODEL]["tokens_in"] == 4000 and m["other:2b"]["seconds"] == 1.125, ("a half-written line is skipped", m)
L2.record("r3", ROLE, MODEL, 10, 5, 0.5)
assert @CLS@(p).totals("run_id")["r3"]["calls"] == 1
assert sum(v["calls"] for v in @CLS@(p).totals("run_id").values()) == 5
try:
    L2.totals("colour")
    raise SystemExit("an unknown grouping was accepted")
except ValueError:
    pass
try:
    L2.record("r4", "x", "m", -1, 0, 0)
    raise SystemExit("negative tokens were accepted")
except ValueError:
    pass
print("ok")
'''


def t_ledger(v):
    mod, cls, role, model = v
    m = dict(MOD=mod, CLS=cls, ROLE=repr(role), MODEL=repr(model))
    goal = (f"Build a Python module `{mod}.py`: the cost ledger of an agent harness, with tests in tests/. Public interface, exactly: "
            f"`from {mod} import {cls}`; `{cls}(path)` keeps one JSON object per line in the file at path (folders and file created "
            f"when missing). `record(run_id, role, model, tokens_in, tokens_out, seconds, cached=False)` appends one line (negative "
            f"tokens or seconds raise ValueError). `totals(by='role')` groups by 'role', 'model' or 'run_id' (anything else raises "
            f"ValueError) and returns {{key: {{'calls', 'tokens_in', 'tokens_out', 'seconds', 'cached'}}}}: a cached call counts in "
            f"calls and cached but adds no tokens; seconds are summed and rounded to 3 decimals. A new {cls} on the same file sees "
            f"every earlier record, and a line that is not valid JSON (a crash mid-write) is skipped, never fatal. The first entries "
            f"will be the {role} role on {model}. Read CONVENTIONS.md.")
    src = _fill(LEDGER_SRC, **m)
    wrong = _mutate(src, "                try:\n                    row = json.loads(line)\n                except ValueError:\n                    continue\n",
                    "                row = json.loads(line)\n")
    return mod, goal, _fill(LEDGER_HIDDEN, **m), src, _fill(LEDGER_TESTS, **m), wrong


# ------------------------------------------------------------------ the family
TEMPLATES = [
    ("gate", "easy", GATE_V, t_gate),
    ("budget", "easy", BUDGET_V, t_budget),
    ("stall", "easy", STALL_V, t_stall),
    ("ledger", "medium", LEDGER_V, t_ledger),
    ("prompt", "medium", PROMPT_V, t_prompt),
    ("reask", "medium", REASK_V, t_reask),
    ("tools", "medium", TOOLS_V, t_tools),
    ("alternates", "hard", ALT_V, t_alt),
]


def _task(key, level, k, built):
    mod, goal, hidden, src, tests, wrong = built
    tpath = f"tests/test_{mod}.py"
    test_src = TEST_HEAD + tests + TEST_TAIL
    return {
        "id": f"{PREFIX}-{key}-{k:02d}", "template": key,
        "tags": [TAG, "training", "python", "from-nothing", key, "level-" + level] + (["holdout"] if k == HOLDOUT else []),
        "goal": goal, "params": {"domains": ["api"]},
        "fixture": {"files": {"fakemodel.py": FAKEMODEL, "CONVENTIONS.md": CONVENTIONS}},
        "budget": {"calls": 24, "tokens": 90000, "seconds": 1200},
        "judge": [{"type": "file_exists", "paths": [f"{mod}.py", "tests/test_*.py"]},
                  {"type": "files_unchanged", "paths": ["fakemodel.py"]},
                  {"type": "python_assert", "code": hidden, "timeout": 90},
                  {"type": "tests_pass"}],
        "solution": {"files": {f"{mod}.py": src, tpath: test_src}},
        "wrong": {"files": {f"{mod}.py": wrong, tpath: test_src}},
    }


def all_tasks():
    return [_task(key, level, k, build(v)) for key, level, variants, build in TEMPLATES for k, v in enumerate(variants)]


def generate(n, seed=0):
    """n tasks, round-robin over the templates in a seed-stable order."""
    by_key = {}
    for t in all_tasks():
        by_key.setdefault(t["template"], []).append(t)
    keys = sorted(by_key)
    random.Random(f"{seed}:{TAG}").shuffle(keys)
    out, k = [], 0
    while len(out) < sum(len(v) for v in by_key.values()):
        out += [by_key[key][k] for key in keys if k < len(by_key[key])]
        k += 1
    return out[:max(0, n)]


if __name__ == "__main__":
    import distributed
    sys.exit(distributed.main(family="harness"))
