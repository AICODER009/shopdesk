import hashlib
import json
from typing import Any, Literal, Protocol

import structlog

from shopdesk.agent.state import CustomerContext
from shopdesk.integrations.zendesk.models import HandoffTicket, TicketRef
from shopdesk.privacy import EMAIL

log = structlog.get_logger()

HandoffReason = Literal[
    "customer_request",
    "refund_or_cancellation",
    "account_change",
    "fraud_or_chargeback",
    "legal_complaint",
    "data_protection_request",
    "safety",
    "unresolved",
    "assistant_unavailable",
]

_PRIORITY_FLOOR: dict[str, str] = {
    "fraud_or_chargeback": "high",
    "safety": "urgent",
    "legal_complaint": "high",
}
_RANK = {"low": 0, "normal": 1, "high": 2, "urgent": 3}


class TicketSink(Protocol):
    async def create_ticket(self, ticket: HandoffTicket) -> TicketRef: ...

    async def add_customer_comment(self, ticket: TicketRef, body: str) -> None: ...

    async def ticket_statuses(self, ticket_ids: list[int]) -> dict[int, str]: ...


class InMemoryTicketSink:
    """Stands in for Zendesk in dev and tests."""

    def __init__(self) -> None:
        self.tickets: list[HandoffTicket] = []
        self.comments: list[tuple[int, str]] = []
        self.statuses: dict[int, str] = {}
        self._by_key: dict[str, int] = {}

    async def create_ticket(self, ticket: HandoffTicket) -> TicketRef:
        if ticket.idempotency_key not in self._by_key:
            self.tickets.append(ticket)
            self._by_key[ticket.idempotency_key] = len(self.tickets)
            log.info("handoff.recorded_locally", subject=ticket.subject)
        tid = self._by_key[ticket.idempotency_key]
        return TicketRef(id=tid, agent_url=f"local://tickets/{tid}")

    async def add_customer_comment(self, ticket: TicketRef, body: str) -> None:
        self.comments.append((ticket.id, body))

    async def ticket_statuses(self, ticket_ids: list[int]) -> dict[int, str]:
        return {i: self.statuses.get(i, "open") for i in ticket_ids
                if 0 < i <= len(self.tickets)}


def transcript_lines(messages: list[dict[str, Any]], limit: int = 12) -> list[str]:
    lines: list[str] = []
    for msg in messages:
        if msg["role"] == "user":
            lines.append(f"Customer: {msg['text']}")
        elif msg["role"] == "assistant" and msg["text"]:
            try:
                lines.append(f"Assistant: {json.loads(msg['text'])['answer']}")
            except (ValueError, KeyError, TypeError):
                lines.append(f"Assistant: {msg['text']}")
    return lines[-limit:]


def build_ticket(
    *,
    conversation_id: str,
    tool_use_id: str,
    customer: CustomerContext,
    reason: HandoffReason,
    summary: str,
    priority: str,
    contact_email: str,
    messages: list[dict[str, Any]],
    order_ids: list[str],
) -> HandoffTicket:
    if _RANK[priority] < _RANK[_PRIORITY_FLOOR.get(reason, "low")]:
        priority = _PRIORITY_FLOOR[reason]

    email = customer.email
    if email is None and contact_email and EMAIL.fullmatch(contact_email.strip()):
        email = contact_email.strip()

    who = (
        f"Signed-in customer {customer.customer_id}"
        if customer.signed_in
        else "Guest (not signed in)" + (" - contact email given in chat" if email else "")
    )
    note = "\n".join(
        [
            "Handed off by the AI assistant.",
            f"Reason: {reason.replace('_', ' ')}",
            f"Customer: {who}",
            f"Orders discussed: {', '.join(order_ids) if order_ids else 'none'}",
            "",
            "Summary from the assistant:",
            summary.strip(),
            "",
            "Recent conversation:",
            *transcript_lines(messages),
        ]
    )
    key = hashlib.sha256(f"{conversation_id}:{tool_use_id}".encode()).hexdigest()
    return HandoffTicket(
        conversation_id=conversation_id,
        subject=f"[AI handoff] {summary.strip().splitlines()[0][:110]}",
        internal_note=note,
        requester_name=customer.first_name,
        requester_email=email,
        priority=priority,
        tags=[f"reason_{reason}", "signed_in" if customer.signed_in else "guest"],
        idempotency_key=key,
    )
