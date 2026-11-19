"""Interface v0 routes. The Porch Engine is the only caller of the command route.

Device ids contain slashes (home-1/front-door-lock), so routes use path params.
"""

import asyncio
import json
import contextlib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.websockets import WebSocketDisconnect

from . import agent_grants
from .details import rows_from_clusters
from .digital_copy import DigitalCopy, SimulateRequest, SimulateResponse
from .drivers import DriverError
from .energy import EnergyLog
from .events import Subscription, SubscriberLagged
from .household import Household, NotAllowed, PersonUnknown, places, rooms_of
from .layout import load_layout
from .models import Clusters, CommandRequest, CommandResponse, Device
from .protection import (DEFAULT_COMMANDS, MAX_MINUTES, NOT_ENFORCED, FamilyProtection, PinLocked, PinNotSet,
                         WrongPin, close_window, open_window, owner_command)
from .registry import DeviceRegistry
from .router import CommandRouter


@dataclass
class Hub:
    registry: DeviceRegistry
    commands: CommandRouter
    digital_copy: DigitalCopy
    protection: FamilyProtection | None = None
    home_layout_path: Path | None = None
    energy: EnergyLog = field(default_factory=EnergyLog)
    # Who lives here and where each person is (household.py), and the gateway the pages ask about
    # agents and approvals.
    household: Household | None = None
    gateway_url: str = "http://127.0.0.1:8000"


class OfflineMode(BaseModel):
    enabled: bool


class AttributeChange(BaseModel):
    cluster: str
    attribute: str
    value: Any


def get_hub(request: Request) -> Hub:
    return request.app.state.hub


router = APIRouter()


@router.get("/devices", response_model=list[Device])
async def list_devices(hub: Hub = Depends(get_hub)) -> list[Device]:
    """Every device across all connected platforms, one record per physical device."""
    return await hub.registry.refresh()


@router.get("/devices/{device_id:path}/state", response_model=Clusters)
async def read_state(device_id: str, hub: Hub = Depends(get_hub)) -> Clusters:
    """Live cluster state, read from the device's platform now."""
    device = await hub.registry.resolve(device_id)
    _, clusters = await hub.registry.read_state(device.id)
    return clusters


@router.post("/devices/{device_id:path}/commands", response_model=CommandResponse)
async def run_command(device_id: str, body: CommandRequest, hub: Hub = Depends(get_hub)) -> CommandResponse:
    """Run one command and answer only after reading the device's state back."""
    return await hub.commands.execute(device_id, body)


@router.get("/devices/{device_id:path}/details")
async def device_details(device_id: str, hub: Hub = Depends(get_hub)) -> dict[str, Any]:
    """Everything each platform that reaches the device reports about it, read now, in plain words
    (details.py). Identifiers and network addresses are left out."""
    device = hub.registry.get(device_id)
    if hub.registry.offline_mode:
        return {"device_id": device.id, "name": device.name, "sections": [
            {"path": "digital_copy", "online": device.online, "rows": rows_from_clusters(device.clusters),
             "problem": "The hub is in offline mode: this is its digital copy, not the platforms."}]}
    sections = []
    for route in hub.registry.routes(device_id):
        timeout_s = route.driver.call_timeout_s or 20.0
        try:
            rows = await asyncio.wait_for(route.driver.details(route.device_id), timeout_s)
            sections.append({"path": route.path, "online": route.record.online, "rows": rows})
        except Exception as exc:  # one platform that does not answer must not hide the others
            if isinstance(exc, asyncio.TimeoutError):
                problem = f"No answer in {timeout_s:g} seconds."
            elif isinstance(exc, DriverError):
                problem = str(exc)  # drivers word these for people, with nothing private in them
            else:  # anything else may carry the platform's own text: its type only
                problem = f"The answer could not be read ({type(exc).__name__})."
            sections.append({"path": route.path, "online": route.record.online, "rows": [], "problem": problem})
    return {"device_id": device.id, "name": device.name, "sections": sections}


