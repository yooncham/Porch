"""One record per physical device, however many platforms expose it.

A Yale lock can be in Google Home through the cloud and paired to Porch's own
Matter fabric at the same time. Each driver reports its own record. This
module finds the records that describe the same device and merges them into
one, keeping every path that can control it, local Matter first.

Records match, strongest evidence first, on:
  1. the same Porch id (set by bindings in both drivers),
  2. the same serial number,
  3. the same MAC address,
  4. the same device type, name and room (room must be known on both).
Two records from the same platform are never merged: one platform lists each
device once, so two of its records are two devices. A serial number or MAC
shared by several records on one platform (the outlets of one power strip,
the endpoints behind one bridge) says nothing about which device is which,
so it is not used for those records. Weaker evidence never overrides
stronger evidence against a match: different serial numbers keep two records
apart however alike their MACs or names are, and different MACs keep them
apart however alike their names are. Records of different device types never
merge, except through an explicit shared Porch id: a hub's or bridge's serial
number on a sensor's record must not fold the sensor into a lock.
"""

import base64
import binascii
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable

from .models import Clusters, ControlPath, Device

# Local Matter first: lowest latency, and it works without the internet.
# LG's cloud comes before Google's because it is LG's own, fuller API.
PATH_PRIORITY: tuple[ControlPath, ...] = (ControlPath.MATTER, ControlPath.LG_THINQ, ControlPath.GOOGLE_HOME)

_UNKNOWN_ROOMS = frozenset({"", "unassigned", "unknown", "none"})
_PLACEHOLDER_SERIALS = frozenset({"", "0", "NA", "NONE", "UNKNOWN", "TESTSN", "SERIALNUMBER"})


def normalize_serial(value: str | None) -> str | None:
    if value is None:
        return None
    serial = re.sub(r"[\s\-_:./]", "", str(value)).upper()
    return None if serial in _PLACEHOLDER_SERIALS else serial


def normalize_mac(value: str | None) -> str | None:
    """A MAC (EUI-48) or Thread extended address (EUI-64) as lowercase hex, whatever the separators."""
    if not value:
        return None
    digits = re.sub(r"[\s:\-.]", "", str(value)).lower()
    if not re.fullmatch(r"[0-9a-f]{12}|[0-9a-f]{16}", digits) or len(set(digits)) == 1:
        return None  # malformed, or all zeros / all ones
    return digits


def hardware_address(value: object) -> str | None:
    """A hardware address as platforms send it (hex text, base64 bytes, or a byte list) as aa:bb:..."""
    raw: bytes | None = None
    if isinstance(value, list) and all(isinstance(b, int) for b in value):
        raw = bytes(value)
    elif isinstance(value, str):
        if (digits := normalize_mac(value)) is not None:
            raw = bytes.fromhex(digits)
        else:
            try:
                raw = base64.b64decode(value, validate=True)
            except (binascii.Error, ValueError):
                return None
    if raw is None or len(raw) not in (6, 8) or normalize_mac(raw.hex()) is None:
        return None
    return ":".join(f"{b:02x}" for b in raw)


def _text(value: str) -> str:
    """Names and rooms compared by their letters and digits in any script (현관문 as well as Front-Door)."""
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join("".join(ch if ch.isalnum() else " " for ch in value).split())


def _priority(record: Device) -> int:
    path = record.primary_control_path
    return PATH_PRIORITY.index(path) if path in PATH_PRIORITY else len(PATH_PRIORITY)


def _keys(record: Device) -> dict[str, tuple]:
    keys: dict[str, tuple] = {"id": (record.id,)}
    if serial := normalize_serial(record.serial_number):
        keys["serial"] = (serial,)
    if mac := normalize_mac(record.mac_address):
        keys["mac"] = (mac,)
    room = _text(record.room)
    if room not in _UNKNOWN_ROOMS and (name := _text(record.name)):
        keys["name_room"] = (record.device_type, name, room)
    return keys


def merge_device_records(records: Iterable[Device]) -> list[Device]:
    """Merge records of the same physical device. Each record is one platform's view of one device.

    Input records carry their own platform in primary_control_path, as drivers
    report them. Output records list every platform in control_paths and say
    which platform-specific id belongs to each path in path_device_ids.
    """
    ordered = sorted(records, key=_priority)  # stable: keeps each platform's own order
    parent = list(range(len(ordered)))
    paths = [{r.primary_control_path} for r in ordered]
    serials = [{s} if (s := normalize_serial(r.serial_number)) else set() for r in ordered]
    macs = [{m} if (m := normalize_mac(r.mac_address)) else set() for r in ordered]
    types = [r.device_type for r in ordered]

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def conflicting(a: int, b: int, kind: str) -> bool:
        if kind == "id":
            return False  # the same Porch id is an explicit binding
        if types[a] != types[b]:
            return True
        if serials[a] and serials[b] and not serials[a] & serials[b]:
            return True
        return kind == "name_room" and bool(macs[a] and macs[b] and not macs[a] & macs[b])

    def union(i: int, j: int, kind: str) -> None:
        a, b = sorted((find(i), find(j)))
        if a == b or paths[a] & paths[b] or conflicting(a, b, kind):  # at most one record per platform
            return
        parent[b] = a
        paths[a] |= paths[b]
        serials[a] |= serials[b]
        macs[a] |= macs[b]

    all_keys = [_keys(r) for r in ordered]
    for kind in ("id", "serial", "mac", "name_room"):
        index: dict[tuple, list[int]] = defaultdict(list)
        for i, keys in enumerate(all_keys):
            if kind in keys:
                index[keys[kind]].append(i)
        for members in index.values():
            member_paths = [ordered[i].primary_control_path for i in members]
            if len(set(member_paths)) < len(member_paths):
                continue  # shared by several devices on one platform: not an identity
            for j in members[1:]:
                union(members[0], j, kind)

    groups: dict[int, list[Device]] = defaultdict(list)
    for i, record in enumerate(ordered):
        groups[find(i)].append(record)
    return [combine(group) for _, group in sorted(groups.items())]


def combine(group: list[Device]) -> Device:
    """One unified record from one device's per-platform records."""
    group = sorted(group, key=_priority)
    base = group[0]
    reachable = [r for r in group if r.online]
    # Local Matter by default; a cloud path takes over only while every better path is down.
    primary = (reachable or group)[0]

    clusters: Clusters = {}
    for record in [primary, *(r for r in group if r is not primary)]:
        for name, cluster in record.clusters.items():
            clusters.setdefault(name, cluster.model_copy(deep=True))

    return Device(
        id=base.id,
        name=base.name,
        room=next((r.room for r in group if _text(r.room) not in _UNKNOWN_ROOMS), base.room),
        device_type=base.device_type,
        online=bool(reachable),
        primary_control_path=primary.primary_control_path,
        control_paths=[r.primary_control_path for r in group],
        serial_number=next((r.serial_number for r in group if normalize_serial(r.serial_number)), None),
        mac_address=next((r.mac_address for r in group if normalize_mac(r.mac_address)), None),
        clusters=clusters,
        last_updated_at=max(r.last_updated_at for r in group),
        path_device_ids={r.primary_control_path: r.id for r in group},
    )
