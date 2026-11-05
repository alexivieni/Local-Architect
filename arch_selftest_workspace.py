"""Self-tests for the agents' workspace in the hub: Fleet, Map, Logbook, Library, Monitor and the price of a Claude message.

What is proven here (no Claude, no network, no model): the hub's endpoints in architect_hub.py turn the backend modules' data into
what the pages draw, refuse what they must refuse, keep "could not run" as its own outcome, and survive a missing module; the page in
hub_ui.html has the five pages wired into the four places, its second script block parses (node --check, when node is installed),
and the new styles use the theme's tokens only. Runs against a throwaway data folder and a throwaway project.

    python arch_selftest_workspace.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="arch_ws_")
os.environ["ARCHITECT_DATA_DIR"] = os.path.join(TMP, "data")
os.environ["ARCHITECT_NO_CACHE"] = "1"
os.environ["ARCHITECT_SELFTEST"] = "1"          # arch_cloud.allowed() then refuses every Claude request: the price stays an estimate
os.makedirs(os.environ["ARCHITECT_DATA_DIR"], exist_ok=True)
sys.path.insert(0, HERE)

import architect_hub as hub  # noqa: E402

RESULTS = []


def check(name, ok, detail=None):
    RESULTS.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok or detail is None else f"\n      -> {str(detail)[:400]}"))


def raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except ValueError as e:
        return str(e)
    return None


def write_jsonl(path, recs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in recs:
            f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")


def main():
    data = os.environ["ARCHITECT_DATA_DIR"]
    proj = os.path.join(TMP, "proj")
    os.makedirs(proj)

    # ---- plain JSON: whatever a module returns must reach _send
    p = hub._plain({"s": {"b", "a"}, "o": object})
    check("_plain turns sets into sorted lists and odd objects into strings", p["s"] == ["a", "b"] and isinstance(p["o"], str), p)

    # ---- Fleet: the table rows -> the plan, and the check that spends nothing
    rows = [{"name": "ui", "kind": "claude", "brief": "Do the UI.", "files": "hub_ui.html, docs/ui.md", "budget": 3},
            {"name": "loc", "kind": "local", "brief": "Fix the parser.", "files": "parser.py\nparser_test.py", "budget": ""}]
    plan = hub.fleet_plan_from(rows)
    check("a row's files may be one comma or newline separated string",
          plan[0]["files"] == ["hub_ui.html", "docs/ui.md"] and plan[1]["files"] == ["parser.py", "parser_test.py"], plan)
    check("a local agent's brief becomes its goal, a Claude agent's stays its brief and gets accept-edits",
          plan[1].get("goal") == "Fix the parser." and "brief" not in plan[1] and plan[0]["brief"] == "Do the UI." and plan[0]["mode"] == "acceptEdits", plan)
    ok = hub.fleet_check({"check": rows, "cap": 10, "tests": "python arch_hub_check.py"})
    check("a sound plan passes the check, with the caps added up (a local agent costs nothing)",
          ok["ok"] and ok["problems"] == [] and ok["total"] == 3.0 and ok["agents"] == 2, ok)
    clash = hub.fleet_check({"check": rows + [{"name": "two", "brief": "x", "files": "docs", "budget": 1}], "cap": 10})
    check("two agents owning the same file (a folder and a file in it) are refused in plain words",
          not clash["ok"] and any("both own" in x for x in clash["problems"]), clash)
    bad_t = hub.fleet_check({"check": rows, "cap": 10, "tests": "rm -rf /"})
    check("a test command outside the allowed ones is refused", any("not an allowed test command" in x for x in bad_t["problems"]), bad_t)
    left = hub.fleet_check({"check": [{"name": "a", "brief": "Change {module}.", "files": "{module}.py", "budget": 1}], "cap": 5})
    check("a template slot left unfilled is a problem before anything is spent", any("fill in {module}" in x for x in left["problems"]), left)
    over = hub.fleet_check({"check": rows, "cap": 1})
    check("caps that add up past the fleet cap are refused", any("above the cap" in x for x in over["problems"]), over)
    check("a cap of 0 or above the hub's maximum is refused", raises(hub.fleet_check, {"check": rows, "cap": 0}) and raises(hub.fleet_check, {"check": rows, "cap": 10 ** 6}))
    msg = raises(hub.fleet_change, {"launch": rows + [{"name": "ui", "brief": "y", "files": "z.py", "budget": 1}], "cap": 10, "root": proj})
    fleet_dir = os.path.join(data, "fleet")
    check("launching a refused plan starts nothing and writes no fleet", msg and "not launched" in msg and not os.path.isdir(fleet_dir), msg)
    check("stop, check or launch must be said", raises(hub.fleet_change, {}) is not None)
    info = hub.fleet_info()
    check("the fleet list answers on a fresh install (empty, with the allowed test prefixes)",
          info.get("available") and info["fleets"] == [] and info["allowed"] and info["cap_max"] == hub.FLEET_CAP_MAX, info)
    check("a fleet id that is not one is answered with an error, not a crash", "error" in hub.fleet_info("../../x"), hub.fleet_info("../../x"))

    fid = "fl1730000000000"
    os.makedirs(fleet_dir, exist_ok=True)
    with open(os.path.join(fleet_dir, fid + ".json"), "w", encoding="utf-8") as f:
        json.dump({"id": fid, "root": proj, "created": time.time() - 60, "ended": time.time(), "phase": "done", "cap_usd": 10,
                   "tests_planned": ["python arch_a.py", "python arch_b.py"], "verdict": "not_sure", "diffstat": " a.py | 2 +-",
                   "strays": ["c.py"], "tests": [{"cmd": "python arch_a.py", "ok": True, "rc": 0, "seconds": 1.0, "output": "ok"},
                                                 {"cmd": "python arch_b.py", "ok": None, "rc": None, "seconds": 0, "output": "timed out"}],
                   "agents": [{"name": "ui", "kind": "claude", "status": "done", "cost": 0.4, "budget_usd": 3, "files": ["a.py"], "run": None,
                               "last_event": {"type": "result", "text": "done"}},
                              {"name": "docs", "kind": "local", "lane": "gpu", "status": "error", "cost": 0, "budget_usd": 0,
                               "files": ["docs/"], "after": ["ui"], "error": "boom"}]}, f)
    st = hub.fleet_info(fid)
    check("one fleet's status reaches the page: agents, total cost, tests, diffstat",
          len(st.get("agents") or []) == 2 and st["total_cost"] == 0.4 and len(st["tests"]) == 2 and st["diffstat"], st)
    g = hub.map_graph("fleet", fid)
    types = {n["type"] for n in g.get("nodes", [])}
    check("a fleet maps to the request, its agents, Claude and the worker, the files they own, the tests and a verdict",
          {"request", "agent", "claude", "tier", "file", "check", "verdict"} <= types, (types, g.get("error")))
    unsure = next((n for n in g["nodes"] if n["type"] == "check" and n["detail"].get("command") == "python arch_b.py"), {})
    check("a test that could not run keeps ok None on the map (never dropped, never a failure)",
          "ok" in unsure.get("detail", {}) and unsure["detail"]["ok"] is None, unsure)
    ed = {(e["from"], e["to"]) for e in g["edges"]}
    check("an agent that waits for another is drawn after it, not from the request",
          ("agent:ui", "agent:docs") in ed and ("req", "agent:docs") not in ed and ("req", "agent:ui") in ed, sorted(ed))

    # ---- Map: a run of the project, from the trace the engine writes
    rd = os.path.join(proj, ".arch", "runs", "20261104-r1")
    os.makedirs(rd)
    with open(os.path.join(rd, "report.md"), "w", encoding="utf-8") as f:
        f.write("# Run\n- Flow: `bugfix`\n- Goal: fix paginate() page 1\n")
    write_jsonl(os.path.join(rd, "trace.jsonl"), [
        {"type": "step", "step": "reproduce", "status": "started"},
        {"type": "call", "id": 1, "role": "coder", "model": "qwen:7b", "tier": "worker", "prompt_tokens": 100, "gen_tokens": 20, "usd": 0,
         "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "write a failing test"}], "reply": "FILE: t.py"},
        {"type": "call", "id": 2, "role": "coder", "model": "qwen:7b", "tier": "worker", "prompt_tokens": 50, "gen_tokens": 10},
        {"type": "tool", "step": "reproduce", "tool": "apply", "args": {"path": "t.py"}, "ok": True, "result": "applied"},
        {"type": "check", "step": "reproduce", "gate": "tests_fail", "ok": False, "text": "1 failed"},
        {"type": "step", "step": "reproduce", "ok": True, "changed": ["t.py"], "tries": 1},
        {"type": "step", "step": "fix", "status": "started"},
        {"type": "call", "id": 3, "role": "fixer", "model": "claude:claude-opus-5-5", "tier": "claude", "prompt_tokens": 900, "gen_tokens": 80, "usd": 0.02},
        {"type": "check", "step": "fix", "gate": "tests", "ok": None, "text": "could not run"},
        "not json",
        {"type": "verdict", "verdict": "not_sure", "why": "a check could not run"}])
    g = hub.map_graph("run", "20261104-r1", proj)
    by = {n["id"]: n for n in g.get("nodes", [])}
    check("a run maps to its request (with the goal), its steps in order and a verdict",
          by.get("req", {}).get("detail", {}).get("prompt") == "fix paginate() page 1" and "step:reproduce" in by and "step:fix" in by
          and by.get("verdict", {}).get("label") == "Verdict: not_sure", (g.get("error"), list(by)))
    ed = [(e["from"], e["to"]) for e in g["edges"]]
    check("the edges go in order: request -> first step -> next step -> verdict",
          ("req", "step:reproduce") in ed and ("step:reproduce", "step:fix") in ed and ("step:fix", "verdict") in ed, ed)
    w = by.get("tier:worker", {}).get("detail", {})
    check("a tier box adds up its calls and tokens; Claude gets its own box with its cost",
          w.get("calls") == 2 and w.get("tokens_in") == 150 and by.get("tier:claude", {}).get("detail", {}).get("cost") == 0.02, (w, by.get("tier:claude")))
    s1 = by["step:reproduce"]["detail"]
    check("a step box carries the prompt it sent and the reply, for the details panel",
          s1.get("prompt") == "write a failing test" and s1.get("reply") == "FILE: t.py" and s1.get("ok") is True, s1)
    check("tools, edited files and checks hang off their step; a failed and an unrunnable check stay apart",
          ("step:reproduce", "tool:apply") in ed and ("step:reproduce", "file:t.py") in ed
          and by.get("check:step:reproduce:tests_fail", {}).get("detail", {}).get("ok") is False
          and by.get("check:step:fix:tests", {}).get("detail", {}).get("ok", "missing") is None, ed)
    check("a run id that climbs out of the runs folder is refused", "error" in hub.map_graph("run", "../../etc", proj))
    check("a run that does not exist says so", "no such run" in hub.map_graph("run", "nope", proj).get("error", ""))
    check("an unknown map kind is refused", "error" in hub.map_graph("banana", "x"))
    big = os.path.join(proj, ".arch", "runs", "big")
    os.makedirs(big)
    write_jsonl(os.path.join(big, "trace.jsonl"), [{"type": "step", "step": "s", "status": "started"}]
                + [{"type": "tool", "step": "s", "tool": f"t{i}", "args": {}} for i in range(300)])
    gb = hub.map_graph("run", "big", proj)
    check("a huge run is cut at the node limit and says it was cut", len(gb["nodes"]) <= hub.MAP_MAX_NODES and gb.get("truncated"), len(gb.get("nodes", [])))

    flows = sorted(n[:-5] for n in os.listdir(os.path.join(HERE, "flows")) if n.endswith(".yaml"))
    gf = hub.map_graph("flow", "bugfix" if "bugfix" in flows else flows[0])
    tf = [n["type"] for n in gf.get("nodes", [])]
    check("a workflow maps to the request, its steps, the tiers that run them and a verdict",
          tf[:1] == ["request"] and "step" in tf and "verdict" in tf, (gf.get("error"), tf))
    check("a workflow id with odd characters is refused", "error" in hub.map_graph("flow", "Bad!id"))

    ccd = os.path.join(data, "cc_runs")
    os.makedirs(ccd)
    with open(os.path.join(ccd, "cc1730000000001.meta.json"), "w", encoding="utf-8") as f:
        json.dump({"id": "cc1730000000001", "prompt": "Tidy the parser", "status": "done", "cost": 0.12, "turns": 4, "model": "sonnet",
                   "started": time.time()}, f)
    write_jsonl(os.path.join(ccd, "cc1730000000001.jsonl"), [
        {"type": "assistant", "text": "ok", "usage": {"input_tokens": 300, "output_tokens": 40}},
        {"type": "tool", "id": "t1", "name": "Read", "arg": "parser.py", "text": json.dumps({"file_path": "parser.py"})},
        {"type": "tool", "id": "t2", "name": "Edit", "arg": "parser.py", "text": json.dumps({"file_path": "parser.py", "old_string": "a"})},
        {"type": "result", "text": "Done: tidied."}])
    gc = hub.map_graph("cc", "cc1730000000001")
    bc = {n["id"]: n for n in gc.get("nodes", [])}
    check("a Claude Code run maps to the request, the agent, Claude (tokens and cost), its tools and the file it edited",
          bc.get("req", {}).get("detail", {}).get("prompt") == "Tidy the parser" and "tool:Edit" in bc and "file:parser.py" in bc
          and bc.get("tier:claude", {}).get("detail", {}).get("tokens_in") == 300 and bc["tier:claude"]["detail"].get("cost") == 0.12,
          (gc.get("error"), list(bc)))
    check("a file only read is not drawn as edited", not any(e["from"] == "tool:Read" for e in gc["edges"]), gc["edges"])

    src = hub.map_sources(proj)
    check("the Map's picker lists the project's runs, the fleets, the workflows and the Claude Code runs",
          any(r["id"] == "20261104-r1" for r in src["runs"]) and any(f["id"] == fid for f in src["fleets"]) and src["flows"]
          and any(c["id"] == "cc1730000000001" for c in src["cc"]), src)

    # ---- Logbook
    lb = hub.logbook_info()
    check("the logbook answers with its stats on a fresh install", lb.get("available") and isinstance(lb.get("stats"), dict), lb)
    check("the logbook search answers with a list", isinstance(hub.logbook_info("fix paginate page").get("similar"), list))
    import arch_learn
    real = arch_learn.ingest
    arch_learn.ingest = lambda *a, **k: {"added": 2, "updated": 1, "skipped": 0, "files": 3}
    try:
        ing = hub.logbook_ingest()
    finally:
        arch_learn.ingest = real
    check("Ingest now reads, rebuilds the playbooks and hands back fresh stats",
          ing.get("ok") and ing["ingested"]["added"] == 2 and "stats" in ing and isinstance(ing.get("playbooks"), str), ing)

    # ---- Library
    li = hub.library_info()
    agents = [t for t in li.get("templates") or [] if t.get("kind") == "agent"]
    check("the library lists the agent templates", li.get("available") and agents, li.get("error"))
    if agents:
        one = hub.library_info(agents[0]["name"])
        t = one.get("template") or {}
        check("one template comes with its explanation, and never with its path on disk",
              one.get("explain") and "path" not in t and t.get("body"), one.get("error"))
        vals = {k: v for k, v in (t.get("examples") or {}).items()}
        try:
            row = hub.library_fill({"fill": t["name"], "values": vals})
            check("Use in a fleet fills a Fleet row: name, brief, files as text, cap, mode",
                  row["row"]["brief"] and isinstance(row["row"]["files"], str) and row["row"]["budget"] > 0 and isinstance(row["tests"], list), row)
        except ValueError as e:
            check("Use in a fleet fills a Fleet row: name, brief, files as text, cap, mode", False, e)
        needs = [k for k in t.get("placeholders") or [] if k != "app" and k not in (t.get("defaults") or {})]
        if needs:
            check("a slot left empty is refused in plain words", "fill in" in (raises(hub.library_fill, {"fill": t["name"], "values": {}}) or ""))
    check("an unknown template is answered with an error", "error" in hub.library_info("no_such_template"))
    check("a footer template cannot be put in a fleet", raises(hub.library_fill, {"fill": "common", "values": {}}) is not None)

    # ---- Monitor
    mo = hub.monitor_info()
    check("the monitor answers: agents, totals, conflicts, timeline and lessons, all plain JSON",
          mo.get("available") and isinstance(mo.get("agents"), list) and isinstance(mo.get("totals"), dict) and isinstance(mo.get("timeline"), list)
          and isinstance(mo.get("lessons"), list) and json.loads(json.dumps(mo)) == mo, mo.get("error"))
    check("each monitor row carries what the page shows (status, cost, tools, files edited, stalls)",
          all({"name", "status", "cost", "tools_total", "files_edited", "stalls", "errors"} <= set(a) for a in mo["agents"]), mo["agents"][:1])

    # ---- the price of a Claude message
    tp = hub.token_preview({"model": "claude:claude-opus-5-5", "message": "x" * 3500, "ctx": "small"})
    check("a Claude message is priced before it is sent, labelled as an estimate while online is off (always so in the tests)",
          tp.get("available") and tp["usd"] > 0 and tp["line"].startswith("about $") and tp["source"] == "estimate" and tp["context_tokens"] == 1000, tp)
    big_ctx = hub.token_preview({"model": "claude-opus-5-5", "message": "x" * 3500, "ctx": "max"})
    check("more project context costs more", big_ctx["usd"] > tp["usd"], (tp, big_ctx))
    check("the Council warning is added when the Council would convene", "several times" in hub.token_preview({"model": "claude-opus-5-5", "message": "hi", "council": True})["line"])
    check("a model name that is not Claude's is refused", raises(hub.token_preview, {"model": "gpt-9", "message": "hi"}) is not None)

    # ---- a missing module: the hub keeps answering
    real_mod = hub._mod

    def gone(name):
        raise ValueError(f"{name}.py is not in this install (ImportError)")
    hub._mod = gone
    try:
        miss = [hub.fleet_info(), hub.logbook_info(), hub.library_info(), hub.monitor_info(), hub.token_preview({"message": "hi"})]
    finally:
        hub._mod = real_mod
    check("every page answers 'not available' when its module is missing, instead of failing",
          all(m.get("available") is False and m.get("error") for m in miss), miss)

    # ---- over HTTP, through the hub's own handler
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), hub.AHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path, body=None, token=True):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **({"X-Hub-Token": hub.mh.Hub.token} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")
    try:
        codes = [call(p)[0] for p in ("/api/arch/fleet", "/api/arch/monitor", "/api/arch/library", "/api/arch/logbook",
                                      "/api/arch/map_sources?root=" + urllib.request.quote(proj),
                                      "/api/arch/map?kind=run&id=20261104-r1&root=" + urllib.request.quote(proj))]
        check("every workspace page's data is served over HTTP", codes == [200] * 6, codes)
        c, b = call("/api/arch/fleet", {"check": rows, "cap": 10, "tests": ""})
        check("the plan check is served over HTTP and spends nothing", c == 200 and b.get("ok"), b)
        c, b = call("/api/arch/fleet", {"launch": [{"name": "x"}], "cap": 10})
        check("a refused launch comes back as 400 with the problems in plain words", c == 400 and "not launched" in b.get("error", ""), b)
        c, b = call("/api/arch/fleet", {"check": rows, "cap": 10}, token=False)
        check("a change without the hub's token is refused", c == 403, b)
        c, b = call("/api/arch/token_preview", {"model": "claude-opus-5-5", "message": "hello"})
        check("the price of a message is served over HTTP", c == 200 and b.get("available"), b)
    finally:
        srv.shutdown()

    # ---- the page
    html = open(os.path.join(HERE, "hub_ui.html"), encoding="utf-8").read()
    tabs = re.search(r"const TABS=\[(.*?)\];", html, re.S).group(1)
    for pid in ("fleet", "monitor", "map", "library", "logbook"):
        check(f"the {pid} page sits in exactly one of the four places and has a nav entry and a name",
              len(re.findall(r'"%s"' % pid, tabs)) == 1 and re.search(r'\["%s","[^"]*","[^"]*","[^"]*"\]' % pid, html) and re.search(r"\b%s:\"" % pid, html))
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    ws = next((b for b in blocks if "function fleetPage()" in b), "")
    check("the workspace lives in its own script block, after the main one", ws and blocks.index(ws) > 0 and "function render()" not in ws)
    render = re.search(r"function render\(\)\{(.*?)\n\}", html, re.S)
    check("render() looks the workspace pages up only when one is opened (they load after the first render)",
          render and "()=>fleetPage()" in render.group(1) and ",fleetPage]" not in render.group(1))
    check("the Console has the price chip, hidden until a Claude message is typed", 'id:"cCost",hidden:true' in html and "function tokPreview()" in ws)
    check("the map drags with pointer events, keeps positions per item and moves with the arrow keys",
          "setPointerCapture" in ws and 'MAP.key="archMap:"' in ws and "ArrowLeft" in ws and "localStorage" not in ws.replace("store.", ""))
    check("the workspace block loads nothing from anywhere (only the SVG namespace is a URL)",
          set(re.findall(r"https?://[^\s\"'`)]+", ws)) <= {"http://www.w3.org/2000/svg"}, re.findall(r"https?://[^\s\"'`)]+", ws))
    css = html[html.find("/* ===== 2026-11-04: the agents' workspace"):html.find("</style>")]
    check("the new styles use the theme's tokens, so dark and light both work (no fixed colours)",
          css and not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css), re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)", css)[:5])
    check("the Fleet table stacks on a phone and the map details fold at 480 px",
          "@media (max-width:760px){.fl-row{" in css and "@media (max-width:480px){.mp-detail dl" in css)
    node = shutil.which("node")
    if node:
        bad = []
        for i, b in enumerate(blocks):
            pth = os.path.join(TMP, f"block{i}.js")
            with open(pth, "w", encoding="utf-8") as f:
                f.write(b.replace('"__HUB_TOKEN__"', '"x"'))
            r = subprocess.run([node, "--check", pth], capture_output=True, text=True)
            if r.returncode:
                bad.append((i, r.stderr[-400:]))
        check("every script block on the page parses (node --check)", not bad, bad)
        decl = {}
        for i, b in enumerate(blocks):
            for m in re.finditer(r"^(?:const|let|var|function|async function|class)\s+([A-Za-z_$][\w$]*)", b, re.M):
                decl.setdefault(m.group(1), set()).add(i)
        dup = sorted(k for k, v in decl.items() if len(v) > 1)
        check("no top-level name is declared in two script blocks (a second const would stop the whole block)", not dup, dup)
    else:
        print("SKIP node is not installed: the script blocks were not parsed")

    n_ok = sum(1 for _, ok in RESULTS if ok)
    shutil.rmtree(TMP, ignore_errors=True)
    if n_ok != len(RESULTS):
        print(f"\n{len(RESULTS) - n_ok} of {len(RESULTS)} WORKSPACE CHECKS FAILED")
        return 1
    print(f"\nALL {len(RESULTS)} WORKSPACE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
