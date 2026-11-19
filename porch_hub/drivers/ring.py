"""Ring driver: the linked account's doorbells, read only.

Ring's partner API lists the devices of a Ring account that its owner linked to a private Ring
Partner app. The hub lists each doorbell and whether it is online, and sends nothing to it.
Button presses, motion and video are not here yet: Ring reports events by webhook, which needs
an HTTPS address Ring can reach (see docs/REAL_HOME.md).

The link is a file only the owner can read (RING_AUTHORIZATION_FILE, normally
~/.config/porch-hub/ring-authorization.json): the app's client id and secret, and the refresh
token Ring issued for the linked account.

    python -m porch_hub.drivers.ring configure      enter the three values at hidden prompts

Ring can replace the refresh token when it is used, and the driver saves the new one at once.
So two programs must not each keep a copy of the file: the first to refresh could leave the
other with a token that no longer works. Run one, or point both at the same file.
"""

import argparse
import asyncio
import getpass
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .. import clusters as c
from ..models import Clusters, ControlPath, Device, DeviceType, utcnow
from ..private_files import config_dir, private_write, read_private
from .base import BaseDriver, CommandRejected, DeviceNotFound, DeviceOffline, DriverError

log = logging.getLogger(__name__)

RING_API = "https://api.amazonvision.com/v1"
TOKEN_URL = "https://oauth.ring.com/oauth/token"
# Ring is asked for the device list at most this often.
LIST_TTL_S = 30.0
# After a failed listing Ring is left alone this long, doubling up to RETRY_MAX_S.
RETRY_S = 30.0
RETRY_MAX_S = 600.0
DEFAULT_ROOM = "Unassigned"


class RingAuthorization(BaseModel):
    """The Ring Partner app's link to one Ring account. Its values never appear in errors or logs."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    client_id: str = Field(min_length=1, repr=False)
    client_secret: str = Field(min_length=1, repr=False)
    refresh_token: str = Field(min_length=1, repr=False)
    access_token: str | None = Field(default=None, min_length=1, repr=False)
    expires_at: AwareDatetime | None = None

    def refreshed(self, payload: dict[str, Any]) -> "RingAuthorization":
        """This link after Ring's answer to a token refresh. Keeps the refresh token if Ring sent no new one."""
        expires_in = payload["expires_in"]
        if type(expires_in) is not int or expires_in <= 0:
            raise ValueError("unexpected Ring token lifetime")
        return RingAuthorization(
            client_id=self.client_id, client_secret=self.client_secret,
            refresh_token=payload.get("refresh_token") or self.refresh_token,
            access_token=payload["access_token"],
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in))

    @classmethod
    def load(cls, path: Path) -> "RingAuthorization":
        return cls.model_validate_json(read_private(path))

    def save(self, path: Path) -> None:
        private_write(path, self.model_dump_json(indent=2) + "\n")


@dataclass(frozen=True)
class RingBinding:
    """Where a Ring device sits in the Porch home."""

    porch_id: str
    room: str = DEFAULT_ROOM


@dataclass(frozen=True)
class RingDevice:
    ring_id: str
    name: str
    doorbell: bool  # it has a button, which is how the API marks a doorbell
    online: bool


@dataclass(frozen=True)
class RingRow:
    """One device Ring reports, for discovery: what it is and what the hub does with it."""

    ring_id: str
    name: str
    online: bool
    porch_id: str | None  # None when the hub leaves this kind out


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "device"


