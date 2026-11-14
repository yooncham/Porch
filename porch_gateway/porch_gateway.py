"""Porch: a safety gateway that lets any AI agent act in a connected home.

Agents connect over MCP (streamable HTTP) with their own key. Every request is
checked against that agent's permissions and the home's safety rules, dry-run on
the Device Hub's digital copy, executed through the hub, and verified twice: the
hub's own read-back, then Porch's independent read of the device state.
Every decision is written to the audit log and pushed to the stage display.

Below Porch sits the Porch Device Hub (Interface v0), which drives LG ThinQ,
Matter and Google Home, or simulated copies of them.

Run:  .venv/bin/python porch_gateway.py
      MCP endpoint http://localhost:8000/mcp · stage feed ws://localhost:8000/ws/stage
"""

import asyncio
import json
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

ROOT = Path(__file__).parent
# PORCH_GATEWAY_CONFIG: a home's own config file, so a real home never edits the demo's copy here.
CONFIG_PATH = Path(os.environ.get("PORCH_GATEWAY_CONFIG") or ROOT / "porch_config.json").expanduser()
CONFIG = json.loads(CONFIG_PATH.read_text())
AUDIT_FILE = ROOT / "audit.jsonl"
HUB = CONFIG["hub_url"]
MAX_DELAY_H = CONFIG["limits"]["max_delay_start_hours"]
APPROVAL_TTL_S = CONFIG["limits"]["approval_ttl_seconds"]

# Shared capability vocabulary: rules are written per capability, never per brand.
# A plug says nothing about what it powers, so plugs are mapped per device in the config,
# and an unmapped plug is treated as needing a human.
CAPABILITY_BY_TYPE = {
    "OnOffLight": "lighting",
    "LaundryDryer": "appliance",
    "LaundryWasher": "appliance",
    "RoboticVacuumCleaner": "appliance",
    "AirPurifier": "appliance",
    "DoorLock": "entry",
    "WaterValve": "water_shutoff",
    "WaterLeakSensor": "sensor",
    "OnOffPlug": "unknown_load",
}
RISK_TIER = {
    "lighting": "low_risk",
    "appliance": "optimize_within_limits",
    "heating": "needs_human",
    "unknown_load": "needs_human",
    "entry": "never_autonomous",
    "water_shutoff": "never_autonomous",
    "sensor": "read_only",
}
# Commands that move a device toward its safe state are always allowed within scope.
SAFE_DIRECTION = {("heating", "Off"), ("unknown_load", "Off"), ("entry", "LockDoor")}

STATE: dict[str, Any] = {"occupied": False}
AUDIT: list[dict] = []
APPROVALS: dict[str, dict] = {}
STAGE_SOCKETS: set[WebSocket] = set()

mcp = FastMCP(
    name="Porch",
    instructions=(
        "Porch is the safety gateway for this home. Call describe_home first to see the devices and "
        "what you are allowed to do. Use request_action for every change and always give a short reason. "
        "Answer the user from the verified state Porch returns, not from what you expected. "
        "If Porch denies a request, do not retry it; tell the user why. If it is held for the owner, "
        "tell the user; once the owner approves, call request_action again with the approval_id."
    ),
)


# ---------- helpers ----------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_agent() -> dict:
    headers = get_http_headers(include={"authorization"})
    token = headers.get("authorization", "").removeprefix("Bearer ").strip()
    agent = CONFIG["agents"].get(token)
    if not agent:
        raise ToolError("Unknown agent key. Ask the household owner to connect you to Porch.")
    return agent


def capability(device: dict) -> str:
    return CONFIG["device_capabilities"].get(device["id"]) or CAPABILITY_BY_TYPE.get(device["device_type"], "unknown_load")


def flatten(clusters: dict) -> dict:
    """{"DoorLock": {"attributes": {"LockState": "Locked"}}} -> {"DoorLock.LockState": "Locked"}."""
    return {f"{c}.{k}": v for c, body in (clusters or {}).items() for k, v in body.get("attributes", {}).items()}


def commands_of(device: dict) -> list[str]:
    return sorted({cmd for body in device["clusters"].values() for cmd in body.get("commands", [])})


