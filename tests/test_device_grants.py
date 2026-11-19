"""The devices each agent may control, as the owner chose them (agent-devices.json).

The owner gives an agent devices on the How Porch works page, with the owner PIN, or with
`agent.sh devices`; the gateway reads the file on every request and refuses any other device. An
agent the owner never limited controls what its rights allow, as before. A damaged file stops every
action rather than guess who was limited.
"""

import importlib.util
import json
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from porch_hub import agent_grants
from porch_hub import api as hub_api
from porch_hub import household as hh
from porch_hub import protection as p
from porch_hub.main import Settings, create_app
from porch_hub.seed import demo_home

ROOT = Path(__file__).parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


grants = load("device_grants", ROOT / "porch_gateway" / "device_grants.py")
agents_tool = load("home_agents_for_grants", ROOT / "deploy" / "home" / "agents.py")
GATEWAY = (ROOT / "porch_gateway" / "porch_gateway.py").read_text()

LAMP, HEATER, DRYER = "home-1/desk-lamp-plug", "home-1/space-heater-plug", "home-1/laundry-dryer"
HOME = {LAMP: {"name": "Desk lamp"}, HEATER: {"name": "Space heater"}, DRYER: {"name": "Dryer"}}
MUSE = {"id": "muse", "name": "Muse"}
PIN = "482915"


@pytest.fixture(autouse=True)
def no_real_settings(tmp_path, monkeypatch):
    """Every hub here keeps its settings in a temporary folder: a test never reads, or guesses
    against, the real home's household or owner PIN."""
    monkeypatch.setenv("PORCH_CONFIG_DIR", str(tmp_path / "hub-settings"))
    for name in ("PORCH_HOUSEHOLD_FILE", "PORCH_OWNER_PIN_FILE", "PORCH_PROTECTION_FILE"):
        monkeypatch.delenv(name, raising=False)


def fake_gateway(monkeypatch):
    async def gateway(_hub, method, path, body=None):
        assert (method, path) == ("GET", "/household/agents")
        return [{"id": "muse", "name": "Muse"}, {"id": "dot", "name": "Dot"}]

    monkeypatch.setattr(hub_api, "_gateway", gateway)


# ---------- what the gateway reads, and what it says ----------

def test_no_file_means_no_agent_is_limited(tmp_path):
    assert grants.read(tmp_path / "agent-devices.json") == {}


def test_a_damaged_file_is_never_taken_as_no_limits(tmp_path):
    path = tmp_path / "agent-devices.json"
    for text in ("{not json", "[]", '{"agents": []}', '{"agents": {"muse": {}}}', '{"agents": {"muse": {"devices": "x"}}}',
                 '{"agents": {"muse": {"devices": [1]}}}'):
        path.write_text(text)
        with pytest.raises(grants.Unreadable):
            grants.read(path)


def test_an_agent_given_one_device_is_refused_every_other_and_told_which_it_has():
    assert grants.refusal(MUSE, {"id": LAMP, "name": "Desk lamp"}, [LAMP], HOME) is None
    assert grants.refusal(MUSE, {"id": HEATER, "name": "Space heater"}, [LAMP], HOME) == \
        "Permission: Muse may control only Desk lamp in this home, as the owner chose."
    assert grants.refusal(MUSE, {"id": HEATER}, [LAMP, DRYER], HOME).endswith("only Desk lamp and Dryer in this home, as the owner chose.")
    assert grants.refusal(MUSE, {"id": LAMP}, [], HOME) == "Permission: the owner has given Muse no device to control in this home."
    assert grants.names([LAMP, HEATER, DRYER], HOME) == "Desk lamp, Space heater and Dryer"
    assert grants.summary(None) == "any device its rights allow" and grants.summary([], HOME) == "no device"


def test_the_gateway_checks_the_grant_after_the_agents_rights_and_before_anything_is_allowed():
    decide = GATEWAY.split("def decide(", 1)[1].split("\ndef ", 1)[0]
    scope, grant = decide.index('if cap not in agent["scopes"]'), decide.index("device_grants.read(AGENT_DEVICES_FILE)")
    safe, emergency = decide.index("if (cap, command) in SAFE_DIRECTION"), decide.index("Emergency exception")
    assert emergency < scope < grant < safe  # a leak still lets any agent close the water main
    assert "except device_grants.Unreadable" in decide and 'return "deny", f"Porch could not read which devices' in decide
    assert 'or CONFIG_PATH.parent / device_grants.FILE' in GATEWAY  # beside the gateway's settings, like the hub's
    route = GATEWAY.split('@mcp.custom_route("/household/agents"', 1)[1].split("@mcp.custom_route", 1)[0]
    assert '"devices": grants.get(a["id"])' in route


