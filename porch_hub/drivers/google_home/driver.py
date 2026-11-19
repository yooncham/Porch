"""Google Home driver: the home's Google devices through Google's Home MCP server.

Lights can be switched on and off. Plugs are listed and read, and switched only when the
owner's binding says "switchable": true. A TV is listed and read, and can be played and paused. Other kinds are left out; run `python -m porch_hub.discover` to see
everything Google reports and what the hub does with it.

State is what Google answers when asked. Google sends no push to this driver, so the device list
is re-read at most every LIST_TTL_S, and a command is confirmed by asking again until Google
reports the new state.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from ... import clusters as c
from ...models import Clusters, ControlPath, Device, utcnow
from ...private_files import config_dir, private_write
from ..base import BaseDriver, CommandRejected, DeviceNotFound, DeviceOffline, DriverError
from .inventory import (PLUG_TYPE, TV_TYPE, ActionRefused, Endpoint, PreflightError, Reading, answer_shape, failure_text,
                        inventory_endpoints, inventory_failure, read_home_states, read_inventory,
                        read_state_record, safe_error, validate_action_result, validate_command)
from .session import ACTION_TOOL, google_session, setup_problems, SetupIncomplete

log = logging.getLogger(__name__)

T = TypeVar("T")

# Google is asked for the whole device list at most this often.
LIST_TTL_S = 30.0
# How long the hub waits for the owner's browser sign-in when the saved one no longer works.
SIGN_IN_TIMEOUT_S = 60
# After a failed listing, Google is left alone this long, doubling up to RETRY_MAX_S. Without it,
# a dashboard that lists devices every few seconds would start a new session each time.
RETRY_S = 30.0
RETRY_MAX_S = 600.0
DEFAULT_ROOM = "Unassigned"
# Google's whole answer to the last action sent, for the owner to read on this computer: why it
# was not confirmed, or what Google said when it accepted an action the device then did not
# carry out. Nothing from it goes into a message or a log line.
LAST_ACTION_FILE = "google-home-last-action.json"


@dataclass(frozen=True)
class GoogleHomeBinding:
    """The Porch id for a Google Home device, and its room when Google's own is not the one wanted.

    read_only: the hub lists and reads the device and never commands it, whatever Google offers.
    switchable: a plug the owner lets Porch switch on and off. Plugs are read only without it.
    """

    porch_id: str
    room: str | None = None
    read_only: bool = False
    switchable: bool = False


@dataclass
class _Job:
    run: Callable[[Any], Awaitable[Any]]
    actions: bool
    done: asyncio.Future


class KeptSession:
    """One Google Home session at a time, kept open while calls keep coming.

    Opening a session starts the sign-in bridge, a Node.js process, which takes seconds. A
    command needs a read, a send and a read-back, so they share one session, which closes after
    idle_s without a call. The MCP SDK requires a session to be opened and closed by the same
    task, so one worker task owns the session and runs every call, one at a time.

    A session that can send actions is opened only when a call needs one; reads use whichever
    session is open. After a call fails for a reason that is not the driver's own refusal, the
    session is closed, so the next call starts from a fresh one.
    """

    def __init__(self, open_session: Callable[[bool], AbstractAsyncContextManager[Any]], idle_s: float = 45.0) -> None:
        self._open = open_session
        self._idle_s = idle_s
        self._jobs: asyncio.Queue[_Job] = asyncio.Queue()
        self._worker: asyncio.Task | None = None

    async def call(self, run: Callable[[Any], Awaitable[T]], *, actions: bool = False) -> T:
        """Run run(client) on the open session, opening one first if needed.

        Cancelling the caller does not stop a call that has started: an action must not be cut
        off halfway. A call that has not started yet is dropped.
        """
        job = _Job(run, actions, asyncio.get_running_loop().create_future())
        self._jobs.put_nowait(job)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work(), name="porch-google-home-session")
        return await job.done

    async def close(self) -> None:
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        while not self._jobs.empty():
            job = self._jobs.get_nowait()
            if not job.done.done():
                job.done.set_exception(DriverError("the Google Home driver is stopping"))

    async def _work(self) -> None:
        turn = _Turn()
        try:
            # No await between finding nothing left and returning, so call() never hands a job
            # to a worker that is about to end.
            while turn.next is not None or not self._jobs.empty():
                turn.current = turn.next or self._jobs.get_nowait()
                turn.next = None
                if turn.current.done.done():  # the caller gave up before it started
                    continue
                try:
                    await self._serve(turn)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:  # opening or closing the session failed (may be an ExceptionGroup)
                    if not turn.current.done.done():
                        turn.current.done.set_exception(exc)
        except asyncio.CancelledError:
            for job in (turn.current, turn.next):
                if job is not None and not job.done.done():
                    job.done.set_exception(DriverError("the Google Home driver is stopping"))
            raise

    async def _serve(self, turn: "_Turn") -> None:
        """Open a session for turn.current and keep serving jobs on it.

        Leaves a job in turn.next when that job needs a session of another kind, so it is not
        lost if closing this one fails.
        """
        actions = turn.current.actions
        async with self._open(actions) as client:
            while True:
                job = turn.current
                if not job.done.done():
                    try:
                        result = await job.run(client)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if not job.done.done():
                            job.done.set_exception(exc)
                        if not isinstance(exc, DriverError):
                            return  # the session itself may be broken
                    else:
                        if not job.done.done():
                            job.done.set_result(result)
                try:
                    async with asyncio.timeout(self._idle_s):
                        job = await self._jobs.get()
                except TimeoutError:
                    return
                if job.actions and not actions:
                    turn.next = job  # reopen with the action tool for this one
                    return
                turn.current = job


@dataclass
class _Turn:
    """The job a session is serving, and one waiting for a session of another kind."""

    current: _Job | None = None
    next: _Job | None = None


class GoogleUnavailable(RuntimeError):
    """Google Home could not be read on this session. Its text is safe to show."""


@dataclass
class _Known:
    """A device the hub lists, and the last thing Google said about it."""

    porch_id: str
    room: str
    endpoint: Endpoint
    reading: Reading = Reading()
    read_only: bool = False  # from this home's settings
    switchable: bool = False  # a plug the owner lets Porch switch
    updated_at: datetime = field(default_factory=utcnow)

    def commands(self) -> list[str]:
        """What the hub offers for this device now: what Google's description allows, unless
        this home's settings say the device is read only."""
        return [] if self.read_only else self.endpoint.commands(self.switchable)


