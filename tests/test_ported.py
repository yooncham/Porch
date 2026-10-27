"""Scenarios ported from the ChatGPT/Porch test suite, written against this hub's API.

Each test notes the test it comes from (file::name). Scenarios that only exercise
that project's own design choices (path-label aliases, native-id aliases,
quarantined duplicate ids, lock PINs, multi-tub WashTowers) are not ported.
"""

import asyncio
import json
import random
import threading

import pytest
from fake_matter_server import FakeMatterServer, node
from fastapi.testclient import TestClient

from porch_hub import clusters as c
from porch_hub import digital_copy as digital_copy_module
from porch_hub.dedup import merge_device_records
from porch_hub.digital_copy import DigitalCopy
from porch_hub.drivers import CommandRejected, DriverError, SimulatedMatterDriver
from porch_hub.drivers.matter import MatterDriver
from porch_hub.drivers.matter.driver import server_url
from porch_hub.drivers.matter.mapping import node_endpoints
from porch_hub.drivers.thinq import FakeThinQCloud, ThinQBinding, ThinQDriver
from porch_hub.drivers.thinq.connect import load_client_id
from porch_hub.idempotency import IdempotencyStore, fingerprint
from porch_hub.main import Settings, create_app
from porch_hub.models import CommandRequest, ControlPath, Device, DeviceType, utcnow
from porch_hub.registry import DeviceRegistry
from porch_hub.router import CommandRouter
from porch_hub.seed import demo_home

M, G, L = ControlPath.MATTER, ControlPath.GOOGLE_HOME, ControlPath.LG_THINQ
PLUG, LOCK = DeviceType.ON_OFF_PLUG, DeviceType.DOOR_LOCK
FAST = dict(confirm_timeout_s=0.3, poll_interval_s=0.02)


def rec(id_, path=M, name="Lamp", room="Office", device_type=PLUG, online=True, clusters=None, **fields):
    return Device(id=id_, name=name, room=room, device_type=device_type, online=online, primary_control_path=path,
                  clusters=clusters if clusters is not None else {c.ON_OFF: c.on_off(False)},
                  last_updated_at=utcnow(), **fields)


def paths(devices):
    return sorted(sorted(p.value for p in d.control_paths) for d in devices)


async def hub(*drivers, timeout_s=0.3, driver_timeout_s=10.0, snapshot=None, offline=False):
    registry = DeviceRegistry(list(drivers))
    copy = DigitalCopy(registry, list(drivers), offline=offline, snapshot_path=snapshot)
    router = CommandRouter(registry, IdempotencyStore(), confirm_timeout_s=timeout_s, poll_interval_s=0.01,
                           digital_copy=copy, driver_timeout_s=driver_timeout_s)
    await copy.start()
    return registry, router, copy


def cmd(command, key, params=None):
    return CommandRequest(command=command, params=params or {}, idempotency_key=key)


class Counting(SimulatedMatterDriver):
    """Counts calls; can hold sends for chosen devices, hang them, or refuse every command."""

    def __init__(self, devices, hold=(), hang=False, refuse=False):
        super().__init__(devices)
        self.sends, self.reads, self.lists = [], 0, 0
        self.hold, self.hang, self.refuse = set(hold), hang, refuse
        self.applied, self.release = asyncio.Event(), asyncio.Event()

    async def list_devices(self):
        self.lists += 1
        return await super().list_devices()

    async def read_state(self, device_id):
        self.reads += 1
        return await super().read_state(device_id)

    async def send_command(self, device_id, command, params):
        self.sends.append((device_id, command))
        if self.hang:
            await asyncio.sleep(60)
        if self.refuse:
            raise CommandRejected("refused by the device")
        await super().send_command(device_id, command, params)
        if device_id in self.hold:
            self.applied.set()
            await self.release.wait()


# test_deduplication.py

def test_empty_and_singleton_inputs():
    assert merge_device_records([]) == []
    (only,) = merge_device_records([rec("m")])
    assert only.control_paths == [M] and only.path_device_ids == {M: "m"}


