"""Porch: a safety gateway that lets any AI agent act in a connected home.

Agents connect over MCP (streamable HTTP) with their own key. Every request is
checked against that agent's permissions and the home's safety rules, dry-run on
the Device Hub's digital copy, executed through the hub, and verified twice: the
hub's own read-back, then Porch's independent read of the device state.
Every decision is written to the audit log and pushed to the stage display.

Below Porch sits the Porch Device Hub (Interface v0), which drives LG ThinQ,
Matter and Google Home, or simulated copies of them.

Household rules (household_rules.py), when the hub keeps a household: an agent acts for one
person, and may not change a device in a room where someone else is, while its own person is not
in that room, without that person's permission, given with their PIN. An agent with
"reads": "energy" sees energy readings only and controls nothing.

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

import command_params
import device_grants
import flow
import household_rules as rules
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
# A stand-in or test run sets its own file, so its entries never mix into the real home's record.
AUDIT_FILE = Path(os.environ.get("PORCH_AUDIT_FILE") or ROOT / "audit.jsonl").expanduser()
# Which devices each agent may control, as the owner chose them (device_grants.py): beside the config.
AGENT_DEVICES_FILE = Path(os.environ.get("PORCH_AGENT_DEVICES_FILE")
                          or CONFIG_PATH.parent / device_grants.FILE).expanduser()
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
    "StickVacuumCleaner": "appliance",
    "AirPurifier": "appliance",
    "DoorLock": "entry",
    "WaterValve": "water_shutoff",
    "WaterLeakSensor": "sensor",
    "Doorbell": "sensor",
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
    # A plug that powers the home's network (its router): switching it off takes the home offline,
    # so both On and Off wait for the owner. Mapped per device in the config.
    "network": "never_autonomous",
}
# Commands that move a device toward its safe state are always allowed within scope.
SAFE_DIRECTION = {("heating", "Off"), ("unknown_load", "Off"), ("entry", "LockDoor")}

STATE: dict[str, Any] = {"occupied": False}
LAST_SEEN: dict[str, str] = {}  # agent id -> when it last called Porch, for the pages
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
        "tell the user who was asked; when they say yes, Porch carries the request out itself. Then "
        "get_audit_log, or request_action again with the approval_id, says what Porch did."
    ),
)
# Tools that only read: apps that ask before a tool changes something (ChatGPT asks for every tool
# without this hint) then ask only for request_action.
READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}


# ---------- helpers ----------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_agent() -> dict:
    headers = get_http_headers(include={"authorization"})
    token = headers.get("authorization", "").removeprefix("Bearer ").strip()
    agent = CONFIG["agents"].get(token)
    if not agent:
        raise ToolError("Unknown agent key. Ask the household owner to connect you to Porch.")
    LAST_SEEN[agent["id"]] = _now()
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
    # The devices the owner gave this agent, if they limited it: read on every request, so a change
    # on the owner's page applies at once. A damaged file stops every action rather than guess.
    try:
        granted = device_grants.read(AGENT_DEVICES_FILE).get(agent["id"])
    except device_grants.Unreadable as exc:
        return "deny", f"Porch could not read which devices each agent may control ({exc}), so it did not act."
    if granted is not None and (refused := device_grants.refusal(agent, device, granted, home)):
        return "deny", refused
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
        if cap == "network" and command == "Off":
            return "hold", (f"Off on {device['name']} needs the owner's approval. It powers the home's network: switching it "
                            "off takes the whole home offline, and only someone at home can switch it back on, with the "
                            "plug's own button.")
        return "hold", f"{command} on {device['name']} needs the owner's approval. Agents can never do this on their own."
    return "deny", "No rule allows this action."


async def household_context() -> dict | None:
    """Where people are and which room each device is in, from the hub. None when the hub keeps no
    household (an older hub): then only the capability rules apply."""
    try:
        async with httpx.AsyncClient(base_url=HUB, timeout=10) as c:
            people, placed = await c.get("/household"), await c.get("/home/places")
    except httpx.TransportError as e:
        raise ToolError(f"The Porch Device Hub is unreachable ({e!r}).") from e
    if people.status_code in (404, 409) or placed.status_code == 404:
        return None
    if people.status_code != 200 or placed.status_code != 200:
        # Not knowing who is where must not mean nobody is: stop rather than guess.
        raise ToolError("Porch could not read who is home, so it did not decide. Try again in a moment.")
    return {"people": people.json().get("people", []),
            "rooms": {r["id"]: r["name"] for r in placed.json().get("rooms", [])},
            "devices": placed.json().get("devices", {})}


def decide_for(agent: dict, device: dict, command: str, params: dict, home: dict[str, dict],
               ctx: dict | None) -> tuple[str, str, list[dict]]:
    """decide(), then the household rules: (decision, reason, the people who must say yes)."""
    if rules.reads_energy_only(agent):
        return "deny", f"Permission: {agent['name']} has no authority to control devices in this home; it may only read energy readings.", []
    decision, reason = decide(agent, device, command, params, home)
    if decision != "allow" or not ctx:
        return decision, reason, []
    cap = capability(device)
    if (cap, command) in SAFE_DIRECTION or (cap == "water_shutoff" and command == "Close" and leak_detected(home)):
        return decision, reason, []  # making the home safer is never held for anyone
    room = ctx["devices"].get(device["id"])
    asked = rules.must_ask(agent, room, ctx["people"])
    if not asked:
        return decision, reason, []
    return "hold", rules.why_ask(agent, ctx["rooms"].get(room), asked, ctx["people"]), asked


def _who(a: dict) -> str:
    """Who answers an approval: the people named in it, else the owner."""
    return " or ".join(a.get("approver_names") or []) or "The owner"


def check_approval(approval_id: str, agent: dict, device_id: str, command: str, params: dict) -> tuple[str, str]:
    """An approval covers one exact action by one agent, once, for a limited time."""
    a = APPROVALS.get(approval_id)
    if a is None or time.time() - a["created"] > APPROVAL_TTL_S:
        return "hold", f"Approval {approval_id} is unknown or expired; the owner must be asked again."
    if (a["agent"], a["device"], a["command"], a["params"]) != (agent["id"], device_id, command, params):
        return "deny", f"Approval {approval_id} was given for a different action."
    if a["status"] == "pending":
        return "waiting", f"{_who(a)} has not answered yet."
    if a["status"] == "denied":
        return "deny", f"{a.get('answered_by') or _who(a)} declined this request."
    if a["status"] == "used":
        return "deny", f"Approval {approval_id} was already used; each approval covers one action."
    a["status"] = "used"
    return "allow", f"{a.get('answered_by') or _who(a)} approved this exact action."


async def execute_and_verify(device: dict, command: str, params: dict, flow_id: str | None = None) -> dict:
    device_id = device["id"]
    sim = await hub("POST", f"/devices/{device_id}/simulate", {"command": command, "params": params})
    if flow_id:
        await flow_event(flow_id, "dry_run", **dry_run_fields(device, sim))
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
    if flow_id:
        await flow_event(flow_id, "sent", status=result["status"], path=result.get("control_path"),
                         latency_ms=result.get("latency_ms"), error=result.get("error"))
    # Independent check: read the device ourselves and compare with the dry run's prediction.
    after = flatten(await hub("GET", f"/devices/{device_id}/state"))
    expected = {k: v for k, v in predicted.items() if before.get(k) != v}
    matches = all(after.get(k) == v for k, v in expected.items())
    answer = {
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
    if flow_id:
        await flow_event(flow_id, "verified", verified=answer["verified"], expected=expected,
                         after={k: after.get(k) for k in expected}, latency_ms=answer["latency_ms"],
                         path=answer["control_path"])
    return answer


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


# ---------- the live view: each request, step by step (flow.py; drawn by the hub's /flow page) ----------

def new_flow() -> str:
    return f"f-{uuid.uuid4().hex[:10]}"


async def flow_event(flow_id: str, phase: str, **fields: Any) -> None:
    await stage({"type": "flow", "id": flow_id, "phase": phase, **fields})


def flow_rules(agent: dict, device: dict, command: str, params: dict, home: dict[str, dict], ctx: dict | None,
               decision: str, why: str, asked: list[dict], approval: tuple[str, str] | None = None) -> list[dict]:
    """Porch's three checks for this request, in words. decision, why and asked are decide_for()'s
    verdict before any approval; approval is check_approval()'s answer, when there was one."""
    cap = capability(device)
    people = (ctx or {}).get("people", [])
    room_id = (ctx or {}).get("devices", {}).get(device["id"])
    mine = rules.person_of(agent, people) if ctx else None
    safe = (cap, command) in SAFE_DIRECTION or (cap == "water_shutoff" and command == "Close" and leak_detected(home))
    return flow.rule_steps(
        agent=agent, cap=cap, rule=None if rules.reads_energy_only(agent) else decide(agent, device, command, params, home),
        decision=decision, why=why, asked=asked, room=(ctx or {}).get("rooms", {}).get(room_id),
        household=ctx is not None, mine=mine, mine_here=rules.surely_in(mine, room_id), safe=safe, approval=approval,
        device_type=device.get("device_type"))