# ---------- what the hub writes, for the owner only ----------

def test_what_the_hub_writes_the_gateway_reads_and_other_agents_keep_theirs(tmp_path):
    path = tmp_path / "agent-devices.json"
    agent_grants.write(path, "dot", [HEATER], by="test")
    agent_grants.write(path, "muse", [LAMP, LAMP], by="test")
    assert grants.read(path) == {"dot": [HEATER], "muse": [LAMP]}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    agent_grants.write(path, "muse", None, by="test")  # any device its rights allow again
    assert grants.read(path) == {"dot": [HEATER]}
    agent_grants.write(path, "dot", [], by="test")  # no device
    assert grants.read(path) == {"dot": []}


def test_a_damaged_file_is_left_for_the_owner_to_look_at(tmp_path):
    path = tmp_path / "agent-devices.json"
    path.write_text('{"agents": {"muse": {"devices": "everything"}}}')
    with pytest.raises(agent_grants.Damaged):
        agent_grants.write(path, "dot", [LAMP], by="test")
    assert path.read_text() == '{"agents": {"muse": {"devices": "everything"}}}'


@pytest.fixture
def owner_page(tmp_path, monkeypatch):
    """A hub with family protection and an owner PIN, whose gateway knows Muse."""
    window = tmp_path / "settings" / "permission.json"
    pin_file = tmp_path / "settings" / "owner-pin.json"
    p.set_pin(pin_file, PIN)
    monkeypatch.setattr(p, "PIN_LOCK_S", 0.2)
    fake_gateway(monkeypatch)
    settings = Settings(confirm_timeout_s=0.3, poll_interval_s=0.02, family_protection=True, protection_path=window,
                        owner_pin_path=pin_file)
    with TestClient(create_app(demo_home(simulate_latency=False).drivers, settings)) as client:
        yield client, window.parent / "agent-devices.json"


def test_the_owner_gives_muse_one_device_with_the_pin(owner_page):
    client, path = owner_page
    answer = client.post("/household/agents/muse/devices", json={"devices": [LAMP], "pin": PIN})
    assert answer.status_code == 200 and answer.json() == {"agent": "muse", "devices": [LAMP]}
    assert grants.read(path) == {"muse": [LAMP]}
    assert json.loads(path.read_text())["agents"]["muse"]["by"] == "the owner's page"
    assert client.post("/household/agents/muse/devices", json={"devices": None, "pin": PIN}).status_code == 200
    assert grants.read(path) == {}


@pytest.mark.parametrize(("body", "status"), [
    ({"devices": [LAMP], "pin": "000000"}, 401),                # not the owner
    ({"devices": ["home-1/no-such-thing"], "pin": PIN}, 404),   # not this home's device
    ({"devices": [LAMP]}, 422),                                 # no PIN at all
])
def test_nothing_changes_without_the_owner_or_for_a_device_the_home_lacks(owner_page, body, status):
    client, path = owner_page
    assert client.post("/household/agents/muse/devices", json=body).status_code == status
    assert not path.exists()


def test_an_agent_porch_does_not_know_is_refused(owner_page):
    client, path = owner_page
    answer = client.post("/household/agents/ghost/devices", json={"devices": [LAMP], "pin": PIN})
    assert answer.status_code == 404 and "no agent ghost" in answer.json()["detail"] and not path.exists()


def test_without_family_protection_the_household_owners_pin_works(tmp_path, monkeypatch):
    folder = tmp_path / "hub-settings"
    folder.mkdir()
    home = hh.Household(folder / "household.json", p.OwnerPin(folder / "owner-pin.json"))
    home.add("yoonchan", "Yoonchan", owner=True)
    p.set_pin(folder / "owner-pin.json", PIN)
    fake_gateway(monkeypatch)
    with TestClient(create_app(demo_home(simulate_latency=False).drivers,
                               Settings(confirm_timeout_s=0.3, poll_interval_s=0.02))) as client:
        assert client.post("/household/agents/muse/devices", json={"devices": [LAMP], "pin": "000000"}).status_code == 401
        assert client.post("/household/agents/muse/devices", json={"devices": [LAMP], "pin": PIN}).status_code == 200
    assert grants.read(folder / "agent-devices.json") == {"muse": [LAMP]}


