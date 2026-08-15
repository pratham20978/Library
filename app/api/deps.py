"""FastAPI dependencies and error translation.

The runtime is attached to the application state at startup and read from the
request here, so no module-level singleton exists and a test can run several
independent hubs in one process.

Authorization is a dependency rather than a check inside each handler. Arch §64
requires administrative endpoints to be protected separately from tool access,
and a `Depends(require(SCOPE_INTEGRATIONS_WRITE))` on the route is auditable at a
glance — a handler that forgot its check is visible in the routing table, not
buried in a function body.

`HubError` becomes an HTTP response through one exception handler, so the status
code for `integration_not_found` is decided once rather than at forty call sites.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.responses import JSONResponse

from app.core.context import ANONYMOUS, Principal
from app.core.errors import HubError, NotAuthenticated
from app.core.logging import get_logger
from app.server.runtime import HubRuntime

__all__ = ["CurrentPrincipal", "Runtime", "hub_error_handler", "require", "unhandled_error_handler"]

log = get_logger(__name__)


def get_runtime(request: Request) -> HubRuntime:
    """The hub runtime bound to this application."""
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:  # pragma: no cover - only reachable if startup failed
        raise HubError("The hub runtime is not available; the application failed to start.")
    assert isinstance(runtime, HubRuntime)
    return runtime


def get_principal(request: Request) -> Principal:
    """The authenticated caller, resolved by `RequestContextMiddleware`."""
    from app.auth.middleware import principal_from_scope

    return principal_from_scope(request.scope) or ANONYMOUS


Runtime = Annotated[HubRuntime, Depends(get_runtime)]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def require(*scopes: str) -> Callable[[Principal, HubRuntime], Principal]:
    """Build a dependency asserting the caller holds one of `scopes` (arch §64).

    Raises:
        NotAuthenticated: No credential was presented and auth is enabled.
        NotAuthorized: The caller holds none of the required scopes.
    """

    def dependency(
        principal: CurrentPrincipal,
        runtime: Runtime,
    ) -> Principal:
        if not runtime.settings.auth_required:
            # A closed development loop with auth off. The warning is on the
            # settings themselves, not repeated per request.
            return principal
        if not principal.is_authenticated:
            raise NotAuthenticated("This endpoint requires an Authorization: Bearer <token> header.")
        from app.auth.permissions import require_any_scope

        require_any_scope(principal, *scopes)
        return principal

    return dependency


# --------------------------------------------------------------------------- errors


async def hub_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render a `HubError` using its own status and machine-readable code."""
    assert isinstance(exc, HubError)
    headers: dict[str, str] = {}
    retry_after = getattr(exc, "retry_after_seconds", None)
    if retry_after:
        headers["Retry-After"] = str(max(1, int(retry_after)))

    if exc.http_status >= 500:
        log.error("api.error", code=exc.code, message=exc.message, path=request.url.path)
    else:
        log.info("api.rejected", code=exc.code, message=exc.message, path=request.url.path)

    return JSONResponse(status_code=exc.http_status, content=exc.to_payload(), headers=headers)


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render an unexpected exception without leaking its detail.

    The message and traceback go to the log; the caller gets a request id to
    quote. An exception string can contain a file path, a query, or a credential
    fragment, none of which belong in an HTTP response.
    """
    from app.core.context import current_request

    request_id = current_request().request_id
    log.exception(
        "api.unhandled",
        error=f"{type(exc).__name__}: {exc}",
        path=request.url.path,
        request_id=request_id,
    )
    return JSONResponse(
        status_code=500,
        content={
            "code": "internal_error",
            "message": "The hub failed to process this request. The failure has been logged.",
            "details": {"request_id": request_id},
        },
    )


def ok(payload: Any) -> dict[str, Any]:
    """Wrap a payload in the API's standard success envelope."""
    return {"ok": True, "data": payload}