def dry_run_fields(device: dict, sim: dict) -> dict:
    """The digital copy's answer, as the live view shows it: what would change, or why it would not work."""
    before = flatten(device["clusters"])
    predicted = flatten(sim.get("simulated_state_after") or {})
    return {"ok": bool(sim.get("would_succeed")), "reason": sim.get("reason"),
            "changes": {k: v for k, v in predicted.items() if before.get(k) != v},
            "path": sim.get("control_path"), "latency_ms": sim.get("estimated_latency_ms")}


async def flow_read(agent: dict, tool: str, what: str, listed: list[dict], device: dict | None = None) -> None:
    """A read, for the live view: the ask and what the agent was shown, sent together (reads are quick)."""
    fid = new_flow()
    await flow_event(fid, "ask", agent=flow.agent_card(agent), tool=tool, device=device)
    await flow_event(fid, "read", steps=flow.read_steps(agent, what), what=what,
                     devices=[d["device_id"] for d in listed],
                     paths=sorted({d["platform"] for d in listed if d.get("platform")}))


# ---------- tools agents use ----------

@mcp.tool(annotations=READ_ONLY)
async def describe_home() -> dict:
    """List this home's devices, their current state, and what YOU are allowed to do with each one."""
    agent = current_agent()
    home = await devices()
    if rules.reads_energy_only(agent):
        meters = [{"device_id": d["id"], "name": d["name"], "room": d["room"], "type": d["device_type"],
                   "online": d["online"], "platform": d["primary_control_path"],
                   "state": rules.energy_state(flatten(d["clusters"])), "your_access": {}}
                  for d in home.values() if rules.is_meter(d)]
        print(f"\033[2m[PORCH READ    ] {agent['id']:16} describe_home  | {len(meters)} energy meters only\033[0m", flush=True)
        await flow_read(agent, "describe_home",
                        f"Only the energy meters: {len(meters)} of the home's {len(home)} devices, with their readings.", meters)
        return {"you_are": f"{agent['name']} acting for {agent['acts_for']}",
                "you_may": "read energy readings only: the devices that report power or energy. You control nothing.",
                "devices": meters, "also": "get_energy gives the whole home's power now and each meter's history."}
    ctx = await household_context()
    listing = []
    for d in home.values():
        access = {}
        for cmd in commands_of(d):
            decision, reason, asked = decide_for(agent, d, cmd, {}, home, ctx)
            held = f"needs {' or '.join(p['name'] for p in asked)}'s approval" if asked else "needs owner approval"
            access[cmd] = {"allow": "allowed", "hold": held, "deny": f"blocked: {reason}"}[decision]
        entry = {
            "device_id": d["id"], "name": d["name"], "room": d["room"], "type": d["device_type"],
            "capability": capability(d), "risk_tier": RISK_TIER[capability(d)], "online": d["online"],
            "platform": d["primary_control_path"], "state": flatten(d["clusters"]), "your_access": access,
        }
        if params := command_params.for_device(d):  # what this device's commands take (command_params.py)
            entry["command_params"] = params
        listing.append(entry)
    print(f"\033[2m[PORCH READ    ] {agent['id']:16} describe_home  | {len(listing)} devices, with what this agent may do\033[0m", flush=True)
    platforms = len({d["platform"] for d in listing})
    await flow_read(agent, "describe_home", f"All {len(listing)} devices on {platforms} platform{'' if platforms == 1 else 's'}, "
                                            f"and what {agent['name']} may do with each.", listing)
    return {"home": "Demo Home", "hardware": CONFIG.get("hardware", "physical"), "someone_home": STATE["occupied"], "leak_detected": leak_detected(home),
            "you_are": f"{agent['name']} acting for {agent['acts_for']}", "devices": listing,
            "command_params": command_params.GENERAL}


