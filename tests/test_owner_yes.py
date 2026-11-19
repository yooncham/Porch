"""The owner's yes to a held request: Porch carries it out at once, and family protection lets that
one command through, once.

An agent's request that Porch holds (the person in the room must say yes, or the owner) is answered
on any hub page with a PIN. A yes makes the gateway carry the request out right away, as the agent
that asked. The owner's yes, with the owner PIN, is the owner's own consent to that exact request,
so the hub lets its one command through family protection while it is carried out, and never
longer. Anyone else's yes does not: family protection is the owner's.
"""

import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from porch_hub import api as hub_api
from porch_hub import household as hh
from porch_hub import protection as p
from porch_hub.main import Settings, create_app
from porch_hub.seed import demo_home

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("household_rules_for_yes", ROOT / "porch_gateway" / "household_rules.py")
rules = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rules)
GATEWAY = (ROOT / "porch_gateway" / "porch_gateway.py").read_text()
NAV = (ROOT / "porch_hub" / "dashboard" / "nav.js").read_text()

LAMP, HEATER = "home-1/desk-lamp-plug", "home-1/space-heater-plug"
OWNER_PIN, WIFE_PIN = "482915", "731264"


@pytest.fixture(autouse=True)
def no_real_settings(tmp_path, monkeypatch):
    """A temporary settings folder: no test reads, or guesses against, the real home's PINs."""
    monkeypatch.setenv("PORCH_CONFIG_DIR", str(tmp_path / "hub-settings"))
    for name in ("PORCH_HOUSEHOLD_FILE", "PORCH_OWNER_PIN_FILE", "PORCH_PROTECTION_FILE"):
        monkeypatch.delenv(name, raising=False)


# ---------- family protection: one command, once, for the owner's yes ----------

def test_a_pass_lets_one_command_on_one_device_through_until_it_is_used(tmp_path):
    guard = p.FamilyProtection(tmp_path / "permission.json")
    assert guard.refusal(LAMP, "Off") is not None
    guard.allow_once(LAMP, "Off", why="the owner said yes to Concierge agent's request")
    assert guard.refusal(LAMP, "Off") is None  # the dry run and both checks before sending see it
    assert guard.refusal(LAMP, "Off") is None
    assert guard.refusal(LAMP, "On") is not None and guard.refusal(HEATER, "Off") is not None
    assert [(a["device_id"], a["command"]) for a in guard.summary()["approved_once"]] == [(LAMP, "Off")]
    guard.spend(LAMP, "Off")  # sent
    assert guard.refusal(LAMP, "Off") is not None and guard.summary()["approved_once"] == []


def test_a_pass_runs_out_by_itself(tmp_path):
    guard = p.FamilyProtection(tmp_path / "permission.json")
    guard.allow_once(LAMP, "Off", why="test", seconds=-1)
    assert guard.refusal(LAMP, "Off") is not None


def test_the_hub_sends_a_passed_command_once_and_then_protects_again(tmp_path):
    window = tmp_path / "hub-settings" / "permission.json"
    settings = Settings(confirm_timeout_s=0.3, poll_interval_s=0.02, family_protection=True, protection_path=window)
    app = create_app(demo_home(simulate_latency=False).drivers, settings)
    with TestClient(app) as client:
        app.state.hub.protection.allow_once(LAMP, "On", why="test")
        send = lambda key: client.post(f"/devices/{LAMP}/commands", json={"command": "On", "params": {}, "idempotency_key": key})  # noqa: E731
        assert client.post(f"/devices/{LAMP}/simulate", json={"command": "On"}).json()["would_succeed"] is True
        assert send("first").json()["status"] == "done"
        assert send("second").status_code == 403  # used up: protected again


# ---------- the hub's answer route: the pass is made only for the owner's own yes ----------

@pytest.fixture
def home_with_people(tmp_path, monkeypatch):
    """A hub with family protection, an owner (Yoonchan) and Wife, each with a PIN, and a gateway
    that holds one request for each kind of answer."""
    folder = tmp_path / "hub-settings"
    folder.mkdir(parents=True, exist_ok=True)
    pin = p.OwnerPin(folder / "owner-pin.json")
    home = hh.Household(folder / "household.json", pin)
    home.add("yoonchan", "Yoonchan", owner=True)
    home.add("wife", "Wife")
    p.set_pin(folder / "owner-pin.json", OWNER_PIN)
    p.set_pin(home.pin_path("wife"), WIFE_PIN)
    held = {"a-owner": {"id": "a-owner", "agent": "concierge-agent", "agent_name": "Concierge agent", "device": LAMP,
                        "command": "Off", "approvers": ["yoonchan"], "approver_names": ["Yoonchan"]},
            "a-wife": {"id": "a-wife", "agent": "dot", "agent_name": "Dot", "device": LAMP, "command": "On",
                       "approvers": ["wife"], "approver_names": ["Wife"]}}
    seen = []
    settings = Settings(confirm_timeout_s=0.3, poll_interval_s=0.02, family_protection=True,
                        protection_path=folder / "permission.json")
    app = create_app(demo_home(simulate_latency=False).drivers, settings)

    async def gateway(hub, method, path, body=None, timeout=15):
        if method == "GET":
            return list(held.values())
        approval = held[path.split("/")[3]]
        # While the gateway carries the request out, what does family protection say?
        seen.append((path, hub.protection.refusal(approval["device"], approval["command"]) is None))
        return {**approval, "status": "approved", "outcome": {"executed": True, "verified": True, "latency_ms": 5}}

    monkeypatch.setattr(hub_api, "_gateway", gateway)
    with TestClient(app) as client:
        yield client, app.state.hub, seen


