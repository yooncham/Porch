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


def test_home_3d_has_a_model_for_every_demo_device_type():
    page = PAGE.read_text()
    for device_type in {DeviceType.DOOR_LOCK, DeviceType.WATER_LEAK_SENSOR, DeviceType.WATER_VALVE,
                        DeviceType.LAUNDRY_DRYER, DeviceType.ROBOTIC_VACUUM_CLEANER, DeviceType.AIR_PURIFIER,
                        DeviceType.ON_OFF_PLUG}:
        assert f'case "{device_type}"' in page, device_type
    for room in ["Entry", "Kitchen", "Utility", "Office", "Laundry", "Living room", "Bedroom"]:
        assert f'"{room}"' in page, room  # every room the demo home uses has a place on the plan


def test_home_3d_follows_the_live_event_stream_and_command_flow():
    page = PAGE.read_text()
    for text in ["/ws/events", "state_changed", "command_simulated", "command_executed", "mode_changed",
                 "resync_required", "/simulate", "/commands", "idempotency_key"]:
        assert text in page, text


def test_home_3d_never_writes_hub_text_as_html():
    script = PAGE.read_text().split('<script type="module">')[-1]
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script