@mcp.tool(annotations=READ_ONLY)
async def get_state(device_id: str) -> dict:
    """Read a device's current state directly from its platform."""
    agent = current_agent()
    try:
        if rules.reads_energy_only(agent):
            device = (await devices()).get(device_id)
            if device is None or not rules.is_meter(device):
                raise ToolError(f"{agent['name']} reads energy readings only, and {device_id} reports none.")
            state = rules.energy_state(flatten(await hub("GET", f"/devices/{device_id}/state")))
            what = f"{device['name']}'s energy readings, read from the device now."
        else:
            state = flatten(await hub("GET", f"/devices/{device_id}/state"))
            device = (await devices()).get(device_id) or {"id": device_id, "name": device_id}
            what = f"{device['name']}'s state, read from the device now."
    except ToolError as e:
        fid = new_flow()
        await flow_event(fid, "ask", agent=flow.agent_card(agent), tool="get_state", device={"id": device_id, "name": device_id})
        await flow_event(fid, "error", text=str(e))
        raise
    await flow_read(agent, "get_state", what, [{"device_id": device_id, "platform": device.get("primary_control_path")}],
                    flow.device_card(device))
    return {"device_id": device_id, "state": state}


@mcp.tool(annotations=READ_ONLY)
async def get_energy() -> dict:
    """What every plug in the home draws now, its own energy counter, and how its power moved since the
    hub started. Readings only, never estimates; a plug that reports no power says so."""
    agent = current_agent()
    print(f"\033[2m[PORCH READ    ] {agent['id']:16} get_energy     | the home's energy readings\033[0m", flush=True)
    summary = await hub("GET", "/home/energy")
    meters = [{"device_id": m.get("id"), "platform": m.get("path")} for m in summary.get("meters", []) if isinstance(m, dict)]
    await flow_read(agent, "get_energy", f"Every plug's power and energy now: {len(meters)} meters.", meters)
    return summary


