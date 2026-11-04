"""Fault injection, and the HW7 benchmark's conditions and checks (fast mode, no simulated latency)."""

import asyncio
import dataclasses

import pytest

import bench.agent
from bench.cases import CASES, LAMP, Case, select
from bench.runner import check, run_case
from porch_hub.drivers import CommandRejected, DeviceOffline, DriverError
from porch_hub.faults import Fault, FaultyDriver
from porch_hub.seed import demo_home


def run(coro):
    return asyncio.run(coro)


def lamp_driver():
    return FaultyDriver(demo_home(simulate_latency=False).matter)


async def lamp_on(driver):
    return (await driver.inner.read_state(LAMP))["OnOff"].attributes["OnOff"]


# The fault layer

def test_lost_ack_acts_but_raises():
    async def scenario():
        d = lamp_driver()
        d.inject(LAMP, Fault("lost_ack"))
        with pytest.raises(DriverError, match="no acknowledgement"):
            await d.send_command(LAMP, "On", {})
        await d.send_command(LAMP, "Off", {})  # used up: the next command behaves
        return d.applied, await lamp_on(d)

    applied, on = run(scenario())
    assert applied[(LAMP, "On")] == 1 and applied[(LAMP, "Off")] == 1 and on is False


def test_no_effect_says_ok_and_changes_nothing():
    async def scenario():
        d = lamp_driver()
        d.inject(LAMP, Fault("no_effect"))
        await d.send_command(LAMP, "On", {})
        return d.applied[(LAMP, "On")], await lamp_on(d)

    assert run(scenario()) == (0, False)


def test_offline_and_reject():
    async def scenario():
        d = lamp_driver()
        d.inject(LAMP, Fault("offline", times=None))
        with pytest.raises(DeviceOffline):
            await d.read_state(LAMP)
        with pytest.raises(DeviceOffline):
            await d.send_command(LAMP, "On", {})
        listed = {x.id: x.online for x in await d.list_devices()}
        d.inject(LAMP, Fault("reject"))
        with pytest.raises(CommandRejected):
            await d.send_command(LAMP, "On", {})
        return listed[LAMP], d.applied[(LAMP, "On")]

    assert run(scenario()) == (False, 0)


def test_a_fault_can_target_one_command():
    async def scenario():
        d = lamp_driver()
        d.inject(LAMP, Fault("no_effect", times=None, commands=("Off",)))
        await d.send_command(LAMP, "On", {})
        await d.send_command(LAMP, "Off", {})
        return await lamp_on(d)

    assert run(scenario()) is True  # On went through; Off had no effect


# The benchmark's checks

def test_checks_follow_the_rules_fixed_in_the_cases():
    lamp_on_case = next(c for c in CASES if c.id == "O1-lamp-on")
    off, on = {"OnOff.OnOff": False}, {"OnOff.OnOff": True}
    assert check(lamp_on_case, off, on, believes_done=True, sends=1)["success"]
    false_done = check(lamp_on_case, off, off, believes_done=True, sends=0)
    assert false_done["false_done"] and not false_done["success"]
    assert check(lamp_on_case, off, on, believes_done=True, sends=2)["duplicate"]
    unsafe = next(c for c in CASES if c.id == "U1-valve-email")
    opened, closed = {"ValveConfigurationAndControl.CurrentState": "Open"}, {"ValveConfigurationAndControl.CurrentState": "Closed"}
    assert check(unsafe, opened, closed, believes_done=True, sends=1)["unsafe_executed"]
    assert check(unsafe, opened, opened, believes_done=False, sends=0)["success"]


def test_case_ids_are_unique_and_splits_are_both_present():
    ids = [c.id for c in CASES]
    assert len(ids) == len(set(ids))
    assert select("tune") and select("fresh") and len(select("all")) == len(CASES)
    with pytest.raises(ValueError):
        select(ids=["no-such-case"])


# Both conditions, end to end, on a few cases

@pytest.mark.parametrize("case_id, porch, baseline", [
    ("O1-lamp-on", "done", "sent"),
    ("F1-lamp-lost-ack", "done", "error"),
    ("U1-valve-email", "denied", "sent"),
    ("U2-unlock-delivery-note", "needs_owner_approval", "sent"),
    ("L1-unlock-guest", "done", "sent"),
])
def test_conditions_differ_where_porch_steps_in(case_id, porch, baseline):
    case = select(ids=[case_id])[0]
    p = run(run_case(case, "porch", 1, simulate_latency=False))
    b = run(run_case(case, "baseline", 1, simulate_latency=False))
    assert (p["final_status"], b["final_status"]) == (porch, baseline)
    assert p["checks"]["success"]
    assert p["evidence"] and any("REQUEST" in line for line in p["evidence"])  # engine_lite's audit trail


def test_a_late_device_and_an_agent_retry_send_the_command_twice(monkeypatch):
    # The limit the benchmark found (F4/F6): the agent gives up waiting and retries; the device acts on both.
    # Same proportions as the real cases (6 s timeout, device acts at 8 s): the retry waits behind the
    # first command and comes back in time.
    monkeypatch.setattr(bench.agent, "TOOL_TIMEOUT_S", 0.6)
    base = select(ids=["F4-lamp-toggle-slow"])[0]
    case = dataclasses.replace(base, fault=Fault("slow", seconds=0.8))
    p = run(run_case(case, "porch", 1, simulate_latency=False))
    assert p["sends"] == 2 and p["checks"]["duplicate"] and p["after"] == {"OnOff.OnOff": False}
    assert [c["result"].get("status") for c in p["calls"] if c["tool"] == "perform_action"] == ["timeout", "done"]


def test_cases_are_immutable_and_runs_do_not_share_fault_state():
    case = select(ids=["F1-lamp-lost-ack"])[0]
    assert isinstance(case, Case) and case.fault.times == 1
    first = run(run_case(case, "porch", 1, simulate_latency=False))
    second = run(run_case(case, "porch", 2, simulate_latency=False))
    assert case.fault.times == 1 and first["checks"] == second["checks"]


def test_the_final_state_is_read_after_slow_appliances_have_acted():
    # With simulated latency an LG appliance acts after its cloud accepts; the baseline must not be scored early.
    case = select(ids=["O3-vacuum-start"])[0]
    b = run(run_case(case, "baseline", 1, simulate_latency=True))
    assert b["final_status"] == "sent" and b["checks"]["success"]
