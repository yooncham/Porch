"""The live Google Home driver, against a stand-in for Google's Home MCP server.

The stand-in answers with the shapes seen from a real home (Tapo bulbs and plugs, a Google TV
Streamer) and the shapes Google documents for list_home_states and run_home_actions.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fake_google_home_server import (DISPLAY, LIGHT, PLUG, TV, home_metadata, home_resources, state_of,
                                     tv_metadata, tv_resource, tv_state)
from fastapi.testclient import TestClient

from porch_hub import protection
from porch_hub.drivers import CommandRejected, DeviceOffline, DriverError, GoogleHomeBinding, GoogleHomeMcpDriver
from porch_hub.drivers.google_home import driver as driver_module
from porch_hub.drivers.google_home import inventory, login, session
from porch_hub.drivers.google_home.driver import KeptSession, google_home_driver_from_env
from porch_hub.main import Settings, create_app

FAST = Settings(confirm_timeout_s=0.3, poll_interval_s=0.02)
LAMP = "home-1/living-stand"
TV_ID = "home-1/living-room-tv"
BINDINGS = {LIGHT: GoogleHomeBinding(LAMP), PLUG: GoogleHomeBinding("home-1/plug-1", "Hall")}
TV_BINDINGS = {**BINDINGS, TV: GoogleHomeBinding(TV_ID)}
PLAYBACK = {"MediaPlayback.Play": "PLAYBACK_STATE_ENUM_PLAYING", "MediaPlayback.Pause": "PLAYBACK_STATE_ENUM_PAUSED"}
SECRET = "PERMISSION_DENIED: private-provider-detail Bearer private-access-token"


def result(payload, *, text_only=False):
    if text_only:
        return {"content": [{"type": "text", "text": json.dumps(payload)}]}
    return {"structuredContent": payload, "isError": False}


class FakeGoogle:
    def __init__(self):
        self.resources = home_resources()
        self.metadata = home_metadata()
        self.states = {LIGHT: state_of(LIGHT, "GoogleLightDevice"),
                       PLUG: state_of(PLUG, "GooglePluginUnitDevice", on=True)}
        self.tools = ["list_homes", "list_home_resources", "list_home_states", "run_home_actions"]
        self.calls, self.actions, self.opened = [], [], []
        self.closed = 0
        self.failures = {}  # tool name -> exceptions to raise on its next calls, or a tool error to return
        self.apply = True
        self.action_result = None
        self.allow_actions = False

    def add_tv(self, **state):
        """Put the TV in the home. state is what it reports (see tv_state)."""
        self.resources.append(tv_resource())
        self.metadata.append(tv_metadata())
        self.states[TV] = tv_state(**state)
        return self

    @asynccontextmanager
    async def session(self, directory, *, auth_timeout, allow_actions):
        self.opened.append(allow_actions)
        self.allow_actions = allow_actions
        try:
            yield self
        finally:
            self.closed += 1

    def names(self):
        return [name for name, _ in self.calls]

    def _fail(self, name):
        pending = self.failures.get(name)
        if isinstance(pending, list) and pending:
            raise pending.pop(0)
        return pending if isinstance(pending, dict) else None

    async def list_tools(self, cursor):
        self.calls.append(("tools/list", cursor))
        self._fail("tools/list")
        return {"tools": [{"name": name} for name in self.tools]}

    async def call_tool(self, name, args):
        self.calls.append((name, deepcopy(args)))
        if (tool_error := self._fail(name)) is not None:
            return tool_error
        if name == "list_homes":
            return result({"structures": [{"structureId": "private-home"}]}, text_only=True)
        if name == "list_home_resources":
            return result({"resources": self.resources, "resourceMetadata": self.metadata})
        if name == "list_home_states":
            return result({"states": [deepcopy(self.states[i]) for i in args["resourceSearch"]["resourceIds"]
                                      if i in self.states]})
        assert name == "run_home_actions" and self.allow_actions, "an action outside an action session"
        self.actions.append(deepcopy(args))
        request = args["homeActionRequests"][0]
        if self.apply:
            traits = self.states[request["id"]]["traits"]
            if request["command"] in PLAYBACK:
                traits["MediaPlayback"]["state"]["currentState"] = PLAYBACK[request["command"]]
            else:
                traits["OnOff"]["state"]["onOff"] = request["command"] == "OnOff.On"
        if self.action_result is not None:
            if isinstance(self.action_result, Exception):
                raise self.action_result
            return deepcopy(self.action_result)
        # A real TV answers Pause with its own status; a light's answer carries none here.
        answer = {"status": "STATUS_ENUM_SUCCESS"} if request["command"] in PLAYBACK else {}
        return result({"homeActionResponses": [{"id": request["id"], "response": answer}]})


def make_driver(google, tmp_path, bindings=BINDINGS, **options) -> GoogleHomeMcpDriver:
    driver = GoogleHomeMcpDriver(tmp_path, bindings=dict(bindings), session_factory=google.session,
                                 **{"request_timeout_s": 1.0, "idle_s": 5.0, **options})
    driver.read_back_interval_s, driver.read_back_timeout_s = 0.01, 0.3
    return driver


def run(scenario):
    return asyncio.run(asyncio.wait_for(scenario(), 10))


@pytest.fixture
def google():
    return FakeGoogle()


@pytest.fixture
def hub(google, tmp_path):
    with TestClient(create_app([make_driver(google, tmp_path)], FAST)) as client:
        yield google, client


def send(client, device_id, name, key):
    return client.post(f"/devices/{device_id}/commands",
                       json={"command": name, "params": {}, "idempotency_key": key})


def same_answer(first: dict, again: dict) -> bool:
    """Whether a repeat with the same key got the first answer back. The hub with the gateway
    fixes also marks the repeat as replayed, which is the one field allowed to differ."""
    def without_mark(answer):
        return {k: v for k, v in answer.items() if k != "replayed"}
    return without_mark(first) == without_mark(again)


# Listing and reading

def test_lists_lights_and_plugs_and_leaves_other_kinds_out(google, tmp_path):
    async def scenario():
        driver = make_driver(google, tmp_path, bindings={})
        await driver.start()
        try:
            return await driver.list_devices(), driver.rows()
        finally:
            await driver.stop()

    devices, rows = run(scenario)
    light, plug = devices
    assert (light.name, light.device_type, light.room, light.online) == (
        "Living stand", "OnOffLight", "Living Room", True)
    assert light.clusters["OnOff"].attributes == {"OnOff": False} and light.clusters["OnOff"].commands == ["On", "Off"]
    assert light.primary_control_path == "google_home"
    # A plug is listed and read, never switched, even though Google describes On and Off for it.
    assert (plug.device_type, plug.clusters["OnOff"].attributes, plug.clusters["OnOff"].commands) == (
        "OnOffPlug", {"OnOff": True}, [])
    # Without a binding the id is the name plus a mark that stays the same for this device.
    assert light.id.startswith("home-1/living-stand-") and len(light.id.rsplit("-", 1)[1]) == 6
    assert "private" not in json.dumps([d.model_dump(mode="json") for d in devices])
    display = next(row for row in rows if row.google_id == DISPLAY)
    assert (display.porch_id, display.hub_type, display.online) == (None, None, None)
    assert [row.porch_id for row in rows[:2]] == [light.id, plug.id]


def test_bindings_give_ids_and_can_replace_googles_room(hub):
    _, client = hub
    devices = {d["id"]: d for d in client.get("/devices").json()}
    assert set(devices) == {LAMP, "home-1/plug-1"}
    assert devices[LAMP]["room"] == "Living Room" and devices["home-1/plug-1"]["room"] == "Hall"


def test_the_list_is_cached_and_a_burst_of_calls_shares_one_session(google, tmp_path):
    async def scenario():
        driver = make_driver(google, tmp_path, idle_s=0.05)
        await driver.start()
        try:
            await driver.list_devices()
            await driver.read_state(LAMP)
            burst = (list(google.opened), google.names().count("list_home_resources"), google.closed)
            await asyncio.sleep(0.15)  # idle: the session closes by itself
            idle = google.closed
            await driver.read_state(LAMP)
            return burst, idle, list(google.opened)
        finally:
            await driver.stop()

    burst, closed_when_idle, opened = run(scenario)
    assert burst == ([False], 1, 0)
    assert closed_when_idle == 1 and opened == [False, False]


def test_unknown_power_is_never_passed_on_as_off(google, tmp_path):
    google.states[LIGHT]["traits"]["OnOff"]["stateReportingOmitted"] = True
    google.states[PLUG] = state_of(PLUG, "GooglePluginUnitDevice", on=True, online=False)

    async def scenario():
        driver = make_driver(google, tmp_path)
        await driver.start()
        try:
            devices = {d.id: d for d in await driver.list_devices()}
            with pytest.raises(DeviceOffline):
                await driver.read_state("home-1/plug-1")
            return devices
        finally:
            await driver.stop()

    devices = run(scenario)
    assert devices[LAMP].online and devices[LAMP].clusters["OnOff"].attributes == {"OnOff": None}
    assert not devices["home-1/plug-1"].online
    assert devices["home-1/plug-1"].clusters["OnOff"].attributes == {"OnOff": None}


def test_a_light_shows_its_brightness_and_white_tone_when_google_reports_them(google, tmp_path):
    """Read only: the hub still only switches a light on and off."""
    google.states[LIGHT] = state_of(LIGHT, "GoogleLightDevice", on=True, level=157, mireds=370)
    google.states[PLUG] = state_of(PLUG, "GooglePluginUnitDevice", on=True, level=200)  # not a plug's to report

    async def scenario():
        driver = make_driver(google, tmp_path)
        await driver.start()
        try:
            lit = {d.id: d for d in await driver.list_devices()}
            color = google.states[LIGHT]["traits"]["ColorControl"]["state"]
            color["colorMode"] = "COLOR_MODE_ENUM_CURRENT_HUE_AND_CURRENT_SATURATION"
            google.states[LIGHT]["traits"]["LevelControl"]["state"]["currentLevel"] = 0  # not a level a light has
            in_colour = await driver.read_state(LAMP)
            google.states[LIGHT] = state_of(LIGHT, "GoogleLightDevice", on=True)
            plain = await driver.read_state(LAMP)
            return lit, in_colour, plain
        finally:
            await driver.stop()

    lit, in_colour, plain = run(scenario)
    lamp = lit[LAMP].clusters
    assert lamp["LevelControl"].attributes == {"CurrentLevel": 157} and lamp["LevelControl"].commands == []
    assert lamp["ColorControl"].attributes == {"ColorTemperatureMireds": 370} and lamp["ColorControl"].commands == []
    assert lamp["OnOff"].commands == ["On", "Off"]
    assert list(lit["home-1/plug-1"].clusters) == ["OnOff"]
    # In a colour mode the white tone is stale, and a level outside 1 to 254 is no reading: both are left out.
    assert list(in_colour) == ["OnOff"]
    # A light that reports neither is still a light.
    assert list(plain) == ["OnOff"]


def test_a_state_of_another_kind_is_not_trusted(google, tmp_path):
    google.states[LIGHT]["type"] = "GooglePluginUnitDevice"

    async def scenario():
        driver = make_driver(google, tmp_path)
        await driver.start()
        try:
            return {d.id: d for d in await driver.list_devices()}[LAMP]
        finally:
            await driver.stop()

    lamp = run(scenario)
    assert not lamp.online and lamp.clusters["OnOff"].attributes == {"OnOff": None}


def test_a_stale_session_is_replaced_once_for_a_read(google, tmp_path):
    async def scenario():
        driver = make_driver(google, tmp_path)
        await driver.start()
        try:
            google.failures["list_home_states"] = [ConnectionError(SECRET)]
            state = await driver.read_state(LAMP)
            google.failures["list_home_states"] = [ConnectionError(SECRET), ConnectionError(SECRET)]
            with pytest.raises(DriverError) as failed:
                await driver.read_state(LAMP)
            return state, str(failed.value), list(google.opened)
        finally:
            await driver.stop()

    state, error, opened = run(scenario)
    assert state["OnOff"].attributes == {"OnOff": False}
    assert error == "Google Home did not answer (ConnectionError)"
    # The first read failed on the starting session and passed on a second one. The next read
    # failed on that one and on a third, and then gave up.
    assert opened == [False, False, False]


def test_a_failed_listing_says_why_without_googles_text_and_backs_off(google, tmp_path, monkeypatch):
    google.failures["list_homes"] = {"isError": True, "content": [{"type": "text", "text": SECRET}]}

    async def scenario():
        driver = make_driver(google, tmp_path)
        with pytest.raises(DriverError) as failed:
            await driver.start()
        opened = len(google.opened)
        with pytest.raises(DriverError) as not_started:
            await driver.list_devices()  # not connected: no new session
        with pytest.raises(DriverError):
            await driver.start()  # the hub retries the start; inside the back-off it does not ask Google again
        return str(failed.value), str(not_started.value), opened, len(google.opened)

    error, not_started, opened, opened_after = run(scenario)
    assert error == "Google Home's device list could not be read (RemoteToolError in list_homes)"
    assert not_started == error and "private" not in error
    assert opened == 2 and opened_after == 2  # the first try and one fresh session, then nothing

    # Once it answers again and the back-off has passed, the list comes back.
    google.failures.clear()

    async def recovered():
        driver = make_driver(google, tmp_path)
        with pytest.raises(DriverError):
            google.failures["list_homes"] = [TimeoutError()]
            google.failures["tools/list"] = [TimeoutError()]
            await driver.start()
        monkeypatch.setattr(driver_module, "RETRY_S", 0.0)
        driver._retry_at = 0.0
        await driver.start()
        try:
            return len(await driver.list_devices())
        finally:
            await driver.stop()

    assert run(recovered) == 2


def test_a_subscription_refusal_is_named(google, tmp_path):
    google.failures["list_homes"] = {"isError": True, "content": [
        {"type": "text", "text": "At least one Home Premium Advanced subscription is required."}]}

    async def scenario():
        with pytest.raises(DriverError) as failed:
            await make_driver(google, tmp_path).start()
        return str(failed.value)

    assert run(scenario) == ("Google Home's device list could not be read (RemoteToolError in list_homes: Google "
                             "requires a Google Home Premium Advanced subscription for this account)")


# Commands

def test_switching_a_light_is_checked_sent_once_and_confirmed(hub):
    google, client = hub
    done = send(client, LAMP, "On", "k-1").json()
    assert done["status"] == "done" and done["control_path"] == "google_home" and done["simulated"] is False
    assert done["state_after"]["OnOff"]["attributes"] == {"OnOff": True}
    assert google.actions == [{"structureId": "private-home", "homeActionRequests": [
        {"id": LIGHT, "type": "GoogleLightDevice", "command": "OnOff.On", "parameters": {}}]}]
    assert google.opened == [False, True]  # the action tool is offered only to the session that sends

    assert same_answer(done, send(client, LAMP, "On", "k-1").json())  # the same key replays
    assert send(client, LAMP, "On", "k-2").json()["status"] == "done"  # already on: nothing to send
    assert len(google.actions) == 1
    assert send(client, LAMP, "Off", "k-3").json()["state_after"]["OnOff"]["attributes"] == {"OnOff": False}
    assert [a["homeActionRequests"][0]["command"] for a in google.actions] == ["OnOff.On", "OnOff.Off"]
    assert client.get(f"/devices/{LAMP}/state").json()["OnOff"]["attributes"] == {"OnOff": False}


def test_a_plug_and_an_unknown_command_are_refused_before_google_is_asked(hub):
    google, client = hub
    before = len(google.calls)
    assert send(client, "home-1/plug-1", "On", "k-1").status_code == 422
    assert send(client, LAMP, "Toggle", "k-2").status_code == 422
    dry_run = client.post(f"/devices/{LAMP}/simulate", json={"command": "On"}).json()
    assert dry_run["would_succeed"] and dry_run["control_path"] == "google_home"
    assert len(google.calls) == before and google.actions == []


def test_a_plug_the_owner_lets_porch_switch_is_switched_checked_and_confirmed(google, tmp_path):
    """The owner's binding says "switchable": true. Google's description still decides: On and
    Off only while it allows them."""
    bindings = {**BINDINGS, PLUG: GoogleHomeBinding("home-1/plug-1", "Hall", switchable=True)}
    with TestClient(create_app([make_driver(google, tmp_path, bindings=bindings)], FAST)) as client:
        plug = next(d for d in client.get("/devices").json() if d["id"] == "home-1/plug-1")
        assert plug["device_type"] == "OnOffPlug" and plug["clusters"]["OnOff"]["commands"] == ["On", "Off"]
        off = send(client, "home-1/plug-1", "Off", "k-1").json()
        assert off["status"] == "done" and off["state_after"]["OnOff"]["attributes"] == {"OnOff": False}
        assert google.actions == [{"structureId": "private-home", "homeActionRequests": [
            {"id": PLUG, "type": "GooglePluginUnitDevice", "command": "OnOff.Off", "parameters": {}}]}]
        assert send(client, "home-1/plug-1", "Toggle", "k-2").status_code == 422

        # Google no longer describes On for the plug: the hub stops offering it and sends nothing.
        plug_meta = next(m for m in google.metadata if m["type"] == "GooglePluginUnitDevice")
        del plug_meta["traits"]["OnOff"]["commands"]["OnOff.On"]

    async def described_again():
        driver = make_driver(google, tmp_path, bindings=bindings)
        await driver.start()
        try:
            listed = {d.id: d for d in await driver.list_devices()}
            with pytest.raises(CommandRejected):
                driver.preflight("home-1/plug-1", "On", {}, {})
            return listed["home-1/plug-1"].clusters["OnOff"].commands
        finally:
            await driver.stop()

    assert run(described_again) == ["Off"]
    assert len(google.actions) == 1


def test_switchable_is_for_plugs_and_changes_nothing_for_a_light_or_a_read_only_device(google, tmp_path):
    google.add_tv()
    bindings = {LIGHT: GoogleHomeBinding(LAMP, switchable=True),
                PLUG: GoogleHomeBinding("home-1/plug-1", read_only=True, switchable=True),
                TV: GoogleHomeBinding(TV_ID, switchable=True)}

    async def scenario():
        driver = make_driver(google, tmp_path, bindings=bindings)
        await driver.start()
        try:
            return {d.id: {name: c.commands for name, c in d.clusters.items()} for d in await driver.list_devices()}
        finally:
            await driver.stop()

    commands = run(scenario)
    assert commands[LAMP]["OnOff"] == ["On", "Off"]
    assert commands["home-1/plug-1"]["OnOff"] == []  # read only wins
    assert commands[TV_ID]["MediaPlayback"] == ["Play", "Pause"]


def test_details_show_every_trait_google_reports_and_none_of_its_ids(google, tmp_path):
    google.add_tv()
    google.states[LIGHT] = state_of(LIGHT, "GoogleLightDevice", on=True, level=157, mireds=370)
    google.resources[1]["resourceInfo"] = {"deviceInfo": {
        "vendorName": "TP-Link", "productName": "Tapo L535E", "softwareVersion": "1.1.0",
        "serialNumber": "private-serial", "uniqueId": "private-unique-id", "vendorId": "private-vendor-id"}}
    with TestClient(create_app([make_driver(google, tmp_path, bindings=TV_BINDINGS)], FAST)) as client:
        before = len(google.calls)
        response = client.get(f"/devices/{LAMP}/details")
        (section,) = response.json()["sections"]
        assert section["path"] == "google_home" and section["online"] is True and "problem" not in section
        assert [(r["group"], r["name"], r["value"]) for r in section["rows"]] == [
            ("Device", "Name in Google Home", "Living stand"),
            ("Device", "Kind in Google Home", "Google light device"),
            ("Device", "Vendor name", "TP-Link"),
            ("Device", "Product name", "Tapo L535E"),
            ("Device", "Software version", "1.1.0"),
            ("Device", "Online", "yes"),
            ("Color control", "Color mode", "Color temperature mireds"),
            ("Color control", "Color temperature mireds", "370"),
            ("Color control", "Current hue", "0"),
            ("Color control", "Current saturation", "0"),
            ("Level control", "Current level", "157"),
            ("On off", "On off", "yes"),
        ]
        # One read for every trait: no trait filter, and no action.
        (read,) = [args for name, args in google.calls[before:] if name == "list_home_states"]
        assert "filter" not in read and read["resourceSearch"] == {"resourceIds": [LIGHT]}
        assert google.actions == []

        tv = client.get(f"/devices/{TV_ID}/details")
        rows = {(r["group"], r["name"]): r["value"] for r in tv.json()["sections"][0]["rows"]}
        assert rows[("Media playback", "Current state")] == "Playing"
        assert rows[("Media activity state", "Activity state")] == "Active"  # not ACTIVITY_STATE_ACTIVE
        assert rows[("Device", "Product name")] == "Google TV Streamer"
        assert not any(group == "Connectivity" for group, _ in rows)
        for answer in (response, tv):
            assert "private" not in answer.text and "device@" not in answer.text


@pytest.mark.parametrize("applied, expected", [(True, "done"), (False, "failed")])
def test_an_unconfirmed_action_is_read_back_and_never_sent_twice(hub, applied, expected):
    """Google's answer to the action is lost or wrong. What the light then reports decides."""
    google, client = hub
    google.apply = applied
    wrong_answer = result({"homeActionResponses": [{"id": "other", "response": {}}]})
    google.action_result = TimeoutError() if applied else wrong_answer
    answer = send(client, LAMP, "On", "k-1").json()
    assert answer["status"] == expected
    assert answer["state_after"]["OnOff"]["attributes"] == {"OnOff": applied}
    if not applied:
        assert "Google Home did not confirm the action (InvalidResponse)" in answer["error"]
    assert same_answer(answer, send(client, LAMP, "On", "k-1").json())
    assert len(google.actions) == 1