@mcp.tool(annotations=READ_ONLY)
async def preview_action(device_id: str, command: str, params: dict[str, Any] | None = None) -> dict:
    """See what an action would do without touching the device: Porch's decision for you and the
    device's predicted state from the home's digital copy."""
    agent = current_agent()
    params = params or {}
    home = await devices()
    device = home.get(device_id)
    fid = new_flow()
    await flow_event(fid, "ask", agent=flow.agent_card(agent), tool="preview_action",
                     device=flow.device_card(device) if device else {"id": device_id, "name": device_id},
                     command=command, params=params)
    if device is None:
        problem = f"No device '{device_id}'. Call describe_home for the list."
        await flow_event(fid, "error", text=problem)
        raise ToolError(problem)
    try:
        ctx = None if rules.reads_energy_only(agent) else await household_context()
        decision, why, asked = decide_for(agent, device, command, params, home, ctx)
        waiting_for = " or ".join(p["name"] for p in asked) or ("the owner" if decision == "hold" else None)
        await flow_event(fid, "rules", steps=flow_rules(agent, device, command, params, home, ctx, decision, why, asked),
                         decision=decision, why=why, waiting_for=waiting_for, preview=True)
        if rules.reads_energy_only(agent):
            return {"porch_decision": decision, "reason": why, "note": "Nothing was sent to the device."}
        sim = await hub("POST", f"/devices/{device_id}/simulate", {"command": command, "params": params})
        await flow_event(fid, "dry_run", preview=True, **dry_run_fields(device, sim))
    except ToolError as e:
        await flow_event(fid, "error", text=str(e))
        raise
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
    fid = new_flow()
    await flow_event(fid, "ask", agent=flow.agent_card(agent), tool="request_action",
                     device=flow.device_card(device) if device else {"id": device_id, "name": device_id},
                     command=command, params=params, reason=reason, approval_id=approval_id)
    if device is None:
        problem = f"No device '{device_id}' in this home. Call describe_home for the list."
        await flow_event(fid, "error", text=problem)
        raise ToolError(problem)
    try:
        return await _request_action(agent, device, command, params, reason, approval_id, home, fid)
    except ToolError as e:
        await flow_event(fid, "error", text=str(e))
        raise


