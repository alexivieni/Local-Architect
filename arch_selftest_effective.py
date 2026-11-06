"""Self-test for effective settings (arch_effective): the value and the layer that set it, per role, with roles, presets and overrides
built into a throwaway data folder. No model is called.
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="archeff_")
os.environ["ARCHITECT_DATA_DIR"] = _TMP
os.environ["ARCHITECT_HOME"] = _TMP                     # the settings file lives in HOME: never touch the real one
os.environ["ARCHITECT_NO_CACHE"] = "1"


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    print("\n== effective settings ==")
    import arch_core as core
    import arch_effective as E
    import arch_roleprofile as rp
    import arch_roles as roles
    try:
        base = core.deep_merge(core.DEFAULTS, {})
        r = E.effective_settings(base, "generator")
        k = r["keys"]
        check("every layer is named", E.LAYERS == ["explicit arg", "person role profile", "preset", "shipped role profile", "model family default", "built-in default"])
        check("nothing set -> role sampling is from the shipped role, limits are built-in",
              all(v["layer"] in E.LAYERS for v in k.values()) and k["limits.max_calls"]["layer"] == E.BUILTIN, {a: b["layer"] for a, b in k.items()})
        check("temperature without any profile comes from the role/built-in", k["temperature"]["layer"] in (E.ROLE, E.BUILTIN), k["temperature"])
        cfg = core.deep_merge(core.DEFAULTS, {"limits": {"max_calls": 99}, "roles": {"generator": "mymodel:7b"}})
        k = E.effective_settings(cfg, "generator")["keys"]
        check("your own limit shows as set by you", k["limits.max_calls"] == {"value": 99, "layer": E.PERSON, "why": "your settings"}, k["limits.max_calls"])
        check("a model assigned to the role is yours", k["model"]["value"] == "mymodel:7b" and k["model"]["layer"] == E.PERSON, k["model"])
        k = E.effective_settings(cfg, "generator", explicit={"temperature": 0.9, "model": "x:1b"})["keys"]
        check("an explicit argument wins and says so", k["temperature"]["layer"] == E.EXPLICIT and k["temperature"]["value"] == 0.9
              and k["model"]["layer"] == E.EXPLICIT and k["model"]["value"] == "x:1b", (k["temperature"], k["model"]))
        cfg = core.deep_merge(core.DEFAULTS, {"role_overrides": {"generator": {"sampling": {"temperature": 0.05}, "think": True}}})
        k = E.effective_settings(cfg, "generator")["keys"]
        check("a role override is a person layer", k["temperature"]["layer"] == E.PERSON and k["temperature"]["value"] == 0.05 and k["think"]["layer"] == E.PERSON, k["temperature"])
        cfg = core.deep_merge(core.DEFAULTS, {"behavior": {"model_defaults": "auto"}, "roles": {"generator": "qwen3:8b"}})
        k = E.effective_settings(cfg, "generator", family={"sampling": {"top_k": 20, "temperature": 0.6}, "thinking": False})["keys"]
        check("a model-family default fills what you left unset", k["top_k"]["layer"] == E.FAMILY and k["think"]["layer"] in (E.FAMILY, E.PERSON, E.ROLE)
              and k["temperature"]["layer"] == E.FAMILY, (k.get("top_k"), k["temperature"]))
        # shipped role profile / person role profile / preset
        ship = rp.shipped()
        cfg = core.deep_merge(core.DEFAULTS, {"behavior": {"role_profiles": "on"}})
        role = next((x for x in ship if x in roles.ROLES and ship[x].get("reply_max")), None)
        if role:
            k = E.effective_settings(cfg, role)["keys"]
            check("role profiles on: the shipped profile's reply cap shows as the shipped layer",
                  k["num_predict"]["layer"] == E.ROLE and k["num_predict"]["value"] == ship[role]["reply_max"], k["num_predict"])
            cfg2 = core.deep_merge(cfg, {"role_profiles": {role: {"reply_max": 333}}})
            k = E.effective_settings(cfg2, role)["keys"]
            check("your role profile beats the shipped one", k["num_predict"]["layer"] == E.PERSON and k["num_predict"]["value"] == 333, k["num_predict"])
            k = E.effective_settings(cfg2, role, explicit={"num_predict": 50})["keys"]
            check("and an explicit argument beats both", k["num_predict"]["layer"] == E.EXPLICIT, k["num_predict"])
        else:
            check("a shipped role profile with a reply cap exists", False)
        check("role profiles off: no profile keys leak in", "ctx_max" not in E.effective_settings(core.deep_merge(core.DEFAULTS, {}), "planner")["keys"])
        # a preset applied through arch_roleprofile.apply_preset (isolated config) is attributed to the preset
        import model_hub as mh
        assert os.path.abspath(core.CONFIG_FILE).startswith(os.path.abspath(_TMP)), core.CONFIG_FILE
        mh.CONFIG_FILE = core.CONFIG_FILE
        os.makedirs(os.path.dirname(core.CONFIG_FILE) or ".", exist_ok=True)
        mh.write_config({})
        rec = rp.apply_preset("fast")
        cfg = core.deep_merge(core.DEFAULTS, mh.read_config())
        k = E.effective_settings(cfg, "planner")["keys"]
        check("a value from the preset you applied says which preset", k["ctx_max"]["layer"] == E.PRESET and "fast" in k["ctx_max"]["why"].lower(), k.get("ctx_max"))
        cfg_mine = core.deep_merge(cfg, {"role_profiles": {"planner": {"ctx_max": 5000}}})
        check("changing it yourself makes it yours again", E.effective_settings(cfg_mine, "planner")["keys"]["ctx_max"]["layer"] == E.PERSON)
        check("preset undo restores the old config", rp.undo() is not None and "role_profiles" not in mh.read_config())
        try:
            E.effective_settings(base, "nonsense")
            check("an unknown role is refused", False)
        except ValueError:
            check("an unknown role is refused", True)
        n = sum(1 for x in roles.ROLES if E.effective_settings(base, x)["keys"])
        check("every role answers", n == len(roles.ROLES), n)
    except Exception as ex:
        import traceback
        traceback.print_exc()
        check("effective checks ran without an exception", False, repr(ex))
    shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(run())
