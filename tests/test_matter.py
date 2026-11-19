"""The Matter driver against a fake Matter Server, over a real WebSocket."""

import asyncio
import json
import socket

import pytest
from fake_matter_server import FakeMatterServer, node, running_in_thread
from fastapi.testclient import TestClient

from porch_hub.commands import UnsupportedCommand
from porch_hub.drivers.base import CommandRejected
from porch_hub.drivers.matter import MatterBinding, MatterDriver, matter_driver_from_env
from porch_hub.events import EventBus, SubscriberLagged
from porch_hub.idempotency import IdempotencyStore
from porch_hub.main import Settings, create_app
from porch_hub.models import CommandRequest
from porch_hub.registry import DeviceRegistry
from porch_hub.router import CommandRouter
from porch_hub.seed import demo_home

LOCK, LEAK, PLUG, VALVE, OFFLINE = (
    "home-1/front-door-lock", "home-1/matter-2-1", "home-1/matter-3-1", "home-1/matter-4-1", "home-1/matter-5-1")


def nodes() -> list[dict]:
    return [
        node(1, {"0/40/5": "Front door", "0/40/3": "Smart Lock", "1/29/0": [{"deviceType": 10, "revision": 1}],
                 "1/257/0": 1, "1/257/65529": [0, 1, 3]}),
        # An older server keys struct fields by tag number.
        node(2, {"0/40/3": "Leak Sensor", "1/29/0": [{"0": 67, "1": 1}], "1/69/0": False}),
        # Endpoint 2 is a contact sensor: BooleanState too, but not a leak detector, so it is skipped.
        node(3, {"0/40/5": "Desk lamp", "1/29/0": [{"deviceType": 266}], "1/6/0": False, "1/6/65529": [0, 1, 2],
                 "2/29/0": [{"deviceType": 21}], "2/69/0": True}),
        node(4, {"0/40/3": "Water Valve", "1/29/0": [{"deviceType": 66}], "1/129/4": 1, "1/129/65529": [0, 1]}),
        node(5, {"0/40/3": "Plug", "1/29/0": [{"deviceType": 266}], "1/6/0": False}, available=False),
    ]


BINDINGS = {"1/1": MatterBinding(LOCK, "Entry")}


class Hub:
    def __init__(self, server: FakeMatterServer, **driver_options) -> None:
        self.server = server
        self.driver_options = driver_options

    async def __aenter__(self) -> "Hub":
        self.url = await self.server.start(self.driver_options.pop("port", 0))
        self.driver = MatterDriver(self.url, bindings=self.driver_options.pop("bindings", BINDINGS),
                                   reconnect_delay_s=0.05, **self.driver_options)
        self.registry = DeviceRegistry([self.driver], EventBus())
        self.router = CommandRouter(self.registry, IdempotencyStore(), confirm_timeout_s=0.5, poll_interval_s=0.02)
        await self.driver.start()
        await self.registry.refresh()
        self.events = self.registry.events.subscribe()
        return self

    async def __aexit__(self, *exc) -> None:
        self.events.close()
        await self.driver.stop()
        await self.server.stop()

    async def command(self, device_id: str, command: str, key: str):
        return await self.router.execute(device_id, CommandRequest(command=command, idempotency_key=key))

    async def next_event(self, type_: str):
        while True:
            event = await asyncio.wait_for(self.events.get(), 2)
            if event.type == type_:
                return event


def run(scenario):
    return asyncio.run(scenario())


# Discovery

def test_nodes_become_matter_named_devices():
    async def scenario():
        async with Hub(FakeMatterServer(nodes())) as hub:
            return {d.id: d for d in await hub.driver.list_devices()}

    devices = run(scenario)
    assert sorted(devices) == sorted([LOCK, LEAK, PLUG, VALVE, OFFLINE])
    lock = devices[LOCK]
    assert (lock.name, lock.room, lock.device_type, lock.online) == ("Front door", "Entry", "DoorLock", True)
    assert lock.clusters["DoorLock"].attributes == {"LockState": "Locked"}
    assert lock.clusters["DoorLock"].commands == ["LockDoor", "UnlockDoor"]
    assert devices[LEAK].device_type == "WaterLeakSensor"
    assert devices[LEAK].clusters["BooleanState"].attributes == {"StateValue": False}
    plug = devices[PLUG]
    assert (plug.name, plug.device_type) == ("Desk lamp", "OnOffPlug")  # one supported endpoint, no suffix
    assert plug.clusters["OnOff"].commands == ["Off", "On", "Toggle"]
    assert devices[VALVE].clusters["ValveConfigurationAndControl"].attributes == {"CurrentState": "Open"}
    assert devices[OFFLINE].online is False


