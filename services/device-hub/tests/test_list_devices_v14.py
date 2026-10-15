"""Hub 1.4.0: list_devices returns registry_version (content hash), read-only scenes and settle_s; output is valid
against ListDevicesResult in contracts/openapi/device-hub-internal.yaml."""
from __future__ import annotations

import jsonschema
import yaml

from conftest import CONTRACTS, curtain_ep, make_announce

SPEC = yaml.safe_load((CONTRACTS / "openapi" / "device-hub-internal.yaml").read_text(encoding="utf-8"))
VALIDATOR = jsonschema.Draft202012Validator({"$ref": "#/components/schemas/ListDevicesResult",
                                             "components": SPEC["components"]})


def lst(home, **kw):
    out = home.hub.executor.list_devices(**kw)
    VALIDATOR.validate(out)
    return out


def light_scene(home, name="Evening"):
    return home.hub.scenes.create_scene(
        name, [{"target": {"endpoint_id": home.ids["light"]}, "capability": "power", "value": True}], "u", "t")


def test_real_output_is_valid_and_has_new_fields(home):
    out = lst(home)
    assert set(out) == {"registry_version", "devices", "scenes"} and out["scenes"] == []
    assert isinstance(out["registry_version"], int) and out["registry_version"] >= 0


def test_settle_s_on_motion_capabilities_only(home):
    home.add_node(make_announce("esp32-000000000003", [curtain_ep("c1", settle_s=2)]), {"c1": ("Curtain", "Bedroom", None)})
    caps = {c["id"]: c for d in lst(home)["devices"] for c in d["capabilities"]}
    assert caps["position"]["settle_s"] == 2
    assert all("settle_s" not in c for k, c in caps.items() if k != "position")


def test_scenes_listed_without_actions_and_not_filtered(home):
    sc = light_scene(home)
    out = lst(home, room="Hall")
    assert out["scenes"] == [{"scene_id": sc["scene_id"], "name": "Evening"}]
    assert "actions" not in str(out["scenes"])


def test_registry_version_is_stable_and_changes_on_registry_and_scene_changes(home):
    ex = home.hub.executor
    v0 = lst(home)["registry_version"]
    assert lst(home)["registry_version"] == v0 == lst(home, room="Hall")["registry_version"]
    set_ok = ex.set_state(home.ids["light"], "power", True)
    assert set_ok["ok"] and lst(home)["registry_version"] == v0          # live state does not count
    sc = light_scene(home)
    v1 = lst(home)["registry_version"]
    assert v1 != v0
    home.hub.admin.patch_endpoint(home.ids["fan"], {"name": "Big fan"}, "t")
    v2 = lst(home)["registry_version"]
    assert v2 not in (v0, v1)
    home.hub.scenes.delete_scene(sc["scene_id"], "t")
    assert lst(home)["registry_version"] not in (v1, v2)
