"""Dead-letter queue for handoffs that couldn't reach Zendesk.

A failed handoff must never lose the customer's request. When the handoff node exhausts its
retries the ticket is parked here (encrypted: it contains the transcript) and the customer is
told the team will get back to them. `replay` re-sends with the original Idempotency-Key, so
a ticket that actually got created during the outage isn't duplicated.
"""

import argparse
import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

import structlog
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from shopdesk.agent.handoff import TicketSink
from shopdesk.integrations.zendesk.credentials import TokenCipher
from shopdesk.integrations.zendesk.models import HandoffTicket, TicketRef
from shopdesk.runtime import run

log = structlog.get_logger()

MAX_REPLAYS = 12


def next_attempt(attempts: int, now: datetime) -> datetime:
    return now + timedelta(minutes=min(2**attempts, 360))


@dataclass
class DeadLetter:
    id: str
    thread_id: str
    ticket: HandoffTicket
    error: str
    attempts: int = 0
    status: str = "pending"  # pending | delivered | dead
    followups: list[str] = field(default_factory=list)
    delivered: TicketRef | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    next_attempt_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class DeadLetterStore(Protocol):
    async def add(self, thread_id: str, ticket: HandoffTicket, error: str) -> str: ...

    async def due(self, limit: int = 50) -> list[DeadLetter]: ...

    async def mark_delivered(self, letter_id: str, ref: TicketRef) -> None: ...

    async def mark_failed(self, letter_id: str, error: str) -> None: ...

    async def add_followup(self, thread_id: str, text: str) -> bool: ...

    async def delivered_ticket(self, thread_id: str) -> TicketRef | None: ...

    async def delete_thread(self, thread_id: str) -> None: ...

    async def counts(self) -> dict[str, int]: ...


class InMemoryDeadLetterStore:
    def __init__(self) -> None:
        self.letters: dict[str, DeadLetter] = {}

    async def add(self, thread_id: str, ticket: HandoffTicket, error: str) -> str:
        letter = DeadLetter(str(uuid.uuid4()), thread_id, ticket, error)
        self.letters[letter.id] = letter
        return letter.id

    async def due(self, limit: int = 50) -> list[DeadLetter]:
        now = datetime.now(UTC)
        ready = [d for d in self.letters.values()
                 if d.status == "pending" and d.next_attempt_at <= now]
        return sorted(ready, key=lambda d: d.created_at)[:limit]

    async def mark_delivered(self, letter_id: str, ref: TicketRef) -> None:
        letter = self.letters[letter_id]
        letter.status, letter.delivered = "delivered", ref

    async def mark_failed(self, letter_id: str, error: str) -> None:
        letter = self.letters[letter_id]
        letter.attempts += 1
        letter.error = error
        letter.next_attempt_at = next_attempt(letter.attempts, datetime.now(UTC))
        if letter.attempts >= MAX_REPLAYS:
            letter.status = "dead"

    async def add_followup(self, thread_id: str, text: str) -> bool:
        for letter in self.letters.values():
            if letter.thread_id == thread_id and letter.status == "pending":
                letter.followups.append(text)
                return True
        return False

    async def delivered_ticket(self, thread_id: str) -> TicketRef | None:
        for letter in self.letters.values():
            if letter.thread_id == thread_id and letter.delivered:
                return letter.delivered
        return None

    async def delete_thread(self, thread_id: str) -> None:
        self.letters = {k: v for k, v in self.letters.items() if v.thread_id != thread_id}

    async def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for letter in self.letters.values():
            out[letter.status] = out.get(letter.status, 0) + 1
        return out