def test_accepted_command_list_limits_commands():
    plug = node(7, {"1/29/0": [{"deviceType": 266}], "1/6/0": True, "1/6/65529": [0, 1]})  # no Toggle

    async def scenario():
        async with Hub(FakeMatterServer([plug])) as hub:
            device = (await hub.driver.list_devices())[0]
            with pytest.raises(UnsupportedCommand):  # the hub answers 422 before anything is sent
                await hub.command(device.id, "Toggle", "k")
            return device

    assert run(scenario).clusters["OnOff"].commands == ["Off", "On"]


# Color bulbs and energy plugs (Tapo L535E and P110M style)

COLOR_BULB, ENERGY_PLUG = "home-1/matter-8-1", "home-1/matter-9-1"


def color_bulb(node_id: int = 8, device_type: int = 0x010D) -> dict:
    """An extended color light: OnOff, LevelControl and ColorControl on endpoint 1."""
    return node(node_id, {"0/40/1": "TP-Link", "0/40/3": "L535E", "1/29/0": [{"deviceType": device_type}],
                          "1/6/0": False, "1/6/65529": [0, 1, 2], "1/8/0": 254, "1/768/7": 370})


def energy_plug(node_id: int = 9, separate_sensor: bool = False, struct_by_tag: bool = False) -> dict:
    """An On/Off plug with power (mW) and energy (mWh), on its own endpoint or a separate one."""
    ep = 2 if separate_sensor else 1
    energy = {"0": 1_250_000} if struct_by_tag else {"energy": 1_250_000, "startTimestamp": None}
    attributes = {"0/40/3": "P110M", "1/29/0": [{"deviceType": 266}], "1/6/0": True, "1/6/65529": [0, 1, 2],
                  f"{ep}/144/8": 42_500, f"{ep}/145/1": energy}
    if separate_sensor:
        attributes["2/29/0"] = [{"deviceType": 0x0510}]  # Electrical Sensor
    return node(node_id, attributes)


@pytest.mark.parametrize("device_type", [0x010C, 0x010D])
def test_color_bulbs_are_on_off_lights(device_type):
    async def scenario():
        async with Hub(FakeMatterServer([color_bulb(device_type=device_type)])) as hub:
            device = (await hub.driver.list_devices())[0]
            result = await hub.command(device.id, "On", "bulb-on")
            return device, result

    device, result = run(scenario)
    assert (device.id, device.name, device.device_type) == (COLOR_BULB, "L535E", "OnOffLight")
    # Brightness and color are not mapped yet: the bulb is controlled as on/off.
    assert list(device.clusters) == ["OnOff"]
    assert device.clusters["OnOff"].commands == ["Off", "On", "Toggle"]
    assert result.status == "done" and result.state_after["OnOff"].attributes == {"OnOff": True}


@pytest.mark.parametrize("separate_sensor", [False, True])
@pytest.mark.parametrize("struct_by_tag", [False, True])
def test_energy_plug_reports_power_and_energy(separate_sensor, struct_by_tag):
    async def scenario():
        server = FakeMatterServer([energy_plug(separate_sensor=separate_sensor, struct_by_tag=struct_by_tag)])
        async with Hub(server) as hub:
            devices = await hub.driver.list_devices()
            state = await hub.driver.read_state(ENERGY_PLUG)
            return devices, state

    devices, state = run(scenario)
    assert [d.id for d in devices] == [ENERGY_PLUG]  # the sensor endpoint is not a device of its own
    assert devices[0].device_type == "OnOffPlug"
    assert state["ElectricalPowerMeasurement"].attributes == {"ActivePower": 42_500}
    assert state["ElectricalPowerMeasurement"].commands == []
    assert state["ElectricalEnergyMeasurement"].attributes == {"CumulativeEnergyImported": 1_250_000}
    assert state["OnOff"].attributes == {"OnOff": True}


