"""In-memory broker with MQTT topic rules. Delivery is synchronous and in subscription order."""


def valid_filter(pattern):
    parts = pattern.split("/")
    if not pattern or any(p != "+" and "+" in p for p in parts) or any("#" in p for p in parts[:-1]):
        return False
    return parts[-1] == "#" or "#" not in parts[-1]


def matches(pattern, topic):
    """MQTT matching: '+' is exactly one level, a trailing '#' is zero or more levels."""
    p, t = pattern.split("/"), topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(p) == len(t)


class Broker:
    def __init__(self):
        self.subs = []          # (pattern, callback)
        self.retained = {}
        self.log = []

    def subscribe(self, pattern, callback):
        if not valid_filter(pattern):
            raise ValueError(f"bad topic filter {pattern!r}")
        self.subs.append((pattern, callback))
        for topic, payload in sorted(self.retained.items()):
            if matches(pattern, topic):
                callback(topic, payload)
        return lambda: self.subs.remove((pattern, callback))

    def publish(self, topic, payload, retain=False):
        if not topic or "+" in topic or "#" in topic:
            raise ValueError(f"cannot publish to {topic!r}")
        self.log.append((topic, payload))
        if retain:
            if payload in (None, ""):
                self.retained.pop(topic, None)          # MQTT: empty retained payload clears it
            else:
                self.retained[topic] = payload
        for pattern, cb in list(self.subs):
            if matches(pattern, topic):
                cb(topic, payload)
