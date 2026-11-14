"""List what each live platform reports, with ready-to-edit bindings for the hub.

Run it with the same environment as the hub (LG_THINQ_PAT, MATTER_SERVER_URL, PORCH_HOME_ID):

    .venv/bin/python -m porch_hub.discover

It only reads: nothing is sent to any device, and the hub's LG client id file is reused, never
replaced. Edit the printed LG_THINQ_BINDINGS and MATTER_BINDINGS so each id and room says what
the device really is, put them in your env file, and restart the hub.
"""

import asyncio
import json
import logging
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .drivers.base import DriverError
from .drivers.matter.mapping import (DESCRIPTOR, ENERGY_CLUSTER_IDS, ROOT_ENDPOINT, MatterBinding, node_endpoints,
                                     parse_path)
from .drivers.thinq.profiles import PROFILES
from .models import DeviceType

# Matter Device Library names for the device types a home is likely to report.
MATTER_DEVICE_TYPES = {
    0x000A: "Door Lock", 0x000F: "Generic Switch", 0x0011: "Power Source", 0x0012: "OTA Requestor",
    0x0013: "Bridged Node", 0x0015: "Contact Sensor", 0x0016: "Root Node", 0x0042: "Water Valve",
    0x0043: "Water Leak Detector", 0x0100: "On/Off Light", 0x0101: "Dimmable Light",
    0x010A: "On/Off Plug-in Unit", 0x010B: "Dimmable Plug-in Unit", 0x010C: "Color Temperature Light",
    0x010D: "Extended Color Light", 0x0510: "Electrical Sensor",
}
_READING_NAMES = {0x0090: "power", 0x0091: "energy"}

# The ids the demo scripts, the gateway config and the dashboards already use.
_LG_DEMO_IDS = {"DEVICE_AIR_PURIFIER": "air-purifier", "DEVICE_DRYER": "laundry-dryer",
                "DEVICE_WASHER": "laundry-washer", "DEVICE_ROBOT_CLEANER": "robot-vacuum"}
_MATTER_DEMO_IDS = {DeviceType.DOOR_LOCK: "front-door-lock", DeviceType.WATER_VALVE: "main-water-valve",
                    DeviceType.WATER_LEAK_SENSOR: "kitchen-leak-sensor"}

DEFAULT_ROOM = "Unassigned"


