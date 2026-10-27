"""The MCP surface the agent sees, plus the owner's approval endpoints and the stage feed."""

import functools
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import WebSocketRoute

from .engine import EngineLite, HubUnavailable

INSTRUCTIONS = """\
Porch controls real devices in the user's home: locks, appliances, plugs, sensors, a water valve.
This server is a stand-in for the Porch Engine. It checks your scope, holds risky actions for the
home owner's approval, dry-runs every action on a digital copy, then executes it and reads the
device back before answering.

How to work with it:
- Call list_devices first. Use the device_id values it returns; "you_may" lists what your scope allows.
- preview_action shows what an action would change without touching the device.
- perform_action carries it out. Answer the user from confirmed_state and latency_ms in the result:
  that is what the device reported after the command, not a guess.
- "needs_owner_approval": tell the user the owner must approve; retry with the approval_id once they have.
- "denied" or "would_fail": explain the reason to the user; do not try to work around it.
"""


def _errors(tool):
    """Turn hub and lookup failures into a result the agent can read and act on."""
    @functools.wraps(tool)
    async def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await tool(*args, **kwargs)
        except (LookupError, ValueError, HubUnavailable) as exc:
            return {"status": "error", "error": str(exc)}
    return wrapped


def build_server(engine: EngineLite) -> MCPServer:
    server = MCPServer("porch", title="Porch Engine (stand-in for HW6)", instructions=INSTRUCTIONS)

    @server.tool(annotations=ToolAnnotations(title="List home devices", readOnlyHint=True))
    @_errors
    async def list_devices() -> dict[str, Any]:
        """List the devices in the home with their current state, and which commands you may use on each."""
        return await engine.list_devices()

    @server.tool(annotations=ToolAnnotations(title="Preview an action", readOnlyHint=True))
    @_errors
    async def preview_action(device_id: str, command: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Dry-run a command on the home's digital copy: the predicted state, whether it would succeed, the
        estimated latency, and whether your scope allows it. Nothing is sent to the device.

        command is a Matter-style name, e.g. LockDoor, UnlockDoor, On, Off, Toggle, Open, Close, Start, Stop,
        SetDelayStart (params {"hours": 4}).
        """
        return await engine.preview_action(device_id, command, params or {})

    @server.tool(annotations=ToolAnnotations(title="Perform an action", destructiveHint=True, idempotentHint=False))
    @_errors
    async def perform_action(device_id: str, command: str, reason: str, params: dict[str, Any] | None = None,
                             approval_id: str | None = None) -> dict[str, Any]:
        """Carry out a command on a real device. Porch checks your scope, holds risky actions (such as unlocking
        a door) for the owner's approval, dry-runs it, executes it, and reads the device back.

        reason: one sentence on why, for the owner's audit log.
        approval_id: only when retrying an action the owner has approved.
        Returns status done/failed with confirmed_state (the device's state after the command) and latency_ms,
        or denied / needs_owner_approval / would_fail with the reason. Nothing is sent unless status is
        done or failed.
        """
        return await engine.perform_action(device_id, command, params or {}, reason, approval_id)

    # The owner answers here, outside MCP, so an agent can never approve its own request.
    @server.custom_route("/approvals", methods=["GET"])
    async def pending(request: Request) -> JSONResponse:
        return JSONResponse({"pending": engine.approvals()})

    @server.custom_route("/approvals/{approval_id}/approve", methods=["POST"])
    async def approve(request: Request) -> JSONResponse:
        return JSONResponse(await engine.answer(request.path_params["approval_id"], approved=True))

    @server.custom_route("/approvals/{approval_id}/deny", methods=["POST"])
    async def deny(request: Request) -> JSONResponse:
        return JSONResponse(await engine.answer(request.path_params["approval_id"], approved=False))

    return server


def build_http_app(server: MCPServer, stage_endpoint: Any, host: str) -> Starlette:
    """Streamable HTTP for MCP at /mcp, the approval routes, and the stage display feed at /ws/stage."""
    app = server.streamable_http_app(host=host)
    app.router.routes.append(WebSocketRoute("/ws/stage", stage_endpoint))
    return app