async def _request_action(agent: dict, device: dict, command: str, params: dict, reason: str,
                          approval_id: str | None, home: dict[str, dict], fid: str) -> dict:
    device_id = device["id"]
    done = APPROVALS.get(approval_id) if approval_id else None
    if done and done.get("carried_out") and \
            (done["agent"], done["device"], done["command"], done["params"]) == (agent["id"], device_id, command, params):
        # Porch carried it out when the person said yes: the agent's retry only learns what happened.
        result = done.get("result") or {}
        return {"decision": "already_carried_out",
                "reason": f"{done.get('answered_by') or _who(done)} said yes, and Porch carried this exact action out then.",
                "outcome": {k: result[k] for k in ("decision", "reason", "executed", "verified", "after", "error",
                                                   "hub_error", "latency_ms") if k in result},
                "next_step": "Tell the user what Porch did. Do not ask again."}
    ctx = None if rules.reads_energy_only(agent) else await household_context()
    decision, why, asked = decide_for(agent, device, command, params, home, ctx)
    held, held_why, approval = decision, why, None
    if approval_id and (decision == "hold" or (decision == "allow"
                                               and (APPROVALS.get(approval_id) or {}).get("family_protection"))):
        # Held by the rules, or allowed by them and held by family protection: the approval decides
        # and is used up. A request the rules now refuse stays refused, whatever was approved.
        decision, why = check_approval(approval_id, agent, device_id, command, params)
        approval = (decision, why)
    steps = flow_rules(agent, device, command, params, home, ctx, held, held_why, asked, approval)
    base = {"agent": agent["id"], "acts_for": agent["acts_for"], "device": device_id, "command": command,
            "params": params, "agent_reason": reason, "decision": decision, "reason": why}
    name = device["name"]

    if decision == "deny":
        entry = audit(base)
        await flow_event(fid, "rules", steps=steps, decision="deny", why=why)
        await stage({"type": "safety", "kind": "scope_violation" if why.startswith("Permission") else "blocked",
                     "title": "Blocked by Porch", "detail": f"{command} on {name} → {why}",
                     "agent_id": agent["id"], "device_id": device_id, "command": command})
        await stage({"type": "audit", "summary": f"{agent['name']}: {command} on {name} blocked. {why}",
                     "agent_id": agent["id"], "outcome": "blocked"})
        return {"decision": "denied", "reason": why, "executed": False, "audit_id": entry["id"],
                "next_step": "Do not retry. Tell the user why this was blocked."}
    if decision in ("hold", "waiting"):
        names = [p["name"] for p in asked]
        who = " or ".join(names) or "the owner"
        if decision == "hold" and (same := _pending_same(agent["id"], device_id, command, params)):
            decision, approval_id = "waiting", same["id"]  # asked again: the same request still waits
        if decision == "hold":
            approval_id = secrets.token_hex(3)
            APPROVALS[approval_id] = {"id": approval_id, "agent": agent["id"], "agent_name": agent["name"],
                                      "device": device_id, "device_name": name, "command": command, "params": params,
                                      "reason": reason, "why_held": why, "approvers": [p["id"] for p in asked],
                                      "approver_names": names, "created": time.time(), "status": "pending",
                                      "flow": fid}
            await stage({"type": "safety", "kind": "approval_required",
                         "title": f"{who[0].upper() + who[1:]}'s approval required",
                         "detail": f"{command} on {name} held → approval {approval_id} sent to {who}",
                         "agent_id": agent["id"], "device_id": device_id, "command": command})
            await stage({"type": "audit", "summary": f"{agent['name']}: {command} on {name} held for {who}. {why}",
                         "agent_id": agent["id"], "outcome": "pending_approval"})
        else:
            who = _who(APPROVALS.get(approval_id) or {})
        entry = audit({**base, "decision": decision, "approval_id": approval_id})
        await flow_event(fid, "rules", steps=steps, decision=decision, why=why, waiting_for=who, approval_id=approval_id)
        return {"decision": "held_for_owner_approval" if not asked and who in ("the owner", "The owner") else "held_for_approval",
                "reason": why, "executed": False, "waiting_for": who, "approval_id": approval_id, "audit_id": entry["id"],
                "next_step": f"Tell the user {who} has been asked. When {who} says yes, Porch carries out this "
                             "exact request itself, at once. Then get_audit_log, or request_action again with the "
                             "same device, command and params plus this approval_id, says what happened."}

    await flow_event(fid, "rules", steps=steps, decision="allow", why=why, approval_id=approval_id)
    result = await execute_and_verify(device, command, params, fid)
    if (not result.get("executed") and str(result.get("error") or "").startswith("Family protection")
            and not _owner_said_yes(approval_id)):
        # Porch's rules allow it, but family protection, the owner's, stops it: rather than fail, it
        # waits for the owner's yes, which lets this one command through once (the hub's pass).
        return await _hold_for_protection(agent, device, command, params, reason, base, steps, fid, result["error"])
    entry = audit({**base, "decision": "approved" if approval_id else "allow", "verification": result})
    outcome = "done" if result.get("verified") else "failed"
    summary = (f"{agent['name']}: {name} {command} {json.dumps(params) if params else ''}· "
               + (f"verified in {result['latency_ms']} ms" if result.get("verified")
                  else f"not confirmed: {result.get('error') or result.get('hub_error')}"))
    await stage({"type": "audit", "summary": summary, "agent_id": agent["id"], "outcome": outcome})
    return {"decision": "allowed", "reason": why, **result, "audit_id": entry["id"]}


