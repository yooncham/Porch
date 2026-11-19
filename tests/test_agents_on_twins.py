"""The household's agents on the digital twin and the 3D home (porch_hub/dashboard/agents.js).

Both pages show each agent with the app it lives in and when it last called Porch, and draw every
request from the agent to Porch and from Porch to the device, from the gateway's stage feed. The
script is run in Node with a stand-in feed, so the steps a viewer sees are checked as they play.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from porch_hub.main import Settings, create_app
from porch_hub.seed import demo_home

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "porch_hub" / "dashboard" / "agents.js"
TWIN = (ROOT / "porch_hub" / "dashboard" / "digital-twin.html").read_text()
HOME_3D = (ROOT / "porch_hub" / "dashboard" / "home-3d.html").read_text()
GATEWAY = (ROOT / "porch_gateway" / "porch_gateway.py").read_text()

# Node with a stand-in browser: fetch answers the agents, the stage feed is a fake socket, and timers
# fire at once, so the steps play as fast as the test can read them.
HARNESS = r"""
const fs = require("fs");
globalThis.window = globalThis;
globalThis.location = { search: "", hostname: "127.0.0.1" };
globalThis.setInterval = () => 0;
globalThis.setTimeout = (fn) => { Promise.resolve().then(fn); return 0; };
const agents = JSON.parse(process.argv[3]);
globalThis.fetch = async (url) => ({ ok: url === "/household/agents", json: async () => agents });
let socket;
globalThis.WebSocket = class {
  constructor(url) { this.url = url; this.handlers = {}; socket = this; }
  addEventListener(type, handler) { (this.handlers[type] ||= []).push(handler); }
  emit(type, event) { (this.handlers[type] || []).forEach((handler) => handler(event)); }
};
eval(fs.readFileSync(process.argv[2], "utf8"));
const steps = [], shown = [];
PorchAgents.follow({ onAgents: (list) => shown.push(list.map((a) => a.id)),
                     onStep: (step) => steps.push({ kind: step.kind, text: step.text, sub: step.sub }) });
