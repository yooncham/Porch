import asyncio

from fake_matter_server import FakeMatterServer, node
from fastapi.testclient import TestClient

from porch_hub import clusters as c
from porch_hub.dedup import hardware_address, merge_device_records, normalize_mac, normalize_serial
from porch_hub.drivers import CommandRejected, GoogleHomeDriver, MatterDriver, SimulatedMatterDriver
from porch_hub.events import EventBus
from porch_hub.main import Settings, create_app
from porch_hub.models import ControlPath, Device, DeviceType, utcnow
from porch_hub.registry import DeviceRegistry
from porch_hub.seed import demo_home

M, G, L = ControlPath.MATTER, ControlPath.GOOGLE_HOME, ControlPath.LG_THINQ
LOCK = "home-1/front-door-lock"


def record(id_: str, path: ControlPath, name: str = "Device", room: str = "Unassigned",
           device_type: DeviceType = DeviceType.DOOR_LOCK, online: bool = True, **fields) -> Device:
    clusters = fields.pop("clusters", {c.DOOR_LOCK: c.door_lock(True)})
    return Device(id=id_, name=name, room=room, device_type=device_type, online=online,
                  primary_control_path=path, clusters=clusters, last_updated_at=utcnow(), **fields)


def by_id(devices: list[Device]) -> dict[str, Device]:
    return {d.id: d for d in devices}


def json_by_id(devices: list[dict]) -> dict[str, dict]:
    return {d["id"]: d for d in devices}


# Matching

def test_merges_by_serial_number_and_prefers_local_matter():
    merged = merge_device_records([
        record("google/abc", G, "Yale Lock", "Entryway", serial_number="yrd450-0042"),
        record("home-1/matter-1-1", M, "Smart Lock", serial_number="YRD450 0042"),
    ])
    assert len(merged) == 1
    lock = merged[0]
    assert lock.id == "home-1/matter-1-1"  # the local path's id: stable whichever path is up
    assert lock.control_paths == [M, G]
    assert lock.primary_control_path == M
    assert lock.room == "Entryway"  # Matter has no rooms, so Google's is used
    assert lock.path_device_ids == {M: "home-1/matter-1-1", G: "google/abc"}


def test_merges_by_mac_address_in_any_format():
    merged = merge_device_records([
        record("m", M, mac_address="AA:BB:CC:DD:EE:01"),
        record("g", G, mac_address="aabb.ccdd.ee01"),
    ])
    assert [d.control_paths for d in merged] == [[M, G]]


def test_merges_by_normalized_name_and_room():
    merged = merge_device_records([
        record("m", M, "Front-Door", "Entry "),
        record("g", G, "front door", "entry"),
    ])
    assert [d.control_paths for d in merged] == [[M, G]]


def test_name_and_room_need_the_same_type_and_a_known_room():
    merged = merge_device_records([
        record("m-lock", M, "Front door", "Entry"),
        record("g-sensor", G, "Front door", "Entry", device_type=DeviceType.WATER_LEAK_SENSOR),
        record("m-plug", M, "Lamp", "Unassigned", device_type=DeviceType.ON_OFF_PLUG),
        record("g-plug", G, "Lamp", "Unassigned", device_type=DeviceType.ON_OFF_PLUG),
    ])
    assert len(merged) == 4


def test_same_platform_records_never_merge():
    merged = merge_device_records([
        record("m1", M, "Front door", "Entry", serial_number="SN1"),
        record("m2", M, "Front door", "Entry", serial_number="SN1"),
    ])
    assert len(merged) == 2


def test_serial_shared_on_one_platform_is_not_an_identity():
    # Two outlets of one Matter power strip report the strip's serial; Google lists outlet 1 with it too.
    plug = DeviceType.ON_OFF_PLUG
    merged = by_id(merge_device_records([
        record("m-outlet-1", M, "Outlet 1", "Office", plug, serial_number="STRIP-9"),
        record("m-outlet-2", M, "Outlet 2", "Office", plug, serial_number="STRIP-9"),
        record("g-outlet-2", G, "Outlet 2", "Office", plug, serial_number="STRIP-9"),
    ]))
    assert merged["m-outlet-1"].control_paths == [M]
    assert merged["m-outlet-2"].control_paths == [M, G]  # matched by name and room, not by serial


