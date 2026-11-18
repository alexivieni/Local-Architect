"""Fixtures for the scan-backed fix families (harden, types, document, a11y, migrate): small, real, standard-library modules whose
CLEAN form is fully typed and documented, so each family's task is the clean form with one kind of flaw taken out -- and the clean
form is the reference solution. Tests are the bank's honest unittest files (_bank.py), reused unchanged: a flaw is never a behaviour.

Imported by plain name from evals/families (the loader puts that folder on sys.path), like _bank.py."""
import ast

import _bank as B

# ---------------------------------------------------------------------------------------------- clean modules (typed + documented)
CLEAN = {
    "stats": '''"""Small statistics helpers."""


def average(xs: list[float]) -> float:
    """Mean of a non-empty list of numbers."""
    if not xs:
        raise ValueError("average of empty list")
    return sum(xs) / len(xs)


def total(xs: list[float]) -> float:
    """Sum of a list of numbers."""
    return sum(xs)


def median(xs: list[float]) -> float:
    """Middle value; mean of the two middles for an even count."""
    if not xs:
        raise ValueError("median of empty list")
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def clamp(x: float, lo: float, hi: float) -> float:
    """x limited to the closed range [lo, hi]."""
    return max(lo, min(hi, x))
''',
    "inventory": '''"""A tiny in-memory inventory keyed by item name."""


def add_item(items: dict[str, int], name: str, qty: int = 1) -> dict[str, int]:
    """Add qty of name; quantities accumulate."""
    if qty <= 0:
        raise ValueError("qty must be positive")
    items[name] = items.get(name, 0) + qty
    return items


def remove_item(items: dict[str, int], name: str, qty: int = 1) -> dict[str, int]:
    """Remove qty of name; the item disappears at zero."""
    if name not in items:
        raise KeyError(name)
    left = items[name] - qty
    if left > 0:
        items[name] = left
    else:
        del items[name]
    return items


def count(items: dict[str, int]) -> int:
    """Total quantity across all items."""
    return sum(items.values())


def names(items: dict[str, int]) -> list[str]:
    """The item names, sorted."""
    return sorted(items)


def has(items: dict[str, int], name: str) -> bool:
    """True when name is in the inventory."""
    return name in items
''',
    "textutil": '''"""Text helpers."""
import re


def slugify(text: str) -> str:
    """'Hello, World!' -> 'hello-world'."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def word_count(text: str) -> int:
    """Number of whitespace-separated words."""
    return len(text.split())


def truncate(text: str, n: int) -> str:
    """At most n characters, with an ellipsis when cut."""
    if len(text) <= n:
        return text
    return text[: max(0, n - 1)] + "\\u2026"


def is_palindrome(text: str) -> bool:
    """True when text reads the same backwards, ignoring case and punctuation."""
    s = re.sub(r"[^a-z0-9]", "", text.lower())
    return s == s[::-1]
''',
}
FUNCS = {m: B.MODULES[m]["funcs"] for m in CLEAN}


def tests_of(mod):
    return B.MODULES[mod]["tests"]


def build_hidden(mod, hidden):
    """The `build` family's hidden check, made able to see the cart's planted bugs. Its own check for `cart` never tests the accumulation its goal
    asks for and uses a 50% discount (the same either way round), so both planted cart bugs pass it. Left as it is in `build` on purpose (its
    baselines must not move); the families that reuse BUILDS call this."""
    if mod != "cart":
        return hidden
    return hidden + "\nc2 = Cart(); c2.add('a', 1.0, 2); c2.add('a', 1.0, 3)\nassert c2.total() == 5.0 and c2.apply_discount(25) == 3.75, 'accumulation or discount is wrong'"


# ---------------------------------------------------------------------------------------------- taking a flaw out of the clean form
def strip_annotations(src, params=True, returns=True):
    """The same code with parameter and/or return annotations removed (ast round trip: the behaviour cannot change)."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if returns:
                node.returns = None
            if params:
                a = node.args
                for arg in a.posonlyargs + a.args + a.kwonlyargs + [x for x in (a.vararg, a.kwarg) if x]:
                    arg.annotation = None
    return ast.unparse(tree) + "\n"


def strip_docstrings(src, module=True, functions=True):
    """The same code without its module and/or function docstrings."""
    tree = ast.parse(src)

    def drop(node):
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
    if module:
        drop(tree)
    if functions:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                drop(node)
    return ast.unparse(tree) + "\n"


def typed_check(mod):
    """A runtime check, independent of the AST scanner: every public function of `mod` has every parameter and its return annotated."""
    return (f"import inspect, {mod} as m\nbad = []\nfor n in {FUNCS[mod]!r}:\n    s = inspect.signature(getattr(m, n))\n"
            "    bad += [n + '.' + p.name for p in s.parameters.values() if p.annotation is inspect.Parameter.empty]\n"
            "    if s.return_annotation is inspect.Signature.empty:\n        bad.append(n + ' -> ?')\n"
            "assert not bad, 'not annotated: ' + ', '.join(bad)\n")


def documented_check(mod):
    """A runtime check, independent of the AST scanner: the module and every public function of `mod` has a docstring."""
    return (f"import inspect, {mod} as m\nbad = [] if inspect.getdoc(m) else ['module']\nfor n in {FUNCS[mod]!r}:\n"
            "    if not inspect.getdoc(getattr(m, n)):\n        bad.append(n)\nassert not bad, 'no docstring: ' + ', '.join(bad)\n")