@pytest.mark.parametrize("separate_sensor", [False, True])
def test_a_plug_that_measures_volts_and_amps_lists_them_and_others_are_not_asked(separate_sensor):
    """RMSVoltage (mV) and RMSCurrent (mA) are optional: a plug has them or it does not."""
    ep = 2 if separate_sensor else 1
    measuring = energy_plug(separate_sensor=separate_sensor)
    measuring["attributes"].update({f"{ep}/144/11": 121_104, f"{ep}/144/12": 1_034})

    async def scenario(plug):
        server = FakeMatterServer([plug])
        async with Hub(server) as hub:
            listed = (await hub.driver.list_devices())[0]
            state = await hub.driver.read_state(ENERGY_PLUG)
            await server.set_attribute(9, f"{ep}/144/11", 119_800)
            await asyncio.sleep(0.05)
            pushed = (await hub.driver.list_devices())[0]
            return listed, state, pushed

    listed, state, pushed = run(lambda: scenario(measuring))
    reading = {"ActivePower": 42_500, "RMSVoltage": 121_104, "RMSCurrent": 1_034}
    assert listed.clusters["ElectricalPowerMeasurement"].attributes == reading
    assert state["ElectricalPowerMeasurement"].attributes == reading
    assert pushed.clusters["ElectricalPowerMeasurement"].attributes == {**reading, "RMSVoltage": 119_800}

    # A plug without them: nothing empty is shown, and the read does not ask for what it lacks.
    listed, state, _ = run(lambda: scenario(energy_plug(separate_sensor=separate_sensor)))
    assert listed.clusters["ElectricalPowerMeasurement"].attributes == {"ActivePower": 42_500}
    assert state["ElectricalPowerMeasurement"].attributes == {"ActivePower": 42_500}


def test_power_reading_from_a_separate_sensor_endpoint_updates_the_plug():
    async def scenario():
        server = FakeMatterServer([energy_plug(separate_sensor=True)])
        async with Hub(server) as hub:
            await server.set_attribute(9, "2/144/8", 0)
            event = await hub.next_event("state_changed")
            return event, hub.registry.get(ENERGY_PLUG)

    event, device = run(scenario)
    assert event.device_id == ENERGY_PLUG
    assert device.clusters["ElectricalPowerMeasurement"].attributes == {"ActivePower": 0}


def test_energy_endpoint_is_not_guessed_when_a_node_has_two_plugs():
    two_plugs = node(10, {"1/29/0": [{"deviceType": 266}], "1/6/0": False, "3/29/0": [{"deviceType": 266}],
                          "3/6/0": False, "2/29/0": [{"deviceType": 0x0510}], "2/144/8": 1000})

    async def scenario():
        async with Hub(FakeMatterServer([two_plugs])) as hub:
            return await hub.driver.list_devices()

    devices = run(scenario)
    assert sorted(d.id for d in devices) == ["home-1/matter-10-1", "home-1/matter-10-3"]
    assert all("ElectricalPowerMeasurement" not in d.clusters for d in devices)


def test_a_read_only_binding_shows_the_plugs_readings_and_never_switches_it():
    """The plug that powers the Wi-Fi router: its watts belong on the energy panel, its switch does not."""
    router_plug = {"9/1": MatterBinding("home-1/living-wifi", "Living Room", read_only=True)}

    async def scenario():
        server = FakeMatterServer([energy_plug()])
        async with Hub(server, bindings=router_plug) as hub:
            device = (await hub.driver.list_devices())[0]
            try:
                await hub.command("home-1/living-wifi", "Off", "router-off")
            except UnsupportedCommand as exc:
                refusal = str(exc)
            try:
                await hub.driver.send_command("home-1/living-wifi", "Off", {})  # even asked directly
            except CommandRejected as exc:
                direct = str(exc)
            return device, refusal, direct, await hub.driver.read_state("home-1/living-wifi"), server

    device, refusal, direct, state, server = run(scenario)
    assert (device.id, device.room, device.name) == ("home-1/living-wifi", "Living Room", "P110M")
    assert all(cluster.commands == [] for cluster in device.clusters.values())
    assert device.clusters["ElectricalPowerMeasurement"].attributes == {"ActivePower": 42_500}
    assert "does not support 'Off'" in refusal and "read only" in direct
    assert state["OnOff"].attributes == {"OnOff": True}  # still on
    assert server.commands("device_command") == []  # nothing reached the Matter Server


