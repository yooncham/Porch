"""Break a device on purpose, for benchmarks and demos.

FaultyDriver wraps any driver and injects one failure per device at a time.
The hub cannot tell it apart from a real platform misbehaving, so whatever
the hub does about a fault is what it would do in a home. It also counts
the commands that physically reached each device, which is the benchmark's
ground truth for duplicate actions.

    lost_ack   the device acts, but the acknowledgement never arrives (send raises)
    no_effect  the platform says OK, but the device does not change
    slow       the device acts only after `seconds`; the acknowledgement waits for it
    offline    reads and commands fail as if the device were unreachable
    reject     the platform refuses the command

Each fault fires `times` times (None: until cleared), then the device behaves again.
"""

import asyncio
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

from .drivers import BaseDriver, CommandRejected, DeviceOffline, DriverError
from .drivers.base import DriverListener
from .models import Clusters, Device

FaultKind = Literal["lost_ack", "no_effect", "slow", "offline", "reject"]
FAULT_KINDS: tuple[FaultKind, ...] = ("lost_ack", "no_effect", "slow", "offline", "reject")


@dataclass
class Fault:
    kind: FaultKind
    times: int | None = 1          # how many commands it affects; None for every one until cleared
    seconds: float = 3.0           # for slow: how long the device takes to act
    commands: tuple[str, ...] = () # only these commands; empty for all

    def applies_to(self, command: str) -> bool:
        return (not self.commands or command in self.commands) and (self.times is None or self.times > 0)

    def use(self) -> None:
        if self.times is not None:
            self.times -= 1


class FaultyDriver(BaseDriver):
    """A driver with the same behaviour as `inner`, plus injected faults and a count of real sends."""

    def __init__(self, inner: BaseDriver) -> None:
        super().__init__()
        self.inner = inner
        self.control_path = inner.control_path
        self.read_back_interval_s = inner.read_back_interval_s
        self.read_back_timeout_s = inner.read_back_timeout_s
        self.faults: dict[str, Fault] = {}
        self.applied: Counter[tuple[str, str]] = Counter()   # (device_id, command) -> times the device got it

    # Fault control

    def inject(self, device_id: str, fault: Fault) -> None:
        self.faults[device_id] = fault

    def clear(self, device_id: str | None = None) -> None:
        if device_id is None:
            self.faults.clear()
        else:
            self.faults.pop(device_id, None)

    def _fault(self, device_id: str, command: str | None = None) -> Fault | None:
        fault = self.faults.get(device_id)
        if fault is None:
            return None
        if fault.kind == "offline":
            return fault if fault.times is None or fault.times > 0 else None
        return fault if command is not None and fault.applies_to(command) else None

    # BaseDriver, passed through to the real driver

    async def start(self) -> None:
        await self.inner.start()

    async def stop(self) -> None:
        await self.inner.stop()

    def add_listener(self, listener: DriverListener) -> None:
        self.inner.add_listener(listener)

    async def list_devices(self) -> list[Device]:
        devices = await self.inner.list_devices()
        for device in devices:
            if self._fault(device.id) is not None and self.faults[device.id].kind == "offline":
                device.online = False
        return devices

    async def read_state(self, device_id: str) -> Clusters:
        fault = self.faults.get(device_id)
        if fault is not None and fault.kind == "offline" and (fault.times is None or fault.times > 0):
            raise DeviceOffline(f"{device_id} is unreachable (injected fault)")
        return await self.inner.read_state(device_id)

    async def send_command(self, device_id: str, command: str, params: dict[str, Any]) -> None:
        fault = self.faults.get(device_id)
        if fault is not None and fault.kind == "offline" and (fault.times is None or fault.times > 0):
            fault.use()
            raise DeviceOffline(f"{device_id} is unreachable (injected fault)")
        fault = self._fault(device_id, command)
        if fault is None:
            await self._apply(device_id, command, params)
            return
        fault.use()
        match fault.kind:
            case "lost_ack":
                await self._apply(device_id, command, params)
                raise DriverError(f"no acknowledgement for {command} (injected fault)")
            case "no_effect":
                return  # the platform accepted it; the device never acted
            case "slow":
                await asyncio.sleep(fault.seconds)
                await self._apply(device_id, command, params)
            case "reject":
                raise CommandRejected(f"{command} refused by the platform (injected fault)")

    async def details(self, device_id: str) -> list[dict[str, str]]:
        return await self.inner.details(device_id)

    def preflight(self, device_id: str, command: str, params: dict[str, Any], clusters: Clusters) -> None:
        self.inner.preflight(device_id, command, params, clusters)

    async def _apply(self, device_id: str, command: str, params: dict[str, Any]) -> None:
        await self.inner.send_command(device_id, command, params)
        self.applied[(device_id, command)] += 1


def with_faults(drivers: list[BaseDriver]) -> list[FaultyDriver]:
    return [d if isinstance(d, FaultyDriver) else FaultyDriver(d) for d in drivers]
