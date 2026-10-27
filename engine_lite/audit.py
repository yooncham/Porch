"""What the stand-in Engine decided, for people: terminal audit lines and the stage display feed."""

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from starlette.websockets import WebSocket, WebSocketDisconnect

COLORS = {"REQUEST": "36", "ALLOW": "32", "DENY": "31", "HOLD": "33", "APPROVED": "32", "DENIED": "31",
          "DRY RUN": "34", "DONE": "32", "FAILED": "31", "REFUSED": "31", "ERROR": "31"}


class Audit:
    """One line per decision and outcome, written to the terminal (stderr) and optionally a JSON-lines file.

    stderr, not stdout: when Claude Code runs this server over stdio, stdout carries the protocol.
    """

    def __init__(self, stream: TextIO = sys.stderr, file: Path | None = None) -> None:
        self._stream = stream
        self._file = file
        self._color = hasattr(stream, "isatty") and stream.isatty()
        self.lines: list[str] = []  # kept for tests and the /audit endpoint

    def log(self, kind: str, text: str, **data: Any) -> None:
        stamp = time.strftime("%H:%M:%S")
        label = f"{kind:<8}"
        if self._color and kind in COLORS:
            label = f"\033[{COLORS[kind]};1m{label}\033[0m"
        line = f"{stamp}  {label}  {text}"
        self.lines.append(f"{stamp}  {kind:<8}  {text}")
        print(line, file=self._stream, flush=True)
        if self._file is not None:
            with self._file.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"at": datetime.now(UTC).isoformat(), "kind": kind, "text": text, **data},
                                   default=str) + "\n")


class StageFeed:
    """The Engine side of the stage display contract (README, "Stage display"), over /ws/stage."""

    def __init__(self, agent: dict[str, Any]) -> None:
        self._agent = agent
        self._sockets: set[WebSocket] = set()

    async def endpoint(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._sockets.add(websocket)
        try:
            await websocket.send_json({"type": "agents", "agents": [self._agent]})
            while True:
                await websocket.receive_text()  # nothing to read; this notices the display going away
        except WebSocketDisconnect:
            pass
        finally:
            self._sockets.discard(websocket)

    async def send(self, message: dict[str, Any]) -> None:
        message = {"at": datetime.now(UTC).isoformat(), **message}
        for websocket in list(self._sockets):
            try:
                await websocket.send_json(message)
            except Exception:  # a display that went away must not break a command
                self._sockets.discard(websocket)

    async def audit(self, summary: str, outcome: str | None = None, **extra: Any) -> None:
        await self.send({"type": "audit", "agent_id": self._agent["id"], "summary": summary, "outcome": outcome, **extra})

    async def safety(self, kind: str, title: str, detail: str, **extra: Any) -> None:
        await self.send({"type": "safety", "kind": kind, "title": title, "detail": detail,
                         "agent_id": self._agent["id"], **extra})
