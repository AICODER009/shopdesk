"""In-process stand-ins for Zendesk and the shop backend.

They implement the parts of each API the agent depends on, strictly enough to catch real
integration bugs: PKCE is verified, refresh tokens rotate (a reused one is rejected),
expired access tokens get a 401, tickets honour Idempotency-Key, and the solved-ticket
webhook is signed the way Zendesk signs it.
"""

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp

from shopdesk.demo import demo_commerce


class HostRouter(httpx.AsyncBaseTransport):
    """Sends each outbound request to the in-process app registered for its host."""

    def __init__(self, routes: dict[str, ASGIApp]) -> None:
        self._routes = {host: httpx.ASGITransport(app=app) for host, app in routes.items()}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = self._routes.get(request.url.host)
        if transport is None:
            raise httpx.ConnectError(f"no stand-in for {request.url.host}", request=request)
        return await transport.handle_async_request(request)


class FakeZendesk:
    def __init__(self, client_id: str, client_secret: str, webhook_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.webhook_secret = webhook_secret
        self.codes: dict[str, dict[str, str]] = {}
        self.access: dict[str, datetime] = {}
        self.refresh: dict[str, str] = {}
        self.refreshes = 0
        self.tickets: dict[int, dict[str, Any]] = {}
        self.ticket_requests: list[httpx.Headers | dict[str, str]] = []
        self.idempotency: dict[str, int] = {}
        self.lose_next_create_response = False
        self.app = self._build()

    def expire_access_tokens(self) -> None:
        for token in self.access:
            self.access[token] = datetime.now(UTC) - timedelta(seconds=1)

    def solved_webhook(self, ticket_id: int) -> tuple[bytes, dict[str, str]]:
        ticket = self.tickets[ticket_id]
        body = json.dumps({"ticket_id": ticket_id, "external_id": ticket["external_id"],
                           "status": "solved"}).encode()
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = hmac.new(self.webhook_secret.encode(), stamp.encode() + body, hashlib.sha256)
        return body, {
            "X-Zendesk-Webhook-Signature": base64.b64encode(digest.digest()).decode(),
            "X-Zendesk-Webhook-Signature-Timestamp": stamp,
            "Content-Type": "application/json",
        }

    def _issue(self, body: dict[str, Any]) -> dict[str, Any]:
        access, refresh = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        expires_in = int(body.get("expires_in") or 3600)
        self.access[access] = datetime.now(UTC) + timedelta(seconds=expires_in)
        self.refresh[refresh] = access
        return {"access_token": access, "token_type": "bearer", "scope": body.get("scope", ""),
                "expires_in": expires_in, "refresh_token": refresh,
                "refresh_token_expires_in": int(body.get("refresh_token_expires_in") or 86400)}

    def _authorised(self, request: Request) -> None:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        expiry = self.access.get(token)
        if scheme.lower() != "bearer" or expiry is None or expiry <= datetime.now(UTC):
            raise HTTPException(401, "Couldn't authenticate you")

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.get("/oauth/authorizations/new")
        async def authorize(request: Request) -> RedirectResponse:
            q = request.query_params
            if q.get("client_id") != self.client_id or q.get("response_type") != "code":
                raise HTTPException(400, "invalid_client")
            if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
                raise HTTPException(400, "PKCE required")
            code = secrets.token_urlsafe(12)
            self.codes[code] = {"challenge": q["code_challenge"],
                                "redirect_uri": q["redirect_uri"], "scope": q.get("scope", "")}
            # The admin clicked "Allow".
            target = f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q['state']})}"
            return RedirectResponse(target, status_code=302)

        @app.post("/oauth/tokens")
        async def tokens(request: Request) -> JSONResponse:
            body = await request.json()
            if (body.get("client_id"), body.get("client_secret")) != (
                self.client_id, self.client_secret
            ):
                return JSONResponse({"error": "invalid_client"}, status_code=401)
            if body["grant_type"] == "authorization_code":
                grant = self.codes.pop(body.get("code", ""), None)
                verifier = body.get("code_verifier", "")
                challenge = base64.urlsafe_b64encode(
                    hashlib.sha256(verifier.encode()).digest()
                ).rstrip(b"=").decode()
                if (grant is None or grant["redirect_uri"] != body.get("redirect_uri")
                        or grant["challenge"] != challenge):
                    return JSONResponse({"error": "invalid_grant"}, status_code=400)
                return JSONResponse(self._issue(body | {"scope": grant["scope"]}))
            if body["grant_type"] == "refresh_token":
                old_access = self.refresh.pop(body.get("refresh_token", ""), None)
                if old_access is None:
                    return JSONResponse({"error": "invalid_grant"}, status_code=400)
                self.access.pop(old_access, None)
                self.refreshes += 1
                return JSONResponse(self._issue(body))
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

        @app.post("/api/v2/tickets.json")
        async def create_ticket(request: Request) -> JSONResponse:
            self._authorised(request)
            self.ticket_requests.append(dict(request.headers))
            key = request.headers.get("idempotency-key")
            if key and key in self.idempotency:
                ticket = self.tickets[self.idempotency[key]]
                return JSONResponse({"ticket": ticket}, status_code=201,
                                    headers={"x-idempotency-lookup": "hit"})
            payload = (await request.json())["ticket"]
            ticket_id = len(self.tickets) + 1
            self.tickets[ticket_id] = {
                "id": ticket_id,
                "requester_id": 501,
                "subject": payload["subject"],
                "priority": payload.get("priority"),
                "tags": payload.get("tags", []),
                "external_id": payload.get("external_id"),
                "requester": payload.get("requester"),
                "comments": [payload["comment"]],
                "status": "new",
            }
            if key:
                self.idempotency[key] = ticket_id
            if self.lose_next_create_response:
                # Ticket exists, but the client never hears back.
                self.lose_next_create_response = False
                return JSONResponse({"error": "upstream timeout"}, status_code=503)
            return JSONResponse({"ticket": self.tickets[ticket_id]}, status_code=201)

        @app.get("/api/v2/tickets/show_many.json")
        async def show_many(request: Request, ids: str = "") -> JSONResponse:
            self._authorised(request)
            wanted = [int(i) for i in ids.split(",") if i]
            return JSONResponse({"tickets": [self.tickets[i] for i in wanted
                                             if i in self.tickets]})

        @app.put("/api/v2/tickets/{ticket_id}.json")
        async def update_ticket(ticket_id: int, request: Request) -> JSONResponse:
            self._authorised(request)
            if ticket_id not in self.tickets:
                raise HTTPException(404)
            comment = (await request.json())["ticket"]["comment"]
            self.tickets[ticket_id]["comments"].append(comment)
            return JSONResponse({"ticket": self.tickets[ticket_id]})

        return app


