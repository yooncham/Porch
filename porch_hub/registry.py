import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .dedup import PATH_PRIORITY, combine, merge_device_records
from .drivers import BaseDriver, DeviceNotFound, DeviceOffline
from .events import EventBus
from .models import Clusters, ControlPath, Device, utcnow

log = logging.getLogger(__name__)


def _changes(before: Clusters, after: Clusters) -> dict[str, dict[str, Any]]:
    """Attributes in after that are new or differ from before, by cluster."""
    changed: dict[str, dict[str, Any]] = {}
    for name, cluster in after.items():
        old = before[name].attributes if name in before else {}
        diff = {k: v for k, v in cluster.attributes.items() if k not in old or old[k] != v}
        if diff:
            changed[name] = diff
    return changed


@dataclass(frozen=True)
class Route:
    """One way to reach a device: a platform, and that platform's own id and record for it."""

    path: ControlPath
    device_id: str
    driver: BaseDriver
    record: Device


class _PathListener:
    """Tags what one driver pushes with its path, so the registry knows which record changed."""

    def __init__(self, registry: "DeviceRegistry", path: ControlPath) -> None:
        self._registry = registry
        self._path = path

    def on_state(self, device_id: str, clusters: Clusters) -> None:
        self._registry.record_path_state(self._path, device_id, clusters)

    def on_online(self, device_id: str, online: bool) -> None:
        self._registry.record_path_online(self._path, device_id, online)

    def on_device_event(self, device_id: str, cluster: str, event: str, data: dict[str, Any]) -> None:
        self._registry.record_path_event(self._path, device_id, cluster, event, data)


