"""The cross-implementation behavior checks, run as part of this suite.

Ported from ChatGPT/Porch test-results/2026-10-23/behavior_checks.py, which runs
the same six scenarios against both hub implementations. The scenario names,
the trace driver and the pass criteria follow that script.
"""

import asyncio

from porch_hub import clusters as c
from porch_hub.dedup import merge_device_records
from porch_hub.digital_copy import DigitalCopy
from porch_hub.drivers.base import DriverError
from porch_hub.drivers.matter.simulated import SimulatedMatterDriver
from porch_hub.idempotency import IdempotencyStore
from porch_hub.models import CommandRequest, ControlPath, Device, DeviceType, utcnow
from porch_hub.registry import DeviceRegistry
from porch_hub.router import CommandRouter

ID = "home-1/desk-lamp-plug"


def record(ident=ID, path=ControlPath.MATTER, serial=None):
    return Device(id=ident, name="Desk lamp", room="Office", device_type=DeviceType.ON_OFF_PLUG, online=True,
                  primary_control_path=path, serial_number=serial, clusters={c.ON_OFF: c.on_off(False)},
                  last_updated_at=utcnow())


class TraceDriver(SimulatedMatterDriver):
    """Counts every call; can hold the acknowledgement, or lose it after acting."""

    def __init__(self):
        super().__init__([record()])
        self.counts = {"start": 0, "stop": 0, "list": 0, "read": 0, "send": 0}
        self.applied, self.release = asyncio.Event(), asyncio.Event()
        self.block = self.lose_ack = False

    async def start(self):
        self.counts["start"] += 1

    async def stop(self):
        self.counts["stop"] += 1

    async def list_devices(self):
        self.counts["list"] += 1
        return await super().list_devices()

    async def read_state(self, device_id):
        self.counts["read"] += 1
        return await super().read_state(device_id)

    async def send_command(self, device_id, command, params):
        self.counts["send"] += 1
        await super().send_command(device_id, command, params)
        self.applied.set()
        if self.block:
            await self.release.wait()
        if self.lose_ack:
            raise DriverError("Synthetic acknowledgement loss after action applied")

    def physical_on(self):
        return self._devices[ID].clusters[c.ON_OFF].attributes["OnOff"]


class Core:
    def __init__(self, driver, *, offline=False, snapshot=None):
        self.driver = driver
        self.registry = DeviceRegistry([driver])
        self.copy = DigitalCopy(self.registry, [driver], offline=offline, snapshot_path=snapshot)
        self.router = CommandRouter(self.registry, IdempotencyStore(), digital_copy=self.copy,
                                    confirm_timeout_s=0.1, poll_interval_s=0.005)

    async def start(self):
        await self.copy.start()

    async def stop(self):
        await self.router.drain()
        await self.copy.stop()

    async def command(self, command="On", key="one"):
        return await self.router.execute(ID, CommandRequest(command=command, params={}, idempotency_key=key))


def run(scenario):
    return asyncio.run(asyncio.wait_for(scenario(), 5))


def test_readback_after_lost_ack():
    async def scenario():
        driver = TraceDriver()
        driver.lose_ack = True
        core = Core(driver)
        await core.start()
        try:
            response = await core.command()
            return response, driver
        finally:
            await core.stop()

    response, driver = run(scenario)
    # The read-back reflects what physically happened, never the state from before the command.
    assert response.state_after[c.ON_OFF].attributes["OnOff"] is driver.physical_on() is True
    assert driver.counts["send"] == 1 and driver.counts["read"] >= 2  # before, and after the send


def test_cancellation_retry_once():
    async def scenario():
        driver = TraceDriver()
        driver.block = True
        core = Core(driver)
        await core.start()
        try:
            first = asyncio.create_task(core.command("Toggle", "same-key"))
            await asyncio.wait_for(driver.applied.wait(), 1)
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            retry = asyncio.create_task(core.command("Toggle", "same-key"))
            driver.release.set()
            response = await retry
            return response, driver
        finally:
            driver.release.set()
            await core.stop()

    response, driver = run(scenario)
    assert driver.counts["send"] == 1
    assert response.state_after[c.ON_OFF].attributes["OnOff"] is True


def test_conflicting_serials():
    merged = merge_device_records([record("matter:lamp", ControlPath.MATTER, "SN-A"),
                                   record("google:lamp", ControlPath.GOOGLE_HOME, "SN-B")])
    assert len(merged) == 2


def test_cold_offline_no_driver_calls():
    async def scenario():
        driver = TraceDriver()
        core = Core(driver, offline=True)
        await core.start()
        try:
            devices = await core.registry.refresh()
            response = await core.command()
            return devices, response, driver.counts
        finally:
            await core.stop()

    devices, response, counts = run(scenario)
    assert devices and response.simulated
    assert counts == {"start": 0, "stop": 0, "list": 0, "read": 0, "send": 0}


def test_offline_transition_waits_for_live_confirmation():
    async def scenario():
        driver = TraceDriver()
        driver.block = True
        core = Core(driver)
        await core.start()
        try:
            action = asyncio.create_task(core.command())
            await asyncio.wait_for(driver.applied.wait(), 1)
            switch = asyncio.create_task(core.copy.set_offline(True))
            done, _ = await asyncio.wait({switch}, timeout=0.03)
            returned_early = bool(done)
            driver.release.set()
            response = await asyncio.wait_for(action, 1)
            await asyncio.wait_for(switch, 1)
            return returned_early, response, driver.counts
        finally:
            driver.release.set()
            await core.stop()

    returned_early, response, counts = run(scenario)
    assert not returned_early
    assert response.status == "done" and not response.simulated
    assert counts["stop"] == 1  # the driver is stopped once offline


def test_retry_provenance_survives_restart(tmp_path):
    snapshot = tmp_path / "copy.json"

    async def scenario():
        rehearsal = Core(TraceDriver(), snapshot=snapshot)  # a live run saves the home first
        await rehearsal.start()
        await rehearsal.stop()

        first = Core(TraceDriver(), offline=True, snapshot=snapshot)
        await first.start()
        original = await first.command("Toggle", "persisted-key")
        await first.stop()

        driver = TraceDriver()
        second = Core(driver, snapshot=snapshot)
        await second.start()
        try:
            replay = await second.command("Toggle", "persisted-key")
            return original, replay, driver
        finally:
            await second.stop()

    original, replay, driver = run(scenario)
    assert original.simulated and replay.simulated
    assert driver.counts["send"] == 0 and driver.physical_on() is False
