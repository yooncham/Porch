"""Power and energy across the home, for the twin pages' energy panel.

A device counts as a meter when it is a plug, or when it reports Matter's
ElectricalPowerMeasurement.ActivePower (milliwatts) or
ElectricalEnergyMeasurement.CumulativeEnergyImported (milliwatt-hours). Today those readings
come from energy-monitoring plugs paired over Matter. A plug reached another way (through
Google Home) says only whether it is on, and is listed that way: nothing here is estimated.

The log keeps each meter's recent power readings in memory, so a page can draw how its power
moved since the hub started. It is not a billing record: it starts empty on every restart.
"""

import asyncio
import logging
from collections import deque
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from . import clusters as c
from .events import EventBus, SubscriberLagged
from .models import Clusters, Device, DeviceType, utcnow

log = logging.getLogger(__name__)

KEEP = timedelta(hours=6)
MAX_POINTS = 360
# A meter that draws this much keeps counting: its power wobbles and its energy counter ticks.
# If neither moves for this long, the hub is no longer hearing from it, whatever it last said.
STALE_ABOVE_W = 20.0
STALE_AFTER = timedelta(minutes=45)


def _number(clusters: Clusters, cluster: str, attribute: str) -> float | None:
    found = clusters.get(cluster)
    value = found.attributes.get(attribute) if found is not None else None
    # bool is an int subclass, and a plug that says True is not drawing one milliwatt.
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def power_w(clusters: Clusters) -> float | None:
    """What the device draws now, in watts, or None when it does not say."""
    milliwatts = _number(clusters, c.ELECTRICAL_POWER_MEASUREMENT, "ActivePower")
    return None if milliwatts is None else round(milliwatts / 1000, 1)


def energy_kwh(clusters: Clusters) -> float | None:
    """The device's own energy counter, in kilowatt-hours, or None when it has none."""
    milliwatt_hours = _number(clusters, c.ELECTRICAL_ENERGY_MEASUREMENT, "CumulativeEnergyImported")
    return None if milliwatt_hours is None else round(milliwatt_hours / 1_000_000, 3)


def voltage_v(clusters: Clusters) -> float | None:
    """The voltage at the device, in volts, or None when it does not measure it."""
    for name in ("RMSVoltage", "Voltage"):
        if (millivolts := _number(clusters, c.ELECTRICAL_POWER_MEASUREMENT, name)) is not None:
            return round(millivolts / 1000, 1)
    return None


def current_a(clusters: Clusters) -> float | None:
    """The current the device draws, in amps, or None when it does not measure it."""
    for name in ("RMSCurrent", "ActiveCurrent"):
        if (milliamps := _number(clusters, c.ELECTRICAL_POWER_MEASUREMENT, name)) is not None:
            return round(milliamps / 1000, 2)
    return None


def is_meter(device: Device) -> bool:
    return (device.device_type == DeviceType.ON_OFF_PLUG
            or power_w(device.clusters) is not None or energy_kwh(device.clusters) is not None)


class EnergyLog:
    """Each meter's recent power readings, and the energy counter it showed when first seen."""

    def __init__(self, keep: timedelta = KEEP, max_points: int = MAX_POINTS) -> None:
        self._keep = keep
        self._max_points = max_points
        self._power: dict[str, deque[tuple[datetime, float]]] = {}
        self._first_energy: dict[str, tuple[datetime, float]] = {}
        # What each meter last said, and when that last changed (when it was first seen, at the start).
        self._last: dict[str, tuple[float | None, float | None]] = {}
        self._changed_at: dict[str, datetime] = {}

    def record(self, device_id: str, clusters: Clusters, at: datetime | None = None) -> None:
        at = at or utcnow()
        said = (power_w(clusters), energy_kwh(clusters))
        if said != (None, None) and self._last.get(device_id) != said:
            self._last[device_id] = said
            self._changed_at[device_id] = at
        if (watts := power_w(clusters)) is not None:
            points = self._power.setdefault(device_id, deque(maxlen=self._max_points))
            if not points or points[-1][1] != watts:  # only changes: the page draws steps between them
                points.append((at, watts))
            while len(points) > 1 and points[1][0] < at - self._keep:
                points.popleft()  # the last reading before the window stays, as where the line starts
        if (kwh := energy_kwh(clusters)) is not None:
            first = self._first_energy.get(device_id)
            if first is None or kwh < first[1]:  # a counter that went backwards was reset: count from here
                self._first_energy[device_id] = (at, kwh)

    def summary(self, devices: Iterable[Device], now: datetime | None = None) -> dict[str, Any]:
        """Every meter with what it reports now. Reading it also records it, so the log has a
        first point for a device whose power has not changed since the hub started."""
        now = now or utcnow()
        meters = []
        for device in devices:
            if not is_meter(device):
                continue
            self.record(device.id, device.clusters, now)
            watts, kwh = power_w(device.clusters), energy_kwh(device.clusters)
            first = self._first_energy.get(device.id)
            switch = device.clusters.get(c.ON_OFF)
            changed_at = self._changed_at.get(device.id)
            # Drawing real power with a counter, and not a digit has moved for a long time.
            stale = (watts is not None and kwh is not None and watts >= STALE_ABOVE_W
                     and changed_at is not None and now - changed_at > STALE_AFTER)
            meters.append({
                "id": device.id, "name": device.name, "room": device.room, "online": device.online,
                "path": device.primary_control_path,
                "on": switch.attributes.get("OnOff") if switch is not None else None,
                "power_w": watts,
                "voltage_v": voltage_v(device.clusters),
                "current_a": current_a(device.clusters),
                "energy_kwh": kwh,
                # Energy used since this hub first saw the counter: a difference of two real readings.
                "since_kwh": round(kwh - first[1], 3) if kwh is not None and first is not None else None,
                "since": first[0].isoformat() if kwh is not None and first is not None else None,
                # When its reading last changed, and whether it has stood still too long to be
                # called current. A stale reading is shown as what it is and left out of the total.
                "changed_at": changed_at.isoformat() if changed_at is not None else None,
                "stale": stale,
                "history": [[at.isoformat(), value] for at, value in self._power.get(device.id, ())],
            })
        reporting = [m["power_w"] for m in meters if m["power_w"] is not None and m["online"] and not m["stale"]]
        return {
            "at": now.isoformat(),
            "total_w": round(sum(reporting), 1) if reporting else None,
            "reporting": len(reporting),
            "meters": meters,
        }

    async def follow(self, bus: EventBus) -> None:
        """Record every power or energy change the hub hears of, until cancelled."""
        while True:
            with bus.subscribe() as events:
                try:
                    async for event in events:
                        if event.type == "state_changed" and event.device_id and event.clusters:
                            self.record(event.device_id, event.clusters, event.at)
                except SubscriberLagged:
                    log.warning("the energy log fell behind and missed some readings; it carries on from now")
            await asyncio.sleep(0)
