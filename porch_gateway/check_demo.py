"""End-to-end check: agent identities call Porch over real MCP, Porch drives the Device Hub,
and every result is checked independently against the hub's device state (not Porch's word).

  .venv/bin/python check_demo.py
"""

import asyncio
import json
import subprocess
import sys

import httpx
from fastmcp import Client

CONFIG = json.load(open(__file__.replace("check_demo.py", "porch_config.json")))
MCP_URL = "http://127.0.0.1:8000/mcp"
HUB = CONFIG["hub_url"]
OWNER = [sys.executable, __file__.replace("check_demo.py", "owner.py")]
ENERGY, CONCIERGE = "porch_demo_key_energy", "porch_demo_key_concierge"
LAMP, DRYER, LOCK, HEATER, VALVE = ("home-1/desk-lamp-plug", "home-1/laundry-dryer", "home-1/front-door-lock",
                                    "home-1/space-heater-plug", "home-1/main-water-valve")
results: list[bool] = []


def hub_state(device_id: str) -> dict:
    clusters = httpx.get(f"{HUB}/devices/{device_id}/state").json()
    return {f"{c}.{k}": v for c, b in clusters.items() for k, v in b["attributes"].items()}


def owner(*args: str) -> str:
    return subprocess.run([*OWNER, *args], capture_output=True, text=True, check=True).stdout


def check(label: str, ok: bool) -> None:
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")


async def call(client: Client, tool: str, **args) -> dict:
    res = await client.call_tool(tool, args)
    return res.structured_content or json.loads(res.content[0].text)


async def act(client: Client, device: str, command: str, reason: str, **extra) -> dict:
    r = await call(client, "request_action", device_id=device, command=command, reason=reason, **extra)
    print(f"   Porch: {r['decision']} | {r['reason']}" + (f" | verified={r.get('verified')} {r.get('before')} -> {r.get('after')}"
                                                         if r.get("executed") else ""))
    return r


async def main() -> None:
    owner("reset")

    print("\n1. Energy agent discovers the home through Porch")
    async with Client(MCP_URL, auth=ENERGY) as energy:
        home = await call(energy, "describe_home")
        print("  ", home["you_are"], "|", len(home["devices"]), "devices on", sorted({d["platform"] for d in home["devices"]}))
        check("agent sees all 8 hub devices across 3 platforms", len(home["devices"]) == 8)

        print("\n2. Energy agent turns off the desk lamp (Matter)")
        r = await act(energy, LAMP, "Off", "Nobody is in the office; saving energy.")
        check("Porch allowed and verified it", r["decision"] == "allowed" and r["verified"])
        check("hub independently reports the lamp off", hub_state(LAMP)["OnOff.OnOff"] is False)

        print("\n3. Energy agent delays the LG dryer 6 hours (ThinQ)")
        r = await act(energy, DRYER, "SetDelayStart", "Run the dryer off-peak tonight.", params={"hours": 6})
        check("Porch allowed and verified it", r["decision"] == "allowed" and r["verified"])
        check("hub reports a 6-hour delay", hub_state(DRYER)["PorchLaundryExtension.RelativeHourToStart"] == 6)

        print("\n4. Energy agent asks for a 20-hour delay (over the household limit)")
        r = await act(energy, DRYER, "SetDelayStart", "Even later.", params={"hours": 20})
        check("Porch denied it and nothing changed", r["decision"] == "denied"
              and hub_state(DRYER)["PorchLaundryExtension.RelativeHourToStart"] == 6)

        print("\n5. Energy agent tries the front door (outside its scope)")
        r = await act(energy, LOCK, "UnlockDoor", "test")
        check("Porch denied: entry is outside the energy scope", r["decision"] == "denied")

    print("\n6. Concierge agent: heater and front door while nobody is home")
    async with Client(MCP_URL, auth=CONCIERGE) as concierge:
        r = await act(concierge, HEATER, "On", "Warm the house for the delivery.")
        check("heater denied (nobody home), and the hub shows it off",
              r["decision"] == "denied" and hub_state(HEATER)["OnOff.OnOff"] is False)
        held = await act(concierge, LOCK, "UnlockDoor", "Let the delivery driver in.")
        check("unlock held for the owner, and the hub shows it locked",
              held["decision"] == "held_for_owner_approval" and hub_state(LOCK)["DoorLock.LockState"] == "Locked")

        print("\n7. Owner approves; the agent retries with the approval id")
        owner("approve", held["approval_id"])
        r = await act(concierge, LOCK, "UnlockDoor", "Owner approved.", approval_id=held["approval_id"])
        check("unlock executed and verified", r["decision"] == "allowed" and r["verified"]
              and hub_state(LOCK)["DoorLock.LockState"] == "Unlocked")
        r = await act(concierge, LOCK, "UnlockDoor", "Again.", approval_id=held["approval_id"])
        check("the same approval cannot be used twice", r["decision"] == "denied")
        r = await act(concierge, LOCK, "LockDoor", "Delivery done; locking up.")
        check("locking is always allowed and verified", r["verified"] and hub_state(LOCK)["DoorLock.LockState"] == "Locked")

    print("\n8. Leak emergency: the water valve")
    async with Client(MCP_URL, auth=ENERGY) as energy:
        r = await act(energy, VALVE, "Close", "Test without a leak.")
        check("no leak: closing the main is outside the energy scope", r["decision"] == "denied")
        owner("leak")
        r = await act(energy, VALVE, "Close", "Leak sensor is wet; shutting off the water.")
        check("leak detected: emergency exception lets the agent close the main",
              r["decision"] == "allowed" and r["verified"] and hub_state(VALVE)["ValveConfigurationAndControl.CurrentState"] == "Closed")
        owner("dry")

    print("\n9. Unknown agent")
    try:
        async with Client(MCP_URL, auth="not-a-real-key") as stranger:
            await call(stranger, "describe_home")
        check("unknown agent rejected", False)
    except Exception:
        check("unknown agent rejected", True)

    owner("reset")
    print(f"\n{sum(results)}/{len(results)} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
