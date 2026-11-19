"""The contract every platform connector implements."""

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Protocol

from ..models import Clusters, ControlPath, Device


class DriverError(Exception):
    """The platform could not carry out a read or a command."""


class DeviceNotFound(DriverError):
    pass


class DeviceOffline(DriverError):
    pass


class CommandRejected(DriverError):
    """The platform or device refused the command (for example, Google blocks unlocking)."""


class DriverListener(Protocol):
    """Receives what a platform pushes. The device registry is the listener in the hub."""

    def on_state(self, device_id: str, clusters: Clusters) -> None: ...
    def on_online(self, device_id: str, online: bool) -> None: ...
    def on_device_event(self, device_id: str, cluster: str, event: str, data: dict[str, Any]) -> None: ...


class BaseDriver(ABC):
    """One connector per platform. The hub reaches every platform through this interface only.

    A driver translates its platform's native model into Matter-named clusters
    (see porch_hub.clusters) and raises DriverError subclasses rather than
    platform-specific exceptions. Platform secrets (LG token, Google OAuth
    tokens, Matter fabric keys) stay inside the driver.
    """

    control_path: ClassVar[ControlPath]

    # Read-back pacing for this platform. None uses the hub defaults; rate-limited
    # cloud APIs poll more slowly and wait longer.
    read_back_interval_s: float | None = None
    read_back_timeout_s: float | None = None
    # The longest one read or send may take on this platform. None uses the hub's driver timeout.
    call_timeout_s: float | None = None

    def __init__(self) -> None:
        self._listeners: list[DriverListener] = []

    async def start(self) -> None:
        """Open connections or sessions. Called once at hub startup."""

    async def stop(self) -> None:
        """Release connections. Called once at hub shutdown."""

    @abstractmethod
    async def list_devices(self) -> list[Device]:
        """Every device this platform sees, with primary_control_path set to this driver's path."""

    @abstractmethod
    async def read_state(self, device_id: str) -> Clusters:
        """Fresh cluster state from the platform, not a cache. Raises DeviceOffline."""

    @abstractmethod
    async def send_command(self, device_id: str, command: str, params: dict[str, Any]) -> None:
        """Send one Matter-named command and return once the platform accepts it.

        Acceptance is not success: the router confirms the effect by reading
        state back, so drivers should not wait for or assume the outcome.
        """

    def preflight(self, device_id: str, command: str, params: dict[str, Any], clusters: Clusters) -> None:
        """Raise CommandRejected if this platform would refuse the command, without contacting it.

        Used by dry runs and offline demo mode, so it must not do any I/O: judge
        from the device's last known clusters and what the driver already holds.
        Reachability is not this method's concern.
        """

    async def details(self, device_id: str) -> list[dict[str, str]]:
        """Everything this platform reports about the device, as rows of group, name and value
        (porch_hub/details.py), read now. By default, the hub's own model of the device."""
        from ..details import rows_from_clusters

        return rows_from_clusters(await self.read_state(device_id))

    def add_listener(self, listener: DriverListener) -> None:
        self._listeners.append(listener)

    # Call these, on the hub's event loop, whenever the platform pushes a change.

    def _publish_state(self, device_id: str, clusters: Clusters) -> None:
        for listener in self._listeners:
            listener.on_state(device_id, clusters)

    def _publish_online(self, device_id: str, online: bool) -> None:
        for listener in self._listeners:
            listener.on_online(device_id, online)

    def _publish_event(self, device_id: str, cluster: str, event: str, data: dict[str, Any]) -> None:
        for listener in self._listeners:
            listener.on_device_event(device_id, cluster, event, data)
