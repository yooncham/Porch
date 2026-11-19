"""Porch online: let your own agents reach Porch from apps that run on their makers' servers:
claude.ai and the Claude phone app, and ChatGPT.

    deploy/home/online.sh on        open the door to the internet (Tailscale Funnel) and print its address
    deploy/home/online.sh off       close it again
    deploy/home/online.sh status    whether it is open, and which agents have an online key
    deploy/home/online.sh serve     run the door itself (deploy/home/run_all.sh starts it with Porch)

The door is the only part of Porch the internet reaches. It listens on this Mac only
(127.0.0.1:8090), and Tailscale Funnel carries requests from Claude's servers to it. It passes on
requests to the agents' tools and nothing else: not the hub, its pages, the owner's commands or
the stage feed. Each request must carry an agent's online key; the door swaps it for that agent's
own Porch key, so the gateway applies exactly the rules it applies on this Mac.

The key travels in one of two ways:
    https://<this Mac>.<tailnet>.ts.net/k/<online key>/mcp   in the address itself, for connector
        forms with no field for a key: ChatGPT's, and claude.ai's "No sign-in". An unknown key
        there gets 404, as any other address does.
    https://<this Mac>.<tailnet>.ts.net/mcp with the header Authorization: Bearer <online key>,
        for apps that let you add a request header.

Online keys are made with `deploy/home/agent.sh online-key <agent>`, kept here only as hashes,
and work only while online access is on. Every request is logged, without keys, addresses or
bodies, to ~/.config/porch-hub/logs/online-door.log.
"""

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DOOR_HOST = "127.0.0.1"
DOOR_PORT = int(os.environ.get("PORCH_ONLINE_PORT", "8090"))
GATEWAY_MCP = os.environ.get("PORCH_GATEWAY_MCP", "http://127.0.0.1:8000/mcp")
STATE_FILE = "online.json"
KEYS_FILE = "online-keys.json"
LOG_FILE = "logs/online-door.log"
MAX_BODY = 1_000_000  # bytes in one request
RATE = (120, 60.0)  # at most this many requests per agent in this many seconds
# Headers passed to the gateway, and back to Claude. Everything else stays behind.
FORWARD = ("content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id",
           # MCP's 2026-07-28 revision routes each request by these (ChatGPT uses it, and starts with
           # server/discover): without them the gateway answers as to an old client with no session.
           "mcp-method", "mcp-name")
FORWARD_PREFIX = "mcp-param-"  # that revision's per-parameter routing headers
RETURN = ("content-type", "mcp-session-id", "mcp-protocol-version", "cache-control")
TAILSCALE_APP_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


def settings_dir() -> Path:
    return Path(os.environ.get("PORCH_CONFIG_DIR") or Path.home() / ".config" / "porch-hub").expanduser()


def gateway_config(settings: Path) -> Path:
    named = os.environ.get("PORCH_GATEWAY_CONFIG")
    return Path(named).expanduser() if named else settings / "porch_config.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_private(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def online_state(settings: Path) -> dict:
    """Whether online access is on. A missing or damaged file means off."""
    state = read_json(settings / STATE_FILE, {})
    return state if isinstance(state, dict) and state.get("enabled") is True else {"enabled": False}


def online_agents(settings: Path) -> dict[str, str]:
    """Key hash -> agent id, for every online key."""
    keys = read_json(settings / KEYS_FILE, {}).get("keys", {})
    return {h: entry["agent"] for h, entry in keys.items() if isinstance(entry, dict) and entry.get("agent")}


def porch_key(config_path: Path, agent_id: str) -> str | None:
    """The agent's own key for the gateway, or None if the agent no longer exists."""
    agents = read_json(config_path, {}).get("agents", {})
    return next((key for key, agent in agents.items() if agent.get("id") == agent_id), None)


def rpc_summary(body: bytes) -> str:
    """What a request asks for, for the log: "tools/call request_action home-1/living-stand On"."""
    try:
        message = json.loads(body) if body else None
    except ValueError:
        return "unreadable body"
    parts = []
    for item in message if isinstance(message, list) else [message]:
        if not isinstance(item, dict):
            continue
        method = str(item.get("method") or ("response" if "result" in item or "error" in item else "?"))
        if method == "tools/call":
            params = item.get("params") or {}
            arguments = params.get("arguments") or {}
            method += " " + " ".join(str(x) for x in (params.get("name"), arguments.get("device_id"),
                                                      arguments.get("command")) if x)
        parts.append(method[:120])
    return ", ".join(parts) or "-"


