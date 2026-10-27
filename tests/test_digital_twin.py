from pathlib import Path

from fastapi.testclient import TestClient

from porch_hub.main import Settings, create_app
from porch_hub.seed import demo_home

PAGE = Path(__file__).parents[1] / "porch_hub" / "dashboard" / "digital-twin.html"


def test_digital_twin_is_served_by_the_hub():
    with TestClient(create_app(demo_home(simulate_latency=False).drivers, Settings())) as client:
        response = client.get("/digital-twin")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert "<title>Porch Digital Twin</title>" in response.text


def test_digital_twin_has_the_requested_zones_layers_and_scenarios():
    page = PAGE.read_text()
    for text in ["Entrance", "Utility room", "Living room", "Laundry",                   # zones
                 "Yale smart lock", "Leak sensor", "Shutoff valve", "LG dryer", "Space heater",
                 "PHYSICAL HARDWARE", "DIGITAL COPY", "PORCH ENGINE",                   # layers and pulses
                 "Simulate Water Leak", "Simulate Lock Attack", "Simulate Off-Peak Energy Shift",
                 "Approve unlocking the front door?", "/ws/events", "command_simulated", "command_executed"]:
        assert text in page, text


def test_digital_twin_never_writes_hub_text_as_html():
    script = PAGE.read_text().split("<script>")[-1]
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script
