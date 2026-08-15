"""The single MCP endpoint (arch §10).

One `Server` from the official SDK, two handlers, and a Streamable HTTP mount at
`/mcp`. The agent connects here and nowhere else; every integration behind it is
the hub's business, not the agent's (arch §10, §71).

Four wiring decisions carry weight:

**Handlers are thin.** `on_list_tools` and `on_call_tool` resolve the caller and
this connection's activation state, then hand off to the `Gateway`. All policy,
credential, and audit logic lives there, so it is exercised identically whichever
transport reaches it.

**Per-connection state, keyed by session.** Discovery-mode activations belong to
one agent connection, not the process. They are keyed by the MCP session id so
two agents connected at once cannot see each other's activations, and they are
dropped when the session goes away.

**Request state is sealed.** `RequestStateBoundary` is installed as middleware so
the `requestState` carrying a pending confirmation is encrypted, bound to the
method, tool, arguments and principal, and expires. Without it a client could
mint its own "already confirmed" token — arch §53's bypass, in one line.

**The tool list is dynamic.** `list_changed` is advertised and the notification
is sent whenever discovery or an activation changes what a session should see, so
an agent re-reads instead of holding a stale list (arch §12).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import mcp.types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.request_state import RequestStateBoundary, RequestStateSecurity
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.types import Receive, Scope, Send

from app import __version__
from app.auth.middleware import principal_from_scope
from app.core.context import ANONYMOUS, Principal
from app.core.logging import get_logger
from app.gateway.gateway import Gateway
from app.gateway.tool_router import SessionActivation

__all__ = ["HubMcpServer"]

log = get_logger(__name__)

_SERVER_NAME = "mcp-hub"
_INSTRUCTIONS = """\
This is an MCP Hub: a single endpoint fronting several integrations.

Tools are namespaced as `<integration>.<tool>` — for example `jira.get_issue` or
`github.search_code`. Start with `hub.list_integrations` to see what is available
and `hub.search_tools` to find the right tool without reading every schema.

