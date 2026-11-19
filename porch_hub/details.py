"""Everything a platform reports about one device, in plain words, for the device panels.

GET /devices/{id}/details asks each platform that reaches the device for its own full record:
LG's status, Google Home's traits, the Matter node's attributes, Ring's status, capabilities and
settings. The hub's own model (clusters) keeps only what it acts on; this shows the rest.

Each platform's answer becomes rows of (group, name, value), with names and values turned into
words. What could identify the device or the home's network is left out: serial numbers,
hardware and network addresses, network names and ids, keys and tokens. So is bookkeeping that
means nothing to a person (Matter's attribute and command lists, feature maps, revisions).
"""

import re
from typing import Any

# Keys that could identify a device, a person, the home or its network: never shown, nor anything
# under them. Matched in any letter case, except the camelCase endings: deviceId and apiKey are
# hidden, android and valid are not.
PRIVATE = re.compile(
    r"(?i:serial|unique_?id|ssid|address|network_?id|ipv[46]|token|secret|password|passphrase|credential|"
    r"fabric|node_?label|email|latitude|longitude|geoloc|coordinates|^id$|_id$|^key$|_key$)"
    r"|(?i:mac)(?![a-z])"  # macAddress, mac_address, MAC; not machine
    r"|(?<=[a-z0-9])I[dD]$|(?<=[a-z0-9])Key$")
# Bookkeeping that means nothing to a person.
NOISE = {"attributeList", "acceptedCommandList", "generatedCommandList", "eventList", "featureMap",
         "clusterRevision", "timestamp", "type", "connectivityState", "metadata", "image_url"}


# Short forms people read as letters, in the platforms' snake_case keys (Ring's hdr, ir_led, e2e).
ACRONYMS = {"hdr": "HDR", "ir": "IR", "led": "LED", "e2e": "E2E", "tv": "TV", "usb": "USB", "rms": "RMS",
            "pm": "PM", "wifi": "Wi-Fi", "wi-fi": "Wi-Fi"}


def words(key: str) -> str:
    """airQualitySensor -> Air quality sensor, PM10Level -> PM10 level, filter_remain -> Filter remain,
    ir_led_night_vision -> IR LED night vision, softwareVersionString -> Software version."""
    text = str(key).split("/")[-1].replace("_", " ")
    text = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[0-9])(?=[A-Z][a-z])", " ", text)
    parts = [ACRONYMS.get(p.lower()) or (p if (p.isupper() and len(p) > 1) or any(c.isdigit() for c in p) else p.lower())
             for p in text.split()]
    if len(parts) > 2 and parts[-2:] == ["version", "string"]:
        parts = parts[:-1]  # Matter's VersionString is the version as people read it
    return " ".join(parts)[:1].upper() + " ".join(parts)[1:] if parts else str(key)


def enum_text(value: str, key: str | None = None) -> str:
    """PLAYBACK_STATE_ENUM_PLAYING -> Playing, CHARGING_COMPLETE -> Charging complete, ONLINE -> Online,
    and under activityState, ACTIVITY_STATE_ACTIVE -> Active (the key is not said twice).
    Model names (P110M) and short marks (LG, TV, AC) stay as they are."""
    if not (re.fullmatch(r"[A-Z]{3,}", value) or ("_" in value and re.fullmatch(r"[A-Z0-9_]+", value))):
        return value
    text = value.split("_ENUM_")[-1]
    if key:
        repeated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key).split("/")[-1]).upper() + "_"
        if text.startswith(repeated) and len(text) > len(repeated):
            text = text[len(repeated):]
    text = text.replace("_", " ").strip()
    return text.capitalize() if text else value


def count(n: int, thing: str, things: str | None = None) -> str:
    return f"{n} {thing if n == 1 else things or thing + 's'}"


def value_text(value: Any, key: str | None = None) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, dict):  # a record nested too deep to list: its size, never its keys
        return count(len(value), "entry", "entries")
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, str):
        return enum_text(value, key)
    if isinstance(value, list):
        if not value:
            return "none"
        if all(not isinstance(v, (dict, list)) for v in value):
            return ", ".join(value_text(v) for v in value[:20]) + (f", and {len(value) - 20} more" if len(value) > 20 else "")
        return count(len(value), "item")
    return str(value)


def private(key: str) -> bool:
    return bool(PRIVATE.search(str(key)))


def rows_from(group: str, mapping: Any, depth: int = 0) -> list[dict[str, str]]:
    """Rows for one group from a nested record. Nested records become their own groups."""
    rows: list[dict[str, str]] = []
    if not isinstance(mapping, dict):
        return [{"group": group, "name": "Value", "value": value_text(mapping)}]
    for key, value in mapping.items():
        if key in NOISE or private(key):
            continue
        if isinstance(value, dict) and depth < 3:
            rows += rows_from(f"{group} · {words(key)}" if group else words(key), value, depth + 1)
        elif isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            rows.append({"group": group, "name": words(key), "value": list_text(value)})
        else:
            text = value_text(value, key)
            if words(key).endswith(" percent") and isinstance(value, (int, float)) and not isinstance(value, bool):
                text += "%"
            rows.append({"group": group, "name": words(key), "value": text})
    return rows


def list_text(items: list[dict]) -> str:
    """A list of records, by the names they carry: apps by their names, zones by their count."""
    names = []
    for item in items:
        name = item.get("name") or item.get("displayName")
        if not name and isinstance(item.get("names"), list) and item["names"]:
            synonyms = item["names"][0].get("nameSynonym") if isinstance(item["names"][0], dict) else None
            name = synonyms[0] if synonyms else None
        if not name and isinstance(item.get("keyCode"), str):
            name = item["keyCode"].removeprefix("KEY_CODE_").replace("_", " ").strip().capitalize() or None
        if name:
            names.append(str(name))
    if names:
        return ", ".join(names[:20]) + (f", and {len(names) - 20} more" if len(names) > 20 else "")
    return count(len(items), "item")


def rows_from_clusters(clusters: dict) -> list[dict[str, str]]:
    """The hub's own model of a device, for a platform with nothing more to say (simulated ones)."""
    rows = []
    for name, cluster in clusters.items():
        attributes = cluster.attributes if hasattr(cluster, "attributes") else cluster.get("attributes", {})
        for attribute, value in attributes.items():
            rows.append({"group": words(name), "name": words(attribute), "value": value_text(value)})
    return rows
