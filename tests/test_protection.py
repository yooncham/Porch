"""Family protection: real devices take commands only inside a window the owner opens."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from porch_hub import protection as p
from porch_hub.main import Settings, create_app
from porch_hub.seed import demo_home

LAMP = "home-1/desk-lamp-plug"
HEATER = "home-1/space-heater-plug"


def settings(path, **more) -> Settings:
    return Settings(confirm_timeout_s=0.3, poll_interval_s=0.02, family_protection=True,
                    protection_path=path, **more)


@pytest.fixture
def window(tmp_path):
    return tmp_path / "settings" / "permission.json"


@pytest.fixture
def home():
    return demo_home(simulate_latency=False)


@pytest.fixture
def client(home, window):
    with TestClient(create_app(home.drivers, settings(window))) as test_client:
        yield test_client


def send(client, device_id, name, key):
    return client.post(f"/devices/{device_id}/commands",
                       json={"command": name, "params": {}, "idempotency_key": key})


def lamp_is_on(client) -> bool:
    return client.get(f"/devices/{LAMP}/state").json()["OnOff"]["attributes"]["OnOff"]


# The window itself

def test_no_window_means_protected(window):
    guard = p.FamilyProtection(window)
    reason = guard.refusal(LAMP, "On")
    assert reason.startswith("Family protection is on: no control window has been opened.")
    assert f"deploy/home/owner.sh allow --device {LAMP}" in reason
    assert guard.summary() == {"enforced": True, "live_writes_enabled": False,
                               "reason": "Family protection is on: no control window has been opened.",
                               "expires_at": None, "device_ids": [], "commands": [],
                               "pin_set": False, "max_minutes": 30,
                               "set_pin_command": "deploy/home/owner.sh set-pin",
                               "always_allowed": {"device_ids": [], "commands": []},
                               "always_command": "deploy/home/owner.sh always", "approved_once": []}


def test_a_window_allows_only_its_devices_and_commands(window):
    p.open_window(window, [LAMP], minutes=5)
    guard = p.FamilyProtection(window)
    assert guard.refusal(LAMP, "On") is None and guard.refusal(LAMP, "Off") is None
    assert "is not in the open control window" in guard.refusal(HEATER, "On")
    assert guard.refusal(LAMP, "Toggle").startswith("Family protection: Toggle is not in the open control window")
    assert window.stat().st_mode & 0o777 == 0o600
    state = guard.summary()
    # With no command named, a window covers switching, and playing or pausing a TV.
    assert state["live_writes_enabled"] and state["device_ids"] == [LAMP]
    assert state["commands"] == ["On", "Off", "Play", "Pause"]
    p.open_window(window, [LAMP], minutes=5, commands=["Off"])
    assert guard.refusal(LAMP, "On").startswith("Family protection: On is not in the open control window (Off)")


def test_a_window_closes_by_itself_and_on_request(window):
    p.open_window(window, [LAMP], minutes=1)
    expired = p.Window(family_protection=False, device_ids=[LAMP],
                       expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    window.write_text(expired.model_dump_json())
    guard = p.FamilyProtection(window)
    assert "the control window closed at" in guard.refusal(LAMP, "On")
    assert guard.summary()["device_ids"] == []  # a closed window names nothing as allowed

    p.open_window(window, [LAMP], minutes=5)
    assert guard.refusal(LAMP, "On") is None
    p.close_window(window)
    assert guard.refusal(LAMP, "On").startswith("Family protection is on.")


@pytest.mark.parametrize("devices, minutes, commands", [
    (["*"], 5, None), ([""], 5, None), ([], 5, None), (["home-1/*"], 5, None),
    ([LAMP], 0, None), ([LAMP], 31, None), ([LAMP], 5, ["*"]), ([LAMP], 5, [" "]),
])
def test_a_window_needs_exact_devices_commands_and_a_short_time(window, devices, minutes, commands):
    with pytest.raises(ValueError):
        p.open_window(window, devices, minutes, commands)
    assert not window.exists()


def test_a_window_file_others_could_write_or_swap_is_not_trusted(window, tmp_path):
    p.open_window(window, [LAMP], minutes=5)
    guard = p.FamilyProtection(window)
    window.chmod(0o666)
    assert guard.refusal(LAMP, "On").startswith("Family protection is on: the control window file cannot be used.")
    window.chmod(0o600)
    assert guard.refusal(LAMP, "On") is None

    real = tmp_path / "elsewhere.json"
    window.rename(real)
    window.symlink_to(real)
    assert "cannot be used" in guard.refusal(LAMP, "On")
    with pytest.raises(ValueError, match="symlink"):
        p.open_window(window, [LAMP], minutes=5)

    window.unlink()
    window.write_text("{not json")
    assert "cannot be used" in guard.refusal(LAMP, "On")


# In the hub

def test_protected_hub_sends_nothing_and_says_why(client, window):
    blocked = send(client, LAMP, "On", "k-1")
    assert blocked.status_code == 403
    assert "Family protection is on" in blocked.json()["detail"]
    assert lamp_is_on(client) is False
    assert client.get("/protection").json()["live_writes_enabled"] is False

    dry_run = client.post(f"/devices/{LAMP}/simulate", json={"command": "On"}).json()
    assert dry_run["would_succeed"] is False and "Family protection is on" in dry_run["reason"]
    assert dry_run["simulated_state_after"]["OnOff"]["attributes"]["OnOff"] is False


def test_the_same_request_runs_once_the_owner_opens_a_window(client, window):
    assert send(client, LAMP, "On", "k-1").status_code == 403
    p.open_window(window, [LAMP], minutes=5)
    assert client.post(f"/devices/{LAMP}/simulate", json={"command": "On"}).json()["would_succeed"] is True
    done = send(client, LAMP, "On", "k-1")  # the refusal was not kept under the key
    assert done.status_code == 200 and done.json()["status"] == "done"
    assert lamp_is_on(client) is True
    state = client.get("/protection").json()
    assert state["enforced"] and state["live_writes_enabled"] and state["device_ids"] == [LAMP]

    # Another device, and another command on the same device, stay protected.
    assert send(client, HEATER, "On", "k-2").status_code == 403
    assert send(client, LAMP, "Toggle", "k-3").status_code == 403
    assert lamp_is_on(client) is True


def test_a_window_that_closes_before_the_send_stops_the_command(home, window):
    """The check runs again right before sending, after the read that can take seconds on a cloud path."""
    p.open_window(window, [LAMP], minutes=5)
    sent = []
    read_state, send_command = home.matter.read_state, home.matter.send_command

    async def read_then_close(device_id):
        state = await read_state(device_id)
        p.close_window(window)
        return state

    async def record_send(device_id, command, params):
        sent.append(command)
        await send_command(device_id, command, params)

    home.matter.read_state, home.matter.send_command = read_then_close, record_send
    with TestClient(create_app(home.drivers, settings(window))) as client:
        assert send(client, LAMP, "On", "k-1").status_code == 403
    assert sent == []


def test_offline_copy_still_answers_while_protected(home, window):
    with TestClient(create_app(home.drivers, settings(window, demo_offline_mode=True))) as client:
        answer = send(client, LAMP, "On", "k-1").json()
        assert answer["status"] == "done" and answer["simulated"] is True
        assert client.post(f"/devices/{LAMP}/simulate", json={"command": "Off"}).json()["would_succeed"] is True


def test_unprotected_hub_reports_that_nothing_is_enforced(home):
    with TestClient(create_app(home.drivers, Settings(confirm_timeout_s=0.3, poll_interval_s=0.02))) as client:
        assert client.get("/protection").json() == p.NOT_ENFORCED
        assert send(client, LAMP, "On", "k-1").json()["status"] == "done"


@pytest.mark.parametrize("real_only, explicit, expected", [
    (None, None, False), ("1", None, True), ("1", "0", False), (None, "1", True), ("1", "", True),
])
def test_protection_is_on_by_default_for_a_real_devices_hub(monkeypatch, real_only, explicit, expected):
    for name, value in (("PORCH_REAL_DEVICES_ONLY", real_only), ("PORCH_FAMILY_PROTECTION", explicit)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert Settings().family_protection is expected


def test_the_window_file_follows_the_settings_folder(monkeypatch, tmp_path):
    monkeypatch.delenv("PORCH_PROTECTION_FILE", raising=False)
    monkeypatch.setenv("PORCH_CONFIG_DIR", str(tmp_path))
    assert Settings().protection_path == tmp_path / "permission.json"
    monkeypatch.setenv("PORCH_PROTECTION_FILE", str(tmp_path / "elsewhere.json"))
    assert Settings().protection_path == tmp_path / "elsewhere.json"


# The owner's command

def test_owner_command_opens_reports_and_closes(window, capsys):
    assert p.main(["--file", str(window), "status"]) == 0
    assert "No real command will be sent." in capsys.readouterr().out

    assert p.main(["--file", str(window), "allow", "--device", LAMP, "--minutes", "3"]) == 0
    out = capsys.readouterr().out
    assert "Control window open until" in out and LAMP in out and "Commands: On, Off" in out
    assert p.FamilyProtection(window).refusal(LAMP, "On") is None

    assert p.main(["--file", str(window), "protect"]) == 0
    assert "Family protection is on." in capsys.readouterr().out
    assert p.FamilyProtection(window).refusal(LAMP, "On") is not None

    assert p.main(["--file", str(window), "allow", "--device", "*"]) == 1
    assert "Stopped:" in capsys.readouterr().err


def test_hints_carry_the_scripts_full_path_when_the_scripts_give_it(window, monkeypatch, capsys):
    """deploy/home/owner.sh only resolves from the repository's folder; a pasted hint must work anywhere."""
    monkeypatch.setenv("PORCH_OWNER_COMMAND", "/somewhere/deploy/home/owner.sh")
    refusal = p.FamilyProtection(window).refusal(LAMP, "On")
    assert f"with: /somewhere/deploy/home/owner.sh allow --device {LAMP}" in refusal
    # The pages' panel shows the same full path for choosing the PIN.
    assert p.FamilyProtection(window).summary()["set_pin_command"] == "/somewhere/deploy/home/owner.sh set-pin"
    assert p.main(["--file", str(window), "allow", "--device", LAMP]) == 0
    assert "Close it early with: /somewhere/deploy/home/owner.sh protect" in capsys.readouterr().out


