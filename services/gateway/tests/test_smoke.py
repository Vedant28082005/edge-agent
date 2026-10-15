def test_login_and_devices(gw):
    r = gw.pub.get("/devices", headers=gw.h("admin"))
    assert r.status_code == 200 and len(r.json()) == 6
    r = gw.pub.get("/devices", headers=gw.h("member"))
    assert {e["room"] for e in r.json()} == {"Bedroom", "Kitchen"}
    assert all(e["enabled"] for e in r.json())
