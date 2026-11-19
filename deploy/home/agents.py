"""Your own agents in Porch: give one its own key and rights, and let a Claude on this Mac
connect to Porch as that agent.

    deploy/home/agent.sh add dot --name Dot       a new agent with its own key
    deploy/home/agent.sh list                     the agents and what each may ask for
    deploy/home/agent.sh set dot --person yoonchan    whose agent it is in the household
    deploy/home/agent.sh set energy-agent --reads energy --scopes none
                                                  an agent that reads energy readings and nothing else
    deploy/home/agent.sh connect dot              an MCP server on stdio that speaks to Porch as Dot
    deploy/home/agent.sh desktop                  a Claude Desktop connection for each agent whose app is
                                                  Claude Desktop (asks you to quit and reopen the app)
    deploy/home/agent.sh devices muse home-1/living-stand
                                                  Muse may control only Living stand (--any: any device its
                                                  rights allow again; --none: no device)
    deploy/home/agent.sh online-key dot           Dot's connector address, with its online key in it, for
                                                  ChatGPT or claude.ai (copied, never shown)
    deploy/home/agent.sh online-off dot           take that key away
    deploy/home/agent.sh remove dot               take its key away

An agent reaches every device in the home through Porch alone: it never signs in to LG, Google
Home, Ring or the Matter Server. What it may do is decided by the gateway: its scopes, the
household rules, your approval for what the rules hold, and family protection in the hub.

The key is made here and kept in the gateway's settings file, which only you can read. It is
never printed. A Claude app starts `connect` as a local MCP server, so the key is not typed
into the app or stored in its settings: the app's settings name the agent, and this program
reads the key when it starts.

`add` and `remove` change the settings file; the gateway reads it when it starts, so restart
Porch (deploy/home/start.sh) afterwards.

`connect` works whether or not Porch is running when the app starts it. It remembers the tools
Porch offered last time and lists those when Porch is down, so the app never ends up with an
agent that has no tools; a call made while Porch is down answers that Porch is not running and
that nothing was changed. It also tells the agent, in the connection's own instructions, who it
is in this home and how to use Porch.
"""

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

GATEWAY_URL = "http://127.0.0.1:8000/mcp"
ONLINE_KEYS_FILE = "online-keys.json"  # hashes of the keys agents use from Claude's servers (online.py)
ONLINE_STATE_FILE = "online.json"  # whether the door is open, and its address (online.py)
GRANTS_FILE = "agent-devices.json"  # the devices each agent may control (porch_gateway/device_grants.py)
HUB_URL = "http://127.0.0.1:8100"
READS = ("all", "energy")  # what an agent may read: everything, or energy readings only
TOOLS_FILE = "agent-tools.json"  # the tools Porch offered last time, for when it is down
NOT_RUNNING = ("Porch is not running on this computer, so nothing was read and nothing was changed. "
               "Tell the owner to start Porch, then try again.")
# The gateway's capability names (porch_gateway.py, RISK_TIER). An agent with all of them may
# ask for anything; the gateway still holds what its rules reserve for the owner.
ALL_SCOPES = ["lighting", "appliance", "heating", "entry", "water_shutoff", "sensor", "unknown_load", "network"]


def settings_dir() -> Path:
    return Path(os.environ.get("PORCH_CONFIG_DIR") or Path.home() / ".config" / "porch-hub").expanduser()


def config_files() -> list[Path]:
    """The gateway settings this home uses: the one named by PORCH_GATEWAY_CONFIG, else every
    porch_config*.json in the settings folder (a home may keep one for every day and one for a demo)."""
    if named := os.environ.get("PORCH_GATEWAY_CONFIG"):
        return [Path(named).expanduser()]
    return sorted(settings_dir().glob("porch_config*.json"))


def load(path: Path) -> dict:
    info = path.stat()
    if info.st_mode & 0o077:
        raise SystemExit(f"Stopped: {path.name} can be read by others. Make it private first: chmod 600 {path}")
    return json.loads(path.read_text())


