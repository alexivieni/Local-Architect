import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from __PKG__ import crypto  # noqa: E402
from __PKG__.store import MAX_FAILURES, AuthError, Store  # noqa: E402

PW = "correct horse battery"


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


def make():
    c = Clock()
    return Store(clock=c, iterations=1000), c


def test_password_hash_round_trip_and_unique_salt():
    a, b = crypto.hash_password(PW, iterations=1000), crypto.hash_password(PW, iterations=1000)
    assert a != b and crypto.verify_password(PW, a) and not crypto.verify_password("nope", a)
    assert not crypto.verify_password(PW, "garbage")


def test_signed_tokens_expire_and_detect_tampering():
    t = crypto.sign({"sub": 1}, "k" * 16, ttl_s=60, now=100)
    assert crypto.verify(t, "k" * 16, now=120)["sub"] == 1
    assert crypto.verify(t, "k" * 16, now=200) is None
    assert crypto.verify(t, "x" * 16, now=120) is None
    assert crypto.verify(t[:-2] + "AA", "k" * 16, now=120) is None and crypto.verify("junk", "k") is None


def test_register_validates_and_refuses_duplicates():
    s, _ = make()
    s.register("A@x.io", PW)
    with pytest.raises(AuthError) as e:
        s.register("a@x.io", PW)
    assert e.value.status == 409
    for email, pw in (("bad", PW), ("b@x.io", "short")):
        with pytest.raises(AuthError):
            s.register(email, pw)


def test_sessions_are_stored_hashed_and_logout_works():
    s, _ = make()
    uid = s.register("a@x.io", PW)
    got, token = s.login("a@x.io", PW)
    assert got == uid and s.session_user(token) == uid
    stored = [r[0] for r in s.db.execute("SELECT digest FROM sessions")]
    assert token not in stored and crypto.digest(token) in stored
    s.logout(token)
    assert s.session_user(token) is None


def test_lockout_after_failures_then_recovery():
    s, clock = make()
    s.register("a@x.io", PW)
    for _ in range(MAX_FAILURES):
        with pytest.raises(AuthError):
            s.login("a@x.io", "wrong password")
    with pytest.raises(AuthError) as e:
        s.login("a@x.io", PW)
    assert e.value.status == 429
    clock.t += 301
    assert s.login("a@x.io", PW)[0]


def test_unknown_email_and_wrong_password_look_the_same():
    s, _ = make()
    s.register("a@x.io", PW)
    msgs = set()
    for email in ("a@x.io", "nobody@x.io"):
        with pytest.raises(AuthError) as e:
            s.login(email, "wrong password")
        msgs.add((e.value.status, str(e.value)))
    assert len(msgs) == 1
