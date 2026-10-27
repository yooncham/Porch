import asyncio
import stat

import pytest
from fastapi.testclient import TestClient
from thinqconnect import ThinQAPIException

from porch_hub.drivers import CommandRejected, DeviceOffline, DriverError, RemoteControlDisabled
from porch_hub.drivers.thinq import FakeThinQCloud, ThinQBinding, ThinQDriver
from porch_hub.drivers.thinq.connect import _translate, load_client_id, thinq_driver_from_env
from porch_hub.idempotency import IdempotencyStore
from porch_hub.main import Settings, create_app
from porch_hub.models import CommandRequest
from porch_hub.registry import DeviceRegistry
from porch_hub.router import CommandRouter

FAST = Settings(confirm_timeout_s=0.3, poll_interval_s=0.02)
REMOTE_OFF = "Remote Control switch disabled on physical appliance"
DRYER, WASHER, VACUUM, PURIFIER = "home-1/dryer", "home-1/washer", "home-1/vacuum", "home-1/purifier"


def lg_home() -> tuple[FakeThinQCloud, ThinQDriver]:
    cloud = FakeThinQCloud()
    cloud.add_dryer("lg-dryer", "Dryer")
    cloud.add_washer("lg-washer", "Washer")
    cloud.add_robot_cleaner("lg-vacuum", "Robot vacuum")
    cloud.add_air_purifier("lg-purifier", "Air purifier")
    bindings = {lg: ThinQBinding(f"home-1/{lg[3:]}", "Laundry") for lg in ("lg-dryer", "lg-washer", "lg-vacuum", "lg-purifier")}
    driver = ThinQDriver(cloud, bindings=bindings, read_back_interval_s=None, read_back_timeout_s=None)
    return cloud, driver


@pytest.fixture
def lg():
    cloud, driver = lg_home()
    with TestClient(create_app([driver], FAST)) as client:
        yield cloud, client


def command(client, device_id, name, key, params=None):
    return client.post(f"/devices/{device_id}/commands",
                       json={"command": name, "params": params or {}, "idempotency_key": key}).json()


async def started(driver: ThinQDriver) -> DeviceRegistry:
    registry = DeviceRegistry([driver])
    await driver.start()
    await registry.refresh()
    return registry


# Discovery and mapping

def test_discovers_and_normalizes_each_appliance_type(lg):
    _, client = lg
    devices = {d["id"]: d for d in client.get("/devices").json()}
    assert {d["device_type"] for d in devices.values()} == {
        "LaundryDryer", "LaundryWasher", "RoboticVacuumCleaner", "AirPurifier"}
    assert all(d["primary_control_path"] == "lg_thinq" for d in devices.values())

    dryer = devices[DRYER]["clusters"]
    assert dryer["OperationalState"] == {"attributes": {"OperationalState": "Stopped"}, "commands": ["Start", "Stop"]}
    assert dryer["PorchLaundryExtension"]["attributes"] == {
        "RemoteStartAllowed": True, "RelativeHourToStart": 0, "RelativeMinuteToStart": 0,
        "VendorRunState": "POWER_OFF"}
    assert devices[WASHER]["clusters"]["OperationalState"]["attributes"]["OperationalState"] == "Stopped"
    vacuum = devices[VACUUM]["clusters"]
    assert vacuum["RvcOperationalState"]["attributes"] == {"OperationalState": "Charging"}
    assert vacuum["PowerSource"]["attributes"] == {"BatPercentRemaining": 200}  # Matter counts half percents
    purifier = devices[PURIFIER]["clusters"]
    assert purifier["OnOff"]["attributes"] == {"OnOff": False}
    assert purifier["AirQuality"]["attributes"] == {"AirQuality": "Good"}
    assert purifier["Pm25ConcentrationMeasurement"]["attributes"] == {"MeasuredValue": 8}
    assert purifier["FanControl"]["attributes"] == {"FanMode": "Auto"}


def test_unbound_appliances_get_ids_and_unmapped_types_are_skipped():
    async def scenario():
        cloud = FakeThinQCloud()
        cloud.add_dryer("ABCDEF0123456789", "Basement Dryer")
        cloud._add("fridge-1", "DEVICE_REFRIGERATOR", "Fridge", {"property": {}}, {})
        driver = ThinQDriver(cloud)
        await driver.start()
        return await driver.list_devices()

    devices = asyncio.run(scenario())
    assert [(d.id, d.room) for d in devices] == [("home-1/basement-dryer-abcdef", "Unassigned")]


# Guardrails

