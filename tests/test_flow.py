"""The live view of a request: porch_gateway/flow.py puts each of Porch's checks into words, the
gateway sends them on its stage feed while a request moves, and the hub's /flow page draws them.

flow.py is pure, so its wording is checked here without a running gateway. The page is checked for
what it reads and for drawing every step the gateway sends.
"""

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from porch_hub.main import Settings, create_app
from porch_hub.seed import demo_home

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("porch_flow", ROOT / "porch_gateway" / "flow.py")
flow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flow)
GATEWAY = (ROOT / "porch_gateway" / "porch_gateway.py").read_text()
PAGE = (ROOT / "porch_hub" / "dashboard" / "flow.html").read_text()
SCRIPT = PAGE.split("<script>")[-1]

DOT = {"id": "dot", "name": "Dot", "acts_for": "Yoonchan", "person": "yoonchan", "scopes": ["lighting"]}
ENERGY = {"id": "energy-agent", "name": "Energy agent", "acts_for": "the household", "reads": "energy", "scopes": []}
YOONCHAN = {"id": "yoonchan", "name": "Yoonchan", "where": "away"}
WIFE = {"id": "wife", "name": "Wife", "where": "home", "room": "living"}
LOW_RISK = ("allow", "Low-risk device within the agent's scope.")
HER_ROOM = "Wife is in the Living Room, and Yoonchan is not, so Wife's permission is needed."
ROUTER = ("Off on Living wifi needs the owner's approval. It powers the home's network: switching it off takes the "
          "whole home offline, and only someone at home can switch it back on, with the plug's own button.")


def rules(**changes):
    given = {"agent": DOT, "cap": "lighting", "rule": LOW_RISK, "decision": "allow", "why": LOW_RISK[1], "asked": [],
             "room": "Living Room", "household": True, "mine": YOONCHAN, "mine_here": False, "safe": False,
             "approval": None, **changes}
    return flow.rule_steps(**given)


def shown(steps):
    return [(s["key"], s["state"], s["short"]) for s in steps]


def test_an_allowed_request_passes_all_three_checks_in_order():
    steps = rules()
    assert [s["title"] for s in steps] == ["Who is asking", "What it may control", "Who is in the room"]
    assert shown(steps) == [("who", "ok", "Dot, for Yoonchan"), ("may", "ok", "Light · allowed"),
                            ("room", "ok", "No one else there")]
    assert steps[0]["text"] == "Porch knows Dot by its own key: it acts for Yoonchan."
    assert steps[1]["text"] == LOW_RISK[1]
    assert steps[2]["text"] == "Nobody else is in the Living Room, so no one else is asked."


@pytest.mark.parametrize("changes, short, text", [
    ({"mine_here": True}, "Yoonchan is there", "Yoonchan is in the Living Room, so no one else is asked."),
    ({"safe": True}, "Safer: never held", "Making the home safer is never held for anyone."),
    ({"household": False, "mine": None}, "Rule off", "This hub keeps no household, so no one else is asked."),
    ({"room": None}, "No one else there", "Nobody else is in the home, so no one else is asked."),
])
def test_the_room_check_says_why_no_one_else_is_asked(changes, short, text):
    room = rules(**changes)[2]
    assert (room["state"], room["short"], room["text"]) == ("ok", short, text)


def test_a_request_held_for_the_person_in_the_room_and_what_happens_to_it():
    held = rules(decision="hold", why=HER_ROOM, asked=[WIFE])
    assert shown(held)[1:] == [("may", "ok", "Light · allowed"), ("room", "wait", "Waiting for Wife")]
    assert held[2]["text"] == HER_ROOM

    said_yes = rules(decision="hold", why=HER_ROOM, asked=[WIFE], approval=("allow", "Wife approved this exact action."))
    assert said_yes[2] == {"key": "room", "title": "Who is in the room", "state": "ok", "short": "Wife said yes",
                           "text": "Wife approved this exact action.", "held": HER_ROOM}
    waiting = rules(decision="hold", why=HER_ROOM, asked=[WIFE], approval=("waiting", "Wife has not answered yet."))
    assert (waiting[2]["state"], waiting[2]["short"], waiting[2]["text"]) == ("wait", "Waiting for Wife", "Wife has not answered yet.")
    said_no = rules(decision="hold", why=HER_ROOM, asked=[WIFE], approval=("deny", "Wife declined this request."))
    assert (said_no[2]["state"], said_no[2]["short"]) == ("stop", "Refused")


def test_the_router_plug_waits_for_the_owner_and_the_room_is_not_asked_after_that():
    held = rules(cap="network", rule=("hold", ROUTER), decision="hold", why=ROUTER)
    assert shown(held)[1:] == [("may", "wait", "Waiting for the owner"), ("room", "skip", "Owner decides")]
    assert held[1]["text"] == ROUTER  # the warning reaches the page in full
    approved = rules(cap="network", rule=("hold", ROUTER), decision="hold", why=ROUTER,
                     approval=("allow", "The owner approved this exact action."))
    assert shown(approved)[1] == ("may", "ok", "The owner said yes") and approved[1]["held"] == ROUTER


def test_a_refusal_stops_at_what_it_may_control_and_the_room_is_not_reached():
    scope = "Permission: Dot is not allowed to control entry devices in this home."
    assert shown(rules(cap="entry", rule=("deny", scope), decision="deny", why=scope)) == [
        ("who", "ok", "Dot, for Yoonchan"), ("may", "stop", "Door lock · refused"), ("room", "skip", "Not reached")]
    reads_only = "Permission: Energy agent has no authority to control devices in this home; it may only read energy readings."
    energy = rules(agent=ENERGY, rule=None, decision="deny", why=reads_only)
    assert shown(energy) == [("who", "ok", "Energy agent, for the household"), ("may", "stop", "No authority: reads only"),
                             ("room", "skip", "Not reached")]
    assert energy[0]["text"].endswith("and it reads energy only.") and energy[1]["text"] == reads_only


