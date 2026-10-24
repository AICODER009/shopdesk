import hashlib
import json
import math
import re
from collections.abc import Sequence
from typing import Any

from shopdesk.agent.llm import ModelTurn, ModelUnavailable
from shopdesk.integrations.http import UpstreamError
from shopdesk.integrations.zendesk.models import HandoffTicket, TicketRef
from shopdesk.rag.embeddings import HybridVector, SparseVec
from shopdesk.rag.models import RetrievalResult, RetrievedChunk


def final(answer: str, citations: list[str] | None = None, grounded: str = "none",
          provider: str = "openai") -> ModelTurn:
    body = {"answer": answer, "citations": citations or [], "grounded_in": grounded,
            "confidence": "high"}
    return ModelTurn(provider, "fake", "final", text=json.dumps(body))  # type: ignore[arg-type]


def call(name: str, args: dict[str, Any] | None = None, call_id: str = "call_1",
         provider: str = "openai") -> ModelTurn:
    return ModelTurn(provider, "fake", "tool_calls",  # type: ignore[arg-type]
                     tool_calls=[{"id": call_id, "name": name, "arguments": args or {}}])


class ScriptedModel:
    def __init__(self, *script: ModelTurn | Exception, provider: str = "openai") -> None:
        self.provider = provider
        self.model = f"fake-{provider}"
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def respond(self, **kwargs: Any) -> ModelTurn:
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError(f"{self.model} called more times than scripted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def outage(transient: bool = True) -> ModelUnavailable:
    return ModelUnavailable("fake", "InternalServerError", transient=transient)


class FakeRetriever:
    def __init__(self, chunks: list[RetrievedChunk] | None = None) -> None:
        self.chunks = chunks or []
        self.queries: list[str] = []

    async def search(self, query: str, locale: str | None = None) -> RetrievalResult:
        self.queries.append(query)
        return RetrievalResult(query=query, chunks=list(self.chunks), best_score=3.0)


RETURNS_CHUNK = RetrievedChunk(
    point_id="p1", doc_id="returns-policy", title="Returns policy",
    url="https://help.example.com/returns", breadcrumb="Returns > Time limits",
    text="You can return unworn items within 30 days of delivery for a full refund.",
    rerank_score=5.2,
)


class FlakySink:
    """Ticket sink that fails `failures` times before succeeding."""

    def __init__(self, failures: int, status: int = 503) -> None:
        self.failures = failures
        self.status = status
        self.attempts = 0
        self.tickets: list[HandoffTicket] = []
        self.comments: list[tuple[int, str]] = []

    async def create_ticket(self, ticket: HandoffTicket) -> TicketRef:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise UpstreamError("zendesk", "boom", status=self.status)
        self.tickets.append(ticket)
        return TicketRef(id=900 + len(self.tickets), agent_url="https://x/agent/tickets/1",
                         requester_id=77)

    async def add_customer_comment(self, ticket: TicketRef, body: str) -> None:
        self.comments.append((ticket.id, body))


_TOKEN = re.compile(r"\w+")


class HashEncoder:
    """Deterministic bag-of-words vectors; good enough to exercise Qdrant wiring offline."""

    dim = 64

    def _vec(self, text: str) -> HybridVector:
        counts: dict[int, float] = {}
        for tok in _TOKEN.findall(text.lower()):
            idx = int(hashlib.md5(tok.encode()).hexdigest()[:6], 16) % 50_000  # noqa: S324
            counts[idx] = counts.get(idx, 0) + 1.0
        dense = [0.0] * self.dim
        for idx, c in counts.items():
            dense[idx % self.dim] += c
        norm = math.sqrt(sum(v * v for v in dense)) or 1.0
        return HybridVector([v / norm for v in dense],
                            SparseVec(list(counts), list(counts.values())))

    async def embed_documents(self, texts: Sequence[str]) -> list[HybridVector]:
        return [self._vec(t) for t in texts]

    async def embed_query(self, text: str) -> HybridVector:
        return self._vec(text)
