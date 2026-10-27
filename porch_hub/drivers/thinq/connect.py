"""ThinQCloud on LG's official SDK: ThinQApi for REST, ThinQMQTTClient for push over AWS IoT Core.

Needs the lg extra (pip install -e ".[lg]") and a personal access token from
connect-pat.lgthinq.com with these scopes: view devices, view device status,
device control, event subscription and push notifications.
"""

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar

import aiohttp
from thinqconnect import ThinQApi, ThinQAPIErrorCodes, ThinQAPIException
from thinqconnect.const import CLIENT_PREFIX

from ..base import CommandRejected, DeviceNotFound, DeviceOffline, DriverError
from .driver import RemoteControlDisabled, ThinQBinding, ThinQDriver
from .mqtt import AsyncThinQMQTTClient

log = logging.getLogger(__name__)

T = TypeVar("T")
E = ThinQAPIErrorCodes

_NOT_FOUND = {E.NOT_EXIST_DEVICE, E.NOT_REGISTERED_DEVICE, E.INVALID_DEVICE_ID, E.NOT_OWNED_DEVICE,
              E.ALREADY_DEVICE_DELETED, E.NOT_REGISTERED_DEVICE_BY_USER}
_REJECTED = {E.COMMAND_NOT_SUPPORTED_IN_STATE, E.COMMAND_NOT_SUPPORTED_IN_ERROR,
             E.COMMAND_NOT_SUPPORTED_IN_POWER_OFF, E.COMMAND_NOT_SUPPORTED_IN_MODE,
             E.INVALID_COMMAND_ERROR, E.FAIL_DEVICE_CONTROL, E.NOT_SUPPORTED_FEATURE,
             E.NOT_PROVIDED_FEATURE, E.NO_CONTROL_AUTHORITY, E.UNACCEPTABLE_PARAMETERS}
_ALREADY_SUBSCRIBED = {E.ALREADY_SUBSCRIBED_PUSH}

DEFAULT_CLIENT_ID_FILE = Path.home() / ".config" / "porch-hub" / "thinq-client-id"


def _translate(exc: ThinQAPIException) -> DriverError:
    """LG error codes as hub errors. The SDK's message is kept; the token never appears in it."""
    if exc.code == E.COMMAND_NOT_SUPPORTED_IN_REMOTE_OFF:
        return RemoteControlDisabled()
    if exc.code == E.NOT_CONNECTED_DEVICE:
        return DeviceOffline(f"LG appliance is not connected: {exc.message}")
    if exc.code in _NOT_FOUND:
        return DeviceNotFound(f"LG: {exc.error_name} - {exc.message}")
    if exc.code in _REJECTED:
        return CommandRejected(f"LG refused the command: {exc.error_name} - {exc.message}")
    return DriverError(f"LG ThinQ API error {exc.error_name} ({exc.code}): {exc.message}")


