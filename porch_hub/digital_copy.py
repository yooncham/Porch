"""The digital copy of the home: dry runs, and the offline backup for Demo Day.

The registry already mirrors every device's live state. This module works on
that mirror without touching hardware:

- simulate() predicts what a command would do: the state after, whether it
  would succeed (the same platform rules the live path applies, see
  BaseDriver.preflight), and how long it would take, from observed latencies.
- In offline mode the hub stops using drivers altogether. Commands mutate the
  copy at once, reads come from it, and every change still goes out on the
  event bus, so the Engine and the stage UI keep working when the venue
  network is gone.
- A snapshot of the copy is kept on disk, so the hub can start straight into
  offline mode with the devices and state from the last rehearsal. It also
  keeps every command response by idempotency key, so a retry after a restart
  replays the answer instead of running the command again; above all, a
  command answered by the copy offline never becomes real on a later retry.
"""

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from . import commands
from .commands import CommandSpec, InvalidParams, UnsupportedCommand
from .drivers import BaseDriver, CommandRejected
from .idempotency import IdempotencyConflict
from .models import Clusters, CommandResponse, ControlPath, Device, utcnow
from .registry import DeviceRegistry, Route
from .seed import demo_records

log = logging.getLogger(__name__)

# Starting estimates before any command has been measured: local Matter answers in
# well under a second; the cloud paths add a round trip and poll for the read-back.
DEFAULT_LATENCY_MS: dict[ControlPath, int] = {
    ControlPath.MATTER: 150,
    ControlPath.GOOGLE_HOME: 1500,
    ControlPath.LG_THINQ: 2500,
}
# Weight of the newest measurement in the running estimate.
LATENCY_SMOOTHING = 0.3
SNAPSHOT_SAVE_INTERVAL_S = 10.0
# A platform that failed to start is tried again after this, doubling up to START_RETRY_MAX_S.
START_RETRY_S = 5.0
START_RETRY_MAX_S = 120.0
# How long a saved command response answers retries of its idempotency key.
RESPONSE_TTL_S = 24 * 3600


def env_flag(name: str, env: Mapping[str, str] = os.environ) -> bool:
    return env.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


class SimulateRequest(BaseModel):
    # Extra keys (an idempotency_key sent out of habit) are ignored: a dry run changes nothing.
    model_config = ConfigDict(extra="ignore")

    command: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class SimulateResponse(BaseModel):
    simulated_state_after: Clusters
    would_succeed: bool
    estimated_latency_ms: int
    # The path the command would take, and why it would fail.
    control_path: ControlPath | None = None
    reason: str | None = None


@dataclass
class _Plan:
    route: Route | None
    spec: CommandSpec | None
    params: BaseModel | None
    reason: str | None


