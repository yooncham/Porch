import asyncio
import json

from fastapi.testclient import TestClient

from porch_hub import clusters as c
from porch_hub.digital_copy import DEFAULT_LATENCY_MS
from porch_hub.drivers import BaseDriver
from porch_hub.main import Settings, create_app
from porch_hub.models import ControlPath
from porch_hub.seed import demo_home

LOCK, DRYER, LEAK, VALVE = ("home-1/front-door-lock", "home-1/laundry-dryer",
                            "home-1/kitchen-leak-sensor", "home-1/main-water-valve")
REMOTE_OFF = "Remote Control switch disabled on physical appliance"
FAST = dict(confirm_timeout_s=0.3, poll_interval_s=0.02)


def simulate(client, device_id, command, params=None):
    resp = client.post(f"/devices/{device_id}/simulate", json={"command": command, "params": params or {}})
    assert resp.status_code == 200, resp.text
    return resp.json()


def command(client, device_id, name, key, params=None):
    return client.post(f"/devices/{device_id}/commands",
                       json={"command": name, "params": params or {}, "idempotency_key": key}).json()


def next_event(ws, type_):
    """The next event of one type; the stream also carries mode and command events."""
    while (event := ws.receive_json())["type"] != type_:
        pass
    return event


def lock_state(clusters):
    return clusters["DoorLock"]["attributes"]["LockState"]


# Dry run

def test_simulate_predicts_without_touching_the_device():
    home = demo_home(simulate_latency=False)
    with TestClient(create_app(home.drivers, Settings(**FAST))) as client, client.websocket_connect("/events") as ws:
        result = simulate(client, LOCK, "UnlockDoor")
        live = client.get(f"/devices/{LOCK}/state").json()
        command(client, VALVE, "Close", "k")  # an event that does happen, to show none came before it
        announced = ws.receive_json()
        first_change = next_event(ws, "state_changed")

    assert result == {"simulated_state_after": {"DoorLock": {"attributes": {"LockState": "Unlocked"},
                                                             "commands": ["LockDoor", "UnlockDoor"]}},
                      "would_succeed": True, "estimated_latency_ms": DEFAULT_LATENCY_MS[ControlPath.MATTER],
                      "control_path": "matter", "reason": None}
    assert lock_state(live) == "Locked"
    assert asyncio.run(home.matter.read_state(LOCK))["DoorLock"].attributes["LockState"] == "Locked"
    # The dry run is announced for the stage display, but changes nothing.
    assert (announced["type"], announced["device_id"]) == ("command_simulated", LOCK)
    assert first_change["device_id"] == VALVE


def test_simulate_applies_the_live_paths_rules():
    home = demo_home(simulate_latency=False)
    with TestClient(create_app(home.drivers, Settings(**FAST))) as client:
        client.get("/devices")
        home.thinq_cloud.set_remote_control("lg-dryer-01", False)
        remote_off = simulate(client, DRYER, "Start")
        home.thinq_cloud.set_remote_control("lg-dryer-01", True)
        too_long = simulate(client, DRYER, "SetDelayStart", {"hours": 20})
        delay = simulate(client, DRYER, "SetDelayStart", {"hours": 4})
        home.matter.set_online(LOCK, False)  # Google is the only path left, and it will not unlock
        unlock = simulate(client, LOCK, "UnlockDoor")

    assert (remote_off["would_succeed"], remote_off["reason"]) == (False, REMOTE_OFF)
    assert not too_long["would_succeed"] and "from 0 to 19, not 20" in too_long["reason"]
    assert delay["would_succeed"]
    assert delay["simulated_state_after"]["PorchLaundryExtension"]["attributes"]["RelativeHourToStart"] == 4
    assert delay["estimated_latency_ms"] == DEFAULT_LATENCY_MS[ControlPath.LG_THINQ]
    assert (unlock["would_succeed"], unlock["control_path"]) == (False, "google_home")
    assert "Google Home blocks UnlockDoor" in unlock["reason"]
    assert lock_state(unlock["simulated_state_after"]) == "Locked"  # nothing applied when it would fail


