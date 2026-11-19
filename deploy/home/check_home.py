"""Check a real home end to end, the way an agent reaches it: gateway -> hub -> device.

    deploy/home/check.sh                       reads only
    deploy/home/check.sh home-1/entry-light    also switches that one device and puts it back

Reading: the hub lists the home's devices and which are online, the gateway shows what each
agent may do with each device, and a wrong agent key is refused. Nothing is sent to a device.

With a device id: an agent that may switch the device sends On and Off through Porch's
request_action. Porch must allow each one and verify it against the device, and the device
ends in the state it started in. If no agent may switch it without the owner, or family
protection has no control window open for it, nothing is sent.

Run it through check.sh, which loads this home's settings. deploy/home/start.sh must be running.
"""

import asyncio
import json
import os
import sys

import httpx
from fastmcp import Client

CONFIG_PATH = os.path.expanduser(os.environ.get("PORCH_GATEWAY_CONFIG", ""))
MCP_URL = os.environ.get("PORCH_GATEWAY_URL", "http://127.0.0.1:8000/mcp")
PAUSE_S = 2.0  # between the two switches, so you can watch the device change
REASON = "deploy/home/check.sh: checking this home end to end"

results: list[bool] = []


def check(label: str, ok: bool) -> bool:
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    return ok


def stop(message: str) -> None:
    print(f"\nStopped: {message}", file=sys.stderr)
    sys.exit(1)


def table(header: list[str], rows: list[list[str]]) -> None:
    widths = [max(len(str(cell)) for cell in column) for column in zip(header, *rows)]
    for row in [header, *rows]:
        print("  " + "  ".join(str(cell).ljust(w) for cell, w in zip(row, widths)).rstrip())


async def call(client: Client, tool: str, **args) -> dict:
    res = await client.call_tool(tool, args)
    return res.structured_content or json.loads(res.content[0].text)


def access_summary(access: dict[str, str]) -> str:
    """What one agent may do with one device, from describe_home's your_access."""
    allowed = [cmd for cmd, verdict in access.items() if verdict == "allowed"]
    held = [cmd for cmd, verdict in access.items() if verdict == "needs owner approval"]
    parts = []
    if allowed:
        parts.append(", ".join(allowed))
    if held:
        parts.append(f"owner approval: {', '.join(held)}")
    return "; ".join(parts) or "nothing"


async def main(device_id: str | None) -> None:
    if not CONFIG_PATH or not os.path.exists(CONFIG_PATH):
        stop("PORCH_GATEWAY_CONFIG does not point to this home's config. Run deploy/home/check.sh, not this file.")
    config = json.load(open(CONFIG_PATH))
    hub_url = config["hub_url"]

    print(f"1. The hub at {hub_url}")
    try:
        hub_devices = httpx.get(f"{hub_url}/devices", timeout=30).json()
    except httpx.TransportError:
        stop(f"the hub does not answer at {hub_url}. Is deploy/home/start.sh running in another tab?")
    table(["device id", "type", "room", "online", "path"],
          [[d["id"], d["device_type"], d["room"], "yes" if d["online"] else "NO", d["primary_control_path"]]
           for d in hub_devices])
    check(f"the hub lists {len(hub_devices)} device(s)", bool(hub_devices))
    offline = [d["id"] for d in hub_devices if not d["online"]]
    check("every device is online" + (f" (offline: {', '.join(offline)})" if offline else ""), not offline)

    print(f"\n2. The gateway at {MCP_URL}, as each agent sees the home")
    seen: dict[str, dict] = {}  # agent key -> describe_home
    try:
        for key in config["agents"]:
            async with Client(MCP_URL, auth=key) as client:
                seen[key] = await call(client, "describe_home")
    except Exception as exc:  # the MCP client raises its own error types
        stop(f"the gateway does not answer at {MCP_URL} ({exc!r}). Is deploy/home/start.sh running?")
    names = {key: agent["name"] for key, agent in config["agents"].items()}
    first = next(iter(seen.values()))
    by_agent = {key: {d["device_id"]: d for d in home["devices"]} for key, home in seen.items()}
    table(["device id", "capability", *names.values()],
          [[d["device_id"], d["capability"], *(access_summary(by_agent[key][d["device_id"]]["your_access"])
                                               for key in names)] for d in first["devices"]])
    check("the gateway shows the same devices as the hub",
          sorted(d["device_id"] for d in first["devices"]) == sorted(d["id"] for d in hub_devices))
    check(f"the gateway says the hardware is {first['hardware']}", first["hardware"] != "simulated")
    try:
        async with Client(MCP_URL, auth="not-a-real-key") as stranger:
            await call(stranger, "describe_home")
        check("an unknown agent key is refused", False)
    except Exception:
        check("an unknown agent key is refused", True)

    if device_id is None:
        print("\nRead only: nothing was sent to a device. To switch one device and put it back:")
        print("  deploy/home/check.sh <device id>")
    else:
        await switch(device_id, names, by_agent, hub_url)

    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


