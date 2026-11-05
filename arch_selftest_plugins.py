"""Self-test: plugins (arch_plugins) and their wiring into skills, commands and hooks. No model, no network.

Runs in a throwaway ARCHITECT_DATA_DIR with ARCHITECT_NO_CACHE=1.
"""
import json
import os
import shutil
import sys
import tempfile
import zipfile

N = [0]
FAILS = []


def ok(cond, what):
    N[0] += 1
    if cond:
        print(f"  ok   {what}")
    else:
        FAILS.append(what)
        print(f"  FAIL {what}")


def _write(p, s):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(s)


def _make(folder, name, skill="demo-skill", cmd="demo", hooks=True, bad_agent=False):
    _write(os.path.join(folder, "plugin.json"), json.dumps({"name": name, "version": "1.0.0", "description": "a test plugin",
                                                             "requires": {"architect": ">=13"}}))
    _write(os.path.join(folder, "skills", skill + ".md"),
           f"---\nname: {skill}\ndescription: zebra quokka handling\nwhen: zebra, quokka\n---\n# {skill}\n\nZebra quokka body.\n")
    _write(os.path.join(folder, "commands", cmd + ".md"), f"---\nname: {cmd}\ndescription: demo command\n---\nDo the {{args}} thing.\n")
    if hooks:
        _write(os.path.join(folder, "hooks.json"), json.dumps({"hooks": [{"on": "after_apply", "run": "python -c pass"}]}))
    if bad_agent:
        _write(os.path.join(folder, "agents", "evil.yaml"), 'name: "evil"\nmodel: "claude:opus"\nsystem: "x"\ntask: "y"\ntools:\n  - "read"\n'
               'budget:\n  calls: 1\n')
    return folder


