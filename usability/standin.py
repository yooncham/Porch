"""A stand-in Porch for the cold-start usability test (docs/USABILITY_TEST.md).

The demo home's eight simulated devices run behind the real Device Hub and gateway code, on their
own ports (hub 8102, gateway 8002). The home has a household (Yoonchan, the owner, and Wife) and one
agent made for the test, "Fresh agent", acting for Yoonchan. Nothing real is touched. The settings
and keys are test values made here, kept in ~/.config/porch-standin (private), and nothing the
stand-in writes goes near the real home's settings or audit log.

    porch_gateway/.venv/bin/python usability/standin.py start NAME [--wife-in office]
        Starts it, and keeps it running until Ctrl+C. While it runs, every step the gateway takes
        is recorded in usability/runs/NAME/. On Ctrl+C, the devices' final states and Porch's
        audit log are saved there too. --wife-in puts Wife at home in that room and Yoonchan away.
        --family-protection runs the hub with family protection on, as a real home's is.
    porch_gateway/.venv/bin/python usability/standin.py desktop
        Prints the Claude Desktop entry that connects a chat to the stand-in as Fresh agent.
    porch_gateway/.venv/bin/python usability/standin.py session
        A guided run for a Claude Desktop chat: names the run, asks for the variant, puts the prompt
        on the clipboard, opens the live view, runs the stand-in until Ctrl+C, then keeps the
        agent's answer from the clipboard and checks the run. If Claude Desktop lacks the porch-fresh
        connection, it asks you to quit the app, adds it, and opens the app again first.
        "Start Cold-start Test (HW9).command" next to the repository starts it with a double click.
    porch_gateway/.venv/bin/python usability/standin.py stop
        Stops a stand-in an earlier run left running (its window was closed without Ctrl+C).

It runs with the gateway's Python, which has httpx and websockets.
"""

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, UTC
from pathlib import Path

import httpx
import websockets

REPO = Path(__file__).resolve().parents[1]
SETTINGS = Path(os.environ.get("PORCH_STANDIN_DIR") or Path.home() / ".config" / "porch-standin").expanduser()
RUNS = REPO / "usability" / "runs"
HUB_PORT, GATEWAY_PORT = 8102, 8002
HUB, GATEWAY = f"http://127.0.0.1:{HUB_PORT}", f"http://127.0.0.1:{GATEWAY_PORT}"
AGENT = {"id": "fresh-agent", "name": "Fresh agent", "acts_for": "Yoonchan", "person": "yoonchan",
         "scopes": ["lighting", "appliance", "heating", "entry", "sensor"],
         "scope_label": "Lights, appliances, heating and the front door, within Porch's rules"}
DESKTOP_CONFIG = Path(os.environ.get("CLAUDE_DESKTOP_CONFIG") or
                      Path.home() / "Library/Application Support/Claude/claude_desktop_config.json")
