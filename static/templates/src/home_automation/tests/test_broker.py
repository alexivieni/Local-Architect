import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from __PKG__.broker import Broker, matches, valid_filter  # noqa: E402


@pytest.mark.parametrize("pattern,topic,ok", [
    ("home/+/light", "home/hall/light", True),
    ("home/+/light", "home/hall/x/light", False),
    ("home/#", "home", True),
    ("home/#", "home/a/b/c", True),
    ("home/hall", "home/hall/light", False),
    ("+/+", "a/b", True),
])
def test_matching(pattern, topic, ok):
    assert matches(pattern, topic) is ok


def test_filter_validation():
    assert valid_filter("a/+/#") and not valid_filter("a/#/b") and not valid_filter("a/b+") and not valid_filter("")


def test_retained_messages_reach_late_subscribers_and_can_be_cleared():
    b = Broker()
    b.publish("home/x/state", "1", retain=True)
    got = []
    b.subscribe("home/+/state", lambda t, p: got.append(p))
    assert got == ["1"]
    b.publish("home/x/state", "", retain=True)
    seen = []
    b.subscribe("home/#", lambda t, p: seen.append(p))
    assert seen == []


def test_unsubscribe_and_bad_publish():
    b = Broker()
    got = []
    off = b.subscribe("a", lambda t, p: got.append(p))
    b.publish("a", "1")
    off()
    b.publish("a", "2")
    assert got == ["1"]
    with pytest.raises(ValueError):
        b.publish("a/+", "x")
