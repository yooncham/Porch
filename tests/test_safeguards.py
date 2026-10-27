"""Regression tests for command-execution safeguards and mapping fixes."""

import asyncio
import threading
from concurrent.futures import Future

import pytest
from fake_matter_server import FakeMatterServer, node

from porch_hub import clusters as c
from porch_hub.dedup import merge_device_records
from porch_hub.drivers import CommandRejected, DriverError, SimulatedMatterDriver
from porch_hub.drivers.matter import MatterDriver
from porch_hub.drivers.thinq import FakeThinQCloud, ThinQBinding, ThinQDriver
from porch_hub.drivers.thinq import mqtt as thinq_mqtt
from porch_hub.drivers.thinq.profiles import check_writable, laundry_clusters, robot_cleaner_clusters
from porch_hub.idempotency import IdempotencyConflict, IdempotencyStore
from porch_hub.models import CommandRequest, ControlPath, Device, DeviceType, utcnow
from porch_hub.registry import DeviceRegistry
from porch_hub.router import CommandRouter


def plug(id_: str = "p", path: ControlPath = ControlPath.MATTER, **fields) -> Device:
    return Device(id=id_, name=fields.pop("name", "Lamp"), room=fields.pop("room", "Office"),
                  device_type=DeviceType.ON_OFF_PLUG, online=True, primary_control_path=path,
                  clusters={c.ON_OFF: c.on_off(False)}, last_updated_at=utcnow(), **fields)


async def router_for(*drivers, timeout_s: float = 0.2) -> CommandRouter:
    registry = DeviceRegistry(list(drivers))
    await registry.refresh()
    return CommandRouter(registry, IdempotencyStore(), confirm_timeout_s=timeout_s, poll_interval_s=0.01)


def on(state) -> bool:
    return state["OnOff"].attributes["OnOff"]


# Read-back after a send error

class AckLost(SimulatedMatterDriver):
    """The device acts, then the acknowledgement is lost."""

    async def send_command(self, device_id, command, params):
        await super().send_command(device_id, command, params)
        raise DriverError("acknowledgement timed out")


class NoAck(SimulatedMatterDriver):
    """The request never arrived, but the platform cannot say so."""

    async def send_command(self, device_id, command, params):
        raise DriverError("acknowledgement timed out")


class Refuses(SimulatedMatterDriver):
    def check_allowed(self, device, command):
        raise CommandRejected("lock is jammed")


def test_a_lost_acknowledgement_is_settled_by_reading_back():
    async def scenario():
        acted, missed = AckLost([plug()]), NoAck([plug()])
        return (await (await router_for(acted)).execute("p", CommandRequest(command="On", idempotency_key="k")),
                await (await router_for(missed)).execute("p", CommandRequest(command="On", idempotency_key="k")))

    acted, missed = asyncio.run(scenario())
    assert (acted.status, on(acted.state_after), acted.error) == ("done", True, None)
    assert (missed.status, on(missed.state_after)) == ("failed", False)
    assert missed.error == "acknowledgement timed out"


def test_a_refusal_still_reports_fresh_state():
    driver = Refuses([plug()])

    async def scenario():
        router = await router_for(driver)
        driver.set_attribute("p", c.ON_OFF, "OnOff", True)  # changed at the device since the hub last looked
        return await router.execute("p", CommandRequest(command="Off", idempotency_key="k"))

    response = asyncio.run(scenario())
    assert (response.status, response.error, on(response.state_after)) == ("failed", "lock is jammed", True)


def test_no_state_is_reported_rather_than_stale_state():
    class Unreadable(AckLost):
        reads = 0

        async def read_state(self, device_id):
            Unreadable.reads += 1
            if Unreadable.reads > 1:  # the read before the command works; the ones after do not
                raise DriverError("connection lost")
            return await super().read_state(device_id)

    async def scenario():
        return await (await router_for(Unreadable([plug()]))).execute(
            "p", CommandRequest(command="On", idempotency_key="k"))

    response = asyncio.run(scenario())
    assert (response.status, response.state_after) == ("failed", {})
    assert response.error == "acknowledgement timed out; state read-back failed: connection lost"


# Idempotency under cancellation

class SlowAck(SimulatedMatterDriver):
    sends = 0

    async def send_command(self, device_id, command, params):
        SlowAck.sends += 1
        await super().send_command(device_id, command, params)
        await asyncio.sleep(0.1)


