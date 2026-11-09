"""A real browser for the self-tests, with nothing to install: Edge or Chrome (already on the machine) driven headless over the
Chrome DevTools Protocol on 127.0.0.1, with a stdlib WebSocket client. Used by arch_selftest_uismoke to click through the hub the
way a person does.

    b = Browser.launch()          # None when no Chromium browser is installed: the caller reports "could not run", never a pass
    b.navigate("http://127.0.0.1:8766/")
    b.eval("document.title")
    b.wait_for("document.querySelector('#spGoal') !== null")
    b.type_into("#spGoal", "fix it")
    b.close()

Loopback only: the browser listens on 127.0.0.1 and starts with background networking, sync, component updates and extensions off,
in a throwaway profile that is deleted on close. Its whole process tree is closed with it (a Windows job object where available).
"""
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]
NAMES = ["msedge", "microsoft-edge", "google-chrome", "chrome", "chromium", "chromium-browser"]


def find_browser():
    """The path of an installed Chromium browser (ARCHITECT_BROWSER wins), or None."""
    env = os.environ.get("ARCHITECT_BROWSER")
    if env and os.path.isfile(env):
        return env
    for p in CANDIDATES:
        if os.path.isfile(p):
            return p
    for n in NAMES:
        w = shutil.which(n)
        if w:
            return w
    return None