async def hub(method: str, path: str, body: dict | None = None) -> Any:
    async with httpx.AsyncClient(base_url=HUB, timeout=30) as c:
        try:
            r = await c.request(method, path, json=body)
        except httpx.TransportError as e:
            raise ToolError(f"The Porch Device Hub is unreachable ({e!r}).") from e
    if r.status_code == 404:
        raise ToolError("No such device in this home. Call describe_home for the list.")
    if r.status_code >= 400:
        raise ToolError(f"Device Hub refused the request ({r.status_code}): {r.text[:300]}")
    return r.json()


async def devices() -> dict[str, dict]:
    return {d["id"]: d for d in await hub("GET", "/devices")}


def leak_detected(home: dict[str, dict]) -> bool:
    return any(flatten(d["clusters"]).get("BooleanState.StateValue") is True
               for d in home.values() if d["device_type"] == "WaterLeakSensor")


def decide(agent: dict, device: dict, command: str, params: dict, home: dict[str, dict]) -> tuple[str, str]:
    """Return (decision, reason). decision is 'allow', 'deny' or 'hold' (needs owner approval)."""
    cap = capability(device)
    tier = RISK_TIER[cap]
    if command not in commands_of(device):
        return "deny", f"{device['name']} does not support '{command}'. Supported: {', '.join(commands_of(device)) or 'none'}."
    # Emergency exception: any connected agent may shut the water main while a leak is detected.
    if cap == "water_shutoff" and command == "Close" and leak_detected(home):
        return "allow", "Emergency exception: a leak is detected, so closing the water main is allowed without asking."
    if cap not in agent["scopes"]:
        return "deny", f"Permission: {agent['name']} is not allowed to control {cap.replace('_', ' ')} devices in this home."
    if (cap, command) in SAFE_DIRECTION:
        return "allow", "This moves the device to its safe state, which is always allowed."
    if tier == "low_risk":
        return "allow", "Low-risk device within the agent's scope."
    if tier == "optimize_within_limits":
        if command == "SetDelayStart" and int(params.get("hours", 0)) > MAX_DELAY_H:
            return "deny", f"Household limit: delays can be at most {MAX_DELAY_H} hours."
        return "allow", "Appliance action within household limits."
    if tier == "needs_human":
        if cap == "heating":
            # HW7 change: the benchmark (case L7) showed Porch held heating even with someone home,
            # which the household policy allows. Heating now needs someone home, not the owner.
            if not STATE["occupied"]:
                return "deny", "Safety rule: no heating devices run when nobody is home."
            return "allow", "Someone is home, so heating is allowed for this agent."
        return "hold", f"{device['name']} needs the owner's approval to turn on."
    if tier == "never_autonomous":
        return "hold", f"{command} on {device['name']} needs the owner's approval. Agents can never do this on their own."
    return "deny", "No rule allows this action."


def check_approval(approval_id: str, agent: dict, device_id: str, command: str, params: dict) -> tuple[str, str]:
    """An approval covers one exact action by one agent, once, for a limited time."""
    a = APPROVALS.get(approval_id)
    if a is None or time.time() - a["created"] > APPROVAL_TTL_S:
        return "hold", f"Approval {approval_id} is unknown or expired; the owner must be asked again."
    if (a["agent"], a["device"], a["command"], a["params"]) != (agent["id"], device_id, command, params):
        return "deny", f"Approval {approval_id} was given for a different action."
    if a["status"] == "pending":
        return "waiting", "The owner has not answered yet."
    if a["status"] == "denied":
        return "deny", "The owner declined this request."
    if a["status"] == "used":
        return "deny", f"Approval {approval_id} was already used; each approval covers one action."
    a["status"] = "used"
    return "allow", "The owner approved this exact action."


