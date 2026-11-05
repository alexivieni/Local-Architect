import argparse
import json
import sys

from . import data, metrics, model


def train(args):
    features, X, y = data.load_csv(args.csv, args.target)
    Xtr, ytr, Xte, yte = data.split(X, y, args.test_frac, args.seed)
    scaler = data.fit_scaler(Xtr)
    w, b, hist = model.fit(data.scale(Xtr, scaler), ytr, lr=args.lr, l2=args.l2, epochs=args.epochs)
    preds = [int(model.predict_proba(w, b, r) >= 0.5) for r in data.scale(Xte, scaler)]
    rep = metrics.report(yte, preds) | {"epochs": len(hist), "final_loss": hist[-1], "test_rows": len(yte)}
    model.save(args.out, features, w, b, scaler)
    print(json.dumps(rep, indent=2))
    return 0


def predict(args):
    m = model.load(args.model)
    if len(args.values) != len(m["features"]):
        print(f"expected {len(m['features'])} values: {', '.join(m['features'])}", file=sys.stderr)
        return 2
    row = data.scale([args.values], m["scaler"])[0]
    p = model.predict_proba(m["weights"], m["bias"], row)
    print(json.dumps({"probability": p, "label": int(p >= 0.5)}))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="__PKG__")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train")
    t.add_argument("csv")
    t.add_argument("--target", default="label")
    t.add_argument("--out", default="model.json")
    t.add_argument("--lr", type=float, default=0.5)
    t.add_argument("--l2", type=float, default=0.01)
    t.add_argument("--epochs", type=int, default=500)
    t.add_argument("--test-frac", type=float, default=0.25)
    t.add_argument("--seed", type=int, default=0)
    p = sub.add_parser("predict")
    p.add_argument("model")
    p.add_argument("values", type=float, nargs="+")
    args = ap.parse_args(argv)
    try:
        return train(args) if args.cmd == "train" else predict(args)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
