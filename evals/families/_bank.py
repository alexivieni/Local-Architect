"""The fixture bank every eval family draws from: small, real modules with correct code, honest tests, and a list of
plausible bugs per module (each with the hidden check that exposes it). Families combine these into tasks; nothing here
is tied to one flow. Keep every module standard-library only and under ~40 lines so a 3B model can hold it."""
import random

# module -> {"src": correct source, "tests": unittest file, "funcs": public names, "bugs": [(name, search, replace, hidden_assert)]}
MODULES = {
    "stats": {
        "src": '''"""Small statistics helpers."""


def average(xs):
    """Mean of a non-empty list of numbers."""
    if not xs:
        raise ValueError("average of empty list")
    return sum(xs) / len(xs)


def total(xs):
    return sum(xs)


def median(xs):
    """Middle value; mean of the two middles for an even count."""
    if not xs:
        raise ValueError("median of empty list")
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def clamp(x, lo, hi):
    """x limited to the closed range [lo, hi]."""
    return max(lo, min(hi, x))
''',
        "tests": '''import unittest
from stats import average, total, median, clamp


class T(unittest.TestCase):
    def test_average(self):
        self.assertEqual(average([2, 4, 6]), 4)

    def test_total(self):
        self.assertEqual(total([1, 2]), 3)

    def test_median_odd(self):
        self.assertEqual(median([3, 1, 2]), 2)

    def test_median_even(self):
        self.assertEqual(median([1, 2, 3, 4]), 2.5)

    def test_clamp(self):
        self.assertEqual(clamp(5, 0, 3), 3)
        self.assertEqual(clamp(-1, 0, 3), 0)


if __name__ == "__main__":
    unittest.main()
''',
        "funcs": ["average", "total", "median", "clamp"],
        "bugs": [
            ("off-by-one in average", "return sum(xs) / len(xs)", "return sum(xs) / (len(xs) - 1)", "from stats import average\nassert average([2,4,6]) == 4 and average([10]) == 10"),
            ("median ignores the even case", "    return (s[mid - 1] + s[mid]) / 2", "    return s[mid]", "from stats import median\nassert median([1,2,3,4]) == 2.5 and median([3,1,2]) == 2"),
            ("clamp swaps the bounds", "return max(lo, min(hi, x))", "return min(lo, max(hi, x))", "from stats import clamp\nassert clamp(5,0,3) == 3 and clamp(-1,0,3) == 0 and clamp(2,0,3) == 2"),
            ("average of an empty list returns 0 instead of raising", '    if not xs:\n        raise ValueError("average of empty list")\n', '    if not xs:\n        return 0\n', "from stats import average\ntry:\n    average([])\n    raise SystemExit('no error raised')\nexcept ValueError:\n    pass\nassert average([1,3]) == 2"),
        ],
    },
    "inventory": {
        "src": '''"""A tiny in-memory inventory keyed by item name."""


def add_item(items, name, qty=1):
    """Add qty of name; quantities accumulate."""
    if qty <= 0:
        raise ValueError("qty must be positive")
    items[name] = items.get(name, 0) + qty
    return items


def remove_item(items, name, qty=1):
    """Remove qty of name; the item disappears at zero."""
    if name not in items:
        raise KeyError(name)
    left = items[name] - qty
    if left > 0:
        items[name] = left
    else:
        del items[name]
    return items


def count(items):
    return sum(items.values())


def names(items):
    return sorted(items)


def has(items, name):
    return name in items
''',
        "tests": '''import unittest
from inventory import add_item, remove_item, count, names, has


class T(unittest.TestCase):
    def test_add_accumulates(self):
        self.assertEqual(add_item({}, "a", 2)["a"], 2)
        self.assertEqual(add_item({"a": 2}, "a", 3)["a"], 5)

    def test_remove_to_zero_deletes(self):
        self.assertNotIn("a", remove_item({"a": 1}, "a"))

    def test_count_names_has(self):
        it = {"b": 1, "a": 2}
        self.assertEqual(count(it), 3)
        self.assertEqual(names(it), ["a", "b"])
        self.assertTrue(has(it, "a"))


if __name__ == "__main__":
    unittest.main()
''',
        "funcs": ["add_item", "remove_item", "count", "names", "has"],
        "bugs": [
            ("add_item overwrites instead of accumulating", "items[name] = items.get(name, 0) + qty", "items[name] = qty", "from inventory import add_item\nassert add_item({'a': 2}, 'a', 3)['a'] == 5"),
            ("remove_item leaves a zero entry", "    if left > 0:\n        items[name] = left\n    else:\n        del items[name]\n", "    items[name] = left\n", "from inventory import remove_item\nassert 'a' not in remove_item({'a': 1}, 'a')"),
            ("names is not sorted", "return sorted(items)", "return list(items)", "from inventory import names\nassert names({'b': 1, 'a': 2}) == ['a', 'b']"),
        ],
    },
    "calc": {
        "src": '''"""Four operations plus power, with explicit errors."""


def add(a, b):
    return a + b


def subtract(a, b):
    return a - b


def multiply(a, b):
    return a * b


def divide(a, b):
    """Raises ZeroDivisionError on b == 0."""
    if b == 0:
        raise ZeroDivisionError("divide by zero")
    return a / b


def power(a, n):
    """a to the integer power n (n >= 0)."""
    if n < 0:
        raise ValueError("n must be >= 0")
    result = 1
    for _ in range(n):
        result *= a
    return result
''',
        "tests": '''import unittest
from calc import add, subtract, multiply, divide, power


class T(unittest.TestCase):
    def test_ops(self):
        self.assertEqual(add(2, 3), 5)
        self.assertEqual(subtract(5, 2), 3)
        self.assertEqual(multiply(3, 4), 12)
        self.assertEqual(divide(8, 2), 4)

    def test_divide_by_zero(self):
        with self.assertRaises(ZeroDivisionError):
            divide(1, 0)

    def test_power(self):
        self.assertEqual(power(2, 3), 8)
        self.assertEqual(power(5, 0), 1)


if __name__ == "__main__":
    unittest.main()
''',
        "funcs": ["add", "subtract", "multiply", "divide", "power"],
        "bugs": [
            ("subtract has the operands reversed", "    return a - b", "    return b - a", "from calc import subtract\nassert subtract(5, 2) == 3"),
            ("power is off by one", "    for _ in range(n):", "    for _ in range(n - 1):", "from calc import power\nassert power(2, 3) == 8 and power(5, 0) == 1"),
            ("divide by zero returns 0 instead of raising", '    if b == 0:\n        raise ZeroDivisionError("divide by zero")\n', '    if b == 0:\n        return 0\n', "from calc import divide\ntry:\n    divide(1, 0)\n    raise SystemExit('no error')\nexcept ZeroDivisionError:\n    pass"),
        ],
    },
    "textutil": {
        "src": '''"""Text helpers."""
import re


def slugify(text):
    """'Hello, World!' -> 'hello-world'."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def word_count(text):
    return len(text.split())


def truncate(text, n):
    """At most n characters, with an ellipsis when cut."""
    if len(text) <= n:
        return text
    return text[: max(0, n - 1)] + "\\u2026"


def is_palindrome(text):
    s = re.sub(r"[^a-z0-9]", "", text.lower())
    return s == s[::-1]
''',
        "tests": '''import unittest
from textutil import slugify, word_count, truncate, is_palindrome


class T(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")
        self.assertEqual(slugify("  a   b "), "a-b")

    def test_word_count(self):
        self.assertEqual(word_count("one two  three"), 3)

    def test_truncate(self):
        self.assertEqual(truncate("abcdef", 4), "abc\\u2026")
        self.assertEqual(truncate("abc", 5), "abc")

    def test_palindrome(self):
        self.assertTrue(is_palindrome("A man, a plan, a canal: Panama"))
        self.assertFalse(is_palindrome("hello"))


if __name__ == "__main__":
    unittest.main()
''',
        "funcs": ["slugify", "word_count", "truncate", "is_palindrome"],
        "bugs": [
            ("slugify keeps punctuation", 'return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")', 'return text.lower().replace(" ", "-")', "from textutil import slugify\nassert slugify('Hello, World!') == 'hello-world'"),
            ("truncate is one character too long", '    return text[: max(0, n - 1)] + "\\u2026"', '    return text[:n] + "\\u2026"', "from textutil import truncate\nassert len(truncate('abcdef', 4)) == 4 and truncate('abc', 5) == 'abc'"),
            ("palindrome check is case-sensitive", '    s = re.sub(r"[^a-z0-9]", "", text.lower())', '    s = re.sub(r"[^a-z0-9]", "", text)', "from textutil import is_palindrome\nassert is_palindrome('Abba') and not is_palindrome('hello')"),
        ],
    },
    "cart": {
        "src": '''"""A shopping cart with line items and a percentage discount."""


class Cart:
    def __init__(self):
        self.items = {}

    def add(self, sku, price, qty=1):
        if qty <= 0:
            raise ValueError("qty must be positive")
        cur = self.items.get(sku, {"price": price, "qty": 0})
        cur["qty"] += qty
        cur["price"] = price
        self.items[sku] = cur

    def remove(self, sku):
        self.items.pop(sku, None)

    def total(self):
        return round(sum(v["price"] * v["qty"] for v in self.items.values()), 2)

    def apply_discount(self, pct):
        """Total after a percentage discount (0-100)."""
        if not 0 <= pct <= 100:
            raise ValueError("pct must be 0-100")
        return round(self.total() * (100 - pct) / 100, 2)
''',
        "tests": '''import unittest
from cart import Cart


class T(unittest.TestCase):
    def test_total(self):
        c = Cart()
        c.add("a", 2.5, 2)
        c.add("b", 1.0)
        self.assertEqual(c.total(), 6.0)

    def test_discount(self):
        c = Cart()
        c.add("a", 10.0)
        self.assertEqual(c.apply_discount(25), 7.5)

    def test_remove(self):
        c = Cart()
        c.add("a", 1.0)
        c.remove("a")
        self.assertEqual(c.total(), 0)


if __name__ == "__main__":
    unittest.main()
''',
        "funcs": ["Cart"],
        "bugs": [
            ("add does not accumulate quantity", '        cur["qty"] += qty', '        cur["qty"] = qty', "from cart import Cart\nc = Cart(); c.add('a', 1.0, 2); c.add('a', 1.0, 3)\nassert c.total() == 5.0"),
            ("discount applies the percentage the wrong way", "        return round(self.total() * (100 - pct) / 100, 2)", "        return round(self.total() * pct / 100, 2)", "from cart import Cart\nc = Cart(); c.add('a', 10.0)\nassert c.apply_discount(25) == 7.5 and c.apply_discount(0) == 10.0"),
        ],
    },
    "dates": {
        "src": '''"""Calendar helpers without datetime, so the rules are visible."""


def is_leap(year):
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def days_in_month(year, month):
    if month == 2:
        return 29 if is_leap(year) else 28
    return 30 if month in (4, 6, 9, 11) else 31


def day_of_year(year, month, day):
    """1-based day number within the year."""
    return sum(days_in_month(year, m) for m in range(1, month)) + day
''',
        "tests": '''import unittest
from dates import is_leap, days_in_month, day_of_year


class T(unittest.TestCase):
    def test_leap(self):
        self.assertTrue(is_leap(2000))
        self.assertFalse(is_leap(1900))
        self.assertTrue(is_leap(2024))

    def test_days(self):
        self.assertEqual(days_in_month(2024, 2), 29)
        self.assertEqual(days_in_month(2023, 4), 30)

    def test_day_of_year(self):
        self.assertEqual(day_of_year(2023, 1, 1), 1)
        self.assertEqual(day_of_year(2023, 3, 1), 60)


if __name__ == "__main__":
    unittest.main()
''',
        "funcs": ["is_leap", "days_in_month", "day_of_year"],
        "bugs": [
            ("leap year rule forgets the 400 exception", "    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)", "    return year % 4 == 0 and year % 100 != 0", "from dates import is_leap\nassert is_leap(2000) and not is_leap(1900) and is_leap(2024)"),
            ("day_of_year counts the current month too", "    return sum(days_in_month(year, m) for m in range(1, month)) + day", "    return sum(days_in_month(year, m) for m in range(1, month + 1)) + day", "from dates import day_of_year\nassert day_of_year(2023, 1, 1) == 1 and day_of_year(2023, 3, 1) == 60"),
        ],
    },
}

