"""Platform edges. Only shape conversion and Slack's request signature; no network calls here."""
import hashlib
import hmac
import time

from .bot import Message

MAX_SKEW_S = 300


def verify_slack(secret, timestamp, body, signature, now=None):
    """Slack signs 'v0:{timestamp}:{body}' with HMAC-SHA256. Old timestamps are refused (replay)."""
    now = time.time() if now is None else now
    try:
        if abs(now - int(timestamp)) > MAX_SKEW_S:
            return False
    except (TypeError, ValueError):
        return False
    base = f"v0:{timestamp}:{body}".encode()
    want = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, signature or "")


def from_slack(payload):
    """Slash command form fields or an Events API 'message' event -> Message."""
    if "event" in payload:
        ev = payload["event"]
        return Message(ev.get("user", ""), ev.get("channel", ""), ev.get("text", ""))
    return Message(payload.get("user_id", ""), payload.get("channel_id", ""), "!" + payload.get("command", "/").lstrip("/") + " " + payload.get("text", ""))


def from_discord(msg):
    """A discord.py Message (or anything with .author.id, .channel.id, .content) -> Message."""
    return Message(str(msg.author.id), str(msg.channel.id), msg.content)


def to_slack(text):
    return {"response_type": "in_channel", "text": text}
