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
RVC_CLEAN_MODE = "RvcCleanMode"
POWER_SOURCE = "PowerSource"
FAN_CONTROL = "FanControl"
AIR_QUALITY = "AirQuality"
PM1 = "Pm1ConcentrationMeasurement"
PM25 = "Pm25ConcentrationMeasurement"
PM10 = "Pm10ConcentrationMeasurement"
# How much of an air purifier's filter is left.
HEPA_FILTER_MONITORING = "HepaFilterMonitoring"
# A light's brightness and its white tone. Read only so far: the hub switches lights on and off.
LEVEL_CONTROL = "LevelControl"
COLOR_CONTROL = "ColorControl"
# Read-only readings from energy-monitoring plugs. ActivePower is in milliwatts and
# CumulativeEnergyImported in milliwatt-hours, as the Matter specification counts them. A plug
# that measures them also gives RMSVoltage in millivolts and RMSCurrent in milliamps.
ELECTRICAL_POWER_MEASUREMENT = "ElectricalPowerMeasurement"
ELECTRICAL_ENERGY_MEASUREMENT = "ElectricalEnergyMeasurement"
MEDIA_PLAYBACK = "MediaPlayback"
PORCH_LAUNDRY = "PorchLaundryExtension"
# What a doorbell can do. The hub reads this from the doorbell's platform; it has no commands.
PORCH_DOORBELL = "PorchDoorbellExtension"
# Whether a TV is in use, in its platform's own words. Matter's Media Playback cluster says what
# is playing, and has no field for this. The hub reads it; it has no commands.
PORCH_MEDIA = "PorchMediaExtension"


def on_off(on: bool | None = False, commands: Sequence[str] = ("On", "Off", "Toggle")) -> Cluster:
    """on is None when the platform does not say: unknown is never reported as off."""
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


def rvc_operational_state(state: str = "Docked",
                          commands: Sequence[str] = ("Start", "Pause", "GoHome")) -> Cluster:
    """Vacuum state: Stopped, Running, Paused, Error, SeekingCharger, Charging, Docked or Unknown.

    A vacuum the hub can only read (a stick vacuum) passes no commands.
    """
    return Cluster(attributes={"OperationalState": state}, commands=list(commands))


def rvc_clean_mode(mode: str) -> Cluster:
    """The cleaning mode a vacuum is in now, by its plain name: Off, Normal, High, Turbo, Mop, Auto."""
    return Cluster(attributes={"CurrentMode": mode})


def power_source(battery_percent: float | None = None, charge_level: str | None = None) -> Cluster:
    """A battery, by what the device reports: a percentage, a charge level, or both.

    battery_percent is 0 to 100; Matter's BatPercentRemaining counts half percents, 0 to 200.
    charge_level is Matter's BatChargeLevel: OK, Warning or Critical.
    """
    attributes: dict[str, int | str] = {}
    if battery_percent is not None:
        attributes["BatPercentRemaining"] = max(0, min(200, round(battery_percent * 2)))
    if charge_level is not None:
        attributes["BatChargeLevel"] = charge_level
    return Cluster(attributes=attributes)


def fan_control(fan_mode: str) -> Cluster:
    return Cluster(attributes={"FanMode": fan_mode})


def air_quality(level: str) -> Cluster:
    """Good, Fair, Moderate, Poor, VeryPoor, ExtremelyPoor or Unknown."""
    return Cluster(attributes={"AirQuality": level})


def pm25(measured_value: float) -> Cluster:
    return Cluster(attributes={"MeasuredValue": measured_value})


def particles(measured_value: float) -> Cluster:
    """A particle reading, for the PM1 and PM10 clusters: the same shape as PM2.5's."""
    return Cluster(attributes={"MeasuredValue": measured_value})


def hepa_filter(percent_left: float) -> Cluster:
    """Matter's Condition for a filter that counts down: 100 is new, 0 is used up."""
    return Cluster(attributes={"Condition": percent_left})


def level_control(level: int) -> Cluster:
    """A light's brightness as Matter's CurrentLevel, 1 to 254. No commands: read only."""
    return Cluster(attributes={"CurrentLevel": level})


def color_control(mireds: int) -> Cluster:
    """A light's white tone in mireds: about 153 is cool daylight, about 400 is warm. Read only."""
    return Cluster(attributes={"ColorTemperatureMireds": mireds})


def porch_doorbell(button_press_supported: bool = True) -> Cluster:
    return Cluster(attributes={"ButtonPressSupported": button_press_supported})


def media_playback(state: str | None, commands: Sequence[str] = ("Play", "Pause")) -> Cluster:
    """What a TV is playing: Playing, Paused, NotPlaying or Buffering.

    state is None when the platform does not say: unknown is never reported as not playing.
    """
    return Cluster(attributes={"CurrentState": state}, commands=list(commands))


def porch_media(activity: str | None) -> Cluster:
    """Active, Standby or Inactive, as the TV's platform reports it. None when it does not say."""
    return Cluster(attributes={"ActivityState": activity})


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