@dataclass(frozen=True)
class GoogleRow:
    """One device Google reports, for discovery: what it is and what the hub does with it."""

    google_id: str
    name: str
    google_type: str
    room: str | None
    online: bool | None  # None for a kind the hub does not read
    porch_id: str | None  # None when the hub leaves this kind out
    hub_type: str | None
    commands: tuple[str, ...]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "device"


def to_clusters(endpoint: Endpoint, reading: Reading, commands: list[str]) -> Clusters:
    """commands: the ones the hub offers for this device (none when it is read only)."""
    if endpoint.google_type == TV_TYPE:
        return {c.MEDIA_PLAYBACK: c.media_playback(reading.playback, commands=commands),
                c.PORCH_MEDIA: c.porch_media(reading.activity)}
    clusters = {c.ON_OFF: c.on_off(reading.power, commands=commands)}
    # A light's brightness and white tone, each listed only when Google reports it.
    if reading.level is not None:
        clusters[c.LEVEL_CONTROL] = c.level_control(reading.level)
    if reading.mireds is not None:
        clusters[c.COLOR_CONTROL] = c.color_control(reading.mireds)
    return clusters


def already_there(endpoint: Endpoint, reading: Reading, command: str) -> bool:
    """Whether the device already reports what the command asks for, so nothing needs sending.

    Raises CommandRejected when Google does not report the state that would show the command's
    result, or when the command has nothing to act on.
    """
    if endpoint.google_type == TV_TYPE:
        if reading.playback is None:
            raise CommandRejected(
                "Google Home does not report what this TV is playing, so the result could not be checked")
        if reading.playback == "NotPlaying":
            raise CommandRejected(f"nothing is playing on this TV, so there is nothing to {command.lower()}")
        return reading.playback == ("Playing" if command == "Play" else "Paused")
    if reading.power is None:
        raise CommandRejected(f"Google Home does not report this {'plug' if endpoint.google_type == PLUG_TYPE else 'light'}'s "
                              "power, so the result could not be checked")
    return reading.power is (command == "On")