def _slug(text: str, fallback: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or fallback


def _unique(porch_id: str, taken: set[str]) -> str:
    candidate, n = porch_id, 2
    while candidate in taken:
        candidate, n = f"{porch_id}-{n}", n + 1
    taken.add(candidate)
    return candidate


# LG ThinQ

Bindings = dict[str, dict[str, str]]


def parse_bindings(env: Mapping[str, str], name: str) -> tuple[Bindings, str | None]:
    """The bindings already in the env file, and a problem with them if there is one."""
    raw = env.get(name, "").strip()
    if not raw:
        return {}, None
    try:
        value = json.loads(raw)
    except ValueError as exc:
        return {}, f"{name} is not valid JSON ({exc}); suggesting ids as if it were empty"
    if not isinstance(value, dict) or not all(isinstance(b, dict) and "porch_id" in b for b in value.values()):
        return {}, f'{name} must look like {{"<id>": {{"porch_id": "...", "room": "..."}}}}; ignoring it'
    return {key: {"porch_id": str(b["porch_id"]), "room": str(b.get("room", DEFAULT_ROOM))}
            for key, b in value.items()}, None


@dataclass(frozen=True)
class LgAppliance:
    lg_id: str
    device_type: str
    name: str
    model: str
    supported: bool  # the hub has a profile for this device type


def lg_appliances(entries: Sequence[dict[str, Any]]) -> list[LgAppliance]:
    """LG's device list as rows. Unsupported types are kept, so the owner sees why they are missing."""
    rows = []
    for entry in entries:
        info = entry.get("deviceInfo", {})
        device_type = str(info.get("deviceType", ""))
        rows.append(LgAppliance(lg_id=entry["deviceId"], device_type=device_type,
                                name=str(info.get("alias") or info.get("modelName") or entry["deviceId"]),
                                model=str(info.get("modelName", "")), supported=device_type in PROFILES))
    return rows


def lg_hub_id(appliance: LgAppliance, home_id: str, existing: Bindings) -> str | None:
    """The id the hub gives this appliance: its binding, else the same default the LG driver uses."""
    if not appliance.supported:
        return None
    if appliance.lg_id in existing:
        return existing[appliance.lg_id]["porch_id"]
    return f"{home_id}/{_slug(appliance.name, 'appliance')}-{appliance.lg_id[:6].lower()}"


def lg_bindings(appliances: Sequence[LgAppliance], home_id: str, existing: Bindings | None = None) -> Bindings:
    """The bindings to keep: existing ones as they are, plus a suggestion for each new appliance."""
    bindings = dict(existing or {})
    taken = {b["porch_id"] for b in bindings.values()}
    for a in appliances:
        if a.supported and a.lg_id not in bindings:
            base = _LG_DEMO_IDS.get(a.device_type) or _slug(a.name, "appliance")
            bindings[a.lg_id] = {"porch_id": _unique(f"{home_id}/{base}", taken), "room": DEFAULT_ROOM}
    return bindings


# Matter

@dataclass(frozen=True)
class MatterEndpointRow:
    node_id: int
    endpoint_id: int
    device_types: tuple[str, ...]
    name: str
    online: bool
    porch_id: str | None  # the hub's id for this endpoint, or None when the hub does not use it
    porch_type: str | None
    readings: str  # energy readings this endpoint gives, and where they go


def _declared_types(attributes: dict[str, Any], endpoint: int) -> list[int]:
    entries = attributes.get(f"{endpoint}/{DESCRIPTOR}/0") or []
    return [e.get("deviceType", e.get("0")) for e in entries if isinstance(e, dict)]


def _matter_bindings(existing: Bindings | None) -> dict[str, MatterBinding]:
    return {key: MatterBinding(b["porch_id"], b.get("room", DEFAULT_ROOM)) for key, b in (existing or {}).items()}


def matter_endpoints(nodes: Sequence[dict[str, Any]], home_id: str,
                     existing: Bindings | None = None) -> list[MatterEndpointRow]:
    """One row per endpoint of every node, with the Porch device it becomes, if any."""
    rows = []
    bindings = _matter_bindings(existing)
    for node in sorted(nodes, key=lambda n: n["node_id"]):
        node_id = node["node_id"]
        attributes: dict[str, Any] = node.get("attributes", {})
        used = {ep.endpoint_id: ep for ep in node_endpoints(node, home_id, bindings)}
        linked_to = {linked_ep: ep for ep in used.values() for linked_ep, _ in ep.linked}
        endpoint_ids = sorted({parsed[0] for path in attributes
                               if (parsed := parse_path(path)) and parsed[0] != ROOT_ENDPOINT})
        name = str(attributes.get(f"{ROOT_ENDPOINT}/40/5") or attributes.get(f"{ROOT_ENDPOINT}/40/3")
                   or f"Matter node {node_id}")
        for endpoint_id in endpoint_ids:
            types = tuple(MATTER_DEVICE_TYPES.get(t, f"0x{t:04X}") if isinstance(t, int) else str(t)
                          for t in _declared_types(attributes, endpoint_id))
            readings = sorted(_READING_NAMES[cid] for cid in ENERGY_CLUSTER_IDS
                              if any(path.startswith(f"{endpoint_id}/{cid}/") for path in attributes))
            device = used.get(endpoint_id)
            if device is not None:
                own = ", ".join(readings)
                extra = ", ".join(sorted(f"{_READING_NAMES[cid]} (endpoint {ep})" for ep, cid in device.linked))
                rows.append(MatterEndpointRow(node_id, endpoint_id, types, name, bool(node.get("available")),
                                              device.device_id, device.device_type.value,
                                              ", ".join(r for r in (own, extra) if r)))
            elif endpoint_id in linked_to:
                rows.append(MatterEndpointRow(node_id, endpoint_id, types, name, bool(node.get("available")),
                                              None, None, f"{', '.join(readings)} -> {linked_to[endpoint_id].device_id}"))
            else:
                note = f"{', '.join(readings)} not used: no single plug on this node" if readings else ""
                rows.append(MatterEndpointRow(node_id, endpoint_id, types, name, bool(node.get("available")),
                                              None, None, note))
    return rows


def matter_bindings(nodes: Sequence[dict[str, Any]], home_id: str, existing: Bindings | None = None) -> Bindings:
    """The bindings to keep: existing ones as they are, plus a suggestion for each new endpoint."""
    bindings = dict(existing or {})
    taken = {b["porch_id"] for b in bindings.values()}
    for node in sorted(nodes, key=lambda n: n["node_id"]):
        attributes = node.get("attributes", {})
        product = str(attributes.get(f"{ROOT_ENDPOINT}/40/5") or attributes.get(f"{ROOT_ENDPOINT}/40/3") or "")
        for ep in node_endpoints(node, home_id, {}):
            key = f"{ep.node_id}/{ep.endpoint_id}"
            if key not in bindings:
                base = _MATTER_DEMO_IDS.get(ep.device_type) or _slug(product, ep.device_type.value.lower())
                bindings[key] = {"porch_id": _unique(f"{home_id}/{base}", taken), "room": DEFAULT_ROOM}
    return bindings


# Reading the platforms

async def fetch_lg(env: Mapping[str, str]) -> list[dict[str, Any]]:
    from .drivers.thinq.connect import DEFAULT_CLIENT_ID_FILE, ThinQConnectCloud, load_client_id

    client_id = env.get("LG_THINQ_CLIENT_ID") or load_client_id(
        Path(env.get("LG_THINQ_CLIENT_ID_FILE", DEFAULT_CLIENT_ID_FILE)))
    cloud = ThinQConnectCloud(env["LG_THINQ_PAT"], env.get("LG_THINQ_COUNTRY", "US"), client_id)
    await cloud.start()
    try:
        return await cloud.list_devices()
    finally:
        await cloud.close()


async def fetch_matter(url: str, timeout_s: float = 10.0) -> list[dict[str, Any]] | None:
    """Every commissioned node, or None when the Matter Server cannot be reached."""
    from .drivers.matter.client import MatterServerClient
    from .drivers.matter.driver import server_url

    received: list[dict[str, Any]] = []
    client = MatterServerClient(server_url(url), on_nodes=lambda nodes: received.extend(nodes),
                                on_event=lambda *_: None, on_connection_change=lambda _: None)
    await client.start(timeout_s)
    try:
        return received if client.connected else None
    finally:
        await client.close()


# Output

def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [max(len(str(cell)) for cell in column) for column in zip(header, *rows)]
    return ["  " + "  ".join(str(cell).ljust(w) for cell, w in zip(row, widths)).rstrip() for row in [header, *rows]]


def lg_report(appliances: Sequence[LgAppliance], home_id: str = "home-1",
              existing: Bindings | None = None) -> list[str]:
    if not appliances:
        return ["  No appliances on this LG account."]
    existing = existing or {}
    return _table(("LG device id", "type", "name", "model", "hub id"),
                  [(a.lg_id, a.device_type, a.name, a.model,
                    lg_hub_id(a, home_id, existing) or "not used: no profile for this type yet")
                   for a in appliances])


def matter_report(rows: Sequence[MatterEndpointRow]) -> list[str]:
    if not rows:
        return ["  No devices commissioned to this Matter Server yet."]
    return _table(("node/endpoint", "Matter device types", "name", "online", "hub id", "hub type", "readings"),
                  [(f"{r.node_id}/{r.endpoint_id}", ", ".join(r.device_types) or "-", r.name,
                    "yes" if r.online else "no", r.porch_id or "not used", r.porch_type or "-", r.readings or "-")
                   for r in rows])


def env_line(name: str, bindings: dict[str, dict[str, str]]) -> str:
    return f"{name}='{json.dumps(bindings)}'"


async def run(env: Mapping[str, str] = os.environ, out=None, matter_timeout_s: float = 10.0) -> int:
    out = out or sys.stdout
    home_id = env.get("PORCH_HOME_ID", "home-1")
    if not env.get("LG_THINQ_PAT") and not env.get("MATTER_SERVER_URL"):
        print("Set LG_THINQ_PAT, MATTER_SERVER_URL or both, the same way you start the hub.", file=sys.stderr)
        return 2
    lines: list[str] = []
    env_lines: list[str] = []
    status = 0
    if env.get("LG_THINQ_PAT"):
        lines.append(f"LG ThinQ (country {env.get('LG_THINQ_COUNTRY', 'US')})")
        existing, problem = parse_bindings(env, "LG_THINQ_BINDINGS")
        if problem:
            lines.append(f"  {problem}")
        try:
            appliances = lg_appliances(await fetch_lg(env))
        except ImportError:
            lines.append('  LG\'s SDK is not installed: .venv/bin/pip install -e ".[lg]"')
            status = 1
        except DriverError as exc:
            lines.append(f"  Could not list LG appliances: {exc}")
            status = 1
        else:
            lines += lg_report(appliances, home_id, existing)
            env_lines.append(env_line("LG_THINQ_BINDINGS", lg_bindings(appliances, home_id, existing)))
        lines.append("")
    if url := env.get("MATTER_SERVER_URL"):
        lines.append(f"Matter Server at {url}")
        existing, problem = parse_bindings(env, "MATTER_BINDINGS")
        if problem:
            lines.append(f"  {problem}")
        nodes = await fetch_matter(url, matter_timeout_s)
        if nodes is None:
            lines.append("  Could not reach it. Is the Matter Server running, and is the URL right?")
            status = 1
        else:
            lines += matter_report(matter_endpoints(nodes, home_id, existing))
            env_lines.append(env_line("MATTER_BINDINGS", matter_bindings(nodes, home_id, existing)))
        lines.append("")
    if env_lines:
        lines.append("Bindings for your env file. Yours are kept as they are; check the ids and rooms of new ones:")
        lines += env_lines
    print("\n".join(lines), file=out)
    return status


def main() -> int:
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