def test_a_description_that_no_longer_allows_the_command_stops_it(hub):
    google, client = hub
    google.metadata[0]["traits"]["OnOff"]["commands"]["OnOff.On"]["sensitivityLevel"] = "SENSITIVITY_LEVEL_L2"
    answer = send(client, LAMP, "On", "k-1").json()  # the hub's list still says On is fine
    assert answer["status"] == "failed" and "does not allow a plain On or Off" in answer["error"]
    assert google.actions == []


@pytest.mark.parametrize("problem", ["unreachable", "gone", "no_action_tool", "unknown_power"])
def test_nothing_is_sent_when_the_check_before_sending_fails(hub, problem):
    google, client = hub
    if problem == "unreachable":
        google.failures["tools/list"] = [ConnectionError(SECRET)]
    elif problem == "gone":
        google.resources = [r for r in google.resources if r["id"] != LIGHT]
    elif problem == "no_action_tool":
        google.tools.remove("run_home_actions")
    else:
        google.states[LIGHT]["traits"]["OnOff"]["state"] = {}
    answer = send(client, LAMP, "On", "k-1").json()
    assert answer["status"] == "failed" and google.actions == []
    assert "private" not in answer["error"]
    assert {"unreachable": "could not be asked before the command was sent",
            "gone": "is no longer in Google Home",
            "no_action_tool": "does not offer its action tool",
            "unknown_power": "does not report this light's power"}[problem] in answer["error"]


