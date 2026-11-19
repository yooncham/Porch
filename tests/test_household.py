"""The household (porch_hub/household.py): who lives here, where each person is, and their PINs.

The gateway's room rule depends on it: an agent acting for one person may not change a device in a
room where someone else is, while its own person is not there, without that person's permission.
So where people are must only be changed by them or the owner, with a PIN.
"""

import json

import pytest
from fastapi.testclient import TestClient

from porch_hub import household as h
from porch_hub import protection as p
from porch_hub.layout import load_layout
from porch_hub.main import Settings, create_app
from porch_hub.seed import demo_home

OWNER_PIN, WIFE_PIN = "482915", "731406"
ROOMS = h.rooms_of(load_layout(None))  # the demo home's plan


@pytest.fixture
def folder(tmp_path):
    folder = tmp_path / "porch-hub"
    folder.mkdir()
    return folder


@pytest.fixture
def home(folder):
    owner_pin = p.OwnerPin(folder / "owner-pin.json")
    household = h.Household(folder / "household.json", owner_pin)
    household.add("yoonchan", "Yoonchan", owner=True)
    household.add("wife", "Wife")
    p.set_pin(folder / "owner-pin.json", OWNER_PIN)
    p.set_pin(household.pin_path("wife"), WIFE_PIN)
    return household


def where(home, person):
    return next(x for x in home.summary(ROOMS)["people"] if x["id"] == person)


# Rooms and devices

def test_a_device_is_in_the_room_the_plan_puts_it_else_the_room_its_name_matches():
    layout = load_layout(None)
    assert h.place_of({"id": "home-1/desk-lamp-plug", "room": "Kitchen"}, layout) == "office"  # the plan wins
    assert h.place_of({"id": "home-1/new-lamp", "room": "living room"}, layout) == "living"   # its room's name
    assert h.place_of({"id": "home-1/yale", "room": "Entryway"}, layout) == "entry"           # one of its matches
    assert h.place_of({"id": "home-1/thing", "room": "Unassigned"}, layout) is None
    placed = h.places([{"id": "home-1/thing", "room": "Attic"}], layout)
    assert placed["devices"] == {"home-1/thing": None} and {"id": "living", "name": "Living room", "floor": "Home"} in [
        {**r, "floor": "Home"} for r in placed["rooms"] if r["id"] == "living"]


# People and PINs

def test_people_are_added_named_and_removed_and_the_owner_stays(home, folder):
    assert [x["id"] for x in home.summary(ROOMS)["people"]] == ["yoonchan", "wife"]
    assert where(home, "wife")["where"] == "unknown" and where(home, "yoonchan")["owner"] is True
    for wrong in (lambda: home.add("wife", "Again"), lambda: home.add("guest", "Guest", owner=True),
                  lambda: home.add("two words", "x"), lambda: home.remove("yoonchan")):
        with pytest.raises(ValueError):
            wrong()
    home.rename("wife", "Jiwoo")
    assert where(home, "wife")["name"] == "Jiwoo"
    assert home.pin("wife").is_set() and home.pin("yoonchan").is_set()
    home.remove("wife")
    assert not (folder / "household-pin-wife.json").exists() and [x["id"] for x in home.summary(ROOMS)["people"]] == ["yoonchan"]
    with pytest.raises(h.PersonUnknown):
        home.verify("wife", WIFE_PIN)
    assert (folder / "household.json").stat().st_mode & 0o777 == 0o600


def test_where_someone_is_needs_their_pin_or_the_owners(home):
    home.set_place("wife", "home", "living", ROOMS, by="wife", pin=WIFE_PIN)
    assert (where(home, "wife")["where"], where(home, "wife")["room_name"]) == ("home", "Living room")
    home.set_place("wife", "home", "bedroom", ROOMS, by="yoonchan", pin=OWNER_PIN)  # the owner may
    assert where(home, "wife")["room"] == "bedroom" and where(home, "wife")["set_by"] == "yoonchan"
    with pytest.raises(h.NotAllowed):
        home.set_place("yoonchan", "away", None, ROOMS, by="wife", pin=WIFE_PIN)  # not someone else's
    with pytest.raises(h.WrongPin):
        home.set_place("wife", "away", None, ROOMS, by="wife", pin="000000")
    with pytest.raises(ValueError, match="no room 'attic'"):
        home.set_place("wife", "home", "attic", ROOMS, by="wife", pin=WIFE_PIN)
    home.set_place("wife", "away", "living", ROOMS, by=None, pin=None)  # the hub's computer: the owner's hand
    assert where(home, "wife")["where"] == "away" and where(home, "wife")["room"] is None
    home.set_place("wife", "home", None, ROOMS, by="wife", pin=WIFE_PIN)
    assert (where(home, "wife")["where"], where(home, "wife")["room"]) == ("home", None)


def test_wrong_pins_lock_that_persons_pin_only(home, monkeypatch):
    monkeypatch.setattr(p, "PIN_LOCK_S", 60.0)
    for _ in range(p.PIN_TRIES - 1):
        with pytest.raises(h.WrongPin):
            home.verify("wife", "000000")
    with pytest.raises(h.PinLocked):
        home.verify("wife", "000000")
    with pytest.raises(h.PinLocked):
        home.verify("wife", WIFE_PIN)  # locked, even for the right PIN
    home.verify("yoonchan", OWNER_PIN)  # the owner's PIN is not locked by her wrong guesses


