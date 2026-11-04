"""Assessment: one readiness report that combines three things that are each already measured elsewhere
(real hardware, the real role/model scoreboard, and this specific machine's VRAM/RAM) into a single
"can this rig actually do this workload" read-out -- not a new quality benchmark, just honesty about fit.

    python architect.py assess                 -- this machine + whatever Ollama has installed
    GET /api/arch/assess                        -- same, over the hub's API

Two things come out of it:
  1. Per-workload coverage: for a named bundle of roles (coding / security / data), which roles have an
     installed model that scores >= a usable bar, and which don't -- read straight off roles.recommend(),
     never guessed.
  2. A hardware-tuned settings suggestion: NOT a model-quality tweak (that's what the Scoreboard measures) --
     just keeping THIS machine's VRAM/RAM from thrashing, by picking context size, keep-alive and sampling
     width for the VRAM tier `model_hub.detect_hardware()` actually reports. Returned pre-validated against
     arch_params' schema, so the caller can hand it straight to the same settings-save path a manual edit
     uses.
"""
WORKLOADS = {
    "coding": {"label": "Coding", "roles": ["generator", "generator_ui", "generator_sql", "fullstack", "tester", "optimizer", "bug_hunter", "reviewer"],
               "blurb": "Build, bugfix, refactor, test and optimize flows."},
    "security": {"label": "Security / red-blue", "roles": ["security_engineer", "red_team", "blue_team", "pentester", "ioc_analyst", "network_engineer", "config_engineer"],
                 "blurb": "secure, network, config and pentest flows -- all scope-checked, no live exploitation."},
    "devops": {"label": "CI/CD and infrastructure", "roles": ["ci_engineer", "config_engineer", "network_engineer"],
               "blurb": "cicd, config and network flows: pipelines that run what the repository has, hardened config, checked network plans."},
    "data": {"label": "Data / ML", "roles": ["generator_sql", "ml_engineer"], "blurb": "SQL schema/migration work and ML pipeline review."},
}

USABLE_SCORE = 45  # same bar roles.recommend()'s own "gaps" reporting in the hub uses


def vram_tier(vram_gb):
    vram_gb = vram_gb or 0
    if vram_gb >= 20:
        return "large"
    if vram_gb >= 10:
        return "mid"
    if vram_gb >= 4:
        return "small"
    return "cpu_only"


TUNED_PROFILES = {
    # tier -> a settings patch that keeps THIS machine from swapping/thrashing. Not a creativity/quality knob.
    "large": {"limits.num_ctx": 32768, "limits.context_chars": 60000, "limits.keep_alive": "30m", "behavior.best_of_n": 2},
    "mid": {"limits.num_ctx": 16384, "limits.context_chars": 40000, "limits.keep_alive": "15m", "behavior.best_of_n": 1},
    "small": {"limits.num_ctx": 8192, "limits.context_chars": 24000, "limits.keep_alive": "5m", "behavior.best_of_n": 1},
    "cpu_only": {"limits.num_ctx": 4096, "limits.context_chars": 12000, "limits.keep_alive": "2m", "behavior.best_of_n": 1},
}
import os  # noqa: E402  (used by assess() to find the tune profile)

TUNED_LABEL = {"large": ">=20 GB VRAM", "mid": "10-20 GB VRAM", "small": "4-10 GB VRAM", "cpu_only": "<4 GB VRAM / CPU-only"}


def tuned_settings(hw):
    """-> (tier, validated settings patch) for the hardware dict model_hub.detect_hardware() returns."""
    import arch_params as params
    tier = vram_tier(hw.get("vram_gb"))
    return tier, params.validate_settings_patch(TUNED_PROFILES[tier])


def workload_coverage(ranked):
    """ranked: roles.recommend(models)[0], i.e. {role_id: [scored candidates...]}. -> list of per-workload
    coverage dicts, each role classified by its OWN top scored/guessed candidate -- no new scoring here."""
    out = []
    for wid, w in WORKLOADS.items():
        covered, missing, guessed = [], [], []
        for rid in w["roles"]:
            cands = ranked.get(rid) or []
            top = cands[0] if cands else None
            if not top or top["score"] <= 0:
                missing.append(rid)
            elif top.get("basis") == "guess":
                guessed.append(rid)
            elif top["score"] >= USABLE_SCORE:
                covered.append(rid)
            else:
                missing.append(rid)
        out.append({"id": wid, "label": w["label"], "blurb": w["blurb"], "roles": w["roles"],
                    "covered": covered, "guessed": guessed, "missing": missing, "ready": not missing and not guessed})
    return out


def tuning_drift(cfg, tuned):
    """Self-tuning check: does the CURRENT saved config already match what this machine's hardware tier
    recommends? -> [{"path", "current", "recommended"}, ...] for every setting that differs. Empty means
    the config is already tuned for this machine -- nothing to do, not a guess that it's fine."""
    drift = []
    for path, want in tuned.items():
        d = cfg
        for part in path.split("."):
            d = d.get(part) if isinstance(d, dict) else None
            if d is None:
                break
        if d != want:
            drift.append({"path": path, "current": d, "recommended": want})
    return drift


def assess(models, hw=None, cfg=None):
    """models: hub model rows (as returned by model_hub's state -- the real installed/scored models, not a
    guess). hw: a model_hub.detect_hardware() dict; detected fresh if not given. cfg: the current saved
    config, for self-tuning drift; if given, the report also says whether this machine's settings have
    already drifted from what its hardware tier recommends."""
    import arch_roles as roles
    import model_hub as mh
    hw = hw if hw is not None else mh.detect_hardware()
    ranked, plan = roles.recommend(models or [])
    tier, tuned = tuned_settings(hw)
    # a MEASURED tune on this machine beats the hardware-only estimate: one source of truth for "what is right for this machine"
    import arch_core as core
    prof = core.load_json(os.path.join(core.data_dir(), "profile.json"))
    tuned_from = "estimate"
    if prof.get("applied") and prof.get("fingerprint") == hw.get("fingerprint", prof.get("fingerprint")) and prof.get("settings"):
        measured = {k: v for k, v in prof["settings"].items() if k in tuned}
        if measured:
            tuned, tuned_from = dict(tuned, **measured), "tune"
            tuned.pop("limits.context_chars", None)        # with a measured window the prompt budget is derived from it (arch_tune), not set by hand
    drift = tuning_drift(cfg, tuned) if cfg is not None else None
    return {
        "hw": hw, "vram_tier": tier, "vram_tier_label": TUNED_LABEL[tier], "tuned_from": tuned_from,
        "tuned_settings": tuned, "tuning_drift": drift, "distinct_models_in_use": plan["distinct_models"],
        "workloads": workload_coverage(ranked),
    }


def main(argv=None):
    import argparse
    import json
    import arch_core as core
    import model_hub as mh
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args(argv)
    st = mh.build_state()
    out = assess(st.get("models", []), st.get("hw"), core.load_config())
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