def test_family_protection_stops_a_command_before_google_is_asked(google, tmp_path):
    window = tmp_path / "permission.json"
    settings = Settings(confirm_timeout_s=0.3, poll_interval_s=0.02, family_protection=True, protection_path=window)
    with TestClient(create_app([make_driver(google, tmp_path)], settings)) as client:
        before = len(google.calls)
        assert send(client, LAMP, "On", "k-1").status_code == 403
        assert len(google.calls) == before and google.opened == [False]
        protection.open_window(window, [LAMP], minutes=5)
        assert send(client, LAMP, "On", "k-1").json()["status"] == "done"
    assert len(google.actions) == 1


def test_an_answer_that_is_not_a_confirmation_is_kept_for_the_owner_and_out_of_messages(google, tmp_path, caplog):
    """Google answers an action with an error of its own. Its text stays on this computer, in a
    file only the owner can read; the hub's answer and its log carry the answer's shape at most."""
    kept = tmp_path / "settings" / driver_module.LAST_ACTION_FILE
    google.apply = False
    google.action_result = result({"homeActionResponses": [{"id": LIGHT, "error": {"code": 3, "message": SECRET}}]})
    driver = make_driver(google, tmp_path, diagnostics=kept)
    with caplog.at_level("WARNING"), TestClient(create_app([driver], FAST)) as client:
        answer = send(client, LAMP, "On", "k-1").json()
    assert answer["status"] == "failed" and "did not confirm the action (InvalidResponse)" in answer["error"]
    assert "private" not in json.dumps(answer) and "private" not in caplog.text
    assert '"error": {"code": "int", "message": "str"}' in caplog.text  # field names and types only
    saved = json.loads(kept.read_text())
    assert kept.stat().st_mode & 0o777 == 0o600
    assert (saved["device_id"], saved["type"], saved["command"]) == (LAMP, "GoogleLightDevice", "OnOff.On")
    assert saved["answer"]["structuredContent"]["homeActionResponses"][0]["error"]["message"] == SECRET

    # The answer to a confirmed action replaces it: what Google said when it accepted is what
    # the owner needs when the device then does not act.
    google.apply, google.action_result = True, None
    with TestClient(create_app([make_driver(google, tmp_path, diagnostics=kept)], FAST)) as client:
        assert send(client, LAMP, "On", "k-2").json()["status"] == "done"
    saved = json.loads(kept.read_text())
    assert saved["answer"]["structuredContent"] == {"homeActionResponses": [{"id": LIGHT, "response": {}}]}
    assert kept.stat().st_mode & 0o777 == 0o600

    # A driver that is given no place for it keeps nothing.
    kept.unlink()
    with TestClient(create_app([make_driver(google, tmp_path)], FAST)) as client:
        assert send(client, LAMP, "Off", "k-3").json()["status"] == "done"
    assert not kept.exists() and sorted(p.name for p in tmp_path.iterdir()) == ["settings"]


