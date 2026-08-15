"""Hub-native tools and the exposure modes (arch §13, §37).

Two jobs, both about *what an agent sees*.

**Hub tools.** The hub exposes a handful of `hub.*` tools describing itself:
which integrations exist, whether they are healthy, what tools they offer. They
answer questions an agent would otherwise have to guess at, and they are the
navigation surface `DISCOVERY` mode is built on.

What is deliberately absent is the point. Arch §37 forbids `hub.execute_shell`,
`hub.execute_python`, or anything else that runs arbitrary code, and nothing here
mutates configuration: no install, no update, no remove, no enable. Those are
operator actions behind the admin API and the CLI. The strongest thing an agent
can do through `hub.*` is ask for a tool-list refresh, and even that is
scope-gated.

**Exposure modes.** A hub with every integration enabled can present several
hundred tools, which degrades model tool-selection long before it hits any limit.

    FULL       every tool of every healthy integration
    SELECTIVE  only what policy admits — the default
    DISCOVERY  just `hub.*`; an agent searches, then activates what it needs

`DISCOVERY` activation is per-session and additive: an agent that activates
`jira` sees `jira.*` appear on the next `tools/list`, and a
`notifications/tools/list_changed` tells it to look.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import mcp.types as types

from app.core.domain import ExposureMode, HealthStatus
from app.core.errors import HubError, NotAuthorized, ToolNotFound
from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.core.context import Principal
    from app.gateway.discovery import ToolDiscoverer
    from app.gateway.tool_registry import ToolRegistry
    from app.integrations.manager import IntegrationManager

__all__ = ["HUB_NAMESPACE", "HubTools", "SessionActivation"]

log = get_logger(__name__)

HUB_NAMESPACE = "hub"
"""Namespace the hub's own tools live under. Reserved; no integration may claim it."""

_MAX_SEARCH_RESULTS = 50


@dataclass(slots=True)
class SessionActivation:
    """Which integrations one agent session has activated (arch §13).

    Per-session rather than global: two agents connected at once must be able to
    activate different integrations without changing what the other sees.
    """

    integrations: set[str] = field(default_factory=set)

    def activate(self, integration_id: str) -> bool:
        """Record an activation. Returns whether anything changed."""
        if integration_id in self.integrations:
            return False
        self.integrations.add(integration_id)
        return True

    def deactivate(self, integration_id: str) -> bool:
        """Undo an activation. Returns whether anything changed."""
        if integration_id not in self.integrations:
            return False
        self.integrations.discard(integration_id)
        return True

    def is_active(self, integration_id: str) -> bool:
        """Whether this session has activated an integration."""
        return integration_id in self.integrations


