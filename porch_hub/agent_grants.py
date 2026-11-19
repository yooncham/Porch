"""The owner's choice of which devices each agent may control (agent-devices.json).

The gateway reads this file on every request (porch_gateway/device_grants.py) and refuses an agent
any device the owner did not give it. No entry for an agent: it may control any device its rights
allow, as before. The hub writes it only for the owner, with the owner PIN, from the How Porch works
page; `deploy/home/agent.sh devices` writes it from the hub's computer.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from .private_files import private_write

FILE = "agent-devices.json"


class Damaged(Exception):
    """The file is there but is not what Porch wrote. It is not overwritten: the owner looks first."""


def read(path: Path) -> dict[str, dict]:
    """agent id -> {"devices": [...], "changed": ..., "by": ...}. No file: nobody is limited."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise Damaged(f"{path.name} could not be read") from exc
    agents = data.get("agents") if isinstance(data, dict) else None
    if not isinstance(agents, dict) or not all(
            isinstance(entry, dict) and isinstance(entry.get("devices"), list)
            and all(isinstance(d, str) for d in entry["devices"]) for entry in agents.values()):
        raise Damaged(f"{path.name} is damaged")
    return agents


def write(path: Path, agent_id: str, devices: list[str] | None, by: str) -> dict[str, dict]:
    """Limit one agent to these devices (an empty list: none), or lift its limit (None). Every other
    agent's entry stays as it was."""
    agents = read(path)
    if devices is None:
        agents.pop(agent_id, None)
    else:
        agents[agent_id] = {"devices": list(dict.fromkeys(devices)),
                            "changed": datetime.now(timezone.utc).isoformat(timespec="seconds"), "by": by}
    private_write(path, json.dumps({"agents": agents}, indent=2, ensure_ascii=False) + "\n")
    return agents
