"""The hub's internal event emitter: every device change, in one format, for upstream subscribers.

The registry publishes here whenever a device's state, availability or Matter
events change, whichever platform reported it. The /events WebSocket (and,
later, the Engine's event stream) reads from a subscription.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from .models import Clusters, ControlPath, utcnow

log = logging.getLogger(__name__)

EventType = Literal["state_changed", "online_changed", "device_event", "command_executed", "command_simulated",
                    "mode_changed"]


class DeviceEvent(BaseModel):
    seq: int
    type: EventType
    # The device and the path that reported it. mode_changed is about the hub, so it has neither.
    device_id: str | None
    source: ControlPath | None
    at: datetime
    # state_changed: the attributes that changed, by cluster, and the whole state after.
    changed: dict[str, dict[str, Any]] | None = None
    clusters: Clusters | None = None
    # online_changed
    online: bool | None = None
    # device_event: a Matter event such as DoorLock LockOperation or BooleanState StateChange.
    cluster: str | None = None
    event: str | None = None
    # device_event payload; mode_changed: {"offline": bool}.
    data: dict[str, Any] | None = None
    # command_executed: one command run (or replayed) through the hub. clusters holds its state_after.
    command: str | None = None
    params: dict[str, Any] | None = None
    status: Literal["done", "failed"] | None = None
    latency_ms: int | None = None
    idempotency_key: str | None = None
    simulated: bool | None = None
    replayed: bool | None = None
    error: str | None = None
    # command_simulated: a dry run. clusters holds the predicted state, latency_ms the estimate,
    # error the reason it would fail. Nothing was sent to the device.
    would_succeed: bool | None = None


class SubscriberLagged(Exception):
    """The subscriber fell too far behind and its subscription ended.

    Events were going to be lost, and a lost leak alarm must not look like a
    quiet home. Read GET /devices for the current state, then subscribe again.
    """


_LAGGED = object()


class Subscription:
    """One subscriber's queue. A subscriber that falls behind is ended, never blocks the hub."""

    def __init__(self, bus: "EventBus", max_queue: int) -> None:
        self._bus = bus
        self._queue: asyncio.Queue[Any] = asyncio.Queue(max_queue)
        self.lagged = False

    def offer(self, event: DeviceEvent) -> None:
        if self.lagged:
            return
        if self._queue.full():
            # End the stream explicitly rather than drop events the subscriber would never know it missed.
            log.warning("an event subscriber fell %d events behind; ending its subscription", self._queue.qsize())
            self.lagged = True
            while not self._queue.empty():
                self._queue.get_nowait()
            self._queue.put_nowait(_LAGGED)
            self.close()
            return
        self._queue.put_nowait(event)

    async def get(self) -> DeviceEvent:
        item = await self._queue.get()
        if item is _LAGGED:
            self._queue.put_nowait(_LAGGED)  # every later read fails the same way
            raise SubscriberLagged("event subscriber fell behind; read GET /devices and subscribe again")
        return item

    def close(self) -> None:
        self._bus._subscriptions.discard(self)

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> DeviceEvent:
        return await self.get()

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class EventBus:
    def __init__(self, max_queue: int = 256) -> None:
        self._max_queue = max_queue
        self._subscriptions: set[Subscription] = set()
        self._seq = 0

    def subscribe(self) -> Subscription:
        """Events published from now on. Use as a context manager so the subscription is closed."""
        subscription = Subscription(self, self._max_queue)
        self._subscriptions.add(subscription)
        return subscription

    def publish(self, type: EventType, device_id: str | None, source: ControlPath | None,
                **fields: Any) -> DeviceEvent:
        self._seq += 1
        event = DeviceEvent(seq=self._seq, type=type, device_id=device_id, source=source, at=utcnow(), **fields)
        for subscription in list(self._subscriptions):
            subscription.offer(event)
        log.debug("event %s %s %s", event.seq, type, device_id)
        return event
