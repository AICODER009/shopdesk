import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from cryptography.fernet import Fernet

from shopdesk.config import Settings
from shopdesk.integrations.commerce import HttpCommerceClient
from shopdesk.integrations.http import UpstreamError, request_with_retries
from shopdesk.integrations.zendesk.auth import OAuthTokenAuth
from shopdesk.integrations.zendesk.client import ZendeskClient
from shopdesk.integrations.zendesk.credentials import InMemoryCredentialStore, TokenCipher
from shopdesk.integrations.zendesk.models import HandoffTicket, ZendeskToken
from shopdesk.integrations.zendesk.oauth import ZendeskAuthError, ZendeskOAuth

ZD = "https://acme.zendesk.com"


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


# -- retries -------------------------------------------------------------------------------

@respx.mock
async def test_get_retries_5xx_then_succeeds(http):
    route = respx.get("https://api.test/x").mock(
        side_effect=[httpx.Response(503), httpx.Response(502), httpx.Response(200)]
    )
    resp = await request_with_retries(http, "GET", "https://api.test/x", sleep=no_sleep)
    assert resp.status_code == 200 and route.call_count == 3


@respx.mock
async def test_post_without_idempotency_key_is_not_retried_on_5xx(http):
    route = respx.post("https://api.test/x").mock(return_value=httpx.Response(503))
    resp = await request_with_retries(http, "POST", "https://api.test/x", sleep=no_sleep)
    assert resp.status_code == 503 and route.call_count == 1


@respx.mock
async def test_post_is_retried_on_429_and_honours_retry_after(http):
    respx.post("https://api.test/x").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(201)]
    )
    waits: list[float] = []

    async def sleep(s: float) -> None:
        waits.append(s)

    resp = await request_with_retries(http, "POST", "https://api.test/x", sleep=sleep)
    assert resp.status_code == 201 and waits == [2.0]


@respx.mock
async def test_long_retry_after_fails_fast(http):
    route = respx.get("https://api.test/x").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "120"})
    )
    resp = await request_with_retries(http, "GET", "https://api.test/x", sleep=no_sleep)
    assert resp.status_code == 429 and route.call_count == 1


@respx.mock
async def test_connect_errors_raise_upstream_error_after_attempts(http):
    respx.get("https://api.test/x").mock(side_effect=httpx.ConnectError("nope"))
    with pytest.raises(UpstreamError):
        await request_with_retries(http, "GET", "https://api.test/x", attempts=2,
                                   sleep=no_sleep)


# -- Zendesk OAuth ---------------------------------------------------------------------------

@pytest.fixture
def zd_settings() -> Settings:
    return Settings(_env_file=None, zendesk_subdomain="acme", zendesk_client_id="shopdesk",  # type: ignore[call-arg]
                    zendesk_client_secret="s3cret")


@pytest.fixture
def cipher() -> TokenCipher:
    return TokenCipher(Fernet.generate_key().decode())


def test_authorize_url_uses_pkce_and_opaque_state(zd_settings, cipher, http):
    url, nonce = ZendeskOAuth(zd_settings, cipher, http).begin()
    q = parse_qs(urlparse(url).query)
    assert url.startswith(f"{ZD}/oauth/authorizations/new?")
    assert q["code_challenge_method"] == ["S256"]
    assert nonce not in q["state"][0]
    assert len(q["code_challenge"][0]) == 43


@respx.mock
async def test_callback_exchanges_code_with_matching_verifier(zd_settings, cipher, http):
    oauth = ZendeskOAuth(zd_settings, cipher, http)
    url, nonce = oauth.begin()
    q = parse_qs(urlparse(url).query)
    route = respx.post(f"{ZD}/oauth/tokens").mock(return_value=httpx.Response(200, json={
        "access_token": "at1", "refresh_token": "rt1", "scope": "read", "expires_in": 3600,
    }))

    token = await oauth.complete(code="c0de", state=q["state"][0], nonce=nonce)

    body = json.loads(route.calls[0].request.content)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(body["code_verifier"].encode()).digest()
    ).rstrip(b"=").decode()
    assert challenge == q["code_challenge"][0]
    assert body["grant_type"] == "authorization_code"
    assert token.access_token.get_secret_value() == "at1"
    assert token.expires_at is not None


