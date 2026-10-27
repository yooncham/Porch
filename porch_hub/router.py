"""Turns an Engine-approved command into a platform call and confirms it.

Confirm, don't assume: a cloud API can accept a command the device then
refuses (LG with Remote Start off), so "done" means the state was read back
and matched, never just that the platform said OK.

A device reachable on several paths is tried on its primary path first
(local Matter by default). The next path is tried only when the command
certainly did not reach the device, so a command never runs twice.
"""

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any

from pydantic import BaseModel

from . import commands
from .commands import CommandSpec
from .digital_copy import DigitalCopy
from .drivers import BaseDriver, CommandRejected, DeviceNotFound, DeviceOffline, DriverError
from .idempotency import IdempotencyStore, fingerprint
from .models import Clusters, CommandRequest, CommandResponse, ControlPath, Device
from .registry import DeviceRegistry, Route

log = logging.getLogger(__name__)


class _NotDelivered(Exception):
    """A path failed before the device received the command, so the next path may try."""


def _join(*errors: str | None) -> str | None:
    return "; ".join(e for e in errors if e) or None


class CommandRouter:
    def __init__(self, registry: DeviceRegistry, idempotency: IdempotencyStore,
                 confirm_timeout_s: float = 5.0, poll_interval_s: float = 0.2,
                 digital_copy: DigitalCopy | None = None, driver_timeout_s: float = 10.0) -> None:
        self._registry = registry
        self._driver_timeout_s = driver_timeout_s
        self._digital_copy = digital_copy
        self._idempotency = idempotency
        self._confirm_timeout_s = confirm_timeout_s
        self._poll_interval_s = poll_interval_s
        self._device_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def execute(self, device_id: str, request: CommandRequest) -> CommandResponse:
        """Run one command. Lookup and validation errors raise and are not cached; outcomes are.

        The command runs as its own task (see IdempotencyStore), so cancelling
        this request does not stop it, and a retry with the same key waits for
        it instead of sending the command again.
        """
        key = request.idempotency_key
        fp = fingerprint(device_id, request.command, request.params)
        if self._digital_copy is not None and (saved := self._digital_copy.recall(key, fp)) is not None:
            log.info("replay %s on %s (key %s, saved before a restart)", request.command, device_id, key)
            self._publish(device_id, request, saved, replayed=True)
            return saved
        response, replayed = await self._idempotency.run(key, fp, lambda: self._execute(device_id, request, fp))
        if replayed:
            log.info("replay %s on %s (key %s)", request.command, device_id, key)
            self._publish(device_id, request, response, replayed=True)
        return response

    def _publish(self, device_id: str, request: CommandRequest, response: CommandResponse, replayed: bool) -> None:
        """Tell event subscribers (the dashboard, the Engine) about a command and how it ended."""
        try:
            source = response.control_path or self._registry.get(device_id).primary_control_path
        except DeviceNotFound:
            source = response.control_path
        self._registry.events.publish(
            "command_executed", device_id, source, command=request.command, params=request.params,
            status=response.status, latency_ms=response.latency_ms, idempotency_key=response.idempotency_key,
            simulated=response.simulated, replayed=replayed, error=response.error, clusters=response.state_after,
        )

    async def drain(self) -> None:
        """Let commands already running finish. Call before stopping the drivers."""
        await self._idempotency.drain()

    async def _execute(self, device_id: str, request: CommandRequest, fp: str) -> CommandResponse:
        key = request.idempotency_key
        device = await self._registry.resolve(device_id)
        spec = commands.resolve(device, request.command)
        params = spec.parse_params(request.params)
        copy = self._digital_copy

        # One command at a time per device, so read-backs never interleave.
        async with self._device_locks[device_id]:
            if copy is None:
                response = await self._run(device, spec, params, key)
                self._publish(device_id, request, response, replayed=False)
                return response
            if copy.offline:
                response = copy.run_offline(device, spec, params, key)
            else:
                async with copy.live_command():  # going offline waits for this to finish
                    response = await self._run(device, spec, params, key)
                if response.status == "done" and response.control_path:
                    copy.observe(response.control_path, response.latency_ms)
            # Saved and announced inside the command's own task, so neither is lost if the request is cancelled.
            copy.remember(key, fp, response)
            self._publish(device_id, request, response, replayed=False)
            return response

    async def _run(self, device: Device, spec: CommandSpec, params: BaseModel, key: str) -> CommandResponse:
        """Try the device's paths in order. Fall back only when nothing reached the device."""
        started = time.perf_counter()
        unreachable: list[str] = []
        routes = [r for r in self._registry.routes(device.id) if commands.supports(r.record, spec)]
        for i, route in enumerate(routes):
            try:
                state, error = await self._run_on(route, spec, params)
            except _NotDelivered as exc:
                unreachable.append(f"{route.path}: {exc}")
                log.warning("%s unreachable via %s: %s", device.id, route.path, exc)
                continue
            errors = None
            if error is not None:
                # Say why better paths were not used: they failed above, or are known to be down.
                skipped = [f"{r.path}: unreachable" for r in routes[i + 1:] if not r.record.online]
                errors = [*unreachable, *skipped, f"{route.path}: {error}" if len(routes) > 1 else error]
            fallback = bool(unreachable) or route.path != device.control_paths[0]
            return self._response(device, route.path, spec, key, started, state, errors, fallback=fallback)
        return self._response(device, None, spec, key, started, device.clusters,
                              unreachable or [f"no path to {device.id} supports {spec.name}"])

    async def _run_on(self, route: Route, spec: CommandSpec, params: BaseModel) -> tuple[Clusters, str | None]:
        """Run the command on one path. Raises _NotDelivered when the path failed before the device got it.

        Once a send has been attempted, the state is always read back: an
        acknowledgement can fail after the device has already acted, so the
        response must never carry the state from before the command.
        """
        driver = route.driver
        try:
            before = await self._bounded(driver.read_state(route.device_id), "read")
        except DriverError as exc:
            raise _NotDelivered(str(exc)) from exc  # nothing was sent yet
        try:
            expected = commands.expected_attributes(spec, params, before)
        except commands.CannotPredict as exc:
            return self._record(route, before), str(exc)  # not sent: its outcome could not be checked

        send_error: str | None = None
        try:
            await self._bounded(driver.send_command(route.device_id, spec.name, params.model_dump()), spec.name)
        except DeviceOffline as exc:
            raise _NotDelivered(str(exc)) from exc  # the platform could not reach the device
        except CommandRejected as exc:
            # Refused: nothing changes, so one fresh read is enough, with no waiting.
            state, read_error = await self._read_once(driver, route.device_id)
            return self._record(route, state), _join(str(exc), read_error)
        except DriverError as exc:
            # The outcome is unknown: the device may have acted. Read back as usual; the
            # state decides. Another path must not repeat the command either way.
            send_error = str(exc)
            log.warning("%s on %s: %s; reading back to see whether it acted", spec.name, route.device_id, exc)

        state, confirmed, read_error = await self._read_back(driver, route.device_id, spec, expected)
        if confirmed:
            return self._record(route, state), None
        timeout = (f"{route.device_id} did not report {spec.cluster} {expected} "
                   f"within {self._timeout_for(driver):g}s of {spec.name}")
        return self._record(route, state), _join(send_error or timeout, read_error)

    def _record(self, route: Route, state: Clusters) -> Clusters:
        if state:
            self._registry.record_path_state(route.path, route.device_id, state, fresh=True)
        return state

    async def _read_once(self, driver: BaseDriver, device_id: str) -> tuple[Clusters, str | None]:
        try:
            return await self._bounded(driver.read_state(device_id), "read"), None
        except DriverError as exc:
            return {}, f"state read-back failed: {exc}"

    async def _bounded(self, call: Any, what: str) -> Any:
        """A driver call that gives up after driver_timeout_s. A timed-out send's outcome is unknown."""
        try:
            return await asyncio.wait_for(call, self._driver_timeout_s)
        except TimeoutError:
            raise DriverError(f"{what} did not answer within {self._driver_timeout_s:g}s") from None

    def _response(self, device: Device, path: ControlPath | None, spec: CommandSpec, key: str, started: float,
                  state: Clusters, errors: list[str] | None, fallback: bool = False) -> CommandResponse:
        latency_ms = round((time.perf_counter() - started) * 1000)
        error = "; ".join(errors) if errors else None
        status = "failed" if error else "done"
        log.info("%s %s on %s via %s%s in %d ms%s", status, spec.name, device.id, path,
                 " (fallback)" if fallback else "", latency_ms, f": {error}" if error else "")
        return CommandResponse(status=status, state_after=state, latency_ms=latency_ms,
                               idempotency_key=key, control_path=path, error=error)

    async def _read_back(self, driver: BaseDriver, device_id: str, spec: CommandSpec,
                         expected: commands.Attributes) -> tuple[Clusters, bool, str | None]:
        """Poll the platform until the expected attributes appear or the timeout passes.

        Returns the last state read after the command ({} if no read succeeded),
        whether it confirmed the command, and the last read error.
        """
        deadline = time.monotonic() + self._timeout_for(driver)
        interval = driver.read_back_interval_s if driver.read_back_interval_s is not None else self._poll_interval_s
        state: Clusters = {}
        read_error: str | None = None
        while True:
            fresh, error = await self._read_once(driver, device_id)
            if error is None:
                state, read_error = fresh, None
                if commands.is_confirmed(spec, expected, state):
                    return state, True, None
            else:
                read_error = error
            if time.monotonic() >= deadline:
                return state, False, read_error
            await asyncio.sleep(interval)

    def _timeout_for(self, driver: BaseDriver) -> float:
        return driver.read_back_timeout_s if driver.read_back_timeout_s is not None else self._confirm_timeout_s