def test_start_refused_when_remote_control_is_off(lg):
    cloud, client = lg
    cloud.set_remote_control("lg-dryer", False)
    body = command(client, DRYER, "Start", "k1")
    assert body["status"] == "failed"
    assert body["error"] == REMOTE_OFF
    assert cloud.controls == []  # never sent to LG

    assert command(client, DRYER, "SetDelayStart", "k2", {"hours": 2})["error"] == REMOTE_OFF
    # Stopping is still allowed.
    assert command(client, DRYER, "Stop", "k3")["status"] == "done"


def test_remote_control_check_reads_fresh_status_without_mqtt(lg):
    cloud, client = lg
    cloud.drop_connection()
    cloud._on_message = None  # the machine changes and no report arrives
    cloud.set_remote_control("lg-washer", False)
    assert command(client, WASHER, "Start", "k1")["error"] == REMOTE_OFF


def test_lg_remote_off_error_code_maps_to_the_same_exception():
    err = _translate(ThinQAPIException("2301", "Command not supported in remote off", {}))
    assert isinstance(err, RemoteControlDisabled) and str(err) == REMOTE_OFF
    assert isinstance(_translate(ThinQAPIException("1222", "Not connected device", {})), DeviceOffline)
    assert isinstance(_translate(ThinQAPIException("2302", "Not supported in state", {})), CommandRejected)
    exceeded = _translate(ThinQAPIException("1306", "Exceeded API calls", {}))
    assert type(exceeded) is DriverError and "EXCEEDED_API_CALLS" in str(exceeded)


def test_profile_limits_are_enforced_before_sending(lg):
    cloud, client = lg
    too_long = command(client, DRYER, "SetDelayStart", "k1", {"hours": 20})
    assert too_long["status"] == "failed" and "from 0 to 19, not 20" in too_long["error"]
    minutes = command(client, DRYER, "SetDelayStart", "k2", {"hours": 2, "minutes": 30})
    assert "does not accept timer.relativeMinuteToStart remotely" in minutes["error"]
    assert cloud.controls == []


# Commands and read-back

def test_washer_commands_carry_the_tub_location(lg):
    cloud, client = lg
    body = command(client, WASHER, "Start", "k1")
    assert body["status"] == "done"
    assert body["state_after"]["PorchLaundryExtension"]["attributes"]["VendorRunState"] == "RUNNING"
    assert cloud.controls == [("lg-washer", {"location": {"locationName": "MAIN"},
                                             "operation": {"washerOperationMode": "START"}})]


def test_vacuum_start_and_go_home(lg):
    _, client = lg
    assert command(client, VACUUM, "Start", "k1")["state_after"]["RvcOperationalState"]["attributes"] == {
        "OperationalState": "Running"}
    home = command(client, VACUUM, "GoHome", "k2")
    assert home["status"] == "done"
    assert home["state_after"]["RvcOperationalState"]["attributes"]["OperationalState"] == "SeekingCharger"


def test_purifier_power(lg):
    _, client = lg
    assert command(client, PURIFIER, "On", "k1")["state_after"]["OnOff"]["attributes"] == {"OnOff": True}
    # LG purifiers expose On and Off only.
    assert client.post(f"/devices/{PURIFIER}/commands",
                       json={"command": "Toggle", "idempotency_key": "k3"}).status_code == 422


def test_offline_appliance(lg):
    cloud, client = lg
    cloud.set_online("lg-dryer", False)
    assert client.get(f"/devices/{DRYER}/state").status_code == 503
    assert "not connected" in command(client, DRYER, "Start", "k1")["error"]


# MQTT state sync

def test_push_updates_registry_without_an_api_read():
    async def scenario():
        cloud, driver = lg_home()
        registry = await started(driver)
        cloud.machine_update("lg-dryer", {"runState": {"currentState": "DRYING"}})
        return registry.get(DRYER)

    dryer = asyncio.run(scenario())
    assert dryer.clusters["OperationalState"].attributes["OperationalState"] == "Running"
    extension = dryer.clusters["PorchLaundryExtension"].attributes
    assert extension["VendorRunState"] == "DRYING"
    assert extension["RemoteStartAllowed"] is True  # the partial report kept the rest of the status


def test_push_from_a_command_reaches_registry_and_washer_list_reports_merge():
    async def scenario():
        cloud, driver = lg_home()
        registry = await started(driver)
        router = CommandRouter(registry, IdempotencyStore(), confirm_timeout_s=1, poll_interval_s=0.01)
        response = await router.execute(WASHER, CommandRequest(command="SetDelayStart", params={"hours": 4},
                                                               idempotency_key="k"))
        return response, registry.get(WASHER)

    response, washer = asyncio.run(scenario())
    assert response.status == "done"
    assert washer.clusters["PorchLaundryExtension"].attributes["RelativeHourToStart"] == 4