def test_serial_match_ignores_case_and_surrounding_space_despite_different_names():
    merged = merge_device_records([rec("m", name="Smart plug", serial_number=" ab-123 "),
                                   rec("g", G, name="Desk lamp", room="Study", serial_number="AB123")])
    assert paths(merged) == [["google_home", "matter"]]


def test_mac_formats_match_without_relying_on_name():
    merged = merge_device_records([rec("m", name="One", room="A", mac_address="AA-BB-CC-DD-EE-01"),
                                   rec("g", G, name="Two", room="B", mac_address="aabb.ccdd.ee01")])
    assert paths(merged) == [["google_home", "matter"]]


@pytest.mark.parametrize("left, right", [
    (("Front-Door!", "Entry"), ("front door", "ENTRY")),
    (("Ｆｒｏｎｔ Ｄｏｏｒ", "Ｅｎｔｒｙ"), ("front door", "entry")),  # full-width letters
    (("현관문", "현관"), ("현관문", " 현관 ")),
    (("Café lamp", "Salon"), ("CAFÉ LAMP", "salon")),
])
def test_normalized_name_and_room_match_with_unicode_and_punctuation(left, right):
    merged = merge_device_records([rec("m", name=left[0], room=left[1]), rec("g", G, name=right[0], room=right[1])])
    assert paths(merged) == [["google_home", "matter"]]


def test_distinct_or_incomplete_name_room_matches_stay_separate():
    merged = merge_device_records([
        rec("m1", name="Lamp", room="Office"), rec("g1", G, name="Lamp", room="Kitchen"),
        rec("m2", name="Fan", room=""), rec("g2", G, name="Fan", room=""),
        rec("m3", name="", room="Hall"), rec("g3", G, name="", room="Hall"),
    ])
    assert len(merged) == 6


def test_serial_is_stronger_than_different_network_interface_macs():
    merged = merge_device_records([rec("m", serial_number="SN", mac_address="aa:bb:cc:dd:ee:01"),
                                   rec("g", G, serial_number="SN", mac_address="aa:bb:cc:dd:ee:02")])
    assert len(merged) == 1


def test_strong_match_still_requires_compatible_device_types():
    merged = merge_device_records([rec("m", serial_number="HUB-1", device_type=LOCK,
                                       clusters={c.DOOR_LOCK: c.door_lock()}),
                                   rec("g", G, serial_number="HUB-1")])
    assert len(merged) == 2


def test_transitive_serial_and_mac_matches_merge_all_paths():
    merged = merge_device_records([rec("m", serial_number="SN"),
                                   rec("l", L, name="Other", room="Elsewhere", serial_number="SN",
                                       mac_address="aa:bb:cc:dd:ee:01"),
                                   rec("g", G, name="Third", room="Nowhere", mac_address="AABBCCDDEE01")])
    assert paths(merged) == [["google_home", "lg_thinq", "matter"]]


def test_weak_bridge_cannot_join_conflicting_strong_identities():
    merged = merge_device_records([rec("m", serial_number="SN-1"), rec("g", G, serial_number="SN-1"),
                                   rec("l", L, serial_number="SN-2")])  # same name and room as both
    assert paths(merged) == [["google_home", "matter"], ["lg_thinq"]]


def test_ambiguous_multiple_endpoints_on_one_platform_are_not_arbitrarily_merged():
    merged = merge_device_records([rec("m1"), rec("m2"), rec("g", G)])  # all "Lamp" in "Office"
    assert paths(merged) == [["google_home"], ["matter"], ["matter"]]


def test_local_clusters_win_and_missing_capabilities_are_preserved_without_mutation():
    local = rec("m", serial_number="SN", clusters={c.ON_OFF: c.on_off(True)})
    cloud = rec("g", G, serial_number="SN", clusters={c.ON_OFF: c.on_off(False), c.FAN_CONTROL: c.fan_control("Low")})
    before = (local.model_dump(), cloud.model_dump())
    (merged,) = merge_device_records([cloud, local])
    assert merged.clusters[c.ON_OFF].attributes == {"OnOff": True}
    assert c.FAN_CONTROL in merged.clusters
    assert (local.model_dump(), cloud.model_dump()) == before