def test_a_capability_without_a_word_is_still_named_and_an_unknown_load_by_what_it_is():
    assert rules(cap="pool_pump")[1]["short"] == "Pool pump · allowed"
    unsupported = "Living Room TV does not support 'Pause'. Supported: none."
    tv = rules(cap="unknown_load", rule=("deny", unsupported), decision="deny", why=unsupported, device_type="CastingVideoPlayer")
    assert tv[1]["short"] == "TV · refused" and tv[1]["text"] == unsupported
    assert rules(cap="unknown_load", device_type="OnOffPlug")[1]["short"] == "Plug · allowed"
    assert rules(cap="unknown_load", device_type="SomethingNew")[1]["short"] == "Device · allowed"


def test_reads_ask_no_one():
    assert shown(flow.read_steps(DOT, "All 10 devices on 4 platforms, and what Dot may do with each.")) == [
        ("who", "ok", "Dot, for Yoonchan"), ("may", "ok", "Reads what it may"), ("room", "skip", "Nobody asked")]
    assert flow.read_steps(ENERGY, "Only the energy meters.")[1]["short"] == "Energy meters only"


def test_what_the_page_is_told_about_an_agent_never_includes_its_key_or_rights():
    agent = {**DOT, "key": "private-key", "scope_label": "All devices"}
    assert flow.agent_card({**agent, "client": "ChatGPT"}) == {"id": "dot", "name": "Dot", "acts_for": "Yoonchan",
                                                             "person": "yoonchan", "reads": None, "client": "ChatGPT"}
    device = {"id": "home-1/lamp", "name": "Lamp", "room": "Office", "device_type": "OnOffLight",
              "primary_control_path": "google_home", "clusters": {"OnOff": {}}}
    assert flow.device_card(device) == {"id": "home-1/lamp", "name": "Lamp", "room": "Office", "type": "OnOffLight",
                                        "platform": "google_home"}
    assert flow.device_card(device, room="Living Room")["room"] == "Living Room"


# The gateway and the page agree

def test_every_step_the_gateway_sends_is_described_and_drawn():
    sent = set(re.findall(r'flow_event\([^,]+,\s*"(\w+)"', GATEWAY))
    assert sent == {"ask", "rules", "dry_run", "sent", "verified", "read", "approval", "error"}
    for phase in sent:
        assert f"  {phase} " in flow.__doc__, phase  # listed in flow.py's description of the feed
        assert f'case "{phase}"' in SCRIPT, phase  # drawn by the page
    # Each tool an agent has tells the page what it did.
    for tool in ("describe_home", "get_state", "get_energy", "preview_action", "request_action"):
        assert f'"{tool}"' in GATEWAY.split("# ---------- tools agents use ----------")[1], tool


def test_the_page_reads_the_home_and_both_feeds_and_draws_text_only_as_text():
    for source in ('"/devices"', '"/home/places"', '"/household"', '"/household/agents"', '"/protection"',
                   "/ws/events", "8000/ws/stage", 'params.get("engine")'):
        assert source in SCRIPT, source
    # The checks in the order Porch makes them, the same order the How Porch works page lists them.
    titles = ["Who is asking", "What it may control", "Who is in the room", "Family protection", "Digital copy",
              "Platform", "Read back"]
    positions = [SCRIPT.index(f'title: "{title}"') for title in titles]
    assert positions == sorted(positions)
    assert "innerHTML" not in SCRIPT and "insertAdjacentHTML" not in SCRIPT
    # A replay says it is one, and the keys are on the page.
    assert '"REPLAY"' in SCRIPT and "R replays the last request" in PAGE


def test_the_page_is_served_with_the_menu():
    with TestClient(create_app(demo_home(simulate_latency=False).drivers, Settings())) as client:
        response = client.get("/flow")
        assert response.status_code == 200 and "<porch-nav>" in response.text
        assert response.headers["cache-control"] == "no-store"
        assert '"/flow"' in client.get("/nav.js").text


@pytest.mark.skipif(shutil.which("node") is None, reason="runs the page's own function in Node")
def test_the_page_never_shows_a_file_path_or_a_command_meant_for_the_owners_terminal():
    """The hub's refusals end with the owner's command and its full path, the home folder's name
    included. The page is watched and recorded: it keeps the sentence and drops the rest."""
    source = re.search(r"function plain\(text\) \{.*?\n\}", SCRIPT, re.S).group(0)
    cases = {
        "Family protection is on: the control window closed at 21:27:59. The owner opens a control window with the "
        "Family protection button on any hub page, or with: /Users/someone/Desktop/x/deploy/home/owner.sh allow "
        "--device home-1/lamp":
            "Family protection is on: the control window closed at 21:27:59. The owner opens a control window with the "
            "Family protection button on any hub page",
        "Run /Users/someone/Desktop/x/deploy/home/owner.sh set-pin first": "Run owner.sh set-pin first",
        "See /home/pi/porch/deploy/home/agent.sh list": "See agent.sh list",
        "Nothing private here.": "Nothing private here.",
    }
    program = f"{source}\nconsole.log(JSON.stringify({json.dumps(list(cases))}.map(plain)));"
    shown = json.loads(subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True).stdout)
    assert shown == list(cases.values())
    # Every text the page takes from the gateway or the hub goes through it.
    assert "sentence(step.text)" in SCRIPT and "sentence(e.reason)" in SCRIPT and "sentence(e.text)" in SCRIPT
