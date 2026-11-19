"""Names and units for the Matter attributes people care about, for a device's full details.

Only what is named here is shown. Commissioning, access control, credentials, the node's serial
number and unique id, its label and its network interfaces (which carry hardware and IP
addresses) are never shown.
"""

from typing import Any

from ...details import value_text


def _scaled(divisor: float, unit: str, digits: int = 1):
    return lambda v: "unknown" if not isinstance(v, (int, float)) or isinstance(v, bool) else f"{v / divisor:.{digits}f} {unit}"


def _energy(v: Any) -> str:
    value = v.get("energy", v.get("0")) if isinstance(v, dict) else None
    return _scaled(1_000_000, "kWh", 3)(value)


def _enum(names: dict[int, str]):
    return lambda v: names.get(v, value_text(v)) if not isinstance(v, bool) else value_text(v)


def _duration(v: Any) -> str:
    if not isinstance(v, int) or isinstance(v, bool):
        return value_text(v)
    days, rest = divmod(v, 86400)
    hours, rest = divmod(rest, 3600)
    return f"{days} d {hours} h" if days else f"{hours} h {rest // 60} min"


def _start_up(v: Any) -> str:
    return {0: "Off", 1: "On", 2: "Opposite of before"}.get(v, "As it was before the power cut") if v is not None \
        else "As it was before the power cut"


def _level(v: Any) -> str:
    return f"{round(v / 2.54)}%" if isinstance(v, int) and not isinstance(v, bool) else value_text(v)


def _kelvin(v: Any) -> str:
    return f"{round(1_000_000 / v / 50) * 50} K" if isinstance(v, int) and v > 0 else value_text(v)


ATTRIBUTES: dict[int, tuple[str, dict[int, tuple[str, Any]]]] = {
    0x0006: ("Switch", {0x0000: ("On", value_text), 0x4003: ("After a power cut", _start_up)}),
    0x0008: ("Brightness", {0x0000: ("Level", _level)}),
    0x0028: ("Device", {0x0001: ("Maker", value_text), 0x0003: ("Product", value_text),
                        0x0008: ("Hardware version", value_text), 0x000A: ("Software version", value_text),
                        0x000B: ("Made on", value_text), 0x000E: ("Product label", value_text),
                        0x0011: ("Reachable", value_text)}),
    0x002A: ("Software updates", {0x0001: ("Update possible", value_text),
                                  0x0002: ("Update state", _enum({0: "Unknown", 1: "Idle", 2: "Checking", 3: "Waiting to check",
                                                                  4: "Downloading", 5: "Installing", 6: "Waiting to install",
                                                                  7: "Rolling back", 8: "Waiting for consent"}))}),
    0x0033: ("Health", {0x0001: ("Restarts", value_text), 0x0002: ("Up since the last restart", _duration),
                        0x0003: ("Hours in use", lambda v: f"{v} h" if isinstance(v, int) else value_text(v)),
                        0x0004: ("Last restart because", _enum({0: "Unspecified", 1: "Power on", 2: "Brown-out",
                                                                3: "Software watchdog", 4: "Hardware watchdog",
                                                                5: "Software update", 6: "Software reset"}))}),
    0x0036: ("Wi-Fi", {0x0002: ("Wi-Fi version", _enum({0: "a", 1: "b", 2: "g", 3: "n", 4: "ac", 5: "ax", 6: "ah"})),
                       0x0003: ("Channel", value_text),
                       0x0004: ("Signal", lambda v: f"{v} dBm" if isinstance(v, int) else value_text(v))}),
    0x0045: ("Sensor", {0x0000: ("Water detected", value_text)}),
    0x0081: ("Valve", {0x0004: ("Position", _enum({0: "Closed", 1: "Open", 2: "Moving"}))}),
    0x0090: ("Power", {0x0000: ("Supply", _enum({0: "Unknown", 1: "DC", 2: "AC"})),
                       0x0004: ("Voltage", _scaled(1000, "V")), 0x0005: ("Current", _scaled(1000, "A", 3)),
                       0x0008: ("Power", _scaled(1000, "W")), 0x000B: ("Voltage (RMS)", _scaled(1000, "V")),
                       0x000C: ("Current (RMS)", _scaled(1000, "A", 3)), 0x000D: ("Power (RMS)", _scaled(1000, "W")),
                       0x000E: ("Frequency", _scaled(1000, "Hz")), 0x0011: ("Power factor", _scaled(100, "", 2))}),
    0x0091: ("Energy", {0x0001: ("Used, counter", _energy), 0x0002: ("Sent back, counter", _energy),
                        0x0003: ("Used, last period", _energy), 0x0004: ("Sent back, last period", _energy)}),
    0x0101: ("Lock", {0x0000: ("State", _enum({0: "Not fully locked", 1: "Locked", 2: "Unlocked", 3: "Unlatched"})),
                      0x0003: ("Door", _enum({0: "Open", 1: "Closed", 2: "Jammed", 3: "Forced open", 5: "Ajar"}))}),
    0x0300: ("Colour", {0x0007: ("White tone", _kelvin), 0x0000: ("Hue", value_text), 0x0001: ("Saturation", value_text)}),
    0x0402: ("Temperature", {0x0000: ("Measured", _scaled(100, "°C"))}),
    0x0405: ("Humidity", {0x0000: ("Measured", _scaled(100, "%", 0))}),
}


def rows_for(attributes: dict[str, Any], endpoints: set[int]) -> list[dict[str, str]]:
    """Rows for the attributes of the given endpoints of one node (and its root, endpoint 0)."""
    rows = []
    for path in sorted(attributes, key=lambda p: tuple(int(x) for x in p.split("/")) if p.count("/") == 2 else (99,)):
        try:
            endpoint, cluster, attribute = (int(x) for x in path.split("/"))
        except ValueError:
            continue
        if endpoint not in endpoints and endpoint != 0:
            continue
        named = ATTRIBUTES.get(cluster)
        if named is None or attribute not in named[1]:
            continue
        name, show = named[1][attribute]
        rows.append({"group": named[0], "name": name, "value": show(attributes[path])})
    return rows