def test_merging_is_order_independent():
    records = [rec("m1", serial_number="A"), rec("g1", G, serial_number="A"), rec("m2", name="Fan"),
               rec("g2", G, name="Fan"), rec("l1", L, name="Dryer", room="Laundry"), rec("m3", name="Solo", room="X")]
    expected = sorted((d.id, tuple(d.control_paths)) for d in merge_device_records(records))
    for seed in range(10):
        shuffled = records[:]
        random.Random(seed).shuffle(shuffled)
        assert sorted((d.id, tuple(d.control_paths)) for d in merge_device_records(shuffled)) == expected


def test_placeholder_serials_do_not_join_devices_with_different_names():
    merged = merge_device_records([rec("m", name="Lamp", serial_number="N/A"),
                                   rec("g", G, name="Heater", serial_number="n/a")])
    assert len(merged) == 2


def test_invalid_mac_is_not_a_hardware_identity():
    merged = merge_device_records([rec("m", name="A", mac_address="zz:zz:zz:zz:zz:zz"),
                                   rec("g", G, name="B", mac_address="zz:zz:zz:zz:zz:zz")])
    assert len(merged) == 2


def test_eui64_mac_is_supported():
    merged = merge_device_records([rec("m", name="A", mac_address="00:11:22:33:44:55:66:77"),
                                   rec("g", G, name="B", mac_address="0011223344556677")])
    assert len(merged) == 1


def test_dispatched_action_is_not_replayed_via_cloud_after_failure():
    class AckLost(Counting):
        async def send_command(self, device_id, command, params):
            self.sends.append((device_id, command))
            raise DriverError("connection reset after sending")

    async def scenario():
        local, cloud = AckLost([rec("m", serial_number="SN")]), Counting([rec("g", G, serial_number="SN")])
        cloud.control_path = G
        _, router, _ = await hub(local, cloud)
        response = await router.execute("m", cmd("On", "k"))
        return response, local.sends, cloud.sends

    response, local_sends, cloud_sends = asyncio.run(scenario())
    assert response.status == "failed" and len(local_sends) == 1 and cloud_sends == []


def test_known_identity_survives_platform_discovery_outage_for_cloud_fallback():
    class FlakyListing(Counting):
        down = False

        async def list_devices(self):
            if self.down:
                raise DriverError("Matter Server unreachable")
            return await super().list_devices()

    async def scenario():
        local, cloud = FlakyListing([rec("m", serial_number="SN")]), Counting([rec("g", G, serial_number="SN")])
        cloud.control_path = G
        registry, router, _ = await hub(local, cloud)
        local.down = True
        await registry.refresh()
        device = registry.get("m")
        response = await router.execute("m", cmd("On", "k"))
        return device, response, local.sends, cloud.sends

    device, response, local_sends, cloud_sends = asyncio.run(scenario())
    assert (device.online, device.primary_control_path, device.control_paths) == (True, G, [M, G])
    assert (response.status, response.control_path) == ("done", G)
    assert local_sends == [] and len(cloud_sends) == 1


# test_interface.py

def test_cached_response_does_not_rediscover_or_read_current_state():
    async def scenario():
        driver = Counting([rec("p")])
        _, router, _ = await hub(driver)
        await router.execute("p", cmd("On", "k"))
        calls = (driver.lists, driver.reads, len(driver.sends))
        await router.execute("p", cmd("On", "k"))
        return calls, (driver.lists, driver.reads, len(driver.sends))

    before, after = asyncio.run(scenario())
    assert before == after


def test_parameter_key_order_does_not_change_idempotency_identity():
    assert fingerprint("d", "SetDelayStart", {"hours": 2, "minutes": 0}) == \
        fingerprint("d", "SetDelayStart", {"minutes": 0, "hours": 2})


def test_failures_always_attempt_readback_and_are_cached():
    async def scenario():
        driver = Counting([rec("p")], refuse=True)
        _, router, _ = await hub(driver)
        first = await router.execute("p", cmd("On", "k"))
        reads = driver.reads
        again = await router.execute("p", cmd("On", "k"))
        return first, again, reads, driver.reads, driver.sends

    first, again, reads_after_first, reads_after_retry, sends = asyncio.run(scenario())
    assert first.status == "failed" and first.state_after  # a fresh read followed the refusal
    assert reads_after_first >= 2 and reads_after_retry == reads_after_first
    assert again == first and len(sends) == 1