def test_the_pages_polls_stay_out_of_the_access_log():
    import logging

    from porch_hub.main import QuietPolls

    def access(line: str) -> logging.LogRecord:
        method, path = line.split(" ")
        return logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                                 ("127.0.0.1:5000", method, path, "1.1", 200), None)

    quiet = QuietPolls()
    assert quiet.filter(access("GET /protection")) is False
    assert quiet.filter(access("GET /devices")) is True
    assert quiet.filter(access("POST /devices/home-1/lamp/commands")) is True


# The pages' button: opening needs the owner's PIN, closing needs nothing

PIN = "482915"


@pytest.fixture
def pinned(home, window, tmp_path, monkeypatch):
    pin_file = tmp_path / "settings" / "owner-pin.json"
    p.set_pin(pin_file, PIN)
    monkeypatch.setattr(p, "PIN_LOCK_S", 0.2)
    app = create_app(home.drivers, settings(window, owner_pin_path=pin_file))
    with TestClient(app) as test_client:
        yield test_client, pin_file


def open_from_page(client, pin, devices=(LAMP,), **more):
    return client.post("/protection/window", json={"device_ids": list(devices), "pin": pin, **more})


def test_the_pin_is_kept_only_as_a_salted_hash(tmp_path):
    pin_file = tmp_path / "owner-pin.json"
    p.set_pin(pin_file, PIN)
    saved = pin_file.read_text()
    assert PIN not in saved and pin_file.stat().st_mode & 0o777 == 0o600
    first = json.loads(saved)["hash"]
    p.set_pin(pin_file, PIN)
    assert json.loads(pin_file.read_text())["hash"] != first  # a new salt each time
    owner = p.OwnerPin(pin_file)
    owner.verify(PIN)
    with pytest.raises(p.WrongPin):
        owner.verify("000000")
    for short in ("12345", " 123456", ""):
        with pytest.raises(ValueError):
            p.set_pin(pin_file, short)
    pin_file.chmod(0o644)  # readable by others: as good as not set
    assert owner.is_set() is False