async def execute_and_verify(device: dict, command: str, params: dict) -> dict:
    device_id = device["id"]
    sim = await hub("POST", f"/devices/{device_id}/simulate", {"command": command, "params": params})
    if not sim["would_succeed"]:
        return {"executed": False, "dry_run": "would fail", "error": sim.get("reason"), "verified": False}
    predicted = flatten(sim["simulated_state_after"])
    before = flatten(device["clusters"])
    key = f"porch-{uuid.uuid4().hex}"
    body = {"command": command, "params": params, "idempotency_key": key}
    try:
        result = await hub("POST", f"/devices/{device_id}/commands", body)
    except ToolError:
        result = await hub("POST", f"/devices/{device_id}/commands", body)  # same key: never runs twice
    # Independent check: read the device ourselves and compare with the dry run's prediction.
    after = flatten(await hub("GET", f"/devices/{device_id}/state"))
    expected = {k: v for k, v in predicted.items() if before.get(k) != v}
    matches = all(after.get(k) == v for k, v in expected.items())
    return {
        "executed": True,
        "hub_status": result["status"],
        "hub_error": result.get("error"),
        "before": {k: before.get(k) for k in expected} or before,
        "predicted": expected,
        "after": {k: after.get(k) for k in expected} or after,
        "verified": result["status"] == "done" and matches,
        "latency_ms": result["latency_ms"],
        "control_path": result.get("control_path"),
        "devices": "simulated by the Device Hub" if CONFIG.get("hardware") == "simulated" else "physical hardware",
        "answered_by_offline_copy": result.get("simulated", False),
        "idempotency_key": key,
    }


COLORS = {"allow": "\033[32m", "deny": "\033[31m", "hold": "\033[33m", "waiting": "\033[33m", "approved": "\033[36m",
          "declined": "\033[31m"}


def audit(entry: dict) -> dict:
    entry = {"id": f"a-{len(AUDIT) + 1:03d}", "time": _now(), **entry}
    AUDIT.append(entry)
    with AUDIT_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    c = COLORS.get(entry["decision"], "")
    v = entry.get("verification") or {}
    check = f"  verified={v.get('verified')}  {v.get('before')} -> {v.get('after')}  ({v.get('latency_ms')} ms via {v.get('control_path')})" if v.get("executed") else ""
    if v and not v.get("executed"):
        check = f"  NOT SENT: {v.get('error')}"
    if entry["decision"] == "hold" and entry.get("approval_id"):
        check = f"  APPROVAL ID: {entry['approval_id']}"
    print(f"{c}[PORCH {entry['decision'].upper():8}]\033[0m {entry['agent']:16} {entry['command']:>13} "
          f"{entry['device']:26} | {entry['reason']}{check}", flush=True)
    if entry.get("agent_reason"):
        print(f"{'':28}agent said: \"{entry['agent_reason']}\"", flush=True)
    return entry


async def stage(message: dict) -> None:
    message = {**message, "at": _now()}
    for ws in list(STAGE_SOCKETS):
        try:
            await ws.send_json(message)
        except Exception:
            STAGE_SOCKETS.discard(ws)


def stage_agents() -> dict:
    return {"type": "agents", "agents": [{"id": a["id"], "name": f"{a['name']} ({a['acts_for']})",
                                          "scope_label": a["scope_label"]} for a in CONFIG["agents"].values()]}


# ---------- tools agents use ----------

@mcp.tool
async def describe_home() -> dict:
    """List this home's devices, their current state, and what YOU are allowed to do with each one."""
    agent = current_agent()
    home = await devices()
    listing = []
    for d in home.values():
        access = {}
        for cmd in commands_of(d):
            decision, reason = decide(agent, d, cmd, {}, home)
            access[cmd] = {"allow": "allowed", "hold": "needs owner approval", "deny": f"blocked: {reason}"}[decision]
        listing.append({
            "device_id": d["id"], "name": d["name"], "room": d["room"], "type": d["device_type"],
            "capability": capability(d), "risk_tier": RISK_TIER[capability(d)], "online": d["online"],
            "platform": d["primary_control_path"], "state": flatten(d["clusters"]), "your_access": access,
        })
    print(f"\033[2m[PORCH READ    ] {agent['id']:16} describe_home  | {len(listing)} devices, with what this agent may do\033[0m", flush=True)
    return {"home": "Demo Home", "hardware": CONFIG.get("hardware", "physical"), "someone_home": STATE["occupied"], "leak_detected": leak_detected(home),
            "you_are": f"{agent['name']} acting for {agent['acts_for']}", "devices": listing,
            "command_params": {"SetDelayStart": {"hours": "0-12", "minutes": "0-59 (optional)"}}}


