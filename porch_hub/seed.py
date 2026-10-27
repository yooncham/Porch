"""The demo home: the devices from the build plan, spread across all three paths."""

from dataclasses import dataclass

from . import clusters as c
from .drivers import (BaseDriver, FakeThinQCloud, GoogleHomeDriver, SimulatedMatterDriver, ThinQBinding,
                      ThinQDriver)
from .drivers.thinq.profiles import PROFILES
from .models import Clusters, ControlPath, Device, DeviceType, utcnow

HOME = "home-1"


@dataclass
class DemoHome:
    thinq_cloud: FakeThinQCloud
    thinq: ThinQDriver
    google: GoogleHomeDriver
    matter: SimulatedMatterDriver

    @property
    def drivers(self) -> list[BaseDriver]:
        return [self.matter, self.thinq, self.google]


def _device(slug: str, name: str, room: str, device_type: DeviceType,
            path: ControlPath, clusters: Clusters, serial_number: str | None = None) -> Device:
    return Device(
        id=f"{HOME}/{slug}", name=name, room=room, device_type=device_type, online=True,
        primary_control_path=path, clusters=clusters, serial_number=serial_number, last_updated_at=utcnow(),
    )


LG_BINDINGS = {
    "lg-dryer-01": ThinQBinding(f"{HOME}/laundry-dryer", "Laundry"),
    "lg-vacuum-01": ThinQBinding(f"{HOME}/robot-vacuum", "Living room"),
    "lg-purifier-01": ThinQBinding(f"{HOME}/air-purifier", "Bedroom"),
}


def _matter_records() -> list[Device]:
    return [
        _device("front-door-lock", "Front door", "Entry", DeviceType.DOOR_LOCK,
                ControlPath.MATTER, {c.DOOR_LOCK: c.door_lock(locked=True)}, serial_number="YRD450-0042"),
        _device("kitchen-leak-sensor", "Kitchen leak sensor", "Kitchen", DeviceType.WATER_LEAK_SENSOR,
                ControlPath.MATTER, {c.BOOLEAN_STATE: c.boolean_state(False)}),
        _device("main-water-valve", "Main water valve", "Utility", DeviceType.WATER_VALVE,
                ControlPath.MATTER, {c.VALVE: c.valve(is_open=True)}),
        # HW6 fallback if the LG appliance has no remote control: a Matter plug driving a lamp.
        _device("desk-lamp-plug", "Desk lamp", "Office", DeviceType.ON_OFF_PLUG,
                ControlPath.MATTER, {c.ON_OFF: c.on_off(False)}),
    ]


def _google_records() -> list[Device]:
    return [
        # The same Yale lock as Google Home lists it: its own id and name, the same serial number.
        # The registry merges the two and controls the lock over Matter, with Google as the fallback.
        _device("google-yale-lock", "Yale Lock", "Entryway", DeviceType.DOOR_LOCK,
                ControlPath.GOOGLE_HOME, {c.DOOR_LOCK: c.door_lock(locked=True)}, serial_number="YRD450 0042"),
        _device("space-heater-plug", "Space heater", "Living room", DeviceType.ON_OFF_PLUG,
                ControlPath.GOOGLE_HOME, {c.ON_OFF: c.on_off(False)}),
    ]


def _lg_cloud(latency_s: float = 0.0, apply_delay_s: float = 0.0) -> FakeThinQCloud:
    cloud = FakeThinQCloud(latency_s=latency_s, apply_delay_s=apply_delay_s)
    cloud.add_dryer("lg-dryer-01", "Dryer", remote_control_enabled=True)
    cloud.add_robot_cleaner("lg-vacuum-01", "Robot vacuum")
    cloud.add_air_purifier("lg-purifier-01", "Air purifier")
    return cloud


def demo_records() -> list[Device]:
    """The demo home's per-platform records, built without any driver.

    Offline mode starts from these when no saved digital copy exists, so it
    never has to ask a driver (or the network behind it) for an inventory.
    """
    records = _matter_records() + _google_records()
    for lg_id, device_type, alias, status in _lg_cloud().appliances():
        profile = PROFILES[device_type]
        binding = LG_BINDINGS[lg_id]
        records.append(Device(
            id=binding.porch_id, name=alias, room=binding.room, device_type=profile.device_type, online=True,
            primary_control_path=ControlPath.LG_THINQ, clusters=profile.to_clusters(profile.select(status)),
            last_updated_at=utcnow(),
        ))
    return records


def demo_home(simulate_latency: bool = True) -> DemoHome:
    """Build fresh drivers for the demo home. Latencies roughly match local vs cloud paths."""
    matter = SimulatedMatterDriver(_matter_records(), latency_s=0.02 if simulate_latency else 0.0)
    google = GoogleHomeDriver(_google_records(), latency_s=0.25 if simulate_latency else 0.0)
    thinq_cloud = _lg_cloud(latency_s=0.15 if simulate_latency else 0.0,
                            apply_delay_s=0.4 if simulate_latency else 0.0)
    thinq = ThinQDriver(
        thinq_cloud,
        home_id=HOME,
        bindings=LG_BINDINGS,
        # The fake has no rate limit, so use the hub's read-back defaults.
        read_back_interval_s=None,
        read_back_timeout_s=None,
    )
    return DemoHome(thinq_cloud=thinq_cloud, thinq=thinq, google=google, matter=matter)