def test_driver_timeouts_still_produce_a_cached_failure():
    async def scenario():
        driver = Counting([rec("p")], hang=True)
        _, router, _ = await hub(driver, timeout_s=0.1, driver_timeout_s=0.1)
        first = await router.execute("p", cmd("On", "k"))
        again = await router.execute("p", cmd("On", "k"))
        return first, again, driver

    first, again, driver = asyncio.run(scenario())
    assert first.status == "failed" and "did not answer within 0.1s" in first.error
    assert first.state_after  # read back after the timed-out send
    assert again == first and len(driver.sends) == 1


def test_same_device_actions_cannot_interleave_with_confirmation():
    async def scenario():
        driver = Counting([rec("p")], hold={"p"})
        _, router, _ = await hub(driver)
        first = asyncio.create_task(router.execute("p", cmd("On", "k1")))
        await driver.applied.wait()
        second = asyncio.create_task(router.execute("p", cmd("Off", "k2")))
        await asyncio.sleep(0.05)
        sends_while_held = list(driver.sends)
        driver.hold.clear()
        driver.release.set()
        return sends_while_held, await first, await second

    held, first, second = asyncio.run(scenario())
    assert held == [("p", "On")]
    assert first.state_after[c.ON_OFF].attributes["OnOff"] is True
    assert second.state_after[c.ON_OFF].attributes["OnOff"] is False


def test_different_devices_can_execute_independently():
    async def scenario():
        driver = Counting([rec("a", name="A"), rec("b", name="B")], hold={"a"})
        _, router, _ = await hub(driver)
        held = asyncio.create_task(router.execute("a", cmd("On", "k1")))
        await driver.applied.wait()
        other = await asyncio.wait_for(router.execute("b", cmd("On", "k2")), 1)
        driver.release.set()
        return other, await held

    other, held = asyncio.run(scenario())
    assert other.status == "done" and held.status == "done"


def test_unknown_device_is_404_without_driver_side_effects():
    driver = Counting([rec("p")])
    with TestClient(create_app([driver], Settings(**FAST))) as client:
        response = client.post("/devices/nope/commands", json={"command": "On", "idempotency_key": "k"})
    assert response.status_code == 404 and driver.sends == []


def test_state_failure_does_not_return_cached_clusters():
    class BrokenReads(Counting):
        broken = False

        async def read_state(self, device_id):
            if self.broken:
                raise DriverError("read failed")
            return await super().read_state(device_id)

    driver = BrokenReads([rec("p")])
    with TestClient(create_app([driver], Settings(**FAST))) as client:
        client.get("/devices")
        driver.broken = True
        response = client.get("/devices/p/state")
    assert response.status_code == 502 and "OnOff" not in response.text


def test_toggle_from_an_unknown_state_is_refused_without_sending():
    unknown = rec("p", clusters={c.ON_OFF: c.Cluster(attributes={"OnOff": None}, commands=["On", "Off", "Toggle"])})

    async def scenario():
        driver = Counting([unknown])
        registry, router, copy = await hub(driver)
        response = await router.execute("p", cmd("Toggle", "k"))
        preview = copy.simulate(registry.get("p"), "Toggle", {})
        return response, preview, driver.sends

    response, preview, sends = asyncio.run(scenario())
    assert response.status == "failed" and "unknown OnOff" in response.error and sends == []
    assert not preview.would_succeed and "unknown OnOff" in preview.reason


# test_lg_thinq.py

def lg(simulate_delay=0.0):
    cloud = FakeThinQCloud(apply_delay_s=simulate_delay)
    cloud.add_dryer("lg-dryer", "Dryer")
    cloud.add_robot_cleaner("lg-vacuum", "Robot vacuum")
    driver = ThinQDriver(cloud, bindings={"lg-dryer": ThinQBinding("home-1/dryer", "Laundry"),
                                          "lg-vacuum": ThinQBinding("home-1/vacuum", "Living room")},
                         read_back_interval_s=None, read_back_timeout_s=None)
    return cloud, driver