def test_a_match_cannot_pull_two_records_of_one_platform_together():
    merged = by_id(merge_device_records([
        record("m-a", M, "Lock A", serial_number="SN-A"),
        record("g", G, "Back door", "Garden", serial_number="SN-A"),
        record("m-b", M, "Back door", "Garden"),  # same name and room as the Google record
    ]))
    assert merged["m-a"].control_paths == [M, G]
    assert merged["m-b"].control_paths == [M]


def test_shared_porch_id_from_bindings_merges():
    merged = merge_device_records([record(LOCK, G), record(LOCK, M)])
    assert [d.control_paths for d in merged] == [[M, G]]


def test_lg_cloud_ranks_above_google_cloud():
    dryer = DeviceType.LAUNDRY_DRYER
    clusters = {c.OPERATIONAL_STATE: c.operational_state()}
    merged = merge_device_records([
        record("g-dryer", G, "Dryer", "Laundry", dryer, clusters=clusters),
        record("lg-dryer", L, "Dryer", "Laundry", dryer, clusters=clusters),
    ])
    assert (merged[0].primary_control_path, merged[0].control_paths) == (L, [L, G])


def test_placeholder_identifiers_are_ignored():
    assert normalize_serial("N/A") is None and normalize_serial(" ab-12 ") == "AB12"
    assert normalize_mac("00:00:00:00:00:00") is None and normalize_mac("ff-ff-ff-ff-ff-ff") is None
    assert normalize_mac("not a mac") is None
    merged = merge_device_records([record("m", M, serial_number="0"), record("g", G, serial_number="0")])
    assert len(merged) == 2


# Unified record

def test_primary_falls_back_to_cloud_only_while_matter_is_down():
    def merge(matter_online: bool, google_online: bool) -> Device:
        return merge_device_records([
            record("m", M, online=matter_online, serial_number="SN"),
            record("g", G, online=google_online, serial_number="SN"),
        ])[0]

    assert (merge(True, True).primary_control_path, merge(True, True).online) == (M, True)
    assert (merge(False, True).primary_control_path, merge(False, True).online) == (G, True)
    assert (merge(False, False).primary_control_path, merge(False, False).online) == (M, False)
    assert merge(False, True).id == "m"


def test_clusters_come_from_the_primary_with_extras_from_other_paths():
    merged = merge_device_records([
        record("m", M, serial_number="SN", clusters={c.ON_OFF: c.on_off(True)}),
        record("g", G, serial_number="SN", clusters={c.ON_OFF: c.on_off(False), c.FAN_CONTROL: c.fan_control("Low")}),
    ])[0]
    assert merged.clusters[c.ON_OFF].attributes == {"OnOff": True}
    assert c.FAN_CONTROL in merged.clusters


def test_matter_driver_reports_serial_and_mac():
    assert hardware_address("qrvM3e4B") == "aa:bb:cc:dd:ee:01"  # base64 bytes, as the Matter Server sends them
    assert hardware_address([170, 187, 204, 221, 238, 1]) == "aa:bb:cc:dd:ee:01"
    lock = node(1, {"0/40/15": "YRD450-0042", "1/29/0": [{"deviceType": 10}], "1/257/0": 1,
                    "0/51/0": [{"name": "wlan0", "isOperational": True, "hardwareAddress": "qrvM3e4B"}]})

    async def scenario():
        server = FakeMatterServer([lock])
        driver = MatterDriver(await server.start())
        await driver.start()
        devices = await driver.list_devices()
        await driver.stop()
        await server.stop()
        return devices[0]

    device = asyncio.run(scenario())
    assert (device.serial_number, device.mac_address) == ("YRD450-0042", "aa:bb:cc:dd:ee:01")


# Registry, router and reads with several paths

