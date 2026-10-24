import json
from typing import Protocol

import structlog
from cryptography.fernet import Fernet
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from shopdesk.integrations.zendesk.models import ZendeskToken

log = structlog.get_logger()


class TokenCipher:
    def __init__(self, key: str | None) -> None:
        if key is None:
            log.warning("crypto.ephemeral_key", detail="TOKEN_ENCRYPTION_KEY unset; dev only")
            key = Fernet.generate_key().decode()
        self._fernet = Fernet(key.encode())

    def encrypt(self, data: bytes) -> bytes:
        return self._fernet.encrypt(data)

    def decrypt(self, token: bytes, ttl: int | None = None) -> bytes:
        return self._fernet.decrypt(token, ttl=ttl)


class CredentialStore(Protocol):
    async def load(self, key: str) -> ZendeskToken | None: ...

    async def save(self, key: str, token: ZendeskToken) -> None: ...

    async def delete(self, key: str) -> None: ...


class InMemoryCredentialStore:
    def __init__(self, cipher: TokenCipher) -> None:
        self._cipher = cipher
        self._rows: dict[str, bytes] = {}

    async def load(self, key: str) -> ZendeskToken | None:
        blob = self._rows.get(key)
        if blob is None:
            return None
        return ZendeskToken.model_validate(json.loads(self._cipher.decrypt(blob)))

    async def save(self, key: str, token: ZendeskToken) -> None:
        self._rows[key] = self._cipher.encrypt(json.dumps(token.to_plain()).encode())

    async def delete(self, key: str) -> None:
        self._rows.pop(key, None)


class PostgresCredentialStore:
    DDL = """
        CREATE TABLE IF NOT EXISTS integration_credentials (
            key         text PRIMARY KEY,
            ciphertext  bytea NOT NULL,
            updated_at  timestamptz NOT NULL DEFAULT now()
        )
    """

    def __init__(
        self, pool: AsyncConnectionPool[AsyncConnection[DictRow]], cipher: TokenCipher
    ) -> None:
        self._pool = pool
        self._cipher = cipher

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(self.DDL)

    async def load(self, key: str) -> ZendeskToken | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT ciphertext FROM integration_credentials WHERE key = %s", (key,)
            )
            row = await cur.fetchone()
        if row is None:
            return None
        blob = bytes(row["ciphertext"])
        return ZendeskToken.model_validate(json.loads(self._cipher.decrypt(blob)))

    async def save(self, key: str, token: ZendeskToken) -> None:
        blob = self._cipher.encrypt(json.dumps(token.to_plain()).encode())
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO integration_credentials (key, ciphertext) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET ciphertext = EXCLUDED.ciphertext, updated_at = now()
                """,
                (key, blob),
            )

    async def delete(self, key: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("DELETE FROM integration_credentials WHERE key = %s", (key,))
