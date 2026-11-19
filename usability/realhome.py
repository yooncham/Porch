"""The cold-start test on Yoonchan's real home (docs/USABILITY_TEST.md, "The real-home run").

It starts no servers: Porch must already be running (Start Porch.command). It records what the real
gateway does for Fresh agent, the home's energy readings during the run, and every device before
and after, into usability/runs/home-NN/. It never sends anything to a device and never opens a
control window: the owner does that, with the owner PIN, for exactly Living stand and Airpuri.

    porch_gateway/.venv/bin/python usability/realhome.py session
        A guided run in a Claude Desktop chat with the porch-home-fresh connection. If Claude Desktop
        lacks that connection, it asks you to quit the app, adds it, and opens the app again first.

"Start Cold-start Test (HW9, Real Home).command" next to the repository starts it with a double click.
"""

import argparse
import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prompt as prompt_file  # noqa: E402
import standin  # noqa: E402  (shared: say, now, sha, Claude Desktop's settings)

REPO, RUNS, say, now = standin.REPO, standin.RUNS, standin.say, standin.now
HUB = os.environ.get("PORCH_REALHOME_HUB", "http://127.0.0.1:8100")
GATEWAY = os.environ.get("PORCH_REALHOME_GATEWAY", "http://127.0.0.1:8000")
AUDIT = Path(os.environ.get("PORCH_REALHOME_AUDIT") or REPO / "porch_gateway" / "audit.jsonl")
AGENT_ID = "fresh-agent"
MOVABLE = {"home-1/living-stand": "Living stand", "home-1/air-purifier": "Airpuri"}
ENTRY = {"porch-home-fresh": {"command": str(REPO / "porch_gateway/.venv/bin/python"),
                              "args": [str(REPO / "deploy/home/agents.py"), "connect", AGENT_ID]}}
ENERGY_EVERY_S = 5


def get(path: str, base: str = HUB):
    return httpx.get(f"{base}{path}", timeout=20).json()


def porch_up() -> bool:
    with contextlib.suppress(httpx.HTTPError):
        return httpx.get(f"{HUB}/devices", timeout=5).status_code == 200 and \
            httpx.get(f"{GATEWAY}/household/agents", timeout=5).status_code == 200
    return False


def knows_agent() -> bool:
    with contextlib.suppress(httpx.HTTPError, ValueError):
        return any(a.get("id") == AGENT_ID for a in get("/household/agents", GATEWAY))
    return False


def window_covers(protection: dict) -> tuple[bool, list[str]]:
    """Whether the devices that may move are covered (a window, or always allowed), and what else is."""
    if protection.get("enforced") is False:
        return True, []
    open_now = set(protection.get("device_ids") or []) if protection.get("live_writes_enabled") else set()
    always = set((protection.get("always_allowed") or {}).get("device_ids") or [])
    covered = open_now | always
    return set(MOVABLE) <= covered, sorted(covered - set(MOVABLE))


def snapshot() -> dict:
    devices = get("/devices")
    people = []
    with contextlib.suppress(httpx.HTTPError, ValueError):
        people = get("/household").get("people", [])
    protection = get("/protection")
    return {"at": now(), "devices": {d["id"]: {"name": d["name"], "room": d["room"], "type": d["device_type"],
                                               "online": d["online"], "clusters": d["clusters"]} for d in devices},
            "people": [{k: p.get(k) for k in ("id", "name", "where", "room", "room_name")} for p in people],
            "protection": {k: protection.get(k) for k in ("enforced", "live_writes_enabled", "device_ids", "commands",
                                                          "expires_at", "always_allowed")}}


def energy() -> dict:
    summary = get("/home/energy")
    return {"at": now(), "total_w": summary.get("total_w"),
            "meters": [{k: m.get(k) for k in ("id", "name", "power_w", "energy_kwh", "stale", "online")}
                       for m in summary.get("meters", []) if isinstance(m, dict)]}


async def record(run: Path, stop: asyncio.Event) -> None:
    """The gateway's stage feed into stage.jsonl, and the energy readings into energy.jsonl, until stopped."""
    feed_url = GATEWAY.replace("http://", "ws://").replace("https://", "wss://") + "/ws/stage"

    async def stage() -> None:
        with (run / "stage.jsonl").open("a") as out:
            await standin.follow_feed(feed_url, out, stop)

    async def readings() -> None:
        with (run / "energy.jsonl").open("a") as out:
            while not stop.is_set():
                with contextlib.suppress(httpx.HTTPError, ValueError):
                    out.write(json.dumps(await asyncio.to_thread(energy)) + "\n")
                    out.flush()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), ENERGY_EVERY_S)

    await asyncio.gather(stage(), readings())


