import asyncio
import base64
from typing import Protocol

import structlog

from shopdesk.integrations.zendesk.credentials import CredentialStore
from shopdesk.integrations.zendesk.models import ZendeskToken
from shopdesk.integrations.zendesk.oauth import ZendeskAuthError, ZendeskOAuth

log = structlog.get_logger()


class ZendeskAuth(Protocol):
    async def authorization(self) -> str: ...

    async def handle_unauthorized(self) -> bool:
        """Try to recover from a 401. True means the caller may retry once."""
        ...


class ApiTokenAuth:
    """Basic auth with an agent API token. Handy for local dev; prefer OAuth in production
    because the token is tied to a single agent and never expires."""

    def __init__(self, email: str, api_token: str) -> None:
        raw = f"{email}/token:{api_token}".encode()
        self._header = "Basic " + base64.b64encode(raw).decode()

    async def authorization(self) -> str:
        return self._header

    async def handle_unauthorized(self) -> bool:
        return False


class OAuthTokenAuth:
    def __init__(self, store: CredentialStore, oauth: ZendeskOAuth, key: str) -> None:
        self._store = store
        self._oauth = oauth
        self._key = key
        self._lock = asyncio.Lock()

    async def authorization(self) -> str:
        token = await self._token()
        return f"Bearer {token.access_token.get_secret_value()}"

    async def handle_unauthorized(self) -> bool:
        current = await self._store.load(self._key)
        if current is None or current.refresh_token is None:
            return False
        try:
            await self._refresh(current)
        except ZendeskAuthError:
            log.error("zendesk.refresh_failed", key=self._key)
            return False
        return True

    async def _token(self) -> ZendeskToken:
        token = await self._store.load(self._key)
        if token is None:
            raise ZendeskAuthError("Zendesk is not connected; run the OAuth flow first")
        if token.expiring() and token.refresh_token is not None:
            token = await self._refresh(token)
        return token

    async def _refresh(self, seen: ZendeskToken) -> ZendeskToken:
        async with self._lock:
            # Another coroutine may have refreshed while we waited; refresh tokens rotate,
            # so refreshing twice with the same one would lock us out.
            latest = await self._store.load(self._key)
            if latest is not None and latest.access_token != seen.access_token:
                return latest
            fresh = await self._oauth.refresh(seen)
            await self._store.save(self._key, fresh)
            log.info("zendesk.token_refreshed", key=self._key)
            return fresh
