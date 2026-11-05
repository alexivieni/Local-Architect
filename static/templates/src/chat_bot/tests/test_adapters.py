import hashlib
import hmac
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from __PKG__ import adapters  # noqa: E402

SECRET = "test-secret-not-real"


def sign(ts, body):
    return "v0=" + hmac.new(SECRET.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()


def test_slack_signature_good_bad_and_stale():
    body = "command=%2Froll&text=2d6"
    assert adapters.verify_slack(SECRET, "1000", body, sign(1000, body), now=1010)
    assert not adapters.verify_slack(SECRET, "1000", body + "x", sign(1000, body), now=1010)
    assert not adapters.verify_slack(SECRET, "1000", body, sign(1000, body), now=5000)
    assert not adapters.verify_slack(SECRET, "nope", body, "", now=1010)


def test_slack_payloads_become_messages():
    m = adapters.from_slack({"user_id": "U1", "channel_id": "C1", "command": "/roll", "text": "2d6"})
    assert (m.user, m.channel, m.text) == ("U1", "C1", "!roll 2d6")
    e = adapters.from_slack({"event": {"user": "U2", "channel": "C2", "text": "!help"}})
    assert e.text == "!help" and adapters.to_slack("hi")["text"] == "hi"


def test_discord_message_shape():
    fake = SimpleNamespace(author=SimpleNamespace(id=7), channel=SimpleNamespace(id=9), content="!roll")
    m = adapters.from_discord(fake)
    assert (m.user, m.channel, m.text) == ("7", "9", "!roll")