def test_reconnect_resyncs_changes_missed_while_offline():
    async def scenario():
        cloud, driver = lg_home()
        registry = await started(driver)
        cloud.drop_connection()
        on_message, cloud._on_message = cloud._on_message, None
        cloud.machine_update("lg-vacuum", {"runState": {"currentState": "WORKING"}})  # report lost
        cloud._on_message = on_message
        cloud.drop_connection(connected=True)
        await asyncio.gather(*driver._tasks)
        return registry.get(VACUUM)

    vacuum = asyncio.run(scenario())
    assert vacuum.clusters["RvcOperationalState"].attributes["OperationalState"] == "Running"


def test_registry_ignores_pushes_for_unknown_or_unbased_devices():
    async def scenario():
        cloud, driver = lg_home()
        registry = await started(driver)
        driver._on_push({"pushType": "DEVICE_STATUS", "deviceId": "someone-elses", "report": {}})
        driver._on_push({"pushType": "DEVICE_PUSH", "deviceId": "lg-dryer", "pushCode": "DRYING_IS_COMPLETE"})
        return registry.get(DRYER)

    assert asyncio.run(scenario()).clusters["OperationalState"].attributes["OperationalState"] == "Stopped"


# SDK adapter (ThinQApi and ThinQMQTTClient stubbed; no LG traffic)

class StubApi:
    def __init__(self, *args):
        self.subscribed = []

    async def async_post_device_control(self, device_id, payload):
        raise ThinQAPIException("2301", "Command not supported in remote off", {})

    async def async_post_event_subscribe(self, device_id):
        self.subscribed.append(("event", device_id))

    async def async_post_push_subscribe(self, device_id):
        raise ThinQAPIException("1207", "Already subscribed push", {})


class StubMQTT:
    def __init__(self, api, client_id, on_message_received, on_connection_change, **kwargs):
        self.received, self.on_connection_change = on_message_received, on_connection_change
        self.is_connected = False

    async def async_init(self):
        pass

    async def async_prepare_mqtt(self):
        return True

    async def async_connect_mqtt(self):
        self.is_connected = True
        self.on_connection_change(True)

    async def async_disconnect(self):
        self.is_connected = False


def test_sdk_adapter_maps_errors_and_hops_mqtt_callbacks_onto_the_loop(monkeypatch):
    import threading

    from porch_hub.drivers.thinq import connect

    monkeypatch.setattr(connect, "ThinQApi", StubApi)
    monkeypatch.setattr(connect, "AsyncThinQMQTTClient", StubMQTT)

    async def scenario():
        cloud = connect.ThinQConnectCloud("token", "US", "thinq-open-test")
        await cloud.start()
        with pytest.raises(RemoteControlDisabled):
            await cloud.control("lg-dryer", {"operation": {"dryerOperationMode": "START"}})

        loop_thread = threading.get_ident()
        seen, connected = [], []
        delivered = asyncio.Event()

        def on_message(message):
            seen.append((message, threading.get_ident()))
            delivered.set()

        await cloud.subscribe(["lg-dryer"], on_message, connected.append)
        # awscrt calls back from its own thread with raw bytes.
        mqtt = cloud._mqtt
        threading.Thread(target=lambda: mqtt.received("topic", b'{"pushType": "DEVICE_STATUS"}', False, 1, False)).start()
        await asyncio.wait_for(delivered.wait(), 1)
        subscribed = cloud._api.subscribed
        await cloud.close()
        return seen, connected, loop_thread, subscribed

    seen, connected, loop_thread, subscribed = asyncio.run(scenario())
    assert seen == [({"pushType": "DEVICE_STATUS"}, loop_thread)]
    assert connected == [True]
    assert subscribed == [("event", "lg-dryer")]  # the 1207 "already subscribed" push error was ignored


# Configuration

def test_client_id_is_created_once_and_kept_private(tmp_path):
    path = tmp_path / "porch" / "thinq-client-id"
    first = load_client_id(path)
    assert first.startswith("thinq-open-")
    assert load_client_id(path) == first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_driver_from_env(tmp_path):
    driver = thinq_driver_from_env({
        "LG_THINQ_PAT": "test-token",
        "LG_THINQ_CLIENT_ID_FILE": str(tmp_path / "id"),
        "LG_THINQ_BINDINGS": '{"abc": {"porch_id": "home-1/laundry-dryer", "room": "Laundry"}}',
    })
    assert driver._bindings["abc"] == ThinQBinding("home-1/laundry-dryer", "Laundry")
    assert driver.read_back_interval_s == 1.0
