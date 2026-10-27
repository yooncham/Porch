"""LG ThinQ driver: discovery, guarded commands, and state kept current by MQTT push.

The driver talks to LG only through a ThinQCloud: ThinQConnectCloud (LG's
SDK, connect.py) in production, FakeThinQCloud (fake.py) in tests and the
offline demo. Neither the router nor the Engine ever sees LG field names.
"""

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ... import clusters as c
from ...models import Clusters, ControlPath, Device, utcnow
from ..base import BaseDriver, CommandRejected, DeviceNotFound, DeviceOffline, DriverError
from .profiles import PROFILES, Status, ThinQProfile, check_writable, start_refusal

log = logging.getLogger(__name__)

# Commands that make a machine run, which LG refuses while Remote Start is off at the machine.
START_COMMANDS = frozenset({"Start", "SetDelayStart"})
# Without a live MQTT feed, a status read this recently still counts as current.
STATUS_FRESH_S = 2.0
# LG blocks clients after unusual call volumes, so re-list appliances at most this often.
DEVICE_LIST_TTL_S = 60.0


class RemoteControlDisabled(CommandRejected):
    def __init__(self) -> None:
        super().__init__("Remote Control switch disabled on physical appliance")


class ThinQCloud(Protocol):
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def list_devices(self) -> list[dict[str, Any]]: ...
    async def get_profile(self, lg_device_id: str) -> dict[str, Any]: ...
    async def get_status(self, lg_device_id: str) -> dict[str, Any] | list[dict[str, Any]]: ...
    async def control(self, lg_device_id: str, payload: dict[str, Any]) -> None: ...

    async def subscribe(
        self,
        lg_device_ids: list[str],
        on_message: Callable[[dict[str, Any]], None],
        on_connection_change: Callable[[bool], None],
    ) -> None:
        """Start push delivery. Callbacks must run on the hub's event loop."""


@dataclass(frozen=True)
class ThinQBinding:
    """Where an LG appliance sits in the Porch home. LG itself has no rooms."""

    porch_id: str
    room: str


@dataclass
class _Appliance:
    lg_id: str
    porch_id: str
    room: str
    alias: str
    profile: ThinQProfile
    properties: dict[str, Any]  # the LG profile's property map, for write checks
    status: Status | None = None  # merged from API reads and MQTT reports
    status_at: float = 0.0  # time.monotonic() of the last read or report
    generation: int = 0  # bumped on every change, so a slow read cannot undo a newer report
    online: bool = True
    updated_at: datetime = field(default_factory=utcnow)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "appliance"


def _merge(status: Status, report: Status) -> None:
    """Apply a partial MQTT report: LG sends only the resources that changed."""
    for resource, values in report.items():
        if isinstance(values, dict) and isinstance(status.get(resource), dict):
            status[resource].update(values)
        else:
            status[resource] = values