def lg_command(client, device_id, command, key):
    return client.post(f"/devices/{device_id}/commands", json={"command": command, "idempotency_key": key}).json()


def test_start_guard_requires_a_fresh_explicit_true():
    cloud, driver = lg()
    del cloud._appliances["lg-dryer"]["status"]["remoteControlEnable"]  # the dryer stops reporting the switch
    with TestClient(create_app([driver], Settings(**FAST))) as client:
        dryer = lg_command(client, "home-1/dryer", "Start", "k1")
        vacuum = lg_command(client, "home-1/vacuum", "Start", "k2")  # vacuums have no such switch
    assert dryer["status"] == "failed" and "status is unknown" in dryer["error"]
    assert vacuum["status"] == "done"
    assert [lg_id for lg_id, _ in cloud.controls] == ["lg-vacuum"]


def test_an_appliance_in_error_is_not_started_live_or_in_a_preview():
    cloud, driver = lg()
    cloud.machine_update("lg-dryer", {"runState": {"currentState": "ERROR"}})
    with TestClient(create_app([driver], Settings(**FAST))) as client:
        preview = client.post("/devices/home-1/dryer/simulate", json={"command": "Start"}).json()
        live = lg_command(client, "home-1/dryer", "Start", "k")
    assert not preview["would_succeed"] and "error" in preview["reason"]
    assert live["status"] == "failed" and "error" in live["error"] and cloud.controls == []


def test_readback_polls_without_redispatching():
    cloud, driver = lg(simulate_delay=0.1)
    with TestClient(create_app([driver], Settings(**FAST))) as client:
        response = lg_command(client, "home-1/dryer", "Start", "k")
    assert response["status"] == "done" and len(cloud.controls) == 1


def test_malformed_unknown_and_notification_events_do_not_kill_listener():
    async def scenario():
        cloud, driver = lg()
        registry = DeviceRegistry([driver])
        await driver.start()
        await registry.refresh()
        for message in ["not an object", {"pushType": "DEVICE_STATUS", "deviceId": "lg-dryer", "report": "junk"},
                        {"pushType": "DEVICE_STATUS", "deviceId": "lg-dryer", "report": [1, 2]},
                        {"pushType": "MYSTERY", "deviceId": "lg-dryer"},
                        {"pushType": "DEVICE_PUSH", "deviceId": "lg-dryer", "pushCode": "DRYING_IS_COMPLETE"}]:
            driver._on_push(message)
        cloud.machine_update("lg-dryer", {"runState": {"currentState": "DRYING"}})
        return registry.get("home-1/dryer")

    dryer = asyncio.run(scenario())
    assert dryer.clusters[c.OPERATIONAL_STATE].attributes["OperationalState"] == "Running"


def test_client_identity_creation_is_atomic_across_concurrent_creators(tmp_path):
    path = tmp_path / "id"
    ids, start = [], threading.Barrier(8)

    def create():
        start.wait()
        ids.append(load_client_id(path))

    threads = [threading.Thread(target=create) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(ids)) == 1 and ids[0] == path.read_text()
    assert [p.name for p in tmp_path.iterdir()] == ["id"]  # no temporary files left behind


# test_matter.py

async def matter_hub(server, **driver_options):
    driver = MatterDriver(await server.start(), reconnect_delay_s=0.05, **driver_options)
    registry, router, copy = await hub(driver)
    return driver, registry, router, copy


def plug_node(node_id=3, on=False):
    return node(node_id, {"1/29/0": [{"deviceType": 266}], "1/6/0": on})


def test_correlated_responses_can_arrive_out_of_order():
    async def scenario():
        server = FakeMatterServer([plug_node()])
        driver, _, _, copy = await matter_hub(server)
        server.read_delay_s = 0.2
        read = asyncio.create_task(driver.read_state("home-1/matter-3-1"))
        await asyncio.sleep(0.02)
        await driver.send_command("home-1/matter-3-1", "On", {})  # answered before the read
        server.read_delay_s = 0.0
        state = await read
        await copy.stop()
        await server.stop()
        return state

    assert asyncio.run(scenario())[c.ON_OFF].attributes["OnOff"] is False  # the read's own answer