def answer(client, approval_id, person, pin, verdict="approve"):
    return client.post(f"/household/approvals/{approval_id}/{verdict}", json={"person": person, "pin": pin})


def test_the_owners_yes_lets_that_one_command_through_while_it_is_carried_out(home_with_people):
    client, hub, seen = home_with_people
    response = answer(client, "a-owner", "yoonchan", OWNER_PIN)
    assert response.status_code == 200 and response.json()["outcome"]["executed"] is True
    assert seen == [("/household/approvals/a-owner/approve", True)]
    assert hub.protection.refusal(LAMP, "Off") is not None  # and not a moment longer


def test_a_wrong_owner_pin_reaches_nothing(home_with_people):
    client, hub, seen = home_with_people
    assert answer(client, "a-owner", "yoonchan", "000000").status_code == 401
    assert seen == [] and hub.protection.refusal(LAMP, "Off") is not None


def test_someone_elses_yes_does_not_open_family_protection(home_with_people):
    client, _hub, seen = home_with_people
    assert answer(client, "a-wife", "wife", WIFE_PIN).status_code == 200  # the gateway checks her PIN
    assert seen == [("/household/approvals/a-wife/approve", False)]


def test_the_owner_answering_a_request_that_waits_for_wife_opens_nothing(home_with_people):
    client, _hub, seen = home_with_people
    answer(client, "a-wife", "yoonchan", OWNER_PIN)  # the gateway refuses it; the hub made no pass
    assert seen == [("/household/approvals/a-wife/approve", False)]


def test_a_no_never_opens_anything(home_with_people):
    client, _hub, seen = home_with_people
    answer(client, "a-owner", "yoonchan", OWNER_PIN, verdict="decline")
    assert seen == [("/household/approvals/a-owner/decline", False)]


# ---------- the gateway: a yes is carried out at once; a retry only reports ----------

def test_the_gateway_carries_a_request_out_when_it_is_approved():
    for route in ('@mcp.custom_route("/household/approvals/{approval_id}/{verdict}"',
                  '@mcp.custom_route("/owner/approvals/{approval_id}/{verdict}"'):
        handler = GATEWAY.split(route, 1)[1].split("@mcp.custom_route", 1)[0]
        assert "outcome = await carry_out(a) if approved else None" in handler
    carry = GATEWAY.split("async def carry_out(", 1)[1].split("\ndef ", 1)[0]
    assert "await _request_action(agent, device," in carry and 'a["carried_out"], a["result"] = True, result' in carry
    request = GATEWAY.split("async def _request_action(", 1)[1].split("\n    ctx = ", 1)[0]
    assert 'done.get("carried_out")' in request and '"already_carried_out"' in request


def test_a_request_family_protection_stops_waits_for_the_owner_instead_of_failing():
    request = GATEWAY.split("async def _request_action(", 1)[1].split("\n@mcp.tool", 1)[0]
    stopped = request.index('str(result.get("error") or "").startswith("Family protection")')
    assert "and not _owner_said_yes(approval_id)" in request[stopped:stopped + 200]  # never a second loop
    assert "return await _hold_for_protection(" in request
    hold = GATEWAY.split("async def _hold_for_protection(", 1)[1].split("\n@mcp.tool", 1)[0]
    assert '"approvers": [], "approver_names": []' in hold and '"family_protection": True' in hold
    assert "_pending_same(agent[\"id\"], device_id, command, params)" in hold  # asked again: the same request
    # An approval from family protection counts only while Porch's rules still allow the request.
    assert 'or (decision == "allow"\n' in request and '.get("family_protection")))' in request
    for route in ('@mcp.custom_route("/household/approvals/{approval_id}/{verdict}"',
                  '@mcp.custom_route("/owner/approvals/{approval_id}/{verdict}"'):
        assert 'a["owner_answered"] = ' in GATEWAY.split(route, 1)[1].split("@mcp.custom_route", 1)[0]


def test_a_household_agent_is_described_as_acting_for_everyone():
    people = [{"id": "yoonchan", "name": "Yoonchan", "where": "home", "room": "living"}]
    concierge = {"id": "concierge-agent", "name": "Concierge agent", "acts_for": "the household", "scopes": []}
    asked = rules.must_ask(concierge, "living", people)
    assert [x["id"] for x in asked] == ["yoonchan"]
    assert rules.why_ask(concierge, "Living / Dining", asked, people) == (
        "Yoonchan is in the Living / Dining, and Concierge agent acts for the whole household, "
        "so Yoonchan's permission is needed.")


# ---------- every page shows what waits, and answers it ----------

def test_every_page_shows_requests_waiting_and_answers_them_as_text_only():
    assert 'fetch("/household/approvals", { cache: "no-store" })' in NAV
    assert "`/household/approvals/${encodeURIComponent(a.id)}/${verdict}`" in NAV
    assert "REQUEST WAITING FOR A YES" in NAV
    build = NAV.split("    buildApprovals() {", 1)[1].split("    toggle(open) {", 1)[0]
    assert "innerHTML" not in build and "insertAdjacentHTML" not in build
    assert 'pin.value = "";' in build  # the PIN is never left in the page