async def test_callback_from_another_browser_is_rejected(zd_settings, cipher, http):
    oauth = ZendeskOAuth(zd_settings, cipher, http)
    url, _ = oauth.begin()
    state = parse_qs(urlparse(url).query)["state"][0]
    with pytest.raises(ZendeskAuthError):
        await oauth.complete(code="c", state=state, nonce="someone-elses")
    with pytest.raises(ZendeskAuthError):
        await oauth.complete(code="c", state="garbage", nonce="x")


async def test_tokens_are_encrypted_at_rest(cipher):
    store = InMemoryCredentialStore(cipher)
    await store.save("acme", ZendeskToken(access_token="plain-secret"))
    assert b"plain-secret" not in store._rows["acme"]
    assert (await store.load("acme")).access_token.get_secret_value() == "plain-secret"


def expired_token() -> ZendeskToken:
    return ZendeskToken(access_token="old", refresh_token="rt-old",
                        expires_at=datetime.now(UTC) - timedelta(minutes=1))


@respx.mock
async def test_concurrent_requests_refresh_once(zd_settings, cipher, http):
    store = InMemoryCredentialStore(cipher)
    await store.save("acme", expired_token())
    route = respx.post(f"{ZD}/oauth/tokens").mock(return_value=httpx.Response(200, json={
        "access_token": "new", "refresh_token": "rt-new", "expires_in": 3600,
    }))
    auth = OAuthTokenAuth(store, ZendeskOAuth(zd_settings, cipher, http), "acme")

    import asyncio
    headers = await asyncio.gather(*(auth.authorization() for _ in range(5)))

    assert set(headers) == {"Bearer new"}
    assert route.call_count == 1


@respx.mock
async def test_ticket_create_sends_idempotency_key_and_recovers_from_401(
    zd_settings, cipher, http
):
    store = InMemoryCredentialStore(cipher)
    await store.save("acme", ZendeskToken(access_token="stale", refresh_token="rt"))
    respx.post(f"{ZD}/oauth/tokens").mock(return_value=httpx.Response(200, json={
        "access_token": "fresh", "refresh_token": "rt2", "expires_in": 3600,
    }))
    tickets = respx.post(f"{ZD}/api/v2/tickets.json").mock(side_effect=[
        httpx.Response(401),
        httpx.Response(201, json={"ticket": {"id": 42, "requester_id": 7}}),
    ])
    client = ZendeskClient(ZD, OAuthTokenAuth(store, ZendeskOAuth(zd_settings, cipher, http),
                                                  "acme"), http)

    ref = await client.create_ticket(HandoffTicket(
        conversation_id="t1", subject="s", internal_note="n", requester_email="a@b.co",
        idempotency_key="k1",
    ))

    assert ref.id == 42 and ref.requester_id == 7
    second = tickets.calls[1].request
    assert second.headers["Authorization"] == "Bearer fresh"
    assert second.headers["Idempotency-Key"] == "k1"
    payload = json.loads(second.content)["ticket"]
    assert payload["comment"]["public"] is False
    assert payload["external_id"] == "t1"


# -- commerce ----------------------------------------------------------------------------------

ORDER = {
    "order_id": "1", "customer_id": "cus_a", "status": "shipped",
    "placed_at": "2026-10-01T10:00:00", "total": {"amount": "10.00", "currency": "EUR"},
    "items": [], "shipping_address": {"line1": "Secret St 1"},
}


@respx.mock
async def test_commerce_drops_orders_owned_by_someone_else(http):
    respx.get("https://shop.test/customers/cus_b/orders/1").mock(
        return_value=httpx.Response(200, json=ORDER)
    )
    client = HttpCommerceClient("https://shop.test", "key", http)
    assert await client.get_order("cus_b", "1") is None


@respx.mock
async def test_commerce_404_is_none_and_model_view_hides_address(http):
    respx.get("https://shop.test/customers/cus_a/orders/2").mock(return_value=httpx.Response(404))
    respx.get("https://shop.test/customers/cus_a/orders/1").mock(
        return_value=httpx.Response(200, json=ORDER)
    )
    client = HttpCommerceClient("https://shop.test", "key", http)
    assert await client.get_order("cus_a", "2") is None
    view = (await client.get_order("cus_a", "1")).for_model()
    assert "shipping_address" not in view and "customer_id" not in view
