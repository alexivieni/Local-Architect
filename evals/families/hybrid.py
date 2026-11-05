"""hybrid: ten representative tasks (3 bug fixes, 3 features, 2 refactors, 2 designs) run under every Use Claude mode by
tools/hybrid_bench.py, so local-only, escalation, plan and all are compared on the SAME work.

Each task is self-contained (its own small module, its own honest tests), judged by code: a hidden behaviour check the model never
sees, the project's tests, and structure checks where the request is about structure. Each carries a reference solution and a
plausible wrong one; `tools/hybrid_bench.py --selftest` proves the judges pass the first and catch the second.

Kept out of the normal `eval` on purpose: the regular baselines (`eval --quick`, `--set representative`) must not change because
this family exists. `generate()` returns nothing unless asked for (force=True, or ARCHITECT_HYBRID_TASKS=1 in the environment):

    ARCHITECT_HYBRID_TASKS=1 python architect.py eval --tags hybrid --fake      the plumbing, through the normal eval
"""
import os

import design as _design            # the design family's spec / architecture / ADR texts (already proven against the doc checks)

FLOW = "bugfix"
DEFAULT_N = 10
ENV = "ARCHITECT_HYBRID_TASKS"


# ------------------------------------------------------------------------------------------------------ bug fixes
PAGER = '''"""Pagination helpers."""


def paginate(items, page, size):
    """Rows for a 1-based page number; an empty list past the end."""
    if page < 1 or size < 1:
        raise ValueError("page and size start at 1")
    start = (page - 1) * size
    return list(items[start:start + size])


def page_count(n, size):
    """How many pages n rows fill."""
    if size < 1:
        raise ValueError("size starts at 1")
    return (n + size - 1) // size
'''
PAGER_TESTS = '''import unittest

from pager import page_count, paginate


class TestPager(unittest.TestCase):
    def test_first_page(self):
        self.assertEqual(paginate(list(range(10)), 1, 3), [0, 1, 2])

    def test_last_page(self):
        self.assertEqual(paginate(list(range(10)), 4, 3), [9])

    def test_page_count(self):
        self.assertEqual(page_count(10, 3), 4)
        self.assertEqual(page_count(0, 3), 0)

    def test_bad_page(self):
        with self.assertRaises(ValueError):
            paginate([1], 0, 3)


if __name__ == "__main__":
    unittest.main()
'''

MONEY = '''"""Splitting amounts in whole cents."""


def split_bill(total_cents, people):
    """Shares that add up to exactly total_cents; the first shares carry the leftover cents."""
    if people < 1:
        raise ValueError("at least one person")
    base, extra = divmod(total_cents, people)
    return [base + 1 if i < extra else base for i in range(people)]


def to_cents(amount_text):
    """'12.34' -> 1234, '5' -> 500."""
    whole, _, frac = amount_text.strip().partition(".")
    return int(whole or 0) * 100 + int((frac + "00")[:2])
'''
MONEY_TESTS = '''import unittest

from money import split_bill, to_cents


class TestMoney(unittest.TestCase):
    def test_even_split(self):
        self.assertEqual(split_bill(900, 3), [300, 300, 300])

    def test_uneven_split_adds_up(self):
        self.assertEqual(sum(split_bill(1000, 3)), 1000)

    def test_to_cents(self):
        self.assertEqual(to_cents("12.34"), 1234)
        self.assertEqual(to_cents("5"), 500)


if __name__ == "__main__":
    unittest.main()
'''

CAL = '''"""Calendar helpers."""


def is_leap(year):
    """Gregorian leap year."""
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def days_in_month(year, month):
    """Days in a month of a year (month 1..12)."""
    if not 1 <= month <= 12:
        raise ValueError("month is 1..12")
    if month == 2:
        return 29 if is_leap(year) else 28
    return 30 if month in (4, 6, 9, 11) else 31
'''
CAL_TESTS = '''import unittest

from calendar_util import days_in_month, is_leap


class TestCalendar(unittest.TestCase):
    def test_2000_is_leap(self):
        self.assertTrue(is_leap(2000))

    def test_1900_is_not_leap(self):
        self.assertFalse(is_leap(1900))

    def test_february_2024(self):
        self.assertEqual(days_in_month(2024, 2), 29)

    def test_april(self):
        self.assertEqual(days_in_month(2023, 4), 30)


if __name__ == "__main__":
    unittest.main()
'''