def save_text(path: Path, text: str) -> None:
    """Replace path with text in one step, readable by the owner only."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)
    os.replace(temporary, path)


def save(path: Path, config: dict) -> None:
    save_text(path, json.dumps(config, indent=2, ensure_ascii=False) + "\n")


def find(config: dict, agent_id: str) -> tuple[str, dict] | None:
    """(key, agent) for an agent id. The gateway keeps agents by key."""
    for key, agent in config.get("agents", {}).items():
        if agent.get("id") == agent_id:
            return key, agent
    return None


def parse_scopes(raw: list[str] | None) -> list[str]:
    scopes = ALL_SCOPES if raw in (None, ["all"]) else [] if raw == ["none"] else raw
    if unknown := [scope for scope in scopes if scope not in ALL_SCOPES]:
        raise SystemExit(f"Stopped: unknown scope {', '.join(unknown)}. The gateway knows: {', '.join(ALL_SCOPES)}, "
                         "or all, or none")
    return list(scopes)


def label_for(scopes: list[str], reads: str) -> str:
    if reads == "energy":
        return "Energy readings only"
    return "All devices, with approvals" if scopes == ALL_SCOPES else ", ".join(scopes) or "Reads only"


def add(args: argparse.Namespace) -> int:
    files = config_files()
    if not files:
        raise SystemExit(f"Stopped: no gateway settings in {settings_dir()}. Run deploy/home/setup_mac.sh first.")
    agent_id = args.id.strip().lower()
    if not agent_id or not agent_id.replace("-", "").replace("_", "").isalnum():
        raise SystemExit("Stopped: an agent id is letters, digits, - and _, for example dot")
    scopes = parse_scopes(args.scopes)
    reads = args.reads or "all"
    configs = [(path, load(path)) for path in files]
    if any(find(config, agent_id) for _, config in configs):
        raise SystemExit(f"Stopped: an agent with the id {agent_id} already exists. Nothing was changed.")
    key = secrets.token_urlsafe(32)  # one key, the same in every settings file this home uses
    agent = {"id": agent_id, "name": args.name or agent_id.capitalize(), "acts_for": args.acts_for,
             "scope_label": args.label or label_for(scopes, reads), "scopes": scopes}
    if args.person:
        agent["person"] = args.person
    if reads != "all":
        agent["reads"] = reads
    if args.client:
        agent["client"] = args.client
    for path, config in configs:
        config.setdefault("agents", {})[key] = dict(agent)
        save(path, config)
    print(f"Added {agent['name']} ({agent_id}) to {', '.join(path.name for path, _ in configs)}: {agent['scope_label']}.")
    print("Its key is kept in those files and was not printed. Restart Porch for the gateway to know it.")
    return 0


def change(args: argparse.Namespace) -> int:
    """Change an agent's household person, what it reads, its scopes or its label, in every file."""
    if args.reads is not None and args.reads not in READS:
        raise SystemExit(f"Stopped: --reads is one of {', '.join(READS)}")
    changed = []
    for path in config_files():
        config = load(path)
        if (found := find(config, args.id)) is None:
            continue
        agent = found[1]
        if args.person is not None:
            if args.person in ("", "none"):
                agent.pop("person", None)
            else:
                agent["person"] = args.person
        if args.acts_for is not None:
            agent["acts_for"] = args.acts_for
        if args.client is not None:
            if args.client in ("", "none"):
                agent.pop("client", None)
            else:
                agent["client"] = args.client
        if args.name is not None:
            agent["name"] = args.name
        if args.reads is not None:
            if args.reads == "all":
                agent.pop("reads", None)
            else:
                agent["reads"] = args.reads
        if args.scopes is not None:
            agent["scopes"] = parse_scopes(args.scopes)
        if args.label is not None or args.reads is not None or args.scopes is not None:
            agent["scope_label"] = args.label or label_for(agent.get("scopes", []), agent.get("reads", "all"))
        save(path, config)
        changed.append(path.name)
    if not changed:
        raise SystemExit(f"Stopped: no agent with the id {args.id}.")
    print(f"Changed {args.id} in {', '.join(changed)}. Restart Porch for the gateway to use it.")
    return 0


def remove(args: argparse.Namespace) -> int:
    removed = []
    for path in config_files():
        config = load(path)
        if (found := find(config, args.id)) is not None:
            del config["agents"][found[0]]
            save(path, config)
            removed.append(path.name)
    if not removed:
        raise SystemExit(f"Stopped: no agent with the id {args.id}.")
    drop_online_keys(args.id)
    print(f"Removed {args.id} from {', '.join(removed)}. Restart Porch for its key to stop working.")
    return 0