@router.post("/devices/{device_id:path}/simulate", response_model=SimulateResponse)
async def simulate(device_id: str, body: SimulateRequest, hub: Hub = Depends(get_hub)) -> SimulateResponse:
    """Dry run on the digital copy: the state after, whether it would succeed, and how long it would take.

    Nothing is sent to any device, and the copy itself is left unchanged.
    """
    device = await hub.registry.resolve(device_id)
    result = hub.digital_copy.simulate(device, body.command, body.params)
    # Shown on the stage display: what the digital copy predicts before anything reaches hardware.
    hub.registry.events.publish(
        "command_simulated", device.id, result.control_path or device.primary_control_path,
        command=body.command, params=body.params, clusters=result.simulated_state_after,
        would_succeed=result.would_succeed, latency_ms=result.estimated_latency_ms, error=result.reason,
    )
    return result


@router.get("/home/layout")
async def home_layout(hub: Hub = Depends(get_hub)) -> dict[str, Any]:
    """The home's floor plan for the twin pages: floors, rooms, and where each device sits."""
    return load_layout(hub.home_layout_path)


@router.get("/home/energy")
async def home_energy(hub: Hub = Depends(get_hub)) -> dict[str, Any]:
    """Power now and energy so far, for the twin pages' energy panel.

    Lists every plug, and every other device that reports power or energy. A plug that reports
    neither (one reached through Google Home) is listed with its switch only: nothing is estimated.
    """
    return hub.energy.summary(await hub.registry.refresh())


@router.get("/home/places")
async def home_places(hub: Hub = Depends(get_hub)) -> dict[str, Any]:
    """The plan's rooms, and the room each device is in (null when the plan does not say)."""
    devices = [d.model_dump(mode="json") for d in await hub.registry.refresh()]
    return places(devices, load_layout(hub.home_layout_path))


# ---------- The household: who is where, set by hand with a PIN (household.py) ----------

def _household(hub: Hub) -> Household:
    if hub.household is None:
        raise HTTPException(409, "This hub keeps no household.")
    return hub.household


@router.get("/household")
async def household(hub: Hub = Depends(get_hub)) -> dict[str, Any]:
    """Who lives here and where each person is now. Set by hand: no sensor tells people apart."""
    return _household(hub).summary(rooms_of(load_layout(hub.home_layout_path)))


class PlaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person: str = Field(min_length=1, max_length=40)
    where: str = Field(pattern="^(away|home)$")
    room: str | None = Field(default=None, max_length=80)
    by: str = Field(min_length=1, max_length=40)
    pin: str = Field(min_length=1, max_length=200)


def _pin_error(exc: Exception, who: str) -> HTTPException:
    if isinstance(exc, PinNotSet):
        return HTTPException(403, f"{who} has no PIN yet. It is set on the hub's computer: "
                                  "deploy/home/household.sh set-pin <person>")
    if isinstance(exc, PinLocked):
        return HTTPException(429, f"Too many wrong PINs. Try again in {exc.seconds} seconds.")
    return HTTPException(401, "Wrong PIN.")


@router.post("/household/presence")
async def set_presence(body: PlaceRequest, hub: Hub = Depends(get_hub)) -> dict[str, Any]:
    """Mark where someone is. It needs that person's PIN, or the owner's."""
    home = _household(hub)
    rooms = rooms_of(load_layout(hub.home_layout_path))
    try:
        home.set_place(body.person, body.where, body.room, rooms, by=body.by, pin=body.pin)
    except PersonUnknown as exc:
        raise HTTPException(404, str(exc)) from None
    except NotAllowed as exc:
        raise HTTPException(403, str(exc)) from None
    except (PinNotSet, PinLocked, WrongPin) as exc:
        raise _pin_error(exc, body.by) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    summary = home.summary(rooms)
    hub.registry.events.publish("household_changed", None, None, data=summary)
    return summary


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person: str = Field(min_length=1, max_length=40)
    pin: str = Field(min_length=1, max_length=200)