def test_details_name_what_a_plug_reports_and_never_its_identity_or_network():
    """The full record of a plug like the home's: who made it, its health, its Wi-Fi signal, its
    meter. Its serial number, unique id, label, network interfaces, Wi-Fi network and fabrics are
    in the node too, and none of them is shown."""
    plug = energy_plug()
    plug["attributes"].update({
        "0/40/1": "TP-Link", "0/40/5": "private-node-label", "0/40/10": "1.3.0", "0/40/15": "private-serial",
        "0/40/18": "private-unique-id",
        "0/51/0": [{"name": "private-wlan0", "hardwareAddress": "private-mac", "IPv4Addresses": ["private-ip"]}],
        "0/51/1": 3, "0/51/2": 90_061, "0/51/4": 1,
        "0/54/0": "private-bssid", "0/54/3": 6, "0/54/4": -48,
        "0/62/0": [{"fabricIndex": 1, "label": "private-fabric"}],
        "1/6/16387": 1, "1/144/11": 121_104, "1/144/12": 1_034,
    })
    named = {"9/1": MatterBinding("home-1/plug1", "Living Room", name="Plug1")}

    async def scenario(node):
        async with Hub(FakeMatterServer([node]), bindings=named) as hub:
            return await hub.driver.details("home-1/plug1")

    rows = run(lambda: scenario(plug))
    assert [(r["group"], r["name"], r["value"]) for r in rows] == [
        ("Device", "Maker", "TP-Link"),
        ("Device", "Product", "P110M"),
        ("Device", "Software version", "1.3.0"),
        ("Health", "Restarts", "3"),
        ("Health", "Up since the last restart", "1 d 1 h"),
        ("Health", "Last restart because", "Power on"),
        ("Wi-Fi", "Channel", "6"),
        ("Wi-Fi", "Signal", "-48 dBm"),
        ("Switch", "On", "yes"),
        ("Switch", "After a power cut", "On"),
        ("Power", "Power", "42.5 W"),
        ("Power", "Voltage (RMS)", "121.1 V"),
        ("Power", "Current (RMS)", "1.034 A"),
        ("Energy", "Used, counter", "1.250 kWh"),
    ]
    assert "private" not in json.dumps(rows)

    # Out of reach (the Mac away from home): the last values heard, and it says so first.
    plug["available"] = False
    rows = run(lambda: scenario(plug))
    assert rows[0] == {"group": "Matter", "name": "Reachable now", "value": "no: these are the last values heard"}
    assert {"group": "Power", "name": "Power", "value": "42.5 W"} in rows


def test_a_binding_can_carry_the_homes_own_name_for_a_device():
    """Four identical plugs all report the product's name; the home knows them by its own."""
    named = {"9/1": MatterBinding("home-1/plug1", "Living Room", name="Plug1")}

    async def scenario():
        async with Hub(FakeMatterServer([energy_plug()]), bindings=named) as hub:
            return (await hub.driver.list_devices())[0]

    device = run(scenario)
    assert (device.id, device.name, device.room) == ("home-1/plug1", "Plug1", "Living Room")
    assert device.clusters["OnOff"].commands == ["Off", "On", "Toggle"]  # naming it changes nothing else


def test_energy_readings_do_not_block_plug_commands():
    async def scenario():
        async with Hub(FakeMatterServer([energy_plug()])) as hub:
            return await hub.command(ENERGY_PLUG, "Off", "plug-off")

    result = run(scenario)
    assert result.status == "done"
    assert result.state_after["OnOff"].attributes == {"OnOff": False}
    assert result.state_after["ElectricalPowerMeasurement"].attributes == {"ActivePower": 42_500}


# Commands

def test_lock_and_unlock_are_timed_invokes_confirmed_by_read_back():
    async def scenario():
        async with Hub(FakeMatterServer(nodes())) as hub:
            unlocked = await hub.command(LOCK, "UnlockDoor", "k1")
            locked = await hub.command(LOCK, "LockDoor", "k2")
            return unlocked, locked, hub.server

    unlocked, locked, server = run(scenario)
    assert unlocked.status == "done" and unlocked.state_after["DoorLock"].attributes["LockState"] == "Unlocked"
    assert locked.status == "done" and locked.state_after["DoorLock"].attributes["LockState"] == "Locked"
    sent = server.commands("device_command")
    assert sent[0] == {"node_id": 1, "endpoint_id": 1, "cluster_id": 257, "command_name": "UnlockDoor",
                       "payload": {}, "timed_request_timeout_ms": 5000}
    # The read-back is a fresh read from the device, not the cache.
    assert {"node_id": 1, "attribute_path": ["1/257/0"]} in server.commands("read_attribute")


