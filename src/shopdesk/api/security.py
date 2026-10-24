import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from shopdesk.agent.state import CustomerContext
from shopdesk.config import Settings

_basic = HTTPBasic(auto_error=False)


def settings_dep(request: Request) -> Settings:
    return request.app.state.resources.settings  # type: ignore[no-any-return]


def customer_from_token(token: str, settings: Settings) -> CustomerContext:
    if settings.widget_jwt_secret is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign-in is not configured")
    try:
        claims = jwt.decode(
            token,
            settings.widget_jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            audience=settings.widget_jwt_audience,
            options={"require": ["exp", "sub", "aud"]},
            leeway=30,
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from exc
    return CustomerContext(
        customer_id=str(claims["sub"]),
        email=claims.get("email"),
        first_name=claims.get("given_name"),
        locale=claims.get("locale", "en"),
    )


async def current_customer(
    settings: Annotated[Settings, Depends(settings_dep)],
    authorization: Annotated[str | None, Header()] = None,
) -> CustomerContext:
    """The storefront signs a short-lived JWT for logged-in shoppers; no token means guest."""
    if not authorization:
        return CustomerContext()
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "expected a bearer token")
    return customer_from_token(token, settings)


async def require_admin(
    settings: Annotated[Settings, Depends(settings_dep)],
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic)],
) -> None:
    expected = settings.admin_token.get_secret_value() if settings.admin_token else None
    if not expected:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin endpoints are disabled")
    if credentials is None or not secrets.compare_digest(credentials.password, expected):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": 'Basic realm="shopdesk"'}
        )


def verify_zendesk_signature(
    body: bytes, signature: str, timestamp: str, secret: str, max_skew: timedelta
) -> bool:
    try:
        sent = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if abs(datetime.now(UTC) - sent) > max_skew:
        return False
    digest = hmac.new(secret.encode(), timestamp.encode() + body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)
