"""
netguard.py - best-effort outbound-network guard for the agent sandbox
======================================================================

Loaded in two places by autonomous_agent_optimized.py:
  (a) inside the agent process, loopback-only, so the only thing it can talk to is your
      local Ollama;
  (b) inside every pytest run (via a generated sitecustomize.py), so code the model wrote
      cannot reach the network.

Policy
  offline  (default)  only loopback is reachable (127.0.0.0/8, ::1, localhost).
  practice            loopback + an explicit HOSTNAME allowlist (AGENT_NET_ALLOW).
                      A direct connection to an IP is allowed only if that IP was returned
                      by resolving an allowlisted hostname, so scanning address ranges or
                      random IPs is blocked.

It also refuses to start common network / VCS / scanner programs (curl, wget, nmap, ssh,
git, ...): those open their own sockets, so a Python-level allowlist cannot police them.

LIMITS - please read
  This is a Python-level guard against ACCIDENTS by a small model. It is not a security
  boundary. Code that imports `_socket` directly, uses ctypes, or launches a program that is
  not on the denylist can bypass it. For a hard guarantee use an OS firewall rule, a
  container/VM with networking disabled, or simply turn Wi-Fi off (Ollama works on localhost
  without internet). See README_AGENT.md.
"""

import os
import re
import socket
import subprocess
import ipaddress

__all__ = ["NetworkBlocked", "install", "install_from_env", "DENY_BINARIES"]


class NetworkBlocked(OSError):
    """Raised instead of making a connection / DNS lookup / process launch that policy forbids."""


# Programs that open their own network connections (the Python allowlist cannot police them)
# or push code somewhere.
DENY_BINARIES = {
    "curl", "wget", "nmap", "masscan", "ncat", "nc", "netcat", "ping", "ping6", "traceroute",
    "tracert", "nslookup", "dig", "ssh", "scp", "sftp", "ftp", "tftp", "telnet", "rsync",
    "git", "gh", "hub", "powershell", "pwsh", "certutil", "bitsadmin", "aria2c",
    "nikto", "sqlmap", "hydra", "gobuster", "ffuf", "msfconsole",
}

_state = {"mode": "offline", "allow": set(), "ips": set(), "block_binaries": True}
_ORIG = {}


# ------------------------------------------------------------------------------
# policy checks
# ------------------------------------------------------------------------------
def _norm(host):
    return str(host).strip().lower().rstrip(".").split("%")[0]


def _is_loopback(h):
    if h in ("", "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_loopback or ip.is_unspecified


def _check_host(host, what):
    if host is None or isinstance(host, int):
        return
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "ignore")
    if not isinstance(host, str):
        return
    h = _norm(host)
    if _is_loopback(h):
        return
    if _state["mode"] == "open":
        return
    if _state["mode"] == "practice" and (h in _state["allow"] or h in _state["ips"]):
        return
    scope = ("loopback + " + ", ".join(sorted(_state["allow"]))) if (
        _state["mode"] == "practice" and _state["allow"]) else "loopback only"
    raise NetworkBlocked(
        f"[netguard] blocked {what} to '{host}' (mode={_state['mode']}: {scope}). "
        "Mock the call in tests, or use localhost.")


def _remember(host, ips):
    """In practice mode, remember IPs that belong to allowlisted hostnames."""
    if _state["mode"] == "practice" and isinstance(host, str) and _norm(host) in _state["allow"]:
        for ip in ips:
            _state["ips"].add(_norm(ip))


_SPLIT = re.compile(r"[\s;|&()<>`$]+")


def _check_command(args, shell=False, executable=None):
    if not _state["block_binaries"]:
        return
    tokens = []
    try:
        if isinstance(args, (str, bytes, os.PathLike)):
            text = os.fsdecode(args)
            if shell:
                tokens = [t for t in _SPLIT.split(text) if t]
            else:
                parts = text.split()
                tokens = parts[:1]
        else:
            tokens = [os.fsdecode(a) for a in args]
            if not shell:
                tokens = tokens[:1]          # list form: only the program name matters
        if executable:
            tokens.append(os.fsdecode(executable))
    except Exception:
        return
    for tok in tokens:
        name = os.path.splitext(os.path.basename(tok.replace("\\", "/")))[0].lower()
        if name in DENY_BINARIES:
            raise NetworkBlocked(
                f"[netguard] blocked launching '{name}': network, scanner and VCS tools are "
                "not allowed inside the agent sandbox. Run them yourself, outside the agent.")


# ------------------------------------------------------------------------------
# patched functions
# ------------------------------------------------------------------------------
def _getaddrinfo(host, *a, **k):
    _check_host(host, "DNS lookup")
    res = _ORIG["getaddrinfo"](host, *a, **k)
    _remember(host, [sa[0] for (_f, _t, _p, _c, sa) in res])
    return res


def _gethostbyname(host):
    _check_host(host, "DNS lookup")
    res = _ORIG["gethostbyname"](host)
    _remember(host, [res])
    return res