def grants() -> dict:
    """{"agents": {agent id: {"devices": [...], ...}}}: the devices the owner limited each agent to."""
    path = settings_dir() / GRANTS_FILE
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return {"agents": {}}
    except (OSError, ValueError):
        raise SystemExit(f"Stopped: {path} is damaged, and Porch controls nothing until it is fixed. Look at it, "
                         "or remove it to lift every agent's device limit.") from None
    agents = value.get("agents") if isinstance(value, dict) else None
    if not isinstance(agents, dict) or not all(isinstance(e, dict) and isinstance(e.get("devices"), list)
                                               for e in agents.values()):
        raise SystemExit(f"Stopped: {path} is damaged, and Porch controls nothing until it is fixed.")
    return {"agents": agents}


def hub_devices() -> dict[str, str] | None:
    """The home's devices by id -> name, from the hub on this Mac; None when it is not running."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"{os.environ.get('PORCH_HUB_URL', HUB_URL)}/devices", timeout=5) as answer:
            return {d["id"]: d.get("name", d["id"]) for d in json.loads(answer.read())}
    except Exception:  # not running, or not answering: the ids are then taken as written
        return None


def devices(args: argparse.Namespace) -> int:
    """Which devices one agent may control. Porch's rules, the household's and family protection still apply."""
    if not any(find(load(path), args.id) for path in config_files()):
        raise SystemExit(f"Stopped: no agent with the id {args.id}.")
    chosen = [d.strip() for d in args.device_ids if d.strip()]
    if sum(bool(x) for x in (chosen, args.any, args.none)) != 1:
        raise SystemExit("Stopped: name the devices it may control, or --any, or --none. Nothing was changed.")
    known = hub_devices()
    if known is not None and (unknown := [d for d in chosen if d not in known]):
        raise SystemExit(f"Stopped: this home has no device {', '.join(unknown)}. The ids: {', '.join(sorted(known))}")
    data = grants()
    if args.any:
        data["agents"].pop(args.id, None)
    else:
        data["agents"][args.id] = {"devices": list(dict.fromkeys(chosen)), "by": "agent.sh on the hub's computer",
                                   "changed": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds")}
    settings_dir().mkdir(parents=True, exist_ok=True)
    save(settings_dir() / GRANTS_FILE, data)
    if args.any:
        print(f"{args.id} may control any device its rights allow again.")
    elif args.none:
        print(f"{args.id} may control no device now. It still reads the home.")
    else:
        names = [known.get(d, d) if known else d for d in chosen]
        print(f"{args.id} may control only {', '.join(names)} now.")
    print("Porch uses this from the agent's next request; no restart. Family protection still applies.")
    if known is None:
        print("(The hub was not running, so the device ids were not checked.)")
    return 0


def show(_args: argparse.Namespace) -> int:
    online = {entry.get("agent") for entry in online_keys()["keys"].values()}
    limited = grants()["agents"]
    for path in config_files():
        print(f"{path.name}:")
        for agent in load(path).get("agents", {}).values():
            who = agent.get("acts_for", "-") + (f" ({agent['person']})" if agent.get("person") else "")
            reads = " · reads energy only" if agent.get("reads") == "energy" else ""
            print(f"  {agent.get('id', '?'):16} {agent.get('name', ''):16} acts for {who:22} "
                  f"may ask for: {', '.join(agent.get('scopes', [])) or 'nothing'}{reads}"
                  f"{' · in ' + agent['client'] if agent.get('client') else ''}"
                  f"{' · online key' if agent.get('id') in online else ''}"
                  f"{' · controls only ' + (', '.join(limited[agent['id']]['devices']) or 'nothing') if agent.get('id') in limited else ''}")
    return 0


# ---------- online keys: for an agent that works from ChatGPT, claude.ai or the phone (online.py) ----------

def online_keys() -> dict:
    path = settings_dir() / ONLINE_KEYS_FILE
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"keys": {}}
    return value if isinstance(value, dict) and isinstance(value.get("keys"), dict) else {"keys": {}}


def drop_online_keys(agent_id: str) -> bool:
    keys = online_keys()
    kept = {h: e for h, e in keys["keys"].items() if e.get("agent") != agent_id}
    if len(kept) == len(keys["keys"]):
        return False
    settings_dir().mkdir(parents=True, exist_ok=True)
    save(settings_dir() / ONLINE_KEYS_FILE, {"keys": kept})
    return True


def copy_to_clipboard(text: str) -> None:
    """macOS's clipboard. Replaced in tests."""
    import subprocess

    subprocess.run(["pbcopy"], input=text, text=True, check=True)