const tick = () => new Promise((resolve) => setImmediate(resolve));
(async () => {
  await tick();
  for (const message of JSON.parse(process.argv[4])) socket.emit("message", { data: JSON.stringify(message) });
  for (let i = 0; i < 60; i++) await tick();
  console.log(JSON.stringify({ url: socket.url, steps, shown: shown[shown.length - 1],
    plain: PorchAgents.plain("Family protection is on. The owner opens a window, or with: " +
                             "/Users/someone/Desktop/x/deploy/home/owner.sh allow --device home-1/lamp"),
    path: PorchAgents.plain("see /Users/someone/Desktop/x/deploy/home/owner.sh"),
    never: PorchAgents.ago(null), now: PorchAgents.ago(new Date().toISOString()) }));
})();
"""

AGENTS = [{"id": "dot", "name": "Dot", "client": "ChatGPT", "last_seen": None},
          {"id": "energy-agent", "name": "Energy agent", "client": "Claude Desktop", "last_seen": None},
          {"id": "wife-agent", "name": "Wife's agent", "client": None, "last_seen": None}]
DOT = {"id": "dot", "name": "Dot", "acts_for": "Yoonchan"}
ENERGY = {"id": "energy-agent", "name": "Energy agent", "acts_for": "the household"}
LAMP = {"id": "home-1/living-stand", "name": "Living stand"}
HEATER = {"id": "home-1/space-heater", "name": "Space heater"}
FEED = [
    {"type": "flow", "id": "under-way", "phase": "rules", "decision": "allow"},  # began before the page opened
    {"type": "flow", "id": "f1", "phase": "ask", "tool": "request_action", "agent": DOT, "device": LAMP,
     "command": "On", "reason": "reading light"},
    {"type": "flow", "id": "f1", "phase": "rules", "decision": "allow", "steps": [],
     "why": "Low-risk device within the agent's scope."},
    {"type": "flow", "id": "f1", "phase": "dry_run", "ok": True},
    {"type": "flow", "id": "f1", "phase": "sent", "status": "done", "path": "matter"},
    {"type": "flow", "id": "f1", "phase": "verified", "verified": True, "after": {"OnOff.OnOff": True},
     "latency_ms": 64},
    {"type": "flow", "id": "f2", "phase": "ask", "tool": "request_action", "agent": ENERGY, "device": HEATER,
     "command": "Off", "reason": "save energy"},
    {"type": "flow", "id": "f2", "phase": "rules", "decision": "deny",
     "why": "Permission: Energy agent has no authority to control devices in this home; it may only read energy readings."},
    {"type": "flow", "id": "f3", "phase": "ask", "tool": "get_energy", "agent": ENERGY},
    {"type": "flow", "id": "f3", "phase": "read", "what": "The home's energy readings.", "devices": [], "steps": []},
    {"type": "state_changed", "device_id": "home-1/living-stand"},  # not a flow message: ignored
]


@pytest.fixture(scope="module")
def played(tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("runs agents.js in Node")
    harness = tmp_path_factory.mktemp("agents") / "harness.js"
    harness.write_text(HARNESS)
    result = subprocess.run(["node", str(harness), str(SCRIPT), json.dumps(AGENTS), json.dumps(FEED)],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_each_request_plays_as_agent_to_porch_then_porch_to_the_device(played):
    assert [step["kind"] for step in played["steps"]] == ["ask", "decide", "send", "done", "ask", "decide", "ask",
                                                          "read"]
    texts = [step["text"] for step in played["steps"]]
    assert texts[0] == "Dot asks Porch to switch on Living stand."
    assert texts[1] == "Porch allows it."
    assert texts[2] == "The hub sends it through Matter, on the home network."
    assert texts[3] == "Porch read Living stand back: on. Verified in 64 ms."
    assert played["steps"][3]["sub"] == "Dot can now tell Yoonchan it is done."
    assert texts[5] == "Porch refuses it. Nothing reaches the device."
    assert played["steps"][5]["sub"].startswith("Permission: Energy agent has no authority to control devices")
    assert texts[6] == "Energy agent asks Porch for the home's energy."


@pytest.fixture(scope="module")
def played_recording(tmp_path_factory):
    """The same feed, on a page in recording mode (?record)."""
    if shutil.which("node") is None:
        pytest.skip("runs agents.js in Node")
    harness = tmp_path_factory.mktemp("agents-record") / "harness.js"
    harness.write_text(HARNESS.replace('search: ""', 'search: "?record"'))
    result = subprocess.run(["node", str(harness), str(SCRIPT), json.dumps(AGENTS), json.dumps(FEED)],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_recording_says_verified_without_the_devices_response_time(played_recording):
    """A video shows what happened; how long a device took changes as Porch improves."""
    texts = [step["text"] for step in played_recording["steps"]]
    assert texts[3] == "Porch read Living stand back: on. Verified."
    assert not any(" ms" in text for text in texts)


def test_only_agents_with_an_app_are_shown_and_the_feed_is_the_gateways(played):
    assert played["shown"] == ["dot", "energy-agent"]  # Wife's agent lives in no app yet
    assert played["url"] == "ws://127.0.0.1:8000/ws/stage"


def test_paths_and_owner_commands_never_reach_a_page_people_record(played):
    assert played["plain"] == "Family protection is on. The owner opens a window"
    assert played["path"] == "see owner.sh"
    assert (played["never"], played["now"]) == ("not connected yet", "active now")


def test_the_script_never_writes_text_from_the_hub_or_the_gateway_as_markup():
    script = SCRIPT.read_text()
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script and "outerHTML" not in script


def test_both_twins_load_the_script_and_follow_the_agents():
    assert '<script src="/agents.js" defer></script>' in TWIN and '<script src="/agents.js" defer></script>' in HOME_3D
    # The twin's own script runs before the deferred one, so it starts following once the page is parsed.
    assert ('addEventListener("DOMContentLoaded", () => window.PorchAgents && '
            'PorchAgents.follow({ onAgents: renderAgents, onStep: onAgentStep }))') in TWIN
    # Without the script (a hub started before it existed) both pages work as before.
    assert "if (window.PorchAgents) PorchAgents.follow({ onAgents: renderAgents3d, onStep: onAgentStep3d })" in HOME_3D
    for script in (TWIN.split("<script>")[-1], HOME_3D.split('<script type="module">')[-1]):
        assert "innerHTML" not in script and "insertAdjacentHTML" not in script


def test_the_hub_serves_the_script_fresh_on_every_load():
    with TestClient(create_app(demo_home(simulate_latency=False).drivers, Settings())) as client:
        response = client.get("/agents.js")
        assert response.status_code == 200 and "javascript" in response.headers["content-type"]
        assert response.headers["cache-control"] == "no-store"
        assert "window.PorchAgents" in response.text
        for page in ("/digital-twin", "/home-3d"):
            assert '<script src="/agents.js" defer></script>' in client.get(page).text


def test_the_gateway_says_which_app_each_agent_lives_in_and_when_it_last_called():
    fields = GATEWAY.split("AGENT_FIELDS = ")[1].split("\n")[0]
    assert '"client"' in fields
    assert 'LAST_SEEN[agent["id"]] = _now()' in GATEWAY  # every call an agent makes, whichever tool
    assert '"last_seen": LAST_SEEN.get(a["id"])' in GATEWAY
    assert "key" not in fields  # an agent's key is never among what the pages get


def test_the_gateway_marks_its_reading_tools_read_only_and_only_those():
    marked = GATEWAY.split("@mcp.tool(annotations=READ_ONLY)\n")[1:]
    names = sorted(part.split("def ", 1)[1].split("(", 1)[0] for part in marked)
    assert names == ["describe_home", "get_audit_log", "get_energy", "get_state", "preview_action"]
    assert "@mcp.tool\nasync def request_action(" in GATEWAY  # a change: apps ask before it