def test_a_cancelled_request_does_not_let_a_retry_run_the_command_again():
    SlowAck.sends = 0
    driver = SlowAck([plug()])

    async def scenario():
        router = await router_for(driver)
        request = CommandRequest(command="Toggle", idempotency_key="k")
        first = asyncio.create_task(router.execute("p", request))
        await asyncio.sleep(0.03)  # the toggle is out, the acknowledgement is not
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        retry = await router.execute("p", request)
        return retry, await driver.read_state("p")

    retry, state = asyncio.run(scenario())
    assert SlowAck.sends == 1
    assert retry.status == "done" and on(retry.state_after) is True and on(state) is True


def test_shutdown_waits_for_a_running_command():
    driver = SlowAck([plug()])

    async def scenario():
        router = await router_for(driver)
        task = asyncio.create_task(router.execute("p", CommandRequest(command="On", idempotency_key="k")))
        await asyncio.sleep(0.03)
        await router.drain()
        return task.done(), await task

    finished, response = asyncio.run(scenario())
    assert finished and response.status == "done"


def test_cached_responses_cannot_be_changed_by_a_caller():
    async def scenario():
        router = await router_for(SimulatedMatterDriver([plug()]))
        request = CommandRequest(command="On", idempotency_key="k")
        first = await router.execute("p", request)
        first.state_after.clear()
        return await router.execute("p", request)

    assert on(asyncio.run(scenario()).state_after) is True


# Deduplication conflicts

def test_different_serials_are_never_merged_by_name_and_room():
    merged = merge_device_records([plug("m", serial_number="SN-A"),
                                   plug("g", ControlPath.GOOGLE_HOME, serial_number="SN-B")])
    assert len(merged) == 2


def test_different_serials_are_never_merged_by_mac():
    merged = merge_device_records([plug("m", serial_number="SN-A", mac_address="aa:bb:cc:dd:ee:01"),
                                   plug("g", ControlPath.GOOGLE_HOME, serial_number="SN-B",
                                        mac_address="AABBCCDDEE01")])
    assert len(merged) == 2


def test_different_macs_are_never_merged_by_name_and_room():
    merged = merge_device_records([plug("m", mac_address="aa:bb:cc:dd:ee:01"),
                                   plug("g", ControlPath.GOOGLE_HOME, mac_address="aa:bb:cc:dd:ee:02")])
    assert len(merged) == 2


def test_a_serial_on_one_side_only_does_not_block_a_name_match():
    merged = merge_device_records([plug("m", serial_number="SN-A"), plug("g", ControlPath.GOOGLE_HOME)])
    assert [d.control_paths for d in merged] == [[ControlPath.MATTER, ControlPath.GOOGLE_HOME]]


# LG mapping

def test_unknown_lg_states_are_unknown_and_never_confirm_a_stop():
    assert laundry_clusters({"runState": {"currentState": "SOMETHING_NEW"}})["OperationalState"].attributes == {
        "OperationalState": "Unknown"}
    assert laundry_clusters({})["OperationalState"].attributes == {"OperationalState": "Unknown"}
    assert laundry_clusters({"runState": {"currentState": "RESERVED"}})["OperationalState"].attributes == {
        "OperationalState": "Stopped"}
    vacuum = robot_cleaner_clusters({"runState": {"currentState": "MAPPING"}})
    assert vacuum["RvcOperationalState"].attributes == {"OperationalState": "Unknown"}


def test_battery_is_reported_in_half_percents():
    assert c.power_source(80).attributes == {"BatPercentRemaining": 160}
    assert c.power_source(140).attributes == {"BatPercentRemaining": 200}


def test_profile_ranges_check_step_and_excluded_values():
    properties = {"timer": {"relativeHourToStart": {
        "type": "range", "mode": ["r", "w"], "value": {"w": {"min": 1, "max": 19, "step": 2, "except": [7]}}}}}

    def check(hours):
        check_writable(properties, {"timer": {"relativeHourToStart": hours}}, "Dryer")

    check(3)
    for bad, reason in [(4, "steps of 2"), (7, "does not accept"), (21, "from 1 to 19"), (True, "whole number")]:
        with pytest.raises(CommandRejected, match=reason):
            check(bad)


# Stale reads

@pytest.mark.parametrize("listed_first", [True, False], ids=["after-listing", "before-any-status"])
def test_a_slow_lg_read_does_not_undo_a_newer_mqtt_report(listed_first):
    async def scenario():
        cloud = FakeThinQCloud(latency_s=0.05)
        cloud.add_dryer("lg-dryer", "Dryer")
        driver = ThinQDriver(cloud, bindings={"lg-dryer": ThinQBinding("home-1/dryer", "Laundry")})
        await driver.start()
        if listed_first:
            await driver.list_devices()
        read = asyncio.create_task(driver.read_state("home-1/dryer"))
        await asyncio.sleep(0.01)  # LG has answered; the answer is still on its way
        cloud.machine_update("lg-dryer", {"runState": {"currentState": "DRYING"}})
        answered = await read
        await asyncio.gather(*driver._tasks)
        return answered, (await driver.list_devices())[0]

    answered, cached = asyncio.run(scenario())
    assert answered["OperationalState"].attributes["OperationalState"] == "Stopped"  # what LG said then
    assert cached.clusters["OperationalState"].attributes["OperationalState"] == "Running"  # the newer report


