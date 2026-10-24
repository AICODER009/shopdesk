"""SQLite storage, run against a real database file (no server needed)."""

import time
import uuid

import aiosqlite
import pytest
from cryptography.fernet import Fernet
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from shopdesk.agent.dead_letters import MAX_REPLAYS, replay
from shopdesk.agent.graph import AgentDeps, SupportAgent
from shopdesk.agent.service import ChatService, Pseudonymizer
from shopdesk.agent.tools import Toolbox
from shopdesk.demo import demo_commerce
from shopdesk.integrations.zendesk.credentials import TokenCipher
from shopdesk.integrations.zendesk.models import HandoffTicket, ZendeskToken
from shopdesk.security.injection import InjectionGuard
from shopdesk.storage_sqlite import (
    SqliteCredentialStore,
    SqliteDB,
    SqliteDeadLetterStore,
    SqliteOpenHandoffs,
    sqlite_path,
)
from tests.fakes import FakeRetriever, FlakySink, ScriptedModel, call, final


@pytest.fixture
async def db(tmp_path):
    database = await SqliteDB.open(str(tmp_path / "shopdesk.db"))
    yield database
    await database.close()


@pytest.fixture
def cipher():
    return TokenCipher(Fernet.generate_key().decode())


def ticket(key: str = "k") -> HandoffTicket:
    return HandoffTicket(conversation_id="t", subject="s", internal_note="secret transcript",
                         requester_email="a@b.co", idempotency_key=key)


def test_sqlite_url_parsing():
    assert sqlite_path("sqlite:///data/shopdesk.db") == "data/shopdesk.db"
    assert sqlite_path("sqlite:////var/lib/shopdesk.db") == "/var/lib/shopdesk.db"


async def test_credentials_are_encrypted_on_disk(db, cipher):
    store = SqliteCredentialStore(db, cipher)
    await store.save("acme", ZendeskToken(access_token="plain-at", refresh_token="plain-rt"))
    raw = await db.fetchall("SELECT ciphertext FROM integration_credentials")
    assert b"plain-at" not in raw[0]["ciphertext"] and b"plain-rt" not in raw[0]["ciphertext"]
    assert (await store.load("acme")).refresh_token.get_secret_value() == "plain-rt"
    await store.save("acme", ZendeskToken(access_token="rotated"))
    assert (await store.load("acme")).access_token.get_secret_value() == "rotated"
    await store.delete("acme")
    assert await store.load("acme") is None


async def test_dead_letters_lease_followups_replay_and_erasure(db, cipher):
    store = SqliteDeadLetterStore(db, cipher)
    await store.add("thr-1", ticket(), "UpstreamError 503")
    assert await store.add_followup("thr-1", "any update?")
    assert not await store.add_followup("thr-unknown", "hello")
    raw = await db.fetchall("SELECT payload FROM handoff_dead_letters")
    assert b"secret transcript" not in raw[0]["payload"]

    [letter] = await store.due()
    assert letter.followups == ["any update?"]
    assert await store.due() == []  # leased

    await db.conn.execute("UPDATE handoff_dead_letters SET next_attempt_at = 0")
    await db.conn.commit()
    sink = FlakySink(failures=0)
    assert await replay(store, sink) == {"delivered": 1, "failed": 0}
    assert "any update?" in sink.tickets[0].internal_note
    assert (await store.delivered_ticket("thr-1")).requester_id == 77
    assert await store.counts() == {"delivered": 1}

    await store.delete_thread("thr-1")
    assert await store.counts() == {}


async def test_failed_replays_back_off_then_go_dead(db, cipher):
    store = SqliteDeadLetterStore(db, cipher)
    letter_id = await store.add("thr-2", ticket(), "x")
    await store.mark_failed(letter_id, "still down")
    rows = await db.fetchall("SELECT attempts, status, next_attempt_at FROM handoff_dead_letters")
    assert rows[0]["attempts"] == 1 and rows[0]["status"] == "pending"
    assert rows[0]["next_attempt_at"] > time.time() + 60  # backed off, not retried at once

    await db.conn.execute("UPDATE handoff_dead_letters SET attempts = ?", (MAX_REPLAYS - 1,))
    await db.conn.commit()
    await store.mark_failed(letter_id, "gave up")
    assert await store.counts() == {"dead": 1}


