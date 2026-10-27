"""LG's ThinQMQTTClient with a transport fit for a long-running async hub (thinqconnect 1.0.14).

The SDK's own client has three problems for the hub:
- async_connect_mqtt waits for the broker with a blocking future.result(),
  freezing the hub's event loop while it connects;
- issue_certificate keeps only the first of the topics LG's certificate allows;
- async_disconnect never closes the native connection, and nothing
  resubscribes after a reconnect in which the broker lost the session.

This subclass keeps everything else the SDK does (route lookup, client
registration, CSR and certificate provisioning) and replaces the transport.
"""

import asyncio
import contextlib
import functools
import logging
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from awscrt import mqtt
from awsiot import mqtt_connection_builder
from thinqconnect.mqtt_client import ClientConnectionState, ThinQMQTTClient

log = logging.getLogger(__name__)

RESUBSCRIBE_RETRY_S = 5.0


class AsyncThinQMQTTClient(ThinQMQTTClient):
    def __init__(
        self,
        thinq_api: Any,
        client_id: str,
        on_message_received: Callable[..., None],
        on_connection_change: Callable[[bool], None],
        operation_timeout_s: float = 10.0,
    ) -> None:
        """on_message_received runs on awscrt's thread; on_connection_change runs on the hub's loop."""
        super().__init__(thinq_api, client_id, on_message_received)
        self._on_connection_change = on_connection_change
        self._operation_timeout_s = operation_timeout_s
        self._topics: tuple[str, ...] = ()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connection: mqtt.Connection | None = None
        self._registered = False
        self._closing = False
        self._restoring: asyncio.Task | None = None

    @property
    def topics(self) -> tuple[str, ...]:
        return self._topics or (self.topic_subscription,)

    async def async_prepare_mqtt(self) -> bool:
        self._registered = True  # registration is the SDK's first step; disconnect undoes it
        return await super().async_prepare_mqtt()

    async def issue_certificate(self) -> bool:
        response = await self._thinq_api.async_post_client_certificate(
            {"service-code": "SVC202", "csr": self.csr_str}, timeout=self._operation_timeout_s)
        result = response.get("result", {}) if isinstance(response, dict) else {}
        certificate, topics = result.get("certificatePem"), result.get("subscriptions")
        if not isinstance(certificate, str) or not certificate or not isinstance(topics, list):
            return False
        self._topics = tuple(dict.fromkeys(t for t in topics if isinstance(t, str) and t))
        if not self._topics:
            return False
        self.bytes_certificate = certificate.encode()
        self.topic_subscription = self._topics[0]
        return True

    async def async_connect_mqtt(self) -> None:
        self._loop = asyncio.get_running_loop()
        timeout_ms = int(self._operation_timeout_s * 1000)
        build = functools.partial(
            mqtt_connection_builder.mtls_from_bytes,
            endpoint=self.mqtt_server,
            cert_bytes=self.bytes_certificate,
            pri_key_bytes=self.bytes_private_key,
            ca_bytes=self.bytes_root_ca,
            client_id=self._client_id,
            clean_session=False,
            keep_alive_secs=30,
            tcp_connect_timeout_ms=timeout_ms,
            protocol_operation_timeout_ms=timeout_ms,
            reconnect_min_timeout_secs=1,
            reconnect_max_timeout_secs=30,
            on_connection_interrupted=self._interrupted,
            on_connection_resumed=self._resumed,
        )
        self._connection = await self._loop.run_in_executor(None, build)
        await self._wait(self._connection.connect())
        await self._subscribe_all()
        self._state = ClientConnectionState.CLIENT_CONNECTED
        self._on_connection_change(True)

    async def async_disconnect(self) -> None:
        self._closing = True
        if self._restoring is not None:
            self._restoring.cancel()
            await asyncio.gather(self._restoring, return_exceptions=True)
        if self._connection is not None:
            for topic in self.topics:
                with contextlib.suppress(Exception):
                    future, _ = self._connection.unsubscribe(topic)
                    await self._wait(future)
            with contextlib.suppress(Exception):
                await self._wait(self._connection.disconnect())
            self._connection = None
        self._state = ClientConnectionState.CLIENT_DISCONNECTED
        if self._registered:
            self._registered = False
            await asyncio.wait_for(self._on_disconnect(), self._operation_timeout_s)  # removes LG's client record

    async def _subscribe_all(self) -> None:
        for topic in self.topics:
            future, _ = self._connection.subscribe(topic=topic, qos=mqtt.QoS.AT_LEAST_ONCE,
                                                   callback=self._on_message_received)
            result = await self._wait(future)
            if result.get("qos") is None:
                raise ConnectionError(f"LG's MQTT broker refused the subscription to {topic}")

    async def _wait(self, future: Future) -> Any:
        return await asyncio.wait_for(asyncio.wrap_future(future), self._operation_timeout_s)

    # awscrt calls these on its own thread.

    def _interrupted(self, connection: Any, error: Exception, **kwargs: Any) -> None:
        log.warning("LG MQTT connection interrupted (%s); awscrt is reconnecting", error)
        self._state = ClientConnectionState.CLIENT_DISCONNECTED
        self._call_soon(self._on_connection_change, False)

    def _resumed(self, connection: Any, return_code: Any, session_present: bool, **kwargs: Any) -> None:
        self._call_soon(self._restore, session_present)

    def _call_soon(self, callback: Callable[..., None], *args: Any) -> None:
        if self._loop is not None and not self._closing and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(callback, *args)

    # Back on the hub's loop.

    def _restore(self, session_present: bool) -> None:
        if self._closing or (self._restoring is not None and not self._restoring.done()):
            return
        self._restoring = asyncio.create_task(self._resubscribe(session_present))

    async def _resubscribe(self, session_present: bool) -> None:
        while not self._closing:
            try:
                if not session_present:  # the broker forgot the subscriptions along with the session
                    await self._subscribe_all()
                self._state = ClientConnectionState.CLIENT_CONNECTED
                self._on_connection_change(True)
                return
            except Exception as exc:
                log.warning("could not resubscribe to LG MQTT (%s); retrying in %gs", exc, RESUBSCRIBE_RETRY_S)
                await asyncio.sleep(RESUBSCRIBE_RETRY_S)
