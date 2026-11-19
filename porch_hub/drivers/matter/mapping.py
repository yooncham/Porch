"""Matter node data to Porch devices, and Porch commands to Matter invokes.

Node attributes arrive keyed by "endpoint/cluster/attribute" with decimal ids
(1/257/0 is DoorLock LockState on endpoint 1). Each endpoint that carries a
supported device type becomes one Porch device. Enum attributes arrive as
numbers and are turned into the Matter specification's names.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ... import clusters as c
from ...dedup import hardware_address
from ...models import Cluster, Clusters, DeviceType

ROOT_ENDPOINT = 0
DESCRIPTOR = 0x001D  # DeviceTypeList is attribute 0
BASIC_INFORMATION = 0x0028  # VendorName 1, ProductName 3, NodeLabel 5, SerialNumber 15
BRIDGED_DEVICE_BASIC_INFORMATION = 0x0039  # NodeLabel 5 and SerialNumber 18, per bridged endpoint
GENERAL_DIAGNOSTICS = 0x0033  # NetworkInterfaces 0
ACCEPTED_COMMAND_LIST = 0xFFF9  # global attribute on every cluster


def _bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _enum(names: dict[int, str]) -> Callable[[Any], str]:
    return lambda value: "Unknown" if value is None else names.get(value, f"Unknown({value})")


def _int(value: Any) -> int | None:
    """A nullable Matter number. Null means the device has no reading yet."""
    return None if value is None or isinstance(value, bool) else int(value)


def _energy(value: Any) -> int | None:
    """An EnergyMeasurementStruct reduced to its energy field, in milliwatt-hours."""
    if not isinstance(value, dict):
        return None
    # Struct fields come keyed by name or, from older servers, by tag number (energy is tag 0).
    return _int(value.get("energy", value.get("0")))


@dataclass(frozen=True)
class ClusterMap:
    cluster_id: int
    name: str
    attributes: dict[int, tuple[str, Callable[[Any], Any]]]
    commands: dict[str, int] = field(default_factory=dict)  # Porch/Matter command name -> command id
    timed_commands: frozenset[str] = frozenset()  # the spec requires these as timed invokes
    events: dict[int, str] = field(default_factory=dict)
    # Readings a device may or may not have. They are read and listed only for a device that
    # reports them, so a device without them is not shown an empty value and is not asked for it.
    optional: dict[int, tuple[str, Callable[[Any], Any]]] = field(default_factory=dict)


CLUSTERS: dict[int, ClusterMap] = {m.cluster_id: m for m in [
    ClusterMap(0x0006, c.ON_OFF, {0: ("OnOff", _bool)}, {"Off": 0, "On": 1, "Toggle": 2}),
    ClusterMap(
        0x0101, c.DOOR_LOCK,
        {0: ("LockState", _enum({0: "NotFullyLocked", 1: "Locked", 2: "Unlocked", 3: "Unlatched"}))},
        {"LockDoor": 0, "UnlockDoor": 1},
        timed_commands=frozenset({"LockDoor", "UnlockDoor"}),
        events={0: "DoorLockAlarm", 2: "LockOperation", 3: "LockOperationError"},
    ),
    # For a Water Leak Detector, StateValue True means water is detected.
    ClusterMap(0x0045, c.BOOLEAN_STATE, {0: ("StateValue", _bool)}, events={0: "StateChange"}),
    ClusterMap(0x0081, c.VALVE, {4: ("CurrentState", _enum({0: "Closed", 1: "Open", 2: "Transitioning"}))},
               {"Open": 0, "Close": 1}),
    # Readings only, no commands. ActivePower in mW, CumulativeEnergyImported in mWh, and, from
    # a plug that measures them, voltage in mV and current in mA.
    ClusterMap(0x0090, c.ELECTRICAL_POWER_MEASUREMENT, {8: ("ActivePower", _int)},
               optional={11: ("RMSVoltage", _int), 12: ("RMSCurrent", _int),
                         4: ("Voltage", _int), 5: ("ActiveCurrent", _int)}),
    ClusterMap(0x0091, c.ELECTRICAL_ENERGY_MEASUREMENT, {1: ("CumulativeEnergyImported", _energy)}),
]}

BOOLEAN_STATE_ID = 0x0045
ENERGY_CLUSTER_IDS = frozenset({0x0090, 0x0091})

DEVICE_TYPES: dict[int, DeviceType] = {
    0x000A: DeviceType.DOOR_LOCK,
    0x010A: DeviceType.ON_OFF_PLUG,
    0x010B: DeviceType.ON_OFF_PLUG,  # dimmable plug, controlled as on/off
    0x0100: DeviceType.ON_OFF_LIGHT,
    0x0101: DeviceType.ON_OFF_LIGHT,  # dimmable light, controlled as on/off
    0x010C: DeviceType.ON_OFF_LIGHT,  # color temperature light, controlled as on/off
    0x010D: DeviceType.ON_OFF_LIGHT,  # extended color light (for example Tapo L535E), controlled as on/off
    0x0043: DeviceType.WATER_LEAK_SENSOR,
    0x0042: DeviceType.WATER_VALVE,
}
# Only for endpoints without a Descriptor device type list. BooleanState is left out:
# without a device type it could as well be a contact sensor.
_FALLBACK_TYPES = {0x0101: DeviceType.DOOR_LOCK, 0x0081: DeviceType.WATER_VALVE, 0x0006: DeviceType.ON_OFF_PLUG}


def parse_path(path: str) -> tuple[int, int, int] | None:
    try:
        endpoint, cluster, attribute = (int(part) for part in path.split("/"))
    except ValueError:
        return None
    return endpoint, cluster, attribute


@dataclass(frozen=True)
class MatterBinding:
    """Where a Matter endpoint sits in the Porch home. Matter itself has no rooms.

    read_only keeps the hub from ever switching the device: its state and readings show, and it
    takes no command. It is for a plug whose load must stay on, such as the one that powers the
    Wi-Fi router the hub itself depends on.

    name is what the home calls the device. Without it the device carries the name its node
    reports, which is usually the product's ("Smart Wi-Fi Plug"), the same for every such plug.
    """

    porch_id: str
    room: str
    read_only: bool = False
    name: str | None = None


@dataclass(frozen=True)
class MatterEndpoint:
    """One Porch device: a supported endpoint on a Matter node."""

    device_id: str
    node_id: int
    endpoint_id: int
    device_type: DeviceType
    name: str
    room: str
    cluster_ids: tuple[int, ...]
    serial_number: str | None = None
    mac_address: str | None = None
    # (endpoint, cluster) pairs read from another endpoint of the same node: a plug whose
    # energy readings sit on a separate Electrical Sensor endpoint. Never command targets.
    linked: tuple[tuple[int, int], ...] = ()
    # From the binding: the hub reads this device and never commands it.
    read_only: bool = False
    # The optional readings this device reports (see ClusterMap.optional), as attribute paths.
    optional_paths: tuple[str, ...] = ()

    def locations(self) -> list[tuple[int, int]]:
        """Every (endpoint, cluster) this device's state comes from."""
        return [(self.endpoint_id, cid) for cid in self.cluster_ids] + list(self.linked)

    def covers(self, endpoint_id: int, cluster_id: int) -> bool:
        return (endpoint_id, cluster_id) in self.locations()

    def required_paths(self) -> list[str]:
        """The attributes every device of this kind has."""
        return [f"{ep}/{cid}/{aid}" for ep, cid in self.locations() for aid in CLUSTERS[cid].attributes]

    def attribute_paths(self) -> list[str]:
        """Everything read for this device: the required attributes and the optional ones it reports."""
        return self.required_paths() + list(self.optional_paths)

    def to_clusters(self, attributes: dict[str, Any]) -> Clusters:
        clusters: Clusters = {}
        for ep, cid in self.locations():
            cmap = CLUSTERS[cid]
            values = {name: decode(attributes.get(f"{ep}/{cid}/{aid}"))
                      for aid, (name, decode) in cmap.attributes.items()}
            values.update({name: decode(attributes.get(f"{ep}/{cid}/{aid}"))
                           for aid, (name, decode) in cmap.optional.items()
                           if f"{ep}/{cid}/{aid}" in self.optional_paths})
            accepted = attributes.get(f"{ep}/{cid}/{ACCEPTED_COMMAND_LIST}")
            commands = [] if self.read_only else [name for name, cmd_id in cmap.commands.items()
                                                  if not isinstance(accepted, list) or cmd_id in accepted]
            clusters[cmap.name] = Cluster(attributes=values, commands=commands)
        return clusters

    def command_target(self, command: str) -> ClusterMap | None:
        if self.read_only:
            return None
        for cid in self.cluster_ids:
            if command in CLUSTERS[cid].commands:
                return CLUSTERS[cid]
        return None


