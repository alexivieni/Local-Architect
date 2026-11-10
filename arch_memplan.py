"""The memory planner: which worker, how many parallel slots, which window -- solved together from Eq. 5, 7, 11 and 21.

For each installed worker candidate (passports with an architecture), slot count N in (1, 2, 4) and window n in (4096, 8192):
  layers on the card      Eq. 5/11 (arch_server._ngl, with this machine's graphics memory and the desktop's share)
  decode bound (tok/s)    Eq. 7 with the measured bandwidths: 1 / (g M / B_gpu + (1 - g) M / B_ram), per forward pass
  N candidates cost       Eq. 21: about (M + N kappa n) / (M + kappa n) of one call, when every layer is on the card
  measured pass rate      this model's first-try pass rate where the cost model has one
Read-only; it plans, it changes nothing. `python arch_memplan.py`
"""
import glob
import json
import os

GB = 1024 ** 3


def _passports():
    import arch_core
    out = []
    for p in sorted(glob.glob(os.path.join(arch_core.data_dir(), "passports", "*.json"))):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        a = d.get("arch") or {}
        if a.get("layers") and a.get("kv_heads") and a.get("head_dim") and d.get("size_gb") and not d.get("moe"):
            out.append(d)
    return out


def plan(hw=None, kv="q8_0", slots=(1, 2, 4), windows=(4096, 8192), models=None):
    import arch_server
    import arch_modelmath as mm
    if hw is None:
        import arch_hw
        hw = arch_hw.detect()
    import arch_hw
    B = arch_hw.bandwidth()                  # measured here, else a labelled estimate (it fell back to the reference laptop's 158 / 19.5)
    bg, br = B["gpu_bw"], B["ram_bw"]
    rows = []
    for d in _passports():
        if models and d.get("name") not in models:
            continue
        a, size = d["arch"], float(d["size_gb"])
        kvt = mm.kv_per_token(a, kv) or 0
        for n in windows:
            for N in slots:
                try:
                    ngl, est, full = arch_server._ngl(size * GB, a, hw, n, kv, slots=N)
                except Exception:
                    continue
                g = min(1.0, ngl / max(1, int(a["layers"])))
                t_tok = g * size / bg + (1 - g) * size / br
                kv_gb = kvt * n / GB
                batch = (size + N * kv_gb) / (size + kv_gb) if full else None
                rows.append({"model": d["name"], "window": n, "slots": N, "layers": f"{ngl}/{a['layers']}", "full": bool(full),
                             "gpu_gb": round(est, 2), "tok_s_bound": round(1 / t_tok, 1) if t_tok else None,
                             "n_candidates_cost": round(batch, 2) if batch else None, "bandwidth": B["label"]})
    return rows


def report(rows):
    if not rows:
        return "No worker candidates with an architecture in the passports (python architect.py passport)."
    lines = [f"{'model':<34} {'window':>6} {'slots':>5} {'layers':>7} {'GPU GB':>6} {'tok/s':>6}  N candidates cost"]
    for r in rows:
        lines.append(f"{r['model'][:34]:<34} {r['window']:>6} {r['slots']:>5} {r['layers']:>7} {r['gpu_gb']:>6} {str(r['tok_s_bound']):>6}  "
                     + (f"{r['n_candidates_cost']}x one call" if r["n_candidates_cost"] else "-- does not fit whole: no cheap batch"))
    lines.append(f"tok/s is the per-pass bound from the memory bandwidth (Eq. 7; bandwidth: {rows[0].get('bandwidth') or 'estimate'}); "
                 "speculation can beat it. A batch of N candidates is cheap only "
                 "when every layer and all N windows fit on the card (Eq. 11, 21).")
    return "\n".join(lines)


if __name__ == "__main__":
    print(report(plan(models=None)))