# Through the hub

@pytest.fixture
def client(folder, home):
    settings = Settings(confirm_timeout_s=0.3, poll_interval_s=0.02, household_path=folder / "household.json",
                        owner_pin_path=folder / "owner-pin.json", gateway_url="http://127.0.0.1:9")
    with TestClient(create_app(demo_home(simulate_latency=False).drivers, settings)) as test_client:
        yield test_client


def test_the_hub_shows_the_household_and_takes_places_with_a_pin(client):
    state = client.get("/household").json()
    assert state["set_by_hand"] is True and [x["id"] for x in state["people"]] == ["yoonchan", "wife"]
    assert {"id": "living", "name": "Living room"}.items() <= next(r for r in state["rooms"] if r["id"] == "living").items()
    assert all("pin" not in json.dumps(x).lower().replace("pin_set", "") for x in state["people"])

    def mark(**body):
        return client.post("/household/presence", json={"person": "wife", "where": "home", "room": "living",
                                                          "by": "wife", "pin": WIFE_PIN, **body})

    with client.websocket_connect("/ws/events") as ws:
        assert mark().status_code == 200
        while (event := ws.receive_json())["type"] != "household_changed":
            pass
    assert next(x for x in event["data"]["people"] if x["id"] == "wife")["room"] == "living"
    assert mark(pin="000000").status_code == 401
    assert mark(by="yoonchan", pin=OWNER_PIN).status_code == 200
    assert mark(person="yoonchan", by="wife").status_code == 403
    assert mark(person="ghost").status_code == 404
    assert mark(room="attic").status_code == 422
    assert mark(where="sideways").status_code == 422
    assert client.post("/household/presence", json={"person": "wife", "where": "away", "by": "wife"}).status_code == 422


def test_the_gateway_can_check_a_persons_pin_and_nothing_else(client):
    assert client.post("/household/verify", json={"person": "wife", "pin": WIFE_PIN}).json() == {"ok": True, "person": "wife"}
    assert client.post("/household/verify", json={"person": "wife", "pin": "000000"}).status_code == 401
    assert client.post("/household/verify", json={"person": "ghost", "pin": WIFE_PIN}).status_code == 404


def test_places_say_which_room_each_device_is_in(client):
    placed = client.get("/home/places").json()
    assert placed["devices"]["home-1/space-heater-plug"] == "living" and placed["devices"]["home-1/air-purifier"] == "bedroom"
    assert {r["id"] for r in placed["rooms"]} >= {"living", "bedroom", "office"}


def test_online_access_is_read_from_the_switch_and_the_keys(client, folder):
    assert client.get("/online").json() == {"enabled": False, "since": None, "agents": []}
    (folder / "online.json").write_text(json.dumps({"enabled": True, "since": "2026-11-16T10:00:00+00:00"}))
    (folder / "online-keys.json").write_text(json.dumps({"keys": {"abc": {"agent": "muse"}, "def": {"agent": "dot"}}}))
    assert client.get("/online").json() == {"enabled": True, "since": "2026-11-16T10:00:00+00:00", "agents": ["dot", "muse"]}


def test_agents_and_approvals_say_so_when_the_gateway_is_not_running(client):
    for path in ("/household/agents", "/household/approvals"):
        answer = client.get(path)
        assert answer.status_code == 503 and "gateway is not running" in answer.json()["detail"]
    assert client.post("/household/approvals/abc/maybe", json={"person": "wife", "pin": WIFE_PIN}).status_code == 404


def test_the_household_command_is_the_owners_hand(folder, monkeypatch, capsys):
    monkeypatch.setenv("PORCH_HOUSEHOLD_FILE", str(folder / "household.json"))
    monkeypatch.setenv("PORCH_OWNER_PIN_FILE", str(folder / "owner-pin.json"))
    monkeypatch.delenv("PORCH_HOME_LAYOUT", raising=False)
    assert h.main(["add", "yoonchan", "--name", "Yoonchan", "--owner"]) == 0
    assert h.main(["add", "wife", "--name", "Wife"]) == 0
    assert "set-pin wife" in capsys.readouterr().out
    assert h.main(["where", "wife", "Living room"]) == 0  # a room by its name or its id
    out = capsys.readouterr().out
    assert "home, in Living room" in out and "Rooms: entry, living" in out
    assert h.main(["where", "yoonchan", "away"]) == 0
    assert h.main(["where", "wife", "attic"]) == 1 and "no room 'attic'" in capsys.readouterr().err
    typed = iter([WIFE_PIN, WIFE_PIN])
    monkeypatch.setattr("getpass.getpass", lambda prompt: next(typed))
    assert h.main(["set-pin", "wife"]) == 0 and WIFE_PIN not in capsys.readouterr().out
    assert h.Household(folder / "household.json").pin("wife").is_set()
    assert h.main(["set-pin", "yoonchan"]) == 1  # the owner's PIN is family protection's
