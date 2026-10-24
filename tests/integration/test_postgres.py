"""Runs against a real Postgres: `docker compose up -d postgres` then
TEST_DATABASE_URL=postgresql://shopdesk:shopdesk@localhost:5433/shopdesk pytest -m postgres"""

import os
import uuid

import pytest
from cryptography.fernet import Fernet
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from shopdesk import retention
from shopdesk.agent.dead_letters import PostgresDeadLetterStore, replay
from shopdesk.agent.graph import AgentDeps, SupportAgent
from shopdesk.agent.handback import PostgresOpenHandoffs
from shopdesk.agent.service import ChatService, Pseudonymizer
from shopdesk.agent.tools import Toolbox
from shopdesk.demo import demo_commerce
from shopdesk.integrations.zendesk.credentials import PostgresCredentialStore, TokenCipher
from shopdesk.integrations.zendesk.models import HandoffTicket, ZendeskToken
from shopdesk.security.injection import InjectionGuard
from tests.fakes import FakeRetriever, FlakySink, ScriptedModel, call, final

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set"),
]


@pytest.fixture
async def pool():
    async with AsyncConnectionPool(
        DSN, open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    ) as p:
        await p.open()
        yield p


@pytest.fixture
def cipher():
    return TokenCipher(Fernet.generate_key().decode())


def ticket(key: str = "k") -> HandoffTicket:
    return HandoffTicket(conversation_id="t", subject="s", internal_note="secret transcript",
                         requester_email="a@b.co", idempotency_key=key)


async def test_credentials_round_trip_encrypted(pool, cipher):
    store = PostgresCredentialStore(pool, cipher)
    await store.setup()
    key = f"acme-{uuid.uuid4()}"
    await store.save(key, ZendeskToken(access_token="plain-at", refresh_token="plain-rt"))
    async with pool.connection() as conn:
        row = await (await conn.execute(
            "SELECT ciphertext FROM integration_credentials WHERE key = %s", (key,))).fetchone()
    stored = bytes(row["ciphertext"])
    assert b"plain-at" not in stored and b"plain-rt" not in stored
    assert (await store.load(key)).refresh_token.get_secret_value() == "plain-rt"
    await store.delete(key)
    assert await store.load(key) is None


async def test_dead_letters_lease_replay_and_followups(pool, cipher):
    store = PostgresDeadLetterStore(pool, cipher)
    await store.setup()
    thread = f"thr-{uuid.uuid4()}"
    await store.add(thread, ticket(), "UpstreamError 503")
    assert await store.add_followup(thread, "any update?")

    due = await store.due()
    mine = [d for d in due if d.thread_id == thread]
    assert mine and mine[0].followups == ["any update?"]
    # Leased: a second worker doesn't see it.
    assert not [d for d in await store.due() if d.thread_id == thread]

    async with pool.connection() as conn:
        await conn.execute("UPDATE handoff_dead_letters SET next_attempt_at = now() "
                           "WHERE thread_id = %s", (thread,))
    sink = FlakySink(failures=0)
    stats = await replay(store, sink)
    assert stats["delivered"] >= 1
    assert "any update?" in sink.tickets[-1].internal_note
    ref = await store.delivered_ticket(thread)
    assert ref is not None and ref.requester_id == 77
    await store.delete_thread(thread)
    assert await store.delivered_ticket(thread) is None


async def test_failed_replays_back_off_and_eventually_die(pool, cipher):
    store = PostgresDeadLetterStore(pool, cipher)
    await store.setup()
    thread = f"thr-{uuid.uuid4()}"
    letter_id = await store.add(thread, ticket(), "x")
    async with pool.connection() as conn:
        await conn.execute("UPDATE handoff_dead_letters SET attempts = 11 WHERE id = %s",
                           (letter_id,))
    await store.mark_failed(letter_id, "still down")
    async with pool.connection() as conn:
        row = await (await conn.execute(
            "SELECT status, attempts FROM handoff_dead_letters WHERE id = %s", (letter_id,)
        )).fetchone()
    assert (row["status"], row["attempts"]) == ("dead", 12)


async def test_handoff_survives_restart_with_postgres_checkpointer(pool, settings, alex):
    saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
    await saver.setup()
    letters = PostgresDeadLetterStore(pool, TokenCipher(Fernet.generate_key().decode()))
    await letters.setup()
    sink = FlakySink(failures=0)

    def service(model: ScriptedModel) -> ChatService:
        agent = SupportAgent(AgentDeps(
            settings=settings, primary=model, fallback=None, guard=InjectionGuard(None),
            toolbox=Toolbox(FakeRetriever(), demo_commerce()),  # type: ignore[arg-type]
            tickets=sink, dead_letters=letters, canary="ref-x",
        ))
        return ChatService(agent.compile(saver), saver, sink, letters, Pseudonymizer(b"k"))

    conversation = str(uuid.uuid4())
    args = {"reason": "customer_request", "summary": "wants a person", "priority": "normal",
            "contact_email": ""}
    first = service(ScriptedModel(call("handoff_to_human", args)))
    out = await first.send(alex, conversation, "human please")
    assert out.status == "handed_off"

    # A fresh process (new graph, same database) still knows the human owns the thread.
    second_model = ScriptedModel(final("welcome back"))
    second = service(second_model)
    forwarded = await second.send(alex, conversation, "hello?")
    assert forwarded.status == "handed_off" and second_model.calls == []

    thread = second.thread_id(alex, conversation)
    assert await second.resolve_handoff(thread, out.ticket.id, "solved")
    assert (await second.send(alex, conversation, "thanks")).status == "answered"

    await second.forget(alex, conversation)
    assert await saver.aget_tuple({"configurable": {"thread_id": thread}}) is None


async def test_retention_trims_history_and_deletes_idle_threads(pool, settings, alex):
    saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
    await saver.setup()
    await PostgresDeadLetterStore(pool, TokenCipher(None)).setup()
    model = ScriptedModel(final("a"), final("b"))
    agent = SupportAgent(AgentDeps(
        settings=settings, primary=model, fallback=None, guard=InjectionGuard(None),
        toolbox=Toolbox(FakeRetriever(), demo_commerce()),  # type: ignore[arg-type]
        tickets=FlakySink(0), dead_letters=PostgresDeadLetterStore(pool, TokenCipher(None)),
        canary="ref-x",
    ))
    svc = ChatService(agent.compile(saver), saver, FlakySink(0),
                      PostgresDeadLetterStore(pool, TokenCipher(None)), Pseudonymizer(b"k"))
    conversation = str(uuid.uuid4())
    await svc.send(alex, conversation, "one")
    await svc.send(alex, conversation, "two")
    thread = svc.thread_id(alex, conversation)

    async def count() -> int:
        async with pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT count(*) AS n FROM checkpoints WHERE thread_id = %s", (thread,)
            )).fetchone()
        return int(row["n"])

    assert await count() > 1
    stats = await retention.purge(pool, ttl_days=30)
    assert stats["trimmed_threads"] >= 1
    assert await count() == 1
    state = await saver.aget_tuple({"configurable": {"thread_id": thread}})
    assert len(state.checkpoint["channel_values"]["messages"]) == 4

    await retention.purge(pool, ttl_days=-1)
    assert await count() == 0


async def test_open_handoff_registry_persists(pool):
    store = PostgresOpenHandoffs(pool)
    await store.setup()
    thread = f"thr-{uuid.uuid4()}"
    await store.add(thread, 41)
    await store.add(thread, 42)  # re-handoff on the same thread replaces the ticket
    assert (thread, 42) in await store.all()
    await store.remove(thread)
    assert all(t != thread for t, _ in await store.all())
