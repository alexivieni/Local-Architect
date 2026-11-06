"""Self-test for "OUR data": the person's own requests, mined and turned into code-judged training tasks.

No model, no network, no Claude. Proves
  * mine() keeps only the person's own string messages (assistant text, tool results, tag/pasted blocks, meta entries are dropped),
    strips profanity-only lines, user paths, URLs and secrets, and clusters intents;
  * no goal contains assistant text (synthetic markers, and 8-word shingles of every assistant reply in the real transcripts);
  * every task: tagged training + source:ours + intent, a lesson that exists in data/lessons.json where one is named, the reference
    passes the hidden judge and a wrong answer is caught;
  * held-out templates are held out WHOLE and disjoint from the trainable ones (ids, goals, templates); fresh seeds give fresh variants.
"""
import glob
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="archours_")
os.environ["ARCHITECT_DATA_DIR"] = os.path.join(_TMP, "_data")
os.environ["ARCHITECT_HOME"] = _TMP
os.environ["ARCHITECT_NO_CACHE"] = "1"
os.environ["ARCHITECT_NO_NOTIFY"] = "1"


def _w(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _shingles(text, n=8):
    w = re.findall(r"[a-z0-9_`./-]+", text.lower())
    return {" ".join(w[i:i + n]) for i in range(max(0, len(w) - n + 1))}


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    import arch_ourtasks as ot
    fam = ot.family()

    print("\n== mining the person's own words ==")
    tdir = os.path.join(_TMP, "t")
    os.makedirs(tdir)
    me = os.path.basename(os.path.expanduser("~"))
    assist = "ZEBRAQUARTZ assistant reply: I cached the lookup and wrote plain error words for you"
    rows = [
        {"type": "user", "message": {"content": "can you cache the lookups, the second call is slow every time"}},
        {"type": "user", "message": {"content": "FUCK\nmake the ui clearer please, it is hard to use"}},
        {"type": "user", "message": {"content": "fuck"}},
        {"type": "user", "message": {"content": "<system-reminder>cache everything here</system-reminder> and more"}},
        {"type": "user", "isMeta": True, "message": {"content": "meta line about caching things for the gpu"}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "gpu layers tool output text here"}]}},
        {"type": "user", "message": {"content": "look at C:\\Users\\" + me + "\\Downloads\\x.py and fix gpu placement of layers"}},
        {"type": "user", "message": {"content": "here is my key api_key = 'sk-abcdefghijklmnopqrstuvwxyz123456' please add tests"}},
        {"type": "user", "message": {"content": "measure the speed of the worker, tok/s " + "x" * 2000}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": assist}]}},
        {"type": "user", "message": {"content": "add a plugin command to the cli and write tests"}},
    ]
    _w(os.path.join(tdir, "s1.jsonl"), rows)
    proj = os.path.join(_TMP, "proj")
    os.makedirs(os.path.join(proj, ".arch", "runs", "r1"))
    with open(os.path.join(proj, ".arch", "runs", "r1", "run.json"), "w") as f:
        json.dump({"goal": "make the hub easier to read, better layout and labels"}, f)
    m = ot.mine(transcripts=tdir, run_roots=[proj])
    kept = " | ".join(m["kept"]).lower()
    check("only the person's string messages are kept (assistant, tool result, tags, meta, pasted-size dropped)",
          "zebraquartz" not in kept and "tool output" not in kept and "everything here" not in kept and "meta line" not in kept and "xxxx" not in kept, m["kept"])
    check("profanity-only lines are stripped", "fuck" not in kept and "make the ui clearer please, it is hard to use" in kept, m["kept"])
    check("user paths and the user name are stripped", me.lower() not in kept and "c:\\users" not in kept, m["kept"])
    check("a message carrying a secret is dropped whole", "sk-abc" not in kept and "api_key" not in kept)
    check("run goals are read too", m["goals_kept"] == 1 and any("easier to read" in k for k in m["kept"]), m["goals_kept"])
    c = {k: v["count"] for k, v in m["intents"].items()}
    check("intents cluster: cache, ui (message + run goal), gpu, add_command, add_tests",
          c["cache_more"] >= 1 and c["ui_clearer"] >= 2 and c["gpu_placement"] >= 1 and c["add_command"] >= 1 and c["add_tests"] >= 1, c)
    real = ot.mine()
    check("the real transcripts (when present) give kept messages and several intents",
          (real["messages_kept"] > 20 and sum(1 for v in real["intents"].values() if v["count"]) >= 4) or real["messages_seen"] == 0,
          {k: v["count"] for k, v in real["intents"].items()})

    print("\n== the tasks ==")
    tasks = fam.all_tasks(0)
    import arch_eval as E
    norm = [E.normalize(dict(t, flow=fam.FLOW)) for t in tasks]
    check("at least 8 templates and 30 tasks, all valid", len({t["template"] for t in tasks}) >= 8 and len(tasks) >= 30 and not [p for t in norm for p in E.validate(t)])
    check("ids unique and sort after e2e", len({t["id"] for t in tasks}) == len(tasks) and all(t["id"] > "e2e-~" for t in tasks))
    check("every task is tagged training + source:ours + intent:<known intent>",
          all({"training", "source:ours"} <= set(t["tags"]) and ("intent:" + t["intent"]) in t["tags"] and t["intent"] in ot.INTENTS for t in tasks))
    lessons = {x["id"] for x in json.load(open(os.path.join(HERE, "data", "lessons.json"), encoding="utf-8"))["lessons"]}
    lt = [t for t in tasks if t["lesson"]]
    check("a named lesson exists in data/lessons.json and is tagged; at least 4 templates carry one",
          all(t["lesson"] in lessons and ("lesson:" + t["lesson"]) in t["tags"] for t in lt) and len({t["template"] for t in lt}) >= 4, set(t["lesson"] for t in lt) - lessons)
    check("the ours family loads through the eval's own family loader", any(t["id"].startswith("train-ours-") for t in E._load_families(None, 0)))
    check("templates cover at least 6 distinct intents", len({t["intent"] for t in tasks}) >= 6)

    print("\n== no assistant text ==")
    shingles = set()
    n_assist = 0
    base = ot.transcript_dir()
    # Only replies written BEFORE the task templates existed can have been copied into them; a later reply that quotes a
    # finished goal back to the person (reporting what was built) is the other direction and is not a leak.
    _built = os.path.getmtime(os.path.join(os.path.dirname(os.path.abspath(__file__)), "evals", "families", "ours.py"))
    import datetime as _dt
    def _before_build(d):
        ts = d.get("timestamp")
        try:
            return _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp() < _built
        except (TypeError, ValueError):
            return True                      # unknown time: treat as earlier (strict)
    if os.path.isdir(base):
        for f in glob.glob(os.path.join(base, "*.jsonl")):
            for line in open(f, encoding="utf-8", errors="replace"):
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") == "assistant" and _before_build(d):
                    for part in (d.get("message") or {}).get("content") or []:
                        if isinstance(part, dict) and part.get("type") == "text":
                            n_assist += 1
                            shingles |= _shingles(part.get("text", ""))
    hits = [t["id"] for t in tasks if _shingles(t["goal"]) & shingles]
    check(f"no goal shares an 8-word run with any of {n_assist} real assistant replies", not hits, hits[:5])
    check("no goal contains the synthetic assistant marker, a path or the user name",
          not [t for t in tasks if "zebraquartz" in t["goal"].lower() or me.lower() in t["goal"].lower() or "\\users\\" in t["goal"].lower()])
    src = open(os.path.join(HERE, "evals", "families", "ours.py"), encoding="utf-8").read()
    check("the family reads no transcripts and imports no cloud module", ".claude" not in src and "arch_cloud" not in src and "jsonl" not in src)

    print("\n== judges: reference passes, wrong is caught ==")
    sample = [t for i, t in enumerate(tasks) if i % 4 == 0 or t["template"] in fam.HOLDOUT_TEMPLATES]
    bad = fam.prove(sample, say=lambda s: print(s) if "FAIL" in s else None)
    check(f"reference passes every judge and the wrong answer is caught ({len(sample)} tasks, every template)", bad == 0, bad)

    print("\n== holdout ==")
    hold = [t for t in tasks if "holdout" in t["tags"]]
    train = [t for t in tasks if "holdout" not in t["tags"]]
    check("a couple of whole templates are held out", {t["template"] for t in hold} == set(fam.HOLDOUT_TEMPLATES) and len(fam.HOLDOUT_TEMPLATES) == 2)
    check("holdout and training are disjoint in template, id and goal",
          not ({t["template"] for t in hold} & {t["template"] for t in train}) and not ({t["id"] for t in hold} & {t["id"] for t in train})
          and not ({t["goal"] for t in hold} & {t["goal"] for t in train}))
    check("a held-out template never has a trainable variant, in any seed",
          all(("holdout" in t["tags"]) == (t["template"] in fam.HOLDOUT_TEMPLATES) for s in (1, 2) for t in fam.all_tasks(s)))
    g0 = {t["goal"] for t in fam.all_tasks(0)}
    g1 = {t["goal"] for t in fam.all_tasks(1)}
    check("a fresh seed gives fresh variants; goals distinct within a seed", len(g0 - g1) >= len(g0) // 2 and len(g0) == len(tasks))
    check("tasks(): include_holdout=False drops them; only_seen follows the mined intents",
          all("holdout" not in t["tags"] for t in ot.tasks(include_holdout=False))
          and {t["intent"] for t in ot.tasks(mined=m, only_seen=True)} <= {k for k, v in m["intents"].items() if v["count"]})

    shutil.rmtree(_TMP, ignore_errors=True)
    n = sum(results)
    print(f"\n{n}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(run())