@mcp.tool
async def get_state(device_id: str) -> dict:
    """Read a device's current state directly from its platform."""
    current_agent()
    return {"device_id": device_id, "state": flatten(await hub("GET", f"/devices/{device_id}/state"))}


@mcp.tool
async def preview_action(device_id: str, command: str, params: dict[str, Any] | None = None) -> dict:
    """See what an action would do without touching the device: Porch's decision for you and the
    device's predicted state from the home's digital copy."""
    agent = current_agent()
    params = params or {}
    home = await devices()
    device = home.get(device_id)
    if device is None:
        raise ToolError(f"No device '{device_id}'. Call describe_home for the list.")
    decision, why = decide(agent, device, command, params, home)
    sim = await hub("POST", f"/devices/{device_id}/simulate", {"command": command, "params": params})
    return {"porch_decision": decision, "reason": why, "would_succeed_on_device": sim["would_succeed"],
            "device_reason": sim.get("reason"), "current_state": flatten(device["clusters"]),
            "predicted_state": flatten(sim["simulated_state_after"]), "note": "Nothing was sent to the device."}


@mcp.tool
async def request_action(device_id: str, command: str, reason: str, params: dict[str, Any] | None = None,
                         approval_id: str | None = None) -> dict:
    """Ask Porch to change a device. Porch checks your permissions and the home's safety rules, dry-runs
    the action, executes allowed actions, and verifies the result by reading the device back.

    command: one listed for the device in describe_home, e.g. On, Off, LockDoor, UnlockDoor, Start, Stop,
             SetDelayStart, Open, Close.
    reason: one sentence on why, written to the household's audit log.
    params: e.g. {"hours": 6} for SetDelayStart.
    approval_id: only when retrying an action the owner has approved.
    """
    agent = current_agent()
    params = params or {}
    home = await devices()
    device = home.get(device_id)
    if device is None:
        raise ToolError(f"No device '{device_id}' in this home. Call describe_home for the list.")
    decision, why = decide(agent, device, command, params, home)
    if decision == "hold" and approval_id:
        decision, why = check_approval(approval_id, agent, device_id, command, params)
    base = {"agent": agent["id"], "acts_for": agent["acts_for"], "device": device_id, "command": command,
            "params": params, "agent_reason": reason, "decision": decision, "reason": why}
    name = device["name"]

    if decision == "deny":
        entry = audit(base)
        await stage({"type": "safety", "kind": "scope_violation" if why.startswith("Permission") else "blocked",
                     "title": "Blocked by Porch", "detail": f"{command} on {name} → {why}",
                     "agent_id": agent["id"], "device_id": device_id, "command": command})
        await stage({"type": "audit", "summary": f"{agent['name']}: {command} on {name} blocked. {why}",
                     "agent_id": agent["id"], "outcome": "blocked"})
        return {"decision": "denied", "reason": why, "executed": False, "audit_id": entry["id"],
                "next_step": "Do not retry. Tell the user why this was blocked."}
    if decision in ("hold", "waiting"):
        if decision == "hold":
            approval_id = secrets.token_hex(3)
            APPROVALS[approval_id] = {"id": approval_id, "agent": agent["id"], "device": device_id,
                                      "device_name": name, "command": command, "params": params,
                                      "reason": reason, "created": time.time(), "status": "pending"}
            await stage({"type": "safety", "kind": "approval_required",
                         "title": "Owner approval required",
                         "detail": f"{command} on {name} held → approval {approval_id} sent to owner",
                         "agent_id": agent["id"], "device_id": device_id, "command": command})
            await stage({"type": "audit", "summary": f"{agent['name']}: {command} on {name} held for the owner",
                         "agent_id": agent["id"], "outcome": "pending_approval"})
        entry = audit({**base, "decision": decision, "approval_id": approval_id})
        return {"decision": "held_for_owner_approval", "reason": why, "executed": False,
                "approval_id": approval_id, "audit_id": entry["id"],
                "next_step": "Tell the user the owner has been asked. Once they approve, call request_action again "
                             "with the same device, command and params plus this approval_id."}

    result = await execute_and_verify(device, command, params)
    entry = audit({**base, "decision": "approved" if approval_id else "allow", "verification": result})
    outcome = "done" if result.get("verified") else "failed"
    summary = (f"{agent['name']}: {name} {command} {json.dumps(params) if params else ''}· "
               + (f"verified in {result['latency_ms']} ms" if result.get("verified")
                  else f"not confirmed: {result.get('error') or result.get('hub_error')}"))
    await stage({"type": "audit", "summary": summary, "agent_id": agent["id"], "outcome": outcome})
    return {"decision": "allowed", "reason": why, **result, "audit_id": entry["id"]}