@router.post("/household/verify")
async def verify_pin(body: VerifyRequest, hub: Hub = Depends(get_hub)) -> dict[str, Any]:
    """Whether a PIN is this person's. The gateway asks it before it takes a person's approval.
    Wrong guesses lock that person's PIN for a while, as on the pages."""
    try:
        _household(hub).verify(body.person, body.pin)
    except PersonUnknown as exc:
        raise HTTPException(404, str(exc)) from None
    except (PinNotSet, PinLocked, WrongPin) as exc:
        raise _pin_error(exc, body.person) from None
    return {"ok": True, "person": body.person}


@router.get("/online")
async def online_access(hub: Hub = Depends(get_hub)) -> dict[str, Any]:
    """Whether agents can reach Porch from the internet right now (deploy/home/online.sh), and which."""
    folder = hub.household.path.parent if hub.household is not None else None
    if folder is None:
        return {"enabled": False, "agents": []}

    def read(name: str) -> dict:
        try:
            value = json.loads((folder / name).read_text())
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    state, keys = read("online.json"), read("online-keys.json").get("keys", {})
    return {"enabled": state.get("enabled") is True, "since": state.get("since") if state.get("enabled") is True else None,
            "agents": sorted({e.get("agent") for e in keys.values() if isinstance(e, dict) and e.get("agent")})}


# ---------- The gateway's side, for the pages: its agents and the approvals waiting for someone ----------

async def _gateway(hub: Hub, method: str, path: str, body: dict | None = None, timeout: float = 15) -> Any:
    import httpx

    try:
        async with httpx.AsyncClient(base_url=hub.gateway_url, timeout=timeout) as client:
            answer = await client.request(method, path, json=body)
    except httpx.HTTPError:
        raise HTTPException(503, "Porch's gateway is not running, so agents and approvals cannot be shown.") from None
    try:
        data = answer.json()
    except ValueError:
        data = {"detail": answer.text[:300]}
    if answer.status_code >= 400:
        raise HTTPException(answer.status_code, data.get("error") or data.get("detail") or "The gateway refused.")
    return data


@router.get("/household/agents")
async def household_agents(hub: Hub = Depends(get_hub)) -> Any:
    """Every agent the gateway knows, whose it is and what it may do. No keys."""
    return await _gateway(hub, "GET", "/household/agents")


@router.get("/household/approvals")
async def household_approvals(hub: Hub = Depends(get_hub)) -> Any:
    """Requests the gateway holds, and who may answer each."""
    return await _gateway(hub, "GET", "/household/approvals")


class VerdictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person: str = Field(min_length=1, max_length=40)
    pin: str = Field(min_length=1, max_length=200)


@router.post("/household/approvals/{approval_id}/{verdict}")
async def answer_household_approval(approval_id: str, verdict: str, body: VerdictRequest,
                                    hub: Hub = Depends(get_hub)) -> Any:
    """A person answers a request held for them, with their PIN. The gateway checks both."""
    if verdict not in ("approve", "decline"):
        raise HTTPException(404, "approve or decline")
    granted: tuple[str, str] | None = None
    if verdict == "approve" and hub.protection is not None and hub.household is not None \
            and body.person == hub.household.owner_id():
        # The owner's yes, with the owner PIN, is the owner's own consent to this exact request:
        # family protection lets its one command through once, for a short while. Someone else's
        # yes does not: family protection is the owner's.
        try:
            hub.household.verify(body.person, body.pin)
        except (PinNotSet, PinLocked, WrongPin) as exc:
            raise _pin_error(exc, "The owner") from None
        waiting = await _gateway(hub, "GET", "/household/approvals")
        held = next((a for a in waiting if isinstance(a, dict) and a.get("id") == approval_id), None)
        # Only when this answer is the owner's to give: a request waiting for Wife gets no pass.
        if held is not None and (not held.get("approvers") or body.person in held["approvers"]):
            hub.protection.allow_once(held["device"], held["command"],
                                      why=f"the owner said yes to {held.get('agent_name') or held.get('agent')}'s request")
            granted = (held["device"], held["command"])
    try:
        # The gateway carries the request out within this call (a cloud device can take seconds).
        return await _gateway(hub, "POST", f"/household/approvals/{approval_id}/{verdict}", body.model_dump(),
                              timeout=90)
    finally:
        if granted:
            hub.protection.spend(*granted)  # for this answer only: whatever was not sent is not left open


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    devices: list[str] | None = Field(default=None, max_length=64)  # None: any device its rights allow
    pin: str = Field(min_length=1, max_length=200)


