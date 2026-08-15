"""Serving the hub itself over HTTP, for end-to-end tests.

Arch §41 wants the agent to reach the hub through one endpoint, the way a real
client does. That means a real Streamable HTTP listener rather than an in-memory
shortcut: session ids, the `RequestStateBoundary`, the auth middleware, and the
confirmation round trip all behave differently in memory, and those are exactly
the parts worth testing.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import mcp.types as types
import uvicorn
from mcp.client.client import Client
from mcp.client.session import ClientRequestContext
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from starlette.applications import Starlette
from starlette.types import Receive, Scope, Send

from app.auth.middleware import RequestContextMiddleware
from app.server.asgi import mcp_route
from app.server.runtime import HubRuntime

__all__ = ["agent_client", "serve_hub"]


@contextlib.asynccontextmanager
async def serve_hub(runtime: HubRuntime) -> AsyncIterator[str]:
    """Serve a runtime's MCP endpoint on an ephemeral loopback port.

    Yields the full URL an agent should connect to.
    """
    mcp_server = runtime.mcp_server

    async def handle(scope: Scope, receive: Receive, send: Send) -> None:
        await mcp_server.handle_asgi(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with mcp_server.lifespan():
            yield

    app = Starlette(routes=[mcp_route(runtime.settings.mcp_path, handle)], lifespan=lifespan)
    # Same middleware production installs, so tokens are verified identically.
    wrapped = RequestContextMiddleware(app, verifier=runtime.verifier, source="mcp")

    config = uvicorn.Config(wrapped, host="127.0.0.1", port=0, log_level="error", access_log=False)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        port = await _wait_for_port(server)
        yield f"http://127.0.0.1:{port}{runtime.settings.mcp_path}"
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(task, timeout=10.0)


@contextlib.asynccontextmanager
async def agent_client(
    url: str,
    *,
    token: str | None = None,
    confirm: bool | None = True,
    supports_elicitation: bool = True,
) -> AsyncIterator[Client]:
    """Connect to the hub the way an agent does.

    Args:
        url: The hub's MCP endpoint.
        token: Bearer token to present.
        confirm: How to answer confirmation prompts — `True` accepts, `False`
            declines, `None` cancels.
        supports_elicitation: When false, no elicitation callback is registered,
            so the client declares no elicitation capability. That is the case
            arch §53 cares about: a client that cannot be asked must be refused,
            not silently allowed.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    # The parameter names are part of the SDK's `ElicitationFnT` protocol, which
    # declares them positional-or-keyword — renaming them stops it matching.
    async def elicitation_callback(
        context: ClientRequestContext, params: types.ElicitRequestParams
    ) -> types.ElicitResult:
        if confirm is None:
            return types.ElicitResult(action="cancel")
        if confirm is False:
            return types.ElicitResult(action="decline")
        return types.ElicitResult(action="accept", content={"confirm": True})

    http_client = create_mcp_http_client(headers=headers)
    async with http_client:
        client = Client(
            streamable_http_client(url, http_client=http_client),
            elicitation_callback=elicitation_callback if supports_elicitation else None,
        )
        async with client:
            yield client


async def _wait_for_port(server: uvicorn.Server, *, timeout: float = 10.0) -> int:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if server.started and server.servers:
            sockets = server.servers[0].sockets
            if sockets:
                return int(sockets[0].getsockname()[1])
        await asyncio.sleep(0.02)
    raise TimeoutError("Hub did not start within its timeout.")
