"""SQLite storage for single-server deployments: one file, no database server.

Same contracts as the Postgres stores (credentials, dead letters, open handoffs); the
LangGraph checkpointer gets its own connection to the same file. WAL mode lets readers run
alongside the single writer; multi-statement updates go through one lock so they can't
interleave on the shared connection. For several app replicas, use Postgres.
"""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from shopdesk.agent.dead_letters import MAX_REPLAYS, DeadLetter
from shopdesk.integrations.zendesk.credentials import TokenCipher
from shopdesk.integrations.zendesk.models import HandoffTicket, TicketRef, ZendeskToken

LEASE_SECONDS = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS integration_credentials (
    key         TEXT PRIMARY KEY,
    ciphertext  BLOB NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS handoff_dead_letters (
    id               TEXT PRIMARY KEY,
    thread_id        TEXT NOT NULL,
    payload          BLOB NOT NULL,
    followups        BLOB,
    error            TEXT NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'pending',
    delivered        TEXT,
    created_at       REAL NOT NULL,
    next_attempt_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS handoff_dead_letters_due
    ON handoff_dead_letters (status, next_attempt_at);
CREATE INDEX IF NOT EXISTS handoff_dead_letters_thread ON handoff_dead_letters (thread_id);
CREATE TABLE IF NOT EXISTS open_handoffs (
    thread_id   TEXT PRIMARY KEY,
    ticket_id   INTEGER NOT NULL,
    created_at  REAL NOT NULL
);
"""


def sqlite_path(database_url: str) -> str:
    """`sqlite:///data/shopdesk.db` -> `data/shopdesk.db`; `sqlite:////abs/path` -> `/abs/path`."""
    return database_url.removeprefix("sqlite:///")


class SqliteDB:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn
        self.conn.row_factory = aiosqlite.Row
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: str) -> "SqliteDB":
        conn = await aiosqlite.connect(path)
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.executescript(SCHEMA)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            try:
                yield self.conn
            except BaseException:
                await self.conn.rollback()
                raise
            await self.conn.commit()

    async def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with self._lock, self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())


class SqliteCredentialStore:
    def __init__(self, db: SqliteDB, cipher: TokenCipher) -> None:
        self._db = db
        self._cipher = cipher

    async def load(self, key: str) -> ZendeskToken | None:
        rows = await self._db.fetchall(
            "SELECT ciphertext FROM integration_credentials WHERE key = ?", (key,)
        )
        if not rows:
            return None
        return ZendeskToken.model_validate(json.loads(self._cipher.decrypt(rows[0]["ciphertext"])))

    async def save(self, key: str, token: ZendeskToken) -> None:
        blob = self._cipher.encrypt(json.dumps(token.to_plain()).encode())
        async with self._db.tx() as conn:
            await conn.execute(
                "INSERT INTO integration_credentials (key, ciphertext, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (key) DO UPDATE SET ciphertext = excluded.ciphertext,"
                " updated_at = excluded.updated_at",
                (key, blob, time.time()),
            )

    async def delete(self, key: str) -> None:
        async with self._db.tx() as conn:
            await conn.execute("DELETE FROM integration_credentials WHERE key = ?", (key,))


class SqliteDeadLetterStore:
    def __init__(self, db: SqliteDB, cipher: TokenCipher) -> None:
        self._db = db
        self._cipher = cipher

    def _seal(self, data: object) -> bytes:
        return self._cipher.encrypt(json.dumps(data).encode())

    def _open(self, blob: bytes | None) -> Any:
        return json.loads(self._cipher.decrypt(blob)) if blob else None

    async def add(self, thread_id: str, ticket: HandoffTicket, error: str) -> str:
        letter_id = str(uuid.uuid4())
        now = time.time()
        async with self._db.tx() as conn:
            await conn.execute(
                "INSERT INTO handoff_dead_letters (id, thread_id, payload, error, created_at,"
                " next_attempt_at) VALUES (?, ?, ?, ?, ?, ?)",
                (letter_id, thread_id, self._seal(ticket.model_dump(mode="json")),
                 error[:500], now, now),
            )
        return letter_id

    async def due(self, limit: int = 50) -> list[DeadLetter]:
        now = time.time()
        # Claim with a lease (same idea as the Postgres store): a crashed worker's claim
        # simply expires and the letter becomes due again.
        async with self._db.tx() as conn, conn.execute(
            "UPDATE handoff_dead_letters SET next_attempt_at = ? WHERE id IN ("
            " SELECT id FROM handoff_dead_letters WHERE status = 'pending'"
            " AND next_attempt_at <= ? ORDER BY created_at LIMIT ?)"
            " RETURNING id, thread_id, payload, followups, error, attempts, created_at,"
            " next_attempt_at",
            (now + LEASE_SECONDS, now, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [
            DeadLetter(
                id=r["id"],
                thread_id=r["thread_id"],
                ticket=HandoffTicket.model_validate(self._open(r["payload"])),
                error=r["error"],
                attempts=r["attempts"],
                followups=list(self._open(r["followups"]) or []),
                created_at=datetime.fromtimestamp(r["created_at"], UTC),
                next_attempt_at=datetime.fromtimestamp(r["next_attempt_at"], UTC),
            )
            for r in sorted(rows, key=lambda r: r["created_at"])
        ]

    async def mark_delivered(self, letter_id: str, ref: TicketRef) -> None:
        async with self._db.tx() as conn:
            await conn.execute(
                "UPDATE handoff_dead_letters SET status = 'delivered', delivered = ?,"
                " payload = ?, followups = NULL WHERE id = ?",
                # Once delivered we keep only the ticket reference, not the transcript.
                (json.dumps(ref.model_dump()), self._seal({}), letter_id),
            )

    async def mark_failed(self, letter_id: str, error: str) -> None:
        async with self._db.tx() as conn:
            await conn.execute(
                "UPDATE handoff_dead_letters SET attempts = attempts + 1, error = ?,"
                " next_attempt_at = ? + 60 * MIN(1 << (attempts + 1), 360),"
                " status = CASE WHEN attempts + 1 >= ? THEN 'dead' ELSE status END"
                " WHERE id = ?",
                (error[:500], time.time(), MAX_REPLAYS, letter_id),
            )

    async def add_followup(self, thread_id: str, text: str) -> bool:
        async with self._db.tx() as conn:
            async with conn.execute(
                "SELECT id, followups FROM handoff_dead_letters"
                " WHERE thread_id = ? AND status = 'pending' LIMIT 1",
                (thread_id,),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return False
            followups = list(self._open(row["followups"]) or [])
            followups.append(text)
            await conn.execute(
                "UPDATE handoff_dead_letters SET followups = ? WHERE id = ?",
                (self._seal(followups), row["id"]),
            )
        return True

    async def delivered_ticket(self, thread_id: str) -> TicketRef | None:
        rows = await self._db.fetchall(
            "SELECT delivered FROM handoff_dead_letters WHERE thread_id = ?"
            " AND status = 'delivered' ORDER BY created_at DESC LIMIT 1",
            (thread_id,),
        )
        return TicketRef.model_validate_json(rows[0]["delivered"]) if rows else None

    async def delete_thread(self, thread_id: str) -> None:
        async with self._db.tx() as conn:
            await conn.execute("DELETE FROM handoff_dead_letters WHERE thread_id = ?",
                               (thread_id,))

    async def counts(self) -> dict[str, int]:
        rows = await self._db.fetchall(
            "SELECT status, count(*) AS n FROM handoff_dead_letters GROUP BY status"
        )
        return {r["status"]: r["n"] for r in rows}


class SqliteOpenHandoffs:
    def __init__(self, db: SqliteDB) -> None:
        self._db = db

    async def add(self, thread_id: str, ticket_id: int) -> None:
        async with self._db.tx() as conn:
            await conn.execute(
                "INSERT INTO open_handoffs (thread_id, ticket_id, created_at) VALUES (?, ?, ?)"
                " ON CONFLICT (thread_id) DO UPDATE SET ticket_id = excluded.ticket_id",
                (thread_id, ticket_id, time.time()),
            )

    async def remove(self, thread_id: str) -> None:
        async with self._db.tx() as conn:
            await conn.execute("DELETE FROM open_handoffs WHERE thread_id = ?", (thread_id,))

    async def all(self) -> list[tuple[str, int]]:
        rows = await self._db.fetchall(
            "SELECT thread_id, ticket_id FROM open_handoffs ORDER BY created_at"
        )
        return [(r["thread_id"], int(r["ticket_id"])) for r in rows]
