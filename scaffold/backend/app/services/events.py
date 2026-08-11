"""SSE fan-out backed by Postgres LISTEN/NOTIFY.

Why this design: the API is stateless and runs N replicas, but an SSE connection is
inherently pinned to whichever replica the client landed on. Every replica holds one
LISTEN connection, so a job updated by *any* replica or worker reaches *every*
connected client regardless of routing. No sticky sessions, no shared memory.

Where it stops scaling: one LISTEN connection per replica is fine at tens of replicas,
not thousands. The upgrade path is Valkey pub/sub (managed by DO), then Kafka if the
event log itself needs retention and consumer groups.
"""

from __future__ import annotations

import asyncio
import json

from app.core.db import raw_connection
from app.core.logging import get_logger

log = get_logger(__name__)

CHANNEL = "job_events"


class EventBroker:
    def __init__(self, max_queue: int = 100) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._conn = None
        self._max_queue = max_queue

    async def start(self) -> None:
        self._conn = await raw_connection()
        await self._conn.add_listener(CHANNEL, self._on_notify)
        log.info("events.listening", channel=CHANNEL)

    async def stop(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.remove_listener(CHANNEL, self._on_notify)
                await self._conn.close()
            finally:
                self._conn = None

    def _on_notify(self, _conn, _pid, _channel, payload: str) -> None:
        try:
            msg = json.loads(payload)
        except json.JSONDecodeError:
            log.warning("events.bad_payload", payload=payload[:200])
            return
        for q in list(self._subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # A slow client must not stall the listener or other clients. Dropping
                # is safe: the client resumes from its last seq on reconnect and the
                # event log backfills whatever it missed.
                log.warning("events.subscriber_slow_dropped")

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


broker = EventBroker()