# A TV

@pytest.fixture
def tv_hub(google, tmp_path):
    google.add_tv()
    with TestClient(create_app([make_driver(google, tmp_path, bindings=TV_BINDINGS)], FAST)) as client:
        yield google, client


def test_a_tv_is_listed_with_what_it_plays_and_whether_it_is_in_use(tv_hub):
    google, client = tv_hub
    tv = next(d for d in client.get("/devices").json() if d["id"] == TV_ID)
    assert (tv["name"], tv["device_type"], tv["room"], tv["online"]) == (
        "Living Room TV", "CastingVideoPlayer", "Living Room", True)
    assert tv["clusters"] == {
        "MediaPlayback": {"attributes": {"CurrentState": "Playing"}, "commands": ["Play", "Pause"]},
        "PorchMediaExtension": {"attributes": {"ActivityState": "Active"}, "commands": []}}
    # One read covers the lights' and the TV's traits.
    asked = next(args for name, args in google.calls if name == "list_home_states")
    assert asked["filter"] == {"shortenedResourceTraits": inventory.STATE_TRAITS}
    assert {"OnOff", "MediaPlayback", "MediaActivityState", "LevelControl"} <= set(inventory.STATE_TRAITS)
    assert asked["resourceSearch"]["resourceIds"] == [LIGHT, PLUG, TV]
    assert "private" not in json.dumps(tv)


def test_pausing_and_playing_a_tv_is_checked_sent_once_and_confirmed(tv_hub):
    google, client = tv_hub
    paused = send(client, TV_ID, "Pause", "k-1").json()
    assert paused["status"] == "done" and paused["control_path"] == "google_home"
    assert paused["state_after"]["MediaPlayback"]["attributes"] == {"CurrentState": "Paused"}
    # The request names the part of the TV that Google describes the command for (a light's own
    # kind is that part: see the light's test), and the command as Google's description lists it.
    assert google.actions == [{"structureId": "private-home", "homeActionRequests": [
        {"id": TV, "type": "GoogleTvDevice", "command": "MediaPlayback.Pause", "parameters": {}}]}]

    assert same_answer(paused, send(client, TV_ID, "Pause", "k-1").json())  # the same key replays
    assert send(client, TV_ID, "Pause", "k-2").json()["status"] == "done"  # already paused: nothing to send
    assert len(google.actions) == 1
    played = send(client, TV_ID, "Play", "k-3").json()
    assert played["status"] == "done" and played["state_after"]["MediaPlayback"]["attributes"] == {
        "CurrentState": "Playing"}
    assert [a["homeActionRequests"][0]["command"] for a in google.actions] == [
        "MediaPlayback.Pause", "MediaPlayback.Play"]
    assert client.get(f"/devices/{TV_ID}/state").json()["PorchMediaExtension"]["attributes"] == {
        "ActivityState": "Active"}


def test_googles_success_is_not_the_tvs(tv_hub):
    """What a real TV did: Google answered Pause with success and the TV went on playing. The hub
    reads the TV back, says so, and does not send again."""
    google, client = tv_hub
    google.apply = False
    answer = send(client, TV_ID, "Pause", "k-1").json()
    assert answer["status"] == "failed" and "did not report MediaPlayback" in answer["error"]
    assert answer["state_after"]["MediaPlayback"]["attributes"] == {"CurrentState": "Playing"}
    assert same_answer(answer, send(client, TV_ID, "Pause", "k-1").json()) and len(google.actions) == 1


