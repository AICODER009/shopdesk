import logging
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog
from langfuse import Langfuse

from shopdesk.config import Settings
from shopdesk.privacy import scrub


def _scrub_event(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> Mapping[str, Any]:
    return scrub(event_dict)  # type: ignore[no-any-return]


def configure_logging(settings: Settings) -> None:
    renderer: Any = (
        structlog.dev.ConsoleRenderer()
        if settings.env == "dev"
        else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.format_exc_info,
            _scrub_event,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )


def _mask(*, data: Any, **_: Any) -> Any:
    return scrub(data)


def configure_tracing(settings: Settings) -> Langfuse:
    """Langfuse is a sub-processor: everything leaving the process goes through `scrub` first."""
    return Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=(
            settings.langfuse_secret_key.get_secret_value()
            if settings.langfuse_secret_key
            else None
        ),
        base_url=settings.langfuse_base_url,
        tracing_enabled=settings.langfuse_enabled,
        environment=settings.env,
        mask=_mask,
    )
