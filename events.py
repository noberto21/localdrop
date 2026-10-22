"""WebSocket fan-out hub used for real-time progress and status."""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import WebSocket

_CLOSE = object()


class _Client:
    def __init__(self, ws: WebSocket, kind: str, session: Optional[str]):
        self.ws, self.kind, self.session = ws, kind, session
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=300)


class Hub:
    def __init__(self) -> None:
        self.clients: set[_Client] = set()

    def publish(self, event: dict, *, session: Optional[str] = None, admin_only: bool = False) -> None:
        """Queue an event (safe to call from sync code on the event-loop thread).

        Admin clients get everything. Device clients only get events for their own
        session (or global events when `session` is None and not admin_only).
        """
        for c in list(self.clients):
            if c.kind == "admin" or (not admin_only and (session is None or c.session == session)):
                try:
                    c.queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass

    def close_devices(self, session: Optional[str] = None) -> None:
        for c in list(self.clients):
            if c.kind == "device" and (session is None or c.session == session):
                try:
                    c.queue.put_nowait(_CLOSE)
                except asyncio.QueueFull:
                    pass

    async def serve(self, ws: WebSocket, kind: str, session: Optional[str] = None) -> None:
        await ws.accept()
        client = _Client(ws, kind, session)
        self.clients.add(client)
        sender = asyncio.create_task(self._sender(client))
        try:
            while True:
                await ws.receive_text()      # keep-alive pings from the page
        except Exception:
            pass
        finally:
            sender.cancel()
            self.clients.discard(client)

    async def _sender(self, c: _Client) -> None:
        try:
            while True:
                ev = await c.queue.get()
                if ev is _CLOSE:
                    await c.ws.close(code=1000)
                    return
                await c.ws.send_json(ev)
        except Exception:
            return


hub = Hub()
