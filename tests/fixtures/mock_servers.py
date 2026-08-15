"""Mock upstream MCP servers (arch §42).

Real `mcp.server.lowlevel.Server` instances, served over real Streamable HTTP on
a loopback port. Nothing about the hub's path is stubbed: the tests exercise the
same transport, handshake, session pool, and proxy code that production uses.

That matters more than it sounds. A fake that returns canned `Tool` objects would
pass while the actual wire format, namespacing, or session lifetime was broken.
These servers make the end-to-end tests arch §41 asks for genuinely end-to-end.

No real credentials appear here (arch §42). `mock_jira` records the
`Authorization` header it was sent so a test can assert the *right* token
arrived, which is how per-user credential isolation is verified without any
token being real.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import mcp.types as types
import uvicorn
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.types import Receive, Scope, Send

from app.server.asgi import mcp_route

__all__ = [
    "MockUpstream",
    "build_failing_server",
    "build_mock_figma",
    "build_mock_jira",
    "build_mock_search",
    "build_slow_server",
    "serve_mcp",
]


@dataclass
class CallRecord:
    """One call an upstream received, for assertions."""

    tool: str
    arguments: dict[str, Any]
    authorization: str | None


@dataclass
class MockState:
    """Observable state shared with the test."""

    calls: list[CallRecord] = field(default_factory=list)
    extra_tool_visible: bool = False
    """Flip to true to simulate an upstream adding a tool between refreshes."""

    fail_next: bool = False
    """Flip to true to make the next call return an error result."""


# --------------------------------------------------------------------------- servers


def build_mock_jira(state: MockState | None = None) -> tuple[Server[None], MockState]:
    """A Jira-shaped upstream: reads, a write, and a destructive delete."""
    shared = state or MockState()

    async def on_list_tools(
        ctx: ServerRequestContext[None], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        tools = [
            types.Tool(
                name="searchJiraIssuesUsingJql",
                description="Search issues with JQL.",
                input_schema={
                    "type": "object",
                    "properties": {"jql": {"type": "string"}},
                    "required": ["jql"],
                },
                annotations=types.ToolAnnotations(read_only_hint=True),
            ),
            types.Tool(
                name="getJiraIssue",
                description="Fetch one issue by key.",
                input_schema={
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                },
            ),
            types.Tool(
                name="createJiraIssue",
                description="Create an issue.",
                input_schema={
                    "type": "object",
                    "properties": {"summary": {"type": "string"}, "project": {"type": "string"}},
                    "required": ["summary"],
                },
            ),
            types.Tool(
                name="deleteJiraIssue",
                description="Permanently delete an issue.",
                input_schema={
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                },
                annotations=types.ToolAnnotations(destructive_hint=True),
            ),
        ]
        if shared.extra_tool_visible:
            tools.append(
                types.Tool(
                    name="getJiraProjects",
                    description="List projects the caller can see.",
                    input_schema={"type": "object", "properties": {}},
                    annotations=types.ToolAnnotations(read_only_hint=True),
                )
            )
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(
        ctx: ServerRequestContext[None], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        arguments = dict(params.arguments or {})
        shared.calls.append(
            CallRecord(tool=params.name, arguments=arguments, authorization=_header(ctx, "authorization"))
        )
        if shared.fail_next:
            shared.fail_next = False
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="upstream exploded")], is_error=True
            )
        match params.name:
            case "searchJiraIssuesUsingJql":
                return _text(f"2 issues match {arguments.get('jql', '')!r}: HUB-1, HUB-2")
            case "getJiraIssue":
                return _text(f"{arguments.get('key')}: Build the MCP hub (In Progress)")
            case "createJiraIssue":
                return _text(f"Created HUB-99: {arguments.get('summary')}")
            case "deleteJiraIssue":
                return _text(f"Deleted {arguments.get('key')}")
            case "getJiraProjects":
                return _text("HUB, OPS")
            case _:
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text=f"unknown tool {params.name}")],
                    is_error=True,
                )

    server: Server[None] = Server(
        "mock-jira", version="1.0.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool
    )
    return server, shared


def build_mock_figma(state: MockState | None = None) -> tuple[Server[None], MockState]:
    """A read-only upstream, for asserting that nothing needs confirmation."""
    shared = state or MockState()

    async def on_list_tools(
        ctx: ServerRequestContext[None], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="get_design_context",
                    description="Design context for a node.",
                    input_schema={
                        "type": "object",
                        "properties": {"node_id": {"type": "string"}},
                        "required": ["node_id"],
                    },
                    annotations=types.ToolAnnotations(read_only_hint=True),
                ),
                types.Tool(
                    name="get_screenshot",
                    description="Render a node to an image.",
                    input_schema={"type": "object", "properties": {"node_id": {"type": "string"}}},
                    annotations=types.ToolAnnotations(read_only_hint=True),
                ),
            ]
        )

    async def on_call_tool(
        ctx: ServerRequestContext[None], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        shared.calls.append(
            CallRecord(
                tool=params.name,
                arguments=dict(params.arguments or {}),
                authorization=_header(ctx, "authorization"),
            )
        )
        return _text(f"figma:{params.name}")

    server: Server[None] = Server(
        "mock-figma", version="1.0.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool
    )
    return server, shared


def build_mock_search(state: MockState | None = None) -> tuple[Server[None], MockState]:
    """A search upstream whose tool name collides with Jira's `search` prefix."""
    shared = state or MockState()

    async def on_list_tools(
        ctx: ServerRequestContext[None], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="search",
                    description="Search the web.",
                    input_schema={
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                        "required": ["q"],
                    },
                    annotations=types.ToolAnnotations(read_only_hint=True),
                )
            ]
        )

    async def on_call_tool(
        ctx: ServerRequestContext[None], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        shared.calls.append(
            CallRecord(
                tool=params.name,
                arguments=dict(params.arguments or {}),
                authorization=_header(ctx, "authorization"),
            )
        )
        return _text(f"results for {(params.arguments or {}).get('q')}")

    server: Server[None] = Server(
        "mock-search", version="1.0.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool
    )
    return server, shared


def build_slow_server(*, delay_seconds: float = 30.0) -> tuple[Server[None], MockState]:
    """An upstream that lists tools promptly but never answers a call.

    Arch §40 wants a timeout test, and a timeout is only meaningful against a
    server that is *reachable* and simply does not reply — a refused connection
    exercises a different path entirely.
    """
    shared = MockState()

    async def on_list_tools(
        ctx: ServerRequestContext[None], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="slow_operation",
                    description="Takes far longer than any caller will wait.",
                    input_schema={"type": "object", "properties": {}},
                    annotations=types.ToolAnnotations(read_only_hint=True),
                )
            ]
        )

    async def on_call_tool(
        ctx: ServerRequestContext[None], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        shared.calls.append(CallRecord(tool=params.name, arguments={}, authorization=_header(ctx, "authorization")))
        await asyncio.sleep(delay_seconds)
        return _text("finally")

    server: Server[None] = Server(
        "mock-slow", version="1.0.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool
    )
    return server, shared


def build_failing_server() -> Server[None]:
    """An upstream that refuses `tools/list`, for graceful-degradation tests."""

    async def on_list_tools(
        ctx: ServerRequestContext[None], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        raise RuntimeError("this upstream is broken on purpose")

    async def on_call_tool(
        ctx: ServerRequestContext[None], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        raise RuntimeError("this upstream is broken on purpose")

    return Server("mock-broken", version="1.0.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


# --------------------------------------------------------------------------- serving


@dataclass
class MockUpstream:
    """A running mock server and the URL to reach it at."""

    url: str
    state: MockState
    server: Server[None]

    @property
    def host(self) -> str:
        """Host and port, without the path."""
        return self.url.rsplit("/", 1)[0]


@contextlib.asynccontextmanager
async def serve_mcp(
    server: Server[None],
    state: MockState | None = None,
    *,
    path: str = "/mcp",
) -> AsyncIterator[MockUpstream]:
    """Serve an MCP server over Streamable HTTP on an ephemeral loopback port.

    Binding to port 0 and reading back the assigned port avoids the flakiness of
    guessing a free one when tests run in parallel.
    """
    manager = StreamableHTTPSessionManager(app=server, json_response=False, stateless=False)

    async def handle(scope: Scope, receive: Receive, send: Send) -> None:
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    # A Route, not a Mount: `Mount` would redirect /mcp -> /mcp/, which a
    # no-redirect MCP client refuses. See app/server/asgi.py.
    app = Starlette(routes=[mcp_route(path, handle)], lifespan=lifespan)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", access_log=False)
    uvicorn_server = uvicorn.Server(config)

    task = asyncio.create_task(uvicorn_server.serve())
    try:
        port = await _wait_for_port(uvicorn_server)
        yield MockUpstream(url=f"http://127.0.0.1:{port}{path}", state=state or MockState(), server=server)
    finally:
        uvicorn_server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(task, timeout=10.0)


async def _wait_for_port(server: uvicorn.Server, *, timeout: float = 10.0) -> int:
    """Wait until uvicorn has bound, then report the assigned port."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if server.started and server.servers:
            sockets = server.servers[0].sockets
            if sockets:
                return int(sockets[0].getsockname()[1])
        await asyncio.sleep(0.02)
    raise TimeoutError("Mock upstream did not start within its timeout.")


# --------------------------------------------------------------------------- helpers


def _text(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)])


def _header(ctx: ServerRequestContext[None], name: str) -> str | None:
    """Read a request header the transport attached, if there is one."""
    request: Any = ctx.request
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    value = headers.get(name)
    return str(value) if value is not None else None


ServerFactory = Callable[[], tuple[Server[None], MockState]]
