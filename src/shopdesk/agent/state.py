import operator
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field

from shopdesk.integrations.zendesk.models import TicketRef


class CustomerContext(BaseModel):
    customer_id: str | None = None
    email: str | None = None
    first_name: str | None = None
    locale: str = "en"

    @property
    def signed_in(self) -> bool:
        return self.customer_id is not None


class Source(BaseModel):
    id: str
    doc_id: str
    title: str
    url: str | None = None
    breadcrumb: str = ""


class Citation(BaseModel):
    title: str
    url: str | None = None


class TurnOutcome(BaseModel):
    status: Literal["answered", "declined", "handed_off", "blocked", "error"]
    reply: str
    citations: list[Citation] = Field(default_factory=list)
    ticket: TicketRef | None = None


def _merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {**left, **right}


def _union(left: list[str], right: list[str]) -> list[str]:
    return left + [x for x in right if x not in left]


class AgentState(TypedDict, total=False):
    # Values are plain JSON so the checkpointer never has to deserialize custom classes;
    # Pydantic models above are the validation boundary on the way in and out.
    thread_id: str
    customer: dict[str, Any]
    user_ref: str
    incoming: str
    messages: Annotated[list[dict[str, Any]], operator.add]
    sources: Annotated[dict[str, dict[str, Any]], _merge]
    order_ids: Annotated[list[str], _union]
    turn_steps: int
    repairs: int
    guard: dict[str, Any] | None
    pending_handoff: dict[str, Any] | None
    takeover: dict[str, Any] | None
    outcome: dict[str, Any] | None
