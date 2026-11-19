"""Porch's connections in Claude Desktop: one for each of your agents that lives there.

    deploy/home/agent.sh desktop          make them match the agents whose app is Claude Desktop
                                          (agent.sh set <id> --client "Claude Desktop"), and offer to
                                          remove those whose agent now lives in another app
    deploy/home/agent.sh desktop --check  only say what would change

Claude Desktop reads its settings (claude_desktop_config.json) when it starts and writes its own
copy back while it runs, so a change made while it is open is lost. `desktop` therefore asks you to
quit it, changes the settings (keeping a copy of the old ones beside them), opens it again and waits
until it has started each new connection. A connection names its agent and holds no key:
agents.py connect reads the key from Porch's settings when Claude Desktop starts it.

Left as they are: connections to another Porch (a stand-in, with its own --url or settings folder),
connections for agents with no app set, and everything that is not Porch's.
"""

import json
import os
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

CLIENT = "Claude Desktop"
APP = "Claude.app/Contents/MacOS/Claude"  # the app itself, not its helpers
REPO = Path(__file__).resolve().parents[2]
PYTHON = REPO / "porch_gateway" / ".venv" / "bin" / "python"
BRIDGE = REPO / "deploy" / "home" / "agents.py"
OTHER_PORCH = ("PORCH_GATEWAY_URL", "PORCH_GATEWAY_CONFIG")  # an entry with these speaks to another Porch


def config_path() -> Path:
    return Path(os.environ.get("CLAUDE_DESKTOP_CONFIG") or
                Path.home() / "Library/Application Support/Claude/claude_desktop_config.json")


def log_path() -> Path:
    return Path(os.environ.get("CLAUDE_DESKTOP_LOG") or Path.home() / "Library/Logs/Claude/mcp.log")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")  # Claude Desktop's log is in UTC too


def entry_for(agent_id: str) -> dict:
    return {"command": str(PYTHON), "args": [str(BRIDGE), "connect", agent_id]}


def read_config() -> dict:
    try:
        value = json.loads(config_path().read_text())
    except FileNotFoundError:
        return {}
    return value if isinstance(value, dict) else {}


def servers(config: dict) -> dict:
    value = config.get("mcpServers")
    return value if isinstance(value, dict) else {}


def agent_of(entry: object, settings: Path) -> str | None:
    """The agent a connection speaks for, when it connects to this home's Porch: agents.py connect <id>,
    with the default gateway and this settings folder."""
    if not isinstance(entry, dict):
        return None
    args = entry.get("args")
    if not (isinstance(args, list) and len(args) == 3 and args[1] == "connect"
            and str(args[0]).endswith("deploy/home/agents.py")):
        return None  # not Porch's bridge, or one with its own --url
    env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    if any(env.get(name) for name in OTHER_PORCH):
        return None
    if env.get("PORCH_CONFIG_DIR") and Path(env["PORCH_CONFIG_DIR"]).expanduser().resolve() != settings.resolve():
        return None
    return str(args[2])


def plan(clients: dict[str, str | None], config: dict, settings: Path) -> dict:
    """What to change. clients: every agent in Porch's settings, by id, and the app it lives in.

    add:   {connection name: entry} for each agent in Claude Desktop that has no connection yet
    keep:  {connection name: agent id} for those that have one
    drop:  {connection name: agent id} for this home's connections whose agent now lives in another
           app, or is no longer in Porch's settings (offered, never removed without asking)
    """
    current = servers(config)
    speaks_for = {name: agent_of(entry, settings) for name, entry in current.items()}
    connected = {agent for agent in speaks_for.values() if agent}
    add: dict[str, dict] = {}
    for agent_id, client in sorted(clients.items()):
        if client != CLIENT or agent_id in connected:
            continue
        base = "porch-" + (agent_id.removesuffix("-agent") or agent_id)  # porch-energy for energy-agent
        name, number = base, 2
        while name in current or name in add:
            name, number = f"{base}-{number}", number + 1
        add[name] = entry_for(agent_id)
    keep = {name: agent for name, agent in speaks_for.items() if agent and clients.get(agent) == CLIENT}
    drop = {name: agent for name, agent in speaks_for.items()
            if agent and (agent not in clients or (clients[agent] and clients[agent] != CLIENT))}
    return {"add": add, "keep": keep, "drop": drop}


def running() -> bool:
    """From the whole process list: pgrep leaves out its own ancestors, and a program started from
    inside Claude Desktop has the app among them."""
    listing = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True, check=False).stdout
    return any(line.split(" --")[0].rstrip().endswith(APP) for line in listing.splitlines())


def started(name: str, since: str) -> bool:
    """Whether Claude Desktop has started this connection and had its answer since then (its MCP log)."""
    try:
        lines = log_path().read_text(errors="ignore").splitlines()
    except OSError:
        return False
    return any(f"[{name}]" in line and line[:19] >= since[:19] and ("tools/list" in line or "result" in line)
               for line in lines)


def write_private(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)
    os.replace(temporary, path)


def change(add: dict, drop: list[str]) -> Path | None:
    """Write the new settings, keeping everything else, and a copy of the old ones beside them.
    Only while the app is closed. Returns the copy, if there were settings to copy."""
    path = config_path()
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.before-agents-{datetime.now():%Y%m%d-%H%M%S}")
        write_private(backup, path.read_text())
    config = read_config()
    entries = config.setdefault("mcpServers", {})
    for name in drop:
        entries.pop(name, None)
    entries.update(add)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_private(path, json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    return backup


def apply(add: dict, drop: list[str], *, ask: Callable[[str], str] = input,
          wait: Callable[[float], None] = time.sleep, say: Callable[[str], None] = print,
          reopen: Callable[[], None] = lambda: subprocess.run(["open", "-a", "Claude"], check=False)) -> bool:
    """Quit (by asking), change, reopen, and wait for the new connections. False if nothing changed
    or a new connection has not started."""
    if running():
        ask("Quit Claude Desktop now (Claude menu > Quit Claude, or Cmd+Q), then press Enter here. ")
        for waited in range(120):
            if not running():
                break
            if waited % 10 == 0:
                say("Waiting for Claude Desktop to quit...")
            wait(1)
        else:
            say("Claude Desktop is still open, so nothing was changed. Quit it, then start this again.")
            return False
    since = now()
    backup = change(add, drop)
    say("Changed Claude Desktop's settings" + (f" (the old ones are kept beside them, in {backup.name})." if backup else "."))
    reopen()
    say("Opened Claude Desktop again. Waiting for it to start the new connections...")
    waiting = set(add)
    for _ in range(90):
        waiting = {name for name in waiting if not started(name, since)}
        if not waiting:
            break
        wait(1)
    for name in add:
        say(f"  {name}: {'not started yet: check that Claude Desktop is open' if name in waiting else 'started'}")
    return not waiting
