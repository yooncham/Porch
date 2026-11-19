"""Porch's household rules: who an agent acts for, who must be asked, and what an energy agent sees.

Pure functions, so they can be checked without a running gateway (tests/test_household_rules.py).
porch_gateway.py applies them on top of its own capability rules.

The room rule. An agent acts for one person in the household. If someone else is in the room a
device is in, and the agent's own person is not in that room, the agent may not change the device
until someone in the room says yes. The person in the room decides, not the person who is away.
Where people are comes from the Device Hub (GET /household), set there by hand with a PIN.

- Someone home with no room set counts as in every room: Porch cannot tell, so it asks them.
- A device the floor plan places in no room counts as in every room, for the same reason.
- Unknown (nobody has said where a person is) counts as away.
- Commands that make a device safer (switching a heater off, locking a door) and the emergency
  water shut-off are never held by this rule.

Energy agents. An agent with "reads": "energy" sees energy readings only: the devices that report
power or energy, and only those readings and their switch. It controls nothing.
"""

from typing import Any

ENERGY_CLUSTERS = ("ElectricalPowerMeasurement", "ElectricalEnergyMeasurement")
ENERGY_ATTRIBUTE_PREFIXES = tuple(f"{c}." for c in ENERGY_CLUSTERS) + ("OnOff.OnOff",)


def person_of(agent: dict, people: list[dict]) -> dict | None:
    """The household person an agent acts for: its "person" id, else the person its acts_for names."""
    wanted = (agent.get("person") or "").strip().lower()
    for person in people:
        if wanted and person["id"] == wanted:
            return person
    named = (agent.get("acts_for") or "").strip().lower()
    return next((p for p in people if named and named in (p["id"].lower(), p["name"].lower())), None)


def _home(person: dict) -> bool:
    return person.get("where") == "home"


def may_be_in(person: dict, room: str | None) -> bool:
    """Whether this person is, or may be, in the room: home, and in it or with no room set."""
    return _home(person) and (room is None or person.get("room") is None or person.get("room") == room)


def surely_in(person: dict | None, room: str | None) -> bool:
    """Whether this person is known to be in the room."""
    return bool(person) and _home(person) and room is not None and person.get("room") == room


def must_ask(agent: dict, room: str | None, people: list[dict]) -> list[dict]:
    """Who must say yes before this agent changes a device in this room. Empty: nobody."""
    mine = person_of(agent, people)
    others = [p for p in people if (mine is None or p["id"] != mine["id"]) and may_be_in(p, room)]
    if not others or surely_in(mine, room):
        return []
    return others


def why_ask(agent: dict, room_name: str | None, asked: list[dict], people: list[dict]) -> str:
    names = " or ".join(p["name"] for p in asked)
    mine = person_of(agent, people)
    place = f"the {room_name}" if room_name else "the home, and Porch cannot tell which room this device is in"
    if len(asked) == 1 and asked[0].get("room") is None and room_name:
        place = f"the home with no room set, so may be in the {room_name}"
    verb = "is" if len(asked) == 1 else "are"
    if mine is None and not agent.get("person"):  # an agent for the whole household asks whoever is there
        return (f"{names} {verb} in {place}, and {agent.get('name') or 'this agent'} acts for the whole household, "
                f"so {names}'s permission is needed.")
    absent = mine["name"] if mine else agent.get("acts_for") or "the agent's person"
    return f"{names} {verb} in {place}, and {absent} is not, so {names}'s permission is needed."


def reads_energy_only(agent: dict) -> bool:
    return agent.get("reads") == "energy"


def is_meter(device: dict) -> bool:
    return any(name in (device.get("clusters") or {}) for name in ENERGY_CLUSTERS)


def energy_state(state: dict[str, Any]) -> dict[str, Any]:
    """Only a device's energy readings and its switch, from a flattened state."""
    return {k: v for k, v in state.items() if k.startswith(ENERGY_ATTRIBUTE_PREFIXES)}
