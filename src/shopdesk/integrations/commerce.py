"""Order and account lookups against the shop backend.

Every call is scoped by `customer_id`, which comes from the verified widget token and is
never taken from model output. That's the control that stops "show me order 1234" from
leaking another customer's order, whatever the prompt says.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

import httpx
import structlog
from pydantic import BaseModel, Field

from shopdesk.integrations.http import ensure_ok, request_with_retries
from shopdesk.privacy import mask_email

log = structlog.get_logger()

OrderStatus = Literal[
    "pending", "paid", "processing", "shipped", "delivered", "cancelled", "returned", "refunded"
]


class Money(BaseModel):
    amount: Decimal
    currency: str = Field(min_length=3, max_length=3)


class LineItem(BaseModel):
    sku: str
    name: str
    quantity: int


class Shipment(BaseModel):
    carrier: str
    tracking_number: str | None = None
    tracking_url: str | None = None
    estimated_delivery: date | None = None
    delivered_at: datetime | None = None


class Order(BaseModel):
    order_id: str
    customer_id: str
    status: OrderStatus
    placed_at: datetime
    total: Money
    items: list[LineItem]
    shipment: Shipment | None = None
    return_window_ends: date | None = None
    shipping_address: dict[str, Any] | None = None

    def for_model(self) -> dict[str, Any]:
        """What the LLM is allowed to see: enough to answer, nothing it doesn't need."""
        return self.model_dump(
            mode="json",
            exclude={"customer_id", "shipping_address"},
            exclude_none=True,
        )


class Account(BaseModel):
    customer_id: str
    first_name: str
    email: str
    member_since: date
    loyalty_tier: str | None = None
    open_returns: int = 0

    def for_model(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude={"customer_id"}, exclude_none=True)
        data["email"] = mask_email(self.email)
        return data


class CommerceClient(Protocol):
    async def get_order(self, customer_id: str, order_id: str) -> Order | None: ...

    async def list_orders(self, customer_id: str, limit: int = 5) -> list[Order]: ...

    async def get_account(self, customer_id: str) -> Account | None: ...


class HttpCommerceClient:
    def __init__(self, base_url: str, api_key: str, http: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._http = http
        self._headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    async def _get(self, path: str, **params: Any) -> httpx.Response:
        return await request_with_retries(
            self._http, "GET", f"{self._base}{path}", headers=self._headers, params=params or None
        )

    async def get_order(self, customer_id: str, order_id: str) -> Order | None:
        resp = await self._get(f"/customers/{customer_id}/orders/{order_id}")
        if resp.status_code == 404:
            return None
        order = Order.model_validate(ensure_ok(resp, "commerce").json())
        if order.customer_id != customer_id:
            # The backend scoped by path, but trust nothing that crosses a tenant boundary.
            log.error("commerce.ownership_mismatch", order_id=order_id)
            return None
        return order

    async def list_orders(self, customer_id: str, limit: int = 5) -> list[Order]:
        resp = await self._get(f"/customers/{customer_id}/orders", limit=limit, sort="-placed_at")
        orders = [Order.model_validate(o) for o in ensure_ok(resp, "commerce").json()["orders"]]
        return [o for o in orders if o.customer_id == customer_id][:limit]

    async def get_account(self, customer_id: str) -> Account | None:
        resp = await self._get(f"/customers/{customer_id}")
        if resp.status_code == 404:
            return None
        return Account.model_validate(ensure_ok(resp, "commerce").json())


class InMemoryCommerceClient:
    """Used for demos and tests until the client's order API is wired up."""

    def __init__(self, orders: list[Order], accounts: list[Account]) -> None:
        self._orders = {o.order_id: o for o in orders}
        self._accounts = {a.customer_id: a for a in accounts}

    async def get_order(self, customer_id: str, order_id: str) -> Order | None:
        order = self._orders.get(order_id.strip().lstrip("#"))
        return order if order and order.customer_id == customer_id else None

    async def list_orders(self, customer_id: str, limit: int = 5) -> list[Order]:
        mine = [o for o in self._orders.values() if o.customer_id == customer_id]
        return sorted(mine, key=lambda o: o.placed_at, reverse=True)[:limit]

    async def get_account(self, customer_id: str) -> Account | None:
        return self._accounts.get(customer_id)