class FakeShop:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        # A correct backend scopes by the customer in the path. Flip this to simulate one
        # that doesn't, and check the agent still refuses to leak.
        self.scoped = True
        self.requests: list[tuple[str, str]] = []
        demo = demo_commerce()
        self._orders = demo._orders
        self._accounts = demo._accounts
        self.app = self._build()

    def _build(self) -> FastAPI:
        app = FastAPI()

        def check(request: Request) -> None:
            self.requests.append((request.url.path, request.headers.get("authorization", "")))
            if request.headers.get("authorization") != f"Bearer {self.api_key}":
                raise HTTPException(401)

        @app.get("/api/customers/{customer_id}/orders/{order_id}")
        async def order(customer_id: str, order_id: str, request: Request) -> dict[str, Any]:
            check(request)
            found = self._orders.get(order_id)
            if found is None or (self.scoped and found.customer_id != customer_id):
                raise HTTPException(404)
            return found.model_dump(mode="json")

        @app.get("/api/customers/{customer_id}/orders")
        async def orders(customer_id: str, request: Request, limit: int = 5) -> dict[str, Any]:
            check(request)
            mine = [o for o in self._orders.values() if o.customer_id == customer_id]
            mine.sort(key=lambda o: o.placed_at, reverse=True)
            return {"orders": [o.model_dump(mode="json") for o in mine[:limit]]}

        @app.get("/api/customers/{customer_id}")
        async def account(customer_id: str, request: Request) -> dict[str, Any]:
            check(request)
            found = self._accounts.get(customer_id)
            if found is None:
                raise HTTPException(404)
            return found.model_dump(mode="json")

        return app
