"""App-like launching: reuse a hub that is already open, and make a desktop shortcut so it starts like any other app."""
import os
import stat
import sys
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
MARK = "Architect"


def running_hub(port=8766, span=20, get=None):
    """-> URL of an Architect hub already listening on this computer, else None."""
    get = get or (lambda url: urllib.request.urlopen(url, timeout=1).read(4000).decode("utf-8", "replace"))
    for p in range(port, port + span):
        url = f"http://127.0.0.1:{p}/"
        try:
            head = get(url)
        except (OSError, ValueError):
            continue
        if "<title>" in head and MARK in head.split("<title>", 1)[1].split("</title>", 1)[0]:
            return url
    return None


def open_existing(port=8766, opener=webbrowser.open, get=None):
    """If a hub is already running, open it and say so. -> True when one was found."""
    url = running_hub(port, get=get)
    if url:
        print(f"Architect is already running at {url} - opening it.")
        opener(url)
    return bool(url)


def make_shortcut(dest_dir=None, platform=None):
    """Create a desktop launcher for the hub. -> path written, or None with nothing written. Never overwrites a file that is not ours."""
    plat = platform or sys.platform
    desk = dest_dir or os.path.join(os.path.expanduser("~"), "Desktop")
    if not os.path.isdir(desk):
        return None
    py = sys.executable
    hub = os.path.join(HERE, "architect_hub.py")
    if plat.startswith("win"):
        path = os.path.join(desk, "Architect.bat")
        body = (
            f'@echo off\r\n'
            f'set OLLAMA_FLASH_ATTENTION=1\r\n'
            f'set OLLAMA_KV_CACHE_TYPE=q8_0\r\n'
            f'cd /d "{HERE}"\r\n'
            f'"{py}" "{hub}"\r\n'
        )
    elif plat == "darwin":
        path = os.path.join(desk, "Architect.command")
        body = (
            f'#!/bin/bash\n'
            f'export OLLAMA_FLASH_ATTENTION=1\n'
            f'export OLLAMA_KV_CACHE_TYPE=q8_0\n'
            f'cd "{HERE}"\n'
            f'"{py}" "{hub}"\n'
        )
    else:
        path = os.path.join(desk, "Architect.desktop")
        body = f'[Desktop Entry]\nType=Application\nName=Architect\nComment=Local agentic coding team\nExec=env OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 {py} "{hub}"\nPath={HERE}\nTerminal=true\n'
    if os.path.exists(path):
        try:
            if "architect_hub.py" not in open(path, encoding="utf-8", errors="replace").read():
                return None
        except OSError:
            return None
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(body)
    if not plat.startswith("win"):
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return path


def shortcut_status(dest_dir=None, platform=None):
    """-> {exists, path, desktop_dir} for the desktop launcher without writing anything."""
    plat = platform or sys.platform
    desk = dest_dir or os.path.join(os.path.expanduser("~"), "Desktop")
    name = "Architect.bat" if plat.startswith("win") else ("Architect.command" if plat == "darwin" else "Architect.desktop")
    path = os.path.join(desk, name)
    exists = os.path.isfile(path)
    return {"exists": exists, "path": path if exists else os.path.join(desk, name), "desktop_dir": desk, "dir_exists": os.path.isdir(desk)}
