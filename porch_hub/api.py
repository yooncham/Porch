"""Interface v0 routes. The Porch Engine is the only caller of the command route.

Device ids contain slashes (home-1/front-door-lock), so routes use path params.
"""

import asyncio
import contextlib
from dataclasses import dataclass
from importlib import resources
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

from .digital_copy import DigitalCopy, SimulateRequest, SimulateResponse
from .events import Subscription, SubscriberLagged
from .models import Clusters, CommandRequest, CommandResponse, Device
from .registry import DeviceRegistry
from .router import CommandRouter


@dataclass
class Hub:
    registry: DeviceRegistry
    commands: CommandRouter
    digital_copy: DigitalCopy


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


@router.get("/", include_in_schema=False)
async def home() -> RedirectResponse:
    return RedirectResponse("/dashboard")


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> HTMLResponse:
    """The live dashboard: one self-contained page, served by the hub itself."""
    page = resources.files("porch_hub").joinpath("dashboard/index.html").read_text(encoding="utf-8")
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@router.get("/digital-twin", response_class=HTMLResponse, include_in_schema=False)
async def digital_twin() -> HTMLResponse:
    """The 2D digital twin: floor plan, physical vs digital-copy layers, Engine pulses, stage scenarios."""
    page = resources.files("porch_hub").joinpath("dashboard/digital-twin.html").read_text(encoding="utf-8")
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@router.get("/stage", response_class=HTMLResponse, include_in_schema=False)
async def stage() -> HTMLResponse:
    """The projector display for live demos: agents, safety alerts, hardware, digital copy, audit."""
    page = resources.files("porch_hub").joinpath("dashboard/stage.html").read_text(encoding="utf-8")
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


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