class ThinQDriver(BaseDriver):
    control_path = ControlPath.LG_THINQ

    def __init__(
        self,
        cloud: ThinQCloud,
        home_id: str = "home-1",
        bindings: dict[str, ThinQBinding] | None = None,
        read_back_interval_s: float | None = 1.0,
        read_back_timeout_s: float | None = 10.0,
    ) -> None:
        super().__init__()
        self._cloud = cloud
        self._home_id = home_id
        self._bindings = bindings or {}
        self.read_back_interval_s = read_back_interval_s
        self.read_back_timeout_s = read_back_timeout_s
        self._appliances: dict[str, _Appliance] = {}  # by Porch id
        self._by_lg_id: dict[str, _Appliance] = {}
        self._listed_at: float | None = None
        self._push_connected = False
        self._ever_connected = False
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        await self._cloud.start()
        await self._discover()
        # Appliances registered with LG after startup get push updates from the next restart.
        await self._cloud.subscribe(list(self._by_lg_id), self._on_push, self._on_connection_change)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._push_connected = False
        await self._cloud.close()

    async def list_devices(self) -> list[Device]:
        if self._listed_at is None or time.monotonic() - self._listed_at > DEVICE_LIST_TTL_S:
            await self._discover()
        for appliance in self._appliances.values():
            if not self._is_current(appliance):
                await self._refresh(appliance)
        return [self._to_device(a) for a in self._appliances.values()]

    async def read_state(self, device_id: str) -> Clusters:
        appliance = self._appliance(device_id)
        # Confirm from what LG just answered, not from the cache a push may have moved on.
        status = await self._refresh(appliance, raise_offline=True)
        return appliance.profile.to_clusters(status)

    async def send_command(self, device_id: str, command: str, params: dict[str, Any]) -> None:
        appliance = self._appliance(device_id)
        if command in START_COMMANDS:
            if not self._is_current(appliance):
                await self._refresh(appliance, raise_offline=True)
            self._check_start(appliance, appliance.profile.to_clusters(appliance.status))
        payload = appliance.profile.to_payload(command, params)
        check_writable(appliance.properties, payload, appliance.alias)

        started = time.perf_counter()
        await self._cloud.control(appliance.lg_id, appliance.profile.wrap(payload))
        log.info("LG accepted %s for %s in %d ms", command, device_id,
                 round((time.perf_counter() - started) * 1000))

    def preflight(self, device_id: str, command: str, params: dict[str, Any], clusters: Clusters) -> None:
        appliance = self._appliances.get(device_id)
        if command in START_COMMANDS:
            # Offline, the driver may know nothing yet; the copy's clusters still say whether a start is safe.
            requires = appliance.profile.requires_remote_switch if appliance else c.PORCH_LAUNDRY in clusters
            self._raise_start_refusal(clusters, requires)
        if appliance is not None:
            check_writable(appliance.properties, appliance.profile.to_payload(command, params), appliance.alias)

    def _check_start(self, appliance: _Appliance, clusters: Clusters) -> None:
        self._raise_start_refusal(clusters, appliance.profile.requires_remote_switch)

    @staticmethod
    def _raise_start_refusal(clusters: Clusters, requires_remote_switch: bool) -> None:
        extension = clusters.get(c.PORCH_LAUNDRY)
        remote = extension.attributes.get("RemoteStartAllowed") if extension else None
        state = next((cl.attributes.get("OperationalState") for name, cl in clusters.items()
                      if name in (c.OPERATIONAL_STATE, c.RVC_OPERATIONAL_STATE)), None)
        reason = start_refusal(remote, state, requires_remote_switch)
        if reason == "Remote Control switch disabled on physical appliance":
            raise RemoteControlDisabled()
        if reason is not None:
            raise CommandRejected(reason)

    # Discovery and status

    async def _discover(self) -> None:
        found: dict[str, _Appliance] = {}
        for entry in await self._cloud.list_devices():
            lg_id = entry["deviceId"]
            info = entry.get("deviceInfo", {})
            profile = PROFILES.get(info.get("deviceType"))
            if profile is None:
                log.debug("skipping LG %s %s: no Matter mapping yet", info.get("deviceType"), lg_id)
                continue
            alias = info.get("alias") or info.get("modelName") or lg_id
            if (known := self._by_lg_id.get(lg_id)) is not None:
                known.alias = alias
                found[lg_id] = known
                continue
            binding = self._bindings.get(lg_id) or ThinQBinding(
                f"{self._home_id}/{_slug(alias)}-{lg_id[:6].lower()}", "Unassigned"
            )
            lg_profile = await self._cloud.get_profile(lg_id)
            found[lg_id] = _Appliance(
                lg_id=lg_id, porch_id=binding.porch_id, room=binding.room, alias=alias,
                profile=profile, properties=profile.select(lg_profile.get("property", {})),
            )
        self._by_lg_id = found
        self._appliances = {a.porch_id: a for a in found.values()}
        self._listed_at = time.monotonic()

    async def _refresh(self, appliance: _Appliance, raise_offline: bool = False) -> Status | None:
        """Read LG's status and return it. The cache keeps it only if no report came in meanwhile."""
        generation = appliance.generation
        try:
            raw = await self._cloud.get_status(appliance.lg_id)
        except DeviceOffline:
            if appliance.generation == generation:
                appliance.online = False
            if raise_offline:
                raise
            return None
        status = appliance.profile.select(raw)
        if appliance.generation == generation:
            self._record(appliance, status)
        else:
            log.debug("kept a newer MQTT report for %s over a slower status read", appliance.porch_id)
        return status

    def _record(self, appliance: _Appliance, status: Status) -> None:
        if status != appliance.status:
            appliance.updated_at = utcnow()
        appliance.status = status
        appliance.status_at = time.monotonic()
        appliance.generation += 1
        appliance.online = True

    def _is_current(self, appliance: _Appliance) -> bool:
        if appliance.status is None:
            return False
        return self._push_connected or time.monotonic() - appliance.status_at <= STATUS_FRESH_S

    def _appliance(self, device_id: str) -> _Appliance:
        try:
            return self._appliances[device_id]
        except KeyError:
            raise DeviceNotFound(f"lg_thinq has no device {device_id!r}") from None

    def _to_device(self, appliance: _Appliance) -> Device:
        clusters = appliance.profile.to_clusters(appliance.status) if appliance.status is not None else {}
        return Device(
            id=appliance.porch_id, name=appliance.alias, room=appliance.room,
            device_type=appliance.profile.device_type, online=appliance.online,
            primary_control_path=self.control_path, clusters=clusters,
            last_updated_at=appliance.updated_at,
        )

    # MQTT push

    def _on_push(self, message: dict[str, Any]) -> None:
        try:
            self._apply_push(message)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:  # one bad message must not stop the feed
            log.warning("ignored a malformed LG MQTT message: %s", exc)

    def _apply_push(self, message: dict[str, Any]) -> None:
        if not isinstance(message, dict):
            raise TypeError(f"expected an object, got {type(message).__name__}")
        push_type = message.get("pushType")
        appliance = self._by_lg_id.get(message.get("deviceId", ""))
        if appliance is None:
            return
        if push_type != "DEVICE_STATUS":
            # DEVICE_PUSH carries notifications such as a finished cycle; the event stream will forward them.
            log.info("LG %s for %s: %s", push_type, appliance.porch_id, message.get("pushCode"))
            return
        if appliance.status is None:
            # A partial report needs a full status to merge into. Mark the change, so a read
            # already in flight is not cached as current, and fetch a full status now.
            appliance.generation += 1
            self._spawn(self._refresh(appliance))
            return
        status = {k: dict(v) if isinstance(v, dict) else v for k, v in appliance.status.items()}
        _merge(status, appliance.profile.select(message.get("report", {})))
        self._record(appliance, status)
        self._publish_state(appliance.porch_id, appliance.profile.to_clusters(status))

    def _on_connection_change(self, connected: bool) -> None:
        self._push_connected = connected
        if not connected:
            log.warning("LG MQTT disconnected; reading state from the API until it reconnects")
            return
        if self._ever_connected:
            # Reports sent while disconnected are lost, so re-read everything once.
            self._spawn(self._resync())
        self._ever_connected = True

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _resync(self) -> None:
        for appliance in list(self._appliances.values()):
            try:
                await self._refresh(appliance)
            except DriverError as exc:
                log.warning("resync of %s failed: %s", appliance.porch_id, exc)
                continue
            if appliance.status is not None:
                self._publish_state(appliance.porch_id, appliance.profile.to_clusters(appliance.status))
