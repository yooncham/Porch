"""The Ring driver, against a stand-in for Ring's partner API (shapes as seen from a real account)."""

import asyncio
import json
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from porch_hub.drivers import DeviceOffline, DriverError, RingBinding, RingDriver
from porch_hub.drivers import ring
from porch_hub.main import Settings, create_app

FAST = Settings(confirm_timeout_s=0.3, poll_interval_s=0.02)
DOORBELL = "home-1/front-doorbell"
BINDINGS = {"native-doorbell": RingBinding(DOORBELL, "Entry")}


def inventory(*, online=True, supports_button=True, configurations=False):
    """Ring's device list. With configurations, the full read the details panel asks for: Ring
    then also sends each device's settings, and its status and capabilities in full."""
    document = {
        "data": [
            {"type": "devices", "id": "native-doorbell", "attributes": {"name": "Front Door"},
             "relationships": {
                 "capabilities": {"data": {"type": "device-capabilities", "id": "private-capability"}},
                 "status": {"data": {"type": "device-status", "id": "private-status"}}}},
            {"type": "devices", "id": "private-camera", "attributes": {"name": "Indoor Cam"}},
        ],
        "included": [
            {"type": "device-capabilities", "id": "private-capability",
             "attributes": {"button_press": {"supported": supports_button}}},
            {"type": "device-status", "id": "private-status", "attributes": {"online": online}},
        ],
    }
    if configurations:
        document["data"][0]["relationships"]["configurations"] = {
            "data": {"type": "device-configurations", "id": "private-configuration"}}
        document["included"][0]["attributes"].update({
            "motion_detection": {"supported": True, "configurations": ["motion_zones", "sensitivity"]},
            "video": {"supported_resolutions": [720, 1080], "ratio": "16:9"},
            "siren": None})
        document["included"][1]["attributes"].update({
            "battery_life": 87, "mac_address": "private-mac",
            "wifi": {"ssid": "private-ssid", "signal_strength": -51}})
        document["included"].append({"type": "device-configurations", "id": "private-configuration", "attributes": {
            "motion_detection": {"enabled": True, "motion_zones": [{"name": "private-zone-1"}, {"name": "private-zone-2"}]},
            "image_enhancements": {"privacy_zones": [{"vertices": "private-vertices"}]},
            "volume": {"chime": 7}}})
    return document


class FakeRing:
    def __init__(self, path):
        self.path = path
        self.online = True
        self.refreshes: list[str] = []
        self.gets = 0
        self.reject_tokens: set[str] = set()
        self.status = 200
        self.rotate = True
        ring.RingAuthorization(client_id="client", client_secret="private-secret",
                               refresh_token="refresh-0").save(path)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert str(request.url) == ring.TOKEN_URL
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["refresh_token"] and form["client_secret"] == ["private-secret"]
            self.refreshes.extend(form["refresh_token"])
            answer = {"access_token": f"private-access-{len(self.refreshes)}", "expires_in": 14400}
            if self.rotate:
                answer["refresh_token"] = f"refresh-{len(self.refreshes)}"
            return httpx.Response(200, json=answer)
        assert request.method == "GET" and request.url.host == "api.amazonvision.com"
        include = request.url.params["include"]
        assert request.url.path == "/v1/devices" and include in ("status,capabilities", "status,capabilities,configurations")
        self.gets += 1
        token = request.headers["authorization"].removeprefix("Bearer ")
        if token in self.reject_tokens:
            return httpx.Response(401, json={"errors": [{"detail": "private-error-detail"}]})
        if self.status != 200:
            return httpx.Response(self.status, json={"errors": [{"detail": "private-error-detail"}]})
        return httpx.Response(200, json=inventory(online=self.online, configurations=include.endswith("configurations")))

    def driver(self, bindings=BINDINGS, **options) -> RingDriver:
        return RingDriver(self.path, bindings=dict(bindings),
                          client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
                          **options)


@pytest.fixture
def fake(tmp_path):
    return FakeRing(tmp_path / "ring-authorization.json")


def run(scenario):
    return asyncio.run(asyncio.wait_for(scenario(), 10))


