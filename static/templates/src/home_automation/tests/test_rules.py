import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from __PKG__.sim import Simulation  # noqa: E402


def test_thermostat_rules_keep_temperature_in_band():
    sim = Simulation(motion=lambda m: False).run(240)
    temps = [float(p.split('"temp": ')[1].split(",")[0]) for t, p in sim.broker.log if t.endswith("thermostat/state")]
    assert min(temps[60:]) > 16.5 and max(temps) < 23
    assert any("heating\": true" in p for t, p in sim.broker.log if t.endswith("/set"))


def test_motion_turns_light_on_with_cooldown():
    sim = Simulation(motion=lambda m: True).run(12)
    rule = sim.rules.rules[2]
    assert rule.fired == [18 * 60, 18 * 60 + 5, 18 * 60 + 10]
    assert sim.light.on


def test_quiet_hours_wrap_past_midnight():
    sim = Simulation(start_minute=23 * 60 + 30, motion=lambda m: True).run(60)
    assert sim.rules.rules[2].fired == [] and not sim.light.on
    sim = Simulation(start_minute=6 * 60, motion=lambda m: True).run(1)
    assert sim.rules.rules[2].fired == [6 * 60]