# features to add on top of a correct module: (module, function name, goal sentence, solution source appended, hidden assert, test appended)
FEATURES = [
    ("inventory", "low_stock", "Add a function low_stock(items, threshold) to inventory.py that returns the sorted names whose quantity is below threshold, with a test. Keep every existing function.",
     "\n\ndef low_stock(items, threshold):\n    \"\"\"Sorted names with quantity below threshold.\"\"\"\n    return sorted(n for n, q in items.items() if q < threshold)\n",
     "from inventory import low_stock\nassert low_stock({'a':1,'b':5,'c':0}, 2) == ['a','c'] and low_stock({}, 3) == []",
     "\n    def test_low_stock(self):\n        from inventory import low_stock\n        self.assertEqual(low_stock({'a': 1, 'b': 5}, 2), ['a'])\n"),
    ("stats", "variance", "Add a function variance(xs) to stats.py (population variance; raise ValueError on an empty list), with a test. Keep every existing function.",
     "\n\ndef variance(xs):\n    \"\"\"Population variance.\"\"\"\n    if not xs:\n        raise ValueError(\"variance of empty list\")\n    m = sum(xs) / len(xs)\n    return sum((x - m) ** 2 for x in xs) / len(xs)\n",
     "from stats import variance\nassert variance([2,4,4,4,5,5,7,9]) == 4.0",
     "\n    def test_variance(self):\n        from stats import variance\n        self.assertEqual(variance([2, 4, 4, 4, 5, 5, 7, 9]), 4.0)\n"),
    ("calc", "modulo", "Add a function modulo(a, b) to calc.py that returns a % b and raises ZeroDivisionError when b is 0, with a test. Keep every existing function.",
     "\n\ndef modulo(a, b):\n    if b == 0:\n        raise ZeroDivisionError(\"modulo by zero\")\n    return a % b\n",
     "from calc import modulo\nassert modulo(7, 3) == 1\ntry:\n    modulo(1, 0)\n    raise SystemExit('no error')\nexcept ZeroDivisionError:\n    pass",
     "\n    def test_modulo(self):\n        from calc import modulo\n        self.assertEqual(modulo(7, 3), 1)\n"),
    ("textutil", "title_case", "Add a function title_case(text) to textutil.py that capitalises the first letter of every word and lowercases the rest, with a test. Keep every existing function.",
     "\n\ndef title_case(text):\n    return \" \".join(w[:1].upper() + w[1:].lower() for w in text.split())\n",
     "from textutil import title_case\nassert title_case('hELLO wORLD') == 'Hello World'",
     "\n    def test_title_case(self):\n        from textutil import title_case\n        self.assertEqual(title_case('hELLO wORLD'), 'Hello World')\n"),
    ("cart", "item_count", "Add a method item_count(self) to the Cart class in cart.py that returns the total quantity across all items, with a test. Keep every existing method.",
     "\n    def item_count(self):\n        return sum(v[\"qty\"] for v in self.items.values())\n",
     "from cart import Cart\nc = Cart(); c.add('a', 1.0, 2); c.add('b', 1.0, 3)\nassert c.item_count() == 5",
     "\n    def test_item_count(self):\n        c = Cart()\n        c.add('a', 1.0, 2)\n        self.assertEqual(c.item_count(), 2)\n"),
    ("dates", "is_weekend_index", "Add a function weekday_name(index) to dates.py that maps 0-6 to Monday..Sunday and raises ValueError otherwise, with a test. Keep every existing function.",
     "\n\nWEEKDAYS = [\"Monday\", \"Tuesday\", \"Wednesday\", \"Thursday\", \"Friday\", \"Saturday\", \"Sunday\"]\n\n\ndef weekday_name(index):\n    if not 0 <= index <= 6:\n        raise ValueError(\"index must be 0-6\")\n    return WEEKDAYS[index]\n",
     "from dates import weekday_name\nassert weekday_name(0) == 'Monday' and weekday_name(6) == 'Sunday'",
     "\n    def test_weekday_name(self):\n        from dates import weekday_name\n        self.assertEqual(weekday_name(0), 'Monday')\n"),
]

