import asyncio
import logging
from typing import Any, Dict, Set

from fastapi import WebSocket

logger = logging.getLogger("SupportWebSocket")

COMPANY_ROLE = "company"
ADMIN_ROLE = "superadmin"


class SupportConnectionManager:
    """
    Keeps track of the live Support chat sockets so a saved message can be pushed
    straight to whoever is looking at that thread, and so each side can be told
    whether the other one is currently online.

    Two kinds of membership are tracked:

    * `_threads[company_id]` - everyone watching that company's thread, mapped to
      the role they are watching as, which is what makes "is the other side
      online?" answerable.
    * `_admins` - every connected Super Admin. A company's counterpart is Support
      as a whole rather than one person, so Support counts as online while any
      Super Admin has a socket open, even on somebody else's thread.

    This state lives in the process, which is the right shape for a normal
    long-running server. On a serverless host (Vercel) each request is its own
    short-lived invocation, so WebSockets never connect there at all and the
    client falls back to polling the REST endpoints - see app/api/support.py.
    """

    def __init__(self) -> None:
        self._threads: Dict[int, Dict[WebSocket, str]] = {}
        self._admins: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def join(self, company_id: int, websocket: WebSocket, role: str) -> None:
        async with self._lock:
            self._threads.setdefault(company_id, {})[websocket] = role
            if role == ADMIN_ROLE:
                self._admins.add(websocket)

    async def leave(self, company_id: int, websocket: WebSocket) -> None:
        async with self._lock:
            self._forget(company_id, websocket)

    def _forget(self, company_id: int | None, websocket: WebSocket) -> None:
        """Drop a socket everywhere. Callers must already hold the lock."""
        self._admins.discard(websocket)
        thread_ids = [company_id] if company_id is not None else list(self._threads)
        for thread_id in thread_ids:
            members = self._threads.get(thread_id)
            if not members:
                continue
            members.pop(websocket, None)
            if not members:
                self._threads.pop(thread_id, None)

    async def company_online(self, company_id: int) -> bool:
        async with self._lock:
            return any(role == COMPANY_ROLE for role in self._threads.get(company_id, {}).values())

    async def support_online(self) -> bool:
        async with self._lock:
            return bool(self._admins)

    async def other_side_online(self, company_id: int, my_role: str) -> bool:
        """Is the person on the far end of this thread reachable right now?"""
        if my_role == COMPANY_ROLE:
            return await self.support_online()
        return await self.company_online(company_id)

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
                    self._forget(None, socket)

    async def broadcast_to_thread(
        self,
        company_id: int,
        payload: Dict[str, Any],
        exclude: WebSocket | None = None,
    ) -> None:
        """Push to everyone watching one company's thread."""
        async with self._lock:
            targets = {s for s in self._threads.get(company_id, {}) if s is not exclude}
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

    async def broadcast_to_companies(
        self,
        payload: Dict[str, Any],
        exclude: WebSocket | None = None,
    ) -> None:
        """
        Push to every connected company, across all threads. Support going online
        or offline changes what every one of them should be showing.
        """
        async with self._lock:
            targets = {
                socket
                for members in self._threads.values()
                for socket, role in members.items()
                if role == COMPANY_ROLE and socket is not exclude
            }
        if targets:
            await self._send_to(targets, payload)


# One manager per process, shared by the WebSocket route and the REST routes so a
# message posted over HTTP still reaches a peer that is connected by WebSocket.
support_manager = SupportConnectionManager()
