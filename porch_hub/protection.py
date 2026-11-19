"""Family protection: real devices move only inside a short window the owner opens.

People live in the home. An agent, a script or a stuck retry must not switch something on next
to someone who is asleep, so a hub that runs real devices refuses every command until the owner
opens a control window: named devices, named commands, for at most 30 minutes.

    python -m porch_hub.protection allow --device home-1/living-stand --minutes 5
    python -m porch_hub.protection protect       close the window now
    python -m porch_hub.protection status
    python -m porch_hub.protection set-pin       choose the owner PIN for the pages' button
    python -m porch_hub.protection always --device home-1/living-stand
                                                 let named devices be switched at any time
    python -m porch_hub.protection always --none take that back

(deploy/home/owner.sh allow, protect, protection, set-pin and always run these with this home's settings.)

The owner can also name devices that are always allowed: Porch may switch them on and off at
any time, with no window. It is for a home where the owner's agents should be able to act while
the owner is away from the keyboard. It is a standing choice kept in its own file, set only
from the hub's computer and never from a page or over HTTP, and every page shows which devices
it covers. Everything else still needs a window.

The window is a file in the hub's private settings folder. The hub reads it right before each
command is sent. A missing, damaged or expired file means protected.

The pages have a button for it too. Closing the window from a page needs nothing: anyone at home
may put protection back on. Opening one needs the owner's PIN, which the owner chooses with
set-pin and which the hub keeps only as a salted hash. An agent has no PIN, so nothing that
reaches the hub over HTTP can open a window without it, and wrong guesses lock the button for a
while. A program that runs as you on this computer can write the window file directly, as it can
do anything else you can.

This covers commands sent through this hub only. The devices' own apps are not affected, and a
command already sent is not undone when the window closes.
"""

import argparse
import getpass
import hashlib
import hmac
import json
import os
import secrets
import stat
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool

from .private_files import config_dir, private_write, read_private

MAX_MINUTES = 30
# What a window covers when the owner names no command: switching, and playing or pausing a TV.
DEFAULT_COMMANDS = ("On", "Off", "Play", "Pause")
# What "always allowed" covers when the owner names no command.
ALWAYS_COMMANDS = ("On", "Off")
# The owner's yes, with the owner PIN, to one request Porch held: that device takes that command
# once, within this many seconds, with no window (FamilyProtection.allow_once).
PASS_SECONDS = 120
ALWAYS_FILE = "always-allowed.json"
MIN_PIN_LENGTH = 6
PIN_ITERATIONS = 200_000
# Wrong PINs in a row before the button locks, and for how long: it doubles up to the maximum.
PIN_TRIES = 5
PIN_LOCK_S = 60.0
PIN_LOCK_MAX_S = 900.0
OWNER_COMMAND = "deploy/home/owner.sh"


def owner_command() -> str:
    """How the owner runs the window commands. The real-home scripts set PORCH_OWNER_COMMAND to
    their own full path, so a hint can be pasted into a shell in any folder."""
    return os.environ.get("PORCH_OWNER_COMMAND") or OWNER_COMMAND


def how_to_open(device_id: str) -> str:
    return ("The owner opens a control window with the Family protection button on any hub page, "
            f"or with: {owner_command()} allow --device {device_id}")


class PhysicalCommandBlocked(Exception):
    """Family protection refused a command. Nothing was sent to any device."""


class PinNotSet(Exception):
    """The owner has not chosen a PIN, so no page can open a window."""


class WrongPin(Exception):
    pass


class PinLocked(Exception):
    """Too many wrong PINs in a row. seconds says how long the button stays locked."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"locked for {seconds} seconds")
        self.seconds = seconds


def owner_pin_file(env: Mapping[str, str] = os.environ) -> Path:
    """PORCH_OWNER_PIN_FILE, or owner-pin.json in the hub's settings folder."""
    value = env.get("PORCH_OWNER_PIN_FILE")
    return Path(value).expanduser() if value else config_dir(env) / "owner-pin.json"


def _pin_hash(pin: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, iterations)