def _pending_same(agent_id: str, device_id: str, command: str, params: dict) -> dict | None:
    """The request this agent already has waiting for the exact same action, if any."""
    now = time.time()
    return next((a for a in APPROVALS.values() if a["status"] == "pending" and now - a["created"] <= APPROVAL_TTL_S
                 and (a["agent"], a["device"], a["command"], a["params"]) == (agent_id, device_id, command, params)), None)


def _owner_said_yes(approval_id: str | None) -> bool:
    """Whether this action runs on the owner's own yes, which already decided family protection."""
    a = APPROVALS.get(approval_id) if approval_id else None
    return bool(a and a.get("owner_answered"))


async def _hold_for_protection(agent: dict, device: dict, command: str, params: dict, reason: str, base: dict,
                               steps: list[dict], fid: str, stopped: str) -> dict:
    """Family protection stopped a request Porch's rules allow: it waits for the owner's yes."""
    name, device_id = device["name"], device["id"]
    why = (f"Family protection is on, so {command} on {name} waits for the owner's yes. "
           "The owner's yes, with the owner PIN, lets this one command through, once.")
    same = _pending_same(agent["id"], device_id, command, params)
    approval_id = same["id"] if same else secrets.token_hex(3)
    if not same:
        APPROVALS[approval_id] = {"id": approval_id, "agent": agent["id"], "agent_name": agent["name"],
                                  "device": device_id, "device_name": name, "command": command, "params": params,
                                  "reason": reason, "why_held": why, "approvers": [], "approver_names": [],
                                  "created": time.time(), "status": "pending", "flow": fid, "family_protection": True}
        await stage({"type": "safety", "kind": "approval_required", "title": "The owner's yes is needed",
                     "detail": f"{command} on {name}: family protection → approval {approval_id} sent to the owner",
                     "agent_id": agent["id"], "device_id": device_id, "command": command})
    entry = audit({**base, "decision": "hold", "reason": why, "approval_id": approval_id, "family_protection": stopped})
    await flow_event(fid, "rules", steps=steps, decision="hold", why=why, waiting_for="the owner", approval_id=approval_id)
    return {"decision": "held_for_owner_approval", "reason": why, "executed": False, "waiting_for": "the owner",
            "approval_id": approval_id, "audit_id": entry["id"],
            "next_step": "Tell the user that family protection is on and the owner has been asked. When the owner "
                         "says yes, Porch carries out this exact request itself, at once; get_audit_log then says "
                         "what happened."}


