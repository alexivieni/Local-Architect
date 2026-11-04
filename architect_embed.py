"""Use the Architect from inside another program, with the same behaviour as the command line.

    import sys; sys.path.insert(0, "/path/to/architect")
    from architect_embed import Architect

    a = Architect(home="/path/to/app/data/architect", root="/path/to/project")   # home: where settings and results live (default: next to the code)
    a.ready(yes=True)                        # doctor + models + tune + proof, in one call
    a.run("add rate limiting to the API", review="auto")
    a.fakecode(); a.scan(); a.settings(); a.recommend()

Nothing here opens a window or reads the keyboard. Questions that need a person (a download, a diff to approve) are either refused
(`yes=False`) or passed to the `ask` callback you supply. Existing architect_config.json files from older versions keep working: missing
settings take their defaults.
"""
import os
import sys
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import arch_core as core  # noqa: E402


class Architect:
    def __init__(self, home=None, root=None, say=None):
        self.home = core.set_home(home) if home else core.HOME
        self.root = os.path.abspath(root or os.getcwd())
        self.say = say or (lambda m: None)          # progress sink; silent by default so a host app stays quiet

    def _use(self):
        """The core keeps its paths module-wide; re-assert ours before every call so two instances in one process do not cross."""
        core.set_home(self.home)

    # ---- health and setup
    def doctor(self, fix=False):
        self._use()
        import arch_doctor
        return arch_doctor.fix(self.root) if fix else arch_doctor.run(self.root)

    def ready(self, yes=False, ask=None, probe=True, proof=True, smoke=True):
        self._use()
        import arch_ready
        return arch_ready.ready(self.root, yes=yes, say=self.say, probe=probe, proof=proof, ask=ask, smoke=smoke)

    def tune(self, models=None, probe=True, apply=True, deep=False):
        self._use()
        import arch_autotune
        return arch_autotune.tune(models=models, probe=probe, apply=apply, deep=deep, say=self.say)

    def hosts(self, add=None, remove=None, route=None):
        """Other machines running Ollama. add={'name': 'http://ip:11434'}, remove=name, route={'role': 'host'}. Returns the probed machines."""
        self._use()
        import arch_hosts as H
        import arch_autotune
        import model_hub as mh
        arch_autotune._wire()
        cfg = mh.read_config()
        for n, a in (add or {}).items():
            cfg.setdefault("hosts", {})[n] = H.normalize(a)
        if remove:
            cfg.get("hosts", {}).pop(remove, None)
            for r in [r for r, h in (cfg.get("role_hosts") or {}).items() if h == remove]:
                H.unroute(cfg, r)
        for r, n in (route or {}).items():
            H.route(cfg, r, n)
        if add or remove or route:
            mh.write_config(cfg)
        return {n: H.probe(u) for n, u in H.hosts_of(cfg).items()}

    def power(self):
        """Battery state: {'present','on_battery','pct'}."""
        import arch_power
        return arch_power.battery()

    def optimize(self, apply=False, undo=False, models=None):
        """Per-model sampling, server settings and framework advice for the installed models. apply writes only empty sampling keys."""
        self._use()
        import arch_hw
        import arch_modelopt as mo
        at = mo.at
        at._wire()
        if undo:
            return mo.undo(say=self.say)
        hw = arch_hw.detect()
        rows = mo.mh.model_list(hw, "f16", 192.0, 50.0)
        if apply:
            return mo.apply(rows, hw, models=models, say=self.say)
        return mo.plan(hw, rows, mo.mh.read_config(), at.load_profile())

    def ci(self, run_local=False, init=False):
        """Verify the project's CI/CD pipeline files (no model). -> {"summary", "findings", "local"}; init writes a baseline when there is none."""
        import arch_ci
        root = self.root
        if init and not arch_ci.find_pipelines(root):
            for rel, txt in arch_ci.baseline(root).items():
                os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
                with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
                    f.write(txt)
        res = arch_ci.check_all(root)
        loc = [x for rel in res["pipelines"] for x in arch_ci.run_local(root, rel, say=self.say)] if run_local else []
        return {"summary": arch_ci.summarize(res), "findings": res["findings"], "local": loc}

    def resumable(self):
        """The newest run that crashed or stopped and was not resumed, or None."""
        import arch_resume
        return arch_resume.resumable(self.root)

    def tune_status(self):
        self._use()
        import arch_autotune
        return arch_autotune.status()

    def recommend(self):
        """Models you do not have that fit this machine (never downloads)."""
        self._use()
        import arch_autotune
        import arch_hw
        import model_hub as mh
        arch_autotune._wire()
        hw = arch_hw.detect()
        try:
            rows = mh.model_list(hw, "f16", 192.0, 50.0)
        except mh.OllamaError:
            rows = []
        return arch_autotune.suggest(hw, rows, mh.read_config(), arch_autotune.load_profile())

    def pull(self, tag):
        """Download a model. Only call this after showing the person the size."""
        self._use()
        import arch_autotune
        import arch_catalog
        arch_autotune._wire()
        return arch_catalog.pull(tag, confirm=True, say=self.say)

    # ---- understanding and cleaning a project
    def scan(self, run_tests=False):
        import arch_scan
        p = arch_scan.scan(self.root, run_tests)
        arch_scan.write(self.root, p)
        return p

    def fakecode(self, all_findings=False):
        import arch_fakecode as fc
        files = [rel for rel, _ in __import__("arch_scan").walk(self.root) if rel.endswith(".py")]
        found = fc.scan(self.root, files)
        return {"summary": fc.summarize(found), "findings": found if all_findings else [x for x in found if x["gate"] or x["severity"] != "low"]}

    # ---- doing work
    def run(self, goal, flow="auto", review="auto", model=None, role_models=None, settings=None, params=None):
        """Run any flow on `self.root` and return its result dict (adds "report" = path of report.md).
        review: "auto" applies verified edits, "dry" writes nothing, "ask" needs a terminal (use only from a CLI)."""
        self._use()
        import architect as cli
        import arch_flows as flows
        args = SimpleNamespace(root=self.root, app=None, review=review, model=model, role_model=[f"{k}={v}" for k, v in (role_models or {}).items()],
                               max_revisions=None, best_of=None, say=self.say, set=[f"{k}={v}" for k, v in (settings or {}).items()])
        forced = None if flow == "auto" else flow
        probe = cli.build_engine(args, goal, "route", persist=False)
        flow_id, doms, why = flows.route(probe, goal, forced)
        e = cli.build_engine(args, goal, flow_id)
        p = dict(params or {})
        if "domains" not in p and doms:
            p["domains"] = doms
        out = flows.run_flow(e, flow_id, p)
        out = dict(out or {})
        out.update(flow=flow_id, why=why, report=os.path.join(e.run_dir, "report.md"))
        return out

    def settings(self, patch=None):
        """Read the effective settings, or validate and write a patch such as {"limits.num_ctx": 16384}."""
        self._use()
        import arch_params
        if patch:
            import model_hub as mh
            coerced = arch_params.validate_settings_patch(patch)
            cfg = mh.read_config()
            for path, v in coerced.items():
                d, parts = cfg, path.split(".")
                for part in parts[:-1]:
                    d = d.setdefault(part, {})
                d[parts[-1]] = v
            mh.write_config(cfg)
        return core.load_config(self.root)

    # ---- the Architect improving itself
    def self_check(self):
        import arch_selfimprove as si
        return si.verify()

    def self_improve(self, goal, yes=False, suite="all"):
        import arch_selfimprove as si
        return si.improve(goal, review="auto", yes=yes, suite=suite, say=self.say)
