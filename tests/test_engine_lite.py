"""The HW6 stand-in Engine against the real hub app (in process, simulated devices)."""

import asyncio
import io
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from engine_lite.audit import Audit
from engine_lite.engine import EngineLite
from engine_lite.policy import SCOPES, Verdict, decide
from engine_lite.server import build_server
from engine_lite.walkthrough import walkthrough
from porch_hub.main import Settings, create_app
from porch_hub.seed import demo_home

DRYER, LOCK, VALVE, LAMP = "home-1/laundry-dryer", "home-1/front-door-lock", "home-1/main-water-valve", "home-1/desk-lamp-plug"


# Policy

@pytest.mark.parametrize("scope, device_type, command, verdict", [
    ("energy", "LaundryDryer", "SetDelayStart", Verdict.ALLOW),
    ("energy", "OnOffPlug", "On", Verdict.ALLOW),
    ("energy", "DoorLock", "LockDoor", Verdict.DENY),
    ("energy", "WaterValve", "Close", Verdict.DENY),
    ("energy", "DoorLock", "UnlockDoor", Verdict.NEEDS_OWNER_APPROVAL),
    ("full_home", "DoorLock", "LockDoor", Verdict.ALLOW),
    ("full_home", "WaterValve", "Close", Verdict.ALLOW),
    ("full_home", "DoorLock", "UnlockDoor", Verdict.NEEDS_OWNER_APPROVAL),
])
def test_policy(scope, device_type, command, verdict):
    assert decide(SCOPES[scope], device_type, command).verdict == verdict


# Engine against the hub

@asynccontextmanager
async def engine_for(scope="energy"):
    home = demo_home(simulate_latency=False)
    app = create_app(home.drivers, Settings(confirm_timeout_s=0.3))
    async with app.router.lifespan_context(app):
        hub = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub")
        stream = io.StringIO()
        engine = EngineLite(hub, "Claude Code", SCOPES[scope], Audit(stream=stream))
        yield engine, home, stream
        await hub.aclose()


def run(scenario):
    return asyncio.run(scenario())


def test_list_devices_says_what_the_agent_may_do():
    async def scenario():
        async with engine_for() as (engine, _, _):
            return await engine.list_devices()

    listing = run(scenario)
    devices = {d["device_id"]: d for d in listing["devices"]}
    assert listing["scope"] == "Energy Management Only"
    assert "SetDelayStart" in devices[DRYER]["you_may"]
    assert devices[LOCK]["you_may"] == [] and devices[LOCK]["needs_owner_approval"] == ["UnlockDoor"]
    assert devices[LOCK]["state"] == {"DoorLock.LockState": "Locked"}


def test_an_allowed_action_is_dry_run_executed_and_confirmed():
    async def scenario():
        async with engine_for() as (engine, home, stream):
            result = await engine.perform_action(DRYER, "SetDelayStart", {"hours": 4}, "move to off-peak")
            physical = await home.thinq.read_state(DRYER)
            return result, physical, stream.getvalue()

    result, physical, audit = run(scenario)
    assert result["status"] == "done"
    assert result["confirmed_state"]["PorchLaundryExtension.RelativeHourToStart"] == 4
    assert physical["PorchLaundryExtension"].attributes["RelativeHourToStart"] == 4  # the device really changed
    assert isinstance(result["latency_ms"], int) and result["idempotency_key"].startswith("engine-lite-")
    for kind in ("REQUEST", "ALLOW", "DRY RUN", "DONE"):
        assert kind in audit
    assert "read-back confirmed" in audit


def test_out_of_scope_is_denied_and_never_sent():
    async def scenario():
        async with engine_for() as (engine, home, stream):
            result = await engine.perform_action(VALVE, "Close", {}, "save water")
            physical = await home.matter.read_state(VALVE)
            return result, physical, stream.getvalue()

    result, physical, audit = run(scenario)
    assert result["status"] == "denied" and "does not cover WaterValve" in result["reason"]
    assert physical["ValveConfigurationAndControl"].attributes["CurrentState"] == "Open"
    assert "DENY" in audit and "DONE" not in audit


def test_unlock_waits_for_the_owner_and_each_approval_covers_one_action():
    async def scenario():
        async with engine_for() as (engine, home, stream):
            held = await engine.perform_action(LOCK, "UnlockDoor", {}, "delivery note says to")
            locked = (await home.matter.read_state(LOCK))["DoorLock"].attributes["LockState"]
            early = await engine.perform_action(LOCK, "UnlockDoor", {}, "retry", approval_id=held["approval_id"])
            pending = engine.approvals()
            await engine.answer(held["approval_id"], approved=True)
            other = await engine.perform_action(DRYER, "Start", {}, "sneaky", approval_id=held["approval_id"])
            done = await engine.perform_action(LOCK, "UnlockDoor", {}, "owner approved", approval_id=held["approval_id"])
            reused = await engine.perform_action(LOCK, "UnlockDoor", {}, "again", approval_id=held["approval_id"])
            unlocked = (await home.matter.read_state(LOCK))["DoorLock"].attributes["LockState"]
            return held, locked, early, pending, other, done, reused, unlocked, stream.getvalue()

    held, locked, early, pending, other, done, reused, unlocked, audit = run(scenario)
    assert held["status"] == "needs_owner_approval" and locked == "Locked"
    assert early["status"] == "waiting_for_owner"
    assert [p["approval_id"] for p in pending] == [held["approval_id"]]
    assert other["status"] == "done"  # allowed by scope on its own; it did not use up the approval
    assert done["status"] == "done" and done["confirmed_state"]["DoorLock.LockState"] == "Unlocked"
    assert reused["status"] == "denied" and unlocked == "Unlocked"
    assert "HOLD" in audit and "APPROVED" in audit


