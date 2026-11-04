"""Self-test for the three things that let a person shape how the harness works, without editing it:

  skills    short Markdown notes about how this project does things, loaded only for the tasks they apply to
  commands  saved requests invoked by name (/review, /fix), which expand to words and nothing else
  hooks     your own commands at fixed points in a run -- and the approval that stops a cloned project running them unasked

The approval rule is the one worth testing hardest: a hook file arrives with a project's code, so if it ran on sight, opening someone
else's repository here would run their commands on this computer.

No model, no GPU, no network.
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    print("\n== shaping it yourself: skills, saved commands, your own hooks ==")
    import arch_core as core
    home = tempfile.mkdtemp(prefix="archag_")
    old_home, old_dd = core.HOME, os.environ.get("ARCHITECT_DATA_DIR")
    core.set_home(home)
    os.environ["ARCHITECT_DATA_DIR"] = os.path.join(home, "_data")
    proj = os.path.join(home, "proj")
    os.makedirs(proj)
    try:
        _skills(check, proj)
        _commands(check, proj)
        _hooks(check, proj)
        _port(check, proj, home)
    finally:
        core.set_home(old_home)
        if old_dd is None:
            os.environ.pop("ARCHITECT_DATA_DIR", None)
        else:
            os.environ["ARCHITECT_DATA_DIR"] = old_dd
        shutil.rmtree(home, ignore_errors=True)

    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' AGENTIC CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' AGENTIC CHECKS FAILED'}")
    return 0 if n == len(results) else 1


# ------------------------------------------------------------------ skills

def _skills(check, proj):
    import arch_skills as sk
    shipped = sk.load_all()
    check("the harness ships with skills", len(shipped) >= 5, len(shipped))
    check("every shipped skill is in good shape (a description and a reason it would be chosen)", sk.check() == [], sk.check())

    check("a skill is chosen for a task it applies to",
          "failing-test-first" in [x["name"] for x in sk.relevant("fix the off-by-one bug in allow()")])
    check("and nothing is loaded for a task none of them apply to",
          sk.relevant("what is the capital of France") == [] and sk.relevant("make the report prettier") == [])
    check("at most two are loaded at once, because a small context window is the scarce thing",
          all(len(sk.relevant(g)) <= 2 for g in ["fix the bug and add a migration and tests and security review",
                                                 "migration schema column index bug test crash import error"]))
    check("a character budget is honoured even when more would match",
          sum(len(x["body"]) for x in sk.relevant("fix the bug in the migration", budget=900)) <= 900 + 200)

    # a project's own skill outranks the shipped one and can be written by hand with no header at all
    d = os.path.join(proj, ".arch", "skills")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "house-style.md"), "w", encoding="utf-8") as f:
        f.write("# House style\n\nWe never use an ORM here; write SQL by hand against the migration files.\n")
    local = sk.load_all(proj)
    check("a plain note dropped in the project's folder counts as a skill, header or not", "house style" in local, sorted(local))
    one = local.get("house style") or {}
    check("its first heading becomes its name and its first paragraph its description",
          one.get("name") == "House style" and "ORM" in (one.get("description") or ""), one.get("description"))
    check("a project's own skill is marked as this project's, so the model can tell whose rule it is", one.get("scope") == "project")

    with open(os.path.join(d, "failing-test-first.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: failing-test-first\ndescription: our version\nwhen: bug, crash\n---\nDo it our way.\n")
    both = sk.load_all(proj)
    check("a project skill replaces the shipped one of the same name", both["failing-test-first"]["scope"] == "project")

    text, used = sk.for_prompt("fix the crash in allow()", proj)
    check("what is loaded goes into the prompt, saying where each piece came from",
          "how this project does things" in text and "this project" in text and used, (text[:90], used))
    sk.mark_used(used)
    sk.mark_helped(used)
    rows = {r["name"]: r for r in sk.summary(proj)}
    check("a skill that was in front of the model when the run passed earns a point",
          rows[used[0]]["used"] == 1 and rows[used[0]]["helped"] == 1, rows[used[0]])
    check("and the count is reported as a rate a person can read", rows[used[0]]["rate"] == 100, rows[used[0]])

    with open(os.path.join(d, "bad.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: bad\n---\n")
    probs = sk.check(proj)
    check("a skill with nothing in it is reported as a problem rather than silently ignored",
          any("bad" in p or "no text" in p for p in probs), probs)
    os.remove(os.path.join(d, "bad.md"))


# ------------------------------------------------------------------ saved commands

def _commands(check, proj):
    import arch_commands as C
    shipped = C.load_all()
    check("the harness ships with saved commands", len(shipped) >= 5, sorted(shipped))
    check("each one says what it does and which flow it runs",
          all(c["description"] and c["flow"] for c in shipped.values()),
          [n for n, c in shipped.items() if not (c["description"] and c["flow"])])

    r = C.expand("/review src/api.py")
    check("a command expands to a request, with what you typed in it",
          r["ok"] and "src/api.py" in r["goal"] and r["flow"] == "review", r)
    check("the expansion is words only -- nothing in a command file can run a program",
          isinstance(r["goal"], str) and "{args}" not in r["goal"])
    bad = C.expand("/revue x")
    check("a mistyped name says so and offers the closest ones", not bad["ok"] and "review" in bad["suggestions"], bad)
    need = C.expand("/fix")
    check("a command that needs something to work on says what it needs", not need["ok"] and "needs" in need["why"], need)
    check("ordinary text is not treated as a command", C.split("fix the bug")[0] is None)
    check("a name with a path in it is not a command", C.expand("/../etc/passwd")["ok"] is False)

    d = os.path.join(proj, ".arch", "commands")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "ship.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: ship\ndescription: our release checklist\nflow: ship\n---\nRun our release checks on {args}.\n")
    local = C.load_all(proj)
    check("a project can add its own commands", "ship" in local and local["ship"]["scope"] == "project", sorted(local))
    e2 = C.expand("/ship main", proj)
    check("and they expand the same way", e2["ok"] and "main" in e2["goal"] and e2["flow"] == "ship", e2)


# ------------------------------------------------------------------ hooks, and the approval that guards them

def _hooks(check, proj):
    import arch_core as core
    import arch_hooks as H

    cfg = core.load_config(proj)
    check("with nothing set up, nothing runs and nothing complains", H.load(proj, cfg) == {"hooks": [], "untrusted": []})

    hf = H.project_file(proj)
    os.makedirs(os.path.dirname(hf), exist_ok=True)
    with open(hf, "w", encoding="utf-8") as f:
        json.dump({"hooks": [{"name": "touch", "on": "after_apply", "match": "*.py",
                              "run": sys.executable + " -c \"print('hook ran')\"", "blocking": True}]}, f)

    found = H.load(proj, cfg)
    check("a hook file that came with the project does NOT run until it has been read and approved",
          found["hooks"] == [] and len(found["untrusted"]) == 1, found)
    check("and what it wanted to run is shown, so the decision can be made on the facts",
          "hook ran" in found["untrusted"][0]["hooks"][0]["run"], found["untrusted"][0])

    s1 = H.summary(proj, cfg)
    check("the summary separates what is running from what is waiting on you",
          s1["active"] == [] and s1["waiting_for_approval"] and s1["waiting_for_approval"][0]["count"] == 1, s1)

    H.trust(hf)
    check("once approved, it runs", len(H.load(proj, cfg)["hooks"]) == 1)
    with open(hf, "a", encoding="utf-8") as f:
        f.write("\n")
    check("editing the file undoes the approval: a changed file is a new decision",
          H.load(proj, cfg)["hooks"] == [] and len(H.load(proj, cfg)["untrusted"]) == 1)
    H.trust(hf)

    class E:
        """The smallest thing a hook needs from an engine: where the project is, the settings, and somewhere to record what happened."""

        def __init__(self, root, cfg):
            self.root, self.cfg, self.flow, self.run_id, self.traced = root, cfg, "bugfix", "r1", []

        def say(self, m):
            pass

        def _trace(self, rec):
            self.traced.append(rec)

    e = E(proj, cfg)
    out = H.fire(e, "after_apply", files=["app.py"], step="write the fix")
    check("a hook runs at the point it was attached to", len(out["ran"]) == 1 and out["ran"][0]["ok"] is True, out)
    check("a hook that passed blocks nothing", out["blocked"] == "", out)
    check("every hook that ran is recorded in the trace, with what it was and how long it took",
          any(r.get("type") == "hook" and r.get("point") == "after_apply" and r.get("ok") for r in e.traced), e.traced[-1:])

    out2 = H.fire(e, "after_apply", files=["notes.txt"], step="x")
    check("a hook only fires for the files it asked for", out2["ran"] == [], out2)
    out3 = H.fire(e, "before_run", files=["app.py"])
    check("and only at its own point", out3["ran"] == [], out3)

    with open(hf, "w", encoding="utf-8") as f:
        json.dump({"hooks": [{"name": "strict", "on": "after_apply", "run": sys.executable + " -c \"import sys;print('NOPE: tabs are not allowed here');sys.exit(1)\"",
                              "blocking": True},
                             {"name": "advisory", "on": "after_apply", "run": sys.executable + " -c \"import sys;sys.exit(3)\"",
                              "blocking": False}]}, f)
    H.trust(hf)
    out4 = H.fire(e, "after_apply", files=["app.py"])
    check("a blocking hook that fails gives back text for the model to fix, like a failing test would",
          "NOPE: tabs are not allowed here" in out4["blocked"], out4["blocked"][:120])
    check("a hook that is not blocking is reported and fails nothing",
          len(out4["ran"]) == 2 and "advisory" not in out4["blocked"], out4)

    with open(hf, "w", encoding="utf-8") as f:
        json.dump({"hooks": [{"on": "after_apply", "run": "definitely-not-a-real-program-xyz", "blocking": True}]}, f)
    H.trust(hf)
    out5 = H.fire(e, "after_apply", files=["app.py"])
    check("a hook whose program is not installed is reported, and never fails your work for you",
          out5["blocked"] == "" and len(out5["ran"]) == 1, out5)

    e.cfg = core.deep_merge(cfg, {"hooks": {"enabled": False}})
    check("turning hooks off turns them off", H.fire(e, "after_apply", files=["app.py"])["ran"] == [])
    e.cfg = cfg

    with open(hf, "w", encoding="utf-8") as f:
        json.dump({"hooks": [{"on": "not_a_real_point", "run": "echo x"},
                             {"on": "after_apply"},
                             {"on": "after_apply", "run": sys.executable + " -c \"pass\""}]}, f)
    H.trust(hf)
    check("a malformed hook entry is skipped and the good ones still work", len(H.load(proj, cfg)["hooks"]) == 1, H.load(proj, cfg))


# ------------------------------------------------------------------ fitting a new machine, and moving between machines

def _port(check, proj, home):
    import arch_core as core
    import arch_port as P

    # ---- the arithmetic that decides what is worth running
    check("squeezing a model smaller costs accuracy, and the cost is known per quantisation",
          P.quant_loss("Q8_0") == 0 and 2 <= P.quant_loss("Q4_K_M") <= 3 and P.quant_loss("Q2_K") >= 15,
          (P.quant_loss("Q8_0"), P.quant_loss("Q4_K_M"), P.quant_loss("Q2_K")))
    check("an unusual quantisation name is still recognised by its family", P.quant_loss("Q2_0") >= 15 and P.quant_loss("IQ4_NL") == 4.0,
          (P.quant_loss("Q2_0"), P.quant_loss("IQ4_NL")))
    check("an unknown one is treated cautiously rather than as perfect", P.quant_loss("") == 5.0 and P.quant_loss("WAT") == 5.0)
    big_bad = {"eff_b": 9.0, "quant": "Q2_0", "coder": False}
    small_good = {"eff_b": 8.0, "quant": "Q4_K_M", "coder": False}
    check("a bigger model squeezed to 2 bits scores BELOW a slightly smaller one at 4 bits",
          P.quality(small_good) > P.quality(big_bad), (P.quality(small_good), P.quality(big_bad)))
    check("size still counts when the quantisation is the same",
          P.quality({"eff_b": 14.0, "quant": "Q4_K_M"}) > P.quality({"eff_b": 7.0, "quant": "Q4_K_M"}))
    check("a model built for code gets a little credit for it",
          P.quality({"eff_b": 7.0, "quant": "Q4_K_M", "coder": True}) > P.quality({"eff_b": 7.0, "quant": "Q4_K_M", "coder": False}))

    # ---- choosing a team from a made-up machine and a made-up set of models
    hw = {"gpu": "Test GPU", "vram_gb": 8.0, "ram_gb": 32.0, "cpu_cores": 8}
    A = {"layers": 32, "kv_heads": 8, "head_dim": 128, "ctx_max": 32768}
    models = [
        {"name": "tiny:0.5b", "size_gb": 0.4, "quant": "Q8_0", "family": "qwen", "layers": 24, "eff_b": 0.5, "arch": dict(A, layers=24),
         "ctx_max": 32768, "moe": False, "embed": False, "coder": False, "think": None, "caps": []},
        {"name": "router:1.5b", "size_gb": 1.0, "quant": "Q4_K_M", "family": "qwen", "layers": 28, "eff_b": 1.5, "arch": dict(A, layers=28),
         "ctx_max": 32768, "moe": False, "embed": False, "coder": False, "think": None, "caps": []},
        {"name": "coder:7b", "size_gb": 4.4, "quant": "Q4_K_M", "family": "qwen", "layers": 28, "eff_b": 7.0, "arch": dict(A, layers=28),
         "ctx_max": 32768, "moe": False, "embed": False, "coder": True, "think": None, "caps": []},
        {"name": "squeezed:9b", "size_gb": 3.0, "quant": "Q2_0", "family": "llama", "layers": 32, "eff_b": 9.0, "arch": A,
         "ctx_max": 32768, "moe": False, "embed": False, "coder": False, "think": None, "caps": []},
        {"name": "big:14b", "size_gb": 8.4, "quant": "Q4_K_M", "family": "qwen", "layers": 40, "eff_b": 14.0, "arch": dict(A, layers=40),
         "ctx_max": 32768, "moe": False, "embed": False, "coder": False, "think": None, "caps": []},
        {"name": "an-embedder", "size_gb": 0.5, "quant": "F16", "family": "bert", "layers": 12, "eff_b": 0.3, "arch": dict(A, layers=12),
         "ctx_max": 512, "moe": False, "embed": True, "coder": False, "think": None, "caps": []},
    ]
    plan = P.pick_tiers(hw=hw, cfg=core.load_config(proj), models=models)
    t = plan["tiers"]
    check("the worker is the best model that fits entirely on the card, not simply the biggest file",
          t["worker"]["model"] == "coder:7b" and t["worker"]["full_gpu"] is True, t["worker"])
    check("a 9B squeezed to 2 bits is passed over for a 7B at 4 bits, even though it is smaller on disk",
          t["worker"]["model"] != "squeezed:9b", t["worker"]["model"])
    check("the router is the smallest model, and it is put on the processor",
          t["router"]["model"] == "tiny:0.5b" and t["router"]["cpu"] is True, t["router"])
    check("the heavy tier is the most capable model that still fits in system memory",
          t["heavy"]["model"] == "big:14b", t["heavy"])
    check("an embedding model is never offered as a chat tier",
          "an-embedder" not in [x.get("model") for x in t.values()], t)
    check("the draft is only filled by a tiny model of the worker's OWN family",
          t["draft"]["model"] == "tiny:0.5b", t["draft"])
    check("every choice comes with a sentence saying why", all(x.get("why") for x in t.values()), {k: bool(v.get("why")) for k, v in t.items()})
    text = P.render(plan)
    check("the explanation leads with plain words and puts the numbers after them",
          "fits entirely on the card" in text and "GB on the card" in text, text[:150])

    # a machine with no graphics card at all must still get a usable answer
    cpu_only = P.pick_tiers(hw={"gpu": "", "vram_gb": 0, "ram_gb": 8, "cpu_cores": 4}, cfg=core.load_config(proj), models=models)
    check("a machine with no graphics card still gets a worker, and is told it runs on the processor",
          cpu_only["tiers"]["worker"]["model"] and "processor" in cpu_only["tiers"]["worker"]["why"], cpu_only["tiers"]["worker"])
    none_at_all = P.pick_tiers(hw=hw, cfg=core.load_config(proj), models=[])
    check("with nothing installed it says so plainly instead of inventing a plan",
          none_at_all["tiers"] == {} and "nothing to choose between" in " ".join(none_at_all["notes"]), none_at_all)

    # ---- packing and adopting
    dd = core.data_dir()
    os.makedirs(os.path.join(dd, "memory"), exist_ok=True)
    with open(os.path.join(dd, "memory", "lessons.md"), "w", encoding="utf-8") as f:
        f.write("- [2026-11-03] a lesson worth carrying\n")
    with open(os.path.join(dd, "memory", "machine.md"), "w", encoding="utf-8") as f:
        f.write("this machine has a 6 GB card\n")
    with open(os.path.join(dd, "profile.json"), "w", encoding="utf-8") as f:
        json.dump({"class": "gpu6"}, f)
    import model_hub as mh
    mh.write_config({"behavior": {"review_mode": "auto"}, "tiers": {"worker": "something-only-here:7b"},
                     "ollama_host": "127.0.0.1:1234", "limits": {"max_calls": 42, "num_ctx": 9999}})
    zp = os.path.join(home, "setup.zip")
    res = P.pack(zp, proj)
    inside = " ".join(res["added"])
    check("what you have taught it travels", "memory/lessons.md" in inside, res["added"])
    check("your skills and saved commands travel", "skills/" in inside and "commands/" in inside, res["added"])
    check("what is true of one computer only is deliberately left behind",
          "profile.json" in res["left_behind"] and "memory/machine.md" in res["left_behind"], res["left_behind"])

    import zipfile
    with zipfile.ZipFile(zp) as z:
        names = z.namelist()
        man = json.loads(z.read("port_manifest.json").decode("utf-8"))
        st = json.loads(z.read("settings.json").decode("utf-8"))
    check("the bundle carries no measurement from the old machine",
          not any("machine.md" in n or "profile.json" in n or "passports" in n for n in names), [n for n in names if "profile" in n])
    check("the bundle says what it left behind and why", man["left_behind"] and "hardware" in man["why_left_behind"], man)
    check("the settings that travel are preferences, not this machine's model choices",
          "tiers" not in st and "ollama_host" not in st and st.get("behavior", {}).get("review_mode") == "auto", st)
    check("a preference travels even when it sits beside a hardware setting in the same section",
          st.get("limits", {}).get("max_calls") == 42 and "num_ctx" not in st.get("limits", {}), st.get("limits"))


if __name__ == "__main__":
    sys.exit(run())