@mcp.tool(annotations=READ_ONLY)
def get_audit_log(limit: int = 10) -> list[dict]:
    """Your own recent requests to this home and what Porch decided."""
    agent = current_agent()
    return [e for e in AUDIT if e["agent"] == agent["id"]][-limit:]


# ---------- owner endpoints (the household's side, never exposed to agents) ----------

async def carry_out(a: dict) -> dict:
    """Carry out an approved request at once, as the agent that asked, through the same checks as
    the agent's own retry with the approval id, which this uses up. Nobody waits for the agent to
    come back; its retry then only reports what happened. Family protection in the hub still
    decides: the owner's yes, with the owner PIN, lets this one command through (porch_hub/api.py)."""
    agent = next((x for x in CONFIG["agents"].values() if x.get("id") == a["agent"]), None)
    fid = a.get("flow") or new_flow()
    if agent is None:
        result = {"executed": False, "error": "The agent that asked is no longer in this home, so nothing was done."}
    else:
        try:
            home = await devices()
            device = home.get(a["device"])
            if device is None:
                raise ToolError("That device is no longer in this home, so nothing was done.")
            result = await _request_action(agent, device, a["command"], a["params"], a.get("reason") or "",
                                           a["id"], home, fid)
        except ToolError as exc:
            await flow_event(fid, "error", text=str(exc))
            result = {"executed": False, "error": str(exc)}
    a["carried_out"], a["result"] = True, result
    return result


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


async def _household_owner() -> str | None:
    """The household owner's person id, from the hub; None when the hub keeps no household or is down."""
    try:
        async with httpx.AsyncClient(base_url=HUB, timeout=10) as c:
            answer = await c.get("/household")
    except httpx.TransportError:
        return None
    if answer.status_code != 200:
        return None
    return next((p["id"] for p in answer.json().get("people", []) if p.get("owner")), None)


@mcp.custom_route("/owner/approvals/{approval_id}/{verdict}", methods=["POST"])
async def answer_approval(request: Request):
    if not _owner_ok(request):
        return _unauthorized()
    a = APPROVALS.get(request.path_params["approval_id"])
    if a is None or a["status"] != "pending" or time.time() - a["created"] > APPROVAL_TTL_S:
        return JSONResponse({"error": "no such pending approval"}, status_code=404)
    if a.get("approvers") and await _household_owner() not in a["approvers"]:
        # Held for the person in the room: the owner, away, does not answer for them.
        return JSONResponse({"error": f"This request waits for {_who(a)}: {a.get('why_held')} Only {_who(a)} can "
                                      "answer it, on the Porch page with their PIN."}, status_code=403)
    approved = request.path_params["verdict"] == "approve"
    a["status"] = "approved" if approved else "denied"
    a["owner_answered"] = approved  # the owner's hand, in Terminal
    await flow_event(a.get("flow") or a["id"], "approval", approved=approved, by="The owner", approval_id=a["id"],
                     agent_id=a["agent"], device_id=a["device"], command=a["command"])
    audit({"agent": a["agent"], "acts_for": "owner", "device": a["device"], "command": a["command"],
           "params": a["params"], "decision": "approved" if approved else "declined",
           "reason": f"Owner {'approved' if approved else 'declined'} approval {a['id']}."})
    await stage({"type": "audit", "summary": f"Owner {'approved' if approved else 'declined'} {a['command']} on {a['device_name']}",
                 "agent_id": a["agent"], "outcome": "done" if approved else "blocked"})
    outcome = await carry_out(a) if approved else None
    return JSONResponse({**{k: v for k, v in a.items() if k not in ("created", "result")}, "outcome": outcome})


