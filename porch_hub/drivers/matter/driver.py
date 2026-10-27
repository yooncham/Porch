"""Direct Matter driver: Porch's own fabric through a local Matter Server.

Control is local and needs no cloud, state changes arrive as they happen, and
this is the only path that can unlock a door. The Matter Server runs on the
home node (npx matter-server, or Docker) and serves ws://localhost:5580/ws.
"""

import json
import logging
import os
from urllib.parse import urlsplit, urlunsplit
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from ...models import Clusters, ControlPath, Device, utcnow
from ..base import BaseDriver, CommandRejected, DeviceNotFound, DeviceOffline, DriverError
from .client import MatterServerClient
from .mapping import BOOLEAN_STATE_ID, CLUSTERS, MatterBinding, MatterEndpoint, node_endpoints, parse_path

log = logging.getLogger(__name__)

DEFAULT_URL = "ws://localhost:5580/ws"
# Window between the Timed Request and the invoke for timed commands such as LockDoor.
TIMED_REQUEST_MS = 5000


class MatterDriver(BaseDriver):
    control_path = ControlPath.MATTER

    def __init__(self, url: str = DEFAULT_URL, home_id: str = "home-1",
                 bindings: dict[str, MatterBinding] | None = None,
                 connect_timeout_s: float = 5.0, reconnect_delay_s: float = 1.0) -> None:
        super().__init__()
        url = server_url(url)
        self._home_id = home_id
        self._bindings = bindings or {}
        self._connect_timeout_s = connect_timeout_s
        self._client = MatterServerClient(url, self._on_nodes, self._on_event, self._on_connection_change,
                                          reconnect_delay_s=reconnect_delay_s)
        self._nodes: dict[int, dict[str, Any]] = {}
        self._endpoints: dict[str, MatterEndpoint] = {}  # by Porch device id
        self._by_location: dict[tuple[int, int], MatterEndpoint] = {}  # by (node, endpoint)
        self._updated_at: dict[str, datetime] = {}
        self._versions: dict[tuple[int, str], int] = {}  # (node, path) -> changes seen
        self._last_clusters: dict[str, Clusters] = {}

    async def start(self) -> None:
        await self._client.start(self._connect_timeout_s)

    async def stop(self) -> None:
        await self._client.close()

    async def list_devices(self) -> list[Device]:
        # Node data stays current from the server's pushes, so listing needs no device reads.
        return [self._to_device(ep) for ep in self._endpoints.values()]

    async def read_state(self, device_id: str) -> Clusters:
        endpoint = self._endpoint(device_id)
        if not self._online(endpoint):
            raise DeviceOffline(f"{device_id} is not reachable on the Matter fabric")
        paths = endpoint.attribute_paths()
        versions = {path: self._versions.get((endpoint.node_id, path), 0) for path in paths}
        values = await self._client.command("read_attribute", node_id=endpoint.node_id, attribute_path=paths)
        if not isinstance(values, dict) or (missing := [p for p in paths if p not in values]):
            missing = paths if not isinstance(values, dict) else missing
            raise DriverError(f"Matter Server did not return {', '.join(missing)} for {device_id}")
        node = self._nodes.get(endpoint.node_id)
        if node is not None:
            for path, value in values.items():
                # A report that arrived while the read was in flight is newer; keep it.
                if self._versions.get((endpoint.node_id, path), 0) == versions.get(path, 0):
                    node["attributes"][path] = value
            self._snapshot(endpoint)
            # Confirm from what the device just answered, not from the cache.
            return endpoint.to_clusters({**node["attributes"], **values})
        return endpoint.to_clusters(values)

    async def send_command(self, device_id: str, command: str, params: dict[str, Any]) -> None:
        endpoint = self._endpoint(device_id)
        target = endpoint.command_target(command)
        if target is None:
            raise CommandRejected(f"{device_id} has no Matter cluster for {command}")
        if not self._online(endpoint):
            raise DeviceOffline(f"{device_id} is not reachable on the Matter fabric")
        args: dict[str, Any] = {
            "node_id": endpoint.node_id, "endpoint_id": endpoint.endpoint_id,
            "cluster_id": target.cluster_id, "command_name": command, "payload": {},
        }
        if command in target.timed_commands:
            args["timed_request_timeout_ms"] = TIMED_REQUEST_MS
        await self._client.command("device_command", **args)

    def preflight(self, device_id: str, command: str, params: dict[str, Any], clusters: Clusters) -> None:
        endpoint = self._endpoints.get(device_id)
        if endpoint is not None and endpoint.command_target(command) is None:
            raise CommandRejected(f"{device_id} has no Matter cluster for {command}")

    # Server pushes, on the hub's event loop

    def _on_nodes(self, nodes: list[dict[str, Any]]) -> None:
        """A full snapshot, after every (re)connect."""
        for node in nodes:
            self._bump(node["node_id"], node.get("attributes", {}))
        self._nodes = {node["node_id"]: node for node in nodes}
        self._index()
        for endpoint in self._endpoints.values():
            self._publish_online(endpoint.device_id, self._online(endpoint))
            self._push_state(endpoint)

    def _on_event(self, event: str, data: Any) -> None:
        match event:
            case "attribute_updated":
                node_id, path, value = data
                self._on_attribute(node_id, path, value)
            case "node_added" | "node_updated":
                self._bump(data["node_id"], data.get("attributes", {}))
                self._nodes[data["node_id"]] = data
                self._index()
                for endpoint in self._node_endpoints(data["node_id"]):
                    self._publish_online(endpoint.device_id, self._online(endpoint))
                    self._push_state(endpoint)
            case "node_removed":
                gone = self._node_endpoints(data)
                self._nodes.pop(data, None)
                self._index()
                for endpoint in gone:
                    self._publish_online(endpoint.device_id, False)
            case "node_event":
                self._on_node_event(data)

    def _on_attribute(self, node_id: int, path: str, value: Any) -> None:
        node = self._nodes.get(node_id)
        parsed = parse_path(path)
        if node is None or parsed is None:
            return
        node["attributes"][path] = value
        self._bump(node_id, [path])
        endpoint = self._by_location.get((node_id, parsed[0]))
        if endpoint is not None and parsed[1] in endpoint.cluster_ids:
            self._push_state(endpoint)

    def _on_node_event(self, data: dict[str, Any]) -> None:
        endpoint = self._by_location.get((data.get("node_id"), data.get("endpoint_id")))
        cmap = CLUSTERS.get(data.get("cluster_id"))
        if endpoint is None or cmap is None or (name := cmap.events.get(data.get("event_id"))) is None:
            return
        payload = data.get("data") or {}
        if cmap.cluster_id == BOOLEAN_STATE_ID and name == "StateChange":
            # The event carries the new value; apply it in case the attribute report comes later.
            value = payload.get("stateValue", payload.get("0"))
            if value is not None:
                self._on_attribute(endpoint.node_id, f"{endpoint.endpoint_id}/{BOOLEAN_STATE_ID}/0", value)
        self._publish_event(endpoint.device_id, cmap.name, name, payload)

    def _on_connection_change(self, connected: bool) -> None:
        if not connected:
            log.warning("lost the Matter Server; Matter devices are unreachable until it reconnects")
            for endpoint in self._endpoints.values():
                self._publish_online(endpoint.device_id, False)
        # On reconnect the client delivers a fresh snapshot through _on_nodes.

    # Helpers

    def _bump(self, node_id: int, paths: Any) -> None:
        for path in paths:
            key = (node_id, path)
            self._versions[key] = self._versions.get(key, 0) + 1

    def _index(self) -> None:
        self._endpoints = {}
        self._by_location = {}
        for node in self._nodes.values():
            for endpoint in node_endpoints(node, self._home_id, self._bindings):
                self._endpoints[endpoint.device_id] = endpoint
                self._by_location[(endpoint.node_id, endpoint.endpoint_id)] = endpoint

    def _node_endpoints(self, node_id: int) -> list[MatterEndpoint]:
        return [ep for ep in self._endpoints.values() if ep.node_id == node_id]

    def _snapshot(self, endpoint: MatterEndpoint) -> Clusters:
        clusters = endpoint.to_clusters(self._nodes[endpoint.node_id]["attributes"])
        if clusters != self._last_clusters.get(endpoint.device_id):
            self._last_clusters[endpoint.device_id] = clusters
            self._updated_at[endpoint.device_id] = utcnow()
        return clusters

    def _push_state(self, endpoint: MatterEndpoint) -> None:
        self._publish_state(endpoint.device_id, self._snapshot(endpoint))

    def _online(self, endpoint: MatterEndpoint) -> bool:
        return self._client.connected and bool(self._nodes.get(endpoint.node_id, {}).get("available"))

    def _endpoint(self, device_id: str) -> MatterEndpoint:
        try:
            return self._endpoints[device_id]
        except KeyError:
            raise DeviceNotFound(f"matter has no device {device_id!r}") from None

    def _to_device(self, endpoint: MatterEndpoint) -> Device:
        clusters = self._snapshot(endpoint)
        return Device(
            id=endpoint.device_id, name=endpoint.name, room=endpoint.room,
            device_type=endpoint.device_type, online=self._online(endpoint),
            primary_control_path=self.control_path, clusters=clusters,
            serial_number=endpoint.serial_number, mac_address=endpoint.mac_address,
            last_updated_at=self._updated_at.get(endpoint.device_id, utcnow()),
        )


def server_url(url: str) -> str:
    """A Matter Server WebSocket URL. A bare host and port gets the server's /ws path."""
    parts = urlsplit(url)
    if parts.scheme not in ("ws", "wss") or not parts.hostname:
        raise ValueError(f"Matter Server URL must be ws:// or wss:// with a host, not {url!r}")
    if parts.username or parts.password:
        raise ValueError("credentials in the Matter Server URL are not supported")
    return urlunsplit(parts._replace(path=parts.path if parts.path not in ("", "/") else "/ws"))


def matter_driver_from_env(env: Mapping[str, str] = os.environ) -> MatterDriver:
    """Build the live Matter driver.

    MATTER_SERVER_URL   Matter Server WebSocket (default ws://localhost:5580/ws)
    MATTER_BINDINGS     JSON {"<node id>/<endpoint>": {"porch_id": "...", "room": "..."}}
    PORCH_HOME_ID       home id prefix for device ids (default home-1)
    """
    bindings = {key: MatterBinding(**b) for key, b in json.loads(env.get("MATTER_BINDINGS", "{}")).items()}
    return MatterDriver(env.get("MATTER_SERVER_URL", DEFAULT_URL), home_id=env.get("PORCH_HOME_ID", "home-1"),
                        bindings=bindings)