@router.post("/household/agents/{agent_id}/devices")
async def set_agent_devices(agent_id: str, body: GrantRequest, hub: Hub = Depends(get_hub)) -> dict[str, Any]:
    """The owner chooses the devices one agent may control: a list (empty: none), or null to let it
    control any device its rights allow. Needs the owner PIN. The gateway reads the choice on the
    agent's next request (porch_gateway/device_grants.py); Porch's rules, the household's and family
    protection all still apply."""
    # The owner's PIN: family protection's, or else the household owner's. Both live in the settings
    # folder beside the gateway's, where the gateway reads the choice.
    if hub.protection is not None:
        _check_owner_pin(hub, body.pin)
        folder = hub.protection.path.parent
    elif hub.household is not None and (owner := hub.household.owner_id()):
        try:
            hub.household.verify(owner, body.pin)
        except (PinNotSet, PinLocked, WrongPin) as exc:
            raise _pin_error(exc, "The owner") from None
        folder = hub.household.path.parent
    else:
        raise HTTPException(409, "This hub keeps no owner PIN. Choose an agent's devices on the hub's computer: "
                                 "deploy/home/agent.sh devices <agent> <device ids>")
    agents = await _gateway(hub, "GET", "/household/agents")
    if not any(isinstance(a, dict) and a.get("id") == agent_id for a in agents):
        raise HTTPException(404, f"Porch has no agent {agent_id}.")
    devices = None if body.devices is None else [hub.registry.get(d).id for d in body.devices]  # 404 for a stranger
    try:
        agent_grants.write(folder / agent_grants.FILE, agent_id, devices, by="the owner's page")
    except agent_grants.Damaged as exc:
        raise HTTPException(409, f"{exc}, so it was left as it is. Look at it on the hub's computer.") from None
    return {"agent": agent_id, "devices": devices}


@router.get("/protection")
async def protection(hub: Hub = Depends(get_hub)) -> dict[str, Any]:
    """Family protection: whether real commands are allowed right now, for which devices, until when.

    The owner opens a window on the hub's computer (porch_hub.protection), or from a page with
    the owner's PIN (POST /protection/window).
    """
    return hub.protection.summary() if hub.protection is not None else dict(NOT_ENFORCED)


class WindowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_ids: list[str] = Field(min_length=1, max_length=32)
    minutes: int = Field(default=5, ge=1, le=MAX_MINUTES)
    commands: list[str] | None = None
    pin: str = Field(min_length=1, max_length=200)


def _protection_changed(hub: Hub) -> dict[str, Any]:
    """Tell every open page at once, and answer with the new state."""
    summary = hub.protection.summary()
    hub.registry.events.publish("protection_changed", None, None, data=summary)
    return summary


def _check_owner_pin(hub: Hub, pin: str) -> None:
    """The owner's PIN, which no agent has. Wrong guesses lock it for a while."""
    try:
        hub.protection.pin.verify(pin)
    except PinNotSet:
        raise HTTPException(403, "No owner PIN is set. Choose one on the hub's computer: "
                                 f"{owner_command()} set-pin") from None
    except PinLocked as locked:
        raise HTTPException(429, f"Too many wrong PINs. Try again in {locked.seconds} seconds.") from None
    except WrongPin:
        raise HTTPException(401, "Wrong PIN.") from None