def wait_until_ready() -> None:
    """Porch up, knowing Fresh agent, with a window over exactly the two devices that may move."""
    while not porch_up():
        input("Porch is not running. Double-click Start Porch.command, wait until it is up, then press Enter here. ")
    while not knows_agent():
        input("Porch's gateway does not know Fresh agent yet: it was started before the agent was added.\n"
              "Restart Porch (Ctrl+C in its window, then double-click Start Porch.command), then press Enter here. ")
    while True:
        covered, extra = window_covers(get("/protection"))
        if covered and not extra:
            return
        if covered:
            answer = input(f"The control window also covers {', '.join(extra)}, so the agent could switch those too.\n"
                           "Open it again for Living stand and Airpuri only, then press Enter; or type go to run anyway. ")
            if answer.strip().lower() == "go":
                return
            continue
        input("Open a control window for exactly Living stand and Airpuri: the Family protection button on any hub\n"
              "page, with the owner PIN, for 30 minutes. Then press Enter here. ")


def session(args: argparse.Namespace) -> None:
    if args.clipboard:
        standin.ensure_desktop_entry(ENTRY)
    wait_until_ready()
    RUNS.mkdir(parents=True, exist_ok=True)
    number = 1
    while (RUNS / f"home-{number:02d}").exists():
        number += 1
    name = f"home-{number:02d}"
    say(f"\nThe cold-start test on the real home, run {name}.")
    start = snapshot()
    for person in start["people"]:
        where = person["room_name"] or "home" if person["where"] == "home" else "away" if person["where"] == "away" else "not set"
        say(f"  {person['name']}: {where}")
    if any(p["where"] == "home" and p.get("room") == "living" and p["id"] != "yoonchan" for p in start["people"]) and \
            not any(p["id"] == "yoonchan" and p["where"] == "home" for p in start["people"]):
        say("  Someone else is set in the living room and you are not: Porch will hold the lamp for them.")
    model = input("\nWhich model will the chat use, as Claude Desktop shows it? ").strip() or "not noted"
    run = RUNS / name
    run.mkdir(parents=True)
    (run / "initial_state.json").write_text(json.dumps(start, indent=2))
    text = prompt_file.prompt("task_home.md")
    (run / "prompt.txt").write_text(text)
    meta = {"name": name, "home": "real", "started": now(), "hub": HUB, "gateway": f"{GATEWAY}/mcp",
            "agent": {"id": AGENT_ID, "name": "Fresh agent", "acts_for": "Yoonchan", "scopes": ["lighting", "appliance"]},
            "may_move": MOVABLE, "window": start["protection"],
            "client": f"a new Claude Desktop chat ({model}) with the porch-home-fresh connection, started by Yoonchan, "
                      "no help during the run",
            "documentation": {"file": "docs/AGENTS.md", "sha256_12": standin.sha(REPO / "docs/AGENTS.md")},
            "task": {"file": "usability/task_home.md", "sha256_12": standin.sha(REPO / "usability/task_home.md")}}
    (run / "meta.json").write_text(json.dumps(meta, indent=2))
    view = f"{HUB}/flow"
    if args.clipboard:
        subprocess.run(["pbcopy"], input=text, text=True, check=True)
    say(f"""
1. In Claude Desktop, open a new chat, outside any project (incognito if memory is on).
2. In its tools menu, switch on porch-home-fresh only: switch off porch-dot, porch-muse,
   porch-instinct, porch-fresh and every other connector. If porch-home-fresh is not there, do not
   send anything: press Ctrl+C here and say so.
3. Paste the prompt ({'it is on the clipboard' if args.clipboard else 'usability/runs/' + name + '/prompt.txt'}), send it, and type nothing else.
   If Claude asks to allow porch-home-fresh's tools, choose Always allow.
4. When it has answered, copy its whole answer (the Copy button under it), come back here and press Ctrl+C.

The live view: {view}   (worth recording, with the living room lamp in the shot)
Recording in usability/runs/{name}/. Press Ctrl+C when the agent has answered.
""")
    if args.clipboard:
        subprocess.run(["open", view], check=False)
    stop = asyncio.Event()

    async def main() -> None:
        loop = asyncio.get_running_loop()
        for number_ in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            loop.add_signal_handler(number_, stop.set)
        await record(run, stop)

    asyncio.run(main())
    (run / "final_state.json").write_text(json.dumps(snapshot(), indent=2))
    with contextlib.suppress(OSError):  # Porch's decisions for this agent during the run, from its audit log
        entries = [line for line in AUDIT.read_text().splitlines() if line.strip()]
        mine = [line for line in entries if (json.loads(line).get("agent") == AGENT_ID
                                             and json.loads(line).get("time", "") >= meta["started"])]
        (run / "audit.jsonl").write_text("".join(f"{line}\n" for line in mine))
    meta = {**json.loads((run / "meta.json").read_text()), "stopped": now()}
    (run / "meta.json").write_text(json.dumps(meta, indent=2))
    say(f"\nSaved the final states, the energy readings and Porch's decisions in usability/runs/{name}/")
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    actions = parser.add_subparsers(dest="action", required=True)
    guided = actions.add_parser("session", help="a guided real-home run for a Claude Desktop chat")
    guided.add_argument("--no-clipboard", dest="clipboard", action="store_false",
                        help="leave the clipboard, the browser and Claude Desktop alone (for checking the harness)")
    guided.set_defaults(run=session)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
