"""Binding identity to a request (arch §20, §23).

One ASGI middleware sits in front of both the MCP endpoint and the REST API. It
verifies the bearer token once, resolves it to a `Principal`, and binds a
`RequestContext` for the life of the request. Everything downstream —
credentials, policy, audit, logging — reads that context instead of re-parsing
headers, so there is exactly one place where "who is calling" is decided.

Where the token comes from matters. The `Authorization: Bearer` header is the
only accepted channel: query parameters land in access logs, proxy logs, and
browser history, and MCP clients all send the header.

Unauthenticated requests are *not* rejected here. They proceed as `ANONYMOUS`,
and the policy engine decides whether that is acceptable for the specific thing
being attempted — arch §22's `require_authentication` and per-integration rules
are a finer instrument than a blanket 401, and health probes must work without a
credential.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import ANONYMOUS, Principal, RequestContext, bind_request
from app.core.ids import new_request_id
from app.core.logging import get_logger

if TYPE_CHECKING:
    from mcp.server.auth.provider import TokenVerifier

__all__ = ["REQUEST_ID_HEADER", "RequestContextMiddleware", "principal_from_scope"]

log = get_logger(__name__)

REQUEST_ID_HEADER = "x-request-id"
"""Echoed on every response so a caller can quote it in a bug report."""

_SCOPE_KEY = "mcp_hub.request_context"


class RequestContextMiddleware:
    """Authenticates the caller and binds the per-request context."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        verifier: TokenVerifier | None = None,
        source: str = "api",
    ) -> None:
        self.app = app
        self._verifier = verifier
        self._source = source

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = headers.get(REQUEST_ID_HEADER) or new_request_id()
        principal = await self._authenticate(headers)

        context = RequestContext(
            request_id=request_id,
            principal=principal,
            trace_id=headers.get("x-trace-id"),
            source=self._source,
            client_ip=_client_ip(scope, headers),
        )
        scope[_SCOPE_KEY] = context

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = list(message.get("headers", []))
                raw.append((REQUEST_ID_HEADER.encode(), request_id.encode()))
                message = {**message, "headers": raw}
            await send(message)

        with bind_request(context):
            await self.app(scope, receive, send_with_request_id)

    async def _authenticate(self, headers: Headers) -> Principal:
        """Resolve the caller, or `ANONYMOUS` if no valid credential is present."""
        if self._verifier is None:
            return ANONYMOUS
        authorization = headers.get("authorization")
        if not authorization:
            return ANONYMOUS
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return ANONYMOUS

        from app.auth.tokens import principal_from_access_token

        access_token = await self._verifier.verify_token(token.strip())
        principal = principal_from_access_token(access_token)
        if principal.is_authenticated:
            log.debug("auth.request_authenticated", subject=principal.subject, scopes=sorted(principal.scopes))
        return principal


def principal_from_scope(scope: Scope) -> Principal:
    """Read the principal bound to an ASGI scope.

    Used where the request context is unavailable — a handler reached through a
    transport that does not preserve context variables.
    """
    context = scope.get(_SCOPE_KEY)
    if isinstance(context, RequestContext):
        return context.principal
    return ANONYMOUS


def request_context_from_scope(scope: Scope) -> RequestContext | None:
    """Read the whole request context from an ASGI scope, if one was bound."""
    context = scope.get(_SCOPE_KEY)
    return context if isinstance(context, RequestContext) else None


def _client_ip(scope: Scope, headers: Headers) -> str | None:
    """Best-effort peer address for the audit record.

    `X-Forwarded-For` is honoured because the hub normally sits behind a load
    balancer (arch §43). It is attacker-controlled when the hub is exposed
    directly, so it is used for audit only and never for an authorization
    decision.
    """
    forwarded = headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = scope.get("client")
    if isinstance(client, tuple | list) and client:
        return str(client[0])
    return None


AsgiHandler = Callable[[Scope, Receive, Send], Awaitable[None]]
