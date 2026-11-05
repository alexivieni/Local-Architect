# __NAME__

Home automation without the hardware: an MQTT-style broker in memory, simulated devices and rules.

    python -m __PKG__ --minutes 30     # simulate half an hour; prints every message
    python -m pytest -q

## Pieces

    __PKG__/broker.py   topic matching (+ one level, # the rest), retained messages, subscribe/unsubscribe
    __PKG__/devices.py  thermostat (drifts toward outside temp, heats when on), motion sensor, light
    __PKG__/rules.py    "when topic matches and condition holds, publish X", quiet hours, cooldown
    __PKG__/sim.py      a clock-stepped simulation wiring them together

## Moving to a real broker

`Broker.publish(topic, payload, retain)` and `Broker.subscribe(pattern, callback)` are the only calls the rest
uses. Implement the same two over `paho-mqtt` (`pip install paho-mqtt`) and pass it into `Simulation`.
