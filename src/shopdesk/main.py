import asyncio
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import SecretStr

from shopdesk.agent.handback import poll_forever
from shopdesk.api import dev
from shopdesk.api.routes import router
from shopdesk.config import Settings, get_settings
from shopdesk.observability import configure_logging, configure_tracing
from shopdesk.wiring import Resources

ResourceFactory = Callable[[Settings], AbstractAsyncContextManager[Resources]]


def create_app(
    settings: Settings | None = None, open_resources: ResourceFactory = Resources.open
) -> FastAPI:
    settings = settings or get_settings()
    if settings.env == "dev" and settings.widget_jwt_secret is None:
        # Lets the demo storefront sign shopper tokens without extra setup.
        settings = settings.model_copy(
            update={"widget_jwt_secret": SecretStr(secrets.token_urlsafe(32))}
        )
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        langfuse = configure_tracing(settings)
        async with open_resources(settings) as res:
            app.state.resources = res
            poller = None
            if settings.zendesk_handback_poll_seconds > 0 and settings.zendesk_subdomain:
                poller = asyncio.create_task(poll_forever(
                    res.service, res.ticket_sink, res.service.open_handoffs,
                    settings.zendesk_handback_poll_seconds,
                ))
            yield
            if poller:
                poller.cancel()
        langfuse.shutdown()

    app = FastAPI(
        title="shopdesk",
        lifespan=lifespan,
        docs_url="/docs" if settings.env == "dev" else None,
        redoc_url=None,
    )
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["POST", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
        )
    app.include_router(router)
    widget = Path(__file__).parent / "static" / "widget"
    if widget.is_dir():
        app.mount("/widget", StaticFiles(directory=widget), name="widget")
    if settings.env == "dev":
        app.include_router(dev.router)
        app.mount("/dev/assets", StaticFiles(directory=dev.ASSETS), name="dev-assets")
    return app


app = create_app()