def set_pin(path: Path, pin: str) -> None:
    """Choose the owner PIN. Only a salted hash is written, in a file only the owner can read."""
    if len(pin) < MIN_PIN_LENGTH or pin != pin.strip():
        raise ValueError(f"an owner PIN has at least {MIN_PIN_LENGTH} characters and no spaces around it")
    salt = secrets.token_bytes(16)
    private_write(path, json.dumps({"salt": salt.hex(), "hash": _pin_hash(pin, salt, PIN_ITERATIONS).hex(),
                                    "iterations": PIN_ITERATIONS}) + "\n")


class OwnerPin:
    """Checks the owner's PIN for the pages' button, and locks after wrong guesses."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._wrong = 0
        self._locked_until = 0.0

    def _saved(self) -> tuple[bytes, bytes, int] | None:
        try:
            saved = json.loads(read_private(self.path))
            return bytes.fromhex(saved["salt"]), bytes.fromhex(saved["hash"]), int(saved["iterations"])
        except (OSError, ValueError, KeyError, TypeError):
            return None  # missing, damaged, or readable by others: as good as not set

    def is_set(self) -> bool:
        return self._saved() is not None

    def verify(self, pin: str) -> None:
        """Return when pin is the owner's. Raises PinNotSet, PinLocked or WrongPin otherwise."""
        saved = self._saved()
        if saved is None:
            raise PinNotSet()
        if (wait := self._locked_until - time.monotonic()) > 0:
            raise PinLocked(int(wait) + 1)
        salt, expected, iterations = saved
        if hmac.compare_digest(_pin_hash(pin, salt, iterations), expected):
            self._wrong = 0
            return
        self._wrong += 1
        if self._wrong >= PIN_TRIES:
            lock = min(PIN_LOCK_S * 2 ** (self._wrong - PIN_TRIES), PIN_LOCK_MAX_S)
            self._locked_until = time.monotonic() + lock
            raise PinLocked(int(lock))
        raise WrongPin()


class Window(BaseModel):
    """What the owner allows right now. The default is nothing."""

    model_config = ConfigDict(extra="forbid")

    family_protection: StrictBool = True
    expires_at: AwareDatetime | None = None
    device_ids: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=lambda: list(DEFAULT_COMMANDS))


class Always(BaseModel):
    """The devices the owner lets Porch switch at any time, with no window. The default is none."""

    model_config = ConfigDict(extra="forbid")

    device_ids: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=lambda: list(ALWAYS_COMMANDS))


def protection_file(env: Mapping[str, str] = os.environ) -> Path:
    """PORCH_PROTECTION_FILE, or permission.json in the hub's settings folder."""
    value = env.get("PORCH_PROTECTION_FILE")
    return Path(value).expanduser() if value else config_dir(env) / "permission.json"


def _owner_only(path: Path) -> None:
    """Raise unless path is a regular file that only its owner can write."""
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("not a regular file that only its owner can write")


