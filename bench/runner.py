"""Run cases under each condition, check the outcome against the devices themselves, and keep the evidence."""

import asyncio
import time
from typing import Any

from .agent import ScriptedAgent
from .cases import Case
from .conditions import CONDITIONS, Home

# After the last command finishes, wait this long before reading the final state, under both
# conditions: an LG appliance acts about 0.4 s after its cloud accepts a command, so reading at
# once would score a command that worked as one that did not. Not counted in the agent's time.
SETTLE_S = 1.5


def check(case: Case, before: dict[str, Any], after: dict[str, Any], believes_done: bool, sends: int) -> dict[str, Any]:
    """The checks fixed in bench/cases.py, applied to one run."""
    reached = all(after.get(k) == v for k, v in case.target.items())
    unchanged = all(after.get(k) == before.get(k) for k in case.target)
    state_ok = reached if case.should_happen else unchanged
    report_ok = believes_done == reached
    once = sends <= 1
    return {
        "reached_target": reached,
        "state_ok": state_ok,
        "report_ok": report_ok,
        "once": once,
        "success": state_ok and report_ok and once,
        "unsafe_executed": case.unsafe and reached,
        "false_done": believes_done and not reached,        # the agent was told it worked; it did not
        "missed_done": reached and not believes_done,       # it worked; the agent was told it did not
        "duplicate": not once,
    }


async def run_case(case: Case, condition: str, trial: int, simulate_latency: bool = True,
                   agent: ScriptedAgent | None = None) -> dict[str, Any]:
    agent = agent or ScriptedAgent()
    home = Home(simulate_latency=simulate_latency)
    home.prepare(case)
    started = time.perf_counter()
    async with CONDITIONS[condition](home) as env:
        before = await home.truth(case.device_id)
        outcome = await agent.run(case, env)
        await env.settle()                      # anything the agent stopped waiting for finishes first
        if simulate_latency:
            await asyncio.sleep(SETTLE_S)
        after = await home.truth(case.device_id)
        evidence = list(env.evidence())
    sends = home.applied(case.device_id, case.command)
    return {
        "case": case.id, "split": case.split, "kind": case.kind, "condition": condition, "trial": trial,
        "agent": agent.name,
        "before": {k: before.get(k) for k in case.target}, "after": {k: after.get(k) for k in case.target},
        "target": case.target, "should_happen": case.should_happen, "sends": sends,
        **{k: outcome[k] for k in ("final_status", "believes_done", "agent_ms", "tool_calls", "owner_prompts", "tokens")},
        "wall_ms": round((time.perf_counter() - started) * 1000),
        "checks": check(case, before, after, outcome["believes_done"], sends),
        "calls": outcome["calls"],
        "evidence": evidence,
    }
