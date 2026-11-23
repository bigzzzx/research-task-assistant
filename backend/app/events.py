import asyncio
import json
from collections.abc import AsyncIterator
from time import time
from typing import Any

Event = tuple[str, dict[str, Any]]


class Events:
    """An event stream is bound to a verified account and browser session."""

    def __init__(self):
        self._listeners: dict[asyncio.Queue[Event | None], tuple[str, str]] = {}

    def send(self, owner_id: str, event: str, payload: dict) -> None:
        for listener, (subscribed_owner, _) in tuple(self._listeners.items()):
            if subscribed_owner != owner_id:
                continue
            try:
                listener.put_nowait((event, payload))
            except asyncio.QueueFull:
                # The next connection fetches durable snapshots from SQLite.
                self._disconnect(listener)

    def _disconnect(self, listener: asyncio.Queue[Event | None]) -> None:
        self._listeners.pop(listener, None)
        if listener.full():
            listener.get_nowait()
        listener.put_nowait(None)

    def disconnect(self, token_hash: str) -> None:
        for listener, (_, subscribed_token) in tuple(self._listeners.items()):
            if subscribed_token == token_hash:
                self._disconnect(listener)

    def close(self) -> None:
        for listener in tuple(self._listeners):
            self._disconnect(listener)

    async def listen(self, owner_id: str, token_hash: str, expires_at: int) -> AsyncIterator[str]:
        if time() >= expires_at:
            return
        listener: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=256)
        self._listeners[listener] = (owner_id, token_hash)
        try:
            yield "event: ready\ndata: {}\n\n"
            while listener in self._listeners and (remaining := expires_at - time()) > 0:
                try:
                    event = await asyncio.wait_for(listener.get(), timeout=min(10, remaining))
                except TimeoutError:
                    if listener in self._listeners and time() < expires_at:
                        yield ": heartbeat\n\n"
                    continue
                if event is None or listener not in self._listeners or time() >= expires_at:
                    return
                name, payload = event
                yield f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        finally:
            self._listeners.pop(listener, None)