def test_the_owner_opens_a_window_from_a_page_and_anyone_can_close_it(pinned, window):
    client, _ = pinned
    assert client.get("/protection").json()["pin_set"] is True
    assert send(client, LAMP, "On", "k-1").status_code == 403

    with client.websocket_connect("/ws/events") as ws:
        opened = open_from_page(client, PIN, minutes=3)
        while (event := ws.receive_json())["type"] != "protection_changed":
            pass
    assert opened.status_code == 200 and opened.json()["live_writes_enabled"] is True
    assert opened.json()["device_ids"] == [LAMP] and PIN not in opened.text
    # The window covers what its devices take of On, Off, Play and Pause: for a plug, On and Off.
    assert opened.json()["commands"] == ["On", "Off"]
    assert event["data"]["live_writes_enabled"] is True  # every open page hears at once
    assert send(client, LAMP, "On", "k-1").json()["status"] == "done"

    closed = client.post("/protection/close")  # no PIN: putting protection back on is always allowed
    assert closed.status_code == 200 and closed.json()["live_writes_enabled"] is False
    assert send(client, LAMP, "Off", "k-2").status_code == 403


def test_a_page_cannot_open_a_window_without_the_right_pin(pinned, window):
    client, pin_file = pinned
    assert open_from_page(client, "000000").status_code == 401
    assert not window.exists() and send(client, LAMP, "On", "k-1").status_code == 403
    assert open_from_page(client, PIN, devices=["home-1/no-such-device"]).status_code == 404
    assert open_from_page(client, PIN, devices=["*"]).status_code in (404, 422)
    assert open_from_page(client, PIN, minutes=31).status_code == 422
    assert client.post("/protection/window", json={"device_ids": [LAMP]}).status_code == 422  # no PIN at all

    # Wrong guesses in a row lock the button, even for the right PIN, and then it works again.
    # (The right PIN above started the count again.)
    for _ in range(p.PIN_TRIES - 1):
        assert open_from_page(client, "111111").status_code == 401
    locked = open_from_page(client, "222222")
    assert locked.status_code == 429 and "Too many wrong PINs" in locked.json()["detail"]
    assert open_from_page(client, PIN).status_code == 429
    assert not window.exists()
    import time
    time.sleep(0.3)
    assert open_from_page(client, PIN).status_code == 200

    pin_file.unlink()
    refused = open_from_page(client, PIN)
    assert refused.status_code == 403 and "set-pin" in refused.json()["detail"]


