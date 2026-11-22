import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

Event = tuple[str, dict[str, Any]]


class Events:
    """One SSE connection per browser tab, shared across conversations."""

    def __init__(self):
        self._listeners: set[asyncio.Queue[Event | None]] = set()

    def send(self, event: str, payload: dict) -> None:
        for listener in tuple(self._listeners):
            try:
                listener.put_nowait((event, payload))
            except asyncio.QueueFull:
                # The next connection fetches durable snapshots from SQLite.
                self._listeners.discard(listener)
                listener.get_nowait()
                listener.put_nowait(None)

    def close(self) -> None:
        for listener in tuple(self._listeners):
            if listener.full():
                listener.get_nowait()
            listener.put_nowait(None)
        self._listeners.clear()

    async def listen(self) -> AsyncIterator[str]:
        listener: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=256)
        self._listeners.add(listener)
        try:
            yield "event: ready\ndata: {}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(listener.get(), timeout=10)
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if event is None:
                    return
                name, payload = event
                yield f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        finally:
            self._listeners.discard(listener)
