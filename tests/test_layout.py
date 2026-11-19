"""The home's floor plan: one file that both twin pages draw from."""

import json

import pytest
from fastapi.testclient import TestClient

from porch_hub import layout
from porch_hub.main import Settings, create_app
from porch_hub.seed import demo_home, demo_records

FAST = dict(confirm_timeout_s=0.3, poll_interval_s=0.02)

TOWNHOME = {
    "name": "Townhome",
    "floors": [
        {"id": "first", "name": "First floor", "width": 20, "depth": 37, "rooms": [
            {"id": "living", "name": "Living / Dining", "x": 0, "y": 0, "w": 14, "d": 23, "matches": ["Living Room"]},
            {"id": "kitchen", "name": "Kitchen", "x": 0, "y": 23, "w": 14, "d": 14, "kind": "kitchen"}],
         "fixtures": [{"kind": "stairs", "x": 14, "y": 3, "w": 6, "d": 12}, {"kind": "door", "x": 10, "y": 0, "w": 3}]},
        {"id": "second", "name": "Second floor", "width": 20, "depth": 37, "rooms": [
            {"id": "bedroom-1", "name": "Bedroom 1", "x": 0, "y": 0, "w": 13.75, "d": 11.5, "matches": ["Bedroom"]}]},
    ],
    "placements": {"home-1/living-stand": {"room": "living", "x": 0.15, "y": 0.2}},
    "extras": [{"name": "Dishwasher", "room": "kitchen", "x": 0.8, "y": 0.9}],
}


def client(path=None):
    return TestClient(create_app(demo_home(simulate_latency=False).drivers, Settings(home_layout_path=path, **FAST)))


def test_the_demo_home_has_a_room_for_every_room_its_devices_name():
    with client() as c:
        plan = c.get("/home/layout").json()
    assert (plan["name"], plan["source"], plan["problem"]) == ("Demo home", "demo", None)
    known = {name.lower() for floor in plan["floors"] for room in floor["rooms"]
             for name in (room["name"], *room["matches"])}
    assert {record.room.lower() for record in demo_records()} <= known


def test_the_demo_plan_places_every_demo_device_and_nothing_else():
    """The demo home is drawn deliberately: its lock is on the front door, not wherever a slot is free."""
    with client() as c:
        plan = c.get("/home/layout").json()
        devices = {device["id"]: device for device in c.get("/devices").json()}
    assert set(plan["placements"]) == set(devices)
    rooms = {room["id"]: room for floor in plan["floors"] for room in floor["rooms"]}
    for device_id, place in plan["placements"].items():
        room = rooms[place["room"]]
        # A device stands in the room the hub says it is in.
        assert devices[device_id]["room"].lower() in {room["name"].lower(), *(m.lower() for m in room["matches"])}, device_id
    door = next(f for f in plan["floors"][0]["fixtures"] if f["kind"] == "door")
    lock = plan["placements"]["home-1/front-door-lock"]
    entry = rooms[lock["room"]]
    assert abs(entry["x"] + lock["x"] * entry["w"] - (door["x"] + door["w"] / 2)) < 0.5  # on the door
    assert abs(entry["y"] + lock["y"] * entry["d"] - door["y"]) < 0.5


def test_a_home_serves_its_own_plan_and_an_edit_shows_without_a_restart(tmp_path):
    path = tmp_path / "home-layout.json"
    path.write_text(json.dumps(TOWNHOME))
    with client(path) as c:
        plan = c.get("/home/layout").json()
        assert (plan["name"], plan["source"]) == ("Townhome", "file")
        assert [floor["name"] for floor in plan["floors"]] == ["First floor", "Second floor"]
        assert plan["placements"]["home-1/living-stand"] == {"room": "living", "x": 0.15, "y": 0.2}
        assert plan["floors"][0]["rooms"][1]["kind"] == "kitchen" and plan["extras"][0]["note"]
        path.write_text(json.dumps({**TOWNHOME, "name": "Townhome, edited"}))
        assert c.get("/home/layout").json()["name"] == "Townhome, edited"


@pytest.mark.parametrize("broken, says", [
    (None, "does not exist yet"),
    ("{not json", "cannot be used"),
    ({**TOWNHOME, "placements": {"x": {"room": "attic"}}}, "names a room that is not on the plan"),
    ({**TOWNHOME, "floors": []}, "cannot be used"),
    ({**TOWNHOME, "floors": [{**TOWNHOME["floors"][0], "width": 5}]}, "reaches outside"),
    ({**TOWNHOME, "floors": [TOWNHOME["floors"][0], {**TOWNHOME["floors"][1], "rooms": TOWNHOME["floors"][0]["rooms"]}]},
     "two rooms have the id"),
])
def test_a_plan_that_cannot_be_used_falls_back_to_the_demo_plan_and_says_why(tmp_path, broken, says):
    path = tmp_path / "home-layout.json"
    if broken is not None:
        path.write_text(broken if isinstance(broken, str) else json.dumps(broken))
    with client(path) as c:
        plan = c.get("/home/layout").json()
    assert plan["source"] == "demo" and plan["name"] == "Demo home" and says in plan["problem"]


def test_the_layout_file_comes_from_the_settings(monkeypatch, tmp_path):
    monkeypatch.delenv("PORCH_HOME_LAYOUT", raising=False)
    assert Settings().home_layout_path is None and layout.layout_file({}) is None
    monkeypatch.setenv("PORCH_HOME_LAYOUT", str(tmp_path / "plan.json"))
    assert Settings().home_layout_path == tmp_path / "plan.json"
