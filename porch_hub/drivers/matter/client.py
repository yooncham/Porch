"""WebSocket client for the Open Home Foundation Matter Server (matterjs-server).

Speaks the server's JSON protocol (docs/websockets_api.md in matterjs-server,
compatible with the Python Matter Server): requests carry a message_id and get
a result or an error_code back; after start_listening the server sends every
node and then forwards events (attribute_updated, node_event, node_updated...).
The client reconnects with backoff and re-listens after every reconnect, so
the driver gets a full node snapshot each time.
"""

import asyncio
import itertools
import json
import logging
from collections.abc import Callable
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from ..base import CommandRejected, DeviceNotFound, DeviceOffline, DriverError

log = logging.getLogger(__name__)

# start_listening, read_attribute and device_command are all in schema 11.
MIN_SCHEMA = 11
# Node data for a home can exceed the websockets default of 1 MiB.
MAX_MESSAGE_BYTES = 32 * 1024 * 1024

# Error codes, shared with the Python Matter Server.
NODE_NOT_READY, NODE_NOT_RESOLVING, NODE_NOT_EXISTS = 3, 4, 5
INVALID_ARGUMENTS, INVALID_COMMAND = 8, 9


class MatterServerError(DriverError):
    def __init__(self, message: str, error_code: int | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status = status


class MatterNodeOffline(MatterServerError, DeviceOffline):
    pass


class MatterNodeNotFound(MatterServerError, DeviceNotFound):
    pass


class MatterCommandRejected(MatterServerError, CommandRejected):
    pass


def _error(command: str, message: dict[str, Any]) -> MatterServerError:
    code = message.get("error_code")
    status = message.get("status")
    text = f"Matter Server {command} failed: {message.get('details') or 'no details'}"
    if code in (NODE_NOT_READY, NODE_NOT_RESOLVING):
        return MatterNodeOffline(text, code, status)
    if code == NODE_NOT_EXISTS:
        return MatterNodeNotFound(text, code, status)
    if status is not None or code in (INVALID_ARGUMENTS, INVALID_COMMAND):
        # status is the Matter status the device answered with, for example 0x81 UNSUPPORTED_COMMAND.
        suffix = f" (Matter status 0x{status:02X})" if status is not None else ""
        return MatterCommandRejected(text + suffix, code, status)
    return MatterServerError(text, code, status)


class MatterServerClient:
    def __init__(
        self,
        url: str,
        on_nodes: Callable[[list[dict[str, Any]]], None],
        on_event: Callable[[str, Any], None],
        on_connection_change: Callable[[bool], None],
        request_timeout_s: float = 30.0,
        reconnect_delay_s: float = 1.0,
        max_reconnect_delay_s: float = 30.0,
    ) -> None:
        self._url = url
        self._on_nodes = on_nodes
        self._on_event = on_event
        self._on_connection_change = on_connection_change
        self._request_timeout_s = request_timeout_s
        self._reconnect_delay_s = reconnect_delay_s
        self._max_reconnect_delay_s = max_reconnect_delay_s
        self._ws: ClientConnection | None = None
        self._pending: dict[str, tuple[str, asyncio.Future]] = {}  # message_id -> (command, result)
        self._ids = itertools.count(1)
        self._listen_id: str | None = None
        self._runner: asyncio.Task | None = None
        self._listening = asyncio.Event()
        self._closing = False
        self.server_info: dict[str, Any] = {}

    @property
    def connected(self) -> bool:
        return self._listening.is_set()

    async def start(self, connect_timeout_s: float = 5.0) -> None:
        """Connect and start listening. If the server is not up yet, keep trying in the background."""
        self._closing = False  # the client can be started again after close(), as offline mode does
        self._listening.clear()
        self._runner = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self._listening.wait(), connect_timeout_s)
        except TimeoutError:
            log.warning("Matter Server not reachable at %s yet; retrying in the background", self._url)

    async def close(self) -> None:
        self._closing = True
        if self._ws is not None:
            await self._ws.close()
        if self._runner is not None:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)

    async def command(self, command: str, **args: Any) -> Any:
        """Send one request and wait for its result. Raises a DriverError subclass on any failure."""
        ws = self._ws
        if ws is None or (not self.connected and command != "start_listening"):
            raise DriverError(f"Matter Server at {self._url} is not connected")
        message_id = str(next(self._ids))
        future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = (command, future)
        if command == "start_listening":
            self._listen_id = message_id
        try:
            await ws.send(json.dumps({"message_id": message_id, "command": command, "args": args}))
            return await asyncio.wait_for(future, self._request_timeout_s)
        except TimeoutError:
            raise DriverError(f"Matter Server did not answer {command} within {self._request_timeout_s:g}s") from None
        except WebSocketException as exc:
            raise DriverError(f"Matter Server connection lost during {command}: {exc}") from exc
        finally:
            self._pending.pop(message_id, None)

    async def _run(self) -> None:
        delay = self._reconnect_delay_s
        while not self._closing:
            try:
                async with connect(self._url, max_size=MAX_MESSAGE_BYTES, open_timeout=10, close_timeout=2) as ws:
                    self._ws = ws
                    self.server_info = json.loads(await ws.recv())
                    schema = self.server_info.get("schema_version", 0)
                    if schema < MIN_SCHEMA:
                        raise DriverError(f"Matter Server schema {schema} is older than {MIN_SCHEMA}")
                    log.info("connected to Matter Server %s (schema %s)", self.server_info.get("sdk_version"), schema)
                    reader = asyncio.create_task(self._read(ws))
                    await self.command("start_listening")  # the reader hands the nodes over in order
                    delay = self._reconnect_delay_s
                    await reader
            except asyncio.CancelledError:
                raise
            except (OSError, WebSocketException, DriverError, TimeoutError, ValueError) as exc:
                if not self._closing:
                    log.warning("Matter Server connection to %s failed: %s", self._url, exc)
            finally:
                self._ws = None
                self._listen_id = None
                for _, future in self._pending.values():
                    if not future.done():
                        future.set_exception(DriverError("Matter Server connection lost"))
                if self._listening.is_set():
                    self._listening.clear()
                    if not self._closing:
                        self._on_connection_change(False)
            if not self._closing:
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay_s)

    async def _read(self, ws: ClientConnection) -> None:
        async for raw in ws:
            try:
                message = json.loads(raw)
            except ValueError:
                log.warning("unreadable Matter Server message")
                continue
            if "event" in message:
                self._dispatch_event(message["event"], message.get("data"))
                continue
            message_id = message.get("message_id")
            if message_id == self._listen_id and "result" in message:
                # Hand the snapshot over before reading on, so no later event is applied to an older snapshot.
                self._listening.set()
                self._dispatch(self._on_nodes, message["result"])
                self._on_connection_change(True)
            command, future = self._pending.get(message_id, ("", None))
            if future is None or future.done():
                continue
            if "error_code" in message:
                future.set_exception(_error(command, message))
            else:
                future.set_result(message.get("result"))

    def _dispatch_event(self, event: str, data: Any) -> None:
        self._dispatch(self._on_event, event, data)

    @staticmethod
    def _dispatch(handler: Callable[..., None], *args: Any) -> None:
        try:
            handler(*args)
        except Exception:  # one bad message must not stop the reader
            log.exception("handling Matter Server message failed")