class GoogleHomeMcpDriver(BaseDriver):
    control_path = ControlPath.GOOGLE_HOME
    # The first call of a burst starts the sign-in bridge and may refresh the sign-in.
    call_timeout_s = 45.0
    read_back_interval_s = 1.0
    read_back_timeout_s = 15.0

    def __init__(self, directory: Path, home_id: str = "home-1",
                 bindings: dict[str, GoogleHomeBinding] | None = None, *,
                 session_factory: Callable[..., AbstractAsyncContextManager[Any]] = google_session,
                 request_timeout_s: float = 10.0, idle_s: float = 45.0, list_ttl_s: float = LIST_TTL_S,
                 diagnostics: Path | None = None) -> None:
        super().__init__()
        self._directory = directory
        self._diagnostics = diagnostics  # where Google's answer to the last action is kept, if anywhere
        self._home_id = home_id
        self._bindings = bindings or {}
        self._request_timeout_s = request_timeout_s
        self._list_ttl_s = list_ttl_s
        self._session = KeptSession(
            lambda actions: session_factory(directory, auth_timeout=SIGN_IN_TIMEOUT_S, allow_actions=actions), idle_s)
        self._check_setup = session_factory is google_session
        self._known: dict[str, _Known] = {}  # by Porch id
        self._unmapped: list[Endpoint] = []
        self._listed_at: float | None = None
        self._started = False
        self._failures = 0
        self._retry_at = 0.0
        self._last_problem = "Google Home is not connected yet"

    async def start(self) -> None:
        if self._check_setup and (problems := setup_problems(self._directory)):
            raise SetupIncomplete("Google Home is not set up: " + "; ".join(problems))
        await self._refresh()
        self._started = True

    async def stop(self) -> None:
        self._started = False
        await self._session.close()

    async def list_devices(self) -> list[Device]:
        if not self._started:
            raise DriverError(self._last_problem)  # no session is opened before start() succeeds
        if self._listed_at is None or time.monotonic() - self._listed_at > self._list_ttl_s:
            await self._refresh()
        return [self._to_device(known) for known in self._known.values()]

    async def read_state(self, device_id: str) -> Clusters:
        known = self._device(device_id)
        google_id = known.endpoint.google_id
        states = await self._read(lambda client: read_home_states(
            client, known.endpoint.structure_id, [google_id], self._request_timeout_s))
        reading = read_state_record(known.endpoint, states.get(google_id))
        self._record(known, reading)
        if not reading.online:
            raise DeviceOffline(f"{device_id} is offline in Google Home")
        return to_clusters(known.endpoint, reading, known.commands())

    async def send_command(self, device_id: str, command: str, params: dict[str, Any]) -> None:
        known = self._device(device_id)
        if known.read_only:
            raise CommandRejected(f"{device_id} is read only in this home's settings, so {command} was not sent")
        google_command = self._google_command(known.endpoint, command)
        google_id = known.endpoint.google_id
        sending = False

        async def act(client: Any) -> None:
            nonlocal sending
            # Ask Google again what this device is and whether it takes the command now: the
            # list the hub holds may be half a minute old.
            report = await read_inventory(client, request_timeout=self._request_timeout_s,
                                          authorization_timeout=self._request_timeout_s)
            if (problem := inventory_failure(report)) is not None:
                raise GoogleUnavailable(problem)
            endpoint = inventory_endpoints(report).get(google_id)
            if endpoint is None:
                raise DeviceOffline(f"{device_id} is no longer in Google Home")
            tools = {tool.get("name") for tool in report["tools"]}
            if not {ACTION_TOOL, "list_home_states"} <= tools:
                raise CommandRejected("Google Home does not offer its action tool to this sign-in")
            try:
                validate_command(endpoint.resource, endpoint.metadata, google_command, known.switchable)
            except PreflightError as exc:
                raise CommandRejected(str(exc)) from None
            states = await read_home_states(client, endpoint.structure_id, [google_id], self._request_timeout_s)
            reading = read_state_record(endpoint, states.get(google_id))
            known.endpoint = endpoint
            self._record(known, reading)
            if not reading.online:
                raise DeviceOffline(f"{device_id} is offline in Google Home")
            if already_there(endpoint, reading, command):
                return  # nothing to send
            action = {"structureId": endpoint.structure_id,
                      "homeActionRequests": [{"id": google_id, "type": endpoint.action_type,
                                              "command": google_command, "parameters": {}}]}
            sending = True
            result = await asyncio.wait_for(client.call_tool(ACTION_TOOL, action), self._request_timeout_s)
            self._keep_answer(device_id, endpoint.action_type, google_command, result)
            try:
                validate_action_result(result, google_id)
            except ActionRefused as exc:
                raise CommandRejected(f"the device answered {exc} to {command}, so it was not carried out") from None
            except Exception:
                log.warning("Google Home's answer to %s for %s was not a confirmation; its shape: %s",
                            google_command, device_id, json.dumps(answer_shape(result)))
                raise

        started = time.perf_counter()
        try:
            await self._session.call(act, actions=True)  # one attempt, never repeated
        except (CommandRejected, DeviceOffline):
            raise
        except Exception as exc:
            detail = str(exc) if isinstance(exc, GoogleUnavailable) else failure_text(safe_error(exc))
            if sending:
                # The router reads the device back; that decides whether it acted.
                raise DriverError(f"Google Home did not confirm the action ({detail})") from None
            raise DeviceOffline(f"Google Home could not be asked before the command was sent ({detail})") from None
        if sending:
            log.info("Google Home accepted %s for %s in %d ms", command, device_id,
                     round((time.perf_counter() - started) * 1000))

    async def details(self, device_id: str) -> list[dict[str, str]]:
        """Everything Google Home reports for the device, read now: every trait's state, and what
        Google knows of the product."""
        from ...details import rows_from, words

        known = self._device(device_id)
        google_id = known.endpoint.google_id
        states = await self._read(lambda client: read_home_states(
            client, known.endpoint.structure_id, [google_id], self._request_timeout_s, traits=None))
        state = states.get(google_id) or {}
        info = (known.endpoint.resource.get("resourceInfo") or {}).get("deviceInfo") or {}
        rows = [{"group": "Device", "name": "Name in Google Home", "value": known.endpoint.name},
                {"group": "Device", "name": "Kind in Google Home", "value": words(known.endpoint.google_type)}]
        rows += rows_from("Device", info)
        rows.append({"group": "Device", "name": "Online", "value": "yes" if state.get("resourceConnectivityState") == "ONLINE" else "no"})
        traits = state.get("traits") if isinstance(state.get("traits"), dict) else {}
        for name in sorted(traits):
            trait = traits[name]
            if isinstance(trait, dict) and name != "Connectivity":
                rows += rows_from(words(name), trait.get("state") if isinstance(trait.get("state"), dict) else {})
        return rows

    def preflight(self, device_id: str, command: str, params: dict[str, Any], clusters: Clusters) -> None:
        known = self._known.get(device_id)
        if known is None:
            return
        if known.read_only:
            raise CommandRejected(f"{device_id} is read only in this home's settings, so {command} was not sent")
        self._google_command(known.endpoint, command)
        if command not in known.commands():
            raise CommandRejected(f"Google Home's description of this device does not allow {command} now")
        playback = clusters.get(c.MEDIA_PLAYBACK)
        if playback is not None and playback.attributes.get("CurrentState") == "NotPlaying":
            raise CommandRejected(f"nothing is playing on this TV, so there is nothing to {command.lower()}")

    def rows(self) -> list[GoogleRow]:
        """Everything Google reported at the last listing, for discovery, mapped or not."""
        rows = [GoogleRow(k.endpoint.google_id, k.endpoint.name, k.endpoint.google_type, k.endpoint.room,
                          k.reading.online, k.porch_id, k.endpoint.device_type.value, tuple(k.commands()))
                for k in self._known.values()]
        rows += [GoogleRow(e.google_id, e.name, e.google_type, e.room, None, None, None, ()) for e in self._unmapped]
        return rows

    # Reading Google

    @staticmethod
    def _google_command(endpoint: Endpoint, command: str) -> str:
        """Google's name for a hub command on this kind of device."""
        google_command = endpoint.google_command(command)
        if google_command is None:
            raise CommandRejected("Google Home control is limited to switching lights, and plugs the owner "
                                  "allows, on and off, and playing and pausing a TV")
        return google_command

    async def _read(self, run: Callable[[Any], Awaitable[T]]) -> T:
        """A read on the kept session. If the session had gone stale, once more on a fresh one."""
        for attempt in (1, 2):
            try:
                return await self._session.call(run)
            except DriverError:
                raise
            except Exception as exc:
                if attempt == 2:
                    if isinstance(exc, GoogleUnavailable):
                        raise DriverError(str(exc)) from None
                    raise DriverError(f"Google Home did not answer ({failure_text(safe_error(exc))})") from None
                log.info("Google Home read failed (%s); trying once on a new session", type(exc).__name__)
        raise AssertionError("unreachable")

    async def _refresh(self) -> None:
        if time.monotonic() < self._retry_at:
            raise DriverError(self._last_problem)
        try:
            endpoints, states = await self._read(self._load)
        except DriverError as exc:
            self._failures += 1
            self._retry_at = time.monotonic() + min(RETRY_S * 2 ** (self._failures - 1), RETRY_MAX_S)
            self._last_problem = str(exc)
            raise
        self._failures = 0
        known: dict[str, _Known] = {}
        unmapped: list[Endpoint] = []
        previous = {k.endpoint.google_id: k for k in self._known.values()}
        for google_id, endpoint in endpoints.items():
            if endpoint.device_type is None:
                unmapped.append(endpoint)
                continue
            binding = self._bindings.get(google_id)
            porch_id = binding.porch_id if binding else self.default_id(endpoint)
            if porch_id in known:
                log.warning("two Google Home devices are bound to %s; keeping %s",
                            porch_id, known[porch_id].endpoint.name)
                continue
            entry = previous.get(google_id) or _Known(porch_id, DEFAULT_ROOM, endpoint)
            entry.porch_id, entry.endpoint = porch_id, endpoint
            entry.read_only = bool(binding and binding.read_only)
            entry.switchable = bool(binding and binding.switchable)
            entry.room = (binding.room if binding and binding.room else None) or endpoint.room or DEFAULT_ROOM
            self._record(entry, read_state_record(endpoint, states.get(google_id)))
            known[porch_id] = entry
        self._known, self._unmapped = known, unmapped
        self._listed_at = time.monotonic()

    async def _load(self, client: Any) -> tuple[dict[str, Endpoint], dict[str, dict]]:
        report = await read_inventory(client, request_timeout=self._request_timeout_s,
                                      authorization_timeout=self._request_timeout_s)
        if (problem := inventory_failure(report)) is not None:
            raise GoogleUnavailable(problem)  # not a DriverError, so the session is replaced before the retry
        endpoints = inventory_endpoints(report)
        mapped = [e for e in endpoints.values() if e.device_type is not None]
        if mapped and "list_home_states" not in {tool.get("name") for tool in report["tools"]}:
            raise DriverError("Google Home does not offer its state tool to this sign-in")
        states: dict[str, dict] = {}
        for structure_id in dict.fromkeys(e.structure_id for e in mapped):
            ids = [e.google_id for e in mapped if e.structure_id == structure_id]
            states.update(await read_home_states(client, structure_id, ids, self._request_timeout_s))
        return endpoints, states

    def default_id(self, endpoint: Endpoint) -> str:
        """The id a device gets without a binding: its name, and a few characters that stay the same
        for this device, so two devices with one name do not collide."""
        mark = hashlib.sha256(endpoint.google_id.encode()).hexdigest()[:6]
        return f"{self._home_id}/{_slug(endpoint.name)}-{mark}"

    def _keep_answer(self, device_id: str, action_type: str, google_command: str, answer: Any) -> None:
        """Keep Google's answer to an action privately, in place of the one before it."""
        if self._diagnostics is None:
            return
        try:
            private_write(self._diagnostics, json.dumps(
                {"at": utcnow().isoformat(), "device_id": device_id, "type": action_type,
                 "command": google_command, "answer": answer}, ensure_ascii=False, indent=2, default=str) + "\n")
        except (OSError, ValueError) as exc:
            log.warning("could not keep Google Home's answer (%s)", type(exc).__name__)

    def _record(self, known: _Known, reading: Reading) -> None:
        if reading != known.reading:
            known.updated_at = utcnow()
        known.reading = reading

    def _device(self, device_id: str) -> _Known:
        try:
            return self._known[device_id]
        except KeyError:
            raise DeviceNotFound(f"google_home has no device {device_id!r}") from None

    def _to_device(self, known: _Known) -> Device:
        return Device(
            id=known.porch_id, name=known.endpoint.name, room=known.room,
            device_type=known.endpoint.device_type, online=known.reading.online,
            primary_control_path=self.control_path,
            clusters=to_clusters(known.endpoint, known.reading, known.commands()),
            last_updated_at=known.updated_at,
        )


