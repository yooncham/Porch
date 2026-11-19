"""The 2D digital twin: the home's floor plan with every device on it, live from the hub."""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from porch_hub.main import Settings, create_app
from porch_hub.models import DeviceType
from porch_hub.seed import demo_home

PAGE = Path(__file__).parents[1] / "porch_hub" / "dashboard" / "digital-twin.html"


def test_digital_twin_is_served_by_the_hub():
    with TestClient(create_app(demo_home(simulate_latency=False).drivers, Settings())) as client:
        response = client.get("/digital-twin")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert "<title>Porch Digital Twin</title>" in response.text


def test_digital_twin_needs_no_internet():
    page = PAGE.read_text()
    assert not re.search(r"""(src|href)\s*=\s*["']https?://""", page)
    assert "@import" not in page and "fonts.googleapis" not in page


def test_digital_twin_draws_the_homes_plan_and_follows_the_hub():
    """Nothing about a home is written into the page: rooms and places come from the layout, devices
    and their changes from the hub."""
    page = PAGE.read_text()
    for text in ['"/home/layout"', '"/devices"', "/ws/events", "state_changed", "online_changed",
                 "command_simulated", "command_executed", "protection_changed", "mode_changed",
                 "placements", "matches", "extras", "Not on the plan:"]:
        assert text in page, text
    for home_specific in ("Living stand", "home-1/", "Entrance", "Yale"):
        assert home_specific not in page, home_specific


def test_digital_twin_has_a_picture_for_every_kind_of_device():
    script = PAGE.read_text().split("<script>")[-1]
    icons = script.split("const ICONS = {")[1].split("};")[0]
    for device_type in DeviceType:
        assert f"{device_type.value}:" in icons or device_type.value.startswith("Laundry"), device_type
    assert "/^Laundry/" in script  # washers and dryers share the appliance picture


def test_digital_twin_shows_readings_in_plain_words_with_units():
    script = PAGE.read_text().split("<script>")[-1]
    names = script.split("const READINGS = {")[1].split("};")[0]
    for hub_name, label in (("LevelControl.CurrentLevel", "Brightness"), ("ColorControl.ColorTemperatureMireds", "White tone"),
                            ("Pm1ConcentrationMeasurement.MeasuredValue", "PM1"),
                            ("Pm10ConcentrationMeasurement.MeasuredValue", "PM10"),
                            ("HepaFilterMonitoring.Condition", "Filter left"),
                            ("ElectricalPowerMeasurement.RMSVoltage", "Voltage"),
                            ("ElectricalPowerMeasurement.RMSCurrent", "Current")):
        assert f'"{hub_name}": ["{label}"' in names, hub_name
    # The hub's own name stays one hover away, an unknown value is said to be unknown, and a
    # lamp that is on says how bright it is set.
    assert "term.title = `${clusterName}.${name}`" in script and '"unknown"]' in script
    assert "`ON${bright}${draws}`" in script


def test_digital_twin_says_what_a_tv_is_doing():
    script = PAGE.read_text().split("<script>")[-1]
    tv = script.split('case "CastingVideoPlayer": {')[1].split("\n    }\n")[0]
    # Standby wins over what it last played; an unknown state is never shown as off.
    assert tv.index('"STANDBY"') < tv.index('"PLAYING"') < tv.index('"IN USE"') < tv.index('"UNKNOWN"')
    # A TV nobody is using says only that nothing is playing: that is shown, not "unknown".
    assert tv.index('"IN USE"') < tv.index('"NOT PLAYING"') < tv.index('"UNKNOWN"')
    assert 'Play: "playing"' in script  # the feed's word for a confirmed Play


def test_digital_twin_shows_a_refusal_and_offers_the_owners_window():
    script = PAGE.read_text().split("<script>")[-1]
    assert "/^Family protection/" in script and "openProtectionPanel" in script
    assert "Dry run only" in PAGE.read_text()


def test_digital_twin_never_writes_hub_text_as_html():
    script = PAGE.read_text().split("<script>")[-1]
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script
