import secrets
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
import anthropic
import httpx
import openai
import structlog
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool
from qdrant_client import AsyncQdrantClient

from shopdesk.agent.dead_letters import (
    DeadLetterStore,
    InMemoryDeadLetterStore,
    PostgresDeadLetterStore,
)
from shopdesk.agent.graph import AgentDeps, SupportAgent
from shopdesk.agent.handback import (
    InMemoryOpenHandoffs,
    OpenHandoffStore,
    PostgresOpenHandoffs,
)
from shopdesk.agent.handoff import InMemoryTicketSink, TicketSink
from shopdesk.agent.llm import AgentModel, ClaudeAgentModel, OpenAIAgentModel
from shopdesk.agent.service import ChatService, Pseudonymizer
from shopdesk.agent.tools import Toolbox
from shopdesk.config import Settings
from shopdesk.integrations.commerce import CommerceClient, HttpCommerceClient
from shopdesk.integrations.zendesk.auth import ApiTokenAuth, OAuthTokenAuth, ZendeskAuth
from shopdesk.integrations.zendesk.client import ZendeskClient
from shopdesk.integrations.zendesk.credentials import (
    CredentialStore,
    InMemoryCredentialStore,
    PostgresCredentialStore,
    TokenCipher,
)
from shopdesk.integrations.zendesk.oauth import ZendeskOAuth
from shopdesk.rag.embeddings import (
    Bm25SparseEncoder,
    CrossEncoderReranker,
    DenseEncoder,
    FastEmbedDenseEncoder,
    HybridEncoder,
    OpenAIDenseEncoder,
)
from shopdesk.rag.retriever import Retriever
from shopdesk.rag.store import KnowledgeStore
from shopdesk.security.injection import (
    AnthropicClassifier,
    Classifier,
    InjectionGuard,
    OpenAIClassifier,
)
from shopdesk.storage_sqlite import (
    SqliteCredentialStore,
    SqliteDB,
    SqliteDeadLetterStore,
    SqliteOpenHandoffs,
    sqlite_path,
)

log = structlog.get_logger()


def _secret(value: Any) -> str | None:
    return value.get_secret_value() if value is not None else None


def openai_client(settings: Settings) -> openai.AsyncOpenAI | None:
    key = _secret(settings.openai_api_key)
    return openai.AsyncOpenAI(api_key=key, base_url=settings.openai_base_url,
                              max_retries=1, timeout=60) if key else None


def anthropic_client(settings: Settings) -> anthropic.AsyncAnthropic | None:
    key = _secret(settings.anthropic_api_key)
    # LangGraph's RetryPolicy owns retries; keep SDK retries short so the two don't multiply.
    return anthropic.AsyncAnthropic(api_key=key, max_retries=1, timeout=60) if key else None


def build_store(settings: Settings) -> KnowledgeStore:
    memory = settings.qdrant_url == ":memory:"
    client = AsyncQdrantClient(
        location=":memory:" if memory else None,
        url=None if memory else settings.qdrant_url,
        api_key=_secret(settings.qdrant_api_key),
        timeout=30,
    )
    return KnowledgeStore(client, settings.qdrant_collection)


def build_encoder(settings: Settings) -> HybridEncoder:
    dense: DenseEncoder
    oa = openai_client(settings)
    if settings.embedding_provider == "openai":
        if oa is None:
            raise RuntimeError("EMBEDDING_PROVIDER=openai needs OPENAI_API_KEY")
        dense = OpenAIDenseEncoder(oa, settings.openai_embedding_model,
                                   settings.embedding_dimensions)
    else:
        dense = FastEmbedDenseEncoder(settings.local_dense_model)
    return HybridEncoder(dense, Bm25SparseEncoder(settings.sparse_model))


def _agent_model(provider: str, settings: Settings) -> AgentModel | None:
    if provider == "openai" and (oa := openai_client(settings)):
        return OpenAIAgentModel(oa, settings.openai_agent_model, settings.agent_effort)
    if provider == "anthropic" and (an := anthropic_client(settings)):
        return ClaudeAgentModel(an, settings.anthropic_agent_model, settings.agent_effort,
                                settings.anthropic_refusal_fallback)
    return None


def _classifier(settings: Settings) -> Classifier | None:
    if settings.llm_provider == "openai" and (oa := openai_client(settings)):
        return OpenAIClassifier(oa, settings.openai_guard_model or settings.openai_agent_model)
    if an := anthropic_client(settings):
        return AnthropicClassifier(an, settings.anthropic_guard_model)
    return None


def _commerce(settings: Settings, http: httpx.AsyncClient) -> CommerceClient:
    if settings.commerce_api_url and settings.commerce_api_key:
        return HttpCommerceClient(settings.commerce_api_url,
                                  settings.commerce_api_key.get_secret_value(), http)
    from shopdesk.demo import demo_commerce

    log.warning("commerce.demo_data", detail="COMMERCE_API_URL unset; using demo orders")
    return demo_commerce()