def google_home_driver_from_env(env: Mapping[str, str] = os.environ) -> GoogleHomeMcpDriver:
    """Build the live Google Home driver.

    GOOGLE_HOME_DIRECTORY   the private folder with google-home-client.json and the saved sign-in (required)
    GOOGLE_HOME_BINDINGS    JSON {"<Google device id>": {"porch_id": "...", "room": "..."}}; a binding may
                            add "read_only": true (the hub reads the device and never commands it), and
                            for a plug "switchable": true (the owner lets Porch switch it on and off)
    PORCH_HOME_ID           home id prefix for device ids (default home-1)

    Google's answer to an action it does not confirm is kept in the hub's settings folder
    (google-home-last-action.json), readable by the owner only.
    """
    bindings = {google_id: GoogleHomeBinding(b["porch_id"], b.get("room"), b.get("read_only") is True,
                                             b.get("switchable") is True)
                for google_id, b in json.loads(env.get("GOOGLE_HOME_BINDINGS") or "{}").items()}
    return GoogleHomeMcpDriver(Path(env["GOOGLE_HOME_DIRECTORY"]).expanduser(),
                               home_id=env.get("PORCH_HOME_ID", "home-1"), bindings=bindings,
                               diagnostics=config_dir(env) / LAST_ACTION_FILE)