def test_the_hub_lists_the_doorbell_and_nothing_else(fake):
    with TestClient(create_app([fake.driver()], FAST)) as client:
        response = client.get("/devices")
        (doorbell,) = response.json()
        assert doorbell["id"] == DOORBELL and doorbell["name"] == "Front Door" and doorbell["room"] == "Entry"
        assert doorbell["device_type"] == "Doorbell" and doorbell["online"] is True
        assert doorbell["primary_control_path"] == "ring" and doorbell["control_paths"] == ["ring"]
        assert doorbell["clusters"] == {"PorchDoorbellExtension": {
            "attributes": {"ButtonPressSupported": True}, "commands": []}}
        # Ring's own ids, the camera and every token stay out of what the hub serves.
        assert "private-" not in response.text and "native-" not in response.text and "Indoor Cam" not in response.text

        assert client.get(f"/devices/{DOORBELL}/state").json()["PorchDoorbellExtension"]["attributes"] == {
            "ButtonPressSupported": True}
        refused = client.post(f"/devices/{DOORBELL}/commands",
                              json={"command": "On", "params": {}, "idempotency_key": "k-1"})
        assert refused.status_code == 422  # a doorbell has no commands

        fake.online = False
        assert client.get(f"/devices/{DOORBELL}/state").status_code == 503
    # One refresh for the whole session, and the token Ring replaced was saved privately.
    assert fake.refreshes == ["refresh-0"]
    assert ring.RingAuthorization.load(fake.path).refresh_token == "refresh-1"
    assert fake.path.stat().st_mode & 0o777 == 0o600


def test_details_show_status_support_and_settings_and_nothing_that_identifies_the_doorbell(fake):
    with TestClient(create_app([fake.driver()], FAST)) as client:
        response = client.get(f"/devices/{DOORBELL}/details")
        (section,) = response.json()["sections"]
        assert section["path"] == "ring" and section["online"] is True and "problem" not in section
        assert [(r["group"], r["name"], r["value"]) for r in section["rows"]] == [
            ("Device", "Name in Ring", "Front Door"),
            ("Status", "Online", "yes"),
            ("Status", "Battery life", "87"),
            ("Status · Wi-Fi", "Signal strength", "-51"),
            ("What it supports", "Button press", "yes"),
            ("What it supports", "Motion detection", "yes (settings: motion zones, sensitivity)"),
            ("What it supports", "Video", "resolutions 720, 1080, ratio 16:9"),  # the siren Ring lists as null is left out
            ("Settings · Motion detection", "Enabled", "yes"),
            ("Settings · Motion detection", "Motion zones", "2 zones"),
            ("Settings · Image enhancements", "Privacy zones", "1 zone"),
            ("Settings · Volume", "Chime", "7"),
        ]
        # Ring's ids, the network, the zones' names and shapes, and every token stay out.
        assert "private-" not in response.text and "native-" not in response.text

        fake.status = 503
        (failed,) = client.get(f"/devices/{DOORBELL}/details").json()["sections"]
        assert failed["rows"] == [] and failed["problem"] == "Ring could not be read (HTTPStatusError, HTTP 503)"


def test_ids_come_from_bindings_or_from_the_name_and_a_stable_mark(fake):
    async def scenario():
        driver = fake.driver(bindings={})
        await driver.start()
        return await driver.list_devices(), driver.rows()

    (doorbell,), rows = run(scenario)
    assert doorbell.id.startswith("home-1/front-door-") and len(doorbell.id.rsplit("-", 1)[1]) == 6
    assert doorbell.room == "Unassigned"
    assert [(r.name, r.online, r.porch_id) for r in rows] == [
        ("Front Door", True, doorbell.id), ("Indoor Cam", False, None)]


def test_the_list_is_cached_and_a_state_read_asks_ring_again(fake):
    async def scenario():
        driver = fake.driver()
        await driver.start()
        await driver.list_devices()
        await driver.list_devices()
        listed = fake.gets
        fake.online = False
        with pytest.raises(DeviceOffline):
            await driver.read_state(DOORBELL)
        return listed, fake.gets, (await driver.list_devices())[0].online

    assert run(scenario) == (1, 2, False)


def test_a_rejected_token_is_replaced_and_tried_once_more(fake):
    async def scenario():
        driver = fake.driver()
        await driver.start()
        fake.reject_tokens.add("private-access-1")
        state = await driver.read_state(DOORBELL)
        fake.reject_tokens.add("private-access-2")
        fake.reject_tokens.add("private-access-3")
        with pytest.raises(DriverError) as failed:
            await driver.read_state(DOORBELL)
        return state, str(failed.value)

    state, error = run(scenario)
    assert "PorchDoorbellExtension" in state
    assert error == "Ring could not be read (HTTPStatusError, HTTP 401)"
    assert fake.refreshes == ["refresh-0", "refresh-1", "refresh-2"]


