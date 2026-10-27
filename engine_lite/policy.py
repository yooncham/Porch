"""Which agent may do what: scopes, and the actions that always go to the owner.

A deliberately small slice of the Engine's policy. The real Engine adds risk
tiers, household rules, arbitration between agents and prompt-injection checks.
"""

from dataclasses import dataclass
from enum import StrEnum


class Verdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    NEEDS_OWNER_APPROVAL = "needs_owner_approval"


@dataclass(frozen=True)
class Scope:
    name: str
    label: str  # shown to people, e.g. on the stage display
    device_types: frozenset[str] | None  # None: every device
    commands: frozenset[str] | None  # None: every command


SCOPES: dict[str, Scope] = {
    "energy": Scope(
        "energy", "Energy Management Only",
        device_types=frozenset({"LaundryDryer", "LaundryWasher", "RoboticVacuumCleaner", "AirPurifier",
                                "OnOffPlug", "OnOffLight"}),
        commands=frozenset({"Start", "Stop", "Pause", "Resume", "SetDelayStart", "GoHome", "On", "Off", "Toggle"}),
    ),
    "full_home": Scope("full_home", "Full Home Access", device_types=None, commands=None),
}

# Held for the owner whatever the agent's scope: a mistaken or injected unlock lets a stranger in.
# The owner's approval is the authority, so it can allow what the agent's scope alone cannot.
OWNER_APPROVAL: frozenset[tuple[str, str]] = frozenset({("DoorLock", "UnlockDoor")})


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str


def decide(scope: Scope, device_type: str, command: str) -> Decision:
    if (device_type, command) in OWNER_APPROVAL:
        return Decision(Verdict.NEEDS_OWNER_APPROVAL,
                        f"{command} on a {device_type} always needs the owner's approval")
    if scope.device_types is not None and device_type not in scope.device_types:
        return Decision(Verdict.DENY, f"the {scope.label} scope does not cover {device_type} devices")
    if scope.commands is not None and command not in scope.commands:
        return Decision(Verdict.DENY, f"the {scope.label} scope does not allow {command}")
    return Decision(Verdict.ALLOW, f"within the {scope.label} scope")
