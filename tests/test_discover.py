"""python -m porch_hub.discover: what each platform reports, and bindings to paste."""

import asyncio
import io
import json

from fake_matter_server import FakeMatterServer, node
from test_matter import color_bulb, energy_plug

from porch_hub import discover
from porch_hub.drivers.thinq import ThinQDriver
from porch_hub.drivers.thinq.fake import FakeThinQCloud


def lg_entries() -> list[dict]:
    cloud = FakeThinQCloud()
    cloud.add_air_purifier("lg-purifier-aaaa", "PuriCare 360")
    cloud.add_air_purifier("lg-purifier-bbbb", "Bedroom purifier")
    entries = asyncio.run(cloud.list_devices())
    # A cordless stick vacuum: LG lists it, the hub has no profile for it.
    entries.append({"deviceId": "lg-stick-cccc", "deviceInfo": {"deviceType": "DEVICE_STICK_CLEANER",
                                                                "modelName": "A9", "alias": "Cordless vacuum"}})
    return entries


def test_lg_rows_keep_unsupported_appliances_and_bind_only_supported_ones():
    appliances = discover.lg_appliances(lg_entries())
    assert [(a.lg_id, a.supported) for a in appliances] == [
        ("lg-purifier-aaaa", True), ("lg-purifier-bbbb", True), ("lg-stick-cccc", False)]
    # The first purifier gets the id the demo, gateway and dashboards already use.
    assert discover.lg_bindings(appliances, "home-1") == {
        "lg-purifier-aaaa": {"porch_id": "home-1/air-purifier", "room": "Unassigned"},
        "lg-purifier-bbbb": {"porch_id": "home-1/air-purifier-2", "room": "Unassigned"},
    }
    report = "\n".join(discover.lg_report(appliances))
    assert "lg-stick-cccc" in report and "not used: no profile for this type yet" in report


def test_lg_default_id_is_the_one_the_driver_gives():
    """Without a binding, discovery shows the same id the LG driver will use."""
    async def scenario():
        cloud = FakeThinQCloud()
        cloud.add_air_purifier("lg-purifier-aaaa", "PuriCare 360")
        appliance = discover.lg_appliances(await cloud.list_devices())[0]
        driver = ThinQDriver(cloud, home_id="home-1", bindings={})
        await driver.start()
        try:
            return [d.id for d in await driver.list_devices()], discover.lg_hub_id(appliance, "home-1", {})
        finally:
            await driver.stop()

    driver_ids, shown = asyncio.run(scenario())
    assert driver_ids == [shown]


def test_existing_bindings_are_kept_and_new_devices_get_ids_that_do_not_clash():
    existing = {"1/1": {"porch_id": "home-1/entry-light", "room": "Entry"},
                "3/1": {"porch_id": "home-1/l535e-2", "room": "Office"}}  # a name a suggestion would pick
    bindings = discover.matter_bindings(tapo_home(), "home-1", existing)
    assert bindings["1/1"] == existing["1/1"] and bindings["3/1"] == existing["3/1"]
    assert bindings["2/1"]["porch_id"] == "home-1/l535e"
    assert len({b["porch_id"] for b in bindings.values()}) == len(bindings)
    rows = {(r.node_id, r.endpoint_id): r for r in discover.matter_endpoints(tapo_home(), "home-1", existing)}
    assert rows[(1, 1)].porch_id == "home-1/entry-light"

    appliances = discover.lg_appliances(lg_entries())
    lg_existing = {"lg-purifier-aaaa": {"porch_id": "home-1/air-purifier", "room": "Living room"}}
    lg = discover.lg_bindings(appliances, "home-1", lg_existing)
    assert lg["lg-purifier-aaaa"] == lg_existing["lg-purifier-aaaa"]
    assert lg["lg-purifier-bbbb"]["porch_id"] == "home-1/air-purifier-2"


def test_bindings_that_are_not_json_are_reported_not_used():
    bindings, problem = discover.parse_bindings({"MATTER_BINDINGS": "{1/1: entry}"}, "MATTER_BINDINGS")
    assert bindings == {} and "not valid JSON" in problem
    assert discover.parse_bindings({}, "MATTER_BINDINGS") == ({}, None)


def tapo_home() -> list[dict]:
    return [color_bulb(node_id=1), color_bulb(node_id=2), energy_plug(node_id=3),
            energy_plug(node_id=4, separate_sensor=True),
            node(5, {"1/29/0": [{"deviceType": 266}], "1/6/0": False, "3/29/0": [{"deviceType": 266}],
                     "3/6/0": False, "2/29/0": [{"deviceType": 0x0510}], "2/144/8": 1000})]


def test_matter_rows_say_which_endpoint_becomes_which_device():
    rows = {(r.node_id, r.endpoint_id): r for r in discover.matter_endpoints(tapo_home(), "home-1")}
    bulb = rows[(1, 1)]
    assert (bulb.device_types, bulb.porch_id, bulb.porch_type) == (
        ("Extended Color Light",), "home-1/matter-1-1", "OnOffLight")
    assert rows[(3, 1)].readings == "energy, power"
    assert rows[(4, 1)].readings == "energy (endpoint 2), power (endpoint 2)"
    sensor = rows[(4, 2)]
    assert (sensor.device_types, sensor.porch_id, sensor.readings) == (
        ("Electrical Sensor",), None, "energy, power -> home-1/matter-4-1")
    assert rows[(5, 2)].readings == "power not used: no single plug on this node"


def test_matter_bindings_suggest_unique_ids_from_the_product_name():
    bindings = discover.matter_bindings(tapo_home(), "home-1")
    assert bindings == {
        "1/1": {"porch_id": "home-1/l535e", "room": "Unassigned"},
        "2/1": {"porch_id": "home-1/l535e-2", "room": "Unassigned"},
        "3/1": {"porch_id": "home-1/p110m", "room": "Unassigned"},
        "4/1": {"porch_id": "home-1/p110m-2", "room": "Unassigned"},
        "5/1": {"porch_id": "home-1/onoffplug", "room": "Unassigned"},
        "5/3": {"porch_id": "home-1/onoffplug-2", "room": "Unassigned"},
    }


def test_run_against_a_matter_server_prints_a_pasteable_binding_line():
    async def scenario():
        server = FakeMatterServer(tapo_home())
        url = await server.start()
        out = io.StringIO()
        try:
            status = await discover.run({"MATTER_SERVER_URL": url}, out=out)
        finally:
            await server.stop()
        return status, out.getvalue(), server.requests

    status, text, requests = asyncio.run(scenario())
    assert status == 0
    line = next(line for line in text.splitlines() if line.startswith("MATTER_BINDINGS="))
    assert json.loads(line.removeprefix("MATTER_BINDINGS=").strip("'"))["1/1"]["porch_id"] == "home-1/l535e"
    # Discovery only listens: it never sends a command or reads a device.
    assert [r["command"] for r in requests] == ["start_listening"]


def test_run_says_when_the_matter_server_is_not_there():
    out = io.StringIO()
    status = asyncio.run(discover.run({"MATTER_SERVER_URL": "ws://127.0.0.1:9/ws"}, out=out, matter_timeout_s=0.3))
    assert status == 1
    assert "Could not reach it" in out.getvalue()


def test_run_needs_a_platform():
    assert asyncio.run(discover.run({}, out=io.StringIO())) == 2
