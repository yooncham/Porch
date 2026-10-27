"""A stand-in Matter Server speaking the matterjs-server WebSocket protocol, for tests.

Devices act on commands the way a real node would report back: the response
first, then attribute_updated events to every listening connection.
"""

import asyncio
import copy
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve

# (cluster, command) -> (attribute, value); None means toggle.
_EFFECTS: dict[tuple[int, str], tuple[int, Any]] = {
    (6, "On"): (0, True), (6, "Off"): (0, False), (6, "Toggle"): (0, None),
    (257, "LockDoor"): (0, 1), (257, "UnlockDoor"): (0, 2),
    (129, "Open"): (4, 1), (129, "Close"): (4, 0),
}


def node(node_id: int, attributes: dict[str, Any], available: bool = True) -> dict[str, Any]:
    return {"node_id": node_id, "date_commissioned": "2026-10-20T10:00:00.000000",
            "last_interview": "2026-10-20T10:00:00.000000", "interview_version": 6,
            "available": available, "is_bridge": False, "attributes": attributes,
            "attribute_subscriptions": [], "endpoint_parents": {"1": 0}}


class FakeMatterServer:
    def __init__(self, nodes: list[dict[str, Any]], schema_version: int = 14) -> None:
        self.nodes = {n["node_id"]: copy.deepcopy(n) for n in nodes}
        self.schema_version = schema_version
        self.requests: list[dict[str, Any]] = []
        self.rejections: dict[str, dict[str, Any]] = {}  # command_name -> error fields
        self.unresponsive = False  # accept commands, never report a change
        self.read_delay_s = 0.0  # a read answers with the values at request time, after this delay
        self.omit_from_reads: set[str] = set()  # paths a read leaves out of its result
        self._listeners: set[ServerConnection] = set()
        self._server: Server | None = None

    async def start(self, port: int = 0) -> str:
        self._server = await serve(self._handle, "127.0.0.1", port)
        return f"ws://127.0.0.1:{self._server.sockets[0].getsockname()[1]}/ws"

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def commands(self, name: str) -> list[dict[str, Any]]:
        return [r["args"] for r in self.requests if r["command"] == name]

    # Things that happen on the fabric

    async def set_attribute(self, node_id: int, path: str, value: Any) -> None:
        self.nodes[node_id]["attributes"][path] = value
        await self._broadcast("attribute_updated", [node_id, path, value])

    async def node_event(self, node_id: int, endpoint_id: int, cluster_id: int, event_id: int,
                         data: dict[str, Any]) -> None:
        await self._broadcast("node_event", {
            "node_id": node_id, "endpoint_id": endpoint_id, "cluster_id": cluster_id, "event_id": event_id,
            "event_number": 1, "priority": 1, "timestamp": 1761100000000, "timestamp_type": 1, "data": data})

    async def set_available(self, node_id: int, available: bool) -> None:
        self.nodes[node_id]["available"] = available
        await self._broadcast("node_updated", copy.deepcopy(self.nodes[node_id]))

    async def send_raw(self, text: str) -> None:
        """Send any text to listening connections, malformed or not."""
        for ws in list(self._listeners):
            await ws.send(text)

    async def drop_connections(self) -> None:
        for ws in list(self._listeners):
            await ws.close()

    # Protocol

    async def _handle(self, ws: ServerConnection) -> None:
        await ws.send(json.dumps({
            "fabric_id": 1, "compressed_fabric_id": 2, "schema_version": self.schema_version,
            "min_supported_schema_version": 11, "sdk_version": "fake-matter-server",
            "wifi_credentials_set": True, "thread_credentials_set": True, "bluetooth_enabled": False}))
        try:
            async for raw in ws:
                request = json.loads(raw)
                self.requests.append(request)
                response, reports = self._respond(ws, request)
                if request["command"] == "read_attribute" and self.read_delay_s:
                    asyncio.get_running_loop().create_task(self._send_later(ws, request["message_id"], response))
                    continue
                await ws.send(json.dumps({"message_id": request["message_id"], **response}))
                for node_id, path, value in reports:
                    await self.set_attribute(node_id, path, value)
        finally:
            self._listeners.discard(ws)

    def _respond(self, ws: ServerConnection, request: dict[str, Any]) -> tuple[dict[str, Any], list]:
        args = request.get("args") or {}
        match request["command"]:
            case "start_listening":
                self._listeners.add(ws)
                return {"result": copy.deepcopy(list(self.nodes.values()))}, []
            case "read_attribute":
                if (error := self._node_error(args["node_id"])) is not None:
                    return error, []
                attributes = self.nodes[args["node_id"]]["attributes"]
                paths = args["attribute_path"]
                paths = paths if isinstance(paths, list) else [paths]
                return {"result": {p: attributes.get(p) for p in paths if p not in self.omit_from_reads}}, []
            case "device_command":
                if (error := self._node_error(args["node_id"])) is not None:
                    return error, []
                if args["command_name"] in self.rejections:
                    return self.rejections[args["command_name"]], []
                attribute, value = _EFFECTS[(args["cluster_id"], args["command_name"])]
                path = f"{args['endpoint_id']}/{args['cluster_id']}/{attribute}"
                if value is None:
                    value = not self.nodes[args["node_id"]]["attributes"].get(path)
                reports = [] if self.unresponsive else [(args["node_id"], path, value)]
                return {"result": None}, reports
        return {"error_code": 9, "details": f"Unknown command {request['command']}"}, []

    async def _send_later(self, ws: ServerConnection, message_id: str, response: dict[str, Any]) -> None:
        await asyncio.sleep(self.read_delay_s)
        await ws.send(json.dumps({"message_id": message_id, **response}))

    def _node_error(self, node_id: int) -> dict[str, Any] | None:
        if node_id not in self.nodes:
            return {"error_code": 5, "details": f"Node {node_id} does not exist"}
        if not self.nodes[node_id]["available"]:
            return {"error_code": 3, "details": f"Node {node_id} is not ready"}
        return None

    async def _broadcast(self, event: str, data: Any) -> None:
        message = json.dumps({"event": event, "data": data})
        for ws in list(self._listeners):
            await ws.send(message)


@contextmanager
def running_in_thread(server: FakeMatterServer) -> Iterator[tuple[str, Any]]:
    """Run the server on its own loop, for tests whose hub runs in TestClient's thread.

    Yields the URL and call(coro), which runs a server coroutine and waits for it.
    """
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    def call(coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, loop).result(5)

    url = call(server.start())
    try:
        yield url, call
    finally:
        call(server.stop())
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
