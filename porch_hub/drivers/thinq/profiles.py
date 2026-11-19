"""LG ThinQ appliance profiles: LG's wire format translated into Matter clusters, and back.

Field names are the ThinQ Connect API's JSON keys, the same keys the
thinqconnect SDK's profile maps use (runState.currentState,
remoteControlEnable.remoteControlEnabled, timer.relativeHourToStart). Enum
values come from the SDK and Home Assistant's LG integration; confirm them on
the demo units, since LG profiles vary by model.
"""

import copy
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ... import clusters as c
from ...models import Clusters, DeviceType
from ..base import CommandRejected

Status = dict[str, Any]
Payload = dict[str, Any]


@dataclass(frozen=True)
class ThinQProfile:
    device_type: DeviceType
    to_clusters: Callable[[Status], Clusters]
    to_payload: Callable[[str, dict[str, Any]], Payload]
    # Washers report per tub, as a list keyed by location. Single-tub machines use MAIN.
    location: str | None = None
    # Washers and dryers report a Remote Start switch; a start needs it to read true.
    requires_remote_switch: bool = False

    def select(self, data: dict[str, Any] | list[dict[str, Any]]) -> Status:
        """This profile's slice of LG data (status, push report or profile), without the location key."""
        if isinstance(data, list):
            matches = [e for e in data if e.get("location", {}).get("locationName") == self.location]
            data = (matches or data or [{}])[0]
        return {k: copy.deepcopy(v) for k, v in data.items() if k != "location"}

    def wrap(self, payload: Payload) -> Payload:
        return {"location": {"locationName": self.location}, **payload} if self.location else payload


def check_writable(properties: dict[str, Any], payload: Payload, appliance: str) -> None:
    """Reject a payload the appliance's LG profile does not allow, before it reaches LG."""
    for resource, values in payload.items():
        for key, value in values.items():
            spec = properties.get(resource, {}).get(key)
            if not isinstance(spec, dict) or "w" not in spec.get("mode", []):
                raise CommandRejected(f"{appliance} does not accept {resource}.{key} remotely")
            allowed = spec.get("value", {}).get("w")
            if isinstance(allowed, list) and value not in allowed:
                raise CommandRejected(f"{appliance} does not accept {resource}.{key}={value!r}; allowed: {allowed}")
            if isinstance(allowed, dict):
                _check_range(allowed, value, f"{resource}.{key}", appliance)


def _check_range(allowed: dict[str, Any], value: Any, field: str, appliance: str) -> None:
    """LG ranges give min, max and step, and may list values that are excluded."""
    if type(value) is not int:  # bool is an int subclass, and is not a number of hours
        raise CommandRejected(f"{appliance} needs a whole number for {field}, not {value!r}")
    low, high, step = allowed.get("min", value), allowed.get("max", value), allowed.get("step", 1) or 1
    if not low <= value <= high:
        raise CommandRejected(f"{appliance} accepts {field} from {low} to {high}, not {value}")
    if (value - low) % step:
        raise CommandRejected(f"{appliance} accepts {field} in steps of {step} from {low}, not {value}")
    if value in allowed.get("except", []):
        raise CommandRejected(f"{appliance} does not accept {field}={value}")


def remote_control_enabled(status: Status) -> bool | None:
    """The machine's Remote Start switch, or None when the status does not report it."""
    value = status.get("remoteControlEnable", {}).get("remoteControlEnabled")
    return value if type(value) is bool else None


def start_refusal(remote: bool | None, operational_state: str | None, requires_remote_switch: bool) -> str | None:
    """Why a start (or delay start) must not be sent, if it must not. Shared by live and offline checks."""
    if remote is False:
        return "Remote Control switch disabled on physical appliance"
    if remote is None and requires_remote_switch:
        return "Remote Control switch status is unknown; check the appliance before starting it"
    if operational_state == "Error":
        return "appliance reports an error"
    return None


def _run_state(status: Status) -> str | None:
    return status.get("runState", {}).get("currentState")


# A state name this table does not know reads as Unknown, never as Stopped: a read-back
# must not confirm a Stop from a state it cannot interpret.
UNKNOWN = "Unknown"

