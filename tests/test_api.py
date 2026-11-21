"""Backend tests. Run: VOLT_DATA=$(mktemp -d) pytest -q

Uses the MockRobot (no parol6 needed), so nothing ever moves.
"""
import os
import tempfile

os.environ.setdefault("VOLT_DATA", tempfile.mkdtemp())

from fastapi.testclient import TestClient  # noqa: E402

from volt.server import create_app  # noqa: E402

app = create_app(simulate=True)
hub = app.state.hub
T = {"x-volt-token": app.state.settings.get("security")["token"]}


def client():
    c = TestClient(app, raise_server_exceptions=False)
    c.__enter__()
    hub.robot.connect()
    hub.state = "connected"
    return c


c = client()


def _pairing(on: bool):
    hub.settings.data["security"]["require_token"] = on


def test_pairing_off_by_default():
    _pairing(False)
    assert c.get("/api/state").status_code == 200
    assert c.get("/api/hello").json()["pairing"] is False


def test_auth_required_when_pairing_on():
    _pairing(True)
    try:
        assert c.get("/api/state").status_code == 401
        assert c.get("/api/state", headers=T).status_code == 200
        assert c.get("/api/hello").json()["pairing"] is True
    finally:
        _pairing(False)


def test_motion_needs_enable_then_works():
    hub.stop()
    r = c.post("/api/home", headers=T)
    assert r.status_code >= 400
    assert c.post("/api/enable", headers=T).json()["ok"]
    assert c.post("/api/home", headers=T).json()["ok"]


def test_joint_limits_refused():
    c.post("/api/enable", headers=T)
    r = c.post("/api/move", headers=T, json={"joints": [500, 0, 0, 0, 0, 0]})
    assert r.status_code == 409 and "outside limits" in r.json()["error"]


def test_speed_is_capped():
    assert hub._clamp_speed(100) == hub.settings.get("motion")["speed_cap_pct"]
    assert hub._clamp_speed(100, ai=True) == hub.settings.get("ai")["max_ai_speed_pct"]


def test_poses_roundtrip():
    c.post("/api/enable", headers=T)
    c.post("/api/home", headers=T)
    assert c.post("/api/poses", headers=T, json={"name": "test_pose"}).json()["ok"]
    assert "test_pose" in c.get("/api/poses", headers=T).json()
    assert c.post("/api/poses/test_pose/go", headers=T, json={}).json()["ok"]
    c.delete("/api/poses/test_pose", headers=T)


def test_ws_jog_and_stop():
    c.post("/api/enable", headers=T)
    before = hub.robot.get_angles()[0]
    with c.websocket_connect(f"/ws?token={T['x-volt-token']}") as ws:
        assert ws.receive_json()["t"] == "state"
        for _ in range(3):
            ws.send_json({"t": "jog", "joint": 1, "dir": 1, "speed": 20})
        ws.send_json({"t": "jog_end"})
        ws.send_json({"t": "stop"})
        ws.receive_json()
    assert hub.robot.get_angles()[0] > before
    assert not hub.is_enabled()


def test_ws_rejects_bad_token_when_pairing_on():
    import pytest
    from starlette.websockets import WebSocketDisconnect
    _pairing(True)
    try:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws?token=nope") as ws:
                ws.receive_json()
    finally:
        _pairing(False)


def test_diagnostics_run():
    res = c.get("/api/diagnostics", headers=T).json()
    ids = {r["id"] for r in res}
    assert {"api", "controller", "gripper", "camera", "brain"} <= ids
    assert all(r["status"] in ("pass", "warn", "fail", "skip") for r in res)


def test_ai_off_and_chat_drops_motion():
    from volt import ai
    hub.settings.update({"ai": {"mode": "off"}})
    assert "off" in c.post("/api/ai/command", headers=T, json={"text": "hi"}).json()["reply"]
    hub.settings.update({"ai": {"mode": "chat"}})
    ok, bad = ai.validate_plan(hub, [{"action": "home", "args": {}}, {"action": "look", "args": {}}])
    assert [s["action"] for s in ok] == ["look"] and bad[0]["action"] == "home"
    hub.settings.update({"ai": {"mode": "supervised"}})
    ok, bad = ai.validate_plan(hub, [{"action": "rm_rf", "args": {}}])
    assert not ok and bad


def test_settings_cannot_touch_token():
    tok = T["x-volt-token"]
    c.patch("/api/settings", headers=T, json={"security": {"token": "x", "require_token": True},
                                              "motion": {"speed_cap_pct": 500}})
    assert hub.settings.get("security")["token"] == tok
    assert hub.settings.get("security")["require_token"] is False   # phone can't flip pairing
    assert hub.settings.get("motion")["speed_cap_pct"] == 100
    c.patch("/api/settings", headers=T, json={"motion": {"speed_cap_pct": 30}})


def test_memory_journal_records_and_syncs():
    start = hub.journal._next - 1
    c.post("/api/enable", headers=T)
    c.post("/api/home", headers=T)
    c.post("/api/poses", headers=T, json={"name": "mem_pose"})
    c.post("/api/move", headers=T, json={"joints": [499, 0, 0, 0, 0, 0]})
    c.patch("/api/settings", headers=T, json={"motion": {"speed_cap_pct": 25}})
    c.post("/api/memory/note", headers=T, json={"text": "J3 sags with the MSG gripper"})
    new = c.get(f"/api/memory?since={start}", headers=T).json()
    kinds = [e["kind"] for e in new]
    for k in ("enabled", "homed", "pose_saved", "refused", "settings_changed", "note"):
        assert k in kinds, (k, kinds)
    assert all(e["text"] for e in new)
    assert [e["id"] for e in new] == sorted(e["id"] for e in new)
    c.patch("/api/settings", headers=T, json={"motion": {"speed_cap_pct": 30}})
    c.delete("/api/poses/mem_pose", headers=T)


def test_stop_works_from_http_and_websocket():
    """Regression: STOP used to call the sync robot client on the event loop and fail."""
    import time as _t
    for via in ("http", "ws"):
        c.post("/api/enable", headers=T)
        assert hub.is_enabled()
        hub.robot._enabled = True
        n_err = sum(1 for e in hub.events if "STOP failed" in e["msg"])
        if via == "http":
            assert c.post("/api/stop", headers=T).json()["ok"]
        else:
            with c.websocket_connect(f"/ws?token={T['x-volt-token']}") as ws:
                ws.receive_json()
                ws.send_json({"t": "stop"})
                ws.receive_json()
        _t.sleep(0.2)
        assert not hub.robot.is_enabled(), f"robot not halted via {via}"
        assert sum(1 for e in hub.events if "STOP failed" in e["msg"]) == n_err, f"STOP failed via {via}"