def _device_types(attributes: dict[str, Any], endpoint: int) -> list[int]:
    entries = attributes.get(f"{endpoint}/{DESCRIPTOR}/0") or []
    # Structs come keyed by field name or, from older servers, by tag number.
    return [e.get("deviceType", e.get("0")) for e in entries if isinstance(e, dict)]


def _node_name(attributes: dict[str, Any], node_id: int, endpoint: int) -> str:
    for path in (f"{endpoint}/{BRIDGED_DEVICE_BASIC_INFORMATION}/5", f"{ROOT_ENDPOINT}/{BASIC_INFORMATION}/5",
                 f"{ROOT_ENDPOINT}/{BASIC_INFORMATION}/3"):
        if name := attributes.get(path):
            return str(name)
    return f"Matter node {node_id}"


def _serial_number(attributes: dict[str, Any], endpoint: int, shared_root: bool) -> str | None:
    """The endpoint's own serial (bridged devices), else the node's, unless several devices share it."""
    if serial := attributes.get(f"{endpoint}/{BRIDGED_DEVICE_BASIC_INFORMATION}/18"):
        return str(serial)
    if not shared_root and (serial := attributes.get(f"{ROOT_ENDPOINT}/{BASIC_INFORMATION}/15")):
        return str(serial)
    return None


