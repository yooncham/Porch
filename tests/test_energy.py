"""Power and energy for the twin pages' energy panel: real readings only, never an estimate."""

from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from porch_hub import clusters as c
from porch_hub.drivers import SimulatedMatterDriver
from porch_hub.drivers.google_home import GoogleHomeDriver
from porch_hub.energy import EnergyLog, current_a, energy_kwh, power_w, voltage_v
from porch_hub.main import Settings, create_app
from porch_hub.models import Cluster, ControlPath, Device, DeviceType, utcnow
from porch_hub.seed import demo_home

FAST = Settings(confirm_timeout_s=0.3, poll_interval_s=0.02)
METERED, PLAIN = "home-1/washer-plug", "home-1/tv-plug"


def plug(device_id: str, name: str, path: ControlPath, on: bool, milliwatts: int | None = None,
         milliwatt_hours: int | None = None) -> Device:
    clusters = {c.ON_OFF: c.on_off(on)}
    if milliwatts is not None:
        clusters[c.ELECTRICAL_POWER_MEASUREMENT] = Cluster(attributes={"ActivePower": milliwatts})
    if milliwatt_hours is not None:
        clusters[c.ELECTRICAL_ENERGY_MEASUREMENT] = Cluster(attributes={"CumulativeEnergyImported": milliwatt_hours})
    return Device(id=device_id, name=name, room="Laundry", device_type=DeviceType.ON_OFF_PLUG, online=True,
                  primary_control_path=path, clusters=clusters, last_updated_at=utcnow())


def home():
    """One plug paired over Matter, which reports power and energy, and one known only through
    Google Home, which says whether it is on."""
    matter = SimulatedMatterDriver([plug(METERED, "Washer plug", ControlPath.MATTER, True, 42_500, 1_250_000)])
    google = GoogleHomeDriver([plug(PLAIN, "TV plug", ControlPath.GOOGLE_HOME, True)])
    return matter, google


DASHBOARD = Path(__file__).parents[1] / "porch_hub" / "dashboard"


def test_both_twin_pages_carry_the_same_energy_panel():
    with TestClient(create_app(demo_home(simulate_latency=False).drivers, FAST)) as client:
        served = client.get("/energy.js")
    script = (DASHBOARD / "energy.js").read_text()
    assert served.status_code == 200 and served.headers["content-type"].startswith("text/javascript")
    assert served.headers["cache-control"] == "no-store" and served.text == script
    assert 'fetch("/home/energy"' in script and 'customElements.define("porch-energy"' in script
    # Names come from the hub: they go in as text, and the panel never invents a number.
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script
    assert "no power reading through" in script
    for page, placed in (("digital-twin.html", '<porch-energy layout="tiles">'),
                         ("home-3d.html", 'document.createElement("porch-energy")')):
        text = (DASHBOARD / page).read_text()
        assert '<script src="/energy.js" defer></script>' in text and placed in text, page
        assert "porch:select-device" in text, page  # a meter clicked opens that device on the page
        assert '"ElectricalPowerMeasurement", "ActivePower"' in text, page  # watts on the device itself


def test_the_panel_shows_volts_and_amps_for_a_plug_that_measures_them():
    script = (DASHBOARD / "energy.js").read_text()
    assert "meter.voltage_v.toFixed(1)} V" in script and "meter.current_a.toFixed(2)} A" in script
    assert 'typeof meter.voltage_v === "number"' in script  # a plug that does not measure them shows nothing