def test_missing_attribute_never_falls_back_to_cached_state():
    async def scenario():
        server = FakeMatterServer([plug_node()])
        driver, _, router, copy = await matter_hub(server)
        server.omit_from_reads = {"1/6/0"}
        with pytest.raises(DriverError, match="did not return 1/6/0"):
            await driver.read_state("home-1/matter-3-1")
        response = await router.execute("home-1/matter-3-1", cmd("On", "k"))
        await copy.stop()
        await server.stop()
        return response, server.commands("device_command")

    response, sent = asyncio.run(scenario())
    assert response.status == "failed" and sent == []  # an unreadable device is never commanded


def test_node_snapshot_in_flight_is_preserved_over_an_older_read():
    async def scenario():
        server = FakeMatterServer([plug_node()])
        driver, _, _, copy = await matter_hub(server)
        server.read_delay_s = 0.1
        read = asyncio.create_task(driver.read_state("home-1/matter-3-1"))
        await asyncio.sleep(0.03)
        server.nodes[3]["attributes"]["1/6/0"] = True
        await server.set_available(3, True)  # a node_updated with the new value overtakes the read
        answered = await read
        cached = (await driver.list_devices())[0]
        await copy.stop()
        await server.stop()
        return answered, cached

    answered, cached = asyncio.run(scenario())
    assert answered[c.ON_OFF].attributes["OnOff"] is False
    assert cached.clusters[c.ON_OFF].attributes["OnOff"] is True


def test_malformed_events_do_not_kill_the_reader():
    async def scenario():
        server = FakeMatterServer([plug_node()])
        driver, registry, _, copy = await matter_hub(server)
        for raw in ["not json", '{"event": "attribute_updated", "data": "bad"}',
                    '{"event": "attribute_updated", "data": [3, "x/y/z", 1]}', '{"event": "mystery", "data": {}}',
                    '{"event": "node_event", "data": {"node_id": 3}}', '{"event": "node_removed", "data": 999}']:
            await server.send_raw(raw)
        await server.set_attribute(3, "1/6/0", True)
        for _ in range(100):
            if registry.get("home-1/matter-3-1").clusters[c.ON_OFF].attributes["OnOff"]:
                break
            await asyncio.sleep(0.01)
        state = registry.get("home-1/matter-3-1").clusters[c.ON_OFF].attributes["OnOff"]
        await copy.stop()
        await server.stop()
        return state

    assert asyncio.run(scenario()) is True


def test_invalid_server_urls_are_rejected():
    assert server_url("ws://localhost:5580") == "ws://localhost:5580/ws"
    assert server_url("wss://home-node:5580/") == "wss://home-node:5580/ws"
    assert server_url("ws://home-node:5580/matter") == "ws://home-node:5580/matter"
    for bad in ["http://localhost:5580", "localhost:5580", "ws://user:secret@home-node:5580"]:
        with pytest.raises(ValueError):
            MatterDriver(bad)


def test_device_serial_identity_is_scoped_to_the_physical_endpoint():
    single = node(1, {"0/40/15": "SN-1", "1/29/0": [{"deviceType": 266}], "1/6/0": False})
    strip = node(2, {"0/40/15": "STRIP", "1/29/0": [{"deviceType": 266}], "1/6/0": False,
                     "2/29/0": [{"deviceType": 266}], "2/6/0": False})
    bridge = node(3, {"0/40/15": "BRIDGE", "1/29/0": [{"deviceType": 266}], "1/6/0": False, "1/57/18": "CHILD-1"})
    bridge["is_bridge"] = True
    assert [e.serial_number for e in node_endpoints(single, "home-1", {})] == ["SN-1"]
    assert [e.serial_number for e in node_endpoints(strip, "home-1", {})] == [None, None]
    assert [e.serial_number for e in node_endpoints(bridge, "home-1", {})] == ["CHILD-1"]