# Washers and dryers

_LAUNDRY_RUNNING = frozenset({
    "RUNNING", "DETECTING", "PREWASH", "RINSING", "SPINNING", "SOAKING", "DRYING", "COOLING",
    "STEAM_SOFTENING", "WRINKLE_CARE", "REFRESHING", "ADD_DRAIN", "DISPENSING", "FROZEN_PREVENT_RUNNING",
})
# RESERVED: a delay start is armed and the cycle has not begun.
_LAUNDRY_STOPPED = frozenset({"POWER_OFF", "INITIAL", "END", "STANDBY", "SLEEP", "STOP", "RESERVED"})
_LAUNDRY_PAUSED = frozenset({"PAUSE", "FROZEN_PREVENT_PAUSE", "RINSE_HOLD"})


def _laundry_state(lg_state: str | None) -> str:
    if lg_state in _LAUNDRY_RUNNING:
        return "Running"
    if lg_state in _LAUNDRY_STOPPED:
        return "Stopped"
    if lg_state in _LAUNDRY_PAUSED:
        return "Paused"
    return "Error" if lg_state == "ERROR" else UNKNOWN


def laundry_clusters(status: Status) -> Clusters:
    run_state = _run_state(status)
    timer = status.get("timer", {})
    return {
        c.OPERATIONAL_STATE: c.operational_state(_laundry_state(run_state), commands=["Start", "Stop"]),
        c.PORCH_LAUNDRY: c.porch_laundry(
            remote_start_allowed=remote_control_enabled(status),
            hours=timer.get("relativeHourToStart", 0),
            minutes=timer.get("relativeMinuteToStart", 0),
            vendor_run_state=run_state,
        ),
    }


def laundry_payload(operation_key: str) -> Callable[[str, dict[str, Any]], Payload]:
    def to_payload(command: str, params: dict[str, Any]) -> Payload:
        match command:
            case "Start":
                return {"operation": {operation_key: "START"}}
            case "Stop":
                return {"operation": {operation_key: "STOP"}}
            case "SetDelayStart":
                timer = {"relativeHourToStart": params["hours"]}
                if params.get("minutes"):
                    timer["relativeMinuteToStart"] = params["minutes"]
                return {"timer": timer}
        raise CommandRejected(f"no LG laundry mapping for {command}")

    return to_payload


# Robot vacuums

_RVC_STATES = {
    "WORKING": "Running",
    "SETDATE": "Running",
    "PAUSE": "Paused",
    "HOMING": "SeekingCharger",
    "CHARGING": "Charging",
    "SLEEP": "Stopped",
    "STANDBY": "Stopped",
    "ERROR": "Error",
}


def robot_cleaner_clusters(status: Status) -> Clusters:
    clusters = {c.RVC_OPERATIONAL_STATE: c.rvc_operational_state(_RVC_STATES.get(_run_state(status), UNKNOWN))}
    if (percent := status.get("battery", {}).get("percent")) is not None:
        clusters[c.POWER_SOURCE] = c.power_source(percent)
    return clusters


def robot_cleaner_payload(command: str, _params: dict[str, Any]) -> Payload:
    modes = {"Start": "START", "Pause": "PAUSE", "GoHome": "HOMING"}
    if command not in modes:
        raise CommandRejected(f"no LG robot cleaner mapping for {command}")
    return {"operation": {"cleanOperationMode": modes[command]}}


# Stick vacuums (LG CordZero). LG reports where the vacuum is and what it is doing, and offers
# no remote control at all: every property in its profile is read only.

_STICK_STATES = {"STANDBY": "Stopped", "WORKING": "Running", "CHARGING": "Charging", "CHARGING_COMPLETE": "Docked"}
_STICK_MODES = {"OFF": "Off", "NORMAL": "Normal", "HIGH": "High", "TURBO": "Turbo", "MOP": "Mop", "AUTO": "Auto"}
# LG's four battery levels on Matter's three: only LOW and WARNING ask for attention.
_BATTERY_LEVELS = {"HIGH": "OK", "MID": "OK", "LOW": "Warning", "WARNING": "Critical"}