@mcp.tool
def get_audit_log(limit: int = 10) -> list[dict]:
    """Your own recent requests to this home and what Porch decided."""
    agent = current_agent()
    return [e for e in AUDIT if e["agent"] == agent["id"]][-limit:]


# ---------- owner endpoints (the household's side, never exposed to agents) ----------

def _owner_ok(request: Request) -> bool:
    return request.headers.get("x-owner-key") == CONFIG["owner_key"]


def _unauthorized() -> JSONResponse:
    return JSONResponse({"error": "owner key required"}, status_code=401)


@mcp.custom_route("/owner/approvals", methods=["GET"])
async def list_approvals(request: Request):
    if not _owner_ok(request):
        return _unauthorized()
    now = time.time()
    return JSONResponse([{k: v for k, v in a.items() if k != "created"} for a in APPROVALS.values()
                         if a["status"] == "pending" and now - a["created"] <= APPROVAL_TTL_S])


@mcp.custom_route("/owner/approvals/{approval_id}/{verdict}", methods=["POST"])
async def answer_approval(request: Request):
    if not _owner_ok(request):
        return _unauthorized()
    a = APPROVALS.get(request.path_params["approval_id"])
    if a is None or a["status"] != "pending" or time.time() - a["created"] > APPROVAL_TTL_S:
        return JSONResponse({"error": "no such pending approval"}, status_code=404)
    approved = request.path_params["verdict"] == "approve"
    a["status"] = "approved" if approved else "denied"
    audit({"agent": a["agent"], "acts_for": "owner", "device": a["device"], "command": a["command"],
           "params": a["params"], "decision": "approved" if approved else "declined",
           "reason": f"Owner {'approved' if approved else 'declined'} approval {a['id']}."})
    await stage({"type": "audit", "summary": f"Owner {'approved' if approved else 'declined'} {a['command']} on {a['device_name']}",
                 "agent_id": a["agent"], "outcome": "done" if approved else "blocked"})
    return JSONResponse({k: v for k, v in a.items() if k != "created"})


@mcp.custom_route("/owner/audit", methods=["GET"])
async def owner_audit(request: Request):
    if not _owner_ok(request):
        return _unauthorized()
    return JSONResponse(AUDIT)


@mcp.custom_route("/owner/occupancy", methods=["POST"])
async def set_occupancy(request: Request):
    if not _owner_ok(request):
        return _unauthorized()
    STATE["occupied"] = bool((await request.json())["occupied"])
    return JSONResponse(STATE)


@mcp.custom_route("/owner/reset", methods=["POST"])
async def reset(request: Request):
    """Clear approvals and the audit log, and mark the home empty (devices are reset by owner.py)."""
    if not _owner_ok(request):
        return _unauthorized()
    AUDIT.clear()
    APPROVALS.clear()
    STATE["occupied"] = False
    await stage({"type": "audit", "summary": "Demo reset by the owner", "outcome": "done"})
    return JSONResponse({"ok": True})


async def stage_feed(websocket: WebSocket) -> None:
    """The stage display (/stage on the Device Hub) subscribes here for agents, safety alerts and audit lines."""
    await websocket.accept()
    STAGE_SOCKETS.add(websocket)
    await websocket.send_json(stage_agents())
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        STAGE_SOCKETS.discard(websocket)


app = mcp.http_app(path="/mcp")
app.router.routes.append(WebSocketRoute("/ws/stage", stage_feed))

if __name__ == "__main__":
    print(f"Porch gateway: MCP http://localhost:8000/mcp · stage feed ws://localhost:8000/ws/stage · hub {HUB}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
