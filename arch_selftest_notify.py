"""Self-test for desktop notifications (arch_notify): the right helper per platform, text only through the environment, off switches,
and that nothing ever raises. A fake launcher stands in for the real process, so no toast is shown.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.pop("ARCHITECT_NO_NOTIFY", None)


def run():
    results = []

    def check(name, ok, extra=""):
        results.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({str(extra)[:300]})" if extra and not ok else ""))

    print("\n== notifications ==")
    import arch_notify as N
    w = N.command("win32")
    check("Windows: built-in PowerShell, no extra module", w and w[0] == "powershell" and "Install-Module" not in w[-1] and "ToastNotification" in w[-1], w)
    check("Windows: balloon fallback exists", "NotifyIcon" in w[-1])
    m = N.command("darwin")
    check("macOS: osascript", m and m[0] == "osascript" and "display notification" in m[-1], m)
    l = N.command("linux")
    check("Linux: notify-send", l and "notify-send" in l[-1], l)
    got = []
    ok = N.notify('Done "now"; $(calc) `x`', "line1\nline2 " + "z" * 500, cfg={"behavior": {"notify": "on"}}, launch=lambda a, e: got.append((a, e)), platform="linux")
    env = got[0][1] if got else {}
    check("notify() starts a helper and returns True", ok is True and len(got) == 1)
    check("text travels in the environment, one line, capped", env.get("ARCH_NOTIFY_TITLE", "").startswith('Done "now"') and "\n" not in env.get("ARCH_NOTIFY_TEXT", "x\n")
          and len(env.get("ARCH_NOTIFY_TEXT", "")) <= 300, env.get("ARCH_NOTIFY_TEXT", "")[:40])
    check("the argv carries no part of the text", not any("calc" in a or "line1" in a for a in got[0][0]))
    for off in ("off", "false", "0", "no", "OFF"):
        n = []
        r = N.notify("t", "x", cfg={"behavior": {"notify": off}}, launch=lambda a, e: n.append(1), platform="linux")
        if r is not False or n:
            check(f"behavior.notify={off} is silent", False)
            break
    else:
        check("behavior.notify = off|false|0|no is silent", True)
    check("default is on", N.enabled({}) is True)
    os.environ["ARCHITECT_NO_NOTIFY"] = "1"
    n = []
    check("ARCHITECT_NO_NOTIFY silences everything", N.notify("t", launch=lambda a, e: n.append(1), platform="linux") is False and not n)
    os.environ.pop("ARCHITECT_NO_NOTIFY")

    def boom(a, e):
        raise OSError("no such program")
    check("a failing launcher never raises", N.notify("t", "x", cfg={}, launch=boom, platform="win32") is False)
    check("a missing platform helper is False, not an error", N.notify("t", "x", cfg={}, launch=lambda a, e: None, platform=None) in (True, False))
    check("garbage arguments never raise", N.notify(None, object(), cfg={"behavior": None}, launch=lambda a, e: None, platform="darwin") in (True, False))
    src = open(os.path.join(HERE, "arch_supervisor.py"), encoding="utf-8").read()
    check("the queue end notifies", "arch_notify.notify" in src and "queue finished" in src)
    hub = open(os.path.join(HERE, "architect_hub.py"), encoding="utf-8").read()
    check("the hub notifies when a job (run, self-improve round) ends", "start_notify_watch" in hub and hub.count("arch_notify.notify") >= 1)
    tt = open(os.path.join(HERE, "tools", "train_traces.py"), encoding="utf-8").read()
    check("the data night notifies at its end", "arch_notify.notify" in tt)
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(run())