def test_a_failed_listing_names_no_token_and_backs_off(fake):
    fake.status = 500

    async def scenario():
        driver = fake.driver()
        with pytest.raises(DriverError) as failed:
            await driver.start()
        asked = fake.gets
        with pytest.raises(DriverError) as not_started:
            await driver.list_devices()
        with pytest.raises(DriverError):
            await driver.start()  # the hub retries the start; inside the back-off Ring is not asked again
        return str(failed.value), str(not_started.value), asked, fake.gets

    error, not_started, asked, asked_after = run(scenario)
    assert error == not_started == "Ring could not be read (HTTPStatusError, HTTP 500)"
    assert (asked, asked_after) == (1, 1)


def test_a_refresh_without_a_new_refresh_token_keeps_the_old_one(fake):
    fake.rotate = False

    async def scenario():
        driver = fake.driver()
        await driver.start()
        return await driver.list_devices()

    assert len(run(scenario)) == 1
    saved = ring.RingAuthorization.load(fake.path)
    assert saved.refresh_token == "refresh-0" and saved.access_token == "private-access-1"


@pytest.mark.parametrize("problem, expected", [
    ("missing", "ring-authorization.json is missing"),
    ("readable_by_others", "only you can read"),
    ("symlink", "is a symlink"),
    ("damaged", "cannot be used"),
])
def test_the_link_file_must_be_there_private_and_whole(fake, tmp_path, problem, expected):
    if problem == "missing":
        fake.path.unlink()
    elif problem == "readable_by_others":
        fake.path.chmod(0o644)
    elif problem == "symlink":
        real = tmp_path / "elsewhere.json"
        fake.path.rename(real)
        fake.path.symlink_to(real)
    else:
        fake.path.write_text('{"client_id": "client", "client_secret": "private-secret"}')
        fake.path.chmod(0o600)

    async def scenario():
        with pytest.raises(DriverError) as failed:
            await fake.driver().start()
        return str(failed.value)

    error = run(scenario)
    assert error.startswith("Ring is not set up:") and expected in error and "private-secret" not in error
    assert fake.gets == 0 and fake.refreshes == []


def test_an_answer_of_another_shape_is_refused():
    assert [d.doorbell for d in ring.ring_devices(inventory(supports_button=False))] == [False, False]
    for malformed in ({}, {"data": None}, {"data": [{"type": "devices"}]}, []):
        with pytest.raises(ValueError):
            ring.ring_devices(malformed)
    twice = inventory()
    twice["included"].append({"type": "device-status", "id": "private-status", "attributes": {"online": False}})
    with pytest.raises(ValueError, match="twice"):
        ring.ring_devices(twice)
    # A status Ring did not send is not passed on as online.
    no_status = inventory()
    no_status["included"] = no_status["included"][:1]
    assert ring.ring_devices(no_status)[0].online is False


def test_driver_from_env_and_the_configure_command(tmp_path, monkeypatch, capsys):
    path = tmp_path / "ring.json"
    driver = ring.ring_driver_from_env({
        "RING_AUTHORIZATION_FILE": str(path), "PORCH_HOME_ID": "home-9",
        "RING_BINDINGS": json.dumps({"native-doorbell": {"porch_id": "home-9/bell"}})})
    assert driver._bindings == {"native-doorbell": RingBinding("home-9/bell", "Unassigned")}
    assert driver._authorization_path == path and driver._home_id == "home-9"

    typed = iter(["client", "private-secret", "private-refresh"])
    monkeypatch.setattr(ring.getpass, "getpass", lambda prompt: next(typed))
    assert ring.main(["--file", str(path), "configure"]) == 0
    assert path.stat().st_mode & 0o777 == 0o600
    assert ring.RingAuthorization.load(path).refresh_token == "private-refresh"
    assert "private-" not in capsys.readouterr().out

    typed = iter(["client", "", "private-refresh"])
    assert ring.main(["--file", str(tmp_path / "other.json"), "configure"]) == 1
    captured = capsys.readouterr()
    assert "private-" not in captured.out + captured.err and not (tmp_path / "other.json").exists()