def ring_devices(document: Any) -> list[RingDevice]:
    """The devices in Ring's answer (a JSON:API document). Raises ValueError when it has another shape."""
    if not isinstance(document, dict) or not isinstance(document.get("data"), list):
        raise ValueError("Ring's device list has an unexpected shape")
    included: dict[tuple[str, str], dict] = {}
    for item in document.get("included") or []:
        key, attributes = (item["type"], item["id"]), item.get("attributes") or {}
        if not all(isinstance(part, str) and part for part in key) or not isinstance(attributes, dict):
            raise ValueError("Ring's device list has an unexpected shape")
        if included.setdefault(key, attributes) != attributes:
            raise ValueError("Ring described one resource twice, differently")

    def related(item: dict, name: str, kind: str) -> dict:
        link = ((item.get("relationships") or {}).get(name) or {}).get("data") or {}
        return included.get((kind, link.get("id")), {}) if link.get("type") == kind else {}

    devices = []
    for item in document["data"]:
        if not isinstance(item, dict) or item.get("type") != "devices":
            continue
        ring_id = item.get("id")
        if not isinstance(ring_id, str) or not ring_id:
            raise ValueError("Ring's device list has an unexpected shape")
        name = (item.get("attributes") or {}).get("name")
        button = related(item, "capabilities", "device-capabilities").get("button_press")
        devices.append(RingDevice(
            ring_id=ring_id,
            name=name if isinstance(name, str) and name else "Unnamed Ring device",
            doorbell=isinstance(button, dict) and button.get("supported") is True,
            # Online only when Ring says so: a missing status is not passed on as online.
            online=related(item, "status", "device-status").get("online") is True,
        ))
    return devices


def _default_client() -> Any:
    import httpx

    return httpx.AsyncClient(timeout=4, trust_env=False)


