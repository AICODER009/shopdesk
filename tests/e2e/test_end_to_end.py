"""The whole service over HTTP: real FastAPI app, real Zendesk/shop HTTP clients, OAuth,
webhook auth and the LangGraph flow. Only the model is scripted, and outbound HTTP goes to
the stand-ins in standins.py instead of the internet."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import jwt
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from shopdesk.agent.handback import reconcile
from shopdesk.config import Settings
from shopdesk.demo import DEMO_CUSTOMER
from shopdesk.main import create_app
from shopdesk.wiring import Resources
from tests.e2e.standins import FakeShop, FakeZendesk, HostRouter
from tests.fakes import ScriptedModel, call, final

JWT_SECRET = "e2e-widget-secret-0123456789abcdef"
ADMIN = ("admin", "adm1n")
def cid(label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"shopdesk-e2e/{label}"))


REFUND = {"reason": "refund_or_cancellation", "priority": "normal", "contact_email": "",
          "summary": "Jacket from order 100198 ripped after the return window; wants a refund."}


@dataclass
class Stack:
    client: TestClient
    zendesk: FakeZendesk
    shop: FakeShop
    model: ScriptedModel

    def chat(self, message: str, conversation: str | None = None) -> dict:
        token = jwt.encode(
            {"sub": DEMO_CUSTOMER, "email": "alex@example.com", "given_name": "Alex",
             "aud": "shopdesk-widget", "exp": datetime.now(UTC) + timedelta(minutes=5)},
            JWT_SECRET, algorithm="HS256",
        )
        conversation_id = cid(conversation) if conversation else None
        resp = self.client.post(
            "/v1/chat",
            json={"message": message, "conversation_id": conversation_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def last_tool_result(self) -> str:
        return self.model.calls[-1]["messages"][-1]["results"][0]["content"]

    def connect_zendesk(self) -> None:
        start = self.client.get("/v1/zendesk/oauth/start", auth=ADMIN, follow_redirects=False)
        assert start.status_code == 302
        authorize = urlsplit(start.headers["location"])
        assert authorize.netloc == "acme.zendesk.test"

        # The admin approves in Zendesk, which redirects back to our callback.
        consent = TestClient(self.zendesk.app).get(
            f"{authorize.path}?{authorize.query}", follow_redirects=False
        )
        callback = urlsplit(consent.headers["location"])
        done = self.client.get(f"{callback.path}?{callback.query}")
        assert done.json()["status"] == "connected", done.text


@contextmanager
def running(*script) -> Iterator[Stack]:
    zendesk = FakeZendesk("shopdesk", "client-s3cret", "webhook-s3cret")
    shop = FakeShop("shop-key")
    model = ScriptedModel(*script)
    router = HostRouter({"acme.zendesk.test": zendesk.app, "shop.test": shop.app})
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        env="dev",
        qdrant_url=":memory:",
        embedding_provider="local",
        langfuse_public_key=None,
        admin_token="adm1n",
        widget_jwt_secret=JWT_SECRET,
        token_encryption_key=Fernet.generate_key().decode(),
        pseudonym_key="e2e-pseudonym-key",
        zendesk_subdomain="acme",
        zendesk_base_url="https://acme.zendesk.test",
        zendesk_client_id="shopdesk",
        zendesk_client_secret="client-s3cret",
        zendesk_redirect_uri="http://testserver/v1/zendesk/oauth/callback",
        zendesk_webhook_secret="webhook-s3cret",
        commerce_api_url="https://shop.test/api",
        commerce_api_key="shop-key",
        retry_initial_interval=0.01,
        handoff_attempts=3,
    )
    app = create_app(settings, lambda s: Resources.open(s, transport=router, agent_model=model))
    with TestClient(app) as client:
        yield Stack(client, zendesk, shop, model)


def test_full_lifecycle_connect_lookup_handoff_takeover_handback_refresh():
    with running(
        call("get_order", {"order_id": "100245"}),
        final("It's on its way with DHL, due 27 October.", grounded="account_data"),
        call("handoff_to_human", REFUND),
        final("Glad that's sorted!"),
        call("handoff_to_human", REFUND | {"summary": "Second issue: wrong size."}, "call_2"),
    ) as s:
        s.connect_zendesk()
        status = s.client.get("/v1/admin/zendesk/status", auth=ADMIN).json()
        assert status["connected"] and status["refresh_expires_at"]

        # 1. Live order lookup through the shop API, scoped to the token's customer.
        out = s.chat("where is order 100245?", "c1")
        assert out["status"] == "answered"
        assert ("/api/customers/cus_demo_1/orders/100245", "Bearer shop-key") in s.shop.requests
        assert "JD014600006281934455" in s.last_tool_result()
        assert "Hauptstr" not in s.last_tool_result()

        # 2. Handoff creates a real ticket with the OAuth token and an idempotency key.
        out = s.chat("the jacket from 100198 ripped, I want my money back", "c1")
        assert out["status"] == "handed_off" and out["ticket_id"] == 1
        ticket = s.zendesk.tickets[1]
        assert ticket["requester"] == {"email": "alex@example.com", "name": "Alex"}
        assert ticket["comments"][0]["public"] is False
        assert "Orders discussed: 100245" in ticket["comments"][0]["body"]
        assert "reason_refund_or_cancellation" in ticket["tags"]
        assert ticket["external_id"].endswith(f":{cid('c1')}")
        assert s.zendesk.ticket_requests[0]["idempotency-key"]

        # 3. While a human owns it, messages land on the ticket, not the model.
        calls_before = len(s.model.calls)
        out = s.chat("also the box was damaged", "c1")
        assert out["status"] == "handed_off"
        assert s.zendesk.tickets[1]["comments"][-1] == {
            "body": "also the box was damaged", "public": True, "author_id": 501,
        }
        assert len(s.model.calls) == calls_before

        # 4. Agent solves the ticket; Zendesk's signed webhook hands the chat back.
        body, headers = s.zendesk.solved_webhook(1)
        assert s.client.post("/v1/zendesk/webhook", content=body,
                             headers=headers).status_code == 202
        out = s.chat("thanks!", "c1")
        assert out["status"] == "answered"
        notes = [m for m in s.model.calls[-1]["messages"] if m["role"] == "note"]
        assert "ticket #1" in notes[-1]["text"]

        # 5. Zendesk revokes the access token: we refresh (rotating) and the call succeeds.
        s.zendesk.expire_access_tokens()
        out = s.chat("one more problem, it's the wrong size", "c1")
        assert out["status"] == "handed_off" and out["ticket_id"] == 2
        assert s.zendesk.refreshes == 1


def test_lost_ticket_response_is_retried_without_a_duplicate():
    with running(call("handoff_to_human", REFUND)) as s:
        s.connect_zendesk()
        s.zendesk.lose_next_create_response = True

        out = s.chat("refund please", "c2")

        assert out["status"] == "handed_off" and out["ticket_id"] == 1
        assert len(s.zendesk.tickets) == 1
        keys = {h["idempotency-key"] for h in s.zendesk.ticket_requests}
        assert len(s.zendesk.ticket_requests) >= 2 and len(keys) == 1


def test_zendesk_not_connected_parks_handoff_in_dead_letters():
    with running(call("handoff_to_human", REFUND)) as s:
        out = s.chat("refund please", "c3")
        assert out["status"] == "handed_off" and out["ticket_id"] is None
        assert s.client.get("/v1/admin/dead-letters", auth=ADMIN).json() == {"pending": 1}

        # Connect Zendesk afterwards; the replay delivers the parked ticket.
        s.connect_zendesk()
        replay = s.client.post("/v1/admin/dead-letters/replay", auth=ADMIN).json()
        assert replay == {"delivered": 1, "failed": 0}
        assert s.zendesk.tickets[1]["requester"]["email"] == "alex@example.com"


def test_misbehaving_shop_backend_still_cannot_leak_other_customers_orders():
    with running(
        call("get_order", {"order_id": "100777"}),
        final("I can't find that order on your account.", grounded="account_data"),
    ) as s:
        s.shop.scoped = False  # backend returns any order regardless of customer
        s.chat("what's in order 100777?", "c4")
        result = s.last_tool_result()
        assert "No order 100777" in result and "socks" not in result.lower()


def test_solved_ticket_is_handed_back_by_polling_when_no_webhook_arrives():
    with running(call("handoff_to_human", REFUND), final("Glad that's sorted!")) as s:
        s.connect_zendesk()
        out = s.chat("refund please", "c5")
        res = s.client.app.state.resources
        open_before = s.client.portal.call(res.service.open_handoffs.all)
        assert [t for _, t in open_before] == [out["ticket_id"]]

        # Still open in Zendesk: the poll leaves the conversation with the human.
        stats = s.client.portal.call(reconcile, res.service, res.ticket_sink,
                                     res.service.open_handoffs)
        assert stats == {"checked": 1, "handed_back": 0}

        # The agent solves it; no webhook is delivered, the poll picks it up instead.
        s.zendesk.tickets[out["ticket_id"]]["status"] = "solved"
        stats = s.client.portal.call(reconcile, res.service, res.ticket_sink,
                                     res.service.open_handoffs)
        assert stats == {"checked": 1, "handed_back": 1}
        assert s.client.portal.call(res.service.open_handoffs.all) == []

        back = s.chat("thanks!", "c5")
        assert back["status"] == "answered"
