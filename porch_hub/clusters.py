"""Matter cluster names and builders for the demo device types.

Names follow the Matter specification. Matter has no field for delay start or
an appliance's Remote Start switch, so those live in a Porch extension cluster.
"""

from collections.abc import Sequence

from .models import Cluster

ON_OFF = "OnOff"
DOOR_LOCK = "DoorLock"
BOOLEAN_STATE = "BooleanState"
VALVE = "ValveConfigurationAndControl"
OPERATIONAL_STATE = "OperationalState"
RVC_OPERATIONAL_STATE = "RvcOperationalState"
POWER_SOURCE = "PowerSource"
FAN_CONTROL = "FanControl"
AIR_QUALITY = "AirQuality"
PM25 = "Pm25ConcentrationMeasurement"
PORCH_LAUNDRY = "PorchLaundryExtension"


def on_off(on: bool = False, commands: Sequence[str] = ("On", "Off", "Toggle")) -> Cluster:
    return Cluster(attributes={"OnOff": on}, commands=list(commands))


def door_lock(locked: bool = True) -> Cluster:
    return Cluster(
        attributes={"LockState": "Locked" if locked else "Unlocked"},
        commands=["LockDoor", "UnlockDoor"],
    )


def boolean_state(value: bool = False) -> Cluster:
    """For a leak detector, True means water is detected."""
    return Cluster(attributes={"StateValue": value})


def valve(is_open: bool = True) -> Cluster:
    return Cluster(
        attributes={"CurrentState": "Open" if is_open else "Closed"},
        commands=["Open", "Close"],
    )


def operational_state(
    state: str = "Stopped",
    commands: Sequence[str] = ("Start", "Stop", "Pause", "Resume"),
) -> Cluster:
    return Cluster(attributes={"OperationalState": state}, commands=list(commands))


def rvc_operational_state(state: str = "Docked") -> Cluster:
    """Robot vacuum state: Stopped, Running, Paused, Error, SeekingCharger, Charging, Docked or Unknown."""
    return Cluster(attributes={"OperationalState": state}, commands=["Start", "Pause", "GoHome"])


def power_source(battery_percent: float) -> Cluster:
    """battery_percent is 0 to 100. Matter's BatPercentRemaining counts half percents, 0 to 200."""
    return Cluster(attributes={"BatPercentRemaining": max(0, min(200, round(battery_percent * 2)))})


def fan_control(fan_mode: str) -> Cluster:
    return Cluster(attributes={"FanMode": fan_mode})


def air_quality(level: str) -> Cluster:
    """Good, Fair, Moderate, Poor, VeryPoor, ExtremelyPoor or Unknown."""
    return Cluster(attributes={"AirQuality": level})


def pm25(measured_value: float) -> Cluster:
    return Cluster(attributes={"MeasuredValue": measured_value})


def porch_laundry(remote_start_allowed: bool | None, hours: int = 0, minutes: int = 0,
                  vendor_run_state: str | None = None) -> Cluster:
    attributes = {
        "RemoteStartAllowed": remote_start_allowed,
        "RelativeHourToStart": hours,
        "RelativeMinuteToStart": minutes,
    }
    if vendor_run_state is not None:
        # The platform's own state name, for detail Matter's state loses (for example RESERVED).
        attributes["VendorRunState"] = vendor_run_state
    return Cluster(attributes=attributes, commands=["SetDelayStart"])