def test_units_are_watts_and_kilowatt_hours():
    reading = plug(METERED, "Washer plug", ControlPath.MATTER, True, 42_500, 1_250_000).clusters
    assert (power_w(reading), energy_kwh(reading)) == (42.5, 1.25)
    silent = plug(PLAIN, "TV plug", ControlPath.GOOGLE_HOME, True).clusters
    assert (power_w(silent), energy_kwh(silent)) == (None, None)
    # A switch that is on is not a reading of one milliwatt.
    assert power_w({c.ELECTRICAL_POWER_MEASUREMENT: Cluster(attributes={"ActivePower": True})}) is None
    # Volts and amps come from a plug that measures them (millivolts and milliamps), under either name.
    measuring = {c.ELECTRICAL_POWER_MEASUREMENT: Cluster(attributes={
        "ActivePower": 124_513, "RMSVoltage": 121_104, "RMSCurrent": 1_034})}
    assert (voltage_v(measuring), current_a(measuring)) == (121.1, 1.03)
    direct = {c.ELECTRICAL_POWER_MEASUREMENT: Cluster(attributes={"Voltage": 5_020, "ActiveCurrent": 480})}
    assert (voltage_v(direct), current_a(direct)) == (5.0, 0.48)
    assert (voltage_v(reading), current_a(reading)) == (None, None)


def test_the_panel_lists_every_plug_and_estimates_nothing():
    matter, google = home()
    with TestClient(create_app([matter, google], FAST)) as client:
        energy = client.get("/home/energy").json()
    assert (energy["total_w"], energy["reporting"]) == (42.5, 1)
    meters = {m["id"]: m for m in energy["meters"]}
    assert set(meters) == {METERED, PLAIN}
    assert {k: meters[METERED][k] for k in ("name", "room", "online", "path", "on", "power_w", "energy_kwh", "since_kwh")} == {
        "name": "Washer plug", "room": "Laundry", "online": True, "path": "matter", "on": True,
        "power_w": 42.5, "energy_kwh": 1.25, "since_kwh": 0.0}
    assert (meters[METERED]["voltage_v"], meters[METERED]["current_a"]) == (None, None)  # this plug measures neither
    # The plug Google Home lists is on, and that is all anyone knows about it.
    assert {k: meters[PLAIN][k] for k in ("on", "power_w", "energy_kwh", "since_kwh", "since", "history")} == {
        "on": True, "power_w": None, "energy_kwh": None, "since_kwh": None, "since": None, "history": []}


def test_a_home_where_nothing_reports_power_has_no_total():
    with TestClient(create_app(demo_home(simulate_latency=False).drivers, FAST)) as client:
        energy = client.get("/home/energy").json()
    assert (energy["total_w"], energy["reporting"]) == (None, 0)
    assert {m["name"] for m in energy["meters"]} == {"Desk lamp", "Space heater"}  # the demo home's two plugs
    assert all(m["power_w"] is None and m["history"] == [] for m in energy["meters"])


def test_power_changes_are_kept_as_they_happen_even_with_no_page_open():
    matter, google = home()
    with TestClient(create_app([matter, google], FAST)) as client:
        client.get("/home/energy")
        for milliwatts in (480_000, 480_000, 0):  # the washer starts, keeps going, stops
            matter.set_attribute(METERED, c.ELECTRICAL_POWER_MEASUREMENT, "ActivePower", milliwatts)
        matter.set_attribute(METERED, c.ELECTRICAL_ENERGY_MEASUREMENT, "CumulativeEnergyImported", 1_370_000)
        meter = next(m for m in client.get("/home/energy").json()["meters"] if m["id"] == METERED)
    assert [watts for _, watts in meter["history"]] == [42.5, 480.0, 0.0]  # changes only
    assert (meter["power_w"], meter["energy_kwh"], meter["since_kwh"]) == (0.0, 1.37, 0.12)


def test_the_log_forgets_old_readings_and_restarts_a_counter_that_was_reset():
    log = EnergyLog(keep=timedelta(hours=1), max_points=50)
    start = utcnow()
    for minute, milliwatts in enumerate((1000, 2000, 3000, 4000)):
        reading = plug(METERED, "Washer plug", ControlPath.MATTER, True, milliwatts, 500_000 + minute * 1000)
        log.record(METERED, reading.clusters, start + timedelta(minutes=40 * minute))
    device = plug(METERED, "Washer plug", ControlPath.MATTER, True, 4000, 503_000)
    meter = log.summary([device], start + timedelta(minutes=121))["meters"][0]
    # Two hours on, with one hour kept: the reading the window opens on, and what came after it.
    assert [watts for _, watts in meter["history"]] == [2.0, 3.0, 4.0]
    assert meter["since_kwh"] == 0.003

    reset = plug(METERED, "Washer plug", ControlPath.MATTER, True, 4000, 2_000)  # the plug was reset
    meter = log.summary([reset], start + timedelta(minutes=125))["meters"][0]
    assert (meter["energy_kwh"], meter["since_kwh"]) == (0.002, 0.0)