async def switch(device_id: str, names: dict[str, str], by_agent: dict[str, dict[str, dict]], hub_url: str) -> None:
    print(f"\n3. Switching {device_id} through Porch")
    views = {key: devices.get(device_id) for key, devices in by_agent.items()}
    if not all(views.values()):
        stop(f"there is no device {device_id!r} in this home. The ids are in the first table.")
    state = next(iter(views.values()))["state"]
    if not isinstance(state.get("OnOff.OnOff"), bool):
        stop(f"{device_id} does not report On/Off, so there is nothing to switch.")
    key = next((k for k, d in views.items()
                if d["your_access"].get("On") == "allowed" and d["your_access"].get("Off") == "allowed"), None)
    if key is None:
        print("  No agent may switch this device on its own, so nothing was sent:")
        for k, d in views.items():
            print(f"    {names[k]}: On is {d['your_access'].get('On', 'not a command')}")
        print("  That is Porch's rule at work. To let agents use a plug, say what it powers under")
        print(f"  device_capabilities in {CONFIG_PATH}, then restart deploy/home/start.sh.")
        return

    try:
        protection = httpx.get(f"{hub_url}/protection", timeout=30).json()
    except (httpx.HTTPError, ValueError):
        protection = {}  # a hub from before family protection
    if protection.get("enforced") and not (protection.get("live_writes_enabled")
                                           and device_id in protection.get("device_ids", [])):
        print("  Family protection is on and no control window is open for this device, so nothing was sent.")
        print("  Check who is near it, open a window, then run this again:")
        print(f"    {os.environ.get('PORCH_OWNER_COMMAND') or 'deploy/home/owner.sh'} allow --device {device_id}")
        return

    started_on = state["OnOff.OnOff"]
    print(f"  It is {'on' if started_on else 'off'} now. {names[key]} switches it "
          f"{'off, then on' if started_on else 'on, then off'}. Watch the device.")
    async with Client(MCP_URL, auth=key) as client:
        for step, command in enumerate(["Off", "On"] if started_on else ["On", "Off"]):
            if step:
                await asyncio.sleep(PAUSE_S)
            r = await call(client, "request_action", device_id=device_id, command=command, reason=REASON)
            detail = (f"{r.get('latency_ms')} ms via {r.get('control_path')}" if r.get("executed")
                      else r.get("error") or r.get("reason"))
            check(f"{command}: Porch allowed it and the device confirmed it ({detail})",
                  r["decision"] == "allowed" and bool(r.get("executed")) and bool(r.get("verified")))
            hub_state = httpx.get(f"{hub_url}/devices/{device_id}/state", timeout=30).json()
            check(f"{command}: the hub reads the device as {'on' if command == 'On' else 'off'}",
                  hub_state["OnOff"]["attributes"]["OnOff"] is (command == "On"))
        final = (await call(client, "get_state", device_id=device_id))["state"].get("OnOff.OnOff")
    check(f"the device is back {'on' if started_on else 'off'}, as it started", final is started_on)


if __name__ == "__main__":
    if len(sys.argv) > 2 or (len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help")):
        print(__doc__)
        sys.exit(0 if len(sys.argv) == 2 else 2)
    asyncio.run(main(sys.argv[1] if len(sys.argv) == 2 else None))
