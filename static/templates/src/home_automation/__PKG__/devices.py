"""Simulated devices. Each listens on <base>/set and reports on <base>/state (retained)."""
import json


class Light:
    def __init__(self, broker, name):
        self.broker, self.base, self.on = broker, f"home/{name}/light", False
        broker.subscribe(self.base + "/set", self._set)
        self._report()

    def _set(self, topic, payload):
        self.on = json.loads(payload).get("on", self.on)
        self._report()

    def _report(self):
        self.broker.publish(self.base + "/state", json.dumps({"on": self.on}), retain=True)


class Thermostat:
    """Temperature moves 10% toward outside each minute, plus 1.5 C/min while heating."""

    def __init__(self, broker, name, temp=19.0, outside=8.0):
        self.broker, self.base = broker, f"home/{name}/thermostat"
        self.temp, self.outside, self.heating = temp, outside, False
        broker.subscribe(self.base + "/set", self._set)

    def _set(self, topic, payload):
        self.heating = bool(json.loads(payload).get("heating", self.heating))

    def tick(self):
        self.temp += (self.outside - self.temp) * 0.1 + (1.5 if self.heating else 0.0)
        self.broker.publish(self.base + "/state", json.dumps({"temp": round(self.temp, 2), "heating": self.heating}), retain=True)


class MotionSensor:
    def __init__(self, broker, name, pattern):
        self.broker, self.base, self.pattern = broker, f"home/{name}/motion", pattern   # pattern: minute -> bool

    def tick(self, minute):
        if self.pattern(minute):
            self.broker.publish(self.base, json.dumps({"motion": True, "minute": minute}))