def test_simulate_reports_bad_requests_as_would_fail():
    with TestClient(create_app(demo_home(simulate_latency=False).drivers, Settings(**FAST))) as client:
        unsupported = simulate(client, LEAK, "On")
        bad_params = simulate(client, DRYER, "SetDelayStart", {"hours": "soon"})
        missing = client.post("/devices/home-1/nope/simulate", json={"command": "On"})
    assert not unsupported["would_succeed"] and "does not support 'On'" in unsupported["reason"]
    assert not bad_params["would_succeed"] and "hours" in bad_params["reason"]
    assert missing.status_code == 404


def test_latency_estimate_learns_from_live_commands():
    home = demo_home(simulate_latency=False)
    with TestClient(create_app(home.drivers, Settings(**FAST))) as client:
        before = simulate(client, VALVE, "Close")["estimated_latency_ms"]
        for i in range(10):
            command(client, VALVE, "Close" if i % 2 else "Open", f"k{i}")
        after = simulate(client, VALVE, "Close")["estimated_latency_ms"]
    assert before == 150 and after < 50  # the simulated Matter driver answers in a few ms


# Offline backup mode

class Unreachable(BaseDriver):
    """A live driver with no network: starting, listing or commanding it fails."""

    control_path = ControlPath.MATTER
    calls = 0

    async def start(self):
        Unreachable.calls += 1
        raise OSError("network is unreachable")

    async def list_devices(self):
        Unreachable.calls += 1
        raise OSError("network is unreachable")

    async def read_state(self, device_id):
        Unreachable.calls += 1
        raise OSError("network is unreachable")

    async def send_command(self, device_id, command, params):
        Unreachable.calls += 1
        raise OSError("network is unreachable")


def test_offline_mode_answers_from_the_copy_and_keeps_events_flowing():
    home = demo_home(simulate_latency=False)
    with TestClient(create_app(home.drivers, Settings(demo_offline_mode=True, **FAST))) as client, \
            client.websocket_connect("/events") as ws:
        assert client.get("/demo/offline-mode").json() == {"enabled": True}
        unlocked = command(client, LOCK, "UnlockDoor", "k1")
        event = ws.receive_json()
        replay = command(client, LOCK, "UnlockDoor", "k1")
        listed = {d["id"]: d for d in client.get("/devices").json()}
        state = client.get(f"/devices/{LOCK}/state").json()
        home.thinq_cloud.set_remote_control("lg-dryer-01", False)  # the hub is not listening to LG now
        dryer = command(client, DRYER, "Start", "k2")

    assert (unlocked["status"], unlocked["simulated"], unlocked["control_path"]) == ("done", True, "matter")
    assert unlocked["latency_ms"] < 50
    assert (event["type"], event["device_id"], event["changed"]) == (
        "state_changed", LOCK, {"DoorLock": {"LockState": "Unlocked"}})
    assert replay == unlocked
    assert lock_state(listed[LOCK]["clusters"]) == lock_state(state) == "Unlocked"
    # The hardware was never told.
    assert asyncio.run(home.matter.read_state(LOCK))["DoorLock"].attributes["LockState"] == "Locked"
    assert dryer["status"] == "done"


def test_offline_mode_keeps_the_rules_the_copy_knows():
    home = demo_home(simulate_latency=False)
    remote_off = {"cluster": c.PORCH_LAUNDRY, "attribute": "RemoteStartAllowed", "value": False}
    with TestClient(create_app(home.drivers, Settings(demo_offline_mode=True, **FAST))) as client:
        client.post(f"/demo/devices/{DRYER}/attributes", json=remote_off)
        dryer = command(client, DRYER, "Start", "k1")
        bad = client.post(f"/devices/{LEAK}/commands", json={"command": "On", "idempotency_key": "k2"})
    assert (dryer["status"], dryer["error"], dryer["simulated"]) == ("failed", REMOTE_OFF, True)
    assert bad.status_code == 422