# ---------- the door ----------

def make_app(settings: Path, *, gateway_mcp: str = GATEWAY_MCP, config_path: Path | None = None,
             transport: Any = None, clock=time.monotonic, echo=print):
    """The door as an ASGI app. transport replaces the network in tests."""
    import httpx
    from starlette.applications import Starlette
    from starlette.background import BackgroundTask
    from starlette.requests import Request
    from starlette.responses import JSONResponse, StreamingResponse
    from starlette.routing import Route

    config_path = config_path or gateway_config(settings)
    recent: defaultdict[str, deque] = defaultdict(deque)
    client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(10.0, read=None))

    def log(entry: dict) -> None:
        line = {"time": now_iso(), **entry}
        path = settings / LOG_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "a") as stream:
                stream.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError:
            pass
        echo(f"[PORCH ONLINE] {entry.get('agent') or '-':10} {entry.get('status')} {entry.get('asks', '')}"
             f"{'  ' + entry['why'] if entry.get('why') else ''}", flush=True)

    def refuse(status: int, why: str, agent: str | None = None, asks: str = "") -> JSONResponse:
        log({"agent": agent, "status": status, "asks": asks, "why": why})
        return JSONResponse({"error": why}, status_code=status)

    async def mcp(request: Request):
        """The key in a header: Authorization: Bearer <key>, or X-API-Key."""
        auth = request.headers.get("authorization", "")
        presented = auth[7:].strip() if auth[:7].lower() == "bearer " else request.headers.get("x-api-key", "").strip()
        return await forward(request, presented, in_address=False)

    async def mcp_in_address(request: Request):
        """The key in the address, /k/<key>/mcp, for connector forms with no field for a key."""
        return await forward(request, request.path_params["key"], in_address=True)

    async def forward(request: Request, presented: str, *, in_address: bool):
        if not online_state(settings)["enabled"]:
            return refuse(503, "Porch online access is off. The owner turns it on at home.")
        agent_id = online_agents(settings).get(key_hash(presented)) if presented else None
        if agent_id is None and in_address:
            # The same answer as any address the door does not have: a 401 would make a connector
            # start a sign-in, and this is no sign-in.
            log({"agent": None, "status": 404, "asks": "", "why": "An address with no online key in it."})
            return JSONResponse({"error": "Not found."}, status_code=404)
        if not presented:
            return refuse(401, "An agent key is required.")
        if agent_id is None:
            return refuse(401, "This key is not an online key for any agent.")
        key = porch_key(config_path, agent_id)
        if key is None:
            return refuse(403, f"The agent {agent_id} no longer exists in this home.", agent_id)
        window, start = recent[agent_id], clock()
        while window and window[0] <= start - RATE[1]:
            window.popleft()
        if len(window) >= RATE[0]:
            return refuse(429, f"Too many requests: at most {RATE[0]} a minute.", agent_id)
        window.append(start)
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return refuse(413, "The request is too large.", agent_id)
        body = await request.body()
        if len(body) > MAX_BODY:
            return refuse(413, "The request is too large.", agent_id)
        asks = rpc_summary(body) if request.method == "POST" else request.method
        # As pairs, so a header sent twice reaches the gateway twice, and its own checks refuse it.
        headers = [(name, value) for name, value in request.headers.items()
                   if name in FORWARD or name.startswith(FORWARD_PREFIX)]
        headers += [("authorization", f"Bearer {key}"), ("accept-encoding", "identity")]
        try:
            upstream = await client.send(client.build_request(request.method, gateway_mcp, headers=headers,
                                                              content=body), stream=True)
        except httpx.HTTPError as exc:
            return refuse(502, f"Porch's gateway did not answer ({type(exc).__name__}). Is Porch running?",
                          agent_id, asks)
        log({"agent": agent_id, "status": upstream.status_code, "asks": asks,
             "ms": round((clock() - start) * 1000)})
        back = {name: upstream.headers[name] for name in RETURN if name in upstream.headers}
        return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code, headers=back,
                                 background=BackgroundTask(upstream.aclose))

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield
        await client.aclose()

    return Starlette(routes=[Route("/mcp", mcp, methods=["GET", "POST", "DELETE"]),
                             Route("/k/{key}/mcp", mcp_in_address, methods=["GET", "POST", "DELETE"])],
                     lifespan=lifespan)


