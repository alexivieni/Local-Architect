"""A minute-stepped simulation of one flat."""
from .broker import Broker
from .devices import Light, MotionSensor, Thermostat
from .rules import Engine, Rule


class Simulation:
    def __init__(self, broker=None, start_minute=18 * 60, motion=lambda m: m % 7 == 0):
        self.minute = start_minute
        self.broker = broker or Broker()
        self.rules = Engine(self.broker, lambda: self.minute)
        self.light = Light(self.broker, "hall")
        self.thermo = Thermostat(self.broker, "living")
        self.motion = MotionSensor(self.broker, "hall", motion)
        self.rules.add(Rule("heat on when cold", "home/+/thermostat/state", lambda d: d["temp"] < 18 and not d["heating"],
                            ("home/living/thermostat/set", {"heating": True})))
        self.rules.add(Rule("heat off when warm", "home/+/thermostat/state", lambda d: d["temp"] > 21 and d["heating"],
                            ("home/living/thermostat/set", {"heating": False})))
        self.rules.add(Rule("hall light on motion", "home/hall/motion", lambda d: d.get("motion"),
                            ("home/hall/light/set", {"on": True}), cooldown_min=5, quiet=(23 * 60, 6 * 60)))

    def step(self):
        self.thermo.tick()
        self.motion.tick(self.minute)
        self.minute += 1

    def run(self, minutes):
        for _ in range(minutes):
            self.step()
        return self
