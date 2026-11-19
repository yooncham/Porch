"""Sign in to Google Home for the hub, and show what the account's home holds. Reads only.

    python -m porch_hub.drivers.google_home.login                 sign in if needed, then list the home
    python -m porch_hub.drivers.google_home.login --check-setup   check this computer only; contacts nobody

(deploy/home/google_login.sh runs it with this home's settings.)

The first run opens a browser for the owner to sign in to Google. Google's grant covers reading
the home and controlling its devices; this program lists homes and devices and sends no command.
The sign-in is saved in the private folder, where the hub finds it afterwards.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from ...private_files import config_dir, private_write
from .inventory import HUB_TYPES, Endpoint, inventory_endpoints, inventory_failure, read_inventory, safe_error
from .session import google_session, setup_problems

LAST_CHECK_FILE = "google-home-last-check.json"


def default_directory() -> Path:
    value = os.environ.get("GOOGLE_HOME_DIRECTORY")
    return Path(value).expanduser() if value else config_dir() / "google-home"


async def check(directory: Path, auth_timeout: int) -> dict[str, Any]:
    async with google_session(directory, auth_timeout=auth_timeout) as client:
        return await read_inventory(client, authorization_timeout=auth_timeout)


def _in_the_hub(endpoint: Endpoint) -> str:
    if endpoint.device_type is None:
        return "not used: the hub has no mapping for this kind yet"
    commands = endpoint.commands()
    return f"{endpoint.device_type.value}: {', '.join(commands) if commands else 'read only'}"


def summary_lines(report: dict[str, Any]) -> list[str]:
    """What the owner sees: names and kinds, never Google's ids or its error text."""
    problem = inventory_failure(report)
    if problem is not None:
        lines = [f"{problem}."]
        if report.get("error_reason") == "premium_advanced_required":
            lines.append("Check the subscription for the Google account and home you signed in to, "
                         "then run this again.")
        else:
            lines.append("Check that the sign-in finished in the browser, that the account has a home in the "
                         "Google Home app, and that it may use Google Home's MCP server.")
        return lines
    endpoints = list(inventory_endpoints(report).values())
    homes = len(report["homes"])
    lines = [f"Google Home answered: {homes} home{'s' if homes != 1 else ''}, {len(endpoints)} "
             f"device{'s' if len(endpoints) != 1 else ''}."]
    rows = [("name", "Google's kind", "room", "in the hub"),
            *((e.name, e.google_type, e.room or "-", _in_the_hub(e)) for e in endpoints)]
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    if endpoints:
        lines += ["  " + "  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip() for row in rows]
    if any(e.google_type in HUB_TYPES for e in endpoints):
        lines.append("Next: deploy/home/discover.sh prints the bindings to put in home.env.")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m porch_hub.drivers.google_home.login",
                                     description="Sign in to Google Home for the hub and list the home. Reads only.")
    parser.add_argument("--directory", type=Path, default=None,
                        help="the private folder (default: GOOGLE_HOME_DIRECTORY, else google-home "
                             "in the settings folder)")
    parser.add_argument("--check-setup", action="store_true", help="check this computer only; contact nobody")
    parser.add_argument("--auth-timeout", type=int, default=600, choices=range(60, 901), metavar="SECONDS",
                        help="how long the browser sign-in may take (60 to 900, default 600)")
    args = parser.parse_args(argv)
    directory = (args.directory or default_directory()).expanduser().resolve()

    problems = setup_problems(directory)
    for problem in problems:
        print(f"Not ready: {problem}")
    if problems:
        return 1
    if args.check_setup:
        print("Ready to sign in. Nothing was contacted.")
        return 0

    print("Connecting to Google Home. If a browser opens, sign in there and keep this terminal open "
          f"(up to {args.auth_timeout} seconds).")
    print("Google's grant covers reading the home and controlling devices. This run only reads; "
          "no command is sent.", flush=True)
    previous_umask = os.umask(0o077)
    previous_level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)  # the SDK's logs can carry Google's raw answers
    try:
        try:
            report = asyncio.run(check(directory, args.auth_timeout))
        except KeyboardInterrupt:
            print("Cancelled. No command was sent.")
            return 130
        except Exception as exc:
            report = {"status": "connection_failed", "tools": [], "homes": [], **safe_error(exc)}
        for line in summary_lines(report):
            print(line)
        try:
            private_write(directory / LAST_CHECK_FILE, json.dumps(report, ensure_ascii=False, indent=2))
            print(f"Google's full answer is saved privately in {LAST_CHECK_FILE}. Do not paste it into a chat.")
        except (OSError, ValueError) as exc:
            print(f"Could not save Google's full answer ({type(exc).__name__}).")
        return 0 if report["status"] == "inventory_read" else 2
    finally:
        os.umask(previous_umask)
        logging.disable(previous_level)


if __name__ == "__main__":
    sys.exit(main())
