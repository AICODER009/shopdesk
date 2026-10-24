"""Zendesk OAuth 2.0 authorization-code flow with PKCE and refresh tokens.

The `state` parameter is a Fernet token carrying the PKCE verifier and a nonce. That keeps
the flow stateless across replicas; the nonce is also set as an HttpOnly cookie on the
admin's browser and must match on callback, which is what stops login CSRF.
"""

import base64
import hashlib
import json
import secrets
from urllib.parse import urlencode

import httpx
from cryptography.fernet import InvalidToken

from shopdesk.config import Settings
from shopdesk.integrations.http import request_with_retries
from shopdesk.integrations.zendesk.credentials import TokenCipher
from shopdesk.integrations.zendesk.models import ZendeskToken

STATE_TTL_SECONDS = 600


class ZendeskAuthError(Exception):
    pass


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class ZendeskOAuth:
    def __init__(self, settings: Settings, cipher: TokenCipher, http: httpx.AsyncClient) -> None:
        if not (settings.zendesk_subdomain and settings.zendesk_client_id):
            raise ValueError("ZENDESK_SUBDOMAIN and ZENDESK_CLIENT_ID are required for OAuth")
        self._s = settings
        self._cipher = cipher
        self._http = http
        self._base = settings.zendesk_url

    def begin(self) -> tuple[str, str]:
        verifier, challenge = pkce_pair()
        nonce = secrets.token_urlsafe(24)
        state = self._cipher.encrypt(json.dumps({"n": nonce, "v": verifier}).encode()).decode()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._s.zendesk_client_id,
                "redirect_uri": self._s.zendesk_redirect_uri,
                "scope": self._s.zendesk_scopes,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self._base}/oauth/authorizations/new?{query}", nonce

    def _open_state(self, state: str, nonce: str | None) -> str:
        try:
            payload = json.loads(self._cipher.decrypt(state.encode(), ttl=STATE_TTL_SECONDS))
        except (InvalidToken, ValueError) as exc:
            raise ZendeskAuthError("invalid or expired state") from exc
        if not nonce or not secrets.compare_digest(payload["n"], nonce):
            raise ZendeskAuthError("state does not belong to this browser session")
        return str(payload["v"])

    async def complete(self, *, code: str, state: str, nonce: str | None) -> ZendeskToken:
        verifier = self._open_state(state, nonce)
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._s.zendesk_redirect_uri,
                "scope": self._s.zendesk_scopes,
                "code_verifier": verifier,
                "expires_in": self._s.zendesk_access_token_ttl,
                "refresh_token_expires_in": self._s.zendesk_refresh_token_ttl,
            }
        )

    async def refresh(self, token: ZendeskToken) -> ZendeskToken:
        if token.refresh_token is None:
            raise ZendeskAuthError("no refresh token; reconnect Zendesk")
        return await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": token.refresh_token.get_secret_value(),
                "expires_in": self._s.zendesk_access_token_ttl,
                "refresh_token_expires_in": self._s.zendesk_refresh_token_ttl,
            }
        )

    async def _token_request(self, body: dict[str, object]) -> ZendeskToken:
        secret = self._s.zendesk_client_secret
        body |= {
            "client_id": self._s.zendesk_client_id,
            "client_secret": secret.get_secret_value() if secret else None,
        }
        # Authorization codes and refresh tokens are single-use: never retry this blindly.
        resp = await request_with_retries(
            self._http, "POST", f"{self._base}/oauth/tokens", json=body, attempts=1
        )
        if resp.status_code in (400, 401, 403):
            raise ZendeskAuthError(f"token endpoint rejected the grant ({resp.status_code})")
        resp.raise_for_status()
        return ZendeskToken.from_token_response(resp.json())
