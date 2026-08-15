"""Mounting the MCP endpoint without a trailing-slash redirect.

Starlette's `Mount` redirects `/mcp` to `/mcp/`. That is invisible with a
browser and fatal with an MCP client: the hub deliberately configures its
upstream clients not to follow redirects (arch §31 — a 302 would carry the
caller's `Authorization` header to whatever host the redirect names), and other
clients make the same choice. A hub that only answers on `/mcp/` when every
client asks for `/mcp` is broken for the exact reason it is secure.

So the endpoint is registered as a `Route` with an ASGI-app endpoint, matching
the configured path exactly. This mirrors what the SDK's own
`streamable_http_app` does, and it is why `AsgiEndpoint` is a class rather than a
function: Starlette treats a plain async function as a request/response handler
and only passes raw ASGI through for a non-function callable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.routing import Route
from starlette.types import Receive, Scope, Send

__all__ = ["AsgiEndpoint", "mcp_route"]

AsgiHandler = Callable[[Scope, Receive, Send], Awaitable[None]]


class AsgiEndpoint:
    """Wraps a raw ASGI handler so Starlette routes to it verbatim.

    Being a class instance is load-bearing: Starlette's `Route` inspects the
    endpoint and only skips its request/response adaptation when the endpoint is
    not a plain function. Passing the handler through unchanged is what lets
    Streamable HTTP use GET, POST, DELETE, and SSE on one path.
    """

    __slots__ = ("_handler",)

    def __init__(self, handler: AsgiHandler) -> None:
        self._handler = handler

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._handler(scope, receive, send)


def mcp_route(path: str, handler: AsgiHandler, *, name: str = "mcp") -> Route:
    """Build the exact-path route for the hub's single MCP endpoint (arch §10)."""
    return Route(path, endpoint=AsgiEndpoint(handler), name=name)
