"""Train the worker from the data nights' samples: the dataset export first, then train_lora.py (a dry run unless --real).

    python tools/train_worker.py              # export, then `train_lora.py --dry-run` (no GPU, prints what would be trained on)
    python tools/train_worker.py --real       # export, then the real QLoRA run (needs the packages and the card; read train_lora.py first)

The hub's "Train the worker" button starts exactly this. Exit code is the first non-zero step's."""
import os
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    real = "--real" in argv
    model = argv[argv.index("--model") + 1] if "--model" in argv and argv.index("--model") + 1 < len(argv) else ""
    print("=== step 1 of 2: exporting the training data", flush=True)
    code = subprocess.call([sys.executable, "-u", os.path.join(APP, "architect.py"), "dataset"], cwd=APP)
    if code:
        print(f"=== the export failed (exit {code}); nothing was trained", flush=True)
        return code
    cmd = [sys.executable, "-u", os.path.join(APP, "train_lora.py")] + ([] if real else ["--dry-run"]) + (["--model", model] if model else [])
    print("=== step 2 of 2: " + ("training the worker (real run)" if real else "dry run: showing what would be trained on"), flush=True)
    code = subprocess.call(cmd, cwd=APP)
    print("=== done" if not code else f"=== train_lora.py stopped with exit {code}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
