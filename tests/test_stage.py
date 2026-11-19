import re
from pathlib import Path

from fastapi.testclient import TestClient

from porch_hub.main import Settings, create_app
from porch_hub.seed import demo_home

PAGE = Path(__file__).parents[1] / "porch_hub" / "dashboard" / "stage.html"
LOCK = "home-1/front-door-lock"


def app():
    return create_app(demo_home(simulate_latency=False).drivers, Settings(confirm_timeout_s=0.3))


def next_event(ws, type_):
    while (event := ws.receive_json())["type"] != type_:
        pass
    return event


def test_stage_is_served_by_the_hub():
    with TestClient(app()) as client:
        response = client.get("/stage")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert "<title>Porch · Live Home</title>" in response.text


def test_stage_needs_no_internet():
    """A projector at a venue may have no internet: no CDN, no web font, no remote asset."""
    page = PAGE.read_text()
    assert not re.search(r"""(src|href)\s*=\s*["']https?://""", page)
    assert "@import" not in page and "fonts.googleapis" not in page


def test_stage_never_writes_event_text_as_html():
    script = PAGE.read_text().split("<script>")[-1]
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script


def test_stage_shows_the_requested_alerts():
    page = PAGE.read_text()
    for text in ["Prompt injection detected", "Door unlock blocked → Sent approval request to owner phone",
                 "Emergency override", "Water leak detected → Water valve shut locally without waiting for agent",
                 "Agent conflict arbitrated", "Energy Agent vs. Comfort Agent → Prioritized household rule",
                 "Energy Management Only", "Full Home Access", "(Saved $", "/ws/events"]:
        assert text in page or text.replace("(Saved $", "Saved $") in page, text


def test_dry_runs_are_announced_for_the_digital_copy_panel():
    with TestClient(app()) as client, client.websocket_connect("/ws/events") as ws:
        preview = client.post(f"/devices/{LOCK}/simulate", json={"command": "UnlockDoor"}).json()
        event = next_event(ws, "command_simulated")
        state = client.get(f"/devices/{LOCK}/state").json()
    assert (event["device_id"], event["command"], event["would_succeed"], event["source"]) == (
        LOCK, "UnlockDoor", True, "matter")
    assert event["clusters"]["DoorLock"]["attributes"]["LockState"] == "Unlocked"
    assert event["latency_ms"] == preview["estimated_latency_ms"]
    assert state["DoorLock"]["attributes"]["LockState"] == "Locked"  # announced, not executed


def test_a_dry_run_that_would_fail_carries_its_reason():
    with TestClient(app()) as client, client.websocket_connect("/ws/events") as ws:
        client.post("/devices/home-1/laundry-dryer/simulate", json={"command": "SetDelayStart", "params": {"hours": 20}})
        event = next_event(ws, "command_simulated")
    assert event["would_succeed"] is False and "from 0 to 19, not 20" in event["error"]


def test_a_dry_run_refused_by_family_protection_says_so_for_the_stage_alert(tmp_path):
    """The stage raises its "Family protection is on" alert from this event's reason."""
    settings = Settings(confirm_timeout_s=0.3, family_protection=True, protection_path=tmp_path / "permission.json")
    with TestClient(create_app(demo_home(simulate_latency=False).drivers, settings)) as client, \
            client.websocket_connect("/ws/events") as ws:
        client.post(f"/devices/{LOCK}/simulate", json={"command": "LockDoor"})
        event = next_event(ws, "command_simulated")
    assert event["would_succeed"] is False and event["error"].startswith("Family protection")
    script = PAGE.read_text().split("<script>")[-1]
    assert "/^Family protection/.test(event.error" in script and 'kind: "family_protection"' in script


def test_stage_tiles_follow_the_home():
    """A home without a lock shows no lock tile, lights get tiles of their own, and scenes find devices by role."""
    script = PAGE.read_text().split("<script>")[-1]
    assert '{ key: "light", label: "Light", types: ["OnOffLight"], tiles: 2 }' in script
    assert '{ key: "tv", label: "TV", types: ["CastingVideoPlayer"] }' in script
    assert 'case "CastingVideoPlayer": {' in script and '["PAUSED", "warn"]' in script
    assert '["NOT PLAYING", "plain"]' in script  # a TV nobody is using is not "unknown"
    assert 'params.get("only")' in script and "Not in the registry" not in script
    assert "roleDevice(ROLES[" not in script
    for key in ("lock", "appliance", "leak", "valve", "plug"):
        assert f'roleDevice("{key}")' in script