def door_address() -> str | None:
    """https://<this Mac>.<tailnet>.ts.net, as online.sh on found it, or None before it ever ran."""
    try:
        address = json.loads((settings_dir() / ONLINE_STATE_FILE).read_text()).get("address")
    except (OSError, ValueError, AttributeError):
        return None
    return address.removesuffix("/mcp").rstrip("/") if isinstance(address, str) and address.startswith("https://") else None


def online_key(args: argparse.Namespace) -> int:
    """A new online key for one agent, on the clipboard and nowhere on screen. The one before it stops working.
    By default the clipboard gets the agent's whole connector address, with the key in it."""
    import hashlib

    if not any(find(load(path), args.id) for path in config_files()):
        raise SystemExit(f"Stopped: no agent with the id {args.id}.")
    address = None if args.header else door_address()
    if not args.header and address is None:
        raise SystemExit("Stopped: Porch's online address is not known yet. Open the door first "
                         "(deploy/home/online.sh on), then run this again: the key goes into that address. "
                         "Nothing was changed.")
    key = "porch-online-" + secrets.token_urlsafe(32)
    try:
        copy_to_clipboard(key if args.header else f"{address}/k/{key}/mcp")
    except Exception as exc:  # no clipboard: nothing is kept, so no key exists that nobody can see
        raise SystemExit(f"Stopped: could not copy the key to the clipboard ({type(exc).__name__}). Nothing was changed.")
    keys = online_keys()
    kept = {h: e for h, e in keys["keys"].items() if e.get("agent") != args.id}
    kept[hashlib.sha256(key.encode()).hexdigest()] = {"agent": args.id,
                                                      "created": __import__("datetime").datetime.now().isoformat(timespec="seconds")}
    settings_dir().mkdir(parents=True, exist_ok=True)
    save(settings_dir() / ONLINE_KEYS_FILE, {"keys": kept})
    if args.header:
        print(f"A new online key for {args.id} is on your clipboard. It is not shown and not kept anywhere readable: "
              "paste it now as the request header Authorization: Bearer <key>.")
    else:
        print(f"{args.id}'s connector address is on your clipboard. It holds a new online key, so it is not shown "
              "and not kept anywhere readable: paste it now as the connector's address (URL), and choose "
              "No sign-in (claude.ai) or No authentication (ChatGPT).")
        print(f"Keep it private: whoever has it can ask Porch for what {args.id} may ask for, under the same rules. "
              f"agent.sh online-off {args.id} takes it back.")
    print(f"An earlier online key for {args.id}, if there was one, no longer works.")
    return 0


def online_off(args: argparse.Namespace) -> int:
    if drop_online_keys(args.id):
        print(f"{args.id}'s online key no longer works. On this Mac the agent works as before.")
    else:
        print(f"{args.id} has no online key.")
    return 0


def instructions(agent: dict) -> str:
    """What the connection tells the agent about itself: who it is here and how to use Porch."""
    name, owner = agent.get("name", "this agent"), agent.get("acts_for", "the owner")
    return (
        f"You are connected to Porch, the gateway to {owner}'s home, as the agent {name}, acting for {owner}. "
        "Porch reaches every device in the home for you, whichever platform it is on; you never sign in to "
        "a device platform yourself. "
        "describe_home lists every device with its state and what you may do with it. get_state reads one "
        "device. preview_action says what an action would do without touching the device. request_action "
        "(device_id, command, reason) asks Porch to act; Porch may allow it, hold it for the owner's "
        "approval or refuse it. "
        "Say a device changed only when request_action answered that it was executed and verified. If Porch "
        "refuses or holds an action, tell the user the reason Porch gave, and do not try another way to "
        "reach the device. "
        f"Use only this connection's tools: another Porch connection in the same chat is another agent, with "
        f"other rights, and you are {name}."
        + (" You read energy only: when asked to switch something, ask Porch with request_action anyway and tell "
           "the user Porch's answer, which is that you have no authority to control devices."
           if agent.get("reads") == "energy" else ""))


