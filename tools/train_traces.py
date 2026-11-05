"""Run every training task (distributed systems + harness building) on the LOCAL models, one at a time, to produce verified traces.

    python tools/train_traces.py            # all 105 tasks, ~5 h on an RTX 4050 with the 8B worker
    python tools/train_traces.py --n 10     # a quick taste

Free (local models only: Claude is never used during eval). Every task that passes its hidden tests becomes training data through
`python architect.py dataset`. Stop any time with Ctrl+C or by creating hub_data/STOP.
"""
import os
import subprocess
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
n = sys.argv[sys.argv.index("--n") + 1] if "--n" in sys.argv else "200"
for tag, label in (("distributed", "traces_distributed"), ("harness", "traces_harness")):
    if os.path.exists(os.path.join(APP, "hub_data", "STOP")):
        break
    print(f"=== {tag}", flush=True)
    subprocess.call([sys.executable, os.path.join(APP, "architect.py"), "eval", "--tags", tag, "--n", n, "--label", label], cwd=APP)
print("=== exporting the training data", flush=True)
subprocess.call([sys.executable, os.path.join(APP, "architect.py"), "dataset"], cwd=APP)
