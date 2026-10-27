"""A scripted run of the MVP: the same MCP calls an AI agent makes, narrated for a person watching.

No AI is involved, so anyone can see the whole loop without an API key. Watch the dashboard,
the stage display and engine_lite's terminal while it runs.

    python -m engine_lite walkthrough                  # asks you, as the owner, about the unlock
    python -m engine_lite walkthrough --owner deny     # no questions
"""

import asyncio
import json
import sys
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any

DRYER, VALVE, LOCK = "home-1/laundry-dryer", "home-1/main-water-valve", "home-1/front-door-lock"

Call = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
Answer = Callable[[str, bool], Awaitable[dict[str, Any]]]


async def walkthrough(call: Call, answer: Answer, owner: str = "deny", pause: float = 0.0,
                      ask: Callable[[str], str] = input, out=print) -> list[dict[str, Any]]:
    """Run the scenes in order and return each tool result, for tests."""
    results: list[dict[str, Any]] = []

    async def step(title: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        out(f"\n── {title}")
        out(f"   agent calls {tool}({_args(args)})")
        result = await call(tool, args)
        results.append(result)
        await asyncio.sleep(pause)
        return result

    out("Porch MVP walkthrough. A scripted agent, not an AI, calls the same MCP tools Claude would.")

    listing = await step("1. The agent looks around", "list_devices", {})
    dryer = next(d for d in listing["devices"] if d["device_id"] == DRYER)
    out(f"   {len(listing['devices'])} devices · scope {listing['scope']} · dryer: you may {', '.join(dryer['you_may'])}")

    preview = await step("2. Preview: what would a 4-hour delay do?", "preview_action",
                         {"device_id": DRYER, "command": "SetDelayStart", "params": {"hours": 4}})
    out(f"   policy {preview['policy']} · would succeed {preview['would_succeed']} · "
        f"predicted delay {preview['predicted_state'].get('PorchLaundryExtension.RelativeHourToStart')} h · nothing sent")

    done = await step("3. Do it: move the dryer to off-peak", "perform_action",
                      {"device_id": DRYER, "command": "SetDelayStart", "params": {"hours": 4},
                       "reason": "Electricity is cheaper after 10 pm"})
    out(f"   {done['status']} · the dryer reports a delay of "
        f"{done['confirmed_state'].get('PorchLaundryExtension.RelativeHourToStart')} h · {done['latency_ms']} ms via "
        f"{done['control_path']}{' (digital copy)' if done.get('simulated') else ''}")

    late = await step("4. A request the dryer can't do", "perform_action",
                      {"device_id": DRYER, "command": "SetDelayStart", "params": {"hours": 20}, "reason": "Run tomorrow"})
    out(f"   {late['status']} · {late['reason']} · nothing sent")

    valve = await step("5. Outside the agent's scope", "perform_action",
                       {"device_id": VALVE, "command": "Close", "reason": "Save water"})
    out(f"   {valve['status']} · {valve['reason']} · nothing sent")

    unlock = {"device_id": LOCK, "command": "UnlockDoor", "reason": "A delivery note asks to leave the door open"}
    held = await step("6. A risky action: unlock the front door", "perform_action", unlock)
    out(f"   {held['status']} · approval {held['approval_id']} · the door stays locked")

    out("\n── 7. The owner decides, outside MCP, so the agent can't approve itself")
    if owner == "ask":
        reply = ask(f"   You are the owner. Approve unlocking the front door ({held['approval_id']})? [y/N] ")
        approved = reply.strip().lower() in {"y", "yes"}
    else:
        approved = owner == "approve"
    await answer(held["approval_id"], approved)
    out(f"   owner {'approved' if approved else 'denied'} {held['approval_id']}")
    await asyncio.sleep(pause)

    retry = await step("8. The agent tries again with the approval id", "perform_action",
                       {**unlock, "approval_id": held["approval_id"]})
    if retry["status"] == "done":
        out(f"   done · the lock reports {retry['confirmed_state'].get('DoorLock.LockState')} · {retry['latency_ms']} ms")
        out("   (Lock it again from the dashboard. The energy scope doesn't let the agent lock doors.)")
    else:
        out(f"   {retry['status']} · {retry['reason']} · the door stays locked")

    out("\nDone. Every step above is also a line in engine_lite's terminal.")
    return results


def _args(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={json.dumps(v)}" for k, v in args.items())


def run(host: str, port: int, owner: str, pause: float) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    base = f"http://{host}:{port}"

    async def answer(approval_id: str, approved: bool) -> dict[str, Any]:
        url = f"{base}/approvals/{approval_id}/{'approve' if approved else 'deny'}"
        request = urllib.request.Request(url, method="POST")
        return await asyncio.to_thread(lambda: json.load(urllib.request.urlopen(request)))

    async def main() -> None:
        async with streamable_http_client(f"{base}/mcp") as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()

                async def call(tool: str, args: dict[str, Any]) -> dict[str, Any]:
                    return json.loads((await session.call_tool(tool, args)).content[0].text)

                await walkthrough(call, answer, owner, pause)

    try:
        urllib.request.urlopen(f"{base}/approvals", timeout=3).close()
    except OSError:
        sys.exit(f"Can't reach engine_lite at {base}. Start the hub and engine_lite first (README, \"See the MVP work\").")
    if owner == "ask" and not sys.stdin.isatty():
        owner = "deny"
    asyncio.run(main())