def connect(args: argparse.Namespace) -> int:
    """Serve MCP on stdio, passing every call on to the gateway with this agent's key."""
    found = None
    for path in config_files():
        if (found := find(load(path), args.id)) is not None:
            break
    if found is None:
        raise SystemExit(f"Stopped: no agent with the id {args.id}. Add it first: deploy/home/agent.sh add {args.id}")
    key, agent = found
    # Imported here: the other commands work without the gateway's packages.
    import anyio
    import mcp.types as types
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    remembered = settings_dir() / TOOLS_FILE

    def porch() -> "Client":
        return Client(StreamableHttpTransport(args.url, headers={"Authorization": f"Bearer {key}"}))

    async def list_tools(_context, _params) -> "types.ListToolsResult":
        try:
            async with porch() as client:
                tools = (await client.list_tools_mcp()).tools
        except Exception as exc:  # Porch is down: the tools it offered last time, so the app keeps them
            print(f"Porch did not answer ({type(exc).__name__}); listing the tools it offered last time", file=sys.stderr)
            try:
                tools = [types.Tool.model_validate(tool) for tool in json.loads(remembered.read_text())]
            except (OSError, ValueError):
                tools = []
        else:
            try:
                save_text(remembered, json.dumps([tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                                                  for tool in tools]))
            except OSError:
                pass  # remembering is a convenience; the listing itself worked
        return types.ListToolsResult(tools=tools)

    async def call_tool(_context, params) -> "types.CallToolResult":
        try:
            async with porch() as client:
                return await client.call_tool_mcp(params.name, params.arguments or {})
        except Exception as exc:
            print(f"{params.name} did not reach Porch ({type(exc).__name__})", file=sys.stderr)
            return types.CallToolResult(content=[types.TextContent(type="text", text=NOT_RUNNING)], is_error=True)

    async def serve() -> None:
        server = Server(f"Porch as {agent.get('name', args.id)}", instructions=instructions(agent),
                        on_list_tools=list_tools, on_call_tool=call_tool)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(serve)
    return 0