DESKTOP_LOG = Path(os.environ.get("CLAUDE_DESKTOP_LOG") or Path.home() / "Library/Logs/Claude/mcp.log")
DESKTOP_APP = "Claude.app/Contents/MacOS/Claude"  # the app itself, not its helpers
# What the demo's own config says the two plugs power (porch_gateway/porch_config.json).
CAPABILITIES = {"home-1/desk-lamp-plug": "lighting", "home-1/space-heater-plug": "heating"}
FAMILY_PROTECTION = False  # start --family-protection: the hub refuses commands without the owner's consent


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def write_private(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def prepare() -> dict:
    """The stand-in's settings. Its keys and PINs are made once and kept, so a Claude Desktop entry
    keeps working across starts; the household and the devices start fresh every time."""
    SETTINGS.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(SETTINGS, 0o700)
    sys.path.insert(0, str(REPO))
    from porch_hub import household, protection

    pins_file, config_file = SETTINGS / "pins.json", SETTINGS / "porch_config.json"
    pins = json.loads(pins_file.read_text()) if pins_file.exists() else {
        "yoonchan": f"{secrets.randbelow(10**6):06d}", "wife": f"{secrets.randbelow(10**6):06d}"}
    write_private(pins_file, json.dumps(pins))
    for old in ("household.json", "owner-pin.json", "permission.json"):  # agent-tools.json stays: the bridge's memory
        (SETTINGS / old).unlink(missing_ok=True)
    for old in SETTINGS.glob("household-pin-*.json"):
        old.unlink()
    home = household.Household(SETTINGS / "household.json", protection.OwnerPin(SETTINGS / "owner-pin.json"))
    home.add("yoonchan", "Yoonchan", owner=True)
    home.add("wife", "Wife")
    protection.set_pin(SETTINGS / "owner-pin.json", pins["yoonchan"])
    protection.set_pin(home.pin_path("wife"), pins["wife"])

    if config_file.exists():
        config = json.loads(config_file.read_text())
    else:
        config = {"hub_url": HUB, "hardware": "simulated", "owner_key": secrets.token_urlsafe(24),
                  "limits": {"max_delay_start_hours": 12, "approval_ttl_seconds": 600},
                  "device_capabilities": CAPABILITIES, "agents": {secrets.token_urlsafe(24): AGENT}}
    write_private(config_file, json.dumps(config, indent=2))
    return {"pins": pins, "owner_key": config["owner_key"]}


def environment(run: Path) -> dict:
    """Only what the stand-in needs: no live platform's settings reach it, so its devices are the
    demo's simulated ones."""
    return {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "LANG": "en_US.UTF-8",
            "PORCH_CONFIG_DIR": str(SETTINGS), "PORCH_HOUSEHOLD_FILE": str(SETTINGS / "household.json"),
            "PORCH_OWNER_PIN_FILE": str(SETTINGS / "owner-pin.json"),
            "PORCH_PROTECTION_FILE": str(SETTINGS / "permission.json"),
            "PORCH_GATEWAY_BASE_URL": GATEWAY, "PORCH_GATEWAY_CONFIG": str(SETTINGS / "porch_config.json"),
            "PORCH_AUDIT_FILE": str(run / "audit.jsonl"), "PYTHONDONTWRITEBYTECODE": "1",
            "PORCH_FAMILY_PROTECTION": "1" if FAMILY_PROTECTION else "0"}


def taken(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def leftovers() -> list[int]:
    """Stand-in servers an earlier run left running (its window closed without Ctrl+C): only ours,
    told apart by port and by their own command line."""
    found = []
    for port, marker in ((HUB_PORT, f"porch_hub.main:app --port {HUB_PORT}"),
                         (GATEWAY_PORT, f"porch_gateway:app --port {GATEWAY_PORT}")):
        listing = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"], capture_output=True, text=True).stdout
        for pid in (int(line[1:]) for line in listing.splitlines() if line.startswith("p")):
            command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
            if marker in command:
                found.append(pid)
    return found


def stop_leftovers() -> int:
    pids = leftovers()
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGINT)
    for _ in range(40):
        if not (taken(HUB_PORT) or taken(GATEWAY_PORT)):
            break
        time.sleep(0.25)
    for pid in leftovers():
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    return len(pids)


def start_servers(run: Path) -> list[subprocess.Popen]:
    log = open(run / "servers.log", "w")
    env = environment(run)
    # Their own process group: Ctrl+C in the Terminal window reaches this script only, which saves
    # the devices' final states from the hub before it stops the servers itself.
    hub = subprocess.Popen([str(REPO / ".venv/bin/uvicorn"), "porch_hub.main:app", "--port", str(HUB_PORT),
                            "--log-level", "warning"], cwd=REPO, env=env, stdout=log, stderr=log, start_new_session=True)
    gateway = subprocess.Popen([str(REPO / "porch_gateway/.venv/bin/uvicorn"), "porch_gateway:app", "--port",
                                str(GATEWAY_PORT), "--log-level", "warning"], cwd=REPO / "porch_gateway", env=env,
                               stdout=log, stderr=log, start_new_session=True)
    for _ in range(80):
        with contextlib.suppress(httpx.HTTPError):
            if httpx.get(f"{HUB}/devices", timeout=2).status_code == 200 and \
                    httpx.get(f"{GATEWAY}/household/agents", timeout=2).status_code == 200:
                return [hub, gateway]
        time.sleep(0.5)
    for server in (hub, gateway):
        server.kill()
    raise SystemExit(f"The stand-in did not start: see {run / 'servers.log'}")


def snapshot() -> dict:
    devices = httpx.get(f"{HUB}/devices", timeout=10).json()
    people = httpx.get(f"{HUB}/household", timeout=10).json().get("people", [])
    return {"at": now(), "devices": {d["id"]: {"name": d["name"], "room": d["room"], "type": d["device_type"],
                                               "clusters": d["clusters"]} for d in devices},
            "people": [{k: p.get(k) for k in ("id", "name", "where", "room", "room_name")} for p in people]}


async def follow_feed(url: str, out, stop: asyncio.Event) -> None:
    """Every message on a stage feed, one JSON line each, until stopped. The feed is read without a
    timeout: cancelling a receive again and again can drop a message that arrives at that moment."""
    async def listen() -> None:
        while True:
            try:
                async with websockets.connect(url) as feed:
                    async for message in feed:
                        out.write(message.strip() + "\n")
                        out.flush()
            except (OSError, websockets.WebSocketException):
                await asyncio.sleep(0.5)

    task = asyncio.create_task(listen())
    await stop.wait()
    await asyncio.sleep(0.3)  # what the gateway sent just before the stop
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def record(run: Path, stop: asyncio.Event) -> None:
    """Every message on the gateway's stage feed, one JSON line each, until stopped."""
    with (run / "stage.jsonl").open("a") as out:
        await follow_feed(f"ws://127.0.0.1:{GATEWAY_PORT}/ws/stage", out, stop)


def say(text: str = "") -> None:
    """Print, unless the window has gone (closed while the run was saved)."""
    with contextlib.suppress(OSError):
        print(text, flush=True)


def start(args: argparse.Namespace, on_ready=None) -> None:
    global FAMILY_PROTECTION
    FAMILY_PROTECTION = bool(getattr(args, "family_protection", False))
    if (taken(HUB_PORT) or taken(GATEWAY_PORT)) and stop_leftovers():
        say("Stopped a stand-in an earlier run left running.")
    for port in (HUB_PORT, GATEWAY_PORT):
        if taken(port):
            raise SystemExit(f"Port {port} is in use by another program. Stop it, then try again.")
    run = RUNS / args.name
    if run.exists() and any(f.name != "prompt.txt" for f in run.iterdir()):  # the prompt may be made first
        raise SystemExit(f"{run} already holds a run. Pick another name.")
    run.mkdir(parents=True, exist_ok=True)
    secret = prepare()
    servers = start_servers(run)
    try:
        if args.wife_in:
            for person, where, room, pin in (("wife", "home", args.wife_in, secret["pins"]["wife"]),
                                             ("yoonchan", "away", None, secret["pins"]["yoonchan"])):
                answer = httpx.post(f"{HUB}/household/presence", timeout=10,
                                    json={"person": person, "where": where, "room": room, "by": person, "pin": pin})
                answer.raise_for_status()
        (run / "initial_state.json").write_text(json.dumps(snapshot(), indent=2))
        meta = {"name": args.name, "started": now(), "wife_in": args.wife_in, "hub": HUB, "gateway": f"{GATEWAY}/mcp",
                "agent": {k: AGENT[k] for k in ("id", "name", "acts_for", "scopes")},
                "documentation": {"file": "docs/AGENTS.md", "sha256_12": sha(REPO / "docs/AGENTS.md")},
                "task": {"file": "usability/task.md", "sha256_12": sha(REPO / "usability/task.md")}}
        (run / "meta.json").write_text(json.dumps(meta, indent=2))
        say(f"Stand-in Porch is up: hub {HUB}, gateway {GATEWAY}/mcp, recording in {run.relative_to(REPO)}/")
        say(f"Watch it: {HUB}/flow?engine=ws://127.0.0.1:{GATEWAY_PORT}/ws/stage")
        if on_ready:
            on_ready(run)
        say("Press Ctrl+C when the agent has answered.")
        stop = asyncio.Event()

        async def main() -> None:
            loop = asyncio.get_running_loop()
            # Ctrl+C, and closing the window too: either way the run is saved and the servers stop.
            for name in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                loop.add_signal_handler(name, stop.set)
            await record(run, stop)

        asyncio.run(main())
        (run / "final_state.json").write_text(json.dumps(snapshot(), indent=2))
        meta = {**json.loads((run / "meta.json").read_text()), "stopped": now()}
        (run / "meta.json").write_text(json.dumps(meta, indent=2))
        say(f"\nSaved the final states and the audit log in {run.relative_to(REPO)}/")
    finally:
        for server in servers:
            server.send_signal(signal.SIGINT)
        for server in servers:
            try:
                server.wait(10)
            except subprocess.TimeoutExpired:
                server.kill()


def desktop_entry() -> dict:
    """The stand-in's connection for Claude Desktop: Fresh agent, on the stand-in's gateway."""
    return {"porch-fresh": {
        "command": str(REPO / "porch_gateway/.venv/bin/python"),
        "args": [str(REPO / "deploy/home/agents.py"), "connect", AGENT["id"], "--url", f"{GATEWAY}/mcp"],
        "env": {"PORCH_CONFIG_DIR": str(SETTINGS)}}}


def has_desktop_entry(entry: dict | None = None) -> bool:
    entry = entry or desktop_entry()
    try:
        servers = json.loads(DESKTOP_CONFIG.read_text()).get("mcpServers") or {}
    except (OSError, ValueError):
        return False
    return all(servers.get(name) == value for name, value in entry.items())


def add_desktop_entry(entry: dict | None = None) -> None:
    """Add a connection to Claude Desktop's settings, keeping everything else. Only while the app is
    closed: it keeps its settings in memory and writes them back, so a change made while it runs is lost."""
    text = DESKTOP_CONFIG.read_text()
    backup = DESKTOP_CONFIG.with_name(DESKTOP_CONFIG.name + ".before-" + next(iter(entry or desktop_entry())))
    if not backup.exists():
        write_private(backup, text)
    config = json.loads(text)
    config.setdefault("mcpServers", {}).update(entry or desktop_entry())
    write_private(DESKTOP_CONFIG, json.dumps(config, indent=2, ensure_ascii=False) + "\n")


def claude_running() -> bool:
    """From the whole process list: pgrep leaves out its own ancestors, and a script started from
    inside Claude Desktop has the app among them."""
    listing = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout
    return any(line.split(" --")[0].rstrip().endswith(DESKTOP_APP) for line in listing.splitlines())


def started_by_desktop(since: str, name: str = "porch-fresh") -> bool:
    """Whether Claude Desktop has started this connection and read its tools since then (its MCP log)."""
    with contextlib.suppress(OSError):
        for line in DESKTOP_LOG.read_text(errors="ignore").splitlines():
            if f"[{name}]" in line and line[:19] >= since[:19] and ("tools/list" in line or "result" in line):
                return True
    return False


def ensure_desktop_entry(entry: dict | None = None) -> None:
    entry = entry or desktop_entry()
    name = next(iter(entry))
    if has_desktop_entry(entry):
        return
    say(f"Claude Desktop does not have the {name} connection yet. It reads its settings only when it")
    say("starts, and writes its own copy back while it runs, so they can only be changed while it is closed.\n")
    if claude_running():
        input("Quit Claude Desktop now (Claude menu > Quit Claude, or Cmd+Q), then press Enter here. ")
        for waited in range(120):
            if not claude_running():
                break
            if waited % 10 == 0:
                say("Waiting for Claude Desktop to quit...")
            time.sleep(1)
        else:
            raise SystemExit("Claude Desktop is still open. Quit it, then start this again.")
    since = now()
    add_desktop_entry(entry)
    subprocess.run(["open", "-a", "Claude"], check=False)
    say(f"Added {name} to Claude Desktop's settings and opened it again.")
    for _ in range(60):
        if started_by_desktop(since, name):
            say(f"Claude Desktop has started {name}.\n")
            return
        time.sleep(1)
    say(f"Claude Desktop has not started {name} yet. Check that it is open before you start the chat.\n")


def session(args: argparse.Namespace) -> None:
    """One run in a Claude Desktop chat, guided step by step in this Terminal window."""
    if args.clipboard:
        ensure_desktop_entry()
    RUNS.mkdir(parents=True, exist_ok=True)
    number = 1
    while (RUNS / f"run-{number:02d}").exists():
        number += 1
    name = f"run-{number:02d}"
    say(f"The cold-start test, run {name}.\n")
    wife = input("Is Wife at home in the office for this run (variant B)? [y/N] ").strip().lower().startswith("y")
    model = input("Which model will the chat use, as Claude Desktop shows it? ").strip() or "not noted"
    sys.path.insert(0, str(REPO / "usability"))
    import prompt as prompt_file

    text = prompt_file.prompt()
    view = f"{HUB}/flow?engine=ws://127.0.0.1:{GATEWAY_PORT}/ws/stage"

    def ready(run: Path) -> None:
        (run / "prompt.txt").write_text(text)
        meta = json.loads((run / "meta.json").read_text())
        meta["client"] = f"a new Claude Desktop chat ({model}), started by the team, no help during the run"
        (run / "meta.json").write_text(json.dumps(meta, indent=2))
        if args.clipboard:
            subprocess.run(["pbcopy"], input=text, text=True, check=True)
        say(f"""
1. In Claude Desktop, open a new chat, outside any project. If Claude's memory or search of past
   chats is on, use an incognito chat or turn them off for this test: it must not remember Porch.
2. In the chat's tools menu, switch on porch-fresh only. porch-dot, porch-muse and porch-instinct
   reach the real home: switch them off, and every other connector too. If porch-fresh is not in
   the menu, do not send anything: press Ctrl+C here and say so.
3. Paste the prompt ({'it is on the clipboard' if args.clipboard else 'usability/runs/' + run.name + '/prompt.txt'}), send it, and type nothing else.
   If Claude asks to allow porch-fresh's tools, choose Always allow: that is the access, not help.
4. When it has answered, copy its whole answer (the Copy button under it), then come back here
   and press Ctrl+C.

The live view of its requests: {view}
""")
        if args.clipboard:
            subprocess.run(["open", view], check=False)

    start(argparse.Namespace(name=name, wife_in="office" if wife else None), on_ready=ready)
    run = RUNS / name
    answer = run / "final_answer.txt"
    if not answer.exists() and args.clipboard:
        copied = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout.strip()
        if copied and copied != text.strip():
            answer.write_text(copied + "\n")
            say(f"Kept the agent's answer from the clipboard ({len(copied)} characters).")
    if not answer.exists():
        say(f"No answer yet: paste it into usability/runs/{name}/final_answer.txt, then run: python3 usability/check.py {name}")
    subprocess.run([sys.executable, str(REPO / "usability" / "check.py"), name], check=False)
    if args.clipboard and (run / "report.md").exists():
        subprocess.run(["open", str(run / "report.md")], check=False)


def stop(_: argparse.Namespace) -> None:
    count = stop_leftovers()
    say(f"Stopped {count} stand-in server{'s' if count != 1 else ''}." if count else "No stand-in is running.")


def desktop(_: argparse.Namespace) -> None:
    print(json.dumps(desktop_entry(), indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    actions = parser.add_subparsers(dest="action", required=True)
    starting = actions.add_parser("start", help="start the stand-in and record a run")
    starting.add_argument("name", help="the run's folder name under usability/runs/")
    starting.add_argument("--wife-in", metavar="ROOM", help="Wife at home in this room (a room id: office, living, ...)")
    starting.add_argument("--family-protection", action="store_true", help="the hub with family protection on")
    starting.set_defaults(run=start)
    actions.add_parser("desktop", help="print the Claude Desktop entry for the Fresh agent").set_defaults(run=desktop)
    guided = actions.add_parser("session", help="a guided run for a Claude Desktop chat")
    guided.add_argument("--no-clipboard", dest="clipboard", action="store_false",
                        help="leave the clipboard and the browser alone (for checking the harness)")
    guided.set_defaults(run=session)
    actions.add_parser("stop", help="stop a stand-in an earlier run left running").set_defaults(run=stop)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
