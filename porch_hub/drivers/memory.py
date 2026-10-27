import asyncio
from collections.abc import Iterable
from typing import Any

from .. import commands
from ..models import Clusters, Device, utcnow
from .base import BaseDriver, CommandRejected, DeviceNotFound, DeviceOffline


class InMemoryDriver(BaseDriver):
    """A driver whose devices live in an in-memory digital copy.

    Stands in for a platform whose real connector is not built yet, backs the
    tests, and is the backup demo mode when Wi-Fi or a cloud API fails.
    Subclasses set control_path and add platform rules in check_allowed.
    """

    def __init__(self, devices: Iterable[Device] = (), latency_s: float = 0.0) -> None:
        super().__init__()
        self._devices = {d.id: d.model_copy(deep=True) for d in devices}
        self._latency_s = latency_s

    async def list_devices(self) -> list[Device]:
        await self._round_trip()
        return [d.model_copy(update={"primary_control_path": self.control_path}, deep=True)
                for d in self._devices.values()]

    async def read_state(self, device_id: str) -> Clusters:
        await self._round_trip()
        device = self._online(device_id)
        return {name: cluster.model_copy(deep=True) for name, cluster in device.clusters.items()}

    async def send_command(self, device_id: str, command: str, params: dict[str, Any]) -> None:
        await self._round_trip()
        device = self._online(device_id)
        self.check_allowed(device, command)
        try:
            spec = commands.resolve(device, command)
        except commands.UnsupportedCommand as exc:
            raise CommandRejected(str(exc)) from exc
        try:
            commands.apply(spec, spec.parse_params(params), device.clusters)
        except commands.CannotPredict as exc:
            raise CommandRejected(str(exc)) from exc
        self._changed(device)

    def preflight(self, device_id: str, command: str, params: dict[str, Any], clusters: Clusters) -> None:
        if (device := self._devices.get(device_id)) is not None:
            self.check_allowed(device, command)

    def check_allowed(self, device: Device, command: str) -> None:
        """Platform rule hook: raise CommandRejected for commands the platform refuses."""

    # Demo and test hooks: change the copy as if the physical device had changed.

    def set_online(self, device_id: str, online: bool) -> None:
        self._device(device_id).online = online
        self._publish_online(device_id, online)

    def set_attribute(self, device_id: str, cluster: str, attribute: str, value: Any) -> None:
        device = self._device(device_id)
        device.clusters[cluster].attributes[attribute] = value
        self._changed(device)

    def _changed(self, device: Device) -> None:
        """Push the change, as a Matter subscription or platform event would."""
        device.last_updated_at = utcnow()
        self._publish_state(device.id, {n: cl.model_copy(deep=True) for n, cl in device.clusters.items()})

    def _device(self, device_id: str) -> Device:
        try:
            return self._devices[device_id]
        except KeyError:
            raise DeviceNotFound(f"{self.control_path} has no device {device_id!r}") from None

    def _online(self, device_id: str) -> Device:
        device = self._device(device_id)
        if not device.online:
            raise DeviceOffline(f"{device_id} is offline on {self.control_path}")
        return device

    async def _round_trip(self) -> None:
        if self._latency_s:
            await asyncio.sleep(self._latency_s)
