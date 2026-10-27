import asyncio

import pytest
from fastapi.testclient import TestClient

from porch_hub import clusters as c
from porch_hub.drivers import GoogleHomeDriver
from porch_hub.idempotency import IdempotencyStore
from porch_hub.main import Settings, create_app
from porch_hub.models import CommandRequest, ControlPath, Device, DeviceType, utcnow
from porch_hub.registry import DeviceRegistry
from porch_hub.router import CommandRouter
from porch_hub.seed import demo_home

FAST = Settings(confirm_timeout_s=0.3, poll_interval_s=0.02)
LOCK = "home-1/front-door-lock"
HEATER = "home-1/space-heater-plug"
LAMP = "home-1/desk-lamp-plug"
DRYER = "home-1/laundry-dryer"
VALVE = "home-1/main-water-valve"


@pytest.fixture
def home():
    return demo_home(simulate_latency=False)


@pytest.fixture
def client(home):
    with TestClient(create_app(home.drivers, FAST)) as test_client:
        yield test_client


def command(client, device_id, name, key, params=None):
    return client.post(f"/devices/{device_id}/commands",
                       json={"command": name, "params": params or {}, "idempotency_key": key})


def test_list_devices_merges_platforms(client):
    resp = client.get("/devices")
    assert resp.status_code == 200
    devices = {d["id"]: d for d in resp.json()}
    assert len(devices) == 8  # the lock seen in Google Home is not listed twice

    lock = devices[LOCK]
    assert lock["primary_control_path"] == "matter"
    assert lock["control_paths"] == ["matter", "google_home"]  # merged by serial number
    assert "path_device_ids" not in lock
    assert lock["clusters"]["DoorLock"]["attributes"] == {"LockState": "Locked"}
    assert devices[DRYER]["primary_control_path"] == "lg_thinq"
    assert devices[DRYER]["device_type"] == "LaundryDryer"
    assert devices[HEATER]["primary_control_path"] == "google_home"
    for device in devices.values():
        assert set(device) >= {"id", "name", "room", "device_type", "online",
                               "primary_control_path", "clusters", "last_updated_at"}


def test_read_state_translates_lg_fields_to_matter(client):
    resp = client.get(f"/devices/{DRYER}/state")
    assert resp.status_code == 200
    state = resp.json()
    assert state["OperationalState"]["attributes"] == {"OperationalState": "Stopped"}
    assert state["PorchLaundryExtension"]["attributes"]["RemoteStartAllowed"] is True


def test_command_done_after_read_back(client):
    resp = command(client, HEATER, "On", "k-on")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "done"
    assert body["state_after"]["OnOff"]["attributes"]["OnOff"] is True
    assert body["idempotency_key"] == "k-on"
    assert isinstance(body["latency_ms"], int)
    assert body["error"] is None
    assert client.get(f"/devices/{HEATER}/state").json()["OnOff"]["attributes"]["OnOff"] is True


def test_repeated_key_replays_without_running_again(client):
    first = command(client, LAMP, "Toggle", "k-toggle").json()
    second = command(client, LAMP, "Toggle", "k-toggle").json()
    assert second == first
    # Toggle ran once: a second execution would have switched the lamp back off.
    assert client.get(f"/devices/{LAMP}/state").json()["OnOff"]["attributes"]["OnOff"] is True


def test_reused_key_for_a_different_command_conflicts(client):
    assert command(client, LAMP, "On", "k-reuse").status_code == 200
    assert command(client, LAMP, "Off", "k-reuse").status_code == 409
    assert command(client, HEATER, "On", "k-reuse").status_code == 409


def test_unlock_routes_through_matter_not_google(client):
    body = command(client, LOCK, "UnlockDoor", "k-unlock").json()
    assert body["status"] == "done"
    assert body["state_after"]["DoorLock"]["attributes"]["LockState"] == "Unlocked"


def test_google_only_lock_cannot_unlock():
    lock = Device(id="home-1/back-door-lock", name="Back door", room="Garden",
                  device_type=DeviceType.DOOR_LOCK, online=True,
                  primary_control_path=ControlPath.GOOGLE_HOME,
                  clusters={c.DOOR_LOCK: c.door_lock(True)}, last_updated_at=utcnow())
    with TestClient(create_app([GoogleHomeDriver([lock])], FAST)) as client:
        body = command(client, lock.id, "UnlockDoor", "k-g-unlock").json()
    assert body["status"] == "failed"
    assert "Google Home blocks UnlockDoor" in body["error"]
    assert body["state_after"]["DoorLock"]["attributes"]["LockState"] == "Locked"


def test_lg_command_accepted_but_ignored_fails_read_back(client, home):
    home.thinq_cloud.set_unresponsive("lg-dryer-01")
    body = command(client, DRYER, "Start", "k-start-stuck").json()
    assert body["status"] == "failed"
    assert "did not report OperationalState" in body["error"]
    assert body["state_after"]["OperationalState"]["attributes"]["OperationalState"] == "Stopped"
    assert body["latency_ms"] >= 300  # waited out the confirm timeout


def test_lg_start_confirmed_after_cloud_delay():
    home = demo_home(simulate_latency=False)
    home.thinq_cloud._apply_delay_s = 0.1  # the dryer reports the change a beat after the API says OK
    with TestClient(create_app(home.drivers, FAST)) as client:
        body = command(client, DRYER, "Start", "k-start").json()
    assert body["status"] == "done"
    assert body["state_after"]["OperationalState"]["attributes"]["OperationalState"] == "Running"
    assert body["latency_ms"] >= 100


def test_lg_delay_start(client):
    body = command(client, DRYER, "SetDelayStart", "k-delay", {"hours": 3}).json()
    assert body["status"] == "done"
    attrs = body["state_after"]["PorchLaundryExtension"]["attributes"]
    assert (attrs["RelativeHourToStart"], attrs["RelativeMinuteToStart"]) == (3, 0)


def test_validation_errors_are_not_cached(client):
    assert command(client, DRYER, "SetDelayStart", "k-bad", {"hours": 99}).status_code == 422
    assert command(client, HEATER, "LockDoor", "k-bad").status_code == 422
    assert command(client, "home-1/nope", "On", "k-bad").status_code == 404
    # The key was never consumed, so a corrected request goes through.
    assert command(client, DRYER, "SetDelayStart", "k-bad", {"hours": 2}).json()["status"] == "done"


def test_offline_device(client, home):
    home.matter.set_online(VALVE, False)
    assert client.get(f"/devices/{VALVE}/state").status_code == 503
    body = command(client, VALVE, "Close", "k-offline").json()
    assert body["status"] == "failed"
    assert "offline" in body["error"]
    assert body["state_after"]["ValveConfigurationAndControl"]["attributes"]["CurrentState"] == "Open"


def test_concurrent_requests_with_one_key_run_once():
    async def scenario():
        home = demo_home(simulate_latency=True)
        registry = DeviceRegistry(home.drivers)
        await registry.refresh()
        router = CommandRouter(registry, IdempotencyStore(), confirm_timeout_s=1, poll_interval_s=0.02)
        request = CommandRequest(command="Toggle", idempotency_key="k-race")
        results = await asyncio.gather(*(router.execute(LAMP, request) for _ in range(5)))
        state = await home.matter.read_state(LAMP)
        return results, state

    results, state = asyncio.run(scenario())
    assert all(r == results[0] for r in results)
    assert state["OnOff"].attributes["OnOff"] is True