class DeviceRegistry:
    """The hub's single view of every device, merged across platforms.

    It keeps each platform's own record and, from them, one unified record per
    physical device (see dedup.py). It listens to every driver, so pushed
    changes land here at once, and it publishes each change on the event bus.

    In offline mode the registry is the digital copy alone: it stops listing,
    reading and listening to drivers, and only write_shadow_state changes it.
    """

    def __init__(self, drivers: Sequence[BaseDriver], events: EventBus | None = None) -> None:
        self._drivers = {d.control_path: d for d in drivers}
        self._sources: dict[ControlPath, dict[str, Device]] = {path: {} for path in self._drivers}
        self._devices: dict[str, Device] = {}
        self._owners: dict[tuple[ControlPath, str], str] = {}  # (path, platform id) -> unified id
        self.events = events or EventBus()
        self.change_listeners: list[Callable[[], None]] = []  # told after any change, e.g. to save the copy
        self.offline_mode = False
        self.driver_timeout_s = 10.0
        for driver in drivers:
            driver.add_listener(_PathListener(self, driver.control_path))

    async def refresh(self) -> list[Device]:
        """Re-list every platform and merge. A platform that fails keeps its last known devices."""
        if self.offline_mode:
            return list(self._devices.values())
        paths = list(self._drivers)
        results = await asyncio.gather(*(self._drivers[p].list_devices() for p in paths), return_exceptions=True)
        for path, result in zip(paths, results):
            if isinstance(result, BaseException):
                log.warning("listing %s failed, keeping its last known devices as offline: %s", path, result)
                # Their identities still merge with other paths, so a cloud path can take over.
                for record in self._sources[path].values():
                    record.online = False
                continue
            self._sources[path] = {d.id: d for d in result}
        self._rebuild()
        return list(self._devices.values())

    def _rebuild(self) -> None:
        """Merge the platform records, publishing what changed for devices already known."""
        previous = self._devices
        devices: dict[str, Device] = {}
        owners: dict[tuple[ControlPath, str], str] = {}
        records = [r for p in PATH_PRIORITY if p in self._sources for r in self._sources[p].values()]
        for device in merge_device_records(records):
            if device.id in devices:
                log.warning("two devices share the id %s; keeping the one from %s",
                            device.id, devices[device.id].control_paths)
                continue
            devices[device.id] = device
            for path, platform_id in device.path_device_ids.items():
                owners[(path, platform_id)] = device.id
        self._devices = devices
        self._owners = owners
        for device_id, device in devices.items():
            if (old := previous.get(device_id)) is None:
                continue
            if changed := _changes(old.clusters, device.clusters):
                self.events.publish("state_changed", device_id, device.primary_control_path,
                                    changed=changed, clusters=device.clusters)
            if old.online != device.online:
                self.events.publish("online_changed", device_id, device.primary_control_path, online=device.online)

    # Digital copy

    def source_records(self) -> list[Device]:
        """Every platform's own records, as a snapshot of the digital copy."""
        return [r for records in self._sources.values() for r in records.values()]

    def load_source_records(self, records: list[Device]) -> None:
        for path in self._sources:
            self._sources[path] = {r.id: r for r in records if r.primary_control_path == path}
        self._rebuild()

    def mark_all_reachable(self) -> None:
        """Offline mode: every device answers from the digital copy, over its best path."""
        for record in self.source_records():
            record.online = True
        self._rebuild()

    def write_shadow_state(self, device_id: str, clusters: Clusters) -> None:
        """Set a device's state in the digital copy, as its primary path would have reported it."""
        device = self.get(device_id)
        primary = device.primary_control_path
        if (record := self._sources.get(primary, {}).get(device.path_device_ids.get(primary, ""))) is not None:
            record.clusters = {name: c.model_copy(deep=True) for name, c in clusters.items()}
        self._apply_state(device, {**device.clusters, **clusters}, primary)

    async def resolve(self, device_id: str) -> Device:
        """Find a device, re-listing once in case it was added since the last refresh."""
        if device_id not in self._devices:
            await self.refresh()
        return self.get(device_id)

    def get(self, device_id: str) -> Device:
        """The registry's current record, as kept up to date by reads, commands and pushes."""
        try:
            return self._devices[device_id]
        except KeyError:
            raise DeviceNotFound(f"no device {device_id!r}") from None

    def routes(self, device_id: str) -> list[Route]:
        """The paths to try for a device: reachable ones best first, then unreachable ones."""
        device = self.get(device_id)
        routes = [Route(path, platform_id, self._drivers[path], self._sources[path][platform_id])
                  for path in device.control_paths
                  if (platform_id := device.path_device_ids.get(path)) in self._sources.get(path, {})]
        return sorted(routes, key=lambda r: (not r.record.online, PATH_PRIORITY.index(r.path)))

    async def read_state(self, device_id: str) -> tuple[Route | None, Clusters]:
        """Live state from the best reachable path, falling back while paths are offline.

        In offline mode, the digital copy's state, with no route.
        """
        if self.offline_mode:
            return None, {name: c.model_copy(deep=True) for name, c in self.get(device_id).clusters.items()}
        offline: list[str] = []
        for route in self.routes(device_id):
            try:
                clusters = await asyncio.wait_for(route.driver.read_state(route.device_id), self.driver_timeout_s)
            except TimeoutError:
                offline.append(f"{route.path}: read did not answer within {self.driver_timeout_s:g}s")
                continue
            except DeviceOffline as exc:
                offline.append(f"{route.path}: {exc}")
                continue
            self.record_path_state(route.path, route.device_id, clusters, fresh=True)
            return route, clusters
        raise DeviceOffline("; ".join(offline) or f"{device_id} has no reachable path")

    # Updates from drivers, the router and reads

    def record_path_state(self, path: ControlPath, platform_id: str, clusters: Clusters, fresh: bool = False) -> None:
        """Store one path's state for a device.

        The unified state follows the primary path. Another path's state counts
        only when it is fresh, such as the read-back of a command that path just ran;
        otherwise a slow cloud poll could overwrite what local Matter reported.
        """
        if self.offline_mode:
            return
        if (record := self._sources.get(path, {}).get(platform_id)) is not None:
            record.clusters = clusters
        device = self._unified(path, platform_id)
        if device is None or not (fresh or path == device.primary_control_path):
            return
        merged = {**device.clusters, **{name: c.model_copy(deep=True) for name, c in clusters.items()}}
        self._apply_state(device, merged, path)

    def record_path_online(self, path: ControlPath, platform_id: str, online: bool) -> None:
        if self.offline_mode:
            return
        record = self._sources.get(path, {}).get(platform_id)
        device = self._unified(path, platform_id)
        if record is None or device is None or record.online == online:
            return
        record.online = online
        updated = combine([self._sources[p][pid] for p, pid in device.path_device_ids.items()])
        updated.clusters = device.clusters
        self._devices[device.id] = updated
        if (primary := updated.primary_control_path) != device.primary_control_path:
            log.info("%s now controlled via %s (%s %s)", device.id, primary, path,
                     "back" if online else "unreachable")
            # The new primary's own state is the freshest the hub has for it.
            primary_record = self._sources[primary][updated.path_device_ids[primary]]
            self.record_path_state(primary, primary_record.id, primary_record.clusters)
        if updated.online != device.online:
            self.events.publish("online_changed", device.id, path, online=updated.online)

    def record_path_event(self, path: ControlPath, platform_id: str, cluster: str, event: str,
                          data: dict[str, Any]) -> None:
        if self.offline_mode:
            return
        if (device := self._unified(path, platform_id)) is not None:
            self.events.publish("device_event", device.id, path, cluster=cluster, event=event, data=data)

    def _unified(self, path: ControlPath, platform_id: str) -> Device | None:
        unified_id = self._owners.get((path, platform_id))
        return self._devices.get(unified_id) if unified_id is not None else None

    def _apply_state(self, device: Device, clusters: Clusters, source: ControlPath) -> None:
        changed = _changes(device.clusters, clusters)
        if not changed and device.clusters.keys() == clusters.keys():
            return
        device.clusters = clusters
        device.last_updated_at = utcnow()
        self.events.publish("state_changed", device.id, source, changed=changed, clusters=clusters)
        self._changed()

    def _changed(self) -> None:
        for listener in self.change_listeners:
            listener()
