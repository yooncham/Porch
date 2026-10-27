"""Idempotency keys: one Engine command runs at most once, however often it is retried.

Each key's first request starts the command as its own task. Retries with the
key, during or after the run, wait for that task and get its response. The
task is shielded from the requests themselves: an HTTP client that gives up,
or a cancelled request, cannot stop a command halfway through its read-back,
and the retry that follows cannot send it a second time.

In-memory and single-process. Move to SQLite with the device registry before
running more than one hub worker.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .models import CommandResponse

log = logging.getLogger(__name__)


class IdempotencyConflict(Exception):
    """The key was already used for a different device, command or params."""


def fingerprint(device_id: str, command: str, params: dict[str, Any]) -> str:
    return json.dumps({"device_id": device_id, "command": command, "params": params},
                      sort_keys=True, default=str)


@dataclass
class _Entry:
    fingerprint: str
    task: asyncio.Task[CommandResponse]
    started_at: float


class IdempotencyStore:
    def __init__(self, ttl_s: float = 24 * 3600) -> None:
        self._ttl_s = ttl_s
        self._entries: dict[str, _Entry] = {}

    async def run(self, key: str, fp: str,
                  operation: Callable[[], Awaitable[CommandResponse]]) -> tuple[CommandResponse, bool]:
        """The key's response, running operation only the first time. Returns (response, replayed).

        An operation that raises (unknown device, invalid params) leaves the
        key unused, so a corrected request can reuse it.
        """
        self._evict_expired()
        entry = self._entries.get(key)
        replayed = entry is not None
        if entry is None:
            entry = _Entry(fp, asyncio.create_task(operation()), time.monotonic())
            self._entries[key] = entry
            entry.task.add_done_callback(lambda task: self._forget_if_failed(key, task))
        elif entry.fingerprint != fp:
            raise IdempotencyConflict(f"idempotency_key {key!r} was already used for a different command")
        response = await asyncio.shield(entry.task)
        return response.model_copy(deep=True), replayed

    async def drain(self) -> None:
        """Wait for commands still running, so shutdown never cuts one off mid read-back."""
        pending = [e.task for e in self._entries.values() if not e.task.done()]
        if pending:
            log.info("waiting for %d command(s) to finish before shutdown", len(pending))
            await asyncio.gather(*pending, return_exceptions=True)

    def _forget_if_failed(self, key: str, task: asyncio.Task) -> None:
        if task.cancelled() or task.exception() is not None:
            entry = self._entries.get(key)
            if entry is not None and entry.task is task:
                del self._entries[key]

    def _evict_expired(self) -> None:
        cutoff = time.monotonic() - self._ttl_s
        for key in [k for k, e in self._entries.items() if e.task.done() and e.started_at < cutoff]:
            del self._entries[key]