# things to build from nothing: (name, goal, solution files, hidden assert)
BUILDS = [
    ("calc", "Create calc.py with functions add, subtract, multiply and divide (divide by zero must raise ZeroDivisionError), and unit tests for them in tests/test_calc.py.",
     "import calc\nassert calc.add(2,3)==5 and calc.subtract(5,2)==3 and calc.multiply(3,4)==12 and calc.divide(8,2)==4\ntry:\n    calc.divide(1,0)\n    raise SystemExit('no error')\nexcept ZeroDivisionError:\n    pass"),
    ("stats", "Create stats.py with average(xs) (ValueError on empty), total(xs), median(xs) and clamp(x, lo, hi), plus unit tests in tests/test_stats.py.",
     "from stats import average, median, clamp\nassert average([2,4,6]) == 4 and median([1,2,3,4]) == 2.5 and clamp(5,0,3) == 3"),
    ("textutil", "Create textutil.py with slugify(text) ('Hello, World!' -> 'hello-world'), word_count(text) and is_palindrome(text) (ignores case and punctuation), plus unit tests in tests/test_textutil.py.",
     "from textutil import slugify, word_count, is_palindrome\nassert slugify('Hello, World!') == 'hello-world' and word_count('a b  c') == 3 and is_palindrome('A man, a plan, a canal: Panama')"),
    ("inventory", "Create inventory.py with add_item(items, name, qty=1) (accumulates), remove_item(items, name, qty=1) (deletes at zero), count(items) and names(items) (sorted), plus unit tests in tests/test_inventory.py.",
     "from inventory import add_item, remove_item, count, names\nit = add_item({}, 'a', 2); add_item(it, 'a', 3); add_item(it, 'b')\nassert it['a'] == 5 and count(it) == 6 and names(it) == ['a','b'] and 'b' not in remove_item(it, 'b')"),
    ("dates", "Create dates.py with is_leap(year) (Gregorian rule, 1900 is not a leap year, 2000 is), days_in_month(year, month) and day_of_year(year, month, day), plus unit tests in tests/test_dates.py.",
     "from dates import is_leap, days_in_month, day_of_year\nassert is_leap(2000) and not is_leap(1900) and days_in_month(2024, 2) == 29 and day_of_year(2023, 3, 1) == 60"),
    ("cart", "Create cart.py with a Cart class: add(sku, price, qty=1) accumulates quantity, remove(sku), total() rounded to 2 decimals, apply_discount(pct) returning the discounted total (ValueError outside 0-100), plus unit tests in tests/test_cart.py.",
     "from cart import Cart\nc = Cart(); c.add('a', 2.5, 2); c.add('b', 1.0)\nassert c.total() == 6.0 and c.apply_discount(50) == 3.0"),
]


def with_bug(mod, bug):
    name, search, replace, hidden = bug
    src = MODULES[mod]["src"]
    assert search in src, (mod, name)
    return src.replace(search, replace)


def pick(rng, seq, n):
    """n items of seq in a seed-stable order, cycling when n > len(seq)."""
    out = []
    order = list(seq)
    rng.shuffle(order)
    while len(out) < n:
        out.extend(order)
    return out[:n]


def rng_for(seed, salt):
    return random.Random(f"{seed}:{salt}")


def bug_cases():
    return [(m, b) for m, d in MODULES.items() for b in d["bugs"]]


def test_path(mod):
    return f"tests/test_{mod}.py"
