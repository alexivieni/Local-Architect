"""Rules: when a message on `when` satisfies `condition`, publish `then`. Cooldown and quiet hours are per rule."""
import json
from dataclasses import dataclass, field


@dataclass
class Rule:
    name: str
    when: str                          # topic filter
    condition: callable                # (payload dict) -> bool
    then: tuple                        # (topic, payload dict)
    cooldown_min: int = 0
    quiet: tuple = None                # (start_minute_of_day, end_minute_of_day): do not fire inside
    fired: list = field(default_factory=list)


class Engine:
    def __init__(self, broker, clock):
        self.broker, self.clock, self.rules = broker, clock, []

    def add(self, rule):
        self.rules.append(rule)
        self.broker.subscribe(rule.when, lambda topic, payload, r=rule: self._on(r, payload))

    def _quiet(self, rule, now):
        if not rule.quiet:
            return False
        start, end = rule.quiet
        m = now % 1440
        return start <= m < end if start <= end else (m >= start or m < end)

    def _on(self, rule, payload):
        now = self.clock()
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            return
        if not rule.condition(data) or self._quiet(rule, now):
            return
        if rule.fired and now - rule.fired[-1] < rule.cooldown_min:
            return
        rule.fired.append(now)
        topic, out = rule.then
        self.broker.publish(topic, json.dumps(out))
