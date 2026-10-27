"""The commands the hub understands, and the attribute values each one must produce.

One table drives both the router's read-back check and the digital copy's
simulated effect, so the two cannot drift apart.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import clusters as c
from .models import Clusters, Device

Attributes = dict[str, Any]


class NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DelayStartParams(NoParams):
    hours: int = Field(ge=0, le=23)
    minutes: int = Field(default=0, ge=0, le=59)


class OneOf(tuple):
    """An expected attribute value that may settle on any of several values.

    A robot sent home may report SeekingCharger, then Charging, then Docked.
    The digital copy applies the first.
    """


class CannotPredict(Exception):
    """The device's current state does not say what the command would do (Toggle from unknown)."""


class UnsupportedCommand(Exception):
    """The device does not expose this command."""


class InvalidParams(Exception):
    def __init__(self, command: str, errors: list[dict[str, Any]]) -> None:
        super().__init__(f"invalid params for {command}")
        self.errors = errors


@dataclass(frozen=True)
class CommandSpec:
    name: str
    cluster: str
    # (validated params, cluster attributes before) -> attributes the device must report after
    expect: Callable[[Any, Attributes], Attributes]
    params: type[BaseModel] = NoParams

    def parse_params(self, raw: dict[str, Any]) -> BaseModel:
        try:
            return self.params.model_validate(raw)
        except ValidationError as exc:
            raise InvalidParams(self.name, exc.errors(include_url=False, include_context=False)) from exc


def _known_bool(attributes: Attributes, name: str) -> bool:
    value = attributes.get(name)
    if type(value) is not bool:
        raise CannotPredict(f"cannot toggle from an unknown {name} state ({value!r})")
    return value


def _sets(**attrs: Any) -> Callable[[Any, Attributes], Attributes]:
    return lambda _params, _before: dict(attrs)


_SPECS = [
    CommandSpec("On", c.ON_OFF, _sets(OnOff=True)),
    CommandSpec("Off", c.ON_OFF, _sets(OnOff=False)),
    CommandSpec("Toggle", c.ON_OFF, lambda _p, before: {"OnOff": not _known_bool(before, "OnOff")}),
    CommandSpec("LockDoor", c.DOOR_LOCK, _sets(LockState="Locked")),
    CommandSpec("UnlockDoor", c.DOOR_LOCK, _sets(LockState="Unlocked")),
    CommandSpec("Open", c.VALVE, _sets(CurrentState="Open")),
    CommandSpec("Close", c.VALVE, _sets(CurrentState="Closed")),
    CommandSpec("Start", c.OPERATIONAL_STATE, _sets(OperationalState="Running")),
    CommandSpec("Stop", c.OPERATIONAL_STATE, _sets(OperationalState="Stopped")),
    CommandSpec("Pause", c.OPERATIONAL_STATE, _sets(OperationalState="Paused")),
    CommandSpec("Resume", c.OPERATIONAL_STATE, _sets(OperationalState="Running")),
    CommandSpec("Start", c.RVC_OPERATIONAL_STATE, _sets(OperationalState="Running")),
    CommandSpec("Pause", c.RVC_OPERATIONAL_STATE, _sets(OperationalState="Paused")),
    CommandSpec("GoHome", c.RVC_OPERATIONAL_STATE,
                _sets(OperationalState=OneOf(("SeekingCharger", "Charging", "Docked")))),
    CommandSpec(
        "SetDelayStart",
        c.PORCH_LAUNDRY,
        lambda p, _before: {"RelativeHourToStart": p.hours, "RelativeMinuteToStart": p.minutes},
        DelayStartParams,
    ),
]


def resolve(device: Device, command: str) -> CommandSpec:
    """Return the spec for a command the device actually exposes, on the cluster that exposes it."""
    for spec in _SPECS:
        cluster = device.clusters.get(spec.cluster)
        if spec.name == command and cluster is not None and command in cluster.commands:
            return spec
    supported = sorted({cmd for cl in device.clusters.values() for cmd in cl.commands})
    raise UnsupportedCommand(f"{device.id} does not support {command!r}; supported: {supported}")


def supports(device: Device, spec: CommandSpec) -> bool:
    """Whether one record (a single path's view of a device) exposes the command."""
    cluster = device.clusters.get(spec.cluster)
    return cluster is not None and spec.name in cluster.commands


def expected_attributes(spec: CommandSpec, params: BaseModel, state: Clusters) -> Attributes:
    before = state[spec.cluster].attributes if spec.cluster in state else {}
    return spec.expect(params, before)


def is_confirmed(spec: CommandSpec, expected: Attributes, state: Clusters) -> bool:
    cluster = state.get(spec.cluster)
    return cluster is not None and all(
        cluster.attributes.get(k) in v if isinstance(v, OneOf) else cluster.attributes.get(k) == v
        for k, v in expected.items()
    )


def apply(spec: CommandSpec, params: BaseModel, state: Clusters) -> None:
    """Apply a command's effect to a digital copy, in place."""
    expected = expected_attributes(spec, params, state)
    state[spec.cluster].attributes.update(
        {k: v[0] if isinstance(v, OneOf) else v for k, v in expected.items()}
    )
