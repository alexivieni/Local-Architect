# __NAME__

A small, honest training script: logistic regression on a numeric CSV, written in plain Python so every step is
readable. If numpy is installed the dot products use it; results are the same either way.

    python -m __PKG__ train data/sample.csv --target label --out model.json
    python -m __PKG__ predict model.json 1.2 0.4
    python -m pytest -q

## What it does

1. `data.py` reads the CSV, splits train/test with a fixed seed, standardises features (stats from TRAIN only).
2. `model.py` fits weights by gradient descent with L2, stops early when the loss stops improving.
3. `metrics.py` reports accuracy, precision, recall, F1 and the confusion matrix on the held-out rows.
4. The model (weights + scaling) is saved as JSON so `predict` needs nothing else.

## Next steps

Swap in your CSV, add features, try `--lr` / `--l2`, and keep the test-set numbers honest (never tune on them).
