"""Self-test: every hub page opens, and pressing the buttons on the key pages breaks nothing (a real browser, a throwaway install).

The 2026-11-09 crawl of all 733 buttons found what text checks could not: a "Run checks" that answered from a 60 s cache, empty
"Add"/"Install" presses that did nothing visible, Recent runs that never refreshed after a run. This suite keeps that crawl running:

  - one real run first (the scripted stand-in model), so the run pages have something in them
  - every page opens without an uncaught page error
  - on the key pages every enabled button is pressed: no uncaught error, no frozen page, and a press that saved nothing never shows a
    script error ("Cannot read properties of undefined ...") in a toast
  - the hub logs no traceback and no HTTP 500

Safety: every non-GET request the page makes is answered inside the page and never sent ({"ok":true,"intercepted":true}); window.open,
links, confirm/alert/prompt are recorded and blocked. So no button can change anything outside the throwaway home and data folders
(no desktop shortcut, no download, no explorer window, no second run). ARCHITECT_CRAWL_ALL=1 presses every button on every page
(about 30 minutes); the default covers the key pages. No Edge or Chrome installed -> every check "could not run" (NOT_SURE).
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

KEY_PAGES = ("overview", "run", "review", "queue", "doctor", "finops", "plugins")
BACKGROUND = ("/api/arch/gpu", "/api/arch/ollama", "/api/job", "/api/state", "/api/arch/live", "/api/arch/version", "/api/arch/usage",
              "/api/arch/events", "/api/arch/first_visit", "/api/arch/eta")
CLIENT_GONE = re.compile(r"(ConnectionAbortedError|ConnectionResetError|BrokenPipeError)\b")
SCRIPT_ERROR = re.compile(r"Cannot read propert|is not a function|is not defined|is not iterable|undefined \(reading")

INTERCEPT = r"""(()=>{ if(window.__crawl)return 'already';
 window.__crawl={req:[],opened:[],nav:[],dlg:[]};
 const of=window.fetch.bind(window);
 window.fetch=(u,o)=>{const m=((o&&o.method)||'GET').toUpperCase(); const url=String(u&&u.url||u);
   window.__crawl.req.push({m,url});
   if(m!=='GET')return Promise.resolve(new Response(JSON.stringify({ok:true,intercepted:true}),{status:200,headers:{'Content-Type':'application/json'}}));
   return of(u,o);};
 window.open=(u)=>{window.__crawl.opened.push(String(u));return null;};
 window.confirm=(m)=>{window.__crawl.dlg.push(String(m));return false;};window.alert=(m)=>{window.__crawl.dlg.push(String(m));};
 window.prompt=(m)=>{window.__crawl.dlg.push(String(m));return null;};
 document.addEventListener('click',e=>{const a=e.target.closest&&e.target.closest('a[href]');if(!a)return;const h=a.getAttribute('href')||'';
   if(h&&h[0]!=='#'&&!h.startsWith('javascript:')){window.__crawl.nav.push(h);e.preventDefault();}},true);
 return 'ok';})()"""

SEL = "#view button, #view [role=button], #view a.btn, #runBar button"
BUTTONS = r"""(()=>{const out=[],seen={};
 for(const b of document.querySelectorAll('""" + SEL + r"""')){
   if(!b.offsetParent)continue; const lab=(b.getAttribute('aria-label')||b.textContent||b.title||'').trim().replace(/\s+/g,' ').slice(0,60);
   const k=b.tagName+'|'+lab; seen[k]=(seen[k]||0)+1; out.push({k,n:seen[k],lab,dis:!!b.disabled||b.getAttribute('aria-disabled')==='true'});}
 return out;})()"""
CLICK = r"""((k,n)=>{let i=0;for(const b of document.querySelectorAll('""" + SEL + r"""')){
   if(!b.offsetParent)continue; const lab=(b.getAttribute('aria-label')||b.textContent||b.title||'').trim().replace(/\s+/g,' ').slice(0,60);
   if(b.tagName+'|'+lab===k&&++i===n){b.scrollIntoView({block:'center'});b.click();return true;}}return false;})"""
TOASTS = "[...document.querySelectorAll('.toast')].map(e=>e.textContent.trim()).join(' | ')"


def run():
    import arch_core  # noqa: F401  (a standalone run gets a throwaway home first)
    import arch_browser
    import arch_selftest as base
    import arch_selftest_uismoke as U
    from arch_selftest_pro import fence

    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:500]})" if extra and not ok else ""))

    every = os.environ.get("ARCHITECT_CRAWL_ALL") == "1"
    planned = ["a real run finishes first", "every page opens with no uncaught page error",
               "pressing every button on the " + ("pages" if every else "key pages") + " throws no uncaught error",
               "no press freezes the page", "a press that saved nothing never shows a script error", "the hub logs no traceback or HTTP 500"]
    if not arch_browser.find_browser():
        for p in planned:
            print(f"  NOT RUN  {p} (no Edge or Chrome installed here)")
        print(f"\n  0 of {len(planned)} CRAWL CHECKS FAILED, {len(planned)} COULD NOT RUN (not a pass)")
        return 2

    class Model(base.Fake):
        def script(self, role, user, full, st):
            if "Write ONE focused pytest test that reproduces this bug" in user:
                return fence("tests/test_repro_bug.py", U.REPRO)
            if "Fix this bug with the smallest correct change" in user:
                return U.FIX
            return "- no concerns"

    fake = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp(prefix="archcrawl_")
    home, data, proj = (os.path.join(tmp, x) for x in ("home", "data", "proj"))
    for d in (home, data, os.path.join(proj, "tests")):
        os.makedirs(d)
    with open(os.path.join(home, "architect_config.json"), "w", encoding="utf-8") as f:
        json.dump({"default_model": "fake:7b", "server": {"kind": "ollama"}, "behavior": {"record_live": "off", "close_on_exit": "off"}}, f)
    for rel, text in (("pager.py", U.PAGER), ("tests/test_pager.py", U.TESTS)):
        with open(os.path.join(proj, rel), "w", encoding="utf-8") as f:
            f.write(text)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = dict(os.environ, ARCHITECT_HOME=home, ARCHITECT_DATA_DIR=data, OLLAMA_HOST=f"127.0.0.1:{fake.server_address[1]}",
               ARCHITECT_NO_CACHE="1", PYTHONUNBUFFERED="1")
    log = os.path.join(tmp, "hub.log")
    hub = subprocess.Popen([sys.executable, os.path.join(HERE, "architect_hub.py"), "--no-browser", "--port", str(port)], cwd=HERE, env=env,
                           stdout=open(log, "wb"), stderr=subprocess.STDOUT,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0)
    job = None
    try:
        import arch_sandbox
        job = arch_sandbox.attach(hub, 8192)
    except Exception:                                          # intentional: without a job object the hub is still killed below
        job = None
    b = None
    pressed, threw, froze, script_toasts, page_errs = 0, [], [], [], {}
    try:
        import urllib.request
        base_url = f"http://127.0.0.1:{port}/"
        for _ in range(80):
            try:
                urllib.request.urlopen(base_url, timeout=2).read()
                break
            except OSError:
                time.sleep(0.25)

        def open_browser():
            br = arch_browser.Browser.launch(1280, 900)
            br.navigate(base_url)
            br.wait_for("typeof go==='function'&&!!document.querySelector('#spGoal')", 30)
            br.eval("try{fvSkip()}catch(e){}")
            return br

        b = open_browser()
        b.type_into("#spRoot", proj)
        b.eval("document.querySelector('#spRoot').dispatchEvent(new Event('change',{bubbles:true}))")
        b.type_into("#spGoal", U.GOAL)
        b.eval("document.querySelector('#spGo').click()")
        t0 = time.time()
        while time.time() - t0 < 120 and "Finished" not in (b.eval("(document.querySelector('#spProgress')||{}).innerText||''") or ""):
            time.sleep(0.4)
        check("a real run finishes first (so the run pages have something in them)",
              "Finished" in (b.eval("(document.querySelector('#spProgress')||{}).innerText||''") or ""))
        b.eval(INTERCEPT)

        pages = b.eval("TABS.flatMap(t=>t[3])") or []
        for page in pages:
            n0 = len(b.exceptions)
            b.eval(f"document.querySelectorAll('dialog[open]').forEach(d=>d.close());try{{go('{page}')}}catch(e){{}}")
            time.sleep(1.2)
            if b.exceptions[n0:]:
                page_errs[page] = b.exceptions[n0:][:2]
        check(f"every page opens with no uncaught page error ({len(pages)} pages)", pages and not page_errs, page_errs)

        for page in [p for p in pages if every or p in KEY_PAGES]:
            b.eval(f"document.querySelectorAll('dialog[open]').forEach(d=>d.close());built={{}};go('{page}')")
            time.sleep(1.0)
            for bt in b.eval(BUTTONS) or []:
                if bt["dis"]:
                    continue
                try:
                    if b.eval("!window.__crawl"):
                        b.eval(INTERCEPT)
                    b.eval(f"document.querySelectorAll('dialog[open]').forEach(d=>d.close());built={{}};go('{page}')")
                    time.sleep(0.8)
                    n0, r0, t0s = len(b.exceptions), b.eval("window.__crawl.req.length"), b.eval(TOASTS) or ""
                    ok = b.eval(CLICK + f"({json.dumps(bt['k'])},{bt['n']})", timeout=10)
                    if not ok:                                   # rendered a moment later after the rebuild: once more
                        time.sleep(1.2)
                        ok = b.eval(CLICK + f"({json.dumps(bt['k'])},{bt['n']})", timeout=10)
                    if not ok:
                        continue
                    pressed += 1
                    time.sleep(0.8)
                    saves = b.eval(f"window.__crawl.req.slice({r0}).filter(r=>r.m!=='GET').length", timeout=10)
                    toast = b.eval(TOASTS, timeout=10) or ""
                    if b.exceptions[n0:]:
                        threw.append((page, bt["lab"], b.exceptions[n0][:160]))
                    if not saves and toast != t0s and SCRIPT_ERROR.search(toast):
                        script_toasts.append((page, bt["lab"], toast[-160:]))
                except Exception as ex:                          # a press that froze the page (a native dialog, an endless loop)
                    froze.append((page, bt["lab"], f"{type(ex).__name__}: {ex}"[:160]))
                    try:
                        b.close()
                    except Exception:                            # intentional: the frozen browser is replaced either way
                        pass
                    b = open_browser()
                    b.eval(INTERCEPT)
        check(f"pressing every enabled button on the {'pages' if every else 'key pages'} throws no uncaught error ({pressed} pressed)",
              pressed > 0 and not threw, threw[:4])
        check("no press freezes the page", not froze, froze[:4])
        check("a press that saved nothing never shows a script error in a toast", not script_toasts, script_toasts[:4])
    except Exception as ex:
        check("the crawl ran to the end", False, f"{type(ex).__name__}: {ex}")
    finally:
        if b is not None:
            b.close()
        try:
            if job is not None:
                import arch_sandbox
                arch_sandbox.close(job)
            hub.kill()
            hub.wait(10)
        except Exception:                                       # intentional: the hub may already be gone
            pass
        fake.shutdown()
        time.sleep(0.5)
        try:
            lines = open(log, encoding="utf-8", errors="replace").read().splitlines()
            bad = []
            for i, l in enumerate(lines):
                if '" 500 ' in l:
                    bad.append(l.strip()[:200])
                elif l.startswith("Traceback"):                # report where it happened and what it was, not just "Traceback"
                    block = lines[i:i + 40]
                    end = next((k for k, x in enumerate(block[1:], 1) if x and not x.startswith((" ", "	")) and not x.startswith("Traceback")), None)
                    frames = [x.strip() for x in block[1:end] if x.strip().startswith("File ")]
                    what = block[end].strip() if end else ""
                    # the browser hanging up mid-request (a page change cancels a slow read) is not a hub fault: Python's HTTP server
                    # prints it as a traceback in handle(); anything else, or a traceback that could not be read, still fails
                    if CLIENT_GONE.match(what):
                        continue
                    bad.append(" | ".join(frames[-2:]) + " -> " + (what[:200] or " / ".join(x.strip() for x in block[-3:])))
        except OSError:
            bad = ["could not read the hub log"]
        check("the hub logs no traceback or HTTP 500", not bad, bad[:3])
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(results)
    print(f"\n  {'ALL ' + str(n) + ' CRAWL CHECKS PASSED' if n == len(results) else str(len(results) - n) + ' of ' + str(len(results)) + ' CRAWL CHECKS FAILED'}")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(run())
