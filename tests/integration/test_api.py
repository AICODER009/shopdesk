import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopdesk.agent.dead_letters import InMemoryDeadLetterStore
from shopdesk.agent.handoff import InMemoryTicketSink
from shopdesk.agent.state import CustomerContext, TurnOutcome
from shopdesk.api.routes import router
from shopdesk.config import Settings
from shopdesk.integrations.zendesk.credentials import InMemoryCredentialStore, TokenCipher

JWT_SECRET = "widget-secret-for-tests-0123456789abcdef"
WEBHOOK_SECRET = "zd-webhook-secret"


class RecordingService:
    def __init__(self) -> None:
        self.sent: list[tuple[CustomerContext, str, str]] = []
        self.forgotten: list[tuple[CustomerContext, str]] = []
        self.resolved: list[tuple[str, int, str]] = []
        self.fail = False

    async def send(self, customer, conversation_id, text):
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append((customer, conversation_id, text))
        return TurnOutcome(status="answered", reply="hi")

    async def forget(self, customer, conversation_id):
        self.forgotten.append((customer, conversation_id))

    async def resolve_handoff(self, thread_id, ticket_id, status):
        self.resolved.append((thread_id, ticket_id, status))
        return True


@pytest.fixture
def ctx():
    settings = Settings(_env_file=None, widget_jwt_secret=JWT_SECRET, admin_token="adm1n",  # type: ignore[call-arg]
                        zendesk_webhook_secret=WEBHOOK_SECRET, zendesk_subdomain="acme")
    service = RecordingService()
    app = FastAPI()
    app.include_router(router)
    app.state.resources = SimpleNamespace(
        settings=settings, service=service, oauth=None,
        credentials=InMemoryCredentialStore(TokenCipher(None)),
        dead_letters=InMemoryDeadLetterStore(), ticket_sink=InMemoryTicketSink(),
    )
    return TestClient(app), service


def token(**claims):
    base = {"sub": "cus_1", "aud": "shopdesk-widget", "email": "a@b.co", "given_name": "Ana",
            "exp": datetime.now(UTC) + timedelta(minutes=5)}
    return jwt.encode(base | claims, JWT_SECRET, algorithm="HS256")


def test_guest_chat_gets_a_conversation_id(ctx):
    client, service = ctx
    r = client.post("/v1/chat", json={"message": "hello"})
    assert r.status_code == 200
    uuid.UUID(r.json()["conversation_id"])
    assert service.sent[0][0].signed_in is False


def test_signed_in_customer_comes_from_the_token(ctx):
    client, service = ctx
    r = client.post("/v1/chat", json={"message": "hi"},
                    headers={"Authorization": f"Bearer {token()}"})
    assert r.status_code == 200
    customer = service.sent[0][0]
    assert (customer.customer_id, customer.first_name) == ("cus_1", "Ana")


@pytest.mark.parametrize(
    "bad",
    [
        token(aud="someone-else"),
        token(exp=datetime.now(UTC) - timedelta(minutes=5)),
        jwt.encode({"sub": "x", "aud": "shopdesk-widget", "exp": 9999999999},
                   "wrong-key-0123456789abcdef0123", algorithm="HS256"),
        "not-a-jwt",
    ],
)
def test_bad_tokens_are_rejected(ctx, bad):
    client, _ = ctx
    r = client.post("/v1/chat", json={"message": "hi"}, headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401


def test_message_length_is_bounded(ctx):
    client, _ = ctx
    assert client.post("/v1/chat", json={"message": "x" * 4001}).status_code == 422
    assert client.post("/v1/chat", json={"message": ""}).status_code == 422


def test_internal_errors_do_not_leak(ctx):
    client, service = ctx
    service.fail = True
    r = client.post("/v1/chat", json={"message": "hi"})
    assert r.status_code == 503 and "boom" not in r.text


def test_erasure_endpoint(ctx):
    client, service = ctx
    cid = str(uuid.uuid4())
    r = client.delete(f"/v1/conversations/{cid}", headers={"Authorization": f"Bearer {token()}"})
    assert r.status_code == 204 and service.forgotten[0][1] == cid


def signed(body: dict, secret: str = WEBHOOK_SECRET, ts: datetime | None = None):
    raw = json.dumps(body).encode()
    stamp = (ts or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sig = base64.b64encode(hmac.new(secret.encode(), stamp.encode() + raw,
                                    hashlib.sha256).digest()).decode()
    return raw, {"X-Zendesk-Webhook-Signature": sig,
                 "X-Zendesk-Webhook-Signature-Timestamp": stamp,
                 "Content-Type": "application/json"}


def test_webhook_resumes_conversation_when_ticket_is_solved(ctx):
    client, service = ctx
    raw, headers = signed({"ticket_id": 42, "external_id": "abc:c1", "status": "Solved"})
    r = client.post("/v1/zendesk/webhook", content=raw, headers=headers)
    assert r.status_code == 202
    assert service.resolved == [("abc:c1", 42, "solved")]


def test_webhook_ignores_open_tickets(ctx):
    client, service = ctx
    raw, headers = signed({"ticket_id": 42, "external_id": "abc:c1", "status": "open"})
    assert client.post("/v1/zendesk/webhook", content=raw, headers=headers).status_code == 202
    assert service.resolved == []


@pytest.mark.parametrize(
    "kwargs", [{"secret": "wrong"}, {"ts": datetime.now(UTC) - timedelta(minutes=10)}]
)
def test_webhook_rejects_bad_or_replayed_signatures(ctx, kwargs):
    client, service = ctx
    raw, headers = signed({"ticket_id": 1, "external_id": "t", "status": "solved"}, **kwargs)
    assert client.post("/v1/zendesk/webhook", content=raw, headers=headers).status_code == 401
    assert service.resolved == []


def test_admin_endpoints_need_basic_auth(ctx):
    client, _ = ctx
    assert client.get("/v1/admin/dead-letters").status_code == 401
    assert client.get("/v1/admin/dead-letters", auth=("admin", "nope")).status_code == 401
    r = client.get("/v1/admin/dead-letters", auth=("admin", "adm1n"))
    assert r.status_code == 200 and r.json() == {}
    status = client.get("/v1/admin/zendesk/status", auth=("admin", "adm1n")).json()
    assert status["connected"] is False
    replay = client.post("/v1/admin/dead-letters/replay", auth=("admin", "adm1n"))
    assert replay.json() == {"delivered": 0, "failed": 0}


def test_oauth_routes_404_when_not_configured(ctx):
    client, _ = ctx
    assert client.get("/v1/zendesk/oauth/start", auth=("admin", "adm1n")).status_code == 404
    assert client.get("/v1/zendesk/oauth/callback?code=x&state=y").status_code == 404