def test_a_hub_without_family_protection_has_no_window_to_open(home):
    with TestClient(create_app(home.drivers, Settings(confirm_timeout_s=0.3, poll_interval_s=0.02))) as client:
        assert open_from_page(client, PIN).status_code == 409
        assert client.post("/protection/close").status_code == 409


def test_set_pin_command_asks_twice_and_never_prints_it(tmp_path, monkeypatch, capsys):
    pin_file = tmp_path / "owner-pin.json"
    typed = iter([PIN, PIN])
    monkeypatch.setattr(p.getpass, "getpass", lambda prompt: next(typed))
    assert p.main(["set-pin", "--pin-file", str(pin_file)]) == 0
    assert p.OwnerPin(pin_file).is_set() and PIN not in capsys.readouterr().out

    typed = iter([PIN, "482916"])
    assert p.main(["set-pin", "--pin-file", str(tmp_path / "other.json")]) == 1
    assert "did not match" in capsys.readouterr().err and not (tmp_path / "other.json").exists()


# Always allowed: the owner's standing choice for named devices

def test_an_always_allowed_device_needs_no_window_and_nothing_else_is_opened(window):
    guard = p.FamilyProtection(window)
    always = window.with_name("always-allowed.json")
    p.set_always(always, [LAMP])
    assert guard.refusal(LAMP, "On") is None and guard.refusal(LAMP, "Off") is None
    # Only the named commands, and only the named device: the rest is as protected as before.
    assert guard.refusal(LAMP, "Toggle").startswith("Family protection is on: no control window has been opened.")
    assert guard.refusal(HEATER, "On").startswith("Family protection is on: no control window has been opened.")
    state = guard.summary()
    assert state["always_allowed"] == {"device_ids": [LAMP], "commands": ["On", "Off"]}
    assert state["live_writes_enabled"] is False and state["device_ids"] == []  # no window is open
    assert always.stat().st_mode & 0o777 == 0o600
    # A window still works beside it, for other devices.
    p.open_window(window, [HEATER], minutes=5)
    assert guard.refusal(HEATER, "Off") is None and guard.refusal(LAMP, "On") is None
    # Taking it back protects the lamp again.
    p.set_always(always, [])
    p.close_window(window)
    assert guard.refusal(LAMP, "On") is not None and guard.summary()["always_allowed"] == {"device_ids": [], "commands": []}


