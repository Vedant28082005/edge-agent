"""Device profiles mirroring firmware/examples/*/main.cpp (endpoint ids, capability ids, ranges, boot/failsafe values).

Each profile is the dict form that simesp32.config understands, so the hub-side cross-field rules and the announce
schema are checked at load time. Only `hw` differs from the firmware: it carries a `sim-` prefix, the one place the
announce schema (additionalProperties: false) leaves room to mark a node as simulated."""

import json

PROFILES = {
    # firmware/examples/light: PwmChannel on GPIO25, endpoint l1 (LIGHT)
    "light": {
        "hw": "sim-light",
        "endpoints": [
            {"local_id": "l1", "type": "light", "capabilities": [
                {"id": "power", "boot_value": False, "failsafe_value": False},
                {"id": "brightness", "min": 0, "max": 100, "boot_value": 50},
            ]},
        ],
    },
    # firmware/examples/fan: endpoint ch1 (FAN), power max_on 8 h, speed 1..5
    "fan": {
        "hw": "sim-fan",
        "endpoints": [
            {"local_id": "ch1", "type": "fan", "capabilities": [
                {"id": "power", "boot_value": False, "failsafe_value": False, "max_on_s": 8 * 3600},
                {"id": "speed", "min": 1, "max": 5, "boot_value": 1},
            ]},
        ],
    },
    # firmware/examples/curtain: endpoint c1 (COVER), 20 s travel each way, settle_s 30, 500 ms dead time
    "curtain": {
        "hw": "sim-curtain",
        "endpoints": [
            {"local_id": "c1", "type": "cover",
             "sim": {"travel_time_s": 20, "interlock_s": 0.5, "progress_s": 1},
             "capabilities": [
                 {"id": "position", "min": 0, "max": 100, "boot_value": 0, "settle_s": 30},
             ]},
        ],
    },
    # firmware/examples/dht: endpoint t1 (SENSOR), temperature + humidity every 30 s (slow random walk)
    "dht": {
        "hw": "sim-dht",
        "endpoints": [
            {"local_id": "t1", "type": "sensor", "capabilities": [
                {"id": "temperature", "interval_s": 30,
                 "generator": {"kind": "random_walk", "start": 27.0, "step": 0.15, "min": 15.0, "max": 40.0}},
                {"id": "humidity", "interval_s": 30,
                 "generator": {"kind": "random_walk", "start": 55.0, "step": 0.5, "min": 25.0, "max": 85.0}},
            ]},
        ],
    },
    # firmware/examples/relay4: r1 OUTLET (1 h limit), r2 OUTLET, r3 LIGHT (boots on, failsafe on), r4 OUTLET
    "relay4": {
        "hw": "sim-4relay",
        "endpoints": [
            {"local_id": "r1", "type": "outlet", "capabilities": [
                {"id": "power", "boot_value": False, "failsafe_value": False, "max_on_s": 3600}]},
            {"local_id": "r2", "type": "outlet", "capabilities": [
                {"id": "power", "boot_value": False, "failsafe_value": False}]},
            {"local_id": "r3", "type": "light", "capabilities": [
                {"id": "power", "boot_value": True, "failsafe_value": True}]},
            {"local_id": "r4", "type": "outlet", "capabilities": [
                {"id": "power", "boot_value": False, "failsafe_value": False}]},
        ],
    },
    # firmware/examples/sensors: door (contact, 60 s), hall (motion, 60 s), lux (illuminance, 30 s)
    "sensors": {
        "hw": "sim-sensors",
        "endpoints": [
            {"local_id": "door", "type": "sensor", "capabilities": [
                {"id": "contact", "interval_s": 60}]},
            {"local_id": "hall", "type": "sensor", "capabilities": [
                {"id": "motion", "interval_s": 60,
                 "generator": {"kind": "random_choice", "change_probability": 0.1}}]},
            {"local_id": "lux", "type": "sensor", "capabilities": [
                {"id": "illuminance", "interval_s": 30,
                 "generator": {"kind": "random_walk", "start": 300.0, "step": 15.0, "min": 0.0, "max": 1000.0}}]},
        ],
    },
}


def _room():
    """One node, 9 endpoints (announce limit is 16): the example endpoints side by side. Endpoint ids are those of the
    firmware examples, with the light split into l1/l2; they are unique within the node."""
    P = PROFILES
    l1 = P["light"]["endpoints"][0]
    l2 = {**l1, "local_id": "l2"}
    eps = [l1, l2, *P["fan"]["endpoints"], *P["curtain"]["endpoints"], *P["dht"]["endpoints"], *P["relay4"]["endpoints"]]
    return {"hw": "sim-room", "endpoints": json.loads(json.dumps(eps))}




PROFILES["room"] = _room()