class WS:
    """The client half of RFC 6455, just enough for DevTools: text frames, masking, fragments, ping/pong, close."""

    def __init__(self, url, timeout=30):
        assert url.startswith("ws://"), url
        hostport, _, path = url[5:].partition("/")
        host, _, port = hostport.partition(":")
        if host not in ("127.0.0.1", "localhost", "[::1]"):
            raise ValueError("loopback only: " + host)
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake: connection closed")
            head += chunk
        line = head.split(b"\r\n", 1)[0]
        if b" 101 " not in line + b" ":
            raise ConnectionError("websocket handshake refused: " + line.decode(errors="replace"))
        self.buf = head.split(b"\r\n\r\n", 1)[1]

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self, opcode, payload):
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, text):
        self._frame(0x1, text.encode("utf-8"))

    def recv(self):
        parts = []
        while True:
            b0, b1 = self._read(2)
            fin, op, n = b0 & 0x80, b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 0x9:                     # ping
                self._frame(0xA, data)
                continue
            if op == 0xA:                     # pong
                continue
            if op == 0x8:
                raise ConnectionError("websocket closed by the browser")
            parts.append(data)
            if fin:
                return b"".join(parts).decode("utf-8", errors="replace")

    def close(self):
        try:
            self._frame(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class Browser:
    def __init__(self, proc, profile, port, ws, job=None):
        self.proc, self.profile, self.port, self.ws, self.job = proc, profile, port, ws, job
        self.n = 0
        self.exceptions = []          # uncaught page errors (Runtime.exceptionThrown)
        self.console_errors = []      # console.error(...) calls

    @classmethod
    def launch(cls, width=1280, height=900, timeout=30):
        exe = find_browser()
        if not exe:
            return None
        profile = tempfile.mkdtemp(prefix="arch_browser_")
        args = [exe, "--headless=new", "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
                "--disable-component-update", "--disable-sync", "--disable-extensions", "--disable-default-apps",
                "--metrics-recording-only", "--mute-audio", f"--window-size={width},{height}", "about:blank"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, creationflags=flags)
        job = None
        try:
            import arch_sandbox
            job = arch_sandbox.attach(proc, 4096)
        except Exception:
            job = None
        port_file = os.path.join(profile, "DevToolsActivePort")
        t0 = time.time()
        port = None
        # A launcher that exits with 0 may have handed off to the real browser process (Edge does on this laptop since 2026-11-09:
        # msedge.exe starts the browser as a new process and returns at once), so keep waiting for the port; only a launcher that
        # FAILED (non-zero exit) ends the wait early.
        while time.time() - t0 < timeout and proc.poll() in (None, 0):
            try:
                with open(port_file, encoding="utf-8") as f:
                    port = int(f.readline().strip())
                break
            except (OSError, ValueError):
                time.sleep(0.1)
        if not port:
            cls._kill(proc, job)
            shutil.rmtree(profile, ignore_errors=True)
            raise RuntimeError("the browser did not open its debugging port")
        page = None
        while time.time() - t0 < timeout:
            try:
                targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5).read())
                page = next((t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
                if page:
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        if not page:
            cls._kill(proc, job)
            shutil.rmtree(profile, ignore_errors=True)
            raise RuntimeError("the browser opened no page")
        b = cls(proc, profile, port, WS(page["webSocketDebuggerUrl"]), job)
        b.cmd("Runtime.enable")
        b.cmd("Page.enable")
        return b

    def _event(self, msg):
        m = msg.get("method")
        if m == "Runtime.exceptionThrown":
            d = msg["params"]["exceptionDetails"]
            self.exceptions.append(((d.get("exception") or {}).get("description") or d.get("text") or "")[:300])
        elif m == "Runtime.consoleAPICalled" and msg["params"].get("type") == "error":
            self.console_errors.append(" ".join(str(a.get("value", a.get("description", ""))) for a in msg["params"].get("args", []))[:300])

    def cmd(self, method, params=None, timeout=30):
        self.n += 1
        my = self.n
        self.ws.send(json.dumps({"id": my, "method": method, "params": params or {}}))
        t0 = time.time()
        while time.time() - t0 < timeout:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == my:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result") or {}
            self._event(msg)
        raise TimeoutError(method)

    def eval(self, expr, timeout=30):
        """Evaluate JavaScript in the page (promises awaited) -> its JSON value. A thrown error raises RuntimeError."""
        r = self.cmd("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True, "userGesture": True}, timeout)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise RuntimeError(((d.get("exception") or {}).get("description") or d.get("text") or "error")[:300])
        return (r.get("result") or {}).get("value")

    def navigate(self, url, timeout=30):
        self.cmd("Page.navigate", {"url": url})
        return self.wait_for("document.readyState === 'complete'", timeout)

    def wait_for(self, expr, timeout=30, every=0.1):
        """Poll a JavaScript condition until it is truthy -> True, or False after `timeout` s. Never raises for a page error in it."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if self.eval(f"(()=>{{try{{return !!({expr})}}catch(e){{return false}}}})()"):
                    return True
            except (RuntimeError, TimeoutError):
                pass
            time.sleep(every)
        return False

    def type_into(self, selector, text):
        """Focus the element, select what it holds and type `text` as keyboard input (real input events, like a person)."""
        ok = self.eval(f"(()=>{{const e=document.querySelector({json.dumps(selector)});if(!e)return false;e.focus();"
                       f"if(e.select)e.select();return document.activeElement===e}})()")
        if not ok:
            raise RuntimeError("cannot focus " + selector)
        self.cmd("Input.insertText", {"text": text})

    def click_text(self, text, scope="document"):
        """Click the first visible button whose text is exactly `text` (inside `scope`, a JS expression) -> True when found."""
        return bool(self.eval(f"(()=>{{const s={scope};if(!s)return false;const b=[...s.querySelectorAll('button')].find(x=>x.textContent.trim()==={json.dumps(text)}&&x.offsetParent!==null);"
                              f"if(!b)return false;b.click();return true}})()"))

    @staticmethod
    def _kill(proc, job):
        try:
            if job is not None:
                import arch_sandbox
                arch_sandbox.close(job)
        except Exception:
            pass
        try:
            proc.kill()
            proc.wait(10)
        except Exception:
            pass

    def close(self):
        try:
            try:
                self.cmd("Browser.close", timeout=5)   # the browser shuts itself down, even one a launcher handed off to (not our child)
            except Exception:                          # intentional: a browser that already went away cannot be asked to close; the kill below still runs
                pass
            self.ws.close()
        finally:
            self._kill(self.proc, self.job)
            for _ in range(20):                       # the profile is held for a moment after the browser exits
                shutil.rmtree(self.profile, ignore_errors=True)
                if not os.path.exists(self.profile):
                    break
                time.sleep(0.25)
