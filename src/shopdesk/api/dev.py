"""Dev-only stand-in for the client's storefront: a product page that embeds the widget and
a token endpoint that signs shopper JWTs the way the real shop backend would. Never
mounted outside ENV=dev."""

from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Annotated

import jwt
from fastapi import APIRouter, Cookie, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from shopdesk.api.security import settings_dep
from shopdesk.config import Settings
from shopdesk.demo import DEMO_CUSTOMER

router = APIRouter(include_in_schema=False)
COOKIE = "demo_shopper"
ASSETS = Path(__file__).resolve().parent.parent / "static" / "storefront"
STOREFRONT = ASSETS / "index.html"
WIDGET_JS = ASSETS.parent / "widget" / "shopdesk-widget.js"


@router.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse("/dev/storefront")


@router.get("/dev/token")
async def shopper_token(
    settings: Annotated[Settings, Depends(settings_dep)],
    demo_shopper: Annotated[str | None, Cookie()] = None,
) -> dict[str, str | None]:
    if demo_shopper != DEMO_CUSTOMER or settings.widget_jwt_secret is None:
        return {"token": None}
    claims = {
        "sub": DEMO_CUSTOMER,
        "email": "alex@example.com",
        "given_name": "Alex",
        "aud": settings.widget_jwt_audience,
        "exp": datetime.now(UTC) + timedelta(minutes=15),
    }
    token = jwt.encode(claims, settings.widget_jwt_secret.get_secret_value(), algorithm="HS256")
    return {"token": token}


@router.get("/dev/sign-in")
async def sign_in() -> RedirectResponse:
    resp = RedirectResponse("/dev/storefront?fresh=1", status_code=303)
    resp.set_cookie(COOKIE, DEMO_CUSTOMER, httponly=True, samesite="lax")
    return resp


@router.get("/dev/sign-out")
async def sign_out() -> RedirectResponse:
    resp = RedirectResponse("/dev/storefront?fresh=1", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@router.get("/dev/storefront", response_class=HTMLResponse)
async def storefront(
    request: Request,
    settings: Annotated[Settings, Depends(settings_dep)],
    demo_shopper: Annotated[str | None, Cookie()] = None,
) -> str:
    signed_in = demo_shopper == DEMO_CUSTOMER
    if signed_in:
        account = 'Signed in as <strong>Alex</strong> · <a href="/dev/sign-out">Sign out</a>'
        hint = ("You're signed in as Alex, so order lookups work. Sign out to see how the "
                "assistant treats guests.")
    else:
        account = 'Guest · <a href="/dev/sign-in">Sign in as Alex (demo)</a>'
        hint = ("You're browsing as a guest: policy questions work, order lookups ask you to "
                "sign in. Use the demo sign-in at the top to try orders and handoffs.")
    replacements = {
        "__BRAND__": escape(settings.brand_name),
        "__ACCOUNT__": account,
        "__DEMO_HINT__": hint,
        "__API_BASE__": escape(str(request.base_url).rstrip("/"), quote=True),
        # Bust the browser cache whenever the widget is rebuilt.
        "__WIDGET_VERSION__": str(int(WIDGET_JS.stat().st_mtime)) if WIDGET_JS.exists() else "0",
    }
    page = STOREFRONT.read_text(encoding="utf-8")
    for marker, value in replacements.items():
        page = page.replace(marker, value)
    return page