@router.post("/protection/window")
async def open_protection_window(body: WindowRequest, hub: Hub = Depends(get_hub)) -> dict[str, Any]:
    """The owner opens a control window from a page: named devices, for a few minutes.

    It needs the owner's PIN, which no agent has. Wrong guesses lock it for a while.
    """
    if hub.protection is None:
        raise HTTPException(409, "This hub does not enforce family protection, so there is no window to open.")
    _check_owner_pin(hub, body.pin)
    devices = [hub.registry.get(device_id) for device_id in body.device_ids]  # 404 for one this home lacks
    commands = body.commands
    if commands is None:
        # Of the commands a window covers by default, the ones these devices take: a window for
        # a lamp says On and Off, and one for a TV says Play and Pause.
        taken = {name for device in devices for cluster in device.clusters.values() for name in cluster.commands}
        commands = [name for name in DEFAULT_COMMANDS if name in taken] or None
    try:
        open_window(hub.protection.path, body.device_ids, body.minutes, commands)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return _protection_changed(hub)


@router.post("/protection/close")
async def close_protection_window(hub: Hub = Depends(get_hub)) -> dict[str, Any]:
    """Close the control window now. It needs no PIN: anyone at home may put protection back on."""
    if hub.protection is None:
        raise HTTPException(409, "This hub does not enforce family protection.")
    close_window(hub.protection.path)
    return _protection_changed(hub)


@router.get("/demo/offline-mode", response_model=OfflineMode)
async def get_offline_mode(hub: Hub = Depends(get_hub)) -> OfflineMode:
    return OfflineMode(enabled=hub.digital_copy.offline)


@router.put("/demo/offline-mode", response_model=OfflineMode)
async def set_offline_mode(body: OfflineMode, hub: Hub = Depends(get_hub)) -> OfflineMode:
    """Switch the offline backup on or off without restarting, for example when venue Wi-Fi drops."""
    await hub.digital_copy.set_offline(body.enabled)
    return OfflineMode(enabled=hub.digital_copy.offline)


@router.post("/demo/devices/{device_id:path}/attributes", response_model=Clusters)
async def set_shadow_attribute(device_id: str, body: AttributeChange, hub: Hub = Depends(get_hub)) -> Clusters:
    """Offline mode only: change a reading in the digital copy (a leak, a lock turned by hand) to drive a scene."""
    if not hub.digital_copy.offline:
        raise HTTPException(409, "only in offline demo mode; live readings come from the devices")
    device = await hub.registry.resolve(device_id)
    return hub.digital_copy.set_attribute(device, body.cluster, body.attribute, body.value)


def _page(name: str) -> HTMLResponse:
    """One self-contained page from porch_hub/dashboard, never cached so a restart shows the new one."""
    page = resources.files("porch_hub").joinpath(f"dashboard/{name}").read_text(encoding="utf-8")
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home() -> HTMLResponse:
    """The home page: every page the hub serves, what each is for, and the hub's live status."""
    return _page("home.html")


@router.get("/nav.js", include_in_schema=False)
async def nav_script() -> Response:
    """The Pages menu shared by every page."""
    script = resources.files("porch_hub").joinpath("dashboard/nav.js").read_text(encoding="utf-8")
    return Response(script, media_type="text/javascript", headers={"Cache-Control": "no-store"})


@router.get("/energy.js", include_in_schema=False)
async def energy_script() -> Response:
    """The energy panel the twin pages share."""
    script = resources.files("porch_hub").joinpath("dashboard/energy.js").read_text(encoding="utf-8")
    return Response(script, media_type="text/javascript", headers={"Cache-Control": "no-store"})