class FamilyProtection:
    def __init__(self, path: Path, pin: OwnerPin | None = None, always_path: Path | None = None) -> None:
        self.path = path
        self.pin = pin
        # The always-allowed list sits beside the window file unless it is given its own place.
        self.always_path = always_path or path.with_name(ALWAYS_FILE)
        # One action each, from the owner's yes to a held request: [device id, command, until
        # (monotonic), why]. In memory only, so a restart drops any that were not used.
        self._passes: list[tuple[str, str, float, str]] = []

    def allow_once(self, device_id: str, command: str, why: str, seconds: float = PASS_SECONDS) -> None:
        """The owner said yes, with the owner PIN, to one exact request: this device takes this
        command once, within seconds, as if a window were open for it alone."""
        self._passes.append((device_id, command, time.monotonic() + seconds, why))

    def _pass_for(self, device_id: str, command: str) -> tuple[str, str, float, str] | None:
        now = time.monotonic()
        self._passes = [p for p in self._passes if p[2] > now]
        return next((p for p in self._passes if p[0] == device_id and p[1] == command), None)

    def spend(self, device_id: str, command: str) -> None:
        """The command was sent: its pass, if it had one, is used up."""
        if (used := self._pass_for(device_id, command)) is not None:
            self._passes.remove(used)

    def always(self) -> Always:
        """The devices that need no window. A missing or unusable file means none: protected."""
        try:
            _owner_only(self.always_path)
            return Always.model_validate_json(self.always_path.read_text())
        except (OSError, ValueError):
            return Always()

    def _window(self) -> tuple[Window, str | None]:
        """The window on disk, and why it does not allow anything right now (None when it does)."""
        try:
            _owner_only(self.path)  # someone else being able to write the file would let them open a window
            window = Window.model_validate_json(self.path.read_text())
        except FileNotFoundError:
            return Window(), "Family protection is on: no control window has been opened."
        except (OSError, ValueError):
            return Window(), "Family protection is on: the control window file cannot be used."
        if window.family_protection:
            return window, "Family protection is on."
        if window.expires_at is None or window.expires_at <= datetime.now(timezone.utc):
            closed = f" at {window.expires_at.astimezone():%H:%M:%S}" if window.expires_at else ""
            return window, f"Family protection is on: the control window closed{closed}."
        if not window.device_ids or not window.commands:
            return window, "Family protection is on: the control window names no device or no command."
        return window, None

    def refusal(self, device_id: str, command: str) -> str | None:
        """Why this command must not be sent now, or None when the owner allows it: by naming the
        device as always allowed, or by an open window."""
        always = self.always()
        if device_id in always.device_ids and command in always.commands:
            return None
        if self._pass_for(device_id, command) is not None:
            return None  # the owner's yes to this exact request
        window, reason = self._window()
        if reason:
            return f"{reason} {how_to_open(device_id)}"
        if device_id not in window.device_ids:
            return (f"Family protection: {device_id} is not in the open control window "
                    f"({', '.join(window.device_ids)}). {how_to_open(device_id)}")
        if command not in window.commands:
            return (f"Family protection: {command} is not in the open control window "
                    f"({', '.join(window.commands)}).")
        return None

    def check(self, device_id: str, command: str) -> None:
        if (reason := self.refusal(device_id, command)) is not None:
            raise PhysicalCommandBlocked(reason)

    def summary(self) -> dict:
        window, reason = self._window()
        always = self.always()
        return {
            # The owner's standing choice: these devices take these commands with no window.
            "always_allowed": {"device_ids": always.device_ids, "commands": always.commands if always.device_ids else []},
            "always_command": f"{owner_command()} always",
            "enforced": True,
            "live_writes_enabled": reason is None,
            "reason": reason,
            "expires_at": window.expires_at.isoformat() if window.expires_at else None,
            "device_ids": window.device_ids if reason is None else [],
            "commands": window.commands if reason is None else [],
            # For the pages' button: whether the owner has a PIN, the longest window, and the
            # command that sets the PIN, with its full path so it runs from any folder.
            "pin_set": self.pin is not None and self.pin.is_set(),
            "max_minutes": MAX_MINUTES,
            "set_pin_command": f"{owner_command()} set-pin",
            # Requests the owner said yes to, each letting one command through once.
            "approved_once": [{"device_id": d, "command": c, "seconds_left": max(0, round(until - time.monotonic())), "why": why}
                              for d, c, until, why in self._passes if until > time.monotonic()],
        }


NOT_ENFORCED = {"enforced": False, "live_writes_enabled": True, "reason": None, "expires_at": None,
                "device_ids": [], "commands": [], "pin_set": False, "max_minutes": MAX_MINUTES,
                "always_allowed": {"device_ids": [], "commands": []}}


def open_window(path: Path, device_ids: list[str], minutes: int, commands: list[str] | None = None) -> Window:
    """Let the named devices take the named commands for the next minutes. Replaces any open window."""
    devices = [d.strip() for d in device_ids]
    if not devices or any(not d or "*" in d for d in devices):
        raise ValueError("name each device by its exact id from GET /devices; there are no wildcards")
    if not 1 <= minutes <= MAX_MINUTES:
        raise ValueError(f"a control window lasts 1 to {MAX_MINUTES} minutes")
    names = [c.strip() for c in (commands or DEFAULT_COMMANDS)]
    if any(not c or "*" in c for c in names):
        raise ValueError("name each command exactly, for example On or Off")
    window = Window(family_protection=False, device_ids=list(dict.fromkeys(devices)),
                    commands=list(dict.fromkeys(names)),
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=minutes))
    private_write(path, window.model_dump_json(indent=2) + "\n")
    return window


def close_window(path: Path) -> None:
    private_write(path, Window().model_dump_json(indent=2) + "\n")