def run():
    old = {k: os.environ.get(k) for k in ("ARCHITECT_DATA_DIR", "ARCHITECT_NO_CACHE")}
    tmp = tempfile.mkdtemp(prefix="arch_plugins_")
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["ARCHITECT_NO_CACHE"] = "1"
    os.makedirs(os.environ["ARCHITECT_DATA_DIR"])
    print("== plugins selftest ==")
    try:
        import arch_plugins as P
        import arch_skills
        import arch_commands
        import arch_hooks
        cfg = {"plugins": {"enabled": True, "trust_project": False, "disabled": []}, "hooks": {"enabled": True}}
        root = os.path.join(tmp, "proj")
        os.makedirs(root)

        # manifest validation
        ok(P.validate_manifest({"name": "ok-one", "version": "1.2.0", "description": "x"}) == [], "a good manifest has no problems")
        ok(any("name" in x for x in P.validate_manifest({"name": "Bad Name", "version": "1", "description": "x"})), "bad name refused")
        ok(any("version" in x for x in P.validate_manifest({"name": "a", "version": "one", "description": "x"})), "bad version refused")
        ok(any("architect" in x for x in P.validate_manifest({"name": "a", "version": "1", "description": "x",
                                                               "requires": {"architect": ">=99"}})), "too-new requires refused")

        # shipped examples are valid and off by default
        rows = {r["name"]: r for r in P.list(root, cfg)}
        for n in ("python-quality", "web-starter", "security-pack"):
            ok(n in rows and rows[n]["problems"] == [], f"shipped example {n} is valid")
            ok(n in rows and rows[n]["enabled"] is False, f"shipped example {n} is off until enabled")
        ok(rows.get("python-quality", {}).get("provides", {}).get("agents") == ["python-reviewer"], "python-quality agent validated by arch_squad")

        # install from folder
        src = _make(os.path.join(tmp, "src", "demo-plug"), "demo-plug")
        name = P.install(src)
        ok(name == "demo-plug" and os.path.isdir(os.path.join(P.machine_dir(), "demo-plug")), "install from a folder")
        try:
            P.install(src)
            ok(False, "a second install of the same name is refused")
        except ValueError:
            ok(True, "a second install of the same name is refused")

        # enable/disable changes what loads
        ok("demo-skill" not in arch_skills.load_all(root), "disabled plugin: its skill does not load")
        ok("demo" not in arch_commands.load_all(root), "disabled plugin: its command does not load")
        P.enable("demo-plug")
        sk = arch_skills.load_all(root)
        ok("demo-skill" in sk and sk["demo-skill"].get("plugin") == "demo-plug", "enabled plugin: its skill loads (bare name)")
        cm = arch_commands.load_all(root)
        ok("demo" in cm, "enabled plugin: its command loads")
        ok(arch_commands.expand("/demo-plug:demo widgets", root)["ok"] is True, "a namespaced /plugin:name expands")
        P.disable("demo-plug")
        ok("demo-skill" not in arch_skills.load_all(root), "disable removes the skill again")
        P.enable("demo-plug")

        # namespacing: a plugin skill/command that clashes with a shipped one keeps the shipped one bare
        clash = _make(os.path.join(tmp, "src", "clash-plug"), "clash-plug", skill="smallest-change", cmd="review", hooks=False)
        P.install(clash)
        P.enable("clash-plug")
        sk = arch_skills.load_all(root)
        ok(sk.get("smallest-change", {}).get("scope") == "shipped", "shipped skill wins the bare name")
        ok("clash-plug:smallest-change" in sk, "clashing plugin skill resolves as plugin:name")
        cm = arch_commands.load_all(root)
        ok(cm.get("review", {}).get("scope") == "shipped" and "clash-plug:review" in cm, "shipped command wins; plugin one is plugin:name")

        # hooks blocked until trust, re-blocked after edit; three outcomes
        hk = arch_hooks.load(root, cfg)
        ok(not any(h.get("plugin") == "demo-plug" for h in hk["hooks"]) and
           any(u.get("plugin") == "demo-plug" for u in hk["untrusted"]), "plugin hooks wait for trust")
        ok(P.trusted("demo-plug") is False, "trusted() is False before approval")
        ok(P.trust("demo-plug") is True and P.trusted("demo-plug") is True, "trust() approves the exact content")
        ok(any(h.get("plugin") == "demo-plug" for h in arch_hooks.load(root, cfg)["hooks"]), "trusted plugin hooks are active")
        _write(os.path.join(P.machine_dir(), "demo-plug", "skills", "demo-skill.md"), "---\nname: demo-skill\n---\n# changed\n\nedited\n")
        ok(P.trusted("demo-plug") is False, "editing ANY file revokes trust")
        ok(not any(h.get("plugin") == "demo-plug" for h in arch_hooks.load(root, cfg)["hooks"]), "hooks re-blocked after the edit")
        ok(P.trusted("no-such-plugin") is None, "trusted() is None when there is nothing to fingerprint")
        orig = P.fingerprint
        P.fingerprint = lambda folder: ""
        try:
            ok(P.trusted("demo-plug") is None, "trusted() is None when the files cannot be read")
        finally:
            P.fingerprint = orig

        # bad item isolated, never fatal
        bad = _make(os.path.join(tmp, "src", "bad-plug"), "bad-plug", skill="bad-skill", cmd="badcmd", hooks=False, bad_agent=True)
        P.install(bad)
        P.enable("bad-plug")
        r = P.show("bad-plug", root)
        ok(r and any("evil.yaml" in x for x in r["problems"]), "an invalid agent (claude: model) is listed as a problem")
        ok(r and r["provides"]["skills"] == ["bad-skill"], "the plugin's good items still load")
        ok("bad-skill" in arch_skills.load_all(root), "a plugin with a bad agent still serves its skill")
        ok(P.doctor_check(root)[0] is False, "doctor check reports a plugin with problems (False)")

        # zip: pack, install, traversal + absolute refused
        z = P.pack("web-starter", os.path.join(tmp, "web.zip"))
        P.remove("demo-plug")
        ok(not os.path.isdir(os.path.join(P.machine_dir(), "demo-plug")), "remove deletes a machine-installed plugin")
        ok(P.remove("web-starter") is False, "a shipped plugin is not removed")
        zsrc = os.path.join(tmp, "zipme.zip")
        with zipfile.ZipFile(zsrc, "w") as zf:
            zf.writestr("zip-plug/plugin.json", json.dumps({"name": "zip-plug", "version": "1.0.0", "description": "zipped"}))
            zf.writestr("zip-plug/skills/zs.md", "---\nname: zs\n---\n# zs\n\nbody\n")
        ok(P.install(zsrc) == "zip-plug" and os.path.isfile(os.path.join(P.machine_dir(), "zip-plug", "skills", "zs.md")),
           "install from a zip (top folder stripped)")
        ok(os.path.isfile(z), "pack writes a zip")
        for label, entry in (("../ traversal", "../evil.txt"), ("absolute path", "/abs.txt"), ("drive path", "C:/abs.txt")):
            ev = os.path.join(tmp, "evil.zip")
            with zipfile.ZipFile(ev, "w") as zf:
                zf.writestr("plugin.json", json.dumps({"name": "evil-zip", "version": "1.0.0", "description": "x"}))
                zf.writestr(entry, "x")
            try:
                P.install(ev)
                ok(False, f"zip with {label} refused")
            except ValueError:
                ok(not os.path.exists(os.path.join(tmp, "evil.txt")) and not os.path.isdir(os.path.join(P.machine_dir(), "evil-zip")),
                   f"zip with {label} refused")

        # master switch and disabled list
        P.enable("zip-plug")
        ok(any(p["name"] == "zip-plug" for p in P.active(root, cfg)), "enabled plugin is active")
        ok(P.active(root, {"plugins": {"enabled": False}}) == [], "plugins.enabled false turns every plugin off")
        ok(not any(p["name"] == "zip-plug" for p in P.active(root, {"plugins": {"disabled": ["zip-plug"]}})),
           "plugins.disabled lists win over the enabled state")

        # project plugin: needs enabling; trust_project lets its hooks run
        _make(os.path.join(root, ".arch", "plugins", "proj-plug"), "proj-plug", skill="proj-skill", cmd="projcmd")
        ok("proj-skill" not in arch_skills.load_all(root), "a project plugin is not loaded until enabled")
        P.enable("proj-plug", root)
        ok(any(p["name"] == "proj-plug" and p["trusted"] is False for p in P.active(root, cfg)), "an enabled project plugin is untrusted")
        tp = dict(cfg, plugins={"trust_project": True})
        ok(any(p["name"] == "proj-plug" and p["trusted"] is True for p in P.active(root, tp)), "plugins.trust_project trusts it")

        # dirs for arch_squad
        P.enable("python-quality")
        ok(any(n == "python-quality" for _d, n in P.agent_dirs(root, cfg)), "agent_dirs() lists enabled plugin agent folders")
        ok(any(n == "python-quality" for _d, n in P.workflow_dirs(root, cfg)), "workflow_dirs() lists enabled plugin workflow folders")
        ok(P.agent_allowed({"name": "x"}, "python-quality") is True, "a plain YAML agent needs no trust")
        ok(P.agent_allowed({"name": "x", "code": "collect"}, "python-quality") is False, "a code: agent needs the plugin trusted")
        P.disable("python-quality")

        # scaffold
        d = P.new("fresh-one")
        ok(P.read_manifest(d)[1] == [] and P.contents(d)["skills"] == ["fresh-one"], "new() scaffolds a valid plugin")
    except Exception as e:
        import traceback
        traceback.print_exc()
        FAILS.append(f"crashed: {e}")
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    if FAILS:
        print(f"{len(FAILS)} of {N[0]} PLUGINS CHECKS FAILED")
        return 1
    print(f"ALL {N[0]} PLUGINS CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run())
