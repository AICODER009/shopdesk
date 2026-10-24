from typing import Any

import httpx
import structlog
from langfuse import observe

from shopdesk.integrations.http import UpstreamError, request_with_retries
from shopdesk.integrations.zendesk.auth import ZendeskAuth
from shopdesk.integrations.zendesk.models import HandoffTicket, TicketRef

log = structlog.get_logger()


class ZendeskClient:
    def __init__(self, base_url: str, auth: ZendeskAuth, http: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._auth = auth
        self._http = http

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        for attempt in range(2):
            headers["Authorization"] = await self._auth.authorization()
            resp = await request_with_retries(
                self._http, method, f"{self._base}{path}", headers=headers, **kwargs
            )
            if resp.status_code == 401 and attempt == 0 and await self._auth.handle_unauthorized():
                continue
            return resp
        raise AssertionError("unreachable")

    @observe(name="zendesk.create_ticket", as_type="tool")
    async def create_ticket(self, ticket: HandoffTicket) -> TicketRef:
        requester: dict[str, str] = {}
        if ticket.requester_email:
            requester["email"] = ticket.requester_email
        if ticket.requester_name:
            requester["name"] = ticket.requester_name

        body: dict[str, Any] = {
            "ticket": {
                "subject": ticket.subject,
                # Internal note: the customer sees the human agent's reply, not our context dump.
                "comment": {"body": ticket.internal_note, "public": False},
                "priority": ticket.priority,
                "tags": sorted({"shopdesk", "ai_handoff", *ticket.tags}),
                "external_id": ticket.conversation_id,
            }
        }
        if requester:
            body["ticket"]["requester"] = requester

        resp = await self._send(
            "POST",
            "/api/v2/tickets.json",
            json=body,
            headers={"Idempotency-Key": ticket.idempotency_key},
        )
        if resp.status_code == 422:
            log.error("zendesk.ticket_rejected", details=resp.json().get("details"))
        if not resp.is_success:
            raise UpstreamError("zendesk", f"create ticket failed: HTTP {resp.status_code}",
                                status=resp.status_code)

        data = resp.json()["ticket"]
        ticket_id = int(data["id"])
        if resp.headers.get("x-idempotency-lookup") == "hit":
            log.info("zendesk.ticket_deduplicated", ticket_id=ticket_id)
        return TicketRef(
            id=ticket_id,
            agent_url=f"{self._base}/agent/tickets/{ticket_id}",
            requester_id=data.get("requester_id"),
        )

    async def ticket_statuses(self, ticket_ids: list[int]) -> dict[int, str]:
        if not ticket_ids:
            return {}
        resp = await self._send(
            "GET", "/api/v2/tickets/show_many.json",
            params={"ids": ",".join(str(i) for i in ticket_ids)},
        )
        if not resp.is_success:
            raise UpstreamError("zendesk", f"show_many failed: HTTP {resp.status_code}",
                                status=resp.status_code)
        return {int(t["id"]): t["status"] for t in resp.json().get("tickets", [])}

    @observe(name="zendesk.add_comment", as_type="tool")
    async def add_customer_comment(self, ticket: TicketRef, body: str) -> None:
        comment: dict[str, Any] = {"body": body, "public": True}
        if ticket.requester_id:
            comment["author_id"] = ticket.requester_id
        resp = await self._send(
            "PUT", f"/api/v2/tickets/{ticket.id}.json", json={"ticket": {"comment": comment}}
        )
        if not resp.is_success:
            raise UpstreamError("zendesk", f"add comment failed: HTTP {resp.status_code}",
                                status=resp.status_code)
