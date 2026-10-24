"""Boots the real app wiring with dev settings (in-memory Qdrant, demo orders, local ticket
sink). No network calls: model and embedding clients are created but never used here."""

import os

import pytest
from fastapi.testclient import TestClient

from shopdesk.agent.handoff import InMemoryTicketSink
from shopdesk.config import Settings
from shopdesk.integrations.zendesk.client import ZendeskClient
from shopdesk.main import create_app
from shopdesk.wiring import Resources


def dev_settings(**overrides) -> Settings:
    base = {"env": "dev", "openai_api_key": "sk-test", "anthropic_api_key": "sk-ant-test",
            "qdrant_url": ":memory:", "langfuse_public_key": None, "admin_token": "adm1n"}
    return Settings(_env_file=None, **(base | overrides))  # type: ignore[call-arg]


async def test_dev_wiring_uses_local_fallbacks():
    async with Resources.open(dev_settings()) as res:
        assert isinstance(res.ticket_sink, InMemoryTicketSink)
        assert res.oauth is None
        assert await res.dead_letters.counts() == {}


async def test_zendesk_wiring_in_both_auth_modes():
    oauth = dev_settings(zendesk_subdomain="acme", zendesk_client_id="cid",
                         zendesk_client_secret="sec")
    async with Resources.open(oauth) as res:
        assert isinstance(res.ticket_sink, ZendeskClient) and res.oauth is not None

    token = dev_settings(zendesk_subdomain="acme", zendesk_auth_mode="api_token",
                         zendesk_email="agent@acme.test", zendesk_api_token="t")
    async with Resources.open(token) as res:
        assert isinstance(res.ticket_sink, ZendeskClient)


@pytest.mark.parametrize(
    "overrides",
    [
        {"openai_api_key": None},
        {"zendesk_subdomain": "acme"},
        {"zendesk_subdomain": "acme", "zendesk_auth_mode": "api_token"},
    ],
)
async def test_wiring_refuses_incomplete_config(overrides):
    with pytest.raises(RuntimeError):
        async with Resources.open(dev_settings(**overrides)):
            pass


def test_prod_settings_require_secrets():
    with pytest.raises(ValueError, match="widget_jwt_secret"):
        Settings(_env_file=None, env="prod", openai_api_key="x")  # type: ignore[call-arg]


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="needs Postgres")
async def test_postgres_wiring():
    settings = dev_settings(database_url=os.environ["TEST_DATABASE_URL"])
    async with Resources.open(settings) as res:
        assert isinstance(await res.dead_letters.counts(), dict)
        assert type(res.credentials).__name__ == "PostgresCredentialStore"


def test_app_boots_and_serves_health():
    app = create_app(dev_settings(cors_origins=["https://shop.example"]))
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        status = client.get("/v1/admin/zendesk/status", auth=("admin", "adm1n")).json()
        assert status["connected"] is False


def test_dev_storefront_signs_tokens_the_api_accepts():
    import jwt

    from shopdesk.api.security import customer_from_token

    app = create_app(dev_settings())
    with TestClient(app) as client:
        assert client.get("/", follow_redirects=False).headers["location"] == "/dev/storefront"
        assert client.get("/dev/token").json() == {"token": None}
        page = client.get("/dev/storefront").text
        assert 'src="/widget/shopdesk-widget.js?v=' in page and "Sign in as Alex" in page
        assert "__" not in page.split("<script>")[0]  # every template marker replaced

        client.get("/dev/sign-in", follow_redirects=False)
        token = client.get("/dev/token").json()["token"]
        settings = app.state.resources.settings
        customer = customer_from_token(token, settings)
        assert customer.customer_id == "cus_demo_1"
        assert jwt.get_unverified_header(token)["alg"] == "HS256"

        client.get("/dev/sign-out", follow_redirects=False)
        assert client.get("/dev/token").json() == {"token": None}


def test_dev_routes_do_not_exist_outside_dev():
    from cryptography.fernet import Fernet

    settings = dev_settings(
        env="staging", widget_jwt_secret="w" * 32, pseudonym_key="p" * 32,
        token_encryption_key=Fernet.generate_key().decode(), database_url="postgresql://x",
    )
    app = create_app(settings)
    assert "/v1/chat" in app.openapi()["paths"]
    client = TestClient(app)  # no lifespan: we only care whether the route exists
    for path in ("/dev/storefront", "/dev/token", "/dev/sign-in"):
        assert client.get(path, follow_redirects=False).status_code == 404