def _gethostbyname_ex(host):
    _check_host(host, "DNS lookup")
    res = _ORIG["gethostbyname_ex"](host)
    _remember(host, res[2])
    return res


def _gethostbyaddr(host):
    _check_host(host, "reverse DNS lookup")
    return _ORIG["gethostbyaddr"](host)


def _getnameinfo(sockaddr, flags):
    _check_host(sockaddr[0] if isinstance(sockaddr, tuple) and sockaddr else None,
                "reverse DNS lookup")
    return _ORIG["getnameinfo"](sockaddr, flags)


def _addr_host(address):
    # AF_INET/AF_INET6 addresses are tuples; AF_UNIX paths (str/bytes) are local IPC.
    if isinstance(address, tuple) and address:
        return address[0]
    return None


def _connect(self, address):
    _check_host(_addr_host(address), "connection")
    return _ORIG["connect"](self, address)


def _connect_ex(self, address):
    _check_host(_addr_host(address), "connection")
    return _ORIG["connect_ex"](self, address)


def _sendto(self, *args):
    _check_host(_addr_host(args[-1]) if args else None, "UDP send")
    return _ORIG["sendto"](self, *args)


def _popen_init(self, args, *a, **k):
    _check_command(args, k.get("shell", False), k.get("executable"))
    return _ORIG["popen_init"](self, args, *a, **k)


def _system(command):
    _check_command(command, True, None)
    return _ORIG["system"](command)


def _patch_asyncio():
    """asyncio can skip socket.connect for IP literals (Windows Proactor loop), so guard
    the loop-level entry points too."""
    try:
        import asyncio.base_events as be
        import asyncio.proactor_events as pe
    except Exception:
        return
    orig_cc = be.BaseEventLoop.create_connection

    async def create_connection(self, protocol_factory, host=None, port=None, **kw):
        _check_host(host, "connection")
        return await orig_cc(self, protocol_factory, host, port, **kw)

    be.BaseEventLoop.create_connection = create_connection

    orig_sc = getattr(pe.BaseProactorEventLoop, "sock_connect", None)
    if orig_sc is not None:
        async def sock_connect(self, sock, address):
            _check_host(_addr_host(address), "connection")
            return await orig_sc(self, sock, address)

        pe.BaseProactorEventLoop.sock_connect = sock_connect


def _patch_once():
    if _ORIG:
        return
    _ORIG.update(
        getaddrinfo=socket.getaddrinfo, gethostbyname=socket.gethostbyname,
        gethostbyname_ex=socket.gethostbyname_ex, gethostbyaddr=socket.gethostbyaddr,
        getnameinfo=socket.getnameinfo, connect=socket.socket.connect,
        connect_ex=socket.socket.connect_ex, sendto=socket.socket.sendto,
        popen_init=subprocess.Popen.__init__, system=os.system)
    socket.getaddrinfo = _getaddrinfo
    socket.gethostbyname = _gethostbyname
    socket.gethostbyname_ex = _gethostbyname_ex
    socket.gethostbyaddr = _gethostbyaddr
    socket.getnameinfo = _getnameinfo
    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex
    socket.socket.sendto = _sendto
    subprocess.Popen.__init__ = _popen_init
    os.system = _system
    _patch_asyncio()


# ------------------------------------------------------------------------------
# public API
# ------------------------------------------------------------------------------
def install(mode="offline", allow=(), block_binaries=True):
    """Activate the guard (idempotent). `mode` is 'offline' (loopback only), 'practice' (loopback +
    `allow`, DNS-pinned: only IPs that an allowlisted hostname actually resolved to), or 'open' (no host
    restriction at all -- used only when you explicitly asked for it). `block_binaries` is independent of
    `mode`: it refuses to launch curl/nmap/ssh/git/... regardless, since those open their own sockets a
    host-level allowlist can't police -- pass False only if you explicitly want those runnable too."""
    mode = mode if str(mode).strip().lower() in ("offline", "practice", "open") else "offline"
    mode = str(mode).strip().lower()
    _state["mode"] = mode
    _state["allow"] = {_norm(h) for h in allow if h and str(h).strip()} if mode == "practice" else set()
    _state["ips"] = set()
    _state["block_binaries"] = bool(block_binaries)
    _patch_once()
    return {"mode": mode, "allow": sorted(_state["allow"]), "block_binaries": _state["block_binaries"]}


def install_from_env():
    """Used by the generated sitecustomize.py inside pytest and any Python child process. Reads exactly the
    three env vars arch_core.guard_env() sets from behavior.network/net_allow/net_block_tools -- the single
    source of truth those settings flow through, so this guard never drifts from what the UI/CLI show."""
    return install(os.environ.get("AGENT_NET_MODE", "offline"),
                   os.environ.get("AGENT_NET_ALLOW", "").split(","),
                   block_binaries=os.environ.get("AGENT_NET_TOOLS", "block") != "allow")
