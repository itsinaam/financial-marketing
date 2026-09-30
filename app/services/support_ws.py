import asyncio
import logging
from typing import Any, Dict, Set

from fastapi import WebSocket

logger = logging.getLogger("SupportWebSocket")


class SupportConnectionManager:
    """
    Keeps track of the live Support chat sockets so a saved message can be pushed
    straight to whoever is looking at that thread.

    Two kinds of membership are tracked:

    * `_threads[company_id]` - everyone currently watching that company's thread,
      which is the company itself plus any Super Admin who has it open.
    * `_admins` - every connected Super Admin, so the inbox badge can be nudged
      even when they are not looking at the thread the message belongs to.

    This state lives in the process, which is the right shape for a normal
    long-running server. On a serverless host (Vercel) each request is its own
    short-lived invocation, so WebSockets never connect there at all and the
    client falls back to polling the REST endpoints - see app/api/support.py.
    """

    def __init__(self) -> None:
        self._threads: Dict[int, Set[WebSocket]] = {}
        self._admins: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def join_thread(self, company_id: int, websocket: WebSocket) -> None:
        async with self._lock:
            self._threads.setdefault(company_id, set()).add(websocket)

    async def leave_thread(self, company_id: int, websocket: WebSocket) -> None:
        async with self._lock:
            watchers = self._threads.get(company_id)
            if not watchers:
                return
            watchers.discard(websocket)
            if not watchers:
                self._threads.pop(company_id, None)

    async def join_admins(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._admins.add(websocket)

    async def leave_admins(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._admins.discard(websocket)

    async def _send_to(self, targets: Set[WebSocket], payload: Dict[str, Any]) -> None:
        """
        Send to every socket in the set, dropping the ones that have gone away.
        A single broken client must never stop the others from being delivered to.
        """
        dead: list[WebSocket] = []
        for socket in list(targets):
            try:
                await socket.send_json(payload)
            except Exception as err:  # noqa: BLE001 - a closed socket raises many shapes
                logger.info("Dropping a dead Support socket: %s", err)
                dead.append(socket)

        if dead:
            async with self._lock:
                for socket in dead:
                    self._admins.discard(socket)
                    for watchers in self._threads.values():
                        watchers.discard(socket)

    async def broadcast_to_thread(
        self,
        company_id: int,
        payload: Dict[str, Any],
        exclude: WebSocket | None = None,
    ) -> None:
        """Push to everyone watching one company's thread."""
        async with self._lock:
            targets = {s for s in self._threads.get(company_id, set()) if s is not exclude}
        if targets:
            await self._send_to(targets, payload)

    async def broadcast_to_admins(
        self,
        payload: Dict[str, Any],
        exclude: WebSocket | None = None,
    ) -> None:
        """
        Push to every connected Super Admin. Used for the inbox badge, so it is
        sent even to admins who do not have that thread open.
        """
        async with self._lock:
            targets = {s for s in self._admins if s is not exclude}
        if targets:
            await self._send_to(targets, payload)

    async def watchers_of(self, company_id: int) -> int:
        async with self._lock:
            return len(self._threads.get(company_id, set()))


# One manager per process, shared by the WebSocket route and the REST routes so a
# message posted over HTTP still reaches a peer that is connected by WebSocket.
support_manager = SupportConnectionManager()