@dataclass
class Resources:
    settings: Settings
    http: httpx.AsyncClient
    cipher: TokenCipher
    credentials: CredentialStore
    dead_letters: DeadLetterStore
    ticket_sink: TicketSink
    oauth: ZendeskOAuth | None
    service: ChatService
    graph: CompiledStateGraph  # type: ignore[type-arg]

    @classmethod
    @asynccontextmanager
    async def open(
        cls,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        agent_model: AgentModel | None = None,
    ) -> AsyncIterator["Resources"]:
        """`transport` and `agent_model` exist for the end-to-end tests, which route outbound
        HTTP to stand-in Zendesk/shop servers and script the model."""
        async with AsyncExitStack() as stack:
            http = await stack.enter_async_context(
                httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), transport=transport)
            )
            cipher = TokenCipher(_secret(settings.token_encryption_key))

            checkpointer: Any
            credentials: CredentialStore
            dead_letters: DeadLetterStore
            open_handoffs: OpenHandoffStore
            db_url = settings.database_url.get_secret_value() if settings.database_url else ""
            if db_url.startswith("sqlite"):
                path = Path(sqlite_path(db_url))
                path.parent.mkdir(parents=True, exist_ok=True)
                db = await SqliteDB.open(str(path))
                stack.push_async_callback(db.close)
                saver_conn = await aiosqlite.connect(str(path))
                stack.push_async_callback(saver_conn.close)
                checkpointer = AsyncSqliteSaver(saver_conn)
                await checkpointer.setup()
                credentials = SqliteCredentialStore(db, cipher)
                dead_letters = SqliteDeadLetterStore(db, cipher)
                open_handoffs = SqliteOpenHandoffs(db)
                log.info("storage.sqlite", path=str(path))
            elif db_url:
                pool = AsyncConnectionPool(
                    db_url,
                    connection_class=AsyncConnection[DictRow],
                    open=False,
                    max_size=10,
                    # The LangGraph saver needs autocommit + dict rows; prepared statements
                    # break behind PgBouncer in transaction mode.
                    kwargs={"autocommit": True, "prepare_threshold": 0,
                            "row_factory": dict_row},
                )
                await pool.open()
                stack.push_async_callback(pool.close)
                checkpointer = AsyncPostgresSaver(pool)
                await checkpointer.setup()
                pg_credentials = PostgresCredentialStore(pool, cipher)
                pg_letters = PostgresDeadLetterStore(pool, cipher)
                pg_handoffs = PostgresOpenHandoffs(pool)
                await pg_credentials.setup()
                await pg_letters.setup()
                await pg_handoffs.setup()
                credentials, dead_letters = pg_credentials, pg_letters
                open_handoffs = pg_handoffs
            else:
                log.warning("storage.in_memory", detail="DATABASE_URL unset; state is lost on exit")
                checkpointer = InMemorySaver()
                credentials = InMemoryCredentialStore(cipher)
                dead_letters = InMemoryDeadLetterStore()
                open_handoffs = InMemoryOpenHandoffs()

            oauth: ZendeskOAuth | None = None
            if settings.zendesk_subdomain and settings.zendesk_client_id:
                oauth = ZendeskOAuth(settings, cipher, http)

            sink: TicketSink
            if settings.zendesk_subdomain:
                auth: ZendeskAuth
                if settings.zendesk_auth_mode == "oauth":
                    if oauth is None:
                        raise RuntimeError("ZENDESK_AUTH_MODE=oauth needs ZENDESK_CLIENT_ID")
                    auth = OAuthTokenAuth(credentials, oauth, settings.zendesk_subdomain)
                else:
                    if not (settings.zendesk_email and settings.zendesk_api_token):
                        raise RuntimeError("api_token mode needs ZENDESK_EMAIL/ZENDESK_API_TOKEN")
                    auth = ApiTokenAuth(settings.zendesk_email,
                                        settings.zendesk_api_token.get_secret_value())
                sink = ZendeskClient(settings.zendesk_url, auth, http)
            else:
                log.warning("zendesk.local_sink", detail="ZENDESK_SUBDOMAIN unset")
                sink = InMemoryTicketSink()

            primary = agent_model or _agent_model(settings.llm_provider, settings)
            if primary is None:
                raise RuntimeError(f"no API key for LLM_PROVIDER={settings.llm_provider}")
            fallback = None
            if agent_model is None and settings.fallback_provider not in (
                "none", settings.llm_provider
            ):
                fallback = _agent_model(settings.fallback_provider, settings)
                if fallback is None:
                    log.warning("llm.no_fallback", provider=settings.fallback_provider)

            reranker = (CrossEncoderReranker(settings.rerank_model)
                        if settings.rerank_model else None)
            retriever = Retriever(
                build_store(settings),
                build_encoder(settings),
                reranker,
                candidates=settings.retrieval_candidates,
                top_k=settings.retrieval_top_k,
                min_similarity=settings.retrieval_min_similarity,
            )
            agent = SupportAgent(
                AgentDeps(
                    settings=settings,
                    primary=primary,
                    fallback=fallback,
                    guard=InjectionGuard(_classifier(settings)),
                    toolbox=Toolbox(retriever, _commerce(settings, http)),
                    tickets=sink,
                    dead_letters=dead_letters,
                    canary=f"ref-{secrets.token_hex(6)}",
                )
            )
            graph = agent.compile(checkpointer)
            pseudo_key = _secret(settings.pseudonym_key) or secrets.token_hex(32)
            service = ChatService(graph, checkpointer, sink, dead_letters,
                                  Pseudonymizer(pseudo_key.encode()),
                                  open_handoffs=open_handoffs)
            yield cls(settings, http, cipher, credentials, dead_letters, sink, oauth, service,
                      graph)
