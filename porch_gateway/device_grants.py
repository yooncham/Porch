"""Which devices each agent may control, as the owner chose them (agent-devices.json).

An agent with no entry may control any device its scopes allow, as before. An agent with an entry
may control only the devices it lists, still within its scopes and every other rule: Porch's own,
the household's, and family protection in the hub. What an agent may read is not limited.

The owner chooses them on the hub's How Porch works page, with the owner PIN, or with
`deploy/home/agent.sh devices <agent> <device ids>`. The gateway reads the file on every request,
so a change applies at once, with no restart.

    {"agents": {"muse": {"devices": ["home-1/living-stand"],
                         "changed": "2026-11-17T03:00:00+00:00", "by": "the owner's page"}}}
"""

import json
from pathlib import Path

FILE = "agent-devices.json"


class Unreadable(Exception):
    """The file is there but cannot be read. Porch then controls nothing for anyone rather than guess
    who was limited."""


def read(path: Path) -> dict[str, list[str]]:
    """agent id -> the device ids it may control. No file: no agent is limited."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise Unreadable(f"{path.name} could not be read ({type(exc).__name__})") from exc
    try:
        grants = {}
        for agent_id, entry in json.loads(text)["agents"].items():
            devices = entry["devices"]
            if not isinstance(devices, list) or not all(isinstance(d, str) for d in devices):
                raise TypeError(agent_id)
            grants[str(agent_id)] = list(devices)
        return grants
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise Unreadable(f"{path.name} is damaged") from exc


def names(ids: list[str], home: dict[str, dict]) -> str:
    """'Living stand', 'Living stand and Airpuri', 'A, B and C'."""
    found = [home[i]["name"] if i in home else i for i in ids]
    if len(found) < 2:
        return "".join(found)
    return ", ".join(found[:-1]) + " and " + found[-1]


def refusal(agent: dict, device: dict, granted: list[str], home: dict[str, dict]) -> str | None:
    """Why this agent may not control this device, or None when the owner gave it this device."""
    if device["id"] in granted:
        return None
    if not granted:
        return f"Permission: the owner has given {agent['name']} no device to control in this home."
    return f"Permission: {agent['name']} may control only {names(granted, home)} in this home, as the owner chose."


def summary(granted: list[str] | None, home: dict[str, dict] | None = None) -> str:
    """For the pages: 'any device its rights allow', 'only Living stand', 'no device'."""
    if granted is None:
        return "any device its rights allow"
    return f"only {names(granted, home or {})}" if granted else "no device"