class RingDriver(BaseDriver):
    control_path = ControlPath.RING

    def __init__(self, authorization_path: Path, home_id: str = "home-1",
                 bindings: dict[str, RingBinding] | None = None, *,
                 client_factory: Callable[[], Any] = _default_client, list_ttl_s: float = LIST_TTL_S) -> None:
        super().__init__()
        self._authorization_path = authorization_path
        self._home_id = home_id
        self._bindings = bindings or {}
        self._client_factory = client_factory
        self._list_ttl_s = list_ttl_s
        self._token_lock = asyncio.Lock()
        self._devices: list[RingDevice] = []
        self._listed_at: float | None = None
        self._updated_at: dict[str, datetime] = {}
        self._started = False
        self._failures = 0
        self._retry_at = 0.0
        self._last_problem = "Ring is not connected yet"

    async def start(self) -> None:
        try:
            RingAuthorization.load(self._authorization_path)
        except FileNotFoundError:
            raise DriverError(f"Ring is not set up: {self._authorization_path.name} is missing. "
                              "Run: python -m porch_hub.drivers.ring configure") from None
        except PermissionError as exc:
            raise DriverError(f"Ring is not set up: {exc}") from None
        except (OSError, ValueError):
            raise DriverError(f"Ring is not set up: {self._authorization_path.name} cannot be used") from None
        await self._refresh()
        self._started = True

    async def stop(self) -> None:
        self._started = False

    async def list_devices(self) -> list[Device]:
        if not self._started:
            raise DriverError(self._last_problem)  # Ring is not asked before start() succeeds
        if self._listed_at is None or time.monotonic() - self._listed_at > self._list_ttl_s:
            await self._refresh()
        return [self._to_device(d) for d in self._devices if d.doorbell]

    async def read_state(self, device_id: str) -> Clusters:
        self._known(device_id)
        self._record(await self._fetch())
        doorbell = self._known(device_id)
        if not doorbell.online:
            raise DeviceOffline(f"{device_id} is offline in Ring")
        return self._clusters()

    async def send_command(self, device_id: str, command: str, params: dict[str, Any]) -> None:
        raise CommandRejected("Ring doorbells are read only in Porch")

    def preflight(self, device_id: str, command: str, params: dict[str, Any], clusters: Clusters) -> None:
        raise CommandRejected("Ring doorbells are read only in Porch")

    def rows(self) -> list[RingRow]:
        """Everything Ring reported at the last listing, for discovery."""
        return [RingRow(d.ring_id, d.name, d.online, self.porch_id(d) if d.doorbell else None) for d in self._devices]

    def porch_id(self, device: RingDevice) -> str:
        if (binding := self._bindings.get(device.ring_id)) is not None:
            return binding.porch_id
        mark = hashlib.sha256(device.ring_id.encode()).hexdigest()[:6]
        return f"{self._home_id}/{_slug(device.name)}-{mark}"

    # Reading Ring

    async def _refresh(self) -> None:
        if time.monotonic() < self._retry_at:
            raise DriverError(self._last_problem)
        try:
            devices = await self._fetch()
        except DriverError as exc:
            self._failures += 1
            self._retry_at = time.monotonic() + min(RETRY_S * 2 ** (self._failures - 1), RETRY_MAX_S)
            self._last_problem = str(exc)
            raise
        self._failures = 0
        self._record(devices)

    def _record(self, devices: list[RingDevice]) -> None:
        before = {d.ring_id: d for d in self._devices}
        for device in devices:
            if before.get(device.ring_id) != device:
                self._updated_at[device.ring_id] = utcnow()
        self._devices = devices
        self._listed_at = time.monotonic()

    async def _fetch(self) -> list[RingDevice]:
        """Ring's device list, read now. Errors name their type only: no token, no answer from Ring."""
        document = await self._fetch_document("status,capabilities")
        try:
            return ring_devices(document)
        except ValueError as exc:
            raise DriverError(f"Ring could not be read ({type(exc).__name__})") from None

    async def _fetch_document(self, include: str) -> Any:
        """Ring's device list as Ring sends it (a JSON:API document), with the parts named."""
        try:
            async with self._client_factory() as client:
                token = await self._access_token(client)
                response = await self._get_devices(client, token, include)
                if response.status_code == 401:
                    token = await self._access_token(client, rejected_token=token)
                    response = await self._get_devices(client, token, include)
                return response.raise_for_status().json()
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            detail = type(exc).__name__ + (f", HTTP {status}" if type(status) is int else "")
            raise DriverError(f"Ring could not be read ({detail})") from None

    async def details(self, device_id: str) -> list[dict[str, str]]:
        """Everything Ring reports for the device, read now: its status, what it supports, its settings."""
        from ..details import rows_from, value_text

        device = self._known(device_id)
        document = await self._fetch_document("status,capabilities,configurations")
        included = {(i.get("type"), i.get("id")): i.get("attributes") or {} for i in document.get("included") or []
                    if isinstance(i, dict)}
        item = next((d for d in document.get("data", []) if isinstance(d, dict) and d.get("id") == device.ring_id), None)
        if item is None:
            raise DeviceOffline(f"{device_id} is no longer in Ring")

        def part(name: str, kind: str) -> dict:
            link = ((item.get("relationships") or {}).get(name) or {}).get("data") or {}
            return included.get((kind, link.get("id")), {}) if link.get("type") == kind else {}

        rows = [{"group": "Device", "name": "Name in Ring", "value": device.name}]
        rows += rows_from("Status", part("status", "device-status"))
        supports = part("capabilities", "device-capabilities")
        for feature, value in sorted(supports.items()):
            if value is None:
                continue  # Ring lists every feature it knows; null means this device has none of it
            if isinstance(value, dict):
                settings = value.get("configurations")
                text = "yes" if value.get("supported", True) else "no"
                if isinstance(settings, list) and settings:
                    text += f" (settings: {value_text([s.replace('_', ' ') for s in settings])})"
                if isinstance(value.get("supported_resolutions"), list):  # Ring gives one number per resolution
                    text = f"resolutions {value_text(value['supported_resolutions'])}, ratio {value.get('ratio', '?')}"
            else:
                text = value_text(value)
            rows.append({"group": "What it supports", "name": feature.replace("_", " ").capitalize(), "value": text})
        settings = dict(part("configurations", "device-configurations"))

        def zones(count: int) -> str:  # zones are drawn over the camera's view: shown as a count only
            return f"{count} zone" if count == 1 else f"{count} zones"

        motion = settings.get("motion_detection")
        if isinstance(motion, dict) and isinstance(motion.get("motion_zones"), list):
            settings["motion_detection"] = {**motion, "motion_zones": zones(len(motion["motion_zones"]))}
        image = settings.get("image_enhancements")
        if isinstance(image, dict) and isinstance(image.get("privacy_zones"), list):
            settings["image_enhancements"] = {**image, "privacy_zones": zones(len(image["privacy_zones"]))}
        rows += rows_from("Settings", settings)
        return rows

    @staticmethod
    async def _get_devices(client: Any, token: str, include: str = "status,capabilities") -> Any:
        return await client.get(f"{RING_API}/devices", params={"include": include},
                                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.api+json"})

    async def _access_token(self, client: Any, rejected_token: str | None = None) -> str:
        async with self._token_lock:
            authorization = RingAuthorization.load(self._authorization_path)
            if (authorization.access_token and authorization.access_token != rejected_token
                    and authorization.expires_at
                    and authorization.expires_at > datetime.now(timezone.utc) + timedelta(seconds=30)):
                return authorization.access_token
            # Its own task: a replaced refresh token is saved even if this request is cancelled.
            refresh = asyncio.create_task(self._refresh_token(client, authorization))
            try:
                return await asyncio.shield(refresh)
            except asyncio.CancelledError:
                await asyncio.gather(refresh, return_exceptions=True)
                raise

    async def _refresh_token(self, client: Any, authorization: RingAuthorization) -> str:
        response = await client.post(TOKEN_URL, data={
            "grant_type": "refresh_token", "client_id": authorization.client_id,
            "client_secret": authorization.client_secret, "refresh_token": authorization.refresh_token,
        })
        refreshed = authorization.refreshed(response.raise_for_status().json())
        refreshed.save(self._authorization_path)
        return refreshed.access_token

    def _known(self, device_id: str) -> RingDevice:
        for device in self._devices:
            if device.doorbell and self.porch_id(device) == device_id:
                return device
        raise DeviceNotFound(f"ring has no device {device_id!r}")

    @staticmethod
    def _clusters() -> Clusters:
        return {c.PORCH_DOORBELL: c.porch_doorbell(button_press_supported=True)}

    def _to_device(self, device: RingDevice) -> Device:
        binding = self._bindings.get(device.ring_id)
        return Device(
            id=self.porch_id(device), name=device.name, room=binding.room if binding else DEFAULT_ROOM,
            device_type=DeviceType.DOORBELL, online=device.online, primary_control_path=self.control_path,
            clusters=self._clusters(), last_updated_at=self._updated_at.get(device.ring_id) or utcnow(),
        )


def authorization_file(env: Mapping[str, str] = os.environ) -> Path:
    value = env.get("RING_AUTHORIZATION_FILE")
    return Path(value).expanduser() if value else config_dir(env) / "ring-authorization.json"


def ring_driver_from_env(env: Mapping[str, str] = os.environ) -> RingDriver:
    """Build the Ring driver.

    RING_AUTHORIZATION_FILE   the private file linking the Ring Partner app to the account (required)
    RING_BINDINGS             JSON {"<Ring device id>": {"porch_id": "...", "room": "..."}}
    PORCH_HOME_ID             home id prefix for device ids (default home-1)
    """
    bindings = {ring_id: RingBinding(b["porch_id"], b.get("room") or DEFAULT_ROOM)
                for ring_id, b in json.loads(env.get("RING_BINDINGS") or "{}").items()}
    return RingDriver(authorization_file(env), home_id=env.get("PORCH_HOME_ID", "home-1"), bindings=bindings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m porch_hub.drivers.ring",
                                     description="Save the Ring Partner app's link to your Ring account for the hub.")
    parser.add_argument("--file", type=Path, default=None,
                        help="where to save it (default: RING_AUTHORIZATION_FILE, else the settings folder)")
    parser.add_subparsers(dest="action", required=True).add_parser(
        "configure", help="enter the client id, client secret and refresh token at hidden prompts")
    args = parser.parse_args(argv)
    path = (args.file or authorization_file()).expanduser()
    try:
        authorization = RingAuthorization(
            client_id=getpass.getpass("Ring Partner client ID (hidden): ").strip(),
            client_secret=getpass.getpass("Ring Partner client secret (hidden): ").strip(),
            refresh_token=getpass.getpass("Linked Ring refresh token (hidden): ").strip(),
        )
        authorization.save(path)
    except (OSError, ValueError):
        # A validation error could quote what was typed, so it is not printed.
        print("Stopped: all three values are needed, and the file must be writable.", file=sys.stderr)
        return 1
    print(f"Saved privately to {path}.")
    print("Set RING_AUTHORIZATION_FILE to that path in home.env, then restart the hub.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
