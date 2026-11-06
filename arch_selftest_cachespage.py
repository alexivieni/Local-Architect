"""Self-test for the hub's Caches page (Models > Caches): static checks on hub_ui.html, then the page's own functions run in node
against fixture ledgers shaped like arch_cachestats.ledger() (the real numbers of 2026-11-05, one with prefix data, one failed read).

    python arch_selftest_cachespage.py

Without node the rendering checks could not run; they are reported as such and not counted as passes.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(")

# what `py -3.12 arch_cachestats.py` returned on this machine (2026-11-05), paths shortened
REAL = {
    "t": 1793860747.0,
    "layers": [
        {"id": "route", "name": "Route decisions", "where": "hub_data/route_cache.json", "entries": 0, "bytes": 0, "hits": None, "misses": None,
         "state": "unused"},
        {"id": "reply", "name": "Local replies", "where": "hub_data/cache/responses.db", "entries": 20, "bytes": 24576, "hits": 9, "misses": 20,
         "saved_tokens": 540, "saved_secs": 0.0, "state": "working"},
        {"id": "cloud", "name": "Claude replies", "where": "hub_data/cloud_cache", "hits": None, "misses": None, "entries": 0, "bytes": 0,
         "state": "unused"},
        {"id": "prefix", "name": "KV prefix (in memory)", "where": "llama-server, per call", "runs": 0, "calls": 0, "prompt_tokens": 0,
         "cached_tokens": 0, "exact_share": None, "rho": None, "reply_hits_in_traces": 0, "by_model": [], "by_flow": [], "hits": None,
         "misses": None, "state": "no data"},
        {"id": "slots", "name": "KV slots on disk", "where": "hub_data/server/slots", "entries": 35, "bytes": 8852632780,
         "oldest": 1793836077.2, "newest": 1793860298.4, "hits": None, "misses": None, "orphans": 22, "orphan_bytes": 5873912888,
         "state": "holding data"},
        {"id": "lookup", "name": "N-gram lookup", "where": "hub_data/server/lookup", "entries": 0, "bytes": 0, "oldest": None, "newest": None,
         "hits": None, "misses": None, "state": "unused"},
        {"id": "claude", "name": "Claude prompt cache", "where": "Anthropic, per call", "hits": None, "misses": None, "state": "working",
         "sources": {
             "app": {"files": 40, "replies": 282, "uncached_input": 560, "cache_read": 31546803, "cache_write": 1498756, "output": 6589,
                     "read_share": 0.955, "usd_paid_est": 19.95, "usd_without_cache_est": 130.9, "usd_saved_est": 110.95,
                     "gaps": {"n": 243, "within_5m": 0.984, "5m_to_1h": 0.016, "over_1h": 0.0}},
             "claude_code": {"files": 306, "replies": 17192, "uncached_input": 63952, "cache_read": 5098755390, "cache_write": 105064637,
                             "output": 21651938, "read_share": 0.98, "usd_paid_est": 3512.16, "usd_without_cache_est": 23779.2,
                             "usd_saved_est": 20267.04, "gaps": {"n": 16910, "within_5m": 0.993, "5m_to_1h": 0.006, "over_1h": 0.0}}}},
    ],
    "findings": [
        {"severity": "info", "text": "Route cache is empty: it is off during benchmarks and self-tests on purpose, so it only fills from real runs "
                                     "and hub route previews. Nothing is wrong; it has simply not been used yet."},
        {"severity": "high", "text": "No local prefix-reuse data yet: run traces from benchmarks were thrown away until 2026-11-05. The next live "
                                     "eval (kept traces) will fill this in; it is the cache that matters most locally (Eq. 15)."},
        {"severity": "high", "text": "22 KV slot files (5.9 GB) belong to thrown-away eval or live-test folders and can never be restored. "
                                     "`python architect.py caches` lists them; `--prune --yes` removes them."},
        {"severity": "medium", "text": "KV slots use 8.9 GB with no cap (a cap is being added: storage.slot_cache_gb)."},
        {"severity": "info", "text": "the app's Claude runs: 96% of input served from the prompt cache over 282 replies; estimated 110.95 USD "
                                     "saved at API prices (paid about 19.95)."},
        {"severity": "info", "text": "Claude Code on this machine: 98% of input served from the prompt cache over 17192 replies; estimated "
                                     "20267.04 USD saved at API prices (paid about 3512.16)."},
    ],
}


def _full():
    d = json.loads(json.dumps(REAL))
    L = {l["id"]: l for l in d["layers"]}
    L["prefix"].update(runs=12, calls=340, prompt_tokens=2400000, cached_tokens=1296000, exact_share=0.9, rho=0.54, state="working",
                       by_flow=[{"name": "bugfix", "calls": 200, "rho": 0.71}, {"name": "build", "calls": 140, "rho": 0.29}],
                       by_model=[{"name": "hf.co/x/qwen3:8b", "calls": 340, "rho": 0.54}])
    L["reply"].update(hits=0, state="stored, never reused")
    L["slots"].update(orphans=0, orphan_bytes=0)
    L["lookup"].update(entries=3, bytes=3000000, state="holding data")
    d["layers"].append({"id": "prefix2", "name": "Other prefix", "state": "not reusing", "entries": 0, "bytes": 0})
    d["findings"] = []
    return d


def run():
    html = open(os.path.join(HERE, "hub_ui.html"), encoding="utf-8").read()
    results, skipped = [], []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({extra})" if not ok and extra else ""))

    a, b = html.find("/* ===== Caches (Models)"), html.find("/* ---- Benchmarks: every saved")
    block = html[a:b] if 0 <= a < b else ""
    check("the page's code is one block in hub_ui.html", bool(block) and "function cachesPage(){" in block and "async function loadCaches(" in block)

    # ---- the place, the icon, the drawing
    import arch_uireg
    reg = arch_uireg.load(os.path.join(HERE, "hub_ui.html"))
    cp = next((p for p in reg["places"] if p["id"] == "cachesp"), {})
    check("Caches is its own place in the Models group, reachable from exactly one place",
          cp.get("icon") == "db" and cp.get("label") == "Caches" and cp.get("pages") == ["caches"]
          and sum(p["pages"].count("caches") for p in reg["places"]) == 1)
    cpage = next((p for p in reg["pages"] if p["id"] == "caches"), None)
    check("it has a navigation entry with a plain description, and a page title",
          bool(cpage) and cpage["label"] == "Caches" and bool(cpage["description"]))
    symbols = set(re.findall(r'<symbol id="i-([a-z0-9-]+)"', html))
    icon = re.match(r"(.*)", cpage["icon"]) if cpage else None
    art = re.search(r'\bcaches:"([\w-]+)"', html[html.find("const PAGE_ART="):html.find("const SEC_ART=")])
    check("it has its own icon from the sprite and the Models drawing", bool(icon) and icon.group(1) in symbols and bool(art) and art.group(1) == "models",
          (icon and icon.group(1), art and art.group(1)))
    check("render() builds it once and reads the caches on every visit",
          'if(page==="caches"){ if(!built.caches){v.innerHTML="";put(v,...cachesPage());built.caches=1;} loadCaches(); return; }' in html)

    # ---- what it reads, and that it only reads
    check("it reads /api/arch/caches, and Refresh asks again past the hub's 60 s cache",
          'api("/api/arch/caches"+(fresh?"?fresh=1":""))' in block and "onclick:()=>loadCaches(true)" in block)
    calls = re.findall(r'\bapi\(([^)]*)\)', block)
    check("information only: one GET, no request body, no write endpoint, no setting or delete",
          len(calls) == 1 and "," not in calls[0] and not re.search(r'method:|"POST"|fetch\(|/api/(arch/)?(config|settings|prune|delete|roles|tiers)|store\.set\(', block),
          calls)
    check("the prune command is shown as text, never run from the page",
          '"python architect.py caches"' in block and '"--prune --yes"' in block and "removes them." in block)

    # ---- colours and keyboard
    css_a, css_b = html.find("/* the Caches page:"), html.find("/* ===== reduced motion")
    reset = html.find("/* ===== design reset (2026-11-04)")
    css = html[css_a:css_b] if 0 <= css_a < css_b else ""
    check("its CSS sits in the design-reset block, before reduced motion", bool(css) and reset < css_a < css_b and ".ca-bar{" in css)
    check("theme tokens only: no literal colour in its code or its CSS", bool(block and css) and not HEX.findall(block + css), HEX.findall(block + css)[:4])
    check("every control is a real button with a spoken name; nothing else takes a click",
          'h("button",{type:"button",id:"caRefresh",class:"btn sm","aria-label":"Read the caches again"' in block
          and block.count("onclick:") == 1 and "addEventListener" not in block)
    check("the funnel scrolls in its own focusable box with a name, and the drawing has a text alternative",
          'class:"ca-scroll",tabindex:"0",role:"region","aria-label":' in block and 'role="img" aria-label="${escx(label)}"' in block
          and "min-width:520px" in block and ".ca-scroll:focus-visible{" in css)
    check("bars say their split in words to a screen reader, swatches are hidden from it",
          'role:"img","aria-label":label+": "' in block and '"aria-hidden":"true"' in block)

    # ---- every script on the page still parses
    node = shutil.which("node")
    d = tempfile.mkdtemp(prefix="archcp_")
    try:
        if not node:
            skipped.append("parse")
            print("  ----  every <script> block parses   (could not run: node is not installed)")
        else:
            bad = []
            for i, body in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S)):
                p = os.path.join(d, f"s{i}.js")
                with open(p, "w", encoding="utf-8") as f:
                    f.write(body.replace('"__HUB_TOKEN__"', '"x"'))
                r = subprocess.run([node, "--check", p], capture_output=True, text=True, encoding="utf-8", timeout=60)
                if r.returncode:
                    bad.append((i, r.stderr[-200:]))
            check("every <script> block parses (node --check)", not bad, bad)

        # ---- the page's functions, run on the fixtures
        if not node:
            skipped.append("render")
            print("  ----  the page renders the fixture ledgers   (could not run: node is not installed)")
        else:
            esc = re.search(r"const escx=.*", html).group(0) + "\n" + re.search(r"const cmName=.*", html).group(0)
            script = r'''"use strict";
const h=(tag,attrs,...kids)=>({tag,attrs:attrs||{},kids:kids.flat(2).filter(k=>k!=null&&k!==false)});
const T=e=>e==null?"":typeof e!=="object"?String(e):e.kids.map(T).join("");
const all=(e,f,acc=[])=>{if(e&&typeof e==="object"){if(f(e))acc.push(e);e.kids.forEach(k=>all(k,f,acc));}return acc;};
''' + esc + r'''
const when=ts=>ts?"WHEN":"–";
const els={};
const mk=id=>({id,kids:[],disabled:false,attrs:{},set innerHTML(v){this.kids=[];},append(...k){this.kids.push(...k);},
  setAttribute(k,v){this.attrs[k]=v;},removeAttribute(k){delete this.attrs[k];}});
const $=s=>els[s.slice(1)]||null;
const FIX={real:''' + json.dumps(REAL) + r''',full:''' + json.dumps(_full()) + r''',broken:{layers:[],findings:[],error:"disk unreadable"}};
let paths=[],cur="real";
const api=async p=>{paths.push(p);return JSON.parse(JSON.stringify(FIX[cur]));};
''' + block + r'''
const wrap=k=>({tag:"div",attrs:{},kids:k});
(async()=>{
  const o={};
  const page=cachesPage();o.pageButtons=all(wrap(page),e=>e.tag==="button").map(e=>[T(e),e.attrs.type,e.attrs["aria-label"]]);
  for(const name of ["real","full","broken"]){
    cur=name;els.caOut=mk("caOut");els.caRefresh=mk("caRefresh");
    await loadCaches(name==="full");
    const root=wrap(els.caOut.kids);
    const svgBox=all(root,e=>e.attrs&&typeof e.attrs.html==="string")[0]||null;
    const svg=svgBox?svgBox.attrs.html:"";
    const rows=svg.split("<g><title>").slice(1);
    o[name]={text:T(root),buttons:all(root,e=>e.tag==="button").length,
      box:svgBox?[svgBox.attrs.class,svgBox.attrs.tabindex,svgBox.attrs.role,svgBox.attrs["aria-label"]]:null,
      svgLabel:(svg.match(/role="img" aria-label="([^"]*)"/)||[])[1]||"",
      rows:rows.map(r=>({name:(r.match(/font-weight="600" fill="var\(--ink\)">\d+\. ([^<]*)</)||[])[1],fill:(r.match(/rect [^>]*fill="([^"]+)" stroke="var\(--line\)"/)||[])[1],
        chip:(r.match(/text-anchor="middle" font-size="12" fill="[^"]+">([^<]*)</)||[])[1],
        key:(r.match(/font-size="13" font-weight="600" fill="var\(--ink\)">([^<]*)</)||[])[1],
        what:(r.match(/y="[\d.]+" font-size="12" fill="var\(--mute\)">([^<]*)</)||[])[1]})),
      finds:all(root,e=>e.tag==="li").map(li=>[li.kids[0].attrs.class,T(li.kids[0]),all(li,e=>e.tag==="code").map(T)]),
      bars:all(root,e=>e.attrs&&/\bca-bar\b/.test(e.attrs.class||"")).map(b=>[b.attrs.class,b.attrs["aria-label"],b.kids.map(s=>[s.attrs.class,parseFloat((s.attrs.style||"").replace("width:",""))])]),
      tables:all(root,e=>e.tag==="table").map(t=>all(t,e=>e.tag==="tr").map(r=>r.kids.map(T))),
      notes:all(root,e=>/\bnote\b/.test(e.attrs&&e.attrs.class||"")).map(T),
      refreshFree:!els.caRefresh.disabled&&!("aria-busy" in els.caRefresh.attrs)};
  }
  o.paths=paths;
  console.log(JSON.stringify(o));
})().catch(e=>console.log(JSON.stringify({error:String(e&&e.stack||e)})));
'''
            p = os.path.join(d, "caches.js")
            with open(p, "w", encoding="utf-8") as f:
                f.write(script)
            r = subprocess.run([node, p], capture_output=True, text=True, encoding="utf-8", timeout=60)
            try:
                o = json.loads(r.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                o = {"error": (r.stdout + r.stderr)[-400:]}
            check("render: the page's functions run on all three fixtures (node)", "error" not in o and all(k in o for k in ("real", "full", "broken")),
                  o.get("error"))
            if "error" not in o:
                R, F, B = o["real"], o["full"], o["broken"]
                check("render: one read per visit, Refresh adds ?fresh=1, and the button is usable again after the read",
                      o["paths"] == ["/api/arch/caches", "/api/arch/caches?fresh=1", "/api/arch/caches"] and R["refreshFree"] and B["refreshFree"], o["paths"])
                check("render: the only button is Refresh (type button, spoken name); the drawn page has none",
                      o["pageButtons"] == [["Refresh", "button", "Read the caches again"]] and R["buttons"] == 0 and F["buttons"] == 0, o["pageButtons"])
                names = [x["name"] for x in R["rows"]]
                check("funnel: the seven layers top to bottom, in the order a request meets them",
                      names == ["Route decisions", "Local replies", "Claude replies", "KV prefix (in memory)", "KV slots on disk", "N-gram lookup",
                                "Claude prompt cache"], names)
                check("funnel: it scrolls in its own named box, and its text alternative names every layer",
                      R["box"] and R["box"][:3] == ["ca-scroll", "0", "region"] and R["svgLabel"].startswith("How one request falls through")
                      and all(f"{i + 1}. {n}" in R["svgLabel"] for i, n in enumerate(names)), R["svgLabel"][:200])
                tone = {x["name"]: (x["chip"], x["fill"]) for x in R["rows"]}
                check("funnel: working is the good tone, unused and no data are neutral, orphans are a warning",
                      tone["Local replies"] == ("working", "var(--goodbg)") and tone["Route decisions"] == ("unused", "var(--soft)")
                      and tone["KV prefix (in memory)"] == ("no data", "var(--soft)") and tone["KV slots on disk"] == ("holding data, orphans", "var(--warnbg)")
                      and tone["Claude prompt cache"] == ("working", "var(--goodbg)"), tone)
                ftone = {x["name"]: (x["chip"], x["fill"]) for x in F["rows"]}
                check("funnel: stored-never-reused and not-reusing are warnings; holding data without orphans is neutral",
                      ftone["Local replies"] == ("stored, never reused", "var(--warnbg)") and ftone["Other prefix"] == ("not reusing", "var(--warnbg)")
                      and ftone["KV slots on disk"] == ("holding data", "var(--soft)") and ftone["KV prefix (in memory)"][1] == "var(--goodbg)", ftone)
                key = {x["name"]: x["key"] for x in R["rows"]}
                check("funnel: each layer's key number, rounded (hit rate, size, orphans, read share)",
                      key["Local replies"] == "31% reused (9 of 29 asks), 20 entries, 25 KB" and key["KV slots on disk"] == "35 files, 8.9 GB, 22 orphaned (5.9 GB)"
                      and key["Claude prompt cache"] == "the app 96%, Claude Code 98% of input read from cache" and key["KV prefix (in memory)"] == "no traced calls yet"
                      and key["Route decisions"] == "0 entries, 0 MB", key)
                fkey = {x["name"]: x["key"] for x in F["rows"]}
                check("funnel: with traced calls the prefix row shows rho", fkey["KV prefix (in memory)"] == "54% of prompt tokens read from cache (rho) over 340 calls", fkey)
                check("funnel: every layer has a short plain-words line",
                      [x["what"] for x in R["rows"]][0] == "a router decision reused, no model call" and all(x["what"] and len(x["what"]) <= 80 for x in R["rows"]))
                check("findings: all six, with severity chips (high = bad, medium = warn, info = info) and commands as code",
                      [f[:2] for f in R["finds"]] == [["chip info", "info"], ["chip bad", "high"], ["chip bad", "high"], ["chip warn", "medium"],
                                                      ["chip info", "info"], ["chip info", "info"]]
                      and R["finds"][2][2] == ["python architect.py caches", "--prune --yes"], R["finds"])
                t = R["text"]
                check("Claude: both ends side by side, each with its read share and replies",
                      "The app's Claude runs96% of input read from cache, over 282 replies." in t
                      and "Claude Code on this machine98% of input read from cache, over 17,192 replies." in t, t[:400])
                check("Claude: dollars saved, rounded, labelled estimates; the subscription caveat for Claude Code",
                      "About 111 USD saved at API prices (an estimate; paid about 19.95 USD)." in t
                      and "About 20,267 USD saved at API prices (an estimate; Claude Code on a subscription is not billed per token)." in t,
                      re.findall(r"About [^.]*\.[^.]*\.", t))
                inp = [x for x in R["bars"] if "ca-input" in x[0]]
                gap = [x for x in R["bars"] if "ca-gaps" in x[0]]
                check("Claude: the input bar splits read / write / uncached and adds up to the whole",
                      len(inp) == 2 and all([s[0] for s in x[2]] == ["ca-read", "ca-write", "ca-unc"] and abs(sum(s[1] for s in x[2]) - 100) < 0.1 for x in inp)
                      and inp[1][1] == "Input tokens: read from cache 98%, written to cache 2%, not cached 0%", inp)
                check("Claude: the pause profile is three parts (within 5 min / 5-60 min / over 1 h) with the 1-hour sentence",
                      len(gap) == 2 and gap[0][1] == "Pauses between replies: within 5 min 98%, 5-60 min 2%, over 1 h 0%"
                      and "The 1-hour cache lifetime pays when the middle share (5-60 min) is large; here it is 2%." in t, gap)
                check("prefix: with no data it shows the finding's sentence (filled in by the next live eval)",
                      "Local KV prefix" in t and "No local prefix-reuse data yet" in t and "next live eval" in t[t.find("Local KV prefix"):])
                tabs_f = F["tables"]
                check("prefix: with data, rho overall and a table by workflow and by model",
                      "54%of prompt tokens read from cache (rho)" in F["text"] and ["bugfix", "200", "71%"] in tabs_f[0] and ["build", "140", "29%"] in tabs_f[0]
                      and ["x/qwen3:8b", "340", "54%"] in tabs_f[1], tabs_f[:2])
                disk = R["tables"][-1]
                check("disk stores: slots, lookup, reply database and Claude reply cache with size, dates and orphans",
                      [r[0].split("hub_data")[0] for r in disk[1:]] == ["KV slots on disk", "N-gram lookup", "Local reply database", "Claude reply cache"]
                      and disk[1][1:] == ["35", "8.9 GB", "WHEN", "WHEN", "22 files, 5.9 GB"] and disk[2][1:] == ["0", "0 MB", "–", "–", "–"]
                      and disk[3][1:3] == ["20", "25 KB"], disk)
                check("disk stores: the CLI note is there as text", "python architect.py caches lists them; --prune --yes removes them." in t)
                check("a failed read says so in words and draws nothing else",
                      B["notes"] == ["Could not read every cache: disk unreadable"] and "No cache figures yet." in B["text"] and not B["rows"], B)
    finally:
        shutil.rmtree(d, ignore_errors=True)

    n = sum(results)
    tail = f" ({len(skipped)} could not run: node is not installed)" if skipped else ""
    print(f"\n  {'ALL ' + str(n) + ' CACHES PAGE CHECKS PASSED' + tail if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' CACHES PAGE CHECKS FAILED'}")
    return n == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