# ------------------------------------------------------------------------------------------------------ features
STOCK = '''"""A small stock list."""


class Stock:
    def __init__(self):
        self._qty = {}

    def add(self, name, qty):
        if qty <= 0:
            raise ValueError("qty must be positive")
        self._qty[name] = self._qty.get(name, 0) + qty

    def remove(self, name, qty):
        have = self._qty.get(name, 0)
        if qty <= 0 or qty > have:
            raise ValueError(f"cannot remove {qty} {name}: have {have}")
        self._qty[name] = have - qty

    def qty(self, name):
        return self._qty.get(name, 0)
'''
STOCK_TESTS = '''import unittest

from inventory import Stock


class TestStock(unittest.TestCase):
    def test_add_and_remove(self):
        s = Stock()
        s.add("flour", 5)
        s.remove("flour", 2)
        self.assertEqual(s.qty("flour"), 3)

    def test_remove_too_much(self):
        s = Stock()
        s.add("eggs", 1)
        with self.assertRaises(ValueError):
            s.remove("eggs", 2)


if __name__ == "__main__":
    unittest.main()
'''
LOW_STOCK = '''
    def low_stock(self, threshold):
        """Names with less than `threshold` in stock, alphabetical (an item at 0 counts)."""
        return sorted(name for name, q in self._qty.items() if q < threshold)
'''

TEXT = '''"""Word statistics."""
import re

WORD = re.compile(r"[a-z']+")


def words(text):
    """The words of a text, lower-cased, in order."""
    return WORD.findall(text.lower())


def word_count(text):
    return len(words(text))
'''
TEXT_TESTS = '''import unittest

from textstats import word_count, words


class TestText(unittest.TestCase):
    def test_words(self):
        self.assertEqual(words("Hello, hello World"), ["hello", "hello", "world"])

    def test_count(self):
        self.assertEqual(word_count("one two  three"), 3)


if __name__ == "__main__":
    unittest.main()
'''
TOP_WORDS = '''

def top_words(text, n):
    """The n most frequent words as (word, count), most frequent first, ties in alphabetical order."""
    counts = {}
    for w in words(text):
        counts[w] = counts.get(w, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
'''
TOP_WORDS_WRONG = '''

def top_words(text, n):
    """The n most frequent words as (word, count)."""
    from collections import Counter
    return Counter(words(text)).most_common(n)
'''

KV = '''"""An in-memory key-value store."""


class Store:
    def __init__(self):
        self._data = {}

    def set(self, key, value):
        self._data[key] = value

    def get(self, key, default=None):
        return self._data.get(key, default)
'''
KV_TESTS = '''import unittest

from kvstore import Store


class TestStore(unittest.TestCase):
    def test_set_get(self):
        s = Store()
        s.set("a", 1)
        self.assertEqual(s.get("a"), 1)
        self.assertEqual(s.get("missing", 0), 0)


if __name__ == "__main__":
    unittest.main()
'''
KV_MORE = '''
    def delete(self, key):
        """True when the key was there and is now gone, False when it was not there."""
        if key in self._data:
            del self._data[key]
            return True
        return False

    def keys(self, prefix=""):
        """The keys that start with prefix, sorted."""
        return sorted(k for k in self._data if k.startswith(prefix))
'''
KV_MORE_WRONG = '''
    def delete(self, key):
        self._data.pop(key, None)

    def keys(self, prefix=""):
        return sorted(k for k in self._data if k.startswith(prefix))
'''