class PostgresDeadLetterStore:
    DDL = (
        """
        CREATE TABLE IF NOT EXISTS handoff_dead_letters (
            id               uuid PRIMARY KEY,
            thread_id        text NOT NULL,
            payload          bytea NOT NULL,
            followups        bytea,
            error            text NOT NULL,
            attempts         int NOT NULL DEFAULT 0,
            status           text NOT NULL DEFAULT 'pending',
            delivered        jsonb,
            created_at       timestamptz NOT NULL DEFAULT now(),
            next_attempt_at  timestamptz NOT NULL DEFAULT now()
        )
        """,
        "CREATE INDEX IF NOT EXISTS handoff_dead_letters_due "
        "ON handoff_dead_letters (next_attempt_at) WHERE status = 'pending'",
        "CREATE INDEX IF NOT EXISTS handoff_dead_letters_thread "
        "ON handoff_dead_letters (thread_id)",
    )

    def __init__(
        self, pool: AsyncConnectionPool[AsyncConnection[DictRow]], cipher: TokenCipher
    ) -> None:
        self._pool = pool
        self._cipher = cipher

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            for statement in self.DDL:
                await conn.execute(statement)

    def _seal(self, data: object) -> bytes:
        return self._cipher.encrypt(json.dumps(data).encode())

    def _open(self, blob: bytes | None) -> object:
        return json.loads(self._cipher.decrypt(bytes(blob))) if blob else None

    async def add(self, thread_id: str, ticket: HandoffTicket, error: str) -> str:
        letter_id = str(uuid.uuid4())
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO handoff_dead_letters (id, thread_id, payload, error) "
                "VALUES (%s, %s, %s, %s)",
                (letter_id, thread_id, self._seal(ticket.model_dump(mode="json")), error[:500]),
            )
        return letter_id

    async def due(self, limit: int = 50) -> list[DeadLetter]:
        async with self._pool.connection() as conn:
            # Claim rows with a short lease instead of holding row locks across the Zendesk
            # call; parallel workers skip what's already claimed, and a crashed worker's
            # lease simply expires.
            cur = await conn.execute(
                """
                UPDATE handoff_dead_letters
                SET next_attempt_at = now() + interval '5 minutes'
                WHERE id IN (
                    SELECT id FROM handoff_dead_letters
                    WHERE status = 'pending' AND next_attempt_at <= now()
                    ORDER BY created_at
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING id, thread_id, payload, followups, error, attempts, created_at,
                          next_attempt_at
                """,
                (limit,),
            )
            rows = await cur.fetchall()
        return [
            DeadLetter(
                id=str(r["id"]),
                thread_id=r["thread_id"],
                ticket=HandoffTicket.model_validate(self._open(r["payload"])),
                error=r["error"],
                attempts=r["attempts"],
                followups=list(self._open(r["followups"]) or []),  # type: ignore[call-overload]
                created_at=r["created_at"],
                next_attempt_at=r["next_attempt_at"],
            )
            for r in rows
        ]

    async def mark_delivered(self, letter_id: str, ref: TicketRef) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE handoff_dead_letters SET status = 'delivered', delivered = %s, "
                "payload = %s, followups = NULL WHERE id = %s",
                # Once delivered we keep only the ticket reference, not the transcript.
                (json.dumps(ref.model_dump()), self._seal({}), letter_id),
            )

    async def mark_failed(self, letter_id: str, error: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                UPDATE handoff_dead_letters
                SET attempts = attempts + 1,
                    error = %s,
                    next_attempt_at = now() + make_interval(mins => LEAST(power(2, attempts + 1),
                                                                         360)::int),
                    status = CASE WHEN attempts + 1 >= %s THEN 'dead' ELSE status END
                WHERE id = %s
                """,
                (error[:500], MAX_REPLAYS, letter_id),
            )

    async def add_followup(self, thread_id: str, text: str) -> bool:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT id, followups FROM handoff_dead_letters "
                "WHERE thread_id = %s AND status = 'pending' FOR UPDATE",
                (thread_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return False
            followups = list(self._open(row["followups"]) or [])  # type: ignore[call-overload]
            followups.append(text)
            await conn.execute(
                "UPDATE handoff_dead_letters SET followups = %s WHERE id = %s",
                (self._seal(followups), row["id"]),
            )
        return True

    async def delivered_ticket(self, thread_id: str) -> TicketRef | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT delivered FROM handoff_dead_letters "
                "WHERE thread_id = %s AND status = 'delivered' ORDER BY created_at DESC LIMIT 1",
                (thread_id,),
            )
            row = await cur.fetchone()
        return TicketRef.model_validate(row["delivered"]) if row else None

    async def delete_thread(self, thread_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("DELETE FROM handoff_dead_letters WHERE thread_id = %s",
                               (thread_id,))

    async def counts(self) -> dict[str, int]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT status, count(*) AS n FROM handoff_dead_letters GROUP BY status"
            )
            return {r["status"]: r["n"] for r in await cur.fetchall()}


async def replay(store: DeadLetterStore, sink: TicketSink, limit: int = 50) -> dict[str, int]:
    stats = {"delivered": 0, "failed": 0}
    for letter in await store.due(limit):
        ticket = letter.ticket
        if letter.followups:
            extra = "\n".join(f"Customer (while queued): {t}" for t in letter.followups)
            ticket = ticket.model_copy(
                update={"internal_note": f"{ticket.internal_note}\n\n{extra}"}
            )
        try:
            ref = await sink.create_ticket(ticket)
        except Exception as exc:  # noqa: BLE001 - every failure is a retry candidate here
            await store.mark_failed(letter.id, f"{type(exc).__name__}: {exc}")
            stats["failed"] += 1
            if letter.attempts + 1 >= MAX_REPLAYS:
                log.error("dead_letter.gave_up", letter_id=letter.id, thread_id=letter.thread_id)
            continue
        await store.mark_delivered(letter.id, ref)
        stats["delivered"] += 1
        log.info("dead_letter.delivered", letter_id=letter.id, ticket_id=ref.id)
    return stats


async def _main() -> None:
    from shopdesk.config import get_settings
    from shopdesk.observability import configure_logging
    from shopdesk.wiring import Resources

    parser = argparse.ArgumentParser(description="Replay handoffs parked in the dead-letter queue")
    parser.add_argument("--loop", type=int, default=0, help="seconds between runs; 0 = once")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings)
    async with Resources.open(settings) as res:
        while True:
            stats = await replay(res.dead_letters, res.ticket_sink)
            log.info("dead_letter.replay_run", **stats, **await res.dead_letters.counts())
            if not args.loop:
                break
            await asyncio.sleep(args.loop)


if __name__ == "__main__":
    run(_main())