# ---------- the household's side: agents and approvals for the hub's pages (no keys, PINs instead) ----------

AGENT_FIELDS = ("id", "name", "acts_for", "person", "reads", "scopes", "scope_label", "client")


@mcp.custom_route("/household/agents", methods=["GET"])
async def household_agents(request: Request):
    """Every agent, whose it is, what it may do, the app it lives in, when it last called and the
    devices the owner limited it to (null: not limited). Never its key."""
    try:
        grants, problem = device_grants.read(AGENT_DEVICES_FILE), None
    except device_grants.Unreadable as exc:
        grants, problem = {}, str(exc)
    return JSONResponse([{**{k: a.get(k) for k in AGENT_FIELDS}, "last_seen": LAST_SEEN.get(a["id"]),
                          "devices": grants.get(a["id"]), **({"devices_problem": problem} if problem else {})}
                         for a in CONFIG["agents"].values()])


@mcp.custom_route("/household/approvals", methods=["GET"])
async def household_approvals(request: Request):
    """Requests held now, and who may answer each (an empty list of approvers: the owner)."""
    now = time.time()
    return JSONResponse([{**{k: v for k, v in a.items() if k != "created"}, "age_s": int(now - a["created"])}
                         for a in APPROVALS.values()
                         if a["status"] == "pending" and now - a["created"] <= APPROVAL_TTL_S])


@mcp.custom_route("/household/approvals/{approval_id}/{verdict}", methods=["POST"])
async def household_answer(request: Request):
    """A person answers a request held for them, with their PIN, checked by the hub."""
    a = APPROVALS.get(request.path_params["approval_id"])
    if a is None or a["status"] != "pending" or time.time() - a["created"] > APPROVAL_TTL_S:
        return JSONResponse({"error": "No such request is waiting."}, status_code=404)
    try:
        body = await request.json()
        person, pin = str(body["person"]), str(body["pin"])
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"error": "person and pin are needed"}, status_code=422)
    approvers = a.get("approvers") or []
    if person not in approvers and not (not approvers and person == await _household_owner()):
        return JSONResponse({"error": f"This request waits for {_who(a)}; only {_who(a)} can answer it."}, status_code=403)
    try:
        async with httpx.AsyncClient(base_url=HUB, timeout=10) as c:
            checked = await c.post("/household/verify", json={"person": person, "pin": pin})
    except httpx.TransportError:
        return JSONResponse({"error": "The Device Hub is unreachable, so the PIN could not be checked."}, status_code=503)
    if checked.status_code != 200:
        detail = checked.json().get("detail", "Wrong PIN.") if checked.headers.get("content-type", "").startswith("application/json") else "Wrong PIN."
        return JSONResponse({"error": detail}, status_code=checked.status_code)
    approved = request.path_params["verdict"] == "approve"
    name = next((n for i, n in zip(approvers, a.get("approver_names") or []) if i == person), person)
    if not approvers:
        name = "The owner"
    a["status"], a["answered_by"] = ("approved" if approved else "denied"), name
    a["owner_answered"] = approved and person == await _household_owner()
    await flow_event(a.get("flow") or a["id"], "approval", approved=approved, by=name, approval_id=a["id"],
                     agent_id=a["agent"], device_id=a["device"], command=a["command"])
    audit({"agent": a["agent"], "acts_for": name, "device": a["device"], "command": a["command"],
           "params": a["params"], "decision": "approved" if approved else "declined",
           "reason": f"{name} {'approved' if approved else 'declined'} approval {a['id']}."})
    await stage({"type": "audit", "summary": f"{name} {'approved' if approved else 'declined'} {a['command']} on "
                                             f"{a['device_name']} for {a.get('agent_name', a['agent'])}",
                 "agent_id": a["agent"], "outcome": "done" if approved else "blocked"})
    outcome = await carry_out(a) if approved else None
    return JSONResponse({**{k: v for k, v in a.items() if k not in ("created", "result")}, "outcome": outcome})


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