# ------------------------------------------------------------------------------------------------------ refactors
REPORT = '''"""Spending totals by category."""


def total_food(items):
    total = 0
    for item in items:
        if item["category"] == "food":
            total += item["amount"]
    return total


def total_drink(items):
    total = 0
    for item in items:
        if item["category"] == "drink":
            total += item["amount"]
    return total


def total_other(items):
    total = 0
    for item in items:
        if item["category"] not in ("food", "drink"):
            total += item["amount"]
    return total
'''
REPORT_CLEAN = '''"""Spending totals by category."""


def total_for(items, category):
    """Sum of the amounts in one category."""
    return sum(item["amount"] for item in items if item["category"] == category)


def total_food(items):
    return total_for(items, "food")


def total_drink(items):
    return total_for(items, "drink")


def total_other(items):
    total = 0
    for item in items:
        if item["category"] not in ("food", "drink"):
            total += item["amount"]
    return total
'''
REPORT_TESTS = '''import unittest

from report import total_drink, total_food, total_other

ITEMS = [{"category": "food", "amount": 3}, {"category": "drink", "amount": 2},
         {"category": "rent", "amount": 10}, {"category": "food", "amount": 4}]


class TestReport(unittest.TestCase):
    def test_food(self):
        self.assertEqual(total_food(ITEMS), 7)

    def test_drink(self):
        self.assertEqual(total_drink(ITEMS), 2)

    def test_other(self):
        self.assertEqual(total_other(ITEMS), 10)

    def test_empty(self):
        self.assertEqual(total_food([]), 0)


if __name__ == "__main__":
    unittest.main()
'''
REPORT_CHECK = '''import ast
import report
items = [{"category": "food", "amount": 3}, {"category": "drink", "amount": 2}, {"category": "rent", "amount": 10}, {"category": "food", "amount": 4}]
assert report.total_food(items) == 7 and report.total_drink(items) == 2 and report.total_other(items) == 10, "behaviour changed"
assert report.total_for(items, "rent") == 10 and report.total_for([], "food") == 0, "total_for is wrong"
with open(report.__file__, encoding="utf-8") as f:
    tree = ast.parse(f.read())
fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
for name in ("total_food", "total_drink"):
    body = [s for s in fns[name].body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
    calls = [c.func.id for c in ast.walk(fns[name]) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
    assert len(body) == 1 and "total_for" in calls, name + " still has its own loop"
'''

SIGNUP = '''"""Account checks."""


def register(email, password):
    email = email.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@") or " " in email:
        raise ValueError("invalid email")
    if len(password) < 8:
        raise ValueError("password too short")
    return {"email": email}


def update_profile(profile, email):
    email = email.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@") or " " in email:
        raise ValueError("invalid email")
    return dict(profile, email=email)
'''
SIGNUP_CLEAN = '''"""Account checks."""


def normalize_email(email):
    """The address trimmed and lower-cased; ValueError when it is not an address."""
    email = email.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@") or " " in email:
        raise ValueError("invalid email")
    return email


def register(email, password):
    email = normalize_email(email)
    if len(password) < 8:
        raise ValueError("password too short")
    return {"email": email}


def update_profile(profile, email):
    return dict(profile, email=normalize_email(email))
'''
SIGNUP_WRONG = '''"""Account checks."""


def normalize_email(email):
    """The address trimmed and lower-cased; ValueError when it is not an address."""
    email = email.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@") or " " in email:
        raise ValueError("invalid email")
    return email


def register(email, password):
    email = normalize_email(email)
    if len(password) < 8:
        raise ValueError("password too short")
    return {"email": email}


def update_profile(profile, email):
    email = email.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@") or " " in email:
        raise ValueError("invalid email")
    return dict(profile, email=email)
'''
SIGNUP_TESTS = '''import unittest

from signup import register, update_profile


class TestSignup(unittest.TestCase):
    def test_register_cleans(self):
        self.assertEqual(register(" Ann@Example.org ", "longenough"), {"email": "ann@example.org"})

    def test_register_rejects(self):
        with self.assertRaises(ValueError):
            register("not-an-address", "longenough")

    def test_short_password(self):
        with self.assertRaises(ValueError):
            register("a@b.io", "short")

    def test_update(self):
        self.assertEqual(update_profile({"name": "Ann"}, "ANN@b.io"), {"name": "Ann", "email": "ann@b.io"})


if __name__ == "__main__":
    unittest.main()
'''
SIGNUP_CHECK = '''import ast
import signup
assert signup.register(" A@B.io ", "longpassword") == {"email": "a@b.io"}
assert signup.update_profile({"n": 1}, "X@Y.z") == {"n": 1, "email": "x@y.z"}
assert signup.normalize_email("  Q@R.st") == "q@r.st"
for bad in ("ab", "@b", "a@", "a b@c"):
    for call in (lambda: signup.normalize_email(bad), lambda: signup.register(bad, "longpassword"), lambda: signup.update_profile({}, bad)):
        try:
            call()
        except ValueError:
            continue
        raise SystemExit("accepted a bad address: " + bad)
with open(signup.__file__, encoding="utf-8") as f:
    tree = ast.parse(f.read())
fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
for name in ("register", "update_profile"):
    own = [c for c in ast.walk(fns[name]) if isinstance(c, ast.Constant) and c.value == "@"]
    calls = [c.func.id for c in ast.walk(fns[name]) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
    assert not own and "normalize_email" in calls, name + " still checks the address itself"
'''


