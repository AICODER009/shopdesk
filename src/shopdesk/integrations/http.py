import asyncio
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


class UpstreamError(Exception):
    def __init__(self, service: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{service}: {message}")
        self.service = service
        self.status = status


def retry_after_seconds(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _backoff(attempt: int, base: float, cap: float) -> float:
    return random.uniform(0, min(cap, base * 2**attempt))  # noqa: S311 - jitter, not crypto


async def request_with_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    attempts: int = 4,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
    retry_unsafe: bool = False,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    **kwargs: Any,
) -> httpx.Response:
    """Send a request, retrying transient failures.

    Non-idempotent requests are only retried when we know the server didn't act on them
    (connect errors, 429) or the caller sent an Idempotency-Key / opted in explicitly.
    Returns the last response; callers decide what a 4xx means for them.
    """
    headers = {k.lower() for k in (kwargs.get("headers") or {})}
    safe = method.upper() in IDEMPOTENT_METHODS or retry_unsafe or "idempotency-key" in headers

    for attempt in range(1, attempts + 1):
        last = attempt == attempts
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            not_sent = isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)
            if last or not (safe or not_sent):
                raise UpstreamError(httpx.URL(url).host, type(exc).__name__) from exc
            delay = _backoff(attempt, base_delay, max_delay)
            log.warning("http.retry", url=url, attempt=attempt, error=type(exc).__name__)
            await sleep(delay)
            continue

        if resp.status_code not in RETRYABLE_STATUS or last:
            return resp
        if not (safe or resp.status_code == 429):
            return resp

        hinted = retry_after_seconds(resp)
        if hinted is not None and hinted > max_delay:
            # Waiting a minute inside a chat request is worse than failing over to a fallback.
            return resp
        delay = hinted if hinted is not None else _backoff(attempt, base_delay, max_delay)
        log.warning("http.retry", url=url, attempt=attempt, status=resp.status_code, delay=delay)
        await sleep(delay)

    raise AssertionError("unreachable")


def ensure_ok(resp: httpx.Response, service: str) -> httpx.Response:
    if resp.is_success:
        return resp
    raise UpstreamError(service, f"HTTP {resp.status_code}", status=resp.status_code)
