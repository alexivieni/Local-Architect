"""ml: build a small, pure-Python ML building block from nothing, where the usual mistakes are leakage (the test set shaping the training
side) and irreproducibility (no seed, ties broken by chance). A hidden check the model never saw states the rule as behaviour: the split
shares no row, the scaler does not move when only the test values move, ties resolve the same way twice, no row validates the fold that
trains on it. The flow's own gate then runs the suite twice and compares; the judges add the hidden check, the tests and a tests/ file."""
import _bank as B

FLOW = "ml"
DEFAULT_N = 4

CASES = [
    {"mod": "splitter",
     "goal": "Write splitter.py with train_test_split(rows, test_frac=0.25, seed=0) -> (train, test): a seeded shuffle, no row in both sets, the "
             "input list left unchanged, the same seed giving the same split. Add tests.",
     "src": '''"""Train/test splitting that never leaks a row between the two sets."""
import random


def train_test_split(rows, test_frac=0.25, seed=0):
    """Shuffle a copy of rows with the seed, then cut: -> (train, test). The input list is not changed."""
    if not 0 < test_frac < 1:
        raise ValueError("test_frac must be between 0 and 1")
    order = list(range(len(rows)))
    random.Random(seed).shuffle(order)
    n_test = max(1, round(len(rows) * test_frac))
    test = [rows[i] for i in order[:n_test]]
    train = [rows[i] for i in order[n_test:]]
    return train, test
''',
     "tests": '''import unittest
from splitter import train_test_split


class T(unittest.TestCase):
    def test_sizes_and_disjoint(self):
        train, test = train_test_split(list(range(20)), 0.25, 1)
        self.assertEqual((len(train), len(test)), (15, 5))
        self.assertFalse(set(train) & set(test))

    def test_same_seed_same_split(self):
        rows = list(range(30))
        self.assertEqual(train_test_split(rows, 0.2, 5), train_test_split(rows, 0.2, 5))

    def test_input_unchanged(self):
        rows = list(range(10))
        train_test_split(rows, 0.3, 2)
        self.assertEqual(rows, list(range(10)))

    def test_bad_fraction(self):
        with self.assertRaises(ValueError):
            train_test_split([1, 2, 3], 1.5)


if __name__ == "__main__":
    unittest.main()
''',
     "hidden": '''from splitter import train_test_split
rows = list(range(100))
a = train_test_split(rows, 0.2, 7)
assert a == train_test_split(rows, 0.2, 7), 'the same seed must give the same split'
assert a != train_test_split(rows, 0.2, 8), 'another seed must give another split'
tr, te = a
assert len(te) == 20 and len(tr) == 80, (len(tr), len(te))
assert not set(tr) & set(te), 'a row is in both train and test (leakage)'
assert sorted(tr + te) == rows, 'rows were lost or invented'
assert rows == list(range(100)), 'the input list was changed'
''',
     "wrong": '''"""Train/test splitting."""
import random


def train_test_split(rows, test_frac=0.25, seed=0):
    """Pick test rows with the seed; the training set is all rows."""
    rng = random.Random(seed)
    n_test = max(1, round(len(rows) * test_frac))
    test = rng.sample(rows, n_test)
    return list(rows), test
'''},
    {"mod": "scaler",
     "goal": "Write scaler.py with fit_scaler(train) -> {'mean': m, 'std': s} (the population standard deviation; 1.0 when all values are equal), transform(values, scaler) -> list, "
             "and prepare(train, test) -> (train_scaled, test_scaled, scaler) that fits the scaler on the TRAINING values only. Add tests.",
     "src": '''"""Feature scaling fitted on the training values only (fitting on all values leaks the test set into training)."""
import statistics


def fit_scaler(train):
    """{"mean": m, "std": s} from the training values only; std is 1.0 when all values are equal."""
    return {"mean": statistics.fmean(train), "std": statistics.pstdev(train) or 1.0}


def transform(values, scaler):
    """Each value as (v - mean) / std."""
    return [(v - scaler["mean"]) / scaler["std"] for v in values]


def prepare(train, test):
    """-> (train_scaled, test_scaled, scaler), the scaler fitted on train only."""
    scaler = fit_scaler(train)
    return transform(train, scaler), transform(test, scaler), scaler
''',
     "tests": '''import unittest
from scaler import fit_scaler, transform, prepare


class T(unittest.TestCase):
    def test_fit(self):
        s = fit_scaler([1.0, 2.0, 3.0])
        self.assertAlmostEqual(s["mean"], 2.0)
        self.assertAlmostEqual(s["std"], (2 / 3) ** 0.5)

    def test_constant_values_do_not_divide_by_zero(self):
        self.assertEqual(fit_scaler([4.0, 4.0])["std"], 1.0)

    def test_transform(self):
        self.assertEqual(transform([2.0], {"mean": 1.0, "std": 0.5}), [2.0])

    def test_test_values_do_not_move_the_scaler(self):
        a = prepare([1.0, 2.0, 3.0], [10.0])
        b = prepare([1.0, 2.0, 3.0], [900.0])
        self.assertEqual(a[2], b[2])
        self.assertEqual(a[0], b[0])


if __name__ == "__main__":
    unittest.main()
''',
     "hidden": '''from scaler import fit_scaler, transform, prepare
s = fit_scaler([1.0, 2.0, 3.0])
assert abs(s["mean"] - 2.0) < 1e-9 and abs(s["std"] - (2 / 3) ** 0.5) < 1e-9
assert fit_scaler([5.0, 5.0, 5.0])["std"] == 1.0
train = [float(i) for i in range(20)]
a_tr, a_te, a_sc = prepare(train, [3.0, 4.0, 5.0])
b_tr, b_te, b_sc = prepare(train, [3000.0, -4000.0, 5000.0])
assert a_sc == b_sc and a_tr == b_tr, 'the test values changed what the scaler learned (leakage)'
assert abs(sum(a_tr) / len(a_tr)) < 1e-9, 'the scaled training values must have mean 0'
assert a_te == transform([3.0, 4.0, 5.0], a_sc)
''',
     "wrong": '''"""Feature scaling."""
import statistics


def fit_scaler(train):
    """Mean and std of the values."""
    return {"mean": statistics.fmean(train), "std": statistics.pstdev(train) or 1.0}


def transform(values, scaler):
    """Each value as (v - mean) / std."""
    return [(v - scaler["mean"]) / scaler["std"] for v in values]


def prepare(train, test):
    """Scale both parts; the scaler is fitted on everything so the scales match."""
    scaler = fit_scaler(list(train) + list(test))
    return transform(train, scaler), transform(test, scaler), scaler
'''},
    {"mod": "centroid",
     "goal": "Write centroid.py with fit(X, y) -> {label: centroid} (the per-feature mean of each label's rows) and predict(model, X) -> labels of the "
             "nearest centroid by squared distance, a tie going to the smallest label. Never change X or y. Add tests.",
     "src": '''"""A nearest-centroid classifier in pure Python; the same inputs always give the same labels."""


def fit(X, y):
    """{label: centroid}, the centroid being the per-feature mean of that label's rows."""
    groups = {}
    for row, label in zip(X, y):
        groups.setdefault(label, []).append(row)
    return {label: [sum(col) / len(rows) for col in zip(*rows)] for label, rows in groups.items()}


def predict(model, X):
    """The label of the nearest centroid (squared distance) for each row; a tie goes to the smallest label."""
    return [min(sorted(model), key=lambda lab: sum((a - b) ** 2 for a, b in zip(row, model[lab]))) for row in X]
''',
     "tests": '''import unittest
from centroid import fit, predict


class T(unittest.TestCase):
    def test_fit_means(self):
        self.assertEqual(fit([[0, 0], [2, 4]], ["a", "a"]), {"a": [1.0, 2.0]})

    def test_predict_nearest(self):
        m = fit([[0, 0], [0, 1], [10, 10], [10, 11]], ["a", "a", "b", "b"])
        self.assertEqual(predict(m, [[1, 1], [9, 9]]), ["a", "b"])

    def test_tie_goes_to_the_smallest_label(self):
        m = fit([[0], [2]], ["z", "a"])
        self.assertEqual(predict(m, [[1]]), ["a"])

    def test_inputs_unchanged(self):
        X, y = [[0, 0], [1, 1]], ["a", "b"]
        fit(X, y)
        self.assertEqual((X, y), ([[0, 0], [1, 1]], ["a", "b"]))


if __name__ == "__main__":
    unittest.main()
''',
     "hidden": '''from centroid import fit, predict
X = [[0, 0], [0, 1], [10, 10], [10, 11]]
y = ["a", "a", "b", "b"]
X0, y0 = [r[:] for r in X], y[:]
m = fit(X, y)
assert (X, y) == (X0, y0), 'fit changed its inputs'
assert predict(m, [[1, 1], [9, 9], [0, 0]]) == ["a", "b", "a"]
assert predict(fit([[0], [2]], ["z", "a"]), [[1]]) == ["a"], 'a tie must go to the smallest label'
assert predict(fit([[0], [2]], ["a", "z"]), [[1]]) == ["a"], 'a tie must not depend on the order of the rows'
assert predict(m, [[5, 5]]) == predict(m, [[5, 5]])
''',
     "wrong": '''"""A nearest-centroid classifier."""


def fit(X, y):
    """{label: centroid}."""
    groups = {}
    for row, label in zip(X, y):
        groups.setdefault(label, []).append(row)
    return {label: [sum(col) / len(rows) for col in zip(*rows)] for label, rows in groups.items()}


def predict(model, X):
    """The nearest centroid's label; on a tie the label seen last wins."""
    out = []
    for row in X:
        best, best_d = None, None
        for lab in model:
            d = sum((a - b) ** 2 for a, b in zip(row, model[lab]))
            if best_d is None or d <= best_d:
                best, best_d = lab, d
        out.append(best)
    return out
'''},
    {"mod": "kfold",
     "goal": "Write kfold.py with kfold_indices(n, k=5, seed=0) -> [(train_idx, val_idx)] for k folds over range(n): shuffled with the seed, every index "
             "validated exactly once, never in the train part of its own fold, sizes differing by at most one, ValueError unless 2 <= k <= n. Add tests.",
     "src": '''"""K-fold index splits: every row is validated exactly once and never trains the fold that validates it."""
import random


def kfold_indices(n, k=5, seed=0):
    """[(train_idx, val_idx)] for k folds over range(n), shuffled with the seed; each index list is sorted."""
    if not 2 <= k <= n:
        raise ValueError("need 2 <= k <= n")
    order = list(range(n))
    random.Random(seed).shuffle(order)
    folds = [order[i::k] for i in range(k)]
    out = []
    for i, val in enumerate(folds):
        train = [j for f, idxs in enumerate(folds) if f != i for j in idxs]
        out.append((sorted(train), sorted(val)))
    return out
''',
     "tests": '''import unittest
from kfold import kfold_indices


class T(unittest.TestCase):
    def test_every_index_validated_once(self):
        folds = kfold_indices(23, 5, 1)
        self.assertEqual(sorted(i for _, val in folds for i in val), list(range(23)))

    def test_no_leakage_inside_a_fold(self):
        for train, val in kfold_indices(23, 5, 1):
            self.assertFalse(set(train) & set(val))
            self.assertEqual(sorted(train + val), list(range(23)))

    def test_same_seed_same_folds(self):
        self.assertEqual(kfold_indices(20, 4, 9), kfold_indices(20, 4, 9))

    def test_bad_k(self):
        with self.assertRaises(ValueError):
            kfold_indices(5, 1)


if __name__ == "__main__":
    unittest.main()
''',
     "hidden": '''from kfold import kfold_indices
folds = kfold_indices(23, 5, 3)
assert len(folds) == 5
sizes = [len(v) for _, v in folds]
assert max(sizes) - min(sizes) <= 1, sizes
assert sorted(i for _, v in folds for i in v) == list(range(23)), 'each index must be validated exactly once'
for train, val in folds:
    assert not set(train) & set(val), 'an index is in the train and validation part of one fold (leakage)'
    assert sorted(train + val) == list(range(23))
assert folds == kfold_indices(23, 5, 3), 'the same seed must give the same folds'
assert folds != kfold_indices(23, 5, 4), 'another seed must give other folds'
for bad in ((10, 1), (3, 4)):
    try:
        kfold_indices(*bad)
        raise SystemExit('no error for %r' % (bad,))
    except ValueError:
        pass
''',
     "wrong": '''"""K-fold index splits."""
import random


def kfold_indices(n, k=5, seed=0):
    """[(train_idx, val_idx)] for k folds over range(n)."""
    if not 2 <= k <= n:
        raise ValueError("need 2 <= k <= n")
    order = list(range(n))
    random.Random(seed).shuffle(order)
    return [(sorted(order), sorted(order[i::k])) for i in range(k)]
'''},
]


def generate(n, seed=0):
    rng = B.rng_for(seed, "ml")
    out = []
    for i, c in enumerate(B.pick(rng, CASES, n)):
        mod = c["mod"]
        tpath = B.test_path(mod)
        out.append({"id": f"ml-{mod}-{i:02d}", "tags": ["ml", "from-nothing", "leakage"], "goal": c["goal"],
                    "fixture": {"files": {}},
                    "budget": {"calls": 40, "tokens": 120000, "seconds": 1500},
                    "judge": [{"type": "python_assert", "code": c["hidden"]}, {"type": "file_exists", "paths": ["tests/test_*.py"]}, {"type": "tests_pass"}],
                    "solution": {"files": {f"{mod}.py": c["src"], tpath: c["tests"]}},
                    "wrong": {"files": {f"{mod}.py": c["wrong"], tpath: c["tests"]}}})
    return out