@router.get("/agents.js", include_in_schema=False)
async def agents_script() -> Response:
    """The household's agents and what each asks Porch, live, for the digital twin and the 3D home."""
    script = resources.files("porch_hub").joinpath("dashboard/agents.js").read_text(encoding="utf-8")
    return Response(script, media_type="text/javascript", headers={"Cache-Control": "no-store"})


@router.get("/details.js", include_in_schema=False)
async def details_script() -> Response:
    """The device panels' "everything the platform reports" element."""
    script = resources.files("porch_hub").joinpath("dashboard/details.js").read_text(encoding="utf-8")
    return Response(script, media_type="text/javascript", headers={"Cache-Control": "no-store"})


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> HTMLResponse:
    """The live dashboard: devices, commands and read-back."""
    return _page("index.html")


@router.get("/digital-twin", response_class=HTMLResponse, include_in_schema=False)
async def digital_twin() -> HTMLResponse:
    """The 2D digital twin: floor plan, physical vs digital-copy layers, Engine pulses, stage scenarios."""
    return _page("digital-twin.html")


@router.get("/home-3d", response_class=HTMLResponse, include_in_schema=False)
async def home_3d() -> HTMLResponse:
    """The 3D digital twin: the home and its devices in Three.js, live from /ws/events."""
    return _page("home-3d.html")


# Three.js, served by the hub so the 3D page needs no internet. Only these files, by name.
VENDOR_THREE = {"three.module.min.js", "OrbitControls.js"}


@router.get("/vendor/three/{name}", include_in_schema=False)
async def vendor_three(name: str) -> Response:
    if name not in VENDOR_THREE:
        raise HTTPException(404, "not found")
    script = resources.files("porch_hub").joinpath(f"dashboard/vendor/three/{name}").read_bytes()
    return Response(script, media_type="text/javascript", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/porch", response_class=HTMLResponse, include_in_schema=False)
async def porch_at_work() -> HTMLResponse:
    """How Porch works: the agents, Porch's rules and the people at home, and the devices, live."""
    return _page("porch.html")


@router.get("/flow", response_class=HTMLResponse, include_in_schema=False)
async def live_flow() -> HTMLResponse:
    """Each agent request drawn check by check as it moves through Porch, from the gateway's stage feed."""
    return _page("flow.html")


@router.get("/live", response_class=HTMLResponse, include_in_schema=False)
async def live_both() -> HTMLResponse:
    """The Live flow and the 3D home side by side: what Porch decides, and what it does in the home."""
    return _page("live.html")


@router.get("/stage", response_class=HTMLResponse, include_in_schema=False)
async def stage() -> HTMLResponse:
    """The projector display for live demos: agents, safety alerts, hardware, digital copy, audit."""
    return _page("stage.html")


@router.websocket("/events")
@router.websocket("/ws/events")
async def stream_events(websocket: WebSocket) -> None:
    """Every device change as it happens: one DeviceEvent JSON object per message.

    Events start from the moment of connecting; read GET /devices for the state before that.
    A client that cannot keep up gets {"type": "resync_required"} and the socket closes with
    code 1013: read GET /devices again, then reconnect.
    """
    hub: Hub = websocket.app.state.hub
    await websocket.accept()
    with hub.registry.events.subscribe() as subscription:
        sender = asyncio.create_task(_send_events(websocket, subscription))
        try:
            # The client sends nothing; reading notices when it goes away.
            while (await websocket.receive())["type"] != "websocket.disconnect":
                pass
        finally:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
                await sender


async def _send_events(websocket: WebSocket, subscription: Subscription) -> None:
    try:
        async for event in subscription:
            await websocket.send_text(event.model_dump_json(exclude_none=True))
    except SubscriberLagged as exc:
        await websocket.send_json({"type": "resync_required", "detail": str(exc)})
        await websocket.close(code=1013)  # Try Again Later