def test_reconnect_reloads_state_and_never_replays_an_action():
    async def scenario():
        server = FakeMatterServer([plug_node()])
        driver, registry, router, copy = await matter_hub(server)
        await router.execute("home-1/matter-3-1", cmd("On", "k"))
        await server.drop_connections()
        server.nodes[3]["attributes"]["1/6/0"] = False  # changed while the hub was away
        for _ in range(100):
            if driver._client.connected and not registry.get("home-1/matter-3-1").clusters[c.ON_OFF].attributes["OnOff"]:
                break
            await asyncio.sleep(0.02)
        state = registry.get("home-1/matter-3-1").clusters[c.ON_OFF].attributes["OnOff"]
        await copy.stop()
        await server.stop()
        return state, server.commands("device_command")

    state, sent = asyncio.run(scenario())
    assert state is False and len(sent) == 1


# test_runtime.py

def test_platform_startup_failures_are_isolated_and_recover():
    class LateStart(Counting):
        control_path = G
        attempts = 0

        async def start(self):
            LateStart.attempts += 1
            if LateStart.attempts < 3:
                raise ConnectionError("no network yet")

        async def list_devices(self):
            if LateStart.attempts < 3:
                raise DriverError("not started")
            return await super().list_devices()

    async def scenario():
        healthy, late = Counting([rec("m", name="Healthy")]), LateStart([rec("g", G, name="Late")])
        registry = DeviceRegistry([healthy, late])
        copy = DigitalCopy(registry, [healthy, late])
        copy._retry_s = 0.02
        await copy.start()
        before = {d.id for d in await registry.refresh()}
        for _ in range(100):
            if "g" in {d.id for d in await registry.refresh()}:
                break
            await asyncio.sleep(0.02)
        after = {d.id for d in await registry.refresh()}
        await copy.stop()
        return before, after

    before, after = asyncio.run(scenario())
    assert before == {"m"} and after == {"m", "g"}


def test_latency_learns_from_confirmed_live_actions_only():
    async def scenario():
        refusing = Counting([rec("p")], refuse=True)
        _, router, copy = await hub(refusing)
        start = copy.estimate_latency_ms(M)
        await router.execute("p", cmd("On", "k1"))  # failed
        after_failure = copy.estimate_latency_ms(M)
        await copy.set_offline(True)
        await router.execute("p", cmd("Off", "k2"))  # simulated
        after_offline = copy.estimate_latency_ms(M)
        await copy.set_offline(False)
        refusing.refuse = False
        await router.execute("p", cmd("On", "k3"))  # confirmed
        return start, after_failure, after_offline, copy.estimate_latency_ms(M)

    start, after_failure, after_offline, after_success = asyncio.run(scenario())
    assert start == after_failure == after_offline == 150 and after_success < 150


def test_expiry_never_evicts_inflight_idempotency_work():
    async def scenario():
        store, runs = IdempotencyStore(ttl_s=0), 0

        async def operation():
            nonlocal runs
            runs += 1
            await asyncio.sleep(0.05)
            return await (await hub(Counting([rec("p")])))[1].execute("p", cmd("On", "inner"))

        first = asyncio.create_task(store.run("k", "fp", operation))
        await asyncio.sleep(0.01)
        _, replayed = await store.run("k", "fp", operation)
        await first
        return runs, replayed

    runs, replayed = asyncio.run(scenario())
    assert runs == 1 and replayed


def test_rehearsal_pushes_are_saved_automatically_without_a_command(tmp_path, monkeypatch):
    monkeypatch.setattr(digital_copy_module, "SNAPSHOT_SAVE_INTERVAL_S", 0.05)
    snapshot = tmp_path / "copy.json"

    async def scenario():
        driver = SimulatedMatterDriver([rec("leak", device_type=DeviceType.WATER_LEAK_SENSOR,
                                            clusters={c.BOOLEAN_STATE: c.boolean_state(False)})])
        _, _, copy = await hub(driver, snapshot=snapshot)
        driver.set_attribute("leak", c.BOOLEAN_STATE, "StateValue", True)  # pushed, no command involved
        await asyncio.sleep(0.15)
        saved = json.loads(snapshot.read_text())
        await copy.stop()
        return saved

    saved = asyncio.run(scenario())
    (leak,) = saved["records"]
    assert leak["clusters"]["BooleanState"]["attributes"]["StateValue"] is True


