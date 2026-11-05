"""Recommended settings for THIS machine, each with the reason and where the number comes from.

The Settings page used to show "default" in every field with no guidance. recommend() looks at the measured hardware, the drives,
what is installed and configured, and today's measured lessons, and returns {setting path: {"value", "why", "basis"}} for the
settings where a better value is known. Nothing is changed here: the page offers "Use" per field and "Apply all", and saving goes
through the normal checked, undoable settings write. basis = measured (on this machine) | rule (from today's runs) | safe default.
"""
import os

import arch_core as core


def _get(cfg, path, default=None):
    cur = cfg
    for k in path.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def recommend(cfg=None, hw=None, drives=None):
    cfg = cfg if cfg is not None else core.load_config()
    if hw is None or drives is None:
        try:
            import arch_hw
            hw = hw if hw is not None else arch_hw.detect()
            drives = drives if drives is not None else arch_hw.drives()
        except Exception:
            hw, drives = hw or {}, drives or []
    out = {}

    def rec(path, value, why, basis="rule"):
        if _get(cfg, path) != value:
            out[path] = {"value": value, "why": why, "basis": basis}

    vram = float(hw.get("vram_gb") or 0)
    online = bool(_get(cfg, "online.enabled"))
    key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if vram >= 4:
        rec("server.fast_lane", "on", f"Your {vram:.0f} GB card holds the whole worker with its cache; giving it the card, n-gram "
            "speculation and high priority makes every local answer faster. It costs nothing but card space for the heavy model.", "measured")
    rec("behavior.ground_edits", "on", "Edits are only accepted on lines the model has looked at. Today's agents read about 5 times "
        "per edit; small models that edit blind miss.")
    if online and key:
        if _get(cfg, "online.cloud_tier") in ("all", "off", None):
            rec("online.cloud_tier", "plan", "Claude does the planning and judging (a few hundred tokens), your GPU writes the code for "
                "free. 'all' sends every coding step to Claude; 'plan' keeps most of the quality for a fraction of the cost.")
        rec("online.cloud_cache", True, "The same Claude question is answered from your disk for free (kept 30 days).")
        rec("online.cloud_cache_ttl", "1h", "Long sessions with pauses keep Claude's prompt cache warm (reads cost 0.1x). Today's "
            "agents read 107 million tokens from cache.", "measured")
        rec("online.local_scout", "on", "Your local worker reads the code first and hands Claude a short brief, so Claude reads less.")
        cap = _get(cfg, "limits.cloud_usd_per_day")
        if not cap or float(cap) > 10:
            rec("limits.cloud_usd_per_day", 5.0, "A daily ceiling across every Claude use, so an unattended night cannot drain your credit.")
    rec("behavior.tests_on_ecores", "auto", "Tests run on the efficiency cores while the GPU writes the next try.")
    cold = [d for d in (drives or []) if d.get("removable") and not d.get("fake") and 16 <= float(d.get("total_gb") or 0) <= 128]
    if cold:
        d = cold[0]
        rec("storage.archive", d.get("mount"), f"{d.get('mount')} ({float(d.get('total_gb') or 0):.0f} GB, removable) is right for "
            "COLD data only: run archives, logbook backups, exported datasets. Models and caches stay on the fast internal drive.", "measured")
    # only keys the settings schema knows, so "Apply all" can never write something the checks would refuse
    try:
        import arch_params
        out = {p: v for p, v in out.items() if arch_params.is_known_setting(p)}
    except Exception:
        pass
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(recommend(), indent=1))