def test_a_slow_matter_read_does_not_undo_a_newer_report():
    async def scenario():
        server = FakeMatterServer([node(2, {"1/29/0": [{"deviceType": 67}], "1/69/0": False})])
        driver = MatterDriver(await server.start())
        await driver.start()
        server.read_delay_s = 0.1
        read = asyncio.create_task(driver.read_state("home-1/matter-2-1"))
        await asyncio.sleep(0.03)
        await server.set_attribute(2, "1/69/0", True)  # the leak report overtakes the read
        answered = await read
        cached = (await driver.list_devices())[0]
        await driver.stop()
        await server.stop()
        return answered, cached

    answered, cached = asyncio.run(scenario())
    assert answered["BooleanState"].attributes["StateValue"] is False
    assert cached.clusters["BooleanState"].attributes["StateValue"] is True


# LG MQTT transport

class FakeConnection:
    """awscrt's mqtt.Connection: every call returns a concurrent future, completed from another thread."""

    def __init__(self, **kwargs):
        self.callbacks = kwargs
        self.subscribed, self.unsubscribed, self.disconnected = [], [], False

    @staticmethod
    def _later(result, delay_s=0.0):
        future = Future()
        threading.Timer(delay_s, future.set_result, [result]).start()
        return future

    def connect(self):
        return self._later({"session_present": False}, delay_s=0.1)

    def subscribe(self, topic, qos, callback=None):
        self.subscribed.append(topic)
        return self._later({"packet_id": 1, "topic": topic, "qos": qos}), 1

    def unsubscribe(self, topic):
        self.unsubscribed.append(topic)
        return self._later({"packet_id": 2}), 2

    def disconnect(self):
        self.disconnected = True
        return self._later({})


class CertificateApi:
    deregistered = False

    async def async_post_client_certificate(self, payload, timeout=15):
        return {"result": {"certificatePem": "PEM", "subscriptions": ["app/clients/a", "app/clients/b"]}}

    async def async_delete_client_register(self, payload, timeout=15):
        CertificateApi.deregistered = True


def test_mqtt_adapter_connects_without_blocking_and_keeps_every_topic(monkeypatch):
    connections = []

    def build(**kwargs):
        connections.append(FakeConnection(**kwargs))
        return connections[-1]

    monkeypatch.setattr(thinq_mqtt.mqtt_connection_builder, "mtls_from_bytes", build)

    async def scenario():
        changes = []
        client = thinq_mqtt.AsyncThinQMQTTClient(CertificateApi(), "thinq-open-test", lambda *a, **k: None,
                                                 changes.append, operation_timeout_s=2)
        client.csr_str, client.mqtt_server = "CSR", "broker.example"
        client.bytes_private_key = client.bytes_root_ca = b"x"
        client._registered = True
        assert await client.issue_certificate()

        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        ticking = asyncio.create_task(ticker())
        await client.async_connect_mqtt()  # the broker takes 100 ms to answer
        ticking.cancel()
        connection = connections[0]
        first_subscriptions = list(connection.subscribed)

        # awscrt reports from its own thread: interrupted, then resumed without the session.
        threading.Thread(target=connection.callbacks["on_connection_interrupted"],
                         args=(connection, RuntimeError("wifi"))).start()
        await asyncio.sleep(0.05)
        threading.Thread(target=connection.callbacks["on_connection_resumed"],
                         args=(connection, 0, False)).start()
        for _ in range(100):
            if len(changes) >= 3:
                break
            await asyncio.sleep(0.01)
        resubscribed = connection.subscribed[len(first_subscriptions):]
        await client.async_disconnect()
        return ticks, first_subscriptions, changes, resubscribed, connection

    ticks, first, changes, resubscribed, connection = asyncio.run(scenario())
    assert ticks >= 5  # the loop kept running while the broker answered
    assert first == ["app/clients/a", "app/clients/b"]
    assert changes == [True, False, True]
    assert resubscribed == ["app/clients/a", "app/clients/b"]
    assert connection.unsubscribed == first and connection.disconnected
    assert CertificateApi.deregistered


# Switching offline while a command runs