def test_demo_lock_is_listed_once_and_commanded_over_matter():
    home = demo_home(simulate_latency=False)
    with TestClient(create_app(home.drivers, Settings())) as client:
        devices = json_by_id(client.get("/devices").json())
        body = client.post(f"/devices/{LOCK}/commands", json={"command": "UnlockDoor", "idempotency_key": "k"}).json()
    assert "home-1/google-yale-lock" not in devices
    assert devices[LOCK]["control_paths"] == ["matter", "google_home"]
    assert devices[LOCK]["serial_number"] == "YRD450-0042"
    assert (body["status"], body["control_path"]) == ("done", "matter")


def test_falls_back_to_google_while_matter_is_unreachable():
    home = demo_home(simulate_latency=False)
    with TestClient(create_app(home.drivers, Settings())) as client:
        client.get("/devices")
        home.matter.set_online(LOCK, False)
        lock = json_by_id(client.get("/devices").json())[LOCK]
        locked = client.post(f"/devices/{LOCK}/commands", json={"command": "LockDoor", "idempotency_key": "k1"}).json()
        unlock = client.post(f"/devices/{LOCK}/commands", json={"command": "UnlockDoor", "idempotency_key": "k2"}).json()
        state = client.get(f"/devices/{LOCK}/state")
        home.matter.set_online(LOCK, True)
        back = json_by_id(client.get("/devices").json())[LOCK]

    assert (lock["online"], lock["primary_control_path"]) == (True, "google_home")
    assert (locked["status"], locked["control_path"]) == ("done", "google_home")
    # Google refuses to unlock, and the error says why Matter was not used.
    assert unlock["status"] == "failed" and unlock["control_path"] == "google_home"
    assert unlock["error"].startswith("matter: unreachable; google_home: Google Home blocks UnlockDoor")
    assert state.status_code == 200  # read from Google
    assert back["primary_control_path"] == "matter"


def test_no_fallback_after_the_primary_refuses():
    class StrictMatter(SimulatedMatterDriver):
        def check_allowed(self, device, command):
            raise CommandRejected("lock is jammed")

    class CountingGoogle(GoogleHomeDriver):
        sent = 0

        async def send_command(self, device_id, command, params):
            CountingGoogle.sent += 1
            await super().send_command(device_id, command, params)

    matter = StrictMatter([record("m", M, serial_number="SN")])
    google = CountingGoogle([record("g", G, serial_number="SN")])
    with TestClient(create_app([matter, google], Settings())) as client:
        body = client.post("/devices/m/commands", json={"command": "LockDoor", "idempotency_key": "k"}).json()
    assert (body["status"], body["control_path"], body["error"]) == ("failed", "matter", "matter: lock is jammed")
    assert CountingGoogle.sent == 0


def test_cloud_reports_do_not_overwrite_local_state():
    async def scenario():
        matter = SimulatedMatterDriver([record("m", M, serial_number="SN")])
        google = GoogleHomeDriver([record("g", G, serial_number="SN")])
        registry = DeviceRegistry([matter, google], EventBus())
        await registry.refresh()
        google.set_attribute("g", c.DOOR_LOCK, "LockState", "Unlocked")  # a stale cloud poll
        after_google = registry.get("m").clusters[c.DOOR_LOCK].attributes["LockState"]
        with registry.events.subscribe() as events:
            matter.set_attribute("m", c.DOOR_LOCK, "LockState", "Unlocked")
            event = events._queue.get_nowait()
        return after_google, registry.get("m"), event

    after_google, lock, event = asyncio.run(scenario())
    assert after_google == "Locked"
    assert lock.clusters[c.DOOR_LOCK].attributes["LockState"] == "Unlocked"
    assert (event.device_id, event.source) == ("m", "matter")


def test_online_event_only_when_every_path_is_down():
    async def scenario():
        matter = SimulatedMatterDriver([record("m", M, serial_number="SN")])
        google = GoogleHomeDriver([record("g", G, serial_number="SN")])
        registry = DeviceRegistry([matter, google], EventBus())
        await registry.refresh()
        with registry.events.subscribe() as events:
            matter.set_online("m", False)
            google.set_online("g", False)
            return [(e.type, e.online) for e in [events._queue.get_nowait() for _ in range(events._queue.qsize())]]

    assert asyncio.run(scenario()) == [("online_changed", False)]