Some tools require human confirmation before they run. When one does, you will be
asked to confirm; that is expected, not an error. If a call returns
`integration_unavailable`, that one upstream is down — the rest of the hub is
still usable.
"""


class HubMcpServer:
    """Wraps the SDK server, the session manager, and the ASGI mount."""

    def __init__(
        self,
        gateway: Gateway,
        *,
        request_state_keys: tuple[bytes, ...] = (),
        json_response: bool = False,
        session_idle_timeout: float | None = 1800.0,
    ) -> None:
        self._gateway = gateway
        self._activations: dict[str, SessionActivation] = {}
        self._server = self._build_server(request_state_keys)
        self._manager = StreamableHTTPSessionManager(
            app=self._server,
            json_response=json_response,
            stateless=False,
            session_idle_timeout=session_idle_timeout,
        )

    # ------------------------------------------------------------------ build

    def _build_server(self, request_state_keys: tuple[bytes, ...]) -> Server[None]:
        server: Server[None] = Server(
            _SERVER_NAME,
            version=__version__,
            title="MCP Hub",
            instructions=_INSTRUCTIONS,
            on_list_tools=self._on_list_tools,
            on_call_tool=self._on_call_tool,
        )
        # Derived from MCP_HUB_AUTH_SECRET when one is configured, so a pending
        # confirmation stays valid across replicas; otherwise the SDK's
        # process-local ephemeral key applies and a confirmation is pinned to the
        # replica that issued it.
        security = (
            RequestStateSecurity(keys=list(request_state_keys))
            if request_state_keys
            else RequestStateSecurity.ephemeral()
        )
        server.middleware.append(RequestStateBoundary(security, default_audience=_SERVER_NAME))
        return server

    @property
    def server(self) -> Server[None]:
        """The underlying SDK server, for tests using an in-memory client."""
        return self._server

    # --------------------------------------------------------------- handlers

    async def _on_list_tools(
        self,
        ctx: ServerRequestContext[None],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        """Answer `tools/list` for this caller (arch §11, §13)."""
        principal = _principal_of(ctx)
        activation = self._activation_for(ctx)
        tools = self._gateway.list_tools(principal=principal, activation=activation)
        return types.ListToolsResult(tools=tools)

    async def _on_call_tool(
        self,
        ctx: ServerRequestContext[None],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult | types.InputRequiredResult:
        """Answer `tools/call`, running the full gateway pipeline (arch §23)."""
        principal = _principal_of(ctx)
        activation = self._activation_for(ctx)

        outcome = await self._gateway.call_tool(
            params,
            principal=principal,
            activation=activation,
            session=ctx.session,
        )

        if outcome.tools_changed:
            # Best-effort: a client that cannot receive notifications still works,
            # it just re-lists on its own schedule.
            with contextlib.suppress(Exception):
                await ctx.session.send_tool_list_changed()

        return outcome.result

    # --------------------------------------------------------- session state

    def _activation_for(self, ctx: ServerRequestContext[None]) -> SessionActivation:
        """Per-connection activation state (arch §13).

        Keyed by MCP session id. A stateless connection with no session id falls
        back to a throwaway record, which is correct: without a session there is
        nothing for an activation to persist across.
        """
        session_id = _session_id(ctx)
        if session_id is None:
            return SessionActivation()
        activation = self._activations.get(session_id)
        if activation is None:
            activation = SessionActivation()
            self._activations[session_id] = activation
            self._evict_stale_activations()
        return activation

    def _evict_stale_activations(self, *, limit: int = 512) -> None:
        """Bound the activation map.

        The SDK does not hand us a session-closed hook, so this trims oldest-first
        once the map grows past a generous ceiling. Losing an activation costs an
        agent one `hub.activate_integration` call; leaking them costs memory
        forever.
        """
        if len(self._activations) <= limit:
            return
        excess = len(self._activations) - limit
        for key in list(self._activations)[:excess]:
            del self._activations[key]
        log.debug("mcp_server.activations_trimmed", removed=excess)

    async def notify_tools_changed(self) -> None:
        """Placeholder for a future broadcast to every live session.

        The SDK exposes notifications per session rather than per server, so a
        hub-wide broadcast needs a session registry. Until then, changes reach
        agents on their next `tools/list`, and any change an agent itself caused
        is notified inline in `_on_call_tool`.
        """
        return None

    # -------------------------------------------------------------- transport

    @contextlib.asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """Run the Streamable HTTP session manager for the app's lifetime.

        The manager is single-use — it cannot be restarted after its context
        exits — so it is bound to the ASGI application lifespan.
        """
        async with self._manager.run():
            log.info("mcp_server.ready", version=__version__)
            yield

    async def handle_asgi(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI entry point mounted at the hub's MCP path (arch §10)."""
        await self._manager.handle_request(scope, receive, send)


# --------------------------------------------------------------------------- helpers


def _principal_of(ctx: ServerRequestContext[None]) -> Principal:
    """Resolve the caller for this MCP request.

    The context var bound by `RequestContextMiddleware` is authoritative; the
    ASGI scope on the transport-attached request is the fallback for handlers
    running in a task the context var did not propagate to.
    """
    from app.core.context import current_principal

    principal = current_principal()
    if principal.is_authenticated:
        return principal

    request: Any = ctx.request
    scope = getattr(request, "scope", None)
    if isinstance(scope, dict):
        return principal_from_scope(scope)
    return ANONYMOUS


def _session_id(ctx: ServerRequestContext[None]) -> str | None:
    """The MCP session id for this connection, if the transport assigned one."""
    session_id = getattr(ctx.session, "session_id", None)
    if isinstance(session_id, str) and session_id:
        return session_id
    request: Any = ctx.request
    headers = getattr(request, "headers", None)
    if headers is not None:
        value = headers.get("mcp-session-id")
        if isinstance(value, str) and value:
            return value
    return None
