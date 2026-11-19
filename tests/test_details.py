"""Everything a platform reports about one device (details.py, GET /devices/{id}/details).

Each platform's own record becomes rows of (group, name, value) in plain words. What could
identify a device, the home or its network is never shown, and one platform that does not
answer never hides the others.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from porch_hub import details
from porch_hub.clusters import on_off
from porch_hub.drivers import DriverError
from porch_hub.main import Settings, create_app
from porch_hub.seed import demo_home

FAST = {"confirm_timeout_s": 0.3, "poll_interval_s": 0.02}
LOCK, PURIFIER = "home-1/front-door-lock", "home-1/air-purifier"


@pytest.mark.parametrize("key, expected", [
    ("airQualitySensor", "Air quality sensor"),
    ("filter_remain", "Filter remain"),
    ("RMSVoltage", "RMS voltage"),
    ("PM10Level", "PM10 level"),
    ("PM1", "PM1"),
    ("StartUpOnOff", "Start up on off"),
    ("GoogleTvDevice/MediaPlayback", "Media playback"),
    ("ir_led_night_vision", "IR LED night vision"),
    ("hdr", "HDR"),
    ("e2e_encryption", "E2E encryption"),
    ("wifi", "Wi-Fi"),
    ("softwareVersionString", "Software version"),
    ("currentBrightnessPercent", "Current brightness percent"),
])
def test_keys_become_plain_names(key, expected):
    assert details.words(key) == expected


@pytest.mark.parametrize("value, expected", [
    ("PLAYBACK_STATE_ENUM_PLAYING", "Playing"),
    ("CHARGING_COMPLETE", "Charging complete"),
    ("ONLINE", "Online"),
    ("LOW", "Low"),
    ("Living Room", "Living Room"),
    ("P110M", "P110M"),
    ("LG", "LG"),
    ("A", "A"),
    ("_", "_"),
])
def test_platform_enums_become_words_and_names_stay_as_they_are(value, expected):
    assert details.enum_text(value) == expected


@pytest.mark.parametrize("key, value, expected", [
    ("activityState", "ACTIVITY_STATE_ACTIVE", "Active"),
    ("colorMode", "COLOR_MODE_ENUM_COLOR_TEMPERATURE_MIREDS", "Color temperature mireds"),
    ("currentState", "PLAYBACK_STATE_ENUM_PAUSED", "Paused"),
    ("runState", "RUN_STATE", "Run state"),  # nothing would be left: kept whole
])
def test_an_enum_does_not_repeat_the_name_it_is_shown_under(key, value, expected):
    assert details.enum_text(value, key) == expected


@pytest.mark.parametrize("value, expected", [
    (None, "unknown"), (True, "yes"), (False, "no"), (1.50, "1.5"), (42, "42"),
    ([], "none"), ([720, 1080], "720, 1080"), (list(range(25)), ", ".join(map(str, range(20))) + ", and 5 more"),
    ([{"a": 1}], "1 item"), ([{"a": 1}, {"b": 2}], "2 items"),
    ({"serialNumber": "private-serial"}, "1 entry"), ({"a": 1, "b": 2}, "2 entries"),
])
def test_values_become_words(value, expected):
    assert details.value_text(value) == expected


@pytest.mark.parametrize("key", [
    "serialNumber", "macAddress", "mac_address", "MAC", "ssid", "bssid", "ipv4", "ipv6", "uniqueID", "unique_id",
    "networkId", "accessToken", "secret", "password", "credential", "fabricIndex", "nodeLabel", "email",
    "latitude", "longitude", "id", "ID", "deviceId", "structureID", "lg_id", "key", "apiKey", "api_key",
])
def test_what_could_identify_a_device_or_the_network_is_private(key):
    assert details.private(key)


@pytest.mark.parametrize("key", ["machineState", "android", "valid", "humid", "onOff", "currentLevel",
                                 "RMSVoltage", "remoteControlEnabled", "location", "timer", "hotkey"])
def test_ordinary_keys_are_not_mistaken_for_private_ones(key):
    assert not details.private(key)


def test_rows_leave_out_private_keys_bookkeeping_and_everything_under_them():
    record = {
        "softwareVersion": "1.3.0", "serialNumber": "private-serial", "macAddress": "private-mac",
        "deviceId": "private-id", "attributeList": [0, 1, 65531], "featureMap": 1, "timestamp": "2026-11-16T00:00:00Z",
        "wifi": {"ssid": "private-ssid", "signalStrength": -51}, "brightnessPercent": 49, "levelPercent": True,
        "network": {"ipv4": "private-address", "fabric": {"label": "private-label"}},
        "apps": [{"name": "YouTube"}, {"displayName": "Netflix"}],
        "deep": {"one": {"two": {"three": {"serialNumber": "private-deep"}}}},
    }
    rows = details.rows_from("Device", record)
    assert rows == [
        {"group": "Device", "name": "Software version", "value": "1.3.0"},
        {"group": "Device · Wi-Fi", "name": "Signal strength", "value": "-51"},
        {"group": "Device", "name": "Brightness percent", "value": "49%"},
        {"group": "Device", "name": "Level percent", "value": "yes"},  # not a number: no sign added
        {"group": "Device", "name": "Apps", "value": "YouTube, Netflix"},
        {"group": "Device · Deep · One · Two", "name": "Three", "value": "1 entry"},
    ]
    assert "private" not in json.dumps(rows)


@pytest.mark.parametrize("items, expected", [
    ([{"name": "YouTube"}, {"displayName": "Netflix"}], "YouTube, Netflix"),
    ([{"names": [{"nameSynonym": ["Living lamp", "Lamp"]}]}], "Living lamp"),
    ([{"keyCode": "KEY_CODE_VOLUME_UP"}, {"keyCode": "KEY_CODE_UP"}], "Volume up, Up"),
    ([{"x": 1}, {"y": 2}], "2 items"),
    ([{"name": str(i)} for i in range(22)], ", ".join(str(i) for i in range(20)) + ", and 2 more"),
])
def test_a_list_of_records_is_named_by_what_it_holds(items, expected):
    assert details.list_text(items) == expected


def test_the_hubs_own_model_reads_as_rows():
    rows = details.rows_from_clusters({"OnOff": on_off(True), "Raw": {"attributes": {"MeasuredValue": 21.5}}})
    assert {"group": "On off", "name": "On off", "value": "yes"} in rows
    assert {"group": "Raw", "name": "Measured value", "value": "21.5"} in rows


# The route

def test_each_platform_that_reaches_a_device_gets_its_own_section():
    home = demo_home(simulate_latency=False)
    with TestClient(create_app(home.drivers, Settings(**FAST))) as client:
        lock = client.get(f"/devices/{LOCK}/details").json()
        assert lock["device_id"] == LOCK and lock["name"]
        assert [(s["path"], s["online"]) for s in lock["sections"]] == [("matter", True), ("google_home", True)]
        assert all(s["rows"] and "problem" not in s for s in lock["sections"])

        purifier = client.get(f"/devices/{PURIFIER}/details").json()
        (lg,) = purifier["sections"]
        rows = {(r["group"], r["name"]): r["value"] for r in lg["rows"]}
        assert rows[("Air quality sensor", "PM2.5")] == "8 µg/m³"  # LG calls it PM2
        assert rows[("Filter info", "Filter remain percent")] == "87%"

        assert client.get("/devices/home-1/no-such-device/details").status_code == 404


def test_one_platform_that_fails_or_never_answers_does_not_hide_the_others():
    home = demo_home(simulate_latency=False)

    async def refuses(device_id):
        raise DriverError("Google Home could not be read (ReadTimeout)")

    async def hangs(device_id):
        await asyncio.sleep(10)

    home.google.details = refuses
    home.matter.details = hangs
    home.matter.call_timeout_s = 0.05
    with TestClient(create_app(home.drivers, Settings(**FAST))) as client:
        matter, google = client.get(f"/devices/{LOCK}/details").json()["sections"]
    assert matter == {"path": "matter", "online": True, "rows": [], "problem": "No answer in 0.05 seconds."}
    assert google == {"path": "google_home", "online": True, "rows": [],
                      "problem": "Google Home could not be read (ReadTimeout)"}


def test_an_unexpected_error_is_named_by_its_type_only():
    home = demo_home(simulate_latency=False)

    async def breaks(device_id):
        raise KeyError("private-platform-text")

    home.thinq.details = breaks
    with TestClient(create_app(home.drivers, Settings(**FAST))) as client:
        response = client.get(f"/devices/{PURIFIER}/details")
    (lg,) = response.json()["sections"]
    assert lg["problem"] == "The answer could not be read (KeyError)."
    assert "private" not in response.text


def test_in_offline_mode_the_details_are_the_digital_copy_and_say_so():
    home = demo_home(simulate_latency=False)

    async def must_not_be_asked(device_id):
        raise AssertionError("a platform was asked in offline mode")

    for driver in home.drivers:
        driver.details = must_not_be_asked
    with TestClient(create_app(home.drivers, Settings(demo_offline_mode=True, **FAST))) as client:
        (copy,) = client.get(f"/devices/{LOCK}/details").json()["sections"]
    assert copy["path"] == "digital_copy" and copy["rows"]
    assert "offline mode" in copy["problem"]


# The panel

def test_the_panel_script_is_served_and_both_device_views_use_it():
    home = demo_home(simulate_latency=False)
    with TestClient(create_app(home.drivers, Settings(**FAST))) as client:
        script = client.get("/details.js")
        assert script.status_code == 200 and "javascript" in script.headers["content-type"]
        assert 'customElements.define("porch-details"' in script.text
        assert "/details`" in script.text  # it reads GET /devices/{id}/details
        # Text from the hub goes in as text: no markup is ever built from it.
        assert "innerHTML" not in script.text and "insertAdjacentHTML" not in script.text
        for page in ("/digital-twin", "/home-3d"):
            html = client.get(page).text
            assert '<script src="/details.js"' in html and 'createElement("porch-details")' in html