def test_an_approval_cannot_be_spent_on_a_different_action():
    async def scenario():
        async with engine_for() as (engine, home, _):
            held = await engine.perform_action(LOCK, "UnlockDoor", {}, "guest arriving")
            await engine.answer(held["approval_id"], approved=True)
            altered = await engine.perform_action(LOCK, "UnlockDoor", {"duration": 999}, "x",
                                                  approval_id=held["approval_id"])
            still_locked = (await home.matter.read_state(LOCK))["DoorLock"].attributes["LockState"]
            exact = await engine.perform_action(LOCK, "UnlockDoor", {}, "guest arriving", approval_id=held["approval_id"])
            return altered, still_locked, exact

    altered, still_locked, exact = run(scenario)
    assert altered["status"] == "denied" and "different action" in altered["reason"]
    assert still_locked == "Locked"
    assert exact["status"] == "done"  # the approval still covers exactly what the owner saw


def test_an_owner_denial_is_final():
    async def scenario():
        async with engine_for() as (engine, home, _):
            held = await engine.perform_action(LOCK, "UnlockDoor", {}, "x")
            await engine.answer(held["approval_id"], approved=False)
            retry = await engine.perform_action(LOCK, "UnlockDoor", {}, "x", approval_id=held["approval_id"])
            return retry, (await home.matter.read_state(LOCK))["DoorLock"].attributes["LockState"]

    retry, state = run(scenario)
    assert retry["status"] == "denied" and state == "Locked"


def test_a_dry_run_that_would_fail_is_never_sent():
    async def scenario():
        async with engine_for() as (engine, home, stream):
            result = await engine.perform_action(DRYER, "SetDelayStart", {"hours": 20}, "very late")
            return result, home.thinq_cloud.controls, stream.getvalue()

    result, controls, audit = run(scenario)
    assert result["status"] == "would_fail" and "from 0 to 19, not 20" in result["reason"]
    assert controls == [] and "REFUSED" in audit


def test_preview_touches_nothing():
    async def scenario():
        async with engine_for() as (engine, home, _):
            preview = await engine.preview_action(LAMP, "On", {})
            return preview, (await home.matter.read_state(LAMP))["OnOff"].attributes["OnOff"]

    preview, on = run(scenario)
    assert (preview["policy"], preview["would_succeed"], preview["predicted_state"]["OnOff.OnOff"]) == ("allow", True, True)
    assert on is False


# The MCP surface

def test_the_agent_sees_three_tools_and_readable_results():
    async def scenario():
        async with engine_for() as (engine, _, _):
            server = build_server(engine)
            tools = {t.name for t in await server.list_tools()}
            result = await server.call_tool("perform_action", {"device_id": LAMP, "command": "On", "reason": "reading light"})
            missing = await server.call_tool("perform_action", {"device_id": "nope", "command": "On", "reason": "x"})
            return tools, json.loads(result.content[0].text), json.loads(missing.content[0].text)

    tools, result, missing = run(scenario)
    assert tools == {"list_devices", "preview_action", "perform_action"}
    assert result["status"] == "done" and result["confirmed_state"]["OnOff.OnOff"] is True
    assert missing["status"] == "error" and "call list_devices" in missing["error"]


# The walkthrough teammates run to see the MVP

@pytest.mark.parametrize("owner, lock_after", [("approve", "Unlocked"), ("deny", "Locked")])
def test_the_walkthrough_runs_every_scene_through_mcp(owner, lock_after):
    async def scenario():
        async with engine_for() as (engine, home, _):
            server = build_server(engine)

            async def call(tool, args):
                return json.loads((await server.call_tool(tool, args)).content[0].text)

            lines = []
            results = await walkthrough(call, engine.answer, owner, out=lines.append)
            lock = (await home.matter.read_state(LOCK))["DoorLock"].attributes["LockState"]
            delay = (await home.thinq.read_state(DRYER))["PorchLaundryExtension"].attributes["RelativeHourToStart"]
            return [r.get("status") for r in results], lock, delay, "\n".join(lines)

    statuses, lock, delay, text = run(scenario)
    retry = "done" if owner == "approve" else "denied"
    assert statuses == [None, None, "done", "would_fail", "denied", "needs_owner_approval", retry]
    assert lock == lock_after and delay == 4
    assert "not an AI" in text and f"owner {'approved' if owner == 'approve' else 'denied'}" in text
