"""Hand conversations back to the assistant when their Zendesk ticket is solved.

The webhook (`POST /v1/zendesk/webhook`) is the fast path. This reconciler is the safety net:
webhooks get dropped, misconfigured, or can't reach the service at all (local dev, private
networks). It polls the tickets of open handoffs and resumes the ones a human has closed.
"""

import asyncio
from typing import Protocol

import structlog
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from shopdesk.agent.handoff import TicketSink
from shopdesk.integrations.http import UpstreamError

log = structlog.get_logger()

DONE_STATUSES = frozenset({"solved", "closed"})
BATCH = 100  # Zendesk show_many accepts up to 100 ids


class OpenHandoffStore(Protocol):
    async def add(self, thread_id: str, ticket_id: int) -> None: ...

    async def remove(self, thread_id: str) -> None: ...

    async def all(self) -> list[tuple[str, int]]: ...


class InMemoryOpenHandoffs:
    def __init__(self) -> None:
        self._open: dict[str, int] = {}

    async def add(self, thread_id: str, ticket_id: int) -> None:
        self._open[thread_id] = ticket_id

    async def remove(self, thread_id: str) -> None:
        self._open.pop(thread_id, None)

    async def all(self) -> list[tuple[str, int]]:
        return list(self._open.items())


class PostgresOpenHandoffs:
    DDL = """
        CREATE TABLE IF NOT EXISTS open_handoffs (
            thread_id   text PRIMARY KEY,
            ticket_id   bigint NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now()
        )
    """

    def __init__(self, pool: AsyncConnectionPool[AsyncConnection[DictRow]]) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(self.DDL)

    async def add(self, thread_id: str, ticket_id: int) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO open_handoffs (thread_id, ticket_id) VALUES (%s, %s) "
                "ON CONFLICT (thread_id) DO UPDATE SET ticket_id = EXCLUDED.ticket_id",
                (thread_id, ticket_id),
            )

    async def remove(self, thread_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("DELETE FROM open_handoffs WHERE thread_id = %s", (thread_id,))

    async def all(self) -> list[tuple[str, int]]:
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT thread_id, ticket_id FROM open_handoffs ORDER BY created_at"
            )).fetchall()
        return [(r["thread_id"], int(r["ticket_id"])) for r in rows]


class HandbackTarget(Protocol):
    async def resolve_handoff(self, thread_id: str, ticket_id: int, status: str) -> bool: ...


async def reconcile(
    target: HandbackTarget, tickets: TicketSink, store: OpenHandoffStore
) -> dict[str, int]:
    stats = {"checked": 0, "handed_back": 0}
    pending = await store.all()
    for i in range(0, len(pending), BATCH):
        batch = pending[i : i + BATCH]
        statuses = await tickets.ticket_statuses([ticket_id for _, ticket_id in batch])
        stats["checked"] += len(batch)
        for thread_id, ticket_id in batch:
            status = statuses.get(ticket_id)
            if status is None:
                # Deleted in Zendesk (or merged away): nothing will ever solve it.
                log.warning("handback.ticket_missing", ticket_id=ticket_id)
                await store.remove(thread_id)
            elif status in DONE_STATUSES:
                await target.resolve_handoff(thread_id, ticket_id, status)
                await store.remove(thread_id)
                stats["handed_back"] += 1
    return stats


async def poll_forever(
    target: HandbackTarget, tickets: TicketSink, store: OpenHandoffStore, every: float
) -> None:
    while True:
        try:
            stats = await reconcile(target, tickets, store)
            if stats["handed_back"]:
                log.info("handback.reconciled", **stats)
        except UpstreamError as exc:
            log.warning("handback.poll_failed", error=str(exc))
        except Exception:  # noqa: BLE001 - a poll loop must outlive any single bad run
            log.exception("handback.poll_crashed")
        await asyncio.sleep(every)