def test_a_tv_that_answers_no_is_a_refusal_with_no_waiting(tv_hub):
    """Google passes the command on and the TV answers with a status of its own that is not success."""
    google, client = tv_hub
    google.apply = False
    google.action_result = result({"homeActionResponses": [
        {"id": TV, "response": {"status": "STATUS_ENUM_NOT_ALLOWED"}}]})
    answer = send(client, TV_ID, "Pause", "k-1").json()
    assert answer["status"] == "failed"
    assert answer["error"] == "the device answered STATUS_ENUM_NOT_ALLOWED to Pause, so it was not carried out"
    assert answer["latency_ms"] < 250 and len(google.actions) == 1  # one read after it, not the full wait
    # A status that is not a plain constant is never passed on.
    google.action_result = result({"homeActionResponses": [{"id": TV, "response": {"status": SECRET}}]})
    hidden = send(client, TV_ID, "Pause", "k-2").json()
    assert hidden["error"] == "the device answered a status to Pause, so it was not carried out"


def test_a_tv_takes_only_play_and_pause_and_a_light_takes_neither(tv_hub):
    google, client = tv_hub
    before = len(google.calls)
    # Google describes On, Off, Stop and Seek for the TV too. The hub offers none of them: the TV
    # does not report whether it is on, so On and Off could not be confirmed.
    for command in ("On", "Off", "Toggle", "Stop", "Seek"):
        assert send(client, TV_ID, command, f"k-{command}").status_code == 422
    assert send(client, LAMP, "Pause", "k-lamp").status_code == 422
    dry_run = client.post(f"/devices/{TV_ID}/simulate", json={"command": "Pause"}).json()
    assert dry_run["would_succeed"] and dry_run["control_path"] == "google_home"
    assert dry_run["simulated_state_after"]["MediaPlayback"]["attributes"] == {"CurrentState": "Paused"}
    assert len(google.calls) == before and google.actions == []


@pytest.mark.parametrize("problem, reason", [
    ("nothing_playing", "nothing is playing on this TV, so there is nothing to pause"),
    ("not_reported", "does not report what this TV is playing"),
    ("another_part", "does not report what this TV is playing"),
    ("unknown_word", "does not report what this TV is playing"),
    ("offline", "is offline in Google Home"),
    ("description_changed", "does not allow a plain Play or Pause"),
])
def test_a_tv_is_sent_nothing_when_the_check_before_sending_fails(tv_hub, problem, reason):
    """The hub's list still says the TV is playing. What Google says right before sending decides."""
    google, client = tv_hub
    playback = google.states[TV]["traits"]["MediaPlayback"]
    if problem == "nothing_playing":
        playback["state"]["currentState"] = "PLAYBACK_STATE_ENUM_NOT_PLAYING"
    elif problem == "not_reported":
        playback["stateReportingOmitted"] = True
    elif problem == "another_part":
        playback["type"] = "SpeakerDevice"
    elif problem == "unknown_word":
        playback["state"]["currentState"] = "PLAYBACK_STATE_ENUM_SOMETHING_NEW"
    elif problem == "offline":
        google.states[TV] = tv_state(online=False)
    else:
        google.metadata[-1]["traits"]["GoogleTvDevice/MediaPlayback"]["commands"]["MediaPlayback.Pause"][
            "sensitivityLevel"] = "SENSITIVITY_LEVEL_L2"
    answer = send(client, TV_ID, "Pause", "k-1").json()
    assert answer["status"] == "failed" and reason in answer["error"] and google.actions == []
    assert "private" not in answer["error"]


def test_a_tv_nobody_is_using_reads_as_not_playing_and_is_sent_no_pause(google, tmp_path):
    """What a real TV reported once the show was stopped: nothing playing, and no activity at all."""
    google.add_tv(playback="NOT_PLAYING", activity=None)
    with TestClient(create_app([make_driver(google, tmp_path, bindings=TV_BINDINGS)], FAST)) as client:
        tv = next(d for d in client.get("/devices").json() if d["id"] == TV_ID)
        assert tv["online"] and tv["clusters"]["MediaPlayback"]["attributes"] == {"CurrentState": "NotPlaying"}
        assert tv["clusters"]["PorchMediaExtension"]["attributes"] == {"ActivityState": None}  # unknown, not "Inactive"
        assert tv["clusters"]["MediaPlayback"]["commands"] == ["Play", "Pause"]  # Google still describes them
        answer = send(client, TV_ID, "Pause", "k-1").json()
    assert answer["status"] == "failed"
    assert answer["error"] == "nothing is playing on this TV, so there is nothing to pause"
    # Google was asked on a session that could have sent, and nothing was sent.
    assert google.opened == [False, True] and google.actions == []


def test_a_tv_state_is_never_guessed(google, tmp_path):
    """Standby, nothing playing, an unknown word and an offline TV, each as Google reports it."""
    google.add_tv(playback="NOT_PLAYING", activity="STANDBY")

    async def scenario():
        driver = make_driver(google, tmp_path, bindings=TV_BINDINGS)
        await driver.start()
        try:
            standby = await driver.read_state(TV_ID)
            dry_run = None
            try:
                driver.preflight(TV_ID, "Play", {}, standby)
            except DriverError as exc:
                dry_run = str(exc)
            google.states[TV]["traits"]["MediaActivityState"]["state"]["activityState"] = "ACTIVITY_STATE_NEW"
            google.states[TV]["traits"]["MediaPlayback"]["state"].pop("currentState")
            unknown = await driver.read_state(TV_ID)
            google.states[TV] = tv_state(online=False)
            with pytest.raises(DeviceOffline):
                await driver.read_state(TV_ID)
            driver._listed_at = None  # ask Google again for the list
            listed = {d.id: d for d in await driver.list_devices()}[TV_ID]
            return standby, dry_run, unknown, listed
        finally:
            await driver.stop()

    standby, dry_run, unknown, listed = run(scenario)
    assert standby["MediaPlayback"].attributes == {"CurrentState": "NotPlaying"}
    assert standby["PorchMediaExtension"].attributes == {"ActivityState": "Standby"}
    assert dry_run == "nothing is playing on this TV, so there is nothing to play"
    assert unknown["MediaPlayback"].attributes == {"CurrentState": None}
    assert unknown["PorchMediaExtension"].attributes == {"ActivityState": None}
    assert not listed.online and listed.clusters["MediaPlayback"].attributes == {"CurrentState": None}


@pytest.mark.parametrize("change", ["level", "parameters", "trait_gone", "other_part", "no_description"])
def test_play_and_pause_are_offered_only_while_googles_description_allows_them(google, change):
    resource, metadata = tv_resource(), tv_metadata()
    trait = metadata["traits"]["GoogleTvDevice/MediaPlayback"]
    endpoint = inventory.Endpoint("private-home", resource, metadata, "Living Room")
    assert endpoint.commands() == ["Play", "Pause"] and endpoint.device_type == "CastingVideoPlayer"
    if change == "level":
        trait["commands"]["MediaPlayback.Play"]["sensitivityLevel"] = "SENSITIVITY_LEVEL_L2"
        expected = ["Pause"]
    elif change == "parameters":
        trait["commands"]["MediaPlayback.Pause"]["parameters"] = {"position": {"required": True}}
        expected = ["Play"]
    elif change == "trait_gone":
        resource["supportedTraits"].remove("GoogleTvDevice/MediaPlayback")
        expected = []
    elif change == "other_part":
        trait["resourceTypes"] = ["SpeakerDevice"]
        expected = []
    else:
        endpoint = inventory.Endpoint("private-home", resource, None, "Living Room")
        expected = []
    assert endpoint.commands() == expected
    # Play and Pause are never the light's, and On and Off never the TV's.
    assert endpoint.google_command("On") is None and endpoint.google_command("Pause") == "MediaPlayback.Pause"
    with pytest.raises(inventory.PreflightError):
        inventory.validate_command(resource, metadata, "OnOff.Off")
    with pytest.raises(inventory.PreflightError):
        inventory.validate_command(home_resources()[1], home_metadata()[0], "MediaPlayback.Pause")