class ThinQConnectCloud:
    def __init__(self, access_token: str, country_code: str, client_id: str) -> None:
        self._access_token = access_token
        self._country_code = country_code
        self._client_id = client_id
        self._session: aiohttp.ClientSession | None = None
        self._api: ThinQApi | None = None
        self._mqtt: AsyncThinQMQTTClient | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()
        self._api = ThinQApi(self._session, self._access_token, self._country_code, self._client_id)

    async def close(self) -> None:
        if self._mqtt is not None:
            try:
                await self._mqtt.async_disconnect()
            except Exception as exc:  # shutting down; nothing useful to raise
                log.warning("LG MQTT disconnect failed: %s", exc)
        if self._session is not None:
            await self._session.close()

    async def list_devices(self) -> list[dict[str, Any]]:
        return await self._call(self._require_api().async_get_device_list()) or []

    async def get_profile(self, lg_device_id: str) -> dict[str, Any]:
        return await self._call(self._require_api().async_get_device_profile(lg_device_id)) or {}

    async def get_status(self, lg_device_id: str) -> dict[str, Any] | list[dict[str, Any]]:
        return await self._call(self._require_api().async_get_device_status(lg_device_id)) or {}

    async def control(self, lg_device_id: str, payload: dict[str, Any]) -> None:
        await self._call(self._require_api().async_post_device_control(lg_device_id, payload))

    async def subscribe(
        self,
        lg_device_ids: list[str],
        on_message: Callable[[dict[str, Any]], None],
        on_connection_change: Callable[[bool], None],
    ) -> None:
        api = self._require_api()
        loop = asyncio.get_running_loop()

        # awscrt delivers messages on its own thread; hop back onto the hub's loop.
        def received(topic: str, payload: bytes, dup: bool, qos: Any, retain: bool, **kwargs: Any) -> None:
            try:
                message = json.loads(payload)
            except ValueError:
                log.warning("unreadable LG MQTT message on %s", topic)
                return
            if not loop.is_closed():
                loop.call_soon_threadsafe(on_message, message)

        for lg_id in lg_device_ids:
            await self._subscribe_device(api, lg_id)

        self._mqtt = AsyncThinQMQTTClient(api, self._client_id, received, on_connection_change)
        await self._call(self._mqtt.async_init())  # looks up the account's MQTT broker
        if not await self._call(self._mqtt.async_prepare_mqtt()):
            raise DriverError("LG MQTT setup failed: could not get a client certificate")
        try:
            await self._mqtt.async_connect_mqtt()
        except Exception as exc:  # awscrt raises its own error types; push is optional, reads still work
            log.warning("LG MQTT did not connect (%s); state will come from API reads only", exc)

    async def _subscribe_device(self, api: ThinQApi, lg_id: str) -> None:
        """Status events (DEVICE_STATUS) and notifications (DEVICE_PUSH) for one appliance."""
        for subscribe in (api.async_post_event_subscribe, api.async_post_push_subscribe):
            try:
                await subscribe(lg_id)
            except ThinQAPIException as exc:
                if exc.code not in _ALREADY_SUBSCRIBED:
                    log.warning("LG %s for %s failed: %s", subscribe.__name__, lg_id, exc)

    async def _call(self, request: Awaitable[T]) -> T:
        try:
            return await request
        except ThinQAPIException as exc:
            raise _translate(exc) from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise DriverError(f"LG ThinQ API unreachable: {exc!r}") from exc

    def _require_api(self) -> ThinQApi:
        if self._api is None:
            raise DriverError("ThinQConnectCloud used before start()")
        return self._api


def load_client_id(path: Path) -> str:
    """One stable client id per hub install. LG can block accounts that keep creating new clients."""
    if path.exists():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write a private temporary file, then link it into place: if two hubs start together,
    # exactly one link succeeds and both use that id. Readers never see a half-written file.
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"{CLIENT_PREFIX}-{uuid.uuid4()}")
    try:
        os.link(tmp, path)
    except FileExistsError:
        pass
    finally:
        tmp.unlink()
    return path.read_text().strip()


def thinq_driver_from_env(env: Mapping[str, str] = os.environ) -> ThinQDriver:
    """Build the live LG driver.

    LG_THINQ_PAT           personal access token (required)
    LG_THINQ_COUNTRY       two-letter country of the LG account (default US)
    LG_THINQ_CLIENT_ID     fixed client id; otherwise one is created once in LG_THINQ_CLIENT_ID_FILE
    LG_THINQ_BINDINGS      JSON {"<LG device id>": {"porch_id": "...", "room": "..."}}
    PORCH_HOME_ID          home id prefix for device ids (default home-1)
    """
    client_id = env.get("LG_THINQ_CLIENT_ID") or load_client_id(
        Path(env.get("LG_THINQ_CLIENT_ID_FILE", DEFAULT_CLIENT_ID_FILE))
    )
    bindings = {lg_id: ThinQBinding(**b) for lg_id, b in json.loads(env.get("LG_THINQ_BINDINGS", "{}")).items()}
    cloud = ThinQConnectCloud(env["LG_THINQ_PAT"], env.get("LG_THINQ_COUNTRY", "US"), client_id)
    return ThinQDriver(cloud, home_id=env.get("PORCH_HOME_ID", "home-1"), bindings=bindings)