@pytest.mark.parametrize("damage", ["others_can_write", "not_json", "wildcard_inside", "missing"])
def test_an_always_allowed_file_that_cannot_be_trusted_allows_nothing(window, damage):
    always = window.with_name("always-allowed.json")
    p.set_always(always, [LAMP])
    if damage == "others_can_write":
        always.chmod(0o666)
    elif damage == "not_json":
        always.write_text("{")
    elif damage == "wildcard_inside":
        always.write_text(json.dumps({"device_ids": [LAMP], "commands": ["On"], "everything": True}))
    else:
        always.unlink()
    guard = p.FamilyProtection(window)
    assert guard.refusal(LAMP, "On") is not None and guard.summary()["always_allowed"]["device_ids"] == []
    for bad in ([""], ["home-1/*"]):
        with pytest.raises(ValueError):
            p.set_always(always, bad)
    with pytest.raises(ValueError):
        p.set_always(always, [LAMP], ["*"])


def test_the_hub_sends_to_an_always_allowed_device_and_no_page_or_agent_can_extend_the_list(pinned, window):
    client, _ = pinned
    assert send(client, LAMP, "On", "k-1").status_code == 403
    p.set_always(window.with_name("always-allowed.json"), [LAMP])
    assert client.get("/protection").json()["always_allowed"] == {"device_ids": [LAMP], "commands": ["On", "Off"]}
    done = send(client, LAMP, "On", "k-1").json()
    assert done["status"] == "done" and lamp_is_on(client)
    assert client.post(f"/devices/{LAMP}/simulate", json={"command": "Off"}).json()["would_succeed"] is True
    assert send(client, LAMP, "Toggle", "k-2").status_code == 403
    assert send(client, HEATER, "On", "k-3").status_code == 403
    # It is set on the hub's computer only: nothing over HTTP changes it, with or without the PIN.
    for method, path in (("post", "/protection/always"), ("put", "/protection/always"), ("post", "/protection/always-allowed")):
        assert getattr(client, method)(path, json={"device_ids": [HEATER], "pin": PIN}).status_code in (404, 405)
    opened = open_from_page(client, PIN, devices=[HEATER], always=[HEATER])
    assert opened.status_code == 422  # the window route takes no such field
    assert client.get("/protection").json()["always_allowed"]["device_ids"] == [LAMP]
    # Closing the window from a page does not touch the owner's standing choice.
    client.post("/protection/close")
    assert send(client, LAMP, "Off", "k-4").json()["status"] == "done"


def test_the_always_command_names_devices_or_takes_it_back(window, capsys):
    assert p.main(["--file", str(window), "always", "--device", LAMP, "--device", HEATER]) == 0
    out = capsys.readouterr().out
    assert f"Always allowed, with no window: {LAMP}, {HEATER} (On, Off)" in out and "always --none" in out
    assert "No real command will be sent to any other device" in out
    assert p.FamilyProtection(window).always().device_ids == [LAMP, HEATER]
    assert p.main(["--file", str(window), "status"]) == 0 and "Always allowed" in capsys.readouterr().out
    for wrong in (["always"], ["always", "--device", LAMP, "--none"], ["always", "--device", "*"]):
        assert p.main(["--file", str(window), *wrong]) == 1
        assert "Stopped" in capsys.readouterr().err
    assert p.FamilyProtection(window).always().device_ids == [LAMP, HEATER]  # mistakes change nothing
    assert p.main(["--file", str(window), "always", "--none"]) == 0
    assert "Always allowed" not in capsys.readouterr().out and p.FamilyProtection(window).always().device_ids == []