def test_a_hub_without_an_owner_pin_sends_the_owner_to_the_hubs_computer():
    with TestClient(create_app(demo_home(simulate_latency=False).drivers,
                               Settings(confirm_timeout_s=0.3, poll_interval_s=0.02))) as client:
        answer = client.post("/household/agents/muse/devices", json={"devices": [LAMP], "pin": PIN})
    assert answer.status_code == 409 and "agent.sh devices" in answer.json()["detail"]


# ---------- the same from the hub's computer: agent.sh devices ----------

@pytest.fixture
def settings_dir(tmp_path, monkeypatch):
    folder = tmp_path / "porch-hub"
    folder.mkdir()
    config = folder / "porch_config.json"
    config.write_text(json.dumps({"agents": {"muse-key-never-printed": {"id": "muse", "name": "Muse", "scopes": ["lighting"]}}}))
    config.chmod(0o600)
    monkeypatch.setenv("PORCH_CONFIG_DIR", str(folder))
    monkeypatch.delenv("PORCH_GATEWAY_CONFIG", raising=False)
    monkeypatch.setattr(agents_tool, "hub_devices", lambda: {LAMP: "Desk lamp", HEATER: "Space heater"})
    return folder


def test_the_owner_can_choose_from_the_terminal_too(settings_dir, capsys):
    assert agents_tool.main(["devices", "muse", LAMP]) == 0
    assert "may control only Desk lamp" in capsys.readouterr().out
    assert grants.read(settings_dir / "agent-devices.json") == {"muse": [LAMP]}
    agents_tool.main(["list"])
    listed = capsys.readouterr().out
    assert f"controls only {LAMP}" in listed and "muse-key-never-printed" not in listed
    assert agents_tool.main(["devices", "muse", "--none"]) == 0
    assert grants.read(settings_dir / "agent-devices.json") == {"muse": []}
    assert agents_tool.main(["devices", "muse", "--any"]) == 0
    assert grants.read(settings_dir / "agent-devices.json") == {}


@pytest.mark.parametrize("argv", [["devices", "muse"], ["devices", "muse", LAMP, "--any"], ["devices", "ghost", LAMP],
                                  ["devices", "muse", "home-1/no-such-thing"]])
def test_mistakes_from_the_terminal_change_nothing(settings_dir, argv):
    with pytest.raises(SystemExit):
        agents_tool.main(argv)
    assert not (settings_dir / "agent-devices.json").exists()


# ---------- the pages ----------

def test_the_porch_page_lets_the_owner_choose_with_the_pin_and_writes_no_markup():
    page = (ROOT / "porch_hub" / "dashboard" / "porch.html").read_text()
    script = page.split("<script>")[-1]
    assert "Choose devices" in script and "`/household/agents/${encodeURIComponent(agent.id)}/devices`" in script
    assert 'type: "password"' in script and "Owner PIN" in script
    assert "if (state.editing) return;" in script  # the 15-second refresh never wipes a half-made choice
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script


def test_the_live_flow_shows_each_agents_app_and_its_devices():
    page = (ROOT / "porch_hub" / "dashboard" / "flow.html").read_text()
    assert "`in ${agent.client}`" in page and "may control only" in page


def test_the_side_by_side_page_is_served_and_passes_on_only_the_feed_and_pace():
    page = (ROOT / "porch_hub" / "dashboard" / "live.html").read_text()
    assert '["engine", "pace", "record"].includes(key)' in page
    assert '"/home-3d"' in page and '"/digital-twin"' in page and 'withPass("/flow")' in page
    nav = (ROOT / "porch_hub" / "dashboard" / "nav.js").read_text()
    assert 'href: "/live"' in nav and "around.openProtectionPanel(deviceIds)" in nav
    with TestClient(create_app(demo_home(simulate_latency=False).drivers, Settings())) as client:
        answer = client.get("/live")
    assert answer.status_code == 200 and "<porch-nav>" in answer.text and 'id="flow"' in answer.text
