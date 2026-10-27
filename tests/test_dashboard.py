import re
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from porch_hub.main import Settings, create_app
from porch_hub.seed import demo_home

PAGE = Path(__file__).parents[1] / "porch_hub" / "dashboard" / "index.html"
LOCK = "home-1/front-door-lock"


def app(**settings):
    return create_app(demo_home(simulate_latency=False).drivers, Settings(confirm_timeout_s=0.3, **settings))


def next_event(ws, type_):
    while (event := ws.receive_json())["type"] != type_:
        pass
    return event


def test_dashboard_is_served_by_the_hub():
    with TestClient(app()) as client:
        response = client.get("/dashboard")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert "<title>Porch Device Hub</title>" in response.text
    assert "/ws/events" in response.text
    assert client.get("/", follow_redirects=False).headers["location"] == "/dashboard"
    # Tailwind is pinned to an exact version, never a floating major.
    assert re.search(r"@tailwindcss/browser@\d+\.\d+\.\d+", response.text)


TAILWIND_PAGES = [PAGE, PAGE.with_name("digital-twin.html")]


@pytest.mark.parametrize("page_path", TAILWIND_PAGES, ids=lambda p: p.name)
def test_every_class_the_page_uses_has_an_offline_style(page_path):
    """Demo Day may have no internet for the Tailwind CDN; the inline rules must cover the whole page."""
    page = page_path.read_text()
    css = re.search(r'<style id="fallback-styles">(.*?)</style>', page, re.S).group(1)
    used = set()
    for pattern in (r'class="([^"]*)"', r'\bel\(\s*"[a-z0-9]+"\s*,\s*"([^"]*)"', r'\bcls\("([^"]*)"\)'):
        for match in re.finditer(pattern, page):
            used.update(match.group(1).split())
    assert len(used) > 80  # the extraction found the page's classes

    def selector(name):
        escaped = re.sub(r"([:/.])", r"\\\1", name)
        if name[0].isdigit():  # CSS writes a leading digit as a hex escape: 2xl -> \32xl
            escaped = f"\\3{name[0]}" + escaped[1:]
        return r"\." + re.escape(escaped) + r"(?![\w-])"

    assert sorted(c for c in used if not re.search(selector(c), css)) == []


def test_device_data_is_never_written_as_html():
    script = PAGE.read_text().split("<script>")[-1]
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script


def test_ws_events_streams_commands_with_latency_and_read_back():
    with TestClient(app()) as client, client.websocket_connect("/ws/events") as ws:
        client.post(f"/devices/{LOCK}/commands", json={"command": "UnlockDoor", "idempotency_key": "dash-1"})
        state = next_event(ws, "state_changed")
        executed = next_event(ws, "command_executed")
        client.post(f"/devices/{LOCK}/commands", json={"command": "UnlockDoor", "idempotency_key": "dash-1"})
        replay = next_event(ws, "command_executed")
    assert state["changed"] == {"DoorLock": {"LockState": "Unlocked"}}
    assert {k: executed[k] for k in ("device_id", "command", "status", "idempotency_key", "source", "simulated",
                                     "replayed")} == {
        "device_id": LOCK, "command": "UnlockDoor", "status": "done", "idempotency_key": "dash-1",
        "source": "matter", "simulated": False, "replayed": False}
    assert isinstance(executed["latency_ms"], int)
    assert executed["clusters"]["DoorLock"]["attributes"]["LockState"] == "Unlocked"
    assert replay["replayed"] is True and replay["latency_ms"] == executed["latency_ms"]


def test_rejected_requests_are_not_logged_as_executions():
    with TestClient(app()) as client, client.websocket_connect("/ws/events") as ws:
        assert client.post("/devices/home-1/kitchen-leak-sensor/commands",
                           json={"command": "On", "idempotency_key": "bad"}).status_code == 422
        client.post("/devices/home-1/desk-lamp-plug/commands", json={"command": "On", "idempotency_key": "good"})
        first = next_event(ws, "command_executed")
    assert first["idempotency_key"] == "good"


def test_offline_switch_and_simulated_commands_reach_the_dashboard():
    with TestClient(app()) as client, client.websocket_connect("/ws/events") as ws:
        client.put("/demo/offline-mode", json={"enabled": True})
        mode = next_event(ws, "mode_changed")
        client.post("/devices/home-1/main-water-valve/commands", json={"command": "Close", "idempotency_key": "v1"})
        executed = next_event(ws, "command_executed")
        client.put("/demo/offline-mode", json={"enabled": False})
        back = next_event(ws, "mode_changed")
    assert (mode["data"], back["data"]) == ({"offline": True}, {"offline": False})
    assert "device_id" not in mode  # about the hub, not a device; empty fields are left out
    assert (executed["status"], executed["simulated"], executed["latency_ms"]) == ("done", True, 0)