class DigitalCopy:
    def __init__(self, registry: DeviceRegistry, drivers: Sequence[BaseDriver],
                 offline: bool = False, snapshot_path: Path | None = None) -> None:
        self._registry = registry
        self._drivers = list(drivers)
        self._started: set[int] = set()  # ids of drivers whose start() ran
        self._offline = offline
        self._snapshot_path = snapshot_path
        self._saved_at = 0.0
        self._latency_ms: dict[ControlPath, float] = {p: float(ms) for p, ms in DEFAULT_LATENCY_MS.items()}
        self._responses: dict[str, tuple[str, dict[str, Any], float]] = {}  # key -> (fingerprint, response, saved_at)
        self._live_commands = 0
        self._no_live_commands = asyncio.Event()
        self._no_live_commands.set()
        self._switching = asyncio.Lock()
        self._background: set[asyncio.Task] = set()
        self._retry_s = START_RETRY_S
        self._save_pending: asyncio.TimerHandle | None = None
        registry.change_listeners.append(self._schedule_save)

    @property
    def offline(self) -> bool:
        return self._offline

    # Lifecycle

    async def start(self) -> None:
        self._load_responses()
        if self._offline:
            log.warning("DEMO_OFFLINE_MODE: drivers are not started; commands run on the digital copy only")
            # No driver is called in offline mode, not even to list devices.
            self._registry.offline_mode = True
            if not self._load_snapshot():
                log.warning("no saved digital copy%s; starting from the built-in demo home",
                            f" at {self._snapshot_path}" if self._snapshot_path else "")
                self._registry.load_source_records(demo_records())
            self._registry.mark_all_reachable()
            return
        await self._start_drivers()
        await self._registry.refresh()
        self.save_snapshot(force=True)

    async def stop(self) -> None:
        for task in list(self._background):
            if task.get_name() == "porch-switch":
                await asyncio.gather(task, return_exceptions=True)  # a mode switch finishes first
            else:
                task.cancel()
        if self._save_pending is not None:
            self._save_pending.cancel()
        self.save_snapshot(force=True)
        await self._stop_drivers()

    async def set_offline(self, offline: bool) -> None:
        """Switch modes. The switch runs as its own task: a cancelled request cannot stop it halfway."""
        task = asyncio.create_task(self._switch(offline), name="porch-switch")
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        await asyncio.shield(task)

    async def _switch(self, offline: bool) -> None:
        """Switch modes at runtime, for example when the venue network drops mid-demo.

        Going offline: new commands go to the copy at once; commands already
        running on a device finish, read-back included, before the copy takes
        over and the drivers are stopped. Coming back: the drivers start and the
        home is re-read while commands still go to the copy, so none reaches a
        driver that is not ready.
        """
        async with self._switching:
            if offline == self._offline:
                return
            if offline:
                self._offline = True
                if self._live_commands:
                    log.warning("offline demo mode: waiting for %d running command(s) to finish",
                                self._live_commands)
                await self._no_live_commands.wait()
                self._registry.offline_mode = True
                await self._stop_drivers()
                self._registry.mark_all_reachable()
                self.save_snapshot(force=True)
                log.warning("offline demo mode on: commands now run on the digital copy only")
                self._registry.events.publish("mode_changed", None, None, data={"offline": True})
                return
            await self._start_drivers()
            self._registry.offline_mode = False
            await self._registry.refresh()  # live state replaces the copy; differences go out as events
            self._offline = False
            log.warning("offline demo mode off: commands go to devices again")
            self._registry.events.publish("mode_changed", None, None, data={"offline": False})

    @contextlib.asynccontextmanager
    async def live_command(self) -> AsyncIterator[None]:
        """Wrap each command sent to a real device, so going offline can wait for it."""
        self._live_commands += 1
        self._no_live_commands.clear()
        try:
            yield
        finally:
            self._live_commands -= 1
            if not self._live_commands:
                self._no_live_commands.set()

    async def _stop_drivers(self) -> None:
        for driver in self._drivers:
            if id(driver) not in self._started:
                continue
            self._started.discard(id(driver))
            try:
                await driver.stop()
            except Exception:
                log.exception("stopping %s failed", driver.control_path)

    async def _start_drivers(self) -> bool:
        """Start every driver not yet running. Returns whether all are running.

        One platform failing to start (no network, bad token) must not take the
        hub down; it is retried in the background until it starts.
        """
        failed = not await self._start_drivers_once()
        if failed and not any(t.get_name() == "porch-start-retry" for t in self._background):
            task = asyncio.create_task(self._retry_starts(), name="porch-start-retry")
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        return not failed

    async def _retry_starts(self) -> None:
        delay = self._retry_s
        while not self._offline:
            await asyncio.sleep(delay)
            if self._offline:
                return
            async with self._switching:  # never race a mode switch
                if self._offline:
                    return
                all_started = await self._start_drivers_once()
                await self._registry.refresh()
            if all_started:
                log.warning("every platform is running again")
                return
            delay = min(delay * 2, START_RETRY_MAX_S)

    async def _start_drivers_once(self) -> bool:
        all_started = True
        for driver in self._drivers:
            if id(driver) in self._started:
                continue
            try:
                await driver.start()
                self._started.add(id(driver))
            except Exception as exc:
                all_started = False
                log.warning("starting %s failed (%s); it will be retried", driver.control_path, exc)
        return all_started

    # Dry run and offline commands

    def simulate(self, device: Device, command: str, params: dict[str, Any]) -> SimulateResponse:
        """What the command would do, without sending it anywhere."""
        plan = self._plan(device, command, params)
        state = self._state_after(device, plan)
        path = plan.route.path if plan.route else None
        return SimulateResponse(
            simulated_state_after=state,
            would_succeed=plan.reason is None,
            estimated_latency_ms=self.estimate_latency_ms(path or device.primary_control_path),
            control_path=path,
            reason=plan.reason,
        )

    def run_offline(self, device: Device, spec: CommandSpec, params: BaseModel, key: str) -> CommandResponse:
        """Offline mode: apply the command to the copy at once, as the device would have."""
        started = time.perf_counter()
        plan = self._plan(device, spec.name, params.model_dump(), spec=spec, parsed=params)
        state = self._state_after(device, plan)
        if plan.reason is None:
            self._registry.write_shadow_state(device.id, state)
            self.save_snapshot(force=True)
        latency_ms = round((time.perf_counter() - started) * 1000)
        log.info("simulated %s %s on %s (offline mode)%s", "done" if plan.reason is None else "failed",
                 spec.name, device.id, f": {plan.reason}" if plan.reason else "")
        return CommandResponse(
            status="done" if plan.reason is None else "failed", state_after=state, latency_ms=latency_ms,
            idempotency_key=key, control_path=plan.route.path if plan.route else None,
            simulated=True, error=plan.reason,
        )

    def set_attribute(self, device: Device, cluster: str, attribute: str, value: Any) -> Clusters:
        """Offline mode: change a reading by hand (a leak, a lock turned at the door) to drive a scene."""
        if cluster not in device.clusters:
            raise UnsupportedCommand(f"{device.id} has no {cluster} cluster")
        state = {name: c.model_copy(deep=True) for name, c in device.clusters.items()}
        state[cluster].attributes[attribute] = value
        self._registry.write_shadow_state(device.id, state)
        self.save_snapshot(force=True)
        return state

    def _plan(self, device: Device, command: str, raw_params: dict[str, Any],
              spec: CommandSpec | None = None, parsed: BaseModel | None = None) -> _Plan:
        try:
            if spec is None:
                spec = commands.resolve(device, command)
            if parsed is None:
                parsed = spec.parse_params(raw_params)
        except UnsupportedCommand as exc:
            return _Plan(None, None, None, str(exc))
        except InvalidParams as exc:
            details = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors)
            return _Plan(None, spec, None, f"invalid params for {command}: {details}")

        routes = [r for r in self._registry.routes(device.id) if commands.supports(r.record, spec)]
        if not self._offline:
            # Live, the command can only take a path that is up; offline, the copy answers for every path.
            routes = [r for r in routes if r.record.online]
        if not routes:
            return _Plan(None, spec, parsed, f"{device.id} has no reachable path that supports {spec.name}")
        route = routes[0]
        try:
            route.driver.preflight(route.device_id, spec.name, parsed.model_dump(), device.clusters)
            commands.expected_attributes(spec, parsed, device.clusters)  # the live path checks the same
        except (CommandRejected, commands.CannotPredict) as exc:
            return _Plan(route, spec, parsed, f"{route.path}: {exc}" if len(routes) > 1 else str(exc))
        return _Plan(route, spec, parsed, None)

    @staticmethod
    def _state_after(device: Device, plan: _Plan) -> Clusters:
        state = {name: c.model_copy(deep=True) for name, c in device.clusters.items()}
        if plan.reason is None and plan.spec is not None and plan.params is not None:
            commands.apply(plan.spec, plan.params, state)
        return state

    # Latency estimates

    def observe(self, path: ControlPath, latency_ms: int) -> None:
        """Fold a measured live command into the path's running estimate."""
        current = self._latency_ms.get(path, float(latency_ms))
        self._latency_ms[path] = (1 - LATENCY_SMOOTHING) * current + LATENCY_SMOOTHING * latency_ms

    def estimate_latency_ms(self, path: ControlPath) -> int:
        return round(self._latency_ms.get(path, max(DEFAULT_LATENCY_MS.values())))

    # Command responses, kept across restarts

    def recall(self, key: str, fp: str) -> CommandResponse | None:
        """A response saved for this key by this hub or an earlier run of it. Raises on a reused key."""
        entry = self._responses.get(key)
        if entry is None:
            return None
        saved_fp, response, saved_at = entry
        if time.time() - saved_at > RESPONSE_TTL_S:
            del self._responses[key]
            return None
        if saved_fp != fp:
            raise IdempotencyConflict(f"idempotency_key {key!r} was already used for a different command")
        return CommandResponse.model_validate(response)

    def remember(self, key: str, fp: str, response: CommandResponse) -> None:
        self._responses[key] = (fp, response.model_dump(mode="json"), time.time())
        self.save_snapshot(force=True)

    # Snapshot on disk

    def save_snapshot(self, force: bool = False) -> None:
        if self._snapshot_path is None:
            return
        if not force and time.monotonic() - self._saved_at < SNAPSHOT_SAVE_INTERVAL_S:
            return
        records = [r.model_dump(mode="json") for r in self._registry.source_records()]
        if not records:
            # Never overwrite a good snapshot with an empty home: keep the records already saved.
            records = (self._read_snapshot() or {}).get("records", [])
        cutoff = time.time() - RESPONSE_TTL_S
        payload = {
            "saved_at": utcnow().isoformat(),
            "latency_ms": {p.value: round(ms) for p, ms in self._latency_ms.items()},
            "records": records,
            "responses": {key: {"fingerprint": fp, "response": response, "saved_at": saved_at}
                          for key, (fp, response, saved_at) in self._responses.items() if saved_at >= cutoff},
        }
        try:
            self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._snapshot_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=1))
            tmp.replace(self._snapshot_path)
            self._saved_at = time.monotonic()
        except OSError as exc:
            log.warning("could not save the digital copy to %s: %s", self._snapshot_path, exc)

    def _schedule_save(self) -> None:
        """After a pushed change: save now, or once the save interval has passed. Never loses the last change."""
        if self._snapshot_path is None or self._save_pending is not None:
            return
        wait = SNAPSHOT_SAVE_INTERVAL_S - (time.monotonic() - self._saved_at)
        if wait <= 0:
            self.save_snapshot(force=True)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # not on the hub's loop (a test poking a fake); the next save on the loop covers it
        self._save_pending = loop.call_later(wait, self._save_now)

    def _save_now(self) -> None:
        self._save_pending = None
        self.save_snapshot(force=True)

    def _read_snapshot(self) -> dict[str, Any] | None:
        if self._snapshot_path is None or not self._snapshot_path.exists():
            return None
        try:
            payload = json.loads(self._snapshot_path.read_text())
            if not isinstance(payload, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as exc:
            self._set_aside(f"unreadable ({exc})")
            return None
        return payload

    def _set_aside(self, why: str) -> None:
        """Keep a damaged snapshot for inspection instead of letting the next save overwrite it."""
        aside = self._snapshot_path.with_name(f"{self._snapshot_path.name}.corrupt-{int(time.time())}")
        try:
            self._snapshot_path.replace(aside)
            log.error("the digital copy at %s is %s; kept it as %s and started without it",
                      self._snapshot_path, why, aside)
        except OSError as exc:
            log.error("the digital copy at %s is %s and could not be moved aside: %s", self._snapshot_path, why, exc)

    def _load_responses(self) -> None:
        saved = (self._read_snapshot() or {}).get("responses", {})
        for key, entry in saved.items():
            try:
                self._responses[key] = (entry["fingerprint"], entry["response"], float(entry["saved_at"]))
            except (KeyError, TypeError, ValueError):
                log.warning("ignored an unreadable saved response for idempotency key %r", key)

    def _load_snapshot(self) -> bool:
        payload = self._read_snapshot()
        if payload is None:
            return False
        try:
            records = [Device.model_validate(r) for r in payload["records"]]
        except (ValueError, KeyError, TypeError) as exc:
            self._set_aside(f"not a valid digital copy ({exc})")
            return False
        if not records:
            return False
        self._registry.load_source_records(records)
        for path, ms in payload.get("latency_ms", {}).items():
            if path in ControlPath._value2member_map_:
                self._latency_ms[ControlPath(path)] = float(ms)
        log.warning("loaded %d device records saved at %s", len(records), payload.get("saved_at"))
        return True