def stick_cleaner_clusters(status: Status) -> Clusters:
    clusters = {c.RVC_OPERATIONAL_STATE: c.rvc_operational_state(
        _STICK_STATES.get(_run_state(status), UNKNOWN), commands=())}
    if (mode := status.get("stickCleanerJobMode", {}).get("currentJobMode")) is not None:
        clusters[c.RVC_CLEAN_MODE] = c.rvc_clean_mode(_STICK_MODES.get(mode, UNKNOWN))
    battery = status.get("battery", {})
    percent, level = battery.get("percent"), _BATTERY_LEVELS.get(battery.get("level"))
    if percent is not None or level is not None:
        clusters[c.POWER_SOURCE] = c.power_source(percent, level)
    return clusters


def stick_cleaner_payload(command: str, _params: dict[str, Any]) -> Payload:
    raise CommandRejected(f"LG offers no remote control for stick vacuums, so {command} cannot be sent")


# Air purifiers

_FAN_MODES = {"SLOW": "Low", "LOW": "Low", "MID": "Medium", "HIGH": "High", "POWER": "High", "AUTO": "Auto"}
_AIR_QUALITY = {"GOOD": "Good", "NORMAL": "Fair", "BAD": "Poor", "VERY_BAD": "VeryPoor"}


def air_purifier_clusters(status: Status) -> Clusters:
    # None when LG does not say: an unknown power state is never passed on as off.
    on = {"POWER_ON": True, "POWER_OFF": False}.get(status.get("operation", {}).get("airPurifierOperationMode"))
    sensor = status.get("airQualitySensor", {})
    air_flow = status.get("airFlow", {})
    wind = air_flow.get("windStrengthDetail") or air_flow.get("windStrength")
    clusters = {
        c.ON_OFF: c.on_off(on, commands=["On", "Off"]),
        c.AIR_QUALITY: c.air_quality(_AIR_QUALITY.get(sensor.get("totalPollutionLevel"), "Unknown")),
    }
    if wind is not None:
        clusters[c.FAN_CONTROL] = c.fan_control(_FAN_MODES.get(wind, "On"))
    if (pm2 := sensor.get("PM2")) is not None:
        clusters[c.PM25] = c.pm25(pm2)
    # The other readings a purifier may give. Each is listed only when LG reports a number.
    for lg_name, cluster in (("PM1", c.PM1), ("PM10", c.PM10)):
        if _is_number(value := sensor.get(lg_name)):
            clusters[cluster] = c.particles(value)
    if _is_number(left := status.get("filterInfo", {}).get("filterRemainPercent")):
        clusters[c.HEPA_FILTER_MONITORING] = c.hepa_filter(left)
    return clusters


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def air_purifier_payload(command: str, _params: dict[str, Any]) -> Payload:
    modes = {"On": "POWER_ON", "Off": "POWER_OFF"}
    if command not in modes:
        raise CommandRejected(f"no LG air purifier mapping for {command}")
    return {"operation": {"airPurifierOperationMode": modes[command]}}


# Keyed by the deviceInfo.deviceType LG reports.
PROFILES: dict[str, ThinQProfile] = {
    "DEVICE_DRYER": ThinQProfile(DeviceType.LAUNDRY_DRYER, laundry_clusters,
                                 laundry_payload("dryerOperationMode"), requires_remote_switch=True),
    "DEVICE_WASHER": ThinQProfile(DeviceType.LAUNDRY_WASHER, laundry_clusters,
                                  laundry_payload("washerOperationMode"), location="MAIN",
                                  requires_remote_switch=True),
    "DEVICE_ROBOT_CLEANER": ThinQProfile(DeviceType.ROBOTIC_VACUUM_CLEANER, robot_cleaner_clusters,
                                         robot_cleaner_payload),
    "DEVICE_STICK_CLEANER": ThinQProfile(DeviceType.STICK_VACUUM, stick_cleaner_clusters,
                                         stick_cleaner_payload),
    "DEVICE_AIR_PURIFIER": ThinQProfile(DeviceType.AIR_PURIFIER, air_purifier_clusters,
                                        air_purifier_payload),
}