def test_a_binding_can_make_a_device_read_only(google, tmp_path):
    """This home's settings say the TV is only to be read. Google still describes Play and Pause
    for it; the hub offers neither and sends nothing."""
    google.add_tv()
    bindings = {**BINDINGS, TV: GoogleHomeBinding(TV_ID, read_only=True)}
    driver = make_driver(google, tmp_path, bindings=bindings)
    with TestClient(create_app([driver], FAST)) as client:
        tv = next(d for d in client.get("/devices").json() if d["id"] == TV_ID)
        assert tv["clusters"]["MediaPlayback"] == {"attributes": {"CurrentState": "Playing"}, "commands": []}
        assert client.get(f"/devices/{TV_ID}/state").json()["MediaPlayback"]["commands"] == []
        assert send(client, TV_ID, "Pause", "k-1").status_code == 422  # the hub does not offer it
        assert next(row for row in driver.rows() if row.google_id == TV).commands == ()
        assert send(client, LAMP, "On", "k-2").json()["status"] == "done"  # the lamp is not affected
    assert [a["homeActionRequests"][0]["id"] for a in google.actions] == [LIGHT]

    async def asked_directly():
        direct = make_driver(google, tmp_path, bindings=bindings)
        await direct.start()
        try:
            before = len(google.calls)
            with pytest.raises(CommandRejected, match="is read only in this home's settings, so Pause was not sent"):
                await direct.send_command(TV_ID, "Pause", {})
            with pytest.raises(CommandRejected, match="is read only in this home's settings"):
                direct.preflight(TV_ID, "Pause", {}, {})
            return len(google.calls) - before
        finally:
            await direct.stop()

    assert run(asked_directly) == 0  # refused before Google is asked


def test_family_protection_stops_a_tv_command_before_google_is_asked(google, tmp_path):
    google.add_tv()
    window, pin_file = tmp_path / "permission.json", tmp_path / "owner-pin.json"
    protection.set_pin(pin_file, "482915")
    settings = Settings(confirm_timeout_s=0.3, poll_interval_s=0.02, family_protection=True, protection_path=window,
                        owner_pin_path=pin_file)

    def open_from_page(*devices):
        return client.post("/protection/window", json={"device_ids": list(devices), "pin": "482915"}).json()

    with TestClient(create_app([make_driver(google, tmp_path, bindings=TV_BINDINGS)], settings)) as client:
        before = len(google.calls)
        refused = send(client, TV_ID, "Pause", "k-1")
        assert refused.status_code == 403 and len(google.calls) == before and google.opened == [False]
        # A window names what its devices take: On and Off for the lamp, Play and Pause for the TV.
        assert open_from_page(LAMP)["commands"] == ["On", "Off"]
        assert send(client, TV_ID, "Pause", "k-1").status_code == 403  # the lamp's window does not cover the TV
        assert open_from_page(LAMP, TV_ID)["commands"] == ["On", "Off", "Play", "Pause"]
        assert open_from_page(TV_ID)["commands"] == ["Play", "Pause"]
        assert send(client, TV_ID, "Pause", "k-1").json()["status"] == "done"
        assert send(client, LAMP, "On", "k-2").status_code == 403
        # From Terminal, a window with no command named covers the TV's two as well.
        protection.open_window(window, [TV_ID], minutes=5)
        assert send(client, TV_ID, "Play", "k-3").json()["status"] == "done"
    assert [a["homeActionRequests"][0]["command"] for a in google.actions] == [
        "MediaPlayback.Pause", "MediaPlayback.Play"]


# The kept session

def test_a_caller_that_gives_up_does_not_cut_a_started_call(google):
    async def scenario():
        kept = KeptSession(lambda actions: google.session(None, auth_timeout=1, allow_actions=actions), idle_s=5)
        started, release, finished = asyncio.Event(), asyncio.Event(), []

        async def slow(client):
            started.set()
            await release.wait()
            finished.append("slow")

        async def quick(client):
            finished.append("quick")
            return "ok"

        caller = asyncio.create_task(kept.call(slow, actions=True))
        await started.wait()
        caller.cancel()
        dropped = asyncio.create_task(kept.call(quick))
        await asyncio.sleep(0)
        dropped.cancel()  # gave up before it started: it never runs
        release.set()
        answer = await kept.call(quick)
        await kept.close()
        return answer, finished, list(google.opened), google.closed

    answer, finished, opened, closed = run(scenario)
    assert answer == "ok" and finished == ["slow", "quick"]
    assert opened == [True] and closed == 1  # reads reuse the session that can send


def test_a_session_that_cannot_open_fails_the_call_and_the_next_call_tries_again(google):
    attempts = []

    @asynccontextmanager
    async def flaky(actions):
        attempts.append(actions)
        if len(attempts) == 1:
            raise session.SetupIncomplete("Google Home is not set up: google-home-client.json is missing")
        async with google.session(None, auth_timeout=1, allow_actions=actions) as client:
            yield client

    async def scenario():
        kept = KeptSession(flaky, idle_s=5)

        async def read(client):
            return "read"

        with pytest.raises(session.SetupIncomplete):
            await kept.call(read)
        answer = await kept.call(read)
        await kept.close()
        return answer

    assert run(scenario) == "read" and attempts == [False, False]


# Reading the home (inventory.py)

class Pages(FakeGoogle):
    """Two pages of tools and of resources."""

    async def list_tools(self, cursor):
        self.calls.append(("tools/list", cursor))
        if cursor is None:
            return {"tools": [{"name": "list_homes"}], "nextCursor": "tools-page-2"}
        return {"tools": [{"name": "list_home_resources"}, {"name": "run_home_actions"}]}

    async def call_tool(self, name, args):
        if name == "list_home_resources":
            self.calls.append((name, deepcopy(args)))
            page = {"resources": self.resources[2:]} if args.get("pageToken") else {
                "resources": self.resources[:2], "resourceMetadata": self.metadata, "nextPageToken": "next"}
            return result(page)
        return await super().call_tool(name, args)


def test_inventory_reads_every_page_and_only_with_read_tools():
    google = Pages()
    report = run(lambda: inventory.read_inventory(google))
    assert report["status"] == "inventory_read" and report["homes"][0]["inventory_complete"] is True
    assert google.names() == ["tools/list", "tools/list", "list_homes", "list_home_resources", "list_home_resources"]
    assert google.calls[-1][1] == {"structureId": "private-home", "view": "VIEW_FULL", "pageSize": 100,
                                   "pageToken": "next"}
    assert set(inventory.inventory_endpoints(report)) == {LIGHT, PLUG, DISPLAY}


@pytest.mark.parametrize("page", [
    {"message": "permission failure, secret-token"},
    {"resources": None},
    {"resources": [{"displayName": "Missing identity"}]},
    {"resources": [], "nextPageToken": 0},
])
def test_a_malformed_page_never_passes_as_a_complete_home(google, page):
    google.failures["list_home_resources"] = result(page)
    report = run(lambda: inventory.read_inventory(google))
    assert report["status"] == "partial" and report["homes"][0]["inventory_complete"] is False
    assert "secret-token" not in json.dumps(report)
    with pytest.raises(inventory.InvalidResponse):
        inventory.inventory_endpoints(report)


def test_a_repeated_page_token_stops_instead_of_looping(google):
    google.failures["list_home_resources"] = result({"resources": [], "nextPageToken": "again"})
    report = run(lambda: inventory.read_inventory(google))
    assert report["homes"][0]["error"]["error_type"] == "PaginationError"
    assert google.names().count("list_home_resources") == 2


