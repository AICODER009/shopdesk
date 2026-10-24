import uuid
from datetime import timedelta
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from shopdesk.agent.dead_letters import replay
from shopdesk.agent.state import Citation, CustomerContext
from shopdesk.api.security import current_customer, require_admin, verify_zendesk_signature
from shopdesk.integrations.zendesk.oauth import ZendeskAuthError
from shopdesk.wiring import Resources

log = structlog.get_logger()
router = APIRouter()

OAUTH_COOKIE = "zd_oauth_nonce"


def resources(request: Request) -> Resources:
    return request.app.state.resources  # type: ignore[no-any-return]


Res = Annotated[Resources, Depends(resources)]
Customer = Annotated[CustomerContext, Depends(current_customer)]


class ChatRequest(BaseModel):
    conversation_id: uuid.UUID | None = None
    message: str = Field(min_length=1, max_length=4000)


class ChatResponse(BaseModel):
    conversation_id: uuid.UUID
    status: Literal["answered", "declined", "handed_off", "blocked", "error"]
    reply: str
    citations: list[Citation]
    ticket_id: int | None = None


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/v1/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, customer: Customer, res: Res) -> ChatResponse:
    conversation_id = body.conversation_id or uuid.uuid4()
    try:
        outcome = await res.service.send(customer, str(conversation_id), body.message)
    except Exception:
        log.exception("chat.unhandled")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "The assistant is unavailable, please try again shortly.") from None
    return ChatResponse(
        conversation_id=conversation_id,
        status=outcome.status,
        reply=outcome.reply,
        citations=outcome.citations,
        ticket_id=outcome.ticket.id if outcome.ticket else None,
    )


@router.delete("/v1/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def erase_conversation(conversation_id: uuid.UUID, customer: Customer, res: Res) -> None:
    await res.service.forget(customer, str(conversation_id))


# -- Zendesk connection (admin) -------------------------------------------------------------

@router.get("/v1/zendesk/oauth/start", dependencies=[Depends(require_admin)])
async def zendesk_oauth_start(res: Res) -> RedirectResponse:
    if res.oauth is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Zendesk OAuth is not configured")
    url, nonce = res.oauth.begin()
    response = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        OAUTH_COOKIE, nonce, max_age=600, httponly=True, samesite="lax",
        secure=res.settings.env != "dev", path="/v1/zendesk/oauth",
    )
    return response


@router.get("/v1/zendesk/oauth/callback")
async def zendesk_oauth_callback(
    request: Request,
    res: Res,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> dict[str, str]:
    if res.oauth is None or not res.settings.zendesk_subdomain:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if error or not code or not state:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"authorization failed: {error}")
    try:
        token = await res.oauth.complete(
            code=code, state=state, nonce=request.cookies.get(OAUTH_COOKIE)
        )
    except ZendeskAuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await res.credentials.save(res.settings.zendesk_subdomain, token)
    log.info("zendesk.connected", scope=token.scope)
    return {"status": "connected", "scope": token.scope}


@router.get("/v1/admin/zendesk/status", dependencies=[Depends(require_admin)])
async def zendesk_status(res: Res) -> dict[str, object]:
    sub = res.settings.zendesk_subdomain
    token = await res.credentials.load(sub) if sub else None
    return {
        "subdomain": sub,
        "mode": res.settings.zendesk_auth_mode,
        "connected": token is not None,
        "scope": token.scope if token else None,
        "expires_at": token.expires_at if token else None,
        "refresh_expires_at": token.refresh_expires_at if token else None,
    }


# -- Zendesk -> us: ticket solved, hand the conversation back -------------------------------

class TicketEvent(BaseModel):
    """Body of the Zendesk webhook; configure the trigger to send these fields."""

    ticket_id: int
    external_id: str | None = None
    status: str


@router.post("/v1/zendesk/webhook", status_code=status.HTTP_202_ACCEPTED)
async def zendesk_webhook(
    request: Request,
    res: Res,
    signature: Annotated[str, Header(alias="X-Zendesk-Webhook-Signature")],
    timestamp: Annotated[str, Header(alias="X-Zendesk-Webhook-Signature-Timestamp")],
) -> Response:
    secret = res.settings.zendesk_webhook_secret
    body = await request.body()
    if secret is None or not verify_zendesk_signature(
        body, signature, timestamp, secret.get_secret_value(), timedelta(minutes=5)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad signature")

    event = TicketEvent.model_validate_json(body)
    if event.status.lower() in ("solved", "closed") and event.external_id:
        resumed = await res.service.resolve_handoff(event.external_id, event.ticket_id,
                                                    event.status.lower())
        log.info("zendesk.webhook", ticket_id=event.ticket_id, resumed=resumed)
    return Response(status_code=status.HTTP_202_ACCEPTED)


# -- Dead letters (admin) --------------------------------------------------------------------

@router.get("/v1/admin/dead-letters", dependencies=[Depends(require_admin)])
async def dead_letter_counts(res: Res) -> dict[str, int]:
    return await res.dead_letters.counts()


@router.post("/v1/admin/dead-letters/replay", dependencies=[Depends(require_admin)])
async def dead_letter_replay(res: Res) -> dict[str, int]:
    return await replay(res.dead_letters, res.ticket_sink)