def set_always(path: Path, device_ids: list[str], commands: list[str] | None = None) -> Always:
    """Name the devices that take commands at any time, in place of the list before. An empty
    list takes the standing permission back."""
    devices = [d.strip() for d in device_ids]
    if any(not d or "*" in d for d in devices):
        raise ValueError("name each device by its exact id from GET /devices; there are no wildcards")
    names = [c.strip() for c in (commands or ALWAYS_COMMANDS)]
    if any(not c or "*" in c for c in names):
        raise ValueError("name each command exactly, for example On or Off")
    always = Always(device_ids=list(dict.fromkeys(devices)), commands=list(dict.fromkeys(names)))
    private_write(path, always.model_dump_json(indent=2) + "\n")
    return always


def _describe(protection: FamilyProtection) -> str:
    state = protection.summary()
    always = state["always_allowed"]
    standing = (f"\nAlways allowed, with no window: {', '.join(always['device_ids'])} ({', '.join(always['commands'])}). "
                f"Take it back with: {owner_command()} always --none" if always["device_ids"] else "")
    if not state["live_writes_enabled"]:
        rest = " to any other device" if always["device_ids"] else ""
        return f"{state['reason']} No real command will be sent{rest}.{standing}"
    until = datetime.fromisoformat(state["expires_at"]).astimezone()
    return (f"Control window open until {until:%H:%M:%S} for: {', '.join(state['device_ids'])}\n"
            f"Commands: {', '.join(state['commands'])}\n"
            f"Close it early with: {owner_command()} protect{standing}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m porch_hub.protection",
                                     description="Open or close the owner's control window for real devices.")
    parser.add_argument("--file", type=Path, default=None,
                        help="the window file (default: PORCH_PROTECTION_FILE, else the settings folder)")
    actions = parser.add_subparsers(dest="action", required=True)
    allow = actions.add_parser("allow", help="let named devices take commands for a few minutes")
    allow.add_argument("--device", action="append", required=True, help="exact device id; repeat for more")
    allow.add_argument("--minutes", type=int, default=5, help=f"1 to {MAX_MINUTES} (default 5)")
    allow.add_argument("--command", action="append", help="command to allow; repeat for more (default On, Off, Play and Pause)")
    actions.add_parser("protect", help="close the window now")
    actions.add_parser("status", help="say whether a window is open")
    always = actions.add_parser("always", help="name the devices that take commands at any time, with no window")
    always.add_argument("--device", action="append", default=[], help="exact device id; repeat for more")
    always.add_argument("--none", action="store_true", help="no device: take the standing permission back")
    always.add_argument("--command", action="append", help="command to allow; repeat for more (default On and Off)")
    always.add_argument("--always-file", type=Path, default=None,
                        help="where the list is kept (default: beside the window file)")
    pin = actions.add_parser("set-pin", help="choose the owner PIN that the pages' button asks for")
    pin.add_argument("--pin-file", type=Path, default=None,
                     help="where its hash is kept (default: PORCH_OWNER_PIN_FILE, else the settings folder)")
    args = parser.parse_args(argv)
    path = (args.file or protection_file()).expanduser()
    if args.action == "set-pin":
        first = getpass.getpass(f"Owner PIN, at least {MIN_PIN_LENGTH} characters (hidden): ")
        if first != getpass.getpass("Again (hidden): "):
            print("Stopped: the two did not match. Nothing was changed.", file=sys.stderr)
            return 1
        try:
            set_pin((args.pin_file or owner_pin_file()).expanduser(), first)
        except (OSError, ValueError) as exc:
            print(f"Stopped: {exc}", file=sys.stderr)
            return 1
        print("Owner PIN saved. The pages' Family protection button asks for it to open a control window.")
        return 0
    always_path = getattr(args, "always_file", None)
    try:
        if args.action == "allow":
            open_window(path, args.device, args.minutes, args.command)
        elif args.action == "protect":
            close_window(path)
        elif args.action == "always":
            if bool(args.device) == args.none:
                raise ValueError("name the devices with --device, or take the permission back with --none")
            set_always((always_path or path.with_name(ALWAYS_FILE)).expanduser(), args.device, args.command)
    except (OSError, ValueError) as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1
    print(_describe(FamilyProtection(path, always_path=always_path)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