def _mac_address(attributes: dict[str, Any]) -> str | None:
    """The first operational interface's hardware address: a MAC on Wi-Fi or Ethernet, an EUI-64 on Thread."""
    interfaces = attributes.get(f"{ROOT_ENDPOINT}/{GENERAL_DIAGNOSTICS}/0") or []
    for interface in sorted((i for i in interfaces if isinstance(i, dict)),
                            key=lambda i: not i.get("isOperational", i.get("1", False))):
        if address := hardware_address(interface.get("hardwareAddress", interface.get("4"))):
            return address
    return None


def node_endpoints(node: dict[str, Any], home_id: str, bindings: dict[str, MatterBinding]) -> list[MatterEndpoint]:
    """The Porch devices on one node. Endpoints with no supported device type are skipped."""
    node_id = node["node_id"]
    attributes: dict[str, Any] = node.get("attributes", {})
    clusters_by_endpoint: dict[int, set[int]] = {}
    for path in attributes:
        if (parsed := parse_path(path)) and parsed[0] != ROOT_ENDPOINT and parsed[1] in CLUSTERS:
            clusters_by_endpoint.setdefault(parsed[0], set()).add(parsed[1])

    supported: list[tuple[int, DeviceType, tuple[int, ...]]] = []
    for endpoint_id, cluster_ids in sorted(clusters_by_endpoint.items()):
        declared = _device_types(attributes, endpoint_id)
        device_type = next((DEVICE_TYPES[t] for t in declared if t in DEVICE_TYPES), None)
        if device_type is None and not declared:
            device_type = next((_FALLBACK_TYPES[cid] for cid in sorted(cluster_ids) if cid in _FALLBACK_TYPES), None)
        if device_type is not None:
            supported.append((endpoint_id, device_type, tuple(sorted(cluster_ids))))

    linked = _linked_energy(node, clusters_by_endpoint, supported)

    endpoints = []
    # A bridge's MAC belongs to the bridge, not to the devices behind it.
    mac = _mac_address(attributes) if not node.get("is_bridge") else None
    # A bridge's or multi-device node's root serial does not say which of its devices is which.
    shared_root = bool(node.get("is_bridge")) or len(supported) > 1
    for endpoint_id, device_type, cluster_ids in supported:
        binding = bindings.get(f"{node_id}/{endpoint_id}") or MatterBinding(
            f"{home_id}/matter-{node_id}-{endpoint_id}", "Unassigned")
        name = binding.name or _node_name(attributes, node_id, endpoint_id)
        if len(supported) > 1 and not binding.name:
            name = f"{name} ({endpoint_id})"
        places = [(endpoint_id, cid) for cid in cluster_ids] + list(linked.get(endpoint_id, ()))
        optional = tuple(path for ep, cid in places for aid in CLUSTERS[cid].optional
                         if (path := f"{ep}/{cid}/{aid}") in attributes)
        endpoints.append(MatterEndpoint(binding.porch_id, node_id, endpoint_id, device_type, name,
                                        binding.room, cluster_ids,
                                        _serial_number(attributes, endpoint_id, shared_root), mac,
                                        linked.get(endpoint_id, ()), binding.read_only, optional))
    return endpoints


def _linked_energy(node: dict[str, Any], clusters_by_endpoint: dict[int, set[int]],
                   supported: list[tuple[int, DeviceType, tuple[int, ...]]]) -> dict[int, tuple[tuple[int, int], ...]]:
    """Energy readings on their own endpoint, given to the node's one plug.

    Some plugs report power and energy on a separate Electrical Sensor endpoint. When the
    node has exactly one plug, those readings are that plug's. On a bridge, or with several
    plugs, nothing says which plug they measure, so they are left out.
    """
    plugs = [(ep, cids) for ep, device_type, cids in supported if device_type == DeviceType.ON_OFF_PLUG]
    if node.get("is_bridge") or len(plugs) != 1:
        return {}
    plug_endpoint, plug_clusters = plugs[0]
    claimed = {ep for ep, _, _ in supported}
    source: dict[int, int] = {}  # cluster -> the lowest energy-only endpoint that has it
    for ep, cids in sorted(clusters_by_endpoint.items()):
        if ep in claimed or not cids <= ENERGY_CLUSTER_IDS:
            continue
        for cid in cids:
            if cid not in plug_clusters:
                source.setdefault(cid, ep)
    pairs = tuple(sorted((ep, cid) for cid, ep in source.items()))
    return {plug_endpoint: pairs} if pairs else {}