def desktop(args: argparse.Namespace) -> int:
    """Claude Desktop's Porch connections, made to match the agents whose app is Claude Desktop."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import claude_desktop

    agents: dict[str, dict] = {}
    for path in config_files():  # sorted: porch_config.json comes last, so its word counts
        for agent in load(path).get("agents", {}).values():
            if isinstance(agent, dict) and agent.get("id"):
                agents[agent["id"]] = {"name": agent.get("name") or agent["id"], "client": agent.get("client")}
    if not agents:
        raise SystemExit(f"Stopped: no agents in {settings_dir()}.")
    clients = {agent_id: agent["client"] for agent_id, agent in agents.items()}
    todo = claude_desktop.plan(clients, claude_desktop.read_config(), settings_dir())
    name_of = lambda agent_id: agents.get(agent_id, {}).get("name", agent_id)  # noqa: E731
    print(f"Your agents in {claude_desktop.CLIENT}, and their Porch connections there:")
    rows = [(agent_id, name, "already there") for name, agent_id in todo["keep"].items()]
    rows += [(entry["args"][2], name, "to add") for name, entry in todo["add"].items()]
    for agent_id, name, state in sorted(rows):
        print(f"  {name_of(agent_id):16} {name:18} {state}")
    if not rows:
        print(f'  none: give an agent the app with  agent.sh set <id> --client "{claude_desktop.CLIENT}"')
    if todo["drop"]:
        print("Connections whose agent lives somewhere else now:")
        for name, agent_id in todo["drop"].items():
            where = agents[agent_id]["client"] if agent_id in agents else None
            print(f"  {name:18} {name_of(agent_id)} " + (f"lives in {where}" if where else "is no longer in Porch"))
    if args.check:
        return 0
    drop = []
    if todo["drop"]:
        names = " and ".join(todo["drop"])
        answer = input(f"\nRemove {names} from Claude Desktop? Their agents keep their keys and work in their own "
                       "apps as before. [y/N] ")
        drop = list(todo["drop"]) if answer.strip().lower() in ("y", "yes") else []
    if not todo["add"] and not drop:
        print("\nNothing to change in Claude Desktop.")
        return 0
    print("\nClaude Desktop reads its settings only when it starts, so it has to be closed for this. Its chats,")
    print("and Claude Code sessions in its Code tab, are kept: you can go back to them when it opens again.")
    if not claude_desktop.apply(todo["add"], drop):
        return 1
    print("\nIn Claude Desktop, start a new chat for each agent, open the tools menu under the message box and")
    print("switch on that agent's connection only: Porch then knows the chat as that agent.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deploy/home/agent.sh", description=__doc__.split("\n\n")[0])
    actions = parser.add_subparsers(dest="action", required=True)
    adding = actions.add_parser("add", help="a new agent with its own key")
    adding.add_argument("id", help="a short id, for example dot")
    adding.add_argument("--name", help="the name people see (default: the id, capitalised)")
    adding.add_argument("--acts-for", default="the owner", help="whose agent it is")
    adding.add_argument("--label", help="a few words for its rights, shown on the stage page")
    adding.add_argument("--scopes", nargs="+", metavar="SCOPE",
                        help=f"what it may ask for (default all): {', '.join(ALL_SCOPES)}, or none")
    adding.add_argument("--person", help="the household person it acts for (deploy/home/household.sh list)")
    adding.add_argument("--reads", choices=READS, help="all (default), or energy: energy readings only")
    adding.add_argument("--client", help="the app the agent lives in, for the pages: Claude Desktop, ChatGPT, ...")
    adding.set_defaults(run=add)
    setting = actions.add_parser("set", help="change an agent's person, name, reads, scopes or label")
    setting.add_argument("id")
    setting.add_argument("--person", help="the household person it acts for; none to clear")
    setting.add_argument("--acts-for", help="the name shown for whose agent it is")
    setting.add_argument("--name", help="the name people see")
    setting.add_argument("--reads", help="all, or energy: energy readings only")
    setting.add_argument("--scopes", nargs="+", metavar="SCOPE", help="what it may ask for: all, none, or scopes")
    setting.add_argument("--label", help="a few words for its rights")
    setting.add_argument("--client", help="the app the agent lives in, for the pages; none to clear")
    setting.set_defaults(run=change)
    granting = actions.add_parser("devices", help="the devices an agent may control (the owner's choice)")
    granting.add_argument("id")
    granting.add_argument("device_ids", nargs="*", metavar="DEVICE", help="device ids, for example home-1/living-stand")
    granting.add_argument("--any", action="store_true", help="any device its rights allow again")
    granting.add_argument("--none", action="store_true", help="no device: it only reads")
    granting.set_defaults(run=devices)
    keying = actions.add_parser("online-key", help="the agent's connector address, with a new online key in it, "
                                                   "copied to the clipboard")
    keying.add_argument("id")
    keying.add_argument("--header", action="store_true",
                        help="copy only the key, for an app with a request header field (Authorization: Bearer)")
    keying.set_defaults(run=online_key)
    unkeying = actions.add_parser("online-off", help="take an agent's online key away")
    unkeying.add_argument("id")
    unkeying.set_defaults(run=online_off)
    removing = actions.add_parser("remove", help="take an agent's key away")
    removing.add_argument("id")
    removing.set_defaults(run=remove)
    actions.add_parser("list", help="the agents and what each may ask for").set_defaults(run=show)
    connecting = actions.add_parser("connect", help="an MCP server on stdio that speaks to Porch as this agent")
    connecting.add_argument("id")
    connecting.add_argument("--url", default=os.environ.get("PORCH_GATEWAY_URL", GATEWAY_URL),
                            help=f"the gateway (default {GATEWAY_URL})")
    connecting.set_defaults(run=connect)
    desktopping = actions.add_parser("desktop", help="a Claude Desktop connection for each agent that lives there")
    desktopping.add_argument("--check", action="store_true", help="only say what would change")
    desktopping.set_defaults(run=desktop)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