def test_a_reading_that_stands_still_while_the_plug_draws_power_is_called_stale():
    """What happened in a real home: the link to the plugs died and the last watts stayed on the
    panel for an hour as if they were current."""
    log = EnergyLog()
    start = utcnow()
    busy = plug(METERED, "Washer plug", ControlPath.MATTER, True, 124_513, 3_776_000)
    idle = plug(PLAIN, "TV plug", ControlPath.MATTER, True, 0, 17_000)
    small = plug("home-1/charger", "Charger", ControlPath.MATTER, True, 4_583, 95_000)

    def after(minutes, devices=(busy, idle, small)):
        summary = log.summary(list(devices), start + timedelta(minutes=minutes))
        return summary, {m["id"]: m for m in summary["meters"]}

    summary, meters = after(0)
    assert summary["total_w"] == 129.1 and not any(m["stale"] for m in meters.values())
    summary, meters = after(44)
    assert not meters[METERED]["stale"] and summary["reporting"] == 3
    # Not a digit moved for 45 minutes at 124 W: its counter alone should have ticked 90 times.
    summary, meters = after(46)
    assert meters[METERED]["stale"] is True and meters[METERED]["power_w"] == 124.5  # still shown, as the last heard
    assert meters[METERED]["changed_at"] == start.isoformat()
    # A plug that draws nothing, or too little to tell, is never called stale. The total leaves the stale one out.
    assert not meters[PLAIN]["stale"] and not meters["home-1/charger"]["stale"]
    assert (summary["total_w"], summary["reporting"]) == (4.6, 2)
    # The counter ticks again: the reading is current from that moment.
    moving = plug(METERED, "Washer plug", ControlPath.MATTER, True, 124_513, 3_777_000)
    summary, meters = after(47, (moving, idle, small))
    assert meters[METERED]["stale"] is False and summary["total_w"] == 129.1
    script = (DASHBOARD / "energy.js").read_text()
    assert "No new reading since ${clock(meter.changed_at)}" in script and "meter.stale" in script


def test_a_plug_whose_matter_link_is_down_shows_its_switch_and_no_watts():
    """The same plug on Matter (watts) and Google Home (its switch). With Matter down, the watts
    it last heard are not passed on as current."""
    matter = SimulatedMatterDriver([plug(METERED, "Washer plug", ControlPath.MATTER, True, 42_500, 1_250_000)])
    google = GoogleHomeDriver([plug(METERED, "Washer plug", ControlPath.GOOGLE_HOME, True)])
    with TestClient(create_app([matter, google], FAST)) as client:
        both = client.get("/home/energy").json()
        matter.set_online(METERED, False)
        cloud_only = client.get("/home/energy").json()
        device = next(d for d in client.get("/devices").json() if d["id"] == METERED)
        google.set_online(METERED, False)
        gone = next(d for d in client.get("/devices").json() if d["id"] == METERED)
        matter.set_online(METERED, True)
        back = client.get("/home/energy").json()
    assert (both["total_w"], both["meters"][0]["path"]) == (42.5, "matter")
    meter = cloud_only["meters"][0]
    assert (cloud_only["total_w"], meter["power_w"], meter["energy_kwh"], meter["on"], meter["path"]) == (
        None, None, None, True, "google_home")
    assert device["online"] and list(device["clusters"]) == ["OnOff"] and device["control_paths"] == ["matter", "google_home"]
    # With no path left the device is offline, and what was last known is kept for the record.
    assert not gone["online"] and "ElectricalPowerMeasurement" in gone["clusters"]
    assert back["total_w"] == 42.5