def test_switching_offline_mid_demo_and_back():
    home = demo_home(simulate_latency=False)
    with TestClient(create_app(home.drivers, Settings(**FAST))) as client, client.websocket_connect("/events") as ws:
        client.get("/devices")
        home.matter.set_online(LOCK, False)  # Wi-Fi trouble: the lock drops off Matter, onto Google
        assert client.put("/demo/offline-mode", json={"enabled": True}).json() == {"enabled": True}
        offline_lock = {d["id"]: d for d in client.get("/devices").json()}[LOCK]
        unlocked = command(client, LOCK, "UnlockDoor", "k1")  # Google would refuse; the copy uses Matter
        next_event(ws, "command_executed")
        home.matter.set_online(LOCK, True)
        client.put("/demo/offline-mode", json={"enabled": False})
        resync = next_event(ws, "state_changed")
        back = command(client, LOCK, "LockDoor", "k2")

    assert (offline_lock["online"], offline_lock["primary_control_path"]) == (True, "matter")
    assert (unlocked["status"], unlocked["control_path"], unlocked["simulated"]) == ("done", "matter", True)
    # Back online, the real state replaces the copy, and the UI hears about it.
    assert (resync["type"], resync["changed"]) == ("state_changed", {"DoorLock": {"LockState": "Locked"}})
    assert (back["status"], back["simulated"], back["control_path"]) == ("done", False, "matter")


def test_offline_start_uses_the_rehearsal_snapshot_and_never_calls_drivers(tmp_path):
    snapshot = tmp_path / "digital-copy.json"
    with TestClient(create_app(demo_home(simulate_latency=False).drivers,
                               Settings(digital_copy_path=snapshot, **FAST))) as client:
        command(client, VALVE, "Close", "k1")  # rehearsal state worth keeping
    saved = json.loads(snapshot.read_text())
    assert any(r["id"] == VALVE for r in saved["records"])

    Unreachable.calls = 0
    live = Unreachable()
    others = [d for d in demo_home(simulate_latency=False).drivers if d.control_path != ControlPath.MATTER]
    settings = Settings(demo_offline_mode=True, digital_copy_path=snapshot, **FAST)
    with TestClient(create_app([live, *others], settings)) as client:
        devices = {d["id"]: d for d in client.get("/devices").json()}
        unlocked = command(client, LOCK, "UnlockDoor", "k2")

    assert devices[VALVE]["clusters"]["ValveConfigurationAndControl"]["attributes"]["CurrentState"] == "Closed"
    assert devices[LOCK]["online"] and devices[LOCK]["control_paths"] == ["matter", "google_home"]
    assert unlocked["status"] == "done"
    assert Unreachable.calls == 0

    # A restart keeps what the demo did offline.
    with TestClient(create_app([Unreachable(), *others], settings)) as client:
        assert lock_state(client.get(f"/devices/{LOCK}/state").json()) == "Unlocked"


def test_a_platform_that_cannot_start_does_not_stop_the_hub():
    others = [d for d in demo_home(simulate_latency=False).drivers if d.control_path != ControlPath.MATTER]
    with TestClient(create_app([Unreachable(), *others], Settings(**FAST))) as client:
        devices = client.get("/devices").json()
    assert {d["primary_control_path"] for d in devices} == {"lg_thinq", "google_home"}


def test_shadow_readings_drive_scenes_offline_only():
    home = demo_home(simulate_latency=False)
    leak = {"cluster": c.BOOLEAN_STATE, "attribute": "StateValue", "value": True}
    with TestClient(create_app(home.drivers, Settings(**FAST))) as client:
        assert client.post(f"/demo/devices/{LEAK}/attributes", json=leak).status_code == 409
        client.put("/demo/offline-mode", json={"enabled": True})
        with client.websocket_connect("/events") as ws:
            state = client.post(f"/demo/devices/{LEAK}/attributes", json=leak).json()
            event = ws.receive_json()
    assert state["BooleanState"]["attributes"]["StateValue"] is True
    assert (event["device_id"], event["changed"]) == (LEAK, {"BooleanState": {"StateValue": True}})


def test_offline_start_without_a_snapshot_uses_built_in_devices_and_no_driver():
    Unreachable.calls = 0
    with TestClient(create_app([Unreachable()], Settings(demo_offline_mode=True, **FAST))) as client:
        devices = {d["id"]: d for d in client.get("/devices").json()}
        valve = command(client, VALVE, "Close", "k1")
    assert LOCK in devices and devices[LOCK]["online"]
    assert valve["status"] == "done"
    assert Unreachable.calls == 0