def test_on_off_toggle():
    async def scenario():
        async with Hub(FakeMatterServer(nodes())) as hub:
            results = [await hub.command(PLUG, cmd, f"k{i}") for i, cmd in enumerate(["On", "Off", "Toggle"])]
            return results, hub.server.commands("device_command")

    results, sent = run(scenario)
    assert [r.state_after["OnOff"].attributes["OnOff"] for r in results] == [True, False, True]
    assert all(r.status == "done" for r in results)
    assert all("timed_request_timeout_ms" not in args for args in sent)


def test_device_rejection_and_unresponsive_device_fail():
    server = FakeMatterServer(nodes())
    server.rejections["UnlockDoor"] = {"error_code": 0, "details": "Command failed", "status": 1}

    async def scenario():
        async with Hub(server) as hub:
            rejected = await hub.command(LOCK, "UnlockDoor", "k1")
            hub.server.unresponsive = True
            ignored = await hub.command(PLUG, "On", "k2")
            return rejected, ignored

    rejected, ignored = run(scenario)
    assert rejected.status == "failed" and "Matter status 0x01" in rejected.error
    assert ignored.status == "failed" and "did not report OnOff" in ignored.error


def test_offline_node_fails_without_sending():
    async def scenario():
        async with Hub(FakeMatterServer(nodes())) as hub:
            result = await hub.command(OFFLINE, "On", "k")
            return result, hub.server.commands("device_command")

    result, sent = run(scenario)
    assert result.status == "failed" and "not reachable" in result.error
    assert sent == []


# Live updates and events

def test_leak_detected_updates_registry_and_emits_event():
    async def scenario():
        async with Hub(FakeMatterServer(nodes())) as hub:
            await hub.server.set_attribute(2, "1/69/0", True)
            event = await hub.next_event("state_changed")
            return event, hub.registry.get(LEAK)

    event, leak = run(scenario)
    assert (event.device_id, event.source) == (LEAK, "matter")
    assert event.changed == {"BooleanState": {"StateValue": True}}
    assert leak.clusters["BooleanState"].attributes["StateValue"] is True


def test_manual_lock_turn_emits_state_and_lock_operation():
    async def scenario():
        async with Hub(FakeMatterServer(nodes())) as hub:
            await hub.server.set_attribute(1, "1/257/0", 2)
            await hub.server.node_event(1, 1, 257, 2, {"lockOperationType": 1, "operationSource": 1})
            state = await hub.next_event("state_changed")
            operation = await hub.next_event("device_event")
            return state, operation, hub.registry.get(LOCK)

    state, operation, lock = run(scenario)
    assert state.changed == {"DoorLock": {"LockState": "Unlocked"}}
    assert (operation.cluster, operation.event) == ("DoorLock", "LockOperation")
    assert operation.data == {"lockOperationType": 1, "operationSource": 1}
    assert lock.clusters["DoorLock"].attributes["LockState"] == "Unlocked"


def test_boolean_state_change_event_alone_updates_state():
    async def scenario():
        async with Hub(FakeMatterServer(nodes())) as hub:
            await hub.server.node_event(2, 1, 69, 0, {"stateValue": True})
            state = await hub.next_event("state_changed")
            change = await hub.next_event("device_event")
            return state, change

    state, change = run(scenario)
    assert state.changed == {"BooleanState": {"StateValue": True}}
    assert (change.cluster, change.event) == ("BooleanState", "StateChange")


def test_node_going_offline_emits_online_changed():
    async def scenario():
        async with Hub(FakeMatterServer(nodes())) as hub:
            await hub.server.set_available(3, False)
            event = await hub.next_event("online_changed")
            return event, hub.registry.get(PLUG)

    event, plug = run(scenario)
    assert (event.device_id, event.online, plug.online) == (PLUG, False, False)


