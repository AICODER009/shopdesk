import asyncio
import sys
from dataclasses import dataclass
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from shopdesk.agent.dead_letters import InMemoryDeadLetterStore
from shopdesk.agent.graph import AgentDeps, SupportAgent
from shopdesk.agent.handoff import InMemoryTicketSink, TicketSink
from shopdesk.agent.service import ChatService, Pseudonymizer
from shopdesk.agent.state import CustomerContext
from shopdesk.agent.tools import Toolbox
from shopdesk.config import Settings
from shopdesk.demo import DEMO_CUSTOMER, demo_commerce
from shopdesk.security.injection import InjectionGuard
from tests.fakes import FakeRetriever, ScriptedModel


@pytest.fixture(scope="session")
def event_loop_policy():  # type: ignore[no-untyped-def]
    # psycopg's async driver can't run on Windows' default Proactor loop.
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        env="dev",
        llm_attempts=3,
        handoff_attempts=3,
        retry_initial_interval=0.01,
        max_agent_steps=4,
    )


@dataclass
class Harness:
    service: ChatService
    primary: ScriptedModel
    fallback: ScriptedModel | None
    sink: Any
    dead_letters: InMemoryDeadLetterStore
    retriever: FakeRetriever
    checkpointer: InMemorySaver
    canary: str = "ref-test-canary"

    async def say(self, text: str, customer: CustomerContext | None = None,
                  conversation: str = "c1") -> Any:
        return await self.service.send(customer or CustomerContext(), conversation, text)


@pytest.fixture
def make_harness(settings: Settings):  # type: ignore[no-untyped-def]
    def build(
        primary: ScriptedModel,
        fallback: ScriptedModel | None = None,
        sink: TicketSink | None = None,
        retriever: FakeRetriever | None = None,
    ) -> Harness:
        sink = sink or InMemoryTicketSink()
        retriever = retriever or FakeRetriever()
        letters = InMemoryDeadLetterStore()
        saver = InMemorySaver()
        agent = SupportAgent(
            AgentDeps(
                settings=settings,
                primary=primary,
                fallback=fallback,
                guard=InjectionGuard(None),
                toolbox=Toolbox(retriever, demo_commerce()),  # type: ignore[arg-type]
                tickets=sink,
                dead_letters=letters,
                canary="ref-test-canary",
            )
        )
        service = ChatService(agent.compile(saver), saver, sink, letters,
                              Pseudonymizer(b"test-key"))
        return Harness(service, primary, fallback, sink, letters, retriever, saver)

    return build


@pytest.fixture
def alex() -> CustomerContext:
    return CustomerContext(customer_id=DEMO_CUSTOMER, email="alex@example.com", first_name="Alex")
