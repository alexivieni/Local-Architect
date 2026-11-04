"""Compatibility shim. The real exporter is `arch_dataset`.

There were two exporters reading the same traces and disagreeing: this one screened for credentials but was not registered on the command
line, while the one people actually used had no screen at all. They also wrote to different folders, and `train_lora.py` defaulted to a
path neither of them produced. One exporter now does the work -- `arch_dataset`, which has the credential screen, correct preference pairs,
per-tier output, distillation samples from escalations and tool trajectories.

This file stays only so that `arch_training.export(roots, write=False)` keeps working for callers outside the harness (the LocalIntel
overlay uses it to show counts on its page). Nothing new should import it: use `arch_dataset.build` / `arch_dataset.write`, or run
`python architect.py dataset`.
"""
import os

import arch_core as core
import arch_dataset as _ds

SECRET = _ds.SECRET
has_secret = _ds.has_secret


def default_roots():
    return [core.HOME]


def roots(rs):
    return [r for r in dict.fromkeys(os.path.abspath(r) for r in rs) if os.path.isdir(r)]


def collect(rs):
    """-> (positive, negative, stats) in the old shape, computed by arch_dataset."""
    data = _ds.build(roots(rs))
    pos = [dict(r, meta=dict(r["meta"])) for rows in data["sft"].values() for r in rows]
    neg = [{"messages": ([{"role": "system", "content": p["system"]}] if p.get("system") else []) +
                        [{"role": "user", "content": p["prompt"]}, {"role": "assistant", "content": p["rejected"]}],
            "meta": dict(p["meta"], rejected=True)}
           for rows in data["dpo"].values() for p in rows]
    return pos, neg, data["stats"]


def export(rs, write=True, out_dir=None):
    """-> {"positive", "negative", "per_role", "stats", "files"}. With write=True the files go to hub_data/datasets (one folder, not two)."""
    data = _ds.build(roots(rs))
    out = {"positive": sum(len(v) for v in data["sft"].values()),
           "negative": sum(len(v) for v in data["dpo"].values()),
           "per_role": {r: {"sft": len(data["sft"].get(r, [])), "dpo": len(data["dpo"].get(r, []))}
                        for r in set(data["sft"]) | set(data["dpo"])},
           "stats": data["stats"], "files": {}}
    if write:
        out["files"] = _ds.write(data, out_dir or os.path.join(core.data_dir(), "datasets"))["files"]
    return out


def main(argv=None):
    print("arch_training is now a shim. Use:  python architect.py dataset")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