def test_cancelling_mode_request_does_not_leave_an_incomplete_transition():
    async def scenario():
        driver = Counting([rec("p")], hold={"p"})
        stops = 0

        async def stop():
            nonlocal stops
            stops += 1

        driver.stop = stop
        registry, router, copy = await hub(driver)
        live = asyncio.create_task(router.execute("p", cmd("On", "k")))
        await driver.applied.wait()
        request = asyncio.create_task(copy.set_offline(True))
        await asyncio.sleep(0.02)
        request.cancel()  # the HTTP client gives up on the switch
        await asyncio.gather(request, return_exceptions=True)
        driver.release.set()
        await live
        await asyncio.gather(*copy._background, return_exceptions=True)
        return copy.offline, registry.offline_mode, stops

    assert asyncio.run(scenario()) == (True, True, 1)


def test_invalid_private_snapshot_is_preserved_on_failed_startup(tmp_path):
    snapshot = tmp_path / "copy.json"
    snapshot.write_text("{ this is not json")

    async def scenario():
        _, _, copy = await hub(Counting([rec("p")]), snapshot=snapshot)
        await copy.stop()

    asyncio.run(scenario())
    (aside,) = tmp_path.glob("copy.json.corrupt-*")
    assert aside.read_text() == "{ this is not json"
    assert json.loads(snapshot.read_text())["records"]  # a fresh copy was saved beside it


# test_shadow.py

def test_live_confirmation_survives_device_removal_during_readback():
    class Vanishing(Counting):
        hidden = False

        async def list_devices(self):
            return [] if self.hidden else await super().list_devices()

    async def scenario():
        driver = Vanishing([rec("p")], hold={"p"})
        registry, router, _ = await hub(driver)
        command = asyncio.create_task(router.execute("p", cmd("On", "k")))
        await driver.applied.wait()
        driver.hidden = True
        await registry.refresh()  # the platform stops listing the device mid-command
        driver.release.set()
        return await command

    response = asyncio.run(scenario())
    assert response.status == "done"


def test_unsupported_or_invalid_command_never_mutates_shadow():
    home = demo_home(simulate_latency=False)
    with TestClient(create_app(home.drivers, Settings(demo_offline_mode=True, **FAST))) as client:
        before = client.get("/devices").json()
        bad = [client.post("/devices/home-1/desk-lamp-plug/commands",
                           json={"command": "LockDoor", "idempotency_key": "k1"}),
               client.post("/devices/home-1/laundry-dryer/commands",
                           json={"command": "SetDelayStart", "params": {"hours": "soon"}, "idempotency_key": "k2"}),
               client.post("/devices/home-1/kitchen-leak-sensor/commands",
                           json={"command": "On", "idempotency_key": "k3"})]  # a read-only sensor
        after = client.get("/devices").json()
    assert [r.status_code for r in bad] == [422, 422, 422]
    strip = [{k: v for k, v in d.items() if k != "last_updated_at"} for d in before]
    assert strip == [{k: v for k, v in d.items() if k != "last_updated_at"} for d in after]


def test_offline_retry_and_concurrent_toggles_are_idempotent_and_serialized():
    async def scenario():
        _, router, _ = await hub(Counting([rec("p")]), offline=False)
        await router._digital_copy.set_offline(True)
        distinct = await asyncio.gather(*(router.execute("p", cmd("Toggle", f"k{i}")) for i in range(5)))
        repeated = await asyncio.gather(*(router.execute("p", cmd("Toggle", "same")) for _ in range(3)))
        return distinct, repeated, router._registry.get("p").clusters[c.ON_OFF].attributes["OnOff"]

    distinct, repeated, final = asyncio.run(scenario())
    seen = sorted(r.state_after[c.ON_OFF].attributes["OnOff"] for r in distinct)
    assert seen == [False, False, True, True, True]  # five toggles, one after another
    assert all(r == repeated[0] for r in repeated)
    assert final is False  # five toggles plus one: back to off