# ------------------------------------------------------------------------------------------------------ the tasks
def _bugfix(tid, mod, src, bug_from, bug_to, tests, goal, hidden, repro, wrong):
    tpath = f"tests/test_{mod}.py"
    assert bug_from in src, f"{tid}: the planted bug does not apply"
    return {"id": tid, "flow": "bugfix", "tags": ["hybrid", "bugfix", "python"], "goal": goal,
            "fixture": {"files": {f"{mod}.py": src.replace(bug_from, bug_to, 1), tpath: tests}},
            "budget": {"calls": 20, "tokens": 60000, "seconds": 900},
            "judge": [{"type": "files_unchanged", "paths": [tpath]}, {"type": "python_assert", "code": hidden}, {"type": "tests_pass"}],
            "solution": {"files": {f"{mod}.py": src}},
            "oracle": {"repro": {"tests/test_repro_bug.py": f"from {mod} import *\n\n\ndef test_repro():\n    {repro}\n"}},
            "wrong": {"files": wrong}}


def _feature(tid, mod, src, tests, goal, solution, wrong, hidden, names):
    tpath = f"tests/test_{mod}.py"
    return {"id": tid, "flow": "feature", "tags": ["hybrid", "feature", "python"], "goal": goal,
            "fixture": {"files": {f"{mod}.py": src, tpath: tests}},
            "budget": {"calls": 24, "tokens": 80000, "seconds": 1200},
            "judge": [{"type": "functions_present", "path": f"{mod}.py", "names": names}, {"type": "python_assert", "code": hidden},
                      {"type": "tests_pass"}, {"type": "no_shrink", "path": f"{mod}.py"}],
            "solution": {"files": {f"{mod}.py": solution}}, "wrong": {"files": {f"{mod}.py": wrong}}}


def _refactor(tid, mod, src, tests, goal, clean, wrong, check, names):
    tpath = f"tests/test_{mod}.py"
    return {"id": tid, "flow": "refactor", "tags": ["hybrid", "refactor", "python"], "goal": goal,
            "fixture": {"files": {f"{mod}.py": src, tpath: tests}},
            "budget": {"calls": 20, "tokens": 70000, "seconds": 900},
            "judge": [{"type": "functions_present", "path": f"{mod}.py", "names": names}, {"type": "files_unchanged", "paths": [tpath]},
                      {"type": "python_assert", "code": check}, {"type": "tests_pass"}],
            "solution": {"files": {f"{mod}.py": clean}},
            "oracle": {"characterization": {f"tests/characterization/test_{mod}_char.py": tests}},
            "wrong": {"files": {f"{mod}.py": wrong}}}


