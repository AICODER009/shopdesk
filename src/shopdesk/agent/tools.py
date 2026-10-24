import html
import json
from dataclasses import dataclass, field
from typing import Any, Literal

import anthropic
import structlog
from langfuse import observe
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from shopdesk.agent.handoff import HandoffReason
from shopdesk.agent.llm import ToolSpec
from shopdesk.agent.state import CustomerContext, Source
from shopdesk.agent.transcript import ToolCall, ToolResult
from shopdesk.integrations.commerce import CommerceClient
from shopdesk.integrations.http import UpstreamError
from shopdesk.rag.models import RetrievalResult
from shopdesk.rag.retriever import Retriever
from shopdesk.security.injection import as_untrusted

log = structlog.get_logger()

HANDOFF_TOOL = "handoff_to_human"


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchArgs(_Args):
    query: str = Field(description="A focused search query, rephrased from the customer's words.")


class OrderArgs(_Args):
    order_id: str = Field(description="Order number as the customer gave it, e.g. 100245.")


class NoArgs(_Args):
    pass


class HandoffArgs(_Args):
    reason: HandoffReason
    summary: str = Field(description="Brief for the human agent: request, what you checked, "
                                     "what's still open.")
    priority: Literal["low", "normal", "high", "urgent"]
    contact_email: str = Field(description="Only for guests who gave an email in this chat; "
                                           "otherwise an empty string.")


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = anthropic.transform_schema(model)

    def drop_titles(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: drop_titles(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [drop_titles(v) for v in node]
        return node

    return drop_titles(schema)  # type: ignore[no-any-return]


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        "search_knowledge_base",
        "Search the help centre (policies, shipping, returns, payments, product care, account "
        "help). Returns numbered sources to cite. Use for any question about how things work.",
        strict_schema(SearchArgs),
    ),
    ToolSpec(
        "get_order",
        "Look up one of the signed-in customer's orders: status, items, total, shipment "
        "tracking and return window.",
        strict_schema(OrderArgs),
    ),
    ToolSpec(
        "list_recent_orders",
        "List the signed-in customer's five most recent orders. Use when they don't know the "
        "order number.",
        strict_schema(NoArgs),
    ),
    ToolSpec(
        "get_account_summary",
        "The signed-in customer's account summary: membership, loyalty tier, open returns.",
        strict_schema(NoArgs),
    ),
    ToolSpec(
        HANDOFF_TOOL,
        "Transfer the conversation to a human agent via a support ticket. Ends your part of "
        "the conversation.",
        strict_schema(HandoffArgs),
    ),
]

_ARG_MODELS: dict[str, type[_Args]] = {
    "search_knowledge_base": SearchArgs,
    "get_order": OrderArgs,
    "list_recent_orders": NoArgs,
    "get_account_summary": NoArgs,
    HANDOFF_TOOL: HandoffArgs,
}

NOT_SIGNED_IN = (
    "The customer is not signed in, so no order or account data is available. Ask them to "
    "sign in to the website or app and come back to this chat. Do not ask for an email, "
    "order confirmation or other details to verify them yourself."
)

NO_RESULTS = (
    "No sufficiently relevant help-centre articles were found. Don't answer from general "
    "knowledge. Either try one rephrased search, or tell the customer you don't have that "
    "information and offer to connect them with the team."
)


@dataclass(slots=True)
class ToolRun:
    call: ToolCall
    content: str = ""
    is_error: bool = False
    retrieval: RetrievalResult | None = None
    order_ids: list[str] = field(default_factory=list)

    def result(self) -> ToolResult:
        return {"call_id": self.call["id"], "name": self.call["name"],
                "content": self.content, "is_error": self.is_error}


def parse_args(call: ToolCall) -> _Args:
    return _ARG_MODELS[call["name"]].model_validate(call["arguments"])


class Toolbox:
    def __init__(self, retriever: Retriever, commerce: CommerceClient) -> None:
        self._retriever = retriever
        self._commerce = commerce

    @observe(name="tool", as_type="tool")
    async def run(self, call: ToolCall, customer: CustomerContext) -> ToolRun:
        run = ToolRun(call)
        if call["name"] not in _ARG_MODELS or call["name"] == HANDOFF_TOOL:
            run.content, run.is_error = f"Unknown tool {call['name']}.", True
            return run
        try:
            args = parse_args(call)
        except ValidationError as exc:
            run.content, run.is_error = f"Invalid arguments: {exc.errors()[0]['msg']}", True
            return run

        try:
            match args:
                case SearchArgs(query=query):
                    run.retrieval = await self._retriever.search(query, customer.locale)
                case OrderArgs(order_id=order_id):
                    await self._order(run, customer, order_id)
                case NoArgs() if call["name"] == "list_recent_orders":
                    await self._recent(run, customer)
                case NoArgs():
                    await self._account(run, customer)
        except UpstreamError as exc:
            log.warning("tool.upstream_error", tool=call["name"], error=str(exc))
            run.content = "That system isn't responding right now. Apologise, and offer to " \
                          "try again shortly or connect the customer with the team."
            run.is_error = True
        return run

    async def _order(self, run: ToolRun, customer: CustomerContext, order_id: str) -> None:
        if customer.customer_id is None:
            run.content = NOT_SIGNED_IN
            return
        order = await self._commerce.get_order(customer.customer_id, order_id.strip())
        if order is None:
            run.content = (f"No order {order_id} on this customer's account. It may be a typo "
                           "or belong to a different account; don't speculate which.")
            return
        run.order_ids.append(order.order_id)
        run.content = _data_block("order_data", order.for_model())

    async def _recent(self, run: ToolRun, customer: CustomerContext) -> None:
        if customer.customer_id is None:
            run.content = NOT_SIGNED_IN
            return
        orders = await self._commerce.list_orders(customer.customer_id, limit=5)
        run.order_ids += [o.order_id for o in orders]
        run.content = _data_block("order_data", [o.for_model() for o in orders]) if orders \
            else "This customer has no orders."

    async def _account(self, run: ToolRun, customer: CustomerContext) -> None:
        if customer.customer_id is None:
            run.content = NOT_SIGNED_IN
            return
        account = await self._commerce.get_account(customer.customer_id)
        run.content = _data_block("account_data", account.for_model()) if account \
            else "No account found."


def _data_block(tag: str, data: Any) -> str:
    # Product names and notes can be seller- or customer-supplied; escape like any other
    # untrusted text so it can't close our tags.
    return f"<{tag}>\n{as_untrusted(json.dumps(data, ensure_ascii=False, indent=1))}\n</{tag}>"


def _attr(value: str) -> str:
    return html.escape(value, quote=True)


def render_search(run: ToolRun, first_id: int) -> tuple[str, dict[str, dict[str, Any]]]:
    retrieval = run.retrieval
    assert retrieval is not None
    if retrieval.empty:
        return NO_RESULTS, {}

    sources: dict[str, dict[str, Any]] = {}
    parts = [f'<search_results query="{_attr(retrieval.query)}">']
    for n, chunk in enumerate(retrieval.chunks, start=first_id):
        sid = f"S{n}"
        sources[sid] = Source(id=sid, doc_id=chunk.doc_id, title=chunk.title, url=chunk.url,
                              breadcrumb=chunk.breadcrumb).model_dump()
        section = f' section="{_attr(chunk.breadcrumb)}"' if chunk.breadcrumb else ""
        parts.append(f'<source id="{sid}" title="{_attr(chunk.title)}"{section}>\n'
                     f"{as_untrusted(chunk.text)}\n</source>")
    parts.append("</search_results>")
    return "\n".join(parts), sources
