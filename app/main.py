"""The ASGI application: one MCP endpoint plus the management API (arch §10, §26).

    /mcp          the single MCP endpoint an agent connects to (arch §10)
    /api/...      the management REST API (arch §26)
    /health       liveness, /ready readiness, /metrics Prometheus (arch §44)

Both surfaces sit behind one `RequestContextMiddleware`, so a bearer token means
the same thing to both and every request carries the same identity into policy,
credentials, and audit.

The MCP endpoint is a `Route`, not a `Mount` — see `app/server/asgi.py` for why
the difference is load-bearing rather than stylistic.

Startup and shutdown are bound to the ASGI lifespan. The Streamable HTTP session
manager can only be run once per process, so it lives here and nowhere else.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mcp.server.auth.provider import AccessToken
from starlette.types import Receive, Scope, Send

from app import __version__
from app.api import admin, health, integrations, registry
from app.api.deps import hub_error_handler, unhandled_error_handler
from app.auth.middleware import RequestContextMiddleware
from app.config.settings import Settings, load_settings
from app.core.errors import HubError
from app.core.logging import get_logger
from app.server.asgi import mcp_route
from app.server.runtime import HubRuntime

__all__ = ["create_app"]

log = get_logger(__name__)

_DESCRIPTION = """\
A single MCP endpoint fronting many governed integrations.

Agents connect to `/mcp` and see every enabled integration's tools namespaced as
`<integration>.<tool>`. This REST API is for operators: installing, updating,
enabling, and removing integrations, reading health, and reading the audit trail.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application.

    The runtime is created inside the lifespan rather than here, so importing
    this module has no side effects — `uvicorn app.main:app` and a test client
    both get a clean process.
    """
    resolved = settings or load_settings()

    @contextlib.asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        runtime = await HubRuntime.create(resolved)
        application.state.runtime = runtime
        await runtime.start()

        mcp_server = runtime.mcp_server
        # The session manager is single-use and must be running for the whole
        # application lifetime, so it is entered here rather than per request.
        async with mcp_server.lifespan():
            log.info(
                "hub.listening",
                mcp_endpoint=resolved.mcp_path,
                environment=resolved.env,
                integrations=len(runtime.manager.all()),
                tools=len(runtime.registry),
            )
            try:
                yield
            finally:
                await runtime.stop()

    application = FastAPI(
        title="MCP Hub",
        description=_DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    application.add_exception_handler(HubError, hub_error_handler)
    application.add_exception_handler(Exception, unhandled_error_handler)

    application.include_router(health.router)
    application.include_router(integrations.router)
    application.include_router(admin.router)
    application.include_router(registry.router)

    async def handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        """Forward to the MCP session manager owned by the runtime."""
        runtime: HubRuntime = application.state.runtime
        await runtime.mcp_server.handle_asgi(scope, receive, send)

    application.router.routes.append(mcp_route(resolved.mcp_path, handle_mcp))

    if resolved.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Mcp-Session-Id", "Mcp-Protocol-Version"],
            expose_headers=["Mcp-Session-Id", "X-Request-Id"],
        )

    # Added last so it runs outermost: identity must be bound before any route,
    # exception handler, or CORS response is produced.
    application.add_middleware(
        RequestContextMiddleware,
        verifier=_verifier_for(application),
        source="http",
    )

    return application


class _LazyVerifier:
    """Defers to the runtime's token verifier once the lifespan has built it.

    Implements the SDK's `TokenVerifier` protocol. The middleware stack is
    assembled before the runtime exists, so it holds this instead. Before startup
    completes there is nothing to verify against, and returning `None` means
    "unauthenticated" — the safe answer, not a permissive one.
    """

    def __init__(self, application: FastAPI) -> None:
        self._app = application

    async def verify_token(self, token: str) -> AccessToken | None:
        runtime: HubRuntime | None = getattr(self._app.state, "runtime", None)
        if runtime is None:
            return None
        return await runtime.verifier.verify_token(token)


def _verifier_for(application: FastAPI) -> _LazyVerifier:
    """The token verifier the middleware should consult."""
    return _LazyVerifier(application)


app = create_app()
"""The application `uvicorn app.main:app` serves."""


def main() -> None:
    """Run the hub with uvicorn (`python -m app.main`)."""
    import uvicorn

    settings = load_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_config=None,  # structlog owns logging (arch §44)
        access_log=False,
    )


if __name__ == "__main__":
    main()