class HubTools:
    """Implements the `hub.*` tools and the exposure-mode tool list."""

    def __init__(
        self,
        *,
        manager: IntegrationManager,
        registry: ToolRegistry,
        discoverer: ToolDiscoverer,
    ) -> None:
        self._manager = manager
        self._registry = registry
        self._discoverer = discoverer

    # ------------------------------------------------------------- definitions

    def definitions(self, mode: ExposureMode) -> list[types.Tool]:
        """The `hub.*` tools to advertise for this exposure mode.

        `DISCOVERY` adds `activate_integration`, which has no meaning in the
        other modes because everything permitted is already listed.
        """
        tools = [
            _tool(
                "list_integrations",
                "List the integrations this hub provides, with their health and tool counts. "
                "Call this first to see what is available.",
                {
                    "type": "object",
                    "properties": {
                        "enabled_only": {
                            "type": "boolean",
                            "description": "Only integrations that are switched on. Defaults to true.",
                        }
                    },
                },
                read_only=True,
            ),
            _tool(
                "integration_status",
                "Detailed status for one integration: health, version, credentials, and tool count.",
                {
                    "type": "object",
                    "properties": {"integration": {"type": "string", "description": "Integration id, e.g. 'jira'."}},
                    "required": ["integration"],
                },
                read_only=True,
            ),
            _tool(
                "search_tools",
                "Search every available tool by name or description. Use this to find the right "
                "tool without loading every schema.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Words to match against name and description."},
                        "integration": {"type": "string", "description": "Restrict to one integration."},
                        "limit": {"type": "integer", "description": f"Maximum results (max {_MAX_SEARCH_RESULTS})."},
                    },
                    "required": ["query"],
                },
                read_only=True,
            ),
            _tool(
                "describe_tool",
                "Full definition of one tool, including its input schema and risk classification.",
                {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string", "description": "Qualified name, e.g. 'jira.get_issue'."}
                    },
                    "required": ["tool"],
                },
                read_only=True,
            ),
            _tool(
                "health",
                "Health of every integration, so you can tell an outage from a missing tool.",
                {"type": "object", "properties": {}},
                read_only=True,
            ),
            _tool(
                "refresh_tools",
                "Re-read tool definitions from an upstream. Use when an integration's tools look "
                "stale or a tool you expect is missing.",
                {
                    "type": "object",
                    "properties": {
                        "integration": {
                            "type": "string",
                            "description": "Integration to refresh. Omit to refresh all.",
                        }
                    },
                },
                read_only=False,
            ),
        ]
        if mode is ExposureMode.DISCOVERY:
            tools.append(
                _tool(
                    "activate_integration",
                    "Make one integration's tools available in this session. In discovery mode "
                    "tools are hidden until activated, which keeps the tool list small.",
                    {
                        "type": "object",
                        "properties": {
                            "integration": {"type": "string", "description": "Integration id to activate."}
                        },
                        "required": ["integration"],
                    },
                    read_only=False,
                )
            )
        return tools

    def exposed_tools(self, mode: ExposureMode, activation: SessionActivation) -> list[types.Tool]:
        """The complete tool list for one agent session (arch §13)."""
        tools = self.definitions(mode)
        if mode is not ExposureMode.DISCOVERY:
            tools.extend(self._registry.exposed(mode))
            return tools

        # Discovery mode: only what this session activated, and only what policy
        # would let it call anyway.
        for integration_id in sorted(activation.integrations):
            tools.extend(
                descriptor.as_exposed_tool()
                for descriptor in self._registry.for_integration(integration_id)
                if descriptor.enabled and descriptor.policy_allowed
            )
        return tools

    # ----------------------------------------------------------------- dispatch

    def owns(self, qualified_name: str) -> bool:
        """Whether this name is a hub tool rather than an integration's."""
        return qualified_name.startswith(f"{HUB_NAMESPACE}.")

    async def call(
        self,
        qualified_name: str,
        arguments: dict[str, Any],
        *,
        principal: Principal,
        activation: SessionActivation,
    ) -> tuple[types.CallToolResult, bool]:
        """Run a hub tool.

        Returns:
            The result, and whether the session's tool list changed as a
            consequence (so the caller can send `tools/list_changed`).

        Raises:
            ToolNotFound: No such hub tool.
            NotAuthorized: The caller lacks the scope this tool needs.
        """
        name = qualified_name.removeprefix(f"{HUB_NAMESPACE}.")
        match name:
            case "list_integrations":
                return _ok(self._list_integrations(bool(arguments.get("enabled_only", True)))), False
            case "integration_status":
                return _ok(self._integration_status(str(arguments.get("integration", "")))), False
            case "search_tools":
                return _ok(
                    self._search_tools(
                        query=str(arguments.get("query", "")),
                        integration=arguments.get("integration"),
                        limit=arguments.get("limit"),
                    )
                ), False
            case "describe_tool":
                return _ok(self._describe_tool(str(arguments.get("tool", "")))), False
            case "health":
                return _ok(self._health()), False
            case "refresh_tools":
                payload = await self._refresh(arguments.get("integration"), principal=principal)
                return _ok(payload), True
            case "activate_integration":
                payload, changed = self._activate(str(arguments.get("integration", "")), activation)
                return _ok(payload), changed
            case _:
                raise ToolNotFound(f"No hub tool named {qualified_name!r}.", requested=qualified_name)

    # ------------------------------------------------------------ implementations

    def _list_integrations(self, enabled_only: bool) -> dict[str, Any]:
        rows = []
        for integration in self._manager.all():
            if enabled_only and not integration.enabled:
                continue
            report = self._manager.last_health(integration.id)
            rows.append(
                {
                    "id": integration.id,
                    "name": integration.manifest.name,
                    "namespace": integration.namespace,
                    "description": integration.manifest.description,
                    "enabled": integration.enabled,
                    "health": report.status.value if report else "UNKNOWN",
                    "capabilities": list(integration.manifest.capabilities),
                    "tools": len(self._registry.for_integration(integration.id)),
                }
            )
        return {"integrations": rows, "count": len(rows)}

    def _integration_status(self, integration_id: str) -> dict[str, Any]:
        if not integration_id:
            return {"error": "An `integration` id is required."}
        try:
            integration = self._manager.get(integration_id)
        except HubError:
            # Reported as data rather than an exception: the agent asked a
            # reasonable question and deserves the list of valid answers.
            return {
                "error": f"No integration named {integration_id!r}.",
                "known": [item.id for item in self._manager.all()],
            }

        report = self._manager.last_health(integration_id)
        tools = self._registry.for_integration(integration_id)
        return {
            "id": integration.id,
            "name": integration.manifest.name,
            "namespace": integration.namespace,
            "enabled": integration.enabled,
            "source_type": integration.source_type.value,
            "trust": integration.trust.value,
            "installed": integration.is_installed,
            "version": integration.lock.version_id if integration.lock else None,
            "health": report.status.value if report else "UNKNOWN",
            "health_detail": report.detail if report else "",
            "tool_count": len(tools),
            "tools_allowed": sum(1 for item in tools if item.policy_allowed),
        }

    def _search_tools(self, *, query: str, integration: Any = None, limit: Any = None) -> dict[str, Any]:
        terms = [term for term in query.lower().split() if term]
        if not terms:
            return {"error": "A non-empty `query` is required.", "results": []}

        cap = _MAX_SEARCH_RESULTS
        if isinstance(limit, int) and 0 < limit < cap:
            cap = limit

        scored: list[tuple[int, Any]] = []
        for descriptor in self._registry.all():
            if integration and descriptor.integration_id != integration:
                continue
            if not descriptor.enabled or not descriptor.policy_allowed:
                continue
            haystack = f"{descriptor.qualified_name} {descriptor.description}".lower()
            # A name match is worth more than a description match: an agent
            # searching "issue" wants `create_issue` above anything that merely
            # mentions issues in prose.
            score = sum(
                (3 if term in descriptor.qualified_name.lower() else 0) + (1 if term in haystack else 0)
                for term in terms
            )
            if score:
                scored.append((score, descriptor))

        scored.sort(key=lambda item: (-item[0], item[1].qualified_name))
        return {
            "query": query,
            "results": [descriptor.summary() for _, descriptor in scored[:cap]],
            "total_matches": len(scored),
        }

    def _describe_tool(self, qualified_name: str) -> dict[str, Any]:
        descriptor = self._registry.find(qualified_name)
        if descriptor is None:
            return {"error": f"No tool named {qualified_name!r}."}
        return {
            **descriptor.summary(),
            "description_full": descriptor.description,
            "input_schema": descriptor.tool.input_schema,
            "output_schema": descriptor.tool.output_schema,
        }

    def _health(self) -> dict[str, Any]:
        snapshot = self._manager.health_snapshot()
        return {
            "integrations": {
                integration_id: {
                    "status": report.status.value,
                    "detail": report.detail,
                    "tools": report.tool_count,
                    "latency_ms": round(report.latency_ms, 1) if report.latency_ms else None,
                }
                for integration_id, report in sorted(snapshot.items())
            },
            "healthy": sum(1 for report in snapshot.values() if report.status is HealthStatus.HEALTHY),
            "total": len(snapshot),
        }

    async def _refresh(self, integration: Any, *, principal: Principal) -> dict[str, Any]:
        """Rediscover tools (arch §12).

        Scope-gated: refreshing reconnects to upstreams and is a lever an agent
        could otherwise pull repeatedly.
        """
        if not principal.has_scope("tools:refresh"):
            raise NotAuthorized(
                "Refreshing tools requires the 'tools:refresh' scope.",
                required_scope="tools:refresh",
            )
        if isinstance(integration, str) and integration:
            result = await self._discoverer.refresh(integration, principal=principal)
            return {
                "integration": integration,
                "status": result.status.value,
                "tools": len(result.tools),
                "detail": result.detail,
            }
        results = await self._discoverer.discover_all(principal=principal)
        return {
            "refreshed": {
                key: {"status": value.status.value, "tools": len(value.tools)}
                for key, value in sorted(results.items())
            },
            "total_tools": sum(len(value.tools) for value in results.values()),
        }

    def _activate(self, integration_id: str, activation: SessionActivation) -> tuple[dict[str, Any], bool]:
        if not integration_id:
            return {"error": "An `integration` id is required."}, False
        try:
            integration = self._manager.get(integration_id)
        except HubError:
            return (
                {
                    "error": f"No integration named {integration_id!r}.",
                    "available": [item.id for item in self._manager.enabled()],
                },
                False,
            )
        if not integration.enabled:
            return {"error": f"Integration {integration_id!r} is disabled."}, False

        changed = activation.activate(integration_id)
        tools = [
            item.qualified_name
            for item in self._registry.for_integration(integration_id)
            if item.enabled and item.policy_allowed
        ]
        return (
            {
                "activated": integration_id,
                "already_active": not changed,
                "tools_now_available": tools,
                "count": len(tools),
            },
            changed,
        )


# --------------------------------------------------------------------------- helpers


def _tool(name: str, description: str, schema: dict[str, Any], *, read_only: bool) -> types.Tool:
    """Build one hub tool definition."""
    return types.Tool(
        name=f"{HUB_NAMESPACE}.{name}",
        description=description,
        input_schema=schema,
        annotations=types.ToolAnnotations(read_only_hint=read_only, destructive_hint=False),
    )


def _ok(payload: dict[str, Any] | Sequence[Any]) -> types.CallToolResult:
    """Render a hub tool's answer as an MCP result.

    Both text and `structured_content` are populated: the text is what a model
    reads, and the structured form is what a programmatic client can parse
    without re-deriving the schema.
    """
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, indent=2, default=str))],
        structured_content=payload if isinstance(payload, dict) else {"items": list(payload)},
    )
