"""Load, split and scale. Scaling statistics come from the training rows only (no test leakage)."""
import csv
import math
import random


def load_csv(path, target):
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if target not in (reader.fieldnames or []):
            raise ValueError(f"no column {target!r}; columns: {reader.fieldnames}")
        features = [c for c in reader.fieldnames if c != target]
        X, y = [], []
        for n, row in enumerate(reader, start=2):
            try:
                X.append([float(row[c]) for c in features])
                y.append(int(float(row[target])))
            except (TypeError, ValueError):
                raise ValueError(f"line {n}: every value must be a number") from None
    if set(y) - {0, 1}:
        raise ValueError("the target must be 0 or 1")
    return features, X, y


def split(X, y, test_frac=0.25, seed=0):
    idx = list(range(len(X)))
    random.Random(seed).shuffle(idx)
    cut = max(1, int(len(idx) * test_frac))
    test, train = idx[:cut], idx[cut:]
    return [X[i] for i in train], [y[i] for i in train], [X[i] for i in test], [y[i] for i in test]


def fit_scaler(X):
    cols = list(zip(*X))
    means = [sum(c) / len(c) for c in cols]
    stds = [math.sqrt(sum((v - m) ** 2 for v in c) / len(c)) or 1.0 for c, m in zip(cols, means)]
    return {"mean": means, "std": stds}


def scale(X, scaler):
    return [[(v - m) / s for v, m, s in zip(row, scaler["mean"], scaler["std"])] for row in X]