class Gated(SimulatedMatterDriver):
    """Acts at once, then holds the acknowledgement until released."""

    def __init__(self, devices):
        super().__init__(devices)
        self.sends = self.starts = self.stops = 0
        self.applied, self.release = asyncio.Event(), asyncio.Event()

    async def start(self):
        self.starts += 1

    async def stop(self):
        self.stops += 1

    async def send_command(self, device_id, command, params):
        self.sends += 1
        await super().send_command(device_id, command, params)
        self.applied.set()
        await self.release.wait()


def hub_parts(driver, snapshot=None, offline=False):
    from porch_hub.digital_copy import DigitalCopy

    registry = DeviceRegistry([driver])
    copy = DigitalCopy(registry, [driver], offline=offline, snapshot_path=snapshot)
    router = CommandRouter(registry, IdempotencyStore(), confirm_timeout_s=0.2, poll_interval_s=0.01,
                           digital_copy=copy)
    return registry, copy, router


def test_going_offline_waits_for_a_running_command_then_stops_the_drivers():
    driver = Gated([plug()])

    async def scenario():
        registry, copy, router = hub_parts(driver)
        await copy.start()
        live = asyncio.create_task(router.execute("p", CommandRequest(command="On", idempotency_key="k1")))
        await driver.applied.wait()
        switch = asyncio.create_task(copy.set_offline(True))
        await asyncio.sleep(0.05)
        waited = not switch.done()
        # A command arriving during the switch goes to the copy, never to the driver.
        during = asyncio.create_task(router.execute("p", CommandRequest(command="Off", idempotency_key="k2")))
        driver.release.set()
        first, second = await live, await during
        await switch
        return waited, first, second, driver.sends, driver.stops

    waited, first, second, sends, stops = asyncio.run(scenario())
    assert waited
    assert (first.status, first.simulated) == ("done", False)
    assert (second.status, second.simulated) == ("done", True)
    assert (sends, stops) == (1, 1)


def test_coming_back_online_restarts_the_drivers_before_commands_reach_them():
    driver = Gated([plug()])
    driver.release.set()

    async def scenario():
        registry, copy, router = hub_parts(driver, offline=True)
        await copy.start()
        await copy.set_offline(False)
        response = await router.execute("p", CommandRequest(command="On", idempotency_key="k"))
        return driver.starts, response

    starts, response = asyncio.run(scenario())
    assert starts == 1 and (response.status, response.simulated) == ("done", False)


def test_the_matter_driver_reconnects_after_an_offline_round_trip():
    async def scenario():
        server = FakeMatterServer([node(3, {"1/29/0": [{"deviceType": 266}], "1/6/0": False})])
        driver = MatterDriver(await server.start(), reconnect_delay_s=0.05)
        registry, copy, router = hub_parts(driver)
        await copy.start()
        await copy.set_offline(True)
        await copy.set_offline(False)
        response = await router.execute("home-1/matter-3-1", CommandRequest(command="On", idempotency_key="k"))
        await copy.stop()
        await server.stop()
        return response, server.commands("device_command")

    response, sent = asyncio.run(scenario())
    assert (response.status, response.simulated) == ("done", False)
    assert len(sent) == 1


# Idempotency across restarts

def test_a_command_answered_offline_never_becomes_real_after_a_restart(tmp_path):
    snapshot = tmp_path / "copy.json"

    async def scenario():
        rehearsal = Gated([plug()])  # a live run saves the home to the digital copy
        _, copy, _ = hub_parts(rehearsal, snapshot)
        await copy.start()
        await copy.stop()

        first = Gated([plug()])
        _, copy, router = hub_parts(first, snapshot, offline=True)
        await copy.start()
        original = await router.execute("p", CommandRequest(command="Toggle", idempotency_key="k"))
        await copy.stop()

        second = Gated([plug()])
        second.release.set()
        _, copy, router = hub_parts(second, snapshot)
        await copy.start()
        retry = await router.execute("p", CommandRequest(command="Toggle", idempotency_key="k"))
        with pytest.raises(IdempotencyConflict):
            await router.execute("p", CommandRequest(command="Off", idempotency_key="k"))
        return original, retry, second.sends

    original, retry, sends = asyncio.run(scenario())
    assert original.simulated and retry == original
    assert sends == 0


def test_a_live_command_is_not_sent_again_after_a_restart(tmp_path):
    snapshot = tmp_path / "copy.json"

    async def scenario():
        sends = 0
        for _ in range(2):  # the hub restarts between the request and the Engine's retry
            driver = Gated([plug()])
            driver.release.set()
            _, copy, router = hub_parts(driver, snapshot)
            await copy.start()
            response = await router.execute("p", CommandRequest(command="Toggle", idempotency_key="k"))
            await router.drain()
            await copy.stop()
            sends += driver.sends
        return sends, response

    sends, response = asyncio.run(scenario())
    assert sends == 1 and response.status == "done"
