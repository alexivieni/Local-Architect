"""A desktop notification when long work ends: a run, the queue, a data night, a self-improve round.

Local only (no service, no network): Windows uses the built-in Windows.UI.Notifications toast through PowerShell, with a tray balloon
(System.Windows.Forms.NotifyIcon) as the fallback; macOS uses osascript; Linux uses notify-send. Never raises, never blocks: the
helper is started detached and its outcome is not waited for. Gated by `behavior.notify` (on|off, default on). The text travels in
environment variables, never inside the script, so nothing in a title can become a command.
"""
import os
import shutil
import subprocess
import sys

_PS = r"""
$t = $env:ARCH_NOTIFY_TITLE; $m = $env:ARCH_NOTIFY_TEXT
try {
  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
  [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
  $x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
  $n = $x.GetElementsByTagName('text'); $n.Item(0).AppendChild($x.CreateTextNode($t)) | Out-Null; $n.Item(1).AppendChild($x.CreateTextNode($m)) | Out-Null
  $app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($x))
} catch {
  Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing
  $b = New-Object System.Windows.Forms.NotifyIcon; $b.Icon = [System.Drawing.SystemIcons]::Information; $b.Visible = $true
  $b.ShowBalloonTip(8000, $t, $m, 'Info'); Start-Sleep -Seconds 9; $b.Dispose()
}
"""
_OSA = 'display notification (system attribute "ARCH_NOTIFY_TEXT") with title (system attribute "ARCH_NOTIFY_TITLE")'


def enabled(cfg=None):
    if os.environ.get("ARCHITECT_NO_NOTIFY"):
        return False
    try:
        if cfg is None:
            import arch_core as core
            cfg = core.load_config()
        return str(((cfg or {}).get("behavior") or {}).get("notify", "on")).lower() not in ("off", "false", "0", "no")
    except Exception:
        return True


def command(platform=None):
    """-> the argv that shows a notification on this platform (text comes from the environment), or None."""
    p = platform or sys.platform
    if p.startswith("win"):
        return ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", _PS]
    if p == "darwin":
        return ["osascript", "-e", _OSA]
    if shutil.which("notify-send") or platform:
        return ["sh", "-c", 'notify-send "$ARCH_NOTIFY_TITLE" "$ARCH_NOTIFY_TEXT"']
    return None


def notify(title, text="", cfg=None, launch=None, platform=None):
    """Show one notification. -> True when a helper was started, False when off/unavailable/failed. Never raises.
    `launch(argv, env)` replaces subprocess.Popen in tests."""
    try:
        if not enabled(cfg):
            return False
        argv = command(platform)
        if not argv:
            return False
        env = dict(os.environ, ARCH_NOTIFY_TITLE=" ".join(str(title or "Architect").split())[:120],
                   ARCH_NOTIFY_TEXT=" ".join(str(text or "").split())[:300])
        if launch:
            launch(argv, env)
            return True
        kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "env": env, "close_fds": True}
        if sys.platform.startswith("win"):
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(subprocess, "DETACHED_PROCESS", 0)
        else:
            kw["start_new_session"] = True
        subprocess.Popen(argv, **kw)
        return True
    except Exception:
        return False