async def test_open_handoffs(db):
    store = SqliteOpenHandoffs(db)
    await store.add("a", 1)
    await store.add("a", 2)
    await store.add("b", 3)
    assert await store.all() == [("a", 2), ("b", 3)]
    await store.remove("a")
    assert await store.all() == [("b", 3)]


async def test_takeover_survives_a_restart_and_handback_works(tmp_path, settings, alex, cipher):
    path = str(tmp_path / "app.db")
    sink = FlakySink(failures=0)
    args = {"reason": "customer_request", "summary": "wants a person", "priority": "normal",
            "contact_email": ""}

    async def boot(model: ScriptedModel):
        db = await SqliteDB.open(path)
        conn = await aiosqlite.connect(path)
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
        letters = SqliteDeadLetterStore(db, cipher)
        agent = SupportAgent(AgentDeps(
            settings=settings, primary=model, fallback=None, guard=InjectionGuard(None),
            toolbox=Toolbox(FakeRetriever(), demo_commerce()),  # type: ignore[arg-type]
            tickets=sink, dead_letters=letters, canary="ref-x",
        ))
        service = ChatService(agent.compile(saver), saver, sink, letters, Pseudonymizer(b"k"),
                              open_handoffs=SqliteOpenHandoffs(db))
        return service, saver, db, conn

    conversation = str(uuid.uuid4())
    first, _, db1, conn1 = await boot(ScriptedModel(call("handoff_to_human", args)))
    out = await first.send(alex, conversation, "human please")
    assert out.status == "handed_off"
    await db1.close()
    await conn1.close()

    # New process, same file: still with the human, and the handoff is still registered.
    model = ScriptedModel(final("welcome back"))
    second, saver, db2, conn2 = await boot(model)
    thread = second.thread_id(alex, conversation)
    assert await second.open_handoffs.all() == [(thread, out.ticket.id)]
    assert (await second.send(alex, conversation, "hello?")).status == "handed_off"
    assert model.calls == []

    assert await second.resolve_handoff(thread, out.ticket.id, "solved")
    assert await second.open_handoffs.all() == []
    assert (await second.send(alex, conversation, "thanks")).status == "answered"

    await second.forget(alex, conversation)
    assert await saver.aget_tuple({"configurable": {"thread_id": thread}}) is None
    await db2.close()
    await conn2.close()


async def test_retention_trims_and_deletes_idle_threads(tmp_path, settings, alex, cipher):
    from shopdesk.retention import purge_sqlite

    path = str(tmp_path / "ret.db")
    db = await SqliteDB.open(path)
    conn = await aiosqlite.connect(path)
    saver = AsyncSqliteSaver(conn)
    await saver.setup()
    letters = SqliteDeadLetterStore(db, cipher)
    agent = SupportAgent(AgentDeps(
        settings=settings, primary=ScriptedModel(final("a"), final("b")), fallback=None,
        guard=InjectionGuard(None), toolbox=Toolbox(FakeRetriever(), demo_commerce()),  # type: ignore[arg-type]
        tickets=FlakySink(0), dead_letters=letters, canary="ref-x",
    ))
    svc = ChatService(agent.compile(saver), saver, FlakySink(0), letters, Pseudonymizer(b"k"))
    conversation = str(uuid.uuid4())
    await svc.send(alex, conversation, "one")
    await svc.send(alex, conversation, "two")
    thread = svc.thread_id(alex, conversation)

    async def count() -> int:
        return len([c async for c in saver.alist({"configurable": {"thread_id": thread}})])

    assert await count() > 1
    stats = await purge_sqlite(saver, db, ttl_days=30)
    assert stats["trimmed_threads"] == 1 and await count() == 1
    state = await saver.aget_tuple({"configurable": {"thread_id": thread}})
    assert len(state.checkpoint["channel_values"]["messages"]) == 4

    await purge_sqlite(saver, db, ttl_days=-1)
    assert await count() == 0
    await db.close()
    await conn.close()
