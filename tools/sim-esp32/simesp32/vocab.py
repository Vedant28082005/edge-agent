"""Capability vocabulary (contracts/capabilities.md). tests/test_contract_agreement.py checks this table
against announce.schema.json, so a contract change that is not mirrored here fails the tests."""

# id -> (kind, type, default_min, default_max, fixed_values)
VOCAB = {
    "power": ("actuator", "bool", None, None, None),
    "brightness": ("actuator", "int", 0, 100, None),
    "color_temp": ("actuator", "int", None, None, None),
    "speed": ("actuator", "int", None, None, None),
    "position": ("actuator", "int", 0, 100, None),
    "lock": ("actuator", "enum", None, None, ["locked", "unlocked"]),
    "target_temp": ("actuator", "float", None, None, None),
    "mode": ("actuator", "enum", None, None, None),
    "temperature": ("sensor", "float", None, None, None),
    "humidity": ("sensor", "float", None, None, None),
    "illuminance": ("sensor", "float", None, None, None),
    "motion": ("sensor", "bool", None, None, None),
    "contact": ("sensor", "enum", None, None, ["open", "closed"]),
    "power_w": ("sensor", "float", None, None, None),
    "current_a": ("sensor", "float", None, None, None),
    "voltage_v": ("sensor", "float", None, None, None),
    "battery": ("sensor", "int", 0, 100, None),
}
ENDPOINT_TYPES = ["light", "fan", "outlet", "lock", "cover", "thermostat", "sensor", "generic"]
UNITS = ["C", "%", "lux", "W", "A", "V", "K", "step"]
DEFAULT_UNIT = {
    "temperature": "C", "humidity": "%", "illuminance": "lux", "power_w": "W", "current_a": "A",
    "voltage_v": "V", "battery": "%", "brightness": "%", "position": "%", "target_temp": "C",
    "color_temp": "K", "speed": "step",
}