def test_googles_error_is_kept_for_the_owner_and_out_of_the_summary(google):
    google.failures["list_homes"] = {"isError": True, "content": [{"type": "text", "text": SECRET}]}
    report = run(lambda: inventory.read_inventory(google))
    assert (report["status"], report["failed_operation"]) == ("connection_failed", "list_homes")
    assert report["private_diagnostics"][0]["response"]["content"][0]["text"] == SECRET
    assert "private" not in "\n".join(login.summary_lines(report))
    assert "private" not in inventory.inventory_failure(report)


def test_empty_answers_have_their_own_statuses(google):
    google.failures["list_homes"] = result({"structures": []})
    assert run(lambda: inventory.read_inventory(google))["status"] == "no_homes"
    google.failures.clear()
    google.resources, google.metadata = [], []
    report = run(lambda: inventory.read_inventory(google))
    assert report["status"] == "empty_inventory" and inventory.inventory_endpoints(report) == {}


def test_only_the_sign_in_gets_the_long_wait(google):
    async def slow_first_listing(cursor):
        await asyncio.sleep(0.05)
        return {"tools": [{"name": name} for name in google.tools]}

    google.list_tools = slow_first_listing
    signed_in = run(lambda: inventory.read_inventory(google, request_timeout=0.01, authorization_timeout=1))
    assert signed_in["status"] == "inventory_read"
    timed_out = run(lambda: inventory.read_inventory(google, request_timeout=1, authorization_timeout=0.01))
    assert (timed_out["status"], timed_out["error_type"]) == ("connection_failed", "TimeoutError")


def test_a_missing_read_tool_never_falls_back_to_an_action(google):
    google.tools = ["run_home_actions"]
    report = run(lambda: inventory.read_inventory(google))
    assert (report["status"], report["error_type"]) == ("connection_failed", "MissingReadTool")
    assert google.names() == ["tools/list"]
    with pytest.raises(ValueError):
        run(lambda: inventory.read_tool(google, "run_home_actions", {}, 1))


def test_an_error_flag_wins_over_plausible_data():
    with pytest.raises(inventory.RemoteToolError):
        inventory.tool_payload({"isError": True, "structuredContent": {"structures": []}})
    with pytest.raises(inventory.InvalidResponse):
        inventory.tool_payload({"content": [{"type": "text", "text": "not JSON secret-token"}]})


# The session's files and the bridge (session.py)

def client_file(directory, mode=0o600):
    path = directory / session.CLIENT_FILE
    path.write_text(json.dumps({"client_id": "example.apps.googleusercontent.com", "client_secret": "GOCSPX-test"}))
    path.chmod(mode)
    return path


def test_the_client_file_must_be_private_and_real(tmp_path):
    path = client_file(tmp_path)
    session.validate_credentials(path)
    path.chmod(0o644)
    with pytest.raises(PermissionError):
        session.validate_credentials(path)
    path.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(PermissionError):
        session.validate_credentials(link)
    path.write_text(json.dumps({"client_id": "someone-else", "client_secret": "x"}))
    with pytest.raises(ValueError, match="does not hold a Google OAuth client_id"):
        session.validate_credentials(path)


def test_setup_problems_name_what_is_missing_without_contacting_anyone(tmp_path, monkeypatch):
    monkeypatch.setattr(session.shutil, "which", lambda name: None)
    problems = session.setup_problems(tmp_path)
    assert problems[0] == f"google-home-client.json is missing from {tmp_path}"
    assert any("Node.js (npx) is not installed" in p for p in problems)
    client_file(tmp_path, mode=0o644)
    assert "only you can read" in session.setup_problems(tmp_path)[0]
    monkeypatch.setattr(session.shutil, "which", lambda name: "/usr/bin/npx")
    client_file(tmp_path)
    assert session.setup_problems(tmp_path) == []


def test_the_bridge_gets_paths_loopback_and_none_of_the_hubs_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("LG_THINQ_PAT", "do-not-inherit-this")
    monkeypatch.setenv("NODE_TLS_REJECT_UNAUTHORIZED", "0")
    args = session.bridge_arguments(tmp_path, 600)
    env = session.bridge_environment(tmp_path)
    assert session.HOME_MCP_URL in args and session.BRIDGE_PACKAGE in args and "--debug" not in args
    assert args[args.index("--static-oauth-client-info") + 1] == "@" + str(tmp_path / "google-home-client.json")
    assert args[args.index("--host") + 1] == args[args.index("--bind-host") + 1] == "127.0.0.1"
    ignored = [args[i + 1] for i, arg in enumerate(args) if arg == "--ignore-tool"]
    assert ignored == ["save_automation", "run_home_actions"]
    assert [a for a in session.bridge_arguments(tmp_path, 600, allow_actions=True) if a == "run_home_actions"] == []
    assert "save_automation" in session.bridge_arguments(tmp_path, 600, allow_actions=True)
    assert "LG_THINQ_PAT" not in env and env["NODE_TLS_REJECT_UNAUTHORIZED"] == "1"
    assert env["MCP_REMOTE_CONFIG_DIR"] == str(tmp_path / "google-home-auth")


def test_a_session_calls_only_the_tools_it_is_meant_to():
    called = []

    class Sdk:
        async def call_tool(self, name, arguments):
            called.append(name)
            return SimpleNamespace(model_dump=lambda **_: {"structuredContent": {}})

    async def scenario():
        reader, sender = session.SessionAdapter(Sdk()), session.SessionAdapter(Sdk(), allow_actions=True)
        for adapter, tool in ((reader, "run_home_actions"), (reader, "save_automation"), (sender, "save_automation")):
            with pytest.raises(ValueError):
                await adapter.call_tool(tool, {})
        await reader.call_tool("list_home_states", {})
        await sender.call_tool("run_home_actions", {})

    run(scenario)
    assert called == ["list_home_states", "run_home_actions"]


def test_a_driver_with_the_real_session_checks_setup_before_starting(tmp_path):
    async def scenario():
        with pytest.raises(session.SetupIncomplete, match="google-home-client.json is missing"):
            await GoogleHomeMcpDriver(tmp_path).start()

    run(scenario)
    assert list(tmp_path.iterdir()) == []  # nothing was created, nobody was contacted


def test_driver_from_env_reads_the_folder_and_bindings(tmp_path):
    driver = google_home_driver_from_env({
        "GOOGLE_HOME_DIRECTORY": str(tmp_path), "PORCH_HOME_ID": "home-9",
        "PORCH_CONFIG_DIR": str(tmp_path / "settings"),
        "GOOGLE_HOME_BINDINGS": json.dumps({LIGHT: {"porch_id": "home-9/lamp"},
                                            PLUG: {"porch_id": "home-9/plug", "room": "Hall", "switchable": True},
                                            TV: {"porch_id": "home-9/tv", "read_only": True},
                                            DISPLAY: {"porch_id": "home-9/display", "switchable": "yes"}}),
    })
    assert driver._bindings == {LIGHT: GoogleHomeBinding("home-9/lamp"),
                                PLUG: GoogleHomeBinding("home-9/plug", "Hall", switchable=True),
                                TV: GoogleHomeBinding("home-9/tv", read_only=True),
                                DISPLAY: GoogleHomeBinding("home-9/display")}  # only true itself says yes
    assert driver._home_id == "home-9" and driver._directory == tmp_path
    # An unconfirmed action's answer goes to the hub's settings folder, not the sign-in folder.
    assert driver._diagnostics == tmp_path / "settings" / "google-home-last-action.json"


