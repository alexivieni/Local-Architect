"""Checks for tools/overnight.py. No model, no GPU, no network: the eval children are stand-in Python scripts, the data folder is a
throwaway one, and Claude is a fake object.      python tools/check_overnight.py   ->  ALL N OVERNIGHT CHECKS PASSED"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

DATA = tempfile.mkdtemp(prefix="arch_overnight_")
os.environ["ARCHITECT_DATA_DIR"] = DATA                    # the checks own their data folder (CLAUDE.md)
os.environ["ARCHITECT_NO_CACHE"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import overnight as O  # noqa: E402

O.POLL_S = 0.2
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("  ok    " if cond else "  FAIL  ") + name + ("" if cond else f"  -- {detail}"))


def quiet_night(hours=1.0):
    n = O.Night(hours)
    n.say = lambda m: None
    return n


def fake_popen(script):
    """A Popen that ignores the eval command and runs this Python snippet instead (same pipes, same env)."""
    def popen(cmd, **kw):
        return subprocess.Popen([sys.executable, "-c", script], **kw)
    return popen


# --- which family is which flow, read without running the module
fam = tempfile.mkdtemp(prefix="fam_")
with open(os.path.join(fam, "alpha.py"), "w") as f:
    f.write('FLOW = "build"\nDEFAULT_N = 2\ndef generate(n, seed): raise SystemExit("must not run")\n')
with open(os.path.join(fam, "beta.py"), "w") as f:
    f.write("def generate(n, seed): return []\n")
check("family_flow reads FLOW with ast", O.family_flow("alpha", fam) == "build")
check("family_flow falls back to the family's name", O.family_flow("beta", fam) == "beta")
check("family_flow: no file -> None", O.family_flow("nope", fam) is None)

# --- the narrowest eval arguments for a family
own = [{"family": "harness", "flow": "harness", "tags": ["a"]}]
check("a family that owns its flow runs with --flow only", O.family_args("harness", own) == (["--flow", "harness"], ""))
mixed = [{"family": "distributed", "flow": "build", "tags": ["dist", "x"]}, {"family": "distributed", "flow": "build", "tags": ["dist"]},
         {"family": "e2e", "flow": "build", "tags": ["x"]}]
check("a shared flow narrows by the family's own tag", O.family_args("distributed", mixed)[0] == ["--flow", "build", "--tags", "dist"])
shared = [{"family": "distributed", "flow": "build", "tags": ["x"]}, {"family": "e2e", "flow": "build", "tags": ["x"]}]
a, note = O.family_args("distributed", shared)
check("no unique tag: --flow with a note that others run too", a == ["--flow", "build"] and "1 task" in note, note)
check("a family with no tasks gives no arguments", O.family_args("zzz", own)[0] is None)

# --- the plan: missing families are skipped, the representative set always comes last
plan = O.eval_plan(["surely_not_a_family_xyz"], quick=True, load_tasks=lambda: [])
check("a family not on this install is skipped with a reason", plan[0]["args"] is None and "surely_not_a_family_xyz" in plan[0]["note"])
check("the representative set is last", plan[-1]["name"] == "representative" and plan[-1]["args"] == ["--set", "representative"])

# --- reading the report line
check("last_json takes the final JSON line", O.last_json(["[eval] hello", '{"a": 1}', "noise", '{"summary": {"n": 3}}']) == {"summary": {"n": 3}})
check("last_json: none -> None", O.last_json(["nothing", "{broken"]) is None)

# --- a child that finishes, one ended by the time limit, one ended by the stop file
n = quiet_night()
r = O.run_child([sys.executable, "-c", "print('line1'); print('{\"x\": 2}')"], n, 60)
check("run_child finishes and keeps the lines", r["why"] == "" and r["rc"] == 0 and O.last_json(r["lines"]) == {"x": 2}, r)
os.environ["OVERNIGHT_CHECK_ENV"] = "seen"
r = O.run_child([sys.executable, "-c", "import os; print(os.environ.get('OVERNIGHT_CHECK_ENV'))"], n, 60)
check("the child inherits this environment (the heavy-job hold reaches it)", r["lines"][:1] == ["seen"], r["lines"])
t = time.time()
r = O.run_child([sys.executable, "-c", "import time; time.sleep(60)"], n, 1.0)
check("the time limit ends a child", r["why"] == "time" and time.time() - t < 40, r)
n2 = quiet_night()
with open(O.stop_path(), "w") as f:
    f.write("stop")
r = O.run_child([sys.executable, "-c", "import time; time.sleep(60)"], n2, 60)
check("the stop file ends a child", r["why"] == "stop" and n2.stopped, r)
os.remove(O.stop_path())
old = quiet_night()
with open(O.stop_path(), "w") as f:
    f.write("old")
past = time.time() - 3600
os.utime(O.stop_path(), (past, past))
check("a stop file left from before the night is ignored", not old.stop_asked())
os.remove(O.stop_path())

# --- step 1 end to end with a stand-in eval
report = {"summary": {"n": 4, "passed": 3, "not_sure": 1, "pass_rate": 75.0, "tokens": 1234, "flows": {"build": {"pass_rate": 75.0}}},
          "path": "x.json", "task_changes": {"won": ["t1"], "lost": [], "same": 3}}
n3 = quiet_night()
rows = O.step_evals(n3, ["surely_not_a_family_xyz"], popen=fake_popen("print('[eval] working'); print(%r)" % json.dumps(report)),
                    load_tasks=lambda: [])
check("step_evals: the missing family is skipped", rows[0]["status"] == "skipped")
check("step_evals: the representative run is read from its JSON", rows[1]["status"] == "ran" and rows[1]["pass_rate"] == 75.0
      and rows[1]["passed"] == 3, rows[1])
n4 = quiet_night()
rows = O.step_evals(n4, [], popen=fake_popen("import sys; print('The eval needs a model server.'); sys.exit(1)"), load_tasks=lambda: [])
check("an eval with no report is a failure and a problem", rows[-1]["status"] == "failed" and any("model server" in p for p in n4.problems), n4.problems)
n5 = quiet_night(hours=0.1)                                       # 6 min < the reserve kept for steps 2-6
rows = O.step_evals(n5, [], popen=fake_popen("print('{}')"), load_tasks=lambda: [])
check("no time left: the eval is skipped, not started", rows[-1]["status"] == "skipped" and rows[-1]["detail"] == "time limit", rows)
rows = O.step_evals(quiet_night(), [], dry=True, load_tasks=lambda: [])
check("dry run starts nothing", rows[-1]["status"] == "skipped" and rows[-1]["detail"] == "dry run")


# --- step 2: dedup + quality floor are asked for, and new samples are counted against the last night
class FakeDS:
    asked = {}

    def roots(self, root, include_portfolio=False):
        return [root]

    def build(self, roots, min_quality=0.0, dedupe=True):
        FakeDS.asked = {"min_quality": min_quality, "dedupe": dedupe}
        return {}

    def write(self, data, out):
        return {"stats": {"runs": 2, "sft": FakeDS.sft, "dpo": 1, "duplicates": 4, "low_quality": 2}}


FakeDS.sft = 10
d1 = O.step_dataset(quiet_night(), min_quality=0.6, ds=FakeDS())
check("the export asks for dedup and the quality floor", FakeDS.asked == {"min_quality": 0.6, "dedupe": True}, FakeDS.asked)
check("first night: every sample counts as new", d1["new_sft"] == 10 and d1["first_time"], d1)
FakeDS.sft = 13
d2 = O.step_dataset(quiet_night(), ds=FakeDS())
check("next night: only the growth is new", d2["new_sft"] == 3 and not d2["first_time"], d2)


class BadDS(FakeDS):
    def build(self, *a, **k):
        raise RuntimeError("disk full")


nb = quiet_night()
check("a failed export is a problem, not a crash", O.step_dataset(nb, ds=BadDS()) is None and nb.problems)


# --- steps 3 and 4 with stand-ins
class FakeLearn:
    def ingest(self):
        return {"added": 2, "updated": 1, "files": 5}

    def playbooks(self, include_claude=True):
        return os.path.join(DATA, "memory", "playbooks.md")


lb = O.step_logbook(quiet_night(), learn=FakeLearn())
check("logbook: ingest and playbooks are both done", lb["ingest"]["added"] == 2 and lb["playbooks"].endswith("playbooks.md"))


class FakeOverseer:
    def lessons(self, write=True):
        return ["agents read the same file too often"] if write else []


check("lessons are written and returned", O.step_lessons(quiet_night(), overseer=FakeOverseer()) == ["agents read the same file too often"])

# --- step 5: copies only, nothing moved, unchanged files not copied twice, a missing drive is a problem
os.makedirs(os.path.join(DATA, "evals"), exist_ok=True)
with open(os.path.join(DATA, "evals", "20261104_live.json"), "w") as f:
    f.write("{}")
arch_dir = tempfile.mkdtemp(prefix="archive_")
a1 = O.step_archive(quiet_night(), {"storage": {"archive": arch_dir}})
copied_to = os.path.join(arch_dir, "architect_archive", "_overnight", "evals", "20261104_live.json")
check("archive: a cold result is copied", a1 and a1["copied"] >= 1 and os.path.isfile(copied_to), a1)
check("archive: the original stays where it was", os.path.isfile(os.path.join(DATA, "evals", "20261104_live.json")))
a2 = O.step_archive(quiet_night(), {"storage": {"archive": arch_dir}})
check("archive: an unchanged file is not copied again", a2["copied"] == 0, a2)
check("archive: not set -> skipped", O.step_archive(quiet_night(), {"storage": {"archive": ""}}) is None)
nm = quiet_night()
check("archive: drive missing -> a problem", O.step_archive(nm, {"storage": {"archive": os.path.join(arch_dir, "unplugged")}}) is None
      and nm.problems)


# --- Claude: refused by the gate, capped, and counted
class FakeCloud:
    def __init__(self, ok=True, price=0.01):
        self.ok, self.price, self._s, self.calls = ok, price, {"usd": 0.0, "calls": 0}, 0

    def allowed(self, cfg):
        return (True, "") if self.ok else (False, "Claude is off: online features are off")

    def chosen_model(self, cfg):
        return "claude:test"

    def cost(self, model, pin, pout):
        return self.price

    def spent(self):
        return dict(self._s)

    def chat(self, model, msgs, options=None, cfg=None):
        self.calls += 1
        self._s["usd"] += 0.004
        return {"text": "- look at the harness family first"}


nc = quiet_night()
fc = FakeCloud(ok=False)
check("Claude refused by its gate: no call, a problem", O.claude_opinion(nc, {}, "note", cloud=fc) is None and fc.calls == 0 and nc.problems)
fc = FakeCloud(price=5.0)
check("Claude over the night's cap: no call", O.claude_opinion(quiet_night(), {}, "note", cap_usd=0.1, cloud=fc) is None and fc.calls == 0)
fc = FakeCloud()
co = O.claude_opinion(quiet_night(), {}, "note", cloud=fc)
check("Claude allowed: one call, its cost counted", co and fc.calls == 1 and abs(co["usd"] - 0.004) < 1e-9, co)

# --- step 6: the note
nn = quiet_night()
nn.problem("eval harness gave no report")
evals = [{"name": "harness", "status": "failed"}, {"name": "representative", "status": "ran", "n": 4, "passed": 3, "pass_rate": 75.0,
          "not_sure": 1, "tokens": 1234, "secs": 600, "task_changes": {"won": ["t1"], "lost": [], "same": 3}, "per_flow": {"build": 75.0}}]
text = O.morning_note(nn, evals, d2, lb, ["a lesson"], a1, date="2026-11-05")
for want, label in [("# Morning note, 2026-11-05", "title"), ("| representative | ran | 3/4 | 75.0% |", "pass-rate row"),
                    ("13 samples the checks accepted (3 new", "new samples"), ("local models: $0", "local cost $0"),
                    ("Claude: $0.0000 (not used", "no Claude by default"), ("eval harness gave no report", "problems"),
                    ("now passing t1", "task changes"), ("- a lesson", "lessons")]:
    check("note: " + label, want in text, text[:400])
text2 = O.morning_note(quiet_night(), evals, None, None, None, None, claude=co, date="2026-11-05")
check("note with Claude: its cost and opinion are shown", "Claude: $0.0040" in text2 and "look at the harness family" in text2)
p = O.write_note(text, "2026-11-05")
check("the note is written to learning/morning_<date>.md", p == os.path.join(DATA, "learning", "morning_2026-11-05.md") and os.path.isfile(p))

# --- the whole night: paused, and a dry run that writes nothing
with open(O.pause_path(), "w") as f:
    f.write("")
before = set(os.listdir(os.path.join(DATA, "learning")))
check("paused: the night does nothing", O.main(["--hours", "1"]) == 0 and set(os.listdir(os.path.join(DATA, "learning"))) == before)
os.remove(O.pause_path())
check("dry run: exits 0 and writes no note", O.main(["--dry-run", "--families", "surely_not_a_family_xyz"]) == 0
      and not os.path.isfile(os.path.join(DATA, "learning", "morning_" + time.strftime("%Y-%m-%d") + ".md")))

shutil.rmtree(fam, ignore_errors=True)
shutil.rmtree(arch_dir, ignore_errors=True)
shutil.rmtree(DATA, ignore_errors=True)
ok = sum(1 for _, c in results if c)
print(f"\n  {'ALL ' + str(ok) + ' OVERNIGHT CHECKS PASSED' if ok == len(results) else str(len(results) - ok) + ' of ' + str(len(results)) + ' OVERNIGHT CHECKS FAILED'}")
sys.exit(0 if ok == len(results) else 1)
