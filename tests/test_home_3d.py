import re
from pathlib import Path

from fastapi.testclient import TestClient

from porch_hub.main import Settings, create_app
from porch_hub.models import DeviceType
from porch_hub.seed import demo_home

PAGE = Path(__file__).parents[1] / "porch_hub" / "dashboard" / "home-3d.html"


def client():
    return TestClient(create_app(demo_home(simulate_latency=False).drivers, Settings()))


def test_home_3d_and_three_js_are_served_by_the_hub():
    with client() as c:
        page = c.get("/home-3d")
        three = c.get("/vendor/three/three.module.min.js")
        controls = c.get("/vendor/three/OrbitControls.js")
        other = c.get("/vendor/three/LICENSE.txt")
        escape = c.get("/vendor/three/..%2F..%2Fapi.py")
    assert page.status_code == 200 and "<title>Porch Home 3D</title>" in page.text
    assert three.status_code == 200 and three.headers["content-type"].startswith("text/javascript")
    assert "REVISION" in three.text
    assert controls.status_code == 200 and "class OrbitControls" in controls.text
    assert other.status_code == 404 and escape.status_code == 404  # only the two named files


def test_home_3d_loads_nothing_from_the_internet():
    page = PAGE.read_text()
    assert not re.search(r"""(src|href)=["']https?://""", page)
    assert not re.search(r"""from\s+["']https?://""", page)
    assert '"three": "/vendor/three/three.module.min.js"' in page


def test_home_3d_has_a_model_for_every_device_type_the_homes_use():
    page = PAGE.read_text()
    for device_type in {DeviceType.DOOR_LOCK, DeviceType.WATER_LEAK_SENSOR, DeviceType.WATER_VALVE,
                        DeviceType.LAUNDRY_DRYER, DeviceType.ROBOTIC_VACUUM_CLEANER, DeviceType.AIR_PURIFIER,
                        DeviceType.STICK_VACUUM, DeviceType.CASTING_VIDEO_PLAYER,
                        DeviceType.ON_OFF_PLUG, DeviceType.ON_OFF_LIGHT, DeviceType.DOORBELL}:
        assert f'case "{device_type}"' in page, device_type
    builders = page.split("const BUILDERS = {")[1].split("\n};")[0]
    models = set(re.findall(r'"([a-z]+)"', page.split("function modelFor(device)")[1].split("\n}")[0]))
    assert {"lamp", "plug", "doorbell", "purifier", "tv", "generic"} <= models
    for model in models:
        assert f"\n  {model}(type, d, spot)" in builders, model  # every model named has a builder


def test_home_3d_shows_what_a_tv_is_doing_and_stands_it_against_a_wall():
    script = PAGE.read_text().split('<script type="module">')[-1]
    words = script.split("function tvState(dev) {")[1].split("\n}\n")[0]
    # Standby wins over what it last played; otherwise what it plays, then whether it is in use.
    assert words.index('"Standby"') < words.index('"Playing", "Paused", "Buffering"') < words.index('"In use"')
    assert words.index('"In use"') < words.index('"Not playing"') < words.index('"?"')  # known, so not "?"
    tv = script.split("  tv(type, d, spot) {")[1].split("\n  },\n")[0]
    assert "facingIntoRoom(spot)" in tv and 'bars.visible = s === "Paused"' in tv
    assert "dev.online ? tvState(dev)" in tv  # an offline TV shows a dark screen, whatever it last said


def test_home_3d_lamp_glows_as_bright_as_it_is_set():
    script = PAGE.read_text().split('<script type="module">')[-1]
    lamp = script.split("  lamp(type, d, spot) {")[1].split("\n  },\n")[0]
    assert "brightness(d)" in lamp and "share === null ? 1" in lamp  # a lamp that does not say glows fully
    assert 'attr(dev, "LevelControl", "CurrentLevel")' in script


def test_home_3d_builds_the_house_from_the_homes_layout():
    """The same plan the 2D twin draws: no room, wall or device position of one home is in the page."""
    page = PAGE.read_text()
    script = page.split('<script type="module">')[-1]
    for text in ['"/home/layout"', "layout.floors", "floor.rooms", "floor.fixtures", "layout.placements",
                 "layout.extras", "room.matches", "layout.problem"]:
        assert text in script, text
    for home_specific in ('"Living room"', '"Kitchen"', '"Utility"', '"Entry"', "Living stand", "home-1/"):
        assert home_specific not in script, home_specific
    # Every device model stands where it is told to, not at a fixed place.
    assert not re.search(r"placed\(-?\d", script)


def test_home_3d_places_devices_by_the_same_rules_as_the_2d_twin():
    def slots(path):
        return re.search(r"const SLOTS = (\[\[.*?\]\]);", path.read_text()).group(1).replace(" ", "")
    assert slots(PAGE) == slots(PAGE.with_name("digital-twin.html"))


def test_home_3d_cuts_doorways_and_puts_a_lock_on_the_plans_door():
    script = PAGE.read_text().split('<script type="module">')[-1]
    assert "const DOORS = [];" in script and 'fixture.kind !== "door"' in script
    wall = script.split("function wall(axis, fixed, from, to) {")[1].split("\n}\n")[0]
    assert "DOORS.filter" in wall  # a wall is built in pieces around the doors on its line
    lock = script.split("  lock(type, d, spot) {")[1].split("\n  },\n")[0]
    assert "door.taken = true" in lock and "door.leaf.visible = false" in lock and "release()" in lock


def test_home_3d_shows_a_refusal_and_offers_the_owners_window():
    script = PAGE.read_text().split('<script type="module">')[-1]
    assert "/^Family protection/" in script and "openProtectionPanel" in script
    assert "protection_changed" in script


def test_home_3d_follows_the_live_event_stream_and_command_flow():
    page = PAGE.read_text()
    for text in ["/ws/events", "state_changed", "command_simulated", "command_executed", "mode_changed",
                 "resync_required", "/simulate", "/commands", "idempotency_key"]:
        assert text in page, text


def test_home_3d_never_writes_hub_text_as_html():
    script = PAGE.read_text().split('<script type="module">')[-1]
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script
