"""The two ways an agent can reach the home, behind the same tools.

  porch     list_devices / perform_action through engine_lite and the Device Hub:
            scope rules, owner approval, a dry run, then execute with the hub's
            read-back and idempotency.
  baseline  the same two tools wired straight to each device's platform API, as a
            plain integration would do: no scope, no approval, no dry run, no
            read-back, no idempotency. "sent" means the platform accepted it.

Both run on a fresh demo home with the same simulated devices, latencies and
injected fault, so the agent, the task and the home are identical; only the
layer in between differs.
"""

import asyncio
import io
from typing import Any

import httpx

from engine_lite.audit import Audit
from engine_lite.engine import EngineLite, flatten
from engine_lite.policy import SCOPES
from porch_hub.drivers import DriverError
from porch_hub.faults import FaultyDriver, with_faults
from porch_hub.main import Settings, create_app
from porch_hub.models import ControlPath
from porch_hub.seed import LG_BINDINGS, demo_home

from .cases import PATH_OF, Case

AGENT_NAME = "bench-agent"
LG_ID = {binding.porch_id: lg_id for lg_id, binding in LG_BINDINGS.items()}
# The hub's read-back gives up after this long. Shorter than the agent's tool timeout, so a
# command that never takes effect is reported as failed before the agent stops waiting.
CONFIRM_TIMEOUT_S = 3.0


class Home:
    """One fresh demo home, with faults injectable and the real device state readable."""

    def __init__(self, simulate_latency: bool = True) -> None:
        self.demo = demo_home(simulate_latency=simulate_latency)
        self.drivers: list[FaultyDriver] = with_faults(self.demo.drivers)
        self.by_path: dict[ControlPath, FaultyDriver] = {d.control_path: d for d in self.drivers}

    def prepare(self, case: Case) -> None:
        if case.fault is not None:
            # A copy, so one run using the fault up does not change the case for the next run.
            fault = type(case.fault)(**{**case.fault.__dict__})
            self.by_path[PATH_OF[case.device_id]].inject(case.device_id, fault)
        if case.remote_control_off:
            self.demo.thinq_cloud.set_remote_control(LG_ID[case.device_id], False)

    async def truth(self, device_id: str) -> dict[str, Any]:
        """What the device itself shows, read past any injected fault: the benchmark's ground truth."""
        driver = self.by_path[PATH_OF[device_id]]
        clusters = await driver.inner.read_state(device_id)
        return flatten({name: cluster.model_dump() for name, cluster in clusters.items()})

    def applied(self, device_id: str, command: str) -> int:
        """How many times the device physically received this command."""
        return self.by_path[PATH_OF[device_id]].applied[(device_id, command)]


class Porch:
    name = "porch"

    def __init__(self, home: Home) -> None:
        self.home = home
        self.app = create_app(home.drivers, Settings(confirm_timeout_s=CONFIRM_TIMEOUT_S, demo_offline_mode=False,
                                                     digital_copy_path=None))
        self.audit = Audit(stream=io.StringIO())

    async def __aenter__(self) -> "Porch":
        self._lifespan = self.app.router.lifespan_context(self.app)
        await self._lifespan.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://hub", timeout=60)
        self.engine = EngineLite(self.client, AGENT_NAME, SCOPES["energy"], self.audit)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.client.aclose()
        await self._lifespan.__aexit__(*exc)

    async def list_devices(self) -> dict[str, Any]:
        return await self.engine.list_devices()

    async def perform_action(self, device_id: str, command: str, params: dict[str, Any], reason: str,
                             approval_id: str | None = None) -> dict[str, Any]:
        return await self.engine.perform_action(device_id, command, params, reason, approval_id)

    async def owner_answers(self, approval_id: str, approved: bool) -> None:
        await self.engine.answer(approval_id, approved)

    async def settle(self) -> None:
        """Wait for commands still running in the hub (one the agent stopped waiting for keeps going)."""
        await self.app.state.hub.commands.drain()

    def evidence(self) -> list[str]:
        return self.audit.lines


class Baseline:
    name = "baseline"

    def __init__(self, home: Home) -> None:
        self.home = home
        self._pending: list[asyncio.Future] = []
        self._log: list[str] = []

    async def __aenter__(self) -> "Baseline":
        for driver in self.home.drivers:
            await driver.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        for driver in self.home.drivers:
            await driver.stop()

    async def list_devices(self) -> dict[str, Any]:
        devices = []
        for driver in self.home.drivers:
            for device in await driver.list_devices():
                devices.append({
                    "device_id": device.id, "name": device.name, "room": device.room, "type": device.device_type,
                    "online": device.online, "state": flatten({k: v.model_dump() for k, v in device.clusters.items()}),
                })
        return {"agent": AGENT_NAME, "devices": devices}

    async def perform_action(self, device_id: str, command: str, params: dict[str, Any], reason: str,
                             approval_id: str | None = None) -> dict[str, Any]:
        driver = self.home.by_path[PATH_OF[device_id]]
        # A request already on its way keeps going on the platform's side even if the agent stops waiting.
        task = asyncio.ensure_future(driver.send_command(device_id, command, params))
        self._pending.append(task)
        try:
            await asyncio.shield(task)
        except DriverError as exc:
            self._log.append(f"{command} on {device_id}: error {exc}")
            return {"status": "error", "device_id": device_id, "command": command, "error": str(exc)}
        self._log.append(f"{command} on {device_id}: sent")
        return {"status": "sent", "device_id": device_id, "command": command,
                "note": "The platform accepted the command."}

    async def owner_answers(self, approval_id: str, approved: bool) -> None:
        raise RuntimeError("the baseline has no owner approval")

    async def settle(self) -> None:
        await asyncio.gather(*self._pending, return_exceptions=True)

    def evidence(self) -> list[str]:
        return self._log


CONDITIONS = {"porch": Porch, "baseline": Baseline}
