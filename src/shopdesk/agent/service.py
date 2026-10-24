import asyncio
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Any
from weakref import WeakValueDictionary

import structlog
from langchain_core.runnables import RunnableConfig
from langfuse import get_client, propagate_attributes
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer, Command

from shopdesk.agent.dead_letters import DeadLetterStore
from shopdesk.agent.handback import InMemoryOpenHandoffs, OpenHandoffStore
from shopdesk.agent.handoff import TicketSink
from shopdesk.agent.state import CustomerContext, TurnOutcome
from shopdesk.integrations.http import UpstreamError
from shopdesk.integrations.zendesk.models import TicketRef
from shopdesk.privacy import redact_for_model

log = structlog.get_logger()

FORWARDED_REPLY = (
    "Thanks, I've added that to your request. Our team is on it and will reply as soon as "
    "they can."
)
FORWARD_FAILED_REPLY = (
    "Sorry, I couldn't pass that on just now. Please try again in a minute; your request is "
    "still with our team."
)


class Pseudonymizer:
    """Keyed hash for identifiers that leave the core system (traces, thread ids, the
    OpenAI safety identifier). Stable per customer, not reversible without the key."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    def __call__(self, value: str) -> str:
        return hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()[:24]


class ChatService:
    def __init__(
        self,
        graph: CompiledStateGraph,  # type: ignore[type-arg]
        checkpointer: Checkpointer,
        tickets: TicketSink,
        dead_letters: DeadLetterStore,
        pseudonymize: Pseudonymizer,
        takeover_ttl: timedelta = timedelta(hours=72),
        open_handoffs: OpenHandoffStore | None = None,
    ) -> None:
        self._graph = graph
        self._checkpointer = checkpointer
        self._tickets = tickets
        self._dead_letters = dead_letters
        self._pseudo = pseudonymize
        self._takeover_ttl = takeover_ttl
        self.open_handoffs = open_handoffs or InMemoryOpenHandoffs()
        # In-process only. With several replicas, route by conversation id (sticky) so one
        # worker owns a thread; otherwise a double-sent message can race on the checkpoint.
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def thread_id(self, customer: CustomerContext, conversation_id: str) -> str:
        owner = self._pseudo(customer.customer_id) if customer.customer_id else "guest"
        return f"{owner}:{conversation_id}"

    def _lock(self, thread_id: str) -> asyncio.Lock:
        lock = self._locks.get(thread_id)
        if lock is None:
            lock = self._locks[thread_id] = asyncio.Lock()
        return lock

    @staticmethod
    def _config(thread_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": thread_id}, "recursion_limit": 60}

    async def send(
        self, customer: CustomerContext, conversation_id: str, text: str
    ) -> TurnOutcome:
        thread_id = self.thread_id(customer, conversation_id)
        config = self._config(thread_id)
        text = redact_for_model(text)

        async with self._lock(thread_id):
            snapshot = await self._graph.aget_state(config)
            if snapshot.interrupts:
                takeover = snapshot.values.get("takeover") or {}
                if not self._expired(takeover):
                    return await self._forward_to_human(thread_id, takeover, text)
                await self._graph.ainvoke(Command(resume={"status": "expired"}), config)

            user_ref = self._pseudo(customer.customer_id or thread_id)
            turn: dict[str, Any] = {
                "thread_id": thread_id,
                "customer": customer.model_dump(),
                "user_ref": user_ref,
                "incoming": text,
                "turn_steps": 0,
                "repairs": 0,
                "pending_handoff": None,
                "outcome": None,
            }
            langfuse = get_client()
            with (
                langfuse.start_as_current_observation(
                    name="chat.turn", as_type="agent", input=text
                ) as span,
                propagate_attributes(
                    session_id=self._pseudo(thread_id),
                    user_id=user_ref,
                    tags=["signed_in" if customer.signed_in else "guest"],
                ),
            ):
                # sync durability: a turn may create a Zendesk ticket, so the checkpoint
                # must be on disk before the next step runs.
                result = await self._graph.ainvoke(turn, config, durability="sync")
                outcome = TurnOutcome.model_validate(result["outcome"])
                span.update(output=outcome.reply, metadata={"status": outcome.status})
            if outcome.status == "handed_off" and outcome.ticket is not None:
                await self.open_handoffs.add(thread_id, outcome.ticket.id)
        return outcome

    def _expired(self, takeover: dict[str, Any]) -> bool:
        since = takeover.get("since")
        return since is not None and (
            datetime.now(UTC) - datetime.fromisoformat(since) > self._takeover_ttl
        )

    async def _ticket_for(self, thread_id: str, takeover: dict[str, Any]) -> TicketRef | None:
        if takeover.get("ticket"):
            return TicketRef.model_validate(takeover["ticket"])
        return await self._dead_letters.delivered_ticket(thread_id)

    async def _forward_to_human(
        self, thread_id: str, takeover: dict[str, Any], text: str
    ) -> TurnOutcome:
        ticket = await self._ticket_for(thread_id, takeover)
        try:
            if ticket is not None:
                await self._tickets.add_customer_comment(ticket, text)
            elif not await self._dead_letters.add_followup(thread_id, text):
                log.error("takeover.orphaned", thread_id=thread_id)
        except UpstreamError as exc:
            log.warning("takeover.forward_failed", error=str(exc))
            return TurnOutcome(status="error", reply=FORWARD_FAILED_REPLY, ticket=ticket)
        return TurnOutcome(status="handed_off", reply=FORWARDED_REPLY, ticket=ticket)

    async def resolve_handoff(self, thread_id: str, ticket_id: int, status: str) -> bool:
        """Hand the conversation back to the assistant. Called from the Zendesk webhook."""
        config = self._config(thread_id)
        async with self._lock(thread_id):
            snapshot = await self._graph.aget_state(config)
            if not snapshot.interrupts:
                return False
            ticket = await self._ticket_for(thread_id, snapshot.values.get("takeover") or {})
            if ticket is None or ticket.id != ticket_id:
                log.warning("takeover.ticket_mismatch", thread_id=thread_id, ticket_id=ticket_id)
                return False
            await self._graph.ainvoke(
                Command(resume={"status": status, "ticket_id": ticket_id}), config
            )
            await self.open_handoffs.remove(thread_id)
        log.info("takeover.resolved", ticket_id=ticket_id, status=status)
        return True

    async def forget(self, customer: CustomerContext, conversation_id: str) -> None:
        """Right to erasure: drop the checkpointed conversation and any queued handoff."""
        thread_id = self.thread_id(customer, conversation_id)
        async with self._lock(thread_id):
            await self._checkpointer.adelete_thread(thread_id)  # type: ignore[union-attr]
            await self._dead_letters.delete_thread(thread_id)
            await self.open_handoffs.remove(thread_id)
        log.info("conversation.erased", thread=self._pseudo(thread_id))
