"""Interface v0 records shared by the Engine and the hub.

Every device is described with Matter's names for device types, clusters,
attributes and commands, whichever platform it actually lives on.
"""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ControlPath(StrEnum):
    LG_THINQ = "lg_thinq"
    GOOGLE_HOME = "google_home"
    MATTER = "matter"
    RING = "ring"


class DeviceType(StrEnum):
    LAUNDRY_DRYER = "LaundryDryer"
    LAUNDRY_WASHER = "LaundryWasher"
    ROBOTIC_VACUUM_CLEANER = "RoboticVacuumCleaner"
    # A cordless stick vacuum on its charging stand. Matter has no such type; the hub reads it only.
    STICK_VACUUM = "StickVacuumCleaner"
    AIR_PURIFIER = "AirPurifier"
    # A TV or a streaming box that shows what is cast to it (Matter's Casting Video Player).
    CASTING_VIDEO_PLAYER = "CastingVideoPlayer"
    DOOR_LOCK = "DoorLock"
    ON_OFF_PLUG = "OnOffPlug"
    ON_OFF_LIGHT = "OnOffLight"
    WATER_LEAK_SENSOR = "WaterLeakSensor"
    WATER_VALVE = "WaterValve"
    DOORBELL = "Doorbell"


class Cluster(BaseModel):
    """One Matter cluster: its current attribute values and the commands it accepts."""

    attributes: dict[str, Any] = Field(default_factory=dict)
    commands: list[str] = Field(default_factory=list)


Clusters = dict[str, Cluster]


class Device(BaseModel):
    id: str
    name: str
    room: str
    device_type: DeviceType
    online: bool
    # The path commands take now: local Matter while it is reachable, else the next path that is.
    primary_control_path: ControlPath
    # Every path that reaches this device, best first; the ones after the primary are fallbacks.
    control_paths: list[ControlPath] = Field(default_factory=list)
    serial_number: str | None = None
    mac_address: str | None = None
    clusters: Clusters
    last_updated_at: datetime
    # Each path's own id for this device. Internal to the hub, so not serialized.
    path_device_ids: dict[ControlPath, str] = Field(default_factory=dict, exclude=True)


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=200)


class CommandResponse(BaseModel):
    status: Literal["done", "failed"]
    state_after: Clusters
    latency_ms: int
    idempotency_key: str
    # The path that carried the command; a fallback path when the primary was unreachable.
    control_path: ControlPath | None = None
    # True when offline demo mode answered from the digital copy and no device was touched.
    simulated: bool = False
    # Why the command failed, in words the Engine can log or show. None when done.
    error: str | None = None
