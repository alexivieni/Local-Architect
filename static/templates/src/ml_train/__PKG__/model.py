"""Logistic regression by batch gradient descent with L2 and early stopping."""
import json
import math

try:                      # optional speed-up; the pure-Python path gives the same numbers
    import numpy as _np
except ImportError:
    _np = None


def _dot(a, b):
    if _np is not None:
        return float(_np.dot(a, b))
    return sum(x * y for x, y in zip(a, b))


def sigmoid(z):
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def predict_proba(w, b, row):
    return sigmoid(_dot(w, row) + b)


def log_loss(w, b, X, y, l2=0.0):
    eps = 1e-12
    total = 0.0
    for row, t in zip(X, y):
        p = min(max(predict_proba(w, b, row), eps), 1 - eps)
        total -= t * math.log(p) + (1 - t) * math.log(1 - p)
    return total / len(X) + l2 * sum(v * v for v in w) / 2


def fit(X, y, lr=0.5, epochs=500, l2=0.01, patience=20, tol=1e-6):
    """-> (weights, bias, history of losses)."""
    n, d = len(X), len(X[0])
    w, b = [0.0] * d, 0.0
    history, best, stale = [], float("inf"), 0
    for _ in range(epochs):
        gw, gb = [0.0] * d, 0.0
        for row, t in zip(X, y):
            err = predict_proba(w, b, row) - t
            for j in range(d):
                gw[j] += err * row[j]
            gb += err
        w = [wj - lr * (g / n + l2 * wj) for wj, g in zip(w, gw)]
        b -= lr * gb / n
        loss = log_loss(w, b, X, y, l2)
        history.append(loss)
        if best - loss > tol:
            best, stale = loss, 0
        else:
            stale += 1
            if stale >= patience:
                break
    return w, b, history


def save(path, features, w, b, scaler):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"features": features, "weights": w, "bias": b, "scaler": scaler}, f, indent=2)


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)