def _design_task(tid, goal):
    spec = _design._spec(goal)
    return {"id": tid, "flow": "design", "tags": ["hybrid", "design"], "goal": goal, "params": {"domains": ["api"]},
            "fixture": {"files": {}},
            "budget": {"calls": 10, "tokens": 50000, "seconds": 900},
            "judge": [{"type": "docs_structure", "kind": "spec", "path": "docs/spec.md"},
                      {"type": "file_exists", "paths": ["docs/architecture.md", "docs/adr/*.md"]},
                      {"type": "docs_structure", "kind": "mermaid", "path": "docs/architecture.md"}],
            "solution": {"files": {"docs/spec.md": spec, "docs/architecture.md": _design.ARCH, "docs/adr/0001-sqlite-store.md": _design.ADR}},
            "oracle": {"spec": spec, "design": "DOC: docs/architecture.md\n" + _design.ARCH + "\nDOC: docs/adr/0001-sqlite-store.md\n" + _design.ADR},
            "wrong": {"files": {"docs/spec.md": "# Spec\nTo be decided.\n"}}}


def tasks():
    """The ten tasks, always in this order (ids are stable, so two benchmark runs compare task by task)."""
    return [
        _bugfix("hybrid-bugfix-pager", "pager", PAGER, "start = (page - 1) * size", "start = page * size", PAGER_TESTS,
                "tests/test_pager.py fails: paginate() in pager.py returns the second page's rows for page 1. Fix the bug in pager.py "
                "without changing the tests.",
                "from pager import paginate\nassert paginate(list(range(10)), 2, 3) == [3, 4, 5]\nassert paginate(list(range(10)), 5, 3) == []\n"
                "assert paginate('abcdef', 1, 4) == ['a', 'b', 'c', 'd']",
                "assert paginate(list(range(10)), 1, 3) == [0, 1, 2]",
                {"tests/test_pager.py": PAGER_TESTS.replace("[0, 1, 2]", "[3, 4, 5]")}),        # 'fixes' the test, not the code
        _bugfix("hybrid-bugfix-money", "money", MONEY, "return [base + 1 if i < extra else base for i in range(people)]",
                "return [base] * people", MONEY_TESTS,
                "split_bill() in money.py loses cents: the shares do not add up to the total (tests/test_money.py fails). Fix it so the "
                "shares always add up and differ by at most one cent.",
                "from money import split_bill\nfor total, n in ((1001, 4), (1003, 4), (7, 3), (0, 2)):\n    s = split_bill(total, n)\n"
                "    assert len(s) == n and sum(s) == total and max(s) - min(s) <= 1, (total, n, s)",
                "assert sum(split_bill(1000, 3)) == 1000",
                {"money.py": MONEY.replace("return [base + 1 if i < extra else base for i in range(people)]",
                                           "return [base + extra] + [base] * (people - 1)")}),   # adds up, but one share takes every cent
        _bugfix("hybrid-bugfix-leap", "calendar_util", CAL, "return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)",
                "return year % 4 == 0 and year % 100 != 0", CAL_TESTS,
                "calendar_util.is_leap(2000) says False, so February 2000 has 28 days (tests/test_calendar_util.py fails). Fix the "
                "leap-year rule in calendar_util.py.",
                "from calendar_util import days_in_month, is_leap\nassert is_leap(2400) and is_leap(2024) and not is_leap(2100) and not is_leap(2023)\n"
                "assert days_in_month(2000, 2) == 29 and days_in_month(1900, 2) == 28",
                "assert is_leap(2000)",
                {"calendar_util.py": CAL.replace("return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)", "return year % 4 == 0")}),
        _feature("hybrid-feature-lowstock", "inventory", STOCK, STOCK_TESTS,
                 "Add a method Stock.low_stock(threshold) to inventory.py: it returns the names whose quantity is below threshold, "
                 "sorted alphabetically. An item removed down to 0 still counts (as low). Keep the existing behaviour.",
                 STOCK.rstrip("\n") + "\n" + LOW_STOCK, STOCK.rstrip("\n") + "\n" + LOW_STOCK.replace("q < threshold", "q <= threshold"),
                 "from inventory import Stock\ns = Stock()\ns.add('flour', 5)\ns.add('eggs', 12)\ns.add('yeast', 1)\ns.remove('flour', 5)\n"
                 "assert s.low_stock(3) == ['flour', 'yeast'], s.low_stock(3)\nassert s.low_stock(1) == ['flour'], s.low_stock(1)\n"
                 "assert s.low_stock(0) == []\nassert s.qty('eggs') == 12",
                 ["Stock", "add", "remove", "qty", "low_stock"]),
        _feature("hybrid-feature-topwords", "textstats", TEXT, TEXT_TESTS,
                 "Add top_words(text, n) to textstats.py: the n most frequent words as a list of (word, count) tuples, most frequent "
                 "first, ties in alphabetical order, case-insensitive (use words()). If n is larger than the number of distinct words, "
                 "return them all.",
                 TEXT.rstrip("\n") + "\n" + TOP_WORDS, TEXT.rstrip("\n") + "\n" + TOP_WORDS_WRONG,
                 "from textstats import top_words\nassert top_words('b a B c a b', 2) == [('b', 3), ('a', 2)]\n"
                 "assert top_words('z y x', 5) == [('x', 1), ('y', 1), ('z', 1)]\nassert top_words('', 3) == []",
                 ["words", "word_count", "top_words"]),
        _feature("hybrid-feature-kvdelete", "kvstore", KV, KV_TESTS,
                 "Add two methods to Store in kvstore.py: delete(key) returns True when the key was there and is now removed, False "
                 "when it was not there; keys(prefix=\"\") returns the keys starting with prefix, sorted.",
                 KV.rstrip("\n") + "\n" + KV_MORE, KV.rstrip("\n") + "\n" + KV_MORE_WRONG,
                 "from kvstore import Store\ns = Store()\ns.set('user:1', 'a')\ns.set('user:2', 'b')\ns.set('order:1', 'c')\n"
                 "assert s.delete('user:1') is True\nassert s.delete('user:1') is False\nassert s.keys('user:') == ['user:2']\n"
                 "assert s.keys() == ['order:1', 'user:2']\nassert s.get('user:1') is None",
                 ["Store", "set", "get", "delete", "keys"]),
        _refactor("hybrid-refactor-totals", "report", REPORT, REPORT_TESTS,
                  "Refactor report.py: total_food and total_drink duplicate the same loop. Add total_for(items, category) and make "
                  "total_food and total_drink one-line calls to it. total_other keeps its behaviour. No behaviour change.",
                  REPORT_CLEAN, REPORT_CLEAN.replace('def total_food(items):\n    return total_for(items, "food")',
                                                     'def total_food(items):\n    total = 0\n    for item in items:\n'
                                                     '        if item["category"] == "food":\n            total += item["amount"]\n    return total'),
                  REPORT_CHECK, ["total_for", "total_food", "total_drink", "total_other"]),
        _refactor("hybrid-refactor-email", "signup", SIGNUP, SIGNUP_TESTS,
                  "Refactor signup.py: the email check is copy-pasted in register and update_profile. Move it into one helper "
                  "normalize_email(email) that returns the trimmed, lower-cased address or raises ValueError, and use it in both. "
                  "No behaviour change.",
                  SIGNUP_CLEAN, SIGNUP_WRONG, SIGNUP_CHECK, ["normalize_email", "register", "update_profile"]),
        _design_task("hybrid-design-loans", "Design a small library-loan service: books, members, and loans with due dates; a member may "
                                            "hold at most 3 loans at once."),
        _design_task("hybrid-design-relay", "Design a webhook relay: receive events over HTTP, deliver them to subscribers, retry failed "
                                            "deliveries with backoff, and keep a delivery log."),
    ]


def generate(n, seed=0, force=False):
    """Nothing unless asked for (see the module docstring); the order is fixed, so `seed` changes nothing."""
    if not force and os.environ.get(ENV) != "1":
        return []
    out = tasks()
    return out[:n] if n else out