# The sign-in command (login.py)

def test_login_checks_setup_without_signing_in(tmp_path, monkeypatch, capsys):
    async def never(*_):
        raise AssertionError("no sign-in during a setup check")

    monkeypatch.setattr(login, "check", never)
    assert login.main(["--directory", str(tmp_path), "--check-setup"]) == 1
    assert "Not ready: google-home-client.json is missing" in capsys.readouterr().out
    client_file(tmp_path)
    monkeypatch.setattr(session.shutil, "which", lambda name: "/usr/bin/npx")
    assert login.main(["--directory", str(tmp_path), "--check-setup"]) == 0
    assert "Ready to sign in. Nothing was contacted." in capsys.readouterr().out


def test_login_lists_the_home_by_name_and_keeps_googles_answer_private(tmp_path, monkeypatch, capsys, google):
    client_file(tmp_path)
    monkeypatch.setattr(session.shutil, "which", lambda name: "/usr/bin/npx")

    async def check(directory, auth_timeout):
        return await inventory.read_inventory(google)

    monkeypatch.setattr(login, "check", check)
    assert login.main(["--directory", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Google Home answered: 1 home, 3 devices." in out
    assert "Living stand" in out and "OnOffLight: On, Off" in out and "OnOffPlug: read only" in out
    # Google's ids and error text all carry "private-" in this stand-in.
    assert "not used: the hub has no mapping for this kind yet" in out and "private-" not in out
    saved = tmp_path / login.LAST_CHECK_FILE
    assert saved.stat().st_mode & 0o777 == 0o600 and LIGHT in saved.read_text()

    google.failures["list_homes"] = {"isError": True, "content": [{"type": "text", "text": SECRET}]}
    assert login.main(["--directory", str(tmp_path)]) == 2
    out = capsys.readouterr().out
    assert "could not be read (RemoteToolError in list_homes)" in out and "private-" not in out
    assert SECRET in saved.read_text()  # for the owner to read on this computer

    async def broken(directory, auth_timeout):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(login, "check", broken)
    assert login.main(["--directory", str(tmp_path)]) == 2
    assert "private-" not in capsys.readouterr().out and "private-" not in saved.read_text()


# Through the real MCP SDK

def test_the_real_mcp_session_lists_reads_and_switches_a_light(tmp_path, monkeypatch):
    """Everything above uses an in-memory stand-in. This goes through the MCP SDK the hub really
    uses: a stand-in server runs as the sign-in bridge would, as a child process over stdio."""
    pytest.importorskip("mcp")
    import os
    import sys
    import time
    from pathlib import Path

    directory = tmp_path / "google-home"
    directory.mkdir(mode=0o700)
    client_file(directory)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"on": {LIGHT: False, PLUG: True}, "calls": []}))
    server = Path(__file__).with_name("fake_google_home_server.py")
    bridge_arguments = session.bridge_arguments
    monkeypatch.setattr(session.shutil, "which", lambda name: sys.executable)
    monkeypatch.setattr(session, "bridge_arguments",
                        lambda folder, auth_timeout, allow_actions=False:
                        [str(server), str(state), *bridge_arguments(folder, auth_timeout, allow_actions=allow_actions)])

    driver = GoogleHomeMcpDriver(directory, bindings=dict(BINDINGS))  # the real session, not a stand-in
    driver.read_back_interval_s, driver.read_back_timeout_s = 0.05, 5.0
    with TestClient(create_app([driver], FAST)) as client:
        devices = {d["id"]: d for d in client.get("/devices").json()}
        assert set(devices) == {LAMP, "home-1/plug-1"}
        assert devices[LAMP]["clusters"]["OnOff"] == {"attributes": {"OnOff": False}, "commands": ["On", "Off"]}
        assert devices["home-1/plug-1"]["clusters"]["OnOff"] == {"attributes": {"OnOff": True}, "commands": []}
        done = send(client, LAMP, "On", "k-1").json()
        assert done["status"] == "done" and done["state_after"]["OnOff"]["attributes"] == {"OnOff": True}
        assert client.get(f"/devices/{LAMP}/state").json()["OnOff"]["attributes"] == {"OnOff": True}

    recorded = json.loads(state.read_text())
    assert recorded["on"][LIGHT] is True
    calls = recorded["calls"]
    assert [c["tool"] for c in calls].count("run_home_actions") == 1
    # The first session could only read: the bridge was told to hide the action tool. The one
    # that sent the action was a second process, opened for it. Neither offered automations.
    readers = {c["pid"] for c in calls if "run_home_actions" in c["ignored"]}
    senders = {c["pid"] for c in calls if "run_home_actions" not in c["ignored"]}
    assert len(readers) == 1 and len(senders) == 1 and readers != senders
    assert next(c["pid"] for c in calls if c["tool"] == "run_home_actions") in senders
    assert all("save_automation" in c["ignored"] for c in calls)
    # The private files a session leaves behind, and no process left running.
    assert (directory / session.LOG_FILE).stat().st_mode & 0o777 == 0o600
    assert (directory / session.CACHE_DIR).stat().st_mode & 0o777 == 0o700
    assert json.loads((directory / session.METADATA_FILE).read_text())["scope"] == session.HOME_SCOPE
    deadline = time.monotonic() + 5
    alive = readers | senders
    while alive and time.monotonic() < deadline:
        for pid in list(alive):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive.discard(pid)
        time.sleep(0.05)
    assert not alive


def test_default_drivers_use_the_live_google_home_and_ring_drivers_when_set(tmp_path):
    from porch_hub.drivers import RingDriver
    from porch_hub.main import default_drivers

    drivers = default_drivers(real_devices_only=True, env={
        "GOOGLE_HOME_DIRECTORY": str(tmp_path / "google-home"),
        "RING_AUTHORIZATION_FILE": str(tmp_path / "ring-authorization.json")})
    assert [type(d) for d in drivers] == [GoogleHomeMcpDriver, RingDriver]  # nothing simulated, nobody contacted
    mixed = default_drivers(env={"GOOGLE_HOME_DIRECTORY": str(tmp_path / "google-home")})
    assert [d.control_path for d in mixed] == ["matter", "lg_thinq", "google_home"]
    assert isinstance(mixed[2], GoogleHomeMcpDriver)


def test_a_light_on_matter_and_google_home_is_one_device_and_google_takes_over(google, tmp_path):
    """The same Porch id in both bindings makes one device: Matter while it is up, Google Home when it is not."""
    from porch_hub import clusters as c
    from porch_hub.drivers import SimulatedMatterDriver
    from porch_hub.models import ControlPath, Device, DeviceType, utcnow

    matter = SimulatedMatterDriver([Device(
        id=LAMP, name="Living stand", room="Living Room", device_type=DeviceType.ON_OFF_LIGHT, online=True,
        primary_control_path=ControlPath.MATTER, clusters={c.ON_OFF: c.on_off(False)}, last_updated_at=utcnow())])
    with TestClient(create_app([matter, make_driver(google, tmp_path)], FAST)) as client:
        lamp = next(d for d in client.get("/devices").json() if d["id"] == LAMP)
        assert lamp["control_paths"] == ["matter", "google_home"] and lamp["primary_control_path"] == "matter"
        assert send(client, LAMP, "On", "k-1").json()["control_path"] == "matter"
        assert google.actions == []

        matter.set_online(LAMP, False)
        taken_over = send(client, LAMP, "On", "k-2").json()
        assert taken_over["status"] == "done" and taken_over["control_path"] == "google_home"
        assert len(google.actions) == 1
