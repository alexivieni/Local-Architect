"""Harness profiles: how much structure the harness puts around a model, in proportion to what that model can do.

A small model needs small steps, whole-file edits, a short leash and fewer roles; a large one can take bigger steps,
a judge after each sprint and sampled retries. The profile is chosen from the BUILDER model (the one writing code, since
step size and edit style are set by whoever writes), by size first, then corrected by what was actually measured:
a model that scores badly on tool calls or following the format is demoted one tier whatever its parameter count says.

The numbers below are starting points drawn from the guardrail practice local-model users report (small steps,
schema in the prompt, targeted retries, step caps, compaction). They are settings, not claims: `tune` applies them,
the Scoreboard and the self-test decide whether they help on a given machine, and every value stays editable in Settings.
"""
import math
import re

TIERS = ["tiny", "small", "mid", "large"]

INFO = {
    "tiny": {"label": "Tiny (about 4B or less)", "crew": "solo",
             "blurb": "One model plays every role. One file per step, whole-file edits, short fix loop, code checks instead of a judge."},
    "small": {"label": "Small (about 5-10B)", "crew": "duo",
              "blurb": "One builder plus one stronger planner if you have it. Up to two files per step; whole-file edits for small files."},
    "mid": {"label": "Mid (about 11-26B)", "crew": "crew",
            "blurb": "A proper team. Up to four files per step, search/replace edits, a final judge pass."},
    "large": {"label": "Large (27B+ or strong MoE)", "crew": "crew",
              "blurb": "Full team. Bigger steps, a judge after every sprint, two sampled attempts per step."},
}

# settings per tier: every key must exist in arch_params.SETTINGS (a test checks this)
SETTINGS = {
    "tiny":  {"limits.max_files_per_step": 1, "limits.max_revisions": 2, "limits.max_sprints": 3, "limits.max_calls": 40,
              "behavior.evaluator": "off", "behavior.best_of_n": 1, "behavior.edit_style": "whole"},
    "small": {"limits.max_files_per_step": 2, "limits.max_revisions": 3, "limits.max_sprints": 4, "limits.max_calls": 60,
              "behavior.evaluator": "final", "behavior.best_of_n": 1, "behavior.edit_style": "auto"},
    "mid":   {"limits.max_files_per_step": 4, "limits.max_revisions": 3, "limits.max_sprints": 5, "limits.max_calls": 80,
              "behavior.evaluator": "final", "behavior.best_of_n": 1, "behavior.edit_style": "patch"},
    "large": {"limits.max_files_per_step": 6, "limits.max_revisions": 3, "limits.max_sprints": 6, "limits.max_calls": 100,
              "behavior.evaluator": "each", "behavior.best_of_n": 2, "behavior.edit_style": "patch"},
}
SLOW_TOK_S = 5.0        # below this generation speed, sampled retries and per-sprint judging cost more than they return

_MOE = re.compile(r"(?:^|[-_:])a(\d+(?:\.\d+)?)b\b|moe|gpt-oss", re.I)


def params_b(name):
    m = re.search(r"(\d+(?:\.\d+)?)\s*[bB]\b", name or "")
    return float(m.group(1)) if m else None


def active_from_arch(total_b, arch):
    """Estimated ACTIVE parameters (billions) of a mixture-of-experts model from the expert counts Ollama reports, or None for a dense model.
    A heuristic: shared attention and embeddings always run, plus the routed fraction of the experts (about 8% + 92% x used/experts;
    it gives about 4B for a 30B-A3B and a 20B gpt-oss, close to their published 3.3B and 3.6B)."""
    ex, used = (arch or {}).get("experts"), (arch or {}).get("experts_used")
    if not total_b or not ex or not used or used >= ex:
        return None
    return round(total_b * (0.08 + 0.92 * used / ex), 1)


def effective_b(name, total_b=None, active_b=None):
    """Rough dense-equivalent size. Dense: the parameter count. Mixture-of-experts: geometric mean of total and active
    parameters, times 1.3 (they punch above their active count but below their total). A heuristic, stated as one."""
    total = total_b or params_b(name)
    if not total:
        return None
    if active_b is None:
        m = _MOE.search(name or "")
        if m and m.group(1):
            active_b = float(m.group(1))
    if active_b and active_b < total:
        return round(math.sqrt(total * active_b) * 1.3, 1)
    return float(total)


def size_tier(eff_b):
    if eff_b is None:
        return "small"          # unknown size: the middle-cautious choice, and the Scoreboard can correct it
    if eff_b < 4.5:
        return "tiny"
    if eff_b < 11:
        return "small"
    if eff_b < 26:
        return "mid"
    return "large"


def tier_for(name, total_b=None, active_b=None, scores=None):
    """-> (tier, why). `scores` is {category: 0-100} measured on the Scoreboard (tool calls and format matter most here)."""
    eff = effective_b(name, total_b, active_b)
    tier = size_tier(eff)
    why = f"{name}: about {eff}B dense-equivalent" if eff else f"{name}: size unknown, assuming small"
    s = scores or {}
    weak = [k for k in ("toolcall", "format") if s.get(k) is not None and s[k] < 45]
    if weak and tier != "tiny":
        tier = TIERS[TIERS.index(tier) - 1]
        why += f"; demoted one tier because measured {' and '.join(weak)} score is low"
    return tier, why


def settings_for(tier, gen_tok_s=None):
    """-> settings patch (dotted paths) for the tier, softened when the builder model is slow on this machine."""
    patch = dict(SETTINGS[tier])
    if gen_tok_s is not None and gen_tok_s < SLOW_TOK_S:
        patch["behavior.best_of_n"] = 1
        if patch.get("behavior.evaluator") == "each":
            patch["behavior.evaluator"] = "final"
    return patch


def crew_assignment(plan_assignment, tier, ranked_models):
    """Shape the role -> model assignment to the tier's crew size. `plan_assignment` is roles.recommend()'s plan;
    `ranked_models` is [model names, best builder first]. solo: everything on the best builder; duo: builder roles on the best
    builder, thinking roles on the best OTHER model if there is one; crew: the recommended assignment as is."""
    if tier in ("mid", "large") or not ranked_models:
        return dict(plan_assignment)
    builder = ranked_models[0]
    out = {r: builder for r in plan_assignment}
    if tier == "small" and len(ranked_models) > 1:
        thinker = ranked_models[1]
        for r in ("planner", "architect", "evaluator", "reviewer", "bug_hunter"):
            if r in out:
                out[r] = thinker
    return out