def serve(_args: argparse.Namespace) -> int:
    import uvicorn

    settings = settings_dir()
    print(f"Porch online door on http://{DOOR_HOST}:{DOOR_PORT}/mcp (open to the internet only while "
          f"online access is on: {'on' if online_state(settings)['enabled'] else 'off'})", flush=True)
    uvicorn.run(make_app(settings), host=DOOR_HOST, port=DOOR_PORT, log_level="warning")
    return 0


# ---------- the switch ----------

def tailscale() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    return TAILSCALE_APP_CLI if Path(TAILSCALE_APP_CLI).exists() else None


def door_listening() -> bool:
    import socket

    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex((DOOR_HOST, DOOR_PORT)) == 0


def public_address(cli: str) -> str | None:
    """https://<this Mac>.<tailnet>.ts.net/mcp, from Tailscale's own status. An agent's own address,
    with its key in it, is made by agent.sh online-key."""
    result = subprocess.run([cli, "status", "--json"], capture_output=True, text=True, timeout=20)
    try:
        name = json.loads(result.stdout)["Self"]["DNSName"].rstrip(".")
    except (ValueError, KeyError, TypeError):
        return None
    return f"https://{name}/mcp" if name else None


def switch_on(_args: argparse.Namespace) -> int:
    settings = settings_dir()
    cli = tailscale()
    if cli is None:
        print("Stopped: Tailscale is not installed. See docs/REAL_HOME.md, \"Your agents in ChatGPT and on claude.ai\".",
              file=sys.stderr)
        return 1
    if not door_listening():
        print(f"Stopped: the door is not running on port {DOOR_PORT}. Start Porch with deploy/home/run_all.sh "
              "(or Start Porch.command) first.", file=sys.stderr)
        return 1
    started = subprocess.run([cli, "funnel", "--bg", str(DOOR_PORT)], text=True, timeout=120)
    if started.returncode != 0:
        print("Stopped: Tailscale did not open the door. If it printed a link, open it to allow Funnel, "
              "then run this again.", file=sys.stderr)
        return 1
    address = public_address(cli)
    write_private(settings / STATE_FILE, {"enabled": True, "since": now_iso(), "address": address})
    print(f"\nOnline access is ON. The door's address: {address or '(run status to see it)'}")
    holders = sorted(set(online_agents(settings).values()))
    print(f"Agents with an online key: {', '.join(holders) or 'none yet'}. Each agent's connector address, with "
          "its key in it, goes to the clipboard with: deploy/home/agent.sh online-key <agent>")
    print("Turn it off with: deploy/home/online.sh off")
    return 0


def switch_off(_args: argparse.Namespace) -> int:
    settings = settings_dir()
    # The door refuses everything from this moment, whatever happens to the tunnel below.
    write_private(settings / STATE_FILE, {"enabled": False, "since": now_iso()})
    print("Online access is OFF: the door refuses every request.")
    cli = tailscale()
    if cli is not None:
        closed = subprocess.run([cli, "funnel", "--bg", str(DOOR_PORT), "off"], capture_output=True, text=True,
                                timeout=60)
        if closed.returncode != 0:
            closed = subprocess.run([cli, "funnel", "reset"], capture_output=True, text=True, timeout=60)
        print("The Tailscale address is closed." if closed.returncode == 0 else
              "Tailscale did not confirm closing the address; the door refuses everything anyway.")
    return 0


def status(_args: argparse.Namespace) -> int:
    settings = settings_dir()
    state = online_state(settings)
    if state["enabled"]:
        print(f"Online access is ON since {state.get('since', '?')}. Address: {state.get('address') or '?'}")
    else:
        print("Online access is OFF.")
    holders = sorted(set(online_agents(settings).values()))
    print(f"Agents with an online key: {', '.join(holders) or 'none'}")
    print(f"The door on this Mac: {'running' if door_listening() else 'not running'} (port {DOOR_PORT})")
    cli = tailscale()
    print(f"Tailscale: {cli or 'not installed'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deploy/home/online.sh", description=__doc__.split("\n\n")[0])
    actions = parser.add_subparsers(dest="action", required=True)
    for name, run, text in (("on", switch_on, "open the door to the internet"),
                            ("off", switch_off, "close it"),
                            ("status", status, "say whether it is open"),
                            ("serve", serve, "run the door (Porch's start script does this)")):
        actions.add_parser(name, help=text).set_defaults(run=run)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