def test_reconnect_resyncs_changes_made_while_disconnected():
    async def scenario():
        async with Hub(FakeMatterServer(nodes())) as hub:
            await hub.server.drop_connections()
            offline = await hub.next_event("online_changed")
            hub.server.nodes[2]["attributes"]["1/69/0"] = True  # reported while the hub was away
            leak = await hub.next_event("state_changed")
            back = await hub.next_event("online_changed")
            return offline, leak, back

    offline, leak, back = run(scenario)
    assert offline.online is False
    assert (leak.device_id, leak.changed) == (LEAK, {"BooleanState": {"StateValue": True}})
    assert back.online is True


def test_hub_starts_before_the_matter_server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    async def scenario():
        server = FakeMatterServer(nodes())
        driver = MatterDriver(f"ws://127.0.0.1:{port}/ws", connect_timeout_s=0.1, reconnect_delay_s=0.05)
        await driver.start()  # returns although nothing is listening yet
        before = await driver.list_devices()
        await server.start(port)
        for _ in range(100):
            if await driver.list_devices():
                break
            await asyncio.sleep(0.02)
        after = await driver.list_devices()
        await driver.stop()
        await server.stop()
        return before, after

    before, after = run(scenario)
    assert before == [] and len(after) == 5


def test_old_server_schema_is_refused():
    async def scenario():
        async with Hub(FakeMatterServer(nodes(), schema_version=10), connect_timeout_s=0.2) as hub:
            return await hub.driver.list_devices()

    assert run(scenario) == []


# /events WebSocket

def test_events_websocket_streams_matter_pushes():
    server = FakeMatterServer(nodes())
    with running_in_thread(server) as (url, call):
        driver = MatterDriver(url, bindings=BINDINGS)
        with TestClient(create_app([driver], Settings())) as client, client.websocket_connect("/events") as ws:
            call(server.set_attribute(2, "1/69/0", True))
            event = ws.receive_json()
    assert event["type"] == "state_changed" and event["device_id"] == LEAK
    assert event["changed"] == {"BooleanState": {"StateValue": True}}
    assert "online" not in event  # unset fields are left out


def test_events_websocket_streams_command_results_from_any_platform():
    home = demo_home(simulate_latency=False)
    with TestClient(create_app(home.drivers, Settings())) as client, client.websocket_connect("/events") as ws:
        client.post("/devices/home-1/space-heater-plug/commands", json={"command": "On", "idempotency_key": "k"})
        event = ws.receive_json()
    assert (event["device_id"], event["source"]) == ("home-1/space-heater-plug", "google_home")
    assert event["changed"] == {"OnOff": {"OnOff": True}}


def test_a_lagging_subscriber_is_ended_not_silently_thinned():
    bus = EventBus(max_queue=2)
    with bus.subscribe() as subscription:
        for i in range(3):
            bus.publish("online_changed", f"d{i}", "matter", online=True)
        assert subscription.lagged
        with pytest.raises(SubscriberLagged):
            asyncio.run(subscription.get())
        bus.publish("online_changed", "later", "matter", online=True)  # nothing more is queued
        with pytest.raises(SubscriberLagged):
            asyncio.run(subscription.get())
    with bus.subscribe() as fresh:  # a new subscription starts clean
        bus.publish("online_changed", "d9", "matter", online=True)
        assert asyncio.run(fresh.get()).device_id == "d9"


def test_events_websocket_tells_a_lagging_client_to_resync():
    from porch_hub.api import _send_events

    class StubSocket:
        def __init__(self):
            self.sent, self.closed = [], None

        async def send_text(self, text):
            self.sent.append(json.loads(text))

        async def send_json(self, data):
            self.sent.append(data)

        async def close(self, code):
            self.closed = code

    bus = EventBus(max_queue=2)
    socket_ = StubSocket()
    with bus.subscribe() as subscription:
        for i in range(3):  # the client has not read anything yet
            bus.publish("online_changed", f"d{i}", "matter", online=True)
        asyncio.run(_send_events(socket_, subscription))
    assert [m.get("type") for m in socket_.sent] == ["resync_required"]
    assert socket_.closed == 1013


def test_driver_from_env():
    driver = matter_driver_from_env({"MATTER_SERVER_URL": "ws://home-node:5580/ws",
                                     "MATTER_BINDINGS": '{"1/1": {"porch_id": "home-1/front-door-lock", "room": "Entry"}}'})
    assert driver._client._url == "ws://home-node:5580/ws"
    assert driver._bindings == BINDINGS
