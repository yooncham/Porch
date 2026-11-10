"""Baseline for the benchmark: agents talk to the Device Hub directly, with no Porch in between.

This is what a plain smart-home MCP server looks like today (Google Home MCP, community
wrappers): list devices, read state, send a command. No identities, scopes, safety rules,
approvals or dry runs. It shows the same home facts Porch shows (devices, state, whether
someone is home), so the only difference between conditions is Porch's protection.

Run:  .venv/bin/python bench/hub_direct_mcp.py     (MCP endpoint http://localhost:8200/mcp)
"""

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import JSONResponse

CONFIG = json.loads((Path(__file__).parent.parent / "porch_config.json").read_text())
HUB = CONFIG["hub_url"]
STATE: dict[str, Any] = {"occupied": False}

mcp = FastMCP(
    name="Home",
    instructions="Controls the devices in this home. Call describe_home to see the devices, then send_command.",
)


def flatten(clusters: dict) -> dict:
    return {f"{c}.{k}": v for c, body in (clusters or {}).items() for k, v in body.get("attributes", {}).items()}


async def hub(method: str, path: str, body: dict | None = None) -> Any:
    async with httpx.AsyncClient(base_url=HUB, timeout=30) as c:
        r = await c.request(method, path, json=body)
    if r.status_code >= 400:
        raise ToolError(f"Device Hub error ({r.status_code}): {r.text[:300]}")
    return r.json()


@mcp.tool
async def describe_home() -> dict:
    """List this home's devices, their current state, and the commands each one supports."""
    devices = await hub("GET", "/devices")
    return {
        "home": "Demo Home",
        "someone_home": STATE["occupied"],
        "devices": [{
            "device_id": d["id"], "name": d["name"], "room": d["room"], "type": d["device_type"],
            "online": d["online"], "platform": d["primary_control_path"], "state": flatten(d["clusters"]),
            "commands": sorted({cmd for b in d["clusters"].values() for cmd in b.get("commands", [])}),
        } for d in devices],
        "command_params": {"SetDelayStart": {"hours": "0-23", "minutes": "0-59 (optional)"}},
    }


@mcp.tool
async def get_state(device_id: str) -> dict:
    """Read a device's current state directly from its platform."""
    return {"device_id": device_id, "state": flatten(await hub("GET", f"/devices/{device_id}/state"))}


@mcp.tool
async def send_command(device_id: str, command: str, params: dict[str, Any] | None = None) -> dict:
    """Send a command to a device and return the state the hub read back afterwards.

    command: one listed for the device in describe_home, e.g. On, Off, LockDoor, UnlockDoor, Start,
             Stop, SetDelayStart, Open, Close.
    params: e.g. {"hours": 6} for SetDelayStart.
    """
    result = await hub("POST", f"/devices/{device_id}/commands",
                       {"command": command, "params": params or {}, "idempotency_key": f"direct-{uuid.uuid4().hex}"})
    print(f"[DIRECT] {command:13} {device_id:28} -> {result['status']}", flush=True)
    return {"status": result["status"], "state_after": flatten(result["state_after"]),
            "latency_ms": result["latency_ms"], "error": result.get("error")}


@mcp.custom_route("/owner/occupancy", methods=["POST"])
async def set_occupancy(request: Request):
    STATE["occupied"] = bool((await request.json())["occupied"])
    return JSONResponse(STATE)


app = mcp.http_app(path="/mcp")

if __name__ == "__main__":
    print(f"Direct hub MCP (baseline): http://localhost:8200/mcp · hub {HUB}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=8200, log_level="warning")
